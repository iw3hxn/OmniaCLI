# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The deploys of an instance: each job that changed its code (a Git pull,
an Update of a repository, a deploy on push, the auto update, a scheduled
pull, a staging rebuilt with its code, a repository added or taken away,
a deploy gone back to), with each repository's commit before and after,
who or what started it, its outcome and the backup made before it; and
going back to one of them.

What is kept: a file per instance in deploys/ next to the servers file
(<server>.<role>.json, the folder 700, each file 600), the last KEEP
deploys, so a history outlives the jobs list (trimmed at 500 jobs of the
whole machine); taken away when its instance is. It also holds whether
the automatic deploys of the instance are paused (a push, the auto update,
a scheduled pull: README, Deploys).

How a deploy is seen: the jobs' watcher (Recorder) reads the commit each
repository has checked out when a job on the instance starts and when it
ends (the files of .git, git rev-parse when they cannot be read); when one
moved, the job is a deploy. No action has to tell it.

Going back (go_back): each repository the deploy knew set to its commit on
its branch (git reset --hard; the branch stays the one it follows, so the
next pull fast-forwards to its head again), its submodules, then what
follows a pull (the Python packages, the changed modules updated) and a
restart, a backup made first. The database is not taken back."""

from __future__ import annotations

import datetime
import json
import os
import re
from pathlib import Path

from . import ops, zerodowntime
from .jobs import Job, JobFailed

FOLDER = "deploys"
# deploys kept per instance
KEEP = 100
# commits listed of each repository of a deploy (the count says how many)
LISTED = 10
_SHA = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?")
_KEY = re.compile(r"[A-Za-z0-9_.-]{1,100}/[a-z]{1,20}")


def folder_of(settings) -> Path:
    return settings.servers_file.parent / FOLDER


def file_of(folder: Path, key: str) -> Path:
    if not _KEY.fullmatch(key) or ".." in key:
        raise ValueError(f"not an instance key: {key!r}")
    server, role = key.split("/", 1)
    return folder / f"{server}.{role}.json"


def load(folder: Path, key: str) -> dict:
    """{"deploys": [oldest first], "paused": {"by", "at"} or None,
    "zero_downtime": {"by", "at"} or None (its updates without stopping,
    zerodowntime.py: who switched them on, when)}."""
    try:
        data = json.loads(file_of(folder, key).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    found = data.get("deploys")
    return {"deploys": [d for d in found if isinstance(d, dict)] if isinstance(found, list) else [],
            "paused": data.get("paused") if isinstance(data.get("paused"), dict) else None,
            "zero_downtime": data.get("zero_downtime") if isinstance(data.get("zero_downtime"), dict) else None}


def save(folder: Path, key: str, data: dict) -> None:
    if not folder.is_dir():
        folder.mkdir(parents=True, mode=0o700, exist_ok=True)
    path = file_of(folder, key)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"deploys": data.get("deploys") or [], "paused": data.get("paused"),
                   **({"zero_downtime": data["zero_downtime"]} if data.get("zero_downtime") else {})}, fh, indent=1)
    os.replace(tmp, path)


def record(folder: Path, key: str, entry: dict) -> None:
    data = load(folder, key)
    data["deploys"] = (data["deploys"] + [entry])[-KEEP:]
    save(folder, key, data)


def remove(folder: Path, key: str) -> None:
    try:
        file_of(folder, key).unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


def paused(folder: Path, key: str) -> dict | None:
    return load(folder, key)["paused"]


def set_paused(folder: Path, key: str, by: str | None) -> dict | None:
    """Paused by by (None: on again); what it is now."""
    data = load(folder, key)
    data["paused"] = {"by": by, "at": datetime.datetime.now().isoformat(timespec="seconds")} if by else None
    save(folder, key, data)
    return data["paused"]


def zero_downtime(folder: Path, key: str) -> dict | None:
    return load(folder, key)["zero_downtime"]


def set_zero_downtime(folder: Path, key: str, by: str | None) -> dict | None:
    """Its updates without stopping switched on by by (None: off); what it is now."""
    data = load(folder, key)
    data["zero_downtime"] = {"by": by, "at": datetime.datetime.now().isoformat(timespec="seconds")} if by else None
    save(folder, key, data)
    return data["zero_downtime"]


def find(folder: Path, key: str, deploy_id: str) -> dict | None:
    return next((d for d in load(folder, key)["deploys"] if d.get("id") == deploy_id), None)


# --- the commit a working copy has checked out ---------------------------

def _git_dir(path: Path) -> Path | None:
    dot = path / ".git"
    if dot.is_dir():
        return dot
    if dot.is_file():
        text = dot.read_text(encoding="utf-8", errors="replace").strip()
        if text.startswith("gitdir:"):
            found = Path(text[len("gitdir:"):].strip())
            return found if found.is_absolute() else (path / found).resolve()
    return None


def head_file(path: Path) -> str | None:
    """The commit checked out in the working copy at path, read from its
    .git (a loose ref, packed-refs, a detached HEAD); None when it cannot
    be read that way."""
    try:
        git = _git_dir(path)
        if git is None:
            return None
        text = (git / "HEAD").read_text(encoding="utf-8").strip()
        if not text.startswith("ref:"):
            return text if _SHA.fullmatch(text) else None
        ref = text[len("ref:"):].strip()
        common = git
        if (git / "commondir").is_file():
            common = (git / (git / "commondir").read_text(encoding="utf-8").strip()).resolve()
        for base in dict.fromkeys((git, common)):
            loose = base / ref
            if loose.is_file():
                value = loose.read_text(encoding="utf-8").strip()
                return value if _SHA.fullmatch(value) else None
        packed = common / "packed-refs"
        if packed.is_file():
            for line in packed.read_text(encoding="utf-8").splitlines():
                sha, _, name = line.partition(" ")
                if name.strip() == ref and _SHA.fullmatch(sha):
                    return sha
    except OSError:
        return None
    return None


async def head(repo) -> str | None:
    found = head_file(repo.path)
    if found is not None or not repo.path.is_dir():
        return found
    code, out = await ops.capture(["git", "rev-parse", "HEAD"], cwd=repo.path, timeout=20)
    return out.strip() if code == 0 and _SHA.fullmatch(out.strip()) else None


async def snapshot(instance) -> dict:
    """{repository: {"sha", "branch"}} of what the instance has checked out."""
    found = {}
    for repo in getattr(instance, "repositories", None) or []:
        sha = await head(repo)
        if sha is not None:
            found[repo.name] = {"sha": sha, "branch": repo.branch}
    return found


async def _lines(path: Path, *args: str) -> list[str]:
    if not path.is_dir():
        return []
    code, out = await ops.capture(["git", *args], cwd=path, timeout=30)
    return out.splitlines() if code == 0 else []


async def subject(path: Path, sha: str | None) -> str:
    if not sha:
        return ""
    found = await _lines(path, "log", "-1", "--format=%s", sha)
    return found[0][:200] if found else ""


async def count(path: Path, span: str) -> int | None:
    found = await _lines(path, "rev-list", "--count", span)
    return int(found[0]) if found and found[0].isdigit() else None


async def commits(path: Path, span: str) -> list[dict]:
    """The newest LISTED commits of span: {"sha" (short), "subject"}."""
    rows = []
    for line in await _lines(path, "log", f"-n{LISTED}", "--format=%h%x00%s", span):
        sha, _, text = line.partition("\0")
        rows.append({"sha": sha, "subject": text[:200]})
    return rows


async def changes(paths: dict, before: dict, after: dict) -> list[dict]:
    """Each repository whose commit moved from before to after (snapshots):
    its commits, their subjects, how many came and how many went; paths:
    {repository: its working copy}."""
    rows = []
    for name in dict.fromkeys([*after, *before]):
        was, now = (before.get(name) or {}).get("sha"), (after.get(name) or {}).get("sha")
        if was == now:
            continue
        path = paths.get(name) or Path("/nonexistent")
        row = {"name": name, "branch": (after.get(name) or before.get(name) or {}).get("branch", ""),
               "before": was, "after": now, "subject": await subject(path, now),
               "before_subject": await subject(path, was), "brought": None, "taken": None, "commits": []}
        if was and now:
            row["brought"], row["taken"] = await count(path, f"{was}..{now}"), await count(path, f"{now}..{was}")
            row["commits"] = await commits(path, f"{was}..{now}")
        rows.append(row)
    return rows


# --- the watcher of the jobs ---------------------------------------------

class Recorder:
    """JobManager's watcher: a job on an instance whose commits moved is a
    deploy of it, recorded with them. lookup(key): the instance of key now
    (None: not, or no more, an instance)."""

    def __init__(self, folder: Path, lookup):
        self.folder, self.lookup = folder, lookup
        self._before: dict[str, dict] = {}

    async def begin(self, job: Job) -> None:
        found = {}
        for key in job.instances:
            instance = self.lookup(key)
            found[key] = None if instance is None else await snapshot(instance)
        self._before[job.id] = found

    async def end(self, job: Job) -> None:
        before = self._before.pop(job.id, None)
        if before is None:
            return
        for key, was in before.items():
            instance = self.lookup(key)
            if instance is None:
                if was is not None:
                    # the instance was taken away: its history with it
                    remove(self.folder, key)
                continue
            now = await snapshot(instance)
            if (was or {}) == now:
                continue
            paths = {r.name: r.path for r in instance.repositories}
            rows = await changes(paths, was or {}, now)
            if not rows:
                continue
            notes = job.notes if isinstance(job.notes, dict) else {}
            entry = {"id": job.id, "job": job.id, "title": job.title, "by": job.source,
                     "started": job.started, "finished": job.finished, "outcome": job.outcome,
                     "error": (job.error or "")[:500] or None, "repositories": rows, "state": now,
                     "backup": (notes.get("backups") or {}).get(key), "back_to": notes.get("back_to"),
                     # the time its users were not served, measured (zerodowntime.py)
                     "unserved": notes.get("unserved") if isinstance(notes.get("unserved"), dict) else None}
            record(self.folder, key, entry)


# --- going back to a deploy -------------------------------------------------

async def back_plan(instance, entry: dict) -> dict:
    """What going back to entry does on instance now: each repository with
    its commit now and the deploy's, the commits that would be taken away
    and those that would come back; the repositories the deploy did not
    know (kept as they are) and those it knew that are gone; refused: why
    it cannot (local changes, another branch, a commit no longer there)."""
    state = entry.get("state") or {}
    rows, kept, refused = [], [], []
    for repo in instance.repositories:
        target = state.get(repo.name)
        if not isinstance(target, dict) or not target.get("sha"):
            kept.append(repo.name)
            continue
        now = await head(repo)
        row = {"name": repo.name, "branch": repo.branch, "now": now, "to": target["sha"],
               "change": now != target["sha"], "now_subject": await subject(repo.path, now),
               "to_subject": "", "taken": None, "brought": None, "taken_commits": [], "missing": False}
        rows.append(row)
        if not row["change"]:
            row["to_subject"] = row["now_subject"]
            continue
        if target.get("branch") and target["branch"] != repo.branch:
            refused.append(f"[{repo.name}] that deploy was on branch {target['branch']}, it follows "
                           f"{repo.branch} now: change its branch back first")
        current = await _lines(repo.path, "rev-parse", "--abbrev-ref", "HEAD")
        if current and current[0] != repo.branch:
            refused.append(f"[{repo.name}] it is on branch {current[0]}, expected {repo.branch}")
        if await _lines(repo.path, "status", "--porcelain", "--untracked-files=no"):
            refused.append(f"[{repo.name}] it has uncommitted changes")
        if not await _lines(repo.path, "cat-file", "-t", target["sha"]):
            # not here: a fetch may bring it (go_back fetches first)
            row["missing"] = True
            continue
        row["to_subject"] = await subject(repo.path, target["sha"])
        if now:
            row["taken"] = await count(repo.path, f"{target['sha']}..{now}")
            row["brought"] = await count(repo.path, f"{now}..{target['sha']}")
            row["taken_commits"] = await commits(repo.path, f"{target['sha']}..{now}")
    gone = sorted(set(state) - {r.name for r in instance.repositories})
    return {"repositories": rows, "kept": kept, "gone": gone, "refused": refused,
            "changes": sum(1 for r in rows if r["change"])}


async def go_back(job: Job, instance, entry: dict, backup_folder: Path | None) -> None:
    """The job of «Back to this deploy»: a backup first (always), each
    repository at the deploy's commit, what follows a pull, a restart."""
    plan = await back_plan(instance, entry)
    if plan["refused"]:
        raise JobFailed("; ".join(plan["refused"]))
    job.notes["back_to"] = entry.get("id")
    moving = [r for r in plan["repositories"] if r["change"]]
    if not moving:
        job.log("Every repository is at the commits of that deploy already: nothing to do")
        return
    repos = {r.name: r for r in instance.repositories}
    for row in moving:
        if row["missing"]:
            repo = repos[row["name"]]
            job.log(f"--- [{repo.name}] {row['to'][:10]} is not in its copy: fetched from {repo.remote}")
            await ops.run(job, repo.git("fetch", repo.remote), cwd=repo.path, check=False)
            if not await _lines(repo.path, "cat-file", "-t", row["to"]):
                raise JobFailed(f"[{repo.name}] the commit {row['to'][:10]} of that deploy is no longer in "
                                "the repository: restore the backup taken before it instead")
    for name in plan["kept"]:
        job.log(f"[{name}] not in that deploy: kept as it is")
    for name in plan["gone"]:
        job.log(f"[{name}] was in that deploy and is no longer a repository of the instance: not added back")

    async def normal() -> None:
        await ops.backup_before_update(job, instance, backup_folder, always=True)
        for row in moving:
            repo = repos[row["name"]]
            job.log(f"--- [{repo.name}] {(row['now'] or '?')[:10]} → {row['to'][:10]} on {repo.branch}"
                    + (f": {row['taken']} commits after that deploy taken away" if row["taken"] else ""))
            await ops.run(job, repo.git("reset", "--hard", row["to"]), cwd=repo.path)
            await ops.update_submodules(job, repo)
        await instance.plugin.after_pull(job, instance)
        await ops.restart(job, instance)

    # the update without stopping, when the instance is switched to it (zerodowntime.py)
    found = zerodowntime.choice(instance)
    if found is None or await ops._stopped(instance):
        await normal()
    elif isinstance(found, str):
        job.warn(f"Not without stopping: {found}. The update restarts Odoo: its users wait for it")
        await normal()
    else:
        job.log(f"--- back without stopping: a second Odoo on the code of that deploy, then nginx of {found.url} to it")
        await zerodowntime.leftover(job, instance, found)
        await zerodowntime.switch(job, instance, found, {r["name"]: (r["now"], r["to"]) for r in moving}, "back",
                                  backup_folder, normal, always_backup=True)
    job.log("The database was not taken back: a module whose data changed after that deploy may need the "
            "backup taken before it")
