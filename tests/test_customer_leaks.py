# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a customer (a user or a token of a workspace) read of the provider's machine and should not have, found by
the proof of step 5 of §9 of docs/adr/AD-008-consegna-e-spostamento.md on lab3/lab4: this machine's name in the
New staging dialog, the project's folder in the Dashboard's search, in the Pull image question and in MCP's
server_status; and the lists of Instances and Backups seen through a central panel's link (their requests left
out its prefix, so they asked the central panel). A member deleted leaves no session behind."""

import json
import re

from omniacli import machines as machines_module

from .test_app import AUTH, LINK_TOKEN, POST, client  # noqa: F401 - client is a fixture
from .test_delegated_scope import HEADERS_MCP, as_user, pair  # noqa: F401 - the fixture
from .test_staging_dialog import acme, two  # noqa: F401 - the fixtures
from .test_workspaces import post_as, workspaces  # noqa: F401 - the workspaces fixture


async def test_the_new_staging_dialog_names_the_machine_to_its_admins_only(two, acme):
    here = machines_module.here_name(__import__("socket").gethostname())
    admin = (await two.get("/api/servers/acme-odoo/stagings/new", headers={"Authorization": AUTH})).json()
    assert admin["here"] == here
    customer = await two.get("/api/servers/acme-odoo/stagings/new", headers=post_as("mario"))
    assert customer.status_code == 200 and customer.json()["here"] == "" and here not in customer.text


async def test_a_customer_reads_no_folder_of_the_machine(two, acme, tmp_path):
    folder = str(tmp_path / "acme" / "odoo")
    # the Dashboard's rows: its search text says …/<last part>, never the project's folder
    for who, sees in (({"Authorization": AUTH}, True), (post_as("mario"), False)):
        page = (await two.get("/", headers=who)).text
        assert (folder in page) is sees, who
    # the question of Pull image names the instance, not its folder (on the page of every reader)
    page = (await two.get("/servers/acme-odoo", headers=post_as("mario"))).text
    admin = (await two.get("/servers/acme-odoo", headers={"Authorization": AUTH})).text
    assert "Pull the newer images of acme-odoo/production and make its containers again?" in admin
    assert folder not in page


async def test_mcp_server_status_says_no_folder_to_a_workspaces_token(pair):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "server_status", "arguments": {"server_name": "srv"}}})
    resp = await pair.post("/mcp", headers={**HEADERS_MCP, **pair.tokens["as-admin"]}, content=body)
    assert resp.status_code == 200 and "isError\":true" not in resp.text.replace(" ", "")
    assert '\\"project\\"' not in resp.text and not re.search(r"/(?:var|srv|tmp|home)/", resp.text), resp.text[:2000]
    body = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "list_servers", "arguments": {}}})
    resp = await pair.post("/mcp", headers={**HEADERS_MCP, **pair.tokens["as-admin"]}, content=body)
    assert resp.status_code == 200 and not re.search(r"/(?:var|srv|tmp|home)/", resp.text), resp.text[:2000]


async def test_the_lists_through_a_central_panels_link_ask_under_its_prefix(client):

    link = {"Authorization": f"Bearer {LINK_TOKEN}", "X-OmniaCLI-Prefix": "/links/cliente", "X-OmniaCLI-User": "devuser"}
    for page, api in (("/instances", "/api/instances"), ("/backups", "/api/backups")):
        text = (await client.get(page, headers=link)).text
        assert f'"/links/cliente" + "{api}"' in text, page
        assert f'= "{api}"' not in text, page


async def test_a_member_deleted_leaves_no_session(pair):
    app = pair.app
    sessions = app.state.sessions
    sessions.create("as-user", app.state.settings.users["as-user"])
    assert any(v.get("user") == "as-user" for v in sessions._sessions.values())
    resp = await pair.post("/api/workspaces/acme/members/delete", headers=as_user("mario"), json={"name": "as-user"})
    assert resp.status_code == 200 and resp.json()["deleted"] is True
    assert not any(v.get("user") == "as-user" for v in sessions._sessions.values())
    on_disk = json.loads(sessions.path.read_text()) if sessions.path.exists() else {}
    assert "as-user" not in json.dumps(on_disk)
