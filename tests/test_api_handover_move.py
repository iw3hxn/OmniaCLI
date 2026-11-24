# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The handover and the move through the REST API and MCP (AD-008 §9 step 10, UX-014 A13 and B6): every route of
/api/v1 for them and its MCP tool answer the same, with the pages' own code behind; their rules (a token of the
machine, never a CI's, the admin role, confirm = the name), the jobs started by the token, the code given once and
never kept for an Idempotency-Key, and a move asked of the home even for an instance placed elsewhere."""

import asyncio
import re

import pytest

from omniacli import apiv1, handover, placement

from .test_app import AUTH, POST, access  # noqa: F401
from .test_from_link import running
from .test_handover import URL, customer, provider  # noqa: F401 - the fixtures
from .test_mcp import HEADERS, call, rpc
from .test_move_back import place
from .test_staging_dialog import acme, two  # noqa: F401 - the fixtures
from .test_workspaces import post_as, workspaces  # noqa: F401 - the workspaces fixture

V1 = apiv1.PREFIX


@pytest.fixture
async def mcp(two):
    """two's app with its lifespan running (the MCP session manager), as uvicorn runs it."""
    async with running(two._transport.app.app):
        yield two


NEW_TOOLS = {"handover_status", "prepare_handover", "receive_workspace", "reception_status", "switch_workspace",
             "move_status", "move_instance", "move_back"}


async def token(client, name, role, **more):
    data = (await access(client, "tokens/save", {"name": name, "role": role, "new": True, **more})).json()
    assert data.get("token"), data
    return {"Authorization": f"Bearer {data['token']}"}


async def tokens(client):
    """An admin, a developer, a CI admin token of the machine, and an admin token of workspace acme."""
    found = {"admin": await token(client, "boss", "admin"), "developer": await token(client, "dev", "developer"),
             "ci": await token(client, "robot", "admin", ci=True)}
    made = (await client.post("/api/workspaces/acme/tokens/save", headers=post_as("mario"),
                              json={"name": "acme-bot", "role": "admin", "new": True})).json()
    found["workspace"] = {"Authorization": f"Bearer {made['token']}"}
    return found


def rest_error(resp):
    assert resp.status_code >= 400, (resp.status_code, resp.text)
    body = resp.json()
    assert set(body) == {"error"}, body
    return resp.status_code, body["error"]["code"], body["error"]["message"]


def mcp_error(found):
    assert "error" in found and ": " in found["error"], found
    said = re.sub(r"^Error executing tool \w+: ", "", found["error"])
    code, _, message = said.partition(": ")
    return code, message


# (REST method, path, body, MCP tool, its arguments): each tool of the handover and the move, with its route
STEPS = [
    ("GET", "/workspaces/acme/handover", None, "handover_status", {"workspace": "acme"}),
    ("POST", "/workspaces/acme/handover", {"confirm": "acme", "instances": ["acme-odoo"]}, "prepare_handover",
     {"workspace": "acme", "instances": ["acme-odoo"], "confirm": "acme"}),
    ("POST", "/handovers/receive", {"code": "nothing", "dry_run": False}, "receive_workspace",
     {"code": "nothing", "dry_run": False}),
    ("GET", "/handovers/0123456789abcdef", None, "reception_status", {"handover_id": "0123456789abcdef"}),
    ("POST", "/handovers/0123456789abcdef/switch", {"confirm": ""}, "switch_workspace",
     {"handover_id": "0123456789abcdef", "confirm": ""}),
    ("GET", "/moves/acme-odoo", None, "move_status", {"server_name": "acme-odoo"}),
    *[("POST", f"/moves/acme-odoo{'/' + tail if tail else ''}", {"confirm": "acme-odoo"}, "move_instance",
       {"server_name": "acme-odoo", "step": step, "confirm": "acme-odoo"}) for step, tail in apiv1.MOVE_STEPS.items()],
    *[("POST", f"/moves/acme-odoo/back{'/' + tail if tail else ''}", {"confirm": "acme-odoo"}, "move_back",
       {"server_name": "acme-odoo", "step": step, "confirm": "acme-odoo"}) for step, tail in apiv1.MOVE_STEPS.items()],
]


