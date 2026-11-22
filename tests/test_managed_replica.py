# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A read replica of the machine's cluster made by OmniaCLI on another
machine (AD-004 option A, UX-010): what root writes on the machine that has
the cluster (the replication's line of pg_hba, the firewall, the role, the
slot, its cap) and on the one that hosts the copy (its project, its
pg_hba, where its tablespaces go), what the rows say of it, an instance
that reads from it, and the two applications talking through the link:
made, read, dropped from both sides, promoted with the other machine
answering and not."""
import asyncio
import datetime
import json
from pathlib import Path

import httpx
import pytest
import yaml

from omniacli import (composehelper, linkcopy, linkedcluster, machinedb, managedreplica, ops, pgtuning, stacktemplates,
                      workspacehelper, workspaceops)
from omniacli.app import create_app
from omniacli.auth import hash_password
from omniacli.composehelper import HelperError
from omniacli.config import Access, Workspace, parse_settings
from omniacli.jobs import JobFailed

from .conftest import LINK_TOKEN, PASSWORD, USER_HASH, BasicAsSession, hash_token
from .test_linked_cluster import ADMIN, HOST, POST, Network, job_of

TEMPLATES = Path(__file__).parent.parent / "templates"
GB, MB = 1 << 30, 1 << 20
CA = machinedb.make_ca("17", "lab2")[1]


# --- on the machine that has the cluster: what root writes ------------------------------

def test_pg_hba_lets_the_replication_in_from_one_address():
    text = machinedb.hba_text({"acme_shop": {}}, {}, {"address": "203.0.113.7", "state": "ready"})
    lines = text.splitlines()
    line = 'hostssl replication "omniacli.replica" 203.0.113.7/32 scram-sha-256'
    assert line in lines and lines.index(line) < lines.index("local all all reject")
    # never without TLS, never a network, never a database: a replication connection only
    assert [x for x in lines if x.startswith("host ")] == ["host all all all reject"]
    assert "0.0.0.0" not in text and "/0 " not in text and " all " not in line
    # a cluster its replica replaced lets it in no more; one without a replica is as it was
    assert "omniacli.replica" not in machinedb.hba_text({}, {}, {"address": "203.0.113.7", "state": "replaced"})
    assert machinedb.hba_text({"acme_shop": {}}, {}) == machinedb.hba_text({"acme_shop": {}}, {}, None)
    with pytest.raises(machinedb.ClusterError):
        machinedb.hba_text({}, {}, {"address": "10.0.0.0/8", "state": "ready"})


def test_the_firewall_lets_a_hosted_replica_out_to_its_primary_only():
    rules = machinedb.firewall_rules({"omnirp17": {"from": ["203.0.113.9"], "to": [["203.0.113.9", 15432]]},
                                      "omnidb17": ["203.0.113.7"]})
    forward = [" ".join(r) for r in rules["forward"]]
    # reached from the primary's machine only, on PostgreSQL's port
    assert "-o omnirp17 -s 203.0.113.9/32 -p tcp --dport 5432 -j RETURN" in forward
    # goes out to the primary's port, and takes its answers; nothing else leaves or enters its network
    assert "-i omnirp17 -d 203.0.113.9/32 -p tcp --dport 15432 -j RETURN" in forward
    assert "-o omnirp17 -s 203.0.113.9/32 -p tcp --sport 15432 -m conntrack --ctstate ESTABLISHED -j RETURN" in forward
    assert forward.index("-i omnirp17 -d 203.0.113.9/32 -p tcp --dport 15432 -j RETURN") < forward.index("-i omnirp17 -j DROP")
    assert "-o omnirp17 -j DROP" in forward and "-i omnirp17 -j DROP" in forward
    # a cluster that listens never goes out, as before
    assert not any(r.startswith("-i omnidb17 -d ") for r in forward)
    assert ["-i", "omnirp17", "-j", "DROP"] in rules["input"]
    for bad in ({"from": ["0.0.0.0"], "to": []}, {"from": [], "to": [["203.0.113.9", 0]]}, {"from": [], "to": [["10.0.0.0/8", 5432]]},
                {"from": [], "to": ["203.0.113.9"]}):
        with pytest.raises(machinedb.ClusterError):
            machinedb.firewall_rules({"omnirp17": bad})
    with pytest.raises(machinedb.ClusterError):
        machinedb.firewall_rules({"eth0": {"from": [], "to": []}})


def test_the_firewall_file_keeps_a_replicas_entry(tmp_path, monkeypatch):
    kept = tmp_path / "db-listen.json"
    kept.write_text(json.dumps({"omnirp17": {"from": ["203.0.113.9"], "to": [["203.0.113.9", 15432]]},
                                "omnidb17": ["203.0.113.7"], "omnirp18": {"from": ["nowhere"], "to": []}, "docker0": []}))
    monkeypatch.setattr(workspacehelper, "DB_LISTEN", kept)
    assert workspacehelper.db_listen_file() == {"omnirp17": {"from": ["203.0.113.9"], "to": [["203.0.113.9", 15432]]},
                                                "omnidb17": ["203.0.113.7"]}


class Sql:
    """The cluster's psql, kept in memory: what was asked, and what it answers."""

    def __init__(self, answers=None):
        self.asked, self.answers = [], answers or {}

    def __call__(self, project, database, statements):
        self.asked.append(statements)
        for mark, answer in reversed(list(self.answers.items())):  # the answer set last first
            if mark in statements:
                if isinstance(answer, Exception):
                    raise answer
                return answer
        return ""

    def has(self, text):
        return any(text in s for s in self.asked)


@pytest.fixture
def primary(tmp_path, monkeypatch):
    """The project of a cluster on the machine that has it, PostgreSQL faked."""
    project = tmp_path / ".machine-db" / "pg17"
    (project / "conf").mkdir(parents=True)
    (project / "compose.yaml").write_text("        source: /var/lib/omniacli-storage/default\n")
    (project / "conf" / "server.crt").write_text("")
    (project / "environments.json").write_text(json.dumps({
        "acme_mach": {"workspace": "acme", "instance": "mach", "mount": "/var/lib/omniacli-storage/default/acme"},
        "beta_shop": {"workspace": "beta", "instance": "shop", "mount": "/var/lib/omniacli-storage/default/beta"}}))
    (project / "grants.json").write_text(json.dumps({"acme_linked": {"address": "203.0.113.7", "link": "lab1"}}))
    registry = tmp_path / "machine-db.json"
    registry.write_text(json.dumps({"17": {"project": str(project), "storage": "default"}}))
    monkeypatch.setattr(machinedb, "REGISTRY", registry)
    monkeypatch.setattr(machinedb, "HOSTED_REPLICAS", tmp_path / "machine-db-replica.json")
    sql = Sql({"current_setting('wal_level')": "replica|10|10\n", "pg_database_size": "172173089|17.12\n",
               "pg_tablespace_location": "/var/lib/omniacli-storage/default/acme/.omniacli-db/ts\n"})
    monkeypatch.setattr(machinedb, "sql", sql)
    monkeypatch.setattr(machinedb, "write", lambda path, text, mode=0o644, uid=0, gid=0: Path(path).write_text(text))
    hba = []
    monkeypatch.setattr(machinedb, "write_hba", lambda p, envs: hba.append(machinedb.hba_text(
        envs, machinedb.grants(p), machinedb.replica(p))))
    listen = {"port": 15432}
    monkeypatch.setattr(machinedb, "listening", lambda p: {"port": listen["port"], "bridge": "omnidb17"} if listen["port"] else None)
    monkeypatch.setattr(machinedb, "listen_on", lambda major, names: {"port": 15432, "bridge": "omnidb17",
                                                                       "restarted_s": None if listen["port"] else 0.5})
    monkeypatch.setattr(machinedb, "ca_certificate", lambda p: CA)
    monkeypatch.setattr(managedreplica.time, "sleep", lambda s: None)
    box = type("Primary", (), {})()
    box.project, box.sql, box.hba, box.listen = project, sql, hba, listen
    return box


SPEC = {"address": "203.0.113.7", "reached_as": "lab2.test", "link": "lab1", "by": "carlo", "cap_gb": 20,
        "verifier": linkedcluster.scram_verifier("a-password-made-on-the-other-machine")}


def test_the_machine_that_hosts_the_replica_is_let_in(primary):
    made = managedreplica.allow("17", SPEC)
    asked = "".join(primary.sql.asked)
    # a role of its own for the replication only, its password never here: its verifier
    assert 'CREATE ROLE "omniacli.replica" LOGIN REPLICATION NOSUPERUSER NOCREATEDB NOCREATEROLE' in asked
    assert "a-password-made-on-the-other-machine" not in asked and "SCRAM-SHA-256$" in asked
    # one slot, reserved at once, and a cap on what it holds
    assert "pg_create_physical_replication_slot('omniacli_replica', true)" in asked
    assert "ALTER SYSTEM SET max_slot_wal_keep_size = '20480MB'" in asked
    assert 'hostssl replication "omniacli.replica" 203.0.113.7/32 scram-sha-256' in primary.hba[-1]
    assert machinedb.replica(primary.project)["state"] == "copying"
    # what the other machine needs to copy and follow, and no secret
    assert made["port"] == 15432 and made["ca"] == CA and made["restarted_s"] is None and made["size"] == 172173089
    assert made["roots"] == ["/var/lib/omniacli-storage/default"] and made["environments"] == ["acme_mach", "beta_shop"]
    assert "verifier" not in made and "SCRAM" not in json.dumps(made)
    # the firewall lets that address reach the port, with those of the grants
    assert machinedb.firewall_state() == {"omnidb17": ["203.0.113.7"]}
    # one replica per cluster
    with pytest.raises(machinedb.ClusterError, match="has a read replica already, on lab1"):
        managedreplica.allow("17", {**SPEC, "link": "lab3", "address": "203.0.113.8"})


