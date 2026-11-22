# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Monitoring with notices: every minute each live instance is asked for its
login page on its own port; each hour the disks of the backups and of the
workspaces are measured; each day the certificate of each workspace's names
is read. Who is told: the machine's (Options › Monitoring) and the
workspace's of the instance (its page).

An instance is down after DOWN_AFTER failed checks in a row while its
service says it runs (one stopped on purpose is stopped, not down); it is
told when it goes down and when it is up again. A disk with less than
DISK_MIN_FREE of room, a certificate that ends within CERT_DAYS: told once a
day while it lasts.

The last day of checks of each instance is kept (monitor.json next to the
servers file): its uptime and its answer times.
"""

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

from . import notify

_logger = logging.getLogger(__name__)
FILE = "monitor.json"
EVERY = 60
DOWN_AFTER = 3
TIMEOUT = 15
KEEP = 24 * 60  # checks kept per instance: a day
DISK_MIN_FREE = 0.10
DISK_EVERY = 3600
CERT_EVERY = 24 * 3600
CERT_DAYS = 14
SAVE_EVERY = 600


class Monitor:
    def __init__(self, settings, jobs, run_state, path: Path):
        self.settings, self.jobs, self.run_state, self.path = settings, jobs, run_state, path
        # {instance key: {"state", "since", "fails", "checks": [[t, ok, ms]], "told"}}
        self.instances: dict[str, dict] = {}
        # {what: {"problem", "told"}}: disks and certificates
        self.machine: dict[str, dict] = {}
        self._task: asyncio.Task | None = None
        self._last = {"disk": 0.0, "cert": 0.0, "save": time.time()}
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

    def save(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            tmp.write_text(json.dumps({"instances": self.instances, "machine": self.machine}), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            _logger.warning("monitor: cannot write %s: %s", self.path, exc)

    def view(self, key: str) -> dict:
        """What the page and the MCP show of an instance: its state, since
        when, the uptime of the last day, the last answer time."""
        found = self.instances.get(key) or {}
        checks = found.get("checks") or []
        day = [c for c in checks if c[0] >= time.time() - 86400]
        up = [c for c in day if c[1]]
        return {"state": found.get("state", "unknown"), "since": found.get("since"),
                "uptime_24h": round(100.0 * len(up) / len(day), 2) if day else None,
                "last_ms": checks[-1][2] if checks else None,
                "last_check": datetime.datetime.fromtimestamp(checks[-1][0]).isoformat(timespec="seconds")
                if checks else None, "checks_24h": len(day)}

    def problems(self) -> dict:
        """The machine's problems now: disks, certificates."""
        return {k: v["problem"] for k, v in self.machine.items() if v.get("problem")}

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

    async def tell(self, server: str | None, subject: str, text: str) -> None:
        recipients = self.recipients(server)
        if recipients:
            await notify.send(self.settings.options, recipients,
                              f"OmniaCLI {socket.gethostname()}: {subject}", text)

    # --- the checks ------------------------------------------------------

    def targets(self) -> list:
        return [inst for srv in self.settings.servers.values() if srv.active for inst in srv.instances.values()
                if inst.plugin is not None and inst.plugin.web_port(inst)]

    async def probe(self, client: httpx.AsyncClient, inst) -> tuple[bool, int | None, str]:
        """(answered, milliseconds, what went wrong)."""
        port = inst.plugin.web_port(inst)
        started = time.monotonic()
        try:
            resp = await client.get(f"http://127.0.0.1:{port}/web/login", timeout=TIMEOUT)
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
            if entry.get("state") == "down":
                entry.update(state="up", since=stamp)
                await self.tell(inst.server, f"{inst.key} is up again",
                                f"{inst.key} answers again ({ms} ms) since {stamp}.")
            elif entry.get("state") != "up":
                entry.update(state="up", since=stamp)
            return
        entry["fails"] = entry.get("fails", 0) + 1
        if entry["fails"] < DOWN_AFTER or entry.get("state") in ("down", "stopped"):
            return
        state = await self.run_state(inst)
        if state == "stopped":
            entry.update(state="stopped", since=stamp)
            return
        entry.update(state="down", since=stamp)
        await self.tell(inst.server, f"{inst.key} is down",
                        f"{inst.key} has not answered for {entry['fails']} minutes ({why}); its service says: {state}.\n"
                        f"Its address: {inst.web_url or '?'}")

    async def check_disks(self) -> None:
        folders = {"backups": Path(self.settings.backup_dir)}
        for name, root in (self.settings.storage_roots or {}).items():
            folders[f"storage {name}"] = Path(root)
        for what, folder in folders.items():
            try:
                usage = shutil.disk_usage(folder)
            except OSError:
                continue
            free = usage.free / usage.total if usage.total else 1.0
            problem = (f"{folder}: {usage.free >> 30} GB free of {usage.total >> 30} ({free:.0%})"
                       if free < DISK_MIN_FREE else "")
            await self.machine_problem(f"disk {what}", problem)

    async def check_certificates(self) -> None:
        for ws in self.settings.workspaces.values():
            if not ws.domain:
                continue
            names = {inst.site.removeprefix("https://") for n in ws.servers if n in self.settings.servers
                     for inst in self.settings.servers[n].instances.values() if inst.site}
            for name in sorted(names):
                days = await asyncio.to_thread(certificate_days, name)
                problem = (f"the certificate of {name} ends in {days} days" if days is not None and days < CERT_DAYS
                           else "")
                await self.machine_problem(f"certificate {name}", problem, server=ws.servers[0] if ws.servers else None)
                # the days left and when they were read, a problem or not: the map shows them (UX-009)
                self.machine[f"certificate {name}"].update(
                    days=days, checked=datetime.datetime.now().isoformat(timespec="seconds"))

    async def machine_problem(self, what: str, problem: str, server: str | None = None) -> None:
        entry = self.machine.setdefault(what, {})
        if not problem:
            entry.update(problem="", told=None)
            return
        today = datetime.date.today().isoformat()
        entry["problem"] = problem
        if entry.get("told") != today:
            entry["told"] = today
            await self.tell(server, problem, problem)

    async def round(self) -> None:
        async with httpx.AsyncClient(follow_redirects=False) as client:
            await asyncio.gather(*(self.check_instance(client, inst) for inst in self.targets()),
                                 return_exceptions=True)
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
