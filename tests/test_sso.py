# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Single sign-on of a workspace (sso.py, README Single sign-on) against a
fake identity provider with its own keys: the admin's configuration (the
secret never shown again, the discovery read through the SSRF guard), the
code flow with PKCE, state and nonce, the ID token's checks (signature,
issuer, audience, time, nonce, a tampered one), the allowed domains, the
user made or linked, the second step, the access log."""
import base64
import hashlib
import json
import os
import stat
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from omniacli import oauth, sso
from omniacli.app import create_app
from omniacli.config import parse_options

from .conftest import PASSWORD
from .test_workspaces import workspaces  # noqa: F401 - the workspaces fixture

PORTAL = "portale.hosting.test"
ISSUER = "https://idp.acme.example"
CLIENT = "omniacli-acme"
SECRET = "s3cret-of-the-idp-0123456789"


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


class FakeIdP:
    """The provider: discovery, JWKS, token endpoint; the authorize step is the test's (the browser's)."""

    def __init__(self, alg="RS256"):
        self.alg = alg
        self.key = (rsa.generate_private_key(public_exponent=65537, key_size=2048) if alg.startswith("RS")
                    else ec.generate_private_key(ec.SECP256R1()))
        self.codes: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.claims: dict = {}
        self.tamper = None

    def jwk(self) -> dict:
        if self.alg.startswith("RS"):
            n = self.key.public_key().public_numbers()
            return {"kty": "RSA", "kid": "k1", "use": "sig", "alg": self.alg,
                    "n": b64(n.n.to_bytes((n.n.bit_length() + 7) // 8, "big")), "e": b64(n.e.to_bytes(3, "big"))}
        n = self.key.public_key().public_numbers()
        return {"kty": "EC", "kid": "k1", "crv": "P-256", "x": b64(n.x.to_bytes(32, "big")), "y": b64(n.y.to_bytes(32, "big"))}

    def sign(self, claims: dict, header: dict | None = None) -> str:
        head = b64(json.dumps(header or {"alg": self.alg, "kid": "k1", "typ": "JWT"}).encode())
        body = b64(json.dumps(claims).encode())
        signed = f"{head}.{body}".encode()
        if self.alg.startswith("RS"):
            signature = self.key.sign(signed, padding.PKCS1v15(), hashes.SHA256())
        else:
            r, s = decode_dss_signature(self.key.sign(signed, ec.ECDSA(hashes.SHA256())))
            signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        return f"{head}.{body}.{b64(signature)}"

    def authorize(self, location: str, email: str, **claims) -> str:
        """What the provider does with the person's browser: a code for the redirect URI."""
        q = {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}
        assert location.startswith(ISSUER + "/authorize?") and q["response_type"] == "code"
        assert q["client_id"] == CLIENT and q["code_challenge_method"] == "S256" and q["scope"] == "openid email profile"
        code = b64(os.urandom(12))
        self.codes[code] = {"q": q, "claims": {"email": email, "email_verified": True, **claims}}
        return f"{q['redirect_uri']}?code={code}&state={q['state']}"

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        if request.headers["host"] != "idp.acme.example":
            return httpx.Response(404)  # a host of the LAN the guard let through: not this provider
        assert request.url.host == "93.184.216.34"  # the address checked is the one connected to
        if path == "/.well-known/openid-configuration":
            return httpx.Response(200, json={
                "issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/authorize", "token_endpoint": f"{ISSUER}/token",
                "jwks_uri": f"{ISSUER}/jwks", "response_types_supported": ["code"],
                "code_challenge_methods_supported": ["S256"], "token_endpoint_auth_methods_supported": ["client_secret_basic"]})
        if path == "/jwks":
            return httpx.Response(200, json={"keys": [self.jwk()]})
        if path == "/token":
            assert request.headers["authorization"] == "Basic " + base64.b64encode(f"{CLIENT}:{SECRET}".encode()).decode()
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            found = self.codes.pop(form["code"], None)
            if found is None:
                return httpx.Response(400, json={"error": "invalid_grant"})
            q = found["q"]
            challenge = b64(hashlib.sha256(form["code_verifier"].encode()).digest())
            if challenge != q["code_challenge"] or form["redirect_uri"] != q["redirect_uri"]:
                return httpx.Response(400, json={"error": "invalid_grant"})
            now = int(time.time())
            claims = {"iss": ISSUER, "aud": CLIENT, "sub": "sub-" + found["claims"]["email"], "iat": now, "exp": now + 300,
                      "nonce": q["nonce"], **found["claims"]}
            token = self.sign(claims)
            if self.tamper == "payload":
                head, body, sig = token.split(".")
                changed = json.loads(base64.urlsafe_b64decode(body + "=="))
                changed["email"] = "boss@acme.example"
                token = f"{head}.{b64(json.dumps(changed).encode())}.{sig}"
            return httpx.Response(200, json={"access_token": "at", "token_type": "Bearer", "id_token": token})
        return httpx.Response(404)


