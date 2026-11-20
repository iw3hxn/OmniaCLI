# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Log in as (loginas.py): a session of a user of the instance, made by the
plugin, handed to the browser on the instance's own host through a token
used once."""

import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
import yaml

from omniacli import composehelper, loginas, workspacehelper, workspaceweb
from omniacli.app import create_app
from omniacli.composehelper import HelperError
from omniacli.config import parse_settings
from omniacli.odoo import OdooPlugin
from omniacli.odoo import ops as odoo_ops
from omniacli.plugin_api import login_answer

from omniacli.auth import hash_token

from .conftest import USER_HASH, BasicAsSession
from .test_links import HOST, as_user

SID = "a-session-id-of-odoo"
USERS = [{"id": 2, "login": "admin", "name": "Administrator", "internal": True, "admin": True},
         {"id": 7, "login": "mario", "name": "Mario Rossi", "internal": True, "admin": False}]
PRODUCTION = "erp.example.it"
TEST = "test.erp.example.it"


@pytest.fixture
async def here(env, tmp_path, monkeypatch):
    """srv of env, its instances opened at their domains over https, with a
    plugin that answers as login_session.py would."""
    asked = []

    async def login_session(self, instance, request):
        asked.append((instance.key, request))
        if request == "users":
            return {"users": USERS}
        if request == "99":
            return {"error": "no active user with id 99"}
        user = next(u for u in USERS if str(u["id"]) == request)
        return {"sid": SID, "id": user["id"], "login": user["login"], "name": user["name"], "home": "/odoo"}

    monkeypatch.setattr(OdooPlugin, "login_session", login_session)
    conf = tmp_path / "here"
    conf.mkdir()
    servers = yaml.safe_load(env["settings"].servers_file.read_text())
    servers["servers"]["srv"]["production"]["domain"] = f"https://{PRODUCTION}"
    servers["servers"]["srv"]["test"]["domain"] = f"https://{TEST}"
    (conf / "servers.yaml").write_text(yaml.safe_dump(servers))
    settings = parse_settings({
        "auth": {"users": {"admin": USER_HASH, "dev": {"password": USER_HASH, "role": "developer"},
                           "viewer": {"password": USER_HASH, "role": "user"}},
                 "links": {"mine": {"token": hash_token("a-link-token"), "role": "admin"}}},
        "backup_dir": str(tmp_path / "here-backups"), "jobs_dir": str(tmp_path / "here" / "jobs"),
        "servers_file": "servers.yaml",
    }, base_dir=conf)
    app = create_app(settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=BasicAsSession(app)),
                                 base_url=f"http://{HOST}") as c:
        c.asked, c.settings = asked, settings
        yield c


def api(role):
    return f"/api/servers/srv/{role}/login-as"


async def ask(client, user, role, uid):
    return await client.post(api(role), headers={**as_user(user, post=True), "Accept": "application/json"},
                             json={"id": uid})


async def test_a_developer_logs_in_as_a_user_of_test(here):
    resp = await here.get(f"{api('test')}/users", headers=as_user("dev"))
    assert resp.status_code == 200 and resp.json() == {"users": USERS}
    resp = await ask(here, "dev", "test", 7)
    assert resp.status_code == 200, resp.text
    link = urlsplit(resp.json()["url"])
    assert (link.scheme, link.netloc, link.path) == ("https", TEST, loginas.LOGIN_PATH)
    assert SID not in resp.json()["url"]
    assert ("srv/test", "7") in here.asked
    # the link opened on the instance's host (its nginx passes the path to OmniaCLI)
    landing = await here.get(f"{link.path}?{link.query}", headers={"Host": TEST})
    assert landing.status_code == 303
    assert landing.headers["location"] == f"https://{TEST}/odoo"
    cookie = landing.headers["set-cookie"]
    assert cookie.startswith(f"session_id={SID};")
    assert "HttpOnly" in cookie and "Secure" in cookie and "Path=/" in cookie
    assert landing.headers["cache-control"] == "no-store"
    # once only
    again = await here.get(f"{link.path}?{link.query}", headers={"Host": TEST})
    assert again.status_code == 403 and "set-cookie" not in again.headers
    lines = [json.loads(line) for line in (here.settings.logs_dir / "access.jsonl").read_text().splitlines()]
    seen = [(e["event"], e["name"], e["detail"]) for e in lines if e["event"] == "login-as"]
    assert seen == [("login-as", "dev", "asked: srv/test as mario (id 7)"),
                    ("login-as", "dev", "opened: srv/test as mario (id 7)")]


async def test_a_link_opened_on_another_host_is_refused_and_spent(here):
    url = urlsplit((await ask(here, "dev", "test", 7)).json()["url"])
    wrong = await here.get(f"{url.path}?{url.query}", headers={"Host": PRODUCTION})
    assert wrong.status_code == 403 and "set-cookie" not in wrong.headers
    right = await here.get(f"{url.path}?{url.query}", headers={"Host": TEST})
    assert right.status_code == 403
    assert (await here.get(f"{loginas.LOGIN_PATH}?t=made-up", headers={"Host": TEST})).status_code == 403


async def test_a_person_through_a_link_logs_in_as_a_user(here):
    """The pages of a machine seen through a link: its OmniaCLI acts for one
    of the other's users, who may log in as a user; the link alone may not."""
    link = {"Authorization": "Bearer a-link-token", "Accept": "application/json"}
    resp = await here.post(api("production"), headers={**link, "x-omniacli-user": "carlo"}, json={"id": 2})
    assert resp.status_code == 200, resp.text
    assert urlsplit(resp.json()["url"]).netloc == PRODUCTION
    assert (await here.post(api("production"), headers=link, json={"id": 2})).status_code == 403
    page = (await here.get("/servers/srv", headers={**link, "x-omniacli-user": "carlo"})).text
    assert "data-login-as" in page


