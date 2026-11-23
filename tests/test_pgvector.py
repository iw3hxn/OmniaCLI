# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""PostgreSQL with pgvector (README, Odoo Enterprise): the ai module of Odoo
Enterprise 19 makes the extension vector in its pre_init_hook, and every
install that brings it in fails without it. The templates' database service
and the machine's cluster run the image with pgvector; Add pgvector to
PostgreSQL… switches an existing one (only the database's container made
again) and makes vector where Odoo's role may not: in its databases and in
the template database Odoo creates the others from; the install job says the
cause and the remedy instead of the traceback, and a retry that passes
without ai is not a plain success."""
import dataclasses
import json
import socket
import types
from pathlib import Path

import pytest
import yaml

from omniacli import i18n, machinedb, machines, stacktemplates, workspacehelper, workspaceops
from omniacli.composehelper import HelperError
from omniacli.docker import DockerPlugin
from omniacli.jobs import JobFailed

from .test_app import AUTH, client, wait_job  # noqa: F401 - the client fixture
from .test_enterprise import _state, ent  # noqa: F401 - acme-main of odoo-19, Enterprise here
from .test_modules_catalog import _read_catalog, modules  # noqa: F401 - the catalog's fixture
from .test_workspace_repositories import instance  # noqa: F401
from .test_workspaces import post_as, workspaces  # noqa: F401

POST = {"Authorization": AUTH, "Origin": "http://omniacli.test"}
REPO_TEMPLATES = Path(__file__).parent.parent / "templates"
PGVECTOR = "pgvector/pgvector:pg17-trixie"
# what Odoo 19 logged on ber when the ai module of Odoo Enterprise came in with another module (23/11/2026)
AI_FAILED = [
    "2026-11-23 13:18:47,446 80 INFO odoo odoo.modules.loading: Loading module ai (190/238)",
    "2026-11-23 13:18:47,554 80 WARNING odoo odoo.modules.loading: Transient module states were reset",
    "2026-11-23 13:18:47,556 80 CRITICAL odoo odoo.service.server: Failed to initialize database `odoo`.",
    '  File "/mnt/enterprise/ai/__init__.py", line 27, in pgvector_is_available',
    "odoo.exceptions.UserError: PostgreSQL extension 'vector' is required to enable RAG for AI agents.",
]


# --- the images ------------------------------------------------------------------

def test_the_templates_and_the_cluster_run_postgresql_with_pgvector():
    assert machinedb.IMAGE["17"] == PGVECTOR and machinedb.PLAIN_IMAGE["17"] == "postgres:17"
    assert machinedb.has_vector(PGVECTOR) and machinedb.has_vector("pgvector/pgvector:0.8.7-pg17-trixie")
    assert not machinedb.has_vector("postgres:17") and not machinedb.has_vector("")
    # a template of before (or a downloaded one) on postgres:17 is still taken; another major, its own image only
    assert stacktemplates.postgres_images("17") == (PGVECTOR, "postgres:17")
    assert stacktemplates.postgres_images("16") == ("postgres:16",)
    own = [t for t in sorted(REPO_TEMPLATES.iterdir()) if (t / "template.json").is_file()
           and stacktemplates.load_template(t).own_database]
    assert {t.name for t in own} == {"odoo-16", "odoo-17", "odoo-18", "odoo-19", "odoo-20", "odooplm-20"}
    for folder in own:
        services = yaml.safe_load((folder / "compose.yaml").read_text())["services"]
        assert services["db"]["image"] == PGVECTOR, folder.name
    # odoo-20 names its PostgreSQL in template.json: its image is checked
    old = stacktemplates.load_template(REPO_TEMPLATES / "odoo-20")
    assert old.postgres == "17" and not stacktemplates.check_compose(dataclasses.replace(old, compose=old.compose.replace(PGVECTOR, "postgres:17")))
    found = stacktemplates.check_compose(dataclasses.replace(old, compose=old.compose.replace(PGVECTOR, "postgres:16")))
    assert found and "pgvector/pgvector:pg17-trixie or postgres:17" in found[0]


def test_a_cluster_keeps_its_image_when_its_port_changes():
    made = machinedb.compose_text("17", 990, 990, ["/srv"])
    assert machinedb.image_of(made) == PGVECTOR
    before = machinedb.compose_text("17", 990, 990, ["/srv"], image="postgres:17")
    assert machinedb.image_of(before) == "postgres:17"
    # what listen_on and listen_off write again: the image it runs, whatever IMAGE says now
    again = machinedb.compose_text("17", 990, 990, machinedb._roots_of(before), 15432, machinedb.image_of(before))
    assert machinedb.image_of(again) == "postgres:17" and '"0.0.0.0:15432:5432"' in again
    with pytest.raises(machinedb.ClusterError, match="not an image"):
        machinedb.compose_text("17", 990, 990, ["/srv"], image="postgres:17\n  privileged: true")


def test_odoo_conf_names_the_template_with_vector():
    template = stacktemplates.load_template(REPO_TEMPLATES / "odoo-19")
    assert "{{DB_TEMPLATE}}" not in template.conf
    text = stacktemplates.conf_text(template, "main", "pw", [], db_template=machinedb.VECTOR_TEMPLATE)
    assert "\ndb_template = omniacli_template\n" in text and text.count("db_template") == 1
    assert "db_template" not in stacktemplates.conf_text(template, "main", "pw", [])
    # the one on the machine's cluster names its workspace's template, once
    machine = stacktemplates.load_template(REPO_TEMPLATES / "odoo-19-machine")
    text = stacktemplates.conf_text(machine, "main", "pw", [], db_template="omniacli.tpl.acme")
    assert text.count("db_template") == 1 and "db_template = omniacli.tpl.acme" in text


def test_dbinit_makes_the_template_with_vector_only_where_the_image_has_it():
    script = workspacehelper.DB_ROLE_SCRIPT
    assert script.index("CREATE ROLE odoo") < script.index("pg_available_extensions WHERE name = 'vector'")
    sql = workspacehelper.VECTOR_TEMPLATE_SQL
    assert sql in script and "TEMPLATE template0 ENCODING ''UTF8'' LC_COLLATE ''C''" in sql
    assert sql.index("IS_TEMPLATE true ALLOW_CONNECTIONS true") < sql.index("\\c omniacli_template\n") \
        < sql.index("CREATE EXTENSION IF NOT EXISTS vector;") < sql.index("ALLOW_CONNECTIONS false")


# --- the machine's cluster ---------------------------------------------------------

@pytest.fixture
def cluster(tmp_path, monkeypatch):
    """The cluster of PostgreSQL 17 on postgres:17, a workspace's template and two
    environments; its SQL, its compose and docker pull recorded."""
    project = tmp_path / "pg17"
    project.mkdir()
    (project / "compose.yaml").write_text(machinedb.compose_text("17", 990, 990, ["/srv"], image="postgres:17"))
    state = {"sql": [], "compose": [], "pulled": [], "available": True}

    def sql(project_, database, statements):
        state["sql"].append((database, statements))
        if "pg_available_extensions" in statements:
            return "1\n" if state["available"] else ""
        if "LIKE 'omniacli.tpl.%'" in statements:
            return "omniacli.tpl.acme\n"
        if statements.startswith("SELECT 1 FROM pg_database"):
            return "1\n"
        return ""

    def run(project_, *args, **kw):
        state["compose"].append(args)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    def pull(cmd, **kw):
        state["pulled"].append(cmd)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    def write(path, text, mode=0o644, uid=0, gid=0):
        Path(path).write_text(text)
    monkeypatch.setattr(machinedb, "registry", lambda: {"17": {"storage": "main", "project": str(project)}})
    monkeypatch.setattr(machinedb, "sql", sql)
    monkeypatch.setattr(machinedb, "_run", run)
    monkeypatch.setattr(machinedb, "ready", lambda project_, *a, **k: None)
    monkeypatch.setattr(machinedb, "write", write)
    monkeypatch.setattr(machinedb, "pg_user", lambda create=False: (990, 990))
    monkeypatch.setattr(machinedb, "environments", lambda project_: {"acme_main": {}, "acme_shop": {}})
    monkeypatch.setattr(machinedb, "grants", lambda project_: {})
    monkeypatch.setattr(machinedb, "replica", lambda project_: None)
    monkeypatch.setattr(machinedb, "listening", lambda project_: None)
    monkeypatch.setattr(machinedb, "_docker", lambda: "docker")
    monkeypatch.setattr(machinedb.subprocess, "run", pull)
    return {"project": project, **state, "state": state}


def test_add_pgvector_to_the_cluster(cluster):
    state, project = cluster["state"], cluster["project"]
    done = machinedb.add_vector("17")
    # the image pulled first, a checkpoint, then the container made again on it
    assert state["pulled"] == [["docker", "pull", "-q", PGVECTOR]]
    assert ("postgres", "CHECKPOINT;\n") in state["sql"] and ("up", "-d", "--remove-orphans") in state["compose"]
    assert machinedb.image_of((project / "compose.yaml").read_text()) == PGVECTOR
    assert done["was"] == "postgres:17" and done["image"] == PGVECTOR and done["restarted_s"] is not None
    # vector in the template (connections allowed for that moment only) and in each environment's database
    assert done["databases"] == ["omniacli.tpl.acme", "acme_main", "acme_shop"]
    tpl = [s for s in state["sql"] if "omniacli.tpl.acme" in s[0] or "omniacli.tpl.acme" in s[1]]
    assert [s[1] for s in tpl] == ['ALTER DATABASE "omniacli.tpl.acme" ALLOW_CONNECTIONS true;\n',
                                  "CREATE EXTENSION IF NOT EXISTS vector;\n",
                                  'ALTER DATABASE "omniacli.tpl.acme" ALLOW_CONNECTIONS false;\n']
    assert ("acme_main", machinedb.VECTOR_SQL) in state["sql"] and ("acme_shop", machinedb.VECTOR_SQL) in state["sql"]
    # again: on pgvector already, the extension only, nothing pulled or restarted
    state["pulled"].clear()
    state["compose"].clear()
    again = machinedb.add_vector("17")
    assert again["restarted_s"] is None and state["pulled"] == [] and state["compose"] == []


def test_a_new_workspace_on_the_cluster_takes_vector_in_its_template(cluster):
    state = cluster["state"]
    machinedb.vector_in_template(cluster["project"], "omniacli.tpl.beta")
    assert [s for s in state["sql"] if s[0] == "omniacli.tpl.beta"] == [("omniacli.tpl.beta", machinedb.VECTOR_SQL)]
    state["available"] = False
    assert machinedb.sync_vector(cluster["project"]) == {"available": False, "databases": []}


def test_a_failed_pull_changes_nothing(cluster, monkeypatch):
    monkeypatch.setattr(machinedb.subprocess, "run", lambda cmd, **kw: types.SimpleNamespace(
        returncode=1, stdout="", stderr="toomanyrequests"))
    with pytest.raises(machinedb.ClusterError, match="nothing was changed: toomanyrequests"):
        machinedb.add_vector("17")
    assert machinedb.image_of((cluster["project"] / "compose.yaml").read_text()) == "postgres:17"
    assert cluster["state"]["compose"] == []


# --- a database of its own (the root helper's db-pgvector) -----------------------------

@pytest.fixture
def dedicated(tmp_path, monkeypatch):
    """acme/main with its production on postgres:17, made from odoo-19; the
    helper's docker, SQL and conf recorded."""
    mount = tmp_path / "acme"
    project = mount / "main" / "production"
    project.mkdir(parents=True)
    compose = (REPO_TEMPLATES / "odoo-19" / "compose.yaml").read_text().replace(PGVECTOR, "postgres:17")
    (project / "compose.yaml").write_text(compose)
    (mount / "main" / "instance.json").write_text(json.dumps({"template": "odoo-19", "database": "main"}))
    state = {"dc": [], "sql": [], "conf": [], "pulled": [], "ready_fails": False}

    def dc(project_, *args, **kw):
        state["dc"].append(args)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    def dsql(project_, database, statements):
        state["sql"].append((database, statements))
        return "main\nmain_copy\n" if "pg_get_userbyid(datdba)" in statements else ""

    def ready(project_, timeout=180):
        if state["ready_fails"]:
            raise HelperError("the dedicated database does not answer within 180 s")

    def pull(cmd, **kw):
        state["pulled"].append(cmd)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(workspacehelper, "workspace", lambda name, storage: (tmp_path / "acme.img", mount))
    monkeypatch.setattr(workspacehelper, "mounted", lambda image, mount_: True)
    monkeypatch.setattr(workspacehelper.os, "chown", lambda *args: None)
    monkeypatch.setattr(workspacehelper, "_dc", dc)
    monkeypatch.setattr(workspacehelper, "_dedicated_sql", dsql)
    monkeypatch.setattr(workspacehelper, "_dedicated_ready", ready)
    monkeypatch.setattr(workspacehelper, "conf", lambda *args: state["conf"].append(args) or {})
    monkeypatch.setattr(workspacehelper.subprocess, "run", pull)
    return {"project": project, "mount": mount, "compose": compose, "state": state}