def test_refused_before_anything_is_made(primary):
    for bad, why in (({"address": "10.0.0.0/8"}, "not an address"), ({"verifier": "a password"}, "not of its form"),
                     ({"link": ""}, "through its link"), ({"cap_gb": "0"}, "between")):
        with pytest.raises(machinedb.ClusterError, match=why):
            managedreplica.allow("17", {**SPEC, **bad})
    # a cluster that does not listen yet is made again only when the caller said so
    primary.listen["port"] = None
    with pytest.raises(machinedb.ClusterError, match="Confirm the restart"):
        managedreplica.allow("17", SPEC)
    assert not primary.sql.has("CREATE ROLE") and machinedb.replica(primary.project) is None
    assert managedreplica.allow("17", {**SPEC, "restart": True})["restarted_s"] == 0.5
    # a cluster set below what a replica needs: said without the names of PostgreSQL's parameters
    primary.sql.answers["current_setting('wal_level')"] = "minimal|10|10\n"
    (primary.project / "replica.json").unlink()
    with pytest.raises(machinedb.ClusterError) as refused:
        managedreplica.allow("17", {**SPEC, "restart": True})
    assert "wal_level" not in str(refused.value) and "base values" in str(refused.value)


def test_what_fails_is_taken_back(primary):
    primary.sql.answers["pg_create_physical_replication_slot"] = machinedb.ClusterError("all replication slots are in use")
    with pytest.raises(machinedb.ClusterError):
        managedreplica.allow("17", SPEC)
    assert machinedb.replica(primary.project) is None
    assert primary.sql.has('DROP ROLE IF EXISTS "omniacli.replica"') and primary.sql.has("RESET max_slot_wal_keep_size")


def test_what_the_cluster_sees_of_its_replica(primary, monkeypatch):
    managedreplica.allow("17", SPEC)
    monkeypatch.setattr(managedreplica.shutil, "disk_usage", lambda p: type("U", (), {"free": 40 * GB})())
    answer = {"current": 5000.0, "size": 172173089, "copy": {"total": 1000, "streamed": 640},
              "slot": {"active": True, "held": 0, "wal_status": "reserved", "inactive_since": None},
              "sender": {"state": "streaming", "applied": 0, "own": None, "application": "pg_basebackup"}}
    primary.sql.answers["pg_stat_progress_basebackup"] = json.dumps(answer)
    # while it is copied: its progress, never "connected"
    seen = managedreplica.state("17", now=1000.0)
    assert seen["state"] == "copying" and not seen["connected"] and seen["progress"] == {"total": 1000, "streamed": 640}
    assert managedreplica.verdict(seen)["code"] == "copying"
    assert seen["instances"] == ["acme-mach", "beta-shop"] and seen["grants"] == ["acme_linked"]
    assert seen["tablespaces"] == ["/var/lib/omniacli-storage/default/acme/.omniacli-db/ts",
                                   "/var/lib/omniacli-storage/default/beta/.omniacli-db/ts"]
    managedreplica.ready("17", {"host": "203.0.113.7", "port": 15433, "ca": CA, "seconds": 3.6, "size": 190 * MB})
    # connected and all applied: 0 seconds behind, not "unknown"
    answer.update(copy=None, sender={"state": "streaming", "applied": 5000.0, "own": None, "application": "omniacli-replica"})
    primary.sql.answers["pg_stat_progress_basebackup"] = json.dumps(answer)
    seen = managedreplica.state("17", now=1010.0)
    assert seen["connected"] and seen["lag"] == 0 and managedreplica.verdict(seen)["code"] == "sync"
    # the primary writes on and the replica applies nothing: behind by the age of what it has not applied
    answer.update(current=9000.0)
    primary.sql.answers["pg_stat_progress_basebackup"] = json.dumps(answer)
    assert managedreplica.state("17", now=1020.0)["lag"] == 0  # seen now for the first time
    seen = managedreplica.state("17", now=1065.0)
    assert seen["lag"] == 45 and managedreplica.verdict(seen) == {
        "code": "behind", "lag": 45, "since": None, "held": 0, "show_held": False, "warn": False}
    # not connected: since when, as PostgreSQL says it, and what the slot holds
    answer.update(sender=None, slot={"active": False, "held": 3.2 * GB, "wal_status": "extended",
                                     "inactive_since": "2026-11-22 10:40:00+00"})
    primary.sql.answers["pg_stat_progress_basebackup"] = json.dumps(answer)
    seen = managedreplica.state("17", now=1100.0)
    found = managedreplica.verdict(seen)
    assert not seen["connected"] and found["code"] == "off" and found["show_held"] and found["warn"]
    assert found["held"] == int(3.2 * GB) and datetime.datetime.fromisoformat(found["since"])
    assert seen["cap"] == 20 * GB and seen["free"] == 40 * GB
    # past its cap PostgreSQL lets the changes go: the replica is to be made again
    answer["slot"]["wal_status"] = "lost"
    primary.sql.answers["pg_stat_progress_basebackup"] = json.dumps(answer)
    assert managedreplica.verdict(managedreplica.state("17", now=1200.0))["code"] == "lost"


def test_the_seconds_behind_and_the_disk_held():
    samples = [[100.0, 10.0], [120.0, 20.0], [140.0, 30.0]]
    assert managedreplica.behind(samples, 150.0, 30.0, 30.0, 4.0) == 0
    assert managedreplica.behind(samples, 150.0, 30.0, 10.0, None) == 30  # what was written at 120 is not applied
    assert managedreplica.behind(samples, 150.0, 30.0, 25.0, 0.4) == 10
    assert managedreplica.behind([], 150.0, 30.0, 25.0, 2.6) == 3  # never less than PostgreSQL's own measure
    assert managedreplica.rate(samples, 150.0, 40.0) is None and managedreplica.rate([[0.0, 0.0]], 1800.0, 1000.0) == 2000
    shown = lambda **s: managedreplica.verdict({"exists": True, "state": "ready", **s})  # noqa: E731
    # under 1 GB and connected the disk held is not shown; from 1 GB on, and whenever it is not connected
    assert not shown(connected=True, lag=2, held=GB - 1)["show_held"]
    big = shown(connected=True, lag=2, held=GB)
    assert big["show_held"] and big["warn"] and big["code"] == "sync"
    assert shown(connected=True, lag=10)["code"] == "sync" and shown(connected=True, lag=11)["code"] == "behind"
    assert shown(connected=False, held=0)["show_held"] and managedreplica.verdict(None) == {"code": "none"}
    assert managedreplica.verdict({"exists": True, "state": "replaced", "replaced_at": "2026-11-22T11:02:00"})["code"] == "replaced"


def test_the_warning_out_of_the_panel():
    now = datetime.datetime(2026, 11, 22, 10, 51)
    off = {"exists": True, "state": "ready", "connected": False, "since": "2026-11-22T10:40:00", "held": int(3.2 * GB)}
    said = managedreplica.problem(off, "lab1", now)
    assert "lab1 is not connected since 2026-11-22 10:40" in said and "3.2 GB" in said
    # not before ten minutes
    assert managedreplica.problem(off, "lab1", datetime.datetime(2026, 11, 22, 10, 49)) == ""
    assert "does not take them fast enough" in managedreplica.problem(
        {"exists": True, "state": "ready", "connected": True, "lag": 3, "held": GB + 1}, "lab1", now)
    assert managedreplica.problem({"exists": True, "state": "ready", "connected": True, "lag": 3, "held": 5}, "lab1", now) == ""
    assert "lost its place" in managedreplica.problem(
        {"exists": True, "state": "ready", "connected": False, "wal_status": "lost"}, "lab1", now)
    for text in (said, managedreplica.problem({"exists": True, "state": "ready", "wal_status": "lost"}, "lab1", now)):
        for word in ("slot", "WAL", "standby", "streaming"):
            assert word not in text


def test_dropped_it_frees_what_it_held(primary):
    managedreplica.allow("17", SPEC)
    primary.sql.asked.clear()
    primary.sql.answers["restart_lsn"] = "205687016\n"
    done = managedreplica.drop("17")
    assert done["freed"] == 205687016 and done["link"] == "lab1"
    assert machinedb.replica(primary.project) is None and "omniacli.replica" not in primary.hba[-1]
    for part in ("pg_drop_replication_slot(slot_name)", 'DROP ROLE IF EXISTS "omniacli.replica"',
                 "RESET max_slot_wal_keep_size", "CHECKPOINT"):
        assert primary.sql.has(part), part
    assert machinedb.firewall_state() == {"omnidb17": ["203.0.113.7"]}  # the grant's address stays
    with pytest.raises(machinedb.ClusterError, match="no read replica"):
        managedreplica.drop("17")


def test_a_cluster_with_a_replica_is_not_taken_away_nor_closed(primary, monkeypatch):
    managedreplica.allow("17", SPEC)
    with pytest.raises(machinedb.ClusterError, match="drop the replica first"):
        machinedb.remove("17")
    (primary.project / "grants.json").write_text("{}")
    with pytest.raises(machinedb.ClusterError, match="follows the cluster through this port"):
        machinedb.listen_off("17")
    # replaced by its replica: stopped for good, it goes with what it holds
    ran = []
    monkeypatch.setattr(machinedb, "_run", lambda project, *args, **kw: ran.append(args) or type(
        "Done", (), {"stdout": "exited\n", "returncode": 0})())
    managedreplica.ready("17", {"host": "203.0.113.7", "port": 15433, "ca": CA})
    done = managedreplica.replaced("17")
    assert done["stopped"] and not done["already"] and ("stop", "-t", "120", "db") in ran
    assert machinedb.replica(primary.project)["state"] == "replaced"
    assert managedreplica.replaced("17")["already"] and managedreplica.target("17")["state"] == "replaced"
    with pytest.raises(machinedb.ClusterError, match="took this cluster's place"):
        managedreplica.drop("17")
    monkeypatch.setattr(machinedb.subprocess, "run", lambda *a, **k: type("Done", (), {"stdout": "", "returncode": 0})())
    monkeypatch.setattr(machinedb, "_docker", lambda: "docker")
    monkeypatch.setattr(machinedb, "_remove_units", lambda: None)
    assert machinedb.remove("17")["removed"] == str(primary.project) and not primary.project.exists()


