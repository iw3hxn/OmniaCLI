# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stagings, as Cloudpepper's: an instance of a workspace made as a copy of
another one's production (its parent), neutralized, on a date. Several may
live beside their parent; each is an instance of its own (its server, its
production with the copy, its test stopped), so everything an instance has
works on it, and deleting it is deleting that instance.

What is not in the servers file is kept here, next to it (stagings.json):
{server: {"parent": server, "created": ISO time, "backup": the zip's name}}."""

from __future__ import annotations

import datetime
import json
import os
import re
from pathlib import Path

FILE = "stagings.json"
# <parent's instance>-stg-<yyyymmdd>, -2, -3 on the same day
SUFFIX = "-stg-"


def load(path: Path) -> dict[str, dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}


def save(path: Path, data: dict[str, dict]) -> None:
    partial = path.with_name(path.name + ".part")
    partial.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(partial, path)


def instance_name(parent_instance: str, when: datetime.datetime, taken) -> str:
    """The new staging's instance name in its workspace (32 characters at most)."""
    stamp = when.strftime("%Y%m%d")
    base = f"{parent_instance[:32 - len(SUFFIX) - len(stamp) - 3].rstrip('-')}{SUFFIX}{stamp}"
    name, number = base, 2
    while name in taken:
        name, number = f"{base}-{number}", number + 1
    return name


def is_staging_name(instance: str) -> bool:
    return re.search(r"-stg-\d{8}(-\d+)?$", instance) is not None
