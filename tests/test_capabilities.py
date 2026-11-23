# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Custom roles of a workspace (capabilities.py, README, Custom roles of a
workspace): the presets are today's roles on every route, a custom role takes
away what it does not tick, a person limited to some instances sees and acts on
those only (pages, REST API, MCP), and its tokens get no more than the person."""

import base64
import dataclasses
import json
import re

import pytest
from fastapi.routing import APIRoute

from omniacli import capabilities, jobs as jobs_module, stagings
from omniacli.app import WORKSPACE_ROUTES
from omniacli.auth import ROLES, hash_password
from omniacli.config import Access, load_workspaces, parse_workspaces, save_workspaces

from .conftest import PASSWORD
from .test_app import client, wait_job  # noqa: F401 - the client fixture
from .test_deploys import staged  # noqa: F401 - srv and its staging stg
from .test_workspaces import ORIGIN

V1 = "/api/v1"
PERSON = "tina"
NO_RESTORE = "Backup senza ripristino"
# the person made through the page (a password of 8 characters at least); the others: the fixture's
PERSON_PASSWORD = "secret-secret"


def basic(user: str) -> dict:
    password = PERSON_PASSWORD if user == PERSON else PASSWORD
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


def post_as(user: str) -> dict:
    return {**basic(user), "Origin": ORIGIN}


def every_route(app):
    """(template, route) of the app, those of its included routers too (the REST API's, the plugins')."""
    for route in app.routes:
        if hasattr(route, "original_router"):
            prefix = route.include_context.prefix
            for sub in route.original_router.routes:
                yield prefix + sub.path, sub
        else:
            yield getattr(route, "path", ""), route


def workspace_routes(app):
    """(method, template) of every route a user of workspaces reaches."""
    found = set()
    for path, route in every_route(app):
        if not isinstance(route, APIRoute) or not (path in WORKSPACE_ROUTES or path.startswith("/x/")):
            continue
        for method in route.methods - {"HEAD"}:
            found.add((method, path))
    return found


def people(client, settings, members: dict, **ws_fields):
    """acme with srv and stg, and members {name: role} (a role of ROLES or a custom one of ws_fields['roles'])."""
    password = hash_password(PASSWORD, iterations=1000)
    names = ["mario", *members]
    users = {n: hash_password(PERSON_PASSWORD, iterations=1000) if n == PERSON else password for n in names}
    settings.set_web_access(Access(users=users, roles={n: "workspace" for n in names}))
    authenticator = client.app.state.authenticator
    authenticator.update(settings.users, settings.tokens, authenticator.links)
    roles = ws_fields.get("roles", {})
    data = {"servers": ["srv", "stg"], "members": {"mario": "admin", **{n: r if r in ROLES else "user"
                                                                      for n, r in members.items()}},
            "member_roles": {n: r for n, r in members.items() if r not in ROLES}, **ws_fields, "roles": roles}
    settings.workspaces = parse_workspaces({"acme": data})
    return settings.workspaces["acme"]


def test_every_route_of_a_workspace_has_its_capability(staged, client):  # noqa: F811
    """A route open to the users of workspaces says what it needs (None: nothing more than seeing):
    a route added there without its line in capabilities.ROUTES fails here, and so does a line of a route gone."""
    routes = workspace_routes(client.app)
    missing = sorted(r for r in routes if r not in capabilities.ROUTES and not r[1].startswith("/x/"))
    assert not missing, missing
    stale = sorted(r for r in capabilities.ROUTES if r not in routes)
    assert not stale, stale
    assert set(capabilities.TOOLS) <= set(__import__("omniacli.mcp_server", fromlist=["TOOL_ROLES"]).TOOL_ROLES) | {
        "read_log"}
    # the presets: each one holds the one below; a custom role's ceiling is the lowest that holds it
    assert capabilities.PRESETS["user"] < capabilities.PRESETS["developer"] < capabilities.PRESETS["admin"]
    assert capabilities.PRESETS["admin"] == set(capabilities.CAPABILITIES)
    for role in ROLES:
        assert capabilities.ceiling(capabilities.PRESETS[role]) == role
    assert capabilities.ceiling({"logs", "backup"}) == "developer" and capabilities.ceiling(()) == "user"


VALUES = {"server": "srv", "role": "test", "workspace": "acme", "job_id": "nojob", "plugin": "odoo",
          "action": "update", "name": "x.zip", "mail_id": "1", "deploy_id": "1", "ident": "x", "ticket": "x",
          "file": "x.zip", "path": "x"}


async def test_the_presets_are_todays_roles_on_every_route(staged, client, env, monkeypatch):  # noqa: F811
    """The table test: on every route a user of workspaces reaches, a person with a custom role equal to a
    preset gets the same answer as one with that role (the jobs do nothing here: only who may start them)."""
    real_submit = jobs_module.JobManager.submit

    def submit(self, title, instances, source, action, workspace=""):
        async def nothing(job):
            return None
        return real_submit(self, title, instances, source, nothing, workspace)
    monkeypatch.setattr(jobs_module.JobManager, "submit", submit)
    settings = env["settings"]
    roles = {f"copy of {r}": sorted(capabilities.PRESETS[r]) for r in ROLES}
    people(client, settings, {**{f"as-{r}": r for r in ROLES}, **{f"copy-{r}": f"copy of {r}" for r in ROLES}}, roles=roles)
    assert {r: settings.workspaces["acme"].members[f"copy-{r}"] for r in ROLES} == {r: r for r in ROLES}
    # and their tokens: of each level, one for an admin, one for an admin by a copy of the admin preset
    tokens = {}
    for role in ROLES:
        for person in ("as-admin", "copy-admin"):
            made = (await client.post("/api/workspaces/acme/tokens/save", headers=post_as("mario"), json={
                "name": f"{person}-{role}", "role": role, "new": True, "person": person})).json()
            tokens[(person, role)] = {"Authorization": f"Bearer {made['token']}"}
    assert settings.token_grant("copy-admin-developer").caps == capabilities.PRESETS["developer"]
    assert settings.token_grant("as-admin-developer") is None
    differ, seen = [], set()
    for method, path in sorted(workspace_routes(client.app)):
        url = re.sub(r"\{(\w+)(?::path)?\}", lambda m: VALUES.get(m.group(1), "x"), path)
        for role in ROLES:
            got = []
            for person in ("as-admin", "copy-admin"):
                resp = await client.request(method, url, headers=tokens[(person, role)], json={},
                                            follow_redirects=False)
                got.append(resp.status_code)
            if got[0] != got[1]:
                differ.append((method, path, f"token {role}", got))
            got = []
            for who in (f"as-{role}", f"copy-{role}"):
                resp = await client.request(method, url, headers=post_as(who), json={}, follow_redirects=False)
                got.append(resp.status_code)
            if got[0] != got[1]:
                differ.append((method, path, role, got))
            if not path.startswith(V1):
                # the REST API takes tokens only: a person's session gets 401 there (its tokens: below)
                seen.update(got)
    assert not differ, '\n'.join(map(str, differ))
    # every request was a person's (none refused for its login), and the roles meant something
    assert 401 not in seen and {200, 403} <= seen, seen
    # MCP: the same tools for both tokens of each level
    from .test_mcp import HEADERS, rpc
    for role in ROLES:
        listed = [{t["name"] for t in (await rpc(client, "tools/list", headers={**HEADERS, **tokens[(p, role)]})).json()[
            "result"]["tools"]} for p in ("as-admin", "copy-admin")]
        assert listed[0] == listed[1], role


async def test_a_role_without_restore(staged, client, env):  # noqa: F811
    """A custom role made from admin without Restore: refused on restore (the page: disabled, with the
    reason), allowed on backup; never given beyond its maker's own."""
    settings = env["settings"]
    people(client, settings, {"luca": "developer"})
    caps = sorted(capabilities.PRESETS["admin"] - {"restore"})
    resp = await client.post("/api/workspaces/acme/roles/save", headers=post_as("mario"),
                             json={"name": NO_RESTORE, "capabilities": caps, "new": True})
    assert resp.status_code == 200, resp.text
    resp = await client.post("/api/workspaces/acme/members/save", headers=post_as("mario"),
                             json={"name": PERSON, "role": NO_RESTORE, "password": PERSON_PASSWORD, "new": True})
    assert resp.status_code == 200, resp.text
    ws = settings.workspaces["acme"]
    assert ws.members[PERSON] == "admin" and ws.member_roles[PERSON] == NO_RESTORE
    saved = load_workspaces(settings.workspaces_file)["acme"]
    assert {n: sorted(c) for n, c in saved.roles.items()} == {NO_RESTORE: caps}
    assert saved.member_roles == {PERSON: NO_RESTORE}
    # restore: refused with the reason; backup: a job
    resp = await client.post("/servers/srv/test/restore", headers=post_as(PERSON), data={"file": "x.zip"})
    assert resp.status_code == 403 and "does not include: Restore a backup" in resp.json()["detail"]
    assert (await client.post("/servers/srv/test/restore", headers=post_as("mario"),
                              data={"file": "x.zip"})).status_code != 403
    resp = await client.post("/servers/srv/test/backup", headers=post_as(PERSON), follow_redirects=False)
    assert resp.status_code == 303, resp.text
    await wait_job(client, resp.headers["location"].rsplit("/", 1)[1])
    # the page: Restore there, disabled, its reason in its title; Back up now live
    page = (await client.get("/servers/srv?view=test", headers=basic(PERSON))).text
    off = re.search(r'<span title="([^"]+)"><button type="button" class="danger" disabled title="([^"]+)">', page)
    assert off and "does not include" in off.group(1) and off.group(1) == off.group(2)
    assert "data-restore-file='" not in page and 'data-backup="/servers/srv/test/backup"' in page
    # never beyond the giver's own: a role with Restore, the admin preset, the full admin mario
    for body in ({"name": "more", "capabilities": ["restore"], "new": True},):
        resp = await client.post("/api/workspaces/acme/roles/save", headers=post_as(PERSON), json=body)
        assert resp.status_code == 403 and "does not include: Restore a backup" in resp.json()["error"]
    resp = await client.post("/api/workspaces/acme/members/save", headers=post_as(PERSON),
                             json={"name": "eve", "role": "admin", "password": PERSON_PASSWORD, "new": True})
    assert resp.status_code == 403 and "Restore a backup" in resp.json()["error"]
    resp = await client.post("/api/workspaces/acme/members/save", headers=post_as(PERSON),
                             json={"name": "mario", "role": "user"})
    assert resp.status_code == 403 and "may do more than your role" in resp.json()["error"]
    resp = await client.post("/api/workspaces/acme/roles/save", headers=post_as(PERSON),
                             json={"name": NO_RESTORE, "capabilities": ["logs"]})
    assert resp.status_code == 400 and "role you have" in resp.json()["error"]
    # what it may give, it gives: a developer with a role of its own capabilities
    resp = await client.post("/api/workspaces/acme/members/save", headers=post_as(PERSON),
                             json={"name": "luca", "role": "user"})
    assert resp.status_code == 200, resp.text
    # every change of a role or of a person's capabilities is in the access log (NIS 2)
    recent = client.app.state.access_log.recent()
    said = [e["detail"] for e in recent if e["event"] == "permissions"]
    assert any(f"role «{NO_RESTORE}» of acme made" in d for d in said), said
    # and the restore refused, with who tried it
    assert any(e["event"] == "action" and e["name"] == PERSON and e.get("status") == 403
               and "/restore" in e.get("route", "") for e in recent), [e for e in recent if e["event"] == "action"]
    assert any(d.startswith(f"{PERSON} in acme: role {NO_RESTORE} (") for d in said), said
    assert any(d.startswith("luca in acme: role developer (") and "→ user (" in d for d in said), said
    # a role given cannot go; the role changed, its person changes with it
    resp = await client.post("/api/workspaces/acme/roles/delete", headers=post_as("mario"), json={"name": NO_RESTORE})
    assert resp.status_code == 400 and PERSON in resp.json()["error"]
    resp = await client.post("/api/workspaces/acme/roles/save", headers=post_as("mario"),
                             json={"name": NO_RESTORE, "capabilities": ["logs", "backup"]})
    assert resp.status_code == 200 and settings.workspaces["acme"].members[PERSON] == "developer"
    assert (await client.get("/workspaces/acme", headers=basic(PERSON))).status_code == 403
    # the person out: the role may go
    assert (await client.post("/api/workspaces/acme/members/delete", headers=post_as("mario"),
                              json={"name": PERSON})).status_code == 200
    assert PERSON not in settings.workspaces["acme"].member_roles
    assert (await client.post("/api/workspaces/acme/roles/delete", headers=post_as("mario"),
                              json={"name": NO_RESTORE})).json() == {"ok": True}


