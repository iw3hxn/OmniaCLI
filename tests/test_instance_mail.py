# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The mail of an instance's monitor (UX-018 §3, §5, §6.2; AD-019 19.3, slice 3): received and not sent read by one
fixed query, read only and bounded, through the helper or psql as Odoo connects; the last 2 days read again; Odoo 16's
outgoing messages not received; only numbers kept; a staging's mail held; the cause «Posta» with its reason by class,
its alert grouped by cause; the section for who acts, nothing of the customer's mail for anyone."""

import json
import re
import time
from pathlib import Path

import pytest

from omniacli import composehelper, instancelog, instancemail, instancemonitor as im, monitor as monitor_module
from omniacli import notify, ops, stagingmail
from omniacli.composehelper import MAIL_CLASSES, mail_query
from omniacli.instancemonitor import InstanceHistory

from .test_app import AUTH, client  # noqa: F401 - the client fixture
from .test_instance_log import _monitor
from .test_instance_monitor import _feed
from .test_mcp import call
from .test_workspaces import post_as, workspaces  # noqa: F401 - the workspaces fixture

NOW = 1_800_000_000  # a multiple of 300


# --- the query ------------------------------------------------------------------------------------------------------

def _selects(sql: str) -> list[str]:
    """Each SELECT of sql with what it reads, up to the parenthesis that closes it."""
    found = []
    for start in [m.start() for m in re.finditer(r"\bSELECT\b", sql)]:
        depth, end = 0, len(sql)
        for i in range(start, len(sql)):
            if sql[i] == "(":
                depth += 1
            elif sql[i] == ")":
                if depth == 0:
                    end = i
                    break
                depth -= 1
        found.append(_outer(sql[start:end]))
    return found


def _outer(select: str) -> str:
    """select without the SELECTs inside it: each (SELECT …) taken away whole."""
    while True:
        start = select.find("(SELECT", 1)
        if start < 0:
            return select
        depth = 0
        for i in range(start, len(select)):
            depth += {"(": 1, ")": -1}.get(select[i], 0)
            if depth == 0:
                select = select[:start] + "(…)" + select[i + 1:]
                break


def _bounded(sql: str) -> int:
    """How many reads of mail_message and mail_mail sql has; AssertionError on one not bounded."""
    reads = 0
    for select in _selects(sql):
        own = select  # this SELECT, not the ones inside it
        for table in ("mail_message", "mail_mail"):
            if re.search(rf"\bFROM {table}\b", own):
                reads += 1
                # mail_message, the big one: by its primary key only (an id range, a LIMIT, its max(id)); mail_mail
                # (a queue, small) in an aggregate too, or by the row of a bounded read (its index on mail_message_id)
                rule = r"\bm\.id > |\bLIMIT \d|max\(id\)" if table == "mail_message" else \
                    r"\bLIMIT \d|count\(|\.mail_message_id = m\.id\b"
                assert re.search(rule, own), own[:200]
    return reads


@pytest.mark.parametrize("from_id", [0, 81234])
def test_no_query_on_the_mail_without_an_id_range_an_aggregate_or_a_limit(from_id):
    """AD-019 §8.3, counted: every read of mail_message or mail_mail is bounded by an id range, is an aggregate or has
    a LIMIT; the whole is one read-only transaction stopped at 10 s; no column of an address, a subject, a body or an
    error's text is selected (the reason is only matched, inside the class)."""
    sql = mail_query(from_id)
    assert sql.startswith("BEGIN TRANSACTION READ ONLY; SET LOCAL statement_timeout = '10s'; ")
    assert sql.endswith("COMMIT;") and sql.count(";") == 4
    assert _bounded(sql) == (6 if not from_id else 5)
    # the count itself catches one that is not: the received without their id range, a count of every message
    with pytest.raises(AssertionError):
        _bounded(sql.replace("m.id > (SELECT v FROM lo) AND ", ""))
    if not from_id:
        with pytest.raises(AssertionError):
            _bounded(sql.replace("ORDER BY id DESC LIMIT 1", "ORDER BY id DESC"))
    for word in ("email_from", "email_to", "subject", "body", "reply_to", "record_name", "author_id"):
        assert word not in sql
    assert re.findall(r"failure_reason ~\*", sql) and "failure_reason," not in sql and "failure_reason)" not in sql
    keys = set(re.findall(r"'(\w+)', \(", sql))
    assert keys == {"version", "top", "lo", "received", "failed_now", "failed"}
    assert (f"SELECT {from_id}::bigint AS v" in sql) == bool(from_id) and ("LIMIT 1) o)" in sql) == (not from_id)


