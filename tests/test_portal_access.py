# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The customers' portal after the access tests of 21/11/2026
(prove-accessi-clienti): what a user of workspaces is no longer told (the
names of the other customers, the integrator's people and machines, the
paths of this machine), what it may do now (restore its backups, Log in
as, the sizes of the backup dialog), and the pages of a refusal. The
machine's admins keep reading everything as before."""
import dataclasses
import io
import types
import zipfile

import pytest

from omniacli import ops
from omniacli.customerview import CustomerView

from .test_app import AUTH, POST, client, wait_job  # noqa: F401 - the client fixture
from .test_workspaces import basic, post_as, workspaces  # noqa: F401 - the workspaces fixture

ADMIN = {"Authorization": AUTH}
TAKEN = "This name is not available: choose another one"


# --- FUGA 2: the names of the other customers ------------------------------

async def test_a_workspace_admin_is_not_told_whose_name_it_is(workspaces, client, env):
    url = "/api/workspaces/acme/members/save"
    for name in ("eva", "admin"):  # beta's user; the machine's, of the main configuration
        resp = await client.post(url, headers=post_as("mario"),
                                 json={"name": name, "role": "user", "password": "long-enough", "new": True})
        assert (resp.status_code, resp.json()["error"]) == (400, TAKEN), name
    # in Italian, a whole sentence
    resp = await client.post(url, headers={**post_as("mario"), "Accept-Language": "it"},
                             json={"name": "eva", "role": "user", "password": "long-enough", "new": True})
    assert resp.json()["error"] == "Questo nome non è disponibile: scegline un altro"
    # the machine's admin is told which and where, in full sentences
    resp = await client.post(url, headers=POST, json={"name": "eva", "role": "user", "password": "long-enough", "new": True})
    assert resp.json()["error"] == "A user named eva already exists"
    resp = await client.post(url, headers={**POST, "Accept-Language": "it"},
                             json={"name": "eva", "role": "user", "password": "long-enough", "new": True})
    assert resp.json()["error"] == "Esiste già un utente di nome eva"
    resp = await client.post(url, headers=POST, json={"name": "admin", "role": "user", "password": "long-enough", "new": True})
    assert "is set in the main configuration" in resp.json()["error"]


async def test_a_workspace_admin_is_not_told_whose_token_it_is(workspaces, client, env):
    # beta's token, made by the machine's admin
    resp = await client.post("/api/workspaces/beta/tokens/save", headers=POST, json={"name": "assistant", "new": True})
    assert resp.status_code == 200, resp.text
    url = "/api/workspaces/acme/tokens/save"
    for name in ("assistant", "tester"):  # beta's; the machine's
        resp = await client.post(url, headers=post_as("mario"), json={"name": name, "new": True})
        assert (resp.status_code, resp.json()["error"]) == (400, TAKEN), name
    # changed or revoked: another workspace's token is answered as one that does not exist
    answers = set()
    for name in ("assistant", "tester", "nonexistent"):
        for action in ("save", "delete"):
            resp = await client.post(f"/api/workspaces/acme/tokens/{action}", headers=post_as("mario"),
                                     json={"name": name, "role": "user"})
            assert resp.status_code == 404, (name, action)
            answers.add(resp.json()["error"].replace(name, "NAME"))
    assert answers == {"No token NAME of workspace acme"}
    resp = await client.post("/api/workspaces/acme/tokens/delete", headers={**post_as("mario"), "Accept-Language": "it"},
                             json={"name": "nonexistent"})
    assert resp.json()["error"] == "Il workspace acme non ha un token di nome nonexistent"
    # the machine's admin is told
    resp = await client.post(url, headers=POST, json={"name": "assistant", "new": True})
    assert resp.json()["error"] == "A token named assistant already exists"


# --- FUGA 4: the integrator's names and the machine's paths in the jobs ---------

LINES = ["--- staging of srv on lab2 through the link lab2 (http://192.0.2.12:8090)",
         "ERROR: cannot mount /var/lib/omniacli-storage/default/.disks/acme.img",
         "--- stop asked by admin", "download /backups/srv/production/x.zip"]


