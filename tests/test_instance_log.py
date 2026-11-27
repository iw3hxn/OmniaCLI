# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The requests and Odoo's log of an instance's monitor (UX-018, AD-019 slice 2): the access lines of Odoo 16 to
20 counted with their times in 12 classes, the log's counts, the log read in its tail by offset and inode (a venv's
file by OmniaCLI, a compose project's json-file by the root helper) without losing a line to a rotation, a source
missing told as a sentence, the causes slow, errors and workers stopped for memory, their alerts grouped by cause,
and no address, query string or text of the log in what the monitor answers."""

import gzip
import io
import json
import os
import re
import time
from pathlib import Path

import pytest

from omniacli import composehelper, instancelog, instancemonitor as im, monitor as monitor_module, notify, odoolog
from omniacli.instancemonitor import InstanceHistory

from .test_app import AUTH, client  # noqa: F401 - the client fixture
from .test_delegated_scope import as_user, pair  # noqa: F401 - the pair fixture
from .test_instance_monitor import _feed
from .test_mcp import call
from .test_workspaces import post_as, workspaces  # noqa: F401 - the workspaces fixture

NOW = 1_800_000_000  # a multiple of 300 and of 3600
HEAD = "2027-01-15 08:00:00"  # time.gmtime(NOW)


def stamp(t: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t))


def access(t: int, path="/web/dataset/call_kw", status=200, sql=0.002, rest=0.010, logger="werkzeug", fourth="",
           ip="10.213.4.1", db="acme"):
    perf = f"2 {sql:.3f} {rest:.3f}{fourth}" if sql is not None else "- - -"
    return (f"{stamp(t)},459 14 INFO {db} {logger}: {ip} - - [15/Jan/2027 08:00:00] "
            f'"POST {path} HTTP/1.1" {status} - {perf}')


def record(t: int, level: str, logger: str, message: str, db="acme") -> str:
    return f"{stamp(t)},101 14 {level} {db} {logger}: {message}"


# --- what is counted -------------------------------------------------------

def test_the_access_lines_of_odoo_16_to_20():
    """Odoo 16-19's werkzeug with three time fields, 18 and 19 with a fourth, 19's #model.method, 20's
    odoo.http.server with four always; a request without times counted, not timed; the websocket not a page."""
    assert stamp(NOW) == HEAD
    counter = odoolog.Counter()
    lines = [
        access(NOW + 1),                                                     # 16-19
        access(NOW + 2, fourth=" -"),                                        # 18, 19: cursor_mode
        access(NOW + 3, path="/json/2/res.partner/search_read#res.partner.search_read", fourth=" rw"),
        access(NOW + 4, logger="odoo.http.server", fourth=" -", status=500, sql=1.0, rest=2.5),   # 20
        access(NOW + 5, sql=None),                                           # «- - -»: counted, not timed
        access(NOW + 6, path="/websocket", status=101, sql=0.0, rest=600.0),
        access(NOW + 7, db="None", path="/web/login?redirect=%2Fodoo", status=303),
        "\x1b[1;32m" + access(NOW + 8) + "\x1b[0m",
        record(NOW + 9, "INFO", "werkzeug", "Something werkzeug says that is no request"),
    ]
    for line in lines:
        counter.line(line)
    tally = counter.spans[NOW]
    assert (tally["rq"], tally["rf"], tally["tm"], tally["a"]) == (7, 1, 6, 1)
    assert tally["h"][odoolog.time_class(0.012)] == 5 and tally["h"][odoolog.time_class(3.5)] == 1
    pages = counter.hours[NOW]
    assert set(pages) == {"/web/dataset/call_kw", "/json/<n>/res.partner/search_read", "/web/login"}
    assert pages["/json/<n>/res.partner/search_read"][0] == 1


def test_the_access_lines_written_by_odoo_16_17_and_20():
    """Lines written by the images odoo:16.0, 17.0 and 20.0 on lab3 (27/11/2026, without a database: «?»); 20's
    ident is a session's short id and its SQL time an integer when 0."""
    real = [
        '{t},203 1 INFO ? werkzeug: 10.0.0.1 - - [27/Nov/2026 06:39:10] "GET /web/health HTTP/1.1" 200 - 0 0.000 0.007',
        '{t},785 1 INFO ? werkzeug: 10.0.0.1 - - [27/Nov/2026 06:39:10] "GET /web/database/selector HTTP/1.1" 200 - 0 0.000 0.580',
        '{t},045 1 INFO ? werkzeug: 10.0.0.1 - - [27/Nov/2026 06:39:12] "GET /web/login?x=1 HTTP/1.1" 303 - 0 0.000 0.002',
        '{t},596 1 INFO ? odoo.http.server: 10.0.0.1 S/eoPr91kF - [27/Nov/2026 06:39:13] "GET /web/health HTTP/1.1" 200 18 0 0 0.008 -',
        '{t},833 1 INFO ? odoo.http.server: 10.0.0.1 S/5CFvzAkG - [27/Nov/2026 06:39:13] "GET /web/database/selector HTTP/1.1" 200 70451 0 0 0.235 -',
        '{t},836 1 INFO ? odoo.http.server: 10.0.0.1 S/v2NPEGdo - [27/Nov/2026 06:39:13] "GET /nothing HTTP/1.1" 404 1484 0 0 0.002 -',
    ]
    counter = odoolog.Counter()
    for line in real:
        counter.line(line.format(t=HEAD))
    tally = counter.spans[NOW]
    assert (tally["rq"], tally["tm"], tally["rf"]) == (4, 4, 0)  # /web/health is no page (review of monitor-s2)
    assert tally["h"][odoolog.time_class(0.580)] == 1 and tally["h"][odoolog.time_class(0.235)] == 1


