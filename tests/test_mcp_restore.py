# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A database from outside into a new instance, by MCP only: upload_backup
gives a one-time URL to PUT it to (here, or sent on to a linked machine),
restore_new_instance tells first what its modules lack (dry run), then
makes the instance from the template of its version, adds the OCA
repositories asked, restores it neutralized; refused while modules are
missing unless allowed."""

import dataclasses
import hashlib
import json
import zipfile
from pathlib import Path

import httpx
import pytest

pytest.importorskip("mcp")  # the SDK needs Python 3.10

from omniacli import dumpinfo, linkcopy, modulecatalog, ops, stacktemplates, workspaceops  # noqa: E402
from omniacli.app import create_app  # noqa: E402
from omniacli.config import Workspace, parse_settings  # noqa: E402
from omniacli.docker import DockerPlugin  # noqa: E402
from omniacli.workspacehelper import INSTANCE_FILE  # noqa: E402

from .conftest import LINK_TOKEN, USER_HASH, BasicAsSession  # noqa: E402
from .test_app import POST, client, wait_job  # noqa: E402,F401
from .test_from_link import running  # noqa: E402
from .test_mcp import HEADERS, call, rpc, token_headers  # noqa: E402

TEMPLATE = Path(__file__).parent.parent / "templates" / "odooplm-20"
COLUMNS = "id, name, category_id, state, latest_version, demo"


def dump_sql(modules: dict[str, str], version: str = "20.0.1.3") -> str:
    """A plain dump of pg_dump with its table of modules."""
    rows = "".join(f"{i}\t{name}\t\\N\t{state}\t{version if state == 'installed' else chr(92) + 'N'}\tf\n"
                   for i, (name, state) in enumerate(modules.items(), 1))
    return ("--\n-- PostgreSQL database dump\n--\n\nSET statement_timeout = 0;\n"
            "CREATE TABLE public.res_users (id integer);\n"
            f"COPY public.ir_module_module ({COLUMNS}) FROM stdin;\n{rows}\\.\n\n"
            "COPY public.res_users (id) FROM stdin;\n1\n\\.\n")


MODULES = {"base": "installed", "web": "installed", "web_responsive": "installed", "acme_private": "installed",
           "sale": "uninstalled"}


def backup_zip(path: Path, modules: dict[str, str] = MODULES) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"db_name": "Acme_Prod", "version": "20.0", "major_version": "20.0"}))
        zf.writestr("dump.sql", dump_sql(modules))
        zf.writestr("filestore/ab/abcdef", b"attachment")
    return path


def test_the_dump_says_its_version_and_modules(tmp_path):
    info = dumpinfo.read(backup_zip(tmp_path / "a.zip"))
    assert info["kind"] == "zip" and info["major"] == 20 and info["database"] == "Acme_Prod" and info["filestore"]
    assert info["modules"] == ["acme_private", "base", "web", "web_responsive"]
    sql = tmp_path / "b.sql"
    sql.write_text(dump_sql({"base": "installed", "mail": "to upgrade"}, "19.0.1.0"))
    info = dumpinfo.read(sql)
    assert (info["kind"], info["major"], info["modules"]) == ("sql", 19, ["base", "mail"])
    wrapped = dumpinfo.wrap_sql(sql, tmp_path / "b.zip", 19, "b")
    with zipfile.ZipFile(wrapped) as zf:
        assert json.loads(zf.read("manifest.json"))["major_version"] == "19.0"
        assert zf.read("dump.sql") == sql.read_bytes()
    for bad, said in ((b"PGDMP custom", "custom format"), (b"hello", "not a zip nor a plain SQL dump")):
        (tmp_path / "c.sql").write_bytes(bad)
        with pytest.raises(dumpinfo.DumpError, match=said):
            dumpinfo.read(tmp_path / "c.sql")


@pytest.fixture
def restoring(env, monkeypatch, tmp_path):
    """Workspace acme with storage, the template odooplm-20 (Odoo 20), the
    OCA catalog of 20.0 with web_responsive (repository web), and the
    machine's work faked: what each step did is in calls."""
    settings = env["settings"]
    template = stacktemplates.load_template(TEMPLATE)
    monkeypatch.setattr(workspaceops, "templates", lambda: {template.name: template})
    storage = tmp_path / "storage"
    settings.storage_roots = {"main": str(storage)}
    settings.workspaces = {"acme": Workspace("acme", "ACME", servers=[], storage="main",
                                             limits={"disk_gb": 10, "instances": 5})}
    modulecatalog.save(modulecatalog.path_of(settings.jobs_dir.parent, "20.0"), {
        "series": "20.0", "read": "2026-11-21T03:00:00", "repositories": {
            "web": {"modules": {"web_responsive": {"name": "Web Responsive", "depends": ["web"]}}}}})
    calls = {"made": [], "restore": [], "cloned": [], "known": {}}

    async def create_instance(job, ws, instance, tpl, values, setup=None):
        folder = storage / ws.name / instance
        (folder / "production" / "addons").mkdir(parents=True)
        (folder / INSTANCE_FILE).write_text(json.dumps({"template": tpl.name}))
        calls["made"].append(instance)
        return {"type": "docker", "production": {
            "database": instance, "http_port": 20000 + len(calls["made"]), "repositories": [],
            "restart": {"type": "compose", "service": str(folder / "production"), "sudo": True}}}

    async def nothing(*args, **kwargs):
        return {}

    async def clone(job, ws, instance, role, name, url, branch, git_options=()):
        path = storage / ws.name / instance / role / "addons" / name
        (path / "web_responsive").mkdir(parents=True)
        (path / "web_responsive" / "__manifest__.py").write_text("{'name': 'x'}")
        calls["cloned"].append((instance, name, url, branch))
        return path

    async def known_modules(self, inst):
        # a fresh database knows Odoo's modules; not yet those of a repository
        # just added (Odoo reads its addons again at the restore)
        found = {"base": "installed", "web": "uninstalled", "sale": "uninstalled"}
        return {**found, **calls["known"].get(inst.key, {})}

    async def restore(self, job, inst, backup, options):
        with zipfile.ZipFile(backup) as zf:
            calls["restore"].append((inst.key, sorted(zf.namelist()), options))

    async def no_space():
        raise workspaceops.SpaceError("not in the tests")
    monkeypatch.setattr(workspaceops, "create_instance", create_instance)
    monkeypatch.setattr(workspaceops, "publish", nothing)
    monkeypatch.setattr(workspaceops, "clone", clone)
    monkeypatch.setattr(workspaceops, "write_conf", nothing)
    monkeypatch.setattr(workspaceops, "space", no_space)
    monkeypatch.setattr(workspaceops, "docker_missing", lambda: None)
    monkeypatch.setattr(workspaceops, "storage_path", lambda ws: storage)
    monkeypatch.setattr(DockerPlugin, "known_modules", known_modules)
    monkeypatch.setattr(DockerPlugin, "restore", restore)
    monkeypatch.setattr(DockerPlugin, "pip_requirements", nothing)
    monkeypatch.setattr(ops, "restart", nothing)
    return calls


