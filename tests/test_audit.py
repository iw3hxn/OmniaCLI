# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The access log of NIS 2 (docs/nis2-audit.md): its chain and its check,
its period against its size, the reads it lists, who reads it, the SIEM."""
import csv
import io
import json
import socket
import threading
import time

from fastapi.routing import APIRoute

from omniacli import accesslog, audit, siem
from omniacli.config import ConfigError, parse_options

from .test_app import AUTH, POST, client, wait_job  # noqa: F401 - the client fixture
from .test_workspaces import basic, workspaces  # noqa: F401 - the workspaces fixture


def lines_of(path):
    return path.read_text(encoding="utf-8").splitlines()


def test_the_chain_follows_rotation_and_finds_a_change(tmp_path, monkeypatch):
    monkeypatch.setattr(accesslog, "MAX_BYTES", 600)
    log = accesslog.AccessLog(tmp_path / "access.jsonl")
    for n in range(60):
        log.record("action", "user", f"u{n}", "10.0.0.1", f"POST /x{n} -> 200")
    assert (tmp_path / "access.jsonl.3").exists()
    found = log.verify()
    assert found == {"ok": True, "entries": 60, "breaks": []}
    # across a rotation: the first line of a file carries the hash of the last of the older one
    first = json.loads(lines_of(tmp_path / "access.jsonl.1")[0])
    assert first["prev"] == accesslog.line_hash(lines_of(tmp_path / "access.jsonl.2")[-1])
    # a line changed (a name), in an older file: the next one says so
    older = tmp_path / "access.jsonl.2"
    rows = lines_of(older)
    rows[1] = rows[1].replace('"name": "u', '"name": "x')
    older.write_text("\n".join(rows) + "\n")
    found = log.verify()
    assert not found["ok"] and found["breaks"][0]["file"] == str(older) and found["breaks"][0]["line"] == 3
    assert "line 2" in found["breaks"][0]["reason"]
    rows[1] = rows[1].replace('"name": "x', '"name": "u')
    older.write_text("\n".join(rows) + "\n")
    assert log.verify()["ok"]
    # a line removed
    older.write_text("\n".join(rows[:1] + rows[2:]) + "\n")
    assert [b["line"] for b in log.verify()["breaks"]] == [2]


def test_the_entries_of_before_start_the_chain(tmp_path):
    path = tmp_path / "access.jsonl"
    path.write_text(json.dumps({"time": "2026-01-01T00:00:00+01:00", "ns": 1, "event": "login", "kind": "user",
                                "name": "old", "ip": "", "detail": ""}) + "\n")
    log = accesslog.AccessLog(path)
    log.record("logout", "user", "old")
    assert log.verify()["ok"]
    assert json.loads(lines_of(path)[1])["prev"] == accesslog.line_hash(lines_of(path)[0])
    # one without the chain among chained ones: added by hand
    with open(path, "a") as fh:
        fh.write(json.dumps({"ns": 3, "event": "login", "name": "intruder"}) + "\n")
    log.record("logout", "user", "old")
    reasons = [b["reason"] for b in log.verify()["breaks"]]
    assert reasons and "without the chain" in reasons[0]


def test_the_cli_checks_the_chain(tmp_path, capsys):
    from omniacli.cli import main

    log = accesslog.AccessLog(tmp_path / "access.jsonl")
    log.record("login", "user", "a")
    log.record("logout", "user", "a")
    assert main(["verify-access-log", "--logs-dir", str(tmp_path)]) == 0
    assert "the chain is intact" in capsys.readouterr().out
    path = tmp_path / "access.jsonl"
    path.write_text(path.read_text().replace('"name": "a"', '"name": "b"', 1))
    assert main(["verify-access-log", "--logs-dir", str(tmp_path)]) == 1
    assert "access.jsonl:2" in capsys.readouterr().out


def test_no_entry_of_the_period_goes_for_size(tmp_path, monkeypatch):
    monkeypatch.setattr(accesslog, "MAX_BYTES", 300)
    log = accesslog.AccessLog(tmp_path / "access.jsonl", retention_days=365)
    now = time.time_ns()
    day = 86400 * 10**9
    clock = {"ns": now - 400 * day}
    monkeypatch.setattr(accesslog.time, "time_ns", lambda: clock["ns"])
    for n in range(30):  # older than the period
        clock["ns"] += 10**9
        log.record("action", "user", f"old{n}")
    clock["ns"] = now
    for n in range(80):  # within it: many more files than KEEP_FILES
        clock["ns"] += 10**9
        log.record("action", "user", f"new{n}")
    names = [e["name"] for e in log.recent(100000)]
    assert len(accesslog.numbered(log.path)) > accesslog.KEEP_FILES
    assert all(f"new{n}" in names for n in range(80))
    # the old ones went as the log grew, as far as KEEP_FILES lets them
    assert not [n for n in names if n.startswith("old")]
    assert log.verify()["ok"]


def test_options_of_the_access_log():
    options = parse_options({})
    assert (options.audit_retention_days, options.syslog_host) == (365, "")
    options = parse_options({"audit": {"retention_days": 730, "syslog": {"host": "siem.example.com",
                                                                         "protocol": "tls"}}})
    assert (options.audit_retention_days, options.syslog_port, options.syslog_protocol) == (730, 6514, "tls")
    for bad in ({"audit": {"retention_days": -1}}, {"audit": {"syslog": {"host": "a b"}}},
                {"audit": {"syslog": {"host": "x", "protocol": "smtp"}}},
                {"audit": {"syslog": {"host": "x", "ca_file": "relative.pem"}}}):
        try:
            parse_options(bad)
        except ConfigError:
            continue
        raise AssertionError(f"{bad} accepted")


def _get_routes(app) -> set[str]:
    """Every GET route of app, a plugin's with its /x/<plugin> prefix,
    whichever way FastAPI keeps an included router."""
    found = set()
    for route in app.routes:
        if isinstance(route, APIRoute) or (hasattr(route, "methods") and route.methods):
            if "GET" in (route.methods or ()):
                found.add(route.path)
        elif hasattr(route, "original_router"):
            prefix = route.include_context.prefix
            for inner in route.original_router.routes:
                if "GET" in (getattr(inner, "methods", None) or ()):
                    found.add(prefix + inner.path)
    return found


async def test_every_read_is_classified(client):
    """A GET route added later fails here until it is put in
    audit.SENSITIVE_READS (customer or security data: logged) or in
    audit.NOT_SENSITIVE (or AUDITED_BY_ROUTE: it logs itself)."""
    routes = _get_routes(client.app)
    listed = set(audit.SENSITIVE_READS) | audit.NOT_SENSITIVE | audit.AUDITED_BY_ROUTE
    assert not routes - listed, f"classify these reads in omniacli/audit.py: {sorted(routes - listed)}"
    assert not listed - routes, f"not routes any more, out of omniacli/audit.py: {sorted(listed - routes)}"
    assert not set(audit.SENSITIVE_READS) & audit.NOT_SENSITIVE


async def test_a_sensitive_read_is_logged_once(client, env):
    log = client.app.state.access_log
    for _ in range(3):  # followed: its lines asked again and again
        assert (await client.get("/api/servers/srv/test/log", headers={"Authorization": AUTH})).status_code == 200
    reads = [e for e in log.recent() if e["event"] == "read"]
    assert len(reads) == 1
    entry = reads[0]
    assert (entry["name"], entry["role"], entry["what"], entry["route"], entry["server"], entry["env"],
            entry["status"]) == ("admin", "admin", "instance log", "/api/servers/{server}/{role}/log", "srv", "test",
                                 200)
    assert entry["utc"].endswith("Z") and entry["time"][:4].isdigit()
    # a page that is not customer data: not listed
    assert (await client.get("/jobs", headers={"Authorization": AUTH})).status_code == 200
    assert len([e for e in log.recent() if e["event"] == "read"]) == 1


async def test_an_action_names_its_job_and_its_end(client, env):
    log = client.app.state.access_log
    resp = await client.post("/servers/srv/test/restart", headers={**POST, "Accept": "application/json"})
    assert resp.status_code in (200, 303)
    action = next(e for e in log.recent() if e["event"] == "action")
    assert action["route"] == "/servers/{server}/{role}/restart" and action["server"] == "srv"
    assert action["role"] == "admin" and len(action["job"]) == 1
    await wait_job(client, action["job"][0])
    for _ in range(50):
        ended = [e for e in log.recent() if e["event"] == "job" and e.get("job") == action["job"]]
        if ended:
            break
        time.sleep(0.01)
    assert ended and ended[0]["status"] in ("success", "failed") and "admin" in ended[0]["name"]


async def test_a_workspace_admin_reads_its_entries_only(client, env, workspaces):
    log = client.app.state.access_log
    # the machine's admin acts on srv (acme's) and on the machine
    assert (await client.get("/api/servers/srv/test/log", headers={"Authorization": AUTH})).status_code == 200
    resp = await client.post("/api/access/users/save", headers=POST,
                             json={"new": True, "name": "machine-only", "role": "developer", "password": "x" * 12})
    assert resp.status_code == 200
    # mario, admin of acme, logs in; eva of beta does something of beta
    assert (await client.get("/workspaces/acme", headers=basic("mario"))).status_code == 200
    log.record("login", "user", "eva", "10.0.0.9", "login form")
    log.record("login", "user", "mario", "10.0.0.8", "login form")
    page = await client.get("/workspaces/acme/audit", headers=basic("mario"))
    assert page.status_code == 200
    assert "instance log" in page.text and "machine-only" not in page.text and "10.0.0.9" not in page.text
    assert "10.0.0.8" in page.text
    # the export of a period: CSV and JSON, the same entries
    resp = await client.get("/api/workspaces/acme/audit/export?format=csv", headers=basic("mario"))
    assert resp.status_code == 200 and "attachment" in resp.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(resp.text)))
    assert rows and all(r["workspace"] == "acme" or r["name"] == "mario" for r in rows)
    assert not [r for r in rows if "machine-only" in r["detail"] or r["name"] == "eva"]
    today = time.strftime("%Y-%m-%d")
    resp = await client.get(f"/api/workspaces/acme/audit/export?format=json&since={today}&until={today}",
                            headers=basic("mario"))
    data = resp.json()
    assert data and all(e.get("workspace") == "acme" or "acme" in e.get("workspaces", []) for e in data)
    assert resp.headers["content-disposition"].endswith('.json"')
    # never another workspace's, never the machine's
    assert (await client.get("/workspaces/beta/audit", headers=basic("mario"))).status_code == 403
    assert (await client.get("/api/workspaces/beta/audit/export", headers=basic("mario"))).status_code == 403
    assert (await client.get("/access", headers=basic("mario"))).status_code == 403
    assert (await client.get("/api/access/export", headers=basic("mario"))).status_code == 403
    # luca, a user of acme: not its admin
    assert (await client.get("/workspaces/acme/audit", headers=basic("luca"))).status_code == 403
    # reading it is itself in it
    assert any(e["event"] == "read" and e.get("what") == "access log of the workspace (export)" for e in log.recent())


async def test_the_machine_admin_reads_and_checks_all_of_it(client, env):
    log = client.app.state.access_log
    log.record("login", "user", "someone", "10.0.0.7", "login form")
    page = await client.get("/access?who=someone", headers={"Authorization": AUTH})
    assert page.status_code == 200 and "10.0.0.7" in page.text and "Retention and integrity" in page.text
    assert (await client.get("/access?who=nobody-here", headers={"Authorization": AUTH})).text.count("10.0.0.7") == 0
    found = (await client.get("/api/access/verify", headers={"Authorization": AUTH})).json()
    assert found["ok"] and found["entries"] >= 2
    resp = await client.get("/api/access/export?format=json&event=login", headers={"Authorization": AUTH})
    assert {e["event"] for e in resp.json()} == {"login"}


def test_the_room_and_the_clock(tmp_path, monkeypatch):
    day = 86400 * 10**9
    now = time.time_ns()
    span = {"bytes": 10 * 1024**2, "files": 3, "first": now - 10 * day, "last": now}
    found = audit.room(span, 365, tmp_path)
    # 1 MiB a day for 365 days: about 355 MiB still to come
    assert abs(found["needed"] - 355 * 1024**2) < 2 * 1024**2 and found["per_day"] > 1000000
    monkeypatch.setattr(audit.shutil, "disk_usage", lambda folder: type("U", (), {"free": 300 * 1024**2})())
    assert audit.room(span, 365, tmp_path)["low"]
    monkeypatch.setattr(audit.shutil, "disk_usage", lambda folder: type("U", (), {"free": 100 * 1024**3})())
    assert not audit.room(span, 365, tmp_path)["low"]
    answers = {"timedatectl": "NTPSynchronized=no\nNTP=yes\n", "chronyc": None}
    monkeypatch.setattr(audit, "_ask", lambda command: answers[command[0]])
    audit._clock.clear()
    assert audit.clock_status()["synced"] is False
    answers["chronyc"] = "Reference ID    : 1\nLeap status     : Normal\n"
    audit._clock.clear()
    assert audit.clock_status() == {"synced": True, "source": "chrony", "detail": "Leap status: Normal"}
    answers.update(timedatectl=None, chronyc=None)
    audit._clock.clear()
    assert audit.clock_status()["synced"] is None


def test_syslog_format_and_delivery():
    entry = {"utc": "2026-11-21T10:00:00Z", "event": "login", "name": "mario"}
    message = siem.format_message(entry, "machine one")
    assert message.startswith(b"<86>1 2026-11-21T10:00:00Z machine_one omniacli ")
    assert b" login - \xef\xbb\xbf{" in message and json.loads(message.split(b"\xef\xbb\xbf", 1)[1]) == entry
    assert siem.format_message({"event": "failed"}).startswith(b"<85>1 ")
    # TCP, octet counting: what a SIEM receives
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    got = []

    def accept():
        conn, _ = server.accept()
        conn.settimeout(5)
        data = b""
        while b"mario" not in data:
            data += conn.recv(4096)
        got.append(data)
        conn.close()
    reader = threading.Thread(target=accept, daemon=True)
    reader.start()
    forwarder = siem.Forwarder(lambda text: None)
    forwarder.configure("127.0.0.1", server.getsockname()[1], "tcp")
    forwarder.send(entry)
    reader.join(5)
    server.close()
    length, _, rest = got[0].partition(b" ")
    assert int(length) == len(rest) and rest.startswith(b"<86>1 ")
    forwarder.configure("", 0, "udp")


async def test_a_siem_that_fails_never_stops_an_action(client, env):
    log = client.app.state.access_log
    # a port nobody listens on
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    resp = await client.post("/api/options/save", headers=POST,
                             json={"audit": {"retention_days": 365,
                                             "syslog": {"host": "127.0.0.1", "port": port, "protocol": "tcp"}}})
    assert resp.status_code == 200
    started = time.monotonic()
    resp = await client.post("/api/access/users/save", headers=POST,
                             json={"new": True, "name": "while-down", "role": "user", "password": "x" * 12})
    assert resp.status_code == 200 and time.monotonic() - started < 2
    for _ in range(200):
        failed = [e for e in log.recent() if e["event"] == "siem"]
        if failed:
            break
        time.sleep(0.02)
    assert failed and "failed" in failed[0]["detail"] and f":{port}" in failed[0]["detail"]
    assert any(e["event"] == "action" and "while-down" in e["detail"] for e in log.recent())
    page = await client.get("/access", headers={"Authorization": AUTH})
    assert "Sending to the SIEM fails" in page.text
    # off again: nothing waits
    resp = await client.post("/api/options/save", headers=POST, json={"audit": {"retention_days": 365}})
    assert resp.status_code == 200
