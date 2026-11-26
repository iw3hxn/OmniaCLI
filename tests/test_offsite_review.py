# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""AD-016 slice 1, after the review of 3f09014: a workspace's people never read the name nor the folder of one of the
machine's destinations in a job or in their access log; a choice left by a test, a staging or a server gone keeps no
destination from being deleted and can be taken back; a server renamed or deleted takes its choices with it; a home's
sync never makes the host adopt one of the host's own destinations; «nowhere» of an older central's form changes
nothing for who cannot see the server's default; the Backups tab draws an older machine's «Copy also to»."""

import asyncio
import json
from pathlib import Path

import httpx

from omniacli import destinations, pullcrypto, schedules
from omniacli.app import create_app
from omniacli.config import Workspace

from .test_app import AUTH, POST, client, wait_job  # noqa: F401 - the client fixture
from .test_delegated_scope import HOME_TOKEN, pair  # noqa: F401 - the pair fixture
from .test_destinations import rclone  # noqa: F401 - the rclone fixture
from .test_offsite_choice import MACHINE, OF_ACME, OTHER, _backup_schedule, _seed
from .test_workspaces import basic, post_as, workspaces  # noqa: F401 - the workspaces fixture

TEMPLATES = Path(destinations.__file__).parent / "templates"


def _remote(root, name, workspace=""):
    return destinations.parse_destination(name, {
        "kind": "sftp", "path": "vault-of-the-provider/odoo", "workspace": workspace,
        "settings": {"host": str(root), "user": "u", "pass": "Remote-pw-1"}, "key": pullcrypto.new_key()})


async def test_a_workspaces_people_never_read_the_machines_destination(workspaces, client, env, rclone):  # noqa: F811
    settings = env["settings"]
    dest = _remote(rclone["root"], MACHINE)
    destinations.save_destinations(settings.destinations_file, {MACHINE: dest})
    choices = destinations.load_choices(settings.destinations_file)
    choices.default = MACHINE
    destinations.save_choices(settings.destinations_file, choices)
    # the automatic backup of acme's production copies to the machine's default
    resp = await client.post("/api/servers/srv/production/backup-schedule", headers=POST,
                             json={"enabled": True, "time": "02:30", "daily": 7, "weekly": 4, "monthly": 12})
    assert resp.status_code == 200, resp.text
    resp = await client.post("/api/schedules/backup.srv.production/run", headers=POST)
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success", job["lines"]
    there = rclone["root"] / "vault-of-the-provider" / "odoo" / "srv" / "production"
    [copy] = list(there.iterdir())
    # the lines OmniaCLI writes name it so; the job's title and error (copied into the workspace's access log) too
    lines = "\n".join(job["lines"])
    assert "--- copy to a destination of the server: srv/production/" in lines and f"for {MACHINE}" not in lines
    # brought back: its title neither
    local = settings.backup_dir / "srv" / "production" / copy.name[:-len(".ocpf")]
    local.unlink()
    resp = await client.post("/api/servers/srv/production/remote-backups/fetch", headers=post_as("mario"),
                             json={"file": copy.name})
    assert resp.status_code == 200, resp.text
    back = await wait_job(client, resp.json()["job"])
    assert back["status"] == "success" and MACHINE not in back["title"], back["title"]
    # what mario reads: the jobs, and his workspace's access log (the jobs' titles are copied there)
    for job_id in (job["id"], back["id"]):
        raw = (await client.get(f"/api/jobs/{job_id}", headers=basic("mario"))).text
        assert MACHINE not in raw and "vault-of-the-provider" not in raw, raw[:400]
    entries = [e for e in client.app.state.access_log.recent() if e.get("workspace") == "acme"]
    assert entries and not [e for e in entries if MACHINE in json.dumps(e) or "vault-of-the-provider" in json.dumps(e)]
    # a line written by rclone or by a release before (with the name and the folder) is taken out when he reads it
    async def action(job):
        job.log(f"--- copy to {MACHINE} (SFTP): vault-of-the-provider/odoo/srv/production/x.ocpf")
    made = client.app.state.jobs.submit("Backup srv/production", ["srv/production"], "schedule: x", action)
    for _ in range(50):
        if made.finished_flag:
            break
        await asyncio.sleep(0.02)
    raw = (await client.get(f"/api/jobs/{made.id}", headers=basic("mario"))).text
    assert MACHINE not in raw and "vault-of-the-provider/odoo" not in raw, raw[:400]


def test_the_migration_leaves_tests_stagings_and_servers_gone(env):
    settings = env["settings"]
    _seed(settings)
    schedules.save_schedules(settings.schedules_file, {
        "backup.srv.test": _backup_schedule("srv/test", OTHER),
        "backup.gone.production": _backup_schedule("gone/production", MACHINE),
        "backup.srv.production": _backup_schedule("srv/production", OTHER)})
    create_app(settings)
    assert destinations.load_choices(settings.destinations_file).instances.keys() == {"srv/production"}


async def test_a_choice_left_behind_keeps_no_destination_and_goes(client, env):
    settings = env["settings"]
    _seed(settings)
    choices = destinations.load_choices(settings.destinations_file)
    choices.instances["srv/test"] = {"destination": OTHER, "since": "x"}
    choices.instances["gone/production"] = {"destination": MACHINE, "since": "x"}
    destinations.save_choices(settings.destinations_file, choices)
    # deleted all the same: nobody's backups go there
    resp = await client.post("/api/destinations/delete", headers=POST, json={"name": MACHINE})
    assert resp.status_code == 200, resp.text
    # a test's choice taken back by who sets its backups, a server gone's by the machine's admin
    resp = await client.post("/api/servers/srv/test/backup-destination", headers=POST, json={"destination": ""})
    assert resp.status_code == 200, resp.text
    left = destinations.load_choices(settings.destinations_file).instances
    assert "srv/test" not in left
    resp = await client.post("/api/destinations/delete", headers=POST, json={"name": OTHER})
    assert resp.status_code == 200, resp.text


async def test_a_server_renamed_or_deleted_takes_its_choice(client, env):
    settings = env["settings"]
    settings.workspaces["acme"] = Workspace("acme", servers=["srv"])
    _seed(settings)
    resp = await client.post("/api/servers/srv/production/backup-destination", headers=POST,
                             json={"destination": OTHER})
    assert resp.status_code == 200
    # a leftover of a server gone with a name a new one takes: not inherited
    choices = destinations.load_choices(settings.destinations_file)
    choices.instances["newer/production"] = {"destination": OTHER, "since": "x"}
    destinations.save_choices(settings.destinations_file, choices)
    resp = await client.post("/api/servers/delete", headers=POST, json={"name": "srv"})
    assert resp.status_code == 200, resp.text
    assert "srv/production" not in destinations.load_choices(settings.destinations_file).instances


async def test_a_homes_sync_never_adopts_a_destination_of_the_host(pair):  # noqa: F811
    pair.place(copy_here=False)
    pair.far_settings.servers["acme-srv"] = pair.far_settings.servers["srv"]
    pair.far_settings.workspaces["acme"].servers.append("acme-srv")
    private = destinations.parse_destination("host-private", {
        "kind": "sftp", "path": "p", "settings": {"host": "h.example", "user": "u", "pass": "pw"},
        "key": pullcrypto.new_key()})
    destinations.save_destinations(pair.far_settings.destinations_file, {"host-private": private})
    body = {"schedules": {"backup.acme-srv.production": {"instance": "acme-srv/production", "cron": "30 3 * * *",
                                                          "backup": {"destination": "host-private"}}}}
    headers = {"Authorization": f"Bearer {HOME_TOKEN}", "Accept": "application/json"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=pair.far), base_url="http://far.test") as far:
        resp = await far.post("/api/placements/acme/srv/sync", headers=headers, json=body)
        assert resp.status_code == 200, resp.text
        job = pair.far.state.jobs.get(resp.json()["job"])
        for _ in range(100):
            if job.finished_flag:
                break
            await asyncio.sleep(0.05)
    assert job.status == "success", job.lines
    assert destinations.load_choices(pair.far_settings.destinations_file).choice("acme-srv/production") == ""


async def test_nowhere_of_an_older_form_changes_nothing_for_a_workspaces_admin(workspaces, client, env):
    settings = env["settings"]
    _seed(settings, default=MACHINE)
    url = "/api/servers/srv/production"
    resp = await client.post(f"{url}/backup-destination", headers=post_as("mario"), json={"destination": OF_ACME})
    assert resp.status_code == 200
    # the form of a central of the release before: its «nowhere» is ""
    resp = await client.post(f"{url}/backup-schedule", headers=post_as("mario"),
                             json={"enabled": True, "time": "02:30", "daily": 7, "weekly": 4, "monthly": 12,
                                   "destination": ""})
    assert resp.status_code == 200, resp.text
    assert destinations.load_choices(settings.destinations_file).choice("srv/production") == OF_ACME


def test_the_backups_tab_draws_an_older_machines_copy_also_to():
    """A central of this release drawing the Backups tab of a machine of the release before (its backup-schedule has
    no offsite): its «Copy also to» from destination and destinations, saved with the automatic backups as that
    release did, the destination always sent (that release writes what it is given), the line and the copies from
    its destination; never «on this machine only» while it copies."""
    page = (TEMPLATES / "server.html").read_text(encoding="utf-8")
    assert 'offLegacy = !("offsite" in data);' in page
    assert "if (offLegacy) drawLegacyOffsite(data); else drawOffsite(data.offsite);" in page
    assert "loadRemoteCopies(offLegacy ? data.destination : data.offsite && data.offsite.effective_kind);" in page
    assert "if (offLegacy) body.destination = offLegacyValue;" in page
    assert "said.push(data.destination ? T.abCopy" in page
    assert "if (offLegacy) { await abSave({destination: event.target.value});" in page
