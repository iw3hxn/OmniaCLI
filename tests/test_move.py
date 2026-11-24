# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The move of an instance between the provider's machines (AD-015, step 4
of §9 of docs/adr/AD-008-consegna-e-spostamento.md; UX-014 part B): its
records (placement.py), the forward of its names, the copy frozen here, the
crons of the instance that arrives, the dialog and its routes."""

import asyncio
import datetime
import os
import re
from pathlib import Path

import httpx
import pytest

from omniacli import composehelper, placement, pluginstore, templatestore, treesync, workspacehelper
from omniacli.config import LinkTarget
from omniacli.jobs import FAILED, SUCCESS, JobManager

from .test_app import AUTH, POST  # noqa: F401
from .test_staging_dialog import FAR_TOKEN, acme, two  # noqa: F401 - the fixtures
from .test_workspaces import post_as, workspaces  # noqa: F401 - the workspaces fixture

ROOT = Path(__file__).parent.parent


# --- the records ----------------------------------------------------------------------------------------------

def test_a_move_is_recorded_privately_with_its_line_of_placements(tmp_path):
    moves = placement.Moves(tmp_path)
    moves.save("acme-erp", {"link": "lab4", "state": "preparing", "since": "2026-11-23T20:40:02", "workspace": "acme",
                            "instance": "erp", "names": ["erp.acme.it"]})
    moves.update("acme-erp", state="ready", lag=0)
    state = moves.state("acme-erp")
    assert (state["state"], state["lag"], state["link"]) == ("ready", 0, "lab4")
    placed = moves.placements()["acme-erp"]
    assert placed["state"] == "ready" and placed["revision"] == 2 and placed["names"] == ["erp.acme.it"]
    folder = tmp_path / placement.MOVE_FOLDER / "acme-erp"
    assert oct(folder.stat().st_mode & 0o777) == "0o700" and oct(folder.parent.stat().st_mode & 0o777) == "0o700"
    assert oct((folder / placement.STATE_FILE).stat().st_mode & 0o777) == "0o600"
    assert oct((tmp_path / placement.PLACEMENTS).stat().st_mode & 0o777) == "0o600"
    moves.forget("acme-erp")
    assert moves.state("acme-erp") == {} and moves.placements() == {}
    assert not folder.exists() and not (tmp_path / placement.PLACEMENTS).exists()


def test_the_copy_of_a_placed_instance_is_frozen_until_it_is_deleted(tmp_path):
    moves = placement.Moves(tmp_path)
    for state in ("preparing", "ready", "failed"):
        moves.save("acme-erp", {"link": "lab4", "state": state})
        assert moves.frozen("acme-erp") == ""
    for state in ("completing", "placed"):
        moves.save("acme-erp", {"link": "lab4", "state": state})
        assert moves.frozen("acme-erp") == "acme-erp was moved to lab4: its copy here is stopped, and nothing runs on it"
    moves.update("acme-erp", copy="deleted")
    assert moves.frozen("acme-erp") == "" and moves.frozen("other") == ""


def test_the_workspace_forwards_its_placed_names_fully_and_else_the_challenges(tmp_path):
    moves = placement.Moves(tmp_path)
    names = {"erp": ["acme.it"], "shop": ["shop.acme.it"], "crm": ["crm.acme.it"]}
    assert placement.forward_wanted(moves, "acme", names.get) == ("off", "", [])
    moves.save("acme-crm", {"link": "lab4", "state": "ready", "workspace": "acme", "instance": "crm",
                            "address": "172.30.0.14", "acme": True})
    assert placement.forward_wanted(moves, "acme", names.get) == ("acme", "172.30.0.14", ["crm.acme.it"])
    # a Prepare that has not passed the challenges yet: nothing of it
    moves.save("acme-shop", {"link": "lab4", "state": "preparing", "workspace": "acme", "instance": "shop"})
    assert placement.forward_wanted(moves, "acme", names.get)[2] == ["crm.acme.it"]
    # one placed: its names fully, the ones prepared wait (one mode for the workspace's record)
    moves.save("acme-erp", {"link": "lab4", "state": "placed", "workspace": "acme", "instance": "erp",
                            "address": "172.30.0.14"})
    assert placement.forward_wanted(moves, "acme", names.get) == ("full", "172.30.0.14", ["acme.it"])
    moves.save("acme-crm", {**moves.state("acme-crm"), "state": "completing"})
    assert placement.forward_wanted(moves, "acme", names.get) == ("full", "172.30.0.14", ["acme.it", "crm.acme.it"])
    # a copy deleted is no longer forwarded; another workspace's move is not this one's
    moves.update("acme-erp", copy="deleted")
    moves.save("beta-x", {"link": "lab4", "state": "placed", "workspace": "beta", "instance": "x", "address": "x"})
    assert placement.forward_wanted(moves, "acme", names.get) == ("full", "172.30.0.14", ["crm.acme.it"])


def test_the_hosted_workspaces_name_their_home(tmp_path):
    hosted = placement.Hosted(tmp_path)
    assert hosted.get("acme") == {} and hosted.home_of("acme") == ""
    hosted.put("acme", {"home": "lab3", "instances": {"erp": {"state": "arriving"}}})
    hosted.put("acme", {**hosted.get("acme"), "made": True})
    assert hosted.home_of("acme") == "lab3" and hosted.get("acme")["revision"] == 2
    assert oct((tmp_path / placement.HOSTED).stat().st_mode & 0o777) == "0o600"
    hosted.put("acme", None)
    assert not (tmp_path / placement.HOSTED).exists()


def test_the_numbers_of_the_plan():
    assert placement.percentile([10, 20, 30, 40, 50, 60, 70, 80, 90, 100]) == 100
    assert placement.percentile(list(range(1, 101)), 95) == 95 and placement.percentile([None]) is None
    points = [{"t": 0, "cpu": 10.0, "ram": 1000, "ram_total": 4000}, {"t": 86400, "cpu": 90.0, "ram": 3000, "ram_total": 4000}]
    assert placement.load_of(points) == {"cpu_p95": 90.0, "ram_p95": 75.0, "days": 1.0}
    assert placement.seconds_at(60 << 20, 60.0) == 1 and placement.seconds_at(0, None) == 0
    sizes = {"database": 1 << 30, "filestore": 1 << 30, "addons": 0, "backups": 0}
    plan = placement.plan(sizes, "dedicated", None)
    assert plan["copied"] == 2 << 30 and plan["rate_mbs"] == placement.RATE_GUESS and not plan["measured_rate"]
    assert plan["stop_seconds"] == 40
    assert placement.plan(sizes, "cluster", 50.0)["stop_seconds"] > 40
    until = datetime.datetime.fromisoformat(placement.copy_until(datetime.datetime(2026, 11, 23, 20, 42)))
    assert until == datetime.datetime(2026, 11, 30, 20, 42)


def test_vector_made_by_the_template_does_not_hold_an_instance_on_its_cluster():
    """odoo-19-machine makes vector in every database of a cluster with pgvector, so that Odoo Enterprise's ai module
    may be installed by its role (only the superuser makes the extension): the move refuses a cluster without
    pgvector only for a database whose tables hold vectors (pg-report's vector_used), and the restore there leaves
    the extension alone out (step 5, 24/11)."""
    assert "'vector_used', EXISTS (SELECT 1 FROM pg_attribute" in composehelper.PG_REPORT
    assert workspacehelper._extensions_here(["pg_trgm", "vector"], vector=False) == (["pg_trgm"], ["vector"])
    assert workspacehelper._extensions_here(["pg_trgm", "vector"], vector=True) == (["pg_trgm", "vector"], [])
    assert workspacehelper._extensions_here(["pg_trgm"], vector=False) == (["pg_trgm"], [])


def test_the_refusals_of_a_move_are_in_the_readers_language():
    """«lab4: the database uses the extension vector…» stayed in English in the job (step 5, 24/11)."""
    from omniacli import i18n
    why = ("the database uses the extension vector of PostgreSQL (pgvector), which the machine's cluster there does "
           "not have")
    said = i18n.tr("it", f"lab4 cannot hold it: {why}")
    assert said == "lab4 non può ospitarla: il database usa l'estensione vector di PostgreSQL (pgvector), che il cluster " \
                   "della macchina là non ha"
    assert i18n.tr("it", "FAILED: lab4 cannot hold it: it has no template odoo-19").endswith(
        "lab4 non può ospitarla: non ha il template odoo-19")
    assert i18n.tr("it", "lab4 did not answer JSON") == "lab4 non ha risposto in JSON"


# --- what runs: the frozen copy, the crons, the trees ----------------------------------------------------------

async def test_no_job_runs_on_a_frozen_copy_but_the_moves_own(tmp_path):
    jobs = JobManager(tmp_path)
    jobs.frozen = lambda key: "acme-erp was moved to lab4" if key.startswith("acme-erp/") else ""
    ran = []

    async def action(job):
        ran.append(job.title)
    refused = jobs.submit("Backup acme-erp/production", ["acme-erp/production"], "manual: admin", action)
    allowed = jobs.submit("Delete the copy", ["acme-erp/production"], "manual: admin", action, frozen_ok=True)
    other = jobs.submit("Backup beta/production", ["beta/production"], "manual: admin", action)
    for _ in range(50):
        if all(j.finished_flag for j in (refused, allowed, other)):
            break
        await asyncio.sleep(0.02)
    assert refused.status == FAILED and refused.error == "acme-erp was moved to lab4"
    assert allowed.status == SUCCESS and other.status == SUCCESS
    assert ran == ["Delete the copy", "Backup beta/production"]


def test_the_crons_of_an_instance_that_arrives(tmp_path, monkeypatch):
    """Switched off before its Odoo starts there (their ids said), on again by id: in its database service, or on
    the machine's cluster from a container of its own of its Odoo service (that Odoo is not started yet)."""
    monkeypatch.setattr(composehelper, "db_target", lambda project, name: ("db", "", ("-T", "-u", "postgres")))
    [off] = composehelper.commands(tmp_path, "crons", ["db", "erp", "off"])
    assert off == ["exec", "-T", "-u", "postgres", "db", "sh", "-c", composehelper.SCRIPTS["pg-modules"], "sh", "erp",
                   composehelper.CRONS_OFF]
    assert "UPDATE ir_cron SET active = false WHERE active RETURNING id" in composehelper.CRONS_OFF
    [on] = composehelper.commands(tmp_path, "crons", ["db", "erp", "on", "1,2,30"])
    assert on[-1] == composehelper.CRONS_ON.format(ids="1,2,30") and "WHERE id IN (1,2,30)" in on[-1]
    monkeypatch.setattr(composehelper, "db_target", lambda project, name: ("odoo", "machine-", ("-T",)))
    [cluster] = composehelper.commands(tmp_path, "crons", ["machine-db", "acme_shop", "off"])
    assert cluster[:9] == ["run", "--rm", "--no-deps", "-T", "--entrypoint", "sh", "odoo", "-c",
                           composehelper.SCRIPTS["machine-pg-modules"]]
    assert "crons" in composehelper.WITH_ARGUMENTS
    # only ids reach the SQL
    for bad in (["db", "erp", "on", "1;DROP"], ["db", "erp", "on"], ["db", "erp", "off", "1"], ["db", "erp", "up"]):
        with pytest.raises(composehelper.HelperError):
            composehelper.commands(tmp_path, "crons", bad)


def test_the_addons_of_an_instance_are_a_tree_root_copies():
    assert treesync.tree_kind("addons") == "root" and treesync.TREES["filestore"] == "root"
    tree = treesync.LocalTree(lambda *a: list(a), "acme", "main", "erp", "production", "addons")
    assert tree.args == ("acme", "main", "erp", "production+addons")
    assert treesync.LocalTree(lambda *a: a, "acme", "main", "erp", "production").args[3] == "production"
    assert workspacehelper.ACTIONS["owncert-read"] == (workspacehelper.owncert_read, 2)
    with pytest.raises(workspacehelper.HelperError):
        workspacehelper._copy_tree("acme", "main", "erp", "production+other")


def test_the_purge_takes_the_moves_away():
    purge = (ROOT / "packaging" / "debian" / "files" / "postrm").read_text().split("    purge)", 1)[1]
    assert "rm -rf /var/lib/omniacli/move" in purge
    for name in ("placements.json", "hosted-workspaces.json", "placed-jobs.json"):
        assert f"/var/lib/omniacli/{name} /var/lib/omniacli/{name}.tmp" in purge or (
            f"/var/lib/omniacli/{name}" in purge and f"/var/lib/omniacli/{name}.tmp" in purge)


# --- the page and the routes --------------------------------------------------------------------------------------

async def test_the_item_is_the_machines_admins_only(two, acme):
    page = (await two.get("/servers/acme-odoo", headers={"Authorization": AUTH})).text
    assert page.count('id="move-open"') == 1
    item = re.search(r'<button type="button" id="move-open"[^>]*>', page).group(0)
    assert re.search(r'title="[^"]{20,}"', item)
    dialog = page[page.index('<dialog id="move-dialog"'):]
    dialog = dialog[:dialog.index("</dialog>")]
    assert len(re.findall(r'class="primary', dialog)) == 1 and 'class="icon-btn dialog-x"' in dialog
    for tag in re.findall(r"<button\b[^>]*>", dialog):
        assert re.search(r'title="[^"]+"', tag), tag
    # the workspace's admin and user: no item, no dialog, no route
    for user in ("mario", "luca"):
        assert 'id="move-open"' not in (await two.get("/servers/acme-odoo", headers=post_as(user))).text
        assert (await two.get("/api/servers/acme-odoo/move", headers=post_as(user))).status_code == 403
        assert (await two.post("/api/servers/acme-odoo/move", headers=post_as(user), json={"link": "far"})
                ).status_code == 403
    # the routes of the host answer another machine only
    assert (await two.get("/api/placements/host", headers={"Authorization": AUTH})).status_code == 403
    assert (await two.get("/api/placements/host", headers=post_as("mario"))).status_code == 403


async def test_the_dialog_reads_the_machines_and_says_why_not_before(two, acme, env):
    # far over https (a link through SSH would do as well): over plain HTTP the customer's data would pass in clear
    env["settings"].link_targets["far"] = LinkTarget("far", "https://far.test", FAR_TOKEN)
    plan = (await two.get("/api/servers/acme-odoo/move", headers={"Authorization": AUTH})).json()
    assert plan["kind"] == "dedicated" and plan["template"] == "odoo-19" and plan["workspace"] == "acme"
    rows = {r["link"]: r for r in plan["targets"]}
    assert set(rows) == {"far", "old", "down"}
    assert rows["down"]["why"] == "down does not answer"
    assert rows["old"]["why"] == "the link is in HTTP: the customer's data do not pass in clear"
    # far is another OmniaCLI of today: what it says of itself, the workspace new there
    far = rows["far"]
    assert far["why"] == "" and far["workspace"] == "new" and far["arch"] == os.uname().machine and "load" in far
    # a workspace with people moves (step 5, AD-014): what is refused is said before anything is made, in the
    # reader's language (here its test environment)
    assert not any("members" in w for w in plan["refusals"])
    assert any("has a test environment" in w for w in plan["refusals"])
    resp = await two.post("/api/servers/acme-odoo/move", headers=POST, json={"link": "far"})
    assert resp.status_code == 400 and resp.json()["error"] in [w for w in plan["refusals"]]
    italian = (await two.get("/api/servers/acme-odoo/move", headers={"Authorization": AUTH, "Accept-Language": "it"})).json()
    assert any("ha un ambiente di test" in w for w in italian["refusals"])
    assert {r["link"]: r["why"] for r in italian["targets"]}["down"] == "down non risponde"
    # Complete, Cancel and Delete the copy without a move: refused, nothing done
    assert (await two.post("/api/servers/acme-odoo/move/complete", headers=POST,
                           json={"confirm": "acme-odoo"})).status_code == 400
    assert (await two.post("/api/servers/acme-odoo/move/cancel", headers=POST)).status_code == 400
    assert (await two.post("/api/servers/acme-odoo/move/delete-copy", headers=POST)).status_code == 400


async def test_a_hosted_workspace_answers_its_home_only(two, acme):
    """The host's routes of a workspace hosted for another machine refuse every other link (AD-014 §3)."""
    far = placement.Hosted(two.far_settings.jobs_dir.parent)
    far.put("acme", {"home": "someone-else", "instances": {"odoo": {"state": "placed"}}})
    headers = {"Authorization": f"Bearer {FAR_TOKEN}", "Accept": "application/json"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=two.far), base_url="http://far.test") as c:
        resp = await c.post("/api/placements/acme/odoo/start", headers=headers, json={})
        assert resp.status_code == 403 and "hosted here for another machine" in resp.json()["error"]
        resp = await c.post("/api/placements/beta/odoo/start", headers=headers, json={})
        assert resp.status_code == 404
        facts = (await c.get("/api/placements/host", headers=headers, params={"workspace": "acme"})).json()
        assert facts["workspace"]["why"] == "workspace acme is hosted here for another machine"


