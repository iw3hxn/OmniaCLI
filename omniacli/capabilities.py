# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a person may do in a workspace, finer than its role (README,
Custom roles of a workspace): a short fixed list of capabilities, each role
(user, developer, admin) a preset of them, and the custom roles a
workspace's admins make from the presets with capabilities ticked off or on.

- Every route a user of workspaces reaches (app.WORKSPACE_ROUTES) is in
  ROUTES with the capability it needs, or None (a read of what the person
  already sees, its own account): a route added there without its line here
  fails tests/test_capabilities.py.
- A person with a custom role keeps a role on the workspace, the lowest
  preset that holds all its capabilities (ceiling): every check of the
  roles works as before with it, and refusal() takes away what the custom
  role does not tick. A person with a preset role and all the instances has
  no Grant: nothing of this applies to it, so nothing changes for anyone
  who does not use the feature.
- A person may be limited to some instances of the workspace: the others
  are not listed and are refused as another workspace's; what acts on the
  whole workspace (its people, its names, new instances, ...) is refused.
- An MCP or REST token of a person (its OAuth grant, or a workspace's token
  made for it) gets no more than the person now: Settings.token_grant.
"""

from __future__ import annotations

from dataclasses import dataclass

from .auth import ROLES

# the capabilities, in the order the pages list them, with their English label
# (the Italian is in i18n/it/permissions.json); the level is the lowest role
# that reaches at least one route of it: the presets follow from it
CAPABILITIES = {
    "logs": "View the logs and the database statistics",
    "run": "Start, stop and restart",
    "login_as": "Log in as an Odoo user",
    "code": "Update the code",
    "backup": "Make backups",
    "download": "Download backups",
    "modules": "Manage repositories and modules",
    "settings": "Change the settings of the instances",
    "stagings": "Make and delete stagings",
    "restore": "Restore a backup",
    "names": "Manage names and certificates",
    "instances": "Create and delete instances",
    "people": "Manage the people and the tokens of the workspace",
}
# the pages, the REST API and MCP together: a developer's token makes stagings (MCP create_staging,
# POST /api/v1/instances/{server}/stagings), the pages' New staging is an admin's
LEVELS = {"logs": "user", "run": "user", "login_as": "developer", "code": "developer", "backup": "developer",
          "download": "developer", "modules": "developer", "settings": "developer", "stagings": "developer",
          "restore": "admin", "names": "admin", "instances": "admin", "people": "admin"}
PRESETS = {role: frozenset(c for c in CAPABILITIES if ROLES.index(LEVELS[c]) <= ROLES.index(role)) for role in ROLES}
# a custom role's name: what the person reads on the pages (Backup senza ripristino)
NAME_MAX = 40


def ceiling(caps) -> str:
    """The lowest preset that holds every capability of caps: the role on the
    workspace a person with those capabilities has for every other check."""
    return max((LEVELS[c] for c in caps if c in LEVELS), key=ROLES.index, default=ROLES[0])


def check_name(name: str) -> str | None:
    """Why name cannot be a custom role's, None when it can."""
    if not name or len(name) > NAME_MAX:
        return f"A role's name is 1-{NAME_MAX} characters"
    if name in ROLES:
        return f"{name} is a role already: choose another name"
    if any(ord(ch) < 32 or ch in "<>\"'&\\" for ch in name):
        return "A role's name has no control characters nor < > \" ' & \\"
    return None


@dataclass(frozen=True)
class Grant:
    """What a person may do in one workspace, when it uses the feature: its
    role's name (a preset's or a custom one), its capabilities, and the
    servers it is limited to (None: every one of the workspace)."""

    role: str
    caps: frozenset
    servers: frozenset | None = None

    def lacks(self, capability: str | None) -> bool:
        return capability is not None and capability not in self.caps

    def reaches(self, server: str) -> bool:
        return self.servers is None or server in self.servers

    def narrowed(self, caps=None, servers=None) -> "Grant":
        """This grant, no more than caps and servers (a token of the person)."""
        return Grant(self.role, self.caps & frozenset(caps) if caps is not None else self.caps,
                     self.servers if servers is None else (
                         frozenset(servers) if self.servers is None else self.servers & frozenset(servers)))


def refusal(grant: Grant | None, capability: str | None, servers=(), whole: bool = False) -> str | None:
    """The one check: why the person with grant may not do capability on
    servers (whole: on its whole workspace), None when it may. No grant: a
    preset role on every instance, nothing refused here."""
    if grant is None:
        return None
    if grant.lacks(capability):
        return f"Your role «{grant.role}» does not include: {CAPABILITIES[capability]}"
    if grant.servers is not None:
        if whole:
            return "Your access is limited to some instances of the workspace: this acts on all of it"
        if any(not grant.reaches(s) for s in servers):
            return NOT_YOURS
    return None


