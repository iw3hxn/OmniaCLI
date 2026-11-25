# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The pages a panel draws itself for a linked machine (AD-017 §17.1-17.2).

A linked machine's page used to be the HTML that machine wrote: a fix of a
page reached it only with its update. With a link's ``pages: here`` (the
default) the panel draws the pages of this list with its own templates, under
``/links/<name>``, from the page's model the machine answers at
``GET /api/pages/<page>`` (its fields by name, no object, no secret); the
page's script calls ``/links/<name>/api/…`` as before.

The contract: a machine says ``agent_api`` (an integer) and its ``plugins``
(``{name: version}``) in ``GET /api/info``; one that says nothing is 0. Each
page here says the least it needs (``needs_agent_api``); the panel reads up to
``CENTRAL_AGENT_API``, and that one and the one before it (AD-017 §10.6). It
draws a page when ``needs_agent_api <= agent_api <= CENTRAL_AGENT_API`` and
it has every plugin of the machine at the same version; else the machine's own
page passes as before, with a line on top that says why.

Within one ``agent_api`` the machine's API only grows (fields, routes): taking
away or renaming one raises the number."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import i18n, plugins
from .auth import ROLES
from .config import Instance, Server, Workspace
from .jobs import Job
from .plugin_api import Action, Field

# what this machine's API speaks: GET /api/info says it, GET /api/pages/* answers from it. 2: the model of an
# instance's page (GET /api/pages/server, AD-017 §9 slice 3); 3: the models of the 14 pages of class B (slice 4);
# 4: the models of the other pages of class C (slice 5: the machine's section and its Database tab, a workspace's
# page, Users, Pullers, a server's form, Options), and the webhooks of Options named by ref
AGENT_API = 4
# the newest a panel of this version reads; it reads that one and the one before it (AD-017 §10.6)
CENTRAL_AGENT_API = 4
OLDEST_READ = CENTRAL_AGENT_API - 1

# a link's pages (links.yaml, pages:): drawn here, or the machine's own as before (the undo switch)
PAGES_HERE = "here"
PAGES_MACHINE = "machine"
PAGES_WAYS = (PAGES_HERE, PAGES_MACHINE)

# why the machine's own page passes (the line on top says it)
BEHIND = "behind"  # the machine is older than the page asks
AHEAD = "ahead"  # the panel is older than the machine
PLUGINS = "plugins"  # a plugin of the machine this panel has not, or at another version
OWN = "own"  # a page of the machine this panel does not draw (MACHINE_OWN)

# how often a link's agent_api is read again, kept in memory (seconds); after an update made from the panel,
# for a while, much sooner: the machine restarts with its new version
READ_EVERY = 600
READ_AFTER_UPDATE = 15
# a machine that did not answer GET /api/info is not asked again before (seconds): no round trip at every page
AGENT_DOWN = 60
# the highest agent_api each link's machine has said, beside the links file: once 1 or more, a machine that says
# less (or has no model of a page) does not get its own HTML shown for a page drawn here by its word alone
SEEN_FILE = "link-agents.json"
# a built-in plugin comes with the package: the package's agent_api answers for it, not its version
BUILT_IN = "built-in"
# the links whose machine said panel: agent (AD-017 §17.3), beside the links file: kept once said, so a machine
# that says full afterwards (one compromised) does not lift what this panel refuses it; forgotten when an admin
# here saves or deletes the link
PANELS_FILE = "link-panels.json"
PANEL_AGENT = "agent"
PANEL_FULL = "full"
# why a page of a machine in agent mode is not shown (agent_refused.html says it): this panel older than the
# machine, a plugin apart, an invitation or a plugin's own page (its HTML), no /api/info
REFUSED_AHEAD = AHEAD
REFUSED_PLUGINS = PLUGINS
REFUSED_INVITE = "invite"
REFUSED_PLUGIN_PAGE = "plugin_page"
REFUSED_NO_INFO = "no_info"

_SEGMENT = r"[A-Za-z0-9._~-]+"


@dataclass(frozen=True)
class Page:
    """A page this panel draws for a linked machine: where it is under /links/<name>/, its template, the least
    agent_api it needs and the fields of its model it takes (any other the machine sends is left out)."""

    name: str
    path: str
    template: str
    fields: tuple[str, ...] = ()
    needs_agent_api: int = 1
    # the parameters of its query the machine's page reads (?view=test), passed to its model
    query: tuple[str, ...] = ()

    @property
    def pattern(self) -> re.Pattern:
        return re.compile(self.path)


# the context of the machine's section (index.html, with its Database and SSH access tabs), by name
INDEX_FIELDS = ("rows", "sections", "total", "q", "machine", "machine_links", "linked_sections", "quick_instance",
                "jobs", "load_workspaces", "machine_database", "machine_ssh", "audit", "machine_version", "update_need")

# the context of an instance's page (server.html and its partials) its model carries, by name (AD-017 §17.1
# constraint 1): app.server_context makes it with the live objects, server_model takes each one as plain values
SERVER_FIELDS = (
    "environments", "denied", "from_link", "map_workspace", "login_as", "name_problems", "row_login_as", "stagings",
    "is_admin_here", "move", "addresses", "mail_capture", "mail_inbox", "mail_old_odoo", "mail_domain",
    "is_developer_here", "shell_paths", "test_state", "pull_choices", "restore_file", "keep_deploys", "available",
    "manual_only", "restore_fields", "backup_fields", "server", "view", "instances", "buttons", "server_buttons",
    "dialogs", "core_groups", "settings_rows", "db_line", "replica_line", "machine_admin", "repo_status",
    "repo_urls", "odoo_params", "param_role", "enterprise", "package_state", "auto_state", "busy", "busy_job",
    "running", "service_state", "run_state", "jobs",
)

