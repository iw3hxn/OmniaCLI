# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A workspace's instance created from a template, as a job: the steps that
go through the root helpers (workspacehelper.py, composehelper.py)."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import sys
import tempfile
from pathlib import Path

from . import config, moduleupload, ops, stacktemplates, templatestore, workspacehelper
from .config import OMNIACLI_COMMAND, Workspace, environment_names, generated_names
from .jobs import Job, JobFailed
from .ops import capture, run

# the disk of a workspace may take a while to be made (mkfs), a database
# minutes to be created (the modules installed)
HELPER_TIMEOUT = 300
INIT_TIMEOUT = 1800


def helper(*args: str) -> list[str]:
    return ["sudo", "-n", str(OMNIACLI_COMMAND), "workspace-helper", *args]


def compose(project: str, *args: str) -> list[str]:
    return ["sudo", "-n", str(OMNIACLI_COMMAND), "compose-helper", project, *args]


def templates() -> dict[str, stacktemplates.Template]:
    """The templates the root helper creates instances from."""
    return stacktemplates.available([workspacehelper.TEMPLATES, templatestore.INSTALLED])


def storage_path(ws: Workspace) -> Path:
    """The root path the workspace's storage is bound on: what root uses, so
    what OmniaCLI reads and writes too (README, Workspaces)."""
    return Path(config.bound_root(ws.storage))


def server_name(ws: Workspace, instance: str) -> str:
    return f"{ws.name}-{instance}"


DOCKER_MISSING = "Docker is not installed on this machine: the instances of a workspace are Docker Compose projects"


def docker_missing() -> str | None:
    """Why a workspace instance cannot be made here (no docker), else None."""
    return None if shutil.which("docker", path=os.environ.get("PATH", "") + ":/usr/bin:/usr/local/bin") else DOCKER_MISSING


def workspacehelper_remote_key() -> str:
    from .workspacehelper import REMOTE_KEY
    return REMOTE_KEY


async def _helper(job: Job, *args: str, timeout: float = HELPER_TIMEOUT, input: bytes | None = None) -> dict:
    """input: what the helper reads on its input (a secret: never an argument, never logged)."""
    job.log("$ " + " ".join(helper(*args)))
    code, out = await capture(helper(*args), timeout=timeout, input=input)
    if code:
        raise JobFailed(out or f"workspace-helper {args[0]} failed ({code})")
    try:
        return json.loads(out.splitlines()[-1])
    except (ValueError, IndexError):
        raise JobFailed(f"workspace-helper {args[0]} answered no JSON: {out}") from None


async def ask(*args: str, timeout: float = HELPER_TIMEOUT, input: bytes | None = None) -> dict:
    """A helper's answer read for a page, without a job; input: what the
    helper reads on its input (never an argument, never logged)."""
    code, out = await capture(helper(*args), timeout=timeout, input=input)
    if code:
        raise JobFailed(out or f"workspace-helper {args[0]} failed ({code})")
    try:
        return json.loads(out.splitlines()[-1])
    except (ValueError, IndexError):
        raise JobFailed(f"workspace-helper {args[0]} answered no JSON: {out}") from None


def _limit(ws: Workspace, key: str) -> str:
    return f"{ws.limits.get(key) or 0:g}"


async def apply_limits(job: Job, ws: Workspace) -> dict:
    """The workspace's CPU and memory: its systemd slice, written again;
    the containers running in it take the new limits."""
    cpus, memory = _limit(ws, "cpus"), _limit(ws, "memory_gb")
    job.log(f"--- the CPU and memory of workspace {ws.name}: "
            + (f"{cpus} cores" if cpus != "0" else "CPU not limited") + ", "
            + (f"{memory} GB" if memory != "0" else "memory not limited"))
    return await _helper(job, "limits", ws.name, cpus, memory)