async def test_a_person_limited_to_one_instance(staged, client, env):  # noqa: F811
    """Limited to stg: the lists, the pages, the REST API and MCP show and act on stg only; what acts on the
    whole workspace is refused; a token made for the person, or by it, gets no more than the person."""
    settings = env["settings"]
    people(client, settings, {}, roles={NO_RESTORE: sorted(capabilities.PRESETS["admin"] - {"restore"})})
    resp = await client.post("/api/workspaces/acme/members/save", headers=post_as("mario"),
                             json={"name": PERSON, "role": NO_RESTORE, "password": PERSON_PASSWORD, "new": True,
                                   "servers": ["stg"]})
    assert resp.status_code == 200, resp.text
    assert settings.workspaces["acme"].member_servers == {PERSON: ["stg"]}
    me = basic(PERSON)
    # the lists
    assert [s["name"] for s in (await client.get("/api/servers", headers=me)).json()] == ["stg"]
    assert [s["name"] for s in (await client.get("/api/instances", headers=me)).json()["servers"]] == ["stg"]
    index = (await client.get("/", headers=me)).text
    assert "/servers/stg" in index and 'href="/servers/srv"' not in index and 'href="/workspaces/acme"' not in index
    # the pages: stg yes, srv as another workspace's
    assert (await client.get("/servers/stg", headers=me)).status_code == 200
    for url in ("/servers/srv", "/api/servers/srv/test/backups", "/servers/srv/test/log"):
        resp = await client.get(url, headers=me)
        assert resp.status_code == 403 and resp.json()["detail"] == "Not a server of your workspaces", url
    assert (await client.post("/servers/srv/test/restart", headers=post_as(PERSON))).status_code == 403
    resp = await client.post("/servers/stg/production/backup", headers=post_as(PERSON), follow_redirects=False)
    assert resp.status_code == 303, resp.text
    job_id = resp.headers["location"].rsplit("/", 1)[1]
    await wait_job(client, job_id)
    assert (await client.get(f"/api/jobs/{job_id}", headers=me)).status_code == 200
    listed = (await client.get("/api/jobs", headers=me)).json()["jobs"]
    assert listed and all(set(j["servers"]) <= {"stg"} for j in listed)
    resp = await client.post("/servers/stg/production/restore", headers=post_as(PERSON), data={"file": "x.zip"})
    assert resp.status_code == 403 and "does not include" in resp.json()["detail"]
    # the whole workspace: refused; an instance in a body: stg only
    for url in ("/workspaces/acme", "/api/workspaces/acme/load"):
        resp = await client.get(url, headers=me)
        assert resp.status_code == 403 and "limited to some instances" in resp.json()["detail"], url
    resp = await client.post("/api/workspaces/acme/members/save", headers=post_as(PERSON), json={"name": "x"})
    assert resp.status_code == 403
    resp = await client.post("/api/workspaces/acme/repositories/branches", headers=post_as(PERSON),
                             json={"server": "srv", "name": "addons"})
    assert resp.status_code == 404 and "No instance srv" in resp.json()["error"]
    # Rebuild backs up stg's production srv, out of its reach: refused, and off on the page with the reason
    resp = await client.post("/api/servers/stg/stagings/refresh", headers=post_as(PERSON), json={})
    assert resp.status_code == 403 and "not one of your instances" in resp.json()["error"]
    stagings.save((settings.links_file or settings.servers_file).parent / stagings.FILE,
                  {"stg": {"parent": "srv", "created": "2026-11-22T09:00:00", "backup": "srv_20261122.zip"}})
    page = (await client.get("/servers/stg", headers=me)).text
    assert 'id="staging-rebuild"' not in page and "not one of your instances" in page
    assert 'id="staging-rebuild"' in (await client.get("/servers/stg", headers=basic("mario"))).text
    # its token: made for it by mario; the REST API and MCP the same way
    made = (await client.post("/api/workspaces/acme/tokens/save", headers=post_as("mario"),
                              json={"name": "tina-ci", "role": "admin", "new": True, "person": PERSON})).json()
    assert made.get("token"), made
    bearer = {"Authorization": f"Bearer {made['token']}"}
    assert {r["server"] for r in (await client.get(f"{V1}/instances", headers=bearer)).json()} == {"stg"}
    resp = await client.get(f"{V1}/instances/srv/production", headers=bearer)
    assert resp.status_code == 403
    resp = await client.post(f"{V1}/instances/stg/production/backups", headers=bearer, json={"confirm": "stg"})
    assert resp.status_code == 202, resp.text
    from .test_mcp import HEADERS, call, rpc
    headers = {**HEADERS, **bearer}
    assert [s["name"] for s in await call(client, "list_servers", headers=headers)] == ["stg"]
    tools = {t["name"] for t in (await rpc(client, "tools/list", headers=headers)).json()["result"]["tools"]}
    assert "backup" in tools and "restore_new_instance" not in tools and "upload_backup" not in tools
    refused = await call(client, "restore_new_instance", headers=headers, server_name="stg", role="production",
                         new_name="x", file="x.zip")
    assert "does not include: Restore a backup" in refused["error"]
    assert "No active server srv" in (await call(client, "server_status", headers=headers, server_name="srv",
                                                 role="production"))["error"]
    # the person's role made smaller: the token with it, at once
    resp = await client.post("/api/workspaces/acme/members/save", headers=post_as("mario"),
                             json={"name": PERSON, "role": "user", "servers": ["stg"]})
    assert resp.status_code == 200, resp.text
    resp = await client.post(f"{V1}/instances/stg/production/backups", headers=bearer, json={"confirm": "stg"})
    assert resp.status_code == 403, resp.text
    tools = {t["name"] for t in (await rpc(client, "tools/list", headers=headers)).json()["result"]["tools"]}
    assert "backup" not in tools
    # the person out of the workspace: its token reaches nothing
    assert (await client.post("/api/workspaces/acme/members/delete", headers=post_as("mario"),
                              json={"name": PERSON})).status_code == 200
    assert (await client.get(f"{V1}/instances", headers=bearer)).json() == []
    said = [e["detail"] for e in client.app.state.access_log.recent() if e["event"] == "permissions"]
    assert any(d == f"token tina-ci of acme for {PERSON}: no more than {PERSON} may" for d in said), said
    assert any(d.startswith(f"{PERSON} in acme: role {NO_RESTORE} (") and d.endswith(", stg") for d in said), said