# --- on the machine that hosts it ----------------------------------------------------------

def test_the_replica_is_a_hardened_standby_that_never_archives():
    text = managedreplica.compose_text("17", 997, 997, 15433, ["/var/lib/omniacli-storage/default"], "lab2.test", 15432)
    data = yaml.safe_load(text)
    db = data["services"]["db"]
    assert db["read_only"] is True and db["cap_drop"] == ["ALL"] and db["security_opt"] == ["no-new-privileges:true"]
    assert db["user"] == "997:997" and db["cgroup_parent"] == machinedb.SLICE and db["ports"] == ["0.0.0.0:15433:5432"]
    command = db["command"]
    for option in ("archive_mode=off", "hot_standby=on", "ssl=on", "listen_addresses=*", "primary_slot_name=omniacli_replica",
                   "unix_socket_permissions=0700", "ssl_min_protocol_version=TLSv1.2"):
        assert option in command, option
    info = next(c for c in command if c.startswith("primary_conninfo="))
    # its primary over TLS, the certificate verified against its CA and name; the password in a file, never here
    assert "sslmode=verify-full" in info and "sslrootcert=/etc/omniacli-db/primary-ca.crt" in info
    assert "host=lab2.test port=15432 user=omniacli.replica" in info and "passfile=/run/secrets/replication.pgpass" in info
    assert "password=" not in text
    # the storage of the primary is a folder of the replica's own: nothing of this machine is at that path
    assert "./roots/0:/var/lib/omniacli-storage/default" in db["volumes"] and "./data:/var/lib/postgresql/data" in db["volumes"]
    assert not any(v.startswith("/") for v in db["volumes"])
    # the superuser's socket is a tmpfs of the container
    assert any(t.startswith("/var/run/postgresql:") for t in db["tmpfs"])
    assert data["networks"]["db"]["driver_opts"]["com.docker.network.bridge.name"] == "omnirp17"
    with pytest.raises(machinedb.ClusterError):
        managedreplica.compose_text("17", 997, 997, 15433, ["/var/lib/../etc"], "lab2.test", 15432)
    with pytest.raises(machinedb.ClusterError):
        managedreplica.conninfo("lab2.test sslmode=disable", 15432)


def test_its_pg_hba_lets_the_primarys_machine_in_as_its_instances():
    text = managedreplica.hba_text(["beta_shop", "acme_mach"], "203.0.113.9", "lab2")
    lines = text.splitlines()
    assert 'hostssl "acme_mach",postgres "acme_mach" 203.0.113.9/32 scram-sha-256' in lines
    assert 'hostssl "beta_shop",postgres "beta_shop" 203.0.113.9/32 scram-sha-256' in lines
    # the superuser by the socket only; no line without TLS, no other address, the reject last
    assert "local all postgres trust" in lines and lines[-2:] == ["local all all reject", "host all all all reject"]
    assert [x for x in lines if x.startswith("host ")] == ["host all all all reject"]
    assert not any("replication" in x for x in lines)
    for bad in ("0.0.0.0", "10.0.0.0/8", ""):
        with pytest.raises(machinedb.ClusterError):
            managedreplica.hba_text(["acme_mach"], bad)
    with pytest.raises(machinedb.ClusterError):
        managedreplica.hba_text(["Bad Name"], "203.0.113.9")


def test_the_tablespaces_go_inside_the_replicas_folder(tmp_path):
    roots = ["/var/lib/omniacli-storage/default", "/mnt/second"]
    project = tmp_path / "replica17"
    assert managedreplica.under_roots("/var/lib/omniacli-storage/default/acme/.omniacli-db/ts", roots, project) == \
        project / "roots" / "0" / "acme" / ".omniacli-db" / "ts"
    assert managedreplica.under_roots("/mnt/second/beta/.omniacli-db/ts", roots, project) == \
        project / "roots" / "1" / "beta" / ".omniacli-db" / "ts"
    for outside in ("/etc/postgresql", "/var/lib/omniacli-storage/default/../../etc", "relative/path"):
        with pytest.raises(machinedb.ClusterError):
            managedreplica.under_roots(outside, roots, project)


def test_the_room_and_the_time_said_before():
    fits = managedreplica.room(142 * MB, 297 * GB, 2)
    assert fits["fits"] and fits["need"] == 142 * MB + 142 * MB // 5 + 256 * MB
    assert not managedreplica.room(142 * MB, int(1.8 * GB), 2)["fits"]
    assert managedreplica.seconds_expected(142 * MB) < 60 < managedreplica.seconds_expected(60 * GB)
    assert managedreplica.cap_gb("20") == 20.0 and managedreplica.reserve_gb("2,5") == 2.5
    for bad in ("0", "a lot", "-1"):
        with pytest.raises(machinedb.ClusterError):
            managedreplica.cap_gb(bad)


def test_why_it_does_not_receive_is_said_in_plain_words():
    assert managedreplica.reason_code('FATAL:  could not start WAL streaming: ERROR:  replication slot "omniacli_replica" '
                                      "does not exist") == "gone"
    assert managedreplica.reason_code("FATAL:  could not receive data from WAL stream: ERROR:  requested WAL segment "
                                      "0000000100000000000000A1 has already been removed") == "gone"
    assert managedreplica.reason_code('FATAL:  no pg_hba.conf entry for replication connection from host "203.0.113.7"') == "refused"
    assert managedreplica.reason_code("FATAL:  could not connect to the primary server: connection to server at "
                                      '"203.0.113.9", port 15432 failed: Connection refused') == "unreachable"
    assert managedreplica.reason_code("") == "unknown"


def test_prepared_only_with_the_room(primary, tmp_path, monkeypatch):
    monkeypatch.setattr(managedreplica.shutil, "disk_usage", lambda p: type("U", (), {"free": int(1.8 * GB)})())
    spec = {"major": "17", "storage": "default", "link": "lab2", "by": "carlo", "host": "lab2.test", "port": 15432,
            "ca": CA, "address": "203.0.113.7", "primary_address": "203.0.113.9", "roots": ["/var/lib/omniacli-storage/default"],
            "environments": ["acme_mach"], "tablespaces": [], "size": 142 * MB, "reserve_gb": 2,
            "password": "a-password-made-on-this-machine-0123"}
    with pytest.raises(machinedb.ClusterError, match="no room: the copy of the cluster of lab2 takes about 426 MB"):
        managedreplica.prepare(spec, tmp_path)
    assert not managedreplica.project_folder(tmp_path, "17").exists() and managedreplica.hosted() == {}
    for bad, why in (({"password": "short"}, "not of its form"), ({"ca": "not a certificate"}, "certificates in PEM"),
                     ({"port": 80}, "not the port"), ({"link": ""}, "one of its links")):
        with pytest.raises(machinedb.ClusterError, match=why):
            managedreplica.prepare({**spec, **bad}, tmp_path)


# --- an instance that reads from the replica ------------------------------------------------

def test_odoo_conf_never_has_an_empty_replica_and_verifies_it():
    template = stacktemplates.load_template(TEMPLATES / "odoo-19-machine")
    plain = stacktemplates.conf_text(template, "acme_mach", "x" * 20, [], db_template="omniacli.tpl.acme")
    assert "db_replica_host" not in plain and "db_sslmode" not in plain
    text = stacktemplates.conf_text(template, "acme_mach", "x" * 20, [], db_template="omniacli.tpl.acme",
                                    replica=("203.0.113.7", 15433), replica_verified=True)
    lines = text.splitlines()
    assert "db_replica_host = 203.0.113.7" in lines and "db_replica_port = 15433" in lines
    # Odoo reaches the replica with the database's own sslmode: verify-full, written once
    assert lines.count("db_sslmode = verify-full") == 1
    # a third party's replica, given at the creation of an instance on its database, as before
    remote = stacktemplates.load_template(TEMPLATES / "odoo-19-remote")
    given = stacktemplates.conf_text(remote, "erp", "x" * 20, [], replica=("db2.example.com", 5432), replica_verified=True)
    assert given.count("db_sslmode = verify-full") == 1
    assert workspacehelper._replica_of({"replica_read": "203.0.113.7:15433"}) == ("203.0.113.7", 15433)
    assert workspacehelper._replica_of({"replica_read": ""}) is None and workspacehelper._replica_of({}) is None
    assert "db_replica_host" in stacktemplates.LOCKED_OPTIONS and "db_replica_port" in stacktemplates.LOCKED_OPTIONS


def test_which_odoo_reads_from_a_replica():
    assert workspacehelper.odoo_major("odoo-18-machine") == 18 and workspacehelper.odoo_major("odoo-19") == 19
    assert workspacehelper.odoo_major("odoo-17-machine") == 17 and workspacehelper.odoo_major("odooplm-20-machine") == 20
    assert workspacehelper.odoo_major("custom") is None and workspacehelper.ODOO_WITH_REPLICA == 18


