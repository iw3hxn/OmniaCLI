# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the monitor of an instance counts in Odoo's log (UX-018, AD-019
slice 2): its requests with their times, and its errors, warnings, workers
stopped for memory, database out of memory, conflicts retried, API calls and
mail; only numbers, never a line, an address, a query string or a message.

An access line is the record of logger werkzeug (Odoo 16-19) or
odoo.http.server (Odoo 20), its message as werkzeug writes it, and Odoo's
perf_info after it: the queries, the time in SQL and the rest, and from 18 a
fourth field (the cursor's mode, "-"); 19 and 20 may write "#model.method"
after the path of an RPC (slice 0):

    10.213.4.1 - - [26/Nov/2026 15:14:15] "POST /web/dataset/call_kw HTTP/1.1" 200 - 2 0.002 0.010

A request is counted at its end (its line is written then); its time is the
SQL time plus the rest, in one of the 12 classes of EDGES, so the median and
«1 in 20» of any period come from the classes summed, never from a mean of
medians. A request without times (log level above info for werkzeug, or
"- - -") is counted, not timed. The long ones that are not pages (the
websocket, the longpolling of 16) are not counted: they last as long as the
browser stays.

The times of Odoo's records are UTC: Odoo sets TZ=UTC for its process.
"""

from __future__ import annotations

import calendar
import re
import time

# the bounds of the 12 classes of a response's time, seconds; 1 s and 3 s are bounds: «slow» reads them exactly
EDGES = (0.1, 0.2, 0.3, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0)
CLASSES = len(EDGES) + 1
TOP = 20.0  # the upper bound the last class is read up to
# the counts of a span: requests, failed (5xx), errors, warnings, workers stopped for memory, the database out of
# memory, conflicts retried, API calls, mail sent and bounced, lines read
COUNTS = ("rq", "rf", "e", "w", "k", "dbo", "s", "a", "ms", "mb", "ln", "tm")
PAGES_KEPT = 50  # the worst pages of an hour kept (AD-019 19.5)
PATH_MOST = 120  # characters of a page's path kept

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_HEAD = re.compile(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ \d+ ([A-Z]+) (\S+) (\S+): ?(.*)")
ACCESS_LOGGERS = ("werkzeug", "odoo.http.server")
_ACCESS = re.compile(
    r'\S+ \S+ \S+ \[[^\]]*\] "(\S+) (\S+)(?: [^"]*)?" (\d{3}|-) \S+'
    r'(?: (\d+|-) (\d+(?:\.\d+)?|-) (\d+(?:\.\d+)?|-)(?: \S+)?)?\s*\Z')
# Odoo 19 writes a WARNING on every XML-RPC and JSON-RPC call (slice 0): counted among the API calls, not the warnings
DEPRECATION_LOGGERS = ("odoo.addons.rpc.controllers.xmlrpc", "odoo.addons.rpc.controllers.jsonrpc")
API_PATHS = ("/xmlrpc/", "/jsonrpc", "/json/2/")
NOT_PAGES = ("/websocket", "/longpolling/")
_KILLED = re.compile(r"virtual memory limit|server memory limit", re.I)
# what Odoo logs when its PostgreSQL ran out of memory, or was killed for it and restarts
_DB_MEMORY = re.compile(r"out of memory|terminating connection because of crash of another server process"
                        r"|the database system is in recovery mode", re.I)
_TRIES = re.compile(r"\btries left\b")
_SENT = re.compile(r"Mail \(mail\.mail\) .* successfully sent")
_BOUNCE = re.compile(r"\ARouting mail .*\bbounce", re.I)
# a page's path: the parts that are a record's number or a token become <n> and <x>
_NUMBER = re.compile(r"\d+")
_TOKEN = re.compile(r"(?=[^/]*\d)[A-Za-z0-9_-]{16,}|[0-9a-fA-F]{8,}(?:-[0-9a-fA-F]{4,}){0,4}")
_UNSAFE = re.compile(r"[^A-Za-z0-9/._~+-]")


def time_class(seconds: float) -> int:
    for i, edge in enumerate(EDGES):
        if seconds <= edge:
            return i
    return len(EDGES)


def over(classes: list[int], edge: float) -> int:
    """The requests above a bound of EDGES."""
    return sum(classes[EDGES.index(edge) + 1:])


def quantile(classes: list[int], share: float) -> float | None:
    """The time under which share of the timed requests are, read in their
    classes (linear inside the class); None with none."""
    total = sum(classes or ())
    if not total:
        return None
    wanted, seen = share * total, 0
    for i, count in enumerate(classes):
        # strictly past: with 5 of 100 over 5 s, «1 in 20 over» is 5 s, not the bound under them
        if count and seen + count > wanted:
            low = EDGES[i - 1] if i else 0.0
            high = EDGES[i] if i < len(EDGES) else TOP
            return round(low + (high - low) * (wanted - seen) / count, 2)
        seen += count
    return TOP


def add_classes(into: list[int], other) -> list[int]:
    if not isinstance(other, list) or len(other) != CLASSES:
        return into
    return [a + (b if isinstance(b, int) else 0) for a, b in zip(into, other)]


def normalised(target: str) -> str:
    """A page's path as the monitor keeps it: no query string, no #model.method, a number <n>, a token <x>, at
    most PATH_MOST characters of a safe alphabet (a path is the customer's, not a place to carry text)."""
    path = target.split("?", 1)[0].split("#", 1)[0]
    if "://" in path:
        path = "/" + path.split("://", 1)[1].partition("/")[2]
    parts = []
    for part in path.split("/"):
        part = _UNSAFE.sub("_", part)
        part = _TOKEN.sub("<x>", part)
        parts.append(_NUMBER.sub("<n>", part))
    found = "/".join(parts) or "/"
    return found[:PATH_MOST]


def empty_tally() -> dict:
    return {**{name: 0 for name in COUNTS}, "h": [0] * CLASSES, "kt": [], "pages": {}}


def moment(text: str) -> int | None:
    try:
        return calendar.timegm(time.strptime(text, "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return None


class Counter:
    """The counts of Odoo's records by span of `span` seconds: feed it lines,
    read the spans it touched."""

    def __init__(self, span: int = 300):
        self.span = span
        self.spans: dict[int, dict] = {}
        # {hour: {path: [requests, total ms, most ms]}}
        self.hours: dict[int, dict] = {}

    def tally(self, start: int) -> dict:
        return self.spans.setdefault(start, empty_tally())

    def line(self, raw: str) -> None:
        head = _HEAD.match(_ANSI.sub("", raw))
        if not head:
            return  # a line of a record (a traceback) or Docker's: its head counted it
        when = moment(head.group(1))
        if when is None:
            return
        level, logger, message = head.group(2), head.group(4), head.group(5)
        tally = self.tally(when // self.span * self.span)
        tally["ln"] += 1
        if logger in ACCESS_LOGGERS:
            self.access(tally, when, message)
        if level in ("ERROR", "CRITICAL"):
            tally["e"] += 1
        elif level == "WARNING" and logger not in DEPRECATION_LOGGERS:
            tally["w"] += 1
        if _KILLED.search(message):
            tally["k"] += 1
            if len(tally["kt"]) < 10:
                tally["kt"].append(when)
        elif _DB_MEMORY.search(message):
            tally["dbo"] += 1
        if _TRIES.search(message):
            tally["s"] += 1
        if logger.startswith("odoo.addons.mail"):
            if _SENT.search(message):
                tally["ms"] += 1
            elif _BOUNCE.search(message):
                tally["mb"] += 1

    def access(self, tally: dict, when: int, message: str) -> None:
        found = _ACCESS.match(message)
        if not found:
            return
        target, status = found.group(2), found.group(3)
        if target.startswith(NOT_PAGES) or status == "101":
            return
        tally["rq"] += 1
        if status.isdigit() and status.startswith("5"):
            tally["rf"] += 1
        if target.startswith(API_PATHS):
            tally["a"] += 1
        sql, rest = found.group(5), found.group(6)
        if sql in (None, "-") or rest in (None, "-"):
            return
        seconds = float(sql) + float(rest)
        tally["tm"] += 1
        tally["h"][time_class(seconds)] += 1
        ms = int(seconds * 1000)
        pages = self.hours.setdefault(when // 3600 * 3600, {})
        page = pages.setdefault(normalised(target), [0, 0, 0])
        page[0] += 1
        page[1] += ms
        page[2] = max(page[2], ms)


def merge(into: dict, other: dict) -> dict:
    """Two tallies of the same span summed (what was read before and now)."""
    found = {name: int(into.get(name) or 0) + int(other.get(name) or 0) for name in COUNTS}
    found["h"] = add_classes(add_classes([0] * CLASSES, into.get("h")), other.get("h"))
    found["kt"] = (list(into.get("kt") or []) + list(other.get("kt") or []))[:10]
    return found


def merge_pages(into: dict, other: dict) -> dict:
    found = {path: list(v) for path, v in (into or {}).items()}
    for path, (count, total, most) in (other or {}).items():
        page = found.setdefault(path, [0, 0, 0])
        page[0] += count
        page[1] += total
        page[2] = max(page[2], most)
    return found


def worst(pages: dict, most: int = PAGES_KEPT) -> dict:
    """The pages kept of an hour: the slowest on average, then the most asked."""
    ranked = sorted(pages.items(), key=lambda kv: (-(kv[1][1] / kv[1][0] if kv[1][0] else 0), -kv[1][0], kv[0]))
    return dict(ranked[:most])
