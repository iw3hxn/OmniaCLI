# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The review of the monitor's slice 2 (AD-019): the log of the instance and never of a compose run, a long
catch-up that keeps its spans whole, causes of instances measured no more, causes kept at once, no text of the
customer in a page's path, health checks and the monitor's own check not counted, a read given up ends its helper."""

import asyncio
import json
from pathlib import Path

import pytest

from omniacli import composehelper, instancelog, monitor as monitor_module, notify, odoolog
from omniacli.instancemonitor import InstanceHistory

from .test_instance_log import NOW, _Inst, _monitor, access, record


def test_the_log_of_a_compose_run_is_never_the_instances(tmp_path, monkeypatch):
    """docker ps on the project's folder and the service also finds the containers of compose run: an update, a
    verify, the preview of the new code (next_name, run -d). Only the service's own container (oneoff False, not the
    preview) is the instance's log: a round during an update must not read the update's log as «recreated»."""
    project = tmp_path / "acme"
    project.mkdir()
    (project / ".env").write_text("PROJECT=acme-odoo-production\n")
    containers = [
        {"id": "a" * 64, "running": "false", "started": "2027-01-15T08:00:00Z", "oneoff": "False",
         "name": "/acme-odoo-production-odoo-1"},
        {"id": "b" * 64, "running": "true", "started": "2027-01-15T08:05:00Z", "oneoff": "True",
         "name": "/acme-odoo-production-odoo-run-1f2e"},
        {"id": "c" * 64, "running": "true", "started": "2027-01-15T08:06:00Z", "oneoff": "True",
         "name": "/acme-odoo-production-next"},
    ]

    def docker(*args):
        if args[0] == "ps":
            wanted = [a.rsplit("=", 1)[1] for a in args if a.startswith("label=com.docker.compose.oneoff=")]
            ids = [c["id"] for c in containers if not wanted or c["oneoff"] == wanted[0]]
            return type("P", (), {"returncode": 0, "stdout": "\n".join(ids)})()
        lines = []
        for c in containers:
            if c["id"] in args:
                row = [c["id"], c["running"], c["started"], f"/var/lib/docker/containers/{c['id']}/{c['id']}-json.log"]
                if "oneoff" in args[2]:
                    row += [c["oneoff"], c["name"]]
                lines.append("|".join(row))
        return type("P", (), {"returncode": 0, "stdout": "\n".join(lines)})()
    monkeypatch.setattr(composehelper, "_docker", docker)
    found = composehelper.log_container(project, "odoo")
    assert found is not None and found[0] == "a" * 64
    # the preview alone, its labels said wrongly: still not the instance's
    containers[0]["oneoff"] = "True"
    containers[2]["oneoff"] = "False"
    assert composehelper.log_container(project, "odoo") is None


async def test_a_catch_up_of_hours_keeps_its_spans_whole(tmp_path, monkeypatch):
    """After a stop of more than 2 hours with more than CATCH_UP to read, a span read in two rounds is written
    whole (its first part kept until the reading has passed it), and its hour's pages once the reading is past it."""
    history = InstanceHistory(tmp_path / "monitor")
    reader = instancelog.InstanceLog(settings=None, history=history)
    log = tmp_path / "odoo.log"
    log.write_text("")
    inst = _Inst(log)
    rounds = [([access(NOW + 10 + i) for i in range(30)], True), ([access(NOW + 100 + i) for i in range(20)], True),
              ([access(NOW + 7200 + 10)], False)]

    def read_file(path, cursor, most=instancelog.CATCH_UP):
        lines, more = rounds.pop(0)
        return lines, {"inode": 1, "pos": 1}, None, more
    monkeypatch.setattr(instancelog, "read_file", read_file)
    history.folder(inst.key).mkdir(parents=True)
    reader.save_cursor(inst.key, {"inode": 1, "pos": 0})
    later = NOW + 5 * 3600
    await reader.read(inst, now=later)
    await reader.read(inst, now=later + 300)
    spans = {p["t"]: p["rq"] for p in history.points(inst.key, 0, "log")}
    assert spans[NOW] == 50
    assert history.points(inst.key, 0, "pages") == []  # the reading is still in that hour
    await reader.read(inst, now=later + 600)
    pages = history.points(inst.key, 0, "pages")
    assert pages[0]["t"] == NOW and pages[0]["p"]["/web/dataset/call_kw"][0] == 50


def _ready(tmp_path):
    mon, settings = _monitor(tmp_path)
    mon.settings, mon.causes, mon.path, mon.instances, mon.machine = settings, {}, tmp_path / "monitor.json", {}, {}
    return mon


