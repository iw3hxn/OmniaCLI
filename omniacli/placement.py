# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The move of an instance between the provider's machines
(docs/adr/AD-008-consegna-e-spostamento.md, AD-014 and AD-015; step 4 of its
§9; the screens are UX-014 part B). The machine of the instance's workspace
(its home) copies the instance onto the machine of one of its links (the
host) while it runs, then, at Complete, stops it here and starts it there.

What is kept, by OmniaCLI's user, 600 in folders 700:

- on the home: move/<server>/state.json, the state of one move (its link,
  its step, its sizes, the times measured; never a secret), and
  placements.json, {server: {link, state, since, copy_until, names,
  revision}} of the instances placed elsewhere or on their way;
- on the host: hosted-workspaces.json, {workspace: {home, provider_label,
  members, revision, made, instances: {instance: {state, since}}}}: home is
  the name of the incoming link of the home (auth.links), the only one that
  may ask about that workspace.

The states of a move: preparing (the job of Prepare runs), ready (copied,
the database in step), completing, placed (it serves on the host; the copy
here is stopped until copy_until), failed (Prepare did not end: Cancel
takes away what it made). Until step 5 of §9 (the delegated scope) only an
instance of a workspace without members moves: its users would not reach it
on the host."""

from __future__ import annotations

import datetime
import json
import math
import os
from pathlib import Path

STATE_FILE = "state.json"
PLACEMENTS = "placements.json"
HOSTED = "hosted-workspaces.json"
MOVE_FOLDER = "move"
# the copy stopped here after Complete, kept this many days (D-B4)
COPY_DAYS = 7
# Complete only with the database at most this many seconds behind (AD-015)
LAG_MOST = 60
# the files: the last round of the copy finished at most this many seconds ago
FILES_FRESH = 600
# the stop of Complete, from the maintenance page to the first 200 through the name: what step 4 promises
STOP_MOST = 90
# a line the plan estimates a copy with when none was measured yet (megabytes per second)
RATE_GUESS = 50.0
STATES = ("preparing", "ready", "completing", "placed", "failed")


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _write(path: Path, data: dict) -> None:
    """A JSON file of OmniaCLI's user, 600 in a folder 700, written whole."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    part = path.with_name(path.name + ".tmp")
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(data, indent=1, sort_keys=True) + "\n")
    os.replace(part, path)


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


class Moves:
    """The moves of this machine's instances to other machines (the home's side)."""

    def __init__(self, base: Path):
        self.base = base

    def folder(self, server: str) -> Path:
        return self.base / MOVE_FOLDER / server

    def state(self, server: str) -> dict:
        return _read(self.folder(server) / STATE_FILE)

    def save(self, server: str, state: dict) -> dict:
        """The move's state written, and its line of placements.json."""
        state = {**state, "server": server, "updated": _now()}
        (self.base / MOVE_FOLDER).mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.base / MOVE_FOLDER, 0o700)
        _write(self.folder(server) / STATE_FILE, state)
        placed = self.placements()
        revision = int((placed.get(server) or {}).get("revision") or 0) + 1
        placed[server] = {**{k: state.get(k) for k in ("link", "state", "since", "copy_until", "names", "copy")},
                          "revision": revision}
        _write(self.base / PLACEMENTS, placed)
        return state

    def update(self, server: str, **values) -> dict:
        return self.save(server, {**self.state(server), **values})

    def forget(self, server: str) -> None:
        """A move cancelled, or a copy deleted with no instance left elsewhere to name: its files go."""
        folder = self.folder(server)
        for path in (folder / STATE_FILE, folder / (STATE_FILE + ".tmp")):
            path.unlink(missing_ok=True)
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
        placed = self.placements()
        if placed.pop(server, None) is not None:
            if placed:
                _write(self.base / PLACEMENTS, placed)
            else:
                (self.base / PLACEMENTS).unlink(missing_ok=True)

    def placements(self) -> dict:
        found = _read(self.base / PLACEMENTS)
        return {str(k): v for k, v in found.items() if isinstance(v, dict)}

    def frozen(self, server: str) -> str:
        """Why nothing but the move's own jobs may act on server's instances here, "" when they may."""
        found = self.placements().get(server) or {}
        if found.get("state") in ("completing", "placed") and found.get("copy") != "deleted":
            return f"{server} was moved to {found.get('link')}: its copy here is stopped, and nothing runs on it"
        return ""


