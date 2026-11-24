# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Two-step verification of the login form (README, Two-step verification;
NIS 2, art. 21(2)(j) of Directive 2022/2555): after the password, a code of
an authenticator app or one of the user's recovery codes.

The codes are those of RFC 6238 (TOTP: HMAC-SHA-1, 30 seconds, 6 digits,
as the authenticator apps expect), with HOTP of RFC 4226 under them. A code
is taken from the step before to the step after (a phone's clock a little
off), and never twice: the step of the last code used is kept.

The secrets and the recovery codes live in one file (mode 600) next to the
users file, {user: {"secret", "since", "last", "salt", "recovery"}}: the
recovery codes only as their salted SHA-256 (random and long, like the
tokens), each struck out when used. A secret being set up is not there
until its first code is confirmed.

A login that passed its password and still owes its code is a Pending:
a random token in a cookie of the login's path, kept in memory only
(a restart asks the password again), for PENDING_SECONDS and MAX_TRIES.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

try:
    import segno
except ImportError:  # why: the QR code only saves typing; the secret as text sets an app up as well
    segno = None

STEP = 30  # seconds
DIGITS = 6
DIGEST = "sha1"
# the steps before and after the current one a code is still taken from
DRIFT = 1
RECOVERY_CODES = 10
ISSUER = "OmniaCLI"

# who must use it (Options › Customers' portal): nobody; who logs in at the
# portal's address, and the users of the workspaces anywhere once the machine
# has one; everybody
POLICIES = ("nobody", "portal", "everybody")
DEFAULT_POLICY = "portal"

STEP_COOKIE = "omniacli_step"
PENDING_SECONDS = 10 * 60
MAX_TRIES = 5

_ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"  # no l, o, 0, 1: read aloud or copied by hand


