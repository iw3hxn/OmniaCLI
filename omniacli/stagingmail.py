# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A staging's mail, caught (UX-008 §5.3): what Odoo of the staging sends
goes to the mail service of its compose project (Mailpit, the templates'
profile mail-capture) and never leaves. OmniaCLI reads it through the root
helper (compose-helper mail-list, mail-get, mail-clear), never through a
published port: the API listens on the loopback of that container only.

What the page gets is what a person reads: who, to whom, when, the subject,
the bodies and the names of the attachments; not the raw headers, not the
address of the service.

Its incoming mail (UX-008 §5.4), in a staging made with the inbox service:
every address of the caught mail is a mailbox of the tab; a person writes
as an outside sender (compose-helper mail-send), and Mailpit hands what goes
to a .test address to the inbox service, whose mailboxes Odoo reads by IMAP
once configured (compose-helper mail-inbox, odoo/mail_inbox.py)."""

from __future__ import annotations

import datetime
import json

from . import ops
from .composehelper import ADDRESS, MAIL_ID, MAIL_LIMIT, TEST_DOMAIN
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


def _plain(item) -> str:
    """The address alone, lower case ("" when it is none)."""
    return str(item.get("Address") or "").strip().lower() if isinstance(item, dict) else ""


def _row(item: dict) -> dict:
    people = [item.get("From"), *(item.get("To") or []), *(item.get("Cc") or []), *(item.get("Bcc") or [])]
    return {"id": str(item.get("ID") or ""), "received": local_time(item.get("Created")),
            "from": _address(item.get("From")), "to": _addresses(item.get("To")),
            "subject": str(item.get("Subject") or ""), "attachments": int(item.get("Attachments") or 0),
            # the mailboxes it is in: of whom wrote it and of whom it went to
            "boxes": sorted({a for a in map(_plain, people) if a})}


def boxes(rows: list[dict]) -> list[dict]:
    """Every address of the caught mail, as a mailbox: [{address, count}],
    the most used first, then by name."""
    counts: dict[str, int] = {}
    for row in rows:
        for address in row["boxes"]:
            counts[address] = counts.get(address, 0) + 1
    return [{"address": a, "count": n} for a, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def domain_of(server: str) -> str:
    """The alias domain of a staging (UX-008 §5.4): its name as one label
    of a .test domain, which exists nowhere (RFC 2606), never a real one."""
    label = "".join(c if c.isascii() and c.isalnum() else "-" for c in server.lower()).strip("-")
    while "--" in label:
        label = label.replace("--", "-")
    label = label[:63].strip("-") or "staging"
    found = f"{label}.test"
    assert TEST_DOMAIN.fullmatch(found), found
    return found


async def _ask(instance, *args: str) -> str:
    code, out = await ops.capture(instance.restart.command(*args), timeout=TIMEOUT)
    if code:
        raise JobFailed(f"the caught mail cannot be read (is the staging running?): {out[-300:]}")
    return out


async def messages(instance, every: bool = False, box: str = "") -> dict:
    """{"total", "messages": [{id, received, from, to, subject, attachments,
    boxes}], "boxes"}, the newest first: the first FIRST, or all of them;
    with box, those of that mailbox (written by or to that address) only.
    Mailpit keeps MAIL_LIMIT messages: they are all read, for the mailboxes."""
    box = box.strip().lower()
    if box and not ADDRESS.fullmatch(box):
        raise LookupError(f"{box!r} is not an address")
    found = _answer(await _ask(instance, "mail-list", "0", str(MAIL_LIMIT)))
    rows = [_row(m) for m in found.get("messages") or [] if isinstance(m, dict)]
    shown = [r for r in rows if box in r["boxes"]] if box else rows
    total = len(shown) if box else int(found.get("messages_count", found.get("total", len(rows))) or 0)
    return {"total": total, "messages": shown if every else shown[:FIRST], "boxes": boxes(rows), "box": box}


async def held(instance, since: float) -> tuple[int, bool]:
    """The caught messages since a time, for the monitor (AD-019 slice 3): (how many, whether the service lists
    MAIL_LIMIT of them, all since then: there may be more). Only the number leaves here."""
    found = _answer(await _ask(instance, "mail-list", "0", str(MAIL_LIMIT)))
    rows = [m for m in found.get("messages") or [] if isinstance(m, dict)]
    count = 0
    for item in rows:
        try:
            moment = datetime.datetime.fromisoformat(str(item.get("Created") or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        if moment.tzinfo is not None and moment.timestamp() >= since:
            count += 1
    return count, len(rows) >= MAIL_LIMIT and count >= len(rows)


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
            "attachments": attachments,
            # what a reply needs: its Message-ID (Odoo finds the record by it) and where to answer
            "message_id": str(found.get("MessageID") or ""),
            "reply_to": [a for a in map(_plain, found.get("ReplyTo") or []) if a],
            "from_address": _plain(found.get("From")),
            "to_addresses": [a for a in map(_plain, found.get("To") or []) if a]}


async def clear(instance) -> None:
    """Every caught message deleted (Odoo and its queue of mail untouched)."""
    code, out = await ops.capture(instance.restart.command("mail-clear"), timeout=TIMEOUT)
    if code:
        raise JobFailed(f"the caught mail was not deleted: {out[-300:]}")


# --- its incoming mail (UX-008 §5.4): written as an outside sender, read by Odoo

# odoo shell starts in some seconds; reading every mailbox may take a minute
INBOX_TIMEOUT = 240
INBOX_MARK = "OMNIACLI-MAIL "


async def send(instance, message: dict) -> None:
    """A message written as an outside sender ({from, name, to, subject,
    body, in_reply_to}), checked and built by the helper, to the staging's
    mail service: it shows in the Mail tab, and a message to a .test address
    reaches its mailbox, which Odoo reads."""
    code, out = await ops.capture(instance.restart.command("mail-send"), input=json.dumps(message).encode(),
                                  timeout=TIMEOUT)
    if code:
        raise JobFailed(f"the message was not sent: {out.removeprefix('compose-helper: ')[-300:]}")


async def inbox(instance, server: str, mode: str = "state") -> dict:
    """Odoo's incoming mail of the staging (odoo/mail_inbox.py): its state;
    with mode on, configured; with fetch, configured and every mailbox read
    now. {on, domain, mailboxes, servers, crons, last_fetch, said, fetched}."""
    opts = instance.options
    cmd = instance.restart.command("mail-inbox", getattr(opts, "odoo_service", "odoo"), opts.database)
    code, out = await ops.capture(cmd, input=json.dumps({"mode": mode, "domain": domain_of(server)}).encode(),
                                  timeout=INBOX_TIMEOUT)
    found = next((line[len(INBOX_MARK):] for line in reversed(out.splitlines()) if line.startswith(INBOX_MARK)), None)
    if code or found is None:
        raise JobFailed(f"Odoo of the staging did not answer about its incoming mail (is it running?): "
                        f"{' | '.join(out.splitlines()[-3:])[-300:]}")
    try:
        data = json.loads(found)
    except ValueError:
        raise JobFailed(f"Odoo of the staging did not answer about its incoming mail: {found[:300]}") from None
    if not isinstance(data, dict) or data.get("error"):
        raise JobFailed(str(data.get("error") if isinstance(data, dict) else data))
    for key in ("last_fetch",):
        if data.get(key):
            data[key] = local_utc(data[key])
    for row in data.get("servers") or []:
        for key in ("date", "error"):
            if row.get(key):
                row[key] = local_utc(row[key])
    return data


def local_utc(stamp: str) -> str:
    """Odoo's UTC time without a zone (2026-11-21T09:58:11) as this machine's."""
    return local_time(f"{stamp}+00:00") if "+" not in stamp and not stamp.endswith("Z") else local_time(stamp)