# the 12 pages of class A (AD-017 §17.1): a shell whose script reads /api, a context of a few plain values
PAGES = (
    Page("disks", r"disks", "disks.html"),
    Page("log", rf"servers/(?P<server>{_SEGMENT})/(?P<role>production|test)/log", "log.html", ("instance",)),
    Page("own_log", r"omniacli/log", "own_log.html", ("log_file",)),
    Page("instances", r"instances", "instances.html",
         ("here_name", "links", "quick_instance", "open_in_rows", "ni_links")),
    Page("backups", r"backups", "backups.html", ("here_name", "links", "upload_here", "upload_links")),
    Page("machines", r"machines", "machines.html",
         ("provision_key", "package_url", "link_user", "machines", "groups")),
    Page("discover", r"config/discover", "discover.html", ("roles",)),
    Page("connection", rf"pullers/connections/(?P<name>{_SEGMENT})", "connection.html",
         ("name", "connection", "commands")),
    Page("shell", rf"servers/(?P<server>{_SEGMENT})/(?P<role>production|test)/(?P<kind>shell|claude|psql|terminal)",
         "shell.html", ("instance", "folder", "kind", "command", "stay", "system_user", "shell", "drops",
                        "drop_days", "drop_max")),
    Page("help", r"help", "help.html", ("base_url",)),
    Page("about", r"about", "about.html"),
    Page("settings", r"settings", "settings.html", ("groups",)),
    # an instance's page (class C, slice 3): its model is server_model, drawn from server_view
    Page("server", rf"servers/(?P<server>{_SEGMENT})", "server.html", SERVER_FIELDS, needs_agent_api=2,
         query=("view",)),
    # the 14 pages of class B (slice 4): the pages a customer sees first (AD-018 §18.8), then the others; each
    # model is page_model, drawn from page_view
    Page("account", r"account", "account.html", needs_agent_api=3),
    Page("jobs", r"jobs", "jobs.html", ("jobs", "total", "servers", "server", "q"), needs_agent_api=3,
         query=("server", "q")),
    Page("job", rf"jobs/(?P<job_id>{_SEGMENT})", "job.html", ("job", "live", "offers_download"), needs_agent_api=3),
    Page("workspaces", r"workspaces", "workspaces.html",
         ("form", "links", "machine_column", "storage_roots", "storage_error", "templates", "workspaces", "can_create",
          "here_name", "two_step_in_row", "open_in_rows", "handover_ws", "can_receive", "workspaces_file", "servers",
          "users", "user_roles", "system_config", "roles", "limits", "templates_index"), needs_agent_api=3,
         query=("edit", "new")),
    Page("destinations", rf"(?:workspaces/(?P<workspace>{_SEGMENT})/)?destinations", "destinations.html",
         ("rows", "kinds", "api", "workspace", "ws", "rclone", "file"), needs_agent_api=3),
    Page("access", rf"access|workspaces/(?P<workspace>{_SEGMENT})/audit", "access.html",
         ("entries", "total", "q", "since", "until", "who", "event", "ws", "log_file", "failed_file", "status"),
         needs_agent_api=3, query=("q", "since", "until", "who", "event")),
    Page("config", r"config", "config.html", ("servers", "servers_file", "busy"), needs_agent_api=3),
    Page("credentials", r"credentials", "credentials.html", ("scopes", "error", "credentials_file"),
         needs_agent_api=3),
    Page("enterprise", r"enterprise", "enterprise.html",
         ("source", "error", "default_url", "public_key", "file", "copies", "root", "helpers"), needs_agent_api=3),
    Page("links", r"links", "links.html", ("links", "links_file", "public_key", "ssh"), needs_agent_api=3),
    Page("tunnels", r"tunnels", "tunnels.html", needs_agent_api=3),
    Page("groups", r"groups", "groups.html", ("lists", "known", "groups_file"), needs_agent_api=3),
    Page("plugins", r"plugins", "plugins.html",
         ("found", "count", "missing", "api_version", "group", "route_errors", "source", "sudoers", "systemd"),
         needs_agent_api=3),
    Page("schedules", r"schedules", "schedules.html",
         ("schedules", "targets", "links", "connections", "scripts", "scripts_dir", "schedules_file", "load_error",
          "default_user"), needs_agent_api=3),
    # the other pages of class C (slice 5): each model is page_model, drawn from page_view
    Page("index", r"", "index.html", INDEX_FIELDS, needs_agent_api=4, query=("q",)),
    # the Dashboard's table of instances again, drawn by its script (a job started or ended): a part of index
    Page("dashboard_servers", r"dashboard/servers", "_servers_part.html", ("rows", "sections", "total", "q"),
         needs_agent_api=4, query=("q",)),
    Page("workspace", rf"workspaces/(?P<workspace>{_SEGMENT})", "workspace.html",
         ("ws", "members", "roles", "me", "min_password", "denied", "capabilities", "presets", "my_caps", "custom_roles",
          "sso", "sso_redirect", "sso_login", "sso_people", "can_mail", "invitation_hours", "servers", "templates",
          "can_create", "docker_missing", "storage_problem", "handover_out", "handover_in", "instances", "sites",
          "relay_links", "relay_text", "tokens", "api_base", "grants", "oauth_reach", "links", "db_links",
          "credentials", "destinations_count", "instances_left"), needs_agent_api=4),
    Page("users", r"users", "users.html",
         ("users", "links", "tokens", "api_base", "server_names", "grants", "oauth_reach", "users_file", "roles",
          "user_roles", "min_password", "can_mail", "portal_url", "portal_settable", "invitation_hours", "link_names"),
         needs_agent_api=4),
    Page("pullers", r"pullers", "pullers.html",
         ("pullers", "links", "pullers_file", "keep_hours", "max_file_mib", "now", "publications", "publications_file",
          "instances", "link_targets", "commands", "connections", "connections_file"), needs_agent_api=4),
    Page("server_form", rf"config/new|config/(?P<name>{_SEGMENT})/(?P<form>edit|clone)", "server_form.html",
         ("title", "model"), needs_agent_api=4),
    Page("options", r"options", "options.html",
         ("portal_url", "portal_refused", "options", "options_file", "machine", "font_sizes", "idle_hours",
          "retention_limits", "syslog_protocols", "metrics", "minutes_limits", "after_limits", "every_limits",
          "webhooks", "machine_logo"), needs_agent_api=4),
)
BY_NAME = {p.name: p for p in PAGES}
# the least any page here needs: below it, every page of the machine is its own
LEAST_NEEDED = min(p.needs_agent_api for p in PAGES)
# the most any page here needs: a machine in agent mode below it is offered Update OmniaCLI as needed (AD-017 §17.3)
NEWEST_NEEDED = max(p.needs_agent_api for p in PAGES)
# the pages of class B (slice 4): drawn from page_view, and the parity harness draws each again from its model
CLASS_B = tuple(BY_NAME[name] for name in (
    "account", "jobs", "job", "workspaces", "destinations", "access", "config", "credentials", "enterprise", "links",
    "tunnels", "groups", "plugins", "schedules"))
