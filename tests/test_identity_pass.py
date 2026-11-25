# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""AD-018 slice 5, «Il passaggio di una macchina e il ritorno» (§18.3): the central panel (lab3) that serves a
machine's workspaces (slice 2) takes their identities over. The machine freezes their identity changes («This
workspace passes to <portal>») and, once passed, switches its people's login off (the form, the sessions, the tokens,
the central's own login through the link) and checks the central's scope against what it kept of them, never a
secret; the people log in on the central with the same password and the same app, checked there, and change them
there; a person whose second step did not pass (a passkey made on the machine's name) never activates one with the
password only: a one-time link; «Back to the machine» carries back a password changed on the central, asked by the
central's admin, of that workspace's people only; after 7 days (a fake clock) the machine holds none of their
secrets; the workspace's access log the customer reads joins both sides."""

import datetime
import json
import re
from pathlib import Path

import httpx
import pytest

from omniacli import placement, totp
from omniacli.auth import verify_password

from .conftest import PASSWORD
from .test_remote_home import ACME_TOKEN, ADMIN, ORIGIN, PEOPLE, SECRET, basic, code_now, home, switch  # noqa: F401

NEW_PASSWORD = "a-new-password-of-elena"
# where a one-time link of a second step opens (identitypass.LINK_PATH)
LINK_PATH = "/second-factor/"


async def passed(c) -> httpx.Response:
    assert (await switch(c, True)).status_code == 200
    resp = await c.post("/api/identities/lab4/pass", headers=ADMIN, json={"confirm": "lab4"})
    assert resp.status_code == 200, resp.text
    return resp


def central_home(c) -> dict:
    return json.loads((c.m.jobs_dir.parent / placement.CENTRAL_HOME).read_text())


def browser_of(c) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=c.app), base_url=ORIGIN)


async def login(c, name: str, password: str = PASSWORD) -> tuple[httpx.AsyncClient, httpx.Response]:
    browser = browser_of(c)
    resp = await browser.post("/login", data={"username": name, "password": password, "next": "/"},
                              headers={"Origin": ORIGIN})
    return browser, resp


def machine_logins(c, since: int) -> list[str]:
    return [p for _, p, _ in c.machine.asked[since:] if p.endswith("/api/identity/v1/login")]


# --- the freeze and the pass, on the machine ----------------------------------------------------------------------

async def test_the_pass_freezes_the_identities_and_switches_the_login_off_on_the_machine(home):
    await passed(home)
    record = central_home(home)
    assert {w: e["pass"]["state"] for w, e in record.items()} == {"acme": "passed", "beta": "passed"}
    kept = json.dumps(record)
    # what the machine keeps of them: names, roles, tokens' levels; never a hash or a secret
    assert "pbkdf2" not in kept and SECRET not in kept and "sha256$" not in kept
    assert set(record["acme"]["pass"]["snapshot"]["workspaces"]["acme"]["members"]) == set(PEOPLE)
    # their login on the machine: refused after the right password, the machine's staff not
    form = await home.on_machine.post("/login", data={"username": "elena", "password": PASSWORD},
                                      headers={"Origin": "http://lab4.test"})
    assert form.status_code == 403 and "Your access moved to" in form.text
    assert (await home.on_machine.get("/", headers=basic("staff"))).status_code == 200
    # a workspace's token on the machine: no more (REST and MCP go through the central)
    token = await home.on_machine.get("/api/v1/instances", headers={"Authorization": f"Bearer {ACME_TOKEN}"})
    assert token.status_code == 401
    # the central's own login through the link: refused (the central checks it itself now)
    from .test_remote_home import CENTRAL_LINK_TOKEN
    through = await home.on_machine.post("/api/identity/v1/login", json={"person": "elena", "password": PASSWORD},
                                         headers={"Authorization": f"Bearer {CENTRAL_LINK_TOKEN}"})
    assert through.status_code == 200 and through.json() == {"ok": False, "passed": True}
    # nor through MCP; and their copy is read by nobody, the central's link included
    mcp = await home.on_machine.post("/mcp", headers={"Authorization": f"Bearer {ACME_TOKEN}",
                                                      "Accept": "application/json, text/event-stream"}, json={})
    assert mcp.status_code == 401
    package = await home.on_machine.get("/api/identity/v1/export", headers={"Authorization": f"Bearer {CENTRAL_LINK_TOKEN}"})
    assert package.status_code == 200 and package.json()["workspaces"] == {} and package.json()["people"] == {}
    # the identity changes of the workspace refused with where it passes; the instances' actions are not
    refused = await home.on_machine.post("/api/workspaces/acme/members/save", headers=basic("staff"),
                                         json={"name": "elena", "role": "admin"})
    assert refused.status_code == 409 and "This workspace passes to" in refused.text
    for path, body in (("/api/access/users/delete", {"name": "elena"}),
                       ("/api/access/tokens/delete", {"name": "t-bruno"}),
                       ("/api/access/users/second-factor/reset", {"name": "anna"})):
        resp = await home.on_machine.post(path, headers=basic("staff"), json=body)
        assert resp.status_code == 409, (path, resp.text)
    assert (await home.on_machine.get("/api/servers", headers=basic("staff"))).status_code == 200


