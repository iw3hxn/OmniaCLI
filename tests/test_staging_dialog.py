# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The New staging dialog and Rebuild (docs/ux/UX-008 §5.1, §5.2): what the
dialog asks before anything is made, the machine chosen by the machine's
admin only, a staging on another machine through its link (the backup sent
there, the staging made there, its record knowing its production is here),
and Rebuild with its code at the commits of the production."""

import datetime
import json
import re
from pathlib import Path

import httpx
import pytest

from omniacli import ops, stacktemplates, stagings, workspaceops
from omniacli.app import create_app
from omniacli.auth import hash_token
from omniacli.config import LinkTarget, Workspace, parse_settings

from .conftest import USER_HASH, BasicAsSession, clone, commit, sh
from .test_app import AUTH, HOST, POST, client, wait_job  # noqa: F401 - the client fixture
from .test_workspaces import post_as, space_of, workspaces  # noqa: F401 - the workspaces fixture

FAR_TOKEN = "far-admin-token"
GB = workspaceops.GB


class Machines(httpx.AsyncBaseTransport):
    """far.test is another OmniaCLI (an app); old.test one that has only
    Odoo 18; down.test refuses connections."""

    def __init__(self, far):
        self.far = httpx.ASGITransport(app=far)
        self.sent = []

    async def handle_async_request(self, request):
        self.sent.append((request.url.host, request.method, request.url.path))
        if request.url.host == "down.test":
            raise httpx.ConnectError("connection refused", request=request)
        if request.url.host == "old.test":
            return httpx.Response(200, json={"template": False, "labels": ["Odoo 18"], "storage": True,
                                             "docker": True, "workspaces": []})
        return await self.far.handle_async_request(request)


@pytest.fixture
def acme(workspaces, env, monkeypatch, tmp_path):
    """acme-odoo, a production of the workspace acme made from odoo-19 (its
    repositories: addons and odoo, each with a commit only it has), and the
    fakes of the helpers: what each step did is in the list returned."""
    settings = env["settings"]
    template = stacktemplates.load_template(Path(__file__).parent.parent / "templates" / "odoo-19")
    monkeypatch.setattr(workspaceops, "templates", lambda: {template.name: template})
    settings.storage_roots = {"main": str(tmp_path)}
    ws = settings.workspaces["acme"]
    ws.storage, ws.limits = "main", {"disk_gb": 50}
    monkeypatch.setattr(workspaceops, "storage_path", lambda w: tmp_path)
    (tmp_path / "acme" / "odoo").mkdir(parents=True)
    (tmp_path / "acme" / "odoo" / "instance.json").write_text(json.dumps({"template": "odoo-19"}))
    ports = iter(range(20000, 20200, 2))
    steps = Steps()

    def repositories(instance):
        addons = tmp_path / "acme" / instance / "production" / "addons"
        if instance == "odoo":
            found = []
            for name in ("addons", "odoo"):
                path = clone(env["remotes"][name], addons / name, "main")
                commit(path, "prod.txt", f"{name} of production\n", "only in production's clone")
                found.append({"name": name, "path": str(path), "branch": "main"})
            return found
        if stagings.is_staging_name(instance) and not (addons / "addons").exists():
            # a staging: addons on test with a commit of its own, a repository only it has
            path = clone(env["remotes"]["addons"], addons / "addons", "test")
            commit(path, "try.txt", "tried here\n", "only in the staging")
            extra = clone(env["remotes"]["odoo"], addons / "extra", "main")
            return [{"name": "addons", "path": str(path), "branch": "test"},
                    {"name": "extra", "path": str(extra), "branch": "main"}]
        return []

    async def create_instance(job, w, instance, tpl, values, setup=None):
        steps.append(("create", w.name, instance, tpl.name, values))
        port = next(ports)
        repos = repositories(instance) if w.name == "acme" else []
        return {"type": "docker", **{role: {
            "database": instance.replace("-", "_"), "odoo_service": "odoo", "db_service": "db", "http_port": port + i,
            "update_modules": True, "restart": {"type": "compose", "service": str(tmp_path / w.name / instance / role),
                                                "sudo": True},
            "repositories": repos if role == "production" else []} for i, role in enumerate(("production", "test"))}}

    async def publish(job, w):
        return {}

    async def backup_into(job, instance, folder, options):
        steps.append(("backup", instance.key, options))
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "odoo_20261121.zip").write_bytes(b"PK-the-backup")
        return folder / "odoo_20261121.zip"

    async def stop(job, instance):
        steps.append(("stop", instance.key))

    async def space():
        return space_of(500, {"acme": (50, 10)}, root="main")

    async def backup_estimate(self, instance):
        return {"database": 2 * GB, "files": GB // 2}

    async def write_conf(job, w, instance, role, names):
        steps.append(("conf", instance, role, names))
        return {}

    async def fake_clone(job, w, instance, role, name, url, branch, options=()):
        target = workspaceops.addons_folder(w, instance, role) / name
        sh("git", "clone", "-q", "-b", branch, url, str(target))
        steps.append(("clone", instance, name))
        return target
    for name, fake in (("create_instance", create_instance), ("publish", publish), ("space", space),
                       ("write_conf", write_conf), ("clone", fake_clone)):
        monkeypatch.setattr(workspaceops, name, fake)
    monkeypatch.setattr(ops, "backup_into", backup_into)
    monkeypatch.setattr(ops, "stop", stop)
    monkeypatch.setattr(workspaceops, "docker_missing", lambda: None)

    async def restore(self, job, instance, backup_zip, options):
        steps.append(("restore", instance.key, backup_zip.name, backup_zip.read_bytes(), options))

    def patch_plugin(server):
        """The restore and the estimate of the plugin of a workspace's instance, faked."""
        plugin = type(settings.servers[server].production.plugin)
        monkeypatch.setattr(plugin, "restore", restore)
        monkeypatch.setattr(plugin, "backup_estimate", backup_estimate)
    steps.patch_plugin = patch_plugin
    return steps


