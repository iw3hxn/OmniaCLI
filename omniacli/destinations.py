# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Remote destinations of the backups: a place out of the machine where the
automatic backups of an instance are copied, encrypted, and kept by the same
rotation as here.

One engine, rclone, for every kind: SFTP, FTP, S3 and the services that speak
it (Wasabi, Backblaze B2, Cloudflare R2, MinIO...), Google Drive, Dropbox,
OneDrive, WebDAV (Nextcloud), Azure Blob, Google Cloud Storage. A
destination belongs to the machine (its admins) or to a workspace (its
admins, for its instances). Its settings, passwords and tokens included,
are in destinations.yaml next to the servers file (mode 600); rclone gets
them in its environment for one command, never in a file nor on its
command line.

Every file is encrypted here before it leaves (pullcrypto's AES-256-GCM in
chunks, the destination's own key): the provider stores what it cannot
read. Without the key a copy cannot be read back: the page shows it, to
keep it somewhere else too. A copy is <path>/<server>/<role>/<backup>.ocpf.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import os
import re
import secrets
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import yaml

from . import ops, pullcrypto, stagings
from .config import ConfigError
from .jobs import Job, JobFailed

# a destination's name: in URLs and in the schedules file
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
# the folder in the destination: no .. nor a leading /
PATH = re.compile(r"[A-Za-z0-9._ @+=,-]+(/[A-Za-z0-9._ @+=,-]+)*")
# what a copy is called there: the backup's name and this suffix
SUFFIX = ".ocpf"
# the context the files' keys are derived with (pullcrypto's "puller")
CONTEXT = "remote backup"
# rclone's name for the remote of a command (RCLONE_CONFIG_<REMOTE>_...)
REMOTE = "omniacli"
RCLONE = "rclone"
# a copy: rclone retries itself; a large backup over a slow line takes hours
COPY_TIMEOUT = 12 * 3600
LIST_TIMEOUT = 120
# a stamp in a backup's name: <db>_YYYYmmdd_HHMMSS.zip
STAMP = re.compile(r"(\d{8}_\d{6})")


@dataclass(frozen=True)
class Setting:
    """A setting of a kind: its rclone option, how it is entered, whether
    rclone wants it obscured (its passwords) and whether it is a secret
    (never sent back to the browser)."""

    name: str
    label: str
    kind: str = "text"  # text, password, number, checkbox, select, textarea
    required: bool = False
    default: str = ""
    choices: tuple = ()
    obscure: bool = False
    help: str = ""

    @property
    def secret(self) -> bool:
        return self.kind in ("password", "textarea") or self.obscure

    def to_dict(self) -> dict:
        return {"name": self.name, "label": self.label, "kind": self.kind, "required": self.required,
                "default": self.default, "choices": [list(c) for c in self.choices], "help": self.help}


TOKEN_HELP = 'The JSON that rclone authorize "{kind}" prints on a computer with a browser'
KINDS: dict[str, tuple[str, str, tuple[Setting, ...]]] = {
    # kind: (label, rclone type, settings)
    "sftp": ("SFTP", "sftp", (
        Setting("host", "Host", required=True), Setting("port", "Port", "number", default="22"),
        Setting("user", "User", required=True), Setting("pass", "Password", "password", obscure=True),
        Setting("key_pem", "Private key (PEM)", "textarea", help="Instead of the password"))),
    "ftp": ("FTP", "ftp", (
        Setting("host", "Host", required=True), Setting("port", "Port", "number", default="21"),
        Setting("user", "User", required=True), Setting("pass", "Password", "password", required=True, obscure=True),
        Setting("explicit_tls", "TLS (explicit, FTPES)", "checkbox", default="true"),
        Setting("tls", "TLS (implicit, FTPS)", "checkbox"))),
    "s3": ("S3 and compatible", "s3", (
        Setting("provider", "Provider", "select", required=True, default="AWS",
                choices=(("AWS", "Amazon S3"), ("Wasabi", "Wasabi"), ("Cloudflare", "Cloudflare R2"),
                         ("Minio", "MinIO"), ("Scaleway", "Scaleway"), ("Other", "Other (Backblaze B2, ...)"))),
        Setting("access_key_id", "Access key", required=True),
        Setting("secret_access_key", "Secret key", "password", required=True),
        Setting("region", "Region"), Setting("endpoint", "Endpoint", help="Empty for Amazon S3"))),
    "drive": ("Google Drive", "drive", (
        Setting("token", "Token", "textarea", required=True, help=TOKEN_HELP.format(kind="drive")),
        Setting("root_folder_id", "Folder ID", help="Empty: the root of My Drive"))),
    "dropbox": ("Dropbox", "dropbox", (
        Setting("token", "Token", "textarea", required=True, help=TOKEN_HELP.format(kind="dropbox")),)),
    "onedrive": ("OneDrive", "onedrive", (
        Setting("token", "Token", "textarea", required=True, help=TOKEN_HELP.format(kind="onedrive")),
        Setting("drive_id", "Drive ID", required=True),
        Setting("drive_type", "Drive type", "select", required=True, default="personal",
                choices=(("personal", "Personal"), ("business", "Business"), ("documentLibrary", "SharePoint"))))),
    "webdav": ("WebDAV (Nextcloud)", "webdav", (
        Setting("url", "URL", required=True),
        Setting("vendor", "Server", "select", default="nextcloud",
                choices=(("nextcloud", "Nextcloud"), ("owncloud", "ownCloud"), ("other", "Other"))),
        Setting("user", "User"), Setting("pass", "Password", "password", obscure=True))),
    "azureblob": ("Azure Blob", "azureblob", (
        Setting("account", "Account", required=True), Setting("key", "Key", "password", required=True))),
    "gcs": ("Google Cloud Storage", "google cloud storage", (
        Setting("service_account_credentials", "Service account (JSON)", "textarea", required=True),
        Setting("bucket_policy_only", "Uniform bucket-level access", "checkbox", default="true"))),
}


@dataclass
class Destination:
    name: str
    kind: str
    path: str
    workspace: str = ""  # empty: the machine's
    settings: dict = field(default_factory=dict)
    key: str = ""  # pullcrypto's text form

    @property
    def label(self) -> str:
        return KINDS[self.kind][0]

    def to_dict(self) -> dict:
        data = {"kind": self.kind, "path": self.path}
        if self.workspace:
            data["workspace"] = self.workspace
        data["settings"] = dict(self.settings)
        data["key"] = self.key
        return data

    def public(self) -> dict:
        """What the page shows: a secret's value never, only that it is set."""
        settings = KINDS[self.kind][2]
        return {"name": self.name, "kind": self.kind, "label": self.label, "path": self.path,
                "workspace": self.workspace,
                "settings": {s.name: ("" if s.secret else self.settings.get(s.name, "")) for s in settings},
                "set": sorted(s.name for s in settings if s.secret and self.settings.get(s.name))}


def check_settings(kind: str, given: dict, saved: dict | None = None) -> dict:
    """The settings of a kind, checked: a secret left empty keeps the saved one."""
    if kind not in KINDS:
        raise ConfigError(f"{kind!r} is not one of {', '.join(KINDS)}")
    if not isinstance(given, dict):
        raise ConfigError("the settings are a mapping")
    saved = saved or {}
    found = {}
    for setting in KINDS[kind][2]:
        raw = given.get(setting.name)
        if setting.kind == "checkbox":
            value = "true" if raw in (True, "true", "on", "1", 1) else "" if raw is not None else setting.default
        else:
            value = str(raw if raw is not None else "").strip() if setting.kind != "textarea" else str(raw or "").strip()
            if not value and setting.secret and saved.get(setting.name):
                value = saved[setting.name]
            if not value and raw is None:
                value = setting.default
        if setting.kind == "number" and value and not value.isdigit():
            raise ConfigError(f"{setting.label}: {value!r} is not a number")
        if setting.kind == "select" and value and value not in [c[0] for c in setting.choices]:
            raise ConfigError(f"{setting.label}: {value!r} is not one of the choices")
        if setting.kind != "textarea" and any(ord(c) < 32 for c in value):
            raise ConfigError(f"{setting.label}: one line")
        if len(value) > 20000:
            raise ConfigError(f"{setting.label}: too long")
        if setting.required and not value:
            raise ConfigError(f"{setting.label} is required")
        if value:
            found[setting.name] = value
    if kind == "sftp" and not (found.get("pass") or found.get("key_pem")):
        raise ConfigError("SFTP: a password or a private key")
    if kind in ("drive", "dropbox", "onedrive"):
        try:
            json.loads(found["token"])
        except ValueError:
            raise ConfigError("Token: the JSON rclone authorize prints, {...}") from None
    return found


def parse_destination(name: str, data) -> Destination:
    where = f"destinations.{name}"
    if not NAME.fullmatch(name):
        raise ConfigError(f"{where}: a name is 1-32 lower case letters, digits, _ and -")
    data = data if isinstance(data, dict) else {}
    kind = str(data.get("kind") or "")
    path = str(data.get("path") or "").strip().strip("/")
    if not PATH.fullmatch(path) or ".." in path.split("/"):
        raise ConfigError(f"{where}.path: a folder (a bucket and a folder for S3), without ..")
    try:
        settings = check_settings(kind, data.get("settings") or {})
    except ConfigError as exc:
        raise ConfigError(f"{where}: {exc}") from None
    key = str(data.get("key") or "")
    try:
        pullcrypto.parse_key(key)
    except pullcrypto.PullerCryptoError:
        raise ConfigError(f"{where}.key: a key {pullcrypto.KEY_PREFIX}...") from None
    return Destination(name, kind, path, str(data.get("workspace") or ""), settings, key)


def _read(path: Path | None) -> dict:
    if path is None or not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: a mapping")
    return data


def _write(path: Path, data: dict) -> None:
    """Mode 600 from the start: the file holds passwords and tokens."""
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def load_destinations(path: Path) -> dict[str, Destination]:
    data = _read(path)
    found = data.get("destinations") or {}
    if not isinstance(found, dict):
        raise ConfigError(f"{path}: destinations must be a mapping")
    try:
        return {str(n): parse_destination(str(n), v) for n, v in found.items()}
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def save_destinations(path: Path, destinations: dict[str, Destination]) -> None:
    """The destinations written, the choices of the file (CHOICE_KEYS) kept as they are."""
    try:
        kept = {k: v for k, v in _read(path).items() if k in CHOICE_KEYS}
    except ConfigError:
        kept = {}
    _write(path, {"destinations": {n: d.to_dict() for n, d in sorted(destinations.items())}, **kept})


# --- where the backups of each instance go (AD-016 §16.1) -------------------
# Beside the destinations, in the same file: the machine's default (a destination of the machine, with when it was
# chosen) and the instances that chose another one ({<server>/<role>: {destination, since}}); an instance not there
# follows the default. The release before this one reads neither: it reads the destination still written in the
# backup step of the schedules (schedules.mirror_offsite), and its save of the destinations leaves these keys out.

CHOICE_KEYS = ("default", "instances", "keys_shown", "migrated")
# why an instance has the destination it has (or none)
FROM_TEST, FROM_STAGING, FROM_INSTANCE, FROM_DEFAULT, FROM_NONE = "test", "staging", "instance", "default", "none"


@dataclass
class Choices:
    default: str = ""
    default_since: str = ""
    # <server>/<role>: {"destination": name, "since": ISO time}
    instances: dict = field(default_factory=dict)
    # a destination's name: the fingerprint of the key that was shown (a destination made again has another key)
    keys_shown: dict = field(default_factory=dict)
    # when the destinations of the backup steps were carried here (the first start of this release)
    migrated: str = ""

    def choice(self, key: str) -> str:
        return str((self.instances.get(key) or {}).get("destination") or "")

    def to_dict(self) -> dict:
        data = {}
        if self.default:
            data["default"] = {"destination": self.default, "since": self.default_since}
        if self.instances:
            data["instances"] = {k: dict(v) for k, v in sorted(self.instances.items())}
        if self.keys_shown:
            data["keys_shown"] = dict(sorted(self.keys_shown.items()))
        if self.migrated:
            data["migrated"] = self.migrated
        return data


def load_choices(path: Path | None) -> Choices:
    """The choices of the file; a part that is not what it should be is left out (a broken file loses no
    destination for it)."""
    try:
        data = _read(path)
    except ConfigError:
        return Choices()
    found = Choices()
    default = data.get("default")
    if isinstance(default, dict) and NAME.fullmatch(str(default.get("destination") or "")):
        found.default, found.default_since = str(default["destination"]), str(default.get("since") or "")
    given = data.get("instances")
    for key, value in (given.items() if isinstance(given, dict) else ()):
        if isinstance(value, dict) and NAME.fullmatch(str(value.get("destination") or "")) and "/" in str(key):
            found.instances[str(key)] = {"destination": str(value["destination"]),
                                         "since": str(value.get("since") or "")}
    shown = data.get("keys_shown")
    if isinstance(shown, dict):
        found.keys_shown = {str(k): str(v) for k, v in shown.items()}
    found.migrated = str(data.get("migrated") or "")
    return found


def save_choices(path: Path, choices: Choices) -> None:
    """The choices written, the destinations of the file kept as they are. A choice written by this release marks the
    migration done (schedules.migrate_offsite): from then a backup step's destination is the mirror of the effective
    one, never a choice to carry."""
    choices.migrated = choices.migrated or now()
    _write(path, {"destinations": _read(path).get("destinations") or {}, **choices.to_dict()})


def now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def key_fingerprint(key: str) -> str:
    """What says a key was shown without being it: 16 hex characters of its SHA-256."""
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def key_shown(choices: Choices, dest: Destination) -> bool:
    return choices.keys_shown.get(dest.name) == key_fingerprint(dest.key)


@dataclass(frozen=True)
class Effective:
    """An instance's destination (None: none) and why (FROM_*); chosen, the name the instance chose when it has a
    choice (one it may no longer use, too)."""

    destination: Destination | None
    why: str
    chosen: str = ""

    @property
    def name(self) -> str:
        return self.destination.name if self.destination is not None else ""


def staging_servers(settings) -> set:
    """The servers the stagings file knows, as the map reads them."""
    base = settings.links_file or settings.servers_file
    return set(stagings.load(base.parent / stagings.FILE)) if base is not None else set()


def usable(dest: Destination | None, instance, settings) -> bool:
    """Whether instance's backups may go to dest: the machine's, or of a workspace its server is part of."""
    if dest is None:
        return False
    if not dest.workspace:
        return True
    ws = settings.workspaces.get(dest.workspace)
    return ws is not None and instance.server in ws.servers


def effective(settings, instance, found: dict | None = None, choices: Choices | None = None,
              staging: set | None = None) -> Effective:
    """THE destination of an instance's backups (AD-016 §16.1), the one function every reader asks (the
    schedule's copy, the Backups tab, the map, the Destinations page): a test or a staging none; else the
    instance's choice when that destination is there and it may use it; else the machine's default; else none."""
    if instance.role == "test":
        return Effective(None, FROM_TEST)
    if instance.server in (staging if staging is not None else staging_servers(settings)):
        return Effective(None, FROM_STAGING)
    path = settings.destinations_file
    if found is None:
        try:
            found = load_destinations(path) if path is not None else {}
        except ConfigError:
            found = {}
    choices = choices if choices is not None else load_choices(path)
    chosen = choices.choice(instance.key)
    if chosen and usable(found.get(chosen), instance, settings):
        return Effective(found[chosen], FROM_INSTANCE, chosen)
    default = found.get(choices.default) if choices.default else None
    if default is not None and not default.workspace:
        return Effective(default, FROM_DEFAULT, chosen)
    return Effective(None, FROM_NONE, chosen)


# --- rclone ---------------------------------------------------------------

def rclone_path() -> str | None:
    return shutil.which(RCLONE)


async def _obscured(text: str) -> str:
    """rclone obscure, the text on its input (never on a command line)."""
    code, out = await ops.capture([RCLONE, "obscure", "-"], input=text.encode(), timeout=30)
    if code:
        raise JobFailed(f"rclone obscure failed: {out[-300:]}")
    return out.strip().splitlines()[-1]


async def environment(dest: Destination, folder: Path) -> dict:
    """rclone's environment for one command: the remote's settings, an empty
    config file of its own (rclone writes a refreshed token there)."""
    _, rtype, settings = KINDS[dest.kind]
    prefix = f"RCLONE_CONFIG_{REMOTE.upper()}_"
    env = {**ops._ENV, "RCLONE_CONFIG": str(folder / "rclone.conf"), prefix + "TYPE": rtype}
    (folder / "rclone.conf").write_text("")
    for setting in settings:
        value = dest.settings.get(setting.name)
        if not value:
            continue
        if setting.obscure:
            value = await _obscured(value)
        if setting.name == "key_pem":
            # rclone reads it on one line, its line ends written \n
            value = value.replace("\r", "").strip().replace("\n", "\\n")
        env[prefix + setting.name.upper()] = value
    if dest.kind == "drive":
        env[prefix + "SCOPE"] = "drive"
    return env


def remote(dest: Destination, *parts: str) -> str:
    return f"{REMOTE}:" + "/".join(p.strip("/") for p in (dest.path, *parts) if p.strip("/"))


async def rclone(dest: Destination, *args: str, job: Job | None = None, timeout: float = LIST_TIMEOUT) -> str:
    """An rclone command on the destination: its output; JobFailed with
    rclone's last lines when it fails. With a job, the output in its log."""
    if rclone_path() is None:
        raise JobFailed("rclone is not installed on this machine (apt install rclone)")
    with tempfile.TemporaryDirectory(prefix="omniacli-rclone-") as tmp:
        env = await environment(dest, Path(tmp))
        cmd = [RCLONE, *args]
        if job is not None:
            before = len(job.lines)
            code = await ops.run(job, cmd, env=env, timeout=timeout, check=False)
            if code:
                # what rclone said last (its errors), without the time of the line
                said = [line.split(" ", 1)[-1] for line in job.lines[before + 1:] if line.strip()][-2:]
                raise JobFailed(f"rclone {args[0]} on {dest.name} ended with exit code {code}"
                                + (": " + " | ".join(said)[:600] if said else ""))
            return ""
        code, out = await ops.capture(cmd, env=env, timeout=timeout)
    if code:
        lines = [line for line in out.splitlines() if line.strip()]
        raise JobFailed(f"rclone {args[0]} on {dest.name}: " + " | ".join(lines[-3:])[:600])
    return out


async def check(dest: Destination) -> str:
    """The destination reached and written: its folder made, a file put and
    removed. What it holds, in a sentence."""
    probe = f".omniacli-probe-{secrets.token_hex(4)}"
    await rclone(dest, "mkdir", remote(dest))
    with tempfile.TemporaryDirectory(prefix="omniacli-probe-") as tmp:
        local = Path(tmp) / probe
        local.write_text("omniacli\n")
        await rclone(dest, "copyto", str(local), remote(dest, probe))
    await rclone(dest, "deletefile", remote(dest, probe))
    found = await listing(dest, "")
    return f"{len(found)} files in {dest.path}"


async def listing(dest: Destination, folder: str, suffix: str = SUFFIX) -> list[dict]:
    """The copies in a folder of the destination (their names end with
    suffix): [{name, size, time}], newest first; none when the folder is not
    there yet."""
    try:
        out = await rclone(dest, "lsjson", "--files-only", "--no-mimetype", remote(dest, folder))
    except JobFailed as exc:
        if "directory not found" in str(exc).lower() or "not found" in str(exc).lower():
            return []
        raise
    try:
        rows = json.loads(out[out.index("["):] if "[" in out else "[]")
    except ValueError:
        raise JobFailed(f"rclone lsjson on {dest.name}: not a list") from None
    found = []
    for row in rows:
        name = str(row.get("Name") or "")
        if not name.endswith(suffix):
            continue
        found.append({"name": name, "file": name[:-len(suffix)], "size": int(row.get("Size") or 0),
                      "time": copy_time(name, str(row.get("ModTime") or ""))})
    return sorted(found, key=lambda r: r["time"], reverse=True)


def copy_time(name: str, modified: str) -> str:
    """When the backup was made: the stamp in its name (an FTP server may not
    keep a file's time), else the time the destination says."""
    stamp = STAMP.search(name)
    if stamp:
        return datetime.datetime.strptime(stamp.group(1), "%Y%m%d_%H%M%S").isoformat(timespec="seconds")
    try:
        when = datetime.datetime.fromisoformat(modified.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return when.astimezone().replace(tzinfo=None).isoformat(timespec="seconds")


def instance_folder(instance) -> str:
    return f"{instance.server}/{instance.role}"


# the settings of a kind that say where a destination is (never a secret): what a customer does not read of the
# machine's destinations, with their names and folders (places, said)
WHERE_SETTINGS = ("host", "endpoint", "url", "account", "root_folder_id", "drive_id")
# how a customer reads one of the machine's destinations (AD-016 constraint 8)
OF_THE_SERVER = "a destination of the server"


def places(dest: Destination) -> set[str]:
    """What names a destination to who reads it: its name, its folder (one of a word alone, «backups», is a word of
    every job: left) and where it is (no secret)."""
    found = {dest.name, *(str(dest.settings.get(k) or "") for k in WHERE_SETTINGS)}
    if "/" in dest.path:
        found.add(dest.path)
    return {p for p in found if len(p) >= 3}


def said_to(settings, instance, dest: Destination) -> str:
    """How a job of instance names dest: its name, or OF_THE_SERVER when the instance is a workspace's and dest the
    machine's (its people read the job and the workspace's access log, AD-016 constraint 8)."""
    return OF_THE_SERVER if not dest.workspace and settings.workspace_of(instance.server) else ""


def _hidden(text: str, dest: Destination, said: str) -> str:
    for word in sorted(places(dest), key=len, reverse=True):
        text = text.replace(word, said)
    return text


async def upload(job: Job, dest: Destination, backup: Path, folder: str, said: str = "") -> str:
    """The backup encrypted with the destination's key next to it, copied
    there, the encrypted file deleted here: the name it has there. said: how the
    job names the destination ("" its name, with its folder)."""
    key = pullcrypto.parse_key(dest.key)
    sealed = backup.with_name(f".{backup.name}{SUFFIX}")
    job.log(f"--- encrypt {backup.name} for {said or dest.name} (AES-256-GCM)")
    try:
        await asyncio.to_thread(pullcrypto.encrypt_file, key, CONTEXT, backup, sealed)
        os.chmod(sealed, 0o600)
        name = backup.name + SUFFIX
        job.log(f"--- copy to {said}: {folder}/{name}" if said else
                f"--- copy to {dest.name} ({dest.label}): {dest.path}/{folder}/{name}")
        try:
            await rclone(dest, "copyto", "--retries", "3", "--low-level-retries", "10", "--stats", "60s",
                         "--stats-one-line", "-v", str(sealed), remote(dest, folder, name), job=job,
                         timeout=COPY_TIMEOUT)
        except JobFailed as exc:
            if not said:
                raise
            raise JobFailed(_hidden(str(exc), dest, said)) from None
    finally:
        sealed.unlink(missing_ok=True)
    return name


async def rotate(job: Job, dest: Destination, folder: str, prefix: str,
                 kept: Callable[[dict], set], suffix: str = SUFFIX, said: str = "") -> None:
    """The copies there whose name starts with prefix that kept ({name:
    datetime} -> the names kept: the schedule's rotation) does not keep, deleted."""
    try:
        found = {r["name"]: datetime.datetime.fromisoformat(r["time"]) for r in await listing(dest, folder, suffix)
                 if r["name"].startswith(prefix) and r["time"]}
        for name in sorted(set(found) - kept(found), key=lambda n: found[n]):
            job.log(f"--- delete {name} on {said or dest.name}: out of the rotation")
            await rclone(dest, "deletefile", remote(dest, folder, name))
    except JobFailed as exc:
        if not said:
            raise
        raise JobFailed(_hidden(str(exc), dest, said)) from None


async def fetch(job: Job, dest: Destination, folder: str, name: str, local_dir: Path, said: str = "") -> Path:
    """A copy brought back and decrypted into local_dir, as the backup it was."""
    try:
        return await _fetch(job, dest, folder, name, local_dir)
    except JobFailed as exc:
        if not said:
            raise
        raise JobFailed(_hidden(str(exc), dest, said)) from None


async def _fetch(job: Job, dest: Destination, folder: str, name: str, local_dir: Path) -> Path:
    if not name.endswith(SUFFIX) or "/" in name or name.startswith("."):
        raise JobFailed(f"{name} is not a copy of a backup")
    local_dir.mkdir(parents=True, exist_ok=True)
    target = local_dir / name[:-len(SUFFIX)]
    if target.exists():
        raise JobFailed(f"{target.name} is already in the backups here")
    sealed = local_dir / f".{name}"
    try:
        await rclone(dest, "copyto", "--retries", "3", "--stats", "60s", "--stats-one-line", "-v",
                     remote(dest, folder, name), str(sealed), job=job, timeout=COPY_TIMEOUT)
        job.log(f"--- decrypt {name}")
        try:
            await asyncio.to_thread(pullcrypto.decrypt_file, pullcrypto.parse_key(dest.key), CONTEXT, sealed, target)
        except pullcrypto.PullerCryptoError:
            raise JobFailed(f"{name} does not decrypt with the key of {dest.name}: another key, "
                            "or the file is damaged") from None
        os.chmod(target, 0o600)
    finally:
        sealed.unlink(missing_ok=True)
    job.log(f"{target.name} is in the backups ({target.stat().st_size} bytes)")
    return target
