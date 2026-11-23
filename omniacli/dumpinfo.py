# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What an Odoo database brought from outside holds, read before it is
restored: its Odoo version and its installed modules, from the table
ir_module_module of its dump (the dump.sql of a backup zip, as odoo db dump
and Odoo's database manager make it, or a plain SQL dump of pg_dump), and
its manifest.json when it has one. And a plain SQL dump made a backup zip,
which every Odoo restores (odoo db load), for a plugin that restores zips
only.

Nothing here runs the dump: its text is only read, line by line, as far as
the end of the table of the modules."""

from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path

from . import sqldump

# the states of a module whose code the database expects to find
INSTALLED = ("installed", "to upgrade", "to remove")
_COPY = re.compile(r"^COPY (?:\"?public\"?\.)?\"?ir_module_module\"? \((?P<columns>[^)]*)\) FROM stdin;")
_MAJOR = re.compile(r"(?:saas~)?(\d+)")


class DumpError(Exception):
    """The file is not a database that can be read."""


def _unescape(value: str) -> str | None:
    """A field of COPY's text format: \\N is NULL, backslash escapes."""
    if value == r"\N":
        return None
    if "\\" not in value:
        return value
    return re.sub(r"\\(.)", lambda m: {"t": "\t", "n": "\n", "r": "\r", "\\": "\\"}.get(m.group(1), m.group(1)),
                  value)


def modules_of(lines) -> tuple[dict[str, tuple[str, str | None]] | None, int]:
    """({module: (state, latest_version)} of the COPY of ir_module_module in
    lines, None when the dump has none (an INSERT dump, say); the lines read)."""
    count, lines = 0, iter(lines)
    for line in lines:
        count += 1
        found = _COPY.match(line)
        if not found:
            continue
        columns = [c.strip().strip('"') for c in found.group("columns").split(",")]
        try:
            name_at, state_at = columns.index("name"), columns.index("state")
        except ValueError:
            return None, count
        version_at = columns.index("latest_version") if "latest_version" in columns else None
        modules: dict[str, tuple[str, str | None]] = {}
        for row in lines:
            count += 1
            row = row.rstrip("\n")
            if row == "\\.":
                break
            fields = row.split("\t")
            if len(fields) != len(columns):
                continue
            name = _unescape(fields[name_at])
            if name:
                modules[name] = (_unescape(fields[state_at]) or "",
                                 _unescape(fields[version_at]) if version_at is not None else None)
        return modules, count
    return None, count


VECTOR = "vector"
_EXTENSION = re.compile(r'^CREATE EXTENSION (?:IF NOT EXISTS )?"?([a-z][a-z0-9_]{0,62})"?')
# pg_dump writes the extensions before the first table: no further is read
_EXTENSIONS_END = ("CREATE TABLE ", "COPY ")
_EXTENSIONS_LINES = 20000


def extensions_of(lines) -> set[str]:
    """The PostgreSQL extensions a dump makes (CREATE EXTENSION), read up to
    its first table."""
    found = set()
    for number, line in enumerate(lines):
        if line.startswith(_EXTENSIONS_END) or number >= _EXTENSIONS_LINES:
            break
        match = _EXTENSION.match(line)
        if match:
            found.add(match.group(1))
    return found


def extensions(path: Path) -> set[str]:
    """extensions_of the dump.sql of a backup zip or of a plain SQL dump;
    DumpError when it is neither. vector: Odoo Enterprise's ai module, whose
    tables a PostgreSQL without pgvector cannot hold."""
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as zf:
                if "dump.sql" not in zf.namelist():
                    raise DumpError(f"{path.name} is not an Odoo backup: it has no dump.sql")
                with zf.open("dump.sql") as raw:
                    return extensions_of(io.TextIOWrapper(raw, encoding="utf-8", errors="replace"))
        with open(path, encoding="utf-8", errors="replace") as text:
            return extensions_of(text)
    except (OSError, zipfile.BadZipFile) as exc:
        raise DumpError(f"{path.name} cannot be read: {exc}") from exc


def _major(text) -> int | None:
    found = _MAJOR.match(str(text or ""))
    return int(found.group(1)) if found else None


def read(path: Path) -> dict:
    """{kind: zip or sql, database, major, version, modules (installed,
    sorted, None: not readable), size (what it holds unpacked), filestore
    (whether a zip has one)}; DumpError when it is neither."""
    if zipfile.is_zipfile(path):
        try:
            with zipfile.ZipFile(path) as zf:
                names = set(zf.namelist())
                if "dump.sql" not in names:
                    raise DumpError(f"{path.name} is not an Odoo backup: it has no dump.sql")
                manifest = {}
                if "manifest.json" in names:
                    try:
                        manifest = json.loads(zf.read("manifest.json"))
                    except ValueError:
                        manifest = {}
                size = sum(i.file_size for i in zf.infolist())
                with zf.open("dump.sql") as raw:
                    found, _ = modules_of(io.TextIOWrapper(raw, encoding="utf-8", errors="replace"))
                filestore = any(n.startswith("filestore/") for n in names)
        except (OSError, zipfile.BadZipFile) as exc:
            raise DumpError(f"{path.name} is not a zip that can be read: {exc}") from exc
        kind = "zip"
    else:
        problem = sqldump.problem(path)
        if problem:
            raise DumpError(problem)
        manifest, filestore, kind = {}, False, "sql"
        size = path.stat().st_size
        with open(path, encoding="utf-8", errors="replace") as text:
            found, _ = modules_of(text)
    manifest = manifest if isinstance(manifest, dict) else {}
    base_version = (found or {}).get("base", ("", None))[1]
    major = _major(base_version) or _major(manifest.get("major_version") or manifest.get("version"))
    modules = None if found is None else sorted(m for m, (state, _) in found.items() if state in INSTALLED)
    if modules is None and isinstance(manifest.get("modules"), dict):
        # Odoo's own manifest names the installed modules too
        modules = sorted(str(m) for m in manifest["modules"])
    return {"kind": kind, "database": str(manifest.get("db_name") or "") or None, "major": major,
            "version": base_version or manifest.get("version"), "modules": modules, "size": size,
            "filestore": filestore, "extensions": sorted(extensions(path))}


def wrap_sql(sql: Path, dest: Path, major: int | None, database: str | None = None) -> Path:
    """The plain SQL dump sql as a backup zip at dest: its dump.sql and a
    manifest.json saying its Odoo version, no filestore (odoo db load takes
    it so: a database without its attachments)."""
    manifest = {"odoo_dump": "1", "db_name": database or sql.stem, "version": f"{major}.0" if major else "",
                "major_version": f"{major}.0" if major else ""}
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=1))
        zf.write(sql, "dump.sql")
    return dest
