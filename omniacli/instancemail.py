# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The mail of a measured instance (AD-019 19.3, slice 3): sent and bounced
from Odoo's log (odoolog: ms, mb), received and not sent from its database,
by one fixed query every 5 minutes (composehelper.MAIL_QUERY), read only and
10 s at most, the way pgreport goes: the root helper's pg-mail for a compose
project (its own database service, the machine's cluster or a database on
another machine), psql as Odoo connects for a venv.

- Received: mail_message of message_type 'email' by 5-minute span of their
  create_date, read by id after a bound. The ids are not in the order of the
  commits: every read reads again the last 2 days (the bound is the top id of
  a read of 2 days before; the first reads find it by the primary key), and
  a span whose count changed is written again whole (the last line wins).
  In Odoo 16 an outgoing message is of type 'email' too: those with a row of
  mail_mail are not counted there (17 has 'email_outgoing').
- Not sent: mail_mail in state 'exception' by 5-minute span of their
  write_date, over the last 2 days, each counted by a fixed class of its
  failure (composehelper.MAIL_CLASSES); the reason itself never leaves the
  database.
- A staging's mail is held (UX-008 §5.3): its caught mail counted in its
  Mailpit over the 24 hours (stagingmail.held), the count only.

Only numbers are kept: no address, subject, body or error's text, in the
history ("mail" records) or in the cursor (mail.json, 600)."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from . import ops, pgreport
from .composehelper import MAIL_CLASSES, mail_query
from .jobs import JobFailed

_logger = logging.getLogger(__name__)

FILE = "mail.json"
SPAN = 300
REREAD = 2 * 86400  # the days read again at every read (mail_message's ids are not in the order of the commits)
TIMEOUT = 40  # the whole command (docker exec and psql); the query itself stops at 10 s
# why there is no count of the database: told as a sentence, never a zero
NO_MAIL = "no_mail"  # the module mail is not installed
UNREADABLE = "unreadable"
NOT_YET = "not_yet"
# the classes of a failure that are the mail server refusing or not answering: the cause says its reason
SMTP = ("smtp_auth", "smtp_connect", "smtp_refused")
DAY = 86400


class NoMail(Exception):
    """The database has no mail_message: the module mail is not installed."""


def answer(out: str) -> dict:
    """The query's JSON line; NoMail when the database has no mail."""
    if 'relation "mail_message" does not exist' in out or 'relation "mail_mail" does not exist' in out:
        raise NoMail()
    for line in out.splitlines():
        if line.startswith("{"):
            found = json.loads(line)
            if isinstance(found, dict):
                return found
    raise JobFailed(f"the database did not answer: {' | '.join(out.splitlines()[-2:])[:300]}")


async def ask(instance, from_id: int) -> dict:
    """The mail's counts of the instance's database from the id from_id (0: the bound found by the database)."""
    cmd, env = pgreport._command(instance, "pg-mail", str(int(from_id)))
    if env is None:
        code, out = await ops.capture(cmd, timeout=TIMEOUT)
    else:
        code, out = await ops.capture([*cmd, "-X", "-At", "-q", "-v", "ON_ERROR_STOP=1", "-c", mail_query(from_id)],
                                      env=env, timeout=TIMEOUT)
    if code:
        answer(out)  # NoMail when it is that
        raise JobFailed(f"the mail of the database cannot be read: {out[-300:]}")
    return answer(out)


def major_of(version) -> int | None:
    """The major version of Odoo of base's latest_version (19.0.1.3, saas~18.2.1.0)."""
    text = str(version or "").removeprefix("saas~")
    head = text.split(".", 1)[0]
    return int(head) if head.isdigit() else None