async def put(client, answer, body: bytes, headers=None, length=None):
    headers = headers or client.admin
    return await client.put(answer["path"], content=body, headers={
        "Authorization": headers["Authorization"], "Content-Length": str(len(body) if length is None else length)})


async def test_upload_one_time_its_size_and_its_token(restoring, client, env, tmp_path):
    admin = client.admin = await token_headers(client, "boss", "admin")
    body = backup_zip(tmp_path / "acme.zip").read_bytes()
    # an admin's tool: a developer's token does not see it
    assert "role admin" in (await call(client, "upload_backup", name="acme.zip", size=len(body),
                                       workspace="acme"))["error"]
    answer = await call(client, "upload_backup", headers=admin, name="acme.zip", size=len(body), workspace="acme",
                        sha256=hashlib.sha256(body).hexdigest())
    assert answer["method"] == "PUT" and answer["url"].endswith(answer["path"]) and answer["one_time"]
    # another token, a wrong size: refused, and the URL is still unused for the first
    other = await token_headers(client, "other", "admin")
    assert (await put(client, answer, body, headers=other)).status_code == 404
    assert (await client.put(answer["path"], content=body)).status_code == 401
    resp = await put(client, answer, body)
    assert resp.status_code == 200, resp.text
    kept = env["settings"].backup_dir / ".imports" / "acme" / resp.json()["file"]
    assert kept.read_bytes() == body and oct(kept.stat().st_mode & 0o777) == "0o600"
    assert resp.json()["sha256"] == hashlib.sha256(body).hexdigest()
    # one time only
    assert "used already" in (await put(client, answer, body)).json()["error"]
    # the size told, the SHA-256 told, a database: or nothing kept
    answer = await call(client, "upload_backup", headers=admin, name="acme.zip", size=len(body) + 1, workspace="acme")
    assert "the request says" in (await put(client, answer, body)).json()["error"]
    answer = await call(client, "upload_backup", headers=admin, name="acme.zip", size=len(body), workspace="acme",
                        sha256="0" * 64)
    assert "its SHA-256 is" in (await put(client, answer, body)).json()["error"]
    answer = await call(client, "upload_backup", headers=admin, name="x.zip", size=5, workspace="acme")
    assert "not a zip" in (await put(client, answer, b"hello")).json()["error"]
    assert sorted(p.name for p in kept.parent.iterdir()) == ["acme.zip"]
    for args, said in (({"name": "x.dump", "size": 5}, "ending in .zip"), ({"name": "x.zip", "size": 0}, "size"),
                       ({"name": "x.zip", "size": 5, "workspace": "nope"}, "No workspace nope"),
                       ({"name": "x.zip", "size": 5, "link": "nope"}, "No link nope")):
        assert said in (await call(client, "upload_backup", headers=admin, **{"workspace": "acme", **args}))["error"]
    # every step in the access log: the tool's call, the PUT and its outcome
    entries = client.app.state.access_log.recent()
    assert any(e["detail"].startswith("PUT /api/uploads/") and e.get("status") == 200 for e in entries)
    assert any(e["detail"] == "tools/call upload_backup" for e in entries)


