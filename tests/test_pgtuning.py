# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The machine's Database tab (UX-008 §6.2, AD-005 steps 1 and 2): the root
helper's fixed query and its closed list of parameters (the point-in-time
recovery's never listed nor touched, never RESET ALL), the proposal (D10,
D13, the memory budget), the routes per role, the restart planned once (it
survives a restart of OmniaCLI, it is cancelled), the pages' markup."""

import asyncio
import base64
import datetime
import json
import re

import pytest

from omniacli import composehelper, ops, pgreport, pgtuning, stagings
from omniacli.app import create_app
from omniacli.composehelper import HelperError
from omniacli.config import Workspace, parse_servers

from .test_app import AUTH, POST, client, wait_job  # noqa: F401 - the client fixture
from .test_pgreport import REPORT
from .test_workspaces import basic, post_as, workspaces  # noqa: F401 - the workspaces fixture

GIB, MIB = pgtuning.GIB, pgtuning.MIB
HIDDEN_WORDS = ("archive_", "wal_level", "max_wal_senders", "restore_command", "recovery_target", "primary_conninfo")


def settings_rows(shared="16384", shared_auto=None, pending=False, libraries="", work_mem="4096", max_conn="100"):
    """pg_settings' rows of the tuned parameters as the helper's query gives them (postgres:17's values)."""
    def row(name, setting, unit, context, boot, file_val=None, auto_val=None, low=None, high=None, kind="integer",
            pend=False):
        return {"name": name, "setting": setting, "unit": unit, "vartype": kind, "context": context, "min_val": low,
                "max_val": high, "boot_val": boot, "pending_restart": pend, "file_val": file_val, "auto_val": auto_val}
    return [
        row("max_connections", max_conn, None, "postmaster", "100", "100", None, "1", "262143"),
        row("shared_buffers", shared, "8kB", "postmaster", "1024", "128MB", shared_auto, "16", "1073741823",
            pend=pending),
        row("effective_cache_size", "524288", "8kB", "user", "524288", None, None, "1", "2147483647"),
        row("work_mem", work_mem, "kB", "user", "4096", None, None, "64", "2147483647"),
        row("maintenance_work_mem", "65536", "kB", "user", "65536", None, None, "64", "2147483647"),
        row("random_page_cost", "4", None, "user", "4", None, None, "0", "1.79769e+308", kind="real"),
        row("shared_preload_libraries", libraries, None, "postmaster", "", None, None, kind="string"),
    ]


def answer(**kw):
    return {"version": "17.12 (Debian 17.12-1.pgdg13+1)", "size": 27 * MIB, "connections": kw.pop("connections", 3),
            "statements_on": False, "settings": settings_rows(**kw), "changed": kw.get("changed", [])}


# --- the root helper

def test_the_settings_query_is_fixed_and_leaves_the_recovery_out(tmp_path, monkeypatch):
    monkeypatch.setattr(composehelper, "services", lambda project: ["odoo", "db"])
    monkeypatch.setattr(composehelper, "psql_options", lambda project, name: ("--user", "postgres"))
    [cmd] = composehelper.commands(tmp_path, "pg-settings", ["db", "main"])
    assert cmd[:5] == ["exec", "-T", "--user", "postgres", "db"]
    # the query is the helper's own, the database an argument
    assert cmd[-3:] == ["sh", "main", composehelper.PG_SETTINGS]
    for prefix in composehelper.HIDDEN_PREFIXES:
        assert f"f.name NOT LIKE '{prefix.replace('_', chr(92) + '_')}%'" in composehelper.PG_SETTINGS
    for name in composehelper.HIDDEN_NAMES:
        assert f"'{name}'" in composehelper.PG_SETTINGS
    # the tuned ones are named, and none of them is hidden
    assert all(f"'{name}'" in composehelper.PG_SETTINGS for name in composehelper.TUNED)
    assert not any(composehelper.hidden(name) for name in composehelper.TUNED)
    for name in ("archive_mode", "archive_command", "archive_timeout", "restore_command", "recovery_target_time",
                 "wal_level", "max_wal_senders", "max_replication_slots", "wal_keep_size", "primary_conninfo",
                 "hot_standby", "synchronous_commit", "synchronous_standby_names"):
        assert composehelper.hidden(name), name
    for bad in (["db"], ["db", "main; drop"], ["nope", "main"]):
        with pytest.raises(HelperError):
            composehelper.commands(tmp_path, "pg-settings", bad)
    # the actions that write read their values on the input, never from the command line
    for action in composehelper.TUNE_ACTIONS:
        with pytest.raises(HelperError, match="on the input"):
            composehelper.commands(tmp_path, action, ["db", "main"])


