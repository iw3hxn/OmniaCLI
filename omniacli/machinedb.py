# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The machine's PostgreSQL cluster (docs/adr/postgresql-della-macchina.md,
AD-001, AD-002, AD-007; steps 3, 4 and 6 of §9), root's side: what the
root helpers (workspace-helper machine-db-*, compose-helper) run.

One cluster per PostgreSQL major (MAJORS), a compose project of root's in
the storage of the workspaces: <storage>/.machine-db/pg<major>, its data in
data/pgdata there. It listens on no port and on no network (network_mode
none): its Unix socket is in run/ of the project, which the instances on it
mount read only on SOCKET_IN_ODOO. Its process runs as USER, a system user
of the machine of its own (never the postgres of the image, whose uid is
some other user's on the machine), in its own systemd slice (SLICE). Its
superuser's password is in secret/, root's and USER's only; only the root
helpers use it. pg_hba.conf is conf/pg_hba.conf, written here from the
environments the cluster serves (environments.json): a line per
environment, its database, postgres (where Odoo creates and drops) and its
own throwaway databases of a backup's check, then reject.

An environment (a production or a staging of a workspace, AD-002):
- a group role per workspace, NOLOGIN, owner of the workspace's tablespace,
  on the workspace's disk (<disk>/.omniacli-db/ts, given to USER; the top of
  the disk lets USER through by an ACL, nobody else more than before);
- a template database per workspace in that tablespace, which odoo.conf's
  db_template names: what Odoo creates (a restore, a staging) lands there;
- a login role per environment, its database's name (env_name), member of
  its group, no superuser, no CREATEDB but inside a job of the helpers
  (allow_createdb), a CONNECTION LIMIT, temp files on its tablespace;
- its database, owned by it, CONNECT and TEMPORARY taken from PUBLIC.

