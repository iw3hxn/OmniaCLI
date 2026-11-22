# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Add a machine (README, Machines): a clean Ubuntu or Debian server given by
its address and an SSH user, prepared from here as a hosting panel does, then
linked to this OmniaCLI.

1. The admin puts OmniaCLI's provisioning key (sshlinks.PROVISION_KEY_NAME,
   not the links' key) in the root's authorized_keys there, or of a user
   with sudo without a password, and gives the server's address: the rest
   has its default (the name from the address, port 22, root, the host key
   read on the first connection, as ssh asks to accept it) and may be
   changed under the form's advanced options. check_access tries the key
   before anything is prepared.
2. A job runs provision.sh there through ssh (its text on bash's input,
   nothing copied there first): updates, nginx, certbot, Docker, OmniaCLI
   from its package with the workspaces allowed, a user that may only
   forward to OmniaCLI, the link's token hash, the firewall.
3. The link is saved here, through SSH (sshlinks.py), with that user: the
   machine is on the Dashboard under Linked, and its OmniaCLI (its
   workspaces, its instances) is opened through the link.

ssh reads no configuration, agent or known_hosts of OmniaCLI's user: only
the provisioning key and the host key the admin confirmed."""

from __future__ import annotations

import asyncio
import base64
import os
import re
import shlex
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import sshlinks
from .config import SSH_HOST, SSH_HOST_KEY, SSH_USER, LinkTarget, SshRoute
from .jobs import Job, JobFailed
from .ops import run

PROVISION_SCRIPT = Path(__file__).with_name("provision.sh")
# the package of the latest release, as the README's Quick start installs it;
# OMNIACLI_PACKAGE_URL in OmniaCLI's environment: another one (a fork's)
PACKAGE_URL = os.environ.get("OMNIACLI_PACKAGE_URL") or \
    "https://github.com/OmniaGit/OmniaCLI/releases/latest/download/OmniaCLI.deb"
# the user on the new machine whose key may only forward to OmniaCLI
LINK_USER = "omniacli-link"
# OmniaCLI there, as the package installs it: the link's URL through SSH
REMOTE_URL = "http://127.0.0.1:8090"
# updates and packages on a slow machine
PROVISION_TIMEOUT = 3600
# seconds the link has to answer once saved (the tunnel starts within 25)
LINK_WAIT = 60
LINK_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
# the add job's title (app.py), read back for the machine's card
JOB_TITLE = re.compile(r"Add machine (\S+) \((.+)\)")
# the job's steps, by the "--- " lines provision.sh (step) and app.py write;
# a mark ending with a space is a prefix
STEPS = ("prepare ", "the system", "updates", "nginx, certbot and Docker", "OmniaCLI", "the link back",
         "the link's token", "the Odoo instances", "the firewall", "the link ", "the instances in OmniaCLI")
# what the machine has: clean (a new one: everything installed, workspaces,
# firewall) or existing (it already runs Odoo, a hosting panel's server: only
# OmniaCLI and the link added, its Odoo made ready by adopt.py, then found
# by Discover and activated through the link)
KINDS = ("clean", "existing")


def progress(lines: list[str]) -> int:
    """The index in STEPS of the last step a job's log reached (0: none yet)."""
    reached = 0
    for line in lines:
        text = line.split(" ", 1)[-1]
        if not text.startswith("--- "):
            continue
        what = text[4:]
        found = next((i for i, mark in enumerate(STEPS) if what == mark), None)
        if found is None:
            found = next((i for i, mark in enumerate(STEPS) if mark.endswith(" ") and what.startswith(mark)), None)
        if found is not None:
            reached = max(reached, found)
    return reached


def here_name(hostname: str) -> str:
    """This OmniaCLI's name in the users file there: its machine's name, as
    a link name may be."""
    name = re.sub(r"[^a-z0-9_-]+", "-", hostname.lower().split(".")[0]).strip("-_")[:32]
    return name if LINK_NAME.fullmatch(name or "") else "omniacli"


class MachineError(Exception):
    """What the admin is told: a value that cannot be used."""


