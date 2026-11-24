# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""AD-017 slices 1 and 2: the contract of a linked machine (agent_api, plugins) and the 12 pages of class A that
the panel draws itself for a link with pages: here, from the model the machine answers; the machine's own page,
with a line on top, when the contract does not allow it; pages: machine as the way back; and a machine's HTML
refused where the panel takes data only."""
import asyncio
import base64
import dataclasses
import gzip
import json
import re
import shutil
import subprocess

import httpx
import pytest
import yaml

from omniacli import app as app_module
from omniacli import centralpages, destinations, enterprise, gitcredentials, pullcrypto
from omniacli.app import create_app
from omniacli.config import (LinkAccess, LinkTarget, Server, SshRoute, parse_connection, parse_settings,
                             parse_workspaces)
from omniacli.listgroups import ListGroup

from .conftest import LINK_TOKEN, PASSWORD, USER_HASH, BasicAsSession, hash_token
from .test_app import AUTH
from .serverparity import SECRETS, Twice, keys, main_of, secret_keys
from .test_delegated_scope import as_user, pair  # noqa: F401 - the pair fixture (a home and its host)

N = centralpages.CENTRAL_AGENT_API
PAGE = {"Authorization": AUTH, "Accept": "text/html,application/xhtml+xml"}
JSON = {"Authorization": AUTH, "Accept": "application/json"}
# a page of each of the 12 of class A and the instance's page (slice 3), as the browser opens it under /links/<name>/
PATHS = {"disks": "disks", "log": "servers/srv/test/log", "own_log": "omniacli/log", "instances": "instances",
         "backups": "backups", "machines": "machines", "discover": "config/discover",
         "connection": "pullers/connections/nobody", "shell": "servers/srv/test/shell", "help": "help",
         "about": "about", "settings": "settings", "server": "servers/srv"}


class Machine(httpx.AsyncBaseTransport):
    """The customer's machine behind the link: what it was asked, and what it says of itself (agent_api,
    plugins) as the test sets it; html_at: an answer of HTML for that path, as a machine that writes one."""

    def __init__(self, app):
        self.asgi = httpx.ASGITransport(app=app)
        self.asked: list[str] = []
        self.info: dict | None = None
        self.html_at = ""
        # path: (status, headers, body) the machine answers instead
        self.answer: dict[str, tuple] = {}
        self.accepts: list[str] = []

    async def handle_async_request(self, request):
        self.asked.append(request.url.path)
        self.accepts.append(request.headers.get("accept", ""))
        if self.html_at and request.url.path == self.html_at:
            return httpx.Response(200, headers={"content-type": "text/html"}, stream=httpx.ByteStream(b"<script>alert(1)</script>"))
        if request.url.path in self.answer:
            code, headers, body = self.answer[request.url.path]
            return httpx.Response(code, headers=headers, stream=httpx.ByteStream(body))
        resp = await self.asgi.handle_async_request(request)
        if request.url.path == "/api/info" and self.info is not None:
            await resp.aread()
            data = {k: v for k, v in json.loads(resp.content).items() if k not in ("agent_api", "plugins")}
            data.update(self.info)
            return httpx.Response(resp.status_code, json=data)
        return resp


@pytest.fixture
async def central(env, tmp_path):
    """This panel, linked to a customer's machine (customer.test) whose link is admin there."""
    machine_settings = env["settings"]
    machine_settings.fixed.links["omnia"] = LinkAccess(hash_token(LINK_TOKEN), "admin")
    machine_settings.set_web_access(machine_settings.web)
    # a puller's connection there: its page has the commands only who logged in there sends
    machine_settings.connections = {"api-prod": parse_connection("api-prod", {"link": "vps", "puller": "api-prod"})}
    machine = Machine(create_app(machine_settings))
    conf = tmp_path / "central"
    conf.mkdir()
    (conf / "links.yaml").write_text(yaml.safe_dump({"links": {
        "cliente": {"url": "http://customer.test", "token": LINK_TOKEN}}}))
    settings = parse_settings({"auth": {"users": {"admin": USER_HASH, "dev": {"password": USER_HASH, "role": "developer"},
                                                  "viewer": {"password": USER_HASH, "role": "user"}}},
                               "backup_dir": str(tmp_path / "b"),
                               "jobs_dir": str(tmp_path / "j"), "servers_file": "servers.yaml"}, base_dir=conf)
    app = create_app(settings, link_transport=machine)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=BasicAsSession(app)),
                                 base_url="http://central.test") as c:
        c.app, c.machine = app, machine
        yield c


def link_pages(central, pages: str) -> None:
    settings = central.app.state.settings
    link = settings.link_targets["cliente"]
    settings.link_targets = {"cliente": type(link)(link.name, link.url, link.token, pages=pages)}


# --- the contract (slice 1) ---------------------------------------------------------------------------------

@pytest.mark.parametrize("needs, agent_api, theirs, why", [
    (N, N, {}, ""),  # the same: drawn here
    (N - 1, N - 1, {}, ""),  # a page that asks the one before, of a machine there: drawn here
    (N, N - 1, {}, centralpages.BEHIND),  # a page that asks more than the machine has
    (N, N + 1, {}, centralpages.AHEAD),  # a machine newer than this panel
    (N, 0, {}, centralpages.BEHIND) if N > 0 else (1, 0, {}, centralpages.BEHIND),  # one that says nothing
    (N, N, {"acme": "1.0"}, centralpages.PLUGINS),  # a plugin this panel has not
    (N, N, {"odoo": "2.0"}, centralpages.PLUGINS),  # one at another version
    (N, N, {"odoo": centralpages.BUILT_IN}, ""),  # a built-in one: the package's, the same agent_api
])
def test_the_rule_of_the_contract(needs, agent_api, theirs, why):
    ours = {"odoo": centralpages.BUILT_IN, "docker": centralpages.BUILT_IN}
    assert centralpages.contract(needs, agent_api, theirs, ours) == why


def test_a_machine_that_says_nothing_is_zero():
    assert centralpages.agent_of({"app": "OmniaCLI", "version": "2026.1.5513"}) == {
        "agent_api": 0, "plugins": {}, "version": "2026.1.5513"}
    assert centralpages.agent_of({"agent_api": True, "plugins": "x"})["agent_api"] == 0
    assert centralpages.agent_of({"agent_api": 1, "plugins": {"odoo": "built-in", "x": 3}})["plugins"] == {
        "odoo": "built-in"}


def test_every_page_is_found_with_its_parameters():
    assert {centralpages.match(path)[0].name for path in PATHS.values()} == set(PATHS) == set(centralpages.BY_NAME) - {
        p.name for p in centralpages.BY_MODEL}
    assert centralpages.match("servers/srv/test/log")[1] == {"server": "srv", "role": "test"}
    assert centralpages.match("servers/srv/test/psql")[1] == {"server": "srv", "role": "test", "kind": "psql"}
    for other in ("servers/srv/x", "servers", "/", "servers/srv/test/log/x", "disks/x", "invite/x"):
        assert centralpages.match(other) is None, other


async def test_the_machine_says_its_contract(central):
    info = (await central.get("/api/info", headers=JSON)).json()
    assert info["agent_api"] == centralpages.AGENT_API == 4
    assert info["plugins"] == centralpages.plugin_versions() and info["plugins"]["odoo"] == centralpages.BUILT_IN
    # Try the link reads it, and keeps it
    tried = (await central.post("/api/links/cliente/test", headers={**JSON, "Origin": "http://central.test"})).json()
    assert tried["ok"] and tried["agent_api"] == N
    assert central.app.state.link_agents["cliente"]["agent_api"] == N


async def test_read_every_ten_minutes_and_again_after_an_update(central, monkeypatch):
    for _ in range(3):
        assert (await central.get("/links/cliente/disks", headers=PAGE)).status_code == 200
    assert central.machine.asked.count("/api/info") == 1
    central.app.state.link_agents["cliente"]["at"] -= centralpages.READ_EVERY + 1
    await central.get("/links/cliente/disks", headers=PAGE)
    assert central.machine.asked.count("/api/info") == 2
    # an update asked from here: read again at the next page, and soon after for a while
    await central.post("/links/cliente/api/machine/omniacli-update", headers={**JSON, "Origin": "http://central.test"},
                       json={"action": "update"})
    await central.get("/links/cliente/disks", headers=PAGE)
    assert central.machine.asked.count("/api/info") == 3
    central.app.state.link_agents["cliente"]["at"] -= centralpages.READ_AFTER_UPDATE + 1
    await central.get("/links/cliente/disks", headers=PAGE)
    assert central.machine.asked.count("/api/info") == 4


# --- the pages drawn here (slice 2) --------------------------------------------------------------------------