@pytest.fixture
def idp(monkeypatch):
    found = FakeIdP()

    async def resolve(host, port):
        return ["93.184.216.34"] if host == "idp.acme.example" else ["10.0.0.7"]

    monkeypatch.setattr(oauth, "_resolve", resolve)
    monkeypatch.setattr(oauth, "TRANSPORT", httpx.MockTransport(found.handler))
    sso._discovered.clear()
    sso._keys.clear()
    yield found
    sso._discovered.clear()
    sso._keys.clear()


def browser(app, host=PORTAL):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"https://{host}")


async def log_in(c, user):
    return await c.post("/login", data={"username": user, "password": PASSWORD, "next": "/"},
                        headers={"Origin": str(c.base_url).rstrip("/")})


def provider(**more):
    return {"name": "Acme ID", "issuer": ISSUER, "client_id": CLIENT, "client_secret": SECRET, "domains": "acme.example",
            "role": "user", "mfa": True, **more}


def portal_app(settings, policy="nobody"):
    settings.options = parse_options({"portal": {"address": PORTAL, "second_factor": policy}})
    return create_app(settings)


async def configure(app, body=None):
    async with browser(app) as c:
        await log_in(c, "mario")  # the admin of acme (no second step with the policy nobody)
        return await c.post("/api/workspaces/acme/sso/save", json=body or provider(), headers={"Origin": f"https://{PORTAL}"})


async def sign_in(app, idp, email, c, **claims):
    """The person's browser c: «Log in with Acme ID», the provider, the way back."""
    resp = await c.get("/sso/acme/start?next=/jobs")
    assert resp.status_code == 303, resp.text
    assert "omniacli_sso=" in resp.headers["set-cookie"] and "samesite=lax" in resp.headers["set-cookie"].lower()
    back = idp.authorize(resp.headers["location"], email, **claims)
    assert back.startswith(f"https://{PORTAL}/sso/acme/callback?")
    return await c.get(back[len(f"https://{PORTAL}"):]), c


def entries(app, event=None):
    return [e for e in app.state.access_log.recent() if event is None or e["event"] == event]


async def test_the_admin_sets_it_up_and_the_secret_is_never_shown(env, workspaces, idp):
    app = portal_app(env["settings"])
    resp = await configure(app)
    assert resp.status_code == 200, resp.text
    path = app.state.sso.path
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    async with browser(app) as c:
        await log_in(c, "mario")
        page = (await c.get("/workspaces/acme")).text
        assert SECRET not in page and "Acme ID" in page and f"https://{PORTAL}/sso/acme/callback" in page
        assert f"https://{PORTAL}/login?sso=acme" in page
        # changed without the secret: the one saved stays
        resp = await c.post("/api/workspaces/acme/sso/save", json=provider(client_secret="", name="Acme"),
                            headers={"Origin": f"https://{PORTAL}"})
        assert resp.status_code == 200 and app.state.sso.provider("acme")["client_secret"] == SECRET
        # a user of the workspace is no admin of it
        await log_in(c, "luca")
        resp = await c.post("/api/workspaces/acme/sso/save", json=provider(), headers={"Origin": f"https://{PORTAL}"})
        assert resp.status_code == 403
    logged = [e["detail"] for e in entries(app, "sso")]
    assert logged[-1].startswith("single sign-on of acme set: Acme ID, https://idp.acme.example, domains acme.example")
    assert all(SECRET not in d for d in logged)
    text = "".join(p.read_text() for p in app.state.access_log.files())
    assert SECRET not in text