def test_the_helper_runs_the_fixed_query_with_a_number(tmp_path, monkeypatch):
    monkeypatch.setattr(composehelper, "services", lambda project: ["odoo", "db"])
    monkeypatch.setattr(composehelper, "psql_options", lambda project, name: ("--user", "postgres"))
    [cmd] = composehelper.commands(tmp_path, "pg-mail", ["db", "main", "81234"])
    assert cmd[:5] == ["exec", "-T", "--user", "postgres", "db"]
    assert cmd[-2:] == ["main", mail_query(81234)] and "-q" in composehelper.SCRIPTS["pg-mail"]
    assert "pg-mail" in composehelper.WITH_ARGUMENTS and "machine-pg-mail" in composehelper.SCRIPTS
    for bad in (["db", "main", "1; DROP TABLE x"], ["db", "main", "-1"], ["db", "main"], ["db", "main", "1" * 19]):
        with pytest.raises(composehelper.HelperError):
            composehelper.commands(tmp_path, "pg-mail", bad)
    # a database on another machine (AD-003): from its Odoo service, as Odoo connects, the same fixed query
    (tmp_path / ".env").write_text("DB_REMOTE=1\nDB_HOST=pg.example.net\nDB_PORT=5432\n")
    [cmd] = composehelper.commands(tmp_path, "pg-mail", ["remote", "main", "5"])
    assert cmd[:3] == ["exec", "-T", "odoo"] and cmd[-2:] == ["main", mail_query(5)]
    assert cmd[cmd.index("-c") + 1] == composehelper.SCRIPTS["machine-pg-mail"]


async def test_through_the_helper_and_through_psql_as_odoo(tmp_path, monkeypatch, env):
    """A compose project's through the helper (its database service, the cluster or another machine: the helper's
    db_target), a venv's with psql as Odoo connects; a database without the module mail is said so."""
    from omniacli.config import parse_servers
    data = {"type": "docker", "test": {"restart": {"type": "compose", "service": str(tmp_path)}, "repositories": [],
                                       "database": "main"}}
    docker = parse_servers({"s": data})["s"].test
    asked = []
    answer = {"version": "19.0.1.3", "top": 9, "lo": 0, "received": [], "failed_now": 0, "failed": []}

    async def capture(cmd, env=None, timeout=None, **kw):
        asked.append((cmd, env))
        return 0, json.dumps(answer)
    monkeypatch.setattr(ops, "capture", capture)
    assert (await instancemail.ask(docker, 0))["top"] == 9
    assert asked[-1][0][-4:] == ["pg-mail", "db", "main", "0"] and asked[-1][1] is None
    venv = env["settings"].servers["srv"].test
    await instancemail.ask(venv, 77)
    cmd, environ = asked[-1]
    assert cmd[-1] == mail_query(77) and "-At" in cmd and "ON_ERROR_STOP=1" in cmd and environ["PGDATABASE"] == "DB_test"

    async def no_mail(cmd, env=None, timeout=None, **kw):
        return 1, 'ERROR:  relation "mail_message" does not exist\nLINE 1: ...'
    monkeypatch.setattr(ops, "capture", no_mail)
    with pytest.raises(instancemail.NoMail):
        await instancemail.ask(docker, 0)


# --- the reading ----------------------------------------------------------------------------------------------------

class _Inst:
    key, server, role = "acme/production", "acme", "production"


def _reader(tmp_path, monkeypatch, answers, staging=False, held=None):
    history = InstanceHistory(tmp_path / "monitor")
    asked = []

    async def ask(instance, from_id):
        asked.append(from_id)
        found = answers.pop(0)
        if isinstance(found, Exception):
            raise found
        return found
    monkeypatch.setattr(instancemail, "ask", ask)
    return instancemail.InstanceMail(history, staging=lambda key: staging, held=held), history, asked


