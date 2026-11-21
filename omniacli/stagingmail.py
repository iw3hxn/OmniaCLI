# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A staging's mail, caught (UX-008 §5.3): what Odoo of the staging sends
goes to the mail service of its compose project (Mailpit, the templates'
profile mail-capture) and never leaves. OmniaCLI reads it through the root
helper (compose-helper mail-list, mail-get, mail-clear), never through a
published port: the API listens on the loopback of that container only.

What the page gets is what a person reads: who, to whom, when, the subject,
the bodies and the names of the attachments; not the raw headers, not the
address of the service."""

from __future__ import annotations

import datetime
import json

from . import ops
from .composehelper import MAIL_ID, MAIL_LIMIT
from .jobs import JobFailed

TIMEOUT = 40
# the rows the tab shows first; «Show all» asks MAIL_LIMIT (Mailpit keeps that many)
FIRST = 50


def _answer(out: str) -> dict:
    """Mailpit's JSON, the last line that is one (docker may say something before)."""
    for line in reversed(out.splitlines()):
        if line.startswith("{"):
            try:
                found = json.loads(line)
            except ValueError:
                break
            if isinstance(found, dict):
                return found
    raise JobFailed(f"the mail service did not answer: {' | '.join(out.splitlines()[-2:])[:300]}")


def _address(item) -> str:
    """"Name <address>", or the address alone."""
    if not isinstance(item, dict):
        return ""
    address, name = str(item.get("Address") or ""), str(item.get("Name") or "").strip()
    return f"{name} <{address}>" if name and name != address else address


def _addresses(items) -> list[str]:
    return [a for a in map(_address, items if isinstance(items, list) else []) if a]


def local_time(stamp) -> str:
    """Mailpit's UTC time (2026-11-21T08:58:11.971Z) as this machine's,
    2026-11-21T09:58:11; as it is when it is no time."""
    text = str(stamp or "")
    try:
        moment = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if moment.tzinfo is None:
        return moment.isoformat(timespec="seconds")
    return moment.astimezone().replace(tzinfo=None).isoformat(timespec="seconds")


def _row(item: dict) -> dict:
    return {"id": str(item.get("ID") or ""), "received": local_time(item.get("Created")),
            "from": _address(item.get("From")), "to": _addresses(item.get("To")),
            "subject": str(item.get("Subject") or ""), "attachments": int(item.get("Attachments") or 0)}


async def _ask(instance, *args: str) -> str:
    code, out = await ops.capture(instance.restart.command(*args), timeout=TIMEOUT)
    if code:
        raise JobFailed(f"the caught mail cannot be read (is the staging running?): {out[-300:]}")
    return out


async def messages(instance, every: bool = False) -> dict:
    """{"total", "messages": [{id, received, from, to, subject, attachments}]},
    the newest first: the first FIRST, or all of them (MAIL_LIMIT)."""
    found = _answer(await _ask(instance, "mail-list", "0", str(MAIL_LIMIT if every else FIRST)))
    rows = [_row(m) for m in found.get("messages") or [] if isinstance(m, dict)]
    total = found.get("messages_count", found.get("total", len(rows)))
    return {"total": int(total or 0), "messages": rows}


async def message(instance, mail_id: str) -> dict:
    """One message as it was: from, to, cc, date, subject, its HTML and text
    bodies, the names (and sizes) of its attachments."""
    if not MAIL_ID.fullmatch(str(mail_id)):
        raise LookupError(f"{mail_id!r} is not a message id")
    code, out = await ops.capture(instance.restart.command("mail-get", str(mail_id)), timeout=TIMEOUT)
    if code and " 404 " in out:
        # wget: server returned error: HTTP/1.1 404 Not Found (deleted, or emptied)
        raise LookupError(f"No message {mail_id} among the caught mail")
    if code:
        raise JobFailed(f"the caught mail cannot be read (is the staging running?): {out[-300:]}")
    found = _answer(out)
    attachments = [{"name": str(a.get("FileName") or ""), "size": int(a.get("Size") or 0)}
                   for a in found.get("Attachments") or [] if isinstance(a, dict)]
    return {"id": str(found.get("ID") or mail_id), "from": _address(found.get("From")),
            "to": _addresses(found.get("To")), "cc": _addresses(found.get("Cc")),
            "date": local_time(found.get("Date")), "subject": str(found.get("Subject") or ""),
            "html": str(found.get("HTML") or ""), "text": str(found.get("Text") or ""),
            "attachments": attachments}


async def clear(instance) -> None:
    """Every caught message deleted (Odoo and its queue of mail untouched)."""
    code, out = await ops.capture(instance.restart.command("mail-clear"), timeout=TIMEOUT)
    if code:
        raise JobFailed(f"the caught mail was not deleted: {out[-300:]}")
