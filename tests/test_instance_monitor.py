# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The monitor of an instance (UX-018, AD-019 slice 1): its Odoo and its
database apart, the machine's cluster read where it runs, 14 days of 5-minute
points in append-only daily files, the verdict on now with its causes, one
function for the page, the REST API and MCP, and the workspace's people never
reading the machine's shared PostgreSQL."""

import json
import re
import time
from collections import deque
from pathlib import Path

import pytest

from omniacli import instancemonitor as im
from omniacli import machinedb, metrics
from omniacli.instancemonitor import InstanceHistory, InstanceRecorder

from .test_app import AUTH, client  # noqa: F401 - the client fixture
from .test_mcp import call
from .test_workspaces import post_as, workspaces  # noqa: F401 - the workspaces fixture

THRESHOLDS = {"cpu": [90, 95], "ram": [85, 95], "disk": [90, 95]}
NOW = 1_800_000_000  # a fixed moment, a multiple of 300


def test_the_cluster_of_the_machine_is_read_where_it_runs(tmp_path):
    """The sampler read system-postgresql.slice while the machine's cluster runs in omniacli_machine_db.slice
    (AD-019 §0): its CPU and memory now count with PostgreSQL's."""
    assert metrics.MACHINE_DB_SLICE == machinedb.SLICE
    own = tmp_path / machinedb.SLICE
    own.mkdir()
    (own / "cpu.stat").write_text("usage_usec 4000000\n")
    (own / "memory.stat").write_text("file 900\nanon 2097152\n")
    assert metrics.postgres_cpu(tmp_path) == 4.0
    debian = tmp_path / metrics.POSTGRES_SLICE
    debian.mkdir(parents=True)
    (debian / "cpu.stat").write_text("usage_usec 1500000\n")
    (debian / "memory.stat").write_text("anon 1048576\n")
    assert metrics.postgres_cpu(tmp_path) == 5.5
    assert metrics.postgres_memory(tmp_path) == 3145728
    assert metrics.postgres_memory(tmp_path / "none") is None


def test_the_connections_by_port():
    """ss -Htn state established, one call for every instance (slice 0: /proc/net/tcp lists the TIME_WAIT too)."""
    text = ("0      0      127.0.0.1:20000    127.0.0.1:51234\n"
            "0      0      127.0.0.1:20000    127.0.0.1:51236\n"
            "0      0      [::1]:8069         [::1]:40000\n"
            "0      0      127.0.0.1:51234    127.0.0.1:20000\n")
    assert metrics.established_by_port(text) == {20000: 2, 8069: 1, 51234: 1}


class _Instance:
    def __init__(self, key="acme-odoo/production", compose=True, db_service="db"):
        self.key = key
        self.server, _, self.role = key.partition("/")
        self.restart = type("R", (), {"type": "compose" if compose else "systemd", "service": "/nowhere"})()
        self.options = type("O", (), {"odoo_service": "odoo", "db_service": db_service, "odoo_config": None})()


def test_a_compose_projects_odoo_and_database_apart(tmp_path, monkeypatch):
    """Each container's compose service says whose its CPU and anonymous memory are: Odoo's, its own
    database's; the mail catcher is neither."""
    monkeypatch.setattr(metrics, "CGROUP_ROOT", tmp_path)
    for name, usec, anon in (("odoo1", 2_000_000, 300 * 1048576), ("db1", 500_000, 100 * 1048576),
                             ("mail1", 9_000_000, 50 * 1048576)):
        (tmp_path / name).mkdir()
        (tmp_path / name / "cpu.stat").write_text(f"usage_usec {usec}\n")
        (tmp_path / name / "memory.stat").write_text(f"file 1\nanon {anon}\n")
    sampler = metrics.Sampler.__new__(metrics.Sampler)
    sampler._services = {"o" * 12: "odoo", "d" * 12: "db", "m" * 12: "mail"}
    sampler._prev = {}
    ids = ["o" * 12, "d" * 12, "m" * 12]
    cgroups = {"o" * 64: "/odoo1", "d" * 64: "/db1", "m" * 64: "/mail1"}
    inst = _Instance()
    parts = sampler._parts(inst, ids, cgroups)
    assert parts == {"o": (2.0, 300 * 1048576), "d": (0.5, 100 * 1048576)}
    point = {"cpu": 1.0, "mem": 1.0}
    sampler._split(inst, 100.0, point, parts)
    assert point["omem"] == 300.0 and point["dmem"] == 100.0 and point["ocpu"] is None
    sampler._split(inst, 110.0, point, {"o": (3.0, 1), "d": (0.6, 1)})
    assert point["ocpu"] == 10.0 and point["dcpu"] == 1.0
    # on the machine's cluster the project has no database of its own
    assert sampler._parts(_Instance(db_service="machine"), ids, cgroups) == {"o": (2.0, 300 * 1048576)}


