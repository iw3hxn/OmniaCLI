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

from . import ops, pullcrypto
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


def load_destinations(path: Path) -> dict[str, Destination]:
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    found = data.get("destinations") or {}
    if not isinstance(found, dict):
        raise ConfigError(f"{path}: destinations must be a mapping")
    try:
        return {str(n): parse_destination(str(n), v) for n, v in found.items()}
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def save_destinations(path: Path, destinations: dict[str, Destination]) -> None:
    """Mode 600 from the start: the file holds passwords and tokens."""
    text = yaml.safe_dump({"destinations": {n: d.to_dict() for n, d in sorted(destinations.items())}},
                          sort_keys=False, allow_unicode=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


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


async def upload(job: Job, dest: Destination, backup: Path, folder: str) -> str:
    """The backup encrypted with the destination's key next to it, copied
    there, the encrypted file deleted here: the name it has there."""
    key = pullcrypto.parse_key(dest.key)
    sealed = backup.with_name(f".{backup.name}{SUFFIX}")
    job.log(f"--- encrypt {backup.name} for {dest.name} (AES-256-GCM)")
    try:
        await asyncio.to_thread(pullcrypto.encrypt_file, key, CONTEXT, backup, sealed)
        os.chmod(sealed, 0o600)
        name = backup.name + SUFFIX
        job.log(f"--- copy to {dest.name} ({dest.label}): {dest.path}/{folder}/{name}")
        await rclone(dest, "copyto", "--retries", "3", "--low-level-retries", "10", "--stats", "60s",
                     "--stats-one-line", "-v", str(sealed), remote(dest, folder, name), job=job, timeout=COPY_TIMEOUT)
    finally:
        sealed.unlink(missing_ok=True)
    return name


async def rotate(job: Job, dest: Destination, folder: str, prefix: str,
                 kept: Callable[[dict], set], suffix: str = SUFFIX) -> None:
    """The copies there whose name starts with prefix that kept ({name:
    datetime} -> the names kept: the schedule's rotation) does not keep, deleted."""
    found = {r["name"]: datetime.datetime.fromisoformat(r["time"]) for r in await listing(dest, folder, suffix)
             if r["name"].startswith(prefix) and r["time"]}
    for name in sorted(set(found) - kept(found), key=lambda n: found[n]):
        job.log(f"--- delete {name} on {dest.name}: out of the rotation")
        await rclone(dest, "deletefile", remote(dest, folder, name))


async def fetch(job: Job, dest: Destination, folder: str, name: str, local_dir: Path) -> Path:
    """A copy brought back and decrypted into local_dir, as the backup it was."""
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