class Hosted:
    """The workspaces this machine hosts instances of for their home (the host's side)."""

    def __init__(self, base: Path):
        self.path = base / HOSTED

    def all(self) -> dict:
        found = _read(self.path)
        return {str(k): v for k, v in found.items() if isinstance(v, dict)}

    def get(self, workspace: str) -> dict:
        return self.all().get(workspace) or {}

    def put(self, workspace: str, entry: dict | None) -> None:
        found = self.all()
        if entry is None:
            found.pop(workspace, None)
        else:
            found[workspace] = {**entry, "revision": int((found.get(workspace) or {}).get("revision") or 0) + 1}
        if found:
            _write(self.path, found)
        else:
            self.path.unlink(missing_ok=True)

    def home_of(self, workspace: str) -> str:
        return str(self.get(workspace).get("home") or "")


def forward_wanted(moves: Moves, workspace: str, names_of) -> tuple[str, str, list[str]]:
    """(mode, to, names) of the forward of a workspace's names from its moves now (AD-012), names_of(instance)
    its names: full for the names of its instances placed on another machine (or completing), whose copy is
    here; else acme for those of the instances prepared there (when none is placed: one record per workspace,
    one mode for its names); else off. A workspace forwards to one machine at a time (the move refuses another)."""
    full, acme, to = [], [], ""
    for server, row in moves.placements().items():
        state = moves.state(server)
        if state.get("workspace") != workspace or row.get("copy") == "deleted":
            continue
        names = list(names_of(str(state.get("instance") or "")))
        if row.get("state") in ("completing", "placed"):
            full += names
        elif row.get("state") in ("preparing", "ready") and state.get("acme"):
            acme += names
        else:
            continue
        to = to or str(state.get("address") or "")
    if full:
        return "full", to, sorted(set(full))
    if acme:
        return "acme", to, sorted(set(acme))
    return "off", "", []


def _date(iso) -> str:
    try:
        return datetime.datetime.fromisoformat(str(iso)).strftime("%d/%m/%Y %H:%M")
    except (TypeError, ValueError):
        return "?"


def copy_until(now: datetime.datetime | None = None) -> str:
    return ((now or datetime.datetime.now()) + datetime.timedelta(days=COPY_DAYS)).isoformat(timespec="seconds")


def percentile(values, share: float = 95) -> float | None:
    """The share-th percentile of values (nearest rank), None for none."""
    found = sorted(v for v in values if v is not None)
    if not found:
        return None
    rank = max(1, math.ceil(share / 100 * len(found)))
    return found[rank - 1]


def load_of(points: list[dict]) -> dict:
    """{cpu_p95, ram_p95, days} of a machine's points of metricshistory
    (percent of the CPU; the memory in use as a percent of its total)."""
    ram = [p["ram"] / p["ram_total"] * 100 for p in points
           if p.get("ram") is not None and p.get("ram_total")]
    cpu = percentile([p.get("cpu") for p in points])
    memory = percentile(ram)
    span = (points[-1]["t"] - points[0]["t"]) / 86400 if len(points) > 1 else 0
    return {"cpu_p95": round(cpu, 1) if cpu is not None else None,
            "ram_p95": round(memory, 1) if memory is not None else None, "days": round(span, 1)}


def seconds_at(size: int, rate_mbs: float | None) -> int:
    """The seconds a copy of size bytes takes at rate_mbs megabytes a second."""
    rate = rate_mbs if rate_mbs and rate_mbs > 0 else RATE_GUESS
    return int(math.ceil(size / (rate * (1 << 20)))) if size else 0


def plan(sizes: dict, database: str, rate_mbs: float | None, restore_rate_mbs: float | None = None) -> dict:
    """What Prepare copies and what Complete stops, in numbers: sizes
    {database, filestore, addons, backups} in bytes; database "dedicated"
    (it follows: the stop is its last records, about 10 s, and the start of
    Odoo) or "cluster" (dumped, copied and restored during the stop)."""
    copied = sum(int(sizes.get(k) or 0) for k in ("database", "filestore", "addons", "backups"))
    if database == "dedicated":
        stop = 10 + 30
    else:
        size = int(sizes.get("database") or 0)
        # the dump and the restore are about the copy's time each, from what was measured on lab3/lab4
        stop = 30 + 3 * seconds_at(size, restore_rate_mbs or rate_mbs)
    return {"copied": copied, "copy_seconds": seconds_at(copied, rate_mbs), "rate_mbs": rate_mbs or RATE_GUESS,
            "measured_rate": bool(rate_mbs), "stop_seconds": stop}
