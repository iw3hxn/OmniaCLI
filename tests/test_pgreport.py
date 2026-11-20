# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The health of an instance's database: fixed queries only, through the
helper (Docker) or psql as Odoo connects (venv); a long query stopped."""

import json

import pytest

from omniacli import composehelper, ops, pgreport
from omniacli.config import parse_servers
from omniacli.jobs import JobFailed

from .test_app import AUTH, POST, client  # noqa: F401 - the client fixture
from .test_workspaces import post_as

REPORT = {"version": "17.12", "size": 28473011, "connections": 3, "max_connections": 100, "cache_hit": 99.6,
          "long_queries": [{"pid": 4321, "state": "active", "seconds": 42, "waiting": None, "query": "SELECT 1"}],
          "tables": [], "unused_indexes": [], "sequential": [], "statements_on": False}


def test_the_helpers_actions(tmp_path, monkeypatch):
    monkeypatch.setattr(composehelper, "services", lambda project: ["odoo", "db"])
    monkeypatch.setattr(composehelper, "psql_options", lambda project, name: ("--user", "postgres"))
    [cmd] = composehelper.commands(tmp_path, "pg-report", ["db", "main"])
    assert cmd[:5] == ["exec", "-T", "--user", "postgres", "db"]
    # the queries are the helper's own, the database an argument
    assert cmd[-4:] == ["sh", "main", composehelper.PG_REPORT, composehelper.PG_STATEMENTS]
    [cmd] = composehelper.commands(tmp_path, "pg-terminate", ["db", "main", "4321"])
    assert cmd[-2:] == ["main", "4321"]
    for bad in (["db", "main", "1; DROP TABLE x"], ["db", "main", "0"], ["db", "main"]):
        with pytest.raises(composehelper.HelperError):
            composehelper.commands(tmp_path, "pg-terminate", bad)


async def test_a_docker_and_a_venv_instance(tmp_path, monkeypatch, env):
    data = {"type": "docker", "test": {"restart": {"type": "compose", "service": str(tmp_path)}, "repositories": [],
                                       "database": "main"}}
    docker = parse_servers({"s": data})["s"].test
    asked = []

    async def capture(cmd, env=None, timeout=None, **kw):
        asked.append((cmd, env))
        if "pg-terminate" in cmd or "pg_terminate_backend" in cmd[-1]:
            return 0, "t"
        return 0, json.dumps(REPORT) + "\nnull"
    monkeypatch.setattr(ops, "capture", capture)
    found = await pgreport.report(docker)
    assert found["size"] == 28473011 and found["statements"] is None
    assert asked[-1][0][-3:] == ["pg-report", "db", "main"] and asked[-1][1] is None
    assert await pgreport.terminate(docker, "4321") is True
    with pytest.raises(JobFailed, match="not a process id"):
        await pgreport.terminate(docker, "1 OR 1=1")
    # a venv's: psql as Odoo connects, the queries on its command line
    venv = env["settings"].servers["srv"].test
    found = await pgreport.report(venv)
    cmd, environ = asked[-1]
    assert cmd[-1] == composehelper.PG_REPORT and "-At" in cmd and environ["PGDATABASE"] == "DB_test"
    assert found["where"] == "local"
    # a database on another machine (as ridix's): reached with the config's host, port and sslmode
    conf = tmp_path / "remote.conf"
    conf.write_text("[options]\ndb_host = pg.example.net\ndb_port = 6432\ndb_user = odoo\n"
                    "db_password = s3cret\ndb_sslmode = require\n")
    monkeypatch.setattr(venv.options, "odoo_config", conf)
    found = await pgreport.report(venv)
    environ = asked[-1][1]
    assert (environ["PGHOST"], environ["PGPORT"], environ["PGSSLMODE"]) == ("pg.example.net", "6432", "require")
    assert found["where"] == "remote" and found["host"] == "pg.example.net:6432" and found["ssl"] == "require"
    # the password in the environment, never on the command line
    assert "s3cret" not in " ".join(asked[-1][0])


async def test_the_tab(client, env, monkeypatch):
    async def report(instance):
        return {**REPORT, "statements": None}
    stopped = []

    async def terminate(instance, pid):
        stopped.append((instance.key, pid))
        return True
    monkeypatch.setattr(pgreport, "report", report)
    monkeypatch.setattr(pgreport, "terminate", terminate)
    data = (await client.get("/api/servers/srv/production/database", headers={"Authorization": AUTH})).json()
    assert data["cache_hit"] == 99.6 and data["can_stop"]
    page = (await client.get("/servers/srv", headers={"Authorization": AUTH})).text
    assert 'data-tab="database"' in page
    url = "/api/servers/srv/production/database/terminate"
    assert (await client.post(url, headers=post_as("dev"), json={"pid": 4321})).status_code == 403
    resp = await client.post(url, headers=POST, json={"pid": 4321})
    assert resp.status_code == 200 and stopped == [("srv/production", "4321")]
