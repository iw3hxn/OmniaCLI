# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the audit log of NIS 2 records of the pages and data read, and what
the machine page says of it (docs/nis2-audit.md).

Every request that changes something is logged (app.py, audit_actions); of
those that only read (GET), the reads of customer data or of security data:
SENSITIVE_READS, by route template. Every GET route is in one of the three
lists below: a route added later and in none fails
tests/test_audit.py::test_every_read_is_classified, so whoever adds it
decides.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

# a read of customer data or of security data: logged as "read", with what it is
SENSITIVE_READS = {
    # customer data
    "/api/servers/{server}/{role}/log": "instance log",
    "/api/servers/{server}/{role}/log/records": "instance log (records, a span of time)",
    "/api/servers/{server}/{role}/database": "database statistics",
    "/api/servers/{server}/{role}/login-as/users": "users of the instance's database",
    "/api/servers/{server}/production/mail": "caught mail (list)",
    "/api/servers/{server}/production/mail/{mail_id}": "caught mail (message)",
    "/api/servers/{server}/production/mail/inbox": "caught mail (reception state, mailboxes)",
    "/api/servers/{server}/{role}/backups": "backups list",
    "/api/servers/{server}/{role}/remote-backups": "backups list (destination)",
    "/api/backups": "backups list (every machine)",
    "/api/v1/instances/{server}/{role}/backups": "backups list (REST API)",
    "/api/workspaces/{workspace}/archives": "archives list",
    "/api/workspaces/{workspace}/instances/from-backup": "plan of a new instance from a backup (its database's name and modules)",
    "/api/workspaces/{workspace}/archives/remote": "archives list (destination)",
    "/api/pullers/{name}/reports": "relayed reports",
    # security data
    "/access": "access log",
    "/api/access/verify": "access log (chain check)",
    "/api/access/export": "access log (export)",
    "/workspaces/{workspace}/audit": "access log of the workspace",
    "/api/workspaces/{workspace}/audit/export": "access log of the workspace (export)",
    "/api/omniacli/log": "OmniaCLI's own log",
    "/credentials": "git credentials",
    "/enterprise": "Odoo Enterprise access (its address, its SSH key)",
    "/x/odoo/servers/{server}/{role}/odoo-config": "Odoo configuration file",
    "/api/machine/ssh-keys": "SSH keys of the machine",
    "/api/access/users/sessions": "sessions of a user (addresses, browsers)",
    # the list of the workspaces (UX-013 §A13): their members, each with its two-step verification
    "/api/workspaces/list": "workspaces' members and their two-step verification",
}

# reads the route logs itself, with more than a read would say
AUDITED_BY_ROUTE = frozenset({
    "/backups/{server}/{role}/{name}",  # download, with the file
    "/api/pullers/{name}/files/{file}",  # download, with the file
    "/mcp",  # every MCP call, with its method and tool
    "/api/v1/instances/{server}/{role}/backups/{file}",  # download (REST API), with the file
})

