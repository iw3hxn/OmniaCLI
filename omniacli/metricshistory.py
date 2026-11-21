# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""This machine's load over the last 14 days (UX-008 §6.3).

The sampler (metrics.py) keeps the last hour of the machine's points, one
every 5 s. Every 5 minutes the points of the 5 minutes just ended become one
point here: the CPU's mean and maximum, the memory in use (mean and maximum)
and in all, and the bytes received and sent. A 5-minute span with no sample
(OmniaCLI was not running) has no point: a gap stays a gap, never a zero.

The points are kept KEEP_DAYS, at most MAX_POINTS, in one JSON file next to
the disks' history, written whole (a temporary file, then renamed) after
each new point: about 4,032 points, some 250 KB, for 14 days.

The bytes of a span are its mean rate times the seconds its samples cover
(INTERVAL each, BUCKET at most): the sampler gives rates, not counters.
"""

from __future__ import annotations

import asyncio
import json
import threading
import logging
import os
import time
from pathlib import Path

from .metrics import INTERVAL

_logger = logging.getLogger(__name__)

BUCKET = 300  # seconds a point covers
KEEP_DAYS = 14
KEEP_SECONDS = KEEP_DAYS * 86400
MAX_POINTS = KEEP_SECONDS // BUCKET  # 4,032
CHECK_EVERY = 30  # seconds between two looks at the sampler
# a span is recorded once its last sample is surely in (the sampler's interval)
SETTLE = INTERVAL + 2
# the fields of a point in the file, in this order
FIELDS = ("t", "n", "cpu", "cpu_max", "ram", "ram_max", "ram_total", "rx", "tx")


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def bucket_point(start: int, samples: list[dict]) -> list | None:
    """The point of the span starting at start from the sampler's points in
    it ([t, n, cpu, cpu_max, ram, ram_max, ram_total, rx, tx]: percent of
    the whole machine, MiB, bytes); None: no sample in the span."""
    if not samples:
        return None
    cpu = [p["cpu"] for p in samples if p.get("cpu") is not None]
    ram = [p["ram"] for p in samples if p.get("ram") is not None]
    totals = [p["ram_total"] for p in samples if p.get("ram_total")]
    covered = min(len(samples) * INTERVAL, BUCKET)

    def total_bytes(key: str) -> int | None:
        rates = [p[key] for p in samples if p.get(key) is not None]
        return round(_mean(rates) * covered) if rates else None

    mean_cpu = _mean(cpu)
    mean_ram = _mean(ram)
    return [start, len(samples),
            round(mean_cpu, 1) if mean_cpu is not None else None, max(cpu) if cpu else None,
            round(mean_ram) if mean_ram is not None else None, max(ram) if ram else None,
            totals[-1] if totals else None,
            total_bytes("rx"), total_bytes("tx")]


_SAVING = threading.Lock()


class MetricsHistory:
    """The 5-minute points of the last KEEP_DAYS, in a JSON file:
    {started: when the history began, points: [[t, n, cpu, ...]]}."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.points: list[list] = []
        self.started: int | None = None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            points = data.get("points")
            if isinstance(points, list):
                self.points = sorted((p for p in points if isinstance(p, list) and len(p) == len(FIELDS)),
                                     key=lambda p: p[0])
            if isinstance(data.get("started"), int):
                self.started = data["started"]
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError, TypeError) as exc:
            _logger.warning("Load history %s not read, starting a new one: %s", self.path, exc)
        if self.started is None and self.points:
            self.started = self.points[0][0]

    @property
    def last(self) -> int | None:
        """The start of the last span recorded."""
        return self.points[-1][0] if self.points else None

    def begin(self, now: float | None = None) -> None:
        """The history starts now, unless it started before."""
        if self.started is None:
            self.started = int(now if now is not None else time.time())
            self._save(now)

    def add(self, points: list[list], now: float | None = None) -> None:
        """New points (after the last one), older than KEEP_DAYS dropped, then saved."""
        last = self.last
        fresh = [p for p in points if last is None or p[0] > last]
        if not fresh:
            return
        self.points.extend(sorted(fresh, key=lambda p: p[0]))
        self._save(now)

    def _save(self, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        oldest = now - KEEP_SECONDS
        self.points = [p for p in self.points if p[0] >= oldest][-MAX_POINTS:]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # one writer at a time: the recorder's loop and a request may save together,
        # and two writers of the one temporary file lose it to each other's rename
        with _SAVING:
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as out:
                json.dump({"started": self.started, "bucket": BUCKET, "fields": FIELDS, "points": self.points},
                          out, separators=(",", ":"))
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, self.path)

    def window(self, days: int, now: float | None = None) -> dict:
        """{started, points: [{t, cpu, cpu_max, ram, ram_max, ram_total, rx, tx}]
        (rx, tx: bytes per second), summary} of the last days."""
        now = now if now is not None else time.time()
        since = now - days * 86400
        found = [p for p in self.points if p[0] >= since]
        points = []
        for t, n, cpu, cpu_max, ram, ram_max, ram_total, rx, tx in found:
            covered = min(n * INTERVAL, BUCKET) or BUCKET
            points.append({"t": t, "cpu": cpu, "cpu_max": cpu_max, "ram": ram, "ram_max": ram_max,
                           "ram_total": ram_total,
                           "rx": round(rx / covered) if rx is not None else None,
                           "tx": round(tx / covered) if tx is not None else None})
        return {"started": self.started, "points": points, "summary": summary(found)}


