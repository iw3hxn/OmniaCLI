# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A staging's mail, caught (UX-008 §5.3): the templates' mail service, run
for a staging only and published nowhere; its Odoo pointed at it; the
helper's actions with checked arguments; the routes per role; the Mail tab
only on a staging whose mail is caught."""

import json
import re

import pytest
import yaml

from omniacli import composehelper, ops, stacktemplates, stagingmail, workspacehelper
from omniacli.composehelper import HelperError

from .test_app import AUTH, POST, client, wait_job  # noqa: F401 - the client fixture
from .test_stacktemplates import REPO_TEMPLATES, copy_template, root  # noqa: F401 - the root fixture
from .test_workspaces import basic, post_as, workspaces  # noqa: F401 - the workspaces fixture

TEMPLATES = ("odoo-18", "odoo-19", "odooplm-20")
# the server odoo neutralize adds to a database (base/data/neutralize.sql, Odoo 16 to 20)
NEUTRALIZED_HOST, NEUTRALIZED_PORT = "invalid", 1025


@pytest.mark.parametrize("name", TEMPLATES)
def test_the_mail_service_of_the_templates(name):
    template = stacktemplates.load_template(REPO_TEMPLATES / name)
    assert template.captures_mail()
    services = yaml.safe_load(template.compose)["services"]
    mail = services[stacktemplates.MAIL_SERVICE]
    # a staging's only, and reachable from its project's network only
    assert mail["profiles"] == [stacktemplates.MAIL_PROFILE]
    assert "ports" not in mail and all("ports" not in s or s is services["odoo"] for s in services.values())
    assert re.fullmatch(r"axllent/mailpit:v\d+\.\d+\.\d+", mail["image"])
    # the neutralized database's server is this service; its API on its own loopback
    assert mail["networks"] == {"default": {"aliases": [NEUTRALIZED_HOST]}}
    assert mail["environment"]["MP_SMTP_BIND_ADDR"] == f"0.0.0.0:{NEUTRALIZED_PORT}" == \
        f"0.0.0.0:{stacktemplates.MAIL_SMTP_PORT}"
    assert composehelper.MAIL_API.startswith(f"http://{mail['environment']['MP_UI_BIND_ADDR']}/")
    assert mail["read_only"] is True and mail["user"] == "65534:65534" and mail["volumes"] == ["./data/mail:/data"]
    assert {"path": "data/mail", "owner": "65534:65534"} in template.data
    # the Odoo and the database run with or without the profile
    assert "profiles" not in services["odoo"] and "profiles" not in services["db"]
    assert composehelper.MAIL_SERVICE == stacktemplates.MAIL_SERVICE


@pytest.mark.parametrize("change, message", [
    (("    image: ${IMAGE}\n", "    image: ${IMAGE}\n    profiles: [x]\n"), "runs always, without profiles"),
    (("    image: ${IMAGE}\n", "    image: ${IMAGE}\n    profiles: [Bad_Name]\n"), "profiles: a list of names"),
    (("        aliases:\n          - invalid\n", "        ipv4_address: 10.213.0.9\n"), "networks: only default"),
    (("        aliases:\n          - invalid\n", "        aliases:\n          - db\n"), "networks: only default"),
    (("    networks:\n      default:\n", "    networks:\n      other:\n"), "networks: only default"),
])
def test_compose_rules_of_profiles_and_networks(tmp_path, change, message):
    text = (REPO_TEMPLATES / "odooplm-20" / "compose.yaml").read_text()
    assert change[0] in text, change[0]
    folder = copy_template(tmp_path, text.replace(change[0], change[1], 1))
    with pytest.raises(stacktemplates.TemplateError, match=message):
        stacktemplates.load_template(folder)


def test_the_odoo_conf_of_a_staging():
    template = stacktemplates.load_template(REPO_TEMPLATES / "odoo-19")
    text = stacktemplates.conf_text(template, "shop", "pw", [], {"smtp_server": "smtp.example.com", "smtp_port": 587,
                                                                 "workers": 2}, extra={"smtp_user": "x"},
                                    mail_capture=True)
    keys = [line.partition("=")[0].strip() for line in text.splitlines() if "=" in line]
    # each option once (Odoo refuses a key twice), the server the project's service
    assert len(keys) == len(set(keys))
    assert "smtp_server = mail\n" in text and "smtp_port = 1025\n" in text and "smtp_ssl = False\n" in text
    assert "smtp.example.com" not in text and "smtp_user" not in text and "workers = 2\n" in text
    assert "smtp_server" not in stacktemplates.conf_text(template, "shop", "pw", [])


def test_a_staging_made_by_the_helper(root):  # noqa: F811
    storage = root["storage"]
    workspacehelper.disk("acme", str(storage), "2")
    workspacehelper.limits("acme", "0", "0")
    made = workspacehelper.create("acme", str(storage), "stg", "odooplm-20",
                                  json.dumps({"_roles": ["production"], workspacehelper.MAIL_KEY: True}))
    assert made["mail_capture"] is True and workspacehelper.MAIL_KEY not in made["values"]
    project = storage / "acme" / "stg" / "production"
    assert "COMPOSE_PROFILES=mail-capture\n" in (project / ".env").read_text()
    assert "smtp_server = mail\n" in (project / "config" / "odoo.conf").read_text()
    assert (project / "data" / "mail").is_dir()
    meta = json.loads((storage / "acme" / "stg" / "instance.json").read_text())
    assert meta["mail_capture"] is True and workspacehelper.captures_mail(meta, "production")
    # conf writes it again: the parameters do not take the mail out
    found = workspacehelper.conf("acme", str(storage), "stg", "production",
                                 json.dumps({"parameters": {"smtp_server": "smtp.example.com"}}))
    text = (project / "config" / "odoo.conf").read_text()
    assert found["mail_capture"] is True and "smtp_server = mail\n" in text and "smtp.example.com" not in text
    # an instance that is no staging: no profile, no mail service, its SMTP its own
    workspacehelper.create("acme", str(storage), "shop", "odooplm-20", json.dumps({"_roles": ["production"]}))
    project = storage / "acme" / "shop" / "production"
    assert "COMPOSE_PROFILES" not in (project / ".env").read_text()
    assert json.loads((storage / "acme" / "shop" / "instance.json").read_text()).get("mail_capture") is None
    found = workspacehelper.conf("acme", str(storage), "shop", "production",
                                 json.dumps({"parameters": {"smtp_server": "smtp.example.com"}}))
    assert found["mail_capture"] is False
    assert "smtp_server = smtp.example.com\n" in (project / "config" / "odoo.conf").read_text()


def test_a_template_of_before_catches_nothing(root, tmp_path):  # noqa: F811
    folder = tmp_path / "templates" / "odooplm-20"
    text = (folder / "compose.yaml").read_text()
    (folder / "compose.yaml").write_text(text[:text.index("  # a staging's mail")])
    storage = root["storage"]
    workspacehelper.disk("acme", str(storage), "2")
    workspacehelper.limits("acme", "0", "0")
    made = workspacehelper.create("acme", str(storage), "stg", "odooplm-20",
                                  json.dumps({"_roles": ["production"], workspacehelper.MAIL_KEY: True}))
    assert made["mail_capture"] is False
    assert "COMPOSE_PROFILES" not in (storage / "acme" / "stg" / "production" / ".env").read_text()


def test_the_helpers_mail_actions(tmp_path, monkeypatch):
    monkeypatch.setattr(composehelper, "services", lambda project: ["odoo", "db", "mail"])
    [cmd] = composehelper.commands(tmp_path, "mail-list", ["0", "50"])
    assert cmd[:3] == ["exec", "-T", "mail"] and cmd[-2:] == ["0", "50"]
    # the numbers and the id are arguments of the script, never in its text
    assert "$1" in cmd[-4] and "$2" in cmd[-4] and "127.0.0.1:8025" in cmd[-4]
    [cmd] = composehelper.commands(tmp_path, "mail-get", ["0E9EbwBBccKOxCBKXQEuu7"])
    assert cmd[-1] == "0E9EbwBBccKOxCBKXQEuu7" and "/message/$1" in cmd[-3]
    [cmd] = composehelper.commands(tmp_path, "mail-clear", [])
    assert cmd[:3] == ["exec", "-T", "mail"] and "DELETE /api/v1/messages" in cmd[-2]
    for action, args in (("mail-list", ["0"]), ("mail-list", ["-1", "50"]), ("mail-list", ["0", "501"]),
                         ("mail-list", ["0", "0"]), ("mail-list", ["0", "5;0"]), ("mail-list", ["0", "1e3"]),
                         ("mail-get", []), ("mail-get", ["../../x"]), ("mail-get", ["abc"]),
                         ("mail-get", ["a" * 41]), ("mail-get", ["0E9EbwBB$(id)"]), ("mail-clear", ["all"])):
        with pytest.raises(HelperError):
            composehelper.commands(tmp_path, action, args)
    # a project without the service (an instance, a staging of before): refused
    monkeypatch.setattr(composehelper, "services", lambda project: ["odoo", "db"])
    with pytest.raises(HelperError, match="no service 'mail'"):
        composehelper.commands(tmp_path, "mail-list", ["0", "50"])


LISTED = {"total": 2, "messages_count": 2, "messages": [
    {"ID": "0E9EbwBBccKOxCBKXQEuu7", "From": {"Name": "Shop", "Address": "shop@example.com"},
     "To": [{"Name": "", "Address": "cliente@example.org"}], "Subject": "Preventivo S00042 " + "x" * 90,
     "Created": "2026-11-21T09:09:26.658Z", "Attachments": 1},
    {"ID": "7OiPbEpfDX4Snt8JpfKi9i", "From": {"Name": "", "Address": "shop@example.com"},
     "To": [], "Subject": "Fattura", "Created": "2026-11-20T08:00:00Z", "Attachments": 0}]}
ONE = {"ID": "0E9EbwBBccKOxCBKXQEuu7", "From": {"Name": "Shop", "Address": "shop@example.com"},
       "To": [{"Name": "Cliente", "Address": "cliente@example.org"}], "Cc": [{"Name": "", "Address": "u@example.org"}],
       "Subject": "Preventivo S00042", "Date": "2026-11-21T09:09:26Z", "Text": "testo",
       "HTML": "<p>ciao</p><script>alert(1)</script>", "ReturnPath": "shop@example.com",
       "Attachments": [{"PartID": "2", "FileName": "S00042.pdf", "ContentType": "application/pdf", "Size": 13}]}


def test_mailpit_s_answer_as_the_page_reads_it():
    row = stagingmail._row(LISTED["messages"][0])
    assert row["from"] == "Shop <shop@example.com>" and row["to"] == ["cliente@example.org"]
    assert re.fullmatch(r"2026-11-2[01]T\d\d:\d\d:\d\d", row["received"])
    assert stagingmail.local_time("nonsense") == "nonsense"
    with pytest.raises(Exception, match="did not answer"):
        stagingmail._answer("Error response from daemon: container is not running")
    assert stagingmail._answer('WARN something\n{"total": 0}') == {"total": 0}


async def make_staging(workspaces, client, env, monkeypatch, tmp_path, caught=True):  # noqa: F811
    """acme-odoo, an instance of acme, and its staging made as the page asks
    it (the helper and the backup answered here); instance.json of the
    staging as the helper writes it."""
    from omniacli import workspaceops

    settings = env["settings"]
    template = stacktemplates.load_template(REPO_TEMPLATES / "odoo-19")
    monkeypatch.setattr(workspaceops, "templates", lambda: {template.name: template})
    settings.storage_roots = {"main": str(tmp_path)}
    ws = settings.workspaces["acme"]
    ws.storage, ws.limits = "main", {"disk_gb": 50}
    monkeypatch.setattr(workspaceops, "storage_path", lambda w: tmp_path)
    ports = iter(range(20000, 20100, 2))
    asked = []

    async def create_instance(job, w, instance, tpl, values, setup=None):
        asked.append(dict(values))
        port = next(ports)
        folder = tmp_path / "acme" / instance
        folder.mkdir(parents=True, exist_ok=True)
        meta = {"template": "odoo-19", **({"mail_capture": True} if caught and values.get(workspacehelper.MAIL_KEY) else {})}
        (folder / "instance.json").write_text(json.dumps(meta))
        return {"type": "docker", "production": {
            "database": instance.replace("-", "_"), "odoo_service": "odoo", "db_service": "db", "http_port": port,
            "update_modules": True, "restart": {"type": "compose", "service": str(folder / "production"), "sudo": True},
            "repositories": []}}

    async def publish(job, w):
        return {}

    async def backup_into(job, instance, folder, options):
        return tmp_path / "odoo_20261121.zip"
    monkeypatch.setattr(workspaceops, "create_instance", create_instance)
    monkeypatch.setattr(workspaceops, "publish", publish)
    monkeypatch.setattr(ops, "backup_into", backup_into)
    resp = await client.post("/api/workspaces/acme/instances/create", headers=post_as("mario"),
                             json={"instance": "odoo", "template": "odoo-19"})
    assert (await wait_job(client, resp.json()["job"]))["status"] == "success"
    plugin = type(settings.servers["acme-odoo"].production.plugin)

    async def restore(self, job, instance, backup_zip, options):
        return None
    monkeypatch.setattr(plugin, "restore", restore)
    resp = await client.post("/api/servers/acme-odoo/stagings", headers=POST)
    assert resp.status_code == 200, resp.text
    assert (await wait_job(client, resp.json()["job"]))["status"] == "success"
    return resp.json()["server"], asked


@pytest.fixture
def helper_answers(monkeypatch):
    """compose-helper's mail actions answered as Mailpit does (the restart
    command of the tests records them, the helper's action and arguments
    last); the commands asked."""
    asked = []
    real = ops.capture
    actions = ("mail-list", "mail-get", "mail-clear")

    async def capture(cmd, *args, **kw):
        action = next((str(c) for c in cmd if str(c) in actions), None)
        if action is None:
            return await real(cmd, *args, **kw)
        asked.append([str(c) for c in cmd])
        if action == "mail-list":
            return 0, json.dumps(LISTED)
        if action == "mail-get":
            if cmd[-1] != ONE["ID"]:
                return 1, "wget: server returned error: HTTP/1.1 404 Not Found"
            return 0, json.dumps(ONE)
        return 0, "cleared"
    monkeypatch.setattr(ops, "capture", capture)
    return asked


async def test_the_mail_of_a_staging_per_role(workspaces, client, env, monkeypatch, tmp_path, helper_answers):  # noqa: F811
    staging, asked = await make_staging(workspaces, client, env, monkeypatch, tmp_path)
    # the instance made for the staging asked its mail caught; the production did not
    assert asked[0].get(workspacehelper.MAIL_KEY) is None and asked[1][workspacehelper.MAIL_KEY] is True
    url = f"/api/servers/{staging}/production/mail"
    # the machine's admin, the workspace's admin, its user: they read it
    for headers in ({"Authorization": AUTH}, basic("mario"), basic("luca")):
        resp = await client.get(url, headers=headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["total"] == 2 and [m["id"] for m in data["messages"]] == [m["ID"] for m in LISTED["messages"]]
        assert data["messages"][0]["from"] == "Shop <shop@example.com>" and data["first"] == 50
    assert helper_answers[-1][-3:] == ["mail-list", "0", "50"]
    assert (await client.get(url + "?every=1", headers=basic("luca"))).status_code == 200
    assert helper_answers[-1][-3:] == ["mail-list", "0", str(composehelper.MAIL_LIMIT)]
    assert (await client.get(url, headers=basic("luca"))).json()["can_clear"] is False
    assert (await client.get(url, headers=basic("mario"))).json()["can_clear"] is True
    # not of their workspaces
    for user in ("eva", "zoe"):
        assert (await client.get(url, headers=basic(user))).status_code == 403
    # one message: as it was, without its raw headers
    resp = await client.get(f"{url}/{ONE['ID']}", headers=basic("luca"))
    message = resp.json()
    assert message["to"] == ["Cliente <cliente@example.org>"] and message["cc"] == ["u@example.org"]
    assert message["attachments"] == [{"name": "S00042.pdf", "size": 13}] and "<script>" in message["html"]
    assert "ReturnPath" not in json.dumps(message) and "shop@example.com" in message["from"]
    assert (await client.get(f"{url}/ZZZZZZZZZZZZ", headers=basic("luca"))).status_code == 404
    assert (await client.get(f"{url}/bad", headers=basic("luca"))).status_code == 404
    # emptied: the workspace's admin only
    assert (await client.post(f"{url}/clear", headers=post_as("luca"))).status_code == 403
    count = len(helper_answers)
    assert (await client.post(f"{url}/clear", headers=post_as("mario"))).json() == {"ok": True}
    assert helper_answers[count][-1] == "mail-clear" and len(helper_answers) == count + 1
    # the production catches nothing
    resp = await client.get("/api/servers/acme-odoo/production/mail", headers={"Authorization": AUTH})
    assert resp.status_code == 404 and "catches no mail" in resp.json()["error"]


async def test_the_mail_tab_on_a_staging_only(workspaces, client, env, monkeypatch, tmp_path, helper_answers):  # noqa: F811
    staging, _ = await make_staging(workspaces, client, env, monkeypatch, tmp_path)
    page = (await client.get(f"/servers/{staging}", headers={"Authorization": AUTH})).text
    assert 'data-tab="mail"' in page and 'data-panel="mail"' in page
    assert "the outgoing mail is read in the Mail tab, it does not leave" in page
    tabs = re.findall(r'<button type="button" role="tab" data-tab="(\w+)"', page)
    # the six tabs of UX-008 §10, and the Mail of a staging (§5.3) beside them
    assert tabs[:2] == ["overview", "mail"] and len(tabs) <= 7
    # the message in a frame without scripts; no primary in the tab
    frame = re.search(r'<iframe id="mm-body"[^>]*>', page).group(0)
    assert 'sandbox=""' in frame and "allow-scripts" not in frame
    panel = page[page.index('data-panel="mail"'):page.index("</section>", page.index('data-panel="mail"'))]
    assert 'class="primary' not in panel and 'id="mail-clear"' in panel
    for button in re.findall(r"<button[^>]*>", panel):
        assert re.search(r'title="[^"]+"', button), button
    # the user of the workspace reads, does not empty
    page = (await client.get(f"/servers/{staging}", headers=basic("luca"))).text
    assert 'data-tab="mail"' in page and 'id="mail-clear"' not in page
    # the production has no Mail tab
    page = (await client.get("/servers/acme-odoo", headers={"Authorization": AUTH})).text
    assert 'data-tab="mail"' not in page


async def test_a_staging_of_before_has_no_mail_tab(workspaces, client, env, monkeypatch, tmp_path, helper_answers):  # noqa: F811
    staging, _ = await make_staging(workspaces, client, env, monkeypatch, tmp_path, caught=False)
    page = (await client.get(f"/servers/{staging}", headers={"Authorization": AUTH})).text
    assert 'data-tab="mail"' not in page and "neutralized: no outgoing mail, no scheduled actions." in page
    resp = await client.get(f"/api/servers/{staging}/production/mail", headers={"Authorization": AUTH})
    assert resp.status_code == 404 and not helper_answers


def test_the_refusals_are_in_italian():
    from omniacli import i18n
    assert i18n.tr("it", "acme-odoo-stg-20261121 catches no mail: it is no staging, or one made before the mail "
                         "was caught (its outgoing mail is off)").startswith("acme-odoo-stg-20261121 non cattura")
    assert i18n.tr("it", "the caught mail cannot be read (is the staging running?): no such service") == \
        "la posta catturata non si legge (la staging è accesa?): no such service"
    assert i18n.tr("it", "No message ZZZZ among the caught mail") == "Nessun messaggio ZZZZ nella posta catturata"
