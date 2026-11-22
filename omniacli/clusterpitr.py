# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Recovery at a minute of the machine's PostgreSQL cluster
(docs/adr/postgresql-della-macchina.md, AD-006), root's side: what
workspace-helper machine-db-archive-* and machine-db-recover run.
PostgreSQL's own way, no other tool, as pitrhelper.py for a dedicated
database.

The archive is the cluster's, never an instance's: archive_mode is one for
the whole cluster. It lives in the cluster's project, wal/ (archive/, the
WAL segments; base/, the base backups), on the storage's real disk, outside
every workspace's disk, the cluster's user's and root's only: it holds every
customer's data. PostgreSQL writes there by itself, the storage being
mounted in its container where it is.

- enable: archive_mode, archive_command (a copy made whole, then renamed),
  archive_timeout; a restart only when archive_mode is not on in the running
  server, and only when the caller said so; pg_hba.conf lets the superuser
  in for a base backup; a first base backup.
- disable: nothing more is archived, at once and without a restart
  (archive_command = /bin/true; archive_mode off at the next restart); the
  bases and the archive are deleted.
- base: pg_basebackup -Ft -z (base.tar.gz and one <oid>.tar.gz per
  tablespace: AD-002 keeps each workspace's databases in a tablespace on its
  disk), its record beside it (info.json: the segment it starts from, when
  it ended, the size of the data); then the retention.
- retention (prune): the newest `keep` bases stay, the segments before the
  start of the oldest kept go. Never while a recovery runs (its lock): what
  a recovery reads is never deleted under it. The budget only warns.
- restore: ONE database as it was at a minute, into the database of a
  staging of the same workspace. A temporary cluster, root's, in a folder
  beside the cluster's project (recovery-<major>-<hex>, 0700 root): the base
  before that minute unpacked, each tablespace unpacked under a folder that
  the temporary container mounts where the storage is on the real machine
  (so tablespace_map is right as it is and the real tablespaces are never
  mounted), the archive mounted read only, replayed up to that minute and
  promoted; pg_dump of that one database from it, on the workspace's own
  disk; the temporary cluster deleted; then the dump restored into the
  staging's database on the machine's cluster, as its own role.
  The temporary cluster has no network and no port, its socket in a tmpfs
  of its container, archive_mode off. It is deleted in every case: at the
  end, at a failure, at a signal (the job stopped: SIGTERM, SIGHUP and
  SIGINT run the same cleanup), and what a kill or a power cut leaves is
  swept by the next recovery, by the hourly loop and when OmniaCLI starts
  (sweep), which a recovery still running holds off with its lock.