async def test_a_machine_with_a_person_of_a_workspace_that_does_not_pass_is_refused(home):
    assert (await switch(home, True)).status_code == 200
    # dario is of beta (served) and of gamma, which is not served: his login would go away with beta's
    import dataclasses
    settings = home.machine_app.state.settings
    settings.workspaces["gamma"] = dataclasses.replace(settings.workspaces["beta"], name="gamma", servers=[])
    resp = await home.post("/api/identities/lab4/pass", headers=ADMIN, json={"confirm": "lab4"})
    assert resp.status_code == 409 and "dario" in resp.json()["error"]
    # nothing passed nor stayed frozen
    assert all("pass" not in e for e in central_home(home).values())
    assert (await home.on_machine.post("/login", data={"username": "elena", "password": PASSWORD},
                                       headers={"Origin": "http://lab4.test"})).status_code == 303


# --- the central the single source of truth -------------------------------------------------------------------------

async def test_the_people_log_in_on_the_central_with_the_same_password_and_app_checked_there(home):
    await passed(home)
    asked = len(home.machine.asked)
    browser, first = await login(home, "anna")
    assert first.status_code == 303 and first.headers["location"] == "/login/code"
    code = code_now()
    ok = await browser.post("/login/code", data={"code": code}, headers={"Origin": ORIGIN})
    assert ok.status_code == 303
    # her requests still go to the machine with her scope, checked there against what it kept at the pass
    servers = await browser.get("/api/servers")
    assert servers.status_code == 200 and [r["name"] for r in servers.json()] == ["srv"]
    # the same code a second time: refused (struck out on the central)
    again, _ = await login(home, "anna")
    assert (await again.post("/login/code", data={"code": code}, headers={"Origin": ORIGIN})).status_code == 401
    elena, resp = await login(home, "elena")
    assert resp.status_code == 303 and (await elena.get("/api/servers")).status_code == 200
    _, wrong = await login(home, "elena", "nope-nope")
    assert wrong.status_code == 401
    # nothing of a login asked of the machine any more
    assert machine_logins(home, asked) == []
    # its import is not read again from the machine, nor switched off while passed
    assert (await home.post("/api/identities/lab4/import", headers=ADMIN, json={"confirm": "lab4"})).status_code == 409
    assert (await switch(home, False)).status_code == 409


async def test_the_account_on_the_central_is_changed_there_after_the_pass(home):
    await passed(home)
    elena, _ = await login(home, "elena")
    page = await elena.get("/account", headers={"Accept": "text/html"})
    assert page.status_code == 200 and 'id="pw-form"' in page.text or "/api/account/password" in page.text
    start = await elena.post("/api/account/second-factor/start", headers={"Origin": ORIGIN})
    assert start.status_code == 200
    secret = start.json()["secret"]
    confirm = await elena.post("/api/account/second-factor/confirm", headers={"Origin": ORIGIN},
                               json={"code": totp.totp(secret)})
    assert confirm.status_code == 200 and len(confirm.json()["recovery"]) == totp.RECOVERY_CODES
    # her next login asks the code of the app she switched on here
    browser, first = await login(home, "elena")
    assert first.headers["location"] == "/login/code"
    # the machine never heard of it
    assert not home.machine_app.state.second_factors.on("elena")


