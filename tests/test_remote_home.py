# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""AD-018 slice 2, «Il centrale casa» (§18.2): a machine (lab4) with two workspaces whose identities the central panel
(lab3) imported (slice 1). Switched on per link (off by default), the people of those workspaces log in on the central
with the same password and the same app; every route of WORKSPACE_ROUTES answers through the central as it answers
on the machine (the parity of constraint 3), the pages drawn on the central from the machine's models; a person of one
workspace reaches nothing of the other; a workspace's token calls the central's REST API and MCP and the machine
records the request with on_behalf; both access logs say who, which workspace, which session or token; a machine
answers data only; a workspace named like one of the central's is never served."""

import asyncio
import base64
import dataclasses
import json
import re
import time

import httpx
import pytest
import yaml

from omniacli import capabilities, centralpages, jobs as jobs_module, placement, remotehome, totp
from omniacli.app import REMOTE_OWN_ROUTES, create_app
from omniacli.auth import hash_password
from omniacli.config import Access, LinkAccess, parse_servers, parse_settings, parse_workspaces, server_to_dict

from .conftest import PASSWORD, USER_HASH, BasicAsSession, hash_token
from .serverparity import main_of, same_controls
from .test_capabilities import VALUES, workspace_routes
from .test_delegated_scope import running
from .test_mcp import HEADERS as MCP_HEADERS, call

CENTRAL_LINK_TOKEN = "central-admin-link-token-0123456789"
HOST = "central.test"
ORIGIN = f"http://{HOST}"
ADMIN = {"Authorization": "Basic " + base64.b64encode(f"admin:{PASSWORD}".encode()).decode(),
         "Accept": "application/json", "Origin": ORIGIN}
SECRET = "jbswy3dpehpk3pxpjbswy3dpehpk3pxp"
ACME_TOKEN = "acme-token-of-bruno-0123456789abcdef"
BETA_TOKEN = "beta-token-of-dario-0123456789abcdef"
# who reaches acme: its admin (with the app), a developer, a user, a custom role without the logs; dario is beta's
PEOPLE = {"anna": "admin", "bruno": "developer", "elena": "user", "carla": "admin"}
CUSTOM = {"carla": "no-logs"}


class Machine(httpx.AsyncBaseTransport):
    """lab4 behind the central's link: what it was asked, with which headers; down: it does not answer."""

    def __init__(self, app):
        self.asgi = httpx.ASGITransport(app=app)
        self.asked: list[tuple[str, str, dict]] = []
        self.down = False
        # path: a function of the request that makes the answer instead (a machine that lies)
        self.answer: dict = {}

    async def handle_async_request(self, request):
        self.asked.append((request.method, request.url.path, dict(request.headers)))
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        if request.url.path in self.answer:
            found = self.answer[request.url.path](request)
            return await found if asyncio.iscoroutine(found) else found
        return await self.asgi.handle_async_request(request)


