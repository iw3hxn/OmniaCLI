# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Odoo Enterprise (docs/ux/UX-012): where its code comes from (an address,
OmniaCLI's own key or a token never on a command line), root's copy of each
version that the instances mount read only, the switch of an instance made
from a template (its override and its addons path, the job, the refusal
while a module of it is installed), a staging that follows its production,
the badge, and who may do what."""
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from omniacli import enterprise, managedreplica, ops, stacktemplates, workspacehelper, workspaceops
from omniacli.composehelper import HelperError
from omniacli.docker import DockerPlugin
from omniacli.jobs import JobFailed

from .test_app import AUTH, client, wait_job  # noqa: F401 - the client fixture
from .test_stacktemplates import root  # noqa: F401 - the helper as root
from .test_workspace_repositories import instance  # noqa: F401 - acme-main, a template's instance
from .test_workspaces import basic, post_as, workspaces  # noqa: F401 - the workspaces fixture

POST = {"Authorization": AUTH, "Origin": "http://omniacli.test"}
REPO_TEMPLATES = Path(__file__).parent.parent / "templates"


# --- where the code comes from ---------------------------------------------------

def test_the_addresses_it_takes():
    assert enterprise.kind_of("git@github.com:odoo/enterprise.git") == "ssh"
    assert enterprise.kind_of("gitstandin@172.30.0.13:enterprise.git") == "ssh"
    assert enterprise.kind_of("ssh://git@git.example.com:2222/odoo/enterprise.git") == "ssh"
    assert enterprise.kind_of("https://github.com/odoo/enterprise.git") == "https"
    for bad in ("http://github.com/odoo/enterprise.git", "https://bot:tok@github.com/odoo/enterprise.git",
                "git@github.com:-oProxyCommand=x", "git@github.com:odoo/../x", "ext::sh -c x", "file:///srv/x",
                "/srv/enterprise", "-u x@github.com:x", "https://github.com/odoo/x.git?u=1"):
        with pytest.raises(enterprise.EnterpriseError, match="not the address"):
            enterprise.kind_of(bad)
    with pytest.raises(enterprise.EnterpriseError, match="the token"):
        enterprise.check("https://github.com/odoo/enterprise.git", "bot", "")
    # an ssh address keeps no user nor token
    assert enterprise.check("git@github.com:odoo/enterprise.git", "bot", "tok") == enterprise.Source(
        "git@github.com:odoo/enterprise.git")


def test_the_source_is_kept_private_and_the_token_never_on_a_command_line(tmp_path):
    path = tmp_path / "enterprise.yaml"
    assert enterprise.load(path) is None
    source = enterprise.check("https://github.com/odoo/enterprise.git", "bot", "s3cret-token")
    enterprise.save(path, source)
    assert oct(path.stat().st_mode & 0o777) == "0o600" and enterprise.load(path) == source
    assert "s3cret-token" not in json.dumps(source.public()) and source.public()["token_set"]
    folder = tmp_path / "enterprise"
    options = enterprise.git_options(source, folder)
    assert not any("s3cret-token" in o for o in options)
    store = folder / enterprise.STORE
    assert oct(store.stat().st_mode & 0o777) == "0o600" and "s3cret-token" in store.read_text()
    assert oct(folder.stat().st_mode & 0o777) == "0o700"
    # the machine's helpers off, ssh and https only
    assert options[:2] == ("-c", "credential.helper=") and "protocol.allow=never" in options
    ssh = enterprise.git_options(enterprise.Source(), folder)
    command = next(o for o in ssh if o.startswith("core.sshCommand="))
    assert f"-i {folder / enterprise.KEY_NAME}" in command and "IdentitiesOnly=yes" in command
    assert "BatchMode=yes" in command and "IdentityAgent=none" in command
    # its own key: never OmniaCLI's git key of the workspaces' repositories
    assert enterprise.KEY_NAME != "id_ed25519_git"


def test_the_versions_of_a_remote():
    out = "\n".join(f"{'a' * 40}\trefs/heads/{b}" for b in ("master", "19.0", "16.0", "saas-17.4", "17.0", "9.0"))
    heads = enterprise.parse_heads(out + "\nnoise")
    assert enterprise.series_of(heads) == ["16.0", "17.0", "19.0"]
    assert enterprise.series_of_template("odoo-19-machine") == "19.0"
    assert enterprise.series_of_template("odooplm-20") == "20.0" and enterprise.series_of_template("custom") is None


# --- what root writes -----------------------------------------------------------

def test_the_override_mounts_it_and_keeps_the_replicas_part(monkeypatch, tmp_path):
    monkeypatch.setattr(enterprise, "ROOT", Path("/var/lib/omniacli-enterprise"))
    ent = {"series": "19.0", "service": "odoo"}
    text = managedreplica.override_text(None, None, ent)
    assert text.startswith(managedreplica.OVERRIDE_MARK)
    assert yaml.safe_load(text) == {"services": {"odoo": {"volumes": ["/var/lib/omniacli-enterprise/19.0:/mnt/enterprise:ro"]}}}
    # with the replica's reads and the dedicated database's port: one service odoo, both parts
    both = managedreplica.override_text("odoo", {"port": 25432}, ent)
    data = yaml.safe_load(both)
    assert data["services"]["odoo"] == {"environment": {"PGSSLROOTCERT": "/etc/odoo/db-replica-ca.pem"},
                                        "volumes": ["/var/lib/omniacli-enterprise/19.0:/mnt/enterprise:ro"]}
    project = tmp_path / "production"
    project.mkdir()
    (project / managedreplica.OVERRIDE).write_text(both)
    assert managedreplica.override_reads(project) == "odoo"
    assert managedreplica.override_listen(project) == {"port": 25432}
    # without Enterprise it is the text of before
    assert managedreplica.override_text("odoo", None) == managedreplica.override_text("odoo", None, None)
    assert managedreplica.override_text(None, None, None) == ""


def test_the_addons_path_has_it_first():
    template = stacktemplates.load_template(REPO_TEMPLATES / "odoo-19")
    text = stacktemplates.conf_text(template, "main", "pw", ["custom"], odoo_enterprise=True)
    assert "addons_path = /mnt/enterprise,/mnt/customer-addons/custom\n" in text
    assert "addons_path = /mnt/customer-addons/custom\n" in stacktemplates.conf_text(template, "main", "pw", ["custom"])


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def copies(monkeypatch, tmp_path):
    """OmniaCLI's working copy of 19.0 (jobs_dir's), root's folder in tmp_path."""
    jobs = tmp_path / "lib" / "jobs"
    jobs.mkdir(parents=True)
    source = enterprise.checkouts_of(jobs) / "19.0"
    (source / "web_enterprise").mkdir(parents=True)
    (source / "web_enterprise" / "__manifest__.py").write_text("{'name': 'Web Enterprise (stand-in)'}\n")
    (source / "web_enterprise" / "static").mkdir()
    (source / "web_enterprise" / "static" / "a.js").write_text("one\n")
    (source / "README.md").write_text("stand-in\n")
    _git("init", "-q", cwd=source)
    _git("add", ".", cwd=source)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "x", cwd=source)
    monkeypatch.setattr(workspacehelper, "_system", lambda config=None: {"jobs_dir": str(jobs)})
    monkeypatch.setattr(enterprise, "ROOT", tmp_path / "root-enterprise")
    # the test is not root: what root owns stays the test's
    monkeypatch.setattr(workspacehelper.os, "chown", lambda *args: None)
    return source


