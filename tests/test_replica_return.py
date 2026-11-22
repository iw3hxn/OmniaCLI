# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What comes after a promotion (UX-011 §A): the return. The cluster a
replica replaced comes back: on the machine that hosts the promoted copy
what root writes to let it in (the replication's line of pg_hba, the
firewall), on the machine it comes back to its data set aside and put back,
its instances back on its socket; and the two applications talking through
the link of the machine that comes back: prepared, completed with the other
machine answering and not, with the last changes arrived and not, cancelled
from both sides, the replica made again."""
import asyncio
import json
import os
from pathlib import Path

import pytest

from omniacli import machinedb, managedreplica, stacktemplates, workspacehelper, workspaceops
from omniacli.composehelper import HelperError
from omniacli.jobs import JobFailed

from .test_linked_cluster import ADMIN, POST, Network, job_of
from .test_managed_replica import (CA, MARIO, MB, GB, TEMPLATES, THERE, access_log, instance, machines, make,  # noqa: F401
                                   primary, promote)

# --- what root writes ----------------------------------------------------------------------

def test_the_promoted_copy_lets_the_cluster_it_replaced_in_for_the_replication():
    rec = {"environments": ["acme_mach"], "primary_address": "203.0.113.9", "link": "lab2", "state": "promoted"}
    line = 'hostssl replication "omniacli.replica" 203.0.113.9/32 scram-sha-256'
    # a promoted copy lets no replication in, until a return is prepared; then from the one address it already knows
    assert "replication" not in managedreplica.hosted_hba(rec)
    for state, there in (("copying", True), ("ready", True), ("stopped", False), ("cancelled", False), ("done", False)):
        text = managedreplica.hosted_hba({**rec, "return": {"state": state, "address": "203.0.113.9"}})
        assert (line in text.splitlines()) is there, state
    text = managedreplica.hosted_hba({**rec, "return": {"state": "ready"}})
    lines = text.splitlines()
    assert lines.index(line) < lines.index("local all all reject")
    # never without TLS, never a network, never a database: a replication connection only
    assert [x for x in lines if x.startswith("host ")] == ["host all all all reject"] and "/0 " not in text


def test_a_copy_being_made_is_not_yet_one_that_follows(tmp_path, monkeypatch):
    monkeypatch.setattr(machinedb, "write", lambda path, text, mode=0o644, uid=0, gid=0: Path(path).write_text(text))
    sender = {"state": "streaming", "applied": 100.0, "own": None}
    found = {"current": 100.0, "slot": {"held": 0, "wal_status": "reserved"}}
    # the copy streams the changes it is made under as its own application: said copying until the cluster follows
    for application, follows in (("omniacli-return-copy", False), ("pg_basebackup", False), ("omniacli-return", True),
                                 ("omniacli-replica", True)):
        answer = managedreplica._seen(tmp_path, {**found, "sender": {**sender, "application": application}}, False, None, {}, 1000.0)
        assert answer["following"] is follows, application


def test_the_firewall_follows_the_return(primary, monkeypatch, tmp_path):
    project = primary.project
    (project / "replica.json").write_text(json.dumps({"link": "lab1", "address": "203.0.113.7", "state": "replaced"}))
    # replaced and stopped: only the grants' addresses, as before
    assert machinedb.firewall_state() == {"omnidb17": ["203.0.113.7"]}
    # it comes back: it goes out to the promoted copy's port, and nowhere else
    (project / "return.json").write_text(json.dumps({"link": "lab1", "state": "ready", "address": "203.0.113.7", "port": 15433}))
    assert machinedb.firewall_state() == {"omnidb17": {"from": ["203.0.113.7"], "to": [["203.0.113.7", 15433]]}}
    rules = [" ".join(r) for r in machinedb.firewall_rules(machinedb.firewall_state())["forward"]]
    assert "-i omnidb17 -d 203.0.113.7/32 -p tcp --dport 15433 -j RETURN" in rules
    assert rules.index("-i omnidb17 -d 203.0.113.7/32 -p tcp --dport 15433 -j RETURN") < rules.index("-i omnidb17 -j DROP")
    # back: as a cluster that listens
    (project / "return.json").write_text(json.dumps({"link": "lab1", "state": "done", "address": "203.0.113.7", "port": 15433}))
    assert machinedb.firewall_state() == {"omnidb17": ["203.0.113.7"]}
    # the copy on the other machine, once the cluster is back: no one reaches it, it reaches no one
    copy = tmp_path / "replica17"
    copy.mkdir()
    (tmp_path / "machine-db-replica.json").write_text(json.dumps({"17": {"project": str(copy), "link": "lab2"}}))
    for state, there in (("ready", {"from": ["203.0.113.9"], "to": [["203.0.113.9", 15432]]}),
                         ("promoted", {"from": ["203.0.113.9"], "to": []}), ("returned", {"from": [], "to": []})):
        (copy / "replica.json").write_text(json.dumps({"primary_address": "203.0.113.9", "port": 15432, "state": state}))
        assert managedreplica.firewall_state() == {"omnirp17": there}, state


def test_a_stopped_cluster_keeps_its_port_and_is_not_taken_away_under_a_return(primary):
    project = primary.project
    (project / "replica.json").write_text(json.dumps({"link": "lab1", "address": "203.0.113.7", "state": "replaced"}))
    (project / "grants.json").write_text("{}")
    # closing the port would make its container again and start it
    with pytest.raises(machinedb.ClusterError, match="replaced by its replica on lab1 and is stopped"):
        machinedb.listen_off("17")
    (project / "return.json").write_text(json.dumps({"link": "lab1", "state": "copying"}))
    with pytest.raises(machinedb.ClusterError, match="a return is under way: cancel it first"):
        machinedb.remove("17")


def test_the_room_of_a_return_is_said_before():
    facts = {"free": 10 * GB, "aside": {"size": 2500 * MB}, "disks": [{"mount": "/s/acme", "size": 90 * MB, "free": 400 * MB}]}
    assert managedreplica.back_room(2600 * MB, facts, 2) == ""
    # the machine's disk: the copy beside the data set aside
    why = managedreplica.back_room(2600 * MB, {**facts, "free": 3100 * MB}, 2)
    assert why.startswith("no room: the copy takes about 3376 MB, the data set aside stays (2500 MB), 3.0 GB are free here")
    # a workspace's disk: its own part
    why = managedreplica.back_room(2600 * MB, {**facts, "disks": [{"mount": "/s/acme", "size": 90 * MB, "free": 100 * MB}]}, 2)
    assert "no room on the disk of the workspace acme" in why and "100 MB are free" in why


@pytest.fixture
def stopped(primary, monkeypatch):
    """The cluster of the primary fixture, replaced by its replica and stopped, with data in its
    folder and in a tablespace on a workspace's disk; root's commands faked."""
    project = primary.project
    mount = project.parent.parent / "acme"
    (project / "environments.json").write_text(json.dumps({"acme_mach": {"workspace": "acme", "instance": "mach", "mount": str(mount)}}))
    (project / "replica.json").write_text(json.dumps({"link": "lab1", "address": "203.0.113.7", "state": "replaced",
                                                       "host": "203.0.113.7", "port": 15433, "replaced_at": "2026-11-22T06:16:00"}))
    pgdata, ts = project / "data" / "pgdata", mount / ".omniacli-db" / "ts"
    for folder, name in ((pgdata, "PG_VERSION"), (ts, "table")):
        folder.mkdir(parents=True)
        (folder / name).write_text("the data of before")
    (pgdata / "postgresql.auto.conf").write_text("archive_mode = 'on'\nshared_buffers = '1GB'\nprimary_conninfo = 'old'\n")
    (project / "secret").mkdir()
    ran = []

    def run(project, *args, **kwargs):
        ran.append(args)
        return type("Done", (), {"stdout": "", "stderr": "", "returncode": 0})()
    monkeypatch.setattr(machinedb, "_run", run)
    monkeypatch.setattr(machinedb, "pg_user", lambda create=False: (os.getuid(), os.getgid()))
    monkeypatch.setattr(machinedb, "_folder", lambda path, mode, uid=0, gid=0: (path.mkdir(exist_ok=True), path)[1])
    box = type("Stopped", (), {})()
    box.project, box.pgdata, box.ts, box.ran, box.sql = project, pgdata, ts, ran, primary.sql
    return box


SPEC = {"link": "lab1", "by": "carlo", "host": "203.0.113.7", "port": 15433, "ca": CA, "size": 100 * MB,
        "password": "a-password-made-here-0123456789", "tablespaces": [], "reserve_gb": 0}


def test_the_data_is_set_aside_and_comes_back_when_the_return_is_cancelled(stopped):
    project, pgdata, ts = stopped.project, stopped.pgdata, stopped.ts
    facts = managedreplica.back_facts("17")
    held = sum(f.stat().st_size for folder in (pgdata, ts) for f in folder.iterdir())
    assert facts["aside"] == {"at": "2026-11-22T06:16:00", "size": held}
    assert facts["machine"] == "lab1" and [d["mount"] for d in facts["disks"]] == [str(ts.parent.parent)]
    made = managedreplica.back_prepare("17", {**SPEC, "tablespaces": [str(ts)]})
    # renamed where it is: nothing copied, nothing deleted; empty folders in its place
    assert (project / "data" / "pgdata.aside" / "PG_VERSION").read_text() == "the data of before"
    assert (ts.parent / "ts.aside" / "table").read_text() == "the data of before"
    assert list(pgdata.iterdir()) == [] and list(ts.iterdir()) == []
    back = machinedb.returning(project)
    assert back["state"] == "copying" and back["machine"] == "lab1" and back["address"] == "203.0.113.7"
    assert made["aside"]["at"] == "2026-11-22T06:16:00"
    # the password is in the file the cluster's container reads, and nowhere else
    assert "a-password-made-here-0123456789" in (project / "secret" / "replication.pgpass").read_text()
    assert "a-password-made-here" not in (project / "return.json").read_text()
    # a second return is refused while this one is there, and what it would take is said
    with pytest.raises(machinedb.ClusterError, match="under way already"):
        managedreplica.back_prepare("17", SPEC)
    # cancelled: the copy deleted, the data of before back where it was
    (pgdata / "new").write_text("the copy of the promoted cluster")
    done = managedreplica.back_cancel("17")
    assert done["cancelled"] and done["machine"] == "lab1"
    assert (pgdata / "PG_VERSION").read_text() == "the data of before" and not (pgdata / "new").exists()
    assert (ts / "table").read_text() == "the data of before"
    assert not (project / "data" / "pgdata.aside").exists() and not (ts.parent / "ts.aside").exists()
    assert machinedb.returning(project) is None and not (project / "secret" / "replication.pgpass").exists()
    assert ("stop", "-t", "120", "db") in stopped.ran
    with pytest.raises(machinedb.ClusterError, match="no return is under way"):
        managedreplica.back_cancel("17")


def test_what_a_preparation_that_was_killed_left_is_put_back(stopped):
    project, pgdata = stopped.project, stopped.pgdata
    # data set aside and no record of a return: cancelled all the same, never deleted
    os.rename(pgdata, project / "data" / "pgdata.aside")
    pgdata.mkdir()
    with pytest.raises(machinedb.ClusterError, match="set aside by a return that did not end"):
        managedreplica.back_prepare("17", SPEC)
    assert managedreplica.back_cancel("17")["cancelled"]
    assert (pgdata / "PG_VERSION").read_text() == "the data of before"


def test_refused_before_anything_is_set_aside(stopped, monkeypatch):
    project, pgdata = stopped.project, stopped.pgdata
    for change, why in (({"link": ""}, "through a link of this machine"), ({"port": 80}, "not the port"),
                        ({"password": "short"}, "not of its form"), ({"ca": "no"}, "certificates in PEM"),
                        ({"tablespaces": ["/var/lib/elsewhere/ts"]}, "does not have"),
                        ({"size": 1 << 50}, "no room")):
        with pytest.raises(machinedb.ClusterError, match=why):
            managedreplica.back_prepare("17", {**SPEC, **change})
    # only a cluster its replica replaced comes back, and only one that is stopped
    monkeypatch.setattr(machinedb, "_run", lambda project, *args, **kw: type("Done", (), {"stdout": "running\n", "returncode": 0})())
    with pytest.raises(machinedb.ClusterError, match="the machine's PostgreSQL runs"):
        managedreplica.back_prepare("17", SPEC)
    (project / "replica.json").write_text(json.dumps({"link": "lab1", "address": "203.0.113.7", "state": "ready"}))
    with pytest.raises(machinedb.ClusterError, match="was not replaced by its replica"):
        managedreplica.back_prepare("17", SPEC)
    assert (pgdata / "PG_VERSION").read_text() == "the data of before" and not (project / "data" / "pgdata.aside").exists()


def test_the_copy_follows_with_the_settings_the_cluster_had(stopped):
    project, pgdata = stopped.project, stopped.pgdata
    managedreplica.back_prepare("17", SPEC)
    asked = []

    def run(project, *args, **kwargs):
        asked.append(args)
        # what pg_basebackup would bring: the copy's own settings, of the promoted cluster
        (pgdata / "postgresql.auto.conf").write_text("shared_buffers = '128MB'\n")
        return type("Done", (), {"stdout": "", "stderr": "", "returncode": 0})()
    machinedb._run = run
    done = managedreplica.back_copy("17")
    command = " ".join(asked[0])
    # over TLS with the copy's certificate verified, the password from its file, never on the command line
    assert "pg_basebackup -h 203.0.113.7 -p 15433 -U omniacli.replica" in command and "PGSSLMODE=verify-full" in command
    assert "PGPASSFILE=/run/secrets/replication.pgpass" in command and "a-password-made-here" not in command
    auto = (pgdata / "postgresql.auto.conf").read_text()
    # the cluster's own tuning and its recovery at a minute, not the copy's; and where it follows from
    assert "shared_buffers = '1GB'" in auto and "archive_mode = 'on'" in auto and "128MB" not in auto
    assert "primary_conninfo = 'old'" not in auto and "application_name=omniacli-return" in auto
    assert "sslmode=verify-full" in auto and "a-password-made-here" not in auto
    assert (pgdata / "standby.signal").is_file() and done["size"] > 0
    assert machinedb.returning(project)["copy_seconds"] is not None


def test_it_takes_the_writes_only_once_the_last_record_arrived(stopped, monkeypatch):
    project = stopped.project
    managedreplica.back_prepare("17", SPEC)
    (project / "return.json").write_text(json.dumps({**machinedb.returning(project), "state": "ready", "synced": True}))
    monkeypatch.setattr(managedreplica.time, "sleep", lambda s: None)
    stopped.sql.answers["pg_last_wal_replay_lsn"] = "f\n"
    waited = managedreplica.back_wait("17", "0/3000028", timeout=0)
    assert waited["arrived"] is False
    stopped.sql.answers["pg_last_wal_replay_lsn"] = "t\n"
    assert managedreplica.back_wait("17", "0/3000028")["arrived"] is True
    assert stopped.sql.has("pg_wal_lsn_diff(pg_last_wal_replay_lsn(), '0/3000028') > 0")
    with pytest.raises(machinedb.ClusterError, match="not a position"):
        managedreplica.back_wait("17", "0/3000028'; DROP")
    # it takes the writes: no replica any more, what the return needed goes
    stopped.sql.answers["pg_is_in_recovery"] = "f\n"
    stopped.sql.answers["pg_promote"] = "t\n"
    (project / "replica-lag.json").write_text("[]")
    done = managedreplica.back_promote("17")
    assert done["returned_at"] and machinedb.replica(project) is None and not (project / "replica-lag.json").exists()
    back = machinedb.returning(project)
    assert back["state"] == "done" and back["returned_at"] == done["returned_at"]
    assert stopped.sql.has("ALTER SYSTEM RESET primary_conninfo") and stopped.sql.has('DROP ROLE IF EXISTS "omniacli.replica"')
    assert not (project / "secret" / "replication.pgpass").exists()
    # the data set aside is deleted only now
    assert (project / "data" / "pgdata.aside").is_dir()
    assert managedreplica.back_purge("17")["freed"] > 0 and not (project / "data" / "pgdata.aside").exists()
    assert managedreplica.back_note("17", {"told": True, "instances": 5, "other": 1}) == {"told": True, "instances": 5}
    assert managedreplica.back_forget("17") == {"forgotten": True} and machinedb.returning(project) is None


def test_an_instance_goes_back_on_the_cluster_as_it_was(instance, monkeypatch):
    project, folder = instance["project"], instance["folder"]
    before = (project / ".env").read_text()
    instance["target"]["state"] = "replaced"
    workspacehelper.machine_db_replica_follow("acme", "default", "mach")
    exceptions = {}
    monkeypatch.setattr(workspacehelper, "db_remote_file", lambda: dict(exceptions))
    state = {"state": "replaced"}
    monkeypatch.setattr(machinedb, "project_of", lambda major: Path("/nowhere"))
    monkeypatch.setattr(machinedb, "replica", lambda p: dict(state))
    # not while the cluster is still the one its replica replaced
    with pytest.raises(HelperError, match="has not come back"):
        workspacehelper.machine_db_replica_back("acme", "default", "mach")
    state.clear()
    # what changed while it was on the copy stays
    (project / ".env").write_text((project / ".env").read_text() + "ODOO_IMAGE=odoo:19-new\n")
    instance["ran"].clear()
    done = workspacehelper.machine_db_replica_back("acme", "default", "mach")
    assert done == {"already": False, "database": "acme_mach", "db_service": "machine", "template": "odoo-19-machine"}
    env = dict(line.split("=", 1) for line in (project / ".env").read_text().splitlines())
    assert env["DB_CLUSTER"] == "17" and env["DB_USER"] == "acme_mach" and env["DB_SOCKET"] == "/run/x"
    assert env["DB_PASSWORD"] == "its-own-password-0123456789" and env["ODOO_IMAGE"] == "odoo:19-new"
    assert "DB_REMOTE" not in env and "DB_HOST" not in env and "DB_PORT" not in env
    assert set(before.splitlines()) <= set((project / ".env").read_text().splitlines())
    assert (project / "compose.yaml").read_text() == stacktemplates.load_template(TEMPLATES / "odoo-19-machine").compose
    meta = json.loads((folder / "instance.json").read_text())
    # the template it had, at the version the promotion recorded; nothing of the copy is left in its record
    assert meta == {"template": "odoo-19-machine", "database": "acme_mach",
                    "version": stacktemplates.load_template(TEMPLATES / "odoo-19-machine").version}
    assert not (project / "config" / "db-ca.pem").exists() and instance["firewall"][str(project)] is None
    assert instance["ran"] == [("down", "--remove-orphans", "--timeout", "2"), ("up", "-d", "--remove-orphans")]
    # what the promotion kept is deleted; asked again, it is done
    assert not (project / ".before-promotion").exists()
    assert workspacehelper.machine_db_replica_back("acme", "default", "mach")["already"]


# --- the two applications talking through the link of the machine that comes back -------------

class Back:
    """What the root helpers of the two machines would do for a return, kept
    in memory beside the Lab's: lab2's cluster comes back from the promoted
    copy lab1 hosts."""

    def __init__(self, lab):
        self.lab, self.calls, self.inputs = lab, [], {}
        self.arrives, self.fail, self.why, self.free = True, {}, "", 40 * GB
        self.receiving = True

    def record(self):
        try:
            return json.loads((self.lab.project / "return.json").read_text())
        except OSError:
            return None

    def save(self, back):
        path = self.lab.project / "return.json"
        if back is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(json.dumps(back))

    def there(self):
        back = (self.lab.local or {}).get("return") or {}
        if back.get("state") not in ("copying", "ready", "stopped"):
            return {"exists": False, "major": "17", "copy": (self.lab.local or {}).get("state"),
                    "cancelled": {"by": back.get("by"), "at": back.get("at")} if back.get("state") == "cancelled" else None}
        return {"exists": True, "major": "17", "copy": "promoted", **back, "connected": back["state"] == "ready",
                "lag": 2 if back["state"] == "ready" else None, "since": None, "held": 3 * MB, "cap": 20 * GB,
                "wal_status": "reserved", "progress": None, "rate": None, "free": 290 * GB}

    def read(self, args, input):
        if args[:1] == ("machine-db-replica-hosted",):
            found = self.lab.hosted()
            if self.lab.local.get("state") == "returned":
                found.update(running=False, receiving=False)
            # the helper says of a return only one that is under way
            found.pop("return", None)
            there = self.there()
            if there["exists"]:
                found["return"] = {**there, "verdict": managedreplica.verdict(there)}
            return found
        if args[:1] == ("machine-db-replica-return",):
            if args[2] == "facts":
                return {"major": "17", "running": True, "size": 142 * MB, "version": "17.12", "port": 15433,
                        "host": "203.0.113.7", "address": "203.0.113.9", "why": self.why, "tablespaces": [],
                        "under_way": self.there()["exists"]}
            return self.there()
        if args[2] == "facts":
            rec = self.lab.record()
            back = self.record()
            return {"major": "17", "machine": rec["link"], "host": rec["host"], "port": rec["port"],
                    "aside": {"at": rec["replaced_at"], "size": 150 * MB}, "free": self.free, "disks": [], "listening": True,
                    "return": {k: back.get(k) for k in ("link", "state", "at")} if back else None}
        if args[2] == "state":
            back = self.record()
            if not back:
                return {"exists": False, "major": "17"}
            return {"exists": True, "major": "17", **back, "running": back["state"] == "ready",
                    "receiving": back["state"] == "ready" and self.receiving, "reason_code": "" if self.receiving else "unreachable",
                    "copied": 90 * MB if back["state"] == "copying" else None, "free": self.free,
                    "in_recovery": back["state"] != "done"}
        if args[2] == "note":
            self.save({**self.record(), **json.loads(input)})
            return {}
        if args[2] == "forget":
            self.save(None)
            return {"forgotten": True}
        raise AssertionError(args)

    def act(self, args, input):
        if args[0] in self.fail or args[:3] in self.fail:
            raise JobFailed(self.fail.get(args[0]) or self.fail[args[:3]])
        lab = self.lab
        if args[0] == "machine-db-replica-back":
            lab.meta(args[3], promoted_from=None, database_on=None, database_link=None, database_major=None)
            return {"already": False, "database": f"acme_{args[3]}", "db_service": "machine", "template": "odoo-19-machine"}
        step = args[2]
        if args[0] == "machine-db-replica-return":
            back = lab.local.get("return") or {}
            if step == "allow":
                spec = self.inputs["allow"] = json.loads(input)
                lab.local["return"] = {"link": spec["link"], "by": spec["by"], "address": "203.0.113.9",
                                       "at": "2026-11-23T09:02:00", "cap_gb": spec["cap_gb"], "state": "copying"}
                return {"port": 15433, "host": "203.0.113.7", "address": "203.0.113.9", "cap_gb": spec["cap_gb"], "ca": CA,
                        "version": "17.12", "size": 142 * MB, "tablespaces": []}
            if step == "stop":
                lab.local["return"] = {**back, "state": "stopped", "checkpoint": "0/3000028", "stopped_at": "2026-11-23T09:12:00"}
                return {"stopped": True, "already": False, "checkpoint": "0/3000028", "stopped_at": "2026-11-23T09:12:00", "seconds": 0.4}
            if step == "resume":
                lab.local["return"] = {**back, "state": "ready"}
                return {"resumed": True, "seconds": 1.2}
            if step == "done":
                lab.local.update(state="returned", returned_at="2026-11-23T09:12:05")
                lab.local["return"] = {**back, "state": "done"}
                return {"returned_at": "2026-11-23T09:12:05", "already": False}
            if step == "cancel":
                lab.local["return"] = {"state": "cancelled", "by": json.loads(input)["by"], "at": "2026-11-23T09:05:00"}
                return {"cancelled": True, "freed": 3 * MB}
        if step == "prepare":
            spec = self.inputs["prepare"] = json.loads(input)
            rec = lab.record()
            self.save({"link": spec["link"], "by": spec["by"], "host": spec["host"], "port": spec["port"], "state": "copying",
                       "at": "2026-11-23T09:02:01", "expected": spec["size"], "machine": rec["link"],
                       "aside": {"at": rec["replaced_at"], "size": 150 * MB}})
            return {"major": "17", "aside": {"at": rec["replaced_at"], "size": 150 * MB}}
        if step == "copy":
            return {"seconds": 3.9, "size": 160 * MB}
        if step == "start":
            self.save({**self.record(), "state": "ready", "ready_at": "2026-11-23T09:02:10", "synced": True})
            # the promoted copy sees it follow
            lab.local["return"]["state"] = "ready"
            return {"state": "ready", "seconds": 1.5}
        if step == "wait":
            self.inputs["wait"] = json.loads(input)
            return {"arrived": self.arrives, "seconds": 0.4 if self.arrives else 60.0}
        if step == "promote":
            self.save({**self.record(), "state": "done", "returned_at": "2026-11-23T09:12:03"})
            lab.save(None)
            return {"returned_at": "2026-11-23T09:12:03", "seconds": 0.3}
        if step == "purge":
            return {"freed": 150 * MB}
        if step == "cancel":
            self.save(None)
            return {"cancelled": True, "freed": 160 * MB, "link": "lab1", "machine": "lab1"}
        raise AssertionError(args)

    def steps(self):
        return [c[0].replace("machine-db-", "") + ":" + c[2] if c[0] != "machine-db-replica-back" else "back:" + c[3]
                for c in self.calls if c[0] in ("machine-db-return", "machine-db-replica-return", "machine-db-replica-back")
                and c[2] not in ("state", "facts", "note")]


@pytest.fixture
async def back(machines, monkeypatch):  # noqa: F811
    """The two machines after a promotion: lab1 hosts the promoted copy of
    lab2's cluster, acme-mach of lab2 is pointed at it; lab2 has a link of
    its own to lab1 (HTTPS), which lab1 lets in as an admin's."""
    client = machines
    await make(client)
    job = await job_of(client, (await promote(client)).json()["job"])
    assert job["status"] == "success", job
    box = Back(client.lab)
    ask, helper = workspaceops.ask, workspaceops._helper
    ours = ("machine-db-return", "machine-db-replica-return")

    async def asked(*args, timeout=None, input=None):
        if args[0] in ours or args[0] == "machine-db-replica-hosted":
            box.calls.append(args)
            return box.read(args, input)
        return await ask(*args, timeout=timeout, input=input)

    async def helped(job, *args, timeout=None, input=None):
        if args[0] in ours or args[0] == "machine-db-replica-back":
            box.calls.append(args)
            job.log("$ sudo -n omniacli workspace-helper " + " ".join(args))
            return box.act(args, input)
        return await helper(job, *args, timeout=timeout, input=input)
    monkeypatch.setattr(workspaceops, "ask", asked)
    monkeypatch.setattr(workspaceops, "_helper", helped)
    # the copy's own record, which the application reads for the map
    monkeypatch.setattr(managedreplica, "hosted_record", lambda project: dict(client.lab.local or {}))
    client.box = box
    client.way = Network(client.lab1)
    client.lab2.state.link_clients.transport = client.way
    yield client