def test_db_pgvector_switches_the_image_of_its_database_only(dedicated):
    state, project = dedicated["state"], dedicated["project"]
    done = workspacehelper.db_pgvector("acme", "main", "main", "production")
    assert state["pulled"] == [["docker", "pull", "-q", PGVECTOR]]
    # its compose.yaml as it was but the database's image; only that container made again, a long stop allowed
    assert (project / "compose.yaml").read_text() == dedicated["compose"].replace("image: postgres:17\n",
                                                                                f"image: {PGVECTOR}\n")
    assert state["dc"] == [("up", "-d", "--no-deps", "--timeout", "120", "db")]
    statements = [s for _d, s in state["sql"]]
    assert statements[0] == "CHECKPOINT;\n" and statements[1] == workspacehelper.VECTOR_TEMPLATE_SQL
    assert ("main", machinedb.VECTOR_SQL) in state["sql"] and ("main_copy", machinedb.VECTOR_SQL) in state["sql"]
    # its record says it, and odoo.conf is written again (it names the template)
    meta = json.loads((dedicated["mount"] / "main" / "instance.json").read_text())
    assert meta["pgvector"] == ["production"] and state["conf"] == [("acme", "main", "main", "production", "{}")]
    assert done["was"] == "postgres:17" and done["image"] == PGVECTOR and done["databases"] == ["main", "main_copy"]
    assert done["restarted_s"] is not None
    # again: the template and the extension only, nothing pulled nor restarted
    state["dc"].clear()
    state["pulled"].clear()
    again = workspacehelper.db_pgvector("acme", "main", "main", "production")
    assert again["restarted_s"] is None and state["dc"] == [] and state["pulled"] == []