@dataclass
class Machine:
    """What the Add machine form gives."""

    name: str  # the link's name here, and this OmniaCLI's name there
    host: str
    port: int
    user: str
    host_key: str
    package_url: str = PACKAGE_URL
    firewall: bool = True
    # the name this OmniaCLI has in the users file there (its auth.links)
    here: str = "omniacli"
    kind: str = "clean"

    @classmethod
    def parse(cls, data: dict, here: str) -> "Machine":
        host = str(data.get("host") or "").strip()
        if not SSH_HOST.fullmatch(host):
            raise MachineError(f"{host!r} is not a host name or an IP address")
        # as typed, made a link's name: Acme Cloud → acme-cloud
        given = re.sub(r"[^a-z0-9_-]+", "-", str(data.get("name") or "").strip().lower()).strip("-_")
        name = given or name_from_host(host)
        if not LINK_NAME.fullmatch(name):
            raise MachineError("Name: lowercase letters, digits, - and _, 32 at most")
        port = data.get("port") or 22
        if isinstance(port, bool) or not str(port).isdigit() or not 1 <= int(port) <= 65535:
            raise MachineError(f"{port!r} is not a port")
        user = str(data.get("user") or "root").strip()
        if not SSH_USER.fullmatch(user):
            raise MachineError(f"{user!r} is not a user name")
        # blank: read on the first connection (the add endpoint does)
        host_key = str(data.get("host_key") or "").strip()
        if host_key and not SSH_HOST_KEY.fullmatch(host_key):
            raise MachineError("The server's host key is not a key")
        package_url = str(data.get("package_url") or PACKAGE_URL).strip()
        if not re.fullmatch(r"https://[^\s'\"]+", package_url):
            raise MachineError("Package: an https:// address of an omniacli .deb")
        if not LINK_NAME.fullmatch(here):
            here = "omniacli"
        kind = str(data.get("kind") or "clean")
        if kind not in KINDS:
            raise MachineError(f"{kind!r}: the server is clean or existing")
        # a machine at work keeps its firewall as it is
        firewall = bool(data.get("firewall", True)) and kind == "clean"
        return cls(name, host, int(port), user, host_key, package_url, firewall, here, kind)


def name_from_host(host: str) -> str:
    """A link's name from the machine's address: erp.acme.it → erp-acme-it,
    203.0.113.10 → 203-0-113-10."""
    name = re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-")[:32].rstrip("-")
    return name if LINK_NAME.fullmatch(name or "") else "machine"


def ssh_command(machine: Machine, key: Path, known_hosts: Path, remote: str, ssh: str = "ssh") -> list[str]:
    """ssh to the machine as its user, only the provisioning key and only its
    confirmed host key, the remote command as one string."""
    options = {
        "IdentitiesOnly": "yes", "IdentityAgent": "none", "BatchMode": "yes",
        "StrictHostKeyChecking": "yes", "UserKnownHostsFile": str(known_hosts),
        "GlobalKnownHostsFile": "/dev/null", "HostKeyAlias": f"omniacli-machine-{machine.name}",
        "CheckHostIP": "no", "UpdateHostKeys": "no", "ConnectTimeout": "15",
        "ServerAliveInterval": "15", "ServerAliveCountMax": "8",
    }
    cmd = [ssh, "-T", "-F", "/dev/null", "-i", str(key)]
    for name, value in options.items():
        cmd += ["-o", f"{name}={value}"]
    return cmd + ["-p", str(machine.port), "-l", machine.user, machine.host, remote]


def remote_command(machine: Machine) -> str:
    """bash reading provision.sh from its input, as root (sudo -n for another
    user: no password can be typed)."""
    return "bash -s" if machine.user == "root" else "sudo -n bash -s"


def script_input(machine: Machine, link_line: str, token_hash: str) -> bytes:
    """provision.sh with its arguments set in front of it (set --): they reach
    bash on its input, never a command line, which ps and the job log show."""
    args = [machine.package_url, str(machine.port), "1" if machine.firewall else "0", LINK_USER,
            machine.here, token_hash, base64.b64encode(link_line.encode()).decode(), machine.kind]
    return f"set -- {shlex.join(args)}\n".encode() + PROVISION_SCRIPT.read_bytes()


# what ssh says, and what the admin is told to do about it
ACCESS_ERRORS = (
    ("Permission denied", "The server does not accept OmniaCLI's key yet: run the command of step 1 there, as root"),
    ("password is required", "{user} needs sudo without a password there, or use root"),
    ("Connection refused", "No SSH server on {host}:{port}"),
    ("timed out", "{host}:{port} does not answer: check the address, the port and the provider's firewall"),
    ("Could not resolve", "{host} is not a known name"),
    ("No route to host", "{host}:{port} does not answer: check the address, the port and the provider's firewall"),
)