def summary(found: list[list]) -> dict:
    """The tiles' numbers of the points found: the CPU's mean (by samples)
    and maximum with its time, the memory now and its maximum, the bytes."""
    cpu = [(p[2], p[1]) for p in found if p[2] is not None]
    weight = sum(n for _, n in cpu)
    peak = max((p for p in found if p[3] is not None), key=lambda p: p[3], default=None)
    ram_peak = max((p[5] for p in found if p[5] is not None), default=None)
    latest = next((p for p in reversed(found) if p[4] is not None), None)
    rx = [p[7] for p in found if p[7] is not None]
    tx = [p[8] for p in found if p[8] is not None]
    return {
        "cpu_mean": round(sum(v * n for v, n in cpu) / weight, 1) if weight else None,
        "cpu_max": peak[3] if peak else None, "cpu_max_t": peak[0] if peak else None,
        "ram_now": latest[4] if latest else None, "ram_total": latest[6] if latest else None,
        "ram_max": ram_peak,
        "rx_bytes": sum(rx) if rx else None, "tx_bytes": sum(tx) if tx else None,
    }


class MetricsRecorder:
    """Turns the sampler's machine points into a 5-minute point once each
    span has ended, while OmniaCLI runs."""

    def __init__(self, sampler, history: MetricsHistory):
        self.sampler, self.history = sampler, history
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self.history.begin()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    def check(self, now: float | None = None) -> int:
        """Record every span ended since the last one recorded (of the hour
        the sampler holds); the number of points added."""
        now = now if now is not None else time.time()
        samples = list(self.sampler.machine)
        if not samples:
            return 0
        # the last span that has surely ended
        end = int((now - SETTLE) // BUCKET) * BUCKET
        first = samples[0]["t"] // BUCKET * BUCKET
        last = self.history.last
        if last is not None:
            first = max(first, last + BUCKET)
        spans: dict[int, list[dict]] = {}
        for p in samples:
            start = p["t"] // BUCKET * BUCKET
            if first <= start < end:
                spans.setdefault(start, []).append(p)
        points = [bucket_point(start, spans[start]) for start in sorted(spans)]
        points = [p for p in points if p is not None]
        if points:
            self.history.add(points, now)
        return len(points)

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.check)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the recording must not stop
                _logger.exception("Load history not recorded")
            await asyncio.sleep(CHECK_EVERY)
