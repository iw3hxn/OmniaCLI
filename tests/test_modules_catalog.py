# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Modules tab of an instance (docs/ux/UX-008 §7): the catalog of the
OCA's modules for its version of Odoo, read from a source faked here (no
network in the tests), kept on disk with its date and used from there when
the source does not answer; the search; each module's state in the
instance; what installing one takes; the install as a job, in test for a
developer, in production for an admin with a confirmation and a backup."""
import json
import re

import pytest

from omniacli import modulecatalog, ops, stagings, workspaceops
from omniacli.docker import DockerPlugin
from omniacli.jobs import Job

from .test_app import client, wait_job  # noqa: F401 - the client fixture
from .test_workspace_repositories import instance  # noqa: F401 - the instance fixture
from .test_workspaces import post_as, workspaces  # noqa: F401 - the workspaces fixture


def manifest(name, depends=("base",), summary="", python=(), installable=True, status="Beta"):
    return repr({"name": name, "summary": summary, "version": "19.0.1.0.0", "license": "AGPL-3",
                 "author": "Odoo Community Association (OCA)", "development_status": status,
                 "depends": list(depends), "external_dependencies": {"python": list(python)},
                 "installable": installable})


# the OCA as the fake source gives it: three repositories, one without 19.0
REPOSITORIES = {
    "account-financial-reporting": {
        "account_financial_report": manifest("Account Financial Reports", ("account", "date_range", "report_xlsx"),
                                             "OCA financial reports"),
    },
    "reporting-engine": {
        "report_xlsx": manifest("Base report xlsx", ("base", "web"), "Base module to create xlsx report",
                                python=("xlsxwriter",)),
        "report_py3o": manifest("Py3o Report Engine", ("web",), "Reporting engine based on Libreoffice",
                                status="Alpha"),
        "report_old": manifest("Old", installable=False),
    },
    "server-ux": {
        "date_range": manifest("Date Range", ("web",), "Manage all kind of date range"),
        "base_tier_validation": manifest("Base Tier Validation", ("mail",), "Implement a validation process"),
        "account_enterprise_thing": manifest("Needs enterprise", ("account_accountant",), "An extension"),
    },
    "no-series": {},
}


class FakeSource:
    def __init__(self, repositories=None):
        self.data = repositories if repositories is not None else REPOSITORIES
        self.commits = {name: "a" * 40 for name in self.data if name != "no-series"}
        self.down = False
        self.failing: set[str] = set()
        self.read: list[str] = []

    def url(self, repository):
        return f"https://github.com/OCA/{repository}.git"

    async def repositories(self, series):
        if self.down:
            raise modulecatalog.SourceError("Could not resolve host: github.com")
        return {name: name.replace("-", " ") for name in self.data}

    async def head(self, repository, series):
        if repository in self.failing:
            raise modulecatalog.SourceError("timeout")
        return self.commits.get(repository)

    async def manifests(self, repository, series):
        self.read.append(repository)
        return dict(self.data[repository])


def _job():
    return Job(id="j", title="t", instances=[], source="s")


def test_a_manifest_is_read_never_run():
    found = modulecatalog.parse_manifest(manifest("X", ("web", "Bad Name"), "Sum", ("xlsxwriter",)))
    assert found["name"] == "X" and found["summary"] == "Sum" and found["license"] == "AGPL-3"
    assert found["depends"] == ["web"] and found["python"] == ["xlsxwriter"]
    assert modulecatalog.parse_manifest(manifest("Y", installable=False)) is None
    # code is not a literal: never evaluated
    assert modulecatalog.parse_manifest("__import__('os').system('touch /tmp/x')") is None
    assert modulecatalog.parse_manifest("{'name': open('/etc/passwd').read()}") is None
    assert modulecatalog.parse_manifest("not python at all {") is None
    assert modulecatalog.parse_manifest("['a list']") is None


def test_the_repositories_of_a_series(tmp_path):
    (tmp_path / "reporting.yml").write_text(
        "reporting-engine:\n  branches: ['18.0', '19.0']\n  name: Odoo alternative reporting engine\n"
        "report-print-send:\n  branches: ['18.0']\n  name: printing\n")
    (tmp_path / "web.yml").write_text("web:\n  branches: ['19.0', '20.0']\n  name: Web addons\n'../x':\n  branches: ['19.0']\n")
    (tmp_path / "broken.yml").write_text(": : :\n")
    assert modulecatalog.repositories_in_conf(tmp_path, "19.0") == {
        "reporting-engine": "Odoo alternative reporting engine", "web": "Web addons"}


def test_the_series_of_a_template():
    assert modulecatalog.series_of("odoo-19", "Odoo 19 Community") == "19.0"
    assert modulecatalog.series_of("odooplm-20") == "20.0"
    assert modulecatalog.series_of("custom", "Odoo 18 with OCA") == "18.0"
    assert modulecatalog.series_of("odoo:17.0") == "17.0"
    assert modulecatalog.series_of("mine", "no version") is None
    with pytest.raises(ValueError):
        modulecatalog.path_of("/x", "../19.0")


async def test_the_catalog_is_kept_on_disk_and_read_again_only_where_it_changed(tmp_path):
    path = modulecatalog.path_of(tmp_path, "19.0")
    source = FakeSource()
    data = await modulecatalog.refresh(_job(), path, "19.0", source)
    assert path.is_file() and "error" not in data and data["read"]
    catalog = modulecatalog.catalog_of(modulecatalog.load(path), "19.0")
    # not installable: left out; the repository without the branch: none
    assert catalog.count == 6 and "report_old" not in catalog.modules and "no-series" not in catalog.repositories
    assert catalog.modules["report_xlsx"]["repository"] == "reporting-engine"
    assert sorted(source.read) == ["account-financial-reporting", "reporting-engine", "server-ux"]
    # the same commits: nothing downloaded again
    source.read.clear()
    await modulecatalog.refresh(_job(), path, "19.0", source)
    assert source.read == []
    # one changed: only that one
    source.commits["server-ux"] = "b" * 40
    source.data = {**REPOSITORIES, "server-ux": {"date_range": manifest("Date Range", ("web",))}}
    await modulecatalog.refresh(_job(), path, "19.0", source)
    assert source.read == ["server-ux"]
    catalog = modulecatalog.catalog_of(modulecatalog.load(path), "19.0")
    assert "base_tier_validation" not in catalog.modules and catalog.count == 4


async def test_offline_the_catalog_of_before_stays_with_why(tmp_path):
    path = modulecatalog.path_of(tmp_path, "19.0")
    source = FakeSource()
    first = await modulecatalog.refresh(_job(), path, "19.0", source)
    source.down = True
    data = await modulecatalog.refresh(_job(), path, "19.0", source)
    assert "Could not resolve host" in data["error"] and data["read"] == first["read"]
    catalog = modulecatalog.catalog_of(modulecatalog.load(path), "19.0")
    assert catalog.count == 6 and catalog.read == first["read"] and catalog.error == data["error"]
    # one repository that does not answer keeps what was read of it
    source.down = False
    source.failing = {"server-ux"}
    source.commits["server-ux"] = "c" * 40
    data = await modulecatalog.refresh(_job(), path, "19.0", source)
    assert "1 repositories" in data["error"] and "server-ux" in data["error"]
    assert modulecatalog.catalog_of(modulecatalog.load(path), "19.0").count == 6
    # never read: no catalog, no error
    assert modulecatalog.catalog_of(modulecatalog.load(tmp_path / "none.json"), "19.0").count == 0


async def _catalog(tmp_path):
    path = modulecatalog.path_of(tmp_path, "19.0")
    await modulecatalog.refresh(_job(), path, "19.0", FakeSource())
    return modulecatalog.catalog_of(modulecatalog.load(path), "19.0")


async def test_search(tmp_path):
    catalog = await _catalog(tmp_path)
    assert modulecatalog.search(catalog, "") == ([], 0)
    assert modulecatalog.search(catalog, "  ") == ([], 0)
    # excel: the xlsx ones, by their English words
    rows, total = modulecatalog.search(catalog, "excel")
    assert [r["module"] for r in rows] == ["report_xlsx"] and total == 1
    assert rows[0]["repository"] == "reporting-engine"
    # every word must match; those whose name holds it first
    rows, _ = modulecatalog.search(catalog, "report")
    assert {r["module"] for r in rows} == {"report_xlsx", "report_py3o", "account_financial_report"}
    # engine: in the name of one, in the repository of the other: the name first
    rows, _ = modulecatalog.search(catalog, "engine")
    assert [r["module"] for r in rows] == ["report_py3o", "report_xlsx"]
    rows, _ = modulecatalog.search(catalog, "xlsx")
    # in its name before in a summary
    assert [r["module"] for r in rows][0] == "report_xlsx"
    assert modulecatalog.search(catalog, "report nothing-like-this") == ([], 0)
    # at most the limit
    rows, total = modulecatalog.search(catalog, "a", limit=2)
    assert len(rows) <= 2
    rows, total = modulecatalog.search(catalog, "approvazione")
    assert [r["module"] for r in rows] == ["base_tier_validation"]


async def test_the_state_and_what_an_install_takes(tmp_path):
    catalog = await _catalog(tmp_path)
    core = {"base": "installed", "web": "installed", "mail": "installed", "account": "uninstalled"}
    known = {"production": dict(core), "test": {**core, "report_xlsx": "installed"}}
    assert modulecatalog.state_of("report_xlsx", known, set()) == "installed_test"
    assert modulecatalog.state_of("base", known, set()) == "installed"
    assert modulecatalog.state_of("date_range", known, {"date_range"}) == "in_code"
    assert modulecatalog.state_of("date_range", known, set()) == "to_add"
    plan = modulecatalog.plan(catalog, "account_financial_report", "production", known, set(), set())
    # its repository and those of what it needs, transitively; account from the image
    assert plan["repositories"] == ["account-financial-reporting", "server-ux", "reporting-engine"]
    assert plan["modules"] == ["account_financial_report", "account", "date_range", "report_xlsx"]
    assert plan["python"] == ["xlsxwriter"] and plan["blocked"] is None
    # in test report_xlsx is there: one repository fewer
    plan = modulecatalog.plan(catalog, "account_financial_report", "test", known, set(), {"server-ux"})
    assert plan["repositories"] == ["account-financial-reporting"] and "report_xlsx" not in plan["modules"]
    # in the code already: no repository
    plan = modulecatalog.plan(catalog, "date_range", "production", known, {"date_range"}, set())
    assert plan["repositories"] == [] and plan["modules"] == ["date_range"]
    # a module of Odoo Enterprise: neither the instance nor the catalog has it
    plan = modulecatalog.plan(catalog, "account_enterprise_thing", "production", known, set(), set())
    assert plan["missing"] == ["account_accountant"] and "account_accountant" in plan["blocked"]
    assert "Enterprise" in plan["blocked"]
    # installed already: nothing to do
    assert modulecatalog.plan(catalog, "report_xlsx", "test", known, set(), set())["modules"] == []
    # a database that did not answer: the modules of Odoo are not checked
    plan = modulecatalog.plan(catalog, "account_enterprise_thing", "production", {"production": None}, set(), set())
    assert plan["blocked"] is None and plan["checked"] is False
    assert "not in the catalog" in modulecatalog.plan(catalog, "nope", "test", known, set(), set())["blocked"]


# --- the routes and the page


@pytest.fixture
def modules(instance, env, tmp_path, monkeypatch):
    """acme-main made from odoo-19, with a catalog on disk; its databases
    know the modules of Odoo; eva a developer of acme; the install and the
    backup recorded."""
    settings = env["settings"]
    settings.workspaces["acme"].members["eva"] = "developer"
    folder = instance["folder"]
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "instance.json").write_text(json.dumps({"template": "odoo-19"}))
    template = type("T", (), {"name": "odoo-19", "label": "Odoo 19 Community", "parameters": []})()
    monkeypatch.setattr(workspaceops, "templates", lambda: {"odoo-19": template})
    monkeypatch.setattr(modulecatalog, "GitSource", FakeSource)
    calls = instance["calls"]
    calls.update(install=[], backup=[])
    known = {"production": {"base": "installed", "web": "installed", "mail": "installed", "account": "installed"},
             "test": {"base": "installed", "web": "installed", "mail": "installed", "account": "installed"}}

    async def known_modules(self, inst):
        return dict(known[inst.role])

    async def install_modules(self, job, inst, names):
        calls["install"].append((inst.key, names))
        for name in names:
            known[inst.role][name] = "installed"

    async def backup_before_update(job, inst, folder):
        calls["backup"].append((inst.key, inst.backup_before_update))
    monkeypatch.setattr(DockerPlugin, "known_modules", known_modules)
    monkeypatch.setattr(DockerPlugin, "install_modules", install_modules)
    monkeypatch.setattr(ops, "backup_before_update", backup_before_update)

    # the fake clone of the instance fixture, with the repository's modules in it
    cloned = workspaceops.clone

    async def clone(job, ws, name, role, repo, url, branch, git_options=()):
        path = await cloned(job, ws, name, role, repo, url, branch, git_options)
        for module in REPOSITORIES.get(repo, {}):
            (path / module).mkdir()
            (path / module / "__manifest__.py").write_text(REPOSITORIES[repo][module])
        return path
    monkeypatch.setattr(workspaceops, "clone", clone)
    return {**instance, "known": known}


async def _read_catalog(client):
    resp = await client.post("/api/catalog/refresh", headers=post_as("admin"), json={"series": "19.0"})
    assert resp.status_code == 200, resp.text
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success", job["lines"]


async def test_the_catalog_routes_and_who_reads_it_again(modules, client):
    resp = await client.get("/api/servers/acme-main/modules", headers=post_as("mario"))
    assert resp.status_code == 200 and resp.json()["read"] is None and resp.json()["count"] == 0
    # a workspace's admin does not read the catalog again; the machine's does
    assert (await client.post("/api/catalog/refresh", headers=post_as("mario"), json={"series": "19.0"})).status_code == 403
    assert (await client.post("/api/catalog/refresh", headers=post_as("admin"), json={"series": "x"})).status_code == 400
    await _read_catalog(client)
    data = (await client.get("/api/servers/acme-main/modules", headers=post_as("mario"))).json()
    assert data["series"] == "19.0" and data["count"] == 6 and data["read"] and data["can_refresh"] is False
    assert data["default"] == "test" and [r["role"] for r in data["roles"]] == ["production", "test"]
    assert all(r["refused"] is None for r in data["roles"])
    assert (await client.get("/api/servers/acme-main/modules", headers=post_as("admin"))).json()["can_refresh"]
    # the machine's general search
    found = (await client.get("/api/catalog?series=19.0&q=excel", headers=post_as("admin"))).json()
    assert [m["module"] for m in found["modules"]] == ["report_xlsx"]
    # a user of the workspace: none of it
    assert (await client.get("/api/servers/acme-main/modules", headers=post_as("luca"))).status_code == 403
    assert (await client.get("/api/servers/acme-main/modules/catalog?q=x", headers=post_as("zoe"))).status_code == 403
    # not an instance of a workspace: no catalog
    resp = await client.get("/api/servers/srv/modules", headers=post_as("admin"))
    assert resp.status_code == 404 and "not an instance of a workspace" in resp.json()["error"]


async def test_the_search_says_each_state(modules, client):
    await _read_catalog(client)
    modules["known"]["test"]["report_xlsx"] = "installed"
    url = "/api/servers/acme-main/modules/catalog"
    data = (await client.get(url, headers=post_as("eva"))).json()
    assert data["modules"] == [] and data["total"] == 0
    rows = {m["module"]: m for m in (await client.get(url + "?q=report", headers=post_as("eva"))).json()["modules"]}
    assert rows["report_xlsx"]["state"] == "installed_test" and rows["report_py3o"]["state"] == "to_add"
    assert rows["report_py3o"]["development_status"] == "Alpha"
    rows = {m["module"]: m for m in (await client.get(url + "?q=enterprise", headers=post_as("eva"))).json()["modules"]}
    assert "account_accountant" in rows["account_enterprise_thing"]["blocked"]
    # in Italian, the reason too
    resp = await client.get(url + "?q=enterprise", headers={**post_as("eva"), "Accept-Language": "it"})
    assert resp.json()["modules"][0]["blocked"].startswith("Richiede account_accountant")
    # nothing like it
    assert (await client.get(url + "?q=zzzz", headers=post_as("eva"))).json()["modules"] == []


async def test_install_in_test_as_a_developer(modules, client):
    calls, settings = modules["calls"], modules["settings"]
    await _read_catalog(client)
    plan = (await client.get("/api/servers/acme-main/modules/plan?module=account_financial_report&role=test",
                             headers=post_as("eva"))).json()
    assert plan["repositories"] == ["account-financial-reporting", "server-ux", "reporting-engine"]
    assert plan["restarts"] == ["production", "test"] and plan["python"] == ["xlsxwriter"]
    assert len(plan["modules"]) == 4 - 1  # account is installed already
    # a developer: production refused, test allowed
    resp = await client.post("/api/servers/acme-main/production/modules/install", headers=post_as("eva"),
                             json={"module": "account_financial_report", "confirm": True})
    assert resp.status_code == 403
    data = (await client.get("/api/servers/acme-main/modules", headers=post_as("eva"))).json()
    assert [r["refused"] is not None for r in data["roles"]] == [True, False]
    resp = await client.post("/api/servers/acme-main/test/modules/install", headers=post_as("eva"),
                             json={"module": "account_financial_report"})
    assert resp.status_code == 200, resp.text
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success", job["lines"]
    # the three repositories in production and test on 19.0, both restarted; the module in test only
    server = settings.servers["acme-main"]
    for role in ("production", "test"):
        assert [(r.name, r.branch) for r in getattr(server, role).repositories] == [
            ("account-financial-reporting", "19.0"), ("server-ux", "19.0"), ("reporting-engine", "19.0")]
    assert [c[3] for c in calls["clone"][:1]] == ["https://github.com/OCA/account-financial-reporting.git"]
    assert calls["restart"][:2] == ["acme-main/production", "acme-main/test"]
    assert calls["install"] == [("acme-main/test", ["account_financial_report"])] and calls["backup"] == []
    assert modules["known"]["production"].get("account_financial_report") is None
    # now in the code, installed in test
    rows = {m["module"]: m for m in (await client.get("/api/servers/acme-main/modules/catalog?q=financial",
                                                      headers=post_as("eva"))).json()["modules"]}
    assert rows["account_financial_report"]["state"] == "installed_test"
    rows = {m["module"]: m for m in (await client.get("/api/servers/acme-main/modules/catalog?q=tier",
                                                      headers=post_as("eva"))).json()["modules"]}
    assert rows["base_tier_validation"]["state"] == "in_code"
    # installed: refused again
    resp = await client.post("/api/servers/acme-main/test/modules/install", headers=post_as("eva"),
                             json={"module": "account_financial_report"})
    assert resp.status_code == 400 and "already" in resp.json()["error"]


async def test_install_in_production_asks_a_confirmation_and_backs_up(modules, client):
    calls = modules["calls"]
    await _read_catalog(client)
    url = "/api/servers/acme-main/production/modules/install"
    resp = await client.post(url, headers=post_as("mario"), json={"module": "date_range"})
    assert resp.status_code == 400 and "confirmation" in resp.json()["error"] and calls["install"] == []
    resp = await client.post(url, headers=post_as("mario"), json={"module": "account_enterprise_thing", "confirm": True})
    assert resp.status_code == 400 and "account_accountant" in resp.json()["error"]
    resp = await client.post(url, headers=post_as("mario"), json={"module": "date_range", "confirm": True})
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success", job["lines"]
    assert calls["backup"] == [("acme-main/production", True)]
    assert calls["install"] == [("acme-main/production", ["date_range"])]
    assert calls["restart"][-1] == "acme-main/production"


async def test_a_module_that_odoo_does_not_install_fails_the_job(modules, client, monkeypatch):
    await _read_catalog(client)

    async def install_modules(self, job, inst, names):
        job.log("2026-11-21 10:00:00,000 1 ERROR main odoo.modules.loading: Some modules have inconsistent states")
    monkeypatch.setattr(DockerPlugin, "install_modules", install_modules)
    resp = await client.post("/api/servers/acme-main/test/modules/install", headers=post_as("eva"),
                             json={"module": "date_range"})
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "failed"
    assert job["error"] == ("The module date_range was not installed: Some modules have inconsistent states. "
                            "The full log is in the job.")


async def test_a_staging_installs_in_itself(modules, client, env):
    settings = env["settings"]
    stagings.save((settings.links_file or settings.servers_file).parent / stagings.FILE,
                  {"acme-main": {"parent": "acme-other", "created": "2026-11-21T10:00:00"}})
    await _read_catalog(client)
    data = (await client.get("/api/servers/acme-main/modules", headers=post_as("eva"))).json()
    assert data["staging"] and [r["role"] for r in data["roles"]] == ["production"]
    assert data["roles"][0]["refused"] is None and data["default"] == "production"
    resp = await client.post("/api/servers/acme-main/test/modules/install", headers=post_as("eva"),
                             json={"module": "date_range"})
    assert resp.status_code == 400 and "stopped" in resp.json()["error"]
    # a copy: no confirmation, no backup
    resp = await client.post("/api/servers/acme-main/production/modules/install", headers=post_as("eva"),
                             json={"module": "date_range"})
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success", job["lines"]
    assert modules["calls"]["backup"] == []


async def test_the_modules_tab(modules, client):
    """§7.3: six tabs, Modules holds Code and Updates and its primary + Add
    modules; the dialog's search starts empty; every button has a title."""
    await _read_catalog(client)
    page = (await client.get("/servers/acme-main", headers=post_as("mario"))).text
    tabs = re.findall(r'role="tab" data-tab="([a-z-]+)"', page)
    assert tabs[-2:] == ["modules", "settings"] and len(tabs) <= 6
    assert re.search(r'data-panel="modules" data-section="addons"', page)
    assert re.search(r'data-panel="modules" data-section="updates"', page)
    assert 'data-panel="settings" data-section="technical"' in page
    body = re.sub(r"<script.*?</script>", "", page, flags=re.S)
    outside = re.sub(r"<dialog.*?</dialog>", "", body, flags=re.S)
    panels = re.findall(r'<(?:section|div) class="tab-panel[^"]*" data-panel="modules".*?</(?:section|div)>', outside, flags=re.S)
    primaries = re.findall(r'class="primary"[^>]*id="(\w[\w-]*)"', "".join(panels))
    assert primaries == ["mods-open"]
    dialog = re.search(r'<dialog id="mods-dialog".*?</dialog>', body, flags=re.S).group(0)
    assert 'id="mods-list"' in dialog and re.search(r'<div class="mods-list" id="mods-list" role="list"></div>', dialog)
    assert "report excel" in dialog
    for match in re.finditer(r"<button\b[^>]*>", dialog):
        assert re.search(r'title="[^"]+"', match.group(0)), match.group(0)
    # the plan hidden at first; its Install in production is the one confirmation (21/11/2026)
    assert re.search(r'id="mods-plan-step" hidden', dialog) and 'mods-confirm-step' not in dialog
    # in Italian
    page = (await client.get("/servers/acme-main", headers={**post_as("mario"), "Accept-Language": "it"})).text
    assert ">Moduli<" in page and "+ Aggiungi moduli" in page and "Cerca per nome o per cosa fa" in page
    # a user of the workspace sees no Modules tab
    page = (await client.get("/servers/acme-main", headers=post_as("luca"))).text
    assert 'data-tab="modules"' not in page and 'id="mods-dialog"' not in page
    # an instance outside a workspace: the tab, its repositories, no catalog
    page = (await client.get("/servers/srv", headers=post_as("admin"))).text
    assert 'data-tab="modules"' in page and 'id="mods-open"' not in page
    assert 'data-panel="modules" data-section="updates"' in page


