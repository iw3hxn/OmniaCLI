# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The root helper of the instances in a Docker Compose project.

    omniacli compose-helper <project folder> <action> [arguments]

OmniaCLI runs every docker compose command of an instance with restart type
compose through this helper, with sudo, one sudoers line per project:

    omniacli ALL=(root) NOPASSWD: /opt/omniacli/venv/bin/omniacli compose-helper /srv/odooplm-19 *

Whoever may change what docker compose reads in a project is root on the
machine: docker runs as root what the files say, and one compose.override.yaml
mounting / into a container is enough. So before every action the helper,
as root, refuses a project that the user who called sudo could change:

- the folder, and every folder above it up to / (a writable parent may put
  another folder in its place; a sticky one, as /tmp, only when the folder
  is that user's), must not be writable by that user;
- nor the compose files docker compose reads by itself (compose.yaml,
  compose.yml, docker-compose.yaml, docker-compose.yml, and their
  .override.yaml/.yml) and .env; none of them may be a symbolic link;
- .env may not set COMPOSE_FILE, COMPOSE_PATH_SEPARATOR or
  COMPOSE_PROJECT_DIRECTORY, which make docker compose read other files.

A file is writable by a user who owns it (an owner can change its mode), or
through the group or other write bit that applies to the user. ACLs are not
read: a project secured with chown root and chmod go-w has none that matter.

The helper takes a fixed set of actions with checked arguments, never a
command of the caller's, and runs docker with an environment of its own:

    start, stop, restart, state          up -d; stop; up -d then restart; ps -a
    pull                                 pull, then up -d
    logs <service> --tail <N>            the last N lines (1 to 10000) of the
    logs <service> --since <time>        service, or those since a UTC time,
                                         each with its time
    port <service> <port>                where the machine publishes the port
    version <odoo service>               odoo --version
    dump <odoo service> <db> [--no-filestore]
                                         odoo db dump: the zip to the output
    load <odoo service> <db> [--neutralize]
                                         odoo db load --force of the zip read
                                         from the input, in a container of
                                         its own (the service may be stopped)
    update <odoo service> <db>           click-odoo-update, in a container of
                                         the service with OmniaCLI's tools
                                         (TOOLS) mounted read only
    update <odoo service> <db> --modules a,b
                                         odoo -u a,b --stop-after-init
    terminal <odoo service>              a login shell in the running service,
                                         on the caller's terminal, as the
                                         image's user (never root), in the
                                         repositories of addons
    psql <database service> <db>         psql, on the caller's terminal, as the
                                         image's postgres (never root: its \\!
                                         runs commands)
    init <odoo service> <db> <modules> [<language>]
                                         odoo -d <db> -i <modules>
                                         --stop-after-init, without demo data,
                                         in a container of its own: a new
                                         database (a workspace's instance)
    link-filestore <odoo service> <db> <source project> <source db>
                                         the filestore of db made of hard
                                         links to the source's files (copy
                                         of production to test): the source
                                         is checked as the project is, both
                                         data folders bind mounted on
                                         /var/lib/odoo and of the same user,
                                         who links (link_filestore.py merge),
                                         never root: the containers write there
    login <odoo service> <db> users|<user id>
                                         in the running service, odoo shell
                                         runs odoo/login_session.py (its text
                                         on the input, the helper's own file,
                                         never the caller's): the users, or
                                         a new session of one (its sid)
    image <service>                      the image the service's container
                                         runs and its registry digests, as
                                         JSON {"image", "digests"}: is a
                                         newer one published?
    options <odoo service>               in the running service, its python3
                                         runs odoo/config_options.py (its text
                                         on the input, the helper's own file):
                                         the options its Odoo knows
    pip-requirements <odoo service> <repository>
                                         in the running service, as its
                                         user: pip install --user of the
                                         requirements.txt of that repository
                                         of addons (none: said), on the
                                         volume of its data
    setup <odoo service> <db>            in the running service, odoo shell
                                         runs odoo/setup_database.py on a new
                                         database: the admin's password and
                                         the company's country, read by the
                                         helper as JSON on its input (never
                                         on a command line) and checked

A database name is [A-Za-z0-9_.-], a module [a-z0-9_]; a service must be one
of the project's. They reach the container's shell as its arguments, never
in a script's text. The database connection is the one the official image's
entrypoint makes, from HOST, PORT, USER and PASSWORD (or PASSWORD_FILE).
"""

from __future__ import annotations

import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

# the files docker compose reads in a project's folder by itself
COMPOSE_FILES = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
OVERRIDE_FILES = ("compose.override.yaml", "compose.override.yml",
                  "docker-compose.override.yaml", "docker-compose.override.yml")
# what in .env makes docker compose read files other than the folder's
ENV_REFUSED = ("COMPOSE_FILE", "COMPOSE_PATH_SEPARATOR", "COMPOSE_PROJECT_DIRECTORY")
# the environment docker runs with: none of the caller's
ENV = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}
# what each action runs: start applies a changed .env or image (up -d makes
# again what changed), state lists every container, stopped ones too
ACTIONS = {
    "start": [["up", "-d"]],
    "stop": [["stop"]],
    "restart": [["up", "-d"], ["restart"]],
    "state": [["ps", "-a", "--format", "json"]],
    # a newer image of every service, then the containers made again with it
    "pull": [["pull"], ["up", "-d"]],
}
# the actions that take arguments (commands())
WITH_ARGUMENTS = ("logs", "version", "port", "dump", "load", "update", "psql", "init", "link-filestore", "login",
                  "setup", "options", "image", "pip-requirements", "terminal")
# a repository of the instance's addons (/mnt/customer-addons/<name>)
REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
DATABASE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}")
MODULE = re.compile(r"[a-z0-9][a-z0-9_]{0,99}")
LANGUAGE = re.compile(r"[a-z]{2,3}_[A-Z]{2}")
# where click-odoo-contrib and what it needs are, next to this venv, which
# only root writes (the helper itself is there): pure Python, mounted read
# only into a container of the Odoo service for click-odoo-update
TOOLS = Path(sys.prefix) / "omniacli-tools"
TOOLS_MOUNT = "/opt/omniacli-tools"
# in an Odoo container: the database connection its entrypoint makes (the
# official image's /entrypoint.sh: HOST, PORT, USER, PASSWORD or
# PASSWORD_FILE, with their defaults), as the PG* variables Odoo and
# click-odoo leave to libpq when the Odoo config sets no db_host...: a value
# of the config still wins, as with the entrypoint
ODOO_DB = (
    'if [ -n "${PASSWORD_FILE:-}" ]; then PASSWORD="$(cat "$PASSWORD_FILE")"; fi; '
    ': "${HOST:=${DB_PORT_5432_TCP_ADDR:-db}}"; '
    ': "${PORT:=${DB_PORT_5432_TCP_PORT:-5432}}"; '
    ': "${USER:=${DB_ENV_POSTGRES_USER:-${POSTGRES_USER:-odoo}}}"; '
    ': "${PASSWORD:=${DB_ENV_POSTGRES_PASSWORD:-${POSTGRES_PASSWORD:-odoo}}}"; '
    'export PGHOST="${PGHOST:-$HOST}" PGPORT="${PGPORT:-$PORT}" PGUSER="${PGUSER:-$USER}" '
    'PGPASSWORD="${PGPASSWORD:-$PASSWORD}"; '
)
# around a module update, as omniacli/odoo/cron_guard.py does outside
# containers: on a neutralized database, a cron that was off stays off (the
# update switches on those whose XML says active=True outside noupdate). The
# list is "=" and ids, nothing for another database; only integers reach the SQL.
CRONS_BEFORE = (
    'on="$(psql -d "$1" -Atc "SELECT CASE WHEN EXISTS (SELECT 1 FROM ir_config_parameter '
    "WHERE key = 'database.is_neutralized') THEN '=' || coalesce((SELECT string_agg(id::text, ',') "
    "FROM ir_cron WHERE active), '') END\")\" || { echo \"WARNING: the active crons of $1 are not known: "
    'if it is neutralized, the update may switch some on" >&2; on=; }; '
)
CRONS_AFTER = (
    'code=$?; case "$on" in =*) ids="${on#=}"; psql -d "$1" -v ON_ERROR_STOP=1 -Atc '
    '"WITH off AS (UPDATE ir_cron SET active = false WHERE active AND id NOT IN (-1${ids:+,$ids}) RETURNING id) '
    "SELECT 'Neutralized database: cron switched off again: ' || coalesce(d.module || '.' || d.name, 'id ' || off.id) "
    "FROM off LEFT JOIN ir_model_data d ON d.model = 'ir.cron' AND d.res_id = off.id\" || code=1;; esac; "
    'exit $code'
)
# the scripts the Odoo actions run in the container, the database as $1
# (never in the script's text): dump writes the zip to the output, load
# reads it from the input
SCRIPTS = {
    "version": "exec odoo --version",
    # into a file, then to the output: to the output itself, odoo db dump
    # ignores --no-filestore (Odoo 19 and 20, cli/db.py)
    "dump": ODOO_DB + 'f="$(mktemp /tmp/omniacli-dump-XXXXXX.zip)"; trap \'rm -f "$f"\' EXIT; '
                      'odoo db dump "$1" "$f" >&2 && cat "$f"',
    "dump-no-filestore": ODOO_DB + 'f="$(mktemp /tmp/omniacli-dump-XXXXXX.zip)"; trap \'rm -f "$f"\' EXIT; '
                                   'odoo db dump --no-filestore "$1" "$f" >&2 && cat "$f"',
    "load": ODOO_DB + 'f="$(mktemp /tmp/omniacli-load-XXXXXX.zip)"; trap \'rm -f "$f"\' EXIT; '
                      'cat > "$f"; odoo db load --force "$1" "$f"',
    "load-neutralize": ODOO_DB + 'f="$(mktemp /tmp/omniacli-load-XXXXXX.zip)"; trap \'rm -f "$f"\' EXIT; '
                                 'cat > "$f"; odoo db load --force --neutralize "$1" "$f"',
    "update": ODOO_DB + CRONS_BEFORE + f'export PYTHONPATH={TOOLS_MOUNT}; '
                        f'python3 {TOOLS_MOUNT}/bin/click-odoo-update -d "$1"; ' + CRONS_AFTER,
    "update-modules": ODOO_DB + CRONS_BEFORE + 'odoo -d "$1" -u "$2" --stop-after-init --no-http; ' + CRONS_AFTER,
    "psql": 'exec psql -U "${POSTGRES_USER:-postgres}" "$1"',
    # the terminal of the Odoo container: a login shell of the image's user,
    # in the repositories of addons (bash when the image has it)
    "terminal": 'cd /mnt/customer-addons 2>/dev/null || cd; '
                'if command -v bash >/dev/null; then exec bash -l; fi; exec sh -l',
    # odoo shell reads the script from its input; quiet, the answer is its last line
    "login": ODOO_DB + 'OMNIACLI_LOGIN="$2" exec odoo shell -d "$1" --no-http --log-level=critical',
    "setup": ODOO_DB + 'exec odoo shell -d "$1" --no-http --log-level=critical',
    # the options the image's Odoo knows: its python3 reads the script from its input
    "options": "exec python3 -",
    # the Python packages a repository of addons asks (its requirements.txt),
    # as the image's user in its home (/var/lib/odoo/.local, on the volume of
    # the instance's data: they stay when the container is made again)
    "pip-requirements": 'f="/mnt/customer-addons/$1/requirements.txt"; if [ -f "$f" ]; then '
                        'exec python3 -m pip install --user --break-system-packages --disable-pip-version-check '
                        '--no-warn-script-location -r "$f"; else echo "$1 has no requirements.txt"; fi',
}
_ENV_LINE = re.compile(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
# logs: a service's output with the time of each line, the last N lines or
# those since a time (UTC, as docker writes it)
LOGS = ["logs", "--no-color", "--no-log-prefix", "--timestamps"]
MAX_TAIL = 10000
SINCE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?Z")
# link-filestore: Odoo's data folder in its container (the images' data_dir),
# and the script that links, run as the owner of the filestore (never root)
ODOO_DATA = "/var/lib/odoo"
LINK_SCRIPT = Path(__file__).parent / "odoo" / "link_filestore.py"
# login: the script odoo shell runs, given on its input by the helper (a file
# of the package, root's), the request in OMNIACLI_LOGIN, never in its text
LOGIN_SCRIPT = Path(__file__).parent / "odoo" / "login_session.py"
LOGIN_REQUEST = re.compile(r"users|[1-9][0-9]{0,9}")
# setup: the script odoo shell runs on a new database (the admin's password,
# the company's country), its values read by the helper on its own input as
# JSON and put in front of it: the password is on no command line
SETUP_SCRIPT = Path(__file__).parent / "odoo" / "setup_database.py"
OPTIONS_SCRIPT = Path(__file__).parent / "odoo" / "config_options.py"
COUNTRY = re.compile(r"[A-Z]{2}")
PASSWORD_LENGTH = (8, 128)


class HelperError(Exception):
    pass


def caller() -> tuple[str, int, list[int]]:
    """(name, uid, groups) of the user the helper acts for: the one who
    called sudo (SUDO_USER, which sudo sets itself), else the user running it."""
    name = os.environ.get("SUDO_USER") if os.geteuid() == 0 else None
    try:
        entry = pwd.getpwnam(name) if name else pwd.getpwuid(os.getuid())
    except KeyError as exc:
        raise HelperError(f"unknown user {name!r}") from exc
    return entry.pw_name, entry.pw_uid, os.getgrouplist(entry.pw_name, entry.pw_gid)


def _writable(st: os.stat_result, uid: int, groups: list[int]) -> bool:
    if st.st_uid == uid:
        return True  # its owner may chmod it
    if st.st_gid in groups:
        return bool(st.st_mode & stat.S_IWGRP)
    return bool(st.st_mode & stat.S_IWOTH)


def _fix(path: Path) -> str:
    return f"chown root: {path}; chmod go-w {path}"


def problems(project: Path, uid: int, groups: list[int]) -> list[str]:
    """Why the project is not safe from the user (uid, groups); [] when it is.
    Root is not checked: it may do all this anyway."""
    if uid == 0:
        return []
    found = []
    if not project.is_absolute():
        return [f"{project} is not an absolute path"]
    if Path(os.path.realpath(project)) != project:
        return [f"{project} goes through a symbolic link: give its real path "
                f"{os.path.realpath(project)}"]
    try:
        chain = [project, *project.parents]
        stats = {path: path.lstat() for path in chain}
    except OSError as exc:
        return [f"{exc.filename}: {exc.strerror}"]
    if not stat.S_ISDIR(stats[project].st_mode):
        return [f"{project} is not a folder"]
    # every folder that holds one of the chain may replace it
    for child, parent in zip(chain, chain[1:]):
        st = stats[parent]
        if not _writable(st, uid, groups):
            continue
        if st.st_mode & stat.S_ISVTX and stats[child].st_uid != uid:
            continue  # sticky: only the child's owner may rename it
        found.append(f"{parent} is writable by the user: it may replace {child}; {_fix(parent)}")
    if _writable(stats[project], uid, groups):
        found.append(f"{project} is writable by the user: it may add a compose override; {_fix(project)}")
    for name in (*COMPOSE_FILES, *OVERRIDE_FILES, ".env"):
        path = project / name
        try:
            st = path.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            found.append(f"{path}: {exc.strerror}")
            continue
        if stat.S_ISLNK(st.st_mode):
            found.append(f"{path} is a symbolic link: docker compose would read what it points to")
        elif _writable(st, uid, groups):
            found.append(f"{path} is writable by the user; {_fix(path)}")
    found += env_problems(project / ".env")
    if not any((project / name).is_file() for name in COMPOSE_FILES):
        found.append(f"{project} holds no {', '.join(COMPOSE_FILES)}")
    return found


def env_problems(env_file: Path) -> list[str]:
    try:
        text = env_file.read_text(errors="replace")
    except FileNotFoundError:
        return []
    except OSError as exc:
        return [f"{env_file}: {exc.strerror}"]
    keys = {m.group(1) for m in map(_ENV_LINE.match, text.splitlines()) if m}
    return [f"{env_file} sets {key}: docker compose would read other files; remove it"
            for key in ENV_REFUSED if key in keys]


def compose(project: Path, *args: str) -> list[str]:
    docker = shutil.which("docker", path=ENV["PATH"])
    if docker is None:
        raise HelperError("docker is not installed")
    return [docker, "compose", "--project-directory", str(project), *args]


def services(project: Path) -> list[str]:
    """The services of the project, as docker compose reads them."""
    done = subprocess.run(compose(project, "config", "--services"), env=ENV, cwd=project,
                          capture_output=True, text=True)
    if done.returncode:
        raise HelperError(f"docker compose cannot read {project}: {done.stderr.strip()}")
    return done.stdout.split()


def service(project: Path, name: str) -> str:
    if name not in services(project):
        raise HelperError(f"{project} has no service {name!r}")
    return name


def _database(name: str) -> str:
    if not DATABASE.fullmatch(name):
        raise HelperError(f"{name!r} is not a database name")
    return name


def _script(service_name: str, script: str, *args: str, options: tuple[str, ...] = ("-T",)) -> list[str]:
    """exec in the service's container (-T: no terminal, the output as it
    is), the script's arguments apart from its text."""
    return ["exec", *options, service_name, "sh", "-c", SCRIPTS[script], "sh", *args]


def terminal_options(project: Path, service_name: str) -> tuple[str, ...]:
    """The terminal of a service runs as the image's own user (odoo), never
    root: an image that runs as root is refused."""
    done = subprocess.run(compose(project, "exec", "-T", service_name, "id", "-u"),
                          env=ENV, cwd=project, capture_output=True, text=True)
    if done.returncode:
        raise HelperError(f"cannot ask {service_name} who it runs as (is it running?): {done.stderr.strip()}")
    if done.stdout.strip() in ("", "0"):
        raise HelperError(f"{service_name} runs as root: no terminal is opened in it")
    return ()


def psql_options(project: Path, service_name: str) -> tuple[str, ...]:
    """Who psql runs as: its \\! runs any command as that user, so never root,
    which on an image without a USER (postgres:17) is what exec gives: the
    image's postgres, else the container's own user. Root's only: refused."""
    done = subprocess.run(compose(project, "exec", "-T", service_name, "sh", "-c", "id -u; id -u postgres || true"),
                          env=ENV, cwd=project, capture_output=True, text=True)
    if done.returncode:
        raise HelperError(f"cannot ask {service_name} who it runs as: {done.stderr.strip()}")
    ids = done.stdout.split()
    if len(ids) > 1:
        return ("--user", "postgres")
    if ids and ids[0] != "0":
        return ()
    raise HelperError(f"{service_name} runs as root and has no user postgres: "
                      "psql is not opened, its \\! would run as root")


def commands(project: Path, action: str, args: list[str]) -> list[list[str]]:
    """The docker compose commands of an action, its arguments checked."""
    if action in ACTIONS:
        if args:
            raise HelperError(f"{action} takes no arguments")
        return ACTIONS[action]
    if action == "version":
        # version <odoo service>: odoo --version
        if len(args) != 1:
            raise HelperError("usage: version <service>")
        return [_script(service(project, args[0]), "version")]
    if action == "port":
        # port <service> <port in the container>: where the machine publishes it
        if len(args) != 2 or not (args[1].isdigit() and 1 <= int(args[1]) <= 65535):
            raise HelperError("usage: port <service> <port>")
        return [["port", service(project, args[0]), args[1]]]
    if action == "dump":
        # dump <odoo service> <database> [--no-filestore]: the zip to the output
        if len(args) not in (2, 3) or args[2:] not in ([], ["--no-filestore"]):
            raise HelperError("usage: dump <service> <database> [--no-filestore]")
        script = "dump-no-filestore" if args[2:] else "dump"
        return [_script(service(project, args[0]), script, _database(args[1]))]
    if action == "load":
        # load <odoo service> <database> [--neutralize]: the zip from the input
        if len(args) not in (2, 3) or args[2:] not in ([], ["--neutralize"]):
            raise HelperError("usage: load <service> <database> [--neutralize]")
        # in a container of its own: the Odoo service may be stopped (a
        # restore loads with Odoo stopped), run starts the database it needs
        script = "load-neutralize" if args[2:] else "load"
        return [["run", "--rm", "-T", "--entrypoint", "sh", service(project, args[0]), "-c", SCRIPTS[script],
                 "sh", _database(args[1])]]
    if action == "update":
        # update <odoo service> <database> [--modules a,b]: the changed
        # modules (click-odoo-update, in a container of its own with the
        # tools mounted) or the ones named
        if len(args) == 2:
            if not (TOOLS / "bin" / "click-odoo-update").is_file():
                raise HelperError(f"{TOOLS} holds no click-odoo-update: install OmniaCLI's tools")
            return [["run", "--rm", "--no-deps", "-T", "-v", f"{TOOLS}:{TOOLS_MOUNT}:ro",
                     "--entrypoint", "sh", service(project, args[0]), "-c", SCRIPTS["update"], "sh",
                     _database(args[1])]]
        if len(args) == 4 and args[2] == "--modules":
            modules = args[3].split(",")
            if not all(MODULE.fullmatch(m) for m in modules):
                raise HelperError(f"{args[3]!r} is not a list of module names")
            return [_script(service(project, args[0]), "update-modules", _database(args[1]), ",".join(modules))]
        raise HelperError("usage: update <service> <database> [--modules a,b]")
    if action == "init":
        # init <odoo service> <database> <modules> [<language>]: a new database,
        # in a container of its own (the image's entrypoint gives the connection)
        if len(args) not in (3, 4):
            raise HelperError("usage: init <service> <database> <modules> [<language>]")
        modules = args[2].split(",")
        if not all(MODULE.fullmatch(m) for m in modules):
            raise HelperError(f"{args[2]!r} is not a list of module names")
        if args[3:] and not LANGUAGE.fullmatch(args[3]):
            raise HelperError(f"{args[3]!r} is not a language, as it_IT")
        return [["run", "--rm", "-T", service(project, args[0]), "odoo", "-d", _database(args[1]),
                 "-i", ",".join(modules), "--stop-after-init", "--without-demo=all", "--no-http",
                 *(["--load-language", args[3]] if args[3:] else [])]]
    if action == "login":
        # login <odoo service> <database> users|<user id>: in the running
        # service, as its user (Odoo's sessions are its files)
        if len(args) != 3 or not LOGIN_REQUEST.fullmatch(args[2]):
            raise HelperError("usage: login <service> <database> users|<user id>")
        return [_script(service(project, args[0]), "login", _database(args[1]), args[2])]
    if action == "options":
        # options <odoo service>: in the running service
        if len(args) != 1:
            raise HelperError("usage: options <service>")
        return [_script(service(project, args[0]), "options")]
    if action == "pip-requirements":
        # pip-requirements <odoo service> <repository>: in the running service
        if len(args) != 2 or not REPOSITORY.fullmatch(args[1]) or ".." in args[1]:
            raise HelperError("usage: pip-requirements <service> <repository>")
        return [_script(service(project, args[0]), "pip-requirements", args[1])]
    if action == "setup":
        # setup <odoo service> <database>: the values on the helper's input
        if len(args) != 2:
            raise HelperError("usage: setup <service> <database> (the values, JSON, on the input)")
        return [_script(service(project, args[0]), "setup", _database(args[1]))]
    if action == "psql":
        # psql <database service> <database>: on a terminal, the web shell's
        if len(args) != 2:
            raise HelperError("usage: psql <service> <database>")
        name = service(project, args[0])
        return [_script(name, "psql", _database(args[1]), options=psql_options(project, name))]
    if action == "terminal":
        # terminal <odoo service>: on the caller's terminal, never as root
        if len(args) != 1:
            raise HelperError("usage: terminal <service>")
        name = service(project, args[0])
        return [_script(name, "terminal", options=terminal_options(project, name))]
    if action == "logs":
        # logs <service> --tail <N> | --since <time>
        if len(args) != 3 or args[1] not in ("--tail", "--since"):
            raise HelperError("usage: logs <service> --tail <lines> | --since <time>")
        if args[1] == "--tail" and not (args[2].isdigit() and 1 <= int(args[2]) <= MAX_TAIL):
            raise HelperError(f"--tail takes 1 to {MAX_TAIL} lines")
        if args[1] == "--since" and not SINCE.fullmatch(args[2]):
            raise HelperError("--since takes a UTC time, as 2026-10-28T16:48:50.380582551Z")
        return [[*LOGS, args[1], args[2], service(project, args[0])]]
    raise HelperError(f"unknown action {action!r}: {', '.join([*ACTIONS, *WITH_ARGUMENTS])}")


def data_folder(project: Path, service_name: str) -> Path:
    """The folder of this machine bind mounted on Odoo's data folder."""
    done = subprocess.run(compose(project, "config", "--format", "json"), env=ENV, cwd=project,
                          capture_output=True, text=True)
    if done.returncode:
        raise HelperError(f"docker compose cannot read {project}: {done.stderr.strip()}")
    for mount in json.loads(done.stdout)["services"][service_name].get("volumes") or []:
        if mount.get("target") == ODOO_DATA:
            if mount.get("type") != "bind":
                raise HelperError(f"{project}: {ODOO_DATA} of {service_name} is a {mount.get('type')}, "
                                  "not a folder of this machine: its filestore is not linked")
            return Path(mount["source"])
    raise HelperError(f"{project}: nothing is mounted on {ODOO_DATA} of {service_name}")


def as_owner(cmd: list[str], uid: int, gid: int, extra_groups: tuple[int, ...] = ()) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, env=ENV, cwd="/", user=uid, group=gid, extra_groups=list(extra_groups),
                          capture_output=True, text=True)