class Steps(list):
    """What the fakes did, in order; patch_plugin(server) fakes the plugin of
    a workspace's instance (known once one is made)."""


async def make_production(client):
    resp = await client.post("/api/workspaces/acme/instances/create", headers=post_as("mario"),
                             json={"instance": "odoo", "template": "odoo-19"})
    assert (await wait_job(client, resp.json()["job"]))["status"] == "success"


@pytest.fixture
async def two(acme, env, tmp_path):
    """This machine (env's, with acme-odoo) and far, another OmniaCLI with
    the same template and storage for workspaces, linked as far (an admin
    there), old (Odoo 18 only) and down (no answer)."""
    conf = tmp_path / "far-conf"
    conf.mkdir()
    (conf / "servers.yaml").write_text("servers: {}\n")
    far_settings = parse_settings({
        "auth": {"users": {"admin": USER_HASH},
                 "links": {"inaria": {"token": hash_token(FAR_TOKEN), "role": "admin"}}},
        "backup_dir": str(tmp_path / "far-backups"), "jobs_dir": str(tmp_path / "far-conf" / "jobs"),
        "servers_file": "servers.yaml"}, base_dir=conf)
    far_settings.storage_roots = {"main": str(tmp_path / "far")}
    far = create_app(far_settings)
    machines = Machines(far)
    settings = env["settings"]
    settings.link_targets = {name: LinkTarget(name, f"http://{name}.test", FAR_TOKEN) for name in ("far", "old", "down")}
    app = create_app(settings, link_transport=machines)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=BasicAsSession(app)),
                                 base_url=f"http://{HOST}") as c:
        c.far, c.far_settings, c.machines = far, far_settings, machines
        await make_production(c)
        acme.patch_plugin("acme-odoo")
        yield c


async def test_the_new_staging_dialog(two, acme):
    page = (await two.get("/servers/acme-odoo", headers={"Authorization": AUTH})).text
    # the primary of Environments opens a dialog: nothing is posted at the click, no confirm()
    assert re.search(r'class="primary" id="staging-new"[^>]*title="Opens the form', page)
    dialog = page[page.index('<dialog id="staging-dialog"'):]
    dialog = dialog[:dialog.index("</dialog>")]
    assert 'id="sd-create" disabled' in dialog and 'id="sd-cancel"' in dialog
    assert "the outgoing mail is read in the Mail tab" in dialog and 'id="sd-advanced" hidden' in dialog
    assert len(re.findall(r'class="primary', dialog)) == 1
    assert "Make a new staging of" not in page
    # every button and folded line of the dialog says what it does
    for tag in re.findall(r"<(?:button|summary)\b[^>]*>", dialog):
        assert re.search(r'title="[^"]+"', tag), tag
    # what the dialog reads: the size, the address here, the machines that can hold it and why the others not
    form = (await two.get("/api/servers/acme-odoo/stagings/new", headers={"Authorization": AUTH})).json()
    assert form["size"] == 2 * GB + GB // 2 and form["template"] == "odoo-19" and form["workspace"] == "acme"
    today = datetime.date.today().strftime("%Y%m%d")
    assert form["address"] == f"stg-{today}.odoo.acme.example.com"
    assert [m["name"] for m in form["machines"]] == ["far"]
    far = form["machines"][0]
    # the workspace of the same name, made there when it is missing
    assert far["default"] == "acme" and far["workspaces"][0]["new"] is True
    why = {u["name"]: u["why"] for u in form["unlisted"]}
    assert why == {"down": "does not answer", "old": "it has only Odoo 18"}
    # in Italian, the reasons too
    form = (await two.get("/api/servers/acme-odoo/stagings/new",
                          headers={"Authorization": AUTH, "Accept-Language": "it"})).json()
    assert {u["name"]: u["why"] for u in form["unlisted"]} == {"down": "non risponde", "old": "ha solo Odoo 18"}


