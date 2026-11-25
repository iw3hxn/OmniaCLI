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

from . import i18n, relay

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


# what a forward passes to the other machine (AD-012): the visitor's own address, never one the visitor sent, so that
# the machine that receives, trusting it from this machine's address only (trusted_lines), reads the visitor's
_FORWARD_HEADERS = (
    ("Host", "$host"),
    ("X-Forwarded-Host", "$host"),
    ("X-Forwarded-For", "$remote_addr"),
    ("X-Forwarded-Proto", "$scheme"),
    ("X-Real-IP", "$remote_addr"),
)


def _headers(indent: str, headers=_HEADERS) -> list[str]:
    return [f"{indent}proxy_set_header {name} {value};" for name, value in headers]


def trusted_address(value: str) -> str:
    """An address of another machine whose X-Forwarded-For a site trusts: one IP address (a forward of AD-012 comes
    from one machine). ValueError for anything else, a network or the loopback included."""
    ip = ipaddress.ip_address(str(value))
    if ip.is_loopback or ip.is_unspecified or ip.is_multicast:
        raise ValueError(f"{ip} is not the address of another machine")
    return str(ip)


def trusted_lines(trust) -> list[str]:
    """The visitor's address of a request another machine passed here (its forward of a workspace's names, AD-012):
    taken from X-Forwarded-For when the request comes from that machine's address, and from nothing else."""
    found = sorted({trusted_address(a) for a in trust or ()})
    if not found:
        return []
    return [*(f"    set_real_ip_from {a};" for a in found), "    real_ip_header X-Forwarded-For;"]


def _locations(site: dict, headers: list[str], omniacli: str | None) -> list[str]:
    """The locations of an environment's server block: /websocket upgraded
    (to the http port when Odoo is threaded), OmniaCLI's own paths, the rest."""
    ws_port = site["websocket_port"] or site["http_port"]
    return ["    location /websocket {",
            f"        proxy_pass http://127.0.0.1:{ws_port};",
            "        proxy_http_version 1.1;",
            # a location's proxy_set_header replaces the server's: all again
            *headers,
            "        proxy_set_header Upgrade $http_upgrade;",
            '        proxy_set_header Connection "upgrade";',
            "    }",
            *([f"    location = {LOGIN_PATH} {{",
               f"        proxy_pass http://{omniacli};",
               *headers,
               "    }",
               f"    location ^~ {HOOKS_PATH} {{",
               f"        proxy_pass http://{omniacli};",
               *headers,
               "    }"] if omniacli else []),
            "    location / {",
            f"        proxy_pass http://127.0.0.1:{site['http_port']};",
            *headers,
            "        proxy_redirect off;",
            "    }"]


def relay_blocks(sites: list[dict], omniacli: str | None) -> list[str]:
    """The server blocks of the environments published through a relay
    (relay.py; site["relay_port"]): on 127.0.0.1:<that port> only, where the
    tunnel comes in, without TLS (the tunnel is SSH, the relay holds the
    certificate); the visitor's address taken from X-Real-IP, trusted from
    127.0.0.1 only; https for Odoo, as the visitor sees it."""
    out = []
    headers = [f"        proxy_set_header {name} {value};" for name, value in relay.NAT_HEADERS]
    for site in sites:
        port = site.get("relay_port")
        if not port:
            continue
        names = (*site["names"], *site.get("aliases", ()))
        out += ["", f"# {site['instance']} {site['role']} {RELAY_MARK}", "server {",
                f"    listen 127.0.0.1:{int(port)};", f"    server_name {' '.join(names)};",
                "    set_real_ip_from 127.0.0.1;", "    real_ip_header X-Real-IP;",
                f"    client_max_body_size {MAX_BODY};",
                f"    proxy_read_timeout {TIMEOUT};", f"    proxy_send_timeout {TIMEOUT};",
                *(hold_lines() if site.get("hold") else _locations(site, headers, omniacli)), "}"]
    return out


# the mark of an environment's block that takes the requests of its relay's tunnel
RELAY_MARK = "(through its relay)"