async def link_back(client, url="https://203.0.113.7", name="lab1"):
    """lab1 lets lab2 in as an admin's link, and lab2 gets its link to lab1."""
    made = await client.post("/api/access/links/save", headers=POST, json={"name": "lab2", "role": "admin", "new": True})
    token = made.json().get("token") or client.token
    client.token = token
    saved = await client.there.post("/api/links/save", headers=THERE, json={"name": name, "new": True, "url": url, "token": token})
    assert saved.status_code == 200, saved.text


FACTS = "/api/postgres/machine/17/return/facts"
PREPARE, COMPLETE, CANCEL = (f"/api/postgres/machine/17/return/{step}" for step in ("prepare", "complete", "cancel"))


async def prepared(client):
    await link_back(client)
    resp = await client.there.post(PREPARE, headers=THERE, json={})
    assert resp.status_code == 200, resp.text
    job = await job_of(client.there, resp.json()["job"])
    assert job["status"] == "success", job
    return job


def cluster_row(data):
    return next(r for r in data["rows"] if r["where"] == "machine")


async def test_without_a_link_the_return_is_refused_before_the_click(back):
    client = back
    facts = (await client.there.get(FACTS, headers=ADMIN)).json()
    assert facts["missing"] and "A link from this machine to lab1 is needed, over HTTPS or SSH" in facts["error"]
    assert "+ Add a server" in facts["error"] and facts["instances"] == ["acme-mach"]
    resp = await client.there.post(PREPARE, headers=THERE, json={})
    assert resp.status_code == 400 and "+ Add a server" in resp.json()["error"]
    # a plain-HTTP link carries none: what it answers says where the data comes from
    await link_back(client, url="http://203.0.113.7", name="plain1")
    facts = (await client.there.get(FACTS, headers=ADMIN)).json()
    assert "plain HTTP" in facts["error"] and "HTTPS or SSH" in facts["error"]
    assert "plain HTTP" in (await client.there.post(PREPARE, headers=THERE, json={})).json()["error"]
    assert client.box.steps() == [] and client.way.requests == []


