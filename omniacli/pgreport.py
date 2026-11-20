# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The health of an instance's database, from PostgreSQL's own statistics:
size, connections, cache hits, the queries running for long, the largest
tables, the indexes never used, the tables read whole, and the costliest
statements when pg_stat_statements is installed. Only fixed queries
(composehelper.PG_REPORT): a Docker instance's through the root helper in
its database container, a venv's with psql as Odoo connects (its plugin's
db_shell, its db_host too: a database on another machine is read there,
with its db_sslmode). A query running for long can be stopped (an admin's)."""

from __future__ import annotations

import json
import shlex

from . import ops
from .composehelper import PG_REPORT, PG_STATEMENTS, PID
from .jobs import JobFailed

TIMEOUT = 60


def _command(instance, *helper_args: str) -> tuple[list[str], dict | None]:
    """The helper's action for a compose instance; psql as Odoo connects otherwise."""
    if instance.restart.type == "compose":
        opts = instance.options
        return instance.restart.command(helper_args[0], getattr(opts, "db_service", "db"), opts.database,
                                        *helper_args[1:]), None
    shell = instance.plugin.db_shell(instance)
    if shell is None:
        raise JobFailed(f"{instance.plugin.label} has no database to read")
    command, env = shell
    return shlex.split(command), {**ops._ENV, **env}


def _answer(out: str) -> dict:
    lines = [line for line in out.splitlines() if line.strip()]
    report = next((json.loads(line) for line in lines if line.startswith("{")), None)
    if not isinstance(report, dict):
        raise JobFailed(f"the database did not answer: {' | '.join(lines[-3:])[:400]}")
    statements = json.loads(lines[-1]) if lines and lines[-1].startswith("[") else None
    report["statements"] = statements
    return report


LOCAL_HOSTS = ("", "localhost", "127.0.0.1", "::1")


def where(instance, env: dict | None) -> dict:
    """Where the database is: the compose project's own service, this
    machine's PostgreSQL, or another machine (db_host of its config)."""
    if env is None:
        return {"where": "service", "host": f"{getattr(instance.options, 'db_service', 'db')} of the project"}
    host = env.get("PGHOST", "")
    remote = host not in LOCAL_HOSTS and not host.startswith("/")
    return {"where": "remote" if remote else "local",
            "host": f"{host}:{env.get('PGPORT') or 5432}" if remote else (host or "local socket"),
            "ssl": env.get("PGSSLMODE") or None}


async def report(instance) -> dict:
    cmd, env = _command(instance, "pg-report")
    if env is None:
        code, out = await ops.capture(cmd, timeout=TIMEOUT)
    else:
        code, out = await ops.capture([*cmd, "-X", "-At", "-c", PG_REPORT], env=env, timeout=TIMEOUT)
        if code == 0 and '"statements_on": true' in out:
            more, extra = await ops.capture([*cmd, "-X", "-At", "-c", PG_STATEMENTS], env=env, timeout=TIMEOUT)
            if more == 0:
                out += "\n" + extra
    if code:
        place = where(instance, env)
        raise JobFailed(f"the database's statistics cannot be read ({place['where']}, {place['host']}): {out[-400:]}")
    return {**_answer(out), **where(instance, env)}


async def terminate(instance, pid: str) -> bool:
    """The query of backend pid stopped, if it is of the instance's database."""
    if not PID.fullmatch(str(pid)):
        raise JobFailed(f"{pid!r} is not a process id")
    cmd, env = _command(instance, "pg-terminate", str(pid))
    if env is None:
        code, out = await ops.capture(cmd, timeout=TIMEOUT)
    else:
        code, out = await ops.capture([*cmd, "-X", "-At", "-c",
                                       f"SELECT pg_terminate_backend({int(pid)}) FROM pg_stat_activity "
                                       f"WHERE pid = {int(pid)} AND datname = current_database()"],
                                      env=env, timeout=TIMEOUT)
    if code:
        raise JobFailed(f"the query was not stopped: {out[-300:]}")
    return out.strip().endswith("t")