# what a server out of the person's instances answers: the same as a server
# of another workspace, so that the refusal does not tell it exists
NOT_YOURS = "Not a server of your workspaces"

# the capability of an action of a plugin (plugin_api.Action), by its group
ACTION_GROUPS = {"service": "run", "git": "code", "backup": "backup", "python": "modules"}
# an action of the whole server that replaces a database (copy production to test)
RESTORING_ACTIONS = {"copy-production"}


def of_action(action) -> str:
    if action.id in RESTORING_ACTIONS:
        return "restore"
    return ACTION_GROUPS.get(action.group, "settings")


# the routes of a workspace that name their instance in the body (server):
# the route's code checks it (app.workspace_instance), so a person limited to
# some instances may call them on those
BODY_INSTANCE = frozenset({
    "/api/workspaces/{workspace}/repositories/add", "/api/workspaces/{workspace}/repositories/remove",
    "/api/workspaces/{workspace}/repositories/branches", "/api/workspaces/{workspace}/repositories/update",
    "/api/workspaces/{workspace}/parameters/options", "/api/workspaces/{workspace}/parameters/save",
    # its capability too: a staging's is Make and delete stagings, another instance's Create and delete instances
    "/api/workspaces/{workspace}/instances/delete",
})
# the plugins' actions: the capability is the action's (of_action), checked where it runs
PLUGIN_ACTION = "plugin action"