async def test_what_the_return_says_before_the_click(back):
    client = back
    await link_back(client)
    facts = (await client.there.get(FACTS, headers=ADMIN)).json()
    assert facts["error"] is None and facts["machine"] == "lab1" and facts["link"] == "lab1"
    # the data set aside, the size of the promoted copy, the room, the time, the round trip, what is made there
    assert facts["aside"] == {"at": "2026-11-22T11:02:00", "size": 150 * MB} and facts["size"] == 142 * MB
    assert facts["free"] == 40 * GB and facts["fits"] and facts["need"] == managedreplica.room(142 * MB, 0, 0)["need"]
    assert facts["seconds"] == managedreplica.seconds_expected(142 * MB) and facts["ms"] == 0.4
    assert facts["address"] == "203.0.113.9" and facts["cap_gb"] == 20 and facts["instances"] == ["acme-mach"]
    # nothing was asked of either machine but to read
    assert all(r.method == "GET" for r in client.way.requests) and client.box.steps() == []
    # the other machine does not answer: said, with the way out
    client.way.down = True
    facts = (await client.there.get(FACTS, headers=ADMIN)).json()
    assert "lab1 does not answer" in facts["error"] and "try again when it answers" in facts["error"]
    resp = await client.there.post(PREPARE, headers=THERE, json={})
    assert resp.status_code == 502 and "lab1 does not answer" in resp.json()["error"]
    client.way.down = False
    # no room beside the data set aside: refused before anything is asked to change
    client.box.free = 100 * MB
    facts = (await client.there.get(FACTS, headers=ADMIN)).json()
    assert facts["error"] is None and facts["fits"] is False
    resp = await client.there.post(PREPARE, headers=THERE, json={})
    assert resp.status_code == 507 and "the data set aside stays (150 MB)" in resp.json()["error"]
    assert client.box.steps() == []
    # only the machine's admins
    assert (await client.there.get(FACTS, headers=MARIO)).status_code == 403
    assert (await client.there.post(PREPARE, headers={**MARIO, "Origin": "http://lab2.test"}, json={})).status_code == 403