def test_db_pgvector_puts_back_what_does_not_start(dedicated):
    state, project = dedicated["state"], dedicated["project"]
    state["ready_fails"] = True
    with pytest.raises(HelperError, match="did not start on pgvector/pgvector:pg17-trixie .*it runs on postgres:17 again"):
        workspacehelper.db_pgvector("acme", "main", "main", "production")
    assert (project / "compose.yaml").read_text() == dedicated["compose"]
    assert state["dc"][-1] == ("up", "-d", "--no-deps", "db")
    meta = json.loads((dedicated["mount"] / "main" / "instance.json").read_text())
    assert "pgvector" not in meta and state["conf"] == []


def test_db_pgvector_refuses_an_image_changed_by_hand(dedicated):
    (dedicated["project"] / "compose.yaml").write_text(dedicated["compose"].replace("postgres:17", "postgres:16"))
    with pytest.raises(HelperError, match="runs postgres:16, not postgres:17: its image was changed by hand"):
        workspacehelper.db_pgvector("acme", "main", "main", "production")
    assert dedicated["state"]["dc"] == [] and dedicated["state"]["pulled"] == []
    with pytest.raises(HelperError, match="the role is production or test"):
        workspacehelper.db_pgvector("acme", "main", "main", "staging")


# --- the panel ------------------------------------------------------------------

