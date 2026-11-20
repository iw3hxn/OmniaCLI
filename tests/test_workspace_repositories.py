# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The customer's repositories of a workspace's instance: https only, cloned
by OmniaCLI into the role's addons/ with the workspace's git credentials,
named in that role's odoo.conf, the role restarted; removed the same way."""
import os
import stat
from pathlib import Path

import pytest

from omniacli import gitcredentials, ops, stacktemplates, workspaceops
from omniacli.config import Repository, Workspace, parse_servers
from omniacli.docker import DockerPlugin
from omniacli.jobs import Job, JobFailed

from .test_app import AUTH, client, wait_job  # noqa: F401 - the client fixture
from .test_workspaces import post_as, workspaces  # noqa: F401 - the workspaces fixture

URL = "https://github.com/acme/odoo-custom.git"


def test_what_a_repository_may_be():
    workspaceops.check_repository("custom", URL, "17.0")
    # over ssh, with OmniaCLI's git key
    workspaceops.check_repository("custom", "git@github.com:acme/x.git", "17.0")
    workspaceops.check_repository("custom", "ssh://git@gitlab.example.com:2222/acme/x.git", "17.0")
    for name, url, branch, message in (
            ("../x", URL, "main", "not a repository name"),
            ("custom", "http://github.com/acme/x.git", "main", "not the address"),
            ("custom", "https://bot:tok@github.com/acme/x.git", "main", "not the address"),
            ("custom", "root@github.com:acme/x.git", "main", "not the address"),
            ("custom", "git@github.com:-oProxyCommand=x", "main", "not the address"),
            ("custom", "git@github.com:acme/../x", "main", "not the address"),
            ("custom", "https://github.com/acme/../x", "main", "not the address"),
            ("custom", "https://github.com/acme/x.git?u=1", "main", "not the address"),
            ("custom", URL, "--upload-pack=x", "not a branch"),
            ("custom", URL, "a..b", "not a branch")):
        with pytest.raises(JobFailed, match=message):
            workspaceops.check_repository(name, url, branch)


def _job():
    return Job(id="j", title="t", instances=[], source="s")


async def test_clone(tmp_path, monkeypatch):
    ws = Workspace("acme", storage=str(tmp_path))
    addons = workspaceops.addons_folder(ws, "main", "test")
    addons.mkdir(parents=True)
    ran = []

    async def run(job, cmd, **kw):
        ran.append(cmd)
        target = cmd[-1]
        os.makedirs(os.path.join(target, ".git", "objects"))
        # git keeps its objects read only
        open(os.path.join(target, ".git", "objects", "o"), "w").close()
        os.chmod(os.path.join(target, ".git", "objects", "o"), stat.S_IRUSR)
        os.chmod(os.path.join(target, ".git", "objects"), stat.S_IRUSR | stat.S_IXUSR)
        if fail:
            raise JobFailed("clone failed")
        return 0
    monkeypatch.setattr(workspaceops, "run", run)
    fail = False
    path = await workspaceops.clone(_job(), ws, "main", "test", "custom", URL, "17.0", ("-c", "a.b=1"))
    assert path == addons / "custom"
    assert ran[0] == ["git", "-c", "a.b=1", "clone", "--branch", "17.0", "--recurse-submodules", "--", URL, str(path)]
    with pytest.raises(JobFailed, match="exists already"):
        await workspaceops.clone(_job(), ws, "main", "test", "custom", URL, "17.0")
    # a clone that fails leaves nothing, its read-only objects included
    fail = True
    with pytest.raises(JobFailed, match="clone failed"):
        await workspaceops.clone(_job(), ws, "main", "test", "other", URL, "17.0")
    assert not (addons / "other").exists()
    workspaceops.remove_clone(path)
    assert not path.exists()