# --- a workspace's names passed to another machine (docs/adr/AD-008-consegna-e-spostamento.md, AD-012). acme
# passes the challenges this machine has no token for, so a certificate is made there while the DNS points here;
# full passes everything, from a site of its own (omniacli-forward-<workspace>.conf); hold is the site of the
# machine that receives, until the switch: the challenges, and 503 for the rest

# the named location the challenges not found here go to
FORWARD_LOCATION = "@omniacli_forward"
# the CAs nginx verifies the other machine's certificate with: the system's (Debian's ca-certificates)
SYSTEM_CA = "/etc/ssl/certs/ca-certificates.crt"
# what a held name says (UX-014 A6), on the page of the maintenance
HOLD_TEXT = "The service is moving, come back in a few minutes"
HOLD_MARK = "    # omniacli: held, its names wait for a switch (AD-012)"
_HOST_NAME = re.compile(r"(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]")


def challenge_lines(to: str | None = None) -> list[str]:
    """The challenges of Let's Encrypt (certbot --webroot), from ACME_ROOT;
    to: the address of another machine the challenges not found here are
    asked to (its port 80, the name kept). This machine's own are found here
    first: its renewals do not change."""
    if not to:
        return ["    location /.well-known/acme-challenge/ {", f"        root {ACME_ROOT};", "    }"]
    return ["    location /.well-known/acme-challenge/ {", f"        root {ACME_ROOT};",
            f"        try_files $uri {FORWARD_LOCATION};", "    }",
            f"    location {FORWARD_LOCATION} {{", f"        proxy_pass http://{to};", *_headers("        ", _FORWARD_HEADERS),
            "    }"]


def http_blocks(names: list[str], rest: str, listen80: list[str], acme_to: dict | None = None,
                comment: str = "") -> list[str]:
    """The port 80 server blocks of names: the challenges, rest for the
    other paths; the names of acme_to {"to", "names"} in a block of their
    own, whose challenges not found here go to acme_to["to"]. Each block
    begins with an empty line, then comment."""
    passed = [n for n in names if acme_to and n in acme_to["names"]]
    out = []
    for group, to in (([n for n in names if n not in passed], None), (passed, acme_to["to"] if acme_to else None)):
        if group:
            out += ["", *([comment] if comment else []), "server {", *listen80, f"    server_name {' '.join(group)};",
                    *challenge_lines(to), "    location / {", rest, "    }", "}"]
    return out


def hold_lines() -> list[str]:
    """The location of a held server block: every path answered with the
    page of the maintenance (503, Retry-After, no cache, the reader's
    language), with the text of a move (UX-014 A6); Odoo is not reached."""
    return [HOLD_MARK, "    location / {", *page_lines(HOLD_TEXT), "    }"]


def forward_target(to: str) -> str:
    """The address of the machine names are passed to, as proxy_pass takes
    it: an IP address (IPv6 in brackets) or a host name. ValueError when it
    is neither, or an address that is no other machine's."""
    try:
        ip = ipaddress.ip_address(str(to).strip("[]"))
    except ValueError:
        if not _HOST_NAME.fullmatch(str(to)):
            raise ValueError(f"{to!r} is no IP address nor host name") from None
        return str(to)
    if ip.is_loopback or ip.is_unspecified or ip.is_multicast or ip.is_link_local:
        raise ValueError(f"{ip} is not the address of another machine")
    return f"[{ip}]" if ip.version == 6 else str(ip)