async def test_a_production_is_an_admins(here):
    assert (await ask(here, "dev", "production", 2)).status_code == 403
    assert (await here.get(f"{api('production')}/users", headers=as_user("dev"))).status_code == 403
    assert (await ask(here, "viewer", "test", 2)).status_code == 403
    resp = await ask(here, "admin", "production", 2)
    assert resp.status_code == 200
    assert urlsplit(resp.json()["url"]).netloc == PRODUCTION
    # the button: for whom may use it
    page = (await here.get("/servers/srv", headers=as_user("dev"))).text
    assert "data-login-as=" not in page
    page = (await here.get("/servers/srv?view=test", headers=as_user("dev"))).text
    assert page.count("data-login-as=") == 1 and "login-as-dialog" in page
    for view in ("production", "test"):
        page = (await here.get(f"/servers/srv?view={view}", headers=as_user("admin"))).text
        assert page.count("data-login-as=") == 1
    page = (await here.get("/servers/srv", headers=as_user("viewer"))).text
    assert "data-login-as=" not in page


async def test_what_the_plugin_refuses_is_said(here):
    resp = await ask(here, "dev", "test", 99)
    assert resp.status_code == 502 and resp.json()["error"] == "no active user with id 99"
    for bad in ("7", True, 0, None):
        assert (await ask(here, "dev", "test", bad)).status_code == 400


async def test_an_instance_without_https_domain_says_what_it_needs(env, monkeypatch):
    async def login_session(self, instance, request):
        raise AssertionError("not asked")

    monkeypatch.setattr(OdooPlugin, "login_session", login_session)
    app = create_app(env["settings"])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=BasicAsSession(app)),
                                 base_url=f"http://{HOST}") as c:
        resp = await ask(c, "dev", "test", 7)
    assert resp.status_code == 409 and "Domain name" in resp.json()["detail"]


def test_tickets_expire_and_are_used_once():
    now = [100.0]
    tickets = loginas.LoginTickets(seconds=60, clock=lambda: now[0])
    token = tickets.issue("sid", "Erp.Example.it", "https://erp.example.it/odoo", "dev", "srv/test as mario")
    assert tickets.take(token, "erp.example.it").sid == "sid"
    assert tickets.take(token, "erp.example.it") is None
    late = tickets.issue("sid", "erp.example.it", "/odoo", "dev", "")
    now[0] += 61
    assert tickets.take(late, "erp.example.it") is None
    assert loginas.safe_home("/odoo") == "/odoo" and loginas.safe_home("https://evil.test/") == "/web"


def test_the_answer_of_the_script():
    out = "2026 INFO odoo: loading\n\nOMNIACLI-LOGIN " + json.dumps({"sid": "x", "home": "/odoo"})
    assert login_answer(0, out) == {"sid": "x", "home": "/odoo"}
    assert login_answer(1, "Traceback\nboom")["error"].startswith("no answer (exit code 1): Traceback | boom")


def test_the_helper_runs_only_the_script_of_the_package(tmp_path, monkeypatch):
    monkeypatch.setattr(composehelper, "services", lambda project: ["odoo", "db"])
    cmds = composehelper.commands(tmp_path, "login", ["odoo", "imp", "users"])
    assert cmds == [["exec", "-T", "odoo", "sh", "-c", composehelper.SCRIPTS["login"], "sh", "imp", "users"]]
    assert composehelper.commands(tmp_path, "login", ["odoo", "imp", "7"])[0][-1] == "7"
    for bad in (["odoo", "imp", "0"], ["odoo", "imp", "7;id"], ["odoo", "imp"], ["odoo", "i mp", "7"],
                ["nginx", "imp", "7"]):
        with pytest.raises(HelperError):
            composehelper.commands(tmp_path, "login", bad)
    assert composehelper.LOGIN_SCRIPT.is_file()
    assert 'OMNIACLI_LOGIN="$2" exec odoo shell -d "$1"' in composehelper.SCRIPTS["login"]


