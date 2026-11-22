# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Modules uploaded as a .zip (README, Modules uploaded as a .zip;
moduleupload.py): the zip checked before anything is written (no path out
of its folder, no link, a module with its manifest, its series), put in
production and test in a folder of their addons path, the module list
updated, installed or updated through the install's rules; a module of the
same name replaced only with its name typed; a staging takes them; the
admins of the instance only."""
import io
import json
import re
import stat
import zipfile

import pytest

from omniacli import composehelper, moduleupload, ops, workspaceops
from omniacli.config import Workspace
from omniacli.docker import DockerPlugin
from omniacli.jobs import Job

from .test_app import client, wait_job  # noqa: F401 - the client fixture
from .test_modules_catalog import modules  # noqa: F401 - the modules fixture
from .test_workspace_repositories import instance  # noqa: F401 - the instance fixture
from .test_workspaces import post_as, workspaces  # noqa: F401 - the workspaces fixture


def manifest(name="Hello", version="19.0.1.0.0", depends=("base",), **more):
    return repr({"name": name, "version": version, "depends": list(depends), "license": "LGPL-3", **more})


def make_zip(files: dict, links: dict | None = None) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)
        for name, target in (links or {}).items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, target)
    return out.getvalue()


HELLO = {"hello_omnia/__manifest__.py": manifest(), "hello_omnia/__init__.py": "",
         "hello_omnia/views/hello.xml": "<odoo/>", "README.md": "not unpacked"}


def check(tmp_path, data, series="19.0"):
    path = tmp_path / "up.zip"
    path.write_bytes(data)
    return moduleupload.check(path, series)


def test_what_a_zip_may_hold(tmp_path):
    found = check(tmp_path, make_zip(HELLO))
    assert found["base"] == "" and [m["module"] for m in found["modules"]] == ["hello_omnia"]
    assert found["modules"][0]["warning"] is None and found["modules"][0]["depends"] == ["base"]
    # GitHub's Download ZIP: one folder at the top, two modules in it
    found = check(tmp_path, make_zip({"repo-main/a_mod/__manifest__.py": manifest("A"),
                                      "repo-main/b_mod/__manifest__.py": manifest("B", version="18.0.1.0.0"),
                                      "repo-main/README.md": "x"}))
    assert found["base"] == "repo-main/" and [m["module"] for m in found["modules"]] == ["a_mod", "b_mod"]
    assert found["modules"][1]["warning"] == ("b_mod: its version 18.0.1.0.0 is for Odoo 18.0, this instance runs "
                                              "Odoo 19.0: it may not install")
    # a version without a series is Odoo's to complete: nothing said
    assert check(tmp_path, make_zip({"m/__manifest__.py": manifest(version="1.0")}))["modules"][0]["warning"] is None
    for files, links, why in (
            ({"../evil/__manifest__.py": manifest()}, None, "goes out of its folder"),
            ({"m/__manifest__.py": manifest(), "m/../../x.py": ""}, None, "goes out of its folder"),
            ({"/etc/m/__manifest__.py": manifest()}, None, "not a path inside the zip"),
            ({"m\\__manifest__.py": manifest()}, None, "not a path inside the zip"),
            ({"m/__manifest__.py": manifest()}, {"m/link": "/etc/passwd"}, "symbolic link"),
            ({"__manifest__.py": manifest()}, None, "at the top of the zip"),
            ({"m/readme.txt": "no manifest"}, None, "holds no Odoo module"),
            ({"Bad-Name/__manifest__.py": manifest()}, None, "not the name of a module"),
            ({"m/__manifest__.py": "__import__('os').system('id')"}, None, "read, never run"),
            ({"m/__manifest__.py": manifest(installable=False)}, None, "not installable")):
        with pytest.raises(moduleupload.Refused, match=re.escape(why)):
            check(tmp_path, make_zip(files, links))
    (tmp_path / "up.zip").write_bytes(b"not a zip")
    with pytest.raises(moduleupload.Refused, match="not a zip"):
        moduleupload.check(tmp_path / "up.zip", "19.0")


def test_the_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(moduleupload, "MAX_FILES", 3)
    with pytest.raises(moduleupload.Refused, match="at most 3"):
        check(tmp_path, make_zip(HELLO))
    monkeypatch.setattr(moduleupload, "MAX_FILES", 5000)
    monkeypatch.setattr(moduleupload, "MAX_UNPACKED", 10)
    with pytest.raises(moduleupload.Refused, match="unpacks to"):
        check(tmp_path, make_zip(HELLO))


def test_only_the_modules_are_unpacked(tmp_path):
    path = tmp_path / "up.zip"
    path.write_bytes(make_zip(HELLO))
    checked = moduleupload.check(path, "19.0")
    staged = tmp_path / "staged"
    assert moduleupload.unpack(path, checked, staged) == ["hello_omnia"]
    assert sorted(p.relative_to(staged).as_posix() for p in staged.rglob("*")) == [
        "hello_omnia", "hello_omnia/__init__.py", "hello_omnia/__manifest__.py", "hello_omnia/views",
        "hello_omnia/views/hello.xml"]
    assert oct((staged / "hello_omnia" / "views").stat().st_mode & 0o777) == "0o755"
    assert oct((staged / "hello_omnia" / "__init__.py").stat().st_mode & 0o777) == "0o644"
    (tmp_path / "addons").mkdir()
    folder = tmp_path / "addons" / moduleupload.FOLDER
    assert moduleupload.put(folder, staged, checked["modules"], "mario", "up.zip", "1") == []
    assert moduleupload.record(folder)["hello_omnia"]["by"] == "mario"
    # a staging's copy, made again from its production's
    copy = tmp_path / "staging" / moduleupload.FOLDER
    assert moduleupload.copy_folder(folder, copy) == ["hello_omnia"]
    assert (copy / "hello_omnia" / "views" / "hello.xml").read_text() == "<odoo/>"
    assert moduleupload.copy_folder(tmp_path / "none", copy) == []


async def test_write_conf_keeps_the_folder_in_the_addons_path(tmp_path, monkeypatch):
    ws = Workspace("acme", storage=str(tmp_path))
    asked = []

    async def helper(job, *args, **kw):
        asked.append(args)
        return {}
    monkeypatch.setattr(workspaceops, "_helper", helper)
    job = Job(id="j", title="t", instances=[], source="s")
    await workspaceops.write_conf(job, ws, "main", "test", ["custom"])
    (workspaceops.addons_folder(ws, "main", "test") / moduleupload.FOLDER).mkdir(parents=True)
    await workspaceops.write_conf(job, ws, "main", "test", ["custom"])
    assert [json.loads(a[-1])["repositories"] for a in asked] == [["custom"], ["custom", moduleupload.FOLDER]]


def test_the_helper_updates_the_module_list_in_the_running_service(tmp_path, monkeypatch):
    monkeypatch.setattr(composehelper, "services", lambda project: ["odoo", "db"])
    [cmd] = composehelper.commands(tmp_path, "update-list", ["odoo", "DB_test"])
    assert cmd[:5] == ["exec", "-T", "odoo", "sh", "-c"] and cmd[-1] == "DB_test" and "DB_test" not in cmd[5]
    assert "update_list()" in composehelper.SCRIPTS["update-list"] and "--no-http" in composehelper.SCRIPTS["update-list"]
    with pytest.raises(composehelper.HelperError, match="usage"):
        composehelper.commands(tmp_path, "update-list", ["odoo"])


@pytest.fixture
def uploads(modules, monkeypatch):
    """acme-main (the modules fixture), its roles' addons there; the module
    list and the update of named modules recorded."""
    for role in ("production", "test"):
        (modules["folder"] / role / "addons").mkdir(parents=True, exist_ok=True)
    calls = modules["calls"]
    calls.update(listed=[], updated=[])

    async def update_module_list(self, job, inst):
        calls["listed"].append(inst.key)

    async def update_named_modules(self, job, inst, names):
        calls["updated"].append((inst.key, names))
    monkeypatch.setattr(DockerPlugin, "update_module_list", update_module_list)
    monkeypatch.setattr(DockerPlugin, "update_named_modules", update_named_modules)
    return modules


async def send(client, data, user="mario", name="hello.zip"):
    return await client.post(f"/api/servers/acme-main/modules/upload?name={name}", headers={
        **post_as(user), "Content-Type": "application/zip"}, content=data)


async def apply(client, ident, user="mario", **body):
    return await client.post(f"/api/servers/acme-main/modules/upload/{ident}/apply", headers=post_as(user), json=body)


async def test_put_in_then_replaced_with_its_name_typed(uploads, client):
    calls, folder = uploads["calls"], uploads["folder"]
    resp = await send(client, make_zip(HELLO))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert [m["module"] for m in data["modules"]] == ["hello_omnia"] and data["replaced"] == [] and data["refused"] == []
    assert data["restarts"] == ["production", "test"] and [r["role"] for r in data["roles"]] == ["production", "test"]
    # nothing is written in the instance before the apply
    assert not (folder / "production" / "addons" / moduleupload.FOLDER).exists()
    resp = await apply(client, data["id"])
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success", job["lines"]
    for role in ("production", "test"):
        module = folder / role / "addons" / moduleupload.FOLDER / "hello_omnia"
        assert (module / "views" / "hello.xml").is_file() and not (module.parent / "README.md").exists()
    assert calls["conf"] == [("production", []), ("test", [])]
    assert calls["restart"] == ["acme-main/production", "acme-main/test"]
    assert calls["listed"] == ["acme-main/production", "acme-main/test"] and calls["install"] == []
    # used: not again
    assert (await apply(client, data["id"])).status_code == 404
    listed = (await client.get("/api/servers/acme-main/modules/uploaded", headers=post_as("mario"))).json()
    assert [(m["module"], m["version"], m["by"]) for m in listed["roles"]["test"]] == [("hello_omnia", "19.0.1.0.0", "mario")]
    # the second version: its name typed to replace it
    second = {**HELLO, "hello_omnia/__manifest__.py": manifest(version="19.0.1.1.0"), "hello_omnia/views/hello.xml": "<odoo>2</odoo>"}
    data = (await send(client, make_zip(second))).json()
    assert data["replaced"] == ["hello_omnia"]
    resp = await apply(client, data["id"])
    assert resp.status_code == 400 and resp.json()["error"] == "Type hello_omnia to replace the modules uploaded before"
    resp = await apply(client, data["id"], replace="hello_omnia", install="test")
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success", job["lines"]
    assert any("hello_omnia replaced" in line for line in job["lines"])
    assert (folder / "test" / "addons" / moduleupload.FOLDER / "hello_omnia" / "views" / "hello.xml").read_text() == "<odoo>2</odoo>"
    assert not [p for p in (folder / "test" / "addons" / moduleupload.FOLDER).iterdir() if ".replaced-" in p.name]
    # not in the addons path twice: its odoo.conf named it already the first time (the fake helper keeps no state)
    assert calls["install"] == [("acme-main/test", ["hello_omnia"])] and calls["backup"] == []
    # installed in test: a third upload into test updates it (-u), in production it is installed after a backup
    data = (await send(client, make_zip(second))).json()
    resp = await apply(client, data["id"], replace="hello_omnia", install="test")
    assert (await wait_job(client, resp.json()["job"]))["status"] == "success"
    assert calls["updated"] == [("acme-main/test", ["hello_omnia"])]
    data = (await send(client, make_zip(second))).json()
    resp = await apply(client, data["id"], replace="hello_omnia", install="production")
    assert resp.status_code == 400 and "confirmation" in resp.json()["error"]
    resp = await apply(client, data["id"], replace="hello_omnia", install="production", confirm=True)
    assert (await wait_job(client, resp.json()["job"]))["status"] == "success"
    assert calls["backup"] == [("acme-main/production", True)] and calls["install"][-1] == ("acme-main/production", ["hello_omnia"])
    # the catalog sees it in the code
    state = (await client.get("/api/servers/acme-main/modules", headers=post_as("mario"))).json()
    assert "hello_omnia" in state["installed"]["production"]


async def test_refused(uploads, client):
    resp = await send(client, make_zip({"../evil/__manifest__.py": manifest()}))
    assert resp.status_code == 400 and "goes out of its folder" in resp.json()["error"]
    resp = await send(client, make_zip(HELLO), name="hello.tar.gz")
    assert resp.status_code == 400 and ".zip" in resp.json()["error"]
    # in Italian
    resp = await client.post("/api/servers/acme-main/modules/upload?name=x.zip", headers={
        **post_as("mario"), "Accept-Language": "it"}, content=make_zip({"m/__manifest__.py": manifest(installable=False)}))
    assert resp.json()["error"] == "m dice di non essere installabile (installable: False)"
    # a module of Odoo itself (its database knows it, no zip brought it): refused at the apply
    data = (await send(client, make_zip({"web/__manifest__.py": manifest("Web")}))).json()
    assert data["refused"] == ["web is a module of Odoo itself or of its image: it is not replaced by a zip"]
    resp = await apply(client, data["id"])
    assert resp.status_code == 400 and "Odoo itself" in resp.json()["error"]
    # a dependency missing: said, not refused
    data = (await send(client, make_zip({"m/__manifest__.py": manifest(depends=("base", "nowhere"))}))).json()
    assert data["refused"] == [] and data["warnings"] == [
        "m depends on nowhere, not in acme-main: installing it fails until they are there"]
    # a user of the workspace, a developer: not theirs (an admin's, as a repository)
    assert (await send(client, make_zip(HELLO), user="luca")).status_code == 403
    uploads["settings"].workspaces["acme"].members["eva"] = "developer"
    assert (await send(client, make_zip(HELLO), user="eva")).status_code == 403
    # an instance outside a workspace (an Odoo adopted): refused with the reason
    resp = await client.post("/api/servers/srv/modules/upload?name=h.zip", headers=post_as("admin"), content=make_zip(HELLO))
    assert resp.status_code == 400 and "does not write" in resp.json()["error"]


async def test_a_module_of_a_repository_is_not_shadowed(uploads, client):
    settings = uploads["settings"]
    repo = uploads["folder"] / "production" / "addons" / "custom"
    (repo / "hello_omnia").mkdir(parents=True)
    (repo / "hello_omnia" / "__manifest__.py").write_text(manifest())
    from omniacli.config import Repository
    settings.servers["acme-main"].production.repositories.append(Repository("custom", repo, "19.0"))
    data = (await send(client, make_zip(HELLO))).json()
    assert data["refused"] == ["hello_omnia is in the repository custom of acme-main already: update it there"]


async def test_the_section_of_the_modules_tab(uploads, client):
    page = (await client.get("/servers/acme-main", headers={**post_as("mario"), "Accept-Language": "it"})).text
    assert "Carica un modulo (.zip)…" in page and 'id="mu-dialog"' in page
    dialog = re.search(r'<dialog id="mu-dialog".*?</dialog>', page, flags=re.S).group(0)
    for button in re.findall(r"<button\b[^>]*>", dialog):
        assert 'title="' in button, button
    assert re.search(r'id="mu-open"[^>]*title="[^"]+"', page)
    # a user of the workspace sees none of it
    page = (await client.get("/servers/acme-main", headers=post_as("luca"))).text
    assert 'id="mu-open"' not in page
