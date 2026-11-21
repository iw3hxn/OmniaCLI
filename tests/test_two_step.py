# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Two-step verification (totp.py, README Two-step verification): the codes
of RFC 6238, the second step of the login, its throttle, the recovery codes
used once, who must use it, its switch on Account, its reset by the admins,
and what the access log says of all of it."""
import os
import re
import stat
import time

import httpx
import pytest

from omniacli import totp
from omniacli.app import create_app
from omniacli.config import ConfigError, load_options, parse_options, save_options

from .conftest import PASSWORD, TOKEN, BasicAsSession
from .test_app import AUTH, POST, client  # noqa: F401 - the client fixture
from .test_workspaces import basic, post_as, workspaces  # noqa: F401 - the workspaces fixture

PORTAL = "portale.hosting.test"


# --- the codes ------------------------------------------------------------

def test_the_vectors_of_rfc_6238_appendix_b():
    keys = {"sha1": b"12345678901234567890", "sha256": b"12345678901234567890123456789012",
            "sha512": b"1234567890123456789012345678901234567890123456789012345678901234"}
    table = [(59, "94287082", "46119246", "90693936"),
             (1111111109, "07081804", "68084774", "25091201"),
             (1111111111, "14050471", "67062674", "99943326"),
             (1234567890, "89005924", "91819424", "93441116"),
             (2000000000, "69279037", "90698825", "38618901"),
             (20000000000, "65353130", "77737706", "47863826")]
    for at, *codes in table:
        for digest, code in zip(("sha1", "sha256", "sha512"), codes):
            assert totp.totp(keys[digest], at, digits=8, digest=digest) == code, (at, digest)
    # RFC 4226 Appendix D, under them; and the base32 secret an app reads gives the same
    assert [totp.hotp(keys["sha1"], n) for n in range(3)] == ["755224", "287082", "359152"]
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # base32 of 12345678901234567890
    assert totp.totp(secret, 59) == "287082"


def test_a_code_is_taken_near_its_time_and_once():
    secret = totp.new_secret()
    assert len(secret) == 32 and re.fullmatch(r"[A-Z2-7]+", secret)
    now = 1_800_000_015.0
    code = totp.totp(secret, now)
    step = int(now // 30)
    assert totp.code_step(secret, code, now) == step
    assert totp.code_step(secret, f"{code[:3]} {code[3:]}", now + 30) == step  # a phone a step behind, spaced
    assert totp.code_step(secret, code, now + 90) is None
    assert totp.code_step(secret, code, now, after=step) is None  # used already
    assert totp.code_step(secret, "12345", now) is None
    uri = totp.otpauth_uri(secret, "mario@portale.test")
    assert uri.startswith("otpauth://totp/OmniaCLI:mario@portale.test?secret=" + secret)
    assert "algorithm=SHA1&digits=6&period=30" in uri
    assert totp.qr_svg(uri).startswith("<svg")


def test_the_store_keeps_the_recovery_codes_hashed_and_strikes_them(tmp_path):
    path = tmp_path / "second-factor.json"
    store = totp.SecondFactors(path)
    secret = totp.new_secret()
    now = time.time()
    codes = store.enable("mario", secret, int(now // 30) - 5)
    assert len(codes) == 10 == len(set(codes)) and all(re.fullmatch(r"[a-z2-9]{5}-[a-z2-9]{5}", c) for c in codes)
    text = path.read_text()
    assert secret in text and not any(c in text or c.replace("-", "") in text for c in codes)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert store.check("mario", codes[3].upper()) == ("recovery", 9)
    assert store.check("mario", codes[3]) is None  # once
    assert store.check("mario", totp.totp(secret, now), now) == ("code", 9)
    assert store.check("mario", totp.totp(secret, now), now) is None  # not twice
    again = totp.SecondFactors(path)  # after a restart
    assert again.state("mario")["left"] == 9 and again.check("mario", codes[3]) is None
    again.keep({"luca"})
    assert not again.on("mario") and not totp.SecondFactors(path).on("mario")


def test_the_policy_option():
    assert parse_options({}).second_factor == "portal"
    assert parse_options({"portal": {"second_factor": "everybody"}}).second_factor == "everybody"
    with pytest.raises(ConfigError, match="second_factor"):
        parse_options({"portal": {"second_factor": "some"}})


def test_the_policy_is_saved_only_when_changed(tmp_path):
    path = tmp_path / "options.yaml"
    save_options(path, parse_options({}))
    assert "second_factor" not in path.read_text()  # an upgrade writes the file as before
    save_options(path, parse_options({"portal": {"second_factor": "nobody"}}))
    assert load_options(path).second_factor == "nobody"


# --- the login --------------------------------------------------------------

def portal_app(settings, policy=None):
    settings.options = parse_options({"portal": {"address": PORTAL, **({"second_factor": policy} if policy else {})}})
    return create_app(settings)


def browser(app, host=PORTAL, scheme="https"):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"{scheme}://{host}")


async def password(c, user, next_path="/", pw=PASSWORD):
    origin = str(c.base_url).rstrip("/")
    return await c.post("/login", data={"username": user, "password": pw, "next": next_path}, headers={"Origin": origin})


async def second(c, path, code):
    return await c.post(path, data={"code": code}, headers={"Origin": str(c.base_url).rstrip("/")})


def secret_of(page: str) -> str:
    return re.search(r'<code class="secret" id="secret">([A-Z2-7]+)</code>', page).group(1)


def codes_of(page: str) -> list[str]:
    return re.findall(r"[a-z2-9]{5}-[a-z2-9]{5}", re.search(r'<pre class="codes"[^>]*>(.*?)</pre>', page, re.S).group(1))


def entries(app, event=None):
    return [e for e in app.state.access_log.recent() if event is None or e["event"] == event]


async def set_up(app, c, user="mario", next_path="/jobs"):
    """A first login at the portal: password, set-up, recovery codes; the secret and the codes."""
    resp = await password(c, user, next_path)
    assert (resp.status_code, resp.headers["location"]) == (303, "/login/setup")
    assert "omniacli_session" not in resp.headers["set-cookie"]
    page = (await c.get("/login/setup")).text
    secret = secret_of(page)
    assert "<svg" in page and "otpauth" not in page  # the QR code drawn here; the URI is in it, not as text
    resp = await second(c, "/login/setup", totp.totp(secret))
    assert resp.status_code == 200 and "omniacli_session" in resp.headers["set-cookie"]
    codes = codes_of(resp.text)
    assert len(codes) == 10 and 'href="/jobs"' in resp.text and "showModal" in resp.text
    assert resp.headers["cache-control"] == "no-store"
    return secret, codes


async def test_a_customer_sets_it_up_after_the_password_and_logs_in_with_it(env, workspaces):
    app = portal_app(env["settings"])
    async with browser(app) as c:
        resp = await password(c, "mario")
        assert resp.headers["location"] == "/login/setup"
        assert (await c.get("/api/servers")).status_code == 401  # no session before the second step
        # a wrong code: no session, a failed login
        resp = await second(c, "/login/setup", "000000")
        assert resp.status_code == 401 and "Wrong code." in resp.text and "omniacli_session" not in str(resp.headers)
        c.cookies.clear()
        secret, codes = await set_up(app, c)
        assert (await c.get("/api/servers")).status_code == 200
        page = (await c.get("/account")).text
        assert "Recovery codes left: 10." in page and "it cannot be switched off" in page
    # again: the password, then a code of the app
    async with browser(app) as c:
        resp = await password(c, "mario", "/jobs")
        assert resp.headers["location"] == "/login/code"
        assert (await c.get("/login/setup")).headers["location"] == "/login/code"
        assert "type the code your authenticator app shows" in (await c.get("/login/code")).text
        code = totp.totp(secret, time.time() + 30)  # a phone a step ahead
        resp = await second(c, "/login/code", code)
        assert (resp.status_code, resp.headers["location"]) == (303, "/jobs")
        assert (await c.get("/api/servers")).status_code == 200
    # the same code again: refused (replay)
    async with browser(app) as c:
        await password(c, "mario")
        assert (await second(c, "/login/code", code)).status_code == 401
        # a recovery code: in, once
        resp = await second(c, "/login/code", codes[0])
        assert resp.status_code == 303
    async with browser(app) as c:
        await password(c, "mario")
        assert (await second(c, "/login/code", codes[0])).status_code == 401
    logs = entries(app)
    assert [e["detail"] for e in logs if e["event"] == "second-factor"] == [
        "recovery code used to log in: 9 left", "switched on at the login (required here)"]
    logins = [e["detail"] for e in logs if e["event"] == "login"]
    assert logins == ["login form, second step: recovery code", "login form, second step: code",
                      "login form, second step: code"]
    failed = [e["detail"] for e in logs if e["event"] == "failed"]
    assert failed.count("second step: wrong code") == 3 and all(e["name"] == "mario" for e in logs)
    assert all(e["workspaces"] == ["acme"] for e in logs if e["event"] in ("second-factor", "login"))
    # nothing of the secret or of the codes in the log
    text = "".join(p.read_text() for p in app.state.access_log.files())
    assert secret not in text and not any(c in text for c in codes)


async def test_wrong_codes_count_with_the_failed_logins(env, workspaces):
    app = portal_app(env["settings"])
    async with browser(app) as c:
        await set_up(app, c)
    async with browser(app) as c:
        await password(c, "mario")
        for n in range(totp.MAX_TRIES - 1):
            resp = await second(c, "/login/code", "111111")
            assert resp.status_code == 401 and "Wrong code." in resp.text
        resp = await second(c, "/login/code", "111111")
        assert resp.status_code == 401 and "Too many wrong codes" in resp.text
        assert (await c.get("/login/code")).headers["location"] == "/login"  # the password again
        # and the failed logins' wait holds for the password too
        resp = await password(c, "mario")
        assert resp.status_code == 429 and "Too many failed logins" in resp.text
    failed = [e["detail"] for e in entries(app, "failed")]
    assert "second step: wrong code, the password is asked again" in failed
    assert any(d.startswith("too many failed logins") for d in failed)


async def test_a_step_needs_its_cookie_and_the_same_site(env, workspaces):
    app = portal_app(env["settings"])
    async with browser(app) as c:
        assert (await c.get("/login/code")).headers["location"] == "/login"
        assert (await second(c, "/login/code", "123456")).headers["location"] == "/login"
        resp = await password(c, "mario")
        cookie = resp.headers["set-cookie"].lower()
        assert "omniacli_step=" in cookie and "path=/login" in cookie and "httponly" in cookie and "secure" in cookie
        resp = await c.post("/login/setup", data={"code": "123456"}, headers={"Origin": "https://evil.test"})
        assert resp.status_code == 403


async def test_an_invitation_leads_to_the_set_up(env, workspaces):
    app = portal_app(env["settings"])
    settings = env["settings"]
    # the machine's admin invites gino, on the machine's own address
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=BasicAsSession(app)),
                                 base_url="http://omniacli.test") as admin:
        made = (await admin.post("/api/workspaces/acme/members/save", headers=POST,
                                 json={"name": "gino", "role": "user", "new": True, "invite": True})).json()
    path = made["invite_path"]
    async with browser(app) as c:
        resp = await c.post(path, data={"password": "gino-password", "again": "gino-password"},
                            headers={"Origin": f"https://{PORTAL}"})
        assert (resp.status_code, resp.headers["location"]) == (303, "/login/setup")
        assert "omniacli_session" not in resp.headers["set-cookie"]
        secret = secret_of((await c.get("/login/setup")).text)
        resp = await second(c, "/login/setup", totp.totp(secret))
        assert resp.status_code == 200 and "omniacli_session" in resp.headers["set-cookie"]
    assert [e["detail"] for e in entries(app, "login")] == ["invitation, second step: code"]
    assert settings.users["gino"]


async def test_who_must_use_it(env, workspaces):
    settings = env["settings"]
    # no portal address (an upgrade): nobody is asked, as before
    settings.options = parse_options({})
    app = create_app(settings)
    async with browser(app, "omniacli.test", "http") as c:
        assert (await password(c, "mario")).headers["location"] == "/"
    # a portal: its users at its address, and the users of the workspaces at the machine's own too
    app = portal_app(settings)
    async with browser(app, "omniacli.test", "http") as c:
        assert (await password(c, "mario")).headers["location"] == "/login/setup"
        c.cookies.clear()
        assert (await password(c, "admin")).headers["location"] == "/"  # the machine's admin, on its own address
    # everybody
    app = portal_app(settings, "everybody")
    async with browser(app, "omniacli.test", "http") as c:
        assert (await password(c, "admin")).headers["location"] == "/login/setup"
    # nobody: only who switched it on
    app = portal_app(settings, "nobody")
    async with browser(app) as c:
        assert (await password(c, "mario")).headers["location"] == "/"
        c.cookies.clear()
        app.state.second_factors.enable("luca", totp.new_secret(), 0)
        assert (await password(c, "luca")).headers["location"] == "/login/code"


async def test_tokens_and_links_are_unaffected(env, workspaces):
    app = portal_app(env["settings"], "everybody")
    async with browser(app) as c:
        resp = await c.get("/api/servers", headers={"Authorization": f"Bearer {TOKEN}"})
        assert resp.status_code == 200


# --- the account, the admins ------------------------------------------------

async def test_the_account_switches_it_on_and_off(client, env):
    app = client.app
    me = {"Authorization": AUTH, "Origin": "http://omniacli.test"}
    page = (await client.get("/account", headers={"Authorization": AUTH})).text
    assert "Your password alone logs you in" in page and 'id="ts-start"' in page
    started = (await client.post("/api/account/second-factor/start", headers=me)).json()
    assert started["qr"].startswith("<svg") and started["uri"].startswith("otpauth://totp/OmniaCLI:admin@omniacli.test")
    resp = await client.post("/api/account/second-factor/confirm", headers=me, json={"code": "000000"})
    assert resp.status_code == 400 and resp.json()["error"].startswith("Wrong code")
    resp = await client.post("/api/account/second-factor/confirm", headers={**me, "Accept-Language": "it"},
                             json={"code": "000000"})
    assert resp.json()["error"].startswith("Codice sbagliato")
    resp = await client.post("/api/account/second-factor/confirm", headers=me,
                             json={"code": totp.totp(started["secret"])})
    codes = resp.json()["recovery"]
    assert len(codes) == 10 and resp.headers["cache-control"] == "no-store"
    assert (await client.post("/api/account/second-factor/start", headers=me)).status_code == 400  # on already
    page = (await client.get("/account", headers={"Authorization": AUTH})).text
    assert "Recovery codes left: 10." in page and 'id="ts-off"' in page
    assert ">two-step on<" in (await client.get("/users", headers={"Authorization": AUTH})).text
    resp = await client.post("/api/account/second-factor/disable", headers=me, json={"code": "nope"})
    assert resp.status_code == 400
    resp = await client.post("/api/account/second-factor/disable", headers=me, json={"code": codes[1]})
    assert resp.json() == {"ok": True}
    details = [e["detail"] for e in entries(app, "second-factor")]
    assert details == ["switched off (Account), with a recovery code", "switched on (Account)"]
    failed = [e["detail"] for e in entries(app, "failed")]
    assert failed == ["two-step switch-off: wrong code", "two-step set-up: wrong code", "two-step set-up: wrong code"]
    # the actions name no code
    assert not any("code" in e["detail"] for e in entries(app, "action"))


async def test_where_it_is_required_it_is_not_switched_off(client, env):
    settings = env["settings"]
    settings.options = parse_options({"portal": {"second_factor": "everybody"}})
    me = {"Authorization": AUTH, "Origin": "http://omniacli.test"}
    started = (await client.post("/api/account/second-factor/start", headers=me)).json()
    codes = (await client.post("/api/account/second-factor/confirm", headers=me,
                               json={"code": totp.totp(started["secret"])})).json()["recovery"]
    resp = await client.post("/api/account/second-factor/disable", headers=me, json={"code": codes[0]})
    assert resp.status_code == 403 and "required" in resp.json()["error"]


async def test_the_admins_reset_it(workspaces, client, env):
    app = client.app
    store = app.state.second_factors
    for name in ("mario", "luca", "eva"):
        store.enable(name, totp.new_secret(), 0)
    # the workspace's admin: its users of this workspace only, not itself, not another workspace's
    resp = await client.post("/api/workspaces/acme/members/second-factor/reset", headers=post_as("mario"), json={"name": "luca"})
    assert resp.json() == {"ok": True} and not store.on("luca")
    resp = await client.post("/api/workspaces/acme/members/second-factor/reset", headers=post_as("mario"), json={"name": "mario"})
    assert resp.status_code == 400 and store.on("mario")
    resp = await client.post("/api/workspaces/acme/members/second-factor/reset", headers=post_as("mario"), json={"name": "eva"})
    assert resp.status_code == 404 and store.on("eva")
    resp = await client.post("/api/access/users/second-factor/reset", headers=post_as("mario"), json={"name": "eva"})
    assert resp.status_code == 403 and store.on("eva")
    resp = await client.post("/api/workspaces/beta/members/second-factor/reset", headers=post_as("luca"), json={"name": "eva"})
    assert resp.status_code == 403
    # the workspace page shows who has it on, and the button where it may
    page = (await client.get("/workspaces/acme", headers=basic("mario"))).text
    assert page.count(">two-step on<") == 1 and "data-reset-two-step title" not in page  # its own only
    # the machine's admin: anyone
    assert "data-reset-two-step title" in (await client.get("/users", headers={"Authorization": AUTH})).text
    resp = await client.post("/api/access/users/second-factor/reset", headers=POST, json={"name": "eva"})
    assert resp.json() == {"ok": True} and not store.on("eva")
    resp = await client.post("/api/access/users/second-factor/reset", headers=POST, json={"name": "eva"})
    assert resp.status_code == 400
    resets = [(e["name"], e["detail"]) for e in entries(app, "second-factor")]
    assert resets == [("eva", "reset by admin (Users): it sets it up again"),
                      ("luca", "reset by mario (workspace acme): it sets it up again")]
    # acme's admin reads luca's reset in its access log, not eva's
    page = (await client.get("/workspaces/acme/audit", headers=basic("mario"))).text
    assert "reset by mario" in page and "reset by admin" not in page
    # a deleted user's goes with it
    resp = await client.post("/api/workspaces/acme/members/delete", headers=post_as("mario"), json={"name": "luca"})
    assert resp.json()["deleted"]
    store.enable("luca", totp.new_secret(), 0)
    assert (await client.post("/api/access/users/save", headers=POST,
                              json={"name": "zoe", "role": "user", "password": "long-enough"})).status_code == 200
    assert not store.on("luca") and store.on("mario")


async def test_the_options_page_sets_the_policy(client, env):
    page = (await client.get("/options", headers={"Authorization": AUTH})).text
    assert 'id="second-factor"' in page and '<option value="portal" selected>' in page
    resp = await client.post("/api/options/save", headers=POST, json={"portal": {"second_factor": "everybody"}})
    assert resp.status_code == 200 and env["settings"].options.second_factor == "everybody"
    # a page of before, that does not post it, keeps it
    assert (await client.post("/api/options/save", headers=POST, json={"portal": {"provider": "X"}})).status_code == 200
    assert env["settings"].options.second_factor == "everybody"
    assert [e["detail"] for e in entries(client.app, "second-factor")] == [
        "required for: portal -> everybody (Options)"]


def test_drop_user_ends_the_other_sessions(tmp_path):
    """A reset or switch-off of the second step ends the user's sessions (but the one that asked)."""
    from omniacli.sessions import SessionStore
    store = SessionStore(tmp_path / "s.json")
    users = {"ann": "hash-a", "bob": "hash-b"}
    one, two, other = store.create("ann", "hash-a"), store.create("ann", "hash-a"), store.create("bob", "hash-b")
    assert store.drop_user("ann", keep=one) == 1
    assert store.user(one, users) == "ann" and store.user(two, users) is None and store.user(other, users) == "bob"
    assert store.drop_user("ann") == 1 and store.user(one, users) is None
