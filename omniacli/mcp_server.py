# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The MCP interface: monitor the servers, update a test instance, and
describe an instance so that a client can rebuild its development setup.

Served at /mcp (streamable HTTP, stateless, JSON responses) for clients with
a bearer token from auth.tokens. Needs the mcp SDK, which needs Python 3.10:
without it OmniaCLI runs without /mcp.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import re
from urllib.parse import urlsplit, urlunsplit

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import apiguard, backupcheck, instancearchive, ops, pgreport, plugins
from .auth import Principal
from .accesslog import AccessLog
from .auth import ROLES as ACCESS_ROLES
from .auth import Authenticator
from .config import ConfigError, Instance, Settings, server_to_dict
from .jobs import FINISHED, Job, JobFailed, JobManager, filter_jobs
from .logtail import last_lines
from .plugin_api import ActionRefused
from .metrics import Sampler
from .scheduler import AutoUpdater

MAX_WAIT = 600  # seconds a tool may wait for its job
_RECORD = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+ \d+ (\w+) ")

INSTRUCTIONS = """OmniaCLI deploys services (Odoo and the other types of its plugins) from git:
each server has a production and a test instance. Read with list_servers,
server_status, read_log, list_jobs, get_job, pending_updates. The only action
is update_test (git pull and the install of a new signed package, what the
server's type does after a pull, restart of the test instance). dev_setup describes an instance (repositories,
branches, commits, backups, and what its type tells) so that you can rebuild
it locally; for the data, download a backup zip with an HTTP GET to the
server's base URL + its path, with the same Authorization: Bearer header.
backup_test makes a fresh backup of a test instance. diagnose_instance tells
in one answer what is wrong with an instance (state, load, log errors, last
jobs, last backup). restart_instance, start_instance, stop_instance,
list_actions and run_action (the type's actions: Pull image, Update modules...),
verify_backup, full_archive and stop_job act; on a production they need an
admin token and confirm=<server name>, which you give only after the user
agreed. update_production backs up first, always. A token bound to a
workspace sees that workspace's servers only. list_links, linked_tools and
call_linked reach the OmniaCLI this one is linked to, with their own tools:
one MCP for every machine. With an admin token, backup backs up any
instance (production too) and list_backups lists an instance's backups, each
with the path to download it; these two and dev_setup also reach the draft
servers (complete ones) with an admin token. read_server gives a
server's whole configuration and add_draft_server adds a server as a draft:
an administrator makes it live from the web interface (Test & activate).
To restore a database from outside as a new instance ("restore the database
X on the server Y creating a new neutralized instance"): upload_backup gives
a URL to PUT the file to (with link=<machine> it is sent on there), then
restore_new_instance, first with dry_run=true: it tells the modules without
code, which you report to the user before restoring with dry_run=false."""


def _instructions() -> str:
    """The core's instructions, then what each plugin tells of its type."""
    helps = [p.mcp_help.strip() for p in plugins.registry().plugins.values() if p.mcp_help.strip()]
    return "\n\n".join([INSTRUCTIONS, *helps])

# the role an MCP token needs for a tool; the others: any token
TOOL_ROLES = {"update_test": "developer", "backup_test": "developer",
              # a production's backup asks the admin role, never a CI token's (apiguard.production_refusal)
              "backup": "developer",
              "read_server": "admin", "add_draft_server": "admin",
              "restart_instance": "developer", "start_instance": "developer", "stop_instance": "developer",
              "list_actions": "developer", "run_action": "developer", "verify_backup": "developer",
              "full_archive": "developer", "stop_job": "developer",
              # a staging's production is updated as a test (the CI's); a production's asks admin and confirm
              "update_production": "developer",
              # what the REST API does too (README, REST API): the same rules
              "create_staging": "developer", "list_deploys": "developer", "deploy_back": "developer",
              "database_report": "developer", "upload_backup": "admin", "restore_new_instance": "admin"}
# the tools that change something: a write in the rate limit of a token (apiguard.RateLimiter)
WRITE_TOOLS = {"update_test", "backup_test", "backup", "add_draft_server", "restart_instance", "start_instance",
               "stop_instance", "run_action", "verify_backup", "full_archive", "stop_job", "update_production",
               "upload_backup", "restore_new_instance", "create_staging", "deploy_back"}
# the tools of the machine's tokens only: a workspace's token never reaches them
MACHINE_TOOLS = {"read_server", "add_draft_server", "list_links", "linked_tools", "call_linked"}
# the role of the token of the request being served, for the list of tools,
# and the workspace it is bound to (empty: the machine's)
_ROLE = contextvars.ContextVar("omniacli_mcp_role", default="user")
_SCOPE = contextvars.ContextVar("omniacli_mcp_scope", default="")
# who calls through a link: "link <name> (<user there>)"
_WHO = contextvars.ContextVar("omniacli_mcp_who", default="")
# the headers a link sends with who acts through it and the most it may do
_ON_BEHALF = re.compile(r"[A-Za-z0-9._@-]{1,64}")


def _public_url(url: str) -> str:
    """A remote URL without the credentials an https URL may carry."""
    parts = urlsplit(url)
    if parts.scheme in ("http", "https") and (parts.username or parts.password):
        host = parts.hostname or ""
        if parts.port:
            host += f":{parts.port}"
        return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))
    return url


def _errors_only(lines: list[str]) -> list[str]:
    """The ERROR and CRITICAL records, with their continuation lines
    (tracebacks), as the log page filters them."""
    level, kept = "INFO", []
    for line in lines:
        match = _RECORD.match(line)
        if match:
            level = match.group(1)
        if level in ("ERROR", "CRITICAL"):
            kept.append(line)
    return kept


def _rpc_tools(body: bytes) -> list[str]:
    """The tools a JSON-RPC request calls."""
    try:
        data = json.loads(body or b"null")
    except ValueError:
        return []
    calls = data if isinstance(data, list) else [data]
    return [str(c["params"].get("name")) for c in calls if isinstance(c, dict) and c.get("method") == "tools/call"
            and isinstance(c.get("params"), dict)]


def _refusal(response) -> str:
    """The text of a refusal of the pages' code (a JSONResponse {"error": text})."""
    try:
        data = json.loads(getattr(response, "body", b"") or b"null")
    except ValueError:
        data = None
    return str(data.get("error")) if isinstance(data, dict) and data.get("error") else "Refused"


