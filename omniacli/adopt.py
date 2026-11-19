# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Odoo already on a machine, made OmniaCLI's to operate (README,
Machines, "A server that already has Odoo"): what a Cloudpepper server, or
one installed by hand, needs before Discover and Test & activate pass.

Run as root by provision.sh, once OmniaCLI's package is installed:

    /opt/omniacli/venv/bin/python -I -m omniacli.adopt <OmniaCLI's user>

For every Odoo that Discover finds (a process or a systemd unit), as
Discover finds it, with nothing of Odoo's changed:

- read access for OmniaCLI's user to its Odoo config, its log folder (new
  logs too: a default ACL) and its addons, by ACLs (setfacl): the folders'
  modes, owners and groups stay as they are;
- click-odoo, which backups and updates run: Odoo run by a python outside a
  venv (Odoo's Debian package) gets a venv of its own that sees that
  python's packages (odoo_ops.adopted_venv), the system's python
  untouched; a venv without it gets it installed, as the venv's owner;
- the sudoers lines Test & activate asks for, in
  /etc/sudoers.d/omniacli-instances (checked by visudo before it is put
  there): its venv as the user Odoo runs as, psql, and restart, start, stop
  of its unit.

Odoo run as root, in a container, or without a unit is said and left out.
Run again it does the same: every step can be repeated. Its last line is
OMNIACLI-ADOPTED with the number of instances made ready."""

from __future__ import annotations

import asyncio
import os
import pwd
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from .odoo import discover
from .odoo.ops import adopted_venv, addons_paths_of

SUDOERS = Path("/etc/sudoers.d/omniacli-instances")


def say(text: str) -> None:
    print(text, flush=True)


def call(cmd: list[str], user: str | None = None) -> subprocess.CompletedProcess:
    if user is not None:
        cmd = ["runuser", "-u", user, "--", *cmd]
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def checked(cmd: list[str], user: str | None = None) -> None:
    done = call(cmd, user)
    if done.returncode:
        said = (done.stderr or done.stdout).strip().splitlines()
        raise RuntimeError(f"{' '.join(cmd)}: {said[-1] if said else f'exit {done.returncode}'}")


def let_read(service_user: str, path: Path, folder: bool = False) -> list[str]:
    """ACLs so that service_user reads path (a file, or a folder with what
    is and will be in it) and enters every folder above it; only where the
    others may not already. What was given, for the log."""
    given = []
    for parent in reversed(path.parents):
        if parent == Path("/"):
            continue
        if not os.stat(parent).st_mode & stat.S_IXOTH:
            checked(["setfacl", "-m", f"u:{service_user}:x", str(parent)])
            given.append(f"x {parent}")
    mode = os.stat(path).st_mode
    if folder:
        if mode & (stat.S_IROTH | stat.S_IXOTH) != stat.S_IROTH | stat.S_IXOTH:
            checked(["setfacl", "-R", "-m", f"u:{service_user}:rX", str(path)])
            checked(["setfacl", "-R", "-d", "-m", f"u:{service_user}:rX", str(path)])
            given.append(f"rX {path} (and what comes in it)")
    elif not mode & stat.S_IROTH:
        checked(["setfacl", "-m", f"u:{service_user}:r", str(path)])
        given.append(f"r {path}")
    return given


def system_python(venv_bin: Path) -> Path | None:
    """The python of venv_bin when it is not a venv's (Odoo's Debian
    package: /usr/bin), None for a venv."""
    if (venv_bin.parent / "pyvenv.cfg").is_file():
        return None
    for name in ("python3", "python"):
        if (venv_bin / name).is_file():
            return venv_bin / name
    return None


def ready_venv(venv_bin: Path) -> Path:
    """The venv's bin folder where click-odoo imports with odoo: made for a
    system python, completed for a venv."""
    python = system_python(venv_bin)
    if python is not None:
        venv = adopted_venv(python)
        if not (venv / "bin" / "python").is_file():
            venv.parent.mkdir(parents=True, exist_ok=True)
            checked([str(python), "-m", "venv", "--system-site-packages", str(venv)])
            say(f"  venv {venv}, seeing the packages of {python}")
        venv_bin, owner = venv / "bin", None
    else:
        owner = pwd.getpwuid(os.stat(venv_bin).st_uid).pw_name
    if call([str(venv_bin / "python"), "-c", "import click_odoo"], owner).returncode:
        checked([str(venv_bin / "python"), "-m", "pip", "install", "-q", "--disable-pip-version-check",
                 "click-odoo-contrib"], owner)
        say(f"  click-odoo-contrib installed in {venv_bin.parent}" + (f" as {owner}" if owner else ""))
    checked([str(venv_bin / "python"), "-c", "import click_odoo, odoo"], owner)
    return venv_bin


def sudoers_lines(service_user: str, run_as: str, venv_bin: Path, unit: str) -> list[str]:
    lines = [f"{service_user} ALL=({run_as}) NOPASSWD:SETENV: {venv_bin}/"]
    psql = shutil.which("psql")
    if psql:
        lines.append(f"{service_user} ALL=({run_as}) NOPASSWD:SETENV: {psql}")
    systemctl = shutil.which("systemctl") or "/usr/bin/systemctl"
    lines += [f"{service_user} ALL=(root) NOPASSWD: {systemctl} --no-ask-password {action} {unit}"
              for action in ("restart", "start", "stop")]
    return lines


def write_sudoers(lines: list[str]) -> None:
    """The lines added to SUDOERS, those there kept; visudo checks the file
    before it replaces the old one."""
    old = SUDOERS.read_text().splitlines() if SUDOERS.is_file() else [
        "# OmniaCLI: the Odoo instances it operates (written by omniacli.adopt)"]
    new = old + [line for line in lines if line not in old]
    if new == old:
        return
    with tempfile.NamedTemporaryFile("w", dir=SUDOERS.parent, prefix=".omniacli-", delete=False) as fh:
        fh.write("\n".join(new) + "\n")
    os.chmod(fh.name, 0o440)
    if call(["visudo", "-cf", fh.name]).returncode:
        os.unlink(fh.name)
        raise RuntimeError("visudo refused the sudoers lines: nothing written")
    os.replace(fh.name, SUDOERS)


async def adopt(service_user: str) -> int:
    ready = 0
    for found in await discover.discover([]):
        inst = found.instance
        say(f"--- the Odoo {found.label}")
        if found.skip:
            say(f"  left out: {found.skip}")
            continue
        unit = (inst.get("restart") or {}).get("service") or ""
        run_as = inst.get("run_as")
        if (inst.get("restart") or {}).get("type") != "systemd" or not unit:
            say("  left out: no systemd unit starts it")
            continue
        if not run_as:
            say("  left out: Odoo runs as root, OmniaCLI does not run it as root by itself")
            continue
        if not inst.get("odoo_config") or not inst.get("venv_bin"):
            say("  left out: " + "; ".join(found.notes or ["its config or its python is not known"]))
            continue
        try:
            config = Path(inst["odoo_config"])
            given = let_read(service_user, config)
            if inst.get("log_file"):
                given += let_read(service_user, Path(inst["log_file"]).parent, folder=True)
            for folder in addons_paths_of(config):
                if folder.is_dir():
                    given += let_read(service_user, folder, folder=True)
            for line in given:
                say(f"  read: {line}")
            venv_bin = ready_venv(Path(inst["venv_bin"]))
            write_sudoers(sudoers_lines(service_user, run_as, venv_bin, unit))
        except (OSError, RuntimeError) as exc:
            say(f"  NOT READY: {exc}")
            continue
        say(f"  ready: {unit} as {run_as}, click-odoo in {venv_bin.parent}")
        ready += 1
    return ready


def main() -> None:
    if os.geteuid() != 0 or len(sys.argv) != 2:
        sys.exit("usage, as root: python -m omniacli.adopt <OmniaCLI's user>")
    service_user = sys.argv[1]
    pwd.getpwnam(service_user)
    ready = asyncio.run(adopt(service_user))
    say(f"OMNIACLI-ADOPTED {ready}")


if __name__ == "__main__":
    main()
