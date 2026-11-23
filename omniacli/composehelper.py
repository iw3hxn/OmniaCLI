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
    logs <service> --since <time>        service, or those since a UTC time
         [--until <time>] [--first|--last <N>]  (to a UTC time; only the first or
                                         the last N of them, counted),
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
    update <odoo service> <db> --install a,b
                                         odoo -i a,b --stop-after-init, in
                                         the running service, an existing
                                         database (the catalog of modules),
                                         without demo data (the flag of the
                                         image's Odoo: without_demo())
    next-update <odoo service> <db>      an update without stopping: the
                                         changed modules (click-odoo-update) of
                                         the new code, OmniaCLI's copy in
                                         addons/.omniacli-next/<repository>, its
                                         addons path read here, in a container
                                         of its own; the instance's keeps serving
    next-start <odoo service>            a container of the service on that code,
                                         on free ports of 127.0.0.1, no cron
                                         thread: its name and ports as JSON
    next-stop, next-state                that container removed; it, as JSON
    update-list <odoo service> <db>      Odoo's Update the module list (a fixed
                                         line of odoo shell, in the running
                                         service): the modules of the addons
                                         path known to the database, in Apps
    modules <database service> <db>      the modules the database knows, each
                                         name|state (fixed query)
    terminal <odoo service>              a login shell in the running service,
                                         on the caller's terminal, as the
                                         image's user (never root), in the
                                         repositories of addons
    psql <database service> <db>         psql, on the caller's terminal, as the
                                         image's postgres (never root: its \\!
                                         runs commands)
    init <odoo service> <db> <modules> [<language>]
                                         odoo -d <db> -i <modules>
                                         --stop-after-init, without demo data
                                         (the flag of the image's Odoo), in a
                                         container of its own: a new
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
    neutralize <odoo service> <db>       odoo neutralize (no outgoing mail, no
                                         scheduled actions), in a container
                                         of its own
    pg-report <database service> <db>    the database's health (fixed queries:
                                         size, connections, cache, long
                                         queries, tables, unused indexes,
                                         pg_stat_statements when there), JSON
    pg-terminate <database service> <db> <pid>
                                         a query of that database stopped
    pip-freeze <odoo service>            in the running service: the Python
                                         packages installed for its user
                                         (pip freeze --user)
    verify <odoo service>                a backup's zip read from the input
                                         loaded (neutralized) into a database
                                         of its own, omniacli_verify_<random>,
                                         in a container of its own; odoo shell
                                         runs odoo/verify_check.py there (the
                                         helper's own file); the database and
                                         its filestore dropped, whatever
                                         happened
    image <service>                      the image the service's container
                                         runs and its registry digests, as
                                         JSON {"image", "digests"}: is a
                                         newer one published?
    options <odoo service>               in the running service, its python3
                                         runs odoo/config_options.py (its text
                                         on the input, the helper's own file):
                                         the options its Odoo knows
    pip-requirements <odoo service> <repository>
                                         in a container of the service of
                                         its own (the service may be
                                         stopped: an update stops it before
                                         its code moves), as its user: pip
                                         install --user of the
                                         requirements.txt of that repository
                                         of addons (none: said), on the
                                         volume of its data, which its Odoo
                                         reads
    stop-service <service>               that service stopped, the others
                                         kept running (the database an
                                         update of the modules needs)
    setup <odoo service> <db>            in the running service, odoo shell
                                         runs odoo/setup_database.py on a new
                                         database: the admin's password and
                                         the company's country, read by the
                                         helper as JSON on its input (never
                                         on a command line) and checked
    pg-settings <database service> <db>  the tuning of the dedicated database
                                         (fixed query, JSON): version, size,
                                         connections, the parameters of TUNED
                                         (now, of postgresql.conf, of
                                         postgresql.auto.conf, when a change
                                         takes effect) and the others written
                                         in postgresql.auto.conf; never one of
                                         the point-in-time recovery's or the
                                         replication's (hidden())
    pg-set <database service> <db>       ALTER SYSTEM SET of parameters of TUNED,
                                         {name: value} read as JSON on the
                                         input and checked, then pg_reload_conf
    pg-reset <database service> <db>     ALTER SYSTEM RESET of parameters of
                                         TUNED, [names] as JSON on the input
                                         (never RESET ALL), then pg_reload_conf
    db-restart <database service> <db>   {"set": {...}, "reset": [...]} on the
                                         input written as pg-set and pg-reset
                                         do, CHECKPOINT, a fast stop, a start,
                                         RESTART_TIMEOUT s for it to answer;
                                         when it does not, postgresql.auto.conf
                                         of before put back and started again.
                                         pg_stat_statements created in db when
                                         loaded, dropped when not. JSON
    mail-list <start> <limit>            a staging's caught mail (the service
    mail-get <message id>                mail, Mailpit, of its profile
    mail-clear                           mail-capture): in its container, the
                                         API on its own loopback (no port
                                         published): the messages from the
                                         newest, one message, all deleted;
                                         JSON as Mailpit answers
    mail-send                            a message written as an outside
                                         sender, {from, name, to, subject,
                                         body, in_reply_to} as JSON on the
                                         input, checked and built here, to
                                         the mail service's SMTP (a staging
                                         with the inbox service only)
    mail-inbox <odoo service> <db>       Odoo's incoming mail of a staging
                                         (odoo/mail_inbox.py in odoo shell),
                                         {mode: state|on|fetch, domain:
                                         <name>.test} as JSON on the input

A project on the machine's cluster (its .env says DB_CLUSTER, machinedb.py)
has no database service: its database actions (modules, psql, pg-report,
pg-terminate) name the service "machine" and run in its Odoo service as the
environment's own role; its tuning is the cluster's (refused here); load and
verify give its role CREATEDB for their time only, and load and init leave
the database its role's, closed to the others (machinedb.close). verify
names its throwaway database <role>-verify-<hex> there.

