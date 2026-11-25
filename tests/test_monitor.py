# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Monitoring with notices: an instance down after 3 failed checks while its
service runs, told when it goes and when it comes back; one stopped on
purpose is not down; a full disk told once a day; who is told."""

import httpx
import pytest

from omniacli import monitor as monitor_module
from omniacli.config import ConfigError, Options, Workspace, parse_notify

from .test_app import AUTH, POST, client, wait_job  # noqa: F401 - the client fixture
from .test_workspaces import post_as


class _Jobs:
    def is_busy(self, key):
        return False


@pytest.fixture
def watched(env, tmp_path, monkeypatch):
    settings = env["settings"]
    settings.options = Options(monitor_notify=["ops@acme.it"])
    settings.workspaces["acme"] = Workspace("acme", servers=["srv"], notify=["https://hooks.example.com/x"])
    states = {"srv/test": "running", "srv/production": "running"}

    async def run_state(inst):
        return states[inst.key]
    told = []

    async def send(options, recipients, subject, text, event=None, language=""):
        told.append((tuple(recipients), subject))
        return []
    monkeypatch.setattr(monitor_module.notify, "send", send)
    monkeypatch.setattr(type(settings.servers["srv"].test.plugin), "web_port", lambda self, inst: 8069)
    mon = monitor_module.Monitor(settings, _Jobs(), run_state, tmp_path / "monitor.json")
    return {"monitor": mon, "states": states, "told": told, "settings": settings}


async def test_down_and_up_again(watched):
    mon, told = watched["monitor"], watched["told"]
    inst = watched["settings"].servers["srv"].test
    answers = {"ok": True}

    def handler(request):
        if not answers["ok"]:
            raise httpx.ConnectError("refused")
        return httpx.Response(200)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await mon.check_instance(client, inst)
        assert mon.view(inst.key)["state"] == "up" and mon.view(inst.key)["uptime_24h"] == 100.0
        answers["ok"] = False
        for _ in range(2):
            await mon.check_instance(client, inst)
        assert mon.view(inst.key)["state"] == "up" and told == []
        await mon.check_instance(client, inst)
        assert mon.view(inst.key)["state"] == "down"
        # the machine's and the workspace's told, once
        assert told == [(("ops@acme.it", "https://hooks.example.com/x"), "OmniaCLI " + told[0][1].split(": ", 1)[0]
                         .removeprefix("OmniaCLI ") + ": [critical] srv/test is down")]
        await mon.check_instance(client, inst)
        assert len(told) == 1
        answers["ok"] = True
        await mon.check_instance(client, inst)
        assert mon.view(inst.key)["state"] == "up" and told[-1][1].endswith("srv/test is up again")
        assert mon.view(inst.key)["uptime_24h"] == pytest.approx(100 * 2 / 6, abs=0.01)
    # kept across a restart of OmniaCLI
    mon.save()
    again = monitor_module.Monitor(watched["settings"], _Jobs(), None, mon.path)
    assert again.view(inst.key)["checks_24h"] == 6


async def test_stopped_on_purpose_is_not_down(watched):
    mon, told = watched["monitor"], watched["told"]
    inst = watched["settings"].servers["srv"].production
    watched["states"]["srv/production"] = "stopped"
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(502))) as client:
        for _ in range(4):
            await mon.check_instance(client, inst)
    assert mon.view(inst.key)["state"] == "stopped" and told == []


async def test_a_full_disk_told_once_a_day(watched, monkeypatch):
    mon, told = watched["monitor"], watched["told"]
    usage = type("U", (), {"total": 100 << 30, "free": 5 << 30, "used": 95 << 30})
    monkeypatch.setattr(monitor_module.shutil, "disk_usage", lambda path: usage)
    await mon.check_disks()
    first = len(told)
    await mon.check_disks()
    # each disk once (the backups', each storage's), not again the same day
    assert first >= 1 and len(told) == first and all("5 GB free of 100" in t[1] for t in told) and mon.problems()
    usage.free = 50 << 30
    await mon.check_disks()
    assert not mon.problems()


def test_whom_to_tell():
    assert parse_notify("a@b.it, https://x.example/h", "n") == ["a@b.it", "https://x.example/h"]
    with pytest.raises(ConfigError, match="neither"):
        parse_notify(["http://plain"], "n")
    assert monitor_module.certificate_days("nothing.invalid", port=1) is None


async def test_the_pages(client, env):
    data = (await client.get("/api/servers/srv/test/uptime", headers={"Authorization": AUTH})).json()
    assert data["state"] == "unknown"
    env["settings"].workspaces["acme"] = Workspace("acme", servers=["srv"], members={"dev": "admin"})
    # a list (a text, which a page never sends, is refused: AD-017 slice 5, the review)
    resp = await client.post("/api/workspaces/acme/notify", headers=POST, json={"notify": ["ops@acme.it"]})
    assert resp.status_code == 200 and env["settings"].workspaces["acme"].notify == ["ops@acme.it"]
    assert (await client.post("/api/workspaces/acme/notify", headers=POST, json={"notify": ["nope"]})).status_code == 400
    assert (await client.post("/api/workspaces/acme/notify", headers=POST, json={"notify": "ops@acme.it"})).status_code == 400
    body = {"shell": {"font_size": 14}, "tunnel": {"idle_hours": 4, "bind": "127.0.0.1", "ports": "18000-18099"},
            "monitor": {"notify": ["ops@acme.it"]}}
    assert (await client.post("/api/options/save", headers=POST, json=body)).status_code == 200
    assert env["settings"].options.monitor_notify == ["ops@acme.it"]
    # saved again without it: kept
    del body["monitor"]
    assert (await client.post("/api/options/save", headers=POST, json=body)).status_code == 200
    assert env["settings"].options.monitor_notify == ["ops@acme.it"]