@pytest.mark.parametrize("action, payload, message", [
    ("pg-set", {"archive_mode": "off"}, "not a parameter"),
    ("pg-set", {"archive_command": "cp %p /x"}, "not a parameter"),
    ("pg-set", {"wal_level": "minimal"}, "not a parameter"),
    ("pg-set", {"fsync": "off"}, "not a parameter"),
    ("pg-set", {"work_mem": "8MB'; ALTER SYSTEM SET archive_mode = 'off"}, "not a value"),
    ("pg-set", {"work_mem": "1 G"}, "not a value"),
    ("pg-set", {"work_mem": 8}, "not a value"),
    ("pg-set", {"random_page_cost": "-1"}, "not a value"),
    ("pg-set", {"shared_preload_libraries": "pg_stat_statements;x"}, "not a value"),
    ("pg-set", {}, "no parameter"),
    ("pg-set", ["work_mem"], "set is"),
    ("pg-reset", ["archive_mode"], "not a parameter"),
    ("pg-reset", ["ALL"], "not a parameter"),
    ("pg-reset", "ALL", "set is"),
    ("pg-reset", [], "no parameter"),
    ("db-restart", {"set": {"work_mem": "8MB"}, "reset": ["work_mem"]}, "both set and reset"),
    ("db-restart", {"set": {}, "drop": []}, "set and reset only"),
    ("db-restart", {"reset": ["recovery_target_time"]}, "not a parameter"),
])
def test_the_values_are_refused_unless_closed(action, payload, message):
    with pytest.raises(HelperError, match=message):
        composehelper.tune_input(action, json.dumps(payload).encode())


def test_the_sql_of_the_values():
    values, names = composehelper.tune_input("pg-set", b'{"work_mem": "8MB", "random_page_cost": "1.1"}')
    assert composehelper.tune_sql(values, names) == ("ALTER SYSTEM SET work_mem = '8MB';\n"
                                                     "ALTER SYSTEM SET random_page_cost = '1.1';\n")
    values, names = composehelper.tune_input("db-restart", json.dumps(
        {"set": {"shared_buffers": "1GB"}, "reset": ["shared_preload_libraries", "max_connections"]}).encode())
    sql = composehelper.tune_sql(values, names)
    assert sql == ("ALTER SYSTEM SET shared_buffers = '1GB';\nALTER SYSTEM RESET shared_preload_libraries;\n"
                   "ALTER SYSTEM RESET max_connections;\n")
    # never RESET ALL, nothing of the point-in-time recovery, in any SQL of the helper
    for text in (sql, composehelper.PG_SETTINGS, composehelper.STATEMENTS_SQL, composehelper.AUTO_CONF_SQL):
        assert "RESET ALL" not in text.upper()
    assert not re.search(r"ALTER SYSTEM (SET|RESET) (archive|wal|recovery|restore|primary)", sql)
    assert composehelper.tune_input("db-restart", b"") == ({}, [])
    # a library already loaded stays, pg_stat_statements is the only one added
    composehelper.check_libraries("auto_explain,pg_stat_statements", "auto_explain")
    composehelper.check_libraries("", "auto_explain")
    with pytest.raises(HelperError, match="pg_stat_statements only"):
        composehelper.check_libraries("pg_stat_statements,passwordcheck", "")


class FakeDocker:
    """docker compose as the helper's _run sees it: the SQL given on psql's
    input recorded; pg_isready fails while the database cannot start."""

    def __init__(self, starts=True, auto_conf="archive_mode = 'on'\narchive_command = 'cp %p /x/%f'\n"):
        self.calls, self.inputs, self.starts, self.auto_conf, self.put_back = [], [], starts, auto_conf, None

    def __call__(self, project, *args, input=None):
        self.calls.append(args)
        out, code = "", 0
        if args[:1] == ("exec",) and composehelper.SCRIPTS["pg-input"] in args:
            self.inputs.append(input)
            if input == composehelper.AUTO_CONF_SQL:
                out = base64.encodebytes(self.auto_conf.encode()).decode()
            elif input.startswith("SHOW shared_preload_libraries"):
                out = "\n"
            elif input == composehelper.STATEMENTS_SQL:
                out = "t\n"
        elif args[:1] == ("exec",) and "pg_isready" in args:
            code = 0 if self.starts or self.put_back is not None else 2
        elif args[:1] == ("ps",):
            out = "restarting\n"
        elif args[:1] == ("logs",) or "logs" in args:
            out = "2026-11-21 05:00:01 UTC [1] FATAL:  could not map anonymous shared memory\n"
        elif args[:1] == ("run",):
            self.put_back = input
        return type("Done", (), {"returncode": code, "stdout": out, "stderr": ""})()