# pages and data that are neither customer data nor security data: the
# shells' pages (the shell itself is logged when it opens and closes), the
# lists of servers and jobs, the settings, the load
NOT_SENSITIVE = frozenset({
    "/favicon.ico", "/icon-180.png", "/login-background.jpg", "/login", "/.omniacli/login", "/language/{code}", "/about",
    # the second step of a login (its outcome is logged by the route)
    "/login/code", "/login/setup",
    # the single sign-on of a workspace (sso.py): the way to its provider and back; the outcome
    # (login, refused, a user made) the routes log
    "/sso/{workspace}/start", "/sso/{workspace}/callback",
    # OAuth (oauth.py): the public metadata of the discovery, the authorization request and the consent
    # page; what they give (consent given or refused, token issued, refreshed, revoked) the routes log
    "/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp",
    "/.well-known/oauth-authorization-server", "/oauth/authorize", "/oauth/consent",
    "/", "/dashboard/servers", "/servers/{server}", "/api/info", "/api/portal", "/api/servers", "/api/metrics",
    "/api/metrics/history", "/api/disks", "/api/disks/history", "/disks",
    "/api/servers/{server}/{role}/uptime", "/api/servers/{server}/{role}/summary",
    "/api/servers/{server}/{role}/pull-preview", "/api/servers/{server}/{role}/backup/choices",
    "/api/servers/{server}/{role}/backup/estimate", "/api/servers/{server}/{role}/restore/room",
    "/x/{plugin}/static/{path:path}", "/servers/{server}/{role}/log", "/omniacli/log",
    "/jobs", "/jobs/{job_id}", "/api/jobs", "/api/jobs/{job_id}",
    "/config", "/config/new", "/config/{name}/edit", "/config/{name}/clone", "/config/discover",
    "/api/servers/discover", "/workspaces", "/api/templates/available", "/api/workspaces/load",
    "/api/workspaces/{workspace}/load", "/api/workspaces/space", "/workspaces/{workspace}",
    "/api/instances/versions", "/api/servers/{server}/stagings", "/api/servers/{server}/stagings/new",
    "/api/servers/{server}/stagings/elsewhere", "/api/stagings/host", "/api/servers/{server}/pitr",
    "/api/servers/{server}/{role}/addresses", "/api/catalog", "/api/servers/{server}/modules",
    # the publication through another machine (relay.py): its state, the relays, what a relay hosts
    "/api/servers/{server}/{role}/relay", "/api/relays", "/api/relay/facts", "/api/relay/hosted",
    "/api/servers/{server}/modules/catalog", "/api/servers/{server}/modules/plan",
    "/api/servers/{server}/{role}/modules", "/api/servers/{server}/modules/uploaded", "/users", "/help", "/plugins", "/api/plugins/available",
    "/links", "/machines", "/api/machines", "/links/{name}",
    # through a link: the linked OmniaCLI logs it, with who acts here
    "/links/{name}/{path:path}",
    "/static/{name}", "/servers/{server}/{role}/shell", "/servers/{server}/{role}/claude",
    "/servers/{server}/{role}/psql", "/servers/{server}/{role}/terminal", "/tunnels", "/api/tunnels", "/groups",
    "/api/pullers", "/api/pullers/{name}", "/pullers", "/pullers/connections/{name}", "/api/connections/{name}",
    "/api/connections/{name}/metrics", "/api/connections/{name}/jobs/{job_id}", "/settings", "/options",
    "/schedules", "/api/schedules/next", "/api/servers/{server}/{role}/backup-schedule",
    "/api/servers/{server}/{role}/deploy-hook", "/api/servers/{server}/{role}/deploys",
    # the cards of «Environments and deploys» (UX-015): the deploys of the environments, on this machine and on a link
    "/api/servers/{server}/{role}/environments", "/api/servers/{server}/stagings/elsewhere/environments",
    "/api/servers/{server}/{role}/zero-downtime",
    "/api/servers/{server}/{role}/deploys/{deploy_id}/back", "/destinations", "/workspaces/{workspace}/destinations",
    "/instances", "/api/instances", "/backups", "/account", "/api/postgres",
    # the map of the infrastructure (UX-009): the facts of /api/servers and /api/instances as a
    # diagram; of the caught mail the count only, no list of backups, no text, no log
    "/api/map",
    "/api/postgres/{server}/{role}/proposal",
    # a database on the cluster of a linked OmniaCLI (AD-003): the clusters a machine offers and the
    # round trip to it, read before the grant; the grant, its revoke and its drop are actions, logged
    "/api/postgres/machine/offer", "/api/links/{name}/postgres",
    # a read replica of the machine's cluster on another machine (AD-004): what is said before it is
    # made (sizes, the names of the instances, to the machine's admins and the link that asks) and
    # what each machine sees of it; making, dropping, promoting and the reads are actions, logged
    "/api/postgres/machine/{major}/replica/facts", "/api/postgres/machine/{major}/replica/state",
    "/api/links/{name}/postgres/replica", "/api/postgres/replica/sources",
    # the return after a promotion (UX-011 §A): what is said before it is prepared (sizes, the names
    # of the instances, to the machine's admins and the link that asks) and what the promoted copy
    # sees of the cluster that comes back; preparing, completing and cancelling are actions, logged
    "/api/postgres/machine/{major}/return/facts", "/api/postgres/replica/{major}/return/facts",
    "/api/postgres/replica/{major}/return/state",
    # where a database another machine uses is now (UX-011 §B), asked by that machine every few seconds
    # through its link: the first time each place is told the route logs it itself, as an action
    "/api/postgres/machine/{major}/grants/{name}/place",
    "/x/odoo/servers/{server}/{role}/addons", "/x/odoo/servers/{server}/{role}/addons/versions",
    # an invitation's form (its use is logged by the route) and where a user of a customer may be made
    "/invite/{token}", "/api/access/workspaces", "/api/access/workspace-choices",
    # the REST API (README, REST API): its document, and the lists of instances, jobs and deploys, as the pages'
    "/api/v1/openapi.json", "/api/v1/docs", "/api/v1/instances", "/api/v1/instances/{server}/{role}", "/api/v1/jobs",
    "/api/v1/jobs/{job_id}", "/api/v1/jobs/{job_id}/log", "/api/v1/instances/{server}/{role}/deploys",
    # the bell (README, The bell): the map's things to look at, as /api/map says them; the panel's logo
    # (Whitelabel), public as the icons; whether the system updates itself and its last run (no package
    # list of the machine, no log of anybody's), the switch is an action, logged
    "/api/bell", "/brand/logo", "/api/machine/os-updates",
    # a database uploaded from the Backups page: where it may go (workspaces, sizes) and how much of an upload
    # is here to resume it; the parts sent, the file kept or refused and its deletion are actions, logged
    "/api/imports/places", "/api/workspaces/{workspace}/imports/upload",
})