@pytest.fixture
def instance(workspaces, env, tmp_path, monkeypatch):
    """acme-main, an instance of acme on tmp_path, its roles compose projects;
    the clone, the helper's conf and the restart recorded."""
    settings = env["settings"]
    ws = settings.workspaces["acme"]
    ws.storage = str(tmp_path / "storage")
    ws.servers.append("acme-main")
    folder = tmp_path / "storage" / "acme" / "main"
    data = {"type": "docker", "status": "active", **{role: {
        "database": "main", "http_port": port, "repositories": [],
        "restart": {"type": "compose", "service": str(folder / role), "sudo": True}}
        for role, port in (("production", 20000), ("test", 20002))}}
    settings.servers["acme-main"] = parse_servers({"acme-main": data}, settings.logs_dir)["acme-main"]
    settings.credentials_file = tmp_path / "credentials.yaml"
    gitcredentials.save(settings.credentials_file, {"workspace:acme": {"github.com": gitcredentials.check(
        "github.com", "bot", "tok")}})
    calls = {"clone": [], "conf": [], "restart": [], "pip": [], "fail_conf": False}

    async def clone(job, ws, instance, role, name, url, branch, git_options=()):
        calls["clone"].append((instance, role, name, url, branch, bool(git_options)))
        path = workspaceops.addons_folder(ws, instance, role) / name
        path.mkdir(parents=True)
        return path

    async def write_conf(job, ws, instance, role, repositories):
        calls["conf"].append((role, list(repositories)))
        if calls["fail_conf"] and repositories:
            raise JobFailed("the helper refused")
        return {}

    async def restart(job, inst):
        calls["restart"].append(inst.key)

    async def pip_requirements(self, job, inst, only=None):
        calls["pip"].append((inst.key, only))
    monkeypatch.setattr(DockerPlugin, "pip_requirements", pip_requirements)
    monkeypatch.setattr(workspaceops, "clone", clone)
    monkeypatch.setattr(workspaceops, "write_conf", write_conf)
    monkeypatch.setattr(ops, "restart", restart)
    return {"calls": calls, "folder": folder, "settings": settings}


async def test_add_and_remove(instance, client):
    calls, settings = instance["calls"], instance["settings"]
    url = "/api/workspaces/acme/repositories/"
    body = {"server": "acme-main", "url": URL, "production_branch": "17.0", "test_branch": "17.0-dev", "name": "custom"}
    resp = await client.post(url + "add", headers=post_as("mario"), json=body)
    assert resp.status_code == 200, resp.text
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success", job["lines"]
    # cloned into each role on its branch, with the workspace's credentials; both odoo.conf; both restarted
    assert calls["clone"] == [("main", "production", "custom", URL, "17.0", True),
                              ("main", "test", "custom", URL, "17.0-dev", True)]
    assert calls["conf"] == [("production", ["custom"]), ("test", ["custom"])]
    assert calls["restart"] == ["acme-main/production", "acme-main/test"]
    # the repository's Python packages in each role, before the restart
    assert calls["pip"] == [("acme-main/production", "custom"), ("acme-main/test", "custom")]
    server = settings.servers["acme-main"]
    assert [(r.name, r.branch, r.path) for r in server.test.repositories] == [
        ("custom", "17.0-dev", instance["folder"] / "test" / "addons" / "custom")]
    assert [(r.name, r.branch) for r in server.production.repositories] == [("custom", "17.0")]
    # pulled and merged later with the credentials too
    assert server.test.repositories[0].git_options[:3] == ("-c", "credential.helper=", "-c")
    resp = await client.post(url + "add", headers=post_as("mario"), json=body)
    assert resp.status_code == 400 and "already" in resp.json()["error"]
    page = (await client.get("/workspaces/acme", headers=post_as("mario"))).text
    assert "production 17.0, test 17.0-dev" in page
    resp = await client.post(url + "remove", headers=post_as("mario"), json={"server": "acme-main", "name": "custom"})
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success", job["lines"]
    assert calls["conf"][-2:] == [("production", []), ("test", [])]
    server = settings.servers["acme-main"]
    assert server.test.repositories == [] and server.production.repositories == []
    for role in ("production", "test"):
        assert not (instance["folder"] / role / "addons" / "custom").exists()