async def test_every_page_of_class_a_drawn_here_from_its_model(central):
    """agent_api N: each of the 12 is drawn here from /api/pages/<page>; never the machine's page."""
    for name, path in PATHS.items():
        own = await central.app.state.link_clients.client(central.app.state.settings.link_targets["cliente"]).get(
            f"/{path}", headers={"Authorization": f"Bearer {LINK_TOKEN}", "X-OmniaCLI-Prefix": "/links/cliente",
                                 "X-OmniaCLI-Menu": "servers,instances,backups,settings", "X-OmniaCLI-User": "admin",
                                 "Accept": "text/html"})
        central.machine.asked.clear()
        resp = await central.get(f"/links/cliente/{path}", headers=PAGE)
        # the same answer the machine gives for its own page (a puller connection it has not: 404 both)
        assert resp.status_code == own.status_code, (name, resp.text[:300])
        assert f"/api/pages/{name}" in central.machine.asked and f"/{path}" not in central.machine.asked, name
        if resp.status_code == 200:
            assert "data-machine-page-line" not in resp.text, name
            # the links in it under the link's prefix, the menu this panel's
            assert '"/links/cliente' in resp.text or "/links/cliente/" in resp.text, name


async def test_the_model_carries_no_secret_and_is_for_a_link_only(central, env):
    link = {"Authorization": f"Bearer {LINK_TOKEN}", "X-OmniaCLI-Prefix": "/links/cliente", "X-OmniaCLI-User": "admin",
            "Accept": "application/json"}
    asgi = central.machine.asgi
    async with httpx.AsyncClient(transport=asgi, base_url="http://customer.test") as machine:
        for name in ("disks", "log", "own_log", "instances", "backups", "machines", "discover", "shell", "help",
                     "about", "settings", "server"):
            params = {"log": {"server": "srv", "role": "test"},
                      "shell": {"server": "srv", "role": "test", "kind": "shell"}, "server": {"server": "srv"}}.get(name, {})
            resp = await machine.get(f"/api/pages/{name}", params=params, headers=link)
            assert resp.status_code == 200, (name, resp.text)
            data = resp.json()
            assert data["agent_api"] == centralpages.AGENT_API and data["role"] == "admin" and set(data["context"]) <= set(
                centralpages.BY_NAME[name].fields), name
            # no secret's name among its fields, at any depth, and no secret's value
            assert not [k for k in keys(data["context"]) if any(s in k.lower() for s in SECRETS)], name
            assert LINK_TOKEN not in json.dumps(data), name
        # a person's session or a token: no model
        assert (await machine.get("/api/pages/disks", headers={"Authorization": AUTH})).status_code in (401, 403)
        # the role of the person who looks: a user does not get the developer's pages
        resp = await machine.get("/api/pages/settings", headers={**link, "X-OmniaCLI-Role-Cap": "user"})
        assert resp.status_code == 403
        assert (await machine.get("/api/pages/nope", headers=link)).status_code == 404


async def test_a_text_changed_here_shows_without_the_machine(central, monkeypatch):
    """The panel's template, not the machine's: a text changed in the panel's disks.html shows on the link."""
    from omniacli import app as app_module

    real = app_module._TEMPLATES.TemplateResponse

    def changed(request, name, context=None, **kw):
        resp = real(request, name, context, **kw)
        if name == "disks.html" and request.url.path.startswith("/links/"):
            resp.body = resp.body.replace(b"</h1>", b" (drawn by the panel)</h1>", 1)
            resp.headers["content-length"] = str(len(resp.body))
        return resp

    monkeypatch.setattr(app_module._TEMPLATES, "TemplateResponse", changed)
    page = (await central.get("/links/cliente/disks", headers=PAGE)).text
    assert "(drawn by the panel)" in page


async def test_a_machine_at_zero_writes_its_pages_with_the_line(central):
    """A machine of before (no agent_api): every page is its own, as before; the line that says why only on the
    pages this panel would draw, the others exactly as with pages: machine."""
    central.machine.info = {"version": "2026.1.5513"}
    # (since slice 5 every page of the machine is drawn here, but those of who logs in there: an invitation's)
    for path in ("invite/nothing",):
        here = await central.get(f"/links/cliente/{path}", headers=PAGE)
        link_pages(central, "machine")
        theirs = await central.get(f"/links/cliente/{path}", headers=PAGE)
        link_pages(central, "here")
        assert here.status_code == theirs.status_code and here.text == theirs.text, path
        assert "data-machine-page-line" not in here.text
    for path in ("disks", "help"):
        central.machine.asked.clear()
        resp = await central.get(f"/links/cliente/{path}", headers=PAGE)
        assert resp.status_code == 200 and f"/{path}" in central.machine.asked, path
        assert "/api/pages/" not in " ".join(central.machine.asked)
        assert "data-machine-page-line" in resp.text and "cliente (2026.1.5513): update it" in resp.text, path
    # in Italian
    resp = await central.get("/links/cliente/disks", headers={**PAGE, "Cookie": "omniacli_lang=it"})
    assert "Pagina scritta da cliente (2026.1.5513): aggiornala per vedere quella nuova" in resp.text
    # its data passes as before
    assert (await central.get("/links/cliente/api/disks", headers=JSON)).status_code == 200


async def test_a_newer_machine_and_a_plugin_apart_say_so(central):
    central.machine.info = {"agent_api": N + 1, "plugins": {}}
    assert "the panel is older than the machine" in (await central.get("/links/cliente/disks", headers=PAGE)).text
    central.app.state.link_agents.clear()
    central.machine.info = {"agent_api": N, "plugins": {"acme": "1.0"}}
    text = (await central.get("/links/cliente/disks", headers=PAGE)).text
    assert "its plugins are not those of the panel (acme 1.0)" in text


async def test_pages_machine_brings_back_the_machines_pages(central):
    link_pages(central, "machine")
    resp = await central.get("/links/cliente/disks", headers=PAGE)
    assert resp.status_code == 200 and "/disks" in central.machine.asked
    assert "/api/pages/disks" not in central.machine.asked and "data-machine-page-line" not in resp.text
    # and its HTML passes anywhere, as before
    central.machine.html_at = "/api/disks"
    assert "<script>" in (await central.get("/links/cliente/api/disks", headers=JSON)).text


async def test_a_here_link_refuses_the_machines_html(central):
    """pages: here: from the machine only data passes, but its pages this panel does not draw."""
    central.machine.html_at = "/api/disks"
    resp = await central.get("/links/cliente/api/disks", headers=JSON)
    assert resp.status_code == 502 and "<script>" not in resp.text and "refused" in resp.json()["error"]
    # a page drawn here asked by a script: never the machine's
    central.machine.html_at = "/disks"
    resp = await central.head("/links/cliente/disks", headers=JSON)
    assert resp.status_code == 502
    # a page this panel does not draw yet (AD-017 §9, slices 4-5): the machine's, as before
    resp = await central.get("/links/cliente/users", headers=PAGE)
    assert resp.status_code == 200 and "data-machine-page-line" not in resp.text
    # the panel's own script files, never the machine's
    central.machine.asked.clear()
    assert (await central.get("/links/cliente/static/xterm.js", headers=PAGE)).status_code == 200
    assert "/static/xterm.js" not in central.machine.asked


def as_(user: str) -> dict:
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{PASSWORD}".encode()).decode(),
            "Accept": "text/html"}


async def test_the_drawn_pages_have_the_controls_of_the_machines(central):
    """AD-017 §8.3, parity: for each of the 12 pages, what the person can press (buttons, links, the disabled
    ones, the switches of its scripts: can_act, is_admin, account…) is the same on the page drawn here and on the
    machine's own, for admin, developer and user (a central's person has no custom role: a person of a workspace
    with one does not open a link, either way)."""
    paths = {**PATHS, "connection": "pullers/connections/api-prod"}
    for user in ("admin", "dev", "viewer"):
        for name, path in paths.items():
            here = await central.get(f"/links/cliente/{path}", headers=as_(user))
            link_pages(central, "machine")
            theirs = await central.get(f"/links/cliente/{path}", headers=as_(user))
            link_pages(central, "here")
            assert here.status_code == theirs.status_code, (user, name)
            if here.status_code == 200:
                assert main_of(here.text) == main_of(theirs.text), (user, name)
    # the commands of a puller's connection: off for a person through a link, as there
    page = (await central.get("/links/cliente/pullers/connections/api-prod", headers=as_("admin"))).text
    assert "account = null;" in page and "isAdmin = true" in page