def test_the_log_counts():
    """Errors (a traceback counted once, by its head), warnings without the WARNING Odoo 19 writes on every
    XML-RPC call (slice 0), workers stopped for memory (16-20), the database out of memory, conflicts retried,
    mail sent and bounced."""
    counter = odoolog.Counter()
    for line in [
        record(NOW, "ERROR", "odoo.http", "Exception during request handling."),
        "Traceback (most recent call last):", '  File "x.py", line 1', "ValueError: boom",
        record(NOW, "CRITICAL", "odoo.service.server", "Failed to initialize database"),
        record(NOW, "WARNING", "odoo.models", "a field is deprecated"),
        record(NOW, "WARNING", "odoo.addons.rpc.controllers.xmlrpc",
               "The /xmlrpc, /xmlrpc/2 and /jsonrpc endpoints are deprecated in Odoo 19"),
        access(NOW, path="/xmlrpc/2/object"), access(NOW, path="/jsonrpc"),
        record(NOW, "INFO", "odoo.service.server", "Worker (812) virtual memory limit (2684354560) reached."),
        record(NOW, "WARNING", "odoo.service.server", "Server memory limit (2147483648) reached."),
        record(NOW, "INFO", "odoo.service.server", "Virtual memory limit (2684354560) reached."),       # 20
        record(NOW, "ERROR", "odoo.sql_db", "bad query: out of memory"),
        record(NOW, "INFO", "odoo.service.model", "SerializationFailure, 4 tries left, try again in 0.3 sec..."),
        record(NOW, "INFO", "odoo.addons.mail.models.mail_mail",
               "Mail (mail.mail) with ID 4 and Message-Id '<a@b>' from 'x' to (redacted) 'y' successfully sent"),
        record(NOW, "INFO", "odoo.addons.mail.models.mail_thread",
               "Routing mail from a to b with Message-Id <c>: direct write to catchall, bounce"),
    ]:
        counter.line(line)
    t = counter.spans[NOW]
    assert (t["e"], t["w"], t["k"], t["dbo"], t["s"], t["a"], t["ms"], t["mb"]) == (3, 2, 3, 1, 1, 2, 1, 1)
    assert len(t["kt"]) == 3


def test_a_pages_path_keeps_no_query_string_nor_a_token():
    assert odoolog.normalised("/my/orders/123?access_token=SECRET&x=1") == "/my/orders/<n>"
    assert odoolog.normalised("/web/image/res.partner/3/avatar_128") == "/web/image/res.partner/<n>/avatar_<n>"
    assert odoolog.normalised("/web/assets/1a2b3c4d5e6f/web.assets_web.min.js") == "/web/assets/<x>/web.assets_web.min.js"
    assert odoolog.normalised("/mail/view?token=abc") == "/mail/view"
    assert odoolog.normalised("/report/pdf/a/" + "x" * 300).startswith("/report/pdf/a/")
    assert len(odoolog.normalised("/abc" * 200)) == odoolog.PATH_MOST
    assert odoolog.normalised('/a"b<c>') == "/a_b_c_"


def test_the_median_and_one_in_20_come_from_the_classes():
    """Of two spans, one quiet (95 requests at 0.05 s) and one slow (5 at 8 s): «1 in 20» is over 5 s, not the
    mean of the two spans' own."""
    quiet, slow = [0] * odoolog.CLASSES, [0] * odoolog.CLASSES
    quiet[odoolog.time_class(0.05)] = 95
    slow[odoolog.time_class(8.0)] = 5
    both = odoolog.add_classes(quiet, slow)
    assert odoolog.quantile(both, 0.5) <= 0.1
    assert odoolog.quantile(both, 0.95) >= 5.0
    assert odoolog.over(both, 3.0) == 5 and odoolog.over(both, 1.0) == 5
    assert odoolog.quantile([0] * odoolog.CLASSES, 0.5) is None