@pytest.fixture
def fake_docker(monkeypatch):
    def make(**kw):
        fake = FakeDocker(**kw)
        monkeypatch.setattr(composehelper, "_run", fake)
        monkeypatch.setattr(composehelper, "services", lambda project: ["odoo", "db"])
        monkeypatch.setattr(composehelper, "psql_options", lambda project, name: ("--user", "postgres"))
        monkeypatch.setattr(composehelper, "RESTART_TIMEOUT", 0)
        monkeypatch.setattr(composehelper.time, "sleep", lambda s: None)
        return fake
    return make


def test_the_values_go_on_the_input_never_on_a_command_line(tmp_path, fake_docker, capsys):
    fake = fake_docker()
    assert composehelper.pg_tune(tmp_path, "pg-set", ["db", "main"], b'{"work_mem": "8MB"}') == 0
    assert fake.inputs == ["ALTER SYSTEM SET work_mem = '8MB';\nSELECT pg_reload_conf();\n"]
    assert not any("8MB" in " ".join(call) for call in fake.calls)
    assert json.loads(capsys.readouterr().out) == {"set": ["work_mem"], "reset": []}
    fake = fake_docker()
    assert composehelper.pg_tune(tmp_path, "pg-reset", ["db", "main"], b'["work_mem"]') == 0
    assert fake.inputs == ["ALTER SYSTEM RESET work_mem;\nSELECT pg_reload_conf();\n"]


def test_a_restart_writes_its_values_right_before_it(tmp_path, fake_docker, capsys):
    fake = fake_docker()
    raw = json.dumps({"set": {"shared_buffers": "1GB", "shared_preload_libraries": "pg_stat_statements"}}).encode()
    assert composehelper.pg_tune(tmp_path, "db-restart", ["db", "main"], raw) == 0
    # the file of before read, the values, CHECKPOINT, stop then start, the statistics' extension
    assert fake.inputs[1] == composehelper.AUTO_CONF_SQL
    assert fake.inputs[2] == ("ALTER SYSTEM SET shared_buffers = '1GB';\n"
                              "ALTER SYSTEM SET shared_preload_libraries = 'pg_stat_statements';\n")
    assert fake.inputs[3] == "CHECKPOINT;\n" and fake.inputs[-1] == composehelper.STATEMENTS_SQL
    steps = [c[0] for c in fake.calls if c[0] in ("stop", "start", "run")]
    assert steps == ["stop", "start"] and fake.put_back is None
    done = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert done["rolled_back"] is False and done["statements_on"] is True


def test_a_restart_that_does_not_come_back_puts_the_file_of_before_back(tmp_path, fake_docker, capsys):
    before = "# Do not edit\narchive_mode = 'on'\narchive_command = 'test ! -f /w/%f && cp %p /w/%f'\n"
    fake = fake_docker(starts=False, auto_conf=before)
    code = composehelper.pg_tune(tmp_path, "db-restart", ["db", "main"], b'{"set": {"shared_buffers": "400GB"}}')
    assert code == 3
    # byte for byte the file of before, the archive lines with it, by a one-off container as postgres
    assert fake.put_back == before
    run = next(c for c in fake.calls if c[0] == "run")
    assert run[:7] == ("run", "--rm", "--no-deps", "-T", "--user", "postgres", "--entrypoint")
    assert [c[0] for c in fake.calls if c[0] in ("stop", "start", "run")] == ["stop", "start", "stop", "run", "start"]
    done = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert done["rolled_back"] is True and "could not map" in done["reason"]


# --- the proposal and its checks

def row_of(key, kind, **kw):
    found = answer(**kw)
    return {"key": key, "server": key.split("/")[0], "role": key.split("/")[1], "kind": kind, "where": "dedicated",
            "params": pgtuning.parameters(found), "connections": found["connections"]}


def test_values_as_postgresql_and_people_write_them():
    assert pgtuning.pg_bytes("128MB", "8kB") == 128 * MIB and pgtuning.pg_bytes("16384", "8kB") == 128 * MIB
    assert pgtuning.pg_bytes("4096", "kB") == 4 * MIB and pgtuning.pg_bytes("1024", "8kB") == 8 * MIB
    assert pgtuning.typed_bytes("1 GB") == GIB and pgtuning.typed_bytes("1024 MB") == GIB
    assert pgtuning.typed_bytes("1,5 gb") == int(1.5 * GIB)
    for bad in ("1 G", "1024", "GB", "1.5.5 GB", ""):
        assert pgtuning.typed_bytes(bad) is None, bad
    assert [pgtuning.memory_text(v) for v in (GIB, 992 * MIB, 3 * GIB, 512 * 1024)] == ["1GB", "992MB", "3GB", "512kB"]
    params = pgtuning.by_name(row_of("a/production", "production")["params"])
    # the base is postgresql.conf's 128 MB, not the built-in 8 MB
    assert params["shared_buffers"]["base"] == 128 * MIB and params["shared_buffers"]["restart"]
    assert not params["work_mem"]["restart"] and params["random_page_cost"]["now"] == 4.0
    assert params["shared_preload_libraries"]["now"] is False