# the other pages of class C (slice 5), drawn the same way
CLASS_C = tuple(BY_NAME[name] for name in (
    "index", "dashboard_servers", "workspace", "users", "pullers", "server_form", "options"))
# every page drawn from page_view
BY_MODEL = CLASS_B + CLASS_C
# of those, the pages of who logged in there (its account, its tunnels): a link has none on the machine, which
# refuses their model as it refuses their page (404, 403); drawn here as this panel's error page, never with a
# context (the person's own are this panel's /account and /tunnels; AD-018 brings the customers' account here)
PERSON_PAGES = frozenset({"account", "tunnels"})


# the only pages of a linked machine whose HTML passes through a link whose pages are drawn here (AD-017 slice 5),
# with the line that says so: an invitation (of who logs in there, class D) and a plugin's own pages (under /x/, of
# a plugin's code); anything else not drawn here is data, or this panel's «not found»
MACHINE_OWN = (re.compile(r"invite/[A-Za-z0-9_-]+"), re.compile(r"x/[a-z0-9_-]+/[A-Za-z0-9._~/-]+"))


def machine_own(path: str) -> bool:
    """Whether path (under /links/<name>/) is a page of MACHINE_OWN: no . or .. segment either."""
    return (not any(s in (".", "..") for s in path.split("/"))
            and any(p.fullmatch(path) for p in MACHINE_OWN))


def match(path: str) -> tuple[Page, dict[str, str]] | None:
    """The page drawn here for path (under /links/<name>/, no query) and its parameters; None: none."""
    for page in PAGES:
        found = page.pattern.fullmatch(path)
        if found is not None:
            return page, {k: v for k, v in found.groupdict().items() if v is not None}
    return None


def drawn_for(link: str, role: str, development: bool, person: str, zone: str = "") -> dict:
    """What a page of link's machine drawn here says of who looks (the globals of the templates, app._page_context):
    what the machine's role there allows, never more, as the machine draws it for a link (no account of its own,
    the person's name); zone: the machine's clock (its moments are written by it), "" for this panel's."""
    rank = ROLES.index(role) if role in ROLES else 0
    found = {"drawn_here": link, "can_act": rank >= 1, "is_admin": rank >= 2, "own_user_shells": development,
             "scoped": False, "my_workspaces": [],
             # the crumb «Workspaces» of a workspace's pages (destinations, access log): the machine's admins
             "workspaces_menu": rank >= 2,
             # a person through a link has no account there: what a page offers to who logged in there (the
             # commands of a puller's connection) stays off, as on the machine's own page
             "account": None, "central_user": person}
    if zone:
        found["server_zone"] = zone
    return found


def plugin_versions(registry: plugins.Registry | None = None) -> dict[str, str]:
    """{name: version} of the plugins loaded here; a built-in one is BUILT_IN (it is the package's)."""
    registry = registry or plugins.registry()
    return {i.name: BUILT_IN if i.source == "built-in" else (i.version or "") for i in registry.found
            if i.plugin is not None}


def agent_of(info: dict) -> dict:
    """What a machine's GET /api/info says of the contract: agent_api (0 when it says nothing or nonsense),
    plugins ({name: version}, only strings), its version, its panel (PANEL_AGENT, else full: one that says
    nothing is full, as before)."""
    api = info.get("agent_api")
    found = info.get("plugins")
    return {"agent_api": api if isinstance(api, int) and not isinstance(api, bool) and api >= 0 else 0,
            "plugins": {str(k): str(v) for k, v in found.items() if isinstance(v, str)} if isinstance(found, dict) else {},
            "version": str(info.get("version") or "?"),
            "panel": PANEL_AGENT if info.get("panel") == PANEL_AGENT else PANEL_FULL}


