# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The protection of a central panel (AD-017 §17.4.3, slice 7): every night
its own state, sealed, on a backup destination; and the way back on a
fresh install (omniacli central-restore).

What is saved is what makes a panel open its machines again, nothing of
the machines themselves (INVENTORY): the links with their tokens, the SSH
keys of the links and the host keys pinned, what each machine said of its
agent_api and of its agent mode, the people of this panel with their
second factors, the options, the groups, the destinations (this backup's
own among them), the puller connections and their keys, the CA files of
the links, the access log (NIS 2: who did what from here) and the main
configuration, restored beside the fresh one.

The central encrypts without being able to decrypt. A backup is sealed to
an X25519 public key: each file a key of its own, from a new ephemeral key
pair and the public key (ECDH, HKDF-SHA256), and pullcrypto's AES-256-GCM
in chunks with it. The private key leaves the panel only sealed with the
recovery passphrase (scrypt, AES-256-GCM), and that sealed key travels in
the header of every backup: the file and the passphrase are all a restore
needs. The passphrase is made here, shown once, typed back by the admin to
confirm it was kept, and forgotten: the panel keeps the public key and the
sealed private key, which it cannot open. Whoever takes the panel (or the
destination) has the copies and not the passphrase.

The header (format, when, which panel, the public key, the sealed private
key, the ephemeral public key) is authenticated: its SHA-256 is in the
context the file's chunks are sealed with, so a byte changed anywhere, in
the header or in the body, and the backup does not open.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import datetime
import errno
import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import shutil
import socket
import struct
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import yaml
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from . import centralpages, destinations, identityimport, pullcrypto, remotehome
from .config import ConfigError, invitations_file_of, load_links
from .jobs import Job, JobFailed
from .schedules import rotation_kept

_logger = logging.getLogger(__name__)

# the setup of this panel's backup, beside the links file (mode 600)
FILE = "central-backup.json"
FORMAT = 2
MAGIC = b"OCCB\x02"
SUFFIX = ".occb"
# a backup's name there: its stamp as the destinations read it (destinations.STAMP)
PREFIX = "central_"
# the folder in the destination: this, then the panel's name
FOLDER = "omniacli-central"
# the rotation there: the newest of each of the last so many days, weeks, months
ROTATION = {"daily": 14, "weekly": 8, "monthly": 12}
# the time of the night it runs when none is chosen: before 02:00, where the clocks of Europe move (summer time)
DEFAULT_TIME = "01:30"
TIME = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
# the passphrase: 6 groups of 5 letters and digits that cannot be read one for another (148 bits)
ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
GROUPS, GROUP = 6, 5
# scrypt of the passphrase: 128 MiB, about half a second; a header that asks more than these is refused
SCRYPT_N, SCRYPT_R, SCRYPT_P = 1 << 17, 8, 1
_KEY_AAD = b"omniacli central-backup key v2\0"
_FILE_INFO = b"omniacli central-backup file v2"
_MADE = b"omniacli central-backup made v2\0"
# what the passphrase seals: the X25519 private key, then the key that signs who made a backup (HMAC-SHA256)
_PRIVATE, _MAC = 32, 32
_TAG = 32
# a header is small: refuse one larger before reading it
MAX_HEADER = 64 * 1024
# the archive inside: what each slot is (a file, a folder), as manifest.json lists it
MANIFEST = "manifest.json"


class CentralBackupError(Exception):
    """A backup or a passphrase that cannot be used; the message says why."""


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(text) -> bytes:
    if not isinstance(text, str):
        raise CentralBackupError("not a backup of a central panel: a field is not base64")
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CentralBackupError("not a backup of a central panel: a field is not base64") from exc


# --- the key and the passphrase -------------------------------------------------------------------------------

def new_passphrase() -> str:
    """A new recovery passphrase, in groups (abcde-fghjk-...)."""
    return "-".join("".join(secrets.choice(ALPHABET) for _ in range(GROUP)) for _ in range(GROUPS))


def normalized(passphrase: str) -> bytes:
    """What the key is derived from: the letters and digits, lower case (dashes and spaces as typed or not)."""
    return re.sub(r"[\s-]+", "", str(passphrase or "")).lower().encode("utf-8")