@pytest.fixture
async def integrator_job(workspaces, client, env):
    """A job on acme's srv started by a link acting for the integrator's
    workstation, whose lines name another machine and a path of this one."""
    settings = env["settings"]
    settings.links["carlo-dell-16"] = settings.links["omnia"]
    settings.link_targets = {"lab2": types.SimpleNamespace(url="http://192.0.2.12:8090", ssh=None)}

    async def action(job):
        for line in LINES:
            job.log(line)
        job.result = {"where": "/var/lib/omniacli-storage/default/.disks/acme.img"}
        raise ops.JobFailed("cannot mount /var/lib/omniacli-storage/default/.disks/acme.img on lab2")
    return client.app.state.jobs.submit("Staging of srv on lab2", ["srv/production"],
                                        "manual: link carlo-dell-16 (admin)", action)


async def test_a_customer_reads_its_jobs_without_the_integrators_names(integrator_job, client, env):
    job = await wait_job(client, integrator_job.id)
    # the machine's admin: everything, as it was
    assert job["source"] == "manual: link carlo-dell-16 (admin)" and job["title"] == "Staging of srv on lab2"
    assert "/var/lib/omniacli-storage/default/.disks/acme.img" in job["error"]
    # the customer: who started it is the provider; no machine, link or path
    for user in ("mario", "luca"):
        seen = (await client.get(f"/api/jobs/{integrator_job.id}", headers=basic(user))).json()
        text = str(seen)
        for secret in ("carlo-dell", "lab2", "192.0.2.12", "/var/lib", "omniacli-storage", "stop asked by admin"):
            assert secret not in text, (user, secret)
        assert seen["source"] == "manual: the provider"
        assert seen["title"] == "Staging of srv on another machine"
        assert seen["error"] == "cannot mount …/acme.img on another machine"
        assert seen["lines"][2].endswith("--- stop asked by the provider")
        assert seen["lines"][3].endswith("download /backups/srv/production/x.zip")  # OmniaCLI's own route
        assert seen["result"] == {"where": "…/acme.img"}
        listed = (await client.get("/api/jobs", headers=basic(user))).json()["jobs"]
        assert [j["source"] for j in listed if j["id"] == integrator_job.id] == ["manual: the provider"]
        for page in ("/jobs", f"/jobs/{integrator_job.id}", "/", "/servers/srv"):
            body = (await client.get(page, headers=basic(user))).text
            assert "carlo-dell" not in body and "lab2" not in body and "omniacli-storage" not in body, (user, page)
    # in Italian, and with the name the machine's admin gave the provider
    seen = (await client.get(f"/api/jobs/{integrator_job.id}", headers={**basic("mario"), "Accept-Language": "it"})).json()
    assert seen["title"] == "Staging of srv on un'altra macchina" and seen["source"] == "manual: il fornitore"
    page = (await client.get("/jobs", headers={**basic("mario"), "Accept-Language": "it"})).text
    assert "Staging di srv su un&#39;altra macchina" in page and "manuale: il fornitore" in page
    resp = await client.post("/api/options/save", headers=POST, json={"portal": {"provider": "OmniaSolutions"}})
    assert resp.status_code == 200, resp.text
    seen = (await client.get(f"/api/jobs/{integrator_job.id}", headers=basic("mario"))).json()
    assert seen["source"] == "manual: OmniaSolutions" and seen["lines"][2].endswith("stop asked by OmniaSolutions")
    assert "OmniaSolutions" in (await client.get("/options", headers=ADMIN)).text
    # the job on disk keeps the truth
    assert client.app.state.jobs.get(integrator_job.id).source == "manual: link carlo-dell-16 (admin)"
    # searching finds what it reads, not what was taken out
    assert integrator_job.id not in (await client.get("/jobs?q=carlo-dell", headers=basic("mario"))).text
    assert integrator_job.id in (await client.get("/jobs?q=carlo-dell", headers=ADMIN)).text