def test_d13_the_default_memory_for_the_databases():
    # a quarter of the machine: ber, 15 GB, is 4 GB
    assert pgtuning.default_memory_gb(15.5) == 4.0 and pgtuning.default_memory_gb(15) == 4.0
    assert pgtuning.default_memory_gb(64) == 16.0
    # at most what the workspaces leave to the system less 1 GB
    assert pgtuning.default_memory_gb(16, odoo_gb=13) == 2.0
    assert pgtuning.default_memory_gb(None) == 1.0 and pgtuning.default_memory_gb(2, odoo_gb=4) == 1.0


def test_d10_the_productions_take_the_memory_tests_and_stagings_keep_their_base():
    prod, stg = row_of("odoo19-odoo/production", "production"), row_of("odoo19-odoo-stg-20261121/production", "staging")
    found = pgtuning.propose(prod, [prod, stg], 4, "ssd")
    values = found["values"]
    # ber: 4 GB, the staging holds 128 MB: 1 GB of shared memory or less
    assert values["shared_buffers"] <= GIB and values["shared_buffers"] >= 900 * MIB
    assert values["effective_cache_size"] == pgtuning._down((4 * GIB - 128 * MIB) * 3 / 4)
    assert 4 * MIB <= values["work_mem"] <= 8 * MIB and values["maintenance_work_mem"] == 248 * MIB
    assert values["random_page_cost"] == 1.1 and values["shared_preload_libraries"] is True
    assert values["max_connections"] == 100
    assert pgtuning.propose(prod, [prod, stg], 4, "hdd")["values"]["random_page_cost"] == 4.0
    # the staging: nothing proposed beyond what it has
    same = pgtuning.propose(stg, [prod, stg], 4, "ssd")["values"]
    assert same["shared_buffers"] == 128 * MIB and same["shared_preload_libraries"] is False
    # 3 customers, 9 databases: 6 x 128 MB to tests and stagings, about 270 MB of shared memory each production
    rows = [row_of(f"c{i}/production", "production") for i in range(3)]
    rows += [row_of(f"c{i}/test", "test") for i in range(3)] + [row_of(f"s{i}/production", "staging") for i in range(3)]
    each = pgtuning.propose(rows[0], rows, 4, "ssd")
    assert each["held"] == 6 * 128 * MIB and 260 * MIB <= each["values"]["shared_buffers"] <= 280 * MIB
    # a workspace of 4 GB holds Odoo and its database: under 1 GB of shared memory
    assert pgtuning.propose(prod, [prod], 16, "ssd", limit_gb=4)["values"]["shared_buffers"] <= GIB


def test_what_the_dialog_refuses():
    prod, stg = row_of("p/production", "production", connections=34), row_of("s/production", "staging")
    limits = pgtuning.limits(prod, [prod, stg], 4, 34)
    assert limits["shared_max"] == 4 * GIB - 128 * MIB and limits["connections_min"] == 44
    checked = pgtuning.checked(prod, {"work_mem": "8 MB", "shared_buffers": "1 GB", "random_page_cost": "1,1",
                                      "shared_preload_libraries": True, "max_connections": "100",
                                      "effective_cache_size": "4 GB"}, limits)
    # the values that change only, as ALTER SYSTEM writes them
    assert checked == {"work_mem": "8MB", "shared_buffers": "1GB", "random_page_cost": "1.1",
                       "shared_preload_libraries": "pg_stat_statements"}
    for posted, message in (({"shared_buffers": "6 GB"}, "exceeds the memory for the databases"),
                            ({"max_connections": "20"}, "the least here is 44"),
                            ({"work_mem": "1 G"}, "is not a value: write a number followed by MB or GB"),
                            ({"work_mem": "2 kB"}, "from"),
                            ({"max_connections": "1.5"}, "not a whole number"),
                            ({"archive_mode": "off"}, "not a parameter"),
                            ({"wal_level": "minimal"}, "not a parameter")):
        with pytest.raises(ValueError, match=message):
            pgtuning.checked(prod, posted, limits)
    # a library loaded already stays when the statistics go
    loaded = row_of("p/production", "production", libraries="auto_explain,pg_stat_statements")
    assert pgtuning.checked(loaded, {"shared_preload_libraries": False}, limits) == {
        "shared_preload_libraries": "auto_explain"}


