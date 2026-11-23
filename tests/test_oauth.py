# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""OAuth 2.1 for the MCP clients that cannot send a static header (oauth.py;
README, MCP interface, OAuth): the discovery, the dynamic registration and the
client ID metadata documents (with their SSRF guard), the person's login and
consent, PKCE, the tokens bound to the MCP URL, the rotation of the refresh
tokens and a reuse that ends the grant, the revocation from the pages, the
access log, and a workspace's user who gives only what it has."""

import json
import re
import secrets
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

pytest.importorskip("mcp")  # the SDK needs Python 3.10

from omniacli import apiguard, oauth  # noqa: E402
from omniacli.auth import hash_password  # noqa: E402

from .test_app import AUTH, HOST, client, wait_job  # noqa: E402,F401
from .test_links import basic  # noqa: E402
from .test_mcp import call  # noqa: E402
from .test_workspaces import workspaces  # noqa: E402,F401

BASE = f"http://{HOST}"
MCP = BASE + "/mcp"
CLAUDE = "https://claude.ai/api/mcp/auth_callback"
RPC = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def pkce():
    verifier = secrets.token_urlsafe(48)
    return verifier, oauth.s256(verifier)


def bearer(token):
    return {**RPC, "Authorization": f"Bearer {token}"}


def query(location):
    return {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}


async def register(client, uris=(CLAUDE,), **more):
    resp = await client.post("/oauth/register", json={"client_name": "Claude", "redirect_uris": list(uris),
                                                       "token_endpoint_auth_method": "none", **more})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def authorize(client, client_id, challenge, redirect=CLAUDE, **params):
    """/oauth/authorize, then the page that moves on by itself: the consent's address."""
    resp = await client.get("/oauth/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect, "code_challenge": challenge,
        "code_challenge_method": "S256", "state": "st-1", "resource": MCP, "scope": "read write production", **params})
    assert resp.status_code == 200, resp.text
    return re.search(r'http-equiv="refresh" content="0;url=([^"]+)"', resp.text).group(1).replace("&amp;", "&")


async def consent(client, target, user="admin", decision="approve", **form):
    rid = parse_qs(urlsplit(target).query)["request"][0]
    page = await client.get(target, headers={"Authorization": basic(user), "Accept": "text/html"})
    assert page.status_code == 200, page.text
    resp = await client.post("/oauth/consent", headers={"Authorization": basic(user), "Origin": BASE},
                             data={"request": rid, "decision": decision, "days": "30", "workspace": "", **form})
    return page, resp


async def token(client, **form):
    return await client.post("/oauth/token", data=form)


async def connect(client, user="admin", level="write", uris=(CLAUDE,), redirect=CLAUDE, **form):
    """The whole flow: (the client, its tokens' answer)."""
    reg = await register(client, uris)
    verifier, challenge = pkce()
    target = await authorize(client, reg["client_id"], challenge, redirect)
    _, resp = await consent(client, target, user, level=level, **form)
    assert resp.status_code == 303, resp.text
    code = query(resp.headers["location"])["code"]
    resp = await token(client, grant_type="authorization_code", code=code, redirect_uri=redirect,
                       code_verifier=verifier, client_id=reg["client_id"], resource=MCP)
    assert resp.status_code == 200, resp.text
    return reg, resp.json()


