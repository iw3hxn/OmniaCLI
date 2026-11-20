# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A new instance of a workspace from a full archive: sent by the browser,
checked part by part, made from its template, its repositories at their
commit (from the archive's code when the address cannot be reached), its
parameters, its database restored in production."""

import hashlib
import io
import json
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

from omniacli import instancearchive, stacktemplates, workspaceops
from omniacli.docker import DockerPlugin
from omniacli.jobs import JobFailed

from .test_app import client, wait_job  # noqa: F401 - the client fixture
from .test_workspaces import basic, post_as, workspaces  # noqa: F401 - the workspaces fixture

TEMPLATE = Path(__file__).parent.parent / "templates" / "odooplm-20"


def make_archive(path: Path, commit: str, tamper: bool = False) -> None:
    """A full archive as instancearchive.make writes one."""
    code = io.BytesIO()
    with tarfile.open(fileobj=code, mode="w:gz") as tar:
        for name, text in (("custom_mod/__manifest__.py", b"{'name': 'x'}\n"), ("README.md", b"hello\n")):
            info = tarfile.TarInfo(name)
            info.size = len(text)
            tar.addfile(info, io.BytesIO(text))
        evil = tarfile.TarInfo("link")
        evil.type, evil.linkname = tarfile.SYMTYPE, "/etc/passwd"
        tar.addfile(evil)
    parts = {
        "backup.zip": b"PK backup",
        "repositories.json": json.dumps([{"name": "custom", "branch": "17.0", "url": "https://github.com/acme/custom.git",
                                          "commit": commit, "code": "code/custom.tar.gz"}]).encode(),
        "code/custom.tar.gz": code.getvalue(),
        "config/instance.json": json.dumps({"template": "odooplm-20", "version": "x", "database": "old"}).encode(),
        "config/omniacli.json": json.dumps({"parameters": {"workers": 2}, "debug": False,
                                            "extra": {"unaccent": "True"}}).encode(),
        "config/pip-freeze.txt": b"requests==2.32.0\n",
    }
    manifest = {"format": 1, "kind": "omniacli instance archive", "server": "old-main", "role": "production",
                "type": "docker", "odoo": "20.0", "files": {
                    n: {"size": len(b), "sha256": hashlib.sha256(b).hexdigest()} for n, b in parts.items()}}
    if tamper:
        parts["backup.zip"] = b"PK changed"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        for name, data in parts.items():
            zf.writestr(name, data)


def test_the_code_of_an_archive_never_leaves_its_folder(tmp_path):
    path = tmp_path / "archive_x.zip"
    make_archive(path, "a" * 40)
    target = tmp_path / "out"
    assert instancearchive.extract_code(path, "code/custom.tar.gz", target) == 2
    assert (target / "custom_mod" / "__manifest__.py").is_file() and not (target / "link").exists()


async def test_an_instance_from_an_archive(workspaces, client, env, monkeypatch, tmp_path):
    settings = env["settings"]
    template = stacktemplates.load_template(TEMPLATE)
    monkeypatch.setattr(workspaceops, "templates", lambda: {template.name: template})
    settings.storage_roots = (str(tmp_path / "storage"),)
    ws = settings.workspaces["acme"]
    ws.storage, ws.limits = str(tmp_path / "storage"), {"disk_gb": 10, "instances": 5}
    calls = {"helper": [], "restore": [], "restart": [], "pip": []}

    async def create_instance(job, ws, instance, tpl, values, setup=None):
        folder = tmp_path / "storage" / ws.name / instance
        for role in ("production", "test"):
            (folder / role / "addons").mkdir(parents=True)
        return {"type": "docker", **{role: {
            "database": instance, "http_port": port, "repositories": [],
            "restart": {"type": "compose", "service": str(folder / role), "sudo": True}}
            for role, port in (("production", 20000), ("test", 20002))}}

    async def nothing(*args, **kwargs):
        return {}

    async def clone(job, ws, instance, role, name, url, branch, git_options=()):
        raise JobFailed("could not read from remote repository")

    async def helper(job, *args):
        calls["helper"].append(args)
        return {}

    async def restore(self, job, inst, backup_zip, options):
        calls["restore"].append((inst.key, backup_zip.read_bytes(), options))

    async def restart(job, inst):
        calls["restart"].append(inst.key)

    async def pip_requirements(self, job, inst, only=None):
        calls["pip"].append((inst.key, only))
    monkeypatch.setattr(workspaceops, "create_instance", create_instance)
    monkeypatch.setattr(workspaceops, "publish", nothing)
    monkeypatch.setattr(workspaceops, "clone", clone)
    monkeypatch.setattr(workspaceops, "_helper", helper)
    monkeypatch.setattr(DockerPlugin, "restore", restore)
    monkeypatch.setattr(DockerPlugin, "backup_problem", lambda self, p: None)
    monkeypatch.setattr(DockerPlugin, "pip_requirements", pip_requirements)
    from omniacli import ops
    monkeypatch.setattr(ops, "restart", restart)

    good, bad = tmp_path / "good.zip", tmp_path / "bad.zip"
    make_archive(good, "c" * 40)
    make_archive(bad, "c" * 40, tamper=True)
    url = "/api/workspaces/acme/"
    # sent by the browser: an archive, or refused
    resp = await client.post(url + "archives/upload?name=plain.zip", headers={**post_as("mario"), "Content-Type": "application/zip"},
                             content=b"PK not an archive")
    assert resp.status_code == 400 and "not a full archive" in resp.json()["error"]
    for path in (good, bad):
        resp = await client.post(url + f"archives/upload?name={path.name}",
                                 headers={**post_as("mario"), "Content-Type": "application/zip"}, content=path.read_bytes())
        assert resp.status_code == 200, resp.text
    listing = (await client.get(url + "archives", headers=basic("mario"))).json()["archives"]
    assert {a["id"] for a in listing} == {"upload/archive_good.zip", "upload/archive_bad.zip"}
    assert (await client.get(url + "archives", headers=basic("luca"))).status_code == 403
    # a part not as its manifest says: nothing made
    resp = await client.post(url + "instances/from-archive", headers=post_as("mario"),
                             json={"instance": "copy", "archive": "upload/archive_bad.zip"})
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "failed" and "not as its manifest says" in job["error"] and "acme-copy" not in settings.servers
    assert "acme-copy" not in settings.workspaces["acme"].servers
    resp = await client.post(url + "instances/from-archive", headers=post_as("mario"),
                             json={"instance": "copy", "archive": "../../etc/x.zip"})
    assert resp.status_code == 404
    resp = await client.post(url + "instances/from-archive", headers=post_as("mario"),
                             json={"instance": "moved", "archive": "upload/archive_good.zip", "neutralize": False})
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success", job["lines"]
    server = settings.servers["acme-moved"]
    # the remote cannot be reached: the code of the archive, a git repository at one commit, its address kept
    repo = server.production.repositories[0]
    assert (repo.name, repo.branch) == ("custom", "17.0") and (repo.path / "custom_mod" / "__manifest__.py").is_file()
    log = subprocess.run(["git", "log", "--format=%s"], cwd=repo.path, capture_output=True, text=True).stdout
    assert "c" * 40 in log
    remote = subprocess.run(["git", "remote", "get-url", "origin"], cwd=repo.path, capture_output=True, text=True).stdout
    assert remote.strip() == "https://github.com/acme/custom.git"
    # the parameters of the archive, with the workspace's bounds, in each role
    confs = [json.loads(a[-1]) for a in calls["helper"] if a[0] == "conf"]
    assert [c["repositories"] for c in confs] == [["custom"], ["custom"]]
    assert confs[0]["parameters"] == {"workers": 2} and confs[0]["extra"] == {"unaccent": "True"}
    # the database in production, not neutralized when asked; test restarted
    assert calls["restore"] == [("acme-moved/production", b"PK backup", {"neutralize": False})]
    assert calls["restart"] == ["acme-moved/test"]
    assert calls["pip"] == [("acme-moved/production", "custom"), ("acme-moved/test", "custom")]