async def create_instance(job: Job, ws: Workspace, instance: str, template: stacktemplates.Template,
                          values: dict[str, str], setup: dict | None = None, remote: dict | None = None) -> dict:
    """The disk, the project of its production, its database, started: the
    server's data for the servers file (its production, type docker). The
    copies to try things on are stagings beside it (stagings.py), not a
    test of its own. setup: the admin's password and
    the company's country of the new database (compose-helper setup), as the
    New instance dialog asks them; never in the job's log."""
    if not ws.storage or not ws.limits.get("disk_gb"):
        raise JobFailed(f"workspace {ws.name} has no storage or no disk limit: the machine's admins set them")
    # the connection of a database of another machine may come among the values (the app's make_instance)
    values = dict(values)
    given = values.pop(workspacehelper_remote_key(), None)
    remote = remote or (given if isinstance(given, dict) else None)
    job.log(f"--- the disk of workspace {ws.name} ({ws.limits['disk_gb']:g} GB, in {ws.storage})")
    await _helper(job, "disk", ws.name, ws.storage, str(int(ws.limits["disk_gb"])))
    await apply_limits(job, ws)
    job.log(f"--- instance {instance} from template {template.name} {template.version}")
    # a database of another machine (AD-003): its connection on the helper's input, said without its password
    if remote:
        job.log(f"--- its database is {remote.get('database')} on {remote.get('host')}:{remote.get('port')}, "
                f"as {remote.get('user')}, over TLS verified against the CA given")
        if remote.get("replica"):
            job.log("--- its read replica is %s:%s: Odoo reads from it and writes to the database" % tuple(remote["replica"]))
    made = await _helper(job, "create", ws.name, ws.storage, instance, template.name,
                         json.dumps({**values, "_roles": ["production"],
                                     **({workspacehelper_remote_key(): "-"} if remote else {})}),
                         input=json.dumps(remote).encode() if remote else None)
    try:
        modules = values.get("INIT_MODULES") or "base"
        language = values.get("INIT_LANG") or ""
        for role, info in made["roles"].items():
            job.log(f"--- {role}: database {made['database']} with {modules}")
            await run(job, compose(info["folder"], "init", made["odoo_service"], made["database"], modules,
                                   *([language] if language else [])), timeout=INIT_TIMEOUT)
            await run(job, compose(info["folder"], "start"), timeout=HELPER_TIMEOUT)
            if setup:
                said = (["the admin's password"] if setup.get("password") else []) + \
                    ([f"the country {setup['country']}"] if setup.get("country") else [])
                job.log("--- " + " and ".join(said))
                await _setup(job, info["folder"], made["odoo_service"], made["database"], setup)
    except BaseException:
        job.log(f"--- the creation failed: instance {instance} removed")
        await run(job, helper("remove", ws.name, ws.storage, instance), check=False, timeout=HELPER_TIMEOUT)
        raise
    if setup and setup.get("password"):
        job.log("The new database has the user admin with the password chosen in the form.")
    else:
        job.log("The new database has the user admin with password admin: change it at the first login.")
    return {
        "type": "docker",
        **{role: {"database": made["database"], "odoo_service": made["odoo_service"],
                  "db_service": made["db_service"], "http_port": info["http_port"], "update_modules": True,
                  # production: a backup before every update of its code or modules
                  "backup_before_update": role == "production",
                  "restart": {"type": "compose", "service": info["folder"], "sudo": True}, "repositories": []}
           for role, info in made["roles"].items()},
    }


async def _setup(job: Job, folder: str, odoo_service: str, database: str, setup: dict) -> None:
    """compose-helper setup, its values as JSON on its input through a file
    only OmniaCLI reads, removed at once: the password is on no command
    line and in no log."""
    with tempfile.TemporaryDirectory(prefix="omniacli-setup-") as tmp:
        values = Path(tmp) / "setup.json"
        values.touch(mode=0o600)
        values.write_text(json.dumps({k: v for k, v in setup.items() if v}))
        with values.open("rb") as stdin:
            await run(job, compose(folder, "setup", odoo_service, database), stdin=stdin, timeout=INIT_TIMEOUT)


async def drop_disk(job: Job | None, ws: Workspace) -> str:
    """The workspace's disk taken away (workspace-helper drop: an empty one
    only); what happened, said for the job or the page."""
    if not ws.storage:
        return "no disk"
    if job is not None:
        job.log(f"--- the disk of workspace {ws.name}, empty: taken away")
    code, out = await capture(helper("drop", ws.name, ws.storage), timeout=HELPER_TIMEOUT)
    if code:
        return f"its disk stays: {out.splitlines()[-1] if out else code}"
    try:
        made = json.loads(out.splitlines()[-1])
    except (ValueError, IndexError):
        made = {}
    made = made if isinstance(made, dict) else {}
    # its slice goes with it, and the customer's certificate: a workspace made again with the name has none
    return f"its disk ({made.get('size_gb', 0)} GB) taken away" + (
        ", the customer's certificate with it" if made.get("certificate") else "")


