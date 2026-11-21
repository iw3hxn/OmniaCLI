# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A staging's incoming mail (UX-008 §5.4): the templates' inbox service
(mailboxes Odoo reads, published nowhere) and Mailpit handing it the mail
to a .test address; the helper's mail-send and mail-inbox, their input
checked and never in a script's text; the configuration of Odoo
(odoo/mail_inbox.py) idempotent; the routes per role; the Mail tab."""

import datetime
import email
import email.policy
import json
import re

import pytest
import yaml

from omniacli import composehelper, ops, stacktemplates, stagingmail, workspacehelper
from omniacli.composehelper import HelperError

from .test_app import AUTH, client  # noqa: F401 - the client fixture
from .test_stacktemplates import REPO_TEMPLATES, root  # noqa: F401 - the root fixture
from .test_stagingmail import LISTED, ONE, TEMPLATES, make_staging
from .test_workspaces import basic, post_as, workspaces  # noqa: F401 - the workspaces fixture


# --- the templates

@pytest.mark.parametrize("name", TEMPLATES)
def test_the_inbox_service_of_the_templates(name):
    template = stacktemplates.load_template(REPO_TEMPLATES / name)
    assert template.receives_mail()
    services = yaml.safe_load(template.compose)["services"]
    inbox, mail = services[stacktemplates.INBOX_SERVICE], services[stacktemplates.MAIL_SERVICE]
    # a staging's only, an exact image, nothing published, read only, in the workspace's slice
    assert inbox["profiles"] == [stacktemplates.MAIL_PROFILE]
    assert re.fullmatch(r"greenmail/standalone:\d+\.\d+\.\d+", inbox["image"])
    assert "ports" not in inbox and inbox["read_only"] is True and inbox["cgroup_parent"] == "${SLICE}"
    assert inbox["cap_drop"] == ["ALL"] and "cap_add" not in inbox and "volumes" not in inbox
    # its API on its own loopback; SMTP and IMAP in the project's network
    assert "-Dgreenmail.api.hostname=127.0.0.1" in inbox["environment"]["GREENMAIL_OPTS"]
    assert "-Xmx" in inbox["environment"]["JAVA_OPTS"]
    # Mailpit hands it what goes to a .test address, and nothing else
    env = mail["environment"]
    assert env["MP_SMTP_RELAY_HOST"] == stacktemplates.INBOX_SERVICE and env["MP_SMTP_RELAY_PORT"] == "3025"
    assert "MP_SMTP_RELAY_ALL" not in env and env["MP_SMTP_RELAY_AUTH"] == "none"
    # compose reads $$ as one $
    matching = re.compile(env["MP_SMTP_RELAY_MATCHING"].replace("$$", "$").removeprefix("(?i)"), re.I)
    assert matching.search("catchall@acme-odoo-stg-20261121.test")
    assert matching.search("Sales@ACME.TEST")
    for outside in ("cliente@example.com", "x@test.com", "x@a.test.example.com", "x.test@example.org"):
        assert not matching.search(outside), outside
    assert composehelper.INBOX_SERVICE == stacktemplates.INBOX_SERVICE


def test_a_template_without_the_inbox_receives_nothing(root, tmp_path):  # noqa: F811
    folder = tmp_path / "templates" / "odooplm-20"
    text = (folder / "compose.yaml").read_text()
    (folder / "compose.yaml").write_text(text[:text.index("  # a staging's incoming mail")])
    template = stacktemplates.load_template(folder)
    assert template.captures_mail() and not template.receives_mail()
    storage = root["storage"]
    workspacehelper.disk("acme", str(storage), "2")
    workspacehelper.limits("acme", "0", "0")
    made = workspacehelper.create("acme", str(storage), "stg", "odooplm-20",
                                  json.dumps({"_roles": ["production"], workspacehelper.MAIL_KEY: True}))
    assert made["mail_capture"] is True and made["mail_inbox"] is False
    meta = json.loads((storage / "acme" / "stg" / "instance.json").read_text())
    assert workspacehelper.captures_mail(meta, "production") and not workspacehelper.receives_mail(meta, "production")


def test_a_staging_made_by_the_helper_receives(root):  # noqa: F811
    storage = root["storage"]
    workspacehelper.disk("acme", str(storage), "2")
    workspacehelper.limits("acme", "0", "0")
    made = workspacehelper.create("acme", str(storage), "stg", "odooplm-20",
                                  json.dumps({"_roles": ["production"], workspacehelper.MAIL_KEY: True}))
    assert made["mail_inbox"] is True
    meta = json.loads((storage / "acme" / "stg" / "instance.json").read_text())
    assert workspacehelper.receives_mail(meta, "production") and not workspacehelper.receives_mail(meta, "test")
    # an instance that is no staging: neither
    made = workspacehelper.create("acme", str(storage), "shop", "odooplm-20", json.dumps({"_roles": ["production"]}))
    assert made["mail_inbox"] is False


def test_the_alias_domain_of_a_staging():
    assert stagingmail.domain_of("acme-odoo-stg-20261121") == "acme-odoo-stg-20261121.test"
    assert stagingmail.domain_of("Shop_Odoo.STG--2") == "shop-odoo-stg-2.test"
    assert stagingmail.domain_of("x" * 80) == "x" * 63 + ".test"
    assert stagingmail.domain_of("---") == "staging.test"
    assert stagingmail.domain_of("città") == "citt.test"


# --- the helper

MESSAGE = {"from": "cliente@example.com", "name": "Mario Prova", "to": ["catchall@stg.test", "sales@stg.test"],
           "subject": "Richiesta d'offerta", "body": "Buongiorno,\nvorrei un preventivo.\n"}


def test_a_message_built_by_the_helper():
    sender, recipients, raw = composehelper.mail_message(json.dumps(MESSAGE).encode())
    assert sender == "cliente@example.com" and recipients == ["catchall@stg.test", "sales@stg.test"]
    message = email.message_from_bytes(raw, policy=email.policy.default)
    assert message["From"] == "Mario Prova <cliente@example.com>"
    assert message["To"] == "catchall@stg.test, sales@stg.test" and message["Subject"] == "Richiesta d'offerta"
    assert message["Message-ID"].endswith("@example.com>") and message["In-Reply-To"] is None
    text = message.get_body(preferencelist=("plain",)).get_content().replace("\r\n", "\n")
    assert text == "Buongiorno,\nvorrei un preventivo.\n"
    # and as HTML, escaped, a quote in a blockquote
    _, _, raw = composehelper.mail_message(json.dumps({**MESSAGE, "body": "Sì <b>\n> <script>x</script>\n> y"}).encode())
    html = email.message_from_bytes(raw, policy=email.policy.default).get_body(preferencelist=("html",)).get_content()
    assert html.strip() == "<div>Sì &lt;b&gt;<br><blockquote>&lt;script&gt;x&lt;/script&gt;<br>y</blockquote></div>"
    # a reply: tied to the message it answers, as Odoo finds its record
    _, _, raw = composehelper.mail_message(json.dumps({**MESSAGE, "in_reply_to": "abc.1@stg.test"}).encode())
    message = email.message_from_bytes(raw, policy=email.policy.default)
    assert message["In-Reply-To"] == "<abc.1@stg.test>" == message["References"]
    # the same recipient twice is one
    assert composehelper.mail_message(json.dumps({**MESSAGE, "to": ["a@stg.test", "a@stg.test"]}).encode())[1] == \
        ["a@stg.test"]


@pytest.mark.parametrize("change", [
    {"from": "cliente@example.com\nBcc: x@example.org"}, {"from": "-f@example.com"}, {"from": "Mario <m@x.it>"},
    {"from": "x"}, {"to": []}, {"to": "a@stg.test"}, {"to": [f"a{i}@stg.test" for i in range(11)]},
    {"to": ["a@stg.test\r\nBcc: b@x.org"]}, {"to": ["$(id)@stg.test"]},
    {"subject": "a\r\nBcc: x@example.org"}, {"subject": ""}, {"subject": "x" * 251},
    {"name": "Mario\nBcc: x@example.org"}, {"name": "x" * 81}, {"body": "x" * 100001}, {"body": 3},
    {"in_reply_to": "abc def"}, {"in_reply_to": "<a>\r\nBcc: x@y.z"}, {"extra": 1},
])
def test_a_message_refused(change):
    message = {**MESSAGE, **change}
    with pytest.raises(HelperError, match="mail-send"):
        composehelper.mail_message(json.dumps(message).encode())
    with pytest.raises(HelperError, match="mail-send"):
        composehelper.mail_message(b"not json")
    with pytest.raises(HelperError, match="larger"):
        composehelper.mail_message(b" " * (composehelper.MAIL_INPUT + 1))


def test_the_helper_s_mail_send_and_mail_inbox(tmp_path, monkeypatch):
    monkeypatch.setattr(composehelper, "services", lambda project: ["odoo", "db", "mail", "inbox"])
    cmd, raw = composehelper.mail_send(tmp_path, json.dumps(MESSAGE).encode())
    # in the mail service, the addresses arguments of the script, never in its text
    assert cmd[:3] == ["exec", "-T", "mail"] and cmd[-3:] == ["cliente@example.com", "catchall@stg.test", "sales@stg.test"]
    script = cmd[cmd.index("-c") + 1]
    assert script == composehelper.SCRIPTS["mail-send"] and "example.com" not in script and "127.0.0.1:1025" in script
    assert b"Subject: Richiesta d'offerta" in raw
    with pytest.raises(HelperError, match="on the input"):
        composehelper.commands(tmp_path, "mail-send", [])
    [cmd] = composehelper.commands(tmp_path, "mail-inbox", ["odoo", "acme_stg"])
    assert cmd[:3] == ["exec", "-T", "odoo"] and cmd[-1] == "acme_stg" and "odoo shell" in cmd[-3]
    for args in ([], ["odoo"], ["odoo", "a;b"], ["nope", "acme_stg"], ["odoo", "acme_stg", "x"]):
        with pytest.raises(HelperError):
            composehelper.commands(tmp_path, "mail-inbox", args)
    # the mode and the domain checked, put in front of the script as a literal
    text = composehelper.inbox_input(b'{"mode": "fetch", "domain": "acme-stg.test"}').decode()
    assert text.startswith("OMNIACLI_MAIL = {'mode': 'fetch', 'domain': 'acme-stg.test'}\n")
    assert text.endswith(composehelper.INBOX_SCRIPT.read_text())
    for raw in (b'{"mode": "drop", "domain": "a.test"}', b'{"mode": "on", "domain": "acme.com"}',
                b'{"mode": "on", "domain": "a.b.test"}', b'{"mode": "on", "domain": "a\'.test"}',
                b'{"mode": "on"}', b'{"mode": "on", "domain": "a.test", "x": 1}', b"[]", b"no"):
        with pytest.raises(HelperError, match="mail-inbox"):
            composehelper.inbox_input(raw)
    # a staging of before (no inbox): neither
    monkeypatch.setattr(composehelper, "services", lambda project: ["odoo", "db", "mail"])
    with pytest.raises(HelperError, match="no service 'inbox'"):
        composehelper.mail_send(tmp_path, json.dumps(MESSAGE).encode())
    with pytest.raises(HelperError, match="no service 'inbox'"):
        composehelper.commands(tmp_path, "mail-inbox", ["odoo", "acme_stg"])


def test_mail_send_runs_with_the_message_on_its_input(tmp_path, monkeypatch):
    monkeypatch.setattr(composehelper, "services", lambda project: ["odoo", "db", "mail", "inbox"])
    monkeypatch.setattr(composehelper, "caller", lambda: ("omniacli", 1000, {1000}))
    monkeypatch.setattr(composehelper, "problems", lambda project, uid, groups: [])
    monkeypatch.setattr(composehelper, "compose", lambda project, *args: ["docker", "compose", *args])
    ran = []

    class Done:
        returncode = 0

    monkeypatch.setattr(composehelper.subprocess, "run", lambda cmd, **kw: ran.append((cmd, kw)) or Done())

    class Stdin:
        class buffer:  # noqa: N801 - as sys.stdin.buffer
            @staticmethod
            def read(n):
                return json.dumps(MESSAGE).encode()
    monkeypatch.setattr(composehelper.sys, "stdin", Stdin)
    assert composehelper.run([str(tmp_path), "mail-send"]) == 0
    [(cmd, kw)] = ran
    assert cmd[-3:] == ["cliente@example.com", "catchall@stg.test", "sales@stg.test"]
    assert b"Mario Prova <cliente@example.com>" in kw["input"]
    with pytest.raises(HelperError, match="no arguments"):
        composehelper.run([str(tmp_path), "mail-send", "x@y.z"])


# --- odoo/mail_inbox.py on a database as Odoo has it (a small fake of its ORM)

class Records:
    """A recordset of the fake: records of one model, attributes read of the
    first, written on all."""

    def __init__(self, model, items):
        object.__setattr__(self, "_model", model)
        object.__setattr__(self, "_items", list(items))

    def __iter__(self):
        return (Records(self._model, [r]) for r in self._items)

    def __len__(self):
        return len(self._items)

    def __bool__(self):
        return bool(self._items)

    def __eq__(self, other):
        return isinstance(other, Records) and [id(r) for r in self._items] == [id(r) for r in other._items]

    def __hash__(self):
        return hash(tuple(id(r) for r in self._items))

    def __getattr__(self, name):
        if name in ("sudo", "with_context"):
            return lambda *a, **kw: self
        if not self._items:
            return "" if name == "name" else False
        return self._items[0].get(name, False)

    def __getitem__(self, name):
        return getattr(self, name)

    def __setattr__(self, name, value):
        self.write({name: value})

    def write(self, values):
        values = self._model.env.relational(values)
        for r in self._items:
            r.update(values)
            self._model.env.count += 1

    def mapped(self, name):
        return [r[name] for r in self._items]

    def button_confirm_login(self):
        self.write({"state": "done"})

    def fetch_mail(self):
        for r in self._items:
            r["date"] = datetime.datetime(2026, 11, 21, 9, 0, 0)
            self._model.env.fetched.append(r["user"])
            self._model.env.models["mail.message"].create({"message_type": "email"})


class Model:
    """A model of the fake: its records shared by every view of it (sudo,
    with_context); active_test as Odoo's."""

    def __init__(self, env, name, items, active):
        self.env, self.name, self.items, self.active = env, name, items, active

    def sudo(self):
        return self

    def with_context(self, **kw):
        return Model(self.env, self.name, self.items, self.active and kw.get("active_test", True))

    @staticmethod
    def _value(record, field):
        value = record.get(field, False)
        return (value.id if value else False) if isinstance(value, Records) else value

    def _match(self, record, domain):
        if self.active and not record.get("active", True):
            return False
        for field, op, value in domain:
            mine = self._value(record, field)
            if (op == "=" and mine != value) or (op == "!=" and mine == value) or (op == "in" and mine not in value):
                return False
        return True

    def search(self, domain, order=None, limit=None):
        found = [r for r in self.items if self._match(r, domain)]
        key = (order or "").split(" ")[0].split(",")[0]
        if key in ("user", "alias_name"):
            found.sort(key=lambda r: r.get(key) or "")
        return Records(self, found[:limit] if limit else found)

    def search_count(self, domain):
        return len(self.search(domain))

    def create(self, values):
        record = {"id": len(self.items) + 1, "active": True, **self.env.relational(values)}
        if self.name == "fetchmail.server":
            record.setdefault("state", "draft")
            record.setdefault("date", False)
        self.items.append(record)
        self.env.count += 1
        return Records(self, [record])

    def process_email_queue(self):
        self.env.queue_sent += 1

    def get_param(self, key):
        return self.env.params.get(key)


