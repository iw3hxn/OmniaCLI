# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""After the switch of a handover (AD-013, step 9 of §9 of docs/adr/AD-008-consegna-e-spostamento.md; UX-014 A7,
A10): the copy at the provider's stays stopped until its day, the hourly round deletes it then (or Delete the copy
now… before), whole: its instances, backups, entries, users of it only, the workspace and its disk, one entry in
the access log; a staging on a machine that does not answer leaves it partial, tried again each hour; Light the copy
again… (the name written) runs it here again before that."""

import json
import time
from pathlib import Path

import pytest

from omniacli import handover, machinedb, workspaceops
from omniacli.jobs import SUCCESS

from .test_app import AUTH, HOST
from .test_handover import customer, prepare, provider, provider_trees, receive  # noqa: F401 - fixtures
from .test_handover_switch import FAR, switch, to_ready, wait_job  # noqa: F401 - fixtures
from .test_staging_dialog import acme, two  # noqa: F401 - the fixtures
from .test_workspaces import PASSWORD, workspaces  # noqa: F401 - the workspaces fixture

POST = {"Authorization": AUTH, "Origin": f"http://{HOST}"}


async def committed(switch, provider, env) -> None:
    """acme handed over to the customer's machine (the switch of test_handover_switch, committed)."""
    async with switch as c:
        hid = await to_ready(provider, env, c)
        resp = await c.post(f"/api/handover/{hid}/switch", headers=FAR, json={"confirm": "acme-in"})
        assert resp.status_code == 200, resp.text
        job = await wait_job(c, resp.json()["job"])
        assert job["status"] == SUCCESS, job["lines"]
    assert handover.Out(env["settings"].jobs_dir.parent).state("acme")["phase"] == "committed"


@pytest.fixture
def dropped(monkeypatch):
    """workspace-helper drop faked: the workspaces whose disk was taken away."""
    gone = []

    async def drop_disk(job, ws):
        gone.append(ws.name)
        return "its disk (4 GB) taken away, the customer's certificate with it"
    monkeypatch.setattr(workspaceops, "drop_disk", drop_disk)
    return gone


def access_lines(settings, words: str) -> list[dict]:
    rows = []
    for path in sorted(settings.logs_dir.glob("access*.jsonl")):
        for line in path.read_text().splitlines():
            row = json.loads(line)
            if words in str(row.get("detail") or ""):
                rows.append(row)
    return rows


async def test_the_banner_offers_delete_now_and_light_again_to_the_machines_admins(switch, provider, env, dropped):
    await committed(switch, provider, env)
    page = (await provider.get("/workspaces/acme", headers={"Authorization": AUTH})).text
    assert 'data-phase="committed" data-deletion="none"' in page
    assert 'id="handover-delete"' in page and 'id="handover-relight"' in page
    # their texts stay theirs: no attribute the page's script turns into a moment (data-when) on them
    assert ">Delete the copy now…</button>" in page and ">Light the copy again…</button>" in page
    assert "data-when=" not in page[page.index('id="handover-band"'):page.index('id="hv-redo-dialog"')]
    # nothing new is made in the stopped copy: New instance off, saying why, and refused
    assert 'id="new-instance" disabled' in page and "is handed over to the customer: its copy here is stopped" in page
    resp = await provider.post("/api/workspaces/acme/instances/create", headers=POST,
                               json={"instance": "more", "template": "odoo-19"})
    assert resp.status_code == 400 and "handed over" in resp.json()["error"]
    # neither is the page's primary: both small and danger, each with a title of its own
    for button in ("handover-delete", "handover-relight"):
        tag = page[page.index(f'id="{button}"') - 60:page.index(f'id="{button}"') + 400]
        assert 'class="small danger"' in tag and 'title="' in tag
    # a workspace's own admin reads no banner and may not delete or light it
    for path, body in (("delete", {}), ("relight", {"confirm": "acme"})):
        resp = await provider.post(f"/api/workspaces/acme/handover/{path}", headers={**POST, "Authorization": "Basic bWFyaW86c2VjcmV0"},
                                   json=body)
        assert resp.status_code in (401, 403)


async def test_delete_now_deletes_the_copy_whole_with_one_entry_in_the_access_log(switch, provider, env, dropped):
    settings = env["settings"]
    await committed(switch, provider, env)
    app = provider._transport.app.app
    backups = settings.backup_dir / "acme-odoo" / "production"
    backups.mkdir(parents=True, exist_ok=True)
    (backups / "acme-odoo-1.zip").write_bytes(b"x" * 2048)
    (backups / "acme-odoo-2.zip").write_bytes(b"y" * 1024)
    there = [p.stat().st_size for p in (settings.backup_dir / "acme-odoo").rglob("*") if p.is_file()]
    assert "luca" in settings.users
    # the fixture's link that does not answer would leave it partial (the round's test)
    down = settings.link_targets.pop("down")
    resp = await provider.post("/api/workspaces/acme/handover/delete", headers=POST, json={})
    assert resp.status_code == 200, resp.text
    job = await wait_job(provider, resp.json()["job"])
    assert job["status"] == SUCCESS, job["lines"]
    # its instance removed (workspace-helper remove), its backups' folder gone, the workspace and its disk too
    assert [a for a in provider.root.asked("remove") if a[1] == "acme"]
    assert "acme-odoo" not in settings.servers and not (settings.backup_dir / "acme-odoo").exists()
    assert "acme" not in settings.workspaces and dropped == ["acme"], job["lines"]
    # its users of it only, with their two-step verification and sessions; the handover's record
    assert "luca" not in settings.users and app.state.second_factors.record("luca") is None
    assert not handover.Out(settings.jobs_dir.parent).state("acme")
    deleted = job["result"]["deleted"]
    assert deleted["backups"] == len(there) >= 2 and deleted["backup_bytes"] == sum(there)
    # one entry in the access log says what went
    rows = access_lines(settings, "the copy of acme handed over to the customer deleted")
    assert len(rows) == 1 and f"{len(there)} backup files" in rows[0]["detail"] and "acme-odoo" in rows[0]["detail"]
    settings.link_targets["down"] = down


