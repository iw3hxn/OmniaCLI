# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
import json

import pytest

pytest.importorskip("mcp")  # the SDK needs Python 3.10

from omniacli.mcp_server import _errors_only, _public_url  # noqa: E402

from .conftest import TOKEN, sh  # noqa: E402
from .test_app import AUTH, HOST, POST, client, wait_job  # noqa: E402,F401

BEARER = {"Authorization": f"Bearer {TOKEN}"}
HEADERS = {**BEARER, "Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


async def rpc(client, method, params=None, headers=HEADERS):
    resp = await client.post("/mcp", headers=headers, content=json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}))
    return resp


async def call(client, tool, headers=HEADERS, **arguments):
    resp = await rpc(client, "tools/call", {"name": tool, "arguments": arguments}, headers=headers)
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    if result.get("isError"):
        return {"error": result["content"][0]["text"]}
    if "structuredContent" in result:
        content = result["structuredContent"]
        return content.get("result", content) if set(content) == {"result"} else content
    return json.loads(result["content"][0]["text"])


async def test_token_required(client):
    assert (await rpc(client, "tools/list", headers={**HEADERS, "Authorization": AUTH})).status_code == 401
    assert (await rpc(client, "tools/list", headers={**HEADERS, "Authorization": "Bearer nope"})).status_code == 401
    resp = await rpc(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                             "clientInfo": {"name": "t", "version": "1"}})
    assert resp.status_code == 200 and resp.json()["result"]["serverInfo"]["name"] == "OmniaCLI"
    tools = {t["name"] for t in (await rpc(client, "tools/list")).json()["result"]["tools"]}
    assert tools == {"list_servers", "server_status", "pending_updates", "read_log", "list_jobs",
                     "get_job", "update_test", "backup_test", "dev_setup", "diagnose_instance",
                     "restart_instance", "start_instance", "stop_instance", "list_actions", "run_action",
                     "verify_backup", "full_archive", "stop_job", "list_links", "linked_tools", "call_linked",
                     "database_report",
                     # what the REST API does too (README, REST API): a production's still asks admin and confirm
                     "backup", "list_backups", "update_production", "create_staging", "list_deploys", "deploy_back",
                     "job_log"}
    # the token reads pages but never acts on them
    assert (await client.get("/jobs", headers=BEARER)).status_code == 200
    assert (await client.post("/servers/srv/test/pull", headers={**BEARER, "Origin": f"http://{HOST}"})).status_code == 403


async def test_monitor_and_update_test(client, env):
    servers = await call(client, "list_servers")
    assert servers[0]["name"] == "srv" and servers[0]["instances"]["test"]["database"] == "DB_test"
    status = await call(client, "server_status", server_name="srv")
    assert status["test"]["repositories"]["odoo"]["branch"] == "test" and not status["test"]["job_running"]
    log = await call(client, "read_log", server_name="srv", role="test", lines=3)
    assert log["lines"] == ["line 7", "line 8", "line 9"]

    job = await call(client, "update_test", server_name="srv", wait_seconds=30)
    assert job["status"] == "success" and job["source"] == "mcp: token tester", job
    assert "restart odoo-test" in env["calls"].read_text()
    assert "restart odoo-production" not in env["calls"].read_text()
    assert (await call(client, "get_job", job_id=job["id"], tail=5))["id"] == job["id"]
    assert [j["id"] for j in await call(client, "list_jobs", server_name="srv", query="tester")] == [job["id"]]
    # production has no action, an unknown server is an error
    assert "No active server nope" in (await call(client, "update_test", server_name="nope"))["error"]


async def test_dev_setup_and_backup(client, env):
    tmp = env["tmp"]
    test = env["settings"].instance("srv", "test")
    odoo = test.repository("odoo").path
    (odoo / "sub").mkdir()
    test.options.odoo_config.write_text(
        f"[options]\naddons_path = {odoo}/sub,{test.repository('addons').path}, {tmp}/elsewhere\n"
        "admin_passwd = SECRET\ndb_password = SECRET\ndb_host = db.local\nworkers = 4\n")
    sh("git", "remote", "set-url", "origin", "https://user:SECRET@git.example.com/odoo.git", cwd=odoo)
    setup = await call(client, "dev_setup", server_name="srv")
    assert "SECRET" not in json.dumps(setup)
    repo = setup["repositories"][0]
    assert repo["url"] == "https://git.example.com/odoo.git" and repo["branch"] == "test"
    assert repo["commit"] == sh("git", "rev-parse", "HEAD", cwd=odoo)
    assert setup["addons_path"][:2] == [{"repository": "odoo", "subpath": "sub"},
                                        {"repository": "addons", "subpath": "."}]
    assert "outside_repositories" in setup["addons_path"][2]
    assert setup["odoo_conf_options"] == {"workers": "4"}

    job = await call(client, "backup_test", server_name="srv", wait_seconds=30)
    assert job["status"] == "success", job
    resp = await client.get(job["result"]["download"], headers=BEARER)
    assert resp.status_code == 200 and resp.content == b"dump\n"
    setup = await call(client, "dev_setup", server_name="srv")
    assert setup["backups"][0]["download"] == job["result"]["download"]


