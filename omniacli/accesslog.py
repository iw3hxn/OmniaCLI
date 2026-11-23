# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Who logged in, when and from where, and what they did: the access log
(the audit log of NIS 2, docs/nis2-audit.md).

A user's login and logout are those of the login form. A link or an MCP token
sends its token with every request, so its login is its first request after
SESSION_GAP without requests from the same address. Every call to /mcp is
logged, with its JSON-RPC method and tool, and so is every action of whoever
passed the login (app.py: what it asked and the answer), every read of
customer or security data (audit.SENSITIVE_READS), every shell, tunnel and
download of a backup, and the end of every job.

Wrong passwords and tokens need no login, so they can never push out what the
others wrote: they have a file of their own, and from one address at most
BURST_ADDRESS of them a minute are written (BURST_TOTAL from all addresses),
the rest counted in one entry at the end of that minute.

One JSON object per line in access.jsonl and access-failed.jsonl, next to the
instances' logs; past MAX_BYTES a file moves to <file>.1, the older ones one
number up. A file past the KEEP_FILES-th is dropped only when its newest entry
is older than retention_days: no entry younger than that goes because of
size (retention_days 0: past KEEP_FILES, as before).

Each line carries in "prev" the SHA-256 of the line before it in its file, or
of the last line of the older file when it starts one: a chain per stream
that verify() walks, naming the first line that does not follow.
"""

from __future__ import annotations

import contextvars
import datetime
import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Callable, Iterator

SESSION_GAP = 30 * 60  # seconds
# the same read by the same one from the same address: logged once in this
READ_GAP = 10 * 60  # seconds
MAX_BYTES = 5 * 1024 * 1024
KEEP_FILES = 10
# the period every entry is kept at least, when the options say none
RETENTION_DAYS = 365

BURST_WINDOW = 60  # seconds
BURST_ADDRESS = 10
BURST_TOTAL = 300

# the ASGI scope of the request (or websocket) being served: what an entry
# recorded while serving it takes its role, workspace and route from
REQUEST: contextvars.ContextVar[dict | None] = contextvars.ContextVar("omniacli_audit_request", default=None)
# the fields an entry may carry besides the first seven, in this order
FIELDS = ("on_behalf", "role", "workspace", "workspaces", "method", "route", "server", "env", "status",
          "job", "what", "source")
# the last line of a file is read from its last TAIL_BYTES
TAIL_BYTES = 64 * 1024
_DAY_NS = 86400 * 10**9


def line_hash(line: str) -> str:
    """The hash the next entry carries in "prev": of the line without its newline."""
    return hashlib.sha256(line.rstrip("\n").encode("utf-8")).hexdigest()


def numbered(path: Path) -> list[Path]:
    """path and its older files that exist, the newest first."""
    found = [path] if path.exists() else []
    number = 1
    while True:
        older = path.with_name(f"{path.name}.{number}")
        if not older.exists():
            return found
        found.append(older)
        number += 1


def _last_line(path: Path) -> str | None:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - TAIL_BYTES))
            tail = fh.read()
    except OSError:
        return None
    lines = [line for line in tail.split(b"\n") if line.strip()]
    return lines[-1].decode("utf-8", "replace") if lines else None


def _first_line(path: Path) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.strip():
                    return line
    except OSError:
        return None
    return None


def _ns_of(line: str | None) -> int | None:
    try:
        value = json.loads(line or "").get("ns")
    except (ValueError, AttributeError):
        return None
    return value if isinstance(value, int) else None


def _rotate(path: Path, retention_days: int, now_ns: int) -> None:
    """path becomes <path>.1, the older files one number up; past the
    KEEP_FILES-th, a file whose newest entry is older than retention_days goes."""
    files = numbered(path)
    for number in range(len(files) - 1, 0, -1):
        files[number].replace(path.with_name(f"{path.name}.{number + 1}"))
    path.replace(path.with_name(path.name + ".1"))
    cutoff = now_ns - retention_days * _DAY_NS if retention_days > 0 else None
    for number in range(len(files), KEEP_FILES, -1):
        older = path.with_name(f"{path.name}.{number}")
        newest = _ns_of(_last_line(older))
        if cutoff is not None and (newest is None or newest >= cutoff):
            # this one is still within the period, and the younger ones too
            break
        older.unlink(missing_ok=True)


class _Burst:
    """How many failures one address (or all of them) sent in this minute."""

    def __init__(self, now: float):
        self.start, self.written, self.dropped = now, 0, 0


class AccessLog:
    def __init__(self, path: Path, retention_days: int = RETENTION_DAYS):
        self.path = Path(path)
        self.failed_path = self.path.with_name(f"{self.path.stem}-failed{self.path.suffix}")
        self.retention_days = retention_days
        # enrich(scope): the fields of the request being served (app.py);
        # forward(entry): each entry written, to the SIEM (siem.py)
        self.enrich: Callable[[dict], dict] | None = None
        self.forward: Callable[[dict], None] | None = None
        # scope_of(kind, name): the workspaces an entry with none belongs to
        # (a user of workspaces' login, a workspace's token)
        self.scope_of: Callable[[str, str], list] | None = None
        self._lock = threading.Lock()
        self._seen: dict[tuple, float] = {}
        self._reads: dict[tuple, float] = {}
        # {file: (its size, the hash of its last line)} after this log's last write
        self._tails: dict[Path, tuple[int, str]] = {}
        self._bursts: dict[str, _Burst] = {}
        self._total = _Burst(0.0)

    def _previous(self, path: Path) -> str:
        """The hash the next line of path continues from: of its last line,
        or of the last of its older file; empty: the chain starts. The one
        this log wrote last, while the file has the size it left it with."""
        try:
            size = path.stat().st_size
        except OSError:
            size = None
        known = self._tails.get(path)
        if known is not None and size is not None and known[0] == size:
            return known[1]
        for candidate in (path, path.with_name(path.name + ".1")):
            last = _last_line(candidate)
            if last is not None:
                return line_hash(last)
        return ""

    def _write(self, path: Path, entry: dict) -> None:
        """One line; the caller holds the lock."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > MAX_BYTES:
                _rotate(path, self.retention_days, entry["ns"])
            entry["prev"] = self._previous(path)
            line = json.dumps(entry, ensure_ascii=False)
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640), "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            self._tails[path] = (path.stat().st_size, line_hash(line))
        except OSError:
            return  # the log must never stop a request
        if self.forward is not None:
            try:
                self.forward(entry)
            except Exception:  # noqa: BLE001 - the SIEM must never stop a request (siem.py records its failures)
                pass

    def _entry(self, event: str, kind: str, name: str, ip: str, detail: str, extra: dict, context: bool) -> dict:
        now = time.time_ns()
        stamp = datetime.datetime.fromtimestamp(now / 1e9, datetime.timezone.utc)
        entry = {
            "time": stamp.astimezone().isoformat(timespec="seconds"),
            "ns": now,
            "event": event, "kind": kind, "name": name, "ip": ip, "detail": detail,
            "utc": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        fields = {}
        scope = REQUEST.get() if context else None
        if scope is not None and self.enrich is not None:
            try:
                fields.update(self.enrich(scope))
            except Exception:  # noqa: BLE001 - what the request says is extra: the entry is written without it
                pass
        fields.update({k: v for k, v in extra.items() if v not in (None, "", [], ())})
        if not fields.get("workspace") and not fields.get("workspaces") and self.scope_of is not None:
            try:
                fields["workspaces"] = list(self.scope_of(kind, name))
            except Exception:  # noqa: BLE001 - as above
                pass
        for key in FIELDS:
            if fields.get(key) not in (None, "", [], ()):
                entry[key] = fields[key]
        return entry

    def _close_bursts(self, now: float) -> None:
        """The minutes that ended: what they did not write, counted. The
        caller holds the lock."""
        for ip, burst in list(self._bursts.items()):
            if now - burst.start >= BURST_WINDOW:
                if burst.dropped:
                    self._write(self.failed_path, self._entry(
                        "failed", "burst", "", ip, f"{burst.dropped} more failed attempts in {BURST_WINDOW} s, not listed",
                        {}, False))
                del self._bursts[ip]

    def _allowed(self, ip: str, now: float) -> bool:
        """Whether a failure from ip is written; the caller holds the lock."""
        self._close_bursts(now)
        if now - self._total.start >= BURST_WINDOW:
            self._total = _Burst(now)
        burst = self._bursts.setdefault(ip, _Burst(now))
        if burst.written >= BURST_ADDRESS or self._total.written >= BURST_TOTAL:
            burst.dropped += 1
            return False
        burst.written += 1
        self._total.written += 1
        return True

    def record(self, event: str, kind: str, name: str, ip: str = "", detail: str = "",
               context: bool = True, **extra) -> None:
        """event: login, logout, failed, mcp, shell, tunnel, action, read,
        download, login-as, job, siem, second-factor (two-step
        verification, totp.py) or oauth (oauth.py: a client registered, a
        consent given or refused, a token issued, refreshed, revoked),
        passkey (registered, removed: passkeys.py), session (one ended by
        its person or an admin) or sso (a workspace's single sign-on set,
        changed, removed, a user made by it: sso.py) or permissions (a custom
        role of a workspace made, changed, deleted, a person given a role or
        some instances only, a token made for a person: capabilities.py); kind:
        user, link, token or mcp.
        extra: the FIELDS this entry knows (they win over the request's);
        context False: none taken from the request being served (a job's end)."""
        entry = self._entry(event, kind, name, ip, detail, extra, context)
        with self._lock:
            if event != "failed":
                self._write(self.path, entry)
            elif self._allowed(ip, time.monotonic()):
                self._write(self.failed_path, entry)

    def seen(self, kind: str, name: str, ip: str = "", detail: str = "") -> None:
        """A request that passed the login: logged when it starts a session."""
        key = (kind, name, ip, detail)
        now = time.monotonic()
        with self._lock:
            last = self._seen.get(key)
            self._seen[key] = now
            if len(self._seen) > 10000:  # forget the sessions long ended
                self._seen = {k: t for k, t in self._seen.items() if now - t < SESSION_GAP}
        if last is None or now - last >= SESSION_GAP:
            self.record("login", kind, name, ip, detail)

    def first_read(self, key: tuple) -> bool:
        """Whether a read of key (who, address, path) is logged: not again
        within READ_GAP (a log followed asks for its lines every second)."""
        now = time.monotonic()
        with self._lock:
            last = self._reads.get(key)
            if last is not None and now - last < READ_GAP:
                return False
            self._reads[key] = now
            if len(self._reads) > 10000:
                self._reads = {k: t for k, t in self._reads.items() if now - t < READ_GAP}
        return True

    def files(self) -> list[Path]:
        """Every file of both streams, the newest first in each."""
        return [*numbered(self.path), *numbered(self.failed_path)]

    def _lines(self, base: Path, limit: int) -> list[str]:
        """The last limit lines of base and its older files, the oldest first."""
        found: list[str] = []
        for path in numbered(base):
            try:
                with open(path, encoding="utf-8") as fh:
                    found = fh.readlines() + found
            except OSError:
                continue
            if len(found) >= limit:
                break
        return found[-limit:]

    def recent(self, limit: int = 1000) -> list[dict]:
        """The last entries, the newest first."""
        with self._lock:
            self._close_bursts(time.monotonic())
        entries = []
        for base in (self.path, self.failed_path):
            for line in self._lines(base, limit):
                try:
                    entries.append(json.loads(line))
                except ValueError:
                    continue
        # entries older than "ns" keep the order of their file
        entries.sort(key=lambda e: e.get("ns", 0))
        return entries[-limit:][::-1]

    def period(self, since_ns: int = 0, until_ns: int | None = None) -> list[dict]:
        """The entries of both streams from since_ns to until_ns, the newest
        first: the older files are read only as far back as since_ns."""
        with self._lock:
            self._close_bursts(time.monotonic())
        entries = []
        for base in (self.path, self.failed_path):
            for path in numbered(base):
                newest = _ns_of(_last_line(path))
                if newest is not None and newest < since_ns:
                    break
                for entry in _read(path):
                    ns = entry.get("ns", 0)
                    if ns >= since_ns and (until_ns is None or ns < until_ns):
                        entries.append(entry)
        entries.sort(key=lambda e: e.get("ns", 0), reverse=True)
        return entries

    def span(self) -> dict:
        """The size of every file, and the first and the last entry of the
        main stream (ns, None when there is none)."""
        files = numbered(self.path)
        size = 0
        for path in self.files():
            try:
                size += path.stat().st_size
            except OSError:
                continue
        return {"bytes": size, "files": len(self.files()),
                "first": _ns_of(_first_line(files[-1])) if files else None,
                "last": _ns_of(_last_line(files[0])) if files else None}

    def verify(self) -> dict:
        """Walk the chain of both streams, the oldest line first: {"ok",
        "entries", "breaks": [{"file", "line", "reason"}]} (at most 50)."""
        breaks: list[dict] = []
        total = 0
        for base in (self.path, self.failed_path):
            previous: str | None = None  # the hash of the line before; None: none read yet
            chained = False
            for path in reversed(numbered(base)):
                try:
                    with open(path, encoding="utf-8", errors="replace") as fh:
                        lines = fh.readlines()
                except OSError as exc:
                    breaks.append({"file": str(path), "line": 0, "reason": f"cannot be read: {exc}"})
                    previous = None
                    continue
                for number, line in enumerate(lines, 1):
                    if not line.strip():
                        continue
                    total += 1
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        entry = None
                    if not isinstance(entry, dict):
                        breaks.append({"file": str(path), "line": number, "reason": "not an entry (JSON) any more"})
                    elif "prev" not in entry:
                        if chained:
                            breaks.append({"file": str(path), "line": number,
                                           "reason": "an entry without the chain among chained ones: added or changed"})
                    else:
                        chained = True
                        if previous is not None and entry["prev"] != previous:
                            where = f"line {number - 1}" if number > 1 else "the last line of the older file"
                            breaks.append({"file": str(path), "line": number,
                                           "reason": f"it does not follow {where}: that one was changed, or lines "
                                                     "were removed or added between them"})
                    previous = line_hash(line)
                    if len(breaks) >= 50:
                        return {"ok": False, "entries": total, "breaks": breaks}
        return {"ok": not breaks, "entries": total, "breaks": breaks}


def _read(path: Path) -> Iterator[dict]:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict):
                    yield entry
    except OSError:
        return