def _points(rq, slow3=0, slow1=0, failed=0, t=NOW):
    h = [0] * odoolog.CLASSES
    h[odoolog.time_class(0.05)] = rq - slow3 - slow1
    h[odoolog.time_class(4.0)] = slow3
    h[odoolog.time_class(1.2)] = slow1
    return {"t": t, "y": "log", **{n: 0 for n in odoolog.COUNTS}, "rq": rq, "rf": failed, "tm": rq, "h": h, "kt": []}


def test_slow_and_errors_on_the_last_15_minutes():
    """Carlo's D-N1: half over 1 s or 1 in 20 over 3 s; over 1% failed; on the three spans of the last read, with
    20 requests at least; a read older than 11 minutes says nothing of now."""
    log = [_points(100, slow3=10, t=NOW - 600), _points(100, slow3=8, t=NOW - 300), _points(100, failed=4, t=NOW)]
    causes = im.log_causes(log, NOW + 100, NOW + 100)
    kinds = {c["kind"]: c for c in causes}
    assert set(kinds) == {"slow", "errors"}
    assert kinds["slow"]["p95"] >= 3.0 and kinds["errors"]["percent"] == 1.3 and kinds["errors"]["failed"] == 4
    # 1 in 20 needs 20: 30 % of 10 requests says nothing
    assert im.log_causes([_points(10, slow3=3, t=NOW)], NOW + 10, NOW + 10) == []
    # half over 1 s
    assert [c["kind"] for c in im.log_causes([_points(40, slow1=20, t=NOW)], NOW + 10, NOW + 10)] == ["slow"]
    # just under: 4% over 3 s, 1% failed
    assert im.log_causes([_points(100, slow3=4, failed=1, t=NOW)], NOW + 10, NOW + 10) == []
    # an old read
    assert im.log_causes(log, NOW + 100, NOW + 100 + im.LOG_FRESH + 1) == []
    # a span older than the 15 minutes does not count
    assert im.log_causes([_points(100, slow3=50, t=NOW - 900), _points(100, t=NOW)], NOW + 10, NOW + 10) == []


# --- a venv's file, read by OmniaCLI --------------------------------------

def _write(path: Path, lines: list[str], mode="a"):
    with open(path, mode, encoding="utf-8") as fh:
        fh.write("".join(line + "\n" for line in lines))


def test_a_rotated_venv_log_is_finished_from_its_old_inode(tmp_path):
    """logrotate's rename (and Odoo's logrotate = True's dated name): the lines written to the old file after the
    last read are read from it, then the new file from 0; one already compressed is a gap, declared; a cut short
    (copytruncate) too. A first read starts from the end."""
    log = tmp_path / "odoo.log"
    _write(log, [access(NOW - 10)] * 3)
    lines, cursor, gap, more = instancelog.read_file(log, {})
    assert lines == [] and cursor["pos"] == log.stat().st_size and gap is None
    _write(log, [access(NOW + i) for i in range(5)])
    os.rename(log, tmp_path / "odoo.log.1")
    _write(log, [access(NOW + 100 + i) for i in range(4)], mode="w")
    lines, cursor, gap, more = instancelog.read_file(log, cursor)
    assert len(lines) == 9 and gap is None and cursor["inode"] == log.stat().st_ino
    # Odoo's own rotation: a dated name
    _write(log, [access(NOW + 200)])
    os.rename(log, tmp_path / "odoo.log.2027-01-15")
    _write(log, [access(NOW + 300)], mode="w")
    lines, cursor, gap, _ = instancelog.read_file(log, cursor)
    assert len(lines) == 2 and gap is None
    # compressed before it was finished: a gap
    _write(log, [access(NOW + 400)])
    old = log.read_bytes()
    log.unlink()
    with gzip.open(tmp_path / "odoo.log.2.gz", "wb") as out:
        out.write(old)
    _write(log, [access(NOW + 500)], mode="w")
    lines, cursor, gap, _ = instancelog.read_file(log, cursor)
    assert gap == "compressed" and len(lines) == 1
    # copytruncate
    with open(log, "w"):
        pass
    _write(log, [access(NOW + 600)])
    cursor["pos"] = 10_000
    lines, cursor, gap, _ = instancelog.read_file(log, cursor)
    assert gap == "truncated" and len(lines) == 1


def test_after_a_stop_at_most_20_mb_a_round(tmp_path, monkeypatch):
    monkeypatch.setattr(instancelog, "CATCH_UP", 4096)
    monkeypatch.setattr(instancelog.logtail, "MAX_READ", 1024)
    log = tmp_path / "odoo.log"
    _write(log, [])
    _, cursor, _, _ = instancelog.read_file(log, {})
    _write(log, [access(NOW + i) for i in range(200)])
    total = 0
    for _ in range(20):
        lines, cursor, gap, more = instancelog.read_file(log, cursor, most=instancelog.CATCH_UP)
        total += len(lines)
        assert sum(len(x) + 1 for x in lines) <= instancelog.CATCH_UP + 1024
        if not more:
            break
    assert total == 200 and not more
    assert instancelog.CATCH_UP == 4096 and 20 * 1024 * 1024 == 20971520