async def test_the_discovery(client):
    # /mcp without a token: a 401 that says where to learn how to get one (RFC 9728)
    resp = await client.post("/mcp", headers=RPC, content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}))
    assert resp.status_code == 401
    challenge = resp.headers["www-authenticate"]
    assert challenge.startswith("Bearer ") and f'resource_metadata="{BASE}/.well-known/oauth-protected-resource/mcp"' in challenge
    assert 'scope="read write production"' in challenge and "error=" not in challenge
    resp = await client.post("/mcp", headers=bearer("wrong"), content="{}")
    assert resp.status_code == 401 and 'error="invalid_token"' in resp.headers["www-authenticate"]
    for path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"):
        found = (await client.get(path)).json()
        assert found["resource"] == MCP and found["authorization_servers"] == [BASE]
    meta = await client.get("/.well-known/oauth-authorization-server")
    assert meta.headers["access-control-allow-origin"] == "*" and meta.headers["cache-control"] == "no-store"
    meta = meta.json()
    assert meta["issuer"] == BASE and meta["code_challenge_methods_supported"] == ["S256"]
    assert meta["client_id_metadata_document_supported"] is True
    assert meta["authorization_response_iss_parameter_supported"] is True
    assert "none" in meta["token_endpoint_auth_methods_supported"]
    assert {"authorization_endpoint", "token_endpoint", "registration_endpoint", "revocation_endpoint"} <= set(meta)
    assert meta["scopes_supported"] == ["read", "write", "production", "offline_access"]
    # a browser's client asks first
    assert (await client.options("/oauth/token")).status_code == 204