def _job_dict(job: Job, tail: int = 0) -> dict:
    data = {
        "id": job.id, "title": job.title, "servers": job.servers, "instances": job.instances,
        "status": job.status, "source": job.source, "created": job.created,
        "started": job.started, "finished": job.finished, "error": job.error,
        "result": job.result,
    }
    if tail:
        data["log"] = job.lines[-tail:]
    return data


async def _wait(job: Job, seconds: int) -> None:
    for _ in range(min(max(seconds, 0), MAX_WAIT) * 2):
        if job.status in FINISHED:
            return
        await asyncio.sleep(0.5)


def _rpc_summary(body: bytes) -> str:
    """The JSON-RPC method of an MCP request, with the tool it calls."""
    try:
        data = json.loads(body or b"null")
    except ValueError:
        return ""
    calls = data if isinstance(data, list) else [data]
    found = []
    for call in calls:
        if not isinstance(call, dict) or not isinstance(call.get("method"), str):
            continue
        method = call["method"]
        params = call.get("params") if isinstance(call.get("params"), dict) else {}
        if method == "tools/call" and isinstance(params.get("name"), str):
            method += " " + params["name"]
        found.append(method[:100])
    return ", ".join(found)


def _rpc_server(body: bytes) -> str:
    """The server a tools/call names in its arguments ("" when none, or several calls)."""
    try:
        data = json.loads(body or b"null")
    except ValueError:
        return ""
    if not isinstance(data, dict) or not isinstance(data.get("params"), dict):
        return ""
    arguments = data["params"].get("arguments")
    server = arguments.get("server") if isinstance(arguments, dict) else None
    return server[:64] if isinstance(server, str) else ""


