# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The automatic security updates of the machine's system (README, Automatic
security updates of the system): unattended-upgrades, on or off, and when
it may restart the machine after an update that needs it.

What is read: APT's configuration as APT reads it (every file of
/etc/apt/apt.conf.d in the order of their names, a later one wins), the
last run in unattended-upgrades' own log, whether a restart is waiting
(/var/run/reboot-required). What is written, by root (the root helper's
actions os-updates-set, workspacehelper.ACTIONS): 20auto-upgrades, the
file the package and provision.sh write, with a first line that says
OmniaCLI wrote it (provision.sh run again then leaves it as it is), and
REBOOT_FILE, after the package's 50unattended-upgrades, for the restart.
Each through a temporary file renamed over it, mode 644, never following
a link. The second part is root's; the first makes the texts."""

from __future__ import annotations

import os
import re
from pathlib import Path

APT_CONF = Path("/etc/apt/apt.conf.d")
SWITCH_FILE = "20auto-upgrades"
REBOOT_FILE = "52omniacli-unattended-upgrades"
LOG = Path("/var/log/unattended-upgrades/unattended-upgrades.log")
PROGRAM = Path("/usr/bin/unattended-upgrade")
REBOOT_REQUIRED = Path("/var/run/reboot-required")
MARK = "// written by OmniaCLI (Options, Automatic security updates of the system): changed there"
# a restart: never, or at a time of the day (unattended-upgrades' Automatic-Reboot-Time, the machine's clock)
NEVER = "never"
TIME = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
# the keys read, lower case (APT's are not case sensitive)
UPGRADE, LISTS = "apt::periodic::unattended-upgrade", "apt::periodic::update-package-lists"
REBOOT, REBOOT_TIME = "unattended-upgrade::automatic-reboot", "unattended-upgrade::automatic-reboot-time"
# what of the log is read: its end
LOG_TAIL = 512 * 1024
LINES_KEPT = 12
# a name of a file APT reads in apt.conf.d (apt.conf(5): letters, digits, _ - . and no other extension than .conf)
_APT_NAME = re.compile(r"[A-Za-z0-9_.-]+")
_SETTING = re.compile(r"""^\s*([A-Za-z0-9_:.+-]+)\s+"([^"]*)"\s*;""", re.M)
_COMMENT = re.compile(r"/\*.*?\*/|//[^\n]*|^\s*#[^\n]*", re.S | re.M)
_LOG_LINE = re.compile(r"(?P<at>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)(?:,\d+)? (?P<level>[A-Z]+) (?P<text>.*)")


class UpdatesRefused(ValueError):
    """A value refused: why, said to the person."""


def check_policy(value) -> str:
    policy = str(value or "").strip()
    if policy == NEVER or TIME.fullmatch(policy):
        return policy
    raise UpdatesRefused(f"{policy!r}: the restart is never, or at a time of the day HH:MM")


def switch_text(on: bool) -> str:
    """20auto-upgrades: the package lists every day; the updates every day, or never."""
    return (f"{MARK}\n"
            'APT::Periodic::Update-Package-Lists "1";\n'
            f'APT::Periodic::Unattended-Upgrade "{1 if on else 0}";\n')


def reboot_text(policy: str) -> str:
    """REBOOT_FILE: whether unattended-upgrades restarts the machine when an update asks it, and when."""
    if policy == NEVER:
        return f'{MARK}\nUnattended-Upgrade::Automatic-Reboot "false";\n'
    return (f'{MARK}\nUnattended-Upgrade::Automatic-Reboot "true";\n'
            f'Unattended-Upgrade::Automatic-Reboot-Time "{policy}";\n')


def settings(texts: list[tuple[str, str]]) -> dict[str, tuple[str, str]]:
    """{key: (value, file)} of the keys this module reads, from (file, text) in APT's order: a later one wins."""
    found: dict[str, tuple[str, str]] = {}
    for name, text in texts:
        for key, value in _SETTING.findall(_COMMENT.sub("", text)):
            key = key.lower()
            if key in (UPGRADE, LISTS, REBOOT, REBOOT_TIME):
                found[key] = (value, name)
    return found


def _on(value: str | None) -> bool:
    return str(value or "").strip().lower() not in ("", "0", "false", "no", "off")


def policy_of(found: dict) -> str:
    """never, a time HH:MM, or now (a restart right after the update: set by hand, not offered here)."""
    if not _on((found.get(REBOOT) or ("false", ""))[0]):
        return NEVER
    at = (found.get(REBOOT_TIME) or ("now", ""))[0].strip()
    return at if TIME.fullmatch(at) else "now"


