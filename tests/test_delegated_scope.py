# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The delegated scope (AD-014, step 5 of §9 of docs/adr/AD-008-consegna-e-spostamento.md): the people and the
tokens of a workspace keep reaching an instance placed on another machine through its home, with the same
answers as when it was here; the host takes their scope from the home's link only; the customer reads no name of
the provider's machines; a host that does not answer is a row that says so."""

import asyncio
import contextlib
import json
import re

import httpx
import pytest

from omniacli import capabilities, jobs as jobs_module, placement
from omniacli.app import create_app
from omniacli.auth import ROLES, hash_password, hash_token
from omniacli.config import Access, LinkTarget, Workspace, parse_servers, parse_settings, parse_workspaces, server_to_dict

from .conftest import PASSWORD, USER_HASH, BasicAsSession
from .test_app import AUTH, HOST
from .test_capabilities import V1, VALUES, workspace_routes
from .test_workspaces import ORIGIN

HOME_TOKEN = "home-admin-token"
OTHER_TOKEN = "other-admin-token"
HEADERS_MCP = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def as_user(user: str) -> dict:
    import base64
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{PASSWORD}".encode()).decode(), "Origin": ORIGIN}


class Far(httpx.AsyncBaseTransport):
    """far.test, the host (an app); down.test does not answer (a machine off, a link cut)."""

    def __init__(self, far):
        self.far = httpx.ASGITransport(app=far)
        self.down = False
        self.sent = []

    async def handle_async_request(self, request):
        self.sent.append((request.method, request.url.path, dict(request.headers)))
        if self.down or request.url.host == "down.test":
            raise httpx.ConnectError("connection refused", request=request)
        return await self.far.handle_async_request(request)


@contextlib.asynccontextmanager
async def running(*apps):
    """The apps with their lifespan (MCP's session manager), each in its own task."""
    stops, tasks = [], []
    for app in apps:
        started, stop = asyncio.Event(), asyncio.Event()

        async def life(app=app, started=started, stop=stop):
            async with app.router.lifespan_context(app):
                started.set()
                await stop.wait()
        tasks.append(asyncio.create_task(life()))
        stops.append(stop)
        await started.wait()
    try:
        yield
    finally:
        for stop in stops:
            stop.set()
        await asyncio.gather(*tasks)


PEOPLE = {**{f"as-{r}": r for r in ROLES}, "copy-admin": "copy of admin", "no-logs": "without logs"}
ROLE_CAPS = {"copy of admin": sorted(capabilities.PRESETS["admin"]),
             "without logs": sorted(capabilities.PRESETS["admin"] - {"logs"})}


@pytest.fixture
async def pair(env, tmp_path, monkeypatch):
    """This machine (the home: acme with srv, its people and tokens) and far (the host), linked as far here and
    as home there; srv placed on far (placements.json here, hosted-workspaces.json there) when pair.place()."""
    settings = env["settings"]
    password = hash_password(PASSWORD, iterations=1000)
    names = ["mario", *PEOPLE]
    settings.set_web_access(Access(users={n: password for n in names}, roles={n: "workspace" for n in names}))
    settings.workspaces = parse_workspaces({"acme": {
        "label": "ACME", "servers": ["srv"], "members": {"mario": "admin", **{n: r if r in ROLES else "admin"
                                                                            for n, r in PEOPLE.items()}},
        "member_roles": {n: r for n, r in PEOPLE.items() if r not in ROLES}, "roles": ROLE_CAPS}})
    conf = tmp_path / "far-conf"
    conf.mkdir()
    (conf / "servers.yaml").write_text("servers: {}\n")
    far_settings = parse_settings({
        "auth": {"users": {"admin": USER_HASH},
                 "links": {"home": {"token": hash_token(HOME_TOKEN), "role": "admin"},
                           "other": {"token": hash_token(OTHER_TOKEN), "role": "admin"}}},
        "backup_dir": str(tmp_path / "far-backups"), "jobs_dir": str(conf / "jobs"),
        "servers_file": "servers.yaml"}, base_dir=conf)
    far_settings.servers = {"srv": parse_servers({"srv": server_to_dict(settings.servers["srv"])},
                                                 far_settings.logs_dir)["srv"]}
    far_settings.workspaces = {"acme": Workspace("acme", "ACME", servers=["srv"], members={})}
    far = create_app(far_settings)
    transport = Far(far)
    settings.link_targets = {"far": LinkTarget("far", "http://far.test", HOME_TOKEN),
                             "down": LinkTarget("down", "http://down.test", HOME_TOKEN)}
    app = create_app(settings, link_transport=transport)
    async with running(app, far):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=BasicAsSession(app)),
                                     base_url=f"http://{HOST}") as c:
            c.app, c.far, c.far_settings, c.transport = app, far, far_settings, transport
            tokens = {}
            for person in ("as-user", "as-developer", "as-admin", "copy-admin"):
                made = await c.post("/api/workspaces/acme/tokens/save", headers=as_user("mario"),
                                    json={"name": f"t-{person}", "role": PEOPLE[person] if PEOPLE[person] in ROLES
                                          else "admin", "new": True, "person": person})
                assert made.status_code == 200, made.text
                tokens[person] = {"Authorization": f"Bearer {made.json()['token']}"}
            c.tokens = tokens

            def place(copy_here=True):
                placement.Hosted(far_settings.jobs_dir.parent).put("acme", {
                    "home": "home", "provider_label": "", "members": [], "instances": {"srv": {"state": "placed"}}})
                placement.Moves(settings.jobs_dir.parent).save("srv", {
                    "link": "far", "state": "placed", "workspace": "acme", "instance": "srv",
                    "address": "203.0.113.14", "home_address": "203.0.113.13",
                    **({} if copy_here else {"copy": "deleted"})})
            c.place = place
            yield c