async def test_a_token_of_a_workspace_uploads_there_only(restoring, client, env):
    env["settings"].workspaces["beta"] = Workspace("beta", servers=[])
    resp = await client.post("/api/workspaces/acme/tokens/save", headers=POST,
                             json={"name": "acme-bot", "role": "admin", "new": True})
    scoped = {**HEADERS, "Authorization": f"Bearer {resp.json()['token']}"}
    answer = await call(client, "upload_backup", headers=scoped, name="a.zip", size=10)
    assert answer["path"].startswith("/api/uploads/")
    assert "workspace acme" in (await call(client, "upload_backup", headers=scoped, name="a.zip", size=10,
                                           workspace="beta"))["error"]
    names = {t["name"] for t in (await rpc(client, "tools/list", headers=scoped)).json()["result"]["tools"]}
    assert {"upload_backup", "restore_new_instance"} <= names
    assert "restores there only" in (await call(client, "restore_new_instance", headers=scoped, file="a.zip",
                                                workspace="beta"))["error"]


async def test_dry_run_then_restore(restoring, client, env, tmp_path):
    settings = env["settings"]
    admin = client.admin = await token_headers(client, "boss", "admin")
    folder = settings.backup_dir / ".imports" / "acme"
    folder.mkdir(parents=True)
    backup_zip(folder / "acme.zip")
    # no instance of the template yet: Odoo's own modules are not known
    plan = await call(client, "restore_new_instance", headers=admin, file="acme.zip", workspace="acme")
    assert plan["dry_run"] and plan["odoo"] == "20.0" and plan["installed_modules"] == 4
    assert plan["new_instance"] == {"workspace": "acme", "server": "acme-acme-prod", "template": "odooplm-20",
                                    "neutralize": True, "staging_of": None, "mail_caught": False}
    missing = plan["missing_modules"]
    assert missing["in_oca"] == [{"repository": "web", "url": "https://github.com/OCA/web.git", "branch": "20.0",
                                  "modules": ["web_responsive"]}]
    assert set(missing["not_checked"]) == {"acme_private", "base", "web"} and "not known" in missing["note"]
    assert "Nothing was started" in plan["next"] and restoring["made"] == []
    # missing modules: refused, with the consequence, unless allowed
    refused = await call(client, "restore_new_instance", headers=admin, file="acme.zip", workspace="acme",
                         dry_run=False)
    assert "nothing started" in refused["error"] and "data of those modules stays unreachable" in refused["error"]
    assert "add_repositories=true" in refused["error"] and "acme-acme-prod" not in settings.servers
    done = await call(client, "restore_new_instance", headers=admin, file="acme.zip", workspace="acme",
                      dry_run=False, add_repositories=True, allow_missing=True, wait_seconds=30)
    job = done["job"]
    assert job["status"] == "success", job
    assert job["source"] == "mcp: boss" and job["result"]["server"] == "acme-acme-prod"
    assert job["result"]["missing_modules"] == ["acme_private"] and job["result"]["modules_added_from"] == ["web"]
    assert restoring["cloned"] == [("acme-prod", "web", "https://github.com/OCA/web.git", "20.0")]
    assert restoring["restore"] == [("acme-acme-prod/production", ["dump.sql", "filestore/ab/abcdef", "manifest.json"],
                                     {"neutralize": True})]
    assert [r.name for r in settings.servers["acme-acme-prod"].production.repositories] == ["web"]
    # now Odoo's own modules are read from that instance: the private one is not found
    plan = await call(client, "restore_new_instance", headers=admin, file="acme.zip", workspace="acme")
    assert plan["new_instance"]["server"] == "acme-acme-prod-2"
    assert plan["missing_modules"]["not_found"] == ["acme_private"] and plan["missing_modules"]["not_checked"] == []
    assert plan["missing_modules"]["odoo_core_read_from"] == ["acme-acme-prod"]
    assert "must be provided" in plan["message"]
    # the job checks again on the new instance's database: still missing, and not allowed
    restoring["known"]["acme-other/production"] = {"acme_private": "uninstallable"}
    answer = await call(client, "restore_new_instance", headers=admin, file="acme.zip", workspace="acme",
                        name="other", dry_run=False, add_repositories=True, allow_missing=True, wait_seconds=30)
    assert answer["job"]["status"] == "success" and answer["job"]["result"]["missing_modules"] == ["acme_private"]


