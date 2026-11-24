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

from . import plugins
from .auth import ROLES
from .config import Instance, Server, Workspace
from .jobs import Job
from .plugin_api import Action, Field

# what this machine's API speaks: GET /api/info says it, GET /api/pages/* answers from it. 2: the model of an
# instance's page (GET /api/pages/server, AD-017 §9 slice 3)
AGENT_API = 2
# the newest a panel of this version reads; it reads that one and the one before it (AD-017 §10.6)
CENTRAL_AGENT_API = 2
OLDEST_READ = CENTRAL_AGENT_API - 1

# a link's pages (links.yaml, pages:): drawn here, or the machine's own as before (the undo switch)
PAGES_HERE = "here"
PAGES_MACHINE = "machine"
PAGES_WAYS = (PAGES_HERE, PAGES_MACHINE)

# why the machine's own page passes (the line on top says it)
BEHIND = "behind"  # the machine is older than the page asks
AHEAD = "ahead"  # the panel is older than the machine
PLUGINS = "plugins"  # a plugin of the machine this panel has not, or at another version

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
)
BY_NAME = {p.name: p for p in PAGES}
# the least any page here needs: below it, every page of the machine is its own
LEAST_NEEDED = min(p.needs_agent_api for p in PAGES)


def match(path: str) -> tuple[Page, dict[str, str]] | None:
    """The page drawn here for path (under /links/<name>/, no query) and its parameters; None: none."""
    for page in PAGES:
        found = page.pattern.fullmatch(path)
        if found is not None:
            return page, found.groupdict()
    return None


def drawn_for(link: str, role: str, development: bool, person: str, zone: str = "") -> dict:
    """What a page of link's machine drawn here says of who looks (the globals of the templates, app._page_context):
    what the machine's role there allows, never more, as the machine draws it for a link (no account of its own,
    the person's name); zone: the machine's clock (its moments are written by it), "" for this panel's."""
    rank = ROLES.index(role) if role in ROLES else 0
    found = {"drawn_here": link, "can_act": rank >= 1, "is_admin": rank >= 2, "own_user_shells": development,
             "scoped": False, "my_workspaces": [],
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
    plugins ({name: version}, only strings), its version."""
    api = info.get("agent_api")
    found = info.get("plugins")
    return {"agent_api": api if isinstance(api, int) and not isinstance(api, bool) and api >= 0 else 0,
            "plugins": {str(k): str(v) for k, v in found.items() if isinstance(v, str)} if isinstance(found, dict) else {},
            "version": str(info.get("version") or "?")}


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
    """The machine's page with line (HTML) on top of its body."""
    found = _BODY.search(html)
    at = found.end() if found else 0
    return html[:at] + line.encode() + html[at:]


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
    """A job as the page's last action reads it: no lines, no result."""
    return {"id": job.id, "title": job.title, "status": job.status, "outcome": job.outcome, "created": job.created,
            "duration": job.duration, "error": job.error, "warnings": list(job.warnings)}


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
        return {"name": value.name}
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