# --- a compose project's json-file, read by the root helper ---------------

def _docker_line(text: str) -> bytes:
    return (json.dumps({"log": text + "\n", "stream": "stderr", "time": "2027-01-15T08:00:00.1Z"}) + "\n").encode()


def _helper(monkeypatch, path: Path, container="a" * 64):
    monkeypatch.setattr(composehelper, "log_container", lambda project, name: (container, path) if path else None)


def _ask(*args) -> tuple[dict, bytes]:
    out = io.BytesIO()
    assert composehelper.log_read(Path("/srv/acme"), list(args), out) == 0
    head, _, data = out.getvalue().partition(b"\n")
    fields = dict(part.split("=", 1) for part in head.decode().split()[1:])
    return fields, data


def test_the_helper_reads_by_offset_and_inode(tmp_path, monkeypatch):
    """log-read: a first read from the end; then the bytes after the offset, whole lines only; a rotated file
    (Docker's .1) finished before the new one; a compressed one a gap; a container made again read from its start;
    never a path, never more than its arguments."""
    log = tmp_path / ("a" * 64 + "-json.log")
    log.write_bytes(_docker_line(access(NOW - 5)))
    _helper(monkeypatch, log)
    head, data = _ask("odoo", "-", "0", "0")
    assert head["pos"] == str(log.stat().st_size) and data == b""
    pos, inode = int(head["pos"]), head["inode"]
    with open(log, "ab") as fh:
        fh.write(_docker_line(access(NOW + 1)) + _docker_line(access(NOW + 2)) + b'{"log":"half')
    head, data = _ask("odoo", "a" * 64, inode, str(pos))
    assert instancelog.docker_lines(data) == [access(NOW + 1), access(NOW + 2)] and head["gap"] == "-"
    pos = int(head["pos"])
    # Docker rotates: the file becomes .1, a new one is made
    with open(log, "ab") as fh:
        fh.write(b'":"x"}\n' + _docker_line(access(NOW + 3)))
    os.rename(log, log.with_name(log.name + ".1"))
    log.write_bytes(_docker_line(access(NOW + 4)))
    head, data = _ask("odoo", "a" * 64, inode, str(pos))
    assert [line for line in instancelog.docker_lines(data) if "POST" in line] == [access(NOW + 3), access(NOW + 4)]
    assert head["inode"] == str(log.stat().st_ino) and head["gap"] == "-"
    # the old file compressed: a gap, the new one from 0
    inode, pos = head["inode"], int(head["pos"])
    log.with_name(log.name + ".1").rename(log.with_name(log.name + ".1.gz"))
    os.rename(log, log.with_name(log.name + ".2.gz"))
    log.write_bytes(_docker_line(access(NOW + 5)))
    head, data = _ask("odoo", "a" * 64, inode, str(pos))
    assert head["gap"] == "compressed" and instancelog.docker_lines(data) == [access(NOW + 5)]
    # a container made again: its new file from its start, said
    _helper(monkeypatch, log, container="b" * 64)
    head, data = _ask("odoo", "a" * 64, head["inode"], head["pos"])
    assert head["gap"] == "recreated" and head["container"] == "b" * 64 and instancelog.docker_lines(data) == [access(NOW + 5)]
    # no container
    _helper(monkeypatch, None)
    assert _ask("odoo", "-", "0", "0")[0]["gap"] == "no-container"
    for bad in (["odoo", "-", "0"], ["../x", "-", "0", "0"], ["odoo", "/etc/passwd", "0", "0"],
                ["odoo", "-", "-1", "0"], ["odoo", "-", "0", "1e9"], ["odoo; rm", "-", "0", "0"]):
        with pytest.raises(composehelper.HelperError):
            composehelper.log_read(Path("/srv/acme"), bad, io.BytesIO())
    assert "log-read" in composehelper.WITH_ARGUMENTS


def test_the_helper_finishes_a_long_old_file_before_the_new_one(tmp_path, monkeypatch):
    monkeypatch.setattr(composehelper, "LOG_READ_MOST", 300)
    log = tmp_path / "c-json.log"
    log.write_bytes(b"")
    _helper(monkeypatch, log)
    head, _ = _ask("odoo", "-", "0", "0")
    inode = head["inode"]
    log.write_bytes(b"".join(_docker_line(access(NOW + i)) for i in range(6)))
    os.rename(log, log.with_name(log.name + ".1"))
    log.write_bytes(_docker_line(access(NOW + 10)))
    found, pos, more = [], "0", True
    while more:
        head, data = _ask("odoo", "a" * 64, inode, pos)
        found += instancelog.docker_lines(data)
        inode, pos, more = head["inode"], head["pos"], head["more"] == "1"
    assert found == [access(NOW + i) for i in range(6)] + [access(NOW + 10)]