async def test_the_return_is_prepared_and_nothing_stops(back):
    client = back
    before = list(client.restarted)
    job = await prepared(client)
    # the other machine lets this one in first, then the data is set aside, the copy, the start
    assert client.box.steps() == ["replica-return:allow", "return:prepare", "return:copy", "return:start"]
    assert [s["id"] for s in job["result"]["progress"]["steps"]] == ["allow", "aside", "copy", "follow"]
    allow, prepare = client.box.inputs["allow"], client.box.inputs["prepare"]
    # the password is made here: its verifier goes there, itself to root's input, and it is in no line
    assert machinedb.VERIFIER.fullmatch(allow["verifier"]) and (allow["link"], allow["by"]) == ("lab2", "admin")
    assert prepare["link"] == "lab1" and prepare["host"] == "203.0.113.7" and prepare["port"] == 15433 and prepare["ca"] == CA
    there = (await client.get("/api/jobs", headers=ADMIN)).json()["jobs"]
    lines = job["lines"] + [line for j in there for line in (await client.get(f"/api/jobs/{j['id']}", headers=ADMIN)).json()["lines"]]
    assert not any(prepare["password"] in line or allow["verifier"] in line for line in lines)
    assert any("is set aside (150 MB): it is not deleted until the return is complete" in line for line in job["lines"])
    # nothing of the instances stopped
    assert client.restarted == before
    # both rows say it without opening them
    row = cluster_row((await client.there.get("/api/postgres", headers=ADMIN)).json())
    assert row["state"]["code"] == "replaced" and row["return"]["here"]["state"] == "ready"
    assert row["return"]["there"]["verdict"]["code"] == "sync" and row["return"]["pending"] == ["acme-mach"]
    copy = (await client.get("/api/postgres", headers=ADMIN)).json()["replicas"][0]["here"]
    assert copy["state"] == "promoted" and copy["return"]["state"] == "ready" and copy["return"]["verdict"]["lag"] == 2
    # a second one is refused, on both machines; the cluster is not taken away under it
    assert "under way already" in (await client.there.post(PREPARE, headers=THERE, json={})).json()["error"]
    resp = await client.there.post("/api/postgres/machine/17/remove", headers=THERE)
    assert "A return is under way: cancel it first" in resp.json()["error"]
    # in the access log of both machines, with the user and the link
    assert any("/return/prepare" in (e.get("detail") or "") and e.get("name") == "admin" for e in access_log(client.tmp, "lab2"))
    entry = next(e for e in access_log(client.tmp, "lab1") if (e.get("detail") or "").startswith("POST /api/postgres/replica/17/return "))
    assert (entry["kind"], entry["name"], entry["on_behalf"]) == ("link", "lab2", "admin")


