# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Load and validate the OmniaCLI YAML configuration."""

from __future__ import annotations

import base64
import datetime
import hashlib
import ipaddress
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from .auth import ROLES as ACCESS_ROLES
from .auth import WORKSPACE_ROLE
from . import listgroups

ROLES = ("production", "test")
# the roles a user may have on the machine: the access roles, or none but
# those of its workspaces
USER_ROLES = (*ACCESS_ROLES, WORKSPACE_ROLE)
# an MCP token saved without a role, as before tokens had one: what such a
# token could do then (read, update_test, backup_test, download backups)
TOKEN_DEFAULT_ROLE = "developer"
# process: OmniaCLI starts the service itself (odoo-bin...), its output going
# to a log of its own; compose: a Docker Compose project, the service being
# its folder (only for a plugin that runs in one: has_compose)
RESTART_TYPES = ("systemd", "initd", "process", "compose")
# the omniacli command of this venv: a compose project's commands go
# through its root helper, compose-helper (composehelper.py)
OMNIACLI_COMMAND = Path(sys.executable).parent / "omniacli"
# a server name is part of URLs and of the job keys
SERVER_STATUSES = ("active", "draft")
SERVER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
# the system user an instance's venv commands run as (sudo -u)
USER_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*\$?")


# the kinds of machine: on a development machine nothing is restricted, on a
# server nobody gets a shell of OmniaCLI's own user (parse_machine)
MACHINE_SERVER = "server"
MACHINE_DEVELOPMENT = "development"
MACHINE_KINDS = (MACHINE_SERVER, MACHINE_DEVELOPMENT)


class ConfigError(Exception):
    pass


@dataclass
class Repository:
    name: str
    path: Path
    branch: str
    remote: str = "origin"
    # the -c options of the server's git credentials (gitcredentials.bind),
    # given to every git command on it; not saved
    git_options: tuple = field(default=(), compare=False, repr=False)

    def git(self, *args: str) -> list[str]:
        """A git command on this repository, with its server's credentials."""
        return ["git", *self.git_options, *args]


@dataclass
class Restart:
    type: str
    service: str
    sudo: bool = True

    def command(self, action: str = "restart", *args: str) -> list[str]:
        """The restart command of a systemd or init.d service (a systemd
        service is also started and stopped: action start or stop), or of a
        compose project, through the root helper (also actions state and
        logs, whose arguments follow)."""
        if self.type == "systemd":
            # without --no-ask-password systemctl waits on a polkit prompt
            cmd = ["systemctl", "--no-ask-password", action, self.service]
        elif self.type == "compose":
            cmd = [str(OMNIACLI_COMMAND), "compose-helper", self.service, action, *args]
        else:
            cmd = [f"/etc/init.d/{self.service}", action]
        return (["sudo", "-n"] + cmd) if self.sudo else cmd


@dataclass
class AutoUpdate:
    enabled: bool = False
    interval_minutes: int = 10
    window_start: datetime.time | None = None
    window_end: datetime.time | None = None

    def in_window(self, now: datetime.time) -> bool:
        return in_window(self.window_start, self.window_end, now)

    def window_label(self) -> str:
        return window_label(self.window_start, self.window_end)


def in_window(start: datetime.time | None, end: datetime.time | None, now: datetime.time) -> bool:
    """Whether now is in start-end (no window: always)."""
    if start is None or end is None:
        return True
    if start <= end:
        return start <= now < end
    # The window crosses midnight, e.g. 20:00-06:00
    return now >= start or now < end


def window_label(start: datetime.time | None, end: datetime.time | None) -> str:
    if start is None or end is None:
        return "always"
    return f"{start:%H:%M}-{end:%H:%M}"


@dataclass
class LinkSource:
    """Where an instance's database may be copied from: an instance of a
    server of a linked OmniaCLI (links file), backed up there and restored
    here; or (kind puller) the instance a puller connection reaches, link
    then naming the connection, server and role empty."""
    link: str
    server: str
    role: str
    kind: str = "link"

    @property
    def is_puller(self) -> bool:
        return self.kind == "puller"

    @property
    def label(self) -> str:
        if self.is_puller:
            return f"puller {self.link}"
        return f"{self.link} › {self.server}/{self.role}"

    def to_dict(self) -> dict:
        if self.is_puller:
            return {"puller": self.link}
        return {"link": self.link, "server": self.server, "role": self.role}


@dataclass
class Package:
    """A Debian package an instance is installed from (packages.py): url
    publishes the .deb, url.json its manifest (version, sha256, size) and
    url.json.sig the Ed25519 signature of that manifest, made with the
    private key of public_key. folder holds the downloaded file at a fixed
    path, the one sudoers allows to install."""
    url: str
    name: str
    public_key: str
    folder: Path
    server: str
    role: str

    @property
    def deb_path(self) -> Path:
        return self.folder / f"{self.server}_{self.role}.deb"

    @property
    def state_path(self) -> Path:
        """What was installed last: its checksum and version."""
        return self.folder / f"{self.server}_{self.role}.json"


@dataclass
class Instance:
    server: str
    role: str
    repositories: list[Repository]
    # None with restart type compose: the log is docker compose's
    # (plugin.log_service)
    log_file: Path | None
    restart: Restart
    # the plugin of the server's type (plugin_api.ServicePlugin), and its
    # settings of this instance (what its parse_options returned)
    plugin: Any = field(repr=False, compare=False)
    options: Any
    auto_update: AutoUpdate = field(default_factory=AutoUpdate)
    # a backup before what changes its code or its modules (a pull that
    # brings commits, Update modules, a repository's Update, Pull image),
    # the last few kept (ops.backup_before_update)
    backup_before_update: bool = False
    # the system user the type's programs run as, through sudo -n -u (for
    # Odoo the user of its service: its database login, filestore and venv);
    # None: as the user running OmniaCLI. git always runs as that user.
    run_as: str | None = None
    from_link: LinkSource | None = None
    # the base URL users open the instance at (its domain name); None: this
    # computer's localhost at the port the plugin knows
    domain: str | None = None
    # the address the instance's workspace publishes it on (apply_sites), when
    # it has no domain of its own; not in the servers file
    site: str | None = field(default=None, compare=False)
    # the folder the Claude button of the server page opens a shell in, to
    # run claude there; None: no button
    claude_dir: Path | None = None
    # installed from a published Debian package, besides or instead of git
    package: Package | None = None

    @property
    def type(self) -> str:
        return self.plugin.name

    @property
    def web_url(self) -> str | None:
        """Where the instance's web interface is opened: its domain, else its
        workspace's name for it, else http://localhost:<port>; None when none
        is known."""
        if self.domain or self.site:
            return self.domain or self.site
        port = self.plugin.web_port(self)
        return f"http://localhost:{port}" if port else None

    @property
    def keeps_local_changes(self) -> bool:
        """A test instance without auto update may have uncommitted changes:
        someone works on it, and nothing pulls behind their back."""
        return self.role == "test" and not self.auto_update.enabled

    @property
    def key(self) -> str:
        return f"{self.server}/{self.role}"

    @property
    def log_label(self) -> str:
        """Where its log is read from, as the pages show it."""
        if self.log_file is not None:
            return str(self.log_file)
        service = self.plugin.log_service(self)
        return f"docker compose logs {service}" if service else "no log"

    @property
    def pid_file(self) -> Path:
        """Of an instance OmniaCLI starts itself, next to its log."""
        return self.log_file.with_suffix(".pid")

    def repository(self, name: str) -> Repository | None:
        return next((r for r in self.repositories if r.name == name), None)


@dataclass
class Server:
    name: str
    instances: dict[str, Instance]
    # a draft is saved but not live: no dashboard, no actions, no auto update
    # until "Test & activate" passes its checks
    status: str = "active"
    # a draft may be saved incomplete, and a server's plugin may be missing:
    # then it has no instances, data holds the roles as written and error
    # says what is wrong
    data: dict | None = None
    error: str | None = None
    # the plugin that serves it (plugins.DEFAULT_TYPE when the file names none)
    type: str = "odoo"
    # its plugin is not installed or is disabled (error says which)
    missing_plugin: bool = False

    @property
    def active(self) -> bool:
        """Live: active, and its plugin is there (a server whose plugin is
        missing keeps its status, and is live again with the plugin)."""
        return self.status == "active" and not self.missing_plugin

    @property
    def production(self) -> Instance | None:
        return self.instances.get("production")

    @property
    def test(self) -> Instance | None:
        return self.instances.get("test")


def server_text(server: Server) -> str:
    """What the Dashboard filters on: the name, and of each instance the
    service, repository names and branches, and the words its plugin gives
    (Odoo: the database)."""
    parts = [server.name]
    for inst in server.instances.values():
        parts += [*inst.plugin.search_text(inst), inst.restart.service, inst.domain or "",
                  inst.package.name if inst.package else ""]
        parts += [f"{r.name} {r.branch}" for r in inst.repositories]
    return " ".join(parts)


def matches(text: str, query: str) -> bool:
    """True when text contains every word of query, case insensitive."""
    text = text.lower()
    return all(w in text for w in query.lower().split())


def filter_servers(servers: list[Server], query: str = "") -> list[Server]:
    """The servers whose server_text contains every word of query."""
    return [s for s in servers if matches(server_text(s), query)]


@dataclass
class LinkAccess:
    """A linked OmniaCLI instance allowed in: its token's hash and its role."""

    token: str
    role: str


@dataclass
class Access:
    """The credentials of one source: the main config or the users file."""

    users: dict[str, str] = field(default_factory=dict)  # {name: password hash}
    roles: dict[str, str] = field(default_factory=dict)  # {user name: role}
    tokens: dict[str, str] = field(default_factory=dict)  # MCP clients: {name: token hash}
    links: dict[str, LinkAccess] = field(default_factory=dict)
    token_roles: dict[str, str] = field(default_factory=dict)  # {MCP token name: role}
    # {MCP token name: workspace}: such a token sees that workspace only, with its role there
    token_workspaces: dict[str, str] = field(default_factory=dict)