async def test_a_machine_whose_contract_goes_back_is_not_shown(central):
    """Once a machine said agent_api 1, it saying 0 again (or having no model) does not show its own HTML on the
    pages drawn here: this panel's page says so, until its pages: machine; the highest kept on disk."""
    assert (await central.get("/links/cliente/disks", headers=PAGE)).status_code == 200
    seen_file = central.app.state.settings.links_file.parent / centralpages.SEEN_FILE
    assert json.loads(seen_file.read_text()) == {"cliente": N} and oct(seen_file.stat().st_mode)[-3:] == "600"
    central.machine.info = {"version": "2026.1.5513"}
    central.app.state.link_agents["cliente"]["at"] -= centralpages.READ_EVERY + 1
    central.machine.asked.clear()
    resp = await central.get("/links/cliente/disks", headers=PAGE)
    assert resp.status_code == 502 and "Page of cliente not shown" in resp.text and "now it says 0" in resp.text
    assert "/disks" not in central.machine.asked and "data-machine-page-line" not in resp.text
    # a page not drawn here (an invitation's): the machine's, as before
    central.machine.asked.clear()
    await central.get("/links/cliente/invite/nothing", headers=PAGE)
    assert "/invite/nothing" in central.machine.asked
    # what was seen stays seen in a panel started again
    assert centralpages.load_seen(seen_file) == {"cliente": N}
    # the admin lets it write its own again
    link_pages(central, "machine")
    resp = await central.get("/links/cliente/disks", headers=PAGE)
    assert resp.status_code == 200 and "/disks" in central.machine.asked


async def test_a_machine_without_models_or_answer_is_remembered(central):
    """It says 1 but has no /api/pages: this panel's page, and not asked again at every page; one that does not
    answer /api/info: not asked again for a minute, and never its HTML once it said 1."""
    assert (await central.get("/links/cliente/disks", headers=PAGE)).status_code == 200
    central.app.state.link_agents["cliente"]["at"] -= centralpages.READ_EVERY + 1
    central.machine.answer["/api/pages/help"] = (404, {"content-type": "application/json"}, b'{"detail": "Not Found"}')
    central.machine.asked.clear()
    for _ in range(3):
        resp = await central.get("/links/cliente/help", headers=PAGE)
        assert resp.status_code == 502 and "does not give them" in resp.text
    assert central.machine.asked.count("/api/pages/help") == 1 and central.machine.asked.count("/api/info") == 1
    assert "/help" not in central.machine.asked
    # /api/info answers 503: a minute without asking, and its page not shown
    central.machine.answer["/api/info"] = (503, {"content-type": "text/plain"}, b"down")
    central.app.state.link_agents["cliente"]["at"] -= centralpages.READ_EVERY + 1
    central.machine.asked.clear()
    for _ in range(3):
        resp = await central.get("/links/cliente/disks", headers=PAGE)
        assert resp.status_code == 502 and "/disks" not in central.machine.asked
    assert central.machine.asked.count("/api/info") == 1


async def test_a_placed_servers_log_and_shell_go_to_its_host(central, monkeypatch):
    """AD-014: the model of the log or shell of a server placed elsewhere sends the page to the host through the
    home, as the home's page does (PlacedRoutes 307)."""
    moved = "/links/cliente/placed/srv/servers/srv/test/log"
    central.machine.answer["/api/pages/log"] = (200, {"content-type": "application/json"}, json.dumps({
        "agent_api": 1, "page": "log", "role": "admin", "development": True, "moved": moved}).encode())
    resp = await central.get("/links/cliente/servers/srv/test/log", headers=PAGE, follow_redirects=False)
    assert resp.status_code == 307 and resp.headers["location"] == moved
    # elsewhere than under the link: not followed
    central.machine.answer["/api/pages/log"] = (200, {"content-type": "application/json"}, json.dumps({
        "agent_api": 1, "role": "admin", "moved": "https://evil.test/x", "context": {}}).encode())
    resp = await central.get("/links/cliente/servers/srv/test/log", headers=PAGE, follow_redirects=False)
    assert resp.status_code != 307


async def test_the_api_of_a_here_link_answers_json_and_data_without_body(central):
    """A browser's /api request through a here-link is asked as JSON (the machine's reason, not its error page);
    an answer without a body and without a length is data."""
    resp = await central.get("/links/cliente/api/servers/nope/test/log", headers=PAGE)
    assert central.machine.accepts[-1] == "application/json"
    assert resp.status_code == 404 and "nope" in resp.text and "refused" not in resp.text
    central.machine.answer["/api/empty"] = (204, {}, b"")
    assert (await central.get("/links/cliente/api/empty", headers=JSON)).status_code == 204
    central.machine.answer["/api/empty"] = (200, {}, b"")
    assert (await central.get("/links/cliente/api/empty", headers=JSON)).status_code == 200
    central.machine.answer["/api/empty"] = (200, {}, b"<script>x</script>")
    assert (await central.get("/links/cliente/api/empty", headers=JSON)).status_code == 502


async def test_no_line_in_an_encoded_body_and_the_way_back_of_a_404(central):
    central.machine.info = {"version": "2026.1.5513"}
    central.machine.answer["/disks"] = (200, {"content-type": "text/html", "content-encoding": "gzip"},
                                       gzip.compress(b"<html><body>its own</body></html>"))
    resp = await central.get("/links/cliente/disks", headers={**PAGE, "Accept-Encoding": "gzip"})
    assert resp.text == "<html><body>its own</body></html>"
    # a page drawn here that is not there: this panel's error page, its way back under the link
    central.machine.info = None
    central.app.state.link_agents.clear()
    resp = await central.get("/links/cliente/servers/nope/test/log", headers=PAGE)
    assert resp.status_code == 404 and 'href="/links/cliente/instances"' in resp.text


async def test_the_switch_on_the_links_settings(central):
    post = {**JSON, "Origin": "http://central.test"}
    page = (await central.get("/links", headers=PAGE)).text
    assert 'id="ld-pages"' in page and 'data-pages="here"' in page
    resp = await central.post("/api/links/save", headers=post, json={
        "name": "cliente", "url": "http://customer.test", "token": "", "pages": "machine"})
    assert resp.status_code == 200
    saved = yaml.safe_load(central.app.state.settings.links_file.read_text())["links"]["cliente"]
    assert saved["pages"] == "machine" and saved["token"] == LINK_TOKEN
    # a save that does not say it keeps it; a rename too
    await central.post("/api/links/save", headers=post, json={"name": "cliente", "url": "http://customer.test"})
    assert central.app.state.settings.link_targets["cliente"].pages == "machine"
    resp = await central.post("/api/links/save", headers=post, json={
        "name": "cliente", "url": "http://customer.test", "pages": "elsewhere"})
    assert resp.status_code == 400
    resp = await central.post("/api/links/save", headers=post, json={
        "name": "cliente", "url": "http://customer.test", "pages": "here"})
    saved = yaml.safe_load(central.app.state.settings.links_file.read_text())["links"]["cliente"]
    assert "pages" not in saved
    # a save reads its machine's contract again; a delete forgets what it said
    assert "cliente" not in central.app.state.link_agents
    await central.get("/links/cliente/disks", headers=PAGE)
    assert central.app.state.link_agents_seen == {"cliente": N}
    await central.post("/api/links/delete", headers=post, json={"name": "cliente"})
    assert central.app.state.link_agents_seen == {} and "cliente" not in central.app.state.link_agents


async def test_the_model_of_a_placed_server_sends_to_its_host(pair):
    """The home's model of the log and the shell of a server placed on another machine (AD-014): the address its
    own page sends a browser to (PlacedRoutes), under the panel's prefix."""
    pair.place(copy_here=False)
    pair.app.state.settings.servers.pop("srv")
    link = {"Authorization": f"Bearer {LINK_TOKEN}", "X-OmniaCLI-Prefix": "/links/home", "X-OmniaCLI-User": "admin",
            "Accept": "application/json"}
    for page, params, path in (("log", {"server": "srv", "role": "production"}, "servers/srv/production/log"),
                               ("shell", {"server": "srv", "role": "production", "kind": "terminal"},
                                "servers/srv/production/terminal")):
        data = (await pair.get(f"/api/pages/{page}", params=params, headers=link)).json()
        own = await pair.get(f"/{path}", headers={**link, "Accept": "text/html"}, follow_redirects=False)
        assert own.status_code == 307 and data["moved"] == own.headers["location"] == f"/links/home/placed/srv/{path}"


# --- the instance's page (slice 3) ---------------------------------------------------------------------------

async def test_the_instance_page_is_drawn_here_from_its_model(central):
    """agent_api 2: /links/<m>/servers/<x> is this panel's server.html from /api/pages/server, its test at
    ?view=test too; the Download of a backup's row is this panel's template's, whatever the machine's has."""
    for query in ("", "?view=test"):
        central.machine.asked.clear()
        resp = await central.get(f"/links/cliente/servers/srv{query}", headers=PAGE)
        assert resp.status_code == 200 and "data-machine-page-line" not in resp.text
        assert "/api/pages/server" in central.machine.asked and "/servers/srv" not in central.machine.asked
        # the Download of a backup's row (the case of AD-017 §0) is drawn by this panel's script
        assert "f.download ?" in resp.text and '"/links/cliente' in resp.text
    # an instance that is not there: this panel's error page, as the machine answers it
    resp = await central.get("/links/cliente/servers/nope", headers=PAGE)
    assert resp.status_code == 404 and 'href="/links/cliente/instances"' in resp.text