class Cr:
    dbname = "acme_odoo_stg"

    def commit(self):
        pass

    def rollback(self):
        pass


class Env:
    """A copy of a production as odoo neutralize leaves it: a real alias
    domain on the company, aliases on it (one without a name), the outgoing
    server invalid:1025 and the customer's own archived, the customer's IMAP
    server archived, every cron off."""

    MODELS = ("mail.alias.domain", "res.company", "mail.alias", "ir.mail_server", "fetchmail.server",
              "ir.cron", "mail.message", "mail.mail", "ir.config_parameter")

    def __init__(self, neutralized=True):
        self.cr, self.fetched, self.queue_sent, self.count = Cr(), [], 0, 0
        self.params = {"database.is_neutralized": "True"} if neutralized else {}
        self.models = {n: Model(self, n, [], n in ("ir.mail_server", "fetchmail.server", "ir.cron"))
                       for n in self.MODELS}
        real = self["mail.alias.domain"].create({"name": "acme.com", "catchall_alias": "catchall",
                                                 "bounce_alias": "bounce"})
        self["res.company"].create({"name": "Acme", "alias_domain_id": real})
        for name in ("sales", "jobs", "bounce", False):
            self["mail.alias"].create({"alias_name": name, "alias_domain_id": real})
        self["ir.mail_server"].create({"name": "neutralized", "smtp_host": "invalid", "smtp_port": 1025})
        self["ir.mail_server"].create({"name": "customer", "smtp_host": "smtp.acme.com", "smtp_port": 587,
                                       "active": False})
        self["fetchmail.server"].create({"name": "customer", "server": "imap.acme.com", "port": 993,
                                         "user": "info@acme.com", "active": False})
        self.crons = {x: self["ir.cron"].create({"name": x, "active": False, "lastcall": False})
                      for x in ("mail.ir_cron_mail_scheduler_action", "mail.ir_cron_mail_gateway_action",
                                "base.other")}

    def __getitem__(self, name):
        return self.models[name]

    def __contains__(self, name):
        return name in self.models

    def relational(self, values):
        """A many2one written as an id: the record (as Odoo reads it back)."""
        found = dict(values)
        if isinstance(found.get("alias_domain_id"), int) and not isinstance(found["alias_domain_id"], bool):
            found["alias_domain_id"] = self["mail.alias.domain"].search([("id", "=", found["alias_domain_id"])])
        return found

    def ref(self, xmlid, raise_if_not_found=True):
        return self.crons.get(xmlid)

    def invalidate_all(self):
        pass


