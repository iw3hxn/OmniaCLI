# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The monitor of an instance (UX-018, AD-019 slice 1): its load over 14
days, and the verdict on now with its causes.

The sampler (metrics.py) keeps the last hour of each instance, a point every
5 s, with its Odoo and its database apart and the connections on its HTTP
port. Every 5 minutes the points of the 5 minutes just ended become one point
here, with the mean and the maximum of each measure, and the monitor's checks
of those minutes (monitor.py: answered, asked). A span with no sample and no
check (OmniaCLI was not running) has no point: a gap stays a gap, never a
zero.

The points are kept in monitor/<server>/<role>/ in the machine's state
(folders 700, files 600), one file a day (UTC) of JSON lines, only added to:
for the same time the last line wins, so a span written twice counts once.
Days older than KEEP_DAYS go, and past CAP_BYTES an instance's oldest days go
too (the page says «Misurato dal»): ≈ 0.4 MB an instance for 14 days.

view() makes what the page, the REST API and MCP answer: the verdict on the
last minutes (it does not follow the period), four tiles and the load over
the period chosen (1h: the sampler's own points, after the history's; 24h,
7d, 14d: the history's), at most MAX_POINTS a series. For a workspace's
people (customer) the machine's PostgreSQL shared by every database is not
there: no series, no host, no name of a machine or a cluster.

The requests, their times and the log's counts come from Odoo's log in
slice 2: until then their tiles are None with the reason, never 0.
"""

from __future__ import annotations

import asyncio
import configparser
import json
import logging
import os
import re
import threading
import time
from pathlib import Path

from .metrics import INTERVAL

_logger = logging.getLogger(__name__)

FOLDER = "monitor"
BUCKET = 300  # seconds a point covers
KEEP_DAYS = 14
KEEP_SECONDS = KEEP_DAYS * 86400
CAP_BYTES = 5 * 1024 * 1024  # an instance's history at most (AD-019 19.5)
CHECK_EVERY = 30  # seconds between two looks at the sampler
SETTLE = INTERVAL + 2  # a span is written once its last sample is surely in
PERIODS = {"1h": 3600, "24h": 86400, "7d": 7 * 86400, "14d": 14 * 86400}
DEFAULT_PERIOD = "24h"
MAX_POINTS = 300  # a series drawn at most (UX-018 §7)
NOW_SECONDS = 15 * 60  # the verdict's «now»
COVERED = 0.8  # the share of the window the points must cover before a level is read from them
# the database's own thresholds (UX-006 §4.4, D4): the Database section says the same
DB_CONNECTIONS = 0.8
DB_CACHE = 95
# a source not measured yet: what its tile says instead of a number
FROM_LOG = "from_log"

_SEGMENT = re.compile(r"[A-Za-z0-9._-]+")
_WRITING = threading.Lock()


def _mean(values: list) -> float | None:
    return sum(values) / len(values) if values else None


def _round(value, digits: int = 1):
    return round(value, digits) if value is not None else None


def bucket_point(start: int, samples: list[dict], machine: list[dict], checks: list) -> dict | None:
    """The 5-minute point of the span starting at start: the sampler's
    instance points in it (ocpu, omem, dcpu, dmem, conns), the machine's
    (pg: percent of the whole machine, pg_mem) and the monitor's checks
    ([t, answered, ms]); None: nothing in the span."""
    if not samples and not checks:
        return None
    point = {"t": start, "n": len(samples)}

    def both(name: str, key: str, source: list[dict]) -> None:
        values = [p[key] for p in source if p.get(key) is not None]
        if values:
            point[name] = _round(_mean(values))
            point[name + "x"] = max(values)

    both("oc", "ocpu", samples)
    both("om", "omem", samples)
    both("dc", "dcpu", samples)
    both("dm", "dmem", samples)
    both("pc", "pg", machine)
    both("pm", "pg_mem", machine)
    conns = [p["conns"] for p in samples if p.get("conns") is not None]
    if conns:
        point["w"] = max(conns)
    if checks:
        point["ck"] = len(checks)
        point["up"] = sum(1 for c in checks if c[1])
    return point


def _ends_with_newline(path: Path) -> bool:
    with open(path, "rb") as fh:
        fh.seek(-1, os.SEEK_END)
        return fh.read(1) == b"\n"


class InstanceHistory:
    """The 5-minute points of each instance, in append-only daily files."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._last: dict[str, int | None] = {}

    def folder(self, key: str) -> Path:
        server, _, role = key.partition("/")
        if not _SEGMENT.fullmatch(server) or not _SEGMENT.fullmatch(role) or server in (".", ".."):
            raise ValueError(f"not an instance: {key!r}")
        return self.root / server / role

    def _files(self, key: str) -> list[Path]:
        folder = self.folder(key)
        try:
            return sorted(p for p in folder.iterdir() if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.jsonl", p.name))
        except OSError:
            return []

    @staticmethod
    def _read(path: Path, since: float, found: dict[int, dict]) -> None:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return
        for line in text.splitlines():
            try:
                point = json.loads(line)
            except ValueError:
                continue  # a line cut by a stop: the next one is whole
            if isinstance(point, dict) and isinstance(point.get("t"), int) and point["t"] >= since:
                found[point["t"]] = point

    def points(self, key: str, since: float = 0) -> list[dict]:
        """The points after since, oldest first; for the same time the last line wins."""
        day = time.strftime("%Y-%m-%d", time.gmtime(max(since, 0)))
        found: dict[int, dict] = {}
        for path in self._files(key):
            if path.stem >= day:
                self._read(path, since, found)
        return [found[t] for t in sorted(found)]

    def last(self, key: str) -> int | None:
        """The start of the last span written."""
        if key not in self._last:
            found: dict[int, dict] = {}
            files = self._files(key)
            if files:
                self._read(files[-1], 0, found)
            self._last[key] = max(found) if found else None
        return self._last[key]

    def started(self, key: str) -> int | None:
        """The first point kept."""
        found: dict[int, dict] = {}
        files = self._files(key)
        if files:
            self._read(files[0], 0, found)
        return min(found) if found else None

    def add(self, key: str, points: list[dict], now: float | None = None) -> None:
        """New points written at the end of their day's file; then the old
        days and the cap."""
        if not points:
            return
        folder = self.folder(key)
        with _WRITING:
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            for parent in (folder, folder.parent, self.root):
                try:
                    os.chmod(parent, 0o700)
                except OSError:
                    pass
            by_day: dict[str, list[dict]] = {}
            for p in sorted(points, key=lambda p: p["t"]):
                by_day.setdefault(time.strftime("%Y-%m-%d", time.gmtime(p["t"])), []).append(p)
            for day, found in by_day.items():
                path = folder / f"{day}.jsonl"
                # a line a stop cut short ends here: the new ones start on a line of their own
                cut = path.exists() and path.stat().st_size > 0 and not _ends_with_newline(path)
                fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as out:
                    out.write(("\n" if cut else "") + "".join(json.dumps(p, separators=(",", ":")) + "\n" for p in found))
            self._last[key] = max(self.last(key) or 0, *(p["t"] for p in points))
            self.prune(key, now)

    def prune(self, key: str, now: float | None = None) -> None:
        """The days older than KEEP_DAYS go, and the oldest past CAP_BYTES
        (today's never)."""
        now = now if now is not None else time.time()
        oldest = time.strftime("%Y-%m-%d", time.gmtime(now - KEEP_SECONDS))
        files = self._files(key)
        for path in [p for p in files if p.stem < oldest]:
            path.unlink(missing_ok=True)
        files = self._files(key)
        sizes = {p: p.stat().st_size for p in files if p.exists()}
        total = sum(sizes.values())
        for path in files[:-1]:
            if total <= CAP_BYTES:
                break
            total -= sizes.get(path, 0)
            path.unlink(missing_ok=True)

    def prune_all(self, now: float | None = None) -> None:
        try:
            servers = [p for p in self.root.iterdir() if p.is_dir()]
        except OSError:
            return
        for server in servers:
            for role in [p for p in server.iterdir() if p.is_dir()]:
                try:
                    self.prune(f"{server.name}/{role.name}", now)
                except ValueError:
                    continue


class InstanceRecorder:
    """Turns the sampler's points of each instance into a 5-minute point
    once each span has ended, while OmniaCLI runs."""

    def __init__(self, sampler, history: InstanceHistory, checks=None, measured=None):
        # checks(key): the monitor's checks of an instance ([t, answered, ms]); measured(key): whether it is
        # measured (productions and the stagings that run, AD-019 §10.4)
        self.sampler, self.history = sampler, history
        self.checks = checks or (lambda key: [])
        self.measured = measured or (lambda key: True)
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    def check(self, now: float | None = None) -> int:
        """Write every span of every instance ended since its last one; the
        number of points written."""
        now = now if now is not None else time.time()
        end = int((now - SETTLE) // BUCKET) * BUCKET
        machine = list(self.sampler.machine)
        written = 0
        for key in list(self.sampler.history):
            if not self.measured(key):
                continue
            samples = list(self.sampler.history.get(key, ()))
            checks = [c for c in self.checks(key) or () if isinstance(c, list) and len(c) >= 2]
            if not samples and not checks:
                continue
            firsts = [p["t"] for p in samples[:1]] + [c[0] for c in checks[:1]]
            first = min(firsts) // BUCKET * BUCKET
            last = self.history.last(key)
            if last is not None:
                first = max(first, last + BUCKET)
            spans: dict[int, tuple[list, list, list]] = {}
            for p in samples:
                start = p["t"] // BUCKET * BUCKET
                if first <= start < end:
                    spans.setdefault(start, ([], [], []))[0].append(p)
            for c in checks:
                start = int(c[0]) // BUCKET * BUCKET
                if first <= start < end:
                    spans.setdefault(start, ([], [], []))[2].append(c)
            for p in machine:
                start = p["t"] // BUCKET * BUCKET
                if start in spans:
                    spans[start][1].append(p)
            points = [bucket_point(s, *spans[s]) for s in sorted(spans)]
            points = [p for p in points if p is not None]
            if points:
                self.history.add(key, points, now)
                written += len(points)
        return written

    async def _loop(self) -> None:
        pruned = 0.0
        while True:
            try:
                await asyncio.to_thread(self.check)
                if time.time() - pruned > 3600:
                    pruned = time.time()
                    await asyncio.to_thread(self.history.prune_all)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the recording must not stop
                _logger.exception("Monitor history not recorded")
            await asyncio.sleep(CHECK_EVERY)


def database_causes(report: dict) -> list[dict]:
    """The causes of the database's verdict (UX-006 §4.4, D4) of a
    pgreport answer: queries running for long, the connections near the
    maximum, the cache under 95%."""
    found = []
    slow = report.get("long_queries") or []
    if slow:
        found.append({"kind": "database", "what": "slow", "count": len(slow),
                      "seconds": max((q.get("seconds") or 0) for q in slow)})
    connections, most = report.get("connections"), report.get("max_connections")
    if most and connections is not None and connections >= DB_CONNECTIONS * most:
        found.append({"kind": "database", "what": "connections", "count": connections, "max": most})
    cache = report.get("cache_hit")
    if cache is not None and cache < DB_CACHE:
        found.append({"kind": "database", "what": "cache", "value": cache})
    return found


def _window(points: list[dict], value, seconds: int, now: float) -> list[float] | None:
    """The values of the points of the last seconds; None when they do not cover enough of them."""
    since = now - seconds
    found = [(p["t"], value(p)) for p in points if p.get("t", 0) >= since]
    found = [(t, v) for t, v in found if v is not None]
    if len(found) < 2 or now - found[0][0] < seconds * COVERED:
        return None
    return [v for _, v in found]


def _sum(*values):
    found = [v for v in values if v is not None]
    return sum(found) if found else None


def verdict(state: dict, live: list[dict], *, cores: float, memory_limit: float | None, workers: int | None,
            thresholds: dict, minutes: int, database: list[dict], workspace: list[dict] | None = None,
            has_history: bool = False, now: float | None = None) -> dict:
    """The verdict on now: stopped, down, none (not measured yet), look
    (with its causes) or good. state: the monitor's view of the instance
    (state, since); live: the sampler's points of its last hour; cores and
    memory_limit (MiB, None: none): what it is granted; workspace: the
    points of its workspace's slice (its memory against the same limit)."""
    now = now if now is not None else time.time()
    said = state.get("state")
    if said == "stopped":
        return {"state": "stopped", "since": state.get("since"), "causes": []}
    if said == "down":
        return {"state": "down", "since": state.get("since"), "causes": []}
    if not live and not has_history and said in (None, "unknown"):
        return {"state": "none", "since": None, "causes": []}
    seconds = minutes * 60
    causes = []
    cpu = _window(live, lambda p: _sum(p.get("ocpu"), p.get("dcpu")) if p.get("ocpu") is not None else p.get("cpu"),
                  seconds, now)
    warn = thresholds["cpu"][0]
    if cpu and cores and min(cpu) / cores >= warn:
        causes.append({"kind": "cpu", "value": round(_mean(cpu) / cores), "cores": cores, "threshold": warn,
                       "minutes": minutes, "section": "load"})
    warn = thresholds["ram"][0]
    if memory_limit:
        memory = _window(live, lambda p: _sum(p.get("omem"), p.get("dmem")) if p.get("omem") is not None
                         else p.get("mem"), seconds, now)
        shared = _window(workspace or [], lambda p: p.get("mem"), seconds, now)
        if memory and min(memory) * 100 / memory_limit >= warn:
            causes.append({"kind": "memory", "value": round(_mean(memory) * 100 / memory_limit), "limit": memory_limit,
                           "used": round(_mean(memory)), "threshold": warn, "minutes": minutes, "section": "load"})
        elif shared and min(shared) * 100 / memory_limit >= warn:
            causes.append({"kind": "memory", "workspace": True, "value": round(_mean(shared) * 100 / memory_limit),
                           "limit": memory_limit, "used": round(_mean(shared)), "threshold": warn,
                           "minutes": minutes, "section": "load"})
    if workers:
        busy = _window(live, lambda p: p.get("conns"), seconds, now)
        if busy and min(busy) >= workers:
            causes.append({"kind": "workers", "workers": workers, "minutes": minutes, "section": "load"})
    causes += [{**c, "section": "database"} for c in database]
    return {"state": "look" if causes else "good", "since": None, "causes": causes}


def _shrink(points: list[dict], width: int, keys_mean: tuple, keys_max: tuple) -> list[dict]:
    """Points grouped by spans of width seconds: the mean of keys_mean and
    the maximum of keys_max."""
    if width <= 0:
        return points
    groups: dict[int, list[dict]] = {}
    for p in points:
        groups.setdefault(p["t"] // width * width, []).append(p)
    found = []
    for start in sorted(groups):
        group = groups[start]
        point = {"t": start}
        for key in keys_mean:
            values = [p[key] for p in group if p.get(key) is not None]
            point[key] = _round(_mean(values)) if values else None
        for key in keys_max:
            # a live point has no maximum of its own: its value is
            base = key[:-len("_max")] if key.endswith("_max") else key
            values = [p[key] if p.get(key) is not None else p.get(base) for p in group]
            values = [v for v in values if v is not None]
            point[key] = max(values) if values else None
        found.append(point)
    return found


MEANS = ("odoo_cpu", "db_cpu", "cluster_cpu", "odoo_mem", "db_mem", "cluster_mem")
MAXES = ("odoo_cpu_max", "db_cpu_max", "odoo_mem_max", "db_mem_max", "conns")


def series(history: list[dict], live: list[dict], machine: list[dict], period: str, cores: float,
           machine_cores: int, now: float) -> tuple[list[dict], int]:
    """The load's points over the period ([{t, odoo_cpu, db_cpu,
    cluster_cpu: percent of the cores granted, odoo_mem, db_mem,
    cluster_mem: MiB, the maximums, conns}]) and the seconds between two
    points: 1h is the sampler's own points after the history's, the others
    the history's; at most MAX_POINTS."""
    seconds = PERIODS[period]
    since = now - seconds
    cores = cores or 1

    def granted(value):
        """Percent of one core as percent of the cores granted."""
        return _round(value / cores) if value is not None else None

    def machine_share(value):
        """Percent of the whole machine as percent of the cores granted."""
        return _round(value * machine_cores / cores) if value is not None else None

    found = []
    first_live = live[0]["t"] if (period == "1h" and live) else None
    if period != "1h" and live:
        # the minutes not written yet (the span in course, those since the last check): the sampler's own points
        # as 5-minute points, so the chart of a day fills from the first minute
        after = (max(p["t"] for p in history) + BUCKET) if history else 0
        spans: dict[int, tuple[list, list]] = {}
        for p in live:
            start = p["t"] // BUCKET * BUCKET
            if start >= after:
                spans.setdefault(start, ([], []))[0].append(p)
        for m in machine:
            start = m["t"] // BUCKET * BUCKET
            if start in spans:
                spans[start][1].append(m)
        history = history + [bucket_point(s, spans[s][0], spans[s][1], []) for s in sorted(spans)]
    for p in history:
        if p["t"] < since or (first_live is not None and p["t"] + BUCKET > first_live):
            continue
        found.append({"t": p["t"], "odoo_cpu": granted(p.get("oc")), "odoo_cpu_max": granted(p.get("ocx")),
                      "db_cpu": granted(p.get("dc")), "db_cpu_max": granted(p.get("dcx")),
                      "cluster_cpu": machine_share(p.get("pc")), "cluster_mem": p.get("pm"),
                      "odoo_mem": p.get("om"), "odoo_mem_max": p.get("omx"), "db_mem": p.get("dm"),
                      "db_mem_max": p.get("dmx"), "conns": p.get("w")})
    interval = BUCKET
    if period == "1h":
        by_time = {m["t"]: m for m in machine}
        for p in live:
            if p["t"] < since:
                continue
            m = by_time.get(p["t"]) or by_time.get(p["t"] - 1) or by_time.get(p["t"] + 1) or {}
            found.append({"t": p["t"], "odoo_cpu": granted(p.get("ocpu")), "odoo_cpu_max": None,
                          "db_cpu": granted(p.get("dcpu")), "db_cpu_max": None,
                          "cluster_cpu": machine_share(m.get("pg")), "cluster_mem": m.get("pg_mem"),
                          "odoo_mem": p.get("omem"), "odoo_mem_max": None, "db_mem": p.get("dmem"), "db_mem_max": None,
                          "conns": p.get("conns")})
        interval = INTERVAL if live else BUCKET
    if len(found) > MAX_POINTS:
        width = max(-(-int(seconds) // MAX_POINTS), interval)
        found = _shrink(found, width, MEANS, MAXES)
        interval = width
    return found, interval


def tiles(state_checks: list, history: list[dict], period: str, points: list[dict], memory_limit: float | None,
          now: float) -> dict:
    """The four tiles over the period: answered (percent of the checks),
    the response time and the failed requests (from Odoo's log: slice 2,
    None with the reason), the memory (its maximum against what is
    granted)."""
    since = now - PERIODS[period]
    if period in ("1h", "24h"):
        checks = [c for c in state_checks if c[0] >= since]
        asked, answered = len(checks), sum(1 for c in checks if c[1])
    else:
        found = [p for p in history if p["t"] >= since and p.get("ck")]
        asked, answered = sum(p["ck"] for p in found), sum(p.get("up") or 0 for p in found)
        last = found[-1]["t"] + BUCKET if found else since
        recent = [c for c in state_checks if c[0] >= last]
        asked += len(recent)
        answered += sum(1 for c in recent if c[1])
    memory = [_sum(p.get("odoo_mem_max") if p.get("odoo_mem_max") is not None else p.get("odoo_mem"),
                   p.get("db_mem_max") if p.get("db_mem_max") is not None else p.get("db_mem")) for p in points]
    memory = [m for m in memory if m is not None]
    top = max(memory) if memory else None
    return {
        "answered": {"value": round(100.0 * answered / asked, 1) if asked else None, "checks": asked,
                     "failed": asked - answered},
        "response": {"value": None, "reason": FROM_LOG},
        "failed": {"value": None, "reason": FROM_LOG},
        "memory": {"value": _round(top), "limit": memory_limit,
                   "percent": round(top * 100 / memory_limit) if top is not None and memory_limit else None},
    }


def past(state_checks: list, history: list[dict], period: str, now: float) -> list[dict]:
    """What went wrong in the period that the verdict on now does not say:
    the checks without an answer (minutes, the first one's time)."""
    since = now - PERIODS[period]
    if period in ("1h", "24h"):
        failed = [c[0] for c in state_checks if c[0] >= since and not c[1]]
        return [{"kind": "down", "minutes": len(failed), "at": min(failed)}] if failed else []
    found = sorted((p for p in history if p["t"] >= since and p.get("ck") and (p.get("up") or 0) < p["ck"]),
                   key=lambda p: p["t"])
    if not found:
        return []
    return [{"kind": "down", "minutes": sum(p["ck"] - (p.get("up") or 0) for p in found), "at": found[0]["t"]}]


def build(instance, period: str, *, sampler, history: InstanceHistory, state: dict, checks: list,
          thresholds: dict, minutes: int, cores: float, memory_limit: float | None, workers: int | None,
          database: list[dict], where: str, host: str | None, workspace: list[dict] | None, customer: bool,
          now: float | None = None) -> dict:
    """What the page, the REST API and MCP answer of an instance's monitor
    over a period. customer: a workspace's people, who never read the
    machine's PostgreSQL shared by every database, nor a host."""
    now = now if now is not None else time.time()
    if period not in PERIODS:
        raise ValueError(f"the period is one of {', '.join(PERIODS)}")
    key = instance.key
    since = now - PERIODS[period]
    kept = history.points(key, since)
    started = history.started(key)
    live = list(sampler.history.get(key, ()))
    if live and (started is None or live[0]["t"] < started):
        started = live[0]["t"]
    found = verdict(state, live, cores=cores, memory_limit=memory_limit, workers=workers, thresholds=thresholds,
                    minutes=minutes, database=database, workspace=workspace, has_history=bool(kept), now=now)
    points, interval = series(kept, live, list(sampler.machine), period, cores, sampler.cores, now)
    shared = where == "cluster"
    if customer or not shared:
        for p in points:
            p.pop("cluster_cpu", None)
            p.pop("cluster_mem", None)
    if where != "service":
        for p in points:
            for name in ("db_cpu", "db_cpu_max", "db_mem", "db_mem_max"):
                p.pop(name, None)
    database_place = {"where": where}
    if where == "remote" and host and not customer:
        database_place["host"] = host
    return {
        "instance": key, "period": period, "periods": list(PERIODS), "now": int(now), "interval": interval,
        "since": started, "measured_from": started if started and started > since else None,
        "verdict": found, "past": past(checks, kept, period, now) if found["state"] not in ("down", "none") else [],
        "tiles": tiles(checks, kept, period, points, memory_limit, now),
        "load": {"points": points, "cores": cores, "memory_limit": memory_limit,
                 "cpu_threshold": thresholds["cpu"][0], "memory_threshold": thresholds["ram"][0],
                 "workers": workers, "database": database_place,
                 # the series of the machine's PostgreSQL, every database together: its admins only
                 "cluster": shared and not customer},
    }


def workers_of(instance, role_state=None) -> int | None:
    """Odoo's workers of an instance (0: threads; None: not known): a
    workspace's compose project from what its role was given (role_state:
    workspacehelper.role_state), a venv from its Odoo config."""
    if instance.restart.type == "compose":
        if role_state is None:
            return None
        found = role_state(Path(instance.restart.service)).get("parameters", {}).get("workers")
        return int(found) if isinstance(found, (int, str)) and str(found).isdigit() else None
    conf = getattr(instance.options, "odoo_config", None)
    if not conf:
        return None
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with open(conf, encoding="utf-8") as fh:
            parser.read_file(fh)
        value = parser.get("options", "workers", fallback="0").strip()
    except (OSError, configparser.Error, UnicodeDecodeError):
        return None
    return int(value) if value.isdigit() else 0


def where_kind(instance, place: dict | None) -> str:
    """Where the database is, as the monitor tells it: service (the
    project's own, measured), cluster (the machine's PostgreSQL shared by
    every database), remote (another machine), unknown."""
    if instance.restart.type == "compose":
        return "cluster" if getattr(instance.options, "db_service", "db") == "machine" else "service"
    if not place:
        return "unknown"
    return {"remote": "remote", "local": "cluster"}.get(place.get("where"), "unknown")