# (method, route) -> the capability it needs, None: none (a read of what the person sees, its account)
ROUTES = {
    # its account, the lists (of its instances only), the jobs
    **{(m, p): None for m, p in (
        ("POST", "/logout"), ("GET", "/account"), ("POST", "/api/account/password"),
        ("POST", "/api/oauth/grants/revoke"), ("GET", "/static/{name}"), ("GET", "/x/{plugin}/static/{path:path}"),
        ("POST", "/api/account/second-factor/start"), ("POST", "/api/account/second-factor/confirm"),
        ("POST", "/api/account/second-factor/disable"), ("POST", "/api/account/sessions/end"),
        ("POST", "/api/account/passkeys/options"), ("POST", "/api/account/passkeys/register"),
        ("POST", "/api/account/passkeys/delete"),
        ("GET", "/"), ("GET", "/dashboard/servers"), ("GET", "/api/servers"), ("GET", "/api/metrics"),
        ("GET", "/jobs"), ("GET", "/jobs/{job_id}"), ("GET", "/api/jobs"), ("GET", "/api/jobs/{job_id}"),
        ("GET", "/servers/{server}"), ("GET", "/api/servers/{server}/{role}/summary"),
        ("GET", "/api/servers/{server}/{role}/uptime"), ("GET", "/api/workspaces/{workspace}/load"),
        ("GET", "/api/servers/{server}/{role}/backups"), ("GET", "/instances"), ("GET", "/api/instances"),
        ("GET", "/backups"), ("GET", "/api/backups"), ("GET", "/api/map"),
        # the list of its workspaces (UX-013 §A): those where it is admin, checked in the route
        ("GET", "/workspaces"), ("GET", "/api/workspaces/list"),
        # the bell: the map's things to look at of what the person sees, and its own marks
        ("GET", "/api/bell"), ("POST", "/api/bell/read"),
        ("GET", "/api/servers/{server}/production/mail"), ("GET", "/api/servers/{server}/production/mail/{mail_id}"),
        ("GET", "/api/servers/{server}/production/mail/inbox"),
        ("GET", "/workspaces/{workspace}"),
        # checked in the route, by what it deletes (BODY_INSTANCE)
        ("POST", "/api/workspaces/{workspace}/instances/delete"),
        ("GET", "/api/v1/instances"), ("GET", "/api/v1/instances/{server}/{role}"), ("GET", "/api/v1/jobs"),
        ("GET", "/api/v1/jobs/{job_id}"), ("GET", "/api/v1/jobs/{job_id}/log"),
        ("GET", "/api/v1/instances/{server}/{role}/backups"), ("GET", "/api/v1/openapi.json"),
        ("GET", "/api/v1/docs"))},
    **{(m, p): "logs" for m, p in (
        ("GET", "/servers/{server}/{role}/log"), ("GET", "/api/servers/{server}/{role}/log"),
        ("GET", "/api/servers/{server}/{role}/log/records"), ("GET", "/api/servers/{server}/{role}/database"))},
    **{(m, p): "run" for m, p in (
        ("POST", "/servers/{server}/{role}/restart"), ("POST", "/servers/{server}/{role}/start"),
        ("POST", "/servers/{server}/{role}/stop"), ("POST", "/api/servers/{server}/{role}/database/terminate"),
        ("POST", "/jobs/{job_id}/stop"),
        ("POST", "/api/v1/instances/{server}/{role}/start"), ("POST", "/api/v1/instances/{server}/{role}/stop"),
        ("POST", "/api/v1/instances/{server}/{role}/restart"))},
    **{(m, p): "login_as" for m, p in (
        ("GET", "/api/servers/{server}/{role}/login-as/users"), ("POST", "/api/servers/{server}/{role}/login-as"))},
    **{(m, p): "code" for m, p in (
        ("POST", "/servers/{server}/{role}/pull"), ("POST", "/servers/{server}/{role}/pull-restart"),
        ("GET", "/api/servers/{server}/{role}/pull-preview"), ("POST", "/servers/{server}/merge"),
        ("GET", "/api/servers/{server}/{role}/deploy-hook"), ("POST", "/api/servers/{server}/{role}/deploy-hook"),
        # the update without stopping changes how an update runs: switched by who switches the CI
        ("GET", "/api/servers/{server}/{role}/zero-downtime"), ("POST", "/api/servers/{server}/{role}/zero-downtime"),
        ("GET", "/api/servers/{server}/{role}/deploys"), ("GET", "/api/servers/{server}/{role}/deploys/{deploy_id}/back"),
        ("POST", "/api/servers/{server}/{role}/deploys/{deploy_id}/back"),
        ("POST", "/api/workspaces/{workspace}/repositories/branches"),
        ("POST", "/api/workspaces/{workspace}/repositories/update"),
        ("POST", "/api/v1/instances/{server}/{role}/pull"), ("GET", "/api/v1/instances/{server}/{role}/deploys"),
        ("POST", "/api/v1/instances/{server}/{role}/deploys/{deploy_id}/back"))},
    **{(m, p): "backup" for m, p in (
        ("POST", "/servers/{server}/{role}/backup"), ("GET", "/api/servers/{server}/{role}/backup/estimate"),
        # the room of a restore: the backup dialog shows it too
        ("GET", "/api/servers/{server}/{role}/restore/room"),
        ("POST", "/api/servers/{server}/{role}/backups/verify"), ("POST", "/api/servers/{server}/{role}/archive"),
        ("GET", "/api/servers/{server}/{role}/backup-schedule"), ("POST", "/api/servers/{server}/{role}/backup-schedule"),
        ("POST", "/api/servers/{server}/{role}/backups/delete"), ("GET", "/api/servers/{server}/{role}/remote-backups"),
        ("POST", "/api/servers/{server}/{role}/remote-backups/fetch"), ("GET", "/api/servers/{server}/pitr"),
        ("POST", "/api/servers/{server}/pitr/enable"), ("POST", "/api/servers/{server}/pitr/base"),
        ("POST", "/api/v1/instances/{server}/{role}/backups"))},
    **{(m, p): "download" for m, p in (
        ("GET", "/backups/{server}/{role}/{name}"), ("GET", "/api/v1/instances/{server}/{role}/backups/{file}"))},
    **{(m, p): "restore" for m, p in (
        ("POST", "/servers/{server}/{role}/restore"),
        ("POST", "/api/servers/{server}/pitr/restore"), ("PUT", "/api/uploads/{ticket}"))},
    **{(m, p): "stagings" for m, p in (
        ("GET", "/api/servers/{server}/stagings"), ("GET", "/api/servers/{server}/stagings/new"),
        ("GET", "/api/servers/{server}/stagings/elsewhere"), ("POST", "/api/servers/{server}/stagings"),
        ("POST", "/api/servers/{server}/stagings/refresh"), ("POST", "/api/servers/{server}/stagings/from-backup"),
        ("POST", "/api/v1/instances/{server}/stagings"))},
    **{(m, p): "modules" for m, p in (
        ("POST", "/api/workspaces/{workspace}/credentials/save"), ("POST", "/api/workspaces/{workspace}/credentials/delete"),
        ("POST", "/api/workspaces/{workspace}/credentials/github/save"),
        ("POST", "/api/workspaces/{workspace}/credentials/github/test"),
        ("POST", "/api/workspaces/{workspace}/github/repositories"), ("POST", "/api/workspaces/{workspace}/github/branches"),
        ("POST", "/api/workspaces/{workspace}/repositories/add"), ("POST", "/api/workspaces/{workspace}/repositories/remove"),
        ("POST", "/api/workspaces/{workspace}/git-key"),
        ("GET", "/api/servers/{server}/modules"), ("GET", "/api/servers/{server}/modules/catalog"),
        ("GET", "/api/servers/{server}/modules/plan"), ("GET", "/api/servers/{server}/{role}/modules"),
        ("POST", "/api/servers/{server}/{role}/modules/install"), ("POST", "/api/servers/{server}/modules/upload"),
        ("POST", "/api/servers/{server}/modules/upload/{ident}/apply"), ("GET", "/api/servers/{server}/modules/uploaded"),
        ("POST", "/api/servers/{server}/modules/github/check"), ("POST", "/api/servers/{server}/modules/github/create"))},
    **{(m, p): "settings" for m, p in (
        ("POST", "/api/workspaces/{workspace}/notify"), ("POST", "/api/workspaces/{workspace}/parameters/options"),
        ("POST", "/api/workspaces/{workspace}/parameters/save"),
        ("POST", "/api/servers/{server}/production/mail/inbox"), ("POST", "/api/servers/{server}/production/mail/send"),
        ("POST", "/api/servers/{server}/production/mail/clear"),
        ("GET", "/workspaces/{workspace}/destinations"), ("POST", "/api/workspaces/{workspace}/destinations/save"),
        ("POST", "/api/workspaces/{workspace}/destinations/delete"),
        ("POST", "/api/workspaces/{workspace}/destinations/check"),
        ("POST", "/api/workspaces/{workspace}/destinations/key"))},
    **{(m, p): "names" for m, p in (
        ("POST", "/api/workspaces/{workspace}/web/save"), ("POST", "/api/workspaces/{workspace}/web/publish"),
        ("POST", "/api/workspaces/{workspace}/web/certificate"), ("GET", "/api/servers/{server}/{role}/addresses"),
        ("POST", "/api/servers/{server}/{role}/addresses/check"),
        ("POST", "/api/servers/{server}/{role}/addresses/save"), ("GET", "/api/servers/{server}/{role}/relay"))},
    **{(m, p): "instances" for m, p in (
        ("POST", "/api/workspaces/{workspace}/instances/create"),
        ("GET", "/api/workspaces/{workspace}/archives"), ("POST", "/api/workspaces/{workspace}/archives/upload"),
        ("GET", "/api/workspaces/{workspace}/archives/remote"), ("POST", "/api/workspaces/{workspace}/archives/fetch"),
        ("POST", "/api/workspaces/{workspace}/instances/from-archive"))},
    **{(m, p): "people" for m, p in (
        ("POST", "/api/workspaces/{workspace}/members/save"), ("POST", "/api/workspaces/{workspace}/members/delete"),
        ("POST", "/api/workspaces/{workspace}/members/invitation/resend"),
        ("POST", "/api/workspaces/{workspace}/members/invitation/revoke"),
        ("POST", "/api/workspaces/{workspace}/members/second-factor/reset"),
        ("POST", "/api/workspaces/{workspace}/roles/save"), ("POST", "/api/workspaces/{workspace}/roles/delete"),
        ("POST", "/api/workspaces/{workspace}/tokens/save"), ("POST", "/api/workspaces/{workspace}/tokens/delete"),
        ("POST", "/api/workspaces/{workspace}/sso/save"), ("POST", "/api/workspaces/{workspace}/sso/delete"),
        ("GET", "/workspaces/{workspace}/audit"), ("GET", "/api/workspaces/{workspace}/audit/export"))},
    **{(m, p): PLUGIN_ACTION for m, p in (
        ("POST", "/servers/{server}/{role}/x/{plugin}/{action}"), ("POST", "/servers/{server}/x/{plugin}/{action}"))},
}