def _kek(passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    # never more than this version writes: a header cannot make a restore spend more memory or time
    if not (1 < n <= SCRYPT_N and n & (n - 1) == 0) or not 0 < r <= SCRYPT_R or not 0 < p <= SCRYPT_P:
        raise CentralBackupError("the key's scrypt parameters are out of range")
    return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(normalized(passphrase))


# what of a key travels in a backup's header (never "mac", the maker's key in clear the panel keeps)
SHARED = ("public", "kdf", "nonce", "sealed")


def new_key(passphrase: str) -> dict:
    """A new key: the X25519 public key; the private key and a maker's key (32 random bytes that sign each backup
    this panel makes) sealed with passphrase; the maker's key in clear too ("mac"): the panel signs with it and
    cannot decrypt. Only "public", "kdf", "nonce" and "sealed" go in a backup."""
    private = X25519PrivateKey.generate()
    raw = private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                serialization.NoEncryption())
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    mac = secrets.token_bytes(_MAC)
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
    sealed = AESGCM(_kek(passphrase, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)).encrypt(nonce, raw + mac, _KEY_AAD + public)
    return {"public": _b64(public), "kdf": {"name": "scrypt", "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P,
                                            "salt": _b64(salt)},
            "nonce": _b64(nonce), "sealed": _b64(sealed), "mac": _b64(mac)}


def check_key(key) -> dict:
    """key as new_key made it (its shared part), its fields there and of the right sizes; CentralBackupError
    otherwise."""
    if not isinstance(key, dict) or not isinstance(key.get("kdf"), dict) or key["kdf"].get("name") != "scrypt":
        raise CentralBackupError("not a key of a central panel's backup")
    if len(_unb64(key.get("public"))) != 32 or len(_unb64(key.get("nonce"))) != 12 \
            or len(_unb64(key["kdf"].get("salt"))) != 16 or len(_unb64(key.get("sealed"))) != _PRIVATE + _MAC + 16:
        raise CentralBackupError("not a key of this version of a central panel's backup: make a new key")
    for name in ("n", "r", "p"):
        if isinstance(key["kdf"].get(name), bool) or not isinstance(key["kdf"].get(name), int):
            raise CentralBackupError("not a key of a central panel's backup: its scrypt parameters")
    return key


def open_key(key: dict, passphrase: str) -> tuple[X25519PrivateKey, bytes]:
    """(the private key, the maker's key), opened with passphrase; CentralBackupError for another passphrase."""
    check_key(key)
    kdf, public = key["kdf"], _unb64(key["public"])
    try:
        raw = AESGCM(_kek(passphrase, _unb64(kdf["salt"]), kdf["n"], kdf["r"], kdf["p"])).decrypt(
            _unb64(key["nonce"]), _unb64(key["sealed"]), _KEY_AAD + public)
    except InvalidTag as exc:
        raise CentralBackupError("the passphrase does not open this key: another passphrase, or a key "
                                 "made again since") from exc
    private = X25519PrivateKey.from_private_bytes(raw[:_PRIVATE])
    if private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw) != public:
        raise CentralBackupError("the key opened is not the one of its public key")
    return private, raw[_PRIVATE:]


def fingerprint(key: dict) -> str:
    """What the page shows of a key: the first 16 hex digits of its public key's SHA-256, in groups of 4."""
    digest = hashlib.sha256(_unb64(key["public"])).hexdigest()[:16]
    return " ".join(digest[i:i + 4] for i in range(0, 16, 4))


def _file_key(shared: bytes, ephemeral: bytes, public: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=ephemeral + public, info=_FILE_INFO).derive(shared)


def _context(header_bytes: bytes) -> str:
    """pullcrypto's context of the file's chunks: the header's hash, so the header cannot change either."""
    return "central backup " + hashlib.sha256(header_bytes).hexdigest()


def _header_bytes(header: dict) -> bytes:
    data = json.dumps(header, sort_keys=True, separators=(",", ":")).encode()
    return MAGIC + struct.pack(">I", len(data)) + data


def _made_by(mac: bytes, head: bytes, body_digest: bytes) -> bytes:
    """The maker's tag: who holds the maker's key signs the header and the body's hash."""
    return hmac.new(mac, _MADE + hashlib.sha256(head).digest() + body_digest, hashlib.sha256).digest()


def seal(key: dict, src: Path, dst: Path, about: dict) -> None:
    """src sealed into dst (600) to key's public key, signed with its maker's key: the panel cannot open dst, and
    nobody without the maker's key (in clear on the panel, sealed in the backup) can make one the passphrase opens.
    about: what the header says of the backup (when, which panel, which version)."""
    check_key(key)
    mac = _unb64(key.get("mac"))
    if len(mac) != _MAC:
        raise CentralBackupError("this key has no maker's key: make a new key")
    public = _unb64(key["public"])
    ephemeral = X25519PrivateKey.generate()
    ephemeral_public = ephemeral.public_key().public_bytes(serialization.Encoding.Raw,
                                                           serialization.PublicFormat.Raw)
    header = {"v": FORMAT, **about, "key": {k: key[k] for k in SHARED}, "ephemeral": _b64(ephemeral_public)}
    head = _header_bytes(header)
    file_key = _file_key(ephemeral.exchange(X25519PublicKey.from_public_bytes(public)), ephemeral_public, public)
    body = dst.with_name(dst.name + ".body")
    try:
        pullcrypto.encrypt_file(file_key, _context(head), src, body)
        digest = hashlib.sha256()
        fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as out, open(body, "rb") as inp:
            out.write(head)
            for block in iter(lambda: inp.read(1 << 20), b""):
                digest.update(block)
                out.write(block)
            out.write(_made_by(mac, head, digest.digest()))
    finally:
        body.unlink(missing_ok=True)


