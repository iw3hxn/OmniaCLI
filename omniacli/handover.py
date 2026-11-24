# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The handover of a workspace to the customer's own OmniaCLI (docs/adr/AD-008-consegna-e-spostamento.md,
AD-008…AD-011; UX-014 part A), up to «ready» (step 7 of its §9).

- **The code.** One line, ``omniacli-consegna-1.<base64url JSON>``: ``u`` the public address of the provider's
  panel (its portal), ``h`` the handover's id (16 hex), ``s`` 32 random bytes, ``p`` the SHA-256 of the public key
  (SPKI) of the certificate ``u`` presents today. The provider keeps ``sha256$`` of ``s`` only. 72 hours from its
  making; one machine (the first claim binds it to the X25519 key that machine made for it, and gives it a
  session, the credential of every request after); revoked by Cancel; made again by Redo (the same id, a new secret
  and 72 hours, the session of before gone: the machine that copied resumes from what it has).
- **The customer's side.** It calls the provider in HTTPS, never the other way: the system's CAs and the pin
  ``p`` both (a proxy that intercepts TLS is refused, and meant to be). It copies with treesync (pull) over
  ``/api/handover/v1/<id>/treesync/…`` and follows a database of its own (ArrivePlace) from the provider's
  published port, let in for the one address the customer called from.
- **Files.** ``handover/out/<workspace>/state.json`` (the provider's: the hashes, the choice, the claim, the
  phase) and ``handover/in/<id>/`` (the customer's: state.json, claim.key, session, parts/), in OmniaCLI's own
  /var/lib/omniacli, 600 in folders of 700. No data of the customer in them: the copies go to the workspace's disk.

What passes at the switch (users, two-step secrets, roles, schedules, destinations, credentials, the certificate,
the extract of the access log) is step 8: the sealed secrets travel then (AD-009)."""

from __future__ import annotations

import base64
import binascii
import datetime
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import socket
import ssl
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey

PREFIX = "omniacli-consegna-1."
# the header the code travels in, read only (offer, probe): never in a URL, which logs keep
CODE_HEADER = "X-OmniaCLI-Handover"
PROTOCOL = 1
# the code is valid this long from its making (AD-008)
CODE_HOURS = 72
# the copy of the provider stays this long after the switch (D-A3; step 9)
COPY_DAYS = 7
# a database of its own is «in step» at most this far behind, the files' last round at most this old (AD-011)
LAG_MOST = 60
ROUND_MOST = 600
# the failed tries of one address before 429 (AD-008: 10 in 10 minutes)
FAILED_MOST, FAILED_WINDOW = 10, 600
ID = re.compile(r"[0-9a-f]{16}")
_B64 = re.compile(r"[A-Za-z0-9_-]{43}")
_PIN = re.compile(r"[0-9a-f]{64}")
_KEY = re.compile(r"[A-Za-z0-9_-]{43}")
_HOST = re.compile(r"(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")
STATE = "state.json"
# the trees each instance passes (treesync.TREES): its code with the history, its filestore, its last backup
TREES = ("addons", "filestore", "backups")
# what does not pass, by name, with why (AD-009; UX-014 A9): the dialog's «does not pass»
NOT_PASSED = (
    ("stagings", "a staging is made again from its production in a minute, on the customer's machine"),
    ("passkeys", "a passkey is bound to the address of this panel: it is registered again there"),
    ("tokens", "the MCP and API tokens and the OAuth accesses point to this address: the customer makes them again"),
    ("sessions", "who is logged in here logs in there again"),
    ("invitations", "an invitation not accepted yet leads here"),
    ("replicas", "a copy of a database on another machine of the provider is deleted"),
    ("older backups", "only the last backup of each environment passes; the others are deleted with the copy here"),
    ("jobs", "the history of the jobs, this machine's access log and the load measured stay here"),
    ("brand", "the customer's panel is OmniaCLI as it installs it"),
)


class CodeError(ValueError):
    """A code that is not one: said as «the code is not valid», whatever is wrong in it."""


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        raise CodeError("not base64url") from None


def now() -> float:
    return time.time()


def iso(at: float) -> str:
    """A moment with this machine's offset: the other machine, in another zone, reads the same instant."""
    return datetime.datetime.fromtimestamp(at).astimezone().isoformat(timespec="seconds")


def moment(at: float) -> str:
    """A moment as a text says it to the other machine and to people, dd/mm/yyyy hh:mm, by this machine's clock."""
    return time.strftime("%d/%m/%Y %H:%M", time.localtime(at))


def new_id() -> str:
    return secrets.token_hex(8)


def new_secret() -> str:
    return _b64(secrets.token_bytes(32))


def hashed(secret: str) -> str:
    return "sha256$" + hashlib.sha256(secret.encode()).hexdigest()


def same(stored: str, secret: str) -> bool:
    return bool(stored) and hmac.compare_digest(stored, hashed(secret))


def make_code(url: str, handover_id: str, secret: str, pin: str) -> str:
    body = json.dumps({"u": url, "h": handover_id, "s": secret, "p": pin}, separators=(",", ":"), sort_keys=True)
    return PREFIX + _b64(body.encode())


def read_code(text) -> dict:
    """The parts of a code ({u, h, s, p}), checked; CodeError for anything else."""
    text = str(text or "").strip()
    if not text.startswith(PREFIX) or len(text) > 2048:
        raise CodeError("not a handover code")
    try:
        data = json.loads(_unb64(text[len(PREFIX):]))
    except (ValueError, UnicodeDecodeError):
        raise CodeError("not JSON") from None
    if not isinstance(data, dict) or set(data) != {"u", "h", "s", "p"}:
        raise CodeError("not the parts of a code")
    url, hid, secret, pin = (str(data[k]) for k in ("u", "h", "s", "p"))
    parts = urlsplit(url)
    if (parts.scheme != "https" or not parts.hostname or not _HOST.fullmatch(parts.hostname) or parts.username
            or parts.password or parts.query or parts.fragment or parts.path not in ("", "/")):
        raise CodeError("not the HTTPS address of a panel")
    if not ID.fullmatch(hid) or not _B64.fullmatch(secret) or not _PIN.fullmatch(pin):
        raise CodeError("not the parts of a code")
    return {"u": url.rstrip("/"), "h": hid, "s": secret, "p": pin}


def spki_pin(der: bytes) -> str:
    """The SHA-256 of the public key (SubjectPublicKeyInfo) of a certificate in DER: what ``p`` pins, which a
    renewal with the same key keeps."""
    key = x509.load_der_x509_certificate(der).public_key()
    return hashlib.sha256(key.public_bytes(serialization.Encoding.DER,
                                           serialization.PublicFormat.SubjectPublicKeyInfo)).hexdigest()


def presented_pin(url: str, timeout: float = 10.0) -> str:
    """The pin of the certificate the panel at url presents now (its name sent, nothing verified: it is this
    machine's own address, read to be pinned). OSError when it does not answer."""
    parts = urlsplit(url)
    host, port = parts.hostname or "", parts.port or 443
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=timeout) as raw:
        with context.wrap_socket(raw, server_hostname=host) as tls:
            der = tls.getpeercert(binary_form=True)
    if not der:
        raise OSError(f"{host} presented no certificate")
    return spki_pin(der)


