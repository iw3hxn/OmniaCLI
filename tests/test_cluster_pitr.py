# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Recovery at a minute of the machine's PostgreSQL cluster (AD-006): the
archive switched on (the restart asked) and off, a base backup with its
tablespaces and the retention, the budget, one database at a minute through
a temporary cluster that is gone whatever happens (the end, a failure, a
stop, what a kill left), the room refused; the pages: the machine's admin's
Database tab, the instance's page for a workspace's user, who learns nothing
of the others."""

import datetime
import fcntl
import io
import json
import os
import re
import tarfile
from pathlib import Path

import pytest

from omniacli import clusterpitr, machinedb, ops, pgtuning, stacktemplates, stagings, workspacehelper, workspaceops
from omniacli.composehelper import HelperError
from omniacli.jobs import JobFailed

from .test_app import AUTH, POST, client, wait_job  # noqa: F401 - the client fixture
from .test_workspaces import basic, post_as, workspaces  # noqa: F401 - the workspaces fixture

TABLESPACE = "16390"


def _tar(members: dict[str, bytes]) -> bytes:
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as tar:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return data.getvalue()


class Cluster:
    """The machine's cluster as clusterpitr reaches it: its SQL answered,
    docker compose and docker recorded; pg_basebackup writes a base with a
    tablespace, the temporary container is promoted at once."""

    def __init__(self, tmp_path, monkeypatch):
        self.root = tmp_path / "storage"
        self.project = self.root / ".machine-db" / "pg17"
        (self.project / "data" / "pgdata").mkdir(parents=True)
        (self.project / "secret").mkdir()
        (self.project / "conf").mkdir()
        uid, gid = os.getuid(), os.getgid()
        (self.project / "compose.yaml").write_text(machinedb.compose_text("17", uid, gid, [str(self.root)]))
        self.mounts = {"acme": self.root / "acme", "beta": self.root / "beta"}
        for mount in self.mounts.values():
            (mount / machinedb.WS_FOLDER / "ts").mkdir(parents=True)
        (self.project / "environments.json").write_text(json.dumps({
            "acme_mach": {"workspace": "acme", "instance": "mach", "mount": str(self.mounts["acme"])},
            "acme_mach_stg": {"workspace": "acme", "instance": "mach-stg", "mount": str(self.mounts["acme"])},
            "beta_shop": {"workspace": "beta", "instance": "shop", "mount": str(self.mounts["beta"])}}))
        registry = tmp_path / "machine-db.json"
        registry.write_text(json.dumps({"17": {"project": str(self.project), "storage": "default"}}))
        monkeypatch.setattr(machinedb, "REGISTRY", registry)
        # the folder of a test has characters a storage's path never has
        monkeypatch.setattr(machinedb, "SAFE_PATH", re.compile(r"/[A-Za-z0-9._/-]+"))
        monkeypatch.setattr(machinedb, "pg_user", lambda create=False: (uid, gid))
        monkeypatch.setattr(machinedb, "_docker", lambda: "docker")
        monkeypatch.setattr(os, "chown", lambda *a, **k: None)
        monkeypatch.setattr(os, "fchown", lambda *a, **k: None)
        monkeypatch.setattr(machinedb, "sql", self.sql)
        monkeypatch.setattr(machinedb, "_run", self.compose)
        monkeypatch.setattr(clusterpitr, "_docker", self.docker)
        monkeypatch.setattr(clusterpitr.time, "sleep", lambda s: None)
        monkeypatch.setattr(clusterpitr, "READY_TIMEOUT", 0)
        self.archive_mode, self.archive_command, self.starts = "off", "", 0
        self.statements, self.composed, self.dockered = [], [], []
        self.segment = 0x11
        self.states, self.log, self.present, self.left = [], "", True, []
        self.fail_restore = False
        self.on_inspect = None
        self.size = 150 * clusterpitr.MB

    # --- what PostgreSQL answers
    def sql(self, project, database, statements):
        self.statements.append((database, statements))
        if "SHOW archive_mode" in statements:
            return self.archive_mode + "\n"
        if "SHOW archive_command" in statements:
            return self.archive_command + "\n"
        if "ALTER SYSTEM SET archive_command" in statements:
            self.archive_command = re.search(r"archive_command = '([^']*)'", statements).group(1)
        if "pg_current_wal_lsn" in statements:
            return f"{self.segment:024X}|{self.size}\n"
        if "txid_current" in statements:
            self.segment += 1
            name = f"{self.segment:024X}"
            (clusterpitr.archive_folder(self.project) / name).write_bytes(b"wal")
            return f"731\n{name}\n"
        if "pg_stat_archiver" in statements:
            return f"{self.archive_mode}|0|0|f|{self.size}|{20 * clusterpitr.MB}\n"
        if "sum(pg_database_size(oid)) FROM pg_database" in statements:
            return f"{self.size}\n"
        if "pg_hba_file_rules" in statements:
            return "0\n"
        return ""

    # --- docker compose on the cluster's project
    def compose(self, project, *args, input=None, check=True, stdout=None, timeout=None):
        self.composed.append(args)
        out, code = "", 0
        if "pg_basebackup" in args:
            folder = Path(args[args.index("-D") + 1])
            folder.mkdir(parents=True)
            location = self.mounts["acme"] / machinedb.WS_FOLDER / "ts"
            (folder / "base.tar.gz").write_bytes(_tar({"PG_VERSION": b"17\n",
                                                       "tablespace_map": f"{TABLESPACE} {location}\n".encode()}))
            (folder / f"{TABLESPACE}.tar.gz").write_bytes(_tar({"PG_17_202406281/16400/1259": b"acme's table"}))
            self.segment += 1
        if "start" in args and self.archive_mode != "broken":
            self.archive_mode = "on" if any("archive_mode = 'on'" in s for _, s in self.statements) else self.archive_mode
        elif "start" in args:
            # broken: it does not start with the archive on, it does with the settings of before
            self.starts += 1
            self.archive_mode = "off" if self.starts > 1 else "broken"
        if "pg_isready" in args and self.archive_mode == "broken":
            code = 1
        if "pg_restore" in args and "-l" in args:
            out = "; Archive\n12; 3079 16400 EXTENSION - pg_trgm \n13; 0 0 COMMENT - EXTENSION pg_trgm \n20; 1259 16401 TABLE public res_users odoo\n"
        elif "pg_restore" in args and self.fail_restore:
            code = 1
        if check and code:
            raise machinedb.ClusterError(f"docker compose {' '.join(args[:3])} failed")
        return type("Done", (), {"returncode": code, "stdout": out, "stderr": "pg_restore: error: out of disk"})

    # --- docker on the temporary cluster
    def docker(self, *args, input=None, stdout=None, timeout=None, check=True):
        self.dockered.append(args)
        out = ""
        if args[0] == "inspect":
            if self.on_inspect:
                self.on_inspect()
            out = self.states.pop(0) if self.states else "running"
        elif args[0] == "logs":
            out = self.log
        elif args[0] == "ps":
            out = "\n".join(self.left)
        elif args[0] == "exec" and "pg_dump" in args:
            stdout.write(b"a dump of one database")
        elif args[0] == "exec" and "pg_is_in_recovery" in (input or ""):
            out = "f\n"
        elif args[0] == "exec" and "FROM pg_database WHERE datname" in (input or ""):
            out = "1\n" if self.present else "\n"
        return type("Done", (), {"returncode": 0, "stdout": out, "stderr": ""})

    def temps(self):
        return clusterpitr.temp_folders(self.project, "17")

    def recoveries(self):
        folder = self.mounts["acme"] / machinedb.WS_FOLDER
        return [p.name for p in folder.iterdir() if p.name.startswith("recovery-")]

    def ran(self):
        return next(a for a in self.dockered if a[0] == "run")


@pytest.fixture
def cluster(tmp_path, monkeypatch):
    return Cluster(tmp_path, monkeypatch)


def switched_on(cluster) -> dict:
    """The archive on, its first base backup ended an hour ago."""
    found = clusterpitr.enable("17", "20", "7", "2", "restart")
    for folder in clusterpitr.base_folder(cluster.project).iterdir():
        info = json.loads((folder / clusterpitr.INFO).read_text())
        info["end"] = (datetime.datetime.now() - datetime.timedelta(hours=1)).isoformat(timespec="seconds")
        (folder / clusterpitr.INFO).write_text(json.dumps(info))
    return found


def a_minute_ago() -> str:
    return (datetime.datetime.now() - datetime.timedelta(minutes=1)).isoformat(timespec="minutes")


# --- the archive -------------------------------------------------------------------

def test_a_segment_is_copied_whole_then_renamed():
    command = clusterpitr.archive_command(Path("/var/lib/omniacli-storage/default/.machine-db/pg17/wal/archive"))
    # never over one that is there; a copy cut short keeps another name
    assert command.startswith("test ! -f /var/lib/omniacli-storage/default/.machine-db/pg17/wal/archive/%f && cp %p ")
    assert command.endswith("/%f.part && mv /var/lib/omniacli-storage/default/.machine-db/pg17/wal/archive/%f.part "
                            "/var/lib/omniacli-storage/default/.machine-db/pg17/wal/archive/%f")
    assert "'" not in command
    for bad in ("/var/lib/x y/archive", "/var/lib/x'; DROP/archive", "/var/lib/$(id)/archive"):
        with pytest.raises(machinedb.ClusterError, match="not a path"):
            clusterpitr.archive_command(Path(bad))


def test_only_the_superuser_takes_a_base_backup_and_on_the_socket():
    lines = machinedb.hba_text({"acme_mach": {}}, {"beta_erp": {"address": "203.0.113.7"}}).splitlines()
    assert "local replication postgres scram-sha-256" in lines
    # no replication for an environment's role, none from the network
    assert [line for line in lines if "replication" in line] == ["local replication postgres scram-sha-256"]
    assert lines[-2:] == ["local all all reject", "host all all all reject"]


@pytest.mark.parametrize("budget, keep, reserve", [("0", "7", "2"), ("20", "0", "2"), ("20", "61", "2"), ("20", "7", "-1"),
                                                   ("x", "7", "2"), ("20", "7.5", "2"), ("20; rm", "7", "2")])
def test_the_amounts_are_numbers_within_their_bounds(budget, keep, reserve):
    with pytest.raises(machinedb.ClusterError):
        clusterpitr.amounts(budget, keep, reserve)
    assert clusterpitr.amounts("20", "7", "2,5") == (20.0, 7, 2.5)


def test_switched_on_only_with_the_restart_said(cluster):
    # the cluster does not archive yet: it must restart, and whoever asks was not told
    with pytest.raises(machinedb.ClusterError, match="restarts once"):
        clusterpitr.enable("17", "20", "7", "2", "no-restart")
    assert not any("ALTER SYSTEM" in s for _, s in cluster.statements) and not cluster.composed
    assert not clusterpitr.settings(cluster.project)["enabled"]
    found = switched_on(cluster)
    assert found["changed"] == "on" and found["restarted_s"] is not None and found["serves"] == 3
    written = "".join(s for _, s in cluster.statements)
    archive = clusterpitr.archive_folder(cluster.project)
    assert "ALTER SYSTEM SET archive_mode = 'on'" in written and f"cp %p {archive}/%f.part" in written
    assert f"archive_timeout = '{clusterpitr.ARCHIVE_TIMEOUT}'" in written
    # a checkpoint, PostgreSQL's fast stop, a start; then the first base backup
    assert [a[0] for a in cluster.composed if a[0] in ("stop", "start")] == ["stop", "start"]
    assert any("pg_basebackup" in a for a in cluster.composed)
    # the archive is the cluster's user's and root's only, in the cluster's project, outside the workspaces' disks
    for folder in (clusterpitr.wal_folder(cluster.project), archive, clusterpitr.base_folder(cluster.project)):
        assert folder.stat().st_mode & 0o777 == 0o700
        assert not any(str(folder).startswith(str(mount)) for mount in cluster.mounts.values())
    assert "local replication postgres scram-sha-256" in (cluster.project / "conf" / "pg_hba.conf").read_text()
    conf = clusterpitr.settings(cluster.project)
    assert conf["enabled"] and conf["keep"] == 7 and conf["budget_gb"] == 20.0 and conf["reserve_gb"] == 2.0


def test_on_already_only_the_amounts_change(cluster):
    switched_on(cluster)
    cluster.statements.clear(), cluster.composed.clear()
    found = clusterpitr.enable("17", "5", "3", "10", "no-restart")
    assert found["changed"] == "amounts" and found["restarted_s"] is None
    assert not any("ALTER SYSTEM" in s for _, s in cluster.statements)
    assert not any(a[0] in ("stop", "start") or "pg_basebackup" in a for a in cluster.composed)
    conf = clusterpitr.settings(cluster.project)
    assert (conf["budget_gb"], conf["keep"], conf["reserve_gb"]) == (5.0, 3, 10.0)


def test_a_cluster_that_does_not_come_back_runs_as_before(cluster):
    auto = cluster.project / "data" / "pgdata" / "postgresql.auto.conf"
    auto.write_text("shared_buffers = '1GB'\n")
    cluster.archive_mode = "broken"
    with pytest.raises(machinedb.ClusterError, match="did not come back with the archive on"):
        clusterpitr.enable("17", "20", "7", "2", "restart")
    # the postgresql.auto.conf of before, the archive not recorded as on
    assert auto.read_text() == "shared_buffers = '1GB'\n"
    assert not clusterpitr.settings(cluster.project)["enabled"]


def test_switched_off_without_a_restart_the_copies_deleted(cluster):
    switched_on(cluster)
    cluster.statements.clear(), cluster.composed.clear()
    (clusterpitr.archive_folder(cluster.project) / ("0" * 23 + "9")).write_bytes(b"x" * 1000)
    found = clusterpitr.disable("17")
    written = "".join(s for _, s in cluster.statements)
    assert "archive_command = '/bin/true'" in written and "RESET archive_mode" in written
    assert not any(a[0] in ("stop", "start", "restart") for a in cluster.composed)
    assert found["freed"] > 1000
    assert not list(clusterpitr.archive_folder(cluster.project).iterdir())
    assert not list(clusterpitr.base_folder(cluster.project).iterdir())
    assert not clusterpitr.settings(cluster.project)["enabled"]
    with pytest.raises(machinedb.ClusterError, match="is off"):
        clusterpitr.base("17")
    # on again before any restart of the cluster: it still archives, no restart is needed
    cluster.composed.clear()
    assert clusterpitr.enable("17", "20", "7", "2", "no-restart")["restarted_s"] is None
    assert not any(a[0] in ("stop", "start") for a in cluster.composed)


# --- the base backups, the retention, the budget --------------------------------------

def test_a_base_has_its_tablespaces_and_its_record(cluster):
    switched_on(cluster)
    [base] = clusterpitr.bases(cluster.project)
    folder = clusterpitr.base_folder(cluster.project) / base["name"]
    assert {p.name for p in folder.iterdir()} == {"base.tar.gz", f"{TABLESPACE}.tar.gz", "info.json"}
    info = json.loads((folder / "info.json").read_text())
    assert info["start"] == f"{0x11:024X}" and info["data_size"] == cluster.size
    command = next(a for a in cluster.composed if "pg_basebackup" in a)
    for option in ("-Ft", "-z", "-X", "none"):
        assert option in command


def test_the_retention_keeps_what_a_recovery_needs(cluster):
    switched_on(cluster)
    archive = clusterpitr.archive_folder(cluster.project)
    for number in range(0x05, 0x30):
        (archive / f"{number:024X}").write_bytes(b"wal")
    (archive / "00000002.history").write_text("history")
    (archive / f"{0x06:024X}.00000028.backup").write_text("label")
    made = [clusterpitr.base("17")["base"] for _ in range(3)]
    names = [b["name"] for b in clusterpitr.bases(cluster.project)]
    assert len(names) == 4 and names[-3:] == made
    clusterpitr.enable("17", "20", "2", "2", "no-restart")
    kept = clusterpitr.bases(cluster.project)
    assert [b["name"] for b in kept] == made[-2:]
    # the segments before the start of the oldest kept base are gone, those from it on stay; the history stays
    left = sorted(p.name for p in archive.iterdir())
    assert all(name[:24] >= kept[0]["start"] for name in left if clusterpitr.SEGMENT.fullmatch(name[:24]))
    assert kept[0]["start"] in left and f"{0x2F:024X}" in left and "00000002.history" in left
    assert f"{0x06:024X}.00000028.backup" not in left
    # while a recovery runs nothing is deleted under it
    with open(cluster.project / clusterpitr.LOCK_FILE, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        before = sorted(p.name for p in archive.iterdir())
        found = clusterpitr.enable("17", "20", "1", "2", "no-restart")
        assert "skipped" in found["pruned"]
        assert len(clusterpitr.bases(cluster.project)) == 2 and sorted(p.name for p in archive.iterdir()) == before
        with pytest.raises(machinedb.ClusterError, match="a recovery at a minute is running"):
            clusterpitr.disable("17")
    assert len(clusterpitr.bases(cluster.project)) == 2


def test_a_base_that_did_not_end_is_not_one(cluster):
    switched_on(cluster)
    broken = clusterpitr.base_folder(cluster.project) / "20200101T000000"
    broken.mkdir()
    (broken / "base.tar.gz").write_bytes(b"cut short")
    assert len(clusterpitr.bases(cluster.project)) == 1
    clusterpitr.base("17")
    assert not broken.exists() and len(clusterpitr.bases(cluster.project)) == 2


def test_the_state_says_the_budget_passed_and_the_room(cluster, monkeypatch):
    switched_on(cluster)
    found = clusterpitr.state("17", "acme_mach")
    assert found["enabled"] and found["archive_mode"] == "on" and not found["over"] and found["first"]
    # up to the minute before now: a recovery archives the segment being written first
    assert datetime.datetime.now() - datetime.datetime.fromisoformat(found["last"]) < datetime.timedelta(minutes=2)
    need = found["room"]["need"]
    # the whole cluster, not one database
    assert need > cluster.size and found["room"]["fits"]
    assert found["seconds"] >= clusterpitr.FIXED_SECONDS
    (clusterpitr.archive_folder(cluster.project) / ("0" * 23 + "F")).write_bytes(b"x" * 40000)
    monkeypatch.setattr(clusterpitr, "GB", 1000)
    found = clusterpitr.state("17")
    assert found["over"] and found["used"] > found["budget"] == 20000
    monkeypatch.setattr(clusterpitr, "GB", 1 << 30)
    monkeypatch.setattr(clusterpitr.shutil, "disk_usage", lambda path: type("U", (), {"free": need + (1 << 30)}))
    assert not clusterpitr.state("17")["room"]["fits"]


# --- one database at a minute ----------------------------------------------------------

def test_a_database_at_a_minute(cluster):
    switched_on(cluster)
    stopped = []

    def stop():
        # the temporary cluster is gone before the staging is touched
        stopped.append((cluster.temps(), any("DROP DATABASE" in s for _, s in cluster.statements)))
    cluster.statements.clear()
    found = clusterpitr.restore("17", "acme", "acme_mach", "acme_mach_stg", a_minute_ago(), stop=stop)
    assert found["database"] == "acme_mach_stg" and set(found["steps"]) >= {"unpack", "replay", "dump", "restore"}
    run = cluster.ran()
    temp = Path(next(a for a in run if a.endswith(f":{clusterpitr.DATA_IN_DB}")).split(":")[0]).parent
    # root's folder beside the cluster's project, on the storage's own disk, in no workspace
    assert temp.parent == cluster.project.parent and clusterpitr.TEMP_NAME.fullmatch(temp.name)
    assert run[run.index("--network") + 1] == "none" and "--read-only" in run and "-p" not in run
    assert f"{clusterpitr.archive_folder(cluster.project)}:{clusterpitr.ARCHIVE_IN_TEMP}:ro" in run
    for option in ("archive_mode=off", "listen_addresses=", "recovery_target_action=promote"):
        assert option in run
    assert any(a.startswith("recovery_target_time=") for a in run)
    # the storage of the real machine is never mounted: a folder of the temporary cluster is, at its path
    mounts = [a for a in run if a.endswith(f":{cluster.root}")]
    assert mounts == [f"{temp / 'roots' / '0'}:{cluster.root}"]
    assert not any(a.startswith(f"{cluster.root}:") or a.startswith(f"{cluster.mounts['acme']}:") for a in run)
    # gone: the folder, the container, the dump and its list on the workspace's disk
    assert not cluster.temps() and not cluster.recoveries()
    assert any(a[:3] == ("rm", "-f", "-v") for a in cluster.dockered)
    assert stopped == [([], False)]
    written = [s for _, s in cluster.statements]
    drop = next(i for i, s in enumerate(written) if 'DROP DATABASE IF EXISTS "acme_mach_stg"' in s)
    assert 'CREATE DATABASE "acme_mach_stg" OWNER "acme_mach_stg" TEMPLATE "omniacli.tpl.acme"' in written[drop + 1]
    # the extension by the superuser, the rest as the staging's own role
    assert any("CREATE EXTENSION IF NOT EXISTS pg_trgm" in s for s in written[drop:])
    restore = next(a for a in cluster.composed if "pg_restore" in a and "-l" not in a)
    assert "--role=acme_mach_stg" in restore and "--no-owner" in restore and "--no-acl" in restore
    assert 'REVOKE CONNECT, TEMPORARY ON DATABASE "acme_mach_stg" FROM PUBLIC' in "".join(written)
    # nothing of the production's database is written
    assert not any('DATABASE "acme_mach"' in s for s in written)
    last = clusterpitr.state("17")["last_recovery"]
    assert last["database"] == "acme_mach" and last["temp_bytes"] > 0
    # the staging it was written into is kept too: the page names it, not the database
    assert last["target"] == "acme_mach_stg"


def test_the_extensions_of_a_dump_are_the_superusers():
    # the lines as pg_restore -l of PostgreSQL 17 writes them: an extension has no owner, a space ends its line
    listing = ("; Archive created at 2026-11-22\n2; 3079 72392 EXTENSION - pg_trgm \n5314; 0 0 COMMENT - EXTENSION pg_trgm \n"
               "3; 3079 72400 EXTENSION - unaccent \n20; 1259 16401 TABLE public res_users acme_mach\n")
    extensions, kept = machinedb.restore_list(listing)
    assert extensions == ["pg_trgm", "unaccent"]
    assert "EXTENSION" not in kept and "TABLE public res_users" in kept


def test_the_tablespaces_are_unpacked_where_the_container_mounts_them(cluster):
    switched_on(cluster)
    seen = {}

    def look():
        temp = cluster.temps()[0]
        where = clusterpitr.under_roots(str(cluster.mounts["acme"] / machinedb.WS_FOLDER / "ts"), [str(cluster.root)], temp)
        seen["table"] = (where / "PG_17_202406281" / "16400" / "1259").read_bytes()
        seen["map"] = (temp / "data" / "pgdata" / "tablespace_map").read_text()
        seen["signal"] = (temp / "data" / "pgdata" / "recovery.signal").exists()
        seen["mode"] = temp.stat().st_mode & 0o777
        # a tablespace of now that the base has not: its folder is there for a CREATE TABLESPACE replayed
        seen["beta"] = clusterpitr.under_roots(str(cluster.mounts["beta"] / machinedb.WS_FOLDER / "ts"),
                                               [str(cluster.root)], temp).is_dir()
        seen["hba"] = (temp / "conf" / "pg_hba.conf").read_text()
        cluster.on_inspect = None
    cluster.on_inspect = look
    clusterpitr.restore("17", "acme", "acme_mach", "acme_mach_stg", a_minute_ago())
    assert seen["table"] == b"acme's table" and seen["signal"] and seen["mode"] == 0o700 and seen["beta"]
    # tablespace_map as the base wrote it: the path is the real one, which the container sees in its own folder
    assert seen["map"] == f"{TABLESPACE} {cluster.mounts['acme'] / machinedb.WS_FOLDER / 'ts'}\n"
    assert "trust" not in seen["hba"] and seen["hba"].splitlines()[-2:] == ["local all all reject", "host all all all reject"]
    # the real tablespace was not touched
    assert not list((cluster.mounts["acme"] / machinedb.WS_FOLDER / "ts").iterdir())
    with pytest.raises(machinedb.ClusterError, match="outside the storages"):
        clusterpitr.under_roots("/etc/postgresql", [str(cluster.root)], cluster.project)
    with pytest.raises(machinedb.ClusterError):
        clusterpitr.under_roots(f"{cluster.root}/../etc", [str(cluster.root)], cluster.project)


def test_a_tablespace_made_after_the_base_gets_its_folder(cluster):
    switched_on(cluster)
    later = cluster.root / "gamma" / machinedb.WS_FOLDER / "ts"
    cluster.states = ["exited"]
    cluster.log = (f'FATAL:  directory "{later}" does not exist\n'
                   "HINT:  Create this directory for the tablespace before restarting the server.\n")
    made = {}

    def look():
        if cluster.temps() and any(a[0] == "start" for a in cluster.dockered):
            made["there"] = clusterpitr.under_roots(str(later), [str(cluster.root)], cluster.temps()[0]).is_dir()
    cluster.on_inspect = look
    clusterpitr.restore("17", "acme", "acme_mach", "acme_mach_stg", a_minute_ago())
    assert made["there"] and [a[0] for a in cluster.dockered].count("start") == 1
    # in the temporary folder, never on the real storage
    assert not later.exists() and not cluster.temps()


def test_refused_before_anything_is_made(cluster, monkeypatch):
    with pytest.raises(clusterpitr.RecoveryFailed, match="is off"):
        clusterpitr.restore("17", "acme", "acme_mach", "acme_mach_stg", a_minute_ago())
    switched_on(cluster)
    base = datetime.datetime.fromisoformat(clusterpitr.bases(cluster.project)[0]["time"])
    cases = [("acme", "acme_mach", "acme_mach_stg", (base - datetime.timedelta(hours=1)).isoformat(), "no full copy before"),
             ("acme", "acme_mach", "acme_mach_stg", (base + datetime.timedelta(days=1)).isoformat(), "not past yet"),
             ("acme", "acme_mach", "acme_mach_stg", "yesterday", "not a time"),
             # another customer's database is never read into this workspace, nor written from it
             ("acme", "beta_shop", "acme_mach_stg", a_minute_ago(), "not a database of this workspace"),
             ("acme", "acme_mach", "beta_shop", a_minute_ago(), "not a database of this workspace"),
             ("acme", "acme_mach", "acme_mach", a_minute_ago(), "one database")]
    for workspace, source, target, when, message in cases:
        with pytest.raises(clusterpitr.RecoveryFailed, match=message):
            clusterpitr.restore("17", workspace, source, target, when)
    with pytest.raises(machinedb.ClusterError):
        clusterpitr.restore("17", "acme", "acme_mach; DROP", "acme_mach_stg", a_minute_ago())
    # the disk has not the room for the whole cluster unpacked and what must stay free
    monkeypatch.setattr(clusterpitr.shutil, "disk_usage", lambda path: type("U", (), {"free": 300 * clusterpitr.MB}))
    with pytest.raises(clusterpitr.RecoveryFailed, match="has not the room") as refused:
        clusterpitr.restore("17", "acme", "acme_mach", "acme_mach_stg", a_minute_ago())
    # no number of the cluster in what a workspace's user reads; they are in root's log
    assert not re.search(r"\d", str(refused.value))
    assert "bytes" in (cluster.project / clusterpitr.LOG_FILE).read_text()
    assert not any(a[0] == "run" for a in cluster.dockered) and not cluster.temps()
    assert not any("DROP DATABASE" in s for _, s in cluster.statements)


def test_one_recovery_at_a_time(cluster):
    switched_on(cluster)
    with open(cluster.project / clusterpitr.LOCK_FILE, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert clusterpitr.recovering(cluster.project)
        with pytest.raises(clusterpitr.RecoveryFailed, match="another recovery at a minute is running"):
            clusterpitr.restore("17", "acme", "acme_mach", "acme_mach_stg", a_minute_ago())
    assert not clusterpitr.recovering(cluster.project)


def test_a_failure_leaves_no_temporary_cluster_and_names_nobody(cluster):
    switched_on(cluster)
    cluster.states = ["exited"]
    cluster.log = ('FATAL:  could not open file "/var/lib/omniacli-storage/default/beta/.omniacli-db/ts/PG_17/1/2"\n'
                   "LOG:  startup process (PID 29) exited with exit code 1\n")
    with pytest.raises(clusterpitr.RecoveryFailed) as failed:
        clusterpitr.restore("17", "acme", "acme_mach", "acme_mach_stg", a_minute_ago())
    said = str(failed.value)
    # what the workspace's user reads: the step, no path, no other customer; the reason is root's
    assert "replaying the changes" in said and "/" not in said and "beta" not in said
    assert "beta/.omniacli-db" in (cluster.project / clusterpitr.LOG_FILE).read_text()
    assert (cluster.project / clusterpitr.LOG_FILE).stat().st_mode & 0o777 == 0o600
    assert not cluster.temps() and not cluster.recoveries()
    assert any(a[:3] == ("rm", "-f", "-v") for a in cluster.dockered)
    # the staging's database was not touched: the failure came before
    assert not any("DROP DATABASE" in s for _, s in cluster.statements)
    failure = clusterpitr.state("17")["last_failure"]
    assert failure["step"] == "replay" and "beta" in failure["reason"]


def test_a_minute_before_the_database_was_there(cluster):
    switched_on(cluster)
    cluster.present = False
    with pytest.raises(clusterpitr.RecoveryFailed, match="was not there yet"):
        clusterpitr.restore("17", "acme", "acme_mach", "acme_mach_stg", a_minute_ago())
    assert not cluster.temps() and not cluster.recoveries()


def test_a_staging_half_written_is_made_empty_again(cluster):
    switched_on(cluster)
    cluster.fail_restore = True
    cluster.statements.clear()
    with pytest.raises(clusterpitr.RecoveryFailed, match="writing the database of the staging"):
        clusterpitr.restore("17", "acme", "acme_mach", "acme_mach_stg", a_minute_ago())
    written = [s for _, s in cluster.statements]
    # dropped and made for the restore, then again after it failed: empty, its role's, closed
    assert sum('CREATE DATABASE "acme_mach_stg"' in s for s in written) == 2
    assert 'REVOKE CONNECT, TEMPORARY ON DATABASE "acme_mach_stg"' in written[-1] or any(
        'REVOKE CONNECT, TEMPORARY ON DATABASE "acme_mach_stg"' in s for s in written[-3:])
    assert not cluster.temps() and not cluster.recoveries()


def test_stopped_mid_way_the_temporary_cluster_goes(cluster):
    switched_on(cluster)

    def stop_the_job():
        # the job stopped: the helper gets SIGTERM while the temporary cluster replays
        assert cluster.temps()
        cluster.on_inspect = None
        raise clusterpitr.Interrupted()
    cluster.on_inspect = stop_the_job
    with pytest.raises(clusterpitr.Interrupted):
        clusterpitr.restore("17", "acme", "acme_mach", "acme_mach_stg", a_minute_ago())
    assert not cluster.temps() and not cluster.recoveries()
    assert any(a[:3] == ("rm", "-f", "-v") for a in cluster.dockered)
    assert clusterpitr.state("17")["last_failure"]["reason"] == "stopped"
    assert not clusterpitr.recovering(cluster.project)


def test_a_signal_runs_the_cleanup(cluster):
    import signal
    before = signal.getsignal(signal.SIGTERM)
    with pytest.raises(clusterpitr.Interrupted):
        with clusterpitr._interruptible() as cleanup_starts:
            assert signal.getsignal(signal.SIGTERM) is not before
            os.kill(os.getpid(), signal.SIGTERM)
            raise AssertionError("the signal did not stop the body")
    assert signal.getsignal(signal.SIGTERM) is before
    with clusterpitr._interruptible() as cleanup_starts:
        cleanup_starts()
        # once the cleanup started no signal cuts it short
        os.kill(os.getpid(), signal.SIGTERM)
        assert signal.getsignal(signal.SIGTERM) is signal.SIG_IGN


def test_what_a_kill_left_is_swept(cluster):
    switched_on(cluster)
    left = cluster.project.parent / "recovery-17-0123456789ab"
    (left / "pgdata").mkdir(parents=True)
    (left / "pgdata" / "base").write_bytes(b"every customer's data")
    dump = cluster.mounts["acme"] / machinedb.WS_FOLDER / "recovery-0123456789ab.dump"
    dump.write_bytes(b"a dump")
    other = cluster.project.parent / "recovery-notes"
    other.mkdir()
    cluster.left = ["omniacli-machine-db-17-recovery-0123456789ab"]
    # while a recovery runs (it holds the lock) nothing of it is taken away
    with open(cluster.project / clusterpitr.LOCK_FILE, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert clusterpitr.sweep() == {"swept": {"17": {"running": True}}}
        assert left.exists() and dump.exists()
    assert clusterpitr.sweep() == {"swept": {"17": {"folders": 1, "containers": 1, "files": 1}}}
    assert not left.exists() and not dump.exists() and other.exists()
    assert ("rm", "-f", "-v", "omniacli-machine-db-17-recovery-0123456789ab") in cluster.dockered
    # the next recovery sweeps too, before it makes its own
    (left / "pgdata").mkdir(parents=True)
    clusterpitr.restore("17", "acme", "acme_mach", "acme_mach_stg", a_minute_ago())
    assert not left.exists() and not cluster.temps()


def test_the_helper_takes_two_instances_of_one_workspace_on_the_cluster(cluster, monkeypatch):
    projects = {"mach": cluster.mounts["acme"] / "mach" / "production",
                "mach-stg": cluster.mounts["acme"] / "mach-stg" / "production",
                "dedicated": cluster.mounts["acme"] / "dedicated" / "production"}
    monkeypatch.setattr(workspacehelper, "_project", lambda name, storage, instance, role: projects[instance])
    envs = {"mach": {"major": "17", "role": "acme_mach"}, "mach-stg": {"major": "17", "role": "acme_mach_stg"}}
    monkeypatch.setattr(workspacehelper.composehelper, "on_cluster",
                        lambda project: envs.get(project.parent.name))
    calls = []
    monkeypatch.setattr(workspacehelper, "_dc", lambda project, *args, **kw: calls.append((project, args)))
    switched_on(cluster)
    found = workspacehelper.machine_db_recover("acme", "default", "mach", "mach-stg", a_minute_ago())
    assert found["database"] == "acme_mach_stg"
    # the staging's containers stopped before its database is written, and left stopped
    assert calls == [(projects["mach-stg"], ("stop",))]
    with pytest.raises(HelperError, match="one instance"):
        workspacehelper.machine_db_recover("acme", "default", "mach", "mach", a_minute_ago())
    with pytest.raises(HelperError, match="dedicated database has its own"):
        workspacehelper.machine_db_recover("acme", "default", "dedicated", "mach-stg", a_minute_ago())
    # whatever goes wrong in a way nobody foresaw: its words stay in root's log, the workspace's user reads none
    def broken(*args, **kwargs):
        raise KeyError(f"{cluster.project.parent}/recovery-17-0123456789ab of beta_shop")
    real = clusterpitr.restore
    monkeypatch.setattr(clusterpitr, "restore", broken)
    with pytest.raises(HelperError) as failed:
        workspacehelper.machine_db_recover("acme", "default", "mach", "mach-stg", a_minute_ago())
    assert "recovery-17" not in str(failed.value) and "beta" not in str(failed.value) and "/" not in str(failed.value)
    assert "beta_shop" in (cluster.project / clusterpitr.LOG_FILE).read_text()
    monkeypatch.setattr(clusterpitr, "restore", real)
    # what an instance's page reads: the cluster's archive, with what a recovery of its database takes
    state = workspacehelper.pitr_state("acme", "default", "mach", "production")
    assert state["machine"] and state["enabled"] and state["room"]["fits"] and state["first"]
    assert workspacehelper.ACTIONS["machine-db-recover"][1] == 5


# --- the pages ---------------------------------------------------------------------------

STATE = {"major": "17", "enabled": True, "budget_gb": 20.0, "keep": 7, "reserve_gb": 2.0, "since": "2026-11-20T03:00:00",
         "bases": [{"name": "20261120T030000", "time": "2026-11-20T03:00:04", "size": 20 << 20, "data_size": 150 << 20,
                    "seconds": 2.7}],
         "segments": 4, "archive_size": 64 << 20, "base_size": 20 << 20, "used": 84 << 20, "budget": 20 << 30,
         "over": False, "first": "2026-11-20T03:01:00", "last": "2026-11-20T09:40:00", "failed": 0, "last_failed": "",
         "failing": False, "archive_mode": "on", "recovering": False, "last_recovery": None, "last_failure": None,
         "cluster_size": 150 << 20, "room": {"need": 449526301, "free": 300 << 30, "reserve": 2 << 30, "fits": True},
         "seconds": 129}


@pytest.fixture
async def panel(workspaces, env, monkeypatch, tmp_path):
    """acme has acme-mach, an instance on the machine's cluster (mario its
    admin, luca a user there); the root helper answered here."""
    settings = env["settings"]
    template = stacktemplates.load_template(Path(__file__).parent.parent / "templates" / "odoo-19-machine")
    monkeypatch.setattr(workspaceops, "templates", lambda: {template.name: template})
    settings.storage_roots = {"main": str(tmp_path)}
    ws = settings.workspaces["acme"]
    ws.storage, ws.limits = "main", {"disk_gb": 50}
    monkeypatch.setattr(workspaceops, "storage_path", lambda w: tmp_path)
    (tmp_path / "acme" / "mach").mkdir(parents=True)
    (tmp_path / "acme" / "mach" / "instance.json").write_text(json.dumps({"template": template.name}))
    registry = tmp_path / "machine-db.json"
    registry.write_text(json.dumps({"17": {"project": str(tmp_path / ".machine-db" / "pg17"), "storage": "main"}}))
    monkeypatch.setattr(machinedb, "REGISTRY", registry)
    ports = iter(range(20000, 20100, 2))
    box = {"state": json.loads(json.dumps(STATE)), "helper": [], "ran": [], "fail": None, "asked": []}

    async def create_instance(job, w, instance, tpl, values, setup=None, remote=None):
        port = next(ports)
        return {"type": "docker", "production": {
            "database": f"acme_{instance}".replace("-", "_"), "odoo_service": "odoo", "db_service": machinedb.DB_SERVICE,
            "http_port": port, "update_modules": True,
            "restart": {"type": "compose", "service": str(tmp_path / "acme" / instance / "production"), "sudo": True},
            "repositories": []}}

    async def nothing(*args, **kwargs):
        return {}

    async def helper(job, *args, timeout=None, input=None):
        box["helper"].append(args)
        job.log("$ sudo -n omniacli workspace-helper " + " ".join(args))
        if box["fail"]:
            raise JobFailed(box["fail"])
        if args[0] == "machine-db-recover":
            return {"restored": args[5], "base": "2026-11-20T03:00:04", "database": "x", "seconds": 14.7,
                    "steps": {"unpack": 0.5, "replay": 1.3, "dump": 0.2, "stop": 10.0, "restore": 1.6}}
        if args[0] == "machine-db-archive-on":
            return ({"changed": "amounts", "restarted_s": None} if args[5] == "no-restart" and box["state"]["enabled"]
                    else {"changed": "on", "restarted_s": 0.5 if args[5] == "restart" else None,
                          "base": "20261122T034845", "size": 20 << 20, "base_seconds": 2.7, "serves": 4, "grants": 1})
        if args[0] == "machine-db-archive-off":
            return {"freed": 84 << 20}
        if args[0] == "machine-db-archive-base":
            return {"base": "20261122T050000", "size": 20 << 20, "seconds": 2.7, "used": 30 << 30, "budget": 20 << 30,
                    "over": True, "pruned": {"bases": 1, "segments": 12, "kept": ["a", "b"]}}
        return {}

    async def ask(*args, timeout=None):
        box["asked"].append(args)
        if args[0] == "pitr-state":
            found = box["state"]
            return {"machine": True, **{k: found[k] for k in (
                "major", "enabled", "bases", "segments", "archive_size", "first", "last", "failed", "last_failed",
                "failing", "room", "seconds", "recovering")}}
        if args[0] == "machine-db-archive-state":
            return box["state"]
        if args[0] == "machine-db-recover-sweep":
            return {"swept": {"17": {"folders": 0, "containers": 0, "files": 0}}}
        if args[0] == "machine-db-state":
            return {"clusters": [], "majors": ["17"]}
        return {}

    async def run(job, cmd, **kwargs):
        box["ran"].append(cmd)
        return 0
    monkeypatch.setattr(workspaceops, "create_instance", create_instance)
    monkeypatch.setattr(workspaceops, "publish", nothing)
    monkeypatch.setattr(workspaceops, "_helper", helper)
    monkeypatch.setattr(workspaceops, "ask", ask)
    monkeypatch.setattr(ops, "run", run)
    monkeypatch.setattr(ops, "stop", nothing)
    monkeypatch.setattr(ops, "restart", nothing)
    return box


async def make_mach(client, panel):
    resp = await client.post("/api/workspaces/acme/instances/create", headers=post_as("mario"),
                             json={"instance": "mach", "template": "odoo-19-machine"})
    assert resp.status_code == 200, resp.text
    assert (await wait_job(client, resp.json()["job"]))["status"] == "success"
    panel["helper"].clear()


async def test_a_workspaces_user_learns_nothing_of_the_others(panel, client):
    await make_mach(client, panel)
    mine = (await client.get("/api/servers/acme-mach/pitr", headers=basic("mario"))).json()
    assert mine["machine"] and mine["enabled"] and mine["fits"] and mine["seconds"] == 129
    assert mine["first"] == STATE["first"] and mine["last"] == STATE["last"]
    # nothing of the size of the cluster (every customer's data), of its copies, of the machine's disk
    assert mine["bases"] == [] and mine["segments"] == 0 and mine["archive_size"] == 0
    for key in ("need", "free", "reserve", "room", "whole", "major"):
        assert key not in mine
    whole = (await client.get("/api/servers/acme-mach/pitr", headers={"Authorization": AUTH})).json()
    assert whole["whole"] and whole["need"] == 449526301 and whole["free"] == 300 << 30 and len(whole["bases"]) == 1
    # the page says it, and where a machine's admin switches it on
    page = (await client.get("/servers/acme-mach", headers=basic("mario"))).text
    assert "machineHow" in page and 'machineLink: ""' in page
    page = (await client.get("/servers/acme-mach", headers={"Authorization": AUTH})).text
    assert 'machineLink: "Database tab of the machine' in page
    # switching it on and a full copy are the machine's admin's, in the machine's Database tab
    for action in ("enable", "base"):
        resp = await client.post(f"/api/servers/acme-mach/pitr/{action}", headers=post_as("mario"))
        assert resp.status_code == 400 and "admin of the machine" in resp.json()["error"]
    for what in ("on", "off", "base"):
        resp = await client.post(f"/api/postgres/machine/17/archive/{what}", headers=post_as("mario"), json={})
        assert resp.status_code == 403
    assert panel["helper"] == []


async def test_a_staging_at_a_minute_of_an_instance_on_the_cluster(panel, client, env):
    await make_mach(client, panel)
    # luca, a user of acme who is no admin there: refused, as on a dedicated database
    resp = await client.post("/api/servers/acme-mach/pitr/restore", headers=post_as("luca"), json={"time": "2026-11-20T09:31"})
    assert resp.status_code == 403
    resp = await client.post("/api/servers/acme-mach/pitr/restore", headers=post_as("mario"), json={"time": "2026-11-20T09:31"})
    assert resp.status_code == 200, resp.text
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success", job["lines"]
    staging = resp.json()["server"]
    [recover] = [a for a in panel["helper"] if a[0] == "machine-db-recover"]
    assert recover[1:] == ("acme", "main", "mach", staging[len("acme-"):], "2026-11-20T09:31")
    # the filestore of now linked, neutralized, and only then started
    assert any("link-filestore" in c for c in panel["ran"]) and any("neutralize" in c for c in panel["ran"])
    record = stagings.load((env["settings"].links_file or env["settings"].servers_file).parent / stagings.FILE)
    assert record[staging]["pitr"] == "2026-11-20T09:31" and record[staging]["parent"] == "acme-mach"
    text = "\n".join(job["lines"])
    assert "temporary copy" in text and "The temporary copy is deleted" in text
    # the job's text, which the workspace's users read, has no size of the cluster and no folder of the copy
    assert "recovery-17" not in text and ".machine-db" not in text and "449" not in text


async def test_refused_before_a_staging_is_made(panel, client, env):
    await make_mach(client, panel)
    url, minute = "/api/servers/acme-mach/pitr/restore", {"time": "2026-11-20T09:31"}
    servers = set(env["settings"].servers)

    async def refused(headers, code):
        resp = await client.post(url, headers=headers, json=minute)
        assert resp.status_code == code, resp.text
        assert set(env["settings"].servers) == servers and panel["helper"] == []
        return resp.json()["error"]
    panel["state"]["room"] = {"need": 449526301, "free": 1 << 30, "reserve": 2 << 30, "fits": False}
    # a workspace's admin: no number of the cluster; the machine's admin: the numbers
    said = await refused(post_as("mario"), 507)
    assert "ask the machine's admin" in said and not re.search(r"\d", said)
    said = await refused(POST, 507)
    assert "0.4 GB" in said and "1.0 GB are free" in said and "2.0 GB must stay free" in said
    panel["state"]["room"]["fits"] = True
    panel["state"]["recovering"] = True
    assert "Another recovery" in await refused(post_as("mario"), 409)
    panel["state"]["recovering"] = False
    resp = await client.post(url, headers=post_as("mario"), json={"time": "2026-11-19T09:31"})
    assert resp.status_code == 400 and "outside the minutes that can be reached" in resp.json()["error"]
    # right after the only full copy its minute is not past yet
    panel["state"].update(first="2026-11-20T09:41:00", last="2026-11-20T09:40:00")
    assert "in a moment" in await refused(post_as("mario"), 400)
    panel["state"]["enabled"] = False
    said = await refused(post_as("mario"), 400)
    assert "an admin of the machine switches it on" in said
    assert panel["helper"] == []


async def test_a_failure_says_no_path_in_the_job(panel, client):
    await make_mach(client, panel)
    panel["fail"] = ("workspace-helper: the recovery failed while replaying the changes up to that minute: the temporary "
                     "copy is deleted and the production was not touched. The reason is in the recovery log of the "
                     "machine's PostgreSQL, which the machine's admin reads")
    resp = await client.post("/api/servers/acme-mach/pitr/restore", headers=post_as("mario"), json={"time": "2026-11-20T09:31"})
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "failed" and "temporary copy is deleted" in job["error"]
    mine = (await client.get(f"/api/jobs/{resp.json()['job']}", headers=basic("mario"))).json()
    assert "/var/" not in json.dumps(mine) and "recovery-17" not in json.dumps(mine)


async def test_the_machines_admin_switches_it_on_with_the_restart_said(panel, client):
    await make_mach(client, panel)
    url = "/api/postgres/machine/17/archive/"
    panel["state"].update(enabled=False, archive_mode="off")
    # the cluster would restart: said and asked before, nothing started
    resp = await client.post(url + "on", headers=POST, json={"budget_gb": "20", "keep": "7", "reserve_gb": "2"})
    assert resp.status_code == 409 and "restarts once" in resp.json()["error"] and "1 instances" in resp.json()["error"]
    assert panel["helper"] == []
    resp = await client.post(url + "on", headers=POST, json={"budget_gb": "0", "keep": "7", "reserve_gb": "2", "restart": True})
    assert resp.status_code == 400
    resp = await client.post(url + "on", headers=POST, json={"budget_gb": "20", "keep": "7", "reserve_gb": "2", "restart": True})
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success", job["lines"]
    assert panel["helper"][-1] == ("machine-db-archive-on", "17", "20", "7", "2", "restart")
    text = "\n".join(job["lines"])
    assert "restarted in 0.5 s" in text and "its 4 instances and the 1 databases of other machines" in text
    assert "First full copy 20261122T034845: 20 MiB in 2.7 s" in text
    assert job["title"] == "Recovery at a minute on for the machine's PostgreSQL 17"
    # on already: the amounts only, no restart asked or made
    panel["state"].update(enabled=True, archive_mode="on")
    resp = await client.post(url + "on", headers=POST, json={"budget_gb": "5", "keep": "3", "reserve_gb": "10"})
    job = await wait_job(client, resp.json()["job"])
    assert panel["helper"][-1] == ("machine-db-archive-on", "17", "5", "3", "10", "no-restart")
    assert "Nothing was restarted" in "\n".join(job["lines"])
    # a full copy now: the budget passed is a warning of the job
    resp = await client.post(url + "base", headers=POST, json={})
    job = await wait_job(client, resp.json()["job"])
    assert panel["helper"][-1] == ("machine-db-archive-base", "17")
    assert any("30.0 GB, more than the 20.0 GB of their budget" in line for line in job["lines"])
    resp = await client.post(url + "off", headers=POST, json={})
    job = await wait_job(client, resp.json()["job"])
    assert panel["helper"][-1] == ("machine-db-archive-off", "17") and "without a restart" in "\n".join(job["lines"])
    assert (await client.post(url + "sideways", headers=POST, json={})).status_code == 404
    assert (await client.post("/api/postgres/machine/16/archive/base", headers=POST, json={})).status_code == 404


async def test_each_hour_a_full_copy_a_day_old_is_made_again_and_what_was_left_is_swept(panel, client, caplog):
    await make_mach(client, panel)
    # when OmniaCLI starts: what a recovery that was killed left is deleted
    assert ("machine-db-recover-sweep",) in panel["asked"]
    tick = client._transport.app.app.state.cluster_archive_tick
    panel["asked"].clear()
    # the last full copy is of 20/11: older than a day
    await tick()
    assert ("machine-db-recover-sweep",) in panel["asked"]
    jobs = (await client.get("/api/jobs", headers={"Authorization": AUTH})).json()["jobs"]
    made = next(j for j in jobs if j["title"] == "Full copy of the machine's PostgreSQL 17 for the recovery at a minute")
    assert (await wait_job(client, made["id"]))["status"] == "success"
    assert panel["helper"][-1] == ("machine-db-archive-base", "17")
    # one of now: nothing to make; over the budget: said in OmniaCLI's log
    panel["helper"].clear()
    panel["state"]["bases"][-1]["time"] = datetime.datetime.now().isoformat(timespec="seconds")
    panel["state"].update(over=True, used=30 << 30)
    await tick()
    assert panel["helper"] == [] and "more than the 20.0 GB of its budget" in caplog.text
    # off: nothing is asked of it
    panel["state"]["enabled"] = False
    panel["state"]["bases"][-1]["time"] = "2026-11-20T03:00:04"
    await tick()
    assert panel["helper"] == []


async def test_the_database_tab_has_the_archive_in_the_clusters_row(panel, client, monkeypatch):
    page = (await client.get("/", headers={"Authorization": AUTH})).text
    for part in ('id="pg-archive"', "archiveSwitchOn", "archiveSwitchOff", "archiveRestart", "/archive"):
        assert part in page
    # a workspace's admin has no Database tab
    assert 'id="pg-archive"' not in (await client.get("/", headers=basic("mario"))).text


async def test_what_the_review_of_the_screens_asked(panel, client):
    """The after-the-fact review of the screens of the recovery at a minute (UX-010, part 1)."""
    page = (await client.get("/", headers={"Authorization": AUTH})).text
    # 1. an archive that is not being saved is said on the closed row, in the cell
    cell = page[page.index("function pitrCell"):page.index("function nameCell")]
    assert "a.failing" in cell and "T.archiveFailingBadge" in cell and "archiveFailingBadgeTitle" in page
    # 2. the second confirm of the restart names the databases other machines use, its button its own case
    confirm = page[page.index("function confirmRestart"):page.index('getElementById("pg-confirm-go").addEventListener')]
    assert "T.archiveRestartGrants" in confirm and 'getElementById("pg-confirm-go").title = ' in confirm
    assert "confirmGoTitleGrants" in confirm and "confirmGoTitleMachine" in confirm
    # 3. Switch off… cannot be undone: the name typed, and said
    assert "window.omniaTypeName(fill(T.archiveOffAsk" in page and "window.omniaAsk(fill(T.archiveOffAsk" not in page
    assert "It cannot be undone: the minutes of before are lost. Write {name} to confirm." in page
    # 4. one way to write a size, in the tab and in the instance's page: MB and GB
    assert "const bytes = (n) => window.sizeText(n);" in page and "window.sizeText = (n) =>" in page
    instance = (await client.get("/servers/srv?view=test", headers={"Authorization": AUTH})).text
    # (MiB stays the unit of the memory of the charts, which is not a size on a disk)
    assert "window.sizeText(n)" in instance and '" GiB"' not in instance and 'toFixed(1) + " MiB"' not in instance
    # 5. the last recovery by the instance and the staging born of it
    assert "Last recovery: {instance} " in page and "staging {staging}, at {minute}" in page
    assert "Last recovery: {database}" not in page
    # 6. on the machine's cluster one sentence says who switches it on
    assert "noAdmin.hidden = !!data.machine" in instance
    # 7. the confirm's button has a title of its own, given by who asks
    for part in ("window.omniaTypeName = (message, name, goLabel, goTitle)", 'goTitle = ""} = {}',
                 "window.omniaWrite = (message, value, goLabel, goTitle)", "goLabel.title"):
        assert part in page, part
    assert "T.archiveOffGo, T.archiveOffGoTitle)" in page and "g.name, go, T.dropTitle)" in page


async def test_the_last_recovery_names_the_instance_and_the_staging(panel, client, monkeypatch):
    project = str(Path(machinedb.registry()["17"]["project"]))
    panel["state"]["last_recovery"] = {"at": "2026-11-22T04:47:00", "database": "acme_mach", "target": "acme_mach_stg",
                                       "minute": "2026-11-22T04:40", "seconds": 14.7, "temp_bytes": 200 << 20}
    # a staging deleted since: no name for it, the page says so
    panel["state"]["last_failure"] = {"at": "2026-11-22T04:10:00", "database": "acme_mach", "target": "acme_gone",
                                      "minute": "2026-11-22T04:00", "step": "replay", "reason": "killed"}
    asked = workspaceops.ask

    async def ask(*args, timeout=None):
        if args[0] == "machine-db-state":
            return {"majors": ["17"], "clusters": [{
                "major": "17", "storage": "main", "project": project, "running": True, "promised": 64, "port": None,
                "grants": {}, "environments": {"acme_mach": {"workspace": "acme", "instance": "mach"},
                                               "acme_mach_stg": {"workspace": "acme", "instance": "mach-stg"}}}]}
        return await asked(*args, timeout=timeout)

    async def read(inst):
        return {"version": "17.12", "settings": [], "changed": [], "cluster_size": 150 << 20, "connections": 3}
    monkeypatch.setattr(workspaceops, "ask", ask)
    monkeypatch.setattr(pgtuning, "read", read)
    rows = (await client.get("/api/postgres", headers={"Authorization": AUTH})).json()["rows"]
    archive = next(r for r in rows if r["where"] == "machine")["archive"]
    assert (archive["last_recovery"]["instance"], archive["last_recovery"]["staging"]) == ("acme-mach", "acme-mach-stg")
    assert archive["last_recovery"]["database"] == "acme_mach"
    assert (archive["last_failure"]["instance"], archive["last_failure"]["staging"]) == ("acme-mach", "")
    # the names of the instances are not a field of the row itself
    assert "names" not in next(r for r in rows if r["where"] == "machine")