@pytest.fixture
def instance(tmp_path, monkeypatch):
    """The production of acme-mach, an instance on the machine's cluster, as root's helper finds it."""
    folder = tmp_path / "acme" / "mach"
    project = folder / "production"
    (project / "config").mkdir(parents=True)
    template = stacktemplates.load_template(TEMPLATES / "odoo-19-machine")
    (project / "compose.yaml").write_text(template.compose)
    (project / ".env").write_text("DB_CLUSTER=17\nDB_USER=acme_mach\nDB_PASSWORD=its-own-password-0123456789\n"
                                  "DB_SOCKET=/run/x\nSUBNET=10.213.2.0/24\n")
    (folder / "instance.json").write_text(json.dumps({"template": "odoo-19-machine", "version": "1", "database": "acme_mach"}))
    box = {"folder": folder, "project": project, "firewall": {}, "conf": [], "ran": [],
           "target": {"host": "172.30.0.11", "port": 15433, "ca": CA, "state": "ready", "link": "lab1"}}
    monkeypatch.setattr(workspacehelper, "_project", lambda name, storage, inst, role: project)
    monkeypatch.setattr(workspacehelper, "TEMPLATES", TEMPLATES)
    monkeypatch.setattr(workspacehelper, "template_named", lambda name: stacktemplates.load_template(TEMPLATES / name))
    monkeypatch.setattr(workspacehelper, "_write", lambda path, text, mode=0o644, uid=0, gid=0: Path(path).write_text(text))
    monkeypatch.setattr(workspacehelper, "db_remote_set", lambda path, entry: box["firewall"].__setitem__(str(path), entry))
    monkeypatch.setattr(workspacehelper, "conf", lambda *args: box["conf"].append(json.loads((folder / "instance.json").read_text())))
    monkeypatch.setattr(workspacehelper, "_dc", lambda p, *args, **kw: box["ran"].append(args))
    monkeypatch.setattr(workspacehelper.machinedb, "_folder", lambda path, mode, uid=0, gid=0: (path.mkdir(exist_ok=True), path)[1])
    monkeypatch.setattr(managedreplica, "target", lambda major: box["target"])
    return box


def test_an_instance_reads_from_the_replica_and_stops(instance):
    project, folder = instance["project"], instance["folder"]
    done = workspacehelper.machine_db_replica_read("acme", "default", "mach", "on")
    assert done["reads"] and done["private"]
    override = yaml.safe_load((project / "compose.override.yaml").read_text())
    # the replica's CA for libpq, in Odoo's container only; compose.yaml itself is not touched
    assert override == {"services": {"odoo": {"environment": {"PGSSLROOTCERT": "/etc/odoo/db-replica-ca.pem"}}}}
    assert (project / "config" / "db-replica-ca.pem").read_text() == CA
    assert (project / "compose.yaml").read_text() == stacktemplates.load_template(TEMPLATES / "odoo-19-machine").compose
    assert json.loads((folder / "instance.json").read_text())["replica_read"] == "172.30.0.11:15433"
    # the workspaces' firewall lets that one address and port through, from this project's network
    assert instance["firewall"][f"{project}#replica"] == {"subnet": "10.213.2.0/24", "ip": "172.30.0.11", "port": 15433}
    assert instance["conf"][-1]["replica_read"] == "172.30.0.11:15433"  # odoo.conf written again with it
    # off: as it was
    assert not workspacehelper.machine_db_replica_read("acme", "default", "mach", "off")["reads"]
    assert not (project / "compose.override.yaml").exists() and not (project / "config" / "db-replica-ca.pem").exists()
    assert "replica_read" not in json.loads((folder / "instance.json").read_text())
    assert instance["firewall"][f"{project}#replica"] is None and "replica_read" not in instance["conf"][-1]


def test_who_may_not_read_from_it(instance):
    project, folder = instance["project"], instance["folder"]
    # Odoo 17 has no replica: refused before anything is written
    (folder / "instance.json").write_text(json.dumps({"template": "odoo-17-machine", "database": "acme_mach"}))
    template = stacktemplates.load_template(TEMPLATES / "odoo-19-machine")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(workspacehelper, "template_named", lambda name: template)
        patch.setattr(workspacehelper, "odoo_major", lambda name: 17)
        with pytest.raises(HelperError, match="Odoo 17 cannot read from a replica: it can from 18 on"):
            workspacehelper.machine_db_replica_read("acme", "default", "mach", "on")
    assert not (project / "compose.override.yaml").exists() and not instance["conf"]
    (folder / "instance.json").write_text(json.dumps({"template": "odoo-19-machine", "database": "acme_mach"}))
    # an override that is not OmniaCLI's is never written over
    (project / "compose.override.yaml").write_text("services: {}\n")
    with pytest.raises(HelperError, match="is not OmniaCLI's"):
        workspacehelper.machine_db_replica_read("acme", "default", "mach", "on")
    (project / "compose.override.yaml").unlink()
    # an instance with a database of its own: the replica is the machine's cluster's
    (project / ".env").write_text("DB_PASSWORD=x\n")
    with pytest.raises(HelperError, match="not on the machine's cluster"):
        workspacehelper.machine_db_replica_read("acme", "default", "mach", "on")
    with pytest.raises(HelperError, match="on or off"):
        workspacehelper.machine_db_replica_read("acme", "default", "mach", "maybe")


def test_after_a_promotion_the_instance_takes_the_copy_as_its_database(instance):
    project, folder = instance["project"], instance["folder"]
    # not before the cluster is stopped
    with pytest.raises(HelperError, match="was not replaced"):
        workspacehelper.machine_db_replica_follow("acme", "default", "mach")
    workspacehelper.machine_db_replica_read("acme", "default", "mach", "on")
    instance["target"]["state"] = "replaced"
    done = workspacehelper.machine_db_replica_follow("acme", "default", "mach")
    assert done == {"already": False, "database": "acme_mach", "db_service": "remote", "host": "172.30.0.11", "port": 15433,
                    "template": "odoo-19-remote", "kept": str(project / ".before-promotion")}
    env = dict(line.split("=", 1) for line in (project / ".env").read_text().splitlines())
    # the role, the password and the database it had, reached over TLS as a database of another machine
    assert env["DB_REMOTE"] == "1" and env["DB_HOST"] == "172.30.0.11" and env["DB_PORT"] == "15433"
    assert env["DB_USER"] == "acme_mach" and env["DB_PASSWORD"] == "its-own-password-0123456789" and "DB_CLUSTER" not in env
    assert (project / "compose.yaml").read_text() == stacktemplates.load_template(TEMPLATES / "odoo-19-remote").compose
    assert (project / "config" / "db-ca.pem").read_text() == CA and not (project / "compose.override.yaml").exists()
    meta = json.loads((folder / "instance.json").read_text())
    assert meta["template"] == "odoo-19-remote" and meta["database_on"] == "172.30.0.11:15433"
    assert meta["database_link"] == "lab1" and meta["promoted_from"]["template"] == "odoo-19-machine" and "replica_read" not in meta
    assert instance["firewall"][str(project)] == {"subnet": "10.213.2.0/24", "ip": "172.30.0.11", "port": 15433}
    assert instance["firewall"][f"{project}#replica"] is None
    assert ("down", "--remove-orphans") in instance["ran"] and ("up", "-d", "--remove-orphans") in instance["ran"]
    # what it was is kept beside it; asked again, it is done
    assert (project / ".before-promotion" / "compose.yaml").is_file()
    assert composehelper.on_linked(project)  # a restore over it is refused, as on a linked cluster
    assert workspacehelper.machine_db_replica_follow("acme", "default", "mach")["already"]


# --- the two applications talking through the link -------------------------------------------

