# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Odoo's log of each measured instance read in its tail every 5 minutes
(AD-019 19.2, slice 2), for the monitor's requests and counts.

- A venv's log_file is read by OmniaCLI itself (logtail.read_since), by
  offset and inode. A file rotated (logrotate's .1, Odoo's logrotate =
  True's dated name) is finished from its old inode, found beside it, before
  the new one is read from its start; one already compressed is a gap,
  declared.
- A compose instance's is Docker's json-file of its Odoo's container, read by
  the root helper's log-read (composehelper.log_read): the bytes after an
  offset, never `docker logs --since` (it reads the whole log again: slice
  0). A container made again starts a new file: a job on the instance reads
  the log first (JobManager's begin), so a recreation in it loses nothing.
- After OmniaCLI was stopped, the log is caught up from the cursor, at most
  CATCH_UP bytes a round: the rest the next round.

The cursor (where the reading is) and the spans not finished yet are kept in
cursor.json beside the instance's history (600). What is read becomes
counts by 5-minute span (odoolog.Counter), written to the history as
records of their own kind ("log"); a span read in two rounds is written
again whole (the last line wins). The pages of an hour are written once the
hour is over ("pages"). Nothing of the lines is kept.
"""

from __future__ import annotations

import asyncio
import configparser
import json
import logging
import os
import re
import time
from pathlib import Path

from . import logtail, odoolog

_logger = logging.getLogger(__name__)

EVERY = 300  # seconds between two reads of an instance's log (Carlo, 26/11/2026: also with the tab open)
CATCH_UP = 20 * 1024 * 1024  # bytes read at most in a round
KEEP_OPEN = 2 * 3600  # the spans kept in the cursor, to be written again whole when more of them is read
CURSOR = "cursor.json"
READ_TIMEOUT = 60
# why the log of an instance gives no requests: told as a sentence, never a zero (UX-018 §6.2)
NO_FILE = "no_file"  # a venv whose Odoo writes no file (stdout, the journal)
NO_ACCESS = "no_access"  # its log level writes no access line (warn, or werkzeug muted)
UNREADABLE = "unreadable"
NOT_YET = "not_yet"  # the first read sets the cursor: the counts start from the next
_HEADER = re.compile(rb"#omniacli-log container=(\S+) inode=(\d+) pos=(\d+) gap=(\S+) more=([01])\n")
QUIET_LEVELS = ("warn", "warning", "error", "critical")


def odoo_conf(path) -> dict:
    """The [options] of an Odoo config file; {} when it cannot be read."""
    if not path:
        return {}
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with open(path, encoding="utf-8") as fh:
            parser.read_file(fh)
        return dict(parser.items("options")) if parser.has_section("options") else {}
    except (OSError, configparser.Error, UnicodeDecodeError):
        return {}


def docker_lines(data: bytes) -> list[str]:
    """The lines of Odoo in Docker's json-file bytes: each entry's log, a line Docker split at 16 KB joined back."""
    lines, partial = [], ""
    for raw in data.split(b"\n"):
        if not raw.strip():
            continue
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        text = entry.get("log") if isinstance(entry, dict) else None
        if not isinstance(text, str):
            continue
        if text.endswith("\n"):
            lines.append(partial + text[:-1])
            partial = ""
        else:
            partial += text
    if partial:
        lines.append(partial)
    return lines


def rotated_of(path: Path, inode: int) -> tuple[Path | None, bool]:
    """The file beside path that has the inode of before (logrotate's path.1, Odoo's path.2026-11-27), and whether
    only compressed ones are there."""
    compressed = False
    try:
        siblings = [p for p in path.parent.iterdir() if p.name.startswith(path.name) and p != path]
    except OSError:
        return None, False
    for candidate in siblings:
        if candidate.suffix in (".gz", ".xz", ".bz2", ".zst"):
            compressed = True
            continue
        try:
            if candidate.stat().st_ino == inode:
                return candidate, False
        except OSError:
            continue
    return None, compressed


def read_file(path: Path, cursor: dict, most: int = CATCH_UP) -> tuple[list[str], dict, str | None, bool]:
    """A venv's log after the cursor: (lines, the new cursor, a gap's reason or None, more to read)."""
    stat = os.stat(path)
    if cursor.get("inode") is None:
        return [], {"inode": stat.st_ino, "pos": stat.st_size}, None, False
    lines, gap, read = [], None, 0
    inode, pos = cursor["inode"], int(cursor.get("pos") or 0)
    if stat.st_ino != inode:
        old, compressed = rotated_of(path, inode)
        if old is not None:
            while read < most:
                found = logtail.read_since(old, pos, inode)
                lines += found["lines"]
                read += found["pos"] - pos
                pos = found["pos"]
                if not found["more"]:
                    break
            if read >= most:
                return lines, {"inode": inode, "pos": pos}, None, True
        else:
            gap = "compressed" if compressed else "rotated"
        inode, pos = stat.st_ino, 0
    elif pos > stat.st_size:
        gap, pos = "truncated", 0  # copytruncate: what was written between the last read and the cut is lost
    more = False
    while read < most:
        found = logtail.read_since(path, pos, inode)
        lines += found["lines"]
        read += found["pos"] - pos
        pos = found["pos"]
        more = found["more"]
        if not more:
            break
    return lines, {"inode": inode, "pos": pos}, gap, more and read >= most


async def helper_read(instance, cursor: dict) -> tuple[bytes, dict, str | None, bool]:
    """One log-read of the root helper: (bytes, the new cursor, a gap's reason or None, more)."""
    service = instance.plugin.log_service(instance)
    if not service:
        raise OSError("no service whose output is the log")
    cmd = instance.restart.command("log-read", service, cursor.get("container") or "-",
                                   str(int(cursor.get("inode") or 0)), str(int(cursor.get("pos") or 0)))
    proc = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE, start_new_session=True)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), READ_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise OSError("the helper did not answer") from None
    head = _HEADER.match(out)
    if proc.returncode or not head:
        raise OSError((err.decode(errors="replace").strip().splitlines() or ["the helper's answer"])[-1][:200])
    container, inode, pos, gap, more = (g.decode() for g in head.groups())
    if container == "-":
        raise OSError("no container")
    new = {"container": container, "inode": int(inode), "pos": int(pos)}
    return out[head.end():], new, None if gap == "-" else gap, more == "1"