# the MCP tools (mcp_server.py) and the capability each needs; run_action's is its action's
TOOLS = {"read_log": "logs", "database_report": "logs",
         "restart_instance": "run", "start_instance": "run", "stop_instance": "run", "stop_job": "run",
         "update_test": "code", "update_production": "code", "list_deploys": "code", "deploy_back": "code",
         "backup_test": "backup", "backup": "backup", "verify_backup": "backup", "full_archive": "backup",
         "create_staging": "stagings",
         "upload_backup": "restore", "restore_new_instance": "restore"}


# the MCP tools that make something in the workspace as a whole (a new instance): not for a token limited to
# some instances, as the routes of the whole workspace are not
WHOLE_TOOLS = frozenset({"upload_backup", "restore_new_instance"})


def of_route(method: str, path: str | None) -> str | None:
    """The capability a route needs; a plugin's own route (/x/<plugin>/...) of an instance: a POST changes its
    settings, a GET reads them."""
    if path is None:
        return None
    if (method, path) in ROUTES:
        return ROUTES[(method, path)]
    if path.startswith("/x/"):
        return "settings" if method not in ("GET", "HEAD") else None
    return ROUTES.get(("GET" if method == "HEAD" else method, path))


def whole_workspace(path: str | None) -> bool:
    """A route that acts on its workspace as a whole (not on one instance named in its path or its body)."""
    return bool(path) and "{workspace}" in path and path not in BODY_INSTANCE