def test_root_copies_the_tree_without_git_in_place(copies):
    found = workspacehelper.enterprise_sync("19.0")
    live = enterprise.ROOT / "19.0"
    assert found["modules"] == 1 and len(found["commit"]) == 40 and found["added"] > 0
    assert (live / "web_enterprise" / "__manifest__.py").is_file() and not (live / ".git").exists()
    assert oct(live.stat().st_mode & 0o777) == "0o755"
    assert oct((live / "README.md").stat().st_mode & 0o777) == "0o644"
    assert enterprise.state("19.0")["commit"] == found["commit"] and list(enterprise.states()) == ["19.0"]
    assert not list(enterprise.ROOT.glob(".19.0.new"))
    # fetched again: the same folder (the instances mount it), the files changed in place
    inode = live.stat().st_ino
    (copies / "web_enterprise" / "static" / "a.js").write_text("two, longer\n")
    (copies / "README.md").unlink()
    (copies / "web_enterprise" / "new.py").write_text("x = 1\n")
    again = workspacehelper.enterprise_sync("19.0")
    assert live.stat().st_ino == inode
    assert (again["changed"], again["removed"]) == (1, 1) and again["added"] == 1
    assert (live / "web_enterprise" / "static" / "a.js").read_text() == "two, longer\n"
    assert not (live / "README.md").exists() and (live / "web_enterprise" / "new.py").is_file()