def _on_postgres_17(folder: Path, *roles: str) -> None:
    for role in roles:
        (folder / role).mkdir(parents=True, exist_ok=True)
        (folder / role / "compose.yaml").write_text(
            (REPO_TEMPLATES / "odoo-19" / "compose.yaml").read_text().replace(PGVECTOR, "postgres:17"))


async def test_the_enterprise_switch_says_it_and_offers_pgvector_first(ent, client):  # noqa: F811
    folder, calls, settings = ent["folder"], ent["ent"], ent["settings"]
    _state("19.0")
    page = (await client.get("/servers/acme-main", headers={"Authorization": AUTH})).text
    # its files say no compose.yaml: nothing said (an instance whose database is not read is not guessed)
    assert 'id="ent-pgvector"' not in page
    _on_postgres_17(folder, "production", "test")
    page = (await client.get("/servers/acme-main", headers={"Authorization": AUTH})).text
    section = page.split('data-section="enterprise"', 1)[1].split("</section>", 1)[0]
    assert 'id="ent-pgvector"' in section and "the PostgreSQL of acme-main/production, acme-main/test does not have it" \
        in " ".join(section.split())
    # the action before the switch, the one primary of the section
    assert section.index('id="ent-pgvector-add"') < section.index('id="ent-switch"')
    assert section.count('class="primary"') == 1 and 'data-production="1"' in section
    assert "Add pgvector to PostgreSQL…</button>" in section
    url = "/api/servers/acme-main/database/pgvector"
    # a production: its name typed
    resp = await client.post(url, headers=POST, json={})
    assert resp.status_code == 400 and "Type acme-main to add pgvector" in resp.json()["error"]
    # a workspace's admin does not
    resp = await client.post(url, headers=post_as("mario"), json={"confirm": "acme-main"})
    assert resp.status_code == 403
    resp = await client.post(url, headers=POST, json={"confirm": "acme-main"})
    assert resp.status_code == 200, resp.text
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success", job["lines"]
    storage = str(settings.workspaces["acme"].storage)
    assert [c for c in calls["helper"] if c[0] == "db-pgvector"] == [
        ("db-pgvector", "acme", storage, "main", "production"), ("db-pgvector", "acme", storage, "main", "test")]
    assert job["title"] == "Add pgvector to the PostgreSQL of acme-main"
    # done (what the helper writes): nothing said any more, asked again it is refused
    for role in ("production", "test"):
        text = (folder / role / "compose.yaml").read_text()
        (folder / role / "compose.yaml").write_text(text.replace("image: postgres:17", f"image: {PGVECTOR}"))
    meta = json.loads((folder / "instance.json").read_text())
    (folder / "instance.json").write_text(json.dumps({**meta, "pgvector": ["production", "test"]}))
    page = (await client.get("/servers/acme-main", headers={"Authorization": AUTH})).text
    assert 'id="ent-pgvector"' not in page
    resp = await client.post(url, headers=POST, json={"confirm": "acme-main"})
    assert resp.status_code == 400 and "has pgvector already" in resp.json()["error"]


