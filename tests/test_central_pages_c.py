# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""AD-017 slice 5: the other pages of class C drawn by the panel from the machine's models, agent_api 4: the
machine's section with its Database and SSH access tabs (index, and the table of instances its script asks again),
a workspace's page, Users, Pullers, a server's form (new, edit, clone) and Options. Their models carry none of the
secrets these pages are about (by value), their names and addresses stay on the machine, the model is of the page of
the path, and the page drawn here equals the machine's own for every role."""
import base64
import dataclasses
import re

import httpx

from omniacli import centralpages
from omniacli.config import parse_connection, parse_publication, parse_puller

from .conftest import LINK_TOKEN, USER_HASH
from .serverparity import Twice, main_of, secret_keys
from .test_central_pages import (DEST_KEY, LINK, PAGE, B_SECRETS, answer_b, as_, central, class_b,  # noqa: F401
                                 link_pages, own_b, pair)
from .test_delegated_scope import as_user

# a page of each of the 7 of slice 5, as the browser opens it under /links/<name>/
C_PATHS = {"index": "", "dashboard_servers": "dashboard/servers", "workspace": "workspaces/acme", "users": "users",
           "pullers": "pullers", "server_form": "config/srv/edit", "options": "options"}
# their other forms: the search of the Dashboard, a new server's form and a clone's, a workspace not there
C_MORE = ("?q=srv", "dashboard/servers?q=acme", "config/new", "config/srv/clone", "workspaces/nope", "config/nope/edit")
# what the machine of class_c holds that no model may carry: an SSO provider's secret, a TOTP secret and its
# recovery codes, a passkey's key, a session, a token of a workspace, a webhook's path and its signing secret, a
# puller's keys, the SMTP password (class_b's)
TOTP_SECRET = "JBSWY3DPEHPK3PXPC915"
C_SECRETS = ("SsoSecret-915", TOTP_SECRET, "PasskeyPub915", "MonHook915", "WhSecret-915-abcdefghijklm", "WsTokHash915",
             "/services/T0/B0/Hook914")


async def class_c(central, monkeypatch) -> dict:
    """The machine of class_b with what the other pages of class C show: acme with custom roles and their people,
    an SSO provider (its secret), a token of the workspace, a second factor and a passkey of a person, a session; a
    puller, a publication and a connection with their keys; Options with SMTP, a webhook of Monitoring signed (its
    secret), its brand. {job, own, recovery: the codes of the second factor}."""
    ids = await class_b(central, monkeypatch)
    machine = central.machine.asgi.app
    settings = machine.state.settings
    ws = settings.workspaces["acme"]
    settings.workspaces = {"acme": dataclasses.replace(
        ws, members={**ws.members, "admin": "admin"}, roles={"copy of admin": ["people", "logs"], "no logs": ["people"]},
        member_roles={"upload": "no logs"}, domain="acme.example.com")}
    web = settings.web
    settings.set_web_access(dataclasses.replace(
        web, tokens={**web.tokens, "ci-acme": "sha256$WsTokHash915"}, token_roles={**web.token_roles, "ci-acme": "developer"},
        token_workspaces={**web.token_workspaces, "ci-acme": "acme"}, token_ci={**web.token_ci, "ci-acme": True}))
    machine.state.sso.set_provider("acme", {"issuer": "https://login.example.com", "client_id": "omniacli-acme",
                                            "client_secret": "SsoSecret-915"}, "admin")
    recovery = machine.state.second_factors.enable("backups", TOTP_SECRET, 1)
    machine.state.passkeys.add("upload", b"handle-915", "customer.test", b"cred-915", b"PasskeyPub915", 0, "key 915",
                               [], False)
    session = machine.state.sessions.create("backups", USER_HASH)
    settings.pullers = {"box": parse_puller("box", {"producer": "omnia", "consumers": ["vps"]})}
    settings.publications = {"srv/test": parse_publication("srv/test", {"link": "omnia", "puller": "box",
                                                                        "key": DEST_KEY})}
    settings.connections = {"api-prod": parse_connection("api-prod", {"link": "far", "puller": "api-prod",
                                                                      "key": DEST_KEY})}
    hook = "https://hooks.example.com/services/T1/B1/MonHook915"
    monkeypatch.setattr(settings, "options", dataclasses.replace(
        settings.options, monitor_notify=["ops@example.com", hook], brand_name="ACME Cloud",
        webhook_keys={hook: {"secret": "WhSecret-915-abcdefghijklm", "jobs": True}}, portal_address="portal.example.com"))
    return {**ids, "recovery": recovery, "session": session}


