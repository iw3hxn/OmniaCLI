# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A workspace's names passed to another machine (docs/adr/AD-008-consegna-e-spostamento.md, AD-012, step 3 of
its §9): forward acme|full|off on the machine that has them, hold on the one that receives them; the purge of
what they keep, and the firewall's entry of an instance removed."""
import io
import json
import re
from pathlib import Path

import pytest

from omniacli import machinedb, managedreplica, workspacehelper, workspaceops, workspaceweb
from omniacli.composehelper import HelperError

from .test_app import AUTH, POST, client, wait_job  # noqa: F401 - the client fixture
from .test_stacktemplates import root  # noqa: F401 - the root fixture
from .test_workspaces import post_as, workspaces  # noqa: F401 - the workspaces fixture
from .test_workspaceweb import spec, web_root  # noqa: F401 - the web_root fixture

LAB4 = "172.30.0.14"


def test_the_site_of_a_forward_full():
    text = workspaceweb.forward_conf_text("acme", {"to": LAB4, "names": ["erp.acme.it"],
                                                   "moved": {"acme.studio.it": "erp.172-30-0-14.sslip.io"}},
                                          ("/c/fullchain.pem", "/c/privkey.pem"), False)
    # port 80: the challenges found here first, the rest to the other machine's port 80
    assert "server_name erp.acme.it;\n    location /.well-known/acme-challenge/ {" in text
    assert "try_files $uri @omniacli_forward;" in text and f"proxy_pass http://{LAB4};" in text
    # 443: this machine's certificate, the other one's verified for the name asked against the system's CAs
    assert text.count(f"proxy_pass https://{LAB4};") == 2  # the websocket and the rest
    for line in ("proxy_ssl_verify on;", "proxy_ssl_server_name on;", "proxy_ssl_name $host;",
                 f"proxy_ssl_trusted_certificate {workspaceweb.SYSTEM_CA};", "ssl_certificate /c/fullchain.pem;",
                 "proxy_set_header Upgrade $http_upgrade;", "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;"):
        assert line in text, line
    # the provider's name: a 301 to the new one, from 80 and 443
    assert text.count("return 301 https://erp.172-30-0-14.sslip.io$request_uri;") == 2
    # without a certificate here, port 80 only
    assert "listen 443" not in workspaceweb.forward_conf_text("acme", {"to": LAB4, "names": ["erp.acme.it"]}, None, True)
    assert "proxy_pass https://[2001:db8::1];" in workspaceweb.forward_conf_text(
        "acme", {"to": "2001:db8::1", "names": ["a.it"]}, ("c", "k"), False)


@pytest.mark.parametrize("to", ["127.0.0.1", "0.0.0.0", "::1", "169.254.0.1", "a b", "", "http://x.it", "x;y.it"])
def test_what_a_forward_does_not_go_to(to):
    with pytest.raises(ValueError):
        workspaceweb.forward_target(to)


def test_a_held_site_and_the_challenges_passed():
    sites = [{"instance": "main", "role": "production", "names": ["acme.it"], "aliases": ["www.acme.it"],
              "http_port": 20000, "websocket_port": None, "hold": True},
             {"instance": "main", "role": "test", "names": ["test.acme.it"], "http_port": 20002, "websocket_port": None}]
    text = workspaceweb.conf_text("acme", sites, ("c", "k"), False, "127.0.0.1:8090",
                                  acme_to={"to": LAB4, "names": {"www.acme.it"}})
    held = text.split("# main production\n", 1)[1].split("\n}\n", 1)[0]
    # held: the page of the maintenance with the text of a move, Odoo not reached
    assert workspaceweb.HOLD_MARK in held and "return 503" in held and "127.0.0.1:20000" not in held
    assert "Il servizio si sta spostando" in held and "Retry-After" in held
    assert "proxy_pass http://127.0.0.1:20002;" in text
    # the names passed have a port 80 block of their own, whose challenges not found here go to lab4
    blocks = [b for b in text.split("server {") if "listen 80;" in b]
    assert [re.search(r"server_name ([^;]*);", b).group(1) for b in blocks] == ["acme.it test.acme.it", "www.acme.it"]
    assert "try_files" not in blocks[0] and f"proxy_pass http://{LAB4};" in blocks[1]


def forward_in(monkeypatch, mode: str, **values) -> None:
    monkeypatch.setattr(workspacehelper.sys, "stdin", io.StringIO(json.dumps({"web": json.loads(spec()), **values})))