async def remove_instance(job: Job, ws: Workspace, instance: str, linked: bool = False) -> None:
    """linked: its database is on the cluster of a linked OmniaCLI, which the
    application asks to take it back after this (not a third party's, which stays)."""
    removed = await _helper(job, "remove", ws.name, ws.storage, instance)
    for where in () if linked else removed.get("database_left_on") or []:
        # a database of another machine is not this machine's to drop (AD-003)
        job.log(f"Its database stays on {where}: it is of who runs that machine, to drop there")


class SpaceError(Exception):
    """The root helper did not tell the room of the disks."""


# what an admin does when sudo refuses the workspaces' helpers (README, Workspaces)
HELPERS_HINT = ("sudo does not allow the workspaces' helpers to OmniaCLI's user: as root, add "
                "OMNIACLI_WORKSPACES=1 to /etc/default/omniacli, then run dpkg-reconfigure omniacli "
                "(it writes /etc/sudoers.d/omniacli-workspaces and makes the default storage)")


def helpers_refused(message: str) -> bool:
    """sudo's own refusal (no line of the sudoers for the helper), not the helper's."""
    return message.startswith("sudo:") or "is not allowed to run sudo" in message


async def space() -> dict:
    """The root helper's space: each storage root's real disk and the
    workspaces' disks on it, in bytes."""
    code, out = await capture(helper("space"), timeout=HELPER_TIMEOUT)
    if code:
        raise SpaceError(out or f"workspace-helper space failed ({code})")
    try:
        return json.loads(out.splitlines()[-1])
    except (ValueError, IndexError):
        raise SpaceError(f"workspace-helper space answered no JSON: {out}") from None


GB = workspacehelper.GB


def limit_bytes(ws: Workspace) -> int:
    return int(float(ws.limits.get("disk_gb") or 0) * GB)


def _disks(found: dict, workspaces: dict[str, Workspace], device) -> dict[tuple[str, str], dict]:
    """The workspaces' disks on one real disk, {(root, workspace): {promised,
    used}}: a workspace's limit or its image, the larger (an image no
    workspace names any more still grows up to its size)."""
    roots = {r["root"]: r for r in found.get("roots", []) if not r.get("missing") and r.get("device") == device}
    disks: dict[tuple[str, str], dict] = {}
    for root, info in roots.items():
        for name, image in info.get("images", {}).items():
            disks[(root, name)] = {"promised": image["size"], "used": image["used"]}
    for ws in workspaces.values():
        if ws.storage in roots:
            entry = disks.setdefault((ws.storage, ws.name), {"promised": 0, "used": 0})
            entry["promised"] = max(entry["promised"], limit_bytes(ws))
    return disks


def room(found: dict, workspaces: dict[str, Workspace], storage: str, name: str | None) -> dict | None:
    """What workspace name (None: a new one) may have on storage, in bytes:
    the room free on the real disk, what the other workspaces' disks there
    were promised and do not take yet (reserved), and so the largest disk
    without taking it from them (allowed) and the largest that fits now
    (physical, an admin's choice: README, Workspaces). None: storage unknown
    to the helper or missing."""
    info = next((r for r in found.get("roots", []) if r.get("root") == storage), None)
    if info is None or info.get("missing"):
        return None
    disks = _disks(found, workspaces, info["device"])
    own = disks.get((storage, name), {"promised": 0, "used": 0}) if name else {"promised": 0, "used": 0}
    others = {key: d for key, d in disks.items() if key != (storage, name)}
    reserved = sum(max(d["promised"] - d["used"], 0) for d in others.values())
    image = info.get("images", {}).get(name, {}) if name else {}
    return {"total": info["total"], "free": info["free"], "reserved": reserved,
            "promised": sum(d["promised"] for d in disks.values()), "used": sum(d["used"] for d in disks.values()),
            "own_used": own["used"], "own_size": image.get("size", 0),
            "allowed": max(info["free"] + own["used"] - reserved, 0), "physical": info["free"] + own["used"]}


