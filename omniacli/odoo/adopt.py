# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Odoo already on a machine, made OmniaCLI's to operate (README,
Machines, "A server that already has Odoo"): what a hosting panel's server, or
one installed by hand, needs before Discover and Test & activate pass.

Run as root by provision.sh, once OmniaCLI's package is installed:

    /opt/omniacli/venv/bin/python -I -m omniacli.odoo.adopt <OmniaCLI's user>

For every Odoo that Discover finds (a process or a systemd unit), as
Discover finds it, with nothing of Odoo's changed:

- read access for OmniaCLI's user to its Odoo config, its log folder (new
  logs too: a default ACL) and its addons, by ACLs (setfacl): the folders'
  modes, owners and groups stay as they are;
- click-odoo, which backups and updates run: a venv of OmniaCLI's own
  (odoo_ops.adopted_venv) that sees the packages of the python Odoo runs
  with, a system python's (Odoo's Debian package) or a venv's, with
  click-odoo-contrib there: the instance's python and venv untouched;
- Log in as: in every https site of nginx passing to its port (a hosting
  panel's conf.d), the location /.omniacli/login passed to OmniaCLI, which
  sets Odoo's cookie on the instance's name; nginx -t first, the files put
  back when it refuses;
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

from . import discover
from .ops import adopted_venv, addons_paths_of, locate_odoo_bin, odoo_root

SUDOERS = Path("/etc/sudoers.d/omniacli-instances")


def say(text: str) -> None:
    print(text, flush=True)


def call(cmd: list[str], user: str | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    if user is not None:
        cmd = ["runuser", "-u", user, "--", *cmd]
    return subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)


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


def python_of(venv_bin: Path) -> Path:
    for name in ("python3", "python"):
        if (venv_bin / name).exists():
            return venv_bin / name
    raise RuntimeError(f"no python in {venv_bin}")


def site_packages(python: Path) -> str:
    done = call([str(python), "-c", "import site; print(site.getsitepackages()[0])"])
    if done.returncode or not done.stdout.strip():
        raise RuntimeError(f"{python}: its site-packages cannot be read")
    return done.stdout.strip().splitlines()[-1]


def ready_venv(venv_bin: Path, odoo_root: Path | None = None) -> Path:
    """The bin folder of OmniaCLI's venv for the python of venv_bin
    (odoo_ops.adopted_venv), where click-odoo imports with odoo: it sees
    that python's packages, a system python's (--system-site-packages) or a
    venv's (a .pth naming its site-packages), and has click-odoo-contrib of
    its own. The instance's python and venv are not touched. odoo_root: the
    folder of a source checkout's odoo package (OmniaCLI gives it as
    PYTHONPATH, as here to check the import)."""
    python = python_of(venv_bin)
    venv = adopted_venv(venv_bin)
    if not (venv / "bin" / "python").is_file():
        venv.parent.mkdir(parents=True, exist_ok=True)
        checked([str(python), "-m", "venv", "--system-site-packages", str(venv)])
        if (venv_bin.parent / "pyvenv.cfg").is_file():
            pth = Path(site_packages(venv / "bin" / "python")) / "omniacli-instance.pth"
            pth.write_text(site_packages(python) + "\n")
            say(f"  venv {venv}, seeing the packages of {venv_bin.parent}")
        else:
            say(f"  venv {venv}, seeing the packages of {python}")
    mine = venv / "bin" / "python"
    if call([str(mine), "-c", "import click_odoo"]).returncode:
        checked([str(mine), "-m", "pip", "install", "-q", "--disable-pip-version-check", "click-odoo-contrib"])
        say(f"  click-odoo-contrib installed in {venv}")
    env = {**os.environ, "PYTHONPATH": str(odoo_root)} if odoo_root else None
    done = call([str(mine), "-c", "import click_odoo, odoo"], env=env)
    if done.returncode:
        said = (done.stderr or done.stdout).strip().splitlines()
        raise RuntimeError(f"{mine}: click_odoo or odoo does not import: {said[-1] if said else done.returncode}")
    return venv / "bin"


# Log in as: the path the instance's own name passes to OmniaCLI, which sets
# Odoo's cookie there (loginas.py), in every https site of the instance's port
LOGIN_PATH = "/.omniacli/login"
LOGIN_MARK = "# omniacli: Log in as"
OMNIACLI_ADDRESS = "127.0.0.1:8090"


def _server_blocks(text: str) -> list[tuple[int, int]]:
    """(start, end) of each server { ... } block, by its braces."""
    blocks = []
    for found in discover._SERVER_BLOCK.finditer(text):
        depth, pos = 1, found.end()
        while depth and pos < len(text):
            depth += {"{": 1, "}": -1}.get(text[pos], 0)
            pos += 1
        blocks.append((found.start(), pos))
    return blocks


def with_login_path(text: str, port: int) -> str:
    """text with the Log in as location in each https server block that
    passes / to 127.0.0.1:port, right after its server_name (once)."""
    for start, end in reversed(_server_blocks(text)):
        block = text[start:end]
        if LOGIN_PATH in block or not discover._LISTEN_SSL.search(block) \
                or port not in {int(p) for p in discover._PROXY_PORT.findall(block)}:
            continue
        name = discover._SERVER_NAME.search(block)
        if not name:
            continue
        at = start + name.end()
        indent = "  "
        location = (f"\n\n{indent}{LOGIN_MARK}: the cookie of Odoo set on this name\n"
                    f"{indent}location = {LOGIN_PATH} {{\n"
                    f"{indent}  proxy_pass http://{OMNIACLI_ADDRESS};\n"
                    f"{indent}  proxy_set_header Host $host;\n"
                    f"{indent}  proxy_set_header X-Forwarded-Host $host;\n"
                    f"{indent}  proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
                    f"{indent}  proxy_set_header X-Forwarded-Proto $scheme;\n"
                    f"{indent}  proxy_set_header X-Real-IP $remote_addr;\n"
                    f"{indent}}}")
        text = text[:at] + location + text[at:]
    return text


def add_login_paths(port: int, folders=None) -> int:
    """The Log in as location in nginx's sites of port; nginx checked and
    reloaded, every file put back as it was when the check fails. The
    number of files changed."""
    changed = {}
    for folder in folders or discover.NGINX_SITES:
        try:
            # conf.d reads *.conf only; sites-enabled every file
            files = sorted(f for f in Path(folder).iterdir()
                           if f.is_file() and (f.suffix == ".conf" or f.parent.name == "sites-enabled"))
        except OSError:
            continue
        for path in files:
            old = path.read_text(errors="replace")
            new = with_login_path(old, port)
            if new != old:
                changed[path] = old
                path.write_text(new)
    if not changed:
        return 0
    if call(["nginx", "-t"]).returncode:
        for path, old in changed.items():
            path.write_text(old)
        raise RuntimeError("nginx -t refused the Log in as location: the sites are put back as they were")
    checked(["systemctl", "reload", "nginx"])
    return len(changed)


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
            for line in dict.fromkeys(given):
                say(f"  read: {line}")
            odoo_bin = locate_odoo_bin(Path(inst["odoo_bin"]) if inst.get("odoo_bin") else None,
                                       [Path(r["path"]) for r in inst.get("repositories") or []])
            venv_bin = ready_venv(Path(inst["venv_bin"]), odoo_root(odoo_bin) if odoo_bin else None)
            given_port = next((str(v).split(" ", 1)[0] for k, v in found.facts if k == "HTTP port"), None)
            try:
                sites = add_login_paths(discover._http_port(str(config), given_port))
                if sites:
                    say(f"  Log in as: {LOGIN_PATH} passed to OmniaCLI in {sites} nginx site(s)")
            except (OSError, RuntimeError) as exc:
                # the rest works without it: said, not a failure of the instance
                say(f"  Log in as not ready: {exc}")
            write_sudoers(sudoers_lines(service_user, run_as, venv_bin, unit))
        except (OSError, RuntimeError) as exc:
            say(f"  NOT READY: {exc}")
            continue
        say(f"  ready: {unit} as {run_as}, click-odoo in {venv_bin.parent}")
        ready += 1
    return ready


def main() -> None:
    if os.geteuid() != 0 or len(sys.argv) != 2:
        sys.exit("usage, as root: python -m omniacli.odoo.adopt <OmniaCLI's user>")
    service_user = sys.argv[1]
    pwd.getpwnam(service_user)
    ready = asyncio.run(adopt(service_user))
    say(f"OMNIACLI-ADOPTED {ready}")


if __name__ == "__main__":
    main()