async def test_a_passkey_only_person_never_activates_a_factor_with_the_password_and_does_it_from_the_link(home):
    home.machine_app.state.passkeys.add("bruno", b"h" * 32, "lab4.example", b"cred", b"key-of-bruno", 1, "phone",
                                        [], False)
    await home.post("/api/identities/lab4/import", headers=ADMIN, json={"confirm": "lab4"})
    said = (await passed(home)).json()
    links = {x["person"]: x for x in said["links"]}
    assert set(links) == {"bruno"} and links["bruno"]["path"].startswith(LINK_PATH)
    # kept as hashes, in a file 600 the purge of the package takes away
    kept = home.central.jobs_dir.parent / "second-factor-links.json"
    assert oct(kept.stat().st_mode & 0o777) == "0o600" and links["bruno"]["path"][len(LINK_PATH):] not in kept.read_text()
    postrm = (Path(__file__).parent.parent / "packaging" / "debian" / "files" / "postrm").read_text()
    assert "/var/lib/omniacli/second-factor-links.json" in postrm
    # the password alone: refused with where to go, no session and no set-up offered
    browser, resp = await login(home, "bruno")
    assert resp.status_code == 403 and "link" in resp.text
    assert (await browser.get("/api/servers")).status_code == 401
    assert (await browser.get("/login/setup", follow_redirects=False)).status_code != 200
    # a new link replaces the one of before (only for a person of that machine); the status says the links pending
    # by name, never their tokens
    made = await home.post("/api/identities/lab4/factor-link", headers=ADMIN, json={"confirm": "lab4", "person": "bruno"})
    assert made.status_code == 200 and made.json()["made"]["path"].startswith(LINK_PATH)
    person = browser_of(home)
    assert (await person.get(links["bruno"]["path"])).status_code == 410
    links["bruno"] = made.json()["made"]
    nobody = await home.post("/api/identities/lab4/factor-link", headers=ADMIN, json={"confirm": "lab4", "person": "admin"})
    assert nobody.status_code == 404
    status = (await home.get("/api/identities/lab4", headers=ADMIN)).json()
    assert list(status["home"]["pass"]["links"]) == ["bruno"] and links["bruno"]["path"] not in json.dumps(status)
    # the link: its page, then the password and a code of the app
    page = await person.get(links["bruno"]["path"])
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    secret = re.search(r'id="secret">([A-Z2-7]+)<', page.text).group(1)
    wrong = await person.post(links["bruno"]["path"], data={"password": "nope-nope", "code": totp.totp(secret)},
                              headers={"Origin": ORIGIN})
    assert wrong.status_code == 401
    done = await person.post(links["bruno"]["path"], data={"password": PASSWORD, "code": totp.totp(secret)},
                             headers={"Origin": ORIGIN})
    assert done.status_code == 200 and 'id="recovery-codes"' in done.text
    # once only
    assert (await person.get(links["bruno"]["path"])).status_code == 410
    # now: the password, then the code of that app
    browser, first = await login(home, "bruno")
    assert first.headers["location"] == "/login/code"
    later = totp.totp(secret, datetime.datetime.now().timestamp() + 30)
    assert (await browser.post("/login/code", data={"code": later}, headers={"Origin": ORIGIN})).status_code == 303
    assert (await browser.get("/api/servers")).status_code == 200
    # his second step set up: no more link for him
    again = await home.post("/api/identities/lab4/factor-link", headers=ADMIN, json={"confirm": "lab4", "person": "bruno"})
    assert again.status_code == 409


# --- the return --------------------------------------------------------------------------------------------------

async def test_back_to_the_machine_brings_a_password_changed_on_the_central(home):
    await passed(home)
    elena, _ = await login(home, "elena")
    changed = await elena.post("/api/account/password", headers={"Origin": ORIGIN},
                               json={"current": PASSWORD, "password": NEW_PASSWORD})
    assert changed.status_code == 200
    # the machine still holds the old one, off
    users = home.machine_app.state.settings.users
    assert verify_password(PASSWORD, users["elena"]) and not verify_password(NEW_PASSWORD, users["elena"])
    status = (await home.get("/api/identities/lab4", headers=ADMIN)).json()
    assert status["home"]["pass"]["changed"] == ["elena"]
    back = await home.post("/api/identities/lab4/return", headers=ADMIN, json={"confirm": "lab4"})
    assert back.status_code == 200, back.text
    assert back.json()["returned"] == ["elena"]
    users = home.machine_app.state.settings.users
    assert verify_password(NEW_PASSWORD, users["elena"])
    # her login on the machine again, with the password she chose on the central; the central serves her no more
    form = await home.on_machine.post("/login", data={"username": "elena", "password": NEW_PASSWORD},
                                      headers={"Origin": "http://lab4.test"})
    assert form.status_code == 303
    assert not (home.m.jobs_dir.parent / placement.CENTRAL_HOME).exists()
    assert (await elena.get("/api/servers")).status_code == 401
    # and the workspaces' changes on the machine are open again
    saved = await home.on_machine.post("/api/workspaces/acme/members/save", headers=basic("staff"),
                                       json={"name": "elena", "role": "developer"})
    assert saved.status_code == 200, saved.text


