# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Browser sessions after the login form, and the limit on failed logins.

A session is a random token in an HttpOnly cookie; the server keeps only its
SHA-256, with the user, the end of its LIFETIME and a fingerprint of the
user's password hash: a changed password or a deleted user ends every
session of that user at once. The sessions are kept in a file (mode 600), so
that a restart of OmniaCLI does not log everybody out.

Each session also keeps what its person reads on Account (README, Login
and security, Sessions): the browser's User-Agent (cut to UA_MAX), the
address of its last request, when it began, its last activity (written at
most every SEEN_EVERY: a request does not rewrite the file) and how it was
opened (the password, a passkey, a provider's single sign-on). A session is
named on the pages by sid(): a digest of the digest kept, which opens
nothing; ending one is immediate, its next request is refused.

Failed logins are counted per address and per user name: from the
LIMIT-th failure within WINDOW, the next try must wait, longer each time.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

COOKIE = "omniacli_session"
LIFETIME = 8 * 3600  # seconds from the login, whatever is done meanwhile

LIMIT = 5  # failed logins within WINDOW before the waits start
WINDOW = 15 * 60
FIRST_WAIT = 30  # seconds, doubled at every further failure
MAX_WAIT = 15 * 60

UA_MAX = 300  # characters of a User-Agent kept
SEEN_EVERY = 60  # seconds: the last activity written at most this often


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def sid(key: str) -> str:
    """The name of a session on the pages, from the digest kept (its token never leaves the cookie)."""
    return hashlib.sha256(f"sid\0{key}".encode()).hexdigest()[:16]


# the browser and the system of a User-Agent, the first that matches (Edge and Opera say Chrome too)
_BROWSERS = (("Edge", r"\bEdg(?:e|A|iOS)?/"), ("Opera", r"\bOPR/|\bOpera\b"), ("Samsung Internet", r"SamsungBrowser/"),
             ("Firefox", r"\bFirefox/|\bFxiOS/"), ("Chrome", r"Chrome/|\bCriOS/|\bChromium/"),
             ("Safari", r"\bSafari/"), ("curl", r"^curl/"), ("Python", r"python-|httpx/|aiohttp/"))
_SYSTEMS = (("Android", r"Android"), ("iOS", r"iPhone|iPad|iPod"), ("ChromeOS", r"\bCrOS\b"),
            ("Windows", r"Windows"), ("macOS", r"Mac OS X|Macintosh"), ("Linux", r"Linux|X11"))


def device(ua: str) -> tuple[str, str]:
    """(browser, system) of a User-Agent, "" for what it does not say."""
    browser = next((name for name, pattern in _BROWSERS if re.search(pattern, ua or "")), "")
    system = next((name for name, pattern in _SYSTEMS if re.search(pattern, ua or "")), "")
    return browser, system


class SessionStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._sessions: dict[str, dict] = {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._sessions = {k: v for k, v in data.items() if isinstance(v, dict)}
        except (OSError, ValueError):
            pass  # none yet, or unreadable: everybody logs in again

    def _save(self) -> None:
        """Write the live sessions; the caller holds the lock."""
        now = time.time()
        self._sessions = {k: v for k, v in self._sessions.items() if v.get("expires", 0) > now}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._sessions, fh)
            os.replace(tmp, self.path)
        except OSError:
            pass  # the sessions still work until a restart

    def create(self, user: str, password_hash: str, ua: str = "", ip: str = "", how: str = "password") -> str:
        """A new session of user: the token for the cookie. ua, ip: the
        browser's; how: password, passkey or sso:<provider>."""
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            self._sessions[_digest(token)] = {
                "user": user, "expires": now + LIFETIME, "pw": _digest(password_hash)[:32],
                "ua": (ua or "")[:UA_MAX], "ip": ip, "since": now, "seen": now, "how": how,
            }
            self._save()
        return token

    def seen(self, token: str | None, ip: str = "") -> None:
        """A request of the session of token: its last activity and address,
        in memory; written to the file at most every SEEN_EVERY."""
        if not token:
            return
        now = time.time()
        with self._lock:
            found = self._sessions.get(_digest(token))
            if found is None:
                return
            if ip:
                found["ip"] = ip
            found["now"] = now
            if now - found.get("seen", 0) >= SEEN_EVERY:
                found["seen"] = now
                self._save()

    def listed(self, user: str, current: str | None = None) -> list[dict]:
        """The live sessions of user, this browser's (current: its token)
        first, then the newest: {"id", "browser", "system", "ua", "ip",
        "since", "seen", "expires", "how", "current"}."""
        now = time.time()
        mine = _digest(current) if current else None
        rows = []
        for key, found in list(self._sessions.items()):
            if found.get("user") != user or found.get("expires", 0) <= now:
                continue
            browser, system = device(found.get("ua", ""))
            rows.append({"id": sid(key), "browser": browser, "system": system, "ua": found.get("ua", ""),
                         # a session of before the list (no "since"): its start from its end
                         "ip": found.get("ip", ""), "since": found.get("since") or found.get("expires", 0) - LIFETIME,
                         "seen": max(found.get("seen") or 0, found.get("now") or 0) or None,
                         "expires": found.get("expires"), "how": found.get("how", ""), "current": key == mine})
        return sorted(rows, key=lambda r: (not r["current"], -(r["since"] or 0)))

    def end(self, user: str, session: str) -> dict | None:
        """End the session of user named session (its sid): what it was
        (browser, system, ip, since, how), None when it is none of user's."""
        with self._lock:
            key = next((k for k, v in self._sessions.items() if v.get("user") == user and sid(k) == session), None)
            if key is None:
                return None
            found = self._sessions.pop(key)
            self._save()
        browser, system = device(found.get("ua", ""))
        return {"id": session, "browser": browser, "system": system, "ip": found.get("ip", ""),
                "since": found.get("since"), "how": found.get("how", "")}

    def count(self) -> dict[str, int]:
        """{user: how many live sessions}."""
        now = time.time()
        counts: dict[str, int] = {}
        for found in list(self._sessions.values()):
            if found.get("expires", 0) > now:
                counts[found.get("user", "")] = counts.get(found.get("user", ""), 0) + 1
        return counts

    def user(self, token: str | None, users: dict[str, str]) -> str | None:
        """The user of a live session: None when it ended, its password
        changed or the user is gone."""
        if not token:
            return None
        found = self._sessions.get(_digest(token))
        if found is None or found.get("expires", 0) <= time.time():
            return None
        current = users.get(found.get("user", ""))
        if current is None or not secrets.compare_digest(_digest(current)[:32], found.get("pw", "")):
            return None
        return found["user"]

    def drop_user(self, user: str, keep: str | None = None) -> int:
        """End every session of user but the one of keep (its own token): a
        second step reset or switched off must not leave a session made
        without it, or with a code someone else may know. How many ended."""
        kept = _digest(keep) if keep else None
        with self._lock:
            gone = [k for k, v in self._sessions.items() if v.get("user") == user and k != kept]
            for k in gone:
                del self._sessions[k]
            if gone:
                self._save()
        return len(gone)

    def drop(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            if self._sessions.pop(_digest(token), None) is not None:
                self._save()


class LoginLimiter:
    """The failed logins of the last WINDOW, per address and per user name."""

    def __init__(self):
        self._failures: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> deque:
        times = self._failures[key]
        while times and now - times[0] > WINDOW:
            times.popleft()
        return times

    def wait(self, ip: str, user: str) -> int:
        """Seconds before the next try is allowed; 0: allowed now."""
        now = time.time()
        longest = 0.0
        with self._lock:
            for key in (f"ip:{ip}", f"user:{user.lower()}"):
                times = self._recent(key, now)
                if len(times) >= LIMIT:
                    pause = min(FIRST_WAIT * 2 ** (len(times) - LIMIT), MAX_WAIT)
                    longest = max(longest, times[-1] + pause - now)
        return max(0, int(longest + 0.999))

    def failed(self, ip: str, user: str) -> None:
        now = time.time()
        with self._lock:
            for key in (f"ip:{ip}", f"user:{user.lower()}"):
                self._recent(key, now).append(now)

    def succeeded(self, ip: str, user: str) -> None:
        with self._lock:
            self._failures.pop(f"ip:{ip}", None)
            self._failures.pop(f"user:{user.lower()}", None)