class Lab:
    """What the root helpers of the two machines would do, kept in memory:
    lab2 has the cluster (its project a folder here), lab1 hosts the replica."""

    def __init__(self, tmp_path):
        self.project = tmp_path / "pg17"
        self.project.mkdir()
        self.hosted_file = tmp_path / "machine-db-replica.json"
        self.calls, self.inputs, self.fail = [], {}, {}
        self.port = 15432
        self.connected, self.lag, self.held, self.since = True, 2, 0, None
        self.local = None  # lab1's own record of the copy
        self.reading = set()
        self.instances = {"acme_mach": {"workspace": "acme", "instance": "mach"},
                          "beta_shop": {"workspace": "beta", "instance": "shop"}}

    def record(self):
        try:
            return json.loads((self.project / "replica.json").read_text())
        except OSError:
            return None

    def save(self, rec):
        if rec is None:
            (self.project / "replica.json").unlink(missing_ok=True)
        else:
            (self.project / "replica.json").write_text(json.dumps(rec))

    def state(self):
        rec = self.record()
        if rec is None:
            return {"exists": False, "major": "17"}
        return {"exists": True, "major": "17", **rec, "connected": self.connected and rec["state"] == "ready",
                "lag": self.lag if self.connected else None, "since": self.since, "held": self.held, "cap": 20 * GB,
                "wal_status": "reserved", "progress": None, "rate": None, "free": 40 * GB,
                "environments": sorted(self.instances), "grants": ["acme_linked"],
                "instances": sorted(f"{e['workspace']}-{e['instance']}" for e in self.instances.values()),
                "tablespaces": ["/var/lib/omniacli-storage/default/acme/.omniacli-db/ts"]}

    def hosted(self):
        return {"exists": True, "major": "17", "storage": "default", "running": True, "receiving": self.local["state"] == "ready",
                "reason_code": "", "connections": 3, "max_connections": 300, "free": 297 * GB, "disk": 181 * MB,
                "size": 163 * MB, "copied": 181 * MB, "version": "17.12", "in_recovery": self.local["state"] != "promoted",
                **self.local}

    async def ask(self, *args, timeout=None, input=None):
        self.calls.append(args)
        if args[0] in self.fail:
            raise JobFailed(self.fail[args[0]])
        if args[0] == "machine-db-state":
            rec = self.record()
            return {"majors": ["17"], "clusters": [{
                "major": "17", "storage": "default", "project": str(self.project), "running": (rec or {}).get("state") != "replaced",
                "promised": 64, "port": self.port, "grants": {"acme_linked": {"address": "203.0.113.7", "link": "lab1"}},
                "environments": self.instances, "replica": {k: rec[k] for k in ("link", "state", "address")} if rec else None}]}
        if args[0] == "machine-db-replica-facts":
            rec = self.record()
            return {"major": "17", "running": True, "size": 142 * MB, "version": "17.12", "port": self.port,
                    "restarts": self.port is None, "why": "", "roots": ["/var/lib/omniacli-storage/default"],
                    "instances": ["acme-mach", "beta-shop"], "grants": ["acme_linked"],
                    "replica": {"link": rec["link"], "state": rec["state"]} if rec else None}
        if args[0] == "machine-db-replica-state":
            return self.state()
        if args[0] == "machine-db-replica-ready":
            spec = json.loads(input)
            self.save({**self.record(), "state": "ready", "host": spec["host"], "port": spec["port"], "ca": spec["ca"],
                       "ready_at": "2026-11-22T10:12:40", "size": spec.get("size")})
            return {"state": "ready", "host": spec["host"], "port": spec["port"]}
        if args[0] == "machine-db-replica-hosted":
            return self.hosted()
        if args[0] == "machine-db-replica-sync":
            self.inputs["sync"] = json.loads(input)
            return {"changed": ["pg_hba.conf"]}
        if args[0] == "machine-db-archive-state":
            return {"enabled": False, "bases": [], "room": {}}
        return {}

    async def helper(self, job, *args, timeout=None, input=None):
        self.calls.append(args)
        job.log("$ sudo -n omniacli workspace-helper " + " ".join(args))
        if args[0] in self.fail or args[:3] in self.fail:
            raise JobFailed(self.fail.get(args[0]) or self.fail[args[:3]])
        if args[0] == "machine-db-replica-allow":
            spec = self.inputs["allow"] = json.loads(input)
            if self.record():
                raise JobFailed("workspace-helper: the machine's PostgreSQL 17 has a read replica already")
            first, self.port = self.port is None, 15432
            self.save({"link": spec["link"], "by": spec["by"], "address": spec["address"], "at": "2026-11-22T10:12:00",
                       "cap_gb": spec["cap_gb"], "state": "copying"})
            return {"port": 15432, "restarted_s": 0.5 if first else None, "address": spec["address"], "cap_gb": spec["cap_gb"],
                    "ca": CA, "version": "17.12", "size": 142 * MB, "names": [spec["reached_as"]],
                    "roots": ["/var/lib/omniacli-storage/default"], "environments": sorted(self.instances),
                    "tablespaces": [], "serves": 2, "grants": ["acme_linked"], "firewall": [spec["address"]]}
        if args[0] == "machine-db-replica-make":
            if args[2] == "prepare":
                spec = self.inputs["prepare"] = json.loads(input)
                self.local = {"link": spec["link"], "by": spec["by"], "host": spec["host"], "port": spec["port"],
                              "address": spec["address"], "own_port": 15433, "primary_address": "203.0.113.9",
                              "state": "copying", "at": "2026-11-22T10:12:02", "expected": spec["size"], "synced": False}
                self.hosted_file.write_text(json.dumps({"17": {"storage": "default", "project": "/x/replica17", "link": spec["link"]}}))
                return {"port": 15433, "primary_address": "203.0.113.9", "address": spec["address"]}
            if args[2] == "copy":
                self.local["copy_seconds"] = 3.6
                return {"seconds": 3.6, "size": 181 * MB}
            self.local.update(state="ready", ready_at="2026-11-22T10:12:40", synced=True)
            return {"state": "ready", "port": 15433, "address": self.local["address"], "version": "17.12", "seconds": 1.7,
                    "ca": CA, "size": 181 * MB, "copy_seconds": 3.6}
        if args[0] == "machine-db-replica-remove":
            self.local = None
            self.hosted_file.unlink(missing_ok=True)
            return {"removed": "/x/replica17", "freed": 181 * MB, "link": "lab2"}
        if args[0] == "machine-db-replica-drop":
            rec = self.record()
            self.save(None)
            return {"dropped": True, "link": rec["link"], "address": rec["address"], "freed": self.held, "firewall": []}
        if args[0] == "machine-db-replica-read":
            (self.reading.add if args[4] == "on" else self.reading.discard)(args[3])
            self.meta(args[3], replica_read="203.0.113.7:15433" if args[4] == "on" else None)
            return {"reads": args[4] == "on"}
        if args[0] == "machine-db-replica-replaced":
            rec = self.record()
            already = rec["state"] == "replaced"
            self.save({**rec, "state": "replaced", "replaced_at": rec.get("replaced_at") or "2026-11-22T11:02:00"})
            return {"stopped": True, "already": already, "replaced_at": "2026-11-22T11:02:00", "seconds": 0.6,
                    "grants": ["acme_linked"], "environments": self.instances}
        if args[0] == "machine-db-replica-follow":
            self.meta(args[3], replica_read=None, promoted_from={"template": "odoo-19-machine", "at": "2026-11-22T11:02:05"},
                      database_on="203.0.113.7:15433", database_link="lab1", database_major="17")
            return {"already": False, "database": f"acme_{args[3]}", "db_service": "remote", "host": "203.0.113.7", "port": 15433}
        if args[0] == "machine-db-replica-promote":
            self.local.update(state="promoted", promoted_at="2026-11-22T11:02:03")
            return {"promoted_at": "2026-11-22T11:02:03", "already": False, "seconds": 0.4}
        if args[0] == "machine-db-replica-cap":
            self.save({**self.record(), "cap_gb": float(args[2])})
            return {"cap_gb": float(args[2])}
        if args[0] == "machine-db-remove":
            self.save(None)
            return {"removed": str(self.project), "user": "removed", "image": "kept"}
        raise AssertionError(args)

    def meta(self, instance, **change):
        path = self.storage / "acme" / instance / "instance.json"
        data = json.loads(path.read_text())
        for key, value in change.items():
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value
        path.write_text(json.dumps(data))

    def names(self):
        return [c[0] if c[0] != "machine-db-replica-make" else f"make-{c[2]}" for c in self.calls
                if c[0].startswith("machine-db-replica") and c[0] not in ("machine-db-replica-state", "machine-db-replica-hosted",
                                                                          "machine-db-replica-facts")]


@pytest.fixture
async def machines(tmp_path, monkeypatch):
    """lab2 has the machine's cluster with acme-mach on it (mario is acme's
    admin there) and lets lab1 in as an admin's link; lab1 reaches lab2
    through HTTPS (and, as plain, through HTTP) and hosts the replica."""
    lab = Lab(tmp_path)
    lab.storage = tmp_path / "storage"
    registry = tmp_path / "machine-db.json"
    registry.write_text(json.dumps({"17": {"project": str(lab.project), "storage": "default"}}))
    monkeypatch.setattr(machinedb, "REGISTRY", registry)
    monkeypatch.setattr(machinedb, "HOSTED_REPLICAS", lab.hosted_file)
    monkeypatch.setattr(workspaceops, "ask", lab.ask)
    monkeypatch.setattr(workspaceops, "_helper", lab.helper)
    monkeypatch.setattr(workspaceops, "storage_path", lambda ws: lab.storage)
    monkeypatch.setattr(linkcopy, "POLL_EVERY", 0.01)
    monkeypatch.setattr(linkedcluster, "measure", lambda host, ports, *a: {
        "ms": 0.4, "port": ports[0], "tries": 7, "address": "203.0.113.7"})
    free = {"bytes": 297 * GB}
    monkeypatch.setattr(managedreplica, "free_here", lambda storage: free["bytes"])
    answered = []

    async def odoo_answers(port, timeout):
        answered.append(port)
        return True, 4.0

    async def read(inst):
        if (lab.record() or {}).get("state") == "replaced":
            raise JobFailed("the database does not answer")
        return {"version": "17.12", "settings": [], "changed": [], "cluster_size": 142 * MB, "connections": 3}

    async def nothing(*args, **kwargs):
        return {}
    restarted = []

    async def restart(job, inst):
        restarted.append(inst.key)
    monkeypatch.setattr(linkedcluster, "odoo_answers", odoo_answers)
    monkeypatch.setattr(pgtuning, "read", read)
    monkeypatch.setattr(ops, "restart", restart)

    def settings_of(name, auth):
        conf = tmp_path / name
        conf.mkdir()
        if name == "lab1":
            (conf / "links.yaml").write_text(yaml.safe_dump({"links": {
                "lab2": {"url": "https://lab2.test", "token": LINK_TOKEN},
                "plain": {"url": "http://lab2.test", "token": LINK_TOKEN}}}))
        return parse_settings({"auth": auth, "backup_dir": str(conf / "backups"), "jobs_dir": str(conf / "jobs"),
                               "servers_file": "servers.yaml"}, base_dir=conf)
    holder = settings_of("lab2", {"users": {"admin": USER_HASH}, "links": {
        "lab1": {"token": hash_token(LINK_TOKEN), "role": "admin"}}})
    holder.storage_roots = {"default": str(lab.storage)}
    holder.set_web_access(Access(users={"mario": hash_password(PASSWORD, iterations=1000)}, roles={"mario": "workspace"}))
    holder.workspaces = {"acme": Workspace("acme", "ACME", storage="default", limits={"disk_gb": 10},
                                           members={"mario": "admin"})}
    lab2 = create_app(holder)
    asker = settings_of("lab1", {"users": {"admin": USER_HASH}})
    asker.storage_roots = {"default": str(tmp_path / "lab1-storage")}
    network = Network(lab2)
    lab1 = create_app(asker, link_transport=network)
    found = {t: stacktemplates.load_template(TEMPLATES / t) for t in ("odoo-19-machine", "odoo-19-remote")}
    monkeypatch.setattr(workspaceops, "templates", lambda: found)
    ports = iter(range(20000, 20100, 2))

    async def create_instance(job, ws, instance, tpl, values, setup=None, remote=None):
        folder = lab.storage / ws.name / instance
        (folder / "production").mkdir(parents=True)
        (folder / "instance.json").write_text(json.dumps({"template": tpl.name, "database": f"acme_{instance}"}))
        return {"type": "docker", "production": {
            "database": f"acme_{instance}", "odoo_service": "odoo", "db_service": machinedb.DB_SERVICE,
            "http_port": next(ports), "update_modules": True,
            "restart": {"type": "compose", "service": str(folder / "production"), "sudo": True}, "repositories": []}}
    monkeypatch.setattr(workspaceops, "create_instance", create_instance)
    monkeypatch.setattr(workspaceops, "publish", nothing)
    transport = httpx.ASGITransport(app=BasicAsSession(lab1))
    async with httpx.AsyncClient(transport=transport, base_url=f"http://{HOST}") as client:
        client.lab1, client.lab2, client.network, client.lab = lab1, lab2, network, lab
        client.free, client.restarted, client.answered, client.tmp = free, restarted, answered, tmp_path
        client.there = httpx.AsyncClient(transport=httpx.ASGITransport(app=BasicAsSession(lab2)), base_url="http://lab2.test")
        made = await client.there.post("/api/workspaces/acme/instances/create", headers=THERE,
                                       json={"instance": "mach", "template": "odoo-19-machine"})
        assert made.status_code == 200, made.text
        assert (await job_of(client.there, made.json()["job"]))["status"] == "success"
        lab.calls.clear()
        yield client
        await client.there.aclose()