@pytest.fixture
def fwd(web_root, monkeypatch, tmp_path):
    monkeypatch.setattr(workspacehelper, "FORWARD_DIR", tmp_path / "forward")
    workspacehelper.web("acme", str(web_root["storage"]), spec())
    return {**web_root, "record": tmp_path / "forward" / "acme.json", "passed": tmp_path / "nginx" / "omniacli-forward-acme.conf"}


def test_forward_acme_full_off(fwd, monkeypatch):
    storage, site = str(fwd["storage"]), fwd["site"]
    before = site.read_text()
    # acme: the challenges of acme.it this machine has no token for go to lab4
    forward_in(monkeypatch, "acme", to=LAB4, names=["acme.it"])
    made = workspacehelper.forward("acme", storage, "acme")
    record = json.loads(fwd["record"].read_text())
    assert record["mode"] == "acme" and record["names"] == ["acme.it"] and oct(fwd["record"].stat().st_mode & 0o777) == "0o600"
    assert made["forward"] == "acme" and made["site"] is None and not fwd["passed"].exists()
    text = site.read_text()
    assert "server_name acme.it;\n    location /.well-known/acme-challenge/ {" in text and "try_files" in text
    since = record["since"]
    # a publication keeps it
    workspacehelper.web("acme", storage, spec())
    assert site.read_text() == text
    # full: acme.it leaves the workspace's site for the forward's; the provider's main.acme.it moves
    forward_in(monkeypatch, "full", to=LAB4, names=["acme.it"], moved={"main.acme.it": "erp.172-30-0-14.sslip.io"})
    made = workspacehelper.forward("acme", storage, "full")
    assert made["site"] == str(fwd["passed"]) and json.loads(fwd["record"].read_text())["since"] >= since
    own, passed = site.read_text(), fwd["passed"].read_text()
    assert not re.search(r"server_name[^;]*\bacme\.it\b(?!\.)", own.replace("test.acme.it", "")), own
    assert "main.acme.it" not in own and "server_name test.acme.it;" in own
    assert "server_name acme.it;" in passed and "server_name main.acme.it;" in passed and made["names_here"] == ["test.acme.it"]
    # the certificate still covers every name: the forward's 443 serves with it
    certbot = [c for c in fwd["web"]["commands"] if c[0].endswith("certbot")][-1]
    assert {"acme.it", "test.acme.it"} <= {certbot[i + 1] for i, a in enumerate(certbot) if a == "-d"}
    # the clash check does not take the forward's site for another's
    fwd["web"]["dump"] += f"# configuration file {fwd['passed']}:\nserver {{ server_name acme.it main.acme.it; }}\n"
    workspacehelper.web("acme", storage, spec())
    assert site.read_text() == own and fwd["passed"].read_text() == passed
    # nginx -t refuses: both sites and the record as they were
    kept = fwd["record"].read_text()
    fwd["web"]["nginx_t"] = 1
    forward_in(monkeypatch, "off")
    with pytest.raises(HelperError, match="nginx -t refused"):
        workspacehelper.forward("acme", storage, "off")
    assert (site.read_text(), fwd["passed"].read_text(), fwd["record"].read_text()) == (own, passed, kept)
    # off: the site of before, byte for byte; the forward's site and record gone
    fwd["web"]["nginx_t"] = 0
    forward_in(monkeypatch, "off")
    assert workspacehelper.forward("acme", storage, "off")["forward"] == "off"
    assert site.read_text() == before and not fwd["passed"].exists() and not fwd["record"].exists()


@pytest.mark.parametrize("values, message", [
    ({"to": LAB4, "names": ["other.it"]}, "not names of the workspace: other.it"),
    ({"to": LAB4}, "passes names"),
    ({"to": LAB4, "names": ["acme.it"], "moved": {"acme.it": "x.it"}}, "passed and moved at once"),
    ({"to": LAB4, "moved": {"main.acme.it": "test.acme.it"}}, "no other host name"),
    ({"to": "127.0.0.1", "names": ["acme.it"]}, "to: 127.0.0.1 is not the address of another machine"),
    ({"to": LAB4, "names": "acme.it"}, "names: the list"),
])
def test_forward_refusals_keep_what_was(fwd, monkeypatch, values, message):
    before = fwd["site"].read_text()
    forward_in(monkeypatch, "full", **values)
    with pytest.raises(HelperError, match=message):
        workspacehelper.forward("acme", str(fwd["storage"]), "full")
    assert fwd["site"].read_text() == before and not fwd["record"].exists() and not fwd["passed"].exists()
    with pytest.raises(HelperError, match="usage"):
        workspacehelper.forward("acme", str(fwd["storage"]), "on")