class PinError(Exception):
    """The provider's certificate is not the one of the code (AD-008 consequence 1)."""


PIN_REFUSED = "The certificate of the provider is not the one of the code: ask the provider for a new code"


def verified_pin(url: str, timeout: float = 10.0) -> str:
    """The pin of the certificate the panel at url presents now, verified against this machine's CAs and for its
    name: ssl.SSLCertVerificationError for one this machine does not trust, OSError when it does not answer."""
    parts = urlsplit(url)
    host, port = parts.hostname or "", parts.port or 443
    with socket.create_connection((host, port), timeout=timeout) as raw:
        with ssl.create_default_context().wrap_socket(raw, server_hostname=host) as tls:
            der = tls.getpeercert(binary_form=True)
    return spki_pin(der)


def check_pin(url: str, pin: str, timeout: float = 10.0) -> None:
    """Before anything of the handover is sent (the code, the session): the certificate the provider presents is
    trusted here and its key is the one the code pins; a proxy that reads the connection is refused, and meant to
    be. PinError, ssl.SSLCertVerificationError or OSError otherwise."""
    if verified_pin(url, timeout) != pin:
        raise PinError(PIN_REFUSED)


def client(url: str, pin: str, timeout: httpx.Timeout | None = None) -> httpx.AsyncClient:
    """A client to the provider's panel at url: TLS verified against this machine's CAs, and every answer's
    certificate the one the code pins (the connection is checked before with check_pin). The tests give their
    own (an app of theirs)."""
    async def pinned(response: httpx.Response) -> None:
        stream = response.extensions.get("network_stream")
        tls = stream.get_extra_info("ssl_object") if stream is not None else None
        der = tls.getpeercert(binary_form=True) if tls is not None else None
        if not der or spki_pin(der) != pin:
            raise PinError(PIN_REFUSED)
    return httpx.AsyncClient(base_url=url, verify=ssl.create_default_context(),
                             timeout=timeout or httpx.Timeout(30.0, connect=10.0), event_hooks={"response": [pinned]})


def fingerprint(text: str) -> str:
    """A short print of a key or a pin to compare by phone: 4 groups of 4 hex."""
    digest = hashlib.sha256(text.encode()).hexdigest()[:16]
    return " ".join(digest[i:i + 4] for i in range(0, 16, 4))


def new_key() -> tuple[bytes, str]:
    """(the raw private X25519 key of a claim, its public key in base64url)."""
    private = X25519PrivateKey.generate()
    raw = private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                serialization.NoEncryption())
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return raw, _b64(public)


