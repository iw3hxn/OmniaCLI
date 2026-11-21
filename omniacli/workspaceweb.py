# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The nginx site of a workspace, from validated names and ports only (the
root helper writes it: workspacehelper.web), and the names other sites of
the machine's nginx already answer on (nginx -T), which a workspace may not
take."""

from __future__ import annotations

import ipaddress
import json
import re
import socket
import subprocess

# the challenges of Let's Encrypt (certbot --webroot), root's
ACME_ROOT = "/var/lib/omniacli-acme"
# an upload to Odoo (an attachment, a database restore is not done here)
MAX_BODY = "200m"
# Odoo's longest requests (a report, an import) before nginx gives up
TIMEOUT = "720s"
# server_names_hash_bucket_size: names up to about 250 characters
HASH_BUCKET = 256
# the path of the Log in as links (loginas.LOGIN_PATH), passed to OmniaCLI
LOGIN_PATH = "/.omniacli/login"
# the addresses of deploy on push (deployhooks.py), passed to OmniaCLI too:
# a repository's webhook reaches it on the instance's own name
HOOKS_PATH = "/.omniacli/hooks/"
# next to the servers file: the names of the instances whose nginx is not a
# workspace's, written by the service for root's timer (workspace-helper
# login-path sync all), which keeps the paths above in their sites
LOGIN_HOSTS_FILE = "login-hosts.json"

_HEADERS = (
    ("Host", "$host"),
    ("X-Forwarded-Host", "$host"),
    ("X-Forwarded-For", "$proxy_add_x_forwarded_for"),
    ("X-Forwarded-Proto", "$scheme"),
    ("X-Real-IP", "$remote_addr"),
)


def _headers(indent: str) -> list[str]:
    return [f"{indent}proxy_set_header {name} {value};" for name, value in _HEADERS]


def conf_text(workspace: str, sites: list[dict], tls: tuple[str, str] | None, ipv6: bool,
              omniacli: str | None = None, redirects: list[dict] = ()) -> str:
    """sites: [{instance, role, names, aliases (optional), http_port,
    websocket_port}]; an alias is one more name of its site (UX-008 §6.1).
    Port 80 answers the challenges and sends the rest to https; without a
    certificate yet (tls None) it answers 503, and there is no 443.
    redirects: [{names, to}], names that send to https://<to> with a 301 and
    the same path, from http (straight there) and from https; a server
    block of their own, answering the challenges of their names on port 80.
    omniacli: the address OmniaCLI listens on (127.0.0.1:8090): each name
    passes it LOGIN_PATH, the links of Log in as (loginas.py), and HOOKS_PATH,
    the webhooks of deploy on push (deployhooks.py)."""
    names = [n for site in sites for n in (*site["names"], *site.get("aliases", ()))]
    out = [f"# written by omniacli workspace-helper: the names of workspace {workspace}.",
           "# Changed here, it is written again at the workspace's next change.", ""]
    listen80 = ["    listen 80;"] + (["    listen [::]:80;"] if ipv6 else [])
    challenges = ["    location /.well-known/acme-challenge/ {", f"        root {ACME_ROOT};", "    }"]
    out += ["server {", *listen80, f"    server_name {' '.join(names)};", *challenges,
            "    location / {",
            "        return 301 https://$host$request_uri;" if tls else "        return 503;",
            "    }", "}"]
    for redirect in redirects:
        out += ["", f"# {' '.join(redirect['names'])} → {redirect['to']}", "server {", *listen80,
                f"    server_name {' '.join(redirect['names'])};", *challenges,
                "    location / {",
                f"        return 301 https://{redirect['to']}$request_uri;" if tls else "        return 503;",
                "    }", "}"]
    if tls is None:
        return "\n".join(out) + "\n"
    listen443 = ["    listen 443 ssl;"] + (["    listen [::]:443 ssl;"] if ipv6 else [])
    for redirect in redirects:
        out += ["", f"# {' '.join(redirect['names'])} → {redirect['to']}", "server {", *listen443,
                f"    server_name {' '.join(redirect['names'])};",
                f"    ssl_certificate {tls[0]};", f"    ssl_certificate_key {tls[1]};",
                "    location / {", f"        return 301 https://{redirect['to']}$request_uri;", "    }", "}"]
    for site in sites:
        # Odoo threaded (workers = 0) answers its websocket on its http port
        ws_port = site["websocket_port"] or site["http_port"]
        out += ["", f"# {site['instance']} {site['role']}", "server {", *listen443,
                f"    server_name {' '.join((*site['names'], *site.get('aliases', ())))};",
                f"    ssl_certificate {tls[0]};", f"    ssl_certificate_key {tls[1]};",
                f"    client_max_body_size {MAX_BODY};",
                f"    proxy_read_timeout {TIMEOUT};", f"    proxy_send_timeout {TIMEOUT};",
                "    location /websocket {",
                f"        proxy_pass http://127.0.0.1:{ws_port};",
                "        proxy_http_version 1.1;",
                # a location's proxy_set_header replaces the server's: all again
                *_headers("        "),
                "        proxy_set_header Upgrade $http_upgrade;",
                '        proxy_set_header Connection "upgrade";',
                "    }",
                *([f"    location = {LOGIN_PATH} {{",
                   f"        proxy_pass http://{omniacli};",
                   *_headers("        "),
                   "    }",
                   f"    location ^~ {HOOKS_PATH} {{",
                   f"        proxy_pass http://{omniacli};",
                   *_headers("        "),
                   "    }"] if omniacli else []),
                "    location / {",
                f"        proxy_pass http://127.0.0.1:{site['http_port']};",
                *_headers("        "),
                "        proxy_redirect off;",
                "    }", "}"]
    return "\n".join(out) + "\n"


# --- the customers' portal: OmniaCLI's own panel on a name of the machine
# (Options › Customers' portal), so that the users of the workspaces log in
# from the internet; the machine's people keep coming through their link

# the name of the portal where the helper keeps it: no workspace is named so
# (WORKSPACE_NAME has no _), so its site, certificate and self-signed
# certificate never meet a workspace's (omniacli-<workspace>.conf)
PORTAL = "_portal"
# the largest request: a backup uploaded from the browser or an MCP client
# (uploads.MAX_SIZE, 50 GiB), streamed to OmniaCLI, not kept by nginx first
PORTAL_MAX_BODY = "50g"
# the live pages (consoles, logs, a job's lines) keep their websocket open
PORTAL_TIMEOUT = "3600s"
# the login and the invitations' form: tries a minute from one address
# (POST only), then 429; OmniaCLI's own limit per address and name follows
PORTAL_LOGIN_RATE = "10r/m"
PORTAL_LOGIN_BURST = 5
PORTAL_HSTS = "max-age=31536000"

# the panel is the edge here: the address and the scheme are nginx's own,
# never one a client sent (uvicorn trusts them from 127.0.0.1 only: cli.py)
_PORTAL_HEADERS = (
    ("Host", "$host"),
    ("X-Forwarded-Host", "$host"),
    ("X-Forwarded-For", "$remote_addr"),
    ("X-Forwarded-Proto", "https"),
    ("X-Real-IP", "$remote_addr"),
)


def portal_conf_text(host: str, tls: tuple[str, str] | None, ipv6: bool, omniacli: str) -> str:
    """The portal's site: port 80 answers Let's Encrypt's challenges and
    sends the rest to https; 443 passes the whole panel to OmniaCLI (omniacli:
    host:port, omniacli_address), its websockets too, with HSTS, the body of
    a backup upload and a limit on the login form. Without a certificate
    (tls None) port 80 answers 503 and there is no 443."""
    listen80 = ["    listen 80;"] + (["    listen [::]:80;"] if ipv6 else [])
    out = ["# written by omniacli workspace-helper: the customers' portal, OmniaCLI's own panel.",
           "# Changed here, it is written again when the portal's address is published (Options).", "",
           "server {", *listen80, f"    server_name {host};",
           "    location /.well-known/acme-challenge/ {", f"        root {ACME_ROOT};", "    }",
           "    location / {", "        return 301 https://$host$request_uri;" if tls else "        return 503;",
           "    }", "}"]
    if tls is None:
        return "\n".join(out) + "\n"

    def passed(indent: str) -> list[str]:
        return [f"{indent}proxy_pass http://{omniacli};",
                *(f"{indent}proxy_set_header {name} {value};" for name, value in _PORTAL_HEADERS)]

    form = [f"        limit_req zone=omniacli_portal_login burst={PORTAL_LOGIN_BURST} nodelay;",
            "        limit_req_status 429;", *passed("        ")]
    listen443 = ["    listen 443 ssl;"] + (["    listen [::]:443 ssl;"] if ipv6 else [])
    out = [*out[:3],
           # http context (conf.d is read inside http): the key is empty, so not
           # counted, but for a form sent
           "map $request_method $omniacli_portal_form {", "    POST $binary_remote_addr;", '    default "";', "}",
           f"limit_req_zone $omniacli_portal_form zone=omniacli_portal_login:1m rate={PORTAL_LOGIN_RATE};",
           "map $http_upgrade $omniacli_portal_upgrade {", "    default upgrade;", '    "" "";', "}", "",
           *out[3:], "",
           "server {", *listen443, f"    server_name {host};",
           f"    ssl_certificate {tls[0]};", f"    ssl_certificate_key {tls[1]};",
           "    ssl_protocols TLSv1.2 TLSv1.3;",
           f"    client_max_body_size {PORTAL_MAX_BODY};",
           "    proxy_request_buffering off;",
           f"    proxy_read_timeout {PORTAL_TIMEOUT};", f"    proxy_send_timeout {PORTAL_TIMEOUT};",
           # one HSTS, nginx's, on its own errors (502) too
           "    proxy_hide_header Strict-Transport-Security;",
           f'    add_header Strict-Transport-Security "{PORTAL_HSTS}" always;',
           "    location = /login {", *form, "    }",
           "    location ^~ /invite/ {", *form, "    }",
           "    location / {", *passed("        "),
           "        proxy_http_version 1.1;",
           "        proxy_set_header Upgrade $http_upgrade;",
           "        proxy_set_header Connection $omniacli_portal_upgrade;",
           # a job's lines and a log come as they are written
           "        proxy_buffering off;",
           "        proxy_redirect off;",
           "    }", "}"]
    return "\n".join(out) + "\n"


def portal_name_in(text: str) -> str:
    """The name the portal's site answers on (its first server_name), "" for none."""
    match = _SERVER_NAME.search(text or "")
    return match.group(1).split()[0] if match and match.group(1).split() else ""