A database name is [A-Za-z0-9_.-], a module [a-z0-9_]; a service must be one
of the project's. They reach the container's shell as its arguments, never
in a script's text. The database connection is the one the official image's
entrypoint makes, from HOST, PORT, USER and PASSWORD (or PASSWORD_FILE).
"""

from __future__ import annotations

import base64
import collections
import email.message
import email.policy
import email.utils
import html
import json
import os
import pwd
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

from . import machinedb

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
# the database's health, one JSON line (jsonb: json_agg would break lines):
# only these fixed queries, never the
# caller's text (pgreport.py reads them on a venv's database the same way)
PG_REPORT = """SELECT json_build_object(
 'version', current_setting('server_version'),
 'size', pg_database_size(current_database()),
 'connections', (SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()),
 'max_connections', current_setting('max_connections')::int,
 'cache_hit', (SELECT round(100.0 * sum(blks_hit) / nullif(sum(blks_hit) + sum(blks_read), 0), 2)
               FROM pg_stat_database WHERE datname = current_database()),
 'long_queries', (SELECT coalesce(json_agg(q), '[]') FROM (
     SELECT pid, usename AS "user", state, extract(epoch FROM now() - query_start)::int AS seconds,
            wait_event_type AS waiting,
            left(query, 400) AS query
     FROM pg_stat_activity WHERE datname = current_database() AND state <> 'idle' AND pid <> pg_backend_pid()
       AND query_start < now() - interval '5 seconds' ORDER BY query_start LIMIT 20) q),
 'tables', (SELECT coalesce(json_agg(t), '[]') FROM (
     SELECT relname AS name, pg_total_relation_size(relid) AS size, n_live_tup AS rows, n_dead_tup AS dead,
            seq_scan, idx_scan, last_autovacuum
     FROM pg_stat_user_tables ORDER BY pg_total_relation_size(relid) DESC LIMIT 15) t),
 'unused_indexes', (SELECT coalesce(json_agg(i), '[]') FROM (
     SELECT s.indexrelname AS name, s.relname AS "table", pg_relation_size(s.indexrelid) AS size
     FROM pg_stat_user_indexes s JOIN pg_index x ON x.indexrelid = s.indexrelid
     WHERE s.idx_scan = 0 AND NOT x.indisunique AND NOT x.indisprimary
     ORDER BY pg_relation_size(s.indexrelid) DESC LIMIT 15) i),
 'sequential', (SELECT coalesce(json_agg(t), '[]') FROM (
     SELECT relname AS name, seq_scan, seq_tup_read AS rows_read, idx_scan, n_live_tup AS rows
     FROM pg_stat_user_tables WHERE seq_scan > 0 AND n_live_tup > 10000
     ORDER BY seq_tup_read DESC LIMIT 10) t),
 'statements_on', EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements'))::jsonb"""
PG_STATEMENTS = """SELECT coalesce(json_agg(s), '[]')::jsonb FROM (
 SELECT calls, round(total_exec_time::numeric, 1) AS total_ms, round(mean_exec_time::numeric, 1) AS mean_ms,
        rows, left(query, 400) AS query
 FROM pg_stat_statements WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
 ORDER BY total_exec_time DESC LIMIT 20) s"""
PID = re.compile(r"[1-9][0-9]{0,9}")
# the tuning of a dedicated database (UX-008 §6.2, AD-005): these parameters
# only, each with the form its value takes. Whether one takes effect at once
# or at a restart is PostgreSQL's (pg_settings.context), never written here.
TUNED = {"max_connections": "integer", "shared_buffers": "memory", "effective_cache_size": "memory",
         "work_mem": "memory", "maintenance_work_mem": "memory", "random_page_cost": "real",
         "shared_preload_libraries": "libraries"}
# never shown, never written: the point-in-time recovery's (pitrhelper writes
# archive_* in the same postgresql.auto.conf) and the replication's
HIDDEN_PREFIXES = ("archive_", "recovery_", "wal_", "synchronous_", "primary_", "hot_standby")
HIDDEN_NAMES = ("restore_command", "max_wal_senders", "max_replication_slots", "max_slot_wal_keep_size",
                "promote_trigger_file")


def hidden(name: str) -> bool:
    return name.startswith(HIDDEN_PREFIXES) or name in HIDDEN_NAMES


assert not any(map(hidden, TUNED)), "a tuned parameter is one of the point-in-time recovery's"
# a value as postgresql.conf writes it: 128MB, 1GB, 8192 (in the unit of the
# parameter); 1.1; a list of libraries. No quote, no space: it is put in SQL
TUNED_VALUE = {"integer": re.compile(r"[1-9][0-9]{0,5}"),
               "memory": re.compile(r"[1-9][0-9]{0,9}(?:kB|MB|GB|TB)?"),
               "real": re.compile(r"[0-9]{1,4}(?:\.[0-9]{1,6})?"),
               "libraries": re.compile(r"(?:[a-z_][a-z0-9_]{0,62}(?:,[a-z_][a-z0-9_]{0,62})*)?")}
# the only library a tuning adds; one already loaded may stay
STATEMENTS_LIBRARY = "pg_stat_statements"
TUNE_INPUT = 65536
# the restart: at most so long for the database to answer again
RESTART_TIMEOUT = 180
STOP_TIMEOUT = 120
_LIKE = {p: p.replace("_", "\\_") for p in HIDDEN_PREFIXES}
_NOT_HIDDEN = " AND ".join([*(f"f.name NOT LIKE '{_LIKE[p]}%'" for p in HIDDEN_PREFIXES),
                            "f.name NOT IN (" + ", ".join(f"'{n}'" for n in HIDDEN_NAMES) + ")"])
_TUNED_IN = "(" + ", ".join(f"'{n}'" for n in TUNED) + ")"
# the tuning's state, one JSON line: a value of postgresql.conf (or of an
# included file) is the base, one of postgresql.auto.conf is ALTER SYSTEM's
PG_SETTINGS = f"""SELECT json_build_object(
 'version', current_setting('server_version'),
 'size', pg_database_size(current_database()),
 'cluster_size', (SELECT sum(pg_database_size(oid)) FROM pg_database WHERE datallowconn),
 'connections', (SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'client backend'),
 'statements_on', EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements'),
 'settings', (SELECT coalesce(json_agg(s ORDER BY s.name), '[]') FROM (
     SELECT p.name, p.setting, p.unit, p.vartype, p.context, p.min_val, p.max_val, p.boot_val, p.pending_restart,
            (SELECT f.setting FROM pg_file_settings f WHERE f.name = p.name
               AND f.sourcefile NOT LIKE '%/postgresql.auto.conf' ORDER BY f.seqno DESC LIMIT 1) AS file_val,
            (SELECT f.setting FROM pg_file_settings f WHERE f.name = p.name
               AND f.sourcefile LIKE '%/postgresql.auto.conf' ORDER BY f.seqno DESC LIMIT 1) AS auto_val
     FROM pg_settings p WHERE p.name IN {_TUNED_IN}) s),
 'changed', (SELECT coalesce(json_agg(c ORDER BY c.name), '[]') FROM (
     SELECT DISTINCT ON (f.name) f.name, f.setting AS auto_val, p.setting, p.unit, p.context, p.pending_restart
     FROM pg_file_settings f LEFT JOIN pg_settings p ON p.name = f.name
     WHERE f.sourcefile LIKE '%/postgresql.auto.conf' AND f.name NOT IN {_TUNED_IN} AND {_NOT_HIDDEN}
     ORDER BY f.name, f.seqno DESC) c))::jsonb"""
# the actions that take arguments (commands())
# the modules a database knows (ir_module_module), each name|state: the
# catalog of modules tells installed, not installed and unknown apart
MODULES_QUERY = "SELECT name, state FROM ir_module_module ORDER BY name"
WITH_ARGUMENTS = ("logs", "version", "port", "dump", "load", "update", "psql", "init", "link-filestore", "login",
                  "setup", "options", "image", "verify", "pip-freeze",
                  "pg-report", "pg-terminate", "neutralize", "pip-requirements", "terminal",
                  "mail-list", "mail-get", "mail-clear", "pg-settings", "pg-set", "pg-reset", "db-restart",
                  "modules", "mail-send", "mail-inbox", "update-list",
                  "next-update", "next-start", "next-stop", "next-state", "stop-service")
# the tuning's actions that read their values on the input (pg_tune())
TUNE_ACTIONS = ("pg-set", "pg-reset", "db-restart")
# a staging's caught mail (stacktemplates.MAIL_SERVICE, templates' compose.yaml):
# Mailpit's API, on the loopback of its container only; a message's id is
# Mailpit's (22 letters and digits); at most MAIL_LIMIT messages a time
MAIL_SERVICE = "mail"
MAIL_API = "http://127.0.0.1:8025/api/v1"
MAIL_ID = re.compile(r"[A-Za-z0-9]{8,40}")
MAIL_LIMIT = 500
MAIL_START = 100000
# a staging's incoming mail (UX-008 §5.4): mail-send reads a message as an
# outside sender writes it (JSON on the input, MAIL_INPUT bytes at most), the
# helper builds it and hands it to the mail service's SMTP, which keeps it
# and gives a message to a .test address to the inbox service's mailboxes.
# An address of one plain form (no name, no quote, no space), never starting
# with "-"; a Message-ID as Mailpit shows it, without its brackets
INBOX_SERVICE = "inbox"
MAIL_INPUT = 262144
ADDRESS = re.compile(r"[A-Za-z0-9][A-Za-z0-9._%+-]{0,63}@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                     r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+")
MESSAGE_ID = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~.@-]{1,250}")
MAIL_RECIPIENTS = 10
SUBJECT_MAX, BODY_MAX, NAME_MAX = 250, 100000, 80
# mail-inbox: the script odoo shell runs (odoo/mail_inbox.py), its mode and
# the staging's alias domain, a .test one, put in front of it by the helper
INBOX_SCRIPT = Path(__file__).parent / "odoo" / "mail_inbox.py"
INBOX_MODES = ("state", "on", "fetch")
TEST_DOMAIN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.test")
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
# the click of the tools (8.5) needs Python 3.10: an image with an older one
# (odoo:16.0, Debian 11, Python 3.9) finds click 8.1.8 first, in this folder
# of the tools (packaging/debian/requirements/tools-py39.txt)
TOOLS_PY39 = "py39"
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
    "update": ODOO_DB + CRONS_BEFORE + f'p={TOOLS_MOUNT}; '
                        "python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || "
                        f'{{ p={TOOLS_MOUNT}/{TOOLS_PY39}:$p; [ -d {TOOLS_MOUNT}/{TOOLS_PY39} ] || echo "WARNING: '
                        f"the Python of this image is older than 3.10 and OmniaCLI's tools have no "
                        f'{TOOLS_PY39}/ for it: install the package again" >&2; }}; export PYTHONPATH="$p"; '
                        f'python3 {TOOLS_MOUNT}/bin/click-odoo-update -d "$1"; ' + CRONS_AFTER,
    # no outgoing mail, no scheduled actions (Odoo 16 and later)
    "neutralize": ODOO_DB + 'exec odoo neutralize -d "$1"',
    "update-modules": ODOO_DB + CRONS_BEFORE + 'odoo -d "$1" -u "$2" --stop-after-init --no-http; ' + CRONS_AFTER,
    # modules of the catalog installed into an existing database (UX-008 §7.2), without demo data: $3 is
    # without_demo() of the image's Odoo
    "install-modules": ODOO_DB + CRONS_BEFORE + 'odoo -d "$1" -i "$2" --stop-after-init --no-http "$3"; '
                       + CRONS_AFTER,
    # Apps › Update the module list, as Odoo's button does: a fixed line, the database as $1
    "update-list": ODOO_DB + "echo 'print(\"omniacli: modules updated, added:\", "
                             "env[\"ir.module.module\"].update_list()); env.cr.commit()' | "
                             'odoo shell -d "$1" --no-http --log-level=warn',
    "pg-modules": 'exec psql -X -At -v ON_ERROR_STOP=1 -U "${POSTGRES_USER:-postgres}" -d "$1" -c "$2"',
    "psql": 'exec psql -U "${POSTGRES_USER:-postgres}" "$1"',
    # the report: its line, then pg_stat_statements' when the extension is there
    "pg-report": 'psql -X -At -U "${POSTGRES_USER:-postgres}" -d "$1" -c "$2" || exit 1; '
                 'if psql -X -At -U "${POSTGRES_USER:-postgres}" -d "$1" '
                 '-c "SELECT 1 FROM pg_extension WHERE extname = \'pg_stat_statements\'" | grep -q 1; '
                 'then psql -X -At -U "${POSTGRES_USER:-postgres}" -d "$1" -c "$3"; else echo null; fi',
    # the tuning's state (PG_SETTINGS), the database an argument
    "pg-settings": 'exec psql -X -At -v ON_ERROR_STOP=1 -U "${POSTGRES_USER:-postgres}" -d "$1" -c "$2"',
    # the SQL of the tuning (tune_sql()) on the input, never on a command line
    "pg-input": 'exec psql -X -At -q -v ON_ERROR_STOP=1 -U "${POSTGRES_USER:-postgres}" -d "$1"',
    # a query of the database stopped: its backend ended
    "pg-terminate": 'exec psql -X -At -U "${POSTGRES_USER:-postgres}" -d "$1" '
                    '-c "SELECT pg_terminate_backend($2) FROM pg_stat_activity WHERE pid = $2 '
                    'AND datname = current_database()"',
    # the terminal of the Odoo container: a login shell of the image's user,
    # in the repositories of addons (bash when the image has it), with the
    # database connection: odoo shell and psql typed there connect
    "terminal": ODOO_DB + 'cd /mnt/customer-addons 2>/dev/null || cd; '
                'if command -v bash >/dev/null; then exec bash -l; fi; exec sh -l',
    # odoo shell reads the script from its input; quiet, the answer is its last line
    "login": ODOO_DB + 'OMNIACLI_LOGIN="$2" exec odoo shell -d "$1" --no-http --log-level=critical',
    "setup": ODOO_DB + 'exec odoo shell -d "$1" --no-http --log-level=critical',
    # the options the image's Odoo knows: its python3 reads the script from its input
    "options": "exec python3 -",
    "pip-freeze": "exec python3 -m pip freeze --user --disable-pip-version-check",
    # a backup restored for a try: the zip from the input, the check in
    # OMNIACLI_CHECK; the database dropped on the way out, whatever happened
    "verify": ODOO_DB + 'd="${OMNIACLI_VERIFY:-omniacli_verify_}$(od -An -N4 -tx1 /dev/urandom | tr -d " \\n")"; '
                        'f="$(mktemp /tmp/omniacli-verify-XXXXXX.zip)"; '
                        'trap \'odoo db drop "$d" >&2 2>&1; rm -f "$f"\' EXIT; '
                        'cat > "$f"; odoo db load --neutralize "$d" "$f" >&2 || exit 1; '
                        'printf "%s\\n" "$OMNIACLI_CHECK" | odoo shell -d "$d" --no-http --log-level=critical',
    # the Python packages a repository of addons asks (its requirements.txt),
    # as the image's user in its home (/var/lib/odoo/.local, on the volume of
    # the instance's data: they stay when the container is made again)
    # a staging's caught mail, in the mail service's container (busybox: wget
    # reads, nc deletes, wget has no DELETE): the numbers and the id checked
    "mail-list": f'exec wget -q -T 20 -O - "{MAIL_API}/messages?start=$1&limit=$2"',
    "mail-get": f'exec wget -q -T 20 -O - "{MAIL_API}/message/$1"',
    "mail-clear": 'r="$(printf "DELETE /api/v1/messages HTTP/1.0\\r\\nHost: 127.0.0.1\\r\\nContent-Length: 0\\r\\n\\r\\n" '
                  '| nc -w 20 127.0.0.1 8025 | head -n 1)"; '
                  'case "$r" in "HTTP/1."?" 200"*) echo cleared;; '
                  '*) echo "the mail service answered: ${r:-nothing}" >&2; exit 1;; esac',
    # a message of an outside sender (mail_send()) on the input, to the
    # service's own SMTP: the envelope's sender, then its recipients
    "mail-send": 'f="$1"; shift; exec /mailpit sendmail -S 127.0.0.1:1025 -f "$f" "$@"',
    # odoo shell reads mail_inbox.py from its input; quiet, the answer is its last line
    "mail-inbox": ODOO_DB + 'exec odoo shell -d "$1" --no-http --log-level=critical',
    # an update without stopping: the changed modules of the new code (its addons path, $2), in a container of
    # its own, while the instance's Odoo keeps serving
    "next-update": ODOO_DB + CRONS_BEFORE + f'p={TOOLS_MOUNT}; '
                             "python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || "
                             f'p={TOOLS_MOUNT}/{TOOLS_PY39}:$p; export PYTHONPATH="$p"; '
                             f'python3 {TOOLS_MOUNT}/bin/click-odoo-update -d "$1" --addons-path "$2"; ' + CRONS_AFTER,
    "pip-requirements": 'f="/mnt/customer-addons/$1/requirements.txt"; if [ -f "$f" ]; then '
                        'exec python3 -m pip install --user --break-system-packages --disable-pip-version-check '
                        '--no-warn-script-location -r "$f"; else echo "$1 has no requirements.txt"; fi',
}
# a project on the machine's cluster (machinedb.py): its database actions
# run in its Odoo service as the environment's own role (the PG* Odoo has),
# never as the cluster's superuser: what they read is that database's
MACHINE_ODOO = "odoo"
for _name in ("pg-modules", "psql", "pg-report", "pg-terminate"):
    SCRIPTS["machine-" + _name] = ODOO_DB + SCRIPTS[_name].replace(' -U "${POSTGRES_USER:-postgres}"', "")
