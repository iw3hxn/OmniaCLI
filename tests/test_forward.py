# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A workspace's names passed to another machine (docs/adr/AD-008-consegna-e-spostamento.md, AD-012, step 3 of
its §9): forward acme|full|off on the machine that has them, hold on the one that receives them; the purge of
what they keep, and the firewall's entry of an instance removed."""
import io
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from omniacli import machinedb, managedreplica, relay, workspacehelper, workspaceops, workspaceweb
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
                 "proxy_set_header Upgrade $http_upgrade;", "proxy_set_header X-Forwarded-For $remote_addr;"):
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


LAB3 = "172.30.0.13"


def test_the_site_that_receives_believes_the_forwarding_machine_only(fwd):
    """After a forward the customer's Odoo logged lab3's address (step 5, 24/11): the site of the workspace that
    receives the names takes the visitor's address from X-Forwarded-For when the request comes from the machine
    that passes them, and from no one else; the forward sends the visitor's own address, not a list it began."""
    storage, site = str(fwd["storage"]), fwd["site"]
    workspacehelper.web("acme", storage, spec(trust=[LAB3]))
    text = site.read_text()
    block = text.split("# main production\n", 1)[1].split("\n}\n", 1)[0]
    assert f"    set_real_ip_from {LAB3};\n    real_ip_header X-Forwarded-For;" in block
    assert text.count(f"set_real_ip_from {LAB3};") == 2 and "set_real_ip_from 127.0.0.1" not in text  # both roles
    assert "proxy_set_header X-Real-IP $remote_addr;" in block  # Odoo reads the visitor's, once nginx took it
    kept = fwd["record"].parent.parent / "forwarded" / "acme.json"
    assert json.loads(kept.read_text()) == {"from": [LAB3]} and oct(kept.stat().st_mode & 0o777) == "0o600"
    # a publication of every day (no trust in its spec) keeps it; [] takes it away
    workspacehelper.web("acme", storage, spec())
    assert site.read_text() == text
    workspacehelper.web("acme", storage, spec(trust=[]))
    assert "set_real_ip_from" not in site.read_text() and not kept.exists()
    for bad in (["127.0.0.1"], ["10.0.0.0/8"], "1.2.3.4", ["x"]):
        with pytest.raises(HelperError, match="trust"):
            workspacehelper.web("acme", storage, spec(trust=bad))
    # the forwarding machine passes $remote_addr, never $proxy_add_x_forwarded_for (a visitor's own header)
    passed = workspaceweb.forward_conf_text("acme", {"to": LAB4, "names": ["acme.it"]}, ("c", "k"), False)
    assert "$proxy_add_x_forwarded_for" not in passed and passed.count("X-Forwarded-For $remote_addr;") == 4


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


def _units_the_helpers_write(monkeypatch, systemd: Path) -> dict[str, str]:
    """{path under /etc/systemd/system: its text} of every unit the package's helpers write there, from their
    own writers: a workspace acme on storage default, the relay's tunnels, the machine's PostgreSQL."""
    mount = Path("/var/lib/omniacli-storage/default/acme")
    image = Path("/var/lib/omniacli-storage/default/.disks/acme.img")
    mount_unit = "var-lib-omniacli\\x2dstorage-default-acme.mount"
    tunnel = dict(relay="r1", host="relay.example.com", ssh_port=22, remote_port=relay.RELAY_PORTS[0],
                  folder="/etc/omniacli/relay/acme")
    units = {
        workspacehelper.FIREWALL_UNIT: workspacehelper.firewall_unit_text(),
        workspacehelper.check_unit_name(mount.parent): workspacehelper.storage_unit_text("default", mount.parent),
        mount_unit: workspacehelper.unit_text(image, mount, "acme"),
        "docker.service.d/omniacli-acme.conf": workspacehelper.docker_dropin_text("acme", mount_unit),
        workspacehelper.slice_name("acme"): workspacehelper.slice_text("acme", 2, 4),
        machinedb.SLICE: machinedb.slice_text(),
        machinedb.MOUNTS_UNIT: machinedb.mounts_unit_text(["/var/lib/omniacli-storage/default"], "/usr/bin/omniacli"),
        relay.unit_name("acme"): relay.tunnel_unit_text("acme", local_port=relay.LOCAL_PORTS[0], **tunnel),
        relay.AGENT_UNIT: relay.agent_unit_text(local_port=22, **tunnel),
    }
    # the Log in as path's service and timer, written by login-path keep on itself
    monkeypatch.setattr(workspacehelper, "SYSTEMD", systemd)
    monkeypatch.setattr(workspacehelper, "_run", lambda *cmd, check=True: "")
    monkeypatch.setattr(workspacehelper, "login_path", lambda action, target: {})
    monkeypatch.setattr(workspacehelper.os, "chown", lambda *args: None)
    workspacehelper._login_path_keep("on")
    for path in workspacehelper._keep_units():
        units[path.name] = path.read_text()
    return units


