# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The map of the infrastructure (docs/ux/UX-009): GET /api/map for each
kind of reader, a link that does not answer or is older, what a customer
is not told, the page's markup, the audit's classification."""

import asyncio
import datetime
import json
import re
import socket
import time

import httpx

from omniacli import audit, i18n, inframap, machines, stagings
from omniacli.app import WORKSPACE_ROUTES, create_app
from omniacli.config import LinkTarget
from omniacli.links import describe_error
from omniacli.monitor import Monitor

from .conftest import BasicAsSession
from .test_app import AUTH, client, wait_job  # noqa: F401 - the client fixture
from .test_links import as_user, mine  # noqa: F401 - the mine fixture
from .test_workspaces import basic, workspaces  # noqa: F401 - the workspaces fixture

HERE_NAME = machines.here_name(socket.gethostname())


def every_instance(data: dict) -> list[dict]:
    return [i for m in data["machines"] for i in
            [*(i for ws in m["workspaces"] for i in ws["instances"]), *m["loose"]]]


async def test_the_map_of_every_machine(mine):
    started = time.monotonic()
    data = (await mine.get("/api/map", headers=as_user("admin"))).json()
    # a link that does not answer: its machine at once, saying why, the others drawn all the same
    assert time.monotonic() - started < 2
    assert data["part"] == "all" and data["viewer"] == {"scope": "machine", "role": "admin"}
    here, cliente, down = data["machines"]
    assert (here["id"], here["here"], here["name"], here["machine_name"]) == ("~", True, None, HERE_NAME)
    assert here["answered"] and here["drafts"] == 0 and here["workspaces"] == [] and here["loose"] == []
    # the link's own machine under the link's name: its ids, its pages through the link
    assert (cliente["id"], cliente["here"], cliente["name"], cliente["answered"]) == ("cliente", False, "cliente", True)
    srv = cliente["loose"][0]
    assert (srv["id"], srv["server"], srv["type"], srv["page"]) == ("cliente/srv", "srv", "production",
                                                                     "/links/cliente/servers/srv")
    assert set(srv["envs"]) == {"production", "test"} and srv["envs"]["production"]["busy"] is False
    assert srv["envs"]["production"]["database"]["where"] in ("system", "remote", "dedicated", "machine")
    # the link is a developer there: it reads the backups, not the drafts of the machine
    assert srv["backup"]["scheduled"] is False and cliente["drafts"] is None
    assert (down["id"], down["answered"], down["older"]) == ("down", False, False) and "cannot connect" in down["error"]
    assert down["workspaces"] == [] and down["loose"] == []
    watch = data["verdict"]["watch"]
    assert data["verdict"]["ok"] is False and [w["kind"] for w in watch] == ["machine_unread"]
    assert watch[0]["node"] == "down" and watch[0]["text"].startswith("down does not answer: cannot connect")
    assert {(e["from"], e["to"]) for e in data["edges"] if e["kind"] == "link"} == {("~", "cliente"), ("~", "down")}
    # the automatic backups of every machine that answered, summed
    assert data["tonight"] == {"ok": 0, "failed": 0, "unscheduled": 1}
    # asked with the caller's role at most, in the caller's language
    asked = [r for r in mine.network.requests if r.url.path == "/api/map"]
    assert {r.url.host for r in asked} == {"customer.test", "down.test"}
    assert all(r.headers["x-omniacli-role-cap"] == "admin" and r.headers["x-omniacli-user"] == "admin" for r in asked)
    italian = (await mine.get("/api/map", headers={**as_user("admin"), "Accept-Language": "it"})).json()
    assert italian["verdict"]["watch"][0]["text"].startswith("down non risponde: connessione non riuscita")
    # one machine only; one that is no link is not there
    only = (await mine.get("/api/map?machine=cliente", headers=as_user("admin"))).json()
    assert [m["id"] for m in only["machines"]] == ["cliente"] and all(e["kind"] != "link" for e in only["edges"])
    only = (await mine.get("/api/map?machine=~", headers=as_user("admin"))).json()
    assert [m["id"] for m in only["machines"]] == ["~"] and only["verdict"]["ok"] is True
    assert (await mine.get("/api/map?machine=nope", headers=as_user("admin"))).status_code == 404
    assert (await mine.get("/api/map?part=some", headers=as_user("admin"))).status_code == 400
    # the path of an instance of a link: asked there, its node under the link's name
    flow = (await mine.get("/api/map?focus=cliente/srv", headers=as_user("admin"))).json()["flow"]
    assert flow["node"] == "cliente/srv" and flow["firewall"] is None and flow["mail"] is None
    assert flow["links"]["page"] == "/links/cliente/servers/srv" and flow["web"]["label"] == HERE_NAME
    # a user of the machine reads the same machines with its role there: no backups for a user
    viewer = (await mine.get("/api/map", headers=as_user("viewer"))).json()
    assert viewer["viewer"] == {"scope": "machine", "role": "user"} and viewer["tonight"] is None
    assert viewer["machines"][1]["loose"][0]["backup"] is None and viewer["machines"][0]["drafts"] is None
    assert [r for r in mine.network.requests if r.url.path == "/api/map"][-1].headers["x-omniacli-role-cap"] == "user"


async def test_the_layer_of_now(mine):
    data = (await mine.get("/api/map?part=now", headers=as_user("admin"))).json()
    assert set(data) == {"at", "part", "viewer", "verdict", "tonight", "jobs", "machines"}
    here, cliente, down = data["machines"]
    assert set(here) == {"id", "answered", "error", "older", "load", "envs"}
    assert cliente["id"] == "cliente" and cliente["envs"] == {
        "cliente/srv": {role: {"busy": False, "job": None, "queued": False} for role in ("production", "test")}}
    assert down["answered"] is False and data["verdict"]["watch"][0]["kind"] == "machine_unread"
    assert all(r.url.params.get("part") == "now" for r in mine.network.requests if r.url.path == "/api/map")
    # the link that did not answer is not asked the layer of now again: its reason at once, the others asked
    asked = [r.url.host for r in mine.network.requests if r.url.path == "/api/map"]
    again = (await mine.get("/api/map?part=now", headers=as_user("admin"))).json()
    assert [r.url.host for r in mine.network.requests if r.url.path == "/api/map"] == [*asked, "customer.test"]
    assert again["machines"][2]["error"] == down["error"] and again["machines"][1]["answered"] is True
    # the whole read asks it again
    await mine.get("/api/map", headers=as_user("admin"))
    assert [r.url.host for r in mine.network.requests if r.url.path == "/api/map"][-2:] == ["customer.test", "down.test"]


async def test_a_link_that_is_older_says_so(mine, monkeypatch):
    plain = mine.network.handle_async_request

    async def without_the_map(request):
        if request.url.path == "/api/map":
            return httpx.Response(404, json={"detail": "Not Found"}, request=request)
        return await plain(request)
    monkeypatch.setattr(mine.network, "handle_async_request", without_the_map)
    data = (await mine.get("/api/map", headers={**as_user("admin"), "Accept-Language": "it"})).json()
    cliente = data["machines"][1]
    assert (cliente["id"], cliente["answered"], cliente["older"]) == ("cliente", False, True)
    assert cliente["error"] == "OmniaCLI là è più vecchio: aggiornalo per vederla sulla mappa"
    assert [(w["kind"], w["node"]) for w in data["verdict"]["watch"]] == [("machine_older", "cliente"), ("machine_older", "down")]
    now = (await mine.get("/api/map?part=now", headers=as_user("admin"))).json()
    assert (now["machines"][1]["answered"], now["machines"][1]["older"]) == (False, True)
    assert [m["id"] for m in data["machines"]] == ["~", "cliente", "down"]
    # an answer that is no map: a machine saying so, never an error of the page

    async def nonsense(request):
        if request.url.path == "/api/map":
            return httpx.Response(200, json={"machines": "?"}, request=request)
        return await plain(request)
    monkeypatch.setattr(mine.network, "handle_async_request", nonsense)
    resp = await mine.get("/api/map", headers=as_user("admin"))
    assert resp.status_code == 200 and resp.json()["machines"][1]["error"] == "it did not answer a map"


async def test_what_a_user_of_workspaces_reads(workspaces, client):  # noqa: F811
    settings = workspaces
    # another machine linked from here: a customer is never told its name, nor this machine's
    settings.link_targets["lab9"] = LinkTarget("lab9", "https://lab9.example.net", "token")
    jobs = client.app.state.jobs

    async def action(job):
        job.result["progress"] = {"steps": [{"id": "backup", "label": "Backup", "where": HERE_NAME},
                                            {"id": "download", "label": "Transfer", "where": f"{HERE_NAME} → lab9"}],
                                  "step": "download", "done": 5, "total": 10}
        job.result["transfer"] = {"from": "", "to": "lab9"}
        job.log(f"copied to /var/lib/omniacli/backups on {HERE_NAME}")
        job.warn("sent to lab9 late")
    job = jobs.submit(f"Staging of srv on lab9 from {HERE_NAME}", ["srv/production"], "manual: admin", action)
    await wait_job(client, job.id)
    mario = await client.get("/api/map", headers=basic("mario"))
    data = mario.json()
    assert data["viewer"] == {"scope": "workspace", "role": "admin"}
    machine, = data["machines"]
    assert (machine["id"], machine["name"], machine["machine_name"]) == ("~", None, None)
    assert machine["load"] is None and machine["disk"] is None and machine["drafts"] is None and machine["loose"] == []
    acme, = machine["workspaces"]
    assert (acme["name"], acme["can_open"], acme["members"]) == ("acme", True, 2)
    srv, = acme["instances"]
    assert srv["id"] == "~/srv" and srv["backup"]["scheduled"] is False and data["tonight"]["unscheduled"] == 1
    assert "parent_machine" not in srv and data["edges"] == [] and all(e["kind"] != "link" for e in data["edges"])
    # its job, with no name of a machine: the step is on "the server", the machines of the transfer are not said
    shown = next(j for j in data["jobs"] if j["id"] == job.id)
    assert shown["title"] == "Staging of srv on another machine from the server" and shown["by"] == "manual: the provider"
    assert [s["where"] for s in shown["progress"]["steps"]] == ["the server", "the server"]
    assert (shown["from"], shown["to"], shown["outcome"], shown["instances"]) == (None, None, "warned", ["~/srv"])
    warned = next(w for w in data["verdict"]["watch"] if w["kind"] == "job_warned")
    assert warned["job"] == job.id and warned["node"] == "~/srv" and warned["page"] == f"/jobs/{job.id}"
    for text in (mario.text, (await client.get("/api/map?part=now", headers=basic("mario"))).text,
                 (await client.get("/api/map?focus=srv", headers=basic("mario"))).text):
        for hidden in (HERE_NAME, "lab9", "beta", "/var/", "/srv/"):
            assert hidden not in text, hidden
    # the job says how long ago by this machine's clock: the page never measures against the browser's
    assert shown["age_s"] is not None and 0 <= shown["ended_s"] < 60 and shown["follows"] is None
    # the pages the map lives in and their lists: no name of this machine for a customer, of whichever role
    for user in ("mario", "luca"):
        for path in ("/instances?view=map", "/instances", "/backups"):
            page = await client.get(path, headers={**basic(user), "Accept": "text/html"})
            assert page.status_code == (403 if user == "luca" and path == "/backups" else 200)
            assert HERE_NAME not in page.text and "lab9" not in page.text, (user, path)
        listed = await client.get("/api/instances", headers=basic(user))
        assert listed.json()["name"] == "" and HERE_NAME not in listed.text
    assert (await client.get("/api/backups", headers=basic("mario"))).json()["name"] == ""
    assert (await client.get("/api/instances", headers={"Authorization": AUTH})).json()["name"] == HERE_NAME
    # the machine's admin reads the same job as it is
    whole = (await client.get("/api/map", headers={"Authorization": AUTH})).json()
    shown = next(j for j in whole["jobs"] if j["id"] == job.id)
    assert shown["title"].endswith(f"from {HERE_NAME}") and (shown["from"], shown["to"]) == ("~", "lab9")
    assert shown["progress"]["steps"][1]["where"] == f"{HERE_NAME} → lab9"
    assert [ws["name"] for ws in whole["machines"][0]["workspaces"]] == ["acme", "beta"]
    # the path of its instance: the web server is "the server"
    flow = (await client.get("/api/map?focus=srv", headers=basic("mario"))).json()["flow"]
    assert flow["node"] == "~/srv" and flow["web"] == {"label": "the server"} and flow["firewall"] is None
    assert flow["links"] == {"page": "/servers/srv", "odoo": srv["url"], "mail": None}
    # luca, a user there: the states and the jobs, no backups, no numbers of the monitor, a box that does not open
    data = (await client.get("/api/map", headers=basic("luca"))).json()
    assert data["viewer"] == {"scope": "workspace", "role": "user"} and data["tonight"] is None
    acme, = data["machines"][0]["workspaces"]
    assert (acme["can_open"], acme["members"], acme["limits"]) == (False, None, None)
    srv, = acme["instances"]
    assert srv["backup"] is None and srv["monitor"]["last_ms"] is None and srv["monitor"]["uptime_24h"] is None
    assert srv["envs"]["production"]["state"] in ("running", "stopped", "unknown")
    assert [j["id"] for j in data["jobs"]] == [job.id]
    # a workspace that is not its own is answered as one that does not exist; no machine but this one
    for user, workspace in (("mario", "beta"), ("mario", "nope"), ("eva", "acme")):
        assert (await client.get(f"/api/map?workspace={workspace}", headers=basic(user))).status_code == 404
    assert (await client.get("/api/map?machine=lab9", headers=basic("mario"))).status_code == 404
    own = (await client.get("/api/map?workspace=acme", headers=basic("mario"))).json()
    assert [ws["name"] for ws in own["machines"][0]["workspaces"]] == ["acme"]
    # eva, a developer of a workspace with no instance: its box, empty; zoe, in no workspace: nothing
    data = (await client.get("/api/map", headers=basic("eva"))).json()
    assert [(ws["name"], ws["instances"], ws["can_open"]) for ws in data["machines"][0]["workspaces"]] == [("beta", [], False)]
    assert data["viewer"]["role"] == "developer" and data["tonight"] == {"ok": 0, "failed": 0, "unscheduled": 0}
    data = (await client.get("/api/map", headers=basic("zoe"))).json()
    assert data["machines"][0]["workspaces"] == [] and data["jobs"] == [] and data["verdict"]["ok"] is True


async def test_the_stagings_on_a_linked_machine(mine, monkeypatch):
    """A staging made on the link's machine from a production of this one is
    tied to its production by the machines' own names."""
    plain = mine.network.handle_async_request

    async def with_a_staging(request):
        resp = await plain(request)
        if request.url.path != "/api/map" or request.url.host != "customer.test":
            return resp
        data = json.loads(await resp.aread())
        if "loose" in data["machines"][0]:
            data["machines"][0]["machine_name"] = "there"
            data["machines"][0]["loose"][0].update({"type": "staging", "elsewhere": True, "parent_server": "erp",
                                                    "parent_machine": HERE_NAME})
        return httpx.Response(200, json=data, request=request)
    monkeypatch.setattr(mine.network, "handle_async_request", with_a_staging)
    data = (await mine.get("/api/map", headers=as_user("admin"))).json()
    # no production erp here: the staging stays without a parent, no line
    assert data["machines"][1]["loose"][0]["parent"] is None
    assert [e for e in data["edges"] if e["kind"] == "staging"] == []
    machines_ = [{"id": "~", "machine_name": "lab1", "workspaces": [{"instances": [{"id": "~/erp"}]}], "loose": []},
                 {"id": "lab2", "machine_name": "lab2", "loose": [], "workspaces": [{"instances": [
                     {"id": "lab2/erp-stg", "parent": None, "parent_server": "erp", "parent_machine": "lab1"},
                     {"id": "lab2/other", "parent": None}]}]}]
    assert inframap.link_stagings(machines_) == [{"from": "~/erp", "to": "lab2/erp-stg", "kind": "staging", "count": 1}]
    assert machines_[1]["workspaces"][0]["instances"][0]["parent"] == "~/erp"