async def test_the_instance_model_has_no_secret_value(central, env):
    """The database's password of the instance's configuration, a git credential of its repositories, the link's
    token: none of their values is in the model (its psql is said as a yes, never its command and environment)."""
    (env["tmp"] / "odoo.conf").write_text("[options]\nadmin_passwd = AdminPw-913\ndb_password = DbPw-913\n"
                                          "db_user = odoo\n")
    repo = env["settings"].servers["srv"].production.repositories[0]
    repo.git_options = ("-c", "http.extraHeader=Authorization: Bearer GitTok-913")
    link = {"Authorization": f"Bearer {LINK_TOKEN}", "X-OmniaCLI-Prefix": "/links/cliente", "X-OmniaCLI-User": "admin",
            "Accept": "application/json"}
    async with httpx.AsyncClient(transport=central.machine.asgi, base_url="http://customer.test") as machine:
        for view in ("production", "test"):
            text = (await machine.get("/api/pages/server", params={"server": "srv", "view": view}, headers=link)).text
            data = json.loads(text)
            assert set(data["context"]) == set(centralpages.SERVER_FIELDS)
            for secret in ("AdminPw-913", "DbPw-913", "GitTok-913", LINK_TOKEN, "PGPASSWORD"):
                assert secret not in text, (view, secret)
            assert not [k for k in keys(data["context"]) if any(s in k.lower() for s in SECRETS)], view
            inst = data["context"]["instances"][0]
            assert inst["plugin"]["db_shell"] is True and inst["plugin"]["describe"] == {"database": f"DB_{view}"}


async def test_the_instance_page_has_the_controls_of_the_machines_for_every_role(central, env, monkeypatch):
    """AD-017 §8.3 for the instance's page: the whole <main> drawn here equals the machine's own, for admin,
    developer and user through the link; and on the machine every page it draws equals the one drawn from its
    model (serverparity.Twice): what the person can press, the disabled buttons and why, the scripts' switches."""
    twice = Twice(monkeypatch)
    # this panel's tunnels reach the browser: the machine offers Log in as through them, on both pages
    settings = central.app.state.settings
    monkeypatch.setattr(settings, "options", dataclasses.replace(settings.options, tunnel_bind="0.0.0.0"))
    for inst in env["settings"].servers["srv"].instances.values():
        monkeypatch.setattr(inst, "domain", "http://localhost:8069")
    for user in ("admin", "dev", "viewer"):
        for query in ("", "?view=test"):
            here = await central.get(f"/links/cliente/servers/srv{query}", headers=as_(user))
            link_pages(central, "machine")
            theirs = await central.get(f"/links/cliente/servers/srv{query}", headers=as_(user))
            link_pages(central, "here")
            assert here.status_code == theirs.status_code == 200, (user, query)
            assert main_of(here.text) == main_of(theirs.text), (user, query)
            # Log in as through this panel's tunnel: offered to who may (the developer's role), on both
            assert ("data-login-as=" in here.text) == (user != "viewer"), (user, query)
    # the machine drew each page from the link, with the role of who looks (the link's, never more)
    assert len(twice.compared) == 6 and {c["role"] for c in twice.compared} == {"admin", "developer", "user"}


async def test_the_parity_of_the_custom_roles_and_of_the_delegated_scope(pair, monkeypatch):
    """The people of a workspace (user, developer, admin, a copy of admin and a custom role without the logs) on
    the instance's page of its home, and through the home on its host once it is placed there (AD-014's delegated
    scope): the page drawn from the model equals the page drawn from the live objects, every time."""
    twice = Twice(monkeypatch)
    people = ("as-user", "as-developer", "as-admin", "copy-admin", "no-logs")
    for person in people:
        resp = await pair.get("/servers/srv", headers=as_user(person))
        assert resp.status_code == 200, (person, resp.text[:200])
    pair.place()
    for person in people:
        resp = await pair.get("/servers/srv", headers=as_user(person), follow_redirects=True)
        assert resp.status_code == 200, (person, resp.text[:200])
    here = [c for c in twice.compared if not c["via"]]
    there = [c for c in twice.compared if c["via"]]
    assert {c["user"] for c in here} == {c["user"] for c in there} == set(people)
    assert all(c["scoped"] for c in twice.compared)
    # the custom role without the logs is not drawn as the copy of admin: its log's controls are off, on both
    without = {c["via"]: c for c in twice.compared if c["user"] == "no-logs"}
    full = {c["via"]: c for c in twice.compared if c["user"] == "copy-admin"}
    assert all(without[k]["main"] != full[k]["main"] and without[k]["disabled"] > full[k]["disabled"] for k in full)


async def test_a_machine_one_behind_writes_its_newer_pages_with_the_line(central):
    """A machine of slice 4 (agent_api N-1 = 3): the pages of class A, the instance's page and the pages of class B
    drawn here, the other pages of class C its own with the line that says to update it; once it said N, it saying
    N-1 again is this panel's page that says so for those, and the others are still drawn."""
    central.machine.info = {"agent_api": N - 1, "plugins": {}, "version": "2026.1.5518"}
    for path in ("", "pullers", "users", "options", "config/new"):
        central.machine.asked.clear()
        resp = await central.get(f"/links/cliente/{path}", headers=PAGE)
        assert resp.status_code == 200 and f"/{path}" in central.machine.asked, path
        assert not [a for a in central.machine.asked if a.startswith("/api/pages/")], path
        assert "data-machine-page-line" in resp.text and "cliente (2026.1.5518): update it" in resp.text, path
    for path, name in (("disks", "disks"), ("servers/srv", "server"), ("jobs", "jobs"), ("config", "config")):
        central.machine.asked.clear()
        resp = await central.get(f"/links/cliente/{path}", headers=PAGE)
        assert resp.status_code == 200 and "data-machine-page-line" not in resp.text, path
        assert f"/api/pages/{name}" in central.machine.asked and f"/{path}" not in central.machine.asked, path
    assert central.app.state.link_agents_seen == {"cliente": N - 1}
    # it said N, then N-1 again: its pages of slice 5 are not shown by its word; the others are still drawn
    central.machine.info = None
    central.app.state.link_agents.clear()
    assert (await central.get("/links/cliente/disks", headers=PAGE)).status_code == 200
    assert central.app.state.link_agents_seen == {"cliente": N}
    central.machine.info = {"agent_api": N - 1, "plugins": {}, "version": "2026.1.5518"}
    central.app.state.link_agents.clear()
    central.machine.asked.clear()
    resp = await central.get("/links/cliente/pullers", headers=PAGE)
    assert resp.status_code == 502 and "Page of cliente not shown" in resp.text
    assert "/pullers" not in central.machine.asked
    for path in ("disks", "servers/srv", "jobs"):
        assert (await central.get(f"/links/cliente/{path}", headers=PAGE)).status_code == 200


async def test_a_machine_two_behind_writes_every_page_with_the_line(central):
    """AD-017 §10.6: the panel reads N and N-1; a machine of slices 1-2 (agent_api 1) has every page its own, with
    the line, as long as it never said more."""
    central.machine.info = {"agent_api": N - 2, "plugins": {}, "version": "2026.1.5517"}
    for path in ("disks", "servers/srv", "jobs", "pullers", ""):
        central.machine.asked.clear()
        resp = await central.get(f"/links/cliente/{path}", headers=PAGE)
        assert resp.status_code == 200 and f"/{path}" in central.machine.asked, path
        assert "data-machine-page-line" in resp.text and "cliente (2026.1.5517): update it" in resp.text, path


async def test_a_plugin_only_on_the_machine_brings_back_its_instance_page(central):
    central.machine.info = {"agent_api": centralpages.AGENT_API, "plugins": {"acme": "1.0"}, "version": "2026.1.5520"}
    central.machine.asked.clear()
    resp = await central.get("/links/cliente/servers/srv", headers=PAGE)
    assert resp.status_code == 200 and "/servers/srv" in central.machine.asked
    assert "/api/pages/server" not in central.machine.asked
    assert "its plugins are not those of the panel (acme 1.0)" in resp.text


async def test_a_dialog_this_panel_has_not_is_refused(central):
    """A model that names a dialog of a plugin this panel has not, or a path: refused, never included."""
    link = central.app.state.settings.link_targets["cliente"]
    own = (await central.app.state.link_clients.client(link).get(
        "/api/pages/server", params={"server": "srv"},
        headers={"Authorization": f"Bearer {LINK_TOKEN}", "X-OmniaCLI-User": "admin"})).json()
    for dialog in ("acme/dialog.html", "odoo/../../base.html", "odoo/update_dialog.html/x"):
        own["context"]["dialogs"] = [dialog]
        central.machine.answer["/api/pages/server"] = (200, {"content-type": "application/json"},
                                                      json.dumps(own).encode())
        resp = await central.get("/links/cliente/servers/srv", headers=PAGE)
        assert resp.status_code == 502 and "is not the one this panel reads" in resp.text, dialog


