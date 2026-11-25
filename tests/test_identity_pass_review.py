# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""AD-018 slice 5, the review of fb3867d: a pass that fails takes back its freeze only (the home of slice 2 stays);
two passes at once never leave the central passed and the machine not; the return is refused after the 7 days, on
both sides, and never runs with the purge; a one-time link never replaces a second step a person has; the joined
access log of a workspace holds this machine's entries only; the single sign-on of a workspace passing makes nobody."""

import asyncio
import datetime
import json
import re

import httpx

from omniacli import placement, totp

from .conftest import PASSWORD
from .test_identity_pass import browser_of, login, passed
from .test_remote_home import ADMIN, ORIGIN, code_now, home, switch  # noqa: F401
from .test_sso import browser as sso_browser, configure, idp, portal_app, sign_in  # noqa: F401
from .test_workspaces import workspaces  # noqa: F401


def machine_home(c) -> dict | None:
    path = c.m.jobs_dir.parent / placement.CENTRAL_HOME
    return json.loads(path.read_text()) if path.exists() else None


async def test_a_pass_that_fails_takes_back_its_freeze_and_keeps_the_home(home):
    assert (await switch(home, True)).status_code == 200
    _, ok = await login(home, "elena")
    assert ok.status_code == 303
    # the import of the pass fails (the machine's export answers 500)
    home.machine.answer["/api/identity/v1/export"] = lambda r: httpx.Response(500, json={"detail": "boom"})
    resp = await home.post("/api/identities/lab4/pass", headers=ADMIN, json={"confirm": "lab4"})
    assert resp.status_code == 502
    del home.machine.answer["/api/identity/v1/export"]
    status = (await home.get("/api/identities/lab4", headers=ADMIN)).json()
    record = machine_home(home)
    # the home of slice 2 as it was, with no pass; elena still served through the central
    assert status["home"]["on"] is True and status["home"]["pass"] is None
    assert record is not None and {w: e["link"] for w, e in record.items()} == {"acme": "central", "beta": "central"}
    assert all("pass" not in e for e in record.values())
    _, after = await login(home, "elena")
    assert after.status_code == 303
    # the pass works afterwards
    assert (await home.post("/api/identities/lab4/pass", headers=ADMIN, json={"confirm": "lab4"})).status_code == 200


async def test_two_passes_at_once_never_split_the_central_and_the_machine(home):
    assert (await switch(home, True)).status_code == 200
    a, b = await asyncio.gather(
        home.post("/api/identities/lab4/pass", headers=ADMIN, json={"confirm": "lab4"}),
        home.post("/api/identities/lab4/pass", headers=ADMIN, json={"confirm": "lab4"}))
    assert sorted([a.status_code, b.status_code]) == [200, 409]
    status = (await home.get("/api/identities/lab4", headers=ADMIN)).json()
    assert status["home"]["pass"] is not None
    assert all(e["pass"]["state"] == "passed" for e in machine_home(home).values())
    form = await home.on_machine.post("/login", data={"username": "elena", "password": PASSWORD},
                                      headers={"Origin": "http://lab4.test"})
    assert form.status_code == 403
    # «Back to the machine» still works
    assert (await home.post("/api/identities/lab4/return", headers=ADMIN, json={"confirm": "lab4"})).status_code == 200


async def test_the_machine_refuses_a_second_freeze_and_a_return_of_another_pass(home):
    from .test_remote_home import CENTRAL_LINK_TOKEN
    await passed(home)
    link = {"Authorization": f"Bearer {CENTRAL_LINK_TOKEN}"}
    for body in ({"op": "freeze", "workspaces": ["acme", "beta"]}, {"op": "thaw", "workspaces": ["acme", "beta"]},
                 {"op": "return", "workspaces": ["acme", "beta"], "people": {}},
                 {"op": "return", "workspaces": ["acme", "beta"], "people": {}, "passed": "2020-01-01T00:00:00"}):
        resp = await home.on_machine.post("/api/identity/v1/pass", headers=link, json=body)
        assert resp.status_code == 409, (body, resp.text)
    assert all(e["pass"]["state"] == "passed" for e in machine_home(home).values())