def c_paths() -> list[str]:
    return [*C_PATHS.values(), *C_MORE]


def name_of(path: str) -> str:
    return centralpages.match(path.split("?")[0])[0].name


async def own_c(central, page: str, params: dict | None = None) -> dict:
    """The model the machine answers for page (its parameters by name: a server's form has a name of its own)."""
    async with httpx.AsyncClient(transport=central.machine.asgi, base_url="http://customer.test") as machine:
        return (await machine.get(f"/api/pages/{page}", params=params or {}, headers=LINK)).json()


def test_the_pages_of_slice_5_are_found_with_their_parameters():
    assert {name_of(p) for p in C_PATHS.values()} == set(C_PATHS) == {p.name for p in centralpages.CLASS_C}
    assert all(p.needs_agent_api == 4 for p in centralpages.CLASS_C)
    assert centralpages.match("")[1] == {} and centralpages.match("workspaces/acme")[1] == {"workspace": "acme"}
    assert centralpages.match("config/new")[1] == {}
    assert centralpages.match("config/srv/clone")[1] == {"name": "srv", "form": "clone"}
    for other in ("workspaces/acme/x", "config/srv", "config/srv/delete", "dashboard", "users/x", "options/x"):
        assert centralpages.match(other) is None, other


async def test_every_page_of_slice_5_drawn_here_from_its_model(central, monkeypatch):
    """agent_api 4: each is drawn here from /api/pages/<page>, with the status the machine's own page has; never the
    machine's page."""
    await class_c(central, monkeypatch)
    client = central.app.state.link_clients.client(central.app.state.settings.link_targets["cliente"])
    for path in c_paths():
        own = await client.get(f"/{path}", headers={**LINK, "Accept": "text/html"})
        central.machine.asked.clear()
        resp = await central.get(f"/links/cliente/{path}", headers=PAGE)
        assert resp.status_code == own.status_code, (path, resp.text[:300])
        assert f"/api/pages/{name_of(path)}" in central.machine.asked, path
        assert f"/{path.split('?')[0]}" not in central.machine.asked, path
        assert "data-machine-page-line" not in resp.text, path
        if resp.status_code == 200 and name_of(path) != "dashboard_servers":
            assert "/links/cliente/" in resp.text, path


async def test_the_models_of_slice_5_carry_no_secret_value(central, monkeypatch):
    """AD-017 §8.1 for the 7: no secret's name, no secret's value: the SMTP password, the SSO provider's secret, the
    TOTP secret and its recovery codes, a passkey, a session, a password's hash, a token of the workspace and of a
    link, a webhook's path and its signing secret, a puller's keys, a destination's. Of each the page says whether it
    is set."""
    ids = await class_c(central, monkeypatch)
    found = [*B_SECRETS, *C_SECRETS, *ids["recovery"], USER_HASH, LINK_TOKEN, DEST_KEY, ids["session"],
             base64.urlsafe_b64encode(b"PasskeyPub915").decode().rstrip("="), base64.b64encode(b"PasskeyPub915").decode()]
    async with httpx.AsyncClient(transport=central.machine.asgi, base_url="http://customer.test") as machine:
        for path in c_paths():
            name = name_of(path)
            params = {**centralpages.match(path.split("?")[0])[1], **dict(
                p.split("=", 1) for p in path.partition("?")[2].split("&") if p)}
            resp = await machine.get(f"/api/pages/{name}", params=params, headers=LINK)
            if resp.status_code != 200:
                assert "context" not in resp.json(), path
                continue
            data = resp.json()
            assert data["agent_api"] == 4 and set(data["context"]) <= set(centralpages.BY_NAME[name].fields), path
            for secret in found:
                assert secret not in resp.text, (path, secret)
            assert not secret_keys(data["context"]), (path, secret_keys(data["context"]))
        # what they say of them: set or not, a webhook by its site and its ref
        model = (await machine.get("/api/pages/options", headers=LINK)).json()["context"]
        assert model["options"]["smtp_password_set"] is True and "smtp_password" not in model["options"]
        assert model["options"]["monitor_notify"] == ["ops@example.com", "https://hooks.example.com/…"]
        assert [{k: h[k] for k in ("shown", "signed", "jobs")} for h in model["webhooks"]] == [
            {"shown": "https://hooks.example.com/…", "signed": True, "jobs": True},
            {"shown": "https://hooks.example.com/…", "signed": False, "jobs": False}]
        model = (await machine.get("/api/pages/workspace", params={"workspace": "acme"}, headers=LINK)).json()["context"]
        assert model["sso"]["secret_set"] is True and "client_secret" not in model["sso"]
        assert model["ws"]["notify"] == ["https://hooks.example.com/…"]
        assert [t["name"] for t in model["tokens"]] == ["ci-acme"]
        model = (await machine.get("/api/pages/pullers", headers=LINK)).json()["context"]
        assert model["publications"][0]["has_key"] is True and "key" not in model["publications"][0]
        assert model["connections"][0]["has_key"] is True and model["connections"][0]["key"] == ""
        model = (await machine.get("/api/pages/users", headers=LINK)).json()["context"]
        backups = next(u for u in model["users"] if u["name"] == "backups")
        assert backups["two_step"] is True and backups["sessions"] == 1