async def test_a_workspace_admin_makes_it_here_only(two, acme):
    # the dialog is there, the machine is not asked: no link is asked either
    two.machines.sent.clear()
    resp = await two.get("/api/servers/acme-odoo/stagings/new", headers=post_as("mario"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["machines"] == [] and resp.json()["unlisted"] == [] and not two.machines.sent
    assert 'id="staging-dialog"' in (await two.get("/servers/acme-odoo", headers=post_as("mario"))).text
    resp = await two.post("/api/servers/acme-odoo/stagings", headers=post_as("mario"), json={"machine": "far"})
    assert resp.status_code == 403 and "Only an admin of this machine" in resp.json()["error"]
    # a user of acme reads nothing of it
    assert (await two.get("/api/servers/acme-odoo/stagings/new", headers=post_as("luca"))).status_code == 403
    # the routes of the other machine are not open to the users of workspaces
    assert (await two.get("/api/stagings/host", headers=post_as("mario"))).status_code == 403


async def test_a_staging_on_another_machine(two, acme, env):
    # a machine that cannot hold it: refused before anything, with the reason
    resp = await two.post("/api/servers/acme-odoo/stagings", headers=POST, json={"machine": "old"})
    assert resp.status_code == 400 and "old has no template odoo-19" in resp.json()["error"]
    resp = await two.post("/api/servers/acme-odoo/stagings", headers={**POST, "Accept-Language": "it"},
                          json={"machine": "old"})
    assert resp.json()["error"].startswith("old non ha il template odoo-19")
    resp = await two.post("/api/servers/acme-odoo/stagings", headers=POST, json={"machine": "nope"})
    assert resp.status_code == 404
    acme.clear()
    resp = await two.post("/api/servers/acme-odoo/stagings", headers=POST, json={"machine": "far"})
    assert resp.status_code == 200, resp.text
    job = await wait_job(two, resp.json()["job"])
    assert job["status"] == "success", job["lines"]
    today = datetime.date.today().strftime("%Y%m%d")
    name = f"acme-odoo-stg-{today}"
    assert job["result"]["server"] == name and job["result"]["url"] == f"/links/far/servers/{name}"
    # the backup made here, sent there, restored there neutralized; the staging there from the same template
    assert acme[0] == ("backup", "acme-odoo/production", {"no_filestore": False})
    assert ("create", "acme", f"odoo-stg-{today}", "odoo-19", {"_mail_capture": True}) in acme
    assert ("restore", f"{name}/production", "odoo_20261121.zip", b"PK-the-backup", {"neutralize": True}) in acme
    assert ("stop", f"{name}/test") in acme
    assert ("far.test", "POST", "/api/stagings/receive") in two.machines.sent
    assert any(step["id"] == "download" for step in job["result"]["progress"]["steps"])
    far = two.far_settings
    assert name in far.servers and far.workspaces["acme"].servers == [name]
    assert (far.backup_dir / name / "production" / "odoo_20261121.zip").read_bytes() == b"PK-the-backup"
    # there, it knows its production is on another machine
    record = stagings.load(far.servers_file.parent / stagings.FILE)[name]
    assert record["parent"] == "acme-odoo" and record["machine"] and record["backup"] == "odoo_20261121.zip"
    # nothing of it here
    assert name not in env["settings"].servers
    # its page there names the production without a link, and is not rebuilt from there
    far_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=BasicAsSession(two.far)), base_url="http://far.test")
    async with far_client:
        page = (await far_client.get(f"/servers/{name}", headers={"Authorization": AUTH})).text
        assert "<strong>acme-odoo</strong>" in page and 'href="/servers/acme-odoo"' not in page
        assert 'id="staging-rebuild"' not in page and 'id="rebuild-dialog"' not in page
        resp = await far_client.post(f"/api/servers/{name}/stagings/refresh", headers=POST | {"Origin": "http://far.test"})
        assert resp.status_code == 400 and "is on" in resp.json()["error"]


async def test_no_room_on_the_other_machine(two, acme, monkeypatch):
    async def small():
        return space_of(500, {"acme": (50, 49)}, root="main")
    # far has the workspace acme already, nearly full
    two.far_settings.workspaces = {"acme": Workspace("acme", storage="main", limits={"disk_gb": 3})}
    monkeypatch.setattr(workspaceops, "space", small)
    resp = await two.post("/api/servers/acme-odoo/stagings", headers=POST, json={"machine": "far"})
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"] == "There is no room on far: about 5.0 GB are needed, 0.0 GB are free"