def test_the_purge_takes_every_unit_the_helpers_wrote(monkeypatch, tmp_path):
    """Every unit the package's helpers write under /etc/systemd/system goes with the purge, the firewall of the
    workspaces among them (25/11/2026: omniacli-workspaces-firewall.service left after apt purge), disabled and
    deleted; stopped too, but a storage's bind, a workspace's disk and a slice, which hold the instances the
    purge leaves running. A unit of anyone else stays. The purge's block runs here, on a copy of the folder,
    with a systemctl that records what it is asked."""
    systemd = tmp_path / "systemd"
    systemd.mkdir()
    units = _units_the_helpers_write(monkeypatch, systemd)
    # every unit text of the code is in the list above: a new one is added there (and so to the purge)
    code = "".join(p.read_text() for p in Path(workspacehelper.__file__).parent.rglob("*.py"))
    assert code.count("[Unit]") == 10, "a unit written by the code that this test does not list"
    (systemd / "docker.service.d").mkdir()
    for name, text in units.items():
        assert text.startswith("# written by omniacli workspace-helper"), name
        (systemd / name).write_text(text)
    foreign = {"odoo.service": "[Unit]\n", "srv-data.mount": "[Mount]\nWhat=/dev/sdb1\n",
               "docker.service.d/override.conf": "[Service]\n",
               "docker.service.d/omniacli-mine.conf": "# my own, not the helper's\n[Unit]\n"}
    for name, text in foreign.items():
        (systemd / name).write_text(text)
    postrm = (Path(workspacehelper.__file__).parent.parent / "packaging" / "debian" / "files" / "postrm").read_text()
    purge = postrm.split("    purge)", 1)[1]
    if "# (begin of the helpers' units)" not in purge:
        pytest.fail(f"the purge has no block for the units the helpers write: {sorted(units)}")
    block = purge.split("# (begin of the helpers' units)\n", 1)[1].split("# (end of the helpers' units)", 1)[0]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "systemctl").write_text('#!/bin/sh\necho "$*" >> "%s"\n' % (tmp_path / "calls"))
    (bin_dir / "systemctl").chmod(0o755)
    script = "set -e\n" + block.replace("/etc/systemd/system", str(systemd))
    done = subprocess.run(["sh", "-c", script], env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    left = sorted(str(p.relative_to(systemd)) for p in systemd.rglob("*") if p.is_file())
    assert left == sorted(foreign), f"left by the purge: {sorted(set(left) - set(foreign))}"
    calls = (tmp_path / "calls").read_text().splitlines()
    held = {workspacehelper.check_unit_name(Path("default")), "var-lib-omniacli\\x2dstorage-default-acme.mount",
            workspacehelper.slice_name("acme"), machinedb.SLICE}
    expected = sorted(f"disable {name}" if name in held else f"disable --now {name}"
                      for name in units if "/" not in name)
    assert sorted(calls) == expected, calls
