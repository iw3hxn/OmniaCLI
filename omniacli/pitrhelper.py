# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Point-in-time recovery of a workspace instance's database, the root
helper's part (workspace-helper pitr-*): PostgreSQL's own way, no other
tool.

- enable: the database service gets a volume of its own, data/wal mounted on
  WAL_MOUNT (a line added to its compose.yaml; the service is made again);
  PostgreSQL archives each WAL segment there (archive_mode, archive_command,
  archive_timeout: at most ARCHIVE_TIMEOUT seconds of work at risk), and a
  first base backup is made.
  Each segment is archived compressed (gzip, written beside under a
  temporary name and renamed: never one cut short, never over one that is
  there); an archive of before (segments copied whole) stays readable.
- disable: nothing more is archived, at once and without a restart
  (archive_command = /bin/true; archive_mode off at the next restart of the
  database, whenever it is); the line of the volume taken out of
  compose.yaml, the bases and the archive deleted.
- base: a base backup (pg_basebackup, tar, compressed) into data/wal/base;
  the retention keeps the bases of the last `days` days and the newest one
  before them (so the minutes of the last `days` days can be reached), and
  deletes the archived segments older than the oldest kept base. An
  instance switched on before the archive was compressed gets the new
  archive_command here, with a reload, no restart.
- state: whether it is on, the base backups, the archived segments, the
  first and last minute a restore can reach, the room on its disk.
- restore: another instance's database cluster made again at a minute: its
  data folder replaced by a base backup of the source and the archived
  segments after it, replayed up to that minute (recovery_target_time),
  then promoted; the database renamed as the target names it, its roles
  given the target's passwords. Whatever fails, the target's data folder of
  before is put back.

Every path is under the projects of the workspace's instances; the times are
this machine's.
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from . import composehelper
from .composehelper import ENV, HelperError

WAL_MOUNT = "/var/lib/postgresql/wal"
PGDATA_LINE = "./data/pgdata:/var/lib/postgresql/data/pgdata"
WAL_LINE = f"./data/wal:{WAL_MOUNT}"
PGDATA_MOUNT = PGDATA_LINE.split(":", 1)[1]
RESTORE_WAL = "omniacli_restore_wal"
DB_SERVICE = "db"
ARCHIVE_TIMEOUT = 300
# the days of the window kept when the instance's record says none: what a
# window of 7 daily bases kept before it could be chosen
KEEP_DAYS = 7
DAYS_RANGE = (1, 35)
SEGMENT = re.compile(r"[0-9A-F]{24}")
# a timeline's history file, archived as the segments are
HISTORY = re.compile(r"[0-9A-F]{8}\.history")
COMPRESSED = ".gz"
OFF_COMMAND = "/bin/true"
LOCK_FILE = "pitr.lock"
STAMP = "%Y%m%dT%H%M%S"
READY_TIMEOUT = 180
RECOVERY_TIMEOUT = 3600
DATABASE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_]{0,62}")


def _dc(project: Path, *args: str, check: bool = True, input: str | None = None) -> subprocess.CompletedProcess:
    done = subprocess.run(composehelper.compose(project, *args), env=ENV, cwd=project, capture_output=True,
                          text=True, input=input)
    if check and done.returncode:
        raise HelperError(f"docker compose {args[0]} in {project} failed: {(done.stderr or done.stdout).strip()[-400:]}")
    return done


def _psql(project: Path, sql: str, database: str = "postgres", check: bool = True) -> str:
    done = _dc(project, "exec", "-T", "--user", "postgres", DB_SERVICE, "psql", "-X", "-At", "-v", "ON_ERROR_STOP=1",
               "-U", "postgres", "-d", database, check=check, input=sql)
    return done.stdout.strip()


def _wait_ready(project: Path, timeout: int = READY_TIMEOUT) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        done = _dc(project, "exec", "-T", DB_SERVICE, "pg_isready", "-h", "127.0.0.1", "-U", "postgres", check=False)
        if done.returncode == 0:
            return
        # a database that cannot start restarts in a loop: told at once, with its log
        state = _dc(project, "ps", "-a", "--format", "{{.State}}", DB_SERVICE, check=False).stdout.strip()
        if state in ("restarting", "exited", "dead"):
            log = _dc(project, "--ansi", "never", "logs", "--no-log-prefix", "--tail", "8", DB_SERVICE, check=False)
            fatal = [line for line in log.stdout.splitlines() if "FATAL" in line or "PANIC" in line]
            raise HelperError(f"the database of {project} does not start ({state}): "
                              + (" | ".join(fatal[-2:]) or log.stdout.strip()[-400:]))
        time.sleep(2)
    raise HelperError(f"the database of {project} is not ready after {timeout} s")