async def test_rebuild_with_data_only_or_with_code(two, acme, env):
    resp = await two.post("/api/servers/acme-odoo/stagings", headers=POST)
    assert resp.status_code == 200 and (await wait_job(two, resp.json()["job"]))["status"] == "success"
    name = resp.json()["server"]
    settings = env["settings"]
    page = (await two.get(f"/servers/{name}", headers={"Authorization": AUTH})).text
    assert re.search(r'class="small danger" id="staging-rebuild"[^>]*title="Makes this staging again', page)
    assert "Refresh from production" not in page
    # the note in its head links its production (it pointed at /servers/ before)
    note = page[page.index('<div class="note-box"'):]
    assert note.index('href="/servers/acme-odoo"') < note.index("</div>")
    dialog = page[page.index('<dialog id="rebuild-dialog"'):]
    dialog = dialog[:dialog.index("</dialog>")]
    radios = re.findall(r'<input type="radio" name="rb-what" value="(\w+)"( checked)?', dialog)
    assert radios == [("data", " checked"), ("code", "")]
    assert "The data of now of the staging are lost. The address stays the same." in dialog
    assert 'class="primary danger" id="rb-go"' in dialog
    staging, parent = settings.servers[name].production, settings.servers["acme-odoo"].production
    before = {r.name: sh("git", "rev-parse", "HEAD", cwd=r.path) for r in staging.repositories}
    # data only: the code stays
    acme.clear()
    resp = await two.post(f"/api/servers/{name}/stagings/refresh", headers=POST, json={})
    assert (await wait_job(two, resp.json()["job"]))["status"] == "success"
    assert [s[0] for s in acme] == ["backup", "restore"]
    assert {r.name: sh("git", "rev-parse", "HEAD", cwd=r.path) for r in settings.servers[name].production.repositories} == before
    # data and code: the repositories at the commits of production, then its data
    acme.clear()
    resp = await two.post(f"/api/servers/{name}/stagings/refresh", headers=POST, json={"code": True})
    job = await wait_job(two, resp.json()["job"])
    assert job["status"] == "success", job["lines"]
    assert [s[0] for s in acme] == ["clone", "conf", "backup", "restore"]
    instance = name[len("acme-"):]
    assert ("conf", instance, "production", ["addons", "odoo"]) in acme
    done = settings.servers[name].production
    assert [(r.name, r.branch) for r in done.repositories] == [("addons", "main"), ("odoo", "main")]
    for mine, theirs in zip(done.repositories, parent.repositories):
        assert sh("git", "rev-parse", "HEAD", cwd=mine.path) == sh("git", "rev-parse", "HEAD", cwd=theirs.path)
        assert sh("git", "rev-parse", "--abbrev-ref", "HEAD", cwd=mine.path) == "main"
    # the repository only the staging had is gone
    assert not (Path(staging.repositories[0].path).parent / "extra").exists()
    # a staging made outside a workspace cannot have its code back; not a staging: none
    assert (await two.post("/api/servers/acme-odoo/stagings/refresh", headers=POST, json={"code": True})).status_code == 404


def test_the_estimate_of_a_compose_instance(tmp_path, monkeypatch):
    """A compose environment's backup estimate: the database's size from its
    PostgreSQL, the files from its newest backup with the filestore."""
    import asyncio
    import os

    from omniacli import pgreport
    from omniacli.docker import DockerPlugin

    async def report(instance):
        return {"size": 30_000_000}
    monkeypatch.setattr(pgreport, "report", report)
    folder = tmp_path / "b"
    folder.mkdir()
    (folder / "db_1.zip").write_bytes(b"x" * 10)
    (folder / "db_2.zip").write_bytes(b"x" * 50_000)
    (folder / "db_3_nofilestore.zip").write_bytes(b"x" * 5)
    for i, name in enumerate(("db_1.zip", "db_2.zip", "db_3_nofilestore.zip")):
        os.utime(folder / name, (1000 + i, 1000 + i))

    class Host:
        def backup_folder(self, instance):
            return folder
    plugin = DockerPlugin()
    plugin._host = Host()
    found = asyncio.run(plugin.backup_estimate(object()))
    assert found == {"database": 30_000_000, "files": 0}
    (folder / "db_2.zip").write_bytes(b"x" * 40_000_000)
    os.utime(folder / "db_2.zip", (1001, 1001))
    assert asyncio.run(plugin.backup_estimate(object())) == {"database": 30_000_000, "files": 10_000_000}
    plugin._host = None
    assert asyncio.run(plugin.backup_estimate(object()))["files"] is None
