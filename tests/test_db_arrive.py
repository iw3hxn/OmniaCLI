# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A dedicated database that arrives in a new instance of another machine
(ArrivePlace, docs/adr/AD-008-consegna-e-spostamento.md AD-010): what root
writes on the machine that receives (R) and on the source's (P). The
generalisation of the return of a dedicated database (DedicatedBack)
without its premise: a new instance, never started, with an empty data
folder; the source let in by its allow of today, stopped clean at the
switch, its last record waited for, the copy promoted and given the new
instance's own database name and password. Pure logic, with fakes."""
import json

import pytest
import yaml

from omniacli import machinedb, managedreplica, stacktemplates, workspacehelper
from omniacli.composehelper import HelperError

from .test_dedicated_replica import KEY, SPEC as ALLOW, dedicated  # noqa: F401 - the fixture
from .test_managed_replica import CA, MB, TEMPLATES

SPEC = {"link": "lab3", "by": "carlo", "host": "172.30.0.13", "port": 15433, "ca": CA, "size": 46 * MB,
        "password": "a-password-made-here-0123456789", "major": "17", "database": "src", "reserve_gb": 0}
OWN_PASSWORD = "the-instances-own-password-0123"


class Projects:
    """docker compose of the new instance's project and of the one its database arrives in, in memory."""

    def __init__(self, box):
        self.box, self.calls = box, []
        self.state = {"production": "exited", "arrive": "exited"}
        self.brought = "shared_buffers = '256MB'\nprimary_conninfo = 'of the source'\n"

    def __call__(self, folder, *args, input=None, check=True, stdout=None, timeout=None):
        if "psql" in args:
            return self.box.run(folder, *args, input=input, check=check, stdout=stdout, timeout=timeout)
        self.calls.append((folder.name, args))
        done = type("Done", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        if args[0] == "run":
            pgdata = self.box.project / "data" / "pgdata"
            (pgdata / "PG_VERSION").write_text("17\n")
            (pgdata / "postgresql.auto.conf").write_text(self.brought)
        elif args[0] == "up":
            self.state[folder.name] = "running"
        elif args[0] in ("down", "stop"):
            self.state[folder.name] = "exited"
        elif args[0] == "ps":
            running = self.state.get(folder.name) == "running"
            done.stdout = ("db\nodoo\n" if running else "") if "--services" in args else self.state.get(folder.name, "")
        return done


@pytest.fixture
def arriving(dedicated, tmp_path, monkeypatch):  # noqa: F811
    """acme-odoo as create makes it for a database that arrives: its data folder empty, never started."""
    project = dedicated.project
    (project / "data" / "pgdata" / "PG_VERSION").unlink()
    (project / ".env").write_text(f"{(project / '.env').read_text()}SLICE=omniacli_acme.slice\nDB_PASSWORD={OWN_PASSWORD}\n")
    monkeypatch.setattr(managedreplica, "ARRIVALS", tmp_path / "db-arrivals.json")
    projects = Projects(dedicated)
    monkeypatch.setattr(machinedb, "_run", projects)
    dedicated.projects = projects
    return dedicated


def test_a_database_arrives_only_in_an_instance_never_started(arriving):
    projects, pgdata = arriving.projects, arriving.project / "data" / "pgdata"
    projects.state["production"] = "running"
    with pytest.raises(machinedb.ClusterError, match="runs: a database arrives only in an instance that was never started"):
        managedreplica.arrive_prepare(KEY, SPEC)
    projects.state["production"] = "exited"
    (pgdata / "PG_VERSION").write_text("17\n")
    with pytest.raises(machinedb.ClusterError, match="data folder of acme-odoo is not empty"):
        managedreplica.arrive_prepare(KEY, SPEC)
    (pgdata / "PG_VERSION").unlink()
    # an instance on the machine's cluster has no data folder of its own to take a copy
    (arriving.project / "compose.yaml").write_text(stacktemplates.load_template(TEMPLATES / "odoo-19-machine").compose)
    with pytest.raises(machinedb.ClusterError, match="has no database of its own"):
        managedreplica.arrive_prepare(KEY, SPEC)
    # nothing was made by a refusal
    assert not (arriving.folder / "arrive").exists() and managedreplica.arrivals() == {}
    for bad in ({**SPEC, "link": "../x"}, {**SPEC, "port": 80}, {**SPEC, "password": "short"},
                {**SPEC, "database": "a b"}):
        with pytest.raises(machinedb.ClusterError):
            managedreplica.arrive_prepare(KEY, bad)


def test_a_database_arrives_follows_and_takes_the_writes_as_the_instances_own(arriving):
    project, projects, sql = arriving.project, arriving.projects, arriving.sql
    pgdata, runs = project / "data" / "pgdata", arriving.folder / "arrive"
    made = managedreplica.arrive_prepare(KEY, SPEC)
    assert made["net"] == 100 and made["name"] == "acme-odoo"
    # a project of its own in the new instance's .replica folder, in the slice of its workspace; no port
    compose = yaml.safe_load((runs / "compose.yaml").read_text())
    assert compose["name"] == "omniacli-arrive-acme-odoo" and "ports" not in compose["services"]["db"]
    assert compose["services"]["db"]["cgroup_parent"] == "omniacli_acme.slice"
    assert "../../data/pgdata:/var/lib/postgresql/data/pgdata" in compose["services"]["db"]["volumes"]
    assert "a-password-made-here" in (runs / "secret" / "replication.pgpass").read_text()
    back = json.loads((arriving.folder / "arrive.json").read_text())
    assert (back["state"], back["database"], back["source_database"], back["address"]) == ("copying", "odoo", "src", "172.30.0.13")
    assert "a-password-made-here" not in json.dumps(back) + json.dumps(made)
    # its network goes out to the source's port only, kept for the firewall of the boot
    assert managedreplica.arrivals() == {KEY: {"project": str(project), "workspace": "acme"}}
    assert managedreplica.arriving_state() == {"omnirt100": {"from": [], "to": [["172.30.0.13", 15433]]}}
    assert managedreplica.firewall_state()["omnirt100"] == {"from": [], "to": [["172.30.0.13", 15433]]}
    with pytest.raises(machinedb.ClusterError, match="an arrival is under way already"):
        managedreplica.arrive_prepare(KEY, SPEC)
    # the copy follows the source through the slot of its replica, as the replica of the source
    done = managedreplica.arrive_copy(KEY)
    command = " ".join(next(args for name, args in projects.calls if args[0] == "run"))
    assert done["size"] > 0 and "pg_basebackup -h 172.30.0.13 -p 15433" in command and "-S omniacli_replica" in command
    assert "PGAPPNAME=omniacli-replica-copy" in command and "PGSSLMODE=verify-full" in command
    auto = (pgdata / "postgresql.auto.conf").read_text()
    assert "application_name=omniacli-replica" in auto and "primary_slot_name = 'omniacli_replica'" in auto
    assert "of the source" not in auto and (pgdata / "standby.signal").is_file()
    sql.answers["pg_stat_wal_receiver"] = "streaming\n"
    assert managedreplica.arrive_start(KEY)["state"] == "ready" and projects.state["arrive"] == "running"
    sql.answers["pg_is_in_recovery"] = "t\n"
    sql.answers["pg_last_wal_replay_lsn();"] = "0/3000060\n"
    state = managedreplica.arrive_state(KEY)
    assert state["receiving"] and state["in_recovery"] and state["replayed"] == "0/3000060"
    with pytest.raises(machinedb.ClusterError, match="not a position"):
        managedreplica.arrive_wait(KEY, "x")
    sql.answers["pg_wal_lsn_diff(pg_last_wal_replay_lsn()"] = "t\n"
    assert managedreplica.arrive_wait(KEY, "0/3000028")["arrived"] is True
    # it takes the writes: the instance's own name of its database, its role's password as a verifier only
    sql.answers["pg_is_in_recovery"] = "f\n"
    sql.answers["FROM pg_database WHERE datname = 'src'"] = "1\n"
    promoted = managedreplica.arrive_promote(KEY)
    assert sql.has('ALTER DATABASE "src" RENAME TO "odoo"')
    verifier = next(s for s in sql.asked if "ALTER ROLE" in s and "PASSWORD" in s)
    assert '"odoo" PASSWORD \'SCRAM-SHA-256$' in verifier and OWN_PASSWORD not in "".join(sql.asked)
    # the project that followed is gone, the instance's own database service runs on the data
    assert not runs.exists() and managedreplica.arrivals() == {} and managedreplica.arriving_state() == {}
    assert ("production", ("up", "-d", "--no-deps", "db")) in projects.calls and projects.state["arrive"] == "exited"
    assert promoted["name"] == "acme-odoo" and json.loads((arriving.folder / "arrive.json").read_text())["state"] == "done"
    assert not (runs / "secret" / "replication.pgpass").exists()
    # asked again (the job did not end): done, nothing twice
    assert managedreplica.arrive_promote(KEY)["already"] is True
    with pytest.raises(machinedb.ClusterError, match="it is the database of acme-odoo now"):
        managedreplica.arrive_cancel(KEY)


def test_an_arrival_cancelled_leaves_the_instance_as_it_was_made(arriving):
    pgdata = arriving.project / "data" / "pgdata"
    managedreplica.arrive_prepare(KEY, SPEC)
    managedreplica.arrive_copy(KEY)
    assert any(pgdata.iterdir())
    done = managedreplica.arrive_cancel(KEY)
    assert done["cancelled"] and done["freed"] > 0 and list(pgdata.iterdir()) == [] and pgdata.is_dir()
    assert not (arriving.folder / "arrive").exists() and not (arriving.folder / "arrive.json").exists()
    assert managedreplica.arrivals() == {}
    # and it may arrive again
    assert managedreplica.arrive_prepare(KEY, SPEC)["net"] == 100


def test_the_helper_says_the_steps_and_the_firewall_follows(arriving, monkeypatch):
    applied = []
    monkeypatch.setattr(workspacehelper, "db_firewall", lambda: applied.append(1) or {})
    monkeypatch.setattr(workspacehelper.sys, "stdin", type("In", (), {"read": lambda self, n: json.dumps(SPEC)})())
    assert workspacehelper.machine_db_arrive(KEY, "prepare")["net"] == 100 and applied == [1]
    with pytest.raises(HelperError, match="prepare, copy, start, state, wait, promote, cancel, aside, unaside or purge"):
        workspacehelper.machine_db_arrive(KEY, "bogus")
    with pytest.raises(HelperError, match="machine-db-arrive <workspace>.<storage>.<instance>"):
        workspacehelper.machine_db_arrive("17", "state")
    assert workspacehelper.ACTIONS["machine-db-arrive"][1] == 2 and workspacehelper.ACTIONS["machine-db-replica-stop"][1] == 2


def test_the_stopped_copy_of_a_moved_instance_is_set_aside_for_its_return(arriving):
    """AD-015 «Torna su»: the data of the stopped copy here go aside, with its record of the copy that followed it,
    the database of the other machine arrives in their place; a return that goes back puts them back as they were,
    one that ends deletes them."""
    pgdata, folder = arriving.project / "data" / "pgdata", arriving.folder
    aside = pgdata.with_name("pgdata.aside")
    (pgdata / "PG_VERSION").write_text("17\n")
    (pgdata / "base").mkdir()
    (pgdata / "base" / "1").write_text("the copy of before the move")
    folder.mkdir(exist_ok=True)
    (folder / "replica.json").write_text(json.dumps({"link": "lab4", "address": "172.30.0.14", "state": "stopped"}))
    arriving.projects.state["production"] = "running"
    with pytest.raises(machinedb.ClusterError, match="runs: the data of a stopped copy only is set aside"):
        managedreplica.arrive_aside(KEY)
    arriving.projects.state["production"] = "exited"
    done = managedreplica.arrive_aside(KEY)
    assert done["aside"] and done["moved"] and done["bytes"] > 0
    assert list(pgdata.iterdir()) == [] and (aside / "base" / "1").is_file()
    assert not (folder / "replica.json").exists() and (folder / "replica.json.aside").is_file()
    # asked again: nothing more is set aside, the data aside are not touched
    assert managedreplica.arrive_aside(KEY)["moved"] is False and (aside / "base" / "1").is_file()
    # the database arrives in the empty folder
    managedreplica.arrive_prepare(KEY, SPEC)
    managedreplica.arrive_copy(KEY)
    assert any(pgdata.iterdir())
    # the return goes back: what arrived is deleted, the stopped copy is as it was, its record with it
    back = managedreplica.arrive_unaside(KEY)
    assert back["restored"] and back["freed"] > 0
    assert (pgdata / "base" / "1").read_text() == "the copy of before the move" and not aside.exists()
    assert json.loads((folder / "replica.json").read_text())["state"] == "stopped"
    assert not (folder / "arrive").exists() and not (folder / "arrive.json").exists() and managedreplica.arrivals() == {}
    # asked again (the return went back, then it is cancelled): the stopped copy is not touched
    assert managedreplica.arrive_unaside(KEY)["untouched"] is True
    assert (pgdata / "base" / "1").read_text() == "the copy of before the move"
    # set aside again, arrived, promoted: the data aside are deleted only once it took the writes
    managedreplica.arrive_aside(KEY)
    managedreplica.arrive_prepare(KEY, SPEC)
    managedreplica.arrive_copy(KEY)
    with pytest.raises(machinedb.ClusterError, match="has not taken the writes: what was set aside is kept"):
        managedreplica.arrive_purge(KEY)
    arriving.sql.answers["pg_stat_wal_receiver"] = "streaming\n"
    managedreplica.arrive_start(KEY)
    arriving.sql.answers["pg_is_in_recovery"] = "f\n"
    managedreplica.arrive_promote(KEY)
    purged = managedreplica.arrive_purge(KEY)
    assert purged["freed"] > 0 and not aside.exists() and not (folder / "replica.json.aside").exists()
    assert not (folder / "replica.json").exists() and not (folder / "arrive.json").exists()


def test_an_instance_made_again_for_its_return_is_emptied_when_it_goes_back(arriving):
    """With its copy deleted the instance is made here again: nothing is set aside, and a return that goes back
    leaves its data folder empty, as it was made."""
    pgdata = arriving.project / "data" / "pgdata"
    assert managedreplica.arrive_aside(KEY) == {"aside": False, "moved": False, "bytes": 0}
    managedreplica.arrive_prepare(KEY, SPEC)
    managedreplica.arrive_copy(KEY)
    done = managedreplica.arrive_unaside(KEY)
    assert done["restored"] is False and list(pgdata.iterdir()) == [] and pgdata.is_dir()
    assert managedreplica.arrive_prepare(KEY, SPEC)["net"] == 100


def test_a_database_of_the_cluster_is_emptied_for_the_dump_of_its_return(monkeypatch, tmp_path):
    asked = []
    monkeypatch.setattr(workspacehelper, "_database_place", lambda *a: (tmp_path, "acme_erp", {"major": "17", "role": "acme_erp"}))
    monkeypatch.setattr(workspacehelper, "_odoo_runs", lambda project: False)
    monkeypatch.setattr(machinedb, "project_of", lambda major: tmp_path)
    monkeypatch.setattr(machinedb, "sql", lambda project, db, q: asked.append((db, q)) or "42\n")
    assert workspacehelper.db_empty("acme", "default", "erp") == {"database": "acme_erp", "emptied": 42}
    assert "DROP SCHEMA IF EXISTS public CASCADE" in asked[-1][1] and asked[-1][0] == "acme_erp"
    monkeypatch.setattr(workspacehelper, "_odoo_runs", lambda project: True)
    with pytest.raises(HelperError, match="runs: a database is emptied only under an Odoo that is stopped"):
        workspacehelper.db_empty("acme", "default", "erp")
    monkeypatch.setattr(workspacehelper, "_database_place", lambda *a: (tmp_path, "odoo", None))
    with pytest.raises(HelperError, match="is its own: it is not emptied"):
        workspacehelper.db_empty("acme", "default", "erp")
    assert workspacehelper.ACTIONS["db-empty"][1] == 3


# --- on the source's machine (P) -------------------------------------------------------------

@pytest.fixture(autouse=True)
def services(request, monkeypatch):
    """docker compose ps --services of the source's project: the services that run (none once stopped)."""
    if "dedicated" not in request.fixturenames or "arriving" in request.fixturenames:
        return
    box = request.getfixturevalue("dedicated")
    run = box.run

    def answered(project, *args, **kwargs):
        if args[:1] == ("ps",) and "--services" in args:
            run.ran.append(args)
            return type("Done", (), {"returncode": 0, "stdout": "db\nodoo\n" if run.state == "running" else "",
                                     "stderr": ""})()
        return run(project, *args, **kwargs)
    monkeypatch.setattr(machinedb, "_run", answered)

def test_the_source_is_measured_from_the_start_stopped_clean_and_started_again(dedicated, monkeypatch):  # noqa: F811
    folder, sql = dedicated.folder, dedicated.sql
    managedreplica.allow(KEY, {**ALLOW, "restart": True, "arrives": True})
    rec = machinedb.replica(folder)
    assert rec["arrives"] is True and rec["state"] == "copying"
    # a copy that arrives never says ready: it is measured once it follows
    sql.answers["'current', pg_wal_lsn_diff"] = json.dumps({
        "current": 1000, "slot": {"active": True, "held": 0}, "sender": {
            "state": "streaming", "applied": 1000, "own": 0.0, "application": managedreplica.APPLICATION}}) + "\n"
    state = managedreplica.state(KEY, now=1000.0)
    assert state["connected"] is True and state["lag"] == 0 and state["arrives"] is True
    # the switch: the instance stopped clean (Odoo and its database), the position of its last record kept
    monkeypatch.setattr(managedreplica, "_control", lambda project: {
        "Database cluster state": "shut down", "Latest checkpoint location": "0/40035D8"})
    stopped = managedreplica.replica_stop(KEY)
    assert stopped["checkpoint"] == "0/40035D8" and not stopped["already"]
    assert ("stop", "-t", "120") in dedicated.run.ran
    assert machinedb.replica(folder)["state"] == "stopped"
    assert managedreplica.replica_stop(KEY)["already"] is True
    # stopped, it is read without asking it, and nothing drops it from under the switch
    assert managedreplica.state(KEY)["state"] == "stopped"
    with pytest.raises(machinedb.ClusterError, match="stopped for the switch to its copy"):
        managedreplica.drop(KEY)
    # the switch did not complete: it runs again, followed as before
    assert managedreplica.replica_resume(KEY)["resumed"] is True
    rec = machinedb.replica(folder)
    assert rec["state"] == "copying" and "checkpoint" not in rec and ("up", "-d") in dedicated.run.ran
    assert managedreplica.replica_resume(KEY)["resumed"] is False


def test_a_source_that_does_not_stop_clean_runs_again(dedicated, monkeypatch):  # noqa: F811
    managedreplica.allow(KEY, {**ALLOW, "restart": True, "arrives": True})
    monkeypatch.setattr(managedreplica, "_control", lambda project: {"Database cluster state": "in production"})
    with pytest.raises(machinedb.ClusterError, match="did not stop clean: it runs again and nothing changed"):
        managedreplica.replica_stop(KEY)
    assert machinedb.replica(dedicated.folder)["state"] == "copying" and ("up", "-d") in dedicated.run.ran


def test_what_the_source_says_before_an_arrival(dedicated):  # noqa: F811
    facts = managedreplica.facts(KEY)
    # the machine's architecture and the instance's template and database: what decides copy or dump
    assert facts["arch"] and facts["template"] == "odoo-19" and facts["database"] == "odoo"