async def test_the_return_is_refused_after_the_seven_days_on_both_sides(home):
    await passed(home)
    # the machine's 7 days over (its clock), not yet purged: refused there
    path = home.m.jobs_dir.parent / placement.CENTRAL_HOME
    record = json.loads(path.read_text())
    for e in record.values():
        e["pass"]["until"] = (datetime.datetime.now() - datetime.timedelta(days=1)).isoformat(timespec="seconds")
    path.write_text(json.dumps(record))
    back = await home.post("/api/identities/lab4/return", headers=ADMIN, json={"confirm": "lab4"})
    assert back.status_code == 409
    # the central's 7 days over: refused there, the machine never asked
    store = home.app.state.identity_store
    info = store.passed("lab4")
    store.set_passed("lab4", {**info, "until": (datetime.datetime.now() - datetime.timedelta(days=1))
                              .isoformat(timespec="seconds")})
    asked = len(home.machine.asked)
    back = await home.post("/api/identities/lab4/return", headers=ADMIN, json={"confirm": "lab4"})
    assert back.status_code == 409 and "7 days" in back.json()["error"]
    assert not [p for _, p, _ in home.machine.asked[asked:] if p.endswith("/pass")]


async def test_the_purge_and_a_return_never_run_together(home):
    await passed(home)
    purge = home.machine_app.state.purge_passed(datetime.datetime.now() + datetime.timedelta(days=8))
    back = home.post("/api/identities/lab4/return", headers=ADMIN, json={"confirm": "lab4"})
    lines, resp = await asyncio.gather(purge, back)
    record = machine_home(home)
    users = home.machine_app.state.settings.users
    if resp.status_code == 200:
        # the return first: nothing purged, the people back with their login
        assert lines == [] and record is None and "elena" in users
    else:
        # the purge first: the return refused, the pass and the central's record as they were
        assert resp.status_code == 409 and lines and "elena" not in users
        assert all(e["pass"]["state"] == "passed" and e["pass"].get("purged") for e in record.values())
        assert (await home.get("/api/identities/lab4", headers=ADMIN)).json()["home"]["pass"] is not None


async def test_a_one_time_link_never_replaces_a_second_step(home):
    await passed(home)
    # anna has her app: no link for her
    made = await home.post("/api/identities/lab4/factor-link", headers=ADMIN, json={"confirm": "lab4", "person": "anna"})
    assert made.status_code == 409
    # bruno has none (the portal asks none here: he needs no link either)
    made = await home.post("/api/identities/lab4/factor-link", headers=ADMIN, json={"confirm": "lab4", "person": "bruno"})
    assert made.status_code == 409


async def test_a_link_made_before_a_second_step_is_set_up_is_refused_when_used(home):
    home.machine_app.state.passkeys.add("bruno", b"h" * 32, "lab4.example", b"cred", b"key-of-bruno", 1, "phone",
                                        [], False)
    await home.post("/api/identities/lab4/import", headers=ADMIN, json={"confirm": "lab4"})
    links = {x["person"]: x for x in (await passed(home)).json()["links"]}
    # bruno sets up an app on the central another way first (here: as his Account would, on the record)
    store = home.app.state.identity_store
    store.mutate("lab4", "bruno", lambda p: p.__setitem__("second_factor", {
        "secret": totp.new_secret(), "since": 0, "last": -1, "salt": "", "recovery": []}))
    person = browser_of(home)
    page = await person.get(links["bruno"]["path"])
    secret = re.search(r'id="secret">([A-Z2-7]+)<', page.text)
    code = totp.totp(secret.group(1)) if secret else "000000"
    done = await person.post(links["bruno"]["path"], data={"password": PASSWORD, "code": code},
                             headers={"Origin": ORIGIN})
    assert done.status_code == 410 and "recovery-codes" not in done.text


async def test_the_joined_log_holds_this_machines_entries_only(home):
    await passed(home)
    home.app.state.access_log.record("login", "user", "zed-of-another-customer", "203.0.113.77",
                                     "login form, workspaces of other-machine", via="other-machine",
                                     workspaces=["acme"])
    browser, _ = await login(home, "anna")
    await browser.post("/login/code", data={"code": code_now()}, headers={"Origin": ORIGIN})
    page = await browser.get("/workspaces/acme/audit", headers={"Accept": "text/html"})
    assert page.status_code == 200
    assert "zed-of-another-customer" not in page.text and "203.0.113.77" not in page.text
    assert "other-machine" not in page.text and "lab4" not in page.text
    assert "login form" in page.text


async def test_the_single_sign_on_of_a_workspace_passing_makes_nobody(env, workspaces, idp):
    app = portal_app(env["settings"])
    assert (await configure(app)).status_code == 200
    # acme frozen: it passes to a central panel
    (env["settings"].jobs_dir.parent / placement.CENTRAL_HOME).write_text(json.dumps(
        {"acme": {"link": "central", "at": "", "by": "", "pass": {"state": "frozen", "portal": ""}}}))
    async with sso_browser(app) as c:
        resp, _ = await sign_in(app, idp, "newbie@acme.example", c)
        assert resp.status_code == 403 and "passes to" in resp.text
    assert "newbie@acme.example" not in env["settings"].users
    assert "newbie@acme.example" not in env["settings"].workspaces["acme"].members