async def test_an_odoo_before_19_is_not_asked(ent, client):  # noqa: F811
    folder = ent["folder"]
    _state("19.0")
    _on_postgres_17(folder, "production")
    (folder / "instance.json").write_text(json.dumps({"template": "odoo-18", "database": "main"}))
    page = (await client.get("/servers/acme-main", headers={"Authorization": AUTH})).text
    assert 'id="ent-pgvector"' not in page
    resp = await client.post("/api/servers/acme-main/database/pgvector", headers=POST, json={"confirm": "acme-main"})
    assert resp.status_code == 400 and "or later" in resp.json()["error"]


async def test_the_install_that_odoo_stops_on_ai_says_why_and_the_remedy(modules, client, monkeypatch):  # noqa: F811
    folder = modules["folder"]
    (folder / "instance.json").write_text(json.dumps({"template": "odoo-19",
                                                      "enterprise": {"series": "19.0", "service": "odoo"}}))
    _on_postgres_17(folder, "test")
    await _read_catalog(client)

    async def install_modules(self, job, inst, names):
        for line in AI_FAILED:
            job.log(line)
        raise JobFailed("odoo -i failed (255)")
    monkeypatch.setattr(DockerPlugin, "install_modules", install_modules)
    resp = await client.post("/api/servers/acme-main/test/modules/install", headers=post_as("eva"),
                             json={"module": "report_xlsx"})
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "failed"
    error = job["error"]
    assert error.startswith("The module report_xlsx was not installed: the module ai of Odoo Enterprise came in with it")
    assert "which the database of acme-main/test does not have" in error and "tried again, the install may pass" in error
    assert "Add it with Add pgvector to PostgreSQL… in Settings › Odoo Enterprise of acme-main, then install " \
           "report_xlsx again" in error
    # in the reader's language, the remedy inside it too
    italian = i18n.tr("it", error)
    assert italian.startswith("Il modulo report_xlsx non si è installato: con lui è entrato il modulo ai")
    assert "Aggiungila con Aggiungi pgvector a PostgreSQL… in Impostazioni › Odoo Enterprise di acme-main" in italian