# --- the reader: spans written once and again whole, the pages of an hour --

class _Inst:
    def __init__(self, log_file, key="acme/production", kind="systemd"):
        self.key, self.log_file = key, log_file
        self.server, _, self.role = key.partition("/")
        self.restart = type("R", (), {"type": kind, "service": "/nowhere"})()
        self.options = type("O", (), {"odoo_config": None})()
        self.plugin = object()


async def test_the_reader_writes_each_span_whole_and_the_pages_of_an_hour_once(tmp_path):
    history = InstanceHistory(tmp_path / "monitor")
    reader = instancelog.InstanceLog(settings=None, history=history)
    log = tmp_path / "odoo.log"
    _write(log, [])
    inst = _Inst(log)
    assert await reader.read(inst, now=NOW) == 0
    assert reader.read_state(inst.key)["state"] == instancelog.NOT_YET
    _write(log, [access(NOW + 10), access(NOW + 20, status=502)])
    await reader.read(inst, now=NOW + 100)
    _write(log, [access(NOW + 200), access(NOW + 400)])
    await reader.read(inst, now=NOW + 450)
    spans = history.points(inst.key, 0, "log")
    assert [(p["t"], p["rq"], p["rf"]) for p in spans] == [(NOW, 3, 1), (NOW + 300, 1, 0)]
    assert reader.read_state(inst.key)["state"] is None
    # the pages of the hour: kept in the cursor while the hour runs, written once it is over
    assert history.points(inst.key, 0, "pages") == []
    assert reader.open_spans(inst.key)["hours"][str(NOW)]["/web/dataset/call_kw"][0] == 4
    await reader.read(inst, now=NOW + 3600 + 301)
    pages = history.points(inst.key, 0, "pages")
    assert len(pages) == 1 and pages[0]["p"]["/web/dataset/call_kw"][0] == 4
    # the load's recorder still writes its own point for those spans: the log's records never say «written»
    assert history.last(inst.key) is None
    cursor = history.folder(inst.key) / instancelog.CURSOR
    assert oct(cursor.stat().st_mode & 0o777) == "0o600" and "10.213.4.1" not in cursor.read_text()
    for path in history.folder(inst.key).iterdir():
        assert "10.213.4.1" not in path.read_text() and "acme" not in path.read_text().replace(inst.key, "")


async def test_a_source_missing_is_a_sentence_never_a_zero(tmp_path):
    """UX-018 §6.2 and criterion 12: a venv whose Odoo writes no file; a log level that writes no access line."""
    history = InstanceHistory(tmp_path / "monitor")
    reader = instancelog.InstanceLog(settings=None, history=history)
    conf = tmp_path / "odoo.conf"
    conf.write_text("[options]\nlogfile =\n")
    inst = _Inst(tmp_path / "missing.log")
    inst.options.odoo_config = str(conf)
    await reader.read(inst, now=NOW)
    assert reader.read_state(inst.key)["state"] == instancelog.NO_FILE
    view = im.requests_view([], [], [], {}, "24h", reader.read_state(inst.key), 100, NOW, True)
    assert view["source"] == "no_file" and view["total"] is None and view["points"] == []
    tiles = im.request_tiles(view)
    assert tiles["response"] == {"value": None, "reason": "no_file"} and tiles["failed"]["value"] is None
    assert im.log_counts([], NOW, True, "no_file") == {"source": "no_file"}
    # log_level = warn in the config: no access line
    conf.write_text("[options]\nlogfile = /x/odoo.log\nlog_level = warn\n")
    assert instancelog.source(inst) == instancelog.NO_ACCESS
    # werkzeug muted some other way: lines read, the monitor's checks answered, no request
    quiet = [{**_points(1, t=NOW - 300), "rq": 0, "tm": 0, "ln": 40}]
    view = im.requests_view(quiet, [], [], {}, "24h", {"at": NOW, "state": None}, 5, NOW, True)
    assert view["source"] == "no_access" and im.request_tiles(view)["response"]["reason"] == "no_access"
    # nothing at all asked: no request, said so
    view = im.requests_view([], [], [], {}, "24h", {"at": NOW, "state": None}, 0, NOW, True)
    assert view["source"] == "none" and im.request_tiles(view)["failed"] == {"value": None, "reason": "none"}


async def test_a_job_reads_the_log_of_a_compose_instance_before_it_begins(tmp_path, monkeypatch):
    """A container made again starts a new log: the reader is a watcher of the jobs, and reads first."""
    history = InstanceHistory(tmp_path / "monitor")
    inst = _Inst(None, kind="compose")
    settings = type("S", (), {"servers": {"acme": type("V", (), {"instances": {"production": inst}})()}})()
    reader = instancelog.InstanceLog(settings=settings, history=history)
    asked = []

    async def read(instance, now=None):
        asked.append(instance.key)
        # 20 MB a read: three reads to the end of what is there (a rotation of 50 MB just before the job)
        reader.reads[instance.key] = {"more": len(asked) < 3}
        return 0
    monkeypatch.setattr(reader, "read", read)
    await reader.begin(type("J", (), {"instances": ["acme/production", "other/production"], "id": "j1"})())
    assert asked == ["acme/production"] * 3