async def test_every_read_of_a_model_is_in_the_machines_access_log(central):
    """AD-017 §17.5 decision 5: each read of a page's model, with who looks through the link (on_behalf) and the
    server, every time (not once in ten minutes as a page)."""
    for _ in range(2):
        assert (await central.get("/links/cliente/servers/srv", headers=as_("dev"))).status_code == 200
    reads = [e for e in central.machine.asgi.app.state.access_log.recent(100)
             if e.get("event") == "read" and e.get("what") == "model of the page server"]
    assert len(reads) == 2
    assert all(e["kind"] == "link" and e["name"] == "omnia" and e["on_behalf"] == "dev" and e["server"] == "srv"
               and e["status"] == 200 and "for dev" in e["detail"] for e in reads), reads


async def test_the_machines_word_never_lets_its_html_through(central):
    """A model that says the page is the machine's own (own_page, as 549606a took it) does not bring the machine's
    HTML: its /servers/<x> is never asked, its script never reaches the browser."""
    central.machine.answer["/api/pages/server"] = (200, {"content-type": "application/json"}, json.dumps({
        "agent_api": centralpages.AGENT_API, "page": "server", "role": "admin", "own_page": True}).encode())
    central.machine.answer["/servers/srv"] = (200, {"content-type": "text/html"},
                                             b"<html><body><script>alert(1)</script></body></html>")
    central.machine.asked.clear()
    resp = await central.get("/links/cliente/servers/srv", headers=PAGE)
    assert resp.status_code == 502 and "<script>alert(1)" not in resp.text
    assert "/servers/srv" not in central.machine.asked


async def test_the_page_of_a_move_is_drawn_here_from_its_model(central):
    """An instance placed elsewhere whose copy there was deleted: the page of its move (moved_away.html) drawn
    here from its small model, never the machine's HTML; of another instance than the path's, refused."""
    move = {"state": {"state": "placed", "link": "far", "copy": "deleted", "since": "2026-11-24T10:00:00"},
            "can": False, "here": "cliente", "busy": False, "stop_seconds": 40, "until": "2026-12-08"}
    for name, code in (("srv", 200), ("other", 502)):
        central.machine.answer["/api/pages/server"] = (200, {"content-type": "application/json"}, json.dumps({
            "agent_api": centralpages.AGENT_API, "page": "server", "role": "admin",
            "moved_away": {"server": {"name": name}, "move": move}}).encode())
        central.machine.answer["/servers/srv"] = (200, {"content-type": "text/html"}, b"<script>alert(1)</script>")
        central.machine.asked.clear()
        resp = await central.get("/links/cliente/servers/srv", headers=PAGE)
        assert resp.status_code == code and "<script>alert(1)" not in resp.text, name
        assert "/servers/srv" not in central.machine.asked
        if code == 200:
            assert "<h1>srv</h1>" in resp.text and 'data-move' in resp.text or "far" in resp.text


async def test_the_instance_model_of_a_placed_server_is_its_hosts(pair):
    """AD-014: the model of the page of an instance placed elsewhere answers what the home's own page answers:
    sent to the host under the panel's prefix (its view kept), or the page of its move (the machine's own)."""
    link = {"Authorization": f"Bearer {LINK_TOKEN}", "X-OmniaCLI-Prefix": "/links/home", "X-OmniaCLI-User": "admin",
            "Accept": "application/json"}
    pair.place(copy_here=False)
    data = (await pair.get("/api/pages/server", params={"server": "srv", "view": "production"}, headers=link)).json()
    own = await pair.get("/servers/srv?view=production", headers={**link, "Accept": "text/html"}, follow_redirects=False)
    assert own.status_code == 307
    assert data["moved"] == own.headers["location"] == "/links/home/placed/srv/servers/srv?view=production"
    pair.app.state.settings.servers.pop("srv")
    data = (await pair.get("/api/pages/server", params={"server": "srv"}, headers=link)).json()
    own = await pair.get("/servers/srv", headers={**link, "Accept": "text/html"}, follow_redirects=False)
    assert own.status_code == 307 and data["moved"] == own.headers["location"] == "/links/home/placed/srv/servers/srv"
    # a link that is an admin of the machine keeps the page of the move here: its small model, drawn the same
    settings = pair.app.state.settings
    settings.fixed.links["omnia"] = LinkAccess(hash_token(LINK_TOKEN), "admin")
    settings.set_web_access(settings.web)
    model = await pair.get("/api/pages/server", params={"server": "srv"}, headers=link)
    own = await pair.get("/servers/srv", headers={**link, "Accept": "text/html"}, follow_redirects=False)
    # (srv is not named as a workspace's instance here: no page of its move, on both)
    assert model.status_code == own.status_code == 404 and "own_page" not in model.text


# --- the review of 549606a -----------------------------------------------------------------------------------

async def own_model(central, **params) -> dict:
    link = central.app.state.settings.link_targets["cliente"]
    return (await central.app.state.link_clients.client(link).get(
        "/api/pages/server", params={"server": "srv", **params},
        headers={"Authorization": f"Bearer {LINK_TOKEN}", "X-OmniaCLI-User": "admin",
                 "X-OmniaCLI-Prefix": "/links/cliente"})).json()


def answer_model(central, data: dict) -> None:
    central.machine.answer["/api/pages/server"] = (200, {"content-type": "application/json"}, json.dumps(data).encode())


@pytest.mark.parametrize("where, address", [
    ("web_url", "javascript:alert(1)"),
    ("web_url", " javascript:alert(1)"),
    ("open", "data:text/html,<script>alert(1)</script>"),
    ("page", "javascript:alert(1)"),
    ("page", "/api/links/save"),
    ("button", "/api/links/delete"),
    ("button", "/links/other/servers/srv/production/x/odoo/update"),
    ("button", "/links/cliente/../../api/links/delete"),
    ("button", "/links/cliente/%2e%2e/%2e%2e/api/links/delete"),
    ("button", "//evil.test/x"),
    ("button", "https://evil.test/x"),
    ("login_as", "/links/cliente/..\\..\\api/x"),
])
async def test_an_address_the_page_may_not_use_refuses_the_model(central, where, address):
    """AD-017 review, finding 2: a link to elsewhere is http(s) only (the Odoo of an instance, its repository); an
    address of the machine's own is under /links/<m>/ once its path is read (no .., encoded or not, no other
    machine, no route of this panel): any other refuses the whole model, this panel's error page."""
    data = await own_model(central)
    context = data["context"]
    if where == "web_url":
        context["instances"][0]["web_url"] = address
    elif where in ("open", "page"):
        context["environments"][0][where] = address
    elif where == "button":
        group = next(g for g in context["buttons"]["srv/production"].values() if g)
        group[0]["url"] = address
    else:
        context["login_as"]["srv/production"]["url"] = address
    answer_model(central, data)
    resp = await central.get("/links/cliente/servers/srv", headers=PAGE)
    assert resp.status_code == 502 and "is not the one this panel reads" in resp.text, (where, address)
    assert address not in resp.text


def test_the_addresses_of_a_model():
    assert centralpages.under("/links/m", "/links/m/servers/x?view=test#log")
    assert centralpages.under("/links/m", "/links/m")
    for bad in ("/links/mm/x", "/links/other/x", "/api/x", "/links/m/../x", "/links/m/%2E%2E/x", "//x/y",
                "links/m/x", "javascript:x", "/links/m/a\\b", "/links/m/a b"):
        assert not centralpages.under("/links/m", bad), bad
    assert centralpages.elsewhere("https://acme.example.com/odoo") and centralpages.elsewhere("http://localhost:8069")
    for bad in ("javascript:alert(1)", "data:x", "https:/x", "ftp://x/y", "https://a b"):
        assert not centralpages.elsewhere(bad), bad
    # where a package is published is a text of the page, not an address it opens
    centralpages.checked_addresses({"server": {"test": {"package": {"url": "https://download.example.com/x.deb"}}}},
                                   "/links/m")


async def test_the_models_are_not_reached_through_the_link(central):
    """AD-017 review, finding 3: /links/<m>/api/pages/* is this panel's to read, never a browser's."""
    central.machine.asked.clear()
    for path in ("api/pages/server?server=srv", "api/pages/settings", "api/pages"):
        resp = await central.get(f"/links/cliente/{path}", headers=JSON)
        assert resp.status_code == 404, path
    assert not [p for p in central.machine.asked if p.startswith("/api/pages")]


async def test_a_model_of_another_instance_is_refused(central):
    """AD-017 review, finding 4: the model's instance is the path's."""
    data = await own_model(central)
    data["context"]["server"]["name"] = "other"
    answer_model(central, data)
    resp = await central.get("/links/cliente/servers/srv", headers=PAGE)
    assert resp.status_code == 502 and "is not the one this panel reads" in resp.text


