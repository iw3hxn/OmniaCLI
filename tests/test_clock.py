# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Dates and times (clock.py, README «Dates and times»): the moments OmniaCLI writes by
its machine's clock are said in the reader's time zone, on the machine's own pages, on
a linked machine's pages through the link and in the lists that join several machines;
the summer time changes of Europe/Rome; the strings kept stay naive for their readers."""

import datetime
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import httpx
import pytest
import yaml

from omniacli import clock, i18n, notify
from omniacli.app import _TEMPLATES, create_app
from omniacli.config import parse_settings

from .conftest import LINK_TOKEN, USER_HASH, BasicAsSession
from .test_links import HOST, Network, as_user

ROME, UTC = "Europe/Rome", "UTC"


@pytest.fixture
def zone_of_process():
    """The process's TZ set for a test (the clock Python writes by), as it was after."""
    before = os.environ.get("TZ")

    def use(value):
        if value is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = value
        time.tzset()
        clock.machine_zone.cache_clear()
    yield use
    use(before)


def test_the_zone_of_the_machine(zone_of_process):
    zone_of_process(ROME)
    assert clock.machine_zone() == ROME
    zone_of_process(":Europe/Rome")
    assert clock.machine_zone() == ROME
    for name in ("UTC", "Etc/UTC", "/usr/share/zoneinfo/Etc/UTC"):
        zone_of_process(name)
        assert clock.machine_zone() == UTC, name
    # a TZ with no name of the database (POSIX rules): its offset of now, which a browser reads as well
    zone_of_process("XYZ-3")
    assert clock.machine_zone() == "+03:00"
    assert clock.tzinfo_of("../../etc/passwd") is None and clock.tzinfo_of("Nowhere/Land") is None
    assert clock.tzinfo_of("+19:00") is None and clock.tzinfo_of("-05:30") is not None


def test_a_moment_moved_between_two_clocks():
    # 08:59 by a server in UTC is 09:59 in Rome in winter, 10:59 in summer: the offset of that moment
    assert clock.moved("2026-11-23T08:59:12", UTC, ROME) == "2026-11-23T09:59:12"
    assert clock.moved("2026-07-23 08:59", UTC, ROME) == "2026-07-23 10:59"
    assert clock.moved("2026-11-23T08:59:12.123456", UTC, "+05:30") == "2026-11-23T14:29:12.123456"
    # anything else, or a zone not known: as it is
    for text in ("2026-11-23", "08:59", "srv_1.zip", "19.0.1.2", "2026-11-23T08:59:12Z"):
        assert clock.moved(text, UTC, ROME) == text
    assert clock.moved("2026-11-23T08:59", "Nowhere/Land", ROME) == "2026-11-23T08:59"


def test_the_summer_time_changes_of_rome():
    """The last Sunday of March (29/03/2026: 02:00 → 03:00) and of October (25/10/2026: 03:00 → 02:00)."""
    # the hour that comes twice: the first (summer time, fold 0), as Python reads a naive time
    assert clock.moved("2026-10-25T02:30", ROME, UTC) == "2026-10-25T00:30"
    assert clock.moved("2026-10-25T01:59", ROME, UTC) == "2026-10-24T23:59"
    assert clock.moved("2026-10-25T03:00", ROME, UTC) == "2026-10-25T02:00"
    # the hour that does not exist: the offset before the change
    assert clock.moved("2026-03-29T02:30", ROME, UTC) == "2026-03-29T01:30"
    assert clock.moved("2026-03-29T01:59", ROME, UTC) == "2026-03-29T00:59"
    assert clock.moved("2026-03-29T03:00", ROME, UTC) == "2026-03-29T01:00"
    # and back: from UTC, each side of the change
    assert clock.moved("2026-10-25T00:59", UTC, ROME) == "2026-10-25T02:59"
    assert clock.moved("2026-10-25T01:00", UTC, ROME) == "2026-10-25T02:00"
    assert clock.moved("2026-03-29T00:59", UTC, ROME) == "2026-03-29T01:59"
    assert clock.moved("2026-03-29T01:00", UTC, ROME) == "2026-03-29T03:00"


