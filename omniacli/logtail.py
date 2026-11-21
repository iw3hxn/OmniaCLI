# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read a log file in steps, like tail -F, for a page that polls it; read
the part of it between two times; group Odoo's lines into its records.

The page asks again every few seconds with the position it reached: no
connection stays open (an open stream per log filled the browser's six
connections per server and blocked the other pages).
"""

from __future__ import annotations

import datetime
import os
import re
from pathlib import Path

_BLOCK = 64 * 1024
MAX_READ = 512 * 1024  # bytes returned by one call; the page asks again for more


def last_lines(path: Path, count: int) -> tuple[list[str], int, int]:
    """(last complete lines, position after them, inode) of the file.

    A last line still being written is left for the next read."""
    with open(path, "rb") as fh:
        stat = os.fstat(fh.fileno())
        end = stat.st_size
        data = b""
        pos = end
        while pos > 0 and data.count(b"\n") <= count:
            step = min(_BLOCK, pos)
            pos -= step
            fh.seek(pos)
            data = fh.read(step) + data
    cut = data.rfind(b"\n") + 1
    complete = data[:cut]
    lines = complete.decode(errors="replace").splitlines()[-count:]
    return lines, end - (len(data) - cut), stat.st_ino


def read_since(path: Path, pos: int | None = None, inode: int | None = None,
               initial: int = 300) -> dict:
    """The lines written after `pos`, or the last `initial` lines when pos is None.

    Returns {lines, pos, inode, rotated, more}: pass pos and inode back on the
    next call. rotated: the file was replaced or truncated and is read from
    its start. more: there is more to read right away.
    """
    if pos is None:
        lines, pos, inode = last_lines(path, initial)
        return {"lines": lines, "pos": pos, "inode": inode, "rotated": False, "more": False}
    with open(path, "rb") as fh:
        stat = os.fstat(fh.fileno())
        rotated = stat.st_ino != inode or stat.st_size < pos
        if rotated:
            pos = 0
        fh.seek(pos)
        data = fh.read(min(stat.st_size - pos, MAX_READ))
    cut = data.rfind(b"\n") + 1
    if cut == 0 and len(data) == MAX_READ:
        cut = len(data)  # a single line longer than MAX_READ: give it in pieces
    lines = data[:cut].decode(errors="replace").splitlines()
    pos += cut
    return {
        "lines": lines,
        "pos": pos,
        "inode": stat.st_ino,
        "rotated": rotated,
        "more": pos < stat.st_size and cut > 0,
    }


# ---- Odoo's log records -------------------------------------------------
#
# A record of Odoo's log begins with a head (time, pid, level, database,
# logger: "2026-11-21 17:48:11,585 1 ERROR db odoo.modules.loading: ...");
# the lines after it without a head belong to it (a traceback, a message of
# several lines). The page's filters act on records, never on lines cut
# loose from their head.

HEAD = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ \d+ ([A-Z][A-Z_]*) ")
_HEAD_TIME = re.compile(rb"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ \d+ [A-Z]")
# a terminal's colours (Odoo colours its levels when it writes on one)
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# what Python itself writes on stderr, outside Odoo's records: an exception
# a thread did not catch, one a finalizer ignored
_STRAY = re.compile(r"Exception in thread |Exception ignored in")
_TRACEBACK = "Traceback (most recent call last):"
# the lines between the tracebacks of one chained exception
_CHAINED = ("During handling of the above exception", "The above exception was the direct cause")


def _stray(line: str, current: dict | None) -> bool:
    """Whether line starts what Python wrote on stderr itself, not a line of
    the record above it: "Exception in thread ...", or a traceback that is
    neither right under the record's head (logger.exception) nor the next
    one of a chained exception."""
    if _STRAY.match(line):
        return True
    if not line.startswith(_TRACEBACK) or current is None or len(current["lines"]) < 2:
        return False
    before = next((text for text in reversed(current["lines"]) if text.strip()), "")
    return not before.startswith(_CHAINED) and not current.get("stray")


def records(lines: list[str]) -> list[dict]:
    """The lines grouped into records {"level", "lines"}, the first line the
    head. The lines before the first head (the rest of a record that began
    before what was read) are a record of level None: the page joins it to
    the record it already has. An exception Python writes on stderr itself
    is a record of its own, level ERROR, "stray"."""
    found: list[dict] = []
    current: dict | None = None
    for raw in lines:
        line = _ANSI.sub("", raw)
        head = HEAD.match(line)
        if head:
            current = {"level": head.group(2), "lines": [line]}
        elif _stray(line, current):
            current = {"level": "ERROR", "lines": [line], "stray": True}
        elif current is None:
            current = {"level": None, "lines": [line]}
        else:
            current["lines"].append(line)
            continue
        found.append(current)
    return found


def counts(found: list[dict]) -> dict[str, int]:
    """How many records of each level."""
    total: dict[str, int] = {}
    for record in found:
        if record["level"]:
            total[record["level"]] = total.get(record["level"], 0) + 1
    return total


def head_time(line: str) -> str | None:
    """The time of a record's head as Odoo wrote it ("2026-11-21 17:48:11")."""
    head = HEAD.match(_ANSI.sub("", line))
    return head.group(1) if head else None