async def test_what_fails_while_it_is_prepared_is_taken_back_on_both_machines(back):
    client = back
    await link_back(client)
    client.box.fail[("machine-db-return", "17", "copy")] = "workspace-helper: the copy of the promoted cluster failed: the link broke"
    resp = await client.there.post(PREPARE, headers=THERE, json={})
    job = await job_of(client.there, resp.json()["job"])
    assert job["status"] == "failed" and "the link broke" in job["error"]
    # here the data set aside goes back where it was, there what was kept for the return is taken away
    assert client.box.steps() == ["replica-return:allow", "return:prepare", "return:copy", "return:cancel", "replica-return:cancel"]
    assert client.box.record() is None and client.lab.local["return"]["state"] == "cancelled"
    row = cluster_row((await client.there.get("/api/postgres", headers=ADMIN)).json())
    assert row["state"]["code"] == "replaced" and row["return"] is None


async def complete(client, **payload):
    return await client.there.post(COMPLETE, headers=THERE, json={"confirm": "lab1", **payload})


async def test_the_return_is_completed_and_the_instances_come_back(back):
    client = back
    await prepared(client)
    client.box.calls.clear()
    # the name of the machine whose copy is stopped, typed
    assert "Write lab1 to confirm" in (await complete(client, confirm="lab2")).json()["error"]
    resp = await complete(client)
    assert resp.status_code == 200, resp.text
    job = await job_of(client.there, resp.json()["job"])
    assert job["status"] == "success", job
    # the copy there is stopped first; this cluster waits for its last record, then takes the writes; that
    # machine is told; the instance goes back on the socket; the data set aside is deleted last
    assert client.box.steps() == ["replica-return:stop", "return:wait", "return:promote", "replica-return:done",
                                  "back:mach", "return:purge"]
    assert client.box.inputs["wait"] == {"checkpoint": "0/3000028"}
    assert [s["id"] for s in job["result"]["progress"]["steps"]] == ["stop", "wait", "writes", "instances"]
    assert job["result"]["returned"]["instances"] == {"acme-mach": {"answers": True, "seconds": 4.0}}
    # the instance is on the machine's cluster again, as the servers file says
    servers = (await client.there.get("/api/servers", headers=ADMIN)).text
    meta = json.loads((client.lab.storage / "acme" / "mach" / "instance.json").read_text())
    assert "promoted_from" not in meta and "database_on" not in meta and "acme-mach" in servers
    data = (await client.there.get("/api/postgres", headers=ADMIN)).json()
    row = cluster_row(data)
    # the row is the cluster's again, with where it came back from while the stopped copy is there
    assert row["state"]["code"] != "replaced" and row["replica"] is None and data["remote"] == []
    assert row["using"] == ["acme-mach"]
    here = row["return"]["here"]
    assert (here["state"], here["machine"], here["returned_at"], here["instances"], here["told"]) == \
        ("done", "lab1", "2026-11-23T09:12:03", 1, True)
    page = (await client.there.get("/servers/acme-mach?view=production", headers=ADMIN)).text
    assert "promoted replica" not in page and 'data-db-line="machine"' in page
    # the copy on the other machine: stopped for good, a replica again or dropped
    copy = (await client.get("/api/postgres", headers=ADMIN)).json()["replicas"][0]["here"]
    assert copy["state"] == "returned" and copy["returned_at"] == "2026-11-23T09:12:05" and not copy["running"]
    assert (await client.get("/api/map", headers=ADMIN)).json()["machines"][0]["replicas"][0]["code"] == "returned"
    # a look of either machine starts nothing and deletes nothing
    client.box.calls.clear()
    await client.lab1.state.replica_look_hosted()
    await client.lab2.state.return_look()
    assert client.box.steps() == [] and client.lab.local["state"] == "returned" and client.box.record()["state"] == "done"
    # done twice: refused
    assert "complete already" in (await complete(client)).json()["error"]
    # in the access log of both machines, with the user and the link
    assert any("/return/complete" in (e.get("detail") or "") and e.get("name") == "admin" for e in access_log(client.tmp, "lab2"))
    for step in ("stop", "done"):
        entry = next(e for e in access_log(client.tmp, "lab1") if f"/return/{step} " in (e.get("detail") or ""))
        assert (entry["kind"], entry["name"], entry["on_behalf"]) == ("link", "lab2", "admin")


