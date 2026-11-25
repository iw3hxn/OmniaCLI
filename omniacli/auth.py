# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Password checks with PBKDF2 hashes (the login form), bearer tokens (for
MCP clients and for linked OmniaCLI instances) stored as their SHA-256, and the
roles that decide what each caller may do."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

# user: monitors only; developer: also acts on the servers (pull, update,
# restart, backup and its download); admin: everything (configuration, merge)
ROLES = ("user", "developer", "admin")
# the role of a user of workspaces only: no role on the machine, one of ROLES
# in each of its workspaces (Settings.workspaces), and nothing else
WORKSPACE_ROLE = "workspace"

ALGORITHM = "pbkdf2_sha256"
ITERATIONS = 600_000


def hash_password(password: str, iterations: int = ITERATIONS) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), iterations)
    return f"{ALGORITHM}${iterations}${salt}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, expected = encoded.split("$")
        if algorithm != ALGORITHM:
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt), int(iterations)
        )
    except ValueError:
        return False
    return secrets.compare_digest(digest.hex(), expected)


# the mark of the password of a user who has none yet (unknown_password): no password verifies with it
UNSET = "unset"


def unknown_password() -> str:
    """The password of a user who has none yet: an invited user's until the
    person sets its own from the invitation's link (no login before), a
    person of a workspace made by its single sign-on. Marked, so that it is
    known for one (password_set) also once its invitation is gone."""
    return f"{UNSET}${secrets.token_hex(32)}"


def password_set(encoded: str) -> bool:
    """Whether encoded is the hash of a password someone set (not unknown_password's). A user invited before
    AD-017 slice 6 has a hash of a random password instead: known only by its invitation, while it is kept."""
    return bool(encoded) and not encoded.startswith(f"{UNSET}$")


TOKEN_ALGORITHM = "sha256"


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """A token is random and long, so one SHA-256 is enough (no PBKDF2: MCP
    clients send it with every request)."""
    return f"{TOKEN_ALGORITHM}${hashlib.sha256(token.encode()).hexdigest()}"


@dataclass(frozen=True)
class Principal:
    """Who makes a request: a user (login form), an MCP token or a linked instance."""

    name: str
    role: str
    kind: str = "user"
    # a linked instance: its user who acts through the link (a label only)
    on_behalf: str = ""
    # a user of workspaces only (WORKSPACE_ROLE): ((workspace, role), ...);
    # role is then its role in the workspace of what the request reaches
    workspaces: tuple[tuple[str, str], ...] = ()
    scoped: bool = False
    # a linked instance: the request is one a person's browser makes through that instance's pages (its
    # proxy says which menu it shows them under), not one the machine makes by itself (a job, a look)
    person: bool = False
    # a user of workspaces, or a workspace's token of a person, in the workspaces where a custom role or
    # a limit to some servers applies: ((workspace, capabilities.Grant), ...); elsewhere its role is all
    grants: tuple = ()
    # a person or a token of a workspace whose instance is on this machine for its home (AD-014, the delegated
    # scope): the name of the home's link it came through; name and kind are then the person's, or the token's
    via: str = ""
    # such a token, when its home marks it a CI's: it never acts on a production (apiguard.production_refusal)
    ci: bool = False

    def can(self, role: str) -> bool:
        return ROLES.index(self.role) >= ROLES.index(role)

    def workspace_role(self, workspace: str) -> str | None:
        """Its role in workspace, None when it is not a member."""
        return dict(self.workspaces).get(workspace)

    def grant(self, workspace: str | None):
        """Its capabilities.Grant in workspace, None when its role there is all (no custom role, every server)."""
        return dict(self.grants).get(workspace) if workspace else None

    def __str__(self) -> str:
        """The name the jobs record as who started them."""
        if self.kind == "token":
            return f"token {self.name}"
        if self.kind == "link":
            return f"link {self.name}" + (f" ({self.on_behalf})" if self.on_behalf else "")
        return self.name


class Authenticator:
    """Check a user's password against the configured users (the login form).

    A successful PBKDF2 check is remembered for the life of the process,
    keyed by a keyed hash of the password, never the password.
    """

    def __init__(self, users: dict[str, str], tokens: dict[str, str] | None = None,
                 links: dict[str, str] | None = None):
        self.users = users
        self.tokens = tokens or {}
        self.links = links or {}
        self._key = secrets.token_bytes(32)
        self._verified: dict[str, bytes] = {}
        self._dummy = hash_password(secrets.token_hex(8))
        # the access tokens of the OAuth grants (oauth.OAuthStore, set by the app): name_of(token)
        self.oauth = None

    def update(self, users: dict[str, str], tokens: dict[str, str], links: dict[str, str]) -> None:
        """New credentials: the passwords verified so far are checked again,
        so a changed or deleted one stops working at once."""
        self.users, self.tokens, self.links = users, tokens, links
        self._verified = {}

    def _fingerprint(self, username: str, password: str) -> bytes:
        return hmac.new(self._key, f"{username}\0{password}".encode(), "sha256").digest()

    def check(self, username: str, password: str) -> bool:
        encoded = self.users.get(username)
        if encoded is None:
            # Spend the time of a real check so usernames cannot be probed
            verify_password(password, self._dummy)
            return False
        fingerprint = self._fingerprint(username, password)
        cached = self._verified.get(username)
        if cached is not None and secrets.compare_digest(cached, fingerprint):
            return True
        if verify_password(password, encoded):
            self._verified[username] = fingerprint
            return True
        return False

    def token_name(self, token: str) -> str | None:
        """The name of a configured MCP token or of a valid OAuth access
        token (oauth:…), None when it is neither."""
        found = _match(token, self.tokens)
        if found is None and self.oauth is not None and token:
            found = self.oauth.name_of(token)
        return found

    def link_name(self, token: str) -> str | None:
        """The name of a configured link token, None when it is not one."""
        return _match(token, self.links)


def _match(token: str, hashes: dict[str, str]) -> str | None:
    encoded = hash_token(token)
    found = None
    for name, expected in hashes.items():
        # every token compared, so the time does not tell which one matched
        if secrets.compare_digest(encoded, expected):
            found = name
    return found