async def test_a_conf_refused_leaves_nothing(instance, client):
    calls, settings = instance["calls"], instance["settings"]
    calls["fail_conf"] = True
    resp = await client.post("/api/workspaces/acme/repositories/add", headers=post_as("mario"),
                             json={"server": "acme-main", "url": URL, "production_branch": "17.0",
                                   "test_branch": "17.0", "name": "custom"})
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "failed"
    server = settings.servers["acme-main"]
    assert server.production.repositories == [] and server.test.repositories == []
    assert calls["conf"][-2:] == [("production", []), ("test", [])] and calls["restart"] == []
    for role in ("production", "test"):
        assert not (instance["folder"] / role / "addons" / "custom").exists()


async def test_who_and_what_is_refused(instance, client):
    url = "/api/workspaces/acme/repositories/add"
    good = {"server": "acme-main", "url": URL, "production_branch": "17.0", "test_branch": "17.0", "name": "custom"}
    assert (await client.post(url, headers=post_as("luca"), json=good)).status_code == 403
    assert (await client.post(url, headers=post_as("eva"), json=good)).status_code == 403
    for change, code, message in (({"server": "srv"}, 404, "No instance srv"),
                                  ({"test_branch": "--x"}, 400, "not a branch"),
                                  ({"url": "https://bot:tok@github.com/a/b.git"}, 400, "no user or token"),
                                  ({"name": "a/b"}, 400, "not a repository name")):
        resp = await client.post(url, headers=post_as("mario"), json={**good, **change})
        assert resp.status_code == code and message in resp.json()["error"], change
    resp = await client.post("/api/workspaces/acme/repositories/remove", headers=post_as("mario"),
                             json={"server": "acme-main", "name": "nope"})
    assert resp.status_code == 404


async def test_an_instance_with_production_only(instance, client):
    """The instances made now have no test: the repository goes in production
    only, named after its address, on the branch given."""
    calls, settings = instance["calls"], instance["settings"]
    settings.servers["acme-main"].instances.pop("test")
    resp = await client.post("/api/workspaces/acme/repositories/add", headers=post_as("mario"),
                             json={"server": "acme-main", "url": URL, "branch": "17.0"})
    assert resp.status_code == 200, resp.text
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success", job["lines"]
    assert calls["clone"] == [("main", "production", "odoo-custom", URL, "17.0", True)]
    assert calls["restart"] == ["acme-main/production"]
    server = settings.servers["acme-main"]
    assert server.test is None and [(r.name, r.branch) for r in server.production.repositories] == [("odoo-custom", "17.0")]
    resp = await client.post("/api/workspaces/acme/repositories/remove", headers=post_as("mario"),
                             json={"server": "acme-main", "name": "odoo-custom"})
    assert (await wait_job(client, resp.json()["job"]))["status"] == "success"
    assert settings.servers["acme-main"].production.repositories == []
    # the Addons tab of the instance's page: the add form, posting to its workspace
    page = (await client.get("/servers/acme-main", headers=post_as("mario"))).text
    assert 'data-tab="addons"' in page and 'data-ws="/api/workspaces/acme"' in page and 'id="addons-add"' in page



async def test_a_repository_over_ssh(instance, client):
    """git@ addresses: OmniaCLI's git key, shown to be added as a deploy key,
    used by every git command on the workspace's repositories."""
    calls, settings = instance["calls"], instance["settings"]
    settings.servers["acme-main"].instances.pop("test")
    resp = await client.post("/api/workspaces/acme/git-key", headers=post_as("mario"), json={})
    assert resp.status_code == 200, resp.text
    key = resp.json()["public_key"]
    assert key.startswith("ssh-ed25519 ") and gitcredentials.git_key(settings.credentials_file).is_file()
    # the same key the next time
    assert (await client.post("/api/workspaces/acme/git-key", headers=post_as("mario"), json={})).json()["public_key"] == key
    options = gitcredentials.options_for(settings, "acme-main")
    assert any(o.startswith("core.sshCommand=ssh -i ") and "IdentitiesOnly=yes" in o for o in options)
    resp = await client.post("/api/workspaces/acme/repositories/add", headers=post_as("mario"),
                             json={"server": "acme-main", "url": "git@github.com:DueEsseTI/Common.git", "branch": "17.0"})
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success", job["lines"]
    # named after the address, cloned with the key's option
    assert calls["clone"][-1][:5] == ("main", "production", "Common", "git@github.com:DueEsseTI/Common.git", "17.0")
    repo = settings.servers["acme-main"].production.repositories[0]
    assert any(o.startswith("core.sshCommand=") for o in repo.git_options)
    # a server outside the workspaces keeps the machine's own ssh
    assert not any("sshCommand" in o for o in gitcredentials.options_for(settings, "srv-alone"))


