# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Invitations: a user made without a password gets a one-time link, valid
72 hours, where the person sets its own and is logged in; expired, used,
sent again or revoked links say so. A customer's user is made from the
Users page in a workspace of this machine or of a linked one."""
import time
from urllib.parse import urlsplit

import httpx
import pytest
import yaml

from omniacli.app import create_app
from omniacli.config import LinkAccess, Workspace, load_access, load_invitations, parse_settings

from .conftest import LINK_TOKEN, USER_HASH, BasicAsSession, hash_token
from .test_app import AUTH, HOST, POST, client  # noqa: F401 - the client fixture
from .test_workspaces import basic, post_as, workspaces  # noqa: F401 - the workspaces fixture

JSON = {**POST, "Accept": "application/json"}
NEW = "a new password 1"


def form(password: str, again: str | None = None) -> dict:
    return {"password": password, "again": password if again is None else again}


async def accept(client, path: str, password: str = NEW, again: str | None = None):
    """The invited person's browser: no credentials, the form of the page."""
    return await client.post(path, data=form(password, again), headers={"Origin": f"http://{HOST}"})


def entries(client, event: str = "invitation") -> list[dict]:
    """The entries of event, the oldest first."""
    return [e for e in reversed(client.app.state.access_log.recent()) if e["event"] == event]


async def test_an_invited_person_sets_its_password_and_is_in(client, env):
    resp = await client.post("/api/access/users/save", headers=JSON,
                             json={"name": "anna", "role": "developer", "new": True, "invite": True})
    data = resp.json()
    assert resp.status_code == 200 and data["invite_url"] == f"http://{HOST}{data['invite_path']}"
    assert data["invite_path"].startswith("/invite/") and len(data["invite_path"]) > 40 and data["expires"]
    settings = env["settings"]
    # a user with a password nobody knows, in the users file as any other (an OmniaCLI of before reads it)
    assert settings.user_role("anna") == "developer" and "anna" in load_access(settings.users_file).users
    stored = load_invitations(settings.users_file.with_name("invitations.yaml"))
    assert [(i.name, i.by) for i in stored] == [("anna", "admin")] and data["invite_path"][8:] not in str(stored)
    assert 72 * 3600 - 5 < stored[0].expires - stored[0].created <= 72 * 3600
    page = (await client.get("/users", headers={"Authorization": AUTH})).text
    assert "invitation waiting, until " in page and "data-resend" in page and "data-revoke" in page
    # the page of the link, then the password: too short, two different ones, then right
    page = await client.get(data["invite_path"])
    assert page.status_code == 200 and 'value="anna"' in page.text and 'name="again"' in page.text
    assert page.headers["cache-control"] == "no-store" and page.headers["referrer-policy"] == "same-origin"
    assert (await accept(client, data["invite_path"], "short")).status_code == 400
    wrong = await accept(client, data["invite_path"], NEW, "another one 1")
    assert wrong.status_code == 400 and "The two passwords are not the same" in wrong.text
    cross = await client.post(data["invite_path"], data=form(NEW), headers={"Origin": "http://evil.test"})
    assert cross.status_code == 403
    done = await accept(client, data["invite_path"])
    assert done.status_code == 303 and done.headers["location"] == "/" and "omniacli_session" in done.headers["set-cookie"]
    assert client.app.state.authenticator.check("anna", NEW)
    # used: the link says so, and works no more
    again = await client.get(data["invite_path"])
    assert again.status_code == 410 and "This invitation was used on " in again.text
    assert (await accept(client, data["invite_path"], "yet another 1")).status_code == 410
    assert client.app.state.authenticator.check("anna", NEW)
    page = (await client.get("/users", headers={"Authorization": AUTH})).text
    assert "invitation waiting" not in page
    # the access log: made by the admin, accepted by the person, and its login
    details = [e["detail"] for e in entries(client)]
    assert details[0].startswith("made for anna, valid until ") and details[1] == "accepted: anna set its password"
    assert entries(client)[1]["name"] == "anna" and any(e["detail"] == "invitation" for e in entries(client, "login"))