async def check_access(machine: Machine, ssh_folder: Path, ssh: str = "ssh") -> None:
    """Try the provisioning key on the machine (and sudo -n for another user
    than root): MachineError, said for the admin, when it does not work."""
    key = sshlinks.key_file(ssh_folder, sshlinks.PROVISION_KEY_NAME)
    if not key.is_file():
        raise MachineError("No provisioning key yet: reload the page")
    with tempfile.TemporaryDirectory(prefix="omniacli-machine-") as folder:
        known_hosts = Path(folder) / "known_hosts"
        known_hosts.write_text(f"omniacli-machine-{machine.name} {machine.host_key}\n")
        remote = "true" if machine.user == "root" else "sudo -n true"
        proc = await asyncio.create_subprocess_exec(
            *ssh_command(machine, key, known_hosts, remote, ssh),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        try:
            _, err = await asyncio.wait_for(proc.communicate(), 40)
        except asyncio.TimeoutError:
            proc.kill()
            err = b"timed out"
    if proc.returncode == 0:
        return
    said = err.decode(errors="replace")
    for mark, message in ACCESS_ERRORS:
        if mark in said:
            raise MachineError(message.format(user=machine.user, host=machine.host, port=machine.port))
    lines = [line for line in said.splitlines() if line.strip()]
    reason = lines[-1] if lines else f"ssh ended with code {proc.returncode} and said nothing"
    raise MachineError(f"SSH to {machine.host}:{machine.port} failed: {reason}")


def link_of(machine: Machine, token: str) -> LinkTarget:
    """The link saved here once the machine is ready."""
    return LinkTarget(name=machine.name, url=REMOTE_URL, token=token,
                      ssh=SshRoute(host=machine.host, user=LINK_USER, host_key=machine.host_key, port=machine.port))


async def provision(job: Job, machine: Machine, ssh_folder: Path, link_public_key: str, token_hash: str,
                    ssh: str = "ssh") -> None:
    """Run provision.sh on the machine; JobFailed when it fails (its exit code)."""
    key = sshlinks.key_file(ssh_folder, sshlinks.PROVISION_KEY_NAME)
    if not key.is_file():
        raise JobFailed("no provisioning key: make it on the Machines page and add it to the machine first")
    link = link_of(machine, "")
    line = sshlinks.authorized_line(link_public_key, link)
    with tempfile.TemporaryDirectory(prefix="omniacli-machine-") as folder:
        known_hosts = Path(folder) / "known_hosts"
        known_hosts.write_text(f"omniacli-machine-{machine.name} {machine.host_key}\n")
        stdin = Path(folder) / "provision.sh"
        stdin.write_bytes(script_input(machine, line, token_hash))
        cmd = ssh_command(machine, key, known_hosts, remote_command(machine), ssh)
        job.log(f"--- prepare {machine.user}@{machine.host}:{machine.port} (host key "
                f"{sshlinks.fingerprint(machine.host_key)}), OmniaCLI from {machine.package_url}, "
                + ("a clean server" if machine.kind == "clean" else "a server that already has Odoo"))
        with open(stdin, "rb") as script:
            try:
                await run(job, cmd, timeout=PROVISION_TIMEOUT, stdin=script)
            except JobFailed:
                # what provision.sh said before it stopped is the reason, not ssh's exit code
                reason = failure_said(job.lines)
                if reason is None:
                    raise
                raise JobFailed(reason) from None


FAILED_MARK = "PROVISION FAILED: "


def failure_said(lines: list[str]) -> str | None:
    """The reason provision.sh gave when it stopped (its last PROVISION
    FAILED line, without the mark and the job's time), None when it gave none."""
    for line in reversed(lines):
        if FAILED_MARK in line:
            return line.split(FAILED_MARK, 1)[1].strip() or None
    return None


def left_out(item: dict) -> str:
    """Why an Odoo Discover found is not an instance to add (empty: it is):
    one started by hand (an update someone runs, a shell) has no service of
    its own, one whose config could not be read is not known enough."""
    instance = item.get("instance") or {}
    restart = instance.get("restart") or {}
    if restart.get("type") != "systemd" or not restart.get("service"):
        return "started by hand, no service of its own"
    if not instance.get("odoo_config"):
        return "its Odoo config could not be read"
    return ""


def instances_to_add(found: list[dict], taken: set[str]) -> list[dict]:
    """What Discover found on an existing machine, as the items its OmniaCLI
    saves (POST /api/servers/discover): each Odoo it can add, as the
    production of a server named after its database (-2, -3... beside a
    name taken). Those it cannot add (in a container, already there) are
    left out: the job says so. Their code stays what put it there (a
    hosting panel deploys its own): no repository for OmniaCLI to pull,
    odoo-bin named as Discover found it."""
    items, names = [], set(taken)
    for item in found:
        if item.get("skip") or item.get("known") or item.get("error") or not isinstance(item.get("instance"), dict):
            continue
        if left_out(item):
            continue
        base = item.get("name") or "odoo"
        name, number = base, 2
        while name in names:
            name, number = f"{base}-{number}", number + 1
        names.add(name)
        instance = {**item["instance"], "repositories": []}
        odoo_bin = next((str(value).split(" (", 1)[0] for label, value in item.get("facts") or []
                         if label == "odoo-bin"), "")
        if odoo_bin and not instance.get("odoo_bin"):
            instance["odoo_bin"] = odoo_bin
        items.append({"name": name, "role": "production", "type": item.get("type") or "odoo", "instance": instance})
    return items