def _owner(project: Path) -> tuple[int, int]:
    info = (project / "data" / "pgdata").stat()
    return info.st_uid, info.st_gid


def _wal(project: Path) -> Path:
    return project / "data" / "wal"


def _chown_tree(path: Path, uid: int, gid: int) -> None:
    os.chown(path, uid, gid)
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            os.chown(os.path.join(root, name), uid, gid, follow_symlinks=False)


def archive_command(folder: str = f"{WAL_MOUNT}/archive") -> str:
    """The segment compressed into a temporary name, synced, then renamed:
    one cut short by a crash is never taken for an archived one. One that
    is there already (archived again after a crash) is fine when it is the
    same, a failure otherwise: never written over."""
    target = f"{folder}/%f"
    return (f"if [ -f {target}{COMPRESSED} ]; then "
            f"[ \"$(gzip -dc {target}{COMPRESSED} | md5sum)\" = \"$(md5sum < %p)\" ]; "
            f"elif [ -f {target} ]; then [ \"$(md5sum < {target})\" = \"$(md5sum < %p)\" ]; "
            f"else gzip -c %p > {target}{COMPRESSED}.part && sync {target}{COMPRESSED}.part && "
            f"mv {target}{COMPRESSED}.part {target}{COMPRESSED}; fi")


def restore_command(folder: str) -> str:
    """A segment read back, compressed or copied whole (an archive of before)."""
    return (f"if [ -f {folder}/%f{COMPRESSED} ]; then gzip -dc {folder}/%f{COMPRESSED} > \"%p\"; "
            f"else cp {folder}/%f \"%p\"; fi")


def wal_name(name: str) -> str | None:
    """The name PostgreSQL gives an archived file (a segment, a history
    file), whether it is compressed or not; None for anything else (a file
    being written, a backup's label)."""
    plain = name[:-len(COMPRESSED)] if name.endswith(COMPRESSED) else name
    return plain if SEGMENT.fullmatch(plain) or HISTORY.fullmatch(plain) else None


def days_of(value) -> int:
    """The days of the window: an integer of DAYS_RANGE, KEEP_DAYS when none was chosen."""
    if value in (None, ""):
        return KEEP_DAYS
    try:
        days = int(str(value))
    except ValueError:
        raise HelperError(f"{value!r} is not a number of days") from None
    if not DAYS_RANGE[0] <= days <= DAYS_RANGE[1]:
        raise HelperError(f"the days kept are {DAYS_RANGE[0]} to {DAYS_RANGE[1]}, not {days}")
    return days


@contextlib.contextmanager
def _lock(project: Path, wait: bool):
    """The lock a restore holds while it reads the archive: the retention
    and the switch off never delete under it. Yields whether it was got."""
    with open(project / "data" / LOCK_FILE, "a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _sizes(folder: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(folder):
        for name in files:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(root, name)).st_size
    return total


def enabled(project: Path) -> bool:
    try:
        return WAL_LINE in (project / "compose.yaml").read_text(encoding="utf-8")
    except OSError:
        return False


def _show(project: Path, name: str) -> str:
    return _psql(project, f"SHOW {name};", check=False)


def _write_compose(compose_file: Path, text: str) -> None:
    tmp = compose_file.with_name(compose_file.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, compose_file.stat().st_mode & 0o777)
    os.replace(tmp, compose_file)


def enable(project: Path) -> dict:
    """On: the volume's line, the archive's settings, a first base. A
    restart only when archive_mode is not on in the running database
    (switched off and on again before a restart: a reload is enough)."""
    compose_file = project / "compose.yaml"
    text = compose_file.read_text(encoding="utf-8")
    if WAL_LINE not in text:
        lines = text.splitlines(keepends=True)
        at = next((i for i, line in enumerate(lines) if line.strip() == f"- {PGDATA_LINE}"), None)
        if at is None:
            raise HelperError(f"{compose_file} does not mount {PGDATA_LINE}: not a template's database service")
        indent = lines[at][:len(lines[at]) - len(lines[at].lstrip())]
        lines.insert(at + 1, f"{indent}- {WAL_LINE}\n")
        _write_compose(compose_file, "".join(lines))
    uid, gid = _owner(project)
    for folder in (_wal(project), _wal(project) / "archive", _wal(project) / "base"):
        folder.mkdir(mode=0o700, exist_ok=True)
        os.chown(folder, uid, gid)
    _dc(project, "up", "-d", DB_SERVICE)
    _wait_ready(project)
    running = _show(project, "archive_mode")
    _psql(project, f"ALTER SYSTEM SET archive_mode = 'on';\n"
                   f"ALTER SYSTEM SET archive_command = '{archive_command()}';\n"
                   f"ALTER SYSTEM SET archive_timeout = '{ARCHIVE_TIMEOUT}';\n"
                   "SELECT pg_reload_conf();\n")
    if running != "on":
        _dc(project, "restart", DB_SERVICE)
        _wait_ready(project)
    made = base(project)
    return {**made["state"], "base": made["base"], "restarted": running != "on"}