def test_the_history_is_daily_files_only_added_to(tmp_path):
    history = InstanceHistory(tmp_path / "monitor")
    key = "acme-odoo/production"
    history.add(key, [{"t": NOW, "oc": 10}, {"t": NOW + 300, "oc": 20}], now=NOW)
    # a span written again (a restart of OmniaCLI): the last line wins, counted once
    history.add(key, [{"t": NOW + 300, "oc": 25}], now=NOW)
    folder = tmp_path / "monitor" / "acme-odoo" / "production"
    files = sorted(folder.iterdir())
    assert [f.name for f in files] == [time.strftime("%Y-%m-%d", time.gmtime(NOW)) + ".jsonl"]
    assert len(files[0].read_text().splitlines()) == 3
    assert [p["oc"] for p in history.points(key)] == [10, 25]
    assert history.last(key) == NOW + 300 and InstanceHistory(tmp_path / "monitor").last(key) == NOW + 300
    # private: the folders 700, the files 600
    assert folder.stat().st_mode & 0o777 == 0o700 and (tmp_path / "monitor").stat().st_mode & 0o777 == 0o700
    assert files[0].stat().st_mode & 0o777 == 0o600
    # a line cut by a stop is left out, the next ones are read
    with open(files[0], "a") as out:
        out.write('{"t": 18')
    history.add(key, [{"t": NOW + 600, "oc": 30}], now=NOW)
    assert [p["oc"] for p in history.points(key)] == [10, 25, 30]
    with pytest.raises(ValueError):
        history.folder("../etc/production")


def test_fourteen_days_and_the_cap(tmp_path, monkeypatch):
    history = InstanceHistory(tmp_path)
    key = "acme-odoo/production"
    for day in range(16):
        history.add(key, [{"t": NOW + day * 86400, "pad": "x" * 2000}], now=NOW + day * 86400)
    days = sorted(p.stem for p in (tmp_path / "acme-odoo" / "production").iterdir())
    # older than 14 days: gone
    assert len(days) == 15 and days[0] == time.strftime("%Y-%m-%d", time.gmtime(NOW + 86400))
    # past the cap the oldest days go, never today's
    monkeypatch.setattr(im, "CAP_BYTES", 5000)
    history.prune(key, now=NOW + 15 * 86400)
    days = sorted(p.stem for p in (tmp_path / "acme-odoo" / "production").iterdir())
    assert days == [time.strftime("%Y-%m-%d", time.gmtime(NOW + d * 86400)) for d in (14, 15)]
    assert history.started(key) == NOW + 14 * 86400


class _Sampler:
    def __init__(self, cores=4):
        self.history, self.machine, self.cores, self.workspaces = {}, deque(), cores, {}


