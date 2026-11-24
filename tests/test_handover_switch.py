# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The switch of a handover (AD-011, step 8 of §9 of docs/adr/AD-008-consegna-e-spostamento.md): two phases, the
provider coordinating; the sealed secrets (AD-009); the import of the users with their hashes and two-step
secrets (a name the customer has already arrives as <name>.<workspace>), the roles, the schedules, the
destinations, the credentials and the extract of the access log; a commit whose answer is lost asked again; the
deadline passed without a commit, the instances running at the provider's again and a late commit refused."""

import asyncio
import dataclasses
import json
import time

import httpx
import pytest

from omniacli import (accesslog, deploys, destinations, gitcredentials, handover, ops, schedules, totp, workspaceops,
                      zerodowntime)
from omniacli.customerview import CustomerView
from omniacli.jobs import SUCCESS

from .conftest import BasicAsSession
from .test_app import AUTH, HOST
from .test_handover import Root, customer, prepare, provider, provider_trees, receive  # noqa: F401 - fixtures
from .test_staging_dialog import acme, two  # noqa: F401 - the fixtures
from .test_workspaces import PASSWORD, workspaces  # noqa: F401 - the workspaces fixture

FAR = {"Authorization": AUTH, "Origin": "http://far.test"}


async def wait_job(client, job_id, seconds=60):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        data = (await client.get(f"/api/jobs/{job_id}", headers={"Authorization": AUTH})).json()
        if data["status"] in ("success", "failed", "interrupted", "stopped"):
            return data
        await asyncio.sleep(0.05)
    raise AssertionError("job did not finish")


# --- the parts -----------------------------------------------------------------------------------------------

def test_the_secrets_open_with_the_claims_key_and_for_their_switch_only():
    raw, public = handover.new_key()
    aad = handover.sealed_aad("ab" * 8, "cd" * 8)
    sealed = handover.seal(b'{"users": {}}', public, aad)
    assert b"users" not in json.dumps(sealed).encode()
    assert handover.unseal(raw, sealed, aad) == b'{"users": {}}'
    other, _public = handover.new_key()
    for key, data in ((other, aad), (raw, handover.sealed_aad("ab" * 8, "ef" * 8))):
        with pytest.raises(handover.CodeError):
            handover.unseal(key, sealed, data)
    changed = {**sealed, "data": sealed["data"][:-2] + ("AA" if not sealed["data"].endswith("AA") else "BB")}
    with pytest.raises(handover.CodeError):
        handover.unseal(raw, changed, aad)


def test_the_names_that_arrive_and_the_hour_of_a_schedule():
    assert handover.names_map(["anna", "mario", "luca"], {"mario", "admin"}, "acme") == {
        "anna": "anna", "mario": "mario.acme", "luca": "luca"}
    assert handover.names_map(["mario"], {"mario", "mario.acme"}, "acme") == {"mario": "mario.acme-2"}
    # a destination's name has no dot: one taken arrives as <name>-<workspace>, within its 32 characters
    assert handover.destination_names_map(["s3", "sftp"], {"sftp", "sftp-acme"}, "acme") == {
        "s3": "s3", "sftp": "sftp-acme-2"}
    long = handover.destination_names_map(["a" * 30], {"a" * 30}, "acme")["a" * 30]
    assert len(long) <= 32 and destinations.NAME.fullmatch(long)
    assert handover.server_renamed("acme-odoo", "acme", "acme-2") == "acme-2-odoo"
    assert handover.cron_shifted("0 3 * * *", -1) == "0 2 * * *"
    assert handover.cron_shifted("0 0 * * *", -1) == "0 23 * * *"
    assert handover.cron_shifted("0 */2 * * *", 3) == "0 */2 * * *"
    # a switch closes by its own deadline, past the code's 72 hours
    assert handover.phase_of({"phase": "switching", "expires": 1}) == "switching"
    assert handover.phase_of({"phase": "committed", "expires": 1}) == "committed"
    assert handover.deadline_of(10, at=1000) == 1000 + handover.SWITCH_MOST
    assert handover.deadline_of(400, at=1000) == 2200