def test_the_states_of_the_tuning_column():
    base = pgtuning.parameters(answer())
    assert pgtuning.state("production", base, {})["code"] == "base_production"
    assert pgtuning.state("staging", base, {})["code"] == "base"
    assert pgtuning.state("production", base, {"tuned": "2026-11-21T10:00", "params": ["work_mem"]}) == {
        "code": "tuned", "when": "2026-11-21T10:00"}
    copied = pgtuning.parameters(answer(shared="131072", shared_auto="1GB"))
    assert pgtuning.state("staging", copied, {})["code"] == "tuned_unknown"
    pending = pgtuning.parameters(answer(shared_auto="1GB", pending=True))
    assert pgtuning.state("production", pending, {}) == {"code": "pending", "count": 1}
    plan = {"restart": {"at": "2026-11-22T05:00", "set": {"shared_buffers": "1GB", "shared_preload_libraries": "x"}}}
    assert pgtuning.state("production", base, plan) == {"code": "scheduled", "at": "2026-11-22T05:00", "count": 2}
    assert pgtuning.state("production", None, {}, "the container is stopped")["code"] == "error"
    now = datetime.datetime(2026, 11, 21, 10, 0)
    assert pgtuning.next_time("05:00", now) == datetime.datetime(2026, 11, 22, 5, 0)
    assert pgtuning.next_time("23:30", now) == datetime.datetime(2026, 11, 21, 23, 30)
    with pytest.raises(ValueError):
        pgtuning.next_time("25:00", now)


# --- the routes

@pytest.fixture
def machine(env, monkeypatch):
    """Two dedicated databases (odoo19-odoo, production with point-in-time
    recovery on, and its staging), the venv server srv on this machine's
    PostgreSQL; the helper and the reports answered here."""
    settings = env["settings"]
    tmp = env["tmp"]
    for name in ("odoo19-odoo", "odoo19-odoo-stg-20261121"):
        data = {"type": "docker", "production": {"restart": {"type": "compose", "service": str(tmp / name)},
                                                 "repositories": [], "database": "odoo"}}
        settings.servers[name] = parse_servers({name: data})[name]
    stagings.save((settings.links_file or settings.servers_file).parent / stagings.FILE,
                  {"odoo19-odoo-stg-20261121": {"parent": "odoo19-odoo", "created": "2026-11-21T09:00:00"}})
    (settings.servers_file.parent / "pitr.json").write_text(json.dumps({"odoo19-odoo": {"enabled": True}}))
    state = {"answers": {}, "down": set(), "helper": []}

    async def read(instance):
        if instance.key in state["down"]:
            raise pgtuning.JobFailed("the container is stopped")
        return state["answers"].get(instance.key) or answer()

    async def report(instance):
        return dict(REPORT)

    real = ops.capture

    async def capture(cmd, cwd=None, timeout=None, env=None, input=None):
        if any(a in cmd for a in composehelper.TUNE_ACTIONS):
            state["helper"].append((cmd, json.loads(input)))
            if "db-restart" in cmd:
                return 0, 'written\n{"seconds": 2.1, "total": 2.4, "rolled_back": false, "statements_on": true}'
            return 0, "{}"
        if "logs" in cmd:
            return 0, "odoo ERROR psycopg2.OperationalError: server closed the connection unexpectedly\nok\n"
        return await real(cmd, cwd=cwd, timeout=timeout, env=env, input=input)
    monkeypatch.setattr(pgtuning, "read", read)
    monkeypatch.setattr(pgreport, "report", report)
    monkeypatch.setattr(ops, "capture", capture)
    monkeypatch.setattr(pgtuning, "machine_memory_gb", lambda meminfo=None: 15.5)
    return state


def store_of(env):
    return pgtuning.Store(env["settings"].jobs_dir.parent / pgtuning.FILE)