def disable(project: Path) -> dict:
    """Off, without a restart: nothing more is archived from now
    (archive_command = /bin/true, reloaded); archive_mode is off at the
    next restart of the database, whenever it is. The volume's line goes
    from compose.yaml (enabled() says off from now); its folder stays: a
    database still running keeps the mount until its container is made
    again, and writes nothing there any more, and switching on again before
    that finds the same folder. The bases and the archive are deleted.
    Refused while a restore reads them."""
    with _lock(project, wait=False) as got:
        if not got:
            raise HelperError("a recovery at a minute is reading the archive now: wait for it to end")
        _psql(project, f"ALTER SYSTEM SET archive_command = '{OFF_COMMAND}';\n"
                       "ALTER SYSTEM RESET archive_timeout;\n"
                       "ALTER SYSTEM RESET archive_mode;\n"
                       "SELECT pg_reload_conf();\n")
        compose_file = project / "compose.yaml"
        text = compose_file.read_text(encoding="utf-8")
        if WAL_LINE in text:
            _write_compose(compose_file, "".join(line for line in text.splitlines(keepends=True)
                                                 if line.strip() != f"- {WAL_LINE}"))
        freed = 0
        for folder in (_wal(project) / "archive", _wal(project) / "base"):
            if folder.is_dir() and not folder.is_symlink():
                freed += _sizes(folder)
                for item in folder.iterdir():
                    if item.is_dir() and not item.is_symlink():
                        shutil.rmtree(item)
                    else:
                        item.unlink()
    return {"enabled": False, "freed": freed}


def _bases(project: Path) -> list[tuple[datetime.datetime, Path]]:
    found = []
    folder = _wal(project) / "base"
    for one in sorted(folder.iterdir()) if folder.is_dir() else []:
        try:
            found.append((datetime.datetime.strptime(one.name, STAMP), one))
        except ValueError:
            continue
    return found


def retention(project: Path, days: int, now: datetime.datetime | None = None) -> dict:
    """The bases of the last days days stay, with the newest one before them
    (the minutes of the whole window are reached from it); the older ones
    go, with the segments archived before the oldest kept base (an hour of
    margin), and what an archiving cut short left an hour ago or more."""
    now = now or datetime.datetime.now()
    since = now - datetime.timedelta(days=days)
    bases = _bases(project)
    before = [b for b in bases if b[0] < since]
    keep = [b for b in bases if b[0] >= since] + before[-1:]
    gone = 0
    for one in bases:
        if one not in keep:
            shutil.rmtree(one[1])
            gone += 1
    removed = 0
    if keep:
        oldest = min(when for when, _ in keep).timestamp() - 3600
        archive = _wal(project) / "archive"
        for item in archive.iterdir() if archive.is_dir() else []:
            if item.name.endswith(".part"):
                if time.time() - item.stat().st_mtime > 3600:
                    item.unlink()
                continue
            if SEGMENT.match(item.name) and item.stat().st_mtime < oldest:
                item.unlink()
                removed += 1
    return {"bases": gone, "segments": removed, "kept": len(keep)}


