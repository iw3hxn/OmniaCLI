# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The map of the infrastructure (docs/ux/UX-009): what GET /api/map answers
is put together in app.py from what the panel already knows; here is what
does not need the application: the ids of the nodes, the names of an
environment with their HTTPS and DNS state as the last publication left
them, the count of a staging's caught mail kept for five minutes, what is
to look at (the verdict), and a linked machine's answer taken as one of the
machines of this map.

The ids: a machine is HERE for the one that answers and the link's name
for a linked one; an instance is <machine>/<server>, a workspace
<machine>/ws:<name>, a database <machine>/db:<where>, a destination of the
backups <machine>/dest:<name>. A link's name has no "~" (config._LINK_NAME)."""

from __future__ import annotations

import datetime
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

HERE = "~"
# next to the servers file: {workspace: {"at", "names": {name: {"https", "dns"}}}}
WEB_FILE = "webnames.json"
# a staging's caught mail is counted again after these seconds at the soonest
MAIL_EVERY = 300
# the stagings of this machine's productions made on the linked machines, for
# the users of workspaces: asked again after these seconds
ELSEWHERE_EVERY = 60
# a job that ended stays in the strip for these seconds, and to look at for a day
JOB_SHOWN = 120
WATCH_HOURS = 24
# a certificate with fewer days is to look at (monitor.CERT_DAYS), critical with fewer than CERT_CRITICAL
CERT_DAYS = 14
CERT_CRITICAL = 3
# the level of each kind of thing to look at; one of a linked OmniaCLI that sends none: a warning
LEVELS = {"stopped": "warning", "down": "critical", "backup_failed": "critical", "certificate": "warning",
          "names": "warning", "restarted": "warning", "job_failed": "critical", "job_warned": "warning",
          "disk": "warning", "load": "warning", "replica": "warning", "machine_older": "warning",
          "machine_unread": "critical"}


def leveled(watch: list[dict]) -> list[dict]:
    """Each thing to look at with its level (critical, warning), the critical first, in order otherwise."""
    for item in watch:
        if item.get("level") not in ("critical", "warning"):
            item["level"] = LEVELS.get(item.get("kind"), "warning")
    return sorted(watch, key=lambda w: w["level"] != "critical")
# a link that did not answer the whole map is not asked the layer of now for
# these seconds (the whole read, every 60, asks it again): its last reason at once
UNANSWERED_FOR = 90
# who asks a link for the stagings of this machine's productions: the map
# itself, never the reader (a customer's name would be written in the link's
# access log as a reader of its whole machine)
SYSTEM_ACTOR = "map"
_LOCAL = re.compile(r"(localhost|127\.0\.0\.1|\[::1\])(:|$)")


def now_text() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def public_host(url) -> str:
    """The host of url a reader may open from its own computer: "" for none
    and for localhost (an instance without a name answers on a port of its
    machine: on the reader's computer that address is something else)."""
    host = urlsplit(str(url)).netloc if url else ""
    return "" if not host or _LOCAL.match(host) else host


def public_url(url) -> str | None:
    """url, or None when its host is not one a reader may open (public_host)."""
    return str(url) if public_host(url) else None


def seconds_since(stamp, now: datetime.datetime) -> int | None:
    """The seconds from stamp (a time of this machine's clock, as the jobs
    keep it) to now: the page counts on from them, whatever the time zone of
    the browser that reads it. None for no time."""
    try:
        return max(0, int((now - datetime.datetime.fromisoformat(str(stamp))).total_seconds())) if stamp else None
    except ValueError:
        return None


def instance_id(server: str, machine: str = HERE) -> str:
    return f"{machine}/{server}"


def workspace_id(name: str, machine: str = HERE) -> str:
    return f"{machine}/ws:{name}"


def database_id(place: dict, machine: str = HERE) -> str | None:
    """The node of a database that is not the instance's own: the machine's
    cluster or one on another machine; None for a dedicated one (no node)."""
    if place.get("where") == "machine":
        return f"{machine}/db:machine"
    if place.get("where") == "remote":
        return f"{machine}/db:{place.get('host')}"
    return None


_SERVER_BLOCK = re.compile(r"(?:^|\n)server\s*\{(.*?)\n\}", re.S)
_SERVER_NAME = re.compile(r"(?:^|[;{}\s])server_name\s+([^;]*);")
_LISTEN_TLS = re.compile(r"(?:^|[;{}\s])listen\s+[^;]*\bssl\b[^;]*;")


