# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The pages a panel draws itself for a linked machine (AD-017 §17.1-17.2).

A linked machine's page used to be the HTML that machine wrote: a fix of a
page reached it only with its update. With a link's ``pages: here`` (the
default) the panel draws the pages of this list with its own templates, under
``/links/<name>``, from the page's model the machine answers at
``GET /api/pages/<page>`` (its fields by name, no object, no secret); the
page's script calls ``/links/<name>/api/…`` as before.

The contract: a machine says ``agent_api`` (an integer) and its ``plugins``
(``{name: version}``) in ``GET /api/info``; one that says nothing is 0. Each
page here says the least it needs (``needs_agent_api``); the panel reads up to
``CENTRAL_AGENT_API``, and that one and the one before it (AD-017 §10.6). It
draws a page when ``needs_agent_api <= agent_api <= CENTRAL_AGENT_API`` and
it has every plugin of the machine at the same version; else the machine's own
page passes as before, with a line on top that says why.

Within one ``agent_api`` the machine's API only grows (fields, routes): taking
away or renaming one raises the number."""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import plugins

# what this machine's API speaks: GET /api/info says it, GET /api/pages/* answers from it
AGENT_API = 1
# the newest a panel of this version reads; it reads that one and the one before it (AD-017 §10.6)
CENTRAL_AGENT_API = 1
OLDEST_READ = CENTRAL_AGENT_API - 1

# a link's pages (links.yaml, pages:): drawn here, or the machine's own as before (the undo switch)
PAGES_HERE = "here"
PAGES_MACHINE = "machine"
PAGES_WAYS = (PAGES_HERE, PAGES_MACHINE)

# why the machine's own page passes (the line on top says it)
BEHIND = "behind"  # the machine is older than the page asks
AHEAD = "ahead"  # the panel is older than the machine
PLUGINS = "plugins"  # a plugin of the machine this panel has not, or at another version

# how often a link's agent_api is read again, kept in memory (seconds); after an update made from the panel,
# for a while, much sooner: the machine restarts with its new version
READ_EVERY = 600
READ_AFTER_UPDATE = 15
# a built-in plugin comes with the package: the package's agent_api answers for it, not its version
BUILT_IN = "built-in"

_SEGMENT = r"[A-Za-z0-9._~-]+"


@dataclass(frozen=True)
class Page:
    """A page this panel draws for a linked machine: where it is under /links/<name>/, its template, the least
    agent_api it needs and the fields of its model it takes (any other the machine sends is left out)."""

    name: str
    path: str
    template: str
    fields: tuple[str, ...] = ()
    needs_agent_api: int = 1

    @property
    def pattern(self) -> re.Pattern:
        return re.compile(self.path)


# the 12 pages of class A (AD-017 §17.1): a shell whose script reads /api, a context of a few plain values
PAGES = (
    Page("disks", r"disks", "disks.html"),
    Page("log", rf"servers/(?P<server>{_SEGMENT})/(?P<role>production|test)/log", "log.html", ("instance",)),
    Page("own_log", r"omniacli/log", "own_log.html", ("log_file",)),
    Page("instances", r"instances", "instances.html",
         ("here_name", "links", "quick_instance", "open_in_rows", "ni_links")),
    Page("backups", r"backups", "backups.html", ("here_name", "links", "upload_here", "upload_links")),
    Page("machines", r"machines", "machines.html",
         ("provision_key", "package_url", "link_user", "machines", "groups")),
    Page("discover", r"config/discover", "discover.html", ("roles",)),
    Page("connection", rf"pullers/connections/(?P<name>{_SEGMENT})", "connection.html",
         ("name", "connection", "commands")),
    Page("shell", rf"servers/(?P<server>{_SEGMENT})/(?P<role>production|test)/(?P<kind>shell|claude|psql|terminal)",
         "shell.html", ("instance", "folder", "kind", "command", "stay", "system_user", "shell", "drops",
                        "drop_days", "drop_max")),
    Page("help", r"help", "help.html", ("base_url",)),
    Page("about", r"about", "about.html"),
    Page("settings", r"settings", "settings.html", ("groups",)),
)
BY_NAME = {p.name: p for p in PAGES}
# the least any page here needs: below it, every page of the machine is its own
LEAST_NEEDED = min(p.needs_agent_api for p in PAGES)


def match(path: str) -> tuple[Page, dict[str, str]] | None:
    """The page drawn here for path (under /links/<name>/, no query) and its parameters; None: none."""
    for page in PAGES:
        found = page.pattern.fullmatch(path)
        if found is not None:
            return page, found.groupdict()
    return None


def plugin_versions(registry: plugins.Registry | None = None) -> dict[str, str]:
    """{name: version} of the plugins loaded here; a built-in one is BUILT_IN (it is the package's)."""
    registry = registry or plugins.registry()
    return {i.name: BUILT_IN if i.source == "built-in" else (i.version or "") for i in registry.found
            if i.plugin is not None}


def agent_of(info: dict) -> dict:
    """What a machine's GET /api/info says of the contract: agent_api (0 when it says nothing or nonsense),
    plugins ({name: version}, only strings), its version."""
    api = info.get("agent_api")
    found = info.get("plugins")
    return {"agent_api": api if isinstance(api, int) and not isinstance(api, bool) and api >= 0 else 0,
            "plugins": {str(k): str(v) for k, v in found.items() if isinstance(v, str)} if isinstance(found, dict) else {},
            "version": str(info.get("version") or "?")}


def contract(needs: int, agent_api: int, theirs: dict[str, str], ours: dict[str, str]) -> str:
    """"" when this panel draws a page that needs `needs` of a machine at agent_api with plugins `theirs`;
    else why the machine's own page passes: BEHIND, AHEAD or PLUGINS."""
    if agent_api > CENTRAL_AGENT_API:
        return AHEAD
    if agent_api < needs or agent_api < OLDEST_READ:
        return BEHIND
    if any(ours.get(name) != version for name, version in theirs.items()):
        return PLUGINS
    return ""


def plugins_apart(theirs: dict[str, str], ours: dict[str, str]) -> list[str]:
    """The machine's plugins this panel has not, or at another version: name version."""
    return [f"{name} {version}".strip() for name, version in sorted(theirs.items()) if ours.get(name) != version]


# what a page drawn here takes from the machine through its link: data, files to save, images; never a
# document or a script (AD-017 §17.5 decision 1: a machine that writes HTML under this panel's origin runs it
# with the session of who looks)
_DATA_TYPES = frozenset({
    "application/json", "application/x-ndjson", "text/plain", "text/csv", "application/octet-stream",
    "application/zip", "application/x-tar", "application/gzip", "application/sql",
    "image/png", "image/jpeg", "image/gif", "image/webp", "image/x-icon", "image/vnd.microsoft.icon",
})


def is_data(content_type: str, disposition: str, has_body: bool = True) -> bool:
    """Whether an answer of a machine passes for a page drawn here: JSON and the other data types, a file to save
    (Content-Disposition: attachment), an answer without a body (a redirect, 204)."""
    media = content_type.split(";", 1)[0].strip().lower()
    if disposition.strip().lower().startswith("attachment"):
        return True
    if not media:
        return not has_body
    return media in _DATA_TYPES or (media.startswith("application/") and media.endswith("+json"))


_BODY = re.compile(rb"<body\b[^>]*>", re.I)


def with_line(html: bytes, line: str) -> bytes:
    """The machine's page with line (HTML) on top of its body."""
    found = _BODY.search(html)
    at = found.end() if found else 0
    return html[:at] + line.encode() + html[at:]