async def test_the_whole_flow(client, env):
    """DCR → authorize with PKCE → login → consent → token → MCP → refresh →
    rotation → a reuse ends the grant → a new grant revoked from the token page."""
    reg = await register(client)
    assert reg["client_id"].startswith("dyn-") and reg["token_endpoint_auth_method"] == "none"
    assert "client_secret" not in reg
    verifier, challenge = pkce()
    target = await authorize(client, reg["client_id"], challenge)
    # not logged in: the login first, which comes back to the consent
    resp = await client.get(target)
    assert resp.status_code == 303 and resp.headers["location"].startswith("/login?next=/oauth/consent")
    page, resp = await consent(client, target, level="write")
    assert "Claude" in page.text and "not verified" in page.text and "claude.ai" in page.text
    assert page.text.count('class="primary"') == 1
    assert resp.status_code == 303
    answer = query(resp.headers["location"])
    assert resp.headers["location"].startswith(CLAUDE + "?")
    assert answer["state"] == "st-1" and answer["iss"] == BASE and answer["code"]
    # the code, with a wrong verifier, then the right one
    form = dict(grant_type="authorization_code", code=answer["code"], redirect_uri=CLAUDE, client_id=reg["client_id"],
                resource=MCP)
    resp = await token(client, **form, code_verifier=pkce()[0])
    assert resp.status_code == 400 and resp.json()["error"] == "invalid_grant"
    # a code tried with a wrong verifier is still the client's: the right one is not refused by it
    verifier2, challenge2 = pkce()
    target = await authorize(client, reg["client_id"], challenge2)
    _, resp = await consent(client, target, level="write")
    code = query(resp.headers["location"])["code"]
    resp = await token(client, **{**form, "code": code}, code_verifier=verifier2)
    assert resp.status_code == 200, resp.text
    tokens = resp.json()
    assert resp.headers["cache-control"] == "no-store"
    assert tokens["token_type"] == "Bearer" and tokens["expires_in"] == oauth.ACCESS_SECONDS
    assert tokens["scope"] == "write" and tokens["refresh_token"]
    # MCP with it: the tools of a developer token, its name in the jobs
    headers = bearer(tokens["access_token"])
    tools = (await client.post("/mcp", headers=headers, content=json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}))).json()["result"]["tools"]
    names = {t["name"] for t in tools}
    assert {"list_servers", "restart_instance", "update_test"} <= names and "read_server" not in names
    assert [s["name"] for s in await call(client, "list_servers", headers=headers)] == ["srv"]
    job = await call(client, "restart_instance", headers=headers, server_name="srv", role="test", wait_seconds=30)
    assert job["status"] == "success" and job["source"].startswith("mcp: token oauth:admin:claude:"), job
    # a production: never with this level, whatever the confirm
    said = await call(client, "restart_instance", headers=headers, server_name="srv", role="production", confirm="srv")
    assert "production level" in said["error"]
    assert "restart odoo-production" not in env["calls"].read_text()
    # the pages and the REST API: not for an OAuth token (it is bound to the MCP URL)
    assert (await client.get("/api/v1/instances", headers={"Authorization": f"Bearer {tokens['access_token']}"})).status_code == 403
    assert (await client.get("/jobs", headers={"Authorization": f"Bearer {tokens['access_token']}"})).status_code == 403
    # at another address of the same OmniaCLI: another resource
    resp = await client.post("/mcp", headers={**headers, "Host": "elsewhere.test"}, content="{}")
    assert resp.status_code == 401 and 'error="invalid_token"' in resp.headers["www-authenticate"]

    # the refresh: new tokens, the old refresh token spent
    first = tokens["refresh_token"]
    resp = await token(client, grant_type="refresh_token", refresh_token=first, client_id=reg["client_id"])
    assert resp.status_code == 200, resp.text
    renewed = resp.json()
    assert renewed["refresh_token"] != first and renewed["access_token"] != tokens["access_token"]
    assert [s["name"] for s in await call(client, "list_servers", headers=bearer(renewed["access_token"]))] == ["srv"]
    # the spent one used again (a stolen copy): the grant ends, every token of it
    resp = await token(client, grant_type="refresh_token", refresh_token=first, client_id=reg["client_id"])
    assert resp.status_code == 400 and resp.json()["error"] == "invalid_grant"
    resp = await token(client, grant_type="refresh_token", refresh_token=renewed["refresh_token"],
                       client_id=reg["client_id"])
    assert resp.json()["error"] == "invalid_grant"
    resp = await client.post("/mcp", headers=bearer(renewed["access_token"]), content="{}")
    assert resp.status_code == 401
    # the code used twice: refused, and what it gave ends
    resp = await token(client, **{**form, "code": code}, code_verifier=verifier2)
    assert resp.json()["error"] == "invalid_grant"

    # a new one, revoked from the token page by an admin
    _, tokens = await connect(client)
    store = client.app.state.oauth_store
    grant = store.listed(user="admin")[0]
    page = (await client.get("/users", headers={"Authorization": AUTH, "Accept-Language": "it"})).text
    assert grant["name"] in page and "Collegati con OAuth" in page and "data-revoke-grant" in page
    account = (await client.get("/account", headers={"Authorization": AUTH})).text
    assert grant["name"] in account and "Connected assistants" in account
    resp = await client.post("/api/oauth/grants/revoke", headers={"Authorization": AUTH, "Origin": BASE},
                             json={"id": grant["id"]})
    assert resp.json() == {"ok": True}
    resp = await client.post("/mcp", headers=bearer(tokens["access_token"]), content="{}")
    assert resp.status_code == 401
    resp = await token(client, grant_type="refresh_token", refresh_token=tokens["refresh_token"],
                       client_id=grant["client_id"])
    assert resp.json()["error"] == "invalid_grant"

    # all of it in the access log (NIS 2)
    entries = client.app.state.access_log.recent()
    said = [e["detail"] for e in entries if e["event"] == "oauth"]
    for text in ("client registered: Claude", "consent given: Claude (claude.ai), level write, 30 days",
                 "token issued: Claude for admin, level write", "token refreshed (the refresh token rotated)",
                 "revoked: this refresh token was used already", "revoked " + grant["name"]):
        assert any(text in d for d in said), (text, said)
    failed = [e["detail"] for e in entries if e["event"] == "failed"]
    assert any("OAuth token: invalid_grant" in d for d in failed)


async def test_a_production_with_the_production_level(client, env):
    _, tokens = await connect(client, level="production")
    headers = bearer(tokens["access_token"])
    said = await call(client, "restart_instance", headers=headers, server_name="srv", role="production")
    assert "confirm" in said["error"]
    job = await call(client, "restart_instance", headers=headers, server_name="srv", role="production", confirm="srv",
                     wait_seconds=30)
    assert job["status"] == "success" and "restart odoo-production" in env["calls"].read_text()