def public_key(text) -> str:
    """A claim's X25519 public key in base64url, checked (CodeError otherwise)."""
    text = str(text or "")
    if not _KEY.fullmatch(text):
        raise CodeError("not an X25519 public key")
    X25519PublicKey.from_public_bytes(_unb64(text))
    return text


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


class Out:
    """The provider's handovers: handover/out/<workspace>/state.json, one per workspace."""

    def __init__(self, base: Path):
        self.root = base / "handover" / "out"

    def state(self, workspace: str) -> dict:
        return _read(self.root / workspace / STATE)

    def save(self, workspace: str, state: dict) -> dict:
        _write(self.root / workspace / STATE, state)
        return state

    def update(self, workspace: str, **values) -> dict:
        return self.save(workspace, {**self.state(workspace), **values})

    def forget(self, workspace: str) -> None:
        folder = self.root / workspace
        (folder / STATE).unlink(missing_ok=True)
        try:
            folder.rmdir()
        except OSError:
            pass

    def all(self) -> dict[str, dict]:
        if not self.root.is_dir():
            return {}
        return {p.name: s for p in sorted(self.root.iterdir()) if p.is_dir() and (s := self.state(p.name))}

    def find(self, handover_id: str) -> tuple[str, dict] | None:
        """The workspace and state of a handover by its id (a provider has few: they are read)."""
        if not ID.fullmatch(str(handover_id or "")):
            return None
        for workspace, state in self.all().items():
            if state.get("id") == handover_id:
                return workspace, state
        return None


def phase_of(state: dict, at: float | None = None) -> str:
    """The provider's word for a handover: prepared (code not used yet), receiving, ready, cancelled, expired."""
    if not state:
        return ""
    phase = str(state.get("phase") or "prepared")
    if phase in ("cancelled", "expired"):
        return phase
    if float(state.get("expires") or 0) < (at if at is not None else now()):
        return "expired"
    return phase


class In:
    """The customer's handovers: handover/in/<id>/ (state.json, claim.key, the session, parts/)."""

    def __init__(self, base: Path):
        self.root = base / "handover" / "in"

    def folder(self, handover_id: str) -> Path:
        if not ID.fullmatch(str(handover_id or "")):
            raise CodeError("not the id of a handover")
        return self.root / handover_id

    def state(self, handover_id: str) -> dict:
        return _read(self.folder(handover_id) / STATE)

    def save(self, handover_id: str, state: dict) -> dict:
        _write(self.folder(handover_id) / STATE, state)
        return state

    def update(self, handover_id: str, **values) -> dict:
        return self.save(handover_id, {**self.state(handover_id), **values})

    def all(self) -> dict[str, dict]:
        if not self.root.is_dir():
            return {}
        return {p.name: s for p in sorted(self.root.iterdir()) if p.is_dir() and ID.fullmatch(p.name)
                and (s := self.state(p.name))}

    def of_workspace(self, workspace: str) -> tuple[str, dict] | None:
        for hid, state in self.all().items():
            if state.get("workspace") == workspace:
                return hid, state
        return None

    def put_secret(self, handover_id: str, name: str, value: bytes) -> None:
        path = self.folder(handover_id) / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as fh:
            fh.write(value)

    def get_secret(self, handover_id: str, name: str) -> bytes:
        try:
            return (self.folder(handover_id) / name).read_bytes()
        except OSError:
            return b""

    def parts(self, handover_id: str) -> Path:
        return self.folder(handover_id) / "parts"

    def forget(self, handover_id: str) -> None:
        """Its folder gone: the state, the claim's key, the session, the parts (what was copied is on the
        workspace's disk, which its cancel takes away)."""
        shutil.rmtree(self.folder(handover_id), ignore_errors=True)


class Tries:
    """The failed tries of each address (AD-008): more than FAILED_MOST in FAILED_WINDOW seconds, 429."""

    def __init__(self):
        self.seen: dict[str, list[float]] = {}

    def blocked(self, ip: str, at: float | None = None) -> bool:
        at = at if at is not None else now()
        found = [t for t in self.seen.get(ip, []) if t > at - FAILED_WINDOW]
        self.seen[ip] = found
        return len(found) >= FAILED_MOST

    def failed(self, ip: str, at: float | None = None) -> None:
        self.seen.setdefault(ip, []).append(at if at is not None else now())
        if len(self.seen) > 10000:
            # a scan from many addresses: the oldest forgotten
            for key in list(self.seen)[:5000]:
                del self.seen[key]


def received_name(wanted: str, taken) -> str:
    """The name a workspace arrives with: its own, else <name>-2, -3…"""
    if wanted not in taken:
        return wanted
    n = 2
    while f"{wanted}-{n}" in taken:
        n += 1
    return f"{wanted}-{n}"


def user_name(wanted: str, workspace: str, taken) -> str:
    """A user name the customer has already arrives as <name>.<workspace> (Carlo, 23/11/2026)."""
    return wanted if wanted not in taken else f"{wanted}.{workspace}"
