# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Names published through another machine (README, Published through
another machine): a machine behind NAT (a customer's office server, no port
open to it) has its instances answer at their names on a machine of the
integrator with a public address, the relay, without opening a port.

The machine behind NAT opens the tunnel itself, outward: an SSH connection to
the relay's sshd, as the relay's user omniacli-relay, with a remote forward
(ssh -R) of one port of the relay's loopback to one port of its own loopback,
kept up by a systemd unit (Restart=always). Why SSH and not WireGuard: sshd
is already on every machine OmniaCLI prepares (its links come through it),
the relay's side is one line of authorized_keys, no kernel module, no new
port open on the relay, no address plan, nothing to route; a WireGuard peer
would need a UDP port open on the relay, keys and addresses on both sides
and a route back. What SSH costs: TCP inside TCP (a lossy line is slower
than with WireGuard), and a drop is noticed by the keepalives
(ServerAliveInterval) within about 30 s, then the unit starts ssh again 5 s
later.

On the relay: one key per tunnel in the authorized_keys of omniacli-relay,
root's, each line restrict,port-forwarding,permitlisten="127.0.0.1:<its
port>",command="/bin/false": that key may listen on that one port of the
loopback and do nothing else (no shell, no command, no -L, no agent); a Match
block of sshd_config says the same for the user. The relay's nginx serves the
names (Let's Encrypt with HTTP-01 when they lead to it, a self-signed
certificate until then) and passes every request, /websocket too, to that
port, with the visitor's address in X-Real-IP.

On the machine behind NAT: the workspace's nginx site has a server block
listening on 127.0.0.1:<local port> for the names published through the
relay; it trusts X-Real-IP only from 127.0.0.1 (set_real_ip_from), where
the tunnel comes in, and passes the request on to Odoo as its own blocks do.