async def test_the_last_2_days_are_read_again_and_only_what_changed_is_written(tmp_path, monkeypatch):
    """The ids of mail_message are not in the order of the commits: every read takes the last 2 days again, from the
    top id of a read of 2 days before (the first reads let the database find the bound); a span is written again when
    its count changed (the last line wins), to 0 when its messages went."""
    t1, t2 = NOW - 3600, NOW - 600
    answers = [
        {"version": "19.0.1.3", "top": 500, "received": [[t1, 3, 3], [t2, 2, 1]], "failed_now": 1,
         "failed": [[t2, "smtp_auth", 1]]},
        # a message of t1 committed late, one of t2 deleted, the failure of t2 sent at last
        {"version": "19.0.1.3", "top": 520, "received": [[t1, 4, 4], [t2, 1, 1]], "failed_now": 0, "failed": []},
        {"version": "19.0.1.3", "top": 530, "received": [[t1, 4, 4]], "failed_now": 0, "failed": []},
    ]
    reader, history, asked = _reader(tmp_path, monkeypatch, answers)
    await reader.read(_Inst(), NOW)
    points = {p["t"]: p for p in history.points(_Inst.key, 0, "mail")}
    assert points[t1]["mr"] == 3 and points[t2]["mr"] == 2 and points[t2]["mx"] == 1 and points[t2]["mc"] == {"smtp_auth": 1}
    lines = (history.folder(_Inst.key) / time.strftime("%Y-%m-%d.jsonl", time.gmtime(t1))).read_text().splitlines()
    assert len(lines) == 2
    await reader.read(_Inst(), NOW + 300)
    await reader.read(_Inst(), NOW + 600)
    points = {p["t"]: p for p in history.points(_Inst.key, 0, "mail")}
    assert points[t1]["mr"] == 4 and points[t2]["mr"] == 0 and points[t2]["mx"] == 0
    lines = (history.folder(_Inst.key) / time.strftime("%Y-%m-%d.jsonl", time.gmtime(t1))).read_text().splitlines()
    assert len(lines) == 5  # t1 twice, t2 three times: nothing written when nothing changed
    assert asked == [0, 0, 0]
    # 2 days later the bound is the top of the read of 2 days before
    answers.append({"version": "19.0.1.3", "top": 900, "received": [], "failed_now": 0, "failed": []})
    await reader.read(_Inst(), NOW + 300 + instancemail.REREAD)
    assert asked[-1] == 520
    state = reader.state(_Inst.key)
    assert state["state"] is None and state["failed_now"] == 0
    assert oct((history.folder(_Inst.key) / instancemail.FILE).stat().st_mode & 0o777) == "0o600"


async def test_in_odoo_16_an_outgoing_message_is_not_received(tmp_path, monkeypatch):
    """Odoo 16 writes its outgoing messages as type email too: those with a row of mail_mail are not counted (AD-019
    19.3); from 17 they are email_outgoing, and an incoming one notified to its followers has its rows of mail_mail."""
    for version, received in (("16.0.1.0", 2), ("saas~17.2.1.0", 5), ("19.0.1.3", 5)):
        reader, history, _ = _reader(tmp_path / version, monkeypatch,
                                     [{"version": version, "top": 1, "received": [[NOW, 5, 2]], "failed_now": 0,
                                       "failed": []}])
        await reader.read(_Inst(), NOW + 60)
        assert history.points(_Inst.key, 0, "mail")[0]["mr"] == received, version


async def test_a_database_not_read_is_a_sentence_never_a_zero(tmp_path, monkeypatch):
    reader, history, _ = _reader(tmp_path, monkeypatch, [instancemail.NoMail(), OSError("no answer")])
    assert reader.state(_Inst.key)["state"] == instancemail.NOT_YET
    await reader.read(_Inst(), NOW)
    assert reader.state(_Inst.key)["state"] == instancemail.NO_MAIL
    await reader.read(_Inst(), NOW + 300)
    state = reader.state(_Inst.key)
    assert state["state"] == instancemail.UNREADABLE
    view = instancemail.mail_view([], [], state, "24h", 86400, NOW + 300, None)
    assert view["total"]["received"] is None and view["total"]["failed"] is None
    assert all(p["received"] is None for p in view["points"])
    assert instancemail.mail_causes([], state, NOW + 300) == []


