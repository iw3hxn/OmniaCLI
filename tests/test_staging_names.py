# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A hosting panel's stagings, told by their names when a server that
already runs Odoo is added (stagings.parent_by_name, /api/stagings/mark)."""

from omniacli import stagings

from .test_app import AUTH, HOST, client  # noqa: F401 - the fixture


def test_the_staging_of_a_name():
    names = ["volontarius", "volontarius-stage", "river", "ridix.example.com", "20260805-staging-ridix.example.com",
             "shop-staging", "shop", "alone-stage"]
    assert stagings.parent_by_name("volontarius-stage", names) == ("volontarius", "")
    assert stagings.parent_by_name("shop-staging", names) == ("shop", "")
    assert stagings.parent_by_name("20260805-staging-ridix.example.com", names) == ("ridix.example.com", "2026-08-05")
    assert stagings.parent_by_name("202606-staging-ridix.example.com", names) == ("ridix.example.com", "2026-06-01")
    # no production of that name: a production of its own
    assert stagings.parent_by_name("alone-stage", names) is None
    assert stagings.parent_by_name("volontarius", names) is None
    assert stagings.parent_by_name("river", names) is None


async def test_mark_a_staging(client, env):  # noqa: F811
    headers = {"Authorization": AUTH, "Origin": f"http://{HOST}"}
    resp = await client.post("/api/stagings/mark", json={"name": "srv", "parent": "nope"}, headers=headers)
    assert resp.status_code == 400 and "two servers" in resp.json()["error"]
    resp = await client.post("/api/stagings/mark", json={"name": "srv", "parent": "srv"}, headers=headers)
    assert resp.status_code == 400
    # two servers: the staging is recorded under its parent, with its date
    import dataclasses

    settings = env["settings"]
    settings.servers["srv-stage"] = dataclasses.replace(settings.servers["srv"], name="srv-stage")
    resp = await client.post("/api/stagings/mark", json={"name": "srv-stage", "parent": "srv", "created": "2026-08-05"},
                             headers=headers)
    assert resp.json() == {"ok": True}
    rows = {r["name"]: r for r in (await client.get("/api/servers", headers=headers)).json()}
    assert rows["srv-stage"]["parent"] == "srv" and rows["srv-stage"]["created"] == "2026-08-05"
    assert (await client.post("/api/stagings/mark", json={"name": "srv-stage", "parent": "srv", "created": "ieri"},
                              headers=headers)).status_code == 400
