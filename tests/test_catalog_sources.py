# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The catalogs of modules beyond the OCA (README, Modules from the
catalogs): a GitHub user or organisation added by the machine's admins, its
public repositories listed through GitHub's API (anonymous, or with the
GitHub token of an instance), read as the OCA's; the install plan across the
catalogs, a module that is not free installed only with «I have the
licence». And a module uploaded as a .zip made a repository on GitHub
(README, Modules uploaded as a .zip): the permission measured, the module
pushed as the first commit through the credentials' store file, the
instance following the repository instead of the copy. Against a fake
GitHub (httpx.MockTransport) and a fake git push; no network."""
import json
import subprocess

import httpx
import pytest

from omniacli import github, modulecatalog, moduleupload, ops
from omniacli.jobs import Job

from .test_app import client, wait_job  # noqa: F401 - the client fixture
from .test_github import TOKEN
from .test_module_upload import HELLO, apply, make_zip, send, uploads  # noqa: F401 - the uploads fixture
from .test_modules_catalog import FakeSource, _read_catalog, modules  # noqa: F401 - the modules fixture
from .test_workspace_repositories import instance  # noqa: F401 - the instance fixture
from .test_workspaces import post_as, workspaces  # noqa: F401 - the workspaces fixture


def manifest(name, depends=("base",), license="LGPL-3", **more):
    return repr({"name": name, "summary": f"{name} of Cybrosys", "version": "19.0.1.0.0", "license": license,
                 "depends": list(depends), **more})


CYBRO = {
    "CybroAddons": {
        # it needs date_range, the OCA's (server-ux)
        "pos_extra": manifest("POS Extra Reports", ("point_of_sale", "date_range")),
        "paid_report": manifest("Paid Report", license="OPL-1", price=49.0, currency="EUR"),
        "date_range": manifest("Another date range"),
    },
    "OpenHRMS": {"hrms_dashboard": manifest("HRMS Dashboard", ("hr",))},
}


class OwnerGitHub:
    """GitHub's API for the sources and for a repository made: the public
    repositories of an owner (anonymous or with the token), the token's
    account, the making of a repository; each call recorded with whether it
    carried the token."""

    def __init__(self):
        self.calls, self.made, self.deny = [], [], set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization")
        path, body = request.url.path, json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, auth == f"Bearer {TOKEN}"))
        assert TOKEN not in str(request.url)
        rate = {"X-RateLimit-Remaining": "4990" if auth else "57", "X-RateLimit-Limit": "5000" if auth else "60",
                "X-RateLimit-Reset": "1900000000"}
        if "anonymous-used-up" in self.deny and not auth:
            return httpx.Response(403, json={"message": "API rate limit exceeded"},
                                  headers={**rate, "X-RateLimit-Remaining": "0"})
        if path == "/users/CybroOdoo/repos":
            rows = [{"name": n, "full_name": f"CybroOdoo/{n}", "fork": False, "archived": False, "private": False,
                     "size": size} for n, size in (("CybroAddons", 12_000_000), ("OpenHRMS", 400_000))]
            rows += [{"name": "odoo", "full_name": "CybroOdoo/odoo", "fork": True},
                     {"name": "old-addons", "full_name": "CybroOdoo/old-addons", "archived": True}]
            return httpx.Response(200, json=rows, headers=rate)
        if path.startswith("/users/"):
            return httpx.Response(404, json={"message": "Not Found"}, headers=rate)
        if auth != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"message": "Bad credentials"}, headers=rate)
        if path == "/user":
            return httpx.Response(200, json={"login": "carlo-bot", "type": "User"}, headers=rate)
        if path == "/user/orgs":
            return httpx.Response(200, json=[{"login": "acme"}], headers=rate)
        if request.method == "POST" and path in ("/user/repos", "/orgs/acme/repos"):
            if "admin" in self.deny:
                return httpx.Response(403, json={"message": "Resource not accessible by personal access token"},
                                      headers=rate)
            if not body.get("name"):
                return httpx.Response(422, json={"message": "Validation Failed"}, headers=rate)
            owner = "acme" if path.startswith("/orgs/") else "carlo-bot"
            if any(m[1]["name"] == body["name"] for m in self.made):
                return httpx.Response(422, json={"message": "name already exists on this account"}, headers=rate)
            self.made.append((owner, body))
            full = f"{owner}/{body['name']}"
            return httpx.Response(201, json={"full_name": full, "clone_url": f"https://github.com/{full}.git",
                                             "html_url": f"https://github.com/{full}", "private": body["private"]},
                                  headers=rate)
        return httpx.Response(404, json={"message": "Not Found"}, headers=rate)


class FakeOwnerSource(modulecatalog.GitHubSource):
    """The real listing through the API; the git part (a branch's commit, its
    manifests) from CYBRO."""

    async def head(self, repository, series):
        return "c" * 40 if repository in CYBRO else None

    async def manifests(self, repository, series):
        return dict(CYBRO[repository])


@pytest.fixture
def gh(monkeypatch):
    found = OwnerGitHub()
    monkeypatch.setattr(github, "TRANSPORT", httpx.MockTransport(found))
    monkeypatch.setattr(modulecatalog, "GitHubSource", FakeOwnerSource)
    return found


def _job():
    return Job(id="j", title="t", instances=[], source="s")


def test_a_source_is_named_and_a_module_priced():
    assert modulecatalog.parse_source("CybroOdoo") == ("github.com", "CybroOdoo")
    assert modulecatalog.parse_source(" https://github.com/odoomates/ ") == ("github.com", "odoomates")
    assert modulecatalog.parse_source("https://www.github.com/odoomates") == ("github.com", "odoomates")
    assert modulecatalog.parse_source("git.example.com:8443/Acme") == ("git.example.com:8443", "Acme")
    for bad in ("", "a b", "-x", "github.com/OCA", "OCA", "x/../y", "http://evil/a/b"):
        with pytest.raises(ValueError):
            modulecatalog.parse_source(bad)
    assert modulecatalog.source_id("github.com", "CybroOdoo") == "cybroodoo"
    assert modulecatalog.source_id("git.example.com:8443", "Acme") == "git-example-com-8443-acme"
    assert modulecatalog.not_free({"license": "LGPL-3"}) is None and modulecatalog.not_free({}) is None
    assert modulecatalog.not_free({"license": "OPL-1"}) == "OPL-1"
    assert modulecatalog.not_free({"license": "Other proprietary"}) == "Other proprietary"
    assert modulecatalog.not_free({"license": "LGPL-3", "price": "9.99", "currency": "USD"}) == "LGPL-3, 9.99 USD"
    assert modulecatalog.not_free({"price": "0"}) is None and modulecatalog.not_free({"price": "free"}) is None
    # the price and its currency are read from the manifest
    found = modulecatalog.parse_manifest(CYBRO["CybroAddons"]["paid_report"])
    assert (found["license"], found["price"], found["currency"]) == ("OPL-1", "49.0", "EUR")


async def test_an_owners_public_repositories_read_without_a_token(tmp_path, gh):
    path = modulecatalog.source_path(tmp_path, "cybroodoo", "19.0")
    source = modulecatalog.GitHubSource("github.com", "CybroOdoo")
    job = _job()
    data = await modulecatalog.refresh(job, path, "19.0", source, "cybroodoo")
    # one request, anonymous; forks and archived ones left out
    assert gh.calls == [("GET", "/users/CybroOdoo/repos", False)]
    assert sorted(data["repositories"]) == ["CybroAddons", "OpenHRMS"] and "error" not in data
    assert data["rate"] == {"remaining": 57, "limit": 60, "token": False}
    assert any("read without a token: GitHub allows 60 an hour" in line for line in job.lines)
    catalog = modulecatalog.catalog_of(modulecatalog.load(path), "19.0", "cybroodoo", "CybroOdoo")
    entry = catalog.modules["pos_extra"]
    # its repository's name in an instance carries the owner: never one of the OCA's
    assert entry["repository"] == "CybroOdoo-CybroAddons" and entry["origin"] == "CybroOdoo/CybroAddons"
    assert catalog.urls["CybroOdoo-CybroAddons"] == "https://github.com/CybroOdoo/CybroAddons.git"
    assert catalog.meta()["rate"]["remaining"] == 57 and catalog.count == 4
    # the repositories named: no request to the API
    gh.calls.clear()
    named = modulecatalog.GitHubSource("github.com", "CybroOdoo", ["OpenHRMS"])
    data = await modulecatalog.refresh(_job(), path, "19.0", named, "cybroodoo")
    assert gh.calls == [] and sorted(data["repositories"]) == ["OpenHRMS"]
    # GitHub's limit for who has no token, used up: said, the catalog of before kept
    gh.deny.add("anonymous-used-up")
    data = await modulecatalog.refresh(_job(), path, "19.0", modulecatalog.GitHubSource("github.com", "CybroOdoo"),
                                       "cybroodoo")
    assert "60 requests an hour" in data["error"] and "read it with a GitHub token" in data["error"]
    assert sorted(data["repositories"]) == ["OpenHRMS"]


async def test_the_plan_goes_across_the_catalogs(tmp_path, gh):
    oca_path = modulecatalog.path_of(tmp_path, "19.0")
    await modulecatalog.refresh(_job(), oca_path, "19.0", FakeSource())
    path = modulecatalog.source_path(tmp_path, "cybroodoo", "19.0")
    await modulecatalog.refresh(_job(), path, "19.0", modulecatalog.GitHubSource("github.com", "CybroOdoo"), "cybroodoo")
    oca = modulecatalog.catalog_of(modulecatalog.load(oca_path), "19.0")
    cybro = modulecatalog.catalog_of(modulecatalog.load(path), "19.0", "cybroodoo", "CybroOdoo")
    known = {"test": {"base": "installed", "web": "installed", "point_of_sale": "installed"}}
    # its own source first: date_range is the OCA's as the second (Cybrosys has one of its own)
    merged = modulecatalog.merged([oca, cybro], "19.0")
    plan = modulecatalog.plan(merged, "pos_extra", "test", known, set(), set())
    assert plan["repositories"] == ["CybroOdoo-CybroAddons", "server-ux"] and plan["blocked"] is None
    assert plan["urls"] == {"CybroOdoo-CybroAddons": "https://github.com/CybroOdoo/CybroAddons.git",
                            "server-ux": "https://github.com/OCA/server-ux.git"}
    assert plan["licenses"] == []
    plan = modulecatalog.plan(merged, "paid_report", "test", known, set(), set())
    assert [(row["module"], row["why"], row["source"]) for row in plan["licenses"]] == [
        ("paid_report", "OPL-1, 49.0 EUR", "CybroOdoo")]
    assert "nor in the catalogs" in modulecatalog.plan(merged, "nope", "test", known, set(), set())["blocked"]


# --- the routes


async def _add_cybro(client, **more):
    resp = await client.post("/api/catalog/sources/add", headers=post_as("admin"),
                             json={"source": "CybroOdoo", "series": "19.0", **more})
    assert resp.status_code == 200, resp.text
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success", job["lines"]
    return job


async def test_a_source_added_searched_installed_with_its_licence_and_taken_away(modules, gh, client):
    calls, settings = modules["calls"], modules["settings"]
    modules["known"]["test"]["point_of_sale"] = "installed"
    await _read_catalog(client)
    # a workspace's admin does not add sources; the machine's does
    resp = await client.post("/api/catalog/sources/add", headers=post_as("mario"),
                             json={"source": "CybroOdoo", "series": "19.0"})
    assert resp.status_code == 403
    for bad in ({"source": "OCA", "series": "19.0"}, {"source": "a b", "series": "19.0"},
                {"source": "CybroOdoo", "series": "x"}, {"source": "CybroOdoo", "series": "19.0", "repositories": "../x"},
                {"source": "CybroOdoo", "series": "19.0", "token": True, "server": "acme-main"}):
        assert (await client.post("/api/catalog/sources/add", headers=post_as("admin"), json=bad)).status_code == 400
    job = await _add_cybro(client)
    assert job["title"] == "Read the catalog of CybroOdoo for Odoo 19.0"
    # what the dialog opens with: both catalogs; who reads it said to the machine's admins only
    data = (await client.get("/api/servers/acme-main/modules", headers=post_as("mario"))).json()
    rows = {s["source"]: s for s in data["sources"]}
    assert sorted(rows) == ["cybroodoo", "oca"] and rows["cybroodoo"]["count"] == 4
    assert rows["cybroodoo"]["label"] == "CybroOdoo" and "token_server" not in rows["cybroodoo"]
    assert rows["cybroodoo"]["rate"]["token"] is False and data["count"] == 6
    admin = (await client.get("/api/servers/acme-main/modules", headers=post_as("admin"))).json()
    assert {s["source"]: s for s in admin["sources"]}["cybroodoo"]["token_server"] is None
    # all the sources: a module in two, the OCA's; one source: its own
    url = "/api/servers/acme-main/modules/catalog"
    found = (await client.get(url + "?q=date+range", headers=post_as("eva"))).json()["modules"]
    assert [(m["module"], m["source"]) for m in found] == [("date_range", "oca")]
    found = (await client.get(url + "?q=date+range&source=cybroodoo", headers=post_as("eva"))).json()["modules"]
    assert [(m["module"], m["source"], m["origin"]) for m in found] == [("date_range", "cybroodoo",
                                                                         "CybroOdoo/CybroAddons")]
    found = {m["module"]: m for m in (await client.get(url + "?q=report", headers=post_as("eva"))).json()["modules"]}
    assert found["paid_report"]["not_free"] == "OPL-1, 49.0 EUR" and found["paid_report"]["price"] == "49.0"
    assert found["report_xlsx"]["not_free"] is None
    assert (await client.get(url + "?q=x&source=nobody", headers=post_as("eva"))).status_code == 404
    # not free: refused without the tick, installed with it
    plan = (await client.get("/api/servers/acme-main/modules/plan?module=paid_report&role=test&source=cybroodoo",
                             headers=post_as("eva"))).json()
    assert plan["licenses"][0]["why"] == "OPL-1, 49.0 EUR" and plan["repositories"] == ["CybroOdoo-CybroAddons"]
    assert plan["source"] == "CybroOdoo"
    resp = await client.post("/api/servers/acme-main/test/modules/install", headers=post_as("eva"),
                             json={"module": "paid_report", "source": "cybroodoo"})
    assert resp.status_code == 400 and "Tick that you have the licence" in resp.json()["error"]
    resp = await client.post("/api/servers/acme-main/test/modules/install", headers=post_as("eva"),
                             json={"module": "paid_report", "source": "cybroodoo", "licensed": True})
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success", job["lines"]
    assert any("not free, the licence declared by eva: paid_report (OPL-1, 49.0 EUR)" in line for line in job["lines"])
    assert ("main", "test", "CybroOdoo-CybroAddons", "https://github.com/CybroOdoo/CybroAddons.git", "19.0",
            True) in calls["clone"]
    assert [(r.name, r.branch) for r in settings.servers["acme-main"].test.repositories] == [
        ("CybroOdoo-CybroAddons", "19.0")]
    assert any("modules not free installed in acme-main/test, the licence declared: paid_report" in e.get("detail", "")
               for e in client.app.state.access_log.recent())
    # a module of Cybrosys that needs the OCA's date_range: both repositories
    plan = (await client.get("/api/servers/acme-main/modules/plan?module=pos_extra&role=test&source=cybroodoo",
                             headers=post_as("eva"))).json()
    # its own repository is in the instance now; date_range of Cybrosys too (in the code): nothing of the OCA
    assert plan["repositories"] == [] and plan["modules"] == ["pos_extra", "date_range"]
    # taken away: by the machine's admins; the repositories taken from it stay
    assert (await client.post("/api/catalog/sources/remove", headers=post_as("mario"),
                              json={"id": "cybroodoo"})).status_code == 403
    resp = await client.post("/api/catalog/sources/remove", headers=post_as("admin"), json={"id": "cybroodoo"})
    assert resp.status_code == 200
    folder = settings.jobs_dir.parent / modulecatalog.FOLDER
    assert not list(folder.glob("src-cybroodoo-*")) and modulecatalog.load_sources(settings.jobs_dir.parent) == []
    data = (await client.get("/api/servers/acme-main/modules", headers=post_as("mario"))).json()
    assert [s["source"] for s in data["sources"]] == ["oca"]
    assert (await client.post("/api/catalog/sources/remove", headers=post_as("admin"),
                              json={"id": "cybroodoo"})).status_code == 404
    assert any("source of modules github.com/CybroOdoo taken away" in e.get("detail", "")
               for e in client.app.state.access_log.recent())


async def test_a_source_read_with_the_github_token_of_an_instance(modules, gh, client):
    resp = await client.post("/api/workspaces/acme/credentials/github/save", headers=post_as("mario"),
                             json={"label": "Acme", "token": TOKEN})
    assert resp.status_code == 200, resp.text
    gh.calls.clear()
    job = await _add_cybro(client, token=True, server="acme-main")
    assert ("GET", "/users/CybroOdoo/repos", True) in gh.calls
    assert any("GitHub's requests left this hour: 4990 of 5000" in line for line in job["lines"])
    assert TOKEN not in json.dumps(job)
    data = (await client.get("/api/servers/acme-main/modules", headers=post_as("admin"))).json()
    assert {s["source"]: s for s in data["sources"]}["cybroodoo"]["token_server"] == "acme-main"
    assert data["tokens"] == ["github.com"]


# --- a module uploaded made a repository on GitHub


@pytest.fixture
def pushes(monkeypatch):
    """git push recorded (with what it would send), never run; the other git
    commands run for real in their folder."""
    found = {"pushed": [], "fail": None}
    capture = ops.capture

    async def fake(cmd, *args, **kwargs):
        if "push" in cmd:
            cwd = kwargs.get("cwd")
            files = sorted(str(p.relative_to(cwd)) for p in cwd.rglob("*") if ".git" not in p.parts and p.is_file())
            branch = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True,
                                    text=True).stdout.strip()
            count = subprocess.run(["git", "-C", str(cwd), "rev-list", "--count", "HEAD"], capture_output=True,
                                   text=True).stdout.strip()
            found["pushed"].append({"cmd": list(cmd), "files": files, "branch": branch, "commits": count})
            return (128, "remote: Permission to carlo-bot/x.git denied to carlo-bot.\nfatal: unable to access: "
                         "The requested URL returned error: 403") if found["fail"] else (0, "")
        return await capture(cmd, *args, **kwargs)
    monkeypatch.setattr(ops, "capture", fake)
    return found


async def _uploaded(client):
    data = (await send(client, make_zip({**HELLO, "hello_omnia/__pycache__/x.pyc": "cache"}))).json()
    job = await wait_job(client, (await apply(client, data["id"])).json()["job"])
    assert job["status"] == "success", job["lines"]


async def test_a_module_uploaded_made_a_repository_the_instance_follows(uploads, gh, pushes, client):
    calls, folder, settings = uploads["calls"], uploads["folder"], uploads["settings"]
    await _uploaded(client)
    url = "/api/servers/acme-main/modules/github"
    # no GitHub token reaches the instance: said, nothing offered
    data = (await client.post(url + "/check", headers=post_as("mario"), json={"module": "hello_omnia"})).json()
    assert data["tokens"] == [] and data["may"] is None and data["branch"] == "19.0"
    resp = await client.post(url + "/create", headers=post_as("mario"),
                             json={"module": "hello_omnia", "name": "hello_omnia", "follow": True})
    assert resp.status_code == 400 and "No GitHub token reaches acme-main" in resp.json()["error"]
    await client.post("/api/workspaces/acme/credentials/github/save", headers=post_as("mario"),
                      json={"label": "Acme", "token": TOKEN})
    # a developer, a module not uploaded: refused
    assert (await client.post(url + "/check", headers=post_as("eva"), json={"module": "hello_omnia"})).status_code == 403
    assert (await client.post(url + "/check", headers=post_as("mario"), json={"module": "nope"})).status_code == 404
    # the token's account by default, its organisations offered; the permission measured, nothing made
    data = (await client.post(url + "/check", headers=post_as("mario"), json={"module": "hello_omnia"})).json()
    assert (data["owner"], data["may"], data["tokens"][0]["orgs"]) == ("carlo-bot", True, ["acme"])
    assert gh.made == [] and ("POST", "/user/repos", True) in gh.calls
    gh.deny.add("admin")
    data = (await client.post(url + "/check", headers=post_as("mario"),
                              json={"module": "hello_omnia", "owner": "acme"})).json()
    assert data["may"] is False and "«Administration: Read and write»" in data["missing"]
    resp = await client.post(url + "/check", headers={**post_as("mario"), "Accept-Language": "it"},
                             json={"module": "hello_omnia", "owner": "acme"})
    assert resp.json()["missing"].startswith("Il token non può creare un repository di acme")
    gh.deny.discard("admin")
    for bad, why in (({"owner": "someone"}, "neither the token's account"), ({"name": "a/b"}, "not a name"),
                     ({"name": "x.git"}, "not a name"), ({"name": moduleupload.FOLDER}, "already")):
        resp = await client.post(url + "/create", headers=post_as("mario"),
                                 json={"module": "hello_omnia", "name": "hello-omnia", "follow": True, **bad})
        assert resp.status_code == 400 and why in resp.json()["error"], resp.text
    resp = await client.post(url + "/create", headers=post_as("mario"),
                             json={"module": "hello_omnia", "owner": "acme", "name": "hello-omnia", "follow": True})
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success", job["lines"]
    # made private by default, in the organisation
    assert gh.made == [("acme", {"name": "hello-omnia", "private": True, "auto_init": False, "has_wiki": False,
                                 "description": "Odoo module hello_omnia, made by OmniaCLI from an upload"})]
    # the module's files, its caches left out, one commit on the branch of the series; the token on no command line
    [push] = pushes["pushed"]
    assert push["files"] == ["hello_omnia/__init__.py", "hello_omnia/__manifest__.py", "hello_omnia/views/hello.xml"]
    assert (push["branch"], push["commits"]) == ("19.0", "1")
    assert push["cmd"][-2:] == ["https://github.com/acme/hello-omnia.git", "HEAD:refs/heads/19.0"]
    assert any(arg.startswith("credential.helper=store --file=") for arg in push["cmd"])
    assert all(TOKEN not in arg for arg in push["cmd"]) and TOKEN not in json.dumps(job)
    # the instance follows the repository: cloned into both roles on 19.0, the uploaded copy gone
    for role in ("production", "test"):
        assert ("main", role, "hello-omnia", "https://github.com/acme/hello-omnia.git", "19.0", True) in calls["clone"]
        assert [(r.name, r.branch) for r in getattr(settings.servers["acme-main"], role).repositories] == [
            ("hello-omnia", "19.0")]
        uploaded = folder / role / "addons" / moduleupload.FOLDER
        assert moduleupload.record(uploaded) == {} and not (uploaded / "hello_omnia").exists()
        assert not list((folder / role / "addons").glob(".omniacli-followed-*"))
    assert job["result"]["repository"] == "acme/hello-omnia"
    assert any("acme-main follows acme/hello-omnia (branch 19.0) instead of the uploaded copy of hello_omnia" in line
               for line in job["lines"])
    assert any("made the repository acme/hello-omnia on github.com (private), followed by the instance"
               in e.get("detail", "") for e in client.app.state.access_log.recent())
    # not uploaded any more: nothing to make a repository of
    assert (await client.post(url + "/check", headers=post_as("mario"), json={"module": "hello_omnia"})).status_code == 404


async def test_a_push_refused_or_a_follow_failed_keeps_the_upload(uploads, gh, pushes, client):
    calls, folder = uploads["calls"], uploads["folder"]
    await _uploaded(client)
    await client.post("/api/workspaces/acme/credentials/github/save", headers=post_as("mario"),
                      json={"label": "Acme", "token": TOKEN})
    url = "/api/servers/acme-main/modules/github/create"
    pushes["fail"] = True
    resp = await client.post(url, headers=post_as("mario"), json={"module": "hello_omnia", "name": "one", "follow": True})
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "failed" and "«Contents: Read and write»" in job["error"]
    assert "carlo-bot/one was made" in job["error"] and "Delete the empty repository" in job["error"]
    assert calls["clone"] == [] and "hello_omnia" in moduleupload.record(folder / "test" / "addons" / moduleupload.FOLDER)
    # the push goes, the repository cannot be added: the module put back where it was
    pushes["fail"] = None
    uploads["calls"]["fail_conf"] = True
    resp = await client.post(url, headers=post_as("mario"), json={"module": "hello_omnia", "name": "two", "follow": True})
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "failed"
    assert any("acme-main keeps the uploaded copy of hello_omnia" in line for line in job["lines"])
    for role in ("production", "test"):
        uploaded = folder / role / "addons" / moduleupload.FOLDER
        assert "hello_omnia" in moduleupload.record(uploaded) and (uploaded / "hello_omnia" / "__manifest__.py").is_file()
        assert not list((folder / role / "addons").glob(".omniacli-followed-*"))
    # without follow: the repository made and pushed, the instance keeps its copy
    uploads["calls"]["fail_conf"] = False
    resp = await client.post(url, headers=post_as("mario"),
                             json={"module": "hello_omnia", "name": "three", "follow": False, "private": False})
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success", job["lines"]
    assert gh.made[-1][1]["private"] is False and any("public" in line for line in job["lines"])
    assert "hello_omnia" in moduleupload.record(folder / "production" / "addons" / moduleupload.FOLDER)
    # GitHub refuses a name it has
    resp = await client.post(url, headers=post_as("mario"), json={"module": "hello_omnia", "name": "three"})
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "failed" and "a repository of that name is there already" in job["error"]


def test_take_out_and_put_back(tmp_path):
    folder = tmp_path / "addons" / moduleupload.FOLDER
    (folder / "m").mkdir(parents=True)
    (folder / "m" / "__manifest__.py").write_text("{}")
    (folder / moduleupload.RECORD).write_text(json.dumps({"m": {"name": "M", "by": "mario"}}))
    aside, entry = moduleupload.take_out(folder, "m", "1")
    assert aside == tmp_path / "addons" / ".omniacli-followed-m-1" and entry["by"] == "mario"
    assert moduleupload.record(folder) == {} and not (folder / "m").exists()
    moduleupload.put_back(folder, "m", aside, entry)
    assert moduleupload.record(folder)["m"]["by"] == "mario" and not aside.exists()
    assert moduleupload.take_out(folder, "none", "1") == (None, None)



async def test_a_repository_larger_than_the_room_of_the_workspace_is_refused(modules, gh, client, monkeypatch):
    """CybroAddons is 12 GB on GitHub: cloned into production and test it
    does not fit a workspace of 20 GB, and a disk filled stops its instances
    (lab3, 23/11/2026: the clone failed with No space left on device)."""
    from omniacli import workspaceops

    from .test_workspaces import space_of

    modules["known"]["test"]["point_of_sale"] = "installed"
    await _read_catalog(client)
    await _add_cybro(client)
    ws = modules["settings"].workspaces["acme"]
    monkeypatch.setattr(ws, "limits", {**ws.limits, "disk_gb": 20})

    async def space():
        return space_of(500, {"acme": (20, 1)}, root=ws.storage)
    monkeypatch.setattr(workspaceops, "space", space)
    url = "/api/servers/acme-main/modules/plan?module=pos_extra&role=test&source=cybroodoo"
    plan = (await client.get(url, headers=post_as("eva"))).json()
    assert plan["sizes"] == {"CybroOdoo-CybroAddons": 12_000_000 * 1024}
    assert plan["blocked"].startswith("CybroOdoo-CybroAddons: about 22.9 GB once cloned into production, test")
    assert "has 19.0 GB free" in plan["blocked"]
    resp = await client.post("/api/servers/acme-main/test/modules/install", headers=post_as("eva"),
                             json={"module": "pos_extra", "source": "cybroodoo"})
    assert resp.status_code == 400 and "give it more room" in resp.json()["error"]
    resp = await client.get(url, headers={**post_as("eva"), "Accept-Language": "it"})
    assert "dagli più spazio" in resp.json()["blocked"]
    # the OCA's repositories say no size: nothing refused for them
    plan = (await client.get("/api/servers/acme-main/modules/plan?module=date_range&role=test",
                             headers=post_as("eva"))).json()
    assert plan["blocked"] is None and plan["sizes"] == {}