async def test_a_stagings_mail_is_held_and_never_a_cause(tmp_path, monkeypatch):
    """UX-018 §6.2 and criterion 13: a staging's mail is held: its caught mail counted over the 24 hours, the number
    only; what it could not send is no cause (its mail is held on purpose)."""
    asked = []

    async def held(instance, since):
        asked.append(since)
        return 14, False
    reader, history, _ = _reader(tmp_path, monkeypatch, [
        {"version": "19.0.1.3", "top": 1, "received": [], "failed_now": 3, "failed": [[NOW - 300, "smtp_connect", 3]]}],
        staging=True, held=held)
    await reader.read(_Inst(), NOW)
    state = reader.state(_Inst.key)
    assert state["staging"] and state["held"] == 14 and asked == [NOW - 86400]
    assert instancemail.mail_causes(history.points(_Inst.key, 0, "mail"), state, NOW) == []
    view = instancemail.mail_view([], history.points(_Inst.key, 0, "mail"), state, "24h", 86400, NOW, None)
    assert view["staging"] and view["held"] == 14


async def test_mailpit_counts_the_mail_of_the_24_hours(monkeypatch):
    rows = [{"ID": "a" * 22, "Created": "2027-01-15T07:59:00.000Z", "Subject": "secret", "To": [{"Address": "x@y.z"}]},
            {"ID": "b" * 22, "Created": "2027-01-14T07:59:00.000Z"}, {"ID": "c" * 22, "Created": "nonsense"}]

    async def ask(instance, *args):
        assert args == ("mail-list", "0", str(stagingmail.MAIL_LIMIT))
        return json.dumps({"total": 3, "messages": rows})
    monkeypatch.setattr(stagingmail, "_ask", ask)
    assert await stagingmail.held(object(), NOW - 86400) == (1, False)


# --- the cause and its alert ----------------------------------------------------------------------------------------

def _failed(t, **classes):
    return {"t": t, "y": "mail", "mr": 0, "mx": sum(classes.values()), "mc": classes}


def test_the_cause_mail_with_its_reason_by_class():
    """UX-018 §3: at least 1 e-mail not sent in the last 24 hours; its main reason by class (the mail server's first
    when as many); what failed before the 24 hours is no cause."""
    state = {"state": None, "staging": False}
    assert instancemail.mail_causes([_failed(NOW - 90000, smtp_auth=4)], state, NOW) == []
    [cause] = instancemail.mail_causes([_failed(NOW - 3600, smtp_auth=10, recipient=2), _failed(NOW - 300, smtp_auth=2)],
                                       state, NOW)
    assert cause == {"kind": "mail", "count": 14, "reason": "smtp_auth", "smtp": True,
                     "reasons": {"smtp_auth": 12, "recipient": 2}, "hours": 24, "section": "mail"}
    [cause] = instancemail.mail_causes([_failed(NOW - 300, recipient=1, smtp_connect=1)], state, NOW)
    assert cause["reason"] == "smtp_connect" and set(MAIL_CLASSES) >= set(cause["reasons"])
    [cause] = instancemail.mail_causes([_failed(NOW - 300, recipient=3)], state, NOW)
    assert cause["smtp"] is False


async def test_one_alert_for_two_instances_with_the_smtp_down(tmp_path, monkeypatch):
    """Carlo's §10.5: one SMTP down over many productions is ONE e-mail listing them, with the reason in words and
    never a text of the customer's; not again every round; saved at once."""
    sent = []

    async def send(options, recipients, subject, text, event=None, language=""):
        sent.append({"to": sorted(recipients), "subject": subject, "text": text, "event": event})
        return []
    monkeypatch.setattr(notify, "send", send)
    mon, settings = _monitor(tmp_path)
    mon.settings, mon.causes = settings, {}
    down = {"kind": "mail", "count": 12, "reason": "smtp_auth", "smtp": True, "reasons": {"smtp_auth": 12}}
    other = {**down, "count": 3}
    await mon.instance_causes({"a1/production": [down], "a2/production": [other]})
    assert len(sent) == 1
    assert "2 instances have mail not sent" in sent[0]["subject"]
    assert "a1/production: 12 not sent, the mail server refuses the login" in sent[0]["text"]
    assert "a2/production: 3 not sent" in sent[0]["text"] and sent[0]["event"]["cause"] == "mail"
    assert json.loads((tmp_path / "monitor.json").read_text())["causes"]["mail"]
    for _ in range(3):
        await mon.instance_causes({"a1/production": [down], "a2/production": [other]})
    assert len(sent) == 1
    said = notify.said("it", "\n".join([monitor_module.CAUSE_HEADS["mail"], sent[0]["text"].splitlines()[1]]))
    assert "E-mail non partite nelle ultime 24 ore" in said and "12 non partite, il server di posta rifiuta l'accesso" in said
    assert notify.said("it", "OmniaCLI m: [warning] 2 instances have mail not sent") == \
        "OmniaCLI m: [avviso] 2 istanze hanno e-mail non partite"