async def test_the_consent_refused(client):
    reg = await register(client)
    target = await authorize(client, reg["client_id"], pkce()[1])
    _, resp = await consent(client, target, decision="deny")
    assert resp.status_code == 303
    answer = query(resp.headers["location"])
    assert answer == {"error": "access_denied", "error_description": "The person refused", "state": "st-1", "iss": BASE}
    # answered: the request is no more
    assert (await client.get(target, headers={"Authorization": AUTH})).status_code == 400
    assert any(e["detail"].startswith("consent refused: Claude") for e in client.app.state.access_log.recent()
               if e["event"] == "oauth")
    # a form from another site is refused
    rid = parse_qs(urlsplit(target).query)["request"][0]
    resp = await client.post("/oauth/consent", headers={"Authorization": AUTH, "Origin": "https://evil.test"},
                             data={"request": rid, "decision": "approve"})
    assert resp.status_code == 403


async def test_the_authorization_request_is_checked(client):
    reg = await register(client, ("http://localhost/callback", CLAUDE))
    _, challenge = pkce()
    params = {"response_type": "code", "client_id": reg["client_id"], "code_challenge": challenge,
              "code_challenge_method": "S256", "state": "s"}
    # a redirect URI it did not register: said here, nothing sent anywhere
    resp = await client.get("/oauth/authorize", params={**params, "redirect_uri": "https://evil.test/cb"})
    assert resp.status_code == 400 and "location" not in resp.headers
    resp = await client.get("/oauth/authorize", params={**params, "client_id": "nobody", "redirect_uri": CLAUDE})
    assert resp.status_code == 400 and "location" not in resp.headers
    # a loopback one: any port, the rest exactly (RFC 8252)
    assert (await client.get("/oauth/authorize", params={**params, "redirect_uri": "http://localhost:53682/callback"})).status_code == 200
    assert (await client.get("/oauth/authorize", params={**params, "redirect_uri": "http://localhost:53682/other"})).status_code == 400
    # PKCE S256 only, the resource this one
    resp = await client.get("/oauth/authorize", params={**params, "redirect_uri": CLAUDE, "code_challenge_method": "plain"})
    assert resp.status_code == 303 and query(resp.headers["location"])["error"] == "invalid_request"
    assert query(resp.headers["location"])["iss"] == BASE
    resp = await client.get("/oauth/authorize", params={**params, "redirect_uri": CLAUDE, "resource": "https://other.test/mcp"})
    assert query(resp.headers["location"])["error"] == "invalid_target"
    resp = await client.get("/oauth/authorize", params={**params, "redirect_uri": CLAUDE, "response_type": "token"})
    assert query(resp.headers["location"])["error"] == "unsupported_response_type"


async def test_the_registration_is_checked(client):
    for uris, error in ((["http://evil.test/cb"], "invalid_redirect_uri"), (["javascript:alert(1)"], "invalid_redirect_uri"),
                        ([], "invalid_redirect_uri"), (["https://ok.test/cb#x"], "invalid_redirect_uri")):
        resp = await client.post("/oauth/register", json={"redirect_uris": uris})
        assert resp.status_code == 400 and resp.json()["error"] == error, uris
    resp = await client.post("/oauth/register", json={"redirect_uris": [CLAUDE], "grant_types": ["client_credentials"]})
    assert resp.json()["error"] == "invalid_client_metadata"
    # a native app's own scheme is a redirect (Cursor)
    assert (await client.post("/oauth/register", json={"redirect_uris": ["cursor://anysphere.cursor-mcp/oauth/callback"]})).status_code == 201
    # a confidential client: its secret at the token endpoint
    reg = await register(client, token_endpoint_auth_method="client_secret_basic")
    assert reg["client_secret"] and reg["client_secret_expires_at"] == 0
    stored = client.app.state.oauth_store.client(reg["client_id"])
    assert stored["secret"] != reg["client_secret"] and reg["client_secret"] not in json.dumps(stored)
    resp = await token(client, grant_type="refresh_token", refresh_token="x", client_id=reg["client_id"])
    assert resp.status_code == 401 and resp.json()["error"] == "invalid_client"
    # HTTP Basic (the tests' client turns Basic into a session: this one reaches the app as it is)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url=BASE) as plain:
        resp = await plain.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": "x"},
                                auth=(reg["client_id"], reg["client_secret"]))
        assert resp.status_code == 400 and resp.json()["error"] == "invalid_grant"
        resp = await plain.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": "x"},
                                auth=(reg["client_id"], "wrong"))
        assert resp.status_code == 401 and resp.headers["www-authenticate"] == "Basic"
    post = await register(client, token_endpoint_auth_method="client_secret_post")
    resp = await token(client, grant_type="refresh_token", refresh_token="x", client_id=post["client_id"],
                       client_secret=post["client_secret"])
    assert resp.json()["error"] == "invalid_grant"
    # the token endpoint takes a form only
    resp = await client.post("/oauth/token", json={"grant_type": "refresh_token"})
    assert resp.json()["error"] == "invalid_request"