# the actions where Odoo drops and creates its database (CREATEDB, in them only)
CREATES_DATABASE = ("load", "verify")
_ENV_LINE = re.compile(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
# logs: a service's output with the time of each line, the last N lines or
# those since a time (UTC, as docker writes it)
# --ansi never: docker compose 2.26 starts each line with an escape (ESC[2K)
# otherwise, even without a terminal
LOGS = ["--ansi", "never", "logs", "--no-color", "--no-log-prefix", "--timestamps"]
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
VERIFY_SCRIPT = Path(__file__).parent / "odoo" / "verify_check.py"
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


def problems(project: Path, uid: int, groups: list[int], env_unread_ok: bool = False) -> list[str]:
    """Why the project is not safe from the user (uid, groups); [] when it is.
    Root is not checked: it may do all this anyway. env_unread_ok: asked by
    that user itself (the checks of a server's form), an .env it may not read
    (root's, mode 600, as the workspaces make it) is no problem: the user
    cannot have written it, and the helper, which reads it as root, judges
    what it sets at every action."""
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
    found += env_problems(project / ".env", env_unread_ok)
    if not any((project / name).is_file() for name in COMPOSE_FILES):
        found.append(f"{project} holds no {', '.join(COMPOSE_FILES)}")
    return found


def env_problems(env_file: Path, unread_ok: bool = False) -> list[str]:
    try:
        text = env_file.read_text(errors="replace")
    except FileNotFoundError:
        return []
    except PermissionError as exc:
        return [] if unread_ok else [f"{env_file}: {exc.strerror}"]
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
    # a container of a user of its own (the machine's cluster): as that user,
    # who alone reads its superuser's password
    if ids and ids[0] != "0":
        return ()
    if len(ids) > 1:
        return ("--user", "postgres")
    raise HelperError(f"{service_name} runs as root and has no user postgres: "
                      "psql is not opened, its \\! would run as root")


def on_cluster(project: Path) -> dict | None:
    """{"major", "role"} of a project on the machine's cluster, else None."""
    try:
        return machinedb.environment(project)
    except machinedb.ClusterError as exc:
        raise HelperError(str(exc)) from None


# the name of the database "service" of a project on a database of another
# machine (stacktemplates.REMOTE_DB_SERVICE: this module does not import it)
REMOTE_DB_SERVICE = "remote"


def on_remote(project: Path) -> dict | None:
    """{"host", "port"} of a project whose database is on another machine
    (AD-003: its .env has DB_REMOTE, DB_HOST and DB_PORT), else None."""
    try:
        text = (Path(project) / ".env").read_text(encoding="utf-8")
    except OSError:
        return None
    values = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    if values.get("DB_REMOTE", "").strip() != "1":
        return None
    return {"host": values.get("DB_HOST", "").strip(), "port": values.get("DB_PORT", "").strip()}


def on_linked(project: Path) -> bool:
    """A project whose database is on the cluster of a linked OmniaCLI: on
    another machine, and its instance's record (instance.json, root's)
    names the link that made it."""
    if on_remote(project) is None:
        return False
    try:
        meta = json.loads((Path(project).parent / "instance.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(meta, dict) and bool(meta.get("database_link"))


def db_target(project: Path, name: str, terminal: bool = False) -> tuple[str, str, tuple[str, ...]]:
    """(service, prefix of the script, exec options) a database action of
    the service name runs with: the project's database service as its
    postgres; on the machine's cluster (name machinedb.DB_SERVICE) its Odoo
    service, as the environment's role."""
    remote = on_remote(project)
    if on_cluster(project) is None and remote is None:
        svc = service(project, name)
        return svc, "", (() if terminal else ("-T",)) + psql_options(project, svc)
    expected = machinedb.DB_SERVICE if remote is None else REMOTE_DB_SERVICE
    if name != expected:
        where = "is on the machine's cluster" if remote is None else f"has its database on {remote['host']}"
        raise HelperError(f"{project} {where}: its database is {expected}, reached from its Odoo service")
    return service(project, MACHINE_ODOO), "machine-", () if terminal else ("-T",)


# the database service of a template's project: a restart of the instance leaves it running (up -d still makes it
# again when its own settings changed)
RESTART_KEEPS = ("db",)


_ODOO_VERSION = re.compile(r"Odoo Server (?:saas~)?(\d+)")
# the major version from which --without-demo is a boolean of new databases (odoo/tools/config.py of 19.0 and
# 20.0: "--without-demo", nargs '?'; another value is warned about, "since 19.0, invalid boolean value")
DEMO_BOOLEAN_FROM = 19


def without_demo(major: int | None) -> str:
    """The flag that installs modules without their demo data in the Odoo of
    that major version: before 19 --without-demo takes the modules (all, the
    database's new ones too); from 19 it is a boolean (a module installed in
    an existing database takes the demo data the database has). Unknown:
    the older one, which 19 and 20 still read (with a warning)."""
    return "--without-demo" if major is not None and major >= DEMO_BOOLEAN_FROM else "--without-demo=all"


def installs_modules(action: str, args: list[str]) -> bool:
    """The actions that install modules without their demo data: the flag of the image's Odoo (without_demo())."""
    return action == "init" or (action == "update" and args[2:3] == ["--install"])


def odoo_major(project: Path, service_name: str, running: bool) -> int | None:
    """The major version of the Odoo of the service's image (odoo --version):
    in its running container, or in a container of its own (a new database:
    the service is not started yet). None when it does not say."""
    cmd = (["exec", "-T", service_name, "odoo", "--version"] if running
           else ["run", "--rm", "--no-deps", "-T", "--entrypoint", "odoo", service_name, "--version"])
    try:
        done = subprocess.run(compose(project, *cmd), env=ENV, cwd=project, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    found = _ODOO_VERSION.search(done.stdout or "")
    return int(found.group(1)) if done.returncode == 0 and found else None


def commands(project: Path, action: str, args: list[str], major: int | None = None) -> list[list[str]]:
    """The docker compose commands of an action, its arguments checked;
    major: the Odoo of the image (odoo_major()), for the flag of an action
    that installs modules (installs_modules())."""
    if action in ACTIONS:
        if args:
            raise HelperError(f"{action} takes no arguments")
        if action == "restart":
            # Restart of an instance is its Odoo's (and its mail's): its PostgreSQL restarting with it cost its
            # connections and 2-3 s more for nothing, and every change of its reads restarted it (22/11/2026)
            listed = services(project)
            kept = [s for s in listed if s not in RESTART_KEEPS]
            if kept and len(kept) < len(listed):
                return [["up", "-d"], ["restart", *kept]]
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
        if len(args) == 4 and args[2] in ("--modules", "--install"):
            modules = args[3].split(",")
            if not all(MODULE.fullmatch(m) for m in modules):
                raise HelperError(f"{args[3]!r} is not a list of module names")
            if args[2] == "--modules":
                return [_script(service(project, args[0]), "update-modules", _database(args[1]), ",".join(modules))]
            return [_script(service(project, args[0]), "install-modules", _database(args[1]), ",".join(modules),
                            without_demo(major))]
        raise HelperError("usage: update <service> <database> [--modules a,b | --install a,b]")
    if action == "next-update":
        # next-update <odoo service> <database>: click-odoo-update with the addons path of the new code
        # (next_addons_path), in a container of its own with the tools mounted
        if len(args) != 2:
            raise HelperError("usage: next-update <service> <database>")
        if not (TOOLS / "bin" / "click-odoo-update").is_file():
            raise HelperError(f"{TOOLS} holds no click-odoo-update: install OmniaCLI's tools")
        return [["run", "--rm", "--no-deps", "-T", "-v", f"{TOOLS}:{TOOLS_MOUNT}:ro",
                 "--entrypoint", "sh", service(project, args[0]), "-c", SCRIPTS["next-update"], "sh",
                 _database(args[1]), next_addons_path(project)]]
    if action == "update-list":
        # update-list <odoo service> <database>: the fixed line above, in the running service
        if len(args) != 2:
            raise HelperError("usage: update-list <service> <database>")
        return [_script(service(project, args[0]), "update-list", _database(args[1]))]
    if action == "modules":
        # modules <database service> <database>: the fixed query above
        if len(args) != 2:
            raise HelperError("usage: modules <service> <database>")
        name, prefix, options = db_target(project, args[0])
        return [_script(name, prefix + "pg-modules", _database(args[1]), MODULES_QUERY, options=options)]
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
                 "-i", ",".join(modules), "--stop-after-init", without_demo(major), "--no-http",
                 *(["--load-language", args[3]] if args[3:] else [])]]
    if action == "login":
        # login <odoo service> <database> users|<user id>: in the running
        # service, as its user (Odoo's sessions are its files)
        if len(args) != 3 or not LOGIN_REQUEST.fullmatch(args[2]):
            raise HelperError("usage: login <service> <database> users|<user id>")
        return [_script(service(project, args[0]), "login", _database(args[1]), args[2])]
    if action == "pip-freeze":
        if len(args) != 1:
            raise HelperError("usage: pip-freeze <service>")
        return [_script(service(project, args[0]), "pip-freeze")]
    if action == "verify":
        # verify <odoo service>: in a container of its own (the service may
        # be stopped), the check given in its environment
        if len(args) != 1:
            raise HelperError("usage: verify <service>")
        env = on_cluster(project)
        # on the machine's cluster: a name only its own pg_hba line reaches
        prefix = ["-e", "OMNIACLI_VERIFY=" + machinedb.verify_prefix(env["role"])] if env else []
        return [["run", "--rm", "-T", "-e", "OMNIACLI_CHECK=" + VERIFY_SCRIPT.read_text(), *prefix,
                 "--entrypoint", "sh", service(project, args[0]), "-c", SCRIPTS["verify"], "sh"]]
    if action == "options":
        # options <odoo service>: in the running service
        if len(args) != 1:
            raise HelperError("usage: options <service>")
        return [_script(service(project, args[0]), "options")]
    if action == "pip-requirements":
        # pip-requirements <odoo service> <repository>: in a container of its own, the service maybe stopped (an
        # update stops it before its code moves); the packages go to the volume of its data, which it reads
        if len(args) != 2 or not REPOSITORY.fullmatch(args[1]) or ".." in args[1]:
            raise HelperError("usage: pip-requirements <service> <repository>")
        return [["run", "--rm", "--no-deps", "-T", "--entrypoint", "sh", service(project, args[0]), "-c",
                 SCRIPTS["pip-requirements"], "sh", args[1]]]
    if action == "stop-service":
        # stop-service <service>: that one stopped, the others kept running (the database of an update)
        if len(args) != 1:
            raise HelperError("usage: stop-service <service>")
        return [["stop", service(project, args[0])]]
    if action == "setup":
        # setup <odoo service> <database>: the values on the helper's input
        if len(args) != 2:
            raise HelperError("usage: setup <service> <database> (the values, JSON, on the input)")
        return [_script(service(project, args[0]), "setup", _database(args[1]))]
    if action == "psql":
        # psql <database service> <database>: on a terminal, the web shell's
        if len(args) != 2:
            raise HelperError("usage: psql <service> <database>")
        name, prefix, options = db_target(project, args[0], terminal=True)
        return [_script(name, prefix + "psql", _database(args[1]), options=options)]
    if action == "neutralize":
        # neutralize <odoo service> <database>: in a container of its own
        if len(args) != 2:
            raise HelperError("usage: neutralize <service> <database>")
        return [["run", "--rm", "-T", "--entrypoint", "sh", service(project, args[0]), "-c", SCRIPTS["neutralize"],
                 "sh", _database(args[1])]]
    if action == "pg-report":
        # pg-report <database service> <database>: the fixed queries above
        if len(args) != 2:
            raise HelperError("usage: pg-report <service> <database>")
        name, prefix, options = db_target(project, args[0])
        return [_script(name, prefix + "pg-report", _database(args[1]), PG_REPORT, PG_STATEMENTS, options=options)]
    if action == "pg-settings":
        # pg-settings <database service> <database>: the fixed query above
        if len(args) != 2:
            raise HelperError("usage: pg-settings <service> <database>")
        if on_cluster(project) is not None:
            raise HelperError(f"{project} is on the machine's cluster: it is tuned as the cluster, "
                              "in the Database tab of the machine")
        if on_remote(project) is not None:
            raise HelperError(f"{project} has its database on another machine: it is tuned there, not from here")
        name = service(project, args[0])
        return [_script(name, "pg-settings", _database(args[1]), PG_SETTINGS,
                        options=("-T", *psql_options(project, name)))]
    if action in TUNE_ACTIONS:
        raise HelperError(f"{action} reads its values on the input: compose-helper <project> {action} <service> <database>")
    if action == "pg-terminate":
        if len(args) != 3 or not PID.fullmatch(args[2]):
            raise HelperError("usage: pg-terminate <service> <database> <pid>")
        name, prefix, options = db_target(project, args[0])
        return [_script(name, prefix + "pg-terminate", _database(args[1]), args[2], options=options)]
    if action == "mail-list":
        # mail-list <start> <limit>: the caught messages, the newest first
        if len(args) != 2 or not all(a.isdigit() and len(a) <= 6 for a in args) \
                or int(args[0]) > MAIL_START or not 1 <= int(args[1]) <= MAIL_LIMIT:
            raise HelperError(f"usage: mail-list <start, 0 to {MAIL_START}> <limit, 1 to {MAIL_LIMIT}>")
        return [_script(service(project, MAIL_SERVICE), "mail-list", str(int(args[0])), str(int(args[1])))]
    if action == "mail-get":
        if len(args) != 1 or not MAIL_ID.fullmatch(args[0]):
            raise HelperError("usage: mail-get <message id, 8 to 40 letters and digits>")
        return [_script(service(project, MAIL_SERVICE), "mail-get", args[0])]
    if action == "mail-clear":
        if args:
            raise HelperError("mail-clear takes no arguments")
        return [_script(service(project, MAIL_SERVICE), "mail-clear")]
    if action == "mail-send":
        raise HelperError("mail-send reads its message on the input: compose-helper <project> mail-send")
    if action == "mail-inbox":
        # mail-inbox <odoo service> <database>: the mode and domain on the helper's input
        if len(args) != 2:
            raise HelperError("usage: mail-inbox <service> <database> (mode and domain, JSON, on the input)")
        service(project, INBOX_SERVICE)
        return [_script(service(project, args[0]), "mail-inbox", _database(args[1]))]
    if action == "terminal":
        # terminal <odoo service>: on the caller's terminal, never as root
        if len(args) != 1:
            raise HelperError("usage: terminal <service>")
        name = service(project, args[0])
        return [_script(name, "terminal", options=terminal_options(project, name))]
    if action == "logs":
        name, options, _cut = logs_options(project, args)
        return [[*LOGS, *options, name]]
    raise HelperError(f"unknown action {action!r}: {', '.join([*ACTIONS, *WITH_ARGUMENTS])}")


LOGS_USAGE = "usage: logs <service> --tail <lines> | --since <time> [--until <time>] [--first|--last <lines>]"


def logs_options(project: Path, args: list[str]) -> tuple[str, list[str], tuple[str, int] | None]:
    """logs <service> --tail <N> | --since <time> [--until <time>] [--first|--last <N>]:
    (the service, the options of docker compose logs, the cut the helper
    makes itself: ("first" or "last", N) or None). docker's own --tail is
    taken on the whole log before --since and --until, so the last N lines
    of a span in the past would be none: the helper counts them."""
    if len(args) < 3 or len(args) % 2 == 0:
        raise HelperError(LOGS_USAGE)
    given: dict[str, str] = {}
    for flag, value in zip(args[1::2], args[2::2]):
        if flag not in ("--tail", "--since", "--until", "--first", "--last") or flag in given:
            raise HelperError(LOGS_USAGE)
        given[flag] = value
    for flag in ("--tail", "--first", "--last"):
        if flag in given and not (given[flag].isdigit() and 1 <= int(given[flag]) <= MAX_TAIL):
            raise HelperError(f"{flag} takes 1 to {MAX_TAIL} lines")
    for flag in ("--since", "--until"):
        if flag in given and not SINCE.fullmatch(given[flag]):
            raise HelperError(f"{flag} takes a UTC time, as 2026-10-28T16:48:50.380582551Z")
    if "--tail" in given and len(given) > 1:
        raise HelperError("--tail goes alone: the last lines of the whole log")
    if "--tail" not in given and "--since" not in given:
        raise HelperError(LOGS_USAGE)
    if "--first" in given and "--last" in given:
        raise HelperError("--first or --last, not both")
    options = [part for flag in ("--tail", "--since", "--until") if flag in given for part in (flag, given[flag])]
    cut = next(((flag[2:], int(given[flag])) for flag in ("--first", "--last") if flag in given), None)
    return service(project, args[0]), options, cut


def logs_cut(project: Path, cmd: list[str], cut: tuple[str, int]) -> int:
    """docker compose logs, of which only the first or the last N lines are
    written, then a last line "#omniacli-logs total=<lines> kept=<N>"
    (total "more" when the first N were kept and the rest not read)."""
    keep, count = cut
    proc = subprocess.Popen(compose(project, *cmd), env=ENV, cwd=project, stdout=subprocess.PIPE)
    kept: collections.deque[bytes] = collections.deque(maxlen=count if keep == "last" else None)
    total: int | str = 0
    for line in proc.stdout:
        if keep == "first" and len(kept) >= count:
            total = "more"
            proc.kill()
            break
        kept.append(line)
        total += 1
    proc.stdout.close()
    code = proc.wait()
    out = sys.stdout.buffer
    for line in kept:
        out.write(line)
    out.write(f"#omniacli-logs total={total} kept={len(kept)}\n".encode())
    out.flush()
    return 0 if total == "more" else code


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


# --- an update without stopping (zerodowntime.py) --------------------------
# The new code is OmniaCLI's copy of each repository that moves, at its new
# commit, in addons/NEXT_FOLDER/<repository> (the caller's folder, mounted as
# the repositories are, read only): a container of its own of the Odoo
# service runs it on other ports, against the same database, while the
# instance's keeps serving; nginx is switched to it (workspace-helper
# upstream), the instance's restarted with the code moved, nginx back, this
# container removed. A name never starting with a dot is a repository's
# (REPOSITORY), so the folder is never one of them.
NEXT_FOLDER = ".omniacli-next"
CUSTOMER_ADDONS = "/mnt/customer-addons"
PROJECT_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")


def _odoo_conf(project: Path) -> dict[str, str]:
    """The options of the project's config/odoo.conf."""
    found = {}
    try:
        text = (project / "config" / "odoo.conf").read_text(encoding="utf-8")
    except OSError as exc:
        raise HelperError(f"{project}/config/odoo.conf: {exc.strerror}") from None
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and not key.lstrip().startswith((";", "#", "[")):
            found[key.strip()] = value.strip()
    return found


def next_addons_path(project: Path) -> str:
    """The addons path of config/odoo.conf, each repository whose new code is
    in addons/NEXT_FOLDER/<repository> taken from there; refused when none is."""
    folder = project / "addons" / NEXT_FOLDER
    if folder.is_symlink() or not folder.is_dir():
        raise HelperError(f"{folder} is not there: no new code to run")
    entries, moved = [], 0
    for entry in _odoo_conf(project).get("addons_path", "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        if entry.startswith(CUSTOMER_ADDONS + "/"):
            repo, _, rest = entry[len(CUSTOMER_ADDONS) + 1:].partition("/")
            copy = folder / repo
            if REPOSITORY.fullmatch(repo) and not copy.is_symlink() and copy.is_dir():
                entry = f"{CUSTOMER_ADDONS}/{NEXT_FOLDER}/{repo}" + (f"/{rest}" if rest else "")
                moved += 1
        entries.append(entry)
    if not moved:
        raise HelperError(f"no repository of the addons path has its new code in {folder}")
    return ",".join(entries)


def next_name(project: Path) -> str:
    """The container of the new code: <project's name>-next."""
    try:
        text = (project / ".env").read_text(encoding="utf-8")
    except OSError as exc:
        raise HelperError(f"{project}/.env: {exc.strerror}") from None
    values = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    name = values.get("PROJECT", "").strip()
    if not PROJECT_NAME.fullmatch(name):
        raise HelperError(f"{project}/.env names no project (PROJECT)")
    return f"{name}-next"


def _free_port(taken: tuple[int, ...] = ()) -> int:
    """A port of 127.0.0.1 nothing listens on, the kernel's choice."""
    for _ in range(20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        if port >= 1024 and port not in taken:
            return port
    raise HelperError("no free port on 127.0.0.1")


def _next_container(project: Path) -> dict | None:
    """{"name", "running", "http_port", "websocket_port"} of the container of
    the new code, None when there is none."""
    name = next_name(project)
    done = subprocess.run([compose(project)[0], "inspect", "--format",
                           "{{json .State.Running}} {{json .HostConfig.PortBindings}} "
                           "{{index .Config.Labels \"com.docker.compose.project.working_dir\"}}", name],
                          env=ENV, cwd=project, capture_output=True, text=True)
    if done.returncode:
        return None
    running, _, rest = done.stdout.strip().partition(" ")
    bindings, _, folder = rest.partition(" ")
    if folder.strip() != str(project):
        raise HelperError(f"a container {name} is there, not of {project}: it is not touched")
    try:
        ports = json.loads(bindings) or {}
    except ValueError:
        ports = {}

    def published(inside: str) -> int | None:
        for row in ports.get(f"{inside}/tcp") or []:
            value = str(row.get("HostPort") or "")
            if value.isdigit():
                return int(value)
        return None

    return {"name": name, "running": running == "true", "http_port": published("8069"),
            "websocket_port": published("8072")}


def next_action(project: Path, action: str, args: list[str]) -> int:
    """next-start <odoo service>: the container of the new code started
    (docker compose run -d of the service: its environment, volumes, network
    and slice), the addons path of next_addons_path, no cron thread (the
    instance's Odoo runs them), on a free port of 127.0.0.1 (and one for
    the websocket with workers), as JSON; refused when one is there.
    next-stop: that container removed (none: said). next-state: it as JSON
    (null: none)."""
    if action == "next-start":
        if len(args) != 1:
            raise HelperError("usage: next-start <service>")
        name = service(project, args[0])
        if _next_container(project) is not None:
            raise HelperError(f"a container {next_name(project)} is there already: next-stop first")
        path = next_addons_path(project)
        workers = _odoo_conf(project).get("workers", "0")
        http = _free_port()
        websocket = _free_port((http,)) if workers.isdigit() and int(workers) > 0 else None
        cmd = ["run", "-d", "--no-deps", "--name", next_name(project), "-p", f"127.0.0.1:{http}:8069",
               *(["-p", f"127.0.0.1:{websocket}:8072"] if websocket else []),
               name, "odoo", f"--addons-path={path}", "--max-cron-threads=0"]
        done = subprocess.run(compose(project, *cmd), env=ENV, cwd=project, capture_output=True, text=True)
        if done.returncode:
            raise HelperError(f"docker compose run failed: {(done.stderr or done.stdout).strip()[-400:]}")
        print(json.dumps({"container": next_name(project), "http_port": http, "websocket_port": websocket,
                          "addons_path": path}))
        return 0
    if args:
        raise HelperError(f"{action} takes no arguments")
    found = _next_container(project)
    if action == "next-state":
        print(json.dumps(found))
        return 0
    if found is None:
        print(json.dumps({"removed": None}))
        return 0
    done = subprocess.run([compose(project)[0], "rm", "-f", found["name"]], env=ENV, cwd=project,
                          capture_output=True, text=True)
    if done.returncode:
        raise HelperError(f"docker rm {found['name']} failed: {done.stderr.strip()[-300:]}")
    print(json.dumps({"removed": found["name"]}))
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


def inbox_input(raw: bytes) -> bytes:
    """What odoo shell reads for mail-inbox: {"mode", "domain"} checked (a
    mode of INBOX_MODES, a .test domain of one label) as a Python literal,
    then the script."""
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        raise HelperError("mail-inbox: the input is no JSON") from None
    if not isinstance(data, dict) or set(data) != {"mode", "domain"}:
        raise HelperError("mail-inbox: the input takes mode and domain")
    if data["mode"] not in INBOX_MODES:
        raise HelperError(f"mail-inbox: the mode is one of {', '.join(INBOX_MODES)}")
    if not isinstance(data["domain"], str) or not TEST_DOMAIN.fullmatch(data["domain"]):
        raise HelperError(f"mail-inbox: {data['domain']!r} is not a domain <name>.test (never a real one)")
    clean = {"mode": data["mode"], "domain": data["domain"]}
    return f"OMNIACLI_MAIL = {clean!r}\n".encode() + INBOX_SCRIPT.read_bytes()


def _one_line(value, what: str, most: int, empty: bool = True) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str) or len(value) > most or any(ord(c) < 32 or c == "\x7f" for c in value) \
            or (not empty and not value.strip()):
        raise HelperError(f"mail-send: {what} is one line of at most {most} characters"
                          + ("" if empty else ", not empty"))
    return value.strip()


def _address(value, what: str) -> str:
    if not isinstance(value, str) or not ADDRESS.fullmatch(value):
        raise HelperError(f"mail-send: {what} {value!r} is not an address as name@example.com")
    return value


def mail_message(raw: bytes) -> tuple[str, list[str], bytes]:
    """(envelope sender, recipients, the message) of what an outside sender
    writes, JSON: {"from", "name"?, "to": [..], "subject", "body",
    "in_reply_to"?}; every value checked, the message built here (headers
    folded and encoded by Python's email), never text of the caller's."""
    if len(raw) > MAIL_INPUT:
        raise HelperError(f"mail-send: the message is larger than {MAIL_INPUT} bytes")
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        raise HelperError("mail-send: the input is no JSON") from None
    if not isinstance(data, dict) or not {"from", "to", "subject", "body"} <= set(data) \
            or set(data) - {"from", "name", "to", "subject", "body", "in_reply_to"}:
        raise HelperError("mail-send: the input takes from, to, subject, body, and name and in_reply_to")
    sender = _address(data["from"], "the sender")
    to = data["to"]
    if not isinstance(to, list) or not 1 <= len(to) <= MAIL_RECIPIENTS:
        raise HelperError(f"mail-send: to is a list of 1 to {MAIL_RECIPIENTS} addresses")
    recipients = list(dict.fromkeys(_address(a, "the recipient") for a in to))
    name = _one_line(data.get("name"), "the name", NAME_MAX)
    subject = _one_line(data["subject"], "the subject", SUBJECT_MAX, empty=False)
    body = data["body"]
    if not isinstance(body, str) or len(body) > BODY_MAX or "\x00" in body:
        raise HelperError(f"mail-send: the body is a text of at most {BODY_MAX} characters")
    reply = data.get("in_reply_to") or ""
    if reply and (not isinstance(reply, str) or not MESSAGE_ID.fullmatch(reply.strip("<>"))):
        raise HelperError("mail-send: in_reply_to is a Message-ID")
    message = email.message.EmailMessage(policy=email.policy.SMTP)
    message["From"] = email.utils.formataddr((name, sender)) if name else sender
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    message["Date"] = email.utils.formatdate(localtime=True)
    message["Message-ID"] = email.utils.make_msgid(domain=sender.rpartition("@")[2])
    if reply:
        # Odoo finds the record of a reply by these (mail.thread message_route)
        message["In-Reply-To"] = f"<{reply.strip('<>')}>"
        message["References"] = f"<{reply.strip('<>')}>"
    text = body.replace("\r\n", "\n")
    message.set_content(text)
    # and as HTML, as a mail program sends it: Odoo shows a text alone in a
    # <pre>, and folds the quoted lines (> ...) of a blockquote
    message.add_alternative(_html_of(text), subtype="html")
    return sender, recipients, message.as_bytes()


def _html_of(text: str) -> str:
    """The text as HTML, escaped: its lines with <br>, the lines quoted with
    > in a blockquote."""
    parts, quoted = [], []

    def flush():
        if quoted:
            parts.append("<blockquote>" + "<br>".join(quoted) + "</blockquote>")
            quoted.clear()
    for line in text.split("\n"):
        if line.startswith(">"):
            quoted.append(html.escape(line[1:].removeprefix(" ")))
            continue
        flush()
        parts.append(html.escape(line) + "<br>")
    flush()
    return "<div>" + "".join(parts) + "</div>"


def mail_send(project: Path, raw: bytes) -> tuple[list[str], bytes]:
    """The command of mail-send and its input: the message to the mail
    service, in a staging whose mailboxes Odoo reads (the inbox service)."""
    sender, recipients, message = mail_message(raw)
    service(project, INBOX_SERVICE)
    return _script(service(project, MAIL_SERVICE), "mail-send", sender, *recipients), message


def tune_input(action: str, raw: bytes) -> tuple[dict[str, str], list[str]]:
    """(values to set, names to reset) of a tuning action's input (pg-set:
    {name: value}; pg-reset: [names]; db-restart: {"set": ..., "reset": ...}),
    checked: names of TUNED only, never a hidden one, each value of its form."""
    try:
        data = json.loads(raw or b"null")
    except ValueError:
        raise HelperError(f"{action}: the input is no JSON") from None
    if action == "pg-set":
        data = {"set": data}
    elif action == "pg-reset":
        data = {"reset": data}
    elif data is None:
        data = {}
    if not isinstance(data, dict) or set(data) - {"set", "reset"}:
        raise HelperError(f"{action}: the input takes set and reset only")
    values, names = data.get("set") or {}, data.get("reset") or []
    if not isinstance(values, dict) or not isinstance(names, list):
        raise HelperError(f"{action}: set is {{name: value}}, reset a list of names")
    for name in [*values, *names]:
        if not isinstance(name, str) or hidden(name) or name not in TUNED:
            raise HelperError(f"{action}: {name!r} is not a parameter the tuning touches ({', '.join(TUNED)})")
    clean = {}
    for name, value in values.items():
        if not isinstance(value, str) or not TUNED_VALUE[TUNED[name]].fullmatch(value):
            raise HelperError(f"{action}: {value!r} is not a value of {name}")
        clean[name] = value
    reset = list(dict.fromkeys(names))
    if set(clean) & set(reset):
        raise HelperError(f"{action}: a parameter is both set and reset")
    if action != "db-restart" and not (clean or reset):
        raise HelperError(f"{action}: no parameter given")
    return clean, reset


def check_libraries(value: str, loaded: str) -> None:
    """A tuning loads no library but pg_stat_statements and those loaded now."""
    now = {part.strip() for part in loaded.split(",") if part.strip()}
    extra = [lib for lib in value.split(",") if lib and lib != STATEMENTS_LIBRARY and lib not in now]
    if extra:
        raise HelperError(f"shared_preload_libraries: {', '.join(extra)} is not loaded now, "
                          f"and the tuning adds {STATEMENTS_LIBRARY} only")


def tune_sql(values: dict[str, str], names: list[str]) -> str:
    """The ALTER SYSTEM statements of checked values (tune_input()): one
    parameter each, never RESET ALL."""
    lines = []
    for name, value in values.items():
        assert name in TUNED and TUNED_VALUE[TUNED[name]].fullmatch(value), name
        lines.append(f"ALTER SYSTEM SET {name} = '{value}';")
    for name in names:
        assert name in TUNED, name
        lines.append(f"ALTER SYSTEM RESET {name};")
    return "".join(line + "\n" for line in lines)


# after a restart: the statistics of the queries readable when their library
# is loaded, the extension gone when it is not (reading it would fail, and
# with it Monitoring › Database)
STATEMENTS_SQL = """DO $$ BEGIN
 IF current_setting('shared_preload_libraries') ~ '(^|[ ,])pg_stat_statements([ ,]|$)' THEN
  CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
 ELSE
  DROP EXTENSION IF EXISTS pg_stat_statements;
 END IF;
END $$;
SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements');
"""
# postgresql.auto.conf as it is, to be put back as it was (base64: any byte)
AUTO_CONF_SQL = "SELECT encode(convert_to(pg_read_file('postgresql.auto.conf'), 'UTF8'), 'base64');\n"
# a one-off container of the database service, stopped, writes the file back
PUT_BACK = 'cat > "${PGDATA:-/var/lib/postgresql/data}/postgresql.auto.conf"'


def _run(project: Path, *args: str, input: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(compose(project, *args), env=ENV, cwd=project, capture_output=True, text=True, input=input)


def _sql(project: Path, name: str, options: tuple[str, ...], database: str, sql: str) -> str:
    """The output of SQL given on psql's input in the running service."""
    done = _run(project, *_script(name, "pg-input", database, options=("-T", *options)), input=sql)
    if done.returncode:
        raise HelperError(f"PostgreSQL refused: {(done.stderr or done.stdout).strip()[-400:]}")
    return done.stdout


def _answers(project: Path, name: str, timeout: float) -> str | None:
    """None once the database answers; else why not (a container that
    stops or restarts in a loop is said at once, with its log). The
    machine's cluster listens on its socket only."""
    end = time.monotonic() + timeout
    host = machinedb.SOCKET_IN_DB if machinedb.is_cluster(project) else "127.0.0.1"
    while True:
        if _run(project, "exec", "-T", name, "pg_isready", "-q", "-h", host).returncode == 0:
            return None
        state = _run(project, "ps", "-a", "--format", "{{.State}}", name).stdout.strip()
        if state in ("restarting", "exited", "dead"):
            log = _run(project, "--ansi", "never", "logs", "--no-log-prefix", "--tail", "20", name).stdout
            fatal = [line.strip() for line in log.splitlines() if "FATAL" in line or "PANIC" in line]
            return f"the container is {state}" + (f": {fatal[-1][-300:]}" if fatal else "")
        if time.monotonic() > end:
            return f"no answer within {timeout:.0f} s"
        time.sleep(2)


def db_restart(project: Path, name: str, options: tuple[str, ...], database: str,
               values: dict[str, str], names: list[str]) -> int:
    """The values written, CHECKPOINT, a fast stop (the image's stop signal
    is SIGINT), a start; RESTART_TIMEOUT s to answer, else the
    postgresql.auto.conf of before put back and the database started again."""
    started = time.monotonic()
    before = base64.b64decode("".join(_sql(project, name, options, database, AUTO_CONF_SQL).split()))
    if values or names:
        _sql(project, name, options, database, tune_sql(values, names))
        print(f"written for the restart: {', '.join([*values, *names])}", flush=True)
    _sql(project, name, options, database, "CHECKPOINT;\n")
    stopping = time.monotonic()
    for step in (("stop", "-t", str(STOP_TIMEOUT), name), ("start", name)):
        done = _run(project, *step)
        if done.returncode:
            raise HelperError(f"docker compose {step[0]} {name} failed: {done.stderr.strip()[-300:]}")
    problem = _answers(project, name, RESTART_TIMEOUT)
    rolled_back = problem is not None
    if rolled_back:
        print(f"the database did not come back ({problem}): postgresql.auto.conf of before put back", flush=True)
        _run(project, "stop", "-t", str(STOP_TIMEOUT), name)
        done = _run(project, "run", "--rm", "--no-deps", "-T", *(options or ("--user", "postgres")),
                    "--entrypoint", "sh", name, "-c", PUT_BACK, input=before.decode("utf-8"))
        if done.returncode:
            raise HelperError(f"postgresql.auto.conf of before could not be put back: {done.stderr.strip()[-300:]}")
        _run(project, "start", name)
        again = _answers(project, name, RESTART_TIMEOUT)
        if again is not None:
            raise HelperError(f"the database did not come back ({problem}), "
                              f"nor with the values of before ({again})")
    statements = _sql(project, name, options, database, STATEMENTS_SQL).split()[-1:] == ["t"]
    print(json.dumps({"seconds": round(time.monotonic() - stopping, 1), "total": round(time.monotonic() - started, 1),
                      "rolled_back": rolled_back, "reason": problem, "statements_on": statements}), flush=True)
    return 3 if rolled_back else 0


def pg_tune(project: Path, action: str, args: list[str], raw: bytes) -> int:
    """pg-set, pg-reset, db-restart <database service> <database>, the values on the input."""
    if len(args) != 2:
        raise HelperError(f"usage: {action} <service> <database> (the values, JSON, on the input)")
    values, names = tune_input(action, raw)
    if on_cluster(project) is not None:
        raise HelperError(f"{project} is on the machine's cluster: it is tuned as the cluster, "
                          "in the Database tab of the machine")
    if on_remote(project) is not None:
        raise HelperError(f"{project} has its database on another machine: it is tuned there, not from here")
    name, database = service(project, args[0]), _database(args[1])
    options = psql_options(project, name)
    if "shared_preload_libraries" in values:
        check_libraries(values["shared_preload_libraries"],
                        _sql(project, name, options, database, "SHOW shared_preload_libraries;\n").strip())
    if action == "db-restart":
        code = db_restart(project, name, options, database, values, names)
        if machinedb.is_cluster(project):
            # the statistics of the queries in the database of each environment
            try:
                machinedb.sync_statements(project)
            except machinedb.ClusterError as exc:
                print(f"WARNING: the statistics of the queries of the environments: {exc}", flush=True)
        return code
    _sql(project, name, options, database, tune_sql(values, names) + "SELECT pg_reload_conf();\n")
    print(json.dumps({"set": sorted(values), "reset": names}))
    return 0


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
    if action in CREATES_DATABASE and on_linked(project):
        # Odoo drops the database and makes it again: there its role makes none, the database would be lost
        raise HelperError(f"{project} has its database on the cluster of a linked machine, where its role makes no "
                          f"database: {action} would drop it and not make it again")
    if action == "link-filestore":
        return link_filestore(project, args, uid, groups)
    if action == "image":
        return image_state(project, args)
    if action in ("next-start", "next-stop", "next-state"):
        return next_action(project, action, args)
    if action in TUNE_ACTIONS:
        return pg_tune(project, action, args, sys.stdin.buffer.read(TUNE_INPUT))
    # login: the script on the input of odoo shell, the helper's own file;
    # the other actions keep the caller's input (load reads its zip there)
    extra = {"input": LOGIN_SCRIPT.read_bytes()} if action == "login" else {}
    if action == "options":
        extra = {"input": OPTIONS_SCRIPT.read_bytes()}
    if action == "setup":
        extra = {"input": setup_input(sys.stdin.buffer.read(65536))}
    if action == "mail-inbox":
        extra = {"input": inbox_input(sys.stdin.buffer.read(4096))}
    if action == "mail-send":
        if args:
            raise HelperError("mail-send takes no arguments: the message, JSON, on the input")
        cmd, message = mail_send(project, sys.stdin.buffer.read(MAIL_INPUT + 1))
        return subprocess.run(compose(project, *cmd), env=ENV, cwd=project, input=message).returncode
    if action == "logs":
        name, options, cut = logs_options(project, args)
        if cut:
            return logs_cut(project, [*LOGS, *options, name], cut)
    cmds = commands(project, action, args)
    if installs_modules(action, args):
        # its arguments checked first: the flag without demo data is the image's Odoo's
        cmds = commands(project, action, args, odoo_major(project, args[0], running=action != "init"))
    env = on_cluster(project) if action in (*CREATES_DATABASE, "init") else None
    if env is None:
        return _each(project, cmds, extra)
    return on_machine(project, env, action, args, cmds, extra)


def _each(project: Path, cmds: list[list[str]], extra: dict) -> int:
    for cmd in cmds:
        code = subprocess.run(compose(project, *cmd), env=ENV, cwd=project, **extra).returncode
        if code:
            return code
    return 0


def on_machine(project: Path, env: dict, action: str, args: list[str], cmds: list[list[str]], extra: dict) -> int:
    """An action on the machine's cluster that creates a database: CREATEDB
    for the environment's role during it only (AD-002), and the database
    closed after (its role's, no CONNECT for PUBLIC)."""
    try:
        if action in CREATES_DATABASE:
            machinedb.allow_createdb(env["major"], env["role"], True)
        try:
            code = _each(project, cmds, extra)
        finally:
            if action in CREATES_DATABASE:
                machinedb.allow_createdb(env["major"], env["role"], False)
        if code == 0 and action in ("load", "init"):
            machinedb.close(machinedb.project_of(env["major"]), env["role"], _database(args[1]))
        return code
    except machinedb.ClusterError as exc:
        raise HelperError(str(exc)) from None


def main(argv: list[str]) -> int:
    try:
        return run(argv)
    except (HelperError, machinedb.ClusterError) as exc:
        print(f"compose-helper: {exc}", file=sys.stderr)
        return 2
