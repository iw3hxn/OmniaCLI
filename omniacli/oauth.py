# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""OAuth 2.1 for the MCP clients that cannot send a static bearer header:
claude.ai (and Claude Desktop's, mobile's connectors), ChatGPT; Claude Code,
Codex, VS Code and the others may use it too (README, MCP interface, OAuth).

OmniaCLI is its own authorization server: no external identity provider.
The person logs in on OmniaCLI's own login (password, two-step verification
where the user has it), then agrees on a page of OmniaCLI to what the client
may do: a level (read; act on the tests and stagings; also the productions,
each call with confirm), never more than the person's own role, a workspace's
user only its workspace, optionally some servers only, for some days.

What the client then gets is, for every rule of the tokens (apiguard.py,
mcp_server.py), a token like those of the token page: its level, never a CI,
its workspace and its servers. Unlike those it lives ACCESS_SECONDS and is
renewed with a refresh token that is rotated at every use (a public client
keeps no secret): a refresh token used twice ends the grant, both copies.

The documents followed, as read on 23/11/2026: the MCP authorization
specification 2025-11-25; RFC 6749, 7636 (PKCE, S256 only), 7591 (dynamic
registration), 7009 (revocation), 8414 (server metadata), 8707 (resource:
the token is bound to the MCP URL), 9207 (iss in the answer), 9728
(protected resource metadata), 8252 (loopback redirects); the Client ID
Metadata Documents of the MCP specification; claude.com/docs/connectors/
building/authentication; developers.openai.com/plugins/build/auth.

Kept in FILE next to the jobs (mode 600): the clients, the grants, the
SHA-256 of their tokens (never a token in clear). The authorization requests
waiting for the person and the codes waiting for the client live in memory
only: a restart asks the person again.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import os
import re
import secrets
import socket
import threading
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx

from .auth import ROLES

FILE = "oauth.json"
# the names of the tokens an OAuth grant gives start so: no token of the token page can have one (_ACCESS_NAME)
PREFIX = "oauth:"

ACCESS_SECONDS = 3600  # an access token
REFRESH_SECONDS = 30 * 86400  # a refresh token, renewed at each use, never beyond the grant's end
CODE_SECONDS = 300  # an authorization code, used once
REQUEST_SECONDS = 900  # the person's time on the login and the consent page
DAYS = (1, 7, 30, 90)  # how long a person may let a client in at most
DEFAULT_DAYS = 30
# the level of a grant, as the token page names them, and the role it gives (apiguard.LEVELS)
LEVEL_ROLES = {"read": "user", "write": "developer", "production": "admin"}
SCOPES = tuple(LEVEL_ROLES)
OFFLINE = "offline_access"  # asked by some clients for a refresh token: one is always given
# what a dynamic registration may ask
AUTH_METHODS = ("none", "client_secret_post", "client_secret_basic")
# per address and minute: registrations, token requests (claude.ai's and ChatGPT's come from few addresses)
REGISTER_PER_MINUTE = 30
TOKEN_PER_MINUTE = 300
MAX_CLIENTS = 500  # registered clients kept: the oldest never used go first
MAX_GRANTS_PER_USER = 50
SPENT_KEPT = 20  # refresh tokens already used, kept to tell a reuse
# a client ID metadata document (the MCP specification): fetched over https only, so much at most, so fast
METADATA_MAX_BYTES = 64 * 1024
METADATA_TIMEOUT = 5.0
METADATA_TTL = 3600  # seconds a fetched document is used before it is fetched again
NAME_MAX = 100
_VERIFIER = re.compile(r"[A-Za-z0-9\-._~]{43,128}")
_CHALLENGE = re.compile(r"[A-Za-z0-9\-_]{43}")
_SCHEME = re.compile(r"[a-z][a-z0-9+.\-]*")
# schemes a redirect may never have: they run code or reach files and other protocols
_BAD_SCHEMES = {"javascript", "data", "file", "vbscript", "about", "blob", "ftp", "ws", "wss", "chrome", "view-source"}
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