async def test_completed_only_with_the_other_machine_answering(back):
    client = back
    await prepared(client)
    client.box.calls.clear()
    before = list(client.restarted)
    client.way.down = True
    resp = await complete(client)
    assert resp.status_code == 409
    said = resp.json()["error"]
    assert "lab1 does not answer" in said and "the two copies would diverge" in said and "Try again when it answers" in said
    # nothing stopped, nothing restarted: there is no way with that machine off
    assert client.box.steps() == [] and client.restarted == before and client.box.record()["state"] == "ready"
    # nor before the return was in sync, nor without a return
    client.way.down = False
    client.box.receiving = False
    assert "not a whole copy yet" in (await complete(client)).json()["error"]
    client.box.save(None)
    assert (await complete(client)).status_code == 404


async def test_when_not_everything_arrives_the_copy_runs_again_and_nothing_is_lost(back):
    client = back
    await prepared(client)
    client.box.calls.clear()
    client.box.arrives = False
    job = await job_of(client.there, (await complete(client)).json()["job"])
    assert job["status"] == "failed"
    assert "not everything lab1 wrote arrived here within 60 s" in job["error"]
    assert "the copy on lab1 runs again and the instances stay there. Nothing is lost" in job["error"]
    # this cluster took no write, the other machine's copy was started again, the instance was not touched
    assert client.box.steps() == ["replica-return:stop", "return:wait", "replica-return:resume"]
    assert client.box.record()["state"] == "ready" and client.lab.local["return"]["state"] == "ready"
    assert "promoted_from" in json.loads((client.lab.storage / "acme" / "mach" / "instance.json").read_text())
    # asked again when it does: completed
    client.box.arrives = True
    assert (await job_of(client.there, (await complete(client)).json()["job"]))["status"] == "success"


