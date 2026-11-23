# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The bell in the header (bell.py, README The bell): the map's things to
look at new for the person, the latest with their page, Mark as read kept
per person in a file of mode 600, a user of workspaces' own only, and no
question to a machine while a page is drawn."""

import json
import re
import stat

from omniacli import bell

from .test_app import AUTH, client, wait_job  # noqa: F401 - the client fixture
from .test_inframap import HERE_NAME
from .test_workspaces import ORIGIN, basic, workspaces  # noqa: F401 - the workspaces fixture

ADMIN_POST = {"Authorization": AUTH, "Origin": ORIGIN}


def test_a_thing_is_the_same_thing_in_every_language():
    one = {"kind": "job_failed", "node": "~/srv", "text": "Restart srv", "since": "2026-11-23T10:00:00", "job": "j1"}
    assert bell.key_of(one) == bell.key_of({**one, "text": "Riavvia srv"}) == "job_failed|~/srv|job:j1"
    # a certificate checked again is the same thing; an instance stopped again later is another
    cert = {"kind": "certificate", "node": "~/srv", "since": "2026-11-23T10:00:00"}
    assert bell.key_of(cert) == bell.key_of({**cert, "since": "2026-11-23T11:00:00"})
    stopped = {"kind": "stopped", "node": "~/srv", "since": "2026-11-23T10:00:00"}
    assert bell.key_of(stopped) != bell.key_of({**stopped, "since": "2026-11-23T12:00:00"})
    # the names of a workspace: one thing for its instances
    names = {"kind": "names", "node": "~/a", "nodes": ["~/b", "~/a"], "since": None}
    assert bell.key_of(names) == "names|~/a|~/a,~/b"


def test_where_each_thing_is_fixed():
    assert bell.link_of({"kind": "job_failed", "node": "~/srv", "page": "/jobs/j1"}) == "/jobs/j1"
    assert bell.link_of({"kind": "disk", "node": "~", "page": "/options#monitoring"}) == "/options#monitoring"
    assert bell.link_of({"kind": "down", "node": "~/srv"}) == "/servers/srv"
    assert bell.link_of({"kind": "down", "node": "lab9/erp"}, {"lab9"}) == "/links/lab9/servers/erp"
    assert bell.link_of({"kind": "down", "node": "lab9/erp"}) is None
    assert bell.link_of({"kind": "machine_unread", "node": "lab9"}) == "/links"
    assert bell.link_of({"kind": "names", "node": "~/ws:acme"}) is None


def test_the_new_first_the_critical_first_the_newest_first():
    watch = [{"kind": "job_warned", "node": "~/a", "since": "2026-11-23T09:00:00", "job": "old", "text": "old", "level": "warning"},
             {"kind": "down", "node": "~/b", "since": "2026-11-23T08:00:00", "text": "b down", "level": "critical"},
             {"kind": "job_warned", "node": "~/c", "since": "2026-11-23T10:00:00", "job": "new", "text": "new", "level": "warning"}]
    found = bell.answer(watch, {"job_warned|~/a|job:old"})
    assert found["count"] == 2 and found["total"] == 3 and found["more"] == 0
    assert [(i["text"], i["new"]) for i in found["items"]] == [("b down", True), ("new", True), ("old", False)]
    many = bell.answer([{"kind": "job_failed", "node": "~/x", "job": str(n), "since": f"2026-11-23T10:{n:02d}:00", "text": str(n)}
                        for n in range(12)], set())
    assert len(many["items"]) == bell.SHOWN and many["more"] == 12 - bell.SHOWN and many["items"][0]["text"] == "11"
    assert len(many["keys"]) == 12


def test_the_marks_are_kept_in_a_file_of_mode_600(tmp_path):
    reads = bell.Reads(tmp_path / "bell.json")
    reads.mark("carlo", ["a", "b"], {"a", "b", "c"}, "2026-11-23T10:00:00")
    assert stat.S_IMODE((tmp_path / "bell.json").stat().st_mode) == 0o600
    # a thing gone is forgotten: its key does not stay for ever
    reads.mark("carlo", ["c"], {"b", "c"}, "2026-11-23T10:01:00")
    assert bell.Reads(tmp_path / "bell.json").of("carlo") == {"b", "c"}
    assert reads.of("mario") == set()


async def _warned(client, title, servers):
    async def action(job):
        job.warn("a module was skipped")
    job = client.app.state.jobs.submit(title, servers, "manual: admin", action)
    await wait_job(client, job.id)
    client.app.state.bell_cache.clear()
    return job


async def test_the_bell_of_the_machines_admin(workspaces, client):  # noqa: F811
    first = await _warned(client, "Update the modules of srv", ["srv/production"])
    data = (await client.get("/api/bell", headers={"Authorization": AUTH})).json()
    item = next(i for i in data["items"] if i["key"] == f"job_warned|~/srv|job:{first.id}")
    assert data["count"] >= 1 and item["new"] is True and item["page"] == f"/jobs/{first.id}"
    assert item["level"] == "warning" and data["map"] == "/instances?view=map"
    # Mark as read: the keys the dropdown was given; the count is 0, the thing is still listed, read
    resp = await client.post("/api/bell/read", headers=ADMIN_POST, json={"keys": data["keys"]})
    assert resp.status_code == 200 and resp.json()["count"] == 0
    kept = client.app.state.settings.servers_file.parent / bell.READ_FILE
    assert stat.S_IMODE(kept.stat().st_mode) == 0o600 and f"job:{first.id}" in kept.read_text()
    again = (await client.get("/api/bell", headers={"Authorization": AUTH})).json()
    assert again["count"] == 0 and [i["new"] for i in again["items"]] == [False] * len(again["items"])
    # a thing that comes after is new; the one read stays read
    second = await _warned(client, "Restart srv", ["srv/test"])
    data = (await client.get("/api/bell", headers={"Authorization": AUTH})).json()
    assert data["count"] == 1 and data["items"][0]["key"] == f"job_warned|~/srv|job:{second.id}"
    # the marks are each person's: the developer has both new
    dev = (await client.get("/api/bell", headers=basic("dev"))).json()
    assert dev["count"] == 2
    # what is not a list of keys is refused
    for bad in ({"keys": "all"}, {"keys": [1]}, {"keys": ["x" * 400]}):
        assert (await client.post("/api/bell/read", headers=ADMIN_POST, json=bad)).status_code == 400


async def test_a_customers_bell_is_its_workspaces_only(workspaces, client):  # noqa: F811
    job = await _warned(client, f"Staging of srv on lab9 from {HERE_NAME}", ["srv/production"])
    mario = await client.get("/api/bell", headers=basic("mario"))
    data = mario.json()
    assert [i["key"] for i in data["items"]] == [f"job_warned|~/srv|job:{job.id}"] and data["count"] == 1
    # redacted as the map says it to a customer: no name of this machine, no folder
    assert data["items"][0]["text"] == "Staging of srv on lab9 from the server"
    for hidden in (HERE_NAME, "/var/"):
        assert hidden not in mario.text
    # its mark is its own, the machine's admin still has it new
    resp = await client.post("/api/bell/read", headers={**basic("mario"), "Origin": ORIGIN}, json={"keys": data["keys"]})
    assert resp.status_code == 200 and resp.json()["count"] == 0
    assert (await client.get("/api/bell", headers={"Authorization": AUTH})).json()["count"] >= 1
    # a key that is not one of its things is not kept
    await client.post("/api/bell/read", headers={**basic("mario"), "Origin": ORIGIN}, json={"keys": ["down|~/other|"]})
    kept = json.loads((client.app.state.settings.servers_file.parent / bell.READ_FILE).read_text())
    assert kept["mario"]["read"] == [f"job_warned|~/srv|job:{job.id}"]
    # zoe, in no workspace: nothing, caught up
    zoe = (await client.get("/api/bell", headers=basic("zoe"))).json()
    assert zoe["count"] == 0 and zoe["items"] == []
    # a person removed: its marks go with it
    assert (await client.post("/api/access/users/delete", headers=ADMIN_POST, json={"name": "mario"})).status_code == 200
    assert "mario" not in json.loads((client.app.state.settings.servers_file.parent / bell.READ_FILE).read_text())


async def test_the_bell_in_the_header(workspaces, client):  # noqa: F811
    page = (await client.get("/", headers={"Authorization": AUTH, "Accept": "text/html", "Accept-Language": "it"})).text
    head = page.split('<div class="bell" id="bell">', 1)[1].split("</div>\n    </div>", 1)[0]
    assert "Cose da guardare" in head and "Segna come lette" in head and "Sei in pari" in head
    # every button and link says in its title what it does, in other words than its text; no primary button
    for tag, text in re.findall(r"(<(?:button|a)\b[^>]*>)(.*?)</(?:button|a)>", head, re.S):
        title = re.search(r'title="([^"]*)"', tag)
        assert title and title.group(1) and title.group(1) != re.sub(r"<[^>]+>", "", text).strip(), tag
    assert 'class="primary' not in head
    # the page is drawn without the list: the bell asks it after (nothing put together for the page)
    client.app.state.bell_cache.clear()
    await client.get("/", headers={"Authorization": AUTH, "Accept": "text/html"})
    assert client.app.state.bell_cache.get((("user", "admin", "", "admin", "en")))[0] is None
    # a user of workspaces has it too
    assert 'id="bell"' in (await client.get("/", headers={**basic("mario"), "Accept": "text/html"})).text
    # a token or the login page: none
    assert 'id="bell"' not in (await client.get("/login", headers={"Accept": "text/html"})).text