def run_script(env, mode, domain="acme-odoo-stg.test"):
    text = composehelper.inbox_input(json.dumps({"mode": mode, "domain": domain}).encode()).decode()
    printed = []
    exec(compile(text, "mail_inbox.py", "exec"), {"env": env, "print": printed.append})  # noqa: S102 - the script as odoo shell runs it
    assert printed[-1].startswith("OMNIACLI-MAIL ")
    return json.loads(printed[-1][len("OMNIACLI-MAIL "):])


def test_the_configuration_of_odoo_is_idempotent():
    env = Env()
    before = env.count
    state = run_script(env, "state")
    assert state["on"] is False and state["mailboxes"] == [] and env.count == before
    answer = run_script(env, "on")
    assert answer["on"] is True and answer["domain"] == "acme-odoo-stg.test"
    # catchall and each alias with a name, not the bounce one
    assert answer["mailboxes"] == ["catchall@acme-odoo-stg.test", "jobs@acme-odoo-stg.test",
                                   "sales@acme-odoo-stg.test"]
    assert [s["user"] for s in answer["servers"]] == answer["mailboxes"]
    assert all(s["state"] == "done" and s["active"] for s in answer["servers"])
    assert answer["company_domains"] == ["acme-odoo-stg.test"] and answer["other_servers"] == 0
    assert answer["crons"]["ir_cron_mail_scheduler_action"]["active"] is True
    assert answer["crons"]["ir_cron_mail_gateway_action"]["active"] is True
    # every other cron as it was; the customer's servers stay archived; the caught SMTP alone
    assert env.crons["base.other"].active is False
    assert not env["fetchmail.server"].search([("server", "=", "imap.acme.com")]).active
    assert [s["smtp_host"] for s in env["ir.mail_server"].items if s["active"]] == ["invalid"]
    assert answer["said"] and answer["fetched"] is None
    # again: nothing written, nothing said
    before = env.count
    again = run_script(env, "on")
    assert again["said"] == [] and env.count == before and again["on"] is True
    # an alias made since gets its mailbox; fetch reads each one, the queue sent first
    env["mail.alias"].create({"alias_name": "helpdesk", "alias_domain_id": False})
    fetched = run_script(env, "fetch")
    assert "helpdesk@acme-odoo-stg.test" in fetched["mailboxes"] and env.queue_sent == 1
    assert sorted(env.fetched) == fetched["mailboxes"] and fetched["fetched"] == 4
    assert fetched["last_fetch"] == "2026-11-21T09:00:00"