async def test_the_hourly_round_deletes_on_its_day_and_tries_a_partial_one_again(switch, provider, env, dropped):
    settings = env["settings"]
    await committed(switch, provider, env)
    app = provider._transport.app.app
    out = handover.Out(settings.jobs_dir.parent)
    # before its day the round does nothing
    await app.state.handover_look()
    assert not out.state("acme").get("deletion")
    # a linked machine that does not answer for its stagings: the deletion ends partial
    assert "down" in settings.link_targets
    state = out.state("acme")
    out.save("acme", {**state, "delete_after": time.time() - 1})
    await app.state.handover_look()
    job_id = out.state("acme")["deletion"]["job"]
    job = await wait_job(provider, job_id)
    assert job["status"] == SUCCESS and job["result"].get("warnings"), job["lines"]
    state = out.state("acme")
    assert "down did not say its stagings" in state["deletion"]["partial"] and "acme" in settings.workspaces
    assert "acme-odoo" not in settings.servers and not dropped
    page = (await provider.get("/workspaces/acme", headers={"Authorization": AUTH})).text
    assert 'data-deletion="partial"' in page and 'id="handover-relight"' not in page
    # the next look within the hour does not try again; an hour later it does, and ends it
    await app.state.handover_look()
    down = settings.link_targets.pop("down")
    assert out.state("acme")["deletion"]["job"] == job_id
    out.save("acme", {**out.state("acme"), "deletion": {**out.state("acme")["deletion"], "tried": time.time() - 3601}})
    await app.state.handover_look()
    again = out.state("acme")["deletion"]["job"]
    assert again != job_id
    job = await wait_job(provider, again)
    assert job["status"] == SUCCESS and not job["result"].get("warnings"), job["lines"]
    assert "acme" not in settings.workspaces and dropped == ["acme"] and not out.state("acme")
    rows = access_lines(settings, "the copy of acme handed over to the customer deleted")
    # what went at the partial try is said too
    assert len(rows) == 1 and "users (" in rows[0]["detail"], rows
    settings.link_targets["down"] = down


async def test_light_again_runs_the_copy_here_with_the_name_written(switch, provider, env, dropped):
    settings = env["settings"]
    await committed(switch, provider, env)
    resp = await provider.post("/api/workspaces/acme/handover/relight", headers=POST, json={"confirm": "acm"})
    assert resp.status_code == 400 and "Write the name" in resp.json()["error"]
    started = len([r for r in provider.commands.ran if r[1:] == ("start",)])
    # the commit could not close its database of its own to the customer's machine (stopped for the switch, the
    # helper wants it running: lab3, 24/11): its record is still there
    state = handover.Out(settings.jobs_dir.parent).state("acme")
    folder = Path(provider.root.projects["acme.main.odoo"]) / machinedb.DEDICATED_FOLDER
    (folder / machinedb.REPLICA_FILE).write_text(json.dumps({"link": f"handover:{state['id']}",
                                                             "address": "203.0.113.40", "state": "stopped"}))
    drops = len(provider.root.asked("machine-db-replica-drop"))
    resp = await provider.post("/api/workspaces/acme/handover/relight", headers=POST, json={"confirm": "acme"})
    assert resp.status_code == 200, resp.text
    job = await wait_job(provider, resp.json()["job"])
    assert job["status"] == SUCCESS, job["lines"]
    # its database started where it stopped, its instance started; the handover's record gone: not deleted, not frozen
    assert len([r for r in provider.commands.ran if r[1:] == ("start",)]) == started + 1
    assert [a for a in provider.root.asked("machine-db-replica-stop") if a[2] == "resume"]
    # started again, its database is closed to the customer's machine
    assert len(provider.root.asked("machine-db-replica-drop")) == drops + 1
    assert not (folder / machinedb.REPLICA_FILE).exists()
    assert not handover.Out(settings.jobs_dir.parent).state("acme")
    app = provider._transport.app.app
    assert not app.state.jobs.frozen("acme-odoo/production")
    # its people log in here again; its tokens stay revoked
    resp = await provider.post("/login", data={"username": "luca", "password": PASSWORD, "next": "/"},
                               headers={"Origin": f"http://{HOST}"}, follow_redirects=False)
    assert resp.status_code in (302, 303), resp.text
    assert not [n for n in settings.tokens if settings.token_workspace(n) == "acme"]
    assert len(access_lines(settings, "lit again by")) == 1
    # nothing to delete or light any more
    resp = await provider.post("/api/workspaces/acme/handover/delete", headers=POST, json={})
    assert resp.status_code == 400 and "not handed over" in resp.json()["error"]
