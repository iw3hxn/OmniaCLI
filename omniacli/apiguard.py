# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a program meets when it drives OmniaCLI, through the REST API
(/api/v1, apiv1.py) or through MCP (mcp_server.py): the same rules for both.

- The level of a token is its role: user reads; developer also acts on the
  tests and the stagings (and makes stagings); admin also acts on a
  production, each time with confirm = the server's name, as the pages have
  it typed.
- A production is updated by hand only (Carlo, 22/11/2026: «la CI va fatta
  solo sugli ambienti di stage, la rebuild della produzione deve essere fatta
  solo a mano»): a token marked CI never acts on a production, whatever its
  role; any other token acts on one only with the admin role and the
  server's name in confirm on that very call. A staging is not a production
  here: it is what the CI is for.
- A rate limit per token: READ_PER_MINUTE reads and WRITE_PER_MINUTE writes
  in any WINDOW seconds; past it, 429 with Retry-After.
- Idempotency keys: the same key of the same token with the same request
  within KEEP_HOURS answers what the first answered (the same job); with a
  different request, 409.
"""

from __future__ import annotations

import asyncio
import collections
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

from .auth import ROLES

READ_PER_MINUTE = 600
WRITE_PER_MINUTE = 60
WINDOW = 60  # seconds
KEEP_HOURS = 24
# entries kept at most, the oldest dropped first (a token cannot fill the disk)
KEEP_ENTRIES = 5000
FILE = "api-idempotency.json"
IDEMPOTENCY_KEY = re.compile(r"[\x21-\x7e]{1,200}")

# the level a role gives a token, as the pages and the README name it
LEVELS = {"user": "read", "developer": "write", "admin": "production"}


class ApiError(Exception):
    """A refusal of the REST API: its HTTP status, a stable code (for
    programs) and an English message (for people)."""

    def __init__(self, status: int, code: str, message: str, headers: dict | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.headers = status, code, message, headers or {}

    def body(self) -> dict:
        return {"error": {"code": self.code, "message": self.message}}


def can(role: str, needed: str) -> bool:
    return ROLES.index(role) >= ROLES.index(needed)


def production_refusal(by_hand: bool, server: str, role: str, ci: bool, confirm: str, action: str,
                       confirm_needed: bool = True) -> ApiError | None:
    """Why a token may not do action on an instance (None: it may): by_hand
    says the instance is a production (not a staging); role is the token's,
    ci whether it is a CI's, confirm what this call gave."""
    if not by_hand:
        return None
    if ci:
        return ApiError(403, "production_by_hand",
                        f"A production is updated by hand only: a CI token never acts on the production of {server}")
    if not can(role, "admin"):
        return ApiError(403, "production_level_required",
                        f"{action} on the production of {server} needs a token with the production level "
                        f"(role admin); this one is {role}")
    if confirm_needed and (confirm or "").strip() != server:
        return ApiError(400, "confirm_required",
                        f"{action} acts on the production of {server}: give confirm={server!r}, "
                        "once the token's owner has agreed to it")
    return None


def machine_refusal(scoped: bool, role: str, ci: bool, action: str, confirm: str | None = None,
                    name: str = "") -> ApiError | None:
    """Why a token may not hand a workspace over, receive one, switch one, move an instance or bring it back
    (AD-008 §9 step 10; None: it may). They act on productions, the machine's: a token of the machine (never a
    workspace's), never a CI's, with the admin role; and a write (confirm not None) with confirm = name, the name
    typed on the pages' dialogs."""
    if scoped:
        return ApiError(403, "machine_token_required",
                        "The handover of a workspace and the move of an instance are for a token of the machine: "
                        "a workspace's token never reaches them")
    if ci:
        return ApiError(403, "production_by_hand",
                        f"{action} acts on productions, by hand only: a CI token never does it")
    if not can(role, "admin"):
        return ApiError(403, "production_level_required",
                        f"{action} needs a token with the production level (role admin); this one is {role}")
    if confirm is not None and (confirm or "").strip() != name:
        return ApiError(400, "confirm_required",
                        f"{action}: give confirm={name!r}, once the token's owner has agreed to it")
    return None