Here: the texts (the line of authorized_keys, the Match block, the relay's
site, the unit) and the readings, from validated values only; the root
helper writes them (workspacehelper, relay-*)."""

from __future__ import annotations

import datetime
import ipaddress
import json
import os
import re
import subprocess
import time

from .config import DOMAIN_NAME, WORKSPACE_NAME

# the user of the relay the tunnels log in as: a system user without a shell
RELAY_USER = "omniacli-relay"
# the user the tunnels run as on the machine behind NAT: a system user without a shell nor a home
# (DynamicUser would need nss-systemd, which not every machine has: ssh asks who it runs as)
TUNNEL_USER = "omniacli-tunnel"
# its home, root's: its .ssh/authorized_keys too (sshd accepts a file of root's)
RELAY_HOME = "/var/lib/omniacli-relay"
# the tunnels hosted on the relay, root's: {key: {link, workspace, public_key, port, names, redirects, created}}
HOSTED_FILE = "/etc/omniacli/relay-hosted.json"
# the ports of the relay's loopback a tunnel listens on (not the instances' 20000-29999)
RELAY_PORTS = range(31000, 32000)
# the ports of the loopback of the machine behind NAT where its nginx takes the tunnel's requests
LOCAL_PORTS = range(30000, 30500)
# on the machine behind NAT: /etc/omniacli/relay/<workspace>/ (its key, root's 600; known_hosts; port)
TUNNEL_ROOT = "/etc/omniacli/relay"
SSH_CLIENT = "/usr/bin/ssh"
# a tunnel's ssh alive for this long is connected: a refused key, a port taken on the relay
# (ExitOnForwardFailure) or no answer (ConnectTimeout 10) end it well before
CONNECTED_AFTER = 20
RESTART_SECONDS = 5
ALIVE_SECONDS, ALIVE_COUNT = 10, 3
# the marks of the Match block the helper adds to sshd_config and takes away with the last tunnel
SSHD_START = "# OmniaCLI relay: omniacli-relay may only listen on the ports of its tunnels"
SSHD_END = "# end of OmniaCLI relay"
# names one publication may give the relay
MAX_NAMES = 200
PUBLIC_KEY = re.compile(r"ssh-ed25519 [A-Za-z0-9+/]{68}")
HOST_KEY = re.compile(r"(ssh-ed25519|ecdsa-sha2-nistp256|ecdsa-sha2-nistp384|ecdsa-sha2-nistp521|ssh-rsa) "
                      r"[A-Za-z0-9+/]{40,1024}={0,3}")
# a tunnel's name on the relay: <the caller's link name, made a label>.<its workspace>
KEY = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?\.[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?")
# the headers the relay sets: the visitor's address its own, never one a client sent
RELAY_HEADERS = (
    ("Host", "$host"),
    ("X-Forwarded-Host", "$host"),
    ("X-Forwarded-For", "$remote_addr"),
    ("X-Forwarded-Proto", "https"),
    ("X-Real-IP", "$remote_addr"),
)
# the headers of the block of the machine behind NAT: $remote_addr is the visitor's, read from X-Real-IP
NAT_HEADERS = (
    ("Host", "$host"),
    ("X-Forwarded-Host", "$host"),
    ("X-Forwarded-For", "$remote_addr"),
    ("X-Forwarded-Proto", "https"),
    ("X-Real-IP", "$remote_addr"),
)


class RelayError(Exception):
    """A value refused, with the reason."""


def key_of(link: str, workspace: str) -> str:
    """The tunnel's name on the relay: the link the machine behind NAT comes
    through there, made a label, and its workspace."""
    label = re.sub(r"[^a-z0-9]+", "-", link.lower()).strip("-")[:64].strip("-") or "machine"
    key = f"{label}.{workspace}"
    if not WORKSPACE_NAME.fullmatch(workspace) or not KEY.fullmatch(key):
        raise RelayError(f"{link!r}, {workspace!r} give no name of a tunnel")
    return key


def check_key(key: str) -> str:
    if not KEY.fullmatch(key or ""):
        raise RelayError(f"{key!r} is not the name of a tunnel")
    return key


def check_public_key(value) -> str:
    """The tunnel's key, "ssh-ed25519 <base64>" (its comment left out)."""
    text = " ".join(str(value or "").split()[:2])
    if not PUBLIC_KEY.fullmatch(text):
        raise RelayError("the tunnel's key is not an ssh-ed25519 public key")
    return text


def check_host_key(value) -> str:
    text = " ".join(str(value or "").split()[:2])
    if not HOST_KEY.fullmatch(text):
        raise RelayError("the relay's host key is not an SSH public key")
    return text


def check_host(value) -> str:
    """An address the tunnel goes to: an IP or a host name."""
    host = str(value or "").strip().strip("[]").lower()
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    if not DOMAIN_NAME.fullmatch(host):
        raise RelayError(f"{host!r} is not an address of the relay")
    return host


def check_port(value, ports: range | None = None) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise RelayError(f"{value!r} is not a port") from None
    if isinstance(value, bool) or not 1 <= port <= 65535 or (ports is not None and port not in ports):
        raise RelayError(f"{value!r} is not a port of {ports.start}-{ports.stop - 1}" if ports else f"{value!r} is not a port")
    return port


def authorized_line(public_key: str, port: int, key: str) -> str:
    """The line of the relay's authorized_keys for a tunnel: listening on
    127.0.0.1:<port> and nothing else (no shell, no command, no pty, no
    forward towards anything, no agent)."""
    return (f'restrict,port-forwarding,permitlisten="127.0.0.1:{check_port(port, RELAY_PORTS)}",'
            f'command="/bin/false" {check_public_key(public_key)} omniacli-relay:{check_key(key)}')