def test_the_recorder_writes_a_point_every_5_minutes_and_a_gap_stays_a_gap(tmp_path):
    sampler = _Sampler()
    key = "acme-odoo/production"
    # samples in two spans, none in the 10 minutes between (OmniaCLI stopped)
    samples = [{"t": NOW + s, "ocpu": 20.0 + s / 60, "omem": 300.0, "dcpu": 5.0, "dmem": 100.0, "conns": 1}
               for s in range(0, 300, 5)]
    samples += [{"t": NOW + 900 + s, "ocpu": 40.0, "omem": 320.0, "dcpu": 5.0, "dmem": 100.0, "conns": 3}
                for s in range(0, 300, 5)]
    sampler.history[key] = deque(samples)
    sampler.machine = deque({"t": NOW + s, "pg": 2.0, "pg_mem": 900.0} for s in range(0, 1200, 5))
    checks = [[NOW + 60 * m, m != 2, 30] for m in range(20)]
    history = InstanceHistory(tmp_path)
    recorder = InstanceRecorder(sampler, history, checks=lambda k: checks, measured=lambda k: True)
    assert recorder.check(now=NOW + 1200 + 10) == 4
    points = history.points(key)
    assert [p["t"] - NOW for p in points] == [0, 300, 600, 900]
    first = points[0]
    assert first["n"] == 60 and first["om"] == 300.0 and first["dm"] == 100.0 and first["w"] == 1
    assert first["oc"] == pytest.approx(22.5, abs=0.1) and first["ocx"] == pytest.approx(24.9, abs=0.1)
    assert first["pc"] == 2.0 and first["pm"] == 900.0 and (first["up"], first["ck"]) == (4, 5)
    # the spans without a sample have the checks only: no zero is made of them
    assert "oc" not in points[1] and "om" not in points[2] and points[1]["ck"] == 5
    # read again: nothing written twice
    assert recorder.check(now=NOW + 1200 + 10) == 0
    # a staging stopped, a test instance: not measured
    recorder.measured = lambda k: False
    sampler.history["other/production"] = deque(samples)
    assert recorder.check(now=NOW + 1500) == 0


def _live(seconds, **values):
    return [{"t": NOW - seconds + s, **values} for s in range(0, seconds + 1, 5)]


def test_the_verdict_on_now():
    kw = dict(cores=2, memory_limit=4096, workers=4, thresholds=THRESHOLDS, minutes=5, database=[], now=NOW)
    calm = _live(900, ocpu=20.0, dcpu=5.0, omem=1000.0, dmem=200.0, conns=1)
    assert im.verdict({"state": "up"}, calm, **kw) == {"state": "good", "since": None, "causes": [], "checked": True}
    assert im.verdict({"state": "stopped", "since": "2026-11-26T09:40:00"}, calm, **kw)["state"] == "stopped"
    assert im.verdict({"state": "down", "since": "2026-11-26T10:12:00"}, calm, **kw)["since"] == "2026-11-26T10:12:00"
    assert im.verdict({"state": "unknown"}, [], **kw)["state"] == "none"
    # memory over 85% of 4 GiB for 5 minutes in a row; the processor over 90% of the 2 cores; the 4 workers busy
    hot = _live(900, ocpu=170.0, dcpu=15.0, omem=3300.0, dmem=300.0, conns=6)
    found = im.verdict({"state": "up"}, hot, **kw)
    assert found["state"] == "look"
    kinds = {c["kind"]: c for c in found["causes"]}
    assert kinds["memory"]["value"] == 88 and kinds["memory"]["section"] == "load"
    assert kinds["cpu"]["value"] == 92 and kinds["workers"]["workers"] == 4
    # one moment under the threshold within the window: not «for 5 minutes in a row»
    dip = hot[:-10] + [{**hot[-10], "omem": 100.0}] + hot[-9:]
    assert "memory" not in {c["kind"] for c in im.verdict({"state": "up"}, dip, **kw)["causes"]}
    # too few points to say (OmniaCLI just started): no cause
    assert im.verdict({"state": "up"}, hot[-3:], **kw)["causes"] == []
    # the workspace's slice over the limit, its instances together
    shared = im.verdict({"state": "up"}, calm, **{**kw, "workspace": _live(900, mem=3800.0)})
    assert shared["causes"][0]["workspace"] is True
    # without a limit granted there is no memory cause, without workers no busy ones
    assert {c["kind"] for c in im.verdict({"state": "up"}, hot, **{**kw, "memory_limit": None, "workers": 0})
            ["causes"]} == {"cpu"}
    # the database's own thresholds (UX-006 D4)
    report = {"long_queries": [{"seconds": 42}], "connections": 85, "max_connections": 100, "cache_hit": 91.0}
    causes = im.database_causes(report)
    assert [c["what"] for c in causes] == ["slow", "connections", "cache"]
    found = im.verdict({"state": "up"}, calm, **{**kw, "database": causes})
    assert found["state"] == "look" and {c["section"] for c in found["causes"]} == {"database"}
    assert im.database_causes({"long_queries": [], "connections": 3, "max_connections": 100, "cache_hit": 99.6}) == []