def closed_groups(path: Path) -> tuple[int, ...]:
    """The groups that open the way to path where the others may not pass (a
    workspace's disk): the image's user is no member, and only OmniaCLI's own
    script runs with them."""
    found = {st.st_gid for st in map(os.lstat, path.parents) if st.st_gid and not st.st_mode & stat.S_IXOTH}
    return tuple(sorted(found))


def image_state(project: Path, args: list[str]) -> int:
    """image <service>: {"image": "odoo:19.0", "digests": ["odoo@sha256:..."]}
    of the image its container was made from, on the output."""
    if len(args) != 1:
        raise HelperError("usage: image <service>")
    name = service(project, args[0])
    done = subprocess.run(compose(project, "images", "--format", "json", name), env=ENV, cwd=project,
                          capture_output=True, text=True)
    if done.returncode:
        raise HelperError(f"docker compose images failed: {done.stderr.strip()[-300:]}")
    try:
        rows = json.loads(done.stdout or "[]")
    except ValueError:
        raise HelperError("docker compose images: not JSON") from None
    rows = rows if isinstance(rows, list) else [rows]
    if not rows:
        print(json.dumps({"image": None, "digests": []}))
        return 0
    row = rows[0]
    image_id = str(row.get("ID") or "")
    if not re.fullmatch(r"(sha256:)?[0-9a-f]{12,64}", image_id):
        raise HelperError(f"{image_id!r} is not an image id")
    inspect = subprocess.run([compose(project)[0], "image", "inspect", "--format", "{{json .RepoDigests}}", image_id],
                             env=ENV, cwd=project, capture_output=True, text=True)
    try:
        digests = json.loads(inspect.stdout) if inspect.returncode == 0 else []
    except ValueError:
        digests = []
    print(json.dumps({"image": f"{row.get('Repository')}:{row.get('Tag')}", "digests": digests or []}))
    return 0


