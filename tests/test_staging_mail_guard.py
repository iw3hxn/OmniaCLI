# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A staging whose mail is caught sends none, whatever its database says
(UX-008 §5.3): its network refuses SMTP outside itself (made at create,
kept for the boot, gone with the instance); a restore into it is
neutralized even when not asked; its SMTP options are shown and not saved."""
import json
import re

import pytest
import yaml

from omniacli import composehelper, stacktemplates, workspacehelper
from omniacli.config import parse_servers
from omniacli import plugin_api
from omniacli.plugin_api import catches_mail

from .test_app import client, wait_job  # noqa: F401 - the client fixture
from .test_docker import _zip, helper, job  # noqa: F401 - the helper fixture
from .test_stacktemplates import REPO_TEMPLATES, root  # noqa: F401 - the root fixture
from .test_workspace_parameters import made  # noqa: F401 - the made fixture
from .test_workspace_repositories import instance  # noqa: F401 - the instance fixture
from .test_workspaces import basic, post_as, workspaces  # noqa: F401 - the workspaces fixture

MAIL = workspacehelper.CHAIN_MAIL


def _mail_rules(calls):
    return [c[4:] for c in calls if c[:4] == ["iptables", "-w", "-A", MAIL]]


def test_the_rule_of_a_staging_s_network():
    [rule] = workspacehelper.mail_guard_rules(["10.213.4.0/24", "10.213.4.0/24"])
    # from its network to anything but itself (its mail service is in it), the SMTP ports refused at once
    assert rule == ["-s", "10.213.4.0/24", "!", "-d", "10.213.4.0/24", "-p", "tcp", "-m", "multiport",
                    "--dports", "25,465,587,2525", "-j", "REJECT", "--reject-with", "tcp-reset"]
    assert str(stacktemplates.MAIL_SMTP_PORT) not in rule[rule.index("--dports") + 1].split(",")
    assert workspacehelper.mail_guard_rules([]) == []


def test_made_at_create_kept_for_the_boot_gone_with_the_instance(root, monkeypatch):  # noqa: F811
    storage, calls, state = root["storage"], root["calls"], root["state"]
    workspacehelper.disk("acme", str(storage), "2")
    workspacehelper.limits("acme", "0", "0")
    # an instance that is no staging: no rule
    workspacehelper.create("acme", str(storage), "shop", "odooplm-20", json.dumps({"_roles": ["production"]}))
    assert not _mail_rules(calls) and not workspacehelper.MAIL_GUARD.exists()
    assert ("DOCKER-USER", MAIL) in state["jumps"]
    calls.clear()
    made = workspacehelper.create("acme", str(storage), "stg", "odooplm-20",
                                  json.dumps({"_roles": ["production"], workspacehelper.MAIL_KEY: True}))
    subnet = made["roles"]["production"]["subnet"]
    assert made["mail_guarded"] == [subnet]
    assert _mail_rules(calls) == workspacehelper.mail_guard_rules([subnet])
    project = str(storage / "acme" / "stg" / "production")
    assert json.loads(workspacehelper.MAIL_GUARD.read_text()) == {project: subnet}
    assert workspacehelper.MAIL_GUARD.stat().st_mode & 0o777 == 0o600
    # its network is not given to another project while the rule stands
    assert subnet not in workspacehelper.pick_subnets(3)
    # at boot the firewall comes before the disks: not mounted, the rule kept
    state["mounted"] = False
    calls.clear()
    assert workspacehelper.firewall("apply")["mail_guarded"] == [subnet]
    assert _mail_rules(calls) == workspacehelper.mail_guard_rules([subnet])
    # nor the storage bound yet (its .disks not there): kept too
    (storage / ".disks").rename(storage / "disks-away")
    calls.clear()
    assert workspacehelper.firewall("apply")["mail_guarded"] == [subnet]
    (storage / "disks-away").rename(storage / ".disks")
    # removed: its network is free, the rule gone (the jump stays, the chain empty)
    state["mounted"] = True
    monkeypatch.setattr(workspacehelper.subprocess, "run",
                        lambda cmd, **kw: type("Done", (), {"returncode": 0})())
    monkeypatch.setattr(composehelper.shutil, "which", lambda name, path=None: "/usr/bin/docker")
    calls.clear()
    workspacehelper.remove("acme", str(storage), "stg")
    assert ["iptables", "-w", "-F", MAIL] in calls and not _mail_rules(calls)
    assert json.loads(workspacehelper.MAIL_GUARD.read_text()) == {}
    # firewall off: the chain and the file gone
    workspacehelper.firewall("off")
    assert ("DOCKER-USER", MAIL) not in state["jumps"] and not workspacehelper.MAIL_GUARD.exists()


def test_a_disk_gone_takes_its_rule(root):  # noqa: F811
    storage = root["storage"]
    # the storage bound (a disk on it); gone: the disk of the first, the storage of the second
    workspacehelper.disk("acme", str(storage), "2")
    workspacehelper.MAIL_GUARD.write_text(json.dumps({str(storage / "gone" / "stg" / "production"): "10.213.9.0/24",
                                                      "/elsewhere/acme/stg/production": "10.213.8.0/24"}))
    assert workspacehelper.firewall("apply")["mail_guarded"] == []
    assert json.loads(workspacehelper.MAIL_GUARD.read_text()) == {}


def test_a_staging_made_before_the_rule_gets_it_at_the_next_apply(root):  # noqa: F811
    storage = root["storage"]
    workspacehelper.disk("acme", str(storage), "2")
    workspacehelper.limits("acme", "0", "0")
    made = workspacehelper.create("acme", str(storage), "stg", "odooplm-20",
                                  json.dumps({"_roles": ["production"], workspacehelper.MAIL_KEY: True}))
    workspacehelper.MAIL_GUARD.unlink()
    assert workspacehelper.firewall("apply")["mail_guarded"] == [made["roles"]["production"]["subnet"]]


def _staging(tmp_path, caught=True):
    folder = tmp_path / "acme" / "stg"
    (folder / "production").mkdir(parents=True)
    (folder / "instance.json").write_text(json.dumps({"template": "odoo-19", **({"mail_capture": True} if caught else {})}))
    data = {"type": "docker", "production": {"restart": {"type": "compose", "service": str(folder / "production")},
                                             "repositories": [], "database": "stg"}}
    return parse_servers({"acme-stg": data})["acme-stg"].production


async def test_a_restore_into_a_capturing_staging_is_neutralized(helper, tmp_path):  # noqa: F811
    calls, answers = helper
    answers["version"] = (0, "Odoo Server 20.0-20260926")
    backup = _zip(tmp_path / "prod.zip")
    inst = _staging(tmp_path)
    # the plugins read instance.json as the helper writes it
    assert (plugin_api.INSTANCE_FILE, plugin_api.MAIL_CAPTURE) == (workspacehelper.INSTANCE_FILE, workspacehelper.MAIL_META)
    assert catches_mail(inst)
    done = job()
    await inst.plugin.restore(done, inst, backup, {"neutralize": False})
    assert ["load", "odoo", "stg", "--neutralize"] in calls
    assert any("is a staging whose mail is caught: the database is neutralized even though it was not asked" in line
               for line in done.lines)
    assert not any("not neutralized" in line for line in done.lines)
    # load only too
    calls.clear()
    await inst.plugin.restore(job(), inst, backup, {"neutralize": False, "load_only": True})
    assert calls[-1] == ["load", "odoo", "stg", "--neutralize"]


async def test_a_restore_elsewhere_follows_the_choice(helper, tmp_path):  # noqa: F811
    calls, answers = helper
    answers["version"] = (0, "Odoo Server 20.0-20260926")
    inst = _staging(tmp_path, caught=False)
    assert not catches_mail(inst)
    done = job()
    await inst.plugin.restore(done, inst, _zip(tmp_path / "prod.zip"), {"neutralize": False})
    assert ["load", "odoo", "stg"] in calls and any("not neutralized" in line for line in done.lines)


async def test_the_smtp_options_of_a_capturing_staging(made, client, monkeypatch):  # noqa: F811
    from omniacli import ops
    folder = made["folder"]
    (folder / "instance.json").write_text(json.dumps({"template": "odooplm-20", "version": "x", "database": "main",
                                                      "mail_capture": True}))
    reason = "The mail of this staging goes to the Mail tab: these values are not used"
    page = (await client.get("/servers/acme-main", headers=basic("mario"))).text
    for name in stacktemplates.MAIL_PARAMETERS:
        field = re.search(rf'<input [^>]*data-param="{name}"[^>]*>', page).group(0)
        assert " disabled" in field and 'data-mail="1"' in field and f'title="{reason}"' in field, field
    field = re.search(r'<input [^>]*data-param="workers"[^>]*>', page).group(0)
    assert "disabled" not in field and "data-mail" not in field
    # the Italian page says it in Italian
    page = (await client.get("/servers/acme-main", headers={**basic("mario"), "Accept-Language": "it"})).text
    assert 'title="La posta di questa staging va alla scheda Posta: questi valori non si usano"' in page
    # sent anyway (an older page, a script): not saved
    url = "/api/workspaces/acme/parameters/save"
    body = {"server": "acme-main", "role": "production", "debug": False,
            "parameters": {"workers": "2", "smtp_server": "smtp.example.com", "smtp_port": "587"},
            "extra": {"smtp_user": "u@example.com"}}
    resp = await client.post(url, headers=post_as("mario"), json=body)
    assert resp.status_code == 200, resp.text
    await wait_job(client, resp.json()["job"])
    spec = json.loads(made["confs"][-1][-1])
    assert spec["parameters"] == {"workers": "2"} and spec["extra"] == {}
    # the Extended view greys them with the reason
    (folder / "production" / "config").mkdir(parents=True, exist_ok=True)
    (folder / "production" / "config" / "odoo.conf").write_text("[options]\nsmtp_server = mail\n")

    async def capture(cmd, **kwargs):
        options = [{"name": n, "help": "h", "kind": "string", "group": "g", "default": None}
                   for n in ("workers", "smtp_user")]
        return 0, stacktemplates.OPTIONS_MARK + json.dumps({"options": options, "version": "20.0"})
    monkeypatch.setattr(ops, "capture", capture)
    resp = await client.post("/api/workspaces/acme/parameters/options", headers=post_as("mario"),
                             json={"server": "acme-main", "role": "production"})
    assert resp.json()["mail"] == sorted(stacktemplates.MAIL_PARAMETERS)
    resp = await client.post("/api/workspaces/acme/parameters/options", headers=post_as("mario"),
                             json={"server": "acme-main", "role": "test"})
    assert resp.json()["mail"] == []


def test_conf_keeps_no_smtp_option_of_a_capturing_staging(root):  # noqa: F811
    storage = root["storage"]
    workspacehelper.disk("acme", str(storage), "2")
    workspacehelper.limits("acme", "0", "0")
    workspacehelper.create("acme", str(storage), "stg", "odooplm-20",
                           json.dumps({"_roles": ["production"], workspacehelper.MAIL_KEY: True}))
    workspacehelper.conf("acme", str(storage), "stg", "production",
                         json.dumps({"parameters": {"smtp_server": "smtp.example.com", "workers": 2},
                                     "debug": False}))
    kept = json.loads((storage / "acme" / "stg" / "production" / workspacehelper.ROLE_FILE).read_text())
    assert kept["parameters"] == {"workers": 2} and "extra" not in kept


def test_the_texts_are_in_italian():
    from omniacli import i18n
    assert i18n.tr("it", "--- acme-stg/production is a staging whose mail is caught: the database is neutralized "
                         "even though it was not asked").startswith("--- acme-stg/production è una staging")


@pytest.mark.parametrize("name", ["odoo-18", "odoo-19", "odooplm-20"])
def test_the_mail_service_is_in_the_project_s_network(name):
    """The rule spares the project's own network: the templates' mail service is on it, not on another."""
    services = yaml.safe_load(stacktemplates.load_template(REPO_TEMPLATES / name).compose)["services"]
    assert list(services[stacktemplates.MAIL_SERVICE]["networks"]) == ["default"]