def test_the_series_of_a_period(tmp_path):
    history = [{"t": NOW - 86400 + i * 300, "oc": 50.0, "ocx": 80.0, "om": 500.0, "dc": 10.0, "dm": 90.0,
                "pc": 5.0, "pm": 2000.0, "ck": 5, "up": 5} for i in range(288)]
    points, interval = im.series(history, [], [], "24h", 2, 8, NOW)
    assert len(points) == 288 and interval == 300
    # percent of one core as percent of the 2 cores granted; the machine's share as percent of them too
    assert points[0]["odoo_cpu"] == 25.0 and points[0]["odoo_cpu_max"] == 40.0 and points[0]["cluster_cpu"] == 20.0
    week = [{**history[0], "t": NOW - 7 * 86400 + i * 300} for i in range(2016)]
    points, interval = im.series(week, [], [], "7d", 2, 8, NOW)
    assert len(points) <= im.MAX_POINTS and interval == 2016
    # 1 hour: the history before the sampler's first point, then the sampler's own
    live = _live(600, ocpu=30.0, omem=400.0)
    points, interval = im.series(history, live, [], "1h", 2, 8, NOW)
    assert interval == 5 and points[-1]["odoo_cpu"] == 15.0
    assert all(p["t"] + 300 <= live[0]["t"] for p in points if p["t"] < live[0]["t"])


def test_the_tiles_and_the_past():
    checks = [[NOW - 60 * m, m not in (10, 11), 20] for m in range(1, 200)]
    points = [{"t": NOW - 300, "odoo_mem": 1000.0, "odoo_mem_max": 1200.0, "db_mem": 200.0, "db_mem_max": 250.0}]
    found = im.tiles(checks, [], "24h", points, 4096, NOW)
    assert found["answered"] == {"value": round(100 * 197 / 199, 1), "checks": 199, "failed": 2}
    # the requests come from Odoo's log in slice 2: never a 0 meanwhile
    assert found["response"] == {"value": None, "reason": im.FROM_LOG} and found["failed"]["value"] is None
    # the memory now as the chart's last point (Odoo and its database), with the most of the period beside it
    assert found["memory"] == {"value": 1200.0, "max": 1450.0, "limit": 4096, "percent": 29}
    assert im.past(checks, [], "24h", NOW) == [{"kind": "down", "minutes": 2, "at": NOW - 660}]
    # 7 days: from the history's checks
    history = [{"t": NOW - 3000, "ck": 5, "up": 3}, {"t": NOW - 2700, "ck": 5, "up": 5}]
    assert im.tiles([], history, "7d", [], None, NOW)["answered"]["failed"] == 2
    assert im.past([], history, "7d", NOW) == [{"kind": "down", "minutes": 2, "at": NOW - 3000}]


def _feed(app, key="srv/production"):
    """An hour of the sampler's points and a day of the history, as a running machine has them."""
    now = int(time.time())
    sampler = app.state.sampler
    sampler.history[key] = deque({"t": now - 900 + s, "cpu": 20.0, "mem": 400.0, "procs": 3, "ocpu": 20.0,
                                  "omem": 400.0, "dcpu": None, "dmem": None, "conns": 1} for s in range(0, 900, 5))
    sampler.machine.extend({"t": now - 900 + s, "cpu": 10.0, "pg": 3.0, "pg_mem": 700.0} for s in range(0, 900, 5))
    start = now // 300 * 300 - 86400
    app.state.instance_history.add(key, [{"t": start + i * 300, "n": 60, "oc": 25.0, "ocx": 40.0, "om": 410.0,
                                          "omx": 450.0, "pc": 4.0, "pm": 650.0, "ck": 5, "up": 5}
                                         for i in range(280)])
    app.state.monitor.instances[key] = {"state": "up", "since": "2026-11-26T08:00:00",
                                        "checks": [[now - 60 * m, True, 25] for m in range(1, 100)]}


