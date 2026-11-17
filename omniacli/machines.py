# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Add a machine (README, Machines): a clean Ubuntu or Debian server given by
its address and an SSH user, prepared from here as Cloudpepper does, then
linked to this OmniaCLI.

1. The admin puts OmniaCLI's provisioning key (sshlinks.PROVISION_KEY_NAME,
   not the links' key) in the root's authorized_keys there, or of a user
   with sudo without a password, and confirms the server's host key.
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

import base64
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
# the package of the latest release, as the README's Quick start installs it
PACKAGE_URL = "https://github.com/OmniaGit/OmniaCLI/releases/latest/download/OmniaCLI.deb"
# the user on the new machine whose key may only forward to OmniaCLI
LINK_USER = "omniacli-link"
# OmniaCLI there, as the package installs it: the link's URL through SSH
REMOTE_URL = "http://127.0.0.1:8090"
# updates and packages on a slow machine
PROVISION_TIMEOUT = 3600
# seconds the link has to answer once saved (the tunnel starts within 25)
LINK_WAIT = 60
LINK_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")


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

    @classmethod
    def parse(cls, data: dict, here: str) -> "Machine":
        name = str(data.get("name") or "").strip()
        if not LINK_NAME.fullmatch(name):
            raise MachineError("Name: a-z, 0-9, - and _, 32 at most, as the link is named")
        host = str(data.get("host") or "").strip()
        if not SSH_HOST.fullmatch(host):
            raise MachineError(f"{host!r} is not a host name or an IP address")
        port = data.get("port") or 22
        if isinstance(port, bool) or not str(port).isdigit() or not 1 <= int(port) <= 65535:
            raise MachineError(f"{port!r} is not a port")
        user = str(data.get("user") or "root").strip()
        if not SSH_USER.fullmatch(user):
            raise MachineError(f"{user!r} is not a user name")
        host_key = str(data.get("host_key") or "").strip()
        if not SSH_HOST_KEY.fullmatch(host_key):
            raise MachineError("Fetch the server's host key and compare its fingerprint first")
        package_url = str(data.get("package_url") or PACKAGE_URL).strip()
        if not re.fullmatch(r"https://[^\s'\"]+", package_url):
            raise MachineError("Package: an https:// address of an omniacli .deb")
        if not LINK_NAME.fullmatch(here):
            here = "omniacli"
        return cls(name, host, int(port), user, host_key, package_url, bool(data.get("firewall", True)), here)


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
            machine.here, token_hash, base64.b64encode(link_line.encode()).decode()]
    return f"set -- {shlex.join(args)}\n".encode() + PROVISION_SCRIPT.read_bytes()


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
                f"{sshlinks.fingerprint(machine.host_key)}), OmniaCLI from {machine.package_url}")
        with open(stdin, "rb") as script:
            await run(job, cmd, timeout=PROVISION_TIMEOUT, stdin=script)