async def test_an_instance_measured_no_more_leaves_its_causes(tmp_path, monkeypatch):
    """A deleted, switched off or no longer measured instance gets no round: it leaves every cause at once, and is
    never named again in the message of the others."""
    sent = []

    async def send(options, recipients, subject, text, event=None, language=""):
        sent.append(text)
        return []
    monkeypatch.setattr(notify, "send", send)
    mon = _ready(tmp_path)
    slow = {"kind": "slow", "median": 1.4, "p95": 6.0, "requests": 300}
    await mon.instance_causes({"gone/production": [slow]}, {"gone/production", "b/production"})
    sent.clear()
    await mon.instance_causes({"b/production": [slow]}, {"b/production"})
    assert "gone/production" not in mon.causes.get("slow", {})
    assert sent and not any("gone/production" in t for t in sent)


async def test_a_cause_told_is_kept_at_once(tmp_path, monkeypatch):
    """After a crash, a cause already told is not told again: monitor.json is written when one enters or leaves."""
    async def send(*a, **k):
        return []
    monkeypatch.setattr(notify, "send", send)
    mon = _ready(tmp_path)
    await mon.instance_causes({"a/production": [{"kind": "errors", "percent": 3.0, "failed": 9, "requests": 300}]},
                              {"a/production"})
    saved = json.loads((tmp_path / "monitor.json").read_text())
    assert "a/production" in saved["causes"]["errors"]


@pytest.mark.parametrize("target, kept", [
    ("/web/content/ir.attachment/123/datas/Fattura_Mario_Rossi.pdf", "/web/content/ir.attachment/<n>/<f>"),
    ("/web/content/123/Fattura%20Mario%20Rossi.pdf?download=true", "/web/content/<n>/<f>"),
    ("/web/image/product.template/7/image_1024/Sedia%20Rossi", "/web/image/product.template/<n>/image_<n>/<f>"),
    ("/web/image/42/Sedia%20Rossi.png", "/web/image/<n>/<f>"),
    ("/web/image/res.partner/3/avatar_128", "/web/image/res.partner/<n>/avatar_<n>"),
    ("/partners/mario-rossi-23", "/partners/<x>-<n>"),
    ("/shop/sedia-rossa-da-ufficio-7", "/shop/<x>-<n>"),
    ("/web/signup/abcdefghijklmnopqrstuv", "/web/signup/<x>"),
    ("/my/orders/12", "/my/orders/<n>"),
    ("/json/2/sale.order/action_confirm", "/json/<n>/sale.order/action_confirm"),
])
def test_a_pages_path_keeps_no_text_of_the_customer(target, kept):
    found = odoolog.normalised(target)
    assert found == kept
    for word in ("Mario", "Rossi", "Fattura", "Sedia", "abcdefghijklmnop", "mario", "rossi", "sedia"):
        assert word not in found


def test_health_checks_and_the_monitors_own_check_are_no_requests():
    """A balancer's /web/health and the monitor's own check of /web/login are no person's request: not counted, not
    timed, not a page, never «slow» nor «failed»; Odoo's WARNING «called ignoring args» of a route given an
    argument it does not take (lab3's 7.401: a burst of /web/health with a query string) is not counted."""
    counter = odoolog.Counter()
    for line in [
        access(NOW + 1, path="/web/health"), access(NOW + 2, path="/web/health?pad=ppp", status=500),
        access(NOW + 3, path=f"/web/login?{monitor_module.PROBE}=1"),
        record(NOW + 3, "WARNING", "odoo.http",
               "<function odoo.addons.web.controllers.home.health> called ignoring args {'pad'} "),
        access(NOW + 4, path="/web/login"),
    ]:
        counter.line(line)
    t = counter.spans[NOW]
    assert (t["rq"], t["rf"], t["tm"], t["w"]) == (1, 0, 1, 0)
    assert set(counter.hours[NOW]) == {"/web/login"}
    assert monitor_module.PROBE == odoolog.PROBE
    assert "/web/login?{PROBE}=1" in Path(monitor_module.__file__).read_text(encoding="utf-8")


async def test_a_read_given_up_ends_its_helper(monkeypatch):
    """A job's pre-read past its time, or OmniaCLI stopping: the helper's process does not stay behind, and a
    docker that hangs holds a job's locks 15 s at most."""
    started = []
    real = asyncio.create_subprocess_exec

    async def spawn(*cmd, **kwargs):
        proc = await real("sleep", "30", **kwargs)
        started.append(proc)
        return proc
    monkeypatch.setattr(instancelog.asyncio, "create_subprocess_exec", spawn)
    inst = _Inst(None, kind="compose")
    inst.plugin = type("P", (), {"log_service": lambda self, i: "odoo"})()
    inst.restart.command = lambda *a: ["true"]
    task = asyncio.create_task(instancelog.helper_read(inst, {}))
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert started and started[0].returncode is not None
    assert instancelog.BEGIN_READ <= 15