def test_the_extract_of_the_access_log_is_the_workspaces_redacted_with_the_hash_of_each_line(tmp_path):
    log = accesslog.AccessLog(tmp_path / "access.jsonl")
    log.record("login", "user", "mario", "203.0.113.7", "login form", context=False, workspace="acme")
    log.record("action", "user", "carlo", "10.0.0.5", "restarted acme-odoo on lab3", context=False, workspace="acme")
    log.record("login", "user", "eva", "203.0.113.9", "login form", context=False, workspace="beta")
    view = CustomerView("Studio Rossi", "another machine", machines=frozenset({"lab3"}), members=frozenset({"mario"}))
    text = handover.log_extract(accesslog.numbered(log.path), "acme", view, log.verify())
    head, *rows = [json.loads(line) for line in text.splitlines()]
    assert head["entries"] == 2 and head["verify"]["ok"] is True and head["provider"] == "Studio Rossi"
    assert rows[0]["name"] == "mario" and rows[0]["ip"] == "203.0.113.7"
    # the provider's people as the provider, their address not passed; other machines as «another machine»
    assert rows[1]["name"] == "Studio Rossi" and rows[1]["kind"] == "provider" and "ip" not in rows[1]
    assert "lab3" not in rows[1]["detail"] and "another machine" in rows[1]["detail"]
    lines = (tmp_path / "access.jsonl").read_text().splitlines()
    assert {r["orig"] for r in rows} == {accesslog.line_hash(lines[0]), accesslog.line_hash(lines[1])}


def test_a_second_factor_passes_with_its_last_step(tmp_path):
    there = totp.SecondFactors(tmp_path / "a.json")
    secret = totp.new_secret()
    codes = there.enable("luca", secret, 100)
    here = totp.SecondFactors(tmp_path / "b.json")
    here.adopt("luca.acme", there.record("luca"))
    assert here.record("luca.acme")["secret"] == secret and here.record("luca.acme")["last"] == 100
    # the recovery codes are the same ones
    assert here.check("luca.acme", codes[0]) is not None


# --- the switch, end to end (the provider's app, the customer's, root's helpers and the compose commands faked) ---

class Commands:
    """The commands of compose-helper (stop, start, crons) both machines run in a switch, faked; any other
    goes to the real ops. failing: a project whose start does not answer /web/login."""

    def __init__(self, monkeypatch):
        self.ran, self.failing = [], set()
        self.run_original, self.capture_original = ops.run, ops.capture
        monkeypatch.setattr(ops, "run", self.run)
        monkeypatch.setattr(ops, "capture", self.capture)
        monkeypatch.setattr(zerodowntime, "answers", self.answers)
        monkeypatch.setattr(zerodowntime, "site", lambda inst: None)
        monkeypatch.setattr(zerodowntime, "ask", self.ask)
        self.started = set()

    async def run(self, job, cmd, *args, **kwargs):
        if "compose-helper" in cmd:
            project, action = cmd[cmd.index("compose-helper") + 1], cmd[cmd.index("compose-helper") + 2:]
            self.ran.append((project, *action))
            if action[:1] == ["start"]:
                self.started.add(project)
            if action[:1] in (["stop"], ["stop-service"]):
                self.started.discard(project)
            return ""
        return await self.run_original(job, cmd, *args, **kwargs)

    async def capture(self, cmd, *args, **kwargs):
        # an instance's own command (its restart's: the tests' shell that records it) switching its crons
        if "crons" in cmd and cmd[-1] in ("off", "5,6"):
            self.ran.append(("crons", *cmd[cmd.index("crons") + 1:]))
            return 0, "5,6\n" if cmd[-1] == "off" else "2\n"
        return await self.capture_original(cmd, *args, **kwargs)

    async def answers(self, port, most=None):
        return not any(str(port) in p for p in self.failing)

    async def ask(self, url):
        return True