async def test_a_customer_sees_its_stagings_of_another_machine_unnamed(workspaces):  # noqa: F811
    """A user of workspaces does not reach the links: this machine asks them,
    as a user, for the stagings of its productions, and shows them under
    their production with no name of that machine, of its workspaces or servers."""
    settings = workspaces
    settings.link_targets["lab9"] = LinkTarget("lab9", "https://lab9.example.net", "token")
    asked = []

    class There(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            asked.append(request)
            staging = {"id": "~/pool-erp-stg-20261121", "server": "pool-erp-stg-20261121", "name": "stg.acme.example.com",
                       "type": "staging", "parent": None, "elsewhere": True, "parent_server": "srv",
                       "parent_machine": HERE_NAME, "version": "19.0", "created": "2026-11-21T09:00:00",
                       "url": "https://stg.acme.example.com", "page": "/servers/pool-erp-stg-20261121",
                       "envs": {"production": {"state": "running", "busy": False, "job": None, "queued": False,
                                               "names": [], "database": {"where": "dedicated"}}, "test": None},
                       "mail": {"capture": True, "count": 3, "counted_at": "2026-11-22T10:00:00", "last": "x", "inbox": False}}
            other = {**staging, "id": "~/pool-other-stg", "parent_server": "not-theirs", "created": "2026-11-20T09:00:00"}
            # one without a name: it answers on a port of that machine, which is no address for a customer
            unnamed = {**staging, "id": "~/pool-erp-stg-20261122", "server": "pool-erp-stg-20261122", "name": "localhost:20002",
                       "url": "http://localhost:20002", "created": "2026-11-22T08:00:00"}
            return httpx.Response(200, request=request, json={"machines": [{
                "id": "~", "machine_name": "lab9-real", "workspaces": [{"id": "~/ws:pool", "name": "pool",
                                                                       "instances": [unnamed, other, staging]}], "loose": []}]})
    app = create_app(settings, link_transport=There())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=BasicAsSession(app)), base_url="http://mine.test") as c:
        resp = await c.get("/api/map", headers=basic("luca"))
        acme, = resp.json()["machines"][0]["workspaces"]
        srv, staging, unnamed = acme["instances"]
        # numbered within their production, the oldest first: another customer's staging does not move them
        assert (staging["id"], staging["server"], staging["name"], staging["page"]) == ("elsewhere/srv/1", None, "stg.acme.example.com", None)
        assert (staging["parent"], staging["elsewhere"], staging["type"]) == (srv["id"], True, "staging")
        assert staging["envs"]["production"]["state"] == "running" and staging["url"] == "https://stg.acme.example.com"
        assert staging["mail"] == {"capture": True, "count": 3, "last": "x", "counted_at": "2026-11-22T10:00:00", "inbox": False}
        # the one without a name: the word and its day, no address to open
        assert (unnamed["id"], unnamed["name"], unnamed["url"]) == ("elsewhere/srv/2", "Staging of 22/11", None)
        for hidden in ("lab9", "pool", "not-theirs", HERE_NAME, "localhost", "20002"):
            assert hidden not in resp.text, hidden
        italian = (await c.get("/api/map", headers={**basic("luca"), "Accept-Language": "it"})).json()
        assert italian["machines"][0]["workspaces"][0]["instances"][2]["name"] == "Staging del 22/11"
        # asked once, as a user, and kept: the next read does not ask the link again
        assert (await c.get("/api/map", headers=basic("mario"))).json()["machines"][0]["workspaces"][0]["instances"][1]["elsewhere"]
        assert len(asked) == 1 and asked[0].headers["x-omniacli-role-cap"] == "user" and asked[0].url.path == "/api/map"
        # asked as the map itself and for those stagings only: no customer's name reaches the link or its access log
        assert asked[0].headers["x-omniacli-user"] == inframap.SYSTEM_ACTOR == "map"
        assert asked[0].url.params["stagings_of"] == HERE_NAME
        for request in asked:
            assert not {"mario", "luca", "eva", "zoe"} & set(request.headers.values()), dict(request.headers)
        # the layer of now asks no link for a customer
        await c.get("/api/map?part=now", headers=basic("mario"))
        assert len(asked) == 1
        # nor does the path of one of its instances
        await c.get("/api/map?focus=srv", headers=basic("mario"))
        assert len(asked) == 1