def test_a_link_answer_on_this_machines_clock_stays_naive_for_its_readers():
    answer = {"rows": [{"time": "2026-11-23T08:52:11", "file": "odoo_20261123_085211.zip", "size": 3},
                       {"time": "2026-07-01T23:30:00", "verified": {"time": "2026-07-02T00:10:00", "ok": True}}],
              "at": "2026-11-23 08:59", "version": "2026.1.5490", "day": "2026-11-23"}
    here = clock.to_here(answer, UTC, ROME)
    assert here["rows"][0] == {"time": "2026-11-23T09:52:11", "file": "odoo_20261123_085211.zip", "size": 3}
    assert here["rows"][1]["time"] == "2026-07-02T01:30:00" and here["rows"][1]["verified"]["time"] == "2026-07-02T02:10:00"
    assert here["at"] == "2026-11-23 09:59" and here["version"] == "2026.1.5490" and here["day"] == "2026-11-23"
    # what the code reads them with (fromisoformat, comparisons with now(), sorting): naive, as the stored ones
    for value in (here["rows"][0]["time"], here["rows"][1]["verified"]["time"], here["at"]):
        moment = datetime.datetime.fromisoformat(value)
        assert moment.tzinfo is None and moment < datetime.datetime.now() + datetime.timedelta(days=3650)
    # an older OmniaCLI says no zone, or the same zone: as it answered
    assert clock.to_here(answer, None, ROME) is answer and clock.to_here(answer, ROME, ROME) is answer


def test_what_the_machine_writes_stays_naive():
    """The 28 readers (fromisoformat of a job's created, a schedule's run, a staging's created…) compare
    with datetime.now(): what is written must stay naive, as before."""
    from omniacli import jobs
    written = jobs._now() if hasattr(jobs, "_now") else datetime.datetime.now().isoformat()
    assert datetime.datetime.fromisoformat(str(written)).tzinfo is None


def test_a_text_with_a_moment_in_the_readers_zone():
    here, viewer = clock.HERE.set(UTC), clock.VIEWER.set(ROME)
    i18n._tr.cache_clear()
    try:
        assert clock.for_viewer("Copied on 2026-11-23T08:41:00") == "Copied on 2026-11-23T09:41:00"
        # with Z or an offset: as it says
        assert clock.for_viewer("at 2026-11-23T08:41:00Z and 2026-11-23T08:41:00+02:00") == \
            "at 2026-11-23T09:41:00 and 2026-11-23T07:41:00"
        assert clock.shown("2026-11-23T08:59:12") == "23/11/2026 09:59" and clock.shown("") == ""
        # a text of several lines, a job's lines: each moment moved once
        text = "first 2026-11-23T08:00:00\n14:10:09 last 2026-11-23T08:30:00"
        assert i18n.tr("en", text) == "first 2026-11-23T09:00:00\n14:10:09 last 2026-11-23T09:30:00"
        assert i18n.tr("it", "since 2026-11-23 08:30") .endswith("09:30")
        # the filter of the pages
        page = _TEMPLATES.env.from_string("{{ x | moment }} · {{ y | moment }}").render(x="2026-07-23T08:59:00", y=None)
        assert page == "23/07/2026 10:59 · "
        # seconds since the epoch, as the pages show the passkeys and the invitations
        assert clock.epoch_text(datetime.datetime(2026, 11, 23, 8, 59, tzinfo=datetime.timezone.utc).timestamp()) \
            == "23/11/2026 09:59"
    finally:
        clock.HERE.reset(here)
        clock.VIEWER.reset(viewer)
        i18n._tr.cache_clear()
    # no reader's zone (a job, an email): as it is
    assert clock.for_viewer("Copied on 2026-11-23T08:41:00") == "Copied on 2026-11-23T08:41:00"


