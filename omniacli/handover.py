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

- **The switch** (step 8, AD-011): two phases, the provider coordinating. The customer's machine asks
  ``switch/begin`` with a ``switch_id`` of its own (every step after is idempotent on it); the provider writes the
  phase before it acts, puts its maintenance page up, stops Odoo, stops each database of its own clean (its last
  record) or dumps one of the machine's cluster, suspends the workspace's schedules, and reads «frozen» with a
  deadline (10 minutes at least). The customer takes the last round and the sealed secrets (``seal``: X25519 with
  the claim's key, HKDF-SHA256, AES-256-GCM, the id and the switch_id as associated data), starts its instances
  without their crons, held, and asks ``commit``; the provider passes the names on (forward full) and revokes the
  workspace's tokens; then the customer imports the users with their hashes and two-step secrets, the roles, the
  schedules, the destinations, the credentials, the deploys and the extract of the access log, and releases its
  names. A failure before the commit is ``abort``: everything runs at the provider's again. The deadline passed
  with neither, the provider aborts by itself, and a commit after reads 409."""

from __future__ import annotations

import base64
import binascii
import datetime
import hashlib
import hmac
import ipaddress
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
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

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


def epoch_of(text: str) -> float:
    """The instant of a moment iso wrote; 0 for none."""
    try:
        return datetime.datetime.fromisoformat(str(text)).timestamp()
    except ValueError:
        return 0.0


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


class NoIPv4(OSError):
    """The provider's panel has no IPv4 address this machine reaches."""


NO_IPV4 = ("The panel of the provider has no IPv4 address: a handover goes over IPv4, since the database is let in "
           "for the one IPv4 address this machine calls from")


def connect_v4(host: str, port: int, timeout: float) -> socket.socket:
    """A connection to host over IPv4 only (AD-008): the address the provider reads from the customer's HTTPS calls is
    the IPv4 one, the one its database copy leaves from too, never an IPv6 one its firewall cannot let in."""
    try:
        found = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    except socket.gaierror:
        raise NoIPv4(NO_IPV4) from None
    last: OSError = NoIPv4(NO_IPV4)
    for family, kind, proto, _name, address in found:
        sock = socket.socket(family, kind, proto)
        sock.settimeout(timeout)
        try:
            sock.connect(address)
            return sock
        except OSError as exc:
            sock.close()
            last = exc
    raise last


def ipv4_of(address: str) -> str:
    """The IPv4 address a request came from ("::ffff:a.b.c.d" as a.b.c.d), "" for an IPv6 one."""
    try:
        found = ipaddress.ip_address(str(address or ""))
    except ValueError:
        return ""
    if found.version == 6 and found.ipv4_mapped is not None:
        return str(found.ipv4_mapped)
    return str(found) if found.version == 4 else ""


PIN_REFUSED = "The certificate of the provider is not the one of the code: ask the provider for a new code"


def verified_pin(url: str, timeout: float = 10.0) -> str:
    """The pin of the certificate the panel at url presents now, verified against this machine's CAs and for its
    name: ssl.SSLCertVerificationError for one this machine does not trust, OSError when it does not answer."""
    parts = urlsplit(url)
    host, port = parts.hostname or "", parts.port or 443
    with connect_v4(host, port, timeout) as raw:
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
    # over IPv4 only (connect_v4): the provider lets the database in for the address these calls come from
    transport = httpx.AsyncHTTPTransport(verify=ssl.create_default_context(), local_address="0.0.0.0")
    return httpx.AsyncClient(base_url=url, transport=transport, timeout=timeout or httpx.Timeout(30.0, connect=10.0),
                             event_hooks={"response": [pinned]})


def reaches(ip: str, port: int, timeout: float = 3) -> bool:
    """Whether a connection to ip:port opens (it sends nothing): the provider trying the customer's machine from
    outside, on the address its request came from (UX-014 A5 step 2, A8)."""
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


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


# the codes and sessions a Redo retired, the newest kept as their hashes: the customer's machine that still holds one
# reads why (410 «made anew») and is never counted as a failed try (its look every minute would block its address)
RETIRED_MOST = 20


def retire(state: dict) -> dict:
    """The lists of retired hashes once a Redo makes the code and the session of before stop holding."""
    out = {}
    for field, now_key, was_key in (("old_codes", "code_hash", "was_code_hash"),
                                    ("old_sessions", "session_hash", "was_session_hash")):
        found = [str(h) for h in (state.get(field) or []) if h]
        found += [str(state[k]) for k in (now_key, was_key) if state.get(k) and str(state[k]) not in found]
        out[field] = found[-RETIRED_MOST:]
    return out


def retired(state: dict, field: str, secret: str) -> bool:
    """Whether secret is a code (field old_codes) or a session (old_sessions) a Redo retired."""
    return any(same(str(h), secret) for h in (state.get(field) or []))


def phase_of(state: dict, at: float | None = None) -> str:
    """The provider's word for a handover: prepared (code not used yet), receiving, ready, cancelled, expired;
    switching and committed (the switch: it closes by its own deadline, past the code's 72 hours, AD-008)."""
    if not state:
        return ""
    phase = str(state.get("phase") or "prepared")
    if phase in ("cancelled", "expired", "switching", "committed"):
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


# --- the switch (step 8 of §9, AD-011) -----------------------------------------------------------------------

# the provider waits for a commit at least this long after its instances stopped, else it aborts by itself
SWITCH_MOST = 600
# what a switch_id looks like (made by the customer's machine)
SWITCH_ID = re.compile(r"[0-9a-f]{16}")
# the switch's phases at the provider's while it may still go either way: stopping, stopped and waiting
SWITCH_OPEN = ("freezing", "frozen")
_SEAL_INFO = b"omniacli-handover-1 secrets"


def deadline_of(estimate_s: float, at: float | None = None) -> float:
    """The moment the provider aborts a switch by itself: max(10 minutes, 3 × the restores estimated)."""
    return (at if at is not None else now()) + max(SWITCH_MOST, 3 * float(estimate_s or 0))


def sealed_aad(handover_id: str, switch_id: str) -> bytes:
    return f"omniacli-handover:{handover_id}:{switch_id}".encode()


def _seal_key(shared: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_SEAL_INFO).derive(shared)


def seal(data: bytes, public: str, aad: bytes) -> dict:
    """data sealed to the X25519 public key of a claim (AD-009): an ephemeral key of this machine's, HKDF-SHA256,
    AES-256-GCM with aad as associated data. Nothing of it is written here."""
    ephemeral = X25519PrivateKey.generate()
    key = _seal_key(ephemeral.exchange(X25519PublicKey.from_public_bytes(_unb64(public_key(public)))))
    nonce = secrets.token_bytes(12)
    return {"format": 1, "epk": _b64(ephemeral.public_key().public_bytes(serialization.Encoding.Raw,
                                                                        serialization.PublicFormat.Raw)),
            "nonce": _b64(nonce), "data": _b64(AESGCM(key).encrypt(nonce, data, aad))}


def unseal(private_raw: bytes, sealed: dict, aad: bytes) -> bytes:
    """What seal sealed, with the claim's private key; CodeError for anything else (another key, another
    handover or switch, a byte changed)."""
    try:
        private = X25519PrivateKey.from_private_bytes(private_raw)
        key = _seal_key(private.exchange(X25519PublicKey.from_public_bytes(_unb64(str(sealed["epk"])))))
        return AESGCM(key).decrypt(_unb64(str(sealed["nonce"])), _unb64(str(sealed["data"])), aad)
    except (KeyError, TypeError, ValueError, CodeError, InvalidTag) as exc:
        raise CodeError(f"the secrets do not open with this machine's key ({type(exc).__name__})") from None


def server_renamed(server: str, source: str, workspace: str) -> str:
    """A server <source>-<instance> of the provider's workspace as the customer's <workspace>-<instance>."""
    return f"{workspace}-{server[len(source) + 1:]}" if server.startswith(f"{source}-") else server


def names_map(wanted, taken, workspace: str) -> dict[str, str]:
    """{name there: name here} of users (or destinations, schedules) that arrive: a name taken here arrives as
    <name>.<workspace> (Carlo, 23/11/2026), and -2, -3… after it when that one is taken too."""
    out: dict[str, str] = {}
    used = set(taken)
    for name in wanted:
        new = user_name(name, workspace, used)
        n = 2
        while new in used:
            new = f"{name}.{workspace}-{n}"
            n += 1
        out[name] = new
        used.add(new)
    return out


def destination_names_map(wanted, taken, workspace: str) -> dict[str, str]:
    """{name there: name here} of the destinations that arrive: a name taken here arrives as <name>-<workspace> (a
    destination's name has no dot: 1-32 lower case letters, digits, _ and -), cut to 32, and -2, -3… after it."""
    out: dict[str, str] = {}
    used = set(taken)
    for name in wanted:
        new, n = name, 2
        if new in used:
            new = f"{name}-{workspace}"[:32].rstrip("-_")
        base = new
        while new in used:
            tail = f"-{n}"
            new = base[:32 - len(tail)].rstrip("-_") + tail
            n += 1
        out[name] = new
        used.add(new)
    return out


def cron_shifted(cron: str, hours: int) -> str:
    """A crontab expression whose hour is one number moved by hours: the same instant on a machine in another
    zone (UX-014 A11); any other hour (*/2, 1-5, a list) stays as it is."""
    parts = str(cron).split()
    if len(parts) != 5 or not hours or not parts[1].isdigit():
        return cron
    parts[1] = str((int(parts[1]) + hours) % 24)
    return " ".join(parts)


# the fields of an entry of the access log that pass in its extract (AD-009 «L'estratto del registro»)
EXTRACT_FIELDS = ("utc", "time", "event", "kind", "name", "on_behalf", "role", "ip", "method", "route", "server",
                  "env", "status", "job", "detail")


def log_extract(files, workspace: str, view, verified: dict, at: float | None = None) -> str:
    """The entries of the access log that are the workspace's (its own, or one of its), the oldest first, as
    JSONL: a head with the period and the result of the log's verify() at the extraction, then each entry with
    the redaction of customerview (the provider's people as the provider, their address left out, other machines
    as «another machine», folders as their last part) and orig, the SHA-256 of its line in the provider's log,
    which the provider can show is there. files: the log's files, the newest first (accesslog.numbered)."""
    rows = []
    for path in reversed(list(files)):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except OSError:
            continue
        for line in lines:
            line = line.rstrip("\n")
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict) or (entry.get("workspace") != workspace
                                               and workspace not in (entry.get("workspaces") or [])):
                continue
            out = {k: view.text(entry[k]) if isinstance(entry[k], str) else entry[k]
                   for k in EXTRACT_FIELDS if entry.get(k) not in (None, "", [])}
            name = str(entry.get("name") or "").strip()
            if name and view.who(name) != name:
                out["name"], out["kind"] = view.who(name), "provider"
                out.pop("ip", None)
                out.pop("on_behalf", None)
            out["orig"] = hashlib.sha256(line.encode("utf-8")).hexdigest()
            rows.append(out)
    head = {"extract": workspace, "provider": view.provider, "entries": len(rows),
            "from": rows[0].get("utc") if rows else None, "to": rows[-1].get("utc") if rows else None,
            "made": iso(at if at is not None else now()),
            "verify": {"ok": bool(verified.get("ok")), "entries": verified.get("entries"),
                       "breaks": len(verified.get("breaks") or [])}}
    return "\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True) for r in [head, *rows]) + "\n"