@dataclass
class Settings:
    # {name: password hash}; their roles in user_roles (admin when missing).
    # users, user_roles, tokens and links join the main config's (fixed)
    # and the users file's (edited from the web interface): see set_web_access
    users: dict[str, str]
    backup_dir: Path
    jobs_dir: Path
    # the servers live in their own file, which the web interface rewrites
    servers_file: Path
    servers: dict[str, Server]
    # MCP clients: {name: "sha256$<hex>"} (omniacli new-token)
    tokens: dict[str, str] = field(default_factory=dict)
    user_roles: dict[str, str] = field(default_factory=dict)
    # linked instances: {name: LinkAccess} (omniacli new-link-token)
    links: dict[str, LinkAccess] = field(default_factory=dict)
    users_file: Path | None = None
    # the other OmniaCLI instances this one operates (Links page)
    links_file: Path | None = None
    link_targets: dict[str, LinkTarget] = field(default_factory=dict)
    # how the Dashboard's lists group their items (listgroups.py): {list: groups}
    groups_file: Path | None = None
    groups: dict[str, list] = field(default_factory=dict)
    # the scheduled actions (schedules.py) and the scripts they may run
    schedules_file: Path | None = None
    scripts_dir: Path | None = None
    # the options of the system (Options page)
    options_file: Path | None = None
    options: Options = field(default_factory=lambda: Options())
    # the pullers this instance relays (pullers.yaml, Pullers page)
    pullers_file: Path | None = None
    pullers: dict[str, PullerDef] = field(default_factory=dict)
    # the instances of this one published on a puller of a linked relay
    # (publications.yaml, readable by its owner only: it holds the keys)
    publications_file: Path | None = None
    publications: dict[str, Publication] = field(default_factory=dict)
    # the pullers of linked relays this one is a consumer of (connections.yaml,
    # readable by its owner only: it holds the keys)
    connections_file: Path | None = None
    connections: dict[str, PullerConnection] = field(default_factory=dict)
    # the plugins published for download (main configuration, plugins:)
    plugin_source: PluginSource = field(default_factory=lambda: PluginSource())
    # the customers' workspaces (workspaces.yaml, Workspaces page), and the
    # folders they may live in (main configuration, workspaces.storage)
    workspaces_file: Path | None = None
    # the git credentials of servers and workspaces (gitcredentials.py)
    credentials_file: Path | None = None
    # the remote destinations of the backups (destinations.py, mode 600:
    # it holds passwords, tokens and the keys), read when used
    destinations_file: Path | None = None
    workspaces: dict[str, Workspace] = field(default_factory=dict)
    # {name: folder}; storage_error: why there are none usable (a list of before)
    storage_roots: dict[str, str] = field(default_factory=dict)
    storage_error: str = ""
    machine: str = MACHINE_SERVER
    token_roles: dict[str, str] = field(default_factory=dict)
    token_workspaces: dict[str, str] = field(default_factory=dict)
    fixed: Access = field(default_factory=Access)
    web: Access = field(default_factory=Access)

    def storage_problem(self, ws: Workspace) -> str | None:
        """Why ws cannot use its storage, None when it can (or has none yet)."""
        if ws.storage and self.storage_error:
            return f"{self.storage_error} (in the main configuration; the machine's admins set it)"
        if ws.storage and ws.storage not in self.storage_roots:
            return (f"the storage {ws.storage} of workspace {ws.name} is not one of workspaces.storage of the "
                    f"main configuration ({', '.join(self.storage_roots) or 'none'}): the machine's admins pick "
                    f"another on Settings › Workspaces, or list it again")
        return None

    def set_web_access(self, web: Access) -> None:
        """Make web current: the fixed credentials win a name clash."""
        self.web = web
        self.users = {**web.users, **self.fixed.users}
        self.user_roles = {**web.roles, **self.fixed.roles}
        self.tokens = {**web.tokens, **self.fixed.tokens}
        self.links = {**web.links, **self.fixed.links}
        self.token_roles = {**web.token_roles, **self.fixed.token_roles}
        self.token_workspaces = {**{n: w for n, w in web.token_workspaces.items() if n not in self.fixed.tokens},
                                 **self.fixed.token_workspaces}

    def user_role(self, name: str) -> str:
        return self.user_roles.get(name, "admin")

    def token_role(self, name: str) -> str:
        return self.token_roles.get(name, TOKEN_DEFAULT_ROLE)

    def token_workspace(self, name: str) -> str:
        """The workspace an MCP token is bound to; empty: the machine's token."""
        return self.token_workspaces.get(name, "")

    @property
    def logs_dir(self) -> Path:
        return logs_dir_of(self.jobs_dir)

    def active_servers(self) -> list[Server]:
        return [s for s in self.servers.values() if s.active]

    def instance(self, server: str, role: str, drafts: bool = False) -> Instance | None:
        """An instance of an active server, or also of a draft (drafts: for an
        admin's backup and its download; a draft saved incomplete has none)."""
        srv = self.servers.get(server)
        return srv.instances.get(role) if srv and (srv.active or drafts) else None

    def all_instances(self) -> list[Instance]:
        """The instances of the active servers."""
        return [i for s in self.active_servers() for i in s.instances.values()]

    def workspace_of(self, server: str) -> str | None:
        """The workspace server is in, None: none."""
        return next((ws.name for ws in self.workspaces.values() if server in ws.servers), None)

    def memberships(self, user: str) -> tuple[tuple[str, str], ...]:
        """((workspace, role), ...) of user, by workspace name."""
        return tuple((ws.name, ws.members[user]) for ws in sorted(self.workspaces.values(), key=lambda w: w.name)
                     if user in ws.members)


def _require(data: dict, key: str, where: str):
    if key not in data or data[key] in (None, ""):
        raise ConfigError(f"{where}: missing '{key}'")
    return data[key]


def _parse_time(value, where: str) -> datetime.time:
    try:
        return datetime.datetime.strptime(str(value), "%H:%M").time()
    except ValueError as exc:
        raise ConfigError(f"{where}: invalid time '{value}', expected HH:MM") from exc


def _parse_auto_update(data: dict | None, where: str) -> AutoUpdate:
    data = data or {}
    # the web form sends an empty field as "": the same as no value, the default
    interval = data.get("interval_minutes")
    try:
        interval = 10 if interval in (None, "") else int(interval)
    except (TypeError, ValueError):
        raise ConfigError(f"{where}: interval_minutes {interval!r} is not a number") from None
    auto = AutoUpdate(enabled=bool(data.get("enabled", False)), interval_minutes=interval)
    if auto.interval_minutes < 1:
        raise ConfigError(f"{where}: interval_minutes must be >= 1")
    auto.window_start, auto.window_end = _parse_window(data.get("window"), where)
    return auto


def _parse_window(window, where: str) -> tuple[datetime.time | None, datetime.time | None]:
    """'HH:MM-HH:MM' (it may cross midnight); empty: none."""
    if not window:
        return None, None
    try:
        start, end = str(window).split("-")
    except ValueError as exc:
        raise ConfigError(f"{where}: window must be 'HH:MM-HH:MM'") from exc
    return _parse_time(start.strip(), where), _parse_time(end.strip(), where)


def _parse_instance(server: str, role: str, data: dict, logs_dir: Path | None, plugin) -> Instance:
    where = f"servers.{server}.{role}"
    repositories = []
    for idx, repo in enumerate(_require(data, "repositories", where)):
        rwhere = f"{where}.repositories[{idx}]"
        repositories.append(
            Repository(
                name=str(_require(repo, "name", rwhere)),
                path=Path(_require(repo, "path", rwhere)),
                branch=str(_require(repo, "branch", rwhere)),
                remote=str(repo.get("remote", "origin")),
            )
        )
    names = [r.name for r in repositories]
    if len(names) != len(set(names)):
        raise ConfigError(f"{where}: repository names must be unique")
    restart = _require(data, "restart", where)
    restart_type = _require(restart, "type", f"{where}.restart")
    if restart_type not in RESTART_TYPES:
        raise ConfigError(
            f"{where}.restart: type must be one of {', '.join(RESTART_TYPES)}"
        )
    if restart_type == "compose" and not plugin.has_compose:
        raise ConfigError(f"{where}.restart: {plugin.label} does not run in Docker Compose "
                          "(type compose)")
    if restart_type == "process":
        if not plugin.has_process:
            raise ConfigError(f"{where}.restart: {plugin.label} cannot be started by OmniaCLI "
                              "(type process)")
        if logs_dir is None:
            raise ConfigError(f"{where}.restart: type process needs the logs folder")
        # the output of the process OmniaCLI starts
        log_file = logs_dir / f"{server}_{role}.log"
        service = ""
    else:
        # compose: the log is docker compose's, read through the helper
        log_file = None if restart_type == "compose" else Path(_require(data, "log_file", where))
        service = str(_require(restart, "service", f"{where}.restart"))
        if restart_type == "compose" and not service.startswith("/"):
            raise ConfigError(f"{where}.restart.service: {service!r} is not the absolute path "
                              "of the compose project's folder")
    run_as = str(data.get("run_as") or "").strip() or None
    if run_as is not None:
        if not USER_NAME.fullmatch(run_as):
            raise ConfigError(f"{where}.run_as: {run_as!r} is not a user name")
        if restart_type == "process":
            # OmniaCLI stops its own process with a signal, which it may not
            # send to another user's process
            raise ConfigError(f"{where}.run_as: not available with restart type process")
    from_link = parse_link_source(data.get("from_link"), f"{where}.from_link")
    domain = _parse_domain(data.get("domain"), f"{where}.domain")
    claude_dir = str(data.get("claude_dir") or "").strip() or None
    if claude_dir is not None and not claude_dir.startswith("/"):
        raise ConfigError(f"{where}.claude_dir: {claude_dir!r} is not an absolute path")
    options = plugin.parse_options(data, where, restart_type)
    package = _parse_package(data.get("package"), f"{where}.package", server, role, logs_dir)
    return Instance(
        server=server,
        role=role,
        repositories=repositories,
        log_file=log_file,
        restart=Restart(
            type=restart_type,
            service=service,
            sudo=bool(restart.get("sudo", True)),
        ),
        plugin=plugin,
        options=options,
        auto_update=_parse_auto_update(data.get("auto_update"), f"{where}.auto_update"),
        backup_before_update=bool(data.get("backup_before_update", False)),
        run_as=run_as,
        from_link=from_link,
        domain=domain,
        claude_dir=Path(claude_dir) if claude_dir else None,
        package=package,
    )


# a Debian package name (Debian Policy 5.6.1)
PACKAGE_NAME = re.compile(r"[a-z0-9][a-z0-9+.-]+")


def _parse_package(data, where: str, server: str, role: str, logs_dir: Path | None) -> Package | None:
    """{url, name, public_key}; empty or no url: none. The url is https (http
    only to this computer); the downloads go next to the logs folder."""
    from .packagesign import SigningError, parse_public_key

    if not data:
        return None
    if not isinstance(data, dict):
        raise ConfigError(f"{where}: a mapping of url, name and public_key")
    url = str(data.get("url") or "").strip()
    if not url:
        return None
    parts = urlsplit(url)
    local = parts.hostname in ("localhost", "127.0.0.1", "::1")
    if (parts.scheme not in ("https", "http") or (parts.scheme == "http" and not local)
            or not parts.hostname or parts.query or parts.fragment or parts.username
            or not parts.path.endswith(".deb")):
        raise ConfigError(f"{where}.url: {url!r} is not an https:// URL of a .deb")
    name = str(data.get("name") or "").strip()
    if not PACKAGE_NAME.fullmatch(name):
        raise ConfigError(f"{where}.name: {name!r} is not a Debian package name (api-server)")
    public_key = str(data.get("public_key") or "").strip()
    try:
        parse_public_key(public_key)
    except SigningError as exc:
        raise ConfigError(f"{where}.public_key: {exc}") from None
    if logs_dir is None:
        raise ConfigError(f"{where}: a package needs OmniaCLI's data folder")
    return Package(url=url, name=name, public_key=public_key, folder=logs_dir.parent / "packages",
                   server=server, role=role)


def _parse_domain(value, where: str) -> str | None:
    """A domain name (erp.example.com[:port], opened with https) or a whole
    http(s) URL, as the base URL without the trailing /; empty: none."""
    value = str(value or "").strip().rstrip("/")
    if not value:
        return None
    url = value if "://" in value else f"https://{value}"
    parts = urlsplit(url)
    try:
        parts.port
    except ValueError:
        raise ConfigError(f"{where}: {value!r} has no valid port") from None
    if (parts.scheme not in ("http", "https") or not parts.hostname or parts.query or parts.fragment
            or parts.username or parts.password or any(c.isspace() for c in url)):
        raise ConfigError(f"{where}: {value!r} is not a domain name (erp.example.com) "
                          "or an http(s)://host[:port][/path] URL")
    return url


