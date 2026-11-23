# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Passkeys (passkeys.py, README Passkeys): registered on Account, a login
without password nor code, the second step after the password, the RP ID of
the address the browser uses, the checks of py_webauthn (challenge once,
origin, counter), the last second step that cannot go, the admins' reset,
what the access log says. The browser's side is tests/softkey.py."""
import os
import re
import stat

import httpx
import pytest

from omniacli import passkeys
from omniacli.app import create_app
from omniacli.config import parse_options

from .conftest import PASSWORD
from .softkey import SoftKey
from .test_workspaces import workspaces  # noqa: F401 - the workspaces fixture

pytestmark = pytest.mark.skipif(not passkeys.available(), reason="py_webauthn needs Python 3.10")

PANEL = "omnia.example.test"
PORTAL = "portale.hosting.test"


def browser(app, host=PANEL, scheme="https"):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"{scheme}://{host}",
                             headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                                                    "Chrome/131.0 Safari/537.36"})


def origin(c) -> str:
    return str(c.base_url).rstrip("/")


async def log_in(c, user, password=PASSWORD):
    return await c.post("/login", data={"username": user, "password": password, "next": "/jobs"},
                        headers={"Origin": origin(c)})


async def post(c, path, body):
    return await c.post(path, json=body, headers={"Origin": origin(c)})


async def register(c, key: SoftKey, name="Portatile"):
    resp = await post(c, "/api/account/passkeys/options", {"name": name})
    assert resp.status_code == 200, resp.text
    options = resp.json()["publicKey"]
    assert options["authenticatorSelection"]["residentKey"] == "required"
    assert options["authenticatorSelection"]["userVerification"] == "required"
    assert options["attestation"] == "none"
    return await post(c, "/api/account/passkeys/register", {"credential": key.create(options, origin(c))})


async def passkey_login(c, key: SoftKey, **said):
    resp = await post(c, "/login/passkey/options", {})
    assert resp.status_code == 200, resp.text
    options = resp.json()["publicKey"]
    assert options.get("allowCredentials", []) == [] and options["userVerification"] == "required"
    return await post(c, "/login/passkey", {"credential": key.get(options, said.pop("at", origin(c)), **said),
                                            "next": "/jobs"})


def entries(app, event=None):
    return [e for e in app.state.access_log.recent() if event is None or e["event"] == event]


async def test_register_then_log_in_without_password_nor_code(env):
    app = create_app(env["settings"])
    key = SoftKey()
    async with browser(app) as c:
        await log_in(c, "admin")
        page = (await c.get("/account")).text
        assert 'id="pk-add"' in page and "No passkey yet." in page
        resp = await register(c, key)
        assert resp.status_code == 200, resp.text
        page = (await c.get("/account")).text
        assert "<strong>Portatile</strong>" in page and f"{PANEL}" in page and ">here<" in page
        # the file: mode 600, the public key only, the name the person gave it
        path = app.state.passkeys.path
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600 and "Portatile" in path.read_text()
    async with browser(app) as c:
        resp = await passkey_login(c, key)
        assert resp.status_code == 200 and resp.json() == {"next": "/jobs"}
        assert "omniacli_session" in resp.headers["set-cookie"]
        assert (await c.get("/api/servers")).status_code == 200
        page = (await c.get("/account")).text
        assert "Passkey, without password" in page
    logins = [e["detail"] for e in entries(app, "login")]
    assert "passkey «Portatile», without password" in logins
    assert any(e["detail"] == f"passkey «Portatile» registered for {PANEL}" for e in entries(app, "passkey"))
    assert app.state.passkeys.keys("admin")[0]["count"] == 1 and app.state.passkeys.keys("admin")[0]["used"]


async def test_the_checks_of_an_assertion(env):
    app = create_app(env["settings"])
    key = SoftKey()
    async with browser(app) as c:
        await log_in(c, "admin")
        assert (await register(c, key)).status_code == 200
    async with browser(app) as c:
        # another origin than the page's: refused
        resp = await passkey_login(c, key, at="https://evil.example")
        assert resp.status_code == 401 and resp.json()["error"] == "This passkey was not accepted."
        # in once (the counter here is 5), then a counter that does not grow (a cloned key): refused
        assert (await passkey_login(c, key, count=5)).status_code == 200
        c.cookies.clear()
        resp = await passkey_login(c, key, count=5)
        assert resp.status_code == 401
        # a passkey this machine does not know
        resp = await passkey_login(c, SoftKey())
        assert resp.status_code == 401
        # the same answer twice: the challenge is taken once
        options = (await post(c, "/login/passkey/options", {})).json()["publicKey"]
        answer = key.get(options, origin(c))
        assert (await post(c, "/login/passkey", {"credential": answer})).status_code == 200
        c.cookies.clear()
        assert (await post(c, "/login/passkey", {"credential": answer})).status_code == 400
        # cross-site: refused before anything
        resp = await c.post("/login/passkey/options", json={}, headers={"Origin": "https://evil.example"})
        assert resp.status_code == 403
    failed = [e["detail"] for e in entries(app, "failed")]
    assert sum("passkey: «Portatile» not verified" in d for d in failed) == 2
    assert "passkey: a passkey this machine does not know" in failed


async def test_a_passkey_without_user_verification_is_no_login(env):
    app = create_app(env["settings"])
    async with browser(app) as c:
        await log_in(c, "admin")
        # an authenticator that does not verify the person: not registered (user verification required)
        resp = await register(c, SoftKey(uv=False))
        assert resp.status_code == 400 and not app.state.passkeys.keys("admin")