async def test_a_token_made_by_a_person_with_a_custom_role_is_its_own(staged, client, env):  # noqa: F811
    settings = env["settings"]
    people(client, settings, {PERSON: NO_RESTORE}, roles={NO_RESTORE: sorted(capabilities.PRESETS["admin"] - {"restore"})})
    made = (await client.post("/api/workspaces/acme/tokens/save", headers=post_as(PERSON),
                              json={"name": "mine", "role": "admin", "new": True})).json()
    assert settings.workspaces["acme"].token_people == {"mine": PERSON}
    grant = settings.token_grant("mine")
    assert "restore" not in grant.caps and grant.servers is None
    # not for one who may do more than it: mario is a full admin
    resp = await client.post("/api/workspaces/acme/tokens/save", headers=post_as(PERSON),
                             json={"name": "his", "role": "admin", "new": True, "person": "mario"})
    assert resp.status_code == 403 and "Restore a backup" in resp.json()["error"]
    from .test_mcp import HEADERS, rpc
    tools = {t["name"] for t in (await rpc(client, "tools/list", headers={
        **HEADERS, "Authorization": f"Bearer {made['token']}"})).json()["result"]["tools"]}
    assert "backup" in tools and "restore_new_instance" not in tools


async def test_the_workspace_page_shows_the_roles(staged, client, env):  # noqa: F811
    """Users › Roles: the three presets read, a custom role with its buttons; every button with a title
    other than its text; the capabilities a giver lacks off in the role's dialog."""
    settings = env["settings"]
    people(client, settings, {PERSON: NO_RESTORE}, roles={NO_RESTORE: sorted(capabilities.PRESETS["admin"] - {"restore"})},
           member_servers={PERSON: ["stg"]})
    page = (await client.get("/workspaces/acme", headers=basic("mario"))).text
    assert f'data-role-name="{NO_RESTORE}"' in page and 'id="role-dialog"' in page
    assert f'data-user="{PERSON}" data-role="{NO_RESTORE}"' in page and "only stg" in page
    roles = page.split('id="ws-roles"')[1].split("</table>")[0]
    for button in re.findall(r"<button\b[^>]*>[^<]*", roles):
        title = re.search(r'title="([^"]+)"', button)
        assert title and title.group(1) != button.rsplit(">", 1)[1].strip(), button
    assert len(re.findall(r'class="[^"]*\bprimary\b', page.split("<dialog")[0])) == 1
    # a giver with the custom role: Restore off in the dialog; mario's row off
    settings.workspaces = parse_workspaces({"acme": {**settings.workspaces["acme"].to_dict(), "member_servers": {}}})
    page = (await client.get("/workspaces/acme", headers=basic(PERSON))).text
    assert re.search(r'title="[^"]+"><input type="checkbox" data-cap value="restore" disabled>', page)
    assert re.search(r'<span title="[^"]*may do more than your role[^"]*"><button type="button" class="small" disabled', page)