def update_need(agent: dict | None, agent_mode: bool) -> str:
    """How Update OmniaCLI is offered for a linked machine (AD-017 §17.3 point 3): "needed" for a machine in
    agent mode whose agent_api is behind what the pages here ask (NEWEST_NEEDED), "info" for one that is not
    (a newer version is information, not a need); "" for a full machine (as on its own page)."""
    if not agent_mode:
        return ""
    return "needed" if (agent or {}).get("agent_api", 0) < NEWEST_NEEDED else "info"


def contract(needs: int, agent_api: int, theirs: dict[str, str], ours: dict[str, str]) -> str:
    """"" when this panel draws a page that needs `needs` of a machine at agent_api with plugins `theirs`;
    else why the machine's own page passes: BEHIND, AHEAD or PLUGINS."""
    if agent_api > CENTRAL_AGENT_API:
        return AHEAD
    if agent_api < needs or agent_api < OLDEST_READ:
        return BEHIND
    if any(ours.get(name) != version for name, version in theirs.items()):
        return PLUGINS
    return ""


def plugins_apart(theirs: dict[str, str], ours: dict[str, str]) -> list[str]:
    """The machine's plugins this panel has not, or at another version: name version."""
    return [f"{name} {version}".strip() for name, version in sorted(theirs.items()) if ours.get(name) != version]


# what a page drawn here takes from the machine through its link: data, files to save, images; never a
# document or a script (AD-017 §17.5 decision 1: a machine that writes HTML under this panel's origin runs it
# with the session of who looks)
_DATA_TYPES = frozenset({
    "application/json", "application/x-ndjson", "text/plain", "text/csv", "application/octet-stream",
    "application/zip", "application/x-tar", "application/gzip", "application/sql",
    "image/png", "image/jpeg", "image/gif", "image/webp", "image/x-icon", "image/vnd.microsoft.icon",
})


def load_panels(path: Path) -> set[str]:
    """The links whose machine said panel: agent; none yet, or unreadable, is none."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {str(k) for k, v in data.items() if v == PANEL_AGENT} if isinstance(data, dict) else set()


def save_panels(path: Path, agents: set[str]) -> None:
    """Write it atomically, readable by its owner only (it names the links)."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({name: PANEL_AGENT for name in sorted(agents)}, indent=1) + "\n")
    os.replace(tmp, path)


def load_seen(path: Path) -> dict[str, int]:
    """{link: the highest agent_api its machine said}; none yet, or unreadable, is none."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, int) and not isinstance(v, bool) and v > 0} \
        if isinstance(data, dict) else {}


def save_seen(path: Path, seen: dict[str, int]) -> None:
    """Write it atomically, readable by its owner only (it names the links)."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(dict(sorted(seen.items())), indent=1) + "\n")
    os.replace(tmp, path)


def is_data(content_type: str, disposition: str, has_body: bool = True) -> bool:
    """Whether an answer of a machine passes for a page drawn here: JSON and the other data types, a file to save
    (Content-Disposition: attachment), an answer without a body (a redirect, 204, 304)."""
    media = content_type.split(";", 1)[0].strip().lower()
    if disposition.strip().lower().startswith("attachment"):
        return True
    if not media:
        return not has_body
    return media in _DATA_TYPES or (media.startswith("application/") and media.endswith("+json"))


_BODY = re.compile(rb"<body\b[^>]*>", re.I)


def with_line(html: bytes, line: str) -> bytes:
    """The machine's page with line (HTML) on top of its body; a part of a page (no <body>: the Dashboard's table of
    instances its script asks again) as it is: the page it goes in has the line already."""
    found = _BODY.search(html)
    if found is None:
        return html
    return html[:found.end()] + line.encode() + html[found.end():]


# --- the model of an instance's page (AD-017 §9 slice 3) -----------------------------------------------------
#
# server.html reads live objects: the instances (Instance), the server, the plugin's actions (Action) and their
# fields, the jobs, the workspace. Their model is a list of fields by name for each (never a dump of the
# dataclass: an Instance holds its options, a Repository its git credentials, a plugin's db_shell the database's
# password); what the page calls on them (the plugin's describe, db_shell, backup_fields; the window of its auto
# update) is answered here once, as a value. Everything else of the context is plain values already (the routes
# make those dicts field by field): taken as they are, and any other object is refused (ModelError), never
# written with its repr.

# a dialog of a plugin the page draws once (Action.dialog): <plugin>/<file>.html
_DIALOG = re.compile(r"[a-z0-9_-]+/[A-Za-z0-9_-]+\.html")


class ModelError(TypeError):
    """A value of a page's context that has no model (an object this module does not know)."""


def field_model(field: Field) -> dict:
    """A plugin's Field as the pages read it (a dialog of an action, a backup's choices)."""
    return {"key": field.key, "label": field.label, "kind": field.kind, "default": plain(field.default, field.key),
            "required": field.required, "placeholder": field.placeholder, "help": field.help,
            "choices": [[plain(v, field.key), str(label)] for v, label in field.choices],
            "restart_types": list(field.restart_types), "disabled_placeholder": field.disabled_placeholder,
            "shown_with": field.shown_with, "threshold": field.threshold}


def action_model(action: Action) -> dict:
    """A plugin's action as its button draws it (_actions.html): never its run, prepare or confirm callables."""
    return {"id": action.id, "label": action.label, "icon": action.icon, "tooltip": action.tooltip,
            "dialog": action.dialog, "params": [field_model(f) for f in action.params]}