def test_hold_is_kept_until_a_spec_says_otherwise(fwd):
    storage, site = str(fwd["storage"]), fwd["site"]
    project = fwd["storage"] / "acme" / "main" / "production"
    made = workspacehelper.web("acme", storage, spec(hold={"main": ["production"]}))
    assert (project / workspacehelper.HOLD_FILE).is_file() and made["held"] == ["acme.it"]
    held = site.read_text().split("# main production\n", 1)[1].split("\n}\n", 1)[0]
    assert "return 503" in held and "127.0.0.1:20000" not in held
    # a publication without hold keeps it (the service's of every day)
    workspacehelper.web("acme", storage, spec())
    assert (project / workspacehelper.HOLD_FILE).is_file() and workspaceweb.HOLD_MARK in site.read_text()
    # nginx -t refuses: the hold of before is back
    fwd["web"]["nginx_t"] = 1
    with pytest.raises(HelperError):
        workspacehelper.web("acme", storage, spec(hold={}))
    assert (project / workspacehelper.HOLD_FILE).is_file()
    fwd["web"]["nginx_t"] = 0
    made = workspacehelper.web("acme", storage, spec(hold={}))
    assert not (project / workspacehelper.HOLD_FILE).exists() and made["held"] == []
    assert workspaceweb.HOLD_MARK not in site.read_text() and "proxy_pass http://127.0.0.1:20000;" in site.read_text()
    for bad in ({"nope": ["production"]}, {"main": ["stage"]}, {"main": "production"}):
        with pytest.raises(HelperError, match="hold:"):
            workspacehelper.web("acme", storage, spec(hold=bad))


def test_drop_takes_the_forward_away(fwd, monkeypatch):
    storage = str(fwd["storage"])
    forward_in(monkeypatch, "full", to=LAB4, names=["acme.it"])
    workspacehelper.forward("acme", storage, "full")
    assert fwd["passed"].exists() and fwd["record"].exists()
    # no names any more: the site goes, the certificate and the forward stay (the forward serves with it)
    fwd["calls"].clear()
    workspacehelper.web("acme", storage, spec(domain=""))
    assert not fwd["site"].exists() and fwd["passed"].exists()
    assert not [c for c in fwd["calls"] if c[:2] == ["certbot", "delete"]]
    monkeypatch.setattr(workspacehelper.os.path, "ismount", lambda path: False)
    workspacehelper.drop("acme", storage)
    assert not fwd["passed"].exists() and not fwd["record"].exists()


def test_an_instance_removed_leaves_no_entry_in_the_firewall(root, monkeypatch, tmp_path):
    """Its own database published for a copy on another machine (port<n> in db-listen.json, AD-004), or
    arriving here (db-arrivals.json, AD-010): removed, the firewall forgets it."""
    storage = root["storage"]
    workspacehelper.disk("acme", str(storage), "2")
    workspacehelper.limits("acme", "0", "0")
    workspacehelper.create("acme", str(storage), "main", "odooplm-20", json.dumps({"_roles": ["production"]}))
    subnet = dict(line.split("=", 1) for line in (storage / "acme" / "main" / "production" / ".env").read_text().splitlines())["SUBNET"]
    listen = tmp_path / "db-listen.json"
    listen.write_text(json.dumps({"port15432": {"from": ["172.30.0.14"], "subnet": subnet}}))
    monkeypatch.setattr(workspacehelper, "DB_LISTEN", listen)
    arrivals = tmp_path / "db-arrivals.json"
    key = managedreplica.dedicated_key("acme", str(storage), "main")
    arrivals.write_text(json.dumps({key: {"project": str(storage / "acme" / "main" / "production")}}))
    monkeypatch.setattr(managedreplica, "ARRIVALS", arrivals)
    # the test's storage is a folder, not a storage's name: its key is not one a machine has
    monkeypatch.setattr(machinedb, "DEDICATED_KEY", re.compile(r".+"))
    monkeypatch.setattr(machinedb, "write", lambda path, text, mode=0o644, **kw: Path(path).write_text(text))
    applied = []
    monkeypatch.setattr(workspacehelper, "db_firewall", lambda: applied.append(True) or {})
    monkeypatch.setattr(workspacehelper.subprocess, "run", lambda cmd, **kw: type("Done", (), {"returncode": 0})())
    workspacehelper.remove("acme", str(storage), "main")
    assert applied == [True] and not arrivals.exists()


