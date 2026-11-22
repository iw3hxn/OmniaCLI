# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The REST API for scripts and CI (README, REST API): its tokens' levels and
scopes, every route, the production updated by hand only, the rate limit, the
idempotency keys, the access log, its OpenAPI document of /api/v1 only, and
its routes in step with MCP's tools (the same token, the same rules)."""

import json
import re

import pytest
from fastapi.routing import APIRoute

from omniacli import apiguard, apiv1

from .conftest import TOKEN
from .test_app import AUTH, POST, access, client, wait_job  # noqa: F401 - the client fixture
from .test_deploys import push, staged  # noqa: F401 - the staged fixture
from .test_workspaces import post_as, workspaces  # noqa: F401 - the workspaces fixture

V1 = apiv1.PREFIX
DEVELOPER = {"Authorization": f"Bearer {TOKEN}"}  # the tests' token: developer, as before roles


async def token(client, name, role, **more):
    data = (await access(client, "tokens/save", {"name": name, "role": role, "new": True, **more})).json()
    assert data.get("token"), data
    return {"Authorization": f"Bearer {data['token']}"}


async def done(client, headers, job_id):
    for _ in range(200):
        job = (await client.get(f"{V1}/jobs/{job_id}", headers=headers)).json()
        if job["status"] not in ("queued", "running"):
            return job
        import asyncio
        await asyncio.sleep(0.05)
    raise AssertionError("job did not finish")


def error_of(resp, status, code):
    assert resp.status_code == status, (resp.status_code, resp.text)
    body = resp.json()
    assert set(body) == {"error"} and body["error"]["code"] == code and body["error"]["message"], body
    return body["error"]["message"]


async def test_the_document_lists_v1_only_with_its_descriptions(client):
    no = await client.get(f"{V1}/openapi.json")
    error_of(no, 401, "unauthorized")
    for headers in (DEVELOPER, {"Authorization": AUTH}):
        doc = (await client.get(f"{V1}/openapi.json", headers=headers)).json()
        assert doc["info"]["title"] == apiv1.TITLE and doc["components"]["securitySchemes"]["bearer"]["scheme"] == "bearer"
    listed = {(m.upper(), p) for p, ops in doc["paths"].items() for m in ops}
    assert listed == set(apiv1.V1_TOOLS), listed ^ set(apiv1.V1_TOOLS)
    for path, ops in doc["paths"].items():
        for method, op in ops.items():
            assert op.get("summary") and op.get("description"), (method, path)
            assert all(p.get("description") for p in op.get("parameters") or []), (method, path)
    for name, schema in doc["components"]["schemas"].items():
        if name in ("HTTPValidationError", "ValidationError") or not schema.get("properties"):
            continue
        missing = [f for f, v in schema["properties"].items() if not v.get("description")]
        assert not missing, (name, missing)
    # the docs page, for a token and for a logged-in user, in English with the page in Italian around
    for headers in (DEVELOPER, {"Authorization": AUTH, "Accept-Language": "it"}):
        page = await client.get(f"{V1}/docs", headers=headers)
        assert page.status_code == 200 and "/api/v1/instances/{server}/{role}/restart" in page.text
        assert "Restart an instance" in page.text and "confirm" in page.text
    assert "Le route per gli script e la CI" in page.text


async def test_every_action_of_v1_is_an_mcp_tool(client):
    """A route of /api/v1 without its MCP tool fails here (Carlo, 22/11/2026: «Oltre che api rest deve sempre
    esserci mcp così si può collegare a claude»)."""
    from omniacli import mcp_server  # noqa: F401 - the SDK is there

    routes = set()
    for route in client.app.routes:
        # whichever way FastAPI keeps an included router (test_audit._get_routes)
        inner = [(getattr(route, "include_context", None) and route.include_context.prefix or "", r)
                 for r in route.original_router.routes] if hasattr(route, "original_router") else [("", route)]
        for prefix, r in inner:
            if isinstance(r, APIRoute) and (prefix + r.path).startswith(V1 + "/"):
                routes |= {(m, prefix + r.path) for m in r.methods}
    assert routes == set(apiv1.V1_TOOLS) | apiv1.DOCUMENT_ROUTES, routes ^ (set(apiv1.V1_TOOLS) | apiv1.DOCUMENT_ROUTES)
    from .test_mcp import HEADERS, rpc
    admin = await token(client, "boss", "admin")
    tools = {t["name"] for t in (await rpc(client, "tools/list", headers={**HEADERS, **admin})).json()["result"]["tools"]}
    missing = {route: names for route, names in apiv1.V1_TOOLS.items() if not set(names) <= tools}
    assert not missing, missing
    # what changes something is a write in MCP's rate limit too
    writes = {n for (method, _), names in apiv1.V1_TOOLS.items() if method == "POST" for n in names}
    assert writes <= mcp_server.WRITE_TOOLS