async def test_the_odoo_config_tab_of_a_workspaces_instance(instance, client, monkeypatch):
    """Its template's parameters, on the instance's page, posted to the
    workspace's parameters/save."""
    template = stacktemplates.load_template(Path(__file__).parent.parent / "templates" / "odoo-19")
    monkeypatch.setattr(workspaceops, "templates", lambda: {"odoo-19": template})
    meta = instance["folder"] / "instance.json"
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text('{"template": "odoo-19"}')
    page = (await client.get("/servers/acme-main", headers=post_as("mario"))).text
    assert 'data-tab="odoo-config"' in page and 'data-param="workers"' in page and 'id="op-save"' in page


async def test_the_terminal_of_a_workspaces_instance(instance, client):
    """The Terminal console: a shell in the Odoo container through the
    helper, offered in More and opened on any machine (never OmniaCLI's user)
    to the machine's developers; not to the users of workspaces."""
    page = (await client.get("/servers/acme-main", headers={"Authorization": AUTH})).text
    assert "/servers/acme-main/production/terminal" in page
    resp = await client.get("/servers/acme-main/production/terminal", headers={"Authorization": AUTH})
    assert resp.status_code == 200 and "/mnt/customer-addons" in resp.text and "terminal/ws" in resp.text
    assert "/production/terminal" not in (await client.get("/servers/acme-main", headers=post_as("mario"))).text
    assert (await client.get("/servers/acme-main/production/terminal", headers=post_as("mario"))).status_code == 403


def test_the_page_of_the_differences():
    sha_a, sha_b = "a" * 40, "b" * 40
    assert workspaceops.web_url("git@github.com:DueEsseTI/Common.git") == "https://github.com/DueEsseTI/Common"
    assert workspaceops.web_url("ssh://git@gitlab.example.com:2222/acme/x.git") == "https://gitlab.example.com/acme/x"
    # a token in the address never reaches the page
    assert workspaceops.web_url("https://bot:ghp_SECRET@github.com/acme/x.git") == "https://github.com/acme/x"
    assert workspaceops.compare_url(URL, sha_a, sha_b) == f"https://github.com/acme/odoo-custom/compare/{sha_a}...{sha_b}"
    assert workspaceops.compare_url("git@gitlab.com:acme/x.git", sha_a, sha_b) == \
        f"https://gitlab.com/acme/x/-/compare/{sha_a}...{sha_b}"
    # the same commit, or a host whose pages are not known: no link
    assert workspaceops.compare_url(URL, sha_a, sha_a) is None
    assert workspaceops.compare_url("https://git.example.com/acme/x.git", sha_a, sha_b) is None


async def _git(*args, cwd):
    code, out = await ops.capture(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd)
    assert code == 0, out
    return out


