# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Workspaces menu and its list (docs/ux/UX-013 §A), and «Log in as…» on
the rows of the environments (§B): who has the entry, the rows of this
machine and of its links (one that does not answer, an older one), what
each person reads, and the row-by-row decision of Log in as."""

import json
import socket

import httpx
import pytest
import yaml

from omniacli import app as appmodule, ops
from omniacli.app import create_app
from omniacli.auth import Principal
from omniacli.config import Workspace, parse_settings

from .conftest import LINK_TOKEN, BasicAsSession, TOKEN, USER_HASH, hash_token
from .test_app import AUTH, client  # noqa: F401 - the client fixture
from .test_links import HOST, Network, as_user
from .test_workspaces import basic, workspaces  # noqa: F401 - the workspaces fixture


def nav(page: str) -> list[str]:
    """The entries of the top menu, in order: (text, href)."""
    menu = page.split('<nav class="topnav"', 1)[1].split("</nav>", 1)[0]
    found = []
    for part in menu.split('class="nav-link"')[1:]:
        href = part.split('href="', 1)[1].split('"', 1)[0]
        text = part.split(">", 1)[1].split("</a>", 1)[0].strip()
        found.append((text, href))
    return found


def test_who_has_the_workspaces_entry():
    admin = Principal("admin", "admin")
    assert appmodule.menu_places(admin) == ["servers", "workspaces", "instances", "backups", "settings"]
    assert "workspaces" not in appmodule.menu_places(Principal("dev", "developer"))
    two = Principal("anna", "user", workspaces=(("acme", "admin"), ("beta", "admin"), ("gamma", "user")), scoped=True)
    one = Principal("mario", "user", workspaces=(("acme", "admin"), ("beta", "developer")), scoped=True)
    # D2: from two workspaces where it is admin, one entry instead of one per workspace
    assert appmodule.workspaces_menu(two) and appmodule.admin_workspaces(two) == ["acme", "beta"]
    assert not appmodule.workspaces_menu(one) and appmodule.admin_workspaces(one) == ["acme"]
    assert appmodule.menu_places(two) == ["workspaces", "instances", "backups"]
    assert appmodule.WORKSPACES_MENU_FROM == 2 and appmodule.WORKSPACES_MENU_AFTER == "servers"


async def test_the_menu_and_the_list_of_this_machine(workspaces, client, env):  # noqa: F811
    settings = env["settings"]
    page = (await client.get("/workspaces", headers={"Authorization": AUTH})).text
    # the menu (A14.1): Servers, Workspaces, Instances, Backups, Settings, the place in view current
    assert [t for t, _ in nav(page)] == ["Servers", "Workspaces", "Instances", "Backups", "Settings"]
    menu = page.split('<nav class="topnav"', 1)[1].split("</nav>", 1)[0]
    assert '<a class="nav-link" href="/workspaces" aria-current="page"' in menu
    # one primary in the head (A14.4), no Machine column without links (A14.2), the login-as dialog drawn once
    head = page.split("</header>", 1)[1].split("<dialog", 1)[0]
    assert head.count('class="primary"') == 1 and ">Machine</th>" not in page
    assert page.count('id="login-as-dialog"') == 1 and 'fetch(`${PREFIX}/api/workspaces/list`' in page.replace(
        "const url = `${PREFIX}/api/workspaces/list`", "fetch(`${PREFIX}/api/workspaces/list`")
    # D3: no card in Settings, the crumbs of a workspace say Workspaces › acme (A14.12)
    settings_page = (await client.get("/settings", headers={"Authorization": AUTH})).text
    assert 'href="/workspaces"' not in settings_page.split("</nav>", 1)[1]
    crumbs = (await client.get("/workspaces/acme", headers={"Authorization": AUTH})).text.split('class="crumbs"', 1)[1]
    assert crumbs.split("</nav>", 1)[0].count("<li>") == 2 and '<a href="/workspaces">Workspaces</a>' in crumbs
    data = (await client.get("/api/workspaces/list", headers={"Authorization": AUTH})).json()
    assert data["link"] == "" and data["links"] == [] and data["viewer_role"] == "admin"
    rows = {w["name"]: w for w in data["workspaces"]}
    assert set(rows) == {"acme", "beta"}
    acme = rows["acme"]
    assert acme["can_open"] and acme["page"] == "/workspaces/acme" and acme["domain"] == "acme.example.com"
    assert [i["server"] for i in acme["instances"]] == ["srv"] and rows["beta"]["instances"] == []
    # its members, the admins first, each with its two-step verification (D4: the admin without it counted apart)
    assert acme["members_list"] == [
        {"name": "mario", "role": "admin", "custom": "", "limited": None, "two_step": False},
        {"name": "luca", "role": "user", "custom": "", "limited": None, "two_step": False}]
    assert acme["admins_without_two_step"] == 1 and rows["beta"]["admins_without_two_step"] == 0
    # what is to look at: the map's own items on its nodes, the same number (A14.6); the admin's not in them (D4)
    watch = (await client.get("/api/map", headers={"Authorization": AUTH})).json()["verdict"]["watch"]
    for name, row in rows.items():
        ids = {row["id"], *(i["id"] for i in row["instances"])}
        mapped = [w for w in watch if w.get("node") in ids or set(w.get("nodes") or ()) & ids]
        assert len(row["watch"]) == len(mapped), name
        assert not any("two-step" in w["text"] for w in row["watch"])
    # New workspace: this machine has the disk of the workspaces (the default storage)
    assert data["creatable"]["ok"] is True
    settings.storage_roots = {}
    data = (await client.get("/api/workspaces/list", headers={"Authorization": AUTH})).json()
    assert data["creatable"] == {"ok": False, "why": "this machine has no disk for the workspaces"}
    # the machine's developer and user: no entry, no list (P5)
    for user in ("dev", "viewer"):
        assert ("Workspaces", "/workspaces") not in nav((await client.get("/instances", headers=basic(user))).text)
        assert (await client.get("/api/workspaces/list", headers=basic(user))).status_code == 403
        assert (await client.get("/workspaces", headers=basic(user))).status_code == 403


async def test_a_person_of_workspaces(workspaces, client, env):  # noqa: F811
    settings = env["settings"]
    # mario, admin of acme only (P4): the entry named acme, as before, no list
    page = (await client.get("/instances", headers=basic("mario"))).text
    assert ("acme", "/workspaces/acme") in nav(page) and ("Workspaces", "/workspaces") not in nav(page)
    assert (await client.get("/workspaces", headers=basic("mario"))).status_code == 403
    assert (await client.get("/api/workspaces/list", headers=basic("mario"))).status_code == 403
    # its crumbs: Workspaces, not a link (it has no list), then acme
    crumbs = (await client.get("/workspaces/acme", headers=basic("mario"))).text.split('class="crumbs"', 1)[1]
    assert 'href="/workspaces"' not in crumbs.split("</nav>", 1)[0]
    # admin of beta too (P3): one entry Workspaces instead of two, its two workspaces, no machine's name
    settings.workspaces["beta"].members["mario"] = "admin"
    settings.workspaces["gamma"] = Workspace("gamma", members={"eva": "admin"})
    page = (await client.get("/workspaces", headers=basic("mario"))).text
    entries = nav(page)
    assert ("Workspaces", "/workspaces") in entries and ("acme", "/workspaces/acme") not in entries
    assert "beta" not in [t for t, _ in entries]
    # no primary, no form, no templates, nothing of the machine
    head = page.split("</header>", 1)[1].split("<dialog", 1)[0]
    assert 'class="primary"' not in head and 'id="templates"' not in page and "/var/" not in page
    data = (await client.get("/api/workspaces/list", headers=basic("mario"))).json()
    assert sorted(w["name"] for w in data["workspaces"]) == ["acme", "beta"]
    assert data["name"] == "" and data["machine_name"] is None and data["links"] == [] and data["loose"] is None
    text = json.dumps(data)
    for never in ("gamma", "omnia", "/var/", "/srv/", appmodule.machines.here_name(socket.gethostname())):
        assert never not in text, never
    assert data["creatable"]["ok"] is False
    # another workspace in the URL: refused (its page is its admins')
    assert (await client.get("/workspaces/gamma", headers=basic("mario"))).status_code in (403, 404)
    # no form of the machine
    assert (await client.get("/workspaces?new=1", headers=basic("mario"))).status_code == 403


@pytest.fixture
async def central(env, tmp_path, workspaces):  # noqa: F811
    """A panel linked to the customer's machine of env (its link omnia a developer there: P2b), to a machine
    that does not answer, and to one whose OmniaCLI is older (no list of the workspaces)."""
    customer = create_app(env["settings"])
    conf = tmp_path / "central"
    conf.mkdir()
    (conf / "links.yaml").write_text(yaml.safe_dump({"links": {
        "cliente": {"url": "http://customer.test", "token": LINK_TOKEN},
        "down": {"url": "https://down.test", "token": "whatever"},
        "old": {"url": "http://old.test", "token": LINK_TOKEN},
    }}))
    settings = parse_settings({
        "auth": {"users": {"admin": USER_HASH, "dev": {"password": USER_HASH, "role": "developer"}},
                 "tokens": {"tester": hash_token(TOKEN)}},
        "backup_dir": str(tmp_path / "c-backups"), "jobs_dir": str(tmp_path / "c-jobs"), "servers_file": "servers.yaml",
    }, base_dir=conf)

    class Older(Network):
        async def handle_async_request(self, request):
            if request.url.host == "old.test":
                if request.url.path == "/api/workspaces/list":
                    return httpx.Response(404, json={"detail": "Not Found"}, request=request)
                request.url = request.url.copy_with(host="customer.test")
            return await super().handle_async_request(request)

    network = Older(customer)
    app = create_app(settings, link_transport=network)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=BasicAsSession(app)), base_url=f"http://{HOST}") as c:
        c.app, c.customer, c.network = app, customer, network
        yield c


async def test_the_workspaces_of_every_machine(central):
    page = (await central.get("/workspaces", headers=as_user("admin"))).text
    # with links: the Machine column, every link asked (A14.2)
    assert ">Machine</th>" in page and '["cliente", "down", "old"]' in page.replace("'", '"')
    here = (await central.get("/api/workspaces/list", headers=as_user("admin"))).json()
    assert here["workspaces"] == [] and here["links"] == ["cliente", "down", "old"]
    # a link: its rows, its pages under the link; the link is a developer there (P2b): rows that read only
    there = (await central.get("/api/workspaces/list?link=cliente", headers=as_user("admin"))).json()
    assert there["name"] == "cliente" and there["viewer_role"] == "developer"
    acme = next(w for w in there["workspaces"] if w["name"] == "acme")
    assert acme["page"] == "/links/cliente/workspaces/acme" and acme["can_open"] is False
    assert acme["members"] is None and acme["members_list"] is None
    assert [i["page"] for i in acme["instances"]] == ["/links/cliente/servers/srv"]
    assert there["creatable"]["ok"] is False
    # a machine that does not answer: a row with the reason, at once (A14.3)
    down = (await central.get("/api/workspaces/list?link=down", headers=as_user("admin"))).json()
    assert down["link"] == "down" and down["error"] and down["older"] is False and "workspaces" not in down
    # an older OmniaCLI: to update, not down
    old = (await central.get("/api/workspaces/list?link=old", headers=as_user("admin"))).json()
    assert old["older"] is True and "workspaces" not in old
    assert (await central.get("/api/workspaces/list?link=nope", headers=as_user("admin"))).status_code == 404
    # the linked machine asked with the reader's name and the menu's place: its pages keep the viewer's menu
    page = (await central.get("/links/cliente/servers/srv", headers=as_user("admin"))).text
    entries = nav(page)
    assert [t for t, _ in entries] == ["Servers", "Workspaces", "Instances", "Backups", "Settings"]
    assert all(not h.startswith("/links/") for _, h in entries)
    # the developer of the central: no list
    assert (await central.get("/api/workspaces/list", headers=as_user("dev"))).status_code == 403


async def test_log_in_as_row_by_row(workspaces, client, env, monkeypatch):  # noqa: F811
    settings = env["settings"]
    prod, test = settings.servers["srv"].production, settings.servers["srv"].test
    prod.domain = "https://acme.example.com"
    rows = (await client.get("/api/servers", headers={"Authorization": AUTH})).json()
    envs = rows[0]["instances"]
    # the machine's admin: its production with a name over https opens; the test without one is off, saying why
    offer = envs["production"]["login_as"]
    assert offer["url"] == "/api/servers/srv/production/login-as" and offer["users"].endswith("/login-as/users")
    assert offer["label"] == "acme.example.com" and "acme.example.com" in offer["title"]
    assert "HTTPS" in envs["test"]["login_as"]["disabled"]
    # a developer: none on the production (an admin's), the test's only
    dev = (await client.get("/api/servers", headers=basic("dev"))).json()[0]["instances"]
    assert dev["production"]["login_as"] is None and dev["test"]["login_as"] is not None
    # a user of the machine: none
    viewer = (await client.get("/api/servers", headers=basic("viewer"))).json()[0]["instances"]
    assert viewer["production"]["login_as"] is None and viewer["test"]["login_as"] is None
    # a token: no field at all
    tokens = (await client.get("/api/servers", headers={"Authorization": f"Bearer {TOKEN}"})).json()[0]["instances"]
    assert "login_as" not in tokens["production"] and "login_as" not in tokens["test"]
    # a workspace's admin (role admin there) and user (none)
    mario = (await client.get("/api/servers", headers=basic("mario"))).json()[0]["instances"]
    assert mario["production"]["login_as"]["url"] == "/api/servers/srv/production/login-as"
    luca = (await client.get("/api/servers", headers=basic("luca"))).json()[0]["instances"]
    assert luca["production"]["login_as"] is None
    # a custom role without login_as: off, with the reason of the route
    ws = settings.workspaces["acme"]
    ws.roles["support"] = ("run", "logs")
    ws.member_roles["mario"] = "support"
    settings.workspaces["acme"] = ws
    mario = (await client.get("/api/servers", headers=basic("mario"))).json()[0]["instances"]
    assert mario["production"]["login_as"]["disabled"] and "url" not in mario["production"]["login_as"]
    # the list of the workspaces carries the same decision on each production
    del ws.member_roles["mario"]
    row = (await client.get("/api/workspaces/list", headers={"Authorization": AUTH})).json()["workspaces"]
    acme = next(w for w in row if w["name"] == "acme")
    assert acme["instances"][0]["envs"]["production"]["login_as"]["url"] == "/api/servers/srv/production/login-as"
    # an environment stopped: off, saying so (B3)
    async def stopped(instance):
        return "inactive" if instance.role == "production" else "active"
    monkeypatch.setattr(ops, "service_state", stopped)
    envs = (await client.get("/api/servers", headers={"Authorization": AUTH})).json()[0]["instances"]
    assert envs["production"]["state"] == "stopped" and "is stopped" in envs["production"]["login_as"]["disabled"]
    monkeypatch.undo()
    # the rows of Environments: Log in as… between Open and Addresses…, the dialog drawn once
    page = (await client.get("/servers/srv", headers={"Authorization": AUTH})).text
    table = page.split('<table class="copies">', 1)[1].split("</table>", 1)[0]
    assert table.count("data-login-as=") == 2 and page.count('id="login-as-dialog"') == 1
    # the Instances list: the cell of the two actions, hidden on a phone, the dialog there
    page = (await client.get("/instances", headers={"Authorization": AUTH})).text
    assert 'id="login-as-dialog"' in page and "function actionsCell" in page and 'class="hide-sm"></th>' in page


async def test_the_state_of_each_repository(workspaces, client, env):  # noqa: F811
    """The Code table of the Modules tab (Carlo, 23/11/2026): a State per repository, from the pull dialog's
    route, read when the tab opens: up to date, the commits to bring with the first one's subject, not read."""
    from .conftest import clone, commit, sh

    work = clone(env["remotes"]["addons"], env["tmp"] / "dev_addons_state", "main")
    commit(work, "one.txt", "1\n", "first of two")
    commit(work, "two.txt", "2\n", "second of two")
    sh("git", "push", "-q", "origin", "HEAD:main", cwd=work)
    url = "/api/servers/srv/production/pull-preview"
    rows = {r["name"]: r for r in (await client.get(url, headers={"Authorization": AUTH})).json()["rows"]}
    assert (rows["addons"]["pending"], rows["addons"]["first"]) == (2, "first of two")
    assert (rows["odoo"]["pending"], rows["odoo"]["first"]) == (0, "")
    assert all(r["checked"][:4].isdigit() and "T" in r["checked"] for r in rows.values())
    # a remote that cannot be fetched: not read, with the reason
    addons = env["settings"].servers["srv"].production.repositories[1].path
    sh("git", "remote", "set-url", "origin", str(env["tmp"] / "gone"), cwd=addons)
    rows = {r["name"]: r for r in (await client.get(url, headers={"Authorization": AUTH})).json()["rows"]}
    assert rows["addons"]["pending"] is None and "git fetch failed" in rows["addons"]["error"]
    # the column of the table (an instance of a workspace): tests/test_workspace_repositories.py