async def statuses(c, method, url, headers):
    resp = await c.request(method, url, headers=headers, json={}, follow_redirects=True)
    return resp.status_code


async def test_every_route_of_a_workspace_answers_the_same_through_the_home(pair, monkeypatch):
    """The access test of AD-014 consequence 2: every route of WORKSPACE_ROUTES of a server, for each role, a
    custom role and the tokens, answers the same on srv placed on far (through /placed/srv) as on srv here."""
    real_submit = jobs_module.JobManager.submit

    def submit(self, title, instances, source, action, workspace="", frozen_ok=False):
        async def nothing(job):
            return None
        return real_submit(self, title, instances, source, nothing, workspace, frozen_ok)
    monkeypatch.setattr(jobs_module.JobManager, "submit", submit)
    routes = sorted((m, p) for m, p in workspace_routes(pair.app) if "{server}" in p)
    who = {**{p: as_user(p) for p in PEOPLE}, **{f"token {p}": h for p, h in pair.tokens.items()}}
    values = {**VALUES, "server": "srv", "role": "production"}
    here = {}
    for method, path in routes:
        url = re.sub(r"\{(\w+)(?::path)?\}", lambda m: values.get(m.group(1), "x"), path)
        for name, headers in who.items():
            if name.startswith("token") ^ path.startswith(V1):
                continue  # the REST API takes tokens, the pages people (both refuse the other: 401/403)
            here[(method, path, name)] = await statuses(pair, method, url, headers)
    pair.place()
    sent = len(pair.transport.sent)
    differ = []
    for (method, path, name), before in sorted(here.items()):
        url = re.sub(r"\{(\w+)(?::path)?\}", lambda m: values.get(m.group(1), "x"), path)
        after = await statuses(pair, method, url, who[name])
        if after != before:
            differ.append((method, path, name, before, after))
    assert not differ, "\n".join(map(str, differ))
    # every one went to far (asked there or refused here first), and they meant something
    assert len(pair.transport.sent) > sent and {200, 403} <= set(here.values())
    print(f"{len(routes)} routes, {len(here)} requests, the same answers through the home")
    assert len(here) > 150