def authorized_keys(hosted: dict, agents: dict | None = None) -> str:
    """The relay's authorized_keys: the tunnels of the names (hosted) and of
    the agents' SSH (agents, AD-017 §17.6), one line each, its port only."""
    every = [*hosted.values(), *(agents or {}).values()]
    ports = [int(r.get("port") or 0) for r in every]
    keys = [check_public_key(r.get("public_key")) for r in every]
    # two lines that may listen on one port: either machine could take the other's visitors; one key twice: sshd
    # reads the first line, the other tunnel is lost
    if len(set(ports)) != len(ports):
        raise RelayError(f"two tunnels on one port ({', '.join(str(p) for p in sorted({p for p in ports if ports.count(p) > 1}))})")
    if len(set(keys)) != len(keys):
        raise RelayError("two tunnels with one key")
    out = ["# written by omniacli workspace-helper: the tunnels of the machines published through this one.",
           "# Changed here, it is written again at the next publication."]
    for key in sorted(hosted):
        rec = hosted[key]
        out.append(authorized_line(rec["public_key"], rec["port"], key))
    for label in sorted(agents or {}):
        rec = agents[label]
        out.append(authorized_line(rec["public_key"], rec["port"], f"{check_label(label)}.{AGENT_SUFFIX}"))
    return "\n".join(out) + "\n"


# --- the agent's SSH through the relay (AD-017 §17.6) -------------------------------------------------------
# A machine behind NAT managed from a central panel: its tunnel (its own key, its own permitlisten line, the
# same user omniacli-relay) carries its sshd to a port of the relay's loopback; the central reaches that port
# with ssh -W as the relay's second user, omniacli-relay-jump, whose key may open the ports of the machines that
# let it in and nothing else, and goes on to the machine's sshd as its link user, end to end: the relay passes
# SSH it cannot read (the link's token and pages never in clear there).

# the relay's user the central panels come in as: a direct-tcpip to the agents' ports, nothing else
JUMP_USER = "omniacli-relay-jump"
JUMP_HOME = "/var/lib/omniacli-relay-jump"
# the agents' tunnels hosted on the relay, root's: {label: {link, public_key, port, allowed, created}}
AGENTS_FILE = "/etc/omniacli/relay-agents.json"
# the comment of an agent's line in authorized_keys (omniacli-relay:<label>.agent)
AGENT_SUFFIX = "agent"
# a machine's label on the relay: its link's name made one
LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")
# on the machine behind NAT: its tunnel's key (600, the tunnels' user's) and the relay's host key
AGENT_ROOT = "/etc/omniacli/agent-relay"
AGENT_UNIT = "omniacli-agent-relay.service"
# its record, the service's, next to the servers file: {link, label, host, ssh_port, remote_port, local_port,
# allowed, published, restarts_base}
AGENT_RECORD = "agent-relay.json"
# the user of the machine whose keys (the links of the panels that operate it through SSH) come through the relay
LINK_USER = "omniacli-link"
# central panels one machine lets in through its tunnel
MAX_ALLOWED = 20


def label_of(link: str) -> str:
    """A machine's label on the relay: the link it comes through there, made a label."""
    label = re.sub(r"[^a-z0-9]+", "-", link.lower()).strip("-")[:64].strip("-")
    return check_label(label or "machine")


def check_label(label: str) -> str:
    if not LABEL.fullmatch(label or ""):
        raise RelayError(f"{label!r} is not the name of a machine on the relay")
    return label


def link_keys(text: str) -> list[str]:
    """The keys of the link user's authorized_keys that may open a forward
    (permitopen): the central panels that operate this machine through SSH.
    A line without permitopen (a person's key) is not one of them."""
    found = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "permitopen=" not in line:
            continue
        match = PUBLIC_KEY.search(line)
        if match and match.group(0) not in found:
            found.append(match.group(0))
    return found


def check_allowed(value) -> list[str]:
    """The keys a machine lets in through its tunnel: ed25519 public keys, one at least."""
    if not isinstance(value, list) or not value or len(value) > MAX_ALLOWED:
        raise RelayError(f"allowed: the public keys of the central panels, 1 to {MAX_ALLOWED}")
    return list(dict.fromkeys(check_public_key(k) for k in value))


