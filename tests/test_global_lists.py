# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The lists of every machine (docs/ux/UX-008 §4): Instances and Backups,
this machine's and each link's, a link that does not answer said as such;
whether each production backs up by itself; a staging made from a backup."""

import datetime
import json
import time
import zipfile
from pathlib import Path

from omniacli import backupcheck, ops, stacktemplates, stagings, workspaceops

from .test_app import AUTH, POST, client, wait_job  # noqa: F401 - the client fixture
from .test_links import as_user, mine  # noqa: F401 - the mine fixture
from .test_workspaces import basic, post_as, workspaces  # noqa: F401 - the workspaces fixture


def unscheduled(servers: list[dict]) -> int:
    """What the page's head counts: the productions, not the stagings, with no automatic backups."""
    return sum(1 for s in servers if not s.get("parent") and s.get("backup_schedule")
               and not s["backup_schedule"]["scheduled"])


async def test_the_instances_of_every_machine(mine):
    page = (await mine.get("/instances", headers=as_user("admin"))).text
    # the creating action, the machine's admin only; a column for the servers (there are links)
    assert page.count('class="primary" data-new-instance') == 1 and '<th scope="col">Server</th>' in page
    assert '"cliente", "down"' in page or '["cliente", "down"]' in page
    # the menu: Instances is the list of every machine, Backups beside it
    assert 'class="nav-link" href="/instances"' in page and 'class="nav-link" href="/backups"' in page
    here = (await mine.get("/api/instances", headers=as_user("admin"))).json()
    assert here["link"] == "" and here["servers"] == [] and here["links"] == ["cliente", "down"]
    # a link: its /api/servers, asked through it, with the fields of the list
    there = (await mine.get("/api/instances?link=cliente", headers=as_user("admin"))).json()
    assert there["name"] == "cliente" and [s["name"] for s in there["servers"]] == ["srv"]
    srv = there["servers"][0]
    assert srv["backup_schedule"] == {"scheduled": False, "last": None} and srv["version"] == "" and srv["parent"] == ""
    assert unscheduled(there["servers"]) == 1
    # a link that does not answer: its error, at once, the others not held back
    started = time.monotonic()
    down = (await mine.get("/api/instances?link=down", headers=as_user("admin"))).json()
    assert down["link"] == "down" and "cannot connect" in down["error"] and "servers" not in down
    assert time.monotonic() - started < 2
    assert (await mine.get("/api/instances?link=nope", headers=as_user("admin"))).status_code == 404
    # in Italian, the reason too
    down = (await mine.get("/api/instances?link=down", headers={**as_user("admin"), "Accept-Language": "it"})).json()
    assert down["error"].startswith("connessione non riuscita")
    # a developer and a user: the list, no creating action, no Server menu entry for Backups for the user
    for user in ("dev", "viewer"):
        page = (await mine.get("/instances", headers=as_user(user))).text
        assert 'data-new-instance' not in page.split("</header>", 1)[1].split("<dialog", 1)[0], user
    assert 'href="/backups"' in (await mine.get("/instances", headers=as_user("dev"))).text
    assert 'href="/backups"' not in (await mine.get("/instances", headers=as_user("viewer"))).text


async def test_the_backups_of_every_machine(mine):
    page = await mine.get("/backups", headers=as_user("dev"))
    assert page.status_code == 200 and 'id="fb-dialog"' in page.text
    # the page reads: no primary outside its dialog
    assert 'class="primary"' not in page.text.split('<dialog id="fb-dialog"', 1)[0].split("</header>", 1)[1]
    here = (await mine.get("/api/backups", headers=as_user("dev"))).json()
    assert here["rows"] == [] and here["tonight"] == {"ok": 0, "failed": 0} and here["links"] == ["cliente", "down"]
    there = (await mine.get("/api/backups?link=cliente", headers=as_user("dev"))).json()
    assert there == {"name": "cliente", "link": "cliente", "rows": [], "tonight": {"ok": 0, "failed": 0}}
    assert "error" in (await mine.get("/api/backups?link=down", headers=as_user("dev"))).json()
    # a user has no Backups page
    assert (await mine.get("/backups", headers=as_user("viewer"))).status_code == 403
    assert (await mine.get("/api/backups", headers=as_user("viewer"))).status_code == 403


