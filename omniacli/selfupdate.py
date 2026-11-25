# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""OmniaCLI updated from its own panel (README, Update OmniaCLI).

Where the package comes from is the main configuration's (only root writes
it): self_update.url, an https address of the OmniaCLI .deb (by default the
latest release of this fork, machines.PACKAGE_URL), and the public key its
signed manifest must verify with (self_update.public_key, else the key of
this package's own releases for its own repository, else plugins.public_key). Next to the package the publisher puts its signed
manifest (packagesign.py: OmniaCLI.deb.json and .json.sig): a package whose
manifest does not verify, or that is not the file the manifest signs, is
never installed. Read also there: self_update.version_url, the package of one
version ({version} in it; derived from a GitHub release address), and
self_update.releases, the GitHub API of the releases, whose notes the dialog
shows for every version between the installed and the new one.

Three parts:

1. The service (as its own user) reads what is published and its notes, and
   what root keeps (the folder STATE is readable by all): the dialog.
2. The root helper, `omniacli self-update prepare|start|check` through sudo
   (a line of /etc/sudoers.d/omniacli-self-update, written by postinst),
   downloads and verifies the new package into STATE/packages/<version>/,
   keeps the package of the installed version too (signed, or else the
   release's package checked file by file against what dpkg installed: the
   way back), and starts the install in a transient systemd unit
   (systemd-run) that outlives OmniaCLI's own restart.
3. That unit (selfupdate_unit.py, run with the system's python3) writes
   the facts before and after and its outcome in STATE/runs/<job>.*; the
   restarted OmniaCLI reads them and finishes the job.

The way back is the same: the kept package of the version before, offered
KEEP_DAYS after the install of the current one, installed in the same unit.
"""

from __future__ import annotations

import asyncio
import datetime
import fnmatch
import hashlib
import heapq
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
import yaml

from .config import OMNIACLI_COMMAND
from .jobs import INTERRUPTED, Job, JobFailed
from .ops import capture
from .packagesign import MANIFEST_SUFFIX, MAX_SMALL_FILE, SIGNATURE_SUFFIX, SigningError, file_sha256, verified_manifest, \
    verify_local

PACKAGE = "omniacli"
DEFAULT_URL = "https://github.com/iw3hxn/OmniaCLI/releases/latest/download/OmniaCLI.deb"
# the key the releases of DEFAULT_URL's repository are signed with (its private half is the publisher's, in the
# repository's secrets): a machine updating from there needs no key in its configuration
RELEASES_REPO = "iw3hxn/OmniaCLI"
RELEASES_PUBLIC_KEY = "ed25519:jXyBt0nj4amn3nOnxyAN/OP9i8gAaTGefKwUdEQRSQk="
# root's folder of the packages kept and of the runs (purged by postrm); readable by the service
STATE = Path(os.environ.get("OMNIACLI_SELF_UPDATE_DIR", "/var/lib/omniacli-self-update"))
# the main configuration the root helper reads the source from: fixed, sudo passes no environment
SYSTEM_CONFIG = Path(os.environ.get("OMNIACLI_SYSTEM_CONFIG", "/etc/omniacli/omniacli.yaml"))
DPKG_INFO = Path("/var/lib/dpkg/info")
DPKG_CONFIG = Path("/etc/dpkg")
# where the files of the installed package are (a folder of the test in the tests)
INSTALLED_ROOT = Path("/")
UNIT_SCRIPT = Path(__file__).with_name("selfupdate_unit.py")
SYSTEM_PYTHON = "/usr/bin/python3"
# the way back is offered so many days after the install of the current version
KEEP_DAYS = 14
# a package is not larger than this (the release's is about 2 MB)
MAX_PACKAGE = 200 * 1024 * 1024
CONNECT_TIMEOUT = 15.0
READ_TIMEOUT = 120.0
REDIRECTS = 5
NOTES_TTL = 600
JOB_ID = re.compile(r"\d{8}-\d{6}-[0-9a-f]{6}")
VERSION = re.compile(r"[0-9][0-9A-Za-z.+~]{0,63}")
GITHUB = re.compile(r"https://github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)/releases/"
                    r"(?:latest/download|download/[^/]+)/(?P<file>[^/?#]+)")
TAG = re.compile(r"(?:omniacli-)?v(?P<version>[0-9][0-9A-Za-z.+~]*)")
# how long the service waits for the unit's outcome (the venv is built from PyPI during the install)
OUTCOME_TIMEOUT = 1800


class SelfUpdateError(Exception):
    """What the admin is told: why OmniaCLI is not updated."""


@dataclass(frozen=True)
class Source:
    url: str
    public_key: str
    version_url: str = ""
    releases: str = ""

    @property
    def file(self) -> str:
        return Path(urlsplit(self.url).path).name


def _https(value, what: str) -> str:
    text = str(value or "").strip()
    parts = urlsplit(text)
    if parts.scheme != "https" or not parts.hostname or re.search(r"[\s'\"<>]", text):
        raise SelfUpdateError(f"self_update.{what}: {text!r} is not an https:// address")
    return text


def parse_source(data, plugins_key: str = "", default_url: str = DEFAULT_URL) -> Source:
    """self_update of the main configuration, with its defaults."""
    data = data if isinstance(data, dict) else {}
    url = _https(data.get("url") or default_url, "url")
    found = GITHUB.fullmatch(url)
    # the releases of this package's own repository are verified with its key, before plugins.public_key (a
    # plugin's publisher signs plugins, not OmniaCLI): self_update.public_key, written, still wins
    ours = RELEASES_PUBLIC_KEY if found and f"{found['owner']}/{found['repo']}".lower() == RELEASES_REPO.lower() else ""
    key = str(data.get("public_key") or ours or plugins_key or "").strip()
    version_url = data.get("version_url") or (
        f"https://github.com/{found['owner']}/{found['repo']}/releases/download/omniacli-v{{version}}/{found['file']}"
        if found else "")
    if version_url:
        version_url = _https(version_url, "version_url")
        if "{version}" not in version_url:
            raise SelfUpdateError("self_update.version_url: it names the version with {version}")
    releases = data.get("releases") or (
        f"https://api.github.com/repos/{found['owner']}/{found['repo']}/releases?per_page=100" if found else "")
    if releases:
        releases = _https(releases, "releases")
    return Source(url, key, version_url, releases)


def system_source(config: Path | None = None) -> Source:
    """The source as root reads it, from the main configuration only."""
    config = config or SYSTEM_CONFIG
    try:
        data = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise SelfUpdateError(f"cannot read {config}: {exc}") from None
    return parse_source(data.get("self_update"), str((data.get("plugins") or {}).get("public_key") or ""),
                        _default_url())


def _default_url() -> str:
    return os.environ.get("OMNIACLI_PACKAGE_URL") or DEFAULT_URL


def version_key(version: str) -> tuple:
    """2026.1.5502 > 2026.1.5477 > 2026.1.0~check: the numbers, then 0, or -1 for a ~ version."""
    head, tilde, _ = str(version).partition("~")
    return (*[int(n) for n in re.findall(r"\d+", head)], -1 if tilde else 0)


def newer(version: str, than: str) -> bool:
    return version_key(version) > version_key(than)


# --- the download, https only ------------------------------------------------------


def _client() -> httpx.Client:
    # the system's certificates (an admin's own authority is trusted as curl and apt trust it)
    return httpx.Client(timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT),
                        verify=ssl.create_default_context(), follow_redirects=False,
                        headers={"User-Agent": "OmniaCLI self-update"})


def _follow(client: httpx.Client, url: str, stream: bool = False):
    """The response at url after its redirects, each to an https address;
    a stream is the caller's to close."""
    for _ in range(REDIRECTS + 1):
        _https(url, "url")
        try:
            response = client.send(client.build_request("GET", url), stream=stream)
        except httpx.HTTPError as exc:
            raise SelfUpdateError(f"{url}: {type(exc).__name__}: {exc}") from None
        if response.status_code in (301, 302, 303, 307, 308) and response.headers.get("location"):
            response.close()
            url = urljoin(url, response.headers["location"])
            if urlsplit(url).scheme != "https":
                raise SelfUpdateError(f"the download is sent to {url}: not https, refused")
            continue
        return response
    raise SelfUpdateError(f"{url}: more than {REDIRECTS} redirects")


def _small(client: httpx.Client, url: str, limit: int = MAX_SMALL_FILE) -> bytes | None:
    """The file at url, None when there is none (404)."""
    response = _follow(client, url)
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise SelfUpdateError(f"{url}: HTTP {response.status_code}")
    if len(response.content) > limit:
        raise SelfUpdateError(f"{url}: {len(response.content)} bytes, more than a file of its kind")
    return response.content


def signed_manifest(client: httpx.Client, source: Source, url: str) -> tuple[dict, bytes, bytes] | None:
    """The manifest of the package at url verified with the source's key, and
    its bytes and signature; None when the package has no manifest there."""
    manifest = _small(client, url + MANIFEST_SUFFIX)
    if manifest is None:
        return None
    signature = _small(client, url + SIGNATURE_SUFFIX)
    if signature is None:
        raise SelfUpdateError(f"{url}{MANIFEST_SUFFIX} has no signature next to it ({SIGNATURE_SUFFIX}): not installed")
    if not source.public_key:
        raise SelfUpdateError("no key to verify the package with: self_update.public_key (or plugins.public_key) of "
                              "the main configuration is empty")
    try:
        data = verified_manifest(source.public_key, manifest, signature, Path(urlsplit(url).path).name)
    except SigningError as exc:
        raise SelfUpdateError(f"{url}: {exc}: not installed") from None
    if not VERSION.fullmatch(data["version"]):
        raise SelfUpdateError(f"{url}: the signed version {data['version']!r} is not a version")
    if data.get("package") not in (None, PACKAGE):
        raise SelfUpdateError(f"{url}: the signed manifest is of the package {data.get('package')}, not {PACKAGE}")
    return data, manifest, signature


def download(client: httpx.Client, url: str, dest: Path, size: int | None = None, sha256: str | None = None) -> str:
    """The file at url to dest, through .part; checked against size and
    sha256 when given. Its sha256."""
    part = dest.with_name(dest.name + ".part")
    digest, got = hashlib.sha256(), 0
    limit = size if size is not None else MAX_PACKAGE
    response = _follow(client, url, stream=True)
    try:
        if response.status_code != 200:
            raise SelfUpdateError(f"{url}: HTTP {response.status_code}")
        with open(part, "wb") as fh:
            for chunk in response.iter_bytes(1 << 20):
                got += len(chunk)
                if got > limit:
                    raise SelfUpdateError(f"{url}: larger than the {limit} bytes "
                                          + ("signed" if size is not None else "a package may have"))
                fh.write(chunk)
                digest.update(chunk)
        if (size is not None and got != size) or (sha256 is not None and digest.hexdigest() != sha256):
            raise SelfUpdateError(f"{url}: {got} bytes, sha256 {digest.hexdigest()}: not the file the manifest "
                                  f"signs ({size} bytes, {sha256}): not installed")
        part.chmod(0o644)
        os.replace(part, dest)
    except httpx.HTTPError as exc:
        raise SelfUpdateError(f"{url}: {type(exc).__name__}: {exc}") from None
    finally:
        response.close()
        part.unlink(missing_ok=True)
    return digest.hexdigest()


# --- what is published, for the dialog (the service) -----------------------------------

_cache: dict = {}


def published(source: Source, installed: str) -> dict:
    """{version, built, signed, error, notes}: the package at the source's
    address as its signed manifest says; without a manifest the latest
    release's version, not signed (it cannot be installed from here)."""
    found = {"version": None, "built": None, "size": None, "signed": False, "error": None, "notes": []}
    with _client() as client:
        try:
            signed = signed_manifest(client, source, source.url)
            if signed is not None:
                found.update(version=signed[0]["version"], built=signed[0].get("built"), size=signed[0]["size"],
                             signed=True)
            else:
                found["error"] = (f"{source.url} has no signed manifest ({source.file}{MANIFEST_SUFFIX}): "
                                  "it cannot be installed from here")
        except SelfUpdateError as exc:
            found["error"] = str(exc)
        releases = []
        if source.releases:
            try:
                releases = _releases(client, source.releases)
            except SelfUpdateError:
                releases = []
    if found["version"] is None and releases:
        found["version"] = releases[0]["version"]
    if found["version"]:
        found["notes"] = [r for r in releases if newer(r["version"], installed)
                          and not newer(r["version"], found["version"])]
    return found


def _releases(client: httpx.Client, url: str) -> list[dict]:
    """The releases of the GitHub API, newest first: {version, name, body, url, published}."""
    raw = _small(client, url, 4 << 20)
    if raw is None:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        raise SelfUpdateError(f"{url} is not JSON") from None
    found = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict) or item.get("draft") or item.get("prerelease"):
            continue
        tag = TAG.fullmatch(str(item.get("tag_name") or ""))
        if not tag:
            continue
        found.append({"version": tag["version"], "name": str(item.get("name") or ""),
                      "body": str(item.get("body") or "")[:4000],
                      "url": str(item.get("html_url") or ""), "published": str(item.get("published_at") or "")})
    found.sort(key=lambda r: version_key(r["version"]), reverse=True)
    return found


def published_cached(source: Source, installed: str, fresh: bool = False) -> dict:
    key = (source, installed)
    hit = _cache.get(key)
    if hit and not fresh and time.monotonic() - hit[0] < NOTES_TTL:
        return hit[1]
    found = published(source, installed)
    _cache[key] = (time.monotonic(), found)
    return found


# --- what root keeps (read by the service too) ------------------------------------


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def kept(state: Path | None = None) -> dict[str, dict]:
    """The packages kept: {version: kept.json}."""
    found = {}
    for path in sorted(((state or STATE) / "packages").glob("*/kept.json")):
        data = _read_json(path)
        if data and data.get("version") == path.parent.name:
            found[path.parent.name] = data
    return found


def installed_at(dpkg_info: Path | None = None) -> float | None:
    """When the installed package was installed: dpkg writes its file list then."""
    try:
        return ((dpkg_info or DPKG_INFO) / f"{PACKAGE}.list").stat().st_mtime
    except OSError:
        return None


def way_back(installed: str, state: Path | None = None, dpkg_info: Path | None = None) -> dict | None:
    """{version, until}: the newest package kept older than the installed
    one, while KEEP_DAYS have not passed since the install."""
    since = installed_at(dpkg_info)
    if since is None:
        return None
    until = since + KEEP_DAYS * 86400
    if time.time() > until:
        return None
    older = [v for v in kept(state) if newer(installed, v)]
    if not older:
        return None
    version = max(older, key=version_key)
    return {"version": version, "until": datetime.datetime.fromtimestamp(until).isoformat(timespec="seconds")}


def run_of(job_id: str, state: Path | None = None) -> dict | None:
    if not JOB_ID.fullmatch(job_id or ""):
        return None
    return _read_json((state or STATE) / "runs" / f"{job_id}.json")


def outcome_of(job_id: str, state: Path | None = None) -> dict | None:
    if not JOB_ID.fullmatch(job_id or ""):
        return None
    return _read_json((state or STATE) / "runs" / f"{job_id}.outcome.json")


def unit_lines(job_id: str, state: Path | None = None) -> list[str]:
    if not JOB_ID.fullmatch(job_id or ""):
        return []
    try:
        return ((state or STATE) / "runs" / f"{job_id}.log").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []


def runs(state: Path | None = None, within: float = 86400) -> list[dict]:
    """The runs of the last day, newest last."""
    found = []
    for path in sorted(((state or STATE) / "runs").glob("*.json")):
        if path.name.endswith(".outcome.json") or time.time() - path.stat().st_mtime > within:
            continue
        data = _read_json(path)
        if data and JOB_ID.fullmatch(str(data.get("job") or "")):
            found.append(data)
    return found


def last_away(state: Path | None = None) -> float | None:
    """How long the panel was away at the last install that measured it."""
    folder = (state or STATE) / "runs"
    for path in sorted(folder.glob("*.outcome.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        data = _read_json(path)
        if data and isinstance(data.get("away_seconds"), (int, float)):
            return float(data["away_seconds"])
    return None


# --- the root helper ------------------------------------------------------------


def _run(cmd: list[str], timeout: int = 120) -> tuple[int, str]:
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)
    return done.returncode, (done.stdout + done.stderr).strip()


def installed_version() -> str:
    code, out = _run(["dpkg-query", "-W", "-f=${Status}|${Version}", PACKAGE])
    if code or "|" not in out or not out.split("|", 1)[0].endswith(" installed"):
        raise SelfUpdateError("OmniaCLI is not installed from its package here: it is updated where it was installed")
    return out.split("|", 1)[1]


def deb_field(deb: Path, name: str) -> str:
    code, out = _run(["dpkg-deb", "-f", str(deb), name])
    if code or not out:
        raise SelfUpdateError(f"{deb.name}: dpkg-deb cannot read its {name}: {out or 'empty'}")
    return out


def _check_deb(deb: Path, version: str) -> None:
    if deb_field(deb, "Package") != PACKAGE:
        raise SelfUpdateError(f"{deb.name} is the package {deb_field(deb, 'Package')}, not {PACKAGE}: not installed")
    if deb_field(deb, "Version") != version:
        raise SelfUpdateError(f"{deb.name} is version {deb_field(deb, 'Version')}, its manifest says {version}: "
                              "not installed")


def _folder(path: Path) -> Path:
    """A folder of root's: made 755, never through a link."""
    path.mkdir(mode=0o755, parents=True, exist_ok=True)
    info = path.lstat()
    if path.is_symlink() or (os.geteuid() == 0 and info.st_uid != 0):
        raise SelfUpdateError(f"{path} is not a folder of root's: nothing done")
    return path


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.chmod(0o644)
    os.replace(tmp, path)


def _conffiles() -> dict[str, str]:
    """{path: md5} of the package's configuration files as dpkg installed them."""
    code, out = _run(["dpkg-query", "-W", "-f=${Conffiles}", PACKAGE])
    found = {}
    for line in out.splitlines() if code == 0 else []:
        parts = line.split()
        if len(parts) >= 2:
            found[parts[0]] = parts[1]
    return found


def dpkg_rules(folder: Path | None = None) -> list[tuple[str, str]]:
    """dpkg's path-exclude and path-include, in the order dpkg reads them (dpkg.cfg.d/*, then dpkg.cfg)."""
    folder = folder or DPKG_CONFIG
    found = []
    for path in [*sorted((folder / "dpkg.cfg.d").glob("*")), folder / "dpkg.cfg"]:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            rule = re.fullmatch(r"\s*(path-exclude|path-include)[=\s]\s*(\S+)\s*", line)
            if rule:
                found.append((rule[1], rule[2]))
    return found


def dpkg_excluded(path: str, rules: list[tuple[str, str]]) -> bool:
    """Whether dpkg leaves path out: the last rule that matches decides (a glob whose * matches / too)."""
    excluded = False
    for kind, pattern in rules:
        if fnmatch.fnmatchcase(path, pattern):
            excluded = kind == "path-exclude"
    return excluded


def matches_installed(deb: Path, version: str) -> None:
    """The package is the installed one, file by file (its own files, its
    maintainer scripts; a configuration file as dpkg installed it, before
    any change made there), else SelfUpdateError: a package of a release
    that has no signature is kept for the way back only so."""
    _check_deb(deb, version)
    conffiles = _conffiles()
    with tempfile.TemporaryDirectory(dir=deb.parent) as tmp:
        tree = Path(tmp) / "x"
        code, out = _run(["dpkg-deb", "-R", str(deb), str(tree)], timeout=300)
        if code:
            raise SelfUpdateError(f"{deb.name}: dpkg-deb cannot unpack it: {out}")
        for script in ("preinst", "postinst", "prerm", "postrm"):
            ours, theirs = tree / "DEBIAN" / script, DPKG_INFO / f"{PACKAGE}.{script}"
            if ours.exists() != theirs.exists() or (ours.exists() and ours.read_bytes() != theirs.read_bytes()):
                raise SelfUpdateError(f"{deb.name}: its {script} is not the installed one")
        rules = dpkg_rules()
        for path in sorted(tree.rglob("*")):
            relative = path.relative_to(tree)
            if relative.parts[0] == "DEBIAN" or (path.is_dir() and not path.is_symlink()):
                continue
            target = "/" + relative.as_posix()
            there = INSTALLED_ROOT / relative
            if not there.exists() and not there.is_symlink() and dpkg_excluded(target, rules):
                # a minimal image's dpkg leaves out the documentation (path-exclude): it is not installed there
                continue
            if path.is_symlink():
                if not there.is_symlink() or os.readlink(there) != os.readlink(path):
                    raise SelfUpdateError(f"{deb.name}: {target} is not the installed link")
            elif target in conffiles:
                if hashlib.md5(path.read_bytes()).hexdigest() != conffiles[target]:  # noqa: S324 - dpkg's own md5
                    raise SelfUpdateError(f"{deb.name}: {target} is not the configuration file dpkg installed")
            elif there.is_symlink() or not there.is_file() or file_sha256(there) != file_sha256(path):
                raise SelfUpdateError(f"{deb.name}: {target} is not the installed file")


def _keep_signed(client, source: Source, url: str, version: str, folder: Path) -> dict | None:
    """The package at url with its signed manifest into folder; None when it has no manifest."""
    signed = signed_manifest(client, source, url)
    if signed is None:
        return None
    manifest, manifest_bytes, signature = signed
    if manifest["version"] != version:
        raise SelfUpdateError(f"{url} is version {manifest['version']}, not {version}")
    deb = folder / manifest["file"]
    sha = download(client, url, deb, manifest["size"], manifest["sha256"])
    deb.with_name(deb.name + MANIFEST_SUFFIX).write_bytes(manifest_bytes)
    deb.with_name(deb.name + SIGNATURE_SUFFIX).write_bytes(signature)
    _check_deb(deb, version)
    return {"version": version, "file": deb.name, "sha256": sha, "size": manifest["size"], "how": "signed",
            "built": manifest.get("built"), "kept": datetime.datetime.now().isoformat(timespec="seconds"), "url": url}


def keep_installed(client, source: Source, installed: str, state: Path) -> dict:
    """The package of the installed version kept for the way back."""
    folder = state / "packages" / installed
    record = _read_json(folder / "kept.json")
    if record and (folder / record.get("file", "")).is_file():
        return record
    if not source.version_url:
        raise SelfUpdateError(f"the package of the installed {installed} is not kept, and self_update.version_url "
                              "gives no address for one version: no way back, not updated")
    url = source.version_url.format(version=installed)
    _folder(folder)
    try:
        record = _keep_signed(client, source, url, installed, folder)
        if record is None:
            deb = folder / Path(urlsplit(url).path).name
            sha = download(client, url, deb)
            matches_installed(deb, installed)
            record = {"version": installed, "file": deb.name, "sha256": sha, "size": deb.stat().st_size,
                      "how": "installed", "kept": datetime.datetime.now().isoformat(timespec="seconds"), "url": url}
    except SelfUpdateError as exc:
        shutil.rmtree(folder, ignore_errors=True)
        raise SelfUpdateError(f"the package of the installed {installed} cannot be kept for the way back ({exc}): "
                              "not updated") from None
    _write_json(folder / "kept.json", record)
    return record


def verify_kept(version: str, source: Source, state: Path) -> Path:
    """The kept package of version, checked again: its signature, or its sha256 as root kept it."""
    folder = state / "packages" / version
    record = _read_json(folder / "kept.json")
    if not record or not VERSION.fullmatch(version):
        raise SelfUpdateError(f"no package of {version} is kept")
    deb = folder / str(record.get("file") or "")
    if deb.is_symlink() or not deb.is_file():
        raise SelfUpdateError(f"no package of {version} is kept")
    if record.get("how") == "signed":
        try:
            manifest = verify_local(source.public_key, deb)
        except SigningError as exc:
            raise SelfUpdateError(f"the kept package of {version}: {exc}: not installed") from None
        if manifest["version"] != version:
            raise SelfUpdateError(f"the kept package of {version} is signed as {manifest['version']}: not installed")
    elif file_sha256(deb) != record.get("sha256"):
        raise SelfUpdateError(f"the kept package of {version} changed since it was kept: not installed")
    _check_deb(deb, version)
    return deb


def prepare(source: Source | None = None, state: Path | None = None) -> dict:
    """The new package downloaded and verified, the installed one kept: what the start installs."""
    source = source or system_source()
    state = _folder(state or STATE)
    installed = installed_version()
    with _client() as client:
        signed = signed_manifest(client, source, source.url)
        if signed is None:
            raise SelfUpdateError(f"{source.url} has no signed manifest ({source.file}{MANIFEST_SUFFIX}): "
                                  "an unsigned package is never installed")
        manifest = signed[0]
        new = manifest["version"]
        if not newer(new, installed):
            raise SelfUpdateError(f"{source.url} is version {new}, and {installed} is installed: nothing newer")
        previous = keep_installed(client, source, installed, state)
        folder = _folder(state / "packages" / new)
        try:
            record = _keep_signed(client, source, source.url, new, folder)
        except SelfUpdateError:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        _write_json(folder / "kept.json", record)
    # one version before is kept: the others go
    for other in (state / "packages").iterdir():
        if other.name not in (installed, new) and other.is_dir() and not other.is_symlink():
            shutil.rmtree(other, ignore_errors=True)
    _write_json(state / "pending.json", {"from": installed, "to": new,
                                         "at": datetime.datetime.now().isoformat(timespec="seconds")})
    return {"from": installed, "to": new, "size": record["size"], "sha256": record["sha256"],
            "built": record.get("built"), "previous": {"version": installed, "how": previous["how"]}}


def service_port(defaults: Path = Path("/etc/default/omniacli")) -> int:
    try:
        for line in defaults.read_text(encoding="utf-8").splitlines():
            found = re.fullmatch(r"\s*OMNIACLI_PORT=[\"']?(\d+)[\"']?\s*", line)
            if found:
                return int(found[1])
    except OSError:
        pass
    return 8090


def start(mode: str, job_id: str, source: Source | None = None, state: Path | None = None) -> dict:
    """The install (update: the package prepare kept; back: the version
    before, kept) in a transient unit of systemd."""
    if mode not in ("update", "back"):
        raise SelfUpdateError("self-update start update|back <job>")
    if not JOB_ID.fullmatch(job_id or ""):
        raise SelfUpdateError(f"{job_id!r} is not a job")
    source = source or system_source()
    state = _folder(state or STATE)
    installed = installed_version()
    if mode == "update":
        pending = _read_json(state / "pending.json") or {}
        if pending.get("from") != installed or not pending.get("to"):
            raise SelfUpdateError("nothing downloaded to install: download and verify it first")
        target = str(pending["to"])
    else:
        back = way_back(installed, state)
        if back is None:
            raise SelfUpdateError(f"no version before {installed} is kept (it is kept {KEEP_DAYS} days)")
        target = back["version"]
    deb = verify_kept(target, source, state)
    previous = verify_kept(installed, source, state) if mode == "update" else None
    runs_dir = _folder(state / "runs")
    unit = f"omniacli-self-update-{job_id}"
    run = {"job": job_id, "mode": mode, "from": installed, "to": target, "deb": str(deb),
           "previous_deb": str(previous) if previous else "", "port": service_port(), "unit": unit,
           "started": datetime.datetime.now().isoformat(timespec="seconds")}
    run_file = runs_dir / f"{job_id}.json"
    _write_json(run_file, run)
    script = state / "unit.py"
    shutil.copyfile(UNIT_SCRIPT, script)
    script.chmod(0o644)
    python = SYSTEM_PYTHON if os.path.exists(SYSTEM_PYTHON) else (shutil.which("python3") or SYSTEM_PYTHON)
    code, out = _run(["systemd-run", "--unit", unit, "--collect", "--description", f"OmniaCLI {installed} -> {target}",
                      "--setenv=DEBIAN_FRONTEND=noninteractive", python, "-I", str(script), str(run_file)])
    if code:
        raise SelfUpdateError(f"systemd-run cannot start the install: {out}")
    if mode == "update":
        (state / "pending.json").unlink(missing_ok=True)
    return {"unit": unit, "from": installed, "to": target, "mode": mode}


def helper_main(argv: list[str]) -> int:
    """omniacli self-update check | prepare | start update|back <job>: as root, through sudo."""
    try:
        if os.geteuid() != 0:
            raise SelfUpdateError("run it as root, through sudo")
        if argv == ["check"]:
            found = {"ok": True, "installed": installed_version()}
        elif argv == ["prepare"]:
            found = prepare()
        elif len(argv) == 3 and argv[0] == "start":
            found = start(argv[1], argv[2])
        else:
            raise SelfUpdateError("usage: omniacli self-update check | prepare | start update|back <job>")
    except SelfUpdateError as exc:
        print(f"self-update: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(found))
    return 0


# --- the job (the service) ----------------------------------------------------------

def helper_command(*args: str) -> list[str]:
    return ["sudo", "-n", str(OMNIACLI_COMMAND), "self-update", *args]


async def ask(*args: str, timeout: float = 900) -> dict:
    """The root helper's answer; JobFailed with its reason."""
    code, out = await capture(helper_command(*args), timeout=timeout)
    if code:
        raise JobFailed((out or f"self-update {args[0]} failed ({code})").removeprefix("self-update: ").strip())
    try:
        return json.loads(out.splitlines()[-1])
    except (ValueError, IndexError):
        raise JobFailed(f"self-update {args[0]} answered no JSON: {out}") from None


def helper_refused(text: str) -> bool:
    """sudo's own refusal: the package's sudoers line is not there (an OmniaCLI run from a checkout)."""
    return text.startswith("sudo:") or "is not allowed to run sudo" in text or "password is required" in text


def title(mode: str, from_version: str, to_version: str) -> str:
    return (f"Update OmniaCLI {from_version} → {to_version}" if mode == "update"
            else f"OmniaCLI back to {to_version} (from {from_version})")


async def run_job(job: Job, mode: str, record, helper=None, state: Path | None = None) -> None:
    """The job of an update or of the way back: the package downloaded and
    verified by root, the install started in its unit, then its outcome
    waited for (OmniaCLI usually stops before: the restarted one waits on,
    to_resume). record(detail): an entry of the access log."""
    helper = helper or ask
    if mode == "update":
        job.log("--- download and verify the package (root, from self_update.url of the main configuration)")
        done = await helper("prepare")
        job.log(f"OmniaCLI {done['to']}: {done['size']} bytes, sha256 {done['sha256']}, signed with this machine's key"
                + (f", built {done['built']}" if done.get("built") else ""))
        job.log(f"the package of {done['from']} is kept for the way back ("
                + ("signed" if done["previous"]["how"] == "signed" else "checked file by file against the installed one")
                + ")")
        record(f"OmniaCLI update {done['from']} -> {done['to']}: downloaded and verified (sha256 {done['sha256']})")
    started = await helper("start", mode, job.id)
    job.result["self_update"] = {"mode": mode, "from": started["from"], "to": started["to"], "unit": started["unit"]}
    away = last_away(state)
    job.log(f"--- install OmniaCLI {started['to']} in the unit {started['unit']}, apart from OmniaCLI: "
            "OmniaCLI stops and comes back" + (f" (the last time it was away {away:.0f} s)" if away else ""))
    await wait_outcome(job, record, state=state)


async def wait_outcome(job: Job, record, state: Path | None = None, timeout: float = OUTCOME_TIMEOUT,
                       every: float = 1.0) -> None:
    """The unit's outcome, once it is written: the job finished with it."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = outcome_of(job.id, state)
        if found is not None:
            finish(job, found, record, state)
            return
        await asyncio.sleep(every)
    raise JobFailed(f"no outcome of the install after {timeout:.0f} s: see journalctl -u omniacli-self-update-{job.id}")


def moment(when: str) -> str:
    """A start time as the unit read it, for the job's line: systemd's @<seconds> made a moment of this
    machine's clock (the pages then say it in the reader's zone), Docker's (ISO, with Z) as it is."""
    found = re.fullmatch(r"@(\d+)", str(when or "").strip())
    if found:
        return datetime.datetime.fromtimestamp(int(found[1])).isoformat(timespec="seconds")
    return str(when or "")


def finish(job: Job, outcome: dict, record, state: Path | None = None) -> None:
    """The unit's lines, the facts before and after, and the outcome, in the job."""
    # the unit's lines among the job's by their time (the job's own went on before and after OmniaCLI's restart)
    job.lines[:] = list(heapq.merge(job.lines, unit_lines(job.id, state), key=lambda line: line[:8]))
    for label in ("before", "after"):
        facts = outcome.get(label) or {}
        omnia = facts.get("omniacli") or {}
        job.log(f"--- the facts {label}: OmniaCLI {omnia.get('version') or '?'} {omnia.get('state') or '?'}, "
                f"login page HTTP {facts.get('login')}"
                + (f", nginx -t {facts['nginx']['test']}, its files {facts['nginx']['files']}" if facts.get("nginx") else ""))
        for what in ("units", "containers"):
            for name, when in sorted((facts.get(what) or {}).items()):
                job.log(f"{what[:-1]} {name} started {moment(when)}")
    if outcome.get("apt_exit"):
        for line in outcome.get("apt_tail") or []:
            job.log(f"apt: {line}")
    if isinstance(outcome.get("away_seconds"), (int, float)):
        job.log(f"the panel was away {outcome['away_seconds']:.0f} s (the install took "
                f"{outcome.get('install_seconds') or 0:.0f} s)")
    summary = job.result.setdefault("self_update", {})
    summary.update({"mode": outcome.get("mode"), "from": outcome.get("from"), "to": outcome.get("to"),
                    "ok": bool(outcome.get("ok")), "away_seconds": outcome.get("away_seconds"), "done": True})
    if not outcome.get("ok"):
        back = outcome.get("went_back")
        record(f"OmniaCLI {outcome.get('mode')} {outcome.get('from')} -> {outcome.get('to')} failed: "
               f"{outcome.get('error')}" + (f"; back to {outcome.get('from')}" if back else ""))
        raise JobFailed(str(outcome.get("error")) + (f": OmniaCLI {outcome.get('from')} was installed again" if back else ""))
    for warning in outcome.get("warnings") or []:
        job.warn(f"changed during the install: {warning}")
    if outcome.get("mode") == "back":
        record(f"OmniaCLI back to {outcome.get('to')} (from {outcome.get('from')})")
    else:
        record(f"OmniaCLI {outcome.get('from')} -> {outcome.get('to')} installed")
    job.log(f"--- OmniaCLI {outcome.get('to')} answers")


def to_resume(jobs, state: Path | None = None) -> list[Job]:
    """The jobs of the runs of the last day that OmniaCLI's restart interrupted and whose outcome it has not read."""
    found = []
    for run in runs(state):
        job = jobs.get(run["job"])
        if job is not None and job.status == INTERRUPTED and not (job.result.get("self_update") or {}).get("done"):
            found.append(job)
    return found
