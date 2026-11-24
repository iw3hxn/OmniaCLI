# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The switch of a handover (AD-011, step 8 of §9 of docs/adr/AD-008-consegna-e-spostamento.md): two phases, the
provider coordinating; the sealed secrets (AD-009); the import of the users with their hashes and two-step
secrets (a name the customer has already arrives as <name>.<workspace>), the roles, the schedules, the
destinations, the credentials and the extract of the access log; a commit whose answer is lost asked again; the
deadline passed without a commit, the instances running at the provider's again and a late commit refused."""

import asyncio
import json
import time

import httpx
import pytest

from omniacli import accesslog, handover, ops, totp, workspaceops, zerodowntime
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
