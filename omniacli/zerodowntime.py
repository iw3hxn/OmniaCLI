# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""An update without stopping (blue-green) of an instance made from a
template: a Docker Compose project of a workspace, whose nginx OmniaCLI
writes. Opt-in per instance (its switch, kept in its file of deploys); off,
an update is the normal one, a restart that users see.

The steps of a job (Git pull and restart, a deploy on push, the auto update,
a scheduled pull, Back to this deploy):

1. each repository fetched; the commits it moves to, a fast-forward (a pull)
   or the deploy's (going back);
2. whether the old Odoo can keep serving while the database takes the new
   code's modules (refusals()): no migration script, no field nor model
   that changes, no dependency that changes, no installed module taken
   away, no new Python package, no submodule, no local change. Refused, the
   job says why and runs the normal update, its gap measured;
3. the backup before the update, when the instance asks it;
4. the new code copied (git worktree, addons/.omniacli-next/<repository>);
   its changed modules updated (click-odoo-update with its addons path) in a
   container of its own: the old Odoo keeps serving, with its code, records
   it reads in the same tables and columns;
5. a second Odoo on the new code (compose-helper next-start: a container of
   the service on free ports, no cron thread), checked: /web/login 200 and
   the version of its Odoo, the old one's;
6. nginx sent to it (workspace-helper upstream: nginx -t, a reload: the
   requests under way end where they are), /websocket too; the old Odoo
   drained: no connection open to it, at most DRAIN_MOST seconds (the
   websockets of the open pages reconnect);
7. the instance's repositories moved, its Odoo restarted with them, checked,
   nginx back to it, the second Odoo drained and removed, the copies too.

Until 6 the users are served by the old Odoo: a failure removes the second
Odoo and the copies, the code is not moved, the job says why (the database
keeps what the update committed: modules of the kinds of 2). After 6, a
failure leaves the users on the second Odoo: the job says so, and the next
update takes it away first.

Every PROBE_EVERY seconds of the job the instance's name is asked through
nginx (/web/login): the time without a 200 is the job's last line and its
deploy's (job.notes["unserved"])."""

from __future__ import annotations

import ast
import asyncio
import json
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlsplit

import httpx

from . import ops
from .composehelper import NEXT_FOLDER
from .config import OMNIACLI_COMMAND
from .jobs import Job, JobFailed

HELPER_TIMEOUT = 120
# the changed modules of a big database
UPDATE_TIMEOUT = 6 * 3600
# an Odoo that starts: /web/login 200 within
HEALTH_TIMEOUT = 300
# the old Odoo once nginx is switched: its open connections waited for, at least DRAIN_LEAST seconds (the old
# workers of nginx may still take a request a moment after the reload), at most DRAIN_MOST
DRAIN_LEAST = 2.0
DRAIN_MOST = 30.0
DRAIN_EVERY = 0.5
# the users' side: the instance's name asked through nginx every PROBE_EVERY seconds
PROBE_EVERY = 0.1
PROBE_TIMEOUT = 10.0
# the files a module's Python changes the database with: a field, a model, a constraint, an index (a line
# added or taken away, its +/- gone)
SCHEMA = re.compile(
    r"\bfields\.[A-Z]\w*\s*\("
    r"|^\s*_(?:name|inherit|inherits|table|auto|sql_constraints|rec_name|order|parent_store|parent_name)\s*="
    r"|\bmodels\.(?:Constraint|Index|UniqueIndex)\s*\("
    r"|^\s*(?:required|store|index|related|translate|selection|size|digits|company_dependent|ondelete|precompute"
    r"|group_expand|comodel_name|inverse_name|relation)\s*=")
# a module's folders whose change makes the database another one for the old code
MIGRATIONS = ("migrations", "upgrades")
LISTED = 5


@dataclass
class Target:
    """Where an instance is updated without stopping: its workspace's site."""
    workspace: str
    storage: str
    instance: str
    role: str
    # https://<name>, the address the users open
    url: str
    # its compose project
    project: Path