def test_a_customer_s_server_switched_on_again_is_archived():
    env = Env()
    run_script(env, "on")
    customer = env["fetchmail.server"].with_context(active_test=False).search([("server", "=", "imap.acme.com")])
    customer.active = True
    smtp = env["ir.mail_server"].with_context(active_test=False).search([("smtp_host", "=", "smtp.acme.com")])
    smtp.active = True
    answer = run_script(env, "on")
    assert not customer.active and not smtp.active and len(answer["said"]) == 2


def test_the_configuration_refuses_a_database_not_neutralized():
    env = Env(neutralized=False)
    before = env.count
    answer = run_script(env, "on")
    assert "not neutralized" in answer["error"] and env.count == before


# --- the routes and the page

@pytest.fixture
def inbox_answers(monkeypatch):
    """The helper's mail actions answered here; [(action, cmd, input)] asked."""
    asked = []
    real = ops.capture
    actions = ("mail-list", "mail-get", "mail-clear", "mail-send", "mail-inbox")
    state = {"on": False}

    async def capture(cmd, *args, input=None, **kw):
        action = next((str(c) for c in cmd if str(c) in actions), None)
        if action is None:
            return await real(cmd, *args, input=input, **kw)
        asked.append((action, [str(c) for c in cmd], json.loads(input) if input else None))
        if action == "mail-list":
            return 0, json.dumps(LISTED)
        if action == "mail-get":
            return 0, json.dumps({**ONE, "MessageID": "abc.1@stg.test", "ReplyTo": [{"Address": "catchall@x.test"}]})
        if action == "mail-send":
            return 0, ""
        if action == "mail-inbox":
            mode = json.loads(input)["mode"]
            state["on"] = state["on"] or mode != "state"
            answer = {"on": state["on"], "domain": json.loads(input)["domain"], "mailboxes": ["catchall@x.test"],
                      "servers": [{"user": "catchall@x.test", "state": "done", "active": True,
                                   "date": "2026-11-21T09:00:00", "error": None}],
                      "crons": {}, "last_fetch": "2026-11-21T09:00:00", "said": [],
                      "fetched": 1 if mode == "fetch" else None}
            return 0, "odoo says something\nOMNIACLI-MAIL " + json.dumps(answer)
        return 0, "cleared"
    monkeypatch.setattr(ops, "capture", capture)
    return asked


