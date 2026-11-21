# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The OmniaCLI web application."""

from __future__ import annotations

import asyncio
import configparser
import contextlib
import dataclasses
import datetime
import functools
import importlib.metadata
import importlib.util
import ipaddress
import json
import logging
import os
import pwd
import re
import shutil
import socket
import tempfile
import time
import zipfile
from dataclasses import asdict
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, quote, urlsplit

import httpx
import jinja2
from fastapi import Body, Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketException, status
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask
from starlette.requests import ClientDisconnect, HTTPConnection

from . import (backupcheck, backupchoices, checks, deployhooks, destinations, instancearchive, private, restoreroom, consumer, disks, gitcredentials, linkcopy, listgroups, ops, packages, plugins, pluginstore, puller, publisher,
               schedules, selflog, sshlinks, shell, stacktemplates, templatestore, tunnel, workspacehelper, workspaceops)
from .accesslog import AccessLog
from . import i18n, loginas, machines, pgreport, stagings, workspaceweb
from . import stagingmail
from .diskhistory import DiskHistory, DiskRecorder
from .auth import ROLES as ACCESS_ROLES
from .auth import WORKSPACE_ROLE, Authenticator, Principal, hash_password, hash_token, new_token, verify_password
from .config import (
    PUBLISHED_COMMANDS,
    PULLER_FILE_MIB,
    PULLER_KEEP_HOURS,
    ROLES,
    SERVER_NAME,
    MACHINE_DEVELOPMENT,
    SHELL_FONT_SIZE,
    SHELL_FONT_SIZES,
    TUNNEL_IDLE_HOURS,
    USER_ROLES,
    WORKSPACE_LIMITS,
    Access,
    CERTIFICATES,
    ConfigError,
    Instance,
    LinkAccess,
    LinkSource,
    LinkTarget,
    Settings,
    TEST_LABEL,
    TOKEN_DEFAULT_ROLE,
    WORKSPACE_NAME,
    Workspace,
    load_settings,
    matches,
    parse_link,
    parse_connection,
    parse_notify,
    parse_options,
    parse_publication,
    parse_puller,
    parse_servers,
    parse_workspaces,
    save_access,
    save_connections,
    save_links,
    save_options,
    save_publications,
    apply_sites,
    web_names,
    save_pullers,
    save_servers,
    save_workspaces,
    server_text,
    server_to_dict,
)
from .jobs import SUCCESS, JobFailed, JobManager, filter_jobs
from .plugin_api import ENTRY_POINT_GROUP, PLUGIN_API, ActionRefused, Host, restore_fetched
from .pullcrypto import new_key
from .links import LIST_TIMEOUT, LinkClients, describe_error, link_headers, proxy_websocket
from .monitor import FILE as MONITOR_FILE, Monitor
from .logtail import read_since
from .metrics import Sampler
from .scheduler import AutoUpdater
from .sessions import COOKIE, LIFETIME, LoginLimiter, SessionStore


def request_language(request: HTTPConnection) -> str:
    """The language of the page (i18n.py): the cookie, else the browser's."""
    return i18n.choose(request.cookies.get(i18n.COOKIE), request.headers.get("accept-language"))


def _page_context(request: Request) -> dict:
    """In every template: prefix, the path the pages are served under (a
    linked instance's, empty otherwise), and what the caller may do."""
    user = getattr(request.state, "user", None)
    settings = getattr(request.app.state, "settings", None)
    lang = request_language(request)
    return {
        # the language (i18n.py): _() for the template's own texts, tr() for
        # what the code made (an error, a job's line)
        "lang": lang,
        "languages": i18n.LANGUAGES,
        "_": functools.partial(i18n.gettext, lang),
        "tr": functools.partial(i18n.tr, lang),
        "prefix": getattr(request.state, "prefix", ""),
        # the text size the consoles (logs, terminals) open with: the Options
        # page's, within its limits (_console.html)
        "console_font": settings.options.shell_font_size if settings else SHELL_FONT_SIZE,
        "console_font_sizes": SHELL_FONT_SIZES,
        "can_act": bool(user and user.can("developer")),
        # a shell of OmniaCLI's own user: not on a server
        "own_user_shells": bool(settings and settings.machine == MACHINE_DEVELOPMENT),
        # restart, start and stop: every role; an MCP token only reads
        "can_restart": bool(user and user.kind != "token"),
        "is_admin": bool(user and user.can("admin")),
        # a user of workspaces: none of the machine's menus and consoles
        "scoped": bool(user and user.scoped),
        # the workspaces whose users it manages (admin there)
        "my_workspaces": [ws for ws, role in user.workspaces if role == "admin"] if user and user.scoped else [],
        # the Backups of every machine (UX-008 §4.2): a developer's, or a
        # user of workspaces who is a developer in one of them at least
        "backups_menu": bool(user and (any(r in ("developer", "admin") for _, r in user.workspaces)
                                       if user.scoped else user.can("developer"))),
        # a user who logged in with a password (not a token or a link)
        "account": user.name if user and user.kind == "user" else None,
        # in the footer of every page after the login
        "version": _version(),
        # the instances operated through a relay (consumer.py), in the menu
        "puller_connections": sorted(settings.connections) if settings else [],
    }


class _TemplateLoader(jinja2.BaseLoader):
    """The core's templates, and "<plugin>/<name>" from the templates_dir of
    a loaded plugin (looked up at every load: the plugins may be replaced)."""

    def __init__(self, core: Path):
        self.core = jinja2.FileSystemLoader(str(core))

    def get_source(self, environment, template):
        name, _, rest = template.partition("/")
        plugin = plugins.registry().get(name) if rest else None
        folder = plugin.templates_dir() if plugin is not None else None
        if folder is not None:
            return jinja2.FileSystemLoader(str(folder)).get_source(environment, rest)
        return self.core.get_source(environment, template)


_TEMPLATES = Jinja2Templates(
    env=jinja2.Environment(loader=_TemplateLoader(Path(__file__).parent / "templates"),
                           autoescape=jinja2.select_autoescape()),
    context_processors=[_page_context])
# The OmniaSolutions "O" (Client2019 Setup/dist/images/omnia.ico, 355x280,
# centred on a transparent square); served without login, like any favicon.
_STATIC = Path(__file__).parent / "static"
_BACKUP_NAME = re.compile(r"[A-Za-z0-9._-]+\.zip")
# a value of a checklist field (backup_fields): a name, schema.table.column
_CHECKLIST_VALUE = re.compile(r"[A-Za-z0-9_.$:-]{1,200}")
# the terminal of the shell page: xterm.js 6.0.0 and its fit addon 0.11.0
# (npm @xterm/xterm and @xterm/addon-fit, MIT), served after the login
_STATIC_FILES = {"xterm.js": "text/javascript", "xterm.css": "text/css", "addon-fit.js": "text/javascript"}
# served without login: the icons, and the background of the login page
_PUBLIC_FILES = {"/favicon.ico": ("favicon.ico", "image/x-icon"),
                 "/icon-180.png": ("icon-180.png", "image/png"),
                 "/login-background.jpg": ("login-background.jpg", "image/jpeg")}
# what a linked instance sends: the path it serves our pages under, and the
# name of its user acting through the link
_PREFIX_HEADER = "x-omniacli-prefix"
_ON_BEHALF_HEADER = "x-omniacli-user"
# the role of that user there: a link acts with the lower of it and its own
_ROLE_CAP_HEADER = "x-omniacli-role-cap"
_PREFIX = re.compile(r"(/[A-Za-z0-9._~-]+)+")
_ON_BEHALF = re.compile(r"[A-Za-z0-9._@-]{1,64}")
# users, links and MCP tokens created from the web interface
_ACCESS_NAME = re.compile(r"[A-Za-z0-9._@-]{1,64}")
_MIN_PASSWORD = 8
# what the proxy of a link passes on, each way (never the caller's credentials,
# never the other instance's WWW-Authenticate: the browser would ask for them)
_LINK_REQUEST_HEADERS = ("content-type", "accept", "accept-language")
# sent through a link: the asking OmniaCLI can carry Log in as through a tunnel
TUNNEL_HEADER = "X-OmniaCLI-Tunnel"
# Log in as asked of a linked OmniaCLI: handled here when its link cannot open there
_LINK_LOGIN_AS = re.compile(r"api/servers/([^/]+)/(production|test)/login-as")
_LINK_RESPONSE_HEADERS = ("content-type", "content-disposition", "content-length",
                          "content-encoding", "location", "cache-control")
# the only routes a user of workspaces reaches (in_workspaces): its account,
# the lists (they show its workspaces' servers only), and what a server of
# its workspaces offers, with its role in that workspace. Not the consoles
# (a shell of this machine), the copy from a link, the tunnels, the
# machine's pages and the configuration.
WORKSPACE_ROUTES = frozenset({
    "/logout", "/account", "/api/account/password", "/static/{name}", "/x/{plugin}/static/{path:path}",
    "/", "/dashboard/servers", "/api/servers", "/api/metrics",
    "/jobs", "/jobs/{job_id}", "/jobs/{job_id}/stop", "/api/jobs", "/api/jobs/{job_id}",
    "/servers/{server}", "/servers/{server}/merge", "/servers/{server}/x/{plugin}/{action}",
    "/servers/{server}/{role}/pull", "/servers/{server}/{role}/pull-restart",
    "/api/servers/{server}/{role}/pull-preview", "/api/servers/{server}/{role}/summary",
    "/api/servers/{server}/{role}/uptime", "/api/workspaces/{workspace}/notify",
    "/api/servers/{server}/{role}/database", "/api/servers/{server}/{role}/database/terminate",
    "/servers/{server}/{role}/restart", "/servers/{server}/{role}/start", "/servers/{server}/{role}/stop",
    "/servers/{server}/{role}/backup", "/api/servers/{server}/{role}/backups",
    "/backups/{server}/{role}/{name}", "/servers/{server}/{role}/x/{plugin}/{action}",
    "/servers/{server}/{role}/log", "/api/servers/{server}/{role}/log",
    "/workspaces/{workspace}", "/api/workspaces/{workspace}/members/save",
    "/api/workspaces/{workspace}/members/delete", "/api/workspaces/{workspace}/instances/create",
    "/api/workspaces/{workspace}/instances/delete", "/api/workspaces/{workspace}/web/save",
    "/api/workspaces/{workspace}/web/publish", "/api/workspaces/{workspace}/web/certificate",
    "/api/workspaces/{workspace}/credentials/save", "/api/workspaces/{workspace}/credentials/delete",
    "/api/workspaces/{workspace}/repositories/add", "/api/workspaces/{workspace}/repositories/remove",
    "/api/workspaces/{workspace}/git-key", "/api/workspaces/{workspace}/repositories/branches",
    "/api/workspaces/{workspace}/repositories/update",
    "/api/servers/{server}/{role}/backup-schedule", "/api/servers/{server}/{role}/backups/delete",
    "/api/workspaces/{workspace}/parameters/save", "/api/workspaces/{workspace}/parameters/options",
    "/api/servers/{server}/{role}/remote-backups", "/api/servers/{server}/{role}/remote-backups/fetch",
    "/api/servers/{server}/{role}/backups/verify", "/api/servers/{server}/{role}/archive",
    "/api/workspaces/{workspace}/archives", "/api/workspaces/{workspace}/archives/upload",
    "/api/workspaces/{workspace}/instances/from-archive",
    "/api/workspaces/{workspace}/archives/remote", "/api/workspaces/{workspace}/archives/fetch",
    "/api/workspaces/{workspace}/tokens/save", "/api/workspaces/{workspace}/tokens/delete",
    "/api/servers/{server}/stagings", "/api/servers/{server}/stagings/refresh",
    "/api/servers/{server}/{role}/deploy-hook", "/api/servers/{server}/pitr", "/api/servers/{server}/pitr/enable",
    "/api/servers/{server}/pitr/base", "/api/servers/{server}/pitr/restore",
    "/workspaces/{workspace}/destinations", "/api/workspaces/{workspace}/destinations/save",
    "/api/workspaces/{workspace}/destinations/delete", "/api/workspaces/{workspace}/destinations/check",
    "/api/workspaces/{workspace}/destinations/key",
    "/api/workspaces/{workspace}/load",
    # the lists of every machine (UX-008 §4): of its workspaces' instances only
    "/instances", "/api/instances", "/backups", "/api/backups", "/api/servers/{server}/stagings/from-backup",
    # a staging's caught mail (UX-008 §5.3)
    "/api/servers/{server}/production/mail", "/api/servers/{server}/production/mail/{mail_id}",
    "/api/servers/{server}/production/mail/clear",
})


_logger = logging.getLogger(__name__)
# an MCP call through a link: a tool may wait 600 s for its job
LINK_MCP_TIMEOUT = 660
# the day the automatic backups' Try a restore runs (6: Sunday)
VERIFY_WEEKDAY = 6


def _field_value(field, raw):
    """A posted parameter as its Field's kind wants it (a form posts text,
    a dialog's script JSON); ActionRefused when it cannot be one."""
    if field.kind == "checkbox":
        return raw not in (None, False, "", "0", "false", "off")
    if field.kind == "checklist":
        # the values ticked: a list (JSON), or one text of them separated by
        # commas (a form); not posted: None, nobody chose
        if raw is None:
            return None
        items = raw if isinstance(raw, list) else str(raw).split(",")
        values = [str(v).strip() for v in items if str(v).strip()]
        bad = [v for v in values if not _CHECKLIST_VALUE.fullmatch(v)]
        if bad:
            raise ActionRefused(f"{field.label}: {', '.join(bad)} is not a value of the list")
        return values
    if raw is None:
        # not posted (a script that leaves it out): its default
        raw = field.default
    text = "" if raw is None else str(raw).strip()
    if field.kind == "number":
        if text == "":
            if field.required:
                raise ActionRefused(f"{field.label}: a number is required")
            return None
        try:
            return int(text)
        except ValueError:
            raise ActionRefused(f"{field.label}: {text!r} is not a whole number") from None
    if field.required and not text:
        raise ActionRefused(f"{field.label} is required")
    if field.kind == "select" and text and text not in [str(value) for value, _ in field.choices]:
        raise ActionRefused(f"{field.label}: {text!r} is not one of the choices")
    return text


def _field_dict(field) -> dict:
    """A Field for the server form's script."""
    return {"key": field.key, "label": field.label, "kind": field.kind, "default": field.default,
            "required": field.required, "placeholder": field.placeholder, "help": field.help,
            "choices": [list(c) for c in field.choices], "restart_types": list(field.restart_types),
            "disabled_placeholder": field.disabled_placeholder,
            "shown_with": field.shown_with, "threshold": field.threshold,
            "buttons": [{"label": b.label, "route": b.route, "title": b.title, "confirm": b.confirm,
                         "only_with_value": b.only_with_value} for b in field.buttons]}


@functools.lru_cache(maxsize=1)
def _version() -> str:
    """The installed package's version (read once: it changes only with a
    new install, which restarts OmniaCLI)."""
    try:
        return importlib.metadata.version("omniacli")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


# the most each part of OmniaCLI has to stop when the service stops
STOP_SECONDS = 10


