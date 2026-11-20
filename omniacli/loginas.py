# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Log in as a user of an instance (README, Log in as).

The plugin makes the user's session in the instance's own store
(ServicePlugin.login_session): no password is read or changed, and nothing
is installed in Odoo. The browser still needs Odoo's session cookie on the
instance's own name, which only a page of that name can set. So OmniaCLI
answers one path there, /.omniacli/login, which the instance's nginx passes
to OmniaCLI (the workspaces' sites have it; another nginx needs the lines
of the README):

1. the Log in as button posts the user chosen; OmniaCLI makes the session
   and keeps its sid behind a token, for TOKEN_SECONDS and one use only,
   bound to the instance's host;
2. the browser opens https://<the instance's host>/.omniacli/login?t=<token>:
   OmniaCLI checks the token and the host the request came to, sets the
   cookie session_id there and sends the browser to Odoo's home.

The sid never travels in a URL: a token seen later (a history, a proxy's
log) has been used or has expired."""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass

import httpx

from .workspaceweb import LOGIN_PATH

# the header of OmniaCLI's answers on LOGIN_PATH: asked with ?probe=1 on an
# instance's name, it says the name's nginx passes the path here
MARK = "X-OmniaCLI-Login"


async def reaches_here(host: str) -> bool:
    """Whether https://host/.omniacli/login is answered by an OmniaCLI (its
    MARK), not by Odoo: the certificate is not judged, only the path."""
    try:
        async with httpx.AsyncClient(verify=False, timeout=10, follow_redirects=False) as client:
            resp = await client.get(f"https://{host}{LOGIN_PATH}", params={"probe": "1"})
    except httpx.HTTPError:
        return False
    return resp.headers.get(MARK) == "1"

__all__ = ["LOGIN_PATH", "MARK", "reaches_here", "LoginTickets", "Ticket", "safe_home"]

# how long the browser has to open the link the button gives
TOKEN_SECONDS = 60
# Odoo's own session cookie
ODOO_COOKIE = "session_id"
# Odoo's session lifetime (odoo.http SESSION_LIFETIME, 7 days): the cookie
# lasts as long as Odoo keeps the session
COOKIE_SECONDS = 7 * 24 * 3600


@dataclass
class Ticket:
    sid: str
    host: str
    home: str
    expires: float
    # for the access log: who asked, and as whom on which instance
    who: str
    detail: str


class LoginTickets:
    """The tokens of the Log in as links: kept in memory only (a restart of
    OmniaCLI drops them, a minute's loss), each used once."""

    def __init__(self, seconds: float = TOKEN_SECONDS, clock=time.monotonic):
        self._seconds = seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._tickets: dict[str, Ticket] = {}

    def issue(self, sid: str, host: str, home: str, who: str, detail: str) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._drop_expired()
            self._tickets[token] = Ticket(sid, host.lower(), home, self._clock() + self._seconds, who, detail)
        return token

    def take(self, token: str, host: str) -> Ticket | None:
        """The ticket of token, once, when it has not expired and the request
        came to its host; a token tried on another host is spent too."""
        with self._lock:
            self._drop_expired()
            ticket = self._tickets.pop(token, None)
        if ticket is None or not secrets.compare_digest(ticket.host, host.lower()):
            return None
        return ticket

    def _drop_expired(self) -> None:
        now = self._clock()
        for token in [t for t, ticket in self._tickets.items() if ticket.expires <= now]:
            del self._tickets[token]


def safe_home(home: object) -> str:
    """Odoo's home the script names (/odoo or /web), nothing else: a path of
    this host only, never another site."""
    return home if home in ("/odoo", "/web") else "/web"
