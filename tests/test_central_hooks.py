# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""AD-018 slice 4, «I webhook della CI non passano dal centrale» (§18.5.1): when the central panel (lab3) starts
serving the people of a machine's (lab4) workspaces, a job of the machine rewrites the webhook OmniaCLI made on GitHub
(a fake GitHub here) from the portal's address onto the instance's own name: the same webhook (its id) with the same
secret, as switching the CI on again does; a push signed with that secret then deploys the test on the machine with
the central panel down. An instance with no public name goes onto the central panel's portal, which forwards the push
to the machine as it came (body and signature) and says nothing of a webhook it does not know; its page says its CI
depends on the central panel. Switched off, the webhook goes back. No secret in the pages, the answers or the logs."""

import asyncio
import json
from urllib.parse import urlsplit

import httpx
import pytest

from omniacli import config, github, jobs as jobs_module, ops
from omniacli.jobs import FINISHED

from .conftest import sh
from .test_github import REMOTE, TOKEN, FakeGitHub, _signed
from .test_remote_home import CENTRAL_LINK_TOKEN, basic, home, switch  # noqa: F401 - the home fixture

# the real submit: the home fixture runs no job's action, these tests need the machine's
REAL_SUBMIT = jobs_module.JobManager.submit
NAME = "https://srv-test.acme.example.com"


@pytest.fixture
def ci(home, monkeypatch):
    """lab4 with the test of srv following acme/addons on GitHub, a GitHub token on srv, its own portal
    (lab4.example.com), the CI of the test on and its webhook made on the portal's address; lab3 with a portal of its
    own (central.example.com). Its deploys are counted, not run."""
    monkeypatch.setattr(jobs_module.JobManager, "submit", REAL_SUBMIT)
    fake = FakeGitHub(repos=3)
    monkeypatch.setattr(github, "TRANSPORT", httpx.MockTransport(fake))
    pulled = []

    async def pull_restart(job, inst, only_if_changed=False, only=None, backup_folder=None):
        pulled.append(inst.key)
    monkeypatch.setattr(ops, "pull_restart", pull_restart)
    m = home.m
    for role in ("production", "test"):
        repo = m.instance("srv", role).repository("addons")
        sh("git", "remote", "set-url", repo.remote, REMOTE, cwd=repo.path)
    m.options = config.parse_options({"portal": {"address": "lab4.example.com"}})
    home.central.options = config.parse_options({"portal": {"address": "central.example.com"}})
    return {"fake": fake, "pulled": pulled}


async def machine_job(home, job_id: str) -> jobs_module.Job:
    for _ in range(400):
        job = home.machine_app.state.jobs.get(job_id)
        if job is not None and job.status in FINISHED:
            return job
        await asyncio.sleep(0.05)
    raise AssertionError(f"the job {job_id} of the machine did not finish")


async def ci_on(home) -> dict:
    """The token saved on srv, the test's CI switched on: the webhook made; {id, url, secret}."""
    resp = await home.on_machine.post("/api/credentials/github/save", headers=basic("staff"),
                                      json={"kind": "server", "name": "srv", "label": "Acme", "token": TOKEN})
    assert resp.status_code == 200, resp.text
    resp = await home.on_machine.post("/api/servers/srv/test/deploy-hook", headers=basic("staff"),
                                      json={"enabled": True})
    assert resp.status_code == 200, resp.text
    job = await machine_job(home, resp.json()["github_job"])
    assert job.status == "success", job.lines
    return hook_of(home)


def hook_of(home) -> dict:
    (hook,) = home.ci["fake"].hooks["acme/addons"].values()
    return {"id": hook["id"], "url": hook["config"]["url"], "secret": hook["config"]["secret"]}


def push(secret: str, sha: str) -> tuple[bytes, dict]:
    return _signed(secret, {"ref": "refs/heads/test", "after": sha, "repository": {"clone_url": REMOTE},
                            "head_commit": {"id": sha, "message": "On test"}})


def no_secret(home, secret: str, *texts: str) -> None:
    for app in (home.app, home.machine_app):
        assert all(secret not in json.dumps(e) and TOKEN not in json.dumps(e) for e in app.state.access_log.recent())
        for job in app.state.jobs.jobs.values():
            assert all(secret not in line and TOKEN not in line for line in job.lines)
    for text in texts:
        assert secret not in text and TOKEN not in text


@pytest.fixture
def lab(home, ci):
    home.ci = ci
    return home