class OAuthError(Exception):
    """A refusal as RFC 6749 says it: its error code, a description, the HTTP status."""

    def __init__(self, error: str, description: str, status: int = 400, revoked: str = ""):
        super().__init__(description)
        self.error, self.description, self.status = error, description, status
        # the name of the grant this refusal ended (a code or a refresh token used twice)
        self.revoked = revoked

    def body(self) -> dict:
        return {"error": self.error, "error_description": self.description}


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def clean_text(value, limit: int = NAME_MAX) -> str:
    """A name a client gives itself: one line of printable characters, so long at most."""
    text = "".join(ch for ch in str(value or "") if ch.isprintable()).strip()
    return text[:limit]


def slug(text: str, limit: int = 20) -> str:
    return (re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "client")[:limit].strip("-") or "client"


# --- redirect URIs ---------------------------------------------------------
#
# Only the redirect URIs a client registered (its dynamic registration, or its
# metadata document), compared exactly; a loopback one (http://localhost or
# 127.0.0.1, RFC 8252) with any port, since a native client (Claude Code,
# Codex, VS Code) listens on a port of the moment. Not a list of known
# clients: a fixed list would refuse every client that comes next (Cursor,
# VS Code, a new ChatGPT callback), while an exact match already keeps a code
# from going anywhere the client did not say, and the consent page shows the
# person the host it goes to.

def redirect_problem(uri) -> str | None:
    """Why uri may not be a redirect URI, None when it may."""
    if not isinstance(uri, str) or not uri or len(uri) > 2000:
        return "a redirect URI is a URL of at most 2000 characters"
    try:
        parts = urlsplit(uri)
        parts.port  # noqa: B018 - a port that is not a number raises
    except ValueError:
        return f"{uri} is not a URL"
    if parts.fragment or "#" in uri:
        return f"{uri}: a redirect URI has no fragment"
    scheme = parts.scheme.lower()
    if scheme == "https":
        return None if parts.hostname else f"{uri}: no host"
    if scheme == "http":
        if (parts.hostname or "") in LOOPBACK_HOSTS:
            return None
        return f"{uri}: http only to this computer (localhost, 127.0.0.1): anything else is https"
    if not _SCHEME.fullmatch(scheme) or scheme in _BAD_SCHEMES:
        return f"{uri}: the scheme {scheme or '(none)'} is not one of a redirect"
    # a private-use scheme of a native app (RFC 8252 §7.1), e.g. cursor://
    return None


def loopback(uri: str) -> bool:
    parts = urlsplit(uri)
    return parts.scheme.lower() == "http" and (parts.hostname or "") in LOOPBACK_HOSTS


def redirect_matches(registered: list, given: str) -> bool:
    if given in registered:
        return True
    if not loopback(given):
        return False
    want = urlsplit(given)
    for uri in registered:
        if not loopback(uri):
            continue
        have = urlsplit(uri)
        # the port is the one of the moment (RFC 8252 §7.3): the rest exactly
        if (have.hostname, have.path or "/", have.query) == (want.hostname, want.path or "/", want.query):
            return True
    return False


def redirect_host(uri: str) -> str:
    """What the consent page says of where the answer goes."""
    parts = urlsplit(uri)
    if parts.scheme.lower() in ("http", "https"):
        return parts.hostname or uri
    return f"{parts.scheme}://{parts.netloc}" if parts.netloc else f"{parts.scheme}:"


def with_query(uri: str, params: dict) -> str:
    """uri with params added to its query (the redirect of an answer)."""
    parts = urlsplit(uri)
    query = urlencode({k: v for k, v in params.items() if v is not None and v != ""})
    joined = f"{parts.query}&{query}" if parts.query else query
    return urlunsplit((parts.scheme, parts.netloc, parts.path, joined, ""))


# --- client ID metadata documents: an outbound request, guarded -------------