def parse_link_source(data, where: str) -> LinkSource | None:
    """{link, server, role}, or {puller} (a puller connection); empty or no
    link: none."""
    if not data:
        return None
    if not isinstance(data, dict):
        raise ConfigError(f"{where}: a mapping of link, server and role, or of puller")
    connection = str(data.get("puller") or "").strip()
    if connection:
        if not SERVER_NAME.fullmatch(connection):
            raise ConfigError(f"{where}.puller: {connection!r} is not a name of letters, digits and . _ -")
        return LinkSource(link=connection, server="", role="", kind="puller")
    link = str(data.get("link") or "").strip()
    if not link:
        return None
    server = str(data.get("server") or "").strip()
    role = str(data.get("role") or "production").strip()
    for key, value in (("link", link), ("server", server)):
        if not SERVER_NAME.fullmatch(value):
            raise ConfigError(f"{where}.{key}: {value!r} is not a name of letters, digits and . _ -")
    if role not in ROLES:
        raise ConfigError(f"{where}.role: one of {', '.join(ROLES)}")
    return LinkSource(link=link, server=server, role=role)


def parse_servers(data: dict, logs_dir: Path | None = None) -> dict[str, Server]:
    """Parse the mapping {server name: {production: ..., test: ...}}.

    logs_dir: where the instances of restart type process log."""
    servers = {}
    for name, srv in (data or {}).items():
        name = str(name)
        if not SERVER_NAME.fullmatch(name):
            raise ConfigError(
                f"servers.{name}: the name may contain only letters, digits, '.', '_' and '-'"
            )
        if not isinstance(srv, dict):
            raise ConfigError(f"servers.{name}: must be a mapping")
        status = str(srv.get("status") or "active")
        if status not in SERVER_STATUSES:
            raise ConfigError(f"servers.{name}.status must be one of {', '.join(SERVER_STATUSES)}")
        from .plugins import DEFAULT_TYPE, registry  # the plugins import this module

        kind = str(srv.get("type") or DEFAULT_TYPE)
        written = {role: srv[role] for role in ROLES if srv.get(role)}
        plugin = registry().get(kind)
        if plugin is None:
            # kept as written, without actions, until its plugin is there
            servers[name] = Server(name=name, instances={}, status=status, data=written,
                                   error=registry().why_missing(kind), type=kind, missing_plugin=True)
            continue
        try:
            servers[name] = _parse_server(name, srv, status, logs_dir, plugin)
        except (ConfigError, TypeError, ValueError, AttributeError) as exc:
            if status != "draft":
                raise
            servers[name] = Server(name=name, instances={}, status=status, data=written,
                                   error=str(exc), type=kind)
    return servers


def _parse_server(name: str, srv: dict, status: str, logs_dir: Path | None, plugin) -> Server:
    instances = {
        role: _parse_instance(name, role, srv[role], logs_dir, plugin) for role in ROLES if srv.get(role)
    }
    if not instances:
        raise ConfigError(f"servers.{name}: define 'production' and/or 'test'")
    prod, test = instances.get("production"), instances.get("test")
    if prod and test:
        # merge to production pairs repositories by name
        missing = [r.name for r in test.repositories if not prod.repository(r.name)]
        if missing:
            raise ConfigError(
                f"servers.{name}: test repositories without a production "
                f"repository of the same name: {', '.join(missing)}"
            )
    return Server(name=name, instances=instances, status=status, type=plugin.name)


def instance_to_dict(instance: Instance) -> dict:
    auto = instance.auto_update
    data = {
        **instance.plugin.options_to_dict(instance.options),
        "log_file": str(instance.log_file),
        "restart": {
            "type": instance.restart.type,
            "service": instance.restart.service,
            "sudo": instance.restart.sudo,
        },
        "auto_update": {"enabled": auto.enabled, "interval_minutes": auto.interval_minutes},
        "repositories": [
            {"name": r.name, "path": str(r.path), "remote": r.remote, "branch": r.branch}
            for r in instance.repositories
        ],
    }
    if auto.window_start is not None and auto.window_end is not None:
        data["auto_update"]["window"] = auto.window_label()
    if instance.backup_before_update:
        data["backup_before_update"] = True
    if instance.run_as is not None:
        data["run_as"] = instance.run_as
    if instance.domain is not None:
        data["domain"] = instance.domain
    if instance.claude_dir is not None:
        data["claude_dir"] = str(instance.claude_dir)
    if instance.from_link is not None:
        data["from_link"] = instance.from_link.to_dict()
    if instance.package is not None:
        pkg = instance.package
        data["package"] = {"url": pkg.url, "name": pkg.name, "public_key": pkg.public_key}
    if instance.restart.type == "process":
        # both follow from the type: the log is OmniaCLI's, there is no service
        del data["log_file"], data["restart"]["service"], data["restart"]["sudo"]
    if instance.log_file is None:
        del data["log_file"]
    return data


def server_to_dict(server: Server) -> dict:
    data = {"status": server.status, "type": server.type}
    if server.data is not None:
        data.update(server.data)
    data.update({role: instance_to_dict(i) for role, i in server.instances.items()})
    return data


def load_servers(path: Path, logs_dir: Path | None = None) -> dict[str, Server]:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return parse_servers(data.get("servers") or {}, logs_dir)