class Faked(Root):
    """Root, with the helpers of the switch: the source's database stopped clean and started again, the copy
    waiting for its last record and promoted, emptied again; the forward of the names."""

    async def helper(self, job, *args, timeout=None, input=None):
        name = args[0]
        if name == "machine-db-replica-stop":
            self.calls.append(args)
            return {"checkpoint": "0/3000060", "seconds": 0.4} if args[2] == "stop" else {"resumed": True}
        if name == "machine-db-arrive" and args[2] in ("wait", "promote", "unaside"):
            self.calls.append(args)
            return {"arrived": True, "seconds": 0.2} if args[2] == "wait" else {"promote_s": 0.3}
        if name in ("forward", "db-empty", "owncert-read"):
            self.calls.append(args)
            return {}
        return await super().helper(job, *args, timeout=timeout, input=input)


@pytest.fixture
def switch(customer, provider, env, monkeypatch, tmp_path):
    """The provider and the customer of test_handover, with the switch's helpers and commands faked; luca has
    two-step verification at the provider's, the customer has a user mario already, acme a schedule, a destination
    and a git credential, and a token that must not pass."""
    root = Faked(tmp_path, monkeypatch)
    root.projects.update(provider.root.projects)
    provider.root = root
    provider.commands = Commands(monkeypatch)
    app = provider._transport.app.app
    provider.recovery = app.state.second_factors.enable("luca", totp.new_secret(), 7)
    # the customer's machine is reached from outside on 80 and 443 (Switch wants it, UX-014 A8)
    monkeypatch.setattr(handover, "reaches", lambda ip, port, timeout=3: True)
    provider.published = []

    async def publish(job, w, **values):
        provider.published.append((w.name, values.get("hold")))
        return {}
    monkeypatch.setattr(workspaceops, "publish", publish)
    return customer


async def to_ready(provider, env, c):
    provider_trees(provider, env)
    code = await prepare(provider)
    hid, job = await receive(c, code)
    assert job["status"] == SUCCESS, job["lines"]
    return hid


async def test_the_switch_passes_the_people_and_their_secrets_and_a_lost_commit_is_asked_again(switch, provider, env,
                                                                                               monkeypatch):
    far = provider.far_settings
    far_app = provider.far
    async with switch as c:
        hid = await to_ready(provider, env, c)
        # the customer had a user mario already: the provider's arrives as mario.acme-in
        far.users["mario"] = far.users["admin"]
        # Switch asks the name written
        resp = await c.post(f"/api/handover/{hid}/switch", headers=FAR, json={"confirm": "acme"})
        assert resp.status_code == 400 and "acme-in" in resp.json()["error"]
        # the commit's first answer is lost on its way back: the provider did it; asked again with the same switch
        lost = {"n": 0}
        app = provider._transport.app

        class Losing(httpx.ASGITransport):
            async def handle_async_request(self, request):
                answer = await super().handle_async_request(request)
                if request.url.path.endswith("/switch/commit") and lost["n"] == 0:
                    lost["n"] += 1
                    await answer.aread()
                    raise httpx.ReadTimeout("the answer was lost", request=request)
                return answer
        monkeypatch.setattr(handover, "client", lambda url, pin, timeout=None: httpx.AsyncClient(
            transport=Losing(app=app), base_url=url))
        monkeypatch.setattr(handover, "SWITCH_MOST", 600)
        resp = await c.post(f"/api/handover/{hid}/switch", headers=FAR, json={"confirm": "acme-in"})
        assert resp.status_code == 200, resp.text
        job = await wait_job(c, resp.json()["job"])
        assert job["status"] == SUCCESS, job["lines"]
        text = "\n".join(job["lines"])
        assert "asked again, the same switch" in text and "answered at the try 2" in text
        assert lost["n"] == 1
        # its names published held until the commit, then released
        assert provider.published[-2:] == [("acme-in", {"odoo": ["production"]}), ("acme-in", {})]
        # the crons switched off before the start are switched on again by id after the commit
        crons = [r for r in provider.commands.ran if "crons" in r]

        assert crons[-2][-1] == "off" and crons[-1][-2:] == ("on", "5,6")
        mine = handover.In(far.jobs_dir.parent).state(hid)
        assert mine["phase"] == "switched" and mine["imported"]["renamed"] == {"mario": "mario.acme-in"}
        page = (await c.get("/workspaces/acme-in", headers={"Authorization": AUTH})).text
        assert 'data-phase="switched"' in page
    # the people: the same hashes, the same two-step secret with its last step; the workspace's roles
    settings = env["settings"]
    assert far.users["mario.acme-in"] == settings.users["mario"] and far.users["luca"] == settings.users["luca"]
    assert far.user_roles["luca"] == "workspace"
    there = provider._transport.app.app.state.second_factors.record("luca")
    here = far_app.state.second_factors.record("luca")
    assert here["secret"] == there["secret"] and here["last"] == there["last"] and here["recovery"] == there["recovery"]
    ws = far.workspaces["acme-in"]
    assert ws.members == {"mario.acme-in": "admin", "luca": "user"} and ws.domain == "acme.example.com"
    # no token passes; the claim's key, the sealed secrets and the session are gone from the customer's machine
    assert not [n for n in far.tokens if far.token_workspace(n) == "acme-in"]
    folder = far.jobs_dir.parent / "handover" / "in" / hid
    assert sorted(p.name for p in folder.iterdir()) == ["state.json"]
    # the extract of the access log, kept apart (600)
    archive = list((far.jobs_dir.parent / "access-archive").iterdir())
    assert len(archive) == 1 and oct(archive[0].stat().st_mode & 0o777) == "0o600"
    head = json.loads(archive[0].read_text().splitlines()[0])
    assert head["extract"] == "acme" and head["entries"] >= 1
    # at the provider's: handed over, the copy stopped and kept 7 days, its people sent away at the login
    out = handover.Out(settings.jobs_dir.parent).state("acme")
    assert out["phase"] == "committed" and out["switch"]["phase"] == "committed"
    assert abs(float(out["delete_after"]) - time.time() - 7 * 86400) < 120
    assert "the answer was lost" not in json.dumps(out)
    resp = await provider.post("/login", data={"username": "luca", "password": PASSWORD, "next": "/"},
                               headers={"Origin": f"http://{HOST}"}, follow_redirects=False)
    assert "moved to another machine" in resp.text and resp.status_code == 403
    # switched, the workspace is the customer's own: deleted like any other, its reception's record with it
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=BasicAsSession(far_app)),
                                 base_url="http://far.test") as c:
        resp = await c.post("/api/workspaces/delete", headers=FAR, json={"name": "acme-in"})
        assert "being received" not in resp.text
        if resp.status_code == 200:
            assert not folder.exists()