async def test_reads_and_the_read_level(client, env):
    reader = await token(client, "reader", "user")
    rows = (await client.get(f"{V1}/instances", headers=reader)).json()
    assert {(r["server"], r["role"]) for r in rows} == {("srv", "production"), ("srv", "test")}
    prod = next(r for r in rows if r["role"] == "production")
    assert prod["by_hand"] and prod["state"] in ("running", "stopped", "unknown") and prod["repositories"]
    one = (await client.get(f"{V1}/instances/srv/test", headers=reader)).json()
    assert set(one["commits"]) == {"odoo", "addons"} and one["role"] == "test"
    error_of(await client.get(f"{V1}/instances/srv/nope", headers=reader), 404, "not_found")
    error_of(await client.get(f"{V1}/instances/nope/test", headers=reader), 404, "not_found")
    # a read token changes nothing
    message = error_of(await client.post(f"{V1}/instances/srv/test/restart", headers=reader), 403, "read_only_token")
    assert "read level" in message
    # a session reads the pages, not the API; a token outside the API only reads
    error_of(await client.get(f"{V1}/instances", headers={"Authorization": AUTH}), 401, "unauthorized")
    error_of(await client.get(f"{V1}/instances", headers={"Authorization": "Bearer nope"}), 401, "unauthorized")
    assert (await client.post("/servers/srv/test/restart", headers={**DEVELOPER, "Origin": "http://omniacli.test"})
            ).status_code == 403
    error_of(await client.get(f"{V1}/jobs/nope", headers=reader), 404, "not_found")
    error_of(await client.get(f"{V1}/jobs?limit=0", headers=reader), 422, "invalid_request")


async def test_a_backup_twice_with_one_key_follow_and_download(client, env):
    log = client.app.state.access_log
    headers = {**DEVELOPER, "Idempotency-Key": "ci-run-41"}
    first = await client.post(f"{V1}/instances/srv/test/backups", headers=headers, json={"options": {}})
    assert first.status_code == 202, first.text
    again = await client.post(f"{V1}/instances/srv/test/backups", headers=headers, json={"options": {}})
    assert again.status_code == 202 and again.headers["Idempotent-Replayed"] == "true"
    job_id = first.json()["job"]["id"]
    assert again.json()["job"]["id"] == job_id and first.json()["follow"] == f"{V1}/jobs/{job_id}"
    assert len([j for j in client.app.state.jobs.recent(50) if j.title.startswith("Backup srv/test")]) == 1
    # the same key with another body: 409
    error_of(await client.post(f"{V1}/instances/srv/test/backups", headers=headers,
                               json={"options": {"no_filestore": True}}), 409, "idempotency_conflict")
    error_of(await client.post(f"{V1}/instances/srv/test/backups", headers={**DEVELOPER, "Idempotency-Key": "a b"}),
             400, "invalid_idempotency_key")
    error_of(await client.post(f"{V1}/instances/srv/test/backups", headers=DEVELOPER, json={"options": {"x": 1}}),
             400, "invalid_request")
    job = await done(client, DEVELOPER, job_id)
    assert job["status"] == "success" and job["source"] == "api: token tester", job
    # its log from an offset
    whole = (await client.get(f"{V1}/jobs/{job_id}/log", headers=DEVELOPER)).json()
    assert whole["finished"] and whole["lines"] and whole["next_offset"] == len(whole["lines"])
    tail = (await client.get(f"{V1}/jobs/{job_id}/log?offset=0&limit=1", headers=DEVELOPER)).json()
    assert tail["lines"] == whole["lines"][:1] and tail["next_offset"] == 1 and not tail["finished"]
    listed = (await client.get(f"{V1}/instances/srv/test/backups", headers=DEVELOPER)).json()
    assert listed[0]["file"] == job["result"]["file"] and listed[0]["download"] == job["result"]["download"]
    got = await client.get(listed[0]["download"], headers=DEVELOPER)
    assert got.status_code == 200 and got.content == b"dump\n"
    reader = await token(client, "reader", "user")
    error_of(await client.get(listed[0]["download"], headers=reader), 403, "forbidden")
    error_of(await client.get(f"{V1}/instances/srv/test/backups/nope.zip", headers=DEVELOPER), 404, "not_found")
    # the access log: the writes with the token's name, the list of backups as a read, the download
    entries = log.recent()
    posts = [e for e in entries if e["event"] == "action" and e.get("route") == f"{V1}/instances/{{server}}/{{role}}/backups"]
    assert posts and all(e["name"] == "tester" and e["kind"] == "token" for e in posts)
    assert {e["status"] for e in posts} >= {202, 409}
    assert any(e["event"] == "read" and e["what"] == "backups list (REST API)" and e["name"] == "tester" for e in entries)
    assert any(e["event"] == "download" and e["name"] == "tester" and e["detail"].startswith("srv/test/") for e in entries)