def instance_model(inst: Instance | None) -> dict | None:
    """What the page reads of an instance: no option, no password, no git credential."""
    if inst is None:
        return None
    return {
        "key": inst.key, "server": inst.server, "role": inst.role, "type": inst.type, "web_url": inst.web_url,
        "log_label": inst.log_label, "claude_dir": str(inst.claude_dir) if inst.claude_dir else None,
        "package": {"name": inst.package.name, "url": inst.package.url} if inst.package is not None else None,
        "repositories": [{"name": r.name, "branch": r.branch, "path": str(r.path)} for r in inst.repositories],
        "restart": {"type": inst.restart.type, "service": inst.restart.service},
        "auto_update": {"enabled": inst.auto_update.enabled, "interval_minutes": inst.auto_update.interval_minutes,
                        "window_label": inst.auto_update.window_label()},
        "plugin": plugin_model(inst) if inst.plugin is not None else None,
    }


def plugin_model(inst: Instance) -> dict:
    """What the page reads and asks of an instance's plugin, answered once: whether it has a database shell (never
    the shell's command and its environment, which holds the database's password), its database's name."""
    plugin = inst.plugin

    def asked(call, default):
        # what the page would have asked while drawing: a configuration it cannot read is no shell, as there
        try:
            return call()
        except (OSError, ValueError):
            return default
    found = {"label": plugin.label, "has_backup": bool(plugin.has_backup), "restores_sql": bool(plugin.restores_sql),
             "backup_fields": [field_model(f) for f in plugin.backup_fields()],
             "describe": {"database": str((asked(lambda: plugin.describe(inst), None) or {}).get("database") or "")},
             "db_shell": bool(asked(lambda: plugin.db_shell(inst), None)),
             "db_shell_as_service": bool(asked(lambda: plugin.db_shell_as_service(inst), False))}
    if hasattr(plugin, "container_shell"):
        # the page asks whether it is defined, never calls it
        found["container_shell"] = True
    return found


def job_model(job: Job) -> dict:
    """A job as the pages read it (an instance's last action, the Jobs list, a job's page): no lines (its page's
    script reads them from /api/jobs/<id>), no result."""
    return {"id": job.id, "title": job.title, "status": job.status, "outcome": job.outcome, "created": job.created,
            "finished": job.finished, "duration": job.duration, "error": job.error, "warnings": list(job.warnings),
            "instances": list(job.instances), "servers": job.servers, "source": job.source}


def plain(value, where: str = ""):
    """value as JSON values: the objects of the page by their models, any other object refused."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): plain(v, f"{where}.{k}") for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v, f"{where}[]") for v in value]
    if isinstance(value, Instance):
        return instance_model(value)
    if isinstance(value, Server):
        return {"name": value.name, "production": instance_model(value.production), "test": instance_model(value.test)}
    if isinstance(value, Action):
        return action_model(value)
    if isinstance(value, Field):
        return field_model(value)
    if isinstance(value, Job):
        return job_model(value)
    if isinstance(value, Workspace):
        return {"name": value.name, "label": value.label}
    if isinstance(value, Path):
        return str(value)
    raise ModelError(f"{where or 'a value'}: a {type(value).__name__} has no model")


def server_model(context: dict) -> dict:
    """The model of an instance's page from its context (app.server_context): SERVER_FIELDS, each as plain
    values; of the Odoo configuration of a workspace's instance whether it has options of its own, never them."""
    model = {}
    for name in SERVER_FIELDS:
        value = context.get(name)
        if name == "odoo_params" and isinstance(value, dict):
            value = {"parameters": value.get("parameters") or [], "debug": bool(value.get("debug")),
                     "mail_capture": bool(value.get("mail_capture")), "has_extra": bool(value.get("has_extra"))}
        elif name == "settings_rows" and isinstance(value, dict):
            # a password among a plugin's settings is never in a page
            value = {key: [row for row in rows if row[2] != "password"] for key, rows in value.items()}
        model[name] = plain(value, name)
    return model


class Fields(dict):
    """A dict of a model whose keys a template reads as attributes too (inst.key as inst["key"])."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None


class PluginFields(Fields):
    """An instance's plugin as server.html calls it: what the machine answered once."""

    def describe(self, *_):
        return self.get("describe") or Fields()

    def db_shell(self, *_):
        return self.get("db_shell")

    def db_shell_as_service(self, *_):
        return self.get("db_shell_as_service")

    def backup_fields(self):
        return self.get("backup_fields") or []


class AutoUpdateFields(Fields):
    """An instance's auto update, its window as the machine wrote it."""

    def window_label(self):
        return self.get("window_label") or ""


def fields_of(value):
    """A model's JSON values as a template reads them."""
    if isinstance(value, dict):
        return Fields({k: fields_of(v) for k, v in value.items()})
    if isinstance(value, list):
        return [fields_of(v) for v in value]
    return value


def instance_view(model) -> Fields | None:
    """An instance's model as server.html reads an Instance."""
    if not isinstance(model, dict):
        return None
    view = fields_of(model)
    if isinstance(model.get("plugin"), dict):
        view["plugin"] = PluginFields(fields_of(model["plugin"]))
    if isinstance(model.get("auto_update"), dict):
        view["auto_update"] = AutoUpdateFields(fields_of(model["auto_update"]))
    return view


# the keys whose text a page uses as an address (a link, a form's action, a request of its script)
_URL_KEYS = frozenset({"url", "web_url", "open", "page", "ci_url", "deploys_url", "install", "upload", "backups",
                       "preview", "users"})