def last_run(text: str) -> dict | None:
    """The last run in unattended-upgrades' log: when, how it went (upgraded, nothing, error, running), the
    packages it upgraded, its last lines; None when it never ran."""
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if "Starting unattended upgrades script" in line]
    if not starts:
        return None
    block = [m for m in (_LOG_LINE.match(line) for line in lines[starts[-1]:]) if m]
    if not block:
        return None
    packages: list[str] = []
    outcome = "running"
    errors = [m["text"] for m in block if m["level"] in ("ERROR", "CRITICAL")]
    for m in block:
        if m["text"].startswith("Packages that will be upgraded:"):
            packages = m["text"].split(":", 1)[1].split()
        if m["text"].startswith(("No packages found that can be upgraded", "All upgrades installed",
                                 "No packages found that can be upgraded unattended")):
            outcome = "upgraded" if packages else "nothing"
    if packages and outcome == "running" and any("All upgrades installed" in m["text"] for m in block):
        outcome = "upgraded"
    if errors:
        outcome = "error"
    return {"at": block[0]["at"].replace(" ", "T"), "ended": block[-1]["at"].replace(" ", "T"), "outcome": outcome,
            "packages": packages, "error": errors[-1][:300] if errors else "",
            "lines": [f"{m['at']} {m['level']} {m['text']}"[:300] for m in block[-LINES_KEPT:]]}


# --- root's: read the machine, write the two files


def _apt_files(folder: Path) -> list[tuple[str, str]]:
    found = []
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    for name in names:
        path = folder / name
        if not _APT_NAME.fullmatch(name) or ("." in name and not name.endswith(".conf")) or not path.is_file():
            continue
        try:
            found.append((name, path.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            continue
    return found


def _tail(path: Path) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - LOG_TAIL))
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def state(folder: Path = APT_CONF, log: Path = LOG, program: Path = PROGRAM,
          reboot_flag: Path = REBOOT_REQUIRED) -> dict:
    """What the machine does now: installed, on, the package lists, the restart, which file decides, whether
    OmniaCLI wrote it, a restart waiting (and for which packages), the last run."""
    found = settings(_apt_files(folder))
    upgrade = found.get(UPGRADE)
    run = last_run(_tail(log))
    if run is None:
        run = last_run(_tail(log.with_name(log.name + ".1")))
    try:
        waiting = [p for p in reboot_flag.with_name(reboot_flag.name + ".pkgs").read_text().split() if p][:20]
    except OSError:
        waiting = []
    try:
        managed = (folder / SWITCH_FILE).read_text(encoding="utf-8", errors="replace").startswith(MARK)
    except OSError:
        managed = False
    return {"installed": program.exists(), "enabled": program.exists() and _on(upgrade[0] if upgrade else None),
            "lists": _on((found.get(LISTS) or ("0", ""))[0]), "reboot": policy_of(found),
            "source": upgrade[1] if upgrade else "", "managed": managed,
            "reboot_required": reboot_flag.exists(), "reboot_packages": waiting, "last": run}


def _write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.omniacli")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def apply(on: bool, policy: str, folder: Path = APT_CONF, program: Path = PROGRAM, **where) -> dict:
    """The switch and the restart written; the state read again. Refused when unattended-upgrades is not
    installed and it is switched on, and when a later file of APT decides otherwise (named)."""
    policy = check_policy(policy)
    if on and not program.exists():
        raise UpdatesRefused("unattended-upgrades is not installed on this machine: apt-get install unattended-upgrades")
    if folder.is_symlink() or not folder.is_dir():
        raise UpdatesRefused(f"{folder} is not a folder")
    for name in (SWITCH_FILE, REBOOT_FILE):
        if (folder / name).is_symlink():
            raise UpdatesRefused(f"{folder / name} is a link: it is not followed")
    _write(folder / SWITCH_FILE, switch_text(on))
    _write(folder / REBOOT_FILE, reboot_text(policy))
    found = state(folder, program=program, **where)
    if found["enabled"] != on:
        raise UpdatesRefused(f"{folder / found['source']} sets APT::Periodic::Unattended-Upgrade after "
                             f"{SWITCH_FILE}: it decides, change it there")
    if found["reboot"] != policy:
        raise UpdatesRefused(f"another file of {folder} after {REBOOT_FILE} sets the restart: it decides, change it there")
    return found
