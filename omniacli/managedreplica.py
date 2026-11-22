# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A read replica of a PostgreSQL of this machine, made and run by
OmniaCLI (docs/adr/postgresql-della-macchina.md, AD-004 option A;
docs/ux/UX-010-replica-di-lettura.md), root's side: what workspace-helper
machine-db-replica-* runs. Two machines: P has the PostgreSQL (the
primary), R is another OmniaCLI with a link to P and hosts the replica.
The PostgreSQL is the machine's cluster of a major, or the dedicated
database of the production of one instance (the db service of its compose
project, on its workspace's disk: UX-011 §D): a Source, named by its key
(the major, or <workspace>.<storage>.<instance>); what follows says the
cluster, and Dedicated says where an instance's database differs.
Everything is asked from R, through its link; P never calls R: what P has
to say to R (dropped, replaced) R reads the next time it asks.

On P (the cluster's project, machinedb.py):
- allow: a role for the replication only (ROLE; its password is made on R,
  only its SCRAM verifier comes here), one physical slot (SLOT) that holds
  the changes the replica has not taken yet, a cap on what it holds
  (max_slot_wal_keep_size: beyond it the replica loses its place and is
  made again, the disk of P never fills for a replica that is gone), one
  line of pg_hba.conf (hostssl replication <role> <address of R>/32
  scram-sha-256, machinedb.hba_text) and that address let through the
  firewall (machinedb.firewall_state). The cluster starts listening over
  TLS when it does not yet (machinedb.listen_on: its container is made
  again, only when the caller said so). One replica per cluster.
- ready: R says where the replica listens and the CA its certificate is
  verified against: what an instance of P needs to read from it.
- state: what P sees of it: connected or since when not, how many seconds
  behind, how much the slot holds, the copy's progress. The seconds behind
  are measured here: 0 when the replica applied everything P wrote; else the
  age of the oldest change P saw (the samples each read keeps, LAG_FILE)
  that the replica has not applied, and never less than PostgreSQL's own
  replay_lag.
- drop: the slot, the role, the line, the cap; the changes held are freed.
- replaced: the replica was promoted: the cluster is stopped and stays so.

On R (a project of its own beside the machine's cluster project,
<storage>/.machine-db/replica<major>; the record of the hosted replicas in
machinedb.HOSTED_REPLICAS):
- prepare: the room checked, the cluster's user and slice when this machine
  has none, the folders, a CA and a server certificate of its own, P's CA,
  the password of the replication (secret/, the cluster's user's only: it
  comes on the helper's input), pg_hba.conf, compose.yaml, the firewall.
- copy: pg_basebackup over TLS, P's certificate verified against its CA and
  its name (verify-full), through the slot. P's tablespaces are at paths of
  P's workspaces' disks: the container mounts a folder of the replica's own
  (roots/<n>) at each storage root of P, so each path leads inside the
  replica's folder and nothing of this machine is at those paths.
- start: a hot standby in a hardened container (read only, no capability),
  listening over TLS on a free port of this machine, pg_hba.conf letting
  P's one address in as the roles of P's instances, each to its database.
  The superuser comes in by the socket only, which is a tmpfs of the
  container: who reaches it is root of this machine, who reads the files
  anyway. The copy keeps P's superuser's password, which this machine never
  learns.
- sync: pg_hba.conf from the instances P has now, and the folder of a
  tablespace P made since (PostgreSQL stops replaying until it is there).
- promote: the copy takes the writes (pg_promote), once all it received is
  applied. remove: the container, the folder, the firewall.

The return after a promotion (UX-011 §A) turns the two around: the promoted
copy on R lets P in for the replication (return_*), P sets its cluster's
data aside, copies the promoted copy into the cluster's own project and
follows it read only (back_*); to complete it R's copy is stopped first, P
takes the writes only once it applied the last record that copy wrote, and
R's copy stays stopped for good. There P asks, through a link of its own to
R: the machine that hosts a copy is the one that asks.

The databases the cluster gave to other machines (machinedb.grants) are in
the copy: once promoted it lets the same roles in from the same addresses
(UX-011 §B: promote, adopt, hosted_grant_*), one of them used by an
instance of the very machine that hosts the copy included.

The dedicated database of an instance (UX-011 §D, Dedicated). On P its db
service listens over TLS on a port of this machine of its own, for the
address of R only: a compose.override.yaml beside the instance's
compose.yaml (never a change of it) publishes the port and gives PostgreSQL
its certificate and a pg_hba.conf of OmniaCLI's (the project's own network
as before, the replication's role from R's one address over TLS, nothing
else), kept in the project's folder .replica with the record; the
workspaces' firewall lets that one address reach that port. What it keeps
for the copy is on the workspace's disk: 10 % of that disk at most, and
never more than 20 GB (D11-1). On R the copy is a project of its own beside
the others (replica-d<n>), as a cluster's. One replica per database.

No secret is in an answer, in a log or on a command line: the password
comes on the input and is written once in secret/."""

from __future__ import annotations

import datetime
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path

from . import machinedb
from .machinedb import ENV, SERVICE, ClusterError

# the role of the replication and its slot on the primary: internal names (a dot: no environment has one)
ROLE = machinedb.REPLICA_ROLE
SLOT = "omniacli_replica"
RECORD = machinedb.REPLICA_FILE
LAG_FILE = "replica-lag.json"
APPLICATION = "omniacli-replica"
# what the primary keeps for a replica that does not take it, at most (UX-010 D1); the room the copy
# leaves free on this machine's disk; in sync up to these seconds behind (D2); the warnings (D4)
CAP_GB = 20.0
CAP_RANGE = (0.1, 100000.0)
RESERVE_GB = 2.0
RESERVE_RANGE = (0.0, 100000.0)
BEHIND_S = 10
WARN_HELD = 1 << 30
WARN_AFTER = 600
GB = 1 << 30
MB = 1 << 20
# the samples of the primary's position kept to measure how far behind the replica is
LAG_SAMPLES = 120
LAG_SPAN = 6 * 3600
# the time a copy is expected to take, as the dialog says it before: a fixed part and bytes a
# second. Measured between two machines of the tests on 22/11/2026 (see the ADR): the rate is a
# quarter of the measured one or less, a slower network's
COPY_FIXED = 15
COPY_RATE = 25 * MB
COPY_TIMEOUT = 24 * 3600
READY_TIMEOUT = machinedb.READY_TIMEOUT
STREAM_WAIT = 90
PROMOTE_WAIT = 120
FOLDER = "replica"
PGPASS = "replication.pgpass"
PRIMARY_CA = "primary-ca.crt"
SOCKET = machinedb.SOCKET_IN_DB
DATA_IN_DB = str(Path(machinedb.PGDATA_IN_DB).parent)
PASSWORD = machinedb.PASSWORD
MISSING_FOLDER = re.compile(r'directory "(/[^"\s]+)" does not exist')
STATES = ("copying", "ready", "promoted", "returned")
# the return after a promotion (UX-011 §A): the place on the promoted copy that holds its changes for the
# cluster that comes back, how that cluster names itself there, how long it waits for the last of them,
# the folder its old data is set aside in
BACK_SLOT = "omniacli_return"
BACK_APPLICATION = "omniacli-return"
BACK_WAIT = 60
ASIDE = ".aside"
LSN = re.compile(r"[0-9A-F]{1,8}/[0-9A-F]{1,8}")


# --- the amounts -------------------------------------------------------------------

def amount(value, low: float, high: float, what: str) -> float:
    try:
        found = float(str(value).strip().replace(",", "."))
    except ValueError:
        raise ClusterError(f"{value!r} is not a number: {what}") from None
    if not low <= found <= high:
        raise ClusterError(f"{what}: between {low:g} and {high:g}")
    return found


def cap_gb(value) -> float:
    return amount(value, *CAP_RANGE, "the changes the cluster keeps for its replica at most, in GB")


def reserve_gb(value) -> float:
    return amount(value, *RESERVE_RANGE, "the room the copy leaves free on this machine's disk, in GB")


def room(size: int, free: int, reserve: float) -> dict:
    """What the copy of a cluster of size takes here (a fifth more and 256 MB
    for the changes it takes meanwhile), what is free, what must stay free."""
    need = int(size) + int(size) // 5 + 256 * MB
    kept = int(float(reserve) * GB)
    return {"need": need, "free": int(free), "reserve": kept, "fits": int(free) - need >= kept}


def seconds_expected(size: int) -> int:
    return int(COPY_FIXED + int(size) / COPY_RATE)


def verdict(state: dict | None) -> dict:
    """What the row says of a replica, from what the primary sees of it
    (state()): {"code", "lag", "since", "held", "show_held", "warn"}.
    code: copying, sync (up to BEHIND_S seconds behind), behind, off (not
    connected), lost (past the cap: it is made again), replaced. The disk held
    is shown when the replica is not connected or it is 1 GB or more."""
    if not state or not state.get("exists"):
        return {"code": "none"}
    held = int(state.get("held") or 0)
    out = {"lag": state.get("lag"), "since": state.get("since"), "held": held, "show_held": False, "warn": False}
    if state.get("state") == "replaced":
        return {**out, "code": "replaced", "since": state.get("replaced_at")}
    if state.get("state") == "copying":
        return {**out, "code": "copying", "progress": state.get("progress")}
    if state.get("wal_status") == "lost":
        return {**out, "code": "lost", "show_held": False, "warn": True}
    if not state.get("connected"):
        return {**out, "code": "off", "show_held": True, "warn": True}
    lag = int(state.get("lag") or 0)
    big = held >= WARN_HELD
    return {**out, "code": "sync" if lag <= BEHIND_S else "behind", "lag": lag, "show_held": big, "warn": big}


def problem(state: dict | None, who: str, now: datetime.datetime | None = None,
            whose: str = "the machine's PostgreSQL") -> str:
    """What the monitor says out of the panel (UX-010 D4): the replica not
    connected for WARN_AFTER seconds, or the cluster holding more than
    WARN_HELD for it; "" when there is nothing to say. whose: the
    PostgreSQL the replica is of, when it is not the machine's cluster."""
    found = verdict(state)
    now = now or datetime.datetime.now()
    held = f"{found.get('held', 0) / GB:.1f} GB"
    if found["code"] == "lost":
        return (f"the read replica on {who} lost its place: {'the cluster' if whose.startswith('the machine') else whose} "
                "kept more changes for it than its cap. "
                "Drop it and make it again (Database tab)")
    if found["code"] == "off":
        try:
            since = datetime.datetime.fromisoformat(str(found.get("since") or ""))
        except ValueError:
            return ""
        if (now - since).total_seconds() < WARN_AFTER:
            return ""
        return (f"the read replica on {who} is not connected since {since:%Y-%m-%d %H:%M}: {whose} "
                f"keeps {held} of changes for it, which grow until it comes back or it is dropped (Database tab)")
    if found["code"] in ("sync", "behind") and found.get("held", 0) > WARN_HELD:
        return (f"{whose} keeps {held} of changes for the read replica on {who}: the replica is "
                "connected but does not take them fast enough")
    return ""


# --- which PostgreSQL of this machine (P) a copy follows -------------------------------

# what the dedicated database of an instance keeps for its copy at most (D11-1): a tenth of its
# workspace's disk, and never more than this; the role Odoo has in it (the templates'); the
# seconds its restart takes, as the dialog says them before (measured: see the ADR)
DEDICATED_CAP_GB = 20.0
DEDICATED_SHARE = 0.1
DEDICATED_ROLE = "odoo"
DEDICATED_SERVICE = "db"
DEDICATED_RESTART_S = 5
DEDICATED_CONF = "/etc/omniacli-db"
OVERRIDE = "compose.override.yaml"
OVERRIDE_MARK = "# written by omniacli workspace-helper machine-db-replica"


def dedicated_cap_gb(disk_total: int) -> float:
    """What the dedicated database of an instance keeps for its copy at most
    (D11-1): 10 % of its workspace's disk, 20 GB at most, in tenths of GB."""
    return max(CAP_RANGE[0], min(DEDICATED_CAP_GB, int(int(disk_total) * DEDICATED_SHARE * 10 / GB) / 10))


def dedicated_project(workspace: str, storage: str, instance: str) -> Path:
    """The project of the production of an instance of this machine (the
    root helper puts its own check here: the workspace's disk mounted)."""
    project = Path(machinedb.STORAGE_BASE) / storage / workspace / instance / "production"
    if project.is_symlink() or not (project / "compose.yaml").is_file():
        raise ClusterError(f"no project {instance}/production in workspace {workspace}")
    return project


def dedicated_key(workspace: str, storage: str, instance: str) -> str:
    return f"{workspace}.{storage}.{instance}"


def is_dedicated(key: str) -> bool:
    return bool(machinedb.DEDICATED_KEY.fullmatch(str(key)))


def _save_sources(data: dict) -> None:
    path = machinedb.DEDICATED_SOURCES
    if not data:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    machinedb.write(path, json.dumps(data, indent=1, sort_keys=True) + "\n", mode=0o644)


def dedicated_hba(subnet: str, replicated: dict | None = None) -> str:
    """pg_hba.conf of the dedicated database of an instance that listens for
    its copy: what the image wrote (the socket and the loopback of its
    container), the project's own network with a password as before (Odoo),
    the replication's role from the one address of the machine that hosts
    the copy, over TLS, then reject: the port is of this machine now, and no
    other address gets to try a password."""
    network = ipaddress.ip_network(str(subnet))
    lines = ["# written by omniacli workspace-helper: who reaches the PostgreSQL of this instance",
             "local all all trust", "host all all 127.0.0.1/32 trust", "host all all ::1/128 trust",
             "local replication all trust", "host replication all 127.0.0.1/32 trust", "host replication all ::1/128 trust",
             f"host all all {network} scram-sha-256"]
    if replicated and replicated.get("state") != "replaced":
        lines.append(f'hostssl replication "{ROLE}" {machinedb.address(replicated.get("address"))}/32 scram-sha-256')
    lines.append("host all all all reject")
    return "\n".join(lines) + "\n"


def override_text(reads: str | None, listen: dict | None) -> str:
    """compose.override.yaml of an instance's project, OmniaCLI's: reads, the
    Odoo service that reads from a replica (the CA its certificate is
    verified against, for libpq); listen ({"port"}), the dedicated database
    published on a port of this machine over TLS for its copy on another
    machine. "" with neither: no file."""
    if not reads and not listen:
        return ""
    text = (f"{OVERRIDE_MARK}-read: this instance reads from the\n"
            "# read replica of the machine's cluster, on another machine. The CA the replica's certificate is\n"
            "# verified against, for libpq (the cluster itself is reached by its socket, where TLS is not used)\n"
            if not listen else
            f"{OVERRIDE_MARK}: the dedicated database of this instance has a read\n"
            "# replica on another machine (UX-011 D). It listens over TLS on a port of this machine, which the\n"
            "# firewall lets the machine of the replica reach, with a pg_hba.conf that lets that one address in\n"
            "# for the replication; an Odoo that reads from the replica verifies both certificates\n")
    text += "services:\n"
    if reads:
        text += f"  {reads}:\n    environment:\n      PGSSLROOTCERT: /etc/odoo/db-replica-ca.pem\n"
    if listen:
        conf = DEDICATED_CONF
        text += (f"  {DEDICATED_SERVICE}:\n    ports:\n      - \"0.0.0.0:{int(listen['port'])}:{machinedb.PORT_IN_DB}\"\n"
                 f"    command: [\"postgres\", \"-c\", \"hba_file={conf}/pg_hba.conf\", \"-c\", \"ssl=on\",\n"
                 f"              \"-c\", \"ssl_cert_file={conf}/server.crt\", \"-c\", \"ssl_key_file={conf}/server.key\",\n"
                 f"              \"-c\", \"ssl_min_protocol_version=TLSv1.2\"]\n"
                 f"    volumes:\n      - ./{machinedb.DEDICATED_FOLDER}/conf:{conf}:ro\n")
    return text


def override_reads(project: Path) -> str | None:
    """The Odoo service OmniaCLI's override of a project makes read from a
    replica, None when there is none. A file that is not OmniaCLI's is refused."""
    path = project / OVERRIDE
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if not text.startswith(OVERRIDE_MARK):
        raise ClusterError(f"{path} is not OmniaCLI's: it is not written over")
    found = re.search(r"(?m)^  ([A-Za-z0-9_.-]+):\n    environment:\n      PGSSLROOTCERT: ", text)
    return found.group(1) if found else None


def write_override(project: Path, reads: str | None, listen: dict | None) -> None:
    text = override_text(reads, listen)
    if text:
        machinedb.write(project / OVERRIDE, text)
    else:
        (project / OVERRIDE).unlink(missing_ok=True)


class Cluster:
    """The machine's cluster of a major: its project is the cluster's own."""
    kind = "cluster"

    def __init__(self, major: str):
        self.key = self.major = str(major)
        self.project = self.folder = machinedb.project_of(major)
        self.what = f"the machine's PostgreSQL {major}"
        self.whose = "the machine's PostgreSQL"

    def sql(self, statements: str) -> str:
        return machinedb.sql(self.project, "postgres", statements)

    def listening(self) -> dict | None:
        return machinedb.listening(self.project)

    def listen_on(self, names: list[str]) -> dict:
        return machinedb.listen_on(self.major, names)

    def write_hba(self) -> None:
        machinedb.write_hba(self.project, self.environments())

    def environments(self) -> dict:
        return machinedb.environments(self.project)

    def grants(self) -> dict:
        return machinedb.grants(self.project)

    def instances(self) -> list[str]:
        return sorted(f"{e.get('workspace')}-{e.get('instance')}" for e in self.environments().values())

    def roots(self) -> list[str]:
        return machinedb._roots_of((self.project / "compose.yaml").read_text(encoding="utf-8"))

    def ca(self) -> str:
        return machinedb.ca_certificate(self.project)

    def names(self) -> list[str]:
        return machinedb.certificate_names((self.project / "conf" / "server.crt").read_text(encoding="utf-8"))

    def cap(self, value) -> float:
        return cap_gb(value)

    def restart_refused(self) -> str:
        return ("the machine's PostgreSQL is made again once to open its port over TLS: every instance on "
                "it loses its connections. Confirm the restart to make the replica")

    def stop(self) -> bool:
        machinedb._run(self.project, "stop", "-t", "120", SERVICE, timeout=300)
        done = machinedb._run(self.project, "ps", "--format", "{{.State}}", SERVICE, check=False)
        return (done.stdout or "").strip() != "running"

    def tablespace_folders(self) -> list[str]:
        return sorted({str(Path(str(e.get("mount") or "")) / machinedb.WS_FOLDER / "ts") for e in self.environments().values()
                       if e.get("mount")})


class Dedicated:
    """The dedicated database of the production of an instance: the db
    service of its compose project, on its workspace's disk (UX-011 §D)."""
    kind = "dedicated"

    def __init__(self, key: str):
        found = machinedb.DEDICATED_KEY.fullmatch(str(key))
        if not found:
            raise ClusterError(f"{key!r} is not a PostgreSQL of this machine: the major of its cluster, or "
                               "<workspace>.<storage>.<instance> of an instance with a database of its own")
        self.key = str(key)
        self.workspace, self.storage, self.instance = found.groups()
        self.project = dedicated_project(self.workspace, self.storage, self.instance)
        self.folder = self.project / machinedb.DEDICATED_FOLDER
        self.name = f"{self.workspace}-{self.instance}"
        self.what = self.whose = f"the PostgreSQL of {self.name}"
        try:
            self.major = (self.project / "data" / "pgdata" / "PG_VERSION").read_text(encoding="utf-8").strip()
        except OSError:
            self.major = ""

    def has_database(self) -> bool:
        """Its project has a database service of its own (one replaced by its copy has none any more)."""
        return bool(re.search(rf"(?m)^  {DEDICATED_SERVICE}:\s*$", (self.project / "compose.yaml").read_text(encoding="utf-8")))

    def refusal(self) -> str:
        """Why this instance's database cannot have a copy of its own, "" when it can."""
        if not self.has_database():
            return f"{self.name} has no database of its own: only a dedicated one has a replica of its own"
        if not self.major:
            return f"the database of {self.name} is not there yet"
        if self.major not in machinedb.IMAGE:
            return f"PostgreSQL {self.major} is not one OmniaCLI replicates ({', '.join(machinedb.IMAGE)})"
        return ""

    def _env(self) -> dict:
        try:
            text = (self.project / ".env").read_text(encoding="utf-8")
        except OSError:
            return {}
        return dict(line.split("=", 1) for line in text.splitlines() if "=" in line and not line.startswith("#"))

    def subnet(self) -> str:
        try:
            return str(ipaddress.ip_network(self._env().get("SUBNET", "").strip()))
        except ValueError:
            raise ClusterError(f"the network of {self.name} is not in its .env: its database is not published") from None

    def owner(self) -> tuple[int, int]:
        """The user PostgreSQL runs as in the instance's container: who owns its data."""
        found = (self.project / "data" / "pgdata").stat()
        return found.st_uid, found.st_gid

    def sql(self, statements: str) -> str:
        done = machinedb._run(self.project, "exec", "-T", "--user", "postgres", DEDICATED_SERVICE, "psql", "-X", "-At", "-q",
                              "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", "postgres", input=statements, check=False)
        if done.returncode:
            raise ClusterError(f"{self.what} refused: {(done.stderr or done.stdout).strip()[-400:]}")
        return done.stdout

    def _ready(self, timeout: float = READY_TIMEOUT) -> None:
        end = time.monotonic() + timeout
        while machinedb._run(self.project, "exec", "-T", DEDICATED_SERVICE, "pg_isready", "-q", "-h", "127.0.0.1",
                             check=False).returncode:
            if time.monotonic() > end:
                raise ClusterError(f"{self.what} does not answer within {timeout:.0f} s")
            time.sleep(0.5)

    def listening(self) -> dict | None:
        return machinedb.listening(self.folder)

    def _folders(self) -> None:
        machinedb._folder(self.folder, 0o755)
        machinedb._folder(self.folder / "conf", 0o755)

    def _write_hba_file(self) -> None:
        machinedb.write(self.folder / "conf" / "pg_hba.conf", dedicated_hba(self.subnet(), record(self.folder)), mode=0o644)

    def listen_on(self, names: list[str]) -> dict:
        """The database listens on a port of this machine, over TLS, for
        names (and db, the name Odoo reaches it by in its project): its CA
        and certificate, pg_hba.conf, the override; its container is made
        again, Odoo's is not: the seconds it did not answer. Already
        listening: the certificate only, when a name is new. One that does
        not come back is put back as it was, and said."""
        names = machinedb.reached_as(names)
        if not names:
            raise ClusterError("the name the other machine reaches this one by is missing")
        self._folders()
        found = self.listening()
        changed = machinedb.ensure_tls(self.folder, self.major, [*names, DEDICATED_SERVICE], owner=self.owner())
        if found:
            if changed:
                self.sql("SELECT pg_reload_conf();\n")
            return {**found, "restarted_s": None}
        taken = {int((machinedb.listening(Path(str(v.get("project")))) or {}).get("port") or 0)
                 for v in machinedb.registry().values()}
        taken |= {int((machinedb.listening(Path(str(v.get("project") or "")) / machinedb.DEDICATED_FOLDER) or {}).get("port") or 0)
                  for v in machinedb.dedicated_sources().values()}
        port = machinedb.pick_port(taken)
        found = {"port": port, "bridge": ""}
        self._write_hba_file()
        reads = override_reads(self.project)
        try:
            self.sql("CHECKPOINT;\n")
        except ClusterError:
            pass
        write_override(self.project, reads, found)
        started = time.monotonic()
        try:
            machinedb._run(self.project, "up", "-d", "--no-deps", DEDICATED_SERVICE, timeout=600)
            self._ready()
        except (ClusterError, subprocess.TimeoutExpired) as exc:
            write_override(self.project, reads, None)
            machinedb._run(self.project, "up", "-d", "--no-deps", DEDICATED_SERVICE, check=False, timeout=600)
            raise ClusterError(f"{self.what} did not come back listening ({exc}): it runs as before") from None
        seconds = round(time.monotonic() - started, 1)
        machinedb.write(self.folder / machinedb.LISTEN_FILE, json.dumps(found, indent=1, sort_keys=True) + "\n")
        self._registered(True)
        return {**found, "restarted_s": seconds}

    def listen_off(self) -> dict:
        """The port closed again and the database as its compose.yaml says
        it: its container made again; its CA and certificate go with the
        folder. Refused while a copy follows it."""
        followed = record(self.folder) or {}
        if followed and followed.get("state") != "replaced":
            raise ClusterError(f"the read replica on {followed.get('link')} follows {self.what} through this port: "
                               "drop the replica first")
        found = self.listening()
        reads = override_reads(self.project)
        seconds = None
        if found or (self.project / OVERRIDE).exists():
            write_override(self.project, reads, None)
            started = time.monotonic()
            running = machinedb._run(self.project, "ps", "--format", "{{.State}}", DEDICATED_SERVICE, check=False)
            if (running.stdout or "").strip() == "running":
                try:
                    self.sql("CHECKPOINT;\n")
                except ClusterError:
                    pass
                machinedb._run(self.project, "up", "-d", "--no-deps", DEDICATED_SERVICE, timeout=600)
                self._ready()
                seconds = round(time.monotonic() - started, 1)
        shutil.rmtree(self.folder, ignore_errors=True)
        self._registered(False)
        return {"closed": (found or {}).get("port"), "restarted_s": seconds}

    def write_hba(self) -> None:
        """pg_hba.conf written again and reloaded; refused by PostgreSQL, the file of before is put back."""
        path = self.folder / "conf" / "pg_hba.conf"
        before = path.read_text(encoding="utf-8") if path.is_file() else None
        self._folders()
        self._write_hba_file()
        if not self.listening():
            return
        try:
            errors = self.sql("SELECT count(*) FROM pg_hba_file_rules WHERE error IS NOT NULL;\n").strip()
            if errors != "0":
                raise ClusterError(f"pg_hba.conf refused by PostgreSQL ({errors} lines in error)")
            self.sql("SELECT pg_reload_conf();\n")
        except ClusterError:
            if before is not None:
                machinedb.write(path, before, mode=0o644)
            raise

    def environments(self) -> dict:
        return {DEDICATED_ROLE: {"workspace": self.workspace, "instance": self.instance}}

    def grants(self) -> dict:
        return {}

    def instances(self) -> list[str]:
        return [self.name]

    def roots(self) -> list[str]:
        return []

    def ca(self) -> str:
        return machinedb.ca_certificate(self.folder)

    def names(self) -> list[str]:
        return machinedb.certificate_names((self.folder / "conf" / "server.crt").read_text(encoding="utf-8"))

    def disk(self) -> tuple[int, int]:
        """(the size, the room free) of the workspace's disk the database is on."""
        usage = shutil.disk_usage(self.project)
        return usage.total, usage.free

    def cap_limit(self) -> float:
        return dedicated_cap_gb(self.disk()[0])

    def cap(self, value) -> float:
        limit = self.cap_limit()
        found = cap_gb(limit if value in (None, "") else value)
        if found > limit:
            raise ClusterError(f"what {self.what} keeps for its replica is on the disk of the workspace {self.workspace}: "
                               f"{limit:g} GB at most (a tenth of that disk, and never more than {DEDICATED_CAP_GB:g} GB)")
        return found

    def restart_refused(self) -> str:
        return (f"{self.what} is restarted once to open a port over TLS: {self.name} does not answer for those "
                "seconds. Confirm the restart to make the replica")

    def stop(self) -> bool:
        machinedb._run(self.project, "stop", "-t", "120", DEDICATED_SERVICE, timeout=300)
        done = machinedb._run(self.project, "ps", "--format", "{{.State}}", DEDICATED_SERVICE, check=False)
        return (done.stdout or "").strip() != "running"

    def _registered(self, there: bool) -> None:
        """Kept among the dedicated databases that listen for a copy (root's
        file: the firewall and the application read it), or taken away."""
        kept = machinedb.dedicated_sources()
        if there:
            kept[self.key] = {"project": str(self.project), "workspace": self.workspace, "storage": self.storage,
                              "instance": self.instance}
        else:
            kept.pop(self.key, None)
        _save_sources(kept)

    def tablespace_folders(self) -> list[str]:
        return []


def source(key: str):
    """The PostgreSQL of this machine key names: the cluster of that major,
    or the dedicated database of an instance (<workspace>.<storage>.<instance>)."""
    return Dedicated(key) if is_dedicated(key) else Cluster(key)


# --- on the primary (P): the PostgreSQL's own folder ---------------------------------

def record(project: Path) -> dict | None:
    return machinedb.replica(project)


def _save_record(project: Path, rec: dict | None) -> None:
    path = project / RECORD
    if rec is None:
        path.unlink(missing_ok=True)
        (project / LAG_FILE).unlink(missing_ok=True)
        return
    machinedb.write(path, json.dumps(rec, indent=1, sort_keys=True) + "\n", mode=0o644)


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


UNFIT_SQL = ("SELECT current_setting('wal_level'), current_setting('max_wal_senders'), "
             "current_setting('max_replication_slots');\n")


def unfit_of(line: list[str], what: str = "the machine's PostgreSQL") -> str:
    """Why a PostgreSQL that answered line to UNFIT_SQL cannot feed a copy
    that follows it, "" when it can."""
    if len(line) != 3:
        return f"{what} did not say how it records its changes"
    # said without the names of PostgreSQL's parameters (UX-010 §8): the dialog of the other machine shows it
    if line[0] not in ("replica", "logical"):
        return (f"{what} does not record its changes in the way a replica follows: it was set "
                "below its base values by hand, and wants them back and a restart")
    if int(line[1] or 0) < 3 or int(line[2] or 0) < 1:
        return (f"{what} has no connection left for a replica: it was set below its base values "
                "by hand, and wants them back and a restart")
    return ""


def unfit(src) -> str:
    """Why the PostgreSQL cannot feed a replica as it is, "" when it can."""
    return unfit_of(src.sql(UNFIT_SQL).strip().split("|"), src.whose)


SIZE_SQL = ("SELECT (SELECT sum(pg_database_size(oid)) FROM pg_database), current_setting('server_version');\n")


def facts(major: str) -> dict:
    """What the machine that will host the replica says before it is made:
    the size of the whole PostgreSQL, whose data it holds, whether it
    restarts to open its port, the storages its tablespaces are under; of
    the dedicated database of an instance, the workspace it is of, its
    disk, and what it keeps for the copy at most."""
    src = source(major)
    if src.kind == "dedicated" and src.refusal():
        raise ClusterError(src.refusal())
    rec = record(src.folder)
    answer = {"major": src.major, "key": src.key, "kind": src.kind, "running": False, "size": 0, "version": "",
              "port": (src.listening() or {}).get("port"), "instances": src.instances(),
              "grants": sorted(src.grants()), "why": "", "roots": src.roots(),
              "replica": {"link": rec.get("link"), "state": rec.get("state")} if rec else None}
    answer["restarts"] = answer["port"] is None
    if src.kind == "dedicated":
        total, free = src.disk()
        answer.update(workspace=src.workspace, name=src.name, disk_total=total, disk_free=free,
                      cap_gb=src.cap_limit(), restart_s=DEDICATED_RESTART_S)
    try:
        size, version = src.sql(SIZE_SQL).strip().split("|")
        answer.update(size=int(size or 0), version=version, running=True, why=unfit(src))
        if src.kind == "dedicated" and not answer["why"] and tablespaces_of(src):
            answer["why"] = (f"{src.what} has tablespaces of its own: a copy on another machine has no folder for "
                             "them")
    except (ClusterError, ValueError):
        answer["why"] = f"{src.whose} does not answer"
    return answer


def _drop_what_is_there(src) -> None:
    """The slot and the role of a replica, when they are there: its walsender ended first."""
    for _ in range(10):
        active = src.sql("SELECT coalesce(pg_terminate_backend(active_pid), false) FROM "
                         f"pg_replication_slots WHERE slot_name = '{SLOT}' AND active;\n").strip()
        src.sql(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = '{ROLE}';\n")
        if not active:
            break
        time.sleep(0.5)
    try:
        src.sql(f"SELECT pg_drop_replication_slot(slot_name) FROM pg_replication_slots WHERE slot_name = '{SLOT}';\n")
    except ClusterError:
        # taken again meanwhile by a replica that reconnects: its role goes first, then it cannot
        src.sql(f"ALTER ROLE {machinedb.ident(ROLE)} NOLOGIN;\n"
                f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = '{ROLE}';\n")
        time.sleep(1)
        src.sql(f"SELECT pg_drop_replication_slot(slot_name) FROM pg_replication_slots WHERE slot_name = '{SLOT}';\n")
    src.sql(f"DROP ROLE IF EXISTS {machinedb.ident(ROLE)};\n")


def allow(major: str, spec: dict) -> dict:
    """The machine of spec ("address": the one it comes from, "verifier":
    its role's SCRAM verifier, "reached_as": how it reaches this machine,
    "link", "by", "cap_gb", "restart": the PostgreSQL may be made again to
    open its port) let in to copy the PostgreSQL and follow it: the role,
    the slot, the cap, the line of pg_hba.conf. One replica per PostgreSQL.
    What that machine needs to connect, without any secret."""
    src = source(major)
    if src.kind == "dedicated" and src.refusal():
        raise ClusterError(src.refusal())
    origin = machinedb.address(spec.get("address"))
    verifier = machinedb.literal(str(spec.get("verifier") or ""), machinedb.VERIFIER)
    link, by = str(spec.get("link") or ""), str(spec.get("by") or "")
    if not machinedb.LINK_NAME.fullmatch(link):
        raise ClusterError("a replica is asked by the machine that hosts it, through its link")
    cap = src.cap(spec.get("cap_gb", CAP_GB if src.kind == "cluster" else None))
    found = record(src.folder)
    if found:
        raise ClusterError(f"{src.what} has a read replica already, on {found.get('link')}: "
                           "drop it first, from there or from here")
    why = unfit(src)
    if why:
        raise ClusterError(why)
    if src.listening() is None and not spec.get("restart"):
        raise ClusterError(src.restart_refused())
    listen = src.listen_on([str(spec.get("reached_as") or "")])
    envs = src.environments()
    recorded = False
    try:
        # what an attempt that did not end left: it is ours (the names are internal), it goes
        _drop_what_is_there(src)
        src.sql(f"CREATE ROLE {machinedb.ident(ROLE)} LOGIN REPLICATION NOSUPERUSER "
                f"NOCREATEDB NOCREATEROLE CONNECTION LIMIT 4 PASSWORD {verifier};\n")
        # reserved at once: the changes from now are kept, the copy and what follows it miss none
        src.sql(f"SELECT pg_create_physical_replication_slot('{SLOT}', true);\n")
        src.sql(f"ALTER SYSTEM SET max_slot_wal_keep_size = '{int(cap * 1024)}MB';\nSELECT pg_reload_conf();\n")
        rec = {"link": link, "by": by, "address": origin, "at": _now(), "cap_gb": cap, "state": "copying"}
        _save_record(src.folder, rec)
        recorded = True
        src.write_hba()
    except ClusterError:
        if recorded:
            _save_record(src.folder, None)
        try:
            _drop_what_is_there(src)
            src.sql("ALTER SYSTEM RESET max_slot_wal_keep_size;\nSELECT pg_reload_conf();\n")
            src.write_hba()
        except ClusterError:
            pass
        raise
    size, version = src.sql(SIZE_SQL).strip().split("|")
    return {"port": listen["port"], "restarted_s": listen["restarted_s"], "address": origin, "cap_gb": cap,
            "ca": src.ca(), "version": version, "size": int(size or 0), "names": src.names(), "roots": src.roots(),
            "environments": sorted(envs), "tablespaces": tablespaces_of(src),
            "serves": len(envs), "grants": sorted(src.grants()), "kind": src.kind, "major": src.major,
            **({"workspace": src.workspace, "name": src.name} if src.kind == "dedicated" else {})}


def tablespaces_of(src) -> list[str]:
    """Where the tablespaces of the PostgreSQL are: each folder the replica needs at that same path."""
    out = src.sql("SELECT pg_tablespace_location(oid) FROM pg_tablespace WHERE spcname NOT IN ('pg_default', 'pg_global');\n")
    return sorted(line.strip() for line in out.splitlines() if machinedb.SAFE_PATH.fullmatch(line.strip()))


def ready(major: str, spec: dict) -> dict:
    """The replica answers on the machine that hosts it: where an instance of
    this machine reads from it (host, port) and the CA its certificate is
    verified against, kept in the PostgreSQL's record."""
    src = source(major)
    project = src.folder
    rec = record(project)
    if not rec:
        raise ClusterError(f"{src.what} has no read replica")
    host = machinedb.reached_as([spec.get("host")])[0]
    port = str(spec.get("port") or "")
    if not (port.isdigit() and 1024 <= int(port) <= 65535):
        raise ClusterError(f"{port!r} is not the port of a replica")
    ca = certificate(spec.get("ca"))
    _save_record(project, {**rec, "state": "ready", "host": host, "port": int(port), "ca": ca, "ready_at": _now(),
                           "seconds": spec.get("seconds"), "size": spec.get("size")})
    return {"state": "ready", "host": host, "port": int(port)}


def certificate(value) -> str:
    """A CA in PEM, as workspacehelper.remote_spec takes it: checked line by line."""
    text = str(value or "").strip()
    lines = text.splitlines()
    if (len(text) > 64 * 1024 or not lines or lines[0] != "-----BEGIN CERTIFICATE-----"
            or lines[-1] != "-----END CERTIFICATE-----"
            or any(not re.fullmatch(r"[A-Za-z0-9+/=]{1,76}|-----(BEGIN|END) CERTIFICATE-----", line) for line in lines)):
        raise ClusterError("the CA of a replica is one or more certificates in PEM")
    return text + "\n"


def _samples(project: Path) -> list[list[float]]:
    try:
        data = json.loads((project / LAG_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [s for s in data if isinstance(s, list) and len(s) == 2] if isinstance(data, list) else []


def behind(samples: list[list[float]], now: float, current: float, applied: float, own: float | None) -> int:
    """The whole seconds the replica is behind: 0 when it applied all the
    primary wrote (applied >= current); else the age of the oldest position
    of the primary, among the samples [time, position], that it has not
    applied, and never less than PostgreSQL's own measure (own, or None)."""
    if applied >= current:
        return 0
    waiting = [when for when, position in samples if position > applied]
    oldest = now - min(waiting) if waiting else 0.0
    return max(0, int(round(max(oldest, own or 0.0))))


def rate(samples: list[list[float]], now: float, current: float) -> int | None:
    """The bytes of changes the primary writes in an hour, from its oldest
    sample of the last hour; None with less than a minute of them."""
    recent = [s for s in samples if now - s[0] <= 3600]
    if not recent or now - recent[0][0] < 60:
        return None
    return int((current - recent[0][1]) * 3600 / (now - recent[0][0]))


def state_sql(slot: str) -> str:
    """What a PostgreSQL sees of the copy that follows it through slot."""
    return f"""SELECT json_build_object(
 'current', pg_wal_lsn_diff(pg_current_wal_lsn(), '0/0'),
 'slot', (SELECT json_build_object('active', s.active, 'held', pg_wal_lsn_diff(pg_current_wal_lsn(), s.restart_lsn),
          'wal_status', s.wal_status, 'inactive_since', s.inactive_since, 'pid', s.active_pid)
          FROM pg_replication_slots s WHERE s.slot_name = '{slot}'),
 'sender', (SELECT json_build_object('state', r.state, 'applied', pg_wal_lsn_diff(r.replay_lsn, '0/0'),
            'own', extract(epoch FROM r.replay_lag), 'application', r.application_name)
            FROM pg_stat_replication r JOIN pg_replication_slots s ON s.active_pid = r.pid
            WHERE s.slot_name = '{slot}'),
 'copy', (SELECT json_build_object('total', b.backup_total, 'streamed', b.backup_streamed)
          FROM pg_stat_progress_basebackup b JOIN pg_stat_activity a ON a.pid = b.pid
          WHERE a.usename = '{ROLE}' LIMIT 1),
 'size', (SELECT sum(pg_database_size(oid)) FROM pg_database));
"""


STATE_SQL = state_sql(SLOT)


def _seen(project: Path, found: dict, ready: bool, fallback: str | None, answer: dict, now: float | None) -> dict:
    """answer with what found (the answer to state_sql) says of a copy that
    follows this PostgreSQL: connected or since when not, the seconds
    behind, what is held for it, the copy's progress, what is written in an
    hour; the samples of project's LAG_FILE kept for the next look."""
    now = time.time() if now is None else now
    current = float(found.get("current") or 0)
    slot, sender = found.get("slot") or {}, found.get("sender") or {}
    samples = [s for s in _samples(project) if now - s[0] <= LAG_SPAN][-LAG_SAMPLES:]
    answer.update(held=max(0, int(float(slot.get("held") or 0))), wal_status=str(slot.get("wal_status") or ""),
                  cluster_size=int(found.get("size") or 0), rate=rate(samples, now, current))
    if isinstance(found.get("copy"), dict):
        answer["progress"] = {"total": int(found["copy"].get("total") or 0), "streamed": int(found["copy"].get("streamed") or 0)}
    # the copy itself streams the changes it is made under, as its own application: not yet a copy that follows
    answer["following"] = sender.get("state") == "streaming" and str(sender.get("application") or "") in (
        APPLICATION, BACK_APPLICATION)
    if ready and answer["following"]:
        answer.update(connected=True, lag=behind(samples, now, current, float(sender.get("applied") or 0), sender.get("own")))
    elif ready:
        since = str(slot.get("inactive_since") or "")
        try:
            # as this machine's clock says it, without the zone: what the pages write
            answer["since"] = datetime.datetime.fromisoformat(since).astimezone().replace(tzinfo=None).isoformat(timespec="seconds")
        except ValueError:
            answer["since"] = fallback
    if not samples or current != samples[-1][1] or now - samples[-1][0] >= 60:
        samples.append([round(now, 3), current])
    try:
        machinedb.write(project / LAG_FILE, json.dumps(samples[-LAG_SAMPLES:]) + "\n", mode=0o600)
    except OSError:
        pass
    return answer


def state(major: str, now: float | None = None) -> dict:
    """What this machine sees of the replica of its PostgreSQL ({"exists":
    False} with none): its record, connected or since when not, the seconds
    behind, what the slot holds against the cap, the copy's progress, the
    disk free here and what the PostgreSQL writes in an hour."""
    src = source(major)
    project = src.folder
    rec = record(project)
    if not rec:
        return {"exists": False, "major": major}
    envs = src.environments()
    answer = {"exists": True, "major": major, "kind": src.kind, **{k: rec.get(k) for k in (
        "link", "by", "address", "at", "cap_gb", "state", "host", "port", "ready_at", "replaced_at", "seconds", "size")},
        "connected": False, "lag": None, "since": None, "held": 0, "wal_status": "", "progress": None, "rate": None,
        "cap": int(float(rec.get("cap_gb") or CAP_GB) * GB), "free": shutil.disk_usage(src.project).free,
        # what the machine that hosts it follows: the instances the PostgreSQL has now, and where their tablespaces are
        "environments": sorted(envs), "instances": src.instances(), "tablespaces": src.tablespace_folders(),
        "grants": sorted(src.grants()), "grant_lines": grant_lines(src.project)}
    if src.kind == "dedicated":
        # the most it may keep, on its workspace's disk (D11-1): what Change the cap… is refused above
        answer["cap_max"] = src.cap_limit()
    if rec.get("state") == "replaced":
        return answer
    try:
        found = json.loads(src.sql(STATE_SQL).strip() or "{}")
    except (ClusterError, ValueError):
        return {**answer, "unread": True}
    answer = _seen(project, found, rec.get("state") == "ready", rec.get("ready_at") or rec.get("at"), answer, now)
    answer.pop("following", None)
    return answer


GRANT_KEYS = ("address", "revoked", "link", "by", "at", "workspace", "instance")


def grant_lines(project: Path) -> dict:
    """The databases of the cluster other machines use, as the copy that
    may take its place needs them: who comes in from which address (no secret)."""
    return {role: {k: rec.get(k) for k in GRANT_KEYS} for role, rec in sorted(machinedb.grants(project).items())}


def set_cap(major: str, value) -> dict:
    src = source(major)
    rec = record(src.folder)
    if not rec or rec.get("state") == "replaced":
        raise ClusterError(f"{src.what} has no read replica")
    cap = src.cap(value)
    src.sql(f"ALTER SYSTEM SET max_slot_wal_keep_size = '{int(cap * 1024)}MB';\nSELECT pg_reload_conf();\n")
    _save_record(src.folder, {**rec, "cap_gb": cap})
    return {"cap_gb": cap}


def drop(major: str) -> dict:
    """What the PostgreSQL keeps for its replica taken away: the slot (the
    changes it held are freed at the next checkpoint, asked here), the role,
    the line of pg_hba.conf, the cap. The copy on the other machine is that
    machine's to delete. Nothing restarts: a PostgreSQL that listens goes on
    listening, for no address (pg_hba.conf, the firewall), as the cluster
    does; the next replica finds its port open."""
    src = source(major)
    rec = record(src.folder)
    if not rec:
        raise ClusterError(f"{src.what} has no read replica")
    if rec.get("state") == "replaced":
        raise ClusterError("the replica took this cluster's place: it is not dropped from here")
    held = 0
    try:
        held = int(float(src.sql("SELECT coalesce((SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), "
                                 f"restart_lsn) FROM pg_replication_slots WHERE slot_name = '{SLOT}'), 0);\n").strip() or 0))
    except (ClusterError, ValueError):
        pass
    _save_record(src.folder, None)
    src.write_hba()
    _drop_what_is_there(src)
    src.sql("ALTER SYSTEM RESET max_slot_wal_keep_size;\nSELECT pg_reload_conf();\n")
    try:
        src.sql("CHECKPOINT;\n")
    except ClusterError:
        pass
    return {"dropped": True, "link": rec.get("link"), "address": rec.get("address"), "freed": max(0, held),
            "host": rec.get("host"), "port": rec.get("port")}


def replaced(major: str) -> dict:
    """The replica was promoted, or is about to be: this PostgreSQL is
    stopped (a checkpoint, PostgreSQL's fast stop: a replica that is
    connected gets everything first) and stays stopped; its record says so."""
    src = source(major)
    project = src.folder
    rec = record(project)
    if not rec:
        raise ClusterError(f"{src.what} has no read replica")
    if rec.get("state") == "copying":
        raise ClusterError("the replica is not a whole copy yet: it takes this cluster's place only after it was in sync")
    if rec.get("state") == "replaced":
        return {"stopped": True, "already": True, "replaced_at": rec.get("replaced_at"),
                "environments": src.environments(), "grants": sorted(src.grants()),
                "grant_lines": grant_lines(src.project), "host": rec.get("host"), "port": rec.get("port")}
    try:
        src.sql("CHECKPOINT;\n")
    except ClusterError:
        pass
    started = time.monotonic()
    if not src.stop():
        raise ClusterError(f"{src.whose} did not stop: nothing is promoted over a "
                           f"{'cluster' if src.kind == 'cluster' else 'database'} that runs")
    at = _now()
    _save_record(project, {**rec, "state": "replaced", "replaced_at": at})
    return {"stopped": True, "already": False, "replaced_at": at, "seconds": round(time.monotonic() - started, 1),
            "environments": src.environments(), "grants": sorted(src.grants()),
            "grant_lines": grant_lines(src.project), "host": rec.get("host"), "port": rec.get("port")}


def target(major: str) -> dict:
    """Where an instance of this machine reaches the replica of its
    PostgreSQL: {"host", "port", "ca", "state", "link"}; refused when there
    is none that answers yet."""
    src = source(major)
    rec = record(src.folder)
    if not rec or rec.get("state") not in ("ready", "replaced") or not rec.get("host"):
        raise ClusterError(f"{src.what} has no read replica that answers")
    return {"host": str(rec["host"]), "port": int(rec["port"]), "ca": certificate(rec.get("ca")),
            "state": rec["state"], "link": str(rec.get("link") or "")}


# --- on the machine that hosts it (R) ------------------------------------------------

def hosted() -> dict:
    return machinedb.hosted_replicas()


def _save_hosted(data: dict) -> None:
    path = machinedb.HOSTED_REPLICAS
    if not data:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    machinedb.write(path, json.dumps(data, indent=1, sort_keys=True) + "\n", mode=0o644)


def project_folder(storage_root: Path, major: str) -> Path:
    key = machinedb.hosted_key(major)
    return Path(storage_root) / machinedb.FOLDER / (f"{FOLDER}-{key}" if key.startswith("d") else f"{FOLDER}{key}")


def project_name(key: str) -> str:
    """The name of the compose project of a replica hosted here."""
    return f"omniacli-machine-db-replica-{machinedb.hosted_key(key)}"


def next_hosted_key(found: dict | None = None) -> str:
    """The key of a new copy of an instance's dedicated database: d and the first number free."""
    found = hosted() if found is None else found
    for number in range(100, 1000):
        if f"d{number}" not in found:
            return f"d{number}"
    raise ClusterError("this machine hosts too many copies of dedicated databases")


def hosted_project(major: str) -> Path:
    found = hosted().get(machinedb.hosted_key(major))
    if not found:
        raise ClusterError(f"this machine hosts no read replica {major}" if str(major).startswith("d") else
                           f"this machine hosts no read replica of a PostgreSQL {major}")
    project = Path(str(found.get("project") or ""))
    if not project.is_absolute() or project.is_symlink() or not (project / "compose.yaml").is_file():
        raise ClusterError(f"the read replica is recorded at {project}, which holds no project")
    return project


def hosted_record(project: Path) -> dict:
    try:
        data = json.loads((project / RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_hosted_record(project: Path, rec: dict) -> None:
    machinedb.write(project / RECORD, json.dumps(rec, indent=1, sort_keys=True) + "\n", mode=0o644)


def bridge_name(major: str) -> str:
    return machinedb.replica_bridge(major)


def conninfo(host: str, port: int, application: str = APPLICATION) -> str:
    """How the replica reaches its primary: over TLS, the certificate
    verified against the primary's CA and its name; the password in a file."""
    host = machinedb.reached_as([host])[0]
    return (f"host={host} port={int(port)} user={ROLE} passfile={machinedb.SECRETS_IN_DB}/{PGPASS} "
            f"sslmode=verify-full sslrootcert={machinedb.CONF_IN_DB}/{PRIMARY_CA} application_name={application}")


def compose_text(major: str, uid: int, gid: int, port: int, roots: list[str], host: str, primary_port: int,
                 key: str | None = None) -> str:
    """The replica's project: PostgreSQL of the primary's major on a copy of
    its data, a hot standby that follows it through its slot; read only, no
    capability, the slice of the machine's PostgreSQL, its socket in a tmpfs
    of its own; port of this machine reaches it, over TLS only, on a network
    of its own; a folder of its own mounted at each storage root of the
    primary, where the tablespaces are. It never archives, promoted or not.
    key: the copy's own, when it is not the major (the copy of the
    dedicated database of an instance)."""
    key = machinedb.hosted_key(key or major)
    for root in roots:
        if not machinedb.SAFE_PATH.fullmatch(root) or ".." in Path(root).parts:
            raise ClusterError(f"{root} is not the path of a storage of the primary")
    mounts = "".join(f"      - ./roots/{index}:{root}\n" for index, root in enumerate(roots))
    return f"""# written by omniacli workspace-helper machine-db-replica-make: the read replica of the PostgreSQL {major}
# of another machine (docs/adr/postgresql-della-macchina.md, AD-004). A copy of that whole cluster,
# kept up to date; it runs as {machinedb.USER}, in the slice {machinedb.SLICE}, and is reached on the
# port {int(port)} of this machine, over TLS, from the address pg_hba.conf and the firewall name.
name: {project_name(key)}
services:
  {SERVICE}:
    image: {machinedb.IMAGE[major]}
    restart: unless-stopped
    user: "{uid}:{gid}"
    networks:
      - db
    ports:
      - "0.0.0.0:{int(port)}:{machinedb.PORT_IN_DB}"
    read_only: true
    shm_size: 256m
    tmpfs:
      - /tmp:size=64m
      - {SOCKET}:size=1m,uid={uid},gid={gid},mode=0700
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - ALL
    pids_limit: 2048
    cgroup_parent: {machinedb.SLICE}
    stop_grace_period: 120s
    command: ["postgres", "-c", "hba_file={machinedb.CONF_IN_DB}/pg_hba.conf", "-c", "listen_addresses=*", "-c", "ssl=on",
              "-c", "ssl_cert_file={machinedb.CONF_IN_DB}/server.crt", "-c", "ssl_key_file={machinedb.CONF_IN_DB}/server.key",
              "-c", "ssl_min_protocol_version=TLSv1.2",
              "-c", "unix_socket_directories={SOCKET}", "-c", "unix_socket_permissions=0700",
              "-c", "archive_mode=off", "-c", "hot_standby=on",
              "-c", "primary_conninfo={conninfo(host, primary_port)}",
              "-c", "primary_slot_name={SLOT}"]
    environment:
      PGDATA: {machinedb.PGDATA_IN_DB}
    volumes:
      - ./data:{DATA_IN_DB}
      - ./conf:{machinedb.CONF_IN_DB}:ro
      - ./secret:{machinedb.SECRETS_IN_DB}:ro
{mounts}    healthcheck:
      test: ["CMD", "pg_isready", "-q", "-h", "{SOCKET}", "-U", "postgres", "-d", "postgres"]
      interval: 10s
      timeout: 5s
      retries: 30
      start_period: 60s
    logging:
      driver: json-file
      options:
        max-size: "50m"
        max-file: "5"
networks:
  db:
    driver_opts:
      com.docker.network.bridge.name: {bridge_name(key)}
"""


def hba_text(environments, address: str, link: str = "", back: bool = False, lent: dict | None = None,
             own: dict | None = None, whole: bool = False) -> str:
    """pg_hba.conf of the replica: the superuser by the socket (a tmpfs of
    its container: root's helper only), then the one address of the primary's
    machine as each of its instances' roles, to its database and postgres
    (where Odoo looks whether its database is there), over TLS; lent (the
    copy was promoted, UX-011 §B): the databases that cluster had given to
    other machines, each role from the address it came in from there, a
    revoked one from none; own: {address of this machine: the address its
    containers are seen with by the copy}, for a database an instance of
    this very machine uses: both let in; back (the cluster comes back,
    UX-011 §A): the primary's address for the replication, as the
    replication's role; then reject. whole: the copy of the dedicated
    database of one instance, whose role reaches every database of it (its
    own, whatever its name, and postgres)."""
    source = machinedb.address(address)
    lines = [f"# written by omniacli workspace-helper: who reaches the read replica of the PostgreSQL of {link or 'another machine'}",
             "local all postgres trust"]
    for role in sorted(environments or []):
        name = machinedb._env(role)
        lines.append(f'hostssl all "{name}" {source}/32 scram-sha-256' if whole else
                     f'hostssl "{name}",postgres "{name}" {source}/32 scram-sha-256')
    for role in sorted(lent or {}):
        name, grant = machinedb._env(role), lent[role]
        if grant.get("revoked"):
            continue
        come = machinedb.address(grant.get("address"))
        for one in dict.fromkeys([come, *([machinedb.address((own or {})[come])] if (own or {}).get(come) else [])]):
            lines.append(f'hostssl "{name}",postgres "{name}" {one}/32 scram-sha-256')
    if back:
        # a replication connection only: its role is of no database
        lines.append(f'hostssl replication "{ROLE}" {source}/32 scram-sha-256')
    lines += ["local all all reject", "host all all all reject"]
    return "\n".join(lines) + "\n"


def returning(rec: dict) -> dict:
    """The hosted record's own part on the return of the cluster the copy replaced ({} with none)."""
    return rec["return"] if isinstance(rec.get("return"), dict) else {}


def hosted_hba(rec: dict, environments=None, lent: dict | None = None, own: dict | None = None) -> str:
    """pg_hba.conf of a hosted copy as its record says it; lent: the
    databases other machines use, let in once the copy is promoted."""
    return hba_text(rec.get("environments") if environments is None else environments, str(rec.get("primary_address") or ""),
                    str(rec.get("link") or ""), back=returning(rec).get("state") in ("copying", "ready"),
                    lent=lent if rec.get("state") == "promoted" else None, own=own, whole=bool(rec.get("source")))


def own_address(value: str) -> bool:
    """value is an address of this machine itself (a socket binds to it)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind((str(value), 0))
    except OSError:
        return False
    return True


def copy_gateway(major: str) -> str:
    """The address the containers of this machine are seen with by the copy
    hosted here: the gateway of its network (docker masquerades what goes
    from one of its networks to another); "" when it cannot be read."""
    done = subprocess.run([machinedb._docker(), "network", "inspect", f"{project_name(major)}_db",
                           "--format", "{{range .IPAM.Config}}{{.Gateway}}{{end}}"], env=ENV, capture_output=True, text=True)
    try:
        return machinedb.address((done.stdout or "").strip())
    except ClusterError:
        return ""


def hosted_grants(project: Path) -> dict:
    """The databases of the promoted copy other machines use: as the
    cluster's own grants, kept in the copy's project from the promotion."""
    return machinedb.grants(project)


def own_addresses(lent: dict, major: str) -> dict:
    """{address of this machine a grant comes in from: the address its containers are seen with by the copy}."""
    found = {}
    for grant in lent.values():
        come = str(grant.get("address") or "")
        if come and come not in found and not grant.get("revoked") and own_address(come):
            found[come] = copy_gateway(major)
    return {k: v for k, v in found.items() if v}


def _write_hosted_hba(project: Path, rec: dict) -> None:
    _uid, gid = machinedb.pg_user()
    lent = hosted_grants(project) if rec.get("state") == "promoted" else {}
    text = hosted_hba(rec, lent=lent, own=own_addresses(lent, str(rec.get("key") or rec.get("major") or "")) if lent else None)
    machinedb.write(project / "conf" / "pg_hba.conf", text, mode=0o640, uid=0, gid=gid)
    replica_sql(project, "SELECT pg_reload_conf();\n", check=False)


def under_roots(location: str, roots: list[str], project: Path) -> Path:
    """Where a tablespace of the primary is in the replica's folder: under
    roots/<n>, which its container mounts on the primary's storage roots[n]."""
    if not machinedb.SAFE_PATH.fullmatch(location) or ".." in Path(location).parts:
        raise ClusterError("a tablespace's location is not a path of a storage")
    for index, root in enumerate(roots):
        if location.startswith(root.rstrip("/") + "/"):
            return project / "roots" / str(index) / location[len(root.rstrip("/")) + 1:]
    raise ClusterError("a tablespace of the primary is outside its storages: the replica has no folder for it")


def _owned_folder(path: Path, uid: int, gid: int, top: Path) -> None:
    """path made under top, each folder made the cluster's user's."""
    made = []
    current = path
    while current != top and not current.exists():
        made.append(current)
        current = current.parent
    for folder in reversed(made):
        folder.mkdir(mode=0o700)
        os.chown(folder, uid, gid)


def _compose(project: Path, *args: str, input: str | None = None, check: bool = True,
             timeout: float | None = None) -> subprocess.CompletedProcess:
    return machinedb._run(project, *args, input=input, check=check, timeout=timeout)


def replica_sql(project: Path, statements: str, check: bool = True) -> str:
    """statements given to psql in the replica's container, as its superuser, by its socket."""
    done = _compose(project, "exec", "-T", SERVICE, "psql", "-X", "-At", "-q", "-v", "ON_ERROR_STOP=1", "-h", SOCKET,
                    "-U", "postgres", "-d", "postgres", input=statements, check=False, timeout=120)
    if done.returncode:
        if not check:
            return ""
        raise ClusterError(f"the read replica refused: {(done.stderr or done.stdout).strip()[-400:]}")
    return done.stdout


def _primary_address(host: str) -> str:
    """The one IPv4 address the primary's machine comes from: its host's."""
    try:
        return machinedb.address(host)
    except ClusterError:
        pass
    try:
        found = sorted({info[4][0] for info in socket.getaddrinfo(host, None, socket.AF_INET)})
    except (OSError, UnicodeError):
        found = []
    if len(found) != 1:
        raise ClusterError(f"{host} has not one IPv4 address: the replica lets in the one address its primary's "
                           "machine comes from")
    return machinedb.address(found[0])


def prepare(spec: dict, storage_root: Path, command: str = "") -> dict:
    """The replica's project made, nothing copied yet. spec: {"major",
    "storage", "link", "by", "host", "port", "ca" (how the primary is
    reached and verified), "address" (this machine as the primary's sees
    it: the name of the replica's certificate), "primary_address" (where the
    primary's machine comes from, when it is not its host's), "password" (of
    the replication's role), "roots", "environments", "tablespaces", "size",
    "reserve_gb"}. Refused when the copy does not fit. The copy of the
    dedicated database of an instance (UX-011 §D): "key" (d<n>, the copy's
    own on this machine) and "source" ({"key", "workspace", "name"}: whose
    database it is, on that machine), "major" the PostgreSQL it is of."""
    key = machinedb.hosted_key(str(spec.get("key") or spec.get("major") or ""))
    major = machinedb._major(str(spec.get("major") or ""))
    storage, link = str(spec.get("storage") or ""), str(spec.get("link") or "")
    if not machinedb.STORAGE_NAME.fullmatch(storage) or not machinedb.LINK_NAME.fullmatch(link):
        raise ClusterError("a replica is made on a storage of this machine, of the cluster of one of its links")
    found = hosted()
    origin = None
    if key != major:
        given = spec.get("source") if isinstance(spec.get("source"), dict) else {}
        origin = {"key": str(given.get("key") or ""), "workspace": str(given.get("workspace") or ""),
                  "name": str(given.get("name") or "")}
        if not is_dedicated(origin["key"]) or not re.fullmatch(r"[a-z0-9-]{1,40}", origin["workspace"]) \
                or not re.fullmatch(r"[a-z0-9-]{1,81}", origin["name"]):
            raise ClusterError("the copy of a dedicated database names the instance it is of")
        same = next((v for v in found.values() if v.get("link") == link and v.get("source") == origin["key"]), None)
        if same:
            raise ClusterError(f"this machine hosts a read replica of {origin['name']} of {link} already: drop it first")
    if key in found:
        raise ClusterError(f"this machine hosts a read replica of a PostgreSQL {major} already, of "
                           f"{found[key].get('link')}: drop it first")
    host = machinedb.reached_as([spec.get("host")])[0]
    port = int(spec.get("port") or 0)
    if not 1024 <= port <= 65535:
        raise ClusterError(f"{port} is not the port of the primary")
    own = machinedb.reached_as([spec.get("address")])[0]
    primary = machinedb.address(spec.get("primary_address")) if spec.get("primary_address") else _primary_address(host)
    password = str(spec.get("password") or "")
    if not PASSWORD.fullmatch(password):
        raise ClusterError("the password of the replication's role is not of its form")
    ca = certificate(spec.get("ca"))
    roots = [str(r) for r in spec.get("roots") or []]
    environments = sorted(machinedb._env(str(e)) for e in spec.get("environments") or [])
    size = int(spec.get("size") or 0)
    reserve = reserve_gb(spec.get("reserve_gb", RESERVE_GB))
    project = project_folder(storage_root, key)
    if project.exists():
        raise ClusterError(f"{project} exists already: remove it by hand, or drop the replica first")
    fits = room(size, shutil.disk_usage(storage_root).free, reserve)
    if not fits["fits"]:
        raise ClusterError(f"no room: the copy of the {'database of ' + origin['name'] if origin else 'cluster'} of {link} "
                           f"takes about {fits['need'] // MB} MB, "
                           f"{fits['free'] / GB:.1f} GB are free here and {reserve:g} GB must stay free")
    uid, gid = machinedb.pg_user(create=True)
    if not (machinedb.SYSTEMD / machinedb.SLICE).is_file():
        machinedb.write(machinedb.SYSTEMD / machinedb.SLICE, machinedb.slice_text())
        subprocess.run(["systemctl", "daemon-reload"], env=ENV, check=True, capture_output=True)
    taken = {int((machinedb.listening(Path(str(v.get("project")))) or {}).get("port") or 0)
             for v in machinedb.registry().values()}
    taken |= {int(hosted_record(Path(str(v.get("project") or ""))).get("own_port") or 0) for v in found.values()}
    own_port = machinedb.pick_port(taken)
    machinedb._folder(project.parent, 0o755)
    try:
        machinedb._folder(project, 0o755)
        machinedb._folder(project / "data", 0o700, uid, gid)
        machinedb._folder(project / "data" / "pgdata", 0o700, uid, gid)
        machinedb._folder(project / "conf", 0o750, 0, gid)
        machinedb._folder(project / "secret", 0o750, 0, gid)
        machinedb._folder(project / "roots", 0o750, 0, gid)
        for index in range(len(roots)):
            machinedb._folder(project / "roots" / str(index), 0o700, uid, gid)
        machinedb.write(project / "secret" / PGPASS, f"*:*:*:{ROLE}:{password}\n", mode=0o600, uid=uid, gid=gid)
        machinedb.write(project / "conf" / PRIMARY_CA, ca, mode=0o644, uid=0, gid=gid)
        machinedb.write(project / "conf" / "pg_hba.conf", hba_text(environments, primary, link, whole=bool(origin)),
                        mode=0o640, uid=0, gid=gid)
        # a CA of the replica's own and its server certificate, for the name the primary's machine reaches it by
        machinedb.ensure_tls(project, major, [own])
        for location in spec.get("tablespaces") or []:
            _owned_folder(under_roots(str(location), roots, project), uid, gid, project / "roots")
        machinedb.write(project / "compose.yaml", compose_text(major, uid, gid, own_port, roots, host, port, key=key))
        _save_hosted_record(project, {
            "major": major, "link": link, "by": str(spec.get("by") or ""), "host": host, "port": port,
            "primary_address": primary, "address": own, "own_port": own_port, "roots": roots,
            "environments": environments, "state": "copying", "at": _now(), "expected": size, "reserve_gb": reserve,
            **({"key": key, "source": origin} if origin else {})})
        _save_hosted({**found, key: {"storage": storage, "project": str(project), "link": link,
                                     **({"source": origin["key"], "name": origin["name"], "workspace": origin["workspace"],
                                         "major": major} if origin else {})}})
    except BaseException:
        shutil.rmtree(project, ignore_errors=True)
        _save_hosted({k: v for k, v in hosted().items() if k != key})
        raise
    return {"major": major, "key": key, "project": str(project), "port": own_port, "address": own, "primary_address": primary,
            "room": fits, "user": machinedb.USER}


def copy(major: str) -> dict:
    """The whole cluster of the primary copied into the replica's folder
    (pg_basebackup through the slot, over TLS, the primary's certificate
    verified): its seconds and its size."""
    project = hosted_project(major)
    rec = hosted_record(project)
    if rec.get("state") != "copying":
        raise ClusterError("the replica is copied already")
    pgdata = project / "data" / "pgdata"
    if any(pgdata.iterdir()):
        raise ClusterError("the replica's folder is not empty: drop the replica and make it again")
    started = time.monotonic()
    done = _compose(project, "run", "--rm", "--no-deps", "-T",
                    "-e", f"PGPASSFILE={machinedb.SECRETS_IN_DB}/{PGPASS}", "-e", "PGSSLMODE=verify-full",
                    "-e", f"PGSSLROOTCERT={machinedb.CONF_IN_DB}/{PRIMARY_CA}", "-e", f"PGAPPNAME={APPLICATION}-copy",
                    SERVICE, "pg_basebackup", "-h", str(rec["host"]), "-p", str(rec["port"]), "-U", ROLE,
                    "-D", machinedb.PGDATA_IN_DB, "-Fp", "-X", "stream", "-S", SLOT, "-c", "fast", "--no-password",
                    check=False, timeout=COPY_TIMEOUT)
    if done.returncode:
        raise ClusterError(f"the copy of the cluster of {rec.get('link')} failed: {(done.stderr or done.stdout).strip()[-600:]}")
    seconds = round(time.monotonic() - started, 1)
    size = machinedb_sizes(project)
    _save_hosted_record(project, {**rec, "copy_seconds": seconds, "size": size})
    return {"seconds": seconds, "size": size}


def tree_size(*folders: Path) -> int:
    """The bytes of the files under folders."""
    total = 0
    for folder in folders:
        for root, _dirs, files in os.walk(folder):
            for name in files:
                try:
                    total += os.lstat(os.path.join(root, name)).st_size
                except OSError:
                    pass
    return total


def machinedb_sizes(project: Path) -> int:
    return tree_size(project / "data", project / "roots")


def _receiving(project: Path) -> bool:
    return replica_sql(project, "SELECT status FROM pg_stat_wal_receiver;\n", check=False).strip() == "streaming"


def _ready(project: Path, timeout: float = READY_TIMEOUT) -> None:
    end = time.monotonic() + timeout
    while _compose(project, "exec", "-T", SERVICE, "pg_isready", "-q", "-h", SOCKET, check=False, timeout=60).returncode:
        if time.monotonic() > end:
            log = _compose(project, "--ansi", "never", "logs", "--no-log-prefix", "--tail", "20", SERVICE, check=False)
            raise ClusterError(f"the read replica does not answer within {timeout:.0f} s: "
                               f"{((log.stdout or '') + (log.stderr or '')).strip()[-400:]}")
        time.sleep(1)


def start(major: str) -> dict:
    """The copy started as a hot standby that follows its primary, and seen
    to receive from it."""
    project = hosted_project(major)
    rec = hosted_record(project)
    uid, gid = machinedb.pg_user()
    pgdata = project / "data" / "pgdata"
    if not (pgdata / "PG_VERSION").is_file():
        raise ClusterError("the replica holds no copy yet")
    signal = pgdata / "standby.signal"
    if rec.get("state") == "copying" and not signal.exists():
        signal.write_text("")
        os.chown(signal, uid, gid)
        os.chmod(signal, 0o600)
    started = time.monotonic()
    _compose(project, "up", "-d", "--remove-orphans", timeout=600)
    _ready(project)
    end = time.monotonic() + STREAM_WAIT
    while not _receiving(project):
        if time.monotonic() > end:
            raise ClusterError("the read replica started but does not receive from its primary: "
                               + (_log_tail(project) or "no reason in its log"))
        time.sleep(1)
    version = replica_sql(project, "SHOW server_version;\n").strip()
    rec = {**rec, "state": "ready", "ready_at": _now(), "version": version, "synced": True}
    _save_hosted_record(project, rec)
    return {"state": "ready", "port": rec.get("own_port"), "address": rec.get("address"), "version": version,
            "seconds": round(time.monotonic() - started, 1), "ca": machinedb.ca_certificate(project),
            "size": rec.get("size"), "copy_seconds": rec.get("copy_seconds")}


def _log_tail(project: Path) -> str:
    """The last line of the replica's log that says what is wrong (for a
    job of the machine's admin: it names PostgreSQL's own things)."""
    log = _compose(project, "--ansi", "never", "logs", "--no-log-prefix", "--tail", "40", SERVICE, check=False, timeout=60)
    lines = [line.strip() for line in ((log.stdout or "") + (log.stderr or "")).splitlines()
             if "FATAL" in line or "could not" in line or "ERROR" in line]
    return re.sub(r"^\S+ \S+ \S+ \[\d+\] ", "", lines[-1])[:300] if lines else ""


# why a replica does not receive, as the pages say it: never PostgreSQL's own words (UX-010 §8)
REASONS = (("gone", ("has already been removed", "replication slot", "can no longer get changes", "invalidated")),
           ("refused", ("pg_hba.conf", "password authentication failed", "does not exist", "certificate verify failed",
                        "permission denied")),
           ("unreachable", ("could not connect", "Connection refused", "timed out", "timeout expired", "No route to host",
                            "server closed the connection", "could not receive data", "Network is unreachable")))


def reason_code(text: str) -> str:
    """gone (the primary no longer keeps what the replica needs), refused,
    unreachable, or unknown, from the last line of the replica's log."""
    for code, marks in REASONS:
        if any(mark in text for mark in marks):
            return code
    return "unknown"


def reason(project: Path) -> str:
    return reason_code(_log_tail(project))


def sync(major: str, spec: dict) -> dict:
    """What changed on the primary's side followed: pg_hba.conf for the
    instances it has now, the folder of each tablespace it has now (a
    replica stopped asking for one starts again by itself: its container
    restarts)."""
    project = hosted_project(major)
    rec = hosted_record(project)
    uid, gid = machinedb.pg_user()
    environments = sorted(machinedb._env(str(e)) for e in spec.get("environments") or [])
    changed = []
    made = 0
    if isinstance(spec.get("grants"), dict) and spec["grants"] != rec.get("lent"):
        # who the primary lets in to which database: what this copy lets in when it is promoted
        rec = {**rec, "lent": lent_checked(spec["grants"])}
        _save_hosted_record(project, rec)
    for location in spec.get("tablespaces") or []:
        try:
            where = under_roots(str(location), list(rec.get("roots") or []), project)
        except ClusterError:
            continue
        if not where.exists():
            _owned_folder(where, uid, gid, project / "roots")
            made += 1
    if made:
        changed.append(f"{made} folders of tablespaces")
    text = hosted_hba(rec, environments)
    path = project / "conf" / "pg_hba.conf"
    if environments != list(rec.get("environments") or []) or path.read_text(encoding="utf-8") != text:
        machinedb.write(path, text, mode=0o640, uid=0, gid=gid)
        replica_sql(project, "SELECT pg_reload_conf();\n", check=False)
        _save_hosted_record(project, {**rec, "environments": environments})
        changed.append("pg_hba.conf")
    return {"changed": changed}


def lent_checked(value) -> dict:
    """The grants of a cluster as its machine said them, each checked: a
    role's name, one IPv4 address; what is not right is left out."""
    found = {}
    for role, grant in (value.items() if isinstance(value, dict) else ()):
        try:
            name = machinedb._env(str(role))
            come = machinedb.address((grant or {}).get("address"))
        except (ClusterError, AttributeError):
            continue
        found[name] = {**{k: str(grant.get(k) or "")[:128] for k in ("link", "by", "at", "workspace", "instance")},
                       "address": come, "revoked": bool(grant.get("revoked"))}
    return found


def hosted_state(major: str) -> dict:
    """What this machine sees of the replica it hosts: its record, whether
    its container runs, whether it receives and why not, its size, the
    connections that read from it, the disk free here."""
    project = hosted_project(major)
    rec = hosted_record(project)
    answer = {"exists": True, "major": major, **{k: rec.get(k) for k in (
        "link", "by", "host", "port", "address", "own_port", "primary_address", "state", "at", "ready_at",
        "promoted_at", "returned_at", "copy_seconds", "size", "expected", "version", "synced", "environments", "source")},
        "storage": (hosted().get(major) or {}).get("storage"), "running": False, "receiving": False, "reason_code": "",
        "connections": None, "max_connections": None, "free": shutil.disk_usage(project).free}
    # what it takes on this machine's disk (its data, its tablespaces, the changes it keeps); while it is
    # copied, what is copied so far; then what the copy was when it ended
    answer["disk"] = machinedb_sizes(project)
    if rec.get("state") == "copying":
        answer["copied"] = answer["disk"]
        return answer
    answer["copied"] = rec.get("size")
    if rec.get("state") == "promoted":
        # the databases other machines use live here now (UX-011 §B): who comes in from where
        answer["lent"] = [{"name": role, **{k: grant.get(k) for k in GRANT_KEYS},
                           "local": own_address(str(grant.get("address") or ""))}
                          for role, grant in sorted(hosted_grants(project).items())]
    if rec.get("state") == "returned":
        # the cluster it had replaced came back: stopped for good, its data of that moment
        return answer
    if returning(rec).get("state") in ("copying", "ready", "stopped"):
        # the cluster it replaced comes back (UX-011 §A): what this copy sees of it, with what the rows say
        back = return_state(major)
        answer["return"] = {**back, "verdict": verdict(back)}
    done = _compose(project, "ps", "--format", "{{.State}}", SERVICE, check=False, timeout=60)
    answer["running"] = (done.stdout or "").strip() == "running"
    if not answer["running"]:
        answer["reason_code"] = "stopped"
        return answer
    line = replica_sql(project, "SELECT pg_is_in_recovery(), coalesce((SELECT status FROM pg_stat_wal_receiver), ''), "
                                "(SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'client backend' AND "
                                "pid <> pg_backend_pid()), current_setting('max_connections'), "
                                "(SELECT sum(pg_database_size(oid)) FROM pg_database);\n", check=False).strip().split("|")
    if len(line) != 5:
        answer["reason_code"] = "silent"
        return answer
    answer.update(in_recovery=line[0] == "t", receiving=line[1] == "streaming", connections=int(line[2] or 0),
                  max_connections=int(line[3] or 0), size=int(line[4] or 0) or rec.get("size"))
    if answer["in_recovery"] and not answer["receiving"]:
        answer["reason_code"] = reason(project)
    return answer


def promote(major: str, lent: dict | None = None) -> dict:
    """The copy takes the writes: all it received applied, then pg_promote.
    Only one that was in sync once: a copy not whole is never promoted.
    lent: the databases the primary had given to other machines, as it said
    them when it was stopped (else as this copy read them last): from now
    they live here, and the copy lets the same roles in from the same
    addresses."""
    project = hosted_project(major)
    rec = hosted_record(project)
    if rec.get("state") == "promoted":
        return {"promoted_at": rec.get("promoted_at"), "already": True, "lent": sorted(hosted_grants(project))}
    if rec.get("state") != "ready" or not rec.get("synced"):
        raise ClusterError("the replica is not a whole copy yet: it is promoted only after it was in sync")
    started = time.monotonic()
    end = started + PROMOTE_WAIT
    while replica_sql(project, "SELECT pg_last_wal_receive_lsn() IS NOT DISTINCT FROM pg_last_wal_replay_lsn();\n").strip() != "t":
        if time.monotonic() > end:
            raise ClusterError(f"the replica has not applied what it received after {PROMOTE_WAIT} s: not promoted")
        time.sleep(0.5)
    if replica_sql(project, f"SELECT pg_promote(true, {PROMOTE_WAIT});\n").strip() != "t":
        raise ClusterError("PostgreSQL did not promote the replica")
    if replica_sql(project, "SELECT pg_is_in_recovery();\n").strip() != "f":
        raise ClusterError("the replica is still a replica after pg_promote")
    at = _now()
    rec = {**rec, "state": "promoted", "promoted_at": at, "lent_at": at}
    _save_hosted_record(project, rec)
    given = lent_checked(lent if lent is not None else rec.get("lent"))
    machinedb._save_grants(project, given)
    _write_hosted_hba(project, rec)
    return {"promoted_at": at, "already": False, "seconds": round(time.monotonic() - started, 1), "lent": sorted(given)}


def adopt(major: str, lent: dict) -> dict:
    """A copy promoted before it took the databases its primary had given to
    other machines (by a release that did not, or while that machine had
    said none): it takes them now, once, as its primary's machine says them."""
    project, rec = _hosted_promoted(major)
    if "lent_at" in rec:
        return {"adopted": False, "lent": sorted(hosted_grants(project))}
    given = lent_checked(lent)
    rec = {**rec, "lent_at": _now()}
    _save_hosted_record(project, rec)
    machinedb._save_grants(project, given)
    _write_hosted_hba(project, rec)
    return {"adopted": True, "lent": sorted(given)}


def hosted_grant_set(major: str, role: str, revoked: bool) -> dict:
    """A database of the promoted copy another machine uses, revoked (its
    line of pg_hba gone, its connections closed; the database and its role
    stay) or allowed again, from the same address: as on a cluster."""
    project, rec = _hosted_promoted(major)
    lent = hosted_grants(project)
    name = machinedb._env(role)
    if name not in lent:
        raise ClusterError(f"{role} is no database of another machine on this copy")
    machinedb._save_grants(project, {**lent, name: {**lent[name], "revoked": bool(revoked)}})
    _write_hosted_hba(project, rec)
    closed = 0
    if revoked:
        closed = len(replica_sql(project, f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = '{name}';\n",
                                 check=False).split())
    return {"role": name, "revoked": bool(revoked), "address": lent[name].get("address"), "closed": closed}


def hosted_grant_remove(major: str, role: str) -> dict:
    """A database of the promoted copy another machine uses taken away: its
    line, the database, its role."""
    project, rec = _hosted_promoted(major)
    lent = hosted_grants(project)
    name = machinedb._env(role)
    if name not in lent:
        raise ClusterError(f"{role} is no database of another machine on this copy")
    left = {k: v for k, v in lent.items() if k != name}
    machinedb._save_grants(project, left)
    _write_hosted_hba(project, rec)
    replica_sql(project, f"DROP DATABASE IF EXISTS {machinedb.ident(name)} WITH (FORCE);\n")
    replica_sql(project, f"DROP ROLE IF EXISTS {machinedb.ident(name)};\n")
    return {"role": name, "dropped": [name], "address": lent[name].get("address"), "grants_left": len(left)}


def remove(major: str) -> dict:
    """The replica deleted: its container and network, its folder, its
    record; the cluster's user and slice too when this machine has no
    cluster of its own."""
    project = hosted_project(major)
    rec = hosted_record(project)
    size = machinedb_sizes(project)
    _compose(project, "down", "--remove-orphans", "-v", check=False, timeout=600)
    shutil.rmtree(project)
    parent = project.parent
    if parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()
    _save_hosted({k: v for k, v in hosted().items() if k != major})
    subprocess.run([machinedb._docker(), "network", "rm", f"{project_name(major)}_db"], env=ENV, capture_output=True)
    if not machinedb.registry() and not hosted():
        machinedb._remove_units()
        subprocess.run([shutil.which("userdel", path=ENV["PATH"]) or "userdel", machinedb.USER], env=ENV, capture_output=True)
    return {"removed": str(project), "freed": size, "link": rec.get("link"), "state": rec.get("state")}


def firewall_state(subnet_of=None) -> dict:
    """{bridge: {"from": [addresses], "to": [[address, port]]}} of the
    replicas this machine hosts: reached from the primary's machine only,
    and going out to the primary's port only (machinedb.firewall_rules). A
    promoted one is reached also from the addresses of the databases other
    machines use on it (UX-011 §B); one of them used by an instance of this
    very machine, from the network of that instance's project ("local":
    subnet_of(workspace, instance) says it, "" when there is none)."""
    found = {}
    for major, entry in sorted(hosted().items()):
        rec = hosted_record(Path(str(entry.get("project") or "")))
        try:
            primary = machinedb.address(rec.get("primary_address"))
            port = int(rec.get("port") or 0)
        except (ClusterError, ValueError):
            continue
        if 1 <= port <= 65535:
            # a promoted one no longer goes out: nothing to follow; one the cluster came back from is stopped
            # for good, and no one reaches it
            out = [] if rec.get("state") in ("promoted", "returned") else [[primary, port]]
            coming, local = ([] if rec.get("state") == "returned" else [primary]), []
            if rec.get("state") == "promoted":
                for grant in hosted_grants(Path(str(entry.get("project") or ""))).values():
                    if grant.get("revoked"):
                        continue
                    try:
                        come = machinedb.address(grant.get("address"))
                    except ClusterError:
                        continue
                    if not own_address(come):
                        coming.append(come)
                    elif subnet_of is not None:
                        net = subnet_of(str(grant.get("workspace") or ""), str(grant.get("instance") or ""))
                        if net:
                            local.append(net)
            found[bridge_name(major)] = {"from": sorted(set(coming)), "to": out, **({"local": sorted(set(local))} if local else {})}
    return {**found, **published_state()}


def published_state() -> dict:
    """{"port<n>": {"from": [address], "subnet"}} of the dedicated databases
    of this machine's instances that listen for a copy on another machine
    (machinedb.published): that port is reached from the address of the
    machine that hosts the copy, and from no other; from none when the
    replica was dropped (the database goes on listening, as a cluster
    does). One whose copy was promoted is stopped and its project has no
    database service: no entry. One whose instance is gone is forgotten."""
    found, gone = {}, []
    for key, entry in sorted(machinedb.dedicated_sources().items()):
        if not Path(str(entry.get("project") or "")).is_dir():
            gone.append(key)
            continue
        try:
            src = Dedicated(key)
            listen, rec = src.listening(), record(src.folder) or {}
            if not listen or rec.get("state") == "replaced" or not src.has_database():
                continue
            found[f"port{int(listen['port'])}"] = {"from": [machinedb.address(rec["address"])] if rec else [],
                                                   "subnet": src.subnet()}
        except (ClusterError, OSError, ValueError, KeyError):
            continue
    if gone:
        try:
            _save_sources({k: v for k, v in machinedb.dedicated_sources().items() if k not in gone})
        except OSError:
            pass
    return found


# --- the return after a promotion (UX-011 §A) ------------------------------------------------
# R hosts the promoted copy, P is the machine whose cluster it replaced. P asks, through a link of its
# own to R (the machine that hosts the copy asks the one that has the data, as a replica is asked):
# R never calls P. On R, return_*: the promoted copy lets P's one address in for the replication and
# holds its changes for it, with a cap; then, to complete, it is stopped for good. On P, back_*: the
# cluster's old data is set aside (never deleted before the return is complete, put back when it is
# cancelled), the copy of R is taken into the cluster's own project and follows R read only; to
# complete, P waits until it applied the last thing R wrote before it stopped, then takes the writes.

def _hosted_promoted(major: str) -> tuple[Path, dict]:
    project = hosted_project(major)
    rec = hosted_record(project)
    if rec.get("state") != "promoted":
        raise ClusterError("this copy was not promoted: no cluster comes back from it")
    return project, rec


def hosted_tablespaces(project: Path) -> list[str]:
    out = replica_sql(project, "SELECT pg_tablespace_location(oid) FROM pg_tablespace "
                               "WHERE spcname NOT IN ('pg_default', 'pg_global');\n", check=False)
    return sorted(line.strip() for line in out.splitlines() if machinedb.SAFE_PATH.fullmatch(line.strip()))


def return_facts(major: str) -> dict:
    """What the machine this copy replaced reads before it prepares its
    return: the size of the copy, whether it answers and can be followed,
    the one address it lets that machine in from, where its tablespaces are."""
    project, rec = _hosted_promoted(major)
    answer = {"major": major, "running": False, "size": 0, "version": "", "port": rec.get("own_port"),
              "host": rec.get("address"), "address": rec.get("primary_address"), "why": "", "tablespaces": [],
              "promoted_at": rec.get("promoted_at"), "under_way": returning(rec).get("state") in ("copying", "ready", "stopped")}
    line = replica_sql(project, "SELECT (SELECT sum(pg_database_size(oid)) FROM pg_database), "
                                "current_setting('server_version');\n", check=False).strip().split("|")
    if len(line) != 2:
        answer["why"] = "the promoted copy does not answer"
        return answer
    answer.update(size=int(line[0] or 0), version=line[1], running=True, tablespaces=hosted_tablespaces(project),
                  why=unfit_of(replica_sql(project, UNFIT_SQL, check=False).strip().split("|"), "the promoted copy"))
    return answer


def _return_drop(project: Path) -> int:
    """What the copy keeps for the cluster that comes back taken away: the
    place that holds its changes (their bytes), the role's connections, the role."""
    held = 0
    try:
        held = int(float(replica_sql(project, "SELECT coalesce((SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) "
                                              f"FROM pg_replication_slots WHERE slot_name = '{BACK_SLOT}'), 0);\n").strip() or 0))
    except (ClusterError, ValueError):
        pass
    for _ in range(10):
        replica_sql(project, f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = '{ROLE}';\n", check=False)
        active = replica_sql(project, f"SELECT count(*) FROM pg_replication_slots WHERE slot_name = '{BACK_SLOT}' AND active;\n",
                             check=False).strip()
        if active in ("0", ""):
            break
        time.sleep(0.5)
    replica_sql(project, f"ALTER ROLE {machinedb.ident(ROLE)} NOLOGIN;\n", check=False)
    replica_sql(project, f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = '{ROLE}';\n", check=False)
    replica_sql(project, f"SELECT pg_drop_replication_slot(slot_name) FROM pg_replication_slots WHERE slot_name = '{BACK_SLOT}';\n")
    replica_sql(project, f"DROP ROLE IF EXISTS {machinedb.ident(ROLE)};\n")
    return max(0, held)


def return_allow(major: str, spec: dict) -> dict:
    """The cluster this copy replaced let in to copy it and follow it: spec
    {"verifier" (of a password made on that machine), "link", "by",
    "cap_gb"}. The role of the replication with that verifier, a place
    that holds the changes from now, its cap, one line of pg_hba.conf for
    the one address this copy already lets that machine in from. What that
    machine needs to connect, without any secret. Nothing restarts."""
    project, rec = _hosted_promoted(major)
    back = returning(rec)
    if back.get("state") in ("copying", "ready", "stopped"):
        raise ClusterError(f"a return to {rec.get('link')} is under way already: cancel it first")
    verifier = machinedb.literal(str(spec.get("verifier") or ""), machinedb.VERIFIER)
    link, by = str(spec.get("link") or ""), str(spec.get("by") or "")
    if not machinedb.LINK_NAME.fullmatch(link):
        raise ClusterError("a return is asked by the machine whose cluster comes back, through its link")
    cap = cap_gb(spec.get("cap_gb", CAP_GB))
    source = machinedb.address(rec.get("primary_address"))
    facts = return_facts(major)
    if not facts["running"] or facts["why"]:
        raise ClusterError(facts["why"] or "the promoted copy does not answer")
    recorded = False
    try:
        # what an attempt that did not end left, and the role the copy came with (its password was this machine's)
        _return_drop(project)
        replica_sql(project, f"CREATE ROLE {machinedb.ident(ROLE)} LOGIN REPLICATION NOSUPERUSER NOCREATEDB "
                             f"NOCREATEROLE CONNECTION LIMIT 4 PASSWORD {verifier};\n")
        replica_sql(project, f"SELECT pg_create_physical_replication_slot('{BACK_SLOT}', true);\n")
        replica_sql(project, f"ALTER SYSTEM SET max_slot_wal_keep_size = '{int(cap * 1024)}MB';\nSELECT pg_reload_conf();\n")
        rec = {**rec, "return": {"link": link, "by": by, "address": source, "at": _now(), "cap_gb": cap, "state": "copying"}}
        _save_hosted_record(project, rec)
        recorded = True
        _write_hosted_hba(project, rec)
    except ClusterError:
        if recorded:
            rec = {k: v for k, v in rec.items() if k != "return"}
            _save_hosted_record(project, rec)
        try:
            _return_drop(project)
            replica_sql(project, "ALTER SYSTEM RESET max_slot_wal_keep_size;\nSELECT pg_reload_conf();\n", check=False)
            _write_hosted_hba(project, rec)
        except ClusterError:
            pass
        raise
    return {"port": rec.get("own_port"), "host": rec.get("address"), "address": source, "cap_gb": cap,
            "ca": machinedb.ca_certificate(project), "version": facts["version"], "size": facts["size"],
            "tablespaces": facts["tablespaces"]}


def return_state(major: str, now: float | None = None) -> dict:
    """What the promoted copy sees of the cluster that comes back ({"exists":
    False} with none, and who cancelled the last one): copying, connected or
    since when not, the seconds behind, what it holds for it against the cap."""
    project = hosted_project(major)
    rec = hosted_record(project)
    back = returning(rec)
    if back.get("state") not in ("copying", "ready", "stopped"):
        return {"exists": False, "major": major, "copy": rec.get("state"), "returned_at": rec.get("returned_at"),
                "cancelled": {k: back.get(k) for k in ("by", "at")} if back.get("state") == "cancelled" else None}
    answer = {"exists": True, "major": major, "copy": rec.get("state"), **{k: back.get(k) for k in (
        "link", "by", "address", "at", "cap_gb", "state", "ready_at", "stopped_at")},
        "connected": False, "lag": None, "since": None, "held": 0, "wal_status": "", "progress": None, "rate": None,
        "cap": int(float(back.get("cap_gb") or CAP_GB) * GB), "free": shutil.disk_usage(project).free}
    if back["state"] == "stopped":
        return answer
    try:
        found = json.loads(replica_sql(project, state_sql(BACK_SLOT)).strip() or "{}")
    except (ClusterError, ValueError):
        return {**answer, "unread": True}
    answer = _seen(project, found, back["state"] == "ready", back.get("ready_at") or back.get("at"), answer, now)
    if back["state"] == "copying" and answer.pop("following", False):
        # the copy ended and the cluster follows: said here by what PostgreSQL sees, no one has to tell
        back = {**back, "state": "ready", "ready_at": _now()}
        _save_hosted_record(project, {**rec, "return": back})
        answer.update(state="ready", ready_at=back["ready_at"], connected=True, lag=0)
    answer.pop("following", None)
    return answer


def _control(project: Path) -> dict:
    """What pg_controldata says of the copy's data, while its PostgreSQL is stopped."""
    done = _compose(project, "run", "--rm", "--no-deps", "-T", "-e", "LC_ALL=C", SERVICE, "pg_controldata",
                    "-D", machinedb.PGDATA_IN_DB, check=False, timeout=120)
    found = {}
    for line in (done.stdout or "").splitlines():
        key, sep, value = line.partition(":")
        if sep:
            found[key.strip()] = value.strip()
    return found


def return_stop(major: str) -> dict:
    """The first step of Complete the return…: the promoted copy stopped (a
    checkpoint, PostgreSQL's fast stop: the cluster that follows gets
    everything first), and where its last record is: the cluster that comes
    back takes the writes only once it applied it. One that did not stop
    clean runs again, and nothing changes."""
    project, rec = _hosted_promoted(major)
    back = returning(rec)
    if back.get("state") == "stopped":
        return {"stopped": True, "already": True, "checkpoint": back.get("checkpoint"), "stopped_at": back.get("stopped_at")}
    if back.get("state") != "ready":
        raise ClusterError("the return is not a whole copy yet: it is completed only after it was in sync")
    replica_sql(project, "CHECKPOINT;\n", check=False)
    started = time.monotonic()
    _compose(project, "stop", "-t", "120", SERVICE, timeout=300)
    done = _compose(project, "ps", "--format", "{{.State}}", SERVICE, check=False)
    control = {} if (done.stdout or "").strip() == "running" else _control(project)
    checkpoint = control.get("Latest checkpoint location", "")
    if control.get("Database cluster state") != "shut down" or not LSN.fullmatch(checkpoint):
        _compose(project, "up", "-d", "--remove-orphans", check=False, timeout=600)
        raise ClusterError("the promoted copy did not stop clean: it runs again and nothing changed")
    at = _now()
    _save_hosted_record(project, {**rec, "return": {**back, "state": "stopped", "stopped_at": at, "checkpoint": checkpoint}})
    return {"stopped": True, "already": False, "checkpoint": checkpoint, "stopped_at": at,
            "seconds": round(time.monotonic() - started, 1)}


def return_resume(major: str) -> dict:
    """The copy stopped by return_stop runs again: the cluster that comes
    back did not get everything in time, the instances stay here."""
    project, rec = _hosted_promoted(major)
    back = returning(rec)
    if back.get("state") != "stopped":
        return {"resumed": False, "state": back.get("state")}
    started = time.monotonic()
    _compose(project, "up", "-d", "--remove-orphans", timeout=600)
    _ready(project)
    _save_hosted_record(project, {**rec, "return": {k: v for k, v in back.items() if k not in ("stopped_at", "checkpoint")}
                                  | {"state": "ready"}})
    return {"resumed": True, "seconds": round(time.monotonic() - started, 1)}


def return_done(major: str) -> dict:
    """The cluster came back and took the writes: this copy, stopped, stays
    stopped for good (its container is taken away, its data stays: it never
    starts by itself), until it is made a replica again or dropped."""
    project = hosted_project(major)
    rec = hosted_record(project)
    if rec.get("state") == "returned":
        return {"returned_at": rec.get("returned_at"), "already": True}
    back = returning(rec)
    if rec.get("state") != "promoted" or back.get("state") != "stopped":
        raise ClusterError("this copy was not stopped for a return: it is the database of its instances still")
    _compose(project, "down", "--remove-orphans", check=False, timeout=600)
    at = _now()
    _save_hosted_record(project, {**rec, "state": "returned", "returned_at": at, "return": {**back, "state": "done"}})
    return {"returned_at": at, "already": False}


def return_cancel(major: str, by: str = "") -> dict:
    """The return cancelled on the copy's side: what it keeps for the
    cluster that comes back is taken away (the place that held its changes,
    freed; the role; its line of pg_hba.conf; the cap). Never while the copy
    is stopped to complete it: the two would take writes."""
    project, rec = _hosted_promoted(major)
    back = returning(rec)
    if back.get("state") == "stopped":
        raise ClusterError(f"the return is being completed: this copy is stopped for it and {rec.get('link')} takes the "
                           "writes. It is not started again from here")
    if back.get("state") not in ("copying", "ready"):
        return {"cancelled": False, "freed": 0}
    rec = {**rec, "return": {"state": "cancelled", "by": str(by or ""), "at": _now(), "link": back.get("link")}}
    _save_hosted_record(project, rec)
    _write_hosted_hba(project, rec)
    freed = _return_drop(project)
    replica_sql(project, "ALTER SYSTEM RESET max_slot_wal_keep_size;\nSELECT pg_reload_conf();\n", check=False)
    replica_sql(project, "CHECKPOINT;\n", check=False)
    return {"cancelled": True, "freed": freed, "link": rec.get("link")}


# on the machine whose cluster comes back (P): the cluster's own project

def _save_back(project: Path, back: dict | None) -> None:
    path = project / machinedb.RETURN_FILE
    if back is None:
        path.unlink(missing_ok=True)
        return
    machinedb.write(path, json.dumps(back, indent=1, sort_keys=True) + "\n", mode=0o644)


def _aside(folder: Path) -> Path:
    return folder.with_name(folder.name + ASIDE)


def _data_folders(project: Path) -> list[Path]:
    """Where the cluster's data is: its own folder, and its tablespaces on the workspaces' disks."""
    envs = machinedb.environments(project)
    return [project / "data" / "pgdata",
            *sorted({Path(str(e.get("mount"))) / machinedb.WS_FOLDER / "ts" for e in envs.values() if e.get("mount")})]


def _replaced(major: str) -> tuple[Path, dict]:
    project = machinedb.project_of(major)
    rec = record(project)
    if not rec or rec.get("state") != "replaced":
        raise ClusterError(f"the machine's PostgreSQL {major} was not replaced by its replica: there is nothing to come "
                           "back from")
    return project, rec


def back_facts(major: str) -> dict:
    """What Prepare the return… says of this machine before the click: the
    data that is set aside (since when it is stopped, its size), the room
    here and on each workspace's disk, where the promoted copy is reached."""
    project, rec = _replaced(major)
    folders = _data_folders(project)
    back = machinedb.returning(project)
    return {"major": major, "machine": rec.get("link"), "host": rec.get("host"), "port": rec.get("port"),
            "aside": {"at": rec.get("replaced_at"), "size": tree_size(*folders)},
            "free": shutil.disk_usage(project).free,
            "disks": [{"mount": str(f.parent.parent), "size": tree_size(f), "free": shutil.disk_usage(f.parent).free}
                      for f in folders[1:] if f.is_dir()],
            "listening": bool(machinedb.listening(project)),
            "return": {k: back.get(k) for k in ("link", "state", "at")} if back else None}


def back_room(size: int, facts: dict, reserve: float) -> str:
    """Why the copy of the promoted cluster (size) does not fit beside the
    data set aside, "" when it does: on the machine's disk, and on each
    workspace's disk for its own part (as much as it holds now, a fifth more)."""
    fits = room(size, facts["free"], reserve)
    if not fits["fits"]:
        return (f"no room: the copy takes about {fits['need'] // MB} MB, the data set aside stays "
                f"({int(facts['aside']['size']) // MB} MB), {facts['free'] / GB:.1f} GB are free here and {reserve:g} GB "
                "must stay free")
    for disk in facts.get("disks") or []:
        need = int(disk["size"]) + int(disk["size"]) // 5 + 64 * MB
        if disk["free"] < need:
            return (f"no room on the disk of the workspace {Path(disk['mount']).name}: its part of the copy takes "
                    f"about {need // MB} MB there beside the data set aside, and {disk['free'] // MB} MB are free")
    return ""


def back_prepare(major: str, spec: dict) -> dict:
    """The cluster's project made ready to take the copy of the promoted
    cluster: spec {"link" (this machine's link to the machine that hosts it),
    "by", "host", "port", "ca" (how the copy is reached and verified),
    "password" (of the replication's role, made here), "size",
    "tablespaces", "reserve_gb"}. The room checked, the cluster's data set
    aside (renamed where it is: nothing is copied, nothing deleted), empty
    folders in its place, the password and the CA where the cluster's
    container reads them, the record of the return."""
    project, rec = _replaced(major)
    if (machinedb.returning(project) or {}).get("state") in ("copying", "ready"):
        raise ClusterError("a return is under way already: cancel it first")
    if not machinedb.listening(project):
        raise ClusterError("the cluster listens on no port: it has no network to reach the promoted copy through")
    link = str(spec.get("link") or "")
    if not machinedb.LINK_NAME.fullmatch(link):
        raise ClusterError("a return is prepared through a link of this machine to the one that hosts the copy")
    host = machinedb.reached_as([spec.get("host")])[0]
    port = int(spec.get("port") or 0)
    if not 1024 <= port <= 65535:
        raise ClusterError(f"{port} is not the port of the promoted copy")
    ca = certificate(spec.get("ca"))
    password = str(spec.get("password") or "")
    if not PASSWORD.fullmatch(password):
        raise ClusterError("the password of the replication's role is not of its form")
    size = int(spec.get("size") or 0)
    reserve = reserve_gb(spec.get("reserve_gb", RESERVE_GB))
    done = machinedb._run(project, "ps", "--format", "{{.State}}", SERVICE, check=False)
    if (done.stdout or "").strip() == "running":
        raise ClusterError("the machine's PostgreSQL runs: only a cluster stopped by a promotion comes back")
    facts = back_facts(major)
    why = back_room(size, facts, reserve)
    if why:
        raise ClusterError(why)
    folders = _data_folders(project)
    for location in spec.get("tablespaces") or []:
        if Path(str(location)) not in folders[1:]:
            raise ClusterError("the promoted copy has data in a folder this machine's cluster does not have: the "
                               "return cannot put it where it was")
    if any(_aside(folder).exists() for folder in folders):
        raise ClusterError("data set aside by a return that did not end is still here: cancel that return first")
    uid, gid = machinedb.pg_user()
    moved = []
    try:
        for folder in folders:
            if folder.is_symlink() or not folder.is_dir():
                continue
            os.rename(folder, _aside(folder))
            moved.append(folder)
            machinedb._folder(folder, 0o700, uid, gid)
        machinedb.write(project / "secret" / PGPASS, f"*:*:*:{ROLE}:{password}\n", mode=0o600, uid=uid, gid=gid)
        machinedb.write(project / "conf" / PRIMARY_CA, ca, mode=0o644, uid=0, gid=gid)
        _save_back(project, {"link": link, "by": str(spec.get("by") or ""), "host": host, "port": port,
                             "address": _primary_address(host), "state": "copying", "at": _now(), "expected": size,
                             "reserve_gb": reserve, "aside": facts["aside"], "machine": rec.get("link")})
    except BaseException:
        _put_aside_back(project, moved)
        raise
    return {"major": major, "aside": facts["aside"], "room": room(size, facts["free"], reserve)}


def _put_aside_back(project: Path, folders: list[Path]) -> int:
    """The data set aside back where it was, what took its place deleted
    (its bytes); the password and the CA of the return taken away."""
    freed = 0
    for folder in folders:
        aside = _aside(folder)
        if aside.is_dir() and not aside.is_symlink():
            # only what has its old self beside it is deleted: never the only copy
            freed += tree_size(folder)
            shutil.rmtree(folder, ignore_errors=True)
            os.rename(aside, folder)
    (project / "secret" / PGPASS).unlink(missing_ok=True)
    (project / "conf" / PRIMARY_CA).unlink(missing_ok=True)
    return freed


def _back(project: Path, *states: str) -> dict:
    back = machinedb.returning(project) or {}
    if back.get("state") not in states:
        raise ClusterError("no return is at this step" if back else "no return is under way")
    return back


def back_copy(major: str) -> dict:
    """The promoted copy copied into the cluster's own project (pg_basebackup
    over TLS, its certificate verified), then made a copy that follows it:
    the settings the cluster had (its tuning, its recovery at a minute) put
    back in place of the copy's, with where it follows from."""
    project = machinedb.project_of(major)
    back = _back(project, "copying")
    pgdata = project / "data" / "pgdata"
    if any(pgdata.iterdir()):
        raise ClusterError("the cluster's folder is not empty: cancel the return and prepare it again")
    started = time.monotonic()
    done = machinedb._run(project, "run", "--rm", "--no-deps", "-T",
                          "-e", f"PGPASSFILE={machinedb.SECRETS_IN_DB}/{PGPASS}", "-e", "PGSSLMODE=verify-full",
                          "-e", f"PGSSLROOTCERT={machinedb.CONF_IN_DB}/{PRIMARY_CA}", "-e", f"PGAPPNAME={BACK_APPLICATION}-copy",
                          SERVICE, "pg_basebackup", "-h", str(back["host"]), "-p", str(back["port"]), "-U", ROLE,
                          "-D", machinedb.PGDATA_IN_DB, "-Fp", "-X", "stream", "-S", BACK_SLOT, "-c", "fast", "--no-password",
                          check=False, timeout=COPY_TIMEOUT)
    if done.returncode:
        raise ClusterError(f"the copy of the promoted cluster failed: {(done.stderr or done.stdout).strip()[-600:]}")
    uid, gid = machinedb.pg_user()
    auto, kept = pgdata / "postgresql.auto.conf", _aside(pgdata) / "postgresql.auto.conf"
    source = kept if kept.is_file() else auto
    lines = [line for line in source.read_text(encoding="utf-8").splitlines()
             if not re.match(r"\s*primary_(conninfo|slot_name)\s*=", line)]
    lines += [f"primary_conninfo = '{conninfo(str(back['host']), int(back['port']), BACK_APPLICATION)}'",
              f"primary_slot_name = '{BACK_SLOT}'"]
    machinedb.write(auto, "\n".join(lines) + "\n", mode=0o600, uid=uid, gid=gid)
    machinedb.write(pgdata / "standby.signal", "", mode=0o600, uid=uid, gid=gid)
    seconds = round(time.monotonic() - started, 1)
    size = tree_size(*_data_folders(project))
    _save_back(project, {**back, "copy_seconds": seconds, "size": size})
    return {"seconds": seconds, "size": size}


def _following(project: Path) -> bool:
    try:
        return machinedb.sql(project, "postgres", "SELECT status FROM pg_stat_wal_receiver;\n").strip() == "streaming"
    except ClusterError:
        return False


def back_start(major: str) -> dict:
    """The cluster started on the copy, read only, and seen to receive from
    the promoted copy."""
    project = machinedb.project_of(major)
    back = _back(project, "copying")
    if not (project / "data" / "pgdata" / "PG_VERSION").is_file():
        raise ClusterError("the cluster holds no copy yet")
    started = time.monotonic()
    machinedb._run(project, "up", "-d", "--remove-orphans", timeout=600)
    machinedb.ready(project)
    end = time.monotonic() + STREAM_WAIT
    while not _following(project):
        if time.monotonic() > end:
            raise ClusterError("the cluster started on the copy but does not receive from the promoted copy: "
                               + (_log_tail(project) or "no reason in its log"))
        time.sleep(1)
    _save_back(project, {**back, "state": "ready", "ready_at": _now(), "synced": True})
    return {"state": "ready", "seconds": round(time.monotonic() - started, 1)}


def back_state(major: str) -> dict:
    """What this machine sees of its return: its record, what is copied so
    far, whether its cluster runs on the copy and receives, and why not."""
    project = machinedb.project_of(major)
    back = machinedb.returning(project)
    if not back:
        return {"exists": False, "major": major}
    answer = {"exists": True, "major": major, **{k: back.get(k) for k in (
        "link", "by", "host", "port", "state", "at", "ready_at", "returned_at", "expected", "size", "aside", "synced",
        "machine", "copy_seconds", "told", "instances")},
        "running": False, "receiving": False, "reason_code": "", "copied": None, "free": shutil.disk_usage(project).free}
    if back["state"] == "copying":
        answer["copied"] = tree_size(*_data_folders(project))
        return answer
    if back["state"] != "ready":
        return answer
    done = machinedb._run(project, "ps", "--format", "{{.State}}", SERVICE, check=False, timeout=60)
    answer["running"] = (done.stdout or "").strip() == "running"
    if not answer["running"]:
        answer["reason_code"] = "stopped"
        return answer
    answer["receiving"] = _following(project)
    try:
        # False: it took the writes already (a completion that did not end): the promoted copy is never started again
        answer["in_recovery"] = machinedb.sql(project, "postgres", "SELECT pg_is_in_recovery();\n").strip() == "t"
    except ClusterError:
        answer["in_recovery"] = None
    if not answer["receiving"]:
        answer["reason_code"] = reason(project)
    return answer


def back_wait(major: str, checkpoint: str, timeout: float = BACK_WAIT) -> dict:
    """Whether the cluster applied the last record the promoted copy wrote
    before it stopped (checkpoint: where that record starts), waited for up
    to timeout seconds: only then nothing is lost when it takes the writes."""
    project = machinedb.project_of(major)
    _back(project, "ready")
    if not LSN.fullmatch(str(checkpoint or "")):
        raise ClusterError("where the promoted copy stopped is not a position of its record of the changes")
    started = time.monotonic()
    while True:
        try:
            arrived = machinedb.sql(project, "postgres", "SELECT pg_wal_lsn_diff(pg_last_wal_replay_lsn(), "
                                                         f"'{checkpoint}') > 0;\n").strip() == "t"
        except ClusterError:
            arrived = False
        if arrived or time.monotonic() - started > timeout:
            return {"arrived": arrived, "seconds": round(time.monotonic() - started, 1)}
        time.sleep(0.5)


def back_promote(major: str) -> dict:
    """The cluster takes the writes again: it is this machine's cluster as
    before the promotion, with no replica. What the return needed goes (where
    it followed from, the role of the replication, the password, the CA),
    and the record of the replica that had replaced it."""
    project = machinedb.project_of(major)
    back = _back(project, "ready")
    started = time.monotonic()
    # asked again after it took them (the job did not end): nothing to tell PostgreSQL twice
    if (machinedb.sql(project, "postgres", "SELECT pg_is_in_recovery();\n").strip() == "t"
            and machinedb.sql(project, "postgres", f"SELECT pg_promote(true, {PROMOTE_WAIT});\n").strip() != "t"):
        raise ClusterError("PostgreSQL did not let the cluster take the writes")
    if machinedb.sql(project, "postgres", "SELECT pg_is_in_recovery();\n").strip() != "f":
        raise ClusterError("the cluster still follows the promoted copy after it was told to take the writes")
    at = _now()
    _save_back(project, {**back, "state": "done", "returned_at": at})
    _save_record(project, None)
    for statement in ("ALTER SYSTEM RESET primary_conninfo;\n", "ALTER SYSTEM RESET primary_slot_name;\n",
                      "ALTER SYSTEM RESET max_slot_wal_keep_size;\n", "SELECT pg_reload_conf();\n",
                      f"DROP ROLE IF EXISTS {machinedb.ident(ROLE)};\n"):
        try:
            machinedb.sql(project, "postgres", statement)
        except ClusterError:
            pass
    machinedb.write_hba(project, machinedb.environments(project))
    (project / "secret" / PGPASS).unlink(missing_ok=True)
    (project / "conf" / PRIMARY_CA).unlink(missing_ok=True)
    return {"returned_at": at, "seconds": round(time.monotonic() - started, 1)}


def back_note(major: str, values: dict) -> dict:
    """What the application keeps in the return's record once it is done:
    how many instances came back, whether the machine that hosts the copy
    heard of it."""
    project = machinedb.project_of(major)
    back = _back(project, "done")
    back = {**back, **{k: values[k] for k in ("told", "instances") if k in values}}
    _save_back(project, back)
    return {"told": back.get("told"), "instances": back.get("instances")}


def back_purge(major: str) -> dict:
    """The data set aside deleted, now that the return is complete."""
    project = machinedb.project_of(major)
    _back(project, "done")
    freed = 0
    for folder in _data_folders(project):
        aside = _aside(folder)
        if aside.is_dir() and not aside.is_symlink():
            freed += tree_size(aside)
            shutil.rmtree(aside, ignore_errors=True)
    return {"freed": freed}


def back_cancel(major: str) -> dict:
    """The return cancelled on this machine: the cluster stopped, the copy
    deleted, the data set aside back where it was. The cluster is again the
    one its replica replaced, stopped."""
    project, _rec = _replaced(major)
    back = machinedb.returning(project) or {}
    folders = _data_folders(project)
    # a preparation that was killed before its record was written left data set aside and no record: put back too
    if back.get("state") not in ("copying", "ready") and (back or not any(_aside(f).exists() for f in folders)):
        raise ClusterError("no return is under way")
    machinedb._run(project, "stop", "-t", "120", SERVICE, check=False, timeout=300)
    done = machinedb._run(project, "ps", "--format", "{{.State}}", SERVICE, check=False)
    if (done.stdout or "").strip() == "running":
        raise ClusterError("the cluster did not stop: the copy is not deleted under a PostgreSQL that runs")
    freed = _put_aside_back(project, folders)
    _save_back(project, None)
    return {"cancelled": True, "freed": freed, "link": back.get("link"), "machine": back.get("machine")}


def back_forget(major: str) -> dict:
    """The note of a return that is complete taken away: the copy it came from is gone."""
    project = machinedb.project_of(major)
    _back(project, "done")
    _save_back(project, None)
    return {"forgotten": True}


def free_here(storage: str) -> int:
    """The bytes free on the disk of a storage of this machine (0: it cannot be read)."""
    try:
        return shutil.disk_usage(Path(machinedb.STORAGE_BASE) / storage).free
    except OSError:
        return 0


def private(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return False