@pytest.fixture
async def home(env, tmp_path, monkeypatch):
    """lab4 (acme with srv: anna, bruno, elena, carla; beta with bsrv: dario; a token of each) and lab3 (the central,
    with its admin), linked; the identities imported on lab3, its switch off."""
    m = env["settings"]
    password = hash_password(PASSWORD, iterations=1000)
    m.servers["bsrv"] = parse_servers({"bsrv": server_to_dict(m.servers["srv"])}, m.logs_dir)["bsrv"]
    m.set_web_access(Access(
        users={**{n: password for n in [*PEOPLE, "dario"]}, "staff": USER_HASH},
        roles={**{n: "workspace" for n in [*PEOPLE, "dario"]}, "staff": "admin"},
        tokens={"t-bruno": hash_token(ACME_TOKEN), "t-dario": hash_token(BETA_TOKEN)},
        token_roles={"t-bruno": "developer", "t-dario": "admin"},
        token_workspaces={"t-bruno": "acme", "t-dario": "beta"}))
    m.fixed.links["central"] = LinkAccess(hash_token(CENTRAL_LINK_TOKEN), "admin")
    m.set_web_access(m.web)
    m.workspaces = parse_workspaces({
        "acme": {"label": "ACME", "servers": ["srv"], "members": {**PEOPLE, "staff": "admin"},
                 "roles": {"no-logs": sorted(capabilities.PRESETS["admin"] - {"logs"})}, "member_roles": CUSTOM,
                 "token_people": {"t-bruno": "bruno"}},
        "beta": {"label": "Beta", "servers": ["bsrv"], "members": {"dario": "admin"}}})
    machine_app = create_app(m)
    machine_app.state.second_factors.enable("anna", SECRET, -1)
    machine = Machine(machine_app)
    conf = tmp_path / "central"
    conf.mkdir()
    (conf / "links.yaml").write_text(yaml.safe_dump({"links": {
        "lab4": {"url": "http://lab4.test", "token": CENTRAL_LINK_TOKEN}}}))
    central = parse_settings({"auth": {"users": {"admin": USER_HASH}}, "backup_dir": str(tmp_path / "cb"),
                              "jobs_dir": str(conf / "jobs"), "servers_file": "servers.yaml"}, base_dir=conf)
    app = create_app(central, link_transport=machine)

    async def nothing(job):
        return None
    real_submit = jobs_module.JobManager.submit

    def submit(self, title, instances, source, action, workspace="", frozen_ok=False):
        return real_submit(self, title, instances, source, nothing, workspace, frozen_ok)
    monkeypatch.setattr(jobs_module.JobManager, "submit", submit)
    async with running(app, machine_app), \
            httpx.AsyncClient(transport=httpx.ASGITransport(app=BasicAsSession(app)), base_url=ORIGIN) as c, \
            httpx.AsyncClient(transport=httpx.ASGITransport(app=BasicAsSession(machine_app)),
                              base_url="http://lab4.test") as on_machine:
        c.app, c.machine, c.machine_app, c.m, c.central, c.on_machine = app, machine, machine_app, m, central, on_machine
        imported = await c.post("/api/identities/lab4/import", headers=ADMIN, json={"confirm": "lab4"})
        assert imported.status_code == 200, imported.text
        yield c


async def switch(c, on: bool) -> httpx.Response:
    return await c.post("/api/identities/lab4/home", headers=ADMIN, json={"confirm": "lab4", "on": on})


async def logged_in(c, name: str, code: str | None = None) -> httpx.AsyncClient:
    """A browser of name on the central, through its login form (and its app's code)."""
    browser = httpx.AsyncClient(transport=httpx.ASGITransport(app=c.app), base_url=ORIGIN)
    resp = await browser.post("/login", data={"username": name, "password": PASSWORD, "next": "/"},
                              headers={"Origin": ORIGIN})
    if code is not None:
        assert resp.status_code == 303 and resp.headers["location"] == "/login/code", resp.text[:300]
        resp = await browser.post("/login/code", data={"code": code}, headers={"Origin": ORIGIN})
    assert resp.status_code == 303 and resp.headers["location"] == "/", (name, resp.status_code, resp.text[:300])
    return browser


def basic(name: str) -> dict:
    return {"Authorization": "Basic " + base64.b64encode(f"{name}:{PASSWORD}".encode()).decode(), "Origin": "http://lab4.test"}


def code_now() -> str:
    return totp.totp(SECRET, time.time())


# --- off by default, and the switch -----------------------------------------------------------------------------

async def test_off_by_default_nobody_logs_in_and_the_machine_is_untouched(home):
    browser = httpx.AsyncClient(transport=httpx.ASGITransport(app=home.app), base_url=ORIGIN)
    resp = await browser.post("/login", data={"username": "elena", "password": PASSWORD}, headers={"Origin": ORIGIN})
    assert resp.status_code == 401
    assert not (home.m.jobs_dir.parent / placement.CENTRAL_HOME).exists()
    status = (await home.get("/api/identities/lab4", headers=ADMIN)).json()
    assert status["home"]["on"] is False and status["home"]["people"] == []