async def test_a_switch_whose_deadline_passes_runs_at_the_providers_again(switch, provider, env, monkeypatch):
    settings = env["settings"]
    async with switch as c:
        hid = await to_ready(provider, env, c)
    session = handover.In(provider.far_settings.jobs_dir.parent).get_secret(hid, "session").decode()
    auth = {"Authorization": f"Bearer {session}"}
    api = f"/api/handover/v1/{hid}"
    begun = await provider.post(f"{api}/switch/begin", headers=auth, json={"switch_id": "ab" * 8})
    assert begun.status_code == 200, begun.text
    # the same switch_id again: the same switch; another one: 409
    assert (await provider.post(f"{api}/switch/begin", headers=auth, json={"switch_id": "ab" * 8})).status_code == 200
    assert (await provider.post(f"{api}/switch/begin", headers=auth, json={"switch_id": "cd" * 8})).status_code == 409
    for _ in range(100):
        sw = (await provider.get(f"{api}/switch", headers=auth)).json()["switch"]
        if sw["phase"] == "frozen":
            break
        await asyncio.sleep(0.05)
    assert sw["phase"] == "frozen" and sw["instances"]["acme-odoo"]["checkpoint"] == "0/3000060"
    out = handover.Out(settings.jobs_dir.parent)
    # nothing but the switch's own runs on the instance meanwhile
    app = provider._transport.app.app
    assert "being switched" in app.state.jobs.frozen("acme-odoo/production")
    # the deadline passes without a commit: the provider aborts by itself
    state = out.state("acme")
    out.save("acme", {**state, "switch": {**state["switch"], "deadline": time.time() - 1}})
    await app.state.handover_look()
    for _ in range(200):
        if out.state("acme").get("phase") == "ready":
            break
        await asyncio.sleep(0.05)
    state = out.state("acme")
    assert state["phase"] == "ready" and state["switch"]["phase"] == "expired"
    assert ("acme.main.odoo",) == tuple(a[1] for a in provider.root.asked("machine-db-replica-stop") if a[2] == "resume")
    assert any(r[1:] == ("start",) for r in provider.commands.ran)
    assert not app.state.jobs.frozen("acme-odoo/production")
    # a commit after it: 409, with the phase, so that the customer takes back what it did
    late = await provider.post(f"{api}/switch/commit", headers=auth, json={"switch_id": "ab" * 8})
    assert late.status_code == 409
    # the abort asked again answers how it went
    again = await provider.post(f"{api}/switch/abort", headers=auth, json={"switch_id": "ab" * 8})
    assert again.status_code == 200 and again.json()["switch"]["phase"] == "expired"