THERE = {**ADMIN, "Origin": "http://lab2.test"}
MARIO = {"Authorization": "Basic " + __import__("base64").b64encode(f"mario:{PASSWORD}".encode()).decode()}
MAKE = {"link": "lab2", "major": "17", "address": "203.0.113.7", "cap_gb": 20, "reserve_gb": 2}


async def make(client, **change):
    resp = await client.post("/api/postgres/replica/make", headers=POST, json={**MAKE, **change})
    assert resp.status_code == 200, resp.text
    job = await job_of(client, resp.json()["job"])
    assert job["status"] == "success", job
    return job


def access_log(tmp_path, name):
    return [json.loads(line) for line in (tmp_path / name / "logs" / "access.jsonl").read_text().splitlines()]


async def test_what_is_said_before_the_click(machines):
    client = machines
    data = (await client.get("/api/links/lab2/postgres/replica", headers=ADMIN)).json()
    assert data["error"] is None and data["major"] == "17" and data["size"] == 142 * MB
    # whose data it holds, the room here, the time, the round trip, the address this machine is seen with there
    assert data["instances"] == ["acme-mach", "beta-shop"] and data["grants"] == ["acme_linked"]
    assert data["free"] == 297 * GB and data["need"] == managedreplica.room(142 * MB, 0, 0)["need"]
    assert data["seconds"] == managedreplica.seconds_expected(142 * MB) and data["ms"] == 0.4
    assert data["address"] == "203.0.113.7" and data["restarts"] is False and data["cap_gb"] == 20 and data["reserve_gb"] == 2
    # nothing was asked of that machine but to read
    assert all(r.method == "GET" for r in client.network.requests)
    # a plain-HTTP link carries none: what it answers says where the data comes from
    plain = (await client.get("/api/links/plain/postgres/replica", headers=ADMIN)).json()
    assert "plain HTTP" in plain["error"] and "HTTPS or SSH" in plain["error"]
    resp = await client.post("/api/postgres/replica/make", headers=POST, json={**MAKE, "link": "plain"})
    assert resp.status_code == 400 and "HTTPS or SSH" in resp.json()["error"]
    # that machine not answering is the reason, with the remedy
    client.network.down = True
    down = (await client.get("/api/links/lab2/postgres/replica", headers=ADMIN)).json()
    assert "lab2 does not answer" in down["error"] and "try again when it answers" in down["error"]
    client.network.down = False
    # the tab offers it: the links, and why not for each
    offer = (await client.get("/api/postgres", headers=ADMIN)).json()["replica_offer"]
    assert offer["hosted"] == "" and {l["name"]: bool(l["why"]) for l in offer["links"]} == {"lab2": False, "plain": True}
    assert (await client.get("/api/links/nobody/postgres/replica", headers=ADMIN)).status_code == 404
    # the machine's admins only, on both machines
    for path in ("/api/postgres/machine/17/replica/facts", "/api/postgres/machine/17/replica/state"):
        assert (await client.there.get(path, headers=MARIO)).status_code == 403


async def test_refused_before_anything_is_asked_to_change(machines):
    client = machines
    # no room: the room to leave free above what is free
    client.free["bytes"] = int(1.8 * GB)
    resp = await client.post("/api/postgres/replica/make", headers=POST, json=MAKE)
    assert resp.status_code == 507 and "No room: the copy of the cluster of lab2 takes about 426 MB" in resp.json()["error"]
    assert "2 GB must stay free" in resp.json()["error"]
    client.free["bytes"] = 297 * GB
    # a cluster that would be made again to open its port, not said
    client.lab.port = None
    resp = await client.post("/api/postgres/replica/make", headers=POST, json=MAKE)
    assert resp.status_code == 409 and "Confirm the restart" in resp.json()["error"]
    assert (await client.get("/api/links/lab2/postgres/replica", headers=ADMIN)).json()["restarts"] is True
    client.lab.port = 15432
    for bad in ({"address": "10.0.0.0/8"}, {"cap_gb": "0"}, {"major": "9"}, {"link": "nobody"}):
        assert (await client.post("/api/postgres/replica/make", headers=POST, json={**MAKE, **bad})).status_code in (400, 404)
    assert client.lab.names() == [] and all(r.method == "GET" for r in client.network.requests)
    # the cluster has a replica already, of another machine
    client.lab.save({"link": "lab3", "address": "203.0.113.8", "state": "ready", "at": "2026-11-22T09:00:00"})
    resp = await client.post("/api/postgres/replica/make", headers=POST, json=MAKE)
    assert resp.status_code == 400 and "has a read replica already, on lab3" in resp.json()["error"]
    assert "on lab3" in (await client.get("/api/links/lab2/postgres/replica", headers=ADMIN)).json()["error"]
    # which this machine may not touch, nor read
    assert (await client.get("/api/postgres", headers=ADMIN)).json()["replicas"] == []


async def test_a_replica_is_made_through_the_link(machines):
    client = machines
    job = await make(client)
    lab = client.lab
    # that machine lets this one in, then the copy, then the start, then it is told where the replica answers
    assert lab.names() == ["machine-db-replica-allow", "make-prepare", "make-copy", "make-start", "machine-db-replica-ready"]
    assert [s["id"] for s in job["result"]["progress"]["steps"]] == ["allow", "copy", "start"]
    allowed, prepared = lab.inputs["allow"], lab.inputs["prepare"]
    # the password is made here and stays here: that machine gets its verifier, root's helper here the password
    assert allowed["verifier"].startswith("SCRAM-SHA-256$") and "password" not in allowed
    assert allowed["link"] == "lab1" and allowed["by"] == "admin" and allowed["address"] == "203.0.113.7"
    assert allowed["reached_as"] == "lab2.test" and allowed["cap_gb"] == 20
    password = prepared["password"]
    assert len(password) >= 16 and prepared["host"] == "lab2.test" and prepared["port"] == 15432 and prepared["ca"] == CA
    assert prepared["environments"] == ["acme_mach", "beta_shop"] and prepared["reserve_gb"] == 2
    there = (await client.there.get("/api/jobs", headers=ADMIN)).text
    assert password not in json.dumps(job) and password not in there and allowed["verifier"] not in json.dumps(job)
    for name in ("lab1", "lab2"):
        assert password not in (client.tmp / name / "logs" / "access.jsonl").read_text()
    # in the access log of both machines, with the user and the link
    assert any(e.get("event") == "action" and "/api/postgres/replica/make" in e.get("detail", "") and e.get("name") == "admin"
               for e in access_log(client.tmp, "lab1"))
    assert any(e.get("event") == "action" and "/api/postgres/machine/17/replica" in e.get("detail", "") and e.get("name") == "lab1"
               and e.get("on_behalf") == "admin" for e in access_log(client.tmp, "lab2"))
    # the row of the machine that hosts it: the state on the row, what this machine and that one see
    data = (await client.get("/api/postgres", headers=ADMIN)).json()
    row = data["replicas"][0]
    assert row["link"] == "lab2" and row["here"]["state"] == "ready" and row["here"]["own_port"] == 15433
    assert row["primary"]["verdict"] == {"code": "sync", "lag": 2, "since": None, "held": 0, "show_held": False, "warn": False}
    assert row["primary"]["instances"] == ["acme-mach", "beta-shop"] and row["primary_error"] == ""
    assert data["replica_offer"]["hosted"] == "lab2"  # one replica hosted per machine
    resp = await client.post("/api/postgres/replica/make", headers=POST, json=MAKE)
    assert resp.status_code == 400 and "hosts a read replica already, of lab2" in resp.json()["error"]
    # the cluster's row of the machine that has it: who has the replica, its state, who reads from it
    cluster = next(r for r in (await client.there.get("/api/postgres", headers=ADMIN)).json()["rows"] if r["where"] == "machine")
    assert cluster["replica"]["link"] == "lab1" and cluster["replica"]["verdict"]["code"] == "sync"
    assert cluster["replica"]["readers"] == [] and cluster["replica"]["heard"]
    # that machine not answering becomes the reason in the row, with what it said last
    client.network.down = True
    row = (await client.get("/api/postgres", headers=ADMIN)).json()["replicas"][0]
    assert row["primary_error"] and row["primary"]["verdict"]["code"] == "sync" and row["primary_failed"]