Reached from another machine (AD-003, the cluster of a linked OmniaCLI):
only once a database is asked for another machine (grant_add) the cluster
listens on a port of this machine (PORTS: none a system PostgreSQL has),
over TLS only, with a CA of its own (tls/, its key root's) and a server
certificate for the names the other machines reach this one by; its
container then has a network of its own (the bridge bridge_name), which
firewall_rules lets in from the addresses of the grants only and never out.
A grant is a role and a database of the same name (env_name of the other
machine's workspace and instance), in the cluster's own tablespace, with one
line of pg_hba: hostssl <database>,postgres <role> <address>/32
scram-sha-256 (postgres: where Odoo looks whether its database is there). The
role's password never comes here: the other machine sends its SCRAM
verifier. grants.json holds who asked, from which link and address.

A read replica of the cluster on another machine (AD-004, managedreplica.py):
replica.json holds who has it; pg_hba.conf has one more line, hostssl
replication <REPLICA_ROLE> <address>/32 scram-sha-256, and the firewall lets
that address in too. The replicas this machine hosts of other machines'
clusters are projects beside the cluster's (HOSTED_REPLICAS), each with a
network of its own (replica_bridge).

Names are checked here and quoted; no value of a caller reaches SQL or a
shell but through those checks. The internal names hold a dot, which no
environment's name has (env_name: a-z 0-9 _), so they never meet."""

from __future__ import annotations

import datetime
import hashlib
import ipaddress
import json
import os
import pwd
import re
import secrets
import shutil
import socket
import struct
import subprocess
import time
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

MAJORS = ("17",)
IMAGE = {"17": "postgres:17"}
STORAGE_BASE = "/var/lib/omniacli-storage"
REGISTRY = Path(os.environ.get("OMNIACLI_MACHINE_DB", "/etc/omniacli/machine-db.json"))
FOLDER = ".machine-db"
SERVICE = "db"
# what the servers file says of an instance on the cluster: its db_service,
# never a service of a project (compose-helper sends its database actions there)
DB_SERVICE = "machine"
USER = "omniacli-pg"
SLICE = "omniacli_machine_db.slice"
MOUNTS_UNIT = "omniacli-machine-db-mounts.service"
SYSTEMD = Path("/etc/systemd/system")
SOCKET_IN_ODOO = "/run/omniacli-db"
SOCKET_IN_DB = "/var/run/postgresql"
CONF_IN_DB = "/etc/omniacli-db"
SECRETS_IN_DB = "/run/secrets"
PGDATA_IN_DB = "/var/lib/postgresql/data/pgdata"
# on a workspace's disk: the tablespace (ts) and the files of a move
WS_FOLDER = ".omniacli-db"
# the connections of an environment's role, and of the cluster at its birth
ROLE_LIMIT = 32
BASE_CONNECTIONS = 300
RESERVE = 10
TEMP_FILE_LIMIT = "10GB"
IDLE_IN_TRANSACTION = "1h"
ENV_NAME = re.compile(r"[a-z0-9][a-z0-9_]{0,39}")
NAME_MAX = 40
PASSWORD = re.compile(r"[A-Za-z0-9_-]{16,128}")
STORAGE_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?")
# a path reaches SQL (a tablespace's location) only of these characters
SAFE_PATH = re.compile(r"/[A-Za-z0-9._/-]+")
EXTENSION = re.compile(r"[a-z][a-z0-9_]{0,62}")
# a line of pg_restore -l that makes an extension, or comments it: the
# superuser makes the extensions before, the environment's role the rest.
# An extension has no owner: its line ends with the space before it
# ("2; 3079 72392 EXTENSION - pg_trgm ", read on PostgreSQL 17)
_LIST_EXTENSION = re.compile(r"^\d+; \d+ \d+ EXTENSION - ([a-z][a-z0-9_]{0,62})(?: \S*)?\s*$")
_LIST_DROP = re.compile(r"^\d+; \d+ \d+ (?:EXTENSION|COMMENT - EXTENSION) ")
ENV = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}
READY_TIMEOUT = 180
ACL_XATTR = "system.posix_acl_access"
# reached from another machine (AD-003): the port of this machine the cluster
# listens on (the first free of these; 5432 and those after it are a system
# PostgreSQL's), what the cluster's project keeps of it, the port inside
PORTS = range(15432, 15532)
PORT_IN_DB = 5432
LISTEN_FILE = "listen.json"
GRANTS_FILE = "grants.json"
TLS_FOLDER = "tls"
SYSTEM_POSTGRES = Path("/etc/postgresql")
# what another machine sends in place of the role's password: its SCRAM
# verifier, as PostgreSQL keeps it (the password never leaves that machine)
VERIFIER = re.compile(r"SCRAM-SHA-256\$\d{4,7}:[A-Za-z0-9+/]{16,88}={0,2}\$[A-Za-z0-9+/]{43}=:[A-Za-z0-9+/]{43}=")
# a name another machine reaches this one by (the server certificate's)
REACHED_AS = re.compile(r"(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?")
LINK_NAME = re.compile(r"[A-Za-z0-9._@-]{1,64}")
CA_YEARS = 20
CERT_YEARS = 10
# the read replica of a cluster on another machine (managedreplica.py): the role it comes in as,
# the cluster's record of it, and the record of the replicas this machine hosts
REPLICA_ROLE = "omniacli.replica"
REPLICA_FILE = "replica.json"
HOSTED_REPLICAS = Path(os.environ.get("OMNIACLI_MACHINE_DB_REPLICA", "/etc/omniacli/machine-db-replica.json"))


class ClusterError(Exception):
    pass


# --- names ---------------------------------------------------------------

def env_name(workspace: str, instance: str) -> str:
    """An environment's role and database: <workspace>_<instance>, - as _,
    at most NAME_MAX characters (a longer one cut, with 6 characters of its
    hash: still unique, room left for <name>-verify-<8 hex>)."""
    base = f"{workspace}_{instance}".replace("-", "_")
    if len(base) > NAME_MAX:
        base = f"{base[:NAME_MAX - 7].rstrip('_')}_{hashlib.sha1(base.encode()).hexdigest()[:6]}"
    if not ENV_NAME.fullmatch(base):
        raise ClusterError(f"{base!r} is not a name for the machine's cluster")
    return base


def _workspace(name: str) -> str:
    if not STORAGE_NAME.fullmatch(name):
        raise ClusterError(f"{name!r} is not a workspace name")
    return name


def group_name(workspace: str) -> str:
    """The workspace's group role, owner of its tablespace (of the same name)."""
    return "omniacli.ws." + _workspace(workspace).replace("-", "_")


def template_name(workspace: str) -> str:
    """The workspace's template database, in its tablespace: odoo.conf's db_template."""
    return "omniacli.tpl." + _workspace(workspace).replace("-", "_")


def verify_prefix(role: str) -> str:
    """The throwaway databases of a backup's check of an environment:
    <role>-verify-<hex>, which only its own pg_hba line lets it reach."""
    return f"{_env(role)}-verify-"


def ident(name: str) -> str:
    """A name of ours as SQL writes it: checked, between double quotes."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,62}", name):
        raise ClusterError(f"{name!r} is not a name of the machine's cluster")
    return f'"{name}"'


def literal(text: str, pattern: re.Pattern) -> str:
    if not pattern.fullmatch(text):
        raise ClusterError("a value of the machine's cluster is not of its form")
    return f"'{text}'"


def _env(role: str) -> str:
    if not ENV_NAME.fullmatch(role):
        raise ClusterError(f"{role!r} is not an environment of the machine's cluster")
    return role


def _major(major: str) -> str:
    if major not in MAJORS:
        raise ClusterError(f"PostgreSQL {major!r} is not one the machine's cluster runs ({', '.join(MAJORS)})")
    return major


# --- where it is ---------------------------------------------------------

def registry() -> dict:
    """{major: {"storage", "project"}} of the clusters made (root's file)."""
    try:
        data = json.loads(REGISTRY.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if k in MAJORS and isinstance(v, dict)} if isinstance(data, dict) else {}


def _save_registry(data: dict) -> None:
    if not data:
        REGISTRY.unlink(missing_ok=True)
        return
    REGISTRY.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    tmp = REGISTRY.with_name(REGISTRY.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, REGISTRY)


def project_of(major: str) -> Path:
    """The compose project of the cluster of major; refused when none is made."""
    found = registry().get(_major(major))
    if not found:
        raise ClusterError(f"this machine has no cluster of PostgreSQL {major}: make it in the Database tab")
    project = Path(str(found.get("project") or ""))
    if not project.is_absolute() or not (project / "compose.yaml").is_file() or project.is_symlink():
        raise ClusterError(f"the cluster of PostgreSQL {major} is recorded at {project}, which holds no project")
    return project


def project_folder(storage_root: Path, major: str) -> Path:
    return Path(storage_root) / FOLDER / f"pg{_major(major)}"


def is_cluster(project: Path) -> bool:
    """project is the compose project of one of the clusters."""
    return any(str(v.get("project")) == str(project) for v in registry().values())


def socket_folder(major: str) -> Path:
    return project_of(major) / "run"


def environment(project: Path) -> dict | None:
    """{"major", "role"} of a compose project on the cluster (its .env has
    DB_CLUSTER and DB_USER); None: one with a database of its own."""
    try:
        text = (Path(project) / ".env").read_text(encoding="utf-8")
    except OSError:
        return None
    values = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip()
    if "DB_CLUSTER" not in values:
        return None
    return {"major": _major(values["DB_CLUSTER"]), "role": _env(values.get("DB_USER", ""))}


# --- docker ----------------------------------------------------------------

def _docker() -> str:
    docker = shutil.which("docker", path=ENV["PATH"])
    if docker is None:
        raise ClusterError("docker is not installed")
    return docker


def compose(project: Path, *args: str) -> list[str]:
    return [_docker(), "compose", "--project-directory", str(project), *args]


def _run(project: Path, *args: str, input: str | bytes | None = None, check: bool = True,
         stdout=None, timeout: float | None = None) -> subprocess.CompletedProcess:
    text = not isinstance(input, bytes) and stdout is None
    done = subprocess.run(compose(project, *args), env=ENV, cwd=project, input=input, timeout=timeout,
                          stdout=stdout if stdout is not None else subprocess.PIPE, stderr=subprocess.PIPE,
                          text=text)
    if check and done.returncode:
        err = done.stderr if isinstance(done.stderr, str) else (done.stderr or b"").decode(errors="replace")
        raise ClusterError(f"docker compose {' '.join(args[:3])} failed: {err.strip()[-400:]}")
    return done


def sql(project: Path, database: str, statements: str) -> str:
    """statements given to psql's input as the cluster's superuser (its
    password from the file only the container's user reads), stopping at
    the first error; the output, unaligned."""
    done = _run(project, "exec", "-T", SERVICE, "psql", "-X", "-At", "-q", "-v", "ON_ERROR_STOP=1",
                "-U", "postgres", "-d", database, input=statements, check=False)
    if done.returncode:
        raise ClusterError(f"the machine's cluster refused: {(done.stderr or done.stdout).strip()[-400:]}")
    return done.stdout


def ready(project: Path, timeout: float = READY_TIMEOUT) -> None:
    end = time.monotonic() + timeout
    while _run(project, "exec", "-T", SERVICE, "pg_isready", "-q", "-h", SOCKET_IN_DB, check=False).returncode:
        if time.monotonic() > end:
            log = _run(project, "--ansi", "never", "logs", "--no-log-prefix", "--tail", "20", SERVICE, check=False)
            raise ClusterError(f"the machine's cluster does not answer within {timeout:.0f} s: "
                               f"{(log.stdout or '').strip()[-400:]}")
        time.sleep(2)


# --- the machine's user, the files -------------------------------------------

def pg_user(create: bool = False) -> tuple[int, int]:
    """(uid, gid) of USER, made as a system user without a home or a shell
    when asked."""
    try:
        entry = pwd.getpwnam(USER)
    except KeyError:
        if not create:
            raise ClusterError(f"the user {USER} of the machine's cluster is missing") from None
        subprocess.run([shutil.which("useradd", path=ENV["PATH"]) or "useradd", "--system", "--user-group",
                        "--no-create-home", "--home-dir", "/nonexistent", "--shell", "/usr/sbin/nologin", USER],
                       env=ENV, check=True, capture_output=True)
        entry = pwd.getpwnam(USER)
    return entry.pw_uid, entry.pw_gid


def write(path: Path, text: str, mode: int = 0o644, uid: int = 0, gid: int = 0) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chown(tmp, uid, gid)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _folder(path: Path, mode: int, uid: int = 0, gid: int = 0) -> Path:
    if path.is_symlink():
        raise ClusterError(f"{path} is a symbolic link")
    path.mkdir(mode=mode, exist_ok=True)
    os.chown(path, uid, gid)
    os.chmod(path, mode)
    return path


def _acl(mode: int, uid: int) -> bytes:
    """A POSIX ACL (version 2, as the kernel keeps it): the mode's owner,
    group and others, and uid allowed through (x), the mask the group's
    bits and x."""
    group = (mode >> 3) & 7
    entries = [(0x01, (mode >> 6) & 7, 0xFFFFFFFF), (0x02, 1, uid), (0x04, group, 0xFFFFFFFF),
               (0x10, group | 1, 0xFFFFFFFF), (0x20, mode & 7, 0xFFFFFFFF)]
    return struct.pack("<I", 2) + b"".join(struct.pack("<HHI", tag, perm, who) for tag, perm, who in entries)


def let_through(mount: Path, uid: int) -> None:
    """USER through the top of a workspace's disk (0750, its group the one
    OmniaCLI runs as), to its tablespace: an ACL entry, nobody else more."""
    mode = os.stat(mount).st_mode & 0o777
    os.setxattr(mount, ACL_XATTR, _acl(mode, uid))


def stop_letting_through(mount: Path) -> None:
    mode = os.stat(mount).st_mode & 0o777
    try:
        os.removexattr(mount, ACL_XATTR)
    except OSError:
        pass
    os.chmod(mount, mode)


# --- the cluster -------------------------------------------------------------

def bridge_name(major: str) -> str:
    """The bridge of the network a listening cluster has: what the firewall names."""
    return f"omnidb{_major(major)}"


def replica_bridge(major: str) -> str:
    """The bridge of the network of a replica this machine hosts."""
    return f"omnirp{_major(major)}"


def replica(project: Path) -> dict | None:
    """The cluster's record of its read replica on another machine ({"link",
    "address", "state", ...}: managedreplica.py), None when it has none."""
    try:
        data = json.loads((project / REPLICA_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("address") else None


def hosted_replicas() -> dict:
    """{major: {"storage", "project", "link"}} of the replicas of other
    machines' clusters this machine hosts (root's file)."""
    try:
        data = json.loads(HOSTED_REPLICAS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if k in MAJORS and isinstance(v, dict)} if isinstance(data, dict) else {}


def compose_text(major: str, uid: int, gid: int, roots: list[str], port: int | None = None) -> str:
    """The cluster's project: no port, no network, its socket in run/, the
    storages of the workspaces mounted where they are (rslave: a disk
    mounted later is seen), read only but its data, the slice of its own.
    port (AD-003, a database asked for another machine): that port of this
    machine reaches it, over TLS only, on a network of its own."""
    mounts = "".join(
        f"      - type: bind\n        source: {root}\n        target: {root}\n        bind:\n"
        f"          propagation: rslave\n" for root in roots)
    if port is None:
        head = "No port and no network: the\n# instances on it reach its socket (run/), mounted read only."
        network = "    network_mode: none\n"
        listen = '"-c", "listen_addresses=",'
        networks = ""
    else:
        head = (f"The instances of this machine\n# reach its socket (run/), mounted read only; those of another machine "
                f"the port\n# {int(port)} of this one, over TLS, from the addresses pg_hba.conf and the firewall name.")
        # IPv4 only: the firewall's rules and the lines of pg_hba are of IPv4 addresses
        network = f'    networks:\n      - db\n    ports:\n      - "0.0.0.0:{int(port)}:{PORT_IN_DB}"\n'
        listen = ('"-c", "listen_addresses=*", "-c", "ssl=on",\n'
                  f'              "-c", "ssl_cert_file={CONF_IN_DB}/server.crt", "-c", "ssl_key_file={CONF_IN_DB}/server.key",\n'
                  '              "-c", "ssl_min_protocol_version=TLSv1.2",')
        networks = f"networks:\n  db:\n    driver_opts:\n      com.docker.network.bridge.name: {bridge_name(major)}\n"
    return f"""# written by omniacli workspace-helper machine-db-create: the machine's PostgreSQL {major}
# (docs/adr/postgresql-della-macchina.md, AD-001). {head} It runs as
# {USER}, a user of this machine of its own, in the slice {SLICE}.
name: omniacli-machine-db-{major}
services:
  {SERVICE}:
    image: {IMAGE[major]}
    restart: unless-stopped
    user: "{uid}:{gid}"
{network}    read_only: true
    shm_size: 256m
    tmpfs:
      - /tmp:size=64m
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - ALL
    pids_limit: 2048
    cgroup_parent: {SLICE}
    stop_grace_period: 120s
    command: ["postgres", "-c", "hba_file={CONF_IN_DB}/pg_hba.conf", {listen}
              "-c", "unix_socket_directories={SOCKET_IN_DB}", "-c", "unix_socket_permissions=0777"]
    environment:
      POSTGRES_USER: postgres
      POSTGRES_DB: postgres
      POSTGRES_PASSWORD_FILE: {SECRETS_IN_DB}/superuser
      PGPASSFILE: {SECRETS_IN_DB}/pgpass
      PGDATA: {PGDATA_IN_DB}
      POSTGRES_INITDB_ARGS: "-c max_connections={BASE_CONNECTIONS}"
    volumes:
      - ./data/pgdata:{PGDATA_IN_DB}
      - ./run:{SOCKET_IN_DB}
      - ./conf:{CONF_IN_DB}:ro
      - ./secret:{SECRETS_IN_DB}:ro
{mounts}    healthcheck:
      test: ["CMD", "pg_isready", "-q", "-h", "{SOCKET_IN_DB}", "-U", "postgres", "-d", "postgres"]
      interval: 10s
      timeout: 5s
      retries: 30
      start_period: 60s
    logging:
      driver: json-file
      options:
        max-size: "50m"
        max-file: "5"
{networks}"""


def slice_text() -> str:
    return ("# written by omniacli workspace-helper machine-db-create: the machine's PostgreSQL\n"
            "[Unit]\nDescription=OmniaCLI: the machine's PostgreSQL\n[Slice]\nCPUQuota=\n"
            "MemoryMax=infinity\nMemorySwapMax=infinity\n")


def mounts_unit_text(roots: list[str], command: str) -> str:
    """At every boot, before docker: the storages the cluster mounts made
    shared, so that a workspace's disk mounted under one reaches it."""
    return ("# written by omniacli workspace-helper machine-db-create\n[Unit]\n"
            "Description=OmniaCLI: the storages of the workspaces seen by the machine's PostgreSQL\n"
            "After=local-fs.target " + " ".join(f"omniacli-storage-{Path(r).name}.service" for r in roots) + "\n"
            "Before=docker.service\n[Service]\nType=oneshot\nRemainAfterExit=yes\n"
            f"ExecStart={command} workspace-helper machine-db-mounts\n[Install]\nWantedBy=multi-user.target\n")


def make_shared(roots: list[str]) -> list[str]:
    done = []
    mount = shutil.which("mount", path=ENV["PATH"]) or "mount"
    for root in roots:
        if Path(root).is_dir():
            subprocess.run([mount, "--make-rshared", root], env=ENV, check=True, capture_output=True)
            done.append(root)
    return done


def hba_text(envs: dict, granted: dict | None = None, replicated: dict | None = None) -> str:
    """pg_hba.conf of the cluster: the superuser (and its base backups, on
    the socket only), then a line per
    environment (its database, postgres, its throwaway databases), then a
    line per database of another machine (AD-003: that database and
    postgres, that role, that one address, over TLS; a revoked one has
    none), then the replication of its read replica (AD-004: its own role,
    the one address of the machine that hosts it, over TLS), then reject."""
    lines = ["# written by omniacli workspace-helper: who reaches which database of the machine's cluster",
             "local all postgres scram-sha-256",
             # a base backup of the recovery at a minute (clusterpitr.py): all does not name a replication connection
             "local replication postgres scram-sha-256"]
    for role in sorted(envs):
        name = _env(role)
        lines.append(f'local "{name}",postgres,"/^{name}-verify-[0-9a-f]+$" "{name}" scram-sha-256')
    for role in sorted(granted or {}):
        name, rec = _env(role), granted[role]
        if not rec.get("revoked"):
            # postgres too, as an environment of this machine: Odoo's image waits for the server there
            # and Odoo looks there whether its database exists; no throwaway databases (no CREATEDB)
            lines.append(f'hostssl "{name}",postgres "{name}" {address(rec.get("address"))}/32 scram-sha-256')
    if replicated and replicated.get("state") != "replaced":
        # a replication connection only: its role is of no database
        lines.append(f'hostssl replication "{REPLICA_ROLE}" {address(replicated.get("address"))}/32 scram-sha-256')
    lines += ["local all all reject", "host all all all reject"]
    return "\n".join(lines) + "\n"


def environments(project: Path) -> dict:
    try:
        data = json.loads((project / "environments.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if ENV_NAME.fullmatch(k) and isinstance(v, dict)} \
        if isinstance(data, dict) else {}


def _save_environments(project: Path, envs: dict) -> None:
    write(project / "environments.json", json.dumps(envs, indent=1, sort_keys=True) + "\n", mode=0o644)


def write_hba(project: Path, envs: dict) -> None:
    """pg_hba.conf written again and reloaded; refused by PostgreSQL (its
    pg_hba_file_rules say an error), the file of before is put back."""
    path = project / "conf" / "pg_hba.conf"
    before = path.read_text(encoding="utf-8") if path.is_file() else None
    uid, gid = pg_user()
    write(path, hba_text(envs, grants(project), replica(project)), mode=0o640, uid=0, gid=gid)
    try:
        errors = sql(project, "postgres", "SELECT count(*) FROM pg_hba_file_rules WHERE error IS NOT NULL;\n").strip()
        if errors != "0":
            raise ClusterError(f"pg_hba.conf refused by PostgreSQL ({errors} lines in error)")
        sql(project, "postgres", "SELECT pg_reload_conf();\n")
    except ClusterError:
        if before is not None:
            write(path, before, mode=0o640, uid=0, gid=gid)
        raise


def create(storage: str, storage_root: Path, major: str, roots: list[str], command: str) -> dict:
    """The cluster of major made in storage and started: its user, slice,
    folders, files, project; the boot's unit that lets it see the disks."""
    _major(major)
    if not STORAGE_NAME.fullmatch(storage):
        raise ClusterError(f"{storage!r} is not a storage")
    found = registry()
    if major in found:
        raise ClusterError(f"the cluster of PostgreSQL {major} exists already, in {found[major].get('project')}")
    for root in roots:
        if not SAFE_PATH.fullmatch(root):
            raise ClusterError(f"{root} is not a path the cluster mounts")
    project = project_folder(storage_root, major)
    if project.exists():
        raise ClusterError(f"{project} exists already: remove it by hand, or take the cluster away first")
    uid, gid = pg_user(create=True)
    write(SYSTEMD / SLICE, slice_text())
    write(SYSTEMD / MOUNTS_UNIT, mounts_unit_text(roots, command))
    subprocess.run(["systemctl", "daemon-reload"], env=ENV, check=True, capture_output=True)
    subprocess.run(["systemctl", "enable", MOUNTS_UNIT], env=ENV, check=True, capture_output=True)
    make_shared(roots)
    _folder(project.parent, 0o755)
    try:
        _folder(project, 0o755)
        _folder(project / "data", 0o755)
        _folder(project / "data" / "pgdata", 0o700, uid, gid)
        _folder(project / "run", 0o755, uid, gid)
        _folder(project / "conf", 0o750, 0, gid)
        _folder(project / "secret", 0o750, 0, gid)
        password = secrets.token_urlsafe(32)
        write(project / "secret" / "superuser", password + "\n", mode=0o600, uid=uid, gid=gid)
        write(project / "secret" / "pgpass", f"*:*:*:postgres:{password}\n", mode=0o600, uid=uid, gid=gid)
        write(project / "conf" / "pg_hba.conf", hba_text({}), mode=0o640, uid=0, gid=gid)
        _save_environments(project, {})
        write(project / "compose.yaml", compose_text(major, uid, gid, roots))
        _save_registry({**found, major: {"storage": storage, "project": str(project)}})
        _run(project, "up", "-d", timeout=600)
        ready(project)
        version = sql(project, "postgres", "SHOW server_version;\n").strip()
    except BaseException:
        if (project / "compose.yaml").is_file():
            _run(project, "down", "--remove-orphans", check=False)
        if project.is_dir():
            shutil.rmtree(project, ignore_errors=True)
        _save_registry({k: v for k, v in registry().items() if k != major})
        if not registry():
            _remove_units()
        raise
    return {"major": major, "storage": storage, "project": str(project), "version": version, "user": USER,
            "socket": str(project / "run"), "slice": SLICE, "connections": BASE_CONNECTIONS,
            "undo": f"omniacli workspace-helper machine-db-remove {major}"}


def _remove_units() -> None:
    subprocess.run(["systemctl", "disable", MOUNTS_UNIT], env=ENV, capture_output=True)
    for path in (SYSTEMD / MOUNTS_UNIT, SYSTEMD / SLICE):
        path.unlink(missing_ok=True)
    subprocess.run(["systemctl", "daemon-reload"], env=ENV, capture_output=True)


def remove(major: str) -> dict:
    """The cluster taken away, when it serves no environment: its
    containers, its project and data, its slice and unit; the user too when
    no cluster is left. The image stays when a dedicated database uses it."""
    project = project_of(major)
    envs = environments(project)
    followed = replica(project) or {}
    # replaced by its replica, promoted: its data is old and no instance uses it; it goes with all it holds
    replaced = followed.get("state") == "replaced"
    if followed and not replaced:
        raise ClusterError(f"the cluster of PostgreSQL {major} has a read replica on {followed.get('link')}: "
                           "drop the replica first")
    if envs and not replaced:
        raise ClusterError(f"the cluster of PostgreSQL {major} holds {len(envs)} environments "
                           f"({', '.join(sorted(envs))}): move them to a dedicated database or remove them first")
    lent = grants(project)
    if lent and not replaced:
        raise ClusterError(f"the cluster of PostgreSQL {major} holds {len(lent)} databases of other machines "
                           f"({', '.join(sorted(lent))}): remove their instances there, or drop them here first")
    _run(project, "down", "--remove-orphans", timeout=600)
    if replaced:
        # the tablespaces on the workspaces' disks, which no cluster reads any more
        for mount in sorted({str(rec.get("mount") or "") for rec in envs.values()}):
            folder = Path(mount) / WS_FOLDER
            if Path(mount).is_absolute() and folder.is_dir() and not folder.is_symlink():
                shutil.rmtree(folder, ignore_errors=True)
                stop_letting_through(Path(mount))
    shutil.rmtree(project)
    with_folder = project.parent
    if with_folder.is_dir() and not any(with_folder.iterdir()):
        with_folder.rmdir()
    _save_registry({k: v for k, v in registry().items() if k != major})
    left = registry()
    if not left and not hosted_replicas():
        # a replica hosted here runs as the same user, in the same slice: they stay for it
        _remove_units()
        subprocess.run([shutil.which("userdel", path=ENV["PATH"]) or "userdel", USER], env=ENV, capture_output=True)
    used = subprocess.run([_docker(), "ps", "-a", "-q", "--filter", f"ancestor={IMAGE[major]}"], env=ENV,
                          capture_output=True, text=True).stdout.split()
    image = "kept: other containers use it" if used else "kept: docker image rm it by hand if no one needs it"
    return {"major": major, "removed": str(project), "user": "kept" if left else "removed", "image": image}


def state() -> dict:
    """The clusters made, each with its state and the environments it serves."""
    out = []
    for major, rec in sorted(registry().items()):
        project = Path(str(rec.get("project") or ""))
        row = {"major": major, "storage": rec.get("storage"), "project": str(project),
               "socket": str(project / "run"), "environments": environments(project), "running": False,
               "promised": 0, "port": (listening(project) or {}).get("port"), "grants": grants(project),
               # its read replica on another machine (who has it, in which state: no address of the copy's port)
               "replica": {k: v for k, v in (replica(project) or {}).items() if k in ("link", "state", "address")} or None}
        if (project / "compose.yaml").is_file():
            done = _run(project, "ps", "--format", "{{.State}}", SERVICE, check=False)
            row["running"] = (done.stdout or "").strip() == "running"
        row["promised"] = ROLE_LIMIT * (len(row["environments"]) + len(row["grants"]))
        out.append(row)
    return {"clusters": out, "majors": list(MAJORS)}


def mounts() -> dict:
    """The boot's unit: the storages the clusters mount made shared again."""
    roots = sorted({str(Path(str(v.get("project"))).parent.parent) for v in registry().values()})
    return {"shared": make_shared(roots)}


# --- an environment ------------------------------------------------------------

def _exists(project: Path, query: str) -> bool:
    return sql(project, "postgres", query).strip() == "1"


def env_add(major: str, workspace: str, mount: Path, instance: str, password: str, create_db: bool = True) -> dict:
    """The environment of instance of workspace (its disk mounted on
    mount) on the cluster: group, tablespace, template, role, database,
    pg_hba; refused when the connections promised would go beyond the
    cluster's max_connections less RESERVE."""
    project = project_of(major)
    role = env_name(workspace, instance)
    literal(password, PASSWORD)
    envs = environments(project)
    if role in envs and envs[role].get("instance") != instance:
        raise ClusterError(f"{role} is already an environment of another instance")
    lent = grants(project)
    if role in lent:
        raise ClusterError(f"{role} is already the database of another machine on this cluster: an instance "
                           "of this machine needs another name")
    most = int(sql(project, "postgres", "SHOW max_connections;\n").strip() or 0)
    promised = ROLE_LIMIT * len({*envs, *lent, role})
    if promised > most - RESERVE:
        raise ClusterError(f"the cluster has {most} connections at most: {len({*envs, *lent, role})} environments of "
                           f"{ROLE_LIMIT} each and {RESERVE} in reserve do not fit; raise Connections at most "
                           "in the Database tab")
    uid, gid = pg_user()
    let_through(mount, uid)
    base = _folder(mount / WS_FOLDER, 0o700, uid, gid)
    ts_dir = _folder(base / "ts", 0o700, uid, gid)
    location = str(ts_dir)
    group, tpl = group_name(workspace), template_name(workspace)
    if not _exists(project, f"SELECT 1 FROM pg_roles WHERE rolname = '{group}';\n"):
        sql(project, "postgres", f"CREATE ROLE {ident(group)} NOLOGIN;\n")
    if not _exists(project, f"SELECT 1 FROM pg_tablespace WHERE spcname = '{group}';\n"):
        sql(project, "postgres", f"CREATE TABLESPACE {ident(group)} OWNER {ident(group)} "
                                 f"LOCATION {literal(location, SAFE_PATH)};\n")
    if not _exists(project, f"SELECT 1 FROM pg_database WHERE datname = '{tpl}';\n"):
        sql(project, "postgres", f"CREATE DATABASE {ident(tpl)} TEMPLATE template0 ENCODING 'UTF8' "
                                 f"LC_COLLATE 'C' TABLESPACE {ident(group)};\n")
        sql(project, "postgres", f"ALTER DATABASE {ident(tpl)} IS_TEMPLATE true ALLOW_CONNECTIONS false;\n"
                                 f"REVOKE ALL ON DATABASE {ident(tpl)} FROM PUBLIC;\n")
    if _exists(project, f"SELECT 1 FROM pg_roles WHERE rolname = '{role}';\n"):
        sql(project, "postgres", f"ALTER ROLE {ident(role)} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION "
                                 f"CONNECTION LIMIT {ROLE_LIMIT} PASSWORD {literal(password, PASSWORD)};\n")
    else:
        sql(project, "postgres", f"CREATE ROLE {ident(role)} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION "
                                 f"INHERIT CONNECTION LIMIT {ROLE_LIMIT} PASSWORD {literal(password, PASSWORD)} "
                                 f"IN ROLE {ident(group)};\n")
    sql(project, "postgres", f"ALTER ROLE {ident(role)} SET temp_tablespaces = '{group}';\n"
                             f"ALTER ROLE {ident(role)} SET temp_file_limit = '{TEMP_FILE_LIMIT}';\n"
                             f"ALTER ROLE {ident(role)} SET idle_in_transaction_session_timeout = "
                             f"'{IDLE_IN_TRANSACTION}';\n")
    made = False
    if create_db and not _exists(project, f"SELECT 1 FROM pg_database WHERE datname = '{role}';\n"):
        sql(project, "postgres", f"CREATE DATABASE {ident(role)} OWNER {ident(role)} TEMPLATE {ident(tpl)};\n")
        made = True
    if create_db:
        close(project, role, role)
    _save_environments(project, {**envs, role: {"workspace": workspace, "instance": instance, "mount": str(mount)}})
    write_hba(project, environments(project))
    return {"role": role, "database": role, "tablespace": group, "template": tpl, "created": made,
            "connection_limit": ROLE_LIMIT, "location": location}


def close(project: Path, role: str, database: str) -> None:
    """An environment's database as AD-002 wants it: owned by its role, no
    CONNECT or TEMPORARY for PUBLIC (a database Odoo made has them), the
    statistics of the queries readable when their library is loaded; one
    outside the workspace's tablespace said."""
    _env(role)
    sql(project, "postgres", f"ALTER DATABASE {ident(database)} OWNER TO {ident(role)};\n"
                             f"REVOKE CONNECT, TEMPORARY ON DATABASE {ident(database)} FROM PUBLIC;\n")
    sync_statements(project, [database])


STATEMENTS = """DO $$ BEGIN
 IF current_setting('shared_preload_libraries') ~ '(^|[ ,])pg_stat_statements([ ,]|$)' THEN
  CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
 ELSE
  DROP EXTENSION IF EXISTS pg_stat_statements;
 END IF;
END $$;
"""


def sync_statements(project: Path, databases: list[str] | None = None) -> None:
    """pg_stat_statements made in the environments' databases (only the
    superuser makes it) when its library is loaded, dropped when it is not:
    reading it without its library fails, and Monitoring › Database with it."""
    for database in databases if databases is not None else sorted(environments(project)):
        if _exists(project, f"SELECT 1 FROM pg_database WHERE datname = '{_env(database)}';\n"):
            sql(project, database, STATEMENTS)


def env_remove(major: str, role: str, keep_database: bool = False) -> dict:
    """The environment taken away: its database (unless kept, for a move
    back), its throwaway ones, its role, its pg_hba line; the last of a
    workspace takes the template, the tablespace, the group and its folder."""
    project = project_of(major)
    _env(role)
    envs = environments(project)
    rec = envs.get(role) or {}
    dropped = []
    names = sql(project, "postgres", "SELECT datname FROM pg_database WHERE datname ~ "
                                     f"'^{role}-verify-[0-9a-f]+$';\n").split()
    for name in [*([] if keep_database else [role]), *names]:
        ident(name)
        if _exists(project, f"SELECT 1 FROM pg_database WHERE datname = '{name}';\n"):
            sql(project, "postgres", f"DROP DATABASE {ident(name)} WITH (FORCE);\n")
            dropped.append(name)
    left = {k: v for k, v in envs.items() if k != role}
    _save_environments(project, left)
    write_hba(project, left)
    if not keep_database and _exists(project, f"SELECT 1 FROM pg_roles WHERE rolname = '{role}';\n"):
        sql(project, "postgres", f"DROP ROLE {ident(role)};\n")
    workspace = str(rec.get("workspace") or "")
    freed = {}
    if workspace and not any(v.get("workspace") == workspace for v in left.values()) and not keep_database:
        freed = _workspace_gone(project, workspace, Path(str(rec.get("mount") or "")))
    return {"role": role, "dropped": dropped, **freed}


def _workspace_gone(project: Path, workspace: str, mount: Path) -> dict:
    group, tpl = group_name(workspace), template_name(workspace)
    if _exists(project, f"SELECT 1 FROM pg_database WHERE datname = '{tpl}';\n"):
        sql(project, "postgres", f"ALTER DATABASE {ident(tpl)} IS_TEMPLATE false;\nDROP DATABASE {ident(tpl)};\n")
    if _exists(project, f"SELECT 1 FROM pg_tablespace WHERE spcname = '{group}';\n"):
        sql(project, "postgres", f"DROP TABLESPACE {ident(group)};\n")
    if _exists(project, f"SELECT 1 FROM pg_roles WHERE rolname = '{group}';\n"):
        sql(project, "postgres", f"DROP ROLE {ident(group)};\n")
    if mount.is_absolute() and mount.is_dir() and not mount.is_symlink():
        folder = mount / WS_FOLDER
        if folder.is_dir() and not folder.is_symlink():
            shutil.rmtree(folder)
        stop_letting_through(mount)
    return {"workspace_freed": workspace}


# --- reached from another machine (AD-003, the cluster of a linked OmniaCLI) ----

def address(value) -> str:
    """The one IPv4 address a database of another machine is reached from:
    never a network, never every address, never this machine itself."""
    try:
        found = ipaddress.ip_address(str(value or "").strip())
    except ValueError:
        raise ClusterError(f"{value!r} is not an address (a.b.c.d): the one the other machine comes from") from None
    if found.version != 4 or found.is_unspecified or found.is_loopback or found.is_multicast or found.is_link_local:
        raise ClusterError(f"{found} is not the address of another machine (one IPv4 address, not this machine's own)")
    return str(found)


def reached_as(names) -> list[str]:
    """The names the other machines reach this one by, checked: what the
    server certificate is made for (verify-full checks one of them)."""
    found = []
    for name in names:
        name = str(name or "").strip().lower()
        if not REACHED_AS.fullmatch(name) or name in ("localhost", "127.0.0.1"):
            raise ClusterError(f"{name!r} is not a name or an address another machine reaches this one by")
        if name not in found:
            found.append(name)
    return found


def grants(project: Path) -> dict:
    """{role: {"link", "address", "workspace", "instance", "by", "at",
    "revoked"}} of the databases of this cluster other machines use."""
    try:
        data = json.loads((project / GRANTS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if ENV_NAME.fullmatch(k) and isinstance(v, dict)} \
        if isinstance(data, dict) else {}


def _save_grants(project: Path, found: dict) -> None:
    write(project / GRANTS_FILE, json.dumps(found, indent=1, sort_keys=True) + "\n", mode=0o644)


def listening(project: Path) -> dict | None:
    """{"port", "bridge"} of a cluster that listens on a port of this
    machine, None for one with its socket only."""
    try:
        data = json.loads((project / LISTEN_FILE).read_text(encoding="utf-8"))
        port = int(data["port"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return {"port": port, "bridge": str(data.get("bridge") or "")} if 1024 <= port <= 65535 else None


def system_ports(folder: Path | None = None) -> set[int]:
    """The ports the PostgreSQL of the system has, running or not (its
    clusters' postgresql.conf), and 5432, its own even where there is none yet."""
    folder = folder or SYSTEM_POSTGRES
    found = {5432}
    for conf in folder.glob("*/*/postgresql.conf") if folder.is_dir() else ():
        try:
            text = conf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        found.update(int(p) for p in re.findall(r"(?m)^\s*port\s*=\s*'?(\d{1,5})", text))
    return found


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("0.0.0.0", port))
        except OSError:
            return False
    return True


def pick_port(taken: set[int] = frozenset(), free=_port_free) -> int:
    """The first port of PORTS nothing listens on, that no cluster of ours
    and no PostgreSQL of the system has."""
    avoid = set(taken) | system_ports()
    for port in PORTS:
        if port not in avoid and free(port):
            return port
    raise ClusterError(f"no port free between {PORTS[0]} and {PORTS[-1]} for the cluster to listen on")


def _pem(key) -> str:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


def _san(names: list[str]) -> x509.SubjectAlternativeName:
    """An address is there as an address and as a name: a libpq before 15
    compares an address with the names only."""
    found: list[x509.GeneralName] = []
    for name in names:
        try:
            found.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            pass
        found.append(x509.DNSName(name))
    return x509.SubjectAlternativeName(found)


def make_ca(major: str, machine: str, now: datetime.datetime | None = None) -> tuple[str, str]:
    """(key, certificate) in PEM of the CA of a cluster: made once, its
    certificate what the other machines verify the cluster's against."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"OmniaCLI PostgreSQL {major} of {machine}"[:64])])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=365 * CA_YEARS))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=True,
                                         crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    return _pem(key), cert.public_bytes(serialization.Encoding.PEM).decode()


def make_certificate(ca_key: str, ca_cert: str, names: list[str],
                     now: datetime.datetime | None = None) -> tuple[str, str]:
    """(key, certificate) in PEM of the cluster's server, for names, signed by its CA."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    signer = serialization.load_pem_private_key(ca_key.encode(), None)
    issuer = x509.load_pem_x509_certificate(ca_cert.encode())
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0][:64])]))
            .issuer_name(issuer.subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(min(now + datetime.timedelta(days=365 * CERT_YEARS), issuer.not_valid_after_utc))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=True, key_cert_sign=False,
                                         crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(_san(names), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer.public_key()), critical=False)
            .sign(signer, hashes.SHA256()))
    return _pem(key), cert.public_bytes(serialization.Encoding.PEM).decode()


def certificate_names(pem: str) -> list[str]:
    """The names a server certificate is made for."""
    try:
        cert = x509.load_pem_x509_certificate(pem.encode())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except (ValueError, x509.ExtensionNotFound):
        return []
    return list(dict.fromkeys(san.get_values_for_type(x509.DNSName)))


def ca_certificate(project: Path) -> str:
    try:
        return (project / TLS_FOLDER / "ca.crt").read_text(encoding="utf-8")
    except OSError:
        raise ClusterError("the cluster has no CA yet: it does not listen for another machine") from None


def ensure_tls(project: Path, major: str, names: list[str]) -> bool:
    """The cluster's CA (made once: tls/, its key root's only) and its
    server certificate and key (conf/, the key the cluster's user's only),
    made again when a name is not in it; True when it was made again (the
    cluster then reads it again: a reload, no restart)."""
    uid, gid = pg_user()
    folder = _folder(project / TLS_FOLDER, 0o700)
    ca_key, ca_crt = folder / "ca.key", folder / "ca.crt"
    if not (ca_key.is_file() and ca_crt.is_file()):
        key, cert = make_ca(major, socket.gethostname())
        write(ca_key, key, mode=0o600)
        write(ca_crt, cert, mode=0o644)
    crt, key = project / "conf" / "server.crt", project / "conf" / "server.key"
    have = certificate_names(crt.read_text(encoding="utf-8")) if crt.is_file() and key.is_file() else []
    if have and all(name in have for name in names):
        return False
    wanted = list(dict.fromkeys([*have, *names]))
    made_key, made = make_certificate(ca_key.read_text(encoding="utf-8"), ca_crt.read_text(encoding="utf-8"), wanted)
    write(key, made_key, mode=0o600, uid=uid, gid=gid)
    write(crt, made, mode=0o644, uid=0, gid=gid)
    return True


def _roots_of(text: str) -> list[str]:
    """The storages a cluster's compose.yaml mounts, as it was written."""
    return re.findall(r"(?m)^        source: (/\S+)$", text)


def _recreate(project: Path, text: str, before: str, why: str) -> float:
    """compose.yaml written and the container made again from it (a
    checkpoint first, then PostgreSQL's fast stop: the image's stop signal):
    the seconds the cluster did not answer. One that does not come back is
    put back as it was, and said."""
    try:
        sql(project, "postgres", "CHECKPOINT;\n")
    except ClusterError:
        pass
    write(project / "compose.yaml", text)
    started = time.monotonic()
    try:
        _run(project, "up", "-d", "--remove-orphans", timeout=600)
        ready(project)
    except (ClusterError, subprocess.TimeoutExpired) as exc:
        write(project / "compose.yaml", before)
        _run(project, "up", "-d", "--remove-orphans", check=False, timeout=600)
        ready(project)
        raise ClusterError(f"the cluster did not come back {why} ({exc}): it runs as before") from None
    return round(time.monotonic() - started, 1)


def listen_on(major: str, names: list[str]) -> dict:
    """The cluster listens on a port of this machine, over TLS, for names:
    its CA and certificate, a network of its own, the port published. Its
    container is made again: the instances on it lose the database for the
    seconds said. Already listening: the certificate only, when a name is new."""
    project = project_of(major)
    names = reached_as(names)
    if not names:
        raise ClusterError("the name the other machine reaches this one by is missing")
    found = listening(project)
    changed = ensure_tls(project, major, names)
    if found:
        if changed:
            sql(project, "postgres", "SELECT pg_reload_conf();\n")
        return {**found, "restarted_s": None}
    uid, gid = pg_user()
    before = (project / "compose.yaml").read_text(encoding="utf-8")
    taken = {int((listening(Path(str(v.get("project")))) or {}).get("port") or 0) for v in registry().values()}
    port = pick_port(taken)
    found = {"port": port, "bridge": bridge_name(major)}
    seconds = _recreate(project, compose_text(major, uid, gid, _roots_of(before), port), before, "listening")
    write(project / LISTEN_FILE, json.dumps(found, indent=1, sort_keys=True) + "\n")
    return {**found, "restarted_s": seconds}


def listen_off(major: str) -> dict:
    """The port closed again: the cluster back to its socket only (its
    container made again), when no database of another machine is allowed in.
    Its CA and certificate stay, for the next time."""
    project = project_of(major)
    found = listening(project)
    if not found:
        raise ClusterError(f"the cluster of PostgreSQL {major} listens on no port")
    allowed = sorted(k for k, v in grants(project).items() if not v.get("revoked"))
    if allowed:
        raise ClusterError(f"{', '.join(allowed)} are reached from other machines through this port: revoke or "
                           "drop them first")
    followed = replica(project) or {}
    if followed and followed.get("state") != "replaced":
        raise ClusterError(f"the read replica on {followed.get('link')} follows the cluster through this port: "
                           "drop the replica first")
    uid, gid = pg_user()
    before = (project / "compose.yaml").read_text(encoding="utf-8")
    seconds = _recreate(project, compose_text(major, uid, gid, _roots_of(before)), before, "without its port")
    (project / LISTEN_FILE).unlink(missing_ok=True)
    # the network it listened on, which no container has any more
    subprocess.run([_docker(), "network", "rm", f"omniacli-machine-db-{major}_db"], env=ENV, capture_output=True)
    return {"closed": found["port"], "restarted_s": seconds}


def firewall_state() -> dict[str, list[str]]:
    """{bridge: [addresses]} of the clusters that listen: the addresses of
    their grants not revoked, the only ones their port is reached from."""
    found = {}
    for major, rec in sorted(registry().items()):
        project = Path(str(rec.get("project") or ""))
        if listening(project):
            allowed = {address(v.get("address")) for v in grants(project).values() if not v.get("revoked")}
            followed = replica(project) or {}
            if followed and followed.get("state") != "replaced":
                # the machine that hosts its read replica comes in for the replication
                allowed.add(address(followed.get("address")))
            found[bridge_name(major)] = sorted(allowed)
    return found


BRIDGE = re.compile(r"omni(?:db|rp)\d{1,3}")


def reached(value) -> tuple[list[str], list[tuple[str, int]]]:
    """(the addresses a network is reached from, the address and port it goes
    out to) of an entry of the firewall's state: a list of addresses (a
    cluster that listens: it never goes out), or {"from": [...], "to":
    [[address, port], ...]} (a replica hosted here, which goes out to its
    primary's port and nowhere else)."""
    if isinstance(value, dict):
        coming, going = value.get("from") or [], value.get("to") or []
    else:
        coming, going = value, []
    out = []
    for pair in going:
        try:
            host, port = pair
            port = int(port)
        except (TypeError, ValueError):
            raise ClusterError(f"{pair!r} is not the address and port of a primary") from None
        if not 1 <= port <= 65535:
            raise ClusterError(f"{port} is not a port")
        out.append((address(host), port))
    return sorted({address(a) for a in coming}), sorted(set(out))


def firewall_rules(state: dict) -> dict[str, list[list[str]]]:
    """What the firewall lets reach the clusters that listen (state:
    firewall_state), as iptables arguments after -A <chain>: forward (jumped
    from DOCKER-USER: docker forwards a published port, the host's own
    firewall never sees it), to a cluster's network only its port from the
    addresses of its grants, and from it only the answers; input: nothing
    from a cluster's network to this machine but answers. No address, no
    rule: all dropped. Never a network, never every address (address). A
    replica hosted here (reached) goes out to the port of its primary, and
    takes its answers; nothing else leaves its network."""
    forward, into = [], []
    for bridge in sorted(state):
        if not BRIDGE.fullmatch(bridge):
            raise ClusterError(f"{bridge!r} is not the network of a cluster")
        allowed, going = reached(state[bridge])
        forward += [["-o", bridge, "-s", f"{a}/32", "-p", "tcp", "--dport", str(PORT_IN_DB), "-j", "RETURN"]
                    for a in allowed]
        for host, port in going:
            forward += [["-i", bridge, "-d", f"{host}/32", "-p", "tcp", "--dport", str(port), "-j", "RETURN"],
                        ["-o", bridge, "-s", f"{host}/32", "-p", "tcp", "--sport", str(port), "-m", "conntrack",
                         "--ctstate", "ESTABLISHED", "-j", "RETURN"]]
        forward += [["-o", bridge, "-j", "DROP"],
                    ["-i", bridge, "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "RETURN"],
                    ["-i", bridge, "-j", "DROP"]]
        into += [["-i", bridge, "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "RETURN"],
                 ["-i", bridge, "-j", "DROP"]]
    return {"forward": forward, "input": into}


def grant_add(major: str, spec: dict) -> dict:
    """A database of this cluster for an instance of another machine
    (AD-003): spec {"name" (env_name there), "address" (the one that machine
    comes from), "verifier" (its role's SCRAM verifier), "reached_as" (how
    it reaches this machine), "link", "by", "workspace", "instance"}. The
    cluster listens (the first time: its container made again), the role
    and the database made as an environment's (AD-002) but in the cluster's
    own tablespace, one line of pg_hba. A name that is there already, of
    this machine or of another, is refused and nothing of it is touched.
    What that machine needs to connect, without any secret."""
    project = project_of(major)
    role = _env(str(spec.get("name") or ""))
    source = address(spec.get("address"))
    verifier = literal(str(spec.get("verifier") or ""), VERIFIER)
    link, by = str(spec.get("link") or ""), str(spec.get("by") or "")
    if not LINK_NAME.fullmatch(link):
        raise ClusterError("a database for another machine is asked through its link")
    envs, lent = environments(project), grants(project)
    if role in envs:
        raise ClusterError(f"{role} is an instance of this machine on the cluster already: the instance there "
                           "needs another name")
    if role in lent:
        # never written again: a second instance of that name would take the first one's database and role
        raise ClusterError(f"{role} is a database of another machine on this cluster already: the instance there "
                           "needs another name, or what a removal left here is dropped first (Database tab)")
    taken = sql(project, "postgres", f"SELECT (SELECT count(*) FROM pg_roles WHERE rolname = '{role}') + "
                                     f"(SELECT count(*) FROM pg_database WHERE datname = '{role}');\n").strip()
    if taken != "0":
        raise ClusterError(f"a role or a database {role} is on the cluster already: the instance there needs "
                           "another name")
    most = int(sql(project, "postgres", "SHOW max_connections;\n").strip() or 0)
    if ROLE_LIMIT * len({*envs, *lent, role}) > most - RESERVE:
        raise ClusterError(f"the cluster has {most} connections at most: {len({*envs, *lent, role})} "
                           f"environments of {ROLE_LIMIT} each and {RESERVE} in reserve do not fit; raise "
                           "Connections at most in the Database tab")
    found = listen_on(major, [str(spec.get("reached_as") or "")])
    undo, recorded = [], False
    try:
        # what this grant makes goes if it fails, and only that: CREATE fails on a name made meanwhile
        sql(project, "postgres", f"CREATE ROLE {ident(role)} INHERIT LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                                 f"NOREPLICATION CONNECTION LIMIT {ROLE_LIMIT} PASSWORD {verifier};\n")
        undo.append(f"DROP ROLE IF EXISTS {ident(role)};\n")
        sql(project, "postgres", f"ALTER ROLE {ident(role)} SET temp_file_limit = '{TEMP_FILE_LIMIT}';\n"
                                 f"ALTER ROLE {ident(role)} SET idle_in_transaction_session_timeout = "
                                 f"'{IDLE_IN_TRANSACTION}';\n")
        sql(project, "postgres", f"CREATE DATABASE {ident(role)} OWNER {ident(role)} TEMPLATE template0 "
                                 "ENCODING 'UTF8' LC_COLLATE 'C';\n")
        undo.append(f"DROP DATABASE IF EXISTS {ident(role)} WITH (FORCE);\n")
        close(project, role, role)
        _save_grants(project, {**lent, role: {
            "link": link, "by": by, "address": source, "revoked": False,
            "at": datetime.datetime.now().isoformat(timespec="seconds"),
            "workspace": str(spec.get("workspace") or ""), "instance": str(spec.get("instance") or "")}})
        recorded = True
        write_hba(project, envs)
    except ClusterError:
        # nothing of a grant that failed stays: its line, its database, its role
        if recorded:
            _save_grants(project, {k: v for k, v in grants(project).items() if k != role})
        for statement in reversed(undo):
            try:
                sql(project, "postgres", statement)
            except ClusterError:
                pass
        raise
    return {"role": role, "database": role, "port": found["port"], "address": source, "created": True,
            "restarted_s": found["restarted_s"], "serves": len(envs), "connection_limit": ROLE_LIMIT,
            "names": certificate_names((project / "conf" / "server.crt").read_text(encoding="utf-8")),
            "ca": ca_certificate(project), "version": sql(project, "postgres", "SHOW server_version;\n").strip()}


def grant_set(major: str, role: str, revoked: bool) -> dict:
    """A grant revoked (its line of pg_hba gone, its connections closed: the
    other machine no longer comes in; its database and its role stay) or
    allowed again, from the same address."""
    project = project_of(major)
    lent = grants(project)
    if _env(role) not in lent:
        raise ClusterError(f"{role} is no database of another machine on this cluster")
    if not revoked and not listening(project):
        raise ClusterError(f"the cluster of PostgreSQL {major} listens on no port any more: the instance there is "
                           "made again, or its database dropped here")
    _save_grants(project, {**lent, role: {**lent[role], "revoked": bool(revoked)}})
    write_hba(project, environments(project))
    closed = 0
    if revoked:
        closed = len(sql(project, "postgres", "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                                              f"WHERE usename = '{role}';\n").split())
    return {"role": role, "revoked": bool(revoked), "address": lent[role].get("address"), "closed": closed,
            "kept": [f"database {role}", f"role {role}"]}


def grant_remove(major: str, role: str) -> dict:
    """A grant taken away, as an environment of this machine is
    (env_remove): its database dropped, its role, its line of pg_hba. The
    port stays open, with no line for it, until listen_off."""
    project = project_of(major)
    lent = grants(project)
    if _env(role) not in lent:
        raise ClusterError(f"{role} is no database of another machine on this cluster")
    dropped = []
    left = {k: v for k, v in lent.items() if k != role}
    _save_grants(project, left)
    write_hba(project, environments(project))
    if _exists(project, f"SELECT 1 FROM pg_database WHERE datname = '{role}';\n"):
        sql(project, "postgres", f"DROP DATABASE {ident(role)} WITH (FORCE);\n")
        dropped.append(role)
    if _exists(project, f"SELECT 1 FROM pg_roles WHERE rolname = '{role}';\n"):
        sql(project, "postgres", f"DROP ROLE {ident(role)};\n")
    port = (listening(project) or {}).get("port")
    return {"role": role, "dropped": dropped, "address": lent[role].get("address"), "port": port,
            "still_from": sorted({str(v.get("address")) for v in left.values() if not v.get("revoked")}),
            "grants_left": len(left)}


def allow_createdb(major: str, role: str, on: bool) -> None:
    """CREATEDB for the environment's role, inside a job only (a restore, a
    staging, a backup's check: Odoo drops and creates its database)."""
    sql(project_of(major), "postgres", f"ALTER ROLE {ident(_env(role))} {'CREATEDB' if on else 'NOCREATEDB'};\n")


def database_size(major: str, database: str) -> int:
    return int(sql(project_of(major), "postgres", f"SELECT pg_database_size('{_env(database)}');\n").strip() or 0)


def restore_list(listing: str) -> tuple[list[str], str]:
    """(the extensions a dump makes, its listing without them and their
    comments): the superuser makes the extensions, the role the rest."""
    extensions, kept = [], []
    for line in listing.splitlines():
        found = _LIST_EXTENSION.match(line)
        if found:
            extensions.append(found.group(1))
        if not _LIST_DROP.match(line):
            kept.append(line)
    return sorted(set(extensions)), "\n".join(kept) + "\n"


def make_extensions(run_sql, database: str, names: list[str]) -> None:
    """The extensions a dump needs, made by the superuser in database (each
    name checked, and one PostgreSQL has)."""
    for name in names:
        if not EXTENSION.fullmatch(name):
            raise ClusterError(f"{name!r} is not an extension")
        run_sql(database, f"CREATE EXTENSION IF NOT EXISTS {name};\n")


COUNTS = ("SELECT (SELECT count(*) FROM ir_module_module WHERE state = 'installed') || '|' || "
          "(SELECT count(*) FROM res_users) || '|' || (SELECT count(*) FROM res_partner);\n")
