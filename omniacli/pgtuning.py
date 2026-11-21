# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The tuning of the dedicated databases of this machine (UX-008 §6.2,
docs/adr/postgresql-della-macchina.md AD-005, steps 1 and 2 of §9).

- What a database service is set to now, read by the root helper's fixed
  query (compose-helper pg-settings): the parameters of
  composehelper.TUNED, their base (postgresql.conf, else the built-in
  value), when a change takes effect (pg_settings.context: postmaster is
  at a restart) and the others written by ALTER SYSTEM, never the point-in-
  time recovery's or the replication's.
- The proposal: the memory for the databases of the machine (a quarter of
  its memory by default, D13) goes to the productions; tests and stagings
  stay at their base values (D10), and what they hold counts against it.
- The record (FILE, next to the jobs): who tuned which database, when, the
  parameters touched (the only ones Back to the base values resets) and the
  memory used; a restart planned once, at a time, with the values it
  writes. A plan survives a restart of OmniaCLI, and is cancelled by
  removing it."""

from __future__ import annotations

import datetime
import json
import math
import os
import re
import threading
from pathlib import Path

from . import ops
from .composehelper import TUNED, TUNED_VALUE, STATEMENTS_LIBRARY, hidden
from .jobs import JobFailed

FILE = "postgres_tuning.json"
KIB, MIB, GIB, TIB = 1 << 10, 1 << 20, 1 << 30, 1 << 40
UNITS = {"B": 1, "kB": KIB, "MB": MIB, "GB": GIB, "TB": TIB}
# the order of the parameters on the page (UX-008 §6.2.3)
PARAMETERS = ("max_connections", "shared_buffers", "effective_cache_size", "work_mem", "maintenance_work_mem",
              "random_page_cost", "shared_preload_libraries")
assert set(PARAMETERS) == set(TUNED)
# a database read for the page: at most so long, each on its own
READ_TIMEOUT = 8
# the connections kept free beyond those in use when max_connections is lowered
RESERVE = 10
# D13: a quarter of the memory, at most what the workspaces leave less 1 GB for the system
SHARE = 4
SYSTEM_GB = 1
# the step of a proposed memory, and the least one
STEP = 16 * MIB
DISKS = ("ssd", "hdd")
# who shares the memory for the databases (D10): the productions' dedicated
# databases and the machine's cluster
SHARING = ("production", "machine")
# a planned restart OmniaCLI was not running for: done the next day at the same time, never late in the day
MISSED_AFTER = 15 * 60
# the restart's default time for a production (D11): after the automatic backups of 03:00
DEFAULT_TIME = "05:00"
TIME = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])")
_PG_VALUE = re.compile(r"\s*([0-9]+(?:\.[0-9]+)?)\s*(B|kB|MB|GB|TB)?\s*")
# what a person types in the dialog: a number and MB or GB (kB, TB too), "1,5 GB" as well
_TYPED = re.compile(r"\s*([0-9]+(?:[.,][0-9]+)?)\s*(kB|MB|GB|TB)\s*", re.I)


def unit_bytes(unit: str | None) -> int | None:
    """The bytes of pg_settings' unit (8kB, kB, MB, B); None: not a memory."""
    found = re.fullmatch(r"([0-9]*)(B|kB|MB|GB|TB)", unit or "")
    return int(found[1] or 1) * UNITS[found[2]] if found else None


def pg_bytes(text, unit: str | None) -> int | None:
    """A memory as PostgreSQL writes it (128MB, 4GB, or a number in the
    parameter's unit) in bytes; None when it is none."""
    found = _PG_VALUE.fullmatch(str(text if text is not None else ""))
    if not found:
        return None
    size = float(found[1])
    if found[2]:
        return int(size * UNITS[found[2]])
    per = unit_bytes(unit)
    return int(size * per) if per else None


def typed_bytes(text: str) -> int | None:
    """A memory as typed in the dialog (1 GB, 1024 MB, 1,5 GB); None otherwise ("1 G")."""
    found = _TYPED.fullmatch(str(text or ""))
    if not found:
        return None
    unit = next(u for u in UNITS if u.lower() == found[2].lower())
    return int(float(found[1].replace(",", ".")) * UNITS[unit])


def memory_text(size: int) -> str:
    """A memory for ALTER SYSTEM: whole GB, else whole MB, else kB."""
    for unit in ("GB", "MB"):
        if size >= UNITS[unit] and size % UNITS[unit] == 0:
            return f"{size // UNITS[unit]}{unit}"
    return f"{max(1, size // KIB)}kB"