async def read_compose(instance, cursor: dict, most: int = CATCH_UP) -> tuple[list[str], dict, str | None, bool]:
    lines, gap, read, more = [], None, 0, True
    while more and read < most:
        data, cursor_new, found_gap, more = await helper_read(instance, cursor)
        first = not cursor.get("container")
        cursor = cursor_new
        gap = gap or found_gap
        read += len(data)
        lines += docker_lines(data)
        if first:
            break
    return lines, cursor, gap, more


def source(instance, role_state=None) -> str | None:
    """Why the log of an instance gives no requests, before reading it: None when it may (the reading says
    the rest)."""
    if instance.restart.type == "compose":
        if role_state is not None:
            try:
                level = str(role_state(Path(instance.restart.service)).get("parameters", {}).get("log_level") or "")
            except (OSError, ValueError):
                level = ""
            if level.lower() in QUIET_LEVELS:
                return NO_ACCESS
        return None
    conf = odoo_conf(getattr(instance.options, "odoo_config", None))
    if instance.restart.type == "systemd" and conf and conf.get("logfile", "").strip().lower() in ("", "false", "none"):
        # Odoo writes on its output (the journal): the file named for it is not its log, unless its unit appends
        # its output there (then it grows)
        log_file = Path(instance.log_file) if instance.log_file else None
        try:
            fresh = log_file is not None and time.time() - log_file.stat().st_mtime < 86400
        except OSError:
            fresh = False
        if not fresh:
            return NO_FILE
    if conf.get("log_level", "").strip().lower() in QUIET_LEVELS:
        return NO_ACCESS
    return None


