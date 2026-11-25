# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""New instance with too little room for a new workspace (ber, 25/11/2026): the disk had 121 GB free, but 116 were
promised to the workspace odoo19, and the dialog said «6 GB liberi, una nuova istanza ne vuole almeno 10» with no
way forward. The versions API says the real free room, what is promised and to which workspaces; the dialog says
it in a person's words and offers New instance inside a workspace whose own disk has room."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from omniacli import stacktemplates, workspaceops
from omniacli.config import Workspace

from .test_app import AUTH, client  # noqa: F401 - the client fixture
from .test_workspaces import GB, basic, space_of, workspaces  # noqa: F401 - the workspaces fixture

IT = {"Authorization": AUTH, "Cookie": "omniacli_lang=it"}


def ber(settings, monkeypatch, odoo19_gb=127, free_gb=121):
    """ber's disk: odoo19 promised odoo19_gb and taking 10.5, a full small one, free_gb free on the real disk."""
    root = Path(__file__).parent.parent / "templates"
    found = {n: stacktemplates.load_template(root / n) for n in ("odoo-18", "odoo-19")}
    monkeypatch.setattr(workspaceops, "templates", lambda: found)

    async def space():
        return space_of(free_gb, {"odoo19": (odoo19_gb, 10.5), "small": (12, 11.5)}, root="main")
    monkeypatch.setattr(workspaceops, "space", space)
    settings.storage_roots = {"main": "/s"}
    settings.workspaces = {**settings.workspaces,
                           "odoo19": Workspace("odoo19", storage="main", limits={"disk_gb": odoo19_gb}),
                           "small": Workspace("small", storage="main", limits={"disk_gb": 12})}


async def test_the_versions_say_whose_promise_takes_the_room(workspaces, client, env, monkeypatch):
    ber(env["settings"], monkeypatch)
    data = (await client.get("/api/instances/versions", headers={"Authorization": AUTH})).json()
    assert data["room_gb"] < data["min_gb"] == 10 and data["free_gb"] == 4
    assert (data["disk_free_gb"], data["reserved_gb"]) == (121, 117)
    # the most promised first; odoo19 has room inside its own disk, small has not
    assert data["promised"] == [
        {"name": "odoo19", "workspace": True, "promised_gb": 127, "used_gb": 10.5, "free_gb": 116, "new_instance": True},
        {"name": "small", "workspace": True, "promised_gb": 12, "used_gb": 11.5, "free_gb": 0, "new_instance": False}]
    # a workspace with all its instances has no room for another one, even with room on its disk
    settings = env["settings"]
    settings.workspaces["odoo19"].limits["instances"] = 1
    settings.workspaces["odoo19"].servers.append("srv")
    data = (await client.get("/api/instances/versions", headers={"Authorization": AUTH})).json()
    assert data["promised"][0]["new_instance"] is False
    # with room for a new workspace nothing of it is said
    ber(settings, monkeypatch, odoo19_gb=20, free_gb=300)
    data = (await client.get("/api/instances/versions", headers={"Authorization": AUTH})).json()
    assert data["room_gb"] >= data["min_gb"] and "promised" not in data and "disk_free_gb" not in data
    # the API stays the machine's admins': a workspace's admin does not reach it
    assert (await client.get("/api/instances/versions", headers=basic("mario"))).status_code == 403


def test_an_image_no_workspace_names():
    """An image no workspace names any more is promised room too, with nothing inside to offer."""
    found = space_of(50, {"gone": (40, 5)}, root="main")
    assert workspaceops.promised_room(found, {}, "main") == [
        {"name": "gone", "promised": 40 * GB, "used": 5 * GB, "inside": None}]


def dialog_parts(page: str) -> str:
    """The dialog's words and its room advice, with base.html's address helpers, to run in node."""
    start = page.index("window.omniaPrefix")
    helpers = page[start:page.index("window.omniaOpen", start)]
    words = re.search(r"const T = \{creating:.*?\};\n", page, re.S).group(0)
    advice = page[page.index("// room-advice:"):page.index("// room-advice end")]
    return "const window = {};\n" + helpers + "\nconst omniaPath = window.omniaPath;\n" + words + advice


def run_advice(page: str, data: dict, base: str) -> dict:
    script = (dialog_parts(page) + f"\nconst data = {json.dumps(data)};\n"
              + f"console.log(JSON.stringify({{why: lowRoom(data), advice: roomAdvice(data, {json.dumps(base)})}}));")
    return json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
async def test_the_dialog_says_it_and_offers_the_workspace(workspaces, client, env, monkeypatch):
    ber(env["settings"], monkeypatch)
    page = (await client.get("/instances", headers=IT)).text
    data = (await client.get("/api/instances/versions", headers={"Authorization": AUTH})).json()
    # in Italian, the disk's free room and whose promise takes it
    shown = run_advice(page, data, "")
    assert shown["why"] == ("121 GB liberi sul disco, ma 117 sono promessi ai workspace odoo19, small: "
                            "una nuova istanza ne vuole almeno 10")
    # New instance inside odoo19, the only one with room, on its page
    assert shown["advice"] == {"text": "Si può creare dentro un workspace che ha spazio nel suo disco:",
                               "ways": [{"name": "odoo19", "free": 116, "href": "/workspaces/odoo19#new-instance-panel"}]}
    one = {**data, "promised": data["promised"][:1]}
    assert run_advice(page, one, "")["why"].startswith("121 GB liberi sul disco, ma 117 sono promessi al workspace odoo19:")
    # the link's text, as the dialog draws it
    assert "Crea dentro {name} ({free} GB liberi)" in page
    # through a link: under its prefix (the central panel draws the machine's Overview there)
    shown = run_advice(page, data, "/links/ber")
    assert [w["href"] for w in shown["advice"]["ways"]] == ["/links/ber/workspaces/odoo19#new-instance-panel"]
    # a name that is no workspace's name never becomes an address
    hostile = {**data, "promised": [{**data["promised"][0], "name": n} for n in ("../options", "a/b", "A", "x y", "")]}
    assert run_advice(page, hostile, "/links/ber")["advice"]["ways"] == []


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
async def test_no_workspace_with_room_says_what_to_do(workspaces, client, env, monkeypatch):
    settings = env["settings"]
    ber(settings, monkeypatch)
    settings.workspaces["odoo19"].limits["instances"] = 1
    settings.workspaces["odoo19"].servers.append("srv")
    page = (await client.get("/instances", headers=IT)).text
    data = (await client.get("/api/instances/versions", headers={"Authorization": AUTH})).json()
    shown = run_advice(page, data, "/links/ber")
    assert shown["advice"]["ways"] == []
    # never «lower a workspace's disk»: a disk is not made smaller (workspaceops.disk_problem)
    assert shown["advice"]["text"] == ("Nessun workspace ha spazio per una nuova istanza nel suo disco, e il disco "
                                       "di un workspace non si riduce: libera spazio sul disco di questa macchina, "
                                       "oppure dalle un disco più grande.")
    # an older OmniaCLI says only the room left: the words of before, no advice
    old = {k: v for k, v in data.items() if k not in ("disk_free_gb", "reserved_gb", "promised")}
    shown = run_advice(page, old, "")
    assert shown == {"why": "4 GB liberi sul disco, una nuova istanza ne vuole almeno 10", "advice": None}