def test_names_in_the_scripts_are_written_as_json():
    """AD-017 review, finding 4: a name a script takes is JSON (tojson), never put between quotes by hand."""
    from omniacli import app as app_module

    for name in ("server.html", "_module_upload.html", "_environments.html", "_move.html", "_deploys.html",
                 "_modules_tab.html", "_backup_dialog.html", "_restore_dialog.html", "_from_link_dialog.html"):
        source = app_module._TEMPLATES.env.loader.get_source(app_module._TEMPLATES.env, name)[0]
        for script in re.findall(r"<script\b[^>]*>(.*?)</script>", source, re.S):
            quoted = re.findall(r"""["'`]\{\{(?![^}]*tojson)[^}]*\}\}["'`]""", script)
            assert not [q for q in quoted if "prefix" not in q], (name, quoted)


async def test_the_line_only_on_a_page_of_200(central):
    """AD-017 review, finding 5: a machine at 0 has its pages with the line; its error pages and anything but a
    page of 200 pass as it wrote them (streamed, untouched)."""
    central.machine.info = {"version": "2026.1.5513"}
    resp = await central.get("/links/cliente/servers/srv", headers=PAGE)
    assert resp.status_code == 200 and "data-machine-page-line" in resp.text
    central.machine.answer["/servers/srv"] = (404, {"content-type": "text/html"}, b"<html><body>not here</body></html>")
    resp = await central.get("/links/cliente/servers/srv", headers=PAGE)
    assert resp.status_code == 404 and resp.text == "<html><body>not here</body></html>"


async def test_the_moments_by_the_machines_clock(central):
    """AD-017 review, finding 6: the page drawn here says the machine's zone (window.serverZone) and writes its
    moments by it, not by this panel's."""
    data = await own_model(central)
    data["server_zone"] = "Asia/Tokyo"
    answer_model(central, data)
    resp = await central.get("/links/cliente/servers/srv", headers=PAGE)
    assert resp.status_code == 200 and 'window.serverZone = "Asia/Tokyo";' in resp.text
    data["server_zone"] = "</script><script>alert(1)//"
    answer_model(central, data)
    resp = await central.get("/links/cliente/servers/srv", headers=PAGE)
    assert resp.status_code == 200 and "alert(1)" not in resp.text


@pytest.mark.parametrize("moved", ["/links/cliente/../../api/links/delete", "/links/cliente/%2e%2e/x",
                                   "/links/other/placed/srv/servers/srv", "https://evil.test/links/cliente/x"])
async def test_a_moved_page_is_followed_under_the_link_only(central, moved):
    """AD-017 review, finding 7: «moved» is followed when, its path read, it is under the link."""
    answer_model(central, {"agent_api": centralpages.AGENT_API, "page": "server", "role": "admin", "moved": moved})
    resp = await central.get("/links/cliente/servers/srv", headers=PAGE, follow_redirects=False)
    assert resp.status_code != 307, moved


# --- the pages of class B (slice 4) --------------------------------------------------------------------------

# a page of each of the 14 of class B, as the browser opens it under /links/<name>/ ({job}: a job of the machine),
# in the order AD-018 §18.8 puts them: the pages a customer sees first
B_PATHS = {"account": "account", "jobs": "jobs", "job": "jobs/{job}", "workspaces": "workspaces",
           "destinations": "destinations", "access": "access", "config": "config", "credentials": "credentials",
           "enterprise": "enterprise", "links": "links", "tunnels": "tunnels", "groups": "groups",
           "plugins": "plugins", "schedules": "schedules"}
# their other forms: a workspace's destinations and access log, the filters, a workspace's form, a job not there
B_MORE = ("workspaces/acme/destinations", "workspaces/acme/audit", "jobs?server=srv&q=pull", "access?event=read",
          "workspaces?new=", "workspaces?edit=acme", "jobs/nope", "workspaces/nope/audit", "jobs/{own}",
          "jobs?server=_machine")
# a destination's key (the text form of pullcrypto)
DEST_KEY = pullcrypto.new_key()
# the secrets the machine keeps that its pages of class B are about: none of their values is ever in a model
B_SECRETS = ("FarTok-914", "GitTok-914", "EntTok-914", "DestPass-914", DEST_KEY, "SmtpPw-914", "PRIVATE-KEY-914",
             "WsDestPass-914", "DavPass-914", "Hook914")
LINK = {"Authorization": f"Bearer {LINK_TOKEN}", "X-OmniaCLI-Prefix": "/links/cliente", "X-OmniaCLI-User": "admin",
        "X-OmniaCLI-Menu": "servers,instances,backups,settings",
        "Accept": "application/json"}


async def class_b(central, monkeypatch) -> dict:
    """The machine with what the pages of class B show, and the secrets they are about: a job, a workspace (acme,
    with srv), a link of its own over SSH (its token), its SSH key (the private half), a git credential, the source
    of Odoo Enterprise, three destinations (a password and the key of each, a WebDAV one with its url), the SMTP
    password, a schedule, groups, a server whose plugin is missing; the customer's own names that are also words of
    the pages' addresses (a storage root, a token, users, subdomains called backups, upload, preview, open, install);
    {job: the job's id}."""
    machine = central.machine.asgi.app
    settings = machine.state.settings
    acme = parse_workspaces({"acme": {"label": "ACME", "servers": ["srv"],
                                      "members": {"backups": "admin", "upload": "user"}}})["acme"]
    settings.workspaces = {"acme": dataclasses.replace(
        acme, subdomains={"preview": "srv", "open": "srv", "install": "srv"}, token_people={"backups": "backups",
                                                                                          "users": "upload"},
        notify=["https://hooks.example.com/services/T0/B0/Hook914"])}
    web = settings.web
    settings.set_web_access(dataclasses.replace(web, users={**web.users, "backups": USER_HASH, "upload": USER_HASH},
                                                roles={**web.roles, "backups": "user", "upload": "developer"}))
    settings.storage_roots = {"default": "/srv/omniacli/default", "backups": "/srv/omniacli/backups"}
    settings.groups = {"servers": [ListGroup("Customers", ["srv"])], "links": [ListGroup("upload", ["far"])]}
    settings.servers = {**settings.servers, "old": Server(name="old", instances={}, status="draft", type="acme",
                                                          error="no plugin acme here", missing_plugin=True)}
    settings.link_targets = {"far": LinkTarget("far", "http://127.0.0.1:8090", "FarTok-914", ssh=SshRoute(
        "far.example.com", "omniacli", "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGhvc3Qta2V5LTkxNC1vZi1mYXItZXhhbXBsZS1jb20h"))}
    folder = machine.state.ssh_tunnels.folder
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "id_ed25519").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nPRIVATE-KEY-914\n")
    (folder / "id_ed25519.pub").write_text("ssh-ed25519 AAAApub914 omniacli\n")
    gitcredentials.save(settings.credentials_file, {
        "server:srv": {"github.com": gitcredentials.check("github.com", "bot", "GitTok-914")}})
    enterprise.save(enterprise.file_of(settings.servers_file),
                    enterprise.check("https://github.com/odoo/enterprise.git", "bot", "EntTok-914"))
    destinations.save_destinations(settings.destinations_file, {
        "off": destinations.Destination("off", "sftp", "/b", settings={"host": "h", "user": "u", "pass": "DestPass-914"},
                                        key=DEST_KEY),
        "ws-off": destinations.Destination("ws-off", "sftp", "/w", workspace="acme",
                                           settings={"host": "h", "user": "u", "pass": "WsDestPass-914"}, key=DEST_KEY),
        "dav": destinations.Destination("dav", "webdav", "odoo", workspace="acme", key=DEST_KEY, settings={
            "url": "https://cloud.example.com/remote.php/dav/files/u", "vendor": "nextcloud", "user": "u",
            "pass": "DavPass-914"})})
    saved = await httpx.AsyncClient(transport=central.machine.asgi, base_url="http://customer.test").post(
        "/api/schedules/save", headers=LINK, json={"original": "", "name": "nightly", "schedule": {
            "instance": "srv/test", "cron": "0 2 * * *", "pull": True, "enabled": False,
            "notify": ["https://hooks.example.com/services/T0/B0/Hook914"],
            "copy": {"link": "far", "server": "srv", "role": "production",
                     "restore_options": {"reset_passwords": True}}}})
    assert saved.status_code == 200, saved.text
    monkeypatch.setattr(settings, "options", dataclasses.replace(settings.options, smtp_host="smtp.example.com",
                                                                 smtp_password="SmtpPw-914"))

    async def pulled(job):
        job.log("pulled")

    # a job of the machine's own (its key _machine/<x>: found on lab4, a name of the page that is not a server's)
    own = machine.state.jobs.submit("Names published through this machine", ["_machine/relay"], "manual: admin", pulled)
    job = machine.state.jobs.submit("Git pull srv/test", ["srv/test"], "manual: admin", pulled)
    for _ in range(100):
        if job.finished and own.finished:
            break
        await asyncio.sleep(0.01)
    return {"job": job.id, "own": own.id}