# the app's: instance -> Target (switched on, and it can), a text (switched on, and why it cannot), None (off)
_lookup: Callable[[object], "Target | str | None"] | None = None


def configure(lookup: Callable[[object], "Target | str | None"] | None) -> None:
    global _lookup
    _lookup = lookup


def choice(instance) -> Target | str | None:
    """How the instance is updated: a Target, why not (switched on, and it
    cannot), or None (switched off, or no app said)."""
    return _lookup(instance) if _lookup is not None else None


def workspace_helper(*args: str) -> list[str]:
    return ["sudo", "-n", str(OMNIACLI_COMMAND), "workspace-helper", *args]


# --- what the old Odoo can keep serving -------------------------------------

def module_of(path: str, dirs: set[str]) -> str | None:
    """The folder of the module path is in (its deepest folder with a
    __manifest__.py among dirs), None when it is in none."""
    parts = path.split("/")
    for cut in range(len(parts) - 1, 0, -1):
        folder = "/".join(parts[:cut])
        if folder in dirs:
            return folder
    return None


def _manifest(text: str | None) -> dict:
    try:
        found = ast.literal_eval(text) if text else {}
    except (ValueError, SyntaxError):
        return {"unreadable": True}
    return found if isinstance(found, dict) else {"unreadable": True}


def refusals(repos: list[dict], installed: set[str]) -> list[str]:
    """Why the old Odoo could not keep serving while the database takes the
    new code (empty: it can). repos, each: name, gitlinks (paths of
    submodules that move), submodules (a .gitmodules), files [(status,
    path)], old_dirs and new_dirs (the folders with a __manifest__.py),
    py_lines {path: [lines added or taken away]}, manifests {folder: (old,
    new)}. Only the installed modules count: the others are not loaded."""
    found: list[str] = []
    for repo in repos:
        name = repo["name"]
        if repo.get("submodules"):
            found.append(f"[{name}] has submodules: the new code is not copied with them")
        for path in repo.get("gitlinks") or []:
            found.append(f"[{name}] the submodule {path} moves: its code is not copied for the second Odoo")
        dirs = set(repo.get("old_dirs") or ()) | set(repo.get("new_dirs") or ())
        for status, path in repo.get("files") or []:
            if path == "requirements.txt":
                found.append(f"[{name}] requirements.txt changes: new Python packages, which the running Odoo "
                             "would need first")
                continue
            folder = module_of(path, dirs)
            if folder is None:
                continue
            module = folder.rsplit("/", 1)[-1]
            if module not in installed:
                continue
            inside = path[len(folder) + 1:]
            if folder not in set(repo.get("new_dirs") or ()):
                found.append(f"the module {module} is installed and its folder is taken away")
            elif inside.split("/", 1)[0] in MIGRATIONS:
                found.append(f"the module {module}: a migration script ({path}) changes its data for the new code")
            elif inside == "__manifest__.py":
                old, new = (_manifest(t) for t in (repo.get("manifests") or {}).get(folder, (None, None)))
                if old.get("unreadable") or new.get("unreadable"):
                    found.append(f"the module {module}: its __manifest__.py cannot be read: what changes is not known")
                elif old.get("depends") != new.get("depends") \
                        or old.get("external_dependencies") != new.get("external_dependencies"):
                    found.append(f"the module {module}: what it depends on changes ({path}): new modules would be installed")
            elif inside.endswith(".py") and any(SCHEMA.search(line) for line in (repo.get("py_lines") or {}).get(path, ())):
                found.append(f"the module {module}: {path} changes a field or a model, which the old Odoo does not know")
            elif status == "D" and inside.endswith(".py"):
                found.append(f"the module {module}: {path} is taken away, code the old Odoo still runs")
    return list(dict.fromkeys(found))


async def _lines(path: Path, *args: str) -> str:
    code, out = await ops.capture(["git", *args], cwd=path, timeout=60)
    if code:
        raise JobFailed(f"git {' '.join(args[:2])} in {path}: {out[-300:]}")
    return out