async def test_the_route_the_rest_api_and_mcp_say_the_same(client, monkeypatch):  # noqa: F811
    from omniacli import pgreport

    async def report(instance):
        return {"long_queries": [], "connections": 3, "max_connections": 100, "cache_hit": 99.6}
    monkeypatch.setattr(pgreport, "report", report)
    _feed(client.app)
    page = (await client.get("/api/servers/srv/production/monitor", headers={"Authorization": AUTH})).json()
    assert page["period"] == "24h" and page["verdict"]["state"] == "good"
    assert set(page["tiles"]) == {"answered", "response", "failed", "memory"}
    assert page["tiles"]["answered"]["value"] == 100.0 and page["tiles"]["response"]["value"] is None
    load = page["load"]
    # a venv on the machine's PostgreSQL: its admin sees every database's line, no series of its own
    assert load["database"] == {"where": "cluster"} and load["cluster"] is True
    assert all("db_cpu" not in p for p in load["points"]) and any(p.get("cluster_cpu") for p in load["points"])
    hour = (await client.get("/api/servers/srv/production/monitor?period=1h", headers={"Authorization": AUTH})).json()
    assert hour["interval"] == 5 and len(hour["load"]["points"]) <= im.MAX_POINTS
    bad = await client.get("/api/servers/srv/production/monitor?period=2d", headers={"Authorization": AUTH})
    assert bad.status_code == 400
    rest = (await client.get("/api/v1/instances/srv/production/monitor?period=7d",
                             headers={"Authorization": "Bearer mcp-test-token"})).json()
    assert rest["period"] == "7d" and rest["verdict"]["state"] == "good" and rest["load"]["cluster"] is True
    tool = await call(client, "instance_monitor", server_name="srv", role="production", period="7d")
    assert tool["period"] == "7d" and set(tool) == set(rest) and tool["load"]["database"] == rest["load"]["database"]
    assert "error" in await call(client, "instance_monitor", server_name="srv", role="production", period="1y")


async def test_a_workspaces_admin_never_reads_the_machine(workspaces, client, monkeypatch):  # noqa: F811
    """UX-018 criteria 6 and 8: for the workspace's admin no series of the shared PostgreSQL and no name or path
    of the machine, in the JSON as in the page."""
    from omniacli import pgreport

    async def report(instance):
        return {"long_queries": [], "connections": 3, "max_connections": 100, "cache_hit": 99.6}
    monkeypatch.setattr(pgreport, "report", report)
    _feed(client.app)
    resp = await client.get("/api/servers/srv/production/monitor", headers=post_as("mario"))
    assert resp.status_code == 200
    data = resp.json()
    assert data["load"]["cluster"] is False and data["load"]["database"] == {"where": "cluster"}
    assert all("cluster_cpu" not in p and "cluster_mem" not in p for p in data["load"]["points"])
    text = resp.text
    for word in ("/var/", "/srv/", "/tmp", machinedb.SLICE, "omniacli_", "localhost"):
        assert word not in text, word
    # a person of another workspace does not reach it
    assert (await client.get("/api/servers/srv/production/monitor", headers=post_as("eva"))).status_code == 403
    # the user of the workspace (UX-018 P4) reads the verdict, the tiles and the load too
    assert (await client.get("/api/servers/srv/production/monitor", headers=post_as("luca"))).status_code == 200