def libraries(text: str | None) -> list[str]:
    return [part.strip() for part in str(text or "").split(",") if part.strip()]


def value_of(row: dict, text) -> object:
    """A value of the parameter of row (as pg_settings, postgresql.conf or
    postgresql.auto.conf write it) as the page compares it: bytes, a
    number, or whether the statistics of the queries are loaded."""
    kind = TUNED[row["name"]]
    if text is None:
        return None
    if kind == "memory":
        return pg_bytes(text, row.get("unit"))
    if kind == "libraries":
        return STATEMENTS_LIBRARY in libraries(text)
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    return int(number) if kind == "integer" else number


def parameters(found: dict) -> list[dict]:
    """The parameters of a pg-settings answer, in the page's order: now,
    base (postgresql.conf, else the built-in value), at the restart (the
    value of postgresql.auto.conf waiting for it), when a change takes
    effect, the bounds."""
    rows = {row["name"]: row for row in found.get("settings") or [] if row.get("name") in TUNED}
    out = []
    for name in PARAMETERS:
        row = rows.get(name)
        if row is None:
            continue
        kind = TUNED[name]
        base_text = row.get("file_val") if row.get("file_val") is not None else row.get("boot_val")
        pending = bool(row.get("pending_restart"))
        out.append({
            "name": name, "kind": kind, "unit": row.get("unit"),
            "now": value_of(row, row.get("setting")),
            "base": value_of(row, base_text),
            # what ALTER SYSTEM wrote: tuned, waiting for a restart or not
            "written": value_of(row, row.get("auto_val")),
            "at_restart": value_of(row, row.get("auto_val")) if pending else None,
            "restart": row.get("context") == "postmaster",
            "pending": pending,
            "min": value_of(row, row.get("min_val")) if kind != "libraries" else None,
            "max": value_of(row, row.get("max_val")) if kind != "libraries" else None,
            "libraries": libraries(row.get("setting")) if kind == "libraries" else None,
        })
    return out


def changed(found: dict) -> list[dict]:
    """The other parameters ALTER SYSTEM wrote, never a hidden one (the
    helper's query leaves them out already: twice is the safety net)."""
    return [{"name": c["name"], "now": c.get("setting"), "unit": c.get("unit"), "written": c.get("auto_val"),
             "restart": c.get("context") == "postmaster", "pending": bool(c.get("pending_restart"))}
            for c in found.get("changed") or [] if c.get("name") and not hidden(c["name"])
            and c["name"] not in TUNED]


def by_name(params: list[dict]) -> dict[str, dict]:
    return {p["name"]: p for p in params}


def shared_of(params: list[dict]) -> int:
    """The shared memory a database holds: the value waiting for the restart, else now."""
    row = by_name(params).get("shared_buffers") or {}
    return int(row.get("at_restart") or row.get("now") or 0)


def written_outside(params: list[dict]) -> bool:
    """Values of the tuned ones written by ALTER SYSTEM (a staging copied by
    the point-in-time recovery carries its production's)."""
    return any(p["written"] is not None and p["written"] != p["base"] for p in params)


async def read(instance) -> dict:
    """The tuning's state of a compose instance's database service, read by
    the root helper (pg-settings); JobFailed with why when it does not answer."""
    opts = instance.options
    cmd = instance.restart.command("pg-settings", getattr(opts, "db_service", "db"), opts.database)
    code, out = await ops.capture(cmd, timeout=READ_TIMEOUT)
    if code:
        raise JobFailed(why(out))
    found = next((json.loads(line) for line in out.splitlines() if line.startswith("{")), None)
    if not isinstance(found, dict):
        raise JobFailed(why(out))
    return found


def why(out: str) -> str:
    """Why a database did not answer, in a few words."""
    text = out.strip()
    if "is not running" in text or "No such container" in text or "not running" in text:
        return "the container is stopped"
    if text.startswith("no answer within"):
        return text
    lines = [line for line in text.splitlines() if line.strip()]
    return (lines[-1] if lines else "no answer")[-300:]


# --- the memory, the proposal and its checks

def machine_memory_gb(meminfo: Path = Path("/proc/meminfo")) -> float | None:
    """The machine's memory in GB (GiB), None when unknown."""
    try:
        for line in meminfo.read_text().splitlines():
            key, _, value = line.partition(":")
            if key == "MemTotal":
                return int(value.split()[0]) * KIB / GIB
    except (OSError, ValueError, IndexError):
        return None
    return None