def base(project: Path, days=None) -> dict:
    """A base backup, then the retention of days days (KEEP_DAYS when none).
    An archive_command of before (a whole copy) is replaced by the
    compressed one, reloaded: no restart."""
    days = days_of(days)
    if not enabled(project):
        raise HelperError("point-in-time recovery is not on for this instance")
    upgraded = False
    if _show(project, "archive_command") != archive_command():
        _psql(project, f"ALTER SYSTEM SET archive_command = '{archive_command()}';\nSELECT pg_reload_conf();\n")
        upgraded = True
    stamp = datetime.datetime.now().strftime(STAMP)
    while (_wal(project) / "base" / stamp).exists():
        # one a second: the next second's name
        time.sleep(1)
        stamp = datetime.datetime.now().strftime(STAMP)
    _dc(project, "exec", "-T", "--user", "postgres", DB_SERVICE, "pg_basebackup", "-U", "postgres",
        "-D", f"{WAL_MOUNT}/base/{stamp}", "-Ft", "-z", "-X", "none", "-c", "fast")
    # the segment being written now goes to the archive: the base can be restored at once
    _psql(project, "SELECT pg_switch_wal();", check=False)
    with _lock(project, wait=False) as got:
        pruned = retention(project, days) if got else {"skipped": "a restore reads the archive now", "bases": 0,
                                                       "segments": 0}
    return {"base": stamp, "size": sum(f.stat().st_size for f in (_wal(project) / "base" / stamp).iterdir()),
            "days": days, "upgraded": upgraded, "pruned": pruned, "state": state(project)}


def state(project: Path) -> dict:
    if not enabled(project):
        return {"enabled": False}
    bases = [{"name": folder.name, "time": when.isoformat(timespec="seconds"),
              "size": sum(f.stat().st_size for f in folder.iterdir() if f.is_file())}
             for when, folder in _bases(project)]
    archive = _wal(project) / "archive"
    # every archived file (a segment, a backup's label, a history file), not one being written
    files = [p for p in archive.iterdir() if (SEGMENT.match(p.name) or wal_name(p.name)) and not p.name.endswith(".part")] \
        if archive.is_dir() else []
    segments = [p for p in files if SEGMENT.fullmatch(wal_name(p.name) or "")]
    newest = max((p.stat().st_mtime for p in segments), default=None)
    archiver = _psql(project, "SELECT coalesce(last_archived_time::text, ''), failed_count, "
                              "coalesce(last_failed_time::text, '') FROM pg_stat_archiver;", check=False)
    parts = archiver.split("|") if archiver else []
    archive_size = sum(p.stat().st_size for p in files)
    bases_size = sum(b["size"] for b in bases)
    return {"enabled": True, "bases": bases,
            "segments": len(segments), "archive_size": archive_size,
            "compressed": sum(1 for p in segments if p.name.endswith(COMPRESSED)),
            "bases_size": bases_size, "size": archive_size + bases_size,
            "free": shutil.disk_usage(_wal(project)).free,
            "first": bases[0]["time"] if bases else None,
            "last": datetime.datetime.fromtimestamp(newest).isoformat(timespec="seconds") if newest else None,
            "last_archived": parts[0] if parts else "", "failed": int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0,
            "last_failed": parts[2] if len(parts) > 2 else ""}


def room(found: dict, days: int, now: datetime.datetime | None = None) -> dict:
    """Whether the disk of the archive has the room for a window of days
    days: the archive's bytes a day over the range it holds now (from the
    oldest kept base) times the days, and a base a day, against what they
    take now and the free room. per_day is None while the range is shorter
    than an hour: not measurable yet. It only warns: the window is never
    shortened by itself."""
    now = now or datetime.datetime.now()
    free = int(found.get("free") or 0)
    bases = found.get("bases") or []
    unknown = {"free": free, "per_day": None, "expected": None, "need": 0, "fits": True}
    if not found.get("enabled") or not bases:
        return unknown
    span = (now - datetime.datetime.fromisoformat(bases[0]["time"])).total_seconds() / 86400
    if span < 1 / 24:
        return unknown
    per_day = int((found.get("archive_size") or 0) / span)
    expected = per_day * days + int(bases[-1]["size"]) * (days + 1)
    need = max(0, expected - int(found.get("size") or 0))
    return {"free": free, "per_day": per_day, "expected": expected, "need": need, "fits": free >= need}


def _env_value(project: Path, key: str) -> str:
    for line in (project / ".env").read_text(encoding="utf-8").splitlines():
        name, sep, value = line.partition("=")
        if sep and name.strip() == key:
            return value.strip()
    raise HelperError(f"{project}/.env has no {key}")