async def test_the_round_reads_the_mail_right_after_the_log(tmp_path, monkeypatch):
    """One queue (AD-019 §8.1): the mail is read in the log's round, so its cause is in the same alerts."""
    history = InstanceHistory(tmp_path / "monitor")
    inst = type("I", (), {"key": "acme/production", "plugin": object()})()
    settings = type("S", (), {"servers": {"acme": type("V", (), {"active": True, "instances": {"production": inst}})()}})()
    order, told = [], []

    async def told_(found, measured):
        told.append(found)
    reader = instancelog.InstanceLog(settings, history, told=told_)

    async def read(instance, now=None):
        order.append("log")
        return 0
    monkeypatch.setattr(reader, "read", read)

    class Mail:
        async def read(self, instance, now=None):
            order.append("mail")
    reader.mail = Mail()
    reader.causes = lambda key, now=None: (order.append("causes"), [{"kind": "mail", "count": 1}])[1]
    await reader.round(NOW)
    assert order == ["log", "mail", "causes"] and told == [{"acme/production": [{"kind": "mail", "count": 1}]}]


# --- the route, REST, MCP and the page --------------------------------------------------------------------------------

SECRETS = ("mario.rossi@example.com", "Fattura 2026/0042", "smtp.provider.example", "535 5.7.8 bad credentials")


def _feed_mail(app, key="srv/production", staging=False):
    """A day of mail: 20 sent and 2 bounced in the log, 5 received and 3 not sent (the mail server refusing the
    login) in the database."""
    now = int(time.time())
    last = now // 300 * 300
    spans = [{"t": last - 300 * i, "y": "log", "ms": 2, "mb": 1 if i < 2 else 0} for i in range(10)]
    mail = [{"t": last - 300 * i, "y": "mail", "mr": 1, "mx": 1 if i < 3 else 0, "mc": {"smtp_auth": 1} if i < 3 else {}}
            for i in range(5)]
    app.state.instance_history.add(key, spans + mail, now)
    folder = app.state.instance_history.folder(key)
    (folder / instancelog.CURSOR).write_text(json.dumps({"inode": 1, "pos": 1, "at": now - 30, "state": None}))
    app.state.instance_log.reads.pop(key, None)
    (folder / instancemail.FILE).write_text(json.dumps({"at": now - 20, "state": None, "failed_now": 3, "first": now - 86400,
                                                        **({"held": 14, "held_more": False} if staging else {})}))


async def test_the_route_rest_and_mcp_say_the_mail(client, monkeypatch):  # noqa: F811
    """The slice's numbers by hand: 20 sent, 2 bounced, 5 received, 3 not sent; the cause «Posta» with its reason;
    the same in REST and MCP."""
    from omniacli import pgreport

    async def report(instance):
        return {"long_queries": [], "connections": 3, "max_connections": 100, "cache_hit": 99.6}
    monkeypatch.setattr(pgreport, "report", report)
    _feed(client.app)
    _feed_mail(client.app)
    page = (await client.get("/api/servers/srv/production/monitor", headers={"Authorization": AUTH})).json()
    mail = page["mail"]
    assert mail["total"] == {"sent": 20, "bounced": 2, "received": 5, "failed": 3}
    assert mail["reasons"] == {"smtp_auth": 3} and mail["failed_now"] == 3 and mail["source"] is None
    assert sum(p["sent"] for p in mail["points"]) == 20 and mail["width"] == 3600
    [cause] = [c for c in page["verdict"]["causes"] if c["kind"] == "mail"]
    assert cause["count"] == 3 and cause["reason"] == "smtp_auth" and cause["section"] == "mail"
    week = (await client.get("/api/servers/srv/production/monitor?period=7d", headers={"Authorization": AUTH})).json()
    assert week["mail"]["width"] == 86400 and week["mail"]["total"]["sent"] == 20
    rest = (await client.get("/api/v1/instances/srv/production/monitor",
                             headers={"Authorization": "Bearer mcp-test-token"})).json()
    tool = await call(client, "instance_monitor", server_name="srv", role="production")
    for found in (rest, tool):
        assert found["mail"]["total"] == mail["total"] and any(c["kind"] == "mail" for c in found["verdict"]["causes"])