def test_root_refuses_what_is_no_enterprise_or_points_out(copies):
    os.symlink("/etc/shadow", copies / "web_enterprise" / "shadow")
    with pytest.raises(HelperError, match="refused"):
        workspacehelper.enterprise_sync("19.0")
    assert not (enterprise.ROOT / "19.0").exists() and not list(enterprise.ROOT.glob(".19.0.new"))
    (copies / "web_enterprise" / "shadow").unlink()
    (copies / "web_enterprise" / "__manifest__.py").unlink()
    with pytest.raises(HelperError, match="no Odoo module"):
        workspacehelper.enterprise_sync("19.0")
    for bad in ("19", "../x", "19.0;x"):
        with pytest.raises(HelperError, match="not a version"):
            workspacehelper.enterprise_sync(bad)


def _state(series: str) -> None:
    (enterprise.ROOT / series / "web_enterprise").mkdir(parents=True)
    (enterprise.ROOT / series / "web_enterprise" / "__manifest__.py").write_text("{}\n")
    (enterprise.ROOT / enterprise.STATE).mkdir(exist_ok=True)
    (enterprise.ROOT / enterprise.STATE / f"{series}.json").write_text(
        json.dumps({"series": series, "commit": "c" * 40, "fetched": "2026-11-22T18:00:00", "modules": 1}))


def test_the_switch_of_an_instance_and_a_staging_made_enterprise(root, monkeypatch, tmp_path):
    monkeypatch.setattr(enterprise, "ROOT", tmp_path / "root-enterprise")
    storage = root["storage"]
    workspacehelper.disk("acme", str(storage), "2")
    workspacehelper.limits("acme", "2", "4")
    workspacehelper.create("acme", str(storage), "main", "odooplm-20", "{}")
    folder = storage / "acme" / "main"
    with pytest.raises(HelperError, match="no Odoo Enterprise 20.0 on this machine"):
        workspacehelper.enterprise_switch("acme", str(storage), "main", "on")
    assert "enterprise" not in json.loads((folder / "instance.json").read_text())
    _state("20.0")
    done = workspacehelper.enterprise_switch("acme", str(storage), "main", "on")
    assert done["enterprise"] and done["series"] == "20.0" and done["roles"] == ["production", "test"]
    assert json.loads((folder / "instance.json").read_text())["enterprise"] == {"series": "20.0", "service": "odoo"}
    for role in ("production", "test"):
        override = yaml.safe_load((folder / role / "compose.override.yaml").read_text())
        assert override["services"]["odoo"]["volumes"] == [f"{enterprise.ROOT / '20.0'}:/mnt/enterprise:ro"]
        conf = (folder / role / "config" / "odoo.conf").read_text()
        assert "\naddons_path = /mnt/enterprise" in conf
    # what the replica's code writes again keeps it
    workspacehelper._write_override(folder / "production", "odoo", None)
    assert "/mnt/enterprise:ro" in (folder / "production" / "compose.override.yaml").read_text()
    workspacehelper._write_override(folder / "production", None, None)
    # off: both gone, the admin password of odoo.conf kept
    password = workspacehelper._admin_password((folder / "test" / "config" / "odoo.conf").read_text())
    workspacehelper.enterprise_switch("acme", str(storage), "main", "off")
    assert "enterprise" not in json.loads((folder / "instance.json").read_text())
    assert not (folder / "test" / "compose.override.yaml").exists()
    assert "/mnt/enterprise" not in (folder / "test" / "config" / "odoo.conf").read_text()
    assert workspacehelper._admin_password((folder / "test" / "config" / "odoo.conf").read_text()) == password
    # a staging of an Enterprise production: Enterprise from its first start, of its template's version only
    made = workspacehelper.create("acme", str(storage), "stg", "odooplm-20",
                                  json.dumps({"_roles": ["production"], enterprise.CREATE_KEY: "20.0"}))
    assert made["enterprise"] == "20.0" and enterprise.CREATE_KEY not in made["values"]
    stg = storage / "acme" / "stg" / "production"
    assert "/mnt/enterprise:ro" in (stg / "compose.override.yaml").read_text()
    assert "\naddons_path = /mnt/enterprise" in (stg / "config" / "odoo.conf").read_text()
    with pytest.raises(HelperError, match="another version"):
        workspacehelper.create("acme", str(storage), "other", "odooplm-20", json.dumps({enterprise.CREATE_KEY: "19.0"}))
    assert not (storage / "acme" / "other").exists()
    with pytest.raises(HelperError, match="on or off"):
        workspacehelper.enterprise_switch("acme", str(storage), "main", "maybe")


