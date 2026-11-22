# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Odoo Enterprise for the instances made from a template (docs/ux/UX-012).

Where the code comes from, once per machine (Settings › Odoo Enterprise, the
machine's admins): the git address of the enterprise repository and how it is
reached, an SSH key of its own that OmniaCLI makes (its public half goes to a
GitHub account that can read the repository) or, for an https address, a user
and a token. Kept in enterprise.yaml next to the servers file and the folder
enterprise/ beside it (key, known hosts, the token's store, 0600 in 0700):
never in a repository, never on a command line, never in a log. The key is
not OmniaCLI's git key of the workspaces' repositories: a workspace's admin who
adds a repository reached over ssh would otherwise read with it everything
that account reads.

The code on the machine: OmniaCLI's user fetches each version branch it needs
(16.0, 17.0, 18.0, 19.0…), shallow, into enterprise/checkouts/<version>; the
root helper's enterprise-sync copies that tree (without .git), read as
OmniaCLI's user and written by root, into ROOT/<version>, root's and read
only for everyone else, outside every workspace. That folder is the one every
Enterprise instance of the version mounts read only (MOUNT, first in its
addons_path): the customers' workspaces cannot write it.

The switch of an instance is kept in its instance.json (META, the version and
its Odoo service), which the root helper writes; conf puts MOUNT first in the
addons path and the project's compose.override.yaml mounts ROOT/<version>
there.
"""

from __future__ import annotations

import configparser
import json
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import yaml

# root's: one folder per version branch, and what each holds (written by enterprise-sync)
ROOT = Path("/var/lib/omniacli-enterprise")
STATE = ".state"
# where the instance's Odoo reads it, first in its addons_path
MOUNT = "/mnt/enterprise"
# instance.json's key: {"series": "19.0", "service": "odoo"}
META = "enterprise"
# create's value that asks a new instance (a staging) Enterprise from the start
CREATE_KEY = "_enterprise"
FILE = "enterprise.yaml"
FOLDER = "enterprise"
CHECKOUTS = "checkouts"
KEY_NAME = "id_ed25519_enterprise"
KNOWN_HOSTS = "known_hosts"
STORE = "https.store"
DEFAULT_URL = "git@github.com:odoo/enterprise.git"
# a version branch of Odoo: 16.0, 17.0, 18.0, 19.0, 20.0
SERIES = re.compile(r"\d{2}\.0")
# the module every Enterprise has: an addons folder holding it is Enterprise
SIGN = "web_enterprise"
INSTALLED_STATES = ("installed", "to upgrade", "to remove")

_SCP = re.compile(r"(?P<user>[a-z_][a-z0-9_.-]{0,31})@(?P<host>[A-Za-z0-9](?:[A-Za-z0-9.-]{0,252}))"
                  r":(?P<path>[A-Za-z0-9._~][A-Za-z0-9._~/-]{0,300})")
_SSH = re.compile(r"ssh://(?P<user>[a-z_][a-z0-9_.-]{0,31})@(?P<host>[A-Za-z0-9](?:[A-Za-z0-9.-]{0,252}))"
                  r"(?::(?P<port>\d{1,5}))?/(?P<path>[A-Za-z0-9._~][A-Za-z0-9._~/-]{0,300})")
_HTTPS = re.compile(r"https://(?P<host>[A-Za-z0-9](?:[A-Za-z0-9.-]{0,252}))(?::(?P<port>\d{1,5}))?"
                    r"/(?P<path>[A-Za-z0-9._~][A-Za-z0-9._~/-]{0,300})")
USERNAME = re.compile(r"[^\s:@/]{1,200}")
TOKEN = re.compile(r"\S{1,1000}")
GIT_TIMEOUT = 60
FETCH_TIMEOUT = 3600


class EnterpriseError(ValueError):
    pass


def kind_of(url: str) -> str:
    """"ssh" or "https" for an address OmniaCLI takes; EnterpriseError for any other."""
    if ".." in url or url.startswith("-"):
        raise EnterpriseError(f"{url!r} is not the address of a repository")
    if _SCP.fullmatch(url) or _SSH.fullmatch(url):
        return "ssh"
    if _HTTPS.fullmatch(url):
        return "https"
    raise EnterpriseError(f"{url!r} is not the address of a repository: git@github.com:odoo/enterprise.git "
                          "(the key of this page) or https://github.com/odoo/enterprise.git (a user and a token, "
                          "never in the address)")


def host_of(url: str) -> str:
    found = _SCP.fullmatch(url) or _SSH.fullmatch(url) or _HTTPS.fullmatch(url)
    return found["host"].lower() if found else ""


@dataclass(frozen=True)
class Source:
    url: str = DEFAULT_URL
    username: str = ""
    token: str = ""

    @property
    def kind(self) -> str:
        return kind_of(self.url)

    def public(self) -> dict:
        """What the page shows: never the token."""
        return {"url": self.url, "kind": self.kind, "username": self.username, "token_set": bool(self.token)}


def check(url: str, username: str = "", token: str = "") -> Source:
    url = (url or "").strip()
    kind = kind_of(url)
    if kind == "https":
        if not USERNAME.fullmatch(username or ""):
            raise EnterpriseError("the user is 1-200 characters, no space, : @ or /")
        if not TOKEN.fullmatch(token or ""):
            raise EnterpriseError("the token is 1-1000 characters, no space")
        return Source(url, username, token)
    return Source(url)


def folder_of(servers_file: Path) -> Path:
    return Path(servers_file).parent / FOLDER


def file_of(servers_file: Path) -> Path:
    return Path(servers_file).parent / FILE


def checkouts_of(jobs_dir: Path) -> Path:
    """OmniaCLI's own working copies, one per version: what root copies from
    (read from the main configuration's jobs_dir, as the templates' folder)."""
    return Path(jobs_dir).parent / FOLDER / CHECKOUTS


def load(path: Path) -> Source | None:
    """The source saved, None when none is."""
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return check(str(data.get("url") or ""), str(data.get("username") or ""), str(data.get("token") or ""))
    except (OSError, yaml.YAMLError, AttributeError) as exc:
        raise EnterpriseError(f"cannot read {path}: {exc}") from None
    except EnterpriseError as exc:
        raise EnterpriseError(f"{path}: {exc}") from None


def _private_write(path: Path, text: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def save(path: Path, source: Source) -> None:
    data = {"url": source.url, **({"username": source.username, "token": source.token}
                                  if source.kind == "https" else {})}
    _private_write(path, yaml.safe_dump(data, sort_keys=False))


def public_key(folder: Path) -> str | None:
    try:
        return (folder / (KEY_NAME + ".pub")).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def git_options(source: Source, folder: Path) -> tuple[str, ...]:
    """The -c options of every git command on the enterprise repository: the
    machine's credential helpers off; ssh with this page's key only (no agent,
    no prompt, the host's key kept the first time), or https with the token in
    a store file of its own (0600), never on the command line; no protocol
    but ssh and https."""
    options = ["-c", "credential.helper=", "-c", "protocol.allow=never",
               "-c", "protocol.ssh.allow=always", "-c", "protocol.https.allow=always", "-c", "core.askPass="]
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    if source.kind == "ssh":
        key, known = folder / KEY_NAME, folder / KNOWN_HOSTS
        options += ["-c", f"core.sshCommand=ssh -i {shlex.quote(str(key))} -o IdentitiesOnly=yes "
                          f"-o IdentityAgent=none -o BatchMode=yes -o StrictHostKeyChecking=accept-new "
                          f"-o UserKnownHostsFile={shlex.quote(str(known))}"]
    else:
        store = folder / STORE
        found = _HTTPS.fullmatch(source.url)
        host = found["host"] + (f":{found['port']}" if found["port"] else "")
        _private_write(store, f"https://{quote(source.username, safe='')}:{quote(source.token, safe='')}@{host}\n")
        options += ["-c", f"credential.helper=store --file={shlex.quote(str(store))}"]
    return tuple(options)


def series_of(branches) -> list[str]:
    """The version branches among branches, oldest first."""
    return sorted({b for b in branches if SERIES.fullmatch(b)}, key=lambda s: int(s.split(".")[0]))


def parse_heads(out: str) -> dict[str, str]:
    """{branch: commit} of git ls-remote --heads."""
    found = {}
    for line in out.splitlines():
        sha, _, ref = line.partition("\t")
        if ref.startswith("refs/heads/") and re.fullmatch(r"[0-9a-f]{40,64}", sha):
            found[ref[len("refs/heads/"):]] = sha
    return found


def last_line(out: str) -> str:
    lines = [line for line in (out or "").splitlines() if line.strip()]
    return lines[-1].strip() if lines else ""


def series_of_template(name: str) -> str | None:
    """The version branch of a template's Odoo (odoo-19-machine: 19.0); None
    for a name that says none."""
    found = re.match(r"[a-z]+-(\d{2})(?:-|$)", name or "")
    return f"{found.group(1)}.0" if found else None


def state(series: str, root: Path | None = None) -> dict | None:
    """What root's copy of a version holds ({series, commit, fetched, modules}),
    None when there is none."""
    root = root or ROOT
    if not SERIES.fullmatch(series or ""):
        return None
    try:
        data = json.loads((root / STATE / f"{series}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not (root / series).is_dir():
        return None
    return data


def states(root: Path | None = None) -> dict[str, dict]:
    """Every version root keeps, by version."""
    root = root or ROOT
    found = {}
    folder = root / STATE
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        found_state = state(path.stem, root)
        if found_state is not None:
            found[path.stem] = found_state
    return dict(sorted(found.items(), key=lambda kv: int(kv[0].split(".")[0])))


def modules_in(folder: Path) -> list[str]:
    """The modules of an addons folder (a folder with a __manifest__.py)."""
    try:
        return sorted(p.name for p in folder.iterdir()
                      if not p.name.startswith(".") and (p / "__manifest__.py").is_file())
    except OSError:
        return []


def of_meta(meta: dict) -> dict | None:
    """{series, service} of an instance.json that has Enterprise on, else None."""
    found = (meta or {}).get(META)
    if isinstance(found, dict) and SERIES.fullmatch(str(found.get("series") or "")) \
            and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", str(found.get("service") or "")):
        return {"series": str(found["series"]), "service": str(found["service"])}
    return None


def of_project(project: Path) -> dict | None:
    """{series, service} of the instance a project is a role of (its instance.json beside it)."""
    try:
        meta = json.loads((Path(project).parent / "instance.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return of_meta(meta) if isinstance(meta, dict) else None


def addons_of_conf(odoo_config) -> list[Path]:
    """The folders of the addons_path of an Odoo configuration file (none when it cannot be read)."""
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(odoo_config, encoding="utf-8")
        raw = parser.get("options", "addons_path", fallback="")
    except (configparser.Error, OSError, UnicodeDecodeError):
        return []
    return [Path(p.strip()) for p in raw.replace("\n", ",").split(",") if p.strip()]


def in_addons(paths) -> Path | None:
    """The folder of an addons_path that holds Enterprise (its web_enterprise),
    None: none does (an instance that has Odoo already, read as it is)."""
    for folder in paths:
        try:
            if (Path(folder) / SIGN / "__manifest__.py").is_file():
                return Path(folder)
        except OSError:
            continue
    return None