async def test_the_tab_is_always_there_and_quiet(workspaces, client):  # noqa: F811
    """UX-018 D-N3 and §12: the Monitoring tab for every person, even one who cannot read the database; four
    tiles, the sections closed, no primary button, every segment and section saying what it does with words other
    than its own; the data asked under the page's prefix."""
    for who in ("admin", "luca"):
        page = (await client.get("/servers/srv", headers=post_as(who))).text
        tab = re.search(r'<button type="button" role="tab" data-tab="monitor"[^>]*>', page).group(0)
        assert "hidden" not in tab
        panel = page[page.index('<section class="tab-panel panel mon" data-panel="monitor"'):]
        panel = panel[:panel.index("<style>")]
        assert 'data-api="/api/servers/srv/production/monitor"' in panel
        assert len(re.findall(r'class="tile" data-mon-tile=', panel)) == 4
        assert "<details open" not in panel and "details[open]" not in panel
        assert 'class="primary' not in panel and "primary" not in re.sub(r"<script.*?</script>", "", panel, flags=re.S)
        for match in re.finditer(r'<(button|summary)\b([^>]*)>(.*?)</\1>', panel, flags=re.S):
            title = re.search(r'title="([^"]*)"', match.group(2))
            text = re.sub(r"<[^>]+>", "", match.group(3)).strip()
            assert title and title.group(1) and title.group(1) != text, match.group(0)[:120]
        for tile in re.findall(r'<div class="tile" data-mon-tile="\w+"([^>]*)>', panel):
            assert re.search(r'title="[^"]+"', tile), tile
        assert '[data-period' not in panel or "aria-pressed" in panel
        # the database's section only for who reads it (P4 does not)
        assert ('data-mon="database"' in panel) == (who == "admin")


def test_the_page_asks_through_its_prefix_and_says_an_older_machine():
    """Drawn by the central panel through /links/<m>/: the route under the page's prefix, and a machine of a
    release before the monitor (404) told to be updated, not shown empty."""
    page = (Path(im.__file__).parent / "templates" / "server.html").read_text(encoding="utf-8")
    assert 'data-api="{{ prefix }}/api/servers/{{ server.name }}/{{ inst.role }}/monitor"' in page
    assert "resp.status === 404" in page and "does not have the monitor of its instances yet" in page
    # the period chosen is kept for the session of the browser
    assert 'sessionStorage.getItem(KEY)' in page and 'const KEY = "omniacli.instance.period"' in page


def test_the_purge_takes_the_history_away():
    postrm = (Path(im.__file__).parent.parent / "packaging" / "debian" / "files" / "postrm").read_text()
    assert "rm -rf /var/lib/omniacli/monitor" in postrm
    assert im.FOLDER == "monitor"


def test_the_workers_of_an_instance(tmp_path):
    conf = tmp_path / "odoo.conf"
    conf.write_text("[options]\nworkers = 4\n")
    venv = _Instance(compose=False)
    venv.options.odoo_config = str(conf)
    assert im.workers_of(venv) == 4
    conf.write_text("[options]\ndb_name = x\n")
    assert im.workers_of(venv) == 0
    compose = _Instance()
    assert im.workers_of(compose, lambda path: {"parameters": {"workers": 3}}) == 3
    assert im.workers_of(compose, lambda path: {"parameters": {}}) is None
    assert json.dumps(im.PERIODS) and im.DEFAULT_PERIOD == "24h"


def test_a_database_on_another_machine(tmp_path):
    """UX-018 criterion 7: on another machine, its host for the machine's admin only, no series of its own."""
    sampler = _Sampler()
    sampler.history["s/production"] = deque(_live(600, ocpu=10.0, omem=100.0, dcpu=None, dmem=None, conns=0))
    args = dict(sampler=sampler, history=InstanceHistory(tmp_path), state={"state": "up"}, checks=[],
                thresholds=THRESHOLDS, minutes=5, cores=2, memory_limit=None, workers=0, database=[], where="remote",
                host="db.example.net:5432", workspace=None, now=NOW)
    inst = _Instance("s/production", compose=False)
    admin = im.build(inst, "1h", customer=False, **args)
    assert admin["load"]["database"] == {"where": "remote", "host": "db.example.net:5432"}
    customer = im.build(inst, "1h", customer=True, **args)
    assert customer["load"]["database"] == {"where": "remote"} and "db.example.net" not in json.dumps(customer)
    assert all("db_cpu" not in p and "cluster_cpu" not in p for p in customer["load"]["points"])


# --- the review of monitor-s1 (26/11/2026) -----------------------------------------------------------------------