def jump_keys(agents: dict) -> str:
    """The authorized_keys of omniacli-relay-jump: one line a key (sshd
    reads the first that matches), with a permitopen for the port of each
    machine that lets that key in, and nothing else (no shell, command, pty,
    agent, -R)."""
    ports: dict[str, list[int]] = {}
    for label in sorted(agents):
        rec = agents[label]
        port = check_port(rec["port"], RELAY_PORTS)
        for key in rec.get("allowed") or []:
            opened = ports.setdefault(check_public_key(key), [])
            if port not in opened:
                opened.append(port)
    out = ["# written by omniacli workspace-helper: the central panels that reach a machine behind NAT through "
           "this one.", "# Changed here, it is written again at the next publication of such a machine."]
    for key in sorted(ports):
        opens = ",".join(f'permitopen="127.0.0.1:{p}"' for p in sorted(ports[key]))
        out.append(f'restrict,port-forwarding,{opens},command="/bin/false" {key} {JUMP_USER}')
    return "\n".join(out) + "\n"


def agent_ports(agents: dict) -> list[int]:
    """The ports the central panels may open: those of the agents that let a key in."""
    return sorted({check_port(r["port"], RELAY_PORTS) for r in agents.values() if r.get("allowed")})


def agent_unit_text(relay: str, host: str, ssh_port: int, remote_port: int, local_port: int, folder: str) -> str:
    """The unit that keeps the agent's tunnel up: ssh -R from its port of the
    relay's loopback to this machine's sshd (local_port), as the tunnels' user."""
    return _tunnel_unit(f"this machine's SSH (its OmniaCLI for the central panel) published through {relay}",
                        f"the tunnel of this machine's SSH to {relay}", relay, host, ssh_port, remote_port,
                        check_port(local_port), folder)


def sshd_block(open_ports=()) -> str:
    """The Match blocks of sshd_config for the relay's users: omniacli-relay
    remote forwards only (to the loopback: GatewayPorts no), with the
    keepalives that free the port of a tunnel whose machine went away; and,
    while a machine lets central panels in, omniacli-relay-jump local
    forwards to those ports only (PermitOpen: with the key's permitopen, both
    must allow it), nothing else."""
    jump = []
    if open_ports:
        jump = [
            f"Match User {JUMP_USER}",
            "    AllowTcpForwarding local",
            "    PermitOpen " + " ".join(f"127.0.0.1:{check_port(p, RELAY_PORTS)}" for p in open_ports),
            "    PermitListen none",
            "    GatewayPorts no",
            "    ForceCommand /bin/false",
            "    PermitTTY no",
            "    X11Forwarding no",
            "    AllowAgentForwarding no",
            "    AllowStreamLocalForwarding no",
            "    PermitTunnel no",
            "    PasswordAuthentication no",
            "    KbdInteractiveAuthentication no",
            f"    ClientAliveInterval {ALIVE_SECONDS}",
            f"    ClientAliveCountMax {ALIVE_COUNT}",
        ]
    return "\n".join([
        SSHD_START,
        f"Match User {RELAY_USER}",
        "    AllowTcpForwarding remote",
        "    GatewayPorts no",
        "    PermitOpen none",
        "    ForceCommand /bin/false",
        "    PermitTTY no",
        "    X11Forwarding no",
        "    AllowAgentForwarding no",
        "    AllowStreamLocalForwarding no",
        "    PermitTunnel no",
        "    PasswordAuthentication no",
        "    KbdInteractiveAuthentication no",
        f"    ClientAliveInterval {ALIVE_SECONDS}",
        f"    ClientAliveCountMax {ALIVE_COUNT}",
        *jump,
        SSHD_END,
    ])


def without_block(text: str) -> str:
    """sshd_config without the relay's Match block."""
    if SSHD_START not in text:
        return text
    head, _, rest = text.partition(SSHD_START)
    _, _, tail = rest.partition(SSHD_END)
    return head.rstrip("\n") + "\n" + tail.lstrip("\n")