def test_the_names_as_the_last_publication_left_them(tmp_path):
    names = inframap.WebNames(tmp_path / inframap.WEB_FILE)
    assert names.of("acme") == {}
    names.record("acme", {"names": ["erp.acme.it", "www.acme.it", "old.acme.it"], "https": True,
                          "redirects": {"acme.com": "erp.acme.it"}, "without_dns": ["www.acme.it"],
                          "elsewhere": ["old.acme.it"]})
    # an answer without names (no storage, no domain) keeps what was read
    names.record("acme", {})
    known = inframap.WebNames(tmp_path / inframap.WEB_FILE).of("acme")
    assert known["erp.acme.it"] == {"https": True, "dns": "here"} and known["acme.com"] == {"https": True, "dns": "here"}
    assert known["www.acme.it"] == {"https": False, "dns": "none"} and known["old.acme.it"] == {"https": False, "dns": "elsewhere"}
    found = inframap.env_names(["erp.acme.it"], ["www.acme.it", "new.acme.it"], {"acme.com": "erp.acme.it"}, known)
    assert [(n["name"], n["kind"], n["to"]) for n in found] == [
        ("erp.acme.it", "main", None), ("www.acme.it", "alias", None), ("new.acme.it", "alias", None),
        ("acme.com", "redirect", "erp.acme.it")]
    # a name the publication did not read is not read yet, never an error
    assert (found[2]["https"], found[2]["dns"]) == (None, None)
    assert inframap.bad_names(found) == ["www.acme.it"]