async def test_the_switch_tells_the_machine_and_its_undo_takes_it_back(home):
    resp = await switch(home, True)
    assert resp.status_code == 200, resp.text
    said = resp.json()["home"]
    assert said["on"] and said["workspaces"] == ["acme", "beta"] and said["people"] == sorted([*PEOPLE, "dario"])
    record = json.loads((home.m.jobs_dir.parent / placement.CENTRAL_HOME).read_text())
    assert {w: e["link"] for w, e in record.items()} == {"acme": "central", "beta": "central"}
    assert oct((home.m.jobs_dir.parent / placement.CENTRAL_HOME).stat().st_mode & 0o777) == "0o600"
    homes = home.central.links_file.parent / remotehome.HOMES_FILE
    assert oct(homes.stat().st_mode & 0o777) == "0o600"
    # the people of the machine still log in there (no freeze, no home switched: slice 5)
    assert (await home.on_machine.get("/", headers=basic("elena"))).status_code == 200
    browser = await logged_in(home, "elena")
    assert (await browser.get("/api/servers")).status_code == 200
    # the undo: the machine's record goes, the sessions here end
    assert (await switch(home, False)).status_code == 200
    assert not (home.m.jobs_dir.parent / placement.CENTRAL_HOME).exists()
    assert (await browser.get("/api/servers")).status_code == 401
    # deleting the import while it is on is refused
    await switch(home, True)
    refused = await home.post("/api/identities/lab4/delete", headers=ADMIN, json={"confirm": "lab4"})
    assert refused.status_code == 409


async def test_the_machine_takes_the_scope_of_its_own_workspace_from_the_central_link_only(home):
    await switch(home, True)
    scope = placement.scope_encode({"ws": "acme", "person": "elena", "kind": "user", "role": "user", "grant": None})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=home.machine_app), base_url="http://lab4.test") as m:
        ok = await m.get("/api/servers", headers={placement.SCOPE_HEADER: scope, "Accept": "application/json",
                                                  "Authorization": f"Bearer {CENTRAL_LINK_TOKEN}"})
        assert ok.status_code == 200 and [r["name"] for r in ok.json()] == ["srv"]
        # a scope that carries a workspace whose people the central does not serve: refused whole
        home.machine_app.state.settings.workspaces["gamma"] = dataclasses.replace(
            home.m.workspaces["beta"], name="gamma", servers=[])
        two = placement.scope_encode({"ws": "acme", "person": "elena", "kind": "user", "role": "user", "grant": None,
                                      "also": [{"ws": "gamma", "role": "admin", "grant": None}]})
        refused = await m.get("/api/servers", headers={placement.SCOPE_HEADER: two, "Accept": "application/json",
                                                       "Authorization": f"Bearer {CENTRAL_LINK_TOKEN}"})
        assert refused.status_code == 403
        # the home record is written by the central's admin link only, never through a person's scope
        refused = await m.post("/api/identity/v1/home", json={"workspaces": ["acme"], "on": True}, headers={
            placement.SCOPE_HEADER: scope, "Authorization": f"Bearer {CENTRAL_LINK_TOKEN}"})
        assert refused.status_code == 403


# --- the login: the same password, the same app ---------------------------------------------------------------

async def test_the_same_password_and_the_same_app_on_the_central(home):
    await switch(home, True)
    browser = httpx.AsyncClient(transport=httpx.ASGITransport(app=home.app), base_url=ORIGIN)
    first = await browser.post("/login", data={"username": "anna", "password": PASSWORD}, headers={"Origin": ORIGIN})
    assert first.status_code == 303 and first.headers["location"] == "/login/code"
    # no session before the code
    assert (await browser.get("/api/servers")).status_code == 401
    wrong = await browser.post("/login/code", data={"code": "000000"}, headers={"Origin": ORIGIN})
    assert wrong.status_code == 401
    code = code_now()
    ok = await browser.post("/login/code", data={"code": code}, headers={"Origin": ORIGIN})
    assert ok.status_code == 303
    assert (await browser.get("/api/servers")).status_code == 200
    # the same code a second time: refused
    again = httpx.AsyncClient(transport=httpx.ASGITransport(app=home.app), base_url=ORIGIN)
    await again.post("/login", data={"username": "anna", "password": PASSWORD}, headers={"Origin": ORIGIN})
    assert (await again.post("/login/code", data={"code": code}, headers={"Origin": ORIGIN})).status_code == 401
    # a wrong password: as for anybody
    wrong = await browser.post("/login", data={"username": "elena", "password": "nope"}, headers={"Origin": ORIGIN})
    assert wrong.status_code == 401
    # its account, read only here; a change refused with why
    browser.cookies.set("omniacli_lang", "it")
    page = await browser.get("/account", headers={"Accept": "text/html"})
    assert page.status_code == 200 and "anna" in page.text and "acme" in page.text
    assert "si cambiano dove entravi prima" in page.text and page.text.count('class="primary"') == 1
    refused = await browser.post("/api/account/password", json={"current": PASSWORD, "password": "x" * 12},
                                 headers={"Origin": ORIGIN})
    assert refused.status_code == 409