async def test_expired_sent_again_and_revoked(client, env, monkeypatch):
    data = (await client.post("/api/access/users/save", headers=JSON,
                              json={"name": "bruno", "role": "user", "new": True, "invite": True})).json()
    first = data["invite_path"]
    later = time.time() + 73 * 3600
    monkeypatch.setattr(time, "time", lambda: later)
    expired = await client.get(first)
    assert expired.status_code == 410 and "This invitation expired on " in expired.text
    assert (await accept(client, first)).status_code == 410
    page = (await client.get("/users", headers={"Authorization": AUTH})).text
    assert "invitation expired on " in page
    # sent again: a new link, the old one no longer valid
    resent = (await client.post("/api/access/invitations/resend", headers=JSON, json={"name": "bruno"})).json()
    assert resent["invite_path"] != first and (await client.get(resent["invite_path"])).status_code == 200
    old = await client.get(first)
    assert old.status_code == 410 and "This link is not valid" in old.text
    # a user with its password has no invitation to send
    refused = await client.post("/api/access/invitations/resend", headers=JSON, json={"name": "viewer"})
    assert refused.status_code == 404
    # revoked: the user, who never got in, is gone, and the link is not valid
    assert (await client.post("/api/access/invitations/revoke", headers=JSON, json={"name": "bruno"})).json()["ok"]
    assert "bruno" not in env["settings"].users
    assert (await client.get(resent["invite_path"])).status_code == 410
    assert [e["detail"].split(",")[0] for e in entries(client)] == [
        "made for bruno", "sent again for bruno", "revoked for bruno: the user is deleted"]


async def test_a_password_set_by_the_admin_ends_the_invitation(client, env):
    data = (await client.post("/api/access/users/save", headers=JSON,
                              json={"name": "carla", "role": "user", "new": True, "invite": True})).json()
    assert (await client.post("/api/access/users/save", headers=JSON,
                              json={"name": "carla", "role": "user", "password": "chosen by admin"})).json()["ok"]
    assert (await client.get(data["invite_path"])).status_code == 410
    # and a new user with a password, as before
    resp = await client.post("/api/access/users/save", headers=JSON, json={"name": "dino", "role": "user", "new": True})
    assert resp.status_code == 400 and "at least 8" in resp.json()["error"]


async def test_a_workspace_admin_invites_into_its_workspace(workspaces, client):
    resp = await client.post("/api/workspaces/acme/members/save", headers={**post_as("mario"), "Accept": "application/json"},
                             json={"name": "gino", "role": "developer", "new": True, "invite": True})
    data = resp.json()
    assert resp.status_code == 200 and data["invite_path"].startswith("/invite/")
    assert workspaces.workspaces["acme"].members["gino"] == "developer" and workspaces.user_role("gino") == "workspace"
    page = (await client.get("/workspaces/acme", headers=basic("mario"))).text
    assert "invitation waiting, until " in page and "data-resend" in page and 'id="invite-dialog"' in page
    # luca is a user there, not an admin; beta's eva may not touch acme's
    for who in ("luca", "eva"):
        assert (await client.post("/api/workspaces/acme/members/invitation/resend",
                                  headers={**post_as(who), "Accept": "application/json"},
                                  json={"name": "gino"})).status_code == 403
    resent = await client.post("/api/workspaces/acme/members/invitation/resend",
                               headers={**post_as("mario"), "Accept": "application/json"}, json={"name": "gino"})
    assert resent.status_code == 200 and resent.json()["invite_path"] != data["invite_path"]
    assert (await accept(client, resent.json()["invite_path"])).status_code == 303
    assert client.app.state.authenticator.check("gino", NEW)
    assert any(e.get("workspace") == "acme" for e in entries(client))


async def test_the_workspace_list_opens_the_workspace(workspaces, client):
    page = (await client.get("/workspaces", headers={"Authorization": AUTH})).text
    assert '"{{ prefix }}/workspaces/"' not in page and '"/workspaces/" + encodeURIComponent(row.dataset.ws)' in page
    assert 'href="/workspaces?edit=acme"' in page


