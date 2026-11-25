# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""AD-018 slice 5, the review of a60ccb2: a machine's answer lost after it made a step. Each pass has an id that every
step carries: a lost answer of the pass is settled from the machine's state (passed with this pass's id: the pass ends
on the central too), a lost answer of the freeze takes back that freeze only, a retried pass takes over a freeze of its
link that did not end, and the Identities page says when the machine and the central disagree and reconciles them."""

import json

import httpx

from omniacli import placement

from .conftest import PASSWORD
from .test_identity_pass import login
from .test_remote_home import ADMIN, basic, home, switch  # noqa: F401


def lose_answer_of(c, op: str):
    """The machine makes the step, its answer never reaches the central (a dropped connection, a timeout)."""
    async def hook(request):
        body = json.loads(request.content or b"{}")
        resp = await c.machine.asgi.handle_async_request(request)
        if body.get("op") == op:
            await resp.aread()
            raise httpx.ReadTimeout("the answer was lost", request=request)
        return resp
    c.machine.answer["/api/identity/v1/pass"] = hook


def machine_states(c) -> dict:
    path = c.m.jobs_dir.parent / placement.CENTRAL_HOME
    rec = json.loads(path.read_text()) if path.exists() else {}
    return {w: (e.get("pass") or {}).get("state", "") for w, e in rec.items()}


async def test_a_lost_answer_of_the_pass_ends_the_pass_on_the_central_too(home):
    assert (await switch(home, True)).status_code == 200
    lose_answer_of(home, "pass")
    resp = await home.post("/api/identities/lab4/pass", headers=ADMIN, json={"confirm": "lab4"})
    del home.machine.answer["/api/identity/v1/pass"]
    assert resp.status_code == 200, resp.text
    status = (await home.get("/api/identities/lab4", headers=ADMIN)).json()
    assert status["home"]["pass"] is not None and machine_states(home) == {"acme": "passed", "beta": "passed"}
    assert status["reconcile"]["state"] == "same"
    _, central = await login(home, "elena")
    assert central.status_code == 303
    machine = await home.on_machine.post("/login", data={"username": "elena", "password": PASSWORD},
                                         headers={"Origin": "http://lab4.test"})
    assert machine.status_code == 403
    assert (await home.post("/api/identities/lab4/return", headers=ADMIN, json={"confirm": "lab4"})).status_code == 200


async def test_a_lost_answer_of_the_freeze_takes_the_freeze_back(home):
    assert (await switch(home, True)).status_code == 200
    lose_answer_of(home, "freeze")
    resp = await home.post("/api/identities/lab4/pass", headers=ADMIN, json={"confirm": "lab4"})
    del home.machine.answer["/api/identity/v1/pass"]
    assert resp.status_code == 502
    assert machine_states(home) == {"acme": "", "beta": ""}
    member = await home.on_machine.post("/api/workspaces/acme/members/save", headers=basic("staff"),
                                        json={"name": "elena", "role": "developer"})
    assert member.status_code == 200, member.text
    assert (await home.post("/api/identities/lab4/pass", headers=ADMIN, json={"confirm": "lab4"})).status_code == 200


async def test_a_freeze_left_behind_is_said_and_reconciled_or_taken_over(home):
    from .test_remote_home import CENTRAL_LINK_TOKEN
    assert (await switch(home, True)).status_code == 200
    link = {"Authorization": f"Bearer {CENTRAL_LINK_TOKEN}"}
    # a freeze of a pass of this link that did not end (the central restarted in between)
    left = await home.on_machine.post("/api/identity/v1/pass", headers=link, json={
        "op": "freeze", "workspaces": ["acme", "beta"], "id": "left-behind-0001"})
    assert left.status_code == 200
    status = (await home.get("/api/identities/lab4", headers=ADMIN)).json()
    assert status["reconcile"] == {"action": "thaw", "state": "frozen_there", "workspaces": ["acme", "beta"]}
    page = await home.get("/identities/lab4", headers={**ADMIN, "Accept": "text/html"})
    assert 'id="idn-reconcile"' in page.text and 'data-reconcile="frozen_there"' in page.text
    done = await home.post("/api/identities/lab4/reconcile", headers=ADMIN, json={"confirm": "lab4"})
    assert done.status_code == 200 and done.json()["done"] == "thawed"
    assert machine_states(home) == {"acme": "", "beta": ""}
    # left behind again: a new pass of the same link takes the freeze over
    assert (await home.on_machine.post("/api/identity/v1/pass", headers=link, json={
        "op": "freeze", "workspaces": ["acme", "beta"], "id": "left-behind-0002"})).status_code == 200
    assert (await home.post("/api/identities/lab4/pass", headers=ADMIN, json={"confirm": "lab4"})).status_code == 200
    assert machine_states(home) == {"acme": "passed", "beta": "passed"}


async def test_a_pass_whose_answers_were_all_lost_is_reconciled_from_the_page(home):
    assert (await switch(home, True)).status_code == 200
    # the pass made there, its answer and the read of the state after it both lost (the machine slow, then back)
    lose_answer_of(home, "pass")
    real = home.machine.answer.get("/api/identity/v1/home")
    home.machine.answer["/api/identity/v1/home"] = lambda r: httpx.Response(503)
    resp = await home.post("/api/identities/lab4/pass", headers=ADMIN, json={"confirm": "lab4"})
    del home.machine.answer["/api/identity/v1/pass"]
    if real is None:
        del home.machine.answer["/api/identity/v1/home"]
    assert resp.status_code == 502
    assert machine_states(home) == {"acme": "passed", "beta": "passed"}
    status = (await home.get("/api/identities/lab4", headers=ADMIN)).json()
    assert status["home"]["pass"] is None and status["reconcile"]["action"] == "adopt"
    done = await home.post("/api/identities/lab4/reconcile", headers=ADMIN, json={"confirm": "lab4"})
    assert done.status_code == 200 and done.json()["done"] == "adopted"
    _, central = await login(home, "elena")
    assert central.status_code == 303
    assert (await home.post("/api/identities/lab4/return", headers=ADMIN, json={"confirm": "lab4"})).status_code == 200
    assert not (home.m.jobs_dir.parent / placement.CENTRAL_HOME).exists()


async def test_a_pass_the_central_did_not_make_is_never_adopted(home):
    from .test_remote_home import CENTRAL_LINK_TOKEN
    assert (await switch(home, True)).status_code == 200
    link = {"Authorization": f"Bearer {CENTRAL_LINK_TOKEN}"}
    for op in ("freeze", "pass"):
        assert (await home.on_machine.post("/api/identity/v1/pass", headers=link, json={
            "op": op, "workspaces": ["acme", "beta"], "id": "not-this-panels-1"})).status_code == 200
    status = (await home.get("/api/identities/lab4", headers=ADMIN)).json()
    assert status["reconcile"]["state"] == "mismatch" and status["reconcile"]["action"] == ""
    refused = await home.post("/api/identities/lab4/reconcile", headers=ADMIN, json={"confirm": "lab4"})
    assert refused.status_code == 409 and status["home"]["pass"] is None