async def test_a_passkey_only_person_is_told_and_logs_in_where_it_did(home):
    home.machine_app.state.passkeys.add("elena", b"h" * 32, "lab4.example", b"cred", b"key-of-elena", 1, "phone", [], False)
    await home.post("/api/identities/lab4/import", headers=ADMIN, json={"confirm": "lab4"})
    await switch(home, True)
    browser = httpx.AsyncClient(transport=httpx.ASGITransport(app=home.app), base_url=ORIGIN)
    resp = await browser.post("/login", data={"username": "elena", "password": PASSWORD}, headers={"Origin": ORIGIN})
    assert resp.status_code == 403 and "passkey" in resp.text


# --- the parity of constraint 3 -----------------------------------------------------------------------------

def url_of(path: str, values: dict) -> str:
    return re.sub(r"\{(\w+)(?::path)?\}", lambda m: values.get(m.group(1), "x"), path)


PAGE_PATHS = {p for p in centralpages.CUSTOMER_ROUTES.values()}
# the pages of what a request itself adds to (an entry of the access log, a moment of a job): their controls only
LIVE_PAGES = {"/workspaces/{workspace}/audit", "/jobs", "/jobs/{job_id}", "/", "/dashboard/servers"}


async def answer(client, method: str, url: str, path: str, headers: dict):
    accept = "text/html" if method == "GET" and path in PAGE_PATHS else "application/json"
    resp = await client.request(method, url, headers={**headers, "Accept": accept}, json={}, follow_redirects=False)
    return resp


async def test_every_route_of_a_workspace_answers_the_same_through_the_central(home):
    """Constraint 3: every route of WORKSPACE_ROUTES, for admin, developer, user and a custom role, answers the same
    when the person enters from the central as when it enters on the machine; the pages a person opens are drawn on
    the central from the machine's models with the same controls (buttons, disabled ones and why). The only routes
    apart are the person's own account (the central's: its identity lives there) and the pages without a model,
    which the central says are not available instead of passing the machine's HTML (vincolo 8.4)."""
    await switch(home, True)
    routes = sorted(workspace_routes(home.machine_app))
    values = {**VALUES, "server": "srv", "role": "production", "workspace": "acme"}
    job = home.machine_app.state.jobs.submit("Git pull srv/test", ["srv/test"], "manual: anna", None, workspace="acme")
    values["job_id"] = job.id
    browsers = {"anna": await logged_in(home, "anna", code_now()), **{p: await logged_in(home, p)
                                                                      for p in PEOPLE if p != "anna"}}
    own = {p for p in REMOTE_OWN_ROUTES} | {r for _, r in routes if r.startswith("/api/account/")} | {
        "/api/oauth/grants/revoke", "/logout"}
    differ, compared, same_pages, requests = [], 0, 0, 0
    for method, path in routes:
        if path in own:
            continue
        url = url_of(path, values)
        for person in PEOPLE:
            direct = await answer(home.on_machine, method, url, path, basic(person))
            through = await answer(browsers[person], method, url, path, {"Origin": ORIGIN})
            requests += 1
            if direct.status_code != through.status_code:
                differ.append((method, path, person, direct.status_code, through.status_code))
                continue
            if direct.status_code == 200 and "text/html" in direct.headers.get("content-type", "") \
                    and path in PAGE_PATHS:
                # the address its clients use is the panel's it entered (the machine's there, the central's here)
                same_controls(main_of(direct.text).replace("http://lab4.test", "<panel>"),
                              main_of(through.text).replace(ORIGIN, "<panel>"), (path, person),
                              whole=path not in LIVE_PAGES)
                same_pages += 1
            compared += 1
    # what differs is said, and is only what the ADR says may (none so far)
    assert not differ, "\n".join(map(str, differ))
    assert requests > 400 and same_pages >= 10
    print(f"{len(routes)} routes, {requests} requests, {same_pages} pages with the same controls, 0 differences")