async def test_a_host_without_the_template_installs_it_at_prepare(two, acme, monkeypatch):
    """UX-014 B4: a machine that has not the instance's template, but installs it from where it installs templates,
    is not refused: it says so, and Prepare installs it there first."""
    published = [{"name": "odoo-17", "version": "17.0.3"}]

    async def available(source):
        return published
    monkeypatch.setattr(templatestore, "available", available)
    headers = {"Authorization": f"Bearer {FAR_TOKEN}", "Accept": "application/json"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=two.far), base_url="http://far.test") as c:
        facts = (await c.get("/api/placements/host", headers=headers, params={"template": "odoo-17"})).json()
        assert facts["template"] is False and facts["template_store"] is True
        # one it has: nothing to install
        facts = (await c.get("/api/placements/host", headers=headers, params={"template": "odoo-19"})).json()
        assert facts["template"] is True and facts["template_store"] is False

        async def down(source):
            raise pluginstore.StoreError("the index does not answer")
        monkeypatch.setattr(templatestore, "available", down)
        facts = (await c.get("/api/placements/host", headers=headers, params={"template": "odoo-17"})).json()
        assert facts["template"] is False and facts["template_store"] is False
        # an arrival of a template it has not and cannot install: refused before anything is made
        resp = await c.post("/api/placements/acme/odoo/arrive", headers=headers, json={"template": "odoo-17"})
        assert resp.status_code == 400 and "No template 'odoo-17' here" in resp.json()["error"]
