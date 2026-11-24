# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""GitHub through a fine-grained personal access token (github.py, README
GitHub), against a fake GitHub (httpx.MockTransport): the token saved and
tested, the repositories listed page by page, a repository added from the
list, the webhook made, updated and taken away by the CI switch, a push
deploying a test and never a production, the state written on the commit,
and the token nowhere but in the Authorization header and its 0600 file."""

import hashlib
import hmac
import json
import logging
import stat
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from omniacli import config, deploys, github, gitcredentials, ops, workspaceops
from omniacli.jobs import Job, JobFailed

from .conftest import sh
from .test_app import AUTH, POST, client, wait_job  # noqa: F401 - the client fixture
from .test_workspace_repositories import instance  # noqa: F401 - the instance fixture
from .test_workspaces import post_as, workspaces  # noqa: F401 - the workspaces fixture

TOKEN = "github_pat_11FAKE0token0for0the0tests0only"
REMOTE = "https://github.com/acme/addons.git"


class FakeGitHub:
    """GitHub's API as OmniaCLI uses it, from fixtures, recording each call
    (without the Authorization header: only whether it carried the token)."""

    def __init__(self, repos=105, deny=(), rate=4999):
        self.calls, self.hooks, self.statuses, self.deny, self.rate = [], {}, [], set(deny), rate
        self.next_id = 100
        self.repos = [{"full_name": f"acme/repo{i:03d}", "private": i % 2 == 0, "default_branch": "main",
                       "clone_url": f"https://github.com/acme/repo{i:03d}.git", "archived": False}
                      for i in range(repos - 1)]
        self.repos.insert(0, {"full_name": "acme/addons", "private": True, "default_branch": "main",
                              "clone_url": REMOTE, "archived": False})

    def answer(self, status, data=None, headers=None):
        return httpx.Response(status, json=data if data is not None else {},
                              headers={"X-RateLimit-Remaining": str(self.rate), "X-RateLimit-Limit": "5000",
                                       "X-RateLimit-Reset": "1900000000", **(headers or {})})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        path, query = request.url.path, parse_qs(urlsplit(str(request.url)).query)
        self.calls.append((request.method, path, body, request.headers.get("authorization") == f"Bearer {TOKEN}"))
        assert TOKEN not in str(request.url) and TOKEN not in (request.content or b"").decode()
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return self.answer(401, {"message": "Bad credentials"})
        if self.rate == 0:
            return self.answer(403, {"message": "API rate limit exceeded"})
        if path == "/user":
            return self.answer(200, {"login": "carlo-bot", "type": "User"})
        if path == "/user/orgs":
            return self.answer(200, [{"login": "acme"}])
        if path == "/user/repos":
            page = int((query.get("page") or ["1"])[0])
            rows = self.repos[(page - 1) * 100:page * 100]
            more = page * 100 < len(self.repos)
            link = {"Link": f'<https://api.github.com/user/repos?per_page=100&page={page + 1}>; rel="next"'} if more else {}
            return self.answer(200, rows, link)
        parts = path.split("/")
        if len(parts) < 4 or parts[1] != "repos":
            return self.answer(404, {"message": "Not Found"})
        full, rest = f"{parts[2]}/{parts[3]}", parts[4:]
        if not rest:
            return self.answer(200, {"full_name": full, "default_branch": "main", "private": True,
                                     "clone_url": f"https://github.com/{full}.git"})
        if rest == ["branches"]:
            return self.answer(200, [{"name": n} for n in ("main", "18.0", "19.0", "feature/x")])
        if rest == ["commits"]:
            return self.answer(403 if "contents" in self.deny else 200, [])
        if rest[0] == "hooks":
            if "hooks" in self.deny:
                return self.answer(403, {"message": "Resource not accessible by personal access token"})
            hooks = self.hooks.setdefault(full, {})
            if request.method == "GET" and len(rest) == 1:
                return self.answer(200, list(hooks.values()))
            if request.method == "POST":
                if not body["config"].get("url"):
                    return self.answer(422, {"message": "Validation Failed"})
                self.next_id += 1
                hooks[self.next_id] = {"id": self.next_id, **body}
                return self.answer(201, hooks[self.next_id])
            hook_id = int(rest[1])
            if hook_id not in hooks:
                return self.answer(404, {"message": "Not Found"})
            if request.method == "GET":
                return self.answer(200, hooks[hook_id])
            if request.method == "PATCH":
                hooks[hook_id].update(body)
                return self.answer(200, hooks[hook_id])
            if request.method == "DELETE":
                del hooks[hook_id]
                return httpx.Response(204)
        if rest[0] == "statuses":
            if "statuses" in self.deny:
                return self.answer(403, {"message": "Resource not accessible by personal access token"})
            if rest[1] == "0" * 40:
                return self.answer(422, {"message": "Validation Failed"})
            self.statuses.append((full, rest[1], body))
            return self.answer(201, body)
        return self.answer(404, {"message": "Not Found"})


@pytest.fixture
def fake(monkeypatch):
    found = FakeGitHub()
    monkeypatch.setattr(github, "TRANSPORT", httpx.MockTransport(found))
    return found


@pytest.fixture
def commands(monkeypatch):
    """Every command OmniaCLI runs (git included), to show the token is on none."""
    ran = []
    capture, run = ops.capture, ops.run

    async def capture_seen(cmd, *args, **kwargs):
        ran.append(list(cmd))
        return await capture(cmd, *args, **kwargs)

    async def run_seen(job, cmd, *args, **kwargs):
        ran.append(list(cmd))
        return await run(job, cmd, *args, **kwargs)
    monkeypatch.setattr(ops, "capture", capture_seen)
    monkeypatch.setattr(ops, "run", run_seen)
    return ran


def no_token_anywhere(client, ran, caplog, *texts):
    jobs = client.app.state.jobs
    for job in jobs.jobs.values():
        assert TOKEN not in job.title and all(TOKEN not in line for line in job.lines) and TOKEN not in (job.error or "")
    assert all(TOKEN not in json.dumps(e) for e in client.app.state.access_log.recent())
    assert all(TOKEN not in arg for cmd in ran for arg in cmd)
    assert TOKEN not in caplog.text
    for text in texts:
        assert TOKEN not in text


def test_what_github_is():
    assert github.repository_of("https://github.com/Acme/Addons.git") == ("github.com", "Acme/Addons")
    assert github.repository_of("git@github.com:acme/x.git") == ("github.com", "acme/x")
    assert github.repository_of("https://172.30.0.13:8443/acme/x") == ("172.30.0.13:8443", "acme/x")
    assert github.repository_of("https://bot:tok@github.com/acme/x.git") is None
    assert github.key_of("github.com", "Acme/X") == "github.com/acme/x"
    assert github.api_base("github.com") == "https://api.github.com"
    assert github.api_base("git.acme.it") == "https://git.acme.it/api/v3"
    for url in ("https://<address of this OmniaCLI>", "http://127.0.0.1:8090", "https://172.30.0.13",
                "https://odoo.local", "https://x.lab.test", "https://localhost", "https://intranet"):
        assert github.unreachable(url), url
    assert github.unreachable("https://omnia.example.com") is None
    assert github.unreachable("https://8.8.8.8") is None


async def test_the_token_saved_and_tested(client, env, fake, commands, caplog):
    caplog.set_level(logging.DEBUG)
    api = "/api/credentials/github/"
    scope = {"kind": "server", "name": "srv"}
    # a wrong token: GitHub's refusal, said for a person; nothing saved
    resp = await client.post(api + "save", headers=POST, json={**scope, "label": "Acme", "token": "wrong"})
    assert resp.status_code == 400 and "wrong, expired or revoked" in resp.json()["error"]
    resp = await client.post(api + "save", headers=POST, json={**scope, "label": "Acme", "token": TOKEN})
    assert resp.status_code == 200, resp.text
    assert resp.json()["account"] == {"login": "carlo-bot", "type": "User", "orgs": ["acme"]}
    path = env["settings"].credentials_file
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    cred = gitcredentials.load(path)["server:srv"]["github.com"]
    assert (cred.kind, cred.label, cred.account, cred.orgs, cred.username) == ("github", "Acme", "carlo-bot", ("acme",),
                                                                               "carlo-bot")
    # the git store of its server has it: the clone over https reads it there, never on a command line
    store = gitcredentials.store_dir(path) / "server-srv.store"
    assert stat.S_IMODE(store.stat().st_mode) == 0o600 and "@github.com" in store.read_text()
    page = (await client.get("/credentials", headers={"Authorization": AUTH})).text
    assert "GitHub · Acme" in page and TOKEN not in page
    # changed with the field empty: the saved token is kept
    resp = await client.post(api + "save", headers=POST, json={**scope, "label": "Acme bis", "token": ""})
    assert resp.status_code == 200 and gitcredentials.load(path)["server:srv"]["github.com"].token == TOKEN
    # the test: who, how many repositories (both pages), what each permission is for, the missing ones measured
    fake.deny = {"statuses"}
    data = (await client.post(api + "test", headers=POST, json={**scope, "token": ""})).json()
    assert (data["login"], data["repositories"], data["cut"], data["probe"]) == ("carlo-bot", 105, False, "acme/addons")
    assert data["missing"] == ["Commit statuses"]
    assert [p["permission"] for p in data["permissions"]] == ["Metadata", "Contents", "Webhooks", "Commit statuses"]
    assert fake.hooks.get("acme/addons", {}) == {}, "the test made a webhook"
    assert [c for c in fake.calls if c[0] == "POST" and "/statuses/" in c[1]][-1][1].endswith("0" * 40)
    # in Italian, the purpose of each permission
    it = (await client.post(api + "test", headers={**POST, "Cookie": "omniacli_lang=it"},
                            json={**scope, "token": ""})).json()
    assert it["permissions"][1]["for"] == "clone e pull"
    # the rate limit used up: said with the time it comes back
    fake.rate = 0
    resp = await client.post(api + "test", headers=POST, json={**scope, "token": ""})
    assert resp.status_code == 400 and "rate limit" in resp.json()["error"]
    assert all(c[3] for c in fake.calls if c[1] != "/user" or c[3]), "a call without the token"
    no_token_anywhere(client, commands, caplog, page, json.dumps(data))


async def test_from_github_into_a_workspaces_instance(instance, client, fake, commands, caplog):
    settings, calls = instance["settings"], instance["calls"]
    # the instance of a template of Odoo 19: its branch 19.0 is the one chosen
    instance["folder"].mkdir(parents=True, exist_ok=True)
    (instance["folder"] / "instance.json").write_text(json.dumps({"template": "odoo-19"}))
    # no GitHub token yet: «From GitHub» is not offered
    resp = await client.post("/api/workspaces/acme/github/repositories", headers=post_as("mario"),
                             json={"server": "acme-main"})
    assert resp.json()["tokens"] == []
    resp = await client.post("/api/workspaces/acme/credentials/github/save", headers=post_as("mario"),
                             json={"label": "Acme", "token": TOKEN})
    assert resp.status_code == 200, resp.text
    data = (await client.post("/api/workspaces/acme/github/repositories", headers=post_as("mario"),
                              json={"server": "acme-main"})).json()
    # two pages of GitHub's, followed by its Link
    assert len(data["repositories"]) == 105 and not data["cut"] and data["tokens"][0]["account"] == "carlo-bot"
    assert sum(1 for c in fake.calls if c[1] == "/user/repos") >= 2
    assert data["repositories"][0] == {"full_name": "acme/addons", "private": True, "default_branch": "main",
                                       "clone_url": REMOTE, "html_url": "", "archived": False}
    branches = (await client.post("/api/workspaces/acme/github/branches", headers=post_as("mario"),
                                  json={"server": "acme-main", "repository": "acme/addons"})).json()
    assert (branches["chosen"], branches["series"], branches["default_branch"]) == ("19.0", "19.0", "main")
    for bad in ("../x", "acme", "acme/x y"):
        assert (await client.post("/api/workspaces/acme/github/branches", headers=post_as("mario"),
                                  json={"server": "acme-main", "repository": bad})).status_code == 400
    # the existing way of adding it, with the clone address GitHub gave: the workspace's credentials reach it
    resp = await client.post("/api/workspaces/acme/repositories/add", headers=post_as("mario"),
                             json={"server": "acme-main", "url": REMOTE, "branch": "19.0", "name": "addons"})
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success", job["lines"]
    assert ("main", "production", "addons", REMOTE, "19.0", True) in calls["clone"]
    store = gitcredentials.store_dir(settings.credentials_file) / "server-acme-main.store"
    assert stat.S_IMODE(store.stat().st_mode) == 0o600
    # a workspace's user who is not its admin does not list them
    assert (await client.post("/api/workspaces/acme/github/repositories", headers=post_as("luca"),
                              json={"server": "acme-main"})).status_code == 403
    no_token_anywhere(client, commands, caplog)


def _follow(env, url=REMOTE):
    for role in ("production", "test"):
        repo = env["settings"].instance("srv", role).repository("addons")
        sh("git", "remote", "set-url", repo.remote, url, cwd=repo.path)


def _signed(secret: str, body: dict) -> tuple[bytes, dict]:
    raw = json.dumps(body).encode()
    return raw, {"X-GitHub-Event": "push", "X-Hub-Signature-256": "sha256=" + hmac.new(
        secret.encode(), raw, hashlib.sha256).hexdigest()}


async def test_the_webhook_follows_the_ci_and_a_production_never_moves(client, env, fake, commands, caplog,
                                                                     monkeypatch):
    caplog.set_level(logging.DEBUG)
    settings = env["settings"]
    _follow(env)
    pulled = []

    async def pull_restart(job, inst, only_if_changed=False, only=None, backup_folder=None):
        pulled.append(inst.key)
    monkeypatch.setattr(ops, "pull_restart", pull_restart)
    await client.post("/api/credentials/github/save", headers=POST,
                      json={"kind": "server", "name": "srv", "label": "Acme", "token": TOKEN})
    api = "/api/servers/srv/test/deploy-hook"
    # no public address: the webhook is not made, the job says why and what to do
    data = (await client.post(api, headers=POST, json={"enabled": True})).json()
    job = await wait_job(client, data["github_job"])
    assert job["status"] == "success" and any("give the instance a name in Addresses" in line for line in job["lines"])
    assert fake.hooks.get("acme/addons", {}) == {}
    view = (await client.get(api, headers={"Authorization": AUTH})).json()
    assert view["github"][0]["why"] and not view["github"][0]["made"]
    # the portal has a public address: the webhook is made there (JSON, push only, a secret of its own)
    settings.options = config.parse_options({"portal": {"address": "omnia.example.com"}})
    await wait_job(client, (await client.post(api, headers=POST, json={"enabled": False})).json()["github_job"])
    data = (await client.post(api, headers=POST, json={"enabled": True})).json()
    job = await wait_job(client, data["github_job"])
    assert job["status"] == "success", job["lines"]
    (hook,) = fake.hooks["acme/addons"].values()
    url = hook["config"]["url"]
    assert url.startswith("https://omnia.example.com/hooks/github/") and hook["events"] == ["push"]
    assert hook["config"]["content_type"] == "json" and hook["config"]["secret"]
    assert any("webhook of acme/addons made" in line for line in job["lines"]), job["lines"]
    view = (await client.get(api, headers={"Authorization": AUTH})).json()
    assert view["github"][0]["made"] and view["github"][0]["url"] == url
    assert hook["config"]["secret"] not in json.dumps(view)
    # switched on again: the same webhook updated, not a second one
    data = (await client.post(api, headers=POST, json={"enabled": True})).json()
    await wait_job(client, data["github_job"])
    assert len(fake.hooks["acme/addons"]) == 1 and any(c[0] == "PATCH" for c in fake.calls)
    secret, path = hook["config"]["secret"], urlsplit(url).path
    # a wrong signature: refused
    raw, headers = _signed("nope", {"ref": "refs/heads/test"})
    assert (await client.post(path, content=raw, headers=headers)).status_code == 403
    # a push of main: the production follows it, is updated by hand only: listed as available, not deployed
    sha_main = "a" * 40
    raw, headers = _signed(secret, {"ref": "refs/heads/main", "after": sha_main, "repository": {"clone_url": REMOTE},
                                    "head_commit": {"id": sha_main, "message": "On main"}})
    resp = await client.post(path, content=raw, headers=headers)
    assert resp.status_code == 200 and "srv/production is a production" in resp.json()["note"]
    assert pulled == []
    found = (await client.get("/api/servers/srv/production/deploys", headers={"Authorization": AUTH})).json()
    assert found["available"][0]["sha"] == sha_main[:10]
    # the same push told twice (the webhook pasted by hand too): listed once
    await client.post(path, content=raw, headers=headers)
    assert sum(1 for d in deploys.load(deploys.folder_of(settings), "srv/production")["deploys"]
               if d.get("kind") == "available") == 1
    # a push of test: the test (CI on) deploys, its state on the commit: pending, then success on what it checked out
    sha_test = "b" * 40
    raw, headers = _signed(secret, {"ref": "refs/heads/test", "after": sha_test, "repository": {"clone_url": REMOTE},
                                    "head_commit": {"id": sha_test, "message": "On test"}})
    resp = await client.post(path, content=raw, headers=headers)
    assert resp.status_code == 202, resp.text
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success", job["lines"]
    assert pulled == ["srv/test"]
    head = await deploys.head(settings.instance("srv", "test").repository("addons"))
    assert [(s[1], s[2]["state"], s[2]["context"]) for s in fake.statuses] == [
        (sha_test, "pending", "omniacli/srv/test"), (head, "success", "omniacli/srv/test")]
    # told again by the other webhook within minutes: the same job, no second deploy
    again = await client.post(path, content=raw, headers=headers)
    assert again.json()["job"] == resp.json()["job"] and pulled == ["srv/test"]
    # the token may not write statuses: the deploy goes on, a line says it
    fake.deny = {"statuses"}
    raw, headers = _signed(secret, {"ref": "refs/heads/test", "after": "c" * 40, "repository": {"clone_url": REMOTE}})
    job = await wait_job(client, (await client.post(path, content=raw, headers=headers)).json()["job"])
    assert job["status"] == "success" and any("not written on acme/addons" in line for line in job["lines"])
    fake.deny = set()
    # a deploy that fails: failure on the commit pushed
    async def failing(job, inst, **kw):
        raise JobFailed("the update failed")
    monkeypatch.setattr(ops, "pull_restart", failing)
    raw, headers = _signed(secret, {"ref": "refs/heads/test", "after": "d" * 40, "repository": {"clone_url": REMOTE}})
    job = await wait_job(client, (await client.post(path, content=raw, headers=headers)).json()["job"])
    assert job["status"] == "failed" and fake.statuses[-1][1:] == ("d" * 40, fake.statuses[-1][2])
    assert fake.statuses[-1][2]["state"] == "failure"
    # the CI off: the webhook taken away (only OmniaCLI's: another one of the repository stays)
    fake.hooks["acme/addons"][7] = {"id": 7, "config": {"url": "https://ci.example.org/hook"}}
    data = (await client.post(api, headers=POST, json={"enabled": False})).json()
    job = await wait_job(client, data["github_job"])
    assert any("taken away" in line for line in job["lines"]), job["lines"]
    assert list(fake.hooks["acme/addons"]) == [7]
    assert (await client.post(path, content=raw, headers=headers)).status_code == 403
    # a production's CI is never switched on, and nothing makes it deploy
    assert (await client.post("/api/servers/srv/production/deploy-hook", headers=POST,
                              json={"enabled": True})).status_code == 400
    assert "srv/production" not in pulled
    no_token_anywhere(client, commands, caplog, settings.servers_file.parent.joinpath("github-hooks.json").read_text())


@pytest.mark.parametrize("answers", [True, False, "workspace"])
async def test_a_deleted_instance_takes_its_webhook_away(instance, client, env, fake, monkeypatch, tmp_path, answers):
    """23/11/2026: a staging deleted with its CI on left its webhook on GitHub
    and its record in github-hooks.json. Its deletion runs the sync of the CI
    switched off, with the workspace's token its server no longer names; GitHub
    not answering, a warning, the record kept, and taken away the night after."""
    settings = env["settings"]
    settings.options = config.parse_options({"portal": {"address": "omnia.example.com"}})
    resp = await client.post("/api/workspaces/acme/credentials/github/save", headers=post_as("mario"),
                             json={"label": "Acme", "token": TOKEN})
    assert resp.status_code == 200, resp.text
    repo = tmp_path / "storage" / "acme" / "main" / "test" / "addons" / "addons"
    repo.mkdir(parents=True)
    sh("git", "init", "-q", "-b", "main", str(repo))
    sh("git", "remote", "add", "origin", REMOTE, cwd=repo)
    settings.servers["acme-main"].test.repositories.append(config.Repository("addons", repo, "main"))
    data = (await client.post("/api/servers/acme-main/test/deploy-hook", headers=POST, json={"enabled": True})).json()
    job = await wait_job(client, data["github_job"])
    assert job["status"] == "success" and len(fake.hooks["acme/addons"]) == 1, job["lines"]
    # another webhook of the repository, not OmniaCLI's: it stays
    fake.hooks["acme/addons"][7] = {"id": 7, "config": {"url": "https://ci.example.org/hook"}}
    removed = []

    async def remove_instance(job, ws, name, linked=False):
        removed.append(name)

    async def publish(job, ws):
        return {}
    monkeypatch.setattr(workspaceops, "remove_instance", remove_instance)
    monkeypatch.setattr(workspaceops, "publish", publish)
    hooks_file = settings.servers_file.parent / "github-hooks.json"
    fake.deny = set() if answers is True else {"hooks"}
    resp = await client.post("/api/workspaces/acme/instances/delete", headers=POST, json={"server": "acme-main"})
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success" and removed == ["main"], job["lines"]
    assert "acme-main" not in settings.servers
    # its CI forgotten: an instance made again with its name starts with the CI off
    assert not any(k.startswith("acme-main/") for k in json.loads(
        (settings.servers_file.parent / "deploy-hooks.json").read_text() or "{}"))
    if answers is True:
        assert list(fake.hooks["acme/addons"]) == [7]
        assert any("webhook of acme/addons taken away" in line for line in job["lines"]), job["lines"]
        assert "github.com/acme/addons" not in json.loads(hooks_file.read_text())
        assert await client.app.state.github_retry("test") is None
        return
    # GitHub refused: said, the record kept with the workspace's token's scope, the job a success with a warning
    assert any("cannot read the webhooks of acme/addons" in w and "; it is tried again each night" in w
               for w in job["result"]["warnings"]), job
    record = json.loads(hooks_file.read_text())["github.com/acme/addons"]
    assert record["why"] and record["scopes"] == ["server:acme-main", "workspace:acme"]
    assert len(fake.hooks["acme/addons"]) == 2
    fake.deny = set()
    if answers is False:
        # the night after, GitHub answers: taken away
        retried = await client.app.state.github_retry("test")
        job = await wait_job(client, retried.id)
    else:
        # its workspace deleted first: the same sync, with the workspace's token read before it goes
        resp = await client.post("/api/workspaces/delete", headers=POST, json={"name": "acme"})
        assert resp.status_code == 200, resp.text
        (found,) = [j for j in client.app.state.jobs.jobs.values() if j.title == "GitHub webhooks of workspace acme"]
        job = await wait_job(client, found.id)
    assert job["status"] == "success", job["lines"]
    assert list(fake.hooks["acme/addons"]) == [7]
    assert "github.com/acme/addons" not in json.loads(hooks_file.read_text())


async def test_a_servers_line_deleted_takes_its_webhook_away(client, env, fake):
    """Configure › Servers › Delete: the server's CI and its webhook go with
    it, as when it is switched off; a line without either makes no job."""
    settings = env["settings"]
    _follow(env)
    settings.options = config.parse_options({"portal": {"address": "omnia.example.com"}})
    await client.post("/api/credentials/github/save", headers=POST,
                      json={"kind": "server", "name": "srv", "label": "Acme", "token": TOKEN})
    data = (await client.post("/api/servers/srv/test/deploy-hook", headers=POST, json={"enabled": True})).json()
    await wait_job(client, data["github_job"])
    assert len(fake.hooks["acme/addons"]) == 1
    resp = await client.post("/api/servers/delete", headers=POST, json={"name": "srv"})
    assert resp.status_code == 200, resp.text
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success" and fake.hooks["acme/addons"] == {}, job["lines"]
    assert json.loads((settings.servers_file.parent / "github-hooks.json").read_text()) == {}


async def test_a_refusal_of_github_is_said(client, env, fake, monkeypatch):
    settings = env["settings"]
    _follow(env)
    settings.options = config.parse_options({"portal": {"address": "omnia.example.com"}})
    await client.post("/api/credentials/github/save", headers=POST,
                      json={"kind": "server", "name": "srv", "label": "Acme", "token": TOKEN})
    fake.deny = {"hooks"}
    data = (await client.post("/api/servers/srv/test/deploy-hook", headers=POST, json={"enabled": True})).json()
    job = await wait_job(client, data["github_job"])
    assert any("the token cannot read the webhooks of acme/addons (403)" in line for line in job["lines"])
    view = (await client.get("/api/servers/srv/test/deploy-hook",
                             headers={"Authorization": AUTH, "Cookie": "omniacli_lang=it"})).json()
    assert "il token non può leggere i webhook di acme/addons (403)" in view["github"][0]["why"]


def test_a_plain_credential_stays_one(tmp_path):
    path = tmp_path / "credentials.yaml"
    found = {"server:srv": {"github.com": gitcredentials.check("github.com", "bot", "t1", "github", "Acme", "bot",
                                                               ["acme"]),
                            "gitlab.com": gitcredentials.check("gitlab.com", "o", "t2")}}
    gitcredentials.save(path, found)
    again = gitcredentials.load(path)
    assert again == found
    assert again["server:srv"]["gitlab.com"].public() == {"host": "gitlab.com", "username": "o", "token_set": True}
    assert again["server:srv"]["github.com"].public()["label"] == "Acme"
    with pytest.raises(gitcredentials.CredentialError, match="name of a GitHub token"):
        gitcredentials.check("github.com", "bot", "t", "github", "")


async def test_the_clone_reads_the_token_from_the_store(tmp_path, monkeypatch):
    """The command of the clone: the store file's option, never the token."""
    ran = []

    async def run(job, cmd, **kw):
        ran.append(cmd)
        return 0
    monkeypatch.setattr(workspaceops, "run", run)
    store = gitcredentials.write_store(tmp_path / "s", "acme-main", {"github.com": gitcredentials.check(
        "github.com", "carlo-bot", TOKEN, "github", "Acme")})
    options = gitcredentials.git_options(store, ["github.com"])
    ws = config.Workspace("acme", storage=str(tmp_path))
    workspaceops.addons_folder(ws, "main", "production").mkdir(parents=True)
    await workspaceops.clone(Job(id="j", title="t", instances=[], source="s"), ws, "main", "production", "addons",
                             REMOTE, "19.0", options)
    assert all(TOKEN not in arg for arg in ran[0]) and TOKEN in store.read_text()


