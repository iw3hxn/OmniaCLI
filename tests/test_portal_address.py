# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The customers' portal (docs/portale-clienti.md): OmniaCLI's own panel
published on a name of the machine by the root helper (workspacehelper.portal),
the links it hands out carrying that name, the machine's users kept off it,
and the client's address read from the machine's nginx only."""
import shutil
import subprocess

import httpx
import pytest
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from omniacli import cli, workspacehelper, workspaceops, workspaceweb
from omniacli.app import create_app
from omniacli.composehelper import HelperError
from omniacli.config import ConfigError, Workspace, load_options, parse_options

from .conftest import PASSWORD
from .test_app import AUTH, HOST, POST, client, wait_job  # noqa: F401 - the client fixture
from .test_invitations import central  # noqa: F401 - the central fixture
from .test_stacktemplates import root  # noqa: F401 - the root fixture
from .test_workspaceweb import web_root  # noqa: F401 - the web_root fixture
from .test_workspaces import workspaces  # noqa: F401 - the workspaces fixture

PORTAL = "portale.hosting.test"
JSON = {**POST, "Accept": "application/json"}


def test_the_site_of_the_portal():
    tls = ("/etc/letsencrypt/live/omniacli-_portal/fullchain.pem", "/etc/letsencrypt/live/omniacli-_portal/privkey.pem")
    site = workspaceweb.portal_conf_text(PORTAL, tls, True, "127.0.0.1:8090")
    port80, port443 = site.split("listen 443 ssl;")
    # 80: the challenges, then https; 443: the whole panel to OmniaCLI's own address
    assert "location /.well-known/acme-challenge/ {" in port80 and "return 301 https://$host$request_uri;" in port80
    assert f"server_name {PORTAL};" in port80 and f"server_name {PORTAL};" in port443 and "listen [::]:443 ssl;" in site
    assert "proxy_pass http://127.0.0.1:8090;" in port443 and "location / {" in port443
    # the websockets of the live pages, kept open
    assert "proxy_set_header Upgrade $http_upgrade;" in port443 and "proxy_set_header Connection $omniacli_portal_upgrade;" in port443
    assert "map $http_upgrade $omniacli_portal_upgrade {" in site and "proxy_read_timeout 3600s;" in port443
    # a backup upload (uploads.MAX_SIZE, 50 GiB) streamed through
    assert "client_max_body_size 50g;" in port443 and "proxy_request_buffering off;" in port443
    # HSTS, one, nginx's
    assert 'add_header Strict-Transport-Security "max-age=31536000" always;' in port443
    assert "proxy_hide_header Strict-Transport-Security;" in port443
    # the client's address is nginx's own, never one the client sent; the scheme https
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in port443 and "$proxy_add_x_forwarded_for" not in site
    assert "proxy_set_header X-Forwarded-Proto https;" in port443 and "proxy_set_header Host $host;" in port443
    # the login and the invitations' form: a limit per address on what is posted
    assert "map $request_method $omniacli_portal_form {" in site and "POST $binary_remote_addr;" in site
    assert "limit_req_zone $omniacli_portal_form zone=omniacli_portal_login:1m rate=10r/m;" in site
    for location in ("location = /login {", "location ^~ /invite/ {"):
        block = port443.split(location, 1)[1].split("}", 1)[0]
        assert "limit_req zone=omniacli_portal_login burst=5 nodelay;" in block and "limit_req_status 429;" in block
        assert "proxy_pass http://127.0.0.1:8090;" in block
    assert workspaceweb.portal_name_in(site) == PORTAL
    # no certificate: 503 on 80, no 443 and no limit zones left half-written
    bare = workspaceweb.portal_conf_text(PORTAL, None, False, "127.0.0.1:8090")
    assert "return 503;" in bare and "listen 443" not in bare and "limit_req_zone" not in bare


@pytest.mark.skipif(shutil.which("nginx") is None, reason="nginx is not installed")
def test_nginx_takes_the_site(tmp_path):
    """The site inside an http block of a configuration of its own: nginx -t."""
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert),
                    "-days", "1", "-subj", f"/CN={PORTAL}"], check=True, capture_output=True)
    site = workspaceweb.portal_conf_text(PORTAL, (str(cert), str(key)), False, "127.0.0.1:8090")
    (tmp_path / "nginx.conf").write_text(f"pid {tmp_path}/nginx.pid; error_log {tmp_path}/error.log;\n"
                                         f"events {{}}\nhttp {{\n{site}}}\n")
    done = subprocess.run(["nginx", "-t", "-p", str(tmp_path), "-c", str(tmp_path / "nginx.conf")],
                          capture_output=True, text=True)
    assert done.returncode == 0 or "Permission denied" in done.stderr, done.stderr


@pytest.fixture
def portal_root(web_root, monkeypatch):
    state = {"reaches": True}
    monkeypatch.setattr(workspacehelper, "_reaches_here", lambda host: state["reaches"])
    monkeypatch.setattr(workspacehelper, "omniacli_address", lambda defaults=None: "127.0.0.1:8090")
    web_root["web"].update(state=state)
    workspace_run = workspacehelper.subprocess.run

    def run(cmd, **kw):
        # certbot makes the portal's lineage (web_root's makes the workspace's)
        if cmd[0].endswith("certbot") and "--cert-name" in cmd and not web_root["web"]["certbot"]:
            lineage = web_root["tmp"] / "letsencrypt" / cmd[cmd.index("--cert-name") + 1]
            lineage.mkdir(exist_ok=True)
            (lineage / "fullchain.pem").write_text("chain")
            (lineage / "privkey.pem").write_text("key")
        return workspace_run(cmd, **kw)
    monkeypatch.setattr(workspacehelper.subprocess, "run", run)
    return {**web_root, "portal": web_root["tmp"] / "nginx" / "omniacli-_portal.conf"}


def test_helper_portal_with_lets_encrypt(portal_root):
    state = portal_root["web"]
    made = workspacehelper.portal(PORTAL)
    assert made["address"] == PORTAL and made["https"] and made["certificate"] == "letsencrypt"
    assert made["certificate_error"] is None
    certbot = [c for c in state["commands"] if c[0].endswith("certbot")][0]
    assert certbot[certbot.index("--cert-name") + 1] == "omniacli-_portal" and certbot[-2:] == ["-d", PORTAL]
    site = portal_root["portal"].read_text()
    assert f"ssl_certificate {portal_root['tmp'] / 'letsencrypt' / 'omniacli-_portal' / 'fullchain.pem'};" in site
    assert "proxy_pass http://127.0.0.1:8090;" in site
    # the workspaces cannot take its name: it is another site of nginx for them (web-names)
    assert workspaceweb.portal_name_in(site) == PORTAL
    # taken away: the site and Let's Encrypt's certificate
    made = workspacehelper.portal("-")
    assert not portal_root["portal"].exists() and made["address"] == ""
    assert ["certbot", "delete", "--cert-name", "omniacli-_portal", "--non-interactive"] in portal_root["calls"]


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl is not installed")
def test_helper_portal_self_signed_until_it_leads_here(portal_root):
    """A name not in DNS (or a machine Let's Encrypt cannot reach): https with
    a certificate of the machine's own, said; no certbot."""
    state = portal_root["web"]
    state["dns"] = set()
    made = workspacehelper.portal(PORTAL)
    assert made["https"] and made["certificate"] == "self-signed" and "not in DNS yet" in made["certificate_error"]
    assert not [c for c in state["commands"] if c[0].endswith("certbot")]
    folder = portal_root["tmp"] / "certs" / "_portal"
    assert f"ssl_certificate {folder / 'fullchain.pem'};" in portal_root["portal"].read_text()
    assert oct(folder.stat().st_mode & 0o777) == "0o700" and oct((folder / "privkey.pem").stat().st_mode & 0o777) == "0o600"
    alt = subprocess.run(["openssl", "x509", "-in", str(folder / "fullchain.pem"), "-noout", "-ext", "subjectAltName"],
                         capture_output=True, text=True).stdout
    assert f"DNS:{PORTAL}" in alt
    # in DNS, but not leading here: still its own
    state["dns"], state["state"]["reaches"] = None, False
    made = workspacehelper.portal(PORTAL)
    assert made["certificate"] == "self-signed" and "does not lead to this machine" in made["certificate_error"]
    # another name: a certificate of its own made again
    workspacehelper.portal("panel.hosting.test")
    assert "DNS:panel.hosting.test" in subprocess.run(
        ["openssl", "x509", "-in", str(folder / "fullchain.pem"), "-noout", "-ext", "subjectAltName"],
        capture_output=True, text=True).stdout
    workspacehelper.portal("-")
    assert not folder.exists()


def test_helper_portal_refusals_and_rollback(portal_root, monkeypatch):
    state = portal_root["web"]
    for bad in ("not a name", "*.hosting.test", "10.0.0.1", "stg-20261121.hosting.test"):
        with pytest.raises(HelperError):
            workspacehelper.portal(bad)
    # a name another site of nginx answers on
    with pytest.raises(HelperError, match=r"www\.other\.it \(www\.other\.it in /etc/nginx/sites-enabled/other\)"):
        workspacehelper.portal("www.other.it")
    assert not portal_root["portal"].exists()
    # nginx -t refuses: the site before is back
    workspacehelper.portal(PORTAL)
    before = portal_root["portal"].read_text()
    state["nginx_t"] = 1
    with pytest.raises(HelperError, match="nginx -t refused"):
        workspacehelper.portal("panel.hosting.test")
    assert portal_root["portal"].read_text() == before
    # a machine whose nginx OmniaCLI does not keep (no workspaces): nothing is written
    monkeypatch.setattr(workspacehelper, "storage_roots", lambda config=None: {})
    with pytest.raises(HelperError, match="kept by whoever administers it"):
        workspacehelper.portal(PORTAL)
    assert workspacehelper.portal_taken() == {"elsewhere": {}, "ours": False}
    assert workspacehelper.ACTIONS["portal"] == (workspacehelper.portal, 1)


def test_the_options_keep_the_address():
    options = parse_options({"portal": {"provider": "Hosting", "address": "Portale.Hosting.test", "staff": True}})
    assert (options.portal_address, options.portal_staff, options.provider_label) == (PORTAL, True, "Hosting")
    assert parse_options({}).portal_address == "" and not parse_options({}).portal_staff
    for bad in ({"address": "not a name"}, {"staff": "yes"}):
        with pytest.raises(ConfigError):
            parse_options({"portal": bad})


def published(monkeypatch, settings, calls=None):
    """The root helper answered here; OmniaCLI keeps this machine's nginx."""
    settings.storage_roots = {"main": "/srv/omniacli"}

    async def helper(job, *args, timeout=None):
        if calls is not None:
            calls.append(args)
        if args[1] == "-":
            return {"address": "", "nginx": "nginx reloaded"}
        return {"address": args[1], "https": True, "certificate": "self-signed",
                "certificate_error": f"{args[1]} is not in DNS yet", "nginx": "nginx reloaded"}
    monkeypatch.setattr(workspaceops, "_helper", helper)


async def test_publish_the_portal(client, env, monkeypatch):
    settings = env["settings"]
    settings.storage_roots = {}
    # where OmniaCLI does not keep nginx: refused with the reason, as the Addresses dialog
    resp = await client.post("/api/portal/publish", headers=JSON, json={"host": PORTAL})
    assert resp.status_code == 400 and "kept by whoever administers it" in resp.json()["error"]
    page = (await client.get("/options", headers={"Authorization": AUTH})).text
    assert "kept by whoever administers it" in page and 'id="portal-address"' not in page
    calls = []
    published(monkeypatch, settings, calls)
    page = (await client.get("/options", headers={"Authorization": AUTH})).text
    assert 'id="portal-address"' in page and "The portal has no address yet:" in page
    assert "The customers&#39; portal has no address yet" in (await client.get("/users", headers={"Authorization": AUTH})).text
    for host, message in (("not a name", "not a host name"), ("srv.acme.example.com", "under the domain of another workspace")):
        settings.workspaces = {"acme": Workspace("acme", domain="acme.example.com")}
        resp = await client.post("/api/portal/publish", headers=JSON, json={"host": host})
        assert resp.status_code == 400 and message in resp.json()["error"], resp.text
    # a developer, a user of a workspace: not theirs
    assert (await client.post("/api/portal/publish", headers={**JSON, **dev()}, json={"host": PORTAL})).status_code == 403
    resp = await client.post("/api/portal/publish", headers=JSON, json={"host": PORTAL.upper()})
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success" and calls == [("portal", PORTAL)]
    assert job["result"]["warnings"] and "browsers warn" in job["result"]["warnings"][0]
    assert settings.options.portal_address == PORTAL and load_options(settings.options_file).portal_address == PORTAL
    # in the access log: who published it
    action = [e for e in client.app.state.access_log.recent() if e["event"] == "action" and "/api/portal/publish" in e["detail"]]
    assert action and f"host={PORTAL.upper()}" in action[0]["detail"]
    # the Options page and the Users page say where the customers log in; /api/info says it to a linking OmniaCLI
    page = (await client.get("/options", headers={"Authorization": AUTH})).text
    assert f'href="https://{PORTAL}"' in page and 'id="portal-remove"' in page
    assert f'href="https://{PORTAL}"' in (await client.get("/users", headers={"Authorization": AUTH})).text
    assert (await client.get("/api/info", headers={"Authorization": AUTH})).json()["portal"] == f"https://{PORTAL}"
    # the Options' Save keeps it: the address changes only when published
    assert (await client.post("/api/options/save", headers=JSON, json={"portal": {"provider": "Hosting", "address": "x.test"}})).json()["ok"]
    assert settings.options.portal_address == PORTAL and settings.options.provider_label == "Hosting"
    # taken away
    job = await wait_job(client, (await client.post("/api/portal/publish", headers=JSON, json={"host": ""})).json()["job"])
    assert job["status"] == "success" and calls[-1] == ("portal", "-") and settings.options.portal_address == ""


def dev() -> dict:
    import base64
    return {"Authorization": "Basic " + base64.b64encode(f"dev:{PASSWORD}".encode()).decode()}


async def test_the_links_carry_the_portal(client, env, workspaces, monkeypatch):
    settings = env["settings"]
    settings.options = parse_options({"portal": {"address": PORTAL}})
    # a customer's user: the portal; a user of the machine: the address it was asked at (it does not log in there)
    data = (await client.post("/api/workspaces/acme/members/save", headers=JSON,
                              json={"name": "gino", "role": "user", "new": True, "invite": True})).json()
    assert data["invite_url"] == f"https://{PORTAL}{data['invite_path']}" and data["portal"]
    data = (await client.post("/api/access/users/save", headers=JSON,
                              json={"name": "anna", "role": "developer", "new": True, "invite": True})).json()
    assert data["invite_url"] == f"http://{HOST}{data['invite_path']}" and not data["portal"]
    # the machine's users let in there too: theirs carry it as well
    settings.options = parse_options({"portal": {"address": PORTAL, "staff": True}})
    data = (await client.post("/api/access/users/save", headers=JSON,
                              json={"name": "bea", "role": "user", "new": True, "invite": True})).json()
    assert data["invite_url"] == f"https://{PORTAL}{data['invite_path']}"
    # the address of an assistant's MCP and of deploy on push, too
    page = (await client.get("/workspaces/acme", headers={"Authorization": AUTH})).text
    assert f"https://{PORTAL}/mcp" in page


async def test_only_the_customers_log_in_at_the_portal(env, workspaces):
    settings = env["settings"]
    settings.options = parse_options({"portal": {"address": PORTAL}})
    app = create_app(settings)
    origin = {"Origin": f"https://{PORTAL}"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"https://{PORTAL}") as portal:
        # a customer: in, with a session cookie Secure, HttpOnly, SameSite=Strict
        resp = await portal.post("/login", data={"username": "mario", "password": PASSWORD, "next": "/"}, headers=origin)
        assert resp.status_code == 303
        cookie = resp.headers["set-cookie"].lower()
        assert "secure" in cookie and "httponly" in cookie and "samesite=strict" in cookie
        assert (await portal.get("/api/servers")).status_code == 200
        portal.cookies.clear()
        # the machine's admin: refused at the portal, said once the password is right
        resp = await portal.post("/login", data={"username": "admin", "password": PASSWORD, "next": "/"}, headers=origin)
        assert resp.status_code == 403 and "customers&#39; portal" in resp.text and "set-cookie" not in resp.headers
        resp = await portal.post("/login", data={"username": "admin", "password": "wrong", "next": "/"}, headers=origin)
        assert resp.status_code == 401
        # a session of the admin's made elsewhere does not open the portal
        token = app.state.sessions.create("admin", settings.users["admin"])
        portal.cookies.set("omniacli_session", token)
        assert (await portal.get("/api/servers")).status_code == 403
        portal.cookies.clear()
        failed = [e for e in app.state.access_log.recent() if e["event"] == "failed"]
        assert any("at the customers' portal" in e["detail"] and e["name"] == "admin" for e in failed)
        # a cross-site form is still refused behind the proxy
        resp = await portal.post("/login", data={"username": "mario", "password": PASSWORD}, headers={"Origin": "https://evil.test"})
        assert resp.status_code == 403
    # the same admin at the machine's own address (its link, an SSH tunnel): in, as before
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8090") as here:
        resp = await here.post("/login", data={"username": "admin", "password": PASSWORD, "next": "/"},
                               headers={"Origin": "http://127.0.0.1:8090"})
        assert resp.status_code == 303 and "secure" not in resp.headers["set-cookie"].lower()


async def test_the_clients_address_is_nginxs_only(env):
    """uvicorn trusts X-Forwarded-For and -Proto from this machine (its nginx)
    only: the access log and the login limit read the customer's address."""
    app = create_app(env["settings"])
    wrapped = ProxyHeadersMiddleware(app, trusted_hosts=cli.TRUSTED_PROXIES)
    form = {"username": "admin", "password": "wrong"}
    for peer, forwarded, seen in ((("127.0.0.1", 50000), "172.30.0.1", "172.30.0.1"),
                                  (("203.0.113.5", 50000), "198.51.100.7", "203.0.113.5")):
        transport = httpx.ASGITransport(app=wrapped, client=peer)
        async with httpx.AsyncClient(transport=transport, base_url=f"http://{PORTAL}") as c:
            await c.post("/login", data=form, headers={"Origin": f"http://{PORTAL}", "X-Forwarded-For": forwarded,
                                                       "X-Forwarded-Proto": "https"})
        assert app.state.access_log.recent()[0]["ip"] == seen


def test_serve_pins_the_trusted_proxies(monkeypatch, tmp_path):
    import uvicorn
    given = {}
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: given.update(k))
    monkeypatch.setattr(cli, "load_settings", lambda path: None)
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")

    class Args:
        config, host, port = str(tmp_path / "c.yaml"), "127.0.0.1", 8090
    cli._serve(Args())
    assert given["proxy_headers"] and given["forwarded_allow_ips"] == "127.0.0.1,::1"


async def test_through_a_link_the_portal_of_the_linked_machine_wins(central):
    """The central OmniaCLI makes a customer's user on a linked machine: the
    link carries the linked machine's portal, not the address of the link."""
    central.customer.state.settings.options = parse_options({"portal": {"address": PORTAL}})
    headers = {"Authorization": AUTH, "Accept": "application/json", "Origin": "http://central.test"}
    data = (await central.post("/api/access/workspace-users/save", headers=headers,
                               json={"machine": "cliente", "workspace": "acme", "name": "ugo", "role": "user",
                                     "invite": True})).json()
    assert data["invite_url"] == f"https://{PORTAL}{data['invite_path']}" and not data["through_ssh"]
    # an OmniaCLI of before (no portal said): the address of the link, as before
    central.customer.state.settings.options = parse_options({})
    data = (await central.post("/api/access/workspace-users/save", headers=headers,
                               json={"machine": "cliente", "workspace": "acme", "name": "ivo", "role": "user",
                                     "invite": True})).json()
    assert data["invite_url"] == "http://customer.test" + data["invite_path"]
