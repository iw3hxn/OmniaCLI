# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""AD-017 slice 6, the agent mode (§17.3): panel: full | agent in the main configuration; a machine in agent mode
keeps its own pages as the reserve, says on top whom it is managed from, and never goes without an admin of its
own; the central panel takes from it data only (the way back of a machine behind apart), on its own word, kept
once the machine said it; Update OmniaCLI of such a machine is information until its agent_api is behind."""
import json
import time
from pathlib import Path

import httpx
import pytest
import yaml

from omniacli import app as app_module
from omniacli import centralpages, cli
from omniacli.app import create_app
from omniacli.auth import hash_token
from omniacli.config import ConfigError, Invitation, invitations_file_of, parse_settings, save_invitations

from .conftest import LINK_TOKEN, USER_HASH, BasicAsSession
from .test_app import AUTH
from .test_central_pages import JSON, PAGE, central, link_pages  # noqa: F401 - the central fixture

N = centralpages.CENTRAL_AGENT_API
ADMIN_TOKEN = "an-admin-token-of-the-machine"


def machine_config(tmp_path: Path, panel: str | None, users: dict, web_users: dict | None = None,
                   invited: tuple = ()) -> dict:
    """A machine's main configuration with panel (None: not said), users in auth, web_users in its users file and
    invitations not used for the names invited."""
    conf = tmp_path / "conf"
    conf.mkdir(exist_ok=True)
    if web_users is not None:
        (conf / "users.yaml").write_text(yaml.safe_dump({"users": web_users}))
    if invited:
        now = time.time()
        save_invitations(invitations_file_of(conf / "users.yaml"),
                         [Invitation(n, hash_token(f"t-{n}"), now, now + 3600, "admin") for n in invited], now)
    data = {"auth": {"users": users, "tokens": {"boss": {"token": hash_token(ADMIN_TOKEN), "role": "admin"}},
                     "links": {"omnia-central": {"token": hash_token(LINK_TOKEN), "role": "admin"}}},
            "backup_dir": str(tmp_path / "b"), "jobs_dir": str(tmp_path / "j"), "servers_file": "servers.yaml"}
    if panel is not None:
        data["panel"] = panel
    return data


# --- the machine: panel: full | agent, and its local admin (§17.4.2) ----------------------------------------

def test_the_panel_of_the_main_configuration(tmp_path):
    """full by default and as before; agent with an admin who logs in on the machine; anything else refused."""
    admin = {"admin": USER_HASH}
    assert parse_settings(machine_config(tmp_path, None, admin), base_dir=tmp_path / "conf").panel == "full"
    assert parse_settings(machine_config(tmp_path, "full", admin), base_dir=tmp_path / "conf").panel == "full"
    agent = parse_settings(machine_config(tmp_path, "agent", admin), base_dir=tmp_path / "conf")
    assert agent.panel == "agent"
    with pytest.raises(ConfigError, match="panel: 'central' is not one of full, agent"):
        parse_settings(machine_config(tmp_path, "central", admin), base_dir=tmp_path / "conf")


@pytest.mark.parametrize("users, web_users, invited", [
    ({"viewer": {"password": USER_HASH, "role": "user"}}, None, ()),  # no admin at all
    ({"dev": {"password": USER_HASH, "role": "developer"}}, {"ws-admin": {"password": USER_HASH, "role": "workspace"}},
     ()),  # a person of a workspace is no admin of the machine
    ({"dev": {"password": USER_HASH, "role": "developer"}}, {"nuovo": {"password": USER_HASH, "role": "admin"}},
     ("nuovo",)),  # an admin invited who has not set its password yet
])
def test_agent_without_a_local_admin_is_refused(tmp_path, users, web_users, invited):
    data = machine_config(tmp_path, "agent", users, web_users, invited)
    with pytest.raises(ConfigError, match="panel: agent needs an admin of this machine who logs in on it"):
        parse_settings(data, base_dir=tmp_path / "conf")
    # the same machine full starts as before
    assert parse_settings({**data, "panel": "full"}, base_dir=tmp_path / "conf").panel == "full"
    # and an admin of the users file is one
    (tmp_path / "conf" / "users.yaml").write_text(yaml.safe_dump({"users": {
        **(web_users or {}), "carla": {"password": USER_HASH, "role": "admin"}}}))
    assert parse_settings(data, base_dir=tmp_path / "conf").panel == "agent"


def test_the_service_and_check_config_refuse_it_with_words(tmp_path, capsys):
    """OmniaCLI does not start (serve loads the configuration first) and check-config says why."""
    path = tmp_path / "conf" / "omniacli.yaml"
    path.parent.mkdir()
    path.write_text(yaml.safe_dump(machine_config(tmp_path, "agent", {"viewer": {"password": USER_HASH, "role": "user"}})))
    assert cli.main(["check-config", "-c", str(path)]) == 2
    said = capsys.readouterr().err
    assert "Configuration error: panel: agent needs an admin of this machine" in said and "or leave panel: full" in said
    assert cli.main(["serve", "-c", str(path), "--port", "1"]) == 2


async def test_the_reserve_says_whom_it_is_managed_from(tmp_path):
    """The machine's own pages work in agent mode and say on top whom it is managed from, in Italian too, to the
    people who log in there; never to a link (the central draws its own) or a token; nothing in full mode."""
    for panel, central_name in (("agent", ""), ("agent", "Pannello OmniaSolutions"), ("full", "")):
        data = machine_config(tmp_path, panel, {"admin": USER_HASH})
        if central_name:
            data["central"] = central_name
        app = create_app(parse_settings(data, base_dir=tmp_path / "conf"))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=BasicAsSession(app)),
                                     base_url="http://machine.test") as c:
            for path in ("/", "/users", "/options", "/disks"):
                resp = await c.get(path, headers={"Authorization": AUTH, "Accept": "text/html"})
                assert resp.status_code == 200, (panel, path)
                assert ('id="managed-from"' in resp.text) == (panel == "agent"), (panel, path)
            if panel == "agent":
                name = central_name or "omnia-central"
                assert f"This machine is managed from {name}: these pages are its reserve" in resp.text
                resp = await c.get("/disks", headers={"Authorization": AUTH, "Accept": "text/html",
                                                      "Cookie": "omniacli_lang=it"})
                assert f"Questa macchina si gestisce da {name}: queste pagine sono la sua riserva" in resp.text
            for bearer in (LINK_TOKEN, ADMIN_TOKEN):
                resp = await c.get("/disks", headers={"Authorization": f"Bearer {bearer}", "Accept": "text/html"})
                assert resp.status_code == 200 and 'id="managed-from"' not in resp.text
            info = (await c.get("/api/info", headers={"Authorization": AUTH, "Accept": "application/json"})).json()
            assert info["panel"] == panel and info["agent_api"] == centralpages.AGENT_API


async def test_the_last_local_admin_is_never_taken_away(tmp_path):
    """In agent mode the users file never loses its last admin who logs in there: a role taken away or a user
    deleted by the central through its link is refused with words, even when an admin invited but without a
    password is left; in full mode as before (an invited admin counts as the other admin)."""
    for panel in ("agent", "full"):
        base = tmp_path / panel
        base.mkdir()
        data = machine_config(base, panel, {"viewer": {"password": USER_HASH, "role": "user"}},
                              {"carla": {"password": USER_HASH, "role": "admin"}})
        app = create_app(parse_settings(data, base_dir=base / "conf"))
        # the central, through its link of role admin, for one of its people
        boss = {"Authorization": f"Bearer {LINK_TOKEN}", "X-OmniaCLI-User": "carlo", "Accept": "application/json"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://machine.test") as c:
            made = await c.post("/api/access/users/save", headers=boss,
                                json={"name": "nuovo", "role": "admin", "new": True, "invite": True})
            assert made.status_code == 200, made.text
            demoted = await c.post("/api/access/users/save", headers=boss,
                                   json={"name": "carla", "role": "developer"})
            deleted = await c.post("/api/access/users/delete", headers=boss, json={"name": "carla"})
            if panel == "agent":
                for resp in (demoted, deleted):
                    assert resp.status_code == 400, resp.text
                    assert "This machine is managed from omnia-central (panel: agent): it keeps at least one admin" \
                        in resp.json()["error"]
                assert app.state.settings.user_role("carla") == "admin"
                # in Italian
                resp = await c.post("/api/access/users/delete", headers={**boss, "Accept-Language": "it"},
                                    json={"name": "carla"})
                assert "Questa macchina si gestisce da omnia-central (panel: agent)" in json.dumps(
                    resp.json(), ensure_ascii=False)
                # with a second admin of its own, the first may go
                more = await c.post("/api/access/users/save", headers=boss,
                                    json={"name": "bruno", "role": "admin", "new": True, "password": "a-long-password-9"})
                assert more.status_code == 200, more.text
                assert (await c.post("/api/access/users/delete", headers=boss, json={"name": "carla"})).status_code == 200
            else:
                assert demoted.status_code == 200 and deleted.status_code == 200


# --- the central: from a machine in agent mode, data only (§17.3 point 2, §17.5) -------------------------------

def agent(central, panel: str = "agent", agent_api: int = N) -> None:
    """The machine of the link in that mode, at that agent_api, read again by the central."""
    settings = central.machine.asgi.app.state.settings
    settings.panel = panel
    central.machine.info = {"agent_api": agent_api, "plugins": {}, "panel": panel}
    central.app.state.link_agents.clear()


async def test_an_agent_machines_html_is_refused_but_the_way_back(central):
    """A text/html answer of a machine in agent mode never reaches the browser: not where a script asks data, not
    for a page this panel is older for or a plugin apart (this panel's page says why), not for an invitation or a
    plugin's own page (not even asked of it); the way back of §17.2 (a machine behind the page) passes, with the
    line «update it»."""
    agent(central)
    central.machine.html_at = "/api/disks"
    resp = await central.get("/links/cliente/api/disks", headers=JSON)
    assert resp.status_code == 502 and "<script>" not in resp.text
    # the page is drawn here
    central.machine.html_at = ""
    resp = await central.get("/links/cliente/disks", headers=PAGE)
    assert resp.status_code == 200 and "data-machine-page-line" not in resp.text
    assert "/api/pages/disks" in central.machine.asked
    # this panel older than the machine, a plugin apart: this panel's page, not the machine's with the line
    for info, why, said in (({"agent_api": N + 1, "plugins": {}}, "ahead", "This panel is older than cliente"),
                            ({"agent_api": N, "plugins": {"acme": "1.0"}}, "plugins", "(acme 1.0): install them here")):
        central.machine.info = {**info, "panel": "agent"}
        central.app.state.link_agents.clear()
        central.machine.asked.clear()
        resp = await central.get("/links/cliente/disks", headers=PAGE)
        assert resp.status_code == 502 and f'data-why="{why}"' in resp.text and said in resp.text, why
        assert "data-machine-page-line" not in resp.text and "/disks" not in central.machine.asked, why
        assert (await central.get("/links/cliente/disks", headers=JSON)).json()["error"].startswith(
            "link cliente: managed from this panel (panel: agent)")
    # in Italian
    resp = await central.get("/links/cliente/disks", headers={**PAGE, "Cookie": "omniacli_lang=it"})
    assert "Pagina di cliente non lasciata passare" in resp.text and "si gestisce da questo pannello" in resp.text
    # an invitation and a plugin's own page: never asked of the machine, never its HTML
    agent(central)
    for path, why in (("invite/abc", "invite"), ("x/odoo/servers/srv/production/addons", "plugin_page")):
        central.machine.answer[f"/{path}"] = (200, {"content-type": "text/html"}, b"<html><body>MACHINE-OWN-HTML</body></html>")
        central.machine.asked.clear()
        resp = await central.get(f"/links/cliente/{path}", headers=PAGE)
        assert resp.status_code == 403 and f'data-why="{why}"' in resp.text and "MACHINE-OWN-HTML" not in resp.text, path
        assert f"/{path}" not in central.machine.asked, path
    assert "the plugin odoo is opened on cliente itself" in resp.text
    # asked by a script, it is data: the machine's HTML there is refused too
    resp = await central.get("/links/cliente/x/odoo/servers/srv/production/addons", headers=JSON)
    assert resp.status_code == 404 and "MACHINE-OWN-HTML" not in resp.text
    # the way back: a machine behind a page this panel draws (an index of agent_api N) passes with the line
    # (one that never said N: once said, less is a machine that went back, §17.2)
    central.app.state.link_agents_seen.clear()
    agent(central, agent_api=N - 1)
    resp = await central.get("/links/cliente/", headers=PAGE)
    assert resp.status_code == 200 and "data-machine-page-line" in resp.text and ": update it" in resp.text
    # a machine in agent mode that does not answer GET /api/info: no page of it
    agent(central)
    await central.get("/links/cliente/disks", headers=PAGE)
    central.machine.answer["/api/info"] = (503, {"content-type": "application/json"}, b"{}")
    central.app.state.link_agents.clear()
    central.app.state.link_agents_seen.clear()
    central.machine.answer["/help"] = (200, {"content-type": "text/html"}, b"<html><body>MACHINE-HELP-HTML</body></html>")
    resp = await central.get("/links/cliente/help", headers=PAGE)
    assert resp.status_code == 502 and 'data-why="no_info"' in resp.text and "MACHINE-HELP-HTML" not in resp.text


async def test_pages_machine_does_not_lift_it(central):
    """A link with pages: machine to a machine that says agent: its pages are drawn here and only data passes;
    saving it so again is refused with words."""
    link_pages(central, "machine")
    agent(central)
    central.machine.asked.clear()
    resp = await central.get("/links/cliente/disks", headers=PAGE)
    assert resp.status_code == 200 and "/api/pages/disks" in central.machine.asked
    central.machine.html_at = "/api/disks"
    assert (await central.get("/links/cliente/api/disks", headers=JSON)).status_code == 502
    origin = {**JSON, "Origin": "http://central.test"}
    resp = await central.post("/api/links/save", headers=origin, json={"name": "cliente", "url": "http://customer.test",
                                                                       "pages": "machine"})
    assert resp.status_code == 400 and "cliente is managed from this panel (panel: agent)" in resp.json()["error"]
    resp = await central.post("/api/links/save", headers={**origin, "Accept-Language": "it"},
                              json={"name": "cliente", "url": "http://customer.test", "pages": "machine"})
    assert "cliente si gestisce da questo pannello (panel: agent)" in json.dumps(resp.json(), ensure_ascii=False)


async def test_the_central_keeps_the_agent_mode_once_said(central):
    """The machine's word is kept on disk (link-panels.json, 600): one that says full afterwards (compromised) does
    not get its HTML through; an admin here saving the link takes its word again; deleting it forgets it."""
    agent(central)
    await central.get("/links/cliente/disks", headers=PAGE)
    folder = central.app.state.settings.links_file.parent
    kept = folder / centralpages.PANELS_FILE
    assert json.loads(kept.read_text()) == {"cliente": "agent"} and kept.stat().st_mode & 0o777 == 0o600
    agent(central, panel="full", agent_api=N + 1)
    central.machine.asked.clear()
    resp = await central.get("/links/cliente/disks", headers=PAGE)
    assert resp.status_code == 502 and 'data-why="ahead"' in resp.text and "/disks" not in central.machine.asked
    # a panel started again reads it
    assert centralpages.load_panels(kept) == {"cliente"}
    # saved by an admin here: the machine's word again (full: its page with the line, as before)
    origin = {**JSON, "Origin": "http://central.test"}
    assert (await central.post("/api/links/save", headers=origin,
                               json={"name": "cliente", "url": "http://customer.test"})).status_code == 200
    resp = await central.get("/links/cliente/disks", headers=PAGE)
    assert resp.status_code == 200 and "the panel is older than the machine" in resp.text
    assert json.loads(kept.read_text()) == {}
    # said again, and the link deleted: forgotten
    agent(central)
    await central.get("/links/cliente/disks", headers=PAGE)
    assert (await central.post("/api/links/delete", headers=origin, json={"name": "cliente"})).status_code == 200
    assert json.loads(kept.read_text()) == {}


def test_the_panels_file_is_purged():
    purge = (Path(app_module.__file__).parent.parent / "packaging" / "debian" / "files" / "postrm").read_text()
    assert f"/var/lib/omniacli/{centralpages.PANELS_FILE}" in purge.split("    purge)", 1)[1]


# --- Update OmniaCLI of a machine in agent mode (§17.3 point 3) ---------------------------------------------------

def test_update_is_needed_only_when_the_agent_is_behind():
    assert centralpages.update_need({"agent_api": N}, agent_mode=False) == ""
    assert centralpages.update_need({"agent_api": centralpages.NEWEST_NEEDED}, agent_mode=True) == "info"
    assert centralpages.update_need({"agent_api": centralpages.NEWEST_NEEDED - 1}, agent_mode=True) == "needed"
    assert centralpages.update_need(None, agent_mode=True) == "needed"


async def test_the_section_of_an_agent_says_its_update_is_information(central):
    """The machine's section drawn here: for a machine in agent mode at the agent_api this panel asks, a newer
    OmniaCLI is said as information (data-need="info", not the warning); for a full one as before; the Machines
    page knows what this panel asks."""
    resp = await central.get("/links/cliente/", headers=PAGE)
    assert resp.status_code == 200 and 'id="omniacli-update-fact" data-need=""' in resp.text
    agent(central)
    resp = await central.get("/links/cliente/", headers=PAGE)
    assert resp.status_code == 200 and "data-machine-page-line" not in resp.text
    assert 'id="omniacli-update-fact" data-need="info"' in resp.text
    assert "not needed by this panel" in resp.text
    # the machine's own section says nothing of it (its word would not count)
    machine = central.machine.asgi.app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=BasicAsSession(machine)),
                                 base_url="http://customer.test") as c:
        own = await c.get("/", headers={"Authorization": AUTH, "Accept": "text/html"})
    assert 'id="omniacli-update-fact" data-need=""' in own.text
    resp = await central.get("/machines", headers=PAGE)
    assert f"const AGENT_API_ASKED = {centralpages.NEWEST_NEEDED};" in resp.text
    assert 'info.panel === "agent" && (info.agent_api || 0) >= AGENT_API_ASKED' in resp.text