async def test_the_host_takes_the_scope_from_the_home_only(pair):
    pair.place()
    scope = placement.scope_encode({"ws": "acme", "person": "as-user", "kind": "user", "role": "admin", "grant": None,
                                    "members": ["as-user"], "provider": ""})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=pair.far), base_url="http://far.test") as far:
        base = {"Accept": "application/json", placement.SCOPE_HEADER: scope}
        # the home's link: the person, as admin of acme (its role there); the servers of acme only
        resp = await far.get("/api/servers", headers={**base, "Authorization": f"Bearer {HOME_TOKEN}"})
        assert resp.status_code == 200 and [r["name"] for r in resp.json()] == ["srv"]
        # another link of far with the same scope: refused, and in far's access log
        resp = await far.get("/api/servers", headers={**base, "Authorization": f"Bearer {OTHER_TOKEN}"})
        assert resp.status_code == 403 and "machine of that workspace only" in resp.json()["detail"]
        failed = [e for e in pair.far.state.access_log.recent(50) if e.get("event") == "failed"]
        assert any("delegated scope refused" in e.get("detail", "") for e in failed)
        # a scope that does not decode, or of a workspace not hosted for that link: refused the same
        for value in ("not-base64!", placement.scope_encode({"ws": "beta", "person": "x", "kind": "user",
                                                               "role": "user"})):
            resp = await far.get("/api/servers", headers={"Accept": "application/json", placement.SCOPE_HEADER: value,
                                                          "Authorization": f"Bearer {HOME_TOKEN}"})
            assert resp.status_code == 403
        # the scope only narrows: a link capped to user (Role-Cap) makes an admin scope a user
        resp = await far.post("/servers/srv/production/restart", headers={
            **base, "Authorization": f"Bearer {HOME_TOKEN}", "X-OmniaCLI-Role-Cap": "user"}, json={})
        assert resp.status_code in (200, 303, 403)
        # a token's scope reads only, outside the REST API
        token = placement.scope_encode({"ws": "acme", "person": "t-as-admin", "kind": "token", "role": "admin"})
        resp = await far.post("/servers/srv/production/restart", headers={
            placement.SCOPE_HEADER: token, "Authorization": f"Bearer {HOME_TOKEN}", "Accept": "application/json"},
            json={})
        assert resp.status_code == 403 and "only read" in resp.json()["detail"]


async def test_the_customer_reads_no_machine_of_the_provider(pair):
    pair.place(copy_here=False)
    pair.app.state.settings.servers.pop("srv")
    # the page: sent to /placed/srv, served by far, the names of both machines out
    resp = await pair.get("/servers/srv", headers=as_user("as-admin"), follow_redirects=False)
    assert resp.status_code == 307 and resp.headers["location"] == "/placed/srv/servers/srv"
    page = await pair.get("/servers/srv", headers=as_user("as-admin"), follow_redirects=True)
    assert page.status_code == 200 and 'href="/placed/srv/' in page.text
    for said in (page.text, (await pair.get("/api/servers", headers=as_user("as-admin"))).text):
        for name in ("far", "203.0.113.14", "203.0.113.13", "far.test"):
            assert not re.search(rf"(?<![\w.-]){re.escape(name)}(?![\w-])", said), name
    # its menu is the home's (no prefix), its jobs are far's under the prefix
    assert 'href="/instances"' in page.text
    # what it does there is in far's access log as the home's link acting for it, with its workspace; its job is
    # the person's, and the home remembers it is far's
    resp = await pair.post("/servers/srv/production/restart", headers=as_user("as-admin"), json={})
    assert resp.status_code == 303 and resp.headers["location"].startswith("/placed/srv/jobs/")
    job = resp.headers["location"].rsplit("/", 1)[1]
    entries = pair.far.state.access_log.recent(200)
    assert any(e.get("event") == "action" and e.get("name") == "home" and e.get("on_behalf") == "as-admin"
               and e.get("workspace") == "acme" for e in entries)
    assert pair.far.state.jobs.get(job).source == "manual: as-admin"
    assert (await pair.get(f"/jobs/{job}", headers=as_user("as-user"), follow_redirects=True)).status_code == 200
    # the lists of the home: srv as far says it is
    rows = (await pair.get("/api/servers", headers=as_user("as-user"))).json()
    assert [r["name"] for r in rows] == ["srv"] and rows[0]["placed"] is True
    # a REST call of a workspace's token reaches far, its job remembered here to be asked there
    resp = await pair.post(f"{V1}/instances/srv/production/restart", headers=pair.tokens["as-admin"],
                           json={"confirm": "srv"})
    assert resp.status_code in (200, 202), resp.text
    job = resp.json()["job"]["id"] if isinstance(resp.json().get("job"), dict) else resp.json().get("id")
    assert job and placement.PlacedJobs(pair.app.state.settings.jobs_dir.parent).server(job) == "srv"
    assert (await pair.get(f"{V1}/jobs/{job}", headers=pair.tokens["as-admin"])).status_code == 200


