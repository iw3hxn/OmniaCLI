# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The uploads an MCP client is let make (upload_backup): a database from
outside, put with an HTTP PUT to a URL of its own. Each URL is for one
file, one time, for a short while, for the token that asked it, with the
size it said; where the file goes (a workspace's import area, here or on a
linked machine) was decided when it was asked, not by the PUT."""

from __future__ import annotations

import dataclasses
import re
import secrets
import time

TTL = 30 * 60  # seconds a URL waits for its PUT
MAX_SIZE = 50 << 30  # the largest file a URL takes
# the name a file keeps: a backup zip or a plain SQL dump
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,150}\.(zip|sql)")
TICKET = re.compile(r"[A-Za-z0-9_-]{32,64}")


@dataclasses.dataclass
class Ticket:
    token: str  # the name of the token that asked it: the PUT must carry it
    workspace: str
    name: str
    size: int
    sha256: str = ""
    link: str = ""  # the linked machine the file goes on to ("": here)
    expires: float = 0.0
    used: bool = False


class Uploads:
    def __init__(self, ttl: float = TTL):
        self.ttl = ttl
        self.tickets: dict[str, Ticket] = {}

    def issue(self, token: str, workspace: str, name: str, size: int, sha256: str = "", link: str = "") -> str:
        now = time.monotonic()
        # the old ones go: none outlives its TTL
        self.tickets = {k: t for k, t in self.tickets.items() if t.expires > now and not t.used}
        ident = secrets.token_urlsafe(32)
        self.tickets[ident] = Ticket(token, workspace, name, size, sha256.lower(), link, now + self.ttl)
        return ident

    def take(self, ident: str, token: str) -> Ticket | str:
        """The ticket, used from now (a second PUT is refused), or why not."""
        ticket = self.tickets.get(ident) if TICKET.fullmatch(ident or "") else None
        if ticket is None or ticket.token != token:
            # another token's URL is as unknown as a wrong one
            return "No such upload: ask upload_backup for a URL"
        if ticket.used:
            return "This upload URL was used already: ask upload_backup for a new one"
        if ticket.expires <= time.monotonic():
            del self.tickets[ident]
            return "This upload URL expired: ask upload_backup for a new one"
        ticket.used = True
        return ticket
