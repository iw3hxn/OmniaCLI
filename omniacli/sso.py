# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Single sign-on with OpenID Connect, one identity provider per workspace
(README, Single sign-on): the customer's own (Microsoft Entra ID, Google
Workspace, Okta, Keycloak, authentik…) logs its people in at the customers'
portal, with «Log in with <provider>».

The flow is OpenID Connect Core 1.0's authorization code flow with PKCE
(RFC 7636, S256): a `state` bound to the browser by a cookie of its own and
taken once, a `nonce` checked in the ID token. The ID token comes from the
token endpoint (a confidential client: client_secret_basic, or
client_secret_post when the provider lists only that) and is checked here:
its signature against the provider's JWKS (RS256/384/512, PS256/384/512,
ES256/384/512; never "none" nor an HMAC, whose key would be the client
secret), iss exactly the issuer, aud the client id (azp too when there are
several), exp and iat with SKEW of clock, the nonce. Its e-mail must be
verified by the provider (email_verified true) and of one of the domains the
workspace allows.

Why not authlib: what OmniaCLI needs of it is these checks, while its every
request to the provider (the discovery document, the token endpoint, the
keys) must go through the SSRF guard of oauth.py (https, the addresses of
the internet only, the address checked is the one connected to, no
redirect, a size and a time cap), which authlib's clients do not; the
signature is checked with `cryptography`, already a dependency. The code is
small and every check of OIDC Core §3.1.3.7 is named where it is made.

Kept in FILE next to the users file (mode 600): each workspace's provider
(its name, issuer, client id, client secret, allowed domains, the role a
new person gets, whether its MFA counts as the second step) and the
identities that logged in (issuer + sub → the workspace's user, with its
e-mail). The flows waiting for the provider's answer live in memory only.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import ssl
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from . import oauth

FILE = "sso.json"
COOKIE = "omniacli_sso"
# the workspace whose button the portal's login shows again (the last single sign-on of this browser)
LAST_COOKIE = "omniacli_sso_ws"
FLOW_SECONDS = 10 * 60
DISCOVERY_TTL = 3600
SKEW = 120  # seconds of clock between this machine and the provider
SCOPES = "openid email profile"
NAME_MAX = 40
MAX_DOMAINS = 10
MAX_BYTES = 256 * 1024
TIMEOUT = 10.0
ROLES = ("user", "developer", "admin")
DEFAULT_ROLE = "user"
_HASHES = {"256": hashes.SHA256, "384": hashes.SHA384, "512": hashes.SHA512}
_CURVES = {"ES256": ("P-256", ec.SECP256R1, 32), "ES384": ("P-384", ec.SECP384R1, 48),
           "ES512": ("P-521", ec.SECP521R1, 66)}
