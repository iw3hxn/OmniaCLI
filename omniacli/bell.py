# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The bell in the header of every page (README, The bell): the things to
look at of the map (inframap: the verdict's watch, with their levels) that
are new for the person who reads them, the latest in a dropdown, each with
the page where it is fixed, and «mark as read», kept per person.

A thing is the same thing while its key is (key_of): its kind, its node and
what makes it another time (the job that failed, the moment it stopped); the
key holds no text, so the language of the reader does not make it new. What
a person marked as read is kept next to the servers file (READ_FILE, mode
600), only the keys still to look at: a thing that went away and comes back
with another key is new again.

The list is the map's own, read from what the panel holds in memory (part
"now" of /api/map: no question to a machine per page), and kept for EVERY
seconds per reader: a page asks it after it is drawn, never while."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

READ_FILE = "bell.json"
# the latest shown in the dropdown; the others are on the map
SHOWN = 8
# a reader's list is put together again after these seconds at the soonest
EVERY = 30
# the keys a person may mark at once, and the length of one
MARK_MAX = 500
KEY_MAX = 300
# the kinds whose moment is no other time of the thing: the certificate is checked again
# (its moment is the last check), the names have none, a linked machine is read again
TIMELESS = {"certificate", "names", "machine_older", "machine_unread"}


def key_of(item: dict) -> str:
    """What makes a thing to look at the same thing: kind, node, and its job or moment."""
    kind = str(item.get("kind") or "")
    node = str(item.get("node") or "")
    if item.get("job"):
        return f"{kind}|{node}|job:{item['job']}"[:KEY_MAX]
    if kind in TIMELESS or not item.get("since"):
        nodes = ",".join(sorted(str(n) for n in item.get("nodes") or []))
        return f"{kind}|{node}|{nodes}"[:KEY_MAX]
    return f"{kind}|{node}|{item['since']}"[:KEY_MAX]


def link_of(item: dict, links: set[str] | frozenset[str] = frozenset()) -> str | None:
    """The page where the thing is fixed: its own (a job, Options), an
    instance's page (this machine's, or a linked one's through its link),
    the Links page for a linked machine that does not answer or is older."""
    if item.get("page"):
        return str(item["page"])
    kind, node = str(item.get("kind") or ""), str(item.get("node") or "")
    if kind in ("machine_older", "machine_unread"):
        return "/links"
    machine, _, server = node.partition("/")
    if not server or server.startswith(("ws:", "db:", "dest:", "replica:")):
        return None
    if machine == "~":
        return f"/servers/{server}"
    if machine in links:
        return f"/links/{machine}/servers/{server}"
    return None


class Reads:
    """What each person marked as read: {person: {"read": [keys], "at"}}, a
    file of mode 600 written through a temporary file renamed over it."""

    def __init__(self, path: Path):
        self.path = path
        self._data: dict | None = None

    def load(self) -> dict:
        if self._data is None:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            self._data = data if isinstance(data, dict) else {}
        return self._data

    def of(self, person: str) -> set[str]:
        found = self.load().get(person)
        return set(found.get("read") or []) if isinstance(found, dict) else set()

    def mark(self, person: str, keys: list[str], current: set[str], at: str) -> None:
        """keys read by person; only the keys still current are kept (a thing gone is forgotten)."""
        read = (self.of(person) | set(keys)) & current
        data = self.load()
        data[person] = {"read": sorted(read), "at": at}
        self._save(data)

    def forget(self, person: str) -> None:
        data = self.load()
        if data.pop(person, None) is not None:
            self._save(data)

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=1, sort_keys=True) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)


def answer(watch: list[dict], read: set[str], links: set[str] | frozenset[str] = frozenset()) -> dict:
    """What the bell says: how many are new, the latest SHOWN (the new
    first, the critical first among them, the newest first), each with its
    key, level, text, moment and page, and how many more there are."""
    rows = []
    for item in watch:
        key = key_of(item)
        rows.append({"key": key, "kind": item.get("kind"), "level": "critical" if item.get("level") == "critical" else "warning",
                     "text": str(item.get("text") or ""), "since": item.get("since"), "page": link_of(item, links),
                     "new": key not in read})
    rows.sort(key=lambda r: str(r["since"] or ""), reverse=True)
    rows.sort(key=lambda r: (not r["new"], r["level"] != "critical"))
    new = sum(1 for r in rows if r["new"])
    return {"count": new, "total": len(rows), "items": rows[:SHOWN], "more": max(0, len(rows) - SHOWN),
            "keys": [r["key"] for r in rows]}


class Cache:
    """Each reader's list, put together again after EVERY seconds at the soonest."""

    def __init__(self, every: float = EVERY):
        self.every = every
        self._found: dict[tuple, tuple[float, list[dict]]] = {}

    def get(self, who: tuple) -> tuple[list[dict] | None, bool]:
        """(the list, whether it is fresh); (None, False) when there is none."""
        found = self._found.get(who)
        if found is None:
            return None, False
        return found[1], time.monotonic() - found[0] < self.every

    def put(self, who: tuple, watch: list[dict]) -> None:
        self._found[who] = (time.monotonic(), watch)

    def clear(self) -> None:
        self._found.clear()