async def test_an_instance_stopped_on_purpose_is_not_a_fall(env, tmp_path, monkeypatch):
    """A check taken while the instance is stopped on purpose is neither an answer nor a fall (monitor.STOPPED):
    the 3 checks before the service said so, and every one after; and one found stopped while it was down."""
    import httpx

    from omniacli import monitor as monitor_module

    states = {"srv/production": "stopped"}

    async def run_state(inst):
        return states[inst.key]
    monkeypatch.setattr(type(env["settings"].servers["srv"].production.plugin), "web_port", lambda self, inst: 8069)
    jobs = type("J", (), {"is_busy": lambda self, key: False})()
    mon = monitor_module.Monitor(env["settings"], jobs, run_state, tmp_path / "monitor.json")
    inst = env["settings"].servers["srv"].production
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(502))) as client:
        for _ in range(5):
            await mon.check_instance(client, inst)
        checks = mon.instances[inst.key]["checks"]
        assert mon.view(inst.key)["state"] == "stopped" and [c[1] for c in checks] == [None] * 5
        assert mon.view(inst.key)["uptime_24h"] is None and mon.view(inst.key)["checks_24h"] == 0
        # down for real (its service runs), then stopped: from then on not a fall
        mon.instances[inst.key] = {"state": "down", "since": "x", "fails": 9, "checks": []}
        states[inst.key] = "running"
        await mon.check_instance(client, inst)
        states[inst.key] = "stopped"
        await mon.check_instance(client, inst)
    entry = mon.instances[inst.key]
    assert entry["state"] == "stopped" and [c[1] for c in entry["checks"]] == [False, None]


def test_the_checks_of_a_stopped_instance_count_nowhere(tmp_path):
    """Stopped 2 hours: «Answers» counts the checks it could answer only, nothing «did not answer», the 5-minute
    points of those hours are a gap; after its start it is well without a line on the past."""
    stopped = [[NOW - 7200 + 60 * m, None, None] for m in range(120)]
    answered = [[NOW - 600 + 60 * m, True, 20] for m in range(10)]
    checks = stopped + answered
    assert im.tiles(checks, [], "24h", [], None, NOW)["answered"] == {"value": 100.0, "checks": 10, "failed": 0}
    assert im.past(checks, [], "24h", NOW) == []
    assert im.bucket_point(NOW - 7200, [], [], stopped[:5]) is None
    sampler = _Sampler()
    recorder = InstanceRecorder(sampler, InstanceHistory(tmp_path), checks=lambda k: checks, measured=lambda k: True)
    sampler.history["s/production"] = deque()
    recorder.check(now=NOW + 10)
    kept = recorder.history.points("s/production")
    assert kept and all(p["t"] >= NOW - 600 for p in kept)
    # a period with no check counted: no percent, «Misurato dal» says where the history starts
    assert im.tiles(stopped, [], "24h", [], None, NOW)["answered"]["value"] is None
    args = dict(sampler=sampler, history=recorder.history, checks=checks, thresholds=THRESHOLDS, minutes=5, cores=2,
                memory_limit=None, workers=0, database=[], where="service", host=None, workspace=None, customer=False,
                now=NOW)
    view = im.build(_Instance("s/production"), "24h", state={"state": "up"}, **args)
    assert view["past"] == [] and view["measured_from"] == NOW - 600
    # while stopped no line on the past either
    assert im.build(_Instance("s/production"), "24h", state={"state": "stopped"}, **args)["past"] == []


def test_an_instance_the_monitor_never_asks_is_not_said_to_answer():
    """No web port, never checked (state unknown): the verdict is not «it answers»."""
    kw = dict(cores=2, memory_limit=None, workers=0, thresholds=THRESHOLDS, minutes=5, database=[], now=NOW)
    calm = _live(900, ocpu=5.0, omem=100.0)
    found = im.verdict({"state": "unknown"}, calm, **kw)
    assert found["state"] == "good" and found["checked"] is False
    assert im.verdict({"state": "up"}, calm, **kw)["checked"] is True
    page = (Path(im.__file__).parent / "templates" / "server.html").read_text(encoding="utf-8")
    assert "v.checked ? M.goodFacts : M.goodUnchecked" in page