def test_an_email_says_the_zone_of_its_times(monkeypatch):
    monkeypatch.setattr(clock, "machine_zone", lambda: UTC)
    assert notify.with_zone("OmniaCLI srv: backup", "No time here") == "No time here"
    said = notify.with_zone("OmniaCLI srv: backup failed", "The backup of 08:52 failed")
    assert said.endswith("(UTC).") and "Times are by the clock of " in said
    assert notify.payload("s", "t", None)["zone"] == UTC
    assert i18n.tr("it", said.split("\n")[-1]).startswith("Gli orari sono secondo l'orologio di ")


@pytest.fixture
async def two_zones(env, tmp_path, monkeypatch):
    """A customer's machine in UTC («ber») linked from a central in Europe/Rome."""
    monkeypatch.setattr(clock, "machine_zone", lambda: UTC)
    customer = create_app(env["settings"])
    conf = tmp_path / "central"
    conf.mkdir()
    (conf / "links.yaml").write_text(yaml.safe_dump({"links": {"ber": {"url": "http://customer.test", "token": LINK_TOKEN}}}))
    settings = parse_settings({
        "auth": {"users": {"admin": USER_HASH, "dev": {"password": USER_HASH, "role": "developer"}}},
        "backup_dir": str(tmp_path / "central-backups"), "jobs_dir": str(tmp_path / "central-jobs"),
        "servers_file": "servers.yaml",
    }, base_dir=conf)
    monkeypatch.setattr(clock, "machine_zone", lambda: ROME)
    network = Network(customer)
    app = create_app(settings, link_transport=network)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=BasicAsSession(app)), base_url=f"http://{HOST}") as c:
        c.network, c.env = network, env
        yield c


async def test_each_machine_says_its_zone_and_the_link_carries_the_readers(two_zones):
    reader = {**as_user("dev"), "Cookie": f"{clock.COOKIE}=Europe%2FRome"}
    page = await two_zones.get("/", headers=reader)
    assert page.headers[clock.HEADER] == ROME and 'window.serverZone = "Europe/Rome";' in page.text
    # the linked machine's page through the link: drawn there, with its zone; the reader's zone passed on
    there = await two_zones.get("/links/ber/", headers=reader)
    assert there.status_code == 200 and 'window.serverZone = "UTC";' in there.text
    assert there.headers[clock.HEADER] == UTC
    assert two_zones.network.requests[-1].headers[clock.VIEWER_HEADER] == ROME
    # an answer of its API too
    assert (await two_zones.get("/links/ber/api/servers", headers=reader)).headers[clock.HEADER] == UTC


async def test_a_list_of_two_machines_on_one_clock(two_zones):
    folder = Path(two_zones.env["settings"].backup_dir) / "srv" / "production"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "odoo_20261123_085211.zip").write_bytes(b"x")
    stamp = datetime.datetime(2026, 11, 23, 8, 52, 11).timestamp()
    os.utime(folder / "odoo_20261123_085211.zip", (stamp, stamp))
    reader = as_user("dev")
    # its own list, through the link: by its clock (the page there converts it with its zone)
    own = (await two_zones.get("/links/ber/api/backups", headers=reader)).json()
    assert own["rows"][0]["time"] == "2026-11-23T08:52:11"
    # the central's list of its rows: on the central's clock, as the central's own rows
    joined = (await two_zones.get("/api/backups?link=ber", headers=reader)).json()
    assert joined["rows"][0]["time"] == "2026-11-23T09:52:11" and joined["rows"][0]["file"] == "odoo_20261123_085211.zip"


def _moments_script(html: str) -> str:
    start = html.index("window.serverZone = ")
    end = html.index("// a size as people write it", start)
    return html[start:end]