async def test_the_registrations_of_an_address_are_limited(client):
    client.app.state.oauth_limits.limits[True] = 2
    for _ in range(2):
        await register(client)
    resp = await client.post("/oauth/register", json={"redirect_uris": [CLAUDE]})
    assert resp.status_code == 429 and resp.headers["retry-after"]


async def test_the_mcp_rate_limit_counts_an_oauth_token(client, monkeypatch):
    _, tokens = await connect(client)
    monkeypatch.setattr(client.app.state.api_guard, "limiter", apiguard.RateLimiter(reads=1, writes=1))
    headers = bearer(tokens["access_token"])
    await call(client, "list_servers", headers=headers)
    resp = await client.post("/mcp", headers=headers, content=json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "list_servers", "arguments": {}}}))
    assert resp.status_code == 429


async def test_an_access_token_lives_an_hour(client):
    reg, tokens = await connect(client)
    store = client.app.state.oauth_store
    later = time.time() + oauth.ACCESS_SECONDS + 5
    store.clock = lambda: later
    resp = await client.post("/mcp", headers=bearer(tokens["access_token"]), content="{}")
    assert resp.status_code == 401
    resp = await token(client, grant_type="refresh_token", refresh_token=tokens["refresh_token"], client_id=reg["client_id"])
    assert resp.status_code == 200
    # past the days the person gave: the person agrees again
    store.clock = lambda: later + 31 * 86400
    resp = await token(client, grant_type="refresh_token", refresh_token=resp.json()["refresh_token"],
                       client_id=reg["client_id"])
    assert resp.json()["error"] == "invalid_grant"


async def test_the_file_keeps_no_token(client):
    _, tokens = await connect(client)
    store = client.app.state.oauth_store
    text = store.path.read_text()
    assert tokens["access_token"] not in text and tokens["refresh_token"] not in text
    assert oauth.digest(tokens["access_token"]) in text
    assert oct(store.path.stat().st_mode & 0o777) == "0o600"
    # a restart: the grants are there, the token still works
    again = oauth.OAuthStore(store.path)
    again.person = store.person
    assert again.name_of(tokens["access_token"]) == store.listed()[0]["name"]


