# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""AD-018 slice 3, «Nomi e bordo» (§18.4, §18.5.2): the central answers on two names of its own, the customers'
portal and the staff's panel, one site of nginx and one certificate; on the portal's the staff are refused after
their right password, on the panel's the customers (this panel's users of the workspaces and the people it serves for
a linked machine), with 403 in the access log; the passkeys' RP ID is a setting, a domain both names are under, and
only this OmniaCLI's own names may use an assertion of it; the people served for a linked machine log in with a
passkey of that RP ID, checked by their machine; the addresses a customer is given are the portal's; nginx answers a
static page in Italian and in English while OmniaCLI does not."""

import json
import shutil
import subprocess

import httpx
import pytest

from omniacli import apiv1, identityimport, passkeys, workspacehelper, workspaceweb
from omniacli.app import PANEL_CUSTOMER_REFUSED, REMOTE_PASSKEY_LATER, create_app
from omniacli.config import ConfigError, load_options, parse_options, save_options

from .conftest import PASSWORD
from .softkey import SoftKey
from .test_api_v1 import token
from .test_app import AUTH, POST, client, wait_job  # noqa: F401 - the client fixture
from .test_mcp import HEADERS, call
from .test_portal_address import portal_root, published  # noqa: F401 - the portal_root fixture
from .test_remote_home import ADMIN as CENTRAL_ADMIN, home, switch  # noqa: F401 - the home fixture
from .test_stacktemplates import root  # noqa: F401 - the root fixture
from .test_workspaceweb import web_root  # noqa: F401 - the web_root fixture
from .test_workspaces import workspaces  # noqa: F401 - the workspaces fixture

DOMAIN = "hosting.test"
PORTAL = f"portale.{DOMAIN}"
PANEL = f"pannello.{DOMAIN}"
JSON = {**POST, "Accept": "application/json"}


def browser(app, host: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"https://{host}")


def origin(c) -> str:
    return str(c.base_url).rstrip("/")


async def log_in(c, user: str, password: str = PASSWORD) -> httpx.Response:
    return await c.post("/login", data={"username": user, "password": password, "next": "/"},
                        headers={"Origin": origin(c)})


async def post(c, path: str, body: dict) -> httpx.Response:
    return await c.post(path, json=body, headers={"Origin": origin(c)})


async def passkey_login(c, key: SoftKey, at: str | None = None) -> httpx.Response:
    options = (await post(c, "/login/passkey/options", {})).json()["publicKey"]
    return await post(c, "/login/passkey", {"credential": key.get(options, at or origin(c)), "next": "/"})


def failed(app, detail: str) -> list[dict]:
    return [e for e in app.state.access_log.recent() if e["event"] == "failed" and detail in e["detail"]]


# --- the options and the site ---------------------------------------------------------------------------------------

def test_the_options_keep_the_panel_and_the_passkeys_domain(tmp_path):
    options = parse_options({"portal": {"address": PORTAL, "panel": PANEL.upper(), "passkeys": "Hosting.Test."}})
    assert (options.portal_panel, options.passkey_domain) == (PANEL, DOMAIN)
    save_options(tmp_path / "o.yaml", options)
    again = load_options(tmp_path / "o.yaml")
    assert (again.portal_address, again.portal_panel, again.passkey_domain) == (PORTAL, PANEL, DOMAIN)
    assert parse_options({}).portal_panel == "" and parse_options({}).passkey_domain == ""
    # the panel's name is its own; a domain has two labels at least (a browser refuses a public suffix)
    for bad in ({"address": PORTAL, "panel": PORTAL}, {"panel": "not a name"}, {"passkeys": "test"},
                {"passkeys": "*.hosting.test"}):
        with pytest.raises(ConfigError):
            parse_options({"portal": bad})


def test_one_site_two_names_and_the_maintenance_page():
    tls = ("/etc/letsencrypt/live/omniacli-_portal/fullchain.pem", "/etc/letsencrypt/live/omniacli-_portal/privkey.pem")
    site = workspaceweb.portal_conf_text([PORTAL, PANEL], tls, False, "127.0.0.1:8090")
    port80, port443 = site.split("listen 443 ssl;")
    assert f"server_name {PORTAL} {PANEL};" in port80 and f"server_name {PORTAL} {PANEL};" in port443
    assert workspaceweb.portal_names_in(site) == [PORTAL, PANEL] and workspaceweb.portal_name_in(site) == PORTAL
    # nginx's own 502/503/504 (OmniaCLI stopped), never OmniaCLI's answers; HSTS on it too
    assert "error_page 502 503 504 =503 @omniacli_maintenance;" in port443 and "proxy_intercept_errors" not in site
    block = port443.split("location @omniacli_maintenance {", 1)[1].split("\n    }", 1)[0]
    assert 'default_type "text/html; charset=utf-8";' in block and "Strict-Transport-Security" in block
    assert 'Retry-After "30"' in block and "return 503 '" in block
    page = workspaceweb.MAINTENANCE_HTML
    assert "Il portale è momentaneamente fermo. I vostri siti Odoo funzionano normalmente." in page
    assert "The portal is down for a moment. Your Odoo sites work as usual." in page and 'lang="it"' in page
    # nothing nginx would read inside its quotes
    assert not {"'", "$", "\\"} & set(page)
    # one name: as before
    assert "server_name portale.hosting.test;" in workspaceweb.portal_conf_text(PORTAL, tls, False, "127.0.0.1:8090")


@pytest.mark.skipif(shutil.which("nginx") is None or shutil.which("openssl") is None, reason="nginx is not installed")
def test_nginx_takes_the_site_of_two_names(tmp_path):
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert),
                    "-days", "1", "-subj", f"/CN={PORTAL}"], check=True, capture_output=True)
    site = workspaceweb.portal_conf_text([PORTAL, PANEL], (str(cert), str(key)), False, "127.0.0.1:8090")
    (tmp_path / "nginx.conf").write_text(f"pid {tmp_path}/nginx.pid; error_log {tmp_path}/error.log;\n"
                                         f"events {{}}\nhttp {{\n{site}}}\n")
    done = subprocess.run(["nginx", "-t", "-p", str(tmp_path), "-c", str(tmp_path / "nginx.conf")],
                          capture_output=True, text=True)
    assert done.returncode == 0 or "Permission denied" in done.stderr, done.stderr


def test_the_helper_publishes_two_names_with_one_certificate(portal_root):
    state = portal_root["web"]
    made = workspacehelper.portal(f"{PORTAL},{PANEL}")
    assert (made["address"], made["panel"], made["certificate"]) == (PORTAL, PANEL, "letsencrypt")
    certbot = [c for c in state["commands"] if c[0].endswith("certbot")][0]
    assert certbot[-4:] == ["-d", PORTAL, "-d", PANEL]
    site = portal_root["portal"].read_text()
    assert f"server_name {PORTAL} {PANEL};" in site and "@omniacli_maintenance" in site
    # the panel's name another site has, or the portal's own twice: refused, nothing written
    for bad in (f"{PORTAL},www.other.it", f"{PORTAL},{PORTAL}", f"{PORTAL},{PANEL},third.hosting.test"):
        with pytest.raises(workspacehelper.HelperError):
            workspacehelper.portal(bad)
    assert workspaceweb.portal_names_in(portal_root["portal"].read_text()) == [PORTAL, PANEL]
    # back to one name: the certificate of the two is not kept for it
    workspacehelper.portal(PORTAL)
    assert ["certbot", "delete", "--cert-name", "omniacli-_portal", "--non-interactive"] in portal_root["calls"]


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl is not installed")
def test_the_self_signed_certificate_has_both_names(portal_root):
    portal_root["web"]["dns"] = set()
    made = workspacehelper.portal(f"{PORTAL},{PANEL}")
    assert made["certificate"] == "self-signed" and f"{PORTAL}, {PANEL} are not in DNS yet" == made["certificate_error"]
    folder = portal_root["tmp"] / "certs" / "_portal"
    alt = subprocess.run(["openssl", "x509", "-in", str(folder / "fullchain.pem"), "-noout", "-ext", "subjectAltName"],
                         capture_output=True, text=True).stdout
    assert f"DNS:{PORTAL}" in alt and f"DNS:{PANEL}" in alt


async def test_publish_both_names_from_the_page_and_the_api(client, env, monkeypatch):
    settings = env["settings"]
    calls = []
    published(monkeypatch, settings, calls)
    resp = await client.post("/api/portal/publish", headers=JSON, json={"host": PORTAL, "panel": PORTAL})
    assert resp.status_code == 400 and "a name of its own" in resp.json()["error"]
    job = await wait_job(client, (await client.post("/api/portal/publish", headers=JSON,
                                                    json={"host": PORTAL, "panel": PANEL})).json()["job"])
    assert job["status"] == "success" and calls[-1] == ("portal", f"{PORTAL},{PANEL}")
    assert (settings.options.portal_address, settings.options.portal_panel) == (PORTAL, PANEL)
    assert load_options(settings.options_file).portal_panel == PANEL
    # the Options' Save keeps both (they change only when published)
    assert (await client.post("/api/options/save", headers=JSON, json={"portal": {"provider": "Hosting"}})).json()["ok"]
    assert settings.options.portal_panel == PANEL
    page = (await client.get("/options", headers={"Authorization": AUTH})).text
    assert 'id="portal-panel"' in page and f'href="https://{PANEL}"' in page and 'id="passkey-domain"' in page
    # the portal published again without saying the panel: it stays
    job = await wait_job(client, (await client.post("/api/portal/publish", headers=JSON, json={"host": PORTAL})).json()["job"])
    assert calls[-1] == ("portal", f"{PORTAL},{PANEL}")
    # REST and MCP: the same code; a CI's token and a developer's never
    admin = await token(client, "boss", "admin")
    ci = await token(client, "ci-bot", "admin", ci=True)
    developer = await token(client, "dev-bot", "developer")
    v1 = apiv1.PREFIX + "/portal"
    said = (await client.get(v1, headers=admin)).json()
    assert (said["address"], said["panel"], said["rp"]) == (PORTAL, PANEL, {PORTAL: PORTAL, PANEL: PANEL})
    assert (await client.get(v1, headers=developer)).json()["error"]["code"] == "forbidden"
    assert (await client.post(v1 + "/publish", headers=ci, json={"host": PORTAL})).json()["error"]["code"] == "forbidden"
    resp = await client.post(v1 + "/publish", headers=admin, json={"host": PORTAL, "panel": ""})
    assert resp.status_code == 202
    await wait_job(client, resp.json()["job"]["id"])
    assert calls[-1] == ("portal", PORTAL) and settings.options.portal_panel == ""
    tools = {**HEADERS, **admin}
    job = await call(client, "portal_publish", headers=tools, host=PORTAL, panel=PANEL)
    await wait_job(client, job["job"])
    assert settings.options.portal_panel == PANEL
    assert (await call(client, "portal_status", headers=tools))["panel_url"] == f"https://{PANEL}"
    assert "forbidden" in (await call(client, "portal_status", headers={**HEADERS, **developer}))["error"]
    # audited: who set the names, from where
    actions = [e["detail"] for e in client.app.state.access_log.recent() if e["event"] == "action"]
    assert f"the portal's names: {PORTAL}, {PANEL} (a job)" in actions


# --- portal_admits both ways ----------------------------------------------------------------------------------------

async def test_staff_refused_on_the_portal_customers_on_the_panel(env, workspaces):
    settings = env["settings"]
    settings.options = parse_options({"portal": {"address": PORTAL, "panel": PANEL, "second_factor": "nobody"}})
    app = create_app(settings)
    async with browser(app, PANEL) as panel, browser(app, PORTAL) as portal:
        # the staff at the panel: in; a customer there: refused after the right password, 403 in the access log
        assert (await log_in(panel, "admin")).status_code == 303
        panel.cookies.clear()
        resp = await log_in(panel, "mario")
        assert resp.status_code == 403 and "staff panel" in resp.text and "set-cookie" not in resp.headers
        assert (await log_in(panel, "mario", "wrong")).status_code == 401
        refused = failed(app, "a user of a workspace at the staff's panel")
        assert refused and refused[0]["name"] == "mario" and refused[0]["status"] == 403
        # a customer's session made elsewhere does not open the panel, nor a staff's the portal
        panel.cookies.set("omniacli_session", app.state.sessions.create("mario", settings.users["mario"]))
        resp = await panel.get("/api/servers", headers={"Accept": "application/json"})
        assert resp.status_code == 403 and PANEL_CUSTOMER_REFUSED in resp.text
        assert (await log_in(portal, "mario")).status_code == 303
        portal.cookies.clear()
        assert (await log_in(portal, "admin")).status_code == 403
        refused = failed(app, "a user of the machine at the customers' portal")
        assert refused and refused[0]["status"] == 403
    # in Italian
    async with browser(app, PANEL) as panel:
        resp = await panel.post("/login", data={"username": "mario", "password": PASSWORD, "next": "/"},
                                headers={"Origin": origin(panel), "Accept-Language": "it"})
        assert "pannello del personale del fornitore" in resp.text


async def test_a_served_person_is_refused_on_the_panel(home):
    c = home
    c.central.options = parse_options({"portal": {"address": PORTAL, "panel": PANEL, "second_factor": "nobody"}})
    assert (await switch(c, True)).status_code == 200
    async with browser(c.app, PANEL) as panel, browser(c.app, PORTAL) as portal:
        resp = await log_in(panel, "elena")
        assert resp.status_code == 403 and "staff panel" in resp.text and "set-cookie" not in resp.headers
        refused = failed(c.app, "a user of a workspace at the staff's panel")
        assert refused and refused[0]["name"] == "elena" and refused[0]["status"] == 403
        assert (await log_in(panel, "elena", "wrong")).status_code == 401
        # at the portal: in; its session never opens the panel
        assert (await log_in(portal, "elena")).status_code == 303
        panel.cookies.set("omniacli_session", portal.cookies.get("omniacli_session"))
        assert (await panel.get("/api/servers", headers={"Accept": "application/json"})).status_code == 403


# --- the passkeys' RP ID ----------------------------------------------------------------------------------------------

def test_rp_and_origins_of_the_domain():
    names = [PORTAL, PANEL]
    assert passkeys.rp_here(PORTAL, DOMAIN, names) == DOMAIN and passkeys.rp_here(f"{PANEL}:443", DOMAIN, names) == DOMAIN
    # a name of the domain that is not this OmniaCLI's, another domain, no setting: each its own
    assert passkeys.rp_here(f"odoo.{DOMAIN}", DOMAIN, names) == f"odoo.{DOMAIN}"
    assert passkeys.rp_here("other.example.test", DOMAIN, names) == "other.example.test"
    assert passkeys.rp_here(PORTAL, "", names) == PORTAL
    assert passkeys.origins_here(PORTAL, DOMAIN, names) == [f"https://{PANEL}", f"https://{PORTAL}"]
    assert passkeys.origins_here("localhost:18090", "localhost", names) == ["https://localhost:18090",
                                                                             "http://localhost:18090"]


async def test_a_passkey_of_the_domain_works_on_both_names_and_nowhere_else(env, workspaces):
    settings = env["settings"]
    settings.options = parse_options({"portal": {"address": PORTAL, "panel": PANEL, "passkeys": DOMAIN,
                                                 "staff": True, "second_factor": "nobody"}})
    app = create_app(settings)
    key = SoftKey()
    async with browser(app, PANEL) as c:
        assert (await log_in(c, "admin")).status_code == 303
        options = (await post(c, "/api/account/passkeys/options", {"name": "Portatile"})).json()["publicKey"]
        assert options["rp"]["id"] == DOMAIN
        assert (await post(c, "/api/account/passkeys/register",
                           {"credential": key.create(options, origin(c))})).status_code == 200
    assert app.state.passkeys.keys("admin")[0]["rp"] == DOMAIN
    # made on the panel: it opens the panel and the portal (the same RP ID)
    for host in (PANEL, PORTAL):
        async with browser(app, host) as c:
            resp = await passkey_login(c, key)
            assert resp.status_code == 200, (host, resp.text)
            assert (await c.get("/api/servers", headers={"Accept": "application/json"})).status_code == 200
    # a name of another domain: its own RP ID, the passkey is not of it
    async with browser(app, "other.example.test") as c:
        options = (await post(c, "/login/passkey/options", {})).json()["publicKey"]
        assert options["rpId"] == "other.example.test"
        resp = await post(c, "/login/passkey", {"credential": key.get(options, origin(c), rp_id=DOMAIN)})
        assert resp.status_code == 401
    # a stranger subdomain (an Odoo of the same domain) gets an assertion of the RP ID: refused by its origin
    async with browser(app, PORTAL) as c:
        resp = await passkey_login(c, key, at=f"https://odoo.{DOMAIN}")
        assert resp.status_code == 401 and resp.json()["error"] == "This passkey was not accepted."
    assert any("not verified" in e["detail"] and "origin" in e["detail"].lower() for e in failed(app, "passkey"))
    # with the staff kept off the portal: the passkey of the domain is refused there after it is verified, 403
    settings.options = parse_options({"portal": {"address": PORTAL, "panel": PANEL, "passkeys": DOMAIN,
                                                 "second_factor": "nobody"}})
    async with browser(app, PORTAL) as c:
        assert (await passkey_login(c, key)).status_code == 403


async def test_the_passkeys_domain_is_set_once_with_rest_and_mcp(client, env, monkeypatch):
    settings = env["settings"]
    published(monkeypatch, settings)
    settings.options = parse_options({"portal": {"address": PORTAL, "panel": "panel.elsewhere.test"}})
    resp = await client.post("/api/portal/passkeys", headers=JSON, json={"domain": DOMAIN})
    assert resp.status_code == 400 and "panel.elsewhere.test is not under hosting.test" in resp.json()["error"]
    resp = await client.post("/api/portal/passkeys", headers={**JSON, "Accept-Language": "it"}, json={"domain": "test"})
    assert resp.status_code == 400 and "non è un dominio" in resp.json()["error"]
    settings.options = parse_options({"portal": {"address": PORTAL, "panel": PANEL}})
    # a passkey made on the portal's own name before: the domain asks to confirm, it stops working there
    app_keys = client.app.state.passkeys
    app_keys.add("admin", b"h" * 32, PORTAL, b"id-1", b"key", 0, "Old", [], False)
    resp = await client.post("/api/portal/passkeys", headers=JSON, json={"domain": DOMAIN})
    assert resp.status_code == 400 and resp.json()["code"] == "confirm_required" and resp.json()["lost"] == 1
    assert settings.options.passkey_domain == ""
    resp = await client.post("/api/portal/passkeys", headers=JSON, json={"domain": DOMAIN, "confirm": DOMAIN})
    assert resp.status_code == 200 and resp.json()["lost"] == 1 and settings.options.passkey_domain == DOMAIN
    assert load_options(settings.options_file).passkey_domain == DOMAIN
    assert resp.json()["portal"]["rp"] == {PORTAL: DOMAIN, PANEL: DOMAIN}
    entry = [e for e in client.app.state.access_log.recent() if e["event"] == "action"
             and "passkeys' domain" in e["detail"]][0]
    assert entry["detail"] == ("the passkeys' domain of the portal set to hosting.test; 1 passkeys of the RP IDs of "
                               "before no longer work here")
    # REST and MCP: the same code; a workspace's token and a CI's never
    admin = await token(client, "boss", "admin")
    ci = await token(client, "ci-bot", "admin", ci=True)
    v1 = apiv1.PREFIX + "/portal/passkeys"
    assert (await client.post(v1, headers=ci, json={"domain": ""})).json()["error"]["code"] == "forbidden"
    data = (await client.post(v1, headers=admin, json={"domain": ""})).json()
    assert data["changed"] and settings.options.passkey_domain == ""
    tools = {**HEADERS, **admin}
    # the old passkey of the portal's own name again: MCP asks to confirm as the page does
    assert "confirm_required" in (await call(client, "portal_passkeys", headers=tools, domain=DOMAIN))["error"]
    done = await call(client, "portal_passkeys", headers=tools, domain=DOMAIN, confirm=DOMAIN)
    assert done["portal"]["passkey_domain"] == DOMAIN
    assert (await call(client, "portal_status", headers=tools))["passkeys_of_domain"] == 0


def test_the_dry_run_compares_the_passkeys_with_the_rp_id():
    dry = {"workspaces": {"acme": {"people": [{"name": "anna"}], "passkeys": {DOMAIN: 2, "lab4.example.test": 1}}}}
    rows = identityimport.plan(dry, set(), f"https://{PORTAL}", rp_id=DOMAIN)["workspaces"][0]["items"]
    verdicts = {r["rp"]: r["verdict"] for r in rows if r["item"] == "passkeys"}
    assert verdicts == {DOMAIN: identityimport.SAME, "lab4.example.test": identityimport.REDO}
    rows = identityimport.plan(dry, set(), f"https://{PORTAL}")["workspaces"][0]["items"]
    assert {r["verdict"] for r in rows if r["item"] == "passkeys"} == {identityimport.REDO}


# --- the passkeys of the people served for a linked machine (slice 2 left them to this one) --------------------------

def machine_passkey(c, person: str, rp: str) -> SoftKey:
    """A passkey of person made on the machine with rp: its public key and counter live there."""
    key = SoftKey()
    key.handle = b"handle-of-" + person.encode()
    c.machine_app.state.passkeys.add(person, key.handle, rp, key.credential_id, key._cose(), 0, "Telefono",
                                     ["internal"], True)
    return key


async def test_a_served_person_logs_in_with_a_passkey_of_the_rp_id(home):
    c = home
    elena, bruno = machine_passkey(c, "elena", DOMAIN), machine_passkey(c, "bruno", "lab4.example.test")
    assert (await c.post("/api/identities/lab4/import", headers=CENTRAL_ADMIN,
                         json={"confirm": "lab4"})).status_code == 200
    c.central.options = parse_options({"portal": {"address": PORTAL, "panel": PANEL, "passkeys": DOMAIN,
                                                  "second_factor": "nobody"}})
    assert (await switch(c, True)).status_code == 200
    asked = len(c.machine.asked)
    # the password, then its passkey as the second step: the machine says which are of this RP ID, and checks it
    async with browser(c.app, PORTAL) as b:
        resp = await log_in(b, "elena")
        assert resp.status_code == 303 and resp.headers["location"] == "/login/code", resp.text[:300]
        page = (await b.get("/login/code")).text
        assert 'id="pk-step"' in page and 'name="code"' not in page
        assert (await b.post("/login/code", data={"code": "123456"}, headers={"Origin": origin(b)})).status_code == 400
        options = (await post(b, "/login/code/passkey/options", {})).json()["publicKey"]
        assert options["rpId"] == DOMAIN and [k["id"] for k in options["allowCredentials"]] == [
            passkeys.b64(elena.credential_id)]
        resp = await post(b, "/login/code/passkey", {"credential": elena.get(options, origin(b))})
        assert resp.status_code == 200 and resp.json()["next"] == "/", resp.text
        assert (await b.get("/api/servers", headers={"Accept": "application/json"})).status_code == 200
    assert c.machine_app.state.passkeys.keys("elena")[0]["count"] == 1
    assert any(p.endswith("/api/identity/v1/login") for _, p, _ in c.machine.asked[asked:])
    # without a password, on the other name of the RP ID; the counter grows on the machine
    async with browser(c.app, PORTAL) as b:
        resp = await passkey_login(b, elena)
        assert resp.status_code == 200, resp.text
    assert c.machine_app.state.passkeys.keys("elena")[0]["count"] == 2
    # a counter that went back (a cloned authenticator): refused by the machine
    async with browser(c.app, PORTAL) as b:
        options = (await post(b, "/login/passkey/options", {})).json()["publicKey"]
        resp = await post(b, "/login/passkey", {"credential": elena.get(options, origin(b), count=1), "next": "/"})
        assert resp.status_code == 401
    # a stranger subdomain's assertion of the RP ID: refused by the machine for its origin
    async with browser(c.app, PORTAL) as b:
        assert (await passkey_login(b, elena, at=f"https://odoo.{DOMAIN}")).status_code == 401
    # the panel's name: a customer, refused after it is verified
    async with browser(c.app, PANEL) as b:
        assert (await passkey_login(b, elena)).status_code == 403
    # a passkey made on another name: not of this RP ID, the reason said after the password
    async with browser(c.app, PORTAL) as b:
        resp = await log_in(b, "bruno")
        assert resp.status_code == 403 and "another address" in resp.text
        assert REMOTE_PASSKEY_LATER.split(",")[0] in resp.text.replace("&#39;", "'")
    # both access logs say it: the central the login, the machine the passkey with its name
    logins = [e["detail"] for e in c.app.state.access_log.recent() if e["event"] == "login" and e["name"] == "elena"]
    assert any("second step: passkey" in d for d in logins) and any("passkey, without password" in d for d in logins)
    there = [e["detail"] for e in c.machine_app.state.access_log.recent() if e["event"] == "login"]
    assert any("elena through the central panel: password + passkey «Telefono»" in d for d in there)
    assert any("elena through the central panel: passkey «Telefono», without password" in d for d in there)


# --- the addresses a person is given -----------------------------------------------------------------------------------

async def test_the_addresses_given_are_the_portals_even_from_the_panel(client, env, workspaces):
    settings = env["settings"]
    settings.options = parse_options({"portal": {"address": PORTAL, "panel": PANEL}})
    on_panel = {**JSON, "Host": PANEL, "Origin": f"https://{PANEL}"}
    # a customer invited from the panel: the portal; a member of the staff: the panel, where it logs in
    data = (await client.post("/api/workspaces/acme/members/save", headers=on_panel,
                              json={"name": "gino", "role": "user", "new": True, "invite": True})).json()
    assert data["invite_url"] == f"https://{PORTAL}{data['invite_path']}"
    data = (await client.post("/api/access/users/save", headers=on_panel,
                              json={"name": "anna", "role": "developer", "new": True, "invite": True})).json()
    assert data["invite_url"] == f"https://{PANEL}{data['invite_path']}"
    # a workspace's MCP URL (a token's and an assistant's through OAuth) and its single sign-on's return: the portal's
    page = (await client.get("/workspaces/acme", headers={"Authorization": AUTH, "Host": PANEL})).text
    assert f"https://{PORTAL}/mcp" in page and f"https://{PANEL}/" not in page.replace(f'href="https://{PANEL}"', "")
    for host in (PANEL, PORTAL):
        meta = (await client.get("/.well-known/oauth-protected-resource/mcp", headers={"Host": host})).json()
        assert meta["resource"] == f"https://{PORTAL}/mcp"