async def test_the_incoming_mail_per_role(workspaces, client, env, monkeypatch, tmp_path, inbox_answers):  # noqa: F811
    staging, _ = await make_staging(workspaces, client, env, monkeypatch, tmp_path, inbox=True)
    url = f"/api/servers/{staging}/production/mail"
    domain = stagingmail.domain_of(staging)
    # the list: the mailboxes, and who may write
    data = (await client.get(url, headers=basic("luca"))).json()
    assert data["receives"] is True and data["can_write"] is False and data["domain"] == domain
    assert {"address": "shop@example.com", "count": 2} in data["boxes"]
    assert (await client.get(url, headers=basic("mario"))).json()["can_write"] is True
    one = (await client.get(url + "?box=cliente@example.org", headers=basic("luca"))).json()
    assert one["total"] == 1 and [m["id"] for m in one["messages"]] == [LISTED["messages"][0]["ID"]]
    assert (await client.get(url + "?box=nobody", headers=basic("luca"))).status_code == 400
    # a message: what a reply needs
    message = (await client.get(f"{url}/{ONE['ID']}", headers=basic("luca"))).json()
    assert message["message_id"] == "abc.1@stg.test" and message["reply_to"] == ["catchall@x.test"]
    assert message["to_addresses"] == ["cliente@example.org"] and message["from_address"] == "shop@example.com"
    # the state: anyone of the workspace; the domain of the staging, never another
    state = (await client.get(f"{url}/inbox", headers=basic("luca"))).json()
    assert state["on"] is False and state["can_act"] is False
    action, cmd, given = inbox_answers[-1]
    assert action == "mail-inbox" and given == {"mode": "state", "domain": domain} and cmd[-2:] == ["odoo", cmd[-1]]
    # switched on, read now, written to: the workspace's developers and admins
    payload = {**MESSAGE, "to": "catchall@" + domain, "in_reply_to": "abc.1@stg.test", "x": "dropped"}
    for path, body in (("inbox", {"fetch": False}), ("send", payload)):
        assert (await client.post(f"{url}/{path}", headers=post_as("luca"), json=body)).status_code == 403
    count = len(inbox_answers)
    assert (await client.post(f"{url}/inbox", headers=post_as("mario"), json={})).json()["on"] is True
    assert inbox_answers[count][2] == {"mode": "on", "domain": domain}
    env["settings"].workspaces["acme"].members["luca"] = "developer"
    answer = (await client.post(f"{url}/inbox", headers=post_as("luca"), json={"fetch": True})).json()
    assert answer["fetched"] == 1 and inbox_answers[-1][2]["mode"] == "fetch"
    assert (await client.post(f"{url}/send", headers=post_as("luca"), json=payload)).json() == {"ok": True}
    action, cmd, given = inbox_answers[-1]
    # the message on the helper's input, the addresses split, nothing on its command line
    assert action == "mail-send" and cmd[-1] == "mail-send" and "x" not in given
    assert given["to"] == ["catchall@" + domain] and given["in_reply_to"] == "abc.1@stg.test"
    # not of their workspace
    for user in ("eva", "zoe"):
        assert (await client.get(f"{url}/inbox", headers=basic(user))).status_code == 403
        assert (await client.post(f"{url}/send", headers=post_as(user), json=payload)).status_code == 403
    # the production receives nothing
    resp = await client.get("/api/servers/acme-odoo/production/mail/inbox", headers={"Authorization": AUTH})
    assert resp.status_code == 404 and "receives no mail" in resp.json()["error"]