def test_what_is_to_look_at():
    def say(text, **values):
        return text.format(**values)
    node = {"id": "~/erp", "name": "erp.acme.it", "type": "production", "monitor": {"state": "down", "since": "2026-11-22T09:00:00"},
            "envs": {"production": {"state": "stopped", "names": [{"name": "erp.acme.it", "https": False, "dns": "here"}]},
                     "test": {"state": "stopped", "names": []}},
            "backup": {"last": {"time": "2026-11-22T02:10:00", "ok": False, "job": "j1"}},
            "certificate": {"days": 9, "checked": "2026-11-22T03:00:00"}}
    found = inframap.watch_of(node, say)
    assert [w["kind"] for w in found] == ["stopped", "down", "backup_failed", "certificate", "names"]
    assert found[2]["job"] == "j1" and found[3]["text"] == "The certificate of erp.acme.it ends in 9 days"
    # a staging or a test stopped is a choice; a certificate with days to go and a backup done are nothing
    quiet = {"id": "~/stg", "name": "stg", "type": "staging", "monitor": {"state": "stopped"},
             "envs": {"production": {"state": "stopped", "names": [{"name": "stg", "https": None, "dns": None}]}, "test": None},
             "backup": None, "certificate": {"days": 61, "checked": "x"}}
    assert inframap.watch_of(quiet, say) == []