def b_paths(ids: dict) -> list[str]:
    return [p.format(**ids) for p in (*B_PATHS.values(), *B_MORE)]


def test_the_pages_of_class_b_are_found_with_their_parameters():
    assert {centralpages.match(p.format(job="20261124-101500-abc123"))[0].name for p in B_PATHS.values()} == set(B_PATHS)
    assert {p.name for p in centralpages.CLASS_B} == set(B_PATHS)
    assert all(p.needs_agent_api == 3 for p in centralpages.CLASS_B)
    assert centralpages.match("jobs/20261124-1")[1] == {"job_id": "20261124-1"}
    assert centralpages.match("workspaces/acme/destinations")[1] == {"workspace": "acme"}
    assert centralpages.match("destinations")[1] == {}
    assert centralpages.match("workspaces/acme/audit")[0].name == "access"
    for other in ("jobs/1/stop", "workspaces/acme/users", "access/x", "config/srv/delete"):
        assert centralpages.match(other) is None, other
    # the names a page puts in an address: a server's, a job's, the machine's own (_machine), an instance's key
    for name in ("srv", "20261124-101500-abc123", "_machine", "srv/production", "acme.example"):
        assert centralpages.a_name(name), name
    for name in ("..", ".", "../x", "srv/../x", "a/b/c", "a b", "%2e%2e", "", "/srv", "srv/", "a\\b"):
        assert not centralpages.a_name(name), name


async def test_every_page_of_class_b_drawn_here_from_its_model(central, monkeypatch):
    """agent_api 3: each of the 14 is drawn here from /api/pages/<page>, with the status the machine's own page has;
    never the machine's page. A person's own pages (account, tunnels) are this panel's error page, as the machine
    refuses them to a link."""
    ids = await class_b(central, monkeypatch)
    client = central.app.state.link_clients.client(central.app.state.settings.link_targets["cliente"])
    for path in b_paths(ids):
        name = centralpages.match(path.split("?")[0])[0].name
        own = await client.get(f"/{path}", headers={**LINK, "X-OmniaCLI-Menu": "servers,instances,backups,settings",
                                                    "Accept": "text/html"})
        central.machine.asked.clear()
        resp = await central.get(f"/links/cliente/{path}", headers=PAGE)
        assert resp.status_code == own.status_code, (path, resp.text[:300])
        assert f"/api/pages/{name}" in central.machine.asked, path
        assert f"/{path.split('?')[0]}" not in central.machine.asked, path
        assert "data-machine-page-line" not in resp.text, path
        if name in centralpages.PERSON_PAGES:
            assert resp.status_code in (403, 404) and 'href="/links/cliente/instances"' in resp.text, path
        elif resp.status_code == 200:
            assert "/links/cliente/" in resp.text, path


async def test_the_class_b_models_carry_no_secret_value(central, monkeypatch):
    """AD-017 §8.1 for the 14: no secret's name among the fields of a model, at any depth, and no secret's value: a
    link's token, the private SSH key, a git credential's token, the Enterprise source's, a destination's password
    and key, the SMTP password. Of each the page says only whether it is set."""
    ids = await class_b(central, monkeypatch)
    async with httpx.AsyncClient(transport=central.machine.asgi, base_url="http://customer.test") as machine:
        for name, path in B_PATHS.items():
            params = centralpages.match(path.format(**ids))[1]
            resp = await machine.get(f"/api/pages/{name}", params=params, headers=LINK)
            if name in centralpages.PERSON_PAGES:
                assert resp.status_code in (403, 404) and "context" not in resp.json(), name
                continue
            assert resp.status_code == 200, (name, resp.text[:300])
            data = resp.json()
            assert data["agent_api"] == centralpages.AGENT_API and set(data["context"]) <= set(
                centralpages.BY_NAME[name].fields), name
            for secret in (*B_SECRETS, LINK_TOKEN):
                assert secret not in resp.text, (name, secret)
            assert not secret_keys(data["context"]), name
        # the workspace's destinations too
        resp = await machine.get("/api/pages/destinations", params={"workspace": "acme"}, headers=LINK)
        assert resp.status_code == 200 and not [s for s in B_SECRETS if s in resp.text]
        # what they show of them: set or not, the public halves
        model = (await machine.get("/api/pages/links", headers=LINK)).json()["context"]
        assert model["links"][0]["ssh"]["host_key"] == "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGhvc3Qta2V5LTkxNC1vZi1mYXItZXhhbXBsZS1jb20h" and "token" not in model["links"][0]
        assert model["public_key"] == "ssh-ed25519 AAAApub914 omniacli"
        model = (await machine.get("/api/pages/credentials", headers=LINK)).json()["context"]
        assert next(s for s in model["scopes"] if s["name"] == "srv")["credentials"] == [
            {"host": "github.com", "username": "bot", "token_set": True}]
        model = (await machine.get("/api/pages/enterprise", headers=LINK)).json()["context"]
        assert model["source"]["token_set"] is True
        model = (await machine.get("/api/pages/destinations", headers=LINK)).json()["context"]
        assert {r["name"]: r["set"] for r in model["rows"]} == {"off": ["pass"], "ws-off": ["pass"], "dav": ["pass"]}
        # the kinds in English: the panel says them in its words
        assert model["kinds"]["s3"]["label"] == "S3 and compatible"
        # the notices of a schedule and of a workspace: their sites only (a webhook keeps its secret in its path)
        model = (await machine.get("/api/pages/schedules", headers=LINK)).json()["context"]
        assert model["schedules"][0]["data"]["notify"] == ["https://hooks.example.com/…"]
        model = (await machine.get("/api/pages/workspaces", headers=LINK)).json()["context"]
        assert model["workspaces"][0]["notify"] == ["https://hooks.example.com/…"]


async def test_the_class_b_pages_have_the_controls_of_the_machines(central, monkeypatch):
    """AD-017 §8.3 for the 14: the whole <main> drawn here equals the machine's own, for admin, developer and user
    through the link; and on the machine every page it draws equals the one drawn from its model
    (serverparity.Twice)."""
    ids = await class_b(central, monkeypatch)
    twice = Twice(monkeypatch)
    for user in ("admin", "dev", "viewer"):
        for path in b_paths(ids):
            here = await central.get(f"/links/cliente/{path}", headers=as_(user))
            link_pages(central, "machine")
            theirs = await central.get(f"/links/cliente/{path}", headers=as_(user))
            link_pages(central, "here")
            assert here.status_code == theirs.status_code, (user, path, here.text[:300])
            # (an access log grows between two requests, the reads of these very pages: its page drawn from the
            # model is compared with the one drawn from the same context, by Twice on the machine)
            if here.status_code == 200 and centralpages.match(path.split("?")[0])[0].name != "access":
                assert main_of(here.text) == main_of(theirs.text), (user, path)
    # the machine drew each page through the link, with the role of who looks (never more than the link's)
    compared = {(c["page"], c["role"]) for c in twice.compared}
    assert {p for p, _ in compared} == set(B_PATHS) - centralpages.PERSON_PAGES
    assert {r for p, r in compared if p == "jobs"} == {"admin", "developer", "user"}
    assert {r for p, r in compared if p == "config"} == {"admin"}


async def test_the_class_b_parity_of_the_people_of_a_workspace(pair, monkeypatch):
    """The people of a workspace (user, developer, admin, a copy of admin and a custom role without the logs) on the
    pages of class B they reach on their home, and once srv is placed on its host (AD-014's delegated scope): the
    page drawn from the model equals the page drawn from the live objects, every time."""
    twice = Twice(monkeypatch)
    job = pair.app.state.jobs.submit("Git pull srv/test", ["srv/test"], "manual: mario", _nothing, workspace="acme")
    people = ("mario", "as-user", "as-developer", "as-admin", "copy-admin", "no-logs")
    paths = ("/jobs", f"/jobs/{job.id}", "/workspaces/acme/destinations", "/workspaces/acme/audit", "/workspaces")
    answered = {}
    for placed in (False, True):
        if placed:
            pair.place()
        for person in people:
            for path in paths:
                resp = await pair.get(path, headers=as_user(person), follow_redirects=True)
                answered.setdefault((person, path), set()).add(resp.status_code)
    # the same answers placed or not; each page a person reaches drawn the same from its model
    assert all(len(codes) == 1 for codes in answered.values()), answered
    drawn = {(c["user"], c["page"]) for c in twice.compared}
    assert all(c["scoped"] for c in twice.compared)
    for person in people:
        assert {(person, "jobs"), (person, "job")} <= drawn, person
    assert {("mario", "destinations"), ("mario", "access")} <= drawn
    # a person who reaches a page has it from the model with what its role allows: the user does not get the
    # workspace's destinations
    assert answered[("as-user", "/workspaces/acme/destinations")] == {403}