async def test_backup_schedule_and_the_failed_automatic_backups(client, env):
    settings = env["settings"]
    servers = (await client.get("/api/servers", headers={"Authorization": AUTH})).json()
    assert servers[0]["backup_schedule"] == {"scheduled": False, "last": None} and unscheduled(servers) == 1
    url = "/api/servers/srv/production/backup-schedule"
    body = {"enabled": True, "time": "02:30", "daily": 7, "weekly": 4, "monthly": 12}
    assert (await client.post(url, headers=POST, json=body)).status_code == 200
    servers = (await client.get("/api/servers", headers={"Authorization": AUTH})).json()
    assert servers[0]["backup_schedule"] == {"scheduled": True, "last": None} and unscheduled(servers) == 0
    # an automatic backup that fails: the last one, red, with its job; a row of its own in Backups
    backup_script = Path(settings.servers["srv"].production.options.venv_bin) / "click-odoo-backupdb"
    working = backup_script.read_text()
    backup_script.write_text("#!/bin/sh\necho 'disk full' >&2\nexit 1\n")
    resp = await client.post("/api/schedules/backup.srv.production/run", headers=POST)
    failed = resp.json()["job"]
    assert (await wait_job(client, failed))["status"] == "failed"
    last = (await client.get("/api/servers", headers={"Authorization": AUTH})).json()[0]["backup_schedule"]["last"]
    assert last["ok"] is False and last["status"] == "failed" and last["job"] == failed and last["message"]
    listed = (await client.get("/api/backups", headers={"Authorization": AUTH})).json()
    assert listed["tonight"] == {"ok": 0, "failed": 1}
    row = next(r for r in listed["rows"] if r.get("failed"))
    assert row["job"] == failed and row["tonight"] and row["server"] == "srv" and row["role"] == "production"
    # then one that succeeds: a file row, automatic, the newest first
    backup_script.write_text(working)
    resp = await client.post("/api/schedules/backup.srv.production/run", headers=POST)
    assert (await wait_job(client, resp.json()["job"]))["status"] == "success"
    last = (await client.get("/api/servers", headers={"Authorization": AUTH})).json()[0]["backup_schedule"]["last"]
    assert last["ok"] is True
    listed = (await client.get("/api/backups", headers={"Authorization": AUTH})).json()
    assert listed["tonight"] == {"ok": 1, "failed": 1}
    files = [r for r in listed["rows"] if not r.get("failed")]
    assert len(files) == 1 and files[0]["kind"] == "scheduled" and files[0]["download"].startswith("/backups/srv/production/")
    assert files[0]["can_delete"] and not files[0]["can_stage"]  # srv was not made in a workspace
    times = [r["time"] for r in listed["rows"]]
    assert times == sorted(times, reverse=True)
    # a developer reads them and may not delete the production's
    dev = (await client.get("/api/backups", headers=basic("dev"))).json()
    assert [r["can_delete"] for r in dev["rows"] if not r.get("failed")] == [False]


async def test_what_a_user_of_workspaces_sees(workspaces, client):
    # mario, admin of acme: its instances, no links, no Server column; its backups
    page = (await client.get("/instances", headers=basic("mario"))).text
    assert '<th scope="col">Server</th>' not in page and 'href="/instances"' in page and 'href="/backups"' in page
    here = (await client.get("/api/instances", headers=basic("mario"))).json()
    assert [s["name"] for s in here["servers"]] == ["srv"] and here["links"] == []
    assert (await client.get("/backups", headers=basic("mario"))).status_code == 200
    (Path(workspaces.backup_dir) / "srv" / "production").mkdir(parents=True)
    (Path(workspaces.backup_dir) / "srv" / "production" / "srv_1.zip").write_bytes(b"x")
    rows = (await client.get("/api/backups", headers=basic("mario"))).json()["rows"]
    assert [(r["server"], r["file"], r["can_delete"]) for r in rows] == [("srv", "srv_1.zip", True)]
    # luca, a user in acme: the instances, no Backups
    assert [s["name"] for s in (await client.get("/api/instances", headers=basic("luca"))).json()["servers"]] == ["srv"]
    assert 'href="/backups"' not in (await client.get("/instances", headers=basic("luca"))).text
    assert (await client.get("/api/backups", headers=basic("luca"))).status_code == 403
    # eva, a developer of beta (no servers): an empty list; zoe, in no workspace: none
    assert (await client.get("/api/backups", headers=basic("eva"))).json()["rows"] == []
    assert (await client.get("/backups", headers=basic("zoe"))).status_code == 403
    # a link's list is not for a user of workspaces
    assert (await client.get("/api/instances?link=x", headers=basic("mario"))).status_code == 404