def link_filestore(project: Path, args: list[str], uid: int, groups: list[int]) -> int:
    """link-filestore <odoo service> <database> <source project> <source
    database>: the filestore of database becomes hard links to the files of
    the source's (the copy of production to test). Linked by the owner of
    the source's filestore, not by root: the containers write there."""
    if len(args) != 4:
        raise HelperError("usage: link-filestore <service> <database> <source project> <source database>")
    service_name, database, source, source_db = args[0], _database(args[1]), Path(args[2]), _database(args[3])
    if source == project:
        raise HelperError("the source is the project itself")
    found = problems(source, uid, groups)
    if found:
        raise HelperError(f"{source} is not safe from the user:\n  " + "\n  ".join(found))
    service(project, service_name)
    service(source, service_name)
    source_data, data = data_folder(source, service_name), data_folder(project, service_name)
    try:
        store = (source_data / "filestore").lstat()
        target = data.lstat()
    except OSError as exc:
        raise HelperError(f"{exc.filename}: {exc.strerror}") from None
    if not stat.S_ISDIR(store.st_mode) or not stat.S_ISDIR(target.st_mode):
        raise HelperError(f"{source_data / 'filestore'} and {data} must be folders")
    if store.st_uid == 0 or (target.st_uid, target.st_gid) != (store.st_uid, store.st_gid):
        raise HelperError(f"{source_data / 'filestore'} and {data} are not of the same user (not root)")
    src, dst = str(source_data / "filestore" / source_db), str(data / "filestore" / database)
    # the script's text, read here: the owner need not reach OmniaCLI's folder
    code = LINK_SCRIPT.read_text(encoding="utf-8")
    for mode in ("check", "merge"):
        done = as_owner([sys.executable, "-I", "-c", code, mode, src, dst], store.st_uid, store.st_gid,
                        closed_groups(source_data) + closed_groups(data))
        print(done.stdout.strip() or "", end="\n" if done.stdout.strip() else "")
        if done.returncode:
            print(done.stderr.strip(), file=sys.stderr)
            return done.returncode
    return 0


