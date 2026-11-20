# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deploy on push: an instance's address and secret in a repository's
webhook; a push of a branch it follows pulls that repository, nothing else
does anything."""

import hashlib
import hmac
import json
import stat

from omniacli import deployhooks, ops

from .conftest import sh
from .test_app import AUTH, POST, client, wait_job  # noqa: F401 - the client fixture
from .test_workspaces import post_as


def test_who_pushed_what():
    github = json.dumps({"ref": "refs/heads/test", "repository": {
        "clone_url": "https://github.com/Acme/addons.git", "ssh_url": "git@github.com:Acme/addons.git"}}).encode()
    assert deployhooks.pushed(github) == ("test", {"github.com/acme/addons"})
    gitlab = json.dumps({"ref": "refs/heads/main", "project": {"git_ssh_url": "git@gitlab.com:acme/x.git"}}).encode()
    assert deployhooks.pushed(gitlab) == ("main", {"gitlab.com/acme/x"})
    assert deployhooks.pushed(json.dumps({"ref": "refs/tags/v1"}).encode())[0] is None
    assert deployhooks.host_path("ssh://git@github.com:22/acme/x.git") == "github.com/acme/x"
    assert deployhooks.host_path("https://bot:tok@github.com/acme/x") == "github.com/acme/x"
    body, secret = b'{"a": 1}', "s3cret"
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert deployhooks.verified({"x-hub-signature-256": signature}, body, secret)
    assert not deployhooks.verified({"x-hub-signature-256": signature}, b'{"a": 2}', secret)
    assert deployhooks.verified({"x-gitlab-token": secret}, body, secret)
    assert not deployhooks.verified({"x-gitlab-token": "nope"}, body, secret)
    assert not deployhooks.verified({}, body, "")


async def test_deploy_on_push(client, env, monkeypatch):
    instance = env["settings"].servers["srv"].test
    addons = next(r for r in instance.repositories if r.name == "addons")
    sh("git", "remote", "set-url", addons.remote, "https://github.com/acme/addons.git", cwd=addons.path)
    pulled = []

    async def pull_restart(job, inst, only_if_changed=False, only=None, backup_folder=None):
        pulled.append((inst.key, only, only_if_changed, backup_folder is not None))
    monkeypatch.setattr(ops, "pull_restart", pull_restart)
    api = "/api/servers/srv/test/deploy-hook"
    data = (await client.get(api, headers={"Authorization": AUTH})).json()
    assert not data["enabled"] and data["url"].endswith("/hooks/deploy/srv/test")
    data = (await client.post(api, headers=POST, json={"enabled": True})).json()
    secret = data["secret"]
    assert data["enabled"] and secret
    hooks = env["settings"].servers_file.parent / deployhooks.FILE
    assert stat.S_IMODE(hooks.stat().st_mode) == 0o600
    assert secret not in (await client.get(api, headers={"Authorization": AUTH})).text
    body = json.dumps({"ref": f"refs/heads/{addons.branch}",
                       "repository": {"clone_url": "https://github.com/acme/addons.git"}}).encode()
    sign = lambda b: "sha256=" + hmac.new(secret.encode(), b, hashlib.sha256).hexdigest()  # noqa: E731
    # no secret, a wrong one: refused, nothing done
    for headers in ({}, {"X-Hub-Signature-256": "sha256=00"}, {"X-Gitlab-Token": "nope"}):
        resp = await client.post("/hooks/deploy/srv/test", content=body, headers=headers)
        assert resp.status_code == 403
    assert (await client.post("/hooks/deploy/srv/nope", content=body,
                              headers={"X-Gitlab-Token": secret})).status_code == 403
    # GitHub's ping
    resp = await client.post("/hooks/deploy/srv/test", content=b"{}", headers={
        "X-GitHub-Event": "ping", "X-Hub-Signature-256": sign(b"{}")})
    assert resp.json()["pong"]
    # another branch: nothing to do
    other = body.replace(f"refs/heads/{addons.branch}".encode(), b"refs/heads/elsewhere")
    resp = await client.post("/hooks/deploy/srv/test", content=other, headers={"X-Hub-Signature-256": sign(other)})
    assert resp.status_code == 200 and "nothing to do" in resp.json()["note"] and pulled == []
    # its branch: that repository pulled, the changed modules, a backup when asked, a restart
    resp = await client.post("/hooks/deploy/srv/test", content=body, headers={"X-Hub-Signature-256": sign(body)})
    assert resp.status_code == 202, resp.text
    assert (await wait_job(client, resp.json()["job"]))["status"] == "success"
    assert pulled == [("srv/test", {"addons"}, True, True)]
    assert (await client.get(api, headers={"Authorization": AUTH})).json()["last"]["result"] == "deploy"
    # on the instance's own name too (its nginx passes /.omniacli/hooks/ here)
    resp = await client.post("/.omniacli/hooks/deploy/srv/test", content=body, headers={"X-Gitlab-Token": secret})
    assert resp.status_code == 202
    await wait_job(client, resp.json()["job"])
    # GitLab's token too
    resp = await client.post("/hooks/deploy/srv/test", content=body, headers={"X-Gitlab-Token": secret})
    assert resp.status_code == 202
    await wait_job(client, resp.json()["job"])
    # a developer switches a production's off or on: refused; off: the calls are refused
    assert (await client.post("/api/servers/srv/production/deploy-hook", headers=post_as("dev"),
                              json={"enabled": True})).status_code == 403
    await client.post(api, headers=POST, json={"enabled": False})
    assert (await client.post("/hooks/deploy/srv/test", content=body,
                              headers={"X-Gitlab-Token": secret})).status_code == 403