async def test_actions_and_the_production_by_hand(staged, client, env):
    calls = env["calls"]
    resp = await client.post(f"{V1}/instances/srv/test/restart", headers=DEVELOPER)
    assert resp.status_code == 202, resp.text
    assert (await done(client, DEVELOPER, resp.json()["job"]["id"]))["status"] == "success"
    assert "restart odoo-test" in calls.read_text()
    # a production: the production level (admin) and its name in confirm, never a CI token
    message = error_of(await client.post(f"{V1}/instances/srv/production/restart", headers=DEVELOPER), 403,
                       "production_level_required")
    assert "role admin" in message
    boss = await token(client, "boss", "admin")
    ci = await token(client, "ci-bot", "admin", ci=True)
    for action in ("restart", "stop", "start", "pull"):
        message = error_of(await client.post(f"{V1}/instances/srv/production/{action}", headers=boss, json={}), 400,
                           "confirm_required")
        assert "confirm='srv'" in message
        error_of(await client.post(f"{V1}/instances/srv/production/{action}", headers=boss, json={"confirm": "stg"}),
                 400, "confirm_required")
        error_of(await client.post(f"{V1}/instances/srv/production/{action}", headers=ci, json={"confirm": "srv"}),
                 403, "production_by_hand")
    error_of(await client.post(f"{V1}/instances/srv/production/backups", headers=ci), 403, "production_by_hand")
    error_of(await client.post(f"{V1}/instances/srv/production/backups", headers=DEVELOPER), 403,
             "production_level_required")
    error_of(await client.post(f"{V1}/instances/srv/production/deploys/x/back", headers=ci, json={"confirm": "srv"}),
             403, "production_by_hand")
    assert "restart odoo-production" not in calls.read_text()
    resp = await client.post(f"{V1}/instances/srv/production/restart", headers=boss, json={"confirm": "srv"})
    assert resp.status_code == 202, resp.text
    assert (await done(client, boss, resp.json()["job"]["id"]))["status"] == "success"
    assert "restart odoo-production" in calls.read_text()
    # a production's backup: the admin, no confirm (it does not change it)
    resp = await client.post(f"{V1}/instances/srv/production/backups", headers=boss)
    assert resp.status_code == 202 and (await done(client, boss, resp.json()["job"]["id"]))["status"] == "success"
    # a staging's production is what the CI is for: no confirm, a CI token too
    rows = {(r["server"], r["role"]): r for r in (await client.get(f"{V1}/instances", headers=ci)).json()}
    assert rows[("stg", "production")]["staging_of"] == "srv" and not rows[("stg", "production")]["by_hand"]
    resp = await client.post(f"{V1}/instances/stg/production/restart", headers=ci)
    assert resp.status_code == 202, resp.text
    assert (await done(client, ci, resp.json()["job"]["id"]))["status"] == "success"
    assert "restart odoo-stg" in calls.read_text()
    from .conftest import sh
    addons = next(r for r in env["settings"].servers["stg"].production.repositories if r.name == "addons")
    sh("git", "remote", "set-url", addons.remote, str(env["remotes"]["addons"]), cwd=addons.path)
    resp = await client.post(f"{V1}/instances/stg/production/pull", headers=ci)
    job = await done(client, ci, resp.json()["job"]["id"])
    assert job["status"] == "success" and job["title"] == "Git pull and restart stg/production", job
    # MCP, the same rules with the same tokens
    from .test_mcp import HEADERS, call
    answer = await call(client, "restart_instance", headers={**HEADERS, **ci}, server_name="srv", role="production",
                        confirm="srv")
    assert "production_by_hand: A production is updated by hand only" in answer["error"], answer
    answer = await call(client, "restart_instance", headers={**HEADERS, **ci}, server_name="stg", role="production",
                        wait_seconds=30)
    assert answer["status"] == "success", answer
    assert "production_by_hand" in (await call(client, "update_production", headers={**HEADERS, **ci},
                                               server_name="srv", confirm="srv"))["error"]