async def test_the_pages_of_slice_5_have_the_controls_of_the_machines(central, monkeypatch):
    """AD-017 §8.3 for the 7: the whole <main> drawn here equals the machine's own, for admin, developer and user
    through the link; and on the machine every page it draws equals the one drawn from its model."""
    await class_c(central, monkeypatch)
    twice = Twice(monkeypatch)
    for user in ("admin", "dev", "viewer"):
        for path in c_paths():
            here = await central.get(f"/links/cliente/{path}", headers=as_(user))
            link_pages(central, "machine")
            theirs = await central.get(f"/links/cliente/{path}", headers=as_(user))
            link_pages(central, "here")
            assert here.status_code == theirs.status_code, (user, path, here.text[:300])
            if here.status_code == 200:
                assert main_of(here.text) == main_of(theirs.text), (user, path)
    compared = {(c["page"], c["role"]) for c in twice.compared}
    assert {p for p, _ in compared} >= set(C_PATHS)
    assert {r for p, r in compared if p == "index"} == {"admin", "developer", "user"}
    assert {r for p, r in compared if p == "options"} == {"admin"}


async def test_the_parity_of_slice_5_for_the_people_of_a_workspace(pair, monkeypatch):
    """The people of a workspace (its admin, user, developer, a copy of admin and a custom role without the logs) on
    the Dashboard and the workspace's page, on their home and once srv is placed on its host: the page drawn from the
    model equals the page drawn from the live objects, every time."""
    twice = Twice(monkeypatch)
    people = ("mario", "as-user", "as-developer", "as-admin", "copy-admin", "no-logs")
    paths = ("/", "/dashboard/servers", "/workspaces/acme")
    answered = {}
    for placed in (False, True):
        if placed:
            pair.place()
        for person in people:
            for path in paths:
                resp = await pair.get(path, headers=as_user(person), follow_redirects=True)
                answered.setdefault((person, path), set()).add(resp.status_code)
    assert all(len(codes) == 1 for codes in answered.values()), answered
    drawn = {(c["user"], c["page"]) for c in twice.compared}
    for person in people:
        assert {(person, "index"), (person, "dashboard_servers")} <= drawn, person
    assert {("mario", "workspace"), ("copy-admin", "workspace"), ("no-logs", "workspace")} <= drawn
    assert answered[("as-user", "/workspaces/acme")] == {403}