def test_the_caught_mail_is_counted_at_most_every_five_minutes(monkeypatch):
    counts = inframap.MailCounts()
    assert counts.of("stg") == {"count": None, "last": None, "counted_at": None}
    assert counts.due("stg") is True and counts.due("stg") is False and counts.due("other") is True
    counts.store("stg", 12, "2026-11-22T09:58:00")
    assert counts.of("stg")["count"] == 12 and counts.of("stg")["counted_at"]
    now = time.monotonic()
    monkeypatch.setattr(inframap.time, "monotonic", lambda: now + inframap.MAIL_EVERY + 1)
    assert counts.due("stg") is True


async def test_the_monitor_keeps_the_days_of_each_certificate(workspaces, tmp_path, monkeypatch):
    settings = workspaces
    settings.servers["srv"].production.site = "https://erp.acme.example.com"

    async def run_state(inst):
        return "running"
    monitor = Monitor(settings, None, run_state, tmp_path / "monitor.json")
    assert monitor.certificate("erp.acme.example.com") is None
    monkeypatch.setattr("omniacli.monitor.certificate_days", lambda name: 61)
    await monitor.check_certificates()
    found = monitor.certificate("erp.acme.example.com")
    # no problem at 61 days, and the days are there all the same
    assert found["days"] == 61 and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d", found["checked"])
    assert monitor.problems() == {}