async def test_deploys_listed_and_one_gone_back_to(client, env, tmp_path):
    addons = next(r for r in env["settings"].servers["srv"].test.repositories if r.name == "addons")
    from .conftest import sh
    shas = []
    for message in ("v1", "v2"):
        shas.append(push(env, tmp_path, "addons", "test", message))
        resp = await client.post(f"{V1}/instances/srv/test/pull", headers=DEVELOPER)
        assert resp.status_code == 202 and (await done(client, DEVELOPER, resp.json()["job"]["id"]))["status"] == "success"
    data = (await client.get(f"{V1}/instances/srv/test/deploys", headers=DEVELOPER)).json()
    assert [d["current"] for d in data["deploys"]] == [True, False] and not data["ci"]
    first = data["deploys"][-1]
    assert first["repositories"][0]["after"] == shas[0] and first["by"] == "api: token tester"
    reader = await token(client, "reader", "user")
    error_of(await client.get(f"{V1}/instances/srv/test/deploys", headers=reader), 403, "forbidden")
    error_of(await client.post(f"{V1}/instances/srv/test/deploys/nope/back", headers=DEVELOPER), 404, "not_found")
    resp = await client.post(f"{V1}/instances/srv/test/deploys/{first['id']}/back", headers=DEVELOPER, json={})
    assert resp.status_code == 202, resp.text
    assert (await done(client, DEVELOPER, resp.json()["job"]["id"]))["status"] == "success"
    assert sh("git", "rev-parse", "HEAD", cwd=addons.path) == shas[0]
    error_of(await client.post(f"{V1}/instances/srv/test/deploys/{first['id']}/back", headers=DEVELOPER), 400,
             "refused")
    # MCP: the same list, the same flow
    from .test_mcp import call
    listed = await call(client, "list_deploys", server_name="srv", role="test")
    assert listed["deploys"][0]["current"] and len(listed["deploys"]) == 3
    answer = await call(client, "deploy_back", server_name="srv", role="test", deploy_id=listed["deploys"][1]["id"],
                        wait_seconds=30)
    assert answer["status"] == "success", answer
    assert sh("git", "rev-parse", "HEAD", cwd=addons.path) == shas[1]
    log = await call(client, "job_log", job_id=answer["id"], offset=0, limit=2)
    assert len(log["lines"]) == 2 and log["next_offset"] == 2