async def test_a_retry_that_passes_without_ai_is_no_plain_success(modules, client):  # noqa: F811
    folder = modules["folder"]
    (folder / "instance.json").write_text(json.dumps({"template": "odoo-19",
                                                      "enterprise": {"series": "19.0", "service": "odoo"}}))
    _on_postgres_17(folder, "test")
    await _read_catalog(client)
    resp = await client.post("/api/servers/acme-main/test/modules/install", headers=post_as("eva"),
                             json={"module": "report_xlsx"})
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success" and "report_xlsx is installed in acme-main/test" in "\n".join(job["lines"])
    assert any("The module ai of Odoo Enterprise cannot be installed in the database of acme-main/test" in w
               for w in job["result"]["warnings"])
    # with pgvector: a plain success
    meta = json.loads((folder / "instance.json").read_text())
    (folder / "instance.json").write_text(json.dumps({**meta, "pgvector": ["test"]}))
    (folder / "test" / "compose.yaml").write_text(
        (folder / "test" / "compose.yaml").read_text().replace("image: postgres:17", f"image: {PGVECTOR}"))
    resp = await client.post("/api/servers/acme-main/test/modules/install", headers=post_as("eva"),
                             json={"module": "report_py3o"})
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success" and not (job.get("result") or {}).get("warnings"), job["lines"]


async def test_add_pgvector_to_the_machines_cluster(client, env, tmp_path, monkeypatch):
    project = tmp_path / "pg17"
    project.mkdir()
    (project / "compose.yaml").write_text(machinedb.compose_text("17", 990, 990, ["/srv"], image="postgres:17"))
    monkeypatch.setattr(machinedb, "registry", lambda: {"17": {"storage": "main", "project": str(project)}})
    asked = []

    async def helper(job, *args, timeout=None, input=None):
        asked.append(args)
        return {"image": PGVECTOR, "was": "postgres:17", "restarted_s": 1.2, "databases": ["acme_main"], "replica": ""}
    monkeypatch.setattr(workspaceops, "_helper", helper)
    url = "/api/postgres/machine/17/pgvector"
    here = machines.here_name(socket.gethostname())
    resp = await client.post(url, headers=POST, json={"confirm": "nope"})
    assert resp.status_code == 400 and f"Type {here} to add pgvector to the machine's PostgreSQL 17" in resp.json()["error"]
    assert (await client.post("/api/postgres/machine/16/pgvector", headers=POST, json={})).status_code == 404
    resp = await client.post(url, headers=POST, json={"confirm": here})
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success", job["lines"]
    assert asked == [("machine-db-pgvector", "17")]
    assert any("its container was made again in 1.2 s" in line for line in job["lines"])
    (project / "compose.yaml").write_text(machinedb.compose_text("17", 990, 990, ["/srv"]))
    resp = await client.post(url, headers=POST, json={"confirm": here})
    assert resp.status_code == 400 and "has pgvector already" in resp.json()["error"]


def test_the_routes_are_posts_of_the_machines_admins():
    """Both actions change the machine: POST only (no GET route to list in audit.py), never a capability of a
    workspace (capabilities.ROUTES lists those)."""
    from omniacli import capabilities
    assert not any("pgvector" in path for _method, path in capabilities.ROUTES)