async def test_the_map_view_of_the_instances_page(workspaces, client, mine):  # noqa: F811
    page = (await mine.get("/instances?view=map", headers=as_user("admin"))).text
    # the choice of the view, with what each one does; the map is in the page, hidden until chosen
    views = re.findall(r'<button type="button" role="tab" data-view="(\w+)"[^>]*\stitle="([^"]+)">([^<]+)</button>', page)
    assert [(v[0], v[2]) for v in views] == [("list", "List"), ("map", "Map")]
    assert views[1][1] == ("Shows the same instances as a schema: where each thing configured is and what "
                           "moves now. It changes nothing.")
    assert '<div id="map-view" hidden>' in page and "window.OmniaMap = " in page and 'class="map-skeleton"' in page
    section = page.split('<div id="map-view" hidden>', 1)[1].split("<script>", 1)[0]
    # it reads only: no primary, no form, no button without what it does; the jobs' strip alone speaks by itself
    assert 'class="primary"' not in section and "<form" not in section and section.count("aria-live=") == 1
    for tag in re.findall(r"<(?:button|summary)\b[^>]*>", section):
        assert re.search(r'\stitle="[^"]{12,}"', tag), tag
    # the diagrams take their colours from the theme only
    script = page.split("window.OmniaMap = ", 1)[1].split("</script>", 1)[0]
    assert not re.search(r"""(?:fill|stroke)["']?\s*[:=]\s*["'](?:#|rgb)""", script)
    style = page.split('<div id="map-view" hidden>', 1)[0].rsplit("<style>", 1)[1]
    assert not re.search(r"(?:fill|stroke):\s*(?:#|rgb)", style) and "prefers-reduced-motion" in style
    # none of what the map must never ask
    for never in ("/production/mail", "/mail/inbox", "/database", "/api/postgres", "/api/backups", "/pitr",
                  "/remote-backups", "/addresses/check", "/api/access/verify", "/test"):
        assert never not in script, never
    assert "/api/map" in script and "document.hidden" in script
    # no new place in the menu: the same entries as the list's page
    menu = page.split('<nav class="topnav"', 1)[1].split("</nav>", 1)[0]
    assert menu.count('class="nav-link"') == 4 and "view=map" not in menu
    # in Italian
    page = (await mine.get("/instances", headers={**as_user("admin"), "Accept-Language": "it"})).text
    assert ">Elenco</button>" in page and ">Mappa</button>" in page and "Non cambia niente." in page
    # a user of workspaces has the same view, asked as its workspaces' only
    page = (await client.get("/instances?view=map", headers={**basic("luca"), "Accept": "text/html"})).text
    assert 'data-view="map"' in page and "const NARROW = true;" in page and "data-new-instance" not in page
    # the links to the map: of a workspace, from its Instances tab; of an instance, under its environments
    page = (await client.get("/workspaces/acme", headers={**basic("mario"), "Accept": "text/html"})).text
    assert 'href="/instances?view=map&amp;workspace=acme"' in page and page.count('role="tab"') == 4
    page = (await client.get("/servers/srv", headers={"Authorization": AUTH})).text
    # over the schema of its workspace, where its node is
    assert 'href="/instances?view=map&amp;workspace=acme&amp;focus=srv"' in page
    # what the page counts with: the server's seconds, never this browser's clock against the server's times
    assert "age_s" in script and "ended_s" in script
    assert not re.search(r"Date\.now\(\)\s*-\s*new Date\(", script) and "new Date(j.finished)" not in script
    # a read that does not come is given up, and says so in the reader's language
    assert "AbortController" in script and "READ_WITHIN" in script


def test_the_route_is_classified_and_open_to_the_workspaces():
    assert "/api/map" in audit.NOT_SENSITIVE and "/api/map" not in audit.SENSITIVE_READS
    assert "/api/map" in WORKSPACE_ROUTES


def test_a_links_answer_becomes_one_of_the_machines():
    theirs = {
        "tonight": {"ok": 2, "failed": 1, "unscheduled": 0},
        "verdict": {"ok": False, "watch": [{"kind": "stopped", "node": "~/erp", "text": "x", "since": None},
                                           {"kind": "disk", "node": "~", "text": "y", "since": None},
                                           {"kind": "job_failed", "node": None, "text": "z", "page": "/jobs/j1"}]},
        "machines": [{"id": "~", "here": True, "name": None, "machine_name": "lab2", "answered": True,
                      "workspaces": [{"id": "~/ws:acme", "page": "/workspaces/acme", "instances": [
                          {"id": "~/erp", "parent": None, "page": "/servers/erp"},
                          {"id": "~/erp-stg", "parent": "~/erp", "page": "/servers/erp-stg"}]}], "loose": []},
                     {"id": "theirs", "here": False, "workspaces": [], "loose": []}],
        "edges": [{"from": "~/erp", "to": "~/db:machine", "kind": "database", "count": 1},
                  {"from": "~", "to": "theirs", "kind": "link", "count": 1}],
        "databases": [{"id": "~/db:machine", "kind": "machine", "machine": "~"}],
        "destinations": [{"id": "~/dest:nas", "label": "nas", "machine": "~"}],
        "jobs": [{"id": "j1", "page": "/jobs/j1", "instances": ["~/erp"], "from": "~", "to": "theirs"}],
        "flow": {"node": "~/erp", "links": {"page": "/servers/erp", "odoo": "https://erp.acme.it", "mail": None},
                 "stagings": [{"id": "~/erp-stg"}]},
    }
    found = inframap.rebase(theirs, "lab2")
    node = found["machine"]
    assert (node["id"], node["here"], node["name"], node["machine_name"]) == ("lab2", False, "lab2", "lab2")
    ws, = node["workspaces"]
    assert ws["id"] == "lab2/ws:acme" and ws["page"] == "/links/lab2/workspaces/acme"
    assert [(i["id"], i["parent"], i["page"]) for i in ws["instances"]] == [
        ("lab2/erp", None, "/links/lab2/servers/erp"), ("lab2/erp-stg", "lab2/erp", "/links/lab2/servers/erp-stg")]
    # its own links are not this machine's: their line is left out, a transfer to them has no machine here
    assert found["edges"] == [{"from": "lab2/erp", "to": "lab2/db:machine", "kind": "database", "count": 1}]
    assert found["jobs"] == [{"id": "lab2:j1", "page": "/links/lab2/jobs/j1", "instances": ["lab2/erp"],
                              "from": "lab2", "to": None}]
    assert [w["node"] for w in found["watch"]] == ["lab2/erp", "lab2", None] and found["watch"][2]["page"] == "/links/lab2/jobs/j1"
    assert found["databases"][0] == {"id": "lab2/db:machine", "kind": "machine", "machine": "lab2"}
    assert found["destinations"][0]["id"] == "lab2/dest:nas" and found["tonight"]["failed"] == 1
    assert found["flow"]["node"] == "lab2/erp" and found["flow"]["stagings"] == [{"id": "lab2/erp-stg"}]
    assert found["flow"]["links"] == {"page": "/links/lab2/servers/erp", "odoo": "https://erp.acme.it", "mail": None}
    assert inframap.rebase({"machines": []}, "lab2") is None and inframap.rebase([], "lab2") is None
    assert inframap.tonight_sum([None, {"ok": 1, "failed": 0, "unscheduled": 2}, found["tonight"]]) == {
        "ok": 3, "failed": 1, "unscheduled": 2}
    assert inframap.tonight_sum([None]) is None