async def test_the_discovery_goes_through_the_ssrf_guard(env, workspaces, idp, monkeypatch):
    app = portal_app(env["settings"])
    resp = await configure(app, provider(issuer="https://idp.internal.example"))
    assert resp.status_code == 400 and "not of the internet (10.0.0.7)" in resp.json()["error"]
    resp = await configure(app, provider(issuer="http://idp.acme.example"))
    assert resp.status_code == 400 and "https://" in resp.json()["error"]
    assert not idp.calls
    # a network of the LAN the main configuration lets in (sso.private_networks)
    import ipaddress
    env["settings"].sso_networks = (ipaddress.ip_network("10.0.0.0/8"),)
    resp = await configure(app, provider(issuer="https://idp.internal.example"))
    # reached now: its answer (404 here) is what refuses it, after the guard
    assert resp.status_code == 400 and "answered 404" in resp.json()["error"]


async def test_a_new_person_gets_a_workspace_user(env, workspaces, idp):
    app = portal_app(env["settings"])
    assert (await configure(app)).status_code == 200
    async with browser(app) as c:
        page = (await c.get("/login?sso=acme")).text
        assert "Log in with Acme ID" in page and 'href="/sso/acme/start?next=/"' in page
        resp, _ = await sign_in(app, idp, "anna@acme.example", c)
        # a page that moves on by itself (the session cookie is SameSite strict)
        assert resp.status_code == 200 and 'id="go-on"' in resp.text and 'href="/jobs"' in resp.text
        assert "omniacli_session=" in resp.headers["set-cookie"] and "omniacli_sso_ws=acme" in resp.headers["set-cookie"]
        assert (await c.get("/api/servers")).status_code == 200
        # the next time the login page shows the button by itself
        c.cookies.delete("omniacli_session")
        assert "Log in with Acme ID" in (await c.get("/login")).text
    settings = env["settings"]
    assert settings.user_role("anna@acme.example") == "workspace"
    assert settings.workspaces["acme"].members["anna@acme.example"] == "user"
    assert [d for d in [e["detail"] for e in entries(app, "sso")] if d.startswith("user made")] == [
        "user made by the single sign-on of Acme ID: role user in acme"]
    assert "single sign-on Acme ID, its MFA as the second step" in [e["detail"] for e in entries(app, "login")]
    # the same person again: the same user, linked by issuer and subject
    async with browser(app) as c:
        resp, _ = await sign_in(app, idp, "anna@acme.example", c)
        assert resp.status_code == 200
    assert sum(1 for n in settings.users if n.startswith("anna")) == 1
    assert app.state.sso.users_of("acme") == {"anna@acme.example": "anna@acme.example"}


async def test_what_is_refused(env, workspaces, idp):
    app = portal_app(env["settings"])
    assert (await configure(app)).status_code == 200
    async with browser(app) as c:
        # another domain
        resp, _ = await sign_in(app, idp, "eve@evil.example", c)
        assert resp.status_code == 403 and "not one this workspace lets in" in resp.text
        # an e-mail the provider does not say verified
        resp, _ = await sign_in(app, idp, "bob@acme.example", c, email_verified=False)
        assert resp.status_code == 403
        # a tampered ID token (the e-mail changed after the signature)
        idp.tamper = "payload"
        resp, _ = await sign_in(app, idp, "carl@acme.example", c)
        assert resp.status_code == 401 and "was not accepted" in resp.text
        idp.tamper = None
        # the answer of a flow this browser did not start: no cookie
        start = await c.get("/sso/acme/start")
        back = idp.authorize(start.headers["location"], "dan@acme.example")
    async with browser(app) as other:
        resp = await other.get(back[len(f"https://{PORTAL}"):])
        assert resp.status_code == 400 and "started in another browser" in resp.text
    async with browser(app) as c:
        # the provider said no
        start = await c.get("/sso/acme/start")
        state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
        resp = await c.get(f"/sso/acme/callback?error=access_denied&state={state}")
        assert resp.status_code == 401
        # a user of the machine with that name is never taken by a provider
    env["settings"].users["root@acme.example"] = env["settings"].users["admin"]
    env["settings"].user_roles["root@acme.example"] = "admin"
    async with browser(app) as c:
        resp, _ = await sign_in(app, idp, "root@acme.example", c)
        assert resp.status_code == 403
    assert not any(n in env["settings"].users for n in ("eve@evil.example", "bob@acme.example", "carl@acme.example",
                                                        "boss@acme.example", "dan@acme.example"))
    failed = [e["detail"] for e in entries(app, "failed")]
    assert "single sign-on Acme ID (acme): the e-mail's domain evil.example is not allowed" in failed
    assert any("the signature of the ID token is not the provider's" in d for d in failed)
    assert any("(email_verified)" in d for d in failed)
    assert any("started (state)" in d for d in failed)
    assert any("the provider answered access_denied" in d for d in failed)
    assert any("exists and is not a user of this workspace only" in d for d in failed)