async def test_the_rp_id_is_the_address_used(env):
    app = create_app(env["settings"])
    key = SoftKey()
    async with browser(app) as c:
        await log_in(c, "admin")
        assert (await register(c, key)).status_code == 200
    # another name of the machine: the passkey is of the first one
    async with browser(app, host="other.example.test") as c:
        options = (await post(c, "/login/passkey/options", {})).json()["publicKey"]
        assert options["rpId"] == "other.example.test"
        resp = await post(c, "/login/passkey", {"credential": key.get(options, origin(c), rp_id=PANEL)})
        assert resp.status_code == 401
    # an IP address is no RP ID: no passkey offered there
    async with browser(app, host="192.0.2.10:8090", scheme="http") as c:
        assert 'id="pk-login"' not in (await c.get("/login")).text
        resp = await post(c, "/login/passkey/options", {})
        assert resp.status_code == 400 and "not on an address" in resp.json()["error"]
    # localhost over http (an SSH tunnel) is a secure context: offered
    async with browser(app, host="localhost:18090", scheme="http") as c:
        assert 'id="pk-login"' in (await c.get("/login")).text
        assert (await post(c, "/login/passkey/options", {})).json()["publicKey"]["rpId"] == "localhost"


async def test_a_passkey_is_the_second_step_after_the_password(env, workspaces):
    settings = env["settings"]
    settings.options = parse_options({"portal": {"address": PORTAL}})
    app = create_app(settings)
    key = SoftKey()
    # mario must have a second step at the portal: his passkey is it, no code of an app
    app.state.second_factors.disable("mario")
    async with browser(app, host=PORTAL) as c:
        await log_in(c, "mario")
        # first login: the set-up of the code (he has no second step yet)
        assert (await c.get("/login/setup")).status_code == 200
    # a passkey made from Account (a session made from the set-up first)
    from .test_two_step import set_up
    async with browser(app, host=PORTAL) as c:
        await set_up(app, c)
        assert (await register(c, key, "Telefono")).status_code == 200
        # the code of the app off: the passkey stays the second step
        state = app.state.second_factors
        state.disable("mario")
    async with browser(app, host=PORTAL) as c:
        resp = await log_in(c, "mario")
        assert resp.headers["location"] == "/login/code"
        page = (await c.get("/login/code")).text
        assert 'id="pk-step"' in page and 'name="code"' not in page and "confirm with your passkey" in page
        options = (await post(c, "/login/code/passkey/options", {})).json()["publicKey"]
        assert [k["id"] for k in options["allowCredentials"]] == [app.state.passkeys.keys("mario")[0]["id"]]
        resp = await post(c, "/login/code/passkey", {"credential": key.get(options, origin(c))})
        assert resp.status_code == 200 and resp.json()["next"] == "/jobs"
        assert (await c.get("/api/servers")).status_code == 200
        # the last second step of one who must have one cannot go
        kid = app.state.passkeys.keys("mario")[0]["id"]
        resp = await post(c, "/api/account/passkeys/delete", {"id": kid})
        assert resp.status_code == 403 and "only second step" in resp.json()["error"]
        page = (await c.get("/account")).text
        assert "Password and a passkey" in page or "and a passkey" in page
    assert "login form, second step: passkey «Telefono»" in [e["detail"] for e in entries(app, "login")]
    # a passkey of the step without its pending login (the password) is refused
    async with browser(app, host=PORTAL) as c:
        resp = await post(c, "/login/code/passkey/options", {})
        assert resp.status_code == 401


async def test_remove_and_reset(env, workspaces):
    app = create_app(env["settings"])
    key = SoftKey()
    async with browser(app) as c:
        await log_in(c, "luca")
        assert (await register(c, key, "Telefono")).status_code == 200
        kid = app.state.passkeys.keys("luca")[0]["id"]
        # nobody must have a second step here: it goes
        assert (await post(c, "/api/account/passkeys/delete", {"id": kid})).status_code == 200
        assert not app.state.passkeys.keys("luca")
        assert (await register(c, key, "Telefono")).status_code == 200
    # the admins' reset of two-step verification takes the passkeys too (a lost phone)
    async with browser(app) as c:
        await log_in(c, "admin")
        page = (await c.get("/users")).text
        assert re.search(r'<tr data-user="luca".*?two-step on', page, re.S)
        resp = await post(c, "/api/access/users/second-factor/reset", {"name": "luca"})
        assert resp.status_code == 200
    assert not app.state.passkeys.keys("luca")
    assert any("1 passkeys removed" in e["detail"] for e in entries(app, "second-factor"))
    removed = [e["detail"] for e in entries(app, "passkey")]
    assert f"passkey «Telefono» of {PANEL} removed (Account)" in removed


async def test_a_deleted_user_takes_its_passkeys_with_it(env, workspaces):
    app = create_app(env["settings"])
    async with browser(app) as c:
        await log_in(c, "zoe")
        assert (await register(c, SoftKey())).status_code == 200
    async with browser(app) as c:
        await log_in(c, "admin")
        assert (await post(c, "/api/access/users/delete", {"name": "zoe"})).status_code == 200
    assert not app.state.passkeys.count("zoe")


def test_rp_and_origins():
    assert passkeys.rp_of("Omnia.Example.com:8443") == "omnia.example.com"
    assert passkeys.rp_of("10.0.0.5:8090") is None and passkeys.rp_of("[::1]:8090") is None
    assert passkeys.origins_of("omnia.example.com") == ["https://omnia.example.com"]
    assert passkeys.origins_of("localhost:18090") == ["https://localhost:18090", "http://localhost:18090"]