def site_names(text: str) -> dict:
    """{name: {"https", "dns"}} of a workspace's nginx site as the helper
    wrote it (workspaceweb.conf_text): a name of a server that listens with
    ssl answers on HTTPS, one that is on port 80 only does not; the DNS is
    not in the site (None: not read)."""
    found: dict[str, dict] = {}
    for block in _SERVER_BLOCK.finditer(text or ""):
        body = "\n".join(line.split("#", 1)[0] for line in block.group(1).splitlines())
        tls = bool(_LISTEN_TLS.search(body))
        for match in _SERVER_NAME.finditer(body):
            for name in match.group(1).split():
                name = name.lower().strip("\"'")
                if name and name != "_" and not name.startswith("~"):
                    found[name] = {"https": tls or found.get(name, {}).get("https", False), "dns": None}
    return found


class WebNames:
    """The HTTPS and DNS state of each name of a workspace as its last
    publication found it (the root helper's answer): the map reads it, and
    never asks the helper or the DNS again. A workspace published before
    the map kept this has no such record: its site as nginx holds it (sites:
    the folder of the sites, world readable) is read once, which says the
    HTTPS of each name and not its DNS. A name in neither was not read yet."""

    def __init__(self, path: Path, sites: Path | None = None):
        self.path = path
        self.sites = sites
        # read once: this process alone writes the file
        self._data: dict | None = None
        self._sites: dict[str, dict] = {}

    def load(self) -> dict:
        if self._data is None:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            self._data = data if isinstance(data, dict) else {}
        return self._data

    def record(self, workspace: str, made: dict) -> None:
        """made: what workspaceops.publish returned ({names, https,
        redirects, without_dns, elsewhere}); nothing kept of an answer with
        no names (a workspace without storage or domain)."""
        if not isinstance(made, dict) or "names" not in made:
            return
        missing, away = set(made.get("without_dns") or []), set(made.get("elsewhere") or [])
        names = [*(made.get("names") or []), *(made.get("redirects") or {})]
        found = {str(name): {"https": bool(made.get("https")) and name not in missing | away,
                             "dns": "none" if name in missing else "elsewhere" if name in away else "here"}
                 for name in names}
        data = self.load()
        data[workspace] = {"at": now_text(), "names": found}
        self._sites.pop(workspace, None)
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            # the map says "not read yet": the publication itself is done
            pass

    def of(self, workspace: str) -> dict:
        found = self.load().get(workspace)
        if isinstance(found, dict):
            return found.get("names") or {}
        if self.sites is None:
            return {}
        if workspace not in self._sites:
            try:
                text = (self.sites / f"omniacli-{workspace}.conf").read_text(encoding="utf-8")
            except (OSError, ValueError):
                text = ""
            self._sites[workspace] = site_names(text)
        return self._sites[workspace]


def env_names(names: list[str], aliases: list[str], redirects: dict[str, str], known: dict) -> list[dict]:
    """[{name, kind: main|alias|redirect, to, https, dns}] of an environment:
    its first name, the others it answers on, the names that send to one of
    them; https and dns None when the last publication did not read them."""
    out = []
    for index, name in enumerate([*names, *(a for a in aliases if a not in names)]):
        state = known.get(name) or {}
        out.append({"name": name, "kind": "alias" if index else "main", "to": None,
                    "https": state.get("https"), "dns": state.get("dns")})
    for name, to in redirects.items():
        state = known.get(name) or {}
        out.append({"name": name, "kind": "redirect", "to": to, "https": state.get("https"), "dns": state.get("dns")})
    return out


class MailCounts:
    """How many messages each capturing staging holds, counted at most every
    MAIL_EVERY seconds and never while a request waits: the map shows the
    count it has, with its time."""

    def __init__(self):
        self.found: dict[str, dict] = {}
        self._asked: dict[str, float] = {}

    def due(self, server: str) -> bool:
        """True once per MAIL_EVERY seconds for each staging: who gets it counts."""
        now = time.monotonic()
        if now - self._asked.get(server, -MAIL_EVERY) < MAIL_EVERY:
            return False
        self._asked[server] = now
        return True

    def store(self, server: str, count: int | None, last: str | None) -> None:
        self.found[server] = {"count": count, "last": last, "counted_at": now_text()}

    def of(self, server: str) -> dict:
        return self.found.get(server) or {"count": None, "last": None, "counted_at": None}

    def forget(self, servers) -> None:
        for name in set(self.found) - set(servers):
            self.found.pop(name, None)
            self._asked.pop(name, None)