def setup_input(raw: bytes) -> bytes:
    """What odoo shell reads for setup: the values checked (a password of
    PASSWORD_LENGTH characters, a country code as CH) as a Python literal,
    then the script."""
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        raise HelperError("setup: the input is no JSON") from None
    if not isinstance(data, dict) or set(data) - {"password", "country"}:
        raise HelperError("setup: the input takes password and country only")
    clean = {}
    password = data.get("password")
    if password is not None:
        if not isinstance(password, str) or not PASSWORD_LENGTH[0] <= len(password) <= PASSWORD_LENGTH[1] \
                or any(ord(c) < 32 for c in password):
            raise HelperError(f"setup: a password is {PASSWORD_LENGTH[0]} to {PASSWORD_LENGTH[1]} characters, "
                              "no control character")
        clean["password"] = password
    country = data.get("country")
    if country:
        if not isinstance(country, str) or not COUNTRY.fullmatch(country):
            raise HelperError(f"setup: {country!r} is not a country code, as CH")
        clean["country"] = country
    return f"OMNIACLI_SETUP = {clean!r}\n".encode() + SETUP_SCRIPT.read_bytes()


def run(argv: list[str]) -> int:
    """compose-helper <project> <action> [arguments]: the exit code of the action."""
    if len(argv) < 2:
        raise HelperError("usage: compose-helper <project folder> <action> [arguments]")
    project, action, args = Path(argv[0]), argv[1], argv[2:]
    if action not in (*ACTIONS, *WITH_ARGUMENTS):
        raise HelperError(f"unknown action {action!r}: {', '.join([*ACTIONS, *WITH_ARGUMENTS])}")
    name, uid, groups = caller()
    found = problems(project, uid, groups)
    if found:
        raise HelperError(f"{project} is not safe from user {name}:\n  " + "\n  ".join(found))
    if action == "link-filestore":
        return link_filestore(project, args, uid, groups)
    if action == "image":
        return image_state(project, args)
    # login: the script on the input of odoo shell, the helper's own file;
    # the other actions keep the caller's input (load reads its zip there)
    extra = {"input": LOGIN_SCRIPT.read_bytes()} if action == "login" else {}
    if action == "options":
        extra = {"input": OPTIONS_SCRIPT.read_bytes()}
    if action == "setup":
        extra = {"input": setup_input(sys.stdin.buffer.read(65536))}
    for cmd in commands(project, action, args):
        code = subprocess.run(compose(project, *cmd), env=ENV, cwd=project, **extra).returncode
        if code:
            return code
    return 0


def main(argv: list[str]) -> int:
    try:
        return run(argv)
    except HelperError as exc:
        print(f"compose-helper: {exc}", file=sys.stderr)
        return 2