def with_block(text: str, open_ports=()) -> str:
    """sshd_config with the relay's Match blocks, at the end (a Match block lasts to the next one)."""
    return without_block(text).rstrip("\n") + "\n\n" + sshd_block(open_ports) + "\n"


def _passed(indent: str, port: int) -> list[str]:
    return [f"{indent}proxy_pass http://127.0.0.1:{port};",
            *(f"{indent}proxy_set_header {name} {value};" for name, value in RELAY_HEADERS)]


def relay_site_text(key: str, names: list[str], redirects: dict[str, str], port: int,
                    tls: tuple[str, str] | None, ipv6: bool, acme_root: str, max_body: str, timeout: str) -> str:
    """The relay's nginx site of one tunnel: port 80 answers Let's Encrypt's
    challenges and sends the rest to https; 443 passes the names to the
    tunnel's port (/websocket upgraded), the redirects answer 301. Without a
    certificate (tls None) port 80 answers 503 and there is no 443."""
    listen80 = ["    listen 80;"] + (["    listen [::]:80;"] if ipv6 else [])
    listen443 = ["    listen 443 ssl;"] + (["    listen [::]:443 ssl;"] if ipv6 else [])
    every = [*names, *(n for n in redirects if n not in names)]
    out = [f"# written by omniacli workspace-helper: names published through this machine (tunnel {key}).",
           "# Changed here, it is written again at the next publication of the machine behind it.", "",
           "server {", *listen80, f"    server_name {' '.join(every)};",
           "    location /.well-known/acme-challenge/ {", f"        root {acme_root};", "    }",
           "    location / {", "        return 301 https://$host$request_uri;" if tls else "        return 503;",
           "    }", "}"]
    if tls is None:
        return "\n".join(out) + "\n"
    by_target: dict[str, list[str]] = {}
    for name, to in redirects.items():
        by_target.setdefault(to, []).append(name)
    for to, sent in by_target.items():
        out += ["", f"# {' '.join(sent)} → {to}", "server {", *listen443, f"    server_name {' '.join(sent)};",
                f"    ssl_certificate {tls[0]};", f"    ssl_certificate_key {tls[1]};",
                "    location / {", f"        return 301 https://{to}$request_uri;", "    }", "}"]
    if names:
        out += ["", f"# through tunnel {key}, 127.0.0.1:{port}", "server {", *listen443,
                f"    server_name {' '.join(names)};",
                f"    ssl_certificate {tls[0]};", f"    ssl_certificate_key {tls[1]};",
                f"    client_max_body_size {max_body};",
                f"    proxy_read_timeout {timeout};", f"    proxy_send_timeout {timeout};",
                "    location /websocket {", *_passed("        ", port),
                "        proxy_http_version 1.1;",
                "        proxy_set_header Upgrade $http_upgrade;",
                '        proxy_set_header Connection "upgrade";',
                "    }",
                "    location / {", *_passed("        ", port),
                "        proxy_redirect off;",
                "    }", "}"]
    return "\n".join(out) + "\n"


def unit_name(workspace: str) -> str:
    if not WORKSPACE_NAME.fullmatch(workspace or ""):
        raise RelayError(f"{workspace!r} is not a workspace name")
    return f"omniacli-relay-{workspace}.service"


def known_hosts_line(host: str, port: int, host_key: str) -> str:
    name = check_host(host)
    if port != 22:
        name = f"[{name}]:{port}"
    return f"{name} {check_host_key(host_key)}\n"