def disk_problem(found: dict | None, workspaces: dict[str, Workspace], ws: Workspace, force: bool) -> str | None:
    """Why ws's disk limit may not be saved, or None. Without force, the disk
    may not take what the other workspaces on the same real disk were
    promised; with force, it must still fit in the room free now."""
    limit = limit_bytes(ws)
    if not limit or not ws.storage:
        return None
    if found is None:
        return None if force else "the room of the disks cannot be read: force it to save anyway"
    r = room(found, workspaces, ws.storage, ws.name)
    if r is None:
        return None if force else f"{ws.storage} is not there yet: force it to save anyway"
    gb = lambda value: f"{value / GB:.1f} GB"  # noqa: E731
    if limit < r["own_size"]:
        return f"the disk of {ws.name} is {gb(r['own_size'])} already: a disk is not made smaller"
    if limit > r["physical"]:
        return (f"{gb(limit)} do not fit on the real disk of {ws.storage}: {gb(r['physical'])} at most "
                f"({gb(r['free'])} free" + (f", {gb(r['own_used'])} taken by {ws.name}" if r["own_used"] else "") + ")")
    if limit > r["allowed"] and not force:
        return (f"{gb(limit)} take room promised to other workspaces on the real disk of {ws.storage}: "
                f"{gb(r['allowed'])} without it ({gb(r['free'])} free, {gb(r['reserved'])} promised and not used yet); "
                f"force it to promise more than there is")
    return None


def web_spec(ws: Workspace) -> str:
    """The names of the workspace for the root helper (it reads the ports)."""
    # the environments published through a relay (relay.py): their block on this loopback, out of the certificate
    relayed = {i: [r for r in ("production", "test") if ws.relayed(i, r)] for i in ws.instances()}
    return json.dumps({"domain": ws.domain, "main": ws.main, "subdomains": ws.subdomains,
                       "certificate": ws.certificate, "aliases": ws.aliases, "redirects": ws.redirects,
                       "relayed": {i: r for i, r in relayed.items() if r}})


async def publish(job: Job, ws: Workspace) -> dict:
    """The workspace's nginx site and its certificate: a certificate that
    fails leaves the site on http (port 80 answers 503 until then)."""
    if not ws.storage:
        job.log(f"--- workspace {ws.name} has no storage: no site")
        return {}
    job.log(f"--- the names of workspace {ws.name}" + (f" under {ws.domain}" if ws.domain else ": none (no domain)"))
    made = await _helper(job, "web", ws.name, ws.storage, web_spec(ws))
    relayed = set(made.get("relayed_names") or [])
    for name in made.get("names", []):
        if name in relayed:
            continue  # served by its relay: said by the relay's part of the job
        job.log(("https://" if made.get("https") else "http://") + name)
    for name, to in (made.get("redirects") or {}).items():
        job.log(f"{name} → {to}")
    for note in made.get("redirects_moved") or []:
        job.log(note)
    job.log(f"nginx: {made.get('nginx')}")
    if made.get("without_dns"):
        job.log("Not in DNS yet, so not in the certificate (point them to this machine, then publish again): "
                + ", ".join(made["without_dns"]))
    if made.get("elsewhere"):
        job.log("In DNS, but pointing to another machine, so not in the certificate (point them here, "
                "then publish again): " + ", ".join(made["elsewhere"]))
    if made.get("certificate_error"):
        job.log(f"The certificate is not there yet: {made['certificate_error']}")
    for warning in certificate_warnings(made, opened_elsewhere(ws, made)):
        job.warn(warning)
    return made


def opened_elsewhere(ws: Workspace, made: dict) -> set[str]:
    """The workspace's names left out of the certificate whose environment
    has another name that is in it: the environment is opened at that one
    (config.opening_name), so the name left out is a line of the job's log,
    not a warning of every publication (a staging's stg-<date>.<domain>
    without a wildcard record, beside its automatic name)."""
    left = set(made.get("without_dns") or []) | set(made.get("elsewhere") or [])
    found = set()
    for name, (instance, role) in generated_names(ws).items():
        if name in left and any(n not in left for n in environment_names(ws, instance, role)):
            found.add(name)
    return found


# what certbot says when the names do not lead to this machine's nginx
_DNS_REFUSALS = ("dns problem", "nxdomain", "no valid ip addresses", "unauthorized", "connection refused",
                 "timeout during connect", "invalid response", "no name of the workspace is in dns")
# Let's Encrypt did not reach the challenge here (its details only in certbot's log)
_UNREACHED = ("challenges have failed", "failed to authenticate")


