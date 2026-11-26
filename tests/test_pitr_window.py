# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The recovery at a minute of a workspace's production, as Carlo asked on
26/11/2026 (ber's odoo19: a 7.8 MB database, 9.11 GB of archive): the
archive compressed (written whole under another name, then renamed, never
over one that is there) and an archive of before still read back; a window
of days chosen per instance (1 to 35, 7 when none), kept by the base job's
retention; switched off without a restart and on again; the room of its
disk for its days (it only warns); the page, the REST API and MCP on the
same code, each change in the access log."""

import datetime
import gzip
import os
import subprocess
from pathlib import Path

import pytest

from omniacli import pitrhelper
from omniacli.composehelper import HelperError

from .test_app import client  # noqa: F401 - the client fixture
from .test_pitr import FakeDocker, project
from .test_workspaces import workspaces  # noqa: F401 - the workspaces fixture

OLD_COMMAND = f"test ! -f {pitrhelper.WAL_MOUNT}/archive/%f && cp %p {pitrhelper.WAL_MOUNT}/archive/%f"


class Shown(FakeDocker):
    """FakeDocker whose PostgreSQL answers SHOW with what it was told."""

    def __init__(self, monkeypatch, **shown):
        super().__init__(monkeypatch)
        self.shown = {"archive_mode": "off", "archive_command": "(disabled)", **shown}

    def dc(self, project, *args, check=True, input=None):
        done = super().dc(project, *args, check=check, input=input)
        sql = (input or "").strip()
        if "psql" in args and sql.startswith("SHOW "):
            done.stdout = self.shown.get(sql[5:].rstrip(";"), "")
        return done


def run_command(command: str, **values) -> int:
    """An archive_command or restore_command as PostgreSQL runs it: %p, %f put in, through sh."""
    for key, value in values.items():
        command = command.replace(f"%{key}", value)
    return subprocess.run(["sh", "-c", command], capture_output=True).returncode


def test_the_archive_is_compressed_whole_and_never_written_over(tmp_path):
    archive, wal = tmp_path / "archive", tmp_path / "pg_wal"
    archive.mkdir()
    wal.mkdir()
    name = "000000010000000000000003"
    segment = wal / name
    # a segment as archive_timeout leaves it: a little work, the rest zeros
    segment.write_bytes(os.urandom(4096) + bytes(16 * 1024 * 1024 - 4096))
    command = pitrhelper.archive_command(str(archive))
    assert run_command(command, p=str(segment), f=name) == 0
    kept = archive / (name + ".gz")
    assert sorted(p.name for p in archive.iterdir()) == [kept.name]
    assert gzip.decompress(kept.read_bytes()) == segment.read_bytes()
    assert kept.stat().st_size < 1024 * 1024
    # archived again after a crash: the same is fine, another one is a failure and the first stays
    assert run_command(command, p=str(segment), f=name) == 0
    before = kept.read_bytes()
    other = wal / "other"
    other.write_bytes(b"x" * 100)
    assert run_command(command, p=str(other), f=name) != 0
    assert kept.read_bytes() == before
    # one archived whole by the code of before: the same is fine, never written again compressed
    plain = "000000010000000000000004"
    (archive / plain).write_bytes(b"w" * 100)
    (wal / plain).write_bytes(b"w" * 100)
    assert run_command(command, p=str(wal / plain), f=plain) == 0
    assert not (archive / (plain + ".gz")).exists()
    # a segment that cannot be read: a failure, nothing left under the segment's name
    assert run_command(command, p=str(wal / "missing"), f="000000010000000000000005") != 0
    assert not (archive / "000000010000000000000005.gz").exists()


def test_a_restore_reads_both_names(tmp_path):
    folder, out = tmp_path / "restore", tmp_path / "out"
    folder.mkdir()
    out.mkdir()
    (folder / "000000010000000000000003.gz").write_bytes(gzip.compress(b"new"))
    (folder / "000000010000000000000002").write_bytes(b"old")
    command = pitrhelper.restore_command(str(folder))
    assert run_command(command, f="000000010000000000000003", p=str(out / "a")) == 0
    assert run_command(command, f="000000010000000000000002", p=str(out / "b")) == 0
    assert (out / "a").read_bytes() == b"new" and (out / "b").read_bytes() == b"old"
    # one that is not there: a failure, as PostgreSQL expects at the end of the archive
    assert run_command(command, f="000000010000000000000009", p=str(out / "c")) != 0


def test_switched_on_compressed_and_an_old_archive_made_compressed_by_the_base(tmp_path, monkeypatch):
    docker = Shown(monkeypatch)
    src = project(tmp_path, "odoo")
    found = pitrhelper.enable(src)
    assert f"ALTER SYSTEM SET archive_command = '{pitrhelper.archive_command()}'" in docker.sql[1]
    assert found["restarted"] and ("odoo", ("restart", "db")) in docker.calls
    # an instance switched on with the code of before: the base gives it the new command, reloaded, no restart
    docker.calls.clear()
    docker.sql.clear()
    docker.shown.update(archive_mode="on", archive_command=OLD_COMMAND)
    made = pitrhelper.base(src)
    assert made["upgraded"] and made["days"] == pitrhelper.KEEP_DAYS
    assert any(pitrhelper.archive_command() in q and "pg_reload_conf" in q for q in docker.sql)
    assert not any("restart" in args for _, args in docker.calls)
    docker.shown["archive_command"] = pitrhelper.archive_command()
    docker.sql.clear()
    assert not pitrhelper.base(src)["upgraded"] and not any("ALTER SYSTEM" in q for q in docker.sql if q)


def test_the_window_of_days(tmp_path, monkeypatch):
    Shown(monkeypatch, archive_mode="on", archive_command=pitrhelper.archive_command())
    src = project(tmp_path, "odoo")
    pitrhelper.enable(src)
    bases, archive = src / "data" / "wal" / "base", src / "data" / "wal" / "archive"
    for folder in bases.iterdir():
        for f in folder.iterdir():
            f.unlink()
        folder.rmdir()
    now = datetime.datetime.now()
    for day in range(1, 11):
        folder = bases / (now - datetime.timedelta(days=day, hours=1)).strftime(pitrhelper.STAMP)
        folder.mkdir()
        (folder / "base.tar.gz").write_bytes(b"x")
    # a segment of each day, compressed or not (an archive of before)
    for day in range(0, 12):
        name = f"0000000100000000000000{day:02X}" + (".gz" if day % 2 else "")
        (archive / name).write_bytes(b"w")
        stamp = (now - datetime.timedelta(days=day)).timestamp()
        os.utime(archive / name, (stamp, stamp))
    done = pitrhelper.retention(src, 3)
    # the bases of the last 3 days and the newest one before them: every minute of the 3 days can be reached
    kept = sorted(datetime.datetime.strptime(p.name, pitrhelper.STAMP) for p in bases.iterdir())
    assert len(kept) == 3 and kept[0] < now - datetime.timedelta(days=3) <= kept[1]
    assert done == {"bases": 7, "segments": 8, "kept": 3}
    assert sorted(p.name[22:24] for p in archive.iterdir()) == ["00", "01", "02", "03"]
    # none in the window (the base job failed for days): the newest stays, with what follows it
    pitrhelper.retention(src, 1, now + datetime.timedelta(days=5))
    assert len(list(bases.iterdir())) == 1
    for value in (0, 36, "x", "3.5"):
        with pytest.raises(HelperError):
            pitrhelper.days_of(value)
    assert pitrhelper.days_of(None) == 7 and pitrhelper.days_of("35") == 35


def test_the_state_counts_both_names(tmp_path, monkeypatch):
    Shown(monkeypatch)
    src = project(tmp_path, "odoo")
    pitrhelper.enable(src)
    archive = src / "data" / "wal" / "archive"
    for name, size in (("000000010000000000000002", 16), ("000000010000000000000003.gz", 3),
                       ("000000010000000000000004.gz.part", 5), ("000000010000000000000003.00000028.backup", 1),
                       ("00000002.history.gz", 1)):
        (archive / name).write_bytes(b"w" * size)
    found = pitrhelper.state(src)
    assert found["segments"] == 2 and found["compressed"] == 1
    assert found["archive_size"] == 16 + 3 + 1 + 1
    assert found["size"] == found["archive_size"] + found["bases_size"] and found["free"] > 0
    assert pitrhelper.wal_name("000000010000000000000003.gz") == "000000010000000000000003"
    assert pitrhelper.wal_name("00000002.history.gz") == "00000002.history"
    assert pitrhelper.wal_name("000000010000000000000004.gz.part") is None


def test_switched_off_and_on_again(tmp_path, monkeypatch):
    docker = Shown(monkeypatch)
    src = project(tmp_path, "odoo")
    pitrhelper.enable(src)
    archive = src / "data" / "wal" / "archive"
    (archive / "000000010000000000000002.gz").write_bytes(b"w" * 10)
    docker.sql.clear()
    docker.calls.clear()
    done = pitrhelper.disable(src)
    assert f"archive_command = '{pitrhelper.OFF_COMMAND}'" in docker.sql[0] and "pg_reload_conf" in docker.sql[0]
    assert "RESET archive_mode" in docker.sql[0]
    assert not any(args[0] in ("restart", "up", "stop") for _, args in docker.calls)
    assert done["freed"] > 10 and not pitrhelper.enabled(src)
    assert pitrhelper.WAL_LINE not in (src / "compose.yaml").read_text()
    assert not list(archive.iterdir()) and not list((src / "data" / "wal" / "base").iterdir())
    # the folder stays: a database still running with the mount keeps pointing at it
    assert archive.is_dir()
    assert pitrhelper.state(src) == {"enabled": False}
    # on again before the database restarted: archive_mode is still on, a reload is enough
    docker.calls.clear()
    docker.shown["archive_mode"] = "on"
    found = pitrhelper.enable(src)
    assert found["enabled"] and not found["restarted"] and ("odoo", ("restart", "db")) not in docker.calls
    assert (src / "compose.yaml").read_text().count(pitrhelper.WAL_LINE) == 1


def test_not_switched_off_under_a_restore(tmp_path, monkeypatch):
    Shown(monkeypatch)
    src = project(tmp_path, "odoo")
    pitrhelper.enable(src)
    with pitrhelper._lock(src, wait=True):
        with pytest.raises(HelperError, match="reading the archive"):
            pitrhelper.disable(src)
        # the base's retention waits for another night: nothing deleted under it
        assert pitrhelper.base(src)["pruned"]["skipped"]
    assert pitrhelper.enabled(src)


def test_the_room_for_the_days():
    now = datetime.datetime(2026, 11, 26, 12, 0)
    gb = 1 << 30
    found = {"enabled": True, "bases": [{"time": "2026-11-20T12:00:00", "size": 8 << 20},
                                        {"time": "2026-11-26T03:00:00", "size": 8 << 20}],
             "archive_size": 9 * gb, "size": 9 * gb + (16 << 20), "free": 20 * gb}
    room = pitrhelper.room(found, 7, now)
    assert room["per_day"] == int(9 * gb / 6)
    assert room["expected"] == room["per_day"] * 7 + (8 << 20) * 8 and room["fits"]
    room = pitrhelper.room(found, 35, now)
    assert not room["fits"] and room["need"] == room["expected"] - found["size"]
    # a range of less than an hour: not measurable yet, no warning
    young = {**found, "bases": [{"time": "2026-11-26T11:30:00", "size": 1}]}
    assert pitrhelper.room(young, 35, now)["fits"] and pitrhelper.room(young, 35, now)["per_day"] is None
    assert pitrhelper.room({"enabled": False}, 7, now)["fits"]


async def test_the_page_the_rest_api_and_mcp(workspaces, client, env, monkeypatch, tmp_path):
    import json

    from omniacli import ops, stacktemplates, workspaceops

    from .test_api_v1 import V1, error_of, token
    from .test_app import wait_job
    from .test_mcp import HEADERS, call
    from .test_workspaces import post_as

    settings = env["settings"]
    template = stacktemplates.load_template(Path(__file__).parent.parent / "templates" / "odoo-19")
    monkeypatch.setattr(workspaceops, "templates", lambda: {template.name: template})
    settings.storage_roots = {"main": str(tmp_path)}
    ws = settings.workspaces["acme"]
    ws.storage, ws.limits = "main", {"disk_gb": 50}
    monkeypatch.setattr(workspaceops, "storage_path", lambda w: tmp_path)
    (tmp_path / "acme" / "odoo").mkdir(parents=True)
    (tmp_path / "acme" / "odoo" / "instance.json").write_text(json.dumps({"template": "odoo-19"}))

    async def create_instance(job, w, instance, tpl, values, setup=None):
        return {"type": "docker", **{role: {
            "database": instance.replace("-", "_"), "odoo_service": "odoo", "db_service": "db",
            "http_port": 20000 + i, "update_modules": True,
            "restart": {"type": "compose", "service": str(tmp_path / instance / role), "sudo": True},
            "repositories": []} for i, role in enumerate(("production", "test"))}}

    async def nothing(*args, **kwargs):
        return {}
    helper = []
    gb = 1 << 30
    now = datetime.datetime.now()
    archive = {"enabled": True, "bases": [{"name": "b1", "time": (now - datetime.timedelta(days=6)).isoformat(
        timespec="seconds"), "size": 8 << 20}], "first": (now - datetime.timedelta(days=6)).isoformat(timespec="seconds"),
        "last": now.isoformat(timespec="seconds"), "segments": 589, "archive_size": 9 * gb, "size": 9 * gb + (8 << 20),
        "free": gb, "failed": 0}

    # on or off as the instance's database is: what pitr-state answers
    on = {"now": False}

    async def fake_helper(job, *args, timeout=None):
        helper.append(args)
        if args[0] == "pitr-disable":
            on["now"] = False
            return {"enabled": False, "freed": 9 * gb}
        if args[0] == "pitr-enable":
            on["now"] = True
        if args[0] == "pitr-base":
            return {"base": "20261126T030000", "size": 8 << 20, "days": int(args[5]), "upgraded": True,
                    "pruned": {"bases": 1, "segments": 40, "kept": 7}, "state": archive}
        return {"base": "20261126T030000", "restarted": True}

    async def ask(*args, timeout=None):
        return archive if on["now"] else {"enabled": False}
    monkeypatch.setattr(workspaceops, "create_instance", create_instance)
    monkeypatch.setattr(workspaceops, "publish", nothing)
    monkeypatch.setattr(workspaceops, "_helper", fake_helper)
    monkeypatch.setattr(workspaceops, "ask", ask)
    monkeypatch.setattr(ops, "run", nothing)
    monkeypatch.setattr(ops, "stop", nothing)
    monkeypatch.setattr(ops, "restart", nothing)
    resp = await client.post("/api/workspaces/acme/instances/create", headers=post_as("mario"),
                             json={"instance": "odoo", "template": "odoo-19"})
    assert (await wait_job(client, resp.json()["job"]))["status"] == "success"
    pitr_file = settings.servers_file.parent / "pitr.json"
    api = "/api/servers/acme-odoo/pitr"

    def record():
        return json.loads(pitr_file.read_text()).get("acme-odoo") if pitr_file.exists() else None

    # off in its record: no days to set
    resp = await client.post(f"{api}/settings", headers=post_as("mario"), json={"days": 14})
    assert resp.status_code == 409 and "switch it on first" in resp.json()["error"]
    resp = await client.post(f"{api}/enable", headers=post_as("mario"))
    assert (await wait_job(client, resp.json()["job"]))["status"] == "success"
    since = record()["since"]
    assert since and "days" not in record()
    # the state: since when, 7 days when none was chosen, what it takes and the room for it
    data = (await client.get(api, headers=post_as("mario"))).json()
    assert data["days"] == 7 and data["since"] == since and data["size"] == 9 * gb + (8 << 20)
    assert data["room"]["fits"] is False and abs(data["room"]["per_day"] - 9 * gb / 6) < gb / 100
    # the days: a whole number of 1 to 35, by its admins
    for wrong in (0, 36, "x", 3.5, True, None):
        resp = await client.post(f"{api}/settings", headers=post_as("mario"), json={"days": wrong})
        assert resp.status_code == 400, wrong
    assert (await client.post(f"{api}/settings", headers=post_as("luca"), json={"days": 14})).status_code == 403
    resp = await client.post(f"{api}/settings", headers=post_as("mario"), json={"days": 14})
    assert resp.json() == {"days": 14, "before": 7} and record()["days"] == 14 and record()["since"] == since
    # the base job keeps those days, makes an old archive compressed and warns of the room, by itself never shortened
    resp = await client.post(f"{api}/base", headers=post_as("mario"))
    job = await wait_job(client, resp.json()["job"])
    assert helper[-1] == ("pitr-base", "acme", "main", "odoo", "production", "14")
    text = json.dumps(job)
    assert "kept compressed from now" in text and "The last 14 days kept" in text
    assert "Keeping the last 14 days of acme-odoo/production is expected" in text and record()["days"] == 14
    # the page: the switch off and the days, for an admin of acme
    page = (await client.get("/servers/acme-odoo", headers=post_as("mario"))).text
    assert 'id="pitr-disable"' in page and 'id="pitr-days"' in page and 'class="banner warn pitr-room"' in page
    # off: a job, the record gone, nothing more to set
    resp = await client.post(f"{api}/disable", headers=post_as("mario"))
    assert (await wait_job(client, resp.json()["job"]))["status"] == "success"
    assert helper[-1] == ("pitr-disable", "acme", "main", "odoo", "production") and record() is None
    assert (await client.post(f"{api}/disable", headers=post_as("mario"))).status_code == 409
    said = [e["detail"] for e in client.app.state.access_log.recent() if "recovery at a minute of" in e["detail"]]
    assert any("switched off" in s for s in said) and any("14 days kept (they were 7)" in s for s in said)

    # the REST API: the same code, a production by hand
    admin = await token(client, "boss", "admin")
    dev = await token(client, "devtok", "developer")
    found = (await client.get(f"{V1}/instances/acme-odoo/production/pitr", headers=dev)).json()
    assert found["enabled"] is False, found
    error_of(await client.post(f"{V1}/instances/acme-odoo/production/pitr/enable", headers=admin, json={}), 400,
             "confirm_required")
    error_of(await client.post(f"{V1}/instances/acme-odoo/production/pitr/enable", headers=dev, json={"confirm": "acme-odoo"}),
             403, "production_level_required")
    resp = await client.post(f"{V1}/instances/acme-odoo/production/pitr/enable", headers=admin, json={"confirm": "acme-odoo"})
    assert resp.status_code == 202 and helper[-1][0] == "pitr-enable"
    assert (await wait_job(client, resp.json()["job"]["id"]))["status"] == "success"
    assert (await client.get(f"{V1}/instances/acme-odoo/production/pitr", headers=dev)).json()["days"] == 7
    error_of(await client.post(f"{V1}/instances/acme-odoo/production/pitr/settings", headers=admin, json={"days": 40}), 400,
             "refused")
    done = (await client.post(f"{V1}/instances/acme-odoo/production/pitr/settings", headers=admin, json={"days": 10})).json()
    assert done["days"] == 10 and record()["days"] == 10
    resp = await client.post(f"{V1}/instances/acme-odoo/production/pitr/disable", headers=admin, json={"confirm": "acme-odoo"})
    assert (await wait_job(client, resp.json()["job"]["id"]))["status"] == "success" and record() is None

    # MCP: the same code again
    found = await call(client, "pitr_status", headers={**HEADERS, **admin}, server_name="acme-odoo")
    assert found["enabled"] is False
    found = await call(client, "pitr_enable", headers={**HEADERS, **admin}, server_name="acme-odoo")
    assert "confirm" in json.dumps(found)
    found = await call(client, "pitr_enable", headers={**HEADERS, **admin}, server_name="acme-odoo",
                       confirm="acme-odoo", wait_seconds=30)
    assert found["status"] == "success" and record()["since"]
    found = await call(client, "pitr_status", headers={**HEADERS, **admin}, server_name="acme-odoo")
    assert found["days"] == 7 and found["room"]["fits"] is False
    found = await call(client, "set_pitr_days", headers={**HEADERS, **admin}, server_name="acme-odoo", days=21)
    assert found["days"] == 21 and record()["days"] == 21
    found = await call(client, "set_pitr_days", headers={**HEADERS, **dev}, server_name="acme-odoo", days=3)
    assert "production_level_required" in json.dumps(found) and record()["days"] == 21
    found = await call(client, "pitr_disable", headers={**HEADERS, **admin}, server_name="acme-odoo",
                       confirm="acme-odoo", wait_seconds=30)
    assert found["status"] == "success" and record() is None

    # digits int() refuses: a refusal, never an error of the server
    resp = await client.post(f"{api}/enable", headers=post_as("mario"))
    assert (await wait_job(client, resp.json()["job"]))["status"] == "success"
    for wrong in ("²", "①", "1²", "", " "):
        resp = await client.post(f"{api}/settings", headers=post_as("mario"), json={"days": wrong})
        assert resp.status_code == 400 and "whole number" in resp.json()["error"], wrong

    # on without its record (its first full copy failed after the database archived, or a promotion took the
    # record away): it is what the database does that counts, and the record is made again for the nightly loop
    def drop_record():
        data = json.loads(pitr_file.read_text())
        data.pop("acme-odoo", None)
        pitr_file.write_text(json.dumps(data))
    drop_record()
    resp = await client.post(f"{api}/settings", headers=post_as("mario"), json={"days": 5})
    assert resp.status_code == 200, resp.text
    assert record()["days"] == 5 and record()["enabled"]
    drop_record()
    data = (await client.get(api, headers=post_as("mario"))).json()
    assert data["enabled"] and data["days"] == 7 and record() == {"enabled": True}
    drop_record()
    resp = await client.post(f"{api}/disable", headers=post_as("mario"))
    assert resp.status_code == 200, resp.text
    assert (await wait_job(client, resp.json()["job"]))["status"] == "success"
    assert helper[-1][0] == "pitr-disable" and record() is None
    # off for the database too: nothing to switch off, no days to set, no record made
    assert (await client.post(f"{api}/disable", headers=post_as("mario"))).status_code == 409
    assert (await client.post(f"{api}/settings", headers=post_as("mario"), json={"days": 5})).status_code == 409
    assert record() is None


def test_switched_off_with_its_database_stopped(tmp_path, monkeypatch):
    """A stopped database is not asked by psql: its postgresql.auto.conf loses the archive's settings, so it
    starts with archive_mode off; the rest as with it running."""
    docker = Shown(monkeypatch)
    src = project(tmp_path, "odoo")
    pitrhelper.enable(src)
    auto = src / "data" / "pgdata" / "postgresql.auto.conf"
    auto.write_text("# Do not edit this file manually!\nshared_buffers = '256MB'\narchive_mode = 'on'\n"
                    f"archive_command = '{pitrhelper.archive_command()}'\narchive_timeout = '300'\n")
    docker.sql.clear()
    docker.crash = False
    real = docker.dc

    def stopped(project, *args, check=True, input=None):
        if "pg_isready" in args:
            return type("Done", (), {"returncode": 2, "stdout": "", "stderr": "no response"})
        if "psql" in args:
            raise HelperError("the database is not running")
        return real(project, *args, check=check, input=input)
    monkeypatch.setattr(pitrhelper, "_dc", stopped)
    done = pitrhelper.disable(src)
    assert done["database"] == "stopped" and not docker.sql
    assert auto.read_text() == "# Do not edit this file manually!\nshared_buffers = '256MB'\n"
    assert not pitrhelper.enabled(src) and not list((src / "data" / "wal" / "archive").iterdir())


def test_the_rename_is_synced_with_its_folder():
    """After the rename the folder is synced (coreutils' sync of a directory, in both images of the
    templates), and a failure of that sync is the command's failure."""
    command = pitrhelper.archive_command("/var/lib/postgresql/wal/archive")
    assert command.endswith("mv /var/lib/postgresql/wal/archive/%f.gz.part /var/lib/postgresql/wal/archive/%f.gz && "
                            "sync /var/lib/postgresql/wal/archive; fi")