_FILE = re.compile(r"^# configuration file (.+):$")
_SERVER_NAME = re.compile(r"(?:^|[;{}\s])server_name\s+([^;]*);")


def names_elsewhere(dump: str, own_file: str) -> dict[str, str]:
    """The server names of nginx -T's dump, outside own_file: {name: file}.
    Regular expressions (~...) are left out: they cannot be compared."""
    found: dict[str, str] = {}
    current = ""
    chunks: dict[str, list[str]] = {}
    for line in dump.splitlines():
        match = _FILE.match(line)
        if match:
            current = match.group(1)
            continue
        chunks.setdefault(current, []).append(line.split("#", 1)[0])
    for path, lines in chunks.items():
        if path == own_file:
            continue
        for match in _SERVER_NAME.finditer("\n".join(lines)):
            for name in match.group(1).split():
                name = name.lower().strip("\"'")
                if name and name != "_" and not name.startswith("~"):
                    found.setdefault(name, path)
    return found


def clashes(names: list[str], elsewhere: dict[str, str]) -> list[str]:
    """The names another site takes: the same name, or a wildcard of it
    (*.acme.it, .acme.it, www.*)."""
    return [f"{name} ({other} in {path})" for name in names for other, path in takers(name, elsewhere)]


def takers(name: str, elsewhere: dict[str, str]) -> list[tuple[str, str]]:
    """(server name, file) of the other sites that answer on name."""
    found = []
    for other, path in elsewhere.items():
        if other.startswith("*."):
            hit = name.endswith(other[1:])
        elif other.startswith("."):
            hit = name == other[1:] or name.endswith(other)
        elif other.endswith(".*"):
            hit = name.startswith(other[:-1])
        else:
            hit = name == other
        if hit:
            found.append((other, path))
    return found


