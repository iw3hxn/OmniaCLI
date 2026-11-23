# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The automatic security updates of the system (README; osupdates.py):
unattended-upgrades as APT reads its configuration, switched by the root
helper with the restart, the last run from its log; a later file of APT
that decides otherwise named; provision.sh leaves the switch as it is; the
routes for the machine's admins only, each change in the access log. The
machine is a folder of the test: nothing runs as root here."""

import base64
import json
import os
import stat
import subprocess
from pathlib import Path

import httpx
import pytest

from omniacli import osupdates, workspacehelper, workspaceops
from omniacli.app import create_app
from omniacli.auth import hash_password
from omniacli.config import Access, Workspace, parse_settings
from omniacli.jobs import JobFailed

from .conftest import PASSWORD, USER_HASH, BasicAsSession, hash_token

HOST = "lab3.test"
ADMIN = {"Authorization": "Basic " + base64.b64encode(f"admin:{PASSWORD}".encode()).decode(), "Origin": f"http://{HOST}"}
MARIO = {"Authorization": "Basic " + base64.b64encode(f"mario:{PASSWORD}".encode()).decode(), "Origin": f"http://{HOST}"}
CENTRAL_TOKEN = "central-" + "c" * 40
ALONE = {"Authorization": f"Bearer {CENTRAL_TOKEN}", "X-OmniaCLI-Prefix": "/links/lab3", "X-OmniaCLI-User": "carlo",
         "X-OmniaCLI-Role-Cap": "admin"}
PERSON = {**ALONE, "X-OmniaCLI-Menu": "servers,instances,backups,settings"}
PROVISION = Path(osupdates.__file__).with_name("provision.sh")

UPGRADED = """2026-11-22 06:14:02,101 INFO Starting unattended upgrades script
2026-11-22 06:14:02,102 INFO Allowed origins are: o=Ubuntu,a=noble-security
2026-11-22 06:14:09,500 INFO Packages that will be upgraded: libssl3t64 openssl
2026-11-22 06:14:09,501 INFO Writing dpkg log to /var/log/unattended-upgrades/unattended-upgrades-dpkg.log
2026-11-22 06:14:40,900 INFO All upgrades installed
"""
NOTHING = """2026-11-23 06:20:01,000 INFO Starting unattended upgrades script
2026-11-23 06:20:01,001 INFO Allowed origins are: o=Ubuntu,a=noble-security
2026-11-23 06:20:05,000 INFO No packages found that can be upgraded unattended and no pending auto-removals
"""
FAILED = """2026-11-23 06:20:01,000 INFO Starting unattended upgrades script
2026-11-23 06:20:03,000 ERROR Lock could not be acquired (another package manager running?)
"""


@pytest.fixture
def machine(tmp_path):
    """/etc/apt/apt.conf.d as the package leaves it, the log, the program, the restart flag: in a folder."""
    conf = tmp_path / "apt.conf.d"
    conf.mkdir()
    (conf / "20auto-upgrades").write_text('APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n')
    (conf / "50unattended-upgrades").write_text(
        '// Automatically reboot *WITHOUT CONFIRMATION* if\n//Unattended-Upgrade::Automatic-Reboot "false";\n'
        'Unattended-Upgrade::Allowed-Origins {\n        "${distro_id}:${distro_codename}-security";\n};\n'
        '/* a block\nUnattended-Upgrade::Automatic-Reboot "true";\n*/\n')
    (conf / "99custom.dpkg-old").write_text('APT::Periodic::Unattended-Upgrade "0";\n')
    log = tmp_path / "log" / "unattended-upgrades.log"
    log.parent.mkdir()
    log.write_text(UPGRADED)
    program = tmp_path / "unattended-upgrade"
    program.write_text("#!/bin/sh\n")
    flag = tmp_path / "reboot-required"
    return {"folder": conf, "log": log, "program": program, "reboot_flag": flag}


def test_read_as_apt_reads_it(machine):
    found = osupdates.state(**machine)
    assert (found["installed"], found["enabled"], found["lists"], found["reboot"]) == (True, True, True, "never")
    # the commented settings, a block comment and a file APT does not read are not read
    assert found["source"] == "20auto-upgrades" and found["managed"] is False and found["reboot_required"] is False
    assert found["last"]["outcome"] == "upgraded" and found["last"]["packages"] == ["libssl3t64", "openssl"]
    assert found["last"]["at"] == "2026-11-22T06:14:02" and found["last"]["ended"] == "2026-11-22T06:14:40"
    # a restart waiting, and for which packages
    machine["reboot_flag"].write_text("*** System restart required ***\n")
    machine["reboot_flag"].with_name("reboot-required.pkgs").write_text("linux-image-6.8.0-49-generic\n")
    found = osupdates.state(**machine)
    assert found["reboot_required"] and found["reboot_packages"] == ["linux-image-6.8.0-49-generic"]
    # not installed: off whatever the files say
    machine["program"].unlink()
    assert osupdates.state(**machine)["enabled"] is False


def test_the_last_run():
    assert osupdates.last_run("") is None
    assert osupdates.last_run(UPGRADED + NOTHING)["outcome"] == "nothing"
    failed = osupdates.last_run(UPGRADED + FAILED)
    assert failed["outcome"] == "error" and failed["error"].startswith("Lock could not be acquired")
    running = osupdates.last_run("2026-11-23 06:20:01,000 INFO Starting unattended upgrades script\n")
    assert running["outcome"] == "running" and running["lines"] == ["2026-11-23 06:20:01 INFO Starting unattended upgrades script"]


def test_switched_and_its_restart(machine):
    where = {k: v for k, v in machine.items() if k != "folder"}
    found = osupdates.apply(False, "never", machine["folder"], **where)
    assert found["enabled"] is False and found["managed"] is True and found["reboot"] == "never"
    switch = machine["folder"] / "20auto-upgrades"
    assert switch.read_text().startswith(osupdates.MARK) and 'Unattended-Upgrade "0"' in switch.read_text()
    assert stat.S_IMODE(switch.stat().st_mode) == 0o644
    found = osupdates.apply(True, "03:30", machine["folder"], **where)
    assert (found["enabled"], found["reboot"]) == (True, "03:30")
    reboot = (machine["folder"] / osupdates.REBOOT_FILE).read_text()
    assert 'Automatic-Reboot "true"' in reboot and 'Automatic-Reboot-Time "03:30"' in reboot
    # a later file decides: named, not hidden
    (machine["folder"] / "99custom").write_text('APT::Periodic::Unattended-Upgrade "0";\n')
    with pytest.raises(osupdates.UpdatesRefused, match="99custom sets APT::Periodic::Unattended-Upgrade"):
        osupdates.apply(True, "03:30", machine["folder"], **where)
    (machine["folder"] / "99custom").unlink()
    # a link is not followed; a time that is not one; switched on without the program
    target = machine["folder"].parent / "elsewhere"
    target.write_text("")
    switch.unlink()
    switch.symlink_to(target)
    with pytest.raises(osupdates.UpdatesRefused, match="is a link"):
        osupdates.apply(True, "never", machine["folder"], **where)
    for bad in ("25:00", "3", "tonight", ""):
        with pytest.raises(osupdates.UpdatesRefused):
            osupdates.check_policy(bad)
    switch.unlink()
    machine["program"].unlink()
    with pytest.raises(osupdates.UpdatesRefused, match="not installed"):
        osupdates.apply(True, "never", machine["folder"], **where)
    assert osupdates.apply(False, "never", machine["folder"], **where)["enabled"] is False


def test_the_root_helpers_actions(monkeypatch):
    calls = []
    monkeypatch.setattr(osupdates, "apply", lambda on, policy: calls.append((on, policy)) or {"enabled": on})
    assert workspacehelper.os_updates_set("on", "04:00") == {"enabled": True} and calls == [(True, "04:00")]
    with pytest.raises(workspacehelper.HelperError):
        workspacehelper.os_updates_set("maybe", "never")

    def refused(on, policy):
        raise osupdates.UpdatesRefused("no")
    monkeypatch.setattr(osupdates, "apply", refused)
    with pytest.raises(workspacehelper.HelperError, match="no"):
        workspacehelper.os_updates_set("off", "never")
    assert workspacehelper.ACTIONS["os-updates"][1] == 0 and workspacehelper.ACTIONS["os-updates-set"][1] == 2


def test_provisioning_again_leaves_the_switch_as_it_is(tmp_path):
    """The lines of provision.sh that write 20auto-upgrades, run on a file of the test."""
    text = PROVISION.read_text()
    start = text.index("        if ! head -n 1 /etc/apt/apt.conf.d/20auto-upgrades")
    block = text[start:text.index("        fi\n", start) + len("        fi\n")]
    target = tmp_path / "20auto-upgrades"
    script = block.replace("/etc/apt/apt.conf.d/20auto-upgrades", str(target))
    # a new machine: on
    subprocess.run(["bash", "-c", script], check=True)
    assert 'Unattended-Upgrade "1"' in target.read_text()
    # switched off on Options: left as it is
    target.write_text(osupdates.switch_text(False))
    subprocess.run(["bash", "-c", script], check=True)
    assert target.read_text() == osupdates.switch_text(False)


@pytest.fixture
async def panel(tmp_path, monkeypatch, machine):
    """A machine (admin) with a workspace acme (mario its admin) and a panel that reaches it through a
    link as an admin; the root helper faked with osupdates' own functions on the test's folder."""
    conf = tmp_path / "lab3"
    conf.mkdir()
    settings = parse_settings({"auth": {"users": {"admin": USER_HASH}, "links": {
        "central": {"token": hash_token(CENTRAL_TOKEN), "role": "admin"}}},
        "backup_dir": str(conf / "backups"), "jobs_dir": str(conf / "jobs"), "servers_file": "servers.yaml"},
        base_dir=conf)
    settings.set_web_access(Access(users={"mario": hash_password(PASSWORD, iterations=1000)}, roles={"mario": "workspace"}))
    settings.workspaces = {"acme": Workspace("acme", "ACME", members={"mario": "admin"})}
    calls = []
    where = {k: v for k, v in machine.items() if k != "folder"}

    async def ask(*args, timeout=None, input=None):
        calls.append(args)
        try:
            if args[0] == "os-updates":
                return osupdates.state(**machine)
            if args[0] == "os-updates-set":
                return osupdates.apply(args[1] == "on", args[2], machine["folder"], **where)
        except osupdates.UpdatesRefused as exc:
            raise JobFailed(f"workspace-helper: {exc}") from None
        raise AssertionError(args)
    monkeypatch.setattr(workspaceops, "ask", ask)
    app = create_app(settings)
    transport = httpx.ASGITransport(app=BasicAsSession(app))
    async with httpx.AsyncClient(transport=transport, base_url=f"http://{HOST}") as client:
        client.calls, client.log, client.machine = calls, conf / "logs" / "access.jsonl", machine
        yield client


async def test_an_admin_switches_it(panel):
    client = panel
    data = (await client.get("/api/machine/os-updates", headers=ADMIN)).json()
    assert data["enabled"] is True and data["last"]["outcome"] == "upgraded"
    resp = await client.post("/api/machine/os-updates", headers=ADMIN, json={"enabled": False, "reboot": "02:15"})
    assert resp.status_code == 200, resp.text
    assert (resp.json()["enabled"], resp.json()["reboot"], resp.json()["managed"]) == (False, "02:15", True)
    assert client.calls[-1] == ("os-updates-set", "off", "02:15")
    # refused before the helper: a time that is not one, a switch that is not true or false
    calls = len(client.calls)
    for bad in ({"enabled": True, "reboot": "26:00"}, {"enabled": "yes", "reboot": "never"}):
        assert (await client.post("/api/machine/os-updates", headers=ADMIN, json=bad)).status_code == 400
    assert len(client.calls) == calls
    # the helper's refusal, in Italian too
    (client.machine["folder"] / "99zz").write_text('APT::Periodic::Unattended-Upgrade "0";\n')
    resp = await client.post("/api/machine/os-updates", headers={**ADMIN, "Accept-Language": "it"},
                             json={"enabled": True, "reboot": "never"})
    assert resp.status_code == 400 and "imposta APT::Periodic::Unattended-Upgrade dopo 20auto-upgrades" in resp.json()["error"]
    # NIS 2: who, from what to what
    said = [json.loads(line)["detail"] for line in client.log.read_text().splitlines()
            if "automatic security updates" in line]
    assert said == ["automatic security updates of the system: on, restart never -> off, restart 02:15"]


async def test_who_may(panel, monkeypatch):
    client = panel
    # a person through a link may; the machine alone, a customer may not
    resp = await client.post("/api/machine/os-updates", headers=PERSON, json={"enabled": True, "reboot": "never"})
    assert resp.status_code == 200, resp.text
    assert "for carlo" in [json.loads(line)["detail"] for line in client.log.read_text().splitlines()
                           if "automatic security updates" in line][-1]
    for headers in (ALONE, MARIO):
        assert (await client.get("/api/machine/os-updates", headers=headers)).status_code == 403
        assert (await client.post("/api/machine/os-updates", headers=headers, json={"enabled": False, "reboot": "never"})).status_code == 403

    async def refused(*args, **kw):
        raise JobFailed("sudo: a password is required")
    monkeypatch.setattr(workspaceops, "ask", refused)
    resp = await client.get("/api/machine/os-updates", headers=ADMIN)
    assert resp.status_code == 502 and "OMNIACLI_WORKSPACES=1" in resp.json()["error"]


async def test_the_options_page(panel):
    client = panel
    page = (await client.get("/options", headers={**ADMIN, "Accept": "text/html", "Accept-Language": "it"})).text
    section = page.split('id="os-updates"', 1)[1].split("</section>", 1)[0]
    assert "Aggiornamenti di sicurezza automatici del sistema" in section and 'role="switch"' in section
    assert "mai: la riavvio io" in section and 'class="primary' not in section
    # through a link: its own address
    page = (await client.get("/options", headers={**PERSON, "Accept": "text/html"})).text
    assert '"/links/lab3/api/machine/os-updates"' in page
    assert os.path.exists(client.machine["folder"] / "20auto-upgrades")
