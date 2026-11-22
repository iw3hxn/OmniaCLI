# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Who is told when a scheduled action fails or cannot start, when the
monitor finds something (monitor.py) or a job fails (opt-in): email
addresses, through the SMTP server of the Options page, in the language of
Options › Monitoring; and webhook URLs (a URL may hold a token: only its
host is ever written in a log) (Slack and Teams incoming webhooks,
Telegram's https://api.telegram.org/bot<token>/sendMessage?chat_id=<id>,
ntfy, a script of one's own).

A webhook gets a JSON in English: {"text": subject and detail, as Slack
reads it, "event": a stable name (EVENTS), "level": info, warning or
critical, "machine", "time", "subject", "detail", and the event's own
fields (instance, job...)}. When Options › Monitoring gave the URL a secret,
the body is signed: X-OmniaCLI-Signature-256: sha256=<HMAC-SHA256 of the
body with the secret, hex>, as GitHub signs its own; X-OmniaCLI-Event
names the event.

A message that cannot be sent is written in OmniaCLI's log, never raised:
the job it tells of has already ended its own way."""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import hmac
import json
import logging
import re
import smtplib
import socket
import ssl
from email.message import EmailMessage
from urllib.parse import urlsplit

import httpx

from . import i18n
from .config import Options

_logger = logging.getLogger(__name__)
TIMEOUT = 20
LEVELS = ("info", "warning", "critical")
# the events a webhook may get: their names do not change
EVENTS = ("instance.down", "instance.up", "instance.restarted", "instance.restart_failed",
          "instance.restart_held", "machine.threshold", "machine.threshold.cleared", "certificate.ending",
          "schedule.failed", "job.failed", "job.warned", "notice.test", "notice")
SIGNATURE_HEADER = "X-OmniaCLI-Signature-256"
EVENT_HEADER = "X-OmniaCLI-Event"


def is_email(address: str) -> bool:
    return "@" in address and not address.startswith("https://")


def _send_mail(options: Options, to: list[str], subject: str, text: str) -> None:
    message = EmailMessage()
    message["From"] = options.mail_from or options.smtp_user
    message["To"] = ", ".join(to)
    message["Subject"] = subject
    message.set_content(text)
    context = ssl.create_default_context()
    if options.smtp_security == "ssl":
        server = smtplib.SMTP_SSL(options.smtp_host, options.smtp_port, timeout=TIMEOUT, context=context)
    else:
        server = smtplib.SMTP(options.smtp_host, options.smtp_port, timeout=TIMEOUT)
    with server:
        if options.smtp_security == "starttls":
            server.starttls(context=context)
        if options.smtp_user:
            server.login(options.smtp_user, options.smtp_password)
        server.send_message(message)


_SUBJECT = re.compile(r"(OmniaCLI [^:\s]+: )(?:\[(info|warning|critical)\] )?(.*)\Z", re.S)


def said(language: str, text: str) -> str:
    """text in language, line by line (a notice's detail has several); a
    subject's head (OmniaCLI <machine>: [level]) kept, its words said."""
    head = _SUBJECT.match(text)
    if head and "\n" not in text:
        level = f"[{i18n.tr(language, head[2])}] " if head[2] else ""
        return head[1] + level + i18n.tr(language, head[3])
    return "\n".join(i18n.tr(language, line) for line in text.split("\n"))


def payload(subject: str, text: str, event: dict | None) -> dict:
    """The JSON a webhook gets (English)."""
    event = dict(event or {})
    name = str(event.pop("event", "") or "notice")
    level = str(event.pop("level", "") or "warning")
    return {"text": f"{subject}\n{text}", "event": name, "level": level if level in LEVELS else "warning",
            "machine": socket.gethostname(), "time": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "subject": subject, "detail": text, **event}


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def send(options: Options, recipients: list[str], subject: str, text: str, event: dict | None = None,
               language: str = "") -> list[str]:
    """The message to each recipient; the problems, one line each (none: all
    sent). event: {"event": a name of EVENTS, "level": one of LEVELS, and
    its own fields} for the webhooks' JSON; language: of the emails (empty:
    Options' notice_language; a text already in its reader's language is
    left as it is)."""
    problems = []
    emails = [r for r in recipients if is_email(r)]
    if emails:
        if not options.smtp_host:
            problems.append("no SMTP server on the Options page: the emails are not sent")
        else:
            language = language or getattr(options, "notice_language", "en") or "en"
            try:
                await asyncio.to_thread(_send_mail, options, emails, said(language, subject), said(language, text))
            except (OSError, smtplib.SMTPException) as exc:
                problems.append(f"email to {', '.join(emails)}: {exc}")
    hooks = [r for r in recipients if not is_email(r)]
    if hooks:
        body = json.dumps(payload(subject, text, event), ensure_ascii=False).encode()
        name = json.loads(body)["event"]
        keys = getattr(options, "webhook_keys", None) or {}
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            for url in hooks:
                headers = {"Content-Type": "application/json", EVENT_HEADER: name}
                secret = (keys.get(url) or {}).get("secret")
                if secret:
                    headers[SIGNATURE_HEADER] = sign(secret, body)
                try:
                    resp = await client.post(url, content=body, headers=headers)
                    if resp.status_code >= 300:
                        problems.append(f"webhook to {urlsplit(url).hostname}: HTTP {resp.status_code}")
                except httpx.HTTPError as exc:
                    problems.append(f"webhook to {urlsplit(url).hostname}: {type(exc).__name__}")
    for problem in problems:
        _logger.warning("Notification not sent: %s", problem)
    return problems
