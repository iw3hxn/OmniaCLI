# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Log tab in Odoo's records (21/11/2026: the ERROR filter found the
lines of a traceback cut loose from their head, and nothing said an empty
view was empty): records grouped on real Odoo 18 and 19 logs, their counts,
a span of time checked and read from a file or through the compose helper,
the page that says an empty result."""
import datetime
import io
import os

import pytest

from omniacli import composehelper, logtail, ops

from .test_app import AUTH, client  # noqa: F401 - the fixture (env: conftest)
from .test_compose import HELPER, _server, with_logged  # noqa: F401 - the plugin with a log service

# Odoo 19 (lab1, acme-methydor-poc, 21/11/2026): a database shut down under
# the cron (logger.exception of the cron, then an uncaught exception of its
# thread that Python writes itself), then the missing Enterprise modules
ODOO19 = """\
2026-11-21 17:47:04,493 1 INFO methydor_poc odoo.sql_db: Connection to the database failed
2026-11-21 17:47:04,493 1 WARNING methydor_poc odoo.addons.base.models.ir_cron: Exception in cron:
Traceback (most recent call last):
  File "/usr/lib/python3/dist-packages/odoo/addons/base/models/ir_cron.py", line 192, in _process_jobs
    with db.cursor() as cron_cr:
         ^^^^^^^^^^^
  File "/usr/lib/python3/dist-packages/psycopg2/__init__.py", line 122, in connect
    conn = _connect(dsn, connection_factory=connection_factory, **kwasync)
psycopg2.OperationalError: connection to server at "db" (10.213.6.2), port 5432 failed: FATAL:  the database system is shutting down

2026-11-21 17:47:04,496 1 INFO ? odoo.service.server: cron0 max age (0s) reached, releasing connection.
2026-11-21 17:47:04,497 1 INFO ? odoo.sql_db: Connection to the database failed
Exception in thread odoo.service.cron.cron0:
Traceback (most recent call last):
  File "/usr/lib/python3.12/threading.py", line 1073, in _bootstrap_inner
    self.run()
psycopg2.OperationalError: connection to server at "db" (10.213.6.2), port 5432 failed: FATAL:  the database system is shutting down

2026-11-21 17:48:11,585 1 ERROR methydor_poc odoo.modules.loading: Some modules have inconsistent states, some dependencies may be missing: ['account_accountant', 'web_enterprise']
2026-11-21 17:48:11,585 1 INFO methydor_poc odoo.registry: verifying fields for every extended model
2026-11-21 17:49:12,006 1 WARNING methydor_poc odoo.addons.base.models.ir_cron: Skipping database methydor_poc because of modules to install/upgrade/remove.
""".splitlines()

# Odoo 18: a request that failed on a missing file, its exception chained
ODOO18 = """\
2026-11-20 03:12:40,101 2817 INFO prod werkzeug: 10.0.0.5 - - [20/Nov/2026 03:12:40] "GET /web/content/12 HTTP/1.1" 500 - 9 0.006 0.020
2026-11-20 03:12:40,118 2817 ERROR prod odoo.http: Exception during request handling.
Traceback (most recent call last):
  File "/opt/odoo/18.0/odoo/tools/misc.py", line 210, in file_open
    return open(path, mode)
FileNotFoundError: [Errno 2] No such file or directory: '/var/lib/odoo/filestore/prod/ab/ab12'

During handling of the above exception, another exception occurred:

Traceback (most recent call last):
  File "/opt/odoo/18.0/odoo/http.py", line 2375, in __call__
    response = request._serve_db()
