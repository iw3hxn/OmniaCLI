# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Is a backup good? Two answers, kept next to the backups of an instance in
.omniacli-backups.json (mode 600): {file: {"sha256", "verified"}}.

- its SHA-256, written when the backup is made: read again later, a file
  changed or damaged on the disk since is told;
- a restore tried for real (the plugin's verify_backup: in a database of its
  own, dropped after, the instance untouched), with what it found there
  (installed modules, users, the newest record), and when.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import os
from pathlib import Path

from . import instancearchive
from .jobs import Job, JobFailed

INDEX = ".omniacli-backups.json"
CHUNK = 4 << 20


def load(folder: Path) -> dict:
    try:
        data = json.loads((folder / INDEX).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(folder: Path, data: dict) -> None:
    """Only the backups still there; mode 600 from the start."""
    data = {k: v for k, v in data.items() if (folder / k).is_file()}
    tmp = folder / (INDEX + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
    os.replace(tmp, folder / INDEX)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(CHUNK):
            digest.update(block)
    return digest.hexdigest()


async def record(path: Path) -> str:
    """The backup's SHA-256, written down: the one made now."""
    sha = await asyncio.to_thread(_sha256, path)
    data = load(path.parent)
    data[path.name] = {"sha256": sha}
    save(path.parent, data)
    return sha


def rename(folder: Path, old: str, new: str) -> None:
    data = load(folder)
    if old in data:
        data[new] = data.pop(old)
        save(folder, data)


def state(folder: Path, name: str) -> dict:
    """What is known of a backup: {"sha256": bool, "verified": {...} or None}."""
    entry = load(folder).get(name) or {}
    return {"sha256": bool(entry.get("sha256")), "verified": entry.get("verified")}


async def verify(job: Job, instance, path: Path) -> dict:
    """The backup checked: its SHA-256 against the one written when it was
    made, then a restore tried by the instance's plugin. The result written
    down, failed too; JobFailed when it is not good."""
    folder = path.parent
    entry = load(folder).get(path.name) or {}
    now = datetime.datetime.now().isoformat(timespec="seconds")
    result = {"time": now, "ok": False, "detail": ""}
    sha = None
    try:
        job.log(f"--- SHA-256 of {path.name}")
        sha = await asyncio.to_thread(_sha256, path)
        if entry.get("sha256") and entry["sha256"] != sha:
            raise JobFailed(f"{path.name} changed since it was made: its SHA-256 is not the one written then "
                            "(a damaged disk, or a file replaced)")
        job.log("the same as when it was made" if entry.get("sha256") else "none written when it was made: written now")
        if instancearchive.read_manifest(path) is not None:
            bad = await asyncio.to_thread(instancearchive.check_parts, path)
            if bad:
                raise JobFailed(f"{path.name}: {', '.join(bad)} not as the archive's manifest says")
            job.log("every part of the archive as its manifest says")
            with instancearchive.backup_of(path) as inner:
                found = await instance.plugin.verify_backup(job, instance, inner)
        else:
            found = await instance.plugin.verify_backup(job, instance, path)
        result.update(ok=True, **{k: v for k, v in found.items() if k in ("modules", "users", "companies", "last")})
        job.log(f"Restore tried: {result.get('modules')} modules installed, {result.get('users')} users, "
                f"newest record {result.get('last') or '?'}")
    except JobFailed as exc:
        result["detail"] = str(exc)[:500]
        raise
    finally:
        data = load(folder)
        current = data.get(path.name) or {}
        if sha and not current.get("sha256"):
            current["sha256"] = sha
        current["verified"] = result
        data[path.name] = current
        if path.is_file():
            save(folder, data)
    return result
