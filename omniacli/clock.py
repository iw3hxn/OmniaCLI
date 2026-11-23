# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Dates and times (README «Dates and times»): what OmniaCLI writes and what people read.

OmniaCLI writes a moment as the clock of the machine it runs on shows it, a naive ISO
(datetime.now().isoformat(): 2026-11-23T08:59:12), in its jobs, schedules, stagings, backups
and lists; those strings stay as they are (no migration: the code that reads them, and an
older OmniaCLI at the other end of a link, keep working). What is added is the machine's
time zone, said with what it serves:

- every answer carries it in the header X-OmniaCLI-Zone, every page in window.serverZone:
  the page converts a naive moment from that zone to the reader's (window.whenText);
- the zone is a name of the IANA database (Europe/Rome, UTC), so a moment is converted
  with the offset it had (summer or winter time), not the offset of now; a machine whose
  zone has no such name says its offset of now (+01:00);
- a list this machine makes of what its links answered (Instances, Backups, the map, the
  bell) moves each link's moments onto this machine's clock first (to_here), so one page
  converts all its rows with one zone, and Python compares them as they are;
- a text the code writes (a job's line, a refusal) with a moment in it is told in the
  reader's zone (for_viewer): the browser says it in the cookie omnia_zone, a link passes
  it on in X-OmniaCLI-Viewer-Zone.

A naive moment of the hour that comes twice when the clocks go back is read as the first
(fold 0), as zoneinfo does; one of the hour that does not exist when they go forward, with
the offset before the change (03:30 of summer time for 02:30)."""

from __future__ import annotations

import contextvars
import datetime
import functools
import os
import re
import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# the zone of the machine that answered (an answer's header) and the reader's (a link passes it on)
HEADER = "X-OmniaCLI-Zone"
VIEWER_HEADER = "X-OmniaCLI-Viewer-Zone"
# the reader's zone as the browser knows it (Intl), written by every page (base.html)
COOKIE = "omnia_zone"
# this machine's zone, and the reader's, while a request is served (app.py's middleware)
HERE: contextvars.ContextVar[str] = contextvars.ContextVar("omnia_here_zone", default="")
VIEWER: contextvars.ContextVar[str] = contextvars.ContextVar("omnia_viewer_zone", default="")

_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_+-]*(?:/[A-Za-z0-9_+-]+){0,2}")
_OFFSET = re.compile(r"([+-])(\d\d):(\d\d)")
_UTC_NAMES = frozenset({"UTC", "Etc/UTC", "Etc/UCT", "UCT", "Universal", "Etc/Universal", "Zulu", "Etc/Zulu",
                        "GMT", "Etc/GMT", "Etc/GMT0", "Etc/GMT+0", "Etc/GMT-0", "Etc/Greenwich", "Greenwich"})
# a moment as OmniaCLI writes it: 2026-11-23T08:59[:12[.123456]], T or a space; an offset or Z after it: aware
MOMENT = re.compile(r"(\d{4})-(\d\d)-(\d\d)([T ])(\d\d):(\d\d)(?::(\d\d)(\.\d{1,6})?)?(Z|[+-]\d\d:?\d\d)?")
_WHOLE_NAIVE = re.compile(MOMENT.pattern[:MOMENT.pattern.rindex("(Z|")] + r"\Z")
_LOCALTIME = Path("/etc/localtime")
_TIMEZONE_FILE = Path("/etc/timezone")


def tzinfo_of(zone: str | None) -> datetime.tzinfo | None:
    """The tzinfo of a zone as OmniaCLI says it (an IANA name, UTC, or ±hh:mm); None: not one."""
    zone = (zone or "").strip()
    if not zone:
        return None
    found = _OFFSET.fullmatch(zone)
    if found:
        sign, hours, minutes = found.groups()
        if int(hours) > 18 or int(minutes) > 59:
            return None
        delta = datetime.timedelta(hours=int(hours), minutes=int(minutes))
        return datetime.timezone(-delta if sign == "-" else delta)
    if zone in _UTC_NAMES:
        return datetime.timezone.utc
    if not _NAME.fullmatch(zone) or ".." in zone:
        return None
    try:
        return ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


def is_zone(zone: str | None) -> bool:
    return tzinfo_of(zone) is not None


def offset_text(seconds: int) -> str:
    sign = "-" if seconds < 0 else "+"
    minutes = abs(seconds) // 60
    return f"{sign}{minutes // 60:02d}:{minutes % 60:02d}"


def _candidates() -> list[str]:
    """The names this machine's zone may have, the one Python's clock follows first: TZ, else the system's."""
    tz = os.environ.get("TZ")
    if tz is not None:
        tz = tz.lstrip(":")
        return [tz.split("zoneinfo/", 1)[1] if "zoneinfo/" in tz else tz] if tz else ["UTC"]
    names = []
    try:
        names.append(_TIMEZONE_FILE.read_text().split()[0])
    except (OSError, IndexError):
        pass
    try:
        target = os.readlink(_LOCALTIME)
        if "zoneinfo/" in target:
            names.append(target.split("zoneinfo/", 1)[1])
    except OSError:
        pass
    return names


def _agrees(zone: datetime.tzinfo) -> bool:
    """Whether zone gives the offsets the machine's clock has now and in half a year (its summer and winter)."""
    now = time.time()
    for when in (now, now + 182 * 86400):
        offset = zone.utcoffset(datetime.datetime.fromtimestamp(when, zone))
        if offset is None or int(offset.total_seconds()) != time.localtime(when).tm_gmtoff:
            return False
    return True


@functools.lru_cache(maxsize=1)
def machine_zone() -> str:
    """This machine's zone as a browser's Intl reads it: its IANA name (Europe/Rome; UTC for
    every name of UTC) when it is known and agrees with the clock OmniaCLI writes by, else
    the offset of now (+01:00). Read once: a zone changed on the machine needs a restart."""
    for name in _candidates():
        zone = tzinfo_of(name)
        if zone is not None and _agrees(zone):
            return "UTC" if zone is datetime.timezone.utc else name
    return offset_text(time.localtime().tm_gmtoff)


def here() -> str:
    """The zone of the machine serving the request (HERE), else of this process."""
    return HERE.get() or machine_zone()


def aware(moment: datetime.datetime, zone: str | None) -> datetime.datetime | None:
    """A naive moment of zone's clock as an aware one (fold 0: the first of an hour that comes twice; in an
    hour that does not exist, the offset before the change). None: no such zone."""
    if moment.tzinfo is not None:
        return moment
    tz = tzinfo_of(zone)
    return moment.replace(tzinfo=tz, fold=0) if tz is not None else None


def _parse(found: re.Match) -> datetime.datetime:
    year, month, day, _, hour, minute, second, fraction = found.groups()[:8]
    micro = int((fraction or ".0")[1:].ljust(6, "0"))
    return datetime.datetime(int(year), int(month), int(day), int(hour), int(minute), int(second or 0), micro)


def _written(moment: datetime.datetime, like: re.Match) -> str:
    """moment written in the shape of the match: its separator, its seconds and fraction if it had them."""
    _, _, _, sep, _, _, second, fraction = like.groups()[:8]
    text = moment.strftime(f"%Y-%m-%d{sep}%H:%M")
    if second is not None:
        text += moment.strftime(":%S")
        if fraction is not None:
            text += "." + f"{moment.microsecond:06d}"[:len(fraction) - 1]
    return text


def moved(text: str, there: str | None, onto: str | None) -> str:
    """A naive moment of there's clock written as onto's clock shows it, in the same shape
    (anything else, or a zone not known: as it is)."""
    found = _WHOLE_NAIVE.fullmatch(text) if isinstance(text, str) else None
    if found is None:
        return text
    source, target = tzinfo_of(there), tzinfo_of(onto)
    if source is None or target is None:
        return text
    try:
        moment = _parse(found).replace(tzinfo=source, fold=0)
        return _written(moment.astimezone(target).replace(tzinfo=None), found)
    except (ValueError, OverflowError):
        return text


def to_here(data, there: str | None, onto: str | None = None):
    """What a link answered (JSON: dicts, lists, strings), its naive moments moved from its
    zone (there: its X-OmniaCLI-Zone) onto this machine's clock (onto, else here()), so a
    list of this machine's rows and a link's says every moment on one clock. A link that
    says no zone (an older OmniaCLI): as it answered, read as this machine's."""
    onto = onto or here()
    if not there or there == onto or tzinfo_of(there) is None:
        return data
    return _walk(data, there, onto)


def _walk(data, there: str, onto: str):
    if isinstance(data, str):
        return moved(data, there, onto)
    if isinstance(data, list):
        return [_walk(item, there, onto) for item in data]
    if isinstance(data, dict):
        return {key: _walk(value, there, onto) for key, value in data.items()}
    return data


def answered_here(resp, data, onto: str | None = None):
    """data (what resp, a link's answer, said as JSON) on this machine's clock: to_here with resp's zone."""
    return to_here(data, resp.headers.get(HEADER), onto)


def viewer() -> str:
    """The reader's zone while a request is served ("" when it is not known)."""
    return VIEWER.get()


def for_viewer(text: str, onto: str | None = None) -> str:
    """A text the code wrote, its moments in the reader's zone: a naive one is of this
    machine's clock, one with an offset or Z is converted as it says. The same shape (the
    language writes it its own way after: i18n._local). No reader's zone, or the same as
    this machine's: as it is."""
    onto = onto if onto is not None else viewer()
    if not onto or not isinstance(text, str) or "-" not in text or ":" not in text:
        return text
    source = here()
    if onto == source and not re.search(r"\d\d(?::\d\d)?(?:Z|[+-]\d\d:?\d\d)", text):
        return text
    target = tzinfo_of(onto)
    if target is None:
        return text

    def one(found: re.Match) -> str:
        offset = found.group(9)
        try:
            moment = _parse(found)
            if offset:
                if offset == "Z":
                    tz = datetime.timezone.utc
                else:
                    digits = offset.replace(":", "")
                    tz = tzinfo_of(f"{digits[0]}{digits[1:3]}:{digits[3:5]}")
                moment = moment.replace(tzinfo=tz)
            else:
                moment = aware(moment, source)
            if moment is None:
                return found.group(0)
            return _written(moment.astimezone(target).replace(tzinfo=None), found)
        except (ValueError, OverflowError):
            return found.group(0)
    return MOMENT.sub(one, text)


def zone_label(zone: str | None = None) -> str:
    """A zone as people read it next to a time: its name (UTC, Europe/Rome) or its offset (UTC+01:00)."""
    zone = zone or here()
    return f"UTC{zone}" if _OFFSET.fullmatch(zone) else zone


def shown(iso, zone: str | None = None) -> str:
    """A moment of this machine's clock as a page writes it, dd/mm/yyyy hh:mm, in the reader's zone when the
    request says it (zone: another reader's), else by this machine's clock; anything else as it is. The
    templates' filter moment."""
    text = "" if iso is None else str(iso)
    found = MOMENT.match(text)
    if found is None:
        return text
    moment = MOMENT.match(for_viewer(found.group(0), zone))
    return f"{moment[3]}/{moment[2]}/{moment[1]} {moment[5]}:{moment[6]}"


def epoch_text(ts: float, reader: bool = True) -> str:
    """A moment given in seconds since the epoch as a page writes it, dd/mm/yyyy hh:mm: in the reader's
    zone when the request says it (reader), else by this machine's clock (a text kept in a log)."""
    zone = tzinfo_of(viewer()) if reader else None
    if zone is None:
        return time.strftime("%d/%m/%Y %H:%M", time.localtime(ts))
    return datetime.datetime.fromtimestamp(ts, zone).strftime("%d/%m/%Y %H:%M")