# ---- a span of time -----------------------------------------------------

SPAN_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?Z")
MAX_SPAN = datetime.timedelta(days=31)
MAX_LINES = 10000  # lines of a span given to the page: the first or the last of it


def parse_span(since: str, until: str = "", now: datetime.datetime | None = None
               ) -> tuple[datetime.datetime, datetime.datetime]:
    """The span the page asks for, checked: UTC times as
    2026-11-21T00:00:00Z, since before until (until missing: now), since
    not in the future, at most MAX_SPAN long. ValueError says what is wrong."""
    now = now or datetime.datetime.now(datetime.timezone.utc)

    def moment(text: str, name: str) -> datetime.datetime:
        if not SPAN_TIME.fullmatch(text):
            raise ValueError(f"{name} takes a UTC time, as 2026-11-21T00:00:00Z")
        try:
            value = datetime.datetime.strptime(text[:19].rstrip("Z"), "%Y-%m-%dT%H:%M:%S" if len(text) > 17
                                               else "%Y-%m-%dT%H:%M")
        except ValueError:
            raise ValueError(f"{name}: {text} is not a date") from None
        return value.replace(tzinfo=datetime.timezone.utc)

    start = moment(since, "since")
    end = moment(until, "until") if until else now
    if start >= end:
        raise ValueError("since must come before until")
    if start > now:
        raise ValueError("since is in the future")
    if end - start > MAX_SPAN:
        raise ValueError(f"a span is at most {MAX_SPAN.days} days")
    return start, min(end, now + datetime.timedelta(minutes=1))


def iso_utc(moment: datetime.datetime) -> str:
    return moment.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_to_iso(text: str | None) -> str | None:
    """A time Odoo wrote in a log file (the machine's time zone) in UTC."""
    if not text:
        return None
    return iso_utc(datetime.datetime.strptime(text, "%Y-%m-%d %H:%M:%S").astimezone())


def _head_of(line: bytes) -> bytes | None:
    found = _HEAD_TIME.match(line)
    return found.group(1) if found else None


def _offset(fh, size: int, key: bytes) -> int:
    """Where the first record written at key or later begins (size: none),
    found by halving: Odoo writes its records in the order of their time."""
    lo, hi = 0, size
    while hi - lo > _BLOCK:
        mid = (lo + hi) // 2
        fh.seek(mid)
        fh.readline()
        when = None
        while when is None and fh.tell() < hi:
            line = fh.readline()
            if not line:
                break
            when = _head_of(line)
        if when is not None and when < key:
            lo = mid
        else:
            hi = mid
    fh.seek(lo)
    if lo:
        fh.readline()
    pos = fh.tell()
    for line in iter(fh.readline, b""):
        when = _head_of(line)
        if when is not None and when >= key:
            return pos
        pos += len(line)
    return size


def _count_lines(fh, start: int, end: int) -> int:
    fh.seek(start)
    total, pos = 0, start
    while pos < end:
        data = fh.read(min(_BLOCK * 16, end - pos))
        if not data:
            break
        total += data.count(b"\n")
        pos += len(data)
    return total


def read_span(path: Path, since: datetime.datetime, until: datetime.datetime,
              keep: str = "last", cap: int = MAX_LINES) -> dict:
    """The records of a log file written from since to until (Odoo writes
    the machine's time): {lines, total, truncated, timed}. Past cap lines,
    the first or the last cap of the span (keep). timed: the file has
    Odoo's times at all (else no span can be read in it)."""
    start_key = since.astimezone().strftime("%Y-%m-%d %H:%M:%S").encode()
    end_key = until.astimezone().strftime("%Y-%m-%d %H:%M:%S").encode()
    with open(path, "rb") as fh:
        size = os.fstat(fh.fileno()).st_size
        start = _offset(fh, size, start_key)
        end = _offset(fh, size, end_key)
        timed = True
        if start >= end:
            fh.seek(max(0, size - _BLOCK))
            timed = any(_head_of(line) for line in fh.read().splitlines())
            return {"lines": [], "total": 0, "truncated": False, "timed": timed}
        total = _count_lines(fh, start, end)
        if keep == "first":
            fh.seek(start)
            data = fh.read(end - start) if total <= cap else b""
            if total > cap:
                kept = []
                for line in iter(fh.readline, b""):
                    kept.append(line)
                    if len(kept) >= cap or fh.tell() >= end:
                        break
                data = b"".join(kept)
        else:
            data, pos = b"", end
            while pos > start and data.count(b"\n") <= cap:
                step = min(_BLOCK, pos - start)
                pos -= step
                fh.seek(pos)
                data = fh.read(step) + data
    lines = data.decode(errors="replace").splitlines()
    lines = lines[:cap] if keep == "first" else lines[-cap:]
    return {"lines": lines, "total": total, "truncated": total > cap, "timed": timed}
