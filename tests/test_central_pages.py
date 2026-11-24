# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""AD-017 slices 1 and 2: the contract of a linked machine (agent_api, plugins) and the 12 pages of class A that
the panel draws itself for a link with pages: here, from the model the machine answers; the machine's own page,
with a line on top, when the contract does not allow it; pages: machine as the way back; and a machine's HTML
refused where the panel takes data only."""
import json

import httpx
import pytest
import yaml

from omniacli import centralpages
from omniacli.app import create_app
from omniacli.config import LinkAccess, parse_settings

from .conftest import LINK_TOKEN, USER_HASH, BasicAsSession, hash_token
from .test_app import AUTH

N = centralpages.CENTRAL_AGENT_API
PAGE = {"Authorization": AUTH, "Accept": "text/html,application/xhtml+xml"}
JSON = {"Authorization": AUTH, "Accept": "application/json"}
# a page of each of the 12, as the browser opens it under /links/<name>/
PATHS = {"disks": "disks", "log": "servers/srv/test/log", "own_log": "omniacli/log", "instances": "instances",
         "backups": "backups", "machines": "machines", "discover": "config/discover",
         "connection": "pullers/connections/nobody", "shell": "servers/srv/test/shell", "help": "help",
         "about": "about", "settings": "settings"}
# names of secrets the model of a page never carries (AD-017 §8.1)
SECRETS = ("admin_passwd", "db_password", "token", "secret", "password")


def keys(value) -> list[str]:
    """Every key of a JSON value, at any depth."""
    if isinstance(value, dict):
        return [*value, *(k for v in value.values() for k in keys(v))]
    if isinstance(value, list):
        return [k for v in value for k in keys(v)]
    return []


class Machine(httpx.AsyncBaseTransport):
    """The customer's machine behind the link: what it was asked, and what it says of itself (agent_api,
    plugins) as the test sets it; html_at: an answer of HTML for that path, as a machine that writes one."""

    def __init__(self, app):
        self.asgi = httpx.ASGITransport(app=app)
        self.asked: list[str] = []
        self.info: dict | None = None
        self.html_at = ""

    async def handle_async_request(self, request):
        self.asked.append(request.url.path)
        if self.html_at and request.url.path == self.html_at:
            return httpx.Response(200, headers={"content-type": "text/html"}, stream=httpx.ByteStream(b"<script>alert(1)</script>"))
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
    machine = Machine(create_app(machine_settings))
    conf = tmp_path / "central"
    conf.mkdir()
    (conf / "links.yaml").write_text(yaml.safe_dump({"links": {
        "cliente": {"url": "http://customer.test", "token": LINK_TOKEN}}}))
    settings = parse_settings({"auth": {"users": {"admin": USER_HASH}}, "backup_dir": str(tmp_path / "b"),
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
    for other in ("servers/srv", "", "jobs/1", "servers/srv/test/log/x", "disks/x"):
        assert centralpages.match(other) is None, other


async def test_the_machine_says_its_contract(central):
    info = (await central.get("/api/info", headers=JSON)).json()
    assert info["agent_api"] == centralpages.AGENT_API == 1
    assert info["plugins"] == centralpages.plugin_versions() and info["plugins"]["odoo"] == centralpages.BUILT_IN
    # Try the link reads it, and keeps it
    tried = (await central.post("/api/links/cliente/test", headers={**JSON, "Origin": "http://central.test"})).json()
    assert tried["ok"] and tried["agent_api"] == 1
    assert central.app.state.link_agents["cliente"]["agent_api"] == 1


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
                     "about", "settings"):
            params = {"log": {"server": "srv", "role": "test"},
                      "shell": {"server": "srv", "role": "test", "kind": "shell"}}.get(name, {})
            resp = await machine.get(f"/api/pages/{name}", params=params, headers=link)
            assert resp.status_code == 200, (name, resp.text)
            data = resp.json()
            assert data["agent_api"] == 1 and data["role"] == "admin" and set(data["context"]) <= set(
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
    """A machine of before (no agent_api): every page is its own, as before, with the line that says why."""
    central.machine.info = {"version": "2026.1.5513"}
    for path in ("disks", "servers/srv", "help"):
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
    # a page this panel does not draw yet (AD-017 §9, slices 3-5): the machine's, as before
    resp = await central.get("/links/cliente/servers/srv", headers=PAGE)
    assert resp.status_code == 200 and "data-machine-page-line" not in resp.text
    # the panel's own script files, never the machine's
    central.machine.asked.clear()
    assert (await central.get("/links/cliente/static/xterm.js", headers=PAGE)).status_code == 200
    assert "/static/xterm.js" not in central.machine.asked


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
