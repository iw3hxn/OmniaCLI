# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The identities of a machine's workspaces carried to the central panel (AD-018 §18.3.1, steps 1-3; slice 1).

On the machine, through the central's link only (an admin link, never a person through it):
- inventory(): what each workspace W holds of its people, as counts and fields that are no secret (the dry run);
- export(): the same with the secrets, as the package the central imports: the hashes of the passwords, the TOTP
  secrets with their last step and their recovery codes' hashes, the passkeys with their counters, the SSO providers
  with their client secrets and the identities linked, the tokens' hashes, the OAuth grants with their clients.
  It is answered to the link only and written nowhere there.

On the central panel:
- plan(): the dry run said item by item, «passes the same» or «to be done again, by whom», and the names a person
  of the machine would arrive with (a name the central has already arrives as <name>.<workspace>, then -2, -3:
  Carlo, 24/11/2026, the rule of the handover);
- checked(): what a package may hold (a machine is not trusted with what it writes here): its people exactly the
  members of its workspaces, every reference to a person one of them, every value of its type and size;
- imported(): the record the central keeps of a machine's import, with the same hashes and secrets;
- Store: one file per link in FOLDER (700, each file 600); the package is read into memory (MAX_PACKAGE at most) and
  never written. Deleting a link's file is the undo: nothing was written on the machine.