async def test_a_model_of_slice_5_that_leaves_the_machine_is_refused(central, monkeypatch):
    """A name the page puts in an address that is not one (a row of an instance, in the list or in a group, a
    workspace's person, a puller's connection, a server's form's link), an address to elsewhere that is not http(s), a
    logo that is not the machine's, a model of another workspace or server than the path's: the whole model refused,
    this panel's error page."""
    await class_c(central, monkeypatch)
    cases = []

    def case(page, path, change, **params):
        cases.append((page, path, change, params))
    case("index", "", lambda c: c["rows"][0].update(name="../../options"))
    case("index", "", lambda c: c["sections"][0]["items"][0].update(parent="../x"))
    case("index", "", lambda c: c["jobs"].append({"id": "../../api/links/delete", "title": "x", "status": "success"}))
    case("dashboard_servers", "dashboard/servers", lambda c: c["rows"][0].update(name="a b"))
    case("workspace", "workspaces/acme", lambda c: c["ws"].update(name="other"), workspace="acme")
    case("workspace", "workspaces/acme", lambda c: c["members"][0].update(name="../../links/x"), workspace="acme")
    case("workspace", "workspaces/acme", lambda c: c.update(db_links="far"), workspace="acme")
    case("users", "users", lambda c: c.update(portal_url="javascript:alert(1)"))
    case("users", "users", lambda c: c["users"][0].update(workspaces=["../../x"]))
    case("pullers", "pullers", lambda c: c["connections"][0].update(name="../../options"))
    case("server_form", "config/srv/edit", lambda c: c["model"].update(original="other", name="other"),
         name="srv", form="edit")
    case("server_form", "config/srv/edit", lambda c: c.update(title="Edit other"), name="srv", form="edit")
    case("server_form", "config/new", lambda c: c["model"].update(links=["../x"]))
    case("options", "options", lambda c: c.update(machine_logo="//evil.test/x.png"))
    case("options", "options", lambda c: c.update(portal_url="//evil.test"))
    for name, path, change, params in cases:
        data = await own_c(central, name, params)
        change(data["context"])
        answer_b(central, name, data)
        resp = await central.get(f"/links/cliente/{path}", headers=PAGE)
        assert resp.status_code == 502 and "is not the one this panel reads" in resp.text, (name, path, resp.text[:300])
        assert "../../" not in resp.text and "evil.test" not in resp.text and "javascript:" not in resp.text, name
        del central.machine.answer[f"/api/pages/{name}"]


async def test_the_dashboards_rows_and_machines_are_never_the_machines_word(central, monkeypatch):
    """A row of an instance opens under the link (its base and its link this panel's, never the model's: another
    machine's, this panel's own pages); Linked is this panel's own, never a machine's list of links."""
    await class_c(central, monkeypatch)
    for name, path in (("index", ""), ("dashboard_servers", "dashboard/servers")):
        data = await own_b(central, name)
        for row in data["context"]["rows"]:
            row.update(base="/links/other", link="other")
        if name == "index":
            data["context"].update(machine=True, machine_links=["../../evil"], linked_sections=[
                {"name": "", "items": [{"name": "../../evil", "url": "https://evil.test"}], "groups": [], "count": 1}])
        answer_b(central, name, data)
        resp = await central.get(f"/links/cliente/{path}", headers=PAGE)
        assert resp.status_code == 200 and 'data-href="/links/cliente/servers/srv"' in resp.text, path
        assert "/links/other" not in resp.text and 'data-link="other"' not in resp.text, path
        assert "../../evil" not in resp.text and "evil.test" not in resp.text, path
        del central.machine.answer[f"/api/pages/{name}"]


async def test_a_servers_form_offers_this_panels_plugins(central, monkeypatch):
    """The plugins a server's form offers (their fields, the buttons of a field and the routes its script fetches
    and opens) are this panel's, as the contract wants them the same: never a route of the machine's word."""
    await class_c(central, monkeypatch)
    data = await own_c(central, "server_form", {"name": "srv", "form": "edit"})
    data["context"]["model"]["types"]["odoo"]["fields"].append({
        "key": "x", "label": "X", "kind": "text", "buttons": [{"label": "Go", "route": "../../../api/links/delete"}]})
    data["context"]["model"]["types"]["evil"] = {"label": "Evil", "fields": []}
    answer_b(central, "server_form", data)
    resp = await central.get("/links/cliente/config/srv/edit", headers=PAGE)
    assert resp.status_code == 200 and "api/links/delete" not in resp.text and '"evil"' not in resp.text
    assert all(f"\"{name}\": {{\"fields\"" in resp.text for name in centralpages.plugin_types())


