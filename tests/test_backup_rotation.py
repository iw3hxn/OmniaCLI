# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The automatic backups of an instance: a schedule's backup step kept by a
rotation (days, weeks, months), its panel in the Backups tab; the backup
before an update; a backup deleted."""

import datetime
import os

import pytest

from omniacli import ops, schedules
from omniacli.config import ConfigError, parse_servers, server_to_dict
from omniacli.jobs import Job

from .test_app import AUTH, POST, client, wait_job  # noqa: F401 - the client fixture
from .test_links import as_user
from .test_workspaces import post_as


def _job():
    return Job(id="j", title="t", instances=[], source="s")


def test_the_rotation_keeps_the_newest_of_each_period():
    start = datetime.datetime(2026, 1, 1, 3, 0)
    # one backup a day for a year, two on the last day
    times = {f"d{i}": start + datetime.timedelta(days=i) for i in range(365)}
    times["last-early"] = start + datetime.timedelta(days=364, hours=-2)
    kept = schedules.rotation_kept(times, daily=7, weekly=4, monthly=12)
    # the last 7 days (the newer of the last day only), the newest of 4 weeks and 12 months
    assert {f"d{i}" for i in range(358, 365)} <= kept and "last-early" not in kept
    assert len(kept) == len({times[k].date() for k in kept})
    months = {(times[k].year, times[k].month) for k in kept}
    assert len(months) == 12
    assert len(kept) <= 7 + 4 + 12
    assert schedules.rotation_kept(times, 0, 0, 1) == {"d364"}
    assert schedules.rotation_kept({}, 7, 4, 12) == set()


def test_a_backup_step_parsed_and_back():
    s = schedules.parse_schedule("nightly", {"instance": "srv/production", "cron": "0 3 * * *",
                                             "backup": {"options": {"no_filestore": False}, "weekly": 0}})
    assert s.steps == ["backup"] and (s.backup.daily, s.backup.weekly, s.backup.monthly) == (7, 0, 12)
    assert schedules.parse_schedule("nightly", schedules.schedule_to_dict(s)) == s
    for bad, message in (({"daily": 0, "weekly": 0, "monthly": 0}, "at least one backup"),
                         ({"daily": -1}, "from 0 to 1000"), ({"monthly": "x"}, "not a number")):
        with pytest.raises(ConfigError, match=message):
            schedules.parse_schedule("n", {"instance": "srv/test", "cron": "0 3 * * *", "backup": bad})


async def test_a_scheduled_backup_rotates_only_its_own(tmp_path, monkeypatch):
    folder = tmp_path / "backups"
    folder.mkdir()
    now = datetime.datetime.now().timestamp()
    old = []
    for i in range(1, 4):
        path = folder / f"{schedules.SCHEDULED_PREFIX}db_{i}.zip"
        path.write_bytes(b"z")
        # three days of the same week, long ago: one of them is kept by its week
        os.utime(path, (now - 86400 * (400 + i), now - 86400 * (400 + i)))
        old.append(path)
    by_hand = folder / "db_hand.zip"
    by_hand.write_bytes(b"z")
    os.utime(by_hand, (now - 86400 * 999, now - 86400 * 999))

    async def backup_into(job, instance, dest_dir, options=None):
        made = dest_dir / "db_new.zip"
        made.write_bytes(b"new")
        return made
    monkeypatch.setattr(ops, "backup_into", backup_into)
    step = schedules.BackupStep({}, daily=1, weekly=0, monthly=0)
    dest = await schedules.scheduled_backup(_job(), None, folder, step)
    assert dest.name == f"{schedules.SCHEDULED_PREFIX}db_new.zip"
    # one day kept: the new one; the backup made by hand untouched
    assert {p.name for p in folder.iterdir()} == {dest.name, "db_hand.zip"}


class _Plugin:
    has_backup = True


class _Instance:
    def __init__(self, on):
        self.backup_before_update = on
        self.plugin = _Plugin()


async def test_the_backup_before_an_update(tmp_path, monkeypatch):
    made = []

    async def backup_into(job, instance, dest_dir, options=None):
        path = dest_dir / f"db_{len(made)}.zip"
        path.write_bytes(b"z")
        os.utime(path, (2000 + len(made), 2000 + len(made)))
        made.append(path)
        return path
    monkeypatch.setattr(ops, "backup_into", backup_into)
    assert await ops.backup_before_update(_job(), _Instance(False), tmp_path) is None
    assert await ops.backup_before_update(_job(), _Instance(True), None) is None
    for _ in range(5):
        assert (await ops.backup_before_update(_job(), _Instance(True), tmp_path)).is_file()
    # the last three kept, named as made before an update
    assert sorted(p.name for p in tmp_path.iterdir()) == [f"{ops.PRE_UPDATE_PREFIX}db_{i}.zip" for i in (2, 3, 4)]


def test_the_option_is_saved_only_when_on(env):
    server = env["settings"].servers["srv"]
    data = server_to_dict(server)
    assert "backup_before_update" not in data["production"]
    data["production"]["backup_before_update"] = True
    parsed = parse_servers({"srv": data}, env["settings"].logs_dir)["srv"]
    assert parsed.production.backup_before_update and not parsed.test.backup_before_update
    assert server_to_dict(parsed)["production"]["backup_before_update"] is True


async def test_a_pull_backs_up_only_when_it_brings_commits(env, monkeypatch):
    instance = env["settings"].servers["srv"].production
    instance.backup_before_update = True
    pending = {"n": 0}
    backups = []

    async def pull_preview(inst, timeout=60):
        return [{"name": r.name, "pending": pending["n"]} for r in inst.repositories]

    async def backup_before_update(job, inst, folder):
        backups.append(folder)

    async def nothing(*args, **kwargs):
        return False
    monkeypatch.setattr(ops, "pull_preview", pull_preview)
    monkeypatch.setattr(ops, "backup_before_update", backup_before_update)
    monkeypatch.setattr(ops, "pull", nothing)
    monkeypatch.setattr(ops, "restart", nothing)
    await ops.pull_restart(_job(), instance, only_if_changed=True, backup_folder=env["tmp"])
    assert backups == []
    pending["n"] = 2
    await ops.pull_restart(_job(), instance, only_if_changed=True, backup_folder=env["tmp"])
    assert backups == [env["tmp"]]
    # none asked (no folder given)
    await ops.pull_restart(_job(), instance, only_if_changed=True)
    assert backups == [env["tmp"]]


async def test_the_automatic_backups_panel(client, env):
    """Every day at a time, the rotation, the choices: a schedule of the
    instance, shown on the Schedules page too; a developer reads a
    production's and does not change it."""
    url = "/api/servers/srv/production/backup-schedule"
    data = (await client.get(url, headers={"Authorization": AUTH})).json()
    assert not data["exists"] and not data["enabled"] and data["time"] == "03:00"
    assert (data["daily"], data["weekly"], data["monthly"]) == (7, 4, 12) and data["can_edit"]
    body = {"enabled": True, "time": "02:30", "daily": 5, "weekly": 2, "monthly": 6, "options": {"no_filestore": True}}
    resp = await client.post(url, headers=POST, json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["enabled"] and data["time"] == "02:30" and data["next"] and data["zone"], data
    assert data["options"]["no_filestore"] is True, data
    sch = schedules.load_schedules(env["settings"].schedules_file)["backup.srv.production"]
    assert sch.cron == "30 2 * * *" and (sch.backup.daily, sch.backup.weekly, sch.backup.monthly) == (5, 2, 6)
    assert "backup.srv.production" in (await client.get("/schedules", headers={"Authorization": AUTH})).text
    # run now: a scheduled backup in the instance's backups, listed as automatic
    resp = await client.post("/api/schedules/backup.srv.production/run", headers=POST)
    assert (await wait_job(client, resp.json()["job"]))["status"] == "success"
    listing = (await client.get("/api/servers/srv/production/backups", headers={"Authorization": AUTH})).json()
    assert [(f["kind"], f["can_delete"]) for f in listing] == [("scheduled", True)]
    assert listing[0]["file"].startswith(schedules.SCHEDULED_PREFIX)
    # refused: a wrong time, no backup kept, a developer on a production
    for bad, message in (({**body, "time": "25:00"}, "not a time"),
                         ({**body, "daily": 0, "weekly": 0, "monthly": 0}, "at least one backup")):
        resp = await client.post(url, headers=POST, json=bad)
        assert resp.status_code == 400 and message in resp.text
    assert (await client.post(url, headers=post_as("dev"), json=body)).status_code == 403
    assert not (await client.get(url, headers=as_user("dev"))).json()["can_edit"]
    # off: the schedule had no other step, it goes
    resp = await client.post(url, headers=POST, json={**body, "enabled": False})
    assert resp.status_code == 200 and not resp.json()["exists"]
    assert "backup.srv.production" not in schedules.load_schedules(env["settings"].schedules_file)
    # the panel in the Backups tab
    page = (await client.get("/servers/srv", headers={"Authorization": AUTH})).text
    assert 'id="ab-form"' in page and 'data-ab-option="no_filestore"' in page


async def test_a_backup_deleted(client, env):
    folder = env["settings"].backup_dir / "srv" / "production"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "db_1.zip").write_bytes(b"z")
    url = "/api/servers/srv/production/backups/delete"
    # a developer does not delete a production's backups; nothing outside the folder
    assert (await client.post(url, headers=post_as("dev"), json={"file": "db_1.zip"})).status_code == 403
    for bad in ("../x.zip", "db_2.zip", "x.txt"):
        assert (await client.post(url, headers=POST, json={"file": bad})).status_code == 404, bad
    resp = await client.post(url, headers=POST, json={"file": "db_1.zip"})
    assert resp.status_code == 200 and not (folder / "db_1.zip").exists()
    entry = next(e for e in client.app.state.access_log.recent() if e["event"] == "delete backup")
    assert entry["detail"] == "srv/production/db_1.zip"
    # a test's: a developer's
    test_folder = env["settings"].backup_dir / "srv" / "test"
    test_folder.mkdir(parents=True, exist_ok=True)
    (test_folder / "db_2.zip").write_bytes(b"z")
    resp = await client.post("/api/servers/srv/test/backups/delete", headers=post_as("dev"),
                             json={"file": "db_2.zip"})
    assert resp.status_code == 200, resp.text