class InstanceMail:
    """Reads the mail's counts of an instance (one call of the queue of the log's reads, instancelog.InstanceLog) and
    writes those that changed to its history (kind "mail")."""

    def __init__(self, history, staging=None, held=None):
        # staging(key): whether the instance is a staging (its mail is held); held(instance): the caught mail of the
        # 24 hours, (count, more than the service lists), or None when it catches none
        self.history = history
        self.staging = staging or (lambda key: False)
        self.held = held

    def _path(self, key: str) -> Path:
        return self.history.folder(key) / FILE

    def load(self, key: str) -> dict:
        try:
            found = json.loads(self._path(key).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return found if isinstance(found, dict) else {}

    def save(self, key: str, cursor: dict) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(cursor, out, separators=(",", ":"))
        os.replace(tmp, path)

    def state(self, key: str) -> dict:
        """What the page tells of the last read: {at, state (None: read; NO_MAIL, UNREADABLE, NOT_YET), failed_now,
        held, held_more, staging}."""
        found = self.load(key)
        return {"at": found.get("at"), "state": found.get("state", NOT_YET) if found else NOT_YET,
                "failed_now": found.get("failed_now"), "held": found.get("held"), "held_more": found.get("held_more"),
                "first": found.get("first"), "staging": bool(self.staging(key))}

    async def read(self, instance, now: float | None = None) -> None:
        key = instance.key
        now = now if now is not None else time.time()
        cursor = self.load(key)
        marks = [m for m in cursor.get("marks") or [] if isinstance(m, list) and len(m) == 2]
        # the bound: the top id of the newest read of 2 days before or more
        old = [m for m in marks if m[0] <= now - REREAD]
        from_id, from_time = (int(old[-1][1]), old[-1][0]) if old else (0, now - REREAD)
        found_held = None
        if self.staging(key) and self.held is not None:
            try:
                found_held = await self.held(instance, now - DAY)
            except Exception as exc:  # noqa: BLE001 - its caught mail not counted: said as not counted, never 0
                _logger.info("monitor: the caught mail of %s is not counted: %s", key, exc)
        held = {"held": found_held[0], "held_more": found_held[1]} if found_held else {"held": None, "held_more": None}
        try:
            found = await ask(instance, from_id)
        except NoMail:
            self.save(key, {**cursor, "at": int(now), "state": NO_MAIL, **held})
            return
        except (JobFailed, OSError, ValueError) as exc:
            _logger.info("monitor: the mail of %s is not read: %s", key, str(exc)[:200])
            self.save(key, {**cursor, "at": int(now), "state": UNREADABLE, **held})
            return
        major = major_of(found.get("version"))
        # Odoo 16: an outgoing message is of type email too, those with a row of mail_mail are not received
        received = {int(r[0]): int(r[2] if major is not None and major <= 16 else r[1])
                    for r in found.get("received") or [] if isinstance(r, list) and len(r) == 3}
        failed: dict[int, dict] = {}
        for t, kind, n in (r for r in found.get("failed") or [] if isinstance(r, list) and len(r) == 3):
            kind = kind if kind in MAIL_CLASSES else "other"
            failed.setdefault(int(t), {})[kind] = failed.get(int(t), {}).get(kind, 0) + int(n)
        # the spans read whole: from the first span after the bound's time (received), after now - 2 days (failed)
        first_r = -(-int(from_time) // SPAN) * SPAN
        first_f = -(-int(now - REREAD) // SPAN) * SPAN
        kept_r = {int(t): n for t, n in (cursor.get("received") or {}).items()}
        kept_f = {int(t): v for t, v in (cursor.get("failed") or {}).items()}
        records = []
        for t in sorted(set(received) | set(failed) | {t for t in kept_r if t >= first_r}
                        | {t for t in kept_f if t >= first_f}):
            r = received.get(t, 0) if t >= first_r else kept_r.get(t, 0)
            f = failed.get(t, {}) if t >= first_f else kept_f.get(t, {})
            if r == kept_r.get(t, 0) and f == kept_f.get(t, {}):
                continue
            records.append({"t": t, "y": "mail", "mr": r, "mx": sum(f.values()), "mc": f})
            kept_r[t], kept_f[t] = r, f
        if records:
            self.history.add(key, records, now)
        keep = now - REREAD - DAY
        top = int(found.get("top") or 0)
        marks = [m for m in marks if m[0] >= now - REREAD - DAY] + [[int(now), top]]
        self.save(key, {"at": int(now), "state": None, "major": major, "failed_now": int(found.get("failed_now") or 0),
                        **held, "marks": marks, "first": cursor.get("first") or int(now),
                        "received": {str(t): n for t, n in kept_r.items() if t >= keep and n},
                        "failed": {str(t): v for t, v in kept_f.items() if t >= keep and v}})


# --- what the page, REST and MCP tell -----------------------------------------------------------------------------

def _classes(points: list[dict]) -> dict:
    found: dict[str, int] = {}
    for p in points:
        for kind, n in (p.get("mc") or {}).items():
            found[kind] = found.get(kind, 0) + int(n)
    return {k: v for k, v in found.items() if v}


def main_reason(classes: dict) -> str | None:
    """The class of most failures (the mail server's first when as many)."""
    if not classes:
        return None
    order = {k: i for i, k in enumerate(MAIL_CLASSES)}
    return sorted(classes.items(), key=lambda kv: (-kv[1], order.get(kv[0], 99)))[0][0]


def mail_causes(points: list[dict], state: dict, now: float) -> list[dict]:
    """The cause «Posta» (UX-018 §3): at least 1 e-mail not sent in the last 24 hours, with its main reason; none for
    a staging (its mail is held on purpose) or when the database is not read."""
    if state.get("staging") or state.get("state") is not None:
        return []
    day = [p for p in points if p["t"] >= now - DAY]
    count = sum(int(p.get("mx") or 0) for p in day)
    if not count:
        return []
    classes = _classes(day)
    reason = main_reason(classes)
    return [{"kind": "mail", "count": count, "reason": reason, "smtp": reason in SMTP, "reasons": classes,
             "hours": 24, "section": "mail"}]


def _width(period: str) -> int:
    return {"1h": SPAN, "24h": 3600}.get(period, DAY)


def _start(t: int, width: int, now: float) -> int:
    """The start of the span of t: by 5 minutes, by hour, or by day of this machine's clock."""
    if width < DAY:
        return t // width * width
    offset = time.localtime(now).tm_gmtoff
    return (t + offset) // DAY * DAY - offset


def mail_view(log: list[dict], points: list[dict], state: dict, period: str, seconds: int, now: float,
              log_state: str | None) -> dict:
    """The section «Posta» over the period: sent and bounced (from the log; None with log_source when the log is not
    read), received and not sent (from the database; None with source when it is not read), their points by 5
    minutes (1 hour), by hour (24 hours) or by day, the reasons of the failures by class, and a staging's held."""
    since = now - seconds
    source = state.get("state")
    log_source = log_state if log_state in ("no_file", "unreadable", "not_yet") else None
    found_log = [p for p in log if p["t"] >= since]
    found = [p for p in points if p["t"] >= since]
    width = _width(period)
    spans: dict[int, dict] = {}

    def span(t: int) -> dict:
        start = _start(t, width, now)
        return spans.setdefault(start, {"t": start, "sent": 0, "failed": 0, "received": 0, "bounced": 0})

    if log_source is None:
        for p in found_log:
            if p.get("ms") or p.get("mb"):
                s = span(p["t"])
                s["sent"] += int(p.get("ms") or 0)
                s["bounced"] += int(p.get("mb") or 0)
    if source is None:
        for p in found:
            s = span(p["t"])
            s["received"] += int(p.get("mr") or 0)
            s["failed"] += int(p.get("mx") or 0)
    shown_db = source is None
    shown_log = log_source is None
    total = {"sent": sum(int(p.get("ms") or 0) for p in found_log) if shown_log else None,
             "bounced": sum(int(p.get("mb") or 0) for p in found_log) if shown_log else None,
             "received": sum(int(p.get("mr") or 0) for p in found) if shown_db else None,
             "failed": sum(int(p.get("mx") or 0) for p in found) if shown_db else None}
    # every span of the period drawn, a 0 where nothing was sent or received, from the first measure on
    firsts = [p["t"] for p in log[:1]] + [p["t"] for p in points[:1]] + [int(state.get("first") or now)]
    if (shown_log and log) or (shown_db and state.get("at")):
        first = max(int(since), min(firsts))
        t = _start(first, width, now)
        while t <= now:
            span(t)
            t += width
    return {"source": source, "log_source": log_source, "read_at": state.get("at"), "staging": state.get("staging"),
            "held": state.get("held"), "held_more": state.get("held_more"),
            "failed_now": state.get("failed_now") if shown_db else None, "total": total, "width": width,
            "reasons": _classes(found) if shown_db else {},
            # a source not read is no line in the charts, never a line at 0
            "points": [{**spans[t], **({} if shown_log else {"sent": None, "bounced": None}),
                        **({} if shown_db else {"received": None, "failed": None})} for t in sorted(spans)]}