async def test_a_person_of_one_workspace_reaches_nothing_of_the_other(home):
    """dario (beta, admin) on every route of acme's names, and elena (acme) on every route of beta's: refused by the
    central, before the machine is asked."""
    await switch(home, True)
    routes = sorted(workspace_routes(home.machine_app))
    browsers = {"dario": await logged_in(home, "dario"), "elena": await logged_in(home, "elena")}
    reached = []
    for person, other in (("dario", {"server": "srv", "workspace": "acme"}), ("elena", {"server": "bsrv",
                                                                                         "workspace": "beta"})):
        values = {**VALUES, "role": "production", **other}
        for method, path in routes:
            if not ("{server}" in path or "{workspace}" in path) or path.startswith("/api/v1/"):
                continue
            before = len(home.machine.asked)
            resp = await answer(browsers[person], method, url_of(path, values), path, {"Origin": ORIGIN})
            asked = [a for a in home.machine.asked[before:] if a[1] != "/api/servers"]
            if resp.status_code not in (403, 404) or asked:
                reached.append((person, method, path, resp.status_code, [a[1] for a in asked]))
    assert not reached, reached
    # the lists: each its own workspace's servers only
    for person, mine in (("dario", ["bsrv"]), ("elena", ["srv"])):
        rows = (await browsers[person].get("/api/servers", headers={"Accept": "application/json"})).json()
        assert sorted(r["name"] for r in rows) == mine


# --- REST and MCP of a workspace's token --------------------------------------------------------------------

async def test_a_workspace_token_calls_the_central_rest_api_and_the_machine_records_on_behalf(home):
    await switch(home, True)
    token = {"Authorization": f"Bearer {BETA_TOKEN}", "Accept": "application/json"}
    listed = await home.get("/api/v1/instances", headers=token)
    assert listed.status_code == 200, listed.text
    assert sorted({s["server"] for s in listed.json()}) == ["bsrv"]
    # an action of the REST API: asked of the machine, a job there
    done = await home.post("/api/v1/instances/bsrv/test/restart", headers={**token, "Idempotency-Key": "k1"}, json={})
    assert done.status_code in (200, 202), done.text
    # the other workspace's server: refused (its machine checks the servers of the REST API's routes)
    assert (await home.get("/api/v1/instances/srv/test", headers=token)).status_code in (403, 404)
    # the machine's access log: the central's link acting for t-dario, in beta
    entries = home.machine_app.state.access_log.recent(200)
    mine = [e for e in entries if e.get("kind") == "link" and e.get("name") == "central"
            and e.get("on_behalf") == "t-dario"]
    assert mine and all(e.get("workspace") in ("beta", "", None) for e in mine)
    assert any("restart" in e.get("detail", "") for e in mine)
    # the central's: the token, its workspace, the link it was asked through
    central = [e for e in home.app.state.access_log.recent(200) if e.get("name") == "t-dario"]
    assert any(e.get("workspace") == "beta" and e.get("session") == "token t-dario" and e.get("via") == "lab4"
               for e in central)


async def test_a_workspace_token_calls_the_central_mcp(home):
    await switch(home, True)
    headers = {**MCP_HEADERS, "Authorization": f"Bearer {ACME_TOKEN}"}
    found = await call(home, "list_servers", headers=headers)
    assert [s["name"] for s in found] == ["srv"], found
    entries = home.machine_app.state.access_log.recent(100)
    assert any(e.get("event") == "mcp" and e.get("name") == "central" for e in entries)


# --- the access logs, the lists, a machine that lies or does not answer ----------------------------------------

