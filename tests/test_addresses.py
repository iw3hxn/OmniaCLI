# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Addresses dialog of an environment (docs/ux/UX-008 §6.1): its other
names (aliases) and the names that send to it (redirects), checked against
the workspaces and the other sites of nginx, written by the root helper in
the workspace's site (one more name of its server block; a server block of
their own answering 301), in the certificate once this nginx answers their
challenge; never offered where OmniaCLI does not write the site."""
import json
import re
import shutil
import subprocess

import pytest

from omniacli import workspacehelper, workspaceops, workspaceweb
from omniacli.composehelper import HelperError
from omniacli.config import (ConfigError, Workspace, addresses_follow, environment_names, parse_servers,
                             parse_workspaces)

from .test_app import POST, client, wait_job  # noqa: F401 - the client fixture
from .test_stacktemplates import root  # noqa: F401 - the root fixture
from .test_workspaces import basic, post_as, workspaces  # noqa: F401 - the workspaces fixture
from .test_workspaceweb import DUMP, spec, web_root  # noqa: F401 - the web_root fixture

ACME = {"label": "ACME", "domain": "acme.it", "servers": ["acme-main", "acme-shop"], "main": "main"}


def parse(**change):
    data = {**ACME, **change}
    return parse_workspaces({"acme": data, "beta": {"domain": "beta.it", "servers": ["beta-x"]}})


# --- the names, as the configuration keeps them ------------------------------

def test_aliases_and_redirects_are_kept():
    found = parse(aliases={"main": {"production": ["WWW.Customer.com."]}},
                  redirects={"main": {"production": {"customer.com": "www.customer.com"}},
                             "shop": {"test": {"old-shop.it": "shop.test.acme.it"}}})
    acme = found["acme"]
    assert acme.aliases == {"main": {"production": ["www.customer.com"]}}
    assert acme.redirects["main"]["production"] == {"customer.com": "www.customer.com"}
    assert environment_names(acme, "main", "production") == ["acme.it", "www.customer.com"]
    # written and read again the same
    assert parse_workspaces({"acme": acme.to_dict(), "beta": found["beta"].to_dict()})["acme"] == acme


@pytest.mark.parametrize("change, message", [
    ({"aliases": {"main": {"production": ["*.customer.com"]}}}, "not a host name"),
    ({"aliases": {"main": {"production": ["1.2.3.4"]}}}, "not a host name"),
    ({"aliases": {"main": {"production": ["customer"]}}}, "not a host name"),
    ({"aliases": {"main": {"production": ["stg-20261121.acme.it"]}}}, "names of the stagings"),
    ({"aliases": {"nope": {"production": ["www.customer.com"]}}}, "not an instance"),
    ({"aliases": {"main": {"staging": ["www.customer.com"]}}}, "production or test"),
    ({"aliases": {"main": {"production": "www.customer.com"}}}, "a list of names"),
    ({"aliases": {"main": {"production": [f"n{i}.customer.com" for i in range(21)]}}}, "more than 20"),
    # one of the workspace's own names, of this environment or another
    ({"aliases": {"main": {"production": ["acme.it"]}}}, "is taken: it is a name of main"),
    ({"aliases": {"main": {"production": ["shop.test.acme.it"]}}}, r"is taken: it is a name of shop \(test\)"),
    # another workspace's
    ({"aliases": {"main": {"production": ["x.beta.it"]}}}, "under the domain of another workspace"),
    ({"aliases": {"main": {"production": ["www.customer.com"]}, "shop": {"production": ["www.customer.com"]}}},
     "is taken: it is a name of"),
    ({"aliases": {"main": {"production": ["www.customer.com"]}},
      "redirects": {"main": {"production": {"www.customer.com": "acme.it"}}}}, "a redirect at once"),
    ({"redirects": {"main": {"production": {"a.customer.com": "b.customer.com", "b.customer.com": "acme.it"}}}},
     "goes to a redirect"),
    ({"redirects": {"main": {"production": {"customer.com": "shop.acme.it"}}}}, "no address of main"),
])
def test_a_name_refused(change, message):
    with pytest.raises(ConfigError, match=message):
        parse(**change)


def test_the_names_follow_their_instance():
    """Renamed, an instance's other names go with it, and a redirect to one of
    its workspace names goes to that name's new form; deleted, they go."""
    before = parse(aliases={"shop": {"production": ["www.shop.com"]}},
                   redirects={"shop": {"production": {"shop.com": "shop.acme.it", "old.shop.com": "www.shop.com"}}})["acme"]
    renamed = Workspace("acme", domain="acme.it", servers=["acme-main", "acme-store"], main="main",
                        aliases=before.aliases, redirects=before.redirects)
    after = addresses_follow(before, renamed, "shop", "store")
    assert after.aliases == {"store": {"production": ["www.shop.com"]}}
    assert after.redirects == {"store": {"production": {"shop.com": "store.acme.it", "old.shop.com": "www.shop.com"}}}
    gone = Workspace("acme", domain="acme.it", servers=["acme-main"], main="main",
                     aliases=before.aliases, redirects=before.redirects)
    assert (addresses_follow(before, gone, "shop", None).aliases, addresses_follow(before, gone, "shop", None).redirects) == ({}, {})