async def rest(client, headers, method, path, body):
    if method == "GET":
        return await client.get(V1 + path, headers=headers)
    return await client.post(V1 + path, headers=headers, json=body)


async def test_every_step_is_a_route_and_a_tool_of_the_machines_admin_tokens_only(two, acme, mcp):
    """Every route of the handover and the move has its tool; the tools are listed for an admin token of the
    machine only; a developer, a CI and a workspace's token are refused the same by both, before anything."""
    assert {(m, V1 + p.split("?")[0]) for m, p, *_ in STEPS} >= {
        (m, p.replace("{workspace}", "acme").replace("{server}", "acme-odoo").replace("{handover_id}", "0123456789abcdef"))
        for m, p in apiv1.V1_MACHINE_TOOLS}
    assert {t for names in apiv1.V1_MACHINE_TOOLS.values() for t in names} == NEW_TOOLS
    found = await tokens(two)
    for who, expected in (("admin", NEW_TOOLS), ("developer", set()), ("ci", set()), ("workspace", set())):
        listed = {t["name"] for t in (await rpc(two, "tools/list", headers={**HEADERS, **found[who]})).json()[
            "result"]["tools"]}
        assert listed & NEW_TOOLS == expected, (who, listed & NEW_TOOLS)
    for who, code in (("developer", "production_level_required"), ("ci", "production_by_hand"),
                      ("workspace", "machine_token_required")):
        for method, path, body, tool, arguments in STEPS:
            status, rest_code, rest_message = rest_error(await rest(two, found[who], method, path, body))
            mcp_code, mcp_message = mcp_error(await call(two, tool, headers={**HEADERS, **found[who]}, **arguments))
            assert (status, rest_code) == (403, code) and mcp_code == code, (who, path, rest_code, mcp_code)
            assert rest_message == mcp_message, (who, path)
    # the admin of the machine without the name: confirm_required, the same both ways, nothing started
    count = len(two._transport.app.app.state.jobs.jobs)
    for method, path, body, tool, arguments in STEPS:
        if body is None or "confirm" not in body or tool == "switch_workspace":
            continue
        status, rest_code, rest_message = rest_error(await rest(two, found["admin"], method, path, {**body,
                                                                                                    "confirm": "nope"}))
        mcp_code, mcp_message = mcp_error(await call(two, tool, headers={**HEADERS, **found["admin"]},
                                                     **{**arguments, "confirm": "nope"}))
        assert (status, rest_code, mcp_code) == (400, "confirm_required", "confirm_required"), path
        assert rest_message == mcp_message and "confirm=" in rest_message
    assert len(two._transport.app.app.state.jobs.jobs) == count
    # a step the move does not know
    assert "step is one of prepare, complete, cancel, delete_copy" in (await call(
        two, "move_instance", headers={**HEADERS, **found["admin"]}, server_name="acme-odoo", step="go"))["error"]