async def test_the_scope_of_a_token(workspaces, staged, client, env):
    """A token of some servers sees those only, of a workspace that workspace's only; MCP the same."""
    limited = await token(client, "only-stg", "developer", servers=["stg"])
    rows = (await client.get(f"{V1}/instances", headers=limited)).json()
    assert {r["server"] for r in rows} == {"stg"}
    error_of(await client.post(f"{V1}/instances/srv/test/restart", headers=limited), 404, "not_found")
    assert all(set(j["instances"]) <= {"stg/production"}
               for j in (await client.get(f"{V1}/jobs", headers=limited)).json())
    # outside the API it reaches nothing (a page lists every server)
    assert (await client.get("/api/servers", headers=limited)).status_code == 403
    from .test_mcp import HEADERS, call
    servers = await call(client, "list_servers", headers={**HEADERS, **limited})
    assert [s["name"] for s in servers] == ["stg"]
    # a workspace's token: acme holds srv
    made = (await client.post("/api/workspaces/acme/tokens/save", headers=post_as("mario"),
                              json={"name": "acme-ci", "role": "developer", "new": True, "ci": True})).json()
    acme = {"Authorization": f"Bearer {made['token']}"}
    assert client.app.state.settings.token_ci("acme-ci")
    rows = (await client.get(f"{V1}/instances", headers=acme)).json()
    assert {r["server"] for r in rows} == {"srv"}
    error_of(await client.get(f"{V1}/instances/stg/production", headers=acme), 403, "forbidden")
    resp = await client.post(f"{V1}/instances/srv/test/restart", headers=acme)
    assert resp.status_code == 202, resp.text
    job = await done(client, acme, resp.json()["job"]["id"])
    assert job["source"] == "api: token acme-ci"
    # its servers: of its workspace only
    resp = await client.post("/api/workspaces/acme/tokens/save", headers=post_as("mario"),
                             json={"name": "acme-ci", "role": "developer", "ci": True, "servers": "stg"})
    assert resp.status_code == 400 and "No server stg" in resp.json()["error"]


async def test_the_rate_limit(client, env, monkeypatch):
    guard = client.app.state.api_guard
    monkeypatch.setattr(guard, "limiter", apiguard.RateLimiter(reads=3, writes=1))
    for _ in range(3):
        assert (await client.get(f"{V1}/instances", headers=DEVELOPER)).status_code == 200
    resp = await client.get(f"{V1}/instances", headers=DEVELOPER)
    error_of(resp, 429, "rate_limited")
    assert int(resp.headers["Retry-After"]) >= 1
    assert (await client.post(f"{V1}/instances/srv/test/restart", headers=DEVELOPER)).status_code == 202
    error_of(await client.post(f"{V1}/instances/srv/test/restart", headers=DEVELOPER), 429, "rate_limited")
    # another token has its own; MCP counts in the same
    other = await token(client, "other", "developer")
    assert (await client.get(f"{V1}/instances", headers=other)).status_code == 200
    from .test_mcp import rpc
    resp = await rpc(client, "tools/call", {"name": "list_servers", "arguments": {}})
    assert resp.status_code == 429 and resp.headers["Retry-After"]
    assert any(e["event"] == "mcp" and e.get("status") == 429 for e in client.app.state.access_log.recent())


def test_the_limiter_and_the_keys(tmp_path):
    limiter = apiguard.RateLimiter(reads=2, writes=1, window=60)
    assert limiter.hit("a", False, 0) == 0 and limiter.hit("a", False, 1) == 0
    assert limiter.hit("a", False, 2) == 58 and limiter.hit("a", False, 60.5) == 0
    assert limiter.hit("a", True, 0) == 0 and limiter.hit("a", True, 1) == 59 and limiter.hit("b", True, 1) == 0
    keys = apiguard.Idempotency(tmp_path / apiguard.FILE)
    print_ = apiguard.fingerprint("POST", "/x", b'{"a": 1, "b": 2}')
    assert print_ == apiguard.fingerprint("POST", "/x", b'{"b":2,"a":1}')
    assert keys.lookup("t", "k", print_) is None
    keys.remember("t", "k", print_, 202, {"job": {"id": "j1"}})
    assert (tmp_path / apiguard.FILE).stat().st_mode & 0o777 == 0o600
    again = apiguard.Idempotency(tmp_path / apiguard.FILE)
    assert again.lookup("t", "k", print_)["body"] == {"job": {"id": "j1"}}
    assert again.lookup("other", "k", print_) is None
    with pytest.raises(apiguard.ApiError) as found:
        again.lookup("t", "k", apiguard.fingerprint("POST", "/x", b"{}"))
    assert found.value.status == 409