async def test_the_return_carries_that_workspaces_people_only_and_comes_from_the_central_link_only(home):
    await passed(home)
    from .test_remote_home import CENTRAL_LINK_TOKEN
    link = {"Authorization": f"Bearer {CENTRAL_LINK_TOKEN}"}
    staff = await home.on_machine.post("/api/identity/v1/pass", headers=link, json={
        "op": "return", "workspaces": ["acme", "beta"], "passed": central_home(home)["acme"]["pass"]["passed"],
        "people": {"staff": {"hash": home.machine_app.state.settings.users["elena"]}}})
    assert staff.status_code == 400 and "staff" in staff.text
    # the machine's admin is no link: it never brings the people back by itself
    mine = await home.on_machine.post("/api/identity/v1/pass", headers=basic("staff"),
                                      json={"op": "return", "workspaces": ["acme"], "people": {}})
    assert mine.status_code == 403
    assert all(e["pass"]["state"] == "passed" for e in central_home(home).values())
    # on the central: an admin, the link's name typed
    no = await home.post("/api/identities/lab4/return", headers=ADMIN, json={"confirm": "nope"})
    assert no.status_code == 400


async def test_after_the_seven_days_the_machine_holds_no_secret_of_its_people(home):
    from .test_remote_home import BETA_TOKEN
    before = dict(home.machine_app.state.settings.users)
    await passed(home)
    lines = await home.machine_app.state.purge_passed(datetime.datetime.now() + datetime.timedelta(days=6))
    assert lines == []
    lines = await home.machine_app.state.purge_passed(datetime.datetime.now() + datetime.timedelta(days=8))
    assert len(lines) == 2 and all("deleted here" in line for line in lines)
    settings = home.machine_app.state.settings
    people = [*PEOPLE, "dario"]
    assert not set(people) & set(settings.users)
    assert not set(people) & set(settings.workspaces["acme"].members) and "staff" in settings.workspaces["acme"].members
    assert not {"t-bruno", "t-dario"} & set(settings.tokens)
    assert not home.machine_app.state.second_factors.on("anna")
    # no hash of theirs, no TOTP secret, no token's hash left in any file of the machine
    central = home.central.jobs_dir.parent.resolve()
    files = [p for p in home.m.jobs_dir.parent.rglob("*") if p.is_file() and p.suffix in (".json", ".yaml", ".yml")
             and central not in p.resolve().parents]
    assert any(p.name == "users.yaml" for p in files) or home.m.users_file is None or home.m.users_file.exists()
    files += [home.m.users_file] if home.m.users_file and home.m.users_file not in files else []
    texts = "".join(p.read_text(errors="replace") for p in files if p.exists())
    names = {p.name for p in files}
    assert {"second-factor.json", placement.CENTRAL_HOME} <= names, names
    for name in people:
        assert before[name] not in texts
    assert SECRET not in texts and BETA_TOKEN not in texts
    # the central still serves them, against what the machine kept of them at the pass
    elena, resp = await login(home, "elena")
    assert resp.status_code == 303 and (await elena.get("/api/servers")).status_code == 200
    # the way back after the 7 days is a handover
    back = await home.post("/api/identities/lab4/return", headers=ADMIN, json={"confirm": "lab4"})
    assert back.status_code == 409


# --- the access log of the workspace, both sides ------------------------------------------------------------------

async def test_the_workspace_access_log_joins_both_sides_without_the_machines_names(home):
    await passed(home)
    browser, _ = await login(home, "anna")
    await browser.post("/login/code", data={"code": code_now()}, headers={"Origin": ORIGIN})
    page = await browser.get("/workspaces/acme/audit", headers={"Accept": "text/html"})
    assert page.status_code == 200, page.text[:300]
    # the central's login of anna and the machine's lines of the pass, in one table
    assert "login form" in page.text and "passed to" in page.text
    assert "lab4" not in page.text