# --- the site --------------------------------------------------------------

SITES = [{"instance": "main", "role": "production", "names": ["acme.it"], "aliases": ["www.customer.com"],
          "http_port": 20000, "websocket_port": None},
         {"instance": "main", "role": "test", "names": ["test.acme.it"], "http_port": 20002, "websocket_port": None}]
REDIRECTS = [{"names": ["customer.com", "customer.it"], "to": "www.customer.com"}]


def test_the_site_with_aliases_and_redirects():
    http = workspaceweb.conf_text("acme", SITES, None, ipv6=False, redirects=REDIRECTS)
    assert "server_name acme.it www.customer.com test.acme.it;" in http
    # without a certificate yet: the redirect's names answer the challenges, the rest 503
    block = http.split("# customer.com customer.it → www.customer.com")[1]
    assert "server_name customer.com customer.it;" in block and "acme-challenge" in block and "return 503;" in block
    assert "listen 443" not in http
    https = workspaceweb.conf_text("acme", SITES, ("/c/fullchain.pem", "/c/privkey.pem"), ipv6=True, redirects=REDIRECTS)
    # http straight to the address it goes to, the same path; https too
    assert https.count("return 301 https://www.customer.com$request_uri;") == 2
    assert https.count("server_name customer.com customer.it;") == 2 and https.count("listen [::]:443 ssl;") == 3
    production = https.split("# main production")[1].split("# main test")[0]
    assert "server_name acme.it www.customer.com;" in production and "proxy_pass http://127.0.0.1:20000;" in production
    # http to https on every other name: the fixed sentence of the dialog, no switch
    assert "return 301 https://$host$request_uri;" in https


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker is not installed")
def test_nginx_accepts_the_site(tmp_path):
    """nginx -t of the real nginx (a throwaway container) on the site written."""
    if subprocess.run(["docker", "image", "inspect", "nginx:stable-alpine"], capture_output=True).returncode:
        pytest.skip("no nginx:stable-alpine image here (docker pull nginx:stable-alpine)")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(tmp_path / "k.pem"),
                    "-out", str(tmp_path / "c.pem"), "-days", "1", "-subj", "/CN=acme.it"], check=True, capture_output=True)
    text = workspaceweb.conf_text("acme", SITES, ("/etc/nginx/c.pem", "/etc/nginx/k.pem"), ipv6=False,
                                  omniacli="127.0.0.1:8090", redirects=REDIRECTS)
    (tmp_path / "site.conf").write_text("server_names_hash_bucket_size 256;\n" + text)
    done = subprocess.run(["docker", "run", "--rm", "--network", "none",
                           "-v", f"{tmp_path / 'site.conf'}:/etc/nginx/conf.d/default.conf:ro",
                           "-v", f"{tmp_path / 'c.pem'}:/etc/nginx/c.pem:ro", "-v", f"{tmp_path / 'k.pem'}:/etc/nginx/k.pem:ro",
                           "nginx:stable-alpine", "nginx", "-t"], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr


def addresses_spec(**change) -> str:
    return spec(**{"aliases": {"main": {"production": ["www.customer.com"]}},
                   "redirects": {"main": {"production": {"customer.com": "www.customer.com"}}}, **change})


def test_helper_writes_the_other_names(web_root, monkeypatch):
    storage, state = str(web_root["storage"]), web_root["web"]
    reached = {"www.customer.com"}
    monkeypatch.setattr(workspacehelper, "_reaches_here", lambda host: host in reached)
    made = workspacehelper.web("acme", storage, addresses_spec())
    assert made["names"] == ["acme.it", "test.acme.it", "www.customer.com"]
    assert made["redirects"] == {"customer.com": "www.customer.com", **{"main.acme.it": "acme.it", "main.test.acme.it": "test.acme.it"}}
    # customer.com is in DNS but this nginx does not answer it: out of the certificate, said
    certbot = [c for c in state["commands"] if c[0].endswith("certbot")][0]
    assert [certbot[i + 1] for i, a in enumerate(certbot) if a == "-d"] == ["acme.it", "test.acme.it", "www.customer.com"]
    assert made["elsewhere"] == ["customer.com", "main.acme.it", "main.test.acme.it"] and made["without_dns"] == []
    site = web_root["site"].read_text()
    assert "server_name acme.it www.customer.com;" in site and "return 301 https://www.customer.com$request_uri;" in site
    # no DNS record at all: out, as the workspace's names
    state["dns"], state["commands"] = {"acme.it", "test.acme.it"}, []
    made = workspacehelper.web("acme", storage, addresses_spec())
    assert made["without_dns"] == ["www.customer.com", "customer.com", "main.acme.it", "main.test.acme.it"] and made["elsewhere"] == []


def test_helper_redirect_to_a_name_gone(web_root, monkeypatch):
    """A redirect to a name its environment no longer has goes to its first
    name, and the job says so (the main instance changed by hand)."""
    monkeypatch.setattr(workspacehelper, "_reaches_here", lambda host: True)
    made = workspacehelper.web("acme", str(web_root["storage"]),
                               addresses_spec(aliases={}, redirects={"main": {"production": {"customer.com": "old.acme.it"}}}))
    assert made["redirects"] == {"customer.com": "acme.it", **{"main.acme.it": "acme.it", "main.test.acme.it": "test.acme.it"}} and "it goes to acme.it" in made["redirects_moved"][0]


@pytest.mark.parametrize("change, message", [
    ({"aliases": {"main": {"production": ["*.customer.com"]}}}, "not a host name"),
    ({"aliases": {"main": {"production": ["acme.it"]}}}, "a name of the workspace already"),
    ({"redirects": {"main": {"production": {"test.acme.it": "acme.it"}}}}, "it is no redirect"),
    ({"aliases": {"main": {"production": ["x.it"], "test": ["x.it"]}}}, r"x\.it is a name of main and of main \(test\)"),
    ({"aliases": {"main": {"production": ["x;y.it"]}}}, "not a host name"),
    ({"aliases": {"main": {"production": ["www.other.it"]}}}, r"other sites of nginx answer on: www\.other\.it"),
    ({"redirects": {"main": {"production": {"a.wild.it": "acme.it"}}}}, r"a\.wild\.it \(\*\.wild\.it"),
])
def test_helper_refuses_a_name(web_root, monkeypatch, change, message):
    monkeypatch.setattr(workspacehelper, "_reaches_here", lambda host: True)
    with pytest.raises(HelperError, match=message):
        workspacehelper.web("acme", str(web_root["storage"]), addresses_spec(**change))
    assert not web_root["site"].exists()


def test_helper_puts_the_site_back_when_nginx_refuses(web_root, monkeypatch):
    monkeypatch.setattr(workspacehelper, "_reaches_here", lambda host: True)
    storage, state = str(web_root["storage"]), web_root["web"]
    workspacehelper.web("acme", storage, spec())
    before = web_root["site"].read_text()
    state["nginx_t"] = 1
    with pytest.raises(HelperError, match="nginx -t refused"):
        workspacehelper.web("acme", storage, addresses_spec())
    assert web_root["site"].read_text() == before and "customer" not in before


def test_helper_names_of_the_other_sites(web_root):
    found = workspacehelper.web_taken("acme")
    assert "www.other.it" in found["elsewhere"] and "acme.it" not in found["elsewhere"]
    with pytest.raises(HelperError, match="not a workspace name"):
        workspacehelper.web_taken("../etc")
    assert workspacehelper.ACTIONS["web-names"][1] == 1


def test_the_probe_of_a_name(web_root, monkeypatch):
    """_reaches_here: the token put in the challenges' folder comes back."""
    asked = []

    class Answer:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, size):
            return self.body

    def urlopen(request, timeout):
        asked.append(request.full_url)
        token = request.full_url.rsplit("/", 1)[1]
        path = web_root["tmp"] / "acme-challenges" / ".well-known" / "acme-challenge" / token
        return Answer(path.read_bytes() if "//www.yes.it/" in request.full_url else b"other")
    monkeypatch.setattr(workspacehelper.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(workspacehelper, "PROBE_WAIT", 0)
    assert workspacehelper._reaches_here("www.yes.it") and not workspacehelper._reaches_here("www.no.it")
    # a name nginx takes only after its reload: the first answers are another server's
    late = []

    def urlopen_late(request, timeout):
        late.append(request.full_url)
        if len(late) < 3:
            raise OSError("404 from the default server")
        token = request.full_url.rsplit("/", 1)[1]
        path = web_root["tmp"] / "acme-challenges" / ".well-known" / "acme-challenge" / token
        return Answer(path.read_bytes())
    monkeypatch.setattr(workspacehelper.urllib.request, "urlopen", urlopen_late)
    assert workspacehelper._reaches_here("www.late.it") and len(late) == 3
    assert asked[0].startswith("http://www.yes.it/.well-known/acme-challenge/")
    assert not list((web_root["tmp"] / "acme-challenges" / ".well-known" / "acme-challenge").iterdir())


def test_dns_state():
    here = {"203.0.113.7"}
    assert workspaceweb.dns_state([], here) == "none"
    assert workspaceweb.dns_state(["203.0.113.7", "2001:db8::1"], here) == "here"
    assert workspaceweb.dns_state(["198.51.100.1"], here) == "elsewhere"
    assert workspaceweb.dns_state(["198.51.100.1"], set()) == "unknown"


# --- the dialog --------------------------------------------------------------

def docker_server(tmp_path, instance: str, roles=("production", "test")) -> dict:
    return {"type": "docker", **{role: {
        "database": instance.replace("-", "_"), "odoo_service": "odoo", "db_service": "db", "http_port": 20000 + i,
        "restart": {"type": "compose", "service": str(tmp_path / "acme" / instance / role), "sudo": True},
        "repositories": []} for i, role in enumerate(roles)}}


@pytest.fixture
def dialog(workspaces, env, monkeypatch):
    """acme (domain acme.it, mario its admin, luca a user) with its instance
    main (production and test, compose) beside srv (adopted: systemd); the
    helper's web-names, the DNS and the publication faked."""
    settings, tmp = env["settings"], env["tmp"]
    settings.servers.update(parse_servers({"acme-main": docker_server(tmp, "main")}, tmp))
    ws = settings.workspaces["acme"]
    ws.servers, ws.domain, ws.main, ws.storage = ["srv", "acme-main"], "acme.it", "main", "main"
    settings.workspaces["beta"].domain = "beta.it"
    state = {"published": [], "asked": [], "dns": {"www.customer.com": ["203.0.113.7"], "customer.com": ["198.51.100.1"]},
             "elsewhere": {"www.other.it": "/etc/nginx/sites-enabled/other"}}

    async def ask(*args, timeout=None):
        state["asked"].append(args)
        return {"elsewhere": state["elsewhere"]}

    async def publish(job, w):
        state["published"].append((dict(w.aliases), dict(w.redirects)))
        return {}
    monkeypatch.setattr(workspaceops, "ask", ask)
    monkeypatch.setattr(workspaceops, "publish", publish)
    monkeypatch.setattr(workspaceweb, "addresses_of", lambda host: state["dns"].get(host, []))
    monkeypatch.setattr(workspaceweb, "machine_addresses", lambda ip: {"203.0.113.7"})
    return state


API = "/api/servers/acme-main/production/addresses"


async def test_the_dialog_reads_and_checks(dialog, client):
    got = (await client.get(API, headers=basic("mario"))).json()
    assert got == {"workspace": "acme", "main": "acme.it", "workspace_names": ["acme.it"], "aliases": [], "redirects": [],
                   "max": 20}
    test = (await client.get("/api/servers/acme-main/test/addresses", headers=basic("mario"))).json()
    assert test["main"] == "test.acme.it"
    names = ["www.customer.com", "customer.com", "new.customer.com", "*.customer.com", "www.other.it",
             "x.beta.it", "test.acme.it"]
    rows = (await client.post(f"{API}/check", headers=post_as("mario"), json={"names": names})).json()["rows"]
    by = {r["name"]: r for r in rows}
    assert (by["www.customer.com"]["dns"], by["customer.com"]["dns"], by["new.customer.com"]["dns"]) == ("here", "elsewhere", "none")
    assert by["customer.com"]["addresses"] == ["198.51.100.1"]
    assert "not a host name" in by["*.customer.com"]["error"]
    # another site of nginx, another workspace: a workspace's admin is told
    # only that the name is not available, not that another customer of this
    # machine has it (21/11/2026, FUGA 3); its own workspace's names keep why
    assert by["www.other.it"]["taken"] == "not available"
    assert by["x.beta.it"]["taken"] == "not available"
    assert by["test.acme.it"]["taken"] == "it is a name of main (test)"
    # the machine's admin is told which file, and that it is another workspace's
    rows = (await client.post(f"{API}/check", headers=POST, json={"names": ["www.other.it", "x.beta.it"]})).json()["rows"]
    assert rows[0]["taken"] == "used by /etc/nginx/sites-enabled/other"
    assert rows[1]["taken"] == "it is under the domain of another workspace of this machine"
    # in Italian for an Italian browser
    rows = (await client.post(f"{API}/check", headers={**post_as("mario"), "Accept-Language": "it"},
                              json={"names": ["www.other.it", "*.x.it"]})).json()["rows"]
    assert rows[0]["taken"] == "non disponibile" and "non è un nome di host" in rows[1]["error"]


async def test_the_dialog_saves_and_publishes(dialog, client, env):
    settings = env["settings"]
    body = {"aliases": ["www.customer.com"], "redirects": [{"from": "customer.com", "to": "www.customer.com"}]}
    resp = await client.post(f"{API}/save", headers=post_as("mario"), json=body)
    assert resp.status_code == 200, resp.text
    assert (await wait_job(client, resp.json()["job"]))["status"] == "success"
    acme = settings.workspaces["acme"]
    assert acme.aliases == {"main": {"production": ["www.customer.com"]}}
    assert acme.redirects == {"main": {"production": {"customer.com": "www.customer.com"}}}
    assert dialog["published"][-1] == (acme.aliases, acme.redirects)
    # Names on the web of the workspace lists them, read only (UX-008 D5)
    page = (await client.get("/workspaces/acme", headers=basic("mario"))).text
    assert "www.customer.com ↗" in page and "customer.com → www.customer.com" in page
    # the same again: nothing to publish
    resp = await client.post(f"{API}/save", headers=post_as("mario"), json=body)
    assert resp.json() == {"ok": True}
    # the Names of the workspace saved by its admins keep them
    resp = await client.post("/api/workspaces/acme/web/save", headers=post_as("mario"),
                             json={"main": "main", "subdomains": {"erp": "main"}, "certificate": "letsencrypt"})
    assert resp.status_code == 200, resp.text
    assert settings.workspaces["acme"].aliases == acme.aliases
    # an instance may not be named so that it takes another one's name
    settings.workspaces["acme"].aliases = {"main": {"production": ["shop.acme.it"]}}
    resp = await client.post("/api/workspaces/acme/instances/create", headers=post_as("mario"),
                             json={"instance": "shop", "template": "odooplm-20"})
    assert resp.status_code == 400 and "would answer on shop.acme.it" in resp.json()["error"]
    # everything taken away
    resp = await client.post(f"{API}/save", headers=post_as("mario"), json={"aliases": [], "redirects": []})
    await wait_job(client, resp.json()["job"])
    assert (settings.workspaces["acme"].aliases, settings.workspaces["acme"].redirects) == ({}, {})


@pytest.mark.parametrize("body, message", [
    ({"aliases": ["*.customer.com"], "redirects": []}, "not a host name"),
    # not said to a workspace's admin: another site of this machine has it (FUGA 3)
    ({"aliases": ["www.other.it"], "redirects": []}, "^www.other.it is not available: choose another name$"),
    ({"aliases": ["x.beta.it"], "redirects": []}, "^x.beta.it is not available: choose another name$"),
    ({"aliases": ["a.it", "a.it"], "redirects": []}, "twice"),
    ({"aliases": [], "redirects": [{"from": "customer.com", "to": "www.customer.com"}]}, "no address of main"),
    ({"aliases": ["acme.it"], "redirects": []}, "is taken: it is a name of main"),
    ({"aliases": "www.customer.com", "redirects": []}, "a list of names"),
])
async def test_the_dialog_refuses(dialog, client, env, body, message):
    resp = await client.post(f"{API}/save", headers=post_as("mario"), json=body)
    assert resp.status_code == 400 and re.search(message, resp.json()["error"]), resp.text
    assert env["settings"].workspaces["acme"].aliases == {} and not dialog["published"]


async def test_the_dialog_is_its_admins_only(dialog, client, env):
    # a user of the workspace: neither read nor saved
    assert (await client.get(API, headers=basic("luca"))).status_code == 403
    resp = await client.post(f"{API}/save", headers=post_as("luca"), json={"aliases": ["www.customer.com"], "redirects": []})
    assert resp.status_code == 403
    # another workspace's admin: not its server
    assert (await client.get(API, headers=basic("eva"))).status_code == 403
    # an adopted server (no workspace made it): its nginx is not OmniaCLI's
    resp = await client.get("/api/servers/srv/production/addresses", headers={"Authorization": POST["Authorization"]})
    assert resp.status_code == 400 and "OmniaCLI does not touch nginx here" in resp.json()["error"]
    resp = await client.post("/api/servers/srv/production/addresses/save", headers=POST,
                             json={"aliases": ["www.customer.com"], "redirects": []})
    assert resp.status_code == 400 and not dialog["asked"] and not dialog["published"]
    # a workspace without a domain writes no site
    env["settings"].workspaces["acme"].domain = ""
    resp = await client.get(API, headers=basic("mario"))
    assert resp.status_code == 400 and "no domain yet" in resp.json()["error"]


async def test_the_environments_rows(dialog, client, env):
    settings = env["settings"]
    settings.workspaces["acme"].aliases = {"main": {"production": ["www.customer.com"]}}
    page = (await client.get("/servers/acme-main", headers=basic("mario"))).text
    buttons = re.findall(r'<button type="button" class="small addr-open"[^>]*>', page)
    assert [re.search(r'data-env="([^"]+)"', b).group(1) for b in buttons] == ["acme-main/production", "acme-main/test"]
    assert all('title="' in b for b in buttons) and "www.customer.com" in page
    dialog_html = page.split('<dialog id="addr-dialog"')[1].split("</dialog>")[0]
    assert 'id="addr-save" disabled' in dialog_html and dialog_html.count('class="primary"') == 1
    assert 'type="checkbox"' not in dialog_html and "https://" in dialog_html
    for button in re.findall(r"<button\b[^>]*>", dialog_html):
        assert 'title="' in button, button
    # a user there reads the other names, without the ⚙
    page = (await client.get("/servers/acme-main", headers=basic("luca"))).text
    assert "www.customer.com" in page and "class=\"small addr-open\"" not in page and 'id="addr-dialog"' not in page


async def test_no_dialog_where_omniacli_does_not_write_the_site(dialog, client, env):
    """srv, adopted from a server: its admin sees the ⚙ off, with the reason."""
    page = (await client.get("/servers/srv", headers={"Authorization": POST["Authorization"],
                                                      "Accept-Language": "it"})).text
    assert "class=\"small addr-open\"" not in page and 'id="addr-dialog"' not in page
    off = re.findall(r'<span title="([^"]+)"><button type="button" class="small" disabled[^>]*>⚙', page)
    assert off and off[0] == "Il sito lo gestisce chi amministra la macchina: OmniaCLI non tocca nginx qui"
