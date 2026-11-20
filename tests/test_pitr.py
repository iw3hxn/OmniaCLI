# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Point-in-time recovery: the WAL volume added to the database service,
archive_mode on, base backups kept and pruned, a cluster made again at a
minute (the base before it, the segments after it, the recovery settings),
the target's data put back when it fails; the pages and the staging."""

import datetime
import io
import os
import tarfile
import time
from pathlib import Path

import pytest

from omniacli import pitrhelper
from omniacli.composehelper import HelperError

from .test_app import client  # noqa: F401 - the client fixture
from .test_workspaces import workspaces  # noqa: F401 - the workspaces fixture

COMPOSE = """services:
  db:
    image: postgres:17
    volumes:
      - ./data/pgdata:/var/lib/postgresql/data/pgdata
      - ./dbinit:/docker-entrypoint-initdb.d:ro
"""


def project(base: Path, name: str) -> Path:
    folder = base / name
    (folder / "data" / "pgdata").mkdir(parents=True)
    (folder / "data" / "pgdata" / "PG_VERSION").write_text("17\n")
    (folder / "compose.yaml").write_text(COMPOSE)
    (folder / ".env").write_text("DB_PASSWORD=pw'1\nDB_SUPER_PASSWORD=super\n")
    return folder


class FakeDocker:
    """docker compose as the helper calls it: pg_basebackup writes a base,
    psql answers, the rest is recorded."""

    def __init__(self, monkeypatch):
        self.calls, self.sql, self.fail_on = [], [], None
        monkeypatch.setattr(pitrhelper, "_dc", self.dc)
        monkeypatch.setattr(pitrhelper.os, "chown", lambda *a, **k: None)

    def dc(self, project, *args, check=True, input=None):
        self.calls.append((project.name, args))
        if self.fail_on and self.fail_on in args:
            raise HelperError(f"{self.fail_on} failed")
        out = ""
        if "pg_basebackup" in args:
            target = project / "data" / "wal" / "base" / args[args.index("-D") + 1].rsplit("/", 1)[1]
            target.mkdir(parents=True)
            data = io.BytesIO()
            with tarfile.open(fileobj=data, mode="w:gz") as tar:
                info = tarfile.TarInfo("PG_VERSION")
                info.size = 3
                tar.addfile(info, io.BytesIO(b"17\n"))
            (target / "base.tar.gz").write_bytes(data.getvalue())
        if "psql" in args:
            self.sql.append(input)
            out = "f" if "pg_is_in_recovery" in (input or "") else ""
        return type("Done", (), {"returncode": 0, "stdout": out, "stderr": ""})


def test_switched_on(tmp_path, monkeypatch):
    docker = FakeDocker(monkeypatch)
    src = project(tmp_path, "odoo")
    found = pitrhelper.enable(src)
    text = (src / "compose.yaml").read_text()
    assert f"      - {pitrhelper.WAL_LINE}\n" in text and text.index(pitrhelper.WAL_LINE) > text.index("pgdata")
    assert found["enabled"] and len(found["bases"]) == 1
    assert "archive_mode = 'on'" in docker.sql[0] and f"{pitrhelper.WAL_MOUNT}/archive/%f" in docker.sql[0]
    # the service made again, then restarted for archive_mode
    assert ("odoo", ("up", "-d", "db")) in docker.calls and ("odoo", ("restart", "db")) in docker.calls
    # again: one line only
    pitrhelper.enable(src)
    assert (src / "compose.yaml").read_text().count(pitrhelper.WAL_LINE) == 1
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "compose.yaml").write_text("services: {}\n")
    with pytest.raises(HelperError, match="does not mount"):
        pitrhelper.enable(tmp_path / "bad")


def test_the_bases_kept_and_the_old_segments(tmp_path, monkeypatch):
    FakeDocker(monkeypatch)
    src = project(tmp_path, "odoo")
    pitrhelper.enable(src)
    archive = src / "data" / "wal" / "archive"
    old = archive / ("0" * 23 + "1")
    old.write_bytes(b"w")
    os.utime(old, (time.time() - 30 * 86400,) * 2)
    bases = src / "data" / "wal" / "base"
    for day in range(1, 10):
        folder = bases / f"202611{day:02d}T030000"
        folder.mkdir()
        (folder / "base.tar.gz").write_bytes(b"x")
        os.utime(folder, (time.time() - (20 - day) * 86400,) * 2)
    pitrhelper.base(src)
    kept = sorted(p.name for p in bases.iterdir())
    assert len(kept) == pitrhelper.KEEP_BASES and "20261101T030000" not in kept
    assert not old.exists()