async def test_the_webhook_moves_onto_the_instances_name_and_back(lab):
    home = lab
    home.m.instance("srv", "test").site = NAME
    before = await ci_on(home)
    # today: on the portal of the machine
    assert before["url"].startswith("https://lab4.example.com/hooks/github/")
    rid = before["url"].rsplit("/", 1)[1]
    resp = await switch(home, True)
    assert resp.status_code == 200, resp.text
    job = await machine_job(home, resp.json()["hooks_job"])
    assert job.status == "success", job.lines
    after = hook_of(home)
    # the same webhook on GitHub (its id), the same secret, on the instance's own name
    assert after == {"id": before["id"], "url": f"{NAME}/.omniacli/hooks/github/{rid}", "secret": before["secret"]}
    assert any("moved from" in line and str(before["id"]) in line for line in job.lines), job.lines
    # made once (CI on), never a second one
    assert sum(1 for c in home.ci["fake"].calls if c[0] == "POST" and c[1].endswith("/hooks")) == 1
    # a normal job of the machine, in its access log
    assert any(e.get("kind") == "job" and "GitHub webhooks of acme" in e.get("detail", "")
               for e in home.machine_app.state.access_log.recent()), "the job is not in the access log"
    # the central panel down: a push told on the instance's name deploys the test on the machine
    home.machine.down = True
    raw, headers = push(after["secret"], "e" * 40)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=home.machine_app),
                                 base_url="https://srv-test.acme.example.com") as gh:
        resp = await gh.post(urlsplit(after["url"]).path, content=raw, headers=headers)
    assert resp.status_code == 202, resp.text
    assert (await machine_job(home, resp.json()["job"])).status == "success"
    assert home.ci["pulled"] == ["srv/test"]
    home.machine.down = False
    # the page of the instance: made on its name, nothing through the central panel
    view = (await home.on_machine.get("/api/servers/srv/test/deploy-hook", headers=basic("staff"))).json()
    assert view["github"][0]["url"] == after["url"] and view["github"][0]["central"] is False
    # switched off: back on the machine's portal, the same webhook and secret
    resp = await switch(home, False)
    assert resp.status_code == 200, resp.text
    job = await machine_job(home, resp.json()["hooks_job"])
    assert job.status == "success", job.lines
    assert hook_of(home) == before
    no_secret(home, before["secret"], resp.text, json.dumps(view))


async def test_an_instance_with_no_name_is_forwarded_by_the_central_panel(lab):
    home = lab
    before = await ci_on(home)
    rid = before["url"].rsplit("/", 1)[1]
    resp = await switch(home, True)
    job = await machine_job(home, resp.json()["hooks_job"])
    assert job.status == "success", job.lines
    after = hook_of(home)
    assert after == {**before, "url": f"https://central.example.com/hooks/github/{rid}"}
    assert any("depends on the central panel" in line for line in job.lines), job.lines
    # its page says its CI depends on the central panel
    view = (await home.on_machine.get("/api/servers/srv/test/deploy-hook", headers=basic("staff"))).json()
    assert view["github"][0]["central"] is True
    # (the page draws that row from this answer: a warning line)
    page = (await home.on_machine.get("/servers/srv?view=test", headers=basic("staff"))).text
    assert page.count("ghCentral") == 2
    # GitHub calls the central panel: the push goes to the machine as it came, body and signature
    raw, headers = push(after["secret"], "f" * 40)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=home.app), base_url="https://central.example.com") as gh:
        resp = await gh.post(f"/hooks/github/{rid}", content=raw, headers={**headers, "Cookie": "x=1"})
        assert resp.status_code == 202, resp.text
        assert (await machine_job(home, resp.json()["job"])).status == "success"
        assert home.ci["pulled"] == ["srv/test"]
        posted = [a for a in home.machine.asked if a[0] == "POST" and a[1] == f"/hooks/github/{rid}"]
        assert len(posted) == 1
        assert posted[0][2]["x-hub-signature-256"] == headers["X-Hub-Signature-256"]
        assert "cookie" not in posted[0][2] and "authorization" not in posted[0][2]
        # a wrong signature: the machine refuses it, and so does the central panel
        bad, bad_headers = push("not-the-secret", "f" * 40)
        assert (await gh.post(f"/hooks/github/{rid}", content=bad, headers=bad_headers)).status_code == 403
        # a webhook of no machine it serves: refused here, nothing posted to the machine
        asked = len(home.machine.asked)
        assert (await gh.post("/hooks/github/nobodys0hook", content=raw, headers=headers)).status_code == 403
        assert not any(a[0] == "POST" for a in home.machine.asked[asked:])
        # the machine down: said, not a deploy
        home.machine.down = True
        assert (await gh.post(f"/hooks/github/{rid}", content=raw, headers=headers)).status_code == 502
        home.machine.down = False
    assert any("forwarded to lab4" in e.get("detail", "") for e in home.app.state.access_log.recent())
    # switched off: back on the machine's portal, and the central panel forwards it no more
    resp = await switch(home, False)
    assert (await machine_job(home, resp.json()["hooks_job"])).status == "success"
    assert hook_of(home) == before
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=home.app), base_url="https://central.example.com") as gh:
        assert (await gh.post(f"/hooks/github/{rid}", content=raw, headers=headers)).status_code == 403
    no_secret(home, before["secret"], page, json.dumps(view))


async def test_the_forwarded_hooks_are_said_to_the_central_panels_link_only(lab):
    home = lab
    await ci_on(home)
    await machine_job(home, (await switch(home, True)).json()["hooks_job"])
    # a person of the machine, even its admin, is refused the list
    assert (await home.on_machine.get("/api/identity/v1/home", headers=basic("staff"))).status_code == 403
    # a portal that is no address is refused
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=home.machine_app), base_url="http://lab4.test") as m:
        link = {"Authorization": f"Bearer {CENTRAL_LINK_TOKEN}", "Accept": "application/json"}
        found = (await m.get("/api/identity/v1/home", headers=link)).json()
        assert len(found["hooks"]) == 1 and "secret" not in json.dumps(found)
        for bad in ("http://central.example.com", "https://central.example.com/x", "javascript:x", 7):
            resp = await m.post("/api/identity/v1/home", headers=link,
                                json={"workspaces": ["acme"], "on": True, "portal": bad})
            assert resp.status_code == 400, bad