async def test_the_pages_refusals_are_the_apis_and_mcps(two, acme, env, mcp):
    """With the rules passed, what the page's own route answers is what the REST API and MCP answer: the same
    refusal, word for word, for each step of a move and of its return, before and while it is placed."""
    found = await tokens(two)
    admin = {**HEADERS, **found["admin"]}
    pages = {"prepare": "", "complete": "/complete", "cancel": "/cancel", "delete_copy": "/delete-copy"}

    async def compare(kind):
        for step, tail in pages.items():
            page_path = f"/api/servers/acme-odoo/move{'/back' if kind == 'back' else ''}{tail}"
            body = {"confirm": "acme-odoo", **({"link": "far"} if (kind, step) == ("move", "prepare") else {})}
            page = await two.post(page_path, headers=POST, json=body)
            api = await two.post(V1 + page_path.replace("/api/servers/acme-odoo/move", "/moves/acme-odoo"),
                                 headers=found["admin"], json=body)
            tool = await call(two, "move_instance" if kind == "move" else "move_back", headers=admin,
                              server_name="acme-odoo", step=step, confirm="acme-odoo",
                              **({"link": "far"} if (kind, step) == ("move", "prepare") else {}))
            if page.status_code == 200:
                assert api.status_code == 202 and "job" in api.json() and "id" in tool, (kind, step)
                continue
            status, _code, message = rest_error(api)
            assert status == page.status_code and message == page.json()["error"], (kind, step, message)
            assert mcp_error(tool)[1] == message, (kind, step, tool)
    await compare("move")
    await compare("back")
    place(env["settings"])
    await compare("back")
    # the move's own state read the same both ways
    plan = (await two.get(V1 + "/moves/acme-odoo", headers=found["admin"])).json()
    tool = await call(two, "move_status", headers=admin, server_name="acme-odoo")
    # (each machine's round trip and load are measured at each read)
    assert set(tool) == set(plan) and tool["move"] == plan["move"] and tool["refusals"] == plan["refusals"]
    assert [(t["link"], t["why"]) for t in tool["targets"]] == [(t["link"], t["why"]) for t in plan["targets"]]
    assert plan["move"]["state"] == "placed" and plan["workspace"] == "acme"
    page = (await two.get("/api/servers/acme-odoo/move", headers={"Authorization": AUTH})).json()
    assert set(page) == set(plan) and page["move"] == plan["move"]


async def test_a_step_that_starts_a_job_is_the_tokens_and_is_followed(two, acme, env, mcp):
    """Cancel a move that failed: the page's code starts its job, by «api: token boss» through REST and «mcp: token
    boss» through MCP, followed at /api/v1/jobs; the same Idempotency-Key answers the same job, another body a 409."""
    moves = placement.Moves(env["settings"].jobs_dir.parent)
    found = await tokens(two)
    jobs = two._transport.app.app.state.jobs
    moves.save("acme-odoo", {"link": "far", "state": "failed", "workspace": "acme", "instance": "odoo",
                             "kind": "cluster"})
    key = {**found["admin"], "Idempotency-Key": "cancel-1"}
    first = await two.post(V1 + "/moves/acme-odoo/cancel", headers=key, json={"confirm": "acme-odoo"})
    assert first.status_code == 202, first.text
    again = await two.post(V1 + "/moves/acme-odoo/cancel", headers=key, json={"confirm": "acme-odoo"})
    assert again.status_code == 202 and again.json()["job"]["id"] == first.json()["job"]["id"]
    assert again.headers.get("Idempotent-Replayed") == "true"
    other = await two.post(V1 + "/moves/acme-odoo/cancel", headers=key, json={"confirm": "acme-odoo", "x": 1})
    assert other.status_code in (409, 422)
    job = jobs.get(first.json()["job"]["id"])
    assert job.source == "api: token boss" and job.title == "Move acme-odoo to far: cancel"
    assert first.json()["follow"] == f"{V1}/jobs/{job.id}"
    for _ in range(200):
        if job.status not in ("queued", "running"):
            break
        await asyncio.sleep(0.05)
    moves.save("acme-odoo", {"link": "far", "state": "failed", "workspace": "acme", "instance": "odoo",
                             "kind": "cluster"})
    answer = await call(two, "move_instance", headers={**HEADERS, **found["admin"]}, server_name="acme-odoo",
                        step="cancel", confirm="acme-odoo", wait_seconds=5)
    assert answer["source"] == "mcp: token boss" and answer["title"] == "Move acme-odoo to far: cancel"