async def test_a_sql_dump_a_name_and_refusals(restoring, client, env):
    settings = env["settings"]
    admin = client.admin = await token_headers(client, "boss", "admin")
    folder = settings.backup_dir / ".imports" / "acme"
    folder.mkdir(parents=True)
    (folder / "old.sql").write_text(dump_sql({"base": "installed"}))
    answer = await call(client, "restore_new_instance", headers=admin, file="old.sql", workspace="acme", name="old",
                        neutralize=False, dry_run=False, allow_missing=True, wait_seconds=30)
    assert answer["kind"] == "sql" and answer["job"]["status"] == "success", answer
    # a SQL dump goes in a zip of its own, gone after
    assert restoring["restore"] == [("acme-old/production", ["dump.sql", "manifest.json"], {"neutralize": False})]
    assert sorted(p.name for p in folder.iterdir()) == ["old.sql"]
    for args, said in (({"file": "nope.zip"}, "No file nope.zip"), ({"file": "../x.zip"}, "file:"),
                       ({"file": "old.sql", "workspace": "nope"}, "No workspace"),
                       ({"file": "old.sql", "name": "old"}, "old is taken"),
                       ({"file": "old.sql", "name": "Bad Name"}, "lower case"),
                       ({"file": "old.sql", "staging_of": "srv"}, "a production of workspace acme"),
                       ({"file": "x.zip", "backup_of": "srv"}, "a server of workspace acme")):
        result = await call(client, "restore_new_instance", headers=admin, **{"workspace": "acme", **args})
        assert said in result["error"], (args, result)
    (folder / "v19.sql").write_text(dump_sql({"base": "installed"}, "19.0.1.0"))
    assert "no template of Odoo 19" in (await call(client, "restore_new_instance", headers=admin, file="v19.sql",
                                                   workspace="acme"))["error"]


async def test_a_staging_of_a_production(restoring, client, env):
    """staging_of: a staging beside that production, dated, its mail caught,
    neutralized whatever is asked."""
    settings = env["settings"]
    admin = client.admin = await token_headers(client, "boss", "admin")
    folder = settings.backup_dir / ".imports" / "acme"
    folder.mkdir(parents=True)
    backup_zip(folder / "acme.zip", {"base": "installed"})
    made = await call(client, "restore_new_instance", headers=admin, file="acme.zip", workspace="acme", name="main",
                      dry_run=False, allow_missing=True, wait_seconds=30)
    assert made["job"]["status"] == "success", made
    answer = await call(client, "restore_new_instance", headers=admin, file="acme.zip", workspace="acme",
                        staging_of="acme-main", neutralize=False, dry_run=False, wait_seconds=30)
    assert answer["new_instance"]["mail_caught"] and answer["new_instance"]["neutralize"]
    assert answer["job"]["status"] == "success", answer
    assert answer["server"].startswith("acme-main-stg-") and restoring["restore"][-1][2] == {"neutralize": True}


