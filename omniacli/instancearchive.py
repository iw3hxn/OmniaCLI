# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The full archive of an instance: what it takes to make it again on
another machine, in one zip of its backups folder
(archive_<backup>.zip, mode 600):

    manifest.json          what it is: the instance, its Odoo, when, and the
                           SHA-256 of every other file
    backup.zip             the Odoo backup (database and filestore)
    repositories.json      each repository: its address (never a user nor a
                           token), its branch, the commit checked out
    code/<repo>.tar.gz     the code of that commit (git archive: the files,
                           not the history)
    config/...             the plugin's files: compose.yaml, odoo.conf, the
                           Python packages installed (pip freeze)...

A file made elsewhere is never trusted as one of these: the zip is only
written here, read by OmniaCLI itself.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import os
import re
import shutil
import tarfile
import tempfile
import zipfile
from pathlib import Path

from . import backupcheck, ops, private
from .jobs import Job, JobFailed

PREFIX = "archive_"
FORMAT = 1
# an address's user and password, never written
_USERINFO = re.compile(r"^(https?://)[^/@]*@")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(4 << 20):
            digest.update(block)
    return digest.hexdigest()


async def _repository(job: Job, repo, folder: Path) -> dict:
    row = {"name": repo.name, "branch": repo.branch, "url": "", "commit": None, "code": None}
    if not repo.path.is_dir():
        job.log(f"WARNING: {repo.path} is not there: {repo.name} is not in the archive")
        return row
    code, out = await ops.capture(repo.git("remote", "get-url", repo.remote), cwd=repo.path)
    row["url"] = _USERINFO.sub(r"\1", out.strip()) if code == 0 else ""
    row["commit"] = (await ops.git(repo, "rev-parse", "HEAD")).strip()
    code, out = await ops.capture(["git", "status", "--porcelain", "--untracked-files=no"], cwd=repo.path)
    if code == 0 and out.strip():
        job.log(f"WARNING: {repo.name} has uncommitted changes: the archive has its commit {row['commit'][:10]}, "
                "not them")
    target = folder / f"{repo.name}.tar.gz"
    code, out = await ops.capture(["git", "archive", "--format=tar.gz", "-o", str(target), "HEAD"], cwd=repo.path,
                                  timeout=1800)
    if code:
        raise JobFailed(f"[{repo.name}] git archive failed: {out[-300:]}")
    row["code"] = f"code/{repo.name}.tar.gz"
    job.log(f"{repo.name}: {repo.branch} {row['commit'][:10]} ({target.stat().st_size} bytes of code)")
    return row


async def make(job: Job, instance, folder: Path, options: dict | None = None) -> Path:
    """The full archive of the instance into folder, its backups'."""
    private.backup_folder(folder)
    with tempfile.TemporaryDirectory(prefix=".archive-", dir=folder) as tmp:
        work = Path(tmp)
        job.log("--- the Odoo backup")
        backup = await ops.backup_into(job, instance, work, options)
        job.log("--- the code of the repositories, at their commit")
        (work / "code").mkdir()
        repositories = [await _repository(job, repo, work / "code") for repo in instance.repositories]
        job.log("--- the files of the configuration")
        config = await instance.plugin.archive_files(job, instance)
        (work / "config").mkdir()
        for name, content in config:
            (work / "config" / Path(name).name).write_bytes(content)
        (work / "repositories.json").write_text(json.dumps(repositories, indent=1) + "\n")
        parts = {"backup.zip": backup, "repositories.json": work / "repositories.json",
                 **{r["code"]: work / r["code"] for r in repositories if r["code"]},
                 **{f"config/{Path(n).name}": work / "config" / Path(n).name for n, _ in config}}
        summary = await instance.plugin.summary(instance)
        manifest = {
            "format": FORMAT, "kind": "omniacli instance archive",
            "made": datetime.datetime.now().isoformat(timespec="seconds"),
            "server": instance.server, "role": instance.role, "type": instance.plugin.name,
            "odoo": summary.get("version"), "image": (summary.get("image") or {}).get("name"),
            "workers": summary.get("workers"),
            "files": {name: {"size": path.stat().st_size, "sha256": _sha256(path)} for name, path in parts.items()},
        }
        name = PREFIX + backup.name
        partial = work / name
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_STORED) as zf:
            zf.writestr("manifest.json", json.dumps(manifest, indent=1) + "\n")
            for arcname, path in parts.items():
                zf.write(path, arcname, compress_type=zipfile.ZIP_STORED if arcname.endswith((".zip", ".gz"))
                         else zipfile.ZIP_DEFLATED)
        os.chmod(partial, 0o600)
        dest = folder / name
        shutil.move(str(partial), dest)
    await backupcheck.record(dest)
    job.log(f"Archive {dest.name} ({dest.stat().st_size} bytes): the backup, {len(repositories)} repositories, "
            f"{len(config)} files of the configuration")
    return dest


def read_manifest(path: Path) -> dict | None:
    """The manifest of a full archive; None when the zip is not one."""
    try:
        with zipfile.ZipFile(path) as zf:
            data = json.loads(zf.read("manifest.json"))
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        return None
    return data if isinstance(data, dict) and data.get("kind") == "omniacli instance archive" else None


def check_parts(path: Path) -> list[str]:
    """The parts of an archive whose SHA-256 is not its manifest's (none: all whole)."""
    manifest = read_manifest(path) or {}
    bad = []
    with zipfile.ZipFile(path) as zf:
        for name, want in (manifest.get("files") or {}).items():
            digest = hashlib.sha256()
            try:
                with zf.open(name) as fh:
                    while block := fh.read(4 << 20):
                        digest.update(block)
            except KeyError:
                bad.append(name)
                continue
            if digest.hexdigest() != want.get("sha256"):
                bad.append(name)
    return bad


@contextlib.contextmanager
def backup_of(path: Path):
    """The Odoo backup inside an archive, as a zip of its own next to it
    (removed after): what a restore or a try loads."""
    with tempfile.TemporaryDirectory(prefix=".archive-", dir=path.parent) as tmp:
        inner = Path(tmp) / "backup.zip"
        with zipfile.ZipFile(path) as zf, zf.open("backup.zip") as src, open(inner, "wb") as dst:
            shutil.copyfileobj(src, dst, 4 << 20)
        yield inner


def read_part(path: Path, name: str) -> bytes | None:
    """A small part of an archive (repositories.json, config/...); None when absent."""
    try:
        with zipfile.ZipFile(path) as zf:
            return zf.read(name)
    except (OSError, KeyError, zipfile.BadZipFile):
        return None


def read_json(path: Path, name: str):
    raw = read_part(path, name)
    try:
        return json.loads(raw) if raw is not None else None
    except ValueError:
        return None


def extract_code(path: Path, arcname: str, target: Path) -> int:
    """The code of a repository (code/<repo>.tar.gz) written into target, a
    new folder: only plain files and folders inside it, never a link nor a
    path that leaves it. The number of files."""
    target.mkdir(parents=False)
    root = target.resolve()
    count = 0
    with zipfile.ZipFile(path) as zf, zf.open(arcname) as raw, tarfile.open(fileobj=raw, mode="r|gz") as tar:
        for member in tar:
            dest = (root / member.name).resolve()
            if dest != root and root not in dest.parents:
                raise JobFailed(f"{arcname}: {member.name} leaves its folder")
            if member.isdir():
                dest.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                dest.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
                os.chmod(dest, 0o755 if member.mode & 0o111 else 0o644)
                count += 1
            # links, devices: not written
    return count