def new_secret() -> str:
    """160 random bits (RFC 4226's advice), in base32 without padding."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key(secret: str) -> bytes:
    clean = secret.replace(" ", "").upper()
    return base64.b32decode(clean + "=" * (-len(clean) % 8))


def hotp(key: bytes, counter: int, digits: int = DIGITS, digest: str = DIGEST) -> str:
    """RFC 4226: the code of counter."""
    mac = hmac.new(key, struct.pack(">Q", counter), digest).digest()
    offset = mac[-1] & 0x0F
    value = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10 ** digits).zfill(digits)


def totp(secret: str | bytes, at: float | None = None, digits: int = DIGITS, digest: str = DIGEST,
         step: int = STEP) -> str:
    """RFC 6238: the code of the moment at (now when None). secret: base32,
    or the key's bytes."""
    key = secret if isinstance(secret, bytes) else _key(secret)
    return hotp(key, int((time.time() if at is None else at) // step), digits, digest)


def code_step(secret: str, code: str, at: float | None = None, after: int = -1) -> int | None:
    """The step code belongs to, within DRIFT of at and later than after
    (the last one used); None when it is none of them."""
    code = "".join(c for c in str(code) if c.isdigit())
    if len(code) != DIGITS:
        return None
    key = _key(secret)
    now = int((time.time() if at is None else at) // STEP)
    found = None
    for counter in range(now - DRIFT, now + DRIFT + 1):
        # every step compared, so the time does not tell which one matched
        if hmac.compare_digest(hotp(key, counter), code) and counter > after:
            found = counter
    return found


def otpauth_uri(secret: str, account: str, issuer: str = ISSUER) -> str:
    """What the QR code says: the key URI format of the authenticator apps."""
    label = quote(f"{issuer}:{account}", safe=":@")
    return (f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}"
            f"&algorithm=SHA1&digits={DIGITS}&period={STEP}")


def qr_svg(text: str) -> str:
    """An inline SVG of the QR code of text, drawn here (no service on the
    network); "" without the segno library."""
    if segno is None:
        return ""
    return segno.make(text, error="m").svg_inline(scale=5, border=4, dark="#111827", light="#ffffff")


def new_recovery_codes(count: int = RECOVERY_CODES) -> list[str]:
    """count codes of 10 characters (50 bits), xxxxx-xxxxx."""
    codes = []
    for _ in range(count):
        raw = "".join(secrets.choice(_ALPHABET) for _ in range(10))
        codes.append(f"{raw[:5]}-{raw[5:]}")
    return codes


def _normal(code: str) -> str:
    return "".join(c for c in str(code).lower() if c.isalnum())


def _recovery_hash(salt: str, code: str) -> str:
    return hashlib.sha256(f"{salt}\0{_normal(code)}".encode()).hexdigest()


def looks_like_recovery(code: str) -> bool:
    return len(_normal(code)) == 10 and not _normal(code).isdigit()


class SecondFactors:
    """The users' second factors (the file of the module's docstring)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._users: dict[str, dict] = {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._users = {k: v for k, v in data.items() if isinstance(v, dict) and v.get("secret")}
        except (OSError, ValueError):
            pass  # none yet: nobody has it on

    def _save(self) -> None:
        """The caller holds the lock; an error is raised: a code struck out
        must stay struck out, a switch must not look made when it is not."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self._users, fh, indent=1, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def on(self, user: str) -> bool:
        return user in self._users

    def state(self, user: str) -> dict:
        """{"on", "since" (epoch), "left" (recovery codes)}."""
        found = self._users.get(user)
        if not found:
            return {"on": False, "since": None, "left": 0}
        return {"on": True, "since": found.get("since"), "left": len(found.get("recovery") or [])}

    def enable(self, user: str, secret: str, used_step: int) -> list[str]:
        """Switched on with secret, whose code of used_step was just
        confirmed: the recovery codes, to be shown once."""
        codes = new_recovery_codes()
        salt = secrets.token_hex(16)
        with self._lock:
            self._users[user] = {"secret": secret, "since": int(time.time()), "last": used_step, "salt": salt,
                                 "recovery": [_recovery_hash(salt, c) for c in codes]}
            self._save()
        return codes

    def record(self, user: str) -> dict | None:
        """user's second factor as it is kept (its secret, the step of its last code, its salted recovery codes),
        for the handover of its workspace (AD-009: it passes sealed); None when it has none."""
        found = self._users.get(user)
        return json.loads(json.dumps(found)) if found else None

    def adopt(self, user: str, record: dict) -> None:
        """user's second factor as another machine kept it (AD-009): the same app, the same recovery codes, and
        the step of its last code, so that a code used there is not used again here."""
        if not isinstance(record, dict) or not isinstance(record.get("secret"), str) or not record["secret"]:
            raise ValueError("a second factor has a secret")
        kept = {"secret": record["secret"], "since": int(record.get("since") or time.time()),
                "last": int(record.get("last", -1)), "salt": str(record.get("salt") or ""),
                "recovery": [str(h) for h in record.get("recovery") or [] if isinstance(h, str)]}
        with self._lock:
            self._users[user] = kept
            self._save()

    def disable(self, user: str) -> bool:
        """Off (switched off, or reset by an admin); False when it was not on."""
        with self._lock:
            if self._users.pop(user, None) is None:
                return False
            self._save()
        return True

    def keep(self, users) -> None:
        """Only the users that exist: a deleted user's goes with it."""
        with self._lock:
            gone = [u for u in self._users if u not in users]
            for user in gone:
                del self._users[user]
            if gone:
                self._save()

    def check(self, user: str, code: str, at: float | None = None) -> tuple[str, int] | None:
        """("code", left) for a code of the app, ("recovery", left) for a
        recovery code, now struck out; None: neither (or used already)."""
        with self._lock:
            found = self._users.get(user)
            if not found:
                return None
            left = found.get("recovery") or []
            if looks_like_recovery(code):
                hashed = _recovery_hash(found.get("salt", ""), code)
                match = next((h for h in left if hmac.compare_digest(h, hashed)), None)
                if match is None:
                    return None
                found["recovery"] = [h for h in left if h != match]
                self._save()
                return "recovery", len(found["recovery"])
            step = code_step(found["secret"], code, at, int(found.get("last", -1)))
            if step is None:
                return None
            found["last"] = step
            self._save()
            return "code", len(left)


@dataclass
class Pending:
    """A login whose password was right and whose code is still owed;
    setup: the user must switch it on first (secret: the one shown)."""

    user: str
    next_path: str
    password: str  # a fingerprint of the user's password hash: changed, the login starts again
    expires: float
    setup: bool = False
    secret: str = ""
    tries: int = 0
    # how the password was given: "login form" or "invitation"
    via: str = "login form"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class PendingLogins:
    """The Pending of each step cookie, in memory."""

    def __init__(self):
        self._lock = threading.Lock()
        self._found: dict[str, Pending] = {}

    def create(self, user: str, next_path: str, password_hash: str, setup: bool, via: str = "login form") -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            self._found = {k: p for k, p in self._found.items() if p.expires > now}
            self._found[_digest(token)] = Pending(user, next_path, _digest(password_hash)[:32], now + PENDING_SECONDS,
                                                  setup, new_secret() if setup else "", via=via)
        return token

    def get(self, token: str | None, users: dict[str, str]) -> Pending | None:
        """The live Pending of token: None when it ended, was used up, or the
        user's password changed or the user is gone."""
        if not token:
            return None
        found = self._found.get(_digest(token))
        if found is None or found.expires <= time.time() or found.tries >= MAX_TRIES:
            return None
        current = users.get(found.user)
        if current is None or not hmac.compare_digest(_digest(current)[:32], found.password):
            return None
        return found

    def failed(self, token: str) -> int:
        """One wrong code more: the tries left (0: the password again)."""
        with self._lock:
            found = self._found.get(_digest(token))
            if found is None:
                return 0
            found.tries += 1
            if found.tries >= MAX_TRIES:
                del self._found[_digest(token)]
                return 0
            return MAX_TRIES - found.tries

    def drop(self, token: str | None) -> None:
        if token:
            with self._lock:
                self._found.pop(_digest(token), None)