async def test_an_existing_user_of_the_workspace_is_linked(env, workspaces, idp):
    settings = env["settings"]
    settings.users["lia@acme.example"] = settings.users["luca"]
    settings.user_roles["lia@acme.example"] = "workspace"
    settings.workspaces["acme"].members["lia@acme.example"] = "developer"
    app = portal_app(settings)
    assert (await configure(app)).status_code == 200
    async with browser(app) as c:
        resp, _ = await sign_in(app, idp, "lia@acme.example", c)
    assert resp.status_code == 200
    assert settings.workspaces["acme"].members["lia@acme.example"] == "developer"  # its role stays
    assert not [e for e in entries(app, "sso") if e["detail"].startswith("user made")]


async def test_without_the_providers_mfa_the_second_step_is_asked(env, workspaces, idp):
    app = portal_app(env["settings"], policy="portal")
    # mario sets up his own second step first (required at the portal)
    from .test_two_step import set_up
    async with browser(app) as c:
        await set_up(app, c, "mario")
        resp = await c.post("/api/workspaces/acme/sso/save", json=provider(mfa=False), headers={"Origin": f"https://{PORTAL}"})
        assert resp.status_code == 200
    async with browser(app) as c:
        resp, _ = await sign_in(app, idp, "nina@acme.example", c)
        # the set-up of the code next, on a page that moves on by itself (its cookie of the login's path)
        assert resp.status_code == 200 and 'href="/login/setup"' in resp.text and "omniacli_step=" in resp.headers["set-cookie"]
        assert "omniacli_session=" not in resp.headers["set-cookie"]
        assert (await c.get("/api/servers")).status_code == 401
        assert (await c.get("/login/setup")).status_code == 200


def test_the_checks_of_an_id_token():
    idp = FakeIdP()
    now = int(time.time())
    good = {"iss": ISSUER, "aud": CLIENT, "sub": "s", "iat": now, "exp": now + 60, "nonce": "n1"}
    keys = [idp.jwk()]
    assert sso.verify_id_token(idp.sign(good), keys, ISSUER, CLIENT, "n1")["sub"] == "s"
    cases = {
        "the ID token is of another issuer": {**good, "iss": "https://other.example"},
        "the ID token is for another client": {**good, "aud": "someone-else"},
        "the ID token has expired": {**good, "exp": now - 600},
        "the ID token was issued in the future": {**good, "iat": now + 600},
        "the ID token is not of this login (nonce)": {**good, "nonce": "n2"},
        "the ID token was given to another client (azp)": {**good, "aud": [CLIENT, "x"], "azp": "x"},
    }
    for said, claims in cases.items():
        with pytest.raises(sso.SsoError, match=said.replace("(", r"\(").replace(")", r"\)")):
            sso.verify_id_token(idp.sign(claims), keys, ISSUER, CLIENT, "n1")
    # alg none and an HMAC with the public key's bytes: refused before any key
    for alg in ("none", "HS256"):
        token = idp.sign(good, {"alg": alg, "kid": "k1"})
        with pytest.raises(sso.SsoError, match="not accepted"):
            sso.verify_id_token(token, keys, ISSUER, CLIENT, "n1")
    # another provider's key
    with pytest.raises(sso.SsoError, match="signature"):
        sso.verify_id_token(FakeIdP().sign(good), keys, ISSUER, CLIENT, "n1")
    # ES256 too
    ec_idp = FakeIdP("ES256")
    assert sso.verify_id_token(ec_idp.sign({**good}, {"alg": "ES256", "kid": "k1"}), [ec_idp.jwk()], ISSUER, CLIENT, "n1")


def test_the_form_of_a_provider():
    with pytest.raises(sso.SsoError, match="https"):
        sso.provider_of(provider(issuer="http://x.example"), None)
    with pytest.raises(sso.SsoError, match="not an e-mail domain"):
        sso.provider_of(provider(domains="acme"), None)
    with pytest.raises(sso.SsoError, match="client secret"):
        sso.provider_of(provider(client_secret=""), None)
    found = sso.provider_of(provider(domains="@Acme.example, acme.example beta.example"), None)
    assert found["domains"] == ["acme.example", "beta.example"] and found["mfa"] is True
    assert "client_secret" not in sso.shown(found) and sso.shown(found)["secret_set"]
