# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The copy engine between two machines (treesync.py and its routes;
docs/adr/AD-008-consegna-e-spostamento.md AD-010, §9 steps 1 and 2): the
two applications talking through their links, root's helpers faked. lab2
has tsx-src; lab1 has tsx-dst and drives (pull), or lab1's tree goes to lab2
(push). The routes of a copy answer another machine's link with the admin
role only. The arrival of a dedicated database: what lab1 asks of lab2 and
of its own helper, the switch, its abort, and the refusal that sends a
database that a copy cannot follow to its dump."""
import asyncio
import hashlib
import json
import os

import httpx
import pytest
import yaml

from omniacli import i18n, linkcopy, ops, treesync, workspaceops
from omniacli.app import create_app
from omniacli.config import Workspace, parse_servers, parse_settings
from omniacli.jobs import JobFailed

from .conftest import LINK_TOKEN, USER_HASH, BasicAsSession, hash_token
from .test_linked_cluster import ADMIN, HOST, POST, Network, job_of
from .test_treesync import InProcess, same, stored

LINK = {"Authorization": f"Bearer {LINK_TOKEN}"}
SOURCE_KEY = "tsx.default.src"


def server(folder, database):
    return {"type": "docker", "production": {
        "restart": {"type": "compose", "service": str(folder / "production"), "sudo": True},
        "repositories": [], "database": database, "odoo_service": "odoo", "db_service": "db"}}


class Helpers:
    """The root helpers of the two machines (one module, two applications): what each asked, and its answers."""

    def __init__(self):
        self.calls, self.answers = [], {}

    async def helper(self, job, *args, timeout=None, input=None):
        self.calls.append((args, input))
        answer = self.answers.get(args[0]) or self.answers.get(" ".join(args[:3]))
        if isinstance(answer, Exception):
            raise answer
        return answer(args, input) if callable(answer) else dict(answer or {})

    async def ask(self, *args, timeout=None, input=None):
        return await self.helper(None, *args, timeout=timeout, input=input)

    def asked(self, *words):
        return [args for args, _ in self.calls if args[:len(words)] == words]


@pytest.fixture
async def pair(tmp_path, monkeypatch):
    trees = {"src": InProcess(tmp_path / "lab2-trees" / "src"), "dst": InProcess(tmp_path / "lab1-trees" / "dst")}
    monkeypatch.setattr(treesync, "LocalTree", lambda helper, ws, storage, instance, role, tree="filestore": trees[instance])
    monkeypatch.setattr(treesync, "BATCH_BYTES", 100_000)
    monkeypatch.setattr(linkcopy, "POLL_EVERY", 0.01)
    helpers = Helpers()
    monkeypatch.setattr(workspaceops, "_helper", helpers.helper)
    monkeypatch.setattr(workspaceops, "ask", helpers.ask)
    started = []

    async def run(job, cmd, **kwargs):
        started.append(cmd)
    monkeypatch.setattr(ops, "run", run)

    def settings_of(name, auth, instance):
        conf = tmp_path / name
        conf.mkdir()
        if name == "lab1":
            (conf / "links.yaml").write_text(yaml.safe_dump({"links": {"lab2": {"url": "https://lab2.test",
                                                                               "token": LINK_TOKEN}}}))
        found = parse_settings({"auth": auth, "backup_dir": str(conf / "backups"), "jobs_dir": str(conf / "jobs"),
                                "servers_file": "servers.yaml"}, base_dir=conf)
        found.storage_roots = {"default": str(conf / "storage")}
        folder = conf / "storage" / "tsx" / instance
        (folder / "production" / ".replica").mkdir(parents=True)
        (folder / "instance.json").write_text(json.dumps({"template": "odoo-19", "database": instance}))
        found.servers = parse_servers({f"tsx-{instance}": server(folder, instance)})
        found.workspaces = {"tsx": Workspace("tsx", "TSX", storage="default", limits={"disk_gb": 10},
                                             servers=[f"tsx-{instance}"])}
        return found
    holder = settings_of("lab2", {"users": {"admin": USER_HASH}, "links": {
        "lab1": {"token": hash_token(LINK_TOKEN), "role": "admin"},
        "reader": {"token": hash_token("a-developer-link"), "role": "developer"}}}, "src")
    lab2 = create_app(holder)
    asker = settings_of("lab1", {"users": {"admin": USER_HASH}}, "dst")
    network = Network(lab2)
    lab1 = create_app(asker, link_transport=network)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=BasicAsSession(lab1)),
                                 base_url=f"http://{HOST}") as client:
        client.there = httpx.AsyncClient(transport=httpx.ASGITransport(app=BasicAsSession(lab2)), base_url="http://lab2.test")
        client.trees, client.helpers, client.network, client.started = trees, helpers, network, started
        client.lab1, client.lab2, client.tmp = asker, holder, tmp_path
        yield client
        await client.there.aclose()


def copy_of(direction="pull", tree="filestore", **more):
    return {"link": "lab2", "direction": direction, "tree": tree, "flows": 1,
            "source": {"server": "tsx-src" if direction == "pull" else "tsx-dst", "role": "production"},
            "target": {"server": "tsx-dst" if direction == "pull" else "tsx-src", "role": "production"}, **more}


async def test_a_filestore_pulled_through_the_link_goes_on_from_its_batch(pair):
    src, dst = pair.trees["src"], pair.trees["dst"]
    for _ in range(12):
        stored(src.top, os.urandom(40_000))
    original, writes = dst.write, []

    async def breaks_once(chunks):
        writes.append(1)
        if len(writes) == 3:
            raise treesync.TreeError("the link broke")
        return await original(chunks)
    dst.write = breaks_once
    first = await job_of(pair, (await pair.post("/api/treesync/copy", headers=POST, json=copy_of())).json()["job"])
    assert first["status"] == "failed" and "the link broke" in first["error"]
    work = pair.lab1.jobs_dir.parent / "treesync" / "copies"
    state = json.loads(next(work.glob("*/state.json")).read_text())
    assert state["phase"] == "copying" and state["done"] == [0, 1] and state["batches"] == 6
    sent = src.sent
    again = await job_of(pair, (await pair.post("/api/treesync/copy", headers=POST, json=copy_of())).json()["job"])
    assert again["status"] == "success" and again["result"]["treesync"]["round"] == 0
    assert any("goes on: 2 of 6 batches were done" in line for line in again["lines"])
    # every line of the job, and its failure, in the reader's language
    untranslated = [line for line in [*first["lines"], *again["lines"]] if i18n.tr("it", line) == line]
    assert not untranslated, untranslated
    # what was done before is not asked again: 4 batches of about 80 kB
    assert src.sent - sent < 4 * 100_000 and same(dst.top, src.top)
    # the delta: nothing changed, nothing copied
    third = await job_of(pair, (await pair.post("/api/treesync/copy", headers=POST, json=copy_of())).json()["job"])
    assert third["result"]["treesync"]["round"] == 1 and third["result"]["treesync"]["copied_files"] == 0


async def test_backups_pushed_to_the_machine_of_the_link(pair):
    mine = pair.lab1.backup_dir / "tsx-dst" / "production"
    mine.mkdir(parents=True)
    data = os.urandom(3 * treesync.CHUNK + 5)
    (mine / "dst_20261123_0200.zip").write_bytes(data)
    done = await job_of(pair, (await pair.post("/api/treesync/copy", headers=POST,
                                               json=copy_of("push", "backups"))).json()["job"])
    assert done["status"] == "success" and done["result"]["treesync"]["copied_bytes"] == len(data)
    there = pair.lab2.backup_dir / "tsx-src" / "production"
    assert (there / "dst_20261123_0200.zip").read_bytes() == data
    # private, as every backup: the folders 700, the file 600
    assert (there / "dst_20261123_0200.zip").stat().st_mode & 0o777 == 0o600
    assert there.stat().st_mode & 0o777 == 0o700 and there.parent.stat().st_mode & 0o777 == 0o700


async def test_the_routes_of_a_copy_answer_a_machine_with_the_admin_role_only(pair):
    stored(pair.trees["src"].top, b"an attachment")
    spec = {"server": "tsx-src", "role": "production", "tree": "filestore"}
    assert (await pair.there.post("/api/treesync/v1/list", headers=LINK, json=spec)).status_code == 200
    # a person, even the machine's admin, and a link with a lesser role: refused
    person = await pair.there.post("/api/treesync/v1/list", headers={**ADMIN, "Origin": "http://lab2.test"}, json=spec)
    reader = await pair.there.post("/api/treesync/v1/list", headers={"Authorization": "Bearer a-developer-link"}, json=spec)
    assert person.status_code == 403 and reader.status_code == 403
    # what is not a tree of an instance here, or a path out of one
    assert (await pair.there.post("/api/treesync/v1/list", headers=LINK, json={**spec, "tree": "etc"})).status_code == 400
    assert (await pair.there.post("/api/treesync/v1/list", headers=LINK, json={**spec, "server": "x"})).status_code == 404
    out = await pair.there.post("/api/treesync/v1/read", headers=LINK, json={**spec, "paths": ["../../etc/passwd"]})
    assert out.status_code == 400 and "not a path under the tree" in out.json()["error"]
    gone = await pair.there.get("/api/treesync/v1/file", headers=LINK, params={
        "server": "tsx-src", "role": "production", "tree": "backups", "path": "../../users.yaml"})
    assert gone.status_code == 404
    # the copy itself is the machine's admins', never a machine's
    refused = await pair.post("/api/treesync/copy", headers={**LINK, "Origin": f"http://{HOST}"}, json=copy_of())
    assert refused.status_code in (401, 403)


async def test_a_backup_is_served_from_the_byte_its_range_says(pair):
    there = pair.lab2.backup_dir / "tsx-src" / "production"
    there.mkdir(parents=True)
    (there / "a.zip").write_bytes(b"0123456789")
    params = {"server": "tsx-src", "role": "production", "tree": "backups", "path": "a.zip"}
    whole = await pair.there.get("/api/treesync/v1/file", headers=LINK, params=params)
    part = await pair.there.get("/api/treesync/v1/file", headers={**LINK, "Range": "bytes=4-"}, params=params)
    assert whole.status_code == 200 and whole.content == b"0123456789"
    assert part.status_code == 206 and part.content == b"456789" and part.headers["content-range"] == "bytes 4-9/10"
    bad = await pair.there.get("/api/treesync/v1/file", headers={**LINK, "Range": "bytes=0-3"}, params=params)
    assert bad.status_code == 416


# --- the arrival of a dedicated database ------------------------------------------------------

FACTS = {"key": SOURCE_KEY, "kind": "dedicated", "major": "17", "arch": os.uname().machine, "size": 46 << 20,
         "template": "odoo-19", "database": "src", "name": "tsx-src", "why": "", "replica": None, "restarts": True,
         "running": True}


def arrival(step, **more):
    return {"link": "lab2", "step": step, "source": SOURCE_KEY, "workspace": "tsx", "instance": "arr",
            "address": "203.0.113.9", **more}


async def test_a_database_another_machine_cannot_follow_goes_by_a_dump(pair):
    pair.helpers.answers["machine-db-replica-facts"] = {**FACTS, "arch": "aarch64-not-this-one"}
    done = await job_of(pair, (await pair.post("/api/postgres/arrive", headers=POST, json=arrival("follow"))).json()["job"])
    assert done["status"] == "failed" and "a copy cannot follow it. It goes by a dump" in done["error"]
    # nothing was asked of lab2 but what it says, nothing made here
    assert not pair.helpers.asked("machine-db-replica-allow") and not pair.helpers.asked("create")
    # an address is asked before anything: the one this machine is seen with there
    refused = await pair.post("/api/postgres/arrive", headers=POST, json=arrival("follow", address="not one"))
    assert refused.status_code == 400


async def test_the_switch_waits_for_the_last_record_and_else_starts_the_source_again(pair):
    replica = pair.tmp / "lab2" / "storage" / "tsx" / "src" / "production" / ".replica" / "replica.json"
    replica.write_text(json.dumps({"link": "lab1", "address": "203.0.113.9", "state": "copying", "arrives": True}))
    helpers = pair.helpers
    helpers.answers["machine-db-replica-stop"] = lambda args, _: (
        {"stopped": True, "checkpoint": "0/40035D8", "seconds": 1.5} if args[2] == "stop" else {"resumed": True})
    helpers.answers["machine-db-arrive"] = lambda args, given: (
        {"arrived": False, "seconds": 60.0} if args[2] == "wait" else {"promote_s": 1.4, "own_service_s": 0.3})
    failed = await job_of(pair, (await pair.post("/api/postgres/arrive", headers=POST, json=arrival("switch"))).json()["job"])
    # not applied in time: the source runs again there, nothing is promoted here
    assert failed["status"] == "failed" and "the instance stays there" in failed["error"]
    assert [a[2] for a in helpers.asked("machine-db-replica-stop")] == ["stop", "resume"]
    assert not [a for a in helpers.asked("machine-db-arrive") if a[2] == "promote"]
    helpers.calls.clear()
    helpers.answers["machine-db-arrive"] = lambda args, given: (
        {"arrived": True, "seconds": 0.1} if args[2] == "wait" else {"promote_s": 1.4, "own_service_s": 0.3})
    done = await job_of(pair, (await pair.post("/api/postgres/arrive", headers=POST, json=arrival("switch"))).json()["job"])
    assert done["status"] == "success", done
    wait = next(given for args, given in helpers.calls if args[:1] == ("machine-db-arrive",) and args[2] == "wait")
    # the position the source said is the one waited for here
    assert json.loads(wait) == {"checkpoint": "0/40035D8"}
    assert [a[2] for a in helpers.asked("machine-db-arrive")] == ["wait", "promote"]
    assert done["result"]["switch"]["checkpoint"] == "0/40035D8" and done["result"]["switch"]["without_database_s"] >= 0
    # the instance here is started once its database took the writes
    assert pair.started and pair.started[-1][-1] == "start"
    # every line of the two jobs, the source's own told here, and the failure, in the reader's language
    untranslated = [line for line in [*failed["lines"], *done["lines"]] if i18n.tr("it", line) == line]
    assert not untranslated, untranslated
    # the stop of the source is a job of its instance, asked by the link of the copy's machine only
    other = await pair.there.post(f"/api/postgres/machine/{SOURCE_KEY}/replica/stop",
                                  headers={**ADMIN, "Origin": "http://lab2.test"}, json={"mode": "stop"})
    assert other.status_code == 403