async def test_the_primary_waits_for_a_running_job(modules, client, env):
    """UX-006 §3: with a job running the tab's primary stays, disabled, its reason on the span."""
    import asyncio

    release = asyncio.Event()

    async def wait(job):
        await release.wait()
    job = client.app.state.jobs.submit("hold", ["acme-main/production"], "test", wait)
    await asyncio.sleep(0.05)
    try:
        page = (await client.get("/servers/acme-main", headers=post_as("mario"))).text
        button = re.search(r'<span title="([^"]+)">\s*<button type="button" class="primary" id="mods-open"[^>]*>', page)
        assert button and "A job is working on acme-main" in button.group(1) and "disabled" in button.group(0)
    finally:
        release.set()
        await wait_job(client, job.id)


def test_the_helper_installs_into_the_running_service_and_lists_the_modules(tmp_path, monkeypatch):
    """compose-helper update --install: odoo -i in the running service, the
    module names checked, the crons of a neutralized database kept off;
    compose-helper modules: the fixed query, never as root."""
    from omniacli import composehelper

    monkeypatch.setattr(composehelper, "services", lambda project: ["odoo", "db"])
    monkeypatch.setattr(composehelper, "psql_options", lambda project, name: ("--user", "postgres"))
    [cmd] = composehelper.commands(tmp_path, "update", ["odoo", "DB_test", "--install", "date_range,report_xlsx"])
    assert cmd[:5] == ["exec", "-T", "odoo", "sh", "-c"] and cmd[-2:] == ["DB_test", "date_range,report_xlsx"]
    script = composehelper.SCRIPTS["install-modules"]
    assert ' -i "$2" --stop-after-init' in script and "DB_test" not in cmd[5]
    assert script.index("database.is_neutralized") < script.index("--stop-after-init") and script.endswith("exit $code")
    for bad in ("web;x", "Base", "a,../b"):
        with pytest.raises(composehelper.HelperError, match="not a list of module names"):
            composehelper.commands(tmp_path, "update", ["odoo", "DB", "--install", bad])
    [cmd] = composehelper.commands(tmp_path, "modules", ["db", "DB_test"])
    assert cmd[:4] == ["exec", "-T", "--user", "postgres"] and cmd[-2:] == ["DB_test", composehelper.MODULES_QUERY]
    with pytest.raises(composehelper.HelperError, match="usage"):
        composehelper.commands(tmp_path, "modules", ["db"])


async def test_the_plugin_reads_what_the_database_knows(monkeypatch):
    from omniacli import docker

    inst = type("I", (), {"options": docker.DockerOptions(database="main"), "key": "acme-main/test",
                          "restart": type("R", (), {"command": staticmethod(lambda *a: ["helper", *a])})()})()

    async def capture(cmd, timeout=None):
        assert cmd == ["helper", "modules", "db", "main"]
        return 0, "base|installed\nweb|installed\nsale|uninstalled\nnot a row\nBad|installed\n"
    monkeypatch.setattr(docker, "capture", capture)
    assert await DockerPlugin().known_modules(inst) == {"base": "installed", "web": "installed", "sale": "uninstalled"}

    async def stopped(cmd, timeout=None):
        return 1, "service \"db\" is not running"
    monkeypatch.setattr(docker, "capture", stopped)
    with pytest.raises(Exception, match="did not answer"):
        await DockerPlugin().known_modules(inst)