def route_template(scope: dict) -> str:
    """The template of the route a request reached ("" when none): a
    plugin's own routes with their /x/<plugin> prefix, whichever FastAPI
    keeps it in the route."""
    route = scope.get("route")
    template = getattr(route, "path", None) or ""
    path = scope.get("path", "")
    if template and path.startswith("/x/") and not template.startswith("/x/"):
        plugin = path.split("/", 3)[2]
        template = f"/x/{plugin}{template}"
    return template


# the system clock (timedatectl, else chrony), asked at most every CLOCK_TTL
CLOCK_TTL = 300  # seconds
_clock: dict = {}


def _ask(command: list[str]) -> str | None:
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def clock_status() -> dict:
    """{"synced": True/False/None (unknown), "source", "detail"}."""
    now = time.monotonic()
    if _clock and now - _clock["at"] < CLOCK_TTL:
        return _clock["status"]
    status = {"synced": None, "source": "", "detail": "neither timedatectl nor chronyc answered"}
    answer = _ask(["timedatectl", "show", "--property=NTPSynchronized", "--property=NTP"])
    if answer:
        values = dict(line.split("=", 1) for line in answer.splitlines() if "=" in line)
        if "NTPSynchronized" in values:
            synced = values["NTPSynchronized"].strip() == "yes"
            status = {"synced": synced, "source": "timedatectl",
                      "detail": f"NTPSynchronized={values['NTPSynchronized'].strip()}, NTP={values.get('NTP', '?').strip()}"}
    if status["synced"] is not True:
        tracking = _ask(["chronyc", "-n", "tracking"])
        if tracking:
            leap = next((line.split(":", 1)[1].strip() for line in tracking.splitlines()
                         if line.lower().startswith("leap status")), "")
            if leap:
                status = {"synced": leap.lower() == "normal", "source": "chrony", "detail": f"Leap status: {leap}"}
    _clock.update(at=now, status=status)
    return status


# the room: warned when what the period still needs, twice, is not free, or
# less than ROOM_MIN_FREE is
ROOM_MIN_FREE = 512 * 1024 * 1024


def room(span: dict, retention_days: int, folder: Path) -> dict:
    """Whether the disk of folder holds the access log for its period, at
    the pace it grew so far. span: AccessLog.span()."""
    try:
        free = shutil.disk_usage(folder).free
    except OSError:
        free = None
    now = time.time_ns()
    first = span.get("first") or now
    days = max((now - first) / 86400e9, 1.0)
    per_day = span["bytes"] / days
    needed = max(0, int(per_day * max(retention_days, 0) - span["bytes"]))
    low = free is not None and (free < 2 * needed or free < ROOM_MIN_FREE)
    return {"bytes": span["bytes"], "files": span["files"], "first": span.get("first"), "days": round(days, 1),
            "per_day": int(per_day), "needed": needed, "free": free, "low": low}