async def test_the_routes_of_the_names(client, workspaces, monkeypatch):  # noqa: F811
    """Hidden routes, the machine's admins only: the helper gets the spec on its input, never in its arguments."""
    settings = workspaces
    settings.workspaces["acme"].storage = "/srv/omniacli"
    asked = []

    async def capture(cmd, timeout=None, input=None, **kw):
        asked.append((cmd, input))
        if "forward" in cmd:
            body = json.loads(input)
            return 0, json.dumps({"workspace": "acme", "forward": cmd[-1], "names": body["names"], "moved": body["moved"],
                                  "nginx": "nginx reloaded", "https": True})
        if "web" in cmd:
            return 0, json.dumps({"workspace": "acme", "names": ["acme.example.com"], "https": True, "held": ["acme.example.com"]})
        return 1, "not this command"
    monkeypatch.setattr(workspaceops, "capture", capture)
    url = "/api/workspaces/acme/forward"
    resp = await client.post(url, headers=POST, json={"mode": "full", "to": LAB4, "names": ["acme.example.com"]})
    assert resp.status_code == 200, resp.text
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success", job
    cmd, given = [a for a in asked if "forward" in a[0]][0]
    assert cmd[-4:] == ["forward", "acme", "/srv/omniacli", "full"] and LAB4 not in cmd
    assert json.loads(given)["to"] == LAB4 and json.loads(given)["web"]["domain"] == "acme.example.com"
    assert any(f"acme.example.com → {LAB4}" in line for line in job["lines"])
    for body, code in (({"mode": "on"}, 400), ({"mode": "full", "to": "127.0.0.1", "names": []}, 400),
                       ({"mode": "acme", "to": LAB4, "names": "x"}, 400)):
        assert (await client.post(url, headers=POST, json=body)).status_code == code
    assert (await client.post("/api/workspaces/nope/forward", headers=POST, json={"mode": "off"})).status_code == 404
    # a workspace's own admin is no admin of the machine: the route is none of the workspaces'
    assert (await client.post(url, headers=post_as("mario"), json={"mode": "off"})).status_code == 401
    # hold: the spec of web with hold, the environments of the workspace's instances only
    settings.workspaces["acme"].servers = ["srv", "acme-main"]
    resp = await client.post("/api/workspaces/acme/hold", headers=POST, json={"environments": {"main": ["production"]}})
    assert resp.status_code == 200, resp.text
    job = await wait_job(client, resp.json()["job"])
    web = [json.loads(a[0][-1]) for a in asked if "web" in a[0]][-1]
    assert web["hold"] == {"main": ["production"]}
    assert any("Held until the switch, the maintenance page only: acme.example.com" in line for line in job["lines"])
    for held in ({"nope": ["production"]}, {"main": ["stage"]}, ["main"]):
        resp = await client.post("/api/workspaces/acme/hold", headers=POST, json={"environments": held})
        assert resp.status_code == 400, held
    assert (await client.post("/api/workspaces/acme/hold", headers=post_as("mario"),
                              json={"environments": {}})).status_code == 401


def test_the_purge_takes_what_the_names_and_the_firewall_kept():
    """Every file of root's under /etc/omniacli that the code writes goes with the package's purge."""
    postrm = (Path(workspacehelper.__file__).parent.parent / "packaging" / "debian" / "files" / "postrm").read_text()
    purge = postrm.split("    purge)", 1)[1]
    assert "rm -f /etc/nginx/conf.d/omniacli-forward-*.conf" in purge and "rm -rf /etc/omniacli/forward" in purge
    assert "/etc/omniacli/certs/*" in purge
    for kept in ("db-listen", "db-remote", "mail-guard", "machine-db-replica", "dedicated-replica"):
        assert kept in purge.split("for kept in ", 1)[1].split(";", 1)[0], kept
    code = "".join(p.read_text() for p in (Path(workspacehelper.__file__).parent).glob("*.py"))
    written = set(re.findall(r'"/etc/omniacli/([a-z][a-z0-9-]*(?:\.json)?)"', code))
    # the machine's PostgreSQL stays with the purge (its data, as the backups): its registry with it
    left = {"machine-db.json"}
    missing = [w for w in sorted(written - left) if w.removesuffix(".json") not in purge]
    assert not missing, missing