def test_its_own_users_and_tokens_are_named():
    view = CustomerView("the provider", "another machine", links=frozenset({"omnia"}), machines=frozenset({"lab2"}),
                        members=frozenset({"mario"}), tokens=frozenset({"helper"}))
    assert view.source("manual: mario") == "manual: mario"
    assert view.source("mcp: token helper") == "mcp: token helper"
    assert view.source("mcp: token tester") == "mcp: the provider"
    assert view.source("manual: admin") == "manual: the provider"
    assert view.source("mcp: link omnia (carlo)") == "mcp: the provider"
    assert view.source("scheduled") == "scheduled"
    # a name inside another word is not one
    assert view.text("OmniaCLI on lab20 and lab2-x, lab2.") == "OmniaCLI on lab20 and lab2-x, another machine."
    assert view.text("https://acme.it/var/x and /opt/odoo/addons/sale") == "https://acme.it/var/x and …/sale"
    # the folder in the name systemd gives its mount (lab1, 21/11/2026)
    assert view.text(r"var-lib-omniacli\x2dstorage-default-acme.mount did not mount") == "….mount did not mount"


async def test_the_metrics_of_a_customer_name_no_folder(workspaces, client, env, monkeypatch):
    sampler = client.app.state.sampler
    sampler.source["srv/production"] = "the containers of /var/lib/omniacli-storage/default/acme"
    got = (await client.get("/api/metrics", headers=basic("mario"))).json()["instances"]["srv/production"]["source"]
    assert "omniacli-storage" not in got and got.endswith("…/acme")
    got = (await client.get("/api/metrics", headers=ADMIN)).json()["instances"]["srv/production"]["source"]
    assert "/var/lib/omniacli-storage/default/acme" in got


# --- the pages of a refusal ------------------------------------------------------

async def test_a_refused_page_is_a_page(workspaces, client):
    html = {"Accept": "text/html,application/xhtml+xml"}
    resp = await client.get("/workspaces/acme", headers={**basic("luca"), **html, "Accept-Language": "it"})
    assert resp.status_code == 403 and resp.headers["content-type"].startswith("text/html")
    assert "Non hai accesso a questa pagina" in resp.text and "Serve il ruolo admin" in resp.text
    assert 'href="/instances"' in resp.text and 'lang="it"' in resp.text
    resp = await client.get("/config", headers={**basic("mario"), **html})
    assert resp.status_code == 403 and "No access to this page" in resp.text and "/instances" in resp.text
    resp = await client.get("/jobs/nope", headers={**ADMIN, **html})
    assert resp.status_code == 404 and "Page not found" in resp.text and "No such job" in resp.text
    resp = await client.get("/no/such/page", headers={**ADMIN, **html, "Accept-Language": "it"})
    assert resp.status_code == 404 and "Pagina non trovata" in resp.text
    # an API's caller keeps its JSON
    resp = await client.get("/workspaces/acme", headers=basic("luca"))
    assert resp.status_code == 403 and resp.json() == {"detail": "The admin role is required"}
    resp = await client.get("/api/jobs/nope", headers={**ADMIN, "Accept": "application/json"})
    assert resp.status_code == 404 and resp.json()["detail"] == "No such job"


# --- what a workspace's users may do now -------------------------------------------

async def test_the_backup_dialog_reads_its_sizes(workspaces, client, env):
    settings = env["settings"]
    settings.workspaces["acme"].members["eva"] = "developer"
    for url in ("/api/servers/srv/production/backup/estimate", "/api/servers/srv/production/restore/room"):
        resp = await client.get(url, headers=basic("eva"))
        assert resp.status_code != 403 or "workspaces" not in resp.text, (url, resp.text)
        resp = await client.get(url, headers=basic("luca"))  # a user does not back up
        assert resp.status_code == 403 and resp.json()["detail"] == "The developer role is required", url