async def test_a_workspace_user_gives_only_what_it_has(workspaces, client, env):  # noqa: F811
    """luca is a user in acme (which holds srv), eva a developer in beta (no server)."""
    # luca: read only, acme only
    reg = await register(client)
    verifier, challenge = pkce()
    target = await authorize(client, reg["client_id"], challenge)
    page, resp = await consent(client, target, "luca", level="write", workspace="acme")
    assert 'value="acme"' in page.text and "above your role" in page.text
    assert resp.status_code == 400  # write: above its role there
    _, resp = await consent(client, target, "luca", level="read", workspace="")
    assert resp.status_code == 400  # the machine: not its
    _, resp = await consent(client, target, "luca", level="read", workspace="acme", servers="other")
    assert resp.status_code == 400  # a server not of acme
    _, resp = await consent(client, target, "luca", level="read", workspace="acme", servers="srv")
    assert resp.status_code == 303, resp.text
    code = query(resp.headers["location"])["code"]
    tokens = (await token(client, grant_type="authorization_code", code=code, redirect_uri=CLAUDE,
                          code_verifier=verifier, client_id=reg["client_id"])).json()
    headers = bearer(tokens["access_token"])
    assert [s["name"] for s in await call(client, "list_servers", headers=headers)] == ["srv"]
    said = await call(client, "restart_instance", headers=headers, server_name="srv", role="test")
    assert "role developer" in said["error"]
    grant = client.app.state.oauth_store.listed(user="luca")[0]
    assert grant["workspace"] == "acme" and grant["servers"] == ["srv"] and grant["level"] == "read"
    # eva: beta has no server, its token sees none
    _, eva = await connect(client, "eva", level="write", workspace="beta")
    assert await call(client, "list_servers", headers=bearer(eva["access_token"])) == []
    # the workspace's page lists its grants; eva may not revoke luca's, mario (admin of acme) may
    page = await client.get("/workspaces/acme", headers={"Authorization": basic("mario")})
    assert grant["name"] in page.text
    resp = await client.post("/api/oauth/grants/revoke", headers={"Authorization": basic("eva"), "Origin": BASE},
                             json={"id": grant["id"]})
    assert resp.status_code == 404
    resp = await client.post("/api/oauth/grants/revoke", headers={"Authorization": basic("mario"), "Origin": BASE},
                             json={"id": grant["id"]})
    assert resp.json() == {"ok": True}
    assert (await client.post("/mcp", headers=headers, content="{}")).status_code == 401
    # out of beta: eva's grant stops at once, its refresh too, and it leaves the lists
    eva_grant = client.app.state.oauth_store.listed(user="eva")[0]
    workspaces.workspaces["beta"].members.pop("eva")
    assert (await client.post("/mcp", headers=bearer(eva["access_token"]), content="{}")).status_code == 401
    resp = await token(client, grant_type="refresh_token", refresh_token=eva["refresh_token"],
                       client_id=eva_grant["client_id"])
    assert resp.json()["error"] == "invalid_grant"
    assert client.app.state.oauth_store.listed(user="eva") == []


async def test_a_lower_role_lowers_the_grant(client, env):
    """dev (a developer) gives write; made a user, its token reads only."""
    _, tokens = await connect(client, "dev", level="write")
    reg = await register(client)
    target = await authorize(client, reg["client_id"], pkce()[1])
    _, resp = await consent(client, target, "dev", level="production")
    assert resp.status_code == 400  # above its role
    env["settings"].user_roles["dev"] = "user"
    said = await call(client, "restart_instance", headers=bearer(tokens["access_token"]), server_name="srv", role="test")
    assert "role developer" in said["error"]


async def test_a_new_password_or_a_new_user_of_the_same_name_ends_it(client, env):
    """As for the sessions: the grant is of who the person was, not of a name."""
    _, tokens = await connect(client, "viewer", level="read")
    headers = bearer(tokens["access_token"])
    assert [s["name"] for s in await call(client, "list_servers", headers=headers)] == ["srv"]
    env["settings"].users["viewer"] = hash_password("another one", iterations=1000)
    assert (await client.post("/mcp", headers=headers, content="{}")).status_code == 401
    assert client.app.state.oauth_store.listed(user="viewer") == []


# --- client ID metadata documents, and the SSRF guard ---------------------

DOC_URL = "https://chatgpt.example/oauth/client.json"


def document(**more):
    return {"client_id": DOC_URL, "client_name": "ChatGPT", "redirect_uris": ["https://chatgpt.example/connector_platform_oauth_redirect"],
            "token_endpoint_auth_method": "none", **more}


@pytest.fixture
def internet(monkeypatch):
    """chatgpt.example resolves to a public address; what is fetched is recorded."""
    seen, answers = [], {"doc": document(), "status": 200, "headers": {}}

    async def resolve(host, port):
        return answers.get("addresses", ["93.184.216.34"])

    def handler(request):
        seen.append(request)
        body = answers["doc"] if isinstance(answers["doc"], bytes) else json.dumps(answers["doc"]).encode()
        return httpx.Response(answers["status"], content=body, headers=answers["headers"])

    monkeypatch.setattr(oauth, "_resolve", resolve)
    monkeypatch.setattr(oauth, "TRANSPORT", httpx.MockTransport(handler))
    return seen, answers