Who imported is in the record and in the access log of both sides. Nothing here changes the machine: no freeze, no
home switched (slice 5)."""

from __future__ import annotations

import copy
import functools
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import handover, passkeys, totp
from .auth import ITERATIONS, ROLES, UNSET, WORKSPACE_ROLE, hash_password, password_set, verify_password
from .capabilities import CAPABILITIES
from .config import _TOKEN_HASH, MEMBER_NAME, USER_NAME, WORKSPACE_NAME
from .oauth import PREFIX

FORMAT = 1
# the internal API of the machine (AD-018 §8.7): the central's link only, never a twin in /api/v1 or MCP
API = "/api/identity/v1"
# on the central: a file per link, the identities imported from that machine
FOLDER = "identity-imports"
_LINK = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@+=-]{0,63}")

# the verdict of an item of the dry run (AD-018 §18.3.3): the page and the REST API say it in words
SAME = "same"  # passes the same
REDO = "redo"  # to be done again, by whom it says
STAYS = "stays"  # does not pass: it stays on the machine (the machine's own people)
# who does an item again
BY_PERSON = "person"
BY_ADMIN = "workspace_admin"
BY_PROVIDER = "provider_admin"
BY_SWITCH = "switch"


class IdentityImportError(ValueError):
    """A package that is not one, or an import refused: said to who asked."""


# --- on the machine -------------------------------------------------------------------------------------------

def people_of(ws, user_roles: dict, users: dict) -> list[str]:
    """The people of ws who pass (AD-018 §8.1): its members of role workspace that are users here. The machine's
    own people who are members (its staff) stay."""
    return sorted(m for m in ws.members if user_roles.get(m, "admin") == WORKSPACE_ROLE and m in users)


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def inventory(src: dict) -> dict:
    """The dry run of the machine: for each workspace, what it holds of identities, as counts and fields that are
    no secret. src: workspaces, users, user_roles, second_factors (totp.SecondFactors), passkeys
    (passkeys.PasskeyStore), sso (sso.SsoStore), grants (the OAuth grants), tokens ({name: {workspace, role, ci,
    servers, person}}), invitations, webhooks (github-hooks.json), portal (https://<address> or ""), now."""
    out = {}
    for name, ws in sorted(src["workspaces"].items()):
        people = people_of(ws, src["user_roles"], src["users"])
        provider = src["sso"].provider(name) or {}
        linked = src["sso"].users_of(name)
        rows, by_rp = [], {}
        for person in people:
            keys = src["passkeys"].keys(person)
            rps: dict[str, int] = {}
            for key in keys:
                rps[str(key.get("rp") or "")] = rps.get(str(key.get("rp") or ""), 0) + 1
            for rp, count in rps.items():
                by_rp[rp] = by_rp.get(rp, 0) + count
            state = src["second_factors"].state(person)
            rows.append({"name": person, "role": ws.members[person], "custom_role": ws.member_roles.get(person, ""),
                         "servers": len(ws.member_servers[person]) if person in ws.member_servers else None,
                         "password": password_set(src["users"][person]), "totp": bool(state["on"]),
                         "recovery_left": int(state["left"]), "passkeys": rps, "sso": person in linked,
                         "other_workspaces": sorted(w for w, o in src["workspaces"].items()
                                                    if w != name and person in o.members)})
        tokens = [{"name": t, "role": d["role"], "ci": bool(d["ci"]), "person": d.get("person") or "",
                   "servers": len(d.get("servers") or ())}
                  for t, d in sorted(src["tokens"].items()) if d.get("workspace") == name]
        grants = [{"client": str(g.get("client_name") or ""), "person": str(g.get("user") or ""),
                   "resource": str(g.get("resource") or ""), "level": str(g.get("level") or "")}
                  for g in src["grants"] if g.get("workspace") == name]
        invited = [{"name": i.name, "expires": i.expires} for i in src["invitations"]
                   if not i.used and i.expires > src["now"] and (i.workspace == name or i.name in ws.members)]
        hooks = []
        portal = src["portal"]
        for record in (src["webhooks"] or {}).values():
            if not isinstance(record, dict) or not portal or not str(record.get("url") or "").startswith(portal + "/"):
                continue
            if any(str(i).split("/")[0] in ws.servers for i in record.get("instances") or []):
                hooks.append({"repository": str(record.get("repository") or ""), "id": record.get("id")})
        roles: dict[str, int] = {}
        for person in people:
            roles[ws.members[person]] = roles.get(ws.members[person], 0) + 1
        out[name] = {
            "label": ws.label, "people": rows, "staff": sorted(m for m in ws.members if m not in people),
            "roles": roles, "custom_roles": {r: len(c) for r, c in sorted(ws.roles.items())},
            "totp": sum(1 for r in rows if r["totp"]), "passkeys": by_rp,
            "sso": {"provider": bool(provider), "issuer": str(provider.get("issuer") or ""),
                    "client_id": str(provider.get("client_id") or ""),
                    "client_secret": bool(provider.get("client_secret")), "identities": len(linked)},
            "tokens": tokens, "grants": grants, "invitations": invited, "webhooks": hooks}
    return {"format": FORMAT, "portal": src["portal"], "workspaces": out}


def export(src: dict, oauth_clients: dict) -> dict:
    """The package: inventory()'s people and workspaces with their secrets (the module's docstring). Answered to the
    central's link only; never written on the machine."""
    people: dict[str, dict] = {}
    workspaces = {}
    for name, ws in sorted(src["workspaces"].items()):
        mine = people_of(ws, src["user_roles"], src["users"])
        for person in mine:
            if person not in people:
                people[person] = {"hash": src["users"][person],
                                  "second_factor": src["second_factors"].record(person),
                                  "passkeys": src["passkeys"].record(person)}
        grants = {str(g["id"]): copy.deepcopy(g) for g in src["grants"] if g.get("workspace") == name}
        provider = src["sso"].provider(name)
        workspaces[name] = {
            "label": ws.label, "members": {m: ws.members[m] for m in mine},
            "roles": copy.deepcopy(ws.roles),
            "member_roles": {m: r for m, r in ws.member_roles.items() if m in mine},
            "member_servers": {m: list(v) for m, v in ws.member_servers.items() if m in mine},
            "token_people": {t: p for t, p in ws.token_people.items() if p in mine},
            "tokens": {t: {"hash": d["hash"], "role": d["role"], "ci": bool(d["ci"]),
                           "servers": list(d.get("servers") or ())}
                       for t, d in sorted(src["tokens"].items()) if d.get("workspace") == name and d.get("hash")},
            "sso": {"provider": provider, "identities": src["sso"].identities_of(name)} if provider else None,
            "grants": grants,
            "clients": {g["client_id"]: copy.deepcopy(oauth_clients[g["client_id"]]) for g in grants.values()
                        if g.get("client_id") in oauth_clients}}
    return {"format": FORMAT, "portal": src["portal"], "people": people, "workspaces": workspaces}


# --- on the central panel -------------------------------------------------------------------------------------

def names_map(people_ws: dict[str, str], taken) -> dict[str, str]:
    """{name on the machine: name here} of the people who arrive: a name taken here arrives as <name>.<workspace>
    (the first workspace of the person, by name), then -2, -3 (handover.names_map, the handover's rule)."""
    out: dict[str, str] = {}
    used = set(taken)
    for name in sorted(people_ws):
        found = handover.names_map([name], used, people_ws[name])
        out[name] = found[name]
        used.add(found[name])
    return out


def first_workspace(dry: dict) -> dict[str, str]:
    """{person: the first workspace (by name) the person is of}, of a dry run or a package's workspaces."""
    found: dict[str, str] = {}
    for ws, data in sorted((dry.get("workspaces") or {}).items()):
        names = [p["name"] for p in data.get("people") or []] if "people" in data else list(data.get("members") or {})
        for person in names:
            found.setdefault(person, ws)
    return found


def token_names_map(tokens_ws: dict[str, str], taken) -> dict[str, str]:
    """{token's name on the machine: its name here}: a name a token of this panel or of another import has already
    arrives as <name>.<workspace>, then -2 (the people's rule). The token is the same: only its name changes, as the
    access log and the token page say it."""
    return names_map(tokens_ws, taken)


def plan(dry: dict, taken, central_portal: str, workspaces_here=(), taken_tokens=()) -> dict:
    """The dry run said item by item (AD-018 §18.3.1 step 1, §18.3.3): for each workspace its items with their
    count, «same», «redo» (who does it again) or «stays», and each person with the name it arrives as. A workspace
    whose name is taken here (this panel's, or another import's) is blocking: slice 1 only says it, slice 2 must
    refuse or rename it before it reads the record. No secret in it: dry is inventory()'s answer."""
    rp_here = passkeys.rp_of(_host(central_portal)) if central_portal else None
    host_here = _host(central_portal)
    names = names_map(first_workspace(dry), taken)
    tokens_ws: dict[str, str] = {}
    for ws_name, ws_data in sorted((dry.get("workspaces") or {}).items()):
        for token in ws_data.get("tokens") or []:
            tokens_ws.setdefault(str(token.get("name") or ""), ws_name)
    token_names = token_names_map(tokens_ws, taken_tokens)
    machine_host = _host(str(dry.get("portal") or ""))
    out, totals = [], {}

    def item(rows, key, count, verdict, who="", **extra):
        rows.append({"item": key, "count": count, "verdict": verdict, "who": who, **extra})
        totals[key] = totals.get(key, 0) + count

    for name, data in sorted((dry.get("workspaces") or {}).items()):
        rows: list[dict] = []
        people = data.get("people") or []
        item(rows, "people", len(people), SAME)
        renamed = [p["name"] for p in people if names.get(p["name"], p["name"]) != p["name"]]
        if renamed:
            item(rows, "renamed", len(renamed), REDO, BY_PERSON)
        item(rows, "roles", sum((data.get("roles") or {}).values()), SAME)
        item(rows, "custom_roles", len(data.get("custom_roles") or {}), SAME)
        item(rows, "totp", int(data.get("totp") or 0), SAME)
        for rp, count in sorted((data.get("passkeys") or {}).items()):
            item(rows, "passkeys", count, SAME if rp_here and rp == rp_here else REDO,
                 "" if rp_here and rp == rp_here else BY_PERSON, rp=rp)
        sso = data.get("sso") or {}
        if sso.get("provider"):
            same_name = bool(host_here) and host_here == machine_host
            item(rows, "sso", 1, SAME if same_name else REDO, "" if same_name else BY_PROVIDER,
                 uri=f"{central_portal}/sso/{name}/callback" if central_portal else "")
            item(rows, "sso_identities", int(sso.get("identities") or 0), SAME)
        item(rows, "tokens", len(data.get("tokens") or []), SAME)
        moved = {t["name"]: token_names[t["name"]] for t in data.get("tokens") or []
                 if token_names.get(t["name"], t["name"]) != t["name"]}
        if moved:
            item(rows, "renamed_tokens", len(moved), SAME, names=moved)
        grants = data.get("grants") or []
        if grants:
            same = [g for g in grants if host_here and _host(g.get("resource") or "") == host_here]
            if same:
                item(rows, "grants", len(same), SAME)
            if len(grants) > len(same):
                item(rows, "grants", len(grants) - len(same), REDO, BY_PERSON)
        if data.get("invitations"):
            item(rows, "invitations", len(data["invitations"]), REDO, BY_ADMIN)
        if data.get("webhooks"):
            item(rows, "webhooks", len(data["webhooks"]), REDO, BY_SWITCH)
        if data.get("staff"):
            item(rows, "staff", len(data["staff"]), STAYS)
        out.append({"name": name, "label": data.get("label") or "", "taken_here": name in set(workspaces_here),
                    "blocking": name in set(workspaces_here),
                    "items": rows,
                    "people": [{**{k: p.get(k) for k in ("name", "role", "custom_role", "totp", "passkeys", "sso",
                                                          "password", "other_workspaces")},
                                "arrives_as": names.get(p["name"], p["name"])} for p in people]})
    return {"portal": central_portal, "rp": rp_here or "", "machine_portal": dry.get("portal") or "",
            "workspaces": out, "totals": totals,
            "renamed": {o: n for o, n in sorted(names.items()) if o != n},
            "renamed_tokens": {o: n for o, n in sorted(token_names.items()) if o != n},
            "blocking": sorted(w["name"] for w in out if w["blocking"])}



# --- what a package may hold: a machine is not trusted with what it writes here (AD-009's rule, «chi riceve non si
# fida del contenuto»). Every person is a member of one of its workspaces, every reference to a person is one of
# them, every name is one this panel takes, every value of the type and size it should be.

MAX_PACKAGE = 64 << 20  # bytes of the package read through the link, at most
MAX_INVENTORY = 16 << 20  # bytes of the dry run
# the whole answer of the machine, read within this (a page's time, AD-018 §8.9), whatever it drips
TIMEOUT = 60.0
MAX_PEOPLE = 5000
MAX_WORKSPACES = 1000
MAX_ITEMS = 1000  # tokens, grants, clients, identities, custom roles, passkeys of one workspace or person
MAX_TEXT = 4096
# a password's hash this panel verifies: PBKDF2-SHA256 with the iterations of a panel (the tests' 1000 to twice the
# default), or the mark of a person with no password yet
MIN_ITERATIONS = 1000
MAX_ITERATIONS = 2 * ITERATIONS
_PBKDF2 = re.compile(r"pbkdf2_sha256\$(\d{1,8})\$[0-9a-f]{2,128}\$[0-9a-f]{64}")
_UNSET = re.compile(re.escape(UNSET) + r"\$[0-9a-f]{16,128}")
_TOKEN_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@:+-]{0,99}")
# an OAuth grant as OAuthStore makes it: its id 8 random bytes in hex, its name oauth:<person>:<client>:<id[:4]>
_GRANT_ID = re.compile(r"[0-9a-f]{16}")
_GRANT_NAME = re.compile(re.escape(PREFIX) + r"([A-Za-z0-9._@-]{1,64}):([a-z0-9-]{1,20}):([0-9a-f]{4})")
_KEY = re.compile(r"[\x21-\x7e]{1,512}")


