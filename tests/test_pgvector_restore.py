# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A database whose dump makes the extension vector (the ai module of Odoo
Enterprise 19 installed in it) restored (README, PostgreSQL with pgvector):
Odoo's restore goes past psql's errors, so into a PostgreSQL without vector it
would lose the tables of vectors and say it succeeded. A new instance or a
staging made from such a file gets pgvector in its own PostgreSQL before the
restore, and its plan says so; the production is not touched. A restore into
an existing instance without it is refused with the remedy, before anything
is touched."""
import json
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("mcp")  # the restore into a new instance is MCP's restore_new_instance

from omniacli import dumpinfo, machinedb, workspaceops  # noqa: E402
from omniacli import docker as docker_plugin  # noqa: E402
from omniacli.jobs import JobFailed  # noqa: E402

from .test_app import AUTH, POST, client, wait_job  # noqa: E402,F401
from .test_from_backup import people  # noqa: E402,F401
from .test_mcp_restore import MODULES, dump_sql, restoring  # noqa: E402,F401
from .test_workspace_repositories import instance  # noqa: E402,F401
from .test_workspaces import workspaces  # noqa: E402,F401

ADMIN = {"Authorization": AUTH}
PLAN = "/api/workspaces/{ws}/instances/from-backup"
REPO_TEMPLATES = Path(__file__).parent.parent / "templates"
PGVECTOR = "pgvector/pgvector:pg17-trixie"
# what pg_dump writes before the tables of a database with the ai module of Odoo Enterprise (read on 23/11/2026)
EXTENSIONS = ("CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;\n"
              "COMMENT ON EXTENSION pg_trgm IS 'text similarity measurement';\n"
              "CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;\n"
              "COMMENT ON EXTENSION vector IS 'vector data type and ivfflat and hnsw access methods';\n")


def vector_zip(path: Path, modules: dict[str, str] = MODULES) -> Path:
    """An Odoo backup whose dump makes vector and has a table with a column of it, as ai_embedding."""
    sql = dump_sql(modules)
    head, rest = sql.split("CREATE TABLE", 1)
    sql = (head + EXTENSIONS + "CREATE TABLE public.ai_embedding (id integer, embedding public.vector(1536));\n"
           + "CREATE TABLE" + rest)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"db_name": "Acme_Prod", "version": "20.0", "major_version": "20.0",
                                                 "pg_version": "16.0"}))
        zf.writestr("dump.sql", sql)
    return path


def compose_of(image: str) -> str:
    return (REPO_TEMPLATES / "odoo-19" / "compose.yaml").read_text().replace(f"image: {PGVECTOR}", f"image: {image}")


def test_the_extensions_a_dump_makes(tmp_path):
    assert dumpinfo.extensions(vector_zip(tmp_path / "a.zip")) == {"pg_trgm", "vector"}
    assert dumpinfo.read(tmp_path / "a.zip")["extensions"] == ["pg_trgm", "vector"]
    plain = tmp_path / "b.sql"
    plain.write_text(dump_sql({"base": "installed"}))
    assert dumpinfo.extensions(plain) == set() and dumpinfo.read(plain)["extensions"] == []
    # read up to the first table only: a CREATE EXTENSION after it is no extension of the dump's head
    late = tmp_path / "c.sql"
    late.write_text(dump_sql({"base": "installed"}) + EXTENSIONS)
    assert dumpinfo.extensions(late) == set()


def test_whether_a_database_gives_vector(tmp_path, monkeypatch):
    project = tmp_path / "main" / "production"
    project.mkdir(parents=True)
    (project / "compose.yaml").write_text(compose_of("postgres:17"))
    # not made by OmniaCLI (no instance.json): not guessed
    assert machinedb.vector_ready(project, "production", "db") is None
    (project.parent / "instance.json").write_text(json.dumps({"template": "odoo-19"}))
    assert machinedb.vector_ready(project, "production", "db") is False
    (project / "compose.yaml").write_text(compose_of(PGVECTOR))
    # the image alone is not enough: the template with vector that odoo.conf names, which the record says
    assert machinedb.vector_ready(project, "production", "db") is False
    (project.parent / "instance.json").write_text(json.dumps({"template": "odoo-19", "pgvector": ["production"]}))
    assert machinedb.vector_ready(project, "production", "db") is True
    assert machinedb.vector_ready(project, "test", "db") is False
    assert machinedb.vector_ready(project, "production", "remote") is None
    # on the machine's cluster: the cluster's image
    cluster = tmp_path / "pg17"
    cluster.mkdir()
    (cluster / "compose.yaml").write_text(machinedb.compose_text("17", 990, 990, ["/srv"], image="postgres:17"))
    monkeypatch.setattr(machinedb, "registry", lambda: {"17": {"storage": "main", "project": str(cluster)}})
    assert machinedb.vector_ready(project, "production", machinedb.DB_SERVICE) is False
    (cluster / "compose.yaml").write_text(machinedb.compose_text("17", 990, 990, ["/srv"]))
    assert machinedb.vector_ready(project, "production", machinedb.DB_SERVICE) is True


async def test_a_restore_into_an_instance_without_vector_is_refused_untouched(instance, tmp_path, monkeypatch):  # noqa: F811
    folder, settings = instance["folder"], instance["settings"]
    (folder / "production").mkdir(parents=True, exist_ok=True)
    (folder / "production" / "compose.yaml").write_text(compose_of("postgres:17"))
    (folder / "instance.json").write_text(json.dumps({"template": "odoo-19"}))
    inst = settings.servers["acme-main"].production
    done = []

    async def major(instance_):
        return 20

    async def stop(job, instance_):
        done.append(("stop", instance_.key))

    async def helper(job, instance_, *args, **kw):
        done.append(args[0])
    monkeypatch.setattr(docker_plugin, "instance_major", major)
    monkeypatch.setattr(docker_plugin, "stop", stop)
    monkeypatch.setattr(docker_plugin, "_helper", helper)
    job = type("J", (), {"lines": [], "log": lambda self, line: self.lines.append(line)})()
    backup = vector_zip(tmp_path / "ai.zip")
    with pytest.raises(JobFailed) as found:
        await inst.plugin.restore(job, inst, backup, {"neutralize": True})
    said = str(found.value)
    assert said.startswith("ai.zip uses the extension vector of PostgreSQL (the ai module of Odoo Enterprise), which "
                           "the PostgreSQL of acme-main/production does not have: restored, its tables of vectors would "
                           "be lost. Nothing restored, acme-main/production not touched")
    assert "Add pgvector to PostgreSQL… in Settings › Odoo Enterprise of acme-main" in said and done == []
    # a backup without vector, or a PostgreSQL with it: restored as before
    plain = tmp_path / "plain.zip"
    with zipfile.ZipFile(plain, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"major_version": "20.0"}))
        zf.writestr("dump.sql", dump_sql({"base": "installed"}))
    monkeypatch.setattr(type(inst.plugin), "_update_changed", lambda self, *a, **k: _none())
    await inst.plugin.restore(job, inst, plain, {"neutralize": True})
    assert done[:2] == [("stop", "acme-main/production"), "load"]
    (folder / "production" / "compose.yaml").write_text(compose_of(PGVECTOR))
    (folder / "instance.json").write_text(json.dumps({"template": "odoo-19", "pgvector": ["production"]}))
    done.clear()
    await inst.plugin.restore(job, inst, backup, {"neutralize": True})
    assert done[:2] == [("stop", "acme-main/production"), "load"]


async def _none():
    return None


async def test_a_new_instance_and_a_staging_from_a_file_with_vector(people, client, env, restoring, tmp_path,  # noqa: F811
                                                                    monkeypatch):
    """The template of this machine runs postgres:17 (one of before): the new
    instance's own PostgreSQL gets pgvector before the restore; the plan says
    it, and the plan of a staging says its production is not touched."""
    settings = env["settings"]
    folder = settings.backup_dir / ".imports" / "acme"
    folder.mkdir(parents=True)
    vector_zip(folder / "ai.zip")
    made = workspaceops.create_instance
    order = []

    async def create_instance(job, ws, instance, tpl, values, setup=None):
        answer = await made(job, ws, instance, tpl, values, setup)
        project = Path(answer["production"]["restart"]["service"])
        (project / "compose.yaml").write_text(compose_of("postgres:17"))
        return answer

    async def helper(job, *args, timeout=None, input=None):
        order.append(args)
        return {"image": PGVECTOR, "template": machinedb.VECTOR_TEMPLATE}

    restore = docker_plugin.DockerPlugin.restore

    async def restored(self, job, inst, backup, options):
        order.append(("restore", inst.key))
        await restore(self, job, inst, backup, options)
    monkeypatch.setattr(workspaceops, "create_instance", create_instance)
    monkeypatch.setattr(workspaceops, "_helper", helper)
    monkeypatch.setattr(docker_plugin.DockerPlugin, "restore", restored)
    plan = (await client.get(PLAN.format(ws="acme") + "?file=ai.zip", headers=ADMIN)).json()
    assert plan["pgvector"] == {"text": "The backup uses the extension vector of PostgreSQL (the ai module of Odoo "
                                        "Enterprise): the new instance will have PostgreSQL with pgvector",
                                "problem": None}
    # in the reader's language
    plan_it = (await client.get(PLAN.format(ws="acme") + "?file=ai.zip", headers={**ADMIN, "Accept-Language": "it"})).json()
    assert plan_it["pgvector"]["text"].endswith("la nuova istanza avrà PostgreSQL con pgvector")
    resp = await client.post(PLAN.format(ws="acme"), headers=POST,
                             json={"server": "", "file": "ai.zip", "name": "main", "allow_missing": True})
    assert resp.status_code == 200, resp.text
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success", job["lines"]
    storage = str(settings.workspaces["acme"].storage)
    # pgvector first, on its own PostgreSQL, then the restore
    assert order == [("db-pgvector", "acme", storage, "main", "production"), ("restore", "acme-main/production")]
    assert any("gets PostgreSQL with pgvector before the restore; its production is not touched" in line
               for line in job["lines"])
    # a staging of it from the same file: said in its plan; the production stays on postgres:17
    staging = (await client.get(PLAN.format(ws="acme") + "?file=ai.zip&staging_of=acme-main", headers=ADMIN)).json()
    assert staging["pgvector"]["text"].endswith("the staging will have PostgreSQL with pgvector; its production is not "
                                                "touched")
    production = Path(settings.servers["acme-main"].production.restart.service)
    assert f"image: {PGVECTOR}" not in (production / "compose.yaml").read_text()


async def test_on_a_cluster_without_pgvector_the_plan_refuses(people, client, env, restoring, tmp_path,  # noqa: F811
                                                             monkeypatch):
    settings = env["settings"]
    folder = settings.backup_dir / ".imports" / "acme"
    folder.mkdir(parents=True)
    vector_zip(folder / "ai.zip")
    template = next(iter(workspaceops.templates().values()))
    machine = type(template)(**{**template.__dict__, "database": "machine"})
    assert machine.on_machine
    monkeypatch.setattr(workspaceops, "templates", lambda: {machine.name: machine})
    monkeypatch.setattr(machinedb, "cluster_has_vector", lambda: False)
    plan = (await client.get(PLAN.format(ws="acme") + "?file=ai.zip", headers=ADMIN)).json()
    assert plan["pgvector"]["problem"].startswith("The backup uses the extension vector of PostgreSQL, which the "
                                                  "machine's cluster does not have")
    resp = await client.post(PLAN.format(ws="acme"), headers=POST,
                             json={"server": "", "file": "ai.zip", "name": "main", "allow_missing": True})
    assert resp.status_code == 400 and "Database tab of the machine first" in resp.json()["error"]
    assert restoring["made"] == []