class RateLimiter:
    """The requests of each token in the last WINDOW seconds, reads and
    writes apart."""

    def __init__(self, reads: int = READ_PER_MINUTE, writes: int = WRITE_PER_MINUTE, window: int = WINDOW):
        self.limits = {False: reads, True: writes}
        self.window = window
        self._seen: dict[tuple[str, bool], collections.deque] = {}
        self._lock = threading.Lock()

    def hit(self, name: str, write: bool, now: float | None = None) -> int:
        """0 when the request may go (it is counted), else the seconds to wait."""
        now = time.monotonic() if now is None else now
        with self._lock:
            seen = self._seen.setdefault((name, write), collections.deque())
            while seen and seen[0] <= now - self.window:
                seen.popleft()
            if len(seen) >= self.limits[write]:
                return max(1, int(seen[0] + self.window - now + 0.999))
            seen.append(now)
            return 0

    def refusal(self, name: str, write: bool) -> ApiError | None:
        wait = self.hit(name, write)
        if not wait:
            return None
        kind = "writes" if write else "reads"
        return ApiError(429, "rate_limited",
                        f"A token makes at most {self.limits[write]} {kind} in {self.window} seconds: "
                        f"try again in {wait} s", {"Retry-After": str(wait)})


def fingerprint(method: str, path: str, body) -> str:
    """The SHA-256 of a request: its method, its path and its body (JSON:
    in a canonical form, so spaces and the order of the keys do not count)."""
    if isinstance(body, (bytes, bytearray)):
        try:
            body = json.loads(body or b"null")
        except ValueError:
            body = body.decode("utf-8", "replace")
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{method}\0{path}\0{text}".encode()).hexdigest()


class Idempotency:
    """The answers given to idempotency keys, KEEP_HOURS each, in a file of
    their own (600) so a restart keeps them."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._entries: dict | None = None
        self._locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def check_key(key: str) -> ApiError | None:
        if not IDEMPOTENCY_KEY.fullmatch(key or ""):
            return ApiError(400, "invalid_idempotency_key",
                            "An Idempotency-Key is 1-200 printable ASCII characters, no spaces")
        return None

    def _load(self) -> dict:
        if self._entries is None:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            self._entries = data if isinstance(data, dict) else {}
        cutoff = time.time() - KEEP_HOURS * 3600
        for slot in [s for s, e in self._entries.items() if not isinstance(e, dict) or e.get("at", 0) < cutoff]:
            del self._entries[slot]
        return self._entries

    def _save(self) -> None:
        entries = self._entries or {}
        while len(entries) > KEEP_ENTRIES:
            del entries[min(entries, key=lambda s: entries[s].get("at", 0))]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(entries, fh)
        os.replace(tmp, self.path)

    @staticmethod
    def _slot(token: str, key: str) -> str:
        return hashlib.sha256(f"{token}\0{key}".encode()).hexdigest()

    def lock(self, token: str, key: str) -> asyncio.Lock:
        """One request at a time for a key: a retry sent while the first is
        still answered waits for it, and gets its answer."""
        return self._locks.setdefault(self._slot(token, key), asyncio.Lock())

    def lookup(self, token: str, key: str, print_: str) -> dict | None:
        """The answer of before ({"status", "body"}), None when there is none;
        ApiError 409 when the key came with another request."""
        entry = self._load().get(self._slot(token, key))
        if entry is None:
            return None
        if entry.get("print") != print_:
            raise ApiError(409, "idempotency_conflict",
                           "This Idempotency-Key was used in the last 24 hours with another request: "
                           "use a new key for a new request")
        return {"status": entry.get("status", 200), "body": entry.get("body")}

    def remember(self, token: str, key: str, print_: str, status: int, body) -> None:
        self._load()[self._slot(token, key)] = {"print": print_, "status": status, "body": body, "at": time.time()}
        try:
            self._save()
        except OSError:
            # kept in memory: a restart forgets it, the answer is given anyway
            pass
        lock = self._locks.get(self._slot(token, key))
        if lock is not None and not lock.locked():
            self._locks.pop(self._slot(token, key), None)


class Guard:
    """The rate limit and the idempotency keys, one of each for the REST API
    and MCP together."""

    def __init__(self, folder: Path):
        self.limiter = RateLimiter()
        self.idempotency = Idempotency(Path(folder) / FILE)