async def test_log_in_as_is_its_admins(workspaces, client, env):
    url = "/api/servers/srv/production/login-as/users"
    # mario, admin of acme: the route is open; this instance has no https name (409)
    resp = await client.get(url, headers=basic("mario"))
    assert resp.status_code == 409 and "Domain name over https" in resp.json()["detail"]
    # a user there, another workspace's: refused
    resp = await client.get(url, headers=basic("luca"))
    assert resp.status_code == 403 and resp.json()["detail"] == "The developer role is required"
    resp = await client.get(url, headers=basic("eva"))
    assert resp.status_code == 403 and resp.json()["detail"] == "Not a server of your workspaces"
    resp = await client.post("/api/servers/srv/production/login-as/nginx", headers=post_as("mario"))
    assert resp.status_code == 403
    # the button: its admin sees it, its user does not
    assert "data-login-as=" not in (await client.get("/servers/srv", headers=basic("luca"))).text


def a_zip():
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as zf:
        zf.writestr("dump.sql", "-- dump")
    return data.getvalue()


async def test_its_admin_restores_a_backup_after_one_made_now(workspaces, client, env, monkeypatch):
    settings = env["settings"]
    instance = settings.instance("srv", "production")
    folder = settings.backup_dir / "srv" / "production"
    folder.mkdir(parents=True)
    (folder / "old.zip").write_bytes(a_zip())
    done = []

    async def backup_into(job, inst, dest, options=None):
        done.append(("backup", inst.key))
        made = dest / "now.zip"
        made.write_bytes(a_zip())
        return made

    async def restore(self, job, inst, path, options):
        done.append(("restore", path.name))
    monkeypatch.setattr(ops, "backup_into", backup_into)
    monkeypatch.setattr(type(instance.plugin), "restore", restore)
    url = "/servers/srv/production/restore"
    # developer and user: refused
    settings.workspaces["acme"].members["eva"] = "developer"
    for user in ("eva", "luca"):
        resp = await client.post(url, headers=post_as(user), json={"backup": "old.zip", "confirm": "DB_production"})
        assert resp.status_code == 403 and resp.json()["detail"] == "The admin role is required", user
        page = (await client.get("/servers/srv", headers=basic(user))).text
        assert "data-restore-file=" not in page, user
    # its admin: the menu item, one of its backups only (no upload)
    page = (await client.get("/servers/srv", headers=basic("mario"))).text
    assert "data-restore-file=" in page and '"upload": null' in page and '"backup_first": true' in page
    # the database typed, or nothing is done
    resp = await client.post(url, headers=post_as("mario"), json={"backup": "old.zip"})
    assert resp.status_code == 400 and "Type DB_production" in resp.json()["error"] and not done
    resp = await client.post(url, headers=post_as("mario"), json={"backup": "old.zip", "confirm": "DB_production"})
    assert resp.status_code == 200, resp.text
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success", job
    assert done == [("backup", "srv/production"), ("restore", "old.zip")]
    assert any("backup before the restore" in line for line in job["lines"])
    assert job["result"] == {"backup_before": "now.zip"}
    # the upload stays the machine's admins'
    resp = await client.post("/api/servers/srv/production/backups/upload", params={"name": "x.zip"},
                             headers=post_as("mario"), content=a_zip())
    assert resp.status_code == 403
    # the machine's admin restores as before: no backup made first
    done.clear()
    resp = await client.post(url, headers=POST, json={"backup": "old.zip", "confirm": "DB_production"})
    await wait_job(client, resp.json()["job"])
    assert done == [("restore", "old.zip")]


# --- smaller ---------------------------------------------------------------------

async def test_an_instance_without_test_has_no_test_address(workspaces, client, env):
    settings = env["settings"]
    srv = settings.servers["srv"]
    # an instance made in the workspace: its production only, no test
    settings.servers["acme-solo"] = dataclasses.replace(srv, name="acme-solo",
                                                        instances={"production": srv.instances["production"]})
    settings.workspaces["acme"].servers = ["srv", "acme-solo"]
    page = (await client.get("/workspaces/acme", headers=basic("mario"))).text
    assert "solo.acme.example.com" in page and "solo.test.acme.example.com" not in page