# --- the alerts, grouped by cause ------------------------------------------

def _monitor(tmp_path, workspaces_=None):
    options = type("O", (), {"monitor_notify": ["ops@example.com"]})()
    settings = type("S", (), {"options": options, "workspaces": workspaces_ or {}})()
    mon = monitor_module.Monitor.__new__(monitor_module.Monitor)
    mon.path, mon.instances, mon.machine = tmp_path / "monitor.json", {}, {}
    return mon, settings


async def test_one_alert_for_many_instances_slow_at_once_and_not_every_5_minutes(tmp_path, monkeypatch):
    """Carlo's «Subito», grouped: two productions slow in one round are ONE email listing both; the next rounds
    while they stay slow send nothing; a third one joining sends one listing the three, the new one marked; an
    instance leaves after 3 rounds without the cause; a workspace's people read only their own instances."""
    sent = []

    async def send(options, recipients, subject, text, event=None, language=""):
        sent.append({"to": sorted(recipients), "subject": subject, "text": text, "event": event})
        return []
    monkeypatch.setattr(notify, "send", send)
    ws = {"acme": type("W", (), {"servers": ["a1", "a2"], "notify": ["it@acme.example"]})(),
          "beta": type("W", (), {"servers": ["b1"], "notify": ["it@beta.example"]})()}
    mon, settings = _monitor(tmp_path, ws)
    mon.settings, mon.causes = settings, {}
    slow = {"kind": "slow", "median": 1.4, "p95": 6.0, "requests": 300}
    await mon.instance_causes({"a1/production": [slow], "b1/production": [slow], "a2/production": []})
    assert len([s for s in sent if "ops@example.com" in s["to"]]) == 1
    machine = next(s for s in sent if "ops@example.com" in s["to"])
    assert "2 instances are slow" in machine["subject"] and "a1/production" in machine["text"] \
        and "b1/production" in machine["text"]
    assert machine["event"]["event"] == "instances.cause" and machine["event"]["event"] in notify.EVENTS
    acme_mail = next(s for s in sent if s["to"] == ["it@acme.example"])
    assert "b1/production" not in acme_mail["text"] and "a1/production is slow" in acme_mail["subject"]
    assert len(sent) == 3
    sent.clear()
    for _ in range(4):
        await mon.instance_causes({"a1/production": [slow], "b1/production": [slow], "a2/production": []})
    assert sent == []
    await mon.instance_causes({"a1/production": [slow], "b1/production": [slow], "a2/production": [slow]})
    machine = next(s for s in sent if "ops@example.com" in s["to"])
    assert "3 instances are slow" in machine["subject"]
    assert re.search(r"a2/production: .*\(new\)", machine["text"]) and not re.search(r"a1/production: .*\(new\)",
                                                                                     machine["text"])
    assert not any(s["to"] == ["it@beta.example"] for s in sent)  # nothing new for beta
    sent.clear()
    # b1 recovers: it stays in for 3 rounds without the cause, then leaves silently
    for _ in range(3):
        await mon.instance_causes({"b1/production": []})
    assert "b1/production" not in mon.causes["slow"] and sent == []
    # it comes back: told again
    await mon.instance_causes({"b1/production": [slow]})
    assert any("b1/production" in s["text"] for s in sent)


async def test_the_alert_says_its_words_in_italian(monkeypatch):
    from omniacli import i18n
    assert notify.said("it", "OmniaCLI m: [warning] 2 instances are slow") == "OmniaCLI m: [avviso] 2 istanze sono lente"
    text = "\n".join([monitor_module.CAUSE_HEADS["slow"],
                      monitor_module.CAUSE_LINES["slow"].format(key="a/production", median=1.4, p95=6.0, requests=3)
                      + " " + monitor_module.NEW_MARK])
    said = notify.said("it", text)
    assert "1 su 20 oltre 3 s" in said and "(nuova)" in said and i18n.tr("it", "Errors") == "Errori"


# --- the route, REST and MCP; who reads what ---------------------------------

