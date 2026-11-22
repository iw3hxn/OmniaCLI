# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The deploys of an instance: each job that moved its commits, recorded
with them, kept in a file of its own; going back to one, with a backup
first; the automatic deploys paused, a push then deploying nothing."""

import hashlib
import hmac
import json
import stat

import pytest

from omniacli import deployhooks, deploys, ops, stagings
from omniacli.config import parse_servers, server_to_dict
from omniacli.jobs import Job

from .conftest import clone, commit, sh
from .test_app import AUTH, POST, client, wait_job  # noqa: F401 - the client fixture
from .test_workspaces import basic, post_as, workspaces  # noqa: F401 - the workspaces fixture


def push(env, tmp_path, name, branch, message) -> str:
    """A commit pushed to the branch of the remote name; its sha."""
    work = tmp_path / f"push-{name}-{branch}"
    if not work.exists():
        sh("git", "clone", "-q", "-b", branch, str(env["remotes"][name]), str(work))
    sh("git", "pull", "-q", "origin", branch, cwd=work)
    sha = commit(work, f"{message}.txt", message, message)
    sh("git", "push", "-q", "origin", f"HEAD:{branch}", cwd=work)
    return sha


def test_the_commit_read_from_the_files(env):
    repo = env["settings"].servers["srv"].test.repositories[0]
    assert deploys.head_file(repo.path) == sh("git", "rev-parse", "HEAD", cwd=repo.path)
    sh("git", "pack-refs", "--all", cwd=repo.path)
    assert deploys.head_file(repo.path) == sh("git", "rev-parse", "HEAD", cwd=repo.path)
    sh("git", "checkout", "-q", "--detach", cwd=repo.path)
    assert deploys.head_file(repo.path) == sh("git", "rev-parse", "HEAD", cwd=repo.path)
    assert deploys.head_file(env["tmp"]) is None


async def test_a_job_that_moves_the_code_is_a_deploy(env, tmp_path):
    instance = env["settings"].servers["srv"].test
    folder = tmp_path / "deploys"
    found = {"srv/test": instance}
    recorder = deploys.Recorder(folder, found.get)
    job = Job("j1", "Git pull srv/test", ["srv/test"], "manual: admin", started="2026-11-22T10:00:00")
    await recorder.begin(job)
    first = push(env, tmp_path, "addons", "test", "one")
    second = push(env, tmp_path, "addons", "test", "two")
    addons = next(r for r in instance.repositories if r.name == "addons")
    before = sh("git", "rev-parse", "HEAD", cwd=addons.path)
    sh("git", "pull", "-q", "--ff-only", "origin", "test", cwd=addons.path)
    job.notes["backups"] = {"srv/test": "pre-update_x.zip"}
    job.status, job.finished = "success", "2026-11-22T10:01:00"
    await recorder.end(job)
    data = deploys.load(folder, "srv/test")
    assert stat.S_IMODE(deploys.file_of(folder, "srv/test").stat().st_mode) == 0o600
    [entry] = data["deploys"]
    [row] = entry["repositories"]
    assert (row["name"], row["before"], row["after"], row["brought"], row["taken"]) == ("addons", before, second, 2, 0)
    assert row["subject"] == "two" and [c["subject"] for c in row["commits"]] == ["two", "one"]
    assert entry["backup"] == "pre-update_x.zip" and entry["by"] == "manual: admin"
    assert set(entry["state"]) == {"odoo", "addons"} and first != second
    # a job that changes nothing is not a deploy
    again = Job("j2", "Backup srv/test", ["srv/test"], "manual: admin", started="x")
    await recorder.begin(again)
    again.status = "success"
    await recorder.end(again)
    assert len(deploys.load(folder, "srv/test")["deploys"]) == 1
    # the last KEEP only
    for i in range(deploys.KEEP + 5):
        deploys.record(folder, "srv/test", {"id": f"x{i}"})
    assert len(deploys.load(folder, "srv/test")["deploys"]) == deploys.KEEP
    # the instance taken away: its history with it
    gone = Job("j3", "Delete srv", ["srv/test"], "manual: admin", started="x")
    await recorder.begin(gone)
    found.clear()
    gone.status = "success"
    await recorder.end(gone)
    assert not deploys.file_of(folder, "srv/test").exists()


async def test_three_deploys_back_to_the_first_and_a_push(workspaces, client, env, tmp_path):
    settings = env["settings"]
    instance = settings.servers["srv"].test
    addons = next(r for r in instance.repositories if r.name == "addons")
    api = "/api/servers/srv/test/deploys"
    assert (await client.get(api, headers={"Authorization": AUTH})).json()["deploys"] == []
    shas = []
    for message in ("v1", "v2", "v3"):
        shas.append(push(env, tmp_path, "addons", "test", message))
        resp = await client.post("/servers/srv/test/pull-restart", headers=POST, follow_redirects=False)
        job_id = resp.headers["location"].rsplit("/", 1)[-1]
        assert (await wait_job(client, job_id))["status"] == "success"
    data = (await client.get(api, headers={"Authorization": AUTH})).json()
    assert [d["repositories"][0]["after"] for d in data["deploys"]] == shas[::-1]
    assert [d["current"] for d in data["deploys"]] == [True, False, False]
    assert data["deploys"][0]["repositories"][0]["subject"] == "v3"
    first = data["deploys"][-1]
    plan = (await client.get(f"{api}/{first['id']}/back", headers={"Authorization": AUTH})).json()
    [row] = [r for r in plan["repositories"] if r["change"]]
    assert (row["name"], row["to"], row["taken"]) == ("addons", shas[0], 2)
    assert [c["subject"] for c in row["taken_commits"]] == ["v3", "v2"] and plan["kept"] == [] and not plan["refused"]
    assert plan["restore_backup"]["before"] and plan["pause_role"] == "developer"
    assert not plan["ci"] and plan["automatic"]
    # a user of the workspace may not go back (its role there is user)
    assert (await client.post(f"{api}/{first['id']}/back", headers=post_as("luca"), json={})).status_code == 403
    # its CI on: going back switches it off first
    hook = (await client.post("/api/servers/srv/test/deploy-hook", headers=POST, json={"enabled": True})).json()
    assert hook["enabled"] and hook["secret"] and hook["follows"] == [{"name": "odoo", "branch": "test"},
                                                                       {"name": "addons", "branch": "test"}]
    assert (await client.get(f"{api}/{first['id']}/back", headers={"Authorization": AUTH})).json()["ci"]
    # back to the first, the CI off: the commit of then, the modules updated, a restart; a backup first
    resp = await client.post(f"{api}/{first['id']}/back", headers=POST, json={"pause": True})
    assert resp.status_code == 200, resp.text
    done = await wait_job(client, resp.json()["job"])
    assert done["status"] == "success", done["lines"]
    assert sh("git", "rev-parse", "HEAD", cwd=addons.path) == shas[0]
    assert sh("git", "rev-parse", "--abbrev-ref", "HEAD", cwd=addons.path) == "test"
    assert any("backup before the update" in line for line in done["lines"])
    data = (await client.get(api, headers={"Authorization": AUTH})).json()
    assert len(data["deploys"]) == 4 and data["deploys"][0]["back_to"] == first["id"]
    assert data["deploys"][0]["current"] and data["deploys"][-1]["current"] is False
    assert data["deploys"][0]["backup"]["kept"] and data["paused"]["by"] == "admin"
    assert data["deploys"][0]["repositories"][0]["taken"] == 2
    # the code is there already: refused
    assert (await client.post(f"{api}/{data['deploys'][0]['id']}/back", headers=POST, json={})).status_code == 400
    # a push while the CI is off deploys nothing
    assert not (await client.get("/api/servers/srv/test/deploy-hook", headers={"Authorization": AUTH})).json()["enabled"]
    sh("git", "remote", "set-url", addons.remote, "https://github.com/acme/addons.git", cwd=addons.path)
    body = json.dumps({"ref": "refs/heads/test", "repository": {"clone_url": "https://github.com/acme/addons.git"}}).encode()
    sign = "sha256=" + hmac.new(hook["secret"].encode(), body, hashlib.sha256).hexdigest()
    resp = await client.post("/hooks/deploy/srv/test", content=body, headers={"X-Hub-Signature-256": sign})
    assert resp.status_code == 200 and resp.json()["paused"] and "CI of srv/test is off" in resp.json()["note"]
    assert "is off" in deployhooks.load(settings.servers_file.parent / deployhooks.FILE)["srv/test"]["last"]["result"]
    # on again: a push deploys the head of the branch again
    view = (await client.post("/api/servers/srv/test/deploy-hook", headers=POST, json={"enabled": True})).json()
    assert view["enabled"] and view["secret"] is None
    resp = await client.post("/hooks/deploy/srv/test", content=body, headers={"X-Hub-Signature-256": sign})
    assert resp.status_code == 202, resp.text
    await wait_job(client, resp.json()["job"])
    # the remote is not reachable at its new address: the pull fails, the code stays where it was
    assert sh("git", "rev-parse", "HEAD", cwd=addons.path) == shas[0]
    sh("git", "remote", "set-url", addons.remote, str(env["remotes"]["addons"]), cwd=addons.path)
    resp = await client.post("/servers/srv/test/pull-restart", headers=POST, follow_redirects=False)
    await wait_job(client, resp.headers["location"].rsplit("/", 1)[-1])
    assert sh("git", "rev-parse", "HEAD", cwd=addons.path) == shas[2]
    # the admin of the workspace reads the deploys of its instance, who started them in its words; its user not
    seen = (await client.get(api, headers=basic("mario"))).json()
    assert seen["deploys"] and all("admin" not in d["by"] for d in seen["deploys"]) and seen["can_back"]
    assert (await client.get(api, headers=basic("luca"))).status_code == 403
    assert (await client.post("/api/servers/srv/test/deploy-hook", headers=post_as("luca"),
                              json={"enabled": False})).status_code == 403


async def test_back_is_refused_with_local_changes(client, env, tmp_path):
    instance = env["settings"].servers["srv"].test
    addons = next(r for r in instance.repositories if r.name == "addons")
    push(env, tmp_path, "addons", "test", "v1")
    resp = await client.post("/servers/srv/test/pull-restart", headers=POST, follow_redirects=False)
    await wait_job(client, resp.headers["location"].rsplit("/", 1)[-1])
    push(env, tmp_path, "addons", "test", "v2")
    resp = await client.post("/servers/srv/test/pull-restart", headers=POST, follow_redirects=False)
    await wait_job(client, resp.headers["location"].rsplit("/", 1)[-1])
    api = "/api/servers/srv/test/deploys"
    first = (await client.get(api, headers={"Authorization": AUTH})).json()["deploys"][-1]
    (addons.path / "base.txt").write_text("changed here\n")
    plan = (await client.get(f"{api}/{first['id']}/back", headers={"Authorization": AUTH})).json()
    assert plan["refused"] and "uncommitted" in plan["refused"][0]
    resp = await client.post(f"{api}/{first['id']}/back", headers=POST, json={})
    assert resp.status_code == 400 and "uncommitted" in resp.json()["error"]
    assert (await client.get(f"{api}/nope/back", headers={"Authorization": AUTH})).status_code == 404


# --- a production is updated by hand only: the CI runs on its test and its stagings (Carlo, 22/11/2026)

GITHUB = "https://github.com/acme/addons.git"


@pytest.fixture
def staged(env, tmp_path):
    """srv's production on main with an old webhook of deploy on push (as before the rule), its addons at
    GITHUB; stg a staging of it here, on main too, with its CI on."""
    settings = env["settings"]
    data = server_to_dict(settings.servers["srv"])
    data.pop("test", None)
    data["production"]["repositories"] = [
        {**r, "path": str(clone(env["remotes"][r["name"]], tmp_path / "stg" / r["name"], "main"))}
        for r in data["production"]["repositories"]]
    data["production"]["restart"] = {**data["production"]["restart"], "service": "odoo-stg"}
    settings.servers["stg"] = parse_servers({"stg": data}, settings.logs_dir)["stg"]
    stagings.save((settings.links_file or settings.servers_file).parent / stagings.FILE,
                  {"stg": {"parent": "srv", "created": "2026-11-22T09:00:00"}})
    for server in ("srv", "stg"):
        addons = next(r for r in settings.servers[server].production.repositories if r.name == "addons")
        sh("git", "remote", "set-url", addons.remote, GITHUB, cwd=addons.path)
    deployhooks.save(settings.servers_file.parent / deployhooks.FILE,
                     {"srv/production": {"secret": "old-secret-of-production", "by": "admin"},
                      "stg/production": {"secret": "secret-of-the-staging", "by": "admin"}})
    return settings


async def test_a_push_reaches_the_stagings_not_the_production(staged, client, env, tmp_path, monkeypatch):
    pulled, real_pull_restart = [], ops.pull_restart

    async def pull_restart(job, inst, only_if_changed=False, only=None, backup_folder=None):
        pulled.append((inst.key, only))
    monkeypatch.setattr(ops, "pull_restart", pull_restart)
    prod = staged.servers["srv"].production
    addons = next(r for r in prod.repositories if r.name == "addons")
    before = sh("git", "rev-parse", "HEAD", cwd=addons.path)
    sha = push(env, tmp_path, "addons", "main", "fix the invoice")
    body = json.dumps({"ref": "refs/heads/main", "after": sha, "repository": {"clone_url": GITHUB},
                       "head_commit": {"id": sha, "message": "fix the invoice\n\nmore"}}).encode()
    resp = await client.post("/hooks/deploy/srv/production", content=body,
                             headers={"X-Gitlab-Token": "old-secret-of-production"})
    assert resp.status_code == 202, resp.text
    assert "updated by hand only" in resp.json()["note"]
    await wait_job(client, resp.json()["job"])
    # the staging deployed (that is the CI), the production untouched
    assert pulled == [("stg/production", {"addons"})]
    assert sh("git", "rev-parse", "HEAD", cwd=addons.path) == before
    # listed on the production: available, not deployed; on its page
    data = (await client.get("/api/servers/srv/production/deploys", headers={"Authorization": AUTH})).json()
    [available] = data["available"]
    assert (available["name"], available["branch"], available["sha"], available["subject"]) == (
        "addons", "main", sha[:10], "fix the invoice")
    assert data["deploys"][0]["kind"] == "available" and data["deploys"][0]["outcome"] == "available"
    page = (await client.get("/servers/srv", headers={"Authorization": AUTH})).text
    assert f"New commit available on addons/main: {sha[:10]} fix the invoice" in page
    # Update by hand: the line goes
    monkeypatch.setattr(ops, "pull_restart", real_pull_restart)
    sh("git", "remote", "set-url", addons.remote, str(env["remotes"]["addons"]), cwd=addons.path)
    resp = await client.post("/servers/srv/production/pull-restart", headers=POST, follow_redirects=False)
    await wait_job(client, resp.headers["location"].rsplit("/", 1)[-1])
    assert sh("git", "rev-parse", "HEAD", cwd=addons.path) == sha
    assert (await client.get("/api/servers/srv/production/deploys",
                             headers={"Authorization": AUTH})).json()["available"] == []
    # a staging whose CI is off is not deployed by its production's push
    monkeypatch.setattr(ops, "pull_restart", pull_restart)
    await client.post("/api/servers/stg/production/deploy-hook", headers=POST, json={"enabled": False})
    resp = await client.post("/hooks/deploy/srv/production", content=body,
                             headers={"X-Gitlab-Token": "old-secret-of-production"})
    assert resp.status_code == 200 and len(pulled) == 1
    # a push to the staging's own webhook: the staging deploys, its production hears of the commit
    sh("git", "remote", "set-url", addons.remote, GITHUB, cwd=addons.path)
    await client.post("/api/servers/stg/production/deploy-hook", headers=POST, json={"enabled": True})
    other = push(env, tmp_path, "addons", "main", "second fix")
    body = json.dumps({"ref": "refs/heads/main", "after": other, "repository": {"clone_url": GITHUB}}).encode()
    resp = await client.post("/hooks/deploy/stg/production", content=body,
                             headers={"X-Gitlab-Token": "secret-of-the-staging"})
    assert resp.status_code == 202
    await wait_job(client, resp.json()["job"])
    assert pulled[-1] == ("stg/production", {"addons"})
    assert [a["sha"] for a in (await client.get("/api/servers/srv/production/deploys",
                                                 headers={"Authorization": AUTH})).json()["available"]] == [other[:10]]


async def test_the_production_s_ci_and_auto_update_refused(staged, client, env):
    # the switch: refused on the production, offered on its staging (a staging's instance is a production too)
    resp = await client.post("/api/servers/srv/production/deploy-hook", headers=POST, json={"new_secret": True})
    assert resp.status_code == 400 and "by hand" in resp.json()["error"]
    view = (await client.get("/api/servers/srv/production/deploy-hook", headers={"Authorization": AUTH})).json()
    assert view["manual"] and not view["enabled"] and view["webhook"]
    assert not (await client.get("/api/servers/stg/production/deploy-hook", headers={"Authorization": AUTH})).json()["manual"]
    # the form of the server: no auto update on a production
    data = server_to_dict(staged.servers["srv"])
    data["production"]["auto_update"] = {"enabled": True, "interval_minutes": 10}
    resp = await client.post("/api/servers/save", headers=POST, json={"original": "srv", "name": "srv", "server": data})
    assert resp.status_code == 400 and "by hand" in resp.json()["error"]
    # one set before: it does not run
    updater = client.app.state.updater
    prod = staged.servers["srv"].production
    prod.auto_update.enabled = True
    await updater.check(prod)
    assert "by hand only" in updater.state[prod.key]["message"]
    # told once, in a job of the system, at the start after the upgrade
    told = [j for j in client.app.state.jobs.jobs.values() if j.title == "Productions updated by hand only"]
    assert told and told[0].source == "system: an upgrade of OmniaCLI"
    await wait_job(client, told[0].id)
    assert any("srv/production: a push of its branch is no longer deployed" in line for line in told[0].lines)


def test_the_head_commit_of_a_push():
    github = json.dumps({"after": "a" * 40, "head_commit": {"id": "a" * 40, "message": "one\ntwo"}}).encode()
    assert deployhooks.head_commit(github) == ("a" * 40, "one")
    gitlab = json.dumps({"checkout_sha": "b" * 40, "commits": [{"id": "c" * 40, "message": "x"},
                                                                {"id": "b" * 40, "message": "y"}]}).encode()
    assert deployhooks.head_commit(gitlab) == ("b" * 40, "y")
    bitbucket = json.dumps({"push": {"changes": [{"new": {"target": {"hash": "d" * 40, "message": "z"}}}]}}).encode()
    assert deployhooks.head_commit(bitbucket) == ("d" * 40, "z")
    assert deployhooks.head_commit(b"{}") == ("", "")
    assert deployhooks.head_commit(json.dumps({"after": "0" * 40}).encode())[0] == ""
