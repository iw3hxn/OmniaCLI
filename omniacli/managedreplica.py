# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A read replica of the machine's PostgreSQL cluster, made and run by
OmniaCLI (docs/adr/postgresql-della-macchina.md, AD-004 option A;
docs/ux/UX-010-replica-di-lettura.md), root's side: what workspace-helper
machine-db-replica-* runs. Two machines: P has the machine's cluster (the
primary), R is another OmniaCLI with a link to P and hosts the replica.
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
STATES = ("copying", "ready", "promoted")


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


def problem(state: dict | None, who: str, now: datetime.datetime | None = None) -> str:
    """What the monitor says out of the panel (UX-010 D4): the replica not
    connected for WARN_AFTER seconds, or the cluster holding more than
    WARN_HELD for it; "" when there is nothing to say."""
    found = verdict(state)
    now = now or datetime.datetime.now()
    held = f"{found.get('held', 0) / GB:.1f} GB"
    if found["code"] == "lost":
        return (f"the read replica on {who} lost its place: the cluster kept more changes for it than its cap. "
                "Drop it and make it again (Database tab)")
    if found["code"] == "off":
        try:
            since = datetime.datetime.fromisoformat(str(found.get("since") or ""))
        except ValueError:
            return ""
        if (now - since).total_seconds() < WARN_AFTER:
            return ""
        return (f"the read replica on {who} is not connected since {since:%Y-%m-%d %H:%M}: the machine's PostgreSQL "
                f"keeps {held} of changes for it, which grow until it comes back or it is dropped (Database tab)")
    if found["code"] in ("sync", "behind") and found.get("held", 0) > WARN_HELD:
        return (f"the machine's PostgreSQL keeps {held} of changes for the read replica on {who}: the replica is "
                "connected but does not take them fast enough")
    return ""


# --- on the primary (P): the cluster's project --------------------------------------

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


def unfit(project: Path) -> str:
    """Why the cluster cannot feed a replica as it is, "" when it can."""
    line = machinedb.sql(project, "postgres", "SELECT current_setting('wal_level'), current_setting('max_wal_senders'), "
                                              "current_setting('max_replication_slots');\n").strip().split("|")
    if len(line) != 3:
        return "the machine's PostgreSQL did not say how it records its changes"
    # said without the names of PostgreSQL's parameters (UX-010 §8): the dialog of the other machine shows it
    if line[0] not in ("replica", "logical"):
        return ("the machine's PostgreSQL does not record its changes in the way a replica follows: it was set "
                "below its base values by hand, and wants them back and a restart")
    if int(line[1] or 0) < 3 or int(line[2] or 0) < 1:
        return ("the machine's PostgreSQL has no connection left for a replica: it was set below its base values "
                "by hand, and wants them back and a restart")
    return ""


def facts(major: str) -> dict:
    """What the machine that will host the replica says before it is made:
    the size of the whole cluster, whose data it holds, whether the cluster
    restarts to open its port, the storages its tablespaces are under."""
    project = machinedb.project_of(major)
    envs, lent = machinedb.environments(project), machinedb.grants(project)
    rec = record(project)
    answer = {"major": major, "running": False, "size": 0, "version": "", "port": (machinedb.listening(project) or {}).get("port"),
              "instances": sorted(f"{e.get('workspace')}-{e.get('instance')}" for e in envs.values()),
              "grants": sorted(lent), "why": "",
              "roots": machinedb._roots_of((project / "compose.yaml").read_text(encoding="utf-8")),
              "replica": {"link": rec.get("link"), "state": rec.get("state")} if rec else None}
    answer["restarts"] = answer["port"] is None
    try:
        size, version = machinedb.sql(project, "postgres", "SELECT (SELECT sum(pg_database_size(oid)) FROM pg_database), "
                                                           "current_setting('server_version');\n").strip().split("|")
        answer.update(size=int(size or 0), version=version, running=True, why=unfit(project))
    except (ClusterError, ValueError):
        answer["why"] = "the machine's PostgreSQL does not answer"
    return answer


