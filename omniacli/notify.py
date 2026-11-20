# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Who is told when a scheduled action fails or cannot start: email
addresses, through the SMTP server of the Options page, and webhook URLs,
which get a JSON {"text": ...} (a URL may hold a token: only its host
is ever written in a log) (Slack and Teams incoming webhooks,
Telegram's https://api.telegram.org/bot<token>/sendMessage?chat_id=<id>,
ntfy, a script of one's own).

A message that cannot be sent is written in OmniaCLI's log, never raised:
the job it tells of has already ended its own way."""

from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage
from urllib.parse import urlsplit

import httpx

from .config import Options

_logger = logging.getLogger(__name__)
TIMEOUT = 20


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


async def send(options: Options, recipients: list[str], subject: str, text: str) -> list[str]:
    """The message to each recipient; the problems, one line each (none: all sent)."""
    problems = []
    emails = [r for r in recipients if is_email(r)]
    if emails:
        if not options.smtp_host:
            problems.append("no SMTP server on the Options page: the emails are not sent")
        else:
            try:
                await asyncio.to_thread(_send_mail, options, emails, subject, text)
            except (OSError, smtplib.SMTPException) as exc:
                problems.append(f"email to {', '.join(emails)}: {exc}")
    hooks = [r for r in recipients if not is_email(r)]
    if hooks:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            for url in hooks:
                try:
                    resp = await client.post(url, json={"text": f"{subject}\n{text}"})
                    if resp.status_code >= 300:
                        problems.append(f"webhook to {urlsplit(url).hostname}: HTTP {resp.status_code}")
                except httpx.HTTPError as exc:
                    problems.append(f"webhook to {urlsplit(url).hostname}: {type(exc).__name__}")
    for problem in problems:
        _logger.warning("Notification not sent: %s", problem)
    return problems