async def test_an_instance_that_does_not_start_here_takes_the_switch_back(switch, provider, env, monkeypatch):
    settings = env["settings"]
    async with switch as c:
        hid = await to_ready(provider, env, c)
        far_inst = provider.far_settings.servers["acme-in-odoo"].production
        provider.commands.failing.add(str(far_inst.options.http_port))
        resp = await c.post(f"/api/handover/{hid}/switch", headers=FAR, json={"confirm": "acme-in"})
        job = await wait_job(c, resp.json()["job"])
        assert job["status"] != SUCCESS
        mine = handover.In(provider.far_settings.jobs_dir.parent).state(hid)
        assert mine["phase"] == "failed" and "nothing is lost" in mine["error"]
    # at the provider's everything runs again; nothing was imported at the customer's
    out = handover.Out(settings.jobs_dir.parent).state("acme")
    assert out["phase"] == "ready" and out["switch"]["phase"] == "aborted"
    assert "luca" not in provider.far_settings.users
    assert [a for a in provider.root.asked("machine-db-arrive") if a[2] == "unaside"]


# --- the review of step 8: a switch never stuck «switching», a committed switch finished with its instances started,
# the blob confined to the received workspace, the provider's people not named in it, a person of two workspaces --

POST = {"Authorization": AUTH, "Origin": f"http://{HOST}"}


class Cut:
    """The customer's calls to the provider, cut (no answer) while on; with after (a path's end), the cut begins
    once a call to that path was answered there, its answer lost."""

    def __init__(self, provider, monkeypatch, after=""):
        self.on, self.after, self.seen = False, after, asyncio.Event()
        app = provider._transport.app
        cut = self

        class Transport(httpx.ASGITransport):
            async def handle_async_request(self, request):
                if cut.on:
                    raise httpx.ConnectError("cut", request=request)
                answer = await super().handle_async_request(request)
                if cut.after and request.url.path.endswith(cut.after):
                    await answer.aread()
                    cut.on = True
                    cut.seen.set()
                    raise httpx.ReadTimeout("the answer was lost", request=request)
                return answer
        monkeypatch.setattr(handover, "client", lambda url, pin, timeout=None: httpx.AsyncClient(
            transport=Transport(app=app), base_url=url))


def running(provider, monkeypatch):
    """/web/login answers on a customer's instance's port while its project was started last (Commands)."""
    async def answers(port, most=None):
        for srv in provider.far_settings.servers.values():
            inst = srv.production
            if inst is not None and str(inst.options.http_port) == str(port):
                return inst.restart.service in provider.commands.started
        return True
    monkeypatch.setattr(zerodowntime, "answers", answers)


async def test_a_retry_while_the_provider_does_not_answer_stays_interrupted(switch, provider, env, monkeypatch):
    far = provider.far_settings
    async with switch as c:
        hid = await to_ready(provider, env, c)
        store = handover.In(far.jobs_dir.parent)
        store.update(hid, phase="interrupted", error="lost", switch={"id": "ab" * 8, "since": handover.iso(time.time())})
        cut = Cut(provider, monkeypatch)
        cut.on = True
        resp = await c.post(f"/api/handover/{hid}/retry", headers=FAR, json={})
        assert resp.status_code == 200, resp.text
        job = await wait_job(c, resp.json()["job"])
        # never «switching» with no job: interrupted again, and Retry is offered again
        state = store.state(hid)
        assert state["phase"] == "interrupted" and "Retry" in state["error"], state
        assert job["status"] == "failed" and "does not answer" in job["error"]
        again = await c.post(f"/api/handover/{hid}/retry", headers=FAR, json={})
        assert again.status_code == 200 and "under way" not in again.text
        await wait_job(c, again.json()["job"])
        # the job of Retry holds the servers of the workspace (AD-011: server_busy)
        assert job["instances"] == ["acme-in-odoo/production"]