# of those, the links to elsewhere (the Odoo of an instance, its repository): http or https only; every other one
# is an address of the machine's own, under the link's prefix
_EXTERNAL = re.compile(r"(?:.*\.)?(?:web_url|open)|.*\.repo\.url|stagings\.parent\.url|stagings\.rows\[\]\.url")
# an address of the machine's own that is not under a key of _URL_KEYS
_INTERNAL_AT = frozenset({"enterprise.pgvector.machine"})
# under such a key, a text the page shows and never opens (where an instance's Debian package is published)
_TEXT_AT = re.compile(r".*\.package\.url")
_CONTROL = re.compile(r"[\x00-\x20\x7f\\]")


def under(prefix: str, address: str) -> bool:
    """Whether address is a path of this site under prefix (/links/<m>): no scheme, no host, no . or .. segment
    (encoded or not), no backslash, space or control character; prefix "" (a machine's own page): any absolute
    path."""
    if not isinstance(address, str) or _CONTROL.search(address) or address.startswith("//"):
        return False
    parts = urlsplit(address)
    if parts.scheme or parts.netloc or not parts.path.startswith("/"):
        return False
    path = unquote(parts.path)
    if _CONTROL.search(path) or any(segment in (".", "..") for segment in path.split("/")):
        return False
    return not prefix or path == prefix or path.startswith(prefix + "/")


def elsewhere(address: str) -> bool:
    """Whether address is a link to another site: http or https, with a host."""
    if not isinstance(address, str) or _CONTROL.search(address):
        return False
    parts = urlsplit(address)
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def checked_addresses(value, prefix: str, where: str = "") -> None:
    """ValueError when an address of a model is not one the page may use: a link to elsewhere that is not http(s),
    an address of the machine that is not under its link's prefix (another machine, this panel's own routes)."""
    if isinstance(value, dict):
        for key, item in value.items():
            at = f"{where}.{key}" if where else str(key)
            if (isinstance(item, str) and item and (key in _URL_KEYS or at in _INTERNAL_AT)
                    and not _TEXT_AT.fullmatch(at)):
                ok = elsewhere(item) if _EXTERNAL.fullmatch(at) else under(prefix, item)
                if not ok:
                    raise ValueError(f"its page has the address {item[:80]!r} at {at}, which is not one it may use")
            else:
                checked_addresses(item, prefix, at)
    elif isinstance(value, list):
        for item in value:
            checked_addresses(item, prefix, f"{where}[]")


def server_view(model: dict, prefix: str) -> dict:
    """The context server.html is drawn with, from the model a machine answered (its fields by name), its
    addresses under prefix (the link's /links/<m>; "" for a machine's own page); ValueError when it names a dialog
    this panel cannot draw or an address it may not use."""
    checked_addresses(model, prefix)
    context = {k: fields_of(v) for k, v in model.items()}
    context["instances"] = [instance_view(i) for i in model.get("instances") or []]
    server = model.get("server")
    if isinstance(server, dict):
        context["server"] = Fields(name=server.get("name"), production=instance_view(server.get("production")),
                                   test=instance_view(server.get("test")))
    dialogs = [str(d) for d in model.get("dialogs") or []]
    loaded = plugins.registry()
    for dialog in dialogs:
        if not _DIALOG.fullmatch(dialog) or loaded.get(dialog.partition("/")[0]) is None:
            raise ValueError(f"its page names the dialog {dialog}, which this panel has not")
    context["dialogs"] = dialogs
    return context


def moved_away_view(model: dict, prefix: str) -> dict:
    """The context of the page of an instance placed on another machine whose copy there was deleted
    (moved_away.html, class D), drawn here from its small model (its name and its move), never the machine's HTML."""
    checked_addresses(model, prefix)
    server = model.get("server") if isinstance(model.get("server"), dict) else {}
    move = model.get("move")
    if not isinstance(server.get("name"), str) or not isinstance(move, dict):
        raise ValueError("its page of a move has no server or no move")
    return {"server": Fields(name=server["name"]), "move": fields_of(move)}


# --- the models of the pages of class B (AD-017 §9 slice 4) ---------------------------------------------------
#
# Each page's context is made on the machine by the route that draws its own page (app.page_contexts); its model
# is the page's fields by name, each as plain values (page_model). The few live objects these pages read have a
# list of fields of their own here: a server of the Servers page, a link of the Links page (never its token), a
# plugin found, a job. Any other object is refused (ModelError).

def config_server_model(server: Server) -> dict:
    """A server as the Servers page (config.html) reads it: its state, of each instance the words it is found by and
    its repositories (name and branch), never its options or its repositories' credentials."""
    instances = {}
    for role, inst in server.instances.items():
        try:
            words = [str(w) for w in inst.plugin.search_text(inst)] if inst.plugin is not None else []
        except (OSError, ValueError):
            # what the page would have asked while drawing: a configuration it cannot read has no words, as there
            words = []
        instances[role] = {"key": inst.key, "plugin": {"search_text": words},
                           "repositories": [{"name": r.name, "branch": r.branch} for r in inst.repositories]}
    return {"name": server.name, "type": server.type, "status": server.status, "active": server.active,
            "missing_plugin": server.missing_plugin, "error": server.error, "instances": instances}