def create_mcp(settings: Settings, jobs: JobManager, updater: AutoUpdater, sampler: Sampler,
               authenticator: Authenticator, access_log: AccessLog, add_draft,
               link_call=None, uptime=None, restores: dict | None = None,
               api: dict | None = None) -> tuple[Route, object]:
    """The /mcp route and the session manager to run in the app's lifespan.
    add_draft(name, data): the app's own way to add a draft server;
    link_call(link, user, role, payload): a JSON-RPC payload to the /mcp of
    a linked OmniaCLI, its answer (LookupError: no such link); uptime(key):
    the monitor's view of an instance; restores: the app's upload (a URL to
    PUT a database to) and restore (a new instance from it); api: what MCP
    shares with the REST API (guard: its rate limit and idempotency keys,
    by_hand(instance): a production, not a staging; staging, deploys and
    deploy_back: the app's code of the pages)."""
    server = MCPServer(name="OmniaCLI", instructions=_instructions())
    api = api or {}
    guard = api.get("guard")
    by_hand = api.get("by_hand") or (lambda inst: inst.role == "production")

    def caller(ctx: Context) -> str:
        """Who calls: the token's name, or "link <name> (<who acts there>)"."""
        # the route checked this very header before the request got here
        _, _, token = (ctx.headers or {}).get("authorization", "").partition(" ")
        return authenticator.token_name(token.strip()) or _WHO.get() or "unknown"

    def role_of(ctx: Context) -> str:
        """The caller's role: its token's, or its link's capped by who acts."""
        _, _, token = (ctx.headers or {}).get("authorization", "").partition(" ")
        name = authenticator.token_name(token.strip())
        return settings.token_role(name) if name else _ROLE.get()

    def allowed(tool: str, role: str) -> bool:
        return ACCESS_ROLES.index(role) >= ACCESS_ROLES.index(TOOL_ROLES.get(tool, "user"))

    def need(ctx: Context, tool: str) -> None:
        role = role_of(ctx)
        if not allowed(tool, role):
            raise ToolError(f"{tool} needs an MCP token with role {TOOL_ROLES[tool]}; this one is {role}")

    every_tool = server.list_tools

    async def list_tools():
        """Only the tools the caller's token may use."""
        role, scope = _ROLE.get(), _SCOPE.get()
        return [t for t in await every_tool() if allowed(t.name, role) and not (scope and t.name in MACHINE_TOOLS)]
    server.list_tools = list_tools

    def scope_of(ctx: Context | None) -> str:
        """The workspace the caller's token is bound to; empty: the machine's
        token, or a link (a link is the machine's)."""
        if ctx is None:
            return _SCOPE.get()
        _, _, token = (ctx.headers or {}).get("authorization", "").partition(" ")
        name = authenticator.token_name(token.strip())
        return settings.token_workspace(name) if name else ""

    def token_of(ctx: Context | None) -> str | None:
        if ctx is None:
            return None
        _, _, token = (ctx.headers or {}).get("authorization", "").partition(" ")
        return authenticator.token_name(token.strip())

    def ci_of(ctx: Context | None) -> bool:
        """A CI's token: it never acts on a production."""
        name = token_of(ctx)
        return bool(name) and settings.token_ci(name)

    def sees(ctx: Context | None, server_name: str) -> bool:
        name = token_of(ctx)
        limited = settings.token_servers(name) if name else ()
        if limited and server_name not in limited:
            return False
        scope = scope_of(ctx)
        if not scope:
            return True
        ws = settings.workspaces.get(scope)
        return ws is not None and server_name in ws.servers

    def machine_only(ctx: Context, tool: str) -> None:
        if scope_of(ctx):
            raise ToolError(f"{tool} is for a token of the machine; this one is of workspace {scope_of(ctx)}")

    def job_seen(ctx: Context | None, job: Job) -> bool:
        name = token_of(ctx)
        limited = settings.token_servers(name) if name else ()
        if limited and not (job.servers and set(job.servers) <= set(limited)):
            return False
        scope = scope_of(ctx)
        return not scope or job.workspace == scope or any(sees(ctx, s) for s in job.servers)

    def instance(name: str, role: str, ctx: Context | None = None, drafts_too: bool = False) -> Instance:
        """An instance of an active server; with drafts_too and an admin
        token of the machine, also of a draft (to rebuild it locally and to
        back it up: never to act on it). A workspace's token reaches the
        workspace's servers only."""
        if not sees(ctx, name):
            raise ToolError(f"No active server {name} with a {role} instance")
        drafts = drafts_too and ctx is not None and role_of(ctx) == "admin" and not scope_of(ctx)
        found = settings.instance(name, role, drafts)
        if found is None:
            srv = settings.servers.get(name)
            if drafts and srv is not None and srv.error:
                raise ToolError(f"{name} is a draft saved incomplete: {srv.error}")
            if srv is not None and not srv.active and not drafts:
                raise ToolError(f"{name} is a draft: not live until Test & activate on the web; "
                                "with an admin token dev_setup, backup and list_backups reach it")
            raise ToolError(f"No server {name} with a {role} instance" if drafts
                            else f"No active server {name} with a {role} instance")
        return found

    @server.tool()
    def list_servers(ctx: Context) -> list[dict]:
        """Every configured server: type (odoo...), status (active or draft),
        its instances with what its plugin tells (Odoo: database) and how the
        service is restarted, and the drafts' problems (or a missing plugin)."""
        return [
            {
                "name": srv.name, "type": srv.type, "status": srv.status, "incomplete": srv.error,
                "instances": {
                    role: {**i.plugin.describe(i), "restart": i.restart.type,
                           "service": i.restart.service or None,
                           "repositories": [r.name for r in i.repositories]}
                    for role, i in srv.instances.items()
                },
            }
            for srv in settings.servers.values() if sees(ctx, srv.name)
        ]

    @server.tool()
    async def server_status(server_name: str, ctx: Context) -> dict:
        """State of an active server's instances: repositories (branch, last
        commit), whether Odoo runs, CPU and memory, auto update, running job,
        and its last jobs."""
        srv = settings.servers.get(server_name)
        if srv is None or not srv.active or not sees(ctx, server_name):
            raise ToolError(f"No active server {server_name}")
        result = {}
        for role, inst in srv.instances.items():
            repos = await asyncio.gather(*(ops.repository_status(r) for r in inst.repositories))
            series = sampler.series(inst.key)
            odoo = None
            if inst.restart.type == "process":
                pid = ops.managed_pid(inst)
                odoo = {"started_by": "OmniaCLI", "running": pid is not None, "pid": pid}
            result[role] = {
                **inst.plugin.describe(inst),
                "repositories": {r.name: s for r, s in zip(inst.repositories, repos)},
                "odoo": odoo or {"started_by": f"{inst.restart.type} {inst.restart.service}"},
                "load": series[-1] if series else None,
                "load_measured_on": sampler.source.get(inst.key),
                "auto_update": {"enabled": inst.auto_update.enabled,
                                "window": inst.auto_update.window_label(),
                                "last_check": updater.state.get(inst.key)},
                "job_running": jobs.is_busy(inst.key),
            }
        result["last_jobs"] = [_job_dict(j) for j in filter_jobs(jobs.recent(len(jobs.jobs)), server_name)[:5]]
        return result

    @server.tool()
    async def pending_updates(server_name: str, ctx: Context, role: str = "test") -> dict:
        """Commits on the remotes not yet pulled, per repository (runs git fetch),
        and "package <name> <version>": 1 when a newer signed package is published."""
        try:
            return await ops.pending_updates(instance(server_name, role, ctx))
        except JobFailed as exc:
            raise ToolError(str(exc)) from exc

    @server.tool()
    async def read_log(server_name: str, ctx: Context, role: str = "test", lines: int = 200,
                       errors_only: bool = False) -> dict:
        """The last lines of an instance's Odoo log; errors_only keeps the
        ERROR and CRITICAL records with their tracebacks."""
        inst = instance(server_name, role, ctx)
        return await log_lines(inst, lines, errors_only)

    async def log_lines(inst: Instance, lines: int, errors_only: bool) -> dict:
        count = min(max(lines, 1), 5000)
        # read more when filtering, so that errors_only still finds some
        wanted = count * (10 if errors_only else 1)
        if inst.log_file is None:
            try:
                read = (await ops.compose_log(inst, None, min(wanted, 10000)))["lines"]
            except ops.LogUnavailable as exc:
                raise ToolError(f"Cannot read {inst.log_label}: {exc}") from exc
        else:
            try:
                read, _, _ = await asyncio.to_thread(last_lines, inst.log_file, wanted)
            except OSError as exc:
                raise ToolError(f"Cannot read {inst.log_file}: {exc.strerror}") from exc
        if errors_only:
            read = _errors_only(read)
        return {"log_file": inst.log_label, "lines": read[-count:]}

    @server.tool()
    def list_jobs(ctx: Context, server_name: str = "", query: str = "", limit: int = 20) -> list[dict]:
        """The last jobs, newest first, of a server and/or containing every
        word of query (title, started by, error, server/role)."""
        found = [j for j in filter_jobs(jobs.recent(len(jobs.jobs)), server_name, query) if job_seen(ctx, j)]
        return [_job_dict(j) for j in found[:min(max(limit, 1), 200)]]

    @server.tool()
    def get_job(job_id: str, ctx: Context, tail: int = 200) -> dict:
        """A job with the last lines of its log."""
        job = jobs.get(job_id)
        if job is None or not job_seen(ctx, job):
            raise ToolError(f"No job {job_id}")
        return _job_dict(job, tail=min(max(tail, 1), 5000))

    @server.tool()
    def job_log(job_id: str, ctx: Context, offset: int = 0, limit: int = 1000) -> dict:
        """The lines of a job's log from offset (the REST API's /jobs/{id}/log):
        call it again with next_offset until finished is true."""
        job = jobs.get(job_id)
        if job is None or not job_seen(ctx, job):
            raise ToolError(f"No job {job_id}")
        start = max(offset, 0)
        given = job.lines[start:start + min(max(limit, 1), 5000)]
        return {"job": job.id, "status": job.status, "offset": start, "next_offset": start + len(given),
                "finished": job.status in FINISHED and start + len(given) >= len(job.lines), "lines": given}

    @server.tool()
    async def update_test(server_name: str, ctx: Context, wait_seconds: int = 0, idempotency_key: str = "") -> dict:
        """Update the test instance: git pull of every repository and the
        install of a new published package, update of the changed modules, restart. Waits up to wait_seconds (max 600) for
        the job; follow it with get_job otherwise. idempotency_key: the same
        key with the same arguments within 24 hours answers the same job."""
        need(ctx, "update_test")
        inst = instance(server_name, "test", ctx)

        async def run():
            job = jobs.submit(f"Git pull and restart {inst.key}", [inst.key], f"mcp: {caller(ctx)}",
                              lambda job: ops.pull_restart(
                                  job, inst, backup_folder=settings.backup_dir / inst.server / inst.role))
            await _wait(job, wait_seconds)
            return _job_dict(job, tail=50)
        return await once(ctx, "update_test", idempotency_key, {"server_name": server_name}, run)

    def backup_options(inst: Instance, options: dict | None) -> dict:
        """options checked against the plugin's backup_fields: a checkbox
        true or false, a number a number, a checklist a list of strings
        (left out: the ticks of the instance's last backup dialog), a text a
        string."""
        fields = {f.key: f for f in inst.plugin.backup_fields()}
        unknown = sorted(set(options or {}) - set(fields))
        if unknown:
            raise ToolError(f"unknown backup options {', '.join(unknown)}; "
                            f"{inst.plugin.label} has: {', '.join(fields) or 'none'}")
        out = {}
        for k, v in (options or {}).items():
            kind = fields[k].kind
            if kind == "checkbox" and not isinstance(v, bool):
                raise ToolError(f"backup option {k}: true or false")
            if kind == "number" and (isinstance(v, bool) or not isinstance(v, (int, float))):
                raise ToolError(f"backup option {k}: a number")
            if kind == "checklist" and not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
                raise ToolError(f"backup option {k}: a list of strings")
            out[k] = v if kind in ("checkbox", "number", "checklist") else str(v)
        return out

    async def backup_job(inst: Instance, ctx: Context, wait_seconds: int, options: dict | None) -> dict:
        chosen = backup_options(inst, options)

        async def action(job):
            dest = await ops.backup(job, inst, settings.backup_dir, chosen)
            job.result = {"file": dest.name, "size": dest.stat().st_size,
                          "download": f"/backups/{inst.server}/{inst.role}/{dest.name}"}
        flags = [k for k, v in chosen.items() if v is True]
        job = jobs.submit(f"Backup {inst.key}" + (f" ({', '.join(flags)})" if flags else ""), [inst.key],
                          f"mcp: {caller(ctx)}", action)
        await _wait(job, wait_seconds)
        return _job_dict(job, tail=20)

    def backups_of(inst: Instance, limit: int) -> list[dict]:
        """The instance's backup zips, newest first, with their download path."""
        folder = settings.backup_dir / inst.server / inst.role
        found = sorted(folder.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True) \
            if folder.is_dir() else []
        return [{"file": p.name, "size": p.stat().st_size, "time": int(p.stat().st_mtime),
                 "download": f"/backups/{inst.server}/{inst.role}/{p.name}"} for p in found[:limit]]

    @server.tool()
    async def backup_test(server_name: str, ctx: Context, wait_seconds: int = 0,
                          options: dict | None = None, idempotency_key: str = "") -> dict:
        """Back up the database and filestore of a test instance; the result
        gives the path to download the zip (GET with the same bearer token).
        options: the plugin's backup choices, Odoo {"no_filestore": true}
        for the database only, {"no_binaries": true} with the binary
        columns emptied (those ticked in the instance's last backup dialog,
        or "binary_columns": ["table.column", ...])."""
        need(ctx, "backup_test")
        inst = instance(server_name, "test", ctx)
        return await once(ctx, "backup_test", idempotency_key, {"server_name": server_name, "options": options},
                          lambda: backup_job(inst, ctx, wait_seconds, options))

    @server.tool()
    async def backup(server_name: str, role: str, ctx: Context, wait_seconds: int = 0,
                     options: dict | None = None, idempotency_key: str = "") -> dict:
        """Back up the database and filestore of an instance, a test or a
        staging (developer token), a production (admin token, never a CI's;
        no confirm: a backup does not change it). Waits up to wait_seconds (max 600); the finished
        job's result gives the path to download the zip: an HTTP GET to the
        server's base URL + that path, with the same Authorization: Bearer
        header. options: the plugin's backup choices, Odoo
        {"no_filestore": true} for the database only, {"no_binaries": true}
        with the binary columns emptied (those ticked in the instance's last
        backup dialog, or "binary_columns": ["table.column", ...])."""
        need(ctx, "backup")
        inst = instance(server_name, role, ctx, drafts_too=True)
        production(ctx, "backup", inst, "", confirm_needed=False)
        return await once(ctx, "backup", idempotency_key,
                          {"server_name": server_name, "role": role, "options": options},
                          lambda: backup_job(inst, ctx, wait_seconds, options))

    @server.tool()
    def list_backups(server_name: str, role: str, ctx: Context, limit: int = 20) -> list[dict]:
        """An instance's backups, newest first: file, size,
        time (Unix) and the path to download it (HTTP GET to the server's base
        URL + that path, with the same Authorization: Bearer header)."""
        need(ctx, "list_backups")
        return backups_of(instance(server_name, role, ctx, drafts_too=True), min(max(limit, 1), 200))

    @server.tool()
    def read_server(server_name: str, ctx: Context) -> dict:
        """(admin token) The whole configuration of a server, active or
        draft, as the web form saves it: status, type, and per instance (production,
        test) database, odoo_config, log_file, venv_bin, restart, repositories,
        auto_update, update_modules, odoo_bin, run_as, http_port, domain. A draft saved
        incomplete has error, which says what is missing."""
        need(ctx, "read_server")
        machine_only(ctx, "read_server")
        srv = settings.servers.get(server_name)
        if srv is None:
            raise ToolError(f"No server {server_name}")
        data = {"name": srv.name, **server_to_dict(srv)}
        if srv.error:
            data["error"] = srv.error
        return data

    @server.tool()
    async def add_draft_server(server_name: str, ctx: Context, production: dict | None = None,
                               test: dict | None = None) -> dict:
        """(admin token) Add a new server as a draft: it is not live, has no
        actions and no auto update until an administrator runs Test & activate
        on the web interface. production and test take the fields read_server
        gives; at least one is needed. A draft may be saved incomplete: then
        the answer has error, which says what to fix on the web form."""
        need(ctx, "add_draft_server")
        machine_only(ctx, "add_draft_server")
        data = {role: value for role, value in (("production", production), ("test", test)) if value}
        if not data:
            raise ToolError("Give production, test or both")
        try:
            srv = await add_draft(server_name, data)
        except ConfigError as exc:
            raise ToolError(str(exc)) from exc
        result = {"name": srv.name, **server_to_dict(srv)}
        if srv.error:
            result["error"] = srv.error
        return result

    @server.tool()
    async def dev_setup(server_name: str, ctx: Context, role: str = "test") -> dict:
        """What is needed to rebuild an instance locally: every repository
        (remote URL, branch, commit), the backups, and what the server's type
        tells (Odoo: Odoo and Python versions, the addons_path as repository
        folders, the Odoo config options without secrets nor server paths).
        Of an active server; with an admin token, of a draft too."""
        inst = instance(server_name, role, ctx, drafts_too=True)
        repos = []
        for repo in inst.repositories:
            _, url = await ops.capture(["git", "remote", "get-url", repo.remote], cwd=repo.path)
            _, commit = await ops.capture(["git", "rev-parse", "HEAD"], cwd=repo.path)
            repos.append({
                "name": repo.name, "url": _public_url(url), "remote": repo.remote,
                "branch": repo.branch, "commit": commit,
                "requirements": sorted(p.name for p in repo.path.glob("requirements*.txt")),
            })
        # the type's part; it may add to the repositories
        own = await inst.plugin.dev_setup(inst, repos)
        return {"server": inst.server, "role": role, "type": inst.type, **own,
                "repositories": repos, "backups": backups_of(inst, 10)}

    # --- the linked OmniaCLI: one MCP for every machine --------------------

    async def linked(ctx: Context, link: str, method: str, params: dict) -> dict:
        """A JSON-RPC call to the /mcp of a linked OmniaCLI, through its link,
        as this token (its role caps the link's there)."""
        machine_only(ctx, "a linked OmniaCLI")
        if link_call is None:
            raise ToolError("This OmniaCLI reaches no linked one")
        user = re.sub(r"[^A-Za-z0-9._@-]", "-", f"mcp-{caller(ctx)}")[:64]
        try:
            return await link_call(link, user, role_of(ctx), {"jsonrpc": "2.0", "id": 1, "method": method,
                                                              "params": params})
        except LookupError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool()
    def list_links(ctx: Context) -> list[dict]:
        """The other OmniaCLI this one is linked to: call their tools with
        linked_tools and call_linked, as if they were here."""
        machine_only(ctx, "list_links")
        return [{"name": t.name, "url": t.url} for t in settings.link_targets.values()]

    @server.tool()
    async def linked_tools(link: str, ctx: Context) -> list[dict]:
        """The tools of a linked OmniaCLI for this token (its role caps the
        link's there): name, what it does, its arguments."""
        answer = await linked(ctx, link, "tools/list", {})
        if "error" in answer:
            raise ToolError(f"{link}: {answer['error'].get('message', answer['error'])}")
        return [{"name": t.get("name"), "description": (t.get("description") or "").split("\n\n")[0],
                 "arguments": sorted((t.get("inputSchema") or {}).get("properties") or {})}
                for t in (answer.get("result") or {}).get("tools") or []]

    @server.tool()
    async def call_linked(link: str, tool: str, ctx: Context,
                          arguments: dict | None = None) -> dict | list | str | int | float | bool | None:
        """Call a tool of a linked OmniaCLI (see linked_tools) with its
        arguments: list_servers, diagnose_instance, restart_instance... there,
        with the same rules (a production asks confirm=<server name>)."""
        answer = await linked(ctx, link, "tools/call", {"name": tool, "arguments": arguments or {}})
        if "error" in answer:
            raise ToolError(f"{link}: {answer['error'].get('message', answer['error'])}")
        result = answer.get("result") or {}
        text = " ".join(c.get("text", "") for c in result.get("content") or [] if isinstance(c, dict))
        if result.get("isError"):
            raise ToolError(f"{link}: {text}")
        content = result.get("structuredContent")
        if isinstance(content, dict):
            return content["result"] if set(content) == {"result"} else content
        try:
            return json.loads(text)
        except ValueError:
            return text

    # --- acting on an instance: production asks an admin's token and its name typed

    def acting(ctx: Context, tool: str, server_name: str, role: str, confirm: str) -> Instance:
        """The instance a tool acts on, for this token: a test's needs a
        developer, a production's an admin and confirm = the server's name,
        as the web interface has its name typed."""
        need(ctx, tool)
        inst = instance(server_name, role, ctx)
        production(ctx, tool, inst, confirm)
        return inst

    def production(ctx: Context, tool: str, inst: Instance, confirm: str, confirm_needed: bool = True) -> None:
        """A production is updated by hand only (apiguard.production_refusal, as the REST API): an admin
        token with confirm = its name, never a CI's token; a staging is not one."""
        refused = apiguard.production_refusal(by_hand(inst), inst.server, role_of(ctx), ci_of(ctx), confirm or "",
                                              tool, confirm_needed)
        if refused is not None:
            raise ToolError(f"{refused.code}: {refused.message}")

    async def once(ctx: Context, tool: str, key: str, arguments: dict, run) -> dict:
        """A tool that starts a job, with its idempotency_key: the same key with the same arguments
        within 24 hours answers the job of the first call (shared with the REST API's keys)."""
        if not key or guard is None:
            return await run()
        refused = guard.idempotency.check_key(key)
        if refused is not None:
            raise ToolError(f"{refused.code}: {refused.message}")
        who = token_of(ctx) or caller(ctx)
        print_ = apiguard.fingerprint("MCP", tool, {k: v for k, v in arguments.items() if k != "wait_seconds"})
        async with guard.idempotency.lock(who, key):
            try:
                before = guard.idempotency.lookup(who, key, print_)
            except apiguard.ApiError as exc:
                raise ToolError(f"{exc.code}: {exc.message}") from None
            if before is not None:
                job = jobs.get(str((before["body"] or {}).get("id") or ""))
                return _job_dict(job, tail=30) if job is not None else before["body"]
            answer = await run()
            guard.idempotency.remember(who, key, print_, 200, {"id": answer.get("id")})
            return answer

    async def submit(ctx: Context, title: str, inst: Instance, action, wait_seconds: int) -> dict:
        job = jobs.submit(title, [inst.key], f"mcp: {caller(ctx)}", action)
        await _wait(job, wait_seconds)
        return _job_dict(job, tail=30)

    @server.tool()
    async def diagnose_instance(server_name: str, ctx: Context, role: str = "production") -> dict:
        """Everything to tell what is wrong with an instance, in one answer:
        whether it runs, whether it answers (the monitor: since when, uptime
        of the last day), CPU and memory, the ERROR records of its log (the
        last 30, with tracebacks), its last jobs (a failed one with its error
        and the end of its log), its last backup and the last restore tried."""
        inst = instance(server_name, role, ctx)
        try:
            state = await ops.service_state(inst) if inst.restart.type != "process" else (
                "running" if ops.managed_pid(inst) else "stopped")
        except Exception as exc:  # noqa: BLE001 - said, not fatal: the rest still helps
            state = f"unknown ({exc})"
        try:
            errors = (await log_lines(inst, 30, True))["lines"]
        except ToolError as exc:
            errors = [str(exc)]
        recent = [j for j in filter_jobs(jobs.recent(len(jobs.jobs)), server_name) if inst.key in j.instances][:5]
        series = sampler.series(inst.key)
        folder = settings.backup_dir / inst.server / inst.role
        backups = backups_of(inst, 1)
        checked = backupcheck.load(folder)
        verified = [{"file": n, **e["verified"]} for n, e in checked.items() if (e or {}).get("verified")]
        return {
            "server": inst.server, "role": inst.role, "type": inst.type, "state": state,
            "load": series[-1] if series else None, "job_running": jobs.is_busy(inst.key),
            "reachable": uptime(inst.key) if uptime else None,
            "log_errors": errors,
            "last_jobs": [_job_dict(j, tail=15 if j.status == "failed" else 0) for j in recent],
            "last_backup": backups[0] if backups else None,
            "last_restore_tried": max(verified, key=lambda v: v.get("time") or "") if verified else None,
        }

    @server.tool()
    async def restart_instance(server_name: str, role: str, ctx: Context, confirm: str = "",
                               wait_seconds: int = 0, idempotency_key: str = "") -> dict:
        """Restart an instance (whoever is connected loses the connection for
        a few seconds). A production's: an admin token and confirm=<server name>. A token marked CI never acts on a production. idempotency_key:
        the same key with the same arguments within 24 hours answers the same job."""
        inst = acting(ctx, "restart_instance", server_name, role, confirm)
        return await once(ctx, "restart_instance", idempotency_key, {"server_name": server_name, "role": role},
                          lambda: submit(ctx, f"Restart {inst.key}", inst, lambda job: ops.restart(job, inst),
                                         wait_seconds))

    @server.tool()
    async def start_instance(server_name: str, role: str, ctx: Context, confirm: str = "",
                             wait_seconds: int = 0, idempotency_key: str = "") -> dict:
        """Start a stopped instance. A production's: an admin token and confirm=<server name>. A token marked CI never acts on a production. idempotency_key:
        the same key with the same arguments within 24 hours answers the same job."""
        inst = acting(ctx, "start_instance", server_name, role, confirm)
        return await once(ctx, "start_instance", idempotency_key, {"server_name": server_name, "role": role},
                          lambda: submit(ctx, f"Start {inst.key}", inst, lambda job: ops.start(job, inst),
                                         wait_seconds))

    @server.tool()
    async def stop_instance(server_name: str, role: str, ctx: Context, confirm: str = "",
                            wait_seconds: int = 0, idempotency_key: str = "") -> dict:
        """Stop an instance: it stays stopped, also for the auto update, until
        start_instance. A production's: an admin token and confirm=<server name>. A token marked CI never acts on a production. idempotency_key:
        the same key with the same arguments within 24 hours answers the same job."""
        inst = acting(ctx, "stop_instance", server_name, role, confirm)
        return await once(ctx, "stop_instance", idempotency_key, {"server_name": server_name, "role": role},
                          lambda: submit(ctx, f"Stop {inst.key}", inst, lambda job: ops.stop(job, inst),
                                         wait_seconds))

    @server.tool()
    async def update_production(server_name: str, ctx: Context, confirm: str = "", wait_seconds: int = 0,
                                idempotency_key: str = "") -> dict:
        """Update the production instance of a server as update_test does a test:
        first a backup (always, whatever the instance's setting), then git pull
        of its repositories, the changed modules updated, a restart. A
        production is updated by hand only: an admin token and
        confirm=<server name>, never a CI token. A staging (its production is
        a copy) is updated with a developer token, no confirm: what a CI does."""
        inst = acting(ctx, "update_production", server_name, "production", confirm)

        async def action(job):
            job.log("--- a backup first")
            made = await ops.backup(job, inst, settings.backup_dir)
            job.log(f"Backup {made.name}")
            await ops.pull_restart(job, inst)
        return await once(ctx, "update_production", idempotency_key, {"server_name": server_name},
                          lambda: submit(ctx, f"Backup, git pull and restart {inst.key}", inst, action, wait_seconds))

    def as_principal(ctx: Context) -> Principal:
        """The token as the pages' code takes who acts."""
        scope = scope_of(ctx)
        return Principal(caller(ctx), role_of(ctx), "token", workspaces=((scope, role_of(ctx)),) if scope else (),
                         scoped=bool(scope))

    @server.tool()
    async def create_staging(server_name: str, ctx: Context, wait_seconds: int = 0, idempotency_key: str = "") -> dict:
        """(developer token) A new staging of a production of a workspace,
        beside it: the same template, a backup of the production made now
        restored neutralized, its mail caught, its test stopped. It does not
        change the production: a CI token may make one. The answer has the
        job and server, the staging's name (then update_production,
        restart_instance... on it). idempotency_key: the same key within 24
        hours answers the same job."""
        need(ctx, "create_staging")
        if api.get("staging") is None:
            raise ToolError("This OmniaCLI makes no staging through MCP")
        instance(server_name, "production", ctx)

        async def run():
            found = await api["staging"](server_name, as_principal(ctx), source=f"mcp: {caller(ctx)}")
            if not isinstance(found, dict):
                raise ToolError(_refusal(found))
            job = jobs.get(found["job"])
            await _wait(job, wait_seconds)
            return {**_job_dict(job, tail=30), "server": found["server"]}
        return await once(ctx, "create_staging", idempotency_key, {"server_name": server_name}, run)

    @server.tool()
    async def list_deploys(server_name: str, role: str, ctx: Context, limit: int = 20) -> dict:
        """(developer token) The deploys of an instance, newest first: each job
        that moved its code, with each repository's commit before and after,
        who started it, its outcome; current marks the one the code is at.
        Give an id to deploy_back to go back to it."""
        need(ctx, "list_deploys")
        inst = instance(server_name, role, ctx)
        if api.get("deploys") is None:
            raise ToolError("This OmniaCLI lists no deploys through MCP")
        found = await api["deploys"](inst, scope_of(ctx))
        return {"ci": found["ci"], "deploys": [
            {**{k: r.get(k) for k in ("id", "kind", "started", "finished", "by", "outcome", "current", "error")},
             "repositories": [{k: x.get(k) for k in ("name", "branch", "before", "after", "subject")}
                              for x in r.get("repositories") or []]}
            for r in found["deploys"][:min(max(limit, 1), 100)]]}

    @server.tool()
    async def deploy_back(server_name: str, role: str, deploy_id: str, ctx: Context, confirm: str = "",
                          pause: bool = False, wait_seconds: int = 0, idempotency_key: str = "") -> dict:
        """(developer token) Back to a deploy of list_deploys (the pages' «Back to
        this deploy»): a backup first, each repository at that deploy's commit
        on its branch, the changed modules updated, a restart; the database is
        not taken back. pause: switch the instance's CI off first, so a push
        does not deploy the branch's head again. A production's: an admin
        token and confirm=<server name>, never a CI token."""
        inst = acting(ctx, "deploy_back", server_name, role, confirm)
        if api.get("deploy_back") is None:
            raise ToolError("This OmniaCLI goes back to no deploy through MCP")

        async def run():
            found = await api["deploy_back"](inst, deploy_id, as_principal(ctx), pause, f"mcp: {caller(ctx)}")
            if not isinstance(found, Job):
                raise ToolError(_refusal(found))
            await _wait(found, wait_seconds)
            return _job_dict(found, tail=30)
        return await once(ctx, "deploy_back", idempotency_key,
                          {"server_name": server_name, "role": role, "deploy_id": deploy_id, "pause": pause}, run)

    @server.tool()
    def list_actions(server_name: str, role: str, ctx: Context) -> list[dict]:
        """The actions of the instance's type (Odoo in Docker: Pull image,
        Update modules...; a venv's Odoo: pip, the modules...): id, label, the
        role they need, their parameters. Run one with run_action."""
        need(ctx, "list_actions")
        inst = instance(server_name, role, ctx)
        return [{"id": a.id, "label": a.label, "role": a.role, "tooltip": a.tooltip if isinstance(a.tooltip, str) else "",
                 "params": [{"key": f.key, "label": f.label, "kind": f.kind, "default": f.default,
                             "choices": [list(c) for c in (f.choices or ())]} for f in a.params]}
                for a in inst.plugin.actions() if a.scope == "instance" and a.applies_to(inst)]

    @server.tool()
    async def run_action(server_name: str, role: str, action: str, ctx: Context, params: dict | None = None,
                         confirm: str = "", wait_seconds: int = 0) -> dict:
        """Run an action of list_actions on an instance, with its params
        ({key: value}). A production's: an admin token and confirm=<server name>."""
        inst = acting(ctx, "run_action", server_name, role, confirm)
        found = next((a for a in inst.plugin.actions() if a.id == action and a.scope == "instance"
                      and a.applies_to(inst)), None)
        if found is None:
            raise ToolError(f"{inst.plugin.label} has no action {action} for {inst.key}: see list_actions")
        token_role = role_of(ctx)
        if ACCESS_ROLES.index(token_role) < ACCESS_ROLES.index(found.role):
            raise ToolError(f"{action} needs the role {found.role}; this token is {token_role}")
        given = dict(params or {})
        values = {}
        for f in found.params:
            raw = given.get(f.key, f.default)
            if f.kind == "checkbox":
                values[f.key] = raw in (True, "true", "1", 1, "on")
            elif f.kind == "number":
                try:
                    values[f.key] = int(raw) if raw not in (None, "") else None
                except (TypeError, ValueError):
                    raise ToolError(f"{f.key}: a whole number") from None
            else:
                values[f.key] = "" if raw is None else str(raw).strip()
        try:
            if found.prepare is not None:
                values = found.prepare(inst, values)
        except ActionRefused as exc:
            raise ToolError(str(exc)) from exc
        return await submit(ctx, found.title_of(inst, values), inst, lambda job: found.run(job, inst, values),
                            wait_seconds)

    @server.tool()
    async def verify_backup(server_name: str, role: str, file: str, ctx: Context, wait_seconds: int = 0) -> dict:
        """Check a backup of list_backups: its SHA-256, then a restore tried in
        a database of its own, dropped after (the instance is not touched)."""
        need(ctx, "verify_backup")
        inst = instance(server_name, role, ctx)
        if not inst.plugin.has_verify:
            raise ToolError(f"{inst.plugin.label} cannot try a restore")
        path = settings.backup_dir / inst.server / inst.role / file
        if "/" in file or not file.endswith(".zip") or not path.is_file():
            raise ToolError(f"No backup {file} of {inst.key}")
        return await submit(ctx, f"Verify backup {file} of {inst.key}", inst,
                            lambda job: backupcheck.verify(job, inst, path), wait_seconds)

    @server.tool()
    async def full_archive(server_name: str, role: str, ctx: Context, wait_seconds: int = 0) -> dict:
        """The full archive of an instance in its backups: the backup, each
        repository's code at its commit, the configuration, a manifest with
        checksums (another OmniaCLI makes the instance again from it). A
        production's needs an admin token."""
        need(ctx, "full_archive")
        inst = instance(server_name, role, ctx)
        if inst.role == "production" and role_of(ctx) != "admin":
            raise ToolError("full_archive of a production needs an MCP token with role admin")
        folder = settings.backup_dir / inst.server / inst.role
        return await submit(ctx, f"Full archive of {inst.key}", inst,
                            lambda job: instancearchive.make(job, inst, folder), wait_seconds)

    @server.tool()
    async def database_report(server_name: str, role: str, ctx: Context) -> dict:
        """The health of an instance's database: size, connections, cache hits,
        the queries running for long, the costliest statements
        (pg_stat_statements, when installed), the largest tables, the indexes
        never used, the tables read whole (an index may be missing)."""
        need(ctx, "database_report")
        try:
            return await pgreport.report(instance(server_name, role, ctx))
        except JobFailed as exc:
            raise ToolError(str(exc)) from exc

    @server.tool()
    def stop_job(job_id: str, ctx: Context) -> dict:
        """Stop a running job (a stuck pull, a long backup)."""
        need(ctx, "stop_job")
        job = jobs.get(job_id)
        if job is None or not job_seen(ctx, job):
            raise ToolError(f"No job {job_id}")
        if not jobs.stop(job_id, f"mcp: {caller(ctx)}"):
            raise ToolError(f"{job_id} is not running")
        return _job_dict(job)

    # --- a database from outside into a new instance ----------------------

    def calls_through_a_link(ctx: Context) -> bool:
        _, _, token = (ctx.headers or {}).get("authorization", "").partition(" ")
        return authenticator.token_name(token.strip()) is None

    @server.tool()
    def upload_backup(ctx: Context, name: str, size: int, workspace: str = "", link: str = "",
                      sha256: str = "") -> dict:
        """(admin token) Step 1 of "restore the database X on the server Y as a
        new instance": a one-time upload URL (valid 30 minutes, for this token
        only) where you PUT the file with the same Authorization: Bearer header.
        The file: an Odoo backup zip (dump.sql, filestore/, manifest.json, as
        odoo db dump or the database manager make it), name ending in .zip, or
        a plain SQL dump of pg_dump, name ending in .sql. size: its exact size
        in bytes; sha256: its SHA-256, checked when given. workspace: the
        workspace that will hold the new instance (a workspace's token: its
        own). link: the linked machine to send it to (list_links), e.g.
        upload_backup(name="acme.zip", size=1234, workspace="acme",
        link="lab1"): you still PUT here, this OmniaCLI sends it on (a job,
        get_job) and keeps no copy. Then restore_new_instance (through
        call_linked for a link) with the file the answer names."""
        need(ctx, "upload_backup")
        if restores is None:
            raise ToolError("This OmniaCLI takes no upload")
        if calls_through_a_link(ctx):
            raise ToolError("upload_backup through a link: call it on the OmniaCLI you talk to, with link=<this machine>")
        _, _, token = (ctx.headers or {}).get("authorization", "").partition(" ")
        request = getattr(ctx.request_context, "request", None)
        base = str(getattr(request, "base_url", "") or "")
        try:
            return restores["upload"](authenticator.token_name(token.strip()), scope_of(ctx), workspace, name, size,
                                      sha256, link, base)
        except ActionRefused as exc:
            raise ToolError(str(exc)) from exc

    @server.tool()
    async def restore_new_instance(ctx: Context, file: str, workspace: str = "", dry_run: bool = True,
                                   name: str = "", neutralize: bool = True, staging_of: str = "",
                                   backup_of: str = "", add_repositories: bool = False,
                                   allow_missing: bool = False, wait_seconds: int = 0) -> dict:
        """(admin token) Step 2 of "restore the database X on the server Y as a
        new neutralized instance": a new instance of workspace made from the
        template of the database's Odoo version, the file restored into it,
        neutralized (no scheduled actions, no outgoing mail) unless
        neutralize=false. file: the name upload_backup answered (the
        workspace's import area), or with backup_of=<server> a backup of
        that server (list_backups). FIRST call it with dry_run=true (the
        default): nothing starts, the answer says the Odoo version, the
        modules installed in the database that have no code in the new
        instance, grouped by where they are (in_oca: OCA repositories of the
        catalog, which add_repositories=true adds; not_found: to be provided,
        e.g. a customer's private repository or Odoo Enterprise) and the
        room. Tell the user and ask before the second call, dry_run=false
        (with add_repositories=true and, only if the user accepts the
        missing modules, allow_missing=true): it answers a job (get_job).
        name: the new instance's name (default: the database's);
        staging_of=<a production of the workspace> makes it a staging of it
        (dated, its mail caught). For a linked machine: call_linked(link,
        "restore_new_instance", {...}). Example: restore_new_instance(
        file="acme.zip", workspace="acme") then restore_new_instance(
        file="acme.zip", workspace="acme", dry_run=false,
        add_repositories=true)."""
        need(ctx, "restore_new_instance")
        if restores is None:
            raise ToolError("This OmniaCLI makes no instance from a database")
        try:
            answer = await restores["restore"](caller(ctx), role_of(ctx), scope_of(ctx), workspace=workspace,
                                               file=file, backup_of=backup_of, name=name, neutralize=neutralize,
                                               staging_of=staging_of, dry_run=dry_run,
                                               add_repositories_=add_repositories, allow_missing=allow_missing)
        except ActionRefused as exc:
            raise ToolError(str(exc)) from exc
        job = jobs.get(answer.get("job") or "") if not dry_run else None
        if job is not None:
            await _wait(job, wait_seconds)
            answer["job"] = _job_dict(job, tail=30)
        return answer

    server.streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True, json_response=True,
        # the bearer token already stops what DNS rebinding protection is for,
        # and it would refuse every Host but localhost behind a reverse proxy
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    manager = server.session_manager
    from mcp.server.streamable_http_manager import StreamableHTTPASGIApp

    endpoint = StreamableHTTPASGIApp(manager)

    class Guarded:
        """The MCP endpoint behind the bearer token check (an ASGI app: a
        plain function would be taken by Starlette for a request handler)."""

        async def __call__(self, scope, receive, send):
            headers = dict(scope.get("headers") or [])
            scheme, _, token = headers.get(b"authorization", b"").decode(errors="replace").partition(" ")
            name = authenticator.token_name(token.strip()) if scheme.lower() == "bearer" else None
            ip = (scope.get("client") or ("",))[0]
            role, workspace, who, kind = settings.token_role(name or ""), settings.token_workspace(name or ""), "", "mcp"
            link = authenticator.link_name(token.strip()) if scheme.lower() == "bearer" and name is None else None
            if link is not None and link in settings.links:
                # another OmniaCLI's MCP, for one of its tokens: the link's
                # role, capped by the token's there
                role = settings.links[link].role
                cap = headers.get(b"x-omniacli-role-cap", b"").decode(errors="replace")
                if cap in ACCESS_ROLES and ACCESS_ROLES.index(cap) < ACCESS_ROLES.index(role):
                    role = cap
                on_behalf = headers.get(b"x-omniacli-user", b"").decode(errors="replace")
                who = f"link {link}" + (f" ({on_behalf})" if _ON_BEHALF.fullmatch(on_behalf) else "")
                name, workspace, kind = who, "", "link"
            if name is None:
                access_log.record("failed", "mcp", "", ip, f"{scope.get('method', '')} /mcp")
                response = JSONResponse({"error": "A bearer token from auth.tokens is required"},
                                        status_code=401, headers={"WWW-Authenticate": "Bearer"})
                await response(scope, receive, send)
                return
            # read the body to log its method, then hand it on unchanged
            chunks, more = [], True
            while more:
                message = await receive()
                if message["type"] != "http.request":
                    break
                chunks.append(message.get("body", b""))
                more = message.get("more_body", False)
            body = b"".join(chunks)
            server = _rpc_server(body)
            if guard is not None and kind == "mcp":
                # the rate limit of the token, as on the REST API: a call of a tool that changes something is a write
                write = any(t in WRITE_TOOLS for t in _rpc_tools(body))
                refused = guard.limiter.refusal(name, write)
                if refused is not None:
                    access_log.record("mcp", kind, name, ip, f"refused, rate limit: {_rpc_summary(body)}"[:300],
                                      role=role, server=server, status=429)
                    response = JSONResponse(refused.body(), status_code=429, headers=refused.headers)
                    await response(scope, receive, send)
                    return
            access_log.record("mcp", kind, name, ip, _rpc_summary(body) or f"{scope.get('method', '')} /mcp",
                              role=role, server=server,
                              workspace=workspace or (settings.workspace_of(server) if server else "") or "")
            _ROLE.set(role)
            _SCOPE.set(workspace)
            _WHO.set(who)
            sent = False

            async def replay():
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()

            await endpoint(scope, replay, send)

    return Route("/mcp", endpoint=Guarded(), methods=["GET", "POST", "DELETE"]), manager