def public_address() -> str | None:
    """The IPv4 address this machine goes out with, when it is a public one:
    the value of the DNS A record of a new instance's domain (the New
    instance dialog says it). A UDP connect sends nothing; behind NAT (a
    private address) there is none to say."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 9))
            address = sock.getsockname()[0]
    except OSError:
        return None
    try:
        return address if ipaddress.ip_address(address).is_global else None
    except ValueError:
        return None


def _same_origin(request: Request) -> bool:
    """Reject cross-site form posts: the browser may send the session cookie
    with them (SameSite=Strict should stop it; this does whatever the browser)."""
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source:
        return False
    return urlsplit(source).netloc == request.headers.get("host")


# what a request does not say in its path: never a password, a token or a key
_AUDIT_FIELDS = ("name", "new_name", "role", "host", "server", "kind")
_AUDIT_MAX_BODY = 64 * 1024
# polled, or they change nothing: an access log full of them would hide the rest
_NOT_AUDITED = re.compile(r"/logout|/api/pullers/[^/]+/producer|/api/servers/(check|branches)|/api/links/[^/]+/test")


async def _audit_target(request: Request) -> str:
    """ name=... role=... of a small JSON request, for the access log."""
    if "json" not in request.headers.get("content-type", ""):
        return ""
    length = request.headers.get("content-length", "")
    if not length.isdigit() or int(length) > _AUDIT_MAX_BODY:
        return ""
    try:
        body = json.loads(await request.body())
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    return "".join(f" {key}={body[key]}" for key in _AUDIT_FIELDS if isinstance(body.get(key), (str, int)))


# on the 401 of a browser without a session (not on a link's or a token's)
_LOGIN_HEADER = "X-OmniaCLI-Login"


def _referer_path(request: Request) -> str:
    """The page of this site a request was sent from, "/" otherwise."""
    referer = urlsplit(request.headers.get("referer", ""))
    if referer.netloc != request.headers.get("host"):
        return "/"
    return _next_path(referer.path + (f"?{referer.query}" if referer.query else ""))


def _next_path(value: str) -> str:
    """Where to go after the login: a path of this site only, never another site."""
    value = value or "/"
    if not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    return value


def create_app(settings: Settings, link_transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    """link_transport replaces the network to the linked instances (tests)."""
    def link_hashes() -> dict[str, str]:
        return {name: link.token for name, link in settings.links.items()}

    authenticator = Authenticator(settings.users, settings.tokens, link_hashes())
    jobs = JobManager(settings.jobs_dir)
    gitcredentials.bind(settings)
    updater = AutoUpdater(settings, jobs)
    async def workspace_networks() -> dict[str, list[str]]:
        """{workspace: the networks of its projects}, from the root helper."""
        found = await workspaceops.space()
        networks: dict[str, list[str]] = {}
        for root in found.get("roots", []):
            networks.update(root.get("networks") or {})
        return networks

    sampler = Sampler(settings, networks=workspace_networks)
    # the disks once a day, next to the jobs like the sessions
    disk_history = DiskHistory(settings.jobs_dir.parent / "disk_history.json")
    async def workspace_disks() -> list[dict]:
        """[{name, used, capacity}] of the workspaces' disks, from the root
        helper: capacity is disk_gb, or the image's size without one."""
        found = await workspaceops.space()
        result = []
        for ws in settings.workspaces.values():
            if not ws.storage or settings.storage_problem(ws):
                continue
            r = workspaceops.room(found, settings.workspaces, ws.storage, ws.name)
            capacity = workspaceops.limit_bytes(ws) or (r or {}).get("own_size", 0)
            if r is not None and capacity:
                result.append({"name": ws.name, "used": r["own_used"], "capacity": capacity})
        return result

    disk_recorder = DiskRecorder(settings, disk_history, workspaces=workspace_disks)
    link_clients = LinkClients(link_transport)
    # the links through SSH: their ssh -L, and OmniaCLI's key, next to the links file
    ssh_tunnels = sshlinks.SshTunnels((settings.links_file or settings.servers_file).parent / "ssh")
    # the pullers of relays this one is a consumer of: their state next to the jobs
    consumers = consumer.Consumers(settings, link_clients, settings.jobs_dir.parent / "connections")
    # the scheduled actions; a schedules file that cannot be read is shown on
    # their page, and OmniaCLI starts without them
    scheduler = schedules.Scheduler(settings, jobs, link_clients,
                                    schedules.ScheduleHistory(settings.jobs_dir.parent / "schedule_history.json"),
                                    consumers)
    schedules_error = ""
    try:
        scheduler.load()
    except ConfigError as exc:
        schedules_error = str(exc)
    access_log = AccessLog(settings.logs_dir / "access.jsonl")
    # OmniaCLI's own log (selflog), shown to the admins
    own_log_file = settings.logs_dir / "omniacli.log"
    # the browsers' logins: next to the jobs, like the access log
    sessions = SessionStore(settings.jobs_dir.parent / "sessions.json")
    tunnels = tunnel.TunnelManager(
        lambda: settings.options, link_headers,
        lambda user, detail: access_log.record("tunnel", "user", user, "", detail))
    limiter = LoginLimiter()
    # the links of the Log in as button (loginas.py)
    login_tickets = loginas.LoginTickets()
    # the pullers this instance relays: their state next to the jobs
    pullers = puller.PullerHub(settings, settings.jobs_dir.parent / "pullers")

    async def published_instance(inst: Instance) -> dict:
        """What the report of a publication says of its instance (publisher.py)."""
        return {"state": await run_state(inst), "busy": jobs.is_busy(inst.key), "restart": inst.restart.type,
                "backup_fields": [_field_dict(f) for f in inst.plugin.backup_fields()]}

    def published_backup_options(inst: Instance, params: dict) -> dict:
        return {f.key: _field_value(f, params.get(f.key)) for f in inst.plugin.backup_fields()}

    # the instances of this one published on a puller of a relay
    publications = publisher.Publisher(settings, jobs, sampler, disk_history, link_clients,
                                       settings.jobs_dir.parent / "publications", published_instance,
                                       published_backup_options, _version())

    def identify(request: HTTPConnection) -> Principal:
        # a WebSocket has no method: it is checked as a post (no token, same origin)
        method = request.scope.get("method", "WEBSOCKET")
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() == "bearer":
            token = token.strip()
            name = authenticator.token_name(token)
            if name is not None:
                # an MCP client's token: it may read what its role allows (and
                # download backups); its actions go through the MCP tools only
                if method not in ("GET", "HEAD"):
                    raise HTTPException(status.HTTP_403_FORBIDDEN, "A token may only read")
                return Principal(name, settings.token_role(name), "token")
            name = authenticator.link_name(token)
            if name is None:
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token",
                                    headers={"WWW-Authenticate": "Bearer"})
            # a linked instance: no browser sends its token by itself, so no
            # cross-site check; it serves our pages under its own prefix
            prefix = request.headers.get(_PREFIX_HEADER, "")
            request.state.prefix = prefix if _PREFIX.fullmatch(prefix) else ""
            on_behalf = request.headers.get(_ON_BEHALF_HEADER, "")
            role = settings.links[name].role
            cap = request.headers.get(_ROLE_CAP_HEADER, "")
            if cap in ACCESS_ROLES and ACCESS_ROLES.index(cap) < ACCESS_ROLES.index(role):
                role = cap
            return Principal(name, role, "link", on_behalf if _ON_BEHALF.fullmatch(on_behalf) else "")
        # a browser: the session of the login form
        name = sessions.user(request.cookies.get(COOKIE), settings.users)
        if name is None:
            # marked, so the pages' scripts tell it from a link's or a token's 401
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Login required", headers={_LOGIN_HEADER: "required"})
        if method not in ("GET", "HEAD") and not _same_origin(request):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Cross-site request refused")
        role = settings.user_role(name)
        if role == WORKSPACE_ROLE:
            # its role is decided for each request (in_workspaces)
            return Principal(name, "user", workspaces=settings.memberships(name), scoped=True)
        return Principal(name, role)

    def in_workspaces(request: HTTPConnection, user: Principal) -> Principal:
        """A user of workspaces only reaches the routes of WORKSPACE_ROUTES,
        and of them only what is in its workspaces, with its role there; any
        other route is refused (a route added later is closed until it is
        listed there)."""
        route = request.scope.get("route")
        path = getattr(route, "path", None)
        params = request.scope.get("path_params") or {}
        allowed = path in WORKSPACE_ROUTES or (
            # a plugin's own routes, of an instance only
            path is not None and path.startswith("/x/") and "server" in params and "role" in params)
        if not allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Not open to the users of workspaces")
        if "server" in params:
            names = [str(params["server"])]
        elif "job_id" in params:
            job = jobs.get(str(params["job_id"]))
            if job is not None and job.workspace:
                # an instance created or removed: its server is not the workspace's before or after
                role = user.workspace_role(job.workspace)
                if role is None:
                    raise HTTPException(status.HTTP_403_FORBIDDEN, "Not a workspace of yours")
                return dataclasses.replace(user, role=role)
            # a job of none of its servers is refused like one that does not exist
            names = job.servers if job is not None and job.servers else [""]
        elif "workspace" in params:
            role = user.workspace_role(str(params["workspace"]))
            if role is None:
                raise HTTPException(status.HTTP_403_FORBIDDEN, "Not a workspace of yours")
            return dataclasses.replace(user, role=role)
        else:
            # a list (Dashboard, jobs, account): it shows only its workspaces' servers
            return user
        roles = []
        for name in names:
            workspace = settings.workspace_of(name)
            role = user.workspace_role(workspace) if workspace else None
            if role is None:
                raise HTTPException(status.HTTP_403_FORBIDDEN, "Not a server of your workspaces")
            roles.append(role)
        # a job of several servers: the lowest of its roles there
        return dataclasses.replace(user, role=min(roles, key=ACCESS_ROLES.index))

    def visible_servers(user: Principal | None) -> set[str] | None:
        """The servers user may see: None, all of them (a user of the machine)."""
        if user is None or not user.scoped:
            return None
        mine = {ws for ws, _ in user.workspaces}
        return {srv for ws in settings.workspaces.values() if ws.name in mine for srv in ws.servers}

    def visible_jobs(user: Principal | None, found: list) -> list:
        """Of found, the jobs user may see: for a user of workspaces, those of
        its workspaces and those whose every server is in its workspaces."""
        shown = visible_servers(user)
        if shown is None:
            return found
        mine = {ws for ws, _ in user.workspaces}
        return [j for j in found if (j.workspace and j.workspace in mine) or (j.servers and set(j.servers) <= shown)]

    def current_user(request: HTTPConnection) -> Principal:
        ip = request.client.host if request.client else ""
        try:
            user = identify(request)
        except HTTPException as exc:
            # a wrong token; a missing session is no failure (the login form counts those)
            if exc.status_code == 401 and "bearer" in (exc.headers or {}).get("WWW-Authenticate", "").lower():
                method = request.scope.get("method", "WEBSOCKET")
                access_log.record("failed", "token", "", ip, f"{method} {request.url.path}"[:200])
            if isinstance(request, WebSocket):
                # refused before the handshake: the browser sees 403
                raise WebSocketException(1008, exc.detail) from None
            raise
        if user.kind != "user":
            # a link (with who acts there through it) or a token: a login is
            # its first request after a pause; a user's login is the form's
            access_log.seen(user.kind, user.name, ip, user.on_behalf)
        if user.scoped:
            try:
                user = in_workspaces(request, user)
            except HTTPException as exc:
                if isinstance(request, WebSocket):
                    raise WebSocketException(1008, exc.detail) from None
                raise
        request.state.user = user
        return user

    def needs(role: str):
        """A dependency: the caller, refused unless its role reaches role."""
        def check(user: Principal = Depends(current_user)) -> Principal:
            if not user.can(role):
                raise HTTPException(status.HTTP_403_FORBIDDEN, f"The {role} role is required")
            return user
        return check

    anyone, developer, admin = needs("user"), needs("developer"), needs("admin")

    if importlib.util.find_spec("mcp") is None:
        # the mcp SDK needs Python 3.10: no /mcp without it
        mcp_route, mcp_manager = None, None
    else:
        from .mcp_server import create_mcp

        # add_draft_server is defined further down, with the configuration's lock
        async def mcp_link_call(name: str, user: str, role: str, payload: dict) -> dict:
            """An MCP call to a linked OmniaCLI, through its link."""
            link = settings.link_targets.get(name)
            if link is None:
                raise LookupError(f"No link {name}: see list_links")
            try:
                resp = await link_clients.client(link).post(
                    "/mcp", json=payload, timeout=LINK_MCP_TIMEOUT,
                    headers={**link_headers(link, user, role), "Accept": "application/json, text/event-stream"})
            except httpx.HTTPError as exc:
                raise LookupError(f"{name}: {describe_error(exc)}") from exc
            if resp.status_code != 200:
                raise LookupError(f"{name} answered {resp.status_code} on /mcp (an OmniaCLI older than "
                                  "2026.1.5431 has no MCP through links)")
            try:
                return resp.json()
            except ValueError:
                raise LookupError(f"{name}: its /mcp did not answer JSON") from None

        mcp_route, mcp_manager = create_mcp(settings, jobs, updater, sampler, authenticator, access_log,
                                            lambda name, data: add_draft_server(name, data), mcp_link_call,
                                            lambda key: monitor.view(key))

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        own_log = selflog.attach(own_log_file)
        _logger.warning("this machine is a %s: shells of OmniaCLI's own user %s", settings.machine,
                        "open" if settings.machine == MACHINE_DEVELOPMENT else "closed")
        for ws in settings.workspaces.values():
            if settings.storage_problem(ws):
                _logger.warning("workspace %s cannot be used: %s", ws.name, settings.storage_problem(ws))
        updater.start()
        scheduler.start()
        sampler.start()
        monitor.start()
        await asyncio.to_thread(save_login_hosts)
        pitr_tasks.append(asyncio.get_running_loop().create_task(pitr_loop()))
        disk_recorder.start()
        tunnels.start()
        ssh_tunnels.sync(settings.link_targets)
        pullers.start()
        publications.start()
        consumers.start()
        # not nullcontext(): it is an async context manager only from Python 3.10
        if mcp_manager:
            async with mcp_manager.run():
                yield
        else:
            yield
        # each part stops within STOP_SECONDS: one whose task does not end
        # (a child process that does not die) is said, and the others stop
        for what, stop in (("consumers", consumers.stop), ("publications", publications.stop),
                           ("pullers", pullers.stop), ("tunnels", tunnels.stop), ("ssh tunnels", ssh_tunnels.close),
                           ("disk history", disk_recorder.stop), ("load sampler", sampler.stop),
                           ("monitor", monitor.stop), ("point-in-time recovery", stop_pitr),
                           ("auto update", updater.stop), ("schedules", scheduler.stop),
                           ("link clients", link_clients.close)):
            # a task of its own, waited for and never cancelled from here: a
            # timeout cancelling this task (the lifespan's) would reach the
            # server running it (anyio's in the tests) as a cancellation
            task = asyncio.ensure_future(stop())
            done, _ = await asyncio.wait({task}, timeout=STOP_SECONDS)
            if not done:
                _logger.error("%s did not stop within %d s: left behind", what, STOP_SECONDS)
            elif task.exception() is not None:
                _logger.error("%s stopped with an error: %s", what, task.exception())
        selflog.detach(own_log)

    app = FastAPI(
        title="OmniaCLI",
        lifespan=lifespan,
        dependencies=[Depends(current_user)],
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings, app.state.jobs, app.state.updater = settings, jobs, updater
    app.state.sampler = sampler
    app.state.disk_history, app.state.disk_recorder = disk_history, disk_recorder
    app.state.access_log = access_log
    app.state.tunnels = tunnels
    app.state.ssh_tunnels = ssh_tunnels
    app.state.pullers, app.state.publications, app.state.consumers = pullers, publications, consumers
    app.state.sessions, app.state.authenticator = sessions, authenticator

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        # no page of OmniaCLI inside another site's frame (clickjacking)
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if request.url.scheme == "https":
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return response

    @app.middleware("http")
    async def audit_actions(request: Request, call_next):
        """Every request that changes something, by whoever passed the login,
        in the access log: a route added later is logged by itself."""
        if request.method in ("GET", "HEAD") or _NOT_AUDITED.fullmatch(request.url.path):
            return await call_next(request)
        target = await _audit_target(request)
        response = await call_next(request)
        user = getattr(request.state, "user", None)
        if user is not None:
            who = f" for {user.on_behalf}" if user.on_behalf else ""
            access_log.record("action", user.kind, user.name, request.client.host if request.client else "",
                              f"{request.method} {request.url.path}{target} -> {response.status_code}{who}"[:300])
        return response

    @app.exception_handler(StarletteHTTPException)
    async def to_login(request: Request, exc: StarletteHTTPException):
        """A browser without a session goes to the login form: a page it opens
        comes back after it; a form it sends (a button: Stop, Start, ...) is not
        sent again, the login goes back to the page the form was on."""
        if (exc.status_code == 401 and (exc.headers or {}).get(_LOGIN_HEADER)
                and "text/html" in request.headers.get("accept", "")
                and not request.headers.get("authorization")):
            if request.method == "GET":
                target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            else:
                target = _referer_path(request)
            return RedirectResponse(f"/login?next={quote(target)}", status.HTTP_303_SEE_OTHER)
        return await http_exception_handler(request, exc)

    async def public_file(request: Request):
        name, media_type = _PUBLIC_FILES[request.url.path]
        return FileResponse(_STATIC / name, media_type=media_type,
                            headers={"Cache-Control": "public, max-age=86400"})

    def login_page(request: Request, next_path: str, error: str = "", code: int = 200):
        return _TEMPLATES.TemplateResponse(request, "login.html", {"next": next_path, "error": error},
                                           status_code=code)

    async def login(request: Request):
        """The login form (GET) and its check (POST): a session cookie of
        LIFETIME, failed tries limited per address and user name."""
        if request.method == "GET":
            next_path = _next_path(request.query_params.get("next", "/"))
            if sessions.user(request.cookies.get(COOKIE), settings.users):
                return RedirectResponse(next_path, status.HTTP_303_SEE_OTHER)
            return login_page(request, next_path)
        if not _same_origin(request):
            return HTMLResponse("Cross-site login refused", status_code=403)
        form = parse_qs((await request.body()).decode("utf-8", "replace"))
        name = (form.get("username") or [""])[0].strip()[:64]
        password = (form.get("password") or [""])[0]
        next_path = _next_path((form.get("next") or ["/"])[0])
        ip = request.client.host if request.client else ""
        wait = limiter.wait(ip, name)
        if wait:
            access_log.record("failed", "user", name, ip, f"too many failed logins: wait {wait} s")
            return login_page(request, next_path, f"Too many failed logins: try again in {wait} seconds.", 429)
        if not name or not await asyncio.to_thread(authenticator.check, name, password):
            limiter.failed(ip, name)
            access_log.record("failed", "user", name, ip, "login form")
            return login_page(request, next_path, "Wrong user name or password.", 401)
        limiter.succeeded(ip, name)
        access_log.record("login", "user", name, ip, "login form")
        response = RedirectResponse(next_path, status.HTTP_303_SEE_OTHER)
        set_session_cookie(request, response, sessions.create(name, settings.users[name]))
        return response

    def set_session_cookie(request: Request, response, token: str) -> None:
        response.set_cookie(COOKIE, token, max_age=LIFETIME, httponly=True, samesite="strict",
                            secure=request.url.scheme == "https", path="/")

    # Starlette routes: the app's login dependency does not apply to them
    for path in _PUBLIC_FILES:
        app.add_route(path, public_file, methods=["GET"])
    app.add_route("/login", login, methods=["GET", "POST"])

    async def login_as_landing(request: Request):
        """/.omniacli/login?t=<token>, opened on the instance's own host (its
        nginx passes the path here): Odoo's session cookie set on that host,
        then Odoo's home. A token is used once, on the host it was made for."""
        host = request.headers.get("host", "")
        # marked: OmniaCLI asks its own path on a name to know nginx passes it here
        headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", loginas.MARK: "1"}
        if request.query_params.get("probe") == "1":
            return Response(status_code=status.HTTP_204_NO_CONTENT, headers=headers)
        ticket = login_tickets.take(request.query_params.get("t", ""), host)
        ip = request.client.host if request.client else ""
        if ticket is None:
            access_log.record("failed", "user", "", ip, f"log in as: no valid link on {host}"[:200])
            return HTMLResponse("This link to log in has been used, has expired or belongs to another address: "
                                "ask OmniaCLI for a new one.", status_code=403, headers=headers)
        access_log.record("login-as", "user", ticket.who, ip, f"opened: {ticket.detail}"[:300])
        response = RedirectResponse(ticket.home, status.HTTP_303_SEE_OTHER, headers=headers)
        response.set_cookie(loginas.ODOO_COOKIE, ticket.sid, max_age=loginas.COOKIE_SECONDS, httponly=True,
                            samesite="lax", secure=ticket.home.startswith("https://"), path="/")
        return response

    app.add_route(loginas.LOGIN_PATH, login_as_landing, methods=["GET"])

    async def choose_language(request: Request):
        """/language/<code>?next=<page>: the language of the pages in this
        browser (a cookie of a year), also before the login."""
        code = request.path_params["code"]
        response = RedirectResponse(_next_path(request.query_params.get("next", "/")), status.HTTP_303_SEE_OTHER)
        if code in i18n.LANGUAGES:
            response.set_cookie(i18n.COOKIE, code, max_age=365 * 24 * 3600, httponly=True, samesite="lax",
                                secure=request.url.scheme == "https", path="/")
        return response

    app.add_route("/language/{code}", choose_language, methods=["GET"])

    @app.middleware("http")
    async def translate_errors(request: Request, call_next):
        """The error of a JSON answer (refused, 4xx, 5xx: {"error"} or
        {"detail"}) in the caller's language; the code says it in English."""
        response = await call_next(request)
        lang = request_language(request)
        if (lang == i18n.DEFAULT or response.status_code < 400
                or "application/json" not in response.headers.get("content-type", "")):
            return response
        body = b"".join([chunk async for chunk in response.body_iterator])
        try:
            data = json.loads(body)
        except ValueError:
            data = None
        if isinstance(data, dict):
            for key in ("error", "detail"):
                if isinstance(data.get(key), str):
                    data[key] = i18n.tr(lang, data[key])
            body = json.dumps(data).encode()
        headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
        return Response(body, status_code=response.status_code, headers=headers, media_type="application/json")

    @app.post("/logout")
    async def logout(request: Request, me: Principal = Depends(current_user)):
        sessions.drop(request.cookies.get(COOKIE))
        access_log.record("logout", me.kind, me.name, request.client.host if request.client else "")
        response = RedirectResponse("/login", status.HTTP_303_SEE_OTHER)
        response.delete_cookie(COOKIE, path="/")
        return response
    if mcp_route is not None:
        app.router.routes.append(mcp_route)  # its own bearer token check

    def get_instance(server: str, role: str, drafts: bool = False) -> Instance:
        instance = settings.instance(server, role, drafts)
        if instance is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No instance {server}/{role}")
        return instance

    def started(request: Request, job, query: str = "") -> RedirectResponse:
        return RedirectResponse(f"{prefix_of(request)}/jobs/{job.id}{query}", status.HTTP_303_SEE_OTHER)

    async def started_or_back(request: Request, job) -> RedirectResponse:
        """Start, stop and restart from a button: back to the page of the
        button (its form's next) instead of the job's page; that page shows
        the job running and its end."""
        form = parse_qs((await request.body()).decode("utf-8", "replace"))
        back = (form.get("next") or [""])[0]
        if back:
            return RedirectResponse(_next_path(back), status.HTTP_303_SEE_OTHER)
        return started(request, job)

    def prefix_of(request: HTTPConnection) -> str:
        return getattr(request.state, "prefix", "")

    def active_server(name: str):
        srv = settings.servers.get(name)
        if srv is None or not srv.active:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No active server {name}")
        return srv

    async def run_state(inst: Instance) -> str:
        """running, stopped or unknown (an init.d service, a unit not found)."""
        if inst.restart.type == "process":
            return "running" if ops.managed_pid(inst) else "stopped"
        state = await ops.service_state(inst)
        if state in ("active", "activating", "reloading"):
            return "running"
        if state in ("inactive", "failed", "deactivating"):
            return "stopped"
        return "unknown"

    # monitoring with notices (monitor.py): started with the app
    monitor = Monitor(settings, jobs, run_state, settings.servers_file.parent / MONITOR_FILE)

    async def run_states(servers) -> dict[str, str]:
        """{instance key: run_state} of the servers' instances, asked together."""
        insts = [i for srv in servers for i in srv.instances.values()]
        return dict(zip([i.key for i in insts], await asyncio.gather(*(run_state(i) for i in insts))))

    async def remote_servers(me: Principal) -> list[dict]:
        """The servers of every link, as Dashboard rows: an error row for a
        link that does not answer. Only for a user who logs in here."""
        if me.kind != "user":
            return []

        async def one(link: LinkTarget) -> list[dict]:
            try:
                resp = await link_clients.client(link).get(
                    "/api/servers", headers=link_headers(link, me.name, me.role), timeout=LIST_TIMEOUT)
                if resp.status_code != 200:
                    return [{"link": link.name, "error": f"answered {resp.status_code}"}]
                found = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                return [{"link": link.name, "error": describe_error(exc)}]
            rows = []
            for srv in found:
                instances = srv.get("instances") or {}
                text = [link.name, srv["name"]]
                for inst in instances.values():
                    text += [inst.get("database", ""), inst.get("service", "")]
                    text += [f"{r['name']} {r['branch']}" for r in inst.get("repositories", [])]
                rows.append({
                    "link": link.name, "name": srv["name"], "label": f"{link.name} › {srv['name']}",
                    "base": f"/links/{link.name}",
                    "roles": [r for r in ("production", "test") if r in instances],
                    "busy": {r: bool(i.get("busy")) for r, i in instances.items()},
                    # a linked OmniaCLI older than the state: no buttons
                    "state": {r: i["state"] for r, i in instances.items() if i.get("state")},
                    "restart": {r: i.get("restart", "") for r, i in instances.items()},
                    # its domain; an older OmniaCLI, or none set: localhost at its port
                    "web": {r: i.get("url") or f"http://localhost:{i['port']}"
                            for r, i in instances.items() if i.get("url") or i.get("port")},
                    "text": " ".join(text),
                })
            return rows

        found = await asyncio.gather(*(one(link) for link in settings.link_targets.values()))
        return [row for rows in found for row in rows]

    async def server_sections(request: Request, q: str, me: Principal) -> dict:
        """The Dashboard's server table: this instance's live servers (a
        linked instance's are on its own Dashboard, opened from Linked),
        arranged by the groups of the servers list (listgroups.py); the list
        widget searches them (_list.html), q fills its search. A user of
        workspaces sees its workspaces' servers only, with no groups."""
        shown = visible_servers(me)
        live = [srv for srv in settings.active_servers() if shown is None or srv.name in shown]
        states = await run_states(live)
        rows = [
            {
                "link": "", "name": srv.name, "label": srv.name, "base": prefix_of(request),
                "workspace": settings.workspace_of(srv.name) or "",
                "roles": [r for r in ("production", "test") if r in srv.instances],
                "busy": {r: jobs.is_busy(i.key) for r, i in srv.instances.items()},
                "state": {r: states[i.key] for r, i in srv.instances.items()},
                "restart": {r: i.restart.type for r, i in srv.instances.items()},
                "web": {r: url for r, i in srv.instances.items() if (url := i.web_url)},
                "text": server_text(srv),
            }
            for srv in live
        ]
        rows = nest_stagings(rows)
        groups = settings.groups.get("servers", []) if shown is None else []
        for row in rows:
            # the search finds a server by its workspace and its groups too;
            # a staging is in its parent's groups, under it
            row["text"] += " " + " ".join([row["workspace"], *group_names(groups, row["parent"] or row["name"])])
        return {"rows": rows, "sections": listgroups.sections(rows, groups, lambda r: r["parent"] or r["name"]),
                "total": len(rows), "q": q}

    def nest_stagings(rows: list[dict]) -> list[dict]:
        """rows with each staging right under its parent, the newest first, as
        a hosting panel's ↳ children: "parent" and "created" on every row (empty
        for one that is no staging, or whose parent is not shown)."""
        known = stagings.load(stagings_file)
        names = {row["name"] for row in rows}
        children: dict[str, list[dict]] = {}
        for row in rows:
            info = known.get(row["name"]) or {}
            parent = info.get("parent", "")
            row["parent"] = parent if parent in names and parent != row["name"] else ""
            row["created"] = str(info.get("created", ""))[:16].replace("T", " ")
            if row["parent"]:
                row["text"] += " " + row["parent"] + " staging"
                children.setdefault(row["parent"], []).append(row)
        out = []
        for row in rows:
            if not row["parent"]:
                out.append(row)
                out += sorted(children.get(row["name"], []), key=lambda r: r["created"], reverse=True)
        return out

    def group_names(groups: list, key: str) -> list[str]:
        """The names of the groups that hold key, from the top down."""
        for group in groups:
            if group.contains(key):
                return [group.name, *group_names(group.groups, key)]
        return []

    @app.get("/")
    async def index(request: Request, q: str = "", me: Principal = Depends(current_user)):
        """The Dashboard: this instance's servers, its workspaces (for its
        admins), the machine of each link (Linked), this machine, the jobs."""
        shown = visible_servers(me)
        context = await server_sections(request, q, me)
        links = sorted(settings.link_targets) if me.kind == "user" and shown is None else []
        linked = [{"name": n, "url": settings.link_targets[n].url} for n in links]
        context.update({
            # the load and disks of this machine: not for a user of
            # workspaces, who sees its own servers only
            "machine": shown is None,
            "machine_links": links,
            "linked_sections": listgroups.sections(linked, settings.groups.get("links", []), lambda r: r["name"]),
            # New instance (a version only): the machine's admins, workspaces allowed here
            "quick_instance": shown is None and me.can("admin") and bool(settings.storage_roots)
            and workspaceops.docker_missing() is None,
            "jobs": visible_jobs(me, jobs.recent(len(jobs.jobs)))[:10],
            # a user of workspaces: the load of each of its workspaces instead
            "load_workspaces": sorted((ws, role) for ws, role in me.workspaces if ws in settings.workspaces) if me.scoped else [],
        })
        return _TEMPLATES.TemplateResponse(request, "index.html", context)

    @app.get("/dashboard/servers")
    async def dashboard_servers(request: Request, q: str = "", me: Principal = Depends(current_user)):
        """The Dashboard's server table again (a job started or ended)."""
        context = await server_sections(request, q, me)
        return _TEMPLATES.TemplateResponse(request, "_servers_part.html", context)

    @app.get("/servers/{server}")
    async def server_page(request: Request, server: str, view: str = "production"):
        """An instance's page (UX-003, UX-004 of docs/ux): one copy at a time,
        its production by default; its test at ?view=test; a staging shows
        its production only (its test is stopped, it is not offered)."""
        srv = active_server(server)
        staging_of = stagings.load(stagings_file).get(srv.name)
        if view == "both" or (staging_of and view == "test" and srv.production is not None):
            view = "production"
        if view not in srv.instances:
            if view == "production" and srv.instances:
                view = next(iter(srv.instances))
            else:
                raise HTTPException(status.HTTP_404_NOT_FOUND, f"No instance {server}/{view}")
        roles = [view]
        shown = [srv.instances[r] for r in roles]
        me = request.state.user
        base = f"{prefix_of(request)}/servers/{srv.name}"
        buttons = {i.key: action_buttons(i, i.plugin, "instance", me, f"{base}/{i.role}") for i in shown}
        plugin = plugins.registry().get(srv.type)
        server_buttons = action_buttons(srv, plugin, "server", me, base)
        # each dialog of the plugin's own, drawn once
        dialogs = sorted({f"{b['plugin']}/{b['action'].dialog}"
                          for groups in [*buttons.values(), server_buttons]
                          for group in groups.values() for b in group if b["action"].dialog})
        statuses = await asyncio.gather(
            *(asyncio.gather(*(ops.repository_status(r) for r in i.repositories)) for i in shown)
        )

        async def remote_url(repo) -> str:
            code, out = await ops.capture(repo.git("remote", "get-url", repo.remote), cwd=repo.path, timeout=10)
            return out if code == 0 else ""
        # the Odoo config tab of a workspace's instance: its template's parameters
        made_by = workspace_of(srv.name)
        param_role = "test" if view == "test" else "production"
        odoo_params = (await asyncio.to_thread(role_parameters, made_by[0], made_by[1], param_role)
                       if made_by is not None and getattr(srv, param_role) is not None else None)
        # the Addons tab: where each repository comes from
        repo_urls = {r.name: url for i in shown for r, url in
                     zip(i.repositories, await asyncio.gather(*(remote_url(r) for r in i.repositories)))}
        # Copy from link: for a user who logs in here, with the role it needs;
        # shown also without a usable link, disabled, saying how to get one
        from_link = {}
        for i in shown:
            # not for a user of workspaces: the links are the machine's
            if not (i.plugin.has_restore and me.kind == "user" and not me.scoped and me.can(from_link_role(i))):
                continue
            src = i.from_link
            if src is None:
                missing = ("No database from a link: "
                           + ("set 'Database from link' in the server's form (Edit)" if settings.link_targets
                              else "add a link on the Links page, then set 'Database from link' "
                                   "in the server's form (Edit)"))
                from_link[i.key] = {"disabled": missing}
                continue
            known = src.link in (settings.connections if src.is_puller else settings.link_targets)
            from_link[i.key] = {
                "url": f"{base}/{i.role}/from-link", "link": src.link, "server": src.server,
                "role": src.role, "label": src.label, "puller": src.is_puller, "known": known,
                "database": str(i.plugin.describe(i).get("database") or i.key) if i.role == "production" else ""}
            if src.is_puller and not known:
                from_link[i.key]["disabled"] = (f"No puller connection {src.link} here: add it on the Pullers "
                                                "page, or change 'Database from link' in the server's form (Edit)")
            elif not known:
                from_link[i.key]["disabled"] = (f"No link {src.link} here: add it on the Links page, "
                                                "or change 'Database from link' in the server's form (Edit)")
        # Restore: for an admin who logs in here
        restore_file = {}
        for i in shown:
            if i.plugin.has_restore and me.kind == "user" and not me.scoped and me.can("admin"):
                restore_file[i.key] = {
                    "url": f"{base}/{i.role}/restore",
                    "upload": f"{prefix_of(request)}/api/servers/{srv.name}/{i.role}/backups/upload",
                    "backups": f"{prefix_of(request)}/api/servers/{srv.name}/{i.role}/backups",
                    "database": str(i.plugin.describe(i).get("database") or i.key) if i.role == "production" else ""}
        # Log in as: a user who logs in here, with the role it needs
        login_as_buttons = {}
        for i in shown:
            if i.plugin.has_login and acts_for_a_person(me) and me.can(from_link_role(i)):
                api = f"{prefix_of(request)}/api/servers/{srv.name}/{i.role}/login-as"
                login_as_buttons[i.key] = {"users": f"{api}/users", "url": api, "key": i.key}
        # Git pull: with more than one thing to pull, the dialog chooses which
        pull_choices = {}
        for i in shown:
            rows = [{"name": r.name, "label": r.name} for r in i.repositories]
            if i.package is not None:
                rows.append({"name": ops.PACKAGE_CHOICE, "label": f"package {i.package.name}"})
            if len(rows) > 1:
                pull_choices[i.key] = {
                    "key": i.key, "rows": rows,
                    "preview": f"{prefix_of(request)}/api/servers/{srv.name}/{i.role}/pull-preview"}
        return _TEMPLATES.TemplateResponse(
            request,
            "server.html",
            {
                "from_link": from_link,
                "login_as": login_as_buttons,
                # its copies: its test and its stagings (made beside it in its
                # workspace); or the production it is a staging of
                "stagings": {"rows": await staging_states(staging_rows(srv.name)),
                             "can": request.state.user.can("admin") and workspace_of(srv.name) is not None
                             and srv.production is not None and not staging_of,
                             # what it is a copy of, at the address the page names it by
                             "parent": staging_of and {**staging_of, "url": getattr(
                                 (settings.servers.get(staging_of.get("parent")) or srv).production, "web_url", None)},
                             "workspace": (workspace_of(srv.name) or (None,))[0]},
                "is_admin_here": request.state.user.can("admin"),
                # a staging whose mail is caught: its Mail tab (UX-008 §5.3)
                "mail_capture": bool(staging_of) and await asyncio.to_thread(staging_mail, srv.name) is not None,
                # the paths and the commands of an instance, for the machine's admins (ssh)
                "shell_paths": {i.key: i.plugin.shell_paths(i) for i in shown if i.plugin is not None}
                if request.state.user.can("admin") and not request.state.user.workspaces else {},
                "test_state": await run_state(srv.test) if srv.test is not None and view != "test" else None,
                "pull_choices": pull_choices,
                "restore_file": restore_file,
                "restore_fields": [_field_dict(f) for f in plugin.restore_fields()] if plugin else [],
                "backup_fields": [_field_dict(f) for f in plugin.backup_fields()] if plugin else [],
                "server": srv,
                "view": view,
                "instances": shown,
                "buttons": buttons,
                "server_buttons": server_buttons,
                "dialogs": dialogs,
                "core_groups": core_groups,
                # the plugin's settings of each instance, as its fields name them
                "settings_rows": {i.key: settings_rows(i) for i in shown},
                "repo_status": {i.key: s for i, s in zip(shown, statuses)},
                "repo_urls": repo_urls,
                "odoo_params": odoo_params, "param_role": param_role,
                "package_state": {i.key: packages.installed_state(i.package) for i in shown if i.package},
                "auto_state": updater.state,
                "busy": {i.key: jobs.is_busy(i.key) for i in srv.instances.values()},
                "busy_job": {i.key: jobs.running_on(i.key) for i in shown},
                # Odoo started by OmniaCLI: its pid, None when stopped
                "running": {i.key: ops.managed_pid(i) for i in shown if i.restart.type == "process"},
                # a systemd service: its ActiveState, None when unknown
                "service_state": dict(zip([i.key for i in shown],
                                          await asyncio.gather(*(ops.service_state(i) for i in shown)))),
                "jobs": [j for j in jobs.recent(200) if any(k.startswith(f"{server}/") for k in j.instances)][:5],
            },
        )

    def settings_rows(inst: Instance) -> list[tuple]:
        """(label, value, kind) of the plugin's fields that apply to the instance."""
        values = inst.plugin.options_to_dict(inst.options)
        return [(f.label, values.get(f.key), f.kind) for f in inst.plugin.fields()
                if not f.restart_types or inst.restart.type in f.restart_types]

    @app.get("/api/info")
    async def info(user: Principal = Depends(current_user)):
        """What a linking instance checks: this version and the caller's role."""
        return {"app": "OmniaCLI", "version": _version(), "caller": str(user), "role": user.role}

    @app.get("/api/servers")
    async def server_list(request: Request):
        """The live servers, with what the Dashboard shows and filters on."""
        shown = visible_servers(request.state.user)
        live = [srv for srv in settings.active_servers() if shown is None or srv.name in shown]
        states = await run_states(live)
        known = stagings.load(stagings_file)
        return [
            {
                "name": srv.name,
                "type": srv.type,
                "instances": {
                    role: {
                        # the plugin's fields (Odoo: database)
                        **inst.plugin.describe(inst),
                        "service": inst.restart.service,
                        "repositories": [{"name": r.name, "branch": r.branch} for r in inst.repositories],
                        "busy": jobs.is_busy(inst.key),
                        "state": states[inst.key],
                        "restart": inst.restart.type,
                        "port": inst.plugin.web_port(inst),
                        # its address: its domain, else its workspace's name for it; None: none
                        "url": inst.domain or inst.site,
                    }
                    for role, inst in srv.instances.items()
                },
                # a staging: the production it is a copy of, and when (stagings.py)
                "parent": known.get(srv.name, {}).get("parent", ""),
                "created": known.get(srv.name, {}).get("created", ""),
                # the Instances of every machine (UX-008 §4.1): its Odoo, and
                # whether its production backs up by itself (none for a staging)
                "version": odoo_version(srv.name), "workspace": settings.workspace_of(srv.name) or "",
                "backup_schedule": backup_schedule_state(srv.production, request_language(request))
                if srv.production is not None and srv.name not in known else None,
            }
            for srv in live
        ]

    @app.get("/api/metrics")
    async def metrics(request: Request, since: int = 0, server: Optional[str] = None, machine_only: bool = False):
        """{cores, interval, machine: [points], instances: {key: {source, points}}};
        machine_only: no instances (the Dashboard's machine charts). A user
        of workspaces gets its instances only, and not the machine's load."""
        shown = visible_servers(request.state.user)
        data = {}
        for inst in [] if machine_only else settings.all_instances():
            if (server is None or inst.server == server) and (shown is None or inst.server in shown):
                data[inst.key] = {
                    "source": i18n.tr(request_language(request), sampler.source.get(inst.key, "")),
                    "points": sampler.series(inst.key, since),
                }
        return {"cores": sampler.cores, "interval": 5,
                "machine": sampler.machine_series(since) if shown is None else [], "instances": data}

    @app.get("/api/disks")
    async def disk_space():
        """This machine's disks: size, free space, what is kept on each, and
        when each will be full (diskhistory.forecast)."""
        found = await disks.disks(settings)
        for disk in found:
            disk["forecast"] = disk_history.forecast_of(disk["mount"])
        return {"disks": found}

    @app.get("/api/disks/history")
    async def disk_history_json():
        """Every disk's daily points (up to three years) and its forecast,
        and the workspaces' disks the same way."""
        return {"disks": disk_history.series(), "workspaces": disk_history.workspace_series(settings.workspaces)}

    @app.get("/disks")
    async def disks_page(request: Request):
        """The history of this machine's disks, and when each will be full."""
        return _TEMPLATES.TemplateResponse(request, "disks.html", {})

    async def pull_choice(request: Request, instance: Instance) -> set[str] | None:
        """The repositories chosen in the pull dialog (fields repo, with
        chosen=1); None: all of them and the package (Pull all, a post with
        no body)."""
        form = parse_qs((await request.body()).decode("utf-8", "replace"))
        if (form.get("chosen") or [""])[-1] != "1":
            return None
        known = {r.name for r in instance.repositories} | (
            {ops.PACKAGE_CHOICE} if instance.package is not None else set())
        only = set(form.get("repo") or [])
        unknown = sorted(only - known)
        if unknown:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                f"{instance.key} has no repository {', '.join(unknown)}")
        if not only:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Choose at least one repository")
        return only

    def pull_title(what: str, instance: Instance, only: set[str] | None) -> str:
        if only is None:
            return f"{what} {instance.key}"
        names = [r.name for r in instance.repositories if r.name in only]
        if ops.PACKAGE_CHOICE in only:
            names.append(f"package {instance.package.name}")
        return f"{what} {instance.key} ({', '.join(names)})"

    @app.get("/api/servers/{server}/{role}/database")
    async def database_report(server: str, role: str, me: Principal = Depends(developer)):
        """The health of the instance's database (pgreport.py)."""
        instance = get_instance(server, role)
        try:
            found = await pgreport.report(instance)
        except JobFailed as exc:
            return refuse(str(exc), status.HTTP_502_BAD_GATEWAY)
        return {**found, "can_stop": me.can(backup_role(instance))}

    @app.post("/api/servers/{server}/{role}/database/terminate")
    async def database_terminate(request: Request, server: str, role: str, payload: dict = Body(...),
                                 me: Principal = Depends(current_user)):
        """A query of the instance's database stopped (an admin's on a production)."""
        instance = get_instance(server, role)
        if not me.can(backup_role(instance)):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"The {backup_role(instance)} role is required")
        pid = str(payload.get("pid") or "")
        try:
            stopped = await pgreport.terminate(instance, pid)
        except JobFailed as exc:
            return refuse(str(exc))
        access_log.record("action", me.kind, me.name, request.client.host if request.client else "",
                          f"stop query {pid} of {instance.key}")
        return {"ok": True, "stopped": stopped}

    @app.get("/api/servers/{server}/{role}/uptime")
    async def instance_uptime(server: str, role: str, user: Principal = Depends(developer)):
        """The monitor's view of an instance: up, down or stopped, since when,
        its uptime over the last day, its last answer time."""
        return monitor.view(get_instance(server, role).key)

    @app.post("/api/workspaces/{workspace}/notify", dependencies=[Depends(admin)])
    async def workspace_notify(workspace: str, payload: dict = Body(...)):
        """Whom the monitor tells when an instance of the workspace goes down."""
        ws = a_workspace(workspace)
        try:
            notify_list = parse_notify(payload.get("notify"), "notify")
        except ConfigError as exc:
            return refuse(str(exc))
        async with workspaces_lock:
            current = a_workspace(workspace)
            refused = await store_workspaces({**settings.workspaces,
                                              ws.name: dataclasses.replace(current, notify=notify_list)})
            if refused:
                return refused
        return {"ok": True, "notify": notify_list}

    @app.get("/api/servers/{server}/{role}/summary")
    async def instance_summary(server: str, role: str, user: Principal = Depends(developer)):
        """The head of the Environments tab: the Odoo running and whether a
        newer one is there (its repository's commits, or its image), the
        workers, each repository with its commit and the commits not yet
        pulled, with the page of the differences."""
        instance = get_instance(server, role)
        found, repos = await asyncio.gather(instance.plugin.summary(instance), ops.repository_states(instance))
        for row in repos:
            row["compare"] = workspaceops.compare_url(row["url"], row["commit"], row["new"]) \
                if row["url"] and row["commit"] and row["new"] and row["pending"] else None
            row["url"] = workspaceops.web_url(row["url"]) if row["url"] else None
            row["short"] = (row["commit"] or "")[:10]
        return {**found, "repositories": repos}

    @app.get("/api/servers/{server}/{role}/pull-preview")
    async def pull_preview(server: str, role: str, user: Principal = Depends(developer)):
        """The pull dialog's list: each repository with the commits not yet
        pulled (a git fetch), and the package."""
        return {"rows": await ops.pull_preview(get_instance(server, role))}

    @app.post("/servers/{server}/{role}/pull")
    async def action_pull(request: Request, server: str, role: str, user: Principal = Depends(developer)):
        instance = get_instance(server, role)
        only = await pull_choice(request, instance)
        return started(
            request,
            jobs.submit(
                pull_title("Git pull", instance, only),
                [instance.key],
                f"manual: {user}",
                lambda job: ops.pull(job, instance, only),
            )
        )

    @app.post("/servers/{server}/{role}/pull-restart")
    async def action_pull_restart(request: Request, server: str, role: str, user: Principal = Depends(developer)):
        instance = get_instance(server, role)
        only = await pull_choice(request, instance)
        return started(
            request,
            jobs.submit(
                pull_title("Git pull and restart", instance, only),
                [instance.key],
                f"manual: {user}",
                lambda job: ops.pull_restart(job, instance, only=only, backup_folder=backup_folder(instance)),
            )
        )

    @app.post("/servers/{server}/{role}/restart")
    async def action_restart(request: Request, server: str, role: str, user: Principal = Depends(anyone)):
        instance = get_instance(server, role)
        return await started_or_back(
            request,
            jobs.submit(
                f"Restart {instance.key}",
                [instance.key],
                f"manual: {user}",
                lambda job: ops.restart(job, instance),
            )
        )

    @app.post("/servers/{server}/{role}/start")
    async def action_start(request: Request, server: str, role: str, user: Principal = Depends(anyone)):
        instance = get_instance(server, role)
        if instance.restart.type not in ("systemd", "compose", "process"):
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                f"Start is for a systemd service, a compose project or an {instance.plugin.label} "
                                "OmniaCLI starts itself")
        return await started_or_back(
            request,
            jobs.submit(
                f"Start {instance.key}",
                [instance.key],
                f"manual: {user}",
                lambda job: ops.start(job, instance),
            )
        )

    @app.post("/servers/{server}/{role}/stop")
    async def action_stop(request: Request, server: str, role: str, user: Principal = Depends(anyone)):
        instance = get_instance(server, role)
        if instance.restart.type not in ("systemd", "compose", "process"):
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                f"Stop is for a systemd service, a compose project or an {instance.plugin.label} "
                                "OmniaCLI starts itself")
        return await started_or_back(
            request,
            jobs.submit(
                f"Stop {instance.key}",
                [instance.key],
                f"manual: {user}",
                lambda job: ops.stop(job, instance),
            )
        )

    @app.get("/api/servers/{server}/{role}/backup/choices")
    async def backup_choices(server: str, role: str, key: str = "", user: Principal = Depends(developer)):
        """What the backup dialog opens with: the instance's last choice
        (remembered) and, for key, the rows of that checklist field."""
        instance = get_instance(server, role)
        remembered = backupchoices.load(backup_folder(instance))
        out = {"remembered": {"values": remembered.get("values") or {}, "shown": remembered.get("shown") or {},
                              "user": remembered.get("user"), "saved": remembered.get("saved")}}
        if key:
            field = next((f for f in instance.plugin.backup_fields() if f.key == key and f.kind == "checklist"), None)
            if field is None:
                return refuse(f"{instance.plugin.label} has no list {key} in its backup")
            try:
                found = await instance.plugin.backup_choices(instance, key)
                out.update(rows=found["rows"], total=found.get("total"), total_label=found.get("total_label"))
            except JobFailed as exc:
                return refuse(str(exc))
        return out

    @app.get("/api/servers/{server}/{role}/backup/estimate")
    async def backup_estimate(server: str, role: str, user: Principal = Depends(developer)):
        """What a backup of the instance would hold, for the previews of the
        dialogs (plugin_api.backup_estimate), with the rows of its lists the
        instance's last backup dialog ticks (backupchoices) and that choice."""
        instance = get_instance(server, role)
        try:
            found = await instance.plugin.backup_estimate(instance)
        except JobFailed as exc:
            return refuse(str(exc))
        remembered = backupchoices.load(backup_folder(instance))
        fields = {f.key: f for f in instance.plugin.backup_fields()}
        ticked = {}
        for key, rows in (found.get("lists") or {}).items():
            field = fields.get(key)
            threshold = fields.get(field.threshold) if field is not None and field.threshold else None
            megabytes = (remembered.get("values") or {}).get(field.threshold) if threshold else None
            if megabytes in (None, "") and threshold is not None:
                megabytes = threshold.default
            ticked[key] = backupchoices.ticked(rows, remembered, key, megabytes)
        return {**found, "ticked": ticked,
                "remembered": {"values": remembered.get("values") or {}, "shown": remembered.get("shown") or {},
                               "user": remembered.get("user"), "saved": remembered.get("saved")}}

    @app.get("/api/servers/{server}/{role}/restore/room")
    async def restore_room(server: str, role: str, user: Principal = Depends(developer)):
        """The disks a restore into the instance writes to, with their room
        (restoreroom.py)."""
        instance = get_instance(server, role)
        try:
            found = await instance.plugin.restore_room(instance)
        except JobFailed as exc:
            return refuse(str(exc))
        paths = {**(found.get("paths") or {}), "backups": str(backup_folder(instance))}
        return await restoreroom.room(paths, found.get("freed") or {}, found.get("notes") or [])

    @app.post("/servers/{server}/{role}/backup")
    async def action_backup(
        request: Request, server: str, role: str, download: bool = False,
        user: Principal = Depends(developer),
    ):
        instance = get_instance(server, role)
        raw, _ = await posted_params(request)
        try:
            options = {f.key: _field_value(f, raw.get(f.key)) for f in instance.plugin.backup_fields()}
        except ActionRefused as exc:
            return refuse(str(exc))
        chosen = [f.label for f in instance.plugin.backup_fields() if f.kind == "checkbox" and options[f.key]]
        if raw.get("remember"):
            # the dialog's choice: it opens with it next time, and the backups
            # without a dialog take its ticks (backupchoices)
            # a list not posted (its checkbox off) keeps its last ticks
            before = backupchoices.load(backup_folder(instance))
            values, shown = dict(options), {}
            for f in instance.plugin.backup_fields():
                if f.kind != "checklist":
                    continue
                if values[f.key] is None:
                    values[f.key] = (before.get("values") or {}).get(f.key)
                    shown[f.key] = (before.get("shown") or {}).get(f.key) or []
                else:
                    shown[f.key] = [v for v in str(raw.get(f"{f.key}__shown") or "").split(",") if v]
            backupchoices.save(backup_folder(instance), values, shown, user.name)

        async def action(job):
            dest = await ops.backup(job, instance, settings.backup_dir, options)
            job.result = {
                "file": dest.name,
                "size": dest.stat().st_size,
                "download": f"/backups/{instance.server}/{instance.role}/{dest.name}",
            }

        job = jobs.submit(
            f"Backup {instance.key}" + (" + download" if download else "")
            + (f" ({', '.join(chosen).lower()})" if chosen else ""),
            [instance.key],
            f"manual: {user}",
            action,
        )
        # the job page starts the download when the backup is ready
        return started(request, job, "?download=1" if download else "")

    def backup_folder(instance: Instance) -> Path:
        return settings.backup_dir / instance.server / instance.role

    @app.get("/api/servers/{server}/{role}/backups")
    async def backup_list(request: Request, server: str, role: str, me: Principal = Depends(current_user)):
        instance = get_instance(server, role)
        can_delete = me.can(backup_role(instance))
        folder = backup_folder(instance)
        files = sorted(folder.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True) \
            if folder.is_dir() else []
        return [
            {
                "file": p.name,
                "size": p.stat().st_size,
                "time": int(p.stat().st_mtime),
                "download": f"{prefix_of(request)}/backups/{server}/{role}/{p.name}",
                # scheduled, before an update, made by hand (or brought from a link)
                "kind": ("scheduled" if p.name.startswith(schedules.SCHEDULED_PREFIX)
                         else "pre-update" if p.name.startswith(ops.PRE_UPDATE_PREFIX) else "manual"),
                "can_delete": can_delete,
                "archive": p.name.startswith(instancearchive.PREFIX)
                or p.name.startswith(schedules.SCHEDULED_PREFIX + instancearchive.PREFIX),
                # its SHA-256 written, the last restore tried (backupcheck)
                **backupcheck.state(folder, p.name),
                "can_verify": instance.plugin.has_verify and me.can("developer"),
            }
            for p in files
        ]

    @app.post("/api/servers/{server}/{role}/backups/verify")
    async def backup_verify(server: str, role: str, payload: dict = Body(...), me: Principal = Depends(developer)):
        """A backup checked: its SHA-256, then a restore tried in a database
        of its own beside the instance (dropped after): a job."""
        instance = get_instance(server, role)
        if not instance.plugin.has_verify:
            return refuse(f"{instance.plugin.label} cannot try a restore")
        name = str(payload.get("file") or "")
        folder = backup_folder(instance).resolve()
        path = (folder / name).resolve()
        if not _BACKUP_NAME.fullmatch(name) or path.parent != folder or not path.is_file():
            return refuse("No such backup", status.HTTP_404_NOT_FOUND)
        job = jobs.submit(f"Verify backup {name} of {instance.key}", [instance.key], f"manual: {me}",
                          lambda job: backupcheck.verify(job, instance, path))
        return {"job": job.id}

    @app.get("/backups/{server}/{role}/{name}", dependencies=[Depends(developer)])
    async def backup_download(request: Request, server: str, role: str, name: str,
                              me: Principal = Depends(current_user)):
        # an admin also backs up draft servers (MCP), and downloads their backups
        instance = get_instance(server, role, drafts=me.can("admin"))
        folder = backup_folder(instance).resolve()
        path = (folder / name).resolve()
        # only a zip directly in this instance's backup folder
        if not _BACKUP_NAME.fullmatch(name) or path.parent != folder or not path.is_file():
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such backup")
        access_log.record("download", me.kind, me.name, request.client.host if request.client else "",
                          f"{instance.key}/{name}" + (f" for {me.on_behalf}" if me.on_behalf else ""))
        return FileResponse(path, media_type="application/zip", filename=name)

    def from_link_role(instance: Instance) -> str:
        """Who may copy a linked instance's database into instance: a production
        is an admin's."""
        return "admin" if instance.role == "production" else "developer"

    @app.post("/servers/{server}/{role}/from-link")
    async def action_from_link(request: Request, server: str, role: str,
                               user: Principal = Depends(developer)):
        """Back up the instance's from_link there (or take one of its backups),
        download it into the instance's backups and restore it here."""
        instance = get_instance(server, role)
        src = instance.from_link
        if src is None or not instance.plugin.has_restore:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"{instance.key} has no database from a link")
        if user.kind != "user":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Only a user who logs in here may use a link")
        if not user.can(from_link_role(instance)):
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                f"The {from_link_role(instance)} role is required for a production")
        link = None if src.is_puller else settings.link_targets.get(src.link)
        if src.is_puller and src.link not in settings.connections:
            return refuse(f"No puller connection {src.link}: add it on the Pullers page, or change the server")
        if not src.is_puller and link is None:
            return refuse(f"No link {src.link}: add it on the Links page, or change the server")
        raw, _ = await posted_params(request)
        backup = str(raw.get("backup") or "").strip()
        if backup and src.is_puller:
            return refuse("A puller brings a new backup only")
        if backup and not linkcopy.BACKUP_NAME.fullmatch(backup):
            return refuse(f"{backup!r} is not the name of a backup zip")
        if instance.role == "production":
            # the database it replaces, typed
            expected = str(instance.plugin.describe(instance).get("database") or instance.key)
            if str(raw.get("confirm") or "").strip() != expected:
                return refuse(f"Type {expected} to replace the production database")
        try:
            options = {f.key: _field_value(f, raw.get(f.key)) for f in instance.plugin.restore_fields()}
            # the choices of a new backup there, asked as this instance's
            # plugin asks them (an OmniaCLI there without them backs up as usual)
            # (the checkboxes: the other fields come from the last backup
            # dialog of the instance there)
            backup_options = {} if backup else {
                f.key: _field_value(f, raw.get(f.key)) for f in instance.plugin.backup_fields()
                if f.kind == "checkbox"}
        except ActionRefused as exc:
            return refuse(str(exc))
        chosen = [f.label.lower() for f in instance.plugin.backup_fields()
                  if f.kind == "checkbox" and backup_options.get(f.key)]
        what = backup or "new backup" + (f" {', '.join(chosen)}" if chosen else "")
        if src.is_puller:
            connection = settings.connections[src.link]

            async def from_puller(job):
                progress = job.result["progress"] = {"steps": consumer.copy_steps(connection, instance.key),
                                                     "step": "backup", "done": 0, "total": 0}
                dest = await consumers.fetch_backup(job, connection.name, backup_folder(instance), progress,
                                                    backup_options, user.name)
                progress["step"] = "restore"
                await restore_fetched(job, instance, dest, options)

            job = jobs.submit(f"Copy from {src.label} to {instance.key} ({what})",
                              [instance.key], f"manual: {user}", from_puller)
            return {"job": job.id}
        headers = link_headers(link, user.name, user.role)

        async def action(job):
            # the steps, drawn by the job page as they go
            progress = job.result["progress"] = {
                "steps": [{"id": "backup", "label": f"Backup {backup}" if backup else "Backup",
                           "where": link.name},
                          {"id": "download", "label": "Transfer", "where": f"{link.name} → here"},
                          {"id": "restore", "label": "Restore", "where": instance.key}],
                "step": "backup", "done": 0, "total": 0,
            }
            dest = await linkcopy.fetch_backup(job, link_clients.client(link), link, headers, src,
                                               backup, backup_folder(instance), progress, backup_options)
            progress["step"] = "restore"
            await restore_fetched(job, instance, dest, options)

        job = jobs.submit(f"Copy from {src.label} to {instance.key} ({what})",
                          [instance.key], f"manual: {user}", action)
        return {"job": job.id}

    def _restorable(instance: Instance) -> Instance:
        if not instance.plugin.has_restore:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"{instance.key} cannot restore a backup")
        return instance

    @app.post("/api/servers/{server}/{role}/backups/upload")
    async def backup_upload(request: Request, server: str, role: str, name: str = "",
                            user: Principal = Depends(admin)):
        """A backup zip sent by the browser (the raw body), into the instance's
        backups, through a .part file: whole or absent. An admin's: what is
        restored runs in the database's tools (a dump.sql goes through psql)."""
        instance = _restorable(get_instance(server, role))
        if user.kind != "user":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Only a user who logs in here may upload a backup")
        name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(name).name)
        if not linkcopy.BACKUP_NAME.fullmatch(name):
            return refuse("The file must be a .zip")
        folder = private.backup_folder(backup_folder(instance))
        dest = folder / name
        if dest.exists():
            return refuse(f"{name} is in the backups of {instance.key} already: choose it from the list",
                          status.HTTP_409_CONFLICT)
        length = request.headers.get("content-length", "")
        free = shutil.disk_usage(folder).free
        if length.isdigit() and free - int(length) < puller.MIN_FREE:
            return refuse(f"{name}: {free >> 20} MiB free here, not enough for {int(length) >> 20} MiB",
                          status.HTTP_507_INSUFFICIENT_STORAGE)
        handle, part_name = tempfile.mkstemp(dir=folder, prefix=f"{name}.", suffix=".part")
        part = Path(part_name)
        try:
            with os.fdopen(handle, "wb") as out:
                async for chunk in request.stream():
                    out.write(chunk)
            if not zipfile.is_zipfile(part):
                return refuse(f"{name} is not a zip file")
            os.replace(part, dest)
        except ClientDisconnect:
            return refuse(f"{name}: the upload was interrupted")
        finally:
            part.unlink(missing_ok=True)
        return {"file": name}

    @app.post("/servers/{server}/{role}/restore")
    async def action_restore(request: Request, server: str, role: str, user: Principal = Depends(admin)):
        """Restore a zip of the instance's backups (made here or uploaded)
        into it, with the choices its plugin asks."""
        instance = _restorable(get_instance(server, role))
        if user.kind != "user":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Only a user who logs in here may restore a backup")
        raw, _ = await posted_params(request)
        name = str(raw.get("backup") or "").strip()
        if not linkcopy.BACKUP_NAME.fullmatch(name):
            return refuse(f"{name!r} is not the name of a backup zip")
        path = backup_folder(instance) / name
        if not path.is_file():
            return refuse(f"No backup {name} of {instance.key}", status.HTTP_404_NOT_FOUND)
        # a full archive: its Odoo backup is what is restored
        archive = instancearchive.read_manifest(path) is not None
        problem = None if archive else instance.plugin.backup_problem(path)
        if problem:
            return refuse(problem)
        if instance.role == "production":
            # the database it replaces, typed
            expected = str(instance.plugin.describe(instance).get("database") or instance.key)
            if str(raw.get("confirm") or "").strip() != expected:
                return refuse(f"Type {expected} to replace the production database")
        try:
            options = {f.key: _field_value(f, raw.get(f.key)) for f in instance.plugin.restore_fields()}
        except ActionRefused as exc:
            return refuse(str(exc))
        async def restore(job):
            if not archive:
                await instance.plugin.restore(job, instance, path, options)
                return
            job.log(f"--- the Odoo backup of the archive {name}")
            with instancearchive.backup_of(path) as inner:
                await restore_fetched(job, instance, inner, options)

        job = jobs.submit(f"Restore {name} into {instance.key}", [instance.key], f"manual: {user}", restore)
        return {"job": job.id}

    @app.post("/api/servers/{server}/{role}/archive")
    async def action_archive(server: str, role: str, payload: dict = Body(default={}),
                             user: Principal = Depends(developer)):
        """The full archive of the instance into its backups: a job."""
        instance = get_instance(server, role)
        if not instance.plugin.has_backup:
            return refuse(f"{instance.plugin.label} has no backup")
        job = jobs.submit(f"Full archive of {instance.key}", [instance.key], f"manual: {user}",
                          lambda job: instancearchive.make(job, instance, backup_folder(instance)))
        return {"job": job.id}

    @app.post("/servers/{server}/merge")
    async def action_merge(request: Request, server: str, user: Principal = Depends(admin)):
        srv = settings.servers.get(server)
        if srv is None or not srv.active or srv.production is None or srv.test is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"No server {server} with production and test"
            )
        return started(
            request,
            jobs.submit(
                f"Merge test to production {server}",
                [srv.production.key, srv.test.key],
                f"manual: {user}",
                lambda job: ops.merge_to_production(job, srv, settings.backup_dir),
            )
        )

    # --- the plugins: their buttons on the server page, their routes -------

    # the core's own groups of buttons, which a plugin's action may join
    core_groups = ("service", "git", "backup")

    def action_buttons(target, plugin, scope: str, me: Principal, base: str) -> dict:
        """The plugin's actions the caller may run on target, by group:
        {group: [button]}, the core's groups first."""
        groups = {g: [] for g in core_groups}
        if me.kind == "token":
            return groups
        info = plugin.describe(target) if scope == "instance" else {}
        for action in plugin.actions():
            if action.scope != scope or not me.can(action.role) or not action.applies_to(target):
                continue
            groups.setdefault(action.group or plugin.name, []).append({
                "action": action, "plugin": plugin.name,
                "url": f"{base}/x/{plugin.name}/{action.id}",
                "confirm": action.confirm_of(target),
                "key": getattr(target, "key", getattr(target, "name", "")),
                "info": info,
                "dom_id": re.sub(r"[^A-Za-z0-9_-]", "-", f"act-{target_name(target)}-{plugin.name}-{action.id}"),
            })
        return groups

    def target_name(target) -> str:
        return getattr(target, "key", None) or target.name

    async def posted_params(request: Request) -> tuple[dict, bool]:
        """The parameters posted to an action: JSON (a dialog's script) or a
        form (a button, the core's dialog); True when JSON. No body: no
        parameters, answered as a form (a redirect), as the routes before the
        plugins' actions answered (test_deb.sh posts so)."""
        if request.headers.get("content-type", "").split(";")[0].strip() == "application/json":
            if not await request.body():
                return {}, False
            try:
                data = await request.json()
            except ValueError:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "The body is not JSON") from None
            if not isinstance(data, dict):
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "The body must be a JSON object")
            return data, True
        form = parse_qs((await request.body()).decode("utf-8", "replace"), keep_blank_values=True)
        return {k: v[-1] for k, v in form.items()}, False

    async def run_action(request: Request, target, keys: list[str], plugin, name: str,
                         action_id: str, scope: str, user: Principal):
        if plugin is None or plugin.name != name:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"{target_name(target)} is not of type {name}")
        action = next((a for a in plugin.actions() if a.id == action_id and a.scope == scope), None)
        if action is None or not action.applies_to(target):
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"{plugin.label} has no action {action_id} "
                                                           f"for {target_name(target)}")
        if not user.can(action.role):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"The {action.role} role is required")
        raw, as_json = await posted_params(request)
        try:
            if action.params:
                params = {f.key: _field_value(f, raw.get(f.key)) for f in action.params}
            else:
                # a dialog of the plugin's own: what its script posted
                params = {k: v for k, v in raw.items() if k != "next"} if action.dialog else {}
            if action.prepare is not None:
                params = action.prepare(target, params)
        except ActionRefused as exc:
            return refuse(str(exc))
        job = jobs.submit(action.title_of(target, params), keys, f"manual: {user}",
                          lambda job: action.run(job, target, params))
        if as_json:
            return {"job": job.id}
        back = raw.get("next")
        if back:
            return RedirectResponse(_next_path(back), status.HTTP_303_SEE_OTHER)
        return started(request, job)

    @app.post("/servers/{server}/{role}/x/{plugin}/{action}")
    async def instance_action(request: Request, server: str, role: str, plugin: str, action: str,
                              user: Principal = Depends(current_user)):
        """An action of the plugin of the server, on one of its instances."""
        instance = get_instance(server, role)
        return await run_action(request, instance, [instance.key], instance.plugin, plugin, action,
                                "instance", user)

    @app.post("/servers/{server}/x/{plugin}/{action}")
    async def server_action(request: Request, server: str, plugin: str, action: str,
                            user: Principal = Depends(current_user)):
        """An action of the plugin of the server, on the whole server."""
        srv = active_server(server)
        return await run_action(request, srv, [i.key for i in srv.instances.values()],
                                plugins.registry().get(srv.type), plugin, action, "server", user)

    class AppHost(Host):
        """What a plugin's routes get from this application."""

        def __init__(self, plugin):
            self.plugin = plugin

        def needs(self, role: str):
            return needs(role)

        def instance(self, server: str, role: str) -> Instance:
            instance = get_instance(server, role)
            if instance.plugin is not self.plugin:
                raise HTTPException(status.HTTP_404_NOT_FOUND,
                                    f"{server} is not a server of type {self.plugin.name}")
            return instance

        def submit(self, request, title: str, keys: list[str], action):
            return jobs.submit(title, keys, f"manual: {request.state.user}", action)

        def backup_folder(self, instance: Instance) -> Path:
            return backup_folder(instance)

        def render(self, request, template: str, context: dict):
            return _TEMPLATES.TemplateResponse(request, f"{self.plugin.name}/{template}", context)

    @app.get("/x/{plugin}/static/{path:path}")
    async def plugin_static(plugin: str, path: str):
        """A file of a plugin's static_dir (before its router: it wins)."""
        found = plugins.registry().get(plugin)
        folder = found.static_dir() if found is not None else None
        if folder is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such file")
        folder = Path(folder).resolve()
        target = (folder / path).resolve()
        if folder not in target.parents or not target.is_file():
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such file")
        return FileResponse(target)

    # a plugin whose router cannot be made keeps its servers and buttons,
    # without its routes; the Plugins page says why
    route_errors: dict[str, str] = {}
    app.state.plugin_route_errors = route_errors
    for plugin_name, plugin in plugins.registry().plugins.items():
        try:
            router = plugin.router(AppHost(plugin))
            if router is not None:
                app.include_router(router, prefix=f"/x/{plugin_name}")
        except Exception as exc:  # noqa: BLE001 - a plugin must not stop OmniaCLI
            route_errors[plugin_name] = f"{type(exc).__name__}: {exc}"
            _logger.warning("Plugin %s: its routes are not served: %s", plugin_name, route_errors[plugin_name])

    @app.get("/servers/{server}/{role}/log")
    async def log_page(request: Request, server: str, role: str):
        return _TEMPLATES.TemplateResponse(
            request, "log.html", {"instance": get_instance(server, role)}
        )

    @app.get("/api/servers/{server}/{role}/log")
    async def log_read(
        server: str, role: str, pos: Optional[int] = None, inode: Optional[int] = None
    ):
        """The log lines after pos (the last 300 when pos is missing)."""
        instance = get_instance(server, role)
        if instance.log_file is None:
            # compose: docker compose logs, pos is the time of the last line
            try:
                return await ops.compose_log(instance, pos)
            except ops.LogUnavailable as exc:
                return JSONResponse({"error": f"Cannot read {instance.log_label}: {exc}"}, status_code=404)
        try:
            return await asyncio.to_thread(read_since, instance.log_file, pos, inode)
        except OSError as exc:
            return JSONResponse({"error": f"Cannot read {instance.log_file}: {exc.strerror}"},
                                status_code=404)

    @app.get("/omniacli/log", dependencies=[Depends(admin)])
    async def own_log_page(request: Request):
        return _TEMPLATES.TemplateResponse(request, "own_log.html", {"log_file": own_log_file})

    @app.get("/api/omniacli/log", dependencies=[Depends(admin)])
    async def own_log_read(pos: Optional[int] = None, inode: Optional[int] = None):
        """OmniaCLI's own log lines after pos (the last 300 when pos is missing)."""
        try:
            return await asyncio.to_thread(read_since, own_log_file, pos, inode)
        except FileNotFoundError:
            return {"lines": [], "pos": None, "inode": None, "rotated": False, "more": False}
        except OSError as exc:
            return JSONResponse({"error": f"Cannot read {own_log_file}: {exc.strerror}"}, status_code=404)

    @app.get("/jobs")
    async def job_list(request: Request, server: str = "", q: str = ""):
        """Every job kept, filtered by server and by text (title, started
        by, error, server)."""
        me = request.state.user
        found = visible_jobs(me, jobs.recent(len(jobs.jobs)))
        shown = visible_servers(me)
        names = sorted((set(settings.servers) if shown is None else shown) | {n for j in found for n in j.servers})
        found = filter_jobs(found, server, q)
        return _TEMPLATES.TemplateResponse(
            request, "jobs.html",
            {"jobs": found[:200], "total": len(found), "servers": names, "server": server, "q": q},
        )

    @app.get("/jobs/{job_id}")
    async def job_page(request: Request, job_id: str):
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such job")
        # a draft or deleted server has no page: its crumb goes to its jobs
        live = [srv.name for srv in settings.active_servers()]
        return _TEMPLATES.TemplateResponse(request, "job.html", {"job": job, "live": live})

    @app.post("/jobs/{job_id}/stop")
    async def job_stop(request: Request, job_id: str, user: Principal = Depends(admin)):
        """Stop a queued or running job (one that hangs or loops): its program
        is ended, what it did before stays done."""
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such job")
        if not jobs.stop(job_id, str(user)):
            raise HTTPException(status.HTTP_409_CONFLICT, f"The job is not running: it is {job.status}")
        return RedirectResponse(f"{prefix_of(request)}/jobs/{job.id}", status.HTTP_303_SEE_OTHER)

    @app.get("/api/jobs")
    async def jobs_json(request: Request, limit: int = 20):
        """The last jobs, without their logs, and the instances busy now: the
        Dashboard follows them (runners, end notices)."""
        me = request.state.user
        found = visible_jobs(me, jobs.recent(len(jobs.jobs)))[:min(max(limit, 1), 200)]
        running = visible_jobs(me, [j for j in jobs.jobs.values() if j.status == "running"])
        return {
            "jobs": [{"id": j.id, "title": j.title, "status": j.status, "instances": j.instances,
                      "servers": j.servers, "source": j.source, "created": j.created,
                      "finished": j.finished, "duration": j.duration, "error": j.error} for j in found],
            "busy": sorted({key for j in running for key in j.instances}),
        }

    @app.get("/api/jobs/{job_id}")
    async def job_json(job_id: str, since: int = 0):
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such job")
        data = asdict(job)
        data["duration"] = job.duration
        data["lines"] = job.lines[since:]
        data["next"] = len(job.lines)
        return JSONResponse(data)

    # --- server configuration -------------------------------------------

    config_lock = asyncio.Lock()

    def server_busy(name: str) -> bool:
        return any(jobs.is_busy(f"{name}/{role}") for role in ("production", "test"))

    def server_form(request: Request, title: str, original, name: str, data: dict):
        return _TEMPLATES.TemplateResponse(
            request,
            "server_form.html",
            {
                "title": title,
                "model": {
                    "original": original,
                    "name": name,
                    "server": data,
                    "active": bool(original and settings.servers[original].active),
                    "logs_dir": str(settings.logs_dir),
                    # what the form offers: the loaded plugins, with their fields
                    "types": {n: {"label": p.label, "has_process": p.has_process, "has_compose": p.has_compose,
                                  "has_restore": p.has_restore,
                                  "fields": [_field_dict(f) for f in p.fields()]}
                              for n, p in plugins.registry().plugins.items()},
                    "default_type": plugins.DEFAULT_TYPE,
                    # where a database may come from (Copy from link)
                    "links": sorted(settings.link_targets),
                    # or a puller connection (Pullers page)
                    "connections": sorted(settings.connections),
                },
            },
        )

    def configured(name: str):
        srv = settings.servers.get(name)
        if srv is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No server {name}")
        return srv

    def editable(name: str):
        """A server the form edits: one whose plugin is there (one whose
        plugin is missing is left as written)."""
        srv = configured(name)
        if srv.missing_plugin:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{name} cannot be edited: {srv.error}")
        return srv

    @app.get("/config", dependencies=[Depends(admin)])
    async def config_page(request: Request):
        return _TEMPLATES.TemplateResponse(
            request,
            "config.html",
            {
                "servers": settings.servers.values(),
                "servers_file": settings.servers_file,
                "busy": {name: server_busy(name) for name in settings.servers},
            },
        )

    @app.get("/config/new", dependencies=[Depends(admin)])
    async def config_new(request: Request):
        return server_form(request, "New server", None, "", {})

    @app.get("/config/{name}/edit", dependencies=[Depends(admin)])
    async def config_edit(request: Request, name: str):
        srv = editable(name)
        return server_form(request, f"Edit {name}", name, name, server_to_dict(srv))

    @app.get("/config/{name}/clone", dependencies=[Depends(admin)])
    async def config_clone(request: Request, name: str):
        srv = editable(name)
        new_name = f"{name}_copy"
        while new_name in settings.servers:
            new_name += "_copy"
        return server_form(request, f"Clone of {name}", None, new_name, server_to_dict(srv))

    def refuse(message: str, code: int = status.HTTP_400_BAD_REQUEST):
        return JSONResponse({"error": message}, status_code=code)

    def acts_for_a_person(user: Principal) -> bool:
        """A user who logs in here, or a linked OmniaCLI acting for one of its
        users (its pages through the link): not a token, not a link alone."""
        return user.kind == "user" or (user.kind == "link" and bool(user.on_behalf))

    def login_as_target(request: Request, instance: Instance, user: Principal) -> tuple[str, str]:
        """Who may log in as a user of instance (a production is an admin's,
        a test a developer's, a user who logs in here), and where: the base
        URL the browser opens /.omniacli/login at and the instance's own base
        URL. Raises HTTPException when it cannot."""
        if not instance.plugin.has_login:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"{instance.key} cannot log in as a user")
        if not acts_for_a_person(user):
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                "Only a person may log in as a user: one who logs in here, or through a link")
        if not user.can(from_link_role(instance)):
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                f"The {from_link_role(instance)} role is required for a production")
        web = instance.web_url or ""
        parts = urlsplit(web)
        if parts.scheme == "https" and parts.hostname:
            # the instance's nginx passes /.omniacli/login to OmniaCLI
            base = f"https://{parts.netloc}"
            return base, base
        if parts.hostname in ("localhost", "127.0.0.1") and request.url.hostname == parts.hostname:
            # this machine: the browser keeps cookies by name, not by port, so
            # OmniaCLI's own page sets Odoo's cookie for the same localhost
            return f"{request.url.scheme}://{request.headers.get('host', parts.hostname)}", f"http://{parts.netloc}"
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "Log in as needs the instance's Domain name over https (server form, Edit), "
                            "with its nginx passing /.omniacli/login to OmniaCLI (README, Log in as); "
                            f"the instance is opened at {web or 'no address'}")

    login_path_seen: dict[str, float] = {}
    LOGIN_PATH_TRIES, LOGIN_PATH_PAUSE = 10, 1.0

    async def login_path_reached(host: str) -> bool:
        """Whether https://host/.omniacli/login reaches this OmniaCLI (its
        answer carries LOGIN_MARK); a yes is kept 10 minutes."""
        if time.monotonic() - login_path_seen.get(host, -1e9) < 600:
            return True
        # this machine's nginx first (asked on the loopback), then the public name
        if await loginas.passed_locally(host) or await loginas.reaches_here(host):
            login_path_seen[host] = time.monotonic()
            return True
        return False

    def save_login_hosts() -> None:
        """The names of the instances whose nginx is not a workspace's, for
        root's timer that keeps the Log in as path in their sites
        (workspace-helper login-path keep on); written when they change."""
        hosts = sorted({urlsplit(inst.web_url).hostname for srv in settings.servers.values() if srv.active
                        for inst in srv.instances.values()
                        if workspace_of(srv.name) is None and (inst.web_url or "").startswith("https://")
                        and urlsplit(inst.web_url).hostname})
        path = settings.servers_file.parent / workspaceweb.LOGIN_HOSTS_FILE
        text = json.dumps(hosts)
        try:
            if path.read_text(encoding="utf-8") == text:
                return
        except OSError:
            pass
        try:
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            _logger.warning("the names for Log in as cannot be written in %s: %s", path, exc)

    async def login_path_problem(request: Request, instance: Instance, user: Principal):
        """A refusal when the instance's nginx (not a workspace's: those pass
        it by themselves) does not pass the Log in as path here; for an admin
        of the machine, the address that adds it."""
        if workspace_of(instance.server) is not None or (user.on_behalf and request.headers.get(TUNNEL_HEADER) == "1"):
            # a linked OmniaCLI whose browser reaches its tunnels opens the link through one
            return None
        host = urlsplit(instance.web_url or "").hostname or ""
        if not host or not (instance.web_url or "").startswith("https://") or await login_path_reached(host):
            return None
        api = f"{prefix_of(request)}/api/servers/{instance.server}/{instance.role}/login-as/nginx"
        return JSONResponse({"error": f"The nginx of {host} does not pass {loginas.LOGIN_PATH} to OmniaCLI: "
                                      "Odoo would answer the link, not OmniaCLI (README, Log in as)",
                             "nginx": api if machine_admin(user) else None, "host": host,
                             # root's switch on the machine: the line kept in every site, every minute
                             "keep": "omniacli workspace-helper login-path keep on" if machine_admin(user) else None},
                            status_code=status.HTTP_409_CONFLICT)

    @app.post("/api/servers/{server}/{role}/login-as/nginx")
    async def login_as_nginx(server: str, role: str, me: Principal = Depends(current_user)):
        """One line in the instance's server block of 443 of this machine's
        nginx (a hosting panel's site): an include of the paths OmniaCLI
        answers there; the file before kept, nginx -t, undone by
        workspace-helper login-path remove <host>."""
        if not machine_admin(me):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "An admin of the machine is required")
        instance = get_instance(server, role)
        host = urlsplit(instance.web_url or "").hostname or ""
        if not host:
            return refuse(f"{instance.key} has no domain name")

        async def action(job):
            found = await workspaceops._helper(job, "login-path", "add", host)
            login_path_seen.pop(host, None)
            job.log(f"nginx of {host}: {', '.join(found.get('changed') or []) or 'already passing it'} "
                    f"({found.get('nginx')}); to undo: {found.get('undo')}")
            # nginx's workers take the reload a moment later: asked a few times
            for _ in range(LOGIN_PATH_TRIES):
                if await login_path_reached(host):
                    break
                await asyncio.sleep(LOGIN_PATH_PAUSE)
            else:
                raise JobFailed(f"https://{host}{loginas.LOGIN_PATH} still does not reach OmniaCLI: "
                                "another nginx or a proxy in front of this one answers that name")
            job.result = found
        job = jobs.submit(f"Log in as through the nginx of {host}", [instance.key], f"manual: {me}", action)
        return {"job": job.id}

    @app.get("/api/servers/{server}/{role}/login-as/users")
    async def login_as_users(request: Request, server: str, role: str, user: Principal = Depends(developer)):
        """The users of the instance one may log in as."""
        instance = get_instance(server, role)
        login_as_target(request, instance, user)
        problem = await login_path_problem(request, instance, user)
        if problem is not None:
            return problem
        found = await instance.plugin.login_session(instance, "users")
        if "error" in found:
            return refuse(found["error"], status.HTTP_502_BAD_GATEWAY)
        return {"users": found.get("users") or []}

    @app.post("/api/servers/{server}/{role}/login-as")
    async def login_as(request: Request, server: str, role: str, payload: dict = Body(...),
                       user: Principal = Depends(developer)):
        """A session of the user chosen ({"id": n}) in the instance's store,
        and the link that hands it to the browser: {"url"}."""
        instance = get_instance(server, role)
        base, odoo_base = login_as_target(request, instance, user)
        uid = payload.get("id")
        if not isinstance(uid, int) or isinstance(uid, bool) or uid < 1:
            return refuse("id: the id of a user of the instance")
        made = await instance.plugin.login_session(instance, str(uid))
        if "error" in made or not isinstance(made.get("sid"), str):
            return refuse(made.get("error") or "no session made", status.HTTP_502_BAD_GATEWAY)
        detail = f"{instance.key} as {made.get('login')} (id {uid})"
        home = odoo_base + loginas.safe_home(made.get("home"))
        token = login_tickets.issue(made["sid"], urlsplit(base).netloc, home, str(user), detail)
        access_log.record("login-as", user.kind, str(user), request.client.host if request.client else "",
                          f"asked: {detail}")
        return {"url": f"{base}{loginas.LOGIN_PATH}?t={token}", "login": made.get("login"),
                "seconds": loginas.TOKEN_SECONDS}

    @app.post("/api/servers/save", dependencies=[Depends(admin)])
    async def config_save(payload: dict = Body(...)):
        original = payload.get("original") or None
        name = str(payload.get("name") or "").strip()
        async with config_lock:
            if original is not None and original not in settings.servers:
                return refuse(f"Server {original} no longer exists", status.HTTP_404_NOT_FOUND)
            if name != original and name in settings.servers:
                return refuse(f"A server named {name} already exists")
            for busy_name in {original, name} - {None}:
                if server_busy(busy_name):
                    return refuse(f"A job is running on {busy_name}: try again when it ends")
            server_data = dict(payload.get("server") or {})
            # the status is never taken from the form: a save makes a draft,
            # only passing checks make it active
            server_data["status"] = "draft"
            try:
                parsed = parse_servers({name: server_data}, settings.logs_dir)[name]
            except (ConfigError, TypeError, ValueError, AttributeError) as exc:
                return refuse(str(exc))
            was_active = original is not None and settings.servers[original].active
            report = None
            if payload.get("activate"):
                report = await checks.check_server(parsed, settings.backup_dir, settings.machine)
                if checks.passed(report):
                    parsed.status = "active"
                elif was_active:
                    # the live server stays as it was
                    return JSONResponse(
                        {"error": "The checks failed: nothing was saved, the server is "
                                  "still live with its previous settings",
                         "checks": [c.to_dict() for c in report]},
                        status_code=400,
                    )
            servers = {}
            for key, srv in settings.servers.items():
                # an edited server keeps its place in the list
                servers[name if key == original else key] = parsed if key == original else srv
            servers.setdefault(name, parsed)
            error = await store(servers)
            if error:
                return error
            # a renamed server stays in its workspace; a new one is in none,
            # whatever a workspace still names
            error = await rename_in_workspaces(original, name)
            if error:
                return error
        return {
            "ok": True,
            "status": parsed.status,
            "checks": [c.to_dict() for c in report] if report is not None else None,
        }

    @app.post("/api/servers/check", dependencies=[Depends(admin)])
    async def config_check(payload: dict = Body(...)):
        """The checks on what the form holds: nothing is saved, the form stays open."""
        name = str(payload.get("name") or "").strip() or "new"
        server_data = dict(payload.get("server") or {})
        server_data["status"] = "draft"
        try:
            parsed = parse_servers({name: server_data}, settings.logs_dir)[name]
        except (ConfigError, TypeError, ValueError, AttributeError) as exc:
            return refuse(str(exc))
        options = gitcredentials.options_for(settings, name)
        for inst in (parsed.production, parsed.test):
            for repo in (inst.repositories if inst is not None else []):
                repo.git_options = options
        report = await checks.check_server(parsed, settings.backup_dir, settings.machine)
        return {"ok": checks.passed(report), "checks": [c.to_dict() for c in report]}

    @app.post("/api/servers/branches", dependencies=[Depends(admin)])
    async def config_branches(payload: dict = Body(...)):
        """The branches of a repository row of the form (its path and remote)."""
        path = str(payload.get("path") or "").strip()
        remote = str(payload.get("remote") or "").strip() or "origin"
        if not path or not os.path.isabs(path):
            return refuse("Type the repository's full path first")
        if remote.startswith("-"):
            return refuse(f"Invalid remote {remote}")
        server = str(payload.get("server") or "")
        options = gitcredentials.options_for(settings, server) if SERVER_NAME.fullmatch(server) else ()
        data = await ops.remote_branches(Path(path), remote, checks.GIT_TIMEOUT, options)
        if "error" in data:
            return refuse(data["error"])
        return data

    async def write_servers(servers):
        """Write the servers file and make it current (OSError when it cannot)."""
        save_servers(settings.servers_file, servers)
        settings.servers = servers
        apply_sites(settings.servers, settings.workspaces)
        save_login_hosts()
        gitcredentials.bind(settings)
        await updater.reload()

    async def store(servers):
        """write_servers; a response on error."""
        try:
            await write_servers(servers)
        except OSError as exc:
            return refuse(f"Cannot write {settings.servers_file}: {exc}", 500)
        return None

    async def add_draft_server(name: str, data: dict):
        """A new server, always a draft (from MCP): it goes live only with
        Test & activate on the web. ConfigError when it cannot be added."""
        async with config_lock:
            if name in settings.servers:
                raise ConfigError(f"A server named {name} already exists")
            if not isinstance(data, dict):
                raise ConfigError("the server must be a mapping of production and test")
            try:
                parsed = parse_servers({name: {**data, "status": "draft"}}, settings.logs_dir)[name]
            except (TypeError, ValueError, AttributeError) as exc:
                raise ConfigError(str(exc)) from exc
            try:
                await write_servers({**settings.servers, name: parsed})
            except OSError as exc:
                raise ConfigError(f"Cannot write {settings.servers_file}: {exc}") from exc
            return parsed

    @app.get("/config/discover", dependencies=[Depends(admin)])
    async def config_discover(request: Request):
        return _TEMPLATES.TemplateResponse(request, "discover.html", {"roles": ROLES})

    @app.get("/api/servers/discover", dependencies=[Depends(admin)])
    async def discover_list(request: Request):
        """What every plugin finds on this machine (Discover page)."""
        known = [i for s in settings.servers.values() for i in s.instances.values()]
        found = []
        for name, plugin in plugins.registry().plugins.items():
            try:
                items = await plugin.discover([i for i in known if i.type == name])
            except Exception as exc:  # noqa: BLE001 - one plugin does not hide the others
                _logger.exception("discover of %s", name)
                found.append({"type": name, "type_label": plugin.label, "error": str(exc)})
                continue
            found.extend({"type": name, "type_label": plugin.label, **item.to_dict()} for item in items)
        # what the page shows as text, in its language (the values stay as they are)
        lang = request_language(request)
        for item in found:
            for key in ("notes", "missing"):
                if isinstance(item.get(key), list):
                    item[key] = [i18n.tr(lang, text) if isinstance(text, str) else text for text in item[key]]
            if isinstance(item.get("facts"), list):
                item["facts"] = [[i18n.tr(lang, f[0]), i18n.tr(lang, f[1])] if isinstance(f, (list, tuple)) and len(f) == 2
                                 else f for f in item["facts"]]
            if isinstance(item.get("error"), str):
                item["error"] = i18n.tr(lang, item["error"])
        return {"found": found, "names": sorted(settings.servers)}

    @app.post("/api/servers/discover", dependencies=[Depends(admin)])
    async def discover_save(payload: dict = Body(...)):
        """Save what the Discover page chose as drafts: items of {name, role,
        type, instance}; the items of one name are the instances of one server."""
        servers: dict[str, dict] = {}
        for item in payload.get("items") or []:
            if not isinstance(item, dict) or not isinstance(item.get("instance"), dict):
                return refuse("Every item needs a name, a role, a type and its instance")
            name, role = str(item.get("name") or "").strip(), str(item.get("role") or "")
            kind = str(item.get("type") or "")
            if not SERVER_NAME.fullmatch(name):
                return refuse(f"{name!r}: a server name is letters, digits, '.', '_' and '-'")
            if role not in ROLES:
                return refuse(f"{name}: the role is {' or '.join(ROLES)}")
            server = servers.setdefault(name, {"type": kind, "status": "draft"})
            if server["type"] != kind:
                return refuse(f"{name}: its instances are of different types")
            if role in server:
                return refuse(f"{name}: two instances of role {role}")
            server[role] = item["instance"]
        if not servers:
            return refuse("Choose at least one instance")
        async with config_lock:
            taken = [n for n in servers if n in settings.servers]
            if taken:
                return refuse(f"A server named {', '.join(taken)} already exists")
            try:
                parsed = parse_servers(servers, settings.logs_dir)
            except (ConfigError, TypeError, ValueError, AttributeError) as exc:
                return refuse(str(exc))
            error = await store({**settings.servers, **parsed})
            if error:
                return error
        return {"ok": True, "created": [{"name": n, "error": s.error} for n, s in parsed.items()]}

    @app.post("/api/servers/{name}/test", dependencies=[Depends(admin)])
    async def config_test(name: str):
        """Run the checks; a draft that passes them becomes active."""
        async with config_lock:
            srv = settings.servers.get(name)
            if srv is None:
                return refuse(f"No server {name}", status.HTTP_404_NOT_FOUND)
            report = await checks.check_server(srv, settings.backup_dir, settings.machine)
            ok = checks.passed(report)
            if ok and not srv.active:
                srv.status = "active"
                error = await store(dict(settings.servers))
                if error:
                    srv.status = "draft"
                    return error
        return {"ok": ok, "status": srv.status, "checks": [c.to_dict() for c in report]}

    @app.post("/api/servers/{name}/deactivate", dependencies=[Depends(admin)])
    async def config_deactivate(name: str):
        async with config_lock:
            srv = settings.servers.get(name)
            if srv is None:
                return refuse(f"No server {name}", status.HTTP_404_NOT_FOUND)
            if server_busy(name):
                return refuse(f"A job is running on {name}: try again when it ends")
            srv.status = "draft"
            error = await store(dict(settings.servers))
            if error:
                srv.status = "active"
                return error
        return {"ok": True, "status": "draft"}

    @app.post("/api/servers/delete", dependencies=[Depends(admin)])
    async def config_delete(payload: dict = Body(...)):
        name = str(payload.get("name") or "")
        async with config_lock:
            if name not in settings.servers:
                return refuse(f"No server {name}", status.HTTP_404_NOT_FOUND)
            if server_busy(name):
                return refuse(f"A job is running on {name}: try again when it ends")
            # an instance a workspace made: its line alone would leave its
            # containers running and its data on the disk (18/11/2026: a
            # staging taken off here kept 2 containers up). It is deleted
            # from its page; the line alone only when asked for by name
            made_by = next((ws for ws in settings.workspaces.values()
                            if ws.storage and name in ws.servers and name.startswith(f"{ws.name}-")), None)
            if made_by is not None and payload.get("configuration_only") is not True:
                return refuse(f"{name} is an instance of workspace {made_by.name}: delete it from its page, which "
                              f"removes its containers and its data too")
            servers = {k: v for k, v in settings.servers.items() if k != name}
            error = await store(servers)
            if error:
                return error
            # out of its workspace: a later server of the same name is not in it
            error = await rename_in_workspaces(name, None)
            if error:
                return error
        return {"ok": True}

    # --- workspaces -------------------------------------------------------

    workspaces_lock = asyncio.Lock()

    async def store_workspaces(found: dict[str, Workspace]):
        """Write the workspaces file and make it current; a response on error."""
        try:
            await asyncio.to_thread(save_workspaces, settings.workspaces_file, found)
        except OSError as exc:
            return refuse(f"Cannot write {settings.workspaces_file}: {exc}", 500)
        settings.workspaces = found
        apply_sites(settings.servers, settings.workspaces)
        gitcredentials.bind(settings)
        return None

    async def rename_in_workspaces(old: str | None, new: str | None):
        """The server old is now new (None: deleted); a new server (old None)
        leaves any workspace that still names it."""
        if old == new:
            return None
        async with workspaces_lock:
            changed = {}
            for ws in settings.workspaces.values():
                # a workspace naming new still (a deleted server's leftover) loses it
                names = [s for s in ws.servers if new is None or s != new]
                if old is not None:
                    names = [n for n in (new if s == old else s for s in names) if n is not None]
                changed[ws.name] = names_follow(ws, dataclasses.replace(ws, servers=names), old, new)
            if changed == settings.workspaces:
                return None
            return await store_workspaces(changed)

    def names_follow(before: Workspace, ws: Workspace, old: str | None, new: str | None) -> Workspace:
        """The main instance and the subdomains of ws after the server old
        became new (None: deleted): renamed with it, or dropped."""
        prefix = f"{ws.name}-"
        gone = old[len(prefix):] if old and old in before.servers and old.startswith(prefix) else None
        now = new[len(prefix):] if new and new in ws.servers and new.startswith(prefix) else None
        main = (now or "") if gone and ws.main == gone else ws.main
        subdomains = {sub: (now if gone and target == gone else target) for sub, target in ws.subdomains.items()}
        instances = ws.instances()
        return dataclasses.replace(
            ws, main=main if main in instances else "",
            subdomains={s: t for s, t in subdomains.items() if t in instances and s not in instances})

    def submit_publish(ws: Workspace, source: str):
        """A job: the workspace's nginx site and certificate, as it is when the job runs."""
        async def action(job):
            current = settings.workspaces.get(ws.name)
            if current is None:
                raise JobFailed(f"no workspace {ws.name}")
            job.result = await workspaceops.publish(job, current)
        return jobs.submit(f"Names of workspace {ws.name}", [f"{s}/{r}" for s in ws.servers for r in ROLES],
                           source, action)

    def site_names(ws: Workspace) -> list[dict]:
        try:
            return web_names(ws.domain, [i for i in ws.instances() if workspaceops.server_name(ws, i) in settings.servers],
                             ws.main, ws.subdomains) if ws.domain else []
        except ConfigError:
            return []

    def workspace_rows() -> list[dict]:
        return [{**ws.to_dict(), "name": ws.name, "storage_problem": settings.storage_problem(ws),
                 "instances_count": len([s for s in ws.servers if s in settings.servers])}
                for ws in sorted(settings.workspaces.values(), key=lambda w: w.name)]

    # --- git credentials (gitcredentials.py) ---------------------------------

    credentials_lock = asyncio.Lock()

    def workspace_credentials(name: str) -> list[dict]:
        try:
            return credential_rows(gitcredentials.load(settings.credentials_file), f"workspace:{name}")
        except gitcredentials.CredentialError:
            return []

    def credential_rows(found, scope: str) -> list[dict]:
        return [c.public() for c in sorted(found.get(scope, {}).values(), key=lambda c: c.host)]

    async def change_credentials(scope: str, payload: dict, delete: bool):
        """Add or change (a token left empty keeps the one saved) or delete
        one host's credential of a scope; then every repository gets them."""
        host = str(payload.get("host") or "").strip().lower()
        async with credentials_lock:
            try:
                found = await asyncio.to_thread(gitcredentials.load, settings.credentials_file)
                entries = dict(found.get(scope, {}))
                if delete:
                    if entries.pop(host, None) is None:
                        return refuse(f"No credential for {host}", status.HTTP_404_NOT_FOUND)
                else:
                    token = str(payload.get("token") or "")
                    if not token and host in entries:
                        token = entries[host].token
                    cred = gitcredentials.check(host, str(payload.get("username") or "").strip(), token)
                    entries[cred.host] = cred
                found[scope] = entries
                await asyncio.to_thread(gitcredentials.save, settings.credentials_file, found)
            except gitcredentials.CredentialError as exc:
                return refuse(str(exc))
            except OSError as exc:
                return refuse(f"Cannot write {settings.credentials_file}: {exc}", 500)
            await asyncio.to_thread(gitcredentials.bind, settings)
        return {"ok": True, "credentials": credential_rows(found, scope)}

    @app.get("/credentials", dependencies=[Depends(admin)])
    async def credentials_page(request: Request):
        """The git credentials of every server and workspace (users of the machine only)."""
        try:
            found = await asyncio.to_thread(gitcredentials.load, settings.credentials_file)
            error = None
        except gitcredentials.CredentialError as exc:
            found, error = {}, str(exc)
        scopes = ([{"kind": "server", "name": n, "workspace": settings.workspace_of(n),
                    "credentials": credential_rows(found, f"server:{n}")} for n in sorted(settings.servers)]
                  + [{"kind": "workspace", "name": n, "workspace": None,
                      "credentials": credential_rows(found, f"workspace:{n}")} for n in sorted(settings.workspaces)])
        return _TEMPLATES.TemplateResponse(
            request, "credentials.html",
            {"scopes": scopes, "error": error, "credentials_file": settings.credentials_file})

    @app.post("/api/credentials/save", dependencies=[Depends(admin)])
    async def credentials_save(payload: dict = Body(...)):
        try:
            scope = gitcredentials.scope_of(str(payload.get("kind") or ""), str(payload.get("name") or ""))
        except gitcredentials.CredentialError as exc:
            return refuse(str(exc))
        name = scope.split(":", 1)[1]
        if name not in (settings.servers if scope.startswith("server:") else settings.workspaces):
            return refuse(f"No {scope.split(':', 1)[0]} {name}", status.HTTP_404_NOT_FOUND)
        return await change_credentials(scope, payload, delete=False)

    @app.post("/api/credentials/delete", dependencies=[Depends(admin)])
    async def credentials_delete(payload: dict = Body(...)):
        try:
            scope = gitcredentials.scope_of(str(payload.get("kind") or ""), str(payload.get("name") or ""))
        except gitcredentials.CredentialError as exc:
            return refuse(str(exc))
        return await change_credentials(scope, payload, delete=True)

    @app.post("/api/workspaces/{workspace}/credentials/save", dependencies=[Depends(admin)])
    async def workspace_credentials_save(workspace: str, payload: dict = Body(...)):
        """The workspace's git credentials, set by its admins."""
        ws = a_workspace(workspace)
        return await change_credentials(gitcredentials.scope_of("workspace", ws.name), payload, delete=False)

    @app.post("/api/workspaces/{workspace}/credentials/delete", dependencies=[Depends(admin)])
    async def workspace_credentials_delete(workspace: str, payload: dict = Body(...)):
        ws = a_workspace(workspace)
        return await change_credentials(gitcredentials.scope_of("workspace", ws.name), payload, delete=True)

    @app.get("/workspaces", dependencies=[Depends(admin)])
    async def workspaces_page(request: Request):
        """The customers' workspaces (users of the machine only)."""
        return _TEMPLATES.TemplateResponse(
            request, "workspaces.html",
            {"workspaces": workspace_rows(), "workspaces_file": settings.workspaces_file,
             "servers": sorted(settings.servers), "users": sorted(settings.users),
             "user_roles": {n: settings.user_role(n) for n in settings.users},
             "storage_roots": settings.storage_roots, "storage_error": settings.storage_error,
             "system_config": pluginstore.SYSTEM_CONFIG,
             "roles": ACCESS_ROLES, "limits": WORKSPACE_LIMITS,
             "templates": await asyncio.to_thread(template_rows),
             "templates_index": templatestore.index_url(settings.plugin_source)},
        )

    def template_rows() -> list[dict]:
        """The templates here: the package's and those downloaded, and which one is used."""
        package = stacktemplates.available([workspacehelper.TEMPLATES])
        downloaded = stacktemplates.available([templatestore.INSTALLED])
        used = workspaceops.templates()
        return [{"name": name, "label": used[name].label, "package": package[name].version if name in package else None,
                 "downloaded": downloaded[name].version if name in downloaded else None,
                 "used": used[name].version} for name in sorted(used)]

    @app.get("/api/templates/available", dependencies=[Depends(admin)])
    async def templates_available():
        """The templates published for download (signed), with the version used here."""
        try:
            found = await templatestore.available(settings.plugin_source)
        except pluginstore.StoreError as exc:
            return refuse(str(exc), status.HTTP_502_BAD_GATEWAY)
        used = await asyncio.to_thread(workspaceops.templates)
        return {"index": templatestore.index_url(settings.plugin_source),
                "templates": [{**t, "used": used[t["name"]].version if t["name"] in used else None} for t in found]}

    @app.post("/api/templates/{name}/install")
    async def template_install(name: str, user: Principal = Depends(admin)):
        """Download a published template, check it, and install it through
        the root helper: new instances are made from it (the higher version)."""
        try:
            found = {t["name"]: t for t in await templatestore.available(settings.plugin_source)}
        except pluginstore.StoreError as exc:
            return refuse(str(exc), status.HTTP_502_BAD_GATEWAY)
        template = found.get(name)
        if template is None:
            return refuse(f"The index publishes no template {name}", status.HTTP_404_NOT_FOUND)
        if template.get("error"):
            return refuse(f"{name}: {template['error']}")

        async def action(job):
            manifest = await templatestore.download(job, settings.plugin_source, template,
                                                    templatestore.templates_dir(settings.jobs_dir))
            await ops.run(job, workspaceops.helper("template-install", name), timeout=workspaceops.HELPER_TIMEOUT)
            job.result = {"template": name, "version": manifest["version"]}
            job.log(f"Template {name} {manifest['version']} installed: new instances are made from it")

        job = jobs.submit(f"Install template {name} {template.get('version', '')}".strip(), ["omniacli/templates"],
                          f"manual: {user}", action)
        return {"job": job.id}

    @app.get("/api/workspaces/load", dependencies=[Depends(admin)])
    async def workspaces_load(since: int = 0):
        """The machine's load and each workspace's now (metrics.py): CPU in
        percent of one core, memory in MiB, traffic in bytes per second;
        series, each workspace's points after since (the list's sparks)."""
        machine = sampler.machine_series()
        return {"cores": sampler.cores, "interval": 5, "machine": machine[-1] if machine else None,
                "workspaces": sampler.workspace_now(),
                "series": {name: sampler.workspace_series(name, since) for name in settings.workspaces} if since else {}}

    @app.get("/api/workspaces/{workspace}/load")
    async def workspace_load(workspace: str, since: int = 0):
        """One workspace's load for its members (the Dashboard): its points
        after since (metrics.py), its limits, and its disk, used and size, in
        bytes. Not the machine's load, nor the room of its real disk."""
        ws = settings.workspaces.get(workspace)
        if ws is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No workspace {workspace}")
        disk, disk_error = None, ""
        if settings.storage_problem(ws):
            disk_error = "its storage is not usable: ask an admin"
        elif ws.storage:
            try:
                found = await workspaceops.space()
            except workspaceops.SpaceError:
                disk_error = "the disk cannot be read now"
            else:
                r = workspaceops.room(found, settings.workspaces, ws.storage, ws.name)
                if r is not None:
                    disk = {"used": r["own_used"], "size": r["own_size"],
                            "forecast": disk_history.workspace_forecast(ws.name)}
        return {"interval": 5, "points": sampler.workspace_series(ws.name, since),
                "limits": {k: ws.limits.get(k) for k in ("cpus", "memory_gb", "disk_gb")},
                "disk": disk, "disk_error": disk_error}

    @app.get("/api/workspaces/space", dependencies=[Depends(admin)])
    async def workspaces_space():
        """The room of each storage root's real disk, for each workspace and
        for a new one (""): what its disk may have (README, Workspaces)."""
        try:
            found = await workspaceops.space()
        except workspaceops.SpaceError as exc:
            hint = f". {workspaceops.HELPERS_HINT}" if workspaceops.helpers_refused(str(exc)) else ""
            return refuse(f"The room of the disks cannot be read: {exc}{hint}", status.HTTP_502_BAD_GATEWAY)
        return {"roots": {root: {name or "": workspaceops.room(found, settings.workspaces, root, name)
                                 for name in (None, *settings.workspaces)}
                          for root in settings.storage_roots},
                "forecasts": {name: disk_history.workspace_forecast(name) for name in settings.workspaces},
                # the machine's disk under each storage, and its estimate (Disks page)
                "real_disks": await storage_disks()}

    async def storage_disks() -> dict[str, dict]:
        points = [m[1] for m in await asyncio.to_thread(disks.mounts)]
        found = {}
        for root, folder in settings.storage_roots.items():
            mount = disks.holder(folder, points)
            if mount:
                found[root] = {"mount": mount, "forecast": disk_history.forecast_of(mount)}
        return found

    @app.post("/api/workspaces/save", dependencies=[Depends(admin)])
    async def workspace_save(payload: dict = Body(...), me: Principal = Depends(current_user)):
        """Create (new: true) or change a workspace: its servers and members
        must exist, a server and a domain are in one workspace only."""
        name = str(payload.get("name") or "").strip()
        new = bool(payload.get("new"))
        async with workspaces_lock:
            if new and name in settings.workspaces:
                return refuse(f"A workspace named {name} already exists")
            if not new and name not in settings.workspaces:
                return refuse(f"No workspace {name}", status.HTTP_404_NOT_FOUND)
            data = {key: payload.get(key) for key in ("label", "domain", "storage", "limits", "servers", "members",
                                                       "parameter_bounds")}
            before = settings.workspaces.get(name)
            if before is not None:
                # the names are the workspace's admins' (its page)
                data.update(main=before.main, subdomains=before.subdomains, certificate=before.certificate)
            try:
                found = parse_workspaces({**{n: ws.to_dict() for n, ws in settings.workspaces.items()}, name: data},
                                         settings.storage_roots)
            except ConfigError as exc:
                return refuse(str(exc))
            ws = found[name]
            if ws.storage and ws.storage not in settings.storage_roots:
                return refuse(f"storage: {ws.storage} is not one of workspaces.storage of the main configuration "
                              f"({', '.join(settings.storage_roots) or 'none'}): pick one of them")
            unknown = [s for s in ws.servers if s not in settings.servers]
            if unknown:
                return refuse(f"No server {', '.join(unknown)}")
            strangers = [u for u in ws.members if u not in settings.users]
            if strangers:
                return refuse(f"No user {', '.join(strangers)}")
            if ws.storage and ws.limits.get("disk_gb") and (
                    before is None or before.storage != ws.storage
                    or before.limits.get("disk_gb") != ws.limits.get("disk_gb")):
                try:
                    room = await workspaceops.space()
                except workspaceops.SpaceError:
                    room = None
                problem = workspaceops.disk_problem(room, found, ws, bool(payload.get("force_disk")))
                if problem:
                    return refuse(f"disk_gb: {problem}")
            refused = await store_workspaces(found)
            if refused:
                return refused
        if before is None or not ws.instances():
            return {"ok": True}
        names = before.domain != ws.domain
        resources = any(before.limits.get(k) != ws.limits.get(k) for k in ("cpus", "memory_gb"))
        if not (names or resources):
            return {"ok": True}

        async def action(job):
            current = settings.workspaces.get(ws.name)
            if current is None:
                raise JobFailed(f"no workspace {ws.name}")
            if resources:
                await workspaceops.apply_limits(job, current)
            if names:
                job.result = await workspaceops.publish(job, current)
        job = jobs.submit(f"Workspace {ws.name}: " + ", ".join(
            [w for w, on in (("CPU and memory", resources), ("names", names)) if on]),
            [f"{s}/{r}" for s in ws.servers for r in ROLES], f"manual: {me}", action)
        return {"ok": True, "job": job.id}

    @app.post("/api/workspaces/delete", dependencies=[Depends(admin)])
    async def workspace_delete(payload: dict = Body(...)):
        """Delete a workspace: its servers stay, its users of workspaces only
        no longer see them."""
        name = str(payload.get("name") or "")
        async with workspaces_lock:
            if name not in settings.workspaces:
                return refuse(f"No workspace {name}", status.HTTP_404_NOT_FOUND)
            ws = settings.workspaces[name]
            refused = await store_workspaces({n: w for n, w in settings.workspaces.items() if n != name})
            if refused:
                return refused
        # with no instance of its own left, its disk goes too (an empty one only)
        if ws.storage and not [s for s in ws.servers if s in settings.servers and s.startswith(f"{name}-")]:
            return {"ok": True, "disk": await workspaceops.drop_disk(None, ws)}
        return {"ok": True}

    def a_workspace(name: str) -> Workspace:
        found = settings.workspaces.get(name)
        if found is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No workspace {name}")
        return found

    @app.get("/workspaces/{workspace}", dependencies=[Depends(admin)])
    async def workspace_page(request: Request, workspace: str):
        """The users and the instances of a workspace, for its admins (and the machine's)."""
        ws = a_workspace(workspace)
        me = request.state.user
        members = [{"name": n, "role": r, "only_here": only_here(n, ws.name)} for n, r in sorted(ws.members.items())]
        found = await asyncio.to_thread(workspaceops.templates)
        return _TEMPLATES.TemplateResponse(
            request, "workspace.html",
            {"ws": ws, "members": members, "roles": ACCESS_ROLES, "me": me.name, "min_password": _MIN_PASSWORD,
             "servers": [n for n in ws.servers if n in settings.servers],
             "templates": [t.to_dict() for t in found.values()],
             "can_create": bool(ws.storage and ws.limits.get("disk_gb")) and settings.storage_problem(ws) is None,
             "docker_missing": workspaceops.docker_missing(),
             "storage_problem": settings.storage_problem(ws),
             "instances": [i for i in ws.instances() if workspaceops.server_name(ws, i) in settings.servers],
             "parameters": {n: {role: role_parameters(ws, n[len(ws.name) + 1:], role) for role in ROLES}
                            for n in ws.servers if n in settings.servers and n.startswith(f"{ws.name}-")},
             "repositories": {n: {role: [{"name": r.name, "branch": r.branch} for r in getattr(settings.servers[n], role).repositories]
                                  for role in ROLES if getattr(settings.servers[n], role) is not None}
                              for n in ws.servers if n in settings.servers},
             "sites": site_names(ws), "certificates": CERTIFICATES,
             "tokens": [{"name": n, "role": settings.token_role(n)} for n in sorted(settings.tokens)
                        if settings.token_workspace(n) == ws.name],
             "mcp_url": str(request.base_url).rstrip("/") + "/mcp",
             # the links an archive may be copied through: the machine's admins'
             "links": sorted(settings.link_targets) if me.kind == "user" and not me.workspaces and me.can("admin")
             else [],
             "credentials": await asyncio.to_thread(workspace_credentials, ws.name),
             # how many remote destinations its backups have (their page lists them)
             "destinations_count": len(destination_rows(ws.name)),
             "instances_left": None if not ws.limits.get("instances")
             else int(ws.limits["instances"]) - len([n for n in ws.servers if n in settings.servers])},
        )

    async def make_instance(job, ws: Workspace, instance: str, template, values: dict, setup: dict | None = None):
        """The job of a new instance of ws (its server already in ws.servers):
        made, written in the servers file, its names published; its server
        out of ws again when it fails."""
        name = workspaceops.server_name(ws, instance)
        try:
            data = await workspaceops.create_instance(job, ws, instance, template, values, setup)
            parsed = parse_servers({name: {**data, "status": "active"}}, settings.logs_dir)[name]
            async with config_lock:
                error = await store({**settings.servers, name: parsed})
            if error is not None:
                raise JobFailed(f"the instance runs, but the servers file was not written: {error.body.decode()}")
        except BaseException:
            await rename_in_workspaces(name, None)
            # a New instance's own workspace, left with nothing: it goes, and
            # its disk, so that the next try finds the room and the name free
            current = settings.workspaces.get(ws.name)
            if ws.name in quick_workspaces and current is not None and not current.servers:
                async with workspaces_lock:
                    await store_workspaces({n: w for n, w in settings.workspaces.items() if n != ws.name})
                job.log(f"workspace {ws.name} removed: {await workspaceops.drop_disk(job, current)}")
                quick_workspaces.discard(ws.name)
            raise
        quick_workspaces.discard(ws.name)
        job.result = {"server": name}
        job.log(f"Server {name}: production at http://127.0.0.1:{parsed.production.options.http_port}"
                + (f", test at http://127.0.0.1:{parsed.test.options.http_port}" if parsed.test else ""))
        current = settings.workspaces.get(ws.name)
        if current is not None and current.domain:
            try:
                await workspaceops.publish(job, current)
            except JobFailed as exc:
                job.log(f"The instance runs, its names are not published: {exc}")
        return parsed

    def admin_setup(setup):
        """The admin's password and the company's country of a new database
        ({password, country}, both optional) checked: the values to keep, or
        the refusal."""
        if not isinstance(setup, dict) or set(setup) - {"password", "country"}:
            return refuse("setup takes password and country only")
        password, country = str(setup.get("password") or ""), str(setup.get("country") or "").upper()
        if password and (not 8 <= len(password) <= 128 or any(ord(c) < 32 for c in password)):
            return refuse("The admin's password is 8 to 128 characters")
        if country and not re.fullmatch(r"[A-Z]{2}", country):
            return refuse(f"{country!r} is not a country code, as CH")
        return {k: v for k, v in (("password", password), ("country", country)) if v}

    @app.post("/api/workspaces/{workspace}/instances/create", dependencies=[Depends(admin)])
    async def instance_create(workspace: str, payload: dict = Body(...), me: Principal = Depends(current_user)):
        """A new instance of the workspace, production and test, from a
        template: a job (the disk, the projects, the databases); then the
        server, of type docker, is the workspace's."""
        ws = a_workspace(workspace)
        if workspaceops.docker_missing():
            return refuse(workspaceops.docker_missing())
        instance = str(payload.get("instance") or "").strip()
        if not WORKSPACE_NAME.fullmatch(instance):
            return refuse("An instance's name is 1-32 lower case letters, digits and -")
        if instance == TEST_LABEL or instance in ws.subdomains:
            return refuse(f"{instance} is {'the name of the test sites' if instance == TEST_LABEL else 'a subdomain'} "
                          "of the workspace: an instance needs another name")
        name = workspaceops.server_name(ws, instance)
        template = (await asyncio.to_thread(workspaceops.templates)).get(str(payload.get("template") or ""))
        if template is None:
            return refuse(f"No template {payload.get('template')!r}")
        try:
            values = template.values({str(k): str(v) for k, v in (payload.get("values") or {}).items()})
        except stacktemplates.TemplateError as exc:
            return refuse(str(exc))
        setup = admin_setup(payload.get("setup") or {})
        if not isinstance(setup, dict):
            return setup
        if not ws.storage or not ws.limits.get("disk_gb"):
            return refuse(f"Workspace {ws.name} has no storage or no disk limit: the machine's admins set them")
        if settings.storage_problem(ws):
            return refuse(f"Workspace {ws.name} cannot be used: {settings.storage_problem(ws)}")
        async with workspaces_lock:
            ws = a_workspace(workspace)
            if name in settings.servers or name in ws.servers:
                return refuse(f"A server named {name} exists already")
            mine = [n for n in ws.servers if n in settings.servers]
            if ws.limits.get("instances") and len(mine) >= ws.limits["instances"]:
                return refuse(f"Workspace {ws.name} has its {int(ws.limits['instances'])} instances already")
            # the server is the workspace's from now: its job is seen there
            refused = await store_workspaces({**settings.workspaces,
                                              ws.name: dataclasses.replace(ws, servers=[*ws.servers, name])})
            if refused:
                return refused

        async def action(job):
            await make_instance(job, ws, instance, template, values, setup or None)

        job = jobs.submit(f"Create {name} from {template.name}", [f"{name}/production", f"{name}/test"],
                          f"manual: {me}", action, workspace=ws.name)
        return {"job": job.id}

    # --- a new instance from a full archive (instancearchive.py) --------------

    def uploaded_archives(ws: Workspace) -> Path:
        """Where the archives sent to a workspace wait (700, 600 each)."""
        return settings.backup_dir / ".archives" / ws.name

    def workspace_archives(ws: Workspace) -> list[dict]:
        """The full archives a new instance of ws may be made from: those sent
        to it, and those in the backups of its servers."""
        found = []
        places = [("upload", uploaded_archives(ws))] + [
            (f"{n}/{role}", settings.backup_dir / n / role) for n in ws.servers if n in settings.servers
            for role in ROLES if getattr(settings.servers[n], role) is not None]
        for source, folder in places:
            if not folder.is_dir():
                continue
            for path in sorted(folder.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True):
                if instancearchive.PREFIX not in path.name:
                    continue
                manifest = instancearchive.read_manifest(path)
                if manifest is None:
                    continue
                found.append({"source": source, "file": path.name, "id": f"{source}/{path.name}",
                              "size": path.stat().st_size, "made": manifest.get("made"),
                              "of": f"{manifest.get('server')}/{manifest.get('role')}", "odoo": manifest.get("odoo"),
                              "type": manifest.get("type")})
        return found

    def archive_path(ws: Workspace, ident: str) -> Path | None:
        """The archive an id of workspace_archives names (nothing else)."""
        for row in workspace_archives(ws):
            if row["id"] == ident:
                folder = uploaded_archives(ws) if row["source"] == "upload" else \
                    settings.backup_dir / row["source"]
                return folder / row["file"]
        return None

    @app.get("/api/workspaces/{workspace}/archives", dependencies=[Depends(admin)])
    async def archives_list(workspace: str):
        return {"archives": await asyncio.to_thread(workspace_archives, a_workspace(workspace))}

    @app.post("/api/workspaces/{workspace}/archives/upload", dependencies=[Depends(admin)])
    async def archive_upload(request: Request, workspace: str, name: str = "", me: Principal = Depends(current_user)):
        """A full archive sent by the browser (the raw body), kept for the
        workspace's New instance from an archive: whole, and an archive, or
        nothing."""
        ws = a_workspace(workspace)
        if me.kind != "user":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Only a user who logs in here may send an archive")
        name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(name).name)
        if not linkcopy.BACKUP_NAME.fullmatch(name):
            return refuse("The file must be a .zip")
        if instancearchive.PREFIX not in name:
            name = instancearchive.PREFIX + name
        folder = private.backup_folder(uploaded_archives(ws))
        dest = folder / name
        if dest.exists():
            return refuse(f"{name} was sent already: choose it from the list", status.HTTP_409_CONFLICT)
        length = request.headers.get("content-length", "")
        free = shutil.disk_usage(folder).free
        if length.isdigit() and free - int(length) < puller.MIN_FREE:
            return refuse(f"{name}: {free >> 20} MiB free here, not enough for {int(length) >> 20} MiB",
                          status.HTTP_507_INSUFFICIENT_STORAGE)
        handle, part_name = tempfile.mkstemp(dir=folder, prefix=f".{name}.", suffix=".part")
        part = Path(part_name)
        try:
            with os.fdopen(handle, "wb") as out:
                async for chunk in request.stream():
                    out.write(chunk)
            if instancearchive.read_manifest(part) is None:
                return refuse(f"{name} is not a full archive of OmniaCLI (no manifest.json of one)")
            os.replace(part, dest)
            os.chmod(dest, 0o600)
        except ClientDisconnect:
            return refuse(f"{name}: the upload was interrupted")
        finally:
            part.unlink(missing_ok=True)
        return {"id": f"upload/{name}"}

    def link_of_machine_admin(me: Principal, name: str) -> LinkTarget:
        """A link of the machine, for its admins only: a workspace's admins do
        not reach the other OmniaCLI this one is linked to."""
        if me.kind != "user" or me.workspaces or not me.can("admin"):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Only an admin of the machine copies through its links")
        link = settings.link_targets.get(name)
        if link is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No link {name}")
        return link

    @app.get("/api/workspaces/{workspace}/archives/remote", dependencies=[Depends(admin)])
    async def archives_remote(workspace: str, link: str, me: Principal = Depends(current_user)):
        """The full archives in the backups of the servers of a linked OmniaCLI."""
        a_workspace(workspace)
        target = link_of_machine_admin(me, link)
        client, headers = link_clients.client(target), link_headers(target, me.name, me.role)
        try:
            resp = await client.get("/api/servers", headers=headers, timeout=LIST_TIMEOUT)
            if resp.status_code != 200:
                return refuse(f"{link} answered {resp.status_code}", status.HTTP_502_BAD_GATEWAY)
            servers = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            return refuse(f"{link}: {describe_error(exc)}", status.HTTP_502_BAD_GATEWAY)

        async def backups(server: str, role: str) -> list[dict]:
            try:
                got = await client.get(f"/api/servers/{server}/{role}/backups", headers=headers, timeout=LIST_TIMEOUT)
                rows = got.json() if got.status_code == 200 else []
            except (httpx.HTTPError, ValueError):
                return []
            return [{"server": server, "role": role, "file": r["file"], "size": r.get("size"), "time": r.get("time")}
                    for r in rows if isinstance(r, dict) and (r.get("archive") or instancearchive.PREFIX in str(r.get("file")))]
        pairs = [(srv["name"], role) for srv in servers if isinstance(srv, dict)
                 for role in ("production", "test") if role in (srv.get("instances") or {})]
        found = await asyncio.gather(*(backups(s, r) for s, r in pairs))
        return {"archives": [row for rows in found for row in rows]}

    @app.post("/api/workspaces/{workspace}/archives/fetch", dependencies=[Depends(admin)])
    async def archive_fetch(workspace: str, payload: dict = Body(...), me: Principal = Depends(current_user)):
        """A full archive of a linked OmniaCLI copied among the workspace's
        archives (a job): then a New instance from it."""
        ws = a_workspace(workspace)
        target = link_of_machine_admin(me, str(payload.get("link") or ""))
        server, role, file = (str(payload.get(k) or "") for k in ("server", "role", "file"))
        if not SERVER_NAME.fullmatch(server) or role not in ROLES or not linkcopy.BACKUP_NAME.fullmatch(file):
            return refuse("server, role and file of an archive there")
        headers = link_headers(target, me.name, me.role)
        folder = uploaded_archives(ws)

        async def action(job):
            dest = await linkcopy.fetch_backup(job, link_clients.client(target), target, headers,
                                               LinkSource(target.name, server, role), file, folder,
                                               job.result.setdefault("progress", {}))
            if instancearchive.read_manifest(dest) is None:
                dest.unlink(missing_ok=True)
                raise JobFailed(f"{file} of {target.name} is not a full archive")
            job.log(f"{dest.name} is among the archives of workspace {ws.name}: New instance › From a full archive")

        job = jobs.submit(f"Copy the archive {file} from {target.name}", [], f"manual: {me}", action, workspace=ws.name)
        return {"job": job.id}

    async def repository_from_archive(job, ws: Workspace, instance: str, role: str, row: dict, archive: Path,
                                      options: tuple) -> Path:
        """A repository of the archive in the role's addons, at the archive's
        commit: cloned and reset to it; when its address cannot be reached or
        no longer has that commit, made from the archive's code (a git
        repository of one commit, its address kept: a later pull fetches)."""
        target = workspaceops.addons_folder(ws, instance, role) / row["name"]
        try:
            await workspaceops.clone(job, ws, instance, role, row["name"], row["url"], row["branch"], options)
            code, out = await ops.capture(["git", "cat-file", "-e", f"{row['commit']}^{{commit}}"], cwd=target)
            if code:
                raise JobFailed(f"{row['url']} no longer has the commit {row['commit'][:10]}")
            await ops.run(job, ["git", "reset", "--hard", row["commit"]], cwd=target)
            await ops.run(job, ["git", "submodule", "update", "--init", "--recursive"], cwd=target, check=False)
            return target
        except JobFailed as exc:
            if not row.get("code"):
                raise
            job.log(f"{row['name']}: {exc}; made from the archive's code instead")
            workspaceops.remove_clone(target)
        count = await asyncio.to_thread(instancearchive.extract_code, archive, row["code"], target)
        for cmd in (["git", "init", "-q", "-b", row["branch"]], ["git", "add", "-A"],
                    ["git", "-c", "user.name=OmniaCLI", "-c", "user.email=omniacli@localhost", "commit", "-q",
                     "-m", f"{row['name']} at {row['commit']}, from the archive {archive.name}"],
                    ["git", "remote", "add", "origin", row["url"]]):
            if cmd[-1]:
                await ops.run(job, cmd, cwd=target)
        job.log(f"{row['name']}: {count} files of the commit {row['commit'][:10]}, from the archive")
        return target

    async def instance_from_archive(job, ws: Workspace, instance: str, template, archive: Path, neutralize: bool):
        name = workspaceops.server_name(ws, instance)
        job.log(f"--- the archive {archive.name}: every part against its manifest")
        bad = await asyncio.to_thread(instancearchive.check_parts, archive)
        if bad:
            # the name kept for it is free again
            await rename_in_workspaces(name, None)
            raise JobFailed(f"{archive.name}: {', '.join(bad)} not as its manifest says: nothing made")
        values = template.values({})
        await make_instance(job, ws, instance, template, values, None)
        server = settings.servers[name]
        roles = [role for role in ROLES if getattr(server, role) is not None]
        rows = [r for r in instancearchive.read_json(archive, "repositories.json") or []
                if isinstance(r, dict) and r.get("name") and r.get("commit")]
        options = gitcredentials.options_for(settings, name)
        made = {role: [] for role in roles}
        for row in rows:
            for role in roles:
                path = await repository_from_archive(job, ws, instance, role, row, archive, options)
                made[role].append({"name": row["name"], "path": str(path), "branch": row["branch"], "remote": "origin"})
        state = instancearchive.read_json(archive, "config/omniacli.json") or {}
        spec = {"repositories": [r["name"] for r in rows], "bounds": ws.parameter_bounds}
        for key in ("parameters", "debug", "extra"):
            if key in state:
                spec[key] = state[key]
        for role in roles:
            try:
                await workspaceops._helper(job, "conf", ws.name, ws.storage, instance, role, json.dumps(spec))
            except JobFailed as exc:
                # the bounds here may be narrower than there: the repositories at least
                job.log(f"WARNING: the parameters of the archive are not taken ({exc}): the template's defaults")
                await workspaceops.write_conf(job, ws, instance, role, spec["repositories"])
        done = await set_repositories(name, made) if rows else server
        for role in roles:
            current = getattr(done, role)
            pip = getattr(current.plugin, "pip_requirements", None)
            for row in rows:
                if pip is not None:
                    await pip(job, current, row["name"])
        if instancearchive.read_part(archive, "config/pip-freeze.txt"):
            job.log("The Python packages the archive's instance had are in its config/pip-freeze.txt: the "
                    "repositories' requirements.txt are installed, not that list")
        production = done.production
        job.log(f"--- the database of the archive into {production.key}" + (" (neutralized)" if neutralize else ""))
        with instancearchive.backup_of(archive) as inner:
            await restore_fetched(job, production, inner, {"neutralize": neutralize})
        for role in roles:
            if role != "production":
                await ops.restart(job, getattr(done, role))
        job.result = {"server": name}
        job.log(f"{name} is made from {archive.name}")

    @app.post("/api/workspaces/{workspace}/instances/from-archive", dependencies=[Depends(admin)])
    async def instance_create_from_archive(workspace: str, payload: dict = Body(...),
                                           me: Principal = Depends(current_user)):
        """A new instance of the workspace from a full archive: its template,
        its repositories at their commits, its parameters, its database (in
        production; neutralized unless asked). A job."""
        ws = a_workspace(workspace)
        instance = str(payload.get("instance") or "").strip()
        if not WORKSPACE_NAME.fullmatch(instance):
            return refuse("An instance's name is 1-32 lower case letters, digits and -")
        if instance == TEST_LABEL or instance in ws.subdomains:
            return refuse(f"{instance} is a name the workspace's sites use: an instance needs another name")
        archive = await asyncio.to_thread(archive_path, ws, str(payload.get("archive") or ""))
        if archive is None:
            return refuse("No such archive for this workspace", status.HTTP_404_NOT_FOUND)
        manifest = instancearchive.read_manifest(archive) or {}
        if manifest.get("type") != "docker":
            return refuse(f"{archive.name} is an archive of a {manifest.get('type')} instance: a workspace's "
                          "instance is made from one of Odoo in Docker Compose")
        made_from = instancearchive.read_json(archive, "config/instance.json") or {}
        template = (await asyncio.to_thread(workspaceops.templates)).get(str(made_from.get("template") or ""))
        if template is None:
            return refuse(f"{archive.name} was made from the template {made_from.get('template')!r}, "
                          "not installed here")
        name = workspaceops.server_name(ws, instance)
        if not ws.storage or not ws.limits.get("disk_gb"):
            return refuse(f"Workspace {ws.name} has no storage or no disk limit: the machine's admins set them")
        if settings.storage_problem(ws):
            return refuse(f"Workspace {ws.name} cannot be used: {settings.storage_problem(ws)}")
        async with workspaces_lock:
            ws = a_workspace(workspace)
            if name in settings.servers or name in ws.servers:
                return refuse(f"A server named {name} exists already")
            mine = [n for n in ws.servers if n in settings.servers]
            if ws.limits.get("instances") and len(mine) >= ws.limits["instances"]:
                return refuse(f"Workspace {ws.name} has its {int(ws.limits['instances'])} instances already")
            refused = await store_workspaces({**settings.workspaces,
                                              ws.name: dataclasses.replace(ws, servers=[*ws.servers, name])})
            if refused:
                return refused
        neutralize = bool(payload.get("neutralize", True))
        job = jobs.submit(f"Create {name} from the archive {archive.name}", [f"{name}/production", f"{name}/test"],
                          f"manual: {me}", lambda job: instance_from_archive(job, ws, instance, template, archive,
                                                                          neutralize),
                          workspace=ws.name)
        return {"job": job.id}

    # --- a new Odoo by its version only, as a hosting panel's Add instance ------

    QUICK_INSTANCE = "odoo"
    # the workspaces New instance made, until their instance is made: one
    # whose creation fails is taken away with its disk (make_instance)
    quick_workspaces: set[str] = set()
    QUICK_DISK_GB = (10, 200)

    @app.get("/api/instances/versions", dependencies=[Depends(admin)])
    async def quick_versions():
        """The Odoo versions a new instance may have: the templates odoo-<major> here."""
        found = await asyncio.to_thread(workspaceops.templates)
        majors = sorted((int(n.split("-", 1)[1]) for n in found if re.fullmatch(r"odoo-\d+", n)), reverse=True)
        return {"versions": [{"major": m, "label": found[f"odoo-{m}"].label} for m in majors],
                "storage": bool(settings.storage_roots), "address": await asyncio.to_thread(public_address)}

    @app.post("/api/instances/new", dependencies=[Depends(admin)])
    async def quick_instance(payload: dict = Body(...), me: Principal = Depends(current_user)):
        """A new Odoo given its version (and, if wanted, a name, a domain and a
        language): a workspace of its own on the first storage, with a disk as
        large as the real disk allows (at most QUICK_DISK_GB), then its
        production and test from the template odoo-<version>; with a domain
        its production answers there and its test on test.<domain>."""
        major = str(payload.get("version") or "").strip()
        template = f"odoo-{major}"
        if template not in await asyncio.to_thread(workspaceops.templates):
            return refuse(f"No Odoo {major} here (no template {template})")
        if not settings.storage_roots:
            return refuse(f"No storage for workspaces on this machine: {workspaceops.HELPERS_HINT}")
        if workspaceops.docker_missing():
            return refuse(workspaceops.docker_missing())
        given = re.sub(r"[^a-z0-9-]+", "-", str(payload.get("name") or "").strip().lower()).strip("-")[:32]
        name = given or f"odoo{major}"
        if not WORKSPACE_NAME.fullmatch(name):
            return refuse("A name is 1-32 lower case letters, digits and -")
        if given and name in settings.workspaces:
            return refuse(f"A workspace named {name} already exists")
        base, number = name, 2
        while name in settings.workspaces:
            name, number = f"{base[:29]}-{number}", number + 1
        domain = str(payload.get("domain") or "").strip().lower().rstrip(".")
        setup = admin_setup({"password": payload.get("password"), "country": payload.get("country")})
        if not isinstance(setup, dict):
            return setup
        storage = next(iter(settings.storage_roots))
        try:
            room = workspaceops.room(await workspaceops.space(), settings.workspaces, storage, None)
        except workspaceops.SpaceError as exc:
            return refuse(f"The room of the disks cannot be read: {exc}", status.HTTP_502_BAD_GATEWAY)
        allowed = int((room or {}).get("allowed", 0) * 0.9 / workspaceops.GB)
        if allowed < QUICK_DISK_GB[0]:
            return refuse(f"Not enough room on the disk for a new instance: {allowed} GB, "
                          f"{QUICK_DISK_GB[0]} GB at least")
        data = {"label": str(payload.get("label") or name).strip()[:100], "domain": domain, "storage": storage,
                "limits": {"disk_gb": min(allowed, QUICK_DISK_GB[1])}, "servers": [], "members": {}}
        async with workspaces_lock:
            try:
                found = parse_workspaces({**{n: ws.to_dict() for n, ws in settings.workspaces.items()}, name: data},
                                         settings.storage_roots)
            except ConfigError as exc:
                return refuse(str(exc))
            refused = await store_workspaces(found)
            if refused:
                return refused
        quick_workspaces.add(name)
        language = str(payload.get("language") or "")
        made = await instance_create(name, {"instance": QUICK_INSTANCE, "template": template,
                                            "values": {"INIT_LANG": language} if language else {},
                                            "setup": setup}, me)
        if not isinstance(made, dict):
            quick_workspaces.discard(name)
            async with workspaces_lock:
                await store_workspaces({n: ws for n, ws in settings.workspaces.items() if n != name})
            return made
        # its production on the domain itself, its test on test.<domain>
        async with workspaces_lock:
            ws = settings.workspaces.get(name)
            if ws is not None:
                await store_workspaces({**settings.workspaces, name: dataclasses.replace(ws, main=QUICK_INSTANCE)})
        return {**made, "workspace": name, "server": workspaceops.server_name(found[name], QUICK_INSTANCE)}

    # --- stagings: dated copies of a production beside it (stagings.py) -----

    stagings_file = (settings.links_file or settings.servers_file).parent / stagings.FILE

    def workspace_of(server: str) -> tuple[Workspace, str] | None:
        """The workspace that made server, and its instance name there."""
        for ws in settings.workspaces.values():
            if server in ws.servers and server.startswith(f"{ws.name}-"):
                return ws, server[len(ws.name) + 1:]
        return None

    def staging_rows(server: str) -> list[dict]:
        """The stagings of server, the newest first, with what the page shows."""
        rows = []
        for name, info in stagings.load(stagings_file).items():
            srv = settings.servers.get(name)
            if info.get("parent") == server and srv is not None and srv.production is not None:
                rows.append({"server": name, "created": info.get("refreshed") or info.get("created", ""),
                             "backup": info.get("backup", ""),
                             "url": srv.production.web_url, "port": srv.production.plugin.web_port(srv.production),
                             "busy": server_busy(name)})
        return sorted(rows, key=lambda r: r["created"], reverse=True)

    async def staging_states(rows: list[dict]) -> list[dict]:
        """rows of staging_rows with the run state of each one's production."""
        found = [settings.servers[r["server"]].production for r in rows]
        states = await asyncio.gather(*(run_state(i) for i in found))
        return [{**r, "state": st} for r, st in zip(rows, states)]

    @app.get("/api/servers/{server}/stagings", dependencies=[Depends(admin)])
    async def staging_list(server: str):
        return {"stagings": staging_rows(server), "can": workspace_of(server) is not None,
                "parent": stagings.load(stagings_file).get(server, {}).get("parent")}

    @app.post("/api/servers/{server}/stagings", dependencies=[Depends(admin)])
    async def staging_create(server: str, me: Principal = Depends(current_user)):
        """A new staging of server's production: an instance beside it in its
        workspace, from the same template, its production loaded with a
        backup of server's production made now, neutralized, its test
        stopped; recorded with its parent and its date."""
        return await new_staging(server, me)

    async def new_staging(server: str, me: Principal, load=None, title: str = "", extra: dict | None = None):
        """A staging of server beside it; load(job, parent production, the
        staging's production) fills it (default: a backup made now, restored
        neutralized); extra goes in its record (stagings.py)."""
        srv = settings.servers.get(server)
        if srv is None or srv.production is None:
            return refuse(f"No server {server} with a production", status.HTTP_404_NOT_FOUND)
        found = workspace_of(server)
        if found is None:
            return refuse(f"{server} was not made in a workspace: a staging is made beside an instance of a workspace")
        ws, instance = found
        template = await asyncio.to_thread(instance_template, ws, instance)
        if template is None:
            return refuse(f"The template {server} was made from is not on this machine any more")
        if server_busy(server):
            return refuse(f"{server} is busy: wait for its job")
        now = datetime.datetime.now()
        async with workspaces_lock:
            ws = settings.workspaces[ws.name]
            taken = {n[len(ws.name) + 1:] for n in ws.servers}
            new_instance = stagings.instance_name(instance, now, taken)
            name = workspaceops.server_name(ws, new_instance)
            mine = [n for n in ws.servers if n in settings.servers]
            if ws.limits.get("instances") and len(mine) >= ws.limits["instances"]:
                return refuse(f"Workspace {ws.name} has its {int(ws.limits['instances'])} instances already")
            refused = await store_workspaces({**settings.workspaces,
                                              ws.name: dataclasses.replace(ws, servers=[*ws.servers, name])})
            if refused:
                return refused

        async def backup_and_restore(job, prod, target) -> dict:
            backup_zip = await ops.backup_into(job, prod, backup_folder(prod), {"no_filestore": False})
            await target.plugin.restore(job, target, backup_zip, {"neutralize": True})
            return {"backup": backup_zip.name}

        async def action(job):
            # its mail caught in its project, read in its Mail tab (UX-008 §5.3)
            made = await make_instance(job, settings.workspaces[ws.name], new_instance, template,
                                       {workspacehelper.MAIL_KEY: True})
            prod = settings.servers[server].production
            target = settings.servers[name].production
            record = await (load or backup_and_restore)(job, prod, target)
            if settings.servers[name].test is not None:
                job.log(f"--- {name}: its test stopped, a staging needs its production only")
                await ops.stop(job, settings.servers[name].test)
            data = stagings.load(stagings_file)
            data[name] = {"parent": server, "created": now.isoformat(timespec="seconds"), **record, **(extra or {})}
            await asyncio.to_thread(stagings.save, stagings_file, data)
            job.result = {"server": name, "port": made.production.options.http_port}

        job = jobs.submit(title or f"Staging {name} of {server}",
                          [f"{server}/production", f"{name}/production", f"{name}/test"],
                          f"manual: {me}", action, workspace=ws.name)
        return {"job": job.id, "server": name}

    @app.post("/api/servers/{server}/stagings/refresh", dependencies=[Depends(admin)])
    async def staging_refresh(server: str, me: Principal = Depends(current_user)):
        """A staging made again from its production as it is now: a new
        backup of the parent's production restored into the staging's,
        neutralized; its code stays as it is (what is being tried there)."""
        info = stagings.load(stagings_file).get(server) or {}
        parent = info.get("parent")
        srv, parent_srv = settings.servers.get(server), settings.servers.get(parent or "")
        if srv is None or srv.production is None or parent_srv is None or parent_srv.production is None:
            return refuse(f"{server} is not a staging of a production here", status.HTTP_404_NOT_FOUND)
        if not srv.production.plugin.has_restore or not parent_srv.production.plugin.has_backup:
            return refuse(f"{server} cannot be loaded from {parent}")
        if server_busy(server) or server_busy(parent):
            return refuse("A job is running on the staging or its production: wait for it")

        async def action(job):
            prod, target = settings.servers[parent].production, settings.servers[server].production
            job.log(f"--- a new backup of {prod.key}")
            backup_zip = await ops.backup_into(job, prod, backup_folder(prod), {"no_filestore": False})
            await target.plugin.restore(job, target, backup_zip, {"neutralize": True})
            data = stagings.load(stagings_file)
            data[server] = {**data.get(server, {}), "parent": parent, "backup": backup_zip.name,
                            "refreshed": datetime.datetime.now().isoformat(timespec="seconds")}
            await asyncio.to_thread(stagings.save, stagings_file, data)
            job.log(f"{server} has the data of {prod.key} of now, neutralized; its code is as it was")

        ws = workspace_of(server)
        job = jobs.submit(f"Refresh the staging {server} from {parent}", [f"{parent}/production", f"{server}/production"],
                          f"manual: {me}", action, workspace=ws[0].name if ws else "")
        return {"job": job.id}

    # --- a staging's mail, caught in its project and read here (stagingmail.py, UX-008 §5.3)

    def staging_mail(server: str):
        """The production of a staging of a workspace whose mail is caught
        (its instance.json says so: a staging made since the templates have
        the service), or None."""
        srv, found = settings.servers.get(server), workspace_of(server)
        if srv is None or srv.production is None or found is None or srv.production.restart.type != "compose" \
                or server not in stagings.load(stagings_file):
            return None
        ws, instance = found
        try:
            meta = json.loads((workspaceops.storage_path(ws) / ws.name / instance / workspacehelper.INSTANCE_FILE)
                              .read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return srv.production if isinstance(meta, dict) and workspacehelper.captures_mail(meta, "production") else None

    def no_mail(server: str):
        return refuse(f"{server} catches no mail: it is no staging, or one made before the mail was caught "
                      "(its outgoing mail is off)", status.HTTP_404_NOT_FOUND)

    @app.get("/api/servers/{server}/production/mail")
    async def staging_mail_list(server: str, every: bool = False, me: Principal = Depends(anyone)):
        """The mail Odoo of the staging sent, caught: the newest first, the
        first 50 or every one (?every=1)."""
        instance = await asyncio.to_thread(staging_mail, server)
        if instance is None:
            return no_mail(server)
        try:
            found = await stagingmail.messages(instance, every)
        except JobFailed as exc:
            return refuse(str(exc), status.HTTP_502_BAD_GATEWAY)
        return {**found, "first": stagingmail.FIRST, "can_clear": me.can("admin")}

    @app.get("/api/servers/{server}/production/mail/{mail_id}")
    async def staging_mail_get(server: str, mail_id: str, me: Principal = Depends(anyone)):
        """One caught message as it was: who, to whom, when, its bodies and
        the names of its attachments (not its raw headers)."""
        instance = await asyncio.to_thread(staging_mail, server)
        if instance is None:
            return no_mail(server)
        try:
            return await stagingmail.message(instance, mail_id)
        except LookupError as exc:
            return refuse(str(exc), status.HTTP_404_NOT_FOUND)
        except JobFailed as exc:
            return refuse(str(exc), status.HTTP_502_BAD_GATEWAY)

    @app.post("/api/servers/{server}/production/mail/clear")
    async def staging_mail_clear(request: Request, server: str, me: Principal = Depends(admin)):
        """Every caught message of the staging deleted (the workspace's admin):
        Odoo and its queue of mail are not touched."""
        instance = await asyncio.to_thread(staging_mail, server)
        if instance is None:
            return no_mail(server)
        try:
            await stagingmail.clear(instance)
        except JobFailed as exc:
            return refuse(str(exc), status.HTTP_502_BAD_GATEWAY)
        access_log.record("action", me.kind, me.name, request.client.host if request.client else "",
                          f"caught mail of {instance.key} deleted")
        return {"ok": True}

    # --- point-in-time recovery of a workspace instance's database (pitrhelper.py)

    pitr_file = settings.servers_file.parent / "pitr.json"
    PITR_RESTORE_TIMEOUT = 6 * 3600

    def pitr_target(server: str):
        """(workspace, instance name there, production) of a workspace's
        instance; a refusal otherwise."""
        srv = settings.servers.get(server)
        found = workspace_of(server)
        if srv is None or srv.production is None or found is None or srv.production.restart.type != "compose":
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                f"{server} is not an instance of a workspace: point-in-time recovery is for those")
        return found[0], found[1], srv.production

    def pitr_record(server: str, **values) -> None:
        data = deployhooks.load(pitr_file)
        if values.get("enabled") is False:
            data.pop(server, None)
        else:
            data[server] = {**data.get(server, {}), **values}
        deployhooks.save(pitr_file, data)

    @app.get("/api/servers/{server}/pitr", dependencies=[Depends(developer)])
    async def pitr_state(server: str):
        """Whether it is on, the base backups, the first and last minute a restore can reach."""
        ws, instance, _ = pitr_target(server)
        try:
            found = await workspaceops.ask("pitr-state", ws.name, ws.storage, instance, "production")
        except JobFailed as exc:
            return refuse(str(exc), status.HTTP_502_BAD_GATEWAY)
        # this machine's time: the page tells a range that ended long ago from one that is still growing
        return {**found, "now": datetime.datetime.now().isoformat(timespec="seconds")}

    @app.post("/api/servers/{server}/pitr/enable", dependencies=[Depends(admin)])
    async def pitr_enable(server: str, me: Principal = Depends(current_user)):
        """Point-in-time recovery switched on: the database archives its WAL
        (a volume of its own, its service made again: a few seconds without
        database), a first base backup; then one each night."""
        ws, instance, prod = pitr_target(server)

        async def action(job):
            found = await workspaceops._helper(job, "pitr-enable", ws.name, ws.storage, instance, "production",
                                               timeout=PITR_RESTORE_TIMEOUT)
            await asyncio.to_thread(pitr_record, server, enabled=True,
                                    base=datetime.datetime.now().isoformat(timespec="seconds"))
            job.log(f"Point-in-time recovery on for {prod.key}: base backup {found.get('base')}")
            job.result = found
        job = jobs.submit(f"Point-in-time recovery on for {prod.key}", [prod.key], f"manual: {me}", action,
                          workspace=ws.name)
        return {"job": job.id}

    async def pitr_base_job(job, ws: Workspace, instance: str, server: str) -> None:
        found = await workspaceops._helper(job, "pitr-base", ws.name, ws.storage, instance, "production",
                                           timeout=PITR_RESTORE_TIMEOUT)
        await asyncio.to_thread(pitr_record, server, base=datetime.datetime.now().isoformat(timespec="seconds"))
        job.log(f"Base backup {found.get('base')} ({found.get('size', 0) >> 20} MiB)")

    PITR_EVERY = 3600
    PITR_BASE_AGE = 24 * 3600

    async def pitr_loop() -> None:
        """Each hour: a base backup of each instance whose last one is a day old."""
        while True:
            await asyncio.sleep(PITR_EVERY)
            try:
                for server, info in (await asyncio.to_thread(deployhooks.load, pitr_file)).items():
                    found = workspace_of(server)
                    srv = settings.servers.get(server)
                    if found is None or srv is None or srv.production is None or jobs.is_busy(srv.production.key):
                        continue
                    try:
                        last = datetime.datetime.fromisoformat(str(info.get("base") or "")).timestamp()
                    except ValueError:
                        last = 0
                    if time.time() - last >= PITR_BASE_AGE:
                        ws, instance = found
                        jobs.submit(f"Base backup for point-in-time recovery of {srv.production.key}",
                                    [srv.production.key], "point-in-time recovery",
                                    lambda job, ws=ws, instance=instance, server=server:
                                    pitr_base_job(job, ws, instance, server), workspace=ws.name)
            except Exception:  # noqa: BLE001 - the loop must survive
                _logger.exception("point-in-time recovery: the nightly base backups failed")

    pitr_tasks: list[asyncio.Task] = []

    async def stop_pitr() -> None:
        for task in pitr_tasks:
            task.cancel()
        await asyncio.gather(*pitr_tasks, return_exceptions=True)
        pitr_tasks.clear()

    @app.post("/api/servers/{server}/pitr/base", dependencies=[Depends(admin)])
    async def pitr_base(server: str, me: Principal = Depends(current_user)):
        ws, instance, prod = pitr_target(server)
        job = jobs.submit(f"Base backup for point-in-time recovery of {prod.key}", [prod.key], f"manual: {me}",
                          lambda job: pitr_base_job(job, ws, instance, server), workspace=ws.name)
        return {"job": job.id}

    @app.post("/api/servers/{server}/pitr/restore", dependencies=[Depends(admin)])
    async def pitr_restore(server: str, payload: dict = Body(...), me: Principal = Depends(current_user)):
        """A new staging with the database of the production as it was at a
        minute: its cluster made again from the archive, the filestore of
        now linked (Odoo never changes a file there, only adds), neutralized."""
        ws, instance, prod = pitr_target(server)
        when = str(payload.get("time") or "").strip()
        try:
            at = datetime.datetime.fromisoformat(when)
        except ValueError:
            return refuse(f"{when!r} is not a time, as 2026-11-20T09:41")

        async def load(job, parent, target) -> dict:
            target_instance = target.server[len(ws.name) + 1:]
            job.log(f"--- the database of {parent.key} as it was at {at:%Y-%m-%d %H:%M}")
            found = await workspaceops._helper(job, "pitr-restore", ws.name, ws.storage, instance, target_instance,
                                               at.isoformat(timespec="minutes"), parent.options.database,
                                               target.options.database, timeout=PITR_RESTORE_TIMEOUT)
            job.log(f"Replayed from the base backup {found.get('base')} with {found.get('segments')} WAL segments")
            await ops.run(job, target.restart.command("link-filestore", target.options.odoo_service,
                                                      target.options.database, parent.restart.service,
                                                      parent.options.database), timeout=3600)
            # neutralized as every staging: no outgoing mail, no scheduled actions
            await ops.run(job, target.restart.command("neutralize", target.options.odoo_service,
                                                      target.options.database), timeout=3600)
            await ops.restart(job, target)
            return {"pitr": at.isoformat(timespec="minutes")}

        return await new_staging(server, me, load, title=f"Staging of {server} at {at:%Y-%m-%d %H:%M}")

    @app.post("/api/stagings/mark", dependencies=[Depends(admin)])
    async def staging_mark(payload: dict = Body(...)):
        """Record a server as a staging of another one (a hosting panel's
        staging, found by Add a server): shown under its parent with the
        Staging badge; created: when it was copied, if known."""
        name, parent = str(payload.get("name") or ""), str(payload.get("parent") or "")
        created = str(payload.get("created") or "")
        if name not in settings.servers or parent not in settings.servers or name == parent:
            return refuse("name and parent: two servers of this OmniaCLI")
        if created and not re.fullmatch(r"\d{4}-\d{2}-\d{2}(T[\d:]{5,8})?", created):
            return refuse(f"{created!r} is not a date, as 2026-11-19")
        data = stagings.load(stagings_file)
        data[name] = {**data.get(name, {}), "parent": parent, "created": created}
        await asyncio.to_thread(stagings.save, stagings_file, data)
        return {"ok": True}

    @app.post("/api/workspaces/{workspace}/instances/delete", dependencies=[Depends(admin)])
    async def instance_delete(workspace: str, payload: dict = Body(...), me: Principal = Depends(current_user)):
        """An instance of the workspace removed: its containers, data and
        folders, then its server."""
        ws = a_workspace(workspace)
        name = str(payload.get("server") or "")
        prefix = f"{ws.name}-"
        if name not in ws.servers or not name.startswith(prefix) or name not in settings.servers:
            return refuse(f"No instance {name} in workspace {ws.name}", status.HTTP_404_NOT_FOUND)
        if server_busy(name):
            return refuse(f"A job is running on {name}: try again when it ends")
        instance = name[len(prefix):]

        async def action(job):
            await workspaceops.remove_instance(job, ws, instance)
            async with config_lock:
                error = await store({k: v for k, v in settings.servers.items() if k != name})
            if error is not None:
                raise JobFailed(f"the instance is removed, but the servers file was not written: {error.body.decode()}")
            await rename_in_workspaces(name, None)
            job.log(f"Server {name} removed")
            # a staging is no longer one; the stagings of a removed parent stay, saying whose copy they were
            known = stagings.load(stagings_file)
            if known.pop(name, None) is not None:
                await asyncio.to_thread(stagings.save, stagings_file, known)
            current = settings.workspaces.get(ws.name)
            if current is not None:
                try:
                    await workspaceops.publish(job, current)
                except JobFailed as exc:
                    job.log(f"The names of the workspace are not published again: {exc}")

        job = jobs.submit(f"Remove {name}", [f"{name}/production", f"{name}/test"], f"manual: {me}", action,
                          workspace=ws.name)
        return {"job": job.id}

    @app.post("/api/workspaces/{workspace}/web/save", dependencies=[Depends(admin)])
    async def web_save(workspace: str, payload: dict = Body(...), me: Principal = Depends(current_user)):
        """The names of the workspace (its admins): the main instance, the
        subdomains, where the certificate comes from; then a job publishes them."""
        async with workspaces_lock:
            ws = a_workspace(workspace)
            data = {**ws.to_dict(), "main": payload.get("main") or "",
                    "subdomains": payload.get("subdomains") or {}, "certificate": payload.get("certificate")}
            try:
                found = parse_workspaces({**{n: w.to_dict() for n, w in settings.workspaces.items()}, workspace: data},
                                         settings.storage_roots)
            except ConfigError as exc:
                return refuse(str(exc))
            ws = found[workspace]
            refused = await store_workspaces(found)
            if refused:
                return refused
        if not ws.domain or not ws.instances():
            return {"ok": True}
        return {"ok": True, "job": submit_publish(ws, f"manual: {me}").id}

    @app.post("/api/workspaces/{workspace}/web/publish", dependencies=[Depends(admin)])
    async def web_publish(workspace: str, me: Principal = Depends(current_user)):
        """The names published again (after the DNS records were made, a certificate failed)."""
        ws = a_workspace(workspace)
        if not ws.storage:
            return refuse(f"Workspace {ws.name} has no storage: the machine's admins set it")
        return {"job": submit_publish(ws, f"manual: {me}").id}

    @app.post("/api/workspaces/{workspace}/web/certificate", dependencies=[Depends(admin)])
    async def web_certificate(workspace: str, payload: dict = Body(...), me: Principal = Depends(current_user)):
        """The customer's own certificate (PEM, with its chain) and key; the
        names are then published with it."""
        ws = a_workspace(workspace)
        if ws.certificate != "own":
            return refuse("The workspace takes its certificate from Let's Encrypt: choose its own certificate first")
        ok, out = await workspaceops.own_certificate(ws, str(payload.get("certificate") or ""),
                                                     str(payload.get("key") or ""))
        if not ok:
            return refuse(out.removeprefix("workspace-helper: "))
        try:
            made = json.loads(out.splitlines()[-1])
        except (ValueError, IndexError):
            made = {}
        return {"ok": True, **made, "job": submit_publish(ws, f"manual: {me}").id}

    def workspace_instance(ws: Workspace, payload: dict):
        """(server name, instance) of a request on an instance of ws; a response when refused."""
        name = str(payload.get("server") or "")
        prefix = f"{ws.name}-"
        if name not in ws.servers or not name.startswith(prefix) or name not in settings.servers:
            return refuse(f"No instance {name} in workspace {ws.name}", status.HTTP_404_NOT_FOUND)
        # a production at least: the instances made now have no test
        if settings.servers[name].production is None:
            return refuse(f"{name} has no production")
        if server_busy(name):
            return refuse(f"A job is running on {name}: try again when it ends")
        return name, name[len(prefix):]

    def repository_rows(repos) -> list[dict]:
        return [{"name": r.name, "path": str(r.path), "branch": r.branch, "remote": r.remote} for r in repos]

    async def set_repositories(name: str, repositories: dict[str, list[dict]]):
        """The repositories of the roles in the servers file; the server as it is then."""
        async with config_lock:
            data = server_to_dict(settings.servers[name])
            for role, repos in repositories.items():
                data[role]["repositories"] = repos
            try:
                parsed = parse_servers({name: data}, settings.logs_dir)[name]
            except ConfigError as exc:
                raise JobFailed(str(exc)) from None
            error = await store({**settings.servers, name: parsed})
        if error is not None:
            raise JobFailed(f"the servers file was not written: {error.body.decode()}")
        return settings.servers[name]

    @app.post("/api/workspaces/{workspace}/repositories/add", dependencies=[Depends(admin)])
    async def repository_add(workspace: str, payload: dict = Body(...), me: Principal = Depends(current_user)):
        """A customer's repository (https, the workspace's git credentials)
        cloned into production and test of an instance, each on its branch
        (a test branch is merged into production's as for every server),
        named in their odoo.conf, both restarted: its modules can then be
        installed from Odoo's Apps."""
        ws = a_workspace(workspace)
        found = workspace_instance(ws, payload)
        if not isinstance(found, tuple):
            return found
        name, instance = found
        server = settings.servers[name]
        # the roles it has: production, and its test when it has one
        roles = [role for role in ROLES if getattr(server, role) is not None]
        url = str(payload.get("url") or "").strip()
        # no name given: the repository's own (…/OCA/web.git → web)
        repo = str(payload.get("name") or "").strip() or re.split(r"[/:]", url.rstrip("/"))[-1].removesuffix(".git")
        given = str(payload.get("branch") or payload.get("production_branch") or "").strip()
        branches = {role: str(payload.get(f"{role}_branch") or "").strip() or given for role in roles}
        try:
            for branch in branches.values():
                workspaceops.check_repository(repo, url, branch)
        except JobFailed as exc:
            return refuse(str(exc))
        if any(r.name == repo for role in roles for r in getattr(server, role).repositories):
            return refuse(f"{name} has a repository {repo} already")

        async def action(job):
            options = gitcredentials.options_for(settings, name)
            current = {role: list(getattr(server, role).repositories) for role in roles}
            cloned = {}
            try:
                for role in roles:
                    cloned[role] = await workspaceops.clone(job, ws, instance, role, repo, url, branches[role], options)
                for role in roles:
                    await workspaceops.write_conf(job, ws, instance, role, [*(r.name for r in current[role]), repo])
                done = await set_repositories(name, {role: [*repository_rows(current[role]), {
                    "name": repo, "path": str(cloned[role]), "branch": branches[role], "remote": "origin"}]
                    for role in roles})
            except BaseException:
                for role in roles:
                    await workspaceops.write_conf(job, ws, instance, role, [r.name for r in current[role]])
                    if role in cloned:
                        workspaceops.remove_clone(cloned[role])
                raise
            for role in roles:
                # its Python packages before Odoo starts with its modules
                pip = getattr(getattr(done, role).plugin, "pip_requirements", None)
                if pip is not None:
                    await pip(job, getattr(done, role), repo)
                await ops.restart(job, getattr(done, role))
            job.log(f"{repo} is in {name}: install its modules from Odoo's Apps")
            job.result = {"server": name, "repository": repo}

        job = jobs.submit(f"Add repository {repo} to {name}", [f"{name}/{role}" for role in roles],
                          f"manual: {me}", action)
        return {"job": job.id}

    @app.post("/api/workspaces/{workspace}/git-key", dependencies=[Depends(admin)])
    async def workspace_git_key(workspace: str):
        """OmniaCLI's git key for the repositories reached over ssh, made the
        first time: its public half, to add as a read-only deploy key."""
        a_workspace(workspace)
        if settings.credentials_file is None:
            return refuse("No credentials file in this configuration: the git key has no place")
        key = gitcredentials.git_key(settings.credentials_file)
        try:
            public = await sshlinks.ensure_key(key.parent, key.name)
        except (sshlinks.SshError, OSError) as exc:
            return refuse(str(exc), 500)
        # the repositories of the workspaces' servers use it from now
        await asyncio.to_thread(gitcredentials.bind, settings)
        return {"ok": True, "public_key": public}

    @app.post("/api/workspaces/{workspace}/repositories/remove", dependencies=[Depends(admin)])
    async def repository_remove(workspace: str, payload: dict = Body(...), me: Principal = Depends(current_user)):
        """A customer's repository out of an instance: out of the odoo.conf
        and of the server, both roles restarted, the clones deleted."""
        ws = a_workspace(workspace)
        found = workspace_instance(ws, payload)
        if not isinstance(found, tuple):
            return found
        name, instance = found
        repo = str(payload.get("name") or "")
        server = settings.servers[name]
        roles = [role for role in ROLES if getattr(server, role) is not None]
        if not any(r.name == repo for role in roles for r in getattr(server, role).repositories):
            return refuse(f"{name} has no repository {repo}", status.HTTP_404_NOT_FOUND)

        async def action(job):
            kept = {role: [r for r in getattr(server, role).repositories if r.name != repo] for role in roles}
            for role in roles:
                await workspaceops.write_conf(job, ws, instance, role, [r.name for r in kept[role]])
            done = await set_repositories(name, {role: repository_rows(kept[role]) for role in roles})
            for role in roles:
                await ops.restart(job, getattr(done, role))
                workspaceops.remove_clone(workspaceops.addons_folder(ws, instance, role) / repo)
            job.log(f"{repo} removed from {name}")

        job = jobs.submit(f"Remove repository {repo} from {name}", [f"{name}/{role}" for role in roles],
                          f"manual: {me}", action)
        return {"job": job.id}

    def workspace_repository(ws: Workspace, payload: dict):
        """(server name, its instance of the role asked, the repository) of
        the Update dialog, or a refusal."""
        found = workspace_instance(ws, payload)
        if not isinstance(found, tuple):
            return found
        name = found[0]
        role = str(payload.get("role") or "production")
        inst = getattr(settings.servers[name], role, None) if role in ROLES else None
        if inst is None:
            return refuse(f"{name} has no {role}", status.HTTP_404_NOT_FOUND)
        repo = next((r for r in inst.repositories if r.name == str(payload.get("name") or "")), None)
        if repo is None:
            return refuse(f"{name} has no repository {payload.get('name')}", status.HTTP_404_NOT_FOUND)
        return name, inst, repo

    @app.post("/api/workspaces/{workspace}/repositories/branches", dependencies=[Depends(developer)])
    async def repository_branches(workspace: str, payload: dict = Body(...)):
        """The Update dialog of a repository: the commit checked out, the
        branches of its remote with their last commit, the page of the
        differences for each."""
        found = workspace_repository(a_workspace(workspace), payload)
        if not isinstance(found, tuple):
            return found
        _, _, repo = found
        try:
            data = await workspaceops.repository_branches(repo)
        except JobFailed as exc:
            return refuse(str(exc))
        for branch in data["branches"]:
            branch["compare"] = workspaceops.compare_url(data["url"], data["commit"], branch["commit"])
        return data

    @app.post("/api/workspaces/{workspace}/repositories/update", dependencies=[Depends(developer)])
    async def repository_update(workspace: str, payload: dict = Body(...), me: Principal = Depends(current_user)):
        """A repository of an instance on the last commit of the branch
        chosen (its own: a pull; another: checked out and kept in the
        servers file), its Python packages, the changed modules updated
        when the instance says so, restarted."""
        found = workspace_repository(a_workspace(workspace), payload)
        if not isinstance(found, tuple):
            return found
        name, inst, repo = found
        branch = str(payload.get("branch") or "").strip() or repo.branch
        try:
            workspaceops.check_branch(branch)
        except JobFailed as exc:
            return refuse(str(exc))

        async def action(job):
            previous = repo.branch
            await ops.backup_before_update(job, inst, backup_folder(inst))
            await workspaceops.update_repository(job, repo, branch)
            current = inst
            if branch != previous:
                try:
                    done = await set_repositories(name, {inst.role: [
                        {**row, "branch": branch} if row["name"] == repo.name else row
                        for row in repository_rows(inst.repositories)]})
                except BaseException:
                    # the servers file still names the old branch: the copy goes back to it
                    await ops.run(job, repo.git("checkout", previous), cwd=repo.path, check=False)
                    raise
                current = getattr(done, inst.role)
                job.log(f"{repo.name} follows {branch} now")
            await current.plugin.after_pull(job, current)
            await ops.restart(job, current)
            job.result = {"server": name, "repository": repo.name, "branch": branch}

        what = f"Update {repo.name} of {inst.key}" + (f" to {branch}" if branch != repo.branch else "")
        job = jobs.submit(what, [inst.key], f"manual: {me}", action)
        return {"job": job.id}

    def instance_template(ws: Workspace, instance: str):
        """The template an instance was made from (the higher version installed), or None."""
        try:
            meta = json.loads((workspaceops.storage_path(ws) / ws.name / instance / workspacehelper.INSTANCE_FILE).read_text())
        except (OSError, ValueError):
            return None
        return workspaceops.templates().get(str(meta.get("template") or ""))

    def role_parameters(ws: Workspace, instance: str, role: str) -> dict | None:
        """What the page shows of a role: each parameter with its value (a
        password: whether it is set), the debug switch."""
        template = instance_template(ws, instance)
        if template is None or not template.parameters:
            return None
        state = workspacehelper.role_state(workspaceops.storage_path(ws) / ws.name / instance / role)
        rows = []
        for param in template.parameters:
            value = state["parameters"].get(param.name, param.default)
            bounds = ws.parameter_bounds.get(param.name) or [param.min, param.max]
            rows.append({**param.to_dict(), "min": bounds[0], "max": bounds[1],
                         "value": "" if param.type == "password" else value,
                         "set": bool(value) if param.type == "password" else None})
        return {"parameters": rows, "debug": state["debug"], "extra": state["extra"]}

    @app.post("/api/workspaces/{workspace}/parameters/options", dependencies=[Depends(admin)])
    async def parameters_options(workspace: str, payload: dict = Body(...)):
        """The Extended view of the Odoo config of a role of an instance:
        every option the Odoo of its container knows (config_options.py,
        in the running service), the other options saved, those it does not
        set (the template's, the layout's, the parameters)."""
        ws = a_workspace(workspace)
        name = str(payload.get("server") or "")
        role = str(payload.get("role") or "production")
        if name not in ws.servers or not name.startswith(f"{ws.name}-") or name not in settings.servers:
            return refuse(f"No instance {name} in workspace {ws.name}", status.HTTP_404_NOT_FOUND)
        instance = getattr(settings.servers[name], role, None) if role in ROLES else None
        if instance is None:
            return refuse(f"{name} has no {role}")
        template = await asyncio.to_thread(instance_template, ws, name[len(ws.name) + 1:])
        if template is None:
            return refuse(f"{name} was not made from a template installed here")
        code, out = await ops.capture(instance.restart.command("options", template.odoo_service), timeout=120)
        found = stacktemplates.options_answer(code, out)
        if "error" in found:
            return JSONResponse({"error": "Its Odoo does not answer (is it running?): " + found["error"]},
                                status_code=502)
        project = workspaceops.storage_path(ws) / ws.name / name[len(ws.name) + 1:] / role
        state = workspacehelper.role_state(project)
        locked = template.locked_options()
        # the values its odoo.conf has now: what a greyed option is set to (a secret's never)
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(project / "config" / "odoo.conf", encoding="utf-8")
            written = dict(parser.items("options")) if parser.has_section("options") else {}
        except (configparser.Error, OSError, UnicodeDecodeError):
            written = {}
        options = [o for o in found.get("options") or [] if o.get("name") not in stacktemplates.COMMAND_LINE_ONLY]
        return {"options": options, "version": found.get("version"),
                "values": {k: ("" if stacktemplates.SECRET_OPTION.search(k) else v) for k, v in written.items()
                           if k in locked},
                "extra": {k: ("" if stacktemplates.SECRET_OPTION.search(k) else v) for k, v in state["extra"].items()},
                "secrets": sorted(k for k in state["extra"] if stacktemplates.SECRET_OPTION.search(k)),
                "locked": sorted(locked)}

    @app.post("/api/workspaces/{workspace}/parameters/save", dependencies=[Depends(admin)])
    async def parameters_save(workspace: str, payload: dict = Body(...), me: Principal = Depends(current_user)):
        """The editable Odoo parameters and the debug log of a role of an
        instance: checked against the template's bounds or the workspace's,
        and workers x limit_memory_hard against the workspace's memory; the
        helper writes its odoo.conf again, the role is restarted (and the
        workspace's names published again: the websocket follows workers)."""
        ws = a_workspace(workspace)
        found = workspace_instance(ws, payload)
        if not isinstance(found, tuple):
            return found
        name, instance = found
        role = str(payload.get("role") or "")
        if role not in ROLES:
            return refuse(f"The role is {' or '.join(ROLES)}")
        template = await asyncio.to_thread(instance_template, ws, instance)
        if template is None:
            return refuse(f"{name} was not made from a template installed here")
        given = {str(k): v for k, v in (payload.get("parameters") or {}).items()}
        saved = workspacehelper.role_state(workspaceops.storage_path(ws) / ws.name / instance / role)["parameters"]
        for param in template.parameters:
            # a password left empty keeps the one saved
            if param.type == "password" and not str(given.get(param.name) or "") and saved.get(param.name):
                given[param.name] = saved[param.name]
        bounds = {k: tuple(v) for k, v in ws.parameter_bounds.items()}
        try:
            values = template.parameter_values(given, bounds)
        except stacktemplates.TemplateError as exc:
            return refuse(str(exc))
        memory = ws.limits.get("memory_gb")
        workers, hard = values.get("workers") or 0, values.get("limit_memory_hard") or 0
        if memory and workers and workers * hard > memory * 1024:
            return refuse(f"{workers} workers x {hard} MB is more than the workspace's {memory:g} GB: the kernel "
                          "would kill Odoo instead of Odoo stopping one request")
        debug = bool(payload.get("debug"))
        spec = {"parameters": given, "debug": debug, "bounds": ws.parameter_bounds}
        if "extra" in payload:
            # the other options (the Extended view), the whole set: a secret
            # left empty keeps the one saved
            extra = payload.get("extra")
            if isinstance(extra, dict):
                saved_extra = workspacehelper.role_state(
                    workspaceops.storage_path(ws) / ws.name / instance / role)["extra"]
                for key in [k for k, v in extra.items() if stacktemplates.SECRET_OPTION.search(str(k)) and not str(v or "").strip()]:
                    if key in saved_extra:
                        extra[key] = saved_extra[key]
            try:
                spec["extra"] = template.extra_options(extra)
            except stacktemplates.TemplateError as exc:
                return refuse(str(exc))

        async def action(job):
            await workspaceops._helper(job, "conf", ws.name, ws.storage, instance, role, json.dumps(spec))
            await ops.restart(job, getattr(settings.servers[name], role))
            current = settings.workspaces.get(ws.name)
            if current is not None and current.domain:
                try:
                    await workspaceops.publish(job, current)
                except JobFailed as exc:
                    job.log(f"The names of the workspace are not published again: {exc}")
            if not workers and values.get("limit_time_cpu"):
                job.log("Note: with workers = 0 Odoo does not apply limit_time_cpu (only with workers)")

        job = jobs.submit(f"Odoo parameters of {name}/{role}", [f"{name}/{role}"], f"manual: {me}", action)
        return {"job": job.id}

    def only_here(user: str, workspace: str) -> bool:
        """user is a user of workspaces only, and of this one only: its
        workspace's admins may change its password and delete it."""
        return (settings.user_role(user) == WORKSPACE_ROLE and user not in settings.fixed.users
                and [ws for ws, _ in settings.memberships(user)] == [workspace])

    @app.post("/api/workspaces/{workspace}/members/save", dependencies=[Depends(admin)])
    async def member_save(workspace: str, payload: dict = Body(...), me: Principal = Depends(current_user)):
        """A workspace's admin creates a user of it (new: true, with a
        password: a user of workspaces only), changes a member's role, and
        the password of a member of this workspace only."""
        name, role = str(payload.get("name") or "").strip(), str(payload.get("role") or "")
        password, new = str(payload.get("password") or ""), bool(payload.get("new"))
        if role not in ACCESS_ROLES:
            return refuse(f"The role must be one of {', '.join(ACCESS_ROLES)}")
        async with access_lock, workspaces_lock:
            ws = a_workspace(workspace)
            if new:
                refused = check_name("User", name, settings.fixed.users, settings.users, True)
                if refused:
                    return refused
                if len(password) < _MIN_PASSWORD:
                    return refuse(f"The password needs at least {_MIN_PASSWORD} characters")
            else:
                if name not in ws.members:
                    return refuse(f"{name} is not a user of {workspace}", status.HTTP_404_NOT_FOUND)
                if me.kind == "user" and name == me.name and role != ws.members[name]:
                    return refuse("You cannot change your own role")
                if password and not only_here(name, workspace):
                    return refuse(f"{name} is also a user of other workspaces or of the machine: "
                                  "ask the administrator of the machine for its password")
                if password and len(password) < _MIN_PASSWORD:
                    return refuse(f"The password needs at least {_MIN_PASSWORD} characters")
            if new or password:
                web = web_copy()
                web.users[name] = await asyncio.to_thread(hash_password, password)
                if new:
                    web.roles[name] = WORKSPACE_ROLE
                refused = await store_access(web)
                if refused:
                    return refused
            found = dict(settings.workspaces)
            found[workspace] = dataclasses.replace(ws, members={**ws.members, name: role})
            return await store_workspaces(found) or {"ok": True}

    @app.post("/api/workspaces/{workspace}/members/delete", dependencies=[Depends(admin)])
    async def member_delete(workspace: str, payload: dict = Body(...), me: Principal = Depends(current_user)):
        """Out of the workspace; a user of this workspace only is deleted."""
        name = str(payload.get("name") or "")
        async with access_lock, workspaces_lock:
            ws = a_workspace(workspace)
            if name not in ws.members:
                return refuse(f"{name} is not a user of {workspace}", status.HTTP_404_NOT_FOUND)
            if me.kind == "user" and name == me.name:
                return refuse("You cannot remove yourself")
            delete_user = only_here(name, workspace)
            found = dict(settings.workspaces)
            found[workspace] = dataclasses.replace(ws, members={n: r for n, r in ws.members.items() if n != name})
            refused = await store_workspaces(found)
            if refused:
                return refused
            if delete_user:
                web = web_copy()
                web.users.pop(name, None)
                web.roles.pop(name, None)
                refused = await store_access(web)
                if refused:
                    return refused
            return {"ok": True, "deleted": delete_user}

    # --- users, links and MCP tokens ------------------------------------

    access_lock = asyncio.Lock()

    def web_copy() -> Access:
        web = settings.web
        return Access(dict(web.users), dict(web.roles), dict(web.tokens), dict(web.links), dict(web.token_roles),
                      dict(web.token_workspaces))

    async def store_access(web: Access):
        """Write the users file and make it current; a response on error."""
        try:
            await asyncio.to_thread(save_access, settings.users_file, web)
        except OSError as exc:
            return refuse(f"Cannot write {settings.users_file}: {exc}", 500)
        settings.set_web_access(web)
        authenticator.update(settings.users, settings.tokens, link_hashes())
        return None

    def other_admins(name: str) -> int:
        return sum(1 for n in settings.users if n != name and settings.user_role(n) == "admin")

    def check_name(kind: str, name: str, fixed: dict, current: dict, new: bool):
        """None when name may be created (new) or edited, a response otherwise."""
        if not _ACCESS_NAME.fullmatch(name):
            return refuse("A name is 1-64 letters, digits or . _ @ -")
        if name in fixed:
            return refuse(f"{kind} {name} is set in the main configuration: change it there")
        if new and name in current:
            return refuse(f"A {kind.lower()} named {name} already exists")
        if not new and name not in current:
            return refuse(f"No {kind.lower()} {name}", status.HTTP_404_NOT_FOUND)
        return None

    def check_role(role: str, roles: tuple[str, ...] = ACCESS_ROLES):
        return None if role in roles else refuse(f"The role must be one of {', '.join(roles)}")

    @app.get("/users", dependencies=[Depends(admin)])
    async def users_page(request: Request):
        fixed = settings.fixed
        return _TEMPLATES.TemplateResponse(
            request,
            "users.html",
            {
                "users": [{"name": n, "role": settings.user_role(n), "fixed": n in fixed.users,
                           # where a customer's user gets in
                           "workspaces": sorted(w.name for w in settings.workspaces.values() if n in w.members)}
                          for n in sorted(settings.users)],
                "links": [{"name": n, "role": link.role, "fixed": n in fixed.links}
                          for n, link in sorted(settings.links.items())],
                "tokens": [{"name": n, "role": settings.token_role(n), "fixed": n in fixed.tokens,
                            "workspace": settings.token_workspace(n)}
                           for n in sorted(settings.tokens)],
                "users_file": settings.users_file,
                "roles": ACCESS_ROLES,
                "user_roles": USER_ROLES,
                "min_password": _MIN_PASSWORD,
            },
        )

    @app.get("/help", dependencies=[Depends(developer)])
    async def help_page(request: Request):
        """How to create a link and how to connect an MCP client."""
        # seen through a link, the address the browser knows is the other instance's
        base = "https://<address of this OmniaCLI>" if prefix_of(request) else str(request.base_url).rstrip("/")
        return _TEMPLATES.TemplateResponse(request, "help.html", {"base_url": base})

    @app.get("/plugins", dependencies=[Depends(admin)])
    async def plugins_page(request: Request):
        """The plugins found, those disabled with the reason, and the servers
        whose plugin is missing."""
        found = plugins.registry().found
        count = {}
        for srv in settings.servers.values():
            count[srv.type] = count.get(srv.type, 0) + 1
        missing = [srv for srv in settings.servers.values() if srv.missing_plugin]
        return _TEMPLATES.TemplateResponse(
            request, "plugins.html",
            {"found": found, "count": count, "missing": missing, "api_version": PLUGIN_API,
             "group": ENTRY_POINT_GROUP, "route_errors": route_errors,
             "source": settings.plugin_source,
             "sudoers": pluginstore.sudoers_line(pwd.getpwuid(os.getuid()).pw_name),
             "systemd": bool(os.environ.get("INVOCATION_ID"))},
        )

    def installed_plugins() -> dict[str, str]:
        """{plugin name: version of its package} of the plugins found."""
        return {i.name: i.version for i in plugins.registry().found}

    @app.get("/api/plugins/available", dependencies=[Depends(admin)])
    async def plugins_available():
        """The plugins published for download (signed), with the version loaded here."""
        try:
            found = await pluginstore.available(settings.plugin_source)
        except pluginstore.StoreError as exc:
            return refuse(str(exc), status.HTTP_502_BAD_GATEWAY)
        loaded = installed_plugins()
        return {"index": settings.plugin_source.index,
                "plugins": [{**p, "installed": loaded.get(p["name"])} for p in found]}

    @app.post("/api/plugins/{name}/install")
    async def plugin_install(name: str, user: Principal = Depends(admin)):
        """Download a published plugin, check it, and install it through the
        root helper; OmniaCLI loads it once restarted."""
        try:
            found = {p["name"]: p for p in await pluginstore.available(settings.plugin_source)}
        except pluginstore.StoreError as exc:
            return refuse(str(exc), status.HTTP_502_BAD_GATEWAY)
        plugin = found.get(name)
        if plugin is None:
            return refuse(f"The index publishes no plugin {name}", status.HTTP_404_NOT_FOUND)
        if plugin.get("error"):
            return refuse(f"{name}: {plugin['error']}")

        async def action(job):
            manifest = await pluginstore.download(job, settings.plugin_source, plugin,
                                                  pluginstore.plugins_dir(settings.jobs_dir))
            await ops.run(job, pluginstore.install_command(name), timeout=pluginstore.INSTALL_TIMEOUT)
            job.result = {"plugin": name, "version": manifest["version"], "restart": True}
            job.log(f"{name} {manifest['version']} installed: it is loaded when OmniaCLI restarts "
                    "(Restart OmniaCLI on the Plugins page)")

        job = jobs.submit(f"Install plugin {name} {plugin.get('version', '')}".strip(), ["omniacli/plugins"],
                          f"manual: {user}", action)
        return {"job": job.id}

    @app.post("/api/omniacli/restart")
    async def omniacli_restart(user: Principal = Depends(admin)):
        """Restart OmniaCLI (a plugin installed is loaded): it exits, and
        systemd starts it again (Restart=on-failure)."""
        if not os.environ.get("INVOCATION_ID"):
            return refuse("OmniaCLI is not run by systemd here: restart it yourself")
        running = [j.title for j in jobs.recent(500) if not j.finished_flag]
        if running:
            return refuse(f"Jobs are running ({', '.join(running[:3])}): restart when they end")
        logging.getLogger(__name__).warning("restart asked by %s", user)
        asyncio.get_running_loop().call_later(1.0, os._exit, 75)
        return {"ok": True}

    @app.get("/access", dependencies=[Depends(admin)])
    async def access_page(request: Request, q: str = ""):
        """The access log: logins, failed logins and MCP calls, the newest first."""
        entries = await asyncio.to_thread(access_log.recent, 5000)
        found = [e for e in entries if matches(" ".join(str(v) for v in e.values()), q)]
        return _TEMPLATES.TemplateResponse(
            request, "access.html",
            {"entries": found[:500], "total": len(found), "q": q, "log_file": access_log.path,
             "failed_file": access_log.failed_path},
        )

    @app.post("/api/access/users/save")
    async def user_save(payload: dict = Body(...), me: Principal = Depends(admin)):
        """Create a user (new: true, with a password) or change its role and,
        when given, its password."""
        name, role = str(payload.get("name") or "").strip(), str(payload.get("role") or "")
        password, new = str(payload.get("password") or ""), bool(payload.get("new"))
        async with access_lock:
            web = web_copy()
            refused = (check_name("User", name, settings.fixed.users, settings.users, new)
                       or check_role(role, USER_ROLES))
            if refused:
                return refused
            if (new or password) and len(password) < _MIN_PASSWORD:
                return refuse(f"The password needs at least {_MIN_PASSWORD} characters")
            if not new:
                if me.kind == "user" and name == me.name and role != me.role:
                    return refuse("You cannot change your own role")
                if role != "admin" and settings.user_role(name) == "admin" and not other_admins(name):
                    return refuse(f"{name} is the last admin")
            if password:
                web.users[name] = await asyncio.to_thread(hash_password, password)
            web.roles[name] = role
            return await store_access(web) or {"ok": True}

    @app.post("/api/access/users/delete")
    async def user_delete(payload: dict = Body(...), me: Principal = Depends(admin)):
        name = str(payload.get("name") or "")
        async with access_lock:
            refused = check_name("User", name, settings.fixed.users, settings.users, False)
            if refused:
                return refused
            if me.kind == "user" and name == me.name:
                return refuse("You cannot delete yourself")
            if settings.user_role(name) == "admin" and not other_admins(name):
                return refuse(f"{name} is the last admin")
            web = web_copy()
            del web.users[name]
            web.roles.pop(name, None)
            return await store_access(web) or {"ok": True}

    @app.post("/api/access/links/save", dependencies=[Depends(admin)])
    async def link_save(payload: dict = Body(...)):
        """Create a link (new: true; its token is in the answer, once) or
        change its role."""
        name, role = str(payload.get("name") or "").strip(), str(payload.get("role") or "")
        new = bool(payload.get("new"))
        async with access_lock:
            refused = check_name("Link", name, settings.fixed.links, settings.links, new) or check_role(role)
            if refused:
                return refused
            web = web_copy()
            token = new_token() if new else None
            web.links[name] = LinkAccess(hash_token(token) if token else web.links[name].token, role)
            return await store_access(web) or {"ok": True, "token": token}

    @app.post("/api/access/tokens/save", dependencies=[Depends(admin)])
    async def token_save(payload: dict = Body(...)):
        """Create an MCP token (new: true; it is in the answer, once) or change its role."""
        name = str(payload.get("name") or "").strip()
        role = str(payload.get("role") or TOKEN_DEFAULT_ROLE)
        new = bool(payload.get("new"))
        async with access_lock:
            refused = check_name("Token", name, settings.fixed.tokens, settings.tokens, new) or check_role(role)
            if refused:
                return refused
            web = web_copy()
            token = new_token() if new else None
            if token:
                web.tokens[name] = hash_token(token)
            web.token_roles[name] = role
            return await store_access(web) or {"ok": True, "token": token}

    @app.post("/api/access/{kind}/delete", dependencies=[Depends(admin)])
    async def access_delete(kind: str, payload: dict = Body(...)):
        """Revoke a link or an MCP token."""
        if kind not in ("links", "tokens"):
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No {kind}")
        name = str(payload.get("name") or "")
        label = "Link" if kind == "links" else "Token"
        async with access_lock:
            fixed, current = getattr(settings.fixed, kind), getattr(settings, kind)
            refused = check_name(label, name, fixed, current, False)
            if refused:
                return refused
            web = web_copy()
            del getattr(web, kind)[name]
            if kind == "tokens":
                web.token_roles.pop(name, None)
                web.token_workspaces.pop(name, None)
            return await store_access(web) or {"ok": True}

    @app.post("/api/workspaces/{workspace}/tokens/save", dependencies=[Depends(admin)])
    async def workspace_token_save(workspace: str, payload: dict = Body(...)):
        """An MCP token of the workspace (new: true; it is in the answer, once),
        or its role changed: it sees the workspace's instances only, with its
        role there; never a machine's token."""
        ws = a_workspace(workspace)
        name = str(payload.get("name") or "").strip()
        role = str(payload.get("role") or TOKEN_DEFAULT_ROLE)
        new = bool(payload.get("new"))
        async with access_lock:
            refused = check_name("Token", name, settings.fixed.tokens, settings.tokens, new) or check_role(role)
            if refused:
                return refused
            if not new and settings.token_workspace(name) != ws.name:
                return refuse(f"No token {name} of workspace {ws.name}", status.HTTP_404_NOT_FOUND)
            web = web_copy()
            token = new_token() if new else None
            if token:
                web.tokens[name] = hash_token(token)
            web.token_roles[name] = role
            web.token_workspaces[name] = ws.name
            return await store_access(web) or {"ok": True, "token": token}

    @app.post("/api/workspaces/{workspace}/tokens/delete", dependencies=[Depends(admin)])
    async def workspace_token_delete(workspace: str, payload: dict = Body(...)):
        ws = a_workspace(workspace)
        name = str(payload.get("name") or "")
        async with access_lock:
            if name in settings.fixed.tokens or settings.token_workspace(name) != ws.name:
                return refuse(f"No token {name} of workspace {ws.name}", status.HTTP_404_NOT_FOUND)
            web = web_copy()
            web.tokens.pop(name, None)
            web.token_roles.pop(name, None)
            web.token_workspaces.pop(name, None)
            return await store_access(web) or {"ok": True}

    # --- links to other OmniaCLI instances -------------------------------

    links_lock = asyncio.Lock()

    def a_link(name: str) -> LinkTarget:
        link = settings.link_targets.get(name)
        if link is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No link {name}")
        return link

    async def store_links(links: dict[str, LinkTarget]):
        try:
            await asyncio.to_thread(save_links, settings.links_file, links)
        except OSError as exc:
            return refuse(f"Cannot write {settings.links_file}: {exc}", 500)
        settings.link_targets = links
        ssh_tunnels.sync(links)
        return None

    @app.get("/links", dependencies=[Depends(admin)])
    async def links_page(request: Request):
        public = sshlinks.public_key(ssh_tunnels.folder)
        ssh = {}
        for link in settings.link_targets.values():
            if link.ssh is not None:
                ssh[link.name] = {"status": ssh_tunnels.status(link.name), "fingerprint": sshlinks.fingerprint(
                    link.ssh.host_key), "line": sshlinks.authorized_line(public, link) if public else "",
                    "block": sshlinks.sshd_block(link)}
        return _TEMPLATES.TemplateResponse(
            request, "links.html",
            {"links": list(settings.link_targets.values()), "links_file": settings.links_file,
             "public_key": public, "ssh": ssh},
        )

    @app.post("/api/links/ssh/key", dependencies=[Depends(admin)])
    async def link_ssh_key():
        """OmniaCLI's SSH key for the links through SSH, made the first time."""
        try:
            return {"ok": True, "public_key": await sshlinks.ensure_key(ssh_tunnels.folder)}
        except (sshlinks.SshError, OSError) as exc:
            return refuse(str(exc), 500)

    @app.post("/api/links/ssh/host-key", dependencies=[Depends(admin)])
    async def link_ssh_host_key(payload: dict = Body(...)):
        """The SSH server's key, to be compared with the customer's before saving."""
        port = payload.get("port") or 22
        if isinstance(port, bool) or not str(port).isdigit():
            return refuse(f"{port!r} is not a port")
        try:
            key = await sshlinks.fetch_host_key(str(payload.get("host") or "").strip(), int(port))
        except sshlinks.SshError as exc:
            return refuse(str(exc))
        return {"ok": True, "host_key": key, "fingerprint": sshlinks.fingerprint(key)}

    @app.post("/api/links/save", dependencies=[Depends(admin)])
    async def link_target_save(payload: dict = Body(...)):
        """Create a link (new: true) or change it; a blank token keeps the one saved."""
        name, new = str(payload.get("name") or "").strip(), bool(payload.get("new"))
        async with links_lock:
            if new and name in settings.link_targets:
                return refuse(f"A link named {name} already exists")
            if not new and name not in settings.link_targets:
                return refuse(f"No link {name}", status.HTTP_404_NOT_FOUND)
            data = {k: payload.get(k) for k in ("url", "token", "ca_file", "ssh")}
            if not new and not str(data["token"] or "").strip():
                data["token"] = settings.link_targets[name].token
            try:
                link = parse_link(name, data)
            except ConfigError as exc:
                return refuse(str(exc))
            return await store_links({**settings.link_targets, name: link}) or {"ok": True}

    @app.post("/api/links/rename", dependencies=[Depends(admin)])
    async def link_target_rename(payload: dict = Body(...)):
        """A linked server's name changed (its page, its URLs here): the
        link, the instances whose database comes from it, the publications
        through it; its open tunnels closed (they name it)."""
        name, new_name = str(payload.get("name") or ""), str(payload.get("new_name") or "").strip()
        async with links_lock:
            link = a_link(name)
            if new_name == name:
                return {"ok": True, "name": name}
            if new_name in settings.link_targets:
                return refuse(f"A link named {new_name} already exists")
            try:
                renamed = parse_link(new_name, {"url": link.url, "token": link.token, "ca_file": link.ca_file,
                                                "ssh": link.ssh.to_dict() if link.ssh else None})
            except ConfigError as exc:
                return refuse(str(exc))
            uses = [inst for srv in settings.servers.values() for inst in srv.instances.values()
                    if inst.from_link is not None and not inst.from_link.is_puller and inst.from_link.link == name]
            if any(jobs.is_busy(inst.key) for inst in uses):
                return refuse(f"A job runs on an instance copied from {name}: try again when it ends")
            publications = {k: dataclasses.replace(v, link=new_name) if v.link == name else v
                            for k, v in settings.publications.items()}
            for found in [t for t in tunnels.tunnels.values() if t.link == name]:
                await tunnels.close(found.id, f"{name} renamed {new_name}")
            links = {(new_name if k == name else k): (renamed if k == name else v)
                     for k, v in settings.link_targets.items()}
            failed = await store_links(links)
            if failed:
                return failed
            before = [(inst, inst.from_link) for inst in uses]
            try:
                if publications != settings.publications:
                    await asyncio.to_thread(save_publications, settings.publications_file, publications)
                    settings.publications = publications
                # the instances operated from here through a puller of that link follow it too
                connections = {k: dataclasses.replace(v, link=new_name) if v.link == name else v
                               for k, v in settings.connections.items()}
                if connections != settings.connections:
                    await asyncio.to_thread(save_connections, settings.connections_file, connections)
                    settings.connections = connections
                    consumers.reload()
                if uses:
                    for inst in uses:
                        inst.from_link = dataclasses.replace(inst.from_link, link=new_name)
                    await write_servers(settings.servers)
            except OSError as exc:
                for inst, source in before:
                    inst.from_link = source
                return refuse(f"The link is renamed, but its uses could not be written: {exc}", 500)
            return {"ok": True, "name": new_name}

    @app.post("/api/links/delete", dependencies=[Depends(admin)])
    async def link_target_delete(payload: dict = Body(...)):
        name = str(payload.get("name") or "")
        async with links_lock:
            a_link(name)
            links = {k: v for k, v in settings.link_targets.items() if k != name}
            return await store_links(links) or {"ok": True}

    @app.post("/api/links/{name}/test")
    async def link_target_test(name: str, request: Request, me: Principal = Depends(admin)):
        """Ask the other instance who we are there: its version and our role
        (the Links page asks it of every link when it opens: it changes
        nothing, so it is not in the access log); why not, in the caller's language."""
        found = await probe_link(a_link(name), me.name, me.role)
        if not found["ok"]:
            found["error"] = i18n.tr(request_language(request), found["error"])
        return found

    async def probe_link(link: LinkTarget, who: str, role: str) -> dict:
        """The other instance's /api/info through the link: {"ok", ...}."""
        name = link.name
        tunnel = ssh_tunnels.status(name) if link.ssh is not None else None
        if tunnel is not None and tunnel["state"] != "up":
            return {"ok": False, "tunnel": tunnel["state"],
                    "error": f"SSH tunnel {tunnel['state']}: {tunnel['detail'] or 'starting'}"}
        try:
            resp = await link_clients.client(link).get(
                "/api/info", headers=link_headers(link, who, role), timeout=LIST_TIMEOUT)
        except httpx.HTTPError as exc:
            return {"ok": False, "error": describe_error(exc)}
        if resp.status_code == 401:
            return {"ok": False, "error": "the other instance refused the token"}
        try:
            info = resp.json() if resp.status_code == 200 else None
        except ValueError:
            info = None
        if not info or info.get("app") != "OmniaCLI":
            return {"ok": False, "error": f"no OmniaCLI with links there (HTTP {resp.status_code})"}
        return {"ok": True, **info}

    # --- machines prepared from here (machines.py) ------------------------

    @app.get("/machines", dependencies=[Depends(admin)])
    async def machines_page(request: Request):
        # the provisioning key, made the first time the page is opened: one step less
        try:
            provision_key = await sshlinks.ensure_key(ssh_tunnels.folder, sshlinks.PROVISION_KEY_NAME)
        except (sshlinks.SshError, OSError):
            provision_key = None
        return _TEMPLATES.TemplateResponse(request, "machines.html", {
            "provision_key": provision_key,
            "package_url": machines.PACKAGE_URL, "link_user": machines.LINK_USER,
            "machines": machine_cards(),
        })

    def machine_cards() -> list[dict]:
        """The machines linked from here (their link's user is LINK_USER) and
        the ones being prepared, each with its add job when there is one: the
        last of that name, running, or failed in the last three days."""
        cards: dict[str, dict] = {}
        for link in settings.link_targets.values():
            if link.ssh is not None and link.ssh.user == machines.LINK_USER:
                tunnel = ssh_tunnels.status(link.name) or {}
                cards[link.name] = {"name": link.name, "host": link.ssh.host, "port": link.ssh.port, "linked": True,
                                    "state": tunnel.get("state") or "down", "detail": tunnel.get("detail") or ""}
        recent = (datetime.datetime.now() - datetime.timedelta(days=3)).isoformat(timespec="seconds")
        seen: set[str] = set()
        for job in jobs.recent(len(jobs.jobs)):
            found = machines.JOB_TITLE.fullmatch(job.title)
            if found is None or found[1] in seen:
                continue
            seen.add(found[1])
            if job.status == SUCCESS or (job.finished_flag and job.created < recent):
                continue
            card = cards.setdefault(found[1], {"name": found[1], "host": found[2], "port": None, "linked": False,
                                               "state": "", "detail": ""})
            card["job"] = {"id": job.id, "status": job.status, "error": job.error, "created": job.created,
                           "step": machines.progress(job.lines), "steps": len(machines.STEPS)}
        return sorted(cards.values(), key=lambda c: c["name"])

    @app.get("/api/machines", dependencies=[Depends(admin)])
    async def machines_json():
        return {"machines": machine_cards()}

    @app.post("/api/machines/key", dependencies=[Depends(admin)])
    async def machines_key():
        """The key that prepares a machine as its root, made the first time."""
        try:
            return {"ok": True, "public_key": await sshlinks.ensure_key(ssh_tunnels.folder,
                                                                      sshlinks.PROVISION_KEY_NAME)}
        except (sshlinks.SshError, OSError) as exc:
            return refuse(str(exc), 500)

    async def mark_stagings(job, client, headers: dict, names: list[str]) -> None:
        """The servers there whose names say they are a hosting panel's
        stagings (stagings.parent_by_name) recorded under their production."""
        for name in names:
            staging = stagings.parent_by_name(str(name), [str(n) for n in names])
            if staging is None:
                continue
            parent, created = staging
            try:
                resp = await client.post("/api/stagings/mark", headers=headers, timeout=30,
                                         json={"name": name, "parent": parent, "created": created})
                said = f"HTTP {resp.status_code}" if resp.status_code != 200 else ""
            except httpx.HTTPError as exc:
                said = describe_error(exc)
            job.log(f"{name}: not marked as a staging of {parent} ({said})" if said else f"{name}: a staging of {parent}")

    async def add_found_instances(job, link: LinkTarget, me: Principal) -> None:
        """A machine that already runs Odoo: what Discover finds there, saved
        there as the productions of its servers and activated (Test &
        activate), through the link; a check that fails is said, the server
        stays a draft there to complete."""
        job.log("--- the instances in OmniaCLI")
        client, headers = link_clients.client(link), link_headers(link, me.name, me.role)
        try:
            resp = await client.get("/api/servers/discover", headers=headers, timeout=120)
            data = resp.json() if resp.status_code == 200 else {}
        except (httpx.HTTPError, ValueError) as exc:
            raise JobFailed(f"Discover there: {describe_error(exc) if isinstance(exc, httpx.HTTPError) else exc}")
        found = data.get("found") or []
        for item in found:
            if item.get("error"):
                job.log(f"Discover there ({item.get('type', '?')}): {item['error']}")
            elif item.get("skip") or item.get("known"):
                job.log(f"{item.get('label', '?')}: left out ({item.get('skip') or 'already there as ' + item['known']})")
            elif machines.left_out(item):
                job.log(f"{item.get('label', '?')}: left out ({machines.left_out(item)})")
        items = machines.instances_to_add(found, set(data.get("names") or []))
        if not items:
            # the machine was added for its Odoo: none is a failure, said so;
            # added again, those OmniaCLI has there already are enough
            if any(item.get("known") for item in found):
                await mark_stagings(job, client, headers, list(data.get("names") or []))
                return
            raise JobFailed("linked, but no Odoo instance was added there: the lines above say why")
        try:
            resp = await client.post("/api/servers/discover", json={"items": items}, headers=headers, timeout=60)
            saved = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise JobFailed(f"the instances cannot be saved there: {exc}")
        if not saved.get("ok"):
            raise JobFailed(f"the instances cannot be saved there: {saved.get('error')}")
        await mark_stagings(job, client, headers, [item["name"] for item in items] + list(data.get("names") or []))
        for item in items:
            name = item["name"]
            try:
                resp = await client.post(f"/api/servers/{name}/test", headers=headers, timeout=300)
                report = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                job.log(f"{name}: Test & activate not run ({exc}): it stays a draft there")
                continue
            if report.get("ok"):
                job.log(f"{name}: active")
                continue
            failed = [c for c in report.get("checks") or [] if not c.get("ok") and c.get("level") != "warning"]
            job.log(f"{name}: a draft there, to complete: " +
                    "; ".join(f"{c.get('name')}: {c.get('detail')}" for c in failed))

    @app.post("/api/machines/add")
    async def machines_add(request: Request, payload: dict = Body(...), me: Principal = Depends(admin)):
        """A job: the machine prepared through SSH (provision.sh), then linked
        here through SSH, its OmniaCLI trusting this one as admin."""
        if me.kind != "user" or me.scoped:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Only an admin of this machine who logs in here")
        try:
            machine = machines.Machine.parse(payload, machines.here_name(socket.gethostname()))
        except machines.MachineError as exc:
            return refuse(str(exc))
        # one link for a machine: adding it again would give its OmniaCLI a new
        # token for this one, and the link it has here (whatever its name,
        # perhaps renamed) would be refused
        same = next((link.name for link in settings.link_targets.values() if link.ssh is not None
                     and (link.ssh.host.lower(), link.ssh.port) == (machine.host.lower(), machine.port)), None)
        if same is not None:
            return refuse(f"{machine.host} is already linked here as {same}: use that link, rename it on its "
                          "page, or delete it first to prepare the machine again")
        if not str(payload.get("name") or "").strip():
            # the name from the address: another machine's with the same one gets -2, -3...
            base, number = machine.name, 2
            while machine.name in settings.link_targets:
                machine = dataclasses.replace(machine, name=f"{base[:29]}-{number}")
                number += 1
        if machine.name in settings.link_targets:
            return refuse(f"A link named {machine.name} already exists")
        if not machine.host_key:
            # as ssh does on a first connection; given under the advanced options, only that one
            try:
                machine = dataclasses.replace(
                    machine, host_key=await sshlinks.fetch_host_key(machine.host, machine.port))
            except sshlinks.SshError as exc:
                return refuse(str(exc))
        try:
            await machines.check_access(machine, ssh_tunnels.folder)
            link_public = await sshlinks.ensure_key(ssh_tunnels.folder)
        except machines.MachineError as exc:
            return refuse(str(exc))
        except (sshlinks.SshError, OSError) as exc:
            return refuse(str(exc), 500)
        token = new_token()

        async def action(job):
            await machines.provision(job, machine, ssh_folder=ssh_tunnels.folder, link_public_key=link_public,
                                     token_hash=hash_token(token))
            job.log(f"--- the link {machine.name}, through SSH as {machines.LINK_USER}")
            async with links_lock:
                if machine.name in settings.link_targets:
                    raise JobFailed(f"a link named {machine.name} was made meanwhile: not replaced")
                failed = await store_links({**settings.link_targets, machine.name: machines.link_of(machine, token)})
                if failed is not None:
                    raise JobFailed(f"the link cannot be saved: {failed.body.decode(errors='replace')}")
            found = {"ok": False, "error": "not tried"}
            for _ in range(machines.LINK_WAIT):
                await asyncio.sleep(1)
                found = await probe_link(settings.link_targets[machine.name], me.name, me.role)
                if found.get("ok"):
                    break
            if not found.get("ok"):
                raise JobFailed(f"the machine is prepared, its link does not answer yet: {found.get('error')}")
            job.log(f"linked: OmniaCLI {found.get('version', '?')} there, this one {found.get('role', '?')} there")
            if machine.kind == "existing":
                await add_found_instances(job, settings.link_targets[machine.name], me)
            job.result = {"link": machine.name}

        job = jobs.submit(f"Add machine {machine.name} ({machine.host})", [], me.name, action)
        return {"job": job.id}

    def link_failure(request: Request, link: LinkTarget, message: str):
        """A link that cannot answer: JSON for the pages' scripts, a page otherwise."""
        if "text/html" not in request.headers.get("accept", ""):
            return JSONResponse({"error": f"link {link.name}: {message}"}, status_code=502)
        return _TEMPLATES.TemplateResponse(
            request, "link_error.html", {"link": link, "message": message}, status_code=502)

    @app.get("/links/{name}")
    async def link_root(name: str):
        a_link(name)
        return RedirectResponse(f"/links/{name}/", status.HTTP_307_TEMPORARY_REDIRECT)

    def tunnel_reaches_browser(request: Request) -> bool:
        """Whether a tunnel's port here is reached by this browser: tunnels on
        every address, or on the loopback with the browser on this machine
        (an OmniaCLI on a server: no, its tunnels are its own)."""
        bind = settings.options.tunnel_bind
        if bind in ("0.0.0.0", "::"):
            return True
        return bind in ("127.0.0.1", "::1", "localhost") and request.url.hostname in ("localhost", "127.0.0.1", "::1")

    async def login_as_through_link(request: Request, link, client, url: str, headers: dict, server: str,
                                    role: str, me: Principal):
        """Log in as on an instance of a linked OmniaCLI. Its link opens on the
        instance's name; when the nginx there does not pass that path to the
        OmniaCLI there (a hosting panel's site, left untouched), the link is
        opened here through the link instead, the session it hands over is
        kept, and Odoo is reached through a tunnel: the browser gets the
        session's cookie from this OmniaCLI on the tunnel's own address
        (cookies go by name, not by port)."""
        try:
            resp = await client.post(url, headers={**headers, "content-type": "application/json"},
                                     content=await request.body())
        except httpx.HTTPError as exc:
            return link_failure(request, link, describe_error(exc))
        try:
            data = resp.json()
        except ValueError:
            data = {}
        given = urlsplit(str(data.get("url") or ""))
        if resp.status_code != 200 or not given.hostname:
            return JSONResponse(data or {"error": f"{link.name} answered {resp.status_code}"},
                                status_code=resp.status_code)
        if given.scheme != "https" or not tunnel_reaches_browser(request) or await loginas.reaches_here(given.hostname):
            return JSONResponse(data)
        try:
            landing = await client.get(f"{given.path}?{given.query}", headers={**headers, "host": given.netloc})
        except httpx.HTTPError as exc:
            return link_failure(request, link, describe_error(exc))
        sid = landing.cookies.get(loginas.ODOO_COOKIE)
        if landing.status_code != 303 or not sid:
            return refuse(f"{link.name} did not hand over the session ({landing.status_code})",
                          status.HTTP_502_BAD_GATEWAY)
        try:
            opened = await tunnels.open(link, server, role, me.name, me.role)
        except tunnel.TunnelError as exc:
            return refuse(f"The nginx of {given.hostname} does not pass {loginas.LOGIN_PATH} to OmniaCLI, "
                          f"and no tunnel opens: {exc}", status.HTTP_502_BAD_GATEWAY)
        odoo = urlsplit(tunnel_url(request, opened))
        port = request.url.port or (443 if request.url.scheme == "https" else 80)
        here = f"{odoo.netloc.rsplit(':', 1)[0]}:{port}"
        home = f"http://{odoo.netloc}" + loginas.safe_home(urlsplit(landing.headers.get("location", "")).path or "/odoo")
        detail = f"{link.name} › {server}/{role} as {data.get('login')}, through tunnel {odoo.netloc}"
        token = login_tickets.issue(sid, here, home, me.name, detail)
        access_log.record("login-as", me.kind, me.name, request.client.host if request.client else "",
                          f"asked: {detail}")
        return {"url": f"{request.url.scheme}://{here}{loginas.LOGIN_PATH}?t={token}", "login": data.get("login"),
                "seconds": loginas.TOKEN_SECONDS, "tunnel": f"http://{odoo.netloc}/"}

    @app.api_route("/links/{name}/{path:path}", methods=["GET", "HEAD", "POST"])
    async def link_proxy(request: Request, name: str, path: str, me: Principal = Depends(current_user)):
        """The other instance's page or API, as the link, with at most our role."""
        if me.kind != "user":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Only a user who logs in here may use a link")
        link = a_link(name)
        client = link_clients.client(link)
        headers = {k: request.headers[k] for k in _LINK_REQUEST_HEADERS if k in request.headers}
        # the body is passed on raw: ask only the encodings the browser accepts
        # (httpx would ask gzip by itself, and an nginx there would gzip)
        headers["accept-encoding"] = request.headers.get("accept-encoding", "identity")
        # the pages there in this browser's language (its cookie is this site's)
        headers["accept-language"] = request_language(request)
        headers.update(link_headers(link, me.name, me.role))
        if tunnel_reaches_browser(request):
            headers[TUNNEL_HEADER] = "1"
        url = "/" + path + (f"?{request.url.query}" if request.url.query else "")
        found = _LINK_LOGIN_AS.fullmatch(path)
        if found and request.method == "POST":
            return await login_as_through_link(request, link, client, url, headers, found[1], found[2], me)
        body = await request.body() or None
        # a read is asked once more: a connection kept open may have been closed there
        # in the meantime (its server drops an idle one after a few seconds)
        for last in (False, True) if request.method in ("GET", "HEAD") else (True,):
            outgoing = client.build_request(request.method, url, headers=headers, content=body)
            try:
                resp = await client.send(outgoing, stream=True)
                break
            except (httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError, httpx.CloseError) as exc:
                if last:
                    return link_failure(request, link, describe_error(exc))
            except httpx.HTTPError as exc:
                return link_failure(request, link, describe_error(exc))
        if resp.status_code == 401:
            await resp.aclose()
            return link_failure(request, link, "the other instance refused the token: ask for a new one")
        kept = {k: resp.headers[k] for k in _LINK_RESPONSE_HEADERS if k in resp.headers}
        return StreamingResponse(resp.aiter_raw(), status_code=resp.status_code, headers=kept,
                                 background=BackgroundTask(resp.aclose))

    # --- the shell of an instance ---------------------------------------

    @app.get("/static/{name}")
    async def static_file(name: str):
        if name not in _STATIC_FILES:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such file")
        return FileResponse(_STATIC / name, media_type=_STATIC_FILES[name],
                            headers={"Cache-Control": "private, max-age=86400"})

    def shell_folder(instance: Instance) -> Path | None:
        """Where the shell of an instance starts: its first repository."""
        return instance.repositories[0].path if instance.repositories else None

    def claude_run(instance: Instance):
        # Claude Code in the instance's Claude folder (no folder: no Claude)
        if instance.claude_dir is None:
            return f"{instance.key} has no Claude folder"
        return shell.CLAUDE_COMMAND, {}, True

    def psql_run(instance: Instance):
        # the plugin's database client; the page ends with it
        found = instance.plugin.db_shell(instance)
        if found is None:
            return f"{instance.key} has no database shell"
        command, env = found
        return command, env, False

    def terminal_run(instance: Instance):
        # a shell in the instance's Odoo container (the docker plugin's); the page ends with it
        found = getattr(instance.plugin, "container_shell", None)
        if found is None:
            return f"{instance.key} runs in no container"
        return found(instance), {}, False

    # the shells of an instance: kind -> (where it starts, what it runs:
    # (command first, its environment, the shell stays after it), or why
    # the instance has none of that kind)
    shell_kinds = {
        "shell": (shell_folder, lambda instance: (None, {}, True)),
        "claude": (lambda instance: instance.claude_dir, claude_run),
        "psql": (shell_folder, psql_run),
        "terminal": (lambda instance: None, terminal_run),
    }

    def shell_refused(instance: Instance, kind: str) -> str | None:
        """Why this machine does not open that shell: on a server nobody gets
        a shell of OmniaCLI's own user, whatever the role, also through a link."""
        if settings.machine == MACHINE_DEVELOPMENT:
            return None
        if kind == "psql" and not instance.plugin.db_shell_as_service(instance):
            return None
        if kind == "terminal":
            # in the container, as its own user: never OmniaCLI's
            return None
        return f"This machine is a server: no {kind} as OmniaCLI's own user"

    def shell_instance(server: str, role: str, kind: str) -> Instance:
        active_server(server)
        instance = get_instance(server, role)
        refused = shell_refused(instance, kind)
        if refused:
            raise HTTPException(status.HTTP_403_FORBIDDEN, refused)
        run = shell_kinds[kind][1](instance)
        if isinstance(run, str):
            raise HTTPException(status.HTTP_404_NOT_FOUND, run)
        return instance

    async def shell_page(request: Request, server: str, role: str, kind: str):
        instance = shell_instance(server, role, kind)
        folder, run = shell_kinds[kind]
        command, _, stay = run(instance)
        return _TEMPLATES.TemplateResponse(request, "shell.html", {
            "instance": instance, "folder": folder(instance), "kind": kind, "command": command, "stay": stay,
            "system_user": pwd.getpwuid(os.getuid()).pw_name, "shell": shell.login_shell()[0],
            "drops": shell.drops_folder(instance.server, instance.role), "drop_days": shell.DROP_DAYS,
            "drop_max": shell.DROP_MAX})

    async def shell_socket(websocket: WebSocket, server: str, role: str, kind: str, me: Principal):
        """A login shell of the user running OmniaCLI, in the instance's first
        repository, or running claude in its Claude folder (developer: a user
        here, or a link acting for one)."""
        await websocket.accept()
        srv = settings.servers.get(server)
        instance = srv.instances.get(role) if srv is not None and srv.active else None
        if not me.can("developer"):
            await websocket.close(1008, "The developer role is required")
            return
        if instance is None:
            await websocket.close(1008, f"No instance {server}/{role}")
            return
        refused = shell_refused(instance, kind)
        if refused:
            await websocket.close(1008, refused)
            return
        folder, run = shell_kinds[kind]
        run = run(instance)
        if isinstance(run, str):
            await websocket.close(1008, run)
            return
        command, env, stay = run
        ip = websocket.client.host if websocket.client else ""
        who = f", acting for {me.on_behalf}" if me.on_behalf else ""
        what = f"{instance.key} {kind}" if command else instance.key
        access_log.record("shell", me.kind, me.name, ip, f"{what} opened{who}")
        opened = time.monotonic()
        try:
            await shell.serve(websocket, folder(instance), command, env, stay)
        except OSError as exc:
            _logger.warning("shell of %s: %s", instance.key, exc)
            with contextlib.suppress(RuntimeError):
                await websocket.close(1011, f"The shell did not start: {exc}"[:120])
        finally:
            minutes = round((time.monotonic() - opened) / 60)
            access_log.record("shell", me.kind, me.name, ip, f"{what} closed after {minutes} min{who}")

    async def shell_upload(request: Request, server: str, role: str, kind: str, name: str, me: Principal):
        """A file dropped on the terminal of a shell (the raw body), into the
        instance's drops folder: the page types the path it answers. Allowed
        where that shell opens, for whom may open it (a link too)."""
        instance = shell_instance(server, role, kind)
        name = shell.drop_name(name)
        length = request.headers.get("content-length", "")
        if length.isdigit() and int(length) > shell.DROP_MAX:
            return refuse(f"{name}: more than {shell.DROP_MAX >> 20} MiB", 413)
        folder = shell.drops_folder(instance.server, instance.role)
        shell.prune_drops(folder)
        probe = folder if folder.is_dir() else Path(shell.login_shell()[1])
        free = shutil.disk_usage(probe).free
        if free - (int(length) if length.isdigit() else 0) < puller.MIN_FREE:
            return refuse(f"{name}: {free >> 20} MiB free here, not enough", status.HTTP_507_INSUFFICIENT_STORAGE)
        try:
            dest = await shell.save_drop(folder, name, request.stream())
        except shell.DropRefused as exc:
            return refuse(str(exc), 413)
        except ClientDisconnect:
            return refuse(f"{name}: the upload was interrupted")
        who = f", acting for {me.on_behalf}" if me.on_behalf else ""
        access_log.record("shell", me.kind, me.name, request.client.host if request.client else "",
                          f"{instance.key} {kind}: {dest.name} dropped ({dest.stat().st_size} bytes){who}")
        return {"path": str(dest)}

    async def link_shell_socket(websocket: WebSocket, name: str, server: str, role: str, kind: str,
                                me: Principal):
        """The shell of a linked instance's server, as the link, with at most our role."""
        await websocket.accept()
        link = settings.link_targets.get(name)
        if me.kind != "user" or link is None:
            await websocket.close(1008, "Only a user who logs in here may use a link" if link else f"No link {name}")
            return
        try:
            await proxy_websocket(websocket, link, f"servers/{server}/{role}/{kind}/ws",
                                  link_headers(link, me.name, me.role))
        except Exception as exc:  # refused, unreachable, closed: said to the page
            with contextlib.suppress(RuntimeError):
                await websocket.close(1011, f"link {name}: {describe_error(exc)}"[:120])

    def shell_routes(kind: str) -> None:
        """The page and the WebSockets of one kind of shell (a path each: a
        /servers/{server}/{role}/{kind} would catch the plugins' routes)."""
        @app.get(f"/servers/{{server}}/{{role}}/{kind}", name=f"{kind}_page")
        async def page(request: Request, server: str, role: str, me: Principal = Depends(developer)):
            return await shell_page(request, server, role, kind)

        @app.post(f"/servers/{{server}}/{{role}}/{kind}/upload", name=f"{kind}_upload")
        async def upload(request: Request, server: str, role: str, name: str = "",
                         me: Principal = Depends(developer)):
            return await shell_upload(request, server, role, kind, name, me)

        @app.websocket(f"/servers/{{server}}/{{role}}/{kind}/ws", name=f"{kind}_socket")
        async def socket(websocket: WebSocket, server: str, role: str, me: Principal = Depends(current_user)):
            await shell_socket(websocket, server, role, kind, me)

        @app.websocket(f"/links/{{name}}/servers/{{server}}/{{role}}/{kind}/ws", name=f"link_{kind}_socket")
        async def link_socket(websocket: WebSocket, name: str, server: str, role: str,
                              me: Principal = Depends(current_user)):
            await link_shell_socket(websocket, name, server, role, kind, me)

    for kind in shell_kinds:
        shell_routes(kind)

    # --- tunnels -----------------------------------------------------------

    @app.websocket("/servers/{server}/{role}/tunnel/ws")
    async def tunnel_socket(websocket: WebSocket, server: str, role: str,
                            me: Principal = Depends(current_user)):
        """One connection of a tunnel opened by a linked OmniaCLI (or by a
        user here), to the instance's web interface; ?probe=1 only says
        whether it can be reached."""
        await websocket.accept()
        if me.kind == "token":
            await websocket.close(1008, "An MCP token cannot open a tunnel")
            return
        srv = settings.servers.get(server)
        instance = srv.instances.get(role) if srv is not None and srv.active else None
        if instance is None:
            await websocket.close(1008, f"No instance {server}/{role}")
            return
        if instance.plugin.tunnel_address(instance, "/") is None:
            await websocket.close(1008, f"{instance.key} has no web port")
            return
        if websocket.query_params.get("probe"):
            ip = websocket.client.host if websocket.client else ""
            who = f", acting for {me.on_behalf}" if me.on_behalf else ""
            access_log.record("tunnel", me.kind, me.name, ip, f"{instance.key} opened{who}")
            await websocket.close(1000, "ready")
            return
        await tunnel.serve_far(websocket, lambda path: instance.plugin.tunnel_address(instance, path))

    def tunnel_url(request: Request, found: tunnel.Tunnel) -> str:
        """The address of a tunnel, as the browser reaches this OmniaCLI."""
        wildcard = found.bind in ("0.0.0.0", "::")
        host = (request.url.hostname or "localhost") if wildcard else found.bind
        host = f"[{host}]" if ":" in host else host
        return f"http://{host}:{found.port}/"

    def tunnel_rows(request: Request, me: Principal) -> list[dict]:
        rows = []
        for found in tunnels.tunnels.values():
            row = found.to_dict()
            row["url"] = tunnel_url(request, found)
            row["can_close"] = found.owner == me.name or me.can("admin")
            rows.append(row)
        return rows

    def tunnel_user(me: Principal) -> Principal:
        if me.kind != "user":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Only a user who logs in here may open a tunnel")
        return me

    @app.get("/tunnels")
    async def tunnels_page(request: Request, me: Principal = Depends(current_user)):
        tunnel_user(me)
        rows = await remote_servers(me)
        return _TEMPLATES.TemplateResponse(request, "tunnels.html", {
            "tunnels": tunnel_rows(request, me), "servers": [r for r in rows if "error" not in r],
            "errors": [r for r in rows if "error" in r], "options": settings.options})

    @app.get("/api/tunnels")
    async def tunnels_list(request: Request, me: Principal = Depends(current_user)):
        return tunnel_rows(request, tunnel_user(me))

    @app.post("/api/tunnels/open")
    async def tunnels_open(request: Request, payload: dict = Body(...), me: Principal = Depends(current_user)):
        tunnel_user(me)
        link = settings.link_targets.get(str(payload.get("link") or ""))
        server, role = str(payload.get("server") or ""), str(payload.get("role") or "")
        if link is None:
            return refuse(f"No link {payload.get('link')}")
        if not server or role not in ("production", "test"):
            return refuse("Choose a server and an instance (production or test)")
        try:
            found = await tunnels.open(link, server, role, me.name, me.role)
        except tunnel.TunnelError as exc:
            return refuse(str(exc))
        return {**found.to_dict(), "url": tunnel_url(request, found)}

    @app.post("/api/tunnels/{tunnel_id}/close")
    async def tunnels_close(tunnel_id: str, me: Principal = Depends(current_user)):
        tunnel_user(me)
        found = tunnels.tunnels.get(tunnel_id)
        if found is None:
            return refuse("No such tunnel (closed already?)", status.HTTP_404_NOT_FOUND)
        if found.owner != me.name and not me.can("admin"):
            return refuse("Only who opened it, or an admin, closes a tunnel", status.HTTP_403_FORBIDDEN)
        await tunnels.close(tunnel_id, f"closed by {me.name}")
        return {"ok": True}

    # --- groups of the Dashboard ----------------------------------------

    @app.get("/groups")
    async def groups_page(request: Request, me: Principal = Depends(admin)):
        """The groups of the Dashboard's lists: servers and linked instances."""
        return _TEMPLATES.TemplateResponse(
            request, "groups.html",
            {"lists": {name: [g.to_dict() for g in settings.groups.get(name, [])] for name in listgroups.LISTS},
             "known": {"servers": [srv.name for srv in settings.active_servers()],
                       "links": sorted(settings.link_targets)},
             "groups_file": settings.groups_file},
        )

    @app.post("/api/groups/save", dependencies=[Depends(admin)])
    async def groups_save(payload: dict = Body(...)):
        """The groups of every list: {lists: {servers: [...], links: [...]}}."""
        try:
            lists = listgroups.parse_lists({"lists": payload.get("lists") or {}})
        except listgroups.GroupsError as exc:
            return refuse(str(exc))
        try:
            await asyncio.to_thread(listgroups.save, settings.groups_file, lists)
        except OSError as exc:
            return refuse(f"Cannot write {settings.groups_file}: {exc}", 500)
        settings.groups = lists
        return {"ok": True}

    # --- pullers this instance relays ----------------------------------

    pullers_lock = asyncio.Lock()

    def puller_answer(exc: puller.PullerRefused) -> JSONResponse:
        return refuse(str(exc), exc.status)

    def puller_link(me: Principal) -> str:
        """The link a puller's request comes from: only links take part."""
        if me.kind != "link":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "A puller is used by a linked OmniaCLI only")
        return me.name

    async def puller_message(request: Request, key: str) -> dict | None:
        """The envelope (a mapping) at key of the JSON body, at most
        puller.MAX_MESSAGE bytes; None when key is missing or null."""
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > puller.MAX_MESSAGE:
                raise puller.PullerRefused(f"a message is at most {puller.MAX_MESSAGE >> 20} MiB", 413)
        try:
            data = json.loads(bytes(body) or b"{}")
        except ValueError:
            raise puller.PullerRefused("the body is not JSON") from None
        if not isinstance(data, dict):
            raise puller.PullerRefused("the body is not a JSON object")
        value = data.get(key)
        if value is not None and not isinstance(value, dict):
            raise puller.PullerRefused(f"{key} is not an envelope")
        return value

    def wait_param(request: Request) -> float:
        try:
            return float(request.query_params.get("wait", 0))
        except ValueError:
            raise puller.PullerRefused("wait is a number of seconds") from None

    @app.get("/api/pullers")
    async def puller_list(me: Principal = Depends(current_user)):
        """The pullers the calling link takes part in, and its part."""
        link = puller_link(me)
        return {"pullers": [{"name": d.name, "part": d.part(link)}
                            for d in settings.pullers.values() if d.part(link)]}

    @app.post("/api/pullers/{name}/producer")
    async def puller_producer(request: Request, name: str, me: Principal = Depends(current_user)):
        """The producer's poll: its report (if any) is kept, then the
        commands for it, waiting up to ?wait= seconds for one."""
        link = puller_link(me)
        try:
            pullers.member(name, link, "producer")
            report = await puller_message(request, "report")
            return await pullers.producer_poll(name, link, report, wait_param(request))
        except puller.PullerRefused as exc:
            return puller_answer(exc)

    @app.put("/api/pullers/{name}/producer/files/{file}")
    async def puller_upload(request: Request, name: str, file: str, me: Principal = Depends(current_user)):
        link = puller_link(me)
        length = request.headers.get("content-length")
        try:
            return await pullers.upload(name, link, file, request.stream(),
                                        int(length) if length and length.isdigit() else None)
        except puller.PullerRefused as exc:
            return puller_answer(exc)

    @app.get("/api/pullers/{name}")
    async def puller_info(name: str, me: Principal = Depends(current_user)):
        link = puller_link(me)
        try:
            definition = pullers.definition(name)
            if definition.part(link) is None:
                raise puller.PullerRefused(f"No puller {name}", 404)
            return pullers.info(name)
        except puller.PullerRefused as exc:
            return puller_answer(exc)

    @app.post("/api/pullers/{name}/commands")
    async def puller_command(request: Request, name: str, me: Principal = Depends(current_user)):
        """A consumer's command (an envelope), queued for the producer."""
        link = puller_link(me)
        try:
            pullers.member(name, link, "consumer")
            envelope = await puller_message(request, "command")
            if envelope is None:
                raise puller.PullerRefused("command missing")
            return await pullers.consumer_command(name, link, envelope)
        except puller.PullerRefused as exc:
            return puller_answer(exc)

    @app.get("/api/pullers/{name}/reports")
    async def puller_reports(request: Request, name: str, since: int = 0, me: Principal = Depends(current_user)):
        """The producer's reports after ?since=, waiting up to ?wait= seconds
        for one, with what the relay knows of the puller."""
        link = puller_link(me)
        try:
            return await pullers.consumer_reports(name, link, since, wait_param(request))
        except puller.PullerRefused as exc:
            return puller_answer(exc)

    @app.get("/api/pullers/{name}/files/{file}")
    async def puller_download(request: Request, name: str, file: str, me: Principal = Depends(current_user)):
        link = puller_link(me)
        try:
            path = pullers.download_path(name, link, file)
        except puller.PullerRefused as exc:
            return puller_answer(exc)
        access_log.record("download", me.kind, me.name, request.client.host if request.client else "",
                          f"puller {name}/{file}" + (f" for {me.on_behalf}" if me.on_behalf else ""))
        return FileResponse(path, media_type="application/octet-stream", filename=file)

    @app.delete("/api/pullers/{name}/files/{file}")
    async def puller_taken(name: str, file: str, me: Principal = Depends(current_user)):
        """The consumer has the file whole: it is deleted here."""
        link = puller_link(me)
        try:
            return pullers.taken(name, link, file)
        except puller.PullerRefused as exc:
            return puller_answer(exc)

    @app.get("/pullers", dependencies=[Depends(admin)])
    async def pullers_page(request: Request):
        rows = []
        for name in sorted(settings.pullers):
            info = pullers.info(name)
            info["missing"] = [p for p in [info["producer"]["link"]] + [c["link"] for c in info["consumers"]]
                               if p not in settings.links]
            rows.append(info)
        published = []
        for key, pub in settings.publications.items():
            row = pub.to_dict()
            row.pop("key", None)
            published.append({**row, "instance": key, "has_key": bool(pub.key), "status": publications.status(key),
                              "live": settings.instance(*key.split("/", 1)) is not None,
                              "link_missing": pub.link not in settings.link_targets})
        instances = [i.key for srv in settings.servers.values() for i in srv.instances.values()]
        return _TEMPLATES.TemplateResponse(request, "pullers.html", {
            "pullers": rows, "links": sorted(settings.links), "pullers_file": settings.pullers_file,
            "keep_hours": PULLER_KEEP_HOURS, "max_file_mib": PULLER_FILE_MIB,
            # this machine's clock: who publishes on a box is online when seen in the last two minutes
            "now": time.time(),
            "publications": published, "publications_file": settings.publications_file,
            "instances": [k for k in instances if k not in settings.publications],
            "link_targets": sorted(settings.link_targets), "commands": PUBLISHED_COMMANDS,
            "connections": [{**c.to_dict(), "name": c.name, "key": "", "has_key": bool(c.key),
                             "status": consumers.conn(c.name).status,
                             "link_missing": c.link not in settings.link_targets}
                            for c in settings.connections.values()],
            "connections_file": settings.connections_file})

    @app.post("/api/pullers/save", dependencies=[Depends(admin)])
    async def pullers_save(payload: dict = Body(...)):
        """Create a puller (new: true) or change it."""
        name, new = str(payload.get("name") or "").strip(), bool(payload.get("new"))
        async with pullers_lock:
            if new and name in settings.pullers:
                return refuse(f"A puller named {name} already exists")
            if not new and name not in settings.pullers:
                return refuse(f"No puller {name}", status.HTTP_404_NOT_FOUND)
            try:
                found = parse_puller(name, {k: payload.get(k) for k in
                                            ("producer", "consumers", "keep_hours", "max_file_mib")
                                            if payload.get(k) not in (None, "")})
            except ConfigError as exc:
                return refuse(str(exc))
            unknown = [n for n in [found.producer, *found.consumers] if n not in settings.links]
            if unknown:
                return refuse(f"No incoming link {', '.join(unknown)}: create it on the Users page")
            changed = {**settings.pullers, name: found}
            try:
                await asyncio.to_thread(save_pullers, settings.pullers_file, changed)
            except OSError as exc:
                return refuse(f"Cannot write {settings.pullers_file}: {exc}", 500)
            settings.pullers = changed
            return {"ok": True}

    @app.post("/api/pullers/delete", dependencies=[Depends(admin)])
    async def pullers_delete(payload: dict = Body(...)):
        """A puller goes, with its commands, reports and files."""
        name = str(payload.get("name") or "")
        async with pullers_lock:
            if name not in settings.pullers:
                return refuse(f"No puller {name}", status.HTTP_404_NOT_FOUND)
            changed = {k: v for k, v in settings.pullers.items() if k != name}
            try:
                await asyncio.to_thread(save_pullers, settings.pullers_file, changed)
            except OSError as exc:
                return refuse(f"Cannot write {settings.pullers_file}: {exc}", 500)
            settings.pullers = changed
            pullers.forget(name)
            return {"ok": True}

    # --- instances of this one published on a relay's puller -------------

    @app.post("/api/publications/key", dependencies=[Depends(admin)])
    async def publication_new_key():
        """A new random key, for the publication and its consumers."""
        return {"key": new_key()}

    @app.post("/api/publications/save", dependencies=[Depends(admin)])
    async def publication_save(payload: dict = Body(...)):
        """Publish an instance (new: true) or change its publication; a blank
        key keeps the one saved, remove_key drops it."""
        key, new = str(payload.get("instance") or "").strip(), bool(payload.get("new"))
        async with pullers_lock:
            if new and key in settings.publications:
                return refuse(f"{key} is already published")
            if not new and key not in settings.publications:
                return refuse(f"{key} is not published", status.HTTP_404_NOT_FOUND)
            if not any(key in (i.key for i in srv.instances.values()) for srv in settings.servers.values()):
                return refuse(f"No instance {key}")
            data = {k: payload.get(k) for k in ("link", "puller", "key", "commands")}
            if payload.get("remove_key"):
                data["key"] = ""
            elif not str(data["key"] or "").strip() and not new:
                data["key"] = settings.publications[key].key
            try:
                pub = parse_publication(key, data)
            except ConfigError as exc:
                return refuse(str(exc))
            if pub.link not in settings.link_targets:
                return refuse(f"No link {pub.link}: add the relay on the Links page")
            changed = {**settings.publications, key: pub}
            try:
                await asyncio.to_thread(save_publications, settings.publications_file, changed)
            except OSError as exc:
                return refuse(f"Cannot write {settings.publications_file}: {exc}", 500)
            settings.publications = changed
            publications.reload()
        risky = [a for a in ("start", "stop", "restart") if pub.exposed(a).enabled]
        warning = (f"{', '.join(risky)} exposed without a key: whoever controls the relay can send them"
                   if risky and not pub.key else "")
        return {"ok": True, "warning": warning}

    @app.post("/api/publications/delete", dependencies=[Depends(admin)])
    async def publication_delete(payload: dict = Body(...)):
        key = str(payload.get("instance") or "")
        async with pullers_lock:
            if key not in settings.publications:
                return refuse(f"{key} is not published", status.HTTP_404_NOT_FOUND)
            changed = {k: v for k, v in settings.publications.items() if k != key}
            try:
                await asyncio.to_thread(save_publications, settings.publications_file, changed)
            except OSError as exc:
                return refuse(f"Cannot write {settings.publications_file}: {exc}", 500)
            settings.publications = changed
            publications.reload()
        return {"ok": True}

    # --- puller connections: instances operated through a relay ---------

    def a_connection(name: str):
        try:
            return consumers.conn(name)
        except consumer.ConsumerError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None

    @app.post("/api/connections/save", dependencies=[Depends(admin)])
    async def connection_save(payload: dict = Body(...)):
        """Create a puller connection (new: true) or change it; a blank key
        keeps the one saved, remove_key drops it."""
        name, new = str(payload.get("name") or "").strip(), bool(payload.get("new"))
        async with pullers_lock:
            if new and name in settings.connections:
                return refuse(f"A puller connection named {name} already exists")
            if not new and name not in settings.connections:
                return refuse(f"No puller connection {name}", status.HTTP_404_NOT_FOUND)
            data = {k: payload.get(k) for k in ("link", "puller", "key")}
            if payload.get("remove_key"):
                data["key"] = ""
            elif not str(data["key"] or "").strip() and not new:
                data["key"] = settings.connections[name].key
            try:
                found = parse_connection(name, data)
            except ConfigError as exc:
                return refuse(str(exc))
            if found.link not in settings.link_targets:
                return refuse(f"No link {found.link}: add the relay on the Links page")
            changed = {**settings.connections, name: found}
            try:
                await asyncio.to_thread(save_connections, settings.connections_file, changed)
            except OSError as exc:
                return refuse(f"Cannot write {settings.connections_file}: {exc}", 500)
            settings.connections = changed
            consumers.reload()
        return {"ok": True}

    @app.post("/api/connections/delete", dependencies=[Depends(admin)])
    async def connection_delete(payload: dict = Body(...)):
        """A connection goes; what it received stays on disk until purged."""
        name = str(payload.get("name") or "")
        async with pullers_lock:
            if name not in settings.connections:
                return refuse(f"No puller connection {name}", status.HTTP_404_NOT_FOUND)
            changed = {k: v for k, v in settings.connections.items() if k != name}
            try:
                await asyncio.to_thread(save_connections, settings.connections_file, changed)
            except OSError as exc:
                return refuse(f"Cannot write {settings.connections_file}: {exc}", 500)
            settings.connections = changed
            consumers.reload()
        return {"ok": True}

    @app.get("/pullers/connections/{name}")
    async def connection_page(request: Request, name: str):
        """The instance a puller connection reaches: its state, disks, load,
        commands and jobs, as its reports told them."""
        run = a_connection(name)
        return _TEMPLATES.TemplateResponse(request, "connection.html", {
            "name": name, "connection": run.connection, "commands": PUBLISHED_COMMANDS})

    @app.get("/api/connections/{name}")
    async def connection_json(name: str):
        run = a_connection(name)
        return {"name": name, "puller": run.connection.puller, "link": run.connection.link,
                "has_key": bool(run.connection.key), "status": run.status, "relay": run.relay,
                "latest": run.latest, "disk_history": run.disk_history,
                "jobs": sorted(({k: v for k, v in j.items() if k not in ("lines", "dropped", "next")}
                                for j in run.jobs.values()), key=lambda j: j.get("created") or "", reverse=True),
                "sent": sorted(run.sent.values(), key=lambda c: c["at"], reverse=True)}

    @app.get("/api/connections/{name}/metrics")
    async def connection_metrics(name: str, since: float = 0):
        """The samples received after since: the last hour from memory, older from the files."""
        run = a_connection(name)
        if since >= time.time() - 3600:
            return {"machine": [p for p in run.machine if p["t"] > since],
                    "instance": [p for p in run.instance if p["t"] > since]}
        found = await asyncio.to_thread(run.metrics, since)
        return {"machine": [i["p"] for i in found if i["s"] == "m"],
                "instance": [i["p"] for i in found if i["s"] == "i"]}

    @app.get("/api/connections/{name}/jobs/{job_id}")
    async def connection_job(name: str, job_id: str, since: int = 0):
        try:
            return a_connection(name).job_lines(job_id, since)
        except consumer.ConsumerError as exc:
            return refuse(str(exc), status.HTTP_404_NOT_FOUND)

    @app.post("/api/connections/{name}/commands")
    async def connection_command(name: str, payload: dict = Body(...), me: Principal = Depends(current_user)):
        """A command to the instance there: backup (developer), start, stop and
        restart (every role, as here), stop-job (admin, as here)."""
        run = a_connection(name)
        action = str(payload.get("action") or "")
        params = payload.get("params") or {}
        needed = {"backup": "developer", "stop-job": "admin"}.get(action, "user")
        if action not in (*PUBLISHED_COMMANDS, "stop-job") or not isinstance(params, dict):
            return refuse(f"{action!r} is not a command of a puller")
        if me.kind != "user":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Only a user who logs in here sends commands")
        if not me.can(needed):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"The {needed} role is required")
        if action == "backup":
            fields = (run.latest.get("backup_fields") or [])
            params = {f["key"]: params.get(f["key"]) for f in fields if isinstance(f, dict) and f.get("key") in params}
        elif action == "stop-job":
            params = {"job": str(params.get("job") or "")}
        else:
            params = {}
        try:
            return await run.send(action, params, me.name)
        except consumer.ConsumerError as exc:
            return refuse(str(exc), status.HTTP_502_BAD_GATEWAY)

    # --- Settings: the places the bar on top does not name ----------------

    @app.get("/settings", dependencies=[Depends(developer)])
    async def settings_page(request: Request, me: Principal = Depends(current_user)):
        """What the sidebar listed before (docs/ux/REVIEW-2026-11-18.md): each
        place with what it is for, the admins' ones for the admins."""
        lang = request_language(request)
        t = functools.partial(i18n.gettext, lang)
        admin_here = me.can("admin") and not me.scoped

        def group(title, *cards):
            found = [{"path": path, "title": title_, "text": text} for path, title_, text, shown in cards if shown]
            return {"title": title, "cards": found} if found else None

        groups = [
            group(t("Activity"),
                  ("/jobs", t("Jobs"), t("Every action OmniaCLI ran, with its steps and its log"), True),
                  ("/disks", t("Disks"), t("The disks of this machine, their free space and when they fill up"), not me.scoped),
                  ("/omniacli/log", t("Log"), t("OmniaCLI's own log"), admin_here)),
            group(t("Servers and links"),
                  ("/config", t("Servers by hand"), t("The servers' configuration, field by field: for the instances OmniaCLI did not make"), admin_here),
                  ("/links", t("Links"), t("Other OmniaCLI reached from here, and the tokens they use"), admin_here),
                  ("/groups", t("Groups"), t("The groups the instances and the linked OmniaCLI are arranged in"), admin_here),
                  ("/pullers", t("Publications"), t("The instances that cannot be reached from outside: who publishes them, who relays, who operates them"), admin_here),
                  ("/schedules", t("Schedules"), t("Actions run at set times: backups, updates, restarts"), admin_here),
                  ("/destinations", t("Backup destinations"), t("Where the automatic backups are copied, encrypted: SFTP, FTP, S3, Drive, Dropbox…"), admin_here),
                  ("/tunnels", t("Tunnels"), t("Ports of the instances reached from your computer"), me.kind == "user" and not getattr(request.state, "prefix", ""))),
            group(t("People and access"),
                  ("/users", t("Users"), t("Who logs in, with which role"), admin_here),
                  ("/workspaces", t("Workspaces"), t("Customers' closed parts of the machine: their instances, users, limits and names"), admin_here),
                  ("/credentials", t("Git credentials"), t("The git credentials of the servers and workspaces"), admin_here)),
            group(t("System"),
                  ("/plugins", t("Plugins"), t("What OmniaCLI knows how to run, besides Odoo"), admin_here),
                  ("/options", t("Options"), t("Consoles, tunnels and the other options of this OmniaCLI"), admin_here),
                  ("/help", t("Help"), t("How OmniaCLI works, section by section"), True)),
        ]
        return _TEMPLATES.TemplateResponse(request, "settings.html", {"groups": [g for g in groups if g]})

    # --- options of the system ------------------------------------------

    @app.get("/options", dependencies=[Depends(admin)])
    async def options_page(request: Request):
        return _TEMPLATES.TemplateResponse(request, "options.html", {
            "options": settings.options, "options_file": settings.options_file, "machine": settings.machine,
            "font_sizes": SHELL_FONT_SIZES, "idle_hours": TUNNEL_IDLE_HOURS})

    @app.post("/api/options/save", dependencies=[Depends(admin)])
    async def options_save(payload: dict = Body(...)):
        # the mail: not posted keeps the saved one; a password left empty too
        mail = payload.get("mail")
        if mail is None:
            current = settings.options
            payload = {**payload, "mail": {"host": current.smtp_host, "port": current.smtp_port,
                                           "security": current.smtp_security, "user": current.smtp_user,
                                           "password": current.smtp_password, "from": current.mail_from}}
        elif isinstance(mail, dict) and not mail.get("password"):
            payload = {**payload, "mail": {**mail, "password": settings.options.smtp_password}}
        if "monitor" not in payload:
            payload = {**payload, "monitor": {"notify": list(settings.options.monitor_notify)}}
        try:
            options = parse_options(payload)
        except ConfigError as exc:
            return refuse(str(exc))
        try:
            await asyncio.to_thread(save_options, settings.options_file, options)
        except OSError as exc:
            return refuse(f"Cannot write {settings.options_file}: {exc}", 500)
        settings.options = options
        return {"ok": True}

    # --- scheduled actions ----------------------------------------------

    schedules_lock = asyncio.Lock()

    def schedule_view(sch: schedules.Schedule) -> dict:
        try:
            upcoming = [t.isoformat(sep=" ", timespec="minutes") for t in schedules.next_runs(sch.cron)]
        except (ValueError, KeyError):
            upcoming = []
        return {"name": sch.name, "data": schedules.schedule_to_dict(sch), "steps": sch.steps,
                "user": sch.acting_user, "next": upcoming if sch.enabled else [],
                "live": scheduler.instance_of(sch) is not None,
                "runs": scheduler.history.runs(sch.name, jobs)}

    @app.get("/schedules", dependencies=[Depends(admin)])
    async def schedules_page(request: Request):
        targets = {}
        for srv in settings.servers.values():
            plugin = plugins.registry().get(srv.type)
            for inst in srv.instances.values():
                src = inst.from_link
                targets[inst.key] = {
                    "live": srv.active,
                    "type": srv.type,
                    "has_restore": bool(plugin and plugin.has_restore),
                    "has_backup": bool(plugin and plugin.has_backup),
                    "backup_fields": [_field_dict(f) for f in plugin.backup_fields()] if plugin else [],
                    "restore_fields": [_field_dict(f) for f in plugin.restore_fields()] if plugin else [],
                    "from_link": src.to_dict() if src else None,
                }
        return _TEMPLATES.TemplateResponse(
            request, "schedules.html",
            {"schedules": [schedule_view(s) for s in scheduler.schedules.values()], "targets": targets,
             "links": list(settings.link_targets), "connections": list(settings.connections),
             "scripts": schedules.list_scripts(settings.scripts_dir),
             "scripts_dir": settings.scripts_dir, "schedules_file": settings.schedules_file,
             "load_error": schedules_error, "default_user": schedules.DEFAULT_USER},
        )

    @app.get("/api/schedules/next", dependencies=[Depends(admin)])
    async def schedules_next(cron: str = ""):
        """The next runs of a crontab expression, for the form."""
        try:
            expr = schedules.check_cron(cron, "cron")
        except ConfigError as exc:
            return refuse(str(exc).removeprefix("cron: "))
        return {"cron": expr, "next": [t.isoformat(sep=" ", timespec="minutes")
                                       for t in schedules.next_runs(expr, 5)]}

    @app.post("/api/schedules/save", dependencies=[Depends(admin)])
    async def schedules_save(payload: dict = Body(...)):
        nonlocal schedules_error
        original = payload.get("original") or None
        name = str(payload.get("name") or "").strip()
        data = dict(payload.get("schedule") or {})
        async with schedules_lock:
            current = scheduler.schedules
            if original is not None and original not in current:
                return refuse(f"Schedule {original} no longer exists", status.HTTP_404_NOT_FOUND)
            if name != original and name in current:
                return refuse(f"A schedule named {name} already exists")
            server, _, role = str(data.get("instance") or "").partition("/")
            srv = settings.servers.get(server)
            if srv is None or role not in srv.instances:
                return refuse(f"No instance {data.get('instance')}")
            instance = srv.instances[role]
            # what this form does not show stays as it was: the destination of
            # the backup and whom to tell (the instance's Backups tab sets them)
            before = current.get(original) if original is not None else None
            if before is not None and "notify" not in data:
                data["notify"] = list(before.notify)
            backup = data.get("backup")
            if backup and before is not None and before.backup and "destination" not in backup:
                backup["destination"] = before.backup.destination
            if backup:
                if not instance.plugin.has_backup:
                    return refuse(f"{instance.plugin.label} has no backup")
                try:
                    backup["options"] = {f.key: _field_value(f, (backup.get("options") or {}).get(f.key))
                                         for f in instance.plugin.backup_fields() if f.kind == "checkbox"}
                except ActionRefused as exc:
                    return refuse(str(exc))
            copy = data.get("copy")
            if copy:
                if not instance.plugin.has_restore:
                    return refuse(f"{instance.plugin.label} cannot restore a database: no copy from link")
                # the choices as the instance's plugin asks them
                try:
                    # the checkboxes: the other fields come from the last
                    # backup dialog of the instance there
                    copy["backup_options"] = {f.key: _field_value(f, (copy.get("backup_options") or {}).get(f.key))
                                              for f in instance.plugin.backup_fields() if f.kind == "checkbox"}
                    copy["restore_options"] = {
                        f.key: _field_value(f, (copy.get("restore_options") or {}).get(f.key))
                        for f in instance.plugin.restore_fields()}
                except ActionRefused as exc:
                    return refuse(str(exc))
            try:
                parsed = schedules.parse_schedule(name, data)
            except (ConfigError, TypeError, ValueError, AttributeError) as exc:
                return refuse(str(exc))
            updated = {}
            for key, sch in current.items():
                # an edited schedule keeps its place in the list
                updated[name if key == original else key] = parsed if key == original else sch
            updated.setdefault(name, parsed)
            try:
                await asyncio.to_thread(schedules.save_schedules, settings.schedules_file, updated)
            except OSError as exc:
                return refuse(f"Cannot write {settings.schedules_file}: {exc}", 500)
            if original and original != name:
                scheduler.history.rename(original, name)
            scheduler.schedules = updated
            schedules_error = ""
        return {"ok": True}

    @app.post("/api/schedules/delete", dependencies=[Depends(admin)])
    async def schedules_delete(payload: dict = Body(...)):
        name = str(payload.get("name") or "")
        async with schedules_lock:
            if name not in scheduler.schedules:
                return refuse(f"No schedule {name}", status.HTTP_404_NOT_FOUND)
            updated = {k: v for k, v in scheduler.schedules.items() if k != name}
            try:
                await asyncio.to_thread(schedules.save_schedules, settings.schedules_file, updated)
            except OSError as exc:
                return refuse(f"Cannot write {settings.schedules_file}: {exc}", 500)
            scheduler.schedules = updated
            scheduler.history.keep_only(updated)
        return {"ok": True}

    @app.post("/api/schedules/{name}/run")
    async def schedules_run(name: str, user: Principal = Depends(admin)):
        """Run the schedule now, enabled or not."""
        sch = scheduler.schedules.get(name)
        if sch is None:
            return refuse(f"No schedule {name}", status.HTTP_404_NOT_FOUND)
        job = scheduler.fire(sch, f"manual: {user}")
        if job is None:
            return refuse(scheduler.history.data[name][0]["message"])
        return {"job": job.id}

    # --- the automatic backups of an instance (its Backups tab) ---------

    def backup_schedule_name(instance: Instance) -> str:
        """The schedule of an instance's automatic backups: one per instance,
        a schedule as the others (the Schedules page shows it too)."""
        return f"backup.{instance.server}.{instance.role}"

    def backup_schedule_view(instance: Instance, me: Principal) -> dict:
        sch = scheduler.schedules.get(backup_schedule_name(instance))
        backup = sch.backup if sch is not None else None
        rotation = ({k: getattr(backup, k) for k in schedules.DEFAULT_ROTATION} if backup is not None
                    else dict(schedules.DEFAULT_ROTATION))
        time = "03:00"
        if sch is not None:
            minute, hour, *rest = sch.cron.split(" ")
            if minute.isdigit() and hour.isdigit() and rest == ["*", "*", "*"]:
                time = f"{int(hour):02d}:{int(minute):02d}"
        runs = scheduler.history.runs(sch.name, jobs) if sch is not None else []
        try:
            upcoming = schedules.next_runs(sch.cron, 1)[0].isoformat(sep=" ", timespec="minutes") \
                if sch is not None and sch.enabled else None
        except (ValueError, KeyError):
            upcoming = None
        return {"exists": sch is not None, "enabled": bool(sch and sch.enabled and backup), "time": time,
                "cron": sch.cron if sch is not None else "", **rotation,
                "options": dict(backup.options) if backup is not None else {},
                "next": upcoming, "last": runs[0] if runs else None,
                # the schedule runs at this machine's time
                "zone": datetime.datetime.now().astimezone().tzname() or "",
                "fields": [_field_dict(f) for f in instance.plugin.backup_fields() if f.kind == "checkbox"],
                "destination": backup.destination if backup is not None else "",
                "verify": backup is not None and backup.verify_weekday is not None,
                "archive": bool(backup and backup.archive),
                "has_verify": instance.plugin.has_verify,
                "verified": last_verified(instance),
                "destinations": [{"name": d.name, "label": d.label, "path": d.path, "workspace": d.workspace}
                                 for d in destination_choices(instance, me, backup.destination if backup else "")],
                "notify": list(sch.notify) if sch is not None else [],
                "can_edit": me.can(backup_role(instance))}

    def last_verified(instance: Instance) -> dict | None:
        """The newest restore tried of the instance's backups: {file, time, ok, ...}."""
        found = [{"file": name, **entry["verified"]} for name, entry in
                 backupcheck.load(backup_folder(instance)).items() if (entry or {}).get("verified")]
        return max(found, key=lambda v: v.get("time") or "") if found else None

    def load_destinations() -> dict:
        try:
            return destinations.load_destinations(settings.destinations_file)
        except ConfigError as exc:
            _logger.warning("%s", exc)
            return {}

    def machine_admin(me: Principal) -> bool:
        """An admin of the machine (a user, a token, a link acting as one), not
        of a workspace only."""
        return not me.workspaces and me.can("admin")

    def destination_choices(instance: Instance, me: Principal, current: str = "") -> list:
        """The destinations the instance's backups may go to, for me: the
        workspaces' it is in, the machine's for the machine's admins; and the
        one chosen now, whoever chose it."""
        found = load_destinations()
        here = {ws.name for ws in settings.workspaces.values() if instance.server in ws.servers}
        return [d for d in found.values()
                if (d.workspace in here) or (not d.workspace and machine_admin(me)) or d.name == current]

    def backup_role(instance: Instance) -> str:
        """Who may delete an instance's backups and set its automatic ones:
        a production's are an admin's."""
        return "admin" if instance.role == "production" else "developer"

    @app.get("/api/servers/{server}/{role}/backup-schedule", dependencies=[Depends(developer)])
    async def backup_schedule_get(server: str, role: str, me: Principal = Depends(current_user)):
        """The automatic backups of the instance: when, the rotation, the
        choices, the last run."""
        return backup_schedule_view(get_instance(server, role), me)

    @app.post("/api/servers/{server}/{role}/backup-schedule")
    async def backup_schedule_save(server: str, role: str, payload: dict = Body(...),
                                   me: Principal = Depends(current_user)):
        """The automatic backups of the instance saved: every day at a time,
        kept by a rotation (days, weeks, months); off keeps the schedule's
        other steps, or removes it when it has none."""
        instance = get_instance(server, role)
        if not me.can(backup_role(instance)):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"The {backup_role(instance)} role is required")
        if not instance.plugin.has_backup:
            return refuse(f"{instance.plugin.label} has no backup")
        name = backup_schedule_name(instance)
        if not schedules.SCHEDULE_NAME.fullmatch(name):
            return refuse(f"The server's name is too long for a schedule ({name})")
        time = str(payload.get("time") or "").strip()
        found = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", time)
        if found is None:
            return refuse(f"{time!r} is not a time HH:MM")
        enabled = bool(payload.get("enabled"))
        try:
            options = {f.key: _field_value(f, (payload.get("options") or {}).get(f.key))
                       for f in instance.plugin.backup_fields() if f.kind == "checkbox"}
        except ActionRefused as exc:
            return refuse(str(exc))
        async with schedules_lock:
            current = scheduler.schedules.get(name)
            data = schedules.schedule_to_dict(current) if current is not None else {
                "instance": instance.key, "user": me.name if me.kind == "user" else ""}
            destination = str(payload.get("destination") or "")
            was = current.backup.destination if current is not None and current.backup else ""
            if destination and destination != was and destination not in [
                    d.name for d in destination_choices(instance, me)]:
                return refuse(f"No destination {destination} for {instance.key}")
            # a restore tried every Sunday: on, or as it was
            weekday = (current.backup.verify_weekday if current is not None and current.backup else None)
            if "verify" in payload:
                weekday = VERIFY_WEEKDAY if payload.get("verify") and instance.plugin.has_verify else None
            data.update({"cron": f"{int(found[2])} {int(found[1])} * * *", "enabled": True,
                         "backup": {"options": options, "destination": destination, "verify_weekday": weekday,
                                    "archive": bool(payload.get("archive", current.backup.archive
                                                                if current is not None and current.backup else False)),
                                    **{k: payload.get(k) for k in schedules.DEFAULT_ROTATION}}})
            if "notify" in payload:
                data["notify"] = payload.get("notify") or []
            if not enabled:
                data.pop("backup")
            updated = dict(scheduler.schedules)
            if not enabled and not (data.get("pull") or data.get("copy") or data.get("script")):
                updated.pop(name, None)
            else:
                try:
                    updated[name] = schedules.parse_schedule(name, data)
                except ConfigError as exc:
                    return refuse(str(exc).removeprefix(f"schedules.{name}."))
            try:
                await asyncio.to_thread(schedules.save_schedules, settings.schedules_file, updated)
            except OSError as exc:
                return refuse(f"Cannot write {settings.schedules_file}: {exc}", 500)
            scheduler.schedules = updated
            scheduler.history.keep_only(updated)
        return {"ok": True, **backup_schedule_view(instance, me)}

    # --- deploy on push (deployhooks.py) -------------------------------------

    deploy_hooks_file = settings.servers_file.parent / deployhooks.FILE

    def deploy_hook_view(request: Request, instance: Instance, me: Principal) -> dict:
        hook = deployhooks.load(deploy_hooks_file).get(instance.key) or {}
        # on the instance's own name when it has one (its nginx passes it here):
        # OmniaCLI itself may answer on 127.0.0.1 only
        url = (f"{instance.site}{workspaceweb.HOOKS_PATH}deploy/{instance.server}/{instance.role}" if instance.site
               else str(request.base_url).rstrip("/") + f"/hooks/deploy/{instance.server}/{instance.role}")
        return {"enabled": bool(hook.get("secret")), "url": url,
                "last": hook.get("last"), "can_edit": me.can(backup_role(instance)),
                "repositories": [r.name for r in instance.repositories]}

    @app.get("/api/servers/{server}/{role}/deploy-hook", dependencies=[Depends(developer)])
    async def deploy_hook_get(request: Request, server: str, role: str, me: Principal = Depends(current_user)):
        return deploy_hook_view(request, get_instance(server, role), me)

    @app.post("/api/servers/{server}/{role}/deploy-hook")
    async def deploy_hook_save(request: Request, server: str, role: str, payload: dict = Body(...),
                               me: Principal = Depends(current_user)):
        """Deploy on push switched on (a new secret, in the answer once: the
        address and it go in the repository's webhook) or off."""
        instance = get_instance(server, role)
        if not me.can(backup_role(instance)):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"The {backup_role(instance)} role is required")
        data = deployhooks.load(deploy_hooks_file)
        secret = None
        if payload.get("enabled"):
            secret = deployhooks.new_secret()
            data[instance.key] = {"secret": secret, "by": me.name}
        else:
            data.pop(instance.key, None)
        await asyncio.to_thread(deployhooks.save, deploy_hooks_file, data)
        return {**deploy_hook_view(request, instance, me), "secret": secret}

    async def deploy_hook_call(request: Request):
        """A push told by GitHub, GitLab, Bitbucket or a CI: with the
        instance's secret, the repository pushed pulled if the instance
        follows that branch, then the changed modules and a restart. A
        Starlette route: no login, the secret is its check."""
        server, role = request.path_params["server"], request.path_params["role"]
        instance = settings.instance(server, role) if role in ROLES else None
        hook = deployhooks.load(deploy_hooks_file).get(f"{server}/{role}") or {}
        body = await request.body()
        headers = {k.lower(): v for k, v in request.headers.items()}
        ip = request.client.host if request.client else ""
        if instance is None or not deployhooks.verified(headers, body[:deployhooks.MAX_BODY], hook.get("secret", "")):
            access_log.record("failed", "hook", "", ip, f"deploy {server}/{role}")
            return JSONResponse({"error": "Unknown instance or wrong secret"}, status_code=403)
        if headers.get("x-github-event") == "ping" or headers.get("x-gitlab-event") == "System Hook":
            return JSONResponse({"ok": True, "pong": True})
        branch, urls = deployhooks.pushed(body)
        followed = []
        for repo in instance.repositories:
            code, out = await ops.capture(repo.git("remote", "get-url", repo.remote), cwd=repo.path)
            if code == 0 and deployhooks.host_path(out.strip()) in urls and repo.branch == branch:
                followed.append(repo.name)
        record = {"time": datetime.datetime.now().isoformat(timespec="seconds"), "branch": branch}
        if not followed:
            record["result"] = f"nothing to do: {instance.key} follows no pushed branch {branch or '?'} of that repository"
            job = None
        else:
            job = jobs.submit(f"Deploy on push {', '.join(followed)} to {instance.key}", [instance.key],
                              f"push ({branch})", lambda job: ops.pull_restart(
                                  job, instance, only_if_changed=True, only=set(followed),
                                  backup_folder=backup_folder(instance)),
                              workspace=(workspace_of(server) or (None,))[0].name if workspace_of(server) else "")
            record.update(result="deploy", job=job.id, repositories=followed)
        access_log.record("action", "hook", "push", ip, f"deploy {instance.key}: {record['result']}")
        data = deployhooks.load(deploy_hooks_file)
        if instance.key in data:
            data[instance.key]["last"] = record
            await asyncio.to_thread(deployhooks.save, deploy_hooks_file, data)
        return JSONResponse({"ok": True, **({"job": job.id} if job else {"note": record["result"]})},
                            status_code=202 if job else 200)

    app.add_route("/hooks/deploy/{server}/{role}", deploy_hook_call, methods=["POST"])
    # the same on an instance's own name, which its nginx passes here
    app.add_route(workspaceweb.HOOKS_PATH + "deploy/{server}/{role}", deploy_hook_call, methods=["POST"])

    @app.post("/api/servers/{server}/{role}/backups/delete")
    async def backup_delete(request: Request, server: str, role: str, payload: dict = Body(...),
                            me: Principal = Depends(current_user)):
        """A backup of the instance deleted (an admin's for a production)."""
        instance = get_instance(server, role)
        if not me.can(backup_role(instance)):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"The {backup_role(instance)} role is required")
        name = str(payload.get("file") or "")
        folder = backup_folder(instance).resolve()
        path = (folder / name).resolve()
        if not _BACKUP_NAME.fullmatch(name) or path.parent != folder or not path.is_file():
            return refuse("No such backup", status.HTTP_404_NOT_FOUND)
        if jobs.is_busy(instance.key):
            return refuse(f"A job is running on {instance.key}: try again when it ends")
        path.unlink()
        access_log.record("delete backup", me.kind, me.name, request.client.host if request.client else "",
                          f"{instance.key}/{name}")
        return {"ok": True}

    async def remote_of(instance: Instance) -> "destinations.Destination | None":
        sch = scheduler.schedules.get(backup_schedule_name(instance))
        name = sch.backup.destination if sch is not None and sch.backup else ""
        return load_destinations().get(name) if name else None

    @app.get("/api/servers/{server}/{role}/remote-backups", dependencies=[Depends(developer)])
    async def remote_backups(server: str, role: str, me: Principal = Depends(current_user)):
        """The copies of the instance's backups in the destination of its
        automatic backups, newest first."""
        instance = get_instance(server, role)
        dest = await remote_of(instance)
        if dest is None:
            return {"destination": None, "files": []}
        try:
            files = await destinations.listing(dest, destinations.instance_folder(instance))
        except JobFailed as exc:
            return JSONResponse({"error": str(exc), "destination": dest.name}, status_code=502)
        local = backup_folder(instance)
        return {"destination": {"name": dest.name, "label": dest.label, "path": dest.path},
                "files": [{**f, "here": (local / f["file"]).exists()} for f in files],
                "can_fetch": me.can(backup_role(instance))}

    @app.post("/api/servers/{server}/{role}/remote-backups/fetch")
    async def remote_backup_fetch(request: Request, server: str, role: str, payload: dict = Body(...),
                                  me: Principal = Depends(current_user)):
        """A copy brought back into the instance's backups, decrypted: a job.
        Restoring it is then the Backups tab's Restore."""
        instance = get_instance(server, role)
        if not me.can(backup_role(instance)):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"The {backup_role(instance)} role is required")
        dest = await remote_of(instance)
        name = str(payload.get("file") or "")
        if dest is None:
            return refuse("The automatic backups of this instance have no destination")
        if not name.endswith(destinations.SUFFIX) or not _BACKUP_NAME.fullmatch(name[:-len(destinations.SUFFIX)]):
            return refuse("No such copy", status.HTTP_404_NOT_FOUND)
        folder = backup_folder(instance)
        job = jobs.submit(f"Bring back {name[:-len(destinations.SUFFIX)]} from {dest.name}", [instance.key],
                          f"manual: {me}", lambda job: destinations.fetch(
                              job, dest, destinations.instance_folder(instance), name, folder))
        return {"job": job.id}

    # --- remote destinations of the backups -----------------------------

    destinations_lock = asyncio.Lock()

    def destination_rows(workspace: str | None) -> list:
        """The destinations of a workspace ("" the machine's, None all of them),
        as the page shows them, and which schedules use each."""
        used = {}
        for sch in scheduler.schedules.values():
            if sch.backup and sch.backup.destination:
                used.setdefault(sch.backup.destination, []).append(sch.instance)
        return [{**d.public(), "used_by": sorted(used.get(d.name, []))}
                for d in sorted(load_destinations().values(), key=lambda d: (d.workspace, d.name))
                if workspace is None or d.workspace == workspace]

    def destination_kinds(request: Request) -> dict:
        """The kinds and their settings, in the page's language."""
        lang = request_language(request)
        return {k: {"label": i18n.tr(lang, label),
                    "settings": [{**s.to_dict(), "label": i18n.tr(lang, s.label), "help": i18n.tr(lang, s.help)}
                                 for s in found]}
                for k, (label, _, found) in destinations.KINDS.items()}

    async def destination_save(workspace: str, payload: dict):
        """A destination of workspace ("": the machine's) made or changed:
        a secret left empty keeps the saved one; a new one gets a key (or the
        one given: copies made before, elsewhere, can be read again)."""
        name = str(payload.get("name") or "").strip()
        async with destinations_lock:
            found = await asyncio.to_thread(load_destinations)
            current = found.get(name)
            if payload.get("new") and current is not None:
                return refuse(f"A destination {name} exists already")
            if current is not None and current.workspace != workspace:
                return refuse(f"No destination {name} here", status.HTTP_404_NOT_FOUND)
            kind = str(payload.get("kind") or "")
            try:
                checked = destinations.check_settings(
                    kind, payload.get("settings") or {}, current.settings if current and current.kind == kind else {})
            except ConfigError as exc:
                return refuse(str(exc))
            key = current.key if current is not None else (str(payload.get("key") or "").strip()
                                                           or new_key())
            data = {"kind": kind, "path": str(payload.get("path") or ""), "workspace": workspace,
                    "settings": checked, "key": key}
            try:
                dest = destinations.parse_destination(name, data)
            except ConfigError as exc:
                return refuse(str(exc).removeprefix(f"destinations.{name}: ").removeprefix(f"destinations.{name}."))
            try:
                await asyncio.to_thread(destinations.save_destinations, settings.destinations_file,
                                        {**found, name: dest})
            except OSError as exc:
                return refuse(f"Cannot write {settings.destinations_file}: {exc}", 500)
        return {"ok": True, "destination": dest.public(), "new_key": current is None}

    async def destination_delete(workspace: str, payload: dict):
        name = str(payload.get("name") or "")
        async with destinations_lock:
            found = await asyncio.to_thread(load_destinations)
            if name not in found or found[name].workspace != workspace:
                return refuse(f"No destination {name} here", status.HTTP_404_NOT_FOUND)
            users = [s.instance for s in scheduler.schedules.values() if s.backup and s.backup.destination == name]
            if users:
                return refuse(f"The automatic backups of {', '.join(sorted(users))} go to {name}: choose another first")
            found.pop(name)
            await asyncio.to_thread(destinations.save_destinations, settings.destinations_file, found)
        return {"ok": True}

    def destination_of(workspace: str, payload: dict):
        found = load_destinations().get(str(payload.get("name") or ""))
        if found is None or found.workspace != workspace:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such destination here")
        return found

    async def destination_check(workspace: str, payload: dict):
        """The destination reached and written now (a file put and removed)."""
        dest = destination_of(workspace, payload)
        try:
            return {"ok": True, "message": await destinations.check(dest)}
        except JobFailed as exc:
            return refuse(str(exc), status.HTTP_502_BAD_GATEWAY)

    def destination_key(request: Request, me: Principal, workspace: str, payload: dict):
        """The destination's key, to keep elsewhere: without it, its copies
        cannot be read back on another machine."""
        dest = destination_of(workspace, payload)
        access_log.record("show destination key", me.kind, me.name, request.client.host if request.client else "",
                          dest.name)
        return {"key": dest.key}

    @app.get("/destinations", dependencies=[Depends(admin)])
    async def destinations_page(request: Request):
        """The machine's destinations, and the workspaces' (each workspace's
        admins set theirs on its page)."""
        return _TEMPLATES.TemplateResponse(request, "destinations.html", {
            "rows": destination_rows(None), "kinds": destination_kinds(request), "api": "/api/destinations",
            "workspace": "", "rclone": destinations.rclone_path() is not None,
            "file": settings.destinations_file})

    @app.post("/api/destinations/save", dependencies=[Depends(admin)])
    async def machine_destination_save(payload: dict = Body(...)):
        return await destination_save("", payload)

    @app.post("/api/destinations/delete", dependencies=[Depends(admin)])
    async def machine_destination_delete(payload: dict = Body(...)):
        return await destination_delete("", payload)

    @app.post("/api/destinations/check", dependencies=[Depends(admin)])
    async def machine_destination_check(payload: dict = Body(...)):
        return await destination_check("", payload)

    @app.post("/api/destinations/key", dependencies=[Depends(admin)])
    async def machine_destination_key(request: Request, payload: dict = Body(...),
                                      me: Principal = Depends(current_user)):
        return destination_key(request, me, "", payload)

    @app.get("/workspaces/{workspace}/destinations", dependencies=[Depends(admin)])
    async def workspace_destinations_page(request: Request, workspace: str):
        ws = a_workspace(workspace)
        return _TEMPLATES.TemplateResponse(request, "destinations.html", {
            "rows": destination_rows(ws.name), "kinds": destination_kinds(request),
            "api": f"/api/workspaces/{ws.name}/destinations", "workspace": ws.name, "ws": ws,
            "rclone": destinations.rclone_path() is not None, "file": None})

    @app.post("/api/workspaces/{workspace}/destinations/save", dependencies=[Depends(admin)])
    async def workspace_destination_save(workspace: str, payload: dict = Body(...)):
        return await destination_save(a_workspace(workspace).name, payload)

    @app.post("/api/workspaces/{workspace}/destinations/delete", dependencies=[Depends(admin)])
    async def workspace_destination_delete(workspace: str, payload: dict = Body(...)):
        return await destination_delete(a_workspace(workspace).name, payload)

    @app.post("/api/workspaces/{workspace}/destinations/check", dependencies=[Depends(admin)])
    async def workspace_destination_check(workspace: str, payload: dict = Body(...)):
        return await destination_check(a_workspace(workspace).name, payload)

    @app.post("/api/workspaces/{workspace}/destinations/key", dependencies=[Depends(admin)])
    async def workspace_destination_key(request: Request, workspace: str, payload: dict = Body(...),
                                        me: Principal = Depends(current_user)):
        return destination_key(request, me, a_workspace(workspace).name, payload)

    # --- the lists of every machine: Instances and Backups (docs/ux/UX-008 §4) --
    # this machine's lists, and each link's asked through it, one request a
    # machine (the page asks them all at once: one that does not answer is a
    # row saying so, and does not hold the others back)

    # a run of the automatic backups that did not start or did not end
    BACKUP_NOT_DONE = ("failed", "interrupted", "stopped", "skipped")
    # "tonight" of the Backups page: the runs of the last day
    TONIGHT = datetime.timedelta(hours=24)

    def backup_schedule_state(instance: Instance, lang: str) -> dict:
        """Whether the instance backs up by itself, and its last run ended
        or refused: {scheduled, last: {time, status, ok, job, message} | None}."""
        sch = scheduler.schedules.get(backup_schedule_name(instance))
        runs = scheduler.history.runs(sch.name, jobs) if sch is not None else []
        last = next((r for r in runs if r.get("status") in (SUCCESS, *BACKUP_NOT_DONE)), None)
        return {"scheduled": bool(sch is not None and sch.enabled and sch.backup),
                "last": None if last is None else {
                    "time": last.get("time"), "status": last.get("status"), "ok": last.get("status") == SUCCESS,
                    "job": last.get("job"), "message": i18n.tr(lang, last.get("message") or "")}}

    def odoo_version(server: str) -> str:
        """The Odoo of a workspace's instance, as 19.0, from the template it was
        made from; empty for one made elsewhere."""
        found = workspace_of(server)
        if found is None:
            return ""
        ws, instance = found
        try:
            meta = json.loads((workspaceops.storage_path(ws) / ws.name / instance / workspacehelper.INSTANCE_FILE)
                              .read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        major = re.fullmatch(r"[a-z]+-(\d+)", str(meta.get("template") or "") if isinstance(meta, dict) else "")
        return f"{major.group(1)}.0" if major else ""

    def here_name() -> str:
        """This machine's name in the lists of every machine."""
        return machines.here_name(socket.gethostname())

    def listed_links(me: Principal) -> list[str]:
        """The links whose lists join this machine's: for a user who logs in
        here and sees the whole machine."""
        return sorted(settings.link_targets) if me.kind == "user" and visible_servers(me) is None else []

    async def link_list(request: Request, me: Principal, name: str, path: str):
        """(what a link answered to GET path, "") or (None, why not), asked
        with me's name and role and in the page's language."""
        link = settings.link_targets.get(name) if name in listed_links(me) else None
        if link is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No link {name}")
        headers = {**link_headers(link, me.name, me.role), "Accept": "application/json",
                   "Accept-Language": request_language(request)}
        try:
            resp = await link_clients.client(link).get(path, headers=headers, timeout=LIST_TIMEOUT)
        except httpx.HTTPError as exc:
            return None, describe_error(exc)
        if resp.status_code == 404:
            return None, "its OmniaCLI is older: it has no such list yet"
        if resp.status_code != 200:
            return None, f"answered HTTP {resp.status_code}"
        try:
            return resp.json(), ""
        except ValueError:
            return None, "it did not answer JSON"

    def role_on(me: Principal, server: str) -> str | None:
        """me's role on server: its own; for a user of workspaces, its role in
        the server's workspace (None: none)."""
        if not me.scoped:
            return me.role
        workspace = settings.workspace_of(server)
        return me.workspace_role(workspace) if workspace else None

    def reaches(role: str | None, needed: str) -> bool:
        return role is not None and ACCESS_ROLES.index(role) >= ACCESS_ROLES.index(needed)

    def sees_backups(me: Principal) -> bool:
        return any(reaches(r, "developer") for _, r in me.workspaces) if me.scoped else me.can("developer")

    def new_instance_here(me: Principal) -> bool:
        """New instance (a version only): the machine's admins, workspaces allowed here."""
        return (visible_servers(me) is None and me.can("admin") and bool(settings.storage_roots)
                and workspaceops.docker_missing() is None)

    @app.get("/instances")
    async def instances_page(request: Request, me: Principal = Depends(current_user)):
        """The instances of every machine: this one's and each link's, their
        stagings under them, whether each production backs up by itself."""
        links = listed_links(me)
        return _TEMPLATES.TemplateResponse(request, "instances.html", {
            "here_name": here_name(), "links": links, "quick_instance": new_instance_here(me),
            # the new instance on a link's machine too (the dialog asks each one its versions)
            "ni_links": links if me.can("admin") else []})

    @app.get("/api/instances")
    async def instances_list(request: Request, link: str = "", me: Principal = Depends(current_user)):
        """The instances of this machine ({name, servers, links}: the rows of
        /api/servers, the links whose lists join it), or of one link
        ({name, link, servers} or {name, link, error})."""
        if not link:
            return {"name": here_name(), "link": "", "servers": await server_list(request), "links": listed_links(me)}
        found, why = await link_list(request, me, link, "/api/servers")
        if found is not None and not isinstance(found, list):
            found, why = None, "it did not answer a list"
        if found is None:
            return {"name": link, "link": link, "error": i18n.tr(request_language(request), why)}
        return {"name": link, "link": link, "servers": found}

    def backup_rows(request: Request, me: Principal) -> dict:
        """The backups of this machine me may see, the newest first, with the
        automatic runs that did not end well as rows of their own, and the
        count of the runs of the last day."""
        lang = request_language(request)
        known = stagings.load(stagings_file)
        since = (datetime.datetime.now() - TONIGHT).isoformat(timespec="seconds")
        rows, tonight = [], {"ok": 0, "failed": 0}
        for srv in settings.active_servers():
            mine = role_on(me, srv.name)
            if not reaches(mine, "developer"):
                continue
            for role, inst in srv.instances.items():
                if not inst.plugin.has_backup:
                    continue
                folder = backup_folder(inst)
                common = {"server": srv.name, "role": role, "label": inst.domain or inst.site or srv.name,
                          "version": odoo_version(srv.name), "staging": srv.name in known}
                # a staging of the production with the database of the file (D6)
                stage = (reaches(mine, "admin") and role == "production" and srv.name not in known
                         and workspace_of(srv.name) is not None and inst.plugin.has_restore)
                for p in (sorted(folder.glob("*.zip")) if folder.is_dir() else []):
                    stat = p.stat()
                    rows.append({**common, "file": p.name, "size": stat.st_size,
                                 "time": datetime.datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                                 "kind": ("scheduled" if p.name.startswith(schedules.SCHEDULED_PREFIX)
                                          else "pre-update" if p.name.startswith(ops.PRE_UPDATE_PREFIX) else "manual"),
                                 "archive": p.name.startswith(instancearchive.PREFIX)
                                 or p.name.startswith(schedules.SCHEDULED_PREFIX + instancearchive.PREFIX),
                                 "download": f"/backups/{srv.name}/{role}/{p.name}",
                                 "verified": backupcheck.state(folder, p.name).get("verified"),
                                 "can_delete": reaches(mine, backup_role(inst)), "can_stage": stage})
                sch = scheduler.schedules.get(backup_schedule_name(inst))
                for run in scheduler.history.runs(sch.name, jobs) if sch is not None else []:
                    done = run.get("status")
                    if str(run.get("time") or "") >= since and done in (SUCCESS, *BACKUP_NOT_DONE):
                        tonight["ok" if done == SUCCESS else "failed"] += 1
                    if done in BACKUP_NOT_DONE:
                        # of the last day: what the count of the page's head filters
                        rows.append({**common, "failed": True, "time": run.get("time"), "status": done,
                                     "tonight": str(run.get("time") or "") >= since,
                                     "job": run.get("job"), "message": i18n.tr(lang, run.get("message") or "")})
        rows.sort(key=lambda r: str(r.get("time") or ""), reverse=True)
        return {"rows": rows, "tonight": tonight}

    @app.get("/backups")
    async def backups_page(request: Request, me: Principal = Depends(current_user)):
        """The backups of every machine: this one's and each link's."""
        if not sees_backups(me):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "The developer role is required")
        return _TEMPLATES.TemplateResponse(request, "backups.html", {"here_name": here_name(), "links": listed_links(me)})

    @app.get("/api/backups")
    async def backups_list(request: Request, link: str = "", me: Principal = Depends(current_user)):
        """The backups of this machine ({name, rows, tonight, links}), or of
        one link ({name, link, rows, tonight} or {name, link, error})."""
        if not sees_backups(me):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "The developer role is required")
        if not link:
            return {"name": here_name(), "link": "", **backup_rows(request, me), "links": listed_links(me)}
        found, why = await link_list(request, me, link, "/api/backups")
        if found is not None and not (isinstance(found, dict) and isinstance(found.get("rows"), list)):
            found, why = None, "it did not answer a list"
        if found is None:
            return {"name": link, "link": link, "error": i18n.tr(request_language(request), why)}
        return {"name": link, "link": link, "rows": found["rows"], "tonight": found.get("tonight") or {}}

    @app.post("/api/servers/{server}/stagings/from-backup", dependencies=[Depends(admin)])
    async def staging_from_backup(server: str, payload: dict = Body(...), me: Principal = Depends(current_user)):
        """A new staging of server's production with the database of one of
        its backups, neutralized (UX-008 D6): new_staging, its load the
        restore of that file. The production and the file do not change."""
        srv = settings.servers.get(server)
        if srv is None or srv.production is None:
            return refuse(f"No server {server} with a production", status.HTTP_404_NOT_FOUND)
        source = srv.production
        name = str(payload.get("file") or "")
        folder = backup_folder(source).resolve()
        path = (folder / name).resolve()
        if not _BACKUP_NAME.fullmatch(name) or path.parent != folder or not path.is_file():
            return refuse("No such backup", status.HTTP_404_NOT_FOUND)
        tried = backupcheck.state(folder, name).get("verified")
        if tried and not tried.get("ok"):
            try:
                when = datetime.datetime.fromisoformat(str(tried.get("time"))).strftime("%d/%m/%Y")
            except ValueError:
                when = str(tried.get("time") or "")
            return refuse(f"This backup did not restore when it was tried on {when}: choose another one")
        archive = instancearchive.read_manifest(path) is not None
        problem = None if archive else source.plugin.backup_problem(path)
        if problem:
            return refuse(problem)

        async def restore_the_file(job, prod, target) -> dict:
            job.log(f"--- the backup {name} of {prod.key} into {target.key}, neutralized")
            if archive:
                with instancearchive.backup_of(path) as inner:
                    await restore_fetched(job, target, inner, {"neutralize": True})
            else:
                await target.plugin.restore(job, target, path, {"neutralize": True})
            return {"backup": name, "from_backup": True}

        return await new_staging(server, me, load=restore_the_file,
                                 title=f"Staging of {server} from the backup {name}")

    # --- the caller's own password --------------------------------------

    def password_user(me: Principal) -> Principal:
        if me.kind != "user":
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Only a user who logs in has an account")
        return me

    @app.get("/account")
    async def account_page(request: Request, me: Principal = Depends(current_user)):
        password_user(me)
        return _TEMPLATES.TemplateResponse(
            request, "account.html",
            {"name": me.name, "role": WORKSPACE_ROLE if me.scoped else me.role, "workspaces": me.workspaces,
             "fixed": me.name in settings.fixed.users},
        )

    @app.post("/api/account/password")
    async def account_password(request: Request, payload: dict = Body(...), me: Principal = Depends(current_user)):
        password_user(me)
        current, password = str(payload.get("current") or ""), str(payload.get("password") or "")
        async with access_lock:
            if me.name in settings.fixed.users:
                return refuse("Your user is set in the main configuration: change its password there")
            if not await asyncio.to_thread(verify_password, current, settings.users.get(me.name, "")):
                return refuse("The current password is wrong")
            if len(password) < _MIN_PASSWORD:
                return refuse(f"The password needs at least {_MIN_PASSWORD} characters")
            web = web_copy()
            web.users[me.name] = await asyncio.to_thread(hash_password, password)
            refused = await store_access(web)
            if refused:
                return refused
            # the new password ends every session of the user: a new one for this browser
            response = JSONResponse({"ok": True})
            set_session_cookie(request, response, sessions.create(me.name, settings.users[me.name]))
            return response

    return app


def app_from_env() -> FastAPI:
    """uvicorn factory: `uvicorn --factory omniacli.app:app_from_env`."""
    return create_app(load_settings(os.environ.get("OMNIACLI_CONFIG", "omniacli.yaml")))
