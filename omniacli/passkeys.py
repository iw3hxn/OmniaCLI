# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Passkeys (WebAuthn Level 2, README Passkeys): a key pair the person's
device keeps (a laptop's or a phone's authenticator, a security key), used
with its PIN or fingerprint. It logs in without a password (a discoverable
credential: the browser offers the passkeys it has for this site) and it is
a second step after the password, in the place of the code of an app.

The ceremonies are py_webauthn's (the `webauthn` package of the Duo Labs
project, 3.x): the options, the checks of a registration and of an
assertion (the challenge, the origin, the RP ID's hash, the user's presence
and verification, the signature, the counter that must grow). Attestation
is not asked ("none"): what kind of device it is does not matter here, only
that the same one comes back. The library needs Python 3.10, like MCP: an
older Python runs without passkeys (available() is False, the pages do
not offer them).

The RP ID is the name the browser opened OmniaCLI on (its Host without the
port): the panel's name (or localhost through an SSH tunnel) and the
customers' portal are two RP IDs, and a passkey is of the one it was made
on, listed with it. An IP address is no RP ID (WebAuthn wants a name), and
the browser offers passkeys over HTTPS (or on localhost) only.

Kept in FILE next to the users file (mode 600): for each user a random
handle (the WebAuthn user.id: not the name) and its passkeys, each with its
credential id, its COSE public key, its counter, the name the person gave
it, its RP ID, when it was made and last used. A ceremony's challenge lives
in memory only, for CEREMONY_SECONDS and once (Ceremonies).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path

try:
    import webauthn
    from webauthn.helpers import options_to_json
    from webauthn.helpers.exceptions import WebAuthnException
    from webauthn.helpers.structs import (
        AttestationConveyancePreference,
        AuthenticatorSelectionCriteria,
        PublicKeyCredentialDescriptor,
        ResidentKeyRequirement,
        UserVerificationRequirement,
    )
except ImportError:  # why: py_webauthn needs Python 3.10; an older one runs without passkeys, as without MCP
    webauthn = None

    class WebAuthnException(Exception):  # noqa: N818 (the library's name)
        pass

FILE = "passkeys.json"
CEREMONY_SECONDS = 5 * 60
TIMEOUT_MS = 120_000  # what the browser waits for the person
MAX_PER_USER = 10
NAME_MAX = 40
RP_NAME = "OmniaCLI"
COOKIE = "omniacli_passkey"
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


def available() -> bool:
    return webauthn is not None


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def rp_of(host_header: str) -> str | None:
    """The RP ID of a request's Host ("name:port"): its name; None for an
    address (WebAuthn wants a name) or no host."""
    host = (host_header or "").strip().lower()
    if host.startswith("["):
        return None
    name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    if not name:
        return None
    try:
        ipaddress.ip_address(name)
        return None
    except ValueError:
        return name


def origins_of(host_header: str) -> list[str]:
    """The origins a ceremony may come from on this Host: https, and http
    for localhost (the only plain-HTTP origin a browser runs WebAuthn on)."""
    host = (host_header or "").strip().lower()
    found = [f"https://{host}"]
    if rp_of(host) == "localhost":
        found.append(f"http://{host}")
    return found


def clean_name(value) -> str:
    text = " ".join(str(value or "").split())
    return "".join(c for c in text if c.isprintable())[:NAME_MAX]