def test_a_cluster_at_a_minute(tmp_path, monkeypatch):
    docker = FakeDocker(monkeypatch)
    src, dst = project(tmp_path, "odoo"), project(tmp_path, "odoo-stg")
    pitrhelper.enable(src)
    base = next((src / "data" / "wal" / "base").iterdir())
    when = datetime.datetime.strptime(base.name, pitrhelper.STAMP)
    os.utime(base, (when.timestamp(),) * 2)
    segment = src / "data" / "wal" / "archive" / ("0" * 23 + "2")
    segment.write_bytes(b"wal")
    later = when + datetime.timedelta(minutes=10)
    os.utime(segment, (later.timestamp(),) * 2)
    # before the first base, after the last segment: refused, nothing touched
    with pytest.raises(HelperError, match="no base backup before"):
        pitrhelper.restore(src, dst, (when - datetime.timedelta(hours=1)).isoformat(), "odoo", "odoo_stg")
    with pytest.raises(HelperError, match="after the last archived segment"):
        pitrhelper.restore(src, dst, (later + datetime.timedelta(hours=1)).isoformat(), "odoo", "odoo_stg")
    assert (dst / "data" / "pgdata" / "PG_VERSION").exists()
    at = (when + datetime.timedelta(minutes=5)).isoformat(timespec="minutes")
    found = pitrhelper.restore(src, dst, at, "odoo", "odoo_stg")
    pgdata = dst / "data" / "pgdata"
    assert found["segments"] == 1 and (pgdata / "pg_wal" / segment.name).read_bytes() == b"wal"
    auto = (pgdata / "postgresql.auto.conf").read_text()
    assert "recovery_target_time" in auto and "archive_mode = 'off'" in auto and (pgdata / "recovery.signal").exists()
    assert not (dst / "data" / "pgdata.omniacli-before-restore").exists()
    # renamed as the target names it; the target's passwords, quoted
    assert any('ALTER DATABASE "odoo" RENAME TO "odoo_stg"' in (q or "") for q in docker.sql)
    assert any("PASSWORD 'pw''1'" in (q or "") for q in docker.sql)
    # a failure: the target's data of before put back
    marker = pgdata / "MINE"
    marker.write_text("before")
    docker.fail_on = "pg_isready"
    with pytest.raises(HelperError):
        pitrhelper.restore(src, dst, at, "odoo", "odoo_stg")
    assert marker.read_text() == "before"
    with pytest.raises(HelperError, match="not a database name"):
        pitrhelper.restore(src, dst, at, "odoo; DROP", "odoo_stg")


async def test_the_pages_and_a_staging_at_a_minute(workspaces, client, env, monkeypatch, tmp_path):
    import json

    from omniacli import ops, stacktemplates, stagings, workspaceops

    from .test_app import POST, wait_job
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
    ports = iter(range(20000, 20100, 2))

    async def create_instance(job, w, instance, tpl, values, setup=None):
        port = next(ports)
        return {"type": "docker", **{role: {
            "database": instance.replace("-", "_"), "odoo_service": "odoo", "db_service": "db", "http_port": port + i,
            "update_modules": True, "restart": {"type": "compose", "service": str(tmp_path / instance / role), "sudo": True},
            "repositories": []} for i, role in enumerate(("production", "test"))}}

    async def nothing(*args, **kwargs):
        return {}
    helper, ran = [], []

    async def fake_helper(job, *args, timeout=None):
        helper.append(args)
        return {"base": "20261120T030000", "segments": 4, "size": 1 << 20}

    async def ask(*args, timeout=None):
        return {"enabled": True, "bases": [{"name": "20261120T030000", "time": "2026-11-20T03:00:00", "size": 1}],
                "first": "2026-11-20T03:00:00", "last": "2026-11-20T09:40:00", "segments": 4, "archive_size": 64,
                "failed": 0}

    async def run(job, cmd, **kwargs):
        ran.append(cmd)
        return 0
    monkeypatch.setattr(workspaceops, "create_instance", create_instance)
    monkeypatch.setattr(workspaceops, "publish", nothing)
    monkeypatch.setattr(workspaceops, "_helper", fake_helper)
    monkeypatch.setattr(workspaceops, "ask", ask)
    monkeypatch.setattr(ops, "run", run)
    monkeypatch.setattr(ops, "stop", nothing)
    monkeypatch.setattr(ops, "restart", nothing)
    resp = await client.post("/api/workspaces/acme/instances/create", headers=post_as("mario"),
                             json={"instance": "odoo", "template": "odoo-19"})
    assert (await wait_job(client, resp.json()["job"]))["status"] == "success"
    helper.clear()
    # srv was not made in a workspace
    assert (await client.get("/api/servers/srv/pitr", headers=POST)).status_code == 404
    assert (await client.get("/api/servers/acme-odoo/pitr", headers=post_as("mario"))).json()["enabled"]
    resp = await client.post("/api/servers/acme-odoo/pitr/enable", headers=post_as("mario"))
    assert (await wait_job(client, resp.json()["job"]))["status"] == "success"
    assert helper[-1][0] == "pitr-enable" and helper[-1][1:] == ("acme", "main", "odoo", "production")
    # a user of acme who is no admin there: refused
    assert (await client.post("/api/servers/acme-odoo/pitr/enable", headers=post_as("luca"))).status_code == 403
    resp = await client.post("/api/servers/acme-odoo/pitr/restore", headers=post_as("mario"), json={"time": "yesterday"})
    assert resp.status_code == 400
    resp = await client.post("/api/servers/acme-odoo/pitr/restore", headers=post_as("mario"),
                             json={"time": "2026-11-20T09:31"})
    assert resp.status_code == 200, resp.text
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success", job["lines"]
    staging = resp.json()["server"]
    restore = next(a for a in helper if a[0] == "pitr-restore")
    assert restore[1:] == ("acme", "main", "odoo", staging[len("acme-"):], "2026-11-20T09:31", "odoo",
                           staging[len("acme-"):].replace("-", "_"))
    # the filestore of now linked, then neutralized
    assert any("link-filestore" in c for c in ran) and any("neutralize" in c for c in ran)
    record = stagings.load((settings.links_file or settings.servers_file).parent / stagings.FILE)
    assert record[staging]["pitr"] == "2026-11-20T09:31" and record[staging]["parent"] == "acme-odoo"