async def test_the_list_for_the_machines_admin_only(machine, workspaces, client):
    resp = await client.get("/api/postgres", headers={"Authorization": AUTH})
    assert resp.status_code == 200
    data = resp.json()
    rows = data["rows"]
    # the production, its staging under it, then this machine's PostgreSQL (one row for srv's two instances)
    assert [(r["key"], r["kind"]) for r in rows] == [("odoo19-odoo/production", "production"),
                                                      ("odoo19-odoo-stg-20261121/production", "staging"),
                                                      ("system:5432", "system")]
    prod, stg, system = rows
    assert re.fullmatch(r"17\.\d+", prod["version"]) and prod["max_connections"] == 100
    assert prod["state"]["code"] == "base_production" and prod["pitr"] == "on"
    assert stg["state"]["code"] == "base" and stg["pitr"] is None and stg["parent"] == "odoo19-odoo"
    assert [s["key"] for s in system["serves"]] == ["srv/production", "srv/test"]
    assert system["state"]["code"] == "system" and system["params"] == []
    assert data["memory"]["memory_gb"] == 4.0 and data["memory"]["assigned"] == 256 * MIB
    assert data["memory"]["at_base"] == 2
    # nothing of the point-in-time recovery or the replication reaches the page
    assert not any(word in resp.text for word in HIDDEN_WORDS)
    for user in ("dev", "viewer", "mario", "luca"):
        assert (await client.get("/api/postgres", headers=basic(user))).status_code in (403, 404), user
        url = "/api/postgres/odoo19-odoo/production/apply"
        assert (await client.post(url, headers=post_as(user), json={"values": {}})).status_code in (403, 404), user


async def test_a_database_that_does_not_answer_is_its_own_row(machine, client):
    machine["down"].add("odoo19-odoo-stg-20261121/production")
    rows = (await client.get("/api/postgres", headers={"Authorization": AUTH})).json()["rows"]
    stg = rows[1]
    assert stg["state"] == {"code": "error", "reason": "the container is stopped"} and rows[0]["params"]
    # in Italian where the page is Italian
    resp = await client.get("/api/postgres", headers={"Authorization": AUTH, "Accept-Language": "it"})
    assert resp.json()["rows"][1]["state"]["reason"] == "il container è fermo"


async def test_the_proposal(machine, client):
    url = "/api/postgres/odoo19-odoo/production/proposal"
    data = (await client.get(url, headers={"Authorization": AUTH})).json()
    assert data["proposal"]["values"]["shared_buffers"] <= GIB and data["when"] == "at" and data["time"] == "05:00"
    assert data["machine"]["total_gb"] == 15.5 and data["machine"]["memory_gb"] == 4.0
    assert [s["key"] for s in data["stays"]] == ["odoo19-odoo-stg-20261121/production"]
    more = (await client.get(url + "?memory_gb=8&disk=hdd", headers={"Authorization": AUTH})).json()
    assert more["proposal"]["values"]["shared_buffers"] > GIB and more["proposal"]["values"]["random_page_cost"] == 4.0
    assert (await client.get(url + "?memory_gb=99", headers={"Authorization": AUTH})).status_code == 400
    assert (await client.get(url + "?disk=tape", headers={"Authorization": AUTH})).status_code == 400
    stg = (await client.get("/api/postgres/odoo19-odoo-stg-20261121/production/proposal",
                            headers={"Authorization": AUTH})).json()
    assert stg["when"] == "now" and stg["proposal"]["values"]["shared_buffers"] == 128 * MIB
    # only a dedicated database is tuned
    assert (await client.get("/api/postgres/srv/production/proposal", headers={"Authorization": AUTH})).status_code == 404


async def test_apply_at_five_then_the_planned_restart(machine, client, env):
    url = "/api/postgres/odoo19-odoo/production/apply"
    values = {"work_mem": "8 MB", "shared_buffers": "1 GB", "shared_preload_libraries": True}
    for bad, message in (({"shared_buffers": "6 GB"}, "exceeds"), ({"max_connections": "5"}, "the least here is 13"),
                         ({"work_mem": "1 G"}, "not a value"), ({"archive_mode": "off"}, "not a parameter"),
                         ({}, "Nothing to apply")):
        resp = await client.post(url, headers=POST, json={"values": bad, "when": "05:00"})
        assert resp.status_code == 400 and message in resp.json()["error"], bad
    assert machine["helper"] == []
    resp = await client.post(url, headers=POST, json={"values": values, "when": "05:00"})
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success", job
    # now: the values valid at once, on the helper's input; the restart's are not written yet
    [(cmd, payload)] = machine["helper"]
    assert "pg-set" in cmd and payload == {"work_mem": "8MB"} and "8MB" not in " ".join(cmd)
    record = store_of(env).cluster("odoo19-odoo/production")
    assert record["params"] == ["work_mem"] and record["memory_gb"] == 4.0 and record["by"] == "admin"
    plan = record["restart"]
    at = datetime.datetime.fromisoformat(plan["at"])
    assert (at.hour, at.minute) == (5, 0) and at > datetime.datetime.now()
    assert plan["set"] == {"shared_buffers": "1GB", "shared_preload_libraries": "pg_stat_statements"}
    # the row says it, and the instance's Settings too, without reading PostgreSQL
    row = (await client.get("/api/postgres", headers={"Authorization": AUTH})).json()["rows"][0]
    assert row["state"] == {"code": "scheduled", "at": plan["at"], "count": 2}
    page = (await client.get("/servers/odoo19-odoo", headers={"Authorization": AUTH})).text
    assert "dedicated · restart at 05:00 of" in " ".join(page.split())
    # a second tuning waits for the plan
    resp = await client.post(url, headers=POST, json={"values": {"work_mem": "16 MB"}})
    assert resp.status_code == 400 and "is planned" in resp.json()["error"]
    # at five: the job writes the restart's values right before it, then restarts
    fired = client._transport.app.app.state.postgres_due(at + datetime.timedelta(seconds=30))
    assert fired == ["odoo19-odoo/production"]
    for _ in range(100):
        rec = store_of(env).cluster("odoo19-odoo/production")
        if "restart" not in rec and len(machine["helper"]) == 2:
            break
        await asyncio.sleep(0.05)
    cmd, payload = machine["helper"][1]
    assert "db-restart" in cmd and payload == {"set": plan["set"], "reset": []}
    rec = store_of(env).cluster("odoo19-odoo/production")
    assert rec["params"] == ["shared_buffers", "shared_preload_libraries", "work_mem"] and "restart" not in rec
    jobs = (await client.get("/api/jobs", headers={"Authorization": AUTH})).json()
    restart = next(j for j in jobs["jobs"] if "(planned)" in j["title"])
    done = await wait_job(client, restart["id"])
    assert done["status"] == "success"
    log = "\n".join(done.get("lines") or [])
    assert "answers again: 2.1 s without it" in log and "database error since the restart: 1" in log