def read_header(path: Path) -> tuple[dict, bytes, int]:
    """(header, its bytes as written, where the body starts) of a backup; CentralBackupError for any other file."""
    try:
        with open(path, "rb") as fh:
            magic = fh.read(len(MAGIC) + 4)
            if len(magic) != len(MAGIC) + 4 or magic[:4] != MAGIC[:4]:
                raise CentralBackupError(f"{path.name} is not a backup of a central panel")
            if magic[:len(MAGIC)] != MAGIC:
                raise CentralBackupError(f"{path.name} is a backup of another version of the central panel's "
                                         f"protection (format {magic[4]}, this one reads {FORMAT})")
            (size,) = struct.unpack(">I", magic[len(MAGIC):])
            if not 0 < size <= MAX_HEADER:
                raise CentralBackupError(f"{path.name}: its header is damaged")
            data = fh.read(size)
    except OSError as exc:
        raise CentralBackupError(f"cannot read {path}: {exc.strerror}") from exc
    if len(data) != size:
        raise CentralBackupError(f"{path.name} is cut short")
    try:
        header = json.loads(data)
    except ValueError as exc:
        raise CentralBackupError(f"{path.name}: its header is damaged") from exc
    if not isinstance(header, dict) or header.get("v") != FORMAT:
        raise CentralBackupError(f"{path.name} is not a backup of format {FORMAT}")
    check_key(header.get("key"))
    return header, magic + data, len(magic) + size