def bad_names(names: list[dict]) -> list[str]:
    """The names read without HTTPS or with a DNS that does not lead here."""
    return [n["name"] for n in names if n.get("https") is False or n.get("dns") in ("none", "elsewhere")]


def watch_of(instance: dict, say) -> list[dict]:
    """What is to look at on an instance of the map: a production stopped
    (a staging or a test stopped is a choice), one that does not answer,
    an automatic backup that failed, a certificate about to end, a name
    without HTTPS or DNS. say(text, **values): the reader's language."""
    out = []
    label, node = instance["name"], instance["id"]
    production = (instance.get("envs") or {}).get("production") or {}
    monitor = instance.get("monitor") or {}
    if instance["type"] == "production" and production.get("state") == "stopped":
        out.append({"kind": "stopped", "node": node, "text": say("{name} is not running", name=label),
                    "since": monitor.get("since") if monitor.get("state") == "stopped" else None})
    if monitor.get("state") == "down":
        out.append({"kind": "down", "node": node, "text": say("{name} does not answer", name=label),
                    "since": monitor.get("since")})
    restarted = monitor.get("restarted")
    if restarted and restarted >= (datetime.datetime.now() - datetime.timedelta(hours=WATCH_HOURS)).isoformat():
        out.append({"kind": "restarted", "node": node, "since": restarted, "job": monitor.get("restart_job"),
                    "page": f"/jobs/{monitor['restart_job']}" if monitor.get("restart_job") else None,
                    "text": say("{name} did not answer and was restarted automatically", name=label)})
    last = (instance.get("backup") or {}).get("last")
    if last and not last.get("ok"):
        out.append({"kind": "backup_failed", "node": node, "since": last.get("time"), "job": last.get("job"),
                    "text": say("The automatic backup of {name} failed", name=label)})
    certificate = instance.get("certificate")
    if certificate and certificate.get("days") is not None and certificate["days"] < CERT_DAYS:
        out.append({"kind": "certificate", "node": node, "since": certificate.get("checked"),
                    "level": "critical" if certificate["days"] < CERT_CRITICAL else "warning",
                    "text": say("The certificate of {name} ends in {days} days", name=label, days=certificate["days"])})
    wrong = [n for env in (instance.get("envs") or {}).values() if env for n in bad_names(env.get("names") or [])]
    if wrong:
        out.append({"kind": "names", "node": node, "since": None,
                    "text": say("{names}: without HTTPS, or the DNS does not point here", names=", ".join(wrong))})
    return out


def names_by_workspace(watch: list[dict], workspaces: list[dict], say) -> list[dict]:
    """The names of the instances of one workspace without HTTPS have one
    cause (its domain, its certificate): one line for the workspace, every
    instance of it marked on the map (nodes), not a line for each."""
    for ws in workspaces:
        ids = {i["id"] for i in ws.get("instances") or []}
        same = [w for w in watch if w["kind"] == "names" and w.get("node") in ids]
        if len(same) < 2:
            continue
        wrong = [n for i in ws["instances"] for env in (i.get("envs") or {}).values() if env
                 for n in bad_names(env.get("names") or [])]
        shown = ", ".join(wrong[:3]) + (", …" if len(wrong) > 3 else "")
        first = watch.index(same[0])
        watch = [w for w in watch if w not in same]
        watch.insert(first, {"kind": "names", "node": same[0]["node"], "nodes": [w["node"] for w in same], "since": None,
                             "text": say("{n} instances of {workspace}: {names} without HTTPS, or the DNS does not point here",
                                         n=len(same), workspace=ws.get("label") or ws.get("name") or "", names=shown)})
    return watch


def rebase_id(value, machine: str):
    """An id of a linked machine's own map as an id of this one."""
    return machine + value[len(HERE):] if isinstance(value, str) and value.startswith(HERE + "/") else value


def _rebase_instance(instance: dict, machine: str, base: str) -> dict:
    out = {**instance, "id": rebase_id(instance.get("id"), machine), "parent": rebase_id(instance.get("parent"), machine)}
    if out.get("page"):
        out["page"] = base + str(out["page"])
    return out


