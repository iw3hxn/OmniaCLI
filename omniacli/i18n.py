# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The languages of the web interface (README, Languages).

English is the source: the templates and the code are written in it. A
catalog is a folder of JSON files, omniacli/i18n/<language>/*.json, each
{"English text": "its translation"}, merged; several files so that each
part of the interface keeps its own (no two hands on one file).

- In a template: {{ _("Log out") }}, with values {{ _("Restore into {key}",
  key=key) }} (the values are put in after the translation, and escaped as
  any value); in a script, {{ _("...") | tojson }}.
- What the code says (an error, a job's line) is translated where it is
  shown: tr(text) finds the text itself, else a pattern of the catalog
  whose {name} parts match any text ("No link {name}" turns "No link acme"
  into "Nessun link acme"). The code stays in English, and a text with no
  translation is shown as it is.

The language: the cookie omniacli_lang (the selector at the bottom of the
menu), else the browser's Accept-Language, else English."""

from __future__ import annotations

import functools
import json
import re
import string
from pathlib import Path

LANGUAGES = {"en": "English", "it": "Italiano"}
DEFAULT = "en"
COOKIE = "omniacli_lang"
CATALOGS = Path(__file__).with_name("i18n")
_FIELD = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


@functools.lru_cache(maxsize=None)
def catalog(language: str) -> dict[str, str]:
    """{English: translation} of a language, its files merged in name order."""
    merged: dict[str, str] = {}
    folder = CATALOGS / language
    if language == DEFAULT or not folder.is_dir():
        return merged
    for path in sorted(folder.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        merged.update({k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str) and v})
    return merged


@functools.lru_cache(maxsize=None)
def _patterns(language: str) -> list[tuple[re.Pattern, str, int]]:
    """The catalog's texts with {name} parts as regular expressions, the
    longest fixed text first: the most specific wins."""
    found = []
    for source, target in catalog(language).items():
        if not _FIELD.search(source):
            continue
        parts, names = [], []
        for literal, name, _spec, _conv in string.Formatter().parse(source):
            parts.append(re.escape(literal))
            if name is not None:
                if name in names:
                    parts.append(f"(?P={name})")
                else:
                    names.append(name)
                    parts.append(f"(?P<{name}>.+?)")
        fixed = len(_FIELD.sub("", source))
        found.append((fixed, re.compile("".join(parts) + r"\Z", re.S), target, _measured(source)))
    found.sort(key=lambda item: -item[0])
    return [(pattern, target, fixed, measured) for fixed, pattern, target, measured in found]


# a value the code writes with a dot that is a measure in the text ("{cap} GB", "in {seconds} s"): with the
# language's decimal mark; a version ("PostgreSQL {version}") is not a measure and stays as it is
_UNITS = re.compile(r"\s?(?:[kMGT]i?B|ms|s|%|min|h)\b")
_DECIMAL_COMMA = frozenset({"it", "de", "fr", "es", "pt", "nl"})
_ISO = re.compile(r"(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d)(?::\d\d(?:\.\d+)?)?")


def _measured(source: str) -> frozenset:
    """The {names} of source followed by a unit (a measure)."""
    out = set()
    for literal_after, (name, _) in zip([lit for lit, *_ in string.Formatter().parse(source)][1:] + [""],
                                         [(n, None) for _lit, n, *_ in string.Formatter().parse(source)]):
        if name and _UNITS.match(literal_after):
            out.add(name)
    return frozenset(out)


def _local(language: str, name: str, value: str, measured: frozenset) -> str:
    """A value as the language writes it: a date and time the code wrote as ISO (2026-11-22T16:37:36) as
    the pages write it (22/11/2026 16:37), a measure with the decimal comma where the language has it."""
    if language == DEFAULT:
        return value
    if _ISO.fullmatch(value):
        found = _ISO.fullmatch(value)
        return f"{found[3]}/{found[2]}/{found[1]} {found[4]}:{found[5]}"
    if name in measured and language in _DECIMAL_COMMA and re.fullmatch(r"\d+\.\d+", value):
        return value.replace(".", ",")
    return value


# a pattern of a few letters between its values ("{a} and {b}", "{n} of {max}", "on {host}") is for a label:
# a sentence it happens to match (a line of a job with no text of its own) stays whole, not half in each language
_LABEL_FIXED = 8
_LABEL_WORDS = 8


def gettext(language: str, text: str, **values) -> str:
    """text in language, with values put in its {name} parts."""
    translated = catalog(language).get(text, text)
    if not values:
        return translated
    try:
        return translated.format(**values)
    except (KeyError, IndexError, ValueError):
        return text.format(**values)


def tr(language: str, text: object) -> str:
    """A text the code made, in language: itself in the catalog, else the
    first pattern that matches it whole, else as it is. Each line of a text
    of several lines on its own."""
    if text is None:
        return ""
    text = str(text)
    if language == DEFAULT or not text:
        return text
    return _tr(language, text)


# a job's log repeats its lines' shapes: each text is matched once
@functools.lru_cache(maxsize=20000)
def _tr(language: str, text: str) -> str:
    if "\n" in text:
        return "\n".join(tr(language, line) for line in text.split("\n"))
    found = catalog(language).get(text)
    if found is not None:
        return found
    # a job's line: its time stays in front ("14:10:09 --- restart")
    stamp = re.match(r"(\d\d:\d\d:\d\d )(.*)\Z", text, re.S)
    if stamp:
        rest = tr(language, stamp.group(2))
        return stamp.group(1) + rest
    # a line another machine's job wrote, told here under its name ("  lab1: 14:10:09 The firewall…"): its own text
    relayed = re.match(r"(\s*[a-z0-9][a-z0-9-]*: \d\d:\d\d:\d\d )(.*)\Z", text, re.S)
    if relayed:
        return relayed.group(1) + tr(language, relayed.group(2))
    # the line a job fails with ("FAILED: the reason"): its reason is a sentence of its own, which a pattern of
    # a few letters is not applied to
    failed = re.match(r"(FAILED: )(.+)\Z", text, re.S)
    if failed and len(text.split()) > _LABEL_WORDS:
        head = catalog(language).get("FAILED: {error}", "FAILED: {error}").replace("{error}", "")
        return head + tr(language, failed.group(2))
    words = len(text.split())
    for pattern, target, fixed, measured in _patterns(language):
        if fixed <= _LABEL_FIXED and words > _LABEL_WORDS:
            continue
        match = pattern.match(text)
        if match:
            try:
                # the values as they are (a server named test stays test), but a value of several words
                # that is itself a text of the catalog: the reason of a refusal, said inside another line;
                # a whole sentence (more words than a label has) that the catalog has as a pattern, too:
                # the reason a job failed for, which names an instance or a machine ("FAILED: …")
                known = catalog(language)

                def inner(name: str, value: str) -> str:
                    if " " not in value:
                        return _local(language, name, value, measured)
                    if value in known:
                        return known[value]
                    return _tr(language, value) if len(value.split()) > _LABEL_WORDS else value
                return target.format(**{name: inner(name, value) for name, value in match.groupdict().items()})
            except (KeyError, IndexError, ValueError):
                return text
    return text


def choose(cookie: str | None, accept_language: str | None) -> str:
    """The language of a request: the cookie's, else the browser's first known one."""
    if cookie in LANGUAGES:
        return cookie
    for part in (accept_language or "").split(","):
        code = part.split(";", 1)[0].strip().lower()[:2]
        if code in LANGUAGES:
            return code
    return DEFAULT