def certificate_warnings(made: dict, covered=()) -> list[str]:
    """The web helper's result said as the warnings of its job: the names
    whose certificate did not come and why, one sentence each. covered: the
    names left out whose environment is opened at another name (opened_elsewhere): not said."""
    out_of_it = list(made.get("without_dns") or []) + list(made.get("elsewhere") or [])
    left = [n for n in out_of_it if n not in covered]
    known = [n for n in made.get("names") or [] if n not in out_of_it]
    error = str(made.get("certificate_error") or "")
    out = []
    if error:
        names = ", ".join(known or left) or "the workspace"
        if any(word in error.lower() for word in _DNS_REFUSALS):
            out.append(f"The certificate for {names} did not come: the DNS does not point here")
        elif any(word in error.lower() for word in _UNREACHED):
            out.append(f"The certificate for {names} did not come: the DNS does not point here, "
                       "or port 80 is not open to the internet")
        elif "upload it" in error:
            out.append(f"The certificate for {names} is not there: the customer's certificate is not uploaded yet")
        else:
            out.append(f"The certificate for {names} did not come: Let's Encrypt refused it (see the log)")
    if left and (known or not error):
        out.append(f"Left out of the certificate, on http only: {', '.join(left)}: the DNS does not point here")
    return out


async def own_certificate(ws: Workspace, certificate: str, key: str) -> tuple[bool, str]:
    """The customer's certificate to the root helper, on its standard input."""
    if not ws.storage:
        return False, f"workspace {ws.name} has no storage"
    code, out = await capture(helper("owncert", ws.name, ws.storage), timeout=HELPER_TIMEOUT,
                              input=json.dumps({"certificate": certificate, "key": key}).encode())
    return code == 0, out


def instance_folder(ws: Workspace, instance: str) -> Path:
    return storage_path(ws) / ws.name / instance


# a customer's repository: https only, no user or token in it (they are the
# interface's git credentials), no query
REPOSITORY_URL = re.compile(r"https://[A-Za-z0-9.-]+(?::\d{1,5})?/[A-Za-z0-9._~/-]+")
# over ssh, with OmniaCLI's git key (gitcredentials.GIT_KEY_NAME) as a deploy key
REPOSITORY_SSH = re.compile(r"(?:git@[A-Za-z0-9.-]+:|ssh://git@[A-Za-z0-9.-]+(?::\d{1,5})?/)[A-Za-z0-9._~][A-Za-z0-9._~/-]*")
BRANCH = re.compile(r"[A-Za-z0-9._/-]{1,200}")
CLONE_TIMEOUT = 1800


def check_repository(name: str, url: str, branch: str) -> None:
    if not workspacehelper.REPOSITORY.fullmatch(name):
        raise JobFailed(f"{name!r} is not a repository name (a-z A-Z 0-9 . _ -)")
    if not (REPOSITORY_URL.fullmatch(url) or REPOSITORY_SSH.fullmatch(url)) or ".." in url:
        raise JobFailed(f"{url!r} is not the address of a repository: https://host/org/repo.git (no user or token in "
                        "it: those are the workspace's git credentials) or git@host:org/repo.git (OmniaCLI's git key)")
    check_branch(branch)


def addons_folder(ws: Workspace, instance: str, role: str) -> Path:
    return storage_path(ws) / ws.name / instance / role / "addons"


def check_branch(branch: str) -> None:
    if not BRANCH.fullmatch(branch) or branch.startswith(("-", "/")) or ".." in branch:
        raise JobFailed(f"{branch!r} is not a branch name")


def web_url(url: str) -> str | None:
    """The web page of a repository from its address (https or git@/ssh://),
    without .git; None when it is not one of these."""
    # a user and a token in the address never reach a page
    found = (re.fullmatch(r"https://(?:[^/@]*@)?([^/@]+)/(.+?)(?:\.git)?/?", url)
             or re.fullmatch(r"git@([^:]+):(.+?)(?:\.git)?/?", url)
             or re.fullmatch(r"ssh://git@([^/:]+)(?::\d+)?/(.+?)(?:\.git)?/?", url))
    return f"https://{found[1]}/{found[2]}" if found else None


def compare_url(url: str, old: str, new: str) -> str | None:
    """The page of the differences between two commits, on GitHub and GitLab."""
    web = web_url(url)
    if web is None or old == new:
        return None
    host = web.split("/")[2]
    if host == "github.com":
        return f"{web}/compare/{old}...{new}"
    if "gitlab" in host:
        return f"{web}/-/compare/{old}...{new}"
    return None