def forward_conf_text(workspace: str, record: dict, tls: tuple[str, str] | None, ipv6: bool) -> str:
    """The site of a forward full (AD-012), record {"to", "names", "moved"}.
    The names: port 80 answers the challenges found here and passes the
    rest, the other challenges too, to the port 80 of "to"; port 443, with
    this machine's certificate of today, passes everything to the 443 of
    "to", its certificate verified for the name asked against the system's
    CAs, the websocket upgraded, the visitor's address in X-Forwarded-For.
    The moved names {old: new} (the provider's, which do not pass): a 301 to
    the new name, from port 80 and from 443."""
    to = forward_target(str(record["to"]))
    names, moved = list(record.get("names") or []), dict(record.get("moved") or {})
    listen80 = ["    listen 80;"] + (["    listen [::]:80;"] if ipv6 else [])
    listen443 = ["    listen 443 ssl;"] + (["    listen [::]:443 ssl;"] if ipv6 else [])
    out = [f"# written by omniacli workspace-helper: the names of workspace {workspace} passed to {to} (AD-012).",
           "# Changed here, it is written again at the forward's next change."]
    if names:
        out += ["", "server {", *listen80, f"    server_name {' '.join(names)};", *challenge_lines(to),
                "    location / {", f"        proxy_pass http://{to};", *_headers("        ", _FORWARD_HEADERS),
                "        proxy_redirect off;", "    }", "}"]
    groups: dict[str, list[str]] = {}
    for old, new in moved.items():
        groups.setdefault(new, []).append(old)
    for new, olds in groups.items():
        out += http_blocks(olds, f"        return 301 https://{new}$request_uri;", listen80, None,
                           f"# {' '.join(olds)} → {new} (moved)")
    if tls is None:
        return "\n".join(out) + "\n"
    tls_lines = [f"    ssl_certificate {tls[0]};", f"    ssl_certificate_key {tls[1]};"]
    passed = [f"        proxy_pass https://{to};", "        proxy_http_version 1.1;",
              "        proxy_ssl_server_name on;", "        proxy_ssl_name $host;", "        proxy_ssl_verify on;",
              "        proxy_ssl_verify_depth 4;", f"        proxy_ssl_trusted_certificate {SYSTEM_CA};",
              *_headers("        ", _FORWARD_HEADERS)]
    if names:
        out += ["", "server {", *listen443, f"    server_name {' '.join(names)};", *tls_lines,
                f"    client_max_body_size {MAX_BODY};",
                f"    proxy_read_timeout {TIMEOUT};", f"    proxy_send_timeout {TIMEOUT};",
                "    location /websocket {", *passed,
                "        proxy_set_header Upgrade $http_upgrade;", '        proxy_set_header Connection "upgrade";', "    }",
                "    location / {", *passed, "        proxy_redirect off;", "    }", "}"]
    for new, olds in groups.items():
        out += ["", f"# {' '.join(olds)} → {new} (moved)", "server {", *listen443, f"    server_name {' '.join(olds)};",
                *tls_lines, "    location / {", f"        return 301 https://{new}$request_uri;", "    }", "}"]
    return "\n".join(out) + "\n"


