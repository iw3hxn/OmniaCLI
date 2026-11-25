# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The pass of a machine's workspaces to the central panel, and their return (AD-018 §18.3.1 steps 2, 4 and 6,
§18.3.2; slice 5).

Slice 2 made the central panel the home of a machine's workspaces while the machine stayed the source of their
identities (it checked every login). The pass moves the source:

1. **Freeze** (the machine): the identity changes of those workspaces are refused there, «This workspace passes to
   <portal>» (members, tokens, roles, single sign-on, invitations, passwords, second steps, passkeys); the
   instances' actions are not.
2. The central panel imports them again (slice 1, the same hashes and secrets), after the freeze: nothing changes in
   between.
3. **Pass** (the machine): its record of those workspaces (central-home.json, under "pass") keeps who the people were
   (snapshot(): members, roles, custom roles, servers, tokens, never a secret, with a revision) and from now the
   central panel's scope of them is checked against it; the people's login there is switched off (the form, the
   passkeys, the single sign-on, the sessions, the tokens, the invitations, the central's own login through the link).
   Their secrets stay there, off and read by no page, COPY_DAYS (AD-013's 7 days), then the machine deletes them (a
   job): the hashes, the TOTP secrets, the passkeys, the single sign-on with its client secret, the tokens, the
   grants, the invitations; their names leave the workspaces' members.
4. On the central panel the import is the identity's single source of truth from now (Store.mutate): the login
   checks the password, the code and the passkey there, the person changes its password, its app and its passkeys
   on its Account there, and every change is marked (changed) with the record's revision.
5. A person whose second step does not pass (a passkey made on another name, or none where the portal asks one)
   never activates one with the password only (AD-018 vincolo 8.5): a one-time link (FactorLinks, LINK_HOURS) is
   made for it at the pass, shown once to the admin who passed; with it, and its password, the person sets up the
   code of an app.

**Return** («Torna sulla macchina», within the 7 days): asked on the central panel by its admin, never by the
machine (which never calls the central panel). The central panel sends the people who changed since the pass, that
workspace's only (returned() refuses any other on the machine): their hashes, apps and passkeys; the machine takes
them, switches their login on again and is no more served. After the 7 days the secrets are gone there: the way back
is a handover."""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import secrets
import threading
import time
from pathlib import Path

from . import identityimport, placement, totp
from .passkeys import b64, unb64

# the machine's side: the state of a workspace passing, kept in its central-home.json entry under PASS_KEY
PASS_KEY = "pass"
FROZEN = "frozen"
PASSED = "passed"
# the days the people's secrets stay on the machine, off (AD-013's stopped copy, AD-018 §10.6: Carlo's 7)
PASS_DAYS = placement.COPY_DAYS
# the one-time links of a second step to set up again (AD-018 vincolo 8.5): as an invitation's
LINK_HOURS = 72
LINKS_FILE = "second-factor-links.json"
LINK_PATH = "/second-factor/"


def until(at: datetime.datetime | None = None) -> str:
    """When the people's secrets of a pass of at are deleted from the machine."""
    return ((at or datetime.datetime.now()) + datetime.timedelta(days=PASS_DAYS)).isoformat(timespec="seconds")


def expired(entry: dict, now: datetime.datetime | None = None) -> bool:
    """The 7 days of a pass are over (its secrets are due to go)."""
    try:
        end = datetime.datetime.fromisoformat(str(entry.get("until") or ""))
    except ValueError:
        return False
    return (now or datetime.datetime.now()) >= end


def portal_said(portal: str) -> str:
    """The name a refusal of the freeze gives: the portal's host, or the central panel."""
    host = identityimport._host(portal) if portal else ""
    return host or "the central panel"


def frozen_text(portal: str) -> str:
    """The refusal of an identity change of a workspace frozen or passed (AD-018 §18.3.1 step 2)."""
    return f"This workspace passes to {portal_said(portal)}: its people, tokens and access are changed there"


# --- on the machine -------------------------------------------------------------------------------------------

def snapshot(workspaces: dict, names: list[str], people_of, token_info: dict, revision: int = 1) -> dict:
    """What the machine keeps of the people of the workspaces names passing (no secret): {workspace: {members,
    member_roles, roles, member_servers, token_people, tokens: {name: {role, ci, servers}}}}, revision; people_of(ws)
    the people who pass (identityimport.people_of), token_info {token: {workspace, role, ci, servers}}."""
    out = {}
    for name in names:
        ws = workspaces[name]
        mine = set(people_of(name))
        out[name] = {"members": {m: ws.members[m] for m in sorted(mine)},
                     "member_roles": {m: r for m, r in ws.member_roles.items() if m in mine},
                     "roles": {r: sorted(c) for r, c in ws.roles.items()},
                     "member_servers": {m: sorted(v) for m, v in ws.member_servers.items() if m in mine},
                     "token_people": {t: p for t, p in ws.token_people.items() if p in mine},
                     "tokens": {t: {"role": d["role"], "ci": bool(d["ci"]), "servers": sorted(d.get("servers") or ())}
                                for t, d in sorted(token_info.items()) if d.get("workspace") == name}}
    return {"revision": int(revision), "workspaces": out}


def people_in(snap: dict) -> set[str]:
    """The people a pass record names (all its workspaces)."""
    return {m for w in ((snap or {}).get("workspaces") or {}).values() for m in (w.get("members") or {})}


def returned(package, allowed: set[str]) -> dict:
    """The people of a return as this machine takes them (AD-018 §18.3.2): only names of allowed (the people of the
    workspaces passed, as the machine knew them at the pass), each with a hash this OmniaCLI verifies, an app and
    passkeys of their type and size (identityimport's checks: the central panel is trusted with no more than
    those people). IdentityImportError otherwise."""
    if not isinstance(package, dict):
        raise identityimport.IdentityImportError("the return is not a mapping of people")
    people = identityimport._mapping(package, "people", identityimport.MAX_PEOPLE, none=False)
    out = {}
    for name, data in people.items():
        if name not in allowed:
            raise identityimport.IdentityImportError(
                f"the return is refused: {name[:40]!r} is no person of the workspaces passed")
        data = identityimport._mapping(data, f"person {name[:40]!r}", 8, none=False)
        out[name] = {"hash": identityimport._password_hash(data.get("hash"), f"the hash of {name[:40]!r}"),
                     "second_factor": identityimport._second_factor(data.get("second_factor"),
                                                                     f"the app of {name[:40]!r}"),
                     "passkeys": identityimport._passkeys(data.get("passkeys"), f"the passkeys of {name[:40]!r}")}
    return out


# --- on the central panel: a person's record of an import as the source of its identity -------------------------

def totp_on(person: dict) -> bool:
    factor = person.get("second_factor")
    return isinstance(factor, dict) and isinstance(factor.get("secret"), str) and bool(factor["secret"])


def totp_state(person: dict) -> dict:
    """As totp.SecondFactors.state: {on, since, left}."""
    if not totp_on(person):
        return {"on": False, "since": None, "left": 0}
    factor = person["second_factor"]
    return {"on": True, "since": factor.get("since"), "left": len(factor.get("recovery") or [])}


def totp_check(person: dict, code: str, at: float | None = None) -> tuple[str, int] | None:
    """As totp.SecondFactors.check on a record: ("code"|"recovery", left), the step or the recovery code struck out
    in person; None: neither (or used already)."""
    if not totp_on(person):
        return None
    factor = person["second_factor"]
    left = [h for h in factor.get("recovery") or [] if isinstance(h, str)]
    if totp.looks_like_recovery(code):
        hashed = totp._recovery_hash(str(factor.get("salt") or ""), code)
        match = None
        for h in left:
            if hmac.compare_digest(h, hashed):
                match = h
        if match is None:
            return None
        factor["recovery"] = [h for h in left if h != match]
        return "recovery", len(factor["recovery"])
    last = factor.get("last", -1)
    if isinstance(last, bool) or not isinstance(last, int):
        return None
    try:
        step = totp.code_step(factor["secret"], str(code), at if at is not None else time.time(), last)
    except (ValueError, TypeError):
        return None
    if step is None:
        return None
    factor["last"] = step
    return "code", len(left)


def totp_enable(person: dict, secret: str, step: int) -> list[str]:
    """The app switched on in person with secret (its code of step confirmed): the recovery codes, shown once."""
    codes = totp.new_recovery_codes()
    salt = secrets.token_hex(16)
    person["second_factor"] = {"secret": secret, "since": int(time.time()), "last": int(step), "salt": salt,
                               "recovery": [totp._recovery_hash(salt, c) for c in codes]}
    return codes


def passkey_keys(person: dict, rp: str | None = None) -> list[dict]:
    """As passkeys.PasskeyStore.keys on a record: its passkeys (of rp when given), the newest first."""
    found = ((person.get("passkeys") or {}).get("keys") or []) if isinstance(person.get("passkeys"), dict) else []
    return sorted((dict(k) for k in found if isinstance(k, dict) and (rp is None or k.get("rp") == rp)),
                  key=lambda k: -(k.get("created") or 0))


def passkey_handle(person: dict) -> bytes:
    found = person.get("passkeys") if isinstance(person.get("passkeys"), dict) else None
    if found and found.get("handle"):
        try:
            return unb64(str(found["handle"]))
        except ValueError:
            pass
    return secrets.token_bytes(32)


def passkey_add(person: dict, handle: bytes, key: dict) -> None:
    found = person.get("passkeys") if isinstance(person.get("passkeys"), dict) else None
    if not found:
        found = person["passkeys"] = {"handle": b64(handle), "keys": []}
    found.setdefault("handle", b64(handle))
    found.setdefault("keys", [])
    if any(k.get("id") == key["id"] for k in found["keys"]):
        raise ValueError("This passkey is registered already")
    found["keys"].append(key)


def passkey_remove(person: dict, key_id: str) -> dict | None:
    found = person.get("passkeys") if isinstance(person.get("passkeys"), dict) else None
    gone = next((k for k in (found or {}).get("keys") or [] if k.get("id") == key_id), None)
    if gone is not None:
        found["keys"] = [k for k in found["keys"] if k is not gone]
    return gone


def passkey_used(person: dict, key_id: str, count: int) -> None:
    for key in passkey_keys_raw(person):
        if key.get("id") == key_id:
            key["count"], key["used"] = int(count), time.time()


def passkey_keys_raw(person: dict) -> list[dict]:
    found = person.get("passkeys") if isinstance(person.get("passkeys"), dict) else None
    return [k for k in (found or {}).get("keys") or [] if isinstance(k, dict)]


def needs_link(person: dict, rp_here: str, required: bool) -> bool:
    """Whether a person's second step does not pass to the central panel (AD-018 vincolo 8.5): it has no app and
    no passkey of the portal's RP ID, and it had a passkey on the machine (made on another name) or the portal asks
    a second step of everybody. Never activated with the password only: a one-time link."""
    if totp_on(person):
        return False
    keys = passkey_keys(person)
    if keys and rp_here and any(k.get("rp") == rp_here for k in keys):
        return False
    return bool(keys) or required


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class FactorLinks:
    """The one-time links of a second step to set up again, on the central panel: {digest of the token: {link,
    person, at, by, expires, used}} in a file 600; the token itself is shown once and kept nowhere."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}

    def _save(self, data: dict) -> None:
        if not data:
            self.path.unlink(missing_ok=True)
            return
        identityimport._write_private(self.path, data)

    def create(self, link: str, person: str, by: str, now: float | None = None) -> tuple[str, float]:
        """A new link of person (a link of before of the same person is no more): its token and its end."""
        now = now if now is not None else time.time()
        token = secrets.token_urlsafe(32)
        with self._lock:
            data = {k: v for k, v in self._load().items()
                    if not (v.get("link") == link and v.get("person") == person) and v.get("expires", 0) > now}
            data[_digest(token)] = {"link": link, "person": person, "at": now, "by": by,
                                    "expires": now + LINK_HOURS * 3600}
            self._save(data)
        return token, now + LINK_HOURS * 3600

    def get(self, token: str | None, now: float | None = None) -> dict | None:
        """The link of token, unused and not expired; None otherwise."""
        if not token or len(token) > 200:
            return None
        found = self._load().get(_digest(token))
        if found is None or found.get("used") or found.get("expires", 0) <= (now if now is not None else time.time()):
            return None
        return dict(found)

    def use(self, token: str) -> bool:
        """The link of token used (once): False when it was not valid any more."""
        with self._lock:
            data = self._load()
            found = data.get(_digest(token))
            if found is None or found.get("used") or found.get("expires", 0) <= time.time():
                return False
            data.pop(_digest(token))
            self._save(data)
        return True

    def pending(self, link: str, now: float | None = None) -> dict[str, float]:
        """{person: its end} of the links of link still valid."""
        now = now if now is not None else time.time()
        return {v["person"]: v["expires"] for v in self._load().values()
                if v.get("link") == link and not v.get("used") and v.get("expires", 0) > now}

    def drop_link(self, link: str) -> int:
        """Every link of a machine's people away (its return): how many."""
        with self._lock:
            data = self._load()
            kept = {k: v for k, v in data.items() if v.get("link") != link}
            self._save(kept)
        return len(data) - len(kept)