def test_the_history_follows_its_servers_name(tmp_path):
    history = InstanceHistory(tmp_path)
    history.add("acme-odoo/production", [{"t": NOW, "oc": 1}], now=NOW)
    history.add("old/production", [{"t": NOW, "oc": 2}], now=NOW)
    assert history.last("old/production") == NOW
    # renamed: it moves, what the new name had before goes
    history.renamed("acme-odoo", "old")
    assert [p["oc"] for p in history.points("old/production")] == [1] and not (tmp_path / "acme-odoo").exists()
    # deleted: it goes, and its last time with it
    history.renamed("old", None)
    assert not (tmp_path / "old").exists() and history.last("old/production") is None
    # a server made with a name of before in another workspace starts with nothing
    history.add("beta-shop/production", [{"t": NOW, "oc": 3}], now=NOW)
    history.renamed(None, "beta-shop")
    assert history.points("beta-shop/production") == [] and history.last("beta-shop/production") is None
    with pytest.raises(ValueError):
        history.renamed("..", None)
    # the days that went take their empty folders with them
    history.add("gone/production", [{"t": NOW, "oc": 4}], now=NOW)
    history.prune_all(now=NOW + 20 * 86400)
    assert not (tmp_path / "gone").exists()


async def test_a_deleted_server_takes_its_monitor_with_it(client, env):  # noqa: F811
    from .test_app import POST

    app = client.app
    app.state.instance_history.add("srv/production", [{"t": NOW, "oc": 1}], now=time.time())
    app.state.monitor.instances["srv/production"] = {"state": "up", "checks": [[NOW, True, 5]]}
    folder = app.state.instance_history.root / "srv"
    assert folder.is_dir()
    resp = await client.post("/api/servers/delete", headers=POST, json={"name": "srv"})
    assert resp.status_code == 200, resp.text
    assert not folder.exists() and "srv/production" not in app.state.monitor.instances


def test_the_period_never_has_more_than_300_points():
    """7 and 14 days: the groups counted from the period's start, never 301 of them."""
    for period, seconds in (("7d", 7 * 86400), ("14d", 14 * 86400)):
        now = NOW + 137  # not on a 5-minute edge
        history = [{"t": (now - seconds) // 300 * 300 + i * 300, "oc": 1.0} for i in range(seconds // 300 + 2)]
        points, _ = im.series(history, [], [], period, 1, 1, now)
        assert len(points) <= im.MAX_POINTS, (period, len(points))


def test_a_period_chosen_while_a_read_is_in_flight_is_not_lost():
    page = (Path(im.__file__).parent / "templates" / "server.html").read_text(encoding="utf-8")
    script = page[page.index('const KEY = "omniacli.instance.period"'):]
    script = script[:script.index("</script>")]
    assert "if (busy || box.hidden) return;" not in script
    assert "new AbortController()" in script and "signal: asked.signal" in script


def test_the_memory_tile_and_the_chart_say_the_same():
    """The tile is the chart's last point (Odoo and its own database) and says the limit is the whole
    workspace's."""
    points = [{"t": NOW - 600, "odoo_mem": 90.0, "db_mem": 3.0}, {"t": NOW - 300, "odoo_mem": 18.0, "db_mem": 2.0}]
    found = im.tiles([], [], "24h", points, 512, NOW)["memory"]
    assert found == {"value": 20.0, "max": 93.0, "limit": 512, "percent": 4}
    page = (Path(im.__file__).parent / "templates" / "server.html").read_text(encoding="utf-8")
    assert "the workspace has {limit} for all its instances" in page


def test_the_services_of_containers_gone_are_forgotten():
    sampler = metrics.Sampler.__new__(metrics.Sampler)
    sampler._containers = {"a/production": (0.0, ["new1"])}
    sampler._services = {"old1": "odoo", "new1": "odoo"}

    async def capture(cmd, timeout=None):
        return 0, json.dumps({"ID": "new2", "State": "running", "Service": "odoo"})
    import asyncio
    from unittest.mock import patch

    inst = _Instance("a/production")
    inst.restart.command = lambda *a: ["state"]
    with patch.object(metrics.ops, "capture", capture):
        asyncio.run(sampler._container_ids(inst))
    assert sampler._services == {"new2": "odoo"}