async def test_mcp_of_a_workspace_token_reaches_the_placed_instance(pair):
    pair.place(copy_here=False)
    pair.app.state.settings.servers.pop("srv")
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "server_status", "arguments": {"server_name": "srv"}}})
    resp = await pair.post("/mcp", headers={**HEADERS_MCP, **pair.tokens["as-user"]}, content=body)
    assert resp.status_code == 200, resp.text
    assert "DB_production" in resp.text and "isError\":true" not in resp.text.replace(" ", "")
    assert any(p == "/mcp" and placement.SCOPE_HEADER.lower() in h for _m, p, h in pair.transport.sent)


async def test_the_host_down_is_a_row_that_says_it(pair):
    pair.place(copy_here=False)
    pair.app.state.settings.servers.pop("srv")
    pair.transport.down = True
    rows = (await pair.get("/api/servers", headers=as_user("as-user"))).json()
    assert rows[0]["instances"]["production"]["down"].startswith("The machine of the provider does not answer")
    page = await pair.get("/servers/srv", headers={**as_user("as-user"), "Accept": "text/html"}, follow_redirects=True)
    assert page.status_code == 502 and "goes on serving" in page.text and "far" not in page.text
    italian = await pair.get("/api/servers", headers={**as_user("as-user"), "Accept-Language": "it"})
    assert "non risponde" in italian.json()[0]["instances"]["production"]["down"]
    # the machine's admins: not a row here (its machine's list says it does not answer), and its page says which
    assert "srv" not in [r["name"] for r in (await pair.get("/api/servers", headers={"Authorization": AUTH})).json()]
    page = await pair.get("/placed/srv/servers/srv", headers={"Authorization": AUTH, "Accept": "application/json"})
    assert page.status_code == 502 and page.json()["error"].startswith("far does not answer: ")


# --- the records and the helpers (placement.py) -------------------------------------------------------------------

def test_the_scope_header_is_checked_and_only_narrows():
    scope = {"ws": "acme", "person": "anna", "kind": "user", "role": "developer",
             "grant": {"role": "senza-log", "capabilities": ["backup", "restart"], "servers": ["acme-erp"]},
             "members": ["anna", "bad name", "mario"], "provider": "OmniaSolutions", "ci": False}
    found = placement.scope_decode(placement.scope_encode(scope))
    assert found["grant"]["servers"] == ["acme-erp"] and found["members"] == ["anna", "mario"]
    assert found["provider"] == "OmniaSolutions" and found["ci"] is False
    for bad in ({**scope, "role": "root"}, {**scope, "kind": "link"}, {**scope, "ws": "../x"},
                {**scope, "person": "a b"}, {**scope, "grant": {"capabilities": "all"}}, ["not", "a", "dict"]):
        assert placement.scope_decode(placement.scope_encode(bad)) is None, bad
    assert placement.scope_decode("@@@") is None and placement.scope_decode("") is None


def test_the_paths_of_a_placed_server():
    assert placement.server_of_path("/servers/acme-erp") == ("acme-erp", "")
    assert placement.server_of_path("/api/servers/acme-erp/production/log") == ("acme-erp", "production/log")
    assert placement.server_of_path("/backups/acme-erp/production/x.zip")[0] == "acme-erp"
    assert placement.server_of_path("/api/v1/instances/acme-erp/production/restart")[0] == "acme-erp"
    assert placement.server_of_path("/workspaces/acme") == ("", "") and placement.server_of_path("/api/servers") == ("", "")
    assert placement.job_of_path("/jobs/20261123-222744-d2e2bc") == "20261123-222744-d2e2bc"
    assert placement.job_of_path("/api/v1/jobs/20261123-222744-d2e2bc/log") == "20261123-222744-d2e2bc"
    assert placement.inner_path("/placed/acme-erp/api/v1/jobs/x") == "/api/v1/jobs/x"
    assert placement.inner_path("/servers/acme-erp") == "/servers/acme-erp"
    assert placement.job_ids({"job": "a-1"}) == ["a-1"] and placement.job_ids({"job": {"id": "b-2"}}) == ["b-2"]
    assert placement.job_ids({"jobs": [{"id": "c-3"}], "job": "../x"}) == ["c-3"]


