# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Monitoring with notices: every minute each live instance is asked for its
login page on its own port and the machine's CPU and memory are read from
the sampler's last minutes; every five minutes the disks of the backups and
of the workspaces are measured; each day the certificate of each
workspace's names is read. Who is told: the machine's (Options ›
Monitoring) and the workspace's of the instance (its page).

An instance is down after DOWN_AFTER failed checks in a row while its
service says it runs (one stopped on purpose, a staging stopped too, is
stopped, not down); it is told when it goes down and when it is up again.
With Options › Monitoring › Restart on, one that has not answered for
restart_after checks in a row is restarted, in a job of the system that
says why, never while another job works on it, at most once in
restart_every minutes (past that it is told, not restarted again).

The CPU and the memory in use above a threshold of Options (warning,
critical) for monitor_minutes in a row, a disk above its own: told when the
level is reached or rises, and when it is back under the warning; a disk
once a day while it lasts. A certificate that ends within CERT_DAYS
(critical within CERT_CRITICAL): told once a day.

Each notice has a level (info, warning, critical) and an event of
notify.EVENTS for the webhooks. The last day of checks of each instance is
kept (monitor.json next to the servers file): its uptime, its answer times,
its last automatic restart."""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import shutil
import socket
import ssl
import time
from pathlib import Path

import httpx
from cryptography import x509

from . import notify, ops, relay
from .jobs import JobFailed

_logger = logging.getLogger(__name__)
FILE = "monitor.json"
EVERY = 60
DOWN_AFTER = 3
TIMEOUT = 15
KEEP = 24 * 60  # checks kept per instance: a day
# the answer of a check taken while the instance is stopped on purpose: [t, None, ms], counted neither as an answer
# nor as a fall (the instance's monitor, AD-019)
STOPPED = None
DISK_EVERY = 300
CERT_EVERY = 24 * 3600
CERT_DAYS = 14
CERT_CRITICAL = 3
SAVE_EVERY = 600
# the share of the window the sampler's points must cover before a level is read from them
COVERED = 0.8
METRIC_LABELS = {"cpu": "CPU", "ram": "Memory"}
# the machine's problem of its SSH tunnel to a relay (AD-017 §17.6): never «relay <workspace>», a name of a workspace
AGENT_RELAY = "agent-relay"


# the monitor's own check, in its query string: the monitor of an instance does not count it among its requests
# (odoolog.PROBE)
PROBE = "omniacli-monitor"
# the causes of an instance read in its log that are told (Carlo, 26/11/2026: at once, grouped by cause); an
# instance leaves a cause after CAUSE_CLEAR rounds of the log (5 minutes each) without it
ALERTED_CAUSES = ("slow", "errors", "killed")
CAUSE_CLEAR = 3
CAUSE_SUBJECT_ONE = {"slow": "{key} is slow", "errors": "{key} fails requests",
                     "killed": "{key} had workers stopped for memory"}
CAUSE_SUBJECTS = {"slow": "{n} instances are slow", "errors": "{n} instances fail requests",
                  "killed": "{n} instances had workers stopped for memory"}
CAUSE_HEADS = {"slow": "Slow: half the requests over 1 s, or 1 in 20 over 3 s, in the last 15 minutes.",
               "errors": "Failed requests: over 1% of them ended with an error, in the last 15 minutes.",
               "killed": "Workers stopped for memory in the last 15 minutes: who was on them got an error."}
CAUSE_LINES = {"slow": "{key}: half within {median} s, 1 in 20 over {p95} s ({requests} requests)",
               "errors": "{key}: {percent}% failed ({failed} of {requests})",
               "killed": "{key}: {count} workers stopped"}
NEW_MARK = "(new)"


def _cause_numbers(cause: dict) -> dict:
    """The numbers of a cause its message says, never a text of the log."""
    return {k: cause.get(k) for k in ("median", "p95", "requests", "percent", "failed", "count")}


def level_of(value: float, limits) -> str | None:
    """critical, warning or None for a percent and its [warning, critical]."""
    if value >= limits[1]:
        return "critical"
    if value >= limits[0]:
        return "warning"
    return None


class Monitor:
    def __init__(self, settings, jobs, run_state, path: Path, sampler=None, audit=None, workspace_of=None):
        self.settings, self.jobs, self.run_state, self.path = settings, jobs, run_state, path
        # the machine's points (metrics.Sampler.machine), audit(text): the access log's line of a restart,
        # workspace_of(server): the workspace a job of it belongs to
        self.sampler, self.audit, self.workspace_of = sampler, audit, workspace_of
        # {instance key: {"state", "since", "fails", "checks": [[t, ok, ms]], "restart_at", "restarted", ...}}
        self.instances: dict[str, dict] = {}
        # {what: {"problem", "level", "told", "since"}}: disks, certificates, the load (cpu, ram)
        self.machine: dict[str, dict] = {}
        # {cause: {instance key: {"since", "clear", "cause"}}}: the instances in each cause of their log, told once
        self.causes: dict[str, dict] = {}
        self._task: asyncio.Task | None = None
        self._last = {"disk": 0.0, "cert": 0.0, "save": time.time()}
        self._restarts: set[asyncio.Task] = set()
        # {workspace: checks in a row its relay's tunnel was not connected}
        self._relay_misses: dict[str, int] = {}
        self.load()

    # --- state -----------------------------------------------------------

    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(data, dict):
            self.instances = {k: v for k, v in (data.get("instances") or {}).items() if isinstance(v, dict)}
            self.machine = {k: v for k, v in (data.get("machine") or {}).items() if isinstance(v, dict)}
            self.causes = {k: v for k, v in (data.get("causes") or {}).items() if isinstance(v, dict)}

    def save(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            tmp.write_text(json.dumps({"instances": self.instances, "machine": self.machine, "causes": self.causes}), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            _logger.warning("monitor: cannot write %s: %s", self.path, exc)

    def view(self, key: str) -> dict:
        """What the page and the MCP show of an instance: its state, since
        when, the uptime of the last day, the last answer time, its last
        automatic restart."""
        found = self.instances.get(key) or {}
        checks = found.get("checks") or []
        # a check taken while it was stopped on purpose (STOPPED) is neither an answer nor a fall
        day = [c for c in checks if c[0] >= time.time() - 86400 and c[1] is not STOPPED]
        up = [c for c in day if c[1]]
        return {"state": found.get("state", "unknown"), "since": found.get("since"),
                "uptime_24h": round(100.0 * len(up) / len(day), 2) if day else None,
                "last_ms": checks[-1][2] if checks else None,
                "last_check": datetime.datetime.fromtimestamp(checks[-1][0]).isoformat(timespec="seconds")
                if checks else None, "checks_24h": len(day),
                "restarted": found.get("restarted"), "restart_job": found.get("restart_job")}

    def problems(self) -> dict:
        """The machine's problems now: disks, certificates, the load."""
        return {k: v["problem"] for k, v in self.machine.items() if v.get("problem")}

    def alerts(self) -> list[dict]:
        """The machine's problems now with their level and since when:
        [{"what", "problem", "level", "since"}], the critical first."""
        found = [{"what": k, "problem": v["problem"], "level": v.get("level") or "warning", "since": v.get("since")}
                 for k, v in self.machine.items() if v.get("problem")]
        return sorted(found, key=lambda a: (a["level"] != "critical", a["what"]))

    def certificate(self, name: str) -> dict | None:
        """{days, checked} of the certificate of name as the daily check read
        it, None when it was not read (no check yet, or no https there)."""
        found = self.machine.get(f"certificate {name}") or {}
        if found.get("days") is None or not found.get("checked"):
            return None
        return {"days": found["days"], "checked": found["checked"]}

    # --- who is told -----------------------------------------------------

    def recipients(self, server: str | None) -> list[str]:
        """The machine's, and those of the workspaces server is in."""
        found = list(self.settings.options.monitor_notify)
        if server is not None:
            for ws in self.settings.workspaces.values():
                if server in ws.servers:
                    found += ws.notify
        return list(dict.fromkeys(found))

    async def tell(self, server: str | None, subject: str, text: str, event: dict | None = None) -> None:
        recipients = self.recipients(server)
        if recipients:
            level = (event or {}).get("level") or "warning"
            await notify.send(self.settings.options, recipients,
                              f"OmniaCLI {socket.gethostname()}: [{level}] {subject}", text, event=event)

    # --- the causes of the instances read in their log (AD-019 slice 2) ---

    async def instance_causes(self, found: dict[str, list[dict]], measured: set | None = None) -> None:
        """The causes of now of the instances whose log was read in a round ({key: [cause]}, instancemonitor.
        log_causes): an instance enters a cause when it has it, and leaves it after CAUSE_CLEAR rounds without.
        When instances enter a cause, ONE message for that cause tells every instance in it, the new ones marked
        (12 productions slow at once are one email, not 12); one already in it is not told again while it lasts.
        Each workspace's people are told of their own instances only; the machine's of all. measured: the
        instances measured now; one that is not any more (deleted, switched off, a staging stopped) leaves every
        cause at once, never named again in a message of the others."""
        stamp = datetime.datetime.now().isoformat(timespec="seconds")
        changed = False
        for kind in ALERTED_CAUSES:
            entry = self.causes.setdefault(kind, {})
            if measured is not None:
                for key in [k for k in entry if k not in measured]:
                    del entry[key]
                    changed = True
            entered = []
            for key, causes in found.items():
                cause = next((c for c in causes if c.get("kind") == kind), None)
                if cause is not None:
                    if key not in entry:
                        entered.append(key)
                        entry[key] = {"since": stamp}
                    entry[key].update(clear=0, cause=_cause_numbers(cause))
                elif key in entry:
                    entry[key]["clear"] = entry[key].get("clear", 0) + 1
                    if entry[key]["clear"] >= CAUSE_CLEAR:
                        del entry[key]
                        changed = True
            if entered:
                changed = True
                await self.tell_cause(kind, entry, entered)
        self.causes = {k: v for k, v in self.causes.items() if v}
        if changed:
            # kept at once: after a crash a cause already told is not told again
            await asyncio.to_thread(self.save)

    async def tell_cause(self, kind: str, entry: dict, entered: list[str]) -> None:
        """One message for the cause kind to each group of recipients that reads the same instances."""
        shown: dict[str, set] = {}
        for key in entry:
            for recipient in self.recipients(key.partition("/")[0]):
                shown.setdefault(recipient, set()).add(key)
        groups: dict[frozenset, list[str]] = {}
        for recipient, keys in shown.items():
            if keys & set(entered):
                groups.setdefault(frozenset(keys), []).append(recipient)
        level = "warning"
        for keys, recipients in groups.items():
            listed = sorted(keys)
            subject = CAUSE_SUBJECTS[kind].format(n=len(listed)) if len(listed) > 1 else \
                CAUSE_SUBJECT_ONE[kind].format(key=listed[0])
            lines = [CAUSE_LINES[kind].format(key=key, **entry[key].get("cause", {}))
                     + (" " + NEW_MARK if key in entered and len(listed) > 1 else "") for key in listed]
            text = "\n".join([CAUSE_HEADS[kind], *lines])
            await notify.send(self.settings.options, recipients,
                              f"OmniaCLI {socket.gethostname()}: [{level}] {subject}", text,
                              event={"event": "instances.cause", "level": level, "cause": kind, "instances": listed,
                                     "new": sorted(set(entered) & keys)})

    # --- the checks ------------------------------------------------------

    def targets(self) -> list:
        return [inst for srv in self.settings.servers.values() if srv.active for inst in srv.instances.values()
                if inst.plugin is not None and inst.plugin.web_port(inst)]

    async def probe(self, client: httpx.AsyncClient, inst) -> tuple[bool, int | None, str]:
        """(answered, milliseconds, what went wrong)."""
        port = inst.plugin.web_port(inst)
        started = time.monotonic()
        try:
            resp = await client.get(f"http://127.0.0.1:{port}/web/login?{PROBE}=1", timeout=TIMEOUT)
        except httpx.HTTPError as exc:
            return False, None, type(exc).__name__
        ms = int((time.monotonic() - started) * 1000)
        return resp.status_code < 500, ms, f"HTTP {resp.status_code}"

    async def check_instance(self, client: httpx.AsyncClient, inst) -> None:
        if self.jobs.is_busy(inst.key):
            return  # a restart, an update, a restore: not a fall
        ok, ms, why = await self.probe(client, inst)
        entry = self.instances.setdefault(inst.key, {"state": "unknown", "fails": 0, "checks": []})
        now = time.time()
        entry["checks"] = (entry.get("checks") or [])[-(KEEP - 1):] + [[int(now), ok, ms]]
        stamp = datetime.datetime.now().isoformat(timespec="seconds")
        if ok:
            entry["fails"] = 0
            entry.pop("held_told", None)
            if entry.get("state") == "down":
                entry.update(state="up", since=stamp)
                await self.tell(inst.server, f"{inst.key} is up again",
                                f"{inst.key} answers again ({ms} ms) since {stamp}.",
                                {"event": "instance.up", "level": "info", "instance": inst.key, "ms": ms})
            elif entry.get("state") != "up":
                entry.update(state="up", since=stamp)
            return
        entry["fails"] = entry.get("fails", 0) + 1
        if entry.get("state") == "stopped":
            entry["checks"][-1][1] = STOPPED
            return
        if entry.get("state") == "down":
            # down, then stopped on purpose (or found stopped after OmniaCLI was away): stopped from now, not
            # a fall that lasts
            if await self.run_state(inst) == "stopped":
                entry.update(state="stopped", since=stamp)
                entry["checks"][-1][1] = STOPPED
                return
        else:
            if entry["fails"] < DOWN_AFTER:
                return
            state = await self.run_state(inst)
            if state == "stopped":
                # the checks of this run without an answer were of an instance stopped on purpose
                entry.update(state="stopped", since=stamp)
                for check in entry["checks"][-entry["fails"]:]:
                    if not check[1]:
                        check[1] = STOPPED
                return
            entry.update(state="down", since=stamp)
            await self.tell(inst.server, f"{inst.key} is down",
                            f"{inst.key} has not answered for {entry['fails']} minutes ({why}); its service says: {state}.\n"
                            f"Its address: {inst.web_url or '?'}",
                            {"event": "instance.down", "level": "critical", "instance": inst.key, "reason": why,
                             "checks": entry["fails"]})
        options = self.settings.options
        if options.restart_down and entry["fails"] >= options.restart_after:
            await self.restart(inst, entry, why)

    async def restart(self, inst, entry: dict, why: str) -> None:
        """The automatic restart of inst, down for entry["fails"] checks:
        at most once in restart_every minutes, never during another job on
        it, never when its service says it was stopped (on purpose)."""
        options, now = self.settings.options, time.time()
        if now - float(entry.get("restart_at") or 0) < options.restart_every * 60:
            if not entry.get("held_told"):
                entry["held_told"] = True
                await self.tell(inst.server, f"{inst.key} is not restarted again",
                                f"{inst.key} still does not answer ({why}) after its automatic restart of "
                                f"{entry.get('restarted') or '?'}: it is restarted at most once every "
                                f"{options.restart_every} minutes.",
                                {"event": "instance.restart_held", "level": "critical", "instance": inst.key,
                                 "restarted": entry.get("restarted")})
            return
        if self.jobs.is_busy(inst.key) or await self.run_state(inst) == "stopped":
            return
        checks = entry["fails"]
        reason = f"no answer for {checks} checks in a row ({why})"
        stamp = datetime.datetime.now().isoformat(timespec="seconds")
        # the count starts again: the next restart only after as many failed checks
        entry.update(restart_at=now, restarted=stamp, fails=0, held_told=False, restart_reason=reason)

        async def action(job):
            job.log(f"--- {inst.key}: {reason}; Options › Monitoring restarts it")
            async with httpx.AsyncClient(follow_redirects=False) as client:
                answered, ms, _ = await self.probe(client, inst)
            if answered:
                job.log(f"{inst.key} answers again ({ms} ms): not restarted")
                return
            try:
                await ops.restart(job, inst)
            except JobFailed as exc:
                await self.tell(inst.server, f"{inst.key} could not be restarted",
                                f"The automatic restart of {inst.key} failed: {exc}\nJob {job.id}.",
                                {"event": "instance.restart_failed", "level": "critical", "instance": inst.key,
                                 "job": job.id, "reason": str(exc)[:500]})
                raise
            job.log(f"{inst.key} restarted: the monitor checks whether it answers in a minute")

        ws = self.workspace_of(inst.server) if self.workspace_of else ""
        job = self.jobs.submit(f"Automatic restart of {inst.key}", [inst.key], f"system: monitor, {reason}",
                               action, workspace=ws or "")
        entry["restart_job"] = job.id
        if self.audit is not None:
            self.audit(f"automatic restart of {inst.key}: {reason} (job {job.id})")
        await self.tell(inst.server, f"{inst.key} restarted automatically",
                        f"{inst.key} had not answered for {checks} checks in a row ({why}): restarted in job {job.id}.",
                        {"event": "instance.restarted", "level": "warning", "instance": inst.key, "job": job.id,
                         "checks": checks, "reason": why})

    def window(self, metric: str) -> list[float] | None:
        """The percent of metric (cpu, ram) of the sampler's points of the
        last monitor_minutes; None when they do not cover enough of them."""
        if self.sampler is None:
            return None
        seconds = self.settings.options.monitor_minutes * 60
        since = time.time() - seconds
        values, first = [], None
        for point in list(self.sampler.machine):
            if point.get("t", 0) < since:
                continue
            if metric == "cpu":
                value = point.get("cpu")
            else:
                value = 100.0 * point["ram"] / point["ram_total"] if point.get("ram") and point.get("ram_total") else None
            if value is not None:
                values.append(float(value))
                first = point["t"] if first is None else first
        if len(values) < 2 or first is None or time.time() - first < seconds * COVERED:
            return None
        return values

    async def check_load(self) -> None:
        """CPU and memory: a level when every point of the window is above it,
        cleared when every point is under the warning."""
        options = self.settings.options
        for metric in ("cpu", "ram"):
            values = self.window(metric)
            if values is None:
                continue
            limits = options.monitor_thresholds[metric]
            entry = self.machine.setdefault(f"load {metric}", {})
            level = level_of(min(values), limits)
            mean = round(sum(values) / len(values))
            label = METRIC_LABELS[metric]
            if level is not None:
                text = (f"{label} of the machine at {mean}% for {options.monitor_minutes} min "
                        f"(threshold {limits[0] if level == 'warning' else limits[1]}%)")
                rises = entry.get("level") != level and (entry.get("level") != "critical")
                entry.update(problem=text, level=level, since=entry.get("since") or datetime.datetime.now().isoformat(
                    timespec="seconds"), value=mean)
                if rises:
                    await self.tell(None, text, text, {"event": "machine.threshold", "level": level, "metric": metric,
                                                       "value": mean, "warning": limits[0], "critical": limits[1],
                                                       "minutes": options.monitor_minutes})
            elif entry.get("level") and max(values) < limits[0]:
                text = f"{label} of the machine back under {limits[0]}% ({mean}%)"
                entry.clear()
                await self.tell(None, text, text, {"event": "machine.threshold.cleared", "level": "info",
                                                   "metric": metric, "value": mean, "warning": limits[0]})

    async def check_disks(self) -> None:
        folders = {"backups": Path(self.settings.backup_dir)}
        for name, root in (self.settings.storage_roots or {}).items():
            folders[f"storage {name}"] = Path(root)
        limits = self.settings.options.monitor_thresholds["disk"]
        for what, folder in folders.items():
            try:
                usage = shutil.disk_usage(folder)
            except OSError:
                continue
            used = 100.0 * (usage.total - usage.free) / usage.total if usage.total else 0.0
            level = level_of(used, limits)
            problem = (f"{folder}: {usage.free >> 30} GB free of {usage.total >> 30} ({used:.0f}% in use)"
                       if level else "")
            await self.machine_problem(f"disk {what}", problem, level=level,
                                       event={"event": "machine.threshold", "metric": "disk", "folder": what,
                                              "value": round(used), "warning": limits[0], "critical": limits[1]})

    async def check_certificates(self) -> None:
        for ws in self.settings.workspaces.values():
            if not ws.domain:
                continue
            names = {inst.site.removeprefix("https://") for n in ws.servers if n in self.settings.servers
                     for inst in self.settings.servers[n].instances.values() if inst.site}
            for name in sorted(names):
                days = await asyncio.to_thread(certificate_days, name)
                ending = days is not None and days < CERT_DAYS
                problem = f"the certificate of {name} ends in {days} days" if ending else ""
                await self.machine_problem(f"certificate {name}", problem, server=ws.servers[0] if ws.servers else None,
                                           level=("critical" if days < CERT_CRITICAL else "warning") if ending else None,
                                           event={"event": "certificate.ending", "name": name, "days": days})
                # the days left and when they were read, a problem or not: the map shows them (UX-009)
                self.machine[f"certificate {name}"].update(
                    days=days, checked=datetime.datetime.now().isoformat(timespec="seconds"))

    async def machine_problem(self, what: str, problem: str, server: str | None = None, level: str | None = None,
                              event: dict | None = None) -> None:
        """A problem of the machine: told once a day while it lasts, and at
        once when its level rises."""
        entry = self.machine.setdefault(what, {})
        if not problem:
            entry.update(problem="", told=None, level=None, since=None)
            return
        level = level or "warning"
        today = datetime.date.today().isoformat()
        rises = entry.get("level") == "warning" and level == "critical"
        entry["problem"] = problem
        entry["since"] = entry.get("since") or datetime.datetime.now().isoformat(timespec="seconds")
        entry["level"] = level
        if entry.get("told") != today or rises:
            entry["told"] = today
            await self.tell(server, problem, problem, {**(event or {"event": "notice"}), "level": level})

    async def check_relays(self) -> None:
        """The tunnel of each workspace published through another machine
        (relay.py): not connected for two checks in a row (its unit restarts
        it every few seconds: a drop it gets over is not told), a problem of
        the machine, «relay <workspace>», told as the others; gone when the
        tunnel is connected again or the publication taken away."""
        # a publication taken away here whose relay did not take its part back yet ("left") has no tunnel
        records = {k: v for k, v in relay.load_records(self.settings.servers_file.parent / relay.RECORDS_FILE).items()
                   if not v.get("left")}
        for what in [w for w in self.machine if w.startswith("relay ") and w[6:] not in records]:
            del self.machine[what]
        for name, rec in records.items():
            state = await asyncio.to_thread(relay.read_tunnel, name, int(rec.get("restarts_base") or 0))
            down = state["state"] != "connected"
            self._relay_misses[name] = self._relay_misses.get(name, 0) + 1 if down else 0
            if down and self._relay_misses[name] < 2:
                continue  # a first miss: what was said before stays (a restart of OmniaCLI counts again)
            problem = ""
            if down:
                # the same words while it lasts: since when is the entry's own
                problem = f"the tunnel of workspace {name} to {rec.get('link')} is not connected: its names do not open"
            await self.machine_problem(f"relay {name}", problem, server=None, level="warning",
                                       event={"event": "relay.down", "workspace": name, "relay": rec.get("link"),
                                              "state": state["state"]})
        await self.check_agent_relay()

    async def check_agent_relay(self) -> None:
        """This machine's SSH published through a relay for its central panel (AD-017 §17.6): its tunnel not
        connected for two checks in a row is a problem of the machine, «agent-relay», told as the workspaces'
        ones (event relay.down with agent: true); gone when it is connected again or the publication taken away."""
        try:
            rec = json.loads((self.settings.servers_file.parent / relay.AGENT_RECORD).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            rec = None
        if not isinstance(rec, dict) or not rec.get("link"):
            self.machine.pop(AGENT_RELAY, None)
            self._relay_misses.pop(AGENT_RELAY, None)
            return
        state = await asyncio.to_thread(relay.read_tunnel, "", int(rec.get("restarts_base") or 0), relay.AGENT_UNIT)
        down = state["state"] != "connected"
        self._relay_misses[AGENT_RELAY] = self._relay_misses.get(AGENT_RELAY, 0) + 1 if down else 0
        if down and self._relay_misses[AGENT_RELAY] < 2:
            return
        problem = (f"this machine's SSH tunnel to {rec.get('link')} is not connected: the central panel does not "
                   "reach it") if down else ""
        await self.machine_problem(AGENT_RELAY, problem, server=None, level="warning",
                                   event={"event": "relay.down", "agent": True, "relay": rec.get("link"),
                                          "state": state["state"]})

    async def round(self) -> None:
        async with httpx.AsyncClient(follow_redirects=False) as client:
            await asyncio.gather(*(self.check_instance(client, inst) for inst in self.targets()),
                                 return_exceptions=True)
        await self.check_load()
        await self.check_relays()
        now = time.time()
        if now - self._last["disk"] >= DISK_EVERY:
            self._last["disk"] = now
            await self.check_disks()
        if now - self._last["cert"] >= CERT_EVERY:
            self._last["cert"] = now
            await self.check_certificates()
        if now - self._last["save"] >= SAVE_EVERY:
            self._last["save"] = now
            await asyncio.to_thread(self.save)

    async def _loop(self) -> None:
        while True:
            try:
                await self.round()
            except Exception:  # noqa: BLE001 - the loop must survive
                _logger.exception("monitor round failed")
            await asyncio.sleep(EVERY)

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        await asyncio.to_thread(self.save)


def certificate_days(name: str, port: int = 443) -> int | None:
    """Days left of the certificate nginx gives for name here; None when it
    cannot be read (no https yet)."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=10) as sock, \
                context.wrap_socket(sock, server_hostname=name) as tls:
            der = tls.getpeercert(binary_form=True)
    except OSError:
        return None
    if not der:
        return None
    cert = x509.load_der_x509_certificate(der)
    ends = cert.not_valid_after_utc if hasattr(cert, "not_valid_after_utc") else cert.not_valid_after.replace(
        tzinfo=datetime.timezone.utc)
    return (ends - datetime.datetime.now(datetime.timezone.utc)).days
