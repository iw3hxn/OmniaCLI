# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a user of workspaces (a customer) reads of what the machine wrote:
its jobs' titles, lines, errors and who started them, without the names of
the integrator's people, links and other machines, and without the paths of
this machine (the access tests of 21/11/2026, FUGA 4). The job kept on disk
and the access log are not touched: the machine's admins read them whole."""

from __future__ import annotations

import dataclasses
import re

# an absolute path of this machine's: its folders, not a URL's path (a
# download /backups/... is OmniaCLI's own route, kept)
_HOST_PATH = re.compile(
    r"(?<![\w.:/~-])/(?:var|opt|etc|srv|home|root|tmp|mnt|data|run|usr|media|snap|lib|lib64|boot|dev)"
    r"(?:/[\w.@+%-]+)+/?")
# the same folder as systemd names its mount unit: var-lib-omniacli\x2dstorage-....mount
_UNIT_PATH = re.compile(r"(?<![\w.-])(?:var|opt|etc|srv|home|root|tmp|mnt|data|run|media)-[\w\\-]+\.(?:mount|automount)\b")
# who a job's source names: "manual: <who>", "mcp: <who>"; a stop's line
_SOURCE = re.compile(r"(?P<kind>manual|mcp|api): (?P<who>.+)\Z", re.S)
_STOPPED_BY = re.compile(r"(?P<head>(?:\d\d:\d\d:\d\d )?--- stop asked by )(?P<who>.+)\Z", re.S)
# a linked OmniaCLI acting for one of its users: "link <name> (<who>)"
_LINK_ACTOR = r"\blink {name}(?: \([A-Za-z0-9._@-]{{1,64}}\))?"


@dataclasses.dataclass(frozen=True)
class CustomerView:
    """provider: how the machine's people are named ("the provider", or the
    label of the Options page); elsewhere: another machine, in the viewer's
    language; links: the names of the links that act here (their people);
    machines: the other machines (the links from here, their hosts);
    members, tokens: the viewer's workspaces' own users and tokens, kept."""

    provider: str
    elsewhere: str
    links: frozenset[str] = frozenset()
    machines: frozenset[str] = frozenset()
    members: frozenset[str] = frozenset()
    tokens: frozenset[str] = frozenset()
    # this machine's own names (its host name), and how the customer reads them: "the server" (the map's word)
    here: frozenset[str] = frozenset()
    here_word: str = "the server"

    def who(self, name: str) -> str:
        """Who acted, as the customer may read it: its own users and tokens
        by name, anyone else (the machine's users, a link) as the provider."""
        name = name.strip()
        if name in self.members:
            return name
        if name.startswith("token ") and name[6:] in self.tokens:
            return name
        return self.provider

    def text(self, value):
        """A line, a title or an error, the names and paths taken out."""
        if not isinstance(value, str) or not value:
            return value
        if "\n" in value:
            return "\n".join(self.text(line) for line in value.split("\n"))
        stopped = _STOPPED_BY.match(value)
        if stopped:
            return stopped["head"] + self.who(stopped["who"])
        for name in sorted(self.links, key=len, reverse=True):
            value = re.sub(_LINK_ACTOR.format(name=re.escape(name)), self.provider, value)
        for name in sorted(self.links | self.machines, key=len, reverse=True):
            value = re.sub(rf"(?<![\w.-]){re.escape(name)}(?![\w-])", self.elsewhere if name in self.machines
                           else self.provider, value)
        for name in sorted(self.here - self.links - self.machines, key=len, reverse=True):
            value = re.sub(rf"(?<![\w.-]){re.escape(name)}(?![\w-])", self.here_word, value)
        value = _UNIT_PATH.sub(lambda m: "…" + m.group(0)[m.group(0).rindex("."):], value)
        return _HOST_PATH.sub(lambda m: "…/" + m.group(0).rstrip("/").rsplit("/", 1)[-1], value)

    def source(self, value: str) -> str:
        found = _SOURCE.match(value or "")
        if found:
            return f"{found['kind']}: {self.who(found['who'])}"
        return self.text(value)

    def data(self, value):
        """A job's result: every text in it, at any depth."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {k: self.data(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.data(v) for v in value]
        return value

    def job(self, job, lines: bool = True):
        """A copy of job to show, its lines too (a list shows none: they are
        left out); the job itself is not changed. A job that writes what its
        customer reads (result["customer"]: {title, lines}, the move of an
        instance between the provider's machines) shows that only."""
        own = customer_part(job)
        if own is not None:
            return dataclasses.replace(job, title=self.text(str(own.get("title") or "")), source=self.source(job.source),
                                       error=self.text(job.error) if own.get("error") else None, result={},
                                       lines=[self.text(str(line)) for line in own.get("lines") or []] if lines else [])
        return dataclasses.replace(job, title=self.text(job.title), source=self.source(job.source),
                                   error=self.text(job.error), result=self.data(job.result),
                                   lines=[self.text(line) for line in job.lines] if lines else [])


def customer_part(job) -> dict | None:
    """What a job writes for its customer to read instead of itself (result["customer"]), None for none."""
    found = job.result.get("customer") if isinstance(job.result, dict) else None
    return found if isinstance(found, dict) else None


def hidden_from_customers(job) -> bool:
    """A job of the provider's own on a customer's instance (the move's Prepare, Cancel, Delete the copy): the
    customer does not see it in its lists."""
    return bool((customer_part(job) or {}).get("hidden"))
