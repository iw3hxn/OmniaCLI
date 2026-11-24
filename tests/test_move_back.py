# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The return of an instance moved to another machine (AD-015 «Torna su», step 6 of §9 of
docs/adr/AD-008-consegna-e-spostamento.md; UX-014 B6): its records, the banner's actions for each state, the page
of an instance whose copy here was deleted, and the routes of both machines with their guards."""

import re

import httpx

from omniacli import placement
from omniacli.config import Workspace, parse_servers, server_to_dict

from .test_app import AUTH, POST  # noqa: F401
from .test_staging_dialog import FAR_TOKEN, acme, two  # noqa: F401 - the fixtures
from .test_workspaces import post_as, workspaces  # noqa: F401 - the workspaces fixture

FAR = {"Authorization": f"Bearer {FAR_TOKEN}", "Accept": "application/json"}


def place(settings, **values):
    """acme-odoo placed on far (the move's state here)."""
    moves = placement.Moves(settings.jobs_dir.parent)
    moves.save("acme-odoo", {"link": "far", "state": "placed", "workspace": "acme", "instance": "odoo",
                             "address": "203.0.113.14", "home_address": "203.0.113.13", "kind": "cluster",
                             "placed_at": "2026-11-24T10:00:00", "copy_until": "2026-12-01T10:00:00", **values})
    return moves


def host_it(two, settings, state="placed"):
    """acme-odoo on far too, its workspace hosted there for this machine (its link there: inaria)."""
    far = two.far_settings
    far.servers = {"acme-odoo": parse_servers({"acme-odoo": server_to_dict(settings.servers["acme-odoo"])},
                                              far.logs_dir)["acme-odoo"]}
    far.workspaces = {"acme": Workspace("acme", "ACME", servers=["acme-odoo"], members={})}
    placement.Hosted(far.jobs_dir.parent).put("acme", {"home": "inaria", "provider_label": "", "members": [],
                                                      "instances": {"odoo": {"state": state}}})


def test_a_returned_instance_serves_here_and_is_not_frozen(tmp_path):
    moves = placement.Moves(tmp_path)
    moves.save("acme-erp", {"link": "lab4", "state": "placed", "workspace": "acme", "instance": "erp",
                            "address": "172.30.0.14"})
    # a return under way: the instance is still placed there, its copy here frozen
    moves.update("acme-erp", back={"state": "ready", "mode": "copy"})
    assert moves.host_of("acme-erp") == "lab4" and moves.frozen("acme-erp")
    # the copy deleted, made here again for the return: frozen too, and its page here (copy_here)
    moves.update("acme-erp", copy="back")
    assert moves.copy_here("acme-erp") and moves.frozen("acme-erp")
    moves.update("acme-erp", state="returned", copy=None, back={"state": "done"})
    assert moves.host_of("acme-erp") == "" and moves.frozen("acme-erp") == ""
    names = {"erp": ["acme.it"]}
    assert placement.forward_wanted(moves, "acme", names.get) == ("off", "", [])
    assert set(placement.HOSTED_STOPPED) == {"returning", "returned"} and "returned" in placement.STATES


async def test_back_to_here_is_the_machines_admins_only_and_asks_the_name(two, acme, env):
    # no move: nothing to bring back
    resp = await two.post("/api/servers/acme-odoo/move/back", headers=POST, json={"confirm": "acme-odoo"})
    assert resp.status_code == 404
    place(env["settings"])
    for user in ("mario", "luca"):
        for path in ("back", "back/complete", "back/cancel", "back/delete-copy"):
            assert (await two.post(f"/api/servers/acme-odoo/move/{path}", headers=post_as(user),
                                   json={"confirm": "acme-odoo"})).status_code == 403
    resp = await two.post("/api/servers/acme-odoo/move/back", headers=POST, json={"confirm": "acme"})
    assert resp.status_code == 400 and resp.json()["error"] == "Type acme-odoo to bring it back"
    # Complete, Cancel and Delete the copy there before their state: refused, nothing done
    resp = await two.post("/api/servers/acme-odoo/move/back/complete", headers=POST, json={"confirm": "acme-odoo"})
    assert resp.status_code == 400 and "is not ready" in resp.json()["error"]
    assert (await two.post("/api/servers/acme-odoo/move/back/cancel", headers=POST)).status_code == 400
    assert (await two.post("/api/servers/acme-odoo/move/back/delete-copy", headers=POST)).status_code == 400
    # the host does not answer for it: said before anything is made
    resp = await two.post("/api/servers/acme-odoo/move/back", headers=POST, json={"confirm": "acme-odoo"})
    assert resp.status_code == 400 and "far " in resp.json()["error"] and "both machines answering" in resp.json()["error"]
    italian = await two.post("/api/servers/acme-odoo/move/back", headers={**POST, "Accept-Language": "it"},
                             json={"confirm": "acme"})
    assert italian.json()["error"] == "Scrivi acme-odoo per riportarla qui"


async def test_the_banner_has_the_action_of_each_state_of_the_return(two, acme, env):
    moves = place(env["settings"])

    async def band():
        page = (await two.get("/servers/acme-odoo", headers={"Authorization": AUTH})).text
        found = page[page.index('<div class="move-band'):]
        return found[:found.index("</div>")]
    placed = await band()
    button = re.search(r'<button type="button" id="move-back"[^>]*>([^<]*)</button>', placed)
    assert button and "Back to " in button.group(1) and 'class="primary' not in button.group(0)
    assert re.search(r'title="Brings the instance back to [^"]{20,}"', button.group(0))
    assert 'id="move-delete-copy"' in placed
    moves.update("acme-odoo", back={"state": "ready", "mode": "copy", "kind": "dedicated", "lag": 0,
                                    "ready_at": "2026-11-24T11:00:00"})
    ready = await band()
    assert len(re.findall(r'class="primary', ready)) == 1 and 'id="move-back-complete"' in ready
    assert 'id="move-back-cancel"' in ready and 'id="move-back"' not in ready and 'id="move-delete-copy"' not in ready
    assert "Ready to come back from far since 24/11/2026 11:00" in ready
    moves.update("acme-odoo", back={"state": "failed", "error": "it did not start"})
    failed = await band()
    assert "did not end: it did not start" in failed and 'id="move-back-cancel"' in failed
    assert 'class="primary' not in failed
    moves.update("acme-odoo", state="returned", returned_at="2026-11-24T12:00:00", there_until="2026-12-01T12:00:00",
                 back={"state": "done"})
    returned = await band()
    assert "Back here from far since 24/11/2026 12:00" in returned and "01/12/2026 12:00" in returned
    delete = re.search(r'<button type="button" class="small danger" id="move-back-delete"[^>]*>([^<]*)</button>', returned)
    assert delete and delete.group(1) == "Delete the copy on far…" and 'title="Deletes at once its stopped copy' in delete.group(0)
    for tag in re.findall(r"<button\b[^>]*>", returned + ready + placed):
        assert re.search(r'title="[^"]+"', tag), tag
    # a new move waits for the copy there to be deleted, and says why
    page = (await two.get("/api/servers/acme-odoo/move", headers={"Authorization": AUTH})).json()
    assert page["move"]["state"] == "returned"


async def test_the_page_of_an_instance_whose_copy_was_deleted(two, acme, env):
    """Its copy here deleted: the machine's admins keep its page here (where it is, Open it there, Back to…);
    its workspace's people are sent to its page there."""
    place(env["settings"], copy="deleted")
    env["settings"].servers.pop("acme-odoo")
    resp = await two.get("/servers/acme-odoo", headers={"Authorization": AUTH})
    assert resp.status_code == 200 and 'id="move-back"' in resp.text and "its copy here was deleted" in resp.text
    assert 'href="/placed/acme-odoo/servers/acme-odoo"' in resp.text and 'id="move-delete-copy"' not in resp.text
    assert len(re.findall(r'class="primary', resp.text[resp.text.index('<div class="move-band'):])) == 0
    resp = await two.get("/servers/acme-odoo", headers=post_as("mario"), follow_redirects=False)
    assert resp.status_code == 307 and resp.headers["location"] == "/placed/acme-odoo/servers/acme-odoo"
    # any other path of it is the host's, for the admins too
    resp = await two.get("/servers/acme-odoo/logs", headers={"Authorization": AUTH}, follow_redirects=False)
    assert resp.status_code == 307


async def test_the_host_answers_the_return_to_the_home_only(two, acme, env):
    host_it(two, env["settings"])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=two.far), base_url="http://far.test") as c:
        facts = (await c.get("/api/placements/acme/odoo/back", headers=FAR)).json()
        assert facts["server"] == "acme-odoo" and facts["template"] == "odoo-19" and facts["kind"] in ("cluster", "dedicated")
        assert facts["state"] == "placed" and isinstance(facts["deploys"], dict)
        assert {r["name"] for r in facts["repositories"]} == {"addons", "odoo"}
        assert "token" not in str(facts) and "password" not in str(facts)
        # an instance that serves here is not deleted as a copy; an unknown step is refused
        resp = await c.post("/api/placements/acme/odoo/back/delete", headers=FAR, json={})
        assert resp.status_code == 400 and "only the copy of an instance that went back is deleted" in resp.json()["error"]
        assert (await c.post("/api/placements/acme/odoo/back/bogus", headers=FAR, json={})).status_code == 404
        # the machine's own admin, and any other workspace: refused
        assert (await c.get("/api/placements/acme/odoo/back", headers={"Authorization": AUTH})).status_code in (401, 403)
        assert (await c.get("/api/placements/beta/odoo/back", headers=FAR)).status_code == 404
        placement.Hosted(two.far_settings.jobs_dir.parent).put("acme", {"home": "someone-else", "instances": {}})
        resp = await c.post("/api/placements/acme/odoo/back/stop", headers=FAR, json={})
        assert resp.status_code == 403 and "hosted here for another machine" in resp.json()["error"]


async def test_the_copy_that_went_back_is_frozen_on_the_host(two, acme, env):
    """After its return the host keeps its copy stopped, passing its names to the home: no job but the return's own
    runs on it there."""
    host_it(two, env["settings"], state="returned")
    frozen = two.far.state.jobs.frozen
    assert "went back to the machine of its workspace" in frozen("acme-odoo/production")
    host_it(two, env["settings"], state="placed")
    assert frozen("acme-odoo/production") == ""