async def test_an_instance_that_did_not_come_back_is_brought_back_by_asking_again(back):
    client = back
    await prepared(client)
    client.box.fail["machine-db-replica-back"] = "workspace-helper: docker compose up failed"
    job = await job_of(client.there, (await complete(client)).json()["job"])
    assert job["status"] == "success" and "acme-mach was not pointed at this machine's cluster" in " ".join(job["result"]["warnings"])
    row = cluster_row((await client.there.get("/api/postgres", headers=ADMIN)).json())
    assert row["return"]["here"]["state"] == "done" and row["return"]["pending"] == ["acme-mach"]
    client.box.fail.clear()
    client.box.calls.clear()
    job = await job_of(client.there, (await complete(client)).json()["job"])
    # only the instance: nothing is stopped or promoted twice
    assert job["status"] == "success" and client.box.steps() == ["back:mach", "return:purge"]
    row = cluster_row((await client.there.get("/api/postgres", headers=ADMIN)).json())
    assert row["return"]["pending"] == [] and row["return"]["here"]["instances"] == 1


async def test_cancelled_from_the_machine_that_comes_back(back):
    client = back
    await prepared(client)
    client.box.calls.clear()
    job = await job_of(client.there, (await client.there.post(CANCEL, headers=THERE)).json()["job"])
    assert job["status"] == "success", job
    # here first (the copy deleted, the data of before back), then there
    assert client.box.steps() == ["return:cancel", "replica-return:cancel"]
    row = cluster_row((await client.there.get("/api/postgres", headers=ADMIN)).json())
    assert row["state"]["code"] == "replaced" and row["return"] is None
    copy = (await client.get("/api/postgres", headers=ADMIN)).json()["replicas"][0]["here"]
    assert copy["state"] == "promoted" and "return" not in copy
    assert (await client.there.post(CANCEL, headers=THERE)).status_code == 404
    entry = next(e for e in access_log(client.tmp, "lab1") if "/return/cancel " in (e.get("detail") or ""))
    assert (entry["kind"], entry["name"], entry["on_behalf"]) == ("link", "lab2", "admin")
    # with the other machine silent the return is cancelled here all the same, and what stays there is said
    await prepared_again(client)
    client.way.down = True
    job = await job_of(client.there, (await client.there.post(CANCEL, headers=THERE)).json()["job"])
    assert job["status"] == "success" and "on it stay the role of the replication" in " ".join(job["result"]["warnings"])
    assert client.box.record() is None and client.lab.local["return"]["state"] == "ready"


