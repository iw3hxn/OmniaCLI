# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A machine behind NAT managed from a central panel (AD-017 §17.6, slice 8;
README, Agent mode): its SSH published through a relay by a tunnel of its
own (its key, its permitlisten line), the central panels it lets in allowed
to open that one port of the relay and nothing else, the central's link
through the relay end to end; the root helper on both sides, the routes,
the REST API, MCP, the page, the purge."""

import asyncio
import base64
import json
import re
import shlex
import stat

import httpx
import pytest
import yaml

from omniacli import apiv1, relay, sshlinks, workspacehelper, workspaceops
from omniacli.app import create_app
from omniacli.auth import Principal, hash_token
from omniacli.composehelper import HelperError
from omniacli.config import ConfigError, LinkTarget, parse_link, parse_settings

from .conftest import LINK_TOKEN, USER_HASH
from .test_api_v1 import token
from .test_app import POST, client, wait_job  # noqa: F401 - the client fixture
from .test_links import as_user
from .test_mcp import HEADERS, call
from .test_relay import HOST_KEY, PUBLIC, host, relay_root  # noqa: F401 - the relay_root fixture
from .test_stacktemplates import root  # noqa: F401 - the root fixture
from .test_tunnel import serve
from .test_workspaceweb import web_root  # noqa: F401 - the web_root fixture


def ed25519(seed: int) -> str:
    raw = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes([seed]) * 32
    return "ssh-ed25519 " + base64.b64encode(raw).decode()


# the keys of two central panels (their links' key), and a person's key that forwards nothing
CENTRAL, OTHER_CENTRAL, PERSON = ed25519(1), ed25519(2), ed25519(3)
RELAY_KEY = ed25519(4)


# --- the texts -----------------------------------------------------------------

def test_the_agents_lines_on_the_relay():
    agents = {"lab4": {"public_key": PUBLIC, "port": 31002, "allowed": [CENTRAL]},
              "lab6": {"public_key": ed25519(9), "port": 31001, "allowed": [CENTRAL, OTHER_CENTRAL]}}
    keys = relay.authorized_keys({"lab4.acme": {"public_key": PUBLIC, "port": 31000}}, agents)
    lines = [line for line in keys.splitlines() if not line.startswith("#")]
    # the agent's tunnel: its own line, listening on its port only, as a workspace's
    assert f'restrict,port-forwarding,permitlisten="127.0.0.1:31002",command="/bin/false" {PUBLIC} ' \
           "omniacli-relay:lab4.agent" in lines
    assert len(lines) == 3 and all("permitopen" not in line for line in lines)
    jump = [line for line in relay.jump_keys(agents).splitlines() if not line.startswith("#")]
    # one line a key (sshd reads the first that matches): each central panel opens the ports of the machines
    # that let it in, nothing else: no listen, no shell, no command, no pty
    assert jump == [
        f'restrict,port-forwarding,permitopen="127.0.0.1:31001",permitopen="127.0.0.1:31002",command="/bin/false" '
        f"{CENTRAL} omniacli-relay-jump",
        f'restrict,port-forwarding,permitopen="127.0.0.1:31001",command="/bin/false" {OTHER_CENTRAL} omniacli-relay-jump']
    assert all("permitlisten" not in line and "pty" not in line for line in jump)
    assert relay.agent_ports(agents) == [31001, 31002]
    with pytest.raises(relay.RelayError, match="not a port of 31000-31999"):
        relay.jump_keys({"x": {"public_key": PUBLIC, "port": 22, "allowed": [CENTRAL]}})
    with pytest.raises(relay.RelayError, match="ssh-ed25519"):
        relay.jump_keys({"x": {"public_key": PUBLIC, "port": 31000, "allowed": ['ssh-ed25519 AAAA" ,command="sh']}})
    with pytest.raises(relay.RelayError):
        relay.check_allowed([])
    assert relay.label_of("Ufficio Milano") == "ufficio-milano"


def test_the_match_block_lets_the_central_panels_open_those_ports_only():
    block = relay.sshd_block([31001, 31002])
    jump = block.split("Match User omniacli-relay-jump")[1]
    for line in ("AllowTcpForwarding local", "PermitOpen 127.0.0.1:31001 127.0.0.1:31002", "PermitListen none",
                 "ForceCommand /bin/false", "PermitTTY no", "AllowAgentForwarding no", "PasswordAuthentication no"):
        assert f"    {line}" in jump.splitlines(), line
    # the tunnels' user stays as it was: remote forwards only, PermitOpen none
    tunnels = block.split("Match User omniacli-relay-jump")[0]
    assert "    AllowTcpForwarding remote" in tunnels and "    PermitOpen none" in tunnels
    assert "omniacli-relay-jump" not in relay.sshd_block() and block.endswith(relay.SSHD_END)
    text = relay.with_block("Port 22\n", [31001])
    assert relay.with_block(text, [31001]) == text and relay.without_block(text).strip() == "Port 22"


def test_the_central_panels_a_machine_lets_in():
    text = "\n".join([
        f'restrict,port-forwarding,permitopen="127.0.0.1:8090",command="/bin/false" {CENTRAL} omniacli@lab3',
        f"{PERSON} carlo@laptop",
        "# a comment",
        f'restrict,port-forwarding,permitopen="127.0.0.1:8090",command="/bin/false" {CENTRAL} again',
        f'permitopen="127.0.0.1:8090" {OTHER_CENTRAL}'])
    assert relay.link_keys(text) == [CENTRAL, OTHER_CENTRAL]


def test_the_unit_of_the_agents_tunnel():
    text = relay.agent_unit_text("lab5", "203.0.113.9", 2222, 31004, 22, "/etc/omniacli/agent-relay")
    start = next(line for line in text.splitlines() if line.startswith("ExecStart="))
    for option in ("-R 127.0.0.1:31004:127.0.0.1:22", "omniacli-relay@203.0.113.9", "-p 2222",
                   "ExitOnForwardFailure=yes", "StrictHostKeyChecking=yes", "ServerAliveInterval=10",
                   "UserKnownHostsFile=/etc/omniacli/agent-relay/known_hosts", "-i /etc/omniacli/agent-relay/id_ed25519"):
        assert option in start, option
    for line in ("User=omniacli-tunnel", "Restart=always", "RestartSec=5", "StartLimitIntervalSec=0"):
        assert line in text.splitlines()
    with pytest.raises(relay.RelayError):
        relay.agent_unit_text("lab5", "203.0.113.9", 22, 8090, 22, "/f")


# --- the central's link: through the relay, end to end ------------------------------------------------------

SSH = {"host": "lab4", "user": "omniacli-link", "host_key": HOST_KEY}
HOP = {"host": "relay.example", "port": 2222, "host_key": RELAY_KEY, "tunnel_port": 31004}


def test_a_link_through_a_relay():
    link = parse_link("lab4", {"url": "http://127.0.0.1:8090", "token": "t", "ssh": {**SSH, "relay": HOP}})
    assert link.ssh.relay.tunnel_port == 31004 and link.to_dict()["ssh"]["relay"] == HOP
    assert parse_link("lab4", link.to_dict()) == link
    for bad, error in (({**HOP, "tunnel_port": 8090}, "not a port of a relay's tunnel"),
                       ({**HOP, "tunnel_port": True}, "not a port of a relay's tunnel"),
                       ({**HOP, "host": "-oProxyCommand=sh"}, "not a host name"),
                       ({**HOP, "host_key": ""}, "key is missing"), ("yes", "must be a mapping")):
        with pytest.raises(ConfigError, match=error):
            parse_link("lab4", {"url": "http://127.0.0.1:8090", "token": "t", "ssh": {**SSH, "relay": bad}})


def test_the_ssh_of_a_link_through_a_relay(tmp_path):
    link = parse_link("lab4", {"url": "http://127.0.0.1:8090", "token": "t", "ssh": {**SSH, "relay": HOP}})
    cmd = sshlinks.ssh_command(link, tmp_path / "id", tmp_path / "known", 18001, "ssh")
    options = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-o"]
    proxy = shlex.split(next(o for o in options if o.startswith("ProxyCommand=")).removeprefix("ProxyCommand="))
    # the relay: its own key only, its user of the central panels, a -W to the machine's tunnel and nothing else
    assert proxy[proxy.index("-W") + 1] == "127.0.0.1:31004" and proxy[-3:] == ["-l", "omniacli-relay-jump", "relay.example"]
    assert {"StrictHostKeyChecking=yes", f"UserKnownHostsFile={tmp_path / 'known'}", "HostKeyAlias=omniacli-relay-lab4",
            "BatchMode=yes", "IdentitiesOnly=yes"} <= {proxy[i + 1] for i, a in enumerate(proxy) if a == "-o"}
    assert "-L" not in proxy and "-R" not in proxy and proxy[proxy.index("-p") + 1] == "2222"
    # the session itself is the machine's, end to end: its own key, its link user, the forward to its OmniaCLI
    assert "HostKeyAlias=omniacli-link-lab4" in options
    assert cmd[-7:] == ["-L", "127.0.0.1:18001:127.0.0.1:8090", "-p", "22", "-l", "omniacli-link", "lab4"]
    assert sshlinks.known_hosts_text(link) == (f"omniacli-link-lab4 {HOST_KEY}\nomniacli-relay-lab4 {RELAY_KEY}\n")
    # ssh runs a ProxyCommand in $SHELL: the service's user has nologin
    assert sshlinks.ssh_env()["SHELL"] == "/bin/sh"
    direct = parse_link("lab4", {"url": "http://127.0.0.1:8090", "token": "t", "ssh": SSH})
    assert not any(a.startswith("ProxyCommand") for a in sshlinks.ssh_command(direct, tmp_path / "id", tmp_path / "k", 1))
    assert sshlinks.known_hosts_text(direct) == f"omniacli-link-lab4 {HOST_KEY}\n"


async def test_a_drop_after_the_tunnel_was_up_waits_the_first_wait(monkeypatch, tmp_path):
    """The central's tunnel to a machine behind a relay drops whenever the machine's tunnel does (killed, a line
    cut): it was up, so it tries again after the first wait, not after one grown by the drops before (on the lab
    the third kill in a row was back in 40 s, past the 35 s of AD-017 §9.8)."""
    monkeypatch.setattr(sshlinks, "RETRY_SECONDS", (0.01, 0.03, 0.09, 0.27))
    tunnels = sshlinks.SshTunnels(tmp_path)
    link = parse_link("lab4", {"url": "http://127.0.0.1:8090", "token": "t", "ssh": {**SSH, "relay": HOP}})
    tunnel = sshlinks._Tunnel(link, 18001)
    # up, dropped; up, dropped; then refused twice (the machine's tunnel not back yet); up
    runs, waits = iter([True, True, False, False, True]), []

    async def run_once(t):
        waits.append(t.detail.rsplit("again in ", 1)[-1] if "again in" in t.detail else "")
        try:
            up = next(runs)
        except StopIteration:
            raise asyncio.CancelledError from None
        if up:
            tunnels._set(t, "up")
            t.came_up = True
        else:
            tunnels._set(t, "down", "channel 0: open failed: connect failed")
    monkeypatch.setattr(tunnels, "_run_once", run_once)
    with pytest.raises(asyncio.CancelledError):
        await tunnels._keep(tunnel)
    # after each run that was up the first wait; the refusals in a row grow it as before
    assert waits == ["", "0.01 s", "0.01 s", "0.03 s", "0.09 s", "0.01 s"]


# --- the root helper on the relay ----------------------------------------------------------------------------

def agent_host(monkeypatch, label="lab4", **change):
    spec = {"link": label, "public_key": PUBLIC, "allowed": [CENTRAL], **change}
    monkeypatch.setattr(workspacehelper.sys, "stdin", type("In", (), {"read": lambda self, n: json.dumps(spec)})())
    return workspacehelper.relay_agent_host(label)


@pytest.fixture
def agents_root(relay_root, monkeypatch):
    paths = {"AGENTS": relay_root["tmp"] / "etc-agents.json", "JUMP_HOME": relay_root["tmp"] / "jump-home",
             "AGENT_ROOT": relay_root["tmp"] / "etc" / "agent-relay"}
    for name, path in paths.items():
        monkeypatch.setattr(workspacehelper, name, path)
    return {**relay_root, **paths}


def test_the_relay_hosts_an_agents_tunnel(agents_root, monkeypatch):
    monkeypatch.setattr(workspacehelper, "_reaches_here", lambda name: False)
    # a workspace's names already there: the agent takes the next port, both lines in the same file
    assert host(monkeypatch)["port"] == 31000
    made = agent_host(monkeypatch)
    assert (made["port"], made["user"], made["jump_user"], made["host_key"], made["ssh_port"]) == \
        (31001, "omniacli-relay", "omniacli-relay-jump", HOST_KEY, 2222)
    keys = (agents_root["RELAY_HOME"] / ".ssh" / "authorized_keys").read_text()
    assert 'permitlisten="127.0.0.1:31000"' in keys and \
        f'permitlisten="127.0.0.1:31001",command="/bin/false" {PUBLIC} omniacli-relay:lab4.agent' in keys
    jump = (agents_root["JUMP_HOME"] / ".ssh" / "authorized_keys").read_text()
    assert f'restrict,port-forwarding,permitopen="127.0.0.1:31001",command="/bin/false" {CENTRAL}' in jump
    # the central's key opens the agent's port, never the Odoo names' one (31000)
    assert "31000" not in jump and "permitlisten" not in jump
    sshd = agents_root["SSHD_CONFIG"].read_text()
    assert "Match User omniacli-relay-jump" in sshd and "    PermitOpen 127.0.0.1:31001\n" in sshd
    assert stat.S_IMODE(agents_root["AGENTS"].stat().st_mode) == 0o600
    useradd = [c[-1] for c in agents_root["calls"] if c[0] == "useradd"]
    assert useradd == ["omniacli-relay", "omniacli-relay-jump"]
    # again: the same port; another machine letting in the same central panel: one line, both ports
    assert agent_host(monkeypatch)["port"] == 31001
    assert agent_host(monkeypatch, label="lab6", public_key=ed25519(9), allowed=[CENTRAL, OTHER_CENTRAL])["port"] == 31002
    jump = (agents_root["JUMP_HOME"] / ".ssh" / "authorized_keys").read_text()
    assert f'permitopen="127.0.0.1:31001",permitopen="127.0.0.1:31002",command="/bin/false" {CENTRAL}' in jump
    listed = workspacehelper.relay_hosted()
    assert set(listed["agents"]) == {"lab4", "lab6"} and listed["agents"]["lab6"]["allowed"] == 2
    assert set(listed["hosted"]) == {"lab4.acme"}
    # the workspace's tunnel taken away: the agents' lines stay
    workspacehelper.relay_unhost("lab4.acme")
    assert "permitlisten=\"127.0.0.1:31001\"" in (agents_root["RELAY_HOME"] / ".ssh" / "authorized_keys").read_text()
    assert "omniacli-relay" in agents_root["users"]
    # one agent taken away: its port leaves both files and the Match block
    done = workspacehelper.relay_agent_unhost("lab4")
    assert done["was"] and done["left"] == 1
    jump = (agents_root["JUMP_HOME"] / ".ssh" / "authorized_keys").read_text()
    assert "31001" not in jump and "31002" in jump and "PermitOpen 127.0.0.1:31002\n" in agents_root["SSHD_CONFIG"].read_text()
    # the last: nothing of the relay stays
    done = workspacehelper.relay_agent_unhost("lab6")
    assert done["left"] == 0 and {"the user omniacli-relay", "the user omniacli-relay-jump"} <= set(done["removed"])
    assert not agents_root["JUMP_HOME"].exists() and not agents_root["RELAY_HOME"].exists()
    assert not agents_root["AGENTS"].exists() and "OmniaCLI relay" not in agents_root["SSHD_CONFIG"].read_text()
    assert not agents_root["users"]
    assert workspacehelper.relay_agent_unhost("lab6")["was"] is False


def test_the_relay_refuses_an_agent(agents_root, monkeypatch):
    with pytest.raises(HelperError, match="central panels, 1 to 20"):
        agent_host(monkeypatch, allowed=[])
    with pytest.raises(HelperError, match="ssh-ed25519"):
        agent_host(monkeypatch, allowed=["ssh-rsa AAAA"])
    with pytest.raises(HelperError, match="not one of the central panels"):
        agent_host(monkeypatch, allowed=[PUBLIC])
    with pytest.raises(HelperError, match="name of a machine"):
        agent_host(monkeypatch, label="../x")
    # an sshd that refuses the Match blocks: nothing of a new agent stays
    agents_root["state"]["sshd_t"] = 1
    with pytest.raises(HelperError, match="sshd -t refused"):
        agent_host(monkeypatch)
    assert workspacehelper._agents() == {} and agents_root["SSHD_CONFIG"].read_text() == "Port 22\n"
    assert not (agents_root["JUMP_HOME"] / ".ssh" / "authorized_keys").exists()


# --- the root helper on the machine behind NAT -------------------------------------------------------------

def test_the_machine_publishes_its_ssh_and_takes_it_away(agents_root, monkeypatch, tmp_path):
    home = tmp_path / "link-home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "authorized_keys").write_text(
        f'restrict,port-forwarding,permitopen="127.0.0.1:8090",command="/bin/false" {CENTRAL} omniacli@lab3\n'
        f"{PERSON} carlo@laptop\n")
    getpwnam = workspacehelper.pwd.getpwnam

    def pwnam(name):
        if name == "omniacli-link":
            return type("pw", (), {"pw_dir": str(home), "pw_uid": 0, "pw_gid": 0})()
        return getpwnam(name)
    monkeypatch.setattr(workspacehelper.pwd, "getpwnam", pwnam)
    keyed = workspacehelper.agent_relay_key()
    # the central panels its link user lets forward, never a person's key; the port of this sshd
    assert keyed == {"public_key": PUBLIC, "allowed": [CENTRAL], "user": "omniacli-link", "ssh_port": 2222}
    assert stat.S_IMODE((agents_root["AGENT_ROOT"] / "id_ed25519").stat().st_mode) == 0o600
    monkeypatch.setattr(workspacehelper.sys, "stdin", type("In", (), {"read": lambda self, n: json.dumps({
        "relay": "lab5", "host": "203.0.113.9", "ssh_port": 22, "remote_port": 31001, "host_key": HOST_KEY})})())
    monkeypatch.setattr(workspacehelper.os, "chown", lambda *a: None)
    done = workspacehelper.agent_relay_tunnel("on")
    unit = agents_root["systemd"] / "omniacli-agent-relay.service"
    assert done["unit"] == unit.name and "-R 127.0.0.1:31001:127.0.0.1:2222" in unit.read_text()
    assert (agents_root["AGENT_ROOT"] / "known_hosts").read_text() == f"203.0.113.9 {HOST_KEY}\n"
    assert ["systemctl", "restart", unit.name] in agents_root["calls"] and "omniacli-tunnel" in agents_root["users"]
    # a workspace's tunnel there too: taking the agent's away keeps the tunnels' user for it
    workspacehelper.relay_key("acme")
    done = workspacehelper.agent_relay_tunnel("off")
    assert not unit.exists() and not agents_root["AGENT_ROOT"].exists() and "omniacli-tunnel" in agents_root["users"]
    assert "the user omniacli-tunnel" not in done["removed"]
    with pytest.raises(HelperError, match="on\\|off"):
        workspacehelper.agent_relay_tunnel("maybe")
    for action, count in (("relay-agent-host", 1), ("relay-agent-unhost", 1), ("agent-relay-key", 0),
                          ("agent-relay-tunnel", 1)):
        assert workspacehelper.ACTIONS[action][1] == count


# --- the routes: the relay's, the machine's (a real relay app, through its link), the REST API, MCP, the page ---

@pytest.fixture
def helpers(monkeypatch):
    """The root helper of both machines faked: what each one was asked, the agents the relay holds."""
    state = {"calls": [], "agents": {}, "allowed": [CENTRAL], "listening": True}

    async def ask(*args, timeout=None, input=None):
        if args[0] == "relay-hosted":
            return {"hosted": {}, "agents": state["agents"]}
        return {"ours": False, "nginx": False, "addresses": ["203.0.113.9"], "ssh_port": 22, "host_key": HOST_KEY,
                "hosted": 0}

    async def helper(job, *args, timeout=None, input=None):
        given = json.loads(input) if input else None
        state["calls"].append((args, given))
        if args[0] == "relay-agent-host":
            state["agents"][args[1]] = {"link": given["link"], "port": 31001, "allowed": len(given["allowed"]),
                                        "listening": state["listening"]}
            return {"label": args[1], "port": 31001, "host_key": HOST_KEY, "ssh_port": 22, "user": "omniacli-relay",
                    "jump_user": "omniacli-relay-jump", "allowed": len(given["allowed"]), "new": True,
                    "sshd": "sshd reloaded"}
        if args[0] == "relay-agent-unhost":
            state["agents"].pop(args[1], None)
            return {"label": args[1], "removed": ["its lines"], "left": 0}
        if args[0] == "agent-relay-key":
            return {"public_key": PUBLIC, "allowed": list(state["allowed"]), "user": "omniacli-link", "ssh_port": 22}
        if args[0] == "agent-relay-tunnel":
            return {"unit": relay.AGENT_UNIT, "local_port": 22, "removed": ["the unit"]}
        raise AssertionError(args)
    monkeypatch.setattr(workspaceops, "ask", ask)
    monkeypatch.setattr(workspaceops, "_helper", helper)
    return state


@pytest.fixture
def relay_app(tmp_path, helpers):
    """Another OmniaCLI, the relay: the machine behind NAT comes in through its link (omnia, admin)."""
    conf = tmp_path / "relay"
    conf.mkdir()
    settings = parse_settings({
        "auth": {"users": {"admin": USER_HASH},
                 "links": {"omnia": {"token": hash_token(LINK_TOKEN), "role": "admin"},
                           "other": {"token": hash_token("other-link-token"), "role": "admin"}}},
        "backup_dir": str(tmp_path / "relay-backups"), "jobs_dir": str(tmp_path / "relay-jobs"),
        "servers_file": "servers.yaml"}, base_dir=conf)
    (conf / "servers.yaml").write_text(yaml.safe_dump({}))
    server, thread, url = serve(create_app(settings))
    yield url
    server.should_exit = True
    thread.join(10)


async def test_the_relays_routes(relay_app, helpers):
    link = {"Authorization": f"Bearer {LINK_TOKEN}", "Accept": "application/json"}
    body = {"public_key": PUBLIC, "allowed": [CENTRAL]}
    async with httpx.AsyncClient(base_url=relay_app) as http:
        # a person may not ask it: a machine asks it by itself, through its link
        person = await http.post("/api/relay/agents", json=body, auth=("admin", "secret"),
                                 headers={"Origin": relay_app, "Accept": "application/json"})
        assert person.status_code in (401, 403)
        for bad in ({**body, "allowed": []}, {**body, "public_key": "x"}, {**body, "allowed": ["ssh-rsa AAAA"]}):
            assert (await http.post("/api/relay/agents", json=bad, headers=link)).status_code == 400
        resp = await http.post("/api/relay/agents", json=body, headers=link)
        assert resp.status_code == 200, resp.text
        for _ in range(100):
            job = (await http.get(f"/api/jobs/{resp.json()['job']}", headers=link)).json()
            if job.get("status") not in ("queued", "running"):
                break
            await asyncio.sleep(0.05)
        assert job["status"] == "success" and job["result"]["agent"]["port"] == 31001
        assert helpers["calls"][-1] == (("relay-agent-host", "omnia"), {"link": "omnia", "public_key": PUBLIC,
                                                                         "allowed": [CENTRAL]})
        # each machine reads and takes away its own only
        helpers["agents"]["x"] = {"link": "other", "port": 31002, "allowed": 1, "listening": True}
        assert set((await http.get("/api/relay/hosted", headers=link)).json()["agents"]) == {"omnia"}
        assert (await http.post("/api/relay/agents/x/remove", headers=link)).status_code == 403
        assert (await http.post("/api/relay/agents/omnia/remove", headers=link)).status_code == 200
        await asyncio.sleep(0.3)
        assert "omnia" not in helpers["agents"]
        assert (await http.post("/api/relay/agents/omnia/remove", headers=link)).json() == {"ok": True, "gone": True}


async def test_the_machine_publishes_its_ssh_through_its_relay(client, relay_app, helpers):  # noqa: F811
    settings = client.app.state.settings
    settings.link_targets["lab5"] = LinkTarget("lab5", relay_app, LINK_TOKEN)
    admin = await token(client, "boss", "admin")
    v1 = apiv1.PREFIX + "/agent-relay"
    status = (await client.get(v1, headers=admin)).json()
    assert not status["published"] and status["choices"] == [
        {"name": "lab5", "can": True, "reason": "", "address": "203.0.113.9", "private": False}]
    # no central panel let in yet: the job says what to do first, nothing asked of the relay
    helpers["allowed"] = []
    run = await client.post(v1, headers=admin, json={"relay": "lab5"})
    assert run.status_code == 202
    job = await wait_job(client, run.json()["job"]["id"])
    assert job["status"] == "failed" and "No central panel is let in" in json.dumps(job)
    assert not any(c[0][0] == "relay-agent-host" for c in helpers["calls"])
    helpers["allowed"] = [CENTRAL]
    run = await client.post(v1, headers=admin, json={"relay": "lab5"})
    job = await wait_job(client, run.json()["job"]["id"])
    assert job["status"] == "success", job
    # the relay was asked through the link, its job followed; then the unit here, to the port it gave
    asked = next(given for args, given in helpers["calls"] if args[0] == "relay-agent-host")
    assert asked == {"link": "omnia", "public_key": PUBLIC, "allowed": [CENTRAL]}
    tunnel = next(given for args, given in helpers["calls"] if args == ("agent-relay-tunnel", "on"))
    assert tunnel == {"relay": "lab5", "host": "203.0.113.9", "ssh_port": 22, "remote_port": 31001,
                      "host_key": HOST_KEY}
    status = (await client.get(v1, headers=admin)).json()
    assert status["published"] and status["tunnel_port"] == 31001 and status["address"] == "203.0.113.9"
    assert status["allowed"] == [sshlinks.fingerprint(CENTRAL)] and status["relay_key"] == sshlinks.fingerprint(HOST_KEY)
    record = settings.servers_file.parent / relay.AGENT_RECORD
    assert stat.S_IMODE(record.stat().st_mode) == 0o600 and LINK_TOKEN not in record.read_text()
    log = (settings.logs_dir / "access.log").read_text() if (settings.logs_dir / "access.log").exists() else ""
    assert "this machine's SSH published through lab5" in log or "published through lab5" in json.dumps(job)
    # MCP: the same code; then taken away, on both machines
    tools = {**HEADERS, **admin}
    assert (await call(client, "agent_relay_status", headers=tools))["tunnel_port"] == 31001
    removed = await call(client, "agent_relay_remove", headers=tools, wait_seconds=30)
    assert removed["status"] == "success", removed
    assert ("agent-relay-tunnel", "off") in [c[0] for c in helpers["calls"]] and not helpers["agents"]
    assert not record.exists() and not (await client.get(v1, headers=admin)).json()["published"]


async def test_who_publishes_the_machines_ssh(client):  # noqa: F811
    developer = await token(client, "dev-bot", "developer")
    ci = await token(client, "ci-bot", "admin", ci=True)
    v1 = apiv1.PREFIX + "/agent-relay"
    assert (await client.get(v1, headers=developer)).json()["error"]["code"] == "forbidden"
    assert (await client.post(v1, headers=ci, json={"relay": "x"})).json()["error"]["code"] == "forbidden"
    admin = await token(client, "boss", "admin")
    assert (await client.post(v1, headers=admin, json={"relay": "nope"})).json()["error"]["code"] == "not_found"
    # never through a link: the link may be the one that rides on it
    hook = client.app.state.api_hooks["agent_relay"]
    code, data = await hook("status", Principal("omnia", "admin", "link"), "link omnia")
    assert code == 403 and "not through a link" in data["error"]
    assert (await client.get("/api/agent-relay", headers={"Authorization": f"Bearer {LINK_TOKEN}",
                                                           "Accept": "application/json"})).status_code == 403
    for method, url in (("GET", "/agent-relay"), ("POST", "/api/agent-relay/publish")):
        assert (await client.request(method, url, headers=as_user("dev", post=True), json={})).status_code == 403


async def test_the_page_of_the_machine(client, relay_app, helpers):  # noqa: F811
    settings = client.app.state.settings
    settings.link_targets["lab5"] = LinkTarget("lab5", relay_app, LINK_TOKEN)
    page = await client.get("/agent-relay", headers={**as_user("admin"), "Cookie": "omniacli_lang=it"})
    assert page.status_code == 200 and "<h1>Raggiungibile attraverso un relay</h1>" in page.text
    # one primary button, its text other than the title; no browser dialog; the addresses under the prefix
    assert page.text.count('class="primary"') == 1 and ">Pubblica l&#39;SSH<" in page.text
    assert "confirm(" not in page.text and "alert(" not in page.text and "omniaAsk(" in page.text
    assert '<option value="lab5"' in page.text and "203.0.113.9" in page.text
    assert 'href="/agent-relay"' in (await client.get("/settings", headers=as_user("admin"))).text
    # published: the port the central panel's link names, the tunnel, the keys let in
    resp = await client.post("/api/agent-relay/publish", headers=POST, json={"relay": "lab5"})
    assert (await wait_job(client, resp.json()["job"]))["status"] == "success"
    page = await client.get("/agent-relay", headers=as_user("admin"))
    assert 'id="ar-port">31001<' in page.text and sshlinks.fingerprint(CENTRAL) in page.text
    assert "Stop publishing…" in page.text and page.text.count('class="primary"') == 1


async def test_the_central_sets_its_link_through_the_relay(client, monkeypatch):  # noqa: F811
    settings = client.app.state.settings
    settings.link_targets["lab4"] = parse_link("lab4", {"url": "http://127.0.0.1:8090", "token": "t", "ssh": SSH})
    settings.link_targets["plain"] = parse_link("plain", {"url": "https://x.example", "token": "t"})
    fetched = []

    async def fetch(host, port):
        fetched.append((host, port))
        return RELAY_KEY

    async def fetch_via(hop, folder):
        fetched.append(("via", hop.host, hop.tunnel_port))
        return HOST_KEY
    monkeypatch.setattr(sshlinks, "fetch_host_key", fetch)
    monkeypatch.setattr(sshlinks, "fetch_host_key_via", fetch_via)
    admin = await token(client, "boss", "admin")
    url = apiv1.PREFIX + "/links/lab4/relay"
    # without the relay's key: it is given, to be compared, and nothing is saved
    first = (await client.post(url, headers=admin, json={"host": "relay.example", "port": 2222, "tunnel_port": 31004})).json()
    assert first["saved"] is False and first["host_key"] == RELAY_KEY and fetched == [("relay.example", 2222)]
    assert settings.link_targets["lab4"].ssh.relay is None
    # the machine's key read through the relay, the same way
    second = (await client.post(url, headers=admin, json={**HOP, "fetch_machine_key": True})).json()
    assert second == {"saved": False, "machine_host_key": HOST_KEY, "fingerprint": sshlinks.fingerprint(HOST_KEY),
                      "hint": second["hint"]} and fetched[-1] == ("via", "relay.example", 31004)
    saved = (await client.post(url, headers=admin, json=HOP)).json()
    assert saved["saved"] and saved["relay"] == HOP and settings.link_targets["lab4"].ssh.relay.tunnel_port == 31004
    assert "relay" in yaml.safe_load(settings.links_file.read_text())["links"]["lab4"]["ssh"]
    assert (await client.post(url, headers=admin, json={**HOP, "tunnel_port": 22})).status_code == 400
    assert (await client.post(apiv1.PREFIX + "/links/plain/relay", headers=admin, json=HOP)).status_code == 409
    developer = await token(client, "dev-bot", "developer")
    assert (await client.post(url, headers=developer, json=HOP)).status_code == 403
    # MCP: directly again
    tools = {**HEADERS, **admin}
    assert (await call(client, "link_relay", headers=tools, link="lab4"))["relay"] is None
    assert settings.link_targets["lab4"].ssh.relay is None
    # the Links page: the relay in its row and in its dialog; its save names it
    body = {"name": "lab4", "url": "http://127.0.0.1:8090", "token": "", "ssh": {**SSH, "relay": HOP}}
    assert (await client.post("/api/links/save", headers=POST, json=body)).json() == {"ok": True}
    page = (await client.get("/links", headers={**as_user("admin"), "Cookie": "omniacli_lang=it"})).text
    assert "attraverso il relay relay.example:2222, porta 31004" in page
    assert 'data-relay-tunnel="31004"' in page and 'id="ld-relay"' in page
    # the server's key through the relay, from the dialog
    resp = await client.post("/api/links/ssh/host-key", headers=POST, json={"relay": HOP})
    assert resp.json()["host_key"] == HOST_KEY
    assert (await client.post("/api/links/ssh/host-key", headers=POST,
                              json={"relay": {**HOP, "tunnel_port": 80}})).status_code == 400


def test_purge_removes_what_the_agents_relay_leaves():
    text = (relay.__file__.rsplit("/omniacli/", 1)[0] + "/packaging/debian/files/postrm")
    postrm = open(text, encoding="utf-8").read()
    for item in ("omniacli-agent-relay.service", "/etc/omniacli/agent-relay", "/etc/omniacli/relay-agents.json",
                 "userdel omniacli-relay-jump", "/var/lib/omniacli-relay-jump", "/var/lib/omniacli/agent-relay.json"):
        assert item in postrm, item
    assert re.search(r"sed -i .*OmniaCLI relay", postrm)