class PasskeyStore:
    """The users' passkeys (the file of the module's docstring)."""

    def __init__(self, path: Path, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self._lock = threading.Lock()
        self._users: dict[str, dict] = {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._users = {u: v for u, v in data.items() if isinstance(v, dict) and isinstance(v.get("keys"), list)}
        except (OSError, ValueError):
            pass  # none yet

    def _save(self) -> None:
        """The caller holds the lock; an error is raised (a counter must not go back)."""
        self._users = {u: v for u, v in self._users.items() if v.get("keys")}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self._users, fh, indent=1, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def handle(self, user: str) -> bytes:
        """The WebAuthn user.id of user (random, made once; kept with its first passkey)."""
        found = self._users.get(user)
        if found and found.get("handle"):
            return unb64(found["handle"])
        return secrets.token_bytes(32)

    def keys(self, user: str, rp: str | None = None) -> list[dict]:
        """user's passkeys (of rp only when given), the newest first."""
        found = (self._users.get(user) or {}).get("keys") or []
        return sorted((dict(k) for k in found if rp is None or k.get("rp") == rp),
                      key=lambda k: -(k.get("created") or 0))

    def has(self, user: str, rp: str | None) -> bool:
        return bool(rp) and any(k.get("rp") == rp for k in (self._users.get(user) or {}).get("keys") or [])

    def count(self, user: str) -> int:
        return len((self._users.get(user) or {}).get("keys") or [])

    def add(self, user: str, handle: bytes, rp: str, credential_id: bytes, public_key: bytes, sign_count: int,
            name: str, transports: list, backed_up: bool) -> dict:
        now = self.clock()
        key = {"id": b64(credential_id), "key": b64(public_key), "count": int(sign_count), "name": clean_name(name),
               "rp": rp, "created": now, "used": None, "transports": [str(t) for t in transports or []][:6],
               "synced": bool(backed_up)}
        with self._lock:
            found = self._users.setdefault(user, {"handle": b64(handle), "keys": []})
            found.setdefault("handle", b64(handle))
            if any(k["id"] == key["id"] for k in found["keys"]):
                raise ValueError("This passkey is registered already")
            found["keys"].append(key)
            self._save()
        return dict(key)

    def remove(self, user: str, key_id: str) -> dict | None:
        with self._lock:
            found = self._users.get(user) or {}
            gone = next((k for k in found.get("keys") or [] if k.get("id") == key_id), None)
            if gone is None:
                return None
            found["keys"] = [k for k in found["keys"] if k is not gone]
            self._save()
        return gone

    def drop_user(self, user: str) -> int:
        """Every passkey of user away (a reset by an admin): how many."""
        with self._lock:
            found = self._users.pop(user, None)
            if found is None:
                return 0
            self._save()
        return len(found.get("keys") or [])

    def keep(self, users) -> None:
        """Only the users that exist: a deleted user's go with it."""
        with self._lock:
            gone = [u for u in self._users if u not in users]
            for user in gone:
                del self._users[user]
            if gone:
                self._save()

    def find(self, credential_id: str, rp: str) -> tuple[str, dict] | None:
        """(user, passkey) of a credential id made on rp."""
        for user, found in self._users.items():
            for key in found.get("keys") or []:
                if hmac.compare_digest(str(key.get("id", "")), credential_id) and key.get("rp") == rp:
                    return user, dict(key)
        return None

    def owner(self, handle: bytes) -> str | None:
        """The user of a WebAuthn user.id."""
        wanted = b64(handle)
        return next((u for u, f in self._users.items() if hmac.compare_digest(str(f.get("handle", "")), wanted)), None)

    def used(self, user: str, key_id: str, sign_count: int) -> None:
        with self._lock:
            for key in (self._users.get(user) or {}).get("keys") or []:
                if key.get("id") == key_id:
                    key["count"], key["used"] = int(sign_count), self.clock()
                    self._save()
                    return


@dataclass
class Ceremony:
    """A challenge given to a browser: for what (register, login, second),
    whose (register, second: the user), on which RP ID and Host."""

    kind: str
    user: str
    rp: str
    host: str
    challenge: bytes
    expires: float
    name: str = ""
    handle: bytes = b""


class Ceremonies:
    """The challenges given, in memory, each taken once."""

    def __init__(self):
        self._lock = threading.Lock()
        self._found: dict[str, Ceremony] = {}

    def start(self, kind: str, user: str, rp: str, host: str, name: str = "", handle: bytes = b"") -> tuple[str, bytes]:
        """(the token for the browser's cookie, the challenge)."""
        token, challenge, now = secrets.token_urlsafe(32), secrets.token_bytes(32), time.time()
        with self._lock:
            self._found = {k: c for k, c in self._found.items() if c.expires > now}
            if len(self._found) > 5000:  # a flood of ceremonies never finished: the oldest go
                for k in sorted(self._found, key=lambda k: self._found[k].expires)[:1000]:
                    del self._found[k]
            self._found[_digest(token)] = Ceremony(kind, user, rp, host, challenge, now + CEREMONY_SECONDS, name, handle)
        return token, challenge

    def take(self, token: str | None, kind: str) -> Ceremony | None:
        """The live ceremony of token, of kind, taken out (once)."""
        if not token:
            return None
        with self._lock:
            found = self._found.pop(_digest(token), None)
        if found is None or found.kind != kind or found.expires <= time.time():
            return None
        return found


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def registration_options(rp: str, user: str, handle: bytes, challenge: bytes, exclude: list[dict]) -> dict:
    """What navigator.credentials.create() is given (as JSON): a
    discoverable credential (resident key) with the user's verification."""
    options = webauthn.generate_registration_options(
        rp_id=rp, rp_name=RP_NAME, user_name=user, user_id=handle, user_display_name=user, challenge=challenge,
        timeout=TIMEOUT_MS, attestation=AttestationConveyancePreference.NONE,
        authenticator_selection=AuthenticatorSelectionCriteria(resident_key=ResidentKeyRequirement.REQUIRED,
                                                               user_verification=UserVerificationRequirement.REQUIRED),
        exclude_credentials=[PublicKeyCredentialDescriptor(id=unb64(k["id"])) for k in exclude])
    return json.loads(options_to_json(options))


def authentication_options(rp: str, challenge: bytes, allow: list[dict] | None, verification: str) -> dict:
    """What navigator.credentials.get() is given: allow None, any passkey
    of this site (the browser asks which: the login without a password);
    else the user's (the second step)."""
    options = webauthn.generate_authentication_options(
        rp_id=rp, challenge=challenge, timeout=TIMEOUT_MS,
        allow_credentials=[PublicKeyCredentialDescriptor(id=unb64(k["id"])) for k in allow or []],
        user_verification=UserVerificationRequirement(verification))
    return json.loads(options_to_json(options))


def verify_registration(credential: dict, ceremony: Ceremony):
    """The library's VerifiedRegistration; WebAuthnException when it is not one."""
    return webauthn.verify_registration_response(
        credential=credential, expected_challenge=ceremony.challenge, expected_rp_id=ceremony.rp,
        expected_origin=origins_of(ceremony.host), require_user_verification=True)


def verify_assertion(credential: dict, ceremony: Ceremony, key: dict, verification: bool):
    """The library's VerifiedAuthentication (new_sign_count); WebAuthnException when it is not one."""
    return webauthn.verify_authentication_response(
        credential=credential, expected_challenge=ceremony.challenge, expected_rp_id=ceremony.rp,
        expected_origin=origins_of(ceremony.host), credential_public_key=unb64(key["key"]),
        credential_current_sign_count=int(key.get("count") or 0), require_user_verification=verification)