async def test_a_move_of_a_placed_instance_is_asked_of_its_home(two, acme, env, mcp):
    """An instance placed elsewhere whose copy here is gone (its other routes and tools are its host's): its move
    and its return are answered here, the home's, through REST and MCP alike (AD-014 §2)."""
    place(env["settings"], copy="deleted")
    found = await tokens(two)
    resp = await two.get(V1 + "/moves/acme-odoo", headers=found["admin"])
    assert resp.status_code == 200 and resp.json()["move"]["state"] == "placed", resp.text
    tool = await call(two, "move_status", headers={**HEADERS, **found["admin"]}, server_name="acme-odoo")
    assert tool["move"]["state"] == "placed"
    resp = await two.post(V1 + "/moves/acme-odoo/back/complete", headers=found["admin"],
                          json={"confirm": "acme-odoo"})
    assert resp.status_code == 400 and "is not ready" in resp.json()["error"]["message"]
    # its routes are not a server's path, which the home sends to the host (placement.server_of_path)
    assert placement.server_of_path(V1 + "/moves/acme-odoo/back") == ("", "")


async def test_a_handover_prepared_through_mcp_received_and_read_through_rest(customer, provider, env, mcp):
    """prepare_handover gives the code once (an Idempotency-Key answers code null, the file of the keys never holds
    it); the customer's machine reads it and receives it with its own admin token through REST, the reception
    followed there; its switch refused without its name, the same both ways."""
    found = await tokens(provider)
    admin = {**HEADERS, **found["admin"]}
    view = await call(provider, "handover_status", headers=admin, workspace="acme")
    rest_view = (await provider.get(V1 + "/workspaces/acme/handover", headers=found["admin"])).json()
    # the same, but the dates of a code made now (each read says them at its own second)
    now = ("expires", "delete_on")
    assert {k: v for k, v in view.items() if k not in now} == {k: v for k, v in rest_view.items() if k not in now}
    assert "acme-odoo" in {r["server"] for r in view["rows"]} and view["url_why"] == ""
    made = await call(provider, "prepare_handover", headers=admin, workspace="acme", instances=["acme-odoo"],
                      confirm="acme", idempotency_key="hand-1")
    code = made["code"]
    assert handover.read_code(code)["u"] == URL and made["handover"]["phase"] == "prepared"
    again = await call(provider, "prepare_handover", headers=admin, workspace="acme", instances=["acme-odoo"],
                       confirm="acme", idempotency_key="hand-1")
    assert again["code"] is None and again["handover"]["phase"] == "prepared"
    kept = (env["settings"].jobs_dir.parent / "api-idempotency.json").read_text()
    assert code not in kept and handover.read_code(code)["s"] not in kept
    # one handover at a time, said the same as the page says it
    resp = await provider.post(V1 + "/workspaces/acme/handover", headers=found["admin"],
                               json={"confirm": "acme", "instances": ["acme-odoo"]})
    assert resp.status_code == 400 and "prepared already" in resp.json()["error"]["message"]
    jobs = provider._transport.app.app.state.jobs
    opened = [j for j in jobs.recent(20) if "opened for the copy" in j.title]
    assert opened and opened[0].source == "mcp: token boss"
    # the customer's machine, its own admin token
    async with customer as c:
        made = (await c.post("/api/access/tokens/save", headers={"Authorization": AUTH, "Origin": "http://far.test"},
                             json={"name": "mine", "role": "admin", "new": True})).json()
        mine = {"Authorization": f"Bearer {made['token']}"}
        read = await c.post(V1 + "/handovers/receive", headers=mine, json={"code": code})
        assert read.status_code == 200, read.text
        offer = read.json()["offer"]
        assert read.json()["id"] == handover.read_code(code)["h"] and offer["workspace"]["name"] == "acme"
        assert offer["provider"] == "Studio Rossi" and offer["resume"] is False
        bad = await c.post(V1 + "/handovers/receive", headers=mine, json={"code": "nothing"})
        assert rest_error(bad)[2] == "The code is not valid"
        # reading claims nothing there
        assert handover.Out(env["settings"].jobs_dir.parent).state("acme")["phase"] == "prepared"
        hid = read.json()["id"]
        status, code_, _message = rest_error(await c.get(V1 + f"/handovers/{hid}", headers=mine))
        assert (status, code_) == (404, "not_found")
        status, code_, message = rest_error(await c.post(V1 + f"/handovers/{hid}/switch", headers=mine,
                                                         json={"confirm": "acme"}))
        assert (status, code_) == (404, "not_found")


