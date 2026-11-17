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
def _patterns(language: str) -> list[tuple[re.Pattern, str]]:
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
        found.append((fixed, re.compile("".join(parts) + r"\Z", re.S), target))
    found.sort(key=lambda item: -item[0])
    return [(pattern, target) for _fixed, pattern, target in found]


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
    for pattern, target in _patterns(language):
        match = pattern.match(text)
        if match:
            try:
                # the values as they are: a server named test stays test
                return target.format(**match.groupdict())
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