@functools.lru_cache(maxsize=1)
def _dummy() -> str:
    """A hash a check of an unknown name pays (Authenticator does the same): who was imported is not told by time.
    Made at the first check, not when the module is imported."""
    return hash_password(secrets.token_hex(8))


def _refuse(where: str, what: str):
    raise IdentityImportError(f"the package of identities is refused: {where} {what}")


def _text(value, where: str, limit: int = MAX_TEXT, empty: bool = True) -> str:
    if not isinstance(value, str) or len(value) > limit or (not empty and not value):
        _refuse(where, "is not a text of its size")
    return value


def _mapping(value, where: str, limit: int = MAX_ITEMS, none: bool = True) -> dict:
    if value is None and none:
        return {}
    if not isinstance(value, dict) or len(value) > limit or any(not isinstance(k, str) or len(k) > 512 for k in value):
        _refuse(where, "is not a mapping of its size")
    return value


def _number(value, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _refuse(where, "is not a number")
    return value


def _texts(value, where: str, limit: int = MAX_ITEMS) -> list:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > limit:
        _refuse(where, "is not a list of its size")
    return [_text(v, f"{where}[]", 512) for v in value]


def _password_hash(value, where: str) -> str:
    found = _PBKDF2.fullmatch(value) if isinstance(value, str) else None
    if found is not None and MIN_ITERATIONS <= int(found.group(1)) <= MAX_ITERATIONS:
        return value
    if isinstance(value, str) and _UNSET.fullmatch(value):
        return value
    _refuse(where, f"is not a password's hash this panel verifies (PBKDF2-SHA256, {MIN_ITERATIONS} to "
                   f"{MAX_ITERATIONS} iterations)")


def _second_factor(value, where: str) -> dict | None:
    if value is None:
        return None
    _mapping(value, where, 16, none=False)
    secret = _text(value.get("secret"), f"{where}.secret", 128, empty=False)
    try:
        totp.totp(secret, 0)
    except (ValueError, TypeError):
        _refuse(f"{where}.secret", "is not a TOTP secret")
    last = value.get("last", -1)
    if isinstance(last, bool) or not isinstance(last, int):
        _refuse(f"{where}.last", "is not a step")
    return {"secret": secret, "since": _number(value.get("since", 0), f"{where}.since"), "last": last,
            "salt": _text(value.get("salt", ""), f"{where}.salt", 128),
            "recovery": _texts(value.get("recovery"), f"{where}.recovery", 64)}


def _passkeys(value, where: str) -> dict | None:
    if value is None:
        return None
    _mapping(value, where, 8, none=False)
    keys = value.get("keys")
    if not isinstance(keys, list) or len(keys) > MAX_ITEMS:
        _refuse(f"{where}.keys", "is not a list of passkeys")
    kept = []
    for i, key in enumerate(keys):
        at = f"{where}.keys[{i}]"
        _mapping(key, at, 32, none=False)
        count = key.get("count", 0)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            _refuse(f"{at}.count", "is not a counter")
        kept.append({"id": _text(key.get("id"), f"{at}.id", 1024, empty=False),
                     "key": _text(key.get("key"), f"{at}.key", 4096, empty=False),
                     "rp": _text(key.get("rp"), f"{at}.rp", 253, empty=False), "count": count,
                     "name": _text(key.get("name", ""), f"{at}.name", 200),
                     "created": _number(key.get("created") or 0, f"{at}.created"),
                     "used": None if key.get("used") is None else _number(key["used"], f"{at}.used"),
                     "transports": _texts(key.get("transports"), f"{at}.transports", 8),
                     "synced": bool(key.get("synced"))})
    return {"handle": _text(value.get("handle", ""), f"{where}.handle", 256), "keys": kept}


def _json_leaf(value, where: str, depth: int = 0):
    """A value of a provider, a grant or a client: texts, numbers, booleans, lists and mappings of them, bounded."""
    if depth > 4:
        _refuse(where, "is nested too deep")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _text(value, where)
    if isinstance(value, list):
        if len(value) > MAX_ITEMS:
            _refuse(where, "is too long")
        return [_json_leaf(v, f"{where}[]", depth + 1) for v in value]
    if isinstance(value, dict):
        _mapping(value, where, MAX_ITEMS, none=False)
        return {k: _json_leaf(v, f"{where}.{k}", depth + 1) for k, v in value.items()}
    _refuse(where, "is not a value of a package")


def checked(package) -> dict:
    """The package as this panel takes it, or IdentityImportError: its people are exactly the members of its
    workspaces, each reference to a person (custom roles, servers, tokens, single sign-on, grants) is one of them,
    every name is a user name, every role one of ROLES, every value of its type and size."""
    if not isinstance(package, dict) or package.get("format") != FORMAT:
        raise IdentityImportError("the machine did not answer a package of identities this panel reads (format 1)")
    people = _mapping(package.get("people"), "people", MAX_PEOPLE, none=False)
    spaces = _mapping(package.get("workspaces"), "workspaces", MAX_WORKSPACES, none=False)
    members_all: set[str] = set()
    token_names_seen: set[str] = set()
    grant_ids: set[str] = set()
    out_spaces = {}
    for ws, data in spaces.items():
        at = f"workspace {ws[:40]!r}"
        if not WORKSPACE_NAME.fullmatch(ws):
            _refuse(at, "is not a workspace's name")
        data = _mapping(data, at, 32, none=False)
        members = _mapping(data.get("members"), f"{at}.members", MAX_PEOPLE)
        for person, role in members.items():
            if not MEMBER_NAME.fullmatch(person) or not USER_NAME.fullmatch(person):
                _refuse(f"{at}.members", f"has {person[:40]!r}, not a user name")
            if role not in ROLES:
                _refuse(f"{at}.members.{person}", f"has the role {str(role)[:20]!r}, not one of {', '.join(ROLES)}")
        mine = set(members)
        members_all |= mine

        def person_of(value, where, mine=mine):
            if not isinstance(value, str) or value not in mine:
                _refuse(where, "names someone who is not a member of the workspace")
            return value
        roles = {}
        for name, caps in _mapping(data.get("roles"), f"{at}.roles").items():
            _text(name, f"{at}.roles", 40, empty=False)
            caps = _texts(caps, f"{at}.roles.{name}", 64)
            if any(c not in CAPABILITIES for c in caps):
                _refuse(f"{at}.roles.{name}", "has a capability this panel does not know")
            roles[name] = caps
        member_roles = {}
        for person, role in _mapping(data.get("member_roles"), f"{at}.member_roles").items():
            person_of(person, f"{at}.member_roles")
            if role not in roles:
                _refuse(f"{at}.member_roles.{person}", "is not one of its custom roles")
            member_roles[person] = role
        member_servers = {person_of(p, f"{at}.member_servers"): _texts(v, f"{at}.member_servers.{p}")
                          for p, v in _mapping(data.get("member_servers"), f"{at}.member_servers").items()}
        tokens = {}
        for name, token in _mapping(data.get("tokens"), f"{at}.tokens").items():
            if not _TOKEN_NAME.fullmatch(name) or name.startswith("oauth:"):
                _refuse(f"{at}.tokens", f"has {name[:40]!r}, not a token's name")
            token = _mapping(token, f"{at}.tokens.{name}", 8, none=False)
            if not isinstance(token.get("hash"), str) or not _TOKEN_HASH.fullmatch(token["hash"]):
                _refuse(f"{at}.tokens.{name}.hash", "is not a token's hash")
            if token.get("role") not in ROLES:
                _refuse(f"{at}.tokens.{name}.role", f"is not one of {', '.join(ROLES)}")
            if name in token_names_seen:
                _refuse(f"{at}.tokens", f"has {name[:40]!r}, a token of another of its workspaces")
            token_names_seen.add(name)
            tokens[name] = {"hash": token["hash"], "role": token["role"], "ci": bool(token.get("ci")),
                            "servers": _texts(token.get("servers"), f"{at}.tokens.{name}.servers")}
        token_people = {}
        for name, person in _mapping(data.get("token_people"), f"{at}.token_people").items():
            if name not in tokens:
                _refuse(f"{at}.token_people", f"names a token {name[:40]!r} that did not come")
            token_people[name] = person_of(person, f"{at}.token_people.{name}")
        sso = data.get("sso")
        if sso is not None:
            sso = _mapping(sso, f"{at}.sso", 4, none=False)
            provider = _json_leaf(_mapping(sso.get("provider"), f"{at}.sso.provider", 32, none=False), f"{at}.sso.provider")
            identities = {}
            for key, ident in _mapping(sso.get("identities"), f"{at}.sso.identities", MAX_PEOPLE).items():
                ident = _mapping(ident, f"{at}.sso.identities", 8, none=False)
                person_of(ident.get("user"), f"{at}.sso.identities")
                identities[key] = _json_leaf(ident, f"{at}.sso.identities")
            sso = {"provider": provider, "identities": identities}
        grants = {}
        for gid, grant in _mapping(data.get("grants"), f"{at}.grants").items():
            grant = _json_leaf(_mapping(grant, f"{at}.grants", 64, none=False), f"{at}.grants")
            person_of(grant.get("user"), f"{at}.grants.{gid[:20]}.user")
            if grant.get("workspace") != ws:
                _refuse(f"{at}.grants.{gid[:20]}", "is of another workspace")
            # its id is its key, its name the one OAuthStore gives it (oauth:<person>:<client>:<id[:4]>): the
            # store finds a token's grant by name, and a name made up here would be another grant's
            found = _GRANT_NAME.fullmatch(str(grant.get("name") or ""))
            if not _GRANT_ID.fullmatch(gid) or grant.get("id") != gid:
                _refuse(f"{at}.grants.{gid[:20]}", "has an id that is not its own")
            if found is None or found.group(1) != grant["user"] or found.group(3) != gid[:4]:
                _refuse(f"{at}.grants.{gid[:20]}.name", "is not the name of its person's grant")
            if gid in grant_ids:
                _refuse(f"{at}.grants.{gid[:20]}", "came twice")
            grant_ids.add(gid)
            grants[gid] = grant
        clients = {cid: _json_leaf(_mapping(c, f"{at}.clients", 64, none=False), f"{at}.clients")
                   for cid, c in _mapping(data.get("clients"), f"{at}.clients").items()}
        out_spaces[ws] = {"label": _text(data.get("label") or "", f"{at}.label", 200), "members": dict(members),
                          "roles": roles, "member_roles": member_roles, "member_servers": member_servers,
                          "token_people": token_people, "tokens": tokens, "sso": sso, "grants": grants,
                          "clients": clients}
    if set(people) != members_all:
        _refuse("people", "are not the members of its workspaces: "
                + ", ".join(sorted(set(people) ^ members_all)[:5]))
    out_people = {}
    for name, person in people.items():
        person = _mapping(person, f"person {name[:40]!r}", 8, none=False)
        out_people[name] = {"hash": _password_hash(person.get("hash"), f"the hash of {name[:40]!r}"),
                            "second_factor": _second_factor(person.get("second_factor"), f"the app of {name[:40]!r}"),
                            "passkeys": _passkeys(person.get("passkeys"), f"the passkeys of {name[:40]!r}")}
    return {"format": FORMAT, "portal": _text(package.get("portal") or "", "portal", 300),
            "people": out_people, "workspaces": out_spaces}


def imported(package: dict, taken, link: str, by: str, now: str, taken_tokens=(), taken_grants=()) -> dict:
    """The record of a machine's import (the module's docstring): its people under the names they arrive with,
    with the same hashes and secrets, and its workspaces with their members, roles, custom roles, tokens, SSO and
    grants renamed the same way. A package checked() refuses is refused (IdentityImportError).
    taken_tokens: the names of this panel's tokens and of the other imports' (a token of the package with one of
    them arrives as <name>.<workspace>, its person following it); taken_grants: the ids and names of this panel's
    OAuth grants and of the other imports' (a grant of the package with one of them refuses the package)."""
    package = checked(package)
    names = names_map(first_workspace(package), taken)
    if set(names) != set(package["people"]):
        raise IdentityImportError("the package of identities is refused: a person without a workspace")
    token_names = token_names_map({t: ws for ws, data in sorted(package["workspaces"].items())
                                   for t in data["tokens"]}, taken_tokens)
    people = {names[there]: {"from": there, **data} for there, data in sorted(package["people"].items())}
    grants_taken = set(taken_grants)

    def rn(name):
        return names[name]

    def grant_here(gid: str, grant: dict) -> dict:
        found = _GRANT_NAME.fullmatch(grant["name"])
        renamed = f"{PREFIX}{rn(grant['user'])}:{found.group(2)}:{found.group(3)}"
        for value in (gid, grant["name"], renamed):
            if value in grants_taken:
                raise IdentityImportError(f"the package of identities is refused: the grant {value[:60]} is one "
                                          "of this panel's or of another import's")
        return {**grant, "user": rn(grant["user"]), "name": renamed}

    workspaces = {}
    for name, data in sorted(package["workspaces"].items()):
        sso = data["sso"]
        if sso is not None:
            sso = {"provider": sso["provider"],
                   "identities": {k: {**v, "user": rn(v["user"])} for k, v in sso["identities"].items()}}
        workspaces[name] = {
            "label": data["label"], "members": {rn(m): r for m, r in data["members"].items()},
            "roles": data["roles"], "member_roles": {rn(m): r for m, r in data["member_roles"].items()},
            "member_servers": {rn(m): v for m, v in data["member_servers"].items()},
            "token_people": {token_names[t]: rn(p) for t, p in data["token_people"].items()},
            "tokens": {token_names[t]: v for t, v in data["tokens"].items()},
            "sso": sso, "grants": {g: grant_here(g, v) for g, v in data["grants"].items()},
            "clients": data["clients"]}
    return {"format": FORMAT, "link": link, "at": now, "by": by, "portal": package["portal"],
            "names": {o: n for o, n in sorted(names.items())},
            "token_names": {o: n for o, n in sorted(token_names.items())}, "people": people, "workspaces": workspaces}


def summary(record: dict | None) -> dict | None:
    """What an import holds, by count and name: no secret (the page, the REST API, MCP)."""
    if not record:
        return None
    ws = record.get("workspaces") or {}
    people = record.get("people") or {}
    return {"at": record.get("at"), "by": record.get("by") or "",
            "people": len(people), "renamed": {o: n for o, n in (record.get("names") or {}).items() if o != n},
            "renamed_tokens": {o: n for o, n in (record.get("token_names") or {}).items() if o != n},
            "totp": sum(1 for p in people.values() if p.get("second_factor")),
            "passkeys": sum(len((p.get("passkeys") or {}).get("keys") or []) for p in people.values()),
            "workspaces": {name: {"members": len(w.get("members") or {}), "custom_roles": len(w.get("roles") or {}),
                                  "tokens": len(w.get("tokens") or {}), "sso": bool(w.get("sso")),
                                  "grants": len(w.get("grants") or {})} for name, w in sorted(ws.items())}}


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


class Store:
    """The imports of this central panel, one file per link (the module's docstring). The package itself is never
    written: it is read through the link into memory, checked, and what this panel keeps of it is the record."""

    def __init__(self, base: Path):
        self.base = Path(base)
        self.folder = self.base / FOLDER
        self._lock = threading.Lock()
        # a package of before slice 1's review was staged beside the folder: none may stay on the disk
        for left in self.base.glob(".identity-package-*") if self.base.is_dir() else ():
            left.unlink(missing_ok=True)

    def path(self, link: str) -> Path:
        if not _LINK.fullmatch(link or ""):
            raise IdentityImportError(f"{str(link)[:64]!r} is not the name of a link")
        return self.folder / f"{link}.json"

    def load(self, link: str) -> dict | None:
        try:
            data = json.loads(self.path(link).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def links(self) -> list[str]:
        if not self.folder.is_dir():
            return []
        return sorted(p.stem for p in self.folder.glob("*.json") if _LINK.fullmatch(p.stem))

    def taken(self, link: str, own) -> set[str]:
        """The names a link's import may not take: this panel's own people, and the people other links' imports
        brought."""
        found = set(own)
        for other in self.links():
            if other != link:
                found |= set(((self.load(other) or {}).get("people") or {}))
        return found

    def tokens_of_others(self, link: str) -> set[str]:
        """The names of the tokens other links' imports brought."""
        return {t for other in self.links() if other != link
                for w in ((self.load(other) or {}).get("workspaces") or {}).values() for t in (w.get("tokens") or {})}

    def grants_of_others(self, link: str) -> set[str]:
        """The ids and names of the OAuth grants other links' imports brought."""
        found: set[str] = set()
        for other in self.links():
            if other != link:
                for w in ((self.load(other) or {}).get("workspaces") or {}).values():
                    for gid, grant in (w.get("grants") or {}).items():
                        found |= {gid, str((grant or {}).get("name") or "")}
        found.discard("")
        return found

    def workspaces_of_others(self, link: str) -> set[str]:
        """The workspaces other links' imports brought: a name of them, like one of this panel's, is taken."""
        found: set[str] = set()
        for other in self.links():
            if other != link:
                found |= set(((self.load(other) or {}).get("workspaces") or {}))
        return found

    def save(self, link: str, record: dict) -> None:
        with self._lock:
            self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self.folder, 0o700)
            _write_private(self.path(link), record)

    def delete(self, link: str) -> bool:
        with self._lock:
            path = self.path(link)
            if not path.exists():
                return False
            path.unlink()
            return True

    # the proof of slice 1 (AD-018 §9.1): a person imported here with the same password and the same app
    def check(self, link: str, name: str, password: str, code: str, at: float | None = None) -> bool:
        """Whether name, imported from link, logs in with password and a code of its app of two-step verification:
        the same hash and the same secret as on the machine. An unknown name pays a dummy hash's time (who was
        imported is not told by time); the hash is verified outside the lock. A code accepted is struck out (its
        step is kept, as totp.SecondFactors does): the same code is not accepted twice. The login of the imported
        people on this panel's pages is slice 2's: this is the check it will call."""
        person = ((self.load(link) or {}).get("people") or {}).get(name)
        encoded = person.get("hash") if isinstance(person, dict) and isinstance(person.get("hash"), str) else None
        found = _PBKDF2.fullmatch(encoded or "")
        if found is None or not MIN_ITERATIONS <= int(found.group(1)) <= MAX_ITERATIONS:
            verify_password(password, _dummy())
            return False
        if not verify_password(password, encoded):
            return False
        return self.check_code(link, name, code, encoded, at)

    def check_code(self, link: str, name: str, code: str, encoded: str, at: float | None = None) -> bool:
        """Whether code is a code of the app of name, imported from link, whose hash is still encoded (the
        password was verified against it): accepted once (its step struck out under the lock). The login of the
        people a central panel serves (slice 2) is checked by their machine instead (one counter of the codes)."""
        with self._lock:
            # read again: a code accepted meanwhile is struck out already
            record = self.load(link)
            person = ((record or {}).get("people") or {}).get(name)
            if not isinstance(person, dict) or person.get("hash") != encoded:
                # imported again meanwhile with another password (or deleted): the one verified is not its any more
                return False
            factor = person.get("second_factor")
            if not isinstance(factor, dict) or not isinstance(factor.get("secret"), str) or not factor["secret"]:
                return False
            last = factor.get("last", -1)
            if isinstance(last, bool) or not isinstance(last, int):
                return False
            try:
                step = totp.code_step(factor["secret"], str(code), at if at is not None else time.time(), last)
            except (ValueError, TypeError):
                return False
            if step is None:
                return False
            factor["last"] = step
            _write_private(self.path(link), record)
            return True