async def test_a_switch_whose_job_is_gone_is_interrupted_at_the_look(switch, provider, env, monkeypatch):
    far = provider.far_settings
    async with switch as c:
        hid = await to_ready(provider, env, c)
        store = handover.In(far.jobs_dir.parent)
        # OmniaCLI stopped during the switch: its job is not running any more
        store.update(hid, phase="switching", job="20261124-000000-gone00", switch={"id": "ab" * 8})
        await provider.far.state.handover_look()
        state = store.state(hid)
        assert state["phase"] == "interrupted" and "Retry asks Studio Rossi how it ended" in state["error"]


async def test_a_failure_after_the_commit_is_interrupted_and_retry_finishes_it_without_the_provider(
        switch, provider, env, monkeypatch):
    far = provider.far_settings
    async with switch as c:
        hid = await to_ready(provider, env, c)
        # the names are not released here after the commit (publish fails): the switch is committed there
        real = workspaceops.publish
        failing = {"on": True}

        async def publish(job, w, **values):
            if values.get("hold") == {} and failing["on"]:
                raise ops.JobFailed("nginx -t refused the site")
            return await real(job, w, **values)
        monkeypatch.setattr(workspaceops, "publish", publish)
        resp = await c.post(f"/api/handover/{hid}/switch", headers=FAR, json={"confirm": "acme-in"})
        job = await wait_job(c, resp.json()["job"])
        assert job["status"] == "failed", job["lines"]
        store = handover.In(far.jobs_dir.parent)
        state = store.state(hid)
        assert state["phase"] == "interrupted" and "Retry finishes it" in state["error"], state
        assert handover.Out(env["settings"].jobs_dir.parent).state("acme")["phase"] == "committed"
        # Retry, the provider not answering: it was committed, this machine finishes it without asking
        failing["on"] = False
        cut = Cut(provider, monkeypatch)
        cut.on = True
        resp = await c.post(f"/api/handover/{hid}/retry", headers=FAR, json={})
        job = await wait_job(c, resp.json()["job"])
        assert job["status"] == SUCCESS, job["lines"]
        assert store.state(hid)["phase"] == "switched"
        assert provider.published[-1] == ("acme-in", {})


async def test_a_committed_switch_stopped_with_its_answer_lost_starts_its_instances_at_retry(
        switch, provider, env, monkeypatch):
    far = provider.far_settings
    running(provider, monkeypatch)
    async with switch as c:
        hid = await to_ready(provider, env, c)
        # the commit is done there, its answer lost, and the provider does not answer any more
        cut = Cut(provider, monkeypatch, after="/switch/commit")
        resp = await c.post(f"/api/handover/{hid}/switch", headers=FAR, json={"confirm": "acme-in"})
        job_id = resp.json()["job"]
        await asyncio.wait_for(cut.seen.wait(), 60)
        await asyncio.sleep(0.2)
        # the job stops meanwhile: the end is not known here
        provider.far.state.jobs.stop(job_id, "manual: admin")
        job = await wait_job(c, job_id)
        assert job["status"] == "stopped"
        store = handover.In(far.jobs_dir.parent)
        assert store.state(hid)["phase"] == "interrupted"
        project = far.servers["acme-in-odoo"].production.restart.service
        # the instance held here is left as it was (a switch committed there finishes with it)
        assert project in provider.commands.started
        # say the machine restarted meanwhile: the instance is stopped here
        provider.commands.started.discard(project)
        cut.on, cut.after = False, ""
        resp = await c.post(f"/api/handover/{hid}/retry", headers=FAR, json={})
        job = await wait_job(c, resp.json()["job"])
        assert job["status"] == SUCCESS, job["lines"]
        assert store.state(hid)["phase"] == "switched"
        # started again (its crons off, by id) before its names were released, and its crons on after
        assert project in provider.commands.started
        assert "it starts again before its names lead here" in "\n".join(job["lines"])
        crons = [r for r in provider.commands.ran if "crons" in r]
        assert crons[-1][-2:] == ("on", "5,6")


