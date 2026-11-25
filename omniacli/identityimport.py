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
- imported(): the record the central keeps of a machine's import, with the same hashes and secrets;
- Store: one file per link in FOLDER (700, each file 600), the package staged beside it (600) only while it is
  imported, and deleted after. Deleting a link's file is the undo: nothing was written on the machine.

Who imported is in the record and in the access log of both sides. Nothing here changes the machine: no freeze, no
home switched (slice 5)."""

from __future__ import annotations

import copy
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import handover, passkeys, totp
from .auth import WORKSPACE_ROLE, password_set, verify_password

FORMAT = 1
# the internal API of the machine (AD-018 §8.7): the central's link only, never a twin in /api/v1 or MCP
API = "/api/identity/v1"
# on the central: a file per link, the identities imported from that machine
FOLDER = "identity-imports"
# the package while it is imported, beside the folder (never inside it: a backup of the folder never holds it)
STAGED = ".identity-package-"
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


def plan(dry: dict, taken, central_portal: str, workspaces_here=()) -> dict:
    """The dry run said item by item (AD-018 §18.3.1 step 1, §18.3.3): for each workspace its items with their
    count, «same», «redo» (who does it again) or «stays», and each person with the name it arrives as. No secret
    in it: dry is inventory()'s answer."""
    rp_here = passkeys.rp_of(_host(central_portal)) if central_portal else None
    host_here = _host(central_portal)
    names = names_map(first_workspace(dry), taken)
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
                    "items": rows,
                    "people": [{**{k: p.get(k) for k in ("name", "role", "custom_role", "totp", "passkeys", "sso",
                                                          "password", "other_workspaces")},
                                "arrives_as": names.get(p["name"], p["name"])} for p in people]})
    return {"portal": central_portal, "rp": rp_here or "", "machine_portal": dry.get("portal") or "",
            "workspaces": out, "totals": totals,
            "renamed": {o: n for o, n in sorted(names.items()) if o != n}}


def _check_package(package) -> None:
    if not isinstance(package, dict) or package.get("format") != FORMAT:
        raise IdentityImportError("the machine did not answer a package of identities this panel reads (format 1)")
    if not isinstance(package.get("people"), dict) or not isinstance(package.get("workspaces"), dict):
        raise IdentityImportError("the package of identities has no people or no workspaces")
    for name, person in package["people"].items():
        if not isinstance(person, dict) or not isinstance(person.get("hash"), str) or not person["hash"]:
            raise IdentityImportError(f"the person {str(name)[:40]} of the package has no password's hash")


def imported(package: dict, taken, link: str, by: str, now: str) -> dict:
    """The record of a machine's import (the module's docstring): its people under the names they arrive with,
    with the same hashes and secrets, and its workspaces with their members, roles, custom roles, tokens, SSO and
    grants renamed the same way."""
    _check_package(package)
    names = names_map(first_workspace(package), taken)
    people = {}
    for there, data in sorted(package["people"].items()):
        here = names.get(there, there)
        people[here] = {"from": there, "hash": data["hash"], "second_factor": data.get("second_factor"),
                        "passkeys": data.get("passkeys")}

    def rn(name):
        return names.get(name, name)

    workspaces = {}
    for name, data in sorted(package["workspaces"].items()):
        sso = data.get("sso")
        if isinstance(sso, dict):
            sso = {"provider": sso.get("provider"),
                   "identities": {k: {**v, "user": rn(v.get("user"))} for k, v in (sso.get("identities") or {}).items()
                                  if isinstance(v, dict)}}
        workspaces[name] = {
            "label": data.get("label") or "",
            "members": {rn(m): r for m, r in (data.get("members") or {}).items()},
            "roles": data.get("roles") or {},
            "member_roles": {rn(m): r for m, r in (data.get("member_roles") or {}).items()},
            "member_servers": {rn(m): v for m, v in (data.get("member_servers") or {}).items()},
            "token_people": {t: rn(p) for t, p in (data.get("token_people") or {}).items()},
            "tokens": data.get("tokens") or {},
            "sso": sso,
            "grants": {g: {**v, "user": rn(v.get("user"))} for g, v in (data.get("grants") or {}).items()
                       if isinstance(v, dict)},
            "clients": data.get("clients") or {}}
    return {"format": FORMAT, "link": link, "at": now, "by": by, "portal": package.get("portal") or "",
            "names": {o: n for o, n in sorted(names.items())}, "people": people, "workspaces": workspaces}


def summary(record: dict | None) -> dict | None:
    """What an import holds, by count and name: no secret (the page, the REST API, MCP)."""
    if not record:
        return None
    ws = record.get("workspaces") or {}
    people = record.get("people") or {}
    return {"at": record.get("at"), "by": record.get("by") or "",
            "people": len(people), "renamed": {o: n for o, n in (record.get("names") or {}).items() if o != n},
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
    """The imports of this central panel, one file per link (the module's docstring)."""

    def __init__(self, base: Path):
        self.base = Path(base)
        self.folder = self.base / FOLDER
        self._lock = threading.Lock()

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

    # the package, while it is imported
    def stage(self, link: str, raw: bytes) -> Path:
        """The package as the link brought it, in a file of its own (600) beside the folder, never in it."""
        self.path(link)
        self.base.mkdir(parents=True, exist_ok=True)
        path = self.base / f"{STAGED}{link}-{secrets.token_hex(6)}.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
        return path

    def staged(self) -> list[Path]:
        return sorted(self.base.glob(f"{STAGED}*")) if self.base.is_dir() else []

    # the proof of slice 1 (AD-018 §9.1): a person imported here with the same password and the same app
    def check(self, link: str, name: str, password: str, code: str, at: float | None = None) -> bool:
        """Whether name, imported from link, logs in with password and a code of its app of two-step verification:
        the same hash and the same secret as on the machine. A code accepted is struck out (its step is kept, as
        totp.SecondFactors does): the same code is not accepted twice. The login of the imported people on this
        panel's pages is slice 2's: this is the check it will call."""
        with self._lock:
            record = self.load(link)
            person = ((record or {}).get("people") or {}).get(name)
            if not person or not verify_password(password, str(person.get("hash") or "")):
                return False
            factor = person.get("second_factor")
            if not isinstance(factor, dict) or not factor.get("secret"):
                return False
            step = totp.code_step(str(factor["secret"]), code, at if at is not None else time.time(),
                                  int(factor.get("last", -1)))
            if step is None:
                return False
            factor["last"] = step
            _write_private(self.path(link), record)
            return True