async def test_both_access_logs_say_who_which_workspace_and_which_session(home):
    await switch(home, True)
    browser = await logged_in(home, "bruno")
    resp = await browser.post("/servers/srv/test/restart", headers={"Origin": ORIGIN, "Accept": "application/json"})
    assert resp.status_code in (200, 303), resp.text
    central = [e for e in home.app.state.access_log.recent(100) if e.get("name") == "bruno"]
    acted = [e for e in central if e.get("event") == "action"]
    assert acted and acted[-1]["workspace"] == "acme" and acted[-1]["session"].startswith("session ") \
        and acted[-1]["via"] == "lab4"
    assert any(e.get("event") == "login" and e.get("via") == "lab4" for e in central)
    machine = [e for e in home.machine_app.state.access_log.recent(100) if e.get("on_behalf") == "bruno"
               and e.get("event") == "action" and "restart" in e.get("detail", "")]
    assert machine and machine[-1]["name"] == "central" and machine[-1]["workspace"] == "acme"
    # the chains of both still verify
    for app in (home.app, home.machine_app):
        assert app.state.access_log.verify()["ok"]


async def test_a_list_waits_five_seconds_and_says_the_providers_machine(home, monkeypatch):
    await switch(home, True)
    browser = await logged_in(home, "elena")
    home.machine.down = True
    resp = await browser.get("/api/servers", headers={"Accept": "application/json"})
    assert resp.status_code == 502 and "lab4" not in resp.text and "provider" in resp.text
    page = await browser.get("/", headers={"Accept": "text/html"})
    assert page.status_code == 502 and "lab4" not in page.text
    # a machine that answers a list slower than 5 s: the list says so within them (AD-018 §8.9)
    home.machine.down = False

    async def slow(request):
        await asyncio.sleep(30)
        return httpx.Response(200, json=[])
    home.machine.answer["/api/instances"] = slow
    started = time.monotonic()
    resp = await browser.get("/api/instances", headers={"Accept": "application/json"})
    assert resp.status_code == 502 and time.monotonic() - started < 6.5


async def test_a_machine_that_answers_html_or_lies_passes_nothing(home):
    await switch(home, True)
    browser = await logged_in(home, "elena")

    def html(request):
        return httpx.Response(200, headers={"content-type": "text/html", "set-cookie": "omniacli_session=evil"},
                              content=b"<script>steal()</script>")
    home.machine.answer["/api/jobs"] = html
    resp = await browser.get("/api/jobs", headers={"Accept": "application/json"})
    assert resp.status_code == 502 and "steal" not in resp.text and "set-cookie" not in resp.headers

    def admin_model(request):
        return httpx.Response(200, json={"agent_api": 4, "page": "jobs", "role": "admin", "context": {
            "jobs": [], "total": 0, "servers": [], "server": "", "q": ""}})
    home.machine.answer["/api/pages/jobs"] = admin_model
    page = await browser.get("/jobs", headers={"Accept": "text/html"})
    # the machine said admin for a user: drawn with no more than the person's role here
    assert page.status_code == 200 and "workspaces_menu" not in page.text
    # a page of the machine never passes to a customer: a model that names another job is refused
    home.machine.answer.pop("/api/jobs")


async def test_no_secret_in_what_the_people_see(home):
    await switch(home, True)
    browser = await logged_in(home, "anna", code_now())
    seen = ""
    for path in ("/", "/instances", "/backups", "/jobs", "/workspaces/acme", "/servers/srv",
                 "/account", "/api/servers", "/api/jobs", "/api/bell"):
        resp = await browser.get(path, headers={"Accept": "text/html" if not path.startswith("/api") else "application/json"})
        assert resp.status_code == 200, (path, resp.status_code, resp.text[:300])
        seen += resp.text
    record = home.app.state.identity_store.load("lab4")
    secrets_ = [SECRET, ACME_TOKEN, BETA_TOKEN, CENTRAL_LINK_TOKEN, hash_token(ACME_TOKEN),
                *(p["hash"] for p in record["people"].values())]
    assert not [s for s in secrets_ if s in seen]
    assert "lab4" not in seen and "lab4.test" not in seen


# --- the slice-1 MUST: a workspace named like one here ---------------------------------------------------------