async def describe(repo, old: str, new: str) -> dict:
    """What moving repo from old to new changes, for refusals()."""
    raw = await _lines(repo.path, "diff", "--raw", "--no-renames", "-z", old, new)
    files, gitlinks = [], []
    parts = raw.split("\0")
    for head, path in zip(parts[0::2], parts[1::2]):
        fields = head.lstrip(":").split()
        if len(fields) < 5 or not path:
            continue
        if "160000" in fields[:2]:
            gitlinks.append(path)
        files.append((fields[4][:1], path))

    async def dirs(sha: str) -> set[str]:
        names = (await _lines(repo.path, "ls-tree", "-r", "--name-only", "-z", sha)).split("\0")
        return {n.rsplit("/", 1)[0] if "/" in n else "" for n in names
                if n == "__manifest__.py" or n.endswith("/__manifest__.py")} - {""}

    old_dirs, new_dirs = await dirs(old), await dirs(new)
    py_lines: dict[str, list[str]] = {}
    current = None
    for line in (await _lines(repo.path, "diff", "-U0", "--no-color", "--no-renames", old, new, "--", "*.py")).splitlines():
        if line.startswith(("+++ ", "--- ")):
            target = line[4:]
            if target != "/dev/null":
                current = target[2:] if target[:2] in ("a/", "b/") else target
            continue
        if current and line[:1] in "+-":
            py_lines.setdefault(current, []).append(line[1:])
    manifests = {}
    for _, path in files:
        if path == "__manifest__.py" or path.endswith("/__manifest__.py"):
            folder = path.rsplit("/", 1)[0] if "/" in path else ""
            texts = []
            for sha in (old, new):
                code, out = await ops.capture(["git", "show", f"{sha}:{path}"], cwd=repo.path, timeout=30)
                texts.append(out if code == 0 else None)
            manifests[folder] = tuple(texts)
    submodules = (await ops.capture(["git", "cat-file", "-e", f"{new}:.gitmodules"], cwd=repo.path))[0] == 0
    return {"name": repo.name, "files": files, "gitlinks": gitlinks, "old_dirs": old_dirs, "new_dirs": new_dirs,
            "py_lines": py_lines, "manifests": manifests, "submodules": submodules}


async def blockers(instance, target: Target, moves: dict[str, tuple[str, str]]) -> list[str]:
    """refusals() of moves on instance, and what the instance itself
    stands in the way with."""
    found = []
    if instance.package is not None:
        found.append("the instance installs a Debian package too, which a second Odoo does not have")
    if not instance.options.http_port:
        found.append("the port of its Odoo is not known (http_port of its settings)")
    repos = {r.name: r for r in instance.repositories}
    described = []
    for name, (old, new) in moves.items():
        repo = repos[name]
        if repo.path != target.project / "addons" / name:
            found.append(f"[{name}] is not in the addons of its project ({repo.path})")
            continue
        if not old:
            found.append(f"[{name}] the commit it has checked out cannot be read")
            continue
        if await _lines(repo.path, "status", "--porcelain", "--untracked-files=no"):
            found.append(f"[{name}] has uncommitted changes, which the copy of the new code would not have")
            continue
        described.append(await describe(repo, old, new))
    if found:
        return found
    try:
        known = await instance.plugin.known_modules(instance)
    except JobFailed as exc:
        return [f"the modules of its database cannot be read: {exc}"]
    installed = {m for m, state in known.items() if state in ("installed", "to upgrade")}
    return refusals(described, installed)


# --- the users' side --------------------------------------------------------

async def ask(url: str) -> bool:
    """One ask of the users' page through nginx: https://<name>/web/login,
    to this machine (its certificate may be the customer's own, or not yet
    trusted: not checked), 200 or not."""
    host = urlsplit(url).hostname or ""
    try:
        async with httpx.AsyncClient(verify=False, timeout=PROBE_TIMEOUT) as client:
            answer = await client.get("https://127.0.0.1/web/login", headers={"Host": host},
                                      extensions={"sni_hostname": host})
            return answer.status_code == 200
    except httpx.HTTPError:
        return False