async def _nothing(job):
    job.log("done")


async def test_a_model_of_class_b_that_leaves_the_machine_is_refused(central, monkeypatch):
    """A name the page puts in an address (a job's id, a server's, a workspace's) that is not a name, the address
    of a page's requests that is not a path of the machine, a model of another job or workspace than the path's, a
    person's page answered with a context: the whole model refused, this panel's error page."""
    ids = await class_b(central, monkeypatch)

    async def own(name: str, **params) -> dict:
        async with httpx.AsyncClient(transport=central.machine.asgi, base_url="http://customer.test") as machine:
            return (await machine.get(f"/api/pages/{name}", params=params, headers=LINK)).json()

    cases = []
    data = await own("jobs")
    data["context"]["jobs"][0]["id"] = "../../api/links/delete"
    cases.append(("jobs", "jobs", data))
    data = await own("job", job_id=ids["job"])
    data["context"]["job"]["id"] = "20261124-000000-other"
    cases.append(("job", f"jobs/{ids['job']}", data))
    data = await own("job", job_id=ids["job"])
    data["context"]["job"]["instances"] = ["../x/production"]
    cases.append(("job", f"jobs/{ids['job']}", data))
    data = await own("destinations")
    data["context"]["api"] = "//evil.test/x"
    cases.append(("destinations", "destinations", data))
    data = await own("destinations")
    data["context"]["rows"][0]["workspace"] = "../../links/other"
    cases.append(("destinations", "destinations", data))
    data = await own("access", workspace="acme")
    data["context"]["ws"]["name"] = "other"
    cases.append(("access", "workspaces/acme/audit", data))
    data = await own("config")
    data["context"]["servers"][0]["name"] = "../../links/other/config"
    cases.append(("config", "config", data))
    cases.append(("account", "account", {"agent_api": 3, "page": "account", "role": "admin",
                                         "context": {"name": "admin"}}))
    for name, path, data in cases:
        central.machine.answer[f"/api/pages/{name}"] = (200, {"content-type": "application/json"},
                                                       json.dumps(data).encode())
        resp = await central.get(f"/links/cliente/{path}", headers=PAGE)
        assert resp.status_code == 502 and "is not the one this panel reads" in resp.text, (name, path)
        assert "../../" not in resp.text and "evil.test" not in resp.text, (name, path)
        del central.machine.answer[f"/api/pages/{name}"]


async def test_the_kinds_of_destination_in_the_words_of_who_looks(central, monkeypatch):
    """The model says the kinds of destination in English; the page drawn here says them in the reader's
    language, by this panel's catalogue."""
    await class_b(central, monkeypatch)
    resp = await central.get("/links/cliente/destinations", headers={**PAGE, "Cookie": "omniacli_lang=it"})
    assert resp.status_code == 200 and "S3 e compatibili" in resp.text and "Chiave privata (PEM)" in resp.text
    assert "S3 and compatible" not in resp.text


async def test_every_read_of_a_class_b_model_is_in_the_machines_access_log(central, monkeypatch):
    """AD-017 §17.5 decision 5: each read of a model of class B, with who looks through the link (on_behalf)."""
    ids = await class_b(central, monkeypatch)
    for path in ("jobs", f"jobs/{ids['job']}", "workspaces/acme/audit"):
        assert (await central.get(f"/links/cliente/{path}", headers=as_("dev"))).status_code in (200, 403), path
    reads = [e for e in central.machine.asgi.app.state.access_log.recent(200)
             if e.get("event") == "read" and str(e.get("what", "")).startswith("model of the page ")]
    assert {e["what"] for e in reads} >= {"model of the page jobs", "model of the page job",
                                          "model of the page access"}
    assert all(e["kind"] == "link" and e["on_behalf"] == "dev" for e in reads), reads


# --- the review of 6228654 -----------------------------------------------------------------------------------

async def own_b(central, name: str, **params) -> dict:
    async with httpx.AsyncClient(transport=central.machine.asgi, base_url="http://customer.test") as machine:
        return (await machine.get(f"/api/pages/{name}", params=params, headers=LINK)).json()


def answer_b(central, name: str, data: dict) -> None:
    central.machine.answer[f"/api/pages/{name}"] = (200, {"content-type": "application/json"}, json.dumps(data).encode())


async def test_a_links_row_stays_under_the_link(central, monkeypatch):
    """Review finding 1: the row of a link of the machine opens it under the link's prefix (the machine's own link
    far, never this panel's link far), and a link named to leave the machine refuses the model."""
    await class_b(central, monkeypatch)
    page = (await central.get("/links/cliente/links", headers=PAGE)).text
    assert 'href="/links/cliente/links/far/"' in page and 'href="/links/far/"' not in page
    for name in ("../../options", "../other/servers", "a/b"):
        data = await own_b(central, "links")
        data["context"]["links"][0]["name"] = name
        answer_b(central, "links", data)
        resp = await central.get("/links/cliente/links", headers=PAGE)
        assert resp.status_code == 502 and f"/links/{name}" not in resp.text, name


async def test_the_customers_names_are_not_addresses(central, monkeypatch):
    """Review finding 2: the mappings of these pages are keyed by the customer's own names (a WebDAV destination's
    url, a storage root, a token, users, subdomains called backups, upload, preview, open, install): drawn, never
    taken for addresses; the address of a link to elsewhere is still checked at its place."""
    await class_b(central, monkeypatch)
    for path in ("workspaces", "workspaces?edit=acme", "workspaces/acme/destinations", "destinations", "links"):
        resp = await central.get(f"/links/cliente/{path}", headers=PAGE)
        assert resp.status_code == 200 and "data-machine-page-line" not in resp.text, (path, resp.text[:300])
    data = await own_b(central, "links")
    data["context"]["links"][0]["url"] = "javascript:alert(1)"
    answer_b(central, "links", data)
    resp = await central.get("/links/cliente/links", headers=PAGE)
    assert resp.status_code == 502 and "javascript:alert(1)" not in resp.text


async def test_the_workspaces_list_has_no_machines_of_the_machines_word(central, monkeypatch):
    """Review finding 3: through a link the list's machines are never the machine's word (through a link it has none):
    a name there would build rows and forms under /links/<name> of this panel."""
    await class_b(central, monkeypatch)
    data = await own_b(central, "workspaces")
    data["context"].update(links=["../../evil", "other"], machine_column=True)
    answer_b(central, "workspaces", data)
    resp = await central.get("/links/cliente/workspaces", headers=PAGE)
    assert resp.status_code == 200 and "../../evil" not in resp.text and '"other"' not in resp.text
    assert "const MACHINE_COLUMN = false" in resp.text


async def test_a_list_of_names_is_a_list(central, monkeypatch):
    """Review finding 5: the jobs of an access log's entry, a job's instances, its live servers are lists of names;
    a text there (which the page would go through letter by letter) refuses the model."""
    ids = await class_b(central, monkeypatch)
    for name, path, params, change in (
            ("access", "access", {}, lambda c: c["entries"][0].update(job="../x")),
            ("job", f"jobs/{ids['job']}", {"job_id": ids["job"]}, lambda c: c["job"].update(instances="srv/test")),
            ("job", f"jobs/{ids['job']}", {"job_id": ids["job"]}, lambda c: c.update(live="srv"))):
        data = await own_b(central, name, **params)
        change(data["context"])
        answer_b(central, name, data)
        resp = await central.get(f"/links/cliente/{path}", headers=PAGE)
        assert resp.status_code == 502 and "is not the one this panel reads" in resp.text, (name, path)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_a_jobs_download_is_a_path_of_the_machine():
    """Review finding 4: the file a job's page offers (and with ?download=1 opens by itself) comes from the job's
    result: a path of the machine under the page's prefix, never another site, another machine or a route of the
    panel it is seen through."""
    source = app_module._TEMPLATES.env.loader.get_source(app_module._TEMPLATES.env, "job.html")[0]
    function = re.search(r"function downloadPath\(path\) \{.*?\n  \}", source, re.S).group(0)
    assert "|| !downloadPath(result.download)) return;" in source
    cases = {"/api/servers/srv/test/backups/x.zip/download": True, "/api/jobs/1/file?name=a%20b.zip": True,
             "//evil.test/x": False, "https://evil.test/x": False, "javascript:alert(1)": False,
             "/../../api/links/delete": False, "/%2e%2e/%2e%2e/api/x": False, "/a\\b": False, "/a b": False,
             "x.zip": False, "": False, "/%E0%A4%A": False}
    script = function + f"\nconsole.log(JSON.stringify({json.dumps(list(cases))}.map(downloadPath)));" + \
        "\nconsole.log(JSON.stringify([downloadPath(undefined), downloadPath(5)]));"
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout.splitlines()
    assert dict(zip(cases, json.loads(out[0]))) == cases
    assert json.loads(out[1]) == [False, False]