async def test_the_branches_and_a_change_of_branch(tmp_path):
    """The Update dialog's data from a real remote, then the copy moved to
    the last commit of its branch and to another branch."""
    origin, copy = tmp_path / "origin", tmp_path / "copy"
    origin.mkdir()
    await _git("init", "-q", "-b", "18.0", cwd=origin)
    await _git("commit", "-q", "--allow-empty", "-m", "one", cwd=origin)
    await _git("clone", "-q", str(origin), str(copy), cwd=tmp_path)
    await _git("commit", "-q", "--allow-empty", "-m", "two", cwd=origin)
    await _git("checkout", "-q", "-b", "19.0", cwd=origin)
    await _git("commit", "-q", "--allow-empty", "-m", "three", cwd=origin)
    repo = Repository("custom", copy, "18.0")
    data = await workspaceops.repository_branches(repo)
    assert data["branch"] == "18.0" and data["commit"] == await _git("rev-parse", "HEAD", cwd=copy)
    assert [b["name"] for b in data["branches"]] == ["18.0", "19.0"]
    assert data["branches"][1]["commit"] == await _git("rev-parse", "19.0", cwd=origin)
    # its own branch: a pull
    assert await workspaceops.update_repository(_job(), repo, "18.0")
    assert await _git("rev-parse", "HEAD", cwd=copy) == await _git("rev-parse", "18.0", cwd=origin)
    assert not await workspaceops.update_repository(_job(), repo, "18.0")
    # another: fetched and checked out, following its remote branch
    assert await workspaceops.update_repository(_job(), repo, "19.0")
    assert await _git("rev-parse", "--abbrev-ref", "HEAD", cwd=copy) == "19.0"
    assert await _git("rev-parse", "--abbrev-ref", "@{upstream}", cwd=copy) == "origin/19.0"
    # uncommitted changes are never overwritten
    await _git("checkout", "-q", "18.0", cwd=copy)
    (copy / "f").write_text("x")
    await _git("add", "f", cwd=copy)
    with pytest.raises(JobFailed, match="uncommitted changes"):
        await workspaceops.update_repository(_job(), Repository("custom", copy, "18.0"), "19.0")
    with pytest.raises(JobFailed, match="not a branch name"):
        await workspaceops.update_repository(_job(), repo, "--upload-pack=x")


async def test_the_update_dialog(instance, client, monkeypatch):
    """Update: the branches read for the dialog, then the repository moved to
    the branch chosen, kept in the servers file, its packages, restarted."""
    calls, settings = instance["calls"], instance["settings"]
    settings.servers["acme-main"].instances.pop("test")
    resp = await client.post("/api/workspaces/acme/repositories/add", headers=post_as("mario"),
                             json={"server": "acme-main", "url": URL, "branch": "18.0", "name": "custom"})
    assert (await wait_job(client, resp.json()["job"]))["status"] == "success"
    old, new = "a" * 40, "b" * 40

    async def branches(repo, timeout=60):
        return {"branch": repo.branch, "commit": old, "url": URL,
                "branches": [{"name": "18.0", "commit": old}, {"name": "19.0", "commit": new}]}
    moved = []

    async def update_repository(job, repo, branch):
        moved.append((repo.name, repo.branch, branch))
        return True
    after = []

    async def after_pull(self, job, inst):
        after.append(inst.key)
    monkeypatch.setattr(workspaceops, "repository_branches", branches)
    monkeypatch.setattr(workspaceops, "update_repository", update_repository)
    monkeypatch.setattr(DockerPlugin, "after_pull", after_pull)
    url = "/api/workspaces/acme/repositories/"
    body = {"server": "acme-main", "name": "custom", "role": "production"}
    data = (await client.post(url + "branches", headers=post_as("mario"), json=body)).json()
    assert data["branches"][0]["compare"] is None
    assert data["branches"][1]["compare"] == f"https://github.com/acme/odoo-custom/compare/{old}...{new}"
    calls["restart"].clear()
    resp = await client.post(url + "update", headers=post_as("mario"), json={**body, "branch": "19.0"})
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success", job["lines"]
    assert moved == [("custom", "18.0", "19.0")]
    assert settings.servers["acme-main"].production.repositories[0].branch == "19.0"
    assert after == calls["restart"] == ["acme-main/production"]
    for bad, status_code in (({**body, "name": "nope"}, 404), ({**body, "role": "test"}, 404),
                             ({**body, "branch": "a..b"}, 400)):
        assert (await client.post(url + "update", headers=post_as("mario"), json=bad)).status_code == status_code
    # the dialog on the Addons tab
    page = (await client.get("/servers/acme-main", headers=post_as("mario"))).text
    assert 'id="addons-update"' in page and 'data-repo-update="custom"' in page