def conf_text(workspace: str, sites: list[dict], tls: tuple[str, str] | None, ipv6: bool,
              omniacli: str | None = None, redirects: list[dict] = (), acme_to: dict | None = None,
              trust=()) -> str:
    """sites: [{instance, role, names, aliases (optional), http_port,
    websocket_port}]; an alias is one more name of its site (UX-008 §6.1).
    Port 80 answers the challenges and sends the rest to https; without a
    certificate yet (tls None) it answers 503, and there is no 443.
    redirects: [{names, to}], names that send to https://<to> with a 301 and
    the same path, from http (straight there) and from https; a server
    block of their own, answering the challenges of their names on port 80.
    omniacli: the address OmniaCLI listens on (127.0.0.1:8090): each name
    passes it LOGIN_PATH, the links of Log in as (loginas.py), and HOOKS_PATH,
    the webhooks of deploy on push (deployhooks.py).
    A site with "hold" answers the challenges on port 80 and 503 on 443
    (hold_lines): its names wait for a switch (AD-012). acme_to {"to",
    "names"}, a forward acme (AD-012): the challenges of those names not
    found here are asked to the machine at "to" (http_blocks). trust: the
    addresses of the machines that pass these names here (their forward,
    AD-012), whose X-Forwarded-For says the visitor's address (trusted_lines);
    from any other address the header is not believed."""
    trusted = trusted_lines(trust)
    names = [n for site in sites for n in (*site["names"], *site.get("aliases", ()))]
    out = [f"# written by omniacli workspace-helper: the names of workspace {workspace}.",
           "# Changed here, it is written again at the workspace's next change."]
    listen80 = ["    listen 80;"] + (["    listen [::]:80;"] if ipv6 else [])
    out += http_blocks(names, "        return 301 https://$host$request_uri;" if tls else "        return 503;",
                       listen80, acme_to)
    for redirect in redirects:
        out += http_blocks(redirect["names"], f"        return 301 https://{redirect['to']}$request_uri;" if tls
                           else "        return 503;", listen80, acme_to,
                           f"# {' '.join(redirect['names'])} → {redirect['to']}")
    out += relay_blocks(sites, omniacli)
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
                f"    proxy_read_timeout {TIMEOUT};", f"    proxy_send_timeout {TIMEOUT};", *trusted]
        if site.get("hold"):
            out += [*hold_lines(), "}"]
            continue
        out += [*(maintenance_lines() if site.get("maintenance") else []),
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
# the page nginx answers itself while OmniaCLI does not (AD-018 §18.5.2): static, in Italian and in English, in the
# site's own configuration (no file to keep, nothing of OmniaCLI's own to load: it is down); no ', $ or \ in it
MAINTENANCE_LOCATION = "@omniacli_maintenance"
MAINTENANCE_RETRY = "30"
MAINTENANCE_IT = "Il portale è momentaneamente fermo. I vostri siti Odoo funzionano normalmente."
MAINTENANCE_EN = "The portal is down for a moment. Your Odoo sites work as usual."
MAINTENANCE_HTML = f"""<!doctype html>
<html lang="it"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><title>Portale fermo · Portal down</title>
<style>
:root {{ color-scheme: light dark; --bg: #f6f7f9; --fg: #1f2328; --muted: #57606a; --card: #ffffff; --line: #d0d7de; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg: #0e1117; --fg: #e6edf3; --muted: #9da7b3; --card: #161b22;
--line: #30363d; }} }}
body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; background: var(--bg); color: var(--fg);
font: 16px/1.5 system-ui, -apple-system, Segoe UI, Roboto, sans-serif; padding: 16px; box-sizing: border-box; }}
main {{ max-width: 34rem; background: var(--card); border: 1px solid var(--line); border-radius: 8px;
padding: 24px; }}
h1 {{ font-size: 1.25rem; margin: 0 0 8px; }} p {{ margin: 0 0 16px; }} .en {{ color: var(--muted); margin: 0; }}
</style></head><body><main>
<h1>{MAINTENANCE_IT}</h1>
<p>Riprovate tra qualche minuto.</p>
<p class="en" lang="en">{MAINTENANCE_EN} Please try again in a few minutes.</p>
</main></body></html>"""

# the panel is the edge here: the address and the scheme are nginx's own,
# never one a client sent (uvicorn trusts them from 127.0.0.1 only: cli.py)
_PORTAL_HEADERS = (
    ("Host", "$host"),
    ("X-Forwarded-Host", "$host"),
    ("X-Forwarded-For", "$remote_addr"),
    ("X-Forwarded-Proto", "https"),
    ("X-Real-IP", "$remote_addr"),
)


def maintenance_return() -> str:
    """nginx's return of the maintenance page: the HTML in one quoted line
    (no ' and no $ in it: nginx would read them)."""
    text = " ".join(line.strip() for line in MAINTENANCE_HTML.splitlines() if line.strip())
    return f"return 503 '{text}';"


def portal_conf_text(host: str | list[str], tls: tuple[str, str] | None, ipv6: bool, omniacli: str) -> str:
    """The portal's site: port 80 answers Let's Encrypt's challenges and
    sends the rest to https; 443 passes the whole panel to OmniaCLI (omniacli:
    host:port, omniacli_address), its websockets too, with HSTS, the body of
    a backup upload and a limit on the login form, and the maintenance page
    when OmniaCLI does not answer (AD-018 §18.5.2). host: its name, or its
    names (the customers' portal and the staff's panel, AD-018 §18.4: one
    site, one certificate). Without a certificate (tls None) port 80 answers
    503 and there is no 443."""
    names = " ".join([host] if isinstance(host, str) else host)
    listen80 = ["    listen 80;"] + (["    listen [::]:80;"] if ipv6 else [])
    out = ["# written by omniacli workspace-helper: the customers' portal, OmniaCLI's own panel.",
           "# Changed here, it is written again when the portal's address is published (Options).", "",
           "server {", *listen80, f"    server_name {names};",
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
           "server {", *listen443, f"    server_name {names};",
           f"    ssl_certificate {tls[0]};", f"    ssl_certificate_key {tls[1]};",
           "    ssl_protocols TLSv1.2 TLSv1.3;",
           f"    client_max_body_size {PORTAL_MAX_BODY};",
           "    proxy_request_buffering off;",
           f"    proxy_read_timeout {PORTAL_TIMEOUT};", f"    proxy_send_timeout {PORTAL_TIMEOUT};",
           # one HSTS, nginx's, on its own errors (502) too
           "    proxy_hide_header Strict-Transport-Security;",
           f'    add_header Strict-Transport-Security "{PORTAL_HSTS}" always;',
           # OmniaCLI stopped (an update by hand, the process fallen): nginx's own 502/503/504 only, never
           # OmniaCLI's answers (no proxy_intercept_errors); always said as 503 (without =503 nginx keeps its 502);
           # a location's add_header replaces the server's: HSTS again
           f"    error_page 502 503 504 =503 {MAINTENANCE_LOCATION};",
           f"    location {MAINTENANCE_LOCATION} {{", '        default_type "text/html; charset=utf-8";',
           f'        add_header Strict-Transport-Security "{PORTAL_HSTS}" always;',
           '        add_header Cache-Control "no-store" always;',
           f'        add_header Retry-After "{MAINTENANCE_RETRY}" always;',
           f"        {maintenance_return()}", "    }",
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
    found = portal_names_in(text)
    return found[0] if found else ""


def portal_names_in(text: str) -> list[str]:
    """The names the portal's site answers on (its first server_name: the portal's, then the staff's panel's)."""
    match = _SERVER_NAME.search(text or "")
    return match.group(1).split() if match else []


_FILE = re.compile(r"^# configuration file (.+):$")
_SERVER_NAME = re.compile(r"(?:^|[;{}\s])server_name\s+([^;]*);")


def names_elsewhere(dump: str, own_file: str | tuple[str, ...]) -> dict[str, str]:
    """The server names of nginx -T's dump, outside own_file (a file, or
    several: a workspace's site and the site of its forward): {name: file}.
    Regular expressions (~...) are left out: they cannot be compared."""
    own = {own_file} if isinstance(own_file, str) else set(own_file)
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
        if path in own:
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


# the ports an environment's nginx proxies to now, when an update without stopping (zerodowntime.py) has
# switched it to another container: a file of root's in the role's project, which a site written again keeps
UPSTREAM_FILE = "upstream.json"
_PROXY_LOCAL = re.compile(r"^(\s*proxy_pass http://127\.0\.0\.1:)(\d+)(;\s*)$")


def switch_upstream(text: str, instance: str, role: str, ports: dict[int, int]) -> str:
    """The site's text with the server block of instance's role (its line
    «# <instance> <role>», as conf_text writes it) proxying to the new
    ports: ports {now: then}, its http port and its websocket port; the
    other proxy_pass of the block (OmniaCLI's own address) left as they are.
    ValueError when the block is not there or proxies to none of them."""
    lines = text.split("\n")
    mark = f"# {instance} {role}"
    # its block of the relay's tunnel (relay_blocks) follows the same ports
    starts = [i for i, line in enumerate(lines) if line in (mark, f"{mark} {RELAY_MARK}")
              and i + 1 < len(lines) and lines[i + 1] == "server {"]
    if not starts:
        raise ValueError(f"the site has no server block of {instance} {role}")
    changed = 0
    for start in starts:
        for index in range(start + 2, len(lines)):
            if lines[index] == "}":
                break
            match = _PROXY_LOCAL.match(lines[index])
            if match and int(match.group(2)) in ports:
                lines[index] = f"{match.group(1)}{ports[int(match.group(2))]}{match.group(3)}"
                changed += 1
    if not changed:
        raise ValueError(f"the server block of {instance} {role} proxies to none of the ports "
                         f"{', '.join(str(p) for p in sorted(ports))}")
    return "\n".join(lines)


# The environment's page while a normal update has its Odoo stopped (the docker plugin's stop_serving): nginx
# answers what would be its 502 with a 503, Retry-After and a page in the reader's language, until Odoo answers
# again; a file of root's in the role's project says it, which a site written again keeps
MAINTENANCE_FILE = "maintenance.json"
MAINTENANCE_TEXT = "Update in progress, come back shortly"
MAINTENANCE_RETRY = 30
_MAINTENANCE_BEGIN = "    # omniacli: maintenance, its Odoo stopped for an update"
_MAINTENANCE_END = "    # omniacli: maintenance end"
_NGINX_STRING_SAFE = re.compile(r"[^\"$\\\n]*")


def maintenance_page(language: str, said: str = MAINTENANCE_TEXT) -> str:
    """The page, one line for nginx's return: its title the text said in
    language (i18n's catalog), reloaded by itself."""
    text = i18n.gettext(language, said)
    page = (f"<!doctype html><html lang='{language}'><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width'><meta http-equiv='refresh' content='15'>"
            f"<title>{text}</title><body style='font-family:sans-serif;text-align:center;padding-top:20vh'>"
            f"<h1>{text}</h1></body></html>")
    if not _NGINX_STRING_SAFE.fullmatch(page):
        raise ValueError(f"the maintenance page of {language} has a character nginx's string does not take")
    return page


def maintenance_lines() -> list[str]:
    """The lines of a server block that answer its Odoo's 502 and 504 (Odoo
    stopped, starting) with the maintenance page: 503, Retry-After, in the
    first language of the reader's Accept-Language that the catalog has,
    else in English. Odoo's own answers pass as they are."""
    return [_MAINTENANCE_BEGIN, "    error_page 502 504 = @omniacli_maintenance;",
            "    location @omniacli_maintenance {", *page_lines(MAINTENANCE_TEXT), "    }", _MAINTENANCE_END]


def page_lines(said: str) -> list[str]:
    """The lines of a location that answers 503 with the page of said:
    Retry-After, no cache, in the first language of the reader's
    Accept-Language that the catalog has, else in English."""
    lines = ["        default_type text/html;", "        charset utf-8;",
             f"        add_header Retry-After {MAINTENANCE_RETRY} always;",
             '        add_header Cache-Control "no-store" always;']
    for language in i18n.LANGUAGES:
        if language != i18n.DEFAULT and i18n.catalog(language).get(said):
            lines += [f'        if ($http_accept_language ~* "^{language}") {{',
                      f'            return 503 "{maintenance_page(language, said)}";', "        }"]
    return lines + [f'        return 503 "{maintenance_page(i18n.DEFAULT, said)}";']


def switch_maintenance(text: str, instance: str, role: str, on: bool) -> str:
    """The site's text with the server block of instance's role answering
    with the maintenance page when its Odoo does not (on), or not (off)."""
    lines = text.split("\n")
    mark = f"# {instance} {role}"
    try:
        start = lines.index(mark)
    except ValueError:
        raise ValueError(f"the site has no server block of {instance} {role}") from None
    if start + 1 >= len(lines) or lines[start + 1] != "server {":
        raise ValueError(f"the site has no server block of {instance} {role}")
    end = lines.index("}", start + 2)
    block = lines[start + 2:end]
    if _MAINTENANCE_BEGIN in block:
        first, last = block.index(_MAINTENANCE_BEGIN), block.index(_MAINTENANCE_END)
        block = block[:first] + block[last + 1:]
    if on:
        at = next((i for i, line in enumerate(block) if line.startswith("    location ")), len(block))
        block = block[:at] + maintenance_lines() + block[at:]
    return "\n".join(lines[:start + 2] + block + lines[end:])


def workers(conf: str) -> int:
    """workers of an odoo.conf, 0 when not set."""
    for line in conf.splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "workers" and value.strip().isdigit():
            return int(value.strip())
    return 0