async def prepared_again(client):
    resp = await client.there.post(PREPARE, headers=THERE, json={})
    assert resp.status_code == 200, resp.text
    assert (await job_of(client.there, resp.json()["job"]))["status"] == "success"


async def test_cancelled_from_the_machine_that_hosts_the_copy(back):
    client = back
    await prepared(client)
    client.box.calls.clear()
    resp = await client.post("/api/postgres/replica/17/return/cancel", headers=POST)
    assert resp.status_code == 200, resp.text
    job = await job_of(client, resp.json()["job"])
    assert job["status"] == "success" and client.box.steps() == ["replica-return:cancel"]
    assert any("lab2 asks this machine every 20 s" in line for line in job["lines"])
    # the other machine reads it at its next look: its copy is deleted, its data of before put back, by a job of its own
    await client.lab2.state.return_look()
    for _ in range(200):
        if client.box.record() is None:
            break
        await asyncio.sleep(0.02)
    assert client.box.steps() == ["replica-return:cancel", "return:cancel"]
    jobs = (await client.there.get("/api/jobs", headers=ADMIN)).json()["jobs"]
    assert any(j["source"] == "system: cancelled on lab1 by admin" for j in jobs)
    assert any("cancelled on lab1 by admin" in (e.get("detail") or "") for e in access_log(client.tmp, "lab2"))
    # the steps of the completion are the other machine's to ask, never an admin's of this one
    for step in ("stop", "resume", "done"):
        assert (await client.post(f"/api/postgres/replica/17/return/{step}", headers=POST)).status_code == 403
    # asked again: nothing to cancel
    assert (await client.post("/api/postgres/replica/17/return/cancel", headers=POST)).json() == {"ok": True, "gone": True}


async def test_the_copy_stopped_for_the_completion_is_never_started_from_its_machine(back):
    client = back
    await prepared(client)
    client.lab.local["return"]["state"] = "stopped"
    resp = await client.post("/api/postgres/replica/17/return/cancel", headers=POST)
    assert resp.status_code == 400 and "this copy is stopped for it and lab2 takes the writes" in resp.json()["error"]


async def test_the_replica_is_made_again_from_the_stopped_copy(back):
    client = back
    await prepared(client)
    assert (await job_of(client.there, (await complete(client)).json()["job"]))["status"] == "success"
    # only a copy the cluster came back from; what it says before, the stopped copy counted as room
    said = (await client.get("/api/links/lab2/postgres/replica?again=1", headers=ADMIN)).json()
    assert said["error"] is None and said["stopped"] == 181 * MB and said["free"] == 297 * GB + 181 * MB
    assert "drop it first" in (await client.get("/api/links/lab2/postgres/replica", headers=ADMIN)).json()["error"]
    client.lab.calls.clear()
    resp = await client.post("/api/postgres/replica/17/remake", headers=POST, json={})
    assert resp.status_code == 200, resp.text
    job = await job_of(client, resp.json()["job"])
    assert job["status"] == "success", job
    # the stopped copy deleted first, then a replica as any other
    assert client.lab.names() == ["machine-db-replica-remove", "machine-db-replica-allow", "make-prepare", "make-copy",
                                  "make-start", "machine-db-replica-ready"]
    assert client.lab.local["state"] == "ready" and client.lab.record()["state"] == "ready"
    # the note of the return goes with the copy it came back from
    assert client.box.record() is None
    row = cluster_row((await client.there.get("/api/postgres", headers=ADMIN)).json())
    assert row["return"] is None and row["replica"]["verdict"]["code"] == "sync"
    assert "is not one lab2 came back from" in (await client.post("/api/postgres/replica/17/remake", headers=POST, json={})).json()["error"]


async def test_the_stopped_copy_is_dropped_and_the_note_goes(back):
    client = back
    await prepared(client)
    assert (await job_of(client.there, (await complete(client)).json()["job"]))["status"] == "success"
    client.lab.calls.clear()
    job = await job_of(client, (await client.post("/api/postgres/replica/17/drop", headers=POST)).json()["job"])
    # nothing is asked of the other machine: it keeps nothing for a copy it came back from
    assert job["status"] == "success" and client.lab.names() == ["machine-db-replica-remove"]
    await client.lab2.state.return_look()
    assert client.box.record() is None


async def test_the_tab_says_the_return_in_its_words(back):
    client = back
    page = (await client.there.get("/", headers=ADMIN)).text
    for part in ("prepareButton(row, machine)", "window.omniaTypeName(text, machine, T.rt.completeGo", "returnBlock(row)",
                 "hostedReturn(row)", "remakeHosted(row, e.currentTarget)", "${returnApi(row)}/facts", "T.rt.removeNot"):
        assert part in page, part
    texts = page[page.index("rt: {"):page.index("const panel = document.querySelector")]
    # never these words on screen (UX-011 §0)
    for word in ("failback", "failover", "standby", "slot", "WAL", "timeline", "streaming"):
        assert word not in texts, word
    # the promotion's dialog says the way back
    assert "The way back is Prepare the return" in page and "There is no way back" not in page
    assert 'id="pg-replica"' not in (await client.there.get("/", headers=MARIO)).text