@pytest.fixture
async def here(env, tmp_path, monkeypatch):
    """This OmniaCLI, linked to cliente (the app of env, where this one is
    the link omnia, made an admin there): an admin token boss here."""
    monkeypatch.setattr(linkcopy, "POLL_EVERY", 0.02)
    env["settings"].links["omnia"] = dataclasses.replace(env["settings"].links["omnia"], role="admin")
    conf = tmp_path / "here"
    conf.mkdir()
    (conf / "servers.yaml").write_text("servers: {}\n")
    (conf / "links.yaml").write_text(json.dumps({"links": {"cliente": {"url": "http://customer.test",
                                                                       "token": LINK_TOKEN}}}))
    settings = parse_settings({"auth": {"users": {"admin": USER_HASH}}, "backup_dir": str(tmp_path / "here-backups"),
                               "jobs_dir": str(tmp_path / "here" / "jobs"), "servers_file": "servers.yaml"},
                              base_dir=conf)
    customer = create_app(env["settings"])
    app = create_app(settings, link_transport=httpx.ASGITransport(app=customer))
    async with running(app), running(customer):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=BasicAsSession(app)),
                                     base_url="http://testserver") as c:
            c.settings, c.customer, c.app = settings, customer, app
            yield c


async def through(client, headers, link, tool, arguments):
    """call_linked (its argument tool would clash with call's own)."""
    resp = await rpc(client, "tools/call", {"name": "call_linked", "arguments": {
        "link": link, "tool": tool, "arguments": arguments}}, headers=headers)
    result = resp.json()["result"]
    if result.get("isError"):
        return {"error": result["content"][0]["text"]}
    content = result.get("structuredContent")
    return content.get("result", content) if set(content) == {"result"} else content


async def test_upload_for_a_linked_machine(restoring, here, env, tmp_path):
    """The client puts the file here; this OmniaCLI sends it on through the
    link and keeps no copy; restore_new_instance there, through call_linked."""
    from .test_app import AUTH

    resp = await here.post("/api/access/tokens/save", headers={"Authorization": AUTH, "Origin": "http://testserver"},
                           json={"name": "boss", "role": "admin", "new": True})
    admin = {**HEADERS, "Authorization": f"Bearer {resp.json()['token']}"}
    body = backup_zip(tmp_path / "acme.zip").read_bytes()
    answer = await call(here, "upload_backup", headers=admin, name="acme.zip", size=len(body), workspace="acme",
                        link="cliente")
    assert "call_linked" in answer["then"]
    resp = await put(here, answer, body, headers=admin)
    assert resp.status_code == 200 and resp.json()["link"] == "cliente", resp.text
    job = await wait_job(here, resp.json()["job"])
    assert job["status"] == "success", job
    assert job["result"]["file"] == "acme.zip" and job["result"]["workspace"] == "acme"
    there = env["settings"].backup_dir / ".imports" / "acme" / "acme.zip"
    assert there.read_bytes() == body
    assert not any((here.settings.backup_dir / ".imports" / ".outgoing").iterdir())
    plan = await through(here, admin, "cliente", "restore_new_instance", {"file": "acme.zip", "workspace": "acme"})
    assert plan["dry_run"] and plan["missing_modules"]["in_oca"][0]["repository"] == "web"
    # upload_backup through a link: refused there, it is asked here
    refused = await through(here, admin, "cliente", "upload_backup", {"name": "a.zip", "size": 3, "workspace": "acme"})
    assert "with link=" in refused["error"]
    # a token never reaches the receiving end of a link
    resp = await here.post("/api/imports/receive?workspace=acme&file=a.zip", content=b"x",
                           headers={"Authorization": admin["Authorization"]})
    assert resp.status_code == 403