def rebase(data: dict, machine: str) -> dict | None:
    """The answer of the link machine to GET /api/map as a part of this
    machine's map: its own machine (the first: the others are its links,
    not ours) under the link's name, every id and page with it. None when
    the answer is not a map."""
    machines = data.get("machines") if isinstance(data, dict) else None
    if not isinstance(machines, list) or not machines or not isinstance(machines[0], dict):
        return None
    base = f"/links/{machine}"
    theirs = machines[0]
    if "workspaces" in theirs:
        node = {**theirs, "id": machine, "here": False, "name": machine,
                "workspaces": [{**ws, "id": rebase_id(ws.get("id"), machine), "page": base + str(ws.get("page") or ""),
                                "instances": [_rebase_instance(i, machine, base) for i in ws.get("instances") or []]}
                               for ws in theirs.get("workspaces") or [] if isinstance(ws, dict)],
                "loose": [_rebase_instance(i, machine, base) for i in theirs.get("loose") or []]}
    else:
        # part=now: its load and what is busy
        node = {**theirs, "id": machine, "here": False,
                "envs": {rebase_id(key, machine): value for key, value in (theirs.get("envs") or {}).items()}}

    def own(value) -> bool:
        return isinstance(value, str) and (value == HERE or value.startswith(HERE + "/"))

    def machine_of(value):
        # a transfer: its own machine is the link's; another of its links is not known here
        return machine if value == HERE else None

    jobs = [{**job, "id": f"{machine}:{job.get('id')}", "page": base + str(job.get("page") or ""),
             "instances": [rebase_id(i, machine) for i in job.get("instances") or []],
             "from": machine_of(job.get("from")), "to": machine_of(job.get("to"))}
            for job in data.get("jobs") or [] if isinstance(job, dict)]
    verdict = data.get("verdict") if isinstance(data.get("verdict"), dict) else {}
    watch = [{**w, "node": machine if w.get("node") == HERE else rebase_id(w.get("node"), machine),
              **({"page": base + str(w["page"])} if w.get("page") else {})}
             for w in verdict.get("watch") or [] if isinstance(w, dict)]
    flow = data.get("flow")
    if isinstance(flow, dict):
        links = {k: (base + v if v and k != "odoo" else v) for k, v in (flow.get("links") or {}).items()}
        flow = {**flow, "node": rebase_id(flow.get("node"), machine), "links": links,
                "stagings": [{**s, "id": rebase_id(s.get("id"), machine)} for s in flow.get("stagings") or []]}
    return {
        "machine": node, "jobs": jobs, "watch": watch,
        "tonight": data.get("tonight") if isinstance(data.get("tonight"), dict) else None,
        "edges": [{**e, "from": rebase_id(e["from"], machine), "to": rebase_id(e["to"], machine)}
                  for e in data.get("edges") or [] if isinstance(e, dict) and own(e.get("from")) and own(e.get("to"))],
        "databases": [{**d, "id": rebase_id(d.get("id"), machine), "machine": machine}
                      for d in data.get("databases") or [] if isinstance(d, dict)],
        "destinations": [{**d, "id": rebase_id(d.get("id"), machine), "machine": machine}
                         for d in data.get("destinations") or [] if isinstance(d, dict)],
        "flow": flow if isinstance(flow, dict) else None,
    }


def link_stagings(machines: list[dict]) -> list[dict]:
    """The stagings whose production is on another machine of the map tied
    to it: each machine says its own name (machine_name) and each such
    staging the name of its production's machine (parent_machine), so the
    staging gets its parent's id and a line from it. Returns the lines."""
    by_name = {m["machine_name"]: m["id"] for m in machines if m.get("machine_name")}
    servers = {i["id"] for m in machines for ws in m.get("workspaces") or [] for i in ws.get("instances") or []}
    servers |= {i["id"] for m in machines for i in m.get("loose") or []}
    edges = []
    for machine in machines:
        for instance in [i for ws in machine.get("workspaces") or [] for i in ws.get("instances") or []] \
                + list(machine.get("loose") or []):
            there = by_name.get(instance.get("parent_machine") or "")
            if not there or there == machine["id"] or not instance.get("parent_server"):
                continue
            parent = instance_id(instance["parent_server"], there)
            if parent in servers:
                instance["parent"] = parent
                edges.append({"from": parent, "to": instance["id"], "kind": "staging", "count": 1})
    return edges


def tonight_sum(found: list[dict | None]) -> dict | None:
    """The counts of the last day's automatic backups of several machines; None when none gave them."""
    read = [t for t in found if isinstance(t, dict)]
    if not read:
        return None
    return {key: sum(int(t.get(key) or 0) for t in read) for key in ("ok", "failed", "unscheduled")}
