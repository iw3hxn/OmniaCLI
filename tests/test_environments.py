# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""«Environments and deploys» (docs/ux/UX-015), the Overview's view of the builds as in Odoo.sh: the data it
needs that the deploys did not keep (the author of each commit, a job of the code that failed before moving a
commit), the rows of the section and the cards of each environment, and who sees what of them."""

import re

import pytest

from omniacli import capabilities, deployhooks, deploys, ops, stagings, workspaceops
from omniacli.config import parse_servers, server_to_dict
from omniacli.jobs import Job

from .conftest import clone, sh
from .test_app import AUTH, POST, client, wait_job  # noqa: F401 - the client fixture
from .test_capabilities import people
from .test_workspaces import basic, post_as


def push(env, tmp_path, name: str, branch: str, message: str, author: str = "") -> str:
    """A commit pushed to branch of the remote name, written by author (none: the tests' identity); its sha."""
    work = tmp_path / f"push-{name}-{branch}"
    if not work.exists():
        sh("git", "clone", "-q", "-b", branch, str(env["remotes"][name]), str(work))
    sh("git", "pull", "-q", "origin", branch, cwd=work)
    (work / f"{message}.txt").write_text(message)
    sh("git", "add", f"{message}.txt", cwd=work)
    sh("git", "commit", "-q", "-m", message, *([f"--author={author} <author@example.com>"] if author else []), cwd=work)
    sh("git", "push", "-q", "origin", f"HEAD:{branch}", cwd=work)
    return sh("git", "rev-parse", "HEAD", cwd=work)


@pytest.fixture
def three(env, tmp_path):
    """srv's production (main) and test (test), and stg, a staging of srv here (main): the rows of srv's page."""
    settings = env["settings"]
    data = server_to_dict(settings.servers["srv"])
    data.pop("test", None)
    data["production"]["repositories"] = [
        {**r, "path": str(clone(env["remotes"][r["name"]], tmp_path / "stg" / r["name"], "main"))}
        for r in data["production"]["repositories"]]
    data["production"]["restart"] = {**data["production"]["restart"], "service": "odoo-stg"}
    settings.servers["stg"] = parse_servers({"stg": data}, settings.logs_dir)["stg"]
    stagings.save(settings.servers_file.parent / stagings.FILE, {"stg": {"parent": "srv", "created": "2026-11-22T09:00:00"}})
    return settings


async def pulled(client, server: str, role: str, who=None) -> dict:
    resp = await client.post(f"/servers/{server}/{role}/pull-restart", headers=who or POST, follow_redirects=False)
    assert resp.status_code == 303, resp.text
    return await wait_job(client, resp.headers["location"].rsplit("/", 1)[-1])


async def strips(client, server: str, role: str, who=None) -> dict:
    resp = await client.get(f"/api/servers/{server}/{role}/environments", headers=who or {"Authorization": AUTH})
    assert resp.status_code == 200, resp.text
    return {e["key"]: e for e in resp.json()["environments"]}


# --- the data -----------------------------------------------------------------------------------------------------

async def test_each_repository_of_a_deploy_keeps_the_author_of_its_commit(env, tmp_path):
    instance = env["settings"].servers["srv"].test
    folder = tmp_path / "deploys"
    recorder = deploys.Recorder(folder, {"srv/test": instance}.get)
    job = Job("j1", "Git pull srv/test", ["srv/test"], "push (test)", started="2026-11-23T10:00:00")
    await recorder.begin(job)
    sha = push(env, tmp_path, "addons", "test", "fix the VAT rounding", author="Mario Rossi")
    addons = next(r for r in instance.repositories if r.name == "addons")
    sh("git", "pull", "-q", "--ff-only", "origin", "test", cwd=addons.path)
    job.status, job.finished = "success", "2026-11-23T10:00:33"
    await recorder.end(job)
    [entry] = deploys.load(folder, "srv/test")["deploys"]
    [row] = entry["repositories"]
    # who wrote the commit, beside who brought it
    assert (row["after"], row["subject"], row["author"], entry["by"]) == (sha, "fix the VAT rounding", "Mario Rossi",
                                                                          "push (test)")
    # what a card says when no deploy recorded is the code of now: each repository's commit, read from its copy
    now = {r["name"]: r for r in await deploys.code_now(instance)}
    assert (now["addons"]["after"], now["addons"]["author"]) == (sha, "Mario Rossi") and now["odoo"]["subject"] == "initial"


async def test_a_job_of_the_code_that_moved_nothing_is_kept_when_it_failed(env, tmp_path):
    """D4: a Git pull, a push, the auto update or a Back to that failed (or was stopped) before moving a commit is
    a deploy of kind failed, without commits; another job that failed, or a pull that brought nothing, is not."""
    instance = env["settings"].servers["srv"].test
    folder = tmp_path / "deploys"
    recorder = deploys.Recorder(folder, {"srv/test": instance}.get)

    async def ended(job_id: str, status: str, code: bool):
        job = Job(job_id, f"Job {job_id}", ["srv/test"], "manual: admin", started="2026-11-23T10:00:00")
        await recorder.begin(job)
        if code:
            ops.mark_code(job, instance)
        job.status, job.error, job.finished = status, "git exited with code 1" if status != "success" else None, \
            "2026-11-23T10:00:05"
        await recorder.end(job)
    await ended("a", "failed", code=False)
    await ended("b", "success", code=True)
    assert deploys.load(folder, "srv/test")["deploys"] == []
    await ended("c", "failed", code=True)
    await ended("d", "stopped", code=True)
    entries = deploys.load(folder, "srv/test")["deploys"]
    assert [(e["id"], e["kind"], e["outcome"], e["repositories"], e["state"]) for e in entries] == [
        ("c", "failed", "failed", [], None), ("d", "failed", "stopped", [], None)]
    assert entries[0]["error"] == "git exited with code 1" and entries[0]["job"] == "c"


async def test_a_failed_pull_is_a_red_card_without_back(client, env, tmp_path):
    """A real Git pull whose remote is gone (ops.pull marks it a job of the code): kept, a card with the error and
    its job; Back to it is refused, as it moved nothing."""
    instance = env["settings"].servers["srv"].test
    addons = next(r for r in instance.repositories if r.name == "addons")
    sh("git", "remote", "set-url", addons.remote, str(tmp_path / "gone.git"), cwd=addons.path)
    job = await pulled(client, "srv", "test")
    assert job["status"] == "failed"
    data = (await client.get("/api/servers/srv/test/deploys", headers={"Authorization": AUTH})).json()
    [row] = data["deploys"]
    assert (row["kind"], row["outcome"], row["has_state"], row["repositories"]) == ("failed", "failed", False, [])
    card = (await strips(client, "srv", "test"))["srv/test"]["cards"][1]  # after the code of now
    assert (card["kind"], card["outcome"], card["back"], card["job"]) == ("failed", "failed", False, f"/jobs/{row['id']}")
    assert card["error"] and card["message"] == "Git pull and restart srv/test"
    resp = await client.post(f"/api/servers/srv/test/deploys/{row['id']}/back", headers=POST, json={})
    assert resp.status_code == 400 and "moved no commit" in resp.json()["error"]


def test_the_pages_of_a_commit_and_of_a_branch():
    web = workspaceops.web_url("git@github.com:acme/web.git")
    assert workspaceops.commit_url(web, "abc1234") == "https://github.com/acme/web/commit/abc1234"
    assert workspaceops.branch_url(web, "18.0") == "https://github.com/acme/web/tree/18.0"
    lab = workspaceops.web_url("https://gitlab.example.com/acme/web.git")
    assert workspaceops.commit_url(lab, "abc") == "https://gitlab.example.com/acme/web/-/commit/abc"
    assert workspaceops.branch_url(lab, "main") == "https://gitlab.example.com/acme/web/-/tree/main"
    assert workspaceops.commit_url("https://git.example.com/acme/web", "abc") is None
    assert workspaceops.commit_url(None, "abc") is None and workspaceops.branch_url(web, "") is None


def test_the_author_a_push_names():
    github = b'{"after": "a1", "head_commit": {"id": "a1", "author": {"name": "Mario Rossi", "email": "m@x"}}}'
    gitlab = b'{"checkout_sha": "b2", "commits": [{"id": "b1", "author": {"name": "Eva"}}, {"id": "b2", "author": {"name": "Luca"}}]}'
    bitbucket = b'{"push": {"changes": [{"new": {"target": {"hash": "c3", "author": {"raw": "Giulia Bianchi <g@x>"}}}}]}}'
    assert [deployhooks.head_author(b) for b in (github, gitlab, bitbucket, b"{}", b"no")] == [
        "Mario Rossi", "Luca", "Giulia Bianchi", "", ""]


# --- the rows and the cards -----------------------------------------------------------------------------------------

async def test_the_rows_and_the_cards_of_a_production(three, client, env, tmp_path):
    page = (await client.get("/servers/srv", headers={"Authorization": AUTH})).text
    section = page[page.index('id="envs"'):page.index("<style>\n/* «Environments and deploys»")]
    assert re.findall(r'data-env="([^"]+)" data-env-kind="([^"]+)"', section) == [
        ("srv/production", "production"), ("srv/test", "test"), ("stg/production", "staging")]
    assert re.findall(r'data-ci="(\w+)"', section) == ["manual", "off", "off"]
    assert 'data-api="/api/servers/srv/production/environments"' in section and section.count('class="env-strip"') == 3
    # no note of a commit available above the tabs any more (UX-015 §9)
    assert 'class="note-box" data-available' not in page
    # nothing deployed yet: each environment's card of now is its code, read from its copies
    found = await strips(client, "srv", "production")
    assert list(found) == ["srv/production", "srv/test", "stg/production"]
    assert [(e["ci"], e["total"], [c["kind"] for c in e["cards"]]) for e in found.values()] == [
        ("manual", 0, ["now"]), ("off", 0, ["now"]), ("off", 0, ["now"])]
    assert found["srv/production"]["confirm"] == "srv" and found["srv/test"]["confirm"] == ""
    assert found["srv/test"]["more"] == "/servers/srv?view=test#deploys"
    # five deploys of the test: the three newest, the one of now first, each with its author and its time
    for i in range(5):
        push(env, tmp_path, "addons", "test", f"change {i}", author=f"Dev {i}")
        assert (await pulled(client, "srv", "test"))["status"] == "success"
    test = (await strips(client, "srv", "test"))["srv/test"]
    assert test["total"] == 5 and [c["message"] for c in test["cards"]] == ["change 4", "change 3", "change 2"]
    assert [c["current"] for c in test["cards"]] == [True, False, False]
    assert [c["back"] for c in test["cards"]] == [False, True, True]
    assert test["cards"][0]["author"] == "Dev 4" and test["cards"][0]["by"] == "manual: admin"
    assert test["cards"][0]["seconds"] is not None and test["cards"][0]["finished"]
    # gone back to the oldest: it is the card of now, saying that deploy's commit, and still listed
    oldest = (await client.get("/api/servers/srv/test/deploys", headers={"Authorization": AUTH})).json()["deploys"][-1]
    resp = await client.post(f"/api/servers/srv/test/deploys/{oldest['id']}/back", headers=POST, json={})
    assert resp.status_code == 200, resp.text
    await wait_job(client, resp.json()["job"])
    test = (await strips(client, "srv", "test"))["srv/test"]
    now = [c for c in test["cards"] if c["current"]]
    assert len(now) == 1 and now[0]["message"] == "change 0" and test["cards"][0] is now[0]
    # a test's page: its own row only
    assert list(await strips(client, "srv", "test")) == ["srv/test"]
    page = (await client.get("/servers/srv?view=test", headers={"Authorization": AUTH})).text
    assert re.findall(r'data-env="([^"]+)" data-env-kind', page) == ["srv/test"]


async def test_going_back_on_a_production_asks_its_name(three, client, env, tmp_path):
    """D2: on a production the page's route wants the server's name written, as the REST API."""
    push(env, tmp_path, "addons", "main", "first")
    await pulled(client, "srv", "production")
    push(env, tmp_path, "addons", "main", "second")
    await pulled(client, "srv", "production")
    first = (await client.get("/api/servers/srv/production/deploys", headers={"Authorization": AUTH})).json()["deploys"][-1]
    url = f"/api/servers/srv/production/deploys/{first['id']}/back"
    for body in ({}, {"confirm": "sr"}, {"confirm": "SRV"}):
        resp = await client.post(url, headers=POST, json=body)
        assert resp.status_code == 400 and "Write the name of the instance, srv" in resp.json()["error"], body
    resp = await client.post(url, headers=POST, json={"confirm": "srv"})
    assert resp.status_code == 200, resp.text
    assert (await wait_job(client, resp.json()["job"]))["status"] == "success"


# --- who sees what (UX-015 §12) --------------------------------------------------------------------------------------

async def test_who_sees_what(three, client, env, tmp_path):
    settings = env["settings"]
    caps = sorted(capabilities.PRESETS["developer"] - {"login_as", "backup"})
    people(client, settings, {"eva": "developer", "luca": "user", "pia": "No access"}, roles={"No access": caps})
    push(env, tmp_path, "addons", "test", "a change of the test")
    await pulled(client, "srv", "test")

    async def page_of(who) -> str:
        page = (await client.get("/servers/srv", headers=who)).text
        return page[page.index('id="envs"'):page.index("<style>\n/* «Environments and deploys»")]

    def menu(section: str, key: str) -> list[str]:
        row = section[section.index(f'data-env="{key}"'):]
        row = row[:row.index('<div class="env-row"')] if '<div class="env-row"' in row else row
        return re.findall(r'data-menu="(\w+)"', row)

    # the machine's admin: every row, the menu with the shell (a development machine), Rebuild of the staging
    admin = await page_of({"Authorization": AUTH})
    # (Log in as… and Addresses… off with why: srv has no name over https, its site is not OmniaCLI's)
    assert menu(admin, "srv/test") == ["login_as", "logs", "backup", "shell", "addresses"]
    assert 'data-rebuild="stg"' in admin
    # the workspace's admin: no shell; the person who started a deploy in the customer's words
    mario = await page_of(basic("mario"))
    assert menu(mario, "srv/test") == ["login_as", "logs", "backup", "addresses"] and 'data-rebuild="stg"' in mario
    found = await strips(client, "srv", "production", basic("mario"))
    assert found["srv/test"]["cards"][0]["by"] != "manual: admin" and "admin" not in str(found)
    # a developer of the workspace: the cards, no Rebuild, no shell
    eva = await page_of(basic("eva"))
    assert "data-api=" in eva and "data-rebuild" not in eva and "env-rebuild" not in eva
    assert menu(eva, "srv/test") == ["login_as", "logs", "backup"]
    assert (await client.get("/api/servers/srv/test/environments", headers=basic("eva"))).status_code == 200
    # a custom role without Log in as and backups: there, off, each saying why
    pia = await page_of(basic("pia"))
    for what in ("login_as", "backup"):
        off = re.search(r'<span title="([^"]+)"><button type="button" disabled (?:data-login-as="" )?data-menu="%s"' % what, pia)
        assert off and "No access" in off.group(1), what
    # a user of the workspace: the rows and Open, no cards, no menu; the route refuses
    luca = await page_of(basic("luca"))
    assert re.findall(r'data-env="([^"]+)" data-env-kind', luca) == ["srv/production", "srv/test", "stg/production"]
    assert "data-api=" not in luca and "env-strip" not in luca and "data-menu" not in luca and "data-ci" not in luca
    assert (await client.get("/api/servers/srv/production/environments", headers=basic("luca"))).status_code == 403
    # a person limited to the staging: its row only, on its own page
    resp = await client.post("/api/workspaces/acme/members/save", headers=post_as("mario"),
                             json={"name": "eva", "role": "developer", "servers": ["stg"]})
    assert resp.status_code == 200, resp.text
    assert (await client.get("/servers/srv", headers=basic("eva"))).status_code == 403
    assert list(await strips(client, "stg", "production", basic("eva"))) == ["stg/production"]