def default_memory_gb(total_gb: float | None, odoo_gb: float = 0) -> float:
    """D13: a quarter of the machine's memory, at most the memory less what
    the workspaces keep for Odoo less 1 GB for the system; at least 1 GB."""
    if not total_gb:
        return 1.0
    quarter = round(total_gb / SHARE * 2) / 2
    return max(1.0, min(quarter, math.floor(total_gb - odoo_gb - SYSTEM_GB)))


def disk_kind(path: Path) -> str:
    """ssd or hdd, as the kernel says of the disk that holds path (a
    virtual disk may say hdd without being one: the dialog corrects it)."""
    try:
        dev = os.stat(path).st_dev
        block = Path(f"/sys/dev/block/{os.major(dev)}:{os.minor(dev)}").resolve()
        for folder in (block, block.parent):
            flag = folder / "queue" / "rotational"
            if flag.is_file():
                return "hdd" if flag.read_text().strip() == "1" else "ssd"
    except OSError:
        pass
    return "ssd"


def _down(size: float, step: int = STEP) -> int:
    return max(step, int(size // step) * step)


def propose(target: dict, rows: list[dict], memory_gb: float, disk: str, limit_gb: float | None = None) -> dict:
    """The values proposed for the cluster of target (a row of the page,
    with "kind" and "params"), from the memory for the databases of the
    machine: the productions share what the tests and stagings do not hold
    (D10); a test or a staging keeps its values. A workspace's memory limit
    holds Odoo and its database together: the database takes at most a
    quarter of it as shared memory."""
    params = by_name(target["params"])
    budget = memory_gb * GIB
    others = [r for r in rows if r["key"] != target["key"] and r.get("params")]
    # the machine's cluster takes its share as a production does: it serves productions
    held = sum(shared_of(r["params"]) for r in others if r["kind"] not in SHARING)
    productions = sum(1 for r in rows if r["kind"] in SHARING and r.get("params"))
    if target["kind"] not in SHARING:
        values = {name: p["at_restart"] if p["at_restart"] is not None else p["now"] for name, p in params.items()}
        share = None
    else:
        share = max(0.0, (budget - held) / max(1, productions))
        if limit_gb:
            share = min(share, limit_gb * GIB)
        values = {
            "max_connections": params["max_connections"]["now"] if "max_connections" in params else None,
            "shared_buffers": _down(share / 4),
            "effective_cache_size": _down(share * 3 / 4),
            "work_mem": min(64 * MIB, max(4 * MIB, _down(share / 512, MIB))),
            "maintenance_work_mem": min(2 * GIB, max(64 * MIB, _down(share / 16, MIB))),
            "random_page_cost": 1.1 if disk == "ssd" else 4.0,
            # D12: the statistics of the queries, already ticked
            "shared_preload_libraries": True,
        }
    return {"values": {k: v for k, v in values.items() if k in params}, "share": share, "held": held,
            "productions": productions, "budget": budget}


def limits(target: dict, rows: list[dict], memory_gb: float, connections: int) -> dict:
    """What the dialog checks while one types: the most shared memory this
    database may take (the memory less what the others hold), the fewest
    connections (those in use and RESERVE), the bounds of pg_settings."""
    others = sum(shared_of(r["params"]) for r in rows if r["key"] != target["key"] and r.get("params"))
    # the machine's cluster: never fewer than its environments' roles may open (AD-005 §7)
    promised = int(target.get("promised") or 0)
    return {"shared_max": int(memory_gb * GIB - others), "others": others, "budget": int(memory_gb * GIB),
            "connections_min": max(int(connections), promised) + RESERVE, "connections": max(int(connections), promised),
            "promised": promised,
            "bounds": {p["name"]: [p["min"], p["max"]] for p in target["params"] if p["kind"] != "libraries"}}


def gb_text(size: float) -> str:
    return f"{size / GIB:.2f}".rstrip("0").rstrip(".") + " GB"


def size_text(size: float) -> str:
    return gb_text(size) if size >= GIB else f"{size / MIB:.0f} MB"


def checked(target: dict, posted: dict, bounds: dict) -> dict:
    """The posted values (text as typed, a bool for the statistics) as
    ALTER SYSTEM writes them, of the changed parameters only; ValueError
    with what is wrong, in the dialog's words."""
    params = by_name(target["params"])
    out, sizes = {}, {}
    for name, raw in (posted or {}).items():
        if name not in params or hidden(name):
            raise ValueError(f"{name} is not a parameter of the tuning")
        p = params[name]
        current = p["at_restart"] if p["at_restart"] is not None else p["now"]
        if p["kind"] == "libraries":
            on = bool(raw)
            if on == bool(current):
                continue
            libs = [lib for lib in p["libraries"] if lib != STATEMENTS_LIBRARY] + ([STATEMENTS_LIBRARY] if on else [])
            out[name] = ",".join(libs)
            continue
        text = str(raw if raw is not None else "").strip()
        if p["kind"] == "memory":
            size = typed_bytes(text)
            if size is None:
                raise ValueError(f'"{text}" is not a value: write a number followed by MB or GB, for example 1024 MB')
            # whole MB: what PostgreSQL rounds to anyway for most of them
            size = size // MIB * MIB if size >= MIB else size
            value, written = size, memory_text(size)
            sizes[name] = size
        else:
            try:
                value = float(text.replace(",", "."))
            except ValueError:
                raise ValueError(f'"{text}" is not a number') from None
            if p["kind"] == "integer":
                if value != int(value):
                    raise ValueError(f'"{text}" is not a whole number')
                value = int(value)
            written = str(value) if p["kind"] == "integer" else f"{value:g}"
        low, high = p["min"], p["max"]
        if (low is not None and value < low) or (high is not None and value > high):
            shown = (size_text if p["kind"] == "memory" else (lambda v: f"{v:g}"))
            raise ValueError(f"{name}: from {shown(low)} to {shown(high)}")
        if value == current:
            continue
        if not TUNED_VALUE[TUNED[name]].fullmatch(written):
            raise ValueError(f'"{text}" is not a value of {name}')
        out[name] = written
    sb = sizes.get("shared_buffers") if "shared_buffers" in out else None
    if sb is not None and sb > bounds["shared_max"]:
        raise ValueError(f"Shared memory: {size_text(sb)} exceeds the memory for the databases of this machine "
                         f"({gb_text(bounds['budget'])}, of which {gb_text(bounds['others'])} already assigned)")
    if "max_connections" in out and int(out["max_connections"]) < bounds["connections_min"]:
        raise ValueError(f"Connections at most: {out['max_connections']} is less than the {bounds['connections']} "
                         f"in use now and {RESERVE} in reserve: the least here is {bounds['connections_min']}")
    return out


def next_time(hhmm: str, now: datetime.datetime | None = None) -> datetime.datetime:
    """The next hh:mm from now: today if it is still to come, else tomorrow."""
    found = TIME.fullmatch(hhmm or "")
    if not found:
        raise ValueError(f"{hhmm!r} is not a time, as 05:00")
    now = now or datetime.datetime.now()
    at = now.replace(hour=int(found[1]), minute=int(found[2]), second=0, microsecond=0)
    return at if at > now else at + datetime.timedelta(days=1)


# --- the record

class Store:
    """The record of the tuning, a JSON file:
    {"machine": {"memory_gb": 4, "disk": "ssd"},
     "clusters": {"<server>/<role>": {"tuned": ISO, "by": user, "params": [names],
                  "memory_gb": 4, "restart": {"at": ISO, "set": {name: value},
                  "reset": [names], "by": user, "job": id or null} or absent}}}"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data.setdefault("machine", {})
        data.setdefault("clusters", {})
        return data

    def update(self, change) -> dict:
        """change(data) under the lock, then data saved whole."""
        with self._lock:
            data = self.load()
            change(data)
            partial = self.path.with_name(self.path.name + ".part")
            partial.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(partial, self.path)
            return data

    def cluster(self, key: str) -> dict:
        return self.load()["clusters"].get(key) or {}


def state(kind: str, params: list[dict] | None, record: dict, error: str | None = None) -> dict:
    """The Tuning column (UX-008 §6.2.2): its code, and the values its words take."""
    if error is not None:
        return {"code": "error", "reason": error}
    restart = record.get("restart")
    if restart:
        return {"code": "scheduled", "at": restart.get("at"),
                "count": len(restart.get("set") or {}) + len(restart.get("reset") or [])}
    pending = [p["name"] for p in params or [] if p["pending"]]
    if pending:
        return {"code": "pending", "count": len(pending)}
    if record.get("tuned"):
        return {"code": "tuned", "when": record["tuned"]}
    if params and written_outside(params):
        return {"code": "tuned_unknown"}
    return {"code": "base_production" if kind in SHARING else "base"}