# --- the panel ------------------------------------------------------------------

@pytest.fixture
def ent(instance, monkeypatch, tmp_path):
    """acme-main made from odoo-19 (its instance.json), Enterprise 19.0 copied
    here; the helper's calls and the modules of its database recorded."""
    monkeypatch.setattr(enterprise, "ROOT", tmp_path / "root-enterprise")
    folder = instance["folder"]
    for role in ("production", "test"):
        (folder / role).mkdir(parents=True, exist_ok=True)
    (folder / "instance.json").write_text(json.dumps({"template": "odoo-19", "database": "main"}))
    calls = {"helper": [], "modules": {}, "run": []}

    async def helper(job, *args, timeout=None, input=None):
        calls["helper"].append(args)
        if args[0] == "enterprise":
            meta = json.loads((folder / "instance.json").read_text())
            if args[-1] == "on":
                meta["enterprise"] = {"series": "19.0", "service": "odoo"}
            else:
                meta.pop("enterprise", None)
            (folder / "instance.json").write_text(json.dumps(meta))
        if args[0] == "enterprise-sync":
            _state(args[1])
            return {"series": args[1], "commit": "d" * 40, "modules": 1, "added": 3, "changed": 0, "removed": 0}
        return {}

    async def known_modules(self, inst):
        if calls["modules"] is None:
            raise JobFailed("connection refused")
        return calls["modules"]

    async def run(job, cmd, **kw):
        calls["run"].append([str(c) for c in cmd])
        return 0
    monkeypatch.setattr(workspaceops, "_helper", helper)
    monkeypatch.setattr(DockerPlugin, "known_modules", known_modules)
    monkeypatch.setattr(ops, "run", run)
    return {**instance, "ent": calls}


