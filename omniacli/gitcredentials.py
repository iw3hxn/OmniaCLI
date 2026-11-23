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

The rewrite to https is for an ssh remote OmniaCLI has no key for (an
adopted machine's working copies): a repository of a workspace added over
ssh is read with its keys, its deploy key and the machine's git key, and a
token of its host never takes it to https. A GitHub fine-grained token
reaches the repositories it was given only: on ber (23/11/2026) a token
limited to one repository took every ssh repository of github.com to https,
where it could not read them (403).

The keys of the repositories of the workspaces reached over ssh: one deploy
key per repository (DEPLOY_KEYS/<its name>), since GitHub refuses a key that
is a deploy key of another repository already («Key is already in use»),
and the machine's git key (GIT_KEY_NAME), for an account's SSH keys (an
account that reads several repositories). git's ssh offers the deploy key
first, then the machine's: a server accepts the first key it knows, so the
deploy key of the repository comes before a key that may be another
repository's.
"""

from __future__ import annotations

import hashlib
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


# the machine's key of OmniaCLI for the repositories of the workspaces reached
# over ssh (git@host:org/repo.git): for an account's SSH keys (an account that
# reads several repositories); made on demand (the Addons tab), in the store
# folder. A repository's own deploy key is in DEPLOY_KEYS (deploy_key)
GIT_KEY_NAME = "id_ed25519_git"
GIT_KNOWN_HOSTS = "known_hosts_git"
DEPLOY_KEYS = "deploy-keys"
# git@host:path, ssh://git@host[:port]/path
_SCP = re.compile(r"git@([A-Za-z0-9.-]+):/?(.+)")
_SSH = re.compile(r"ssh://git@([A-Za-z0-9.-]+)(?::(\d{1,5}))?/(.+)")


def git_key(credentials_file: Path) -> Path:
    return store_dir(credentials_file) / GIT_KEY_NAME


def ssh_address(url: str) -> tuple[str, str, str] | None:
    """(host, port or "", path without .git) of an ssh address of git, None
    for any other (https, a folder)."""
    url = (url or "").strip()
    found = _SSH.fullmatch(url)
    if found:
        host, port, path = found[1], found[2] or "", found[3]
    else:
        found = _SCP.fullmatch(url)
        if not found:
            return None
        host, port, path = found[1], "", found[2]
    path = path.strip("/").removesuffix(".git").strip("/")
    return (host.lower(), port, path) if path and ".." not in path else None


def deploy_key_name(url: str) -> str | None:
    """The file name of the deploy key of the repository at an ssh address
    (the same for git@host:org/repo.git and ssh://git@host/org/repo); None
    for an address that is not over ssh."""
    found = ssh_address(url)
    if found is None:
        return None
    host, port, path = found
    # no dot: the public half is the name with .pub as its suffix (sshlinks.public_key)
    readable = re.sub(r"[^a-z0-9_-]+", "_", f"{host}_{path}".lower()).strip("_")[:60]
    digest = hashlib.sha256(f"{host}:{port}/{path.lower()}".encode()).hexdigest()[:10]
    return f"id_ed25519_{readable}-{digest}"


def deploy_key(credentials_file: Path, url: str) -> Path | None:
    """Where the deploy key of the repository at an ssh address is (made or
    not); None for an address that is not over ssh."""
    name = deploy_key_name(url)
    return store_dir(credentials_file) / DEPLOY_KEYS / name if name else None


def deploy_key_page(url: str) -> str | None:
    """The page where a deploy key of the repository at an ssh address is
    added: GitHub's, GitLab's, Bitbucket's; None for another host."""
    found = ssh_address(url)
    if found is None:
        return None
    host, _, path = found
    if host == "github.com":
        return f"https://github.com/{path}/settings/keys/new"
    if host == "bitbucket.org":
        return f"https://bitbucket.org/{path}/admin/access-keys/"
    if "gitlab" in host:
        return f"https://{host}/{path}/-/settings/repository#js-deploy-keys-settings"
    return None


def ssh_options(*keys: Path, known: Path | None = None) -> tuple[str, ...]:
    """The -c option that has git's ssh use these keys only, in this order
    (no agent, no prompt), a host's key recorded the first time it is met."""
    known = known or keys[0].parent / GIT_KNOWN_HOSTS
    command = ("ssh " + " ".join(f"-i {shlex.quote(str(key))}" for key in keys)
               + " -o IdentitiesOnly=yes -o IdentityAgent=none -o BatchMode=yes"
               f" -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile={shlex.quote(str(known))}")
    return ("-c", f"core.sshCommand={command}")


def keys_for(settings, server: str, url: str | None = None) -> list[Path]:
    """The keys of OmniaCLI a workspace's server reads a repository with over
    ssh: the repository's deploy key (when made), then the machine's git key
    (when made); none for a server outside the workspaces."""
    if settings.credentials_file is None or settings.workspace_of(server) is None:
        return []
    own = deploy_key(settings.credentials_file, url) if url else None
    machine = git_key(settings.credentials_file)
    return [key for key in (own, machine) if key is not None and key.is_file()]


def _with_keys(settings, server: str, options: tuple[str, ...], url: str | None = None) -> tuple[str, ...]:
    """options and, for a workspace's server, its keys' (keys_for)."""
    keys = keys_for(settings, server, url)
    if not keys:
        return options
    return (*options, *ssh_options(*keys, known=store_dir(settings.credentials_file) / GIT_KNOWN_HOSTS))


def remote_url(path: Path, remote: str = "origin") -> str | None:
    """The address of a remote of a working copy, read from its .git/config
    (no git run); None when it cannot be read."""
    folder = Path(path) / ".git"
    try:
        if folder.is_file():
            found = re.match(r"gitdir:\s*(.+)", folder.read_text(encoding="utf-8"))
            if not found:
                return None
            folder = (Path(path) / found[1].strip()).resolve()
        text = (folder / "config").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    section = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            section = line
        elif section == f'[remote "{remote}"]':
            key, sep, value = line.partition("=")
            if sep and key.strip() == "url":
                return value.strip()
    return None


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


def git_options(store: Path, hosts, rewrite: bool = True) -> tuple[str, ...]:
    """The -c options of every git command on a repository of the server:
    the store's credentials, and (rewrite) an ssh remote of a host that has
    one reached over https."""
    options = ["-c", "credential.helper=", "-c", f"credential.helper=store --file={shlex.quote(str(store))}"]
    for host in sorted(hosts) if rewrite else []:
        if ":" in host:
            continue  # an https port of its own: its ssh one is not known
        options += ["-c", f"url.https://{host}/.insteadOf=git@{host}:",
                    "-c", f"url.https://{host}/.insteadOf=ssh://git@{host}/"]
    return tuple(options)


def _options(settings, server: str, creds: dict, store: Path | None, url: str | None) -> tuple[str, ...]:
    """The options of a repository of a server at url (None: not known): an
    ssh remote OmniaCLI has a key for keeps ssh, whatever token its host
    has; an https one gets the token of its host from the store."""
    keys = keys_for(settings, server, url)
    options = git_options(store, creds, rewrite=not keys) if creds and store is not None else ()
    return _with_keys(settings, server, options, url)


def options_for(settings, server: str, url: str | None = None) -> tuple[str, ...]:
    """The git options of a server's credentials (a server of the form, not
    saved yet, included) for its repository at url (a clone: its deploy key
    over ssh; None: any); () without any."""
    if settings.credentials_file is None:
        return ()
    try:
        creds = merged(load(settings.credentials_file), server, settings.workspace_of(server))
    except CredentialError:
        creds = {}
    store = write_store(store_dir(settings.credentials_file), server, creds) if creds else None
    return _options(settings, server, creds, store, url)


def bind(settings) -> None:
    """Every repository of every server gets the git options of its server's
    credentials (none: the machine's git) and of its address (its keys over
    ssh, read from its .git/config); the store files of servers that have
    none any more are removed."""
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
        store = None
        if creds:
            store = write_store(folder, name, creds)
            kept.add(store.name)
        for inst in (server.production, server.test):
            for repo in (inst.repositories if inst is not None else []):
                repo.git_options = _options(settings, name, creds, store, remote_url(repo.path, repo.remote))
    if folder.is_dir():
        for stale in folder.glob("server-*.store"):
            if stale.name not in kept:
                stale.unlink(missing_ok=True)
