# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The read replica of the dedicated database of one instance (UX-011 §D;
docs/adr/postgresql-della-macchina.md, AD-004 option A): the same code as
the cluster's replica, where the PostgreSQL is the db service of the
instance's compose project. Pure logic, with fakes: no PostgreSQL, no
docker, no root. P has the instance, R hosts the copy."""

import json
from pathlib import Path

import pytest
import yaml

from omniacli import composehelper, machinedb, managedreplica, stacktemplates, workspacehelper
from omniacli.composehelper import HelperError

from .test_app import AUTH, client  # noqa: F401 - the client fixture
from .test_managed_replica import CA, GB, MB, TEMPLATES, Sql

KEY = "acme.default.odoo"
SUBNET = "10.213.2.0/24"
VERIFIER = "SCRAM-SHA-256$4096:c2FsdHNhbHRzYWx0c2FsdA==$" + "A" * 43 + "=:" + "B" * 43 + "="
SPEC = {"address": "172.30.0.13", "reached_as": "172.30.0.14", "link": "lab3", "by": "carlo", "verifier": VERIFIER}


# --- the amounts and the names ----------------------------------------------------------

def test_the_cap_is_a_tenth_of_the_workspaces_disk_and_20_gb_at_most():
    # D11-1: 5 GB on a workspace of 50 GB; never more than 20 GB; in tenths of GB, rounded down
    assert managedreplica.dedicated_cap_gb(50 * GB) == 5.0
    assert managedreplica.dedicated_cap_gb(500 * GB) == 20.0
    assert managedreplica.dedicated_cap_gb(31526391808) == 2.9
    assert managedreplica.dedicated_cap_gb(GB // 2) == 0.1


def test_the_keys_of_a_dedicated_database_and_of_its_copy():
    assert managedreplica.is_dedicated(KEY) and not managedreplica.is_dedicated("17")
    assert managedreplica.dedicated_key("acme", "default", "odoo") == KEY
    for bad in ("acme.odoo", "acme/default/odoo", "../default.odoo", "acme.default.odoo.x", ""):
        assert not managedreplica.is_dedicated(bad)
    # on the machine that hosts it: d and a number, which is its network's too
    assert machinedb.hosted_key("d100") == "d100" and machinedb.hosted_key("17") == "17"
    assert machinedb.replica_bridge("d100") == "omnirp100" and machinedb.replica_bridge("17") == "omnirp17"
    assert machinedb.BRIDGE.fullmatch("omnirp100")
    assert managedreplica.project_folder(Path("/s"), "d100") == Path("/s/.machine-db/replica-d100")
    assert managedreplica.project_folder(Path("/s"), "17") == Path("/s/.machine-db/replica17")
    assert managedreplica.project_name("d100") == "omniacli-machine-db-replica-d100"
    assert managedreplica.next_hosted_key({}) == "d100" and managedreplica.next_hosted_key({"d100": {}, "17": {}}) == "d101"
    for bad in ("d1", "d1000", "x100", "d100/..", "16"):
        with pytest.raises(machinedb.ClusterError):
            machinedb.hosted_key(bad)


def test_the_hosted_replicas_keep_a_cluster_and_dedicated_copies(tmp_path, monkeypatch):
    kept = tmp_path / "machine-db-replica.json"
    kept.write_text(json.dumps({"17": {"link": "lab2"}, "d100": {"link": "lab4", "source": KEY}, "bad": {}, "d7": {}}))
    monkeypatch.setattr(machinedb, "HOSTED_REPLICAS", kept)
    assert sorted(machinedb.hosted_replicas()) == ["17", "d100"]


# --- pg_hba.conf, the override, the firewall --------------------------------------------

def test_pg_hba_of_a_dedicated_database_lets_the_copys_machine_in_for_the_replication_only():
    text = managedreplica.dedicated_hba(SUBNET, {"address": "172.30.0.13", "state": "ready"})
    lines = text.splitlines()
    # the project's own network as before (Odoo), the replication from one address over TLS, then nothing
    assert f"host all all {SUBNET} scram-sha-256" in lines
    assert 'hostssl replication "omniacli.replica" 172.30.0.13/32 scram-sha-256' in lines
    assert lines[-1] == "host all all all reject" and "local all all trust" in lines
    assert lines.index(f"host all all {SUBNET} scram-sha-256") < lines.index("host all all all reject")
    # no replica, or one that was promoted: no one comes in from another machine
    for rec in (None, {"address": "172.30.0.13", "state": "replaced"}):
        assert "172.30.0.13" not in managedreplica.dedicated_hba(SUBNET, rec)
    # never a network, never every address
    with pytest.raises(machinedb.ClusterError):
        managedreplica.dedicated_hba(SUBNET, {"address": "0.0.0.0", "state": "ready"})


def test_the_override_publishes_the_port_and_leaves_compose_yaml_alone():
    text = managedreplica.override_text(None, {"port": 15440})
    assert text.startswith(managedreplica.OVERRIDE_MARK)
    db = yaml.safe_load(text)["services"]["db"]
    assert db["ports"] == ["0.0.0.0:15440:5432"]
    assert db["volumes"] == ["./.replica/conf:/etc/omniacli-db:ro"]
    command = db["command"]
    assert command[0] == "postgres" and "ssl=on" in command and "hba_file=/etc/omniacli-db/pg_hba.conf" in command
    assert "ssl_min_protocol_version=TLSv1.2" in command
    # an instance that reads from the replica too: both, in one file
    both = yaml.safe_load(managedreplica.override_text("odoo", {"port": 15440}))["services"]
    assert both["odoo"] == {"environment": {"PGSSLROOTCERT": "/etc/odoo/db-replica-ca.pem"}} and "db" in both
    # an instance of the cluster that reads: what the release before wrote, to the letter
    assert managedreplica.override_text("odoo", None) == workspacehelper.replica_override("odoo")
    assert workspacehelper.replica_override("odoo").startswith("# written by omniacli workspace-helper machine-db-replica-read")
    assert managedreplica.override_text(None, None) == ""


def test_the_override_says_which_service_reads(tmp_path):
    assert managedreplica.override_reads(tmp_path) is None
    (tmp_path / "compose.override.yaml").write_text(managedreplica.override_text("odoo", {"port": 15440}))
    assert managedreplica.override_reads(tmp_path) == "odoo"
    (tmp_path / "compose.override.yaml").write_text(managedreplica.override_text(None, {"port": 15440}))
    assert managedreplica.override_reads(tmp_path) is None
    (tmp_path / "compose.override.yaml").write_text("services: {}\n")
    with pytest.raises(machinedb.ClusterError, match="is not OmniaCLI's"):
        managedreplica.override_reads(tmp_path)


def test_the_firewall_lets_one_address_reach_the_published_port():
    rules = machinedb.firewall_rules({"port15440": {"from": ["172.30.0.13"], "subnet": SUBNET}})
    made_to = ["-d", SUBNET, "-p", "tcp", "-m", "conntrack", "--ctorigdstport", "15440"]
    assert rules["forward"] == [[*made_to, "-s", "172.30.0.13/32", "-j", "RETURN"], [*made_to, "-j", "DROP"]]
    # nothing of the project's network is closed: only the connections made to that port of this machine
    assert rules["input"] == []
    # the replica dropped, the database still listening: from no address
    assert machinedb.firewall_rules({"port15440": {"from": [], "subnet": SUBNET}})["forward"] == [[*made_to, "-j", "DROP"]]
    for bad in ({"port15440": {"from": ["0.0.0.0"], "subnet": SUBNET}}, {"port15440": {"from": [], "subnet": "0.0.0.0/0"}},
                {"port15440": {"from": [], "subnet": "10.0.0.0/8"}}, {"port80": {"from": [], "subnet": SUBNET}},
                {"port15440": ["172.30.0.13"]}):
        with pytest.raises(machinedb.ClusterError):
            machinedb.firewall_rules(bad)
    # beside a cluster's and a hosted copy's entries
    mixed = machinedb.firewall_rules({"omnidb17": ["172.30.0.13"], "port15440": {"from": ["172.30.0.13"], "subnet": SUBNET}})
    assert [*made_to, "-j", "DROP"] in mixed["forward"] and ["-o", "omnidb17", "-j", "DROP"] in mixed["forward"]


def test_the_firewall_file_keeps_a_published_port(tmp_path, monkeypatch):
    kept = tmp_path / "db-listen.json"
    monkeypatch.setattr(workspacehelper, "DB_LISTEN", kept)
    kept.write_text(json.dumps({"port15440": {"from": ["172.30.0.13"], "subnet": SUBNET},
                                "port15441": {"from": ["not an address"], "subnet": SUBNET},
                                "port15442": {"from": [], "subnet": "8.8.8.0/24"}}))
    assert workspacehelper.db_listen_file() == {"port15440": {"from": ["172.30.0.13"], "subnet": SUBNET}}


# --- on the machine that has the instance (P) ----------------------------------------------

class Compose:
    """docker compose of the instance's project, kept in memory: its psql answers as Sql, the rest is recorded."""

    def __init__(self, sql):
        self.sql, self.ran, self.state = sql, [], "running"

    def __call__(self, project, *args, input=None, check=True, stdout=None, timeout=None):
        done = type("Done", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        if "psql" in args:
            try:
                done.stdout = self.sql(project, "postgres", input)
            except machinedb.ClusterError as exc:
                done.returncode, done.stderr = 1, str(exc)
            return done
        self.ran.append(args)
        if args[0] == "stop":
            self.state = "exited"
        if args[0] == "ps":
            done.stdout = self.state + "\n"
        return done


@pytest.fixture
def dedicated(tmp_path, monkeypatch):
    """The production of acme-odoo, an instance with a database of its own, as root's helper finds it."""
    project = tmp_path / "acme" / "odoo" / "production"
    (project / "data" / "pgdata").mkdir(parents=True)
    (project / "config").mkdir()
    (project / "data" / "pgdata" / "PG_VERSION").write_text("17\n")
    compose = stacktemplates.load_template(TEMPLATES / "odoo-19").compose
    (project / "compose.yaml").write_text(compose)
    (project / ".env").write_text(f"PROJECT=acme-odoo-production\nDB_PASSWORD=its-own-password-0123456789\nSUBNET={SUBNET}\n")
    (project.parent / "instance.json").write_text(json.dumps({"template": "odoo-19", "version": "1", "database": "odoo"}))
    sql = Sql({"current_setting('wal_level')": "replica|10|10\n", "pg_database_size": "46114344|17.12\n",
               "pg_tablespace_location": "", "pg_hba_file_rules": "0\n"})
    run = Compose(sql)
    monkeypatch.setattr(managedreplica, "dedicated_project", lambda ws, storage, inst: project)
    monkeypatch.setattr(machinedb, "_run", run)
    monkeypatch.setattr(machinedb, "write", lambda path, text, mode=0o644, uid=0, gid=0: Path(path).write_text(text))
    monkeypatch.setattr(machinedb, "_folder", lambda path, mode, uid=0, gid=0: (path.mkdir(parents=True, exist_ok=True), path)[1])
    monkeypatch.setattr(machinedb, "DEDICATED_SOURCES", tmp_path / "dedicated-replica.json")
    monkeypatch.setattr(machinedb, "REGISTRY", tmp_path / "machine-db.json")
    monkeypatch.setattr(machinedb, "pick_port", lambda taken=frozenset(), free=None: 15440)
    names = []

    def ensure_tls(folder, major, wanted, owner=None):
        new = [n for n in wanted if n not in names]
        names.extend(new)
        (folder / "tls").mkdir(exist_ok=True)
        (folder / "tls" / "ca.crt").write_text(CA)
        (folder / "conf" / "server.crt").write_text("")
        return bool(new)
    monkeypatch.setattr(machinedb, "ensure_tls", ensure_tls)
    monkeypatch.setattr(machinedb, "certificate_names", lambda pem: list(names))
    monkeypatch.setattr(managedreplica.shutil, "disk_usage", lambda p: type("U", (), {"total": 50 * GB, "free": 31 * GB})())
    monkeypatch.setattr(managedreplica.time, "sleep", lambda s: None)
    box = type("Dedicated", (), {})()
    box.project, box.sql, box.run, box.compose, box.names = project, sql, run, compose, names
    box.folder = project / ".replica"
    return box


def test_what_is_said_of_a_dedicated_database_before_its_replica(dedicated):
    facts = managedreplica.facts(KEY)
    assert facts["kind"] == "dedicated" and facts["key"] == KEY and facts["major"] == "17"
    # one customer's data: the workspace, the instance; its disk and what it keeps for the copy at most (D11-1)
    assert (facts["workspace"], facts["name"], facts["instances"]) == ("acme", "acme-odoo", ["acme-odoo"])
    assert facts["size"] == 46114344 and facts["disk_total"] == 50 * GB and facts["disk_free"] == 31 * GB
    assert facts["cap_gb"] == 5.0
    # its PostgreSQL is restarted to open a port over TLS, the first time: said, with the seconds
    assert facts["restarts"] is True and facts["port"] is None and facts["restart_s"] == managedreplica.DEDICATED_RESTART_S
    assert facts["replica"] is None and facts["why"] == ""
    # reading it made nothing
    assert not dedicated.folder.exists() and not (dedicated.project / "compose.override.yaml").exists()


def test_an_instance_without_a_database_of_its_own_has_no_replica_of_its_own(dedicated):
    (dedicated.project / "compose.yaml").write_text(stacktemplates.load_template(TEMPLATES / "odoo-19-machine").compose)
    with pytest.raises(machinedb.ClusterError, match="no database of its own"):
        managedreplica.facts(KEY)
    with pytest.raises(machinedb.ClusterError, match="no database of its own"):
        managedreplica.allow(KEY, {**SPEC, "restart": True})
    with pytest.raises(machinedb.ClusterError, match="not a PostgreSQL of this machine"):
        managedreplica.Dedicated("acme.odoo")


def test_the_restart_is_asked_before_anything_is_made(dedicated):
    with pytest.raises(machinedb.ClusterError, match="restarted once to open a port over TLS: acme-odoo does not answer"):
        managedreplica.allow(KEY, SPEC)
    assert not dedicated.folder.exists() and not (dedicated.project / "compose.override.yaml").exists()
    assert not dedicated.sql.has("CREATE ROLE") and dedicated.run.ran == []
    # more than a tenth of the customer's disk is never kept for the copy
    with pytest.raises(machinedb.ClusterError, match="5 GB at most"):
        managedreplica.allow(KEY, {**SPEC, "restart": True, "cap_gb": 20})
    assert not dedicated.sql.has("CREATE ROLE")


def test_the_machine_of_the_copy_is_let_in_to_a_dedicated_database(dedicated):
    made = managedreplica.allow(KEY, {**SPEC, "restart": True})
    project, folder = dedicated.project, dedicated.folder
    # its own service listens over TLS on a port of its own: an override, never a change of compose.yaml
    assert (project / "compose.yaml").read_text() == dedicated.compose
    override = yaml.safe_load((project / "compose.override.yaml").read_text())
    assert override["services"]["db"]["ports"] == ["0.0.0.0:15440:5432"] and list(override["services"]) == ["db"]
    # only the database's container is made again, Odoo's is not
    assert ("up", "-d", "--no-deps", "db") in dedicated.run.ran
    assert made["port"] == 15440 and made["restarted_s"] is not None and made["kind"] == "dedicated"
    # the certificate is for the name the copy's machine reaches this one by, and for db (Odoo, in the project)
    assert dedicated.names == ["172.30.0.14", "db"] and made["names"] == ["172.30.0.14", "db"] and made["ca"] == CA
    # the role with the verifier only, one slot, the cap (a tenth of 50 GB), one line of access
    assert dedicated.sql.has(f'CREATE ROLE "omniacli.replica" LOGIN REPLICATION') and dedicated.sql.has(VERIFIER)
    assert dedicated.sql.has("pg_create_physical_replication_slot('omniacli_replica', true)")
    assert dedicated.sql.has("max_slot_wal_keep_size = '5120MB'") and made["cap_gb"] == 5.0
    hba = (folder / "conf" / "pg_hba.conf").read_text()
    assert 'hostssl replication "omniacli.replica" 172.30.0.13/32 scram-sha-256' in hba and f"host all all {SUBNET}" in hba
    rec = machinedb.replica(folder)
    assert (rec["link"], rec["address"], rec["state"], rec["cap_gb"]) == ("lab3", "172.30.0.13", "copying", 5.0)
    # kept among those that listen: the firewall lets that one address reach that port
    assert list(machinedb.dedicated_sources()) == [KEY]
    assert managedreplica.published_state() == {"port15440": {"from": ["172.30.0.13"], "subnet": SUBNET}}
    assert managedreplica.firewall_state() == {"port15440": {"from": ["172.30.0.13"], "subnet": SUBNET}}
    # what its machine is told: whose it is, the role Odoo has in it; no secret
    assert (made["workspace"], made["name"], made["environments"], made["roots"]) == ("acme", "acme-odoo", ["odoo"], [])
    assert VERIFIER not in json.dumps(made) and "password" not in json.dumps(made).lower()
    # one replica per dedicated database
    with pytest.raises(machinedb.ClusterError, match="the PostgreSQL of acme-odoo has a read replica already, on lab3"):
        managedreplica.allow(KEY, {**SPEC, "link": "lab9", "restart": True})


def test_the_state_the_cap_and_the_drop_of_a_dedicated_database(dedicated):
    managedreplica.allow(KEY, {**SPEC, "restart": True})
    managedreplica.ready(KEY, {"host": "172.30.0.13", "port": 15432, "ca": CA, "seconds": 1.0, "size": 63 * MB})
    assert managedreplica.target(KEY) == {"host": "172.30.0.13", "port": 15432, "ca": CA, "state": "ready", "link": "lab3"}
    dedicated.sql.answers["pg_replication_slots s"] = json.dumps({
        "current": 1000, "size": 46114344, "copy": None,
        "slot": {"active": True, "held": 200 * MB, "wal_status": "reserved", "inactive_since": None, "pid": 9},
        "sender": {"state": "streaming", "applied": 1000, "own": 0.0, "application": "omniacli-replica"}}) + "\n"
    state = managedreplica.state(KEY, now=1000.0)
    assert state["kind"] == "dedicated" and state["connected"] and state["lag"] == 0 and state["held"] == 200 * MB
    # against its own cap, on its workspace's disk
    assert state["cap"] == 5 * GB and state["free"] == 31 * GB and state["instances"] == ["acme-odoo"]
    assert state["cap_max"] == 5.0  # what Change the cap… is refused above, before any job
    assert managedreplica.verdict(state)["code"] == "sync"
    # the cap changes within the tenth of the disk, never above
    assert managedreplica.set_cap(KEY, "2.5") == {"cap_gb": 2.5} and dedicated.sql.has("max_slot_wal_keep_size = '2560MB'")
    with pytest.raises(machinedb.ClusterError, match="5 GB at most"):
        managedreplica.set_cap(KEY, "6")
    # a database that listens for a copy is not unpublished under it
    with pytest.raises(machinedb.ClusterError, match="drop the replica first"):
        managedreplica.source(KEY).listen_off()
    restarts = len([a for a in dedicated.run.ran if a[0] == "up"])
    done = managedreplica.drop(KEY)
    assert done["dropped"] and done["link"] == "lab3"
    assert dedicated.sql.has("pg_drop_replication_slot") and dedicated.sql.has('DROP ROLE IF EXISTS "omniacli.replica"')
    assert dedicated.sql.has("RESET max_slot_wal_keep_size")
    assert machinedb.replica(dedicated.folder) is None
    # nothing restarted: it goes on listening, for no address (pg_hba.conf, the firewall), as a cluster does
    assert len([a for a in dedicated.run.ran if a[0] == "up"]) == restarts
    assert "172.30.0.13" not in (dedicated.folder / "conf" / "pg_hba.conf").read_text()
    assert managedreplica.published_state() == {"port15440": {"from": [], "subnet": SUBNET}}
    # the next replica finds the port open: no restart is asked
    assert managedreplica.facts(KEY)["restarts"] is False
    again = managedreplica.allow(KEY, SPEC)
    assert again["restarted_s"] is None and again["port"] == 15440
    managedreplica.drop(KEY)
    # by root, by hand: the port closed and the override gone, the database as its compose.yaml says it
    closed = managedreplica.source(KEY).listen_off()
    assert closed["closed"] == 15440 and not (dedicated.project / "compose.override.yaml").exists()
    assert not dedicated.folder.exists() and machinedb.dedicated_sources() == {} and managedreplica.published_state() == {}
    assert (dedicated.project / "compose.yaml").read_text() == dedicated.compose


def test_a_promotion_stops_that_database_only(dedicated):
    managedreplica.allow(KEY, {**SPEC, "restart": True})
    with pytest.raises(machinedb.ClusterError, match="not a whole copy yet"):
        managedreplica.replaced(KEY)
    managedreplica.ready(KEY, {"host": "172.30.0.13", "port": 15432, "ca": CA})
    done = managedreplica.replaced(KEY)
    assert done["stopped"] and not done["already"] and done["environments"] == {"odoo": {"workspace": "acme", "instance": "odoo"}}
    # the service db of that project, nothing else
    assert [a for a in dedicated.run.ran if a[0] == "stop"] == [("stop", "-t", "120", "db")]
    assert dedicated.sql.has("CHECKPOINT")
    rec = machinedb.replica(dedicated.folder)
    assert rec["state"] == "replaced" and rec["replaced_at"] == done["replaced_at"]
    # stopped: its port is no one's to reach, and the copy is not dropped from here
    assert managedreplica.published_state() == {}
    assert "172.30.0.13" not in managedreplica.dedicated_hba(SUBNET, rec)
    with pytest.raises(machinedb.ClusterError, match="not dropped from here"):
        managedreplica.drop(KEY)
    assert managedreplica.replaced(KEY)["already"] is True
    assert managedreplica.target(KEY)["state"] == "replaced"


def test_a_database_that_does_not_stop_is_not_replaced(dedicated, monkeypatch):
    managedreplica.allow(KEY, {**SPEC, "restart": True})
    managedreplica.ready(KEY, {"host": "172.30.0.13", "port": 15432, "ca": CA})
    monkeypatch.setattr(managedreplica.Dedicated, "stop", lambda self: False)
    with pytest.raises(machinedb.ClusterError, match="the PostgreSQL of acme-odoo did not stop: nothing is promoted"):
        managedreplica.replaced(KEY)
    assert machinedb.replica(dedicated.folder)["state"] == "ready"


def test_the_warning_out_of_the_panel_names_the_instances_database():
    state = {"exists": True, "state": "ready", "connected": True, "lag": 2, "held": 2 * GB, "wal_status": "reserved"}
    said = managedreplica.problem(state, "lab3", whose="the PostgreSQL of acme-odoo")
    assert said.startswith("the PostgreSQL of acme-odoo keeps 2.0 GB of changes for the read replica on lab3")
    # the cluster's words are the ones of before
    assert managedreplica.problem(state, "lab3").startswith("the machine's PostgreSQL keeps 2.0 GB")


# --- on the machine that hosts the copy (R) ----------------------------------------------------

def test_the_copy_lets_the_instances_role_in_to_its_database_whatever_its_name():
    text = managedreplica.hba_text(["odoo"], "172.30.0.14", "lab4", whole=True)
    assert 'hostssl all "odoo" 172.30.0.14/32 scram-sha-256' in text.splitlines()
    assert text.splitlines()[-2:] == ["local all all reject", "host all all all reject"]
    # a cluster's copy: each role to its own database, as before
    assert 'hostssl "acme_mach",postgres "acme_mach" 172.30.0.14/32 scram-sha-256' in managedreplica.hba_text(
        ["acme_mach"], "172.30.0.14", "lab4")
    rec = {"environments": ["odoo"], "primary_address": "172.30.0.14", "link": "lab4", "state": "ready",
           "source": {"key": KEY, "workspace": "acme", "name": "acme-odoo"}}
    assert 'hostssl all "odoo" 172.30.0.14/32 scram-sha-256' in managedreplica.hosted_hba(rec)


def test_the_copy_of_a_dedicated_database_is_a_project_of_its_own():
    text = managedreplica.compose_text("17", 990, 990, 15433, [], "172.30.0.14", 15440, key="d100")
    data = yaml.safe_load(text)
    assert data["name"] == "omniacli-machine-db-replica-d100"
    assert data["networks"]["db"]["driver_opts"]["com.docker.network.bridge.name"] == "omnirp100"
    db = data["services"]["db"]
    # the same hardened standby as a cluster's copy, of the PostgreSQL the instance has
    assert db["image"] == "postgres:17" and db["read_only"] is True and db["cap_drop"] == ["ALL"]
    assert db["ports"] == ["0.0.0.0:15433:5432"] and "hot_standby=on" in db["command"]
    assert any("host=172.30.0.14 port=15440" in part and "sslmode=verify-full" in part for part in db["command"])
    # a cluster's copy is named as before
    assert yaml.safe_load(managedreplica.compose_text("17", 990, 990, 15433, [], "lab2.test", 15432))["name"] == \
        "omniacli-machine-db-replica-17"


@pytest.fixture
def host(tmp_path, monkeypatch):
    """The machine that hosts the copies, root's part faked."""
    monkeypatch.setattr(machinedb, "HOSTED_REPLICAS", tmp_path / "machine-db-replica.json")
    monkeypatch.setattr(machinedb, "REGISTRY", tmp_path / "machine-db.json")
    monkeypatch.setattr(machinedb, "pg_user", lambda create=False: (990, 990))
    monkeypatch.setattr(machinedb, "SYSTEMD", tmp_path)
    (tmp_path / machinedb.SLICE).write_text("")
    monkeypatch.setattr(machinedb, "write", lambda path, text, mode=0o644, uid=0, gid=0: Path(path).write_text(text))
    monkeypatch.setattr(machinedb, "_folder", lambda path, mode, uid=0, gid=0: (path.mkdir(parents=True, exist_ok=True), path)[1])
    monkeypatch.setattr(machinedb, "ensure_tls", lambda project, major, names, owner=None: True)
    ports = iter(range(15432, 15440))
    monkeypatch.setattr(machinedb, "pick_port", lambda taken=frozenset(), free=None: next(p for p in ports if p not in taken))
    monkeypatch.setattr(managedreplica.shutil, "disk_usage", lambda p: type("U", (), {"free": 300 * GB})())
    return tmp_path


def prepared(spec=None):
    return {"major": "17", "key": "d100", "storage": "default", "link": "lab4", "by": "carlo", "host": "172.30.0.14",
            "port": 15440, "ca": CA, "address": "172.30.0.13", "roots": [], "environments": ["odoo"], "tablespaces": [],
            "size": 46 * MB, "reserve_gb": 2, "password": "a-password-made-on-this-machine-0123",
            "source": {"key": KEY, "workspace": "acme", "name": "acme-odoo"}, **(spec or {})}


def test_the_copy_is_prepared_beside_the_others(host):
    made = managedreplica.prepare(prepared(), host)
    project = host / ".machine-db" / "replica-d100"
    assert made["key"] == "d100" and made["project"] == str(project) and made["port"] == 15432
    rec = managedreplica.hosted_record(project)
    assert (rec["major"], rec["key"], rec["link"], rec["state"]) == ("17", "d100", "lab4", "copying")
    assert rec["source"] == {"key": KEY, "workspace": "acme", "name": "acme-odoo"} and rec["port"] == 15440
    assert managedreplica.hosted() == {"d100": {"storage": "default", "project": str(project), "link": "lab4",
                                                "source": KEY, "name": "acme-odoo", "workspace": "acme", "major": "17"}}
    compose = yaml.safe_load((project / "compose.yaml").read_text())
    assert compose["name"] == "omniacli-machine-db-replica-d100"
    assert 'hostssl all "odoo" 172.30.0.14/32 scram-sha-256' in (project / "conf" / "pg_hba.conf").read_text()
    # the password of the replication is in its file only
    assert "a-password-made-on-this-machine-0123" not in json.dumps(made)
    assert "a-password-made-on-this-machine-0123" not in (project / "replica.json").read_text()
    # reached from that machine only, going out to that database's port only
    assert managedreplica.firewall_state()["omnirp100"] == {"from": ["172.30.0.14"], "to": [["172.30.0.14", 15440]]}
    # one copy per dedicated database; another instance's beside it, on a port of its own
    with pytest.raises(machinedb.ClusterError, match="hosts a read replica of acme-odoo of lab4 already"):
        managedreplica.prepare(prepared({"key": "d101"}), host)
    other = managedreplica.prepare(prepared({"key": "d101", "source": {"key": "acme.default.shop", "workspace": "acme",
                                                                         "name": "acme-shop"}}), host)
    assert other["port"] == 15433 and sorted(managedreplica.hosted()) == ["d100", "d101"]
    assert managedreplica.next_hosted_key() == "d102"
    # and the copy of a cluster too: one of each kind does not exclude the other
    roots = ["/var/lib/omniacli-storage/default"]
    cluster = managedreplica.prepare({**prepared(), "key": None, "source": None, "link": "lab2", "host": "lab2.test",
                                      "primary_address": "172.30.0.12", "roots": roots}, host)
    assert cluster["key"] == "17" and (host / ".machine-db" / "replica17" / "compose.yaml").is_file()
    assert sorted(managedreplica.hosted()) == ["17", "d100", "d101"]


def test_what_a_copy_must_say_of_its_source(host):
    for bad in ({"source": None}, {"source": {"key": "17", "workspace": "acme", "name": "acme-odoo"}},
                {"source": {"key": KEY, "workspace": "../x", "name": "acme-odoo"}}):
        with pytest.raises(machinedb.ClusterError, match="names the instance it is of"):
            managedreplica.prepare(prepared(bad), host)
    with pytest.raises(machinedb.ClusterError):
        managedreplica.prepare(prepared({"key": "d1"}), host)
    assert managedreplica.hosted() == {}


def test_a_promoted_copy_of_a_dedicated_database_goes_out_nowhere(host):
    managedreplica.prepare(prepared(), host)
    project = host / ".machine-db" / "replica-d100"
    rec = managedreplica.hosted_record(project)
    managedreplica._save_hosted_record(project, {**rec, "state": "promoted"})
    # reached from the machine of the instance still (Odoo is there), following nothing
    assert managedreplica.firewall_state()["omnirp100"] == {"from": ["172.30.0.14"], "to": []}
    assert 'hostssl all "odoo" 172.30.0.14/32 scram-sha-256' in managedreplica.hosted_hba(managedreplica.hosted_record(project))


# --- the instance: its reads, and where it goes after a promotion --------------------------------

@pytest.fixture
def instance(dedicated, monkeypatch):
    """dedicated, as workspace-helper's read and follow find it; its database has a replica that answers."""
    project, folder = dedicated.project, dedicated.project.parent
    managedreplica.allow(KEY, {**SPEC, "restart": True})
    managedreplica.ready(KEY, {"host": "172.30.0.13", "port": 15432, "ca": CA})
    box = {"folder": folder, "project": project, "firewall": {}, "conf": [], "ran": [], "applied": 0, "box": dedicated}
    monkeypatch.setattr(workspacehelper, "_project", lambda name, storage, inst, role: project)
    monkeypatch.setattr(workspacehelper, "TEMPLATES", TEMPLATES)
    monkeypatch.setattr(workspacehelper, "template_named", lambda name: stacktemplates.load_template(TEMPLATES / name))
    monkeypatch.setattr(workspacehelper, "_write", lambda path, text, mode=0o644, uid=0, gid=0: Path(path).write_text(text))
    monkeypatch.setattr(workspacehelper, "db_remote_set", lambda path, entry: box["firewall"].__setitem__(str(path), entry))
    monkeypatch.setattr(workspacehelper, "db_firewall", lambda: box.__setitem__("applied", box["applied"] + 1) or {})
    monkeypatch.setattr(workspacehelper, "conf", lambda *args: box["conf"].append(json.loads((folder / "instance.json").read_text())))
    monkeypatch.setattr(workspacehelper, "_dc", lambda p, *args, **kw: box["ran"].append(args))
    return box


def test_an_instance_with_a_database_of_its_own_reads_from_its_replica(instance):
    project, folder = instance["project"], instance["folder"]
    done = workspacehelper.machine_db_replica_read("acme", "default", "odoo", "on")
    assert done == {"reads": True, "host": "172.30.0.13", "port": 15432, "private": True, "odoo_service": "odoo"}
    override = yaml.safe_load((project / "compose.override.yaml").read_text())["services"]
    # Odoo's part beside the database's own, which stays as it was
    assert override["odoo"] == {"environment": {"PGSSLROOTCERT": "/etc/odoo/db-replica-ca.pem"}}
    assert override["db"]["ports"] == ["0.0.0.0:15440:5432"]
    # db_sslmode holds for its own database too, reached as db: the CA of the replica and the database's own
    assert (project / "config" / "db-replica-ca.pem").read_text() == CA + CA and "db" in instance["box"].names
    assert json.loads((folder / "instance.json").read_text())["replica_read"] == "172.30.0.13:15432"
    assert instance["firewall"][f"{project}#replica"] == {"subnet": SUBNET, "ip": "172.30.0.13", "port": 15432}
    assert instance["conf"][-1]["replica_read"] == "172.30.0.13:15432"
    assert (project / "compose.yaml").read_text() == instance["box"].compose
    # db_replica_host and db_replica_port are never empty, and the replica is verified
    conf = stacktemplates.conf_text(stacktemplates.load_template(TEMPLATES / "odoo-19"), "odoo", "x" * 20, [],
                                    replica=("172.30.0.13", 15432), replica_verified=True)
    assert "db_replica_host = 172.30.0.13\n" in conf and "db_replica_port = 15432\n" in conf and "db_sslmode = verify-full\n" in conf
    # off: Odoo's part goes, the database's stays (the copy still follows it)
    assert not workspacehelper.machine_db_replica_read("acme", "default", "odoo", "off")["reads"]
    left = yaml.safe_load((project / "compose.override.yaml").read_text())["services"]
    assert list(left) == ["db"] and not (project / "config" / "db-replica-ca.pem").exists()
    assert "replica_read" not in json.loads((folder / "instance.json").read_text())
    assert instance["firewall"][f"{project}#replica"] is None


def test_an_instance_without_a_replica_does_not_read_from_one(instance):
    managedreplica.drop(KEY)
    with pytest.raises(HelperError, match="a database of its own with no read replica"):
        workspacehelper.machine_db_replica_read("acme", "default", "odoo", "on")
    assert "odoo" not in yaml.safe_load((instance["project"] / "compose.override.yaml").read_text())["services"]


def test_a_move_to_the_cluster_is_not_made_under_a_replica(instance):
    template = stacktemplates.load_template(TEMPLATES / "odoo-19")
    with pytest.raises(HelperError, match="has a read replica on lab3: drop the replica first"):
        workspacehelper._to_machine("acme", "default", instance["folder"].parent, "odoo", instance["folder"],
                                    instance["project"], {}, template)


def test_after_its_promotion_the_instance_takes_the_copy_as_its_database(instance):
    project, folder = instance["project"], instance["folder"]
    workspacehelper.machine_db_replica_read("acme", "default", "odoo", "on")
    with pytest.raises(HelperError, match="was not replaced by its replica: the instance stays on it"):
        workspacehelper.machine_db_replica_follow("acme", "default", "odoo")
    managedreplica.replaced(KEY)
    instance["ran"].clear()
    done = workspacehelper.machine_db_replica_follow("acme", "default", "odoo")
    assert (done["already"], done["database"], done["db_service"], done["host"], done["port"]) == (
        False, "odoo", "remote", "172.30.0.13", 15432)
    assert done["template"] == "odoo-19-remote"
    # its project is one on a database of another machine: no database service, TLS, the copy's CA
    assert (project / "compose.yaml").read_text() == stacktemplates.load_template(TEMPLATES / "odoo-19-remote").compose
    env = dict(line.split("=", 1) for line in (project / ".env").read_text().splitlines())
    assert (env["DB_REMOTE"], env["DB_HOST"], env["DB_PORT"], env["DB_USER"]) == ("1", "172.30.0.13", "15432", "odoo")
    assert env["DB_PASSWORD"] == "its-own-password-0123456789" and composehelper.on_remote(project) is not None
    assert (project / "config" / "db-ca.pem").read_text() == CA
    # the override that published its port is gone with the service, and it reads from no replica
    assert not (project / "compose.override.yaml").exists() and not (project / "config" / "db-replica-ca.pem").exists()
    meta = json.loads((folder / "instance.json").read_text())
    assert meta["template"] == "odoo-19-remote" and meta["database_on"] == "172.30.0.13:15432" and "replica_read" not in meta
    assert meta["promoted_from"]["template"] == "odoo-19" and meta["promoted_from"]["dedicated"] is True
    assert meta["database_link"] == "lab3" and meta["database_major"] == "17"
    # down, then up, of this project only; its network reaches that one address and port; the firewall forgets the port
    assert [a[0] for a in instance["ran"]] == ["down", "up"]
    assert instance["firewall"][str(project)] == {"subnet": SUBNET, "ip": "172.30.0.13", "port": 15432}
    assert instance["applied"] == 1
    # what it was is kept beside it, and its data stays until it is freed
    assert (project / ".before-promotion" / "compose.yaml").read_text() == instance["box"].compose
    assert (project / "data" / "pgdata" / "PG_VERSION").is_file()
    # asked again: done already
    assert workspacehelper.machine_db_replica_follow("acme", "default", "odoo")["already"] is True


def test_what_a_promotion_left_is_freed(instance, monkeypatch):
    project = instance["project"]
    managedreplica.replaced(KEY)
    workspacehelper.machine_db_replica_follow("acme", "default", "odoo")
    monkeypatch.setattr(workspacehelper, "workspace", lambda name, storage: (project, project.parent.parent))
    monkeypatch.setattr(workspacehelper, "mounted", lambda image, mount: True)
    done = workspacehelper.machine_db_purge("acme", "default", "odoo")
    assert str(project / "data" / "pgdata") in done["removed"] and not (project / "data" / "pgdata").exists()


# --- the map (UX-009) -------------------------------------------------------------------

async def test_the_map_draws_the_copy_of_an_instances_database_it_hosts(client, tmp_path, monkeypatch):
    """R: the copy of one instance's database is a node of the machine that
    hosts it, as a cluster's copy is, named by the instance it is of."""
    kept = tmp_path / "machine-db-replica.json"
    kept.write_text(json.dumps({"17": {"link": "lab2", "project": str(tmp_path / "r17")},
                                "d100": {"link": "lab1", "name": "acme-odoo", "source": KEY, "project": str(tmp_path / "rd100")}}))
    monkeypatch.setattr(machinedb, "HOSTED_REPLICAS", kept)
    resp = await client.get("/api/map", headers={"Authorization": AUTH})
    assert resp.status_code == 200, resp.text
    hosted = resp.json()["machines"][0]["replicas"]
    assert [(r["major"], r["link"], r["name"]) for r in hosted] == [("17", "lab2", None), ("d100", "lab1", "acme-odoo")]
    page = (Path(__file__).parent.parent / "omniacli" / "templates" / "_map.html").read_text(encoding="utf-8")
    assert "replicaOfInstance" in page and "copyAway" in page and "db.copy" in page