async def test_a_client_of_a_metadata_document(client, internet):
    seen, _ = internet
    verifier, challenge = pkce()
    redirect = document()["redirect_uris"][0]
    target = await authorize(client, DOC_URL, challenge, redirect)
    # fetched at the address checked, with its name for TLS and Host
    assert seen[0].url.host == "93.184.216.34" and seen[0].headers["host"] == "chatgpt.example"
    assert seen[0].extensions["sni_hostname"] == "chatgpt.example"
    page, resp = await consent(client, target, level="read")
    assert "identity published by chatgpt.example" in page.text and "not verified" not in page.text
    code = query(resp.headers["location"])["code"]
    resp = await token(client, grant_type="authorization_code", code=code, redirect_uri=redirect,
                       code_verifier=verifier, client_id=DOC_URL)
    assert resp.status_code == 200 and resp.json()["scope"] == "read"
    # kept a while: not fetched again at once
    await authorize(client, DOC_URL, challenge, redirect)
    assert len(seen) == 1


@pytest.mark.parametrize("addresses", [["10.0.0.5"], ["127.0.0.1"], ["::ffff:127.0.0.1"], ["169.254.169.254"],
                                       ["93.184.216.34", "192.168.1.1"], ["fd00::1"], ["100.64.0.1"], []])
async def test_the_ssrf_guard_refuses_a_private_address(client, internet, addresses):
    seen, answers = internet
    answers["addresses"] = addresses
    resp = await client.get("/oauth/authorize", params={"response_type": "code", "client_id": DOC_URL,
                                                        "code_challenge": pkce()[1], "code_challenge_method": "S256"})
    assert resp.status_code == 400 and not seen and "location" not in resp.headers


@pytest.mark.parametrize("change, text", [
    ({"status": 302, "headers": {"Location": "http://127.0.0.1/"}}, "answered 302"),
    ({"doc": b"x" * (oauth.METADATA_MAX_BYTES + 10)}, "larger than"),
    ({"doc": document(client_id="https://other.example/c.json")}, "not its own URL"),
    ({"doc": document(redirect_uris=["http://evil.test/cb"])}, "http only"),
    ({"doc": document(token_endpoint_auth_method="client_secret_post")}, "no secret"),
    ({"doc": b"not json"}, "not JSON"),
])
async def test_a_bad_metadata_document_is_refused(client, internet, change, text):
    _, answers = internet
    answers.update(change)
    resp = await client.get("/oauth/authorize", params={"response_type": "code", "client_id": DOC_URL,
                                                        "code_challenge": pkce()[1], "code_challenge_method": "S256"})
    assert resp.status_code == 400 and text in resp.text


def test_the_guards_themselves():
    assert oauth.public_address("93.184.216.34") and oauth.public_address("2606:4700::1111")
    for address in ("127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.0.1", "169.254.169.254", "::1", "fe80::1%eth0",
                    "fd12::1", "::ffff:10.0.0.1", "224.0.0.1", "0.0.0.0", "100.64.0.1", "nonsense"):
        assert not oauth.public_address(address), address
    assert oauth.metadata_url_problem("http://a.example/c.json")
    assert oauth.metadata_url_problem("https://a.example/")
    assert oauth.metadata_url_problem("https://u:p@a.example/c.json")
    assert oauth.metadata_url_problem("https://a.example/c.json?x=1")
    assert oauth.metadata_url_problem("https://a.example/../c.json")
    assert oauth.metadata_url_problem("https://a.example/c.json") is None
    assert oauth.redirect_matches(["http://127.0.0.1/callback"], "http://127.0.0.1:4000/callback")
    assert not oauth.redirect_matches(["http://127.0.0.1/callback"], "http://localhost:4000/callback")
    assert not oauth.redirect_matches([CLAUDE], CLAUDE + "/x")
    assert oauth.with_query("https://a.example/cb?x=1", {"code": "c", "state": ""}) == "https://a.example/cb?x=1&code=c"
