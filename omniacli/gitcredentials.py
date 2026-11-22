# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The git credentials OmniaCLI uses instead of the machine's: per host
(github.com, bitbucket.org, a GitLab...), a user and a token, set in the
interface on a server (the machine's admins) or on a workspace (its
admins). A server's win over its workspace's, host by host.

They are kept in credentials.yaml next to the servers file (0600, the
service's user only), never in a repository: every git command OmniaCLI
runs on a repository of a server that has some gets

    -c credential.helper=                       the machine's helpers off
    -c credential.helper=store --file=<file>    the server's, written here
    -c url.https://<host>/.insteadOf=git@<host>:
    -c url.https://<host>/.insteadOf=ssh://git@<host>/

so a working copy found on the machine with an ssh remote is reached over
https with the token as well, its .git/config untouched (*verified* with
git 2.30.2: the clone of an ssh remote went over https with ssh disabled,
and no token reached .git/config). A host without a credential is reached
as before, as the machine's git user.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import yaml

HOST = re.compile(r"(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*(?::\d{1,5})?")
USERNAME = re.compile(r"[^\s:@/]{1,200}")
TOKEN = re.compile(r"\S{1,1000}")
SCOPE = re.compile(r"(server|workspace):([A-Za-z0-9_.-]{1,64})")


class CredentialError(ValueError):
    pass


# a credential's kinds: a user and a token for git only, or a GitHub token
# (github.py), which is also the git credential of its host and with which
# OmniaCLI lists the repositories, makes their webhooks and writes the state
# of a deploy on its commit
TOKEN_KIND, GITHUB_KIND = "token", "github"
LABEL = re.compile(r"[^\x00-\x1f<>]{1,80}")


@dataclass(frozen=True)
class Credential:
    host: str
    username: str
    token: str
    kind: str = TOKEN_KIND
    # a GitHub token's: its name, and the account and organisations GitHub said it is
    label: str = ""
    account: str = ""
    orgs: tuple = ()

    @property
    def github(self) -> bool:
        return self.kind == GITHUB_KIND

    def public(self) -> dict:
        """What the pages show: never the token."""
        found = {"host": self.host, "username": self.username, "token_set": bool(self.token)}
        if self.github:
            found.update(kind=self.kind, label=self.label, account=self.account, orgs=list(self.orgs))
        return found


def check(host: str, username: str, token: str, kind: str = TOKEN_KIND, label: str = "", account: str = "",
          orgs=()) -> Credential:
    host = host.strip().lower()
    if not HOST.fullmatch(host):
        raise CredentialError(f"{host!r} is not a host name (github.com, git.example.com:8443)")
    if not USERNAME.fullmatch(username or ""):
        raise CredentialError("the user is 1-200 characters, no space, : @ or /")
    if not TOKEN.fullmatch(token or ""):
        raise CredentialError("the token is 1-1000 characters, no space")
    if kind not in (TOKEN_KIND, GITHUB_KIND):
        raise CredentialError(f"{kind!r} is not a kind of credential")
    if kind != GITHUB_KIND:
        return Credential(host, username, token)
    label = (label or "").strip()
    if not LABEL.fullmatch(label):
        raise CredentialError("the name of a GitHub token is 1-80 characters")
    orgs = tuple(sorted({str(o) for o in orgs or () if USERNAME.fullmatch(str(o))}))
    return Credential(host, username, token, kind, label, account if USERNAME.fullmatch(account or "") else "", orgs)


def scope_of(kind: str, name: str) -> str:
    scope = f"{kind}:{name}"
    if not SCOPE.fullmatch(scope):
        raise CredentialError(f"{scope!r} is not a scope")
    return scope


def load(path: Path) -> dict[str, dict[str, Credential]]:
    """{scope: {host: Credential}}; no file is none."""
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise CredentialError(f"cannot read {path}: {exc}") from None
    found: dict[str, dict[str, Credential]] = {}
    for scope, entries in (data.get("credentials") or {}).items():
        if not SCOPE.fullmatch(str(scope)):
            raise CredentialError(f"{path}: {scope!r} is not a scope")
        found[str(scope)] = {}
        for entry in entries or []:
            entry = entry if isinstance(entry, dict) else {}
            try:
                cred = check(str(entry.get("host") or ""), str(entry.get("username") or ""),
                             str(entry.get("token") or ""), str(entry.get("kind") or TOKEN_KIND),
                             str(entry.get("label") or ""), str(entry.get("account") or ""),
                             entry.get("orgs") if isinstance(entry.get("orgs"), list) else ())
            except CredentialError as exc:
                raise CredentialError(f"{path}: {scope}: {exc}") from None
            found[str(scope)][cred.host] = cred
    return found


def save(path: Path, found: dict[str, dict[str, Credential]]) -> None:
    data = {"credentials": {scope: [_entry(c) for c in sorted(entries.values(), key=lambda c: c.host)]
                            for scope, entries in sorted(found.items()) if entries}}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _entry(cred: Credential) -> dict:
    entry = {"host": cred.host, "username": cred.username, "token": cred.token}
    if cred.github:
        entry.update(kind=cred.kind, label=cred.label, account=cred.account, orgs=list(cred.orgs))
    return entry


def github_tokens(settings, server: str) -> dict[str, Credential]:
    """The GitHub tokens of a server's repositories, by host: its
    workspace's, then its own over them (as every credential)."""
    if settings.credentials_file is None:
        return {}
    try:
        found = merged(load(settings.credentials_file), server, settings.workspace_of(server))
    except CredentialError:
        return {}
    return {host: cred for host, cred in found.items() if cred.github}


def merged(found: dict[str, dict[str, Credential]], server: str, workspace: str | None) -> dict[str, Credential]:
    """The credentials of a server: its workspace's, then its own over them."""
    result = dict(found.get(f"workspace:{workspace}", {})) if workspace else {}
    result.update(found.get(f"server:{server}", {}))
    return result


def store_dir(credentials_file: Path) -> Path:
    return credentials_file.parent / "credentials"


# the key of OmniaCLI for the repositories of the workspaces reached over ssh
# (git@host:org/repo.git): its public half is added to each repository as a
# read-only deploy key; made on demand (the Addons tab), in the store folder
GIT_KEY_NAME = "id_ed25519_git"
GIT_KNOWN_HOSTS = "known_hosts_git"


def git_key(credentials_file: Path) -> Path:
    return store_dir(credentials_file) / GIT_KEY_NAME


def ssh_options(key: Path) -> tuple[str, ...]:
    """The -c option that has git's ssh use OmniaCLI's git key only (no agent,
    no prompt), a host's key recorded the first time it is met."""
    known = key.parent / GIT_KNOWN_HOSTS
    command = (f"ssh -i {shlex.quote(str(key))} -o IdentitiesOnly=yes -o IdentityAgent=none -o BatchMode=yes"
               f" -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile={shlex.quote(str(known))}")
    return ("-c", f"core.sshCommand={command}")


def _with_key(settings, server: str, options: tuple[str, ...]) -> tuple[str, ...]:
    """options and, for a workspace's server once the git key is made, the key's."""
    key = git_key(settings.credentials_file)
    if settings.workspace_of(server) is None or not key.is_file():
        return options
    return (*options, *ssh_options(key))


def write_store(folder: Path, server: str, creds: dict[str, Credential]) -> Path:
    """git credential-store's file of a server: one https://user:token@host per host."""
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    path = folder / f"server-{server}.store"
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        for cred in sorted(creds.values(), key=lambda c: c.host):
            fh.write(f"https://{quote(cred.username, safe='')}:{quote(cred.token, safe='')}@{cred.host}\n")
    os.replace(tmp, path)
    return path


def git_options(store: Path, hosts) -> tuple[str, ...]:
    """The -c options of every git command on a repository of the server."""
    options = ["-c", "credential.helper=", "-c", f"credential.helper=store --file={shlex.quote(str(store))}"]
    for host in sorted(hosts):
        if ":" in host:
            continue  # an https port of its own: its ssh one is not known
        options += ["-c", f"url.https://{host}/.insteadOf=git@{host}:",
                    "-c", f"url.https://{host}/.insteadOf=ssh://git@{host}/"]
    return tuple(options)


def options_for(settings, server: str) -> tuple[str, ...]:
    """The git options of a server's credentials (a server of the form, not
    saved yet, included); () without any."""
    if settings.credentials_file is None:
        return ()
    try:
        creds = merged(load(settings.credentials_file), server, settings.workspace_of(server))
    except CredentialError:
        creds = {}
    if not creds:
        return _with_key(settings, server, ())
    return _with_key(settings, server,
                     git_options(write_store(store_dir(settings.credentials_file), server, creds), creds))


def bind(settings) -> None:
    """Every repository of every server gets the git options of its server's
    credentials (none: the machine's git); the store files of servers that
    have none any more are removed."""
    if settings.credentials_file is None:
        return
    try:
        found = load(settings.credentials_file)
    except CredentialError:
        found = {}
    folder = store_dir(settings.credentials_file)
    kept = set()
    for name, server in settings.servers.items():
        creds = merged(found, name, settings.workspace_of(name))
        options: tuple[str, ...] = ()
        if creds:
            store = write_store(folder, name, creds)
            kept.add(store.name)
            options = git_options(store, creds)
        options = _with_key(settings, name, options)
        for inst in (server.production, server.test):
            for repo in (inst.repositories if inst is not None else []):
                repo.git_options = options
    if folder.is_dir():
        for stale in folder.glob("server-*.store"):
            if stale.name not in kept:
                stale.unlink(missing_ok=True)