def save_servers(path: Path, servers: dict[str, Server]) -> None:
    """Write the servers file atomically, keeping the previous one as .bak."""
    text = yaml.safe_dump(
        {"servers": {name: server_to_dict(srv) for name, srv in servers.items()}},
        sort_keys=False,
        allow_unicode=True,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    os.replace(tmp, path)


_TOKEN_HASH = re.compile(r"sha256\$[0-9a-f]{64}")


def _access_role(where: str, value, roles: tuple[str, ...] = ACCESS_ROLES) -> str:
    role = str(value or "")
    if role not in roles:
        raise ConfigError(f"{where}: role must be one of {', '.join(roles)}, not {role!r}")
    return role


def parse_auth(auth: dict, where: str = "auth", need_user: bool = True) -> Access:
    """users (a hash, admin, or {password, role}), tokens and links ({token,
    role}) of the main config's auth section or of the users file."""
    users, roles = {}, {}
    for name, value in (auth.get("users") or {}).items():
        name = str(name)
        if isinstance(value, dict):
            users[name] = str(value.get("password") or "")
            roles[name] = _access_role(f"{where}.users.{name}", value.get("role"), USER_ROLES)
        else:
            # the format before roles: such a user is an admin
            users[name], roles[name] = str(value), "admin"
        if not users[name]:
            raise ConfigError(f"{where}.users.{name}: password hash missing (omniacli hash-password)")
    if need_user and not users:
        raise ConfigError(f"{where}.users: at least one user is required")
    tokens, token_roles, token_workspaces = {}, {}, {}
    for name, value in (auth.get("tokens") or {}).items():
        name = str(name)
        if isinstance(value, dict):
            tokens[name] = str(value.get("token") or "")
            token_roles[name] = _access_role(f"{where}.tokens.{name}", value.get("role") or TOKEN_DEFAULT_ROLE)
            workspace = str(value.get("workspace") or "").strip()
            if workspace:
                if not WORKSPACE_NAME.fullmatch(workspace):
                    raise ConfigError(f"{where}.tokens.{name}.workspace: {workspace!r} is not a workspace name")
                token_workspaces[name] = workspace
        else:
            # the format before roles: what such a token could do then
            tokens[name], token_roles[name] = str(value), TOKEN_DEFAULT_ROLE
        if not _TOKEN_HASH.fullmatch(tokens[name]):
            raise ConfigError(f"{where}.tokens: {name} is not a token hash (omniacli new-token)")
    links = {}
    for name, value in (auth.get("links") or {}).items():
        name, value = str(name), value if isinstance(value, dict) else {}
        token = str(value.get("token") or "")
        if not _TOKEN_HASH.fullmatch(token):
            raise ConfigError(f"{where}.links.{name}: token is not a token hash (omniacli new-link-token)")
        links[name] = LinkAccess(token, _access_role(f"{where}.links.{name}", value.get("role")))
    return Access(users, roles, tokens, links, token_roles, token_workspaces)


def load_access(path: Path) -> Access:
    """The users file; none yet is no credentials."""
    if not path.exists():
        return Access()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    return parse_auth(data, where=path.name, need_user=False)


def save_access(path: Path, access: Access) -> None:
    """Write the users file atomically, readable by its owner only."""
    data = {
        "users": {n: {"password": h, "role": access.roles.get(n, "admin")} for n, h in access.users.items()},
        "links": {n: {"token": link.token, "role": link.role} for n, link in access.links.items()},
        "tokens": {n: {"token": h, "role": access.token_roles.get(n, TOKEN_DEFAULT_ROLE),
                       **({"workspace": access.token_workspaces[n]} if n in access.token_workspaces else {})}
                   for n, h in access.tokens.items()},
    }
    _write_private(path, yaml.safe_dump(data, sort_keys=False, allow_unicode=True))


def _write_private(path: Path, text: str) -> None:
    """Write path atomically, readable by its owner only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


# an invitation: a link used once, where the person sets its own password
INVITATION_HOURS = 72
# an invitation used or expired is kept this long, so that its link says so
INVITATION_KEPT_DAYS = 30


@dataclass
class Invitation:
    """An invitation of a user made without a password: the hash of its
    one-time link's token, when it ends, who made it and when; used: when
    the person set its password (0: not yet)."""

    name: str
    token: str  # sha256$<hex> of the link's token
    created: float
    expires: float
    by: str = ""
    # made from a workspace's page: its admins may send it again or revoke it
    workspace: str = ""
    used: float = 0.0

    def state(self, now: float) -> str:
        """pending, expired or used."""
        if self.used:
            return "used"
        return "expired" if now >= self.expires else "pending"

    def to_dict(self) -> dict:
        data = {"name": self.name, "token": self.token, "created": self.created, "expires": self.expires,
                "by": self.by, "workspace": self.workspace, "used": self.used}
        return {k: v for k, v in data.items() if v or k in ("name", "token")}


def invitations_file_of(users_file: Path) -> Path:
    """The invitations live beside the users file, in a file of their own
    (an OmniaCLI without invitations never reads it)."""
    return users_file.with_name("invitations.yaml")


def load_invitations(path: Path) -> list[Invitation]:
    """The invitations of the file; none yet is none, and an entry that is
    not one is left out (the file is OmniaCLI's own)."""
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    found = []
    for item in data.get("invitations") or []:
        if not isinstance(item, dict) or not _TOKEN_HASH.fullmatch(str(item.get("token") or "")):
            continue
        try:
            found.append(Invitation(str(item["name"]), str(item["token"]), float(item.get("created") or 0),
                                    float(item.get("expires") or 0), str(item.get("by") or ""),
                                    str(item.get("workspace") or ""), float(item.get("used") or 0)))
        except (KeyError, TypeError, ValueError):
            continue
    return found


def save_invitations(path: Path, invitations: list[Invitation], now: float) -> list[Invitation]:
    """Write the invitations, without those used or expired more than
    INVITATION_KEPT_DAYS ago: the ones kept."""
    keep = now - INVITATION_KEPT_DAYS * 86400
    kept = [i for i in invitations if (i.used or i.expires) >= keep]
    _write_private(path, yaml.safe_dump({"invitations": [i.to_dict() for i in kept]}, sort_keys=False,
                                        allow_unicode=True))
    return kept


# an SSH host key as a known_hosts line has it, without the host: type and key
SSH_HOST_KEY = re.compile(r"(ssh-ed25519|ecdsa-sha2-nistp(256|384|521)|ssh-rsa) [A-Za-z0-9+/]+={0,2}")
SSH_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}|\[?[0-9A-Fa-f:]+\]?")
SSH_USER = re.compile(r"[a-z_][a-z0-9_.-]{0,31}")


@dataclass
class SshRoute:
    """The SSH server a link goes through: OmniaCLI keeps ssh -L open to it
    (sshlinks.py) and reaches the link's URL from there."""

    host: str
    user: str
    host_key: str  # the server's key, "<type> <base64>": no other is accepted
    port: int = 22

    def to_dict(self) -> dict:
        return {"host": self.host, "port": self.port, "user": self.user, "host_key": self.host_key}


@dataclass
class LinkTarget:
    name: str
    # the other instance's base URL, https://…; with ssh, http:// as the SSH
    # server reaches it (http://127.0.0.1:8090: the tunnel carries it encrypted)
    url: str
    token: str  # in clear: it is sent with every request
    ca_file: str = ""  # a CA bundle for a certificate of the customer's own CA
    ssh: SshRoute | None = None
    # with ssh: the port of this machine its tunnel listens on (sshlinks.py)
    local_port: int = field(default=0, compare=False, repr=False)

    @property
    def address(self) -> str:
        """Where the requests go: the URL, or with ssh the tunnel's port here."""
        if self.ssh is None:
            return self.url
        path = urlsplit(self.url).path.rstrip("/")
        return f"http://127.0.0.1:{self.local_port or 9}{path}"

    @property
    def remote(self) -> tuple[str, int]:
        """With ssh: host and port the SSH server forwards to (permitopen)."""
        parts = urlsplit(self.url)
        return parts.hostname or "", parts.port or 80

    def to_dict(self) -> dict:
        data = {"url": self.url, "token": self.token}
        if self.ca_file:
            data["ca_file"] = self.ca_file
        if self.ssh is not None:
            data["ssh"] = self.ssh.to_dict()
        return data


def check_ssh_address(where: str, host, port) -> tuple[str, int]:
    """An SSH server's host and port (22 when empty): a name or an address,
    never something ssh would take as an option."""
    host = str(host or "").strip()
    if not SSH_HOST.fullmatch(host):
        raise ConfigError(f"{where}.ssh.host: {host!r} is not a host name or an IP address")
    port = port or 22
    if isinstance(port, bool) or not str(port).isdigit() or not 1 <= int(port) <= 65535:
        raise ConfigError(f"{where}.ssh.port: {port!r} is not a port")
    return host, int(port)


def parse_ssh_route(where: str, data) -> SshRoute | None:
    """A link's ssh: host, port, user and the host key; empty is no SSH."""
    if not data:
        return None
    if not isinstance(data, dict):
        raise ConfigError(f"{where}.ssh must be a mapping")
    host, port = check_ssh_address(where, data.get("host"), data.get("port"))
    user = str(data.get("user") or "").strip()
    if not SSH_USER.fullmatch(user):
        raise ConfigError(f"{where}.ssh.user: {user!r} is not a user name")
    host_key = " ".join(str(data.get("host_key") or "").split()[:2])
    if not SSH_HOST_KEY.fullmatch(host_key):
        raise ConfigError(f"{where}.ssh.host_key: the server's key is missing (Fetch host key)")
    try:
        # the key names its own type first
        raw = base64.b64decode(host_key.split()[1], validate=True)
        ok = raw[4:4 + int.from_bytes(raw[:4], "big")].decode() == host_key.split()[0]
        # an ed25519 key is its type and 32 bytes
        ok = ok and (host_key.split()[0] != "ssh-ed25519" or len(raw) == 51)
    except (ValueError, UnicodeDecodeError):
        ok = False
    if not ok:
        raise ConfigError(f"{where}.ssh.host_key: not a key of type {host_key.split()[0]}")
    return SshRoute(host.strip("[]"), user, host_key, port)


def parse_link(name: str, data: dict) -> LinkTarget:
    if not SERVER_NAME.fullmatch(name):
        raise ConfigError(f"links.{name}: a name is letters, digits and . _ - (it is part of URLs)")
    url = str(data.get("url") or "").strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.query or parts.fragment:
        raise ConfigError(f"links.{name}: url must be http(s)://host[:port][/path]")
    token = str(data.get("token") or "").strip()
    if not token:
        raise ConfigError(f"links.{name}: token missing (the other instance creates it on its Users page)")
    ca_file = str(data.get("ca_file") or "").strip()
    if ca_file and not Path(ca_file).is_file():
        raise ConfigError(f"links.{name}: ca_file {ca_file} is not a file")
    ssh = parse_ssh_route(f"links.{name}", data.get("ssh"))
    if ssh is not None and parts.scheme != "http":
        # the tunnel ends on this machine: a certificate would not match it
        raise ConfigError(f"links.{name}: through SSH the url is http://, as the SSH server reaches OmniaCLI "
                          "(http://127.0.0.1:8090); SSH encrypts it")
    return LinkTarget(name, url, token, ca_file, ssh)


def load_links(path: Path) -> dict[str, LinkTarget]:
    """The links file; none yet is no links."""
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    links = data.get("links") or {}
    if not isinstance(links, dict):
        raise ConfigError(f"{path}: links must be a mapping")
    return {str(name): parse_link(str(name), value or {}) for name, value in links.items()}


def save_links(path: Path, links: dict[str, LinkTarget]) -> None:
    """Write the links file atomically, readable by its owner only: it holds the tokens."""
    text = yaml.safe_dump({"links": {n: link.to_dict() for n, link in links.items()}},
                          sort_keys=False, allow_unicode=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def load_groups(path: Path) -> dict[str, list]:
    """The groups of the Dashboard's lists (listgroups.py); none yet is none."""
    try:
        return listgroups.load(path)
    except listgroups.GroupsError as exc:
        raise ConfigError(str(exc)) from exc


# a workspace's name is part of URLs, of folders and of systemd unit names
WORKSPACE_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?")
# the domain production answers on (test: test.<domain>)
DOMAIN_NAME = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
# the users of a workspace: names as the Users page makes them
MEMBER_NAME = re.compile(r"[A-Za-z0-9._@-]{1,64}")
# what a workspace may use; a limit not set is no limit
WORKSPACE_LIMITS = ("disk_gb", "cpus", "memory_gb", "instances")
# a subdomain of a workspace's domain (and of test.<domain>): one label
SUBDOMAIN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
# the label of the test names, never an instance's or a subdomain's
TEST_LABEL = "test"
# where the certificate of a workspace's names comes from
CERTIFICATES = ("letsencrypt", "own")
# the other names of one environment (UX-008 §6.1), aliases and redirects together
ADDRESSES_MAX = 20
# the first label of a staging's names (web_names): never another name's
STAGING_LABEL = re.compile(r"stg-\d{8}(?:-\d+)?")


@dataclass
class Workspace:
    """A customer's closed part of the machine: its servers, and the users
    who see them, each with its role there. Set by the users of the machine
    (classes of users: README, Workspaces)."""

    name: str
    label: str = ""
    domain: str = ""
    # one of the storage roots of the main configuration (workspaces.storage)
    storage: str = ""
    limits: dict[str, float] = field(default_factory=dict)
    servers: list[str] = field(default_factory=list)
    members: dict[str, str] = field(default_factory=dict)  # {user name: role}
    # the names (README, Domains of a workspace): the instance on <domain>
    # and test.<domain>, the subdomains {<sub>: instance}, the certificate
    main: str = ""
    subdomains: dict[str, str] = field(default_factory=dict)
    certificate: str = "letsencrypt"
    # the bounds of the editable Odoo parameters for this workspace, set by
    # the machine's admins over the template's: {name: [min, max]}
    parameter_bounds: dict[str, list[int]] = field(default_factory=dict)
    # who is told when one of its instances goes down (monitor.py), set by its admins
    notify: list[str] = field(default_factory=list)
    # the other names of an environment (UX-008 §6.1, its admins): names it
    # answers on too, {instance: {role: [name]}}, and names that send their
    # visitors to one of its names with a 301, {instance: {role: {name: to}}}
    aliases: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    redirects: dict[str, dict[str, dict[str, str]]] = field(default_factory=dict)

    def instances(self) -> list[str]:
        """The instances of the workspace: its servers <name>-<instance>."""
        prefix = f"{self.name}-"
        return [s[len(prefix):] for s in self.servers if s.startswith(prefix) and len(s) > len(prefix)]

    def to_dict(self) -> dict:
        data: dict[str, Any] = {"label": self.label}
        if self.domain:
            data["domain"] = self.domain
        if self.storage:
            data["storage"] = self.storage
        if self.limits:
            data["limits"] = dict(self.limits)
        data["servers"] = list(self.servers)
        data["members"] = dict(self.members)
        if self.main:
            data["main"] = self.main
        if self.subdomains:
            data["subdomains"] = dict(self.subdomains)
        if self.certificate != "letsencrypt":
            data["certificate"] = self.certificate
        if self.parameter_bounds:
            data["parameter_bounds"] = {k: list(v) for k, v in self.parameter_bounds.items()}
        if self.notify:
            data["notify"] = list(self.notify)
        if self.aliases:
            data["aliases"] = {i: {r: list(n) for r, n in roles.items()} for i, roles in self.aliases.items()}
        if self.redirects:
            data["redirects"] = {i: {r: dict(n) for r, n in roles.items()} for i, roles in self.redirects.items()}
        return data

    def extra_names(self) -> list[tuple[str, str, str]]:
        """(name, instance, role) of every alias and redirect of the workspace."""
        found = [(n, i, r) for i, roles in self.aliases.items() for r, names in roles.items() for n in names]
        return found + [(n, i, r) for i, roles in self.redirects.items() for r, names in roles.items() for n in names]


def environment_label(instance: str, role: str) -> str:
    """An environment as a message names it: main, main (test)."""
    return instance if role == "production" else f"{instance} (test)"


def _environments(where: str, data, instances: list[str]) -> dict[str, dict]:
    """{instance: {role: value}} of aliases or redirects, the instance and the
    role checked; an empty one left out."""
    if data in (None, ""):
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{where}: a mapping {{instance: {{role: names}}}}")
    found = {}
    for instance, roles in data.items():
        instance = str(instance)
        if instance not in instances:
            raise ConfigError(f"{where}: {instance!r} is not an instance of the workspace")
        if not isinstance(roles, dict) or any(str(r) not in ROLES for r in roles):
            raise ConfigError(f"{where}.{instance}: a mapping {{production or test: names}}")
        kept = {str(r): v for r, v in roles.items() if v}
        if kept:
            found[instance] = kept
    return found


def address_name(where: str, value) -> str:
    """A name an environment may answer on besides the workspace's: a host
    name (no wildcard, no address), lower case, not a staging's."""
    name = str(value or "").strip().lower().rstrip(".")
    if not DOMAIN_NAME.fullmatch(name):
        raise ConfigError(f"{where}: {name!r} is not a host name (as www.customer.com; no * and no address)")
    if STAGING_LABEL.fullmatch(name.split(".", 1)[0]):
        raise ConfigError(f"{where}: {name} starts as the names of the stagings (stg-<date>): choose another")
    return name


def parse_addresses(where: str, aliases, redirects, instances: list[str]) -> tuple[dict, dict]:
    """(aliases, redirects) of a workspace's data, each name a host name
    (address_name), at most ADDRESSES_MAX for an environment. The target of a
    redirect is a host name too; that it is one of its environment's names is
    checked where those are known (check_addresses, the root helper)."""
    found_aliases: dict[str, dict[str, list[str]]] = {}
    for instance, roles in _environments(f"{where}.aliases", aliases, instances).items():
        for role, names in roles.items():
            if not isinstance(names, list):
                raise ConfigError(f"{where}.aliases.{instance}.{role}: a list of names")
            found_aliases.setdefault(instance, {})[role] = list(dict.fromkeys(
                address_name(f"{where}.aliases.{instance}.{role}", n) for n in names))
    found_redirects: dict[str, dict[str, dict[str, str]]] = {}
    for instance, roles in _environments(f"{where}.redirects", redirects, instances).items():
        for role, pairs in roles.items():
            if not isinstance(pairs, dict):
                raise ConfigError(f"{where}.redirects.{instance}.{role}: a mapping {{name: the name it goes to}}")
            here = f"{where}.redirects.{instance}.{role}"
            found_redirects.setdefault(instance, {})[role] = {
                address_name(here, k): address_name(f"{here}.{k}", v) for k, v in pairs.items()}
    for instance in set(found_aliases) | set(found_redirects):
        for role in ROLES:
            own = found_aliases.get(instance, {}).get(role, [])
            sent = found_redirects.get(instance, {}).get(role, {})
            if len(own) + len(sent) > ADDRESSES_MAX:
                raise ConfigError(f"{where}: {environment_label(instance, role)} has more than {ADDRESSES_MAX} other names")
            both = sorted(set(own) & set(sent))
            if both:
                raise ConfigError(f"{where}: {both[0]} is a name of {environment_label(instance, role)} and a redirect at once")
            loop = sorted(n for n, to in sent.items() if to in sent)
            if loop:
                raise ConfigError(f"{where}: the redirect {loop[0]} goes to a redirect, not to an address")
    return found_aliases, found_redirects


def parse_workspace(name: str, data: dict, storage_roots=()) -> Workspace:
    where = f"workspaces.{name}"
    if not WORKSPACE_NAME.fullmatch(name):
        raise ConfigError(f"{where}: a name is 1-32 lower case letters, digits and -, "
                          "not starting or ending with -")
    data = data if isinstance(data, dict) else {}
    label = str(data.get("label") or "").strip()
    if len(label) > 100:
        raise ConfigError(f"{where}.label: at most 100 characters")
    domain = str(data.get("domain") or "").strip().lower()
    if domain and not DOMAIN_NAME.fullmatch(domain):
        raise ConfigError(f"{where}.domain: {domain!r} is not a domain name")
    # kept even when it is no storage root any more (taken out of the main
    # configuration): the workspace is then unusable until an admin picks
    # another (Settings.storage_problem), OmniaCLI still starts
    storage = str(data.get("storage") or "").strip()
    limits = {}
    for key, value in (data.get("limits") or {}).items():
        if key not in WORKSPACE_LIMITS:
            raise ConfigError(f"{where}.limits: {key} is not one of {', '.join(WORKSPACE_LIMITS)}")
        if value in (None, ""):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ConfigError(f"{where}.limits.{key}: {value!r} is not a number") from None
        if number <= 0 or (key == "instances" and not number.is_integer()):
            raise ConfigError(f"{where}.limits.{key}: {value!r} is not a positive "
                              + ("whole number" if key == "instances" else "number"))
        limits[key] = int(number) if number.is_integer() else number
    servers = [str(s).strip() for s in data.get("servers") or [] if str(s).strip()]
    bad = [s for s in servers if not SERVER_NAME.fullmatch(s)]
    if bad:
        raise ConfigError(f"{where}.servers: {', '.join(bad)} is not a server name")
    members = {}
    for user, role in (data.get("members") or {}).items():
        user = str(user)
        if not MEMBER_NAME.fullmatch(user):
            raise ConfigError(f"{where}.members: {user!r} is not a user name")
        members[user] = _access_role(f"{where}.members.{user}", role)
    ws = Workspace(name, label, domain, storage, limits, list(dict.fromkeys(servers)), members)
    instances = ws.instances()
    main = str(data.get("main") or "").strip()
    if main and main not in instances:
        raise ConfigError(f"{where}.main: {main} is not an instance of the workspace")
    subdomains = {}
    for sub, instance in (data.get("subdomains") or {}).items():
        sub, instance = str(sub).strip().lower(), str(instance or "").strip()
        if not SUBDOMAIN.fullmatch(sub) or sub == TEST_LABEL:
            raise ConfigError(f"{where}.subdomains: {sub!r} is not a subdomain (one label a-z 0-9 -, not {TEST_LABEL})")
        if sub in instances:
            raise ConfigError(f"{where}.subdomains: {sub} is the name of an instance already")
        if instance not in instances:
            raise ConfigError(f"{where}.subdomains.{sub}: {instance!r} is not an instance of the workspace")
        subdomains[sub] = instance
    certificate = str(data.get("certificate") or "letsencrypt")
    if certificate not in CERTIFICATES:
        raise ConfigError(f"{where}.certificate: one of {', '.join(CERTIFICATES)}, not {certificate!r}")
    bounds = {}
    for key, value in (data.get("parameter_bounds") or {}).items():
        try:
            low, high = (int(v) for v in value)
        except (TypeError, ValueError):
            raise ConfigError(f"{where}.parameter_bounds.{key}: [min, max], two whole numbers") from None
        if not re.fullmatch(r"[a-z_]{1,40}", str(key)) or low > high:
            raise ConfigError(f"{where}.parameter_bounds.{key}: a parameter name and min <= max")
        bounds[str(key)] = [low, high]
    aliases, redirects = parse_addresses(where, data.get("aliases"), data.get("redirects"), instances)
    return replace(ws, main=main, subdomains=subdomains, certificate=certificate, parameter_bounds=bounds,
                   notify=parse_notify(data.get("notify"), f"{where}.notify"), aliases=aliases, redirects=redirects)


def parse_workspaces(data, storage_roots=()) -> dict[str, Workspace]:
    if not isinstance(data, dict):
        raise ConfigError("workspaces must be a mapping {name: workspace}")
    found = {str(name): parse_workspace(str(name), value, storage_roots) for name, value in data.items()}
    # a server is in one workspace at most, and so is a domain
    owner: dict[str, str] = {}
    for ws in found.values():
        for srv in ws.servers:
            if srv in owner:
                raise ConfigError(f"server {srv} is in two workspaces: {owner[srv]} and {ws.name}")
            owner[srv] = ws.name
    domains: dict[str, str] = {}
    for ws in found.values():
        if ws.domain and ws.domain in domains:
            raise ConfigError(f"domain {ws.domain} is in two workspaces: {domains[ws.domain]} and {ws.name}")
        domains[ws.domain] = ws.name
    # every name of a workspace is under its domain: no domain under another's
    for domain, ws_name in domains.items():
        for other, other_ws in domains.items():
            if domain and other and domain != other and domain.endswith("." + other):
                raise ConfigError(f"domain {domain} of workspace {ws_name} is under {other} of workspace {other_ws}")
    check_addresses(found)
    return found


def generated_names(ws: Workspace) -> dict[str, tuple[str, str]]:
    """The names web_names gives the workspace's environments, both roles of
    every instance (what it may have): {name: (instance, role)}."""
    if not ws.domain:
        return {}
    try:
        sites = web_names(ws.domain, ws.instances(), ws.main, ws.subdomains)
    except ConfigError:
        return {}
    return {n: (s["instance"], s["role"]) for s in sites for n in s["names"]}


def address_taken(name: str, workspaces: dict, ws_name: str, instance: str, role: str) -> str | None:
    """Why name is not free for the environment (ws_name, instance, role)
    among OmniaCLI's own workspaces, None when it is (the other sites of the
    machine's nginx are the root helper's: workspaceweb.clashes). Another
    workspace is not named: its admins may be another customer."""
    for other in workspaces.values():
        mine = other.name == ws_name
        if not mine and other.domain and (name == other.domain or name.endswith("." + other.domain)):
            return "it is under the domain of another workspace of this machine"
        # one of the workspace's own names, of this environment too: no other name
        owner = generated_names(other).get(name)
        if owner is not None:
            return f"it is a name of {environment_label(*owner)}" if mine else "another workspace of this machine answers on it"
        for extra, i, r in other.extra_names():
            if extra == name and not (mine and (i, r) == (instance, role)):
                return f"it is a name of {environment_label(i, r)}" if mine else "another workspace of this machine answers on it"
    return None


def environment_names(ws: Workspace, instance: str, role: str) -> list[str]:
    """The names (instance, role) of ws answers on: the workspace's, then its aliases."""
    own = [n for n, at in generated_names(ws).items() if at == (instance, role)]
    return own + [n for n in ws.aliases.get(instance, {}).get(role, []) if n not in own]


def addresses_follow(before: Workspace, ws: Workspace, gone: str | None, now: str | None) -> Workspace:
    """The aliases and redirects of ws after its instance gone became now
    (None: deleted): moved with it, or dropped. A redirect to a name of the
    workspace that changed with it goes to the name in the same place (the
    instance renamed: shop.acme.it → store.acme.it); one whose name is gone
    is dropped, as an instance's are when it is deleted."""
    instances = ws.instances()

    def moved(data: dict) -> dict:
        out = {}
        for instance, roles in data.items():
            instance = now if gone and instance == gone else instance
            if instance and instance in instances:
                out[instance] = roles
        return out
    aliases, redirects = moved(ws.aliases), moved(ws.redirects)
    after = replace(ws, aliases=aliases, redirects={})
    kept: dict[str, dict[str, dict[str, str]]] = {}
    for instance, roles in redirects.items():
        was = gone if now and instance == now else instance
        for role, pairs in roles.items():
            old, new = environment_names(before, was, role), environment_names(after, instance, role)
            for name, to in pairs.items():
                if to not in new and to in old and old.index(to) < len(new):
                    to = new[old.index(to)]
                if to in new:
                    kept.setdefault(instance, {}).setdefault(role, {})[name] = to
    return replace(after, redirects=kept)


def check_addresses(workspaces: dict) -> None:
    """Every alias and redirect is one environment's only, and a redirect
    goes to a name of its environment."""
    for ws in workspaces.values():
        for name, instance, role in ws.extra_names():
            taken = address_taken(name, workspaces, ws.name, instance, role)
            if taken:
                raise ConfigError(f"workspaces.{ws.name}: {name} is taken: {taken}")
        for instance, roles in ws.redirects.items():
            for role, pairs in roles.items():
                names = environment_names(ws, instance, role)
                for name, to in pairs.items():
                    if to not in names:
                        raise ConfigError(f"workspaces.{ws.name}: the redirect {name} goes to {to}, "
                                          f"which is no address of {environment_label(instance, role)}")


# a staging's instance (stagings.py): <its production's instance>-stg-<yyyymmdd>[-n]
STAGING_INSTANCE = re.compile(r"(?P<parent>.+)-stg-(?P<stamp>\d{8}(?:-\d+)?)")


def web_names(domain: str, instances, main: str = "", subdomains: dict[str, str] | None = None,
              roles: dict[str, tuple[str, ...]] | None = None) -> list[dict]:
    """The names each instance and role of a workspace answers on:
    [{instance, role, names}], production first. The main instance takes
    <domain> and test.<domain> (and not its own name too); every other
    instance <instance>.<domain> and <instance>.test.<domain>; a subdomain <sub>.<domain> and <sub>.test.<domain>.
    A staging of an instance there answers on stg-<date>.<its first name>,
    as a hosting panel's. roles: {instance: the roles it has}, both by default
    (an instance with its production only has no test names)."""
    found, first = [], {}
    instances = list(instances)
    stagings = {i: m for i in instances if (m := STAGING_INSTANCE.fullmatch(i)) and m["parent"] in instances}
    for instance in sorted(instances, key=lambda i: i in stagings):
        subs = [sub for sub, target in (subdomains or {}).items() if target == instance]
        for role, base in (("production", domain), ("test", f"{TEST_LABEL}.{domain}")):
            if roles is not None and role not in roles.get(instance, ()):
                continue
            if instance in stagings:
                parent = first.get((stagings[instance]["parent"], role))
                if parent is None:
                    continue
                names = [f"stg-{stagings[instance]['stamp']}.{parent}"]
            else:
                # the main instance answers on the domain itself, not also on <instance>.<domain>
                names = [base, *(f"{sub}.{base}" for sub in subs)] if instance == main else \
                    [f"{label}.{base}" for label in (instance, *subs)]
            long = [n for n in names if len(n) > 253]
            if long:
                raise ConfigError(f"{long[0]} is longer than a domain name may be (253)")
            first[(instance, role)] = names[0]
            found.append({"instance": instance, "role": role, "names": names})
    return found


# the HTTPS and DNS of each name as the last publication of its workspace
# found it (inframap.WebNames writes it): {workspace: {"names": {name: {dns, https}}}}
WEB_NAMES_FILE = "webnames.json"


def load_web_names(path: Path) -> dict[str, dict]:
    """{workspace: {name: {dns, https}}} of the file the publications keep; none yet is nothing known."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {ws: found["names"] for ws, found in data.items()
            if isinstance(found, dict) and isinstance(found.get("names"), dict)}


def opening_name(names: list[str], known: dict | None) -> str:
    """The name an environment is opened at: the first of its names (the
    workspace's, then its other names) whose DNS leads to this machine and
    that came out with HTTPS, as the last publication found them; else the
    first whose DNS leads here; else, and when nothing is known, the first.
    A name with no DNS record (a staging's stg-<date>.<domain> without a
    wildcard record) is not where the browser is sent."""
    known = known or {}
    here = [n for n in names if (known.get(n) or {}).get("dns") == "here"]
    with_https = [n for n in here if (known.get(n) or {}).get("https")]
    return (with_https or here or names)[0]


def automatic_name(workspace: str, instance: str, role: str, address: str, domain: str) -> str | None:
    """The name OmniaCLI gives by itself to an environment whose own names
    are not in DNS: <instance>-<workspace>.<a-b-c-d>.<domain> (test-… for a
    test), which a service as sslip.io resolves to a.b.c.d, the machine's
    public IPv4. None without that address or that domain."""
    try:
        if not domain or ipaddress.ip_address(address).version != 4:
            return None
    except ValueError:
        return None
    label = f"{instance}-{workspace}" if role == "production" else f"{TEST_LABEL}-{instance}-{workspace}"
    if len(label) > 63:
        label = f"{label[:54]}-{hashlib.sha1(label.encode()).hexdigest()[:8]}"
    name = f"{label}.{address.replace('.', '-')}.{domain}"
    return name if len(name) <= 253 and DOMAIN_NAME.fullmatch(name) else None


def apply_sites(servers: dict, workspaces: dict, known: dict | None = None) -> None:
    """The address of each instance of a workspace with a domain (Instance.site):
    https on the name it is opened at (opening_name) among those nginx serves
    it on; known: {workspace: {name: {dns, https}}} (load_web_names), the
    first name without it. None for an environment whose names were all read
    without HTTPS: https://<name> would reach another site of the machine."""
    for srv in servers.values():
        for inst in srv.instances.values():
            inst.site = None
    for ws in workspaces.values():
        if not ws.domain:
            continue
        here = {i: tuple(servers[f"{ws.name}-{i}"].instances) for i in ws.instances() if f"{ws.name}-{i}" in servers}
        try:
            sites = web_names(ws.domain, list(here), ws.main, ws.subdomains, here)
        except ConfigError:
            continue
        found = (known or {}).get(ws.name)
        for site in sites:
            names = site["names"] + [n for n in ws.aliases.get(site["instance"], {}).get(site["role"], [])
                                     if n not in site["names"]]
            read = [found[n] for n in names if n in found] if found else []
            if read and not any(state.get("https") for state in read if isinstance(state, dict)):
                # the last publication left every name of it without HTTPS (no certificate yet: nginx
                # answers 503 on them): no name opens it, its page says so and its port is its address
                continue
            servers[f"{ws.name}-{site['instance']}"].instances[site["role"]].site = \
                f"https://{opening_name(names, found)}"


def load_workspaces(path: Path, storage_roots=()) -> dict[str, Workspace]:
    """The workspaces file; none yet is no workspaces."""
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        return parse_workspaces(data.get("workspaces") or {}, storage_roots)
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def save_workspaces(path: Path, workspaces: dict[str, Workspace]) -> None:
    text = yaml.safe_dump({"workspaces": {n: ws.to_dict() for n, ws in workspaces.items()}},
                          sort_keys=False, allow_unicode=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def parse_machine(value, service_user: str | None = None) -> str:
    """The kind of this machine: machine of the main configuration (root
    writes it, the service only reads it); without it development for a
    service run as another user (OMNIACLI_USER of /etc/default/omniacli, which
    root writes too), otherwise server, the restricted one."""
    if value is not None:
        if value not in MACHINE_KINDS:
            raise ConfigError(f"machine: {value!r} is not one of {', '.join(MACHINE_KINDS)}")
        return value
    # empty or omniacli: the package's own user, as postinst takes them
    return MACHINE_DEVELOPMENT if service_user and service_user != "omniacli" else MACHINE_SERVER


# the storage the workspaces live in when the main configuration lists none
# (workspaces.storage missing): root's, made by the package (postinst) when
# the workspaces are allowed
DEFAULT_STORAGE = "/srv/omniacli/default_workspace"
DEFAULT_STORAGE_NAME = "default"
# a storage's name: what the workspaces and every path of root's use, so its
# folder may change (a new disk) and the name stays
STORAGE_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")


# where the root helper binds each folder of workspaces.storage (a systemd
# bind mount, README, Workspaces): root uses only these paths, which nobody
# else can move, so the folders above a storage may be anyone's
STORAGE_BASE = "/var/lib/omniacli-storage"


def bound_root(storage: str) -> str:
    """The root path the storage named storage is bound on: its name, which a
    new folder of it (another disk) keeps."""
    return f"{STORAGE_BASE}/{storage}"


def parse_storage_roots(data) -> dict[str, str]:
    """workspaces.storage of the main configuration: {name: folder}, the
    storages a workspace may live in, each a name (a-z 0-9 -) and an
    absolute, real path (no symbolic links). Missing (or null): the default
    one, {default: DEFAULT_STORAGE}; empty: none. A list of folders (the
    format of before, no names) is refused: StorageListError."""
    data = data if isinstance(data, dict) else {}
    value = data.get("storage")
    if value is None:
        return {DEFAULT_STORAGE_NAME: DEFAULT_STORAGE}
    if isinstance(value, list):
        raise StorageListError(
            "workspaces.storage is a list of folders: give each a name (name: folder), as "
            f"{DEFAULT_STORAGE_NAME}: {DEFAULT_STORAGE}")
    if not isinstance(value, dict):
        raise ConfigError("workspaces.storage must be {name: folder}")
    roots, folders = {}, {}
    for name, folder in value.items():
        name = str(name).strip()
        if not STORAGE_NAME.fullmatch(name):
            raise ConfigError(f"workspaces.storage: {name!r} is not a storage name (a-z 0-9 -, 32 at most)")
        root = str(folder or "").strip().rstrip("/")
        # in a sudoers line too (packaging: postinst): no space, no wildcard
        if not re.fullmatch(r"(/[A-Za-z0-9._-]+)+", root) or "/../" in root + "/" or "/./" in root + "/":
            raise ConfigError(f"workspaces.storage.{name}: {folder!r} is not an absolute path of letters, digits "
                              "and . _ -")
        if root in folders:
            raise ConfigError(f"workspaces.storage: {name} and {folders[root]} are the same folder {root}")
        roots[name], folders[root] = root, name
    return roots


class StorageListError(ConfigError):
    """workspaces.storage in the format of before: OmniaCLI still starts, its
    workspaces unusable until the storages have names."""


# the text size of the shell pages, in pixels
SHELL_FONT_SIZE = 14
SHELL_FONT_SIZES = (8, 40)
# tunnels: closed after this many hours without traffic (TUNNEL_IDLE_HOURS
# the limits); the local address and ports they listen on
TUNNEL_IDLE = 4
TUNNEL_IDLE_HOURS = (1, 720)
TUNNEL_BIND = "127.0.0.1"
TUNNEL_PORTS = "18000-18099"
# the access log's period (NIS 2): days; 0 = by size only, as before
AUDIT_RETENTION_DAYS = 365
AUDIT_RETENTION_LIMITS = (0, 3650)
SYSLOG_PROTOCOLS = ("udp", "tcp", "tls")


# a DNS service that answers <anything>.<a-b-c-d>.sslip.io with a.b.c.d: a
# name built on the machine's public address resolves without a record of anyone's
AUTOMATIC_NAMES = "sslip.io"


@dataclass
class Options:
    """The options of the system, set on the Options page (admin)."""

    shell_font_size: int = SHELL_FONT_SIZE
    tunnel_idle_hours: int = TUNNEL_IDLE
    tunnel_bind: str = TUNNEL_BIND
    tunnel_ports: str = TUNNEL_PORTS
    # the domain of the names OmniaCLI gives by itself to an environment
    # whose own name is not in DNS (automatic_name); empty: none is given
    automatic_names: str = AUTOMATIC_NAMES
    # the SMTP server the notifications of the scheduled actions go through
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_security: str = "starttls"  # SMTP_SECURITY
    smtp_user: str = ""
    smtp_password: str = ""
    mail_from: str = ""
    # who is told when an instance goes down, a disk fills, a certificate ends
    monitor_notify: list = field(default_factory=list)
    # the access log (accesslog.py, docs/nis2-audit.md): the days every
    # entry is kept at least, and the SIEM each entry is sent to (siem.py)
    audit_retention_days: int = AUDIT_RETENTION_DAYS
    syslog_host: str = ""
    syslog_port: int = 514
    syslog_protocol: str = "udp"  # SYSLOG_PROTOCOLS
    syslog_ca_file: str = ""
    # how the users of workspaces read the machine's people (a link's user,
    # a machine admin) in their jobs: empty is "the provider" in their language
    provider_label: str = ""
    # the customers' portal: the name this OmniaCLI is published on for the
    # users of the workspaces (workspacehelper.portal), "" for none; staff:
    # the machine's own users log in there too (else through their link only)
    portal_address: str = ""
    portal_staff: bool = False
    # who logs in with a second step (totp.py, README Two-step verification):
    # nobody; portal: at the portal's address, and the users of the workspaces
    # anywhere once the machine has one (else they would skip it by the
    # machine's own address); everybody
    second_factor: str = "portal"


SMTP_SECURITY = ("starttls", "ssl", "none")
# Options.second_factor
SECOND_FACTOR_POLICIES = ("nobody", "portal", "everybody")
SECOND_FACTOR_DEFAULT = "portal"
# whom a notice goes to: an email address (the SMTP server of Options) or a
# webhook (https, a JSON {"text": ...})
NOTIFY_EMAIL = re.compile(r"[^@\s<>,;]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,63}")
NOTIFY_WEBHOOK = re.compile(r"https://[^\s]{4,2000}")
NOTIFY_MAX = 10


def parse_notify(value, where: str) -> list[str]:
    """Emails and https webhook URLs, a list or a text separated by commas."""
    if value in (None, ""):
        return []
    if isinstance(value, str):
        value = [v for v in re.split(r"[\s,;]+", value) if v]
    if not isinstance(value, list) or len(value) > NOTIFY_MAX:
        raise ConfigError(f"{where}: at most {NOTIFY_MAX} addresses or webhook URLs")
    found = [str(v).strip() for v in value if str(v).strip()]
    bad = [v for v in found if not (NOTIFY_EMAIL.fullmatch(v) or NOTIFY_WEBHOOK.fullmatch(v))]
    if bad:
        raise ConfigError(f"{where}: {bad[0]!r} is neither an email address nor an https:// URL")
    return list(dict.fromkeys(found))


def _whole(value, where: str, limits: tuple[int, int]) -> int:
    low, high = limits
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ConfigError(f"{where} must be a whole number from {low} to {high}")
    return value


def parse_options(data) -> Options:
    data = data if isinstance(data, dict) else {}
    shell = data.get("shell") or {}
    tunnel = data.get("tunnel") or {}
    if not isinstance(shell, dict) or not isinstance(tunnel, dict):
        raise ConfigError("shell and tunnel must be mappings")
    size = _whole(shell.get("font_size", SHELL_FONT_SIZE), "shell.font_size", SHELL_FONT_SIZES)
    idle = _whole(tunnel.get("idle_hours", TUNNEL_IDLE), "tunnel.idle_hours", TUNNEL_IDLE_HOURS)
    bind = str(tunnel.get("bind", TUNNEL_BIND)).strip()
    try:
        ipaddress.ip_address(bind)
    except ValueError:
        raise ConfigError(f"tunnel.bind: {bind!r} is not an IP address (127.0.0.1, 0.0.0.0...)") from None
    ports = str(tunnel.get("ports", TUNNEL_PORTS)).strip()
    match = re.fullmatch(r"(\d+)(?:-(\d+))?", ports)
    low, high = (int(match[1]), int(match[2] or match[1])) if match else (0, 0)
    if not 1024 <= low <= high <= 65535:
        raise ConfigError(f"tunnel.ports: {ports!r} must be a port or a range low-high, from 1024 to 65535")
    mail = data.get("mail") or {}
    if not isinstance(mail, dict):
        raise ConfigError("mail must be a mapping")
    texts = {}
    for key in ("host", "user", "password", "from"):
        value = str(mail.get(key) or "").strip()
        if len(value) > 254 or any(ord(c) < 32 for c in value):
            raise ConfigError(f"mail.{key}: one line of at most 254 characters")
        texts[key] = value
    port = _whole(mail.get("port", 587), "mail.port", (1, 65535))
    security = str(mail.get("security") or "starttls")
    if security not in SMTP_SECURITY:
        raise ConfigError(f"mail.security: one of {', '.join(SMTP_SECURITY)}")
    if texts["from"] and "@" not in texts["from"]:
        raise ConfigError("mail.from: an email address")
    monitor = data.get("monitor") or {}
    if not isinstance(monitor, dict):
        raise ConfigError("monitor must be a mapping")
    audit = data.get("audit") or {}
    syslog = (audit.get("syslog") or {}) if isinstance(audit, dict) else None
    if not isinstance(audit, dict) or not isinstance(syslog, dict):
        raise ConfigError("audit and audit.syslog must be mappings")
    retention = _whole(audit.get("retention_days", AUDIT_RETENTION_DAYS), "audit.retention_days", AUDIT_RETENTION_LIMITS)
    syslog_host = str(syslog.get("host") or "").strip()
    if syslog_host and not re.fullmatch(r"[A-Za-z0-9.:_-]{1,253}", syslog_host):
        raise ConfigError(f"audit.syslog.host: {syslog_host!r} is not a host name or an IP address")
    protocol = str(syslog.get("protocol") or "udp")
    if protocol not in SYSLOG_PROTOCOLS:
        raise ConfigError(f"audit.syslog.protocol: one of {', '.join(SYSLOG_PROTOCOLS)}")
    syslog_port = _whole(syslog.get("port", 6514 if protocol == "tls" else 514), "audit.syslog.port", (1, 65535))
    ca_file = str(syslog.get("ca_file") or "").strip()
    if ca_file and (not ca_file.startswith("/") or any(ord(c) < 32 for c in ca_file) or len(ca_file) > 1024):
        raise ConfigError("audit.syslog.ca_file: an absolute path, or empty for the system's certificates")
    portal = data.get("portal") or {}
    if not isinstance(portal, dict):
        raise ConfigError("portal must be a mapping")
    provider = str(portal.get("provider") or "").strip()
    if len(provider) > 100 or any(ord(c) < 32 for c in provider):
        raise ConfigError("portal.provider: one line of at most 100 characters")
    address = address_name("portal.address", portal["address"]) if portal.get("address") else ""
    staff = portal.get("staff", False)
    if not isinstance(staff, bool):
        raise ConfigError("portal.staff must be true or false")
    second = str(portal.get("second_factor") or SECOND_FACTOR_DEFAULT)
    if second not in SECOND_FACTOR_POLICIES:
        raise ConfigError(f"portal.second_factor: one of {', '.join(SECOND_FACTOR_POLICIES)}")
    names = data.get("names") or {}
    if not isinstance(names, dict):
        raise ConfigError("names must be a mapping")
    automatic = str(names.get("automatic", AUTOMATIC_NAMES) or "").strip().lower().rstrip(".")
    if automatic in ("off", "no", "false"):
        automatic = ""
    if automatic and not DOMAIN_NAME.fullmatch(automatic):
        raise ConfigError(f"names.automatic: {automatic!r} is not a domain (as sslip.io), or off")
    return Options(shell_font_size=size, tunnel_idle_hours=idle, tunnel_bind=bind, tunnel_ports=ports,
                   automatic_names=automatic,
                   smtp_host=texts["host"], smtp_port=port, smtp_security=security, smtp_user=texts["user"],
                   smtp_password=texts["password"], mail_from=texts["from"],
                   monitor_notify=parse_notify(monitor.get("notify"), "monitor.notify"),
                   audit_retention_days=retention, syslog_host=syslog_host, syslog_port=syslog_port,
                   syslog_protocol=protocol, syslog_ca_file=ca_file, provider_label=provider,
                   portal_address=address, portal_staff=staff, second_factor=second)


def load_options(path: Path) -> Options:
    """The options file; none yet is the defaults."""
    if not path.exists():
        return Options()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        return parse_options(data)
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def save_options(path: Path, options: Options) -> None:
    """Mode 600: it may hold the SMTP password."""
    data = {"shell": {"font_size": options.shell_font_size},
            "tunnel": {"idle_hours": options.tunnel_idle_hours, "bind": options.tunnel_bind,
                       "ports": options.tunnel_ports}}
    if options.automatic_names != AUTOMATIC_NAMES:
        data["names"] = {"automatic": options.automatic_names or "off"}
    if options.smtp_host:
        data["mail"] = {"host": options.smtp_host, "port": options.smtp_port, "security": options.smtp_security,
                        "user": options.smtp_user, "password": options.smtp_password, "from": options.mail_from}
    if options.monitor_notify:
        data["monitor"] = {"notify": list(options.monitor_notify)}
    if options.audit_retention_days != AUDIT_RETENTION_DAYS or options.syslog_host:
        data["audit"] = {"retention_days": options.audit_retention_days}
        if options.syslog_host:
            data["audit"]["syslog"] = {"host": options.syslog_host, "port": options.syslog_port,
                                       "protocol": options.syslog_protocol, "ca_file": options.syslog_ca_file}
    portal = {"provider": options.provider_label, "address": options.portal_address, "staff": options.portal_staff}
    portal = {key: value for key, value in portal.items() if value}
    if options.second_factor != SECOND_FACTOR_DEFAULT:
        portal["second_factor"] = options.second_factor
    if portal:
        data["portal"] = portal
    text = yaml.safe_dump(data, sort_keys=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


# the limits of a puller: hours a file and a report are kept, MiB of a file
PULLER_KEEP_HOURS = 24
PULLER_KEEP_LIMITS = (1, 720)
PULLER_FILE_MIB = 10240
PULLER_FILE_LIMITS = (1, 1 << 20)
_LINK_NAME = re.compile(r"[A-Za-z0-9._@-]{1,64}")


@dataclass
class PullerDef:
    """A puller this instance relays (puller.py): the link (Users page) of
    the producer, the OmniaCLI whose instance it serves, and those of its
    consumers. The link names are those of auth.links."""

    name: str
    producer: str
    consumers: list[str] = field(default_factory=list)
    keep_hours: int = PULLER_KEEP_HOURS
    max_file_mib: int = PULLER_FILE_MIB

    def part(self, link: str) -> str | None:
        """producer, consumer or None: what the link is in this puller."""
        if link == self.producer:
            return "producer"
        return "consumer" if link in self.consumers else None

    def to_dict(self) -> dict:
        return {"producer": self.producer, "consumers": list(self.consumers),
                "keep_hours": self.keep_hours, "max_file_mib": self.max_file_mib}


def parse_puller(name: str, data) -> PullerDef:
    where = f"pullers.{name}"
    if not SERVER_NAME.fullmatch(name) or len(name) > 64:
        raise ConfigError(f"{where}: a name is letters, digits and . _ - (it is part of URLs)")
    data = data if isinstance(data, dict) else {}
    producer = str(data.get("producer") or "").strip()
    if not _LINK_NAME.fullmatch(producer):
        raise ConfigError(f"{where}.producer: the name of a link of the Users page")
    consumers = data.get("consumers") or []
    if not isinstance(consumers, list):
        raise ConfigError(f"{where}.consumers: a list of names of links of the Users page")
    consumers = list(dict.fromkeys(str(c).strip() for c in consumers if str(c).strip()))
    bad = [c for c in consumers if not _LINK_NAME.fullmatch(c)]
    if bad:
        raise ConfigError(f"{where}.consumers: {', '.join(bad)} is not a link name")
    if not consumers:
        raise ConfigError(f"{where}.consumers: at least one")
    if producer in consumers:
        raise ConfigError(f"{where}: {producer} cannot be the producer and a consumer")
    keep = _whole(data.get("keep_hours", PULLER_KEEP_HOURS), f"{where}.keep_hours", PULLER_KEEP_LIMITS)
    size = _whole(data.get("max_file_mib", PULLER_FILE_MIB), f"{where}.max_file_mib", PULLER_FILE_LIMITS)
    return PullerDef(name, producer, consumers, keep, size)


def load_pullers(path: Path) -> dict[str, PullerDef]:
    """The pullers file; none yet is no pullers."""
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    pullers = data.get("pullers") or {}
    if not isinstance(pullers, dict):
        raise ConfigError(f"{path}: pullers must be a mapping")
    try:
        return {str(name): parse_puller(str(name), value) for name, value in pullers.items()}
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def save_pullers(path: Path, pullers: dict[str, PullerDef]) -> None:
    text = yaml.safe_dump({"pullers": {n: p.to_dict() for n, p in pullers.items()}},
                          sort_keys=False, allow_unicode=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# what a publication may let its consumers do, each off unless enabled
PUBLISHED_COMMANDS = ("backup", "start", "stop", "restart")


@dataclass
class Exposed:
    """A command a publication lets the consumers send: enabled, and the
    hours it is taken in (AutoUpdate's window: none is always)."""

    enabled: bool = False
    window_start: datetime.time | None = None
    window_end: datetime.time | None = None

    def in_window(self, now: datetime.time) -> bool:
        return in_window(self.window_start, self.window_end, now)

    def window_label(self) -> str:
        return window_label(self.window_start, self.window_end)


@dataclass
class Publication:
    """An instance of this OmniaCLI published as the producer of a puller of
    a relay this one links to (publisher.py). key: the puller's key (text
    opk1-...), empty for none."""

    instance: str  # server/role
    link: str
    puller: str
    key: str = ""
    commands: dict[str, Exposed] = field(default_factory=dict)

    def exposed(self, action: str) -> Exposed:
        return self.commands.get(action) or Exposed()

    def to_dict(self) -> dict:
        data = {"link": self.link, "puller": self.puller}
        if self.key:
            data["key"] = self.key
        data["commands"] = {}
        for action in PUBLISHED_COMMANDS:
            rule = self.exposed(action)
            entry = {"enabled": rule.enabled}
            if rule.window_start is not None and rule.window_end is not None:
                entry["window"] = rule.window_label()
            data["commands"][action] = entry
        return data


def parse_publication(instance: str, data) -> Publication:
    where = f"publications.{instance}"
    server, _, role = instance.partition("/")
    if not SERVER_NAME.fullmatch(server) or role not in ROLES:
        raise ConfigError(f"{where}: an instance is server/role (production or test)")
    data = data if isinstance(data, dict) else {}
    link, name = str(data.get("link") or "").strip(), str(data.get("puller") or "").strip()
    for key, value in (("link", link), ("puller", name)):
        if not SERVER_NAME.fullmatch(value):
            raise ConfigError(f"{where}.{key}: {value!r} is not a name of letters, digits and . _ -")
    key = str(data.get("key") or "").strip()
    if key:
        from .pullcrypto import PullerCryptoError, parse_key  # it imports cryptography

        try:
            parse_key(key)
        except PullerCryptoError as exc:
            raise ConfigError(f"{where}.key: {exc}") from None
    commands = data.get("commands") or {}
    if not isinstance(commands, dict):
        raise ConfigError(f"{where}.commands: a mapping of {', '.join(PUBLISHED_COMMANDS)}")
    unknown = [str(c) for c in commands if c not in PUBLISHED_COMMANDS]
    if unknown:
        raise ConfigError(f"{where}.commands: {', '.join(unknown)} is not one of {', '.join(PUBLISHED_COMMANDS)}")
    exposed = {}
    for action in PUBLISHED_COMMANDS:
        entry = commands.get(action) or {}
        if not isinstance(entry, dict):
            raise ConfigError(f"{where}.commands.{action}: a mapping of enabled and window")
        start, end = _parse_window(entry.get("window"), f"{where}.commands.{action}")
        exposed[action] = Exposed(bool(entry.get("enabled", False)), start, end)
    return Publication(instance, link, name, key, exposed)


def load_publications(path: Path) -> dict[str, Publication]:
    """The publications file; none yet is no publications."""
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    found = data.get("publications") or {}
    if not isinstance(found, dict):
        raise ConfigError(f"{path}: publications must be a mapping")
    try:
        return {str(k): parse_publication(str(k), v) for k, v in found.items()}
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def save_publications(path: Path, publications: dict[str, Publication]) -> None:
    """Write the publications file atomically, readable by its owner only: it holds the keys."""
    text = yaml.safe_dump({"publications": {k: p.to_dict() for k, p in publications.items()}},
                          sort_keys=False, allow_unicode=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


@dataclass
class PullerConnection:
    """This OmniaCLI as a consumer of a puller of a relay it links to
    (consumer.py): the instance the puller's producer publishes, operated from
    here. key: the puller's key (text opk1-...), empty for none."""

    name: str
    link: str
    puller: str
    key: str = ""

    def to_dict(self) -> dict:
        data = {"link": self.link, "puller": self.puller}
        if self.key:
            data["key"] = self.key
        return data


def parse_connection(name: str, data) -> PullerConnection:
    where = f"connections.{name}"
    if not SERVER_NAME.fullmatch(name) or len(name) > 64:
        raise ConfigError(f"{where}: a name is letters, digits and . _ - (it is part of URLs)")
    data = data if isinstance(data, dict) else {}
    link, puller = str(data.get("link") or "").strip(), str(data.get("puller") or "").strip()
    for key, value in (("link", link), ("puller", puller)):
        if not SERVER_NAME.fullmatch(value):
            raise ConfigError(f"{where}.{key}: {value!r} is not a name of letters, digits and . _ -")
    key = str(data.get("key") or "").strip()
    if key:
        from .pullcrypto import PullerCryptoError, parse_key  # it imports cryptography

        try:
            parse_key(key)
        except PullerCryptoError as exc:
            raise ConfigError(f"{where}.key: {exc}") from None
    return PullerConnection(name, link, puller, key)


def load_connections(path: Path) -> dict[str, PullerConnection]:
    """The connections file; none yet is no connections."""
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    found = data.get("connections") or {}
    if not isinstance(found, dict):
        raise ConfigError(f"{path}: connections must be a mapping")
    try:
        return {str(k): parse_connection(str(k), v) for k, v in found.items()}
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def save_connections(path: Path, connections: dict[str, PullerConnection]) -> None:
    """Write the connections file atomically, readable by its owner only: it holds the keys."""
    text = yaml.safe_dump({"connections": {k: c.to_dict() for k, c in connections.items()}},
                          sort_keys=False, allow_unicode=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


# where the plugins published for download are listed (pluginstore.py)
DEFAULT_PLUGIN_INDEX = "https://www.omniasolutions.website/download/omniacli-plugins.json"


@dataclass
class PluginSource:
    """The index of the plugins published for download, and the public key
    their manifests are signed with (empty: none can be installed). Set in
    the main configuration, which only root writes: the root helper that
    installs a plugin reads the key from there (pluginstore.py)."""

    index: str = DEFAULT_PLUGIN_INDEX
    public_key: str = ""


def parse_plugin_source(data) -> PluginSource:
    data = data if isinstance(data, dict) else {}
    index = str(data.get("index") or DEFAULT_PLUGIN_INDEX).strip()
    parts = urlsplit(index)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigError(f"plugins.index: {index!r} is not an http(s) URL")
    key = str(data.get("public_key") or "").strip()
    if key:
        from .packagesign import SigningError, parse_public_key  # it imports cryptography

        try:
            parse_public_key(key)
        except SigningError as exc:
            raise ConfigError(f"plugins.public_key: {exc}") from None
    return PluginSource(index, key)


def parse_settings(data: dict, base_dir: Path = Path(".")) -> Settings:
    fixed = parse_auth(data.get("auth") or {})
    servers_file = Path(data.get("servers_file") or "servers.yaml")
    if not servers_file.is_absolute():
        servers_file = base_dir / servers_file
    # next to the servers file by default: a folder the service may write
    users_file = Path(data.get("users_file") or servers_file.parent / "users.yaml")
    if not users_file.is_absolute():
        users_file = base_dir / users_file
    links_file = Path(data.get("links_file") or servers_file.parent / "links.yaml")
    if not links_file.is_absolute():
        links_file = base_dir / links_file
    groups_file = Path(data.get("groups_file") or servers_file.parent / "groups.yaml")
    if not groups_file.is_absolute():
        groups_file = base_dir / groups_file
    schedules_file = Path(data.get("schedules_file") or servers_file.parent / "schedules.yaml")
    if not schedules_file.is_absolute():
        schedules_file = base_dir / schedules_file
    options_file = Path(data.get("options_file") or servers_file.parent / "options.yaml")
    if not options_file.is_absolute():
        options_file = base_dir / options_file
    pullers_file = Path(data.get("pullers_file") or servers_file.parent / "pullers.yaml")
    if not pullers_file.is_absolute():
        pullers_file = base_dir / pullers_file
    publications_file = Path(data.get("publications_file") or servers_file.parent / "publications.yaml")
    if not publications_file.is_absolute():
        publications_file = base_dir / publications_file
    connections_file = Path(data.get("connections_file") or servers_file.parent / "connections.yaml")
    if not connections_file.is_absolute():
        connections_file = base_dir / connections_file
    scripts_dir = Path(data.get("scripts_dir") or servers_file.parent / "scripts")
    if not scripts_dir.is_absolute():
        scripts_dir = base_dir / scripts_dir
    workspaces_file = Path(data.get("workspaces_file") or servers_file.parent / "workspaces.yaml")
    if not workspaces_file.is_absolute():
        workspaces_file = base_dir / workspaces_file
    try:
        storage_roots, storage_error = parse_storage_roots(data.get("workspaces")), ""
    except StorageListError as exc:
        storage_roots, storage_error = {}, str(exc)
    jobs_dir = Path(_require(data, "jobs_dir", "root"))
    settings = Settings(
        users={},
        backup_dir=Path(_require(data, "backup_dir", "root")),
        jobs_dir=jobs_dir,
        servers_file=servers_file,
        servers=load_servers(servers_file, logs_dir_of(jobs_dir)),
        users_file=users_file,
        links_file=links_file,
        link_targets=load_links(links_file),
        groups_file=groups_file,
        groups=load_groups(groups_file),
        schedules_file=schedules_file,
        scripts_dir=scripts_dir,
        options_file=options_file,
        options=load_options(options_file),
        pullers_file=pullers_file,
        pullers=load_pullers(pullers_file),
        publications_file=publications_file,
        publications=load_publications(publications_file),
        connections_file=connections_file,
        connections=load_connections(connections_file),
        plugin_source=parse_plugin_source(data.get("plugins")),
        workspaces_file=workspaces_file,
        workspaces=load_workspaces(workspaces_file, storage_roots),
        credentials_file=servers_file.parent / "credentials.yaml",
        destinations_file=servers_file.parent / "destinations.yaml",
        storage_roots=storage_roots,
        storage_error=storage_error,
        machine=parse_machine(data.get("machine"), os.environ.get("OMNIACLI_USER")),
        fixed=fixed,
    )
    settings.set_web_access(load_access(users_file))
    apply_sites(settings.servers, settings.workspaces, load_web_names(servers_file.parent / WEB_NAMES_FILE))
    return settings


def logs_dir_of(jobs_dir: Path) -> Path:
    """The logs of the instances OmniaCLI starts itself: next to the jobs."""
    return jobs_dir.parent / "logs"


def load_settings(path: str | Path) -> Settings:
    path = Path(path)
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror}") from exc
    return parse_settings(data, base_dir=path.parent)