async def test_the_switch_on_and_off_of_an_instance(ent, client):
    calls, settings = ent["ent"], ent["settings"]
    # no source and no copy: said before the click
    resp = await client.post("/api/servers/acme-main/enterprise", headers=POST, json={"on": True})
    assert resp.status_code == 400 and "Settings › Odoo Enterprise" in resp.json()["error"]
    _state("19.0")
    page = (await client.get("/servers/acme-main", headers={"Authorization": AUTH})).text
    assert 'data-section="enterprise"' in page and 'id="ent-switch"' in page and 'data-on="1"' in page
    # the copy here, said with its numbers (a key of the row named copy once read dict.copy)
    assert "The copy of 19.0 here: 1 modules, commit cccccccccc, fetched on 22/11/2026 18:00." in " ".join(page.split())
    assert '>Enterprise</span>' not in page
    resp = await client.post("/api/servers/acme-main/enterprise", headers=POST, json={"on": True})
    assert resp.status_code == 200, resp.text
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success", job["lines"]
    assert ("enterprise", "acme", str(settings.workspaces["acme"].storage), "main", "on") in calls["helper"]
    # both roles restarted; nothing installed
    assert ent["calls"]["restart"] == ["acme-main/production", "acme-main/test"]
    assert not any(a[0] == "update" for a in calls["helper"])
    page = (await client.get("/servers/acme-main", headers={"Authorization": AUTH})).text
    assert '>Enterprise</span>' in page and 'id="ent-install"' in page and 'data-on="0"' in page
    listed = (await client.get("/api/servers", headers={"Authorization": AUTH})).json()
    assert next(s for s in listed if s["name"] == "acme-main")["enterprise"] is True
    # off: refused while a module of it is installed, or while the database does not say
    calls["modules"] = {"web_enterprise": "installed", "base": "installed"}
    resp = await client.post("/api/servers/acme-main/enterprise", headers=POST, json={"on": False})
    assert resp.status_code == 409 and "web_enterprise" in resp.json()["error"] and "would not start" in resp.json()["error"]
    calls["modules"] = None
    resp = await client.post("/api/servers/acme-main/enterprise", headers=POST, json={"on": False})
    assert resp.status_code == 409 and "does not say" in resp.json()["error"]
    calls["modules"] = {"web_enterprise": "uninstalled", "base": "installed"}
    resp = await client.post("/api/servers/acme-main/enterprise", headers=POST, json={"on": False})
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success", job["lines"]
    assert ("enterprise", "acme", str(settings.workspaces["acme"].storage), "main", "off") in calls["helper"]
    resp = await client.post("/api/servers/acme-main/enterprise", headers=POST, json={"on": False})
    assert resp.status_code == 400 and "already" in resp.json()["error"]


async def test_install_web_enterprise_is_its_own_action(ent, client, monkeypatch):
    installed = []

    async def install(self, job, inst, modules):
        installed.append((inst.key, modules))
    monkeypatch.setattr(DockerPlugin, "install_modules", install)
    resp = await client.post("/api/servers/acme-main/production/enterprise/install", headers=POST, json={})
    assert resp.status_code == 400 and "switch it on first" in resp.json()["error"]
    _state("19.0")
    folder = ent["folder"]
    (folder / "instance.json").write_text(json.dumps({"template": "odoo-19", "enterprise": {"series": "19.0",
                                                                                            "service": "odoo"}}))
    resp = await client.post("/api/servers/acme-main/production/enterprise/install", headers=POST, json={})
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success" and installed == [("acme-main/production", ["web_enterprise"])]


async def test_the_first_switch_fetches_its_version(ent, client, tmp_path):
    settings, calls = ent["settings"], ent["ent"]
    enterprise.save(enterprise.file_of(settings.servers_file),
                    enterprise.check("https://github.com/odoo/enterprise.git", "bot", "s3cret-token"))
    resp = await client.post("/api/servers/acme-main/enterprise", headers=POST, json={"on": True})
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success", job["lines"]
    clone = next(c for c in calls["run"] if "clone" in c)
    assert clone[clone.index("clone"):] == [
        "clone", "--depth", "1", "--single-branch", "--no-tags", "--branch", "19.0", "--",
        "https://github.com/odoo/enterprise.git", str(enterprise.checkouts_of(settings.jobs_dir) / "19.0")]
    assert calls["helper"][0] == ("enterprise-sync", "19.0")
    # the token: in no command, in no line of the job
    assert not any("s3cret-token" in part for c in calls["run"] for part in c)
    assert "s3cret-token" not in json.dumps(job)