async def test_a_deleted_instance_leaves_no_folders_nor_caches(instance, client, env, monkeypatch):
    """Step 6 found an instance deleted leaving its empty backups/<server>/ folders and the hash caches of its trees
    (treesync/hashes/<server>.<role>.<tree>.json): they go with it; a backup file kept stays, and another server's
    cache too."""
    settings = env["settings"]

    async def remove_instance(job, ws, name, linked=False):
        return None

    async def publish(job, ws):
        return {}
    monkeypatch.setattr(workspaceops, "remove_instance", remove_instance)
    monkeypatch.setattr(workspaceops, "publish", publish)
    for role in ("production", "test"):
        (settings.backup_dir / "acme-main" / role).mkdir(parents=True, exist_ok=True)
    hashes = settings.jobs_dir.parent / "treesync" / "hashes"
    hashes.mkdir(parents=True)
    for name in ("acme-main.production.backups.json", "acme-main.test.backups.json", "acme-mainx.production.backups.json"):
        (hashes / name).write_text("{}")
    copy = settings.jobs_dir.parent / "treesync" / "copies" / "move-acme-main-filestore"
    copy.mkdir(parents=True)
    (settings.backup_dir / "kept" / "production").mkdir(parents=True)
    (settings.backup_dir / "kept" / "production" / "a.zip").write_bytes(b"zip")
    resp = await client.post("/api/workspaces/acme/instances/delete", headers=POST, json={"server": "acme-main"})
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success", job["lines"]
    assert not (settings.backup_dir / "acme-main").exists() and not copy.exists()
    assert sorted(p.name for p in hashes.iterdir()) == ["acme-mainx.production.backups.json"]
    assert (settings.backup_dir / "kept" / "production" / "a.zip").is_file()