async def test_a_workspace_named_like_one_here_is_never_served(home):
    """The rule chosen (AD-018, the code of slice 2): refused, never renamed. A workspace of the link named like one
    of this panel's is never told to the machine nor served, the others are; one that becomes so later is withheld,
    its people without it, its tokens refused; with every one taken the switch is refused."""
    home.central.workspaces = parse_workspaces({"acme": {"label": "Ours", "servers": [], "members": {}}})
    partly = await switch(home, True)
    assert partly.status_code == 200 and partly.json()["home"]["workspaces"] == ["beta"]
    assert partly.json()["home"]["refused"] == ["acme"]
    told = json.loads((home.m.jobs_dir.parent / placement.CENTRAL_HOME).read_text())
    assert sorted(told) == ["beta"]
    await switch(home, False)
    home.central.workspaces = parse_workspaces({"acme": {"label": "Ours", "servers": [], "members": {}},
                                                "beta": {"label": "Ours too", "servers": [], "members": {}}})
    refused = await switch(home, True)
    assert refused.status_code == 409 and "acme, beta" in refused.json()["error"]
    home.central.workspaces = {}
    assert (await switch(home, True)).status_code == 200
    home.central.workspaces = parse_workspaces({"acme": {"label": "Ours", "servers": [], "members": {}}})
    found = home.app.state.serving()
    assert "acme" not in found.workspaces and "beta" in found.workspaces
    assert "elena" not in found.people and "dario" in found.people
    browser = httpx.AsyncClient(transport=httpx.ASGITransport(app=home.app), base_url=ORIGIN)
    resp = await browser.post("/login", data={"username": "elena", "password": PASSWORD}, headers={"Origin": ORIGIN})
    assert resp.status_code == 401
    assert (await home.get("/api/v1/instances", headers={"Authorization": f"Bearer {ACME_TOKEN}"})).status_code == 401


def test_a_workspace_another_import_brought_is_withheld_for_both():
    one = {"people": {"a": {"hash": "pbkdf2_sha256$1000$00$00"}}, "names": {"a": "a"},
           "workspaces": {"acme": {"members": {"a": "user"}}}}
    two = {"people": {"b": {"hash": "pbkdf2_sha256$1000$00$00"}}, "names": {"b": "b"},
           "workspaces": {"acme": {"members": {"b": "user"}}, "beta": {"members": {"b": "admin"}}}}
    found = remotehome.served({"l1": one, "l2": two}, {"l1": {"on": True}, "l2": {"on": True}}, {}, {})
    assert found.workspaces == {"beta": "l2"} and found.blocked == {"l1": ["acme"], "l2": ["acme"]}
    assert set(found.people) == {"b"} and found.people["b"].workspaces == (("beta", "admin"),)


# --- the page, REST and MCP of the switch -----------------------------------------------------------------------

async def test_the_switch_on_the_page_in_italian_rest_and_mcp(home):
    page = await home.get("/identities/lab4", headers={**ADMIN, "Accept": "text/html", "Cookie": "omniacli_lang=it"})
    text = page.text
    assert "Questo pannello casa dei suoi workspace" in text and "Servili qui…" in text
    assert text.count('class="primary"') == 1
    for title, label in re.findall(r'<button[^>]*title="([^"]+)"[^>]*>([^<]+)</button>', text):
        assert title.strip() != label.strip(), label
    made = await home.post("/api/access/tokens/save", headers=ADMIN, json={"name": "boss", "role": "admin", "new": True})
    boss = {"Authorization": f"Bearer {made.json()['token']}", "Accept": "application/json"}
    v1 = "/api/v1/links/lab4/identities/home"
    assert (await home.post(v1, headers=boss, json={"confirm": "x", "on": True})).json()["error"]["code"] == \
        "confirm_required"
    assert (await home.post(v1, headers=boss, json={"confirm": "lab4", "on": True})).json()["home"]["on"] is True
    tools = {**MCP_HEADERS, **boss}
    off = await call(home, "identity_home", headers=tools, link="lab4", confirm="lab4", on=False)
    assert off["home"]["on"] is False


def test_the_purge_takes_the_new_files():
    from pathlib import Path
    from omniacli import cli
    purge = (Path(cli.__file__).parent.parent / "packaging" / "debian" / "files" / "postrm").read_text()
    purge = purge.split("    purge)", 1)[1]
    for name in (remotehome.HOMES_FILE, remotehome.SESSIONS_FILE, placement.CENTRAL_HOME):
        assert name in purge, name
