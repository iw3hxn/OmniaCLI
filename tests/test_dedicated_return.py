# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The return of the dedicated database of one instance after the promotion
of its replica (UX-011 §A for one instance; docs/adr/postgresql-della-macchina.md,
AD-004 option A): what root writes on the machine of the instance. The
database comes back on the instance's own data folder, run by a project of
its own that no command of the instance reaches, and is the instance's own
service again only when the return is complete. Pure logic, with fakes."""
import json

import pytest
import yaml

from omniacli import composehelper, machinedb, managedreplica, stacktemplates, workspacehelper
from omniacli.composehelper import HelperError

from .test_dedicated_replica import KEY, SUBNET, dedicated, instance  # noqa: F401 - the fixtures
from .test_managed_replica import CA, GB, MB, TEMPLATES

SPEC = {"link": "lab3", "by": "carlo", "host": "172.30.0.13", "port": 15432, "ca": CA, "size": 46 * MB,
        "password": "a-password-made-here-0123456789", "tablespaces": [], "reserve_gb": 0, "major": "17.12"}


@pytest.fixture
def promoted(instance, monkeypatch):  # noqa: F811
    """acme-odoo after the promotion of its replica: pointed at the copy on
    another machine, its own data still beside it; docker compose of the
    project its database comes back in kept in memory."""
    project = instance["project"]
    (project / ".env").write_text((project / ".env").read_text() + "SLICE=omniacli_acme.slice\n")
    managedreplica.replaced(KEY)
    workspacehelper.machine_db_replica_follow("acme", "default", "odoo")
    run, box = instance["box"].run, instance["box"]
    box.where, box.brought = [], "shared_buffers = '128MB'\nprimary_conninfo = 'of the copy'\n"

    def compose(folder, *args, **kwargs):
        box.where.append((folder.name, args[0]))
        if args[0] == "run":
            # what pg_basebackup brings: the promoted copy's own data and settings
            pgdata = project / "data" / "pgdata"
            (pgdata / "PG_VERSION").write_text("17\n")
            (pgdata / "postgresql.auto.conf").write_text(box.brought)
        if args[0] in ("down", "stop"):
            run.state = "exited"
        if args[0] == "up":
            run.state = "running"
        return run(folder, *args, **kwargs)
    monkeypatch.setattr(machinedb, "_run", compose)
    monkeypatch.setattr(workspacehelper, "db_remote_file", lambda: {})
    instance["ran"].clear()
    return instance


def test_the_project_a_database_comes_back_in_reaches_nothing_but_the_promoted_copy():
    data = yaml.safe_load(managedreplica.back_compose_text("17", 999, 999, "acme-odoo", "omniacli_acme.slice", 100))
    assert data["name"] == "omniacli-return-acme-odoo"
    db = data["services"]["db"]
    # the instance's own data folder, read only container, no capability, no port: nothing reaches it
    assert "ports" not in db and db["read_only"] is True and db["cap_drop"] == ["ALL"] and db["user"] == "999:999"
    assert "../../data/pgdata:/var/lib/postgresql/data/pgdata" in db["volumes"] and "listen_addresses=" in db["command"]
    # in the slice of the customer's workspace, and it never archives
    assert db["cgroup_parent"] == "omniacli_acme.slice" and "archive_mode=off" in db["command"]
    assert data["networks"]["back"]["driver_opts"]["com.docker.network.bridge.name"] == "omnirt100"
    assert machinedb.BRIDGE.fullmatch("omnirt100")
    for bad in (("17", "../x", "s.slice", 100), ("17", "acme-odoo", "a b", 100), ("17", "acme-odoo", "s.slice", 7)):
        with pytest.raises(machinedb.ClusterError):
            managedreplica.back_compose_text(bad[0], 999, 999, *bad[1:])


def test_only_a_database_a_promotion_took_out_of_its_project_comes_back(instance):  # noqa: F811
    with pytest.raises(machinedb.ClusterError, match="the database of acme-odoo was not replaced by its replica"):
        managedreplica.back_prepare(KEY, SPEC)
    managedreplica.replaced(KEY)
    # stopped, but the instance still has its database service: nothing was pointed at the copy
    with pytest.raises(machinedb.ClusterError, match="has its database service in its own project"):
        managedreplica.back_prepare(KEY, SPEC)
    assert machinedb.returning(instance["box"].folder) is None
    # its folder holds what brings it back: closing its port, by root, by hand, would delete it
    with pytest.raises(machinedb.ClusterError, match="was replaced by its replica on lab3: its port is not touched"):
        managedreplica.source(KEY).listen_off()
    assert machinedb.replica(instance["box"].folder)["state"] == "replaced"


def test_the_database_of_an_instance_comes_back_beside_it_and_is_its_own_again(promoted, monkeypatch):
    project, box = promoted["project"], promoted["box"]
    pgdata, folder, runs = project / "data" / "pgdata", box.folder, box.folder / "return"
    twin = stacktemplates.load_template(TEMPLATES / "odoo-19-remote").compose
    facts = managedreplica.back_facts(KEY)
    assert (facts["kind"], facts["name"], facts["workspace"], facts["machine"]) == ("dedicated", "acme-odoo", "acme", "lab3")
    assert facts["aside"]["size"] > 0 and facts["free"] == 31 * GB and facts["disks"] == []
    made = managedreplica.back_prepare(KEY, SPEC)
    # the data of before is set aside where it is; an empty folder takes its place
    assert (project / "data" / "pgdata.aside" / "PG_VERSION").read_text() == "17\n" and list(pgdata.iterdir()) == []
    assert made["aside"] == facts["aside"]
    # a project of its own, in the instance's .replica folder: the instance's own project is not touched
    compose = yaml.safe_load((runs / "compose.yaml").read_text())
    assert compose["name"] == "omniacli-return-acme-odoo" and compose["services"]["db"]["image"] == "postgres:17"
    assert compose["services"]["db"]["cgroup_parent"] == "omniacli_acme.slice"
    assert (project / "compose.yaml").read_text() == twin and not (project / "compose.override.yaml").exists()
    assert "a-password-made-here" in (runs / "secret" / "replication.pgpass").read_text()
    back = machinedb.returning(folder)
    assert (back["state"], back["net"], back["major"], back["made"], back["machine"]) == ("copying", 100, "17", [], "lab3")
    assert "a-password-made-here" not in json.dumps(back) + json.dumps(made)
    # its network goes out to the promoted copy's port, and nothing reaches it
    assert managedreplica.returning_state() == {"omnirt100": {"from": [], "to": [["172.30.0.13", 15432]]}}
    rules = [" ".join(r) for r in machinedb.firewall_rules(managedreplica.firewall_state())["forward"]]
    assert "-i omnirt100 -d 172.30.0.13/32 -p tcp --dport 15432 -j RETURN" in rules and "-o omnirt100 -j DROP" in rules
    # the data of before is never freed under a return, and a second return is not prepared
    monkeypatch.setattr(workspacehelper, "workspace", lambda name, storage: (project, project.parent.parent))
    monkeypatch.setattr(workspacehelper, "mounted", lambda image, mount: True)
    with pytest.raises(HelperError, match="the data of before is set aside for the return"):
        workspacehelper.machine_db_purge("acme", "default", "odoo")
    with pytest.raises(machinedb.ClusterError, match="a return is under way already"):
        managedreplica.back_prepare(KEY, SPEC)
    # the copy: in that project, over TLS with the certificate verified, the password from its file
    box.where.clear()
    box.run.ran.clear()
    done = managedreplica.back_copy(KEY)
    command = " ".join(next(a for a in box.run.ran if a[0] == "run"))
    assert box.where == [("return", "run")] and done["size"] > 0
    assert "pg_basebackup -h 172.30.0.13 -p 15432 -U omniacli.replica" in command and "-S omniacli_return" in command
    assert "PGSSLMODE=verify-full" in command and "a-password-made-here" not in command
    auto = (pgdata / "postgresql.auto.conf").read_text()
    # where it follows from, and never what the copy followed
    assert "application_name=omniacli-return" in auto and "of the copy" not in auto and (pgdata / "standby.signal").is_file()
    box.sql.answers["pg_stat_wal_receiver"] = "streaming\n"
    assert managedreplica.back_start(KEY)["state"] == "ready" and ("return", "up") in box.where
    box.sql.answers["pg_is_in_recovery"] = "t\n"
    state = managedreplica.back_state(KEY)
    assert state["state"] == "ready" and state["running"] and state["receiving"] and state["in_recovery"] is True
    # not written back before it took the writes
    with pytest.raises(HelperError, match="has not come back: odoo stays on the promoted copy"):
        workspacehelper.machine_db_replica_back("acme", "default", "odoo")
    box.sql.answers["pg_last_wal_replay_lsn"] = "t\n"
    assert managedreplica.back_wait(KEY, "0/3000028")["arrived"] is True
    box.sql.answers["pg_is_in_recovery"] = "f\n"
    managedreplica.back_promote(KEY)
    # no replica any more, what the return needed goes; the firewall lets its network out nowhere
    assert machinedb.replica(folder) is None and machinedb.returning(folder)["state"] == "done"
    assert not (runs / "secret" / "replication.pgpass").exists() and managedreplica.returning_state() == {}
    assert box.sql.has("ALTER SYSTEM RESET primary_conninfo") and box.sql.has('DROP ROLE IF EXISTS "omniacli.replica"')
    assert "172.30.0.13" not in (folder / "conf" / "pg_hba.conf").read_text()
    # the instance written back: the project its database came back in is stopped clean first and goes
    box.where.clear()
    promoted["ran"].clear()
    applied = promoted["applied"]
    done = workspacehelper.machine_db_replica_back("acme", "default", "odoo")
    assert (done["already"], done["database"], done["db_service"], done["template"], done["listens"]) == (
        False, "odoo", "db", "odoo-19", True)
    assert ("return", "down") in box.where and not runs.exists() and box.sql.has("CHECKPOINT")
    # its own project is the one it had before the promotion, on the data that came back
    assert (project / "compose.yaml").read_text() == box.compose and composehelper.on_remote(project) is None
    env = dict(line.split("=", 1) for line in (project / ".env").read_text().splitlines())
    assert not {"DB_REMOTE", "DB_HOST", "DB_PORT", "DB_USER"} & set(env) and env["DB_PASSWORD"] == "its-own-password-0123456789"
    meta = json.loads((promoted["folder"] / "instance.json").read_text())
    assert meta["template"] == "odoo-19" and not {"promoted_from", "database_on", "database_link"} & set(meta)
    # it listens as before the promotion, for no other machine: the next replica restarts nothing
    override = yaml.safe_load((project / "compose.override.yaml").read_text())["services"]
    assert override["db"]["ports"] == ["0.0.0.0:15440:5432"] and list(override) == ["db"]
    assert managedreplica.published_state() == {"port15440": {"from": [], "subnet": SUBNET}}
    assert promoted["firewall"][str(project)] is None and promoted["applied"] == applied + 1
    # Odoo and its database, once: down, then up, of that project only
    assert [a[0] for a in promoted["ran"]] == ["down", "up"] and not (project / ".before-promotion").exists()
    assert not (project / "config" / "db-ca.pem").exists()
    # asked again: done already; the data set aside goes only now
    assert workspacehelper.machine_db_replica_back("acme", "default", "odoo")["already"] is True
    assert managedreplica.back_purge(KEY)["freed"] > 0 and not (project / "data" / "pgdata.aside").exists()
    assert managedreplica.facts(KEY)["restarts"] is False


def test_a_database_that_came_back_and_does_not_stop_is_not_started_twice(promoted, monkeypatch):
    project, box = promoted["project"], promoted["box"]
    managedreplica.back_prepare(KEY, SPEC)
    managedreplica.back_copy(KEY)
    box.sql.answers.update({"pg_stat_wal_receiver": "streaming\n", "pg_is_in_recovery": "f\n"})
    managedreplica.back_start(KEY)
    managedreplica.back_promote(KEY)
    monkeypatch.setattr(managedreplica.DedicatedBack, "stop", lambda self: None)
    with pytest.raises(HelperError, match="did not stop: acme-odoo is not started on it while it runs"):
        workspacehelper.machine_db_replica_back("acme", "default", "odoo")
    # the instance is as it was, pointed at the copy; asked again, it is written back
    assert composehelper.on_remote(project) is not None and (box.folder / "return" / "compose.yaml").is_file()


def test_a_return_cancelled_puts_back_what_the_instance_had(promoted):
    project, box = promoted["project"], promoted["box"]
    pgdata = project / "data" / "pgdata"
    managedreplica.back_prepare(KEY, SPEC)
    managedreplica.back_copy(KEY)
    box.sql.answers["pg_stat_wal_receiver"] = "streaming\n"
    managedreplica.back_start(KEY)
    (pgdata / "base").write_text("the copy of the promoted copy")
    done = managedreplica.back_cancel(KEY)
    assert done["cancelled"] and done["freed"] > 0 and done["machine"] == "lab3"
    # its PostgreSQL stopped and its project gone, the copy deleted, the data of before back
    assert ("return", "down") in box.where and not (box.folder / "return").exists()
    assert sorted(p.name for p in pgdata.iterdir()) == ["PG_VERSION"] and not (project / "data" / "pgdata.aside").exists()
    assert machinedb.returning(box.folder) is None and machinedb.replica(box.folder)["state"] == "replaced"
    assert managedreplica.returning_state() == {}
    with pytest.raises(machinedb.ClusterError, match="no return is under way"):
        managedreplica.back_cancel(KEY)
    # one that does not stop keeps its copy: nothing is deleted under a PostgreSQL that runs
    managedreplica.back_prepare(KEY, SPEC)
    box.run.state = "running"
    managedreplica.DedicatedBack.stop, kept = (lambda self: None), managedreplica.DedicatedBack.stop
    try:
        with pytest.raises(machinedb.ClusterError, match="did not stop: the copy is not deleted"):
            managedreplica.back_cancel(KEY)
    finally:
        managedreplica.DedicatedBack.stop = kept
    assert (project / "data" / "pgdata.aside").is_dir()


def test_with_the_data_of_before_freed_the_return_makes_its_own_folder(promoted):
    project, box = promoted["project"], promoted["box"]
    pgdata = project / "data" / "pgdata"
    __import__("shutil").rmtree(pgdata)  # Free the space of before…
    assert managedreplica.back_facts(KEY)["aside"]["size"] == 0
    # nothing here says which PostgreSQL it was: the promoted copy does
    with pytest.raises(machinedb.ClusterError, match="is not one OmniaCLI replicates"):
        managedreplica.back_prepare(KEY, {**SPEC, "major": ""})
    assert not pgdata.exists() and not (box.folder / "return").exists()
    managedreplica.back_prepare(KEY, SPEC)
    back = machinedb.returning(box.folder)
    assert back["made"] == [str(pgdata)] and back["major"] == "17" and pgdata.is_dir()
    assert not (project / "data" / "pgdata.aside").exists()
    managedreplica.back_copy(KEY)
    # cancelled: what the return made is deleted, there was nothing to put back
    assert managedreplica.back_cancel(KEY)["freed"] > 0 and not pgdata.exists()
