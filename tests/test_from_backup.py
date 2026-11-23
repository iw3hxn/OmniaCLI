# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Backups page makes a new instance from a backup, and takes a
database uploaded from the person's computer (README, Instances and
Backups of every machine): the plan is restore_new_instance's dry run
(GET /api/workspaces/<ws>/instances/from-backup), the job its job (POST);
the upload goes in parts that resume into the workspace's import area,
checked, and its file is a row of the list for 7 days."""

import json
import os
import time
import zipfile

import pytest

pytest.importorskip("mcp")  # the restore code is MCP's restore_new_instance

from omniacli import instancearchive  # noqa: E402
from omniacli.auth import hash_password  # noqa: E402
from omniacli.config import Access, Workspace  # noqa: E402

from .test_app import AUTH, POST, client, wait_job  # noqa: E402,F401
from .test_mcp_restore import backup_zip, dump_sql, here, restoring  # noqa: E402,F401
from .test_workspaces import PASSWORD, basic, post_as  # noqa: E402

ADMIN = {"Authorization": AUTH}
PLAN = "/api/workspaces/{ws}/instances/from-backup"
UPLOAD = "/api/workspaces/{ws}/imports/upload"


@pytest.fixture
def people(env, restoring):
    """acme (mario its admin, luca a user) and beta (eva its admin), both with storage."""
    settings = env["settings"]
    password = hash_password(PASSWORD, iterations=1000)
    names = ("mario", "luca", "eva")
    settings.set_web_access(Access(users={n: password for n in names}, roles={n: "workspace" for n in names}))
    settings.workspaces = {
        "acme": Workspace("acme", "ACME", servers=[], storage="main", limits={"disk_gb": 10, "instances": 5},
                          members={"mario": "admin", "luca": "user"}),
        "beta": Workspace("beta", "Beta", servers=[], storage="main", limits={"disk_gb": 10},
                          members={"eva": "admin"}),
    }
    return settings


async def upload(client, ws, name, body: bytes, parts=3, headers=POST, base=""):
    """The page's upload: the file in parts, each at its offset; the last answer."""
    size, step = len(body), max(1, -(-len(body) // parts))
    url = f"{base}{UPLOAD.format(ws=ws)}?name={name}&size={size}"
    offset, resp = 0, None
    while offset < size:
        resp = await client.post(f"{url}&offset={offset}", content=body[offset:offset + step],
                                 headers={**headers, "Content-Type": "application/octet-stream"})
        if resp.status_code != 200 or "file" in resp.json():
            return resp
        offset = resp.json()["received"]
    return resp


async def test_an_upload_in_parts_then_a_new_instance(people, client, env, restoring, tmp_path):
    settings = env["settings"]
    places = (await client.get("/api/imports/places", headers=ADMIN)).json()
    assert [w["name"] for w in places["workspaces"]] == ["acme", "beta"] and places["keep_days"] == 7
    assert places["part"] == 16 << 20 and places["max_size"] == 50 << 30
    body = backup_zip(tmp_path / "acme.zip").read_bytes()
    url = UPLOAD.format(ws="acme") + f"?name=acme.zip&size={len(body)}"
    # the first part; one at another offset is told where to go on from; the resume says it too
    first = await client.post(f"{url}&offset=0", content=body[:100], headers=POST)
    assert first.json() == {"received": 100}
    wrong = await client.post(f"{url}&offset=40", content=body[40:80], headers=POST)
    assert wrong.status_code == 409 and wrong.json()["received"] == 100
    assert (await client.get(url, headers=ADMIN)).json()["received"] == 100
    middle = await client.post(f"{url}&offset=100", content=body[100:200], headers=POST)
    assert middle.json() == {"received": 200}
    done = await client.post(f"{url}&offset=200", content=body[200:], headers=POST)
    assert done.status_code == 200, done.text
    facts = done.json()
    assert facts["file"] == "acme.zip" and facts["odoo"] == "20.0" and facts["database"] == "Acme_Prod"
    assert facts["filestore"] is True and facts["modules"] == 4 and facts["keep_days"] == 7
    kept = settings.backup_dir / ".imports" / "acme" / "acme.zip"
    assert kept.read_bytes() == body and oct(kept.stat().st_mode & 0o777) == "0o600"
    # nothing else left beside it but what its check found
    assert sorted(p.name for p in kept.parent.iterdir()) == [".acme.zip.facts", "acme.zip"]
    # a row of the Backups page, uploaded, with what it holds
    row = next(r for r in (await client.get("/api/backups", headers=ADMIN)).json()["rows"] if r.get("imported"))
    assert (row["workspace"], row["file"], row["kind"], row["odoo"], row["database"]) == (
        "acme", "acme.zip", "uploaded", "20.0", "Acme_Prod")
    assert row["can_instance"] and not row["can_stage"] and row["can_delete"] and row["expires"] > row["time"]
    # the plan: restore_new_instance's dry run, nothing started
    plan = (await client.get(PLAN.format(ws="acme") + "?file=acme.zip", headers=ADMIN)).json()
    assert plan["odoo"] == "20.0" and plan["instance"] == "acme-prod" and plan["name_problem"] == ""
    assert plan["new_instance"]["template"] == "odooplm-20" and plan["new_instance"]["server"] == "acme-acme-prod"
    assert plan["missing_modules"]["in_oca"][0]["repository"] == "web"
    assert [w["name"] for w in plan["workspaces"]] == ["acme", "beta"] and "next" not in plan
    # a file made elsewhere: its users and passwords come with it, and the plan says so
    assert "passwords are the ones of the file" in plan["passwords"]
    assert restoring["made"] == []
    bad = (await client.get(PLAN.format(ws="acme") + "?file=acme.zip&name=Bad%20Name", headers=ADMIN)).json()
    assert "lower case" in bad["name_problem"] and bad["instance"] == "acme-prod"
    # created: modules missing refused unless allowed, then the job of restore_new_instance
    create = {"server": "", "file": "acme.zip", "name": "main", "neutralize": False}
    refused = await client.post(PLAN.format(ws="acme"), headers=POST, json=create)
    assert refused.status_code == 400 and "nothing started" in refused.json()["error"]
    made = await client.post(PLAN.format(ws="acme"), headers=POST,
                             json={**create, "add_repositories": True, "allow_missing": True})
    assert made.status_code == 200, made.text
    job = await wait_job(client, made.json()["job"])
    assert job["status"] == "success", job
    assert made.json()["server"] == "acme-main" and job["source"] == "manual: admin"
    assert restoring["restore"][-1] == ("acme-main/production", ["dump.sql", "filestore/ab/abcdef", "manifest.json"],
                                        {"neutralize": False})
    assert [r.name for r in settings.servers["acme-main"].production.repositories] == ["web"]
    # the name is taken now: said in the plan, refused at Create
    taken = (await client.get(PLAN.format(ws="acme") + "?file=acme.zip&name=main", headers=ADMIN)).json()
    assert "main is taken in workspace acme" in taken["name_problem"]
    again = await client.post(PLAN.format(ws="acme"), headers=POST, json={**create, "allow_missing": True})
    assert again.status_code == 400 and "taken" in again.json()["error"]
    # a staging of that production from the uploaded file: its plan, the productions it may be of
    staging = (await client.get(PLAN.format(ws="acme") + "?file=acme.zip&staging_of=acme-main", headers=ADMIN)).json()
    assert staging["new_instance"]["staging_of"] == "acme-main" and staging["instance"] == ""
    assert [p["server"] for p in staging["productions"]] == ["acme-main"]
    # every action in the access log: the parts, the instance made
    details = [e["detail"] for e in client.app.state.access_log.recent()]
    assert any(d.startswith("POST /api/workspaces/acme/instances/from-backup") for d in details)
    assert any(d.startswith("POST /api/workspaces/acme/imports/upload") for d in details)
    # deleted from its row
    assert (await client.post("/api/workspaces/acme/imports/delete", headers=POST, json={"file": "acme.zip"})).json() \
        == {"deleted": "acme.zip"}
    assert not any(kept.parent.iterdir())


async def test_a_wrong_file_is_refused_with_its_reason(people, client, env, tmp_path):
    folder = env["settings"].backup_dir / ".imports" / "acme"
    # not a zip: refused at its first part, nothing kept
    resp = await upload(client, "acme", "x.zip", b"hello, not a zip" * 10)
    assert resp.status_code == 400 and "x.zip is not a zip file" in resp.json()["error"]
    # a pg_dump of the custom format: by its name before a byte, by its head when named .sql
    resp = await client.post(UPLOAD.format(ws="acme") + "?name=prod.dump&size=10&offset=0", content=b"PGDMP12345",
                             headers=POST)
    assert resp.status_code == 400 and "custom or tar format" in resp.json()["error"]
    resp = await upload(client, "acme", "prod.sql", b"PGDMP" + b"x" * 100, parts=1)
    assert "custom format" in resp.json()["error"]
    # a zip that is no Odoo backup, and a database of a version the templates do not have
    with zipfile.ZipFile(tmp_path / "other.zip", "w") as zf:
        zf.writestr("readme.txt", "hi")
    resp = await upload(client, "acme", "other.zip", (tmp_path / "other.zip").read_bytes())
    assert resp.status_code == 400 and "no dump.sql" in resp.json()["error"]
    old = dump_sql({"base": "installed"}, "15.0.1.3").encode()
    resp = await upload(client, "acme", "v15.sql", old)
    assert resp.status_code == 400 and "no template of Odoo 15" in resp.json()["error"]
    # in Italian, with the reason
    resp = await upload(client, "acme", "v15.sql", old, headers={**POST, "Accept-Language": "it"})
    assert "non ha un modello di Odoo 15" in resp.json()["error"]
    assert not folder.exists() or not [p for p in folder.iterdir()]
    # too large, and a workspace that is not here
    resp = await client.post(UPLOAD.format(ws="acme") + f"?name=a.zip&size={(50 << 30) + 1}&offset=0", content=b"PK",
                             headers=POST)
    assert resp.status_code == 400 and "at most 50 GiB" in resp.json()["error"]
    assert (await upload(client, "nope", "a.zip", b"PK\x03\x04")).status_code == 404


async def test_kept_seven_days(people, client, env, tmp_path):
    """An uploaded file is deleted after 7 days, an upload not finished after 2, each in the access log."""
    folder = env["settings"].backup_dir / ".imports" / "acme"
    assert (await upload(client, "acme", "a.zip", backup_zip(tmp_path / "a.zip").read_bytes())).status_code == 200
    assert (await upload(client, "acme", "b.zip", backup_zip(tmp_path / "b.zip").read_bytes())).status_code == 200
    first = await client.post(UPLOAD.format(ws="acme") + "?name=c.zip&size=1000&offset=0",
                              content=b"PK\x03\x04" * 10, headers=POST)
    assert first.json() == {"received": 40}
    part = next(p for p in folder.iterdir() if p.name.endswith(".part"))
    week, days = time.time() - 8 * 86400, time.time() - 3 * 86400
    os.utime(folder / "a.zip", (week, week))
    os.utime(part, (days, days))
    assert sorted(client.app.state.imports_swept()) == sorted(["acme/a.zip", f"acme/{part.name}"])
    assert sorted(p.name for p in folder.iterdir()) == [".b.zip.facts", "b.zip"]
    rows = [r["file"] for r in (await client.get("/api/backups", headers=ADMIN)).json()["rows"] if r.get("imported")]
    assert rows == ["b.zip"]
    details = [e["detail"] for e in client.app.state.access_log.recent()]
    assert any("a.zip of workspace acme: deleted after 7 days" in d for d in details)


async def test_a_backup_of_an_instance_and_a_workspace_users_limits(people, client, env, restoring, tmp_path):
    settings = env["settings"]
    assert (await upload(client, "acme", "acme.zip", backup_zip(tmp_path / "acme.zip").read_bytes())).status_code == 200
    made = await client.post(PLAN.format(ws="acme"), headers=POST, json={
        "server": "", "file": "acme.zip", "name": "main", "add_repositories": True, "allow_missing": True})
    assert (await wait_job(client, made.json()["job"]))["status"] == "success"
    # a backup of acme-main/production, and a full archive beside it
    folder = settings.backup_dir / "acme-main" / "production"
    folder.mkdir(parents=True)
    backup_zip(folder / "main_1.zip")
    (folder / f"{instancearchive.PREFIX}main_2.zip").write_bytes((folder / "main_1.zip").read_bytes())
    rows = {r["file"]: r for r in (await client.get("/api/backups", headers=basic("mario"))).json()["rows"]
            if r.get("server") == "acme-main"}
    assert rows["main_1.zip"]["can_instance"] and rows["main_1.zip"]["workspace"] == "acme"
    archive = rows[f"{instancearchive.PREFIX}main_2.zip"]
    assert not archive["can_instance"] and "From a full archive" in archive["instance_refused"]
    query = "?server=acme-main&role=production&file=main_1.zip"
    # mario, admin of acme: its workspace only, the backups of its own instances
    plan = await client.get(PLAN.format(ws="acme") + query + "&name=copy", headers=basic("mario"))
    assert plan.status_code == 200, plan.text
    assert [w["name"] for w in plan.json()["workspaces"]] == ["acme"] and plan.json()["instance"] == "copy"
    assert (await client.get(PLAN.format(ws="beta") + query, headers=basic("mario"))).status_code == 403
    # luca, a user of acme: refused; eva, admin of beta: another workspace's backup is not hers
    assert (await client.get(PLAN.format(ws="acme") + query, headers=basic("luca"))).status_code == 403
    assert (await client.get(PLAN.format(ws="beta") + query, headers=basic("eva"))).status_code == 404
    assert (await client.post(PLAN.format(ws="beta"), headers=post_as("eva"), json={
        "server": "acme-main", "role": "production", "file": "main_1.zip"})).status_code == 404
    assert (await client.get("/api/imports/places", headers=basic("luca"))).status_code == 403
    assert [w["name"] for w in (await client.get("/api/imports/places", headers=basic("eva"))).json()["workspaces"]] \
        == ["beta"]
    # eva uploads to beta, not to acme; mario does not see beta's files
    assert (await upload(client, "acme", "e.zip", b"PK\x03\x04", headers=post_as("eva"))).status_code == 403
    # a machine's admin: into another workspace of the machine
    plan = (await client.get(PLAN.format(ws="beta") + query, headers=ADMIN)).json()
    assert plan["new_instance"]["server"] == "beta-acme-prod"
    # the full archive: refused with where it is made from
    resp = await client.get(PLAN.format(ws="acme") + f"?server=acme-main&file={instancearchive.PREFIX}main_2.zip",
                            headers=basic("mario"))
    assert resp.status_code == 400 and "From a full archive" in resp.json()["error"]
    # mario makes it: the job, his
    made = await client.post(PLAN.format(ws="acme"), headers=post_as("mario"), json={
        "server": "acme-main", "role": "production", "file": "main_1.zip", "name": "copy", "allow_missing": True})
    assert made.status_code == 200, made.text
    job = await wait_job(client, made.json()["job"])
    assert job["status"] == "success" and job["source"] == "manual: mario" and made.json()["server"] == "acme-copy"
    assert restoring["restore"][-1][0] == "acme-copy/production" and restoring["restore"][-1][2] == {"neutralize": True}


async def test_a_links_row_and_its_upload(restoring, here, env, tmp_path):
    """Through a link the row's machine does it: its rows say can_instance, its plan and its upload are asked
    through /links/<name>/ (a part at a time: a link's page passes a body on whole)."""
    env["settings"].workspaces["acme"].members.clear()
    post = {"Authorization": AUTH, "Origin": "http://testserver"}
    folder = env["settings"].backup_dir / ".imports" / "acme"
    body = backup_zip(tmp_path / "acme.zip").read_bytes()
    base = "/links/cliente"
    places = await here.get(f"{base}/api/imports/places", headers=ADMIN)
    assert places.status_code == 200, places.text
    assert [w["name"] for w in places.json()["workspaces"]] == ["acme"]
    resp = await upload(here, "acme", "acme.zip", body, headers=post, base=base)
    assert resp.status_code == 200, resp.text
    assert (folder / "acme.zip").read_bytes() == body
    rows = (await here.get("/api/backups?link=cliente", headers=ADMIN)).json()["rows"]
    row = next(r for r in rows if r.get("imported"))
    assert row["can_instance"] and row["workspace"] == "acme"
    plan = await here.get(f"{base}{PLAN.format(ws='acme')}?file=acme.zip", headers=ADMIN)
    assert plan.status_code == 200, plan.text
    assert plan.json()["odoo"] == "20.0" and plan.json()["instance"] == "acme-prod"
    made = await here.post(f"{base}{PLAN.format(ws='acme')}", headers=post,
                           json={"file": "acme.zip", "name": "linked", "allow_missing": True, "add_repositories": True})
    assert made.status_code == 200, made.text
    assert made.json()["server"] == "acme-linked"


def test_the_italian_of_the_page_and_the_routes():
    from omniacli import i18n
    assert json.loads((i18n.CATALOGS / "it" / "lists.json").read_text(encoding="utf-8"))["Upload a backup…"] \
        == "Carica un backup…"