NODE_CHECKS = r"""
const results = {};
const say = (name, value) => { results[name] = value; };
window.serverZone = "UTC";
// Carlo's case on ber (UTC), in Rome: 08:59 there is 09:59 here, and in summer 10:59
say("winter", whenText("2026-11-23T08:59:12"));
say("summer", whenText("2026-07-23 08:59"));
say("asWritten", whenText("2026-11-23T08:59:12", ""));
say("aware", whenText("2026-11-23T08:59:12Z", "Europe/Rome"));
say("offset", whenText("2026-11-23T08:59:12", "+05:30"));
say("date", whenText(new Date(Date.UTC(2026, 10, 23, 8, 59))));
say("notMoment", whenText("srv_1.zip"));
// the minute of the recovery: shown and typed by the reader's clock, sent by the server's
say("pitrMax", localWall("2026-11-23T08:57:00"));
say("pitrMin", localWall("2026-11-20T10:40:00"));
say("pitrSent", serverWall(new Date("2026-11-23T09:35")));
say("pitrInRange", "2026-11-23T09:35" <= localWall("2026-11-23T08:57:00"));
// a job's lines: moved as on the day it started
say("shift", clockShift("2026-11-23T08:00:00"));
say("stamp", shiftedTime("23:30:05", clockShift("2026-11-23T08:00:00")));
// a time of every day: typed by the reader's clock, kept by the server's
say("daily", [toYourTime("02:00"), toServerTime(toYourTime("02:00"))]);
// a server in Rome read in UTC: the summer time changes
window.serverZone = "Europe/Rome";
say("octTwice", whenText("2026-10-25T02:30", "Europe/Rome"));
say("marMissing", whenText("2026-03-29T02:30", "Europe/Rome"));
say("marAfter", whenText("2026-03-29T03:00", "Europe/Rome"));
say("octAfter", whenText("2026-10-25T03:00", "Europe/Rome"));
console.log(JSON.stringify(results));
"""


def _node(script: str, tz: str) -> dict:
    stub = "const window = globalThis; const document = {cookie: '', addEventListener() {}, querySelector() { return null; }};\n"
    found = subprocess.run(["node", "-e", stub + script + NODE_CHECKS], capture_output=True, text=True,
                           env={**os.environ, "TZ": tz}, timeout=60)
    assert found.returncode == 0, found.stderr
    return json.loads(found.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
async def test_the_page_says_the_moments_in_the_readers_zone(two_zones):
    script = _moments_script((await two_zones.get("/", headers=as_user("dev"))).text)
    rome = _node(script, ROME)
    assert rome["winter"] == "23/11/2026 09:59" and rome["summer"] == "23/07/2026 10:59"
    assert rome["asWritten"] == "23/11/2026 08:59" and rome["aware"] == "23/11/2026 09:59"
    assert rome["offset"] == "23/11/2026 04:29" and rome["date"] == "23/11/2026 09:59" and rome["notMoment"] == "srv_1.zip"
    assert rome["pitrMax"] == "2026-11-23T09:57" and rome["pitrMin"] == "2026-11-20T11:40"
    assert rome["pitrSent"] == "2026-11-23T08:35" and rome["pitrInRange"] is True
    assert rome["shift"] == 60 and rome["stamp"] == "00:30:05"
    assert rome["daily"][0] != "02:00" and rome["daily"][1] == "02:00"
    utc = _node(script, UTC)
    assert utc["winter"] == "23/11/2026 08:59" and utc["shift"] == 0 and utc["daily"] == ["02:00", "02:00"]
    assert utc["octTwice"] == "25/10/2026 00:30" and utc["octAfter"] == "25/10/2026 02:00"
    assert utc["marMissing"] == "29/03/2026 01:30" and utc["marAfter"] == "29/03/2026 01:00"
    # no moment in it is written by hand any more (the pages draw them with whenText or the filter moment)
    pages = Path(clock.__file__).parent / "templates"
    for path in pages.glob("*.html"):
        text = path.read_text()
        assert not re.search(r"\[8:10\] \}\}/\{\{ [a-z.]+\[5:7\]", text), path.name
