# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A person's sessions (sessions.py, README Login and security, Sessions):
listed on Account with their device, address, start and last activity,
ended one by one or all the others, by the person or by an admin on Users;
the next request of an ended one is refused; the access log says who ended
which."""
import json
import time

import httpx

from omniacli import sessions as sessions_mod
from omniacli.app import create_app
from omniacli.sessions import SessionStore, device

from .conftest import PASSWORD

HOST = "omnia.example.test"
CHROME_LINUX = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
FIREFOX_WINDOWS = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0"


def browser(app, ua=CHROME_LINUX, ip="198.51.100.7"):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(ip, 50000)), base_url=f"https://{HOST}",
                             headers={"User-Agent": ua})


async def log_in(c, user):
    resp = await c.post("/login", data={"username": user, "password": PASSWORD, "next": "/"},
                        headers={"Origin": f"https://{HOST}"})
    assert resp.status_code == 303 and "omniacli_session" in resp.headers["set-cookie"]


async def post(c, path, body):
    return await c.post(path, json=body, headers={"Origin": f"https://{HOST}"})


def test_the_device_of_a_user_agent():
    assert device(CHROME_LINUX) == ("Chrome", "Linux")
    assert device(FIREFOX_WINDOWS) == ("Firefox", "Windows")
    assert device("Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
                  "Version/18.1 Mobile/15E148 Safari/604.1") == ("Safari", "iOS")
    assert device("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 "
                  "Safari/537.36 Edg/131.0") == ("Edge", "Windows")
    assert device("Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36 Chrome/131.0 Mobile Safari/537.36") == ("Chrome", "Android")
    assert device("curl/8.5.0") == ("curl", "") and device("") == ("", "")
    # a headless Chrome (Playwright) is Chrome, not the Safari its tail names
    assert device("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/149.0.0.0 "
                  "Safari/537.36") == ("Chrome", "Linux")


def test_the_store_lists_names_and_ends(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "s.json")
    a = store.create("anna", "h", CHROME_LINUX, "198.51.100.7", "password")
    b = store.create("anna", "h", FIREFOX_WINDOWS, "203.0.113.9", "passkey")
    store.create("bruno", "h")
    rows = store.listed("anna", current=a)
    assert [r["current"] for r in rows] == [True, False] and rows[1]["browser"] == "Firefox"
    # the name of a session opens nothing: neither its token nor the digest kept
    data = json.loads((tmp_path / "s.json").read_text())
    assert all(r["id"] not in data for r in rows) and a not in json.dumps(data)
    # the last activity is written at most every SEEN_EVERY
    before = (tmp_path / "s.json").read_text()
    store.seen(b, "203.0.113.10")
    assert (tmp_path / "s.json").read_text() == before and store.listed("anna")[0]["ip"] == "203.0.113.10"
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + sessions_mod.SEEN_EVERY + 1)
    store.seen(b, "203.0.113.10")
    assert (tmp_path / "s.json").read_text() != before
    monkeypatch.setattr(time, "time", real)
    gone = store.end("anna", rows[1]["id"])
    assert gone["browser"] == "Firefox" and store.user(b, {"anna": "h"}) is None and store.user(a, {"anna": "h"}) == "anna"
    assert store.end("bruno", rows[0]["id"]) is None  # another user's session is not named by it
    assert store.count() == {"anna": 1, "bruno": 1}


async def test_end_one_and_all_the_others_from_account(env):
    app = create_app(env["settings"])
    async with browser(app) as one, browser(app, FIREFOX_WINDOWS, "203.0.113.9") as two, \
            browser(app, ua="curl/8.5.0", ip="192.0.2.44") as three:
        for c in (one, two, three):
            await log_in(c, "admin")
        page = (await one.get("/account")).text
        assert "Sessions (3)" in page and "Chrome on Linux" in page and "Firefox on Windows" in page
        assert page.count('data-ss-end title') == 2 and "this session" in page and "198.51.100.7" in page
        assert 'id="ss-others"' in page
        rows = app.state.sessions.listed("admin")
        firefox = next(r for r in rows if r["browser"] == "Firefox")
        mine = next(r for r in rows if r["browser"] == "Chrome")
        # this browser's own: Log out ends it, not this button
        resp = await post(one, "/api/account/sessions/end", {"id": mine["id"]})
        assert resp.status_code == 400
        assert (await post(one, "/api/account/sessions/end", {"id": firefox["id"]})).status_code == 200
        # ended: its next request is refused at once
        assert (await two.get("/api/servers")).status_code == 401
        assert (await two.get("/jobs", headers={"Accept": "text/html"})).headers["location"].startswith("/login")
        assert (await three.get("/api/servers")).status_code == 200
        # an unknown or already ended one
        assert (await post(one, "/api/account/sessions/end", {"id": firefox["id"]})).status_code == 404
        resp = await post(one, "/api/account/sessions/end", {"all": True})
        assert resp.json() == {"ok": True, "ended": 1}
        assert (await three.get("/api/servers")).status_code == 401
        assert (await one.get("/api/servers")).status_code == 200
    ended = [e for e in app.state.access_log.recent() if e["event"] == "session"]
    assert len(ended) == 2 and all(e["name"] == "admin" for e in ended)
    assert any(e["detail"].startswith("session ended by admin (Account): Firefox on Windows from 203.0.113.9")
               for e in ended)


async def test_an_admin_ends_a_users_sessions_on_users(env):
    app = create_app(env["settings"])
    async with browser(app) as admin, browser(app, FIREFOX_WINDOWS) as dev:
        await log_in(admin, "admin")
        await log_in(dev, "dev")
        page = (await admin.get("/users")).text
        assert "Sessions (1)…" in page and 'id="sessions-dialog"' in page
        listed = (await admin.get("/api/access/users/sessions", params={"name": "dev"})).json()
        assert [s["device"] for s in listed["sessions"]] == ["Firefox on Windows"]
        assert listed["sessions"][0]["how_said"] == "Password"
        # a read of security data: in the access log
        assert any(e["event"] == "read" and "sessions of a user" in e["detail"] for e in app.state.access_log.recent())
        resp = await post(admin, "/api/access/users/sessions/end", {"name": "dev", "id": listed["sessions"][0]["id"]})
        assert resp.json() == {"ok": True, "ended": 1}
        assert (await dev.get("/api/servers")).status_code == 401
        # a user may not
        await log_in(dev, "dev")
        assert (await dev.get("/api/access/users/sessions", params={"name": "admin"})).status_code == 403
        assert (await post(dev, "/api/access/users/sessions/end", {"name": "admin", "all": True})).status_code == 403
        # all of a user's: the admin's own browser stays
        resp = await post(admin, "/api/access/users/sessions/end", {"name": "admin", "all": True})
        assert resp.json()["ended"] == 0 and (await admin.get("/api/servers")).status_code == 200
    assert any(e["detail"].startswith("session ended by admin (Users): Firefox on Windows")
               for e in app.state.access_log.recent() if e["event"] == "session")


async def test_the_sessions_in_italian(env):
    app = create_app(env["settings"])
    async with browser(app) as c:
        await log_in(c, "admin")
        page = (await c.get("/account", headers={"Accept-Language": "it-IT,it"})).text
        assert "Chrome su Linux" in page and "Sessioni (1)" in page and "questa sessione" in page