async def test_cancel_the_planned_restart(machine, client, env):
    url = "/api/postgres/odoo19-odoo/production"
    resp = await client.post(url + "/apply", headers=POST, json={"values": {"shared_buffers": "1 GB"}, "when": "05:00"})
    await wait_job(client, resp.json()["job"])
    plan = store_of(env).cluster("odoo19-odoo/production")["restart"]
    assert (await client.post(url + "/restart/cancel", headers=post_as("dev"))).status_code in (403, 404)
    assert (await client.post(url + "/restart/cancel", headers=POST)).json() == {"ok": True}
    assert store_of(env).cluster("odoo19-odoo/production") == {}
    at = datetime.datetime.fromisoformat(plan["at"])
    assert client._transport.app.app.state.postgres_due(at + datetime.timedelta(seconds=30)) == []
    assert machine["helper"] == []
    resp = await client.post(url + "/restart/cancel", headers=POST)
    assert resp.status_code == 400 and "No restart" in resp.json()["error"]


async def test_a_planned_restart_survives_a_restart_of_omniacli(machine, client, env):
    url = "/api/postgres/odoo19-odoo/production"
    resp = await client.post(url + "/apply", headers=POST, json={"values": {"max_connections": "150"}, "when": "05:00"})
    await wait_job(client, resp.json()["job"])
    plan = store_of(env).cluster("odoo19-odoo/production")["restart"]
    at = datetime.datetime.fromisoformat(plan["at"])
    # another OmniaCLI on the same files: the plan is there, and fired at its time
    again = create_app(env["settings"])
    assert again.state.postgres_due(at - datetime.timedelta(minutes=1)) == []
    assert again.state.postgres_due(at + datetime.timedelta(minutes=1)) == ["odoo19-odoo/production"]
    for _ in range(100):
        if len(machine["helper"]) == 1:
            break
        await asyncio.sleep(0.05)
    assert machine["helper"][0][1] == {"set": {"max_connections": "150"}, "reset": []}


async def test_a_restart_missed_while_omniacli_was_stopped_moves_to_the_next_day(machine, client, env):
    url = "/api/postgres/odoo19-odoo/production"
    resp = await client.post(url + "/apply", headers=POST, json={"values": {"shared_buffers": "1 GB"}, "when": "05:00"})
    await wait_job(client, resp.json()["job"])
    at = datetime.datetime.fromisoformat(store_of(env).cluster("odoo19-odoo/production")["restart"]["at"])
    late = at + datetime.timedelta(hours=4)
    assert client._transport.app.app.state.postgres_due(late) == []
    moved = datetime.datetime.fromisoformat(store_of(env).cluster("odoo19-odoo/production")["restart"]["at"])
    assert moved == at + datetime.timedelta(days=1) and machine["helper"] == []