def _drop_what_is_there(project: Path) -> None:
    """The slot and the role of a replica, when they are there: its walsender ended first."""
    for _ in range(10):
        active = machinedb.sql(project, "postgres", "SELECT coalesce(pg_terminate_backend(active_pid), false) FROM "
                                                    f"pg_replication_slots WHERE slot_name = '{SLOT}' AND active;\n").strip()
        machinedb.sql(project, "postgres", "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                                           f"WHERE usename = '{ROLE}';\n")
        if not active:
            break
        time.sleep(0.5)
    try:
        machinedb.sql(project, "postgres", f"SELECT pg_drop_replication_slot(slot_name) FROM pg_replication_slots "
                                           f"WHERE slot_name = '{SLOT}';\n")
    except ClusterError:
        # taken again meanwhile by a replica that reconnects: its role goes first, then it cannot
        machinedb.sql(project, "postgres", f"ALTER ROLE {machinedb.ident(ROLE)} NOLOGIN;\n"
                                           f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = '{ROLE}';\n")
        time.sleep(1)
        machinedb.sql(project, "postgres", f"SELECT pg_drop_replication_slot(slot_name) FROM pg_replication_slots "
                                           f"WHERE slot_name = '{SLOT}';\n")
    machinedb.sql(project, "postgres", f"DROP ROLE IF EXISTS {machinedb.ident(ROLE)};\n")


def allow(major: str, spec: dict) -> dict:
    """The machine of spec ("address": the one it comes from, "verifier":
    its role's SCRAM verifier, "reached_as": how it reaches this machine,
    "link", "by", "cap_gb", "restart": the cluster may be made again to open
    its port) let in to copy the cluster and follow it: the role, the slot,
    the cap, the line of pg_hba.conf. One replica per cluster. What that
    machine needs to connect, without any secret."""
    project = machinedb.project_of(major)
    source = machinedb.address(spec.get("address"))
    verifier = machinedb.literal(str(spec.get("verifier") or ""), machinedb.VERIFIER)
    link, by = str(spec.get("link") or ""), str(spec.get("by") or "")
    if not machinedb.LINK_NAME.fullmatch(link):
        raise ClusterError("a replica is asked by the machine that hosts it, through its link")
    cap = cap_gb(spec.get("cap_gb", CAP_GB))
    found = record(project)
    if found:
        raise ClusterError(f"the machine's PostgreSQL {major} has a read replica already, on {found.get('link')}: "
                           "drop it first, from there or from here")
    why = unfit(project)
    if why:
        raise ClusterError(why)
    if machinedb.listening(project) is None and not spec.get("restart"):
        raise ClusterError("the machine's PostgreSQL is made again once to open its port over TLS: every instance on "
                           "it loses its connections. Confirm the restart to make the replica")
    listen = machinedb.listen_on(major, [str(spec.get("reached_as") or "")])
    envs = machinedb.environments(project)
    recorded = False
    try:
        # what an attempt that did not end left: it is ours (the names are internal), it goes
        _drop_what_is_there(project)
        machinedb.sql(project, "postgres", f"CREATE ROLE {machinedb.ident(ROLE)} LOGIN REPLICATION NOSUPERUSER "
                                           f"NOCREATEDB NOCREATEROLE CONNECTION LIMIT 4 PASSWORD {verifier};\n")
        # reserved at once: the changes from now are kept, the copy and what follows it miss none
        machinedb.sql(project, "postgres", f"SELECT pg_create_physical_replication_slot('{SLOT}', true);\n")
        machinedb.sql(project, "postgres", f"ALTER SYSTEM SET max_slot_wal_keep_size = '{int(cap * 1024)}MB';\n"
                                           "SELECT pg_reload_conf();\n")
        _save_record(project, {"link": link, "by": by, "address": source, "at": _now(), "cap_gb": cap,
                               "state": "copying"})
        recorded = True
        machinedb.write_hba(project, envs)
    except ClusterError:
        if recorded:
            _save_record(project, None)
        try:
            _drop_what_is_there(project)
            machinedb.sql(project, "postgres", "ALTER SYSTEM RESET max_slot_wal_keep_size;\nSELECT pg_reload_conf();\n")
            machinedb.write_hba(project, envs)
        except ClusterError:
            pass
        raise
    size, version = machinedb.sql(project, "postgres", "SELECT (SELECT sum(pg_database_size(oid)) FROM pg_database), "
                                                       "current_setting('server_version');\n").strip().split("|")
    return {"port": listen["port"], "restarted_s": listen["restarted_s"], "address": source, "cap_gb": cap,
            "ca": machinedb.ca_certificate(project), "version": version, "size": int(size or 0),
            "names": machinedb.certificate_names((project / "conf" / "server.crt").read_text(encoding="utf-8")),
            "roots": machinedb._roots_of((project / "compose.yaml").read_text(encoding="utf-8")),
            "environments": sorted(envs), "tablespaces": tablespaces_of(project),
            "serves": len(envs), "grants": sorted(machinedb.grants(project))}


def tablespaces_of(project: Path) -> list[str]:
    """Where the tablespaces of the cluster are: each folder the replica needs at that same path."""
    out = machinedb.sql(project, "postgres", "SELECT pg_tablespace_location(oid) FROM pg_tablespace "
                                             "WHERE spcname NOT IN ('pg_default', 'pg_global');\n")
    return sorted(line.strip() for line in out.splitlines() if machinedb.SAFE_PATH.fullmatch(line.strip()))


def ready(major: str, spec: dict) -> dict:
    """The replica answers on the machine that hosts it: where an instance of
    this machine reads from it (host, port) and the CA its certificate is
    verified against, kept in the cluster's record."""
    project = machinedb.project_of(major)
    rec = record(project)
    if not rec:
        raise ClusterError(f"the machine's PostgreSQL {major} has no read replica")
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


STATE_SQL = f"""SELECT json_build_object(
 'current', pg_wal_lsn_diff(pg_current_wal_lsn(), '0/0'),
 'slot', (SELECT json_build_object('active', s.active, 'held', pg_wal_lsn_diff(pg_current_wal_lsn(), s.restart_lsn),
          'wal_status', s.wal_status, 'inactive_since', s.inactive_since, 'pid', s.active_pid)
          FROM pg_replication_slots s WHERE s.slot_name = '{SLOT}'),
 'sender', (SELECT json_build_object('state', r.state, 'applied', pg_wal_lsn_diff(r.replay_lsn, '0/0'),
            'own', extract(epoch FROM r.replay_lag), 'application', r.application_name)
            FROM pg_stat_replication r JOIN pg_replication_slots s ON s.active_pid = r.pid
            WHERE s.slot_name = '{SLOT}'),
 'copy', (SELECT json_build_object('total', b.backup_total, 'streamed', b.backup_streamed)
          FROM pg_stat_progress_basebackup b JOIN pg_stat_activity a ON a.pid = b.pid
          WHERE a.usename = '{ROLE}' LIMIT 1),
 'size', (SELECT sum(pg_database_size(oid)) FROM pg_database));
"""


def state(major: str, now: float | None = None) -> dict:
    """What this machine sees of the replica of its cluster ({"exists":
    False} with none): its record, connected or since when not, the seconds
    behind, what the slot holds against the cap, the copy's progress, the
    disk free here and what the cluster writes in an hour."""
    project = machinedb.project_of(major)
    rec = record(project)
    if not rec:
        return {"exists": False, "major": major}
    envs = machinedb.environments(project)
    answer = {"exists": True, "major": major, **{k: rec.get(k) for k in (
        "link", "by", "address", "at", "cap_gb", "state", "host", "port", "ready_at", "replaced_at", "seconds", "size")},
        "connected": False, "lag": None, "since": None, "held": 0, "wal_status": "", "progress": None, "rate": None,
        "cap": int(float(rec.get("cap_gb") or CAP_GB) * GB), "free": shutil.disk_usage(project).free,
        # what the machine that hosts it follows: the instances the cluster has now, and where their tablespaces are
        "environments": sorted(envs), "instances": sorted(f"{e.get('workspace')}-{e.get('instance')}" for e in envs.values()),
        "tablespaces": sorted({str(Path(str(e.get("mount") or "")) / machinedb.WS_FOLDER / "ts") for e in envs.values()
                               if e.get("mount")}),
        "grants": sorted(machinedb.grants(project))}
    if rec.get("state") == "replaced":
        return answer
    try:
        found = json.loads(machinedb.sql(project, "postgres", STATE_SQL).strip() or "{}")
    except (ClusterError, ValueError):
        return {**answer, "unread": True}
    now = time.time() if now is None else now
    current = float(found.get("current") or 0)
    slot, sender = found.get("slot") or {}, found.get("sender") or {}
    samples = [s for s in _samples(project) if now - s[0] <= LAG_SPAN][-LAG_SAMPLES:]
    answer.update(held=max(0, int(float(slot.get("held") or 0))), wal_status=str(slot.get("wal_status") or ""),
                  cluster_size=int(found.get("size") or 0), rate=rate(samples, now, current))
    if isinstance(found.get("copy"), dict):
        answer["progress"] = {"total": int(found["copy"].get("total") or 0), "streamed": int(found["copy"].get("streamed") or 0)}
    streaming = sender.get("state") == "streaming" and sender.get("application") != "pg_basebackup"
    if rec.get("state") == "ready" and streaming:
        answer.update(connected=True, lag=behind(samples, now, current, float(sender.get("applied") or 0), sender.get("own")))
    elif rec.get("state") == "ready":
        since = str(slot.get("inactive_since") or "")
        try:
            # as this machine's clock says it, without the zone: what the pages write
            answer["since"] = datetime.datetime.fromisoformat(since).astimezone().replace(tzinfo=None).isoformat(timespec="seconds")
        except ValueError:
            answer["since"] = rec.get("ready_at") or rec.get("at")
    if not samples or current != samples[-1][1] or now - samples[-1][0] >= 60:
        samples.append([round(now, 3), current])
    try:
        machinedb.write(project / LAG_FILE, json.dumps(samples[-LAG_SAMPLES:]) + "\n", mode=0o600)
    except OSError:
        pass
    return answer


def set_cap(major: str, value) -> dict:
    project = machinedb.project_of(major)
    rec = record(project)
    if not rec or rec.get("state") == "replaced":
        raise ClusterError(f"the machine's PostgreSQL {major} has no read replica")
    cap = cap_gb(value)
    machinedb.sql(project, "postgres", f"ALTER SYSTEM SET max_slot_wal_keep_size = '{int(cap * 1024)}MB';\n"
                                       "SELECT pg_reload_conf();\n")
    _save_record(project, {**rec, "cap_gb": cap})
    return {"cap_gb": cap}


def drop(major: str) -> dict:
    """What the cluster keeps for its replica taken away: the slot (the
    changes it held are freed at the next checkpoint, asked here), the role,
    the line of pg_hba.conf, the cap. The copy on the other machine is that
    machine's to delete."""
    project = machinedb.project_of(major)
    rec = record(project)
    if not rec:
        raise ClusterError(f"the machine's PostgreSQL {major} has no read replica")
    if rec.get("state") == "replaced":
        raise ClusterError("the replica took this cluster's place: it is not dropped from here")
    held = 0
    try:
        held = int(float(machinedb.sql(project, "postgres", "SELECT coalesce((SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), "
                                       f"restart_lsn) FROM pg_replication_slots WHERE slot_name = '{SLOT}'), 0);\n").strip() or 0))
    except (ClusterError, ValueError):
        pass
    _save_record(project, None)
    machinedb.write_hba(project, machinedb.environments(project))
    _drop_what_is_there(project)
    machinedb.sql(project, "postgres", "ALTER SYSTEM RESET max_slot_wal_keep_size;\nSELECT pg_reload_conf();\n")
    try:
        machinedb.sql(project, "postgres", "CHECKPOINT;\n")
    except ClusterError:
        pass
    return {"dropped": True, "link": rec.get("link"), "address": rec.get("address"), "freed": max(0, held),
            "host": rec.get("host"), "port": rec.get("port")}


def replaced(major: str) -> dict:
    """The replica was promoted, or is about to be: this cluster is stopped
    (a checkpoint, PostgreSQL's fast stop: a replica that is connected gets
    everything first) and stays stopped; its record says so."""
    project = machinedb.project_of(major)
    rec = record(project)
    if not rec:
        raise ClusterError(f"the machine's PostgreSQL {major} has no read replica")
    if rec.get("state") == "copying":
        raise ClusterError("the replica is not a whole copy yet: it takes this cluster's place only after it was in sync")
    if rec.get("state") == "replaced":
        return {"stopped": True, "already": True, "replaced_at": rec.get("replaced_at"),
                "environments": machinedb.environments(project), "grants": sorted(machinedb.grants(project)),
                "host": rec.get("host"), "port": rec.get("port")}
    try:
        machinedb.sql(project, "postgres", "CHECKPOINT;\n")
    except ClusterError:
        pass
    started = time.monotonic()
    machinedb._run(project, "stop", "-t", "120", SERVICE, timeout=300)
    done = machinedb._run(project, "ps", "--format", "{{.State}}", SERVICE, check=False)
    if (done.stdout or "").strip() == "running":
        raise ClusterError("the machine's PostgreSQL did not stop: nothing is promoted over a cluster that runs")
    at = _now()
    _save_record(project, {**rec, "state": "replaced", "replaced_at": at})
    return {"stopped": True, "already": False, "replaced_at": at, "seconds": round(time.monotonic() - started, 1),
            "environments": machinedb.environments(project), "grants": sorted(machinedb.grants(project)),
            "host": rec.get("host"), "port": rec.get("port")}


def target(major: str) -> dict:
    """Where an instance of this machine reaches the replica of its cluster:
    {"host", "port", "ca", "state", "link"}; refused when there is none that
    answers yet."""
    rec = record(machinedb.project_of(major))
    if not rec or rec.get("state") not in ("ready", "replaced") or not rec.get("host"):
        raise ClusterError(f"the machine's PostgreSQL {major} has no read replica that answers")
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
    return Path(storage_root) / machinedb.FOLDER / f"{FOLDER}{machinedb._major(major)}"


def hosted_project(major: str) -> Path:
    found = hosted().get(machinedb._major(major))
    if not found:
        raise ClusterError(f"this machine hosts no read replica of a PostgreSQL {major}")
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


def conninfo(host: str, port: int) -> str:
    """How the replica reaches its primary: over TLS, the certificate
    verified against the primary's CA and its name; the password in a file."""
    host = machinedb.reached_as([host])[0]
    return (f"host={host} port={int(port)} user={ROLE} passfile={machinedb.SECRETS_IN_DB}/{PGPASS} "
            f"sslmode=verify-full sslrootcert={machinedb.CONF_IN_DB}/{PRIMARY_CA} application_name={APPLICATION}")


def compose_text(major: str, uid: int, gid: int, port: int, roots: list[str], host: str, primary_port: int) -> str:
    """The replica's project: PostgreSQL of the primary's major on a copy of
    its data, a hot standby that follows it through its slot; read only, no
    capability, the slice of the machine's PostgreSQL, its socket in a tmpfs
    of its own; port of this machine reaches it, over TLS only, on a network
    of its own; a folder of its own mounted at each storage root of the
    primary, where the tablespaces are. It never archives, promoted or not."""
    for root in roots:
        if not machinedb.SAFE_PATH.fullmatch(root) or ".." in Path(root).parts:
            raise ClusterError(f"{root} is not the path of a storage of the primary")
    mounts = "".join(f"      - ./roots/{index}:{root}\n" for index, root in enumerate(roots))
    return f"""# written by omniacli workspace-helper machine-db-replica-make: the read replica of the PostgreSQL {major}
# of another machine (docs/adr/postgresql-della-macchina.md, AD-004). A copy of that whole cluster,
# kept up to date; it runs as {machinedb.USER}, in the slice {machinedb.SLICE}, and is reached on the
# port {int(port)} of this machine, over TLS, from the address pg_hba.conf and the firewall name.
name: omniacli-machine-db-replica-{major}
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
      com.docker.network.bridge.name: {bridge_name(major)}
"""


def hba_text(environments, address: str, link: str = "") -> str:
    """pg_hba.conf of the replica: the superuser by the socket (a tmpfs of
    its container: root's helper only), then the one address of the primary's
    machine as each of its instances' roles, to its database and postgres
    (where Odoo looks whether its database is there), over TLS; then reject."""
    source = machinedb.address(address)
    lines = [f"# written by omniacli workspace-helper: who reaches the read replica of the PostgreSQL of {link or 'another machine'}",
             "local all postgres trust"]
    for role in sorted(environments or []):
        name = machinedb._env(role)
        lines.append(f'hostssl "{name}",postgres "{name}" {source}/32 scram-sha-256')
    lines += ["local all all reject", "host all all all reject"]
    return "\n".join(lines) + "\n"


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
    "reserve_gb"}. Refused when the copy does not fit."""
    major = machinedb._major(str(spec.get("major") or ""))
    storage, link = str(spec.get("storage") or ""), str(spec.get("link") or "")
    if not machinedb.STORAGE_NAME.fullmatch(storage) or not machinedb.LINK_NAME.fullmatch(link):
        raise ClusterError("a replica is made on a storage of this machine, of the cluster of one of its links")
    found = hosted()
    if major in found:
        raise ClusterError(f"this machine hosts a read replica of a PostgreSQL {major} already, of "
                           f"{found[major].get('link')}: drop it first")
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
    project = project_folder(storage_root, major)
    if project.exists():
        raise ClusterError(f"{project} exists already: remove it by hand, or drop the replica first")
    fits = room(size, shutil.disk_usage(storage_root).free, reserve)
    if not fits["fits"]:
        raise ClusterError(f"no room: the copy of the cluster of {link} takes about {fits['need'] // MB} MB, "
                           f"{fits['free'] / GB:.1f} GB are free here and {reserve:g} GB must stay free")
    uid, gid = machinedb.pg_user(create=True)
    if not (machinedb.SYSTEMD / machinedb.SLICE).is_file():
        machinedb.write(machinedb.SYSTEMD / machinedb.SLICE, machinedb.slice_text())
        subprocess.run(["systemctl", "daemon-reload"], env=ENV, check=True, capture_output=True)
    taken = {int((machinedb.listening(Path(str(v.get("project")))) or {}).get("port") or 0)
             for v in machinedb.registry().values()}
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
        machinedb.write(project / "conf" / "pg_hba.conf", hba_text(environments, primary, link), mode=0o640, uid=0, gid=gid)
        # a CA of the replica's own and its server certificate, for the name the primary's machine reaches it by
        machinedb.ensure_tls(project, major, [own])
        for location in spec.get("tablespaces") or []:
            _owned_folder(under_roots(str(location), roots, project), uid, gid, project / "roots")
        machinedb.write(project / "compose.yaml", compose_text(major, uid, gid, own_port, roots, host, port))
        _save_hosted_record(project, {
            "major": major, "link": link, "by": str(spec.get("by") or ""), "host": host, "port": port,
            "primary_address": primary, "address": own, "own_port": own_port, "roots": roots,
            "environments": environments, "state": "copying", "at": _now(), "expected": size, "reserve_gb": reserve})
        _save_hosted({**found, major: {"storage": storage, "project": str(project), "link": link}})
    except BaseException:
        shutil.rmtree(project, ignore_errors=True)
        _save_hosted({k: v for k, v in hosted().items() if k != major})
        raise
    return {"major": major, "project": str(project), "port": own_port, "address": own, "primary_address": primary,
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


def machinedb_sizes(project: Path) -> int:
    total = 0
    for folder in (project / "data", project / "roots"):
        for root, _dirs, files in os.walk(folder):
            for name in files:
                try:
                    total += os.lstat(os.path.join(root, name)).st_size
                except OSError:
                    pass
    return total


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
    text = hba_text(environments, str(rec.get("primary_address") or ""), str(rec.get("link") or ""))
    path = project / "conf" / "pg_hba.conf"
    if environments != list(rec.get("environments") or []) or path.read_text(encoding="utf-8") != text:
        machinedb.write(path, text, mode=0o640, uid=0, gid=gid)
        replica_sql(project, "SELECT pg_reload_conf();\n", check=False)
        _save_hosted_record(project, {**rec, "environments": environments})
        changed.append("pg_hba.conf")
    return {"changed": changed}


def hosted_state(major: str) -> dict:
    """What this machine sees of the replica it hosts: its record, whether
    its container runs, whether it receives and why not, its size, the
    connections that read from it, the disk free here."""
    project = hosted_project(major)
    rec = hosted_record(project)
    answer = {"exists": True, "major": major, **{k: rec.get(k) for k in (
        "link", "by", "host", "port", "address", "own_port", "primary_address", "state", "at", "ready_at",
        "promoted_at", "copy_seconds", "size", "expected", "version", "synced", "environments")},
        "storage": (hosted().get(major) or {}).get("storage"), "running": False, "receiving": False, "reason_code": "",
        "connections": None, "max_connections": None, "free": shutil.disk_usage(project).free}
    # what it takes on this machine's disk (its data, its tablespaces, the changes it keeps); while it is
    # copied, what is copied so far; then what the copy was when it ended
    answer["disk"] = machinedb_sizes(project)
    if rec.get("state") == "copying":
        answer["copied"] = answer["disk"]
        return answer
    answer["copied"] = rec.get("size")
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


def promote(major: str) -> dict:
    """The copy takes the writes: all it received applied, then pg_promote.
    Only one that was in sync once: a copy not whole is never promoted."""
    project = hosted_project(major)
    rec = hosted_record(project)
    if rec.get("state") == "promoted":
        return {"promoted_at": rec.get("promoted_at"), "already": True}
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
    _save_hosted_record(project, {**rec, "state": "promoted", "promoted_at": at})
    return {"promoted_at": at, "already": False, "seconds": round(time.monotonic() - started, 1)}


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
    subprocess.run([machinedb._docker(), "network", "rm", f"omniacli-machine-db-replica-{major}_db"], env=ENV,
                   capture_output=True)
    if not machinedb.registry() and not hosted():
        machinedb._remove_units()
        subprocess.run([shutil.which("userdel", path=ENV["PATH"]) or "userdel", machinedb.USER], env=ENV, capture_output=True)
    return {"removed": str(project), "freed": size, "link": rec.get("link"), "state": rec.get("state")}


def firewall_state() -> dict:
    """{bridge: {"from": [addresses], "to": [[address, port]]}} of the
    replicas this machine hosts: reached from the primary's machine only,
    and going out to the primary's port only (machinedb.firewall_rules)."""
    found = {}
    for major, entry in sorted(hosted().items()):
        rec = hosted_record(Path(str(entry.get("project") or "")))
        try:
            primary = machinedb.address(rec.get("primary_address"))
            port = int(rec.get("port") or 0)
        except (ClusterError, ValueError):
            continue
        if 1 <= port <= 65535:
            # a promoted one no longer goes out: nothing to follow
            out = [] if rec.get("state") == "promoted" else [[primary, port]]
            found[bridge_name(major)] = {"from": [primary], "to": out}
    return found


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