async def test_the_settings_page_and_the_test(ent, client, monkeypatch):
    settings = ent["settings"]
    page = await client.get("/enterprise", headers={"Authorization": AUTH})
    assert page.status_code == 200 and enterprise.DEFAULT_URL in page.text and 'id="ent-makekey"' in page.text
    resp = await client.post("/api/enterprise/test", headers=POST, json={"url": enterprise.DEFAULT_URL})
    assert resp.status_code == 400 and "No SSH key yet" in resp.json()["error"]
    seen = []

    async def capture(cmd, **kw):
        seen.append(cmd)
        return 0, f"{'a' * 40}\trefs/heads/18.0\n{'b' * 40}\trefs/heads/19.0\n{'c' * 40}\trefs/heads/master"
    monkeypatch.setattr(ops, "capture", capture)
    body = {"url": "https://github.com/odoo/enterprise.git", "username": "bot", "token": "s3cret-token"}
    resp = await client.post("/api/enterprise/test", headers=POST, json=body)
    assert resp.json() == {"ok": True, "versions": ["18.0", "19.0"], "branches": 3}
    assert seen[0][-3:] == ["--heads", "--", "https://github.com/odoo/enterprise.git"]
    assert not any("s3cret-token" in part for part in seen[0])
    resp = await client.post("/api/enterprise/save", headers=POST, json=body)
    assert resp.json()["source"] == {"url": body["url"], "kind": "https", "username": "bot", "token_set": True}
    # saved again with the token empty: the one saved stays
    resp = await client.post("/api/enterprise/save", headers=POST, json={**body, "token": ""})
    assert enterprise.load(enterprise.file_of(settings.servers_file)).token == "s3cret-token"
    page = (await client.get("/enterprise", headers={"Authorization": AUTH})).text
    assert "s3cret-token" not in page
    resp = await client.post("/api/enterprise/save", headers=POST, json={"url": "file:///srv/x"})
    assert resp.status_code == 400
    resp = await client.post("/api/enterprise/update", headers=POST, json={"series": "19"})
    assert resp.status_code == 400


async def test_a_workspace_sees_the_badge_and_switches_nothing(ent, client):
    _state("19.0")
    (ent["folder"] / "instance.json").write_text(json.dumps({"template": "odoo-19", "enterprise": {"series": "19.0",
                                                                                                  "service": "odoo"}}))
    mario = {"Authorization": basic("mario")["Authorization"]}
    page = (await client.get("/servers/acme-main", headers=mario)).text
    assert '>Enterprise</span>' in page and 'id="ent-switch"' not in page and 'id="ent-install"' not in page
    assert "root-enterprise" not in page
    listed = (await client.get("/api/servers", headers=mario)).json()
    assert next(s for s in listed if s["name"] == "acme-main")["enterprise"] is True
    for url, body in (("/api/servers/acme-main/enterprise", {"on": False}),
                      ("/api/servers/acme-main/production/enterprise/install", {}),
                      ("/api/enterprise/save", {"url": enterprise.DEFAULT_URL}), ("/api/enterprise/key", {}),
                      ("/api/enterprise/update", {"series": "19.0"})):
        resp = await client.post(url, headers=post_as("mario"), json=body)
        assert resp.status_code == 403, (url, resp.text)
    assert (await client.get("/enterprise", headers=mario)).status_code == 403


async def test_an_odoo_that_was_there_shows_its_enterprise(client, env, tmp_path):
    """An instance adopted with its own odoo.conf: the badge when its addons_path holds web_enterprise, no switch."""
    settings = env["settings"]
    srv = settings.servers["srv"]
    addons = tmp_path / "enterprise"
    (addons / "web_enterprise").mkdir(parents=True)
    (addons / "web_enterprise" / "__manifest__.py").write_text("{}\n")
    conf = tmp_path / "odoo.conf"
    conf.write_text(f"[options]\naddons_path = /nowhere,{addons}\n")
    for inst in (srv.production, srv.test):
        if inst is not None:
            inst.options.odoo_config = conf
    assert any(s["enterprise"] for s in (await client.get("/api/servers", headers={"Authorization": AUTH})).json()
               if s["name"] == "srv") == (srv.production.restart.type in ("process", "systemd"))
    resp = await client.post("/api/servers/srv/enterprise", headers=POST, json={"on": True})
    assert resp.status_code == 400 and "not made from a template" in resp.json()["error"]