async def test_what_the_provider_seals_stays_in_the_workspace_received_and_names_none_of_its_people(
        switch, provider, env, monkeypatch):
    settings = env["settings"]
    far = provider.far_settings
    app = provider._transport.app.app
    # at the provider's: a destination, a schedule made by its admin with its address, a deploy started by its admin
    # from another machine, the workspace's addresses; luca is a member of beta too
    resp = await provider.post("/api/workspaces/acme/destinations/save", headers=POST, json={
        "name": "acme-sftp", "new": True, "kind": "sftp", "path": "backups/acme",
        "settings": {"host": "sftp.acme.example.com", "port": "22", "user": "acme", "pass": "not-a-real-one"}})
    assert resp.status_code == 200, resp.text
    resp = await provider.post("/api/schedules/save", headers=POST, json={"name": "acme-nightly", "schedule": {
        "instance": "acme-odoo/production", "cron": "30 3 * * *", "enabled": True, "user": "admin",
        "notify": ["ops@provider.example"],
        "backup": {"daily": 7, "weekly": 4, "monthly": 3, "destination": "acme-sftp"}}})
    assert resp.status_code == 200, resp.text
    resp = await provider.post("/api/credentials/save", headers=POST, json={
        "kind": "workspace", "name": "acme", "host": "git.acme.example.com", "username": "deploy", "token": "t0ken-acme"})
    assert resp.status_code == 200, resp.text
    deploys.save(deploys.folder_of(settings), "acme-odoo/production", {"deploys": [
        {"id": "j1", "job": "j1", "title": "Update acme-odoo from far.test", "by": "manual: admin", "outcome": "success",
         "repositories": [], "state": None},
        {"id": "j2", "job": "j2", "title": "Pull acme-odoo", "by": "manual: luca", "outcome": "success",
         "repositories": [], "state": None},
        {"id": "j3", "job": "j3", "title": "Restart acme-odoo", "by": "manual: mario", "outcome": "success",
         "repositories": [], "state": None}], "paused": {"by": "admin", "at": "2026-11-20T10:00:00"}})
    # the customer has a user mario already: the provider's arrives as mario.acme-in, its deploys with it
    far.users["mario"] = far.users["admin"]
    settings.workspaces["acme"] = dataclasses.replace(settings.workspaces["acme"], notify=["ops@provider.example"])
    settings.workspaces["beta"] = dataclasses.replace(settings.workspaces["beta"],
                                                      members={**settings.workspaces["beta"].members, "luca": "user"})
    # the summary of Prepare says who keeps reaching another workspace here
    view = (await provider.get("/api/workspaces/acme/handover", headers={"Authorization": AUTH})).json()
    assert view["people"]["shared_names"] == ["luca"] and view["people"]["shared"] == 1
    # a provider that seals more than its workspace: another instance's schedule, a script, a destination that did
    # not come with it, the git credentials, the deploys and the instances of another server
    real_seal = handover.seal

    def seal(data, key, aad):
        blob = json.loads(data)
        blob["schedules"]["evil-other"] = {"instance": "zz-other/production", "cron": "0 1 * * *", "enabled": True,
                                           "backup": {"daily": 1, "weekly": 0, "monthly": 0}}
        blob["schedules"]["evil-script"] = {"instance": "acme-odoo/production", "cron": "0 1 * * *", "enabled": True,
                                            "script": {"name": "x", "args": []}}
        blob["schedules"]["evil-dest"] = {"instance": "acme-odoo/production", "cron": "0 2 * * *", "enabled": True,
                                          "backup": {"daily": 1, "weekly": 0, "monthly": 0, "destination": "offsite"}}
        blob["credentials"]["server:zz-other"] = [{"host": "git.evil.example", "username": "x", "token": "t0ken-x"}]
        blob["workspace"]["member_servers"] = {"luca": ["acme-odoo", "zz-other"]}
        blob["deploys"]["zz-other"] = {"deploys": []}
        return real_seal(json.dumps(blob).encode(), key, aad)
    monkeypatch.setattr(handover, "seal", seal)
    async with switch as c:
        hid = await to_ready(provider, env, c)
        # the customer has a destination of that name already: the one that arrives is acme-sftp-acme-in (no dot)
        resp = await c.post("/api/destinations/save", headers=FAR, json={
            "name": "acme-sftp", "new": True, "kind": "sftp", "path": "mine",
            "settings": {"host": "sftp.customer.example", "port": "22", "user": "me", "pass": "not-a-real-one"}})
        assert resp.status_code == 200, resp.text
        resp = await c.post(f"/api/handover/{hid}/switch", headers=FAR, json={"confirm": "acme-in"})
        job = await wait_job(c, resp.json()["job"])
        assert job["status"] == SUCCESS, job["lines"]
        text = "\n".join(job["lines"])
    # nothing outside acme-in: only its own schedule, with the destination that came with it
    found = schedules.load_schedules(far.schedules_file)
    assert sorted(found) == ["acme-nightly"], sorted(found)
    nightly = found["acme-nightly"]
    assert nightly.instance == "acme-in-odoo/production" and nightly.backup.destination == "acme-sftp-acme-in"
    left = [line for line in text.splitlines() if "Left out" in line]
    assert "is left out" in text and left and "zz-other" in left[0] and "acme-odoo" not in left[0], left
    creds = gitcredentials.load(far.credentials_file)
    assert not [s for s in creds if "zz-other" in s] and "git.acme.example.com" in creds["workspace:acme-in"]
    assert far.workspaces["acme-in"].member_servers.get("luca") == ["acme-in-odoo"]
    # the provider's people are not named: the schedule has no maker here and no address, the deploy started by the
    # provider's admin reads as the provider, its machine as another machine; luca's own stays luca's
    assert not nightly.user and not nightly.notify and not far.workspaces["acme-in"].notify
    history = deploys.load(deploys.folder_of(far), "acme-in-odoo/production")
    assert [d["by"] for d in history["deploys"]] == ["manual: Studio Rossi", "manual: luca",
                                                     "manual: mario.acme-in"], history
    assert "far.test" not in history["deploys"][0]["title"] and history["paused"]["by"] == "Studio Rossi"
    # luca keeps reaching beta at the provider's: its app of two-step verification did not pass, its password did
    assert provider.far.state.second_factors.record("luca") is None
    assert far.users["luca"] == settings.users["luca"]
    assert app.state.second_factors.record("luca") is not None


