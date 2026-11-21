# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The catalog of the free modules an instance may add (docs/ux/UX-008 §7):
the OCA's repositories for a series of Odoo, read with git from github.com
(no account, no paid service, no API limit), kept on disk with its date,
read again on demand or at night, used from the disk when github.com does
not answer.

- the repositories: OCA/repo-maintainer-conf, conf/repo/*.yml, those whose
  branches name the series (a shallow, sparse clone of that folder only);
- each one's branch of the series, git ls-remote: a commit read already is
  not downloaded again;
- a changed one: a shallow, partial, sparse clone (--depth 1
  --filter=blob:none, only */__manifest__.py and requirements.txt) in a
  temporary folder, deleted after;
- the manifests read with ast.literal_eval, never run; the ones not
  installable are left out.

The file, <state folder>/catalog/oca-<series>.json:
{"series", "source", "read": when it was last read whole, "tried": when a
reading was last tried, "error": why that one failed (absent when it did
not), "repositories": {name: {"commit", "label", "modules": {module: {...}}}}}"""

from __future__ import annotations

import ast
import asyncio
import datetime
import json
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import ops

SOURCE = "https://github.com/OCA"
CONF_URL = f"{SOURCE}/repo-maintainer-conf.git"
CONF_FOLDER = "conf/repo"
FOLDER = "catalog"
SERIES = re.compile(r"\d{1,2}\.0")
REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
MODULE = re.compile(r"[a-z0-9][a-z0-9_]{0,99}")
# the network: each git command within its time, a few at once
LS_REMOTE_TIMEOUT = 30
CLONE_TIMEOUT = 180
PARALLEL = 8
# what a search shows at most (UX-008 §7.3)
LIMIT = 50
# the fields a manifest gives the catalog
FIELDS = ("name", "summary", "version", "license", "author", "category", "development_status")
_GIT_ENV = {**ops._ENV, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/true"}
# words of an Italian search and the English ones the manifests use
SYNONYMS = {
    "excel": ("xlsx", "xls", "excel", "spreadsheet"),
    "magazzino": ("stock", "warehouse", "inventory"),
    "iva": ("vat", "tax"),
    "partita": ("vat", "partner"),
    "fattura": ("invoice", "invoicing"), "fatture": ("invoice", "invoicing"),
    "fatturazione": ("invoice", "invoicing"),
    "contabilita": ("account", "accounting"), "contabile": ("account", "accounting"),
    "vendite": ("sale", "sales"), "vendita": ("sale", "sales"),
    "acquisti": ("purchase",), "acquisto": ("purchase",),
    "banca": ("bank", "banking"), "pagamenti": ("payment",), "pagamento": ("payment",),
    "dipendenti": ("employee", "hr"), "personale": ("hr", "employee"),
    "progetto": ("project",), "progetti": ("project",),
    "produzione": ("mrp", "manufacturing"), "spedizioni": ("delivery", "shipping"),
    "spedizione": ("delivery", "shipping"), "listino": ("pricelist",), "prezzi": ("price", "pricelist"),
    "stampa": ("report", "print"), "rapporti": ("report", "reporting"), "rapporto": ("report",),
    "cliente": ("customer", "partner"), "clienti": ("customer", "partner"),
    "fornitore": ("supplier", "vendor"), "fornitori": ("supplier", "vendor"),
    "sito": ("website",), "documenti": ("document", "dms"), "firma": ("sign", "signature"),
    "ore": ("timesheet",), "contratti": ("contract",), "contratto": ("contract",),
    "approvazione": ("tier", "validation", "approval"), "punto": ("pos",), "cassa": ("pos",),
}


def now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def path_of(state_dir: Path, series: str) -> Path:
    if not SERIES.fullmatch(series):
        raise ValueError(f"{series!r} is not a series of Odoo, as 19.0")
    return Path(state_dir) / FOLDER / f"oca-{series}.json"


def series_of(*names: str) -> str | None:
    """The series of Odoo a template is for: odoo-19 → 19.0, odooplm-20 →
    20.0, «Odoo 19 Community» → 19.0, odoo:18.0 → 18.0; None when none says."""
    for name in names:
        found = re.search(r"(?:^|[^0-9])(\d{2})(?:\.0)?(?:[^0-9.]|$)", str(name or ""))
        if found and 8 <= int(found.group(1)) <= 40:
            return f"{int(found.group(1))}.0"
    return None


def load(path: Path) -> dict | None:
    """The catalog on disk, None when there is none (or it is not one)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("repositories"), dict):
        return None
    return data