async def test_a_staging_made_and_mcp_idempotent(workspaces, client, env, monkeypatch, tmp_path):
    """POST /instances/{server}/stagings is new_staging, the pages' code; MCP's create_staging the same, and an
    MCP tool with an idempotency_key answers one job."""
    calls = []

    async def new_staging(server, me, load=None, title="", extra=None, source=""):
        calls.append((server, str(me), source))
        job = client.app.state.jobs.submit("Staging x of srv", ["srv/production"], source, lambda job: _noop())
        return {"job": job.id, "server": "acme-odoo-stg-1"}

    async def _noop():
        return None
    # the code of the pages is tested in test_workspaces (a dated staging): here only its wiring
    monkeypatch.setitem(client.app.state.api_hooks, "staging", new_staging)
    resp = await client.post(f"{V1}/instances/srv/stagings", headers=DEVELOPER)
    assert resp.status_code == 202, resp.text
    assert resp.json()["server"] == "acme-odoo-stg-1" and calls == [("srv", "token tester", "api: token tester")]
    reader = await token(client, "reader", "user")
    error_of(await client.post(f"{V1}/instances/srv/stagings", headers=reader), 403, "read_only_token")
    error_of(await client.post(f"{V1}/instances/nope/stagings", headers=DEVELOPER), 404, "not_found")
    from .test_mcp import call
    answer = await call(client, "create_staging", server_name="srv")
    assert answer["server"] == "acme-odoo-stg-1" and calls[-1] == ("srv", "token tester", "mcp: token tester")
    one = await call(client, "restart_instance", server_name="srv", role="test", idempotency_key="k-1")
    two = await call(client, "restart_instance", server_name="srv", role="test", idempotency_key="k-1")
    assert one["id"] == two["id"]
    other = await call(client, "stop_instance", server_name="srv", role="test", idempotency_key="k-1")
    assert "idempotency_conflict" in other["error"], other


async def test_the_token_page_says_how_to_connect(client, env):
    page = (await client.get("/users", headers={"Authorization": AUTH, "Accept-Language": "it"})).text
    assert "API e assistenti AI" in page and 'data-connect' in page and 'id="connect-dialog"' in page
    for snippet in ("claude mcp add --transport http omniacli", "mcp-remote", "bearer_token_env_var",
                    "opencode.ai/config.json", "${input:omniacli-token}", "/api/v1"):
        assert snippet in page, snippet
    assert "claude.ai e ChatGPT si collegano con OAuth: non ancora disponibile." in page
    assert TOKEN not in page
    # the CI mark and the servers saved and read back
    resp = await access(client, "tokens/save", {"name": "ci-bot", "role": "developer", "new": True, "ci": True,
                                                "servers": "srv"})
    assert resp.status_code == 200 and resp.json()["token"]
    settings = client.app.state.settings
    assert settings.token_ci("ci-bot") and settings.token_servers("ci-bot") == ("srv",)
    saved = settings.users_file.read_text() if settings.users_file else ""
    assert not saved or ("ci: true" in saved and resp.json()["token"] not in saved)
    page = (await client.get("/users", headers={"Authorization": AUTH})).text
    assert re.search(r'data-token="ci-bot"[^>]*data-ci="1" data-servers="srv"', page)
    assert (await access(client, "tokens/save", {"name": "x", "role": "user", "new": True, "servers": ["nope"]})
            ).status_code == 400
    resp = await access(client, "tokens/save", {"name": "ci-bot", "role": "developer", "ci": False, "servers": ""})
    assert resp.status_code == 200 and not settings.token_ci("ci-bot") and not settings.token_servers("ci-bot")
    assert (await access(client, "tokens/delete", {"name": "ci-bot"})).status_code == 200
    assert "ci-bot" not in settings.token_cis
    json.dumps(settings.web.token_servers)