async def test_a_link_that_answers_a_byte_at_a_time_does_not_hold_the_map(workspaces, monkeypatch):  # noqa: F811
    """httpx's timeout is of each phase: a link that sends its headers at
    once and then a byte a second never ends by it. The whole question has
    LIST_TIMEOUT, for the map of the machine and for a customer's stagings."""
    settings = workspaces
    settings.link_targets["goccia"] = LinkTarget("goccia", "https://goccia.example.net", "token")
    monkeypatch.setattr("omniacli.app.LIST_TIMEOUT", 0.3)

    class Drops(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(200):
                await asyncio.sleep(0.1)
                yield b" "

    class Drip(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return httpx.Response(200, headers={"content-type": "application/json"}, stream=Drops(), request=request)
    app = create_app(settings, link_transport=Drip())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=BasicAsSession(app)), base_url="http://mine.test") as c:
        for query, user in (("", {"Authorization": AUTH}), ("?machine=goccia", {"Authorization": AUTH}),
                            ("?part=now", {"Authorization": AUTH}), ("", basic("mario"))):
            started = time.monotonic()
            resp = await c.get(f"/api/map{query}", headers={**user, "Accept-Language": "it"})
            assert resp.status_code == 200 and time.monotonic() - started < 3, query
            if "Basic" not in user["Authorization"] or user["Authorization"] == AUTH:
                goccia = next(m for m in resp.json()["machines"] if m["id"] == "goccia")
                assert (goccia["answered"], goccia["older"], goccia["error"]) == (False, False, "nessuna risposta in tempo")
            else:
                # a customer: its own instances, the link is not named
                assert [m["id"] for m in resp.json()["machines"]] == ["~"] and "goccia" not in resp.text
        # the lists of the same page (the instances and the backups of a link) give up as soon
        for path in ("/api/instances?link=goccia", "/api/backups?link=goccia"):
            started = time.monotonic()
            listed = await c.get(path, headers={"Authorization": AUTH, "Accept-Language": "it"})
            assert time.monotonic() - started < 3 and listed.json()["error"] == "nessuna risposta in tempo", path


async def test_the_stagings_of_another_machines_productions_only(workspaces, client):  # noqa: F811
    """What a machine asks a link for its users of workspaces (stagings_of):
    the stagings made here of its productions and nothing else."""
    settings = workspaces
    stagings.save((settings.links_file or settings.servers_file).parent / stagings.FILE, {
        "srv": {"parent": "erp", "machine": "lab1", "created": "2026-11-21T09:00:00"}})
    data = (await client.get("/api/map?stagings_of=lab1", headers={"Authorization": AUTH})).json()
    assert set(data) == {"at", "part", "viewer", "machines"}
    machine, = data["machines"]
    ws, = machine["workspaces"]
    assert set(ws) == {"id", "instances"} and machine["loose"] == []
    srv, = ws["instances"]
    assert (srv["server"], srv["type"], srv["parent_server"], srv["parent_machine"]) == ("srv", "staging", "erp", "lab1")
    # of another machine: nothing; no job, no verdict, no other workspace either way
    other = (await client.get("/api/map?stagings_of=lab7", headers={"Authorization": AUTH})).json()
    assert other["machines"][0]["workspaces"] == [] and "beta" not in json.dumps(data) + json.dumps(other)
    # a user of workspaces asks nothing with it: its own map as ever
    own = (await client.get("/api/map?stagings_of=lab1", headers=basic("mario"))).json()
    assert "verdict" in own and own["viewer"]["scope"] == "workspace"