async def test_the_native_odoo_runs_the_script_with_click_odoo(env):
    instance = env["settings"].instance("srv", "test")
    venv = instance.options.venv_bin
    (venv / "click-odoo").write_text('#!/bin/sh\necho "args: $*"\necho \'OMNIACLI-LOGIN {"users": []}\'\n')
    assert await odoo_ops.login_session(instance, "users") == {"users": []}
    (venv / "click-odoo").write_text("#!/bin/sh\necho no odoo here\nexit 1\n")
    assert "no odoo here" in (await odoo_ops.login_session(instance, "7"))["error"]


def test_the_workspace_site_passes_the_path_to_omniacli(tmp_path):
    sites = [{"instance": "shop", "role": "production", "names": ["shop.acme.it"], "http_port": 20001,
              "websocket_port": 0}]
    text = workspaceweb.conf_text("acme", sites, ("/c/f.pem", "/c/k.pem"), False, "127.0.0.1:8090")
    block = text.split(f"location = {loginas.LOGIN_PATH} {{", 1)[1].split("}", 1)[0]
    assert "proxy_pass http://127.0.0.1:8090;" in block and "proxy_set_header Host $host;" in block
    # and the webhooks of deploy on push
    block = text.split(f"location ^~ {workspaceweb.HOOKS_PATH} {{", 1)[1].split("}", 1)[0]
    assert "proxy_pass http://127.0.0.1:8090;" in block
    assert loginas.LOGIN_PATH not in workspaceweb.conf_text("acme", sites, ("/c/f.pem", "/c/k.pem"), False)
    # port 80 sends everything to https, the login path too
    assert loginas.LOGIN_PATH not in workspaceweb.conf_text("acme", sites, None, False, "127.0.0.1:8090")


@pytest.mark.parametrize("lines, address", [
    ("", "127.0.0.1:8090"),
    ("#OMNIACLI_PORT=9000\n", "127.0.0.1:8090"),
    ("OMNIACLI_PORT=9000\n", "127.0.0.1:9000"),
    ("OMNIACLI_HOST=0.0.0.0\nOMNIACLI_PORT='8091'\n", "127.0.0.1:8091"),
    ("OMNIACLI_HOST=10.0.0.5\nOMNIACLI_PORT=http\n", "10.0.0.5:8090"),
    ("OMNIACLI_HOST=a;b\n", "127.0.0.1:8090"),
])
def test_the_address_of_omniacli_for_nginx(tmp_path, lines, address):
    defaults = tmp_path / "omniacli"
    defaults.write_text(lines)
    assert workspacehelper.omniacli_address(defaults) == address
    assert workspacehelper.omniacli_address(Path("/nonexistent")) == "127.0.0.1:8090"


async def test_a_panels_nginx_that_does_not_pass_the_path(here, monkeypatch):
    """The nginx of a hosting panel answers the instance's name and passes
    /.omniacli/login to Odoo: said before listing the users, with the
    address that adds the line for an admin of the machine only; the
    landing answers a probe with its mark."""
    from omniacli import workspaceops

    seen = {"passes": False}

    async def reaches_here(host):
        return seen["passes"]
    monkeypatch.setattr(loginas, "reaches_here", reaches_here)
    resp = await here.get(f"{api('test')}/users", headers=as_user("dev"))
    assert resp.status_code == 409 and resp.json()["nginx"] is None and TEST in resp.json()["error"]
    resp = await here.get(f"{api('production')}/users", headers=as_user("admin"))
    assert resp.status_code == 409 and resp.json()["nginx"] == f"{api('production')}/nginx"
    assert (await here.post(f"{api('test')}/nginx", headers=as_user("dev", post=True))).status_code == 403
    helper = []

    async def fake_helper(job, *args, timeout=None):
        helper.append(args)
        seen["passes"] = True
        return {"changed": ["/etc/nginx/sites-enabled/x.conf"], "nginx": "nginx reloaded", "undo": "…"}
    monkeypatch.setattr(workspaceops, "_helper", fake_helper)
    resp = await here.post(f"{api('test')}/nginx", headers={**as_user("admin", post=True), "Accept": "application/json"})
    assert resp.status_code == 200, resp.text
    for _ in range(100):
        job = (await here.get(f"/api/jobs/{resp.json()['job']}", headers=as_user("admin"))).json()
        if job["status"] not in ("queued", "running"):
            break
        await asyncio.sleep(0.05)
    assert job["status"] == "success" and helper == [("login-path", "add", TEST)]
    assert (await here.get(f"{api('test')}/users", headers=as_user("dev"))).status_code == 200
    probe = await here.get(f"{loginas.LOGIN_PATH}?probe=1", headers={"Host": TEST})
    assert probe.status_code == 204 and probe.headers[loginas.MARK] == "1"