def tunnel_unit_text(workspace: str, relay: str, host: str, ssh_port: int, remote_port: int, local_port: int,
                     folder: str) -> str:
    """The unit that keeps the tunnel of a workspace up: ssh -N -R as the
    tunnels' user (TUNNEL_USER), with its key (<folder>/id_ed25519, 600, that
    user's: LoadCredential would keep it root's, but not every machine gives
    the units their credentials, a container does not), the relay's host key only
    (StrictHostKeyChecking with its own known_hosts), started again
    RESTART_SECONDS after any end, without a limit."""
    unit_name(workspace)
    return _tunnel_unit(f"the names of workspace {workspace} published through {relay}",
                        f"the tunnel of workspace {workspace} to {relay}", relay, host, ssh_port, remote_port,
                        check_port(local_port, LOCAL_PORTS), folder)


def _tunnel_unit(description: str, what: str, relay: str, host: str, ssh_port: int, remote_port: int,
                 local_port: int, folder: str) -> str:
    host = check_host(host)
    ssh_port, remote_port = check_port(ssh_port), check_port(remote_port, RELAY_PORTS)
    if not re.fullmatch(r"[A-Za-z0-9._@-]{1,64}", relay):
        raise RelayError(f"{relay!r} is not the name of a link")
    target = f"[{host}]" if ":" in host else host
    options = ["-F", "/dev/null", "-N", "-T", "-i", f"{folder}/id_ed25519",
               "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
               "-o", f"UserKnownHostsFile={folder}/known_hosts", "-o", "GlobalKnownHostsFile=/dev/null",
               "-o", "UpdateHostKeys=no", "-o", "ExitOnForwardFailure=yes",
               "-o", f"ServerAliveInterval={ALIVE_SECONDS}", "-o", f"ServerAliveCountMax={ALIVE_COUNT}",
               "-o", "ConnectTimeout=10", "-o", "TCPKeepAlive=yes", "-p", str(ssh_port),
               "-R", f"127.0.0.1:{remote_port}:127.0.0.1:{local_port}", f"{RELAY_USER}@{target}"]
    return "\n".join([
        f"# written by omniacli workspace-helper: {what}.",
        "[Unit]",
        f"Description=OmniaCLI: {description}",
        "After=network-online.target",
        "Wants=network-online.target",
        "StartLimitIntervalSec=0",
        "",
        "[Service]",
        "Type=simple",
        f"User={TUNNEL_USER}",
        f"Group={TUNNEL_USER}",
        f"ExecStart={SSH_CLIENT} " + " ".join(options),
        "Restart=always",
        f"RestartSec={RESTART_SECONDS}",
        "NoNewPrivileges=yes",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "PrivateTmp=yes",
        "PrivateDevices=yes",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
        ""])


def cloudflare_tunnel_unit_text(workspace: str, local_port: int) -> str:
    """The other way to publish the names of a machine behind NAT, not built:
    Cloudflare Tunnel (cloudflared), for whoever has a Cloudflare account and
    its zone there. It would go here, beside tunnel_unit_text: a unit running
    `cloudflared tunnel run --token-file <the tunnel's token, root's 600>`
    as TUNNEL_USER, its ingress sending the workspace's names to
    http://127.0.0.1:<local_port> — the same block of the workspace's site
    the relay's tunnel reaches (set_real_ip_from 127.0.0.1, the visitor's
    address in the header CF-Connecting-IP instead of X-Real-IP) —, the token
    and the DNS records made with Cloudflare's API from a token in Git
    credentials. The choice «Pubblica attraverso…» would list it beside the
    relays. README, Published through another machine."""
    raise NotImplementedError("Cloudflare Tunnel is not built: publish through a relay (another machine)")


# --- reading a tunnel ---------------------------------------------------------

def _stamp(value: str) -> str | None:
    """A timestamp of systemctl show ("Mon 2026-11-23 10:20:30 CET") as ISO, None when unset."""
    value = (value or "").strip()
    if not value or value in ("n/a", "0"):
        return None
    parts = value.split()
    try:
        found = datetime.datetime.strptime(" ".join(parts[1:3]), "%Y-%m-%d %H:%M:%S")
    except (ValueError, IndexError):
        return None
    return found.isoformat(timespec="seconds")