class InstanceLog:
    """Reads the log of the measured instances every EVERY seconds, one after the other, and writes their counts
    to the history (instancemonitor.InstanceHistory, kind "log", and "pages" for each hour ended)."""

    def __init__(self, settings, history, measured=None, role_state=None, told=None):
        # measured(key): whether it is measured (AD-019 §10.4); told(found): the causes read, {key: [cause]}, for
        # the monitor's alerts (monitor.Monitor.instance_causes)
        self.settings, self.history = settings, history
        self.measured = measured or (lambda key: True)
        self.role_state = role_state
        self.told = told
        self.causes = None  # causes(key): the log's causes of now (instancemonitor.log_causes)
        self._locks: dict[str, asyncio.Lock] = {}
        self._task: asyncio.Task | None = None
        # {key: {"at": time, "state": None or a reason, "gap": [t, why]}}: the last read of each, for the page
        self.reads: dict[str, dict] = {}

    def instances(self) -> list:
        return [inst for srv in self.settings.servers.values() if getattr(srv, "active", True)
                for inst in srv.instances.values() if inst.plugin is not None and self.measured(inst.key)]

    def _cursor_path(self, key: str) -> Path:
        return self.history.folder(key) / CURSOR

    def load_cursor(self, key: str) -> dict:
        try:
            found = json.loads(self._cursor_path(key).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return found if isinstance(found, dict) else {}

    def save_cursor(self, key: str, cursor: dict) -> None:
        path = self._cursor_path(key)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(cursor, out, separators=(",", ":"))
        os.replace(tmp, path)

    def read_state(self, key: str) -> dict:
        """The last read of key: {at, state (None: it reads; NO_FILE, NO_ACCESS, UNREADABLE, NOT_YET), gaps}."""
        found = self.reads.get(key)
        if found is None:
            cursor = self.load_cursor(key)
            found = {"at": cursor.get("at"), "state": cursor.get("state", NOT_YET if not cursor else None)}
        return found

    def open_spans(self, key: str) -> dict:
        """The spans and the pages of the hour not written yet, as the cursor keeps them."""
        cursor = self.load_cursor(key)
        return {"spans": cursor.get("spans") or {}, "hours": cursor.get("hours") or {}}

    async def read(self, instance, now: float | None = None) -> int:
        """Read the new lines of an instance's log and write their counts; the lines read."""
        key = instance.key
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            now = now if now is not None else time.time()
            cursor = self.load_cursor(key)
            state = source(instance, self.role_state)
            if state == NO_FILE:
                self.reads[key] = {"at": int(now), "state": NO_FILE}
                self.save_cursor(key, {**cursor, "at": int(now), "state": NO_FILE})
                return 0
            where = {k: cursor.get(k) for k in ("container", "inode", "pos") if cursor.get(k) is not None}
            try:
                if instance.restart.type == "compose":
                    lines, where, gap, more = await read_compose(instance, where)
                elif instance.log_file is None or not Path(instance.log_file).exists():
                    self.reads[key] = {"at": int(now), "state": NO_FILE}
                    self.save_cursor(key, {**cursor, "at": int(now), "state": NO_FILE})
                    return 0
                else:
                    lines, where, gap, more = await asyncio.to_thread(read_file, Path(instance.log_file), where)
            except OSError as exc:
                _logger.info("monitor: the log of %s is not read: %s", key, exc)
                self.reads[key] = {"at": int(now), "state": UNREADABLE}
                self.save_cursor(key, {**cursor, "at": int(now), "state": UNREADABLE})
                return 0
            first = not any(cursor.get(k) for k in ("inode", "container"))
            counter = odoolog.Counter(span=300)
            for line in lines:
                counter.line(line)
            spans = {int(t): v for t, v in (cursor.get("spans") or {}).items()}
            hours = {int(t): v for t, v in (cursor.get("hours") or {}).items()}
            touched = []
            for start, tally in counter.spans.items():
                pages = tally.pop("pages", None)  # noqa: F841 - the pages are the hour's
                spans[start] = odoolog.merge(spans.get(start, {}), tally)
                touched.append(start)
            for hour, pages in counter.hours.items():
                hours[hour] = odoolog.worst(odoolog.merge_pages(hours.get(hour, {}), pages))
            records = [{"t": t, "y": "log", **spans[t]} for t in sorted(touched)]
            if gap:
                records.append({"t": int(now) // 300 * 300, "y": "gap", "why": gap})
            # an hour is over 5 minutes after its end (a request is written when it ends): its pages written once
            done = [h for h in hours if h + 3600 + 300 <= now]
            records += [{"t": h, "y": "pages", "p": hours.pop(h)} for h in sorted(done)]
            if records:
                self.history.add(key, records, now)
            spans = {t: v for t, v in spans.items() if t >= now - KEEP_OPEN}
            state = state if state == NO_ACCESS else None
            if first:
                state = NOT_YET
            self.reads[key] = {"at": int(now), "state": state, "more": more}
            self.save_cursor(key, {**where, "at": int(now), "state": state,
                                   "spans": {str(t): v for t, v in spans.items()},
                                   "hours": {str(h): v for h, v in hours.items()}})
            return len(lines)

    async def round(self, now: float | None = None) -> None:
        found = self.instances()
        causes: dict[str, list] = {}
        for i, inst in enumerate(found):
            try:
                await self.read(inst, now)
            except Exception:  # noqa: BLE001 - one instance's log must not stop the others'
                _logger.exception("monitor: the log of %s", inst.key)
            if self.causes is not None:
                try:
                    causes[inst.key] = self.causes(inst.key, now)
                except Exception:  # noqa: BLE001
                    _logger.exception("monitor: the causes of %s", inst.key)
            if now is None and i + 1 < len(found):
                await asyncio.sleep(min(5.0, EVERY / max(len(found), 1) / 4))  # one queue, staggered
        if self.told is not None and causes:
            await self.told(causes)

    # the jobs' watcher (JobManager.watchers): before a job acts on a compose instance its log is read, so a
    # container it makes again loses nothing of what the one before wrote
    async def begin(self, job) -> None:
        for key in job.instances:
            server, _, role = key.partition("/")
            srv = self.settings.servers.get(server)
            inst = srv.instances.get(role) if srv is not None else None
            if inst is None or inst.restart.type != "compose" or not self.measured(key):
                continue
            try:
                await asyncio.wait_for(self.read(inst), READ_TIMEOUT)
            except Exception:  # noqa: BLE001 - a read must not change the job
                _logger.info("monitor: the log of %s was not read before job %s", key, job.id)

    async def end(self, job) -> None:
        return None

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _loop(self) -> None:
        await asyncio.sleep(10)
        while True:
            started = time.time()
            try:
                await self.round()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the reading must not stop
                _logger.exception("monitor: the round of the logs")
            await asyncio.sleep(max(5.0, EVERY - (time.time() - started)))