class Probe:
    """The instance's name asked every PROBE_EVERY seconds while a job works:
    the time its users were not served (from an ask without a 200 to the
    next one with), how many asks, how many failed."""

    def __init__(self, url: str, every: float = PROBE_EVERY):
        self.url, self.every = url, every
        self.points: list[tuple[float, bool]] = []
        self._task: asyncio.Task | None = None
        self._ended: float | None = None

    def start(self) -> "Probe":
        self._task = asyncio.ensure_future(self._loop())
        return self

    async def _loop(self) -> None:
        while True:
            began = time.monotonic()
            self.points.append((began, await ask(self.url)))
            await asyncio.sleep(max(0.0, self.every - (time.monotonic() - began)))

    async def settle(self, most: float = HEALTH_TIMEOUT) -> None:
        """Until an ask after now is answered with 200 (at most most seconds)."""
        began = time.monotonic()
        while time.monotonic() - began < most:
            if any(ok for moment, ok in self.points if moment >= began):
                return
            await asyncio.sleep(self.every)

    async def stop(self) -> dict:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._ended = time.monotonic()
        return measure(self.points, self._ended, self.every)


def measure(points: list[tuple[float, bool]], ended: float, every: float = PROBE_EVERY) -> dict:
    """{"asked", "failed", "seconds", "longest", "every"}: seconds without a
    200, from the first ask that failed to the next that did not (to ended
    when none did)."""
    seconds = longest = 0.0
    failed, since = 0, None
    for moment, ok in points:
        if not ok:
            failed += 1
            since = moment if since is None else since
        elif since is not None:
            seconds, longest, since = seconds + moment - since, max(longest, moment - since), None
    if since is not None:
        seconds, longest = seconds + ended - since, max(longest, ended - since)
    return {"asked": len(points), "failed": failed, "seconds": round(seconds, 1), "longest": round(longest, 1),
            "every": every}


def said(job: Job, found: dict, how: str, url: str) -> None:
    """The job's line of what its users saw, kept for its deploy."""
    found = {**found, "how": how}
    if isinstance(getattr(job, "notes", None), dict):
        job.notes["unserved"] = found
    job.log(f"Users not served: {found['seconds']:.1f} s ({how}; {found['failed']} of {found['asked']} asks of "
            f"{url}/web/login through nginx failed, one every {int(found['every'] * 1000)} ms)")


# --- the second Odoo ----------------------------------------------------------

async def _status(url: str) -> int | None:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            return (await client.get(url)).status_code
    except httpx.HTTPError:
        return None


