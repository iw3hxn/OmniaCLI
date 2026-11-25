# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""AD-018 slice 2, the review: the machine's own records decide, never only the central's import. A token deleted, a
member removed, a role lowered, a password changed, on the machine or through the central, count at once; a token is
never more than its person's role; a code of the app is taken once whichever panel it is typed on; a machine's redirect
never leaves the central. Uses the fixture of tests/test_remote_home.py."""

import dataclasses
import datetime
import time

import httpx
import pytest

from omniacli import placement, totp
from omniacli.auth import hash_password
from omniacli.config import parse_servers, server_to_dict

from .conftest import hash_token
from .test_remote_home import (ACME_TOKEN, ADMIN, CENTRAL_LINK_TOKEN, ORIGIN, PASSWORD, SECRET, home,  # noqa: F401
                               logged_in, switch)

JSON = {"Accept": "application/json", "Origin": ORIGIN}


def code_now() -> str:
    return totp.totp(SECRET, time.time())


async def refused_login(c, name: str, password: str = PASSWORD) -> httpx.Response:
    browser = httpx.AsyncClient(transport=httpx.ASGITransport(app=c.app), base_url=ORIGIN)
    return await browser.post("/login", data={"username": name, "password": password, "next": "/"},
                              headers={"Origin": ORIGIN})


async def test_a_token_deleted_through_the_central_is_refused_at_once(home):
    await switch(home, True)
    token = {"Authorization": f"Bearer {ACME_TOKEN}", "Accept": "application/json"}
    assert (await home.get("/api/v1/instances", headers=token)).status_code == 200
    anna = await logged_in(home, "anna", code_now())
    deleted = await anna.post("/api/workspaces/acme/tokens/delete", json={"name": "t-bruno"}, headers=JSON)
    assert deleted.status_code == 200, deleted.text
    # read again from the machine after the change made through here: the token is no more served
    assert (await home.get("/api/v1/instances", headers=token)).status_code == 401
    assert not [t for t in home.app.state.serving().tokens.values() if t.name == "t-bruno"]


async def test_a_token_deleted_on_the_machine_is_refused_by_the_machine_whatever_the_central_says(home):
    await switch(home, True)
    token = {"Authorization": f"Bearer {ACME_TOKEN}", "Accept": "application/json"}
    m = home.machine_app.state.settings
    web = m.web
    web.tokens.pop("t-bruno")
    m.set_web_access(web)
    # the central's import still has it: the machine's own records refuse its scope
    resp = await home.get("/api/v1/instances", headers=token)
    assert resp.status_code in (401, 403) and resp.status_code != 200
    failed = [e for e in home.machine_app.state.access_log.recent(50) if e.get("event") == "failed"]
    assert any("delegated scope refused" in e.get("detail", "") for e in failed)


async def test_a_member_removed_through_the_central_loses_its_session_and_its_login(home):
    await switch(home, True)
    elena = await logged_in(home, "elena")
    anna = await logged_in(home, "anna", code_now())
    removed = await anna.post("/api/workspaces/acme/members/delete", json={"name": "elena"}, headers=JSON)
    assert removed.status_code == 200, removed.text
    assert (await elena.get("/api/servers", headers={"Accept": "application/json"})).status_code == 401
    assert (await refused_login(home, "elena")).status_code == 401


async def test_a_member_removed_on_the_machine_reaches_nothing_and_cannot_log_in(home):
    await switch(home, True)
    elena = await logged_in(home, "elena")
    m = home.machine_app.state.settings
    acme = m.workspaces["acme"]
    m.workspaces["acme"] = dataclasses.replace(acme, members={k: v for k, v in acme.members.items() if k != "elena"})
    # the machine refuses its scope at once, the central's import is older
    assert (await elena.get("/api/servers", headers={"Accept": "application/json"})).status_code in (403, 502)
    page = await elena.get("/", headers={"Accept": "text/html"})
    assert page.status_code in (403, 502) and "srv" not in page.text.split("<main>")[-1][:2000]
    # its login: the machine says it is no member
    assert (await refused_login(home, "elena")).status_code == 401


async def test_a_role_lowered_and_a_password_changed_on_the_machine_count_at_once(home):
    await switch(home, True)
    bruno = await logged_in(home, "bruno")
    m = home.machine_app.state.settings
    acme = m.workspaces["acme"]
    m.workspaces["acme"] = dataclasses.replace(acme, members={**acme.members, "bruno": "user"})
    # a developer's action: refused now by the machine's own record
    resp = await bruno.post("/servers/srv/test/pull", headers=JSON)
    assert resp.status_code == 403, resp.text
    # a new password there: the old one is refused here, the new one logs in (the machine checks it)
    web = m.web
    web.users["elena"] = hash_password("a-new-password", iterations=1000)
    m.set_web_access(web)
    home.machine_app.state.authenticator.users = m.users
    assert (await refused_login(home, "elena")).status_code == 401
    browser = httpx.AsyncClient(transport=httpx.ASGITransport(app=home.app), base_url=ORIGIN)
    ok = await browser.post("/login", data={"username": "elena", "password": "a-new-password", "next": "/"},
                            headers={"Origin": ORIGIN})
    assert ok.status_code == 303 and ok.headers["location"] == "/", ok.text[:300]
    # and the central read its record again: the new hash is the one its sessions are bound to
    record = home.app.state.identity_store.load("lab4")
    assert record["people"]["elena"]["hash"] == m.users["elena"]


async def test_a_code_is_taken_once_whichever_panel(home):
    await switch(home, True)
    code = code_now()
    machine = httpx.AsyncClient(transport=httpx.ASGITransport(app=home.machine_app), base_url="http://lab4.test")
    await machine.post("/login", data={"username": "anna", "password": PASSWORD, "next": "/"},
                       headers={"Origin": "http://lab4.test"})
    used = await machine.post("/login/code", data={"code": code}, headers={"Origin": "http://lab4.test"})
    assert used.status_code == 303 and used.headers["location"] == "/"
    # the same code on the central: refused (one counter, the machine's)
    browser = httpx.AsyncClient(transport=httpx.ASGITransport(app=home.app), base_url=ORIGIN)
    first = await browser.post("/login", data={"username": "anna", "password": PASSWORD}, headers={"Origin": ORIGIN})
    assert first.headers.get("location") == "/login/code"
    assert (await browser.post("/login/code", data={"code": code}, headers={"Origin": ORIGIN})).status_code == 401
    # a recovery code of the machine works here too, once
    codes = home.machine_app.state.second_factors.enable("anna", SECRET, -1)
    again = httpx.AsyncClient(transport=httpx.ASGITransport(app=home.app), base_url=ORIGIN)
    await again.post("/login", data={"username": "anna", "password": PASSWORD}, headers={"Origin": ORIGIN})
    ok = await again.post("/login/code", data={"code": codes[0]}, headers={"Origin": ORIGIN})
    assert ok.status_code == 303 and ok.headers["location"] == "/", ok.text[:300]
    third = httpx.AsyncClient(transport=httpx.ASGITransport(app=home.app), base_url=ORIGIN)
    await third.post("/login", data={"username": "anna", "password": PASSWORD}, headers={"Origin": ORIGIN})
    assert (await third.post("/login/code", data={"code": codes[0]}, headers={"Origin": ORIGIN})).status_code == 401


async def test_a_token_is_never_more_than_its_person_on_both_sides(home):
    m = home.m
    m.servers["srv2"] = parse_servers({"srv2": server_to_dict(m.servers["srv"])}, m.logs_dir)["srv2"]
    acme = m.workspaces["acme"]
    m.workspaces["acme"] = dataclasses.replace(acme, servers=[*acme.servers, "srv2"],
                                               token_people={**acme.token_people, "t-elena": "elena"})
    web = m.web
    web.tokens.update({"t-elena": hash_token("elena-admin-token-0123456789abcdef"),
                       "t-lim": hash_token("lim-token-0123456789abcdef0123")})
    web.token_roles.update({"t-elena": "admin", "t-lim": "developer"})
    web.token_workspaces.update({"t-elena": "acme", "t-lim": "acme"})
    web.token_servers["t-lim"] = ("srv",)
    m.set_web_access(web)
    home.machine_app.state.authenticator.tokens = m.tokens
    assert (await home.post("/api/identities/lab4/import", headers=ADMIN, json={"confirm": "lab4"})).status_code == 200
    await switch(home, True)
    served = {t.name: t for t in home.app.state.serving().tokens.values()}
    assert served["t-elena"].role == "user"
    elena = {"Authorization": "Bearer elena-admin-token-0123456789abcdef", "Accept": "application/json"}
    direct = await home.on_machine.post("/api/v1/instances/srv/test/restart", headers={**elena, "Idempotency-Key": "a"},
                                        json={})
    central = await home.post("/api/v1/instances/srv/test/restart", headers={**elena, "Idempotency-Key": "b"}, json={})
    assert direct.status_code == central.status_code == 403, (direct.status_code, direct.text, central.status_code, central.text)
    # the machine caps it on its own too, whatever level the scope says
    scope = placement.scope_encode({"ws": "acme", "person": "t-elena", "kind": "token", "role": "admin", "grant": None})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=home.machine_app), base_url="http://lab4.test") as c:
        resp = await c.post("/api/v1/instances/srv/test/restart", json={}, headers={
            placement.SCOPE_HEADER: scope, "Authorization": f"Bearer {CENTRAL_LINK_TOKEN}", "Idempotency-Key": "c"})
        assert resp.status_code == 403 and "read level" in resp.text
    lim = {"Authorization": "Bearer lim-token-0123456789abcdef0123", "Accept": "application/json"}
    for path in ("/api/v1/instances/srv2/test",):
        assert (await home.on_machine.get(path, headers=lim)).status_code == (await home.get(path, headers=lim)).status_code


@pytest.mark.parametrize("location", ["/\\evil.example/phish", "/\t/evil.example", "//evil.example", "https://evil.example/",
                                      "/\\\\evil.example", " //evil.example"])
async def test_a_machine_redirect_elsewhere_is_dropped(home, location):
    await switch(home, True)
    bruno = await logged_in(home, "bruno")

    def redirect(request):
        return httpx.Response(303, headers={"location": location, "content-type": "application/json"}, content=b"{}")
    home.machine.answer["/servers/srv/test/restart"] = redirect
    resp = await bruno.post("/servers/srv/test/restart", headers={"Origin": ORIGIN})
    assert "location" not in resp.headers, resp.headers.get("location")


async def test_a_redirect_of_the_central_own_paths_passes(home):
    await switch(home, True)
    bruno = await logged_in(home, "bruno")

    def redirect(request):
        return httpx.Response(303, headers={"location": "/jobs/20261125-120000-abcdef", "content-type": "application/json"},
                              content=b"{}")
    home.machine.answer["/servers/srv/test/restart"] = redirect
    resp = await bruno.post("/servers/srv/test/restart", headers={"Origin": ORIGIN})
    assert resp.headers.get("location") == "/jobs/20261125-120000-abcdef"


async def test_an_old_record_is_read_again_by_itself(home):
    await switch(home, True)
    store = home.app.state.identity_store
    record = store.load("lab4")
    record["at"] = (datetime.datetime.now() - datetime.timedelta(hours=1)).isoformat(timespec="seconds")
    store.save("lab4", record)
    elena = await logged_in(home, "elena")
    m = home.machine_app.state.settings
    acme = m.workspaces["acme"]
    m.workspaces["acme"] = dataclasses.replace(acme, members={k: v for k, v in acme.members.items() if k != "carla"})
    await elena.get("/api/servers", headers={"Accept": "application/json"})
    for _ in range(50):
        if "carla" not in (store.load("lab4") or {}).get("people", {}):
            break
        await __import__("asyncio").sleep(0.1)
    assert "carla" not in store.load("lab4")["people"]