ALGORITHMS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512", *_CURVES)
_DOMAIN = re.compile(r"(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
_CLIENT_ID = re.compile(r"[\x21-\x7e]{1,255}")


class SsoError(Exception):
    """Why a configuration or a login is refused (said to the person in its language, logged in English)."""


def b64decode(text: str) -> bytes:
    if not isinstance(text, str) or not re.fullmatch(r"[A-Za-z0-9_-]*", text):
        raise SsoError("the token is not base64url")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


def issuer_problem(url: str) -> str | None:
    """Why url may not be an issuer, None when it may: https, a host, no
    credentials, no query, no fragment (OIDC Discovery §2)."""
    if not url or len(url) > 512:
        return "The issuer is an https:// address of the provider"
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018
    except ValueError:
        return "The issuer is an https:// address of the provider"
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        return "The issuer is an https:// address of the provider, without user or password"
    if parts.query or parts.fragment or "#" in url or "?" in url:
        return "The issuer has neither query nor fragment"
    return None


def domains_of(text) -> list[str]:
    """The domains of a comma- or space-separated list, lower case; SsoError for one that is not a domain."""
    found = []
    for item in re.split(r"[\s,;]+", str(text or "").strip().lower()):
        if not item:
            continue
        item = item.lstrip("@")
        if not _DOMAIN.fullmatch(item):
            raise SsoError(f"{item} is not an e-mail domain")
        if item not in found:
            found.append(item)
    if not found:
        raise SsoError("Give the e-mail domain of the people who may log in (example.com)")
    if len(found) > MAX_DOMAINS:
        raise SsoError(f"At most {MAX_DOMAINS} domains")
    return found


def clean_name(value) -> str:
    text = " ".join(str(value or "").split())
    return "".join(c for c in text if c.isprintable())[:NAME_MAX]


def provider_of(data: dict, current: dict | None) -> dict:
    """A provider as the form gives it, checked (SsoError); an empty client
    secret keeps the one saved."""
    name = clean_name(data.get("name"))
    if not name:
        raise SsoError("Give the provider a name: the button says «Log in with» it")
    issuer = str(data.get("issuer") or "").strip()
    problem = issuer_problem(issuer)
    if problem:
        raise SsoError(problem)
    client_id = str(data.get("client_id") or "").strip()
    if not _CLIENT_ID.fullmatch(client_id):
        raise SsoError("Give the client ID the provider gave for OmniaCLI")
    secret = str(data.get("client_secret") or "")
    if not secret:
        if current is None or not current.get("client_secret"):
            raise SsoError("Give the client secret the provider gave for OmniaCLI")
        secret = current["client_secret"]
    if len(secret) > 1024 or not secret.isprintable():
        raise SsoError("The client secret is not one a provider gives")
    role = str(data.get("role") or DEFAULT_ROLE)
    if role not in ROLES:
        raise SsoError(f"The role must be one of {', '.join(ROLES)}")
    return {"name": name, "issuer": issuer, "client_id": client_id, "client_secret": secret,
            "domains": domains_of(data.get("domains")), "role": role, "mfa": bool(data.get("mfa"))}


def shown(provider: dict | None) -> dict | None:
    """A provider as the pages show it: never its secret."""
    if provider is None:
        return None
    return {k: v for k, v in provider.items() if k != "client_secret"} | {"secret_set": bool(provider.get("client_secret"))}


class SsoStore:
    """The providers and the identities (the file of the module's docstring)."""

    def __init__(self, path: Path, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self._lock = threading.Lock()
        self._data: dict = {"providers": {}, "identities": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._data["providers"] = {k: v for k, v in (data.get("providers") or {}).items() if isinstance(v, dict)}
                self._data["identities"] = {k: v for k, v in (data.get("identities") or {}).items() if isinstance(v, dict)}
        except (OSError, ValueError):
            pass

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh, indent=1, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def provider(self, workspace: str) -> dict | None:
        found = self._data["providers"].get(workspace)
        return dict(found) if found else None

    def set_provider(self, workspace: str, provider: dict, by: str) -> None:
        with self._lock:
            before = self._data["providers"].get(workspace) or {}
            self._data["providers"][workspace] = {**provider, "created": before.get("created") or self.clock(),
                                                  "changed": self.clock(), "by": by}
            self._save()

    def remove(self, workspace: str) -> bool:
        """The workspace's provider away, and its identities (a new one starts without)."""
        with self._lock:
            found = self._data["providers"].pop(workspace, None)
            self._data["identities"].pop(workspace, None)
            if found is not None:
                self._save()
        return found is not None

    def keep(self, workspaces, users) -> None:
        """Only the workspaces and the users that exist."""
        with self._lock:
            before = json.dumps(self._data, sort_keys=True)
            self._data["providers"] = {w: p for w, p in self._data["providers"].items() if w in workspaces}
            self._data["identities"] = {
                w: {k: i for k, i in ids.items() if i.get("user") in users}
                for w, ids in self._data["identities"].items() if w in workspaces}
            if json.dumps(self._data, sort_keys=True) != before:
                self._save()

    @staticmethod
    def _key(issuer: str, sub: str) -> str:
        return f"{issuer}|{sub}"

    def identity(self, workspace: str, issuer: str, sub: str) -> dict | None:
        found = (self._data["identities"].get(workspace) or {}).get(self._key(issuer, sub))
        return dict(found) if found else None

    def link(self, workspace: str, issuer: str, sub: str, user: str, email: str) -> None:
        with self._lock:
            ids = self._data["identities"].setdefault(workspace, {})
            key = self._key(issuer, sub)
            ids[key] = {"user": user, "email": email, "first": (ids.get(key) or {}).get("first") or self.clock(),
                        "last": self.clock()}
            self._save()

    def users_of(self, workspace: str) -> dict[str, str]:
        """{user: e-mail} of the people of workspace who logged in with its provider."""
        return {i["user"]: i.get("email", "") for i in (self._data["identities"].get(workspace) or {}).values()}


@dataclass
class Flow:
    workspace: str
    nonce: str
    verifier: str
    next_path: str
    redirect_uri: str
    binding: str  # the digest of the browser's cookie
    expires: float


class Flows:
    """The logins sent to a provider, by state, each taken once."""

    def __init__(self):
        self._lock = threading.Lock()
        self._found: dict[str, Flow] = {}

    def start(self, workspace: str, next_path: str, redirect_uri: str) -> tuple[str, str, Flow]:
        """(state, the browser's cookie, the flow)."""
        state, cookie, now = secrets.token_urlsafe(24), secrets.token_urlsafe(32), time.time()
        flow = Flow(workspace, secrets.token_urlsafe(24), secrets.token_urlsafe(48), next_path, redirect_uri,
                    _digest(cookie), now + FLOW_SECONDS)
        with self._lock:
            self._found = {k: f for k, f in self._found.items() if f.expires > now}
            if len(self._found) > 5000:
                for k in sorted(self._found, key=lambda k: self._found[k].expires)[:1000]:
                    del self._found[k]
            self._found[state] = flow
        return state, cookie, flow

    def take(self, state: str, cookie: str | None) -> Flow | None:
        """The flow of state, if this browser started it (its cookie) and in time."""
        if not state:
            return None
        with self._lock:
            found = self._found.pop(state, None)
        if found is None or found.expires <= time.time():
            return None
        if not cookie or not hmac.compare_digest(found.binding, _digest(cookie)):
            return None
        return found


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# --- the provider, through the SSRF guard -----------------------------------

def _verify():
    # the machine's trust store (a provider with the company's own authority, as git and GitHub Enterprise)
    return ssl.create_default_context()


_discovered: dict[str, tuple[float, dict]] = {}
_keys: dict[str, tuple[float, list]] = {}


async def _get_json(url: str, networks, what: str) -> dict:
    try:
        code, body = await oauth.guarded_fetch(url, networks=networks, verify=_verify(), max_bytes=MAX_BYTES,
                                               timeout=TIMEOUT)
    except oauth.FetchRefused as exc:
        raise SsoError(f"The {what} cannot be read: {exc}") from None
    if code != 200:
        raise SsoError(f"The {what} cannot be read: {url} answered {code}")
    try:
        data = json.loads(body)
    except ValueError:
        raise SsoError(f"The {what} is not JSON: {url}") from None
    if not isinstance(data, dict):
        raise SsoError(f"The {what} is not a JSON object: {url}")
    return data


def _https(url) -> bool:
    try:
        parts = urlsplit(str(url))
    except ValueError:
        return False
    return parts.scheme == "https" and bool(parts.hostname) and not parts.fragment


async def discover(issuer: str, networks=(), fresh: bool = False) -> dict:
    """The provider's discovery document (OIDC Discovery §4), checked: its
    issuer exactly the one configured, its endpoints https, the code flow;
    kept DISCOVERY_TTL."""
    now = time.time()
    found = _discovered.get(issuer)
    if found and not fresh and now - found[0] < DISCOVERY_TTL:
        return found[1]
    url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    meta = await _get_json(url, networks, "discovery document of the provider")
    if meta.get("issuer") != issuer:
        raise SsoError(f"The provider says its issuer is {str(meta.get('issuer'))[:200]!r}, not {issuer!r}: "
                       "write it exactly as the provider does")
    for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not _https(meta.get(key)):
            raise SsoError(f"The provider's {key} is not an https:// address")
    if "code" not in (meta.get("response_types_supported") or ["code"]):
        raise SsoError("The provider does not give authorization codes")
    methods = meta.get("code_challenge_methods_supported")
    if methods is not None and "S256" not in methods:
        raise SsoError("The provider does not take PKCE with S256")
    _discovered[issuer] = (now, meta)
    return meta


def authorize_url(meta: dict, provider: dict, flow: Flow, state: str) -> str:
    query = {"response_type": "code", "client_id": provider["client_id"], "redirect_uri": flow.redirect_uri,
             "scope": SCOPES, "state": state, "nonce": flow.nonce, "code_challenge": s256(flow.verifier),
             "code_challenge_method": "S256"}
    endpoint = meta["authorization_endpoint"]
    return endpoint + ("&" if "?" in endpoint else "?") + urlencode(query)


async def exchange(meta: dict, provider: dict, code: str, flow: Flow, networks=()) -> str:
    """The ID token the token endpoint gives for code."""
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": flow.redirect_uri,
            "code_verifier": flow.verifier}
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    methods = meta.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
    if "client_secret_basic" in methods:
        pair = f"{_form_quote(provider['client_id'])}:{_form_quote(provider['client_secret'])}"
        headers["Authorization"] = "Basic " + base64.b64encode(pair.encode()).decode()
    else:
        form.update(client_id=provider["client_id"], client_secret=provider["client_secret"])
    try:
        status, body = await oauth.guarded_fetch(meta["token_endpoint"], "POST", form, headers, networks, _verify(),
                                                 MAX_BYTES, TIMEOUT)
    except oauth.FetchRefused as exc:
        raise SsoError(f"The token endpoint cannot be reached: {exc}") from None
    try:
        answer = json.loads(body)
    except ValueError:
        answer = None
    if status != 200 or not isinstance(answer, dict):
        said = answer.get("error") if isinstance(answer, dict) else ""
        raise SsoError(f"The token endpoint answered {status}" + (f" ({str(said)[:80]})" if said else ""))
    token = answer.get("id_token")
    if not isinstance(token, str) or not token:
        raise SsoError("The token endpoint gave no ID token")
    return token


def _form_quote(text: str) -> str:
    # RFC 6749 §2.3.1: the client id and secret form-encoded before the Basic pair
    return urlencode({"": text})[1:]


async def keys(meta: dict, networks=(), fresh: bool = False) -> list:
    url = meta["jwks_uri"]
    now = time.time()
    found = _keys.get(url)
    if found and not fresh and now - found[0] < DISCOVERY_TTL:
        return found[1]
    data = await _get_json(url, networks, "keys of the provider (JWKS)")
    listed = [k for k in data.get("keys") or [] if isinstance(k, dict)]
    _keys[url] = (now, listed)
    return listed


# --- the ID token --------------------------------------------------------------

def _int(value: str) -> int:
    return int.from_bytes(b64decode(value), "big")


def _public_key(jwk: dict, alg: str):
    if alg.startswith(("RS", "PS")):
        if jwk.get("kty") != "RSA":
            raise SsoError("the key is not an RSA key")
        key = rsa.RSAPublicNumbers(_int(jwk["e"]), _int(jwk["n"])).public_key()
        if key.key_size < 2048:
            raise SsoError("the RSA key is shorter than 2048 bits")
        return key
    crv, curve, _size = _CURVES[alg]
    if jwk.get("kty") != "EC" or jwk.get("crv") != crv:
        raise SsoError(f"the key is not an EC key of {crv}")
    return ec.EllipticCurvePublicNumbers(_int(jwk["x"]), _int(jwk["y"]), curve()).public_key()


def _check_signature(jwk: dict, alg: str, signed: bytes, signature: bytes) -> bool:
    try:
        key = _public_key(jwk, alg)
    except (KeyError, ValueError, TypeError):
        raise SsoError("a key of the provider cannot be read") from None
    digest = _HASHES[alg[2:]]()
    try:
        if alg.startswith("RS"):
            key.verify(signature, signed, padding.PKCS1v15(), digest)
        elif alg.startswith("PS"):
            key.verify(signature, signed, padding.PSS(mgf=padding.MGF1(digest), salt_length=digest.digest_size), digest)
        else:
            size = _CURVES[alg][2]
            if len(signature) != 2 * size:
                return False
            r, s = int.from_bytes(signature[:size], "big"), int.from_bytes(signature[size:], "big")
            key.verify(encode_dss_signature(r, s), signed, ec.ECDSA(digest))
    except InvalidSignature:
        return False
    return True


def token_parts(token: str) -> tuple[dict, dict, bytes, bytes]:
    """(header, claims, signed bytes, signature) of a compact JWS; SsoError when it is not one."""
    pieces = token.split(".") if isinstance(token, str) else []
    if len(pieces) != 3:
        raise SsoError("the ID token is not a signed JWT")
    try:
        header, claims = json.loads(b64decode(pieces[0])), json.loads(b64decode(pieces[1]))
    except ValueError:
        raise SsoError("the ID token cannot be read") from None
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise SsoError("the ID token cannot be read")
    return header, claims, f"{pieces[0]}.{pieces[1]}".encode(), b64decode(pieces[2])


def verify_id_token(token: str, jwks: list, issuer: str, client_id: str, nonce: str, now: float | None = None) -> dict:
    """The claims of an ID token, once every check of OIDC Core §3.1.3.7
    passed; SsoError (said why) otherwise. jwks: the provider's keys."""
    header, claims, signed, signature = token_parts(token)
    alg = header.get("alg")
    if alg not in ALGORITHMS:
        # 6: "none" and the HMACs are refused (an HMAC's key would be the client secret)
        raise SsoError(f"the ID token is signed with {str(alg)[:20]!r}, which is not accepted")
    if header.get("crit"):
        raise SsoError("the ID token has critical header parameters")
    kid = header.get("kid")
    usable = [k for k in jwks if k.get("use", "sig") == "sig" and k.get("alg", alg) == alg
              and k.get("kty") == ("RSA" if alg[0] in "RP" else "EC")]
    candidates = [k for k in usable if k.get("kid") == kid] if kid else usable
    if not candidates:
        raise SsoError("no key of the provider matches the ID token's (kid)")
    if not any(_check_signature(k, alg, signed, signature) for k in candidates):
        raise SsoError("the signature of the ID token is not the provider's")
    now = time.time() if now is None else now
    # 2: the issuer, exactly
    if claims.get("iss") != issuer:
        raise SsoError("the ID token is of another issuer")
    # 3-5: the audience is this client; with several, azp names it
    aud = claims.get("aud")
    audiences = aud if isinstance(aud, list) else [aud]
    if client_id not in audiences:
        raise SsoError("the ID token is for another client")
    if (len(audiences) > 1 or "azp" in claims) and claims.get("azp") != client_id:
        raise SsoError("the ID token was given to another client (azp)")
    # 9-10: in time
    exp, iat = claims.get("exp"), claims.get("iat")
    if not isinstance(exp, (int, float)) or exp + SKEW < now:
        raise SsoError("the ID token has expired")
    if not isinstance(iat, (int, float)) or iat - SKEW > now:
        raise SsoError("the ID token was issued in the future")
    # 11: the nonce of this login
    if not isinstance(claims.get("nonce"), str) or not hmac.compare_digest(claims["nonce"], nonce):
        raise SsoError("the ID token is not of this login (nonce)")
    if not isinstance(claims.get("sub"), str) or not claims["sub"] or len(claims["sub"]) > 255:
        raise SsoError("the ID token has no subject")
    return claims


def email_of(claims: dict, domains: list[str]) -> str:
    """The person's verified e-mail, lower case, of an allowed domain; SsoError otherwise."""
    email = str(claims.get("email") or "").strip().lower()
    if not re.fullmatch(r"[^@\s]{1,64}@[^@\s]{1,253}", email):
        raise SsoError("the provider gave no e-mail address (the scope email)")
    if claims.get("email_verified") not in (True, "true"):
        raise SsoError(f"the provider does not say {email} is verified (email_verified)")
    domain = email.rsplit("@", 1)[1]
    if domain not in domains:
        raise SsoError(f"the e-mail's domain {domain} is not allowed")
    return email