def test_helpers():
    assert _public_url("https://u:p@host:8443/r.git") == "https://host:8443/r.git"
    assert _public_url("git@github.com:org/r.git") == "git@github.com:org/r.git"
    lines = ["2026-10-21 05:00:00,1 9 INFO db x: ok", "2026-10-21 05:00:01,1 9 ERROR db x: bad",
             "Traceback (most recent call last):", "2026-10-21 05:00:02,1 9 INFO db x: ok again"]
    assert _errors_only(lines) == lines[1:3]


async def test_mcp_access_logged(client):
    log = client.app.state.access_log
    await rpc(client, "tools/list", headers={**HEADERS, "Authorization": "Bearer nope"})
    await call(client, "list_servers")
    entries = log.recent()
    assert entries[0]["event"] == "mcp" and entries[0]["name"] == "tester"
    assert entries[0]["detail"] == "tools/call list_servers"
    assert entries[1]["event"] == "failed" and entries[1]["kind"] == "mcp" and entries[1]["name"] == ""
    # the tool still got its arguments after the guard read the body
    page = (await client.get("/access", headers={"Authorization": AUTH})).text
    assert "tools/call list_servers" in page and "MCP call" in page


async def token_headers(client, name, role):
    from .test_app import access

    data = (await access(client, "tokens/save", {"name": name, "role": role, "new": True})).json()
    return {**HEADERS, "Authorization": f"Bearer {data['token']}"}


async def test_token_roles_choose_the_tools(client, env):
    admin = await token_headers(client, "boss", "admin")
    viewer = await token_headers(client, "viewer-bot", "user")
    tools = lambda h: rpc(client, "tools/list", headers=h)  # noqa: E731
    names = {t["name"] for t in (await tools(admin)).json()["result"]["tools"]}
    assert {"read_server", "add_draft_server", "backup", "list_backups", "update_test"} <= names
    # the default token of the tests has no role: developer, as before roles
    names = {t["name"] for t in (await tools(HEADERS)).json()["result"]["tools"]}
    assert "update_test" in names and "add_draft_server" not in names and "backup" in names
    names = {t["name"] for t in (await tools(viewer)).json()["result"]["tools"]}
    assert "update_test" not in names and "list_servers" in names
    # a tool called anyway is refused
    assert "needs an MCP token with role admin" in (await call(client, "read_server", server_name="srv"))["error"]
    assert "role developer" in (await call(client, "update_test", headers=viewer, server_name="srv"))["error"]

    srv = await call(client, "read_server", headers=admin, server_name="srv")
    assert srv["status"] == "active" and srv["test"]["database"] == "DB_test"
    # a copy as a draft: never live until Test & activate on the web
    test = dict(srv["test"], database="DB_copy")
    added = await call(client, "add_draft_server", headers=admin, server_name="srv2", test=test)
    assert added["status"] == "draft" and "error" not in added, added
    settings = env["settings"]
    assert settings.servers["srv2"].status == "draft" and "srv2" in settings.servers_file.read_text()
    assert "already exists" in (await call(client, "add_draft_server", headers=admin, server_name="srv2",
                                           test=test))["error"]
    assert "Give production, test or both" in (await call(client, "add_draft_server", headers=admin,
                                                          server_name="srv3"))["error"]
    # incomplete: saved, and the answer says what is missing
    half = await call(client, "add_draft_server", headers=admin, server_name="srv3", test={"database": "X"})
    assert half["status"] == "draft" and half["error"]
    assert (await call(client, "read_server", headers=admin, server_name="srv3"))["error"] == half["error"]