async def test_a_custom_role_arrives_with_its_people_and_a_passkey_does_not(switch, provider, env, monkeypatch):
    settings = env["settings"]
    far = provider.far_settings
    app = provider._transport.app.app
    acme = settings.workspaces["acme"]
    settings.workspaces["acme"] = dataclasses.replace(acme, roles={"Backup only": ["logs", "backup", "download"]},
                                                      member_roles={"mario": "Backup only"})
    app.state.passkeys.add("luca", b"h" * 16, "omniacli.test", b"c" * 16, b"k" * 32, 0, "laptop", [], False)
    assert app.state.passkeys.count("luca") == 1
    async with switch as c:
        hid = await to_ready(provider, env, c)
        far.users["mario"] = far.users["admin"]
        resp = await c.post(f"/api/handover/{hid}/switch", headers=FAR, json={"confirm": "acme-in"})
        # every server of the workspace is busy for the switch (AD-011): another job on it waits for its end
        for _ in range(100):
            if provider.far.state.jobs.is_busy("acme-in-odoo/production"):
                break
            await asyncio.sleep(0.01)
        assert provider.far.state.jobs.is_busy("acme-in-odoo/production")
        job = await wait_job(c, resp.json()["job"])
        assert job["status"] == SUCCESS, job["lines"]
        assert job["instances"] == ["acme-in-odoo/production"]
    ws = far.workspaces["acme-in"]
    assert ws.roles == {"Backup only": ["logs", "backup", "download"]}
    # the custom role passes with the map of the names: mario arrived as mario.acme-in
    assert ws.member_roles == {"mario.acme-in": "Backup only"} and set(ws.members) == {"mario.acme-in", "luca"}
    assert provider.far.state.passkeys.count("luca") == 0