SHOWN = ("LoadState", "ActiveState", "SubState", "ExecMainPID", "ExecMainStartTimestamp",
         "ExecMainStartTimestampMonotonic", "ExecMainExitTimestamp", "NRestarts")


def tunnel_state(show: str, now_monotonic_us: int, baseline: int = 0) -> dict:
    """The tunnel as systemctl show of its unit says it: {state: connected |
    connecting | down | none, since, last_drop, restarts, nrestarts}.
    connected: its ssh has run for CONNECTED_AFTER seconds (a refused key, a
    port taken on the relay or no answer end it well before); since: when
    that ssh started; restarts: the automatic restarts since the
    publication (systemd's NRestarts less baseline, its count then); last_drop:
    when the last one ended (the unit starts ssh again RESTART_SECONDS after
    it ends; systemd forgets the end of the run before when the next starts)."""
    values = dict(line.split("=", 1) for line in show.splitlines() if "=" in line)
    if values.get("LoadState") != "loaded":
        return {"state": "none", "since": None, "last_drop": None, "restarts": 0, "nrestarts": 0}
    count = int(values["NRestarts"]) if (values.get("NRestarts") or "").isdigit() else 0
    restarts = max(0, count - baseline)
    running = values.get("ActiveState") == "active" and values.get("SubState") == "running"
    started_us = int(values["ExecMainStartTimestampMonotonic"]) \
        if (values.get("ExecMainStartTimestampMonotonic") or "").isdigit() else 0
    alive = (now_monotonic_us - started_us) / 1e6 if running and started_us else 0
    state = "connected" if running and alive >= CONNECTED_AFTER else "connecting" if running or \
        values.get("ActiveState") in ("activating", "reloading") else "down"
    since = _stamp(values.get("ExecMainStartTimestamp", "")) if running else None
    last = None
    if not running:
        last = _stamp(values.get("ExecMainExitTimestamp", ""))
    elif restarts and since:
        last = (datetime.datetime.fromisoformat(since) - datetime.timedelta(seconds=RESTART_SECONDS)).isoformat(
            timespec="seconds")
    return {"state": state, "since": since, "last_drop": last, "restarts": restarts, "nrestarts": count}


def read_tunnel(workspace: str, baseline: int = 0, unit: str = "") -> dict:
    """tunnel_state of the workspace's unit (or of unit: the agent's), read now (no root needed)."""
    try:
        done = subprocess.run(["systemctl", "show", unit or unit_name(workspace), "-p", ",".join(SHOWN)],
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError, RelayError):
        return {"state": "none", "since": None, "last_drop": None, "restarts": 0, "nrestarts": 0}
    # CLOCK_MONOTONIC, the clock of systemd's *Monotonic values (/proc/uptime counts a suspend too)
    return tunnel_state(done.stdout, int(time.monotonic() * 1e6), baseline)


def listening_ports(texts: list[str]) -> set[int]:
    """The TCP ports something listens on, from /proc/net/tcp and tcp6."""
    found = set()
    for text in texts:
        for line in text.splitlines()[1:]:
            parts = line.split()
            if len(parts) > 3 and parts[3] == "0A":
                try:
                    found.add(int(parts[1].rsplit(":", 1)[1], 16))
                except (ValueError, IndexError):
                    continue
    return found


# --- the record of the machine behind NAT (the service's, next to the servers file) -----------------------

# {workspace: {link, key, host, ssh_port, remote_port, local_port, names, redirects, certificate,
#  certificate_error, published}}: what each workspace's publication through a relay made
RECORDS_FILE = "relays.json"


def load_records(path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict) and WORKSPACE_NAME.fullmatch(k)} \
        if isinstance(data, dict) else {}


def save_records(path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def dns_state(found: list[str], relay_address: str) -> str:
    """Where the DNS leads a name published through a relay: here (to the
    relay), none (no record), elsewhere."""
    if not found:
        return "none"
    return "here" if relay_address in found else "elsewhere"
