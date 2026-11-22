# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Alerts and the automatic restart: the thresholds of the machine in
Options, a level on each notice and each thing to look at, an instance that
does not answer restarted (on, at most once in a while, never during
another job nor when stopped), the webhooks signed and told the jobs that
fail."""

import asyncio
import hashlib
import hmac
import json
import time

import httpx
import pytest

from omniacli import monitor as monitor_module
from omniacli import notify, ops
from omniacli.config import ConfigError, Options, Workspace, load_options, parse_options, save_options
from omniacli.jobs import JobManager

from .test_app import AUTH, POST, client, wait_job  # noqa: F401 - the client fixture

SECRET = "s" * 40


def test_the_options_of_monitoring(tmp_path):
    options = parse_options({"monitor": {
        "notify": ["https://hooks.example.com/a"], "language": "it",
        "thresholds": {"cpu": {"warning": 70, "critical": 80}, "minutes": 3},
        "restart": {"enabled": True, "after": 4, "every": 15},
        "webhooks": {"https://hooks.example.com/a": {"secret": SECRET, "jobs": True}}}})
    assert options.monitor_thresholds["cpu"] == [70, 80] and options.monitor_thresholds["ram"] == [85, 95]
    assert (options.monitor_minutes, options.restart_down, options.restart_after, options.restart_every) == (3, True, 4, 15)
    assert options.notice_language == "it" and options.webhook_keys["https://hooks.example.com/a"]["jobs"]
    save_options(tmp_path / "o.yaml", options)
    assert load_options(tmp_path / "o.yaml") == options
    # the defaults: nothing written
    save_options(tmp_path / "d.yaml", Options())
    assert "monitor" not in (tmp_path / "d.yaml").read_text()
    for bad, message in (({"thresholds": {"cpu": {"warning": 90, "critical": 80}}}, "above warning"),
                         ({"thresholds": {"ram": {"warning": 0}}}, "whole number"),
                         ({"restart": {"after": 1}}, "whole number"), ({"language": "fr"}, "one of"),
                         ({"webhooks": {"http://x": {}}}, "https"), ({"webhooks": {"https://x.example/h": {"secret": "short"}}}, "secret")):
        with pytest.raises(ConfigError, match=message):
            parse_options({"monitor": bad})


async def test_a_signed_webhook_and_an_email_in_its_language(monkeypatch):
    posted = []

    def handler(request):
        posted.append((str(request.url), dict(request.headers), request.content))
        return httpx.Response(200)
    real = httpx.AsyncClient
    monkeypatch.setattr(notify.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    mails = []
    monkeypatch.setattr(notify, "_send_mail", lambda options, to, subject, text: mails.append((subject, text)))
    options = Options(smtp_host="smtp.acme.it", notice_language="it",
                      webhook_keys={"https://signed.example.com/h": {"secret": SECRET, "jobs": False}})
    problems = await notify.send(options, ["https://signed.example.com/h", "https://plain.example.com/h", "ops@acme.it"],
                                 "OmniaCLI m1: [critical] srv/test is down", "srv/test does not answer",
                                 event={"event": "instance.down", "level": "critical", "instance": "srv/test"})
    assert problems == []
    (_, signed, body), (_, plain, _) = posted
    data = json.loads(body)
    assert (data["event"], data["level"], data["instance"]) == ("instance.down", "critical", "srv/test")
    assert signed["x-omniacli-event"] == "instance.down"
    assert signed["x-omniacli-signature-256"] == "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert "x-omniacli-signature-256" not in plain
    # the email in Italian, the webhook in English
    assert mails[0][0] == "OmniaCLI m1: [critico] srv/test è giù" and "is down" in data["subject"]


class _Sampler:
    def __init__(self):
        self.machine = []

    def fill(self, cpu, ram=10, minutes=6):
        now = int(time.time())
        self.machine = [{"t": now - s, "cpu": cpu, "ram": ram, "ram_total": 100} for s in range(minutes * 60, -1, -5)]


@pytest.fixture
def watched(env, tmp_path, monkeypatch):
    settings = env["settings"]
    settings.options = Options(monitor_notify=["ops@acme.it"], monitor_minutes=5)
    states = {"srv/test": "running", "srv/production": "running"}

    async def run_state(inst):
        return states[inst.key]
    told = []

    async def send(options, recipients, subject, text, event=None, language=""):
        told.append((subject, event))
        return []
    monkeypatch.setattr(monitor_module.notify, "send", send)
    monkeypatch.setattr(type(settings.servers["srv"].test.plugin), "web_port", lambda self, inst: 8069)
    jobs = JobManager(tmp_path / "jobs-here")
    sampler = _Sampler()
    audit = []
    mon = monitor_module.Monitor(settings, jobs, run_state, tmp_path / "monitor.json", sampler=sampler,
                                 audit=audit.append, workspace_of=settings.workspace_of)
    return {"monitor": mon, "states": states, "told": told, "settings": settings, "sampler": sampler,
            "jobs": jobs, "audit": audit}


async def test_the_load_above_its_thresholds(watched):
    mon, told, sampler = watched["monitor"], watched["told"], watched["sampler"]
    sampler.fill(cpu=50)
    await mon.check_load()
    assert told == [] and not mon.alerts()
    # only a part of the window above: nothing yet
    sampler.fill(cpu=95)
    sampler.machine[-10]["cpu"] = 40
    await mon.check_load()
    assert told == []
    sampler.fill(cpu=95)
    await mon.check_load()
    [(subject, event)] = told
    assert "[warning] CPU of the machine at 95% for 5 min" in subject
    assert (event["event"], event["level"], event["metric"], event["value"]) == ("machine.threshold", "warning", "cpu", 95)
    await mon.check_load()
    assert len(told) == 1
    sampler.fill(cpu=99)
    await mon.check_load()
    assert told[-1][1]["level"] == "critical" and mon.alerts()[0]["level"] == "critical"
    # the memory: its own threshold
    sampler.fill(cpu=10, ram=96)
    await mon.check_load()
    assert told[-2][1]["event"] == "machine.threshold.cleared" and told[-1][1]["metric"] == "ram"
    assert [a["what"] for a in mon.alerts()] == ["load ram"]
    # too few points (the sampler just started): not read
    sampler.machine = sampler.machine[-3:]
    await mon.check_load()
    assert [a["what"] for a in mon.alerts()] == ["load ram"]


async def test_a_disk_warning_then_critical(watched, monkeypatch):
    mon, told = watched["monitor"], watched["told"]
    usage = type("U", (), {"total": 100 << 30, "free": 8 << 30})
    monkeypatch.setattr(monitor_module.shutil, "disk_usage", lambda path: usage)
    await mon.check_disks()
    assert told and all(e["level"] == "warning" and e["metric"] == "disk" for _, e in told)
    first = len(told)
    usage.free = 2 << 30
    await mon.check_disks()
    assert len(told) == 2 * first and told[-1][1]["level"] == "critical"
    await mon.check_disks()
    assert len(told) == 2 * first


async def _checks(mon, inst, n, answer=False):
    def handler(request):
        if not answer:
            raise httpx.ConnectError("refused")
        return httpx.Response(200)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        for _ in range(n):
            await mon.check_instance(client, inst)


async def test_an_instance_that_does_not_answer_is_restarted(watched, monkeypatch):
    mon, told, jobs, settings = watched["monitor"], watched["told"], watched["jobs"], watched["settings"]
    inst = settings.servers["srv"].test
    restarted = []

    async def restart(job, instance):
        restarted.append(instance.key)
    monkeypatch.setattr(monitor_module.ops, "restart", restart)

    async def probe(client, instance):
        return False, None, "ConnectError"
    monkeypatch.setattr(mon, "probe", probe)
    # off: told, not restarted
    await _checks(mon, inst, 6)
    assert [e["event"] for _, e in told] == ["instance.down"] and not jobs.jobs
    # on: after 3 checks in a row, one job of the system that says why
    settings.options = Options(monitor_notify=["ops@acme.it"], restart_down=True, restart_after=8, restart_every=30)
    await _checks(mon, inst, 2)
    [job] = jobs.jobs.values()
    while not job.finished_flag:
        await asyncio.sleep(0.01)
    assert job.status == "success" and restarted == ["srv/test"] and job.source.startswith("system: monitor")
    assert "no answer for 8 checks in a row" in job.source and watched["audit"]
    assert told[-1][1]["event"] == "instance.restarted" and mon.view(inst.key)["restart_job"] == job.id
    # still down: not again within 30 minutes, told once
    await _checks(mon, inst, 20)
    assert len(jobs.jobs) == 1 and [e["event"] for _, e in told].count("instance.restart_held") == 1
    # later: once more
    mon.instances[inst.key]["restart_at"] -= 31 * 60
    await _checks(mon, inst, 8)
    assert len(jobs.jobs) == 2


async def test_no_restart_during_a_job_nor_when_stopped(watched, monkeypatch):
    mon, jobs, settings, states = watched["monitor"], watched["jobs"], watched["settings"], watched["states"]
    settings.options = Options(restart_down=True, restart_after=3)
    inst = settings.servers["srv"].test
    await _checks(mon, inst, 2)
    # another job holds the instance: the monitor does not even count
    lock = jobs.lock(inst.key)
    await lock.acquire()
    await _checks(mon, inst, 10)
    lock.release()
    assert not jobs.jobs
    # stopped on purpose (a staging stopped too): stopped, not down, not restarted
    states["srv/test"] = "stopped"
    await _checks(mon, inst, 10)
    assert mon.view(inst.key)["state"] == "stopped" and not jobs.jobs


async def test_the_webhooks_of_options_and_the_jobs_that_fail(client, env, monkeypatch):
    settings = env["settings"]
    url = "https://hooks.example.com/ops"
    body = {"shell": {"font_size": 14}, "tunnel": {"idle_hours": 4, "bind": "127.0.0.1", "ports": "18000-18099"},
            "monitor": {"notify": [url], "thresholds": {"cpu": {"warning": 50, "critical": 60}},
                        "restart": {"enabled": True, "after": 3, "every": 10}}}
    assert (await client.post("/api/options/save", headers=POST, json=body)).status_code == 200
    assert settings.options.restart_down and settings.options.monitor_thresholds["cpu"] == [50, 60]
    page = (await client.get("/options", headers={"Authorization": AUTH})).text
    assert 'data-url="https://hooks.example.com/ops"' in page
    secret = (await client.post("/api/options/webhooks/secret", headers=POST, json={"url": url})).json()["secret"]
    assert secret not in (await client.get("/options", headers={"Authorization": AUTH})).text
    assert settings.options.webhook_keys[url]["secret"] == secret
    # the Options page saved again keeps the secret
    assert (await client.post("/api/options/save", headers=POST, json=body)).status_code == 200
    assert settings.options.webhook_keys[url]["secret"] == secret
    assert (await client.post("/api/options/webhooks/secret", headers=POST,
                              json={"url": "https://other.example.com"})).status_code == 404
    sent = []

    async def send(options, recipients, subject, text, event=None, language=""):
        sent.append((recipients, subject, event, options.webhook_keys.get(recipients[0])))
        return []
    monkeypatch.setattr(notify, "send", send)
    resp = await client.post("/api/options/webhooks/test", headers=POST, json={"url": url})
    assert resp.json()["ok"] and sent[-1][2]["event"] == "notice.test" and sent[-1][3]["secret"] == secret
    # a job that fails: told only once the webhook asks for it
    jobs = client.app.state.jobs

    async def fails(job):
        raise ops.JobFailed("it broke")
    done = jobs.submit("Something on srv/test", ["srv/test"], "manual: admin", fails)
    await wait_job(client, done.id)
    await asyncio.sleep(0.05)
    assert [s for s in sent if s[2]["event"].startswith("job.")] == []
    assert (await client.post("/api/options/webhooks/save", headers=POST, json={"url": url, "jobs": True})).status_code == 200
    done = jobs.submit("Something on srv/test", ["srv/test"], "manual: admin", fails)
    await wait_job(client, done.id)
    for _ in range(50):
        if any(s[2]["event"] == "job.failed" for s in sent):
            break
        await asyncio.sleep(0.02)
    event = next(s[2] for s in sent if s[2]["event"] == "job.failed")
    assert (event["level"], event["job"], event["instances"]) == ("critical", done.id, ["srv/test"])
    # unsigned, the secret goes; a workspace's webhook is listed too
    await client.post("/api/options/webhooks/save", headers=POST, json={"url": url, "unsigned": True})
    assert not settings.options.webhook_keys[url]["secret"] and settings.options.webhook_keys[url]["jobs"]
    settings.workspaces["acme"] = Workspace("acme", servers=["srv"], notify=["https://acme.example.com/h"])
    assert 'data-url="https://acme.example.com/h"' in (await client.get("/options", headers={"Authorization": AUTH})).text


async def test_a_level_on_each_thing_to_look_at(client, env):
    mon = client.app.state.monitor
    mon.machine["load cpu"] = {"problem": "CPU of the machine at 99% for 5 min (threshold 98%)", "level": "critical",
                               "since": "2026-11-22T10:00:00"}
    mon.machine["disk backups"] = {"problem": "/b: 5 GB free of 100 (95% in use)", "level": "warning"}
    data = (await client.get("/api/map?part=all", headers={"Authorization": AUTH})).json()
    watch = data["verdict"]["watch"]
    assert watch[0]["kind"] == "load" and watch[0]["level"] == "critical"
    assert all(w["level"] in ("warning", "critical") for w in watch)
    assert any(w["kind"] == "disk" and w["level"] == "warning" for w in watch)