def addresses_of(host: str) -> list[str]:
    """The addresses DNS gives host (A and AAAA), sorted; none: no record."""
    try:
        found = socket.getaddrinfo(host, 80, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        return []
    return sorted({info[4][0] for info in found})


def machine_addresses(ip: str | None) -> set[str]:
    """The public addresses of this machine: those of its interfaces (ip:
    the path of ip, its addr) and the IPv4 it goes out with (a UDP connect
    sends nothing); none behind NAT, where the public address is the router's."""
    found = set()
    try:
        done = subprocess.run([ip, "-j", "addr", "show"], capture_output=True, text=True, timeout=5) if ip else None
        for link in json.loads(done.stdout or "[]") if done else []:
            for info in link.get("addr_info") or []:
                found.add(str(info.get("local") or ""))
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 9))
            found.add(sock.getsockname()[0])
    except OSError:
        pass
    public = set()
    for address in found:
        try:
            if ipaddress.ip_address(address).is_global:
                public.add(address)
        except ValueError:
            continue
    return public


def dns_state(found: list[str], here: set[str]) -> str:
    """none: no record; here: one of its addresses is this machine's;
    elsewhere: none is (this machine's public addresses known); unknown: it
    has a record, this machine's public address is not known (behind NAT)."""
    if not found:
        return "none"
    if set(found) & here:
        return "here"
    return "elsewhere" if here else "unknown"


def workers(conf: str) -> int:
    """workers of an odoo.conf, 0 when not set."""
    for line in conf.splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "workers" and value.strip().isdigit():
            return int(value.strip())
    return 0