def open_sealed(path: Path, passphrase: str, dst: Path) -> dict:
    """The archive in a backup, opened with passphrase into dst: its header. Its maker checked first: refused
    (CentralBackupError, dst not written) for another passphrase, a backup this key's panel did not make, or a
    file changed anywhere, cut short or lengthened."""
    header, head, start = read_header(path)
    private, mac = open_key(header["key"], passphrase)
    ephemeral = _unb64(header.get("ephemeral"))
    if len(ephemeral) != 32:
        raise CentralBackupError(f"{path.name}: its header is damaged")
    refused = (f"{path.name} was not made by the panel of this key, or was changed, cut short or damaged: it does "
               "not open, nothing was restored")
    size = path.stat().st_size
    if size < start + _TAG:
        raise CentralBackupError(refused)
    public = _unb64(header["key"]["public"])
    file_key = _file_key(private.exchange(X25519PublicKey.from_public_bytes(ephemeral)), ephemeral, public)
    body = dst.with_name(dst.name + ".body")
    try:
        digest, left = hashlib.sha256(), size - start - _TAG
        with open(path, "rb") as fh, open(os.open(body, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as out:
            fh.seek(start)
            while left:
                block = fh.read(min(left, 1 << 20))
                if not block:
                    raise CentralBackupError(refused)
                digest.update(block)
                out.write(block)
                left -= len(block)
            tag = fh.read(_TAG + 1)
        # who made it, before anything of it is believed
        if len(tag) != _TAG or not hmac.compare_digest(tag, _made_by(mac, head, digest.digest())):
            raise CentralBackupError(refused)
        try:
            pullcrypto.decrypt_file(file_key, _context(head), body, dst)
        except pullcrypto.PullerCryptoError as exc:
            raise CentralBackupError(refused) from exc
    finally:
        body.unlink(missing_ok=True)
    os.chmod(dst, 0o600)
    return header


# --- what is saved ----------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Slot:
    """One thing of the panel's state: its name in the archive, what it is for, a file or a folder."""

    name: str
    why: str
    folder: bool = False


INVENTORY = (
    Slot("links", "the links: each machine's address, its token in clear, its SSH settings"),
    Slot("ssh", "the SSH keys of the links and the host keys pinned at the first connection", folder=True),
    Slot("link_agents", "the highest agent_api each machine said (AD-017 §17.5)"),
    Slot("link_panels", "the machines that said panel: agent, kept once said (AD-017 slice 6)"),
    Slot("users", "the people of this panel, their roles, the MCP tokens and the incoming links (hashes)"),
    Slot("invitations", "the invitations not used yet (hashes)"),
    Slot("second_factor", "the second factors of the people (TOTP)"),
    Slot("passkeys", "the passkeys of the people"),
    Slot("sso", "the single sign-on of the workspaces"),
    Slot("oauth", "the OAuth clients and grants of MCP"),
    Slot("identity_imports", "the identities of the linked machines' workspaces imported here (AD-018)", folder=True),
    Slot("workspace_homes", "the links whose machines' workspaces this panel serves (AD-018 slice 2)"),
    Slot("options", "the options of this panel"),
    Slot("groups", "the groups the lists are arranged in"),
    Slot("destinations", "the backup destinations, this backup's own among them, with their keys"),
    Slot("connections", "the puller connections of machines that nobody reaches, with their keys"),
    Slot("central_backup", "this backup's setup: destination, time, public key and sealed private key"),
)
BY_SLOT = {s.name: s for s in INVENTORY}
# not saved, and why (README, Protection of the central panel)
LEFT_OUT = (
    "the sessions (everybody logs in again)", "the jobs and their logs", "the histories and the monitor",
    "the servers, workspaces and backups of this machine's own instances (they have their own backups)",
    "anything of the linked machines")


def slot_paths(settings) -> dict[str, Path]:
    """Where each slot of INVENTORY is on this panel."""
    data_dir = (settings.links_file or settings.servers_file).parent
    users = settings.users_file or settings.servers_file.parent / "users.yaml"
    private = users.parent
    return {
        "links": settings.links_file or data_dir / "links.yaml",
        "ssh": data_dir / "ssh",
        "link_agents": data_dir / centralpages.SEEN_FILE,
        "link_panels": data_dir / centralpages.PANELS_FILE,
        "users": users,
        "invitations": invitations_file_of(users),
        "second_factor": private / "second-factor.json",
        "passkeys": private / "passkeys.json",
        "sso": private / "sso.json",
        "oauth": settings.jobs_dir.parent / "oauth.json",
        "identity_imports": settings.jobs_dir.parent / identityimport.FOLDER,
        "workspace_homes": data_dir / remotehome.HOMES_FILE,
        "options": settings.options_file or settings.servers_file.parent / "options.yaml",
        "groups": settings.groups_file or settings.servers_file.parent / "groups.yaml",
        "destinations": settings.destinations_file or settings.servers_file.parent / "destinations.yaml",
        "connections": settings.connections_file or settings.servers_file.parent / "connections.yaml",
        "central_backup": data_dir / FILE,
    }


def state_file(settings) -> Path:
    return slot_paths(settings)["central_backup"]


# the access log a restore brought (logs/before-restore-<date>/): in the next backups too, so a panel restored and
# lost again keeps the record of before
BEFORE_RESTORE = "before-restore-"


def _access_files(settings) -> list[tuple[str, Path]]:
    """(its name in the archive, its path) of each file of the access log, those a restore brought included."""
    folder = settings.logs_dir
    if not folder.is_dir():
        return []
    found = [(p.relative_to(folder).as_posix(), p)
             for p in [*folder.glob("access*.jsonl*"), *folder.glob(f"{BEFORE_RESTORE}*/access*.jsonl*")]
             if p.is_file() and not p.name.endswith(".tmp")]
    return sorted(found)


def build_archive(settings, config_path: Optional[Path], target: Path, made: str, version: str) -> dict:
    """The panel's state as a gzipped tar in target (600): its manifest, what it holds and what it could not read."""
    paths = slot_paths(settings)
    manifest = {"v": FORMAT, "made": made, "host": socket.gethostname(), "version": version,
                "slots": {}, "ca": {}, "access": [], "config": False, "unreadable": [],
                "links": sorted(settings.link_targets)}
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as raw, tarfile.open(fileobj=raw, mode="w:gz") as tar:
        def add(name: str, path: Path) -> bool:
            try:
                data = path.read_bytes()
            except OSError:
                manifest["unreadable"].append(str(path))
                return False
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o600, int(path.stat().st_mtime)
            tar.addfile(info, io.BytesIO(data))
            return True

        for slot in INVENTORY:
            path = paths[slot.name]
            if slot.folder:
                if not path.is_dir():
                    continue
                files = sorted(p.name for p in path.iterdir() if p.is_file() and not p.name.endswith(".tmp"))
                kept = [f for f in files if add(f"slots/{slot.name}/{f}", path / f)]
                manifest["slots"][slot.name] = {"folder": True, "files": kept}
            elif path.is_file() and add(f"slots/{slot.name}", path):
                manifest["slots"][slot.name] = {"folder": False}
        for name, link in sorted(settings.link_targets.items()):
            if link.ca_file and add(f"ca/{name}", Path(link.ca_file)):
                manifest["ca"][name] = link.ca_file
        for name, path in _access_files(settings):
            if add(f"access/{name}", path):
                manifest["access"].append(name)
        if config_path is not None and config_path.is_file():
            manifest["config"] = add("config/omniacli.yaml", config_path)
        data = json.dumps(manifest, indent=1).encode()
        info = tarfile.TarInfo(MANIFEST)
        info.size, info.mode = len(data), 0o600
        tar.addfile(info, io.BytesIO(data))
    return manifest


# --- the setup and the nightly run ------------------------------------------------------------------------------

def load_state(path: Path) -> dict:
    """The setup, {} when there is none yet."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    return data if isinstance(data, dict) else {}


def save_state(path: Path, state: dict) -> None:
    """Mode 600 from the start: it holds the sealed private key."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1, sort_keys=True)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def check_time(value) -> str:
    value = str(value or "").strip()
    if not TIME.fullmatch(value):
        raise ConfigError(f"{value!r} is not a time of the day, as 02:30")
    return value


def panel_name(settings) -> str:
    """This panel's folder in the destination: its name, letters, digits, . _ -."""
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", settings.central or socket.gethostname()).strip(".-") or "central"
    return name[:63]


def folder_of(settings) -> str:
    return f"{FOLDER}/{panel_name(settings)}"


def next_run(state: dict, now: Optional[datetime.datetime] = None) -> Optional[str]:
    """When it runs next (ISO, local time); None when it does not."""
    if ready(state):
        return None
    now = now or datetime.datetime.now()
    hour, minute = map(int, state["time"].split(":"))
    when = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if when <= now:
        when += datetime.timedelta(days=1)
    return when.isoformat(timespec="minutes")


def ready(state: dict) -> str:
    """'' when the nightly backup runs; else why not, in English."""
    if not state.get("key"):
        return "no key yet: make one"
    if not state.get("confirmed"):
        return "the key's passphrase is not confirmed yet: type it back"
    if not state.get("destination"):
        return "no destination chosen"
    if not state.get("enabled", True):
        return "switched off"
    if not TIME.fullmatch(str(state.get("time") or "")):
        return "no time chosen"
    return ""


def due(state: dict, minute: datetime.datetime) -> bool:
    """The nightly backup is due at minute (a minute, seconds 0)."""
    return not ready(state) and minute.strftime("%H:%M") == state["time"]


class Nightly:
    """Starts the backup at its time every day, as the schedules do (schedules.Scheduler): each minute once; a
    run missed while OmniaCLI was stopped is not made up; one that finds a run going is not started twice. The
    time is the machine's local clock: once a day at most, so the hour the clocks go back (its minutes twice) runs
    it once; a time in the hour they skip (02:00-02:59 in Europe in spring) has no run that night, which is why
    the default is 01:30."""

    def __init__(self, state: Callable[[], dict], fire: Callable[[], object],
                 not_started: Optional[Callable[[str], object]] = None):
        self.state = state
        self.fire = fire
        # told when the night's run cannot start (a run said to be going, a refusal, an error): nobody watches
        self.not_started = not_started
        self._task: Optional[asyncio.Task] = None
        # the day it last started: never twice the same day
        self.fired: Optional[datetime.date] = None

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    def tick(self, minute: datetime.datetime) -> bool:
        """The backup started when due at minute: whether it was."""
        if minute.date() == self.fired or not due(self.state(), minute):
            return False
        self.fired = minute.date()
        try:
            answer = self.fire()
        except Exception as exc:  # noqa: BLE001 - the loop must survive
            _logger.exception("The nightly backup of this panel did not start")
            self._tell(f"it raised: {exc}")
            return False
        if isinstance(answer, tuple) and answer and answer[0] >= 300:
            why = str((answer[1] or {}).get("error") or answer[0])
            _logger.warning("The nightly backup of this panel did not start: %s", why)
            self._tell(why)
            return False
        return True

    def _tell(self, why: str) -> None:
        if self.not_started is None:
            return
        try:
            self.not_started(why)
        except Exception:  # noqa: BLE001 - the loop must survive
            _logger.exception("The failure of the nightly backup of this panel was not told")

    async def _loop(self) -> None:
        last = None
        while True:
            now = datetime.datetime.now()
            minute = now.replace(second=0, microsecond=0)
            if minute != last:
                self.tick(minute)
            last = minute
            await asyncio.sleep(60 - now.second - now.microsecond / 1e6 + 0.5)


async def run(job: Job, settings, config_path: Optional[Path], version: str,
              dest: destinations.Destination, state: dict) -> dict:
    """The panel's state sealed and copied to dest, the older copies there out of the rotation: what was copied."""
    key = state.get("key")
    if not key:
        raise JobFailed("no key for the backup of this panel: make one on its page")
    made = datetime.datetime.now()
    name = f"{PREFIX}{made.strftime('%Y%m%d_%H%M%S')}{SUFFIX}"
    folder = folder_of(settings)
    work = Path(tempfile.mkdtemp(prefix=".central-backup-", dir=settings.jobs_dir.parent))
    try:
        os.chmod(work, 0o700)
        archive, sealed = work / "state.tar.gz", work / name
        job.log("--- the state of this panel: links, SSH keys, people, options, destinations, access log")
        manifest = await asyncio.to_thread(build_archive, settings, config_path, archive,
                                           made.isoformat(timespec="seconds"), version)
        job.log(f"{len(manifest['slots'])} parts, {len(manifest['links'])} links "
                f"({', '.join(manifest['links']) or 'none'}), {len(manifest['access'])} files of the access log")
        for path in manifest["unreadable"]:
            job.warn(f"{path} could not be read: it is not in this backup")
        job.log(f"--- seal it to the key {fingerprint(key)} (X25519, AES-256-GCM): this panel cannot open it")
        await asyncio.to_thread(seal, key, archive, sealed,
                                {"made": made.isoformat(timespec="seconds"), "host": socket.gethostname(),
                                 "version": version})
        size = sealed.stat().st_size
        job.log(f"--- copy to {dest.name} ({dest.label}): {dest.path}/{folder}/{name} ({size} bytes)")
        await destinations.rclone(dest, "copyto", "--retries", "3", "--low-level-retries", "10", "-v",
                                  str(sealed), destinations.remote(dest, folder, name), job=job,
                                  timeout=destinations.COPY_TIMEOUT)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    await destinations.rotate(job, dest, folder, PREFIX,
                              lambda times: rotation_kept(times, ROTATION["daily"], ROTATION["weekly"],
                                                          ROTATION["monthly"]), suffix=SUFFIX)
    job.log(f"{name} is in {dest.name}: to restore it on a new machine, omniacli central-restore {name}")
    return {"file": name, "size": size, "folder": folder, "destination": dest.name}


# --- the way back -----------------------------------------------------------------------------------------------

_SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@+=-]{0,200}")
# what an archive may hold at most, unpacked: a restore does not fill the disk of a fresh install
MAX_ARCHIVE = 1 << 30
MAX_MEMBERS = 20000
# the CA files of the links, where a restore puts them (beside the links file): never at a path the backup names
CA_FOLDER = "link-ca"
# the slots read as YAML or JSON before anything is written: a broken one stops the restore before it starts
_YAML_SLOTS = {"links", "users", "invitations", "options", "groups", "destinations", "connections"}
_JSON_SLOTS = {"link_agents", "link_panels", "second_factor", "passkeys", "sso", "oauth", "central_backup"}
# the name of a file being written by a restore (in the purge)
_TEMP = ".central-restore-"


def _members(tar: tarfile.TarFile) -> dict[str, bytes]:
    """The archive's regular files by name: nothing else, no name outside it, no more than MAX_ARCHIVE unpacked."""
    found, total = {}, 0
    for count, member in enumerate(tar, 1):
        parts = member.name.split("/")
        if not member.isfile() or any(not _SAFE.fullmatch(p) for p in parts) or len(parts) > 3:
            raise CentralBackupError(f"the archive holds {member.name!r}: not one of a central panel's backup")
        total += member.size
        if count > MAX_MEMBERS or total > MAX_ARCHIVE:
            raise CentralBackupError("the archive is larger than a central panel's state: refused")
        data = tar.extractfile(member).read(member.size + 1)
        if len(data) != member.size:
            raise CentralBackupError(f"the archive's {member.name} is damaged")
        found[member.name] = data
    return found


def _plan(files: dict[str, bytes]) -> dict:
    """The manifest of an archive opened, checked against what it holds: every name it lists is there, each list
    and mapping is one, every YAML and JSON part reads. Anything else is a CentralBackupError, before a byte is
    written."""
    def bad(why: str) -> CentralBackupError:
        return CentralBackupError(f"the archive is not a central panel's backup: {why}")

    try:
        manifest = json.loads(files.get(MANIFEST, b""))
    except ValueError as exc:
        raise bad("no manifest") from exc
    if not isinstance(manifest, dict):
        raise bad("its manifest is not a mapping")
    slots, ca, access = manifest.get("slots", {}), manifest.get("ca", {}), manifest.get("access", [])
    if not isinstance(slots, dict) or not isinstance(ca, dict) or not isinstance(access, list):
        raise bad("its manifest's slots, ca or access are not what they should be")
    for name, given in slots.items():
        slot = BY_SLOT.get(name)
        if slot is None or not isinstance(given, dict) or given.get("folder", False) is not slot.folder:
            raise bad(f"an unknown part {name!r}")
        if slot.folder:
            listed = given.get("files", [])
            if not isinstance(listed, list) or any(not isinstance(f, str) or not _SAFE.fullmatch(f)
                                                   or f"slots/{name}/{f}" not in files for f in listed):
                raise bad(f"the files of {name} are not in it")
            continue
        data = files.get(f"slots/{name}")
        if data is None:
            raise bad(f"{name} is listed and not in it")
        try:
            if name in _YAML_SLOTS:
                found = yaml.safe_load(data.decode("utf-8"))
                if found is not None and not isinstance(found, dict):
                    raise ValueError("not a mapping")
            elif name in _JSON_SLOTS:
                json.loads(data)
        except (UnicodeDecodeError, ValueError, yaml.YAMLError) as exc:
            raise bad(f"{name} does not read: {exc}") from exc
    links = {}
    if "links" in slots:
        links = (yaml.safe_load(files["slots/links"].decode("utf-8")) or {}).get("links") or {}
        if not isinstance(links, dict) or any(not isinstance(v, dict) for v in links.values()):
            raise bad("its links are not a mapping")
    for link, path in ca.items():
        if not isinstance(link, str) or not _SAFE.fullmatch(link) or link not in links or not isinstance(path, str) \
                or f"ca/{link}" not in files:
            raise bad(f"the CA file of {link!r} is not one of its links'")
    for name in access:
        if not isinstance(name, str) or f"access/{name}" not in files:
            raise bad("a file of its access log is not in it")
    return manifest


def inspect(backup: Path, passphrase: str) -> tuple[dict, dict, dict]:
    """(header, manifest, files) of a backup opened with passphrase, its maker and its archive checked."""
    with tempfile.TemporaryDirectory(prefix="omniacli-central-restore-") as tmp:
        os.chmod(tmp, 0o700)
        archive = Path(tmp) / "state.tar.gz"
        header = open_sealed(backup, passphrase, archive)
        try:
            with tarfile.open(archive, mode="r:gz") as tar:
                files = _members(tar)
        except (tarfile.TarError, OSError, EOFError) as exc:
            raise CentralBackupError(f"{backup.name}: the archive inside does not read: {exc}") from exc
    return header, _plan(files), files


def _walk(path: Path, make: bool = False, owner: Optional[tuple[int, int]] = None, mode: int = 0o700) -> int:
    """An fd of the folder path, each of its parts opened without following a symbolic link (a folder of the
    service's user replaced by a link to one of root's is refused); the parts missing made with mode and owner
    when make. Missing and not make: FileNotFoundError."""
    path = Path(os.path.abspath(path))
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            try:
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                if not make:
                    raise
                os.mkdir(part, mode, dir_fd=fd)
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.fchmod(nxt, mode)
                if owner is not None:
                    os.fchown(nxt, *owner)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise CentralBackupError(f"{path} is, or goes through, a symbolic link or a file: refused, "
                                             "nothing written there") from exc
                raise
            os.close(fd)
            fd = nxt
    except BaseException:
        os.close(fd)
        raise
    return fd


def _check_place(path: Path, folder: bool = False) -> None:
    """Before anything is written: the folder that will hold path (or path, a folder) goes through no symbolic
    link, as far as it exists."""
    target = path if folder else path.parent
    probe = target
    while True:
        try:
            os.close(_walk(probe))
            break
        except FileNotFoundError:
            if probe.parent == probe:
                break
            probe = probe.parent
    if folder and os.path.islink(path):
        raise CentralBackupError(f"{path} is a symbolic link: refused, nothing written")


def _write(path: Path, data: bytes, owner: Optional[tuple[int, int]], mode: int = 0o600,
           folder_mode: int = 0o700) -> None:
    """data in path: a new file of a random name in its folder (O_EXCL, O_NOFOLLOW), its mode and owner set on it,
    then renamed over path; the folder reached without following a symbolic link."""
    dfd = _walk(path.parent, make=True, owner=owner, mode=folder_mode)
    try:
        temp = f"{_TEMP}{secrets.token_hex(8)}"
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=dfd)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fchmod(fh.fileno(), mode)
                if owner is not None:
                    os.fchown(fh.fileno(), *owner)
            os.replace(temp, path.name, src_dir_fd=dfd, dst_dir_fd=dfd)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temp, dir_fd=dfd)
            raise
    finally:
        os.close(dfd)


