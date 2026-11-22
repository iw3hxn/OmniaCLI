# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Odoo modules uploaded as a .zip into an instance of a workspace (README,
Modules uploaded as a .zip): the zip checked before anything is written,
its modules unpacked into a folder of each role's addons/ that is in the
addons path as a repository is (/mnt/customer-addons/omniacli-uploads,
workspaceops.write_conf), a record of who uploaded what beside them.

The checks: a zip, at most MAX_ZIP bytes, MAX_UNPACKED unpacked and
MAX_FILES entries, none encrypted; no name that is absolute, holds .. or a
backslash, no link, no special file; one or more modules, each a folder
with its __manifest__.py, at the top of the zip or in the one folder at its
top (GitHub's "Download ZIP"); a manifest read with ast.literal_eval, never
run, installable; a module's version of another series of Odoo is said.
What is outside the modules' folders (a README at the top) is not
unpacked."""

from __future__ import annotations

import ast
import datetime
import json
import os
import re
import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath

FOLDER = "omniacli-uploads"
RECORD = ".omniacli-uploads.json"
MAX_ZIP = 50 << 20
MAX_UNPACKED = 200 << 20
MAX_FILES = 5000
MAX_MANIFEST = 100 << 10
MODULE = re.compile(r"[a-z0-9][a-z0-9_]{0,99}")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ ()-]{0,150}\.zip")
_SERIES_VERSION = re.compile(r"(\d{1,2}\.\d)\.\d+(?:\.\d+)*")


class Refused(ValueError):
    """Why the zip is not taken, said to the person."""


def _entry_problem(info: zipfile.ZipInfo) -> str | None:
    name = info.filename
    if "\\" in name or name.startswith("/") or re.match(r"[A-Za-z]:", name) or "\0" in name:
        return f"{name!r} is not a path inside the zip"
    if any(part in ("..", ".") for part in PurePosixPath(name).parts) or name.startswith("./"):
        return f"{name!r} goes out of its folder (..)"
    mode = info.external_attr >> 16
    if stat.S_ISLNK(mode):
        return f"{name} is a symbolic link: links are not taken"
    # many zip makers write the permissions only, without the kind of file
    if stat.S_IFMT(mode) and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
        return f"{name} is not a file nor a folder"
    if info.flag_bits & 0x1:
        return f"{name} is encrypted"
    return None


def read_manifest(text: bytes, module: str) -> dict:
    """The manifest's dict, read as a literal, never run; or why not."""
    try:
        data = ast.literal_eval(text.decode("utf-8"))
    except (ValueError, SyntaxError, UnicodeDecodeError, MemoryError, RecursionError):
        raise Refused(f"The __manifest__.py of {module} is not a plain dictionary: it is read, never run") from None
    if not isinstance(data, dict):
        raise Refused(f"The __manifest__.py of {module} is not a dictionary")
    if data.get("installable", True) is False:
        raise Refused(f"{module} says it is not installable (installable: False)")
    return data


def series_warning(module: str, version: str, series: str | None) -> str | None:
    """A module's version of another series of Odoo than the instance's: said."""
    found = _SERIES_VERSION.fullmatch(version or "")
    if series is None or found is None or found[1] == series:
        return None
    return (f"{module}: its version {version} is for Odoo {found[1]}, this instance runs Odoo {series}: "
            "it may not install")


def check(path: Path, series: str | None) -> dict:
    """What the zip holds, checked: {modules: [{module, name, version,
    summary, depends, warning}], base, files, size}; Refused: why not."""
    if path.stat().st_size > MAX_ZIP:
        raise Refused(f"The zip is larger than {MAX_ZIP >> 20} MB")
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        raise Refused("The file is not a zip") from None
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_FILES:
            raise Refused(f"The zip holds {len(infos)} entries: at most {MAX_FILES}")
        size = 0
        for info in infos:
            problem = _entry_problem(info)
            if problem:
                raise Refused(problem)
            size += info.file_size
        if size > MAX_UNPACKED:
            raise Refused(f"The zip unpacks to {size >> 20} MB: at most {MAX_UNPACKED >> 20} MB")
        names = [i.filename for i in infos]
        if "__manifest__.py" in names:
            raise Refused("The __manifest__.py is at the top of the zip: zip the module's folder, whose name is the "
                          "module's")
        tops = {PurePosixPath(n).parts[0] for n in names if n.strip("/")}
        manifests = [PurePosixPath(n) for n in names if PurePosixPath(n).name == "__manifest__.py"]
        base = ""
        found = [m.parts[0] for m in manifests if len(m.parts) == 2]
        if not found and len(tops) == 1:
            base = next(iter(tops)) + "/"
            found = [m.parts[1] for m in manifests if len(m.parts) == 3]
        if not found:
            raise Refused("The zip holds no Odoo module: a module is a folder with its __manifest__.py, at the top "
                          "of the zip or in its one folder")
        modules = []
        for module in sorted(set(found)):
            if not MODULE.fullmatch(module):
                raise Refused(f"{module!r} is not the name of a module (a-z 0-9 _)")
            info = archive.getinfo(f"{base}{module}/__manifest__.py")
            if info.file_size > MAX_MANIFEST:
                raise Refused(f"The __manifest__.py of {module} is too large")
            data = read_manifest(archive.read(info), module)
            version = str(data.get("version") or "")
            depends = [d for d in data.get("depends") or [] if isinstance(d, str)]
            modules.append({"module": module, "name": str(data.get("name") or module)[:200],
                            "version": version[:40], "summary": str(data.get("summary") or "")[:300],
                            "depends": depends, "warning": series_warning(module, version, series)})
    return {"modules": modules, "base": base, "files": len(infos), "size": size}


def unpack(path: Path, checked: dict, target: Path) -> list[str]:
    """The checked modules' files into target/<module>/ (a folder made here,
    755, files 644), nothing else of the zip; the sizes read are held to
    what the zip says. The modules unpacked."""
    base, wanted = checked["base"], {m["module"] for m in checked["modules"]}
    target.mkdir(mode=0o755)
    budget = MAX_UNPACKED
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if _entry_problem(info):
                raise Refused(_entry_problem(info))
            if not info.filename.startswith(base):
                continue
            parts = PurePosixPath(info.filename[len(base):]).parts
            if not parts or parts[0] not in wanted:
                continue
            dest = target.joinpath(*parts)
            if target.resolve() not in dest.resolve().parents and dest.resolve() != target.resolve():
                raise Refused(f"{info.filename!r} goes out of its folder")
            if info.is_dir():
                dest.mkdir(mode=0o755, parents=True, exist_ok=True)
                continue
            dest.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            with archive.open(info) as source, open(dest, "xb") as out:
                left = info.file_size
                while True:
                    chunk = source.read(min(1 << 20, left + 1))
                    if not chunk:
                        break
                    left -= len(chunk)
                    budget -= len(chunk)
                    if left < 0 or budget < 0:
                        raise Refused(f"{info.filename} unpacks larger than the zip says")
                    out.write(chunk)
            os.chmod(dest, 0o644)
    for folder in [target, *[p for p in target.rglob("*") if p.is_dir()]]:
        os.chmod(folder, 0o755)
    return sorted(wanted)


def record(folder: Path) -> dict:
    """{module: {name, version, uploaded, by, file}} of a role's uploaded modules."""
    try:
        data = json.loads((folder / RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    present = {p.name for p in folder.iterdir() if p.is_dir() and (p / "__manifest__.py").is_file()} \
        if folder.is_dir() else set()
    return {m: (data.get(m) if isinstance(data.get(m), dict) else {"name": m}) for m in sorted(present)}


def put(folder: Path, staged: Path, modules: list[dict], by: str, file: str, stamp: str) -> list[str]:
    """The modules unpacked in staged moved into folder, each one in place of
    the one of the same name (set aside first, then deleted); the record
    written. The modules that were there before."""
    folder.mkdir(mode=0o755, exist_ok=True)
    os.chmod(folder, 0o755)
    kept = record(folder)
    replaced = []
    for row in modules:
        module = row["module"]
        dest = folder / module
        aside = folder / f".{module}.replaced-{stamp}"
        if dest.is_symlink():
            dest.unlink()
        elif dest.exists():
            dest.rename(aside)
            replaced.append(module)
        (staged / module).rename(dest)
        if aside.exists():
            shutil.rmtree(aside)
        kept[module] = {"name": row["name"], "version": row["version"], "file": file, "by": by,
                        "uploaded": datetime.datetime.now().isoformat(timespec="seconds")}
    text = json.dumps(kept, indent=1, sort_keys=True) + "\n"
    (folder / (RECORD + ".part")).write_text(text, encoding="utf-8")
    os.replace(folder / (RECORD + ".part"), folder / RECORD)
    return replaced


def copy_folder(source: Path, target: Path) -> list[str]:
    """A production's uploaded modules into a staging's (made beside it):
    the target's folder made again from the source's. The modules copied."""
    if not source.is_dir():
        return []
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target, symlinks=True, ignore=shutil.ignore_patterns(".*.replaced-*", "*.part"))
    return sorted(record(target))