async def test_admin_backs_up_production_and_downloads(client, env):
    admin = await token_headers(client, "boss", "admin")
    assert "role admin" in (await call(client, "backup", server_name="srv", role="production"))["error"]
    job = await call(client, "backup", headers=admin, server_name="srv", role="production", wait_seconds=30)
    assert job["status"] == "success", job
    listed = await call(client, "list_backups", headers=admin, server_name="srv", role="production")
    assert listed[0]["download"] == job["result"]["download"]
    small = await call(client, "backup", headers=admin, server_name="srv", role="production", wait_seconds=30,
                       options={"no_filestore": True})
    assert small["status"] == "success" and small["result"]["file"].endswith("_nofilestore.zip"), small
    for bad in ({"filestore": False}, {"no_filestore": "yes"}):
        answer = await call(client, "backup", headers=admin, server_name="srv", role="production", options=bad)
        assert "backup option" in str(answer.get("error")), (bad, answer)
    resp = await client.get(listed[0]["download"], headers={"Authorization": admin["Authorization"]})
    assert resp.status_code == 200 and resp.content == b"dump\n"


async def test_admin_reaches_draft_servers(client, env):
    admin = await token_headers(client, "boss", "admin")
    env["settings"].servers["srv"].status = "draft"
    # a developer token, as before: only active servers
    assert "srv is a draft" in (await call(client, "dev_setup", server_name="srv"))["error"]
    setup = await call(client, "dev_setup", headers=admin, server_name="srv")
    assert setup["database"] == "DB_test" and setup["backups"] == []
    job = await call(client, "backup", headers=admin, server_name="srv", role="test", wait_seconds=30)
    assert job["status"] == "success", job
    listed = await call(client, "list_backups", headers=admin, server_name="srv", role="test")
    assert listed[0]["download"] == job["result"]["download"]
    resp = await client.get(listed[0]["download"], headers={"Authorization": admin["Authorization"]})
    assert resp.status_code == 200 and resp.content == b"dump\n"
    # the download of a draft's backup: admins only
    assert (await client.get(listed[0]["download"], headers=BEARER)).status_code == 404
    # still no actions on a draft
    assert "srv is a draft" in (await call(client, "update_test", headers=admin, server_name="srv"))["error"]
    # a draft saved incomplete has no instances: the answer says why
    await call(client, "add_draft_server", headers=admin, server_name="half", test={"database": "X"})
    assert "saved incomplete" in (await call(client, "backup", headers=admin, server_name="half",
                                             role="test"))["error"]


async def test_a_token_of_a_workspace(client, env):
    """Made on the workspace's page by its admins: it sees that workspace's
    servers only, with its role there, never the machine's tools."""
    from omniacli.config import Workspace

    from .test_workspaces import post_as
    settings = env["settings"]
    settings.workspaces["acme"] = Workspace("acme", servers=[], members={"dev": "admin"})
    resp = await client.post("/api/workspaces/acme/tokens/save", headers=POST,
                             json={"name": "acme-bot", "role": "admin", "new": True})
    assert resp.status_code == 200, resp.text
    scoped = {**HEADERS, "Authorization": f"Bearer {resp.json()['token']}"}
    assert settings.token_workspace("acme-bot") == "acme" and "workspace: acme" in settings.users_file.read_text()
    names = {t["name"] for t in (await rpc(client, "tools/list", headers=scoped)).json()["result"]["tools"]}
    assert "update_production" in names and "read_server" not in names and "add_draft_server" not in names
    # srv is not acme's: not seen, not reached
    assert await call(client, "list_servers", headers=scoped) == []
    for tool, args in (("server_status", {}), ("read_log", {"role": "test"}), ("diagnose_instance", {}),
                       ("restart_instance", {"role": "test"})):
        answer = await call(client, tool, headers=scoped, server_name="srv", **args)
        assert "No active server srv" in answer["error"], (tool, answer)
    assert "token of the machine" in (await call(client, "read_server", headers=scoped, server_name="srv"))["error"]
    job = await call(client, "update_test", server_name="srv", wait_seconds=30)
    assert "No job" in (await call(client, "get_job", headers=scoped, job_id=job["id"]))["error"]
    assert await call(client, "list_jobs", headers=scoped) == []
    # over REST too it reads acme only (21/11/2026: it read the whole machine,
    # the other customers' backups too): srv is not acme's, the machine's pages are not its
    rest = {"Authorization": scoped["Authorization"]}
    for path in ("/api/servers/srv/test/log", "/api/servers/srv/test/database", "/config", "/users", "/links",
                 "/api/postgres", "/api/metrics/history", "/api/disks", "/backups/srv/test/x.zip"):
        assert (await client.get(path, headers=rest)).status_code in (403, 404), path
    listed = (await client.get("/api/servers", headers=rest))
    assert listed.status_code == 403 or "srv" not in listed.text
    # srv in acme: seen
    settings.workspaces["acme"].servers.append("srv")
    assert [s["name"] for s in await call(client, "list_servers", headers=scoped)] == ["srv"]
    # another workspace's admin does not touch it; its own token list is on its page
    page = (await client.get("/workspaces/acme", headers={"Authorization": AUTH})).text
    assert 'data-token="acme-bot"' in page
    settings.workspaces["beta"] = Workspace("beta", members={"dev": "admin"})
    resp = await client.post("/api/workspaces/beta/tokens/delete", headers=POST, json={"name": "acme-bot"})
    assert resp.status_code == 404
    resp = await client.post("/api/workspaces/acme/tokens/delete", headers=POST, json={"name": "acme-bot"})
    assert resp.status_code == 200 and "acme-bot" not in settings.tokens
    assert (await rpc(client, "tools/list", headers=scoped)).status_code == 401