def _empty(folder: Path, owner: Optional[tuple[int, int]]) -> None:
    """folder made (700, owner) or emptied of its files, reached without following a symbolic link."""
    dfd = _walk(folder, make=True, owner=owner)
    try:
        os.fchmod(dfd, 0o700)
        if owner is not None:
            os.fchown(dfd, *owner)
        with os.scandir(dfd) as entries:
            for entry in entries:
                if not entry.is_dir(follow_symlinks=False):
                    os.unlink(entry.name, dir_fd=dfd)
    finally:
        os.close(dfd)


def restore(backup: Path, passphrase: str, settings, config_path: Path, replace: bool = False,
            main_config: bool = False, owner: Optional[tuple[int, int]] = None,
            stop: Callable[[], None] = lambda: None, start: Callable[[], None] = lambda: None,
            now: Optional[datetime.datetime] = None) -> dict:
    """The panel's state of backup put back where settings (this install's) keep it: what was restored.
    Refused, before the service stops and before anything is written: another passphrase, a backup the panel of
    its key did not make, a file changed or damaged, an archive that is not a panel's state, a place that goes
    through a symbolic link, and a panel that has links already (replace: they are replaced). The CA files of the
    links go to link-ca/ beside the links file, the links say so. The main configuration of the backup goes
    beside this one (main_config: in its place). owner: the (uid, gid) of what is written (root restores for the
    service's user). stop and start: the service, around the writes."""
    now = now or datetime.datetime.now()
    header, manifest, files = inspect(backup, passphrase)
    paths = slot_paths(settings)
    have = load_links(paths["links"]) if paths["links"].is_file() else {}
    if have and not replace:
        raise CentralBackupError(
            f"this panel has links already ({', '.join(sorted(have))}): a backup is restored on a fresh install; "
            "--replace to put the backup's in their place")
    config_data = files.get("config/omniacli.yaml")
    if main_config:
        if config_data is None:
            raise CentralBackupError("the backup has no main configuration")
        try:
            data = yaml.safe_load(config_data.decode("utf-8")) or {}
        except (UnicodeDecodeError, yaml.YAMLError) as exc:
            raise CentralBackupError(f"the main configuration of the backup does not read: {exc}") from exc
        if not isinstance(data, dict):
            raise CentralBackupError("the main configuration of the backup is not a mapping")
    slots = manifest.get("slots", {})
    data_dir = paths["links"].parent
    ca_dir = data_dir / CA_FOLDER
    # the links, their CA files moved to ca_dir
    links_data = files.get("slots/links")
    ca = manifest.get("ca", {})
    if links_data is not None and ca:
        found = yaml.safe_load(links_data.decode("utf-8")) or {}
        for link in ca:
            found["links"][link]["ca_file"] = str(ca_dir / f"{link}.pem")
        links_data = yaml.safe_dump(found, sort_keys=False, allow_unicode=True).encode("utf-8")
    stamp = now.strftime("%Y%m%d-%H%M%S")
    access = settings.logs_dir / f"{BEFORE_RESTORE}{stamp}"
    config_target = config_path if main_config else config_path.with_name(config_path.name + ".from-backup")
    # every place checked before the service stops
    for slot in INVENTORY:
        if slot.name in slots:
            _check_place(paths[slot.name], folder=slot.folder)
    for place in ((ca_dir / "x") if ca else None, (access / "x") if manifest.get("access") else None,
                  config_target if config_data is not None else None):
        if place is not None:
            _check_place(place)
    restored = []
    stop()
    try:
        for slot in INVENTORY:
            given = slots.get(slot.name)
            if given is None:
                continue
            path = paths[slot.name]
            if slot.folder:
                _empty(path, owner)
                for name in given.get("files", []):
                    _write(path / name, files[f"slots/{slot.name}/{name}"], owner,
                           0o644 if name.endswith(".pub") or name.startswith("known_hosts") else 0o600)
            else:
                _write(path, links_data if slot.name == "links" else files[f"slots/{slot.name}"], owner)
            restored.append(slot.name)
        for link in ca:
            _write(ca_dir / f"{link}.pem", files[f"ca/{link}"], owner, 0o600)
        for name in manifest.get("access", []):
            # the access log an earlier restore brought stays in its own folder
            _write(access / name, files[f"access/{name}"], owner, 0o640, folder_mode=0o750)
        if config_data is not None:
            if main_config:
                mode = config_path.stat().st_mode & 0o777 if config_path.exists() else 0o640
                group = (0, config_path.stat().st_gid if config_path.exists() else owner[1]) if owner else None
                _write(config_path, config_data, group, mode)
            else:
                _write(config_target, config_data, (0, 0) if owner else None, 0o600)
    except (CentralBackupError, OSError) as exc:
        # a place changed under the restore after its check (the service user's folder): what is written stays,
        # so the way to finish is said
        raise CentralBackupError(f"{exc}; part of this panel's state is already written"
                                 f"{' (' + ', '.join(restored) + ')' if restored else ''}: "
                                 "run central-restore again with --replace") from exc
    finally:
        start()
    return {"made": header.get("made"), "host": header.get("host"), "version": header.get("version"),
            "links": manifest.get("links", []), "restored": restored,
            "access": str(access) if manifest.get("access") else "",
            "config_beside": "" if main_config or config_data is None else str(config_target),
            "main_config": main_config, "replaced": sorted(have), "ca": sorted(ca)}
