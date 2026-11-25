# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The central panel as the home of the workspaces of its linked machines (AD-018 §18.2, slice 2).

The identities of a machine's workspaces were imported in slice 1 (identityimport.py, identity-imports/<link>.json).
This module says which of them this panel serves now:

- Homes: one switch per link (HOMES_FILE beside the links file, 600), off by default. With it on, the people and the
  tokens of that machine's workspaces log in here; every request of theirs is checked here, then asked of the
  machine through the link with their scope (placement.SCOPE_HEADER), which the machine checks again.
- served(): the people, the tokens and the workspaces served, from the imports of the links switched on. A workspace
  is served only when its name is no workspace of this panel's and no other import brought one of the same name
  (the slice-1 MUST: members, roles, tokens, SSO and grants are bound to the workspace by its name, and read under
  another workspace they would give a machine's people that workspace's scope). Such a workspace is refused, never
  renamed: the machine knows it by its name (its paths, its jobs, its access log), and a name this panel translated
  both ways is a mapping the parity of constraint 3 cannot prove. It is withheld (fail closed), the switch is refused
  while one of the link's workspaces is taken, and an import that would take a served one is refused.
- A name of a person that is a user of this panel is never served (its import renamed it; one added here later wins).

Nothing of a person's secrets leaves this panel: the scope says who, in which workspaces, with which role, custom
role and servers (the machine's own names, as its records say them), never a hash or a code."""

from __future__ import annotations

import json
import os
import secrets
import threading
from dataclasses import dataclass, field
from pathlib import Path

from . import capabilities
from .auth import ROLES, hash_token

HOMES_FILE = "workspace-homes.json"
# the sessions of the people served here, apart from this panel's own (a session of one is never read as the
# other's: two stores, two files)
SESSIONS_FILE = "remote-sessions.json"
# what a person of a served workspace keeps of its second step here (slice 2): the code of its authenticator app,
# with the same secret; a passkey is the portal's name's (slice 3), the rest is done where the identity lives now
TOTP = "totp"
PASSKEY_ONLY = "passkey_only"
NONE = "none"


@dataclass(frozen=True)
class Person:
    """A person of a served workspace: its name here, its name on the machine, its link, its hash, its second
    step, and its workspaces there with their roles and grants."""

    name: str
    there: str
    link: str
    hash: str
    second: str
    workspaces: tuple = ()
    # ((workspace, {role, capabilities, servers|None}), ...): where a custom role or some servers apply
    grants: tuple = ()
    # its password and its code are checked by its machine (POST /api/identity/v1/login): no secret here
    # ((credential id, RP ID), ...) of its passkeys as imported: which person a passkey's answer is of, before its
    # machine checks it with its own key and counter (AD-018 slice 3)
    passkeys: tuple = ()


@dataclass(frozen=True)
class Token:
    """A token of a served workspace (REST, MCP): its name here and on the machine, its link, its workspace and
    level, whether it is a CI's, its grant (a person's token)."""

    name: str
    there: str
    link: str
    workspace: str
    role: str
    ci: bool
    grant: dict | None = None


@dataclass
class Served:
    people: dict = field(default_factory=dict)
    # {hash of the token: Token}
    tokens: dict = field(default_factory=dict)
    # {workspace: link}
    workspaces: dict = field(default_factory=dict)
    # {link: [workspace refused, ...]}
    blocked: dict = field(default_factory=dict)
    # {link: {workspace: [members here]}}
    members: dict = field(default_factory=dict)


def _write_private(path: Path, data: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


class Homes:
    """The links whose machines' workspaces this panel serves: {link: {on, at, by, workspaces}}."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def all(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {str(k): v for k, v in data.items() if isinstance(v, dict) and v.get("on") is True} \
            if isinstance(data, dict) else {}

    def on(self, link: str) -> bool:
        return link in self.all()

    def put(self, link: str, entry: dict | None) -> None:
        with self._lock:
            found = self.all()
            if entry is None:
                found.pop(link, None)
            else:
                found[link] = {**entry, "on": True}
            if found:
                _write_private(self.path, found)
            else:
                self.path.unlink(missing_ok=True)

    def stamp(self):
        try:
            st = self.path.stat()
            return st.st_mtime_ns, st.st_size
        except OSError:
            return None


def _grant(ws: dict, person: str) -> dict | None:
    """The grant of person in a workspace of an import (config.Workspace.grant's rule), None: its preset role on
    every server."""
    custom = (ws.get("member_roles") or {}).get(person)
    limited = (ws.get("member_servers") or {}).get(person)
    role = (ws.get("members") or {}).get(person)
    if role is None or (custom is None and limited is None):
        return None
    caps = sorted(ws.get("roles", {}).get(custom, ())) if custom is not None else sorted(capabilities.PRESETS[role])
    return {"role": custom or role, "capabilities": caps, "servers": None if limited is None else sorted(limited)}


def taken_workspaces(imports: dict, link: str, here) -> set[str]:
    """The workspaces of link's import whose name is taken: a workspace of this panel (here), or one another
    link's import brought."""
    own = set(((imports.get(link) or {}).get("workspaces") or {}))
    others: set[str] = set()
    for other, record in imports.items():
        if other != link:
            others |= set(((record or {}).get("workspaces") or {}))
    return {w for w in own if w in set(here) or w in others}


def served(imports: dict, homes: dict, here_workspaces, here_users) -> Served:
    """What this panel serves (the module's docstring): imports {link: the record of identityimport}, homes
    Homes.all(), here_workspaces and here_users this panel's own."""
    out = Served()
    for link in sorted(homes):
        record = imports.get(link)
        if not isinstance(record, dict):
            continue
        blocked = taken_workspaces(imports, link, here_workspaces)
        if blocked:
            out.blocked[link] = sorted(blocked)
        spaces = {w: d for w, d in (record.get("workspaces") or {}).items() if w not in blocked and isinstance(d, dict)}
        names = record.get("names") or {}
        back = {here: there for there, here in names.items()}
        token_back = {here: there for there, here in (record.get("token_names") or {}).items()}
        # the members as the machine names them (a customer's own names are never taken out of its pages there)
        out.members[link] = {w: sorted(back.get(m, m) for m in (d.get("members") or {})) for w, d in spaces.items()}
        for ws in spaces:
            out.workspaces[ws] = link
        for name, data in sorted((record.get("people") or {}).items()):
            if name in here_users or not isinstance(data, dict) or not isinstance(data.get("hash"), str):
                continue
            mine = tuple((w, d["members"][name]) for w, d in sorted(spaces.items())
                         if name in (d.get("members") or {}) and d["members"][name] in ROLES)
            if not mine:
                continue
            factor = data.get("second_factor")
            keys = ((data.get("passkeys") or {}).get("keys") or []) if isinstance(data.get("passkeys"), dict) else []
            second = TOTP if isinstance(factor, dict) and factor.get("secret") else (PASSKEY_ONLY if keys else NONE)
            grants = tuple((w, g) for w, _ in mine if (g := _grant(spaces[w], name)) is not None)
            pairs = tuple((str(k.get("id")), str(k.get("rp"))) for k in keys if isinstance(k, dict) and k.get("id"))
            out.people[name] = Person(name, str(data.get("from") or back.get(name) or name), link, data["hash"],
                                      second, mine, grants, pairs)
        for ws, data in sorted(spaces.items()):
            for token, info in sorted((data.get("tokens") or {}).items()):
                if not isinstance(info, dict) or not isinstance(info.get("hash"), str) or info.get("role") not in ROLES:
                    continue
                person = (data.get("token_people") or {}).get(token)
                grant = None
                role = info["role"]
                if person is not None:
                    # a token of a person: never more than the person's role there (none: it reads), as
                    # config.token_role says it on the machine
                    cap = (data.get("members") or {}).get(person, ROLES[0])
                    role = min(role, cap if cap in ROLES else ROLES[0], key=ROLES.index)
                    level = capabilities.PRESETS[role]
                    if person not in (data.get("members") or {}):
                        grant = {"role": person, "capabilities": [], "servers": []}
                    else:
                        found = _grant(data, person)
                        if found is not None:
                            grant = {**found, "capabilities": sorted(set(found["capabilities"]) & level)}
                out.tokens[info["hash"]] = Token(token, token_back.get(token, token), link, ws, role,
                                                 bool(info.get("ci")), grant)
    return out


def token_of(found: Served, token: str) -> Token | None:
    """The served token whose value is token (its hash compared), None for none."""
    if not token:
        return None
    hashed = hash_token(token)
    match = None
    for value, entry in found.tokens.items():
        # every hash compared: the time does not say which one it was
        if secrets.compare_digest(value, hashed):
            match = entry
    return match


def passkey_person(found: Served, credential_id: str, rp: str) -> Person | None:
    """The served person a passkey (its credential id, made on rp) is of, None for none: the passkey itself is
    checked by its machine, where it lives with its counter."""
    if not credential_id or not rp:
        return None
    match = None
    for person in found.people.values():
        for key_id, key_rp in person.passkeys:
            if key_rp == rp and secrets.compare_digest(key_id.encode(), credential_id.encode()):
                match = person
    return match


def scope_of(person: Person | Token, first: str, provider: str, members: dict) -> dict:
    """The scope (placement.scope_decode) of a person or a token for its machine: first its workspace of the
    request (or its first one), then its others (a person's only); its names as the machine has them."""
    if isinstance(person, Token):
        return {"ws": person.workspace, "person": person.there, "kind": "token", "role": person.role,
                "grant": person.grant, "members": members.get(person.workspace, []), "provider": provider,
                "ci": person.ci}
    spaces = dict(person.workspaces)
    grants = dict(person.grants)
    first = first if first in spaces else person.workspaces[0][0]
    return {"ws": first, "person": person.there, "kind": "user", "role": spaces[first], "grant": grants.get(first),
            "members": members.get(first, []), "provider": provider,
            "also": [{"ws": w, "role": r, "grant": grants.get(w)} for w, r in person.workspaces if w != first]}
