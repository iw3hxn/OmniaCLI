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
- base: a base backup (pg_basebackup, tar, compressed) into data/wal/base;
  the newest KEEP_BASES kept, the archived segments older than the oldest
  kept base deleted.
- state: whether it is on, the base backups, the archived segments, the
  first and last minute a restore can reach.
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

import datetime
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
KEEP_BASES = 7
SEGMENT = re.compile(r"[0-9A-F]{24}")
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


def enabled(project: Path) -> bool:
    try:
        return WAL_LINE in (project / "compose.yaml").read_text(encoding="utf-8")
    except OSError:
        return False


def enable(project: Path) -> dict:
    compose_file = project / "compose.yaml"
    text = compose_file.read_text(encoding="utf-8")
    if WAL_LINE not in text:
        lines = text.splitlines(keepends=True)
        at = next((i for i, line in enumerate(lines) if line.strip() == f"- {PGDATA_LINE}"), None)
        if at is None:
            raise HelperError(f"{compose_file} does not mount {PGDATA_LINE}: not a template's database service")
        indent = lines[at][:len(lines[at]) - len(lines[at].lstrip())]
        lines.insert(at + 1, f"{indent}- {WAL_LINE}\n")
        tmp = compose_file.with_name(compose_file.name + ".tmp")
        tmp.write_text("".join(lines), encoding="utf-8")
        os.chmod(tmp, compose_file.stat().st_mode & 0o777)
        os.replace(tmp, compose_file)
    uid, gid = _owner(project)
    for folder in (_wal(project), _wal(project) / "archive", _wal(project) / "base"):
        folder.mkdir(mode=0o700, exist_ok=True)
        os.chown(folder, uid, gid)
    _dc(project, "up", "-d", DB_SERVICE)
    _wait_ready(project)
    command = f"test ! -f {WAL_MOUNT}/archive/%f && cp %p {WAL_MOUNT}/archive/%f"
    _psql(project, f"ALTER SYSTEM SET archive_mode = 'on';\n"
                   f"ALTER SYSTEM SET archive_command = '{command}';\n"
                   f"ALTER SYSTEM SET archive_timeout = '{ARCHIVE_TIMEOUT}';\n")
    _dc(project, "restart", DB_SERVICE)
    _wait_ready(project)
    made = base(project)
    return {**state(project), "base": made["base"]}


def base(project: Path) -> dict:
    if not enabled(project):
        raise HelperError("point-in-time recovery is not on for this instance")
    stamp = datetime.datetime.now().strftime(STAMP)
    while (_wal(project) / "base" / stamp).exists():
        # one a second: the next second's name
        time.sleep(1)
        stamp = datetime.datetime.now().strftime(STAMP)
    _dc(project, "exec", "-T", "--user", "postgres", DB_SERVICE, "pg_basebackup", "-U", "postgres",
        "-D", f"{WAL_MOUNT}/base/{stamp}", "-Ft", "-z", "-X", "none", "-c", "fast")
    # the segment being written now goes to the archive: the base can be restored at once
    _psql(project, "SELECT pg_switch_wal();", check=False)
    bases = sorted(p for p in (_wal(project) / "base").iterdir() if p.is_dir())
    for old in bases[:-KEEP_BASES]:
        shutil.rmtree(old)
    kept = sorted(p for p in (_wal(project) / "base").iterdir() if p.is_dir())
    if kept:
        oldest = kept[0].stat().st_mtime - 3600
        for segment in (_wal(project) / "archive").iterdir():
            if SEGMENT.match(segment.name) and segment.stat().st_mtime < oldest:
                segment.unlink()
    return {"base": stamp, "size": sum(f.stat().st_size for f in (_wal(project) / "base" / stamp).iterdir())}


def state(project: Path) -> dict:
    if not enabled(project):
        return {"enabled": False}
    bases = []
    for folder in sorted((_wal(project) / "base").iterdir()) if (_wal(project) / "base").is_dir() else []:
        try:
            when = datetime.datetime.strptime(folder.name, STAMP)
        except ValueError:
            continue
        bases.append({"name": folder.name, "time": when.isoformat(timespec="seconds"),
                      "size": sum(f.stat().st_size for f in folder.iterdir() if f.is_file())})
    segments = [p for p in (_wal(project) / "archive").iterdir() if SEGMENT.match(p.name)] \
        if (_wal(project) / "archive").is_dir() else []
    newest = max((p.stat().st_mtime for p in segments), default=None)
    archiver = _psql(project, "SELECT coalesce(last_archived_time::text, ''), failed_count, "
                              "coalesce(last_failed_time::text, '') FROM pg_stat_archiver;", check=False)
    parts = archiver.split("|") if archiver else []
    return {"enabled": True, "bases": bases,
            "segments": len(segments), "archive_size": sum(p.stat().st_size for p in segments),
            "first": bases[0]["time"] if bases else None,
            "last": datetime.datetime.fromtimestamp(newest).isoformat(timespec="seconds") if newest else None,
            "last_archived": parts[0] if parts else "", "failed": int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0,
            "last_failed": parts[2] if len(parts) > 2 else ""}


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
        since = chosen.stat().st_mtime - 600
        copied = 0
        for segment in sorted((_wal(source) / "archive").iterdir()):
            if SEGMENT.match(segment.name) and (segment.stat().st_mtime >= since or segment.name.endswith(".history")):
                shutil.copy2(segment, pg_wal / segment.name)
                copied += 1
        target_time = at.astimezone().isoformat(sep=" ")
        with open(pgdata / "postgresql.auto.conf", "a", encoding="utf-8") as fh:
            fh.write("\n# written by omniacli workspace-helper pitr-restore\n"
                     "archive_mode = 'off'\n"
                     f"restore_command = 'cp {PGDATA_MOUNT}/{RESTORE_WAL}/%f \"%p\"'\n"
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