def test_the_workspaces_file_keeps_the_roles(tmp_path):
    data = {"acme": {"servers": ["acme-a", "acme-b"], "members": {"mario": "admin", "tina": "admin"},
                     "roles": {NO_RESTORE: ["backup", "logs"]}, "member_roles": {"tina": NO_RESTORE},
                     "member_servers": {"tina": ["acme-b"]}, "token_people": {"t": "tina"}}}
    found = parse_workspaces(data)
    ws = found["acme"]
    # the member's role is its custom role's ceiling, whatever was written; the capabilities in their order
    assert ws.members["tina"] == "developer" and ws.roles[NO_RESTORE] == ["logs", "backup"]
    assert ws.grant("tina") == capabilities.Grant(NO_RESTORE, frozenset({"logs", "backup"}), frozenset({"acme-b"}))
    assert ws.grant("mario") is None
    save_workspaces(tmp_path / "w.yaml", found)
    assert load_workspaces(tmp_path / "w.yaml") == found
    for bad, message in (({"roles": {"admin": []}}, "is a role already"), ({"roles": {"x": ["fly"]}}, "a list of"),
                         ({"member_roles": {"ghost": NO_RESTORE}}, "not a member"),
                         ({"member_roles": {"tina": "nope"}}, "not a role of the workspace"),
                         ({"member_servers": {"tina": "acme-b"}}, "a list of server names")):
        with pytest.raises(Exception, match=message):
            parse_workspaces({"acme": {**data["acme"], **bad}})
    # a member given another role by the machine's admins loses its custom role; one taken out, its limit
    assert dataclasses.replace(ws, member_roles={}).grant("tina").role == "developer"
    assert capabilities.refusal(ws.grant("tina"), "restore") == \
        f"Your role «{NO_RESTORE}» does not include: Restore a backup"
    assert capabilities.refusal(ws.grant("tina"), "backup", ["acme-a"]) == capabilities.NOT_YOURS
    assert "limited" in capabilities.refusal(ws.grant("tina"), None, whole=True)
    assert json.dumps(ws.to_dict())  # plain data