async def test_a_staging_from_a_backup(workspaces, client, env, monkeypatch, tmp_path):
    """Restore into a new instance (D6): a new staging of the production with
    the database of the chosen file, neutralized; no backup made now; a file
    whose restore failed when tried is refused."""
    settings = env["settings"]
    template = stacktemplates.load_template(Path(__file__).parent.parent / "templates" / "odoo-19")
    monkeypatch.setattr(workspaceops, "templates", lambda: {template.name: template})
    settings.storage_roots = {"main": str(tmp_path)}
    ws = settings.workspaces["acme"]
    ws.storage, ws.limits = "main", {"disk_gb": 50}
    monkeypatch.setattr(workspaceops, "storage_path", lambda w: tmp_path)
    (tmp_path / "acme" / "odoo").mkdir(parents=True)
    (tmp_path / "acme" / "odoo" / "instance.json").write_text(json.dumps({"template": "odoo-19"}))
    ports = iter(range(20000, 20100, 2))

    async def create_instance(job, w, instance, tpl, values, setup=None):
        port = next(ports)
        return {"type": "docker", **{role: {
            "database": instance.replace("-", "_"), "odoo_service": "odoo", "db_service": "db", "http_port": port + i,
            "update_modules": True, "restart": {"type": "compose", "service": str(tmp_path / instance / role), "sudo": True},
            "repositories": []} for i, role in enumerate(("production", "test"))}}

    async def publish(job, w):
        return {}
    steps = []

    async def backup_into(job, instance, folder, options):
        steps.append(("backup", instance.key))
        return tmp_path / "now.zip"

    async def stop(job, instance):
        steps.append(("stop", instance.key))
    monkeypatch.setattr(workspaceops, "create_instance", create_instance)
    monkeypatch.setattr(workspaceops, "publish", publish)
    monkeypatch.setattr(ops, "backup_into", backup_into)
    monkeypatch.setattr(ops, "stop", stop)
    resp = await client.post("/api/workspaces/acme/instances/create", headers=post_as("mario"),
                             json={"instance": "odoo", "template": "odoo-19"})
    assert (await wait_job(client, resp.json()["job"]))["status"] == "success"
    plugin = type(settings.servers["acme-odoo"].production.plugin)

    async def restore(self, job, instance, backup_zip, options):
        steps.append(("restore", instance.key, backup_zip.name, options))
    monkeypatch.setattr(plugin, "restore", restore)
    folder = Path(settings.backup_dir) / "acme-odoo" / "production"
    folder.mkdir(parents=True)
    for name in ("good.zip", "bad.zip"):
        with zipfile.ZipFile(folder / name, "w") as zf:
            zf.writestr("dump.sql", "-- dump")
    backupcheck.save(folder, {"bad.zip": {"verified": {"time": "2026-11-16T03:10:00", "ok": False, "detail": "no"}}})
    # the list offers it on the production's backups, to the admin of the workspace
    rows = (await client.get("/api/backups", headers=basic("mario"))).json()["rows"]
    assert {r["file"]: r["can_stage"] for r in rows if r["server"] == "acme-odoo"} == {"good.zip": True, "bad.zip": True}
    assert next(r for r in rows if r.get("file") == "bad.zip")["verified"]["ok"] is False
    url = "/api/servers/acme-odoo/stagings/from-backup"
    # a backup whose restore failed when tried, one that is not there, a user who is no admin: refused
    resp = await client.post(url, headers=post_as("mario"), json={"file": "bad.zip"})
    assert resp.status_code == 400 and "16/11/2026" in resp.json()["error"]
    for bad in ("nope.zip", "../good.zip"):
        assert (await client.post(url, headers=post_as("mario"), json={"file": bad})).status_code == 404
    assert (await client.post(url, headers=post_as("luca"), json={"file": "good.zip"})).status_code == 403
    assert settings.workspaces["acme"].servers == ["srv", "acme-odoo"]
    steps.clear()
    resp = await client.post(url, headers=post_as("mario"), json={"file": "good.zip"})
    assert resp.status_code == 200, resp.text
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success", job["lines"]
    made = resp.json()["server"]
    assert made == f"acme-odoo-stg-{datetime.date.today():%Y%m%d}"
    # no backup made now: the file, restored neutralized; its test stopped
    assert steps == [("restore", f"{made}/production", "good.zip", {"neutralize": True}), ("stop", f"{made}/test")]
    assert job["title"] == f"Staging of acme-odoo from the backup good.zip"
    record = stagings.load((settings.links_file or settings.servers_file).parent / stagings.FILE)
    assert record[made]["parent"] == "acme-odoo" and record[made]["backup"] == "good.zip" and record[made]["from_backup"]
    # in the lists: a staging under its production, no schedule of its own; the version of its template
    servers = {s["name"]: s for s in (await client.get("/api/instances", headers=basic("mario"))).json()["servers"]}
    assert servers[made]["parent"] == "acme-odoo" and servers[made]["backup_schedule"] is None
    assert servers["acme-odoo"]["version"] == "19.0" and servers["acme-odoo"]["workspace"] == "acme"
    # the file and the production did not change
    assert (folder / "good.zip").is_file()