def _literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def restore(source: Path, target: Path, when: str, source_db: str, target_db: str) -> dict:
    """target's database cluster made again from source's archive at when
    (ISO, this machine's time)."""
    if not enabled(source):
        raise HelperError(f"point-in-time recovery is not on for {source}")
    for name in (source_db, target_db):
        if not DATABASE.fullmatch(name):
            raise HelperError(f"{name!r} is not a database name")
    try:
        at = datetime.datetime.fromisoformat(when)
    except ValueError:
        raise HelperError(f"{when!r} is not a time, as 2026-11-20T09:41") from None
    at = at.replace(tzinfo=None)
    info = state(source)
    bases = [b for b in info["bases"] if datetime.datetime.fromisoformat(b["time"]) <= at]
    if not bases:
        raise HelperError(f"no base backup before {at:%Y-%m-%d %H:%M}: the first minute that can be restored is "
                          f"{info['first'] or 'none yet'}")
    if info["last"] is None or at > datetime.datetime.fromisoformat(info["last"]):
        raise HelperError(f"{at:%Y-%m-%d %H:%M} is after the last archived segment ({info['last']}): "
                          f"wait {ARCHIVE_TIMEOUT // 60} minutes, or choose an earlier minute")
    chosen = _wal(source) / "base" / bases[-1]["name"]
    pgdata = target / "data" / "pgdata"
    uid, gid = _owner(target)
    old = pgdata.with_name("pgdata.omniacli-before-restore")
    if old.exists():
        shutil.rmtree(old)
    _dc(target, "stop")
    os.rename(pgdata, old)
    try:
        pgdata.mkdir(mode=0o700)
        subprocess.run(["tar", "-xzf", str(chosen / "base.tar.gz"), "-C", str(pgdata)], check=True, env=ENV,
                       capture_output=True)
        (pgdata / "pg_wal").mkdir(mode=0o700, exist_ok=True)
        # the archived segments beside it, read by restore_command (a recovery
        # without one is refused by PostgreSQL)
        pg_wal = pgdata / RESTORE_WAL
        pg_wal.mkdir(mode=0o700)
        # from the start of the base (its name), with a margin, compressed or not as they are (restore_command
        # reads both); under the lock: the retention and the switch off delete nothing while they are copied
        since = datetime.datetime.strptime(chosen.name, STAMP).timestamp() - 600
        copied = 0
        with _lock(source, wait=True):
            for segment in sorted((_wal(source) / "archive").iterdir()):
                name = wal_name(segment.name)
                if name and (HISTORY.fullmatch(name) or segment.stat().st_mtime >= since):
                    shutil.copy2(segment, pg_wal / segment.name)
                    copied += 1
        target_time = at.astimezone().isoformat(sep=" ")
        with open(pgdata / "postgresql.auto.conf", "a", encoding="utf-8") as fh:
            fh.write("\n# written by omniacli workspace-helper pitr-restore\n"
                     "archive_mode = 'off'\n"
                     f"restore_command = '{restore_command(f'{PGDATA_MOUNT}/{RESTORE_WAL}')}'\n"
                     f"recovery_target_time = '{target_time}'\n"
                     "recovery_target_action = 'promote'\n")
        (pgdata / "recovery.signal").write_text("")
        _chown_tree(pgdata, uid, gid)
        _dc(target, "up", "-d", DB_SERVICE)
        _wait_ready(target, RECOVERY_TIMEOUT)
        end = time.monotonic() + RECOVERY_TIMEOUT
        while _psql(target, "SELECT pg_is_in_recovery();", check=False) != "f":
            if time.monotonic() > end:
                raise HelperError(f"the database is still replaying after {RECOVERY_TIMEOUT} s")
            time.sleep(3)
        # promoted: the recovery's settings and segments are not needed any more
        _psql(target, "ALTER SYSTEM RESET restore_command;\nALTER SYSTEM RESET recovery_target_time;\n"
                      "ALTER SYSTEM RESET recovery_target_action;\n")
        shutil.rmtree(pg_wal, ignore_errors=True)
        if source_db != target_db:
            _psql(target, f'ALTER DATABASE "{source_db}" RENAME TO "{target_db}";')
        _psql(target, f"ALTER ROLE odoo PASSWORD {_literal(_env_value(target, 'DB_PASSWORD'))};\n"
                      f"ALTER ROLE postgres PASSWORD {_literal(_env_value(target, 'DB_SUPER_PASSWORD'))};\n")
    except BaseException:
        _dc(target, "stop", check=False)
        if pgdata.exists():
            shutil.rmtree(pgdata)
        os.rename(old, pgdata)
        _dc(target, "up", "-d", check=False)
        raise
    shutil.rmtree(old)
    _dc(target, "up", "-d")
    return {"restored": at.isoformat(timespec="minutes"), "base": bases[-1]["name"], "segments": copied,
            "database": target_db}