What a workspace's user may read of a failure is a fixed sentence
(RecoveryFailed): the reason, which may name other customers' paths, goes to
recovery.log in the cluster's project, root's only."""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

from . import machinedb
from .machinedb import ENV, SERVICE, ClusterError

WAL = "wal"
SETTINGS_FILE = "archive.json"
LOCK_FILE = "recovery.lock"
BASE_LOCK_FILE = "base.lock"
LOG_FILE = "recovery.log"
LAST_FILE = "recovery.json"
INFO = "info.json"
ARCHIVE_TIMEOUT = 300
# the bases kept, the disk the archive may take before the warning, the room
# a recovery leaves free on the machine's disk: the defaults, each changed in
# the Database tab
KEEP_BASES = 7
BUDGET_GB = 20.0
RESERVE_GB = 2.0
KEEP_RANGE = (1, 60)
BUDGET_RANGE = (0.1, 100000.0)
RESERVE_RANGE = (0.0, 100000.0)
GB = 1 << 30
MB = 1 << 20
SEGMENT = re.compile(r"[0-9A-F]{24}")
STAMP = "%Y%m%dT%H%M%S"
TEMP_PREFIX = "recovery-"
TEMP_NAME = re.compile(r"recovery-(\d{1,3})-[0-9a-f]{12}")
LABEL = "omniacli.cluster-recovery"
ARCHIVE_IN_TEMP = "/omniacli-wal"
# what the image declares as its volume: PGDATA_IN_DB is a folder of it
DATA_IN_DB = str(Path(machinedb.PGDATA_IN_DB).parent)
OFF_COMMAND = "/bin/true"
BASE_TIMEOUT = 6 * 3600
RECOVERY_TIMEOUT = 6 * 3600
ARCHIVE_WAIT = 180
READY_TIMEOUT = machinedb.READY_TIMEOUT
# a tablespace made after the base: PostgreSQL asks for its folder, once each
MISSING_FOLDER = re.compile(r'directory "(/[^"\s]+)" does not exist')
MISSING_TRIES = 50
# the time a staging at a minute is expected to take, as the dialog says it
# before: a fixed part, then bytes a second of the base unpacked, of the WAL
# replayed, of the database dumped and restored. Measured on a machine of
# the tests on 22/11/2026 (a cluster of 150 MB, a database of 20 MB: 126 s
# in all, of which 99 s the staging made, 90 of them the check of the
# workspace's disk, 10 s its Odoo stopped, 3.6 s to unpack, replay, dump and
# restore): the fixed part is that staging's, the rates a quarter of the
# measured ones or less, a slower machine's
FIXED_SECONDS = 120
UNPACK_RATE = 60 * MB
REPLAY_RATE = 30 * MB
COPY_RATE = 8 * MB
STEPS = {"check": "checking the room on the machine's disk",
         "archive": "saving the changes of now", "unpack": "unpacking the full copy",
         "replay": "replaying the changes up to that minute", "dump": "reading the database at that minute",
         "restore": "writing the database of the staging"}


class RecoveryFailed(ClusterError):
    """A recovery that failed, in words a workspace's user may read: no
    path, no name but its own."""


class Interrupted(BaseException):
    """The helper was told to stop (the job stopped): the cleanup runs."""


# --- its files ---------------------------------------------------------------------

def wal_folder(project: Path) -> Path:
    return project / WAL


def archive_folder(project: Path) -> Path:
    return project / WAL / "archive"


def base_folder(project: Path) -> Path:
    return project / WAL / "base"


def _within(value, default, low, high, kind=float):
    try:
        found = kind(value)
    except (TypeError, ValueError):
        return default
    return found if low <= found <= high else default


def settings(project: Path) -> dict:
    """{"enabled", "budget_gb", "keep", "reserve_gb", "since"} of the
    cluster's archive (archive.json in its project, root's)."""
    try:
        data = json.loads((project / SETTINGS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {"enabled": bool(data.get("enabled")),
            "budget_gb": _within(data.get("budget_gb"), BUDGET_GB, *BUDGET_RANGE),
            "keep": _within(data.get("keep"), KEEP_BASES, *KEEP_RANGE, kind=int),
            "reserve_gb": _within(data.get("reserve_gb"), RESERVE_GB, *RESERVE_RANGE),
            "since": str(data.get("since") or "")}


def _save_settings(project: Path, conf: dict) -> None:
    machinedb.write(project / SETTINGS_FILE, json.dumps(conf, indent=1, sort_keys=True) + "\n", mode=0o644)


def amounts(budget_gb, keep, reserve_gb) -> tuple[float, int, float]:
    """The budget, the bases kept and the room left free, as the Database
    tab sends them: each a number within its bounds, or refused."""
    def one(value, low, high, kind, what):
        try:
            found = kind(str(value).strip().replace(",", "."))
        except ValueError:
            raise ClusterError(f"{value!r} is not a number: {what}") from None
        if not low <= found <= high:
            raise ClusterError(f"{what}: between {low:g} and {high:g}")
        return found
    return (one(budget_gb, *BUDGET_RANGE, float, "the disk the archive may take, in GB"),
            one(keep, *KEEP_RANGE, int, "the full copies kept"),
            one(reserve_gb, *RESERVE_RANGE, float, "the room a recovery leaves free on the disk, in GB"))


def archive_command(archive: Path) -> str:
    """A segment copied whole and then renamed: one cut short by a crash is
    never taken for an archived one. Never over one that is there."""
    if not machinedb.SAFE_PATH.fullmatch(str(archive)):
        raise ClusterError(f"{archive} is not a path the cluster archives into")
    return f"test ! -f {archive}/%f && cp %p {archive}/%f.part && mv {archive}/%f.part {archive}/%f"


def _log(project: Path, text: str) -> None:
    """A line of recovery.log, root's only: what a job's text must not say."""
    path = project / LOG_FILE
    with contextlib.suppress(OSError):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.datetime.now().isoformat(timespec='seconds')} {text.rstrip()}\n")


def _last(project: Path) -> dict:
    try:
        data = json.loads((project / LAST_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_last(project: Path, **values) -> None:
    with contextlib.suppress(OSError):
        machinedb.write(project / LAST_FILE, json.dumps({**_last(project), **values}, indent=1, sort_keys=True) + "\n",
                        mode=0o600)


@contextlib.contextmanager
def _flock(path: Path):
    """The lock of path held (True) or not got (False: another process has
    it). The kernel lets it go when its process ends, however it ends."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def recovering(project: Path) -> bool:
    try:
        with _flock(project / LOCK_FILE) as got:
            return not got
    except OSError:
        return False


def _sizes(folder: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(folder):
        for name in files:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(root, name)).st_size
    return total