def test_the_jobs_of_the_hosts_are_remembered_privately(tmp_path):
    found = placement.PlacedJobs(tmp_path)
    found.add("j-1", "acme-erp")
    found.add("j-2", "beta-x")
    assert placement.PlacedJobs(tmp_path).server("j-1") == "acme-erp"
    assert oct((tmp_path / placement.PLACED_JOBS).stat().st_mode & 0o777) == "0o600"
    for n in range(placement.PLACED_JOBS_KEPT + 3):
        found.add(f"k-{n}", "acme-erp")
    assert len(placement.PlacedJobs(tmp_path).all()) == placement.PLACED_JOBS_KEPT and found.server("j-1") == ""
    found.forget("acme-erp")
    assert placement.PlacedJobs(tmp_path).all() == {}


def test_a_move_job_reads_for_its_customer_as_it_writes_for_it():
    """The customer of a moved instance reads «moved to another machine of the provider», its maintenance and its
    stop, never the steps (they name the machines); the provider's own jobs of a move are not in its lists."""
    from omniacli.customerview import CustomerView, hidden_from_customers
    from omniacli.jobs import Job
    view = CustomerView("the provider", "another machine", links=frozenset({"central"}),
                        machines=frozenset({"lab4"}), here=frozenset({"lab3"}))
    job = Job("j", "Move acme-erp to lab4: complete", ["acme-erp/production"], "manual: link central (carlo)",
              lines=["22:27:44 --- acme-erp stops here (lab3)"], result={
                  "progress": {"steps": [{"id": "start", "label": "Start on lab4"}]},
                  "customer": {"title": "acme-erp moved to another machine of the provider",
                               "lines": ["Without service for 13.0 s"]}})
    shown = view.job(job)
    assert shown.title == "acme-erp moved to another machine of the provider" and shown.result == {}
    assert shown.lines == ["Without service for 13.0 s"] and shown.source == "manual: the provider"
    assert job.title.startswith("Move acme-erp to lab4") and not hidden_from_customers(job)
    assert hidden_from_customers(Job("k", "Move acme-erp to lab4: prepare", [], "", result={"customer": {"hidden": True}}))
    # this machine's own name: «the server», as the map says it; another machine's: another machine
    assert view.text("on lab3, then lab4") == "on the server, then another machine"


async def test_the_schedules_of_a_placed_instance_pass_to_its_host(pair):
    """At the completion the instance's schedules and the workspace's destinations they copy to are given to the
    host, which takes an instance's schedules and its workspace's destinations only."""
    from omniacli import destinations, pullcrypto, schedules
    pair.place(copy_here=False)
    # the host names an instance of a workspace <workspace>-<instance>, as its home does
    pair.far_settings.servers["acme-srv"] = pair.far_settings.servers["srv"]
    pair.far_settings.workspaces["acme"].servers.append("acme-srv")
    body = {"schedules": {"backup.acme-srv.production": {"instance": "acme-srv/production", "cron": "30 3 * * *",
                                                    "backup": {"destination": "acme-offsite"}},
                          "backup.other.production": {"instance": "other/production", "cron": "0 1 * * *"}},
            "destinations": {"acme-offsite": {"kind": "sftp", "path": "acme", "workspace": "acme",
                                              "settings": {"host": "backup.invalid", "user": "u", "port": "22",
                                                           "pass": "a-secret"},
                                              "key": pullcrypto.new_key()},
                             "theirs": {"kind": "sftp", "path": "x", "workspace": "beta",
                                        "settings": {"host": "backup.invalid", "user": "u", "port": "22"}}}}
    headers = {"Authorization": f"Bearer {HOME_TOKEN}", "Accept": "application/json"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=pair.far), base_url="http://far.test") as far:
        resp = await far.post("/api/placements/acme/srv/sync", headers=headers, json=body)
        assert resp.status_code == 200, resp.text
        job = pair.far.state.jobs.get(resp.json()["job"])
        for _ in range(100):
            if job.finished_flag:
                break
            await asyncio.sleep(0.05)
    assert job.status == "success", job.lines
    kept = schedules.load_schedules(pair.far_settings.schedules_file)
    assert set(kept) == {"backup.acme-srv.production"}
    assert kept["backup.acme-srv.production"].backup.destination == "acme-offsite"
    assert "The destinations of its workspace: acme-offsite" in "\n".join(job.lines), job.lines
    found = destinations.load_destinations(pair.far_settings.destinations_file)
    assert set(found) == {"acme-offsite"} and found["acme-offsite"].workspace == "acme"
    # the provider's own job: not in the lists of the workspace's people
    assert job.result.get("customer") == {"hidden": True}