async def repository_branches(repo: config.Repository, timeout: float = 60) -> dict:
    """What the Update dialog of a repository shows: the commit checked out,
    and every branch of its remote with its last commit (git ls-remote, with
    the server's git credentials)."""
    if not repo.path.is_dir():
        raise JobFailed(f"{repo.path} does not exist")
    code, head = await capture(["git", "rev-parse", "HEAD"], cwd=repo.path)
    if code:
        raise JobFailed(f"[{repo.name}] {head.splitlines()[-1] if head else 'not a git repository'}")
    code, out = await capture(repo.git("ls-remote", "--heads", repo.remote), cwd=repo.path, timeout=timeout)
    if code:
        raise JobFailed(f"[{repo.name}] the branches of {repo.remote} cannot be read: "
                        f"{out.splitlines()[-1] if out else code}")
    branches = []
    for line in out.splitlines():
        sha, _, ref = line.partition("\t")
        if ref.startswith("refs/heads/"):
            branches.append({"name": ref[len("refs/heads/"):], "commit": sha})
    _, url = await capture(["git", "remote", "get-url", repo.remote], cwd=repo.path)
    return {"branch": repo.branch, "commit": head, "url": url,
            "branches": sorted(branches, key=lambda b: b["name"])}


async def update_repository(job: Job, repo: config.Repository, branch: str) -> bool:
    """The repository on the last commit of a branch: its own (a fast-forward
    pull) or another (fetched and checked out), with its submodules; refused
    with uncommitted changes. True when the commit checked out changed."""
    check_branch(branch)
    changes = (await ops.git(repo, "status", "--porcelain", "--untracked-files=no"))
    if changes:
        job.log(changes)
        raise JobFailed(f"[{repo.name}] {repo.path} has uncommitted changes")
    before = await ops.git(repo, "rev-parse", "HEAD")
    if branch == repo.branch:
        job.log(f"--- [{repo.name}] pull {repo.remote}/{branch}")
        await ops.ensure_ready(job, repo)
        await run(job, repo.git("pull", "--ff-only", repo.remote, branch), cwd=repo.path)
    else:
        job.log(f"--- [{repo.name}] from {repo.branch} to {branch}")
        await run(job, repo.git("fetch", repo.remote, f"+refs/heads/{branch}:refs/remotes/{repo.remote}/{branch}"),
                  cwd=repo.path)
        await run(job, repo.git("checkout", "-B", branch, "--track", f"{repo.remote}/{branch}"), cwd=repo.path)
    await ops.update_submodules(job, repo)
    after = await ops.git(repo, "rev-parse", "HEAD")
    if before == after:
        job.log(f"[{repo.name}] already on {after[:10]}")
        return False
    code, log = await capture(["git", "log", "--oneline", f"{before}..{after}"], cwd=repo.path)
    job.log(log if code == 0 and log else f"[{repo.name}] {before[:10]} → {after[:10]}")
    return True


def _remove_tree(path: Path) -> None:
    """A clone of OmniaCLI's user, its read-only folders and objects included."""
    def retry(func, target, _exc):
        for folder in (os.path.dirname(target), target):
            if os.path.isdir(folder) and not os.path.islink(folder):
                os.chmod(folder, stat.S_IRWXU)
        func(target)
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:
        shutil.rmtree(path, onerror=retry)


async def clone(job: Job, ws: Workspace, instance: str, role: str, name: str, url: str, branch: str,
                git_options: tuple = ()) -> Path:
    """The customer's repository cloned, with its submodules, into the role's
    addons/ (OmniaCLI's own folder there), with the server's git credentials."""
    check_repository(name, url, branch)
    target = addons_folder(ws, instance, role) / name
    if target.exists() or target.is_symlink():
        raise JobFailed(f"{target} exists already")
    job.log(f"--- clone {url} ({branch}) into {target}")
    try:
        await run(job, ["git", *git_options, "clone", "--branch", branch, "--recurse-submodules", "--", url,
                        str(target)], timeout=CLONE_TIMEOUT)
    except BaseException:
        if target.exists():
            _remove_tree(target)
        raise
    return target


async def write_conf(job: Job, ws: Workspace, instance: str, role: str, repositories: list[str]) -> dict:
    """The role's odoo.conf again, its addons path naming these repositories,
    and the folder of the modules uploaded as a .zip when the role has it
    (moduleupload.py: it stays in the addons path whatever repository is
    added or taken away)."""
    uploads = addons_folder(ws, instance, role) / moduleupload.FOLDER
    if uploads.is_dir() and not uploads.is_symlink() and moduleupload.FOLDER not in repositories:
        repositories = [*repositories, moduleupload.FOLDER]
    job.log(f"--- odoo.conf of {role}: the addons of {', '.join(repositories) or 'no repository'}")
    return await _helper(job, "conf", ws.name, ws.storage, instance, role, json.dumps({"repositories": repositories}))


def remove_clone(path: Path) -> None:
    if path.exists():
        _remove_tree(path)