@pytest.fixture
async def central(env, tmp_path):
    """This machine (central, a workspace here) linked to a customer's
    (customer.test, workspace acme there), and to one that is down."""
    customer_settings = env["settings"]
    customer_settings.fixed.links["omnia"] = LinkAccess(hash_token(LINK_TOKEN), "admin")
    customer_settings.set_web_access(customer_settings.web)
    customer_settings.workspaces = {"acme": Workspace("acme", "ACME", members={})}
    customer = create_app(customer_settings)
    conf = tmp_path / "central"
    conf.mkdir()
    (conf / "links.yaml").write_text(yaml.safe_dump({"links": {
        "cliente": {"url": "http://customer.test", "token": LINK_TOKEN},
        "down": {"url": "https://down.test", "token": "whatever"},
    }}))
    settings = parse_settings({"auth": {"users": {"admin": USER_HASH}}, "backup_dir": str(tmp_path / "b"),
                               "jobs_dir": str(tmp_path / "j"), "servers_file": "servers.yaml"}, base_dir=conf)
    settings.workspaces = {"here": Workspace("here", members={})}

    class Network(httpx.AsyncBaseTransport):
        def __init__(self):
            self.asgi = httpx.ASGITransport(app=customer)

        async def handle_async_request(self, request):
            if request.url.host == "down.test":
                raise httpx.ConnectError("connection refused", request=request)
            return await self.asgi.handle_async_request(request)

    app = create_app(settings, link_transport=Network())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=BasicAsSession(app)),
                                 base_url="http://central.test") as c:
        c.app, c.customer = app, customer
        yield c


async def test_a_customers_user_on_a_linked_machine_from_the_users_page(central):
    headers = {"Authorization": AUTH, "Accept": "application/json"}
    machines = (await central.get("/api/access/workspace-choices", headers=headers)).json()["machines"]
    assert [(m["machine"], [w["name"] for w in m["workspaces"]]) for m in machines] == [
        ("", ["here"]), ("cliente", ["acme"]), ("down", [])]
    assert machines[2]["error"].startswith("it does not answer")
    page = (await central.get("/users", headers={"Authorization": AUTH})).text
    assert 'id="id-workspace"' in page and 'name="id-way" value="invite" checked' in page
    resp = await central.post("/api/access/workspace-users/save", headers={**headers, "Origin": "http://central.test"},
                              json={"machine": "cliente", "workspace": "acme", "name": "ugo", "role": "user",
                                    "invite": True})
    data = resp.json()
    assert resp.status_code == 200 and data["page"] == "/links/cliente/workspaces/acme#users"
    assert data["invite_url"] == "http://customer.test" + data["invite_path"] and not data["through_ssh"]
    # made there, not here: a customer's user of acme only
    customer = central.customer.state.settings
    assert "ugo" not in central.app.state.settings.users and customer.workspaces["acme"].members == {"ugo": "user"}
    assert customer.user_role("ugo") == "workspace"
    # the person opens the link on the customer's machine
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=central.customer),
                                 base_url="http://customer.test") as there:
        assert (await there.get(data["invite_path"])).status_code == 200
        done = await there.post(data["invite_path"], data=form(NEW), headers={"Origin": "http://customer.test"})
        assert done.status_code == 303
    assert central.customer.state.authenticator.check("ugo", NEW)
    # here: in a workspace of this machine, with a password
    resp = await central.post("/api/access/workspace-users/save", headers={**headers, "Origin": "http://central.test"},
                              json={"machine": "", "workspace": "here", "name": "vera", "role": "admin",
                                    "password": NEW})
    assert resp.status_code == 200 and resp.json()["page"] == "/workspaces/here#users"
    assert central.app.state.settings.workspaces["here"].members == {"vera": "admin"}
    # refusals: no workspace, a machine that is not linked, one that does not answer, a workspace not there
    for body, code in (({"machine": "", "workspace": ""}, 400), ({"machine": "nope", "workspace": "acme"}, 404),
                       ({"machine": "down", "workspace": "acme"}, 502), ({"machine": "cliente", "workspace": "zzz"}, 404)):
        resp = await central.post("/api/access/workspace-users/save", headers={**headers, "Origin": "http://central.test"},
                                  json={**body, "name": "x1", "role": "user", "invite": True})
        assert resp.status_code == code, (body, resp.text)
    # through the link, the customer's own Users page hands out its address in the link
    resp = await central.post("/links/cliente/api/access/users/save", headers={**headers, "Origin": "http://central.test"},
                              json={"name": "zeno", "role": "user", "new": True, "invite": True})
    assert urlsplit(resp.json()["invite_url"]).netloc == "customer.test"