async def test_a_workspaces_people_read_no_address_host_or_text_of_the_mail(workspaces, client, monkeypatch):  # noqa: F811
    """AD-019 §8.5 for the mail: numbers only, for anyone; a workspace's admin reads the section, its user only the
    cause, without the section and without a remedy (the page's), and nothing kept holds a text of the mail."""
    from omniacli import pgreport

    async def report(instance):
        return {"long_queries": [], "connections": 3, "max_connections": 100, "cache_hit": 99.6}
    monkeypatch.setattr(pgreport, "report", report)
    app = client.app
    _feed(app)
    inst = app.state.settings.servers["srv"].instances["production"]

    async def ask(instance, from_id):
        # what a database would hold, of which the query selects nothing: only its counts come back
        return {"version": "19.0.1.3", "top": 3, "received": [[int(time.time()) // 300 * 300, 2, 2]], "failed_now": 1,
                "failed": [[int(time.time()) // 300 * 300, "smtp_auth", 1]]}
    monkeypatch.setattr(instancemail, "ask", ask)
    await app.state.instance_mail.read(inst)
    for who in (None, "mario", "luca"):
        headers = {"Authorization": AUTH} if who is None else post_as(who)
        resp = await client.get("/api/servers/srv/production/monitor", headers=headers)
        assert resp.status_code == 200, who
        data = resp.json()
        for word in (*SECRETS, "lab3", "lab4", "/var/", "/srv/"):
            assert word not in resp.text, (who, word)
        assert any(c["kind"] == "mail" for c in data["verdict"]["causes"])
        assert ("mail" in data) is (who != "luca")
    for path in Path(app.state.instance_history.root).rglob("*"):
        if path.is_file():
            text = path.read_text()
            for word in SECRETS:
                assert word not in text, (path.name, word)


async def test_the_mail_on_the_page(workspaces, client):  # noqa: F811
    """UX-018 §5: Posta closed, its line, two charts, for the machine's admin and the workspace's admin, never for its
    user; no primary, titles other than the text; the cause opens it; no browser dialog."""
    for who in ("admin", "mario", "luca"):
        page = (await client.get("/servers/srv", headers=post_as(who) if who != "admin" else {"Authorization": AUTH})).text
        panel = page[page.index('<section class="tab-panel panel mon" data-panel="monitor"'):]
        panel = panel[:panel.index("<style>")]
        assert ('data-mon="mail"' in panel) == (who != "luca"), who
        if who == "luca":
            continue
        fold = panel[panel.index('data-mon="mail"'):]
        fold = fold[:fold.index("</details>")]
        assert 'data-mon-chart="mail-out"' in fold and 'data-mon-chart="mail-in"' in fold and "aria-label" in fold
        assert "<details open" not in panel and 'class="primary' not in panel
        for match in re.finditer(r'<(button|summary)\b([^>]*)>(.*?)</\1>', fold, flags=re.S):
            title = re.search(r'title="([^"]*)"', match.group(2))
            text = re.sub(r"<[^>]+>", "", match.group(3)).strip()
            assert title and title.group(1) and title.group(1) != text, match.group(0)[:120]
    template = (Path(im.__file__).parent / "templates" / "server.html").read_text(encoding="utf-8")
    script = template[template.index("// Mail: what left"):template.index("async function load()")]
    for word in ("alert(", "confirm(", "prompt(", "#fff", "rgb("):
        assert word not in script
    assert "drawMail(data);" in template and "mail: M.openMail" in template
    it = json.loads((Path(im.__file__).parent / "i18n" / "it" / "server.json").read_text(encoding="utf-8"))
    assert it["The e-mails of a staging are held: {n} held in the 24 hours."] == \
        "Le e-mail di una staging sono trattenute: {n} trattenute nelle 24 ore."
    assert it["Sent {sent} · not sent {failed} · received {received} · bounced {bounced}"] == \
        "Inviate {sent} · non partite {failed} · ricevute {received} · respinte {bounced}"


async def test_a_staging_says_its_mail_is_held(client, monkeypatch):  # noqa: F811
    """Criterion 13: a staging's section says «trattenute», with its count; no cause «Posta»."""
    app = client.app
    _feed(app)
    _feed_mail(app, staging=True)
    monkeypatch.setattr(app.state.instance_mail, "staging", lambda key: True)
    page = (await client.get("/api/servers/srv/production/monitor", headers={"Authorization": AUTH})).json()
    assert page["mail"]["staging"] and page["mail"]["held"] == 14
    assert not any(c["kind"] == "mail" for c in page["verdict"]["causes"])