async def version_of(port: int) -> str | None:
    """The version its Odoo says (/web/webclient/version_info)."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            answer = await client.post(f"http://127.0.0.1:{port}/web/webclient/version_info",
                                       json={"jsonrpc": "2.0", "method": "call", "params": {}})
            return str(((answer.json() or {}).get("result") or {}).get("server_version") or "") or None
    except (httpx.HTTPError, ValueError, AttributeError):
        return None


async def healthy(job: Job, port: int, version: str | None, what: str, alive=None) -> None:
    """/web/login 200 on port within HEALTH_TIMEOUT, then its Odoo's version
    the one given; JobFailed otherwise. alive(): False when its container
    stopped (no need to wait more)."""
    began = time.monotonic()
    status = None
    while True:
        status = await _status(f"http://127.0.0.1:{port}/web/login")
        if status == 200:
            break
        if time.monotonic() - began > HEALTH_TIMEOUT:
            raise JobFailed(f"{what} did not answer /web/login with 200 within {HEALTH_TIMEOUT} s "
                            f"(last: {status or 'no answer'})")
        if alive is not None and not await alive():
            raise JobFailed(f"{what} stopped before answering /web/login")
        await asyncio.sleep(1)
    found = await version_of(port)
    if version and found and found != version:
        raise JobFailed(f"{what} runs Odoo {found}, the instance's runs {version}")
    job.log(f"{what}: /web/login 200, Odoo {found or '?'}, in {time.monotonic() - began:.1f} s")


def open_connections(ports: set[int], tables: tuple[str, ...] = ("/proc/net/tcp", "/proc/net/tcp6")) -> int:
    """The TCP connections of this machine established to 127.0.0.1 on
    ports (nginx's to an Odoo)."""
    count = 0
    for table in tables:
        try:
            rows = Path(table).read_text().splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            fields = row.split()
            if len(fields) < 4 or fields[3] != "01":
                continue
            address, _, port = fields[2].rpartition(":")
            if address in ("0100007F", "0000000000000000FFFF00000100007F") and int(port, 16) in ports:
                count += 1
    return count


async def drain(job: Job, ports: set[int], what: str) -> None:
    """Until no connection is open to ports (at least DRAIN_LEAST, at most
    DRAIN_MOST seconds)."""
    began = time.monotonic()
    await asyncio.sleep(DRAIN_LEAST)
    while True:
        left = open_connections(ports)
        waited = time.monotonic() - began
        if not left:
            job.log(f"{what}: no request open after {waited:.1f} s")
            return
        if waited >= DRAIN_MOST:
            job.log(f"{what}: {left} connections still open after {DRAIN_MOST:.0f} s (the websockets of the pages "
                    "open, which reconnect, or requests longer than that): it stops now")
            return
        await asyncio.sleep(DRAIN_EVERY)


async def _json(job: Job, cmd: list[str], what: str, timeout: float = HELPER_TIMEOUT) -> dict:
    code, out = await ops.capture(cmd, timeout=timeout)
    lines = [line for line in out.splitlines() if line.strip()]
    if code:
        raise JobFailed(f"{what}: {lines[-1] if lines else code}")
    try:
        found = json.loads(lines[-1]) if lines else None
    except ValueError:
        raise JobFailed(f"{what}: no answer the helper should give ({lines[-1][:200]})") from None
    return found if isinstance(found, dict) else {}


async def upstream(job: Job, target: Target, http_port: int | None, websocket_port: int | None = None) -> dict:
    """nginx of the environment sent to those ports (None: back to its own)."""
    args = ["-", "-"] if http_port is None else [str(http_port), str(websocket_port) if websocket_port else "-"]
    job.log(f"--- nginx of {target.url} to " + (f"127.0.0.1:{http_port}" if http_port else "its own Odoo")
            + (f" (websocket {websocket_port})" if websocket_port else ""))
    found = await _json(job, workspace_helper("upstream", target.workspace, target.storage, target.instance,
                                              target.role, *args), "nginx was not switched")
    job.log(f"nginx: {found.get('nginx') or 'reloaded'}")
    return found


async def next_state(instance) -> dict | None:
    code, out = await ops.capture(instance.restart.command("next-state"), timeout=HELPER_TIMEOUT)
    lines = [line for line in out.splitlines() if line.strip()]
    try:
        found = json.loads(lines[-1]) if code == 0 and lines else None
    except ValueError:
        found = None
    return found if isinstance(found, dict) else None


async def next_stop(job: Job, instance) -> None:
    job.log("--- the second Odoo removed")
    await ops.run(job, instance.restart.command("next-stop"), timeout=HELPER_TIMEOUT, check=False)


def copies_folder(target: Target) -> Path:
    return target.project / "addons" / NEXT_FOLDER


async def remove_copies(job: Job, instance, target: Target) -> None:
    folder = copies_folder(target)
    if not folder.exists():
        return
    for repo in instance.repositories:
        if (folder / repo.name).exists():
            await ops.capture(["git", "worktree", "remove", "--force", str(folder / repo.name)], cwd=repo.path)
        await ops.capture(["git", "worktree", "prune"], cwd=repo.path)
    shutil.rmtree(folder, ignore_errors=True)


async def make_copies(job: Job, instance, target: Target, moves: dict[str, tuple[str, str]]) -> None:
    """Each repository that moves at its new commit, in addons/NEXT_FOLDER (a git worktree)."""
    await remove_copies(job, instance, target)
    folder = copies_folder(target)
    folder.mkdir(mode=0o755)
    folder.chmod(0o755)
    repos = {r.name: r for r in instance.repositories}
    for name, (_, new) in moves.items():
        job.log(f"--- [{name}] the new code, {new[:10]}, copied for the second Odoo")
        await ops.run(job, ["git", "worktree", "add", "--detach", str(folder / name), new], cwd=repos[name].path)


async def leftover(job: Job, instance, target: Target) -> None:
    """A second Odoo of an update before (it failed after nginx was sent to
    it, or it was stopped): nginx back to the instance's own Odoo when it
    answers, the second removed, its copies too; refused when it does not."""
    found = await next_state(instance)
    if found is None:
        await remove_copies(job, instance, target)
        return
    job.log(f"--- {found.get('name')}, the second Odoo of an update before, is there")
    if await _status(f"http://127.0.0.1:{instance.options.http_port}/web/login") != 200:
        raise JobFailed(f"{found.get('name')} of an update before is there and the instance's own Odoo does not "
                        "answer: its users may be on it. Start the instance, then update again")
    await upstream(job, target, None)
    await drain(job, {p for p in (found.get("http_port"), found.get("websocket_port")) if p}, "the second Odoo")
    await next_stop(job, instance)
    await remove_copies(job, instance, target)


# --- the job -----------------------------------------------------------------

async def pull_restart(job: Job, instance, found: Target | str, only: set[str] | None, only_if_changed: bool,
                       backup_folder: Path | None, normal: Callable[[], Awaitable[None]]) -> None:
    """Git pull and restart without stopping, or the normal one (normal())
    with why not."""
    if isinstance(found, str):
        job.warn(f"Not without stopping: {found}. The update restarts Odoo: its users wait for it")
        await normal()
        return
    target = found
    job.log(f"--- update without stopping: a second Odoo on the new code, then nginx of {target.url} to it")
    await leftover(job, instance, target)
    moves = {}
    for repo in instance.repositories:
        if only is not None and repo.name not in only:
            continue
        job.log(f"--- [{repo.name}] fetch {repo.remote}/{repo.branch}")
        branch = await ops.git(repo, "rev-parse", "--abbrev-ref", "HEAD")
        if branch != repo.branch:
            raise JobFailed(f"[{repo.name}] {repo.path} is on branch '{branch}', expected '{repo.branch}'")
        await ops.run(job, repo.git("fetch", repo.remote, repo.branch), cwd=repo.path)
        old, new = await ops.git(repo, "rev-parse", "HEAD"), await ops.git(repo, "rev-parse", "FETCH_HEAD")
        if old == new:
            job.log(f"[{repo.name}] already up to date")
            continue
        code, _ = await ops.capture(["git", "merge-base", "--is-ancestor", old, new], cwd=repo.path)
        if code:
            raise JobFailed(f"[{repo.name}] {new[:10]} does not follow {old[:10]}: not a fast-forward, git pull "
                            "--ff-only refuses it")
        job.log(await ops.git(repo, "log", "--oneline", f"{old}..{new}"))
        moves[repo.name] = (old, new)
    if not moves:
        job.log("Nothing new to deploy" if only_if_changed else "Nothing new: Odoo is not restarted")
        return
    await switch(job, instance, target, moves, "pull", backup_folder, normal)


async def switch(job: Job, instance, target: Target, moves: dict[str, tuple[str, str]], how: str,
                 backup_folder: Path | None, normal: Callable[[], Awaitable[None]], always_backup: bool = False) -> None:
    """The steps 2-7 of the module's text on moves {repository: (commit now,
    commit then)}; how: pull (a fast-forward) or back (git reset --hard)."""
    reasons = await blockers(instance, target, moves)
    probe = Probe(target.url).start()
    if reasons:
        for reason in reasons[:LISTED]:
            job.warn(f"Not without stopping: {reason}")
        if len(reasons) > LISTED:
            job.warn(f"Not without stopping: {len(reasons) - LISTED} more reasons")
        job.log("--- the normal update follows: Odoo restarts, its users wait for it")
        try:
            await normal()
        except BaseException:
            said(job, await probe.stop(), "the normal update, a restart", target.url)
            raise
        # the restart's job ends when the container has started, its Odoo answers a moment later
        await probe.settle()
        said(job, await probe.stop(), "the normal update, a restart", target.url)
        return
    try:
        await _switch(job, instance, target, moves, how, backup_folder, always_backup)
    finally:
        said(job, await probe.stop(), "without stopping", target.url)


async def _switch(job: Job, instance, target: Target, moves: dict[str, tuple[str, str]], how: str,
                  backup_folder: Path | None, always_backup: bool) -> None:
    opts, port = instance.options, instance.options.http_port
    version = await version_of(port)
    job.log(f"--- the old Odoo keeps serving (port {port}, Odoo {version or '?'}): what changes is of the kinds it "
            "reads with its code")
    await ops.backup_before_update(job, instance, backup_folder, always=always_backup)
    await make_copies(job, instance, target, moves)
    started = None
    try:
        job.log("--- the changed modules updated with the new code, in a container of its own")
        try:
            await ops.run(job, instance.restart.command("next-update", opts.odoo_service, opts.database),
                          timeout=UPDATE_TIMEOUT)
        except JobFailed as exc:
            raise JobFailed(f"the update of the modules with the new code failed ({exc}): the old Odoo keeps serving, "
                            "its code was not moved; the database keeps what Odoo committed before the error") from exc
        job.log("--- a second Odoo on the new code, no cron thread")
        started = await _json(job, instance.restart.command("next-start", opts.odoo_service),
                              "the second Odoo did not start", timeout=HEALTH_TIMEOUT)
        job.log(f"The second Odoo is {started.get('container')}, on port {started.get('http_port')}"
                + (f" (websocket {started['websocket_port']})" if started.get("websocket_port") else ""))

        async def alive() -> bool:
            state = await next_state(instance)
            return bool(state and state.get("running"))

        await healthy(job, int(started["http_port"]), version, "the second Odoo", alive)
        sent = await upstream(job, target, int(started["http_port"]), started.get("websocket_port"))
    except BaseException as exc:
        if started is not None or await next_state(instance) is not None:
            await next_stop(job, instance)
        await remove_copies(job, instance, target)
        if isinstance(exc, JobFailed):
            job.log("Nothing changed for the users: the old Odoo serves them as before")
        raise
    # from here the users are on the second Odoo
    before = sent.get("from") if isinstance(sent.get("from"), dict) else {}
    await drain(job, {p for p in (port, before.get("http_port"), before.get("websocket_port")) if p}, "the old Odoo")
    repos = {r.name: r for r in instance.repositories}
    for name, (old, new) in moves.items():
        repo = repos[name]
        job.log(f"--- [{name}] {old[:10]} → {new[:10]}")
        if how == "back":
            await ops.run(job, repo.git("reset", "--hard", new), cwd=repo.path)
        else:
            await ops.run(job, repo.git("merge", "--ff-only", new), cwd=repo.path)
        await ops.update_submodules(job, repo)
    stay = (f"its users are served by {started.get('container')}, the new code on port {started['http_port']}, "
            "until it is fixed: Update again takes it away")
    try:
        await ops.restart(job, instance)
        await healthy(job, port, version, "the instance's Odoo on the new code")
    except JobFailed as exc:
        raise JobFailed(f"{exc}: {stay}") from exc
    try:
        await upstream(job, target, None)
    except JobFailed as exc:
        raise JobFailed(f"{exc}: {stay}") from exc
    await drain(job, {p for p in (started.get("http_port"), started.get("websocket_port")) if p}, "the second Odoo")
    await next_stop(job, instance)
    await remove_copies(job, instance, target)