async def test_restart_now_and_back_to_the_base_values(machine, client, env):
    url = "/api/postgres/odoo19-odoo/production"
    resp = await client.post(url + "/apply", headers=POST, json={
        "values": {"work_mem": "8 MB", "shared_buffers": "1 GB", "shared_preload_libraries": True}, "when": "05:00"})
    await wait_job(client, resp.json()["job"])
    resp = await client.post(url + "/restart", headers=POST)
    done = await wait_job(client, resp.json()["job"])
    assert done["status"] == "success" and "db-restart" in machine["helper"][-1][0]
    assert "restart" not in store_of(env).cluster("odoo19-odoo/production")
    machine["helper"].clear()
    # the tuned values written in postgresql.auto.conf, archive_* among them (not shown, not touched)
    tuned = answer(shared="131072", shared_auto="1GB", libraries="pg_stat_statements", work_mem="8192")
    machine["answers"]["odoo19-odoo/production"] = tuned
    resp = await client.post(url + "/reset", headers=POST, json={"when": "now"})
    done = await wait_job(client, resp.json()["job"])
    assert done["status"] == "success", done
    (reset_cmd, at_once), (restart_cmd, restart) = machine["helper"]
    assert "pg-reset" in reset_cmd and at_once == ["work_mem"]
    assert "db-restart" in restart_cmd and restart == {"set": {}, "reset": ["shared_buffers", "shared_preload_libraries"]}
    sent = json.dumps([at_once, restart])
    assert "ALL" not in sent and not any(word in sent for word in ("archive", "wal_", "recovery", "restore"))
    assert store_of(env).cluster("odoo19-odoo/production") == {}
    resp = await client.post(url + "/reset", headers=POST, json={"when": "now"})
    assert resp.status_code == 400 and "nothing to put back" in resp.json()["error"]


# --- the pages

async def test_the_tab_on_the_dashboard(machine, workspaces, client):
    page = (await client.get("/", headers={"Authorization": AUTH})).text
    tabs = re.findall(r'<button[^>]*role="tab"[^>]*>', page)
    # Overview, Monitoring, Database and SSH access (README, SSH access)
    assert len(tabs) == 4 and any('data-tab="database"' in t for t in tabs)
    assert all('title="' in t for t in tabs)
    assert 'data-panel="database"' in page and 'id="pg-dialog"' in page
    # no primary in the tab itself: the only one is the dialog's confirm (and the confirm of a restart)
    panel = page[page.index('data-panel="database"'):page.index("<script>", page.index('data-panel="database"'))]
    assert 'class="primary"' not in panel.split("<dialog")[0]
    assert not any(word in panel for word in HIDDEN_WORDS)
    for user in ("dev", "viewer", "mario"):
        other = (await client.get("/", headers=basic(user))).text
        assert 'data-tab="database"' not in other and 'data-panel="database"' not in other, user


async def test_the_database_row_of_the_instance(machine, workspaces, client, env):
    page = " ".join((await client.get("/servers/odoo19-odoo", headers={"Authorization": AUTH})).text.split())
    assert "dedicated · base values" in page and 'href="/#database"' in page
    assert ">Database service<" not in page
    store_of(env).update(lambda d: d["clusters"].update(
        {"odoo19-odoo/production": {"tuned": "2026-11-21T10:00", "params": ["work_mem"]}}))
    page = " ".join((await client.get("/servers/odoo19-odoo", headers={"Authorization": AUTH})).text.split())
    assert "dedicated · tuned on 21/11/2026 10:00" in page
    page = (await client.get("/servers/odoo19-odoo", headers={"Authorization": AUTH, "Accept-Language": "it"})).text
    assert "dedicato · regolato il 21/11/2026 10:00" in " ".join(page.split())
    # a workspace's admin: the row, no link to the machine's tab
    mine = " ".join((await client.get("/servers/srv", headers=basic("mario"))).text.split())
    assert "PostgreSQL of the system (port 5432) · not tuned by OmniaCLI" in mine
    assert 'href="/#database"' not in mine
    assert "The administrator of the machine tunes it" in mine


async def test_a_workspace_limit_holds_odoo_and_its_database(machine, client, env):
    env["settings"].workspaces = {"odoo19": Workspace("odoo19", servers=["odoo19-odoo"], limits={"memory_gb": 4})}
    rows = (await client.get("/api/postgres", headers={"Authorization": AUTH})).json()["rows"]
    assert rows[0]["workspace"] == "odoo19"
    data = (await client.get("/api/postgres/odoo19-odoo/production/proposal?memory_gb=12",
                             headers={"Authorization": AUTH})).json()
    assert data["workspace"] == "odoo19" and data["workspace_gb"] == 4
    assert data["proposal"]["values"]["shared_buffers"] <= GIB
    # D13: what the workspaces keep for Odoo comes off the default
    assert data["machine"]["default_gb"] == pgtuning.default_memory_gb(15.5, 4)