async def test_the_rest_document_lists_the_new_routes_with_their_descriptions(two, acme, mcp):
    found = await tokens(two)
    doc = (await two.get(V1 + "/openapi.json", headers=found["admin"])).json()
    for method, path in apiv1.V1_MACHINE_TOOLS:
        op = doc["paths"][path][method.lower()]
        assert op["summary"] and op["description"], path
    body = doc["components"]["schemas"]["ReceiveBody"]["properties"]
    assert body["dry_run"]["default"] is True and "never in a URL" in body["code"]["description"]
    # a workspace's token reads the routes of its workspace only: the handover and the move are not among them
    from omniacli.app import WORKSPACE_ROUTES
    assert not {p for _m, p in apiv1.V1_MACHINE_TOOLS} & set(WORKSPACE_ROUTES)


async def test_a_token_limited_to_some_servers_reaches_their_moves_only_and_no_handover(two, acme, env, mcp):
    """A machine's admin token limited to some servers (Settings › Users › API e assistenti AI › Servers): the move of
    another server answered as the rest of /api/v1 answers an instance it does not reach (404, no plan, no job), the
    same through MCP; a handover, a reception and a switch act on a whole workspace or on the machine: refused to it,
    and their tools not listed to it. The move of a server of its list: read, done and followed as the token's."""
    other = await token(two, "lim", "admin", servers=["srv"])
    mine = await token(two, "lim2", "admin", servers=["acme-odoo"])
    moves = placement.Moves(env["settings"].jobs_dir.parent)
    moves.save("acme-odoo", {"link": "far", "state": "failed", "workspace": "acme", "instance": "odoo",
                             "kind": "cluster"})
    jobs = two._transport.app.app.state.jobs
    count = len(jobs.jobs)
    whole = {"handover_status", "prepare_handover", "receive_workspace", "reception_status", "switch_workspace"}
    # as the rest of /api/v1 refuses an instance out of its list
    unseen = await two.get(V1 + "/instances/acme-odoo/production", headers=other)
    assert unseen.status_code == 404
    for method, path, body, tool, arguments in STEPS:
        status, rest_code, rest_message = rest_error(await rest(two, other, method, path, body))
        mcp_code, mcp_message = mcp_error(await call(two, tool, headers={**HEADERS, **other}, **arguments))
        if tool in whole:
            assert (status, rest_code, mcp_code) == (403, "forbidden", "forbidden"), (path, rest_code, mcp_code)
            assert "some servers only" in rest_message, rest_message
        else:
            assert (status, rest_code, mcp_code) == (404, "not_found", "not_found"), (path, rest_code, mcp_code)
            assert rest_message == unseen.json()["error"]["message"]
        assert rest_message == mcp_message, path
    # a token of acme-odoo's alone is refused the handover of its workspace all the same
    status, rest_code, _ = rest_error(await rest(two, mine, "GET", "/workspaces/acme/handover", None))
    assert (status, rest_code) == (403, "forbidden")
    assert len(jobs.jobs) == count
    # its tools: the move's (of the servers it reaches), never the whole workspace's or the machine's
    for headers in (other, mine):
        listed = {t["name"] for t in (await rpc(two, "tools/list", headers={**HEADERS, **headers})).json()[
            "result"]["tools"]}
        assert listed & NEW_TOOLS == NEW_TOOLS - whole, listed & NEW_TOOLS
    # the move of its own server: read, done, and its job followed by the token that started it
    assert (await two.get(V1 + "/moves/acme-odoo", headers=mine)).status_code == 200
    done = await two.post(V1 + "/moves/acme-odoo/cancel", headers=mine, json={"confirm": "acme-odoo"})
    assert done.status_code == 202, done.text
    job_id = done.json()["job"]["id"]
    assert jobs.get(job_id).source == "api: token lim2"
    assert (await two.get(V1 + f"/jobs/{job_id}", headers=mine)).status_code == 200
    assert (await two.get(V1 + f"/jobs/{job_id}", headers=other)).status_code == 404
