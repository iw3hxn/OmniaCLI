# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The OmniaCLI web application."""

from __future__ import annotations

import asyncio
import configparser
import contextlib
import csv
import dataclasses
import datetime
import functools
import hashlib
import importlib.metadata
import importlib.util
import io
import ipaddress
import json
import logging
import os
import pwd
import re
import secrets
import shutil
import socket
import tempfile
import time
import types
import zipfile
from dataclasses import asdict
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, quote, urlsplit

import httpx
import jinja2
from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketException, status
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask
from starlette.requests import ClientDisconnect, HTTPConnection

from . import (backupcheck, backupchoices, checks, deployhooks, destinations, enterprise, instancearchive, private, restoreroom, consumer, disks, gitcredentials, linkcopy, listgroups, ops, packages, plugins, pluginstore, puller, publisher,
               schedules, selflog, sqldump, sshlinks, shell, stacktemplates, templatestore, tunnel, workspacehelper, workspaceops)
from .accesslog import REQUEST as AUDIT_REQUEST, AccessLog
from . import audit, siem
from . import i18n, loginas, machines, modulecatalog, pgreport, pgtuning, stagings, workspaceweb
from . import clusterpitr, inframap, linkedcluster, machinedb, managedreplica
from . import stagingmail
from . import moduleupload, sshkeys
from . import dumpinfo
from . import totp
from . import uploads as uploads_mod
from .uploads import Uploads
from .diskhistory import DiskHistory, DiskRecorder
from .customerview import CustomerView
from .auth import ROLES as ACCESS_ROLES
from .auth import WORKSPACE_ROLE, Authenticator, Principal, hash_password, hash_token, new_token, verify_password
from .auth import unknown_password
from . import notify
from .config import INVITATION_HOURS, Invitation, invitations_file_of, load_invitations, save_invitations
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
    AUDIT_RETENTION_LIMITS,
    SYSLOG_PROTOCOLS,
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
    ADDRESSES_MAX,
    address_name,
    addresses_follow,
    address_taken,
    apply_sites,
    automatic_name,
    environment_label,
    environment_names,
    generated_names,
    load_web_names,
    web_names,
    save_pullers,
    save_servers,
    save_workspaces,
    server_text,
    server_to_dict,
)
from .jobs import INTERRUPTED, STARTED as JOBS_STARTED, SUCCESS, JobFailed, JobManager, filter_jobs
from .plugin_api import ENTRY_POINT_GROUP, PLUGIN_API, ActionRefused, Host, restore_fetched
from .pullcrypto import new_key
from .links import LIST_TIMEOUT, LinkClients, describe_error, link_headers, proxy_websocket
from .monitor import FILE as MONITOR_FILE, Monitor
from . import logtail
from .logtail import read_since
from .metrics import Sampler
from . import metricshistory
from .metricshistory import MetricsHistory, MetricsRecorder
from .scheduler import AutoUpdater
from .sessions import COOKIE, LIFETIME, LoginLimiter, SessionStore


def request_language(request: HTTPConnection) -> str:
    """The language of the page (i18n.py): the cookie, else the browser's."""
    return i18n.choose(request.cookies.get(i18n.COOKIE), request.headers.get("accept-language"))


def checks_said(request: Request, report) -> list[dict]:
    """The checks of a server (checks.py) as the reader's language says them:
    what is checked and its result; the rest of each as it is."""
    lang = request_language(request)
    said = []
    for check in report:
        data = check.to_dict()
        for key in ("name", "detail"):
            if isinstance(data.get(key), str):
                data[key] = i18n.tr(lang, data[key])
        said.append(data)
    return said


def menu_places(user) -> list[str]:
    """The places of the top menu user sees (MENU_PLACES), as base.html draws them."""
    places = ["servers"] if user.can("admin") and not user.scoped else []
    places.append("instances")
    if (any(r in ("developer", "admin") for _, r in user.workspaces) if user.scoped else user.can("developer")):
        places.append("backups")
    if user.can("developer") and not user.scoped:
        places.append("settings")
    return places


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
        # the menu of the OmniaCLI showing this page through its link (its
        # places, _MENU_HEADER), None when it sent none: then this machine's
        "central_menu": getattr(request.state, "central_menu", None),
        # who looks through that link
        "central_user": user.on_behalf if user and user.kind == "link" else "",
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
# a backup zip (verify, destinations, downloads of zips only); a restore
# also takes a SQL dump (sqldump.RESTORE_NAME)
_BACKUP_NAME = re.compile(r"[A-Za-z0-9._-]+\.zip")
# where an MCP client PUTs the database its upload_backup asked for
UPLOAD_ROUTE = "/api/uploads/{ticket}"
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
# the places of the top menu of the OmniaCLI showing our pages, those its
# user sees there (comma separated): our pages draw that menu, its own links
# without our prefix, so the menu stays the viewer's inside a linked machine.
# An older OmniaCLI on either side sends or reads none: each page its own menu
_MENU_HEADER = "x-omniacli-menu"
MENU_PLACES = ("servers", "instances", "backups", "settings")
_ON_BEHALF_HEADER = "x-omniacli-user"
# the role of that user there: a link acts with the lower of it and its own
_ROLE_CAP_HEADER = "x-omniacli-role-cap"
_PREFIX = re.compile(r"(/[A-Za-z0-9._~-]+)+")
_ON_BEHALF = re.compile(r"[A-Za-z0-9._@-]{1,64}")
# users, links and MCP tokens created from the web interface
_ACCESS_NAME = re.compile(r"[A-Za-z0-9._@-]{1,64}")
# why a name of the Addresses dialog is taken, when it is so outside the
# workspace: a user of workspaces is told "not available" only
ADDRESS_TAKEN_ELSEWHERE = frozenset({"it is under the domain of another workspace of this machine",
                                     "another workspace of this machine answers on it",
                                     "another site of this machine answers on it"})
# a name taken, as a user of workspaces is told: not where, not by whom
NAME_NOT_AVAILABLE = "This name is not available: choose another one"
# a user of the machine (not of a workspace) at the customers' portal, while
# Options do not let them in there (docs/portale-clienti.md)
PORTAL_STAFF_REFUSED = ("This address is the customers' portal: the users of this machine come in through "
                        "their link, not from here")
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
    # its two-step verification, and its workspace's admins' reset of a member's
    "/api/account/second-factor/start", "/api/account/second-factor/confirm",
    "/api/account/second-factor/disable", "/api/workspaces/{workspace}/members/second-factor/reset",
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
    "/servers/{server}/{role}/log", "/api/servers/{server}/{role}/log", "/api/servers/{server}/{role}/log/records",
    "/workspaces/{workspace}", "/api/workspaces/{workspace}/members/save",
    "/api/workspaces/{workspace}/members/delete", "/api/workspaces/{workspace}/members/invitation/resend",
    "/api/workspaces/{workspace}/members/invitation/revoke", "/api/workspaces/{workspace}/instances/create",
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
    # the New staging dialog (UX-008 §5.1): here only, no choice of the machine
    "/api/servers/{server}/stagings/new", "/api/servers/{server}/stagings/elsewhere",
    "/api/servers/{server}/{role}/deploy-hook", "/api/servers/{server}/pitr", "/api/servers/{server}/pitr/enable",
    "/api/servers/{server}/pitr/base", "/api/servers/{server}/pitr/restore",
    "/workspaces/{workspace}/destinations", "/api/workspaces/{workspace}/destinations/save",
    "/api/workspaces/{workspace}/destinations/delete", "/api/workspaces/{workspace}/destinations/check",
    "/api/workspaces/{workspace}/destinations/key",
    "/api/workspaces/{workspace}/load",
    # its access log (NIS 2), for its admins: its entries only
    "/workspaces/{workspace}/audit", "/api/workspaces/{workspace}/audit/export",
    # the lists of every machine (UX-008 §4): of its workspaces' instances only
    "/instances", "/api/instances", "/backups", "/api/backups", "/api/servers/{server}/stagings/from-backup",
    # the map of the infrastructure (UX-009): the same instances as a diagram, its workspaces only
    "/api/map",
    # a staging's caught mail (UX-008 §5.3)
    "/api/servers/{server}/production/mail", "/api/servers/{server}/production/mail/{mail_id}",
    "/api/servers/{server}/production/mail/clear",
    # and its incoming mail (UX-008 §5.4)
    "/api/servers/{server}/production/mail/inbox", "/api/servers/{server}/production/mail/send",
    # the Addresses dialog of an environment (UX-008 §6.1): its admins (admin)
    "/api/servers/{server}/{role}/addresses", "/api/servers/{server}/{role}/addresses/check",
    "/api/servers/{server}/{role}/addresses/save",
    # the Modules tab: the catalog of the OCA, with the role in the workspace (UX-008 §7)
    "/api/servers/{server}/modules", "/api/servers/{server}/modules/catalog", "/api/servers/{server}/modules/plan",
    "/api/servers/{server}/{role}/modules", "/api/servers/{server}/{role}/modules/install",
    # modules uploaded as a .zip (README, Modules uploaded as a .zip): its admins, as a repository
    "/api/servers/{server}/modules/upload", "/api/servers/{server}/modules/upload/{ident}/apply",
    "/api/servers/{server}/modules/uploaded",
    # the PUT of an upload_backup URL (a workspace's token, for its workspace)
    UPLOAD_ROUTE,
    # what the backup and restore dialogs show (sizes only), with the role
    # that may back up there (developer)
    "/api/servers/{server}/{role}/backup/estimate", "/api/servers/{server}/{role}/restore/room",
    # a backup of its instance restored by its admin, with a backup made just
    # before (action_restore); never the upload of a file (the machine's admins)
    "/servers/{server}/{role}/restore",
    # Log in as on its instances: its admin (from_link_role), a person who
    # logs in here; not the nginx line (the machine's)
    "/api/servers/{server}/{role}/login-as/users", "/api/servers/{server}/{role}/login-as",
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


async def _audit_body(request: Request) -> dict:
    """A small JSON request's body, for what the access log says of it ({}
    for any other)."""
    if "json" not in request.headers.get("content-type", ""):
        return {}
    length = request.headers.get("content-length", "")
    if not length.isdigit() or int(length) > _AUDIT_MAX_BODY:
        return {}
    try:
        body = json.loads(await request.body())
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


async def _audit_target(request: Request) -> str:
    """ name=... role=... of a small JSON request, for the access log."""
    body = await _audit_body(request)
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
    # the machine's load, a point every 5 minutes for 14 days, next to the disks' history
    load_history = MetricsHistory(settings.jobs_dir.parent / "load_history.json")
    load_recorder = MetricsRecorder(sampler, load_history)
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

    def audit_fields(scope: dict) -> dict:
        """What an entry recorded while serving scope says of it: who with
        which role, the route, and the server, environment and workspace."""
        user = (scope.get("state") or {}).get("user")
        params = scope.get("path_params") or {}
        fields = {"method": scope.get("method") or ("WEBSOCKET" if scope.get("type") == "websocket" else ""),
                  "route": audit.route_template(scope)}
        if isinstance(user, Principal):
            fields.update(role=user.role, on_behalf=user.on_behalf)
        server, workspace = str(params.get("server") or ""), str(params.get("workspace") or "")
        if server:
            fields["server"] = server
            workspace = workspace or settings.workspace_of(server) or ""
        if params.get("role"):
            fields["env"] = str(params["role"])
        if not workspace and params.get("job_id"):
            job = jobs.get(str(params["job_id"]))
            if job is not None:
                workspace = job.workspace or next(
                    (ws for ws in map(settings.workspace_of, job.servers) if ws), "")
        fields["workspace"] = workspace
        return fields

    def audit_scope_of(kind: str, name: str) -> list[str]:
        """The workspaces an entry of no workspace belongs to: those of a user
        of workspaces (its logins), of a workspace's token."""
        if kind == "user" and name in settings.users and settings.user_role(name) == WORKSPACE_ROLE:
            return [ws for ws, _ in settings.memberships(name)]
        if kind in ("token", "mcp") and settings.token_workspace(name):
            return [settings.token_workspace(name)]
        return []

    def audit_job_ended(job) -> None:
        """The outcome of a job, after the entry of the request that started it."""
        workspace = job.workspace or next((ws for ws in map(settings.workspace_of, job.servers) if ws), "")
        access_log.record("job", "job", job.source, "",
                          f"{job.title}: {job.status}" + (f" ({job.error})" if job.error else ""),
                          context=False, job=[job.id], workspace=workspace, server=",".join(job.servers),
                          status=job.status)

    siem_forwarder = siem.Forwarder(lambda text: access_log.record("siem", "system", "", "", text, context=False))

    def apply_audit_options() -> None:
        options = settings.options
        access_log.retention_days = options.audit_retention_days
        siem_forwarder.configure(options.syslog_host, options.syslog_port, options.syslog_protocol,
                                 options.syslog_ca_file)

    access_log.enrich, access_log.scope_of, access_log.forward = audit_fields, audit_scope_of, siem_forwarder.send
    jobs.listeners.append(audit_job_ended)
    apply_audit_options()
    # OmniaCLI's own log (selflog), shown to the admins
    own_log_file = settings.logs_dir / "omniacli.log"
    # the browsers' logins: next to the jobs, like the access log
    sessions = SessionStore(settings.jobs_dir.parent / "sessions.json")
    tunnels = tunnel.TunnelManager(
        lambda: settings.options, link_headers,
        lambda user, detail: access_log.record("tunnel", "user", user, "", detail))
    limiter = LoginLimiter()
    # two-step verification (totp.py): the users' second factors, next to the
    # users file; the logins still owing their code; the secrets being set up
    # from the Account page ({user: (secret, until)})
    second_factors = totp.SecondFactors(
        settings.users_file.with_name("second-factor.json") if settings.users_file
        else settings.jobs_dir.parent / "second-factor.json")
    pending_logins = totp.PendingLogins()
    account_setups: dict[str, tuple[str, float]] = {}
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

    # --- the customers' portal (docs/portale-clienti.md): this OmniaCLI on a
    # name of the machine, published by the root helper (workspacehelper.portal)

    def portal_url() -> str:
        """https://<the portal's address>, "" when it has none."""
        address = settings.options.portal_address
        return f"https://{address}" if address else ""

    def on_portal(request: HTTPConnection) -> bool:
        """The request came through the portal's site: nginx passes its name as
        Host (a link, an SSH tunnel, a browser on the machine reach the service
        by its own address)."""
        address = settings.options.portal_address
        host = request.headers.get("host", "").lower()
        return bool(address) and (host == address or host == f"{address}:443")

    def portal_admits(request: HTTPConnection, name: str) -> bool:
        """name may log in where this request came from: on the portal, the
        users of the workspaces only, unless the machine's users were let in
        there too (Options); everywhere else, everyone as before."""
        return (not on_portal(request) or settings.options.portal_staff
                or settings.user_role(name) == WORKSPACE_ROLE)

    def second_step_required(request: HTTPConnection, name: str) -> bool:
        """name must log in with a second step here (Options, Two-step
        verification): everybody; or, with the portal's policy and once the
        machine has a portal, whoever logs in at its address and the users of
        the workspaces wherever they log in (the machine's own address is no
        way around it). No portal address: nobody, as before."""
        policy = settings.options.second_factor
        if policy == "everybody":
            return True
        if policy == "portal" and settings.options.portal_address:
            return on_portal(request) or settings.user_role(name) == WORKSPACE_ROLE
        return False

    def public_base(request: Request) -> str:
        """Where a person or a client reaches this OmniaCLI: the portal when it
        has an address, else the address of this request."""
        return portal_url() or str(request.base_url).rstrip("/")

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
                # the one write a token makes outside /mcp: the PUT of the
                # upload URL its upload_backup got (one time, for it only)
                upload = method == "PUT" and getattr(request.scope.get("route"), "path", "") == UPLOAD_ROUTE
                if method not in ("GET", "HEAD") and not upload:
                    raise HTTPException(status.HTTP_403_FORBIDDEN, "A token may only read")
                workspace = settings.token_workspace(name)
                if workspace:
                    # a workspace's token (its admin made it for an assistant) reads
                    # that workspace only, with its role there, as a user of it does
                    # (in_workspaces): on 21/11/2026 it read the whole machine over
                    # REST, the other customers' backups too, while MCP kept it in
                    return Principal(name, "user", "token", workspaces=((workspace, settings.token_role(name)),),
                                     scoped=True)
                return Principal(name, settings.token_role(name), "token")
            name = authenticator.link_name(token)
            if name is None:
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token",
                                    headers={"WWW-Authenticate": "Bearer"})
            # a linked instance: no browser sends its token by itself, so no
            # cross-site check; it serves our pages under its own prefix
            prefix = request.headers.get(_PREFIX_HEADER, "")
            request.state.prefix = prefix if _PREFIX.fullmatch(prefix) else ""
            menu = request.headers.get(_MENU_HEADER)
            if menu is not None and request.state.prefix:
                request.state.central_menu = tuple(p for p in MENU_PLACES if p in menu.split(","))
            on_behalf = request.headers.get(_ON_BEHALF_HEADER, "")
            role = settings.links[name].role
            cap = request.headers.get(_ROLE_CAP_HEADER, "")
            if cap in ACCESS_ROLES and ACCESS_ROLES.index(cap) < ACCESS_ROLES.index(role):
                role = cap
            return Principal(name, role, "link", on_behalf if _ON_BEHALF.fullmatch(on_behalf) else "",
                             person=menu is not None and bool(request.state.prefix))
        # a browser: the session of the login form
        name = sessions.user(request.cookies.get(COOKIE), settings.users)
        if name is None:
            # marked, so the pages' scripts tell it from a link's or a token's 401
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Login required", headers={_LOGIN_HEADER: "required"})
        if method not in ("GET", "HEAD") and not _same_origin(request):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Cross-site request refused")
        if not portal_admits(request, name):
            raise HTTPException(status.HTTP_403_FORBIDDEN, PORTAL_STAFF_REFUSED)
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

    def customer_view(request: HTTPConnection) -> CustomerView | None:
        """For a user of workspaces, what takes the integrator's names and the
        machine's paths out of the jobs it reads (customerview.py); None for
        the machine's users: they read everything."""
        user = getattr(request.state, "user", None)
        if user is None or not user.scoped:
            return None
        mine = {ws for ws, _ in user.workspaces}
        own = {n for ws in settings.workspaces.values() if ws.name in mine for n in (ws.name, *ws.servers)}
        members = {m for ws in settings.workspaces.values() if ws.name in mine for m in ws.members}
        machines_ = set(settings.link_targets) | set(settings.connections)
        for target in settings.link_targets.values():
            host = urlsplit(target.url).hostname
            if host:
                machines_.add(host)
            if target.ssh is not None and target.ssh.host:
                machines_.add(target.ssh.host)
        lang = request_language(request)
        provider = settings.options.provider_label or i18n.gettext(lang, "the provider")
        # a name of its own (a server, a workspace, a member) is never taken out
        return CustomerView(provider, i18n.gettext(lang, "another machine"),
                            links=frozenset(set(settings.links) - own - members),
                            machines=frozenset(machines_ - own - members - {"localhost", "127.0.0.1"}),
                            members=frozenset(members),
                            tokens=frozenset(n for n in settings.tokens if settings.token_workspace(n) in mine))

    def shown_jobs(request: HTTPConnection, found: list, lines: bool = False) -> list:
        """The jobs as the caller reads them (customer_view); a list's
        without their lines (lines: a job's page)."""
        view = customer_view(request)
        return found if view is None else [view.job(j, lines) for j in found]

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
                                            lambda key: monitor.view(key),
                                            # defined further down, with the workspaces' code
                                            {"upload": lambda *a, **k: upload_ticket(*a, **k),
                                             "restore": lambda *a, **k: restore_new(*a, **k)})

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
        pitr_tasks.append(asyncio.get_running_loop().create_task(cluster_sweep()))
        pitr_tasks.append(asyncio.get_running_loop().create_task(names_that_open()))
        pitr_tasks.append(asyncio.get_running_loop().create_task(interrupted_names_freed()))
        postgres_tasks.append(asyncio.get_running_loop().create_task(postgres_loop()))
        # defined further down, with the Database tab's code
        replica_tasks.append(asyncio.get_running_loop().create_task(replica_loop()))
        replica_tasks.append(asyncio.get_running_loop().create_task(moved_loop()))
        catalog_tasks.append(asyncio.get_running_loop().create_task(catalog_loop()))
        disk_recorder.start()
        load_recorder.start()
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
                           ("disk history", disk_recorder.stop), ("load history", load_recorder.stop),
                           ("load sampler", sampler.stop),
                           ("monitor", monitor.stop), ("point-in-time recovery", stop_pitr),
                           ("planned database restarts", stop_postgres),
                           ("the look at the read replicas", lambda: stop_replicas()),
                           ("nightly catalog of modules", stop_catalog),
                           # defined further down, with the map's code
                           ("the map's readings", lambda: stop_map()),
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
    app.state.link_clients = link_clients
    app.state.sampler = sampler
    app.state.disk_history, app.state.disk_recorder = disk_history, disk_recorder
    app.state.load_history, app.state.load_recorder = load_history, load_recorder
    app.state.access_log = access_log
    app.state.tunnels = tunnels
    app.state.ssh_tunnels = ssh_tunnels
    app.state.pullers, app.state.publications, app.state.consumers = pullers, publications, consumers
    app.state.sessions, app.state.authenticator = sessions, authenticator
    app.state.second_factors = second_factors

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
        in the access log: a route added later is logged by itself; of the
        reads, those of customer or security data (audit.SENSITIVE_READS),
        the same page at most once in READ_GAP."""
        reading = request.method in ("GET", "HEAD")
        if not reading and _NOT_AUDITED.fullmatch(request.url.path):
            return await call_next(request)
        body = {} if reading else await _audit_body(request)
        started: list[str] = []
        mark = JOBS_STARTED.set(started)
        try:
            response = await call_next(request)
        finally:
            JOBS_STARTED.reset(mark)
        user = getattr(request.state, "user", None)
        if user is None:
            return response
        ip = request.client.host if request.client else ""
        who = f" for {user.on_behalf}" if user.on_behalf else ""
        template = audit.route_template(request.scope)
        if reading:
            what = audit.SENSITIVE_READS.get(template)
            if what is None or not access_log.first_read((user.kind, user.name, ip, request.url.path)):
                return response
            access_log.record("read", user.kind, user.name, ip,
                              f"{what}: {request.method} {request.url.path} -> {response.status_code}{who}"[:300],
                              what=what, status=response.status_code)
            return response
        target = "".join(f" {key}={body[key]}" for key in _AUDIT_FIELDS if isinstance(body.get(key), (str, int)))
        extra = {"status": response.status_code, "job": started}
        named = str(body.get("server") or "") if isinstance(body.get("server"), str) else ""
        if named and not request.scope.get("path_params", {}).get("server"):
            extra.update(server=named, workspace=settings.workspace_of(named) or "")
        access_log.record("action", user.kind, user.name, ip,
                          f"{request.method} {request.url.path}{target} -> {response.status_code}{who}"[:300],
                          **extra)
        return response

    class AuditScope:
        """The request (or websocket) being served, for what the access log
        records while serving it (accesslog.REQUEST)."""

        def __init__(self, inner):
            self.inner = inner

        async def __call__(self, scope, receive, send):
            if scope["type"] not in ("http", "websocket"):
                return await self.inner(scope, receive, send)
            mark = AUDIT_REQUEST.set(scope)
            try:
                await self.inner(scope, receive, send)
            finally:
                AUDIT_REQUEST.reset(mark)

    app.add_middleware(AuditScope)

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
        if (exc.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)
                and "text/html" in request.headers.get("accept", "")
                and not request.headers.get("authorization")):
            # a page the browser opened (or a form it sent): a page in the
            # site's layout and language with the way back, not the JSON of
            # an API's caller (a script's fetch, a token, a link: their JSON)
            if getattr(request.state, "user", None) is None:
                with contextlib.suppress(HTTPException):
                    # the menus of who it is (refused before current_user kept it)
                    request.state.user = identify(request)
            return _TEMPLATES.TemplateResponse(request, "error.html",
                                               {"code": exc.status_code, "detail": str(exc.detail or "")},
                                               status_code=exc.status_code, headers=getattr(exc, "headers", None))
        return await http_exception_handler(request, exc)

    async def public_file(request: Request):
        name, media_type = _PUBLIC_FILES[request.url.path]
        return FileResponse(_STATIC / name, media_type=media_type,
                            headers={"Cache-Control": "public, max-age=86400"})

    def login_page(request: Request, next_path: str, error: str = "", code: int = 200, **step):
        return _TEMPLATES.TemplateResponse(request, "login.html", {"next": next_path, "error": error, "step": "", **step},
                                           status_code=code, headers={"Cache-Control": "no-store"})

    def needs_second_step(request: Request, name: str) -> bool:
        return second_factors.on(name) or second_step_required(request, name)

    def to_second_step(request: Request, name: str, next_path: str, via: str) -> RedirectResponse:
        """The password was right: the code before the session, or first its
        set-up when the user must and has none (secret: the pending's)."""
        setup = not second_factors.on(name)
        token = pending_logins.create(name, next_path, settings.users[name], setup, via)
        response = RedirectResponse("/login/setup" if setup else "/login/code", status.HTTP_303_SEE_OTHER)
        response.set_cookie(totp.STEP_COOKIE, token, max_age=totp.PENDING_SECONDS, httponly=True, samesite="strict",
                            secure=request.url.scheme == "https", path="/login")
        return response

    def step_page(request: Request, found: totp.Pending, error: str = "", code: int = 200, recovery=None):
        if recovery:
            return login_page(request, found.next_path, step="recovery", user=found.user, recovery=recovery)
        if not found.setup:
            return login_page(request, found.next_path, error, code, step="code", user=found.user)
        uri = totp.otpauth_uri(found.secret, f"{found.user}@{request.url.hostname}")
        return login_page(request, found.next_path, error, code, step="setup", user=found.user, uri=uri,
                          secret=found.secret, qr=totp.qr_svg(uri))

    async def login_step(request: Request):
        """/login/code and /login/setup: the second step of a login whose
        password was right (its cookie), the session only after it. A wrong
        code counts with the failed logins; MAX_TRIES of them, the password again."""
        token = request.cookies.get(totp.STEP_COOKIE)
        found = pending_logins.get(token, settings.users)
        if found is None:
            # none, ended, used up, or the password changed meanwhile: the password again
            return RedirectResponse("/login", status.HTTP_303_SEE_OTHER)
        if found.setup != (request.url.path == "/login/setup"):
            return RedirectResponse("/login/setup" if found.setup else "/login/code", status.HTTP_303_SEE_OTHER)
        if request.method == "GET":
            return step_page(request, found)
        if not _same_origin(request):
            return HTMLResponse("Cross-site login refused", status_code=403)
        form = parse_qs((await request.body()).decode("utf-8", "replace"))
        code = (form.get("code") or [""])[0].strip()[:64]
        ip = request.client.host if request.client else ""
        name = found.user
        wait = limiter.wait(ip, name)
        if wait:
            access_log.record("failed", "user", name, ip, f"second step: too many failed logins: wait {wait} s")
            return step_page(request, found, f"Too many failed logins: try again in {wait} seconds.", 429)
        used = None
        if found.setup:
            used = totp.code_step(found.secret, code)
            result = None if used is None else ("code", totp.RECOVERY_CODES)
        else:
            result = await asyncio.to_thread(second_factors.check, name, code)
        if result is None:
            limiter.failed(ip, name)
            left = pending_logins.failed(token)
            access_log.record("failed", "user", name, ip,
                              "second step: wrong code" + ("" if left else ", the password is asked again"))
            if not left:
                response = login_page(request, found.next_path, "Too many wrong codes: log in again with your password.", 401)
                response.delete_cookie(totp.STEP_COOKIE, path="/login")
                return response
            return step_page(request, found, "Wrong code.", 401)
        limiter.succeeded(ip, name)
        pending_logins.drop(token)
        role = settings.user_role(name)
        codes = None
        if found.setup:
            codes = await asyncio.to_thread(second_factors.enable, name, found.secret, used)
            access_log.record("second-factor", "user", name, ip, "switched on at the login (required here)", role=role)
        elif result[0] == "recovery":
            access_log.record("second-factor", "user", name, ip, f"recovery code used to log in: {result[1]} left",
                              role=role)
        access_log.record("login", "user", name, ip,
                          f"{found.via}, second step: {'recovery code' if result[0] == 'recovery' else 'code'}",
                          role=role)
        # the recovery codes of a set-up: shown once, on the way to the page asked
        response = (step_page(request, found, recovery=codes) if codes
                    else RedirectResponse(found.next_path, status.HTTP_303_SEE_OTHER))
        response.delete_cookie(totp.STEP_COOKIE, path="/login")
        set_session_cookie(request, response, sessions.create(name, settings.users[name]))
        return response

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
        if not portal_admits(request, name):
            # the password was right: said only once it is, as a wrong one would be otherwise
            access_log.record("failed", "user", name, ip, "login form: a user of the machine at the customers' portal")
            return login_page(request, next_path, PORTAL_STAFF_REFUSED + ".", 403)
        if needs_second_step(request, name):
            # the failed tries are forgotten only after the code: a known
            # password does not buy fresh tries at the codes
            return to_second_step(request, name, next_path, "login form")
        limiter.succeeded(ip, name)
        access_log.record("login", "user", name, ip, "login form", role=settings.user_role(name))
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
    app.add_route("/login/code", login_step, methods=["GET", "POST"])
    app.add_route("/login/setup", login_step, methods=["GET", "POST"])

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
            return run_state_of(inst, None)
        return run_state_of(inst, await ops.service_state(inst))

    def run_state_of(inst: Instance, state: str | None) -> str:
        """run_state of the instance from the service state already asked
        (ops.service_state; not asked for a process OmniaCLI starts)."""
        if inst.restart.type == "process":
            return "running" if ops.managed_pid(inst) else "stopped"
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
            "jobs": shown_jobs(request, visible_jobs(me, jobs.recent(len(jobs.jobs)))[:10]),
            # a user of workspaces: the load of each of its workspaces instead
            "load_workspaces": sorted((ws, role) for ws, role in me.workspaces if ws in settings.workspaces) if me.scoped else [],
            # the Database tab (UX-008 §6.2): the machine's admins only
            "machine_database": shown is None and machine_admin(me),
            # the SSH access tab (README, SSH access): the machine's admins, a person through a link too
            "machine_ssh": shown is None and machine_admin(me) and not a_machine(me),
            # the access log's room, the clock, the SIEM (NIS 2): the machine's admins
            "audit": await asyncio.to_thread(audit_status) if shown is None and machine_admin(me) else None,
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
        # a systemd service: its ActiveState, None when unknown
        service_states = dict(zip([i.key for i in shown],
                                  await asyncio.gather(*(ops.service_state(i) for i in shown))))
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
        # Restore: for an admin who logs in here; an admin of the workspace
        # restores one of its backups (no upload), a backup made just before
        restore_file = {}
        for i in shown:
            if i.plugin.has_restore and me.kind == "user" and me.can("admin"):
                restore_file[i.key] = {
                    "url": f"{base}/{i.role}/restore",
                    "upload": None if me.scoped else f"{prefix_of(request)}/api/servers/{srv.name}/{i.role}/backups/upload",
                    "backups": f"{prefix_of(request)}/api/servers/{srv.name}/{i.role}/backups"
                               + ("?dumps=1" if i.plugin.restores_sql and not me.scoped else ""),
                    "sql": i.plugin.restores_sql and not me.scoped,
                    "backup_first": me.scoped,
                    "database": str(i.plugin.describe(i).get("database") or i.key)
                                if i.role == "production" or me.scoped else ""}
        # Log in as: a user who logs in here, with the role it needs
        login_as_buttons = {}
        for i in shown:
            if i.plugin.has_login and acts_for_a_person(me) and me.can(from_link_role(i)):
                api = f"{prefix_of(request)}/api/servers/{srv.name}/{i.role}/login-as"
                login_as_buttons[i.key] = {"users": f"{api}/users", "url": api, "key": i.key}
        name_problems = {}
        owner = workspace_of(srv.name)
        if owner is not None and owner[0].domain:
            for i in shown:
                found = await name_problem(owner[0], owner[1], i.role, environment_names(owner[0], owner[1], i.role))
                if found:
                    name_problems[i.key] = found
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
                # the workspace it is in: «On the map» opens its schema, where the node of this instance is
                "map_workspace": settings.workspace_of(srv.name),
                "login_as": login_as_buttons,
                # a name of its own the DNS does not lead here: said in the head, with the record to add
                "name_problems": name_problems,
                # its copies: its test and its stagings (made beside it in its
                # workspace); or the production it is a staging of
                "stagings": {"rows": await staging_states(staging_rows(srv.name)),
                             "can": request.state.user.can("admin") and workspace_of(srv.name) is not None
                             and srv.production is not None and not staging_of,
                             # what it is a copy of, at the address the page names it by (none
                             # when its production is on another machine: staging_of["machine"])
                             "parent": staging_of and {**staging_of, "url": None if staging_of.get("machine") else getattr(
                                 (settings.servers.get(staging_of.get("parent")) or srv).production, "web_url", None)},
                             "workspace": (workspace_of(srv.name) or (None,))[0],
                             # the links asked for the stagings of this production made on
                             # them (their /api/instances): for an admin of this machine
                             "elsewhere": bool(settings.link_targets) and chooses_machine(request.state.user)
                             and srv.production is not None and not staging_of,
                             },
                "is_admin_here": request.state.user.can("admin"),
                # the other names of each row of Environments, and its ⚙ Addresses… (UX-008 §6.1)
                "addresses": address_rows(request.state.user, [
                    (srv.name, r) for r in ROLES if getattr(srv, r) is not None]
                    + [(r["server"], "production") for r in staging_rows(srv.name)]),
                # a staging whose mail is caught: its Mail tab (UX-008 §5.3)
                "mail_capture": bool(staging_of) and await asyncio.to_thread(staging_mail, srv.name) is not None,
                # and its mailboxes Odoo reads, written to as an outside sender (UX-008 §5.4)
                "mail_inbox": bool(staging_of) and await asyncio.to_thread(staging_inbox, srv.name) is not None,
                # an Odoo older than the alias domain: why it has no mailboxes
                "mail_old_odoo": workspacehelper.without_alias_domain(
                    (await asyncio.to_thread(staging_mail_meta, srv.name))[1]) if staging_of else None,
                "mail_domain": stagingmail.domain_of(srv.name),
                "is_developer_here": request.state.user.can("developer"),
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
                # the Database row of Settings (UX-008 §6.2.6), from the tuning's record
                "db_line": {i.key: database_line(i) for i in shown},
                # its reads from the replica of the machine's cluster (UX-010 §5.4): the machine's admins only
                "replica_line": ({i.key: replica_offer_of(i) for i in shown}
                                 if machine_admin(request.state.user) else {}),
                "machine_admin": machine_admin(request.state.user),
                "repo_status": {i.key: s for i, s in zip(shown, statuses)},
                "repo_urls": repo_urls,
                "odoo_params": odoo_params, "param_role": param_role,
                # Odoo Enterprise (UX-012): its switch in Settings, its badge in the head
                "enterprise": await asyncio.to_thread(enterprise_line, request, srv, me),
                "package_state": {i.key: packages.installed_state(i.package) for i in shown if i.package},
                "auto_state": updater.state,
                "busy": {i.key: jobs.is_busy(i.key) for i in srv.instances.values()},
                "busy_job": {i.key: jobs.running_on(i.key) for i in shown},
                # Odoo started by OmniaCLI: its pid, None when stopped
                "running": {i.key: ops.managed_pid(i) for i in shown if i.restart.type == "process"},
                "service_state": service_states,
                # running, stopped or unknown, as on the Dashboard: the web link off when stopped
                "run_state": {i.key: run_state_of(i, service_states[i.key]) for i in shown},
                "jobs": shown_jobs(request, [j for j in jobs.recent(200)
                                             if any(k.startswith(f"{server}/") for k in j.instances)][:5]),
            },
        )

    def settings_rows(inst: Instance) -> list[tuple]:
        """(label, value, kind) of the plugin's fields that apply to the instance."""
        values = inst.plugin.options_to_dict(inst.options)
        return [(f.label, values.get(f.key), f.kind) for f in inst.plugin.fields()
                if not f.restart_types or inst.restart.type in f.restart_types]

    @app.get("/api/info")
    async def info(user: Principal = Depends(current_user)):
        """What a linking instance checks: this version and the caller's role;
        and where the customers log in (the portal, "" for none yet, None
        where it cannot be published: OmniaCLI does not keep nginx)."""
        return {"app": "OmniaCLI", "version": _version(), "caller": str(user), "role": user.role,
                "portal": portal_url() or ("" if settings.storage_roots else None)}

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
                        # the plugin's fields (Odoo: database); a customer gets no host
                        # path (a compose project's folder is its service too)
                        **({k: v for k, v in inst.plugin.describe(inst).items() if k != "project"}
                           if request.state.user.scoped else inst.plugin.describe(inst)),
                        "service": None if request.state.user.scoped and str(inst.restart.service).startswith("/")
                        else inst.restart.service,
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
                # the machine of that production when it is not this one (a staging
                # made here from another machine's production, UX-008 §5.1)
                "parent_machine": known.get(srv.name, {}).get("machine", ""),
                "created": known.get(srv.name, {}).get("created", ""),
                # the Instances of every machine (UX-008 §4.1): its Odoo, and
                # whether its production backs up by itself (none for a staging)
                "version": odoo_version(srv.name), "workspace": settings.workspace_of(srv.name) or "",
                # Odoo Enterprise in its addons (UX-012): the badge of the lists
                "enterprise": bool(enterprise_of(srv)),
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
                source = sampler.source.get(inst.key, "")
                if shown is not None:
                    # a customer: not the machine's folders (FUGA 4)
                    source = customer_view(request).text(source)
                data[inst.key] = {
                    "source": i18n.tr(request_language(request), source),
                    "points": sampler.series(inst.key, since),
                }
        return {"cores": sampler.cores, "interval": 5,
                "machine": sampler.machine_series(since) if shown is None else [], "instances": data}

    @app.get("/api/metrics/history")
    async def metrics_history(request: Request, days: int = 14):
        """This machine's load over the last days (1, 7 or 14): a point every
        5 minutes (metricshistory), the tiles' numbers, and the daily free
        space of its main disk (/, else the biggest) with its forecast. Like
        the machine's load of /api/metrics, not for a user of workspaces (who
        does not reach this route: WORKSPACE_ROUTES)."""
        if visible_servers(request.state.user) is not None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Not open to the users of workspaces")
        if days not in (1, 7, 14):
            return refuse("days must be 1, 7 or 14")
        found = load_history.window(days)
        disk = None
        entries = disk_history.disks
        mount = "/" if "/" in entries else max(entries, key=lambda m: entries[m]["points"][-1][3], default=None)
        if mount is not None:
            oldest = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
            disk = {"mount": mount, "forecast": disk_history.forecast_of(mount),
                    "points": [{"day": p[0], "used": p[1], "free": p[2], "total": p[3]}
                               for p in entries[mount]["points"] if p[0] >= oldest]}
        return {"interval": metricshistory.BUCKET, "days": days, "now": int(time.time()), **found, "disk": disk}

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
        # Odoo Enterprise of an instance made from a template: its version fetched first (UX-012)
        # by an admin of the machine only: the copy is shared by every Enterprise instance of the version here,
        # other workspaces' too
        on = enterprise_of(settings.servers[server])
        on = on if on and not on["own"] else None
        series = on["series"] if on and enterprise_admin(user) and enterprise_source()[0] is not None else ""

        async def action(job):
            if series:
                await enterprise_fetch(job, series)
            elif on:
                job.log(f"--- Odoo Enterprise {on['series']} is not fetched: its copy is shared by every Enterprise "
                        "instance of the version on this machine, an admin of the machine updates it")
            await ops.pull_restart(job, instance, only=only, backup_folder=backup_folder(instance))

        return started(
            request,
            jobs.submit(
                pull_title("Git pull and restart", instance, only),
                [instance.key, *([f"{ENTERPRISE_KEY}/{series}"] if series else [])],
                f"manual: {user}",
                action,
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
    async def backup_list(request: Request, server: str, role: str, dumps: bool = False,
                          me: Principal = Depends(current_user)):
        """The backup zips of the instance, newest first; with dumps the SQL
        dumps uploaded for a restore too (the restore dialog, the server
        page: a link's copy and the MCP list take zips only)."""
        instance = get_instance(server, role)
        can_delete = me.can(backup_role(instance))
        folder = backup_folder(instance)
        found = [p for p in folder.iterdir() if p.is_file() and (
            p.suffix == ".zip" or (dumps and sqldump.is_dump_name(p.name)))] if folder.is_dir() else []
        files = sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)
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
        # only a zip or a SQL dump directly in this instance's backup folder
        if not sqldump.RESTORE_NAME.fullmatch(name) or path.parent != folder or not path.is_file():
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such backup")
        access_log.record("download", me.kind, me.name, request.client.host if request.client else "",
                          f"{instance.key}/{name}" + (f" for {me.on_behalf}" if me.on_behalf else ""))
        return FileResponse(path, media_type="application/sql" if sqldump.is_dump_name(name) else "application/zip",
                            filename=name)

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
            # the machines the data goes between ("": this one), for the map's line (UX-009)
            job.result["transfer"] = {"from": link.name, "to": ""}
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
        backups, through a .part file: whole or absent. For a plugin with
        restores_sql, a plain SQL dump too, told by its head and kept with
        the name .sql (17.11.2026.bck: 17.11.2026.bck.sql). An admin's: what
        is restored runs in the database's tools (a dump.sql goes through
        psql)."""
        instance = _restorable(get_instance(server, role))
        if user.kind != "user":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Only a user who logs in here may upload a backup")
        name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(name).name)
        dump = instance.plugin.restores_sql and not name.endswith(".zip")
        given = name
        if dump:
            name = sqldump.stored_name(name)
        if not sqldump.RESTORE_NAME.fullmatch(name):
            return refuse("The file must be a .zip or a plain SQL dump" if instance.plugin.restores_sql
                          else "The file must be a .zip")
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
            if dump:
                problem = sqldump.problem(part, given)
                if problem:
                    return refuse(problem)
            elif not zipfile.is_zipfile(part):
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
        into it, with the choices its plugin asks; or a SQL dump uploaded,
        for a plugin with restores_sql."""
        instance = _restorable(get_instance(server, role))
        if user.kind != "user":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Only a user who logs in here may restore a backup")
        raw, _ = await posted_params(request)
        name = str(raw.get("backup") or "").strip()
        if not sqldump.RESTORE_NAME.fullmatch(name):
            return refuse(f"{name!r} is not the name of a backup zip or SQL dump")
        # an admin of the workspace: a backup zip of the instance's, its
        # database typed whatever the role, and a backup of it made first
        backup_first = user.scoped
        if backup_first and sqldump.is_dump_name(name):
            return refuse("A SQL dump is restored by the administrators of the machine: choose a backup zip")
        if sqldump.is_dump_name(name) and not instance.plugin.restores_sql:
            return refuse(f"{instance.plugin.label} does not restore a SQL dump: a backup zip is asked")
        path = backup_folder(instance) / name
        if not path.is_file():
            return refuse(f"No backup {name} of {instance.key}", status.HTTP_404_NOT_FOUND)
        # a full archive: its Odoo backup is what is restored
        archive = instancearchive.read_manifest(path) is not None
        problem = None if archive else instance.plugin.backup_problem(path)
        if problem:
            return refuse(problem)
        if instance.role == "production" or backup_first:
            # the database it replaces, typed
            expected = str(instance.plugin.describe(instance).get("database") or instance.key)
            if str(raw.get("confirm") or "").strip() != expected:
                return refuse(f"Type {expected} to replace the production database")
        try:
            options = {f.key: _field_value(f, raw.get(f.key)) for f in instance.plugin.restore_fields()}
        except ActionRefused as exc:
            return refuse(str(exc))
        async def restore(job):
            if backup_first:
                # what is replaced can be restored again: a backup of now, kept
                # with the others (the choices of the last backup dialog)
                job.log("--- backup before the restore")
                made = await ops.backup_into(job, instance, backup_folder(instance))
                job.log(f"Backup {made.name} ({made.stat().st_size} bytes): it can be restored to undo this")
                job.result = {"backup_before": made.name}
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

    @app.get("/api/servers/{server}/{role}/log/records")
    async def log_records(server: str, role: str, pos: Optional[int] = None, inode: Optional[int] = None,
                          since: str = "", until: str = "", keep: str = "last"):
        """The log in Odoo's records ({records, counts, lines, first, last...}).
        With since (UTC, until missing: now) the records of that span, the
        first or the last logtail.MAX_LINES lines of it (keep); without, the
        last 300 lines, then those after pos, as /log follows them."""
        instance = get_instance(server, role)
        span = None
        if since or until:
            if keep not in ("first", "last"):
                return JSONResponse({"error": "keep is first or last"}, status_code=400)
            try:
                span = logtail.parse_span(since, until)
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
        try:
            if instance.log_file is None:
                got = await (ops.compose_span(instance, *span, keep, logtail.MAX_LINES) if span
                             else ops.compose_log(instance, pos))
            elif span:
                got = await asyncio.to_thread(logtail.read_span, instance.log_file, *span, keep)
                if not got.pop("timed"):
                    return JSONResponse({"error": f"The lines of {instance.log_file} carry no time of Odoo: "
                                                  "only the live log reads it"}, status_code=422)
            else:
                got = await asyncio.to_thread(read_since, instance.log_file, pos, inode)
        except ops.LogUnavailable as exc:
            return JSONResponse({"error": f"Cannot read {instance.log_label}: {exc}"}, status_code=404)
        except OSError as exc:
            return JSONResponse({"error": f"Cannot read {instance.log_file}: {exc.strerror}"}, status_code=404)
        found = logtail.records(got.pop("lines"))
        if instance.log_file is not None:
            # a file: the times are those Odoo wrote, in the machine's time zone
            heads = [logtail.head_time(r["lines"][0]) for r in found if r["level"] and not r.get("stray")]
            got.update(first=logtail.local_to_iso(heads[0] if heads else None),
                       last=logtail.local_to_iso(heads[-1] if heads else None))
        got.update(records=found, counts=logtail.counts(found), lines=sum(len(r["lines"]) for r in found))
        if span:
            got.update(since=logtail.iso_utc(span[0]), until=logtail.iso_utc(span[1]), keep=keep)
        return got

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
        # a customer searches what it reads: the names taken out are not found
        found = filter_jobs(shown_jobs(request, found), server, q)
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
        return _TEMPLATES.TemplateResponse(request, "job.html", {"job": shown_jobs(request, [job], True)[0], "live": live})

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
        found = shown_jobs(request, visible_jobs(me, jobs.recent(len(jobs.jobs)))[:min(max(limit, 1), 200)])
        running = visible_jobs(me, [j for j in jobs.jobs.values() if j.status == "running"])
        return {
            "jobs": [{"id": j.id, "title": j.title, "status": j.status, "instances": j.instances,
                      "servers": j.servers, "source": j.source, "created": j.created,
                      "finished": j.finished, "duration": j.duration, "error": j.error,
                      "outcome": j.outcome} for j in found],
            "busy": sorted({key for j in running for key in j.instances}),
        }

    @app.get("/api/jobs/{job_id}")
    async def job_json(request: Request, job_id: str, since: int = 0, said: int = 0):
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such job")
        duration, job = job.duration, shown_jobs(request, [job], True)[0]
        data = asdict(job)
        data["duration"] = duration
        data["lines"] = job.lines[since:]
        data["next"] = len(job.lines)
        data["outcome"] = job.outcome
        if said:
            # for the job page (said=1): its lines, steps, error and warnings
            # in the reader's language; the API's own stays the code's English
            say = functools.partial(i18n.tr, request_language(request))

            def say_line(line: str) -> str:
                # a warning (Job.warn): its sentence translated too, after the word
                stamp, warned, text = line.partition(" WARNING: ")
                if warned and re.fullmatch(r"\d\d:\d\d:\d\d", stamp):
                    return f"{stamp} {say('WARNING: {text}').replace('{text}', say(text))}"
                return say(line)
            data["lines"] = [say_line(line) for line in data["lines"]]
            data["error"] = say(data["error"]) if data["error"] else data["error"]
            data["warnings"] = [say(w) for w in job.warnings]
            progress = data["result"].get("progress") if isinstance(data["result"], dict) else None
            if isinstance(progress, dict) and isinstance(progress.get("steps"), list):
                progress["steps"] = [{**s, "label": say(s.get("label")), "where": say(s.get("where"))}
                                     if isinstance(s, dict) else s for s in progress["steps"]]
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
        if parts.hostname in ("localhost", "127.0.0.1") and user.on_behalf and request.headers.get(TUNNEL_HEADER) == "1":
            # asked through a link by an OmniaCLI whose browser reaches its tunnels: no name of the
            # instance opens from outside, so that OmniaCLI takes the session here, through the link,
            # and gives it to the browser on a tunnel to the instance (login_as_through_link there)
            return f"http://{request.headers.get('host', '127.0.0.1')}", f"http://{parts.netloc}"
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
    async def config_save(request: Request, payload: dict = Body(...)):
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
                         "checks": checks_said(request, report)},
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
            "checks": checks_said(request, report) if report is not None else None,
        }

    @app.post("/api/servers/check", dependencies=[Depends(admin)])
    async def config_check(request: Request, payload: dict = Body(...)):
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
        return {"ok": checks.passed(report), "checks": checks_said(request, report)}

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
        apply_sites(settings.servers, settings.workspaces, sites_known())
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
    async def config_test(request: Request, name: str):
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
        return {"ok": ok, "status": srv.status, "checks": checks_said(request, report)}

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
        apply_sites(settings.servers, settings.workspaces, sites_known())
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
        after = dataclasses.replace(
            ws, main=main if main in instances else "",
            subdomains={s: t for s, t in subdomains.items() if t in instances and s not in instances})
        return addresses_follow(before, after, gone, now)

    # the HTTPS and DNS state of each name as its last publication found it: the map reads it (UX-009)
    web_names_state = inframap.WebNames(settings.servers_file.parent / inframap.WEB_FILE, workspacehelper.NGINX_SITES)

    def sites_known() -> dict:
        """{workspace: {name: {dns, https}}} as the last publications found
        the names (apply_sites); of a workspace published before that was
        kept, what its nginx site says: the HTTPS of each name, not its DNS."""
        return {ws.name: web_names_state.of(ws.name) for ws in settings.workspaces.values() if ws.domain}

    # each instance at a name that opens, as this process knows the names (the file alone at the load)
    apply_sites(settings.servers, settings.workspaces, sites_known())

    async def names_that_open() -> None:
        """At the start: a workspace with an environment none of whose names
        is in DNS for this machine, and no automatic name yet (a staging made
        by an OmniaCLI that gave none), has its names published once more, so
        that the environment gets the name it is opened at. Nothing where
        every environment has a name that leads here, behind NAT, or with the option off."""
        await asyncio.sleep(20)
        domain = settings.options.automatic_names
        if not domain:
            return
        try:
            here = await asyncio.to_thread(workspaceweb.machine_addresses, shutil.which("ip"))
            address = public_ipv4(here)
            if not address:
                return
            for ws in list(settings.workspaces.values()):
                if not ws.domain or not ws.storage:
                    continue
                for instance, role in environments_of(ws):
                    names = environment_names(ws, instance, role)
                    name = automatic_name(ws.name, instance, role, address, domain)
                    if not names or name is None or name in names:
                        continue
                    if "here" not in (await names_state(names, here)).values():
                        submit_publish(ws, "system: a name that opens")
                        break
        except Exception:  # noqa: BLE001 - the start goes on: the names are published at the next change
            _logger.exception("the names that open were not looked at")

    def environments_of(ws: Workspace) -> list[tuple[str, str]]:
        """(instance, role) of the environments of ws that exist, productions first."""
        found = [(i, role) for i in ws.instances() if f"{ws.name}-{i}" in settings.servers
                 for role in settings.servers[f"{ws.name}-{i}"].instances]
        return sorted(found, key=lambda e: e[1] != "production")

    async def names_state(names: list[str], here: set[str]) -> dict[str, str]:
        """{name: none|here|elsewhere|unknown}: where the DNS leads each name, asked now."""
        found = await asyncio.gather(*(asyncio.to_thread(workspaceweb.addresses_of, n) for n in names))
        return {n: workspaceweb.dns_state(a, here) for n, a in zip(names, found)}

    def public_ipv4(here: set[str]) -> str:
        """The public IPv4 of this machine among here, "" when it has none (behind NAT)."""
        return next((a for a in sorted(here) if ":" not in a), "")

    async def automatic_names(job, ws: Workspace) -> tuple[Workspace, set]:
        """An environment none of whose names leads here in DNS (a staging's
        stg-<date>.<domain> without a wildcard record, an instance made
        before its record) gets a name that resolves by itself, built on the
        machine's public address (config.automatic_name), among its other
        names: the browser is sent there (opening_name) and the certificate
        covers it. Nothing without the option, a domain or a public IPv4.
        (the workspace as it is now, the environments that got a name)."""
        domain = settings.options.automatic_names
        if not domain or not ws.domain:
            return ws, set()
        here = await asyncio.to_thread(workspaceweb.machine_addresses, shutil.which("ip"))
        address = public_ipv4(here)
        if not address:
            return ws, set()
        added: dict[tuple[str, str], str] = {}
        for instance, role in environments_of(ws):
            names = environment_names(ws, instance, role)
            name = automatic_name(ws.name, instance, role, address, domain)
            if not names or name is None or name in names:
                continue
            if "here" in (await names_state(names, here)).values():
                continue
            added[(instance, role)] = name
        if not added:
            return ws, set()
        async with workspaces_lock:
            current = settings.workspaces.get(ws.name)
            if current is None:
                return ws, set()
            data = current.to_dict()
            aliases = {i: {r: list(n) for r, n in roles.items()} for i, roles in (data.get("aliases") or {}).items()}
            for (instance, role), name in added.items():
                if name not in aliases.setdefault(instance, {}).setdefault(role, []):
                    aliases[instance][role].append(name)
            data["aliases"] = aliases
            try:
                found = parse_workspaces({**{n: w.to_dict() for n, w in settings.workspaces.items()}, ws.name: data},
                                         settings.storage_roots)
            except ConfigError as exc:
                job.log(f"No automatic name: {exc}")
                return current, set()
            if await store_workspaces(found) is not None:
                job.log(f"No automatic name: {settings.workspaces_file} cannot be written")
                return current, set()
        for (instance, role), name in added.items():
            job.log(f"{environment_label(instance, role)}: none of its names is in DNS for this machine, "
                    f"it also answers on {name}")
        return settings.workspaces[ws.name], set(added)

    # what name_problem read from the DNS, kept a minute: the page of an instance asks it at every load
    name_problems_seen: dict[tuple, tuple[float, dict | None]] = {}
    NAME_PROBLEM_SECONDS = 60

    async def name_problem(ws: Workspace, instance: str, role: str, names: list[str]) -> dict | None:
        """None when the first of names opens: the DNS leads it to this
        machine (or where it leads cannot be told: behind NAT) and it has its
        certificate; else {name, dns: none | elsewhere | here (in DNS, no
        certificate yet), address: this machine's public IPv4, instead: the
        name the environment is opened at ("" when none: only a tunnel reaches it)}.
        Asked to the DNS now: for the page of an instance and before a staging is made."""
        if not names:
            return None
        key = (ws.name, instance, role, tuple(names))
        seen = name_problems_seen.get(key)
        if seen is not None and time.monotonic() - seen[0] < NAME_PROBLEM_SECONDS:
            return seen[1]
        here = await asyncio.to_thread(workspaceweb.machine_addresses, shutil.which("ip"))
        state = await names_state(names, here)
        found = None
        if state[names[0]] in ("none", "elsewhere"):
            address = public_ipv4(here)
            instead = next((n for n in names[1:] if state[n] == "here"), "")
            if not instead and settings.options.automatic_names and address:
                # the one the next publication gives it
                instead = automatic_name(ws.name, instance, role, address, settings.options.automatic_names) or ""
            found = {"name": names[0], "dns": state[names[0]], "address": address, "instead": instead}
        else:
            # in DNS, and the last publication left it without HTTPS: no certificate for it yet
            read = sites_known().get(ws.name) or {}
            if names[0] in read and not (read[names[0]] or {}).get("https"):
                found = {"name": names[0], "dns": "here", "address": "",
                         "instead": next((n for n in names[1:] if (read.get(n) or {}).get("https")), "")}
        name_problems_seen[key] = (time.monotonic(), found)
        return found

    def not_recognized(ws: Workspace, made: dict, address: str = "") -> list[tuple[tuple[str, str], bool, str, str]]:
        """What a publication says of the environments whose own name the
        DNS does not lead here: [((instance, role), whether another of its
        names opens, what is wrong and where it is opened instead, the record
        to add)]."""
        missing, away = set(made.get("without_dns") or []), set(made.get("elsewhere") or [])
        said = []
        for instance, role in environments_of(ws):
            names = environment_names(ws, instance, role)
            if not names or names[0] not in missing | away:
                continue
            name, env = names[0], environment_label(instance, role)
            works = [n for n in names if n not in missing | away]
            why = (f"{name} is not recognized: it has no DNS record." if name in missing
                   else f"{name} is not recognized: its DNS record leads to another machine.")
            where = (f"{env} is opened at https://{works[0]} instead" if works
                     else f"{env} has no name that opens from outside: only a tunnel reaches it")
            remedy = (f"To use {name} add an A record {name} → {address}, then publish the names again" if address
                      else f"To use {name} add a DNS record that leads it to this machine, then publish the names again")
            said.append(((instance, role), bool(works), f"{why} {where}", remedy))
        return said

    async def publish_names(job, ws: Workspace) -> dict:
        """workspaceops.publish, its answer kept for the map: which names
        came out with HTTPS and with their DNS here. Before it an environment
        with no name in DNS gets its automatic one; after it each instance is
        opened at a name that leads here. A name that is not recognized is a
        warning of the job the first time (its automatic name was just given)
        and whenever nothing opens the environment, a line of its log after."""
        ws, added = await automatic_names(job, ws)
        made = await workspaceops.publish(job, ws)
        await asyncio.to_thread(web_names_state.record, ws.name, made)
        apply_sites(settings.servers, settings.workspaces, sites_known())
        save_login_hosts()
        here = await asyncio.to_thread(workspaceweb.machine_addresses, shutil.which("ip"))
        for env, opens, line, remedy in not_recognized(ws, made, public_ipv4(here)):
            (job.warn if env in added or not opens else job.log)(line)
            job.log(remedy)
        name_problems_seen.clear()
        return made

    def submit_publish(ws: Workspace, source: str):
        """A job: the workspace's nginx site and certificate, as it is when the job runs."""
        async def action(job):
            current = settings.workspaces.get(ws.name)
            if current is None:
                raise JobFailed(f"no workspace {ws.name}")
            job.result.update(await publish_names(job, current))
        return jobs.submit(f"Names of workspace {ws.name}", [f"{s}/{r}" for s in ws.servers for r in ROLES],
                           source, action)

    def site_names(ws: Workspace) -> list[dict]:
        """The names of each environment, with its other names and redirects
        (set in its Addresses dialog, read only here: UX-008 D5)."""
        # the roles each instance has: one without a test has no test names
        roles = {i: tuple(settings.servers[workspaceops.server_name(ws, i)].instances)
                 for i in ws.instances() if workspaceops.server_name(ws, i) in settings.servers}
        try:
            sites = web_names(ws.domain, list(roles), ws.main, ws.subdomains, roles) if ws.domain else []
        except ConfigError:
            return []
        return [{**s, "aliases": ws.aliases.get(s["instance"], {}).get(s["role"], []),
                 "redirects": [f"{n} → {to}" for n, to in ws.redirects.get(s["instance"], {}).get(s["role"], {}).items()]}
                for s in sites]

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

    # --- Odoo Enterprise (docs/ux/UX-012) -----------------------------------
    # Where its code comes from, once per machine (the machine's admins); one
    # copy per version branch kept by root; a switch per instance made from a
    # template. The token and the key never leave this machine's files.

    enterprise_file = enterprise.file_of(settings.servers_file)
    enterprise_folder = enterprise.folder_of(settings.servers_file)
    enterprise_lock = asyncio.Lock()
    # the jobs that fetch a version wait for each other: enterprise/<version>
    ENTERPRISE_KEY = "enterprise"
    ENTERPRISE_HELPER_TIMEOUT = 1800

    def enterprise_source() -> tuple[enterprise.Source | None, str]:
        """(the source saved or None, why it cannot be read)."""
        try:
            return enterprise.load(enterprise_file), ""
        except enterprise.EnterpriseError as exc:
            return None, str(exc)

    def enterprise_users() -> dict[str, list[str]]:
        """{version: the servers whose instance has Odoo Enterprise of it on}."""
        found: dict[str, list[str]] = {}
        for srv in settings.servers.values():
            inst = srv.production or srv.test
            if inst is not None and inst.restart.type == "compose" and workspace_of(srv.name) is not None:
                on = enterprise.of_meta(instance_meta(inst))
                if on:
                    found.setdefault(on["series"], []).append(srv.name)
        return found

    def enterprise_of(srv) -> dict | None:
        """{series, own, folder} when the server's Odoo has Enterprise in its
        addons: OmniaCLI's switch (an instance made from a template), or an
        addons_path of its own that holds web_enterprise (an Odoo that was
        there already, read as it is); None when it has none."""
        inst = srv.production or srv.test
        if inst is None:
            return None
        if inst.restart.type == "compose":
            on = enterprise.of_meta(instance_meta(inst)) if workspace_of(srv.name) is not None else None
            return {"series": on["series"], "own": False, "folder": ""} if on else None
        if inst.restart.type in ("process", "systemd") and getattr(inst.options, "odoo_config", None):
            folder = enterprise.in_addons(enterprise.addons_of_conf(inst.options.odoo_config))
            if folder is not None:
                return {"series": "", "own": True, "folder": str(folder)}
        return None

    def enterprise_admin(me: Principal) -> bool:
        """Who switches Odoo Enterprise and says where its code comes from: an
        admin of the machine, as a person (here or through another panel), never
        a workspace's admin (the code is reached with the machine's access, the
        subscription is the customer's) nor a machine acting by itself."""
        return machine_admin(me) and not a_machine(me)

    async def enterprise_fetch(job, series: str) -> dict:
        """The version branch fetched (shallow) into OmniaCLI's own working
        copy, then copied by root into its folder the instances mount."""
        source, why = enterprise_source()
        if source is None:
            raise JobFailed(why or "No source of Odoo Enterprise: set it on Settings › Odoo Enterprise")
        options = enterprise.git_options(source, enterprise_folder)
        target = enterprise.checkouts_of(settings.jobs_dir) / series
        job.log(f"--- Odoo Enterprise {series}: the branch {series} of {source.url}, its last commit only")
        if (target / ".git").is_dir():
            await ops.run(job, ["git", *options, "-C", str(target), "remote", "set-url", "origin", source.url])
            await ops.run(job, ["git", *options, "-C", str(target), "fetch", "--depth", "1", "--no-tags", "origin",
                                f"+refs/heads/{series}:refs/remotes/origin/{series}"], timeout=enterprise.FETCH_TIMEOUT)
            await ops.run(job, ["git", "-C", str(target), "reset", "--hard", "--quiet", f"origin/{series}"])
            await ops.run(job, ["git", "-C", str(target), "clean", "-ffdxq"])
        else:
            if target.exists() or target.is_symlink():
                workspaceops.remove_clone(target)
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                await ops.run(job, ["git", *options, "clone", "--depth", "1", "--single-branch", "--no-tags",
                                    "--branch", series, "--", source.url, str(target)], timeout=enterprise.FETCH_TIMEOUT)
            except BaseException:
                if target.exists():
                    workspaceops.remove_clone(target)
                raise
        job.log(f"--- Odoo Enterprise {series}: root's copy, read only, that the instances mount")
        found = await workspaceops._helper(job, "enterprise-sync", series, timeout=ENTERPRISE_HELPER_TIMEOUT)
        job.log(f"Odoo Enterprise {series}: {found.get('modules')} modules at commit {str(found.get('commit'))[:10]} "
                f"({found.get('added')} files added, {found.get('changed')} changed, {found.get('removed')} removed)")
        return found

    @app.get("/enterprise", dependencies=[Depends(admin)])
    async def enterprise_page(request: Request, me: Principal = Depends(current_user)):
        """Settings › Odoo Enterprise: where the code comes from, the copies
        of each version and the instances that use them."""
        if not enterprise_admin(me):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "An admin of the machine is required")
        source, error = enterprise_source()
        users = await asyncio.to_thread(enterprise_users)
        copies = [{**found, "users": users.get(series, [])}
                  for series, found in (await asyncio.to_thread(enterprise.states)).items()]
        return _TEMPLATES.TemplateResponse(request, "enterprise.html", {
            "source": source.public() if source else None, "error": error, "default_url": enterprise.DEFAULT_URL,
            "public_key": enterprise.public_key(enterprise_folder), "file": enterprise_file,
            "copies": copies, "root": enterprise.ROOT, "helpers": bool(settings.storage_roots)})

    def enterprise_given(payload: dict) -> enterprise.Source:
        """The source of the form: a token left empty keeps the one saved for the same address kind."""
        saved, _ = enterprise_source()
        url = str(payload.get("url") or "").strip()
        token = str(payload.get("token") or "")
        if not token and saved is not None and saved.token and enterprise.kind_of(url) == "https":
            token = saved.token
        return enterprise.check(url, str(payload.get("username") or "").strip(), token)

    @app.post("/api/enterprise/save", dependencies=[Depends(admin)])
    async def enterprise_save(payload: dict = Body(...), me: Principal = Depends(current_user)):
        if not enterprise_admin(me):
            return refuse("An admin of the machine is required", status.HTTP_403_FORBIDDEN)
        async with enterprise_lock:
            try:
                source = enterprise_given(payload)
                await asyncio.to_thread(enterprise.save, enterprise_file, source)
            except enterprise.EnterpriseError as exc:
                return refuse(str(exc))
            except OSError as exc:
                return refuse(f"Cannot write {enterprise_file}: {exc}", 500)
        return {"ok": True, "source": source.public()}

    @app.post("/api/enterprise/key", dependencies=[Depends(admin)])
    async def enterprise_key(me: Principal = Depends(current_user)):
        """The SSH key of Odoo Enterprise, made the first time: its public half."""
        if not enterprise_admin(me):
            return refuse("An admin of the machine is required", status.HTTP_403_FORBIDDEN)
        try:
            public = await sshlinks.ensure_key(enterprise_folder, enterprise.KEY_NAME)
        except (sshlinks.SshError, OSError) as exc:
            return refuse(str(exc), 500)
        return {"ok": True, "public_key": public}

    @app.post("/api/enterprise/test", dependencies=[Depends(admin)])
    async def enterprise_test(payload: dict = Body(...), me: Principal = Depends(current_user)):
        """git ls-remote of the address of the form: the version branches it has."""
        if not enterprise_admin(me):
            return refuse("An admin of the machine is required", status.HTTP_403_FORBIDDEN)
        try:
            source = enterprise_given(payload)
        except enterprise.EnterpriseError as exc:
            return refuse(str(exc))
        if source.kind == "ssh" and enterprise.public_key(enterprise_folder) is None:
            return refuse("No SSH key yet: make it with «Make the key», add its public half to the GitHub account, "
                          "then test")
        options = await asyncio.to_thread(enterprise.git_options, source, enterprise_folder)
        code, out = await ops.capture(["git", *options, "ls-remote", "--heads", "--", source.url],
                                      timeout=enterprise.GIT_TIMEOUT)
        if code:
            return refuse(f"{source.url} cannot be read: {enterprise.last_line(out) or code}")
        heads = enterprise.parse_heads(out)
        return {"ok": True, "versions": enterprise.series_of(heads), "branches": len(heads)}

    @app.post("/api/enterprise/update", dependencies=[Depends(admin)])
    async def enterprise_update(payload: dict = Body(...), me: Principal = Depends(current_user)):
        """A version fetched again (or for the first time): a job. The
        Enterprise instances of that version run the new code from their next
        restart; their modules are updated by their own Update."""
        if not enterprise_admin(me):
            return refuse("An admin of the machine is required", status.HTTP_403_FORBIDDEN)
        series = str(payload.get("series") or "")
        if not enterprise.SERIES.fullmatch(series):
            return refuse(f"{series!r} is not a version of Odoo (19.0)")
        source, why = enterprise_source()
        if source is None:
            return refuse(why or "No source of Odoo Enterprise: set it on Settings › Odoo Enterprise")

        async def action(job):
            found = await enterprise_fetch(job, series)
            users = enterprise_users().get(series, [])
            if users:
                job.log(f"The Enterprise instances of {series} ({', '.join(users)}) run the new code from their next "
                        "restart; Update on each one updates its changed modules")
            job.result = {"series": series, "commit": found.get("commit"), "modules": found.get("modules")}

        job = jobs.submit(f"Fetch Odoo Enterprise {series}", [f"{ENTERPRISE_KEY}/{series}"], f"manual: {me}", action)
        return {"job": job.id}

    def enterprise_line(request: Request, srv, me: Principal) -> dict | None:
        """The Odoo Enterprise row of an instance's Settings tab: on or off,
        its version, its copy here, whether me may switch it and why not."""
        inst = srv.production or srv.test
        if inst is None or inst.plugin is None or srv.type not in ("odoo", "docker"):
            return None
        made = workspace_of(srv.name)
        if made is None:
            found = enterprise_of(srv)
            return {"own": True, "on": bool(found), "folder": found["folder"] if found and machine_admin(me) else "",
                    "can": False}
        meta = instance_meta(inst)
        series = enterprise.series_of_template(str(meta.get("template") or ""))
        on = enterprise.of_meta(meta)
        row = {"own": False, "on": bool(on), "series": (on or {}).get("series") or series or "",
               "here": enterprise.state(series) if series else None, "can": False, "reason": "",
               "url": f"{prefix_of(request)}/api/servers/{srv.name}/enterprise",
               "install": f"{prefix_of(request)}/api/servers/{srv.name}/{inst.role}/enterprise/install"}
        source, _ = enterprise_source()
        if series is None:
            row["reason"] = f"Its template {meta.get('template') or '?'} names no version of Odoo"
        elif not enterprise_admin(me):
            row["reason"] = "An admin of the machine switches it: the code is reached with the machine's access"
        elif not on and row["here"] is None and source is None:
            row["reason"] = "Say first where its code comes from: Settings › Odoo Enterprise"
        else:
            row["can"] = True
        return row

    async def enterprise_installed(inst, series: str) -> list[str]:
        """The modules of Odoo Enterprise of series installed in the instance's
        database (JobFailed when it does not answer)."""
        names = set(enterprise.modules_in(enterprise.ROOT / series))
        known = await inst.plugin.known_modules(inst)
        return sorted(m for m, state in known.items() if m in names and state in enterprise.INSTALLED_STATES)

    async def enterprise_refusal(srv, series: str) -> str | None:
        """Why Odoo Enterprise may not be switched off: a module of it is
        installed in a database of the server, or a database does not say."""
        for inst in (srv.production, srv.test):
            if inst is None:
                continue
            try:
                found = await enterprise_installed(inst, series)
            except JobFailed as exc:
                return (f"The database {inst.options.database} of {inst.key} does not say which modules it has "
                        f"installed ({exc}): start it, then switch Odoo Enterprise off")
            if found:
                return (f"Modules of Odoo Enterprise are installed in the database "
                        f"{inst.options.database} of {inst.key} ({', '.join(found[:8])}{'…' if len(found) > 8 else ''}): "
                        "Odoo would not start without their code. Uninstall them in Odoo (Apps), then switch it off")
        return None

    @app.post("/api/servers/{server}/enterprise", dependencies=[Depends(admin)])
    async def enterprise_switch(request: Request, server: str, payload: dict = Body(...),
                                me: Principal = Depends(current_user)):
        """Odoo Enterprise on or off for an instance made from a template: a
        job, which fetches its version first when this machine has no copy of
        it, writes the instance's files (root's helper) and restarts its Odoo.
        No module is installed: web_enterprise is installed by its own button."""
        if not enterprise_admin(me):
            return refuse("An admin of the machine switches Odoo Enterprise", status.HTTP_403_FORBIDDEN)
        srv = settings.servers.get(server)
        if srv is None:
            return refuse(f"No server {server}", status.HTTP_404_NOT_FOUND)
        made = workspace_of(server)
        if made is None:
            return refuse(f"{server} was not made from a template: its code is not OmniaCLI's, and neither is its "
                          "addons_path. Odoo Enterprise is added there by hand")
        ws, instance = made
        inst = srv.production or srv.test
        meta = instance_meta(inst)
        series = enterprise.series_of_template(str(meta.get("template") or ""))
        if series is None:
            return refuse(f"Its template {meta.get('template') or '?'} names no version of Odoo")
        on = payload.get("on") is True
        now = enterprise.of_meta(meta)
        if on == bool(now):
            return refuse(f"Odoo Enterprise is on for {server} already" if on else f"Odoo Enterprise is off for {server} already")
        if on and enterprise.state(series) is None and enterprise_source()[0] is None:
            return refuse("Say first where its code comes from: Settings › Odoo Enterprise")
        if not on:
            why = await enterprise_refusal(srv, series)
            if why:
                return refuse(why, status.HTTP_409_CONFLICT)
        roles = [r for r in ROLES if getattr(srv, r) is not None]

        async def action(job):
            if on and enterprise.state(series) is None:
                job.log(f"--- this machine has no copy of Odoo Enterprise {series} yet: it is fetched first")
                await enterprise_fetch(job, series)
            if not on:
                why = await enterprise_refusal(srv, series)
                if why:
                    raise JobFailed(why)
            job.log(f"--- Odoo Enterprise {series} first in the addons path of {server}" if on
                    else f"--- Odoo Enterprise {series} out of the addons path of {server}")
            await workspaceops._helper(job, "enterprise", ws.name, ws.storage, instance, "on" if on else "off")
            for role in roles:
                await ops.restart(job, getattr(srv, role))
            if on:
                job.log(f"Odoo Enterprise {series} is in the addons of {server}. Its database is still Community "
                        "until web_enterprise is installed (the instance's Settings tab, Install web_enterprise, or "
                        "Odoo's Apps); the subscription code is the customer's, asked by Odoo after")
            else:
                job.log(f"{server} runs Odoo Community again")
            job.result = {"server": server, "enterprise": on, "series": series}

        job = jobs.submit(f"Switch Odoo Enterprise on for {server}" if on else f"Switch Odoo Enterprise off for {server}",
                          [*(f"{server}/{r}" for r in roles), f"{ENTERPRISE_KEY}/{series}"], f"manual: {me}", action,
                          workspace=ws.name)
        return {"job": job.id}

    @app.post("/api/servers/{server}/{role}/enterprise/install", dependencies=[Depends(admin)])
    async def enterprise_install(server: str, role: str, me: Principal = Depends(current_user)):
        """web_enterprise installed in the role's database: what makes a
        database Enterprise (Odoo then asks for the customer's subscription).
        A job, asked on purpose: switching Odoo Enterprise on installs nothing."""
        if not enterprise_admin(me):
            return refuse("An admin of the machine installs web_enterprise", status.HTTP_403_FORBIDDEN)
        inst = get_instance(server, role)
        on = enterprise.of_meta(instance_meta(inst)) if workspace_of(server) is not None else None
        if not on:
            return refuse(f"Odoo Enterprise is off on {server}: switch it on first")
        if enterprise.SIGN not in enterprise.modules_in(enterprise.ROOT / on["series"]):
            return refuse(f"The copy of Odoo Enterprise {on['series']} on this machine has no {enterprise.SIGN}")

        async def action(job):
            await inst.plugin.install_modules(job, inst, [enterprise.SIGN])
            job.log(f"{enterprise.SIGN} is installed in {inst.options.database}: Odoo asks for the subscription "
                    "code of the customer at the next login of an administrator")

        job = jobs.submit(f"Install {enterprise.SIGN} on {inst.key}", [inst.key], f"manual: {me}", action,
                          workspace=workspace_of(server)[0].name)
        return {"job": job.id}



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
                data.update(main=before.main, subdomains=before.subdomains, certificate=before.certificate,
                            aliases=before.to_dict().get("aliases"), redirects=before.to_dict().get("redirects"))
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
                job.result.update(await publish_names(job, current))
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
        members = [{"name": n, "role": r, "only_here": only_here(n, ws.name),
                    "invitation": invitation_state(n) if only_here(n, ws.name) else None,
                    # its two-step verification: on, and whether its admins here may reset it
                    "two_step": second_factors.on(n),
                    "can_reset": second_factors.on(n) and n != me.name and (machine_admin(me) or only_here(n, ws.name))}
                   for n, r in sorted(ws.members.items())]
        found = await asyncio.to_thread(workspaceops.templates)
        return _TEMPLATES.TemplateResponse(
            request, "workspace.html",
            {"ws": ws, "members": members, "roles": ACCESS_ROLES, "me": me.name, "min_password": _MIN_PASSWORD,
             "can_mail": bool(settings.options.smtp_host), "invitation_hours": INVITATION_HOURS,
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
             "mcp_url": public_base(request) + "/mcp",
             # the links an archive may be copied through: the machine's admins'
             "links": sorted(settings.link_targets) if me.kind == "user" and not me.workspaces and me.can("admin")
             else [],
             # the links whose cluster a new instance's database may be asked of (AD-003): the machine's
             # admins', here or through a link for one of its users
             "db_links": sorted(settings.link_targets) if machine_admin(me) and acts_for_a_person(me) else [],
             "credentials": await asyncio.to_thread(workspace_credentials, ws.name),
             # how many remote destinations its backups have (their page lists them)
             "destinations_count": len(destination_rows(ws.name)),
             "instances_left": None if not ws.limits.get("instances")
             else int(ws.limits["instances"]) - len([n for n in ws.servers if n in settings.servers])},
        )

    def left_by_interruption(ws: Workspace, name: str) -> bool:
        """name stays in ws with no server because OmniaCLI was stopped while
        the job that made its instance ran: the last job on it is a creation
        that reads interrupted. Such a name could be neither made nor deleted;
        it is free again, and what was made of it goes first (leftover_removed)."""
        if name not in ws.servers or name in settings.servers:
            return False
        last = next((j for j in reversed(jobs.jobs.values()) if name in j.servers), None)
        return last is not None and last.status == INTERRUPTED

    INTERRUPTED_WAIT = float(os.environ.get("OMNIACLI_INTERRUPTED_WAIT", "5"))

    async def interrupted_names_freed() -> None:
        """At the start: the names a creation or a staging reserved in a
        workspace when OmniaCLI was stopped under its job (no server, the
        last job on them interrupted) leave the workspace, and what was made
        of them goes, in a job of the system; before, such a name stayed in
        the workspace's list for ever (a staging's name: nothing offered to
        take it away)."""
        await asyncio.sleep(INTERRUPTED_WAIT)
        found = [(ws, name) for ws in list(settings.workspaces.values()) for name in ws.servers
                 if left_by_interruption(ws, name) and ws.storage and name.startswith(f"{ws.name}-")]
        if not found:
            return None

        async def action(job):
            for ws, name in found:
                job.log(f"--- {name}: its job was interrupted when OmniaCLI stopped; its name is free again")
                await leftover_removed(job, ws, name[len(ws.name) + 1:])
                await rename_in_workspaces(name, None)
        return jobs.submit("Names left by jobs OmniaCLI was stopped under", [f"{n}/production" for _ws, n in found],
                           "system: start", action, workspace=found[0][0].name)

    async def leftover_removed(job, ws: Workspace, instance: str) -> None:
        """What an interrupted creation left of an instance (its folder, its
        containers) taken away before it is made again."""
        job.log(f"--- what the interrupted creation left of {workspaceops.server_name(ws, instance)} is taken away first")
        await workspaceops.run(job, workspaceops.helper("remove", ws.name, ws.storage, instance), check=False,
                               timeout=workspaceops.HELPER_TIMEOUT)

    async def make_instance(job, ws: Workspace, instance: str, template, values: dict, setup: dict | None = None,
                            linked: dict | None = None):
        """The job of a new instance of ws (its server already in ws.servers):
        made, written in the servers file, its names published; its server
        out of ws again when it fails. linked: its database is asked first
        of the cluster of a linked OmniaCLI (linked_grant), and given back
        when the instance is not made."""
        name = workspaceops.server_name(ws, instance)
        granted = None
        try:
            if linked is not None:
                granted = await linked_grant(job, linked)
                values = {**values, workspacehelper.REMOTE_KEY: granted}
            data = await workspaceops.create_instance(job, ws, instance, template, values, setup)
            parsed = parse_servers({name: {**data, "status": "active"}}, settings.logs_dir)[name]
            async with config_lock:
                error = await store({**settings.servers, name: parsed})
            if error is not None:
                raise JobFailed(f"the instance runs, but the servers file was not written: {error.body.decode()}")
        except BaseException:
            if granted is not None:
                # the instance was not made: its database on the linked machine goes back
                await linked_release(job, linked["link"], linked["major"], granted["database"], linked["who"],
                                     linked["role"])
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
                await publish_names(job, current)
            except JobFailed as exc:
                job.warn(f"The instance runs, its names are not published: {exc}")
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
        if instance_name_problem(ws, instance):
            return refuse(instance_name_problem(ws, instance))
        name = workspaceops.server_name(ws, instance)
        template = (await asyncio.to_thread(workspaceops.templates)).get(str(payload.get("template") or ""))
        if template is None:
            return refuse(f"No template {payload.get('template')!r}")
        try:
            values = template.values({str(k): str(v) for k, v in (payload.get("values") or {}).items()})
        except stacktemplates.TemplateError as exc:
            return refuse(str(exc))
        # a database of another machine (AD-003): the machine's admins only (it opens the
        # workspace's network to an address), its connection checked before anything is made
        linked = None
        if template.on_remote:
            if not machine_admin(me):
                return refuse("Only an admin of the machine makes an instance on a database of another machine",
                              status.HTTP_403_FORBIDDEN)
            if payload.get("linked"):
                # on the cluster of a linked OmniaCLI: it makes the role and the database, the job asks it
                linked = linked_request(ws, instance, payload.get("linked"), me)
                if not isinstance(linked, dict):
                    return linked
            else:
                try:
                    values = {**values, workspacehelper.REMOTE_KEY: workspacehelper.remote_spec(payload.get("remote"))}
                except workspacehelper.HelperError as exc:
                    return refuse(str(exc))
                refused = workspacehelper.replica_refused(template.name, values[workspacehelper.REMOTE_KEY])
                if refused:
                    return refuse(refused)
        elif payload.get("remote") or payload.get("linked"):
            return refuse(f"The template {template.name} has a database of its own: choose a template of a database "
                          "of another machine")
        setup = admin_setup(payload.get("setup") or {})
        if not isinstance(setup, dict):
            return setup
        if not ws.storage or not ws.limits.get("disk_gb"):
            return refuse(f"Workspace {ws.name} has no storage or no disk limit: the machine's admins set them")
        if settings.storage_problem(ws):
            return refuse(f"Workspace {ws.name} cannot be used: {settings.storage_problem(ws)}")
        async with workspaces_lock:
            ws = a_workspace(workspace)
            leftover = left_by_interruption(ws, name)
            if name in settings.servers or (name in ws.servers and not leftover):
                return refuse(f"A server named {name} exists already")
            mine = [n for n in ws.servers if n in settings.servers]
            if ws.limits.get("instances") and len(mine) >= ws.limits["instances"]:
                return refuse(f"Workspace {ws.name} has its {int(ws.limits['instances'])} instances already")
            # the server is the workspace's from now: its job is seen there
            refused = await store_workspaces({**settings.workspaces, ws.name: dataclasses.replace(
                ws, servers=[*(n for n in ws.servers if n != name), name])})
            if refused:
                return refused

        async def action(job):
            if leftover:
                await leftover_removed(job, ws, instance)
            await make_instance(job, ws, instance, template, values, setup or None, linked)

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
        if instance_name_problem(ws, instance):
            return refuse(instance_name_problem(ws, instance))
        archive =await asyncio.to_thread(archive_path, ws, str(payload.get("archive") or ""))
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
            leftover = left_by_interruption(ws, name)
            if name in settings.servers or (name in ws.servers and not leftover):
                return refuse(f"A server named {name} exists already")
            mine = [n for n in ws.servers if n in settings.servers]
            if ws.limits.get("instances") and len(mine) >= ws.limits["instances"]:
                return refuse(f"Workspace {ws.name} has its {int(ws.limits['instances'])} instances already")
            refused = await store_workspaces({**settings.workspaces, ws.name: dataclasses.replace(
                ws, servers=[*(n for n in ws.servers if n != name), name])})
            if refused:
                return refused
        neutralize = bool(payload.get("neutralize", True))
        async def action(job):
            if leftover:
                await leftover_removed(job, ws, instance)
            await instance_from_archive(job, ws, instance, template, archive, neutralize)

        job = jobs.submit(f"Create {name} from the archive {archive.name}", [f"{name}/production", f"{name}/test"],
                          f"manual: {me}", action, workspace=ws.name)
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
        # the disk a new instance would get (quick_room) and the least it needs:
        # the New instance dialog turns off a machine without it before the form
        free_gb = room_gb = None
        storage = next(iter(settings.storage_roots), None)
        try:
            space = await workspaceops.space() if storage else None
        except workspaceops.SpaceError:
            space = None
        if space is not None:
            room = workspaceops.room(space, settings.workspaces, storage, None) or {}
            free_gb, room_gb = int(room.get("allowed", 0) / workspaceops.GB), quick_room(space)
        # the database (AD-001, AD-007): the machine's cluster by default when it
        # is there and the version has a template on it, dedicated otherwise
        clusters = sorted(machinedb.registry())
        versions = []
        for m in majors:
            postgres = quick_machine_postgres(found, m, clusters)
            versions.append({"major": m, "label": found[f"odoo-{m}"].label,
                             "machine": f"odoo-{m}-machine" in found and not postgres,
                             # the PostgreSQL the version runs on when the cluster is another one: the dialog
                             # turns Of the machine off and says why (AD-001, one major per machine)
                             **({"machine_postgres": postgres} if postgres else {})})
        return {"versions": versions,
                "storage": bool(settings.storage_roots), "address": await asyncio.to_thread(public_address),
                "free_gb": free_gb, "room_gb": room_gb, "min_gb": QUICK_DISK_GB[0], "cluster": bool(clusters),
                "cluster_postgres": clusters}

    def quick_machine_postgres(found: dict, major, clusters: list[str]) -> str:
        """The PostgreSQL Odoo <major> needs when the machine's cluster (its
        majors, clusters) is not it: its template on the cluster's, else its
        dedicated template's (postgres of template.json); "" when it fits or
        when nothing says (no cluster, no major named)."""
        twin, own = found.get(f"odoo-{major}-machine"), found.get(f"odoo-{major}")
        needed = getattr(twin if twin is not None else own, "postgres", "") or ""
        return needed if clusters and needed and needed not in clusters else ""

    @app.post("/api/instances/new", dependencies=[Depends(admin)])
    async def quick_instance(payload: dict = Body(...), me: Principal = Depends(current_user)):
        """A new Odoo given its version (and, if wanted, a name, a domain and a
        language): a workspace of its own on the first storage, with a disk as
        large as the real disk allows (at most QUICK_DISK_GB), then its
        production and test from the template odoo-<version>; with a domain
        its production answers there and its test on test.<domain>."""
        major = str(payload.get("version") or "").strip()
        template = f"odoo-{major}"
        found_templates = await asyncio.to_thread(workspaceops.templates)
        if template not in found_templates:
            return refuse(f"No Odoo {major} here (no template {template})")
        # its database: the machine's cluster (the default when there is one) or one of its own
        clusters = sorted(machinedb.registry())
        postgres = quick_machine_postgres(found_templates, major, clusters)
        database = str(payload.get("database") or ("machine" if clusters and not postgres
                                                    and f"{template}-machine" in found_templates else "dedicated"))
        if database not in ("machine", "dedicated"):
            return refuse("database is machine or dedicated")
        if database == "machine":
            if not clusters:
                return refuse("This machine has no cluster of its own yet: make it in Dashboard › Database, "
                              "or choose a dedicated database")
            if postgres:
                return refuse(f"Odoo {major} runs on PostgreSQL {postgres}, the machine's cluster is PostgreSQL "
                              f"{', '.join(clusters)}: choose a dedicated database")
            if f"{template}-machine" not in found_templates:
                return refuse(f"Odoo {major} has no template on the machine's cluster: choose a dedicated database")
            template = f"{template}-machine"
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
        refused = await quick_workspace(name, str(payload.get("label") or name), domain)
        if refused:
            return refused
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
        return {**made, "workspace": name, "server": workspaceops.server_name(settings.workspaces[name], QUICK_INSTANCE)}

    def quick_room(found: dict | None) -> int:
        """The disk a workspace made here by itself gets, in GB: what the real
        disk of the first storage allows, at most QUICK_DISK_GB."""
        storage = next(iter(settings.storage_roots), None)
        room = workspaceops.room(found, settings.workspaces, storage, None) if found and storage else None
        return min(int((room or {}).get("allowed", 0) * 0.9 / workspaceops.GB), QUICK_DISK_GB[1])

    async def quick_workspace(name: str, label: str, domain: str = ""):
        """A workspace of its own made for New instance or for a staging of a
        production of another machine, on the first storage with the disk
        quick_room gives; until its instance is made, it goes when that
        fails (make_instance). The refusal, or None."""
        try:
            found = await workspaceops.space()
        except workspaceops.SpaceError as exc:
            return refuse(f"The room of the disks cannot be read: {exc}", status.HTTP_502_BAD_GATEWAY)
        allowed = quick_room(found)
        if allowed < QUICK_DISK_GB[0]:
            return refuse(f"Not enough room on the disk for a new instance: {allowed} GB, "
                          f"{QUICK_DISK_GB[0]} GB at least")
        data = {"label": label.strip()[:100] or name, "domain": domain, "storage": next(iter(settings.storage_roots)),
                "limits": {"disk_gb": allowed}, "servers": [], "members": {}}
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
        return None

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

    # a staging takes about twice what a backup of its production holds: the
    # zip, and the database and the files restored from it
    STAGING_ROOM = 2
    # the names a staging on another machine is sent with (receive)
    _STAGING_PARENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")

    def chooses_machine(me: Principal) -> bool:
        """Who chooses the machine of a new staging (UX-008 §3): an admin of
        this machine, logged in here or acting through a link for one of its
        users (the pages of a linked machine, as its integrator works); a
        workspace's admin makes it here."""
        return acts_for_a_person(me) and not me.scoped and me.can("admin")

    def gb_text(size: int | float) -> str:
        return f"{size / workspaceops.GB:.1f}"

    def staging_source(server: str):
        """(its production, its workspace, its instance there) of a production
        a staging may be made of, or the refusal."""
        srv = settings.servers.get(server)
        if srv is None or srv.production is None:
            return refuse(f"No server {server} with a production", status.HTTP_404_NOT_FOUND)
        found = workspace_of(server)
        if found is None:
            return refuse(f"{server} was not made in a workspace: a staging is made beside an instance of a workspace")
        if on_remote_db(srv.production):
            # its copy would need a database of its own on that machine, which is not this machine's to make
            return refuse(f"The database of {server} is on another machine: a staging of it needs a database there, "
                          "and it is not made from here. Back it up and restore the backup into a new instance")
        return srv.production, found[0], found[1]

    def staging_address(ws: Workspace, new_instance: str) -> str:
        """The address a new staging new_instance of ws would answer on (the
        rule of web_names), "" when ws has no domain."""
        if not ws.domain:
            return ""
        roles = {i: tuple(settings.servers[workspaceops.server_name(ws, i)].instances)
                 for i in ws.instances() if workspaceops.server_name(ws, i) in settings.servers}
        roles[new_instance] = ("production",)
        try:
            sites = web_names(ws.domain, list(roles), ws.main, ws.subdomains, roles)
        except ConfigError:
            return ""
        return next((s["names"][0] for s in sites if s["instance"] == new_instance and s["role"] == "production"), "")

    def workspace_free(found: dict | None, ws: Workspace) -> int | None:
        """The bytes free in ws's disk (its limit less what its image takes),
        None when not known."""
        if not found or not ws.storage or not ws.limits.get("disk_gb"):
            return None
        room = workspaceops.room(found, settings.workspaces, ws.storage, ws.name)
        return None if room is None else max(workspaceops.limit_bytes(ws) - room["own_used"], 0)

    async def staging_size(prod) -> int | None:
        """What a backup of prod would hold, database and files (backup_estimate), None: not known."""
        try:
            found = await prod.plugin.backup_estimate(prod)
        except JobFailed:
            return None
        return int(found.get("database") or 0) + int(found.get("files") or 0)

    async def reserve_staging(ws_name: str, instance: str, now: datetime.datetime):
        """(the new staging's instance, its server) of a staging of instance
        in workspace ws_name, its name taken there; or the refusal."""
        async with workspaces_lock:
            ws = settings.workspaces[ws_name]
            taken = {n[len(ws.name) + 1:] for n in ws.servers}
            new_instance = stagings.instance_name(instance, now, taken)
            name = workspaceops.server_name(ws, new_instance)
            mine = [n for n in ws.servers if n in settings.servers]
            if ws.limits.get("instances") and len(mine) >= ws.limits["instances"]:
                return refuse(f"Workspace {ws.name} has its {int(ws.limits['instances'])} instances already: "
                              "raise its limit (Limits and disk) or delete a staging")
            refused = await store_workspaces({**settings.workspaces,
                                              ws.name: dataclasses.replace(ws, servers=[*ws.servers, name])})
            if refused:
                return refused
        return new_instance, name

    def staging_job(ws_name: str, new_instance: str, template, fill, record: dict, title: str,
                    keys: list[str], me: Principal, now: datetime.datetime, not_made=None, source: str = "",
                    odoo_enterprise: str = ""):
        """The job of a staging whose name reserve_staging took: the instance
        made from template, its mail caught (UX-008 §5.3), Odoo Enterprise of
        that version on when its production has it (UX-012: its copy follows),
        fill(job, its production) loading it (what it returns goes in its
        record), its test stopped, recorded (stagings.py) with record.
        not_made() runs when the instance could not be made."""
        name = workspaceops.server_name(settings.workspaces[ws_name], new_instance)

        async def action(job):
            try:
                if odoo_enterprise:
                    job.log(f"--- its production is Odoo Enterprise {odoo_enterprise}: so is the staging")
                    if enterprise.state(odoo_enterprise) is None:
                        if enterprise_source()[0] is None:
                            raise JobFailed(f"The production is Odoo Enterprise {odoo_enterprise}, and this machine "
                                            "has neither a copy of it nor its source: set it on Settings › Odoo "
                                            "Enterprise")
                        await enterprise_fetch(job, odoo_enterprise)
                made = await make_instance(job, settings.workspaces[ws_name], new_instance, template,
                                           {workspacehelper.MAIL_KEY: True,
                                            **({enterprise.CREATE_KEY: odoo_enterprise} if odoo_enterprise else {})})
            except BaseException:
                if not_made is not None:
                    not_made()
                raise
            target = settings.servers[name].production
            filled = await fill(job, target)
            if settings.servers[name].test is not None:
                job.log(f"--- {name}: its test stopped, a staging needs its production only")
                await ops.stop(job, settings.servers[name].test)
            data = stagings.load(stagings_file)
            data[name] = {**record, "created": now.isoformat(timespec="seconds"), **filled}
            await asyncio.to_thread(stagings.save, stagings_file, data)
            job.result = {**job.result, "server": name, "port": made.production.options.http_port}

        return jobs.submit(title, [*keys, f"{name}/production", f"{name}/test"], source or f"manual: {me}", action,
                           workspace=ws_name)

    @app.get("/api/servers/{server}/stagings/new", dependencies=[Depends(admin)])
    async def staging_form(request: Request, server: str, me: Principal = Depends(current_user)):
        """What the New staging dialog shows (UX-008 §5.1): the size of the
        copy, the address it gets here, and for an admin of this machine the
        links that can hold it (the same template as the production, room
        for it), the others with why not."""
        found = staging_source(server)
        if not isinstance(found, tuple):
            return found
        prod, ws, instance = found
        template = await asyncio.to_thread(instance_template, ws, instance)
        if template is None:
            return refuse(f"The template {server} was made from is not on this machine any more")
        size = await staging_size(prod)
        new_instance = stagings.instance_name(instance, datetime.datetime.now(), set(ws.instances()))
        out = {"size": size, "address": staging_address(ws, new_instance), "template": template.name, "here": here_name(),
               "workspace": ws.name, "machines": [], "unlisted": []}
        # said before the click: a name the DNS does not lead here, and what the staging is opened at instead
        out["address_problem"] = await name_problem(ws, new_instance, "production", [out["address"]]) if out["address"] else None
        if chooses_machine(me):
            lang = request_language(request)
            names = sorted(settings.link_targets)
            answers = await asyncio.gather(*(staging_host_there(n, me, template.name, instance, ws.name, size)
                                             for n in names))
            for name, (data, why) in zip(names, answers):
                if why:
                    out["unlisted"].append({"name": name, "why": i18n.tr(lang, why)})
                else:
                    out["machines"].append({"name": name, **data})
        return out

    @app.get("/api/servers/{server}/stagings/elsewhere", dependencies=[Depends(admin)])
    async def stagings_elsewhere(server: str, me: Principal = Depends(current_user)):
        """The stagings of server's production made on the linked machines
        (UX-008 §5.1): each link's /api/instances, the servers whose production
        is server of this machine; a link that does not answer is left out
        and named. For an admin of this machine, as New staging's choice."""
        if not chooses_machine(me) or server not in settings.servers:
            return {"stagings": [], "unread": []}
        here = here_name()

        async def ask(name: str):
            link = settings.link_targets[name]
            headers = {**link_headers(link, me.name, me.role), "Accept": "application/json"}
            try:
                resp = await link_clients.client(link).get("/api/instances", headers=headers, timeout=LIST_TIMEOUT)
                data = resp.json() if resp.status_code == 200 else None
            except (httpx.HTTPError, ValueError):
                data = None
            if not isinstance(data, dict):
                return name, None
            return name, [{"link": name, "server": str(srv.get("name") or ""), "created": srv.get("created") or "",
                           "state": ((srv.get("instances") or {}).get("production") or {}).get("state") or "",
                           # its page: through this machine's link, for who logs in here
                           "url": f"/links/{name}/servers/{srv.get('name')}" if me.kind == "user" else None}
                          for srv in data.get("servers") or [] if isinstance(srv, dict)
                          and srv.get("parent") == server and srv.get("parent_machine") == here]
        answers = await asyncio.gather(*(ask(n) for n in sorted(settings.link_targets)))
        return {"stagings": [r for _, rows in answers if rows for r in rows],
                "unread": [n for n, rows in answers if rows is None]}

    async def staging_host_there(name: str, me: Principal, template: str, instance: str, workspace: str,
                                 size: int | None) -> tuple[dict | None, str]:
        """({workspaces, default}, "") when the link name can hold a staging
        from template (GET /api/stagings/host there), else (None, why not);
        without room there ({need, free}, why not)."""
        link = settings.link_targets.get(name)
        if link is None:
            return None, f"No link {name}"
        headers = {**link_headers(link, me.name, me.role), "Accept": "application/json"}
        try:
            resp = await link_clients.client(link).get(
                "/api/stagings/host", headers=headers, timeout=LIST_TIMEOUT,
                params={"template": template, "instance": instance, "workspace": workspace})
        except httpx.HTTPError:
            return None, "does not answer"
        if resp.status_code == 404:
            return None, "its OmniaCLI is older: it makes no staging of another machine"
        if resp.status_code in (401, 403):
            return None, "the link is not an admin there"
        if resp.status_code != 200:
            return None, f"answered HTTP {resp.status_code}"
        try:
            data = resp.json()
        except ValueError:
            return None, "it did not answer JSON"
        if not isinstance(data, dict):
            return None, "it did not answer JSON"
        if not data.get("template"):
            labels = [str(x) for x in data.get("labels") or []]
            return None, f"it has only {', '.join(labels)}" if labels else f"it has no template {template}"
        if not data.get("storage") or not data.get("docker"):
            return None, "no storage for workspaces"
        rows = [w for w in data.get("workspaces") or [] if isinstance(w, dict) and w.get("name")]
        need = STAGING_ROOM * size if size else 0
        fits = [w for w in rows if not need or w.get("free") is None or w["free"] >= need]
        if not fits:
            free = max((w.get("free") or 0 for w in rows), default=0)
            # the numbers too, for the refusal of staging_elsewhere
            return {"need": need, "free": free}, f"no room: about {gb_text(need)} GB are needed, {gb_text(free)} GB are free"
        # the workspace of the same name first, made there when it is missing
        fits.sort(key=lambda w: w["name"] != workspace)
        return {"workspaces": fits, "default": fits[0]["name"], "need": need}, ""

    @app.get("/api/stagings/host", dependencies=[Depends(admin)])
    async def staging_host(template: str = "", instance: str = "", workspace: str = ""):
        """Asked by another machine through its link before it sends a
        staging here (UX-008 §5.1): whether this one has the template and
        room for workspaces; its workspaces with the room in their disk and
        the address the staging would get there; the workspace of that name,
        new, when there is none."""
        found_templates = await asyncio.to_thread(workspaceops.templates)
        # a template on the machine's cluster: only where the cluster is
        if template in found_templates and found_templates[template].on_machine and not machinedb.registry():
            found_templates = {k: v for k, v in found_templates.items() if k != template}
        out = {"template": template in found_templates,
               "labels": sorted({t.label for t in found_templates.values()}),
               "storage": bool(settings.storage_roots), "docker": workspaceops.docker_missing() is None,
               "workspaces": []}
        if not out["template"] or not out["storage"]:
            return out
        try:
            space = await workspaceops.space()
        except workspaceops.SpaceError:
            space = None
        now, base = datetime.datetime.now(), instance if WORKSPACE_NAME.fullmatch(instance) else "odoo"
        for ws in sorted(settings.workspaces.values(), key=lambda w: w.name):
            if not ws.storage or not ws.limits.get("disk_gb"):
                continue
            new_instance = stagings.instance_name(base, now, set(ws.instances()))
            out["workspaces"].append({"name": ws.name, "label": ws.label, "free": workspace_free(space, ws),
                                      "address": staging_address(ws, new_instance)})
        if WORKSPACE_NAME.fullmatch(workspace) and workspace not in settings.workspaces:
            allowed = quick_room(space)
            if allowed >= QUICK_DISK_GB[0]:
                out["workspaces"].append({"name": workspace, "label": workspace, "new": True, "address": "",
                                          "free": allowed * workspaceops.GB})
        return out

    @app.post("/api/servers/{server}/stagings", dependencies=[Depends(admin)])
    async def staging_create(request: Request, server: str, payload: Optional[dict] = Body(None),
                             me: Principal = Depends(current_user)):
        """A new staging of server's production: an instance beside it in its
        workspace, from the same template, its production loaded with a
        backup of server's production made now, neutralized, its test
        stopped; recorded with its parent and its date. With machine (a
        link, an admin of this machine): made there (staging_elsewhere)."""
        payload = payload if isinstance(payload, dict) else {}
        machine = str(payload.get("machine") or "").strip()
        if machine:
            return await staging_elsewhere(request, server, machine, str(payload.get("workspace") or "").strip(), me)
        return await new_staging(server, me)

    async def new_staging(server: str, me: Principal, load=None, title: str = "", extra: dict | None = None,
                          source: str = ""):
        """A staging of server beside it; load(job, parent production, the
        staging's production) fills it (default: a backup made now, restored
        neutralized); extra goes in its record (stagings.py)."""
        found = staging_source(server)
        if not isinstance(found, tuple):
            return found
        _prod, ws, instance = found
        template = await asyncio.to_thread(instance_template, ws, instance)
        if template is None:
            return refuse(f"The template {server} was made from is not on this machine any more")
        if server_busy(server):
            return refuse(f"{server} is busy: wait for its job")
        now = datetime.datetime.now()
        reserved = await reserve_staging(ws.name, instance, now)
        if not isinstance(reserved, tuple):
            return reserved
        new_instance, name = reserved

        async def backup_and_restore(job, prod, target) -> dict:
            backup_zip = await ops.backup_into(job, prod, backup_folder(prod), {"no_filestore": False})
            await target.plugin.restore(job, target, backup_zip, {"neutralize": True})
            return {"backup": backup_zip.name}

        async def fill(job, target) -> dict:
            # the modules uploaded as a .zip into the production go with it (README, Modules uploaded as a .zip)
            copy_uploads(job, server, target.server)
            if uploads_folder(*workspace_of(target.server), "production").is_dir():
                await workspaceops.write_conf(job, *workspace_of(target.server), "production",
                                              [r.name for r in target.repositories])
            return await (load or backup_and_restore)(job, settings.servers[server].production, target)

        on = enterprise_of(settings.servers[server])
        job = staging_job(ws.name, new_instance, template, fill, {"parent": server, **(extra or {})},
                          title or f"Staging {name} of {server}", [f"{server}/production"], me, now, source=source,
                          odoo_enterprise=on["series"] if on and not on["own"] else "")
        return {"job": job.id, "server": name}

    async def staging_elsewhere(request: Request, server: str, machine: str, workspace: str, me: Principal):
        """A staging of server's production on the machine of the link
        machine: the backup made here, sent there through the link
        (linkcopy.upload), the staging made there from the same template in
        workspace (that of the same name by default, made there when it is
        missing), loaded, neutralized, recorded there with this machine as
        its production's (POST /api/stagings/receive there, its job followed)."""
        if not chooses_machine(me):
            return refuse("Only an admin of this machine makes a staging on another machine", status.HTTP_403_FORBIDDEN)
        link = settings.link_targets.get(machine)
        if link is None:
            return refuse(f"No link {machine}", status.HTTP_404_NOT_FOUND)
        found = staging_source(server)
        if not isinstance(found, tuple):
            return found
        prod, ws, instance = found
        if not prod.plugin.has_backup:
            return refuse(f"{server} cannot be backed up: no staging of it")
        template = await asyncio.to_thread(instance_template, ws, instance)
        if template is None:
            return refuse(f"The template {server} was made from is not on this machine any more")
        if server_busy(server):
            return refuse(f"{server} is busy: wait for its job")
        size = await staging_size(prod)
        there, why = await staging_host_there(machine, me, template.name, instance, ws.name, size)
        if why:
            if there is not None:
                return refuse(f"There is no room on {machine}: about {gb_text(there['need'])} GB "
                              f"are needed, {gb_text(there['free'])} GB are free")
            if why.startswith("it has"):
                return refuse(f"{machine} has no template {template.name}, the production's: no staging there")
            return refuse(f"{machine} cannot hold the staging: {i18n.tr(request_language(request), why)}")
        chosen = workspace or there["default"]
        target_ws = next((w for w in there["workspaces"] if w["name"] == chosen), None)
        if target_ws is None:
            return refuse(f"No workspace {chosen} on {machine} with room for the staging")
        headers = link_headers(link, me.name, me.role)
        on = enterprise_of(settings.servers[server])
        params = {"template": template.name, "instance": instance, "parent": server, "machine": here_name(),
                  "workspace": chosen, "label": ws.label,
                  # Odoo Enterprise of the production: the staging there is too (UX-012)
                  **({"enterprise": on["series"]} if on and not on["own"] else {})}

        async def action(job):
            progress = job.result["progress"] = {
                "steps": [{"id": "backup", "label": "Backup", "where": here_name()},
                          {"id": "download", "label": "Transfer", "where": f"{here_name()} → {machine}"},
                          {"id": "restore", "label": "Staging", "where": machine}],
                "step": "backup", "done": 0, "total": 0}
            # the machines the data goes between ("": this one), for the map's line (UX-009)
            job.result["transfer"] = {"from": "", "to": machine}
            source = settings.servers[server].production
            backup_zip = await ops.backup_into(job, source, backup_folder(source), {"no_filestore": False})
            progress["step"] = "download"
            client = link_clients.client(link)
            answer = await linkcopy.upload(job, client, link, headers, backup_zip, "/api/stagings/receive",
                                           {**params, "file": backup_zip.name}, progress)
            remote_id, name = str(answer.get("job") or ""), str(answer.get("server") or "")
            if not remote_id or not name:
                raise JobFailed(f"{machine} started no job for the staging")
            job.result.update(server=name, link=machine, url=f"/links/{machine}/servers/{name}",
                              # its job there, as the map names it: one row for the two (UX-009)
                              follows=f"{machine}:{remote_id}")
            progress["step"] = "restore"
            await linkcopy.follow(job, client, link, headers, remote_id, "the staging")
            job.log(f"{name} on {machine}: a copy of {server} of now, neutralized")

        job = jobs.submit(f"Staging of {server} on {machine}", [f"{server}/production"], f"manual: {me}", action,
                          workspace=ws.name)
        return {"job": job.id, "machine": machine}

    @app.post("/api/stagings/receive", dependencies=[Depends(admin)])
    async def staging_receive(request: Request, template: str = "", instance: str = "", parent: str = "",
                              machine: str = "", workspace: str = "", label: str = "", file: str = "",
                              enterprise_series: str = Query("", alias="enterprise"),
                              me: Principal = Depends(current_user)):
        """A staging of a production of another machine, sent by it through
        its link (staging_elsewhere there): the backup zip is the raw body,
        kept in the staging's backups; the staging made in workspace (made
        here when missing) from template, loaded with it, neutralized,
        recorded with its parent and its machine (stagings.py). A job."""
        if me.kind == "token":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "A token may only read")
        tpl = (await asyncio.to_thread(workspaceops.templates)).get(template)
        if tpl is None:
            return refuse(f"No template {template} here: a staging is made from the template of its production")
        if not (WORKSPACE_NAME.fullmatch(instance) and WORKSPACE_NAME.fullmatch(workspace)
                and _STAGING_PARENT.fullmatch(parent) and _STAGING_PARENT.fullmatch(machine)
                and linkcopy.BACKUP_NAME.fullmatch(file)):
            return refuse("A staging is sent with its template, instance, parent, machine, workspace and file")
        if enterprise_series and enterprise.series_of_template(tpl.name) != enterprise_series:
            return refuse(f"The production is Odoo Enterprise {enterprise_series}: the template {tpl.name} is another "
                          "version")
        if not settings.storage_roots:
            return refuse(f"No storage for workspaces on this machine: {workspaceops.HELPERS_HINT}")
        if workspaceops.docker_missing():
            return refuse(workspaceops.docker_missing())
        made_workspace = workspace not in settings.workspaces
        if made_workspace:
            refused = await quick_workspace(workspace, label or workspace)
            if refused:
                return refused
        now = datetime.datetime.now()
        reserved = await reserve_staging(workspace, instance, now)
        if not isinstance(reserved, tuple):
            if made_workspace:
                await drop_quick_workspace(workspace)
            return reserved
        new_instance, name = reserved
        folder = private.backup_folder(settings.backup_dir / name / "production")
        dest = folder / file
        length = request.headers.get("content-length", "")
        free = shutil.disk_usage(folder).free
        refused = None
        if length.isdigit() and free - int(length) < puller.MIN_FREE:
            refused = refuse(f"There is no room on {here_name()}: about {gb_text(int(length))} GB are needed, "
                             f"{gb_text(free)} GB are free", status.HTTP_507_INSUFFICIENT_STORAGE)
        else:
            handle, part_name = tempfile.mkstemp(dir=folder, prefix=f"{file}.", suffix=".part")
            part = Path(part_name)
            try:
                with os.fdopen(handle, "wb") as out:
                    async for chunk in request.stream():
                        out.write(chunk)
                if length.isdigit() and part.stat().st_size != int(length):
                    refused = refuse(f"{file}: {part.stat().st_size} of {length} bytes received")
                else:
                    part.replace(dest)
            except (ClientDisconnect, OSError) as exc:
                refused = refuse(f"{file} was not received whole: {exc}")
            finally:
                part.unlink(missing_ok=True)
        if refused is not None:
            await rename_in_workspaces(name, None)
            if made_workspace:
                await drop_quick_workspace(workspace)
            return refused

        async def fill(job, target) -> dict:
            job.log(f"--- the backup {file} of {parent} on {machine} into {target.key}, neutralized")
            await target.plugin.restore(job, target, dest, {"neutralize": True})
            return {"backup": file}

        job = staging_job(workspace, new_instance, tpl, fill, {"parent": parent, "machine": machine},
                          f"Staging {name} of {parent} on {machine}", [], me, now,
                          # no instance made: its backup goes too
                          not_made=lambda: dest.unlink(missing_ok=True), odoo_enterprise=enterprise_series)
        access_log.record("action", me.kind, me.name, request.client.host if request.client else "",
                          f"staging {name} of {parent} on {machine} received")
        return {"job": job.id, "server": name}

    async def drop_quick_workspace(name: str) -> None:
        """A workspace quick_workspace made, when nothing went in it: out of
        the workspaces again (its disk is made with its first instance)."""
        current = settings.workspaces.get(name)
        if name in quick_workspaces and current is not None and not current.servers:
            async with workspaces_lock:
                await store_workspaces({n: w for n, w in settings.workspaces.items() if n != name})
            quick_workspaces.discard(name)

    @app.post("/api/servers/{server}/stagings/refresh", dependencies=[Depends(admin)])
    async def staging_refresh(server: str, payload: Optional[dict] = Body(None), me: Principal = Depends(current_user)):
        """Rebuild (UX-008 §5.2): a staging made again from its production as
        it is now: a new backup of the parent's production restored into the
        staging's, neutralized. Its code stays as it is (what is being tried
        there), or with code: true its repositories go back to the commits
        of the production first (code_like_production)."""
        code = bool((payload if isinstance(payload, dict) else {}).get("code"))
        info = stagings.load(stagings_file).get(server) or {}
        parent = info.get("parent")
        if info.get("machine") and settings.servers.get(server) is not None:
            return refuse(f"The production of {server} is on {info['machine']}: make a new staging from there")
        srv, parent_srv = settings.servers.get(server), settings.servers.get(parent or "")
        if srv is None or srv.production is None or parent_srv is None or parent_srv.production is None:
            return refuse(f"{server} is not a staging of a production here", status.HTTP_404_NOT_FOUND)
        if not srv.production.plugin.has_restore or not parent_srv.production.plugin.has_backup:
            return refuse(f"{server} cannot be loaded from {parent}")
        ws = workspace_of(server)
        if code and ws is None:
            return refuse(f"{server} was not made in a workspace: only its data can be loaded again")
        if server_busy(server) or server_busy(parent):
            return refuse("A job is running on the staging or its production: wait for it")

        async def action(job):
            if code:
                await code_like_production(job, server, parent)
            prod, target = settings.servers[parent].production, settings.servers[server].production
            job.log(f"--- a new backup of {prod.key}")
            backup_zip = await ops.backup_into(job, prod, backup_folder(prod), {"no_filestore": False})
            await target.plugin.restore(job, target, backup_zip, {"neutralize": True})
            data = stagings.load(stagings_file)
            data[server] = {**data.get(server, {}), "parent": parent, "backup": backup_zip.name,
                            "refreshed": datetime.datetime.now().isoformat(timespec="seconds")}
            await asyncio.to_thread(stagings.save, stagings_file, data)
            job.log(f"{server} has the data and the code of {prod.key} of now, neutralized" if code else
                    f"{server} has the data of {prod.key} of now, neutralized; its code is as it was")

        title = f"Rebuild the staging {server} from {parent}" if code else f"Refresh the staging {server} from {parent}"
        job = jobs.submit(title, [f"{parent}/production", f"{server}/production"],
                          f"manual: {me}", action, workspace=ws[0].name if ws else "")
        return {"job": job.id}

    async def code_like_production(job, server: str, parent: str) -> None:
        """The repositories of the staging server's production at the commits
        the production of parent has checked out (Rebuild with its code): each
        one of the same name fetched from the production's own clone and
        checked out on the production's branch, its changes here dropped; one
        the production has and the staging not cloned; one only the staging
        has taken away. Its odoo.conf and the servers file follow."""
        ws, instance = workspace_of(server)
        prod, target = settings.servers[parent].production, settings.servers[server].production
        mine = {r.name: r for r in target.repositories}
        rows, added = [], []
        for theirs in prod.repositories:
            commit = await ops.git(theirs, "rev-parse", "HEAD")
            repo = mine.get(theirs.name)
            if repo is None:
                job.log(f"--- [{theirs.name}] not in {server}: cloned as in {parent}")
                url = await ops.git(theirs, "remote", "get-url", theirs.remote)
                path = await workspaceops.clone(job, ws, instance, "production", theirs.name, url, theirs.branch,
                                                gitcredentials.options_for(settings, server))
                repo = dataclasses.replace(theirs, path=path, remote="origin")
                added.append(theirs.name)
            job.log(f"--- [{repo.name}] to {commit[:10]} of {parent} ({theirs.branch})")
            changes = await ops.git(repo, "status", "--porcelain", "--untracked-files=no")
            if changes:
                job.log(f"[{repo.name}] its changes in {server} are dropped:\n{changes}")
            # the commit from the production's clone: it is there even when its remote moved on
            await ops.run(job, repo.git("fetch", str(theirs.path), "+HEAD:refs/omniacli/production"), cwd=repo.path)
            await ops.run(job, repo.git("checkout", "--force", "-B", theirs.branch, commit), cwd=repo.path)
            await ops.update_submodules(job, dataclasses.replace(repo, branch=theirs.branch))
            rows.append({"name": repo.name, "path": str(repo.path), "branch": theirs.branch, "remote": repo.remote})
        gone = [r for name, r in mine.items() if name not in {t.name for t in prod.repositories}]
        # the modules uploaded as a .zip, as the production has them
        copy_uploads(job, parent, server)
        await workspaceops.write_conf(job, ws, instance, "production", [r["name"] for r in rows])
        done = await set_repositories(server, {"production": rows})
        for repo in gone:
            job.log(f"--- [{repo.name}] only in {server}: taken away")
            workspaceops.remove_clone(workspaceops.addons_folder(ws, instance, "production") / repo.name)
        pip = getattr(done.production.plugin, "pip_requirements", None)
        for name in added if pip is not None else []:
            await pip(job, done.production, name)


    # --- a staging's mail, caught in its project and read here (stagingmail.py, UX-008 §5.3)

    def staging_mail_meta(server: str):
        """(the production of a staging of a workspace whose mail is caught,
        its instance.json), or (None, {}): a staging made since the
        templates have the service, its instance.json says so."""
        srv, found = settings.servers.get(server), workspace_of(server)
        if srv is None or srv.production is None or found is None or srv.production.restart.type != "compose" \
                or server not in stagings.load(stagings_file):
            return None, {}
        ws, instance = found
        try:
            meta = json.loads((workspaceops.storage_path(ws) / ws.name / instance / workspacehelper.INSTANCE_FILE)
                              .read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None, {}
        if isinstance(meta, dict) and workspacehelper.captures_mail(meta, "production"):
            return srv.production, meta
        return None, {}

    def staging_mail(server: str):
        """The production of a staging whose mail is caught, or None."""
        return staging_mail_meta(server)[0]

    def staging_inbox(server: str):
        """The production of a staging whose caught mail reaches mailboxes
        its Odoo reads (UX-008 §5.4), or None."""
        instance, meta = staging_mail_meta(server)
        return instance if instance is not None and workspacehelper.receives_mail(meta, "production") else None

    async def json_body(request: Request):
        """The request's JSON, {} when it has none or none that parses."""
        try:
            return await request.json()
        except ValueError:
            return {}

    def no_inbox(server: str):
        major = workspacehelper.without_alias_domain(staging_mail_meta(server)[1])
        if major is not None:
            return refuse(f"{server} receives no mail: Odoo {major} has no alias domain, which its mailboxes need "
                          f"(Odoo {workspacehelper.INBOX_FROM} and later). Its outgoing mail is caught only",
                          status.HTTP_404_NOT_FOUND)
        return refuse(f"{server} receives no mail: it is no staging, or one made before its mailboxes "
                      "(its outgoing mail is caught only)", status.HTTP_404_NOT_FOUND)

    def no_mail(server: str):
        return refuse(f"{server} catches no mail: it is no staging, or one made before the mail was caught "
                      "(its outgoing mail is off)", status.HTTP_404_NOT_FOUND)

    @app.get("/api/servers/{server}/production/mail")
    async def staging_mail_list(server: str, every: bool = False, box: str = "", me: Principal = Depends(anyone)):
        """The mail Odoo of the staging sent, caught: the newest first, the
        first 50 or every one (?every=1); of one mailbox (?box=<address>);
        the mailboxes (every address of it)."""
        instance, meta = await asyncio.to_thread(staging_mail_meta, server)
        if instance is None:
            return no_mail(server)
        try:
            found = await stagingmail.messages(instance, every, box)
        except LookupError as exc:
            return refuse(str(exc))
        except JobFailed as exc:
            return refuse(str(exc), status.HTTP_502_BAD_GATEWAY)
        receives = workspacehelper.receives_mail(meta, "production")
        return {**found, "first": stagingmail.FIRST, "can_clear": me.can("admin"), "receives": receives,
                "can_write": receives and me.can("developer"),
                "domain": stagingmail.domain_of(server) if receives else ""}

    # --- its incoming mail (UX-008 §5.4): declared before .../mail/{mail_id}

    @app.get("/api/servers/{server}/production/mail/inbox")
    async def staging_inbox_state(server: str, me: Principal = Depends(anyone)):
        """What Odoo of the staging reads of its mailboxes: on or off, the
        alias domain, the mailboxes, the last fetch (nothing changes)."""
        instance = await asyncio.to_thread(staging_inbox, server)
        if instance is None:
            return no_inbox(server)
        try:
            found = await stagingmail.inbox(instance, server, "state")
        except JobFailed as exc:
            return refuse(str(exc), status.HTTP_502_BAD_GATEWAY)
        return {**found, "can_act": me.can("developer")}

    @app.post("/api/servers/{server}/production/mail/inbox")
    async def staging_inbox_act(request: Request, server: str, me: Principal = Depends(developer)):
        """{"fetch": false}: Odoo of the staging reads its mailboxes (the
        alias domain of the staging, an incoming server per mailbox, the two
        mail crons on); {"fetch": true}: that, then its outgoing queue sent
        and every mailbox read now. The workspace's developers and admins."""
        instance = await asyncio.to_thread(staging_inbox, server)
        if instance is None:
            return no_inbox(server)
        body = await json_body(request)
        fetch = isinstance(body, dict) and body.get("fetch") is True
        try:
            found = await stagingmail.inbox(instance, server, "fetch" if fetch else "on")
        except JobFailed as exc:
            return refuse(str(exc), status.HTTP_502_BAD_GATEWAY)
        access_log.record("action", me.kind, me.name, request.client.host if request.client else "",
                          f"incoming mail of {instance.key} " + ("read now" if fetch else "switched on"))
        lang = request_language(request)
        said = [i18n.tr(lang, line) for line in found.get("said") or [] if isinstance(line, str)]
        return {**found, "said": said, "can_act": True}

    @app.post("/api/servers/{server}/production/mail/send")
    async def staging_mail_send(request: Request, server: str, me: Principal = Depends(developer)):
        """A message written as an outside sender ({from, name, to, subject,
        body, in_reply_to}): into the caught mail, and to the mailbox of a
        .test address that Odoo reads. The workspace's developers and admins."""
        instance = await asyncio.to_thread(staging_inbox, server)
        if instance is None:
            return no_inbox(server)
        body = await json_body(request)
        if not isinstance(body, dict):
            return refuse("the message is a JSON object")
        message = {k: body[k] for k in ("from", "name", "to", "subject", "body", "in_reply_to") if k in body}
        if isinstance(message.get("to"), str):
            message["to"] = [a.strip() for a in message["to"].replace(";", ",").split(",") if a.strip()]
        try:
            await stagingmail.send(instance, message)
        except JobFailed as exc:
            return refuse(str(exc))
        access_log.record("action", me.kind, me.name, request.client.host if request.client else "",
                          f"mail written to {instance.key} as {str(message.get('from'))[:80]}")
        return {"ok": True}

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
    # the record of the tuning of the dedicated databases (the machine's Database tab)
    tuning = pgtuning.Store(settings.jobs_dir.parent / pgtuning.FILE)
    postgres_tasks: list[asyncio.Task] = []
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

    def cluster_pitr_view(found: dict, whole: bool) -> dict:
        """What the page of an instance on the machine's cluster shows of
        the cluster's archive (AD-006). whole (an admin of the machine): all
        of it; a workspace's user: whether it is on, the range of minutes,
        the time a recovery takes and whether the machine has the room,
        never the sizes of the cluster (they are of every customer's data)."""
        room = found.get("room") or {}
        view = {"machine": True, "enabled": bool(found.get("enabled")), "first": found.get("first"),
                "last": found.get("last"), "failed": int(found.get("failed") or 0) if found.get("failing") else 0,
                "last_failed": found.get("last_failed") if found.get("failing") else "",
                "seconds": found.get("seconds"), "fits": bool(room.get("fits")),
                "recovering": bool(found.get("recovering")), "bases": [], "segments": 0, "archive_size": 0}
        if whole:
            view.update({"whole": True, "bases": found.get("bases") or [], "segments": found.get("segments") or 0,
                         "archive_size": found.get("archive_size") or 0, "need": room.get("need"),
                         "free": room.get("free"), "reserve": room.get("reserve")})
        return view

    @app.get("/api/servers/{server}/pitr", dependencies=[Depends(developer)])
    async def pitr_state(server: str, me: Principal = Depends(current_user)):
        """Whether it is on, the base backups, the first and last minute a restore can reach."""
        ws, instance, _ = pitr_target(server)
        try:
            found = await workspaceops.ask("pitr-state", ws.name, ws.storage, instance, "production")
        except JobFailed as exc:
            return refuse(str(exc), status.HTTP_502_BAD_GATEWAY)
        if found.get("machine"):
            found = cluster_pitr_view(found, machine_admin(me))
        # this machine's time: the page tells a range that ended long ago from one that is still growing
        return {**found, "now": datetime.datetime.now().isoformat(timespec="seconds")}

    @app.post("/api/servers/{server}/pitr/enable", dependencies=[Depends(admin)])
    async def pitr_enable(server: str, me: Principal = Depends(current_user)):
        """Point-in-time recovery switched on: the database archives its WAL
        (a volume of its own, its service made again: a few seconds without
        database), a first base backup; then one each night."""
        ws, instance, prod = pitr_target(server)
        if on_machine_db(prod):
            return refuse(workspacehelper.PITR_ON_CLUSTER)
        if on_remote_db(prod):
            return refuse(workspacehelper.PITR_ON_REMOTE)

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
                    if found is None or srv is None or srv.production is None or jobs.is_busy(srv.production.key) \
                            or on_machine_db(srv.production) or on_remote_db(srv.production):
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
            try:
                await cluster_archive_tick()
            except Exception:  # noqa: BLE001 - the loop must survive
                _logger.exception("recovery at a minute of the machine's PostgreSQL: the hourly round failed")

    async def cluster_sweep() -> None:
        """What a recovery of the machine's cluster that was killed left (its
        temporary cluster, its dump) deleted: when OmniaCLI starts and each
        hour; a recovery still running is left alone (clusterpitr.sweep)."""
        if not settings.storage_roots or not machinedb.registry():
            return
        try:
            found = await workspaceops.ask("machine-db-recover-sweep", timeout=600)
        except JobFailed as exc:
            _logger.warning("the temporary clusters of recoveries could not be swept: %s", exc)
            return
        for major, done in (found.get("swept") or {}).items():
            if any(done.get(k) for k in ("folders", "containers", "files")):
                _logger.warning("machine's PostgreSQL %s: what a recovery at a minute that did not end left is deleted "
                                "(%s temporary clusters, %s containers, %s files)", major, done.get("folders"),
                                done.get("containers"), done.get("files"))
                access_log.record("action", "system", "", "", "temporary cluster of a recovery at a minute of the "
                                  f"machine's PostgreSQL {major} that did not end: deleted", context=False)

    async def cluster_base_job(job, major: str) -> None:
        found = await workspaceops._helper(job, "machine-db-archive-base", major, timeout=PITR_RESTORE_TIMEOUT)
        pruned = found.get("pruned") or {}
        job.log(f"Full copy {found.get('base')} of the machine's PostgreSQL {major}: {found.get('size', 0) >> 20} MiB "
                f"in {found.get('seconds')} s")
        if pruned.get("skipped"):
            job.log(f"Nothing older deleted: {pruned['skipped']}")
        else:
            job.log(f"{len(pruned.get('kept') or [])} full copies stay; deleted as older than them: "
                    f"{pruned.get('bases', 0)} full copies, {pruned.get('segments', 0)} files of changes")
        if found.get("over"):
            job.warn(f"The full copies and the record of the changes take {gb_text(found.get('used', 0))} GB, more "
                     f"than the {gb_text(found.get('budget', 0))} GB of their budget: raise it, or keep fewer full "
                     "copies (Database tab of the machine)")

    async def cluster_archive_tick() -> None:
        """Each hour, for each cluster of the machine whose archive is on: a
        base backup when the last is a day old, the budget passed said."""
        await cluster_sweep()
        if not settings.storage_roots:
            return
        for major in machinedb.registry():
            key = f"{MACHINE_KEY}/{major}"
            try:
                found = await workspaceops.ask("machine-db-archive-state", major, timeout=120)
            except JobFailed as exc:
                _logger.warning("the archive of the machine's PostgreSQL %s cannot be read: %s", major, exc)
                continue
            if not found.get("enabled"):
                continue
            if found.get("over"):
                _logger.warning("the archive of the machine's PostgreSQL %s takes %s GB, more than the %s GB of its "
                                "budget", major, gb_text(found.get("used", 0)), gb_text(found.get("budget", 0)))
            try:
                last = datetime.datetime.fromisoformat(str((found.get("bases") or [{}])[-1].get("time") or ""))
                old = (datetime.datetime.now() - last).total_seconds() >= PITR_BASE_AGE
            except ValueError:
                old = True
            if old and not jobs.is_busy(key):
                jobs.submit(f"Full copy of the machine's PostgreSQL {major} for the recovery at a minute", [key],
                            "point-in-time recovery", lambda job, major=major: cluster_base_job(job, major))

    pitr_tasks: list[asyncio.Task] = []

    async def stop_pitr() -> None:
        for task in pitr_tasks:
            task.cancel()
        await asyncio.gather(*pitr_tasks, return_exceptions=True)
        pitr_tasks.clear()

    @app.post("/api/servers/{server}/pitr/base", dependencies=[Depends(admin)])
    async def pitr_base(server: str, me: Principal = Depends(current_user)):
        ws, instance, prod = pitr_target(server)
        if on_machine_db(prod):
            return refuse(workspacehelper.PITR_ON_CLUSTER)
        if on_remote_db(prod):
            return refuse(workspacehelper.PITR_ON_REMOTE)
        job = jobs.submit(f"Base backup for point-in-time recovery of {prod.key}", [prod.key], f"manual: {me}",
                          lambda job: pitr_base_job(job, ws, instance, server), workspace=ws.name)
        return {"job": job.id}

    @app.post("/api/servers/{server}/pitr/restore", dependencies=[Depends(admin)])
    async def pitr_restore(server: str, payload: dict = Body(...), me: Principal = Depends(current_user)):
        """A new staging with the database of the production as it was at a
        minute: its cluster made again from the archive, the filestore of
        now linked (Odoo never changes a file there, only adds), neutralized."""
        ws, instance, prod = pitr_target(server)
        if on_remote_db(prod):
            return refuse(workspacehelper.PITR_ON_REMOTE)
        when = str(payload.get("time") or "").strip()
        try:
            at = datetime.datetime.fromisoformat(when)
        except ValueError:
            return refuse(f"{when!r} is not a time, as 2026-11-20T09:41")
        if on_machine_db(prod):
            return await cluster_staging_at(server, me, ws, instance, at.replace(tzinfo=None, second=0, microsecond=0))

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

    async def cluster_staging_at(server: str, me: Principal, ws: Workspace, instance: str, at: datetime.datetime):
        """A new staging with the database of an instance on the machine's
        cluster as it was at a minute (AD-006): the root helper replays a
        temporary copy of the whole cluster up to that minute, takes that
        one database from it into the staging's database, and deletes the
        copy whatever happens (clusterpitr.restore). Refused before a
        staging is made when the archive is off, the minute is out of its
        range, the machine's disk has not the room, another one is running."""
        whole = machine_admin(me)
        try:
            found = await workspaceops.ask("pitr-state", ws.name, ws.storage, instance, "production")
        except JobFailed as exc:
            return refuse(str(exc), status.HTTP_502_BAD_GATEWAY)
        if not found.get("enabled"):
            return refuse("Recovery at a minute is off for the machine's PostgreSQL: an admin of the machine switches "
                          "it on, in the Database tab of the machine")
        first, last = str(found.get("first") or ""), str(found.get("last") or "")
        if not first or not last:
            return refuse("No full copy of the machine's PostgreSQL yet: the first is being made, or failed")
        if first[:16] > last[:16]:
            # right after the only full copy: its minute is not past yet
            return refuse(f"The full copy a recovery starts from has just been made: the first minute that can be "
                          f"reached is {first[:16].replace('T', ' ')}, in a moment")
        if not first[:16] <= at.isoformat(timespec="minutes") <= last[:16]:
            return refuse(f"{at:%Y-%m-%d %H:%M} is outside the minutes that can be reached: from "
                          f"{first[:16].replace('T', ' ')} to {last[:16].replace('T', ' ')}")
        if found.get("recovering"):
            return refuse("Another recovery at a minute is running on this machine: wait for it to end",
                          status.HTTP_409_CONFLICT)
        room = found.get("room") or {}
        if not room.get("fits"):
            if whole:
                return refuse(f"The machine's disk has not the room: the temporary copy of the machine's PostgreSQL "
                              f"needs about {gb_text(room.get('need', 0))} GB, {gb_text(room.get('free', 0))} GB are "
                              f"free and {gb_text(room.get('reserve', 0))} GB must stay free",
                              status.HTTP_507_INSUFFICIENT_STORAGE)
            return refuse("The machine's disk has not the room for the temporary copy a recovery works on: ask the "
                          "machine's admin", status.HTTP_507_INSUFFICIENT_STORAGE)

        async def load(job, parent, target) -> dict:
            target_instance = target.server[len(ws.name) + 1:]
            job.log(f"--- the database of {parent.key} as it was at {at:%Y-%m-%d %H:%M}: a temporary copy of the "
                    "machine's PostgreSQL is replayed up to that minute, this database is taken from it, the copy "
                    "is deleted")
            done = await workspaceops._helper(job, "machine-db-recover", ws.name, ws.storage, instance, target_instance,
                                              at.isoformat(timespec="minutes"), timeout=PITR_RESTORE_TIMEOUT)
            steps = done.get("steps") or {}
            job.log(f"Replayed from the full copy of {str(done.get('base') or '').replace('T', ' ')} in "
                    f"{done.get('seconds')} s (unpacked in {steps.get('unpack', '?')} s, replayed in "
                    f"{steps.get('replay', '?')} s, read in {steps.get('dump', '?')} s, the staging stopped in "
                    f"{steps.get('stop', '?')} s, written in {steps.get('restore', '?')} s). The temporary copy "
                    "is deleted")
            await ops.run(job, target.restart.command("link-filestore", target.options.odoo_service,
                                                      target.options.database, parent.restart.service,
                                                      parent.options.database), timeout=3600)
            # neutralized as every staging, before its Odoo starts: no outgoing mail, no scheduled actions
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
        # its database on the cluster of a linked OmniaCLI (read before its record goes): that machine takes it back
        production = settings.servers[name].production
        linked = linked_database(production) if production is not None and on_remote_db(production) else None
        if linked is not None and (instance_meta(production).get("promoted_from") or {}).get("dedicated"):
            # on the promoted copy of its own dedicated database (UX-011 §D): no cluster there to take a database
            # back; the copy stays on that machine, which drops it from its Database tab once this instance is gone
            linked = None
        # on a promoted copy (of the machine's cluster, or of its own database): the provider's machine, whose
        # address the job, which the workspace's users read, does not say
        provider = production is not None and on_remote_db(production) and bool(instance_meta(production).get("promoted_from"))

        async def action(job):
            if linked is None and provider:
                await workspaceops.remove_instance(job, ws, instance, linked=True)
                job.log("Its database stays on its copy on another machine of the provider, which drops it there")
            elif linked is None:
                await workspaceops.remove_instance(job, ws, instance)
            else:
                await workspaceops.remove_instance(job, ws, instance, linked=True)
                await linked_release(job, linked["link"], linked["major"], linked["database"],
                                     me.on_behalf or me.name, me.role)
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
                    await publish_names(job, current)
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

    # --- the Addresses dialog of an environment (UX-008 §6.1) ----------------
    # Its other names (aliases) and the names that send to it (redirects), in
    # the workspace's nginx site the root helper writes (web). Only where
    # OmniaCLI writes that site: an instance of a workspace, never an instance
    # adopted from a server, whose nginx is whoever administers the machine's.

    NOT_OUR_SITE = "The site is kept by whoever administers the machine: OmniaCLI does not touch nginx here"

    def address_owner(server: str, role: str):
        """(workspace, instance) of the environment server/role whose site
        OmniaCLI writes, or why not (a text)."""
        srv = settings.servers.get(server)
        if srv is None or role not in ROLES or getattr(srv, role) is None:
            return f"No instance {server}/{role}"
        found = workspace_of(server)
        if found is None or getattr(srv, role).restart.type != "compose":
            return NOT_OUR_SITE
        ws, instance = found
        if not ws.storage:
            return f"Workspace {ws.name} has no storage: the machine's admins set it"
        if not ws.domain:
            return f"Workspace {ws.name} has no domain yet: its site is written once the machine's admins give it one"
        return ws, instance

    def address_rows(me: Principal, envs: list[tuple[str, str]]) -> dict[str, dict]:
        """For the Environments table: {server/role: {names, can, reason}},
        names its aliases and redirects (everyone reads them), can and reason
        of the ⚙ (its admins only: UX-008 §3)."""
        rows = {}
        for server, role in envs:
            owner = address_owner(server, role)
            if isinstance(owner, tuple):
                ws, instance = owner
                names = [*ws.aliases.get(instance, {}).get(role, []),
                         *(f"{n} → {to}" for n, to in ws.redirects.get(instance, {}).get(role, {}).items())]
            else:
                names = []
            rows[f"{server}/{role}"] = {"names": names, "can": isinstance(owner, tuple) and me.can("admin"),
                                        "reason": "" if isinstance(owner, tuple) else owner}
        return rows

    def address_reason(me: Principal, taken: str) -> str:
        """Why a name is taken, as me is told: a user of workspaces is not
        told that another customer of this machine has it, or another site
        (21/11/2026, FUGA 3): only that it is not available; the names of
        its own workspace keep their reason."""
        if taken and me.scoped and taken in ADDRESS_TAKEN_ELSEWHERE:
            return "not available"
        return taken

    def addresses_of_env(server: str, role: str):
        owner = address_owner(server, role)
        if not isinstance(owner, tuple):
            code = status.HTTP_404_NOT_FOUND if owner.startswith("No instance") else status.HTTP_400_BAD_REQUEST
            return refuse(owner, code)
        return owner

    async def nginx_elsewhere(ws: Workspace) -> dict[str, str]:
        """The names the other sites of nginx answer on (the helper's web-names)."""
        found = await workspaceops.ask("web-names", ws.name)
        return {str(k): str(v) for k, v in (found.get("elsewhere") or {}).items()}

    @app.get("/api/servers/{server}/{role}/addresses", dependencies=[Depends(admin)])
    async def addresses_get(server: str, role: str):
        """What the dialog shows: the main address (the workspace's rule),
        the other names, the redirects, the names a redirect may go to."""
        owner = addresses_of_env(server, role)
        if not isinstance(owner, tuple):
            return owner
        ws, instance = owner
        own = [n for n, at in generated_names(ws).items() if at == (instance, role)]
        return {"workspace": ws.name, "main": own[0] if own else "", "workspace_names": own,
                "aliases": ws.aliases.get(instance, {}).get(role, []),
                "redirects": [{"from": n, "to": to} for n, to in ws.redirects.get(instance, {}).get(role, {}).items()],
                "max": ADDRESSES_MAX}

    @app.post("/api/servers/{server}/{role}/addresses/check", dependencies=[Depends(admin)])
    async def addresses_check(server: str, role: str, request: Request, payload: dict = Body(...),
                              me: Principal = Depends(current_user)):
        """Each name as the dialog shows it before Save: a host name, free
        (OmniaCLI's workspaces, the other sites of nginx), and where its DNS
        points: here, elsewhere (with this machine's address), none."""
        owner = addresses_of_env(server, role)
        if not isinstance(owner, tuple):
            return owner
        ws, instance = owner
        given = payload.get("names")
        if not isinstance(given, list) or len(given) > 2 * ADDRESSES_MAX:
            return refuse(f"names: a list of at most {2 * ADDRESSES_MAX} names")
        try:
            elsewhere, nginx_error = await nginx_elsewhere(ws), ""
        except JobFailed as exc:
            elsewhere, nginx_error = {}, str(exc).removeprefix("workspace-helper: ")
        here = await asyncio.to_thread(workspaceweb.machine_addresses, shutil.which("ip"))
        rows = []
        for raw in given:
            row = {"name": str(raw or "").strip().lower().rstrip("."), "error": "", "taken": "", "dns": "", "addresses": []}
            try:
                row["name"] = address_name("name", raw)
            except ConfigError as exc:
                row["error"] = str(exc).removeprefix("name: ")
                rows.append(row)
                continue
            taken = address_taken(row["name"], settings.workspaces, ws.name, instance, role)
            hit = workspaceweb.takers(row["name"], elsewhere)
            if not taken and hit:
                # the file of another site is the machine's: its admins only
                taken = f"used by {hit[0][1]}" if machine_admin(me) else "another site of this machine answers on it"
            row["taken"] = address_reason(me, taken or "")
            rows.append(row)
        found = await asyncio.gather(*(asyncio.to_thread(workspaceweb.addresses_of, r["name"])
                                       for r in rows if not r["error"]))
        for row, addresses in zip([r for r in rows if not r["error"]], found):
            row["addresses"], row["dns"] = addresses, workspaceweb.dns_state(addresses, here)
        lang = request_language(request)
        for row in rows:
            row["error"], row["taken"] = i18n.tr(lang, row["error"]), i18n.tr(lang, row["taken"])
        return {"rows": rows, "here": sorted(here), "nginx_error": i18n.tr(lang, nginx_error)}

    @app.post("/api/servers/{server}/{role}/addresses/save", dependencies=[Depends(admin)])
    async def addresses_save(server: str, role: str, payload: dict = Body(...), me: Principal = Depends(current_user)):
        """The other names and the redirects of the environment, saved in its
        workspace; then a job writes the site again and asks for the
        certificate (Odoo does not restart)."""
        aliases, redirects = payload.get("aliases"), payload.get("redirects")
        if not isinstance(aliases, list) or not isinstance(redirects, list) or \
                any(not isinstance(r, dict) for r in redirects):
            return refuse("aliases: a list of names; redirects: a list of pairs from, to")
        async with workspaces_lock:
            owner = addresses_of_env(server, role)
            if not isinstance(owner, tuple):
                return owner
            ws, instance = owner
            try:
                aliases = [address_name("name", n) for n in aliases]
                pairs = {address_name("name", r.get("from")): address_name("name", r.get("to")) for r in redirects}
            except ConfigError as exc:
                return refuse(str(exc).removeprefix("name: "))
            if len(pairs) != len(redirects) or len(set(aliases)) != len(aliases):
                return refuse("A name is twice in the addresses of this environment")
            data = ws.to_dict()
            for key, value in (("aliases", aliases), ("redirects", pairs)):
                envs = {i: dict(r) for i, r in (data.get(key) or {}).items()}
                envs.setdefault(instance, {})[role] = value
                data[key] = {i: r for i, r in envs.items() if any(r.values())}
            try:
                found = parse_workspaces({**{n: w.to_dict() for n, w in settings.workspaces.items()}, ws.name: data},
                                         settings.storage_roots)
            except ConfigError as exc:
                said = str(exc).removeprefix(f"workspaces.{ws.name}: ")
                taken = re.fullmatch(r"(\S+) is taken: (.+)", said)
                if taken and address_reason(me, taken[2]) != taken[2]:
                    return refuse(f"{taken[1]} is not available: choose another name")
                return refuse(said)
            new = found[ws.name]
            if (new.aliases, new.redirects) == (ws.aliases, ws.redirects):
                return {"ok": True}
            # a name another site of nginx has would fail every publish of the
            # workspace after this one: refused here, not in the job
            added = [n for n, i, r in new.extra_names() if (i, r) == (instance, role)]
            try:
                elsewhere = await nginx_elsewhere(ws)
            except JobFailed as exc:
                return refuse(f"The other sites of nginx cannot be read: {str(exc).removeprefix('workspace-helper: ')}")
            for name in added:
                hit = workspaceweb.takers(name, elsewhere)
                if hit:
                    # the file of another site is the machine's: its admins only
                    if machine_admin(me):
                        return refuse(f"{name} is used by {hit[0][1]}: choose another name")
                    return refuse(f"Another site of this machine answers on {name}: choose another name"
                                  if not me.scoped else f"{name} is not available: choose another name")
            refused = await store_workspaces(found)
            if refused:
                return refused
        return {"ok": True, "job": submit_publish(new, f"manual: {me}").id}

    def instance_name_problem(ws: Workspace, instance: str) -> str | None:
        """Why a new instance may not be named instance: one of the names it
        would answer on is another one's alias or redirect already."""
        names = {n for n, at in generated_names(dataclasses.replace(
            ws, servers=[*ws.servers, workspaceops.server_name(ws, instance)])).items() if at[0] == instance}
        for name, other, role in ws.extra_names():
            if name in names:
                return f"{instance} would answer on {name}, another name of {environment_label(other, role)}: an instance needs another name"
        return None

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

    async def add_repositories(job, ws: Workspace, name: str, instance: str, wanted: list[tuple[str, str, dict]]):
        """Repositories (name, url, {role: branch}) cloned into each role of
        the instance with the workspace's git credentials, named in their
        odoo.conf and in the servers file, their Python packages installed,
        the roles restarted; none of them left when one fails. The server as
        it is then (Add a repository, the catalog of modules)."""
        server = settings.servers[name]
        roles = [role for role in ROLES if getattr(server, role) is not None]
        options = gitcredentials.options_for(settings, name)
        current = {role: list(getattr(server, role).repositories) for role in roles}
        cloned: dict[str, dict[str, Path]] = {role: {} for role in roles}
        try:
            for repo, url, branches in wanted:
                for role in roles:
                    cloned[role][repo] = await workspaceops.clone(job, ws, instance, role, repo, url, branches[role],
                                                                  options)
            for role in roles:
                await workspaceops.write_conf(job, ws, instance, role,
                                              [*(r.name for r in current[role]), *(w[0] for w in wanted)])
            done = await set_repositories(name, {role: [*repository_rows(current[role]), *(
                {"name": repo, "path": str(cloned[role][repo]), "branch": branches[role], "remote": "origin"}
                for repo, _, branches in wanted)] for role in roles})
        except BaseException:
            for role in roles:
                await workspaceops.write_conf(job, ws, instance, role, [r.name for r in current[role]])
                for path in cloned[role].values():
                    workspaceops.remove_clone(path)
            raise
        for role in roles:
            # their Python packages before Odoo starts with their modules
            pip = getattr(getattr(done, role).plugin, "pip_requirements", None)
            if pip is not None:
                for repo, _, _ in wanted:
                    await pip(job, getattr(done, role), repo)
            await ops.restart(job, getattr(done, role))
        return done

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
        if repo == moduleupload.FOLDER:
            return refuse(f"{repo} is the folder of the modules uploaded as a .zip: give the repository another name")

        async def action(job):
            await add_repositories(job, ws, name, instance, [(repo, url, branches)])
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

    # --- the Modules tab: the catalog of the OCA's modules (UX-008 §7) -----

    catalog_dir = settings.jobs_dir.parent
    # the catalog files read, by their path: (mtime, Catalog)
    catalog_memo: dict[Path, tuple[float, modulecatalog.Catalog]] = {}
    # what each database knows, asked at most every KNOWN_SECONDS: key → (time, {module: state} or None, why)
    known_memo: dict[str, tuple[float, dict | None, str]] = {}
    KNOWN_SECONDS = 30
    CATALOG_EVERY = 3600
    # the hours (this machine's) the catalog of each series is read again, once a day
    CATALOG_NIGHT = range(2, 5)

    def catalog_for(series: str) -> modulecatalog.Catalog:
        path = modulecatalog.path_of(catalog_dir, series)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return modulecatalog.Catalog(series)
        kept = catalog_memo.get(path)
        if kept is None or kept[0] != mtime:
            kept = (mtime, modulecatalog.catalog_of(modulecatalog.load(path), series))
            catalog_memo[path] = kept
        return kept[1]

    def catalog_series() -> list[str]:
        """The series of the templates this machine makes instances from."""
        return sorted({s for t in workspaceops.templates().values()
                       if (s := modulecatalog.series_of(t.name, t.label))}, key=lambda s: int(s.split(".")[0]))

    def submit_catalog_refresh(series: str, source: str):
        key = f"catalog/{series}"
        running = jobs.running_on(key)
        if running is not None:
            return running
        path = modulecatalog.path_of(catalog_dir, series)
        return jobs.submit(f"Read the catalog of the OCA for Odoo {series}", [key], source,
                           lambda job: modulecatalog.refresh(job, path, series, modulecatalog.GitSource()))

    async def catalog_loop() -> None:
        """Each night, the catalog of each series of the templates read again."""
        while True:
            await asyncio.sleep(CATALOG_EVERY)
            try:
                if datetime.datetime.now().hour not in CATALOG_NIGHT or not settings.workspaces:
                    continue
                today = datetime.date.today().isoformat()
                for series in await asyncio.to_thread(catalog_series):
                    if str(catalog_for(series).tried or "")[:10] != today:
                        submit_catalog_refresh(series, "nightly catalog")
            except Exception:  # noqa: BLE001 - the loop must survive
                _logger.exception("the nightly reading of the catalog of modules failed")

    catalog_tasks: list[asyncio.Task] = []

    async def stop_catalog() -> None:
        for task in catalog_tasks:
            task.cancel()
        await asyncio.gather(*catalog_tasks, return_exceptions=True)
        catalog_tasks.clear()

    def modules_instance(server: str):
        """(workspace, instance name, server, series) of an instance whose
        modules come from the catalog; a refusal otherwise."""
        srv = settings.servers.get(server)
        found = workspace_of(server)
        if srv is None or found is None or srv.production is None:
            return refuse(f"{server} is not an instance of a workspace: its modules come from its repositories",
                          status.HTTP_404_NOT_FOUND)
        ws, instance = found
        template = instance_template(ws, instance)
        series = modulecatalog.series_of(template.name, template.label) if template is not None else None
        if series is None:
            return refuse(f"The version of Odoo of {server} is not known: its template says none")
        return ws, instance, srv, series

    def is_staging(server: str) -> bool:
        return server in stagings.load(stagings_file)

    def install_roles(me: Principal, srv, staging: bool) -> dict[str, str | None]:
        """{role: None when me may install there, else why not} of the roles
        srv has: test for a developer, production for an admin (D3); the
        production of a staging is a copy, a developer's too (its test is
        stopped: not offered)."""
        found = {}
        for role in ROLES:
            if getattr(srv, role) is None or (staging and role == "test"):
                continue
            if role == "test" or staging:
                found[role] = None if me.can("developer") else "The developer role is required"
            else:
                found[role] = None if me.can("admin") else (
                    "Installing in production is for the admins: a developer installs in test")
        return found

    async def known_modules(inst: Instance, fresh: bool = False) -> tuple[dict | None, str]:
        """({module: state} the database of inst knows, or None; why not)."""
        kept = known_memo.get(inst.key)
        if kept is not None and not fresh and time.monotonic() - kept[0] < KNOWN_SECONDS:
            return kept[1], kept[2]
        reader = getattr(inst.plugin, "known_modules", None)
        if reader is None:
            found, why = None, f"{inst.key} does not say its modules"
        else:
            try:
                found, why = await reader(inst), ""
            except JobFailed as exc:
                found, why = None, str(exc)
        known_memo[inst.key] = (time.monotonic(), found, why)
        return found, why

    def code_modules(srv) -> set[str]:
        """The modules in the repositories of the instance's roles, and those uploaded as a .zip."""
        found = set()
        for role in ROLES:
            inst = getattr(srv, role)
            for repo in (inst.repositories if inst is not None else []):
                for manifest in repo.path.glob("*/__manifest__.py"):
                    if modulecatalog.MODULE.fullmatch(manifest.parent.name):
                        found.add(manifest.parent.name)
        made = workspace_of(srv.name)
        if made is not None:
            for role in ROLES:
                if getattr(srv, role) is not None:
                    found.update(moduleupload.record(uploads_folder(made[0], made[1], role)))
        return found

    def repository_names(srv) -> set[str]:
        return {r.name for role in ROLES if getattr(srv, role) is not None for r in getattr(srv, role).repositories}

    async def modules_view(srv) -> tuple[dict, dict, set, set]:
        """(known of each role, why each is not known, the modules in the code, the repositories)."""
        roles = [role for role in ROLES if getattr(srv, role) is not None]
        answers = await asyncio.gather(*(known_modules(getattr(srv, role)) for role in roles))
        known = {role: answer[0] for role, answer in zip(roles, answers)}
        why = {role: answer[1] for role, answer in zip(roles, answers) if answer[1]}
        code = await asyncio.to_thread(code_modules, srv)
        return known, why, code, repository_names(srv)

    def module_row(entry: dict, state: str, blocked: str | None) -> dict:
        return {**{key: entry.get(key) for key in ("module", "name", "summary", "repository", "license", "version",
                                                   "development_status", "depends", "python")},
                "state": state, "blocked": blocked}

    @app.get("/api/catalog", dependencies=[Depends(developer)])
    async def catalog_search(series: str = "", q: str = ""):
        """The catalog of the OCA for a series of Odoo (the date it was read,
        its modules), and the modules matching q (at most 50)."""
        if not modulecatalog.SERIES.fullmatch(series):
            return refuse(f"{series!r} is not a series of Odoo, as 19.0")
        found = catalog_for(series)
        rows, total = modulecatalog.search(found, q)
        return {**found.meta(), "total": total, "modules": [module_row(r, "", None) for r in rows]}

    @app.post("/api/catalog/refresh", dependencies=[Depends(admin)])
    async def catalog_refresh(payload: dict = Body(...), me: Principal = Depends(current_user)):
        """The catalog of a series read again from the OCA's repositories, a job."""
        series = str(payload.get("series") or "")
        if not modulecatalog.SERIES.fullmatch(series):
            return refuse(f"{series!r} is not a series of Odoo, as 19.0")
        if not machine_admin(me):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "The admins of the machine read the catalog again")
        return {"job": submit_catalog_refresh(series, f"manual: {me}").id}

    @app.get("/api/servers/{server}/modules", dependencies=[Depends(developer)])
    async def modules_summary(request: Request, server: str, me: Principal = Depends(current_user)):
        """What the Add modules dialog opens with: the catalog's date and
        size, where me may install, and why a database cannot be read."""
        found = modules_instance(server)
        if not isinstance(found, tuple):
            return found
        ws, _, srv, series = found
        staging = is_staging(server)
        roles = install_roles(me, srv, staging)
        known, why, _, _ = await modules_view(srv)
        say = functools.partial(i18n.tr, request_language(request))
        meta = catalog_for(series).meta()
        return {**meta, "error": say(meta["error"]) if meta["error"] else None, "server": server, "staging": staging,
                "roles": [{"role": role, "refused": say(refused) if refused else None,
                           "unread": say(why[role]) if why.get(role) else None}
                          for role, refused in roles.items()],
                "default": "test" if "test" in roles else "production",
                "can_refresh": machine_admin(me), "busy": server_busy(server),
                "installed": {role: sorted(m for m, s in (k or {}).items() if s in modulecatalog.INSTALLED)
                              for role, k in known.items()}}

    @app.get("/api/servers/{server}/modules/catalog", dependencies=[Depends(developer)])
    async def modules_catalog(request: Request, server: str, q: str = "", role: str = ""):
        """The modules of the catalog matching q (at most 50), each with its
        state in the instance and why it cannot be installed in role."""
        found = modules_instance(server)
        if not isinstance(found, tuple):
            return found
        _, _, srv, series = found
        catalog = catalog_for(series)
        rows, total = modulecatalog.search(catalog, q)
        known, _, code, repos = await modules_view(srv)
        role = role if role in known else ("test" if "test" in known else "production")
        result = []
        for entry in rows:
            state = modulecatalog.state_of(entry["module"], known, code)
            blocked = modulecatalog.plan(catalog, entry["module"], role, known, code, repos)["blocked"]
            result.append(module_row(entry, state, i18n.tr(request_language(request), blocked) if blocked else None))
        return {**catalog.meta(), "total": total, "query": q, "modules": result}

    @app.get("/api/servers/{server}/modules/plan", dependencies=[Depends(developer)])
    async def modules_plan(request: Request, server: str, module: str = "", role: str = "test"):
        """What installing module in role takes (Install…): the repositories
        to add, the modules, the Python packages; nothing is done."""
        found = modules_instance(server)
        if not isinstance(found, tuple):
            return found
        _, _, srv, series = found
        if role not in ROLES or getattr(srv, role) is None:
            return refuse(f"{server} has no {role}", status.HTTP_404_NOT_FOUND)
        catalog = catalog_for(series)
        known, why, code, repos = await modules_view(srv)
        made = modulecatalog.plan(catalog, module, role, known, code, repos)
        entry = catalog.modules.get(module) or {}
        say = functools.partial(i18n.tr, request_language(request))
        return {**made, "blocked": say(made["blocked"]) if made["blocked"] else None,
                "name": entry.get("name") or module, "series": series,
                "restarts": [r for r in ROLES if getattr(srv, r) is not None] if made["repositories"] else [role],
                "unread": say(why[role]) if why.get(role) else None}

    @app.get("/api/servers/{server}/{role}/modules", dependencies=[Depends(developer)])
    async def modules_installed(server: str, role: str):
        """The modules installed in the database of the role."""
        inst = get_instance(server, role)
        known, why = await known_modules(inst, fresh=True)
        if known is None:
            return refuse(why, status.HTTP_502_BAD_GATEWAY)
        return {"modules": sorted(m for m, s in known.items() if s in modulecatalog.INSTALLED)}

    @app.post("/api/servers/{server}/{role}/modules/install", dependencies=[Depends(developer)])
    async def modules_install(server: str, role: str, payload: dict = Body(...),
                              me: Principal = Depends(current_user)):
        """A job: the repositories the module needs added to production and
        test (both restarted), a backup of production when it goes there,
        then odoo -i <module> in the database of the role and its restart."""
        found = modules_instance(server)
        if not isinstance(found, tuple):
            return found
        ws, instance, srv, series = found
        if role not in ROLES or getattr(srv, role) is None:
            return refuse(f"{server} has no {role}", status.HTTP_404_NOT_FOUND)
        staging = is_staging(server)
        allowed = install_roles(me, srv, staging)
        if role not in allowed:
            return refuse(f"The test of the staging {server} is stopped: install in the staging itself")
        refused = allowed[role]
        if refused:
            raise HTTPException(status.HTTP_403_FORBIDDEN, refused)
        module = str(payload.get("module") or "")
        if not modulecatalog.MODULE.fullmatch(module):
            return refuse(f"{module!r} is not the name of a module")
        production = role == "production" and not staging
        if production and payload.get("confirm") is not True:
            return refuse(f"Installing {module} in the production of {server} asks for a confirmation: "
                          "a backup first, and production restarts")
        if server_busy(server):
            return refuse(f"A job is running on {server}: try again when it ends")
        catalog = catalog_for(series)
        known, why, code, repos = await modules_view(srv)
        made = modulecatalog.plan(catalog, module, role, known, code, repos)
        if made["blocked"]:
            return refuse(made["blocked"])
        if not made["modules"]:
            return refuse(f"{module} is installed in {server}/{role} already")
        roles = [r for r in ROLES if getattr(srv, r) is not None]
        source = modulecatalog.GitSource()

        async def action(job):
            if made["repositories"]:
                job.log(f"--- the repositories {', '.join(made['repositories'])} of the OCA, branch {series}, "
                        f"added to {', '.join(roles)}")
                await add_repositories(job, ws, server, instance,
                                       [(repo, source.url(repo), {r: series for r in roles})
                                        for repo in made["repositories"]])
            inst = getattr(settings.servers[server], role)
            if production:
                # a backup first, whatever the instance's Backup before an update says
                await ops.backup_before_update(job, dataclasses.replace(inst, backup_before_update=True),
                                               backup_folder(inst))
            first = len(job.lines)
            try:
                await inst.plugin.install_modules(job, inst, [module])
            except JobFailed:
                raise JobFailed(f"The module {module} was not installed: {odoo_error(job.lines[first:])}. "
                                "The full log is in the job.") from None
            finally:
                known_memo.pop(inst.key, None)
            await ops.restart(job, inst)
            now_known, why_not = await known_modules(inst, fresh=True)
            if now_known is not None and now_known.get(module) not in modulecatalog.INSTALLED:
                raise JobFailed(f"The module {module} was not installed: {odoo_error(job.lines[first:])}. "
                                "The full log is in the job.")
            if now_known is None:
                job.log(f"WARNING: whether {module} is installed is not known: {why_not}")
            else:
                job.log(f"{module} is installed in {inst.key}")
            job.result = {"server": server, "role": role, "module": module, "repositories": made["repositories"]}

        keys = [f"{server}/{r}" for r in roles] if made["repositories"] else [f"{server}/{role}"]
        job = jobs.submit(f"Install {module} in {server}/{role}", keys, f"manual: {me}", action, workspace=ws.name)
        return {"job": job.id}

    def odoo_error(lines: list[str]) -> str:
        """The first error Odoo logged, without its time and logger."""
        for line in lines:
            found = re.search(r"\b(?:ERROR|CRITICAL) \S+ \S+: (.+)", line)
            if found:
                return found.group(1).strip()[:300]
        for line in lines:
            if "Error" in line or "error" in line:
                return line.strip()[-300:]
        return "Odoo gave no reason"

    # --- modules uploaded as a .zip (README, Modules uploaded as a .zip;
    # moduleupload.py): sent, checked and kept aside (POST …/modules/upload),
    # then put in production and test with a job (…/upload/<id>/apply)

    upload_dir = settings.jobs_dir.parent / "module-uploads"
    # the zips checked and not put yet: id → {server, path, file, checked, by, at}
    pending_uploads: dict[str, dict] = {}
    UPLOAD_TTL = 3600

    def uploads_folder(ws: Workspace, instance: str, role: str) -> Path:
        return workspaceops.addons_folder(ws, instance, role) / moduleupload.FOLDER

    def upload_instance(server: str, me: Principal):
        """(workspace, instance, server, series) of an instance me may upload a
        module to: a workspace's (an adopted Odoo keeps its addons path in its
        own odoo.conf, which OmniaCLI does not write), an admin's of it, a
        person (not a machine acting by itself); a refusal otherwise."""
        srv = settings.servers.get(server)
        if srv is not None and workspace_of(server) is None:
            return refuse(f"{server} is not an instance of a workspace: its Odoo keeps its addons path in its own "
                          "odoo.conf, which OmniaCLI does not write. Put the module in one of its repositories")
        found = modules_instance(server)
        if not isinstance(found, tuple):
            return found
        if a_machine(me) or not me.can("admin"):
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                "Uploading a module is for the admins of the instance, as adding a repository")
        return found

    def repository_modules(srv) -> dict[str, str]:
        """{module: repository} of the modules in the repositories of the instance's roles."""
        found = {}
        for role in ROLES:
            inst = getattr(srv, role)
            for repo in (inst.repositories if inst is not None else []):
                for manifest in repo.path.glob("*/__manifest__.py"):
                    found.setdefault(manifest.parent.name, repo.name)
        return found

    def uploaded_modules(ws: Workspace, instance: str, srv) -> dict[str, dict]:
        """{role: {module: its record}} of the modules uploaded into each role."""
        return {role: moduleupload.record(uploads_folder(ws, instance, role))
                for role in ROLES if getattr(srv, role) is not None}

    def drop_old_uploads() -> None:
        now = time.time()
        for ident, item in list(pending_uploads.items()):
            if now - item["at"] > UPLOAD_TTL:
                pending_uploads.pop(ident, None)
                item["path"].unlink(missing_ok=True)

    async def upload_facts(ws: Workspace, instance: str, srv, series: str, checked: dict) -> dict:
        """What putting the checked modules in the instance does: which it
        replaces, which are refused (a module of a repository, of Odoo
        itself), what is said (another series, a dependency missing)."""
        repos = await asyncio.to_thread(repository_modules, srv)
        uploaded = await asyncio.to_thread(uploaded_modules, ws, instance, srv)
        known, why, _, _ = await modules_view(srv)
        had = set().union(*(set(r) for r in uploaded.values()))
        mine = {m["module"] for m in checked["modules"]}
        refused, warnings = [], [m["warning"] for m in checked["modules"] if m["warning"]]
        anywhere = set().union(*(set(k or {}) for k in known.values()))
        for module in sorted(mine):
            if module in repos:
                refused.append(f"{module} is in the repository {repos[module]} of {srv.name} already: update it there")
            elif module in anywhere and module not in had:
                refused.append(f"{module} is a module of Odoo itself or of its image: it is not replaced by a zip")
        present = anywhere | set(repos) | had | mine
        for row in checked["modules"]:
            missing = [d for d in row["depends"] if d not in present]
            if missing:
                warnings.append(f"{row['module']} depends on {', '.join(missing)}, not in {srv.name}: installing it "
                                "fails until they are there")
        if why and not any(known.values()):
            warnings.append(f"The databases of {srv.name} did not answer: the modules of Odoo are not checked")
        states = {m: {role: (known.get(role) or {}).get(m) for role in known} for m in mine}
        return {"replaced": sorted(mine & had), "refused": refused, "warnings": warnings, "states": states}

    @app.post("/api/servers/{server}/modules/upload", dependencies=[Depends(admin)])
    async def module_upload(request: Request, server: str, name: str = "", me: Principal = Depends(current_user)):
        """A zip of one or more Odoo modules sent by the browser (the raw
        body), checked and kept aside for an hour: what it holds and what
        putting it in the instance does. Nothing is written in the instance."""
        found = upload_instance(server, me)
        if not isinstance(found, tuple):
            return found
        ws, instance, srv, series = found
        file = Path(name).name
        if not moduleupload.NAME.fullmatch(file):
            return refuse("The file must be a .zip with a plain name (letters, digits, . _ - and spaces)")
        drop_old_uploads()
        folder = private.backup_folder(upload_dir / "pending")
        ident = secrets.token_hex(12)
        dest = folder / f"{ident}.zip"
        size = 0
        try:
            with open(dest, "xb") as out:
                os.chmod(dest, 0o600)
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > moduleupload.MAX_ZIP:
                        raise moduleupload.Refused(f"The zip is larger than {moduleupload.MAX_ZIP >> 20} MB")
                    out.write(chunk)
            checked = await asyncio.to_thread(moduleupload.check, dest, series)
        except moduleupload.Refused as exc:
            dest.unlink(missing_ok=True)
            return refuse(str(exc))
        except ClientDisconnect:
            dest.unlink(missing_ok=True)
            return refuse(f"{file}: the upload was interrupted")
        facts = await upload_facts(ws, instance, srv, series, checked)
        pending_uploads[ident] = {"server": server, "path": dest, "file": file, "checked": checked, "by": str(me),
                                  "at": time.time()}
        say = functools.partial(i18n.tr, request_language(request))
        staging = is_staging(server)
        roles = install_roles(me, srv, staging)
        return {"id": ident, "file": file, "size": size, "series": series, "staging": staging,
                "modules": [{**m, "warning": say(m["warning"]) if m["warning"] else None} for m in checked["modules"]],
                "replaced": facts["replaced"], "refused": [say(t) for t in facts["refused"]],
                "warnings": [say(t) for t in facts["warnings"]], "states": facts["states"],
                "roles": [{"role": role, "refused": say(why) if why else None} for role, why in roles.items()],
                "restarts": [r for r in ROLES if getattr(srv, r) is not None and not (staging and r == "test")],
                "busy": server_busy(server)}

    @app.post("/api/servers/{server}/modules/upload/{ident}/apply", dependencies=[Depends(admin)])
    async def module_upload_apply(request: Request, server: str, ident: str, payload: dict = Body(...),
                                  me: Principal = Depends(current_user)):
        """A job: the modules of the zip checked before into production and
        test (each in place of the one of the same name uploaded before, with
        the names typed), their odoo.conf, a restart, Update the module list;
        then, when asked, installed (or updated, when they are installed) in
        one role, through the install's own rules (a backup first in
        production, after a confirmation)."""
        found = upload_instance(server, me)
        if not isinstance(found, tuple):
            return found
        ws, instance, srv, series = found
        drop_old_uploads()
        item = pending_uploads.get(ident)
        if item is None or item["server"] != server:
            return refuse("This upload is not here any more (an hour has passed, or it was used): send the zip again",
                          status.HTTP_404_NOT_FOUND)
        checked = item["checked"]
        facts = await upload_facts(ws, instance, srv, series, checked)
        if facts["refused"]:
            return refuse(facts["refused"][0])
        typed = " ".join(str(payload.get("replace") or "").split())
        wanted = ", ".join(facts["replaced"])
        if facts["replaced"] and typed != wanted:
            return refuse(f"Type {wanted} to replace the modules uploaded before")
        staging = is_staging(server)
        role = str(payload.get("install") or "")
        allowed = install_roles(me, srv, staging)
        if role:
            if role not in allowed:
                return refuse(f"{server} has no {role} to install in" if getattr(srv, role, None) is None
                              else f"The test of the staging {server} is stopped: install in the staging itself")
            if allowed[role]:
                raise HTTPException(status.HTTP_403_FORBIDDEN, allowed[role])
        production = role == "production" and not staging
        if production and payload.get("confirm") is not True:
            return refuse(f"Installing in the production of {server} asks for a confirmation: a backup first, and "
                          "production restarts")
        if server_busy(server):
            return refuse(f"A job is running on {server}: try again when it ends")
        pending_uploads.pop(ident, None)
        modules = [m["module"] for m in checked["modules"]]
        roles = [r for r in ROLES if getattr(srv, r) is not None]
        running = [r for r in roles if not (staging and r == "test")]

        async def action(job):
            stamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
            try:
                for each in roles:
                    folder = uploads_folder(ws, instance, each)
                    staged = workspaceops.addons_folder(ws, instance, each) / f".omniacli-upload-{ident}"
                    if staged.exists():
                        shutil.rmtree(staged)
                    try:
                        await asyncio.to_thread(moduleupload.unpack, item["path"], checked, staged)
                        before = await asyncio.to_thread(moduleupload.put, folder, staged, checked["modules"],
                                                         item["by"], item["file"], stamp)
                    except moduleupload.Refused as exc:
                        raise JobFailed(str(exc)) from None
                    finally:
                        if staged.exists():
                            shutil.rmtree(staged)
                    job.log(f"--- {', '.join(modules)} of {item['file']} in the uploaded modules of {each}"
                            + (f": {', '.join(before)} replaced" if before else ""))
            finally:
                item["path"].unlink(missing_ok=True)
            current = settings.servers[server]
            for each in roles:
                state = workspacehelper.role_state(workspaceops.storage_path(ws) / ws.name / instance / each)
                if moduleupload.FOLDER not in state["repositories"]:
                    await workspaceops.write_conf(job, ws, instance, each,
                                                  [r.name for r in getattr(current, each).repositories])
            for each in running:
                inst = getattr(current, each)
                await ops.restart(job, inst)
                lister = getattr(inst.plugin, "update_module_list", None)
                if lister is not None:
                    await lister(job, inst)
                known_memo.pop(inst.key, None)
            job.result = {"server": server, "modules": modules}
            if not role:
                job.log(f"{', '.join(modules)} in {server}: install them from Odoo's Apps, or with Upload a module "
                        "again")
                return
            inst = getattr(current, role)
            known, why_not = await known_modules(inst, fresh=True)
            installed = [m for m in modules if (known or {}).get(m) in modulecatalog.INSTALLED]
            new = [m for m in modules if m not in installed]
            if production:
                await ops.backup_before_update(job, dataclasses.replace(inst, backup_before_update=True),
                                               backup_folder(inst))
            first = len(job.lines)
            try:
                if new:
                    await inst.plugin.install_modules(job, inst, new)
                if installed:
                    await inst.plugin.update_named_modules(job, inst, installed)
            except JobFailed:
                raise JobFailed(f"{', '.join(modules)}: not installed in {inst.key}: {odoo_error(job.lines[first:])}. "
                                "The full log is in the job.") from None
            finally:
                known_memo.pop(inst.key, None)
            await ops.restart(job, inst)
            known, why_not = await known_modules(inst, fresh=True)
            if known is None:
                job.log(f"WARNING: whether {', '.join(modules)} are installed is not known: {why_not}")
            else:
                missing = [m for m in modules if known.get(m) not in modulecatalog.INSTALLED]
                if missing:
                    raise JobFailed(f"{', '.join(missing)}: not installed in {inst.key}: "
                                    f"{odoo_error(job.lines[first:])}. The full log is in the job.")
                job.log(f"{', '.join(modules)} installed in {inst.key}" + (f" ({', '.join(installed)} updated)"
                                                                            if installed else ""))
            job.result = {**job.result, "role": role}

        job = jobs.submit(f"Upload {', '.join(modules)} into {server}", [f"{server}/{r}" for r in roles],
                          f"manual: {me}", action, workspace=ws.name)
        access_log.record("action", me.kind, me.name, request.client.host if request.client else "",
                          f"modules {', '.join(modules)} of {item['file']} uploaded into {server}"
                          + (f", {', '.join(facts['replaced'])} replaced" if facts["replaced"] else "")
                          + (f", installed in {role}" if role else ""), server=server, workspace=ws.name)
        return {"job": job.id}

    @app.get("/api/servers/{server}/modules/uploaded", dependencies=[Depends(developer)])
    async def modules_uploaded(server: str):
        """The modules uploaded as a .zip into each role: name, version, who, when, the file."""
        found = modules_instance(server)
        if not isinstance(found, tuple):
            return found
        ws, instance, srv, _ = found
        rows = await asyncio.to_thread(uploaded_modules, ws, instance, srv)
        return {"roles": {role: [{"module": m, **r} for m, r in kept.items()] for role, kept in rows.items()}}

    def copy_uploads(job, parent: str, staging: str) -> None:
        """The modules uploaded into parent's production into the staging's
        production (its folder made again from them); its odoo.conf follows
        with write_conf."""
        source, target = workspace_of(parent), workspace_of(staging)
        if source is None or target is None:
            return
        copied = moduleupload.copy_folder(uploads_folder(source[0], source[1], "production"),
                                          uploads_folder(target[0], target[1], "production"))
        if copied:
            job.log(f"--- the uploaded modules of {parent} ({', '.join(copied)}) copied into {staging}")

    def instance_template(ws: Workspace, instance: str):
        """The template an instance was made from (the higher version installed), or None."""
        try:
            meta = json.loads((workspaceops.storage_path(ws) / ws.name / instance / workspacehelper.INSTANCE_FILE).read_text())
        except (OSError, ValueError):
            return None
        return workspaceops.templates().get(str(meta.get("template") or ""))

    def role_catches_mail(ws: Workspace, instance: str, role: str) -> bool:
        """The role's mail is caught in its project (a staging's production,
        UX-008 §5.3): its odoo.conf takes no SMTP option of the page."""
        try:
            meta = json.loads((workspaceops.storage_path(ws) / ws.name / instance / workspacehelper.INSTANCE_FILE)
                              .read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return isinstance(meta, dict) and workspacehelper.captures_mail(meta, role)

    def role_parameters(ws: Workspace, instance: str, role: str) -> dict | None:
        """What the page shows of a role: each parameter with its value (a
        password: whether it is set), the debug switch; of a staging whose
        mail is caught the SMTP ones marked mail, shown and not changed."""
        template = instance_template(ws, instance)
        if template is None or not template.parameters:
            return None
        state = workspacehelper.role_state(workspaceops.storage_path(ws) / ws.name / instance / role)
        caught = role_catches_mail(ws, instance, role)
        rows = []
        for param in template.parameters:
            value = state["parameters"].get(param.name, param.default)
            bounds = ws.parameter_bounds.get(param.name) or [param.min, param.max]
            rows.append({**param.to_dict(), "min": bounds[0], "max": bounds[1],
                         "value": "" if param.type == "password" else value,
                         "set": bool(value) if param.type == "password" else None,
                         "mail": caught and param.name in stacktemplates.MAIL_PARAMETERS})
        return {"parameters": rows, "debug": state["debug"], "extra": state["extra"], "mail_capture": caught}

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
                "locked": sorted(locked),
                # a staging whose mail is caught: its SMTP options are the mail service's, not changed here
                "mail": sorted(stacktemplates.MAIL_PARAMETERS)
                if await asyncio.to_thread(role_catches_mail, ws, name[len(ws.name) + 1:], role) else []}

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
        # a staging whose mail is caught: its SMTP options are not saved (its odoo.conf has the mail service's)
        caught = await asyncio.to_thread(role_catches_mail, ws, instance, role)
        if caught:
            given = {k: v for k, v in given.items() if k not in stacktemplates.MAIL_PARAMETERS}
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
            if isinstance(extra, dict) and caught:
                extra = {k: v for k, v in extra.items() if k not in stacktemplates.MAIL_PARAMETERS}
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
                    await publish_names(job, current)
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
    async def member_save(request: Request, workspace: str, payload: dict = Body(...),
                          me: Principal = Depends(current_user)):
        """A workspace's admin creates a user of it (new: true, a user of
        workspaces only: with a password, or invite: true, a one-time link
        where the person sets its own), changes a member's role, and the
        password of a member of this workspace only."""
        name, role = str(payload.get("name") or "").strip(), str(payload.get("role") or "")
        password, new = str(payload.get("password") or ""), bool(payload.get("new"))
        invited = new and bool(payload.get("invite"))
        if invited:
            password = ""
        if role not in ACCESS_ROLES:
            return refuse(f"The role must be one of {', '.join(ACCESS_ROLES)}")
        answer: dict = {"ok": True}
        async with access_lock, workspaces_lock:
            ws = a_workspace(workspace)
            if new:
                refused = check_new_name(me, "User", name, settings.fixed.users, settings.users, True)
                if refused:
                    return refused
                if not invited and len(password) < _MIN_PASSWORD:
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
                web.users[name] = (await asyncio.to_thread(unknown_password) if invited
                                   else await asyncio.to_thread(hash_password, password))
                if new:
                    web.roles[name] = WORKSPACE_ROLE
                refused = await store_access(web) or (None if invited else await drop_invitations(name))
                if refused:
                    return refused
            found = dict(settings.workspaces)
            found[workspace] = dataclasses.replace(ws, members={**ws.members, name: role})
            refused = await store_workspaces(found)
            if refused:
                return refused
            if invited:
                made = await invite(request, name, me, workspace)
                if not isinstance(made, dict):
                    return made
                answer.update(made)
        if invited:
            answer["mail_problem"] = await mail_invitation(request, str(payload.get("email") or "").strip(), name,
                                                           answer["invite_url"])
        return answer

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
                refused = await store_access(web) or await drop_invitations(name)
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
        try:
            # a deleted user's two-step verification goes with it (a new one of that name starts without)
            await asyncio.to_thread(second_factors.keep, set(settings.users))
        except OSError as exc:
            _logger.error("The second factors of the deleted users are not removed: %s", exc)
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

    def check_new_name(me: Principal, kind: str, name: str, fixed: dict, current: dict, new: bool):
        """check_name for a workspace's page: one namespace holds the users
        (and one the tokens) of every customer of the machine, so a user of
        workspaces is not told that a name exists elsewhere (21/11/2026,
        FUGA 2); the machine's admins are told which and where."""
        refused = check_name(kind, name, fixed, current, new)
        if refused is not None and me.scoped and new and (name in fixed or name in current):
            return refuse(NAME_NOT_AVAILABLE)
        return refused

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
                           "workspaces": sorted(w.name for w in settings.workspaces.values() if n in w.members),
                           "invitation": None if n in fixed.users else invitation_state(n),
                           "two_step": second_factors.on(n)}
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
                "can_mail": bool(settings.options.smtp_host),
                # where the customers log in (docs/portale-clienti.md)
                "portal_url": portal_url(), "portal_settable": bool(settings.storage_roots),
                "invitation_hours": INVITATION_HOURS,
                "link_names": sorted(settings.link_targets),
            },
        )

    @app.get("/help", dependencies=[Depends(developer)])
    async def help_page(request: Request):
        """How to create a link and how to connect an MCP client."""
        # seen through a link, the address the browser knows is the other instance's
        base = portal_url() or ("https://<address of this OmniaCLI>" if prefix_of(request) else str(request.base_url).rstrip("/"))
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

    # --- the access log (NIS 2, docs/nis2-audit.md) ----------------------

    def audit_day(value: str, end: bool = False) -> int | None:
        """A day gg/mm/aaaa or aaaa-mm-dd of this machine's time, as ns: its
        start (end: the start of the day after); None when empty or not one."""
        value = (value or "").strip()
        for shape in ("%Y-%m-%d", "%d/%m/%Y"):
            try:
                day = datetime.datetime.strptime(value, shape)
            except ValueError:
                continue
            if end:
                day += datetime.timedelta(days=1)
            return int(day.astimezone().timestamp()) * 10**9
        return None

    def audit_entries(since: str, until: str, who: str, event: str, q: str, workspace: str | None) -> list[dict]:
        """The entries of the period (the last 5000 when it has no start),
        of who and of event, with every word of q, of workspace (None:
        every one of them, and those of the machine), the newest first."""
        start, stop = audit_day(since), audit_day(until, end=True)
        entries = access_log.period(start or 0, stop) if start or stop else access_log.recent(5000)
        found = []
        for e in entries:
            if workspace is not None and e.get("workspace") != workspace and workspace not in (e.get("workspaces") or []):
                continue
            if who and who.lower() not in f"{e.get('name', '')} {e.get('on_behalf', '')}".lower():
                continue
            if event and e.get("event") != event:
                continue
            if q and not matches(" ".join(str(v) for v in e.values()), q):
                continue
            found.append(e)
        return found

    _AUDIT_COLUMNS = ("utc", "time", "event", "kind", "name", "on_behalf", "role", "workspace", "ip", "method",
                      "route", "server", "env", "status", "job", "detail", "prev")

    def audit_export(entries: list[dict], fmt: str, label: str) -> Response:
        """The entries as a CSV or a JSON file to save, the oldest first."""
        entries = entries[::-1]
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")
        if fmt == "json":
            body, media, suffix = json.dumps(entries, ensure_ascii=False, indent=1) + "\n", "application/json", "json"
        else:
            out = io.StringIO()
            writer = csv.writer(out)
            writer.writerow(_AUDIT_COLUMNS)
            for e in entries:
                row = []
                for key in _AUDIT_COLUMNS:
                    value = e.get(key, "")
                    row.append(" ".join(map(str, value)) if isinstance(value, list) else value)
                writer.writerow(row)
            body, media, suffix = out.getvalue(), "text/csv; charset=utf-8", "csv"
        return Response(body, media_type=media,
                        headers={"Content-Disposition": f'attachment; filename="access-{label}-{stamp}.{suffix}"'})

    audit_status_cache: dict = {}

    def audit_status() -> dict:
        """What the machine page says of the access log: its room, the clock,
        the SIEM; measured at most every minute."""
        now = time.monotonic()
        if audit_status_cache and now - audit_status_cache["at"] < 60:
            return audit_status_cache["status"]
        found = {"retention_days": settings.options.audit_retention_days,
                 "room": audit.room(access_log.span(), settings.options.audit_retention_days, access_log.path.parent),
                 "clock": audit.clock_status(), "siem": siem_forwarder.status()}
        audit_status_cache.update(at=now, status=found)
        return found

    def audit_context(request: Request, since: str, until: str, who: str, event: str, q: str,
                      workspace: Workspace | None) -> dict:
        entries = audit_entries(since, until, who, event, q, workspace.name if workspace else None)
        return {"entries": entries[:500], "total": len(entries), "q": q, "since": since, "until": until,
                "who": who, "event": event, "ws": workspace, "log_file": access_log.path,
                "failed_file": access_log.failed_path,
                "status": audit_status() if workspace is None else None}

    @app.get("/access", dependencies=[Depends(admin)])
    async def access_page(request: Request, q: str = "", since: str = "", until: str = "", who: str = "",
                          event: str = ""):
        """The access log: logins, failed logins, actions, reads of customer
        or security data, MCP calls, jobs; the newest first."""
        context = await asyncio.to_thread(audit_context, request, since, until, who, event, q, None)
        return _TEMPLATES.TemplateResponse(request, "access.html", context)

    @app.get("/api/access/export", dependencies=[Depends(admin)])
    async def access_export(since: str = "", until: str = "", who: str = "", event: str = "", q: str = "",
                            format: str = "csv"):
        """The entries of a period, as the page filters them: CSV or JSON."""
        entries = await asyncio.to_thread(audit_entries, since, until, who, event, q, None)
        return audit_export(entries, format, "machine")

    @app.get("/api/access/verify", dependencies=[Depends(admin)])
    async def access_verify():
        """Whether the chain of the access log holds, and where it breaks."""
        return await asyncio.to_thread(access_log.verify)

    @app.get("/workspaces/{workspace}/audit", dependencies=[Depends(admin)])
    async def workspace_audit(request: Request, workspace: str, q: str = "", since: str = "", until: str = "",
                              who: str = "", event: str = ""):
        """The access log of a workspace, for its admins (their auditors):
        what was done on its instances and by its users, by anyone, the
        machine's admins too; never the machine's own entries."""
        ws = a_workspace(workspace)
        context = await asyncio.to_thread(audit_context, request, since, until, who, event, q, ws)
        return _TEMPLATES.TemplateResponse(request, "access.html", context)

    @app.get("/api/workspaces/{workspace}/audit/export", dependencies=[Depends(admin)])
    async def workspace_audit_export(workspace: str, since: str = "", until: str = "", who: str = "",
                                     event: str = "", q: str = "", format: str = "csv"):
        ws = a_workspace(workspace)
        entries = await asyncio.to_thread(audit_entries, since, until, who, event, q, ws.name)
        return audit_export(entries, format, ws.name)

    @app.post("/api/access/users/save")
    async def user_save(request: Request, payload: dict = Body(...), me: Principal = Depends(admin)):
        """Create a user (new: true, with a password, or invite: true, a
        one-time link where the person sets its own) or change its role and,
        when given, its password."""
        name, role = str(payload.get("name") or "").strip(), str(payload.get("role") or "")
        password, new = str(payload.get("password") or ""), bool(payload.get("new"))
        invited = new and bool(payload.get("invite"))
        if invited:
            password = ""
        answer: dict = {"ok": True}
        async with access_lock:
            web = web_copy()
            refused = (check_name("User", name, settings.fixed.users, settings.users, new)
                       or check_role(role, USER_ROLES))
            if refused:
                return refused
            if ((new and not invited) or password) and len(password) < _MIN_PASSWORD:
                return refuse(f"The password needs at least {_MIN_PASSWORD} characters")
            if not new:
                if me.kind == "user" and name == me.name and role != me.role:
                    return refuse("You cannot change your own role")
                if role != "admin" and settings.user_role(name) == "admin" and not other_admins(name):
                    return refuse(f"{name} is the last admin")
            if invited:
                web.users[name] = await asyncio.to_thread(unknown_password)
            elif password:
                web.users[name] = await asyncio.to_thread(hash_password, password)
            web.roles[name] = role
            refused = await store_access(web) or (await drop_invitations(name) if password else None)
            if refused:
                return refused
            if invited:
                made = await invite(request, name, me)
                if not isinstance(made, dict):
                    return made
                answer.update(made)
        if invited:
            answer["mail_problem"] = await mail_invitation(request, str(payload.get("email") or "").strip(), name,
                                                           answer["invite_url"])
        return answer

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
            return await store_access(web) or await drop_invitations(name) or {"ok": True}

    # --- invitations: a user made without a password gets a one-time link,
    # valid INVITATION_HOURS, where the person sets its own and is logged in;
    # a user of a customer made from the Users page, here or on a linked machine

    invitations_path = invitations_file_of(settings.users_file) if settings.users_file else None
    try:
        invitations: list[Invitation] = load_invitations(invitations_path) if invitations_path else []
    except ConfigError as exc:
        _logger.error("The invitations are not read, none is valid: %s", exc)
        invitations = []

    def moment(ts: float) -> str:
        """A moment as the pages show it: dd/mm/yyyy hh:mm, the machine's time."""
        return time.strftime("%d/%m/%Y %H:%M", time.localtime(ts))

    def invitation_of(name: str) -> Invitation | None:
        """The newest invitation of name not used yet (pending or expired)."""
        found = [i for i in invitations if i.name == name and not i.used]
        return max(found, key=lambda i: i.created) if found else None

    def invitation_state(name: str) -> dict | None:
        """What the users pages show of name's invitation: None, none."""
        found = invitation_of(name)
        if found is None:
            return None
        return {"state": found.state(time.time()), "expires": moment(found.expires), "by": found.by}

    def invitation_by_token(token: str) -> Invitation | None:
        encoded = hash_token(token) if token else ""
        hit = None
        for item in invitations:
            # every one compared, so the time does not tell which one matched
            if secrets.compare_digest(encoded, item.token):
                hit = item
        return hit

    async def store_invitations(found: list[Invitation]):
        """Write the invitations (the caller holds access_lock); a response on error."""
        if invitations_path is not None:
            try:
                found = await asyncio.to_thread(save_invitations, invitations_path, found, time.time())
            except OSError as exc:
                return refuse(f"Cannot write {invitations_path}: {exc}", 500)
        invitations[:] = found
        return None

    async def drop_invitations(name: str):
        """name has a password now, or is gone: its links not used are no longer valid."""
        if any(i.name == name and not i.used for i in invitations):
            return await store_invitations([i for i in invitations if not (i.name == name and not i.used)])
        return None

    async def invite(request: Request, name: str, me: Principal, workspace: str = "", again: bool = False):
        """A new link for name (the one of before no longer valid), in the
        access log: the answer's part of it, or a response on error. The
        caller holds access_lock."""
        token, now = new_token(), time.time()
        made = Invitation(name, hash_token(token), now, now + INVITATION_HOURS * 3600, str(me), workspace)
        refused = await store_invitations([i for i in invitations if not (i.name == name and not i.used)] + [made])
        if refused:
            return refused
        access_log.record("invitation", me.kind, me.name, request.client.host if request.client else "",
                          f"{'sent again' if again else 'made'} for {name}, valid until {moment(made.expires)}",
                          workspace=workspace)
        path = f"/invite/{token}"
        base = str(request.base_url)
        # the customers' portal, where the person will log in: for a user of
        # the machine only when Options let them in there
        portal = portal_url() if (settings.options.portal_staff or settings.user_role(name) == WORKSPACE_ROLE) else ""
        if portal:
            base = portal
        elif prefix_of(request):
            # through a link the browser knows this machine by the address the other reaches it at
            base = request.headers.get("X-OmniaCLI-Address", "")
        url = base.rstrip("/") + path if re.fullmatch(r"https?://[^\s/]+(/[^\s]*)?", base) else ""
        # portal: the link is this machine's own address, the linking OmniaCLI keeps it
        return {"invite_path": path, "invite_url": url, "expires": moment(made.expires), "portal": bool(portal)}

    async def mail_invitation(request: Request, email: str, name: str, url: str) -> str:
        """The link by e-mail, through the SMTP server of the Options page:
        "" when sent (or none asked), else why not."""
        if not email or not url:
            return ""
        lang = request_language(request)
        if not notify.is_email(email):
            return i18n.tr(lang, f"{email} is not an e-mail address")
        # in the language of who invites: the person's is not known
        problems = await notify.send(
            settings.options, [email], i18n.tr(lang, "OmniaCLI: your invitation"),
            i18n.tr(lang, f"You are invited to OmniaCLI as {name}.\n\nOpen this link to choose your password, "
                          f"within {INVITATION_HOURS} hours (it works once):\n{url}\n"))
        return "; ".join(i18n.tr(lang, p) for p in problems)

    def pending_user(name: str):
        """None when name is a user of the users file with an invitation not
        used, a response otherwise."""
        if name in settings.fixed.users or name not in settings.web.users:
            return refuse(f"No user {name}", status.HTTP_404_NOT_FOUND)
        if invitation_of(name) is None:
            return refuse(f"{name} has no invitation waiting: it has its password")
        return None

    async def revoke_user(name: str):
        """The invitation of name revoked: the user, who never got in, goes,
        out of every workspace too. The caller holds access_lock."""
        web = web_copy()
        web.users.pop(name, None)
        web.roles.pop(name, None)
        refused = await store_access(web)
        if refused:
            return refused
        async with workspaces_lock:
            if any(name in w.members for w in settings.workspaces.values()):
                refused = await store_workspaces({
                    n: dataclasses.replace(w, members={m: r for m, r in w.members.items() if m != name})
                    for n, w in settings.workspaces.items()})
                if refused:
                    return refused
        return await drop_invitations(name)

    @app.post("/api/access/invitations/resend")
    async def invitation_resend(request: Request, payload: dict = Body(...), me: Principal = Depends(admin)):
        """A new link for a user still invited (the one of before no longer
        valid), for INVITATION_HOURS again."""
        name, email = str(payload.get("name") or ""), str(payload.get("email") or "").strip()
        async with access_lock:
            refused = pending_user(name)
            if refused:
                return refused
            answer = await invite(request, name, me, invitation_of(name).workspace, again=True)
        if not isinstance(answer, dict):
            return answer
        return {"ok": True, **answer, "mail_problem": await mail_invitation(request, email, name, answer["invite_url"])}

    @app.post("/api/access/invitations/revoke")
    async def invitation_revoke(request: Request, payload: dict = Body(...), me: Principal = Depends(admin)):
        """The link no longer valid, and the user, who never got in, deleted."""
        name = str(payload.get("name") or "")
        async with access_lock:
            refused = pending_user(name) or await revoke_user(name)
            if refused:
                return refused
        access_log.record("invitation", me.kind, me.name, request.client.host if request.client else "",
                          f"revoked for {name}: the user is deleted")
        return {"ok": True}

    @app.post("/api/workspaces/{workspace}/members/invitation/resend", dependencies=[Depends(admin)])
    async def member_invitation_resend(request: Request, workspace: str, payload: dict = Body(...),
                                       me: Principal = Depends(current_user)):
        """A workspace's admin sends again the invitation of a user of that
        workspace only."""
        name, email = str(payload.get("name") or ""), str(payload.get("email") or "").strip()
        a_workspace(workspace)
        async with access_lock:
            if not only_here(name, workspace):
                return refuse(f"{name} is not a user of {workspace} only", status.HTTP_404_NOT_FOUND)
            refused = pending_user(name)
            if refused:
                return refused
            answer = await invite(request, name, me, workspace, again=True)
        if not isinstance(answer, dict):
            return answer
        return {"ok": True, **answer, "mail_problem": await mail_invitation(request, email, name, answer["invite_url"])}

    @app.post("/api/workspaces/{workspace}/members/invitation/revoke", dependencies=[Depends(admin)])
    async def member_invitation_revoke(request: Request, workspace: str, payload: dict = Body(...),
                                       me: Principal = Depends(current_user)):
        """A workspace's admin revokes the invitation of a user of that
        workspace only: the user, who never got in, is deleted."""
        name = str(payload.get("name") or "")
        a_workspace(workspace)
        async with access_lock:
            if not only_here(name, workspace):
                return refuse(f"{name} is not a user of {workspace} only", status.HTTP_404_NOT_FOUND)
            refused = pending_user(name) or await revoke_user(name)
            if refused:
                return refused
        access_log.record("invitation", me.kind, me.name, request.client.host if request.client else "",
                          f"revoked for {name}: the user is deleted", workspace=workspace)
        return {"ok": True}

    @app.get("/api/access/workspaces", dependencies=[Depends(admin)])
    async def access_workspaces():
        """The workspaces of this machine, for the Users page of a machine
        linked to it (a user of a customer made from there)."""
        return {"workspaces": [{"name": w.name, "label": w.label}
                               for w in sorted(settings.workspaces.values(), key=lambda w: w.name)]}

    @app.get("/api/access/workspace-choices")
    async def workspace_choices(request: Request, me: Principal = Depends(admin)):
        """Where a user of a customer may be made: the workspaces of this
        machine and of each linked machine, asked in parallel; a machine
        that does not answer says why."""
        async def of_link(name: str, link: LinkTarget) -> dict:
            row = {"machine": name, "workspaces": [], "error": "", "ssh": link.ssh is not None}
            headers = {**link_headers(link, me.name, me.role), "Accept": "application/json"}
            try:
                resp = await link_clients.client(link).get("/api/access/workspaces", headers=headers,
                                                           timeout=LIST_TIMEOUT)
            except httpx.HTTPError as exc:
                return {**row, "error": f"it does not answer: {describe_error(exc)}"}
            if resp.status_code == 404:
                return {**row, "error": "its OmniaCLI is older: make the user on its own Users page"}
            if resp.status_code in (401, 403):
                return {**row, "error": "the link is not an admin there"}
            try:
                found = resp.json().get("workspaces") if resp.status_code == 200 else None
            except (ValueError, AttributeError):
                found = None
            if not isinstance(found, list):
                return {**row, "error": f"HTTP {resp.status_code}"}
            return {**row, "workspaces": [{"name": str(w["name"]), "label": str(w.get("label") or "")}
                                          for w in found if isinstance(w, dict) and w.get("name")]}

        here = {"machine": "", "error": "", "ssh": False, "workspaces": (await access_workspaces())["workspaces"]}
        there = await asyncio.gather(*(of_link(n, link) for n, link in sorted(settings.link_targets.items())))
        lang = request_language(request)
        return {"machines": [here, *({**m, "error": i18n.tr(lang, m["error"])} for m in there)]}

    @app.post("/api/access/workspace-users/save")
    async def workspace_user_save(request: Request, payload: dict = Body(...), me: Principal = Depends(admin)):
        """A user of a customer made from the Users page: in workspace, on
        this machine (machine "") or on a linked one through its link, with
        its role there; invited (invite: true) or with a password."""
        machine, workspace = str(payload.get("machine") or ""), str(payload.get("workspace") or "")
        email = str(payload.get("email") or "").strip()
        body = {"name": str(payload.get("name") or "").strip(), "role": str(payload.get("role") or ""), "new": True,
                "invite": bool(payload.get("invite")), "password": str(payload.get("password") or "")}
        if not WORKSPACE_NAME.fullmatch(workspace):
            return refuse("Choose the workspace of the user")
        if not machine:
            answer = await member_save(request, workspace, {**body, "email": email}, me)
            if isinstance(answer, dict):
                answer["page"] = f"/workspaces/{workspace}#users"
            return answer
        link = settings.link_targets.get(machine)
        if link is None:
            return refuse(f"No link {machine}", status.HTTP_404_NOT_FOUND)
        try:
            resp = await link_clients.client(link).post(
                f"/api/workspaces/{quote(workspace)}/members/save", json=body,
                headers={**link_headers(link, me.name, me.role), "Accept": "application/json"})
        except httpx.HTTPError as exc:
            return refuse(f"{machine} does not answer: {describe_error(exc)}", status.HTTP_502_BAD_GATEWAY)
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code != 200 or not isinstance(data, dict):
            message = (data.get("error") or data.get("detail")) if isinstance(data, dict) else ""
            return refuse(message or f"{machine} answered HTTP {resp.status_code}",
                          resp.status_code if 400 <= resp.status_code < 500 else status.HTTP_502_BAD_GATEWAY)
        data["page"] = f"/links/{machine}/workspaces/{workspace}#users"
        if data.get("invite_path"):
            # the person opens it on the other machine: at its customers' portal
            # when it has one (its own address, it said so), else at the address
            # this one knows it by
            if not (data.get("portal") and str(data.get("invite_url") or "").startswith("https://")):
                data["invite_url"] = link.url.rstrip("/") + str(data["invite_path"])
            data["through_ssh"] = link.ssh is not None and not data.get("portal")
            data["mail_problem"] = await mail_invitation(request, email, body["name"], data["invite_url"])
        return data

    def invite_page(request: Request, state: str, invitation: Invitation | None, error: str = "", code: int = 200):
        return _TEMPLATES.TemplateResponse(
            request, "invite.html",
            {"state": state, "name": invitation.name if invitation else "", "error": error,
             "min_password": _MIN_PASSWORD, "expires": moment(invitation.expires) if invitation else "",
             "used": moment(invitation.used) if invitation and invitation.used else ""},
            # the link's token goes to no other site (Referrer-Policy same-origin, security_headers); not
            # no-referrer: the browser would send the form with Origin null, refused as cross-site
            status_code=code, headers={"Cache-Control": "no-store"})

    async def invitation_page(request: Request):
        """/invite/<token>: the page of an invitation (GET), where the person
        sets its password (POST) and is logged in. A link used, expired,
        revoked or sent again says so."""
        token = request.path_params["token"]
        ip = request.client.host if request.client else ""
        found = invitation_by_token(token)
        state = found.state(time.time()) if found else "unknown"
        if found is not None and state == "pending" and found.name not in settings.web.users:
            state = "unknown"
        if request.method == "GET" or state != "pending":
            if request.method == "POST":
                access_log.record("failed", "user", found.name if found else "", ip, f"invitation: {state}")
            return invite_page(request, state, found, code=200 if state == "pending" else 410)
        if not _same_origin(request):
            return HTMLResponse("Cross-site form refused", status_code=403)
        form = parse_qs((await request.body()).decode("utf-8", "replace"))
        password, again = (form.get("password") or [""])[0], (form.get("again") or [""])[0]
        if len(password) < _MIN_PASSWORD:
            return invite_page(request, state, found, f"The password needs at least {_MIN_PASSWORD} characters", 400)
        if password != again:
            return invite_page(request, state, found, "The two passwords are not the same", 400)
        hashed = await asyncio.to_thread(hash_password, password)
        async with access_lock:
            # again: revoked or sent again while the password was hashed
            if invitation_by_token(token) is not found or found.used or found.name not in settings.web.users:
                return invite_page(request, "unknown", None, code=410)
            web = web_copy()
            web.users[found.name] = hashed
            refused = await store_access(web)
            if refused:
                return invite_page(request, state, found, "The password could not be saved: try again", 500)
            used = dataclasses.replace(found, used=time.time())
            await store_invitations([used if i is found else i for i in invitations])
        name = found.name
        access_log.record("invitation", "user", name, ip, f"accepted: {name} set its password",
                          role=settings.user_role(name), workspace=found.workspace)
        if needs_second_step(request, name):
            # its two-step verification first, set up now when it must
            return to_second_step(request, name, "/", "invitation")
        access_log.record("login", "user", name, ip, "invitation", role=settings.user_role(name))
        response = RedirectResponse("/", status.HTTP_303_SEE_OTHER)
        set_session_cookie(request, response, sessions.create(name, settings.users[name]))
        return response

    app.add_route("/invite/{token}", invitation_page, methods=["GET", "POST"])

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
    async def workspace_token_save(workspace: str, payload: dict = Body(...), me: Principal = Depends(current_user)):
        """An MCP token of the workspace (new: true; it is in the answer, once),
        or its role changed: it sees the workspace's instances only, with its
        role there; never a machine's token."""
        ws = a_workspace(workspace)
        name = str(payload.get("name") or "").strip()
        role = str(payload.get("role") or TOKEN_DEFAULT_ROLE)
        new = bool(payload.get("new"))
        async with access_lock:
            if me.scoped and not new and (name in settings.fixed.tokens or settings.token_workspace(name) != ws.name):
                # one that does not exist and another workspace's: the same answer
                return refuse(f"No token {name} of workspace {ws.name}", status.HTTP_404_NOT_FOUND)
            refused = (check_new_name(me, "Token", name, settings.fixed.tokens, settings.tokens, new)
                       or check_role(role))
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
            "machines": machine_cards(request_language(request)),
        })

    def machine_cards(lang: str = "en") -> list[dict]:
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
            # the reason the preparation gave when it stopped, not ssh's exit code (a job of an older OmniaCLI too)
            said = machines.failure_said(job.lines) or ""
            # what was chosen for it (a clean server, one that already has Odoo); of a job that did not
            # keep it, what its failure says: a system accepted only as a server that already has Odoo
            card["kind"] = (job.result or {}).get("kind") if isinstance(job.result, dict) else None
            if machines.ONLY_EXISTING in said:
                card["kind"] = "existing"
            card["job"] = {"id": job.id, "status": job.status, "created": job.created,
                           "error": i18n.tr(lang, machines.failure_said(job.lines) or job.error) if job.error else job.error,
                           "step": machines.progress(job.lines), "steps": len(machines.STEPS)}
        return sorted(cards.values(), key=lambda c: c["name"])

    @app.get("/api/machines", dependencies=[Depends(admin)])
    async def machines_json(request: Request):
        return {"machines": machine_cards(request_language(request))}

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
            # kept with the job: Try again opens the steps with the same choice, not with the default
            job.result["kind"] = machine.kind
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
        if not tunnel_reaches_browser(request):
            return JSONResponse(data)
        # an http link is the other machine's own (an instance no name opens from outside): only a tunnel carries it
        if given.scheme == "https" and await loginas.reaches_here(given.hostname):
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
            return refuse((f"The nginx of {given.hostname} does not pass {loginas.LOGIN_PATH} to OmniaCLI, "
                           if given.scheme == "https" else "No name of the instance opens from outside, ")
                          + f"and no tunnel opens: {exc}", status.HTTP_502_BAD_GATEWAY)
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
        headers[_MENU_HEADER] = ",".join(menu_places(me))
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
                  ("/enterprise", t("Odoo Enterprise"), t("Where the code of Odoo Enterprise comes from, and its copy of each version"), admin_here and not me.workspaces),
                  ("/options", t("Options"), t("Consoles, tunnels and the other options of this OmniaCLI"), admin_here),
                  ("/help", t("Help"), t("How OmniaCLI works, section by section"), True)),
        ]
        return _TEMPLATES.TemplateResponse(request, "settings.html", {"groups": [g for g in groups if g]})

    # --- options of the system ------------------------------------------

    @app.get("/options", dependencies=[Depends(admin)])
    async def options_page(request: Request, me: Principal = Depends(current_user)):
        return _TEMPLATES.TemplateResponse(request, "options.html", {
            "portal_url": portal_url(), "portal_refused": portal_refusal(me),
            "options": settings.options, "options_file": settings.options_file, "machine": settings.machine,
            "font_sizes": SHELL_FONT_SIZES, "idle_hours": TUNNEL_IDLE_HOURS,
            "retention_limits": AUDIT_RETENTION_LIMITS, "syslog_protocols": SYSLOG_PROTOCOLS})

    @app.post("/api/options/save", dependencies=[Depends(admin)])
    async def options_save(request: Request, payload: dict = Body(...), me: Principal = Depends(current_user)):
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
        if "audit" not in payload:
            # the access log's: not posted keeps the saved ones (a script of before)
            current = settings.options
            payload = {**payload, "audit": {"retention_days": current.audit_retention_days, "syslog": {
                "host": current.syslog_host, "port": current.syslog_port, "protocol": current.syslog_protocol,
                "ca_file": current.syslog_ca_file}}}
        current = settings.options
        portal = payload.get("portal")
        if not isinstance(portal, dict):
            # the name the customers read: not posted keeps the saved one
            portal = {"provider": current.provider_label}
        # the portal's address changes only when it is published (portal_publish):
        # its site is written with it
        payload = {**payload, "portal": {**portal, "address": current.portal_address,
                                         "staff": portal.get("staff", current.portal_staff),
                                         "second_factor": portal.get("second_factor", current.second_factor)}}
        try:
            options = parse_options(payload)
        except ConfigError as exc:
            return refuse(str(exc))
        try:
            await asyncio.to_thread(save_options, settings.options_file, options)
        except OSError as exc:
            return refuse(f"Cannot write {settings.options_file}: {exc}", 500)
        settings.options = options
        apply_audit_options()
        audit_status_cache.clear()
        if options.second_factor != current.second_factor:
            access_log.record("second-factor", me.kind, me.name, request.client.host if request.client else "",
                              f"required for: {current.second_factor} -> {options.second_factor} (Options)")
        return {"ok": True}

    # --- the customers' portal: its address, published by the root helper ----

    def portal_refusal(me: Principal) -> str:
        """Why me may not publish the portal here, "" when it may: the admins of
        the machine only, where OmniaCLI keeps nginx (its workspaces)."""
        if not machine_admin(me):
            return "Only the administrators of the machine publish the customers' portal"
        if not settings.storage_roots:
            return workspacehelper.NOT_OUR_NGINX
        return ""

    def portal_name(value) -> str:
        """A host name for the portal, or ConfigError with why not: not one of
        the workspaces' names, nor under their domains."""
        name = address_name("address", value)
        taken = address_taken(name, settings.workspaces, "", "", "")
        if taken:
            raise ConfigError(f"address: {name} is taken: {taken}")
        return name

    def portal_certificate_said(host: str, error: str) -> str:
        """Why Let's Encrypt gave no certificate, in one sentence: certbot's
        own lines (its log, its forum) are in the job's log."""
        if any(mark in error.lower() for mark in ("challenges have failed", "failed to authenticate",
                                                   "connection refused", "timeout during connect")):
            return (f"Let's Encrypt did not reach {host} on port 80: its DNS record leads elsewhere, "
                    "or this machine is not reachable from the internet")
        lines = [line for line in error.splitlines()
                 if line.strip() and not line.startswith(("Saving debug log", "Ask for help"))]
        return "; ".join(lines)[:300]

    @app.get("/api/portal", dependencies=[Depends(admin)])
    async def portal_get(me: Principal = Depends(current_user)):
        """The portal's address and whether it may be published here."""
        return {"address": settings.options.portal_address, "url": portal_url(),
                "staff": settings.options.portal_staff, "refused": portal_refusal(me)}

    @app.post("/api/portal/check", dependencies=[Depends(admin)])
    async def portal_check(request: Request, payload: dict = Body(...), me: Principal = Depends(current_user)):
        """The address as the Options page shows it before Publish: a host
        name, free (the workspaces, the other sites of nginx), and where its
        DNS points: here, elsewhere (with this machine's address), none."""
        lang = request_language(request)
        refused = portal_refusal(me)
        if refused:
            return refuse(refused)
        row = {"name": str(payload.get("host") or "").strip().lower().rstrip("."), "error": "", "taken": "",
               "dns": "", "addresses": []}
        try:
            row["name"] = portal_name(payload.get("host"))
        except ConfigError as exc:
            row["error"] = i18n.tr(lang, str(exc).removeprefix("address: "))
            return row
        try:
            found = await workspaceops.ask("portal-names")
            hit = workspaceweb.takers(row["name"], {str(k): str(v) for k, v in (found.get("elsewhere") or {}).items()})
            if hit:
                row["taken"] = i18n.tr(lang, f"used by {hit[0][1]}")
        except JobFailed as exc:
            row["nginx_error"] = i18n.tr(lang, str(exc).removeprefix("workspace-helper: "))
        here = await asyncio.to_thread(workspaceweb.machine_addresses, shutil.which("ip"))
        row["addresses"] = await asyncio.to_thread(workspaceweb.addresses_of, row["name"])
        row["dns"], row["here"] = workspaceweb.dns_state(row["addresses"], here), sorted(here)
        return row

    @app.post("/api/portal/publish", dependencies=[Depends(admin)])
    async def portal_publish(payload: dict = Body(...), me: Principal = Depends(current_user)):
        """The portal published on host (empty: taken away), as a job: the
        site, the certificate, then the address saved in the options."""
        refused = portal_refusal(me)
        if refused:
            return refuse(refused)
        given = str(payload.get("host") or "").strip()
        try:
            host = portal_name(given) if given else ""
        except ConfigError as exc:
            return refuse(str(exc).removeprefix("address: "))

        async def action(job):
            if host:
                job.log(f"--- the customers' portal at {host}")
            else:
                job.log("--- the customers' portal taken away" + (f" from {settings.options.portal_address}"
                                                                 if settings.options.portal_address else ""))
            made = await workspaceops._helper(job, "portal", host or "-")
            job.log(f"nginx: {made.get('nginx')}")
            options = dataclasses.replace(settings.options, portal_address=host)
            try:
                await asyncio.to_thread(save_options, settings.options_file, options)
            except OSError as exc:
                raise JobFailed(f"the site is written, but {settings.options_file} is not: {exc}") from exc
            settings.options = options
            job.result.update(made)
            if not host:
                return
            job.log(f"https://{host}")
            why = portal_certificate_said(host, str(made.get("certificate_error") or ""))
            if made.get("certificate_error"):
                job.log(f"Let's Encrypt: {made['certificate_error']}")
            if made.get("certificate") == "self-signed":
                job.warn(f"{host} has a certificate of this machine's own for now, which the browsers warn about: "
                         f"Let's Encrypt's comes once {host} points here and the portal is published again"
                         + (f" ({why})" if why else ""))
            elif not made.get("https"):
                job.warn(f"{host} has no certificate yet: {why or 'openssl did not make one'}")
        title = f"Customers' portal at {host}" if host else "Customers' portal taken away"
        return {"ok": True, "job": jobs.submit(title, [], f"manual: {me}", action).id}

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
               else public_base(request) + f"/hooks/deploy/{instance.server}/{instance.role}")
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

    def shown_name(me: Principal) -> str:
        """This machine's name in the lists me reads: "" for a user of
        workspaces, who is not told which machine its instances are on."""
        return "" if me.scoped else here_name()

    def listed_links(me: Principal) -> list[str]:
        """The links whose lists join this machine's: for a user who logs in
        here and sees the whole machine."""
        return sorted(settings.link_targets) if me.kind == "user" and visible_servers(me) is None else []

    LINK_LIST_OLDER = "its OmniaCLI is older: it has no such list yet"

    async def link_list(request: Request, me: Principal, name: str, path: str):
        """(what a link answered to GET path, "") or (None, why not), asked
        with me's name and role and in the page's language."""
        link = settings.link_targets.get(name) if name in listed_links(me) else None
        if link is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No link {name}")
        headers = {**link_headers(link, me.name, me.role), "Accept": "application/json",
                   "Accept-Language": request_language(request)}
        try:
            # in all: httpx's timeout is of each phase, and a link that answers a byte at a time never ends by it
            resp = await asyncio.wait_for(
                link_clients.client(link).get(path, headers=headers, timeout=LIST_TIMEOUT), LIST_TIMEOUT)
        except (httpx.HTTPError, asyncio.TimeoutError) as exc:
            return None, describe_error(exc)
        if resp.status_code == 404:
            return None, LINK_LIST_OLDER
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
            "here_name": shown_name(me), "links": links, "quick_instance": new_instance_here(me),
            # the new instance on a link's machine too (the dialog asks each one its versions)
            "ni_links": links if me.can("admin") else []})

    @app.get("/api/instances")
    async def instances_list(request: Request, link: str = "", me: Principal = Depends(current_user)):
        """The instances of this machine ({name, servers, links}: the rows of
        /api/servers, the links whose lists join it), or of one link
        ({name, link, servers} or {name, link, error})."""
        if not link:
            return {"name": shown_name(me), "link": "", "servers": await server_list(request), "links": listed_links(me)}
        found, why = await link_list(request, me, link, "/api/servers")
        if found is not None and not isinstance(found, list):
            found, why = None, "it did not answer a list"
        if found is None:
            return {"name": link, "link": link, "error": i18n.tr(request_language(request), why),
                    # it answers, it is only older than this list
                    "older": why == LINK_LIST_OLDER}
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
        return _TEMPLATES.TemplateResponse(request, "backups.html", {"here_name": shown_name(me), "links": listed_links(me)})

    @app.get("/api/backups")
    async def backups_list(request: Request, link: str = "", me: Principal = Depends(current_user)):
        """The backups of this machine ({name, rows, tonight, links}), or of
        one link ({name, link, rows, tonight} or {name, link, error})."""
        if not sees_backups(me):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "The developer role is required")
        if not link:
            return {"name": shown_name(me), "link": "", **backup_rows(request, me), "links": listed_links(me)}
        found, why = await link_list(request, me, link, "/api/backups")
        if found is not None and not (isinstance(found, dict) and isinstance(found.get("rows"), list)):
            found, why = None, "it did not answer a list"
        if found is None:
            return {"name": link, "link": link, "error": i18n.tr(request_language(request), why),
                    # it answers, it is only older than this list
                    "older": why == LINK_LIST_OLDER}
        return {"name": link, "link": link, "rows": found["rows"], "tonight": found.get("tonight") or {}}

    # --- the map of the infrastructure (docs/ux/UX-009) ----------------------
    # One route, read only: what the lists above already say (the instances,
    # their state, their backups) and where each thing is (the workspace, the
    # names, the database, where the backups go), as nodes and lines; and what
    # moves now (the jobs). Nothing here asks PostgreSQL, docker or the root
    # helper while a request waits but the run states, as /api/servers does.

    MAP_OLDER = "its OmniaCLI is older: update it to see it on the map"
    # the run states as the last whole read found them: what part=now says is stopped
    map_states: dict[str, str] = {}
    mail_counts = inframap.MailCounts()
    map_tasks: set[asyncio.Task] = set()
    # {link: its stagings of this machine's productions}, and when they were asked
    map_elsewhere: dict = {"at": None, "links": {}}
    # {link: (when it did not answer the whole map, why)}: not asked the layer of now meanwhile
    map_unanswered: dict[str, tuple[float, str]] = {}

    async def map_ask(link: LinkTarget, who: str, role: str, params: dict, lang: str = ""):
        """(what link answered to GET /api/map, "") or (None, why not), within
        LIST_TIMEOUT in all: httpx's timeout is of each phase, and a link
        that answers a byte at a time would hold the whole map. Asked once
        more when the connection kept open was closed there in the meantime."""
        headers = {**link_headers(link, who, role), "Accept": "application/json"}
        if lang:
            headers["Accept-Language"] = lang

        async def get():
            for last in (False, True):
                try:
                    return await link_clients.client(link).get("/api/map", params=params, headers=headers,
                                                               timeout=LIST_TIMEOUT)
                except (httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError, httpx.CloseError):
                    if last:
                        raise
        try:
            resp = await asyncio.wait_for(get(), LIST_TIMEOUT)
        except (httpx.HTTPError, asyncio.TimeoutError) as exc:
            return None, describe_error(exc)
        if resp.status_code == 404:
            return None, MAP_OLDER
        if resp.status_code != 200:
            return None, f"answered HTTP {resp.status_code}"
        try:
            return resp.json(), ""
        except ValueError:
            return None, "it did not answer a map"

    def map_task(work) -> None:
        task = asyncio.get_running_loop().create_task(work)
        map_tasks.add(task)
        task.add_done_callback(map_tasks.discard)

    async def stop_map() -> None:
        """What the map was reading by itself (a count of caught mail, the links' stagings) is left."""
        for task in list(map_tasks):
            task.cancel()
        await asyncio.gather(*map_tasks, return_exceptions=True)

    async def count_mail(server: str, instance: Instance) -> None:
        """The caught mail of a staging counted (the root helper's mail-list),
        kept for the map: never asked by a request that waits."""
        try:
            found = await stagingmail.messages(instance)
        except (JobFailed, LookupError) as exc:
            _logger.info("the caught mail of %s was not counted: %s", server, exc)
            mail_counts.store(server, None, None)
            return
        rows = found.get("messages") or []
        mail_counts.store(server, int(found.get("total") or 0), rows[0].get("received") if rows else None)

    def mail_guarded(instance: Instance) -> bool | None:
        """Whether the firewall refuses the mail ports to the staging's
        project, from the root's record (read only); None: not readable."""
        try:
            data = json.loads(workspacehelper.MAIL_GUARD.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return isinstance(data, dict) and str(Path(str(instance.restart.service))) in data

    def map_viewer(me: Principal) -> dict:
        """Who reads the map: the whole machine or its workspaces, and with which role at most."""
        if not me.scoped:
            return {"scope": "machine", "role": me.role}
        roles = [r for _, r in me.workspaces if r in ACCESS_ROLES] or ["user"]
        return {"scope": "workspace", "role": max(roles, key=ACCESS_ROLES.index)}

    def map_workspace(me: Principal, workspace: str) -> Workspace | None:
        """The workspace the map is asked of; one that is not the caller's
        is answered as one that does not exist."""
        if not workspace:
            return None
        ws = settings.workspaces.get(workspace)
        if ws is None or (me.scoped and me.workspace_role(workspace) is None):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such workspace")
        return ws

    def map_label(srv: Server) -> str:
        """An instance as the lists name it: its address, else its server's name."""
        main = srv.production or srv.test
        return inframap.public_host((main.domain or main.site) if main is not None else None) or srv.name

    def map_here(request: Request, me: Principal, part: str, only: Workspace | None, focus: str,
                 keep: set[str] | None = None) -> dict:
        """This machine's part of the map, for me: its workspaces (only: that
        one), their instances (keep: those servers only), what is to look
        at, the jobs; with focus (a server), the path of that instance. The
        run states are map_states'."""
        lang = request_language(request)
        say = functools.partial(i18n.gettext, lang)
        shown = visible_servers(me)
        whole = shown is None
        view = customer_view(request)
        known = stagings.load(stagings_file)
        live = [srv for srv in settings.active_servers() if srv.instances and (whole or srv.name in shown)
                and (only is None or srv.name in only.servers) and (keep is None or srv.name in keep)]
        names_shown = {srv.name for srv in live}
        running = {key: j.id for j in jobs.jobs.values() if j.status == "running" for key in j.instances}
        queued = {key for j in jobs.jobs.values() if j.status == "queued" for key in j.instances}
        generated: dict[str, dict] = {}
        day_ago = (datetime.datetime.now() - TONIGHT).isoformat(timespec="seconds")
        tonight = {"ok": 0, "failed": 0, "unscheduled": 0}
        found_dest = load_destinations() if part == "all" else {}
        cluster_major = next(iter(machinedb.registry()), None) if part == "all" else None
        # the names of machines a customer does not read: this one's, and those of its stagings' productions
        hidden = {here_name(), *(str(i.get("machine")) for i in known.values() if i.get("machine"))} if view else set()
        own = ({n for ws, _ in me.workspaces if ws in settings.workspaces for n in (ws, *settings.workspaces[ws].servers)}
               | {m for ws, _ in me.workspaces if ws in settings.workspaces for m in settings.workspaces[ws].members}
               ) if view else set()
        hidden_re = [re.compile(rf"(?<![\w.-]){re.escape(n)}(?![\w-])") for n in sorted(hidden - own, key=len, reverse=True)]

        def scrub(text):
            """A job's text as me reads it: a customer's without the machines' names."""
            if view is None or not isinstance(text, str):
                return text
            text = view.text(text)
            for pattern in hidden_re:
                text = pattern.sub(say("the server"), text)
            return text

        def env_names(srv: Server, role: str, inst: Instance) -> list[dict]:
            owner = workspace_of(srv.name)
            if owner is not None and owner[0].domain:
                ws, instance = owner
                if ws.name not in generated:
                    generated[ws.name] = generated_names(ws)
                own_names = [n for n, at in generated[ws.name].items() if at == (instance, role)]
                return inframap.env_names(own_names, ws.aliases.get(instance, {}).get(role, []),
                                          ws.redirects.get(instance, {}).get(role, {}), web_names_state.of(ws.name))
            host = urlsplit(inst.domain).netloc if inst.domain else ""
            return inframap.env_names([host], [], {}, {}) if host else []

        def env_of(srv: Server, role: str) -> dict | None:
            inst = srv.instances.get(role)
            if inst is None:
                return None
            return {"state": map_states.get(inst.key, "unknown"), "busy": jobs.is_busy(inst.key),
                    "job": running.get(inst.key), "queued": inst.key in queued and inst.key not in running,
                    "names": env_names(srv, role, inst)}

        def backup_of(srv: Server) -> dict | None:
            """The automatic backups of a production, for who may see its backups (developer)."""
            prod = srv.production
            if prod is None or srv.name in known or not prod.plugin.has_backup \
                    or not reaches(role_on(me, srv.name), "developer"):
                return None
            sch = scheduler.schedules.get(backup_schedule_name(prod))
            state = backup_schedule_state(prod, lang)
            at, night = None, {"ok": 0, "failed": 0}
            if sch is not None:
                minute, hour, *rest = sch.cron.split(" ")
                if minute.isdigit() and hour.isdigit() and rest == ["*", "*", "*"]:
                    at = f"{int(hour):02d}:{int(minute):02d}"
                for run in scheduler.history.runs(sch.name, jobs):
                    if str(run.get("time") or "") >= day_ago and run.get("status") in (SUCCESS, *BACKUP_NOT_DONE):
                        night["ok" if run.get("status") == SUCCESS else "failed"] += 1
            tonight["ok"] += night["ok"]
            tonight["failed"] += night["failed"]
            if not state["scheduled"]:
                tonight["unscheduled"] += 1
            name = sch.backup.destination if sch is not None and sch.backup and state["scheduled"] else ""
            last = state["last"]
            return {"scheduled": state["scheduled"], "time": at if state["scheduled"] else None,
                    "destination": name or None, "destination_id": f"{inframap.HERE}/dest:{name}" if name else None,
                    # its runs of the last day: what the counts of the head light
                    "tonight": night,
                    "last": last and {"time": last.get("time"), "ok": bool(last.get("ok")), "job": last.get("job")}}

        def light(srv: Server) -> dict:
            """What is in memory of an instance: its states, names, monitor, backup, certificate."""
            prod = srv.production or srv.test
            seen = monitor.view(prod.key)
            numbers = reaches(role_on(me, srv.name), "developer")
            site = (prod.site or "").removeprefix("https://")
            return {"id": inframap.instance_id(srv.name), "server": srv.name, "name": map_label(srv),
                    "type": "staging" if srv.name in known else "production",
                    "envs": {"production": env_of(srv, "production"), "test": env_of(srv, "test")},
                    "backup": backup_of(srv),
                    # the numbers of the monitor: from the developer up
                    "monitor": {"state": seen["state"], "since": seen["since"],
                                "last_ms": seen["last_ms"] if numbers else None,
                                "uptime_24h": seen["uptime_24h"] if numbers else None},
                    "certificate": monitor.certificate(site) if site else None}

        followed = set(machinedb.dedicated_sources())

        def dedicated_copy(inst: Instance) -> dict | None:
            key = dedicated_key(inst) if followed else ""
            brief = replica_brief(key) if key in followed else None
            if brief is None or brief["code"] == "replaced":
                return None
            return {**brief, "machine": brief["machine"] if machine_admin(me) else None}

        def full(srv: Server) -> dict:
            node = light(srv)
            info = known.get(srv.name) or {}
            parent, away = str(info.get("parent") or ""), str(info.get("machine") or "")
            prod = srv.production or srv.test
            for role, env in node["envs"].items():
                if env is not None:
                    place = database_place(srv.instances[role]) or {}
                    if view is not None and place.get("provider"):
                        # the promoted replica of the machine's cluster: the provider's machine, not named to a customer
                        place = {**place, "host": say("another machine"), "link": None}
                    env["database"] = {"where": place.get("where"), "host": place.get("host"),
                                       # reads also from a replica of the machine's cluster (UX-010): no machine, no address
                                       "reads": bool(place.get("reads")),
                                       "replica": place.get("replica") or None, "port": place.get("port"),
                                       # the read replica of its own database on another machine (UX-011 §D): its
                                       # machine for the machine's admins, that a copy exists for its workspace's users
                                       "copy": dedicated_copy(srv.instances[role]),
                                       "major": cluster_major if place.get("where") == "machine" else None,
                                       # on the cluster of a linked OmniaCLI: named for who sees the machines
                                       "label": say("PostgreSQL {major} of {link}").replace(
                                           "{major}", str(place.get("major"))).replace("{link}", str(place["link"]))
                                       if whole and place.get("link") else None,
                                       } if place else None
            node.update({
                # its production: here (its id), or on another machine (elsewhere)
                "parent": inframap.instance_id(parent) if parent and not away and parent in names_shown else None,
                "elsewhere": bool(parent and away),
                "version": odoo_version(srv.name), "created": info.get("refreshed") or info.get("created") or "",
                # an address the reader may open: not localhost's port of an instance without a name
                "url": inframap.public_url(prod.web_url), "page": f"/servers/{srv.name}", "mail": None})
            if whole and parent and away:
                # for the machine that holds its production: the map ties them (inframap.link_stagings)
                node.update({"parent_server": parent, "parent_machine": away})
            if srv.name in known:
                caught, meta = staging_mail_meta(srv.name)
                if caught is not None:
                    if map_states.get(caught.key) == "running" and mail_counts.due(srv.name):
                        map_task(count_mail(srv.name, caught))
                    node["mail"] = {"capture": True, **mail_counts.of(srv.name),
                                    "inbox": workspacehelper.receives_mail(meta, "production")}
            return node

        make = full if part == "all" else light
        mine = None if whole else {ws for ws, _ in me.workspaces}
        workspaces_, in_workspace = [], set()
        for ws in sorted(settings.workspaces.values(), key=lambda w: w.name):
            if (mine is not None and ws.name not in mine) or (only is not None and ws.name != only.name):
                continue
            role = me.role if whole else me.workspace_role(ws.name)
            can_open = reaches(role, "admin")
            point = ((disk_history.workspaces.get(ws.name) or {}).get("points") or [None])[-1]
            in_workspace.update(ws.servers)
            workspaces_.append({
                "id": inframap.workspace_id(ws.name), "name": ws.name, "label": ws.label or ws.name,
                "domain": ws.domain, "certificate": ws.certificate if ws.domain else "",
                "can_open": can_open, "page": f"/workspaces/{ws.name}",
                # as the daily record of the disks found it (no question to the root helper)
                "disk": {"used": point[1], "size": point[3], "day": point[0]} if point else None,
                "limits": dict(ws.limits) if can_open else None, "members": len(ws.members) if can_open else None,
                "instances": [make(settings.servers[n]) for n in ws.servers if n in names_shown]})
        loose = [make(srv) for srv in live if srv.name not in in_workspace] if whole and only is None else []
        instances = [i for ws in workspaces_ for i in ws["instances"]] + loose

        # --- the jobs: those open, those just ended, the last one; a day of those that went wrong
        now = datetime.datetime.now()
        found_jobs = visible_jobs(me, list(reversed(jobs.jobs.values())))
        if only is not None:
            found_jobs = [j for j in found_jobs if j.workspace == only.name or set(j.servers) & set(only.servers)]
        ended = sorted((j for j in found_jobs if j.finished_flag), key=lambda j: j.finished or "", reverse=True)
        just = (now - datetime.timedelta(seconds=inframap.JOB_SHOWN)).isoformat(timespec="seconds")
        strip = [j for j in found_jobs if not j.finished_flag] + [j for j in ended if (j.finished or "") >= just]
        if ended and ended[0] not in strip:
            strip.append(ended[0])

        def machine_of(name) -> str | None:
            if name == "":
                return inframap.HERE
            return name if name in settings.link_targets else None

        def job_of(job) -> dict:
            read = view.job(job, False) if view is not None else job
            progress = read.result.get("progress") if isinstance(read.result, dict) else None
            steps = None
            if isinstance(progress, dict) and isinstance(progress.get("steps"), list):
                steps = {"steps": [{"id": s.get("id"), "label": scrub(i18n.tr(lang, str(s.get("label") or ""))),
                                    # a customer is not told which machine: the server
                                    "where": i18n.tr(lang, str(s.get("where") or "")) if whole else say("the server")}
                                   for s in progress["steps"] if isinstance(s, dict)],
                         "step": progress.get("step"), "done": progress.get("done") or 0,
                         "total": progress.get("total") or 0}
            transfer = job.result.get("transfer") if whole and isinstance(job.result, dict) else None
            transfer = transfer if isinstance(transfer, dict) else {}
            # the job of the linked machine this one waits for (a staging made there): one row for the two
            there = job.result.get("follows") if whole and isinstance(job.result, dict) else None
            return {"id": job.id, "title": scrub(i18n.tr(lang, read.title)), "status": job.status,
                    "outcome": job.outcome, "by": scrub(read.source), "created": job.created,
                    "started": job.started, "finished": job.finished,
                    # by this machine's clock: the browser's may be of another time zone
                    "age_s": inframap.seconds_since(job.started or job.created, now),
                    "ended_s": inframap.seconds_since(job.finished, now),
                    "follows": there if isinstance(there, str) else None,
                    "error": scrub(i18n.tr(lang, read.error)) if read.error else None,
                    "instances": [inframap.instance_id(s) for s in job.servers],
                    "from": machine_of(transfer.get("from")) if transfer else None,
                    "to": machine_of(transfer.get("to")) if transfer else None,
                    "progress": steps, "page": f"/jobs/{job.id}"}

        # --- what is to look at
        watch = [w for i in instances for w in inframap.watch_of(i, say)]
        watch = inframap.names_by_workspace(watch, workspaces_, say)
        backups_failed = {w.get("job") for w in watch if w["kind"] == "backup_failed"}
        since = (now - datetime.timedelta(hours=inframap.WATCH_HOURS)).isoformat(timespec="seconds")
        # the newest first: an action done again on the same instances says how it went now (the one of before is
        # history), and the jobs of an instance that is no longer here (removed: its removal ends its story) are not
        # to look at any more
        redone = set()
        for job in ended:
            again = (job.title, tuple(job.instances))
            earlier = again in redone
            redone.add(again)
            if (job.finished or "") < since or job.outcome == SUCCESS or job.id in backups_failed or earlier:
                continue
            server = job.servers[0] if job.servers else ""
            if server and server != MACHINE_KEY and server not in settings.servers:
                continue
            watch.append({"kind": "job_warned" if job.outcome == "warned" else "job_failed",
                          "node": inframap.instance_id(job.servers[0]) if job.servers else None,
                          "text": scrub(i18n.tr(lang, (view.job(job, False) if view is not None else job).title)),
                          "since": job.finished, "job": job.id, "page": f"/jobs/{job.id}"})
        if whole and machine_admin(me) and only is None:
            watch += [{"kind": "disk", "node": inframap.HERE, "text": i18n.tr(lang, problem), "since": None}
                      for what, problem in monitor.problems().items() if what.startswith("disk ")]
            # the read replica of this machine's cluster, not connected or holding 1 GB or more of its disk
            # (UX-010 §9), as the look saw it last; and a replica hosted here that its primary does not see
            watch += [{"kind": "replica", "node": inframap.HERE, "text": i18n.tr(lang, text), "since": since,
                       "page": "/#database"} for text, since in replica_watch()]
        point = sampler.machine[-1] if whole and sampler.machine else None
        load = None
        if point is not None:
            ram = round(100 * point["ram"] / point["ram_total"]) if point.get("ram") and point.get("ram_total") else None
            load = {"cpu": point.get("cpu"), "ram": ram}
        result = {"at": inframap.now_text(), "part": part, "viewer": map_viewer(me),
                  "verdict": {"ok": not watch, "watch": watch},
                  "tonight": tonight if sees_backups(me) else None,
                  "jobs": [job_of(j) for j in strip]}
        if part == "now":
            result["machines"] = [{"id": inframap.HERE, "answered": True, "error": None, "older": False, "load": load,
                                   "envs": {i["id"]: {role: {k: env[k] for k in ("busy", "job", "queued")}
                                                      for role, env in i["envs"].items() if env}
                                            for i in instances}}]
            return result

        # --- the whole: the machine, the databases, where the backups go, the lines
        databases, edges, used = {}, [], set()
        for node in instances:
            targets: dict[str, int] = {}
            for env in node["envs"].values():
                place = (env or {}).get("database") or {}
                ident = inframap.database_id(place)
                if ident is None:
                    continue
                targets[ident] = targets.get(ident, 0) + 1
                databases[ident] = {"id": ident, "kind": place["where"], "host": place.get("host"),
                                    "major": place.get("major"), "machine": inframap.HERE,
                                    "label": place.get("label")}
                if place.get("replica"):
                    replica = f"{inframap.HERE}/replica:{place['replica']}"
                    databases[replica] = {"id": replica, "kind": "replica", "host": place["replica"], "of": ident,
                                          "major": None, "machine": inframap.HERE}
                    targets[replica] = targets.get(replica, 0) + 1
            edges += [{"from": node["id"], "to": ident, "kind": "replica" if "/replica:" in ident else "database",
                       "count": count} for ident, count in targets.items()]
            backup = node.get("backup") or {}
            if backup.get("destination_id"):
                used.add(backup["destination"])
                edges.append({"from": node["id"], "to": backup["destination_id"], "kind": "backup", "count": 1,
                              "time": backup.get("time")})
        try:
            usage = shutil.disk_usage(next(iter(settings.storage_roots.values()), "/")) if whole else None
        except OSError:
            usage = None
        machine = {
            "id": inframap.HERE, "here": True, "name": None, "machine_name": here_name() if whole else None,
            "answered": True, "error": None, "older": False, "read_at": result["at"], "version": _version(),
            "load": load, "disk": {"free": usage.free, "total": usage.total} if usage else None,
            # the machine's cluster, when one was made (root's record: not asked whether it runs)
            "cluster": [{"major": major, "running": None,
                         # its read replica on another machine, for the machine's admins: where, and how it is
                         "replica": replica_brief(major) if machine_admin(me) else None}
                        for major in sorted(machinedb.registry())] if whole else [],
            # the read replicas of other machines' clusters hosted here (UX-010 §9): a node inside this machine
            "replicas": replica_briefs() if whole and machine_admin(me) else [],
            "drafts": len([s for s in settings.servers.values() if s.status == "draft"]) if machine_admin(me) else None,
            "workspaces": workspaces_, "loose": loose}
        result.update({
            "machines": [machine], "edges": edges, "databases": list(databases.values()),
            "destinations": [{"id": f"{inframap.HERE}/dest:{name}", "label": name,
                              "kind": found_dest[name].label if name in found_dest else "",
                              "workspace": found_dest[name].workspace if name in found_dest else "",
                              "machine": inframap.HERE} for name in sorted(used)]})
        srv = settings.servers.get(focus) if focus else None
        if srv is not None and srv.active and srv.name in names_shown:
            node = next(i for i in instances if i["server"] == srv.name)
            prod = srv.production or srv.test
            series = sampler.series(prod.key, int(time.time()) - 600)
            mail = node.get("mail")
            if mail:
                mail = {**mail, "guarded": mail_guarded(prod),
                        "domain": stagingmail.domain_of(srv.name) if mail.get("inbox") else ""}
            env = node["envs"].get(prod.role) or {}
            result["flow"] = {
                "node": node["id"], "names": env.get("names") or [],
                # the machine by its name for who sees the machine; the server for the others
                "web": {"label": here_name() if whole else say("the server")},
                "odoo": {"state": env.get("state"), "test": (node["envs"].get("test") or {}).get("state")
                         if prod.role == "production" else None, "monitor": node["monitor"],
                         "load": [{"t": p["t"], "cpu": p.get("cpu"), "mem": p.get("mem")} for p in series]},
                "database": env.get("database"),
                # no reading of the workspaces' firewall without changing it yet (UX-009 D4)
                "firewall": None, "mail": mail, "backup": node.get("backup"),
                "stagings": [{"id": i["id"], "name": i["name"], "created": i.get("created"),
                              "state": (i["envs"].get("production") or {}).get("state")}
                             for i in instances if i.get("parent") == node["id"]],
                "links": {"page": f"/servers/{srv.name}", "odoo": node["url"],
                          "mail": f"/servers/{srv.name}#mail" if mail else None}}
        return result

    async def read_elsewhere() -> None:
        """The stagings of this machine's productions made on the linked
        machines, asked to each link's map (stagings_of: those only) as the
        map itself and as a user, the least a link may be: kept for the
        users of workspaces, who do not reach the links and whose names are
        never sent to them. A link that does not answer keeps what it said last."""
        here = here_name()

        async def ask(link: LinkTarget):
            data, _ = await map_ask(link, inframap.SYSTEM_ACTOR, "user", {"stagings_of": here})
            machines_ = data.get("machines") if isinstance(data, dict) else None
            if not isinstance(machines_, list) or not machines_ or not isinstance(machines_[0], dict):
                return link.name, None
            rows = [i for ws in machines_[0].get("workspaces") or [] if isinstance(ws, dict)
                    for i in ws.get("instances") or [] if isinstance(i, dict)]
            # an OmniaCLI there that does not know stagings_of answers its whole map: the same rows are kept
            return link.name, [i for i in rows if i.get("parent_machine") == here and i.get("parent_server")]

        for name, rows in await asyncio.gather(*(ask(link) for link in settings.link_targets.values())):
            if rows is not None:
                map_elsewhere["links"][name] = rows
        for name in set(map_elsewhere["links"]) - set(settings.link_targets):
            del map_elsewhere["links"][name]
        map_elsewhere["at"] = time.monotonic()

    async def stagings_on_links(request: Request, me: Principal, data: dict) -> None:
        """For a user of workspaces: the stagings of its productions that
        are on a linked machine, under their production, with no name of
        that machine, of its workspaces or of its servers; numbered within
        their production, the oldest first, so another customer's staging
        never moves them."""
        if not settings.link_targets:
            return
        if map_elsewhere["at"] is None:
            await read_elsewhere()
        elif time.monotonic() - map_elsewhere["at"] >= inframap.ELSEWHERE_EVERY:
            map_elsewhere["at"] = time.monotonic()
            map_task(read_elsewhere())
        lang = request_language(request)
        found = sorted((r for rows in map_elsewhere["links"].values() for r in rows),
                       key=lambda r: (str(r.get("created") or ""), str(r.get("server") or "")))
        for ws in data["machines"][0]["workspaces"]:
            mine = {i["server"] for i in ws["instances"]}
            numbers: dict[str, int] = {}
            for row in found:
                parent = row["parent_server"]
                if parent not in mine:
                    continue
                numbers[parent] = numbers.get(parent, 0) + 1
                created = str(row.get("created") or "")
                try:
                    day = datetime.datetime.fromisoformat(created).strftime("%d/%m")
                except ValueError:
                    day = ""
                # its address when it has a name; a port of that machine is no address for the reader
                url = inframap.public_url(row.get("url"))
                word = i18n.gettext(lang, "Staging of {date}", date=day) if day else i18n.gettext(lang, "Staging")
                env = (row.get("envs") or {}).get("production") or {}
                ws["instances"].append({
                    "id": f"elsewhere/{parent}/{numbers[parent]}", "server": None,
                    "name": inframap.public_host(url) or word, "type": "staging",
                    "parent": inframap.instance_id(parent), "elsewhere": True,
                    "version": row.get("version") or "", "created": created, "url": url, "page": None,
                    "envs": {"production": {"state": env.get("state") or "unknown", "busy": bool(env.get("busy")),
                                            "job": None, "queued": bool(env.get("queued")), "names": [],
                                            "database": None}, "test": None},
                    "backup": None, "monitor": None, "certificate": None,
                    "mail": row.get("mail") and {k: row["mail"].get(k)
                                                 for k in ("capture", "count", "last", "counted_at", "inbox")}})

    @app.get("/api/map")
    async def infrastructure_map(request: Request, part: str = "all", machine: str = "", workspace: str = "",
                                 focus: str = "", stagings_of: str = "", me: Principal = Depends(current_user)):
        """The map of the infrastructure (docs/ux/UX-009): the machines (this
        one and, for who sees the whole machine and logs in here, each
        link's, asked to its own /api/map with the caller's role at most),
        their workspaces and instances with their state, names, database,
        backups, monitor and caught mail, the lines between them, the jobs
        and what is to look at. part=now: the jobs, what is busy, the load
        and the verdict only, from memory. machine: one machine (~: this
        one); workspace: one workspace; focus (<machine>/<server>): the
        path of that instance too. A user of workspaces gets its workspaces
        only, with no name of a machine, a link or another workspace; a link
        that does not answer or is older is a machine saying so, within
        LIST_TIMEOUT in all. stagings_of (the name of a machine, for who
        sees the whole of this one): the stagings made here of that
        machine's productions and nothing else, which is what that machine
        asks for its users of workspaces."""
        if part not in ("all", "now"):
            return refuse("part must be all or now")
        if stagings_of and not me.scoped:
            theirs = {name for name, info in stagings.load(stagings_file).items()
                      if info.get("machine") == stagings_of and info.get("parent")}
            map_states.update(await run_states([srv for srv in settings.active_servers() if srv.name in theirs]))
            found = map_here(request, me, "all", None, "", theirs)
            return {"at": found["at"], "part": "all", "viewer": found["viewer"],
                    "machines": [{"id": inframap.HERE, "here": True, "answered": True, "loose": [],
                                  "workspaces": [{"id": ws["id"], "instances": ws["instances"]}
                                                 for ws in found["machines"][0]["workspaces"] if ws["instances"]]}]}
        lang = request_language(request)
        links = listed_links(me)
        if machine and machine != inframap.HERE and machine not in links:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such machine")
        here_asked = machine in ("", inframap.HERE)
        only = map_workspace(me, workspace) if here_asked else None
        focus_machine, _, focus_server = focus.rpartition("/")
        focus_machine = focus_machine or machine or inframap.HERE
        # one workspace of this machine: its links are not in the schema
        asked = [n for n in links if machine == n or (not machine and only is None)]

        async def ask(name: str):
            link = settings.link_targets[name]
            unanswered = map_unanswered.get(name)
            if part == "now" and unanswered and time.monotonic() - unanswered[0] < inframap.UNANSWERED_FOR:
                # it did not answer the whole map: the next whole read asks it again, this one does not wait for it
                return name, None, unanswered[1]
            params = {"part": part}
            if focus_server and focus_machine == name:
                params["focus"] = f"{inframap.HERE}/{focus_server}"
            found, why = await map_ask(link, me.name, me.role, params, lang)
            if found is not None:
                found = inframap.rebase(found, name)
                why = "" if found is not None else "it did not answer a map"
            if found is None:
                map_unanswered[name] = (time.monotonic(), why)
            else:
                map_unanswered.pop(name, None)
            return name, found, why

        # the links are asked while this machine reads its own states
        asking = [asyncio.ensure_future(ask(n)) for n in asked]
        data = None
        try:
            if here_asked:
                shown = visible_servers(me)
                if part == "all":
                    map_states.update(await run_states(
                        [srv for srv in settings.active_servers() if (shown is None or srv.name in shown)
                         and (only is None or srv.name in only.servers)]))
                    # what was removed since is forgotten: its states, its count of caught mail
                    current = {inst.key for srv in settings.active_servers() for inst in srv.instances.values()}
                    for key in set(map_states) - current:
                        del map_states[key]
                    mail_counts.forget(stagings.load(stagings_file))
                data = map_here(request, me, part, only, focus_server if focus_machine == inframap.HERE else "")
                if me.scoped and part == "all":
                    await stagings_on_links(request, me, data)
            answers = await asyncio.gather(*asking)
        finally:
            for task in asking:
                task.cancel()
        if data is None:
            data = {"at": inframap.now_text(), "part": part, "viewer": map_viewer(me),
                    "verdict": {"ok": True, "watch": []}, "tonight": None, "jobs": [], "machines": []}
            if part == "all":
                data.update({"edges": [], "databases": [], "destinations": []})
        tonights = [data["tonight"]]
        for name, found, why in answers:
            if found is None:
                older = why == MAP_OLDER
                text = (i18n.gettext(lang, "{name}: its OmniaCLI is older: update it to see it on the map", name=name)
                        if older else i18n.gettext(lang, "{name} does not answer: {why}. Its instances are not here.",
                                                   name=name, why=i18n.tr(lang, why)))
                data["machines"].append({
                    "id": name, "here": False, "name": name, "machine_name": None, "answered": False,
                    "error": i18n.tr(lang, why), "older": older, "read_at": data["at"], "version": "", "load": None,
                    "disk": None, "cluster": [], "drafts": None, "workspaces": [], "loose": [], "envs": {}})
                # an older one answers: it is to update, not a machine that is down
                data["verdict"]["watch"].append({"kind": "machine_older" if older else "machine_unread", "node": name,
                                                 "text": text, "since": None})
                continue
            node = found["machine"]
            if workspace and machine == name:
                # one workspace of that machine: one that is not there is answered as here
                node["workspaces"] = [ws for ws in node.get("workspaces") or [] if ws.get("name") == workspace]
                if not node["workspaces"]:
                    raise HTTPException(status.HTTP_404_NOT_FOUND, "No such workspace")
                node["loose"] = []
                kept = {i["id"] for ws in node["workspaces"] for i in ws.get("instances") or []}
                found["watch"] = [w for w in found["watch"] if w.get("node") in kept]
                found["jobs"] = [j for j in found["jobs"] if set(j.get("instances") or []) & kept]
                found["edges"] = [e for e in found["edges"] if e["from"] in kept]
            data["machines"].append(node)
            data["jobs"] += found["jobs"]
            data["verdict"]["watch"] += found["watch"]
            tonights.append(found["tonight"])
            if part == "all":
                data["edges"] += found["edges"]
                data["databases"] += found["databases"]
                data["destinations"] += found["destinations"]
                if found["flow"] is not None:
                    data["flow"] = found["flow"]
        if asked:
            data["tonight"] = inframap.tonight_sum(tonights) if sees_backups(me) else None
            data["verdict"]["ok"] = not data["verdict"]["watch"]
            data["jobs"].sort(key=lambda j: str(j.get("created") or ""), reverse=True)
            if part == "all":
                if here_asked:
                    data["edges"] += [{"from": inframap.HERE, "to": name, "kind": "link", "count": 1}
                                      for name in asked]
                data["edges"] += inframap.link_stagings(data["machines"])
        if part == "all":
            used = {e["to"] for e in data["edges"]} | {e["from"] for e in data["edges"]}
            data["databases"] = [d for d in data["databases"] if d["id"] in used]
        return data

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

    # --- a database from outside into a new instance (MCP upload_backup and
    # restore_new_instance): the file put with an HTTP PUT into a workspace's
    # import area, here or sent on to a linked machine; the modules its
    # database has installed compared with the code the new instance gets;
    # the instance made from the template of its version, loaded, neutralized

    uploads = Uploads()
    # Odoo's own modules of each template's image, read from its instances here
    core_memo: dict[str, set[str]] = {}

    def imports_folder(ws_name: str) -> Path:
        """Where the databases sent to a workspace wait (700, 600 each)."""
        return settings.backup_dir / ".imports" / ws_name

    def outgoing_folder() -> Path:
        """Where a database put here for a linked machine waits to be sent on."""
        return settings.backup_dir / ".imports" / ".outgoing"

    def free_name(folder: Path, name: str) -> Path:
        """folder/name, or name with -2, -3... before its suffix when it is taken."""
        stem, suffix = name.rsplit(".", 1)
        dest, number = folder / name, 2
        while dest.exists():
            dest, number = folder / f"{stem}-{number}.{suffix}", number + 1
        return dest

    def database_problem(path: Path, name: str) -> str | None:
        """Why the file received is not a database to restore: a zip with a
        dump.sql (named .zip), or a plain SQL dump of pg_dump (named .sql)."""
        if name.endswith(".zip"):
            return backup_problem_of(path, name)
        return sqldump.problem(path, name)

    def backup_problem_of(path: Path, name: str) -> str | None:
        try:
            with zipfile.ZipFile(path) as zf:
                found = "dump.sql" in zf.namelist()
        except (OSError, zipfile.BadZipFile):
            return f"{name} is not a zip file"
        return None if found else f"{name} is not an Odoo backup: it has no dump.sql"

    async def receive_file(request: Request, folder: Path, name: str, size: int | None = None,
                           sha256: str = "") -> tuple[Path | None, JSONResponse | None, str]:
        """The raw body into folder/name (another name when taken) through a
        .part file: whole and a database, or absent. (path, None, its
        SHA-256) or (None, the refusal, "")."""
        folder = private.backup_folder(folder)
        length = request.headers.get("content-length", "")
        if size is not None and length != str(size):
            return None, refuse(f"{name}: upload_backup was told {size} bytes, the request says "
                                f"{length or 'no Content-Length'}: ask for a new URL with the right size"), ""
        free = shutil.disk_usage(folder).free
        if length.isdigit() and free - int(length) < puller.MIN_FREE:
            return None, refuse(f"There is no room on {here_name()}: {int(length) >> 20} MiB sent, "
                                f"{free >> 20} MiB free", status.HTTP_507_INSUFFICIENT_STORAGE), ""
        dest = free_name(folder, name)
        handle, part_name = tempfile.mkstemp(dir=folder, prefix=f".{dest.name}.", suffix=".part")
        part, digest, count = Path(part_name), hashlib.sha256(), 0
        try:
            with os.fdopen(handle, "wb") as out:
                async for chunk in request.stream():
                    count += len(chunk)
                    if size is not None and count > size:
                        return None, refuse(f"{name}: more than the {size} bytes told"), ""
                    digest.update(chunk)
                    out.write(chunk)
            if length.isdigit() and count != int(length):
                return None, refuse(f"{name}: {count} of {length} bytes received"), ""
            if sha256 and digest.hexdigest() != sha256:
                return None, refuse(f"{name}: its SHA-256 is {digest.hexdigest()}, not {sha256} as told: "
                                    "the file changed on its way, nothing kept"), ""
            problem = await asyncio.to_thread(database_problem, part, name)
            if problem:
                return None, refuse(problem), ""
            os.replace(part, dest)
            os.chmod(dest, 0o600)
        except (ClientDisconnect, OSError) as exc:
            return None, refuse(f"{name} was not received whole: {exc}"), ""
        finally:
            part.unlink(missing_ok=True)
        return dest, None, digest.hexdigest()

    def refusal_text(answer) -> str | None:
        """The message of a refusal of refuse(), None when answer is not one."""
        if not isinstance(answer, JSONResponse):
            return None
        try:
            return str(json.loads(answer.body).get("error") or answer.body.decode())
        except ValueError:
            return answer.body.decode(errors="replace")

    def upload_ticket(token: str, scope: str, workspace: str, name: str, size, sha256: str, link: str,
                      base_url: str) -> dict:
        """upload_backup: a URL to PUT one database to (ActionRefused: why not)."""
        name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(str(name or "")).name)
        if not uploads_mod.NAME.fullmatch(name):
            raise ActionRefused("name: the file's name, ending in .zip (an Odoo backup: dump.sql, filestore, "
                                "manifest.json) or .sql (a plain SQL dump of pg_dump)")
        if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= uploads_mod.MAX_SIZE:
            raise ActionRefused(f"size: the file's size in bytes, at most {uploads_mod.MAX_SIZE >> 30} GiB")
        sha256 = str(sha256 or "").strip().lower()
        if sha256 and not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ActionRefused("sha256: the file's SHA-256 as 64 hexadecimal digits, or nothing")
        workspace = str(workspace or "").strip() or scope
        if scope and workspace != scope:
            raise ActionRefused(f"This token is of workspace {scope}: it uploads there only")
        if not WORKSPACE_NAME.fullmatch(workspace or ""):
            raise ActionRefused("workspace: the workspace the database is for (list_servers shows them)")
        if link:
            if scope:
                raise ActionRefused("A workspace's token does not reach the linked machines")
            if link not in settings.link_targets:
                raise ActionRefused(f"No link {link}: see list_links")
            folder = outgoing_folder()
        else:
            if workspace not in settings.workspaces:
                raise ActionRefused(f"No workspace {workspace} here")
            folder = imports_folder(workspace)
        free = shutil.disk_usage(private.backup_folder(folder)).free
        if free - size < puller.MIN_FREE:
            raise ActionRefused(f"There is no room on {here_name()} for {size >> 20} MiB: {free >> 20} MiB free")
        ticket = uploads.issue(token, workspace, name, size, sha256, link)
        path = f"/api/uploads/{ticket}"
        there = f" on {link}" if link else ""
        return {
            "url": base_url.rstrip("/") + path, "path": path, "method": "PUT",
            "headers": {"Authorization": "Bearer <the same token as this MCP connection>",
                        "Content-Type": "application/octet-stream", "Content-Length": str(size)},
            "expires_in_seconds": int(uploads.ttl), "size": size, "one_time": True,
            "then": (f"PUT the file's bytes as the body. The answer names the file kept in the import area of "
                     f"workspace {workspace}{there}"
                     + (f" and the job that sends it on to {link}: follow it with get_job, then call "
                        f"call_linked(link={link!r}, tool='restore_new_instance', arguments={{'workspace': "
                        f"{workspace!r}, 'file': <the job's result file>}})" if link else
                        ": give it to restore_new_instance as file")),
        }

    @app.put(UPLOAD_ROUTE)
    async def upload_put(request: Request, ticket: str, me: Principal = Depends(current_user)):
        """The PUT of an upload_backup URL: the bearer token that asked it,
        the size it told; the file into the workspace's import area, or here
        and then sent on to the linked machine (a job)."""
        if me.kind != "token":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "The bearer token that asked upload_backup is required")
        name = me.name
        found = uploads.take(ticket, name)
        if isinstance(found, str):
            return refuse(found, status.HTTP_404_NOT_FOUND)
        folder = outgoing_folder() if found.link else imports_folder(found.workspace)
        if not found.link and found.workspace not in settings.workspaces:
            return refuse(f"No workspace {found.workspace} here any more", status.HTTP_404_NOT_FOUND)
        dest, refused, digest = await receive_file(request, folder, found.name, found.size, found.sha256)
        if refused is not None:
            return refused
        answer = {"file": dest.name, "size": dest.stat().st_size, "sha256": digest, "workspace": found.workspace}
        if not found.link:
            return answer
        link, workspace = settings.link_targets.get(found.link), found.workspace
        if link is None:
            dest.unlink(missing_ok=True)
            return refuse(f"No link {found.link} any more: nothing kept", status.HTTP_404_NOT_FOUND)
        headers = link_headers(link, re.sub(r"[^A-Za-z0-9._@-]", "-", f"mcp-{name}")[:64], me.role)

        async def send_on(job):
            progress = job.result["progress"] = {
                "steps": [{"id": "download", "label": "Transfer", "where": f"{here_name()} → {link.name}"}],
                "step": "download", "done": 0, "total": 0}
            try:
                there = await linkcopy.upload(job, link_clients.client(link), link, headers, dest,
                                              "/api/imports/receive", {"workspace": workspace, "file": dest.name},
                                              progress)
            finally:
                # never kept here: it goes on, or it goes
                dest.unlink(missing_ok=True)
            if not str(there.get("file") or ""):
                raise JobFailed(f"{link.name} kept no file")
            job.result.update(link=link.name, workspace=workspace, file=there["file"], sha256=digest)
            job.log(f"{there['file']} is in the import area of workspace {workspace} on {link.name}: "
                    f"restore_new_instance there (call_linked)")

        job = jobs.submit(f"Send {dest.name} to workspace {workspace} on {link.name}", [], f"mcp: {name}", send_on)
        return {**answer, "link": link.name, "job": job.id}

    @app.post("/api/imports/receive", dependencies=[Depends(admin)])
    async def import_receive(request: Request, workspace: str = "", file: str = "",
                             me: Principal = Depends(current_user)):
        """A database sent by a linked OmniaCLI (its upload_backup with link),
        the raw body: into the import area of workspace, whole and a
        database, or nothing; then restore_new_instance takes it."""
        if me.kind == "token":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "A token may only read")
        ws = a_workspace(workspace)
        if not uploads_mod.NAME.fullmatch(file):
            return refuse("A database is sent with its file name, .zip or .sql")
        dest, refused, digest = await receive_file(request, imports_folder(ws.name), file)
        if refused is not None:
            return refused
        return {"file": dest.name, "size": dest.stat().st_size, "sha256": digest, "workspace": ws.name}

    async def core_modules(template) -> tuple[set[str] | None, list[str]]:
        """(Odoo's own modules of template's image, the instances it was read
        from): those every instance made here from template knows and that
        are not in its repositories (an instance restored from another
        database also knows that one's); None when none answers."""
        if template.name in core_memo:
            return core_memo[template.name], ["(read before)"]
        found, used = None, []
        for ws in list(settings.workspaces.values()):
            for instance in ws.instances():
                srv = settings.servers.get(workspaceops.server_name(ws, instance))
                if srv is None or srv.production is None:
                    continue
                made_from = await asyncio.to_thread(instance_template, ws, instance)
                if made_from is None or made_from.name != template.name:
                    continue
                known, _ = await known_modules(srv.production)
                if not known:
                    continue
                code = await asyncio.to_thread(code_modules, srv)
                mine = {m for m, state in known.items() if state != "uninstallable"} - code
                found = mine if found is None else found & mine
                used.append(srv.name)
        if found:
            core_memo[template.name] = found
        return found, used

    def new_instance_name(ws: Workspace, given: str, info: dict, path: Path) -> str:
        """The name asked, checked; or one from the dump's database (or the
        file), free in ws."""
        if given:
            if not WORKSPACE_NAME.fullmatch(given):
                raise ActionRefused("name: 1-32 lower case letters, digits and -")
            base = given
        else:
            raw = info.get("database") or path.name.split(".")[0]
            base = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")[:24].strip("-") or "restored"
        taken = set(ws.instances()) | {TEST_LABEL, *ws.subdomains}
        name, number = base, 2
        while name in taken or workspaceops.server_name(ws, name) in settings.servers or instance_name_problem(ws, name):
            if given:
                raise ActionRefused(f"{given} is taken in workspace {ws.name}, or one of its addresses is: "
                                    "choose another name")
            name, number = f"{base[:28]}-{number}", number + 1
        return name

    def template_of_major(major: int):
        found = workspaceops.templates()
        if f"odoo-{major}" in found:
            return found[f"odoo-{major}"]
        return next((t for t in found.values() if modulecatalog.series_of(t.name, t.label) == f"{major}.0"), None)

    async def restore_new(who: str, role: str, scope: str, workspace: str = "", file: str = "",
                          backup_of: str = "", name: str = "", neutralize: bool = True, staging_of: str = "",
                          dry_run: bool = True, add_repositories_: bool = False, allow_missing: bool = False) -> dict:
        """restore_new_instance: the plan (dry_run), or the job of a new
        instance of workspace loaded with file (ActionRefused: why not)."""
        me = Principal(who, role, "token")
        workspace = str(workspace or "").strip() or scope
        if scope and workspace != scope:
            raise ActionRefused(f"This token is of workspace {scope}: it restores there only")
        ws = settings.workspaces.get(workspace)
        if ws is None:
            raise ActionRefused(f"No workspace {workspace!r} here: give workspace (list_servers names the servers, "
                                "<workspace>-<instance>)")
        # the file: in the workspace's import area, or a backup of one of its servers
        file = str(file or "").strip()
        if not sqldump.RESTORE_NAME.fullmatch(file):
            raise ActionRefused("file: the name upload_backup answered, or a backup of list_backups with backup_of")
        if backup_of:
            server, _, from_role = backup_of.partition("/")
            if server not in ws.servers or server not in settings.servers or (from_role or "production") not in ROLES:
                raise ActionRefused(f"backup_of: a server of workspace {ws.name} (and /test for its test)")
            folder = settings.backup_dir / server / (from_role or "production")
        else:
            folder = imports_folder(ws.name)
        path = folder / file
        if not path.is_file():
            raise ActionRefused(f"No file {file} in " + (f"the backups of {backup_of}" if backup_of
                                                         else f"the import area of workspace {ws.name}: upload it with upload_backup"))
        try:
            info = await asyncio.to_thread(dumpinfo.read, path)
        except dumpinfo.DumpError as exc:
            raise ActionRefused(str(exc)) from None
        major = info["major"]
        if major is None:
            raise ActionRefused(f"{file} says no Odoo version (no manifest.json, no version of the module base)")
        # the template: the production's of staging_of, else the one of its version
        parent = None
        if staging_of:
            if staging_of not in ws.servers:
                raise ActionRefused(f"staging_of: a production of workspace {ws.name}")
            found = staging_source(staging_of)
            if not isinstance(found, tuple):
                raise ActionRefused(refusal_text(found))
            parent = found
            template = await asyncio.to_thread(instance_template, ws, found[2])
            if template is None:
                raise ActionRefused(f"The template {staging_of} was made from is not on this machine any more")
            if modulecatalog.series_of(template.name, template.label) not in (None, f"{major}.0"):
                raise ActionRefused(f"{file} is a database of Odoo {major}, {staging_of} runs another version "
                                    f"({template.label}): a staging of it cannot hold it")
        else:
            template = await asyncio.to_thread(template_of_major, major)
            if template is None:
                raise ActionRefused(f"{file} is a database of Odoo {major}: this machine has no template of Odoo "
                                    f"{major} (Templates)")
        series = f"{major}.0"
        # what the new instance's code will have: Odoo's own modules (its
        # stagings are made from the template without repositories either)
        core, read_from = await core_modules(template)
        installed = info["modules"] or []
        catalog = catalog_for(series)
        missing = [m for m in installed if core is None or m not in core]
        groups: dict[str, list[str]] = {}
        not_found = []
        for module in missing:
            entry = catalog.modules.get(module)
            if entry is not None:
                groups.setdefault(entry["repository"], []).append(module)
            elif core is not None:
                not_found.append(module)
        unchecked = [m for m in missing if core is None and m not in catalog.modules]
        source = modulecatalog.GitSource()
        oca = [{"repository": repo, "url": source.url(repo), "branch": series, "modules": mods}
               for repo, mods in sorted(groups.items())]
        try:
            free = workspace_free(await workspaceops.space(), ws)
        except workspaceops.SpaceError:
            free = None
        # the dump unpacked, and the database it becomes: its tables and
        # indexes take about three times its text (methydor_poc, 21/11/2026:
        # 37 MB of dump.sql, 112 MB of database), plus the files
        need = 4 * info["size"]
        instance = None if parent else new_instance_name(ws, str(name or "").strip(), info, path)
        server = workspaceops.server_name(ws, instance) if instance else f"{staging_of} (a staging, dated today)"
        report = {
            "file": file, "database": info["database"], "odoo": series, "kind": info["kind"],
            "filestore": info["filestore"] if info["kind"] == "zip" else False,
            "installed_modules": len(installed) if info["modules"] is not None else None,
            "new_instance": {"workspace": ws.name, "server": server, "template": template.name,
                             "neutralize": True if parent else bool(neutralize),
                             "staging_of": staging_of or None, "mail_caught": bool(parent)},
            "room": {"needed_gb": round(need / workspaceops.GB, 1),
                     "free_gb": None if free is None else round(free / workspaceops.GB, 1)},
            "missing_modules": {
                "count": sum(len(r["modules"]) for r in oca) + len(not_found) + len(unchecked),
                "in_oca": oca, "not_found": not_found,
                "not_checked": unchecked,
                "odoo_core_read_from": read_from if core is not None else [],
                "catalog_read": catalog.meta().get("read"),
            },
        }
        if info["modules"] is None:
            report["missing_modules"]["note"] = ("The dump has no table of modules that can be read (not a COPY of "
                                                 "ir_module_module): nothing was compared")
        if core is None:
            report["missing_modules"]["note"] = (
                f"No instance made from {template.name} answers on this machine: Odoo's own modules are not known, "
                "so the modules not in the catalog are not_checked; they are checked again once the instance is made, "
                "before the restore")
        if free is not None and need > free:
            report["room"]["problem"] = (f"Workspace {ws.name} has {report['room']['free_gb']} GB free, about "
                                         f"{report['room']['needed_gb']} GB are needed: make room (delete a staging)")
        blocking = [*not_found, *unchecked] if not allow_missing else []
        oca_blocking = oca if not (add_repositories_ or allow_missing) else []
        if dry_run:
            parts = [f"{file}: an Odoo {series} database ({report['installed_modules']} modules installed)"]
            if oca:
                parts.append(f"{sum(len(r['modules']) for r in oca)} of its modules are in the OCA repositories "
                             f"{', '.join(r['repository'] for r in oca)} (branch {series}): add_repositories=true adds them")
            if not_found:
                parts.append(f"{len(not_found)} are nowhere here ({', '.join(not_found[:10])}"
                             + ("..." if len(not_found) > 10 else "") + "): their code must be provided, e.g. the "
                             "customer's private repository or Odoo Enterprise, else allow_missing=true restores "
                             "without them (Odoo logs errors and their data stays unreachable)")
            if not oca and not not_found and not unchecked:
                parts.append("every installed module is available")
            report["dry_run"] = True
            report["message"] = ". ".join(parts) + "."
            report["next"] = ("Nothing was started. Ask the user to confirm, then call restore_new_instance again with "
                              "the same arguments and dry_run=false" + (", add_repositories=true" if oca else "")
                              + (", allow_missing=true" if not_found or unchecked else "") + ".")
            return report
        if "problem" in report["room"]:
            raise ActionRefused(report["room"]["problem"])
        if blocking or oca_blocking:
            names = [*blocking, *(m for r in oca_blocking for m in r["modules"])]
            raise ActionRefused(
                f"{len(names)} modules installed in {file} have no code here ({', '.join(names[:15])}"
                + ("..." if len(names) > 15 else "") + "): nothing started. "
                + ("add_repositories=true adds the OCA repositories that have them; " if oca_blocking else "")
                + ("allow_missing=true restores anyway: Odoo logs errors at each start and the data of those "
                   "modules stays unreachable until their code is added" if blocking or oca_blocking else ""))
        wanted = [(r["repository"], r["url"], r["branch"]) for r in oca] if add_repositories_ else []
        modules = installed

        async def load(job, target) -> dict:
            """The repositories added, the modules checked on the new
            instance's own database, then the file restored into it."""
            found = workspace_of(target.server)
            if wanted and found is not None:
                job.log(f"--- the repositories of the OCA {', '.join(w[0] for w in wanted)}, branch {series}")
                roles = [r for r in ROLES if getattr(settings.servers[target.server], r) is not None]
                done = await add_repositories(job, settings.workspaces[found[0].name], target.server, found[1],
                                              [(repo, url, {r: branch for r in roles}) for repo, url, branch in wanted])
                target = done.production
            known, why = await known_modules(target, fresh=True)
            still: list[str] = []
            if known is None:
                job.log(f"WARNING: the modules of {target.key} cannot be read ({why}): not checked again")
            else:
                # a repository just added is not in the database's list of
                # modules until Odoo reads its addons again: its code counts
                # (lab1, 21/11/2026: the 24 modules of 9 OCA repositories added
                # were said missing, and Odoo installed them at the restore)
                code = await asyncio.to_thread(code_modules, settings.servers[target.server])
                still = [m for m in modules if m not in code and known.get(m) in (None, "uninstallable")]
            if still and not allow_missing:
                raise JobFailed(f"{len(still)} modules of {file} have no code in {target.key} "
                                f"({', '.join(still[:15])}{'...' if len(still) > 15 else ''}): not restored, "
                                f"{target.server} is left empty; add their code, or allow_missing=true")
            if still:
                job.log(f"WARNING: restored without the code of {len(still)} modules: {', '.join(still)}. Odoo logs "
                        "errors for them and their data stays unreachable until their code is added")
            wrapped = None
            try:
                if info["kind"] == "sql":
                    wrapped = folder / f".{path.name}.{job.id}.zip"
                    job.log(f"--- {file}, a SQL dump, made a backup zip (dump.sql, no filestore)")
                    await asyncio.to_thread(dumpinfo.wrap_sql, path, wrapped, major, info["database"])
                job.log(f"--- {file} into {target.key}" + (" (neutralized)" if neutralize or parent else ""))
                await restore_fetched(job, target, wrapped or path, {"neutralize": bool(neutralize or parent)})
            finally:
                if wrapped is not None:
                    wrapped.unlink(missing_ok=True)
            job.result = {**job.result, "missing_modules": still, "modules_added_from": [w[0] for w in wanted]}
            return {"backup": file, "from_backup": True}

        if parent:
            made = await new_staging(staging_of, me, load=lambda job, prod, target: load(job, target),
                                     title=f"Staging of {staging_of} from {file}", source=f"mcp: {who}")
            refused = refusal_text(made)
            if refused:
                raise ActionRefused(refused)
            return {**report, "dry_run": False, "job": made["job"], "server": made["server"]}
        if workspaceops.docker_missing():
            raise ActionRefused(workspaceops.docker_missing())
        if not ws.storage or not ws.limits.get("disk_gb") or settings.storage_problem(ws):
            raise ActionRefused(f"Workspace {ws.name} cannot hold an instance: "
                                f"{settings.storage_problem(ws) or 'no storage or no disk limit'}")
        server = workspaceops.server_name(ws, instance)
        async with workspaces_lock:
            ws = settings.workspaces[ws.name]
            mine = [n for n in ws.servers if n in settings.servers]
            if ws.limits.get("instances") and len(mine) >= ws.limits["instances"]:
                raise ActionRefused(f"Workspace {ws.name} has its {int(ws.limits['instances'])} instances already")
            if server in ws.servers or server in settings.servers:
                raise ActionRefused(f"A server named {server} exists already")
            refused = refusal_text(await store_workspaces({**settings.workspaces,
                                                           ws.name: dataclasses.replace(ws, servers=[*ws.servers, server])}))
            if refused:
                raise ActionRefused(refused)

        async def action(job):
            made = await make_instance(job, settings.workspaces[ws.name], instance, template, template.values({}))
            await load(job, settings.servers[server].production)
            job.result.update(server=server, port=made.production.options.http_port)
            job.log(f"{server} is made from {file}" + (", neutralized" if neutralize else ""))

        job = jobs.submit(f"Create {server} from {file}", [f"{server}/production", f"{server}/test"],
                          f"mcp: {who}", action, workspace=ws.name)
        return {**report, "dry_run": False, "job": job.id, "server": server}

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
             "fixed": me.name in settings.fixed.users, "two_step": two_step_state(request, me.name)},
        )

    # --- its two-step verification (totp.py): on with a code of the app, the
    # recovery codes shown once; off with a code, unless it is required here

    def two_step_state(request: Request, name: str) -> dict:
        state = second_factors.state(name)
        return {**state, "since": moment(state["since"]) if state["since"] else "",
                "required": second_step_required(request, name)}

    def code_refused(request: Request, me: Principal):
        """A response when me may try no code now (the failed logins' wait)."""
        wait = limiter.wait(request.client.host if request.client else "", me.name)
        if wait:
            return refuse(f"Too many failed logins: try again in {wait} seconds.", status.HTTP_429_TOO_MANY_REQUESTS)
        return None

    def code_failed(request: Request, me: Principal, what: str):
        ip = request.client.host if request.client else ""
        limiter.failed(ip, me.name)
        access_log.record("failed", "user", me.name, ip, f"{what}: wrong code")
        return refuse("Wrong code: type the one the app shows now (check the clock of the phone)")

    @app.post("/api/account/second-factor/start")
    async def second_factor_start(request: Request, me: Principal = Depends(current_user)):
        """A new secret for the app (its QR code and as text), kept until it is
        confirmed with a code, PENDING_SECONDS at most."""
        password_user(me)
        if second_factors.on(me.name):
            return refuse("Two-step verification is on already")
        secret = totp.new_secret()
        account_setups[me.name] = (secret, time.time() + totp.PENDING_SECONDS)
        uri = totp.otpauth_uri(secret, f"{me.name}@{request.url.hostname}")
        return JSONResponse({"secret": secret, "uri": uri, "qr": totp.qr_svg(uri)}, headers={"Cache-Control": "no-store"})

    @app.post("/api/account/second-factor/confirm")
    async def second_factor_confirm(request: Request, payload: dict = Body(...), me: Principal = Depends(current_user)):
        """On, once a code of the new secret is right: the recovery codes, once."""
        password_user(me)
        found = account_setups.get(me.name)
        if found is None or found[1] < time.time():
            return refuse("The set-up has expired: start it again")
        refused = code_refused(request, me)
        if refused:
            return refused
        used = totp.code_step(found[0], str(payload.get("code") or ""))
        if used is None:
            return code_failed(request, me, "two-step set-up")
        account_setups.pop(me.name, None)
        codes = await asyncio.to_thread(second_factors.enable, me.name, found[0], used)
        access_log.record("second-factor", "user", me.name, request.client.host if request.client else "",
                          "switched on (Account)", role=settings.user_role(me.name))
        return JSONResponse({"ok": True, "recovery": codes}, headers={"Cache-Control": "no-store"})

    @app.post("/api/account/second-factor/disable")
    async def second_factor_disable(request: Request, payload: dict = Body(...), me: Principal = Depends(current_user)):
        """Off with a code of the app or a recovery code; not where it is required."""
        password_user(me)
        if not second_factors.on(me.name):
            return refuse("Two-step verification is not on")
        if second_step_required(request, me.name):
            return refuse("Two-step verification is required for you on this machine: it cannot be switched off",
                          status.HTTP_403_FORBIDDEN)
        refused = code_refused(request, me)
        if refused:
            return refused
        result = await asyncio.to_thread(second_factors.check, me.name, str(payload.get("code") or ""))
        if result is None:
            return code_failed(request, me, "two-step switch-off")
        await asyncio.to_thread(second_factors.disable, me.name)
        # its other sessions end; this one stays (it just gave a code)
        sessions.drop_user(me.name, keep=request.cookies.get(COOKIE))
        access_log.record("second-factor", "user", me.name, request.client.host if request.client else "",
                          "switched off (Account)" + (", with a recovery code" if result[0] == "recovery" else ""),
                          role=settings.user_role(me.name))
        return {"ok": True}

    async def reset_second_factor(request: Request, me: Principal, name: str, where: str):
        """name's two-step verification off, by an admin: the user sets it up
        again (at its next login, where it is required)."""
        if me.kind == "user" and name == me.name:
            return refuse("Your own is switched off on your Account page, with a code")
        if not second_factors.on(name):
            return refuse(f"{name} has no two-step verification on")
        await asyncio.to_thread(second_factors.disable, name)
        # every session of that user ends: it logs in again and sets the second step up
        sessions.drop_user(name)
        access_log.record("second-factor", "user", name, request.client.host if request.client else "",
                          f"reset by {me}{where}: it sets it up again", role=settings.user_role(name))
        return {"ok": True}

    @app.post("/api/access/users/second-factor/reset")
    async def user_second_factor_reset(request: Request, payload: dict = Body(...), me: Principal = Depends(admin)):
        name = str(payload.get("name") or "")
        if name not in settings.users:
            return refuse(f"No user {name}", status.HTTP_404_NOT_FOUND)
        return await reset_second_factor(request, me, name, " (Users)")

    @app.post("/api/workspaces/{workspace}/members/second-factor/reset", dependencies=[Depends(admin)])
    async def member_second_factor_reset(request: Request, workspace: str, payload: dict = Body(...),
                                         me: Principal = Depends(current_user)):
        """A workspace's admin resets it for a user of this workspace only
        (one who gets in elsewhere too: the machine's admins)."""
        name = str(payload.get("name") or "")
        ws = a_workspace(workspace)
        if name not in ws.members:
            return refuse(f"{name} is not a user of {workspace}", status.HTTP_404_NOT_FOUND)
        if not machine_admin(me) and not only_here(name, workspace):
            return refuse(f"{name} is also a user of other workspaces or of the machine: "
                          "ask the administrator of the machine to reset it")
        return await reset_second_factor(request, me, name, f" (workspace {workspace})")

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

    # --- the machine's Database tab (UX-008 §6.2; docs/adr/postgresql-della-macchina.md
    # AD-005, steps 1 and 2): the dedicated databases of this machine read, a
    # proposal of values, the values written (those valid at once now, those of
    # a restart right before it, now or at a time chosen), the base values
    # back. The machine's admins only; the record in pgtuning.Store.

    POSTGRES_EVERY = 20
    POSTGRES_HELPER_TIMEOUT = 120
    # the restart: 3 minutes to answer, as many again to put the values of before back
    POSTGRES_RESTART_TIMEOUT = 900

    def a_machine(me: Principal) -> bool:
        """me is another machine acting by itself through its link (a job, a
        look), not a person who reads this machine's pages through that
        machine's panel: what a machine asks for is its own only (its
        replica, its database), while a person there is an admin as one who
        logs in here, whatever panel they came through."""
        return me.kind == "link" and not me.person

    def postgres_admin(me: Principal) -> None:
        if not machine_admin(me):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "An admin of the machine is required")

    # --- SSH access (README, SSH access): the keys of the people who log in to
    # the machine, written by the root helper (sshkeys.py) in the accounts'
    # authorized_keys, only the lines it marked; the machine's admins, a
    # person through any panel too, never a machine acting by itself
    def ssh_admin(me: Principal) -> None:
        if not machine_admin(me) or a_machine(me):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "The SSH keys of the machine are for its admins")

    def ssh_refusal(text: str) -> str:
        text = text.removeprefix("workspace-helper: ").strip()
        if "sudo" in text and ("password" in text or "not allowed" in text):
            return ("sudo does not let OmniaCLI run its root helper here: the SSH keys are written by root "
                    "(OMNIACLI_WORKSPACES=1, README, SSH access)")
        return text

    @app.get("/api/machine/ssh-keys")
    async def ssh_keys_list(request: Request, me: Principal = Depends(current_user)):
        """The accounts a key may open, the keys OmniaCLI added to each (name,
        type, fingerprint, who, when) and the others there (their fingerprints)."""
        ssh_admin(me)
        try:
            found = await workspaceops.ask("ssh-keys")
        except JobFailed as exc:
            return refuse(ssh_refusal(str(exc)), status.HTTP_502_BAD_GATEWAY)
        lang = request_language(request)
        for row in found.get("accounts") or []:
            if row.get("error"):
                row["error"] = i18n.tr(lang, row["error"])
        return found

    @app.post("/api/machine/ssh-keys/add")
    async def ssh_key_add(request: Request, payload: dict = Body(...), me: Principal = Depends(current_user)):
        """A public key (name, the .pub line, the account it opens) at the end
        of the account's authorized_keys; in the access log with its fingerprint."""
        ssh_admin(me)
        try:
            account = sshkeys.check_account(payload.get("account"))
            name = sshkeys.check_name(payload.get("name"))
            sshkeys.check_pasted(payload.get("key"))
        except sshkeys.KeyRefused as exc:
            return refuse(str(exc))
        given = json.dumps({"name": name, "key": str(payload.get("key")).strip(), "by": str(me)}).encode()
        try:
            done = await workspaceops.ask("ssh-key-add", account, input=given)
        except JobFailed as exc:
            return refuse(ssh_refusal(str(exc)))
        access_log.record("action", me.kind, me.name, request.client.host if request.client else "",
                          f"SSH key {done.get('fingerprint')} ({done.get('type')}, {name}) added: it opens "
                          f"{account} on this machine" + (f", for {me.on_behalf}" if me.on_behalf else ""))
        return done

    @app.post("/api/machine/ssh-keys/remove")
    async def ssh_key_remove(request: Request, payload: dict = Body(...), me: Principal = Depends(current_user)):
        """A key OmniaCLI added taken out of the account's authorized_keys."""
        ssh_admin(me)
        try:
            account = sshkeys.check_account(payload.get("account"))
        except sshkeys.KeyRefused as exc:
            return refuse(str(exc))
        found = str(payload.get("fingerprint") or "")
        if not sshkeys.FINGERPRINT.fullmatch(found):
            return refuse(f"{found!r} is not the fingerprint of a key (SHA256:…)")
        try:
            done = await workspaceops.ask("ssh-key-remove", account, found)
        except JobFailed as exc:
            return refuse(ssh_refusal(str(exc)))
        access_log.record("action", me.kind, me.name, request.client.host if request.client else "",
                          f"SSH key {found} ({done.get('type')}, {done.get('name')}) removed: it no longer opens "
                          f"{account} on this machine" + (f", for {me.on_behalf}" if me.on_behalf else ""))
        return done

    def postgres_machine() -> dict:
        """The memory for the databases of the machine (saved, else D13's
        default), the machine's memory and its disk's kind."""
        saved = tuning.load()["machine"]
        total = pgtuning.machine_memory_gb()
        odoo = sum(float(ws.limits.get("memory_gb") or 0) for ws in settings.workspaces.values())
        default = pgtuning.default_memory_gb(total, odoo)
        root = next(iter(settings.storage_roots.values()), "/")
        disk = saved.get("disk") if saved.get("disk") in pgtuning.DISKS else pgtuning.disk_kind(Path(root))
        return {"memory_gb": float(saved.get("memory_gb") or default), "default_gb": default,
                "total_gb": round(total, 1) if total else None, "odoo_gb": odoo, "disk": disk,
                "cores": os.cpu_count() or 1}

    def postgres_kind(srv: Server, role: str, known: dict) -> tuple[str, str]:
        """production, test or staging (the production of a copy of another's),
        and that other: a staging's test is under it too."""
        parent = str((known.get(srv.name) or {}).get("parent") or "")
        return ("staging" if parent and role == "production" else role), parent

    async def postgres_rows(lang: str) -> dict:
        """Every database of this machine's instances, read in parallel, each
        within its own time: the dedicated ones (a compose project's service),
        this machine's PostgreSQL (one row for the instances it serves), and
        those on another machine (the line Not here)."""
        known = await asyncio.to_thread(stagings.load, stagings_file)
        pitr = await asyncio.to_thread(deployhooks.load, pitr_file)
        records = (await asyncio.to_thread(tuning.load))["clusters"]
        dedicated, system, remote, replaced_rows = [], {}, [], []
        clusters = await machine_clusters()
        for srv in settings.active_servers():
            for role in ("production", "test"):
                inst = srv.instances.get(role)
                if inst is None:
                    continue
                if inst.restart.type == "compose" and on_machine_db(inst):
                    continue  # a row of the machine's cluster, among those it serves
                if inst.restart.type == "compose" and on_remote_db(inst):
                    # on another machine: the line Not here, it is tuned there
                    meta = instance_meta(inst)
                    pg = dedicated_key(inst) if role == "production" else ""
                    followed = replica_record(pg) if pg else None
                    if followed is not None and followed.get("state") == "replaced":
                        # its own database was replaced by its promoted copy (UX-011 §D): its row stays, and
                        # says so; the return (§A, for one instance) is prepared, completed and cancelled from it
                        replaced_rows.append({
                            "key": inst.key, "server": srv.name, "role": role, "kind": "production", "pg": pg,
                            "parent": "", "where": "dedicated", "workspace": ws_found[0].name
                            if (ws_found := workspace_of(srv.name)) else "", "pitr": None, "params": [], "changed": [],
                            "error": None, "tuned": None, "restart": None, "port": None,
                            "host": str(meta.get("database_on") or ""),
                            "replica": {"exists": True, "state": "replaced", "link": followed.get("link"),
                                        "replaced_at": followed.get("replaced_at"), "verdict": {"code": "replaced"},
                                        "readers": []},
                            "state": {"code": "replaced", "at": followed.get("replaced_at"), "link": followed.get("link")},
                            # the data it had here is still beside it (Free the space of before…, in its page)
                            "kept": bool((await asyncio.to_thread(move_offer, inst))["kept"]), "return": None})
                        continue
                    remote.append({"key": inst.key, "server": srv.name, "role": role,
                                   "host": str(meta.get("database_on") or "") or "another machine",
                                   # the cluster of a linked OmniaCLI: named, its Database tab is where it is tuned
                                   "link": link_now(inst) or None,
                                   "major": str(meta.get("database_major") or "") or None,
                                   # the promoted replica of this machine's cluster: the machine that hosts it
                                   "promoted": str(meta.get("database_link") or "") if meta.get("promoted_from") else "",
                                   # on a linked cluster (UX-011 §B): it does not answer (since when, why); it is on
                                   # the promoted copy of that cluster, which may be one this very machine hosts
                                   "down": ({"since": (moved_seen.get(srv.name) or {}).get("since"),
                                             "why": i18n.tr(lang, str((moved_seen.get(srv.name) or {}).get("why") or ""))}
                                            if (moved_seen.get(srv.name) or {}).get("down") is not None else None),
                                   "copy": str((meta.get(workspacehelper.MOVED_META) or {}).get("machine") or "")
                                   if isinstance(meta.get(workspacehelper.MOVED_META), dict) else "",
                                   "local": hosted_here(str(meta.get("database_on") or ""))})
                    continue
                if inst.restart.type == "compose":
                    kind, parent = postgres_kind(srv, role, known)
                    # its read replica on another machine (UX-011 §D): the production's only, by its own record
                    pg = dedicated_key(inst) if kind == "production" else ""
                    dedicated.append({"key": inst.key, "server": srv.name, "role": role, "kind": kind,
                                      "pg": pg, "followed": bool(pg) and replica_record(pg) is not None, "replica": None,
                                      # the port it listens on for a copy on another machine, also when it has
                                      # none any more (Close the port…); its return, once complete (its own record)
                                      "port": (machinedb.listening(dedicated_folder(inst)) or {}).get("port") if pg else None,
                                      "back": bool(pg) and return_record(pg) is not None, "return": None,
                                      "parent": parent, "where": "dedicated", "instance": inst,
                                      "workspace": ws_found[0].name if (ws_found := workspace_of(srv.name)) else "",
                                      "pitr": ("on" if srv.name in pitr else "off") if kind == "production" else None})
                    continue
                try:
                    shell = inst.plugin.db_shell(inst)
                except Exception:  # noqa: BLE001 - a plugin's config that cannot be read: no row
                    shell = None
                if shell is None:
                    continue
                place = pgreport.where(inst, shell[1])
                if place["where"] == "remote":
                    remote.append({"key": inst.key, "server": srv.name, "role": role, "host": place["host"]})
                else:
                    port = str(shell[1].get("PGPORT") or 5432)
                    system.setdefault(port, []).append(inst)

        async def one(row: dict) -> dict:
            inst = row.pop("instance")
            names = row.pop("names", {})
            record = records.get(row["key"]) or {}
            followed = backed = None
            if row.pop("followed", False):
                # its read replica on another machine (AD-004): what this machine sees of it, read beside the rest
                followed = asyncio.get_running_loop().create_task(replica_of_row(row.get("pg") or row["major"]))
            if row.pop("back", False):
                # its return after a promotion (UX-011 §A): what this machine and the one of the promoted copy see of it
                backed = asyncio.get_running_loop().create_task(return_of_row(row.get("pg") or row["major"], lang))

            async def replaced():
                """The replica's and the return's parts of the row; a cluster its replica replaced says so,
                stopped or following the promoted copy to come back (not a cluster that does not answer)."""
                if backed is not None:
                    row["return"] = await backed
                if followed is not None:
                    row["replica"] = await followed
                    if row["where"] == "machine" and (row["replica"] or {}).get("state") == "replaced":
                        row["state"] = {"code": "replaced", "at": row["replica"].get("replaced_at"),
                                        "link": row["replica"].get("link")}
            row.update({"tuned": {k: record.get(k) for k in ("tuned", "by", "params", "memory_gb")}
                        if record.get("tuned") else None, "restart": record.get("restart")})
            try:
                found = await pgtuning.read(inst)
            except (JobFailed, ValueError) as exc:
                row.update({"error": i18n.tr(lang, str(exc)), "params": [], "changed": [],
                            "state": pgtuning.state(row["kind"], None, record, i18n.tr(lang, str(exc)))})
                await replaced()
                return row
            params = pgtuning.parameters(found)
            row.update({"version": str(found.get("version") or "").split(" ")[0], "size": found.get("cluster_size") if row["where"] == "machine" else found.get("size"),
                        "connections": found.get("connections"),
                        "max_connections": (pgtuning.by_name(params).get("max_connections") or {}).get("now"),
                        "statements_on": bool(found.get("statements_on")), "params": params,
                        "changed": pgtuning.changed(found), "error": None,
                        "state": pgtuning.state(row["kind"], params, record)})
            await replaced()
            if row["where"] == "machine":
                # its recovery at a minute (AD-006): the archive, its disk against the budget, a recovery's room
                try:
                    row["archive"] = await workspaceops.ask("machine-db-archive-state", row["major"],
                                                            timeout=pgtuning.READ_TIMEOUT * 4)
                    row["pitr"] = "on" if row["archive"].get("enabled") else "off"
                    # its last recovery by the names people know: the instance and the staging born of it
                    # (the database's own name stays, for the small print); a staging deleted since has none
                    for key in ("last_recovery", "last_failure"):
                        last = row["archive"].get(key)
                        if isinstance(last, dict):
                            last["instance"] = names.get(str(last.get("database") or ""), "")
                            last["staging"] = names.get(str(last.get("target") or ""), "")
                except JobFailed as exc:
                    _logger.warning("the archive of the machine's PostgreSQL cannot be read: %s", exc)
            return row

        async def shared(port: str, served: list[Instance]) -> dict:
            row = {"key": f"system:{port}", "kind": "system", "where": "system", "port": port, "params": [],
                   "changed": [], "serves": [{"key": i.key, "server": i.server, "role": i.role} for i in served],
                   "state": {"code": "system"}, "error": None}
            reports = await asyncio.gather(*(asyncio.wait_for(pgreport.report(i), pgtuning.READ_TIMEOUT)
                                             for i in served), return_exceptions=True)
            good = [r for r in reports if isinstance(r, dict)]
            if not good:
                bad = next((r for r in reports if isinstance(r, BaseException)), None)
                reason = "no answer" if isinstance(bad, asyncio.TimeoutError) or bad is None else str(bad)
                row.update({"error": i18n.tr(lang, reason), "state": {"code": "error", "reason": i18n.tr(lang, reason)}})
                return row
            row.update({"version": str(good[0].get("version") or "").split(" ")[0],
                        "size": sum(int(r.get("size") or 0) for r in good),
                        "connections": sum(int(r.get("connections") or 0) for r in good),
                        "max_connections": good[0].get("max_connections")})
            return row

        async def replica_of_row(major: str) -> dict:
            try:
                return await replica_state_here(major)
            except JobFailed as exc:
                rec = replica_record(major) or {}
                return {"exists": bool(rec), "unread": True, "error": i18n.tr(lang, str(exc)), "link": rec.get("link"),
                        "state": rec.get("state"), "replaced_at": rec.get("replaced_at"), "address": rec.get("address"),
                        "verdict": managedreplica.verdict({"exists": bool(rec), **rec}) if rec else {"code": "none"},
                        "readers": []}

        machine_rows = []
        for found in clusters.get("clusters") or []:
            major = str(found.get("major") or "")
            target = cluster_target(major, str(found.get("project") or ""))
            serves = sorted(f"{e.get('workspace')}-{e.get('instance')}" for e in (found.get("environments") or {}).values())
            machine_rows.append({"key": target.key, "server": MACHINE_KEY, "role": major, "kind": "machine",
                                 "parent": "", "where": "machine", "instance": target, "workspace": "", "pitr": None,
                                 "archive": None,
                                 # the instance of each database of the cluster, by the name its page has
                                 "names": {name: f"{e.get('workspace')}-{e.get('instance')}"
                                           for name, e in (found.get("environments") or {}).items()},
                                 "followed": bool(found.get("replica")), "replica": None,
                                 # its return after a promotion (its own record, a file OmniaCLI reads)
                                 "back": return_record(major) is not None, "return": None,
                                 # the instances that use it now, as the servers file says (after a promotion: none)
                                 "using": sorted(i.server for i in machine_served()),
                                 "major": major, "storage": found.get("storage") or "",
                                 "promised": int(found.get("promised") or 0), "running": bool(found.get("running")),
                                 "serves": [{"key": f"{s}/production", "server": s, "role": "production"}
                                            for s in serves],
                                 # reached from other machines (AD-003): its port, and who uses which database
                                 "port": found.get("port"),
                                 # replaced: the databases other machines use whose instance has not followed yet
                                 "waited": grants_waited(major),
                                 "grants": [{"name": name, "link": str(g.get("link") or ""),
                                             "address": str(g.get("address") or ""), "by": str(g.get("by") or ""),
                                             "at": str(g.get("at") or ""), "revoked": bool(g.get("revoked")),
                                             "followed": grants_followed(major).get(name),
                                             "instance": "-".join(p for p in (str(g.get("workspace") or ""),
                                                                              str(g.get("instance") or "")) if p)}
                                            for name, g in sorted((found.get("grants") or {}).items())]})
        async def gone(row: dict) -> dict:
            """The row of an instance whose database is on its promoted copy: its return, when one is there."""
            if return_record(row["pg"]) is not None:
                row["return"] = await return_of_row(row["pg"], lang)
            return row

        rows = await asyncio.gather(*(one(r) for r in machine_rows), *(one(r) for r in dedicated),
                                    *(shared(p, s) for p, s in system.items()), *(gone(r) for r in replaced_rows))
        done = [r for r in rows if r["where"] == "dedicated"]
        # by instance: the production, its test, its stagings under it, the newest first
        order = {"production": 0, "test": 1}
        top = sorted((r for r in done if not (r["parent"] and any(p["server"] == r["parent"] for p in done))),
                     key=lambda r: (r["server"], order.get(r["role"], 2)))
        out = []
        for row in top:
            out.append(row)
            if row["role"] == "production":
                children = sorted((r for r in done if r["parent"] == row["server"] and r not in top),
                                  key=lambda r: order.get(r["role"], 2))
                out += sorted(children, key=lambda r: r["server"], reverse=True)
        out += [r for r in rows if r["where"] == "system"]
        # the machine's cluster first: it serves many
        out = [r for r in rows if r["where"] == "machine"] + out
        return {"rows": out, "remote": remote, "cluster": cluster_offer(clusters)}

    # --- the machine's cluster (AD-001, steps 3 and 4): a row of the tab, its
    # tuning as a dedicated one's (compose-helper on its project, service db,
    # database postgres), made and taken away by the root helper
    MACHINE_KEY = "_machine"
    MACHINE_TIMEOUT = 900
    cluster_targets: dict[str, types.SimpleNamespace] = {}

    def on_machine_db(inst: Instance) -> bool:
        return getattr(inst.options, "db_service", "") == machinedb.DB_SERVICE

    def on_remote_db(inst: Instance) -> bool:
        """Its database is on another machine (AD-003): no service of its own, not this machine's cluster."""
        return getattr(inst.options, "db_service", "") == stacktemplates.REMOTE_DB_SERVICE

    def instance_meta(inst: Instance) -> dict:
        """The instance's own record (instance.json), {} when it cannot be read."""
        try:
            meta = json.loads((Path(inst.restart.service).parent / workspacehelper.INSTANCE_FILE).read_text("utf-8"))
        except (OSError, ValueError):
            return {}
        return meta if isinstance(meta, dict) else {}

    def remote_db_host(inst: Instance, key: str = "database_on") -> str:
        """host:port of that database (or, key replica_on, of its read replica), from the instance's
        own record ("" when it cannot be read or there is none)."""
        return str(instance_meta(inst).get(key) or "")

    def cluster_target(major: str, project: str) -> types.SimpleNamespace:
        """What the tuning's code takes of an instance, for the cluster of
        major: its key, its helper's commands, its service and database."""
        key = f"{MACHINE_KEY}/{major}"
        target = types.SimpleNamespace(
            key=key, server=MACHINE_KEY, role=major, cluster=True, project=project,
            options=types.SimpleNamespace(db_service=machinedb.SERVICE, database="postgres", odoo_service=None),
            restart=types.SimpleNamespace(type="compose", service=project,
                                          command=lambda *args: workspaceops.compose(project, *args)))
        cluster_targets[key] = target
        return target

    async def machine_clusters() -> dict:
        """The root helper's machine-db-state, {} on a machine without
        workspaces or when it does not answer (said in the log)."""
        if not settings.storage_roots or not machinedb.registry():
            return {}
        try:
            return await workspaceops.ask("machine-db-state", timeout=pgtuning.READ_TIMEOUT * 4)
        except JobFailed as exc:
            _logger.warning("the machine's cluster cannot be read: %s", exc)
            return {}

    def cluster_offer(clusters: dict) -> dict:
        """What + Machine's cluster needs: whether one may be made here, the
        majors without one, the storage it goes on, why not."""
        made = {str(c.get("major")) for c in clusters.get("clusters") or []} or set(machinedb.registry())
        why = ("no storage for workspaces on this machine" if not settings.storage_roots
               else workspaceops.docker_missing() or "")
        missing = [m for m in machinedb.MAJORS if m not in made]
        return {"made": sorted(made), "missing": missing, "storage": next(iter(settings.storage_roots), ""),
                "why": why}

    def known_cluster(major: str):
        """The target of the cluster of major as the root's record says it
        (/etc/omniacli/machine-db.json, readable), None when none is made."""
        found = machinedb.registry().get(major) if major in machinedb.MAJORS else None
        return cluster_target(major, str(found.get("project") or "")) if found else None

    def tuning_target(server: str, role: str):
        """The instance whose dedicated database is tuned, or the machine's cluster."""
        if server == MACHINE_KEY:
            target = known_cluster(role)
            if target is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, f"No cluster of PostgreSQL {role} on this machine")
            return target
        return get_instance(server, role)

    def postgres_row(rows: list[dict], server: str, role: str) -> dict:
        # (an instance whose database is on its promoted copy has a row and nothing to tune here)
        found = next((r for r in rows if r.get("key") == f"{server}/{role}" and r["where"] in ("dedicated", "machine")
                      and not (r["where"] == "dedicated" and (r.get("state") or {}).get("code") == "replaced")), None)
        if found is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                f"{server}/{role} has no database of its own here: only a dedicated one is tuned")
        return found

    def postgres_memory(machine: dict, rows: list[dict]) -> dict:
        read = [r for r in rows if r["where"] in ("dedicated", "machine") and r.get("params")]
        return {**machine, "assigned": sum(pgtuning.shared_of(r["params"]) for r in read),
                "at_base": sum(1 for r in read if r["state"]["code"] in ("base", "base_production"))}

    @app.get("/api/postgres")
    async def postgres_list(request: Request, me: Principal = Depends(current_user)):
        """The databases of this machine (UX-008 §6.2.12)."""
        postgres_admin(me)
        lang = request_language(request)
        found, hosted = await asyncio.gather(postgres_rows(lang), replica_rows(me, lang))
        machine = await asyncio.to_thread(postgres_machine)
        # the read replicas of other machines' clusters this machine hosts (AD-004), and whether one may be made
        return {**found, "replicas": hosted, "replica_offer": replica_offer(), "here": here_name(),
                "memory": postgres_memory(machine, found["rows"]), "now": datetime.datetime.now().isoformat(
            timespec="seconds")}

    @app.post("/api/postgres/machine/create")
    async def postgres_machine_create(payload: Optional[dict] = Body(None), me: Principal = Depends(current_user)):
        """+ Machine's cluster: the PostgreSQL of the machine made (a job of the
        root helper, machine-db-create), its data on the first storage."""
        postgres_admin(me)
        payload = payload if isinstance(payload, dict) else {}
        offer = cluster_offer({})
        if offer["why"]:
            return refuse(f"No cluster of the machine here: {offer['why']}")
        major = str(payload.get("major") or (offer["missing"] or [""])[0])
        if major not in machinedb.MAJORS:
            return refuse(f"PostgreSQL {major or '?'} is not one the machine's cluster runs "
                          f"({', '.join(machinedb.MAJORS)})")
        if major not in offer["missing"]:
            return refuse(f"The machine's cluster of PostgreSQL {major} exists already")
        storage = str(payload.get("storage") or offer["storage"])
        if storage not in settings.storage_roots:
            return refuse(f"{storage!r} is not a storage of this machine")

        async def action(job):
            job.log(f"--- the machine's PostgreSQL {major}: a container of its own, its data on the storage "
                    f"{storage}, no port, reached by its socket")
            found = await workspaceops._helper(job, "machine-db-create", storage, major, timeout=MACHINE_TIMEOUT)
            job.log(f"PostgreSQL {found.get('version', '').split(' ')[0]} answers; it runs as {found.get('user')}, "
                    f"its socket in {found.get('socket')}. The new instances are made on it from now on.")
            job.result = {**job.result, "cluster": major}
        job = jobs.submit(f"Machine's PostgreSQL {major}", [f"{MACHINE_KEY}/{major}"], f"manual: {me}", action)
        return {"job": job.id}

    @app.post("/api/postgres/machine/{major}/remove")
    async def postgres_machine_remove(major: str, me: Principal = Depends(current_user)):
        """The machine's cluster taken away, when it serves nothing."""
        postgres_admin(me)
        if known_cluster(major) is None:
            return refuse(f"No cluster of PostgreSQL {major} on this machine", status.HTTP_404_NOT_FOUND)
        served = machine_served()
        if served:
            return refuse(f"The machine's cluster serves {', '.join(i.key for i in served)}: move them to a "
                          "dedicated database or delete them first")
        followed = replica_record(major) or {}
        if followed and followed.get("state") != "replaced":
            return refuse(f"The machine's cluster has a read replica on {followed.get('link')}: drop the replica first")
        if (return_record(major) or {}).get("state") in ("copying", "ready"):
            return refuse("A return is under way: cancel it first.")
        waited = grants_waited(major)
        if waited:
            return refuse(f"{', '.join(waited)} have not followed their database to the copy on {followed.get('link')} "
                          "yet: the cluster is taken away once they have.")

        async def action(job):
            found = await workspaceops._helper(job, "machine-db-remove", major, timeout=MACHINE_TIMEOUT)
            replica_own.pop(major, None)
            job.log(f"The machine's PostgreSQL {major} is taken away: {found.get('removed')}; "
                    f"its user {found.get('user')}, the image {found.get('image')}")
            await asyncio.to_thread(postgres_record, f"{MACHINE_KEY}/{major}", remove=list(pgtuning.PARAMETERS),
                                    restart=None)
        job = jobs.submit(f"Remove the machine's PostgreSQL {major}", [f"{MACHINE_KEY}/{major}"], f"manual: {me}",
                          action)
        return {"job": job.id}

    # --- a database of the machine's cluster for an instance of another machine, a linked
    # OmniaCLI's (AD-003). This machine's side when it holds the database: the offer, the
    # grant (asked through the link only: its name is recorded), revoke, allow again, drop,
    # the port closed. The other side, the machine that makes the instance: linked_request,
    # linked_grant, linked_release, GET /api/links/{name}/postgres.

    async def cluster_state(major: str) -> dict | None:
        found = await machine_clusters()
        return next((c for c in found.get("clusters") or [] if str(c.get("major")) == major), None)

    def firewall_line(done: dict) -> str:
        """What the firewall lets reach the cluster's port after a helper's action (its answer)."""
        allowed = ", ".join(done.get("firewall") or [])
        return (f"The firewall lets the cluster's port be reached from {allowed} only" if allowed
                else "The firewall lets the cluster's port be reached from no address")

    @app.get("/api/postgres/machine/offer")
    async def postgres_offer(me: Principal = Depends(current_user)):
        """The clusters of this machine a linked OmniaCLI may ask a database
        of: the PostgreSQL, whether it runs, its port when it listens
        already, how many instances lose the database when it starts to."""
        postgres_admin(me)
        found = await machine_clusters()
        return {"machine": here_name(), "clusters": [
            {"major": str(c.get("major")), "running": bool(c.get("running")), "port": c.get("port"),
             "serves": len(c.get("environments") or {}), "grants": len(c.get("grants") or {})}
            for c in found.get("clusters") or []]}

    @app.post("/api/postgres/machine/{major}/grants")
    async def postgres_grant(major: str, payload: dict = Body(...), me: Principal = Depends(current_user)):
        """A database of the cluster for an instance of the machine that asks
        through its link: a job of the root helper (machine-db-grant). Its
        result carries the port, the role, the database and the CA: no secret
        (the role's password stays on the other machine: only its verifier comes)."""
        postgres_admin(me)
        if not a_machine(me):
            return refuse("A database for another machine is asked by that machine, through its link",
                          status.HTTP_403_FORBIDDEN)
        if known_cluster(major) is None:
            return refuse(f"No cluster of PostgreSQL {major} on this machine", status.HTTP_404_NOT_FOUND)
        name, verifier = str(payload.get("name") or ""), str(payload.get("verifier") or "")
        try:
            source = machinedb.address(payload.get("host"))
            reached = machinedb.reached_as([payload.get("reached_as")])[0]
        except machinedb.ClusterError as exc:
            return refuse(str(exc))
        if not machinedb.ENV_NAME.fullmatch(name) or not machinedb.VERIFIER.fullmatch(verifier):
            return refuse("A grant needs the name of its role and database and the SCRAM verifier of its password")
        by = me.on_behalf or me.name
        spec = {"name": name, "address": source, "verifier": verifier, "reached_as": reached, "link": me.name,
                "by": by, "workspace": str(payload.get("workspace") or "")[:64],
                "instance": str(payload.get("instance") or "")[:64]}

        async def action(job):
            # who asks and through which link are in the job's title and source and in the access log, not in
            # its lines: the machine that asked shows them in its own job, which its customers may read
            job.log(f"--- database {name} of the machine's PostgreSQL {major} for an instance of another machine: "
                    f"let in from {source} only, over TLS")
            made = await workspaceops._helper(job, "machine-db-grant", major, timeout=MACHINE_TIMEOUT,
                                              input=json.dumps(spec).encode())
            if made.get("restarted_s") is not None:
                job.log(f"The cluster listens on port {made.get('port')} now: its container was made again in "
                        f"{made.get('restarted_s')} s. Its instances lost their connections: each may answer an error "
                        "to its first request or two, then goes on")
            job.log(f"Role and database {made.get('role')} are there; pg_hba.conf lets {source} in as that role, "
                    "to that database (and to postgres, where Odoo looks for it) only, over TLS")
            job.log(firewall_line(made))
            job.result = {**job.result, "grant": {k: made.get(k) for k in (
                "role", "database", "port", "address", "ca", "names", "restarted_s", "serves", "version", "created")}}
        job = jobs.submit(f"Database {name} of the machine's PostgreSQL {major} for {me.name}",
                          [f"{MACHINE_KEY}/{major}"], f"manual: {me}", action)
        return {"job": job.id}

    @app.post("/api/postgres/machine/{major}/grants/{name}/{what}")
    async def postgres_ungrant(major: str, name: str, what: str, me: Principal = Depends(current_user)):
        """A database of another machine on the cluster: revoke (that machine
        no longer comes in, the database stays), allow (again), drop (the
        database and its role, as an instance of this machine removed)."""
        postgres_admin(me)
        if what not in ("revoke", "allow", "drop") or not machinedb.ENV_NAME.fullmatch(name):
            return refuse("revoke, allow or drop a database of another machine", status.HTTP_404_NOT_FOUND)
        if known_cluster(major) is None:
            return refuse(f"No cluster of PostgreSQL {major} on this machine", status.HTTP_404_NOT_FOUND)
        state = await cluster_state(major)
        if state is None:
            # whose it is cannot be told: nothing is touched
            return refuse(f"The machine's PostgreSQL {major} cannot be read now: try again in a moment",
                          status.HTTP_503_SERVICE_UNAVAILABLE)
        grant = (state.get("grants") or {}).get(name)
        if grant is None:
            if what == "drop":
                # asked again, or dropped here already: nothing of it is left
                return {"ok": True, "gone": True}
            return refuse(f"{name} is no database of another machine on PostgreSQL {major}", status.HTTP_404_NOT_FOUND)
        whose = str(grant.get("link") or "another machine")
        if a_machine(me) and whose != me.name:
            # a machine takes back what it asked for, never a database of the same name of another's
            return refuse(f"{name} is a database of another machine here, not of {me.name}: it is not touched",
                          status.HTTP_403_FORBIDDEN)

        async def action(job):
            done = await workspaceops._helper(job, "machine-db-ungrant", major, name, what, timeout=MACHINE_TIMEOUT)
            source = done.get("address")
            # whose it is stays in the title: the lines of a drop are read on the machine that asked it too
            if what == "revoke":
                job.log(f"Database {name} is no longer reached from {source}: its line of pg_hba.conf is gone, "
                        f"{done.get('closed')} connections closed. The database and its role stay: Allow again, "
                        "or Drop")
            elif what == "allow":
                job.log(f"Database {name} is reached from {source} again: its line of pg_hba.conf is back")
            else:
                job.log(f"Database {name} dropped, with its role and its line of pg_hba.conf")
            job.log(firewall_line(done))
            if what == "drop":
                if not done.get("grants_left") and done.get("port"):
                    job.log(f"The cluster still listens on port {done.get('port')}, closed to every address: "
                            "Close the port, in the Database tab, makes it socket only again")
            job.result = {**job.result, "ungrant": {**done, "what": what}}
        title = {"revoke": f"Revoke database {name} of {whose} on the machine's PostgreSQL {major}",
                 "allow": f"Allow database {name} of {whose} on the machine's PostgreSQL {major} again",
                 "drop": f"Drop database {name} of {whose} on the machine's PostgreSQL {major}"}[what]
        job = jobs.submit(title, [f"{MACHINE_KEY}/{major}"], f"manual: {me}", action)
        return {"job": job.id}

    @app.post("/api/postgres/machine/{major}/port/close")
    async def postgres_port_close(major: str, me: Principal = Depends(current_user)):
        """The cluster back to its socket only, when no other machine is let in.
        Of the dedicated database of an instance (<major> its key): the port
        it listened on for a read replica it no longer has closed again, its
        PostgreSQL as its compose.yaml says it: its container is made again
        (the seconds it does not answer); refused while a replica follows it."""
        postgres_admin(me)
        target = dedicated_instance(major)
        if target is not None:
            ws, _instance, inst = target
            if dedicated_key(inst) != major or on_remote_db(inst):
                return refuse(f"{inst.server} has no database of its own here: there is no port to close")
            followed = replica_record(major)
            if followed is not None:
                return refuse(f"The PostgreSQL of {inst.server} has a read replica on {followed.get('link')}, which follows "
                              "it through this port: drop the replica first")
            port = (machinedb.listening(dedicated_folder(inst)) or {}).get("port")
            if not port:
                return refuse(f"The PostgreSQL of {inst.server} listens on no port of this machine")
            if server_busy(inst.server):
                return refuse(f"{inst.server} is busy: wait for its job")

            async def closing(job):
                job.log(f"--- the port {port} of the PostgreSQL of {inst.server} is closed: it listened over TLS for a read "
                        "replica it no longer has")
                async with jobs.lock(inst.key):
                    done = await workspaceops._helper(job, "machine-db-replica-unlisten", major, timeout=MACHINE_TIMEOUT)
                seconds = done.get("restarted_s")
                job.log(f"The port {done.get('closed')} is closed: the PostgreSQL of {inst.server} is as its compose.yaml "
                        f"says it again, reached from its own project only; its container was made again in {seconds} s. "
                        f"{inst.server} lost its connections: it may answer an error to its first request or two, then "
                        "goes on" if seconds is not None else
                        f"The port {done.get('closed')} is closed: the PostgreSQL of {inst.server} is as its compose.yaml "
                        "says it again. Its container was not running: nothing restarted")
                return_seen.pop(major, None)
                job.result = {**job.result, "port_closed": {"port": done.get("closed"), "restarted_s": seconds}}
                if seconds is not None:
                    # no machine, no port: the workspace's users read it
                    note = customer_note(inst, ws, f"The PostgreSQL of {inst.server} was restarted once by the provider", [
                        "The provider closed the encrypted connection the database of this instance kept open for a "
                        f"copy on another machine, which is no longer kept: its PostgreSQL was restarted once ({seconds} "
                        "s), and Odoo lost its connections for those seconds"], job.source)
                    job.log(f"The workspace's users read of it in the job {note.id} of {inst.server}, which names no "
                            "machine")
            job = jobs.submit(f"Close the port of the PostgreSQL of {inst.server}", [f"{MACHINE_KEY}/{major}"],
                              f"manual: {me}", closing)
            return {"job": job.id}
        if known_cluster(major) is None:
            return refuse(f"No cluster of PostgreSQL {major} on this machine", status.HTTP_404_NOT_FOUND)

        async def action(job):
            done = await workspaceops._helper(job, "machine-db-port", major, "close", timeout=MACHINE_TIMEOUT)
            job.log(f"The port {done.get('closed')} is closed: the machine's PostgreSQL {major} is reached by its "
                    f"socket only again; its container was made again in {done.get('restarted_s')} s. Its instances "
                    "lost their connections: each may answer an error to its first request or two, then goes on")
        job = jobs.submit(f"Close the port of the machine's PostgreSQL {major}", [f"{MACHINE_KEY}/{major}"],
                          f"manual: {me}", action)
        return {"job": job.id}

    # --- recovery at a minute of the machine's cluster (AD-006, clusterpitr.py): its archive switched on
    # and off and its full copies, by the machine's admins only. A staging at a minute of an instance on
    # it is asked from the instance's page (pitr_restore), by who may ask it on a dedicated database.

    @app.post("/api/postgres/machine/{major}/archive/{what}")
    async def postgres_archive(major: str, what: str, payload: Optional[dict] = Body(None),
                               me: Principal = Depends(current_user)):
        """on: the cluster archives its changes and makes its full copies
        (payload budget_gb, keep, reserve_gb; restart: true once whoever asks
        was told the cluster restarts, which it does only when it does not
        archive yet); on again: the amounts only. off: nothing more is
        archived, the copies and the archive deleted, no restart. base: a
        full copy now."""
        postgres_admin(me)
        if what not in ("on", "off", "base"):
            return refuse("on, off or base", status.HTTP_404_NOT_FOUND)
        if known_cluster(major) is None:
            return refuse(f"No cluster of PostgreSQL {major} on this machine", status.HTTP_404_NOT_FOUND)
        payload = payload if isinstance(payload, dict) else {}
        key = f"{MACHINE_KEY}/{major}"
        if what == "base":
            job = jobs.submit(f"Full copy of the machine's PostgreSQL {major} for the recovery at a minute", [key],
                              f"manual: {me}", lambda job: cluster_base_job(job, major))
            return {"job": job.id}
        if what == "off":
            async def off(job):
                done = await workspaceops._helper(job, "machine-db-archive-off", major, timeout=MACHINE_TIMEOUT)
                job.log(f"Recovery at a minute is off for the machine's PostgreSQL {major}: nothing more is recorded "
                        f"from now, without a restart. The full copies and the record of the changes are deleted: "
                        f"{done.get('freed', 0) >> 20} MiB freed. No minute of before can be reached any more")
            job = jobs.submit(f"Recovery at a minute off for the machine's PostgreSQL {major}", [key],
                              f"manual: {me}", off)
            return {"job": job.id}
        try:
            budget, keep, reserve = clusterpitr.amounts(payload.get("budget_gb", clusterpitr.BUDGET_GB),
                                                        payload.get("keep", clusterpitr.KEEP_BASES),
                                                        payload.get("reserve_gb", clusterpitr.RESERVE_GB))
        except machinedb.ClusterError as exc:
            return refuse(str(exc))
        try:
            found = await workspaceops.ask("machine-db-archive-state", major, timeout=120)
        except JobFailed as exc:
            return refuse(str(exc), status.HTTP_502_BAD_GATEWAY)
        restarts = found.get("archive_mode") != "on"
        served = machine_served()
        if restarts and not payload.get("restart"):
            # said and asked before: every instance on the cluster loses the database for the restart
            return refuse(f"The machine's PostgreSQL {major} restarts once to start recording the changes: "
                          f"{len(served)} instances lose the database for a few seconds. Confirm the restart",
                          status.HTTP_409_CONFLICT)

        async def on(job):
            since = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            done = await workspaceops._helper(job, "machine-db-archive-on", major, f"{budget:g}", str(keep),
                                              f"{reserve:g}", "restart" if restarts else "no-restart",
                                              timeout=PITR_RESTORE_TIMEOUT)
            if done.get("changed") == "amounts":
                job.log(f"The recovery at a minute of the machine's PostgreSQL {major} keeps {keep} full copies, "
                        f"warns above {budget:g} GB and leaves {reserve:g} GB free on the disk at a recovery. "
                        "Nothing was restarted")
                return
            if done.get("restarted_s") is not None:
                job.log(f"The machine's PostgreSQL {major} restarted in {done.get('restarted_s')} s to start recording "
                        f"the changes: its {done.get('serves')} instances and the {done.get('grants')} databases of "
                        "other machines lost their connections; each Odoo opens them again at its next request")
                for one in served:
                    odoo = getattr(one.options, "odoo_service", "odoo")
                    code, log = await ops.capture(one.restart.command("logs", odoo, "--since", since), timeout=60)
                    if code == 0:
                        errors = sum(1 for line in log.splitlines() if ODOO_DB_ERRORS.search(line))
                        job.log(f"Odoo log lines with a database error since the restart ({one.key}): {errors}")
            else:
                job.log(f"The machine's PostgreSQL {major} records the changes from now: it was not restarted")
            job.log(f"First full copy {done.get('base')}: {done.get('size', 0) >> 20} MiB in "
                    f"{done.get('base_seconds')} s. From now every instance on it can go back to a minute; "
                    "a full copy is made every night")
        title = (f"Recovery at a minute on for the machine's PostgreSQL {major}" if not found.get("enabled")
                 else f"Recovery at a minute of the machine's PostgreSQL {major}: full copies kept, budget, room")
        job = jobs.submit(title, [key], f"manual: {me}", on)
        return {"job": job.id}

    # --- a read replica of the machine's cluster on another machine, made and run by OmniaCLI
    # (AD-004 option A, UX-010; managedreplica.py). P has the cluster, R hosts the replica and has a
    # link to P: everything is asked from R through that link, as a database on a linked cluster
    # is; what P has to say to R (dropped, replaced) R reads at its next look (replica_loop).
    # P's side: facts, allow, ready, state, cap, drop, replaced, follow, and an instance's reads
    # (POST /api/servers/<server>/database/replica). R's side: GET /api/links/<name>/postgres/replica,
    # POST /api/postgres/replica/make, …/<major>/drop, …/<major>/promote.
    # The PostgreSQL is the machine's cluster, or the dedicated database of the production of one
    # instance (UX-011 §D): <major> in P's routes is then <workspace>.<storage>.<instance>
    # (managedreplica.Dedicated), and in R's d<n>, the key of the copy hosted here. P's jobs of a
    # dedicated database are the machine's (no user of the workspace reads the other machine's name,
    # address or port); what the customer reads of them is a job of its own instance, written
    # without names (customer_note, D11-2).

    REPLICA_EVERY = 20
    REPLICA_RETRY = 10
    REPLICA_ASK_TIMEOUT = 2.5
    REPLICA_ANSWER_WAIT = 60
    replica_tasks: list[asyncio.Task] = []
    # R: what the primary's machine said last, by major; P: what this machine saw last of its
    # replica, and when the machine that hosts it asked last
    replica_seen: dict[str, dict] = {}
    replica_own: dict[str, dict] = {}
    replica_heard: dict[str, str] = {}
    replica_synced: dict[str, tuple] = {}
    # R: a promotion the primary's machine has not heard of yet (it did not answer): tried again
    replica_fence_file = settings.jobs_dir.parent / "replica_fence.json"

    def replica_now() -> str:
        return datetime.datetime.now().isoformat(timespec="seconds")

    def replica_key(major: str) -> str:
        return f"{MACHINE_KEY}/replica-{major}"

    def dedicated_instance(key: str) -> tuple[Workspace, str, Instance] | None:
        """(workspace, instance name there, production) of the instance whose
        dedicated database key names (<workspace>.<storage>.<instance>), None
        when there is none here."""
        found = machinedb.DEDICATED_KEY.fullmatch(str(key or ""))
        if not found:
            return None
        ws = settings.workspaces.get(found.group(1))
        if ws is None or ws.storage != found.group(2):
            return None
        srv = settings.servers.get(f"{ws.name}-{found.group(3)}")
        if srv is None or srv.production is None or srv.name not in ws.servers or srv.production.restart.type != "compose":
            return None
        return ws, found.group(3), srv.production

    def dedicated_key(inst: Instance) -> str:
        """The key of the dedicated database of a workspace's production (as
        managedreplica names it), "" for any other instance. One whose
        database was replaced by its promoted copy keeps its key."""
        if inst.role != "production" or inst.restart.type != "compose" or on_machine_db(inst):
            return ""
        found = workspace_of(inst.server)
        if found is None:
            return ""
        if on_remote_db(inst) and not (instance_meta(inst).get("promoted_from") or {}).get("dedicated"):
            return ""
        return managedreplica.dedicated_key(found[0].name, found[0].storage, found[1])

    def dedicated_folder(inst: Instance) -> Path:
        """Where the project of an instance keeps the record of its database's copy."""
        return Path(inst.restart.service) / machinedb.DEDICATED_FOLDER

    def pg_known(key: str) -> bool:
        """key names a PostgreSQL of this machine: its cluster, or the dedicated database of an instance."""
        return known_cluster(key) is not None or dedicated_instance(key) is not None

    def pg_missing(key: str):
        if managedreplica.is_dedicated(key):
            return refuse(f"No instance with a database of its own {key} on this machine", status.HTTP_404_NOT_FOUND)
        return refuse(f"No cluster of PostgreSQL {key} on this machine", status.HTTP_404_NOT_FOUND)

    def pg_what(key: str) -> str:
        """How a job names the PostgreSQL of key."""
        found = dedicated_instance(key)
        return f"the PostgreSQL of {found[2].server}" if found else f"the machine's PostgreSQL {key}"

    def customer_note(inst: Instance, ws: Workspace, title: str, lines: list[str], asked: str) -> Job:
        """What the workspace's users read of something the machine's admins
        did to the database of their instance (D11-2): a job of the instance
        with its lines, which name no machine, no address and no size. asked:
        who started the machine's job (a link, one of the machine's people): kept
        out of it, the workspace's access log would show the name."""
        async def action(job):
            for line in lines:
                job.log(line)
        return jobs.submit(title, [inst.key], "system: the provider", action, workspace=ws.name)

    def replica_readers(major: str = "") -> list[Instance]:
        """The instances of this machine that read from the replica of major:
        those on its cluster, or the one whose dedicated database it is."""
        found = dedicated_instance(major)
        if found is not None:
            return [found[2]] if remote_db_host(found[2], workspacehelper.REPLICA_META) else []
        return [i for i in machine_served() if remote_db_host(i, workspacehelper.REPLICA_META)]

    def replica_record(major: str) -> dict | None:
        """The PostgreSQL's own record of its replica (a file OmniaCLI reads: no PostgreSQL asked)."""
        found = machinedb.registry().get(major)
        if found:
            return machinedb.replica(Path(str(found.get("project") or "")))
        target = dedicated_instance(major)
        return machinedb.replica(dedicated_folder(target[2])) if target else None

    def replica_sources() -> list[str]:
        """The PostgreSQL of this machine that may have a replica: its clusters, and the dedicated
        databases that listen for one (root's records)."""
        return [*machinedb.registry(), *(k for k in machinedb.dedicated_sources() if dedicated_instance(k) is not None)]

    async def dedicated_pitr(inst: Instance, ws: Workspace, instance: str) -> dict | None:
        """The recovery at a minute of an instance with a database of its own, when it is on (the
        application's own record: PostgreSQL is not asked at every look): a promotion stops it."""
        return {"enabled": True} if inst.server in await asyncio.to_thread(deployhooks.load, pitr_file) else None

    async def replica_state_here(major: str) -> dict:
        """What this machine sees of the replica of its cluster, with what the
        rows say of it, the instances that read from it and when the machine
        that hosts it asked last; kept for the pages that do not read PostgreSQL."""
        found = await workspaceops.ask("machine-db-replica-state", major, timeout=pgtuning.READ_TIMEOUT * 4)
        if not found.get("exists"):
            replica_own.pop(major, None)
            return found
        found["verdict"] = managedreplica.verdict(found)
        found["readers"] = sorted(i.server for i in replica_readers(major))
        found["heard"] = replica_heard.get(major)
        target = dedicated_instance(major)
        if target is not None:
            # of one instance: its name, whether its recovery at a minute is on (a promotion stops it)
            found["name"], found["workspace"] = target[2].server, target[0].name
            found["pitr"] = await dedicated_pitr(target[2], target[0], target[1])
        replica_own[major] = {"at": replica_now(), "state": found}
        return found

    def replica_fences() -> dict:
        try:
            data = json.loads(replica_fence_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def replica_fence_set(major: str, value: dict | None) -> None:
        data = replica_fences()
        if value is None:
            data.pop(major, None)
        else:
            data[major] = value
        if data:
            replica_fence_file.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        else:
            replica_fence_file.unlink(missing_ok=True)

    def replica_refusal(link: LinkTarget, resp: httpx.Response) -> str:
        """Why the machine of the link did not take a request about a replica."""
        if resp.status_code == 401:
            return f"{link.name} refused the link's token: ask it for a new one"
        if resp.status_code == 403:
            return f"{link.name} refused: a replica of its cluster needs the admin role there, for the link and for you"
        try:
            said = resp.json().get("error") or resp.json().get("detail") or ""
        except (ValueError, AttributeError):
            said = ""
        if resp.status_code in (404, 405) and (not said or said == "Not Found"):
            return f"OmniaCLI on {link.name} is older: update it to make a replica"
        return f"{link.name}: {said or f'HTTP {resp.status_code}'}"

    # --- P: the machine that has the cluster

    @app.get("/api/postgres/machine/{major}/replica/facts")
    async def replica_facts(major: str, me: Principal = Depends(current_user)):
        """What the machine that will host a replica of this cluster says
        before it is made: the size of the whole cluster, whose data it
        holds, whether the cluster restarts to open its port."""
        postgres_admin(me)
        if not pg_known(major):
            return pg_missing(major)
        target = dedicated_instance(major)
        if target is not None and dedicated_key(target[2]) != major:
            return refuse(f"{target[2].server} has no database of its own: only a dedicated one has a replica of its own")
        if target is not None and target[2].server in await asyncio.to_thread(stagings.load, stagings_file):
            return refuse(f"{target[2].server} is a staging: only a production has a replica of its own")
        try:
            found = await workspaceops.ask("machine-db-replica-facts", major, timeout=pgtuning.READ_TIMEOUT * 4)
        except JobFailed as exc:
            return refuse(str(exc), status.HTTP_502_BAD_GATEWAY)
        if target is not None:
            found["name"] = target[2].server
            found["pitr"] = bool(await dedicated_pitr(target[2], target[0], target[1]))
        return {"machine": here_name(), **found}

    @app.get("/api/postgres/replica/sources")
    async def replica_sources_offer(me: Principal = Depends(current_user)):
        """What a linked OmniaCLI may make a replica of here (UX-011 §D): the
        machine's cluster, and the PostgreSQL of each production with a
        database of its own; never a staging nor a test. Each with its size,
        the instances it serves and the replica it has already."""
        postgres_admin(me)
        copies = await asyncio.to_thread(stagings.load, stagings_file)
        keys = list(machinedb.registry())
        for srv in settings.active_servers():
            inst = srv.production
            if inst is None or srv.name in copies or on_remote_db(inst):
                continue
            key = dedicated_key(inst)
            if key:
                keys.append(key)

        async def one(key: str) -> dict:
            target = dedicated_instance(key)
            try:
                found = await workspaceops.ask("machine-db-replica-facts", key, timeout=pgtuning.READ_TIMEOUT * 4)
            except JobFailed as exc:
                found = {"why": str(exc), "size": 0, "instances": []}
            return {"key": key, "kind": "dedicated" if target else "cluster", "major": found.get("major") or ("" if target else key),
                    "name": target[2].server if target else "", "workspace": target[0].name if target else "",
                    "size": int(found.get("size") or 0), "instances": found.get("instances") or [],
                    "replica": (found.get("replica") or {}).get("link") if found.get("replica") else None,
                    "why": found.get("why") or ""}
        return {"machine": here_name(), "sources": list(await asyncio.gather(*(one(k) for k in keys)))}

    @app.post("/api/postgres/machine/{major}/replica")
    async def replica_allow(major: str, payload: dict = Body(...), me: Principal = Depends(current_user)):
        """The machine that asks through its link let in to copy this cluster
        and follow it: a job of the root helper (machine-db-replica-allow).
        Its result carries the port and the CA: no secret (the password of
        the replication stays on that machine: only its verifier comes)."""
        postgres_admin(me)
        if not a_machine(me):
            return refuse("A read replica is asked by the machine that hosts it, through its link",
                          status.HTTP_403_FORBIDDEN)
        if not pg_known(major):
            return pg_missing(major)
        target = dedicated_instance(major)
        if target is not None and (dedicated_key(target[2]) != major or on_remote_db(target[2])
                                   or target[2].server in await asyncio.to_thread(stagings.load, stagings_file)):
            return refuse(f"{target[2].server} is not a production with a database of its own: only one has a replica "
                          "of its own")
        verifier = str(payload.get("verifier") or "")
        try:
            source = machinedb.address(payload.get("host"))
            reached = machinedb.reached_as([payload.get("reached_as")])[0]
            # of a dedicated database the root helper says the most (a tenth of its workspace's disk)
            cap = (managedreplica.cap_gb(payload.get("cap_gb", managedreplica.CAP_GB))
                   if target is None or payload.get("cap_gb") not in (None, "") else None)
        except machinedb.ClusterError as exc:
            return refuse(str(exc))
        if not machinedb.VERIFIER.fullmatch(verifier):
            return refuse("A replica needs the SCRAM verifier of the password of its role")
        spec = {"address": source, "verifier": verifier, "reached_as": reached, "link": me.name,
                "by": me.on_behalf or me.name, "cap_gb": cap, "restart": bool(payload.get("restart"))}
        if target is not None:
            return {"job": replica_allow_dedicated(major, target, spec, me).id}

        async def action(job):
            job.log(f"--- a read replica of the machine's PostgreSQL {major} on another machine: let in from "
                    f"{source} only, over TLS, for the replication")
            made = await workspaceops._helper(job, "machine-db-replica-allow", major, timeout=MACHINE_TIMEOUT,
                                              input=json.dumps(spec).encode())
            if made.get("restarted_s") is not None:
                job.log(f"The cluster listens on port {made.get('port')} now: its container was made again in "
                        f"{made.get('restarted_s')} s. Its instances lost their connections: each may answer an error "
                        "to its first request or two, then goes on")
            job.log(f"A role for the replication and a place that holds the changes for the replica are there: the "
                    f"cluster keeps {cap:g} GB of them at most, beyond which the replica is made again; pg_hba.conf "
                    f"lets {source} in for the replication only, over TLS")
            job.log(firewall_line(made))
            if (return_record(major) or {}).get("state") == "done":
                # the copy this cluster came back from is no longer there: nothing more to say of it
                with contextlib.suppress(JobFailed):
                    await workspaceops.ask("machine-db-return", major, "forget", timeout=60)
            job.result = {**job.result, "replica": {k: made.get(k) for k in (
                "port", "ca", "names", "restarted_s", "roots", "size", "environments", "tablespaces", "serves",
                "grants", "version", "cap_gb", "address")}}
        job = jobs.submit(f"Read replica of the machine's PostgreSQL {major} for {me.name}",
                          [f"{MACHINE_KEY}/{major}"], f"manual: {me}", action)
        return {"job": job.id}

    def replica_allow_dedicated(key: str, target: tuple, spec: dict, me: Principal) -> Job:
        """The job that lets the machine of a link in to copy the dedicated
        database of one instance (UX-011 §D): a job of the machine, the
        instance held while its PostgreSQL restarts; then the customer's own
        note of it (D11-2)."""
        ws, _instance, inst = target
        source = spec["address"]

        async def action(job):
            job.log(f"--- a read replica of the PostgreSQL of {inst.server} on another machine: let in from "
                    f"{source} only, over TLS, for the replication")
            async with jobs.lock(inst.key):
                made = await workspaceops._helper(job, "machine-db-replica-allow", key, timeout=MACHINE_TIMEOUT,
                                                  input=json.dumps(spec).encode())
            if made.get("restarted_s") is not None:
                job.log(f"The PostgreSQL of {inst.server} listens on port {made.get('port')} of this machine now, over "
                        f"TLS: its container was made again in {made.get('restarted_s')} s, by an override beside "
                        f"its compose.yaml, which is not changed. {inst.server} lost its connections: it may answer "
                        "an error to its first request or two, then goes on")
            job.log(f"A role for the replication and a place that holds the changes for the replica are there: the "
                    f"database keeps {made.get('cap_gb'):g} GB of them at most, on the disk of the workspace {ws.name}, "
                    f"beyond which the replica is made again; pg_hba.conf lets {source} in for the replication only, "
                    "over TLS")
            allowed = ", ".join(made.get("firewall") or [])
            job.log(f"The firewall lets that port be reached from {allowed} only" if allowed
                    else "The firewall lets that port be reached from no address")
            if (return_record(key) or {}).get("state") == "done":
                # the copy this database came back from is no longer there: nothing more to say of it
                with contextlib.suppress(JobFailed):
                    await workspaceops.ask("machine-db-return", key, "forget", timeout=60)
            job.result = {**job.result, "replica": {k: made.get(k) for k in (
                "port", "ca", "names", "restarted_s", "roots", "size", "environments", "tablespaces", "serves",
                "grants", "version", "cap_gb", "address", "kind", "major", "workspace", "name")}}
            # no machine, no address, no size: the workspace's users read it
            note = customer_note(inst, ws, f"Copy of the database of {inst.server} on another machine of the provider", [
                "A copy of the database of this instance is kept on another machine of the provider, read only and "
                "up to date at each change, over an encrypted connection: it is there to take its place if this "
                "machine fails",
                *([f"The PostgreSQL of this instance was restarted once to open that connection "
                   f"({made.get('restarted_s')} s): Odoo lost its connections for those seconds"]
                  if made.get("restarted_s") is not None else []),
                "What the database keeps for the copy while it is not connected is on this workspace's disk, a tenth "
                "of it at most"], job.source)
            job.log(f"The workspace's users read of it in the job {note.id} of {inst.server}, which names no machine")
        return jobs.submit(f"Read replica of the PostgreSQL of {inst.server} for {me.name}",
                           [f"{MACHINE_KEY}/{key}"], f"manual: {me}", action)

    def replica_of_link(major: str, me: Principal):
        """The record of the replica of this PostgreSQL, when me is the link
        of the machine that hosts it; else the refusal."""
        if not a_machine(me):
            return refuse("Asked by the machine that hosts the replica, through its link", status.HTTP_403_FORBIDDEN)
        if not pg_known(major):
            return pg_missing(major)
        rec = replica_record(major)
        if rec is None:
            return refuse(f"{pg_what(major)[0].upper()}{pg_what(major)[1:]} has no read replica", status.HTTP_404_NOT_FOUND)
        if rec.get("link") != me.name:
            return refuse(f"The read replica of this cluster is of another machine, not of {me.name}: it is not touched",
                          status.HTTP_403_FORBIDDEN)
        return rec

    @app.post("/api/postgres/machine/{major}/replica/ready")
    async def replica_ready(major: str, payload: dict = Body(...), me: Principal = Depends(current_user)):
        """The machine that hosts the replica says where it answers and the
        CA its certificate is verified against: what an instance here needs
        to read from it."""
        postgres_admin(me)
        rec = replica_of_link(major, me)
        if not isinstance(rec, dict):
            return rec
        spec = {k: payload.get(k) for k in ("host", "port", "ca", "seconds", "size")}
        try:
            done = await workspaceops.ask("machine-db-replica-ready", major, timeout=MACHINE_TIMEOUT,
                                          input=json.dumps(spec).encode())
        except JobFailed as exc:
            return refuse(str(exc))
        return {"ok": True, **done}

    @app.get("/api/postgres/machine/{major}/replica/state")
    async def replica_state(major: str, me: Principal = Depends(current_user)):
        """What this machine sees of the replica of its cluster: connected or
        since when not, the seconds behind, the disk it keeps for it. The
        machine that hosts it asks it at each look: it reads there too that
        the replica was dropped or took the cluster's place."""
        postgres_admin(me)
        if not pg_known(major):
            return pg_missing(major)
        try:
            found = await replica_state_here(major)
        except JobFailed as exc:
            return refuse(str(exc), status.HTTP_502_BAD_GATEWAY)
        if a_machine(me) and found.get("exists"):
            if found.get("link") != me.name:
                # another machine's: only that there is one, and whose
                return {"exists": True, "mine": False, "link": found.get("link"), "machine": here_name()}
            replica_heard[major] = found["heard"] = replica_now()
        return {**found, "mine": True, "machine": here_name()}

    @app.post("/api/postgres/machine/{major}/replica/cap")
    async def replica_cap(major: str, payload: dict = Body(...), me: Principal = Depends(current_user)):
        """The changes the cluster keeps for its replica at most, in GB."""
        postgres_admin(me)
        if not pg_known(major) or replica_record(major) is None:
            return refuse(f"{pg_what(major)[0].upper()}{pg_what(major)[1:]} has no read replica", status.HTTP_404_NOT_FOUND)
        try:
            cap = managedreplica.cap_gb(payload.get("cap_gb"))
        except machinedb.ClusterError as exc:
            return refuse(str(exc))
        what = pg_what(major)
        # of a dedicated database, on the customer's disk: refused here above its most, as the root helper would
        most = ((replica_own.get(major) or {}).get("state") or {}).get("cap_max")
        if most and cap > float(most):
            return refuse(f"What {what} keeps for its replica is on the disk of its workspace: {float(most):g} GB at "
                          "most, a tenth of that disk")

        async def action(job):
            await workspaceops._helper(job, "machine-db-replica-cap", major, f"{cap:g}", timeout=MACHINE_TIMEOUT)
            job.log(f"{what[0].upper()}{what[1:]} keeps {cap:g} GB of changes for its read replica at most: "
                    "beyond it the replica loses its place and is made again. Nothing restarted")
        job = jobs.submit(f"Changes kept for the read replica of {what}: {cap:g} GB at most",
                          [f"{MACHINE_KEY}/{major}"], f"manual: {me}", action)
        return {"job": job.id}

    def replica_read_job(inst: Instance, ws: Workspace, instance: str, on: bool, source: str) -> Job:
        """The job that makes an instance of the cluster read from the
        cluster's replica, or from its cluster only: its files written by the
        root helper, then its Odoo restarted. Its texts name no machine and no
        address: the workspace's users read it."""
        async def action(job):
            await workspaceops._helper(job, "machine-db-replica-read", ws.name, ws.storage, instance,
                                       "on" if on else "off", timeout=MACHINE_TIMEOUT)
            job.log("Odoo sends the reads of the pages that do not write to a replica of its database, over TLS "
                    "with the certificate verified, and keeps writing to its database: it restarts to take it"
                    if on else "Odoo reads and writes on its database only: it restarts to take it")
            await ops.restart(job, inst)
            job.result = {**job.result, "server": inst.server, "reads_replica": on}
        title = f"Reads of {inst.server}: also from a replica" if on else f"Reads of {inst.server}: from its database only"
        return jobs.submit(title, [inst.key], source, action, workspace=ws.name)

    async def replica_wait(job: Job, other: Job, timeout: float = 900) -> bool:
        end = time.monotonic() + timeout
        while not other.finished_flag:
            if time.monotonic() > end:
                return False
            await asyncio.sleep(0.5)
        return other.status == SUCCESS

    @app.post("/api/servers/{server}/database/replica")
    async def replica_read(server: str, payload: dict = Body(...), me: Principal = Depends(current_user)):
        """Read from the replica… / Stop reading from the replica… (UX-010
        §5.4): the production of an instance on the machine's cluster, or
        with a database of its own that has a replica (UX-011 §D), Odoo 18
        and later; a job, Odoo restarts."""
        if not machine_admin(me):
            return refuse("Only an admin of the machine chooses where an instance reads from", status.HTTP_403_FORBIDDEN)
        srv = settings.servers.get(server)
        found = workspace_of(server)
        if srv is None or srv.production is None or found is None:
            return refuse(f"{server} is not an instance of a workspace", status.HTTP_404_NOT_FOUND)
        ws, instance = found
        inst = srv.production
        on = bool((payload or {}).get("read"))
        offer = await asyncio.to_thread(replica_offer_of, inst)
        if offer is None:
            return refuse(f"{server} has its database on another machine: only an instance on the machine's cluster "
                          "or with a database of its own reads from its replica")
        if on == bool(offer["reads"]):
            return refuse(f"{server} reads from the replica already" if on else f"{server} does not read from a replica")
        if on:
            if not offer["exists"]:
                return refuse(f"The database of {server} has no read replica: it is made from the machine that hosts it"
                              if offer["dedicated"] else
                              "The machine's cluster has no read replica: it is made from the machine that hosts it")
            if offer["why"]:
                return refuse(offer["why"])
            try:
                state = await replica_state_here(offer["major"])
            except JobFailed as exc:
                return refuse(str(exc), status.HTTP_502_BAD_GATEWAY)
            code = (state.get("verdict") or {}).get("code")
            if code not in ("sync", "behind"):
                return refuse(replica_not_connected(state))
        if server_busy(server):
            return refuse(f"{server} is busy: wait for its job")
        job = replica_read_job(inst, ws, instance, on, f"manual: {me}")
        return {"job": job.id}

    def replica_not_connected(state: dict) -> str:
        since = str((state.get("verdict") or {}).get("since") or "")[:16].replace("T", " ")
        return (f"The replica on {state.get('link')} is not connected" + (f" since {since}" if since else "")
                + ": Odoo would read from the cluster anyway. Switch the reads on when it is in sync")

    def replica_offer_of(inst: Instance) -> dict | None:
        """What the instance's page says of its reads (UX-010 §5.4), for the
        machine's admins: whether it reads from the replica of its
        PostgreSQL (the machine's cluster, or its own dedicated database:
        UX-011 §D), on which machine and address, whether it may, why not;
        from the records and from what the loop saw last (PostgreSQL is not
        read). None for an instance whose database is on another machine."""
        if inst.role != "production" or inst.restart.type != "compose" or on_remote_db(inst):
            return None
        dedicated = not on_machine_db(inst)
        major = dedicated_key(inst) if dedicated else next(iter(machinedb.registry()), "")
        if dedicated and not major:
            return None
        rec = replica_record(major) if major else None
        reads = remote_db_host(inst, workspacehelper.REPLICA_META)
        version = workspacehelper.odoo_major(remote_db_host(inst, "template"))
        seen = replica_own.get(major) or {}
        verdict = ((seen.get("state") or {}).get("verdict") or {}) if rec else {}
        why = ""
        if version is not None and version < workspacehelper.ODOO_WITH_REPLICA:
            why = f"Odoo {version} cannot read from a replica: it can from {workspacehelper.ODOO_WITH_REPLICA} on (db_replica_host)."
        elif rec and rec.get("state") != "ready":
            why = "The replica is not a whole copy yet: the reads are switched on when it is in sync."
        elif rec and verdict.get("code") in ("off", "lost"):
            why = replica_not_connected({"link": rec.get("link"), "verdict": verdict}) + "."
        return {"major": major, "dedicated": dedicated,
                "exists": bool(rec) and rec.get("state") != "replaced", "reads": bool(reads),
                "machine": (rec or {}).get("link") or "", "address": (rec or {}).get("address") or "",
                "code": verdict.get("code") or "", "lag": verdict.get("lag"), "read_at": seen.get("at") or "",
                "why": why, "old": version is not None and version < workspacehelper.ODOO_WITH_REPLICA}

    @app.post("/api/postgres/machine/{major}/replica/drop")
    async def replica_drop_here(major: str, me: Principal = Depends(current_user)):
        """Drop the replica… on the machine that has the cluster: its
        instances that read from the replica go back to their cluster and
        restart, then what the cluster keeps for the replica goes (the role,
        the line, the place that holds its changes: their disk is freed). The
        copy is the other machine's: it deletes it at its next look, or it
        stays there and that is said."""
        postgres_admin(me)
        if not pg_known(major):
            return pg_missing(major)
        rec = replica_record(major)
        if rec is None:
            # asked again, or dropped here already
            return {"ok": True, "gone": True}
        what, target = pg_what(major), dedicated_instance(major)
        who = str(rec.get("link") or "another machine")
        if a_machine(me) and who != me.name:
            return refuse(f"The read replica of this cluster is of another machine, not of {me.name}: it is not touched",
                          status.HTTP_403_FORBIDDEN)
        if rec.get("state") == "replaced":
            return refuse("The replica took this cluster's place: it is not dropped from here")
        readers = replica_readers(major)

        async def action(job):
            for inst in readers:
                found = workspace_of(inst.server)
                if found is None:
                    continue
                other = replica_read_job(inst, found[0], found[1], False, job.source)
                job.log(f"{inst.server} goes back to reading from its cluster and restarts (job {other.id})")
                if not await replica_wait(job, other):
                    raise JobFailed(f"{inst.server} did not go back to its cluster ({other.error or other.status}): "
                                    "the replica is not dropped under an instance that reads from it")
            done = await workspaceops._helper(job, "machine-db-replica-drop", major, timeout=MACHINE_TIMEOUT)
            job.log(f"What {what} kept for its read replica is gone: the role of the "
                    f"replication, its line of pg_hba.conf, the place that held its changes "
                    f"({int(done.get('freed') or 0) >> 20} MB of them, freed)")
            if target is None:
                job.log(firewall_line(done))
            else:
                job.log("Its port stays open over TLS for the next replica, and nothing restarted: pg_hba.conf lets no "
                        "other machine in and the firewall lets no address reach it")
                customer_note(target[2], target[0], f"The copy of the database of {target[2].server} on another machine "
                              "of the provider is no longer kept", [
                                  "The copy of the database of this instance on another machine of the provider is no "
                                  "longer kept: what the database held for it on this workspace's disk is freed. "
                                  "Nothing restarted"], job.source)
            replica_own.pop(major, None)
            await monitor.machine_problem(replica_problem_id(major), "")
            if not a_machine(me):
                heard = replica_heard.get(major)
                try:
                    recent = heard and (datetime.datetime.now() - datetime.datetime.fromisoformat(heard)
                                        ).total_seconds() < 3 * REPLICA_EVERY
                except ValueError:
                    recent = False
                if recent:
                    job.log(f"{who} asks this machine every {REPLICA_EVERY} s: it deletes its copy at its next look")
                else:
                    # it has not asked for a minute: whether its copy is there still cannot be known from here
                    job.warn(f"{who} does not answer: its copy" + (f" ({int(rec.get('size') or 0) >> 20} MB)"
                                                                   if rec.get("size") else "")
                             + ", if it is still there, stays until its admin drops it, in its Database tab")
            job.result = {**job.result, "replica_dropped": {"freed": done.get("freed"), "readers": [i.server for i in readers]}}
        job = jobs.submit(f"Drop the read replica of {what} on {who}",
                          [f"{MACHINE_KEY}/{major}"], f"manual: {me}", action)
        return {"job": job.id}

    def replica_problem_id(major: str) -> str:
        """What the monitor keeps a replica's warning under: the cluster's, or one per dedicated database."""
        return f"replica-{major}" if managedreplica.is_dedicated(major) else "replica"

    @app.post("/api/postgres/machine/{major}/replica/replaced")
    async def replica_replaced(major: str, me: Principal = Depends(current_user)):
        """The first step of a promotion, asked by the machine that hosts the
        replica: this cluster is stopped, for good, before the replica takes
        the writes. Asked again, it answers that it is done."""
        postgres_admin(me)
        rec = replica_of_link(major, me)
        if not isinstance(rec, dict):
            return rec

        what, target = pg_what(major), dedicated_instance(major)

        async def action(job):
            job.log(f"--- the read replica takes the place of {what}: "
                    f"{'the cluster' if target is None else 'it'} is stopped first, so that nothing is written on both")
            if target is None:
                done = await workspaceops._helper(job, "machine-db-replica-replaced", major, timeout=MACHINE_TIMEOUT)
            else:
                # the instance held: no other job of it runs while its database goes
                async with jobs.lock(target[2].key):
                    done = await workspaceops._helper(job, "machine-db-replica-replaced", major, timeout=MACHINE_TIMEOUT)
            if done.get("already"):
                job.log(f"{what[0].upper()}{what[1:]} was stopped already, on {done.get('replaced_at')}")
            else:
                job.log(f"{what[0].upper()}{what[1:]} is stopped ({done.get('seconds')} s): its data is of this "
                        "moment, and it does not start again by itself")
            await monitor.machine_problem(replica_problem_id(major), "")
            job.result = {**job.result, "replaced": {
                "at": done.get("replaced_at"), "already": bool(done.get("already")), "grants": done.get("grants") or [],
                # who comes in to which database from where: the promoted copy lets the same in (no secret)
                "grant_lines": done.get("grant_lines"),
                "instances": [target[2].server] if target else sorted(i.server for i in machine_served())}}
        job = jobs.submit(f"Stop {what}: its read replica takes its place",
                          [f"{MACHINE_KEY}/{major}"], f"manual: {me}", action)
        return {"job": job.id}

    async def replica_answers(inst: Instance, timeout: float = REPLICA_ANSWER_WAIT) -> tuple[bool, float]:
        """(Odoo answers /web/login, the seconds waited)."""
        return await linkedcluster.odoo_answers(inst.plugin.web_port(inst), timeout)

    @app.post("/api/postgres/machine/{major}/replica/follow")
    async def replica_follow(major: str, me: Principal = Depends(current_user)):
        """The last step of a promotion, asked by the machine that hosts the
        replica once it took the writes: each instance of this machine on the
        stopped cluster takes the promoted copy as its database, over TLS,
        and starts again. Asked again, what is done already is left."""
        postgres_admin(me)
        rec = replica_of_link(major, me)
        if not isinstance(rec, dict):
            return rec
        if rec.get("state") != "replaced":
            return refuse("The machine's cluster is not stopped: its instances follow the replica only after it")
        target = dedicated_instance(major)
        if target is not None:
            return {"job": replica_follow_dedicated(major, target, me).id}
        # one after the other, the productions before their stagings: who works on them waits least
        copies = await asyncio.to_thread(stagings.load, stagings_file)
        served = sorted(machine_served(), key=lambda i: i.server in copies)

        async def action(job):
            # no machine and no address in these lines: the job is the machine's, but its words may be quoted
            job.log(f"--- the {len(served)} instances of the machine's PostgreSQL {major} take the promoted copy as "
                    "their database, over TLS with its certificate verified, and start again")
            outcome = await instances_pointed(job, served, "machine-db-replica-follow", "the promoted copy")
            await asyncio.to_thread(postgres_record, f"{MACHINE_KEY}/{major}", restart=None)
            job.result = {**job.result, "followed": outcome}
        job = jobs.submit(f"The instances of the machine's PostgreSQL {major} follow its promoted replica",
                          [f"{MACHINE_KEY}/{major}"], f"manual: {me}", action)
        return {"job": job.id}

    def replica_follow_dedicated(key: str, target: tuple, me: Principal) -> Job:
        """The last step of the promotion of the replica of one instance's
        dedicated database (UX-011 §D): the instance takes the promoted copy
        as its database and starts again; its recovery at a minute, which
        was of the database that stopped, is switched off. No other
        instance of this machine is touched. Then the customer's own note."""
        ws, _instance, inst = target

        async def action(job):
            job.log(f"--- {inst.server} takes the promoted copy of its database as its database, over TLS with its "
                    "certificate verified, and starts again; no other instance of this machine is touched")
            last = None
            if inst.server in await asyncio.to_thread(deployhooks.load, pitr_file):
                last = replica_now()
                await asyncio.to_thread(pitr_record, inst.server, enabled=False)
                job.log(f"Its recovery at a minute stops: it was of the database that is stopped, and its last point is "
                        f"of {last.replace('T', ' ')[:16]}")
            outcome = await instances_pointed(job, [inst], "machine-db-replica-follow", "the promoted copy")
            await asyncio.to_thread(postgres_record, inst.key, restart=None)
            job.result = {**job.result, "followed": outcome}
            answers = (outcome.get(inst.server) or {}).get("answers")
            customer_note(inst, ws, f"The database of {inst.server} is on another machine of the provider now", [
                "The provider moved the database of this instance to its copy on another machine: the database that "
                "was here is stopped, the copy takes the writes, and Odoo was pointed at it over an encrypted "
                "connection and restarted" + ("" if answers else ": it does not answer yet"),
                "Its backups are taken through the network from now",
                *([f"Its recovery at a minute stops: the last point is of {last.replace('T', ' ')[:16]}"] if last else []),
                "Its stagings stay on this machine and do not change"], job.source)
        return jobs.submit(f"{inst.server} follows the promoted replica of its database",
                           [f"{MACHINE_KEY}/{key}"], f"manual: {me}", action)

    # --- R: the machine that hosts the replica

    def replica_hosted() -> dict:
        return machinedb.hosted_replicas()

    def replica_links() -> list[dict]:
        """The links a replica may be made through, each with why not."""
        return [{"name": link.name, "why": (f"The link with {link.name} is plain HTTP: a replica asks for HTTPS or "
                                            "SSH, because what it answers says where the data comes from."
                                            if linkedcluster.unprotected(link) else "")}
                for link in settings.link_targets.values()]

    def replica_there(key: str, entry: dict | None = None) -> str:
        """How the machine of the link names the PostgreSQL the copy key
        hosted here is of: the major of its cluster, or the key of the
        instance whose dedicated database it is."""
        entry = replica_hosted().get(key) if entry is None else entry
        return str((entry or {}).get("source") or key)

    def replica_title(key: str, entry: dict | None = None) -> str:
        """How a job names the copy key hosted here."""
        entry = replica_hosted().get(key) if entry is None else entry
        name, link = str((entry or {}).get("name") or ""), str((entry or {}).get("link") or "")
        return f"the PostgreSQL of {name} of {link}" if name else f"PostgreSQL {key} of {link}"

    def replica_offer() -> dict:
        """What + Replica of another machine… needs: the links, each with why
        not; the copy of a cluster hosted already (one per machine) and the
        copies of dedicated databases (one per database: UX-011 §D); why
        none can be made here."""
        hosted = replica_hosted()
        why = ("no storage for workspaces on this machine" if not settings.storage_roots
               else workspaceops.docker_missing() or "")
        return {"links": replica_links(),
                "hosted": next((str(v.get("link") or "") for k, v in hosted.items() if not v.get("source")), ""),
                "hosted_dedicated": [{"link": str(v.get("link") or ""), "source": str(v.get("source") or ""),
                                      "name": str(v.get("name") or "")} for v in hosted.values() if v.get("source")],
                "storage": next(iter(settings.storage_roots), ""), "why": why,
                "cap_gb": managedreplica.CAP_GB, "reserve_gb": managedreplica.RESERVE_GB}

    async def replica_ask(link: LinkTarget, major: str, who: str, role: str, timeout: float) -> dict:
        """The state of the replica as the machine of the link sees it; {"error"} when it does not say."""
        return await replica_get(link, f"/api/postgres/machine/{major}/replica/state", who, role, timeout)

    async def replica_get(link: LinkTarget, path: str, who: str, role: str, timeout: float) -> dict:
        """What the machine of the link answers to a GET about a replica or a return; {"error"} when it does not say."""
        try:
            resp = await link_clients.client(link).get(
                path, timeout=timeout, headers={**link_headers(link, who, role), "Accept": "application/json"})
        except httpx.HTTPError as exc:
            return {"error": describe_error(exc)}
        if resp.status_code != 200:
            return {"error": replica_refusal(link, resp), "status": resp.status_code}
        try:
            data = resp.json()
        except ValueError:
            return {"error": "an answer that is not JSON"}
        return data if isinstance(data, dict) else {"error": "an answer that is not an object"}

    def replica_orphan(entry: dict, here: dict, there: dict) -> bool:
        """The promoted copy of an instance's dedicated database whose
        instance is no longer on its machine (that machine answers that it
        has none of that name): no one uses it, and it may be dropped."""
        return bool(entry.get("source")) and here.get("state") == "promoted" and there.get("status") == 404 \
            and "older" not in str(there.get("error") or "")

    async def replica_rows(me: Principal, lang: str = "en") -> list[dict]:
        """The replicas this machine hosts, one row each: what this machine
        sees (the root helper) and what the primary's machine sees (through
        the link, within REPLICA_ASK_TIMEOUT: one that does not answer is the
        reason in the row, with what it said last)."""
        rows = []
        for major, entry in sorted(replica_hosted().items()):
            name = str(entry.get("link") or "")
            link = settings.link_targets.get(name)

            async def local():
                try:
                    return await workspaceops.ask("machine-db-replica-hosted", major, timeout=pgtuning.READ_TIMEOUT * 4)
                except JobFailed as exc:
                    return {"error": i18n.tr(lang, str(exc))}

            async def remote():
                if link is None:
                    return {"error": f"the link {name} is no longer here"}
                return await replica_ask(link, replica_there(major, entry), me.on_behalf or me.name, me.role,
                                         REPLICA_ASK_TIMEOUT)
            here, there = await asyncio.gather(local(), remote())
            before = replica_seen.get(major) or {}
            if "error" not in there:
                replica_seen[major] = {"at": replica_now(), "state": there, "error": ""}
            else:
                replica_seen[major] = {**before, "error": there["error"], "failed": before.get("failed") or replica_now()}
            seen = replica_seen[major]
            rows.append({"key": replica_key(major), "kind": "replica", "where": "replica", "major": major,
                         # of the dedicated database of one instance (UX-011 §D): whose, and its PostgreSQL
                         "instance": str(entry.get("name") or ""), "workspace": str(entry.get("workspace") or ""),
                         "source": str(entry.get("source") or ""),
                         "pg": str(entry.get("major") or major), "orphan": replica_orphan(entry, here, there),
                         "link": name, "storage": entry.get("storage") or "", "here": here,
                         "primary": seen.get("state") if "error" in there else there,
                         "primary_error": i18n.tr(lang, there.get("error") or ""), "primary_at": seen.get("at") or "",
                         "primary_failed": seen.get("failed") if "error" in there else "",
                         "fence": replica_fences().get(major)})
        return rows

    @app.get("/api/links/{name}/postgres/replica")
    async def link_replica(request: Request, name: str, reserve_gb: str = "", again: str = "", which: str = "",
                           me: Principal = Depends(current_user)):
        """What + Replica of another machine… says before the click (UX-010
        §5.2): the size of the whole PostgreSQL of that machine, whose data
        it holds, the room here, the time expected, the round trip, the
        address this machine is seen with there, what changes there. Which
        PostgreSQL (UX-011 §D): the machine's cluster, or the dedicated
        database of one of its productions (which: its key there); the
        choices are in the answer, and the first that can be copied is taken
        when none is asked. Every refusal is an error here, before any POST."""
        postgres_admin(me)
        link = settings.link_targets.get(name)
        if link is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No link {name}")
        host = linkedcluster.host_of(link)
        offer = replica_offer()
        base = {"link": name, "host": host, "storage": offer["storage"], "cap_gb": offer["cap_gb"],
                "reserve_gb": offer["reserve_gb"], "here": here_name(), "choices": []}

        def no(error: str):
            # said in the page as it is (an answer of 200, which the errors' translation does not read): in its language
            return {**base, "error": i18n.tr(request_language(request), error)}
        if offer["why"]:
            return no(f"No replica here: {offer['why']}")
        why = next((item["why"] for item in offer["links"] if item["name"] == name), "")
        if why:
            return no(why)
        headers = {**link_headers(link, me.on_behalf or me.name, me.role), "Accept": "application/json"}
        client = link_clients.client(link)
        hosted_here = {d["source"] for d in offer["hosted_dedicated"] if d["link"] == name}
        # again: Make the replica again…, the stopped copy a cluster came back from is deleted first
        stopped = 0
        try:
            resp = await client.get("/api/postgres/replica/sources", timeout=LIST_TIMEOUT * 6, headers=headers)
            if resp.status_code == 200:
                sources = [c for c in resp.json().get("sources") or [] if isinstance(c, dict)]
            elif resp.status_code in (404, 405):
                # an OmniaCLI of before the replica of a dedicated database: its cluster only
                resp = await client.get("/api/postgres/machine/offer", timeout=LIST_TIMEOUT * 4, headers=headers)
                if resp.status_code != 200:
                    return no(replica_refusal(link, resp))
                sources = [{"key": str(c.get("major") or ""), "kind": "cluster", "major": str(c.get("major") or "")}
                           for c in resp.json().get("clusters") or [] if isinstance(c, dict)]
            else:
                return no(replica_refusal(link, resp))
            # a choice: what it is, and why it cannot be copied here ("" when it can)
            for one in sources:
                key, dedicated = str(one.get("key") or ""), one.get("kind") == "dedicated"
                taken = ("" if dedicated and again == key and key in hosted_here else
                         (f"This machine hosts a read replica of {one.get('name')} of {name} already."
                          if key in hosted_here else
                          f"The PostgreSQL of {one.get('name')} on {name} has a read replica already, on "
                          f"{one.get('replica')}: one per database.") if dedicated and (key in hosted_here or one.get("replica"))
                         else "" if dedicated or (again and offer["hosted"] == name) else
                         f"This machine hosts a read replica already, of {offer['hosted']}: drop it first"
                         if offer["hosted"] else
                         f"The cluster of {name} has a read replica already, on {one.get('replica')}: drop it first, from "
                         f"there or from {name}." if one.get("replica") else "")
                base["choices"].append({"key": key, "kind": "dedicated" if dedicated else "cluster",
                                        "name": str(one.get("name") or ""), "size": int(one.get("size") or 0),
                                        "instances": len(one.get("instances") or []), "why": taken})
            if not base["choices"]:
                return no(f"{name} has no machine's cluster and no production with a database of its own: there is "
                          "nothing to copy.")
            chosen = next((c for c in base["choices"] if c["key"] == which), None) if which else \
                next((c for c in base["choices"] if not c["why"]), base["choices"][0])
            if chosen is None:
                return no(f"{name} has no PostgreSQL {which}")
            major, dedicated = chosen["key"], chosen["kind"] == "dedicated"
            base.update(major=major, which=major, kind=chosen["kind"], name=chosen["name"])
            if again and (again == major if dedicated else offer["hosted"] == name):
                major_here = next((k for k, v in replica_hosted().items() if v.get("link") == name and (
                    v.get("source") == major if dedicated else not v.get("source"))), "")
                try:
                    found = await workspaceops.ask("machine-db-replica-hosted", major_here, timeout=pgtuning.READ_TIMEOUT * 4)
                except JobFailed as exc:
                    return no(str(exc))
                if found.get("state") != "returned":
                    return no(f"The copy is not one {name} came back from: it is made again only then")
                stopped = int(found.get("disk") or 0)
            elif chosen["why"]:
                return no(chosen["why"])
            resp = await client.get(f"/api/postgres/machine/{major}/replica/facts", timeout=LIST_TIMEOUT * 4, headers=headers)
            if resp.status_code != 200:
                return no(replica_refusal(link, resp))
            facts = resp.json()
        except httpx.HTTPError as exc:
            return no(f"{name} does not answer: {describe_error(exc)}. The replica is made through the link: try "
                      "again when it answers.")
        except (ValueError, AttributeError):
            return no(f"{name} gave an answer that cannot be read")
        base.update(machine=facts.get("machine") or name, size=int(facts.get("size") or 0),
                    instances=facts.get("instances") or [], grants=facts.get("grants") or [],
                    restarts=bool(facts.get("restarts")), port=facts.get("port"), version=facts.get("version") or "")
        if dedicated:
            # one customer's data, on its workspace's disk: what it keeps for the copy is a tenth of it at most
            base.update(workspace=facts.get("workspace") or "", disk_total=int(facts.get("disk_total") or 0),
                        disk_free=int(facts.get("disk_free") or 0), cap_gb=facts.get("cap_gb") or offer["cap_gb"],
                        cap_max=facts.get("cap_gb") or offer["cap_gb"], restart_s=facts.get("restart_s") or 5,
                        pg=facts.get("major") or "")
        if facts.get("replica"):
            other = str((facts["replica"] or {}).get("link") or "another machine")
            return no((f"The PostgreSQL of {chosen['name']} on {name} has a read replica already, on {other}: one per "
                       "database.") if dedicated else
                      f"The cluster of {name} has a read replica already, on {other}: drop it first, from there or "
                      f"from {name}.")
        if facts.get("why"):
            return no(f"{name}: {facts['why']}")
        ports = [facts["port"]] if isinstance(facts.get("port"), int) else []
        found = await asyncio.to_thread(linkedcluster.measure, host, [*ports, linkedcluster.link_port(link)])
        free = managedreplica.free_here(offer["storage"]) + stopped
        return {**base, "error": None, "free": free, "need": managedreplica.room(base["size"], free, 0)["need"],
                "stopped": stopped,
                "seconds": managedreplica.seconds_expected(base["size"]),
                "address": found["address"], "ms": found["ms"], "tries": found["tries"],
                "warn_ms": linkedcluster.WARN_MS, "trips": linkedcluster.TRIPS_PER_QUERY,
                "nat": bool(found["address"]) and not linkedcluster.public_address(found["address"])
                and linkedcluster.public_address(host)}

    def dedicated_name(key: str) -> str:
        """workspace-instance of the key of a dedicated database, as its machine names the instance; "" for a cluster's."""
        found = machinedb.DEDICATED_KEY.fullmatch(str(key or ""))
        return f"{found.group(1)}-{found.group(3)}" if found else ""

    async def replica_release(job: Job, link_name: str, major: str, who: str, role: str) -> bool:
        """The primary's machine asked to take its part back (the role, the
        line, the place that holds the changes; its instances that read go
        back to their cluster). It never fails the job: when that machine
        does not answer, what stays there is said. True when it did."""
        stays = (f"the role of the replication, its line of pg_hba.conf, the firewall rule for this machine's address "
                 f"and the place that holds the changes for the replica, which grows up to its cap; its instances "
                 f"that read from the replica go on trying it and read from their cluster. An admin there drops "
                 f"them in its Database tab (Drop the replica…)")
        link = settings.link_targets.get(link_name)
        if link is None:
            job.warn(f"The link {link_name} is no longer here: on that machine stay {stays}")
            return False
        client = link_clients.client(link)
        headers = {**link_headers(link, who, role), "Accept": "application/json"}
        job.log(f"--- {'the PostgreSQL of ' + dedicated_name(major) if dedicated_name(major) else 'PostgreSQL ' + major} of "
                f"{link.name}: asked to drop what it keeps for the replica")
        try:
            resp = await client.post(f"/api/postgres/machine/{major}/replica/drop", headers=headers, json={})
            if resp.status_code != 200:
                raise JobFailed(replica_refusal(link, resp))
            answer = resp.json()
            if answer.get("gone"):
                job.log(f"{link.name} kept nothing for a replica any more: nothing stays there")
                return True
            data = await linkcopy.follow(job, client, link, headers, str(answer.get("job") or ""),
                                         "the drop of the replica")
        except (httpx.HTTPError, JobFailed, ValueError) as exc:
            reason = describe_error(exc) if isinstance(exc, httpx.HTTPError) else str(exc)
            job.warn(f"{link.name} did not take its part back ({reason}): on it stay {stays}")
            return False
        done = (data.get("result") or {}).get("replica_dropped") or {}
        readers = ", ".join(done.get("readers") or [])
        job.log(f"{'The PostgreSQL of ' + dedicated_name(major) if dedicated_name(major) else 'PostgreSQL ' + major} of "
                f"{link.name} keeps nothing for the replica any more"
                + (f"; {readers} went back to reading from their cluster and restarted" if readers else ""))
        return True

    async def replica_made(job: Job, link: LinkTarget, major: str, opts: dict, who: str, role: str) -> None:
        """The steps of a replica made here, in the job that asked it (Make
        the replica, Make the replica again…): the machine of the link lets
        this one in, the copy, the start. opts: {"source" (this machine as
        that one sees it), "host" (how that one is reached), "cap",
        "reserve", "storage", "size", "restart"}. What fails at any step is
        taken back, here and there. major: the PostgreSQL as that machine
        names it; of the dedicated database of an instance (UX-011 §D)
        opts["key"] is the copy's own key here (d<n>) and opts["name"] the
        instance."""
        source, host, cap, reserve = opts["source"], opts["host"], opts["cap"], opts["reserve"]
        storage, size = opts["storage"], opts["size"]
        there, major = major, opts.get("key") or major
        name = str(opts.get("name") or "")
        headers = {**link_headers(link, who, role), "Accept": "application/json"}
        client = link_clients.client(link)
        progress = job.result["progress"] = {
            "steps": [{"id": "allow", "label": "Let in", "where": link.name},
                      {"id": "copy", "label": "Copy", "where": f"{link.name} → {here_name()}", "bytes": True},
                      {"id": "start", "label": "Replica", "where": here_name()}],
            "step": "allow", "done": 0, "total": size}
        started = time.monotonic()
        password = stacktemplates.new_password()
        if name:
            job.log(f"--- a read replica of the PostgreSQL of {name} of {link.name} on this machine: the database of that "
                    f"one instance ({size >> 20} MB), copied over TLS and kept up to date; the password of the "
                    "replication is made here and is not sent")
        else:
            job.log(f"--- a read replica of PostgreSQL {major} of {link.name} on this machine: the whole cluster "
                    f"({size >> 20} MB), copied over TLS and kept up to date; the password of the replication is "
                    "made here and is not sent")
        try:
            resp = await client.post(f"/api/postgres/machine/{there}/replica", headers=headers, json={
                "host": source, "reached_as": host, "verifier": linkedcluster.scram_verifier(password),
                "cap_gb": cap, "restart": bool(opts.get("restart"))})
        except httpx.HTTPError as exc:
            raise JobFailed(f"{link.name}: {describe_error(exc)}") from None
        if resp.status_code != 200:
            raise JobFailed(replica_refusal(link, resp))
        remote_id = str((resp.json() or {}).get("job") or "")
        if not remote_id:
            raise JobFailed(f"{link.name} started no job for the replica")
        data = await linkcopy.follow(job, client, link, headers, remote_id, "the replica")
        remote = (data.get("result") or {}).get("replica") or {}
        prepared = False
        watch = None
        try:
            spec = {"storage": storage, "link": link.name, "by": who, "host": host, "port": remote.get("port"),
                    "ca": remote.get("ca"), "address": source, "password": password,
                    "roots": remote.get("roots") or [], "environments": remote.get("environments") or [],
                    "tablespaces": remote.get("tablespaces") or [], "size": remote.get("size") or size,
                    "reserve_gb": reserve,
                    **({"major": str(remote.get("major") or ""),
                        "source": {"key": there, "workspace": str(remote.get("workspace") or ""), "name": name}}
                       if name else {})}
            made = await workspaceops._helper(job, "machine-db-replica-make", major, "prepare",
                                              timeout=MACHINE_TIMEOUT, input=json.dumps(spec).encode())
            prepared = True
            job.log(f"Its project is beside the machine's cluster's, on the storage {storage}; it will answer on "
                    f"port {made.get('port')} of this machine, over TLS, for {made.get('primary_address')} only")
            progress["step"] = "copy"

            async def watching():
                while True:
                    await asyncio.sleep(2)
                    with contextlib.suppress(JobFailed):
                        found = await workspaceops.ask("machine-db-replica-hosted", major, timeout=60)
                        progress["done"] = int(found.get("copied") or progress["done"])
            watch = asyncio.get_running_loop().create_task(watching())
            copied = await workspaceops._helper(job, "machine-db-replica-make", major, "copy",
                                                timeout=managedreplica.COPY_TIMEOUT + 60)
            watch.cancel()
            progress["done"] = progress["total"] = int(copied.get("size") or size)
            job.log(f"Copied: {int(copied.get('size') or 0) >> 20} MB in {copied.get('seconds')} s, over TLS with "
                    f"the certificate of {link.name} verified against its CA and its name")
            progress["step"] = "start"
            up = await workspaceops._helper(job, "machine-db-replica-make", major, "start", timeout=MACHINE_TIMEOUT)
            job.log(f"The replica answers and receives from {link.name} (PostgreSQL {str(up.get('version') or '').split(' ')[0]}, "
                    f"started in {up.get('seconds')} s)")
            try:
                resp = await client.post(f"/api/postgres/machine/{there}/replica/ready", headers=headers, json={
                    "host": source, "port": up.get("port"), "ca": up.get("ca"),
                    "seconds": copied.get("seconds"), "size": copied.get("size")})
            except httpx.HTTPError as exc:
                raise JobFailed(f"{link.name}: {describe_error(exc)}") from None
            if resp.status_code != 200:
                raise JobFailed(replica_refusal(link, resp))
        except (JobFailed, asyncio.CancelledError) as exc:
            if watch is not None:
                watch.cancel()
            # nothing half made stays: here, then there
            if prepared:
                with contextlib.suppress(JobFailed):
                    await asyncio.shield(workspaceops._helper(job, "machine-db-replica-remove", major,
                                                              timeout=MACHINE_TIMEOUT))
            await asyncio.shield(replica_release(job, link.name, there, who, role))
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise JobFailed(str(exc)) from None
        replica_seen.pop(major, None)
        if name:
            job.log(f"The read replica of {name} of {link.name} is made in {round(time.monotonic() - started, 1)} s. "
                    f"{name} reads from it once Read from the replica… is chosen in its page, there")
        else:
            job.log(f"The read replica of {link.name} is made in {round(time.monotonic() - started, 1)} s. Its "
                    f"instances read from it once Read from the replica… is chosen in each one's page, there")
        job.result = {**job.result, "replica": {"link": link.name, "major": major, "size": copied.get("size"),
                                                "seconds": copied.get("seconds")}}

    @app.post("/api/postgres/replica/make")
    async def replica_make(payload: dict = Body(...), me: Principal = Depends(current_user)):
        """Make the replica (UX-010 §5.2): the cluster of the machine of a
        link copied here and started as a replica that follows it. A job with
        its steps: that machine lets this one in, the copy, the start. What
        fails at any step is taken back, here and there."""
        postgres_admin(me)
        payload = payload if isinstance(payload, dict) else {}
        link = settings.link_targets.get(str(payload.get("link") or ""))
        if link is None:
            return refuse(f"No link {payload.get('link')!r}", status.HTTP_404_NOT_FOUND)
        offer = replica_offer()
        if offer["why"]:
            return refuse(f"No replica here: {offer['why']}")
        major = str(payload.get("major") or "")
        dedicated = managedreplica.is_dedicated(major)
        if dedicated and any(d["link"] == link.name and d["source"] == major for d in offer["hosted_dedicated"]):
            return refuse(f"This machine hosts a read replica of {dedicated_name(major)} of {link.name} already: drop it "
                          "first")
        if offer["hosted"] and not dedicated:
            return refuse(f"This machine hosts a read replica already, of {offer['hosted']}: drop it first")
        why = linkedcluster.unprotected(link)
        if why:
            return refuse(f"The link with {link.name} is plain HTTP: a replica asks for HTTPS or SSH, because what "
                          "it answers says where the data comes from.")
        if major not in machinedb.MAJORS and not dedicated:
            return refuse(f"PostgreSQL {major or '?'} is not one OmniaCLI replicates ({', '.join(machinedb.MAJORS)})")
        try:
            source = machinedb.address(payload.get("address"))
            host = machinedb.reached_as([linkedcluster.host_of(link)])[0]
            # of a dedicated database, what its machine says: a tenth of its workspace's disk at most
            cap = (managedreplica.cap_gb(payload.get("cap_gb", managedreplica.CAP_GB))
                   if not dedicated or payload.get("cap_gb") not in (None, "") else None)
            reserve = managedreplica.reserve_gb(payload.get("reserve_gb", managedreplica.RESERVE_GB))
        except machinedb.ClusterError as exc:
            return refuse(str(exc))
        storage = offer["storage"]
        who, role = me.on_behalf or me.name, me.role
        headers = {**link_headers(link, who, role), "Accept": "application/json"}
        client = link_clients.client(link)
        # the room, before anything is asked of that machine
        try:
            resp = await client.get(f"/api/postgres/machine/{major}/replica/facts", timeout=LIST_TIMEOUT * 4, headers=headers)
        except httpx.HTTPError as exc:
            return refuse(f"{link.name} does not answer: {describe_error(exc)}. The replica is made through the link: "
                          "try again when it answers.", status.HTTP_502_BAD_GATEWAY)
        if resp.status_code != 200:
            return refuse(replica_refusal(link, resp))
        facts = resp.json()
        name = str(facts.get("name") or dedicated_name(major)) if dedicated else ""
        if facts.get("replica"):
            return refuse((f"The PostgreSQL of {name} on {link.name} has a read replica already, on "
                           f"{(facts['replica'] or {}).get('link')}: one per database.") if dedicated else
                          f"The cluster of {link.name} has a read replica already, on "
                          f"{(facts['replica'] or {}).get('link')}: drop it first, from there or from {link.name}.")
        if facts.get("why"):
            return refuse(f"{link.name}: {facts['why']}")
        if facts.get("restarts") and not payload.get("restart"):
            return refuse((f"The PostgreSQL of {name} is restarted once to open a port over TLS: {name} does not answer "
                           "for those seconds. Confirm the restart") if dedicated else
                          f"The cluster of {link.name} is made again once to open its port over TLS: its instances "
                          "lose their connections. Confirm the restart", status.HTTP_409_CONFLICT)
        size = int(facts.get("size") or 0)
        free = managedreplica.free_here(storage)
        fits = managedreplica.room(size, free, reserve)
        if not fits["fits"]:
            return refuse(f"No room: the copy of the {'database of ' + name if dedicated else 'cluster'} of {link.name} "
                          f"takes about {fits['need'] >> 20} MB, "
                          f"{free / workspaceops.GB:.1f} GB are free here and {reserve:g} GB must stay free.",
                          status.HTTP_507_INSUFFICIENT_STORAGE)
        # the copy's own key here: the major of a cluster's, d<n> of the one of a dedicated database
        key = managedreplica.next_hosted_key(replica_hosted()) if dedicated else major

        async def action(job):
            await replica_made(job, link, major, {"source": source, "host": host, "cap": cap, "reserve": reserve,
                                                  "storage": storage, "size": size, "key": key, "name": name,
                                                  "restart": bool(payload.get("restart"))}, who, role)
        job = jobs.submit(f"Read replica of the PostgreSQL of {name} of {link.name}" if dedicated else
                          f"Read replica of PostgreSQL {major} of {link.name}", [replica_key(key)],
                          f"manual: {me}", action)
        return {"job": job.id}

    @app.post("/api/postgres/replica/{major}/drop")
    async def replica_drop(major: str, me: Principal = Depends(current_user)):
        """Drop the replica… on the machine that hosts it: the primary's
        machine asked to drop what it keeps for it (its instances that read
        go back to their cluster), then the copy and its container deleted.
        When that machine does not answer the copy goes all the same, and
        what stays there is said."""
        postgres_admin(me)
        entry = replica_hosted().get(major)
        if entry is None:
            return refuse(f"This machine hosts no read replica of a PostgreSQL {major}", status.HTTP_404_NOT_FOUND)
        name = str(entry.get("link") or "")
        try:
            here = await workspaceops.ask("machine-db-replica-hosted", major, timeout=pgtuning.READ_TIMEOUT * 4)
        except JobFailed as exc:
            return refuse(str(exc), status.HTTP_502_BAD_GATEWAY)
        who, role = me.on_behalf or me.name, me.role
        there = replica_there(major, entry)
        orphan = False
        if here.get("state") == "promoted" and entry.get("source"):
            # of one instance: dropped once that instance is no longer on its machine, which says so
            link = settings.link_targets.get(name)
            said = await replica_ask(link, there, who, role, LIST_TIMEOUT * 2) if link is not None else {"error": "no link"}
            orphan = replica_orphan(entry, here, said)
            if not orphan:
                return refuse(f"The replica was promoted: it is the database of {entry.get('name')} of {name}. It is "
                              f"dropped only after {entry.get('name')} is removed on {name}, and {name} says so")
        elif here.get("state") == "promoted":
            return refuse(f"The replica was promoted: it is the database of the instances of {name}. It is not "
                          "dropped from here")

        async def action(job):
            # the stopped copy a cluster came back from: that machine keeps nothing for it; nor for the
            # promoted copy of an instance that is no longer there
            if here.get("state") != "returned" and not orphan:
                await replica_release(job, name, there, who, role)
            done = await workspaceops._helper(job, "machine-db-replica-remove", major, timeout=MACHINE_TIMEOUT)
            replica_seen.pop(major, None)
            replica_synced.pop(major, None)
            replica_fence_set(major, None)
            if orphan:
                job.log(f"{entry.get('name')} is no longer on {name}: its promoted copy and its container are deleted, "
                        f"{int(done.get('freed') or 0) >> 20} MB freed on this machine")
                return
            job.log(f"The copy and its container are deleted: {int(done.get('freed') or 0) >> 20} MB freed on this "
                    "machine. A new replica copies everything again")
        job = jobs.submit(f"Drop the read replica of {replica_title(major, entry)}", [replica_key(major)],
                          f"manual: {me}", action)
        return {"job": job.id}

    async def replica_remote_job(job: Job, link: LinkTarget, headers: dict, path: str, what: str,
                                 payload: dict | None = None) -> dict:
        """A job asked of the machine of the link (POST path, with payload)
        and followed to its end: its result. JobFailed says why not."""
        client = link_clients.client(link)
        try:
            resp = await client.post(path, headers=headers, json=payload or {})
        except httpx.HTTPError as exc:
            raise JobFailed(f"{link.name}: {describe_error(exc)}") from None
        if resp.status_code != 200:
            raise JobFailed(replica_refusal(link, resp))
        remote_id = str((resp.json() or {}).get("job") or "")
        if not remote_id:
            raise JobFailed(f"{link.name} started no job for {what}")
        data = await linkcopy.follow(job, client, link, headers, remote_id, what)
        return data.get("result") or {}

    async def replica_point(job: Job, link: LinkTarget, major: str, headers: dict) -> dict:
        """The primary's machine told that its cluster was replaced: it stops
        it (when it runs still) and points its instances at the promoted
        copy. What each instance answered."""
        await replica_remote_job(job, link, headers, f"/api/postgres/machine/{major}/replica/replaced",
                                 "the stop of its PostgreSQL")
        result = await replica_remote_job(job, link, headers, f"/api/postgres/machine/{major}/replica/follow",
                                          "its instances pointed here")
        followed = result.get("followed") or {}
        down = sorted(name for name, one in followed.items() if not one.get("answers"))
        job.log(f"{len(followed) - len(down)} of the {len(followed)} instances of {link.name} answer on this copy"
                + (f"; {', '.join(down)} do not: see the job of {link.name}" if down else ""))
        if down:
            job.warn(f"{', '.join(down)} of {link.name} do not answer on the promoted copy")
        return followed

    @app.post("/api/postgres/replica/{major}/promote")
    async def replica_promote(major: str, payload: dict = Body(...), me: Principal = Depends(current_user)):
        """Promote… (UX-010 §6): the copy becomes the database of the
        instances of the primary's machine. With that machine answering its
        PostgreSQL is stopped first, then the copy takes the writes, then its
        instances are pointed here and restarted. With that machine not
        answering (offline: the admin said it is off) the copy takes the
        writes, and the stop and the pointing are tried again every
        REPLICA_RETRY seconds until that machine answers."""
        postgres_admin(me)
        entry = replica_hosted().get(major)
        if entry is None:
            return refuse(f"This machine hosts no read replica of a PostgreSQL {major}", status.HTTP_404_NOT_FOUND)
        name = str(entry.get("link") or "")
        payload = payload if isinstance(payload, dict) else {}
        # of one instance's dedicated database (UX-011 §D): the instance is what is typed, and what is said
        one, there_key = str(entry.get("name") or ""), replica_there(major, entry)
        if str(payload.get("confirm") or "") != (one or name):
            return refuse(f"Write {one or name} to confirm the promotion")
        link = settings.link_targets.get(name)
        try:
            here = await workspaceops.ask("machine-db-replica-hosted", major, timeout=pgtuning.READ_TIMEOUT * 4)
        except JobFailed as exc:
            return refuse(str(exc), status.HTTP_502_BAD_GATEWAY)
        if here.get("state") != "ready" or not here.get("synced") or not here.get("running"):
            return refuse("The replica is not a whole copy yet: it is promoted only after it was in sync.")
        who, role = me.on_behalf or me.name, me.role
        there = await replica_ask(link, there_key, who, role, LIST_TIMEOUT * 2) if link is not None else {"error": "no link"}
        answers = "error" not in there
        if not answers and one:
            # its Odoo is on that machine: with it off there is nothing to point here
            return refuse(f"{name} does not answer ({there['error']}): the replica of {one} is promoted only with "
                          f"{name} answering, because {one} itself runs there and is pointed here by it",
                          status.HTTP_409_CONFLICT)
        if not answers and not payload.get("offline"):
            return refuse(f"{name} does not answer ({there['error']}): OmniaCLI cannot stop its PostgreSQL. Promote "
                          f"only if {name} is really off", status.HTTP_409_CONFLICT)
        headers = {**link_headers(link, who, role), "Accept": "application/json"} if link is not None else {}

        async def action(job):
            # that machine not answering, nothing of it is stopped nor pointed by this job: its steps say only what it does
            progress = job.result["progress"] = {
                "steps": [*([{"id": "stop", "label": "Stop", "where": name}] if answers else []),
                          {"id": "promote", "label": "Promote", "where": here_name()},
                          *([{"id": "point", "label": "Instances", "where": name}] if answers else [])],
                "step": "stop" if answers else "promote"}
            started = time.monotonic()
            if answers:
                job.log(f"--- the replica of {one + ' of ' + name if one else name} is promoted: its PostgreSQL is "
                        "stopped first, so that nothing is written on both")
                try:
                    stopped = await replica_remote_job(job, link, headers,
                                                       f"/api/postgres/machine/{there_key}/replica/replaced",
                                                       "the stop of its PostgreSQL")
                except JobFailed as exc:
                    raise JobFailed(f"{name} answers but its PostgreSQL did not stop ({exc}): nothing is promoted") from None
                lent = (stopped.get("replaced") or {}).get("grants") or []
                lines = (stopped.get("replaced") or {}).get("grant_lines")
            else:
                # as this copy read them last (its record): that machine does not say
                lines = None
                job.warn(f"{name} does not answer: its PostgreSQL was not stopped. If it runs and an instance writes "
                         "to it, what it writes from now is lost when it is stopped")
                lent = ((replica_seen.get(major) or {}).get("state") or {}).get("grants") or []
            progress["step"] = "promote"
            done = await workspaceops._helper(job, "machine-db-replica-promote", major, timeout=MACHINE_TIMEOUT,
                                              input=json.dumps({"grants": lines} if isinstance(lines, dict) else {}).encode())
            job.log(f"This copy takes the writes ({done.get('seconds', '?')} s)")
            lent = done.get("lent") if isinstance(done.get("lent"), list) else lent
            access_log.record("action", "system", "", "", (f"read replica of the PostgreSQL of {one} of {name} promoted: "
                              f"it is its database (asked by {who})") if one else
                              f"read replica of PostgreSQL {major} of {name} promoted: it "
                              f"is the database of its instances (asked by {who})", context=False)
            if answers:
                progress["step"] = "point"
                try:
                    await replica_point(job, link, there_key, headers)
                except JobFailed as exc:
                    # promoted and not followed: tried again by the loop, the instances have no database till then
                    replica_fence_set(major, {"link": name, "since": replica_now(), "who": who, "role": role})
                    job.warn(f"The instances of {name} were not pointed here ({exc}): it is tried again every "
                             f"{REPLICA_RETRY} s")
            else:
                replica_fence_set(major, {"link": name, "since": replica_now(), "who": who, "role": role})
                job.warn(f"The instances of {name} are pointed here when it answers again: OmniaCLI tries every "
                         f"{REPLICA_RETRY} s to stop its PostgreSQL and to point them. What they write there "
                         "meanwhile is lost")
            if lent and answers:
                job.log(f"The databases other machines use on the cluster of {name} ({', '.join(lent)}) are in this "
                        f"copy, which lets them in from the same addresses: each machine points its instance here by "
                        f"itself, within a minute of {name} telling it")
            elif lent:
                job.warn(f"The databases other machines use on the cluster of {name} ({', '.join(lent)}) are in this "
                         f"copy, which lets them in from the same addresses: each machine points its instance here by "
                         f"itself once {name} answers again; until then those instances have no database")
            job.result = {**job.result, "promoted": {"link": name, "seconds": round(time.monotonic() - started, 1),
                                                     "stopped_first": answers}}
        job = jobs.submit(f"Promote the read replica of {replica_title(major, entry)}", [replica_key(major)],
                          f"manual: {me}", action)
        return {"job": job.id}

    # --- the return after a promotion (UX-011 §A; managedreplica.py return_* and back_*). P is the
    # machine whose cluster the replica replaced, R hosts the promoted copy. P asks, through a link of
    # its own to R (the machine that hosts a copy asks the one that has the data: in the return the copy
    # is P's); R never calls P: a return cancelled on R, P reads at its next look (return_look).
    # The dedicated database of one instance comes back by the same routes (UX-011 §A for one instance):
    # <major> is then its key on P (<workspace>.<storage>.<instance>), which R takes too for the copy of it
    # that it hosts (hosted_key_of), and the instance's own row of the Database tab is where it is asked.
    # R's side: GET /api/postgres/replica/<major>/return/facts and …/state, POST …/return (let in),
    # …/return/stop, …/resume, …/done, …/cancel, and POST /api/postgres/replica/<major>/remake.
    # P's side: GET /api/postgres/machine/<major>/return/facts, POST …/return/prepare, …/complete, …/cancel.

    # P: what the machine of the promoted copy said last of the return, by major
    return_seen: dict[str, dict] = {}

    def return_record(major: str) -> dict | None:
        """The PostgreSQL's own record of its return (a file OmniaCLI reads: no
        PostgreSQL asked): the cluster's, or the one of the dedicated database
        of an instance (<major> its key), kept in the instance's project."""
        found = machinedb.registry().get(major)
        if found:
            return machinedb.returning(Path(str(found.get("project") or "")))
        target = dedicated_instance(major)
        return machinedb.returning(dedicated_folder(target[2])) if target else None

    def promoted_instances(major: str = "") -> list[Instance]:
        """The instances of this machine pointed at the promoted copy of its
        cluster; of the key of an instance's dedicated database, that
        instance when it is pointed at the promoted copy of its own."""
        target = dedicated_instance(major)
        if target is not None:
            own = (instance_meta(target[2]).get("promoted_from") or {})
            return [target[2]] if on_remote_db(target[2]) and isinstance(own, dict) and own.get("dedicated") else []
        return [i for srv in settings.active_servers() for i in srv.instances.values()
                if i.restart.type == "compose" and on_remote_db(i) and isinstance(instance_meta(i).get("promoted_from"), dict)
                and not instance_meta(i)["promoted_from"].get("dedicated")]

    def return_link(rec: dict) -> tuple[LinkTarget | None, str]:
        """The link of this machine to the one that hosts the promoted copy
        (the one that leads to the copy's host), or why none may carry the return."""
        host, machine = str(rec.get("host") or "").lower(), str(rec.get("link") or "that machine")
        found = [link for link in settings.link_targets.values() if host and linkedcluster.host_of(link) == host]
        good = [link for link in found if linkedcluster.unprotected(link) is None]
        if good:
            return good[0], ""
        if found:
            return None, (f"The link with {found[0].name} is plain HTTP: a replica asks for HTTPS or SSH, because what "
                          "it answers says where the data comes from.")
        return None, (f"A link from this machine to {machine} is needed, over HTTPS or SSH: it is added from "
                      "Servers › + Add a server.")

    async def return_local(major: str) -> dict:
        return await workspaceops.ask("machine-db-return", major, "state", timeout=pgtuning.READ_TIMEOUT * 4)

    def return_not_replaced(major: str):
        """The refusal of a return asked of a PostgreSQL its replica did not replace."""
        target = dedicated_instance(major)
        return refuse((f"The database of {target[2].server} was not replaced by its replica: there is nothing to come "
                       "back from") if target is not None else
                      f"The machine's PostgreSQL {major} was not replaced by its replica: there is nothing to come "
                      "back from", status.HTTP_404_NOT_FOUND)

    async def return_of_row(major: str, lang: str) -> dict | None:
        """P, for the cluster's row: what this machine sees of its return and
        what the machine of the promoted copy says of it (through the link,
        within REPLICA_ASK_TIMEOUT: one that does not answer is the reason in
        the row, with what it said last); the instances not back yet."""
        back = return_record(major)
        if back is None:
            return None
        link = settings.link_targets.get(str(back.get("link") or ""))

        async def local():
            try:
                return await return_local(major)
            except JobFailed as exc:
                return {"exists": True, **{k: back.get(k) for k in ("state", "at", "machine", "aside", "expected")},
                        "error": i18n.tr(lang, str(exc))}

        async def remote():
            if back.get("state") == "done":
                return {}
            if link is None:
                return {"error": f"the link {back.get('link')} is no longer here"}
            return await replica_get(link, f"/api/postgres/replica/{major}/return/state", "omniacli", "admin",
                                     REPLICA_ASK_TIMEOUT)
        here, there = await asyncio.gather(local(), remote())
        before = return_seen.get(major) or {}
        if "error" not in there:
            return_seen[major] = {"at": replica_now(), "state": there, "error": ""}
        else:
            return_seen[major] = {**before, "error": there["error"], "failed": before.get("failed") or replica_now()}
        seen = return_seen[major]
        return {"here": here, "there": seen.get("state") if "error" in there else there,
                "there_error": i18n.tr(lang, there.get("error") or ""),
                "there_failed": seen.get("failed") if "error" in there else "",
                "pending": sorted(i.server for i in promoted_instances(major))}

    async def return_release(job: Job, link: LinkTarget | None, machine: str, major: str, headers: dict) -> bool:
        """The machine of the promoted copy asked to take away what it keeps
        for the cluster that comes back. It never fails the job: when that
        machine does not answer, what stays there is said. True when it did."""
        stays = (f"the role of the replication, its line of access for this machine's address and the place that "
                 f"holds its changes, which grows up to its cap: an admin there takes them away in its Database tab "
                 f"(Cancel the return…)")
        if link is None:
            job.warn(f"The link to {machine} is no longer here: on that machine stay {stays}")
            return False
        try:
            done = await replica_remote_job(job, link, headers, f"/api/postgres/replica/{major}/return/cancel",
                                            "what it kept for the return taken away")
        except JobFailed as exc:
            job.warn(f"{machine} did not take its part back ({exc}): on it stay {stays}")
            return False
        freed = int((done.get("return_cancelled") or {}).get("freed") or 0)
        job.log(f"{machine} keeps nothing for the return any more: {freed >> 20} MB of changes freed there")
        return True

    async def instances_pointed(job: Job, served: list[Instance], command: str, where: str) -> dict:
        """Each instance of served written again by the root helper's command
        (machine-db-replica-follow: at the promoted copy; machine-db-replica-back:
        back on this machine's cluster) and started, one after the other, its
        entry of the servers file following: {server: {"answers", "seconds"}}."""
        outcome = {}
        for inst in served:
            found = workspace_of(inst.server)
            if found is None:
                job.warn(f"{inst.server} is not an instance of a workspace: it is not pointed at {where}")
                continue
            ws, instance = found
            async with jobs.lock(inst.key):
                try:
                    made = await workspaceops._helper(job, command, ws.name, ws.storage, instance, timeout=MACHINE_TIMEOUT)
                except JobFailed as exc:
                    job.warn(f"{inst.server} was not pointed at {where} ({exc}): it has no database until it is")
                    outcome[inst.server] = {"answers": False, "error": str(exc)[:300]}
                    continue
                async with config_lock:
                    current = settings.servers.get(inst.server)
                    if current is not None:
                        data = server_to_dict(current)
                        data["production"] = {**data["production"], "database": made["database"],
                                              "db_service": made["db_service"]}
                        parsed = parse_servers({inst.server: data}, settings.logs_dir)[inst.server]
                        error = await store({**settings.servers, inst.server: parsed})
                        if error is not None:
                            job.warn(f"{inst.server}: the servers file was not written: {error.body.decode()}")
                now_inst = settings.servers[inst.server].production if inst.server in settings.servers else inst
                answers, seconds = await replica_answers(now_inst)
                outcome[inst.server] = {"answers": answers, "seconds": seconds}
                if answers:
                    job.log(f"{inst.server} answers again after {seconds} s, on {where}")
                else:
                    job.warn(f"{inst.server} does not answer {seconds} s after it was pointed at {where}")
        return outcome

    # P: the machine whose cluster comes back

    @app.get("/api/postgres/machine/{major}/return/facts")
    async def return_facts(request: Request, major: str, me: Principal = Depends(current_user)):
        """What Prepare the return… says before the click (UX-011 §A): the
        data set aside, the size of the promoted copy, the room here, the
        time expected, the round trip, what is made on the machine that
        hosts it. Every refusal is an error here, before any POST."""
        postgres_admin(me)
        rec = replica_record(major)
        if not pg_known(major) or rec is None or rec.get("state") != "replaced":
            return return_not_replaced(major)
        machine = str(rec.get("link") or "")
        target = dedicated_instance(major)
        base = {"machine": machine, "major": major, "cap_gb": managedreplica.CAP_GB,
                "reserve_gb": managedreplica.RESERVE_GB, "instances": sorted(i.server for i in promoted_instances(major)),
                # of one instance (UX-011 §A for one instance): its name, and the workspace whose disk takes the copy
                **({"kind": "dedicated", "name": target[2].server, "workspace": target[0].name} if target else {})}

        def no(error: str, **more):
            # the button's box says it: in the reader's language, as a refusal is
            return {**base, **more, "error": i18n.tr(request_language(request), error)}
        link, why = return_link(rec)
        if link is None:
            return no(why, missing=True)
        try:
            here = await workspaceops.ask("machine-db-return", major, "facts", timeout=pgtuning.READ_TIMEOUT * 4)
        except JobFailed as exc:
            return no(str(exc))
        base.update(link=link.name, aside=here["aside"], free=here["free"])
        if (here.get("return") or {}).get("state") in ("copying", "ready"):
            return no("A return is under way already.")
        there = await replica_get(link, f"/api/postgres/replica/{major}/return/facts", me.on_behalf or me.name, me.role,
                                  LIST_TIMEOUT * 4)
        if "error" in there:
            return no(f"{machine} does not answer: {there['error']}. The return is prepared through the link: try "
                      "again when it answers.")
        if there.get("why") or not there.get("running"):
            return no(f"{machine}: {there.get('why') or 'the promoted copy does not answer'}")
        size = int(there.get("size") or 0)
        fits = managedreplica.room(size, here["free"], managedreplica.RESERVE_GB)
        found = await asyncio.to_thread(linkedcluster.measure, str(rec.get("host") or ""),
                                        [int(rec.get("port") or 0), linkedcluster.link_port(link)])
        # (of one instance the room is its workspace's disk's: said whole here, in the reader's language)
        why = managedreplica.back_room(size, here, managedreplica.RESERVE_GB) if fits["fits"] or target else ""
        return {**base, "error": None, "size": size, "need": fits["need"], "fits": fits["fits"],
                "disk_why": i18n.tr(request_language(request), why[:1].upper() + why[1:] + "." if why else ""),
                "seconds": managedreplica.seconds_expected(size), "address": there.get("address") or "",
                "ms": found["ms"], "tries": found["tries"]}

    @app.post("/api/postgres/machine/{major}/return/prepare")
    async def return_prepare(major: str, payload: Optional[dict] = Body(None), me: Principal = Depends(current_user)):
        """Prepare the return… (UX-011 §A): this cluster's data set aside,
        the promoted copy copied here through the link and followed read
        only. Nothing of the instances stops. A job with its steps; what
        fails at any step is taken back, here and there."""
        postgres_admin(me)
        payload = payload if isinstance(payload, dict) else {}
        rec = replica_record(major)
        if not pg_known(major) or rec is None or rec.get("state") != "replaced":
            return return_not_replaced(major)
        machine = str(rec.get("link") or "")
        target = dedicated_instance(major)
        link, why = return_link(rec)
        if link is None:
            return refuse(why)
        if (return_record(major) or {}).get("state") in ("copying", "ready"):
            return refuse("A return is under way already.")
        try:
            cap = managedreplica.cap_gb(payload.get("cap_gb", managedreplica.CAP_GB))
            reserve = managedreplica.reserve_gb(payload.get("reserve_gb", managedreplica.RESERVE_GB))
            here = await workspaceops.ask("machine-db-return", major, "facts", timeout=pgtuning.READ_TIMEOUT * 4)
        except (machinedb.ClusterError, JobFailed) as exc:
            return refuse(str(exc))
        who, role = me.on_behalf or me.name, me.role
        headers = {**link_headers(link, who, role), "Accept": "application/json"}
        there = await replica_get(link, f"/api/postgres/replica/{major}/return/facts", who, role, LIST_TIMEOUT * 4)
        if "error" in there:
            return refuse(f"{machine} does not answer: {there['error']}. The return is prepared through the link: try "
                          "again when it answers.", status.HTTP_502_BAD_GATEWAY)
        if there.get("why") or not there.get("running"):
            return refuse(f"{machine}: {there.get('why') or 'the promoted copy does not answer'}")
        size = int(there.get("size") or 0)
        refusal = managedreplica.back_room(size, here, reserve)
        if refusal:
            return refuse(refusal[0].upper() + refusal[1:] + ".", status.HTTP_507_INSUFFICIENT_STORAGE)
        count = len(promoted_instances(major))
        # of one instance: its name, the workspace whose disk takes the copy
        one = target[2].server if target else ""

        async def action(job):
            progress = job.result["progress"] = {
                "steps": [{"id": "allow", "label": "Let in", "where": machine},
                          {"id": "aside", "label": "Set aside", "where": here_name()},
                          {"id": "copy", "label": "Copy", "where": f"{machine} → {here_name()}", "bytes": True},
                          {"id": "follow", "label": "Follows", "where": here_name()}],
                "step": "allow", "done": 0, "total": size}
            started = time.monotonic()
            password = stacktemplates.new_password()
            if one:
                job.log(f"--- the return of the database of {one} from {machine}: its promoted copy ({size >> 20} MB) is "
                        f"copied here over TLS, on the disk of the workspace {target[0].name}, and followed read only; the "
                        f"password of the replication is made here and is not sent. {one} keeps working on {machine}: "
                        "nothing stops")
            else:
                job.log(f"--- the return of the machine's PostgreSQL {major} from {machine}: its promoted copy "
                        f"({size >> 20} MB) "
                        "is copied here over TLS and followed read only; the password of the replication is made here and "
                        "is not sent. Nothing of the instances stops")
            let_in = await replica_remote_job(job, link, headers, f"/api/postgres/replica/{major}/return",
                                              "the return let in", {"verifier": linkedcluster.scram_verifier(password),
                                                                    "cap_gb": cap})
            remote = let_in.get("return") or {}
            prepared, watch = False, None
            try:
                progress["step"] = "aside"
                spec = {"link": link.name, "by": who, "host": str(rec.get("host") or ""), "port": remote.get("port"),
                        "ca": remote.get("ca"), "password": password, "size": remote.get("size") or size,
                        "tablespaces": remote.get("tablespaces") or [], "reserve_gb": reserve,
                        # the PostgreSQL the copy is of: what the database of an instance comes back with
                        "major": str(remote.get("version") or "").split(".")[0]}
                made = await workspaceops._helper(job, "machine-db-return", major, "prepare", timeout=MACHINE_TIMEOUT,
                                                  input=json.dumps(spec).encode())
                prepared = True
                aside = made.get("aside") or {}
                if one and not int(aside.get("size") or 0):
                    job.log(f"The data {one} had here was freed already: there is nothing to set aside")
                elif one:
                    job.log(f"The data {one} had here, stopped since {aside.get('at')}, is set aside "
                            f"({int(aside.get('size') or 0) >> 20} MB): it is not deleted until the return is complete, "
                            "and comes back if the return is cancelled")
                else:
                    job.log(f"The data of this cluster, stopped since {aside.get('at')}, is set aside "
                            f"({int(aside.get('size') or 0) >> 20} MB): it is not deleted until the return is complete, and "
                            "comes back if the return is cancelled")
                progress["step"] = "copy"

                async def watching():
                    while True:
                        await asyncio.sleep(2)
                        with contextlib.suppress(JobFailed):
                            progress["done"] = int((await return_local(major)).get("copied") or progress["done"])
                watch = asyncio.get_running_loop().create_task(watching())
                copied = await workspaceops._helper(job, "machine-db-return", major, "copy",
                                                    timeout=managedreplica.COPY_TIMEOUT + 60)
                watch.cancel()
                progress["done"] = progress["total"] = int(copied.get("size") or size)
                job.log(f"Copied: {int(copied.get('size') or 0) >> 20} MB in {copied.get('seconds')} s, over TLS with "
                        f"the certificate of {machine} verified against its CA and its name")
                progress["step"] = "follow"
                up = await workspaceops._helper(job, "machine-db-return", major, "start", timeout=MACHINE_TIMEOUT)
                job.log(f"The database of {one} runs here on the copy, read only, in a project of its own beside the "
                        f"instance's, and receives from {machine} (started in {up.get('seconds')} s)" if one else
                        f"This cluster runs on the copy, read only, and receives from {machine} (started in "
                        f"{up.get('seconds')} s)")
            except (JobFailed, asyncio.CancelledError) as exc:
                if watch is not None:
                    watch.cancel()
                # nothing half made stays: here (the data set aside back where it was), then there
                if prepared:
                    with contextlib.suppress(JobFailed):
                        await asyncio.shield(workspaceops._helper(job, "machine-db-return", major, "cancel",
                                                                  timeout=MACHINE_TIMEOUT))
                await asyncio.shield(return_release(job, link, machine, major, headers))
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise JobFailed(str(exc)) from None
            return_seen.pop(major, None)
            if one:
                job.log(f"The return is ready in {round(time.monotonic() - started, 1)} s: the copy here follows {machine} "
                        f"at each change. {one} keeps working on {machine}: Complete the return… brings it back here")
            else:
                job.log(f"The return is ready in {round(time.monotonic() - started, 1)} s: this cluster follows {machine} "
                        f"at each change. The {count} instances keep working on {machine}: Complete the return… brings "
                        "them back here")
            job.result = {**job.result, "return": {"machine": machine, "size": copied.get("size"),
                                                   "seconds": copied.get("seconds")}}
        job = jobs.submit(f"Prepare the return of the database of {one} from {machine}" if one else
                          f"Prepare the return of the machine's PostgreSQL {major} from {machine}",
                          [f"{MACHINE_KEY}/{major}"], f"manual: {me}", action)
        return {"job": job.id}

    @app.post("/api/postgres/machine/{major}/return/complete")
    async def return_complete(major: str, payload: dict = Body(...), me: Principal = Depends(current_user)):
        """Complete the return… (UX-011 §A): the promoted copy is stopped
        first, this cluster waits until all it wrote arrived (BACK_WAIT
        seconds at most: else that copy runs again and the instances stay
        there, nothing lost), takes the writes, and its instances go back on
        it by its socket, one after the other, the productions first; the
        data set aside is deleted. Refused when that machine does not
        answer: there is no way with it off. Asked again after the cluster
        took the writes, the instances not back yet are brought back."""
        postgres_admin(me)
        back = return_record(major)
        if not pg_known(major) or back is None or back.get("state") not in ("ready", "done"):
            return refuse("No return is ready on this machine: it is prepared first", status.HTTP_404_NOT_FOUND)
        machine = str(back.get("machine") or back.get("link") or "")
        target = dedicated_instance(major)
        one = target[2].server if target else ""
        payload = payload if isinstance(payload, dict) else {}
        if str(payload.get("confirm") or "") != machine:
            return refuse(f"Write {machine} to confirm the return")
        waiting = back["state"] == "ready"
        link = settings.link_targets.get(str(back.get("link") or ""))
        who, role = me.on_behalf or me.name, me.role
        headers = {**link_headers(link, who, role), "Accept": "application/json"} if link is not None else {}
        if waiting:
            if link is None:
                return refuse(f"The link {back.get('link')} is no longer here: the return stops the copy on {machine} "
                              "first, through it")
            there = await replica_get(link, f"/api/postgres/replica/{major}/return/state", who, role, LIST_TIMEOUT * 2)
            if "error" in there:
                return refuse(f"{machine} does not answer: the return stops its copy first, and without its answer the "
                              "two copies would diverge. Try again when it answers.", status.HTTP_409_CONFLICT)
            try:
                here = await return_local(major)
            except JobFailed as exc:
                return refuse(str(exc), status.HTTP_502_BAD_GATEWAY)
            if (not back.get("synced") or there.get("state") not in ("ready", "stopped") or not here.get("running")
                    or not (here.get("receiving") or there.get("state") == "stopped")):
                return refuse("The return is not a whole copy yet: it is completed only after it was in sync.")
        elif not promoted_instances(major):
            return refuse("The return is complete already")
        copies = await asyncio.to_thread(stagings.load, stagings_file)

        async def action(job):
            # one after the other, the productions before their stagings: who works on them waits least
            served = sorted(promoted_instances(major), key=lambda i: (i.server in copies, i.server))
            progress = job.result["progress"] = {
                "steps": [*([{"id": "stop", "label": "Stop", "where": machine},
                             {"id": "wait", "label": "Last changes", "where": here_name()},
                             {"id": "writes", "label": "Writes", "where": here_name()}] if waiting else []),
                          {"id": "instances", "label": "Instances", "where": here_name()}],
                "step": "stop" if waiting else "instances"}
            started = time.monotonic()
            told = bool(back.get("told"))
            if waiting:
                job.log(f"--- the return of the database of {one} from {machine} is completed: its copy there is stopped "
                        "first, so that nothing is written on both" if one else
                        f"--- the return from {machine} is completed: its copy is stopped first, so that nothing is "
                        "written on both")
                try:
                    stopped = await replica_remote_job(job, link, headers, f"/api/postgres/replica/{major}/return/stop",
                                                       "the stop of its copy")
                except JobFailed as exc:
                    raise JobFailed(f"{machine} answers but its copy did not stop ({exc}): nothing changed") from None
                checkpoint = str((stopped.get("return_stopped") or {}).get("checkpoint") or "")
                progress["step"] = "wait"
                try:
                    waited = await workspaceops._helper(job, "machine-db-return", major, "wait",
                                                        timeout=managedreplica.BACK_WAIT + 60,
                                                        input=json.dumps({"checkpoint": checkpoint}).encode())
                    if not waited.get("arrived"):
                        raise JobFailed(f"not everything {machine} wrote arrived here within "
                                        f"{managedreplica.BACK_WAIT} s")
                    progress["step"] = "writes"
                    done = await workspaceops._helper(job, "machine-db-return", major, "promote", timeout=MACHINE_TIMEOUT)
                except (JobFailed, asyncio.CancelledError) as exc:
                    # nothing is lost: unless this cluster took the writes all the same, that copy runs again
                    # and the instances stay there
                    try:
                        now_here = await asyncio.shield(return_local(major))
                    except JobFailed:
                        now_here = {}
                    took = now_here.get("state") == "done" or now_here.get("in_recovery") is False
                    if took and one:
                        raise JobFailed(f"{exc}. The database here took the writes: the copy on {machine} stays stopped. "
                                        f"Ask Complete the return… again to bring {one} back on it") from None
                    if took:
                        raise JobFailed(f"{exc}. This cluster took the writes: the copy on {machine} stays stopped. "
                                        "Ask Complete the return… again to bring the instances back") from None
                    try:
                        await asyncio.shield(replica_remote_job(job, link, headers,
                                                                f"/api/postgres/replica/{major}/return/resume",
                                                                "its copy started again"))
                    except JobFailed as again:
                        raise JobFailed((f"{exc}; and the copy on {machine} did not start again ({again}): the database "
                                         f"here took no write. Start that copy from {machine}, or ask Complete the "
                                         "return… again") if one else
                                        f"{exc}; and the copy on {machine} did not start again ({again}): this cluster "
                                        f"took no write. Start that copy from {machine}, or ask Complete the return… "
                                        "again") from None
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    raise JobFailed(f"{exc}: the copy on {machine} runs again and {one} stays there. Nothing is lost"
                                    if one else
                                    f"{exc}: the copy on {machine} runs again and the instances stay there. Nothing "
                                    "is lost") from None
                if one:
                    job.log(f"Everything {machine} wrote arrived here ({waited.get('seconds')} s): the database of {one} "
                            f"takes the writes here ({done.get('seconds')} s)")
                    access_log.record("action", "system", "", "", f"the database of {one} came back from its promoted "
                                      f"replica on {machine} and takes the writes (asked by {who})", context=False)
                else:
                    job.log(f"Everything {machine} wrote arrived here ({waited.get('seconds')} s): this cluster takes the "
                            f"writes ({done.get('seconds')} s). It is the machine's PostgreSQL {major} again")
                    access_log.record("action", "system", "", "", f"the machine's PostgreSQL {major} came back from its "
                                      f"promoted replica on {machine} and takes the writes (asked by {who})", context=False)
                try:
                    await replica_remote_job(job, link, headers, f"/api/postgres/replica/{major}/return/done",
                                             "its copy kept stopped")
                    told = True
                except JobFailed as exc:
                    job.warn(f"{machine} was not told that the return is complete ({exc}): it is told again every "
                             f"{REPLICA_EVERY} s. Its copy is stopped")
                replica_own.pop(major, None)
                return_seen.pop(major, None)
            progress["step"] = "instances"
            if one:
                job.log(f"--- {one} is written again as an instance with a database of its own, on the data that came "
                        "back, and starts again; no other instance of this machine is touched")
                outcome = await instances_pointed(job, served, "machine-db-replica-back", "its own database")
            else:
                job.log(f"--- the {len(served)} instances go back on this machine's cluster, by its socket, and start again")
                outcome = await instances_pointed(job, served, "machine-db-replica-back", "this machine's cluster")
            down = sorted(name for name, found in outcome.items() if not found.get("answers"))
            with contextlib.suppress(JobFailed):
                freed = await workspaceops._helper(job, "machine-db-return", major, "purge", timeout=MACHINE_TIMEOUT)
                if freed.get("freed"):
                    job.log(f"The data set aside is deleted: {int(freed.get('freed') or 0) >> 20} MB freed")
            before = int(back.get("instances") or 0)
            with contextlib.suppress(JobFailed):
                await workspaceops.ask("machine-db-return", major, "note", timeout=60, input=json.dumps(
                    {"told": told, "instances": before + len(outcome) - len(down)}).encode())
            if down and one:
                job.warn(f"{one} does not answer on its own database: Complete the return… again writes it back when it "
                         "was not")
            elif down:
                job.warn(f"{', '.join(down)} do not answer on this machine's cluster: Complete the return… again "
                         "brings back those not pointed here yet")
            if one:
                # what the workspace's users read of it: no machine, no address, no size
                answers = (outcome.get(one) or {}).get("answers")
                customer_note(target[2], target[0], f"The database of {one} is on this machine again", [
                    "The provider brought the database of this instance back from its copy on another machine: the copy "
                    "was stopped, the database here took the writes once every change had arrived, and Odoo was pointed "
                    "at it and restarted" + ("" if answers else ": it does not answer yet"),
                    "Its backups are taken on this machine again",
                    "Its recovery at a minute, when it was on before the database moved, is switched on again from the "
                    "Backups tab"], job.source)
                job.log(f"The recovery at a minute of {one}, which a promotion switches off, is not switched on by the "
                        "return: it is switched on again from its Backups tab")
                await monitor.machine_problem(replica_problem_id(major), "")
                await asyncio.to_thread(postgres_record, target[2].key, restart=None)
                job.result = {**job.result, "returned": {"machine": machine, "instances": outcome,
                                                         "seconds": round(time.monotonic() - started, 1)}}
                return
            if waiting:
                # the recovery at a minute of the cluster: its record starts again from here
                with contextlib.suppress(JobFailed):
                    archive = await workspaceops.ask("machine-db-archive-state", major, timeout=pgtuning.READ_TIMEOUT * 4)
                    if archive.get("enabled"):
                        base = await workspaceops._helper(job, "machine-db-archive-base", major, timeout=MACHINE_TIMEOUT)
                        named = f" ({base['base']})" if base.get("base") else ""
                        job.log(f"Recovery at a minute: a full copy is made now{named}, the minutes "
                                f"from here on can be reached again; those the instances spent on {machine} cannot")
            await monitor.machine_problem("replica", "")
            await asyncio.to_thread(postgres_record, f"{MACHINE_KEY}/{major}", restart=None)
            job.result = {**job.result, "returned": {"machine": machine, "instances": outcome,
                                                     "seconds": round(time.monotonic() - started, 1)}}
        job = jobs.submit(f"Complete the return of the database of {one} from {machine}" if one else
                          f"Complete the return of the machine's PostgreSQL {major} from {machine}",
                          [f"{MACHINE_KEY}/{major}"], f"manual: {me}", action)
        return {"job": job.id}

    def return_cancel_job(major: str, machine: str, link: LinkTarget | None, headers: dict, source: str, there: bool) -> Job:
        """The return cancelled on this machine: the copy deleted, the data
        set aside back where it was; then (there) the machine of the promoted
        copy asked to take its part away."""
        target = dedicated_instance(major)
        one = target[2].server if target else ""

        async def action(job):
            job.log(f"--- the return of the database of {one} from {machine} is cancelled: the copy made here is deleted "
                    "and the data of before goes back where it was" if one else
                    f"--- the return from {machine} is cancelled: the copy made here is deleted and the data of "
                    "before goes back where it was")
            done = await workspaceops._helper(job, "machine-db-return", major, "cancel", timeout=MACHINE_TIMEOUT)
            if one:
                job.log(f"The copy is deleted ({int(done.get('freed') or 0) >> 20} MB freed), with the project it ran in, "
                        f"and what {one} had here before is back as it was. {one} stays on {machine}")
            else:
                job.log(f"The copy is deleted ({int(done.get('freed') or 0) >> 20} MB freed) and the data set aside is "
                        f"back: this cluster is again the one its replica replaced, stopped. Its instances stay on {machine}")
            if there:
                await return_release(job, link, machine, major, headers)
            return_seen.pop(major, None)
            job.result = {**job.result, "return_cancelled": {"machine": machine, "freed": done.get("freed")}}
        return jobs.submit(f"Cancel the return of the database of {one} from {machine}" if one else
                           f"Cancel the return of the machine's PostgreSQL {major} from {machine}",
                           [f"{MACHINE_KEY}/{major}"], source, action)

    @app.post("/api/postgres/machine/{major}/return/cancel")
    async def return_cancel(major: str, me: Principal = Depends(current_user)):
        """Cancel the return… on the machine whose cluster comes back."""
        postgres_admin(me)
        back = return_record(major)
        if not pg_known(major) or back is None or back.get("state") not in ("copying", "ready"):
            return refuse("No return is under way on this machine", status.HTTP_404_NOT_FOUND)
        machine = str(back.get("machine") or back.get("link") or "")
        link = settings.link_targets.get(str(back.get("link") or ""))
        headers = {**link_headers(link, me.on_behalf or me.name, me.role), "Accept": "application/json"} if link else {}
        job = return_cancel_job(major, machine, link, headers, f"manual: {me}", True)
        return {"job": job.id}

    async def return_look() -> None:
        """P: what the machine of the promoted copy says of the return. One
        cancelled there: the copy made here is deleted and the data set aside
        put back. A return complete that machine has not heard of: told
        again. The stopped copy gone from there: the note of the return goes."""
        for major in replica_sources():
            back = return_record(major)
            if back is None:
                return_seen.pop(major, None)
                continue
            link = settings.link_targets.get(str(back.get("link") or ""))
            if link is None or jobs.is_busy(f"{MACHINE_KEY}/{major}"):
                continue
            machine = str(back.get("machine") or back.get("link") or "")
            headers = {**link_headers(link, "omniacli", "admin"), "Accept": "application/json"}
            there = await replica_get(link, f"/api/postgres/replica/{major}/return/state", "omniacli", "admin",
                                      LIST_TIMEOUT * 2)
            before = return_seen.get(major) or {}
            if "error" in there:
                return_seen[major] = {**before, "error": there["error"], "failed": before.get("failed") or replica_now()}
                continue
            return_seen[major] = {"at": replica_now(), "state": there, "error": ""}
            if back.get("state") in ("copying", "ready") and not there.get("exists"):
                who = str((there.get("cancelled") or {}).get("by") or "its admin")
                access_log.record("action", "system", "", "", f"the return of {pg_what(major)} was "
                                  f"cancelled on {machine} by {who}: the copy made here is deleted", context=False)
                return_cancel_job(major, machine, link, headers, f"system: cancelled on {machine} by {who}", False)
            elif back.get("state") == "done":
                if not back.get("told") and there.get("copy") == "promoted":
                    async def telling(job, link=link, headers=headers, major=major, machine=machine):
                        await replica_remote_job(job, link, headers, f"/api/postgres/replica/{major}/return/done",
                                                 "its copy kept stopped")
                        await workspaceops.ask("machine-db-return", major, "note", timeout=60,
                                               input=json.dumps({"told": True}).encode())
                    jobs.submit(f"Tell {machine} that the return of {pg_what(major)} is complete",
                                [f"{MACHINE_KEY}/{major}"], f"system: return asked by {back.get('by') or 'an admin'}", telling)
                elif there.get("copy") != "returned" and not promoted_instances(major):
                    # the stopped copy is no longer there (dropped, or made a replica again): nothing to say of it
                    with contextlib.suppress(JobFailed):
                        await workspaceops.ask("machine-db-return", major, "forget", timeout=60)

    # R: the machine that hosts the promoted copy

    def hosted_key_of(major: str, me: Principal) -> str:
        """The copy hosted here a request about a return names: its own key
        here (the major of a cluster's, d<n> of the one of an instance's
        database); or, as the machine of that instance names it (its key
        there, <workspace>.<storage>.<instance>): the copy of that
        database, of the link that asks."""
        if managedreplica.is_dedicated(major):
            for key, entry in replica_hosted().items():
                if entry.get("source") == major and (not a_machine(me) or entry.get("link") == me.name):
                    return key
        return major

    def hosted_what(major: str) -> str:
        """How a job of a return names the copy hosted here: the PostgreSQL it is of."""
        name = str((replica_hosted().get(major) or {}).get("name") or "")
        return f"the PostgreSQL of {name}" if name else f"PostgreSQL {major}"

    async def return_here(major: str, me: Principal, link_only: bool = True):
        """What the promoted copy hosted here sees of the return, when me may
        act on it (the link of the machine that comes back, or, link_only
        False, an admin of this machine too); else the refusal."""
        if link_only and not a_machine(me):
            return refuse("Asked by the machine whose cluster comes back, through its link", status.HTTP_403_FORBIDDEN)
        if replica_hosted().get(major) is None:
            return refuse(f"This machine hosts no read replica of a PostgreSQL {major}", status.HTTP_404_NOT_FOUND)
        try:
            found = await workspaceops.ask("machine-db-replica-return", major, "state", timeout=pgtuning.READ_TIMEOUT * 4)
        except JobFailed as exc:
            return refuse(str(exc), status.HTTP_502_BAD_GATEWAY)
        if a_machine(me) and found.get("exists") and found.get("link") != me.name:
            return refuse(f"The return on this copy is of another machine, not of {me.name}: it is not touched",
                          status.HTTP_403_FORBIDDEN)
        return found

    @app.get("/api/postgres/replica/{major}/return/facts")
    async def hosted_return_facts(major: str, me: Principal = Depends(current_user)):
        """What the machine whose cluster this copy replaced reads before it
        prepares its return: the size of the copy, whether it can be
        followed, the one address it lets that machine in from."""
        postgres_admin(me)
        major = hosted_key_of(major, me)
        if replica_hosted().get(major) is None:
            return refuse(f"This machine hosts no read replica of a PostgreSQL {major}", status.HTTP_404_NOT_FOUND)
        try:
            found = await workspaceops.ask("machine-db-replica-return", major, "facts", timeout=pgtuning.READ_TIMEOUT * 4)
        except JobFailed as exc:
            return refuse(str(exc))
        return {"machine": here_name(), **found}

    @app.get("/api/postgres/replica/{major}/return/state")
    async def hosted_return_state(major: str, me: Principal = Depends(current_user)):
        """What the promoted copy sees of the cluster that comes back:
        copying, in sync, behind, not connected, what it holds for it. The
        machine that comes back asks it at each look: it reads there too
        that the return was cancelled here, or that the copy is gone."""
        postgres_admin(me)
        major = hosted_key_of(major, me)
        if replica_hosted().get(major) is None:
            return {"exists": False, "copy": None, "machine": here_name()}
        found = await return_here(major, me, link_only=False)
        if not isinstance(found, dict):
            return found
        return {**found, "verdict": managedreplica.verdict(found), "machine": here_name()}

    @app.post("/api/postgres/replica/{major}/return")
    async def hosted_return_allow(major: str, payload: dict = Body(...), me: Principal = Depends(current_user)):
        """The cluster this copy replaced let in to copy it and follow it,
        asked by its machine through its link: a job of the root helper. Its
        result carries the port and the CA: no secret (the password of the
        replication stays on that machine: only its verifier comes)."""
        postgres_admin(me)
        major = hosted_key_of(major, me)
        found = await return_here(major, me)
        if not isinstance(found, dict):
            return found
        one = str((replica_hosted().get(major) or {}).get("name") or "")
        verifier = str((payload or {}).get("verifier") or "")
        if not machinedb.VERIFIER.fullmatch(verifier):
            return refuse("A return needs the SCRAM verifier of the password of its role")
        try:
            cap = managedreplica.cap_gb((payload or {}).get("cap_gb", managedreplica.CAP_GB))
        except machinedb.ClusterError as exc:
            return refuse(str(exc))
        spec = {"verifier": verifier, "link": me.name, "by": me.on_behalf or me.name, "cap_gb": cap}

        async def action(job):
            job.log(f"--- the database of {one} this promoted copy replaced comes back to {me.name}: that machine is "
                    "let in for the replication, over TLS, from the one address it already comes from. Nothing restarts"
                    if one else
                    f"--- the cluster this promoted copy replaced comes back: it is let in for the replication, over "
                    "TLS, from the one address it already comes from. Nothing restarts")
            made = await workspaceops._helper(job, "machine-db-replica-return", major, "allow", timeout=MACHINE_TIMEOUT,
                                              input=json.dumps(spec).encode())
            job.log(f"A role for the replication and a place that holds the changes for the copy {me.name} makes are "
                    f"there: this copy keeps {cap:g} GB of them at most; pg_hba.conf lets {made.get('address')} in for "
                    "the replication too" if one else
                    f"A role for the replication and a place that holds the changes for that cluster are there: this "
                    f"copy keeps {cap:g} GB of them at most; pg_hba.conf lets {made.get('address')} in for the "
                    "replication too")
            job.result = {**job.result, "return": {k: made.get(k) for k in (
                "port", "host", "ca", "size", "tablespaces", "version", "address", "cap_gb")}}
        job = jobs.submit(f"Return of {hosted_what(major)} to {me.name}: let in", [replica_key(major)], f"manual: {me}",
                          action)
        return {"job": job.id}

    @app.post("/api/postgres/replica/{major}/return/{step}")
    async def hosted_return_step(major: str, step: str, me: Principal = Depends(current_user)):
        """stop, resume, done: the steps of Complete the return… on this
        machine's side, asked by the machine whose cluster comes back through
        its link (the copy stopped clean; started again when that cluster
        did not get everything; kept stopped for good). cancel: what this
        copy keeps for the return taken away, asked by that machine or by an
        admin of this one."""
        postgres_admin(me)
        if step not in ("stop", "resume", "done", "cancel"):
            return refuse("stop, resume, done or cancel a return", status.HTTP_404_NOT_FOUND)
        major = hosted_key_of(major, me)
        found = await return_here(major, me, link_only=step != "cancel")
        if not isinstance(found, dict):
            return found
        name = str((replica_hosted().get(major) or {}).get("link") or "")
        # of one instance's database (UX-011 §A for one instance): the instance that goes back, not instances
        one = str((replica_hosted().get(major) or {}).get("name") or "")
        who = me.on_behalf or me.name
        if step == "cancel" and not found.get("exists"):
            return {"ok": True, "gone": True}
        if step == "cancel" and found.get("state") == "stopped":
            return refuse(f"The return is being completed: this copy is stopped for it and {name} takes the writes. "
                          "It is not started again from here")

        async def action(job):
            if step == "stop":
                job.log(f"--- the return to {name} is completed: this copy is stopped first, so that nothing is "
                        "written on both")
                done = await workspaceops._helper(job, "machine-db-replica-return", major, "stop", timeout=MACHINE_TIMEOUT)
                job.log(f"This copy is stopped ({done.get('seconds', '?')} s): {name} takes the writes once it has "
                        "applied the last thing written here")
                job.result = {**job.result, "return_stopped": {"checkpoint": done.get("checkpoint"),
                                                               "at": done.get("stopped_at")}}
            elif step == "resume":
                done = await workspaceops._helper(job, "machine-db-replica-return", major, "resume", timeout=MACHINE_TIMEOUT)
                job.log("This copy was not stopped for a return" if not done.get("resumed") else
                        f"{name} did not get everything in time: this copy runs again and {one} stays here. "
                        "Nothing is lost" if one else
                        f"{name} did not get everything in time: this copy runs again and the instances stay here. "
                        "Nothing is lost")
                job.result = {**job.result, "return_resumed": bool(done.get("resumed"))}
            elif step == "done":
                done = await workspaceops._helper(job, "machine-db-replica-return", major, "done", timeout=MACHINE_TIMEOUT)
                job.log(f"{name} took the writes: this copy stays stopped, with the data of that moment. It never "
                        "starts by itself: it is made a replica again, or dropped")
                access_log.record("action", "system", "", "", f"the promoted copy of {hosted_what(major)} of {name} is "
                                  f"stopped for good: {name} took the writes back (asked by {who})", context=False)
                replica_seen.pop(major, None)
                job.result = {**job.result, "returned_at": done.get("returned_at")}
            else:
                done = await workspaceops._helper(job, "machine-db-replica-return", major, "cancel", timeout=MACHINE_TIMEOUT,
                                                  input=json.dumps({"by": who}).encode())
                job.log(f"What this copy kept for the return of {name} is gone: the role of the replication, its line "
                        f"of pg_hba.conf, the place that held its changes ({int(done.get('freed') or 0) >> 20} MB of "
                        "them, freed)")
                if not a_machine(me):
                    job.log(f"{name} asks this machine every {REPLICA_EVERY} s: it deletes the copy it made and puts "
                            "its data of before back at its next look")
                job.result = {**job.result, "return_cancelled": {"freed": done.get("freed")}}
        what = hosted_what(major)
        title = {"stop": f"Stop the promoted copy of {what}: {name} comes back",
                 "resume": f"Start the promoted copy of {what} again: the return to {name} did not end",
                 "done": f"The return of {what} to {name} is complete: its copy here stays stopped",
                 "cancel": f"Cancel the return of {what} to {name}"}[step]
        job = jobs.submit(title, [replica_key(major)], f"manual: {me}", action)
        return {"job": job.id}

    @app.post("/api/postgres/replica/{major}/remake")
    async def replica_remake(major: str, payload: Optional[dict] = Body(None), me: Principal = Depends(current_user)):
        """Make the replica again… (UX-011 §A): the stopped copy the cluster
        came back from is deleted, and a new replica of that cluster is made
        here, kept up to date. One job."""
        postgres_admin(me)
        payload = payload if isinstance(payload, dict) else {}
        entry = replica_hosted().get(major)
        if entry is None:
            return refuse(f"This machine hosts no read replica of a PostgreSQL {major}", status.HTTP_404_NOT_FOUND)
        name = str(entry.get("link") or "")
        link = settings.link_targets.get(name)
        if link is None:
            return refuse(f"No link {name!r}", status.HTTP_404_NOT_FOUND)
        if linkedcluster.unprotected(link):
            return refuse(f"The link with {link.name} is plain HTTP: a replica asks for HTTPS or SSH, because what "
                          "it answers says where the data comes from.")
        try:
            here = await workspaceops.ask("machine-db-replica-hosted", major, timeout=pgtuning.READ_TIMEOUT * 4)
        except JobFailed as exc:
            return refuse(str(exc), status.HTTP_502_BAD_GATEWAY)
        if here.get("state") != "returned":
            return refuse(f"The copy is not one {name} came back from: it is made again only then")
        # of the database of one instance (UX-011 §D): as its machine names it, and the instance
        there, one = replica_there(major, entry), str(entry.get("name") or "")
        try:
            source = machinedb.address(here.get("address"))
            host = machinedb.reached_as([linkedcluster.host_of(link)])[0]
            # of a dedicated database, what its machine says: a tenth of its workspace's disk at most
            cap = (managedreplica.cap_gb(payload.get("cap_gb", managedreplica.CAP_GB))
                   if not one or payload.get("cap_gb") not in (None, "") else None)
            reserve = managedreplica.reserve_gb(payload.get("reserve_gb", managedreplica.RESERVE_GB))
        except machinedb.ClusterError as exc:
            return refuse(str(exc))
        storage = str(entry.get("storage") or replica_offer()["storage"])
        who, role = me.on_behalf or me.name, me.role
        headers = {**link_headers(link, who, role), "Accept": "application/json"}
        facts = await replica_get(link, f"/api/postgres/machine/{there}/replica/facts", who, role, LIST_TIMEOUT * 4)
        if "error" in facts:
            return refuse(f"{link.name} does not answer: {facts['error']}. The replica is made through the link: try "
                          "again when it answers.", status.HTTP_502_BAD_GATEWAY)
        if facts.get("replica"):
            return refuse((f"The PostgreSQL of {one} on {link.name} has a read replica already, on "
                           f"{(facts['replica'] or {}).get('link')}: one per database.") if one else
                          f"The cluster of {link.name} has a read replica already, on "
                          f"{(facts['replica'] or {}).get('link')}: drop it first, from there or from {link.name}.")
        if facts.get("why") or facts.get("restarts"):
            return refuse(f"{link.name}: " + (facts.get("why") or (
                f"the PostgreSQL of {one} listens on no port any more: drop this copy, and make the replica with "
                "+ Replica of another machine…, which says its restart" if one else
                "its cluster listens on no port any more")))
        size = int(facts.get("size") or 0)
        # the stopped copy goes first: its room is free for the new one
        free = managedreplica.free_here(storage) + int(here.get("disk") or 0)
        fits = managedreplica.room(size, free, reserve)
        if not fits["fits"]:
            return refuse(f"No room: the copy of the {'database of ' + one if one else 'cluster'} of {link.name} takes "
                          f"about {fits['need'] >> 20} MB, "
                          f"{free / workspaceops.GB:.1f} GB are free here and {reserve:g} GB must stay free.",
                          status.HTTP_507_INSUFFICIENT_STORAGE)

        async def action(job):
            job.log(f"--- the replica of {one} of {name} is made again: the stopped copy, of the moment its database "
                    f"went back to {name}, is deleted first" if one else
                    f"--- the replica of {name} is made again: the stopped copy, of the moment {name} came back, is "
                    "deleted first")
            done = await workspaceops._helper(job, "machine-db-replica-remove", major, timeout=MACHINE_TIMEOUT)
            replica_seen.pop(major, None)
            replica_synced.pop(major, None)
            job.log(f"The stopped copy and its container are deleted: {int(done.get('freed') or 0) >> 20} MB freed")
            # the new copy takes the key the stopped one had
            await replica_made(job, link, there, {"source": source, "host": host, "cap": cap, "reserve": reserve,
                                                  "storage": storage, "size": size, "restart": False,
                                                  **({"key": major, "name": one} if one else {})}, who, role)
        job = jobs.submit(f"Make the read replica of {replica_title(major, entry)} again", [replica_key(major)],
                          f"manual: {me}", action)
        return {"job": job.id}

    # --- the databases other machines use, after a promotion (UX-011 §B). A is the machine whose
    # instance uses a database of P's cluster through its link to P. The promoted copy on R lets the
    # same roles in from the same addresses; A's instance is pointed to where its database is now by
    # itself, and only when its database does not answer where it was: A asks P through its link (P
    # never calls A, R never calls A), every MOVED_EVERY seconds. P's side: GET
    # /api/postgres/machine/<major>/grants/<name>/place, POST /api/postgres/machine/<major>/followed/<name>. R's side: POST
    # /api/postgres/replica/<major>/grants/<name>/revoke|allow|drop. A's side: the look (moved_loop).

    MOVED_EVERY = 10
    MOVED_SOON = 5
    MOVED_AFTER = 60
    MOVED_ASK_EVERY = 60
    # A: what the look saw last of each instance on a linked cluster, by server; P: the CA of its
    # cluster (root reads it, once), and what it told each machine and what each said it did
    moved_seen: dict[str, dict] = {}
    cluster_cas: dict[str, str] = {}
    moved_file = settings.jobs_dir.parent / "database_moved.json"

    def moved_notes() -> dict:
        try:
            data = json.loads(moved_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def moved_note(major: str, name: str, **values) -> None:
        data = moved_notes()
        data.setdefault(major, {}).setdefault(name, {}).update(values)
        moved_file.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")

    def grant_of_link(major: str, name: str, me: Principal):
        """The grant name of this machine's cluster, when me is the link of
        the machine it was given to (its record, a file OmniaCLI reads);
        else the refusal."""
        if not a_machine(me):
            return refuse("Asked by the machine that uses the database, through its link", status.HTTP_403_FORBIDDEN)
        found = machinedb.registry().get(major) if machinedb.ENV_NAME.fullmatch(name) else None
        grant = machinedb.grants(Path(str(found.get("project") or ""))).get(name) if found else None
        if grant is None:
            return refuse(f"{name} is no database of another machine on PostgreSQL {major}", status.HTTP_404_NOT_FOUND)
        if str(grant.get("link") or "") != me.name:
            return refuse(f"{name} is a database of another machine here, not of {me.name}", status.HTTP_403_FORBIDDEN)
        return grant

    @app.get("/api/postgres/machine/{major}/grants/{name}/place")
    async def postgres_grant_place(request: Request, major: str, name: str, me: Principal = Depends(current_user)):
        """Where a database of this cluster another machine uses is now,
        asked by that machine through its link (UX-011 §B): on this
        cluster, or on the promoted copy that replaced it, with the address
        and the CA its certificate is verified against (no secret). The
        first time each place is told is in the access log."""
        postgres_admin(me)
        grant = grant_of_link(major, name, me)
        if not isinstance(grant, dict):
            return grant
        rec = replica_record(major) or {}
        if rec.get("state") == "replaced" and rec.get("host"):
            answer = {"place": "copy", "host": str(rec["host"]), "port": int(rec.get("port") or 0), "ca": rec.get("ca"),
                      "machine": str(rec.get("link") or ""), "since": rec.get("replaced_at")}
        else:
            if major not in cluster_cas:
                state = await cluster_state(major)
                if state is None:
                    return refuse(f"The machine's PostgreSQL {major} cannot be read now: try again in a moment",
                                  status.HTTP_503_SERVICE_UNAVAILABLE)
                if state.get("ca"):
                    cluster_cas[major] = str(state["ca"])
            found = machinedb.registry().get(major) or {}
            port = (machinedb.listening(Path(str(found.get("project") or ""))) or {}).get("port")
            answer = {"place": "cluster", "host": None, "port": port, "ca": cluster_cas.get(major), "machine": None,
                      "since": (return_record(major) or {}).get("returned_at")}
        told = f"{answer['place']}:{answer['host'] or ''}:{answer['port'] or ''}"
        note = (moved_notes().get(major) or {}).get(name) or {}
        if note.get("told") != told and (answer["place"] == "copy" or note.get("told")):
            # the question comes every few seconds: said once, when the answer changes
            where = (f"on the promoted copy on {answer['machine']} ({answer['host']}:{answer['port']})"
                     if answer["place"] == "copy" else "on this machine's cluster again")
            access_log.record("action", me.kind, me.name, request.client.host if request.client else "",
                              f"database {name} of PostgreSQL {major}: {me.name} told that it is {where}"
                              + (f" for {me.on_behalf}" if me.on_behalf else ""))
            await asyncio.to_thread(moved_note, major, name, told=told, told_at=replica_now(), followed=None)
        return {**answer, "revoked": bool(grant.get("revoked")), "here": here_name()}

    @app.post("/api/postgres/machine/{major}/followed/{name}")
    async def postgres_grant_followed(major: str, name: str, payload: dict = Body(...), me: Principal = Depends(current_user)):
        """The machine that uses a database of this cluster says its instance
        was pointed to where the database is now: what this machine's
        Database tab says of it."""
        postgres_admin(me)
        grant = grant_of_link(major, name, me)
        if not isinstance(grant, dict):
            return grant
        to = str((payload or {}).get("to") or "")[:120]
        await asyncio.to_thread(moved_note, major, name, followed={"to": to, "at": replica_now(),
                                                                   "answers": bool((payload or {}).get("answers"))})
        return {"ok": True}

    def grants_followed(major: str) -> dict:
        """P, for its rows: {database: {"to", "at", "answers"}} of the machines that said they followed."""
        return {name: note["followed"] for name, note in (moved_notes().get(major) or {}).items()
                if isinstance(note.get("followed"), dict)}

    def grants_waited(major: str) -> list[str]:
        """P: the databases other machines use whose instance has not followed
        to the promoted copy yet (a revoked one, or one whose link is no
        longer let in here, waits for no one)."""
        rec = replica_record(major) or {}
        found = machinedb.registry().get(major) or {}
        if rec.get("state") != "replaced":
            return []
        where, followed = f"{rec.get('host')}:{rec.get('port')}", grants_followed(major)
        return sorted(name for name, grant in machinedb.grants(Path(str(found.get("project") or ""))).items()
                      if not grant.get("revoked") and str(grant.get("link") or "") in settings.links
                      and (followed.get(name) or {}).get("to") != where)

    @app.post("/api/postgres/replica/{major}/grants/{name}/{what}")
    async def hosted_ungrant(major: str, name: str, what: str, me: Principal = Depends(current_user)):
        """A database of the promoted copy another machine uses (the cluster
        it replaced had given it): revoke, allow, drop, as on a cluster."""
        postgres_admin(me)
        if what not in ("revoke", "allow", "drop") or not machinedb.ENV_NAME.fullmatch(name):
            return refuse("revoke, allow or drop a database of another machine", status.HTTP_404_NOT_FOUND)
        entry = replica_hosted().get(major)
        if entry is None or a_machine(me):
            return refuse(f"This machine hosts no promoted copy of a PostgreSQL {major} for you",
                          status.HTTP_404_NOT_FOUND)
        whose = str(entry.get("link") or "")

        async def action(job):
            done = await workspaceops._helper(job, "machine-db-replica-grant", major, name, what, timeout=MACHINE_TIMEOUT)
            source = done.get("address")
            if what == "revoke":
                job.log(f"Database {name} is no longer reached from {source}: its line of pg_hba.conf is gone, "
                        f"{done.get('closed')} connections closed. The database and its role stay: Allow again, or Drop")
            elif what == "allow":
                job.log(f"Database {name} is reached from {source} again: its line of pg_hba.conf is back")
            else:
                job.log(f"Database {name} dropped, with its role and its line of pg_hba.conf")
            job.log(firewall_line(done))
            job.result = {**job.result, "ungrant": {**done, "what": what}}
        title = {"revoke": f"Revoke database {name} on the promoted copy of PostgreSQL {major} of {whose}",
                 "allow": f"Allow database {name} on the promoted copy of PostgreSQL {major} of {whose} again",
                 "drop": f"Drop database {name} on the promoted copy of PostgreSQL {major} of {whose}"}[what]
        job = jobs.submit(title, [replica_key(major)], f"manual: {me}", action)
        return {"job": job.id}

    # A: the machine whose instance uses the database

    def linked_instances() -> list[Instance]:
        """The productions of this machine whose database is on the cluster of a linked machine."""
        return [srv.production for srv in settings.active_servers()
                if srv.production is not None and srv.production.restart.type == "compose"
                and on_remote_db(srv.production) and linked_database(srv.production) is not None
                and not instance_meta(srv.production).get("promoted_from")]

    def hosted_here(place: str) -> bool:
        """place (host:port) is a promoted copy this very machine hosts."""
        for entry in replica_hosted().values():
            rec = managedreplica.hosted_record(Path(str(entry.get("project") or "")))
            if rec.get("state") == "promoted" and place == f"{rec.get('address')}:{rec.get('own_port')}":
                return True
        return False

    async def database_answers(host: str, port: int, ca: str, name: str) -> str:
        """"" when the database name answers at host:port as Odoo will ask
        it (TLS, the certificate verified against ca, its role let in); else
        why not."""
        try:
            await asyncio.to_thread(linkedcluster.check, host, int(port), ca, name, name)
        except linkedcluster.CheckFailed as exc:
            return str(exc)
        return ""

    def moved_job(inst: Instance, ws: Workspace, instance: str, link: LinkTarget, major: str, where: dict, since: str) -> Job:
        """The job that points an instance to where its database is now and
        starts it again. Its texts name no machine and no address: the
        workspace's users read it."""
        server, name = inst.server, inst.options.database

        async def action(job):
            job.log(f"--- the database of {server} does not answer where it was since {since[11:16]}, and the machine "
                    "that has it says where it is now: Odoo is pointed there, over TLS with the certificate verified, "
                    "and starts again")
            made = await workspaceops._helper(job, "machine-db-moved", ws.name, ws.storage, instance,
                                              timeout=MACHINE_TIMEOUT, input=json.dumps(where).encode())
            if made.get("already"):
                job.log("It was pointed there already")
            now_inst = settings.servers[server].production if server in settings.servers else inst
            answers, seconds = await replica_answers(now_inst)
            if answers:
                job.log(f"{server} answers again after {seconds} s, on its database where it is now")
            else:
                job.warn(f"{server} does not answer {seconds} s after it was pointed to where its database is now")
            moved_seen.pop(server, None)
            # the machine that has the database is told: its Database tab says who followed
            try:
                await link_clients.client(link).post(
                    f"/api/postgres/machine/{major}/followed/{name}", timeout=LIST_TIMEOUT * 2,
                    headers={**link_headers(link, "omniacli", "admin"), "Accept": "application/json"},
                    json={"to": f"{where['host']}:{where['port']}", "answers": answers})
            except httpx.HTTPError:
                pass
            job.result = {**job.result, "moved": {"server": server, "answers": answers, "seconds": seconds}}
        return jobs.submit(f"Database of {server} moved: Odoo follows it", [inst.key],
                           f"automatic: database moved by {link.name}", action, workspace=ws.name)

    async def moved_look() -> bool:
        """A: each instance on the cluster of a linked machine. Its database
        answers where it is: nothing (its machine is asked now and then,
        for the pages). It has not answered for MOVED_AFTER seconds, its
        machine says it is somewhere else now, and it answers there with
        its role: the instance is pointed there and restarted, by a job. One
        of the three missing: nothing moves, and the page says which. True
        when one does not answer (the next look comes sooner)."""
        waiting = False
        for inst in linked_instances():
            server, name, meta = inst.server, inst.options.database, instance_meta(inst)
            host, _, port = str(meta.get("database_on") or "").rpartition(":")
            if not host or not port.isdigit() or jobs.is_busy(inst.key):
                continue
            seen = moved_seen.setdefault(server, {})
            try:
                ca = (Path(inst.restart.service) / "config" / workspacehelper.REMOTE_CA_FILE).read_text(encoding="utf-8")
            except OSError:
                ca = ""
            why = await database_answers(host, int(port), ca, name)
            now = time.monotonic()
            if not why:
                for key in ("down", "since", "why", "new_why"):
                    seen.pop(key, None)
            else:
                seen.setdefault("down", now)
                seen.setdefault("since", replica_now())
                seen["why"] = why
                waiting = True
            if not why and now - seen.get("asked", -MOVED_ASK_EVERY) < MOVED_ASK_EVERY:
                continue
            seen["asked"] = now
            link = settings.link_targets.get(link_now(inst))
            major = str(meta.get("database_major") or "")
            if link is None or linkedcluster.unprotected(link):
                # what the link answers says where Odoo connects and which CA it trusts: never over plain HTTP
                seen.update(says=None, says_error="no link over HTTPS or SSH leads to the machine of its database any more",
                            elsewhere=False)
                continue
            says = await replica_get(link, f"/api/postgres/machine/{major}/grants/{name}/place", "omniacli", "admin",
                                     LIST_TIMEOUT * 2)
            if "error" in says:
                seen.update(says=None, says_error=says["error"], elsewhere=False)
                continue
            there = (str(says.get("host") or linkedcluster.host_of(link)), int(says.get("port") or 0))
            seen.update(says={"place": says.get("place"), "machine": says.get("machine") or ""}, says_error="",
                        elsewhere=bool(there[1]) and there != (host, int(port)))
            if not why or not seen["elsewhere"] or now - seen["down"] < MOVED_AFTER or not says.get("ca"):
                continue
            new_why = await database_answers(there[0], there[1], str(says["ca"]), name)
            if new_why:
                seen["new_why"] = new_why
                continue
            found = workspace_of(server)
            if found is not None:
                moved_job(inst, found[0], found[1], link, major, {"host": there[0], "port": there[1], "ca": says["ca"],
                                                                  "machine": says.get("machine") or ""}, seen["since"])
        for server in [s for s in moved_seen if s not in settings.servers]:
            moved_seen.pop(server, None)
        return waiting

    async def moved_loop() -> None:
        while True:
            pause = MOVED_EVERY
            try:
                if settings.storage_roots and await moved_look():
                    pause = MOVED_SOON
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the loop must survive
                _logger.exception("the look at the databases on linked clusters failed")
            await asyncio.sleep(pause)

    # --- the look of every REPLICA_EVERY seconds, on both sides

    def replica_brief(major: str) -> dict | None:
        """P, for the map: where the replica of the cluster is and how it is, as the look saw it last."""
        rec = replica_record(major)
        if rec is None:
            return None
        verdict = (((replica_own.get(major) or {}).get("state") or {}).get("verdict") or {})
        return {"machine": rec.get("link"), "code": "replaced" if rec.get("state") == "replaced" else verdict.get("code") or "",
                "lag": verdict.get("lag"), "since": rec.get("replaced_at") or verdict.get("since")}

    def replica_briefs() -> list[dict]:
        """R, for the map: the copies hosted here, of a cluster or of one
        instance's own database (name: that instance, as its machine names
        it), each as its primary's machine said last."""
        out = []
        fences = replica_fences()
        for major, entry in sorted(replica_hosted().items()):
            seen = replica_seen.get(major) or {}
            verdict = ((seen.get("state") or {}).get("verdict") or {})
            # promoted: that machine's cluster says it was replaced, or has not heard of it yet (the retry is on)
            promoted = major in fences or (seen.get("state") or {}).get("state") == "replaced"
            # the cluster it replaced came back: stopped (its own record, a file OmniaCLI reads)
            returned = managedreplica.hosted_record(Path(str(entry.get("project") or ""))).get("state") == "returned"
            out.append({"major": major, "link": entry.get("link"), "name": entry.get("name") or None, "lag": verdict.get("lag"),
                        "code": "returned" if returned else "promoted" if promoted else "silent" if seen.get("error")
                        else verdict.get("code") or ""})
        return out

    def replica_watch() -> list[tuple[str, str | None]]:
        """What is to look at of the replicas, for the map's verdict: (text, since)."""
        found = []
        for major in replica_sources():
            state = (replica_own.get(major) or {}).get("state") or {}
            verdict = state.get("verdict") or {}
            if verdict.get("warn"):
                # since when is the entry's own, written by the page as every date is
                who, held = state.get("link"), int(verdict.get("held") or 0) / managedreplica.GB
                whose = pg_what(major)
                found.append((f"the read replica on {who} lost its place: it is dropped and made again"
                              if verdict.get("code") == "lost" else
                              f"the read replica on {who} is not connected: {whose} keeps {held:.1f} GB "
                              "of changes for it" if verdict.get("code") == "off" else
                              f"{whose} keeps {held:.1f} GB of changes for the read replica on {who}",
                              verdict.get("since")))
        for major, entry in sorted(replica_hosted().items()):
            verdict = (((replica_seen.get(major) or {}).get("state") or {}).get("verdict") or {})
            if verdict.get("code") in ("off", "lost"):
                found.append(((f"the read replica of {entry.get('name')} of {entry.get('link')} hosted here does not "
                               "receive its changes") if entry.get("name") else
                              f"the read replica of {entry.get('link')} hosted here does not receive its changes",
                              verdict.get("since")))
        return found

    async def replica_look_hosted() -> None:
        """R: what the primary's machine says of each replica hosted here.
        Dropped there: the copy is deleted here. Its instances or its
        tablespaces changed: pg_hba.conf and the folders follow. Promoted
        while it did not answer: the stop and the pointing tried again."""
        fences = replica_fences()
        for major, entry in sorted(replica_hosted().items()):
            name = str(entry.get("link") or "")
            link = settings.link_targets.get(name)
            if link is None or jobs.is_busy(replica_key(major)):
                continue
            fence = fences.get(major)
            who = str((fence or {}).get("who") or "omniacli")
            role = str((fence or {}).get("role") or "admin")
            there_key = replica_there(major, entry)
            there = await replica_ask(link, there_key, who, role, LIST_TIMEOUT * 2)
            before = replica_seen.get(major) or {}
            if "error" in there:
                replica_seen[major] = {**before, "error": there["error"], "failed": before.get("failed") or replica_now()}
                continue
            replica_seen[major] = {"at": replica_now(), "state": there, "error": ""}
            if fence:
                headers = {**link_headers(link, who, role), "Accept": "application/json"}

                async def fencing(job, link=link, major=major, headers=headers, name=name, there_key=there_key):
                    job.log(f"--- {name} answers again after the promotion of its replica: its PostgreSQL is stopped "
                            "and its instances are pointed here")
                    await replica_point(job, link, there_key, headers)
                    replica_fence_set(major, None)
                jobs.submit(f"Point the instances of {name} at its promoted replica (it answers again)",
                            [replica_key(major)], f"system: promotion asked by {who}", fencing)
                continue
            if there.get("mine") is False or not there.get("exists"):
                try:
                    here = await workspaceops.ask("machine-db-replica-hosted", major, timeout=60)
                except JobFailed:
                    continue
                if here.get("state") != "ready":
                    continue  # being made (its job runs), or promoted: never deleted by a look

                async def gone(job, major=major, name=name):
                    job.log(f"--- {name} dropped what it kept for this replica: the copy follows nothing any more")
                    done = await workspaceops._helper(job, "machine-db-replica-remove", major, timeout=MACHINE_TIMEOUT)
                    replica_seen.pop(major, None)
                    replica_synced.pop(major, None)
                    job.log(f"The copy and its container are deleted: {int(done.get('freed') or 0) >> 20} MB freed")
                jobs.submit(f"Drop the read replica of {replica_title(major, entry)} (dropped there)",
                            [replica_key(major)], f"system: dropped on {name}", gone)
                continue
            given = there.get("grant_lines") if isinstance(there.get("grant_lines"), dict) else None
            if there.get("state") == "replaced" and given is not None:
                # promoted before it took the databases that machine had given to others (a release that
                # did not): it takes them now, once
                rec = managedreplica.hosted_record(Path(str(entry.get("project") or "")))
                if rec.get("state") == "promoted" and "lent_at" not in rec:
                    try:
                        done = await workspaceops.ask("machine-db-replica-lent", major, timeout=120,
                                                      input=json.dumps({"grants": given}).encode())
                    except JobFailed as exc:
                        _logger.warning("the promoted copy of %s could not take its primary's grants: %s", name, exc)
                        continue
                    if done.get("adopted"):
                        access_log.record("action", "system", "", "", f"promoted copy of PostgreSQL {major} of {name}: "
                                          f"the databases other machines use on it ({', '.join(done.get('lent') or []) or 'none'}) "
                                          "are let in from the same addresses", context=False)
                continue
            wanted = (tuple(there.get("environments") or []), tuple(there.get("tablespaces") or []),
                      json.dumps(given, sort_keys=True))
            if there.get("state") == "ready" and replica_synced.get(major) != wanted:
                try:
                    done = await workspaceops.ask("machine-db-replica-sync", major, timeout=120, input=json.dumps(
                        {"environments": list(wanted[0]), "tablespaces": list(wanted[1]),
                         **({"grants": given} if given is not None else {})}).encode())
                except JobFailed as exc:
                    _logger.warning("the read replica of %s could not follow its instances: %s", name, exc)
                    continue
                replica_synced[major] = wanted
                if done.get("changed"):
                    _logger.warning("read replica of PostgreSQL %s of %s: %s written again for what %s has now",
                                    major, name, ", ".join(done["changed"]), name)
                    access_log.record("action", "system", "", "", f"read replica of PostgreSQL {major} of {name}: "
                                      f"{', '.join(done['changed'])} written again for the instances it has now",
                                      context=False)

    async def replica_look_own() -> None:
        """P: what this machine sees of the replica of each PostgreSQL of its
        own (its cluster, the dedicated databases that have one), kept for
        the pages; the warning out of the panel (UX-010 D4) when it is not
        connected for ten minutes or the PostgreSQL keeps more than 1 GB for it."""
        for major in replica_sources():
            if replica_record(major) is None:
                if replica_own.pop(major, None) is not None:
                    await monitor.machine_problem(replica_problem_id(major), "")
                continue
            try:
                found = await replica_state_here(major)
            except JobFailed as exc:
                _logger.warning("the read replica of %s cannot be read: %s", pg_what(major), exc)
                continue
            await monitor.machine_problem(replica_problem_id(major), managedreplica.problem(
                found, str(found.get("link") or ""), whose=pg_what(major)))

    async def replica_loop() -> None:
        while True:
            pause = REPLICA_EVERY
            try:
                if settings.storage_roots:
                    await replica_look_own()
                    await replica_look_hosted()
                    await return_look()
                    if replica_fences():
                        pause = REPLICA_RETRY
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the loop must survive
                _logger.exception("the look at the read replicas failed")
            await asyncio.sleep(pause)

    async def stop_replicas() -> None:
        for task in replica_tasks:
            task.cancel()
        await asyncio.gather(*replica_tasks, return_exceptions=True)
        replica_tasks.clear()

    def linked_refusal(link: LinkTarget, resp: httpx.Response, major: str) -> str:
        """Why the linked machine did not take a request about its cluster."""
        if resp.status_code == 401:
            return f"{link.name} refused the link's token: ask it for a new one"
        if resp.status_code == 403:
            return (f"{link.name} refused: a database on its cluster needs the admin role there, for the link "
                    "and for you")
        try:
            said = resp.json().get("error") or resp.json().get("detail") or ""
        except (ValueError, AttributeError):
            said = ""
        if resp.status_code in (404, 405) and not said:
            return (f"{link.name} has no databases for other machines: its OmniaCLI is older than this one, "
                    f"or it has no cluster of PostgreSQL {major}")
        return f"{link.name}: {said or f'HTTP {resp.status_code}'}"

    @app.get("/api/links/{name}/postgres")
    async def link_postgres(name: str, me: Principal = Depends(current_user)):
        """What New instance shows before a database is asked of the cluster
        of a linked OmniaCLI: its clusters, how this machine reaches it, the
        round trip measured now, the address this machine goes out with."""
        postgres_admin(me)
        link = settings.link_targets.get(name)
        if link is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No link {name}")
        host = linkedcluster.host_of(link)
        why = linkedcluster.unprotected(link)
        if why:
            return {"link": name, "host": host, "clusters": [], "error": why}
        try:
            resp = await link_clients.client(link).get(
                "/api/postgres/machine/offer", timeout=LIST_TIMEOUT * 4,
                headers={**link_headers(link, me.on_behalf or me.name, me.role), "Accept": "application/json"})
        except httpx.HTTPError as exc:
            return {"link": name, "host": host, "clusters": [], "error": f"{name}: {describe_error(exc)}"}
        if resp.status_code != 200:
            return {"link": name, "host": host, "clusters": [], "error": linked_refusal(link, resp, "?")}
        try:
            clusters = [c for c in resp.json().get("clusters") or [] if isinstance(c, dict)]
        except (ValueError, AttributeError):
            clusters = []
        ports = [c["port"] for c in clusters if isinstance(c.get("port"), int) and 0 < c["port"] < 65536]
        ports.append(linkedcluster.link_port(link))
        found = await asyncio.to_thread(linkedcluster.measure, host, ports)
        return {"link": name, "host": host, "clusters": clusters, "error": None,
                "address": found["address"], "ms": found["ms"], "measured_on": found["port"],
                "tries": found["tries"], "warn_ms": linkedcluster.WARN_MS, "queries": linkedcluster.PAGE_QUERIES,
                "page_s": linkedcluster.page_seconds(found["ms"]) if found["ms"] is not None else None,
                # a private address here and a public one there: this machine goes out through another one
                "nat": bool(found["address"]) and not linkedcluster.public_address(found["address"])
                and linkedcluster.public_address(host)}

    def linked_request(ws: Workspace, instance: str, asked, me: Principal):
        """What a new instance on the cluster of a linked OmniaCLI needs,
        checked before its job: the link (protected), the PostgreSQL, the
        address this machine is seen with there, the name of its role and
        database (AD-002's); or the refusal."""
        if not isinstance(asked, dict):
            return refuse("linked: the link, its PostgreSQL and the address this machine is seen with there")
        link = settings.link_targets.get(str(asked.get("link") or ""))
        if link is None:
            return refuse(f"No link {asked.get('link')!r}", status.HTTP_404_NOT_FOUND)
        why = linkedcluster.unprotected(link)
        if why:
            return refuse(why)
        major = str(asked.get("major") or "")
        if not re.fullmatch(r"\d{1,3}", major):
            return refuse(f"{major!r} is not a PostgreSQL of {link.name}")
        try:
            source = machinedb.address(asked.get("address"))
            name = machinedb.env_name(ws.name, instance)
            host = machinedb.reached_as([linkedcluster.host_of(link)])[0]
        except machinedb.ClusterError as exc:
            return refuse(str(exc))
        return {"link": link.name, "major": major, "address": source, "name": name, "host": host,
                "who": me.on_behalf or me.name, "role": me.role, "workspace": ws.name, "instance": instance}

    async def linked_grant(job, linked: dict) -> dict:
        """The database asked of the linked machine (its job followed here),
        then reached as Odoo will: the connection of the instance
        (workspacehelper.remote_spec), with the password made here. One that
        cannot be reached is given back."""
        link = settings.link_targets.get(linked["link"])
        if link is None:
            raise JobFailed(f"the link {linked['link']} is no longer here")
        major, name, host = linked["major"], linked["name"], linked["host"]
        password = stacktemplates.new_password()
        client = link_clients.client(link)
        headers = {**link_headers(link, linked["who"], linked["role"]), "Accept": "application/json"}
        job.log(f"--- its database on PostgreSQL {major} of {link.name}: role and database {name}, let in from "
                f"{linked['address']} only, over TLS; its password is made here and is not sent")
        try:
            resp = await client.post(f"/api/postgres/machine/{major}/grants", headers=headers, json={
                "name": name, "host": linked["address"], "reached_as": host,
                "verifier": linkedcluster.scram_verifier(password),
                "workspace": linked["workspace"], "instance": linked["instance"]})
        except httpx.HTTPError as exc:
            raise JobFailed(f"{link.name}: {describe_error(exc)}") from None
        if resp.status_code != 200:
            raise JobFailed(linked_refusal(link, resp, major))
        try:
            remote_id = str(resp.json().get("job") or "")
        except (ValueError, AttributeError):
            remote_id = ""
        if not remote_id:
            raise JobFailed(f"{link.name} started no job for the database")
        try:
            data = await linkcopy.follow(job, client, link, headers, remote_id, "the database")
        except (JobFailed, asyncio.CancelledError) as exc:
            # its job failed (it made nothing, or took it back itself), or it was not seen to its end: nothing
            # is dropped there from here, where a database of that name may be another instance's
            if isinstance(exc, asyncio.CancelledError) or "goes on" in str(exc):
                job.warn(f"The job of {link.name} was not followed to its end: if it made database {name} all the "
                         "same, it stays there: an admin there drops it in its Database tab (From other machines)")
            raise
        try:
            # made there by this job: from here on, what fails gives it back
            grant = (data.get("result") or {}).get("grant") or {}
            remote = workspacehelper.remote_spec({
                "host": host, "port": grant.get("port"), "user": grant.get("role"), "password": password,
                "database": grant.get("database"), "ca": grant.get("ca"), "link": link.name, "major": major})
            found = await asyncio.to_thread(linkedcluster.check, host, remote["port"], remote["ca"], remote["user"],
                                            remote["database"], linked["address"])
            trip = await asyncio.to_thread(linkedcluster.measure, host, [remote["port"]])
        except (workspacehelper.HelperError, linkedcluster.CheckFailed, asyncio.CancelledError) as exc:
            # made there and not usable from here (or this job stopped): given back
            await linked_release(job, link.name, major, name, linked["who"], linked["role"])
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise JobFailed(str(exc)) from None
        job.log(f"PostgreSQL {major} of {link.name} answers on {host}:{remote['port']} over {found['tls']}, its "
                "certificate verified against its CA and its name, and lets this machine in"
                + (f"; round trip {trip['ms']:g} ms (the median of {trip['tries']} connections)"
                   if trip["ms"] is not None else ""))
        if trip["ms"] is not None and trip["ms"] > linkedcluster.WARN_MS:
            job.warn(f"The round trip to the database is {trip['ms']:g} ms: a page of "
                     f"{linkedcluster.PAGE_QUERIES} queries waits {linkedcluster.page_seconds(trip['ms']):g} s "
                     "more for the network alone")
        return remote

    async def linked_release(job, link_name: str, major: str, database: str, who: str, role: str) -> None:
        """The linked machine asked to take its part back: the database and
        its role dropped, its line of pg_hba.conf, the firewall rule when no
        other database uses it. It never fails the job: when that machine
        does not answer, what stays there is said."""
        stays = (f"database {database} and role {database} of its PostgreSQL {major}, their line of pg_hba.conf and "
                 "the firewall rule for this machine's address: an admin there drops them in its Database tab "
                 "(From other machines)")
        link = settings.link_targets.get(link_name)
        if link is None:
            job.warn(f"The link {link_name} is no longer here: on that machine stay {stays}")
            return
        client = link_clients.client(link)
        headers = {**link_headers(link, who, role), "Accept": "application/json"}
        job.log(f"--- its database on PostgreSQL {major} of {link.name}: asked to drop it")
        try:
            resp = await client.post(f"/api/postgres/machine/{major}/grants/{database}/drop", headers=headers, json={})
            if resp.status_code != 200:
                raise JobFailed(linked_refusal(link, resp, major))
            answer = resp.json()
            if answer.get("gone"):
                job.log(f"{link.name} had no database {database} of this machine any more: nothing stays there")
                return
            data = await linkcopy.follow(job, client, link, headers, str(answer.get("job") or ""),
                                         "the removal of the database")
        except (httpx.HTTPError, JobFailed, ValueError) as exc:
            reason = describe_error(exc) if isinstance(exc, httpx.HTTPError) else str(exc)
            job.warn(f"{link.name} did not take its part back ({reason}): on it stay {stays}")
            return
        done = (data.get("result") or {}).get("ungrant") or {}
        still = ", ".join(done.get("firewall") or [])
        job.log(f"PostgreSQL {major} of {link.name}: database {database} and its role dropped, its line of "
                "pg_hba.conf removed; its firewall "
                + (f"still lets {still} in, for other databases" if still else "lets no address in any more"))
        if not done.get("grants_left") and done.get("port"):
            job.log(f"Its port {done.get('port')} stays open there, closed to every address: Close the port, in "
                    f"the Database tab of {link.name}, takes it away")

    def link_now(inst: Instance) -> str:
        """The link of an instance's database on a linked cluster, by the name
        it has now: the one recorded at the creation, or, when that link was
        renamed since, the link that leads to the database's host; "" for an
        instance of any other kind, and when no link leads there any more."""
        recorded = remote_db_host(inst, "database_link")
        if not recorded or recorded in settings.link_targets:
            return recorded
        host = remote_db_host(inst).rpartition(":")[0].lower()
        # of several links to that machine, one that may carry it (HTTPS or SSH), as the one that made it was
        return next((link.name for link in settings.link_targets.values()
                     if host and linkedcluster.host_of(link) == host and linkedcluster.unprotected(link) is None), "")

    def linked_database(inst: Instance) -> dict | None:
        """{"link", "major", "database"} of an instance whose database is on
        the cluster of a linked OmniaCLI (its record), None for any other;
        the link by the name it has now, else as it was recorded."""
        link, major = remote_db_host(inst, "database_link"), remote_db_host(inst, "database_major")
        return ({"link": link_now(inst) or link, "major": major, "database": inst.options.database}
                if link and major else None)

    def postgres_basis(row: dict, machine: dict, memory_gb: float, disk: str, rows: list[dict]) -> dict:
        ws = settings.workspaces.get(row.get("workspace") or "")
        limit = float(ws.limits.get("memory_gb") or 0) if ws is not None else 0
        proposal = pgtuning.propose(row, rows, memory_gb, disk, limit or None)
        limits = pgtuning.limits(row, rows, memory_gb, int(row.get("connections") or 0))
        stays = [{"key": r["key"], "server": r["server"], "kind": r["kind"]} for r in rows
                 if r["where"] == "dedicated" and r["kind"] != "production" and r["key"] != row["key"]]
        return {"proposal": proposal, "limits": limits, "machine": {**machine, "memory_gb": memory_gb, "disk": disk},
                "workspace": row.get("workspace") or "", "workspace_gb": limit or None, "stays": stays,
                "productions": proposal["productions"], "when": "at" if row["kind"] in pgtuning.SHARING else "now",
                "time": pgtuning.DEFAULT_TIME,
                "at": pgtuning.next_time(pgtuning.DEFAULT_TIME).isoformat(timespec="minutes")}

    def postgres_choice(machine: dict, memory_gb, disk) -> tuple[float, str]:
        """The memory and disk of a proposal: those asked (checked), else the machine's."""
        memory = machine["memory_gb"]
        if memory_gb not in (None, ""):
            try:
                memory = float(str(memory_gb).replace(",", "."))
            except ValueError:
                raise ValueError(f"{memory_gb!r} is not a number of GB") from None
            top = machine["total_gb"] or 1024
            if not 0.25 <= memory <= top:
                raise ValueError(f"The memory for the databases is from 0.25 to {top:g} GB")
        disk = disk or machine["disk"]
        if disk not in pgtuning.DISKS:
            raise ValueError(f"{disk!r} is not ssd or hdd")
        return memory, disk

    @app.get("/api/postgres/{server}/{role}/proposal")
    async def postgres_proposal(request: Request, server: str, role: str, memory_gb: str = "", disk: str = "",
                                me: Principal = Depends(current_user)):
        """The values proposed for the database of server/role, and the base of the calculation."""
        postgres_admin(me)
        found = await postgres_rows(request_language(request))
        row = postgres_row(found["rows"], server, role)
        if row.get("error"):
            return refuse(f"The database of {row['key']} does not answer: {row['error']}", status.HTTP_502_BAD_GATEWAY)
        machine = await asyncio.to_thread(postgres_machine)
        try:
            memory, disk = postgres_choice(machine, memory_gb, disk)
        except ValueError as exc:
            return refuse(str(exc))
        return {"row": row, **postgres_basis(row, machine, memory, disk, found["rows"])}

    async def postgres_helper(job, inst: Instance, action: str, payload) -> list[str]:
        """A tuning action of the root helper, its values on the input; its lines in the job."""
        cmd = inst.restart.command(action, getattr(inst.options, "db_service", "db"), inst.options.database)
        job.log("$ " + " ".join(cmd))
        code, out = await ops.capture(cmd, input=json.dumps(payload).encode(), timeout=POSTGRES_HELPER_TIMEOUT)
        for line in out.splitlines():
            if line.strip():
                job.log(line)
        if code:
            raise JobFailed(f"{action} on {inst.key} failed: {out.strip()[-300:] or code}")
        return out.splitlines()

    def postgres_record(key: str, **change) -> None:
        """The record of a database changed: tuned (who, when), the parameters
        written (added or removed), the memory used, its planned restart."""
        def apply(data):
            rec = data["clusters"].setdefault(key, {})
            if "add" in change and change["add"]:
                rec.update({"tuned": datetime.datetime.now().isoformat(timespec="minutes"), "by": change.get("by", ""),
                            "params": sorted(set(rec.get("params") or []) | set(change["add"])),
                            "memory_gb": change.get("memory_gb", rec.get("memory_gb"))})
            if change.get("remove"):
                rec["params"] = sorted(set(rec.get("params") or []) - set(change["remove"]))
                if not rec["params"]:
                    for k in ("tuned", "by", "params", "memory_gb"):
                        rec.pop(k, None)
            if "restart" in change:
                if change["restart"] is None:
                    rec.pop("restart", None)
                else:
                    rec["restart"] = change["restart"]
            if not rec:
                data["clusters"].pop(key, None)
        tuning.update(apply)

    ODOO_DB_ERRORS = re.compile(r"OperationalError|InterfaceError|connection already closed|"
                                r"server closed the connection|terminating connection")

    async def postgres_restart(job, inst: Instance, values: dict, names: list[str], by: str) -> None:
        """The database of inst restarted (db-restart), the values of the
        restart written right before; the record follows what was written.
        The Odoo log lines with a database error since then are counted:
        the requests that failed (AD-005 §5)."""
        since = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        job.log(f"--- restart of the database of {inst.key}: CHECKPOINT, fast stop, start"
                + (f"; written before it: {', '.join([*values, *names])}" if values or names else ""))
        cmd = inst.restart.command("db-restart", getattr(inst.options, "db_service", "db"), inst.options.database)
        job.log("$ " + " ".join(cmd))
        code, out = await ops.capture(cmd, input=json.dumps({"set": values, "reset": names}).encode(),
                                      timeout=POSTGRES_RESTART_TIMEOUT)
        answer = {}
        for line in out.splitlines():
            if line.startswith("{"):
                with contextlib.suppress(ValueError):
                    answer = json.loads(line)
            elif line.strip():
                job.log(line)
        if code == 3:
            raise JobFailed(f"The database of {inst.key} did not come back within 3 minutes: the values of before "
                            f"are back and it started again ({answer.get('reason') or 'no reason given'})")
        if code:
            raise JobFailed(f"The restart of the database of {inst.key} failed: {out.strip()[-300:] or code}")
        await asyncio.to_thread(postgres_record, inst.key, add=list(values), by=by, remove=list(names))
        job.log(f"--- the database of {inst.key} answers again: {answer.get('seconds', '?')} s without it, "
                f"{answer.get('total', '?')} s in all; statistics of the queries "
                + ("on" if answer.get("statements_on") else "off"))
        # the machine's cluster: the log of every instance it serves
        served = machine_served() if getattr(inst, "cluster", False) else [inst]
        for one in served:
            odoo = getattr(one.options, "odoo_service", "odoo")
            code, log = await ops.capture(one.restart.command("logs", odoo, "--since", since), timeout=60)
            if code == 0:
                errors = sum(1 for line in log.splitlines() if ODOO_DB_ERRORS.search(line))
                job.log(f"Odoo log lines with a database error since the restart"
                        + (f" ({one.key})" if len(served) > 1 or one is not inst else "") + f": {errors}")

    def machine_served() -> list[Instance]:
        return [i for srv in settings.active_servers() for i in srv.instances.values()
                if i.restart.type == "compose" and on_machine_db(i)]

    def postgres_title(inst: Instance) -> str:
        if getattr(inst, "cluster", False):
            return f"the machine's PostgreSQL {inst.role}"
        return f"the database of {inst.key}"

    @app.post("/api/postgres/{server}/{role}/apply")
    async def postgres_apply(request: Request, server: str, role: str, payload: dict = Body(...),
                             me: Principal = Depends(current_user)):
        """The values chosen: those valid at once written and reloaded now,
        those of a restart written right before it, now or at the time
        chosen (a restart planned once). A job."""
        postgres_admin(me)
        inst = tuning_target(server, role)
        found = await postgres_rows(request_language(request))
        row = postgres_row(found["rows"], server, role)
        if row.get("error"):
            return refuse(f"The database of {row['key']} does not answer: {row['error']}", status.HTTP_502_BAD_GATEWAY)
        if row.get("restart"):
            return refuse(f"A restart of {postgres_title(inst)} is planned: cancel it or restart now first")
        machine = await asyncio.to_thread(postgres_machine)
        try:
            memory, disk = postgres_choice(machine, payload.get("memory_gb"), payload.get("disk"))
            values = pgtuning.checked(row, payload.get("values") or {},
                                      pgtuning.limits(row, found["rows"], memory, int(row.get("connections") or 0)))
            params = pgtuning.by_name(row["params"])
            at_once = {n: v for n, v in values.items() if not params[n]["restart"]}
            later = {n: v for n, v in values.items() if params[n]["restart"]}
            when = str(payload.get("when") or "now")
            at = None if not later or when == "now" else pgtuning.next_time(when)
        except ValueError as exc:
            return refuse(str(exc))
        if not values:
            return refuse("Nothing to apply: the values are those of now")
        if memory != machine["memory_gb"] or disk != machine["disk"]:
            def keep(data):
                data["machine"].update({"memory_gb": memory, "disk": disk})
            await asyncio.to_thread(tuning.update, keep)
        by = str(me)

        async def action(job):
            job.log(f"--- values for {postgres_title(inst)} on {memory:g} GB for the databases, disk {disk}")
            if at_once:
                await postgres_helper(job, inst, "pg-set", at_once)
                await asyncio.to_thread(postgres_record, inst.key, add=list(at_once), by=by, memory_gb=memory)
                job.log(f"--- valid now, without stopping anything: {', '.join(f'{n} {v}' for n, v in at_once.items())}")
            if not later:
                job.log("No restart: the values are valid now")
            elif at is None:
                await postgres_restart(job, inst, later, [], by)
            else:
                await asyncio.to_thread(postgres_record, inst.key, restart={
                    "at": at.isoformat(timespec="minutes"), "set": later, "reset": [], "by": by, "job": None})
                job.log(f"--- restart of {postgres_title(inst)} planned at {at:%Y-%m-%d %H:%M} for {len(later)} values, "
                        f"written then: {', '.join(f'{n} {v}' for n, v in later.items())}")
        ws = workspace_of(server)
        job = jobs.submit(f"Tune {postgres_title(inst)}", [inst.key], f"manual: {me}", action,
                          workspace=ws[0].name if ws else "")
        return {"job": job.id}

    def postgres_fire(key: str, by: str) -> Job | None:
        """The planned restart of the database of key, as a job (it waits for
        a job running on the instance); the plan is taken when it starts."""
        server, _, role = key.partition("/")
        inst = known_cluster(role) if server == MACHINE_KEY else settings.instance(server, role)
        if inst is None:
            return None

        async def action(job):
            taken = {}

            def take(data):
                rec = data["clusters"].get(key) or {}
                plan = rec.get("restart") or {}
                if plan.get("job") == job.id:
                    taken.update(plan)
                    rec.pop("restart", None)
            await asyncio.to_thread(tuning.update, take)
            if not taken:
                job.log("The planned restart was cancelled before it started: nothing done")
                return
            await postgres_restart(job, inst, dict(taken.get("set") or {}), list(taken.get("reset") or []),
                                   taken.get("by") or by)
        ws = workspace_of(server)
        job = jobs.submit(f"Restart {postgres_title(inst)} (planned)", [inst.key], by, action,
                          workspace=ws[0].name if ws else "")

        def mark(data):
            plan = (data["clusters"].get(key) or {}).get("restart")
            if plan is not None:
                plan["job"] = job.id
        tuning.update(mark)
        return job

    def postgres_due(now: datetime.datetime | None = None) -> list[str]:
        """The planned restarts whose time has come, fired; one OmniaCLI was
        stopped for (more than MISSED_AFTER late) moved to the next day at the
        same time: never a restart late in the working day."""
        now = now or datetime.datetime.now()
        fired, moved = [], {}
        for key, rec in tuning.load()["clusters"].items():
            plan = rec.get("restart")
            if not plan:
                continue
            try:
                at = datetime.datetime.fromisoformat(str(plan.get("at")))
            except ValueError:
                continue
            job = jobs.get(plan.get("job") or "")
            if at > now or (job is not None and not job.finished_flag):
                continue
            if (now - at).total_seconds() > pgtuning.MISSED_AFTER:
                while at <= now:
                    at += datetime.timedelta(days=1)
                moved[key] = at
                _logger.warning("the planned restart of the database of %s was missed: moved to %s", key, at)
                continue
            if postgres_fire(key, plan.get("by") or "planned restart") is not None:
                fired.append(key)
        if moved:
            def move(data):
                for key, at in moved.items():
                    plan = (data["clusters"].get(key) or {}).get("restart")
                    if plan is not None:
                        plan.update({"at": at.isoformat(timespec="minutes"), "job": None})
            tuning.update(move)
        return fired

    async def stop_postgres() -> None:
        for task in postgres_tasks:
            task.cancel()
        await asyncio.gather(*postgres_tasks, return_exceptions=True)
        postgres_tasks.clear()

    async def postgres_loop() -> None:
        while True:
            await asyncio.sleep(POSTGRES_EVERY)
            try:
                postgres_due()
            except Exception:  # noqa: BLE001 - the loop must survive
                _logger.exception("the planned restarts of the databases failed")

    @app.post("/api/postgres/{server}/{role}/restart")
    async def postgres_restart_now(server: str, role: str, me: Principal = Depends(current_user)):
        """The planned restart done now, or a restart for the values changed outside OmniaCLI."""
        postgres_admin(me)
        inst = tuning_target(server, role)
        if inst.restart.type != "compose":
            return refuse(f"{inst.key} has no database of its own here: only a dedicated one is restarted")
        plan = (await asyncio.to_thread(tuning.cluster, inst.key)).get("restart")
        if plan:
            running = jobs.get(plan.get("job") or "")
            if running is not None and not running.finished_flag:
                return {"job": running.id}
            job = postgres_fire(inst.key, f"manual: {me}")
            return {"job": job.id}
        by = str(me)
        ws = workspace_of(server)
        job = jobs.submit(f"Restart {postgres_title(inst)}", [inst.key], f"manual: {me}",
                          lambda job: postgres_restart(job, inst, {}, [], by), workspace=ws[0].name if ws else "")
        return {"job": job.id}

    @app.post("/api/postgres/{server}/{role}/restart/cancel")
    async def postgres_restart_cancel(server: str, role: str, me: Principal = Depends(current_user)):
        """The planned restart taken away: the values waiting for it are not
        written; those valid already stay. Nothing is stopped."""
        postgres_admin(me)
        inst = tuning_target(server, role)
        gone = {}

        def cancel(data):
            rec = data["clusters"].get(inst.key) or {}
            gone.update(rec.pop("restart", None) or {})
            if not rec:
                data["clusters"].pop(inst.key, None)
        await asyncio.to_thread(tuning.update, cancel)
        if not gone:
            return refuse(f"No restart of {postgres_title(inst)} is planned")
        queued = jobs.get(gone.get("job") or "")
        if queued is not None and queued.status == "queued":
            jobs.stop(queued.id, str(me))
        access_log.record("action", me.kind, me.name, "", f"planned restart of {postgres_title(inst)} cancelled")
        return {"ok": True}

    @app.post("/api/postgres/{server}/{role}/reset")
    async def postgres_reset(request: Request, server: str, role: str, payload: dict = Body(...),
                             me: Principal = Depends(current_user)):
        """The parameters the tuning wrote (the record says which, never the
        point-in-time recovery's) back to their base values: those valid at
        once now, those of a restart at it, now or at the time chosen."""
        postgres_admin(me)
        inst = tuning_target(server, role)
        found = await postgres_rows(request_language(request))
        row = postgres_row(found["rows"], server, role)
        if row.get("error"):
            return refuse(f"The database of {row['key']} does not answer: {row['error']}", status.HTTP_502_BAD_GATEWAY)
        if row.get("restart"):
            return refuse(f"A restart of {postgres_title(inst)} is planned: cancel it or restart now first")
        params = pgtuning.by_name(row["params"])
        names = [n for n in (row.get("tuned") or {}).get("params") or [] if n in params]
        if not names:
            return refuse(f"The database of {inst.key} was not tuned here: nothing to put back")
        at_once = [n for n in names if not params[n]["restart"]]
        later = [n for n in names if params[n]["restart"]]
        try:
            when = str(payload.get("when") or "now")
            at = None if not later or when == "now" else pgtuning.next_time(when)
        except ValueError as exc:
            return refuse(str(exc))
        by = str(me)

        async def action(job):
            job.log(f"--- back to the base values: {', '.join(names)}; point-in-time recovery untouched")
            if at_once:
                await postgres_helper(job, inst, "pg-reset", at_once)
                await asyncio.to_thread(postgres_record, inst.key, remove=at_once)
            if later and at is None:
                await postgres_restart(job, inst, {}, later, by)
            elif later:
                await asyncio.to_thread(postgres_record, inst.key, restart={
                    "at": at.isoformat(timespec="minutes"), "set": {}, "reset": later, "by": by, "job": None})
                job.log(f"--- restart of {postgres_title(inst)} planned at {at:%Y-%m-%d %H:%M} for {len(later)} values")
        ws = workspace_of(server)
        job = jobs.submit(f"Base values for {postgres_title(inst)}", [inst.key], f"manual: {me}", action,
                          workspace=ws[0].name if ws else "")
        return {"job": job.id}

    def database_place(inst: Instance) -> dict | None:
        """Where the database of the instance is, from its record (PostgreSQL
        is not read): dedicated, the machine's cluster, another machine with
        its host:port and its read replica, the system's PostgreSQL with its
        port; None when its configuration cannot be read. What the Database
        row says first, and what the map draws (UX-009)."""
        if inst.restart.type == "compose" and on_remote_db(inst):
            # link, major: on the cluster of a linked OmniaCLI, for the machine's admins (a user of
            # workspaces reads the host only, as of any database of another machine)
            meta = instance_meta(inst)
            promoted = meta.get("promoted_from") if isinstance(meta.get("promoted_from"), dict) else None
            linked = bool(meta.get("database_link")) and not promoted
            seen = (moved_seen.get(inst.server) or {}) if linked else {}
            copy = meta.get(workspacehelper.MOVED_META) if isinstance(meta.get(workspacehelper.MOVED_META), dict) else None
            says = seen.get("says") or {}
            return {"where": "remote", "host": str(meta.get("database_on") or "") or "another machine",
                    # on the cluster of a linked machine (UX-011 §B): since when it does not answer and what its
                    # machine says of it; where it is when it followed its database to the promoted copy
                    "down": {"since": seen.get("since"), "machine": says.get("machine") or "",
                             "place": says.get("place") or "", "error": seen.get("says_error") or ""}
                    if seen.get("down") is not None else None,
                    "still": says.get("machine") or "" if seen.get("down") is None and seen.get("elsewhere") else "",
                    # (not "copy": a template would read the dict's own method)
                    "on_copy": {"machine": str(copy.get("machine") or ""), "at": str(copy.get("at") or "")} if copy else None,
                    "promoted": bool(promoted),
                    "replica": str(meta.get("replica_on") or ""),
                    "link": link_now(inst) or None,
                    "major": str(meta.get("database_major") or "") or None,
                    # the promoted replica of this machine's cluster (AD-004): the provider's machine, whose
                    # address a user of workspaces does not read (unlike a third party's database, UX-009 D3);
                    # the machine's admins read which machine hosts it and since when
                    "provider": bool(promoted) or linked,
                    "machine": str(meta.get("database_link") or "") if promoted else "",
                    "since": str(promoted.get("at") or "") if promoted else ""}
        if inst.restart.type == "compose" and on_machine_db(inst):
            # reads: also from the replica of the cluster (AD-004); which machine, only replica_offer_of says
            return {"where": "machine", "reads": bool(remote_db_host(inst, workspacehelper.REPLICA_META))}
        if inst.restart.type == "compose":
            # copied: a copy of it is kept on another machine (UX-011 §D; its own record, a file); reads: also from it
            rec = machinedb.replica(dedicated_folder(inst)) if inst.role == "production" else None
            return {"where": "dedicated", "copied": bool(rec) and rec.get("state") != "replaced",
                    "reads": bool(remote_db_host(inst, workspacehelper.REPLICA_META))}
        try:
            shell = inst.plugin.db_shell(inst)
        except Exception:  # noqa: BLE001 - a config that cannot be read: no row
            return None
        if shell is None:
            return None
        place = pgreport.where(inst, shell[1])
        if place["where"] == "remote":
            return {"where": "remote", "host": place["host"]}
        return {"where": "system", "port": str(shell[1].get("PGPORT") or 5432)}

    def database_line(inst: Instance) -> dict | None:
        """The Database row of the instance's Settings (UX-008 §6.2.6), from
        the record: PostgreSQL is not read, the page is not slowed. With the
        move it may make (AD-007) and whether a move left its undo beside it."""
        place = database_place(inst)
        if place is not None and place["where"] == "remote" and place.get("promoted"):
            # the data of before the promotion of its dedicated database's replica, still beside it
            return {**place, "kept": move_offer(inst)["kept"]}
        if place is None or place["where"] in ("remote", "system"):
            return place
        machine = place["where"] == "machine"
        key = f"{MACHINE_KEY}/{next(iter(machinedb.registry()), '')}" if machine else inst.key
        rec = tuning.cluster(key)
        return {"where": place["where"], "reads": bool(place.get("reads")), "copied": bool(place.get("copied")),
                "tuned": rec.get("tuned") if rec.get("params") else None,
                "restart": (rec.get("restart") or {}).get("at"), **move_offer(inst)}

    # --- a move between a dedicated database and the machine's cluster (AD-007, step 6)

    MOVE_TIMEOUT = 6 * 3600 + 600

    def machine_twin_of(template) -> str:
        """The template on the machine's cluster of a dedicated one, "" when none."""
        # a template of before (or a plugin's) has neither: no twin
        twins = [t for t in workspaceops.templates().values()
                 if getattr(t, "on_machine", False) and getattr(t, "dedicated", "") == template.name]
        return twins[0].name if twins else ""

    def move_offer(inst: Instance) -> dict:
        """{"move": machine|dedicated|"" , "why": why not, "kept": the undo of a move is there}
        of the production of a workspace's instance."""
        found = workspace_of(inst.server)
        if found is None or inst.role != "production":
            return {"move": "", "why": "", "kept": False}
        ws, instance = found
        project = Path(inst.restart.service)
        kept = any((project / name).exists() for name in (workspacehelper.KEPT, workspacehelper.MOVES))
        if on_machine_db(inst):
            return {"move": "dedicated", "why": "", "kept": kept}
        if on_remote_db(inst):
            # its dedicated database was replaced by its promoted replica (UX-011 §D): the data of before is there
            before = (instance_meta(inst).get("promoted_from") or {}).get("dedicated") and (project / "data" / "pgdata").is_dir()
            # while its database comes back (UX-011 §A for one instance) that folder is the copy that follows the
            # promoted one, and the data of before is set aside for the return, which frees it: nothing to free here
            coming = (machinedb.returning(dedicated_folder(inst)) or {}).get("state") in ("copying", "ready")
            return {"move": "", "why": "", "kept": (kept or bool(before)) and not coming}
        if machinedb.replica(dedicated_folder(inst)):
            return {"move": "", "why": "its database has a read replica on another machine: drop the replica first",
                    "kept": kept}
        template = instance_template(ws, instance)
        if template is None:
            return {"move": "", "why": "its template is not on this machine any more", "kept": kept}
        if not machine_twin_of(template):
            return {"move": "", "why": f"the template {template.name} has no twin on the machine's cluster",
                    "kept": kept}
        if not machinedb.registry():
            return {"move": "", "why": "this machine has no cluster of its own yet (Dashboard › Database)",
                    "kept": kept}
        if settings.servers.get(inst.server) is not None and settings.servers[inst.server].test is not None:
            return {"move": "", "why": "it has a test of its own: on the machine's cluster an instance has its "
                                       "production only", "kept": kept}
        return {"move": "machine", "why": "", "kept": kept}

    @app.post("/api/servers/{server}/database/move")
    async def database_move(server: str, payload: dict = Body(...), me: Principal = Depends(current_user)):
        """Move… (AD-007): the production's database to the machine's cluster
        or back to one of its own, a job: Odoo stops for the time of the copy,
        the copy is checked, the servers file follows. On a move to the
        cluster the dedicated database stays beside it, the undo, until
        Free the space of before…"""
        if not machine_admin(me):
            return refuse("Only an admin of the machine moves a database", status.HTTP_403_FORBIDDEN)
        to = str((payload or {}).get("to") or "")
        if to not in ("machine", "dedicated"):
            return refuse("to is machine or dedicated")
        srv = settings.servers.get(server)
        found = workspace_of(server)
        if srv is None or srv.production is None or found is None:
            return refuse(f"{server} is not an instance of a workspace", status.HTTP_404_NOT_FOUND)
        ws, instance = found
        offer = await asyncio.to_thread(move_offer, srv.production)
        if offer["move"] != to:
            return refuse(f"{server} cannot move to " + ("the machine's cluster" if to == "machine"
                                                         else "a dedicated database") + (f": {offer['why']}"
                                                                                          if offer["why"] else ""))
        if server_busy(server):
            return refuse(f"{server} is busy: wait for its job")
        size = None
        with contextlib.suppress(JobFailed, asyncio.TimeoutError):
            size = int((await asyncio.wait_for(pgreport.report(srv.production), pgtuning.READ_TIMEOUT)).get("size") or 0)

        async def action(job):
            where = "the machine's cluster" if to == "machine" else "a dedicated database"
            job.log(f"--- {server}: its database to {where}"
                    + (f" ({size >> 20} MiB)" if size else "") + "; Odoo stops for the time of the copy")
            made = await workspaceops._helper(job, "machine-db-move", ws.name, ws.storage, instance, to,
                                              timeout=MOVE_TIMEOUT)
            async with config_lock:
                current = settings.servers.get(server)
                if current is None:
                    raise JobFailed(f"{server} is no longer in the servers file: its database moved anyway")
                data = server_to_dict(current)
                data["production"] = {**data["production"], "database": made["database"],
                                      "db_service": machinedb.DB_SERVICE if to == "machine" else "db"}
                parsed = parse_servers({server: data}, settings.logs_dir)[server]
                error = await store({**settings.servers, server: parsed})
            if error is not None:
                raise JobFailed(f"the database moved, but the servers file was not written: {error.body.decode()}")
            if to == "machine" and made.get("pitr_was_on"):
                await asyncio.to_thread(pitr_record, server, enabled=False)
                job.warn("The recovery at a minute this database had of its own stops with the move: on the "
                         "machine's cluster it is the cluster's, one for all its instances (Database tab of the "
                         "machine). What was archived before the move stays in the folder of the dedicated "
                         "database, until Free the space of before")
            elif to == "dedicated" and made.get("pitr"):
                await asyncio.to_thread(pitr_record, server, enabled=True)
                job.log("The recovery at a minute archives again: a new full copy is made tonight")
            job.log(f"Database {made['database']} (it was {made['before']}): installed modules|users|partners "
                    f"{made['counts']} on both sides; Odoo was stopped {made['seconds_down']} s")
            if to == "machine":
                job.log("The dedicated database stays beside it, stopped: Move to a dedicated database brings it "
                        "back; Free the space of before deletes it")
            job.result = {**job.result, "server": server, "database": made["database"], "to": to,
                          "seconds_down": made["seconds_down"]}
        title = f"Move {server} to " + ("the machine's cluster" if to == "machine" else "a dedicated database")
        job = jobs.submit(title, [f"{server}/production"], f"manual: {me}", action, workspace=ws.name)
        return {"job": job.id}

    @app.post("/api/servers/{server}/database/purge")
    async def database_purge(server: str, me: Principal = Depends(current_user)):
        """Free the space of before… : what a move left (the dedicated
        database of before, its archive, the dumps) deleted, the undo gone."""
        if not machine_admin(me):
            return refuse("Only an admin of the machine frees it", status.HTTP_403_FORBIDDEN)
        srv = settings.servers.get(server)
        found = workspace_of(server)
        if srv is None or srv.production is None or found is None:
            return refuse(f"{server} is not an instance of a workspace", status.HTTP_404_NOT_FOUND)
        if not (await asyncio.to_thread(move_offer, srv.production))["kept"]:
            return refuse(f"{server} keeps nothing of a move")
        if server_busy(server):
            return refuse(f"{server} is busy: wait for its job")
        ws, instance = found

        async def action(job):
            made = await workspaceops._helper(job, "machine-db-purge", ws.name, ws.storage, instance)
            job.log("Deleted: " + (", ".join(made.get("removed") or []) or "nothing"))
        job = jobs.submit(f"Free the space of before of {server}", [f"{server}/production"], f"manual: {me}", action,
                          workspace=ws.name)
        return {"job": job.id}

    app.state.postgres_due = postgres_due
    # the look at the read replicas, a round of it: what the loop runs (the tests call it)
    app.state.replica_look_own = replica_look_own
    app.state.replica_look_hosted = replica_look_hosted
    app.state.interrupted_names_freed = interrupted_names_freed
    app.state.return_look = return_look
    app.state.moved_look = moved_look
    app.state.moved_seen = moved_seen
    app.state.cluster_archive_tick = cluster_archive_tick

    return app


def app_from_env() -> FastAPI:
    """uvicorn factory: `uvicorn --factory omniacli.app:app_from_env`."""
    return create_app(load_settings(os.environ.get("OMNIACLI_CONFIG", "omniacli.yaml")))