async def test_a_staging_that_catches_only_receives_nothing(workspaces, client, env, monkeypatch, tmp_path,  # noqa: F811
                                                            inbox_answers):
    staging, _ = await make_staging(workspaces, client, env, monkeypatch, tmp_path)
    url = f"/api/servers/{staging}/production/mail"
    data = (await client.get(url, headers={"Authorization": AUTH})).json()
    assert data["receives"] is False and data["can_write"] is False
    assert (await client.get(f"{url}/inbox", headers={"Authorization": AUTH})).status_code == 404
    resp = await client.post(f"{url}/send", headers=post_as("mario"), json=MESSAGE)
    assert resp.status_code == 404 and not [a for a in inbox_answers if a[0] in ("mail-send", "mail-inbox")]
    page = (await client.get(f"/servers/{staging}", headers={"Authorization": AUTH})).text
    assert 'data-tab="mail"' in page and not re.search(r'<button[^>]*id="mail-(write|inbox-on)"', page)
    assert "it was made before its mailboxes" in page


async def test_the_mail_tab_of_a_staging_that_receives(workspaces, client, env, monkeypatch, tmp_path,  # noqa: F811
                                                       inbox_answers):
    staging, _ = await make_staging(workspaces, client, env, monkeypatch, tmp_path, inbox=True)
    page = (await client.get(f"/servers/{staging}", headers=basic("mario"))).text
    panel = page[page.index('data-panel="mail"'):page.index("</section>", page.index('data-panel="mail"'))]
    for button in ('id="mail-write"', 'id="mail-inbox-on"', 'id="mail-inbox-fetch"', 'id="mm-reply"'):
        assert button in panel, button
    # still no primary in the tab: the one of Write as… is its dialog's
    assert 'class="primary' not in panel and 'id="mail-write-dialog"' in page
    dialog = page[page.index('id="mail-write-dialog"'):page.index("</dialog>", page.index('id="mail-write-dialog"'))]
    assert 'class="primary" id="mw-send"' in dialog and f'catchall@{stagingmail.domain_of(staging)}' in dialog
    for button in re.findall(r"<button[^>]*>", panel + dialog) + re.findall(r"<select[^>]*>", panel):
        assert re.search(r'title="[^"]+"', button), button
    # the workspace's user reads: no writing, no switch
    page = (await client.get(f"/servers/{staging}", headers=basic("luca"))).text
    assert 'data-tab="mail"' in page and 'class="mail-inbox' in page and 'id="mail-write-dialog"' not in page
    assert not re.search(r'<button[^>]*id="(mail-write|mail-inbox-on|mm-reply)"', page)