def link_model(link) -> dict:
    """A link as the Links page (links.html) reads it: where it goes, how, its SSH route and the host key it fixed
    (public); never its token."""
    ssh = link.ssh
    return {"name": link.name, "url": link.url, "ca_file": link.ca_file, "pages": link.pages,
            "remote": list(link.remote) if ssh is not None else [],
            "ssh": {"host": ssh.host, "port": ssh.port, "user": ssh.user, "host_key": ssh.host_key}
            if ssh is not None else None}


def plugin_found_model(info: plugins.PluginInfo) -> dict:
    """A plugin found, as the Plugins page reads it: its name, where it comes from, why it failed."""
    return {"name": info.name, "target": info.target, "source": info.source, "version": info.version,
            "error": info.error, "plugin": {"label": info.plugin.label} if info.plugin is not None else None}


def page_model(name: str, context: dict) -> dict:
    """The model of a page of class B from the context its route made: its fields by name (those the route set),
    each as plain values; the objects of the page by their models above."""
    model = {}
    for field in BY_NAME[name].fields:
        if field not in context:
            continue
        value = context[field]
        if name == "config" and field == "servers":
            value = [config_server_model(s) for s in value]
        elif name == "links" and field == "links":
            value = [link_model(link) for link in value]
        elif name == "plugins" and field == "found":
            value = [plugin_found_model(i) for i in value]
        elif name == "plugins" and field == "missing":
            value = [{"name": s.name, "type": s.type, "error": s.error} for s in value]
        elif name == "plugins" and field == "source":
            value = {"index": value.index, "public_key": value.public_key}
        model[field] = plain(value, field)
    return model


class Drawn(dict):
    """The context of a page drawn from its model (page_view): the parity harness draws a page again only when its
    context is a live one."""


class PluginWords(Fields):
    """An instance's plugin as config.html calls it: the words the machine answered once."""

    def search_text(self, *_):
        return self.get("search_text") or []


# a name a page puts in an address of the machine (a job's id, a server's, a workspace's): one segment of a path,
# never . or .., no slash (the machine's own jobs are of _machine); an instance's key (server/role) is two of them
_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._~@+-]*")
# (a place that ends in [] is a list of names: the value there is a list, never a text the page would go through
# letter by letter)
_NAMES_AT = {
    "jobs": ("jobs[].id",),
    "job": ("job.id", "live[]", "job.instances[]"),
    "destinations": ("rows[].name", "rows[].workspace", "workspace", "ws.name"),
    "access": ("entries[].job[]", "ws.name"),
    "config": ("servers[].name",),
    "enterprise": ("copies[].users[]",),
    "links": ("links[].name",),
    "schedules": ("schedules[].name", "schedules[].data.instance", "schedules[].runs[].job"),
    "workspaces": ("handover_ws",),
    # slice 5
    "index": ("rows[].name", "rows[].parent", "jobs[].id", "load_workspaces[][]"),
    "dashboard_servers": ("rows[].name", "rows[].parent"),
    "workspace": ("ws.name", "members[].name", "servers[]", "instances[]", "relay_links[]", "links[]", "db_links[]",
                  "tokens[].name", "grants[].id", "sites[].instance",
                  # the banners of a handover: the id its script POSTs to, the jobs it opens
                  "handover_in.id", "handover_in.job", "handover_out.id", "handover_out.relight.job",
                  "handover_out.deletion.job"),
    "users": ("users[].name", "users[].workspaces[]", "links[].name", "tokens[].name", "tokens[].workspace",
              "grants[].id", "link_names[]"),
    "pullers": ("pullers[].puller", "connections[].name", "publications[].instance", "instances[]", "links[]",
                "link_targets[]"),
    "server_form": ("model.original", "model.name", "model.links[]", "model.connections[]"),
}
# the addresses of the pages of class B, by their exact place (never by the name of a key: these pages' mappings are
# keyed by the customer's own names, a storage root, a token, a subdomain called «backups» or «preview»), all links to
# elsewhere (http or https with a host); the address of the destinations page's requests is checked apart
_ELSEWHERE_AT = {
    "links": ("links[].url",),
    "users": ("portal_url",),
    "options": ("portal_url",),
}
# the logo of a machine's panel (Options › Whitelabel), drawn under its prefix: its route and the word of its version
_LOGO = re.compile(r"/brand/logo\?v=[A-Za-z0-9._-]*")


def a_name(value) -> bool:
    """Whether value is a name a page may put in an address: one or two segments (server/role), each a name."""
    return isinstance(value, str) and 0 < len(value) <= 200 and all(
        _NAME.fullmatch(part) for part in value.split("/", 1))


def places(value, where: str = ""):
    """(place, value) of every value of a model, its place written as the places of _NAMES_AT (jobs[].id)."""
    yield where, value
    if isinstance(value, dict):
        for key, item in value.items():
            yield from places(item, f"{where}.{key}" if where else str(key))
    elif isinstance(value, list):
        for item in value:
            yield from places(item, f"{where}[]")


def checked_names(value, names: tuple[str, ...]) -> None:
    """ValueError when a value at one of the places names puts in an address is not a name (a .. that would take
    the page's links out of its machine, a slash, a space), or a list of names is not a list."""
    for at, found in places(value):
        if at + "[]" in names and found is not None and not isinstance(found, list):
            raise ValueError(f"its page has {str(found)[:80]!r} at {at}, which is not a list of names")
        if at in names and found not in (None, "") and not a_name(found):
            raise ValueError(f"its page has {str(found)[:80]!r} at {at}, which is not a name")