async def test_the_notices_shown_by_their_site_are_kept(central, monkeypatch):
    """A workspace's notices and Options › Monitoring show a webhook by its site only (its secret is in its path), and
    send that back at saving: the saved webhook it stands for is kept, in its place; a site that stands for none is
    refused; a webhook of Options is named by its ref."""
    await class_c(central, monkeypatch)
    settings = central.machine.asgi.app.state.settings
    headers = {**LINK, "Origin": "http://customer.test"}
    async with httpx.AsyncClient(transport=central.machine.asgi, base_url="http://customer.test") as machine:
        page = (await machine.get("/workspaces/acme", headers={**headers, "Accept": "text/html"})).text
        assert "Hook914" not in page and "https://hooks.example.com/…" in page
        resp = await machine.post("/api/workspaces/acme/notify", headers=headers, json={
            "notify": ["mario@example.com", "https://hooks.example.com/…"]})
        assert resp.status_code == 200, resp.text
        assert settings.workspaces["acme"].notify == ["mario@example.com",
                                                      "https://hooks.example.com/services/T0/B0/Hook914"]
        resp = await machine.post("/api/workspaces/acme/notify", headers=headers, json={
            "notify": ["https://other.example.com/…"]})
        assert resp.status_code == 400 and "write its whole address" in resp.text
        # Options › Monitoring: the same, and a webhook named by its ref
        page = (await machine.get("/options", headers={**headers, "Accept": "text/html"})).text
        assert "MonHook915" not in page and 'value="ops@example.com, https://hooks.example.com/…"' in page
        resp = await machine.post("/api/options/save", headers=headers, json={
            "monitor": {"notify": ["https://hooks.example.com/…", "ops@example.com"]}})
        assert resp.status_code == 200, resp.text
        assert settings.options.monitor_notify == ["https://hooks.example.com/services/T1/B1/MonHook915",
                                                   "ops@example.com"]
        assert settings.options.webhook_keys["https://hooks.example.com/services/T1/B1/MonHook915"]["secret"] == \
            "WhSecret-915-abcdefghijklm"
        ref = re.findall(r'data-ref="([0-9a-f]{20})"', page)[0]
        resp = await machine.post("/api/options/webhooks/save", headers=headers, json={"ref": ref, "jobs": False})
        assert resp.status_code == 200 and "MonHook915" not in resp.text
        assert not settings.options.webhook_keys["https://hooks.example.com/services/T1/B1/MonHook915"]["jobs"]
        resp = await machine.post("/api/options/webhooks/save", headers=headers, json={"ref": "0" * 20, "jobs": True})
        assert resp.status_code == 404


async def test_the_machines_version_on_its_section(central, monkeypatch):
    """The machine's section says the OmniaCLI of the machine (machine_version of its model), not of the panel that
    draws it."""
    await class_c(central, monkeypatch)
    data = await own_b(central, "index")
    data["context"]["machine_version"] = "2026.1.9999"
    answer_b(central, "index", data)
    resp = await central.get("/links/cliente/", headers=PAGE)
    assert resp.status_code == 200 and "OmniaCLI 2026.1.9999" in resp.text


async def test_every_read_of_a_model_of_slice_5_is_in_the_machines_access_log(central, monkeypatch):
    await class_c(central, monkeypatch)
    for path in ("", "users", "workspaces/acme", "options"):
        assert (await central.get(f"/links/cliente/{path}", headers=as_("dev"))).status_code in (200, 403), path
    reads = [e for e in central.machine.asgi.app.state.access_log.recent(200)
             if e.get("event") == "read" and str(e.get("what", "")).startswith("model of the page ")]
    assert {e["what"] for e in reads} >= {"model of the page index", "model of the page users",
                                          "model of the page workspace", "model of the page options"}
    assert all(e["kind"] == "link" and e["on_behalf"] == "dev" for e in reads), reads


async def test_a_machine_of_slice_4_has_its_section_with_the_line_and_its_table_without(central, monkeypatch):
    """A machine at 3 (2026.1.5518): its section is its own with the line; the table of instances its script asks
    again is its own too, without the line (found on lab4: the line went inside the table at every job)."""
    await class_c(central, monkeypatch)
    central.machine.info = {"agent_api": 3, "plugins": {}, "version": "2026.1.5518"}
    page = await central.get("/links/cliente/", headers=PAGE)
    part = await central.get("/links/cliente/dashboard/servers", headers=PAGE)
    assert page.status_code == part.status_code == 200
    assert "data-machine-page-line" in page.text and "/" in central.machine.asked
    assert "data-machine-page-line" not in part.text and "data-server=" in part.text