odoo.exceptions.MissingError: Record does not exist or has been deleted.
2026-11-20 03:12:41,002 2817 \x1b[1;33mWARNING\x1b[0m prod odoo.models: a colored level on a terminal
2026-11-20 03:12:42,500 2817 DEBUG prod odoo.sql_db: a debug line
""".splitlines()


def test_a_traceback_belongs_to_its_head():
    found = logtail.records(ODOO19)
    assert [r["level"] for r in found] == ["INFO", "WARNING", "INFO", "INFO", "ERROR", "ERROR", "INFO", "WARNING"]
    cron = found[1]
    assert cron["lines"][0].rstrip().endswith("Exception in cron:") and cron["lines"][1].startswith("Traceback")
    assert cron["lines"][-2].startswith("psycopg2.OperationalError") and cron["lines"][-1] == ""
    # what Python wrote itself is a record of its own, an ERROR, not the rest of the INFO above it
    thread = found[4]
    assert thread["stray"] and thread["lines"][0].startswith("Exception in thread") and len(thread["lines"]) == 6
    assert found[3]["lines"] == ["2026-11-21 17:47:04,497 1 INFO ? odoo.sql_db: Connection to the database failed"]
    assert "account_accountant" in found[5]["lines"][0] and len(found[5]["lines"]) == 1
    assert logtail.counts(found) == {"INFO": 4, "WARNING": 2, "ERROR": 2}
    assert sum(len(r["lines"]) for r in found) == len(ODOO19)


def test_a_chained_exception_stays_one_record():
    found = logtail.records(ODOO18)
    assert [r["level"] for r in found] == ["INFO", "ERROR", "WARNING", "DEBUG"]
    error = found[1]
    assert len(error["lines"]) == 12 and not error.get("stray")
    assert any(line.startswith("FileNotFoundError") for line in error["lines"])
    assert error["lines"][-1].startswith("odoo.exceptions.MissingError")
    # the terminal's colours are taken away
    assert found[2]["lines"][0] == "2026-11-20 03:12:41,002 2817 WARNING prod odoo.models: a colored level on a terminal"


def test_lines_before_the_first_head_are_the_rest_of_a_record():
    found = logtail.records(ODOO18[4:9] + ODOO18[-1:])
    assert found[0]["level"] is None and found[0]["lines"][0].startswith("    return open")
    assert [r["level"] for r in found] == [None, "DEBUG"]
    assert logtail.counts(found) == {"DEBUG": 1}
    assert logtail.records([]) == [] and logtail.records(["plain", "text"]) == [{"level": None, "lines": ["plain", "text"]}]


NOW = datetime.datetime(2026, 11, 21, 18, 0, tzinfo=datetime.timezone.utc)


def test_the_span_is_checked():
    start, end = logtail.parse_span("2026-11-21T00:00:00Z", "2026-11-21T07:00Z", NOW)
    assert (start.hour, end.hour, end.tzinfo) == (0, 7, datetime.timezone.utc)
    assert logtail.parse_span("2026-11-21T17:00:00Z", "", NOW)[1] == NOW
    for since, until, message in (("yesterday", "", "UTC time"), ("2026-11-21 00:00:00", "", "UTC time"),
                                  ("2026-11-21T00:00:00", "", "UTC time"), ("2026-02-30T00:00:00Z", "", "not a date"),
                                  ("2026-11-21T07:00:00Z", "2026-11-21T06:00:00Z", "before until"),
                                  ("2026-11-22T00:00:00Z", "2026-11-23T00:00:00Z", "future"),
                                  ("2026-09-01T00:00:00Z", "", "at most 31 days"),
                                  ("2026-11-21T00:00:00Z", "$(reboot)", "UTC time"),
                                  ("2026-11-21T00:00:00Z;x", "", "UTC time")):
        with pytest.raises(ValueError, match=message):
            logtail.parse_span(since, until, NOW)
    # an end past now is now (and a minute)
    assert logtail.parse_span("2026-11-21T17:00:00Z", "2026-11-21T23:00:00Z", NOW)[1] == NOW + datetime.timedelta(minutes=1)


@pytest.fixture
def utc_machine(monkeypatch):
    """The machine's clock in UTC, so that Odoo's times in a file read as UTC."""
    monkeypatch.setenv("TZ", "UTC")
    import time
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def big_log(path, hours=10, every=1):
    """A log of `hours` hours from 2026-11-21 00:00, a record every `every`
    seconds, an ERROR with its traceback on every whole hour."""
    with open(path, "w") as fh:
        moment = datetime.datetime(2026, 11, 21)
        end = moment + datetime.timedelta(hours=hours)
        while moment < end:
            stamp = moment.strftime("%Y-%m-%d %H:%M:%S")
            if moment.minute == 0 and moment.second < every:
                fh.write(f"{stamp},000 7 ERROR db odoo.http: failed at {stamp}\nTraceback (most recent call last):\n"
                         "FileNotFoundError: x\n")
            else:
                fh.write(f"{stamp},000 7 INFO db werkzeug: GET /web {stamp}\n")
            moment += datetime.timedelta(seconds=every)


def span(text_from, text_to):
    return logtail.parse_span(text_from, text_to, datetime.datetime(2026, 11, 22, tzinfo=datetime.timezone.utc))


def test_a_span_of_a_file(tmp_path, utc_machine):
    log = tmp_path / "odoo.log"
    big_log(log)
    assert os.path.getsize(log) > 10 * logtail._BLOCK  # the halving is what finds the start
    got = logtail.read_span(log, *span("2026-11-21T03:00:00Z", "2026-11-21T03:10:00Z"))
    found = logtail.records(got["lines"])
    assert found[0]["lines"][0].startswith("2026-11-21 03:00:00,000 7 ERROR") and len(found[0]["lines"]) == 3
    assert found[-1]["lines"][0] < "2026-11-21 03:10:00" and not got["truncated"]
    assert got["total"] == len(got["lines"]) == 600 + 2
    assert logtail.counts(found) == {"ERROR": 1, "INFO": 599}
    # past the cap: the last lines of the span, or the first
    last = logtail.read_span(log, *span("2026-11-21T01:00:00Z", "2026-11-21T03:00:00Z"), cap=100)
    assert last["truncated"] and last["total"] > 1000 and len(last["lines"]) == 100
    assert last["lines"][-1].startswith("2026-11-21 02:59:5")
    first = logtail.read_span(log, *span("2026-11-21T01:00:00Z", "2026-11-21T03:00:00Z"), keep="first", cap=100)
    assert first["truncated"] and len(first["lines"]) == 100 and first["lines"][0].startswith("2026-11-21 01:00:00,000 7 ERROR")
    # nothing in the span, and a file without Odoo's times
    assert logtail.read_span(log, *span("2026-11-21T12:00:00Z", "2026-11-21T13:00:00Z")) == {
        "lines": [], "total": 0, "truncated": False, "timed": True}
    plain = tmp_path / "plain.log"
    plain.write_text("no time here\n" * 50)
    assert not logtail.read_span(plain, *span("2026-11-21T00:00:00Z", "2026-11-21T01:00:00Z"))["timed"]
    assert logtail.local_to_iso("2026-11-21 03:00:00") == "2026-11-21T03:00:00Z"


async def test_the_records_route_of_a_file(client, env, utc_machine):  # noqa: F811
    log = env["settings"].instance("srv", "test").log_file
    log.write_text("\n".join(ODOO19) + "\n")
    url = "/api/servers/srv/test/log/records"
    got = (await client.get(url, headers={"Authorization": AUTH})).json()
    assert got["counts"] == {"INFO": 4, "WARNING": 2, "ERROR": 2} and got["lines"] == len(ODOO19)
    assert got["first"] == "2026-11-21T17:47:04Z" and got["last"] == "2026-11-21T17:49:12Z"
    assert got["records"][1]["lines"][1].startswith("Traceback")
    # follow: what is written after, the rest of a record joined by the page
    with open(log, "a") as fh:
        fh.write("2026-11-21 17:50:00,000 1 ERROR methydor_poc odoo.http: boom\nTraceback (most recent call last):\n")
    nxt = (await client.get(f"{url}?pos={got['pos']}&inode={got['inode']}", headers={"Authorization": AUTH})).json()
    assert nxt["records"] == [{"level": "ERROR", "lines": ["2026-11-21 17:50:00,000 1 ERROR methydor_poc odoo.http: boom",
                                                            "Traceback (most recent call last):"]}]
    # a span
    got = (await client.get(f"{url}?since=2026-11-21T17:48:00Z&until=2026-11-21T17:49:00Z",
                            headers={"Authorization": AUTH})).json()
    assert got["counts"] == {"ERROR": 1, "INFO": 1} and got["since"] == "2026-11-21T17:48:00Z" and got["keep"] == "last"
    for query, message in (("since=yesterday", "UTC time"), ("since=2026-11-21T17:48:00Z&keep=all", "first or last"),
                           ("until=2026-11-21T17:48:00Z", "UTC time")):
        bad = await client.get(f"{url}?{query}", headers={"Authorization": AUTH})
        assert bad.status_code == 400 and message in bad.json()["error"]


async def test_the_records_route_of_a_compose_instance(client, env, monkeypatch):  # noqa: F811
    asked = []

    async def compose_span(instance, since, until, keep, cap):
        asked.append((since.isoformat(), until.isoformat(), keep, cap))
        return {"lines": ODOO18[1:3], "total": 40000, "truncated": True, "first": "2026-11-20T03:12:40Z",
                "last": "2026-11-20T03:12:40Z"}

    monkeypatch.setattr(ops, "compose_span", compose_span)
    env["settings"].instance("srv", "test").log_file = None
    got = (await client.get("/api/servers/srv/test/log/records?since=2026-11-20T00:00:00Z&until=2026-11-20T07:00:00Z",
                            headers={"Authorization": AUTH})).json()
    assert asked == [("2026-11-20T00:00:00+00:00", "2026-11-20T07:00:00+00:00", "last", logtail.MAX_LINES)]
    assert got["truncated"] and got["total"] == 40000 and got["counts"] == {"ERROR": 1} and got["first"] == "2026-11-20T03:12:40Z"


T0 = "2026-11-20T00:00:00.000000Z"
T7 = "2026-11-20T07:00:00.000000Z"


def test_the_logs_action_reads_a_span(tmp_path, monkeypatch):
    project = tmp_path
    monkeypatch.setattr(composehelper, "services", lambda project: ["odoo", "db"])
    assert composehelper.commands(project, "logs", ["odoo", "--since", T0, "--until", T7, "--last", "10000"]) == [
        [*composehelper.LOGS, "--since", T0, "--until", T7, "odoo"]]
    assert composehelper.logs_options(project, ["odoo", "--since", T0, "--first", "5"])[2] == ("first", 5)
    assert composehelper.logs_options(project, ["odoo", "--tail", "300"]) == ("odoo", ["--tail", "300"], None)
    for args, message in ((["odoo", "--since", T0, "--until", "now"], "UTC time"),
                          (["odoo", "--tail", "5", "--since", T0], "alone"),
                          (["odoo", "--until", T7], "usage"),
                          (["odoo", "--since", T0, "--last", "10001"], "1 to 10000"),
                          (["odoo", "--since", T0, "--first", "5", "--last", "5"], "not both"),
                          (["odoo", "--since", T0, "--since", T7], "usage"),
                          (["odoo", "--since", T0, "--until"], "usage"),
                          (["odoo", "--since", T0, "--follow", "1"], "usage")):
        with pytest.raises(composehelper.HelperError, match=message):
            composehelper.logs_options(project, args)


@pytest.mark.parametrize("keep, count, kept, total", [("last", 3, ["l8", "l9", "l10"], "10"),
                                                      ("first", 3, ["l1", "l2", "l3"], "more"),
                                                      ("first", 20, [f"l{n}" for n in range(1, 11)], "10")])
def test_the_helper_cuts_a_span(tmp_path, monkeypatch, keep, count, kept, total):
    out = io.BytesIO()
    monkeypatch.setattr(composehelper, "compose", lambda project, *cmd: ["seq", "-f", "l%g", "10"])
    monkeypatch.setattr(composehelper.sys, "stdout", type("Out", (), {"buffer": out})())
    assert composehelper.logs_cut(tmp_path, ["logs"], (keep, count)) == 0
    lines = out.getvalue().decode().splitlines()
    assert lines[:-1] == kept and lines[-1] == f"#omniacli-logs total={total} kept={len(kept)}"


async def test_compose_span(with_logged, monkeypatch):  # noqa: F811
    answers, asked = [], []

    async def capture(cmd, **kwargs):
        asked.append(cmd)
        return answers.pop(0)

    monkeypatch.setattr(ops, "capture", capture)
    instance = _server("logged").test
    since, until = (datetime.datetime(2026, 11, 20, h, tzinfo=datetime.timezone.utc) for h in (0, 7))
    answers.append((0, f"2026-11-20T03:12:40.118Z {ODOO18[1]}\n2026-11-20T03:12:40.119Z {ODOO18[2]}\n"
                       "#omniacli-logs total=12000 kept=2"))
    got = await ops.compose_span(instance, since, until, "last", 2)
    assert asked[-1] == ["sudo", "-n", *HELPER, "logs", "odoo", "--since", T0, "--until", T7, "--last", "2"]
    assert got == {"lines": ODOO18[1:3], "total": 12000, "truncated": True,
                   "first": "2026-11-20T03:12:40Z", "last": "2026-11-20T03:12:40Z"}
    answers.append((0, "#omniacli-logs total=more kept=0"))
    assert (await ops.compose_span(instance, since, until, "first", 2))["total"] is None
    answers.append((2, "compose-helper: --until takes a UTC time"))
    with pytest.raises(ops.LogUnavailable, match="UTC time"):
        await ops.compose_span(instance, since, until)


async def test_the_log_tab_says_an_empty_result(client):  # noqa: F811
    page = (await client.get("/servers/srv?view=test", headers={"Authorization": AUTH, "Cookie": "omniacli_lang=it"})).text
    assert "/api/servers/srv/test/log/records" in page and "OmniaOdooLog.attach(" in page
    assert "Nessuna riga {what} nelle ultime {lines} righe, dalle {time} del {date}" in page
    assert "Carica prima" in page and "Stanotte 00-07" in page and "Copia il blocco" in page
    assert "window.whenText(" in page