async def test_what_fails_while_it_is_made_is_taken_back_on_both_machines(machines):
    client = machines
    client.lab.fail[("machine-db-replica-make", "17", "copy")] = "workspace-helper: the copy of the cluster of lab2 failed"
    resp = await client.post("/api/postgres/replica/make", headers=POST, json=MAKE)
    job = await job_of(client, resp.json()["job"])
    assert job["status"] == "failed" and "the copy of the cluster of lab2 failed" in job["error"]
    assert client.lab.names() == ["machine-db-replica-allow", "make-prepare", "make-copy", "machine-db-replica-remove",
                                  "machine-db-replica-drop"]
    assert client.lab.record() is None and client.lab.local is None
    assert (await client.get("/api/postgres", headers=ADMIN)).json()["replicas"] == []


async def read_from(client, on, headers=THERE):
    return await client.there.post("/api/servers/acme-mach/database/replica", headers=headers, json={"read": on})


async def test_an_instance_reads_from_the_replica(machines):
    client = machines
    # no replica: nothing to read from
    assert "has no read replica" in (await read_from(client, True)).json()["error"]
    await make(client)
    client.lab.calls.clear()
    # the machine's admins only
    assert (await read_from(client, True, {**MARIO, "Origin": "http://lab2.test"})).status_code == 403
    resp = await read_from(client, True)
    assert resp.status_code == 200, resp.text
    job = await job_of(client.there, resp.json()["job"])
    assert job["status"] == "success" and job["title"] == "Reads of acme-mach: also from a replica"
    assert ("machine-db-replica-read", "acme", "default", "mach", "on") in client.lab.calls
    assert client.restarted == ["acme-mach/production"]
    # the job is the workspace's: no machine, no address, no port in it
    text = json.dumps(job)
    for word in ("lab1", "203.0.113.7", "15433"):
        assert word not in text
    assert (await read_from(client, True)).status_code == 400  # it reads already
    # the page: the machine's admin reads on which machine, the workspace's admin only that it reads from a replica
    page = (await client.there.get("/servers/acme-mach?view=production", headers=ADMIN)).text
    assert "reads also from the replica on lab1 (203.0.113.7)" in page and 'data-db-replica="off"' in page
    customer = (await client.there.get("/servers/acme-mach?view=production", headers=MARIO)).text
    assert customer.count("Reads: also from a replica") == 1 and 'data-db-replica="' not in customer
    for word in ("lab1", "203.0.113.7", "15433", "replica_read"):
        assert word not in customer, word
    # nor in what the routes answer to it
    for path in ("/api/servers", "/api/map", "/api/instances", "/api/jobs", f"/api/jobs/{job['id']}"):
        answer = (await client.there.get(path, headers=MARIO)).text
        for word in ("lab1", "203.0.113.7", "15433"):
            assert word not in answer, (path, word)
    assert (await client.there.get("/api/postgres", headers=MARIO)).status_code == 403
    # the cluster's row lists who reads from it
    cluster = next(r for r in (await client.there.get("/api/postgres", headers=ADMIN)).json()["rows"] if r["where"] == "machine")
    assert cluster["replica"]["readers"] == ["acme-mach"]
    # and stops
    resp = await read_from(client, False)
    assert (await job_of(client.there, resp.json()["job"]))["title"] == "Reads of acme-mach: from its database only"
    assert client.lab.reading == set() and client.restarted == ["acme-mach/production"] * 2


async def test_the_reads_are_not_switched_on_when_they_cannot_be(machines, monkeypatch):
    client = machines
    await make(client)
    # a replica that is not connected: Odoo would read from the cluster anyway
    client.lab.connected, client.lab.since = False, "2026-11-22T10:40:00"
    resp = await read_from(client, True)
    assert resp.status_code == 400 and "The replica on lab1 is not connected since 2026-11-22 10:40" in resp.json()["error"]
    # the page has the button off, with why, once the look saw it
    await client.lab2.state.replica_look_own()
    page = (await client.there.get("/servers/acme-mach?view=production", headers=ADMIN)).text
    button = page[page.index('<span class="db-replica"'):page.index("</span>", page.index('data-db-replica="on"'))]
    assert "disabled" in button and "is not connected since 2026-11-22 10:40" in button
    client.lab.connected = True
    # Odoo 17 has no replica: said before, refused after
    monkeypatch.setattr(workspacehelper, "odoo_major", lambda name: 17)
    page = (await client.there.get("/servers/acme-mach?view=production", headers=ADMIN)).text
    button = page[page.index('<span class="db-replica"'):page.index("</span>", page.index('data-db-replica="on"'))]
    assert "disabled" in button and "Odoo 17 cannot read from a replica: it can from 18 on" in button
    assert "Odoo 17 cannot read from a replica" in (await read_from(client, True)).json()["error"]
    customer = (await client.there.get("/servers/acme-mach?view=production", headers=MARIO)).text
    assert "cannot read from a replica" not in customer and 'data-db-replica="' not in customer
    assert client.lab.reading == set()


async def test_dropped_from_the_machine_that_has_the_cluster(machines):
    client = machines
    await make(client)
    await job_of(client.there, (await read_from(client, True)).json()["job"])
    client.lab.held = 205 * MB
    await client.lab1.state.replica_look_hosted()  # the machine that hosts it asks this one at each look
    client.lab.calls.clear()
    resp = await client.there.post("/api/postgres/machine/17/replica/drop", headers=THERE)
    job = await job_of(client.there, resp.json()["job"])
    assert job["status"] == "success", job
    # its instances that read go back to their cluster and restart first, then what the cluster kept goes
    names = [c[0] for c in client.lab.calls if c[0] in ("machine-db-replica-read", "machine-db-replica-drop")]
    assert names == ["machine-db-replica-read", "machine-db-replica-drop"] and client.lab.reading == set()
    assert "205 MB of them, freed" in "\n".join(job["lines"]) and client.lab.record() is None
    # the machine that hosts it asked a moment ago: it deletes its copy at its next look
    assert "it deletes its copy at its next look" in "\n".join(job["lines"])
    cluster = next(r for r in (await client.there.get("/api/postgres", headers=ADMIN)).json()["rows"] if r["where"] == "machine")
    assert cluster["replica"] is None
    assert (await client.there.post("/api/postgres/machine/17/replica/drop", headers=THERE)).json() == {"ok": True, "gone": True}
    # ... and it does: its look reads that it was dropped there
    await client.lab1.state.replica_look_hosted()
    for _ in range(100):
        if client.lab.local is None:
            break
        await asyncio.sleep(0.02)
    assert client.lab.local is None and (await client.get("/api/postgres", headers=ADMIN)).json()["replicas"] == []
    assert any("dropped there" in (e.get("detail") or "") for e in access_log(client.tmp, "lab1"))


async def test_dropped_from_the_machine_that_hosts_it(machines):
    client = machines
    await make(client)
    await job_of(client.there, (await read_from(client, True)).json()["job"])
    client.lab.calls.clear()
    job = await job_of(client, (await client.post("/api/postgres/replica/17/drop", headers=POST)).json()["job"])
    assert job["status"] == "success", job
    names = [c[0] for c in client.lab.calls if c[0].startswith("machine-db-replica") and "state" not in c[0] and "hosted" not in c[0]]
    assert names == ["machine-db-replica-read", "machine-db-replica-drop", "machine-db-replica-remove"]
    assert "acme-mach went back to reading from their cluster" in "\n".join(job["lines"])
    assert client.lab.record() is None and client.lab.local is None
    # with that machine not answering the copy goes all the same, and what stays there is said
    await make(client)
    client.network.down = True
    job = await job_of(client, (await client.post("/api/postgres/replica/17/drop", headers=POST)).json()["job"])
    assert job["status"] == "success" and client.lab.local is None and client.lab.record() is not None
    said = " ".join(job["result"]["warnings"])
    assert "lab2 did not take its part back" in said and "the role of the replication" in said and "Drop the replica" in said
    # the machine that has the cluster drops its part: the other one has not asked for long, its copy stays, said
    client.network.down = False
    resp = await client.there.post("/api/postgres/machine/17/replica/drop", headers=THERE)
    job = await job_of(client.there, resp.json()["job"])
    assert job["status"] == "success" and client.lab.record() is None
    assert "lab1 does not answer: its copy (181 MB), if it is still there, stays until its admin drops it" in " ".join(
        job["result"]["warnings"])


async def test_the_look_follows_the_instances_of_the_primary(machines):
    client = machines
    await make(client)
    client.lab.calls.clear()
    await client.lab1.state.replica_look_hosted()
    assert client.lab.inputs["sync"] == {"environments": ["acme_mach", "beta_shop"],
                                         "tablespaces": ["/var/lib/omniacli-storage/default/acme/.omniacli-db/ts"]}
    # nothing changed: nothing written again
    client.lab.calls.clear()
    await client.lab1.state.replica_look_hosted()
    assert not any(c[0] == "machine-db-replica-sync" for c in client.lab.calls)
    client.lab.instances["acme_new"] = {"workspace": "acme", "instance": "new"}
    await client.lab1.state.replica_look_hosted()
    assert client.lab.inputs["sync"]["environments"] == ["acme_mach", "acme_new", "beta_shop"]


