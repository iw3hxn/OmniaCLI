# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Names published through another machine (relay.py; README, Published
through another machine): the line of the relay's authorized_keys that lets
a tunnel's key listen on one port and nothing else, the Match block of its
sshd, the relay's nginx site, the unit that keeps the tunnel up, the block of
the workspace's site on the machine behind NAT, the root helper's actions on
both machines (made and taken away), the routes and what a customer reads."""

import dataclasses
import json
import re
import subprocess

import pytest

from omniacli import relay, workspacehelper, workspaceops, workspaceweb
from omniacli.composehelper import HelperError
from omniacli.config import Workspace, addresses_follow, parse_workspaces

from .conftest import LINK_TOKEN
from .test_addresses import API, dialog  # noqa: F401 - the dialog fixture
from .test_app import POST, client, wait_job  # noqa: F401 - the client fixture
from .test_stacktemplates import root  # noqa: F401 - the root fixture
from .test_workspaces import basic, post_as, workspaces  # noqa: F401 - the workspaces fixture
from .test_workspaceweb import spec, web_root  # noqa: F401 - the web_root fixture

PUBLIC = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA/Mn4aNxF5yB5noa8ISaSF/ScHulHckMhT6cwmWPB2Y"
HOST_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDmWx8+20Uijlvz36IhkUmlqFDbJzWqRNdT4TkE+XjFf"
TLS = ("/c/fullchain.pem", "/c/privkey.pem")


# --- the texts -----------------------------------------------------------------

def test_the_key_may_listen_on_its_port_only():
    line = relay.authorized_line(PUBLIC + " omniacli-relay@lab4:acme", 31004, "lab4.acme")
    assert line == (f'restrict,port-forwarding,permitlisten="127.0.0.1:31004",command="/bin/false" {PUBLIC} '
                    "omniacli-relay:lab4.acme")
    # restrict takes away the pty, the agent, X11 and every forward; port-forwarding gives back the forwards,
    # permitlisten keeps the remote one to its port of the loopback; no permitopen: no -L goes anywhere
    assert "permitopen" not in line and "0.0.0.0" not in line
    with pytest.raises(relay.RelayError, match="not a port of 31000-31999"):
        relay.authorized_line(PUBLIC, 22, "lab4.acme")
    with pytest.raises(relay.RelayError, match="ssh-ed25519"):
        relay.authorized_line("ssh-rsa AAAA", 31000, "lab4.acme")
    with pytest.raises(relay.RelayError, match="name of a tunnel"):
        relay.authorized_line(PUBLIC, 31000, 'x" ,command="/bin/sh')
    keys = relay.authorized_keys({"b.x": {"public_key": HOST_KEY, "port": 31001}, "a.y": {"public_key": PUBLIC, "port": 31000}})
    assert [line.split('"')[1] for line in keys.splitlines() if not line.startswith("#")] == \
        ["127.0.0.1:31000", "127.0.0.1:31001"]


def test_the_name_of_a_tunnel():
    assert relay.key_of("lab4", "acme") == "lab4.acme"
    assert relay.key_of("Ufficio_Milano@cliente", "acme") == "ufficio-milano-cliente.acme"
    with pytest.raises(relay.RelayError):
        relay.key_of("lab4", "../etc")


def test_the_match_block_of_the_relay():
    block = relay.sshd_block()
    for line in ("Match User omniacli-relay", "AllowTcpForwarding remote", "GatewayPorts no", "PermitOpen none",
                 "ForceCommand /bin/false", "PermitTTY no", "ClientAliveInterval 10", "ClientAliveCountMax 3"):
        assert line in block
    text = "Port 22\n\n# OmniaCLI link: x\nMatch User omniacli-link\n    PermitOpen 127.0.0.1:8090\n# end of OmniaCLI link\n"
    once = relay.with_block(text)
    assert relay.with_block(once) == once and once.count("Match User omniacli-relay") == 1
    # at the end: a Match block lasts to the next one
    assert once.rstrip().endswith(relay.SSHD_END) and "omniacli-link" in once
    assert relay.without_block(once).rstrip() == text.rstrip()


def test_the_relay_site():
    http = relay.relay_site_text("lab4.acme", ["acme.it", "www.acme.it"], {"acme.com": "acme.it"}, 31000, None, False,
                                 "/acme", "200m", "720s")
    assert "server_name acme.it www.acme.it acme.com;" in http and "return 503;" in http and "listen 443" not in http
    https = relay.relay_site_text("lab4.acme", ["acme.it", "www.acme.it"], {"acme.com": "acme.it"}, 31000, TLS, True,
                                  "/acme", "200m", "720s")
    assert "return 301 https://$host$request_uri;" in https and "return 301 https://acme.it$request_uri;" in https
    served = https.split("# through tunnel lab4.acme, 127.0.0.1:31000")[1]
    assert served.count("proxy_pass http://127.0.0.1:31000;") == 2
    # the visitor's address the relay's own: what a client sent is replaced, never passed on
    assert "proxy_set_header X-Real-IP $remote_addr;" in served and "$proxy_add_x_forwarded_for" not in served
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in served
    assert 'proxy_set_header Connection "upgrade";' in served.split("location /websocket {")[1].split("}")[0]
    assert "listen [::]:443 ssl;" in https


def test_the_unit_of_the_tunnel():
    text = relay.tunnel_unit_text("acme", "lab3", "203.0.113.9", 22, 31000, 30000, "/etc/omniacli/relay/acme")
    start = next(line for line in text.splitlines() if line.startswith("ExecStart="))
    for option in ("-N", "ExitOnForwardFailure=yes", "StrictHostKeyChecking=yes", "BatchMode=yes",
                   "UserKnownHostsFile=/etc/omniacli/relay/acme/known_hosts", "ServerAliveInterval=10",
                   "-R 127.0.0.1:31000:127.0.0.1:30000", "omniacli-relay@203.0.113.9", "-i /etc/omniacli/relay/acme/id_ed25519"):
        assert option in start
    for line in ("User=omniacli-tunnel", "Restart=always", "RestartSec=5", "StartLimitIntervalSec=0", "NoNewPrivileges=yes",
                 "ProtectSystem=strict", "WantedBy=multi-user.target"):
        assert line in text.splitlines()
    assert "[2001:db8::1]" in relay.tunnel_unit_text("acme", "lab3", "2001:db8::1", 22, 31000, 30000, "/f")
    for bad in (("acme", "lab3", "-oProxyCommand=x", 22, 31000, 30000), ("acme", "lab3", "h.it", 22, 22, 30000),
                ("acme", "lab 3", "h.it", 22, 31000, 30000), ("acme", "lab3", "h.it", 22, 31000, 8090)):
        with pytest.raises(relay.RelayError):
            relay.tunnel_unit_text(*bad, "/f")
    assert relay.known_hosts_line("203.0.113.9", 22, HOST_KEY + " root@lab3") == f"203.0.113.9 {HOST_KEY}\n"
    assert relay.known_hosts_line("relay.it", 2222, HOST_KEY) == f"[relay.it]:2222 {HOST_KEY}\n"


def test_cloudflare_tunnel_is_a_place_left_for_later():
    with pytest.raises(NotImplementedError, match="not built"):
        relay.cloudflare_tunnel_unit_text("acme", 30000)


SHOW = """LoadState=loaded
ActiveState=active
SubState=running
ExecMainPID=1234
ExecMainStartTimestamp=Mon 2026-11-23 10:20:30 UTC
ExecMainStartTimestampMonotonic=100000000
ExecMainExitTimestamp=
NRestarts=3
"""


def test_the_state_of_a_tunnel():
    connected = relay.tunnel_state(SHOW, 130_000_000, baseline=1)
    assert connected == {"state": "connected", "since": "2026-11-23T10:20:30", "last_drop": "2026-11-23T10:20:25",
                         "restarts": 2, "nrestarts": 3}
    # just started again: connecting, not yet connected
    assert relay.tunnel_state(SHOW, 105_000_000)["state"] == "connecting"
    # no restart since the publication: no drop
    assert relay.tunnel_state(SHOW, 130_000_000, baseline=3)["last_drop"] is None
    waiting = SHOW.replace("ActiveState=active", "ActiveState=activating").replace("SubState=running", "SubState=auto-restart")
    waiting = waiting.replace("ExecMainExitTimestamp=", "ExecMainExitTimestamp=Mon 2026-11-23 10:21:00 UTC")
    assert relay.tunnel_state(waiting, 130_000_000) == {"state": "connecting", "since": None,
                                                        "last_drop": "2026-11-23T10:21:00", "restarts": 3, "nrestarts": 3}
    assert relay.tunnel_state("LoadState=not-found\n", 0)["state"] == "none"
    tcp = ("  sl  local_address rem_address   st\n   0: 0100007F:7918 00000000:0000 0A\n"
           "   1: 0100007F:7919 0100007F:1F9A 01\n")
    assert relay.listening_ports([tcp]) == {31000}


# --- the configuration -------------------------------------------------------------

def test_the_relay_of_a_workspace():
    data = {"domain": "acme.it", "servers": ["acme-main", "acme-shop"]}
    ws = parse_workspaces({"acme": {**data, "relay": {"via": "lab3", "environments": {"main": ["production"]}}}})["acme"]
    assert (ws.relayed("main", "production"), ws.relayed("main", "test"), ws.relayed("shop", "production")) == ("lab3", "", "")
    assert parse_workspaces({"acme": ws.to_dict()})["acme"] == ws
    whole = parse_workspaces({"acme": {**data, "relay": {"via": "lab3", "all": True}}})["acme"]
    assert whole.relayed("shop", "test") == "lab3" and whole.to_dict()["relay"] == {"via": "lab3", "all": True}
    assert parse_workspaces({"acme": {**data, "relay": {"via": "lab3", "environments": {}}}})["acme"].relay == {}
    for bad, message in (({"via": "lab 3", "all": True}, "name of a link"),
                         ({"via": "lab3", "environments": {"nope": ["production"]}}, "not an instance"),
                         ({"via": "lab3", "environments": {"main": ["staging"]}}, "production, test"), ("lab3", "a mapping")):
        with pytest.raises(Exception, match=message):
            parse_workspaces({"acme": {**data, "relay": bad}})
    # renamed, an instance keeps its publication; deleted, it goes
    renamed = Workspace("acme", domain="acme.it", servers=["acme-store", "acme-shop"], relay=ws.relay)
    assert addresses_follow(ws, renamed, "main", "store").relay == {"via": "lab3", "environments": {"store": ["production"]}}
    gone = Workspace("acme", domain="acme.it", servers=["acme-shop"], relay=ws.relay)
    assert addresses_follow(ws, gone, "main", None).relay == {}


# --- the site of the machine behind NAT --------------------------------------------

SITES = [{"instance": "main", "role": "production", "names": ["acme.it"], "aliases": ["www.customer.com"],
          "http_port": 20000, "websocket_port": 20001, "relay_port": 30000},
         {"instance": "shop", "role": "production", "names": ["shop.acme.it"], "http_port": 20002, "websocket_port": None}]


def test_the_block_of_the_tunnel_in_the_workspaces_site():
    for tls in (None, TLS):
        text = workspaceweb.conf_text("acme", SITES, tls, ipv6=False, omniacli="127.0.0.1:8090")
        block = text.split(f"# main production {workspaceweb.RELAY_MARK}")[1].split("\n}\n")[0]
        # on the loopback only, no TLS: the tunnel is SSH and the relay holds the certificate
        assert "listen 127.0.0.1:30000;" in block and "ssl" not in block
        assert "server_name acme.it www.customer.com;" in block
        # the visitor's address from the relay, believed from the loopback only
        assert "set_real_ip_from 127.0.0.1;" in block and "real_ip_header X-Real-IP;" in block
        assert "proxy_set_header X-Forwarded-Proto https;" in block and "proxy_pass http://127.0.0.1:20001;" in block
        assert "location = /.omniacli/login" in block
    assert "(through its relay)" not in text.split("# shop production")[1]
    # an update without stopping switches the tunnel's block too
    switched = workspaceweb.switch_upstream(workspaceweb.conf_text("acme", SITES, TLS, ipv6=False), "main", "production",
                                            {20000: 21000, 20001: 21001})
    assert switched.count("proxy_pass http://127.0.0.1:21000;") == 2 and "127.0.0.1:20000;" not in switched


# --- the root helper, on the relay and on the machine behind NAT --------------------

@pytest.fixture
def relay_root(web_root, monkeypatch, tmp_path):
    """web_root with the relay's and the tunnel's files in tmp_path, the users
    and sshd faked: what each command did is recorded."""
    paths = {"HOSTED": tmp_path / "etc" / "relay-hosted.json", "RELAY_HOME": tmp_path / "relay-home",
             "TUNNELS": tmp_path / "etc" / "relay", "SSHD_CONFIG": tmp_path / "sshd_config"}
    for name, path in paths.items():
        monkeypatch.setattr(workspacehelper, name, path)
    (tmp_path / "etc").mkdir()
    paths["SSHD_CONFIG"].write_text("Port 22\n")
    (tmp_path / "host.pub").write_text(HOST_KEY + " root@relay\n")
    monkeypatch.setattr(workspacehelper, "SSH_HOST_KEYS", (tmp_path / "host.pub",))
    tcp = tmp_path / "tcp"
    tcp.write_text("  sl  local_address rem_address   st\n")
    monkeypatch.setattr(workspacehelper, "PROC_TCP", (tcp,))
    monkeypatch.setattr(workspacehelper, "_free", lambda port: True)
    users, calls, state = {}, web_root["calls"], {"sshd_t": 0}
    real_run = workspacehelper._run

    def run(*cmd, check=True):
        if cmd[0] == "useradd":
            users[cmd[-1]] = dataclasses.make_dataclass("pw", ["pw_uid", "pw_gid"])(990 + len(users), 990 + len(users))
        if cmd[0] == "userdel":
            users.pop(cmd[-1])
        if cmd[0] == "ssh-keygen":
            key = cmd[cmd.index("-f") + 1]
            open(key, "w").write("PRIVATE\n")
            open(key + ".pub", "w").write(PUBLIC + " omniacli-relay@lab4:acme\n")
        return real_run(*cmd, check=check)
    monkeypatch.setattr(workspacehelper, "_run", run)

    def getpwnam(name):
        if name not in users:
            raise KeyError(name)
        return users[name]
    monkeypatch.setattr(workspacehelper.pwd, "getpwnam", getpwnam)
    web_run = workspacehelper.subprocess.run

    def subprocess_run(cmd, **kw):
        if cmd[0].rsplit("/", 1)[-1] == "sshd":
            calls.append(list(cmd))
            if cmd[1] == "-T":
                return subprocess.CompletedProcess(cmd, 0, "port 2222\nx11forwarding no\n", "")
            return subprocess.CompletedProcess(cmd, state["sshd_t"], "", "line 9: Bad configuration option")
        return web_run(cmd, **kw)
    monkeypatch.setattr(workspacehelper.subprocess, "run", subprocess_run)
    return {**web_root, **paths, "users": users, "state": state, "tcp": tcp}


def host(monkeypatch, key="lab4.acme", **change):
    spec = {"link": "lab4", "workspace": "acme", "public_key": PUBLIC, "names": ["web.acme.it", "www.acme.it"],
            "redirects": {"acme.com": "web.acme.it"}, **change}
    monkeypatch.setattr(workspacehelper.sys, "stdin", type("In", (), {"read": lambda self, n: json.dumps(spec)})())
    return workspacehelper.relay_host(key)


def test_the_relay_hosts_a_tunnel_and_takes_it_away(relay_root, monkeypatch):
    monkeypatch.setattr(workspacehelper, "_reaches_here", lambda name: False)
    made = host(monkeypatch)
    assert (made["port"], made["ssh_port"], made["host_key"], made["certificate"]) == (31000, 2222, HOST_KEY, "self-signed")
    assert "not leading to this machine yet" in made["certificate_error"]
    # its user, without a shell; its home and authorized_keys root's, the key's line restricted to its port
    useradd = next(c for c in relay_root["calls"] if c[0] == "useradd")
    assert useradd[-1] == "omniacli-relay" and "/usr/sbin/nologin" in useradd
    keys = (relay_root["RELAY_HOME"] / ".ssh" / "authorized_keys").read_text()
    assert f'restrict,port-forwarding,permitlisten="127.0.0.1:31000",command="/bin/false" {PUBLIC}' in keys
    assert "Match User omniacli-relay" in relay_root["SSHD_CONFIG"].read_text()
    site = (relay_root["tmp"] / "nginx" / "omniacli-relay-lab4.acme.conf").read_text()
    assert "proxy_pass http://127.0.0.1:31000;" in site and "return 301 https://web.acme.it$request_uri;" in site
    assert (relay_root["tmp"] / "certs" / "relay-lab4.acme" / "fullchain.pem").is_file()
    # the same again: the same port; another machine's tunnel: the next one
    assert host(monkeypatch)["port"] == 31000
    other = host(monkeypatch, key="lab5.beta", link="lab5", workspace="beta", names=["beta.it"], redirects={},
                 public_key=HOST_KEY)
    assert other["port"] == 31001
    assert workspacehelper.relay_hosted()["hosted"]["lab4.acme"]["listening"] is False
    relay_root["tcp"].write_text("  sl  local_address rem_address   st\n   0: 0100007F:7918 00000000:0000 0A\n")
    assert workspacehelper.relay_hosted()["hosted"]["lab4.acme"]["listening"] is True
    # one taken away: its line and site go, the user stays for the other
    done = workspacehelper.relay_unhost("lab4.acme")
    assert done["left"] == 1 and not (relay_root["tmp"] / "nginx" / "omniacli-relay-lab4.acme.conf").exists()
    assert "31000" not in (relay_root["RELAY_HOME"] / ".ssh" / "authorized_keys").read_text()
    assert "omniacli-relay" in relay_root["users"]
    # the last one: nothing of the relay stays
    done = workspacehelper.relay_unhost("lab5.beta")
    assert done["left"] == 0 and "the user omniacli-relay" in done["removed"]
    assert not relay_root["RELAY_HOME"].exists() and not relay_root["HOSTED"].exists()
    assert "omniacli-relay" not in relay_root["SSHD_CONFIG"].read_text() and not relay_root["users"]
    assert not list((relay_root["tmp"] / "certs").iterdir())
    # asked again: nothing to do
    assert workspacehelper.relay_unhost("lab5.beta")["was"] is False


def test_the_relay_refuses(relay_root, monkeypatch):
    monkeypatch.setattr(workspacehelper, "_reaches_here", lambda name: False)
    with pytest.raises(HelperError, match="other sites of nginx answer on: www.other.it"):
        host(monkeypatch, names=["www.other.it"], redirects={})
    with pytest.raises(HelperError, match="not a host name"):
        host(monkeypatch, names=["x;y.it"], redirects={})
    with pytest.raises(HelperError, match="ssh-ed25519"):
        host(monkeypatch, public_key="ssh-ed25519 AAAA")
    with pytest.raises(HelperError, match="a redirect goes to a name"):
        host(monkeypatch, redirects={"acme.com": "elsewhere.it"})
    with pytest.raises(HelperError, match="name of a tunnel"):
        host(monkeypatch, key="../x")
    # nothing of a new tunnel stays when its site is refused
    relay_root["web"]["nginx_t"] = 1
    with pytest.raises(HelperError, match="nginx -t refused"):
        host(monkeypatch)
    assert workspacehelper._hosted() == {}
    assert not (relay_root["RELAY_HOME"] / ".ssh" / "authorized_keys").exists()
    # an sshd that refuses the Match block: sshd_config as it was
    relay_root["web"]["nginx_t"], relay_root["state"]["sshd_t"] = 0, 1
    with pytest.raises(HelperError, match="sshd -t refused"):
        host(monkeypatch)
    assert relay_root["SSHD_CONFIG"].read_text() == "Port 22\n"
    monkeypatch.setattr(workspacehelper, "storage_roots", lambda config=None: {})
    with pytest.raises(HelperError, match="OmniaCLI does not write a site here"):
        host(monkeypatch)


def test_the_machine_behind_nat_opens_and_closes_its_tunnel(relay_root, monkeypatch):
    keyed = workspacehelper.relay_key("acme")
    assert keyed == {"workspace": "acme", "public_key": PUBLIC, "local_port": 30000}
    # the workspace's site gets the tunnel's block, its names out of this machine's certificate
    made = workspacehelper.web("acme", str(relay_root["storage"]), spec(relayed={"main": ["production"]}))
    site = relay_root["site"].read_text()
    assert "listen 127.0.0.1:30000;" in site and made["relayed_names"] == ["acme.it", "main.acme.it"]
    certbot = [c for c in relay_root["web"]["commands"] if c[0].endswith("certbot")][-1]
    assert "acme.it" not in certbot and "test.acme.it" in certbot
    monkeypatch.setattr(workspacehelper.sys, "stdin", type("In", (), {"read": lambda self, n: json.dumps({
        "relay": "lab3", "host": "203.0.113.9", "ssh_port": 2222, "remote_port": 31000, "host_key": HOST_KEY})})())
    done = workspacehelper.relay_tunnel("acme", "on")
    unit = relay_root["systemd"] / "omniacli-relay-acme.service"
    assert done["unit"] == unit.name and "-R 127.0.0.1:31000:127.0.0.1:30000" in unit.read_text()
    assert (relay_root["TUNNELS"] / "acme" / "known_hosts").read_text() == f"[203.0.113.9]:2222 {HOST_KEY}\n"
    assert "omniacli-tunnel" in relay_root["users"]
    assert ["systemctl", "restart", unit.name] in relay_root["calls"]
    done = workspacehelper.relay_tunnel("acme", "off")
    assert not unit.exists() and not relay_root["TUNNELS"].exists() and "the user omniacli-tunnel" in done["removed"]
    assert ["systemctl", "disable", "--now", unit.name] in relay_root["calls"]
    with pytest.raises(HelperError, match="on\\|off"):
        workspacehelper.relay_tunnel("acme", "maybe")
    for action, count in (("relay-facts", 0), ("relay-host", 1), ("relay-unhost", 1), ("relay-hosted", 0),
                          ("relay-key", 1), ("relay-tunnel", 2)):
        assert workspacehelper.ACTIONS[action][1] == count


# --- the routes -------------------------------------------------------------------

async def test_the_state_of_an_environment_as_each_one_reads_it(dialog, client, env):
    settings = env["settings"]
    settings.workspaces["acme"].relay = {"via": "lab3", "all": True}
    # a customer (the workspace's admin): a machine of the provider, no address, nothing to choose
    got = (await client.get(f"{API.rsplit('/', 1)[0]}/relay", headers=basic("mario"))).json()
    assert got["via"] == "a machine of the provider" and got["address"] == "" and not got["can_choose"]
    assert "choices" not in got and got["names"] == ["acme.it"]
    page = (await client.get("/workspaces/acme", headers={**basic("mario"), "Accept-Language": "it"})).text
    assert "una macchina del fornitore" in page and 'id="relay-via"' not in page and "lab3" not in page
    # the machine's admin: the link and the machines it may choose
    got = (await client.get(f"{API.rsplit('/', 1)[0]}/relay", headers={"Authorization": POST["Authorization"]})).json()
    assert got["via"] == "lab3" and got["can_choose"] and got["choices"] == []
    page = (await client.get("/workspaces/acme", headers={"Authorization": POST["Authorization"]})).text
    select = re.search(r'<select id="relay-via"[^>]*>', page).group(0)
    # what it does is written under it (UX-017 §5.13), read with it
    assert 'aria-describedby="relay-via-hint"' in select and '<small class="hint" id="relay-via-hint">' in page


async def test_only_the_machines_admins_choose_the_relay(dialog, client, env):
    body = {"aliases": [], "redirects": [], "relay": "lab3"}
    resp = await client.post(f"{API}/save", headers=post_as("mario"), json=body)
    assert resp.status_code == 403 and "machine's admins" in resp.json()["error"]
    resp = await client.post(f"{API}/save", headers=POST, json=body)
    assert resp.status_code == 400 and resp.json()["error"] == "No link lab3"
    resp = await client.post("/api/workspaces/acme/web/save", headers=post_as("mario"),
                             json={"main": "main", "subdomains": {}, "certificate": "letsencrypt", "relay": "lab3"})
    assert resp.status_code == 403 and not dialog["published"]
    assert (await client.get("/api/relays", headers=basic("mario"))).status_code == 403


@pytest.fixture
def relay_side(env, monkeypatch):
    """This machine as the relay: the link omnia an admin here, the helper faked."""
    settings = env["settings"]
    settings.links["omnia"] = dataclasses.replace(settings.links["omnia"], role="admin")
    state = {"hosted": {}, "calls": []}

    async def ask(*args, timeout=None, input=None):
        if args[0] == "relay-hosted":
            return {"hosted": state["hosted"]}
        return {"ours": True, "nginx": True, "addresses": ["203.0.113.9"], "ssh_port": 22, "host_key": HOST_KEY, "hosted": 0}

    async def helper(job, *args, timeout=None, input=None):
        state["calls"].append((args, json.loads(input) if input else None))
        if args[0] == "relay-host":
            state["hosted"][args[1]] = {"link": json.loads(input)["link"], "port": 31000, "listening": False}
            return {"key": args[1], "port": 31000, "host_key": HOST_KEY, "ssh_port": 22, "user": "omniacli-relay",
                    "certificate": "self-signed", "nginx": "nginx reloaded", "sshd": "sshd unchanged"}
        state["hosted"].pop(args[1], None)
        return {"key": args[1], "removed": ["a site"], "left": 0}
    monkeypatch.setattr(workspaceops, "ask", ask)
    monkeypatch.setattr(workspaceops, "_helper", helper)
    return state


async def test_the_relay_side_routes(relay_side, client):
    link = {"Authorization": f"Bearer {LINK_TOKEN}", "Accept": "application/json"}
    assert (await client.get("/api/relay/facts", headers=link)).json()["ours"] is True
    body = {"workspace": "acme", "public_key": PUBLIC, "names": ["web.acme.it"], "redirects": {}}
    # a person may not ask it: a machine asks it by itself, through its link
    assert (await client.post("/api/relay/hosted", headers=POST, json=body)).status_code == 403
    resp = await client.post("/api/relay/hosted", headers=link, json=body)
    assert resp.status_code == 200, resp.text
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success" and job["result"]["hosted"]["port"] == 31000
    (action, key), given = relay_side["calls"][0][0], relay_side["calls"][0][1]
    assert (action, key) == ("relay-host", "omnia.acme") and given["link"] == "omnia"
    for bad in ({**body, "public_key": "x"}, {**body, "names": ["*.acme.it"]}, {**body, "workspace": "../x"}):
        assert (await client.post("/api/relay/hosted", headers=link, json=bad)).status_code == 400
    # another machine's tunnel is neither read nor taken away by this one; the machine's admin does both
    relay_side["hosted"]["other.beta"] = {"link": "other", "port": 31001, "listening": True}
    assert set((await client.get("/api/relay/hosted", headers=link)).json()["hosted"]) == {"omnia.acme"}
    assert (await client.post("/api/relay/hosted/other.beta/remove", headers=link)).status_code == 403
    resp = await client.post("/api/relay/hosted/omnia.acme/remove", headers=link)
    assert (await wait_job(client, resp.json()["job"]))["status"] == "success"
    assert (await client.post("/api/relay/hosted/omnia.acme/remove", headers=link)).json() == {"ok": True, "gone": True}
    assert set((await client.get("/api/relay/hosted", headers=POST)).json()["hosted"]) == {"other.beta"}


def test_purge_removes_what_a_relay_leaves():
    text = (relay.__file__.rsplit("/omniacli/", 1)[0] + "/packaging/debian/files/postrm")
    postrm = open(text, encoding="utf-8").read()
    for item in ("/var/lib/omniacli/relays.json", "omniacli-relay-*.service", "/etc/omniacli/relay",
                 "/etc/omniacli/relay-hosted.json", "/var/lib/omniacli-relay", "omniacli-relay-*.conf",
                 "userdel omniacli-relay", "userdel omniacli-tunnel"):
        assert item in postrm, item
    assert re.search(r"sed -i .*OmniaCLI relay", postrm)