async def test_an_action_done_again_well_is_not_to_look_at(workspaces, client):  # noqa: F811
    jobs = client.app.state.jobs

    async def warned(job):
        job.warn("a module was skipped")

    async def fine(job):
        job.result["follows"] = "lab9:j7"
    first = jobs.submit("Update the modules of srv", ["srv/production"], "manual: admin", warned)
    await wait_job(client, first.id)
    data = (await client.get("/api/map", headers={"Authorization": AUTH})).json()
    assert [(w["kind"], w["job"]) for w in data["verdict"]["watch"] if w["kind"].startswith("job_")] == [("job_warned", first.id)]
    second = jobs.submit("Update the modules of srv", ["srv/production"], "manual: admin", fine)
    await wait_job(client, second.id)
    data = (await client.get("/api/map", headers={"Authorization": AUTH})).json()
    assert [w for w in data["verdict"]["watch"] if w["kind"].startswith("job_")] == []
    # the job of the linked machine this one waited for: said to who sees the machines, one row for the two
    assert next(j for j in data["jobs"] if j["id"] == second.id)["follows"] == "lab9:j7"
    assert next(j for j in (await client.get("/api/map", headers=basic("mario"))).json()["jobs"]
                if j["id"] == second.id)["follows"] is None
    # another action, or the same on another instance, does not clear it
    third = jobs.submit("Restart srv", ["srv/production"], "manual: admin", warned)
    await wait_job(client, third.id)
    await wait_job(client, jobs.submit("Restart srv", ["srv/test"], "manual: admin", fine).id)
    data = (await client.get("/api/map", headers={"Authorization": AUTH})).json()
    assert [w["job"] for w in data["verdict"]["watch"] if w["kind"].startswith("job_")] == [third.id]


def test_the_names_of_a_site_published_before_the_map(tmp_path):
    """A workspace published before the map kept the state of its names: its
    nginx site says which names answer on HTTPS; the DNS is not read."""
    site = """# written by omniacli workspace-helper: the names of workspace acme.

server {
    listen 80;
    listen [::]:80;
    server_name erp.acme.it www.acme.it;
    location / {
        return 301 https://$host$request_uri;
    }
}

# old.acme.it → erp.acme.it
server {
    listen 80;
    server_name old.acme.it;
    location / {
        return 503;
    }
}

# erp production
server {
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name erp.acme.it www.acme.it;
    ssl_certificate /etc/letsencrypt/live/omniacli-acme/fullchain.pem;
    location / {
        proxy_pass http://127.0.0.1:20000;
    }
}
"""
    assert inframap.site_names(site) == {"erp.acme.it": {"https": True, "dns": None}, "www.acme.it": {"https": True, "dns": None},
                                         "old.acme.it": {"https": False, "dns": None}}
    assert inframap.site_names("") == {}
    (tmp_path / "sites").mkdir()
    (tmp_path / "sites" / "omniacli-acme.conf").write_text(site, encoding="utf-8")
    names = inframap.WebNames(tmp_path / inframap.WEB_FILE, tmp_path / "sites")
    assert names.of("acme")["old.acme.it"] == {"https": False, "dns": None} and names.of("beta") == {}
    found = inframap.env_names(["erp.acme.it"], [], {"old.acme.it": "erp.acme.it"}, names.of("acme"))
    assert [(n["name"], n["https"], n["dns"]) for n in found] == [("erp.acme.it", True, None), ("old.acme.it", False, None)]
    assert inframap.bad_names(found) == ["old.acme.it"]
    # the next publication is what counts: the site is not read any more
    names.record("acme", {"names": ["erp.acme.it"], "https": True, "redirects": {}, "without_dns": ["erp.acme.it"]})
    assert names.of("acme") == {"erp.acme.it": {"https": False, "dns": "none"}}
    # without the folder of the sites (a test, a machine with no nginx): not read yet
    assert inframap.WebNames(tmp_path / "none.json").of("acme") == {}


def test_an_address_of_the_machine_is_no_address_for_the_reader():
    assert inframap.public_host("https://erp.acme.it/web") == "erp.acme.it"
    for local in ("http://localhost:20002", "http://127.0.0.1:8069/web", "http://[::1]:8069", "", None):
        assert inframap.public_host(local) == "" and inframap.public_url(local) is None
    assert inframap.public_url("https://erp.acme.it") == "https://erp.acme.it"
    now = datetime.datetime(2026, 11, 22, 10, 0, 0)
    assert inframap.seconds_since("2026-11-22T09:59:56", now) == 4 and inframap.seconds_since(None, now) is None
    # a clock put back: never a negative age; a time that is not one: none
    assert inframap.seconds_since("2026-11-22T10:00:05", now) == 0 and inframap.seconds_since("soon", now) is None


def test_why_a_link_does_not_answer_in_plain_words():
    request = httpx.Request("GET", "https://lab9.example.net/api/map")
    closed = describe_error(httpx.RemoteProtocolError("Server disconnected without sending a response.", request=request))
    assert closed == "it closed the connection without answering: OmniaCLI there is not running, or it is starting"
    assert describe_error(asyncio.TimeoutError()) == describe_error(httpx.ReadTimeout("x")) == "no answer in time"
    assert describe_error(httpx.ReadError("x")) == "the connection broke before the answer was whole"
    # whatever else: its name, without a full stop of its own (the sentence it goes in ends it)
    assert describe_error(httpx.DecodingError("bad gzip.")) == "DecodingError: bad gzip"
    assert i18n.tr("it", closed).startswith("ha chiuso la connessione senza rispondere")
    assert i18n.tr("it", "cannot connect (All connection attempts failed)").startswith("connessione non riuscita: nessun indirizzo")