async def test_acting_on_instances(client, env):
    """A test's with a developer token; a production's with an admin token
    and its name typed. diagnose_instance in one answer."""
    admin = await token_headers(client, "boss", "admin")
    job = await call(client, "restart_instance", server_name="srv", role="test", wait_seconds=30)
    assert job["status"] == "success" and "restart odoo-test" in env["calls"].read_text()
    answer = await call(client, "restart_instance", server_name="srv", role="production")
    assert "role admin" in answer["error"]
    answer = await call(client, "restart_instance", headers=admin, server_name="srv", role="production")
    assert "confirm='srv'" in answer["error"]
    job = await call(client, "restart_instance", headers=admin, server_name="srv", role="production",
                     confirm="srv", wait_seconds=30)
    assert job["status"] == "success" and "restart odoo-production" in env["calls"].read_text()
    # update_production: a backup first, always
    job = await call(client, "update_production", headers=admin, server_name="srv", confirm="srv", wait_seconds=30)
    assert job["status"] == "success", job
    assert any(line.endswith("--- a backup first") for line in job["log"]) or job["title"].startswith("Backup, git pull")
    assert list((env["settings"].backup_dir / "srv" / "production").glob("*.zip"))
    diagnosis = await call(client, "diagnose_instance", server_name="srv", role="production")
    assert diagnosis["server"] == "srv" and diagnosis["last_backup"] and diagnosis["last_jobs"]
    assert "log_errors" in diagnosis and "state" in diagnosis and diagnosis["reachable"]["state"] == "unknown"
    # the type's actions
    actions = await call(client, "list_actions", server_name="srv", role="test")
    assert actions and all({"id", "label", "role", "params"} <= set(a) for a in actions)
    assert "no action nope" in (await call(client, "run_action", server_name="srv", role="test", action="nope"))["error"]
    # stop_job: a finished job is not running
    assert "not running" in (await call(client, "stop_job", job_id=job["id"]))["error"]


async def test_a_workspace_token_reads_its_jobs_redacted(client, env):
    """MCP's last_jobs, list_jobs, get_job and job_log are what the pages and the REST API show its people
    (customerview.py): the provider's links and machines and this machine's folders taken out (step 5, 24/11)."""
    from omniacli.config import LinkTarget, Workspace
    settings = env["settings"]
    settings.workspaces["acme"] = Workspace("acme", servers=["srv"], members={"dev": "admin"})
    settings.link_targets = {"lab9": LinkTarget("lab9", "https://lab9.example.net", "t")}
    resp = await client.post("/api/workspaces/acme/tokens/save", headers=POST,
                             json={"name": "acme-bot", "role": "admin", "new": True})
    scoped = {**HEADERS, "Authorization": f"Bearer {resp.json()['token']}"}

    async def act(job):
        job.log("copied /var/lib/omniacli/backups/srv/test/x.zip to lab9")
        job.result = {"folder": "/srv/omniacli/acme/srv-test"}
    done = client.app.state.jobs.submit("Copy srv/test to lab9.example.net", ["srv/test"], "manual: admin", act)
    await wait_job(client, done.id)
    answers = [await call(client, "list_jobs", headers=scoped),
               await call(client, "get_job", headers=scoped, job_id=done.id),
               await call(client, "job_log", headers=scoped, job_id=done.id),
               (await call(client, "server_status", headers=scoped, server_name="srv"))["last_jobs"],
               (await call(client, "diagnose_instance", headers=scoped, server_name="srv", role="test"))["last_jobs"]]
    text = json.dumps(answers)
    assert done.id in text and "lab9" not in text and "/var/lib" not in text and "/srv/omniacli" not in text
    assert "manual: admin" not in text and "another machine" in text and "x.zip" in text
    # the machine's token reads it whole
    assert "lab9" in json.dumps(await call(client, "get_job", job_id=done.id))