async def test_the_warning_and_what_is_to_look_at(machines):
    client = machines
    await make(client)
    client.lab.connected, client.lab.since, client.lab.held = False, "2020-01-01T10:40:00", int(3.2 * GB)
    await client.lab2.state.replica_look_own()
    watch = (await client.there.get("/api/map", headers=ADMIN)).json()["verdict"]["watch"]
    mine = [w for w in watch if w["kind"] == "replica"]
    assert len(mine) == 1 and "lab1 is not connected" in mine[0]["text"] and "3.2 GB" in mine[0]["text"]
    # the machine's admins only
    theirs = (await client.there.get("/api/map", headers=MARIO)).json()
    assert not any(w["kind"] == "replica" for w in theirs["verdict"]["watch"]) and "lab1" not in json.dumps(theirs)
    machine = (await client.there.get("/api/map", headers=ADMIN)).json()["machines"][0]
    assert machine["cluster"][0]["replica"]["machine"] == "lab1" and machine["cluster"][0]["replica"]["code"] == "off"
    # the machine that hosts it has it as a node of its own
    await client.lab1.state.replica_look_hosted()
    hosted = (await client.get("/api/map", headers=ADMIN)).json()["machines"][0]["replicas"]
    assert hosted == [{"major": "17", "link": "lab2", "lag": None, "code": "off"}]


async def promote(client, **payload):
    return await client.post("/api/postgres/replica/17/promote", headers=POST, json={"confirm": "lab2", **payload})


async def test_promoted_with_the_primarys_machine_answering(machines):
    client = machines
    await make(client)
    await job_of(client.there, (await read_from(client, True)).json()["job"])
    # the name of the machine typed, and nothing else
    assert "Write lab2 to confirm" in (await promote(client, confirm="lab1")).json()["error"]
    client.lab.calls.clear()
    resp = await promote(client)
    assert resp.status_code == 200, resp.text
    job = await job_of(client, resp.json()["job"])
    assert job["status"] == "success", job
    # that machine's PostgreSQL stopped first, then the copy takes the writes, then its instances are pointed here
    order = [c[0] for c in client.lab.calls if c[0] in ("machine-db-replica-replaced", "machine-db-replica-promote",
                                                        "machine-db-replica-follow")]
    assert order == ["machine-db-replica-replaced", "machine-db-replica-promote", "machine-db-replica-replaced",
                     "machine-db-replica-follow"]
    assert [s["id"] for s in job["result"]["progress"]["steps"]] == ["stop", "promote", "point"]
    # the databases that machine gave to others are in the copy, which lets them in: named, with what their
    # machines do by themselves (no warning: nothing is left without a database for good)
    assert any("acme_linked" in line and "each machine points its instance here by itself" in line for line in job["lines"])
    assert "warnings" not in job["result"]
    # the instance there: its database is on another machine now, as the servers file says
    inst = (await client.there.get("/api/servers", headers=ADMIN)).json()
    assert client.answered and "acme-mach" in json.dumps(inst)
    cluster = next(r for r in (await client.there.get("/api/postgres", headers=ADMIN)).json()["rows"] if r["where"] == "machine")
    assert cluster["state"] == {"code": "replaced", "at": "2026-11-22T11:02:00", "link": "lab1"} and cluster["using"] == []
    # here: promoted, never dropped, never promoted twice
    row = (await client.get("/api/postgres", headers=ADMIN)).json()["replicas"][0]
    assert row["here"]["state"] == "promoted" and row["fence"] is None
    assert "was promoted" in (await client.post("/api/postgres/replica/17/drop", headers=POST)).json()["error"]
    assert (await client.get("/api/map", headers=ADMIN)).json()["machines"][0]["replicas"][0]["code"] == "promoted"
    assert "not a whole copy" in (await promote(client)).json()["error"]
    # the workspace's admin there reads no address of the machine that has its database now
    customer = (await client.there.get("/servers/acme-mach?view=production", headers=MARIO)).text
    assert "203.0.113.7" not in customer and "lab1" not in customer and "on another machine" in customer
    assert "203.0.113.7" not in (await client.there.get("/api/map", headers=MARIO)).text
    # the stopped cluster is taken away, now that nothing uses it
    resp = await client.there.post("/api/postgres/machine/17/remove", headers=THERE)
    assert (await job_of(client.there, resp.json()["job"]))["status"] == "success"
    # in the access log of both machines
    assert any("/api/postgres/replica/17/promote" in (e.get("detail") or "") for e in access_log(client.tmp, "lab1"))
    assert any("/replica/replaced" in (e.get("detail") or "") and e.get("name") == "lab1" for e in access_log(client.tmp, "lab2"))


async def test_promoted_with_the_primarys_machine_not_answering(machines):
    client = machines
    await make(client)
    await client.get("/api/postgres", headers=ADMIN)  # what that machine said last: its lag, its grants
    client.network.down = True
    client.lab.calls.clear()
    # it does not answer: refused until whoever asks says that machine is really off
    resp = await promote(client)
    assert resp.status_code == 409 and "lab2 does not answer" in resp.json()["error"] and "really off" in resp.json()["error"]
    assert client.lab.local["state"] == "ready"
    job = await job_of(client, (await promote(client, offline=True)).json()["job"])
    assert job["status"] == "success" and client.lab.local["state"] == "promoted"
    assert [c[0] for c in client.lab.calls if "replaced" in c[0] or "follow" in c[0]] == []
    said = " ".join(job["result"]["warnings"])
    assert "its PostgreSQL was not stopped" in said and "tries every 10 s" in said and "acme_linked" in said
    # its steps say what it did: nothing was stopped, nothing pointed
    assert [s["id"] for s in job["result"]["progress"]["steps"]] == ["promote"]
    row = (await client.get("/api/postgres", headers=ADMIN)).json()["replicas"][0]
    assert row["fence"]["link"] == "lab2" and row["here"]["state"] == "promoted"
    # still silent: nothing happens; it answers again: its PostgreSQL is stopped and its instances pointed here
    await client.lab1.state.replica_look_hosted()
    assert client.lab.record()["state"] == "ready"
    client.network.down = False
    await client.lab1.state.replica_look_hosted()
    for _ in range(200):
        if (await client.get("/api/postgres", headers=ADMIN)).json()["replicas"][0]["fence"] is None:
            break
        await asyncio.sleep(0.02)
    order = [c[0] for c in client.lab.calls if "replaced" in c[0] or "follow" in c[0]]
    assert order == ["machine-db-replica-replaced", "machine-db-replica-follow"]
    assert client.lab.record()["state"] == "replaced"
    assert (await client.get("/api/postgres", headers=ADMIN)).json()["replicas"][0]["fence"] is None
    # a promoted copy is never deleted by a look, whatever that machine says
    await client.lab1.state.replica_look_hosted()
    assert client.lab.local["state"] == "promoted"


async def test_the_tab_has_the_replica_in_its_words(machines):
    client = machines
    page = (await client.get("/", headers=ADMIN)).text
    for part in ('id="pg-replica"', 'id="pg-replica-dialog"', "hostedState", "ownedState", "T.rp.promoteAsk",
                 "window.omniaTypeName(text, link, T.rp.promoteGo", "/postgres/replica"):
        assert part in page, part
    # never the names of the replication's own things in what the tab says (UX-010 §8)
    texts = page[page.index("rp: {"):page.index("const panel = document.querySelector")]
    for word in ("wal_level", "max_wal_senders", "slot", "primary_conninfo", "LSN", "standby", "streaming"):
        assert word not in texts, word
    # a workspace's admin has no Database tab
    assert 'id="pg-replica"' not in (await client.there.get("/", headers=MARIO)).text


async def test_after_a_promotion_the_pages_say_where_the_database_is(machines):
    """The screens after their review (UX-011, part 1): the replaced row says what it served and until
    when, Take the cluster away… names the databases other machines use, the instance's page names the
    machine of the copy to a machine's admin, Not here is one sentence for each place, the job's page
    writes MB and GB."""
    client = machines
    await make(client)
    job = await job_of(client, (await promote(client)).json()["job"])
    assert job["status"] == "success", job
    # the instance's page: the machine's admin reads which machine has the database and since when
    page = (await client.there.get("/servers/acme-mach?view=production", headers=ADMIN)).text
    assert "on the promoted replica on lab1 (203.0.113.7) · since 22/11/2026 11:02" in page
    assert 'href="/#database"' in page and "on 203.0.113.7:15433 · tuned there" not in page
    # a workspace's admin still reads no machine and no address
    customer = (await client.there.get("/servers/acme-mach?view=production", headers=MARIO)).text
    assert "promoted replica" not in customer and "203.0.113.7" not in customer and "lab1" not in customer
    # Not here: the instances on the promoted copy are named with its machine, for one sentence
    data = (await client.there.get("/api/postgres", headers=ADMIN)).json()
    assert [(r["server"], r["promoted"], r["host"]) for r in data["remote"]] == [("acme-mach", "lab1", "203.0.113.7:15433")]
    tab = (await client.there.get("/", headers=ADMIN)).text
    for part in ("notHere(data.remote)", "T.notHerePromotedMany", "T.listMore",
                 # the replaced row: what it served and until when, no line of a port no one reaches
                 "fill(T.served, {list: servedList(row), when: when(row.state.at)})", "if (!replaced) td.append(",
                 # Take the cluster away… names the databases other machines use: they are in the copy
                 "T.rp.removeReplacedLentOne : T.rp.removeReplacedLent"):
        assert part in tab, part
    # the job's page writes a size as every page does
    shown = (await client.get(f"/jobs/{job['id']}", headers=ADMIN)).text
    assert "window.sizeText(Math.max(0, b))" in shown and "window.sizeText(result.size)" in shown
    assert '" GiB"' not in shown and '" MiB"' not in shown and '" KiB"' not in shown