def save(path: Path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".part")
    partial.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
                       encoding="utf-8")
    os.replace(partial, path)


def parse_manifest(text: str) -> dict | None:
    """What the catalog keeps of a manifest, read as a literal (never run);
    None when it is not one or the module is not installable."""
    try:
        manifest = ast.literal_eval(text)
    except (ValueError, SyntaxError, MemoryError, RecursionError, TypeError):
        return None
    if not isinstance(manifest, dict) or manifest.get("installable", True) is False:
        return None
    found = {key: str(manifest[key]).strip() for key in FIELDS if isinstance(manifest.get(key), (str, int, float))}
    depends = manifest.get("depends") or []
    found["depends"] = [str(d) for d in depends if isinstance(d, str) and MODULE.fullmatch(d)] \
        if isinstance(depends, (list, tuple)) else []
    external = manifest.get("external_dependencies") or {}
    python = external.get("python") if isinstance(external, dict) else None
    found["python"] = [str(p) for p in python if isinstance(p, str)] if isinstance(python, (list, tuple)) else []
    return found


def repositories_in_conf(folder: Path, series: str) -> dict[str, str]:
    """{repository: its label} of repo-maintainer-conf's conf/repo/*.yml
    whose branches name the series."""
    found = {}
    for path in sorted(Path(folder).glob("*.yml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(data, dict):
            continue
        for name, info in data.items():
            if not isinstance(info, dict) or not REPOSITORY.fullmatch(str(name)):
                continue
            if series in [str(b) for b in info.get("branches") or []]:
                found[str(name)] = str(info.get("name") or name)
    return found


class SourceError(Exception):
    """The source of the catalog did not answer."""


def _last_line(out: str) -> str:
    lines = [line for line in str(out).splitlines() if line.strip()]
    return (lines[-1] if lines else "no answer")[-300:]


class GitSource:
    """The OCA's repositories on github.com, read with git (no API)."""

    def __init__(self, base: str = SOURCE):
        self.base = base.rstrip("/")

    def url(self, repository: str) -> str:
        return f"{self.base}/{repository}.git"

    async def _git(self, *args: str, cwd: Path | None = None, timeout: float = CLONE_TIMEOUT) -> str:
        code, out = await ops.capture(["git", *args], cwd=cwd, timeout=timeout, env=_GIT_ENV)
        if code:
            raise SourceError(_last_line(out))
        return out

    async def repositories(self, series: str) -> dict[str, str]:
        with tempfile.TemporaryDirectory(prefix="omniacli-catalog-") as tmp:
            target = Path(tmp) / "conf"
            await self._git("clone", "-q", "--depth", "1", "--filter=blob:none", "--sparse", "--",
                            f"{self.base}/repo-maintainer-conf.git", str(target))
            await self._git("sparse-checkout", "set", CONF_FOLDER, cwd=target)
            return repositories_in_conf(target / CONF_FOLDER, series)

    async def head(self, repository: str, series: str) -> str | None:
        """The commit of the series' branch, None when the repository has none."""
        out = await self._git("ls-remote", "--heads", "--", self.url(repository), f"refs/heads/{series}",
                              timeout=LS_REMOTE_TIMEOUT)
        for line in out.splitlines():
            sha, _, ref = line.partition("\t")
            if ref.strip() == f"refs/heads/{series}" and re.fullmatch(r"[0-9a-f]{40,64}", sha):
                return sha
        return None

    async def manifests(self, repository: str, series: str) -> dict[str, str]:
        """{module: the text of its __manifest__.py} at the series' branch."""
        with tempfile.TemporaryDirectory(prefix="omniacli-catalog-") as tmp:
            target = Path(tmp) / repository
            await self._git("clone", "-q", "--depth", "1", "--filter=blob:none", "--no-checkout",
                            "--branch", series, "--", self.url(repository), str(target))
            await self._git("sparse-checkout", "set", "--no-cone", "/*/__manifest__.py", "/requirements.txt",
                            cwd=target)
            await self._git("checkout", "-q", cwd=target)
            found = {}
            for manifest in sorted(target.glob("*/__manifest__.py")):
                if MODULE.fullmatch(manifest.parent.name):
                    try:
                        found[manifest.parent.name] = manifest.read_text(encoding="utf-8")
                    except (OSError, UnicodeDecodeError):
                        continue
            return found


async def refresh(job, path: Path, series: str, source=None) -> dict:
    """The catalog of the series read again: the repositories of the
    source, each one downloaded only when its commit changed. What cannot be
    read stays as it was read before; the error is kept with the date."""
    source = source or GitSource()
    old = load(path) or {}
    before = old.get("repositories") or {}
    data = {"series": series, "source": SOURCE, "read": old.get("read"), "tried": now(),
            "repositories": dict(before)}
    try:
        listed = await source.repositories(series)
    except SourceError as exc:
        data["error"] = f"the repositories of the OCA cannot be read from github.com: {exc}"
        job.log(f"WARNING: {data['error']}")
        save(path, data)
        return data
    job.log(f"--- {len(listed)} repositories of the OCA have a branch {series}")
    semaphore = asyncio.Semaphore(PARALLEL)
    failures: list[str] = []
    changed = 0

    async def one(name: str, label: str):
        nonlocal changed
        async with semaphore:
            try:
                commit = await source.head(name, series)
                if commit is None:
                    return name, None
                kept = before.get(name)
                if kept and kept.get("commit") == commit:
                    return name, {**kept, "label": label}
                texts = await source.manifests(name, series)
            except SourceError as exc:
                failures.append(f"{name}: {exc}")
                # read before: kept as it was
                return name, before.get(name, False)
        modules = {}
        for module, text in texts.items():
            parsed = parse_manifest(text)
            if parsed is not None:
                modules[module] = parsed
        changed += 1
        job.log(f"[{name}] {commit[:10]}: {len(modules)} modules")
        return name, {"commit": commit, "label": label, "modules": modules}

    results = await asyncio.gather(*(one(name, label) for name, label in sorted(listed.items())))
    repositories = {}
    for name, value in results:
        if value:
            repositories[name] = value
    data["repositories"] = repositories
    count = sum(len(r.get("modules") or {}) for r in repositories.values())
    if failures:
        data["error"] = (f"{len(failures)} repositories of the OCA cannot be read from github.com "
                         f"(the first: {failures[0]})")
        job.log(f"WARNING: {data['error']}")
    else:
        data["read"] = data["tried"]
    if failures and not data["read"]:
        # never read whole: the date of this reading, the error said beside it
        data["read"] = data["tried"]
    job.log(f"Catalog of the OCA for Odoo {series}: {count} modules in {len(repositories)} repositories, "
            f"{changed} read again")
    save(path, data)
    return data


# --- the catalog as the pages use it


@dataclass
class Catalog:
    series: str
    read: str | None = None
    tried: str | None = None
    error: str | None = None
    modules: dict[str, dict] = field(default_factory=dict)
    repositories: dict[str, str] = field(default_factory=dict)

    @property
    def count(self) -> int:
        return len(self.modules)

    def meta(self) -> dict:
        return {"series": self.series, "read": self.read, "tried": self.tried, "error": self.error,
                "count": self.count, "repositories": len(self.repositories)}


def catalog_of(data: dict | None, series: str) -> Catalog:
    """The modules of a catalog file, each with its repository (a module in
    two repositories: the first by name)."""
    found = Catalog(series)
    if not data:
        return found
    found.read, found.tried, found.error = data.get("read"), data.get("tried"), data.get("error")
    for repo, info in sorted((data.get("repositories") or {}).items()):
        if not isinstance(info, dict):
            continue
        found.repositories[repo] = str(info.get("label") or repo)
        for module, entry in sorted((info.get("modules") or {}).items()):
            if module not in found.modules and isinstance(entry, dict):
                found.modules[module] = {**entry, "module": module, "repository": repo}
    return found


def _fold(text: str) -> str:
    """Lower case, without accents: contabilità finds contabilita."""
    return "".join(c for c in unicodedata.normalize("NFKD", str(text).lower()) if not unicodedata.combining(c))


def search(catalog: Catalog, query: str, limit: int = LIMIT) -> tuple[list[dict], int]:
    """(the first `limit` modules matching every word of the query, how many
    match): a word matches the technical name, the name, the summary, the
    category or the repository, or one of its English words (SYNONYMS). The
    modules whose name holds the words come first. An empty query: none."""
    words = [w for w in re.split(r"[\s,;]+", _fold(query)) if len(w) >= 2]
    if not words:
        return [], 0
    alternatives = [(w, *SYNONYMS.get(w, ())) for w in words]
    scored = []
    for module, entry in catalog.modules.items():
        head = _fold(f"{module} {entry.get('name', '')}")
        text = _fold(" ".join([head, entry.get("summary", ""), entry.get("category", ""), entry["repository"]]))
        score = 0
        for options in alternatives:
            if any(o in head for o in options):
                score += 2
            elif any(o in text for o in options):
                score += 1
            else:
                break
        else:
            scored.append((-score, module, entry))
    scored.sort(key=lambda item: (item[0], item[1]))
    return [entry for _, _, entry in scored[:limit]], len(scored)


# --- an instance and the catalog

INSTALLED = ("installed", "to upgrade")


def state_of(module: str, known: dict[str, dict[str, str] | None], code: set[str]) -> str:
    """installed (in production), installed_test, in_code, to_add."""
    if (known.get("production") or {}).get(module) in INSTALLED:
        return "installed"
    if (known.get("test") or {}).get(module) in INSTALLED:
        return "installed_test"
    if module in code:
        return "in_code"
    return "to_add"


def plan(catalog: Catalog, module: str, role: str, known: dict[str, dict[str, str] | None],
         code: set[str], repositories: set[str]) -> dict:
    """What installing module in the database of role takes: the
    repositories to add (its own and those of the modules it depends on,
    transitively, from the catalog), the modules newly installed, the Python
    packages of their manifests; blocked: why it cannot be installed (a
    module it needs that neither the instance nor the catalog has)."""
    target = known.get(role)
    repos: list[str] = []
    modules: list[str] = []
    python: list[str] = []
    missing: list[str] = []
    seen: set[str] = set()

    def visit(name: str) -> None:
        if name in seen:
            return
        seen.add(name)
        state = (target or {}).get(name)
        if state in INSTALLED:
            return
        entry = catalog.modules.get(name)
        if state == "uninstallable" and name not in code and entry is None:
            missing.append(name)
            return
        if name in code or (target is not None and state is not None and state != "uninstallable"):
            # in the instance's code or the image's (the database knows it)
            modules.append(name)
            for dep in (entry or {}).get("depends", []) if name in code else []:
                visit(dep)
            return
        if entry is None:
            if target is None:
                # the database did not answer: an Odoo module, likely; Odoo says at the install
                return
            missing.append(name)
            return
        if entry["repository"] not in repositories and entry["repository"] not in repos:
            repos.append(entry["repository"])
        modules.append(name)
        for package in entry.get("python") or []:
            if package not in python:
                python.append(package)
        for dep in entry.get("depends") or []:
            visit(dep)

    visit(module)
    blocked = None
    if module not in catalog.modules and module not in code:
        blocked = f"{module} is not in the catalog of the OCA for Odoo {catalog.series}"
    elif missing:
        blocked = (f"Requires {', '.join(missing)}, which this instance does not have and the catalog of the OCA "
                   "does not know (Odoo Enterprise, or another author's)")
    return {"module": module, "role": role, "repositories": repos, "modules": modules, "python": python,
            "missing": missing, "blocked": blocked, "checked": target is not None}