def checked_elsewhere(value, at_places: tuple[str, ...]) -> None:
    """ValueError when a value at one of at_places is not a link to elsewhere (http or https, with a host)."""
    for at, found in places(value):
        if at in at_places and found not in (None, "") and not elsewhere(found):
            raise ValueError(f"its page has the address {str(found)[:80]!r} at {at}, which is not one it may use")


def section_rows(sections) -> list:
    """The rows of the sections of the list widget (listgroups.sections), at any depth."""
    found = []
    for section in sections if isinstance(sections, list) else []:
        if isinstance(section, dict):
            found += [row for row in section.get("items") or [] if isinstance(row, dict)]
            found += section_rows(section.get("groups"))
    return found


def instance_rows(context: dict, prefix: str) -> None:
    """The Dashboard's rows of instances (index, dashboard_servers) as the page opens them: each under the link's
    prefix, of this machine (a row of the machine's own instances is always so on its page: the rows of a link's
    machine are another page's, Instances), never a base or a link of the machine's word. Its name and its parent
    are names (_NAMES_AT), in the sections too."""
    rows = section_rows(context.get("sections"))
    names = [(row.get("name"), row.get("parent")) for row in rows]
    if any(not a_name(n) or p not in (None, "") and not a_name(p) for n, p in names):
        raise ValueError("its page has a row of an instance whose name is not a name")
    for row in [*(context.get("rows") or []), *rows]:
        if isinstance(row, dict):
            row["base"], row["link"] = prefix, ""


def server_form_view(context: dict, theirs: dict | None = None) -> None:
    """A server's form drawn here: what it offers of the plugins (their fields, the buttons of a field and the
    routes they ask) is this panel's own, as its contract wants them the same as the machine's (never a route of the
    machine's word, which the form's script would fetch and open); of them, only those the machine has (theirs: the
    plugins its contract says; None, a machine's own page: all)."""
    model = context.get("model")
    if not isinstance(model, dict):
        raise ValueError("its page of a server's form has no model")
    model["types"] = fields_of({n: t for n, t in plugin_types().items() if theirs is None or n in theirs})
    model["default_type"] = plugins.DEFAULT_TYPE


def form_field(field: Field) -> dict:
    """A plugin's Field for the scripts of the pages (a server's form, a backup's and a restore's choices)."""
    return {"key": field.key, "label": field.label, "kind": field.kind, "default": field.default,
            "required": field.required, "placeholder": field.placeholder, "help": field.help,
            "choices": [list(c) for c in field.choices], "restart_types": list(field.restart_types),
            "disabled_placeholder": field.disabled_placeholder,
            "shown_with": field.shown_with, "threshold": field.threshold,
            "buttons": [{"label": b.label, "route": b.route, "title": b.title, "confirm": b.confirm,
                         "only_with_value": b.only_with_value} for b in field.buttons]}


def plugin_types() -> dict:
    """What a server's form offers: the plugins loaded here, with their fields."""
    return {n: {"label": p.label, "has_process": p.has_process, "has_compose": p.has_compose,
                "has_restore": p.has_restore, "fields": [form_field(f) for f in p.fields()]}
            for n, p in plugins.registry().plugins.items()}


def page_view(name: str, model: dict, prefix: str, lang: str = i18n.DEFAULT, theirs: dict | None = None) -> Drawn:
    """The context of a page of class B drawn here, from the model a machine answered (its fields by name), under
    prefix (the link's /links/<m>; "" for a machine's own page), its texts in lang; ValueError when it has an address
    it may not use (at the places of _ELSEWHERE_AT) or a name that would leave the machine (_NAMES_AT)."""
    if name in PERSON_PAGES:
        raise ValueError(f"the page {name} is of who logged in there: a link has none")
    model = {k: model[k] for k in BY_NAME[name].fields if k in model}
    checked_elsewhere(model, _ELSEWHERE_AT.get(name, ()))
    checked_names(model, _NAMES_AT.get(name, ()))
    context = Drawn({k: fields_of(v) for k, v in model.items()})
    if name == "workspaces" and prefix:
        # the list's machines are this panel's links on its own page, never a machine's word: through a link the
        # machine has none (listed_links of a link), and a name there would make rows and forms under /links/<name>
        context["links"], context["machine_column"] = [], False
    if name in ("index", "dashboard_servers"):
        instance_rows(context, prefix)
    if name == "index" and prefix:
        # Linked: this panel's links on its own page (a link's machine has none for a link)
        context["machine_links"], context["linked_sections"] = [], []
    elif name == "server_form":
        server_form_view(context, theirs)
    elif name == "options":
        logo = model.get("machine_logo")
        if logo not in (None, "") and not (isinstance(logo, str) and _LOGO.fullmatch(logo)):
            raise ValueError(f"its page has the logo {str(logo)[:80]!r}, which is not one it may use")
    if name == "config":
        for server in context.get("servers") or []:
            for inst in (server.get("instances") or {}).values():
                inst["plugin"] = PluginWords(inst.get("plugin") or {})
    elif name == "destinations":
        api = model.get("api")
        if not isinstance(api, str) or not under("", api):
            raise ValueError(f"its page has the address {str(api)[:80]!r} of its requests")
        # the kinds of destination in the words of who looks: the machine says them in English
        context["kinds"] = Fields({
            k: Fields({**v, "label": i18n.tr(lang, v.get("label")),
                       "settings": [Fields({**s, "label": i18n.tr(lang, s.get("label")), "help": i18n.tr(lang, s.get("help"))})
                                    for s in v.get("settings") or []]})
            for k, v in (context.get("kinds") or {}).items()})
    return context
