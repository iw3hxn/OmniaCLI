# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""AD-017 slices 1 and 2: the contract of a linked machine (agent_api, plugins) and the 12 pages of class A that
the panel draws itself for a link with pages: here, from the model the machine answers; the machine's own page,
with a line on top, when the contract does not allow it; pages: machine as the way back; and a machine's HTML
refused where the panel takes data only."""
import base64
import dataclasses
import gzip
import json

import httpx
import pytest
import yaml

from omniacli import centralpages
from omniacli.app import create_app
from omniacli.config import LinkAccess, parse_connection, parse_settings

from .conftest import LINK_TOKEN, PASSWORD, USER_HASH, BasicAsSession, hash_token
from .test_app import AUTH
from .serverparity import SECRETS, Twice, keys, main_of
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
    assert {centralpages.match(path)[0].name for path in PATHS.values()} == set(centralpages.BY_NAME) == set(PATHS)
    assert centralpages.match("servers/srv/test/log")[1] == {"server": "srv", "role": "test"}
    assert centralpages.match("servers/srv/test/psql")[1] == {"server": "srv", "role": "test", "kind": "psql"}
    for other in ("servers/srv/x", "servers", "", "jobs/1", "servers/srv/test/log/x", "disks/x"):
        assert centralpages.match(other) is None, other


async def test_the_machine_says_its_contract(central):
    info = (await central.get("/api/info", headers=JSON)).json()
    assert info["agent_api"] == centralpages.AGENT_API == 2
    assert info["plugins"] == centralpages.plugin_versions() and info["plugins"]["odoo"] == centralpages.BUILT_IN
    # Try the link reads it, and keeps it
    tried = (await central.post("/api/links/cliente/test", headers={**JSON, "Origin": "http://central.test"})).json()
    assert tried["ok"] and tried["agent_api"] == 2
    assert central.app.state.link_agents["cliente"]["agent_api"] == 2


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
    for path in ("jobs", "", "users"):
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
    resp = await central.get("/links/cliente/jobs", headers=PAGE)
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
    assert json.loads(seen_file.read_text()) == {"cliente": 2} and oct(seen_file.stat().st_mode)[-3:] == "600"
    central.machine.info = {"version": "2026.1.5513"}
    central.app.state.link_agents["cliente"]["at"] -= centralpages.READ_EVERY + 1
    central.machine.asked.clear()
    resp = await central.get("/links/cliente/disks", headers=PAGE)
    assert resp.status_code == 502 and "Page of cliente not shown" in resp.text and "now it says 0" in resp.text
    assert "/disks" not in central.machine.asked and "data-machine-page-line" not in resp.text
    # a page not drawn here: the machine's, as before
    assert (await central.get("/links/cliente/jobs", headers=PAGE)).status_code == 200
    # what was seen stays seen in a panel started again
    assert centralpages.load_seen(seen_file) == {"cliente": 2}
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
    assert central.app.state.link_agents_seen == {"cliente": 2}
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


async def test_a_machine_at_one_writes_its_instance_page_with_the_line(central):
    """A machine of slices 1-2 (agent_api 1): the pages of class A drawn here, its instance's page its own with the
    line that says to update it; once it said 2, it saying 1 again is this panel's page that says so."""
    central.machine.info = {"agent_api": 1, "plugins": {}, "version": "2026.1.5515"}
    central.machine.asked.clear()
    resp = await central.get("/links/cliente/servers/srv", headers=PAGE)
    assert resp.status_code == 200 and "/servers/srv" in central.machine.asked
    assert "/api/pages/server" not in central.machine.asked
    assert "data-machine-page-line" in resp.text and "cliente (2026.1.5515): update it" in resp.text
    resp = await central.get("/links/cliente/disks", headers=PAGE)
    assert resp.status_code == 200 and "data-machine-page-line" not in resp.text
    assert central.app.state.link_agents_seen == {"cliente": 1}
    # it said 2, then 1 again: its instance's page is not shown by its word; the pages of 1 are still drawn
    central.machine.info = None
    central.app.state.link_agents.clear()
    assert (await central.get("/links/cliente/disks", headers=PAGE)).status_code == 200
    assert central.app.state.link_agents_seen == {"cliente": 2}
    central.machine.info = {"agent_api": 1, "plugins": {}, "version": "2026.1.5515"}
    central.app.state.link_agents.clear()
    central.machine.asked.clear()
    resp = await central.get("/links/cliente/servers/srv", headers=PAGE)
    assert resp.status_code == 502 and "Page of cliente not shown" in resp.text
    assert "/servers/srv" not in central.machine.asked
    assert (await central.get("/links/cliente/disks", headers=PAGE)).status_code == 200


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


async def test_an_instance_moved_away_keeps_the_machines_page(central):
    """The machine says the page is its own (an instance placed elsewhere, its copy deleted: the page of its
    move, class D): it passes as before, without the line."""
    central.machine.answer["/api/pages/server"] = (200, {"content-type": "application/json"}, json.dumps({
        "agent_api": centralpages.AGENT_API, "page": "server", "role": "admin", "own_page": True}).encode())
    central.machine.answer["/servers/srv"] = (200, {"content-type": "text/html"},
                                             b"<html><body>its move's page</body></html>")
    resp = await central.get("/links/cliente/servers/srv", headers=PAGE)
    assert resp.status_code == 200 and resp.text == "<html><body>its move's page</body></html>"


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
    if own.status_code == 307:
        assert data["moved"] == own.headers["location"] == "/links/home/placed/srv/servers/srv"
    else:
        assert data.get("own_page") is True and own.status_code == 200
