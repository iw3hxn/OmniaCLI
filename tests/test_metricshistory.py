# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The machine's load over 14 days (UX-008 §6.3): a point every 5 minutes
from the sampler's points, kept in a file, and the Monitoring tab's route."""
import json
import re
import time
from collections import deque
from types import SimpleNamespace

from omniacli import metricshistory
from omniacli.metricshistory import BUCKET, KEEP_SECONDS, MAX_POINTS, MetricsHistory, MetricsRecorder, bucket_point

from .test_app import AUTH, client  # noqa: F401 - the client fixture
from .test_workspaces import basic, workspaces  # noqa: F401 - the workspaces fixture

# a Monday 10:00 UTC, on a 5-minute boundary
T0 = 1_795_000_200
assert T0 % BUCKET == 0


def sample(t, cpu=10.0, ram=1000, rx=1000, tx=2000):
    return {"t": t, "cpu": cpu, "pg": None, "rx": rx, "tx": tx, "ram": ram, "ram_total": 2000}


def sampler_with(points):
    return SimpleNamespace(machine=deque(points, maxlen=720))


def test_a_span_is_its_mean_and_its_maximum():
    samples = [sample(T0 + 5 * i, cpu=10.0 if i else 100.0, ram=1000 + i) for i in range(60)]
    t, n, cpu, cpu_max, ram, ram_max, total, rx, tx = bucket_point(T0, samples)
    assert (t, n, cpu_max, ram_max, total) == (T0, 60, 100.0, 1059, 2000)
    assert cpu == round((100 + 59 * 10) / 60, 1) and ram == round(sum(range(1000, 1060)) / 60)
    # the bytes: the mean rate over the 300 seconds the 60 samples cover
    assert (rx, tx) == (1000 * 300, 2000 * 300)
    # a sample without a CPU (the first after a start) counts for the rest only
    assert bucket_point(T0, [sample(T0, cpu=None), sample(T0 + 5, cpu=40.0)])[2:4] == [40.0, 40.0]
    assert bucket_point(T0, []) is None


def test_the_recorder_makes_5_minute_points_once_a_span_has_ended(tmp_path):
    # 20 minutes of samples, one every 5 s
    sampler = sampler_with(sample(T0 + 5 * i, cpu=float(i % 60)) for i in range(240))
    history = MetricsHistory(tmp_path / "load_history.json")
    recorder = MetricsRecorder(sampler, history)
    # at 10:20:03 the last span (10:15-10:20) may still get a sample: three spans
    assert recorder.check(T0 + 1203) == 3
    assert [p[0] for p in history.points] == [T0, T0 + 300, T0 + 600]
    assert all(p[1] == 60 and p[3] == 59.0 and p[2] == 29.5 for p in history.points)
    # once it has surely ended, the fourth; asked again, nothing twice
    assert recorder.check(T0 + 1210) == 1 and recorder.check(T0 + 1240) == 0
    assert len(history.points) == 4


def test_a_gap_stays_a_gap(tmp_path):
    # 10:00-10:10 and 11:10-11:20 sampled: OmniaCLI off for an hour in between
    points = [sample(T0 + 5 * i) for i in range(120)] + [sample(T0 + 4200 + 5 * i) for i in range(120)]
    history = MetricsHistory(tmp_path / "load_history.json")
    MetricsRecorder(sampler_with(points), history).check(T0 + 4200 + 610)
    assert [p[0] - T0 for p in history.points] == [0, 300, 4200, 4500]
    shown = history.window(1, T0 + 4800)["points"]
    # no point of the hour off, and none with a zero in it
    assert len(shown) == 4 and all(p["cpu"] == 10.0 and p["rx"] == 1000 for p in shown)


def test_kept_14_days_and_bounded(tmp_path):
    history = MetricsHistory(tmp_path / "load_history.json")
    now = T0 + 15 * 86400
    # 15 days of points
    history.add([[T0 + BUCKET * i, 60, 5.0, 9.0, 100, 120, 2000, 10, 20] for i in range(15 * 288)], now)
    assert len(history.points) == MAX_POINTS == 4032
    assert history.points[0][0] >= now - KEEP_SECONDS
    size = (tmp_path / "load_history.json").stat().st_size
    # 14 days of points stay well under half a megabyte
    assert size < 400_000, size
    # the periods: 288 points a day
    assert len(history.window(1, now)["points"]) == 288
    assert len(history.window(7, now)["points"]) == 7 * 288


def test_a_restart_keeps_the_history(tmp_path):
    path = tmp_path / "load_history.json"
    history = MetricsHistory(path)
    history.begin(T0 - 100)
    MetricsRecorder(sampler_with(sample(T0 + 5 * i) for i in range(120)), history).check(T0 + 610)
    again = MetricsHistory(path)
    assert again.points == history.points and again.started == T0 - 100
    # the started time is kept across restarts
    again.begin(T0 + 9999)
    assert again.started == T0 - 100
    # the sampler of the new run holds an hour that overlaps: nothing recorded twice
    MetricsRecorder(sampler_with(sample(T0 + 5 * i) for i in range(240)), again).check(T0 + 1210)
    assert [p[0] for p in again.points] == [T0, T0 + 300, T0 + 600, T0 + 900]
    # the file is written whole: no temporary file is left
    assert not path.with_suffix(".tmp").exists() and json.loads(path.read_text())["points"]
    # a file that does not read starts a new history
    path.write_text("{not json")
    assert MetricsHistory(path).points == []


def test_the_summary():
    history = MetricsHistory.__new__(MetricsHistory)
    history.started = T0
    history.points = [[T0, 60, 10.0, 50.0, 1000, 1100, 2000, 300_000, 600_000],
                      [T0 + 300, 30, 40.0, 100.0, 1200, 1500, 2000, 150_000, 300_000],
                      [T0 + 600, 60, 20.0, 30.0, 900, 950, 2000, 300_000, 600_000]]
    found = history.window(1, T0 + 900)
    s = found["summary"]
    # the mean by samples, the highest maximum with its span
    assert s["cpu_mean"] == round((10 * 60 + 40 * 30 + 20 * 60) / 150, 1)
    assert (s["cpu_max"], s["cpu_max_t"]) == (100.0, T0 + 300)
    assert (s["ram_now"], s["ram_total"], s["ram_max"]) == (900, 2000, 1500)
    assert (s["rx_bytes"], s["tx_bytes"]) == (750_000, 1_500_000)
    # rates: the bytes over the seconds the samples cover (30 samples: 150 s)
    assert [p["rx"] for p in found["points"]] == [1000, 1000, 1000]


async def test_the_route(client, env):  # noqa: F811
    history = client.app.state.load_history
    now = time.time()
    end = int(now // BUCKET) * BUCKET
    history.points = [[end - BUCKET * i, 60, 5.0, 50.0, 1000, 1100, 2000, 300_000, 600_000]
                      for i in range(MAX_POINTS - 1, -1, -1)]
    # an hour without points (12 spans) in the middle
    del history.points[2000:2012]
    history.started = history.points[0][0]
    began = time.perf_counter()
    resp = await client.get("/api/metrics/history?days=14", headers={"Authorization": AUTH})
    took = time.perf_counter() - began
    assert resp.status_code == 200 and took < 1, took
    data = resp.json()
    assert data["interval"] == 300 and data["days"] == 14 and len(data["points"]) == MAX_POINTS - 12
    assert set(data["points"][0]) == {"t", "cpu", "cpu_max", "ram", "ram_max", "ram_total", "rx", "tx"}
    # the gap is a jump in time, not points with zeros
    steps = [b["t"] - a["t"] for a, b in zip(data["points"], data["points"][1:])]
    assert steps.count(BUCKET * 13) == 1 and all(p["cpu"] == 5.0 for p in data["points"])
    assert data["summary"]["cpu_max"] == 50.0 and data["started"] == history.started
    assert "disk" in data
    one = (await client.get("/api/metrics/history?days=1", headers={"Authorization": AUTH})).json()
    assert len(one["points"]) == 288
    bad = await client.get("/api/metrics/history?days=3", headers={"Authorization": AUTH})
    assert bad.status_code == 400 and "1, 7 or 14" in bad.json()["error"]
    # the machine's users of every role, as the machine's load of /api/metrics
    for user in ("viewer", "dev"):
        assert (await client.get("/api/metrics/history", headers=basic(user))).status_code == 200


async def test_not_for_a_workspace_user(workspaces, client, env):  # noqa: F811
    resp = await client.get("/api/metrics/history?days=14", headers=basic("mario"))
    assert resp.status_code == 403
    # nor the Monitoring tab on its Dashboard
    assert "data-period" not in (await client.get("/", headers=basic("mario"))).text


async def test_the_monitoring_tab(client, env):  # noqa: F811
    page = (await client.get("/", headers={"Authorization": AUTH})).text
    start = page.index('data-panel="monitor"')
    monitor = page[start:page.index("</section>\n</div>\n</section>", start)]
    periods = re.findall(r'<button type="button" data-period="(\w+)"[^>]*title="([^"]+)"[^>]*>([^<]+)</button>', monitor)
    assert [(p, label) for p, _, label in periods] == [
        ("hour", "Last hour"), ("1", "24 hours"), ("7", "7 days"), ("14", "14 days")]
    assert all(title and title != label for _, title, label in periods)
    # four tiles with their comparison, four charts; nothing is created here: no primary
    assert len(re.findall(r'data-hist-sub="(cpu|mem|disk|net)"', monitor)) == 4
    assert len(re.findall(r'data-hist-chart="(cpu|mem|net|disk)"', monitor)) == 4
    assert "primary" not in monitor
    # the history's route, 14 days by default, the choice kept in this browser
    assert "/api/metrics/history?days=" in page and 'let period = "14"' in page
    assert "localStorage.getItem(KEY)" in page and "omniacli.monitor.period" in page
    # Italian: the period buttons and the empty state
    it = (await client.get("/", headers={"Authorization": AUTH, "Accept-Language": "it"})).text
    assert ">Ultima ora<" in it and ">14 giorni<" in it
    assert "Lo storico parte dal {date}: il periodo di {period} si riempie da solo." in it
    assert "Il suo OmniaCLI" in it and "ultima ora" in it


def test_the_bound_is_the_contract():
    # UX-008 §6.3: 14 days of 5-minute points are 4,032 per series
    assert metricshistory.KEEP_DAYS == 14 and MAX_POINTS == 4032
