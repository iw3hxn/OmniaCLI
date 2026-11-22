# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""GitHub through a fine-grained personal access token (README, GitHub).

No GitHub App and no OAuth app (Carlo, 22/11/2026): a token the user makes
on github.com and pastes into OmniaCLI, kept as a git credential of kind
«GitHub token» (gitcredentials.py) of a server or of a workspace. With it:

- the repositories it reads and their branches (Add a repository, «From
  GitHub»), cloned over https with the same token through git's credential
  store (gitcredentials.write_store: a 0600 file, never a command line);
- the webhook of a repository made, updated and taken away by OmniaCLI when
  the CI of an instance that follows it is switched on or off (app.py,
  github_sync), one per repository and machine, marked by its address
  (HOOK_PATH + its id);
- the state of a deploy of a staging or a test written back on its commit
  (a commit status, context omniacli/<instance>).

The API: https://api.github.com for github.com; any other host is read as a
GitHub Enterprise Server (https://<host>/api/v3). The token goes in the
Authorization header only (never in an address, a log or a job line); the
answers' X-RateLimit-Remaining is followed, and a limit used up is said with
the time it comes back. TRANSPORT lets the tests answer as GitHub
(httpx.MockTransport). The certificates are checked against the machine's
trust store, as git checks them.
"""

from __future__ import annotations

import datetime
import ipaddress
import re
import ssl
from urllib.parse import urlsplit

import httpx

API = "https://api.github.com"
TIMEOUT = httpx.Timeout(20.0, connect=10.0)
PER_PAGE = 100
# 100 a page: up to 1,000 repositories (or branches) listed, the rest said
PAGES = 10
# requests left of the rate limit that stop a listing early (the rest said)
KEEP = 5
# the context of a commit status: omniacli/<instance>
CONTEXT = "omniacli"
# the address of a webhook OmniaCLI made: <base>/hooks/github/<id> (its mark)
HOOK_PATH = "/hooks/github/"
API_VERSION = "2022-11-28"
# the tests' stand-in for GitHub (an httpx.MockTransport); None: the network
TRANSPORT = None

_OWNER_REPO = r"(?P<full>[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}?)"
_HTTPS = re.compile(r"https://(?P<host>[A-Za-z0-9.-]+(?::\d{1,5})?)/" + _OWNER_REPO + r"(?:\.git)?/?")
_SSH = re.compile(r"(?:git@(?P<host>[A-Za-z0-9.-]+):|ssh://git@(?P<host2>[A-Za-z0-9.-]+)(?::\d{1,5})?/)"
                  + _OWNER_REPO + r"(?:\.git)?/?")
FULL_NAME = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
SHA = re.compile(r"[0-9a-f]{40}")
# names no machine of the internet reaches (RFC 6761, 6762, 8375, and those of the private networks)
_LOCAL_TLDS = frozenset({"local", "localhost", "test", "invalid", "internal", "lan", "intranet", "corp", "home",
                         "example", "localdomain", "arpa"})

# what each permission of the token is for (the test says which are missing): the names are GitHub's
PERMISSIONS = (
    ("Metadata", "Read-only", "the list of the repositories and of their branches"),
    ("Contents", "Read-only", "clone and pull"),
    ("Webhooks", "Read and write", "the CI: the webhook of the repository made by OmniaCLI"),
    ("Commit statuses", "Read and write", "the state of a deploy written back on its commit"),
)


class GitHubError(Exception):
    """A refusal of GitHub, or GitHub not reached, said for a person."""

    def __init__(self, text: str, status: int | None = None):
        super().__init__(text)
        self.status = status


def api_base(host: str) -> str:
    host = (host or "").lower()
    return API if host == "github.com" else f"https://{host}/api/v3"


def repository_of(url: str) -> tuple[str, str] | None:
    """(host, owner/name) of a repository's address: https (its port kept:
    the host of its credential) or ssh (git@host:…, which the credentials
    reach over https too); None for any other."""
    url = (url or "").strip()
    found = _HTTPS.fullmatch(url)
    if found:
        return found["host"].lower(), found["full"]
    found = _SSH.fullmatch(url)
    if found:
        return (found["host"] or found["host2"]).lower(), found["full"]
    return None


def key_of(host: str, full_name: str) -> str:
    """The key of a repository in the file of the webhooks: host/owner/name, lower case."""
    return f"{host}/{full_name}".lower()


def unreachable(url: str) -> str | None:
    """Why GitHub, on the internet, would not reach url (a placeholder, this
    machine, a private address, a name of a local network); None when it
    may. The name is not looked up: a public name is taken at its word."""
    if not url or "<" in url:
        return "this OmniaCLI has no public address"
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme not in ("https", "http") or not host:
        return f"{url} is not an address of the internet"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if not ip.is_global:
            return f"{host} is a private address: GitHub, on the internet, does not reach it"
        return None
    if host == "localhost" or "." not in host:
        return f"{host} is not a name of the internet"
    if host.rsplit(".", 1)[1] in _LOCAL_TLDS or host.endswith(".home.arpa"):
        return f"{host} is a name of a local network: GitHub, on the internet, does not reach it"
    return None


def _verify():
    # the machine's trust store, as git's (a GitHub Enterprise with the company's own authority)
    return ssl.create_default_context()


def _reset_time(value: str | None) -> str:
    try:
        return datetime.datetime.fromtimestamp(int(value or "")).strftime("%H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"


class Client:
    """GitHub's API with one token; use as `async with Client(host, token) as gh`."""

    def __init__(self, host: str, token: str, transport=None):
        self.host = (host or "github.com").lower()
        self.base = api_base(self.host)
        self._token = token
        self._transport = transport if transport is not None else TRANSPORT
        self.remaining: int | None = None
        self.limit: int | None = None
        self.reset: str | None = None
        self._http: httpx.AsyncClient | None = None

    async def __aenter__(self) -> Client:
        options = {"transport": self._transport} if self._transport is not None else {"verify": _verify()}
        self._http = httpx.AsyncClient(
            base_url=self.base, timeout=TIMEOUT, follow_redirects=False, trust_env=False,
            headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": API_VERSION,
                     "User-Agent": "OmniaCLI"}, **options)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._http is not None:
            await self._http.aclose()

    def _rate(self, resp: httpx.Response) -> None:
        try:
            self.remaining = int(resp.headers["x-ratelimit-remaining"])
            self.limit = int(resp.headers.get("x-ratelimit-limit") or 0) or None
            self.reset = resp.headers.get("x-ratelimit-reset")
        except (KeyError, ValueError):
            pass

    def used_up(self) -> bool:
        return self.remaining is not None and self.remaining <= 0

    async def call(self, method: str, path: str, refusal: str, *, params=None, json=None, ok=(200,),
                   **values) -> httpx.Response:
        """One request; a refusal of GitHub raised as GitHubError with refusal
        (an English sentence with {status} and values) or the reason all
        refusals share (the token refused, the rate limit)."""
        if self.used_up():
            raise GitHubError(f"GitHub's rate limit of this token is used up until {_reset_time(self.reset)}: "
                              "try again then", 403)
        assert self._http is not None, "use the client as an async context manager"
        try:
            resp = await self._http.request(method, path, params=params, json=json,
                                            headers={"Authorization": f"Bearer {self._token}"})
        except httpx.HTTPError as exc:
            raise GitHubError(f"GitHub cannot be reached at {self.base}: {type(exc).__name__}") from None
        self._rate(resp)
        if resp.status_code in ok:
            return resp
        status = resp.status_code
        if status == 401:
            raise GitHubError("GitHub refused the token (401): it is wrong, expired or revoked", status)
        if status in (403, 429) and resp.headers.get("x-ratelimit-remaining") == "0":
            raise GitHubError(f"GitHub's rate limit of this token is used up until "
                              f"{_reset_time(resp.headers.get('x-ratelimit-reset'))}: try again then", status)
        if status in (403, 429) and resp.headers.get("retry-after"):
            raise GitHubError(f"GitHub asks to wait {resp.headers['retry-after']} s before the next request "
                              "(its secondary rate limit)", status)
        if status >= 500:
            raise GitHubError(f"GitHub did not answer ({status}): try again in a while", status)
        raise GitHubError(refusal.format(status=status, **values), status)

    async def pages(self, path: str, refusal: str, params=None, **values) -> tuple[list, bool]:
        """Every row of a listing, PER_PAGE at a time, following its Link
        header; True with them when some were left (PAGES pages, or the rate
        limit nearly used up)."""
        rows, url, query = [], path, {**(params or {}), "per_page": PER_PAGE}
        for _ in range(PAGES):
            resp = await self.call("GET", url, refusal, params=query, **values)
            data = resp.json()
            rows.extend(data if isinstance(data, list) else [])
            following = resp.links.get("next", {}).get("url")
            if not following:
                return rows, False
            if self.remaining is not None and self.remaining <= KEEP:
                return rows, True
            # the next page's address is GitHub's own, on the same API
            url, query = following[len(self.base):] if following.startswith(self.base) else following, None
            if not url.startswith("/"):
                return rows, True
        return rows, True

    # --- what OmniaCLI asks ---------------------------------------------------

    async def account(self) -> dict:
        """{login, type, orgs}: who the token is, and its organisations."""
        data = (await self.call("GET", "/user", "GitHub refused: the token cannot read its own account "
                                                "({status})")).json()
        orgs = []
        try:
            found, _ = await self.pages("/user/orgs", "GitHub refused: the token cannot read its organisations "
                                                      "({status})")
            orgs = sorted({str(o.get("login")) for o in found if isinstance(o, dict) and o.get("login")})
        except GitHubError:
            # a fine-grained token may not read them: the account is enough
            pass
        return {"login": str(data.get("login") or ""), "type": str(data.get("type") or ""), "orgs": orgs}

    async def repositories(self) -> tuple[list[dict], bool]:
        """The repositories the token reads, by name: owner/name, private,
        default branch, clone address; True when the list is cut."""
        found, cut = await self.pages("/user/repos", "GitHub refused: the token cannot list its repositories "
                                                     "({status})", params={"sort": "full_name"})
        rows = []
        for repo in found:
            if not isinstance(repo, dict) or not FULL_NAME.fullmatch(str(repo.get("full_name") or "")):
                continue
            rows.append({"full_name": repo["full_name"], "private": bool(repo.get("private")),
                         "default_branch": str(repo.get("default_branch") or ""),
                         "clone_url": str(repo.get("clone_url") or ""), "html_url": str(repo.get("html_url") or ""),
                         "archived": bool(repo.get("archived"))})
        return sorted(rows, key=lambda r: r["full_name"].lower()), cut

    async def repository(self, full: str) -> dict:
        return (await self.call("GET", f"/repos/{full}", "GitHub refused: the token cannot read {repo} ({status})",
                                repo=full)).json()

    async def branches(self, full: str) -> tuple[list[str], bool]:
        found, cut = await self.pages(f"/repos/{full}/branches",
                                      "GitHub refused: the token cannot read the branches of {repo} ({status})",
                                      repo=full)
        return [str(b.get("name")) for b in found if isinstance(b, dict) and b.get("name")], cut

    async def hooks(self, full: str) -> list[dict]:
        found, _ = await self.pages(f"/repos/{full}/hooks",
                                    "GitHub refused: the token cannot read the webhooks of {repo} ({status}): it "
                                    "needs «Webhooks: Read and write» on it", repo=full)
        return [h for h in found if isinstance(h, dict)]

    async def hook(self, full: str, hook_id: int) -> dict | None:
        """One webhook; None when it is not there any more."""
        try:
            return (await self.call("GET", f"/repos/{full}/hooks/{int(hook_id)}",
                                    "GitHub refused: the token cannot read the webhooks of {repo} ({status}): it "
                                    "needs «Webhooks: Read and write» on it", repo=full)).json()
        except GitHubError as exc:
            if exc.status == 404:
                return None
            raise

    async def make_hook(self, full: str, url: str, secret: str) -> dict:
        body = {"name": "web", "active": True, "events": ["push"],
                "config": {"url": url, "content_type": "json", "secret": secret, "insecure_ssl": "0"}}
        return (await self.call("POST", f"/repos/{full}/hooks",
                                "GitHub refused: the token cannot make the webhook of {repo} ({status}): it needs "
                                "«Webhooks: Read and write» on it", json=body, ok=(201,), repo=full)).json()

    async def change_hook(self, full: str, hook_id: int, url: str, secret: str) -> dict:
        body = {"active": True, "events": ["push"],
                "config": {"url": url, "content_type": "json", "secret": secret, "insecure_ssl": "0"}}
        return (await self.call("PATCH", f"/repos/{full}/hooks/{int(hook_id)}",
                                "GitHub refused: the token cannot change the webhook of {repo} ({status}): it "
                                "needs «Webhooks: Read and write» on it", json=body, repo=full)).json()

    async def remove_hook(self, full: str, hook_id: int) -> bool:
        """True when it was taken away, False when it was not there any more."""
        try:
            await self.call("DELETE", f"/repos/{full}/hooks/{int(hook_id)}",
                            "GitHub refused: the token cannot take away the webhook of {repo} ({status}): it needs "
                            "«Webhooks: Read and write» on it", ok=(204,), repo=full)
        except GitHubError as exc:
            if exc.status == 404:
                return False
            raise
        return True

    async def set_status(self, full: str, sha: str, state: str, context: str, description: str,
                         target_url: str | None) -> None:
        body = {"state": state, "context": context, "description": description[:140]}
        if target_url:
            body["target_url"] = target_url
        await self.call("POST", f"/repos/{full}/statuses/{sha}",
                        "GitHub refused: the token cannot write the state of the commit on {repo} ({status}): it "
                        "needs «Commit statuses: Read and write» on it", json=body, ok=(201,), repo=full)

    async def permissions(self, full: str) -> list[dict]:
        """What the token may do on full, measured by asking GitHub (a
        fine-grained token says no scopes): a read for the read permissions,
        and for the write ones a write GitHub refuses as invalid (422) when
        the token may write, and as forbidden (403, 404) when it may not:
        nothing is made."""
        found = {}
        try:
            await self.repository(full)
            found["Metadata"] = True
        except GitHubError as exc:
            if exc.status not in (403, 404):
                raise
            found["Metadata"] = False
        found["Contents"] = await self._may("GET", f"/repos/{full}/commits", params={"per_page": 1}, ok=(200, 409))
        found["Webhooks"] = await self._may("GET", f"/repos/{full}/hooks", params={"per_page": 1}) and await self._may(
            "POST", f"/repos/{full}/hooks", json={"name": "web", "active": False, "events": ["push"],
                                                  "config": {"url": ""}}, ok=(422, 400))
        found["Commit statuses"] = await self._may("POST", f"/repos/{full}/statuses/{'0' * 40}",
                                                   json={"state": "not-a-state", "context": f"{CONTEXT}/probe"},
                                                   ok=(422, 400))
        return [{"permission": name, "access": access, "for": purpose, "ok": found[name]}
                for name, access, purpose in PERMISSIONS]

    async def _may(self, method: str, path: str, ok=(200,), params=None, json=None) -> bool:
        try:
            await self.call(method, path, "GitHub refused ({status})", params=params, json=json, ok=ok)
        except GitHubError as exc:
            if exc.status in (403, 404):
                return False
            raise
        return True