def _feed_log(app, key="srv/production"):
    now = int(time.time())
    last = now // 300 * 300
    spans = [{**_points(200, slow3=20, failed=6, t=last - 300 * i), "e": 1, "w": 2, "a": 3, "k": 1 if i == 0 else 0,
              "kt": [last - 300 * i + 10] if i == 0 else []} for i in range(3)]
    spans += [{**_points(50, t=last - 86400 * 2), "e": 4}]
    pages = [{"t": last // 3600 * 3600 - 3600, "y": "pages",
              "p": {"/my/orders/<n>": [10, 40000, 9000], "/web/login": [5, 100, 30]}}]
    app.state.instance_history.add(key, spans + pages, now)
    folder = app.state.instance_history.folder(key)
    (folder / instancelog.CURSOR).write_text(json.dumps({"inode": 1, "pos": 1, "at": now - 30, "state": None,
                                                         "hours": {str(last // 3600 * 3600): {"/web/action": [2, 3000, 2000]}}}))
    app.state.instance_log.reads.pop(key, None)


async def test_the_route_rest_and_mcp_say_the_requests_and_the_log(client, monkeypatch):  # noqa: F811
    from omniacli import pgreport

    async def report(instance):
        return {"long_queries": [], "connections": 3, "max_connections": 100, "cache_hit": 99.6}
    monkeypatch.setattr(pgreport, "report", report)
    _feed(client.app)
    _feed_log(client.app)
    page = (await client.get("/api/servers/srv/production/monitor", headers={"Authorization": AUTH})).json()
    kinds = [c["kind"] for c in page["verdict"]["causes"]]
    assert page["verdict"]["state"] == "look" and {"slow", "errors", "killed"} <= set(kinds)
    tiles = page["tiles"]
    assert tiles["response"]["value"] is not None and tiles["response"]["p95"] >= 3.0
    assert tiles["failed"]["value"] == round(100 * 18 / 600, 1) and tiles["failed"]["requests"] == 600
    q = page["requests"]
    assert q["total"] == 600 and q["source"] is None and q["pages"][0]["path"] == "/my/orders/<n>"
    assert "/web/action" in [p["path"] for p in q["pages"]]
    assert page["log"]["day"] == {"errors": 3, "warnings": 6, "killed": 1, "db_memory": 0, "retried": 0, "api": 9,
                                  "mail_sent": 0, "mail_bounced": 0}
    assert page["log"]["week"]["errors"] == 7
    assert any(p["kind"] == "killed" for p in page["past"])
    rest = (await client.get("/api/v1/instances/srv/production/monitor",
                             headers={"Authorization": "Bearer mcp-test-token"})).json()
    tool = await call(client, "instance_monitor", server_name="srv", role="production")
    for found in (rest, tool):
        assert found["requests"]["total"] == 600 and found["log"]["day"]["errors"] == 3
        assert {c["kind"] for c in found["verdict"]["causes"]} == set(kinds)


async def test_a_workspaces_people_read_no_address_query_string_or_text(workspaces, client, monkeypatch, tmp_path):  # noqa: F811
    """AD-019 §8.5: no IP, query string, address or text of the log in the aggregates, for anyone, searched by value
    in the real reading of a log; the API calls only for the machine's admins and the developers; a workspace's
    user reads no slowest pages and no log."""
    from omniacli import pgreport

    async def report(instance):
        return {"long_queries": [], "connections": 3, "max_connections": 100, "cache_hit": 99.6}
    monkeypatch.setattr(pgreport, "report", report)
    app = client.app
    _feed(app)
    inst = app.state.settings.servers["srv"].instances["production"]
    log = Path(inst.log_file)
    _write(log, [], mode="a")
    await app.state.instance_log.read(inst)
    secret = ["198.51.100.77", "QSECRET-TOKEN", "boom-SECRET-TEXT", "mario.rossi@example.com", "acme_db_name"]
    now = int(time.time())
    _write(log, [access(now - 5, path="/my/orders/42?access_token=QSECRET-TOKEN", ip="198.51.100.77",
                        db="acme_db_name", status=500, sql=2.0, rest=2.0),
                 record(now - 4, "ERROR", "odoo.http", "boom-SECRET-TEXT for mario.rossi@example.com", db="acme_db_name"),
                 access(now - 3, path="/xmlrpc/2/object", ip="198.51.100.77")])
    await app.state.instance_log.read(inst)
    for who, api in ((None, True), ("mario", False), ("luca", False)):
        headers = {"Authorization": AUTH} if who is None else post_as(who)
        resp = await client.get("/api/servers/srv/production/monitor", headers=headers)
        assert resp.status_code == 200, who
        for word in [*secret, str(log), str(log.parent)]:
            assert word not in resp.text, (who, word)
        data = resp.json()
        assert data["requests"]["total"] >= 2
        if who == "luca":
            assert "log" not in data and "pages" not in data["requests"]
        else:
            assert ("api" in data["log"]["day"]) is api and "/my/orders/<n>" in [p["path"] for p in data["requests"]["pages"]]
    # a workspace's token through the REST API reads as its people do
    for word in secret:
        for path in (Path(app.state.instance_history.root)).rglob("*"):
            if path.is_file():
                assert word not in path.read_text(), (path.name, word)


async def test_the_api_row_is_for_the_machines_admins_and_the_developers(env, client):  # noqa: F811
    app = client.app
    _feed(app)
    _feed_log(app)
    admin = (await client.get("/api/servers/srv/production/monitor", headers={"Authorization": AUTH})).json()
    assert "api" in admin["log"]["day"]
    tool = await call(client, "instance_monitor", server_name="srv", role="production")
    assert "api" in tool["log"]["day"]


async def test_through_its_home_a_workspaces_admin_reads_the_same(pair, monkeypatch, tmp_path):  # noqa: F811
    """An instance placed on another machine (AD-014): its people ask through the home, the host answers with their
    scope: no API calls for its admin, no address, query string, text or folder of the log, no name of a machine."""
    from omniacli import pgreport

    async def report(instance):
        return {"long_queries": [], "connections": 3, "max_connections": 100, "cache_hit": 99.6}
    monkeypatch.setattr(pgreport, "report", report)
    pair.place()
    far = pair.far
    _feed(far)
    inst = pair.far_settings.servers["srv"].instances["production"]
    log = Path(inst.log_file)
    log.parent.mkdir(parents=True, exist_ok=True)
    _write(log, [])
    await far.state.instance_log.read(inst)
    now = int(time.time())
    _write(log, [access(now - 5, path="/my/orders/42?access_token=QSECRET-TOKEN", ip="198.51.100.77", status=500),
                 record(now - 4, "ERROR", "odoo.http", "boom-SECRET-TEXT"), access(now - 3, path="/xmlrpc/2/object")])
    await far.state.instance_log.read(inst)
    resp = await pair.get("/placed/srv/api/servers/srv/production/monitor", headers=as_user("mario"))
    assert resp.status_code == 200, resp.text[:300]
    data = resp.json()
    assert data["requests"]["total"] == 2 and "api" not in data["log"]["day"]
    for word in ("198.51.100.77", "QSECRET-TOKEN", "boom-SECRET-TEXT", str(log.parent), "far.test", "/var/", "/srv/"):
        assert word not in resp.text, word


# --- the page -----------------------------------------------------------------

async def test_the_requests_and_the_log_on_the_page(workspaces, client):  # noqa: F811
    """UX-018 §5: Requests for everyone with its two charts, the slowest pages for who acts; the log of Odoo for who
    acts, each of its numbers a way into the Log tab; closed, no primary, titles other than the text."""
    for who in ("admin", "mario", "luca"):
        page = (await client.get("/servers/srv", headers=post_as(who) if who != "admin" else {"Authorization": AUTH})).text
        panel = page[page.index('<section class="tab-panel panel mon" data-panel="monitor"'):]
        panel = panel[:panel.index("<style>")]
        assert 'data-mon="requests"' in panel and 'data-mon-chart="req"' in panel and 'data-mon-chart="time"' in panel
        assert ('data-mon-table="pages"' in panel) == (who != "luca")
        assert ('data-mon="log"' in panel) == (who != "luca")
        assert "<details open" not in panel and 'class="primary' not in panel
        for match in re.finditer(r'<(button|summary)\b([^>]*)>(.*?)</\1>', panel, flags=re.S):
            title = re.search(r'title="([^"]*)"', match.group(2))
            text = re.sub(r"<[^>]+>", "", match.group(3)).strip()
            assert title and title.group(1) and title.group(1) != text, match.group(0)[:120]
        assert "JSON-RPC" not in re.sub(r"<script.*?</script>", "", panel, flags=re.S)
    template = (Path(im.__file__).parent / "templates" / "server.html").read_text(encoding="utf-8")
    script = template[template.index("// Requests: two charts"):template.index("async function load()")]
    for word in ("alert(", "confirm(", "prompt("):
        assert word not in script
    assert "logBox().omniaLog?.show(" in template and "box.omniaLog = controller" in (
        Path(im.__file__).parent / "templates" / "_odoo_log.html").read_text(encoding="utf-8")
    # the API row is drawn only when the answer has it
    assert 'if (!(name in log.day)) continue;' in template


def test_the_history_stays_under_its_cap():
    """AD-019 19.5 re-stated: 14 days of load points and log spans and 24 pages a day stay under 5 MB."""
    point = json.dumps({"t": NOW, "n": 60, "oc": 25.5, "ocx": 40.1, "om": 410.2, "omx": 450.3, "dc": 3.1, "dcx": 9.2,
                        "dm": 120.4, "dmx": 130.5, "w": 3, "ck": 5, "up": 5}, separators=(",", ":"))
    span = json.dumps({**_points(1200, slow3=30, failed=4), "e": 2, "w": 14, "a": 30, "ln": 1500},
                      separators=(",", ":"))
    pages = json.dumps({"t": NOW, "y": "pages", "p": {f"/web/dataset/call_kw/model.{i}/method_name_{i}": [120, 4800000, 90000]
                                                      for i in range(odoolog.PAGES_KEPT)}}, separators=(",", ":"))
    day = 288 * (len(point) + 1) + 288 * 2 * (len(span) + 1) + 24 * (len(pages) + 1)
    assert 14 * day < im.CAP_BYTES, 14 * day