def metadata_url_problem(url: str) -> str | None:
    """Why url may not be a client ID metadata document's, None when it may."""
    if len(url) > 2048:
        return "the client_id URL is longer than 2048 characters"
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018
    except ValueError:
        return "the client_id is not a URL"
    if parts.scheme != "https":
        return "a client_id URL is https"
    if not parts.hostname or parts.username or parts.password:
        return "a client_id URL has a host and no credentials"
    if not parts.path or parts.path == "/":
        return "a client_id URL has a path"
    if parts.fragment or "#" in url or parts.query:
        return "a client_id URL has neither query nor fragment"
    if "/./" in parts.path or "/../" in parts.path or parts.path.endswith(("/.", "/..")):
        return "a client_id URL has no dot segments"
    return None


def public_address(address: str) -> bool:
    """An address of the internet: never this machine, a private network, a
    link-local, shared, reserved or multicast one (the SSRF guard)."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


# the network a document is fetched through (the tests put theirs)
TRANSPORT: httpx.AsyncBaseTransport | None = None


async def _resolve(host: str, port: int) -> list[str]:
    try:
        found = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM), METADATA_TIMEOUT)
    except (OSError, asyncio.TimeoutError):
        return []
    return list(dict.fromkeys(item[4][0] for item in found))


async def fetch_metadata(url: str) -> dict:
    """The client ID metadata document at url (an OAuthError when it cannot
    be had): its host must be of the internet only, every address it resolves
    to; the request goes to the address checked (no second resolution that
    could answer another), with no redirect, no proxy, METADATA_TIMEOUT and
    METADATA_MAX_BYTES at most."""
    problem = metadata_url_problem(url)
    if problem:
        raise OAuthError("invalid_client", problem)
    parts = urlsplit(url)
    host, port = parts.hostname, parts.port or 443
    addresses = await _resolve(host, port)
    if not addresses:
        raise OAuthError("invalid_client", f"{host} does not resolve")
    refused = [a for a in addresses if not public_address(a)]
    if refused:
        raise OAuthError("invalid_client", f"{host} resolves to an address that is not of the internet ({refused[0]}): "
                         "a client's metadata is not fetched from a private network")
    address = addresses[0]
    netloc = f"[{address}]" if ":" in address else address
    target = urlunsplit(("https", f"{netloc}:{port}", parts.path, "", ""))
    headers = {"Host": parts.netloc.rsplit("@", 1)[-1], "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(transport=TRANSPORT, timeout=METADATA_TIMEOUT, follow_redirects=False,
                                     trust_env=False) as client:
            async with client.stream("GET", target, headers=headers,
                                     extensions={"sni_hostname": host}) as resp:
                if resp.status_code != 200:
                    raise OAuthError("invalid_client", f"{url} answered {resp.status_code}")
                body = b""
                async for chunk in resp.aiter_bytes():
                    body += chunk
                    if len(body) > METADATA_MAX_BYTES:
                        raise OAuthError("invalid_client", f"{url} is larger than {METADATA_MAX_BYTES} bytes")
    except httpx.HTTPError as exc:
        raise OAuthError("invalid_client", f"{url} cannot be fetched: {type(exc).__name__}") from None
    try:
        data = json.loads(body)
    except ValueError:
        raise OAuthError("invalid_client", f"{url} is not JSON") from None
    if not isinstance(data, dict):
        raise OAuthError("invalid_client", f"{url} is not a JSON object")
    return data


def check_metadata(url: str, data: dict) -> dict:
    """The client a metadata document describes: its client_id must be its
    own URL, exactly; its redirect URIs each a valid one; a public client
    (no secret: a document is public)."""
    if data.get("client_id") != url:
        raise OAuthError("invalid_client", "the document's client_id is not its own URL")
    uris = data.get("redirect_uris")
    if not isinstance(uris, list) or not uris or len(uris) > 20:
        raise OAuthError("invalid_client", "the document has no redirect_uris (1-20)")
    for uri in uris:
        problem = redirect_problem(uri)
        if problem:
            raise OAuthError("invalid_client", problem)
    method = data.get("token_endpoint_auth_method")
    if isinstance(method, str) and method.startswith("client_secret"):
        raise OAuthError("invalid_client", "a client of a metadata document has no secret")
    return {"id": url, "kind": "metadata", "name": clean_text(data.get("client_name")) or urlsplit(url).hostname,
            "redirect_uris": list(dict.fromkeys(uris)), "auth": "none", "secret": ""}


# --- the store ---------------------------------------------------------------

class OAuthStore:
    """The clients and the grants (FILE), the requests and codes (memory).

    person(grant): the most a grant may do now, as its person is now (a role
    of ROLES: their role on the machine, or in the grant's workspace), None
    when the person is gone or no longer of that workspace; set by the app.
    """

    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self.clock = clock
        self.person: Callable[[dict], str | None] = lambda grant: None
        self._lock = threading.RLock()
        self.clients: dict[str, dict] = {}
        self.grants: dict[str, dict] = {}
        self.requests: dict[str, dict] = {}
        self.codes: dict[str, dict] = {}
        self._used_codes: dict[str, tuple[str, float]] = {}
        self._access: dict[str, str] = {}
        self._refresh: dict[str, str] = {}
        self._spent: dict[str, str] = {}
        self._names: dict[str, str] = {}
        self._saved_use: dict[str, float] = {}
        self._load()

    # -- the file
    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        self.clients = {k: v for k, v in (data.get("clients") or {}).items() if isinstance(v, dict)}
        self.grants = {k: v for k, v in (data.get("grants") or {}).items() if isinstance(v, dict) and v.get("name")}
        self._index()

    def _index(self) -> None:
        self._access, self._refresh, self._spent, self._names = {}, {}, {}, {}
        for gid, grant in self.grants.items():
            self._names[grant["name"]] = gid
            for h in grant.get("access") or {}:
                self._access[h] = gid
            if grant.get("refresh"):
                self._refresh[grant["refresh"]] = gid
            for h in grant.get("spent") or []:
                self._spent[h] = gid

    def _save(self) -> None:
        now = self.clock()
        for grant in self.grants.values():
            grant["access"] = {h: t for h, t in (grant.get("access") or {}).items() if t > now}
        self._index()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"clients": self.clients, "grants": self.grants}, fh)
        os.replace(tmp, self.path)

    def _expire(self) -> None:
        """The grants past their end or with no refresh left, the requests and codes past theirs."""
        now = self.clock()
        gone = [gid for gid, g in self.grants.items()
                if g.get("ends", 0) <= now or (g.get("refresh_ends", 0) <= now
                                                and not any(t > now for t in (g.get("access") or {}).values()))
                # its person deleted, or out of its workspace: the grant goes with it (the pages list it no more)
                or self.person(g) is None]
        for gid in gone:
            del self.grants[gid]
        self.requests = {k: v for k, v in self.requests.items() if v["created"] + REQUEST_SECONDS > now}
        self.codes = {k: v for k, v in self.codes.items() if v["expires"] > now}
        self._used_codes = {k: v for k, v in self._used_codes.items() if v[1] > now}
        if gone:
            self._save()

    # -- clients
    def register(self, meta) -> dict:
        """A dynamic registration (RFC 7591): the answer, with the client's
        secret in clear for a confidential one (its SHA-256 is kept)."""
        if not isinstance(meta, dict):
            raise OAuthError("invalid_client_metadata", "the registration is a JSON object")
        uris = meta.get("redirect_uris")
        if not isinstance(uris, list) or not uris or len(uris) > 20:
            raise OAuthError("invalid_redirect_uri", "redirect_uris: a list of 1-20 URIs")
        for uri in uris:
            problem = redirect_problem(uri)
            if problem:
                raise OAuthError("invalid_redirect_uri", problem)
        method = meta.get("token_endpoint_auth_method") or "none"
        if method not in AUTH_METHODS:
            raise OAuthError("invalid_client_metadata",
                             f"token_endpoint_auth_method: one of {', '.join(AUTH_METHODS)}")
        grants = meta.get("grant_types") or ["authorization_code", "refresh_token"]
        if not isinstance(grants, list) or "authorization_code" not in grants or not set(grants) <= {
                "authorization_code", "refresh_token"}:
            raise OAuthError("invalid_client_metadata", "grant_types: authorization_code, and refresh_token")
        responses = meta.get("response_types") or ["code"]
        if responses != ["code"]:
            raise OAuthError("invalid_client_metadata", "response_types: code only")
        now = self.clock()
        client_id = "dyn-" + secrets.token_urlsafe(18)
        secret = secrets.token_urlsafe(32) if method != "none" else ""
        client = {"id": client_id, "kind": "dynamic", "name": clean_text(meta.get("client_name")),
                  "redirect_uris": list(dict.fromkeys(uris)), "auth": method, "secret": digest(secret) if secret else "",
                  "created": now, "used": 0}
        with self._lock:
            self._make_room()
            self.clients[client_id] = client
            self._save()
        answer = {"client_id": client_id, "client_id_issued_at": int(now), "redirect_uris": client["redirect_uris"],
                  "token_endpoint_auth_method": method, "grant_types": ["authorization_code", "refresh_token"],
                  "response_types": ["code"]}
        if client["name"]:
            answer["client_name"] = client["name"]
        if secret:
            answer.update(client_secret=secret, client_secret_expires_at=0)
        return answer

    def _make_room(self) -> None:
        dynamic = [c for c in self.clients.values() if c.get("kind") == "dynamic"]
        if len(dynamic) < MAX_CLIENTS:
            return
        held = {g.get("client_id") for g in self.grants.values()}
        free = sorted((c for c in dynamic if c["id"] not in held), key=lambda c: (c.get("used", 0), c.get("created", 0)))
        if not free:
            raise OAuthError("temporarily_unavailable", "too many clients are registered", 503)
        for client in free[:max(1, len(dynamic) - MAX_CLIENTS + 1)]:
            del self.clients[client["id"]]

    def client(self, client_id: str) -> dict | None:
        return self.clients.get(client_id) if isinstance(client_id, str) else None

    def keep_metadata_client(self, client: dict) -> dict:
        """A client of a metadata document, as fetched now (it replaces what was kept of it)."""
        with self._lock:
            kept = self.clients.get(client["id"]) or {}
            client = {**client, "created": kept.get("created", self.clock()), "used": kept.get("used", 0),
                      "fetched": self.clock()}
            self.clients[client["id"]] = client
            self._save()
        return client

    def metadata_fresh(self, client_id: str) -> dict | None:
        client = self.clients.get(client_id)
        if client and client.get("kind") == "metadata" and client.get("fetched", 0) + METADATA_TTL > self.clock():
            return client
        return None

    def authenticate(self, client: dict, secret: str | None, method: str) -> None:
        """The client's authentication at the token endpoint (an OAuthError when it fails)."""
        if client.get("auth", "none") == "none":
            if secret:
                raise OAuthError("invalid_client", "this client is public: it sends no secret", 401)
            return
        if method != client["auth"] or not secret or not secrets.compare_digest(digest(secret), client["secret"]):
            raise OAuthError("invalid_client", "the client's authentication failed", 401)

    # -- the person's consent
    def new_request(self, params: dict) -> str:
        with self._lock:
            self._expire()
            rid = secrets.token_urlsafe(24)
            self.requests[rid] = {**params, "created": self.clock()}
            return rid

    def request(self, rid: str) -> dict | None:
        with self._lock:
            self._expire()
            return self.requests.get(rid) if isinstance(rid, str) else None

    def drop_request(self, rid: str) -> None:
        with self._lock:
            self.requests.pop(rid, None)

    def new_code(self, request: dict, user: str, workspace: str, level: str, servers: list, days: int,
                 person: str = "") -> str:
        """The code of a consent given: what the person chose, for the client's token request. person: a
        fingerprint of who the person is now (the app's: its password hash's SHA-256)."""
        code = secrets.token_urlsafe(32)
        with self._lock:
            self.codes[digest(code)] = {"request": request, "user": user, "workspace": workspace, "level": level,
                                        "servers": list(servers), "days": days, "person": person,
                                        "expires": self.clock() + CODE_SECONDS}
        return code

    # -- tokens
    def _tokens(self, grant: dict) -> dict:
        now = self.clock()
        access, refresh = "oat_" + secrets.token_urlsafe(32), "ort_" + secrets.token_urlsafe(32)
        grant.setdefault("access", {})[digest(access)] = now + ACCESS_SECONDS
        if grant.get("refresh"):
            grant["spent"] = ([grant["refresh"]] + list(grant.get("spent") or []))[:SPENT_KEPT]
        grant["refresh"] = digest(refresh)
        grant["refresh_ends"] = min(now + REFRESH_SECONDS, grant["ends"])
        scope = grant["level"]
        return {"access_token": access, "token_type": "Bearer", "expires_in": ACCESS_SECONDS,
                "refresh_token": refresh, "scope": scope}

    def exchange(self, code: str, client: dict, redirect_uri: str, verifier: str, resource: str) -> tuple[dict, dict]:
        """The authorization code for the tokens: (the grant made, the answer)."""
        with self._lock:
            self._expire()
            key = digest(code or "")
            found = self.codes.pop(key, None)
            if found is None:
                used = self._used_codes.get(key)
                if used is not None:
                    # a code used again: what it gave ends too (RFC 6749 §4.1.2)
                    gone = self.revoke(used[0], "its code was used twice")
                    raise OAuthError("invalid_grant", "this code was used already: the grant it gave is revoked",
                                     revoked=gone["name"] if gone else "")
                raise OAuthError("invalid_grant", "unknown or expired code")
            req = found["request"]
            if req["client_id"] != client["id"]:
                raise OAuthError("invalid_grant", "the code is of another client")
            if redirect_uri != req["redirect_uri"]:
                raise OAuthError("invalid_grant", "redirect_uri is not the one of the authorization request")
            if not isinstance(verifier, str) or not _VERIFIER.fullmatch(verifier) or not secrets.compare_digest(
                    s256(verifier), req["code_challenge"]):
                raise OAuthError("invalid_grant", "the code_verifier does not match the code_challenge (PKCE, S256)")
            if resource and resource != req["resource"]:
                raise OAuthError("invalid_target", "resource is not the one of the authorization request")
            now = self.clock()
            gid = secrets.token_hex(8)
            user_grants = sorted((g for g in self.grants.values() if g["user"] == found["user"]),
                                 key=lambda g: g.get("used") or g["created"])
            for old in user_grants[:max(0, len(user_grants) - MAX_GRANTS_PER_USER + 1)]:
                self.revoke(old["id"], "too many grants of its person: the oldest used goes")
            grant = {"id": gid, "name": f"{PREFIX}{found['user']}:{slug(client.get('name') or 'client')}:{gid[:4]}",
                     "user": found["user"], "workspace": found["workspace"], "client_id": client["id"],
                     "client_name": client.get("name") or "", "client_kind": client.get("kind", "dynamic"),
                     "redirect_host": redirect_host(req["redirect_uri"]), "level": found["level"],
                     "servers": found["servers"], "resource": req["resource"], "created": now,
                     "ends": now + found["days"] * 86400, "days": found["days"], "used": 0, "refreshed": 0,
                     # who the person was (person() compares it): another user made with the same name later
                     # does not inherit it, and a new password ends it as it ends the sessions
                     "person": found.get("person", "")}
            answer = self._tokens(grant)
            self.grants[gid] = grant
            self._used_codes[key] = (gid, now + CODE_SECONDS)
            client["used"] = now
            self._save()
            return grant, answer

    def refresh(self, token: str, client: dict, resource: str, scope: str) -> tuple[dict, dict]:
        """A refresh token for new tokens; the refresh token is rotated. One used
        again (a copy of an old one) ends its grant: whoever holds it, the
        client or a thief, logs in again."""
        with self._lock:
            self._expire()
            key = digest(token or "")
            gid = self._refresh.get(key)
            if gid is None:
                spent = self._spent.get(key)
                if spent is not None and spent in self.grants:
                    gone = self.revoke(spent, "a refresh token was used twice")
                    raise OAuthError("invalid_grant", "this refresh token was used already: the grant is revoked",
                                     revoked=gone["name"] if gone else "")
                raise OAuthError("invalid_grant", "unknown, expired or revoked refresh token")
            grant = self.grants[gid]
            now = self.clock()
            if grant["client_id"] != client["id"]:
                raise OAuthError("invalid_grant", "the refresh token is of another client")
            if grant.get("refresh_ends", 0) <= now or grant["ends"] <= now:
                self.revoke(gid, "ended")
                raise OAuthError("invalid_grant", "the grant has ended: the person agrees again", revoked=grant["name"])
            if resource and resource != grant["resource"]:
                raise OAuthError("invalid_target", "resource is not the one of the grant")
            asked = set((scope or "").split()) - {OFFLINE}
            if asked and not asked <= set(SCOPES[:SCOPES.index(grant["level"]) + 1]):
                raise OAuthError("invalid_scope", f"the grant is of level {grant['level']}")
            if self.person(grant) is None:
                self.revoke(gid, "its person is gone or no longer of its workspace")
                raise OAuthError("invalid_grant", "the person of this grant cannot use it any more",
                                 revoked=grant["name"])
            answer = self._tokens(grant)
            grant["refreshed"] = grant.get("refreshed", 0) + 1
            self._save()
            return grant, answer

    def grant_of_refresh(self, token: str) -> dict | None:
        """The grant a refresh token is (or was) of: for what the access log says."""
        key = digest(token or "")
        gid = self._refresh.get(key) or self._spent.get(key)
        return self.grants.get(gid) if gid else None

    def grant_of(self, token: str) -> dict | None:
        """The grant of a valid access token, None otherwise; its use is noted."""
        key = digest(token)
        gid = self._access.get(key)
        if gid is None:
            return None
        with self._lock:
            grant = self.grants.get(gid)
            now = self.clock()
            if grant is None or (grant.get("access") or {}).get(key, 0) <= now or grant["ends"] <= now:
                return None
            if self.person(grant) is None:
                return None
            grant["used"] = now
            if now - self._saved_use.get(gid, 0) > 60:
                # the last use on the page, saved once a minute at most
                self._saved_use[gid] = now
                try:
                    self._save()
                except OSError:
                    pass
            return grant

    def name_of(self, token: str) -> str | None:
        grant = self.grant_of(token)
        return grant["name"] if grant else None

    def by_name(self, name: str) -> dict | None:
        gid = self._names.get(name)
        return self.grants.get(gid) if gid else None

    def role_of(self, name: str) -> str:
        """The role of a grant's token now: its level, never more than its person's role now."""
        grant = self.by_name(name)
        if grant is None:
            return "user"
        cap = self.person(grant) or "user"
        level = LEVEL_ROLES.get(grant.get("level"), "user")
        return min(level, cap, key=ROLES.index)

    def revoke(self, gid: str, why: str = "") -> dict | None:
        """The grant ends, with every token of it (its refresh chain too)."""
        with self._lock:
            grant = self.grants.pop(gid, None)
            if grant is not None:
                self._used_codes = {k: v for k, v in self._used_codes.items() if v[0] != gid}
                self._save()
            return grant

    def revoke_token(self, token: str, client: dict) -> dict | None:
        """RFC 7009: an access or refresh token of client ends its grant."""
        key = digest(token or "")
        gid = self._access.get(key) or self._refresh.get(key) or self._spent.get(key)
        grant = self.grants.get(gid) if gid else None
        if grant is None or grant["client_id"] != client["id"]:
            return None
        return self.revoke(gid, "revoked by its client")

    def listed(self, user: str | None = None, workspace: str | None = None) -> list[dict]:
        """The grants for a page: a person's, a workspace's, or all of them."""
        with self._lock:
            self._expire()
            found = [g for g in self.grants.values()
                     if (user is None or g["user"] == user) and (workspace is None or g["workspace"] == workspace)]
        return sorted(found, key=lambda g: -g["created"])