def bases(project: Path) -> list[dict]:
    """The base backups that ended (each has its record), the oldest first."""
    found = []
    folder = base_folder(project)
    for one in sorted(folder.iterdir()) if folder.is_dir() else []:
        try:
            info = json.loads((one / INFO).read_text(encoding="utf-8"))
            end = datetime.datetime.fromisoformat(str(info["end"]))
            start = str(info["start"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if not SEGMENT.fullmatch(start):
            continue
        found.append({"name": one.name, "time": end.isoformat(timespec="seconds"), "start": start,
                      "size": sum(f.stat().st_size for f in one.iterdir() if f.is_file()),
                      "data_size": int(info.get("data_size") or 0), "seconds": info.get("seconds")})
    return found


def segments(project: Path) -> list[Path]:
    folder = archive_folder(project)
    return [p for p in folder.iterdir() if SEGMENT.fullmatch(p.name)] if folder.is_dir() else []


def _minute_after(when: datetime.datetime) -> datetime.datetime:
    floor = when.replace(second=0, microsecond=0)
    return floor if floor == when else floor + datetime.timedelta(minutes=1)


def room(project: Path, conf: dict, data_size: int) -> dict:
    """What the temporary cluster takes on the machine's disk (the whole
    base unpacked, not one database: a fifth more and 256 MB for the WAL it
    replays), what is free there, what must stay free after it."""
    need = data_size + data_size // 5 + 256 * MB
    free = shutil.disk_usage(project.parent).free
    reserve = int(conf["reserve_gb"] * GB)
    return {"need": need, "free": free, "reserve": reserve, "fits": free - need >= reserve}


def seconds_expected(data_size: int, wal_size: int, database_size: int) -> int:
    return int(FIXED_SECONDS + data_size / UNPACK_RATE + wal_size / REPLAY_RATE + 2 * database_size / COPY_RATE)


def state(major: str, database: str = "") -> dict:
    """The archive of the cluster of major: on or off, its bases and
    segments, the disk they take against the budget, the first and the last
    minute a recovery reaches, the room and the time a recovery would take
    (of database, when given), the last recovery and the last failure."""
    project = machinedb.project_of(major)
    conf = settings(project)
    found = bases(project)
    kept = segments(project)
    archive_size = sum(p.stat().st_size for p in kept)
    base_size = sum(b["size"] for b in found)
    used = archive_size + base_size
    budget = int(conf["budget_gb"] * GB)
    answer = {"major": major, **conf, "bases": [{k: b[k] for k in ("name", "time", "size", "data_size", "seconds")}
                                                  for b in found],
              "segments": len(kept), "archive_size": archive_size, "base_size": base_size, "used": used,
              "budget": budget, "over": used > budget, "first": None, "last": None, "failed": 0, "last_failed": "",
              "failing": False, "archive_mode": "", "recovering": recovering(project), "last_recovery": None,
              "last_failure": None}
    size = database_size = 0
    try:
        size_of = (f"coalesce((SELECT pg_database_size(oid) FROM pg_database WHERE datname = "
                   f"'{machinedb._env(database)}'), 0)") if database else "0"
        line = machinedb.sql(project, "postgres",
                             "SELECT current_setting('archive_mode'), failed_count, "
                             "coalesce(extract(epoch FROM last_failed_time)::bigint, 0), "
                             "failed_count > 0 AND last_failed_time > coalesce(last_archived_time, '-infinity'), "
                             f"(SELECT sum(pg_database_size(oid)) FROM pg_database), {size_of} "
                             "FROM pg_stat_archiver;\n").strip().split("|")
        answer["archive_mode"] = line[0]
        answer["failed"] = int(line[1] or 0)
        if int(line[2] or 0):
            answer["last_failed"] = datetime.datetime.fromtimestamp(int(line[2])).isoformat(timespec="seconds")
        answer["failing"] = line[3] == "t"
        size, database_size = int(line[4] or 0), int(line[5] or 0)
    except (ClusterError, ValueError, IndexError):
        answer["unread"] = True
    if conf["enabled"] and found:
        answer["first"] = _minute_after(datetime.datetime.fromisoformat(found[0]["time"])).isoformat(timespec="seconds")
        newest = max((p.stat().st_mtime for p in kept), default=None)
        if answer["failing"] or answer.get("unread"):
            # what is not archived cannot be reached: the last segment saved
            answer["last"] = (datetime.datetime.fromtimestamp(newest).isoformat(timespec="seconds")
                              if newest else None)
        else:
            # a recovery archives the segment being written first: up to the minute before now
            now = datetime.datetime.now().replace(second=0, microsecond=0)
            answer["last"] = now.isoformat(timespec="seconds")
    data_size = max([size, *(b["data_size"] for b in found)])
    answer["cluster_size"] = size
    answer["room"] = room(project, conf, data_size)
    # the WAL of a day, the most a recovery replays: the archive over its bases
    answer["seconds"] = seconds_expected(data_size, archive_size // max(1, len(found)), database_size)
    last = _last(project)
    if isinstance(last.get("recovery"), dict):
        answer["last_recovery"] = last["recovery"]
    if isinstance(last.get("failure"), dict):
        answer["last_failure"] = last["failure"]
    return answer


# --- the archive switched on and off ---------------------------------------------------

def _show(project: Path, name: str) -> str:
    return machinedb.sql(project, "postgres", f"SHOW {name};\n").strip()


def _answers(project: Path) -> None:
    timeout = READY_TIMEOUT
    end = time.monotonic() + timeout
    while machinedb._run(project, "exec", "-T", SERVICE, "pg_isready", "-q", "-h", machinedb.SOCKET_IN_DB,
                         check=False).returncode:
        if time.monotonic() > end:
            raise ClusterError(f"no answer within {timeout:.0f} s")
        time.sleep(0.25)


def _restart(project: Path, auto_conf: Path, before: bytes | None) -> float:
    """The cluster restarted (a checkpoint, PostgreSQL's fast stop, a
    start): the seconds it did not answer. One that does not come back gets
    the postgresql.auto.conf of before and starts again, and it is said."""
    with contextlib.suppress(ClusterError):
        machinedb.sql(project, "postgres", "CHECKPOINT;\n")
    started = time.monotonic()
    try:
        machinedb._run(project, "stop", "-t", "120", SERVICE, timeout=300)
        machinedb._run(project, "start", SERVICE, timeout=300)
        _answers(project)
    except (ClusterError, subprocess.TimeoutExpired) as exc:
        machinedb._run(project, "stop", "-t", "120", SERVICE, check=False, timeout=300)
        if before is not None:
            info = auto_conf.stat()
            tmp = auto_conf.with_name(auto_conf.name + ".omniacli")
            tmp.write_bytes(before)
            os.chown(tmp, info.st_uid, info.st_gid)
            os.chmod(tmp, info.st_mode & 0o777)
            os.replace(tmp, auto_conf)
        machinedb._run(project, "start", SERVICE, check=False, timeout=300)
        try:
            _answers(project)
        except ClusterError:
            raise ClusterError(f"the machine's PostgreSQL did not come back with the archive on ({exc}), nor with "
                               "its settings of before: it does not answer") from None
        raise ClusterError(f"the machine's PostgreSQL did not come back with the archive on ({exc}): it runs as "
                           "before, the archive is off") from None
    return round(time.monotonic() - started, 1)


def enable(major: str, budget_gb, keep, reserve_gb, restart: str) -> dict:
    """The archive of the cluster switched on, or its amounts changed when
    it is on already (nothing else touched then). restart: "restart" when
    whoever asks knows the cluster restarts; with anything else, a cluster
    that would have to restart is refused and nothing is written."""
    project = machinedb.project_of(major)
    budget, kept, reserve = amounts(budget_gb, keep, reserve_gb)
    before = settings(project)
    uid, gid = machinedb.pg_user()
    archive = archive_folder(project)
    command = archive_command(archive)
    running = _show(project, "archive_mode")
    conf = {**before, "budget_gb": budget, "keep": kept, "reserve_gb": reserve}
    if before["enabled"] and running == "on" and _show(project, "archive_command") == command:
        _save_settings(project, conf)
        return {"major": major, "changed": "amounts", "restarted_s": None, "base": None, "pruned": prune(project, kept)}
    if running != "on" and restart != "restart":
        raise ClusterError("the machine's PostgreSQL restarts once to start archiving: every instance on it loses "
                           "the database for a few seconds. Confirm the restart to switch the archive on")
    for folder in (wal_folder(project), archive, base_folder(project)):
        machinedb._folder(folder, 0o700, uid, gid)
    # a base backup comes in as a replication connection of the superuser, on the socket
    machinedb.write_hba(project, machinedb.environments(project))
    auto_conf = project / "data" / "pgdata" / "postgresql.auto.conf"
    kept_conf = auto_conf.read_bytes() if auto_conf.is_file() else None
    machinedb.sql(project, "postgres", "ALTER SYSTEM SET archive_mode = 'on';\n"
                                       f"ALTER SYSTEM SET archive_command = '{command}';\n"
                                       f"ALTER SYSTEM SET archive_timeout = '{ARCHIVE_TIMEOUT}';\n"
                                       "SELECT pg_reload_conf();\n")
    seconds = _restart(project, auto_conf, kept_conf) if running != "on" else None
    _save_settings(project, {**conf, "enabled": True,
                             "since": datetime.datetime.now().isoformat(timespec="seconds")})
    made = base(major)
    return {"major": major, "changed": "on", "restarted_s": seconds, "base": made["base"], "size": made["size"],
            "base_seconds": made["seconds"], "serves": len(machinedb.environments(project)),
            "grants": len(machinedb.grants(project))}


def disable(major: str) -> dict:
    """The archive switched off without a restart: nothing more is archived
    from now (archive_command = /bin/true), archive_mode is off at the next
    restart of the cluster, whenever it is; the bases and the archive are
    deleted. Refused while a recovery reads them."""
    project = machinedb.project_of(major)
    conf = settings(project)
    with _flock(project / LOCK_FILE) as got:
        if not got:
            raise ClusterError("a recovery at a minute is running on the machine's PostgreSQL: wait for it to end")
        machinedb.sql(project, "postgres", f"ALTER SYSTEM SET archive_command = '{OFF_COMMAND}';\n"
                                           "ALTER SYSTEM RESET archive_timeout;\n"
                                           "ALTER SYSTEM RESET archive_mode;\n"
                                           "SELECT pg_reload_conf();\n")
        _save_settings(project, {**conf, "enabled": False, "since": ""})
        freed = 0
        for folder in (archive_folder(project), base_folder(project)):
            if folder.is_dir() and not folder.is_symlink():
                freed += _sizes(folder)
                for item in folder.iterdir():
                    if item.is_dir() and not item.is_symlink():
                        shutil.rmtree(item)
                    else:
                        item.unlink()
    return {"major": major, "freed": freed, "archive_mode": _show(project, "archive_mode")}


# --- a base backup, the retention ---------------------------------------------------------

def base(major: str) -> dict:
    """A base backup of the whole cluster (its tablespaces with it), its
    record, the segment being written sent to the archive; the retention."""
    project = machinedb.project_of(major)
    conf = settings(project)
    if not conf["enabled"]:
        raise ClusterError("recovery at a minute is off for the machine's PostgreSQL")
    uid, gid = machinedb.pg_user()
    with _flock(project / BASE_LOCK_FILE) as got:
        if not got:
            raise ClusterError("a full copy of the machine's PostgreSQL is being made already")
        stamp = datetime.datetime.now().strftime(STAMP)
        while (base_folder(project) / stamp).exists():
            time.sleep(1)
            stamp = datetime.datetime.now().strftime(STAMP)
        folder = base_folder(project) / stamp
        start, size = machinedb.sql(project, "postgres", "SELECT pg_walfile_name(pg_current_wal_lsn()), "
                                    "(SELECT sum(pg_database_size(oid)) FROM pg_database);\n").strip().split("|")
        if not SEGMENT.fullmatch(start):
            raise ClusterError("the machine's PostgreSQL did not say which segment it writes")
        began = time.monotonic()
        try:
            machinedb._run(project, "exec", "-T", SERVICE, "pg_basebackup", "-U", "postgres", "-h",
                           machinedb.SOCKET_IN_DB, "-D", str(folder), "-Ft", "-z", "-X", "none", "-c", "fast",
                           timeout=BASE_TIMEOUT)
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        seconds = round(time.monotonic() - began, 1)
        machinedb.write(folder / INFO, json.dumps({
            "start": start, "data_size": int(size or 0), "seconds": seconds,
            "end": datetime.datetime.now().isoformat(timespec="seconds")}, indent=1, sort_keys=True) + "\n",
            mode=0o600, uid=uid, gid=gid)
        # the segment being written now goes to the archive: the base can be used at once
        with contextlib.suppress(ClusterError):
            machinedb.sql(project, "postgres", "SELECT pg_switch_wal();\n")
        pruned = prune(project, conf["keep"])
    made = sum(f.stat().st_size for f in folder.iterdir() if f.is_file())
    used = _sizes(wal_folder(project))
    budget = int(conf["budget_gb"] * GB)
    return {"major": major, "base": stamp, "size": made, "data_size": int(size or 0), "seconds": seconds,
            "pruned": pruned, "used": used, "budget": budget, "over": used > budget}


def prune(project: Path, keep: int) -> dict:
    """The retention: the newest keep bases stay, the others go with the
    segments before the start of the oldest kept (and what an archiving cut
    short left). Skipped, and said, while a recovery runs: the base and the
    segments it reads stay. A base without its record is one that did not
    end: gone too (the one being made is held by base's own lock)."""
    with _flock(project / LOCK_FILE) as got:
        if not got:
            return {"skipped": "a recovery at a minute is running: nothing is deleted under it", "bases": 0,
                    "segments": 0}
        ended = bases(project)
        names = [b["name"] for b in ended]
        drop = set(names[:-keep])
        gone = 0
        folder = base_folder(project)
        folders = sorted(d for d in folder.iterdir() if d.is_dir() and not d.is_symlink()) if folder.is_dir() else []
        for one in folders:
            # one without its record did not end; the newest may be the one being made now
            if one.name in drop or (one.name not in names and one is not folders[-1]):
                shutil.rmtree(one)
                gone += 1
        kept = ended[-keep:]
        removed = 0
        if kept:
            oldest = kept[0]["start"]
            archive = archive_folder(project)
            for item in archive.iterdir() if archive.is_dir() else []:
                name = item.name
                if name.endswith(".history"):
                    continue
                if name.endswith(".part"):
                    if time.time() - item.stat().st_mtime > 3600:
                        item.unlink()
                    continue
                if SEGMENT.fullmatch(name[:24]) and name[:24] < oldest:
                    item.unlink()
                    removed += 1
        return {"bases": gone, "segments": removed, "kept": [b["name"] for b in kept]}


# --- one database at a minute -----------------------------------------------------------------

def _docker(*args: str, input: str | None = None, stdout=None, timeout: float | None = None,
            check: bool = True) -> subprocess.CompletedProcess:
    done = subprocess.run([machinedb._docker(), *args], env=ENV, input=input, timeout=timeout,
                          stdout=stdout if stdout is not None else subprocess.PIPE, stderr=subprocess.PIPE,
                          text=stdout is None)
    if check and done.returncode:
        err = done.stderr if isinstance(done.stderr, str) else (done.stderr or b"").decode(errors="replace")
        raise ClusterError(f"docker {args[0]} failed: {err.strip()[-600:]}")
    return done


def container_name(major: str, folder: Path) -> str:
    return f"omniacli-machine-db-{major}-{folder.name}"


def temp_folders(project: Path, major: str) -> list[Path]:
    """The folders of temporary clusters beside the cluster's project."""
    return sorted(p for p in project.parent.iterdir()
                  if (m := TEMP_NAME.fullmatch(p.name)) and m.group(1) == major and not p.is_symlink())


def _gone(folder: Path) -> None:
    for _ in range(5):
        shutil.rmtree(folder, ignore_errors=True)
        if not folder.exists():
            return
        time.sleep(1)
    shutil.rmtree(folder)


def remove_temp(major: str, folder: Path) -> None:
    """A temporary cluster deleted: its container (killed, with what it
    wrote) and its folder. It has no network; -v: no volume of docker's
    stays, were the image to declare one more."""
    _docker("rm", "-f", "-v", container_name(major, folder), check=False, timeout=300)
    _gone(folder)


def _workspace_files(project: Path) -> list[Path]:
    """The dumps and lists of recoveries on the workspaces' disks."""
    found = []
    mounts = {str(rec.get("mount") or "") for rec in machinedb.environments(project).values()}
    for mount in sorted(mounts):
        folder = Path(mount) / machinedb.WS_FOLDER
        if folder.is_absolute() and folder.is_dir() and not folder.is_symlink():
            found += [p for p in folder.glob("recovery-*") if p.suffix in (".dump", ".list") and p.is_file()]
    return found


def _sweep(project: Path, major: str) -> dict:
    """With the lock held: every temporary cluster there is nobody's."""
    folders = temp_folders(project, major)
    for folder in folders:
        remove_temp(major, folder)
    names = _docker("ps", "-a", "--filter", f"label={LABEL}={major}", "--format", "{{.Names}}",
                    check=False, timeout=120).stdout.split()
    for name in names:
        _docker("rm", "-f", "-v", name, check=False, timeout=300)
    files = _workspace_files(project)
    for path in files:
        path.unlink(missing_ok=True)
    if folders or names or files:
        _log(project, f"swept: {len(folders)} folders, {len(names)} containers, {len(files)} files of a recovery "
                      "that did not end")
    return {"folders": len(folders), "containers": len(names), "files": len(files)}


def sweep() -> dict:
    """What a recovery that was killed left (its temporary cluster, its
    dump), deleted; a cluster where one is running is left alone."""
    out = {}
    for major, rec in sorted(machinedb.registry().items()):
        project = Path(str(rec.get("project") or ""))
        if not (project / "compose.yaml").is_file():
            continue
        with _flock(project / LOCK_FILE) as got:
            out[major] = _sweep(project, major) if got else {"running": True}
    return {"swept": out}


@contextlib.contextmanager
def _interruptible():
    """SIGTERM, SIGHUP and SIGINT raise Interrupted in the body (the job
    was stopped: what follows the body, its finally, runs); the set to call
    when the cleanup starts, which no signal cuts short."""
    if threading.current_thread() is not threading.main_thread():
        yield lambda: None
        return
    signals = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)

    def stop(_number, _frame):
        raise Interrupted()
    before = {s: signal.signal(s, stop) for s in signals}
    try:
        yield lambda: [signal.signal(s, signal.SIG_IGN) for s in signals]
    finally:
        for number, handler in before.items():
            signal.signal(number, handler)


def _archive_now(project: Path) -> str:
    """A commit of now written and its segment sent to the archive: the
    recovery finds a change after the minute asked, so it can stop there,
    and reaches up to now."""
    out = machinedb.sql(project, "postgres", "SELECT txid_current();\n"
                                             "SELECT pg_walfile_name(pg_switch_wal());\n").split()
    name = out[-1] if out else ""
    if not SEGMENT.fullmatch(name):
        raise ClusterError("the machine's PostgreSQL did not say which segment it closed")
    end = time.monotonic() + ARCHIVE_WAIT
    while not (archive_folder(project) / name).is_file():
        if time.monotonic() > end:
            raise ClusterError(f"the segment {name} is not in the archive after {ARCHIVE_WAIT} s")
        time.sleep(0.5)
    return name


def tablespaces(pgdata: Path) -> dict[str, str]:
    """{oid: location} of a base's tablespace_map (none: no tablespace)."""
    try:
        text = (pgdata / "tablespace_map").read_text(encoding="utf-8")
    except OSError:
        return {}
    found = {}
    for line in text.splitlines():
        oid, _, location = line.partition(" ")
        if oid.isdigit() and location:
            found[oid] = location
    return found


def under_roots(location: str, roots: list[str], temp: Path) -> Path:
    """Where a tablespace's location on the real machine is in the folder
    of a temporary cluster: under roots/<n>, which its container mounts on
    the storage roots[n]."""
    if not machinedb.SAFE_PATH.fullmatch(location) or ".." in Path(location).parts:
        raise ClusterError("a tablespace's location is not a path of a storage")
    for index, root in enumerate(roots):
        if location.startswith(root.rstrip("/") + "/"):
            return temp / "roots" / str(index) / location[len(root.rstrip("/")) + 1:]
    raise ClusterError("a tablespace is outside the storages of the workspaces")


def _tablespace_folder(path: Path, uid: int, gid: int) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chown(path, uid, gid)
    os.chmod(path, 0o700)


def run_command(major: str, name: str, temp: Path, project: Path, roots: list[str], uid: int, gid: int,
                target_time: str) -> list[str]:
    """docker run of the temporary cluster: no network, no port, its socket
    in a tmpfs of its own, the image and the user of the cluster, the
    archive read only, archive_mode off, little shared memory."""
    # the whole folder the image declares as a volume: docker makes no volume of its own for it
    mounts = [f"{temp / 'data'}:{DATA_IN_DB}",
              f"{temp / 'conf'}:{machinedb.CONF_IN_DB}:ro",
              f"{project / 'secret'}:{machinedb.SECRETS_IN_DB}:ro",
              f"{archive_folder(project)}:{ARCHIVE_IN_TEMP}:ro",
              *(f"{temp / 'roots' / str(index)}:{root}" for index, root in enumerate(roots))]
    return ["run", "-d", "--name", name, "--label", f"{LABEL}={major}", "--network", "none", "--read-only",
            "--user", f"{uid}:{gid}", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "--pids-limit", "1024", "--shm-size", "256m", "--cgroup-parent", machinedb.SLICE, "--restart", "no",
            "--tmpfs", "/tmp:size=64m", "--tmpfs", f"{machinedb.SOCKET_IN_DB}:size=1m",
            "-e", f"PGDATA={machinedb.PGDATA_IN_DB}", "-e", f"PGPASSFILE={machinedb.SECRETS_IN_DB}/pgpass",
            *(part for mount in mounts for part in ("-v", mount)),
            machinedb.IMAGE[major], "postgres",
            "-c", f"hba_file={machinedb.CONF_IN_DB}/pg_hba.conf", "-c", "listen_addresses=",
            "-c", f"unix_socket_directories={machinedb.SOCKET_IN_DB}", "-c", "archive_mode=off",
            "-c", "shared_buffers=128MB", "-c", "ssl=off",
            "-c", f"restore_command=cp {ARCHIVE_IN_TEMP}/%f \"%p\"",
            "-c", f"recovery_target_time={target_time}", "-c", "recovery_target_action=promote"]


TEMP_HBA = ("# the temporary cluster of a recovery at a minute: its superuser on its socket, nobody else\n"
            "local all postgres scram-sha-256\nlocal all all reject\nhost all all all reject\n")


def _temp_sql(name: str, query: str, check: bool = True) -> subprocess.CompletedProcess:
    return _docker("exec", "-i", name, "psql", "-X", "-At", "-q", "-v", "ON_ERROR_STOP=1", "-U", "postgres",
                   "-d", "postgres", input=query, check=check, timeout=300)


def _replayed(name: str, temp: Path, roots: list[str], uid: int, gid: int) -> int:
    """Waits for the temporary cluster to reach the minute and be promoted.
    A tablespace made after the base and before the minute: PostgreSQL
    stops asking for its folder, which is made (in the temporary folder,
    where the container sees it at that path) and the replay goes on. How
    many folders were made so."""
    end = time.monotonic() + RECOVERY_TIMEOUT
    made: set[str] = set()
    while True:
        status = _docker("inspect", "-f", "{{.State.Status}}", name, timeout=120).stdout.strip()
        if status in ("exited", "dead"):
            log = _docker("logs", "--tail", "60", name, check=False, timeout=120)
            text = (log.stdout or "") + (log.stderr or "")
            missing = MISSING_FOLDER.findall(text)
            if missing and missing[-1] not in made and len(made) < MISSING_TRIES:
                _tablespace_folder(under_roots(missing[-1], roots, temp), uid, gid)
                made.add(missing[-1])
                _docker("start", name, timeout=300)
                continue
            fatal = [line.strip() for line in text.splitlines() if "FATAL" in line or "PANIC" in line]
            raise ClusterError("the temporary cluster stopped while replaying: " + (" | ".join(fatal[-3:])
                                                                                    or text.strip()[-600:]))
        done = _temp_sql(name, "SELECT pg_is_in_recovery();\n", check=False)
        if done.returncode == 0 and done.stdout.strip() == "f":
            return len(made)
        if time.monotonic() > end:
            raise ClusterError(f"the temporary cluster is still replaying after {RECOVERY_TIMEOUT} s")
        time.sleep(1)


def _empty(project: Path, target: str, template: str) -> None:
    """The staging's database made again, empty, its role's, closed."""
    machinedb.sql(project, "postgres", f"DROP DATABASE IF EXISTS {machinedb.ident(target)} WITH (FORCE);\n")
    machinedb.sql(project, "postgres", f"CREATE DATABASE {machinedb.ident(target)} OWNER {machinedb.ident(target)} "
                                       f"TEMPLATE {machinedb.ident(template)};\n")


def restore(major: str, workspace: str, source: str, target: str, when: str, stop=None) -> dict:
    """The database of the environment source as it was at when (ISO, this
    machine's time, a minute) into the database of the environment target,
    both of workspace, on the cluster of major. stop(): called once the
    dump is there, before target's database is dropped (its Odoo stopped).
    Every failure is a RecoveryFailed, in words the workspace's users may
    read; the temporary cluster is deleted whatever happens."""
    project = machinedb.project_of(major)
    conf = settings(project)
    envs = machinedb.environments(project)
    for role in (source, target):
        if (envs.get(machinedb._env(role)) or {}).get("workspace") != workspace:
            raise RecoveryFailed(f"{role} is not a database of this workspace on the machine's PostgreSQL")
    if source == target:
        raise RecoveryFailed("the source and the target are one database")
    if not conf["enabled"]:
        raise RecoveryFailed("recovery at a minute is off for the machine's PostgreSQL: an admin of the machine "
                             "switches it on, in the Database tab of the machine")
    try:
        at = datetime.datetime.fromisoformat(when).replace(tzinfo=None, second=0, microsecond=0)
    except ValueError:
        raise RecoveryFailed(f"{when!r} is not a time, as 2026-11-20T09:41") from None
    if at >= datetime.datetime.now():
        raise RecoveryFailed(f"{at:%Y-%m-%d %H:%M} is not past yet: choose a minute before now")
    found = bases(project)
    usable = [b for b in found if datetime.datetime.fromisoformat(b["time"]) <= at]
    if not usable:
        first = _minute_after(datetime.datetime.fromisoformat(found[0]["time"])) if found else None
        raise RecoveryFailed(f"no full copy before {at:%Y-%m-%d %H:%M}: the first minute that can be reached is "
                             f"{first.isoformat(sep=' ', timespec='minutes') if first else 'none yet'}")
    chosen = usable[-1]
    uid, gid = machinedb.pg_user()
    mount = Path(str(envs[target].get("mount") or ""))
    files = mount / machinedb.WS_FOLDER
    if not mount.is_absolute() or not files.is_dir() or files.is_symlink():
        raise RecoveryFailed("the disk of the workspace has no folder for its databases")
    roots = machinedb._roots_of((project / "compose.yaml").read_text(encoding="utf-8"))
    token = secrets.token_hex(6)
    temp = project.parent / f"{TEMP_PREFIX}{major}-{token}"
    name = container_name(major, temp)
    dump, listing = files / f"recovery-{token}.dump", files / f"recovery-{token}.list"
    template = machinedb.template_name(workspace)
    measures: dict = {}
    step = "check"
    started = time.monotonic()
    with _flock(project / LOCK_FILE) as got, _interruptible() as cleanup_starts:
        if not got:
            raise RecoveryFailed("another recovery at a minute is running on this machine: wait for it to end")
        dropped = False
        try:
            # what a recovery that was killed left: the lock is ours, so it is nobody's
            _sweep(project, major)
            fits = room(project, conf, max(chosen["data_size"], int(machinedb.sql(
                project, "postgres", "SELECT sum(pg_database_size(oid)) FROM pg_database;\n").strip() or 0)))
            if not fits["fits"]:
                _log(project, f"recovery of {source} at {at:%Y-%m-%d %H:%M} refused: the temporary cluster needs "
                              f"{fits['need']} bytes, {fits['free']} are free, {fits['reserve']} must stay free")
                raise RecoveryFailed("the machine's disk has not the room for the temporary copy a recovery works "
                                     "on: the machine's admin frees room, or lowers the room to leave free "
                                     "(Database tab of the machine)")
            step, mark = "archive", time.monotonic()
            _archive_now(project)
            measures["archive"] = round(time.monotonic() - mark, 1)
            step, mark = "unpack", time.monotonic()
            temp.mkdir(mode=0o700)
            pgdata = temp / "data" / "pgdata"
            for folder in (temp / "data", pgdata, temp / "conf", temp / "roots"):
                folder.mkdir(mode=0o700)
            os.chown(temp / "data", uid, gid)
            for index in range(len(roots)):
                (temp / "roots" / str(index)).mkdir(mode=0o755)
            base = base_folder(project) / chosen["name"]
            subprocess.run(["tar", "-xzf", str(base / "base.tar.gz"), "-C", str(pgdata)], check=True, env=ENV,
                           capture_output=True, timeout=BASE_TIMEOUT)
            for oid, location in tablespaces(pgdata).items():
                where = under_roots(location, roots, temp)
                _tablespace_folder(where, uid, gid)
                subprocess.run(["tar", "-xzf", str(base / f"{oid}.tar.gz"), "-C", str(where)], check=True, env=ENV,
                               capture_output=True, timeout=BASE_TIMEOUT)
            # the tablespaces of now that the base has not: their folders, for a CREATE TABLESPACE replayed
            for rec in envs.values():
                with contextlib.suppress(ClusterError):
                    _tablespace_folder(under_roots(str(Path(str(rec.get("mount") or "")) / machinedb.WS_FOLDER / "ts"),
                                                   roots, temp), uid, gid)
            (pgdata / "pg_wal").mkdir(mode=0o700, exist_ok=True)
            (pgdata / "recovery.signal").write_text("")
            for path in (pgdata, pgdata / "pg_wal", pgdata / "recovery.signal"):
                os.chown(path, uid, gid)
            os.chmod(pgdata, 0o700)
            machinedb.write(temp / "conf" / "pg_hba.conf", TEMP_HBA, mode=0o640, uid=0, gid=gid)
            os.chown(temp / "conf", 0, gid)
            os.chmod(temp / "conf", 0o750)
            measures["unpack"] = round(time.monotonic() - mark, 1)
            step, mark = "replay", time.monotonic()
            _docker(*run_command(major, name, temp, project, roots, uid, gid, at.astimezone().isoformat(sep=" ")),
                    timeout=600)
            later = _replayed(name, temp, roots, uid, gid)
            if later:
                measures["tablespaces_after_base"] = later
                _log(project, f"recovery of {source}: {later} tablespaces made after the base got their folder in "
                              "the temporary cluster")
            measures["replay"] = round(time.monotonic() - mark, 1)
            if _temp_sql(name, f"SELECT 1 FROM pg_database WHERE datname = '{source}';\n").stdout.strip() != "1":
                raise RecoveryFailed(f"at {at:%Y-%m-%d %H:%M} the database of this instance was not there yet: "
                                     "choose a later minute")
            step, mark = "dump", time.monotonic()
            fd = os.open(dump, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as fh:
                os.fchown(fh.fileno(), uid, gid)
                _docker("exec", name, "pg_dump", "-Fc", "-U", "postgres", "-d", source, stdout=fh,
                        timeout=RECOVERY_TIMEOUT)
            measures["dump"] = round(time.monotonic() - mark, 1)
            measures["temp_bytes"] = _sizes(temp)
            # the temporary cluster has done its part: it goes before the staging is written
            remove_temp(major, temp)
            step, mark = "restore", time.monotonic()
            found_list = machinedb._run(project, "exec", "-T", SERVICE, "pg_restore", "-l", str(dump)).stdout
            extensions, kept_list = machinedb.restore_list(found_list)
            machinedb.write(listing, kept_list, mode=0o600, uid=uid, gid=gid)
            stopping = time.monotonic()
            if stop is not None:
                stop()
            measures["stop"] = round(time.monotonic() - stopping, 1)
            dropped = True
            _empty(project, target, template)
            machinedb.make_extensions(lambda db, q: machinedb.sql(project, db, q), target, extensions)
            jobs = str(max(1, min(os.cpu_count() or 1, 8)))
            done = machinedb._run(project, "exec", "-T", SERVICE, "pg_restore", "-j", jobs, "--no-owner", "--no-acl",
                                  f"--role={target}", "-U", "postgres", "-d", target, "-L", str(listing), str(dump),
                                  check=False, timeout=RECOVERY_TIMEOUT)
            if done.returncode:
                raise ClusterError(f"pg_restore failed: {(done.stderr or '').strip()[-600:]}")
            machinedb.close(project, target, target)
            measures["restore"] = round(time.monotonic() - mark - measures["stop"], 1)
        except BaseException as exc:
            cleanup_starts()
            if dropped:
                # a staging half written is no staging: its database empty, as it was made
                with contextlib.suppress(ClusterError, subprocess.SubprocessError):
                    _empty(project, target, template)
                    machinedb.close(project, target, target)
            public = isinstance(exc, RecoveryFailed)
            stopped = isinstance(exc, (Interrupted, KeyboardInterrupt))
            reason = "stopped" if stopped else str(exc)
            _log(project, f"recovery of {source} at {at:%Y-%m-%d %H:%M} into {target} "
                          f"{'stopped' if stopped else 'failed'} while {STEPS[step]}: {reason}")
            _save_last(project, failure={"at": datetime.datetime.now().isoformat(timespec="seconds"),
                                         "database": source, "target": target,
                                         "minute": at.isoformat(timespec="minutes"),
                                         "step": step, "reason": reason[-600:]})
            if public or stopped:
                raise
            if not isinstance(exc, (ClusterError, subprocess.SubprocessError, OSError)):
                raise
            raise RecoveryFailed(f"the recovery failed while {STEPS[step]}: the temporary copy is deleted and "
                                 "the production was not touched. The reason is in the recovery log of the "
                                 "machine's PostgreSQL, which the machine's admin reads") from None
        finally:
            cleanup_starts()
            remove_temp(major, temp)
            for path in (dump, listing):
                path.unlink(missing_ok=True)
    seconds = round(time.monotonic() - started, 1)
    _save_last(project, recovery={"at": datetime.datetime.now().isoformat(timespec="seconds"), "database": source,
                                  "target": target,
                                  "minute": at.isoformat(timespec="minutes"), "base": chosen["time"],
                                  "seconds": seconds, **measures})
    _log(project, f"recovery of {source} at {at:%Y-%m-%d %H:%M} into {target}: {seconds} s, "
                  f"{measures.get('temp_bytes', 0)} bytes of temporary cluster, deleted")
    return {"restored": at.isoformat(timespec="minutes"), "base": chosen["time"], "database": target,
            "seconds": seconds, "steps": {k: measures[k] for k in (*STEPS, "stop") if k in measures}}
