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

Names are checked here and quoted; no value of a caller reaches SQL or a
shell but through those checks. The internal names hold a dot, which no
environment's name has (env_name: a-z 0-9 _), so they never meet."""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import re
import secrets
import shutil
import struct
import subprocess
import time
from pathlib import Path

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
# superuser makes the extensions before, the environment's role the rest
_LIST_EXTENSION = re.compile(r"^\d+; \d+ \d+ EXTENSION - ([a-z][a-z0-9_]{0,62})(?: \S+)?$")
_LIST_DROP = re.compile(r"^\d+; \d+ \d+ (?:EXTENSION|COMMENT - EXTENSION) ")
ENV = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}
READY_TIMEOUT = 180
ACL_XATTR = "system.posix_acl_access"


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

def compose_text(major: str, uid: int, gid: int, roots: list[str]) -> str:
    """The cluster's project: no port, no network, its socket in run/, the
    storages of the workspaces mounted where they are (rslave: a disk
    mounted later is seen), read only but its data, the slice of its own."""
    mounts = "".join(
        f"      - type: bind\n        source: {root}\n        target: {root}\n        bind:\n"
        f"          propagation: rslave\n" for root in roots)
    return f"""# written by omniacli workspace-helper machine-db-create: the machine's PostgreSQL {major}
# (docs/adr/postgresql-della-macchina.md, AD-001). No port and no network: the
# instances on it reach its socket (run/), mounted read only. It runs as
# {USER}, a user of this machine of its own, in the slice {SLICE}.
name: omniacli-machine-db-{major}
services:
  {SERVICE}:
    image: {IMAGE[major]}
    restart: unless-stopped
    user: "{uid}:{gid}"
    network_mode: none
    read_only: true
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
    command: ["postgres", "-c", "hba_file={CONF_IN_DB}/pg_hba.conf", "-c", "listen_addresses=",
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
"""


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


def hba_text(envs: dict) -> str:
    """pg_hba.conf of the cluster: the superuser, then a line per
    environment (its database, postgres, its throwaway databases), then
    reject; no line for a network (it listens on none)."""
    lines = ["# written by omniacli workspace-helper: who reaches which database of the machine's cluster",
             "local all postgres scram-sha-256"]
    for role in sorted(envs):
        name = _env(role)
        lines.append(f'local "{name}",postgres,"/^{name}-verify-[0-9a-f]+$" "{name}" scram-sha-256')
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
    write(path, hba_text(envs), mode=0o640, uid=0, gid=gid)
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
    if envs:
        raise ClusterError(f"the cluster of PostgreSQL {major} holds {len(envs)} environments "
                           f"({', '.join(sorted(envs))}): move them to a dedicated database or remove them first")
    _run(project, "down", "--remove-orphans", timeout=600)
    shutil.rmtree(project)
    with_folder = project.parent
    if with_folder.is_dir() and not any(with_folder.iterdir()):
        with_folder.rmdir()
    _save_registry({k: v for k, v in registry().items() if k != major})
    left = registry()
    if not left:
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
               "promised": 0}
        if (project / "compose.yaml").is_file():
            done = _run(project, "ps", "--format", "{{.State}}", SERVICE, check=False)
            row["running"] = (done.stdout or "").strip() == "running"
        row["promised"] = ROLE_LIMIT * len(row["environments"])
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
    most = int(sql(project, "postgres", "SHOW max_connections;\n").strip() or 0)
    promised = ROLE_LIMIT * len({*envs, role})
    if promised > most - RESERVE:
        raise ClusterError(f"the cluster has {most} connections at most: {len(envs) + 1} environments of "
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
