# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The replica of the dedicated database of one instance (UX-011 §D), its
return after a promotion (§A, for one instance) and Close the port…: the
two applications talking through their links, root's helpers faked. lab2
has acme-odoo, a production with a database of its own (P); lab1 hosts the
copy (R). What root itself does is in test_dedicated_replica.py and
test_dedicated_return.py."""
import json

import httpx
import pytest

from omniacli import i18n, machinedb, managedreplica, stacktemplates, workspaceops
from omniacli.jobs import JobFailed

from .conftest import LINK_TOKEN
from .test_linked_cluster import ADMIN, POST, Network, job_of
from .test_linked_cluster import machines as linked_machines  # noqa: F401
from .test_managed_replica import (CA, GB, MARIO, MB, PERSON, CENTRAL, TEMPLATES, THERE, access_log, machines,  # noqa: F401
                                   make)
from .test_replica_return import link_back

KEY, COPY = "acme.default.odoo", "d100"
LINK = {"Authorization": f"Bearer {LINK_TOKEN}"}
MARIO_POST = {**MARIO, "Origin": "http://lab2.test"}
VERIFIER = "SCRAM-SHA-256$4096:c2FsdHNhbHRzYWx0c2FsdA==$" + "A" * 43 + "=:" + "B" * 43 + "="
FACTS, PREPARE, COMPLETE, CANCEL = (f"/api/postgres/machine/{KEY}/return/{step}"
                                    for step in ("facts", "prepare", "complete", "cancel"))
CLOSE = f"/api/postgres/machine/{KEY}/port/close"
# what a user of the workspace never reads: the machines, their addresses, the ports
NAMES = ("lab1", "lab2", "203.0.113", "15433", "15440")


class Ded:
    """What the root helpers of the two machines would do for the dedicated
    database of acme-odoo, kept in memory and in the files the application
    reads (the instance's .replica folder, root's two records)."""

    def __init__(self, lab, tmp_path):
        self.lab, self.calls, self.inputs, self.fail = lab, [], {}, {}
        self.folder = lab.storage / "acme" / "odoo"
        self.replica = self.folder / "production" / ".replica"
        self.sources = tmp_path / "dedicated-replica.json"
        self.local = None  # lab1's own record of the copy
        self.arrives, self.free, self.kept = True, 31 * GB, True

    def read(self, name):
        try:
            return json.loads((self.replica / name).read_text())
        except OSError:
            return None

    def write(self, name, value):
        self.replica.mkdir(exist_ok=True)
        if value is None:
            (self.replica / name).unlink(missing_ok=True)
        else:
            (self.replica / name).write_text(json.dumps(value))

    def meta(self, **change):
        path = self.folder / "instance.json"
        data = json.loads(path.read_text())
        for key, value in change.items():
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value
        path.write_text(json.dumps(data))

    def hosted_file(self, there):
        found = json.loads(self.lab.hosted_file.read_text()) if self.lab.hosted_file.exists() else {}
        found.pop(COPY, None)
        if there:
            found[COPY] = {"storage": "default", "project": "/x/replica-d100", "link": "lab2", "source": KEY,
                           "name": "acme-odoo", "workspace": "acme", "major": "17"}
        if found:
            self.lab.hosted_file.write_text(json.dumps(found))
        else:
            self.lab.hosted_file.unlink(missing_ok=True)

    def state(self):
        rec = self.read("replica.json")
        if rec is None:
            return {"exists": False, "major": KEY}
        return {"exists": True, "major": KEY, "kind": "dedicated", **rec, "connected": rec["state"] == "ready", "lag": 2,
                "since": None, "held": 0, "cap": int(rec["cap_gb"] * GB), "cap_max": 5.0, "wal_status": "reserved",
                "progress": None, "rate": None, "free": self.free, "environments": ["odoo"], "instances": ["acme-odoo"],
                "tablespaces": [], "grants": [], "grant_lines": {}}

    def coming(self):
        back = (self.local or {}).get("return") or {}
        if back.get("state") not in ("copying", "ready", "stopped"):
            return {"exists": False, "major": COPY, "copy": (self.local or {}).get("state"),
                    "cancelled": {"by": back.get("by"), "at": back.get("at")} if back.get("state") == "cancelled" else None}
        return {"exists": True, "major": COPY, "copy": "promoted", **back, "connected": back["state"] == "ready",
                "lag": 2 if back["state"] == "ready" else None, "since": None, "held": 3 * MB, "cap": 20 * GB,
                "wal_status": "reserved", "progress": None, "rate": None, "free": 290 * GB}

    def ask(self, args, input):
        if args[0] == "machine-db-replica-facts":
            rec, listen = self.read("replica.json"), self.read("listen.json")
            return {"major": "17", "key": KEY, "kind": "dedicated", "running": True, "size": 46 * MB, "version": "17.12",
                    "port": (listen or {}).get("port"), "restarts": listen is None, "why": "", "roots": [],
                    "instances": ["acme-odoo"], "grants": [], "workspace": "acme", "name": "acme-odoo",
                    "disk_total": 50 * GB, "disk_free": self.free, "cap_gb": 5.0, "restart_s": 5,
                    "replica": {"link": rec["link"], "state": rec["state"]} if rec else None}
        if args[0] == "machine-db-replica-state":
            return self.state()
        if args[0] == "machine-db-replica-ready":
            spec = json.loads(input)
            self.write("replica.json", {**self.read("replica.json"), "state": "ready", "host": spec["host"],
                                        "port": spec["port"], "ca": spec["ca"], "ready_at": "2026-11-22T12:55:40"})
            return {"state": "ready", "host": spec["host"], "port": spec["port"]}
        if args[0] == "machine-db-replica-hosted":
            local = {k: v for k, v in self.local.items() if k != "return"}
            found = {"exists": True, "major": COPY, "storage": "default", "running": local["state"] != "returned",
                     "receiving": local["state"] == "ready", "reason_code": "", "connections": 1, "max_connections": 100,
                     "free": 297 * GB, "disk": 60 * MB, "size": 46 * MB, "copied": 60 * MB, "version": "17.12",
                     "in_recovery": local["state"] == "ready", **local}
            there = self.coming()
            if there["exists"]:
                found["return"] = {**there, "verdict": managedreplica.verdict(there)}
            return found
        if args[0] == "machine-db-replica-return":
            if args[2] == "facts":
                return {"major": COPY, "running": True, "size": 46 * MB, "version": "17.12", "port": 15433,
                        "host": "203.0.113.7", "address": "203.0.113.9", "why": "", "tablespaces": [],
                        "under_way": self.coming()["exists"]}
            return self.coming()
        if args[0] == "machine-db-replica-sync":
            self.inputs["sync"] = json.loads(input)
            return {"changed": []}
        assert args[0] == "machine-db-return", args
        step, rec, back = args[2], self.read("replica.json"), self.read("return.json")
        if step == "facts":
            return {"major": KEY, "machine": rec["link"], "host": rec["host"], "port": rec["port"],
                    "aside": {"at": rec["replaced_at"], "size": 50 * MB if self.kept else 0}, "free": self.free, "disks": [],
                    "listening": True, "kind": "dedicated", "name": "acme-odoo", "workspace": "acme",
                    "return": {k: back.get(k) for k in ("link", "state", "at")} if back else None}
        if step == "state":
            if not back:
                return {"exists": False, "major": KEY}
            return {"exists": True, "major": KEY, **back, "running": back["state"] == "ready",
                    "receiving": back["state"] == "ready", "reason_code": "", "free": self.free,
                    "copied": 30 * MB if back["state"] == "copying" else None, "in_recovery": back["state"] != "done"}
        if step == "note":
            self.write("return.json", {**back, **json.loads(input)})
            return {}
        if step == "forget":
            self.write("return.json", None)
            return {"forgotten": True}
        raise AssertionError(args)

    def act(self, args, input):
        if args[0] in self.fail or args[:3] in self.fail:
            raise JobFailed(self.fail.get(args[0]) or self.fail[args[:3]])
        rec = self.read("replica.json")
        if args[0] == "machine-db-replica-allow":
            spec = self.inputs["allow"] = json.loads(input)
            cap = 5.0 if spec.get("cap_gb") is None else float(spec["cap_gb"])
            if cap > 5.0:
                raise JobFailed("workspace-helper: what the PostgreSQL of acme-odoo keeps for its replica is on the disk of "
                                "the workspace acme: 5 GB at most (a tenth of that disk, and never more than 20 GB)")
            first = self.read("listen.json") is None
            if first and not spec.get("restart"):
                raise JobFailed("workspace-helper: the PostgreSQL of acme-odoo is restarted once to open a port over TLS")
            self.write("listen.json", {"port": 15440, "bridge": ""})
            self.sources.write_text(json.dumps({KEY: {"project": str(self.replica.parent), "workspace": "acme",
                                                      "storage": "default", "instance": "odoo"}}))
            self.write("replica.json", {"link": spec["link"], "by": spec["by"], "address": spec["address"],
                                        "at": "2026-11-22T12:55:00", "cap_gb": cap, "state": "copying"})
            return {"port": 15440, "restarted_s": 0.4 if first else None, "address": spec["address"], "cap_gb": cap, "ca": CA,
                    "version": "17.12", "size": 46 * MB, "names": [spec["reached_as"], "db"], "roots": [],
                    "environments": ["odoo"], "tablespaces": [], "serves": 1, "grants": [], "kind": "dedicated",
                    "major": "17", "workspace": "acme", "name": "acme-odoo", "firewall": [spec["address"]]}
        if args[0] == "machine-db-replica-make":
            if args[2] == "prepare":
                spec = self.inputs["prepare"] = json.loads(input)
                self.local = {"link": spec["link"], "by": spec["by"], "host": spec["host"], "port": spec["port"],
                              "address": spec["address"], "own_port": 15433, "primary_address": "203.0.113.9",
                              "state": "copying", "at": "2026-11-22T12:55:02", "expected": spec["size"], "synced": False,
                              "source": spec["source"], "key": COPY}
                self.hosted_file(True)
                return {"port": 15433, "primary_address": "203.0.113.9", "address": spec["address"]}
            if args[2] == "copy":
                self.local["copy_seconds"] = 1.9
                return {"seconds": 1.9, "size": 60 * MB}
            self.local.update(state="ready", ready_at="2026-11-22T12:55:40", synced=True)
            return {"state": "ready", "port": 15433, "address": self.local["address"], "version": "17.12", "seconds": 1.2,
                    "ca": CA, "size": 60 * MB, "copy_seconds": 1.9}
        if args[0] == "machine-db-replica-remove":
            self.local = None
            self.hosted_file(False)
            return {"removed": "/x/replica-d100", "freed": 60 * MB, "link": "lab2"}
        if args[0] == "machine-db-replica-drop":
            self.write("replica.json", None)
            return {"dropped": True, "link": rec["link"], "address": rec["address"], "freed": 0, "firewall": []}
        if args[0] == "machine-db-replica-cap":
            self.write("replica.json", {**rec, "cap_gb": float(args[2])})
            return {"cap_gb": float(args[2])}
        if args[0] == "machine-db-replica-replaced":
            self.write("replica.json", {**rec, "state": "replaced", "replaced_at": "2026-11-22T13:02:00"})
            return {"stopped": True, "already": rec["state"] == "replaced", "replaced_at": "2026-11-22T13:02:00",
                    "seconds": 0.5, "grants": [], "environments": {"odoo": {"workspace": "acme", "instance": "odoo"}}}
        if args[0] == "machine-db-replica-follow":
            self.meta(template="odoo-19-remote", database_on="203.0.113.7:15433", database_link="lab1",
                      database_major="17", promoted_from={"template": "odoo-19", "at": "2026-11-22T13:02:05",
                                                          "dedicated": True})
            return {"already": False, "database": "odoo", "db_service": "remote", "host": "203.0.113.7", "port": 15433}
        if args[0] == "machine-db-replica-promote":
            self.local.update(state="promoted", promoted_at="2026-11-22T13:02:03")
            return {"promoted_at": "2026-11-22T13:02:03", "already": False, "seconds": 0.3, "lent": []}
        if args[0] == "machine-db-replica-back":
            self.meta(template="odoo-19", promoted_from=None, database_on=None, database_link=None, database_major=None)
            return {"already": False, "database": "odoo", "db_service": "db", "template": "odoo-19", "listens": True}
        if args[0] == "machine-db-purge":
            return {"workspace": "acme", "instance": "odoo", "removed": [str(self.replica.parent / "data" / "pgdata")]}
        if args[0] == "remove":
            # as root's helper answers of an instance whose database is on another machine
            return {"workspace": "acme", "instance": "odoo", "removed": str(self.folder),
                    "database_left_on": ["203.0.113.7:15433"]}
        if args[0] == "machine-db-replica-unlisten":
            if rec is not None:
                raise JobFailed("workspace-helper: the read replica follows the PostgreSQL of acme-odoo through this port")
            for name in ("listen.json", "return.json"):
                self.write(name, None)
            self.sources.unlink(missing_ok=True)
            return {"closed": 15440, "restarted_s": 0.4}
        step = args[2]
        if args[0] == "machine-db-replica-return":
            back = self.local.get("return") or {}
            if step == "allow":
                spec = self.inputs["let in"] = json.loads(input)
                self.local["return"] = {"link": spec["link"], "by": spec["by"], "address": "203.0.113.9",
                                        "at": "2026-11-23T09:02:00", "cap_gb": spec["cap_gb"], "state": "copying"}
                return {"port": 15433, "host": "203.0.113.7", "address": "203.0.113.9", "cap_gb": spec["cap_gb"], "ca": CA,
                        "version": "17.12", "size": 46 * MB, "tablespaces": []}
            if step == "stop":
                self.local["return"] = {**back, "state": "stopped", "checkpoint": "0/3000028"}
                return {"stopped": True, "already": False, "checkpoint": "0/3000028", "stopped_at": "2026-11-23T09:12:00",
                        "seconds": 0.4}
            if step == "resume":
                self.local["return"] = {**back, "state": "ready"}
                return {"resumed": True, "seconds": 1.2}
            if step == "done":
                self.local.update(state="returned", returned_at="2026-11-23T09:12:05")
                self.local["return"] = {**back, "state": "done"}
                return {"returned_at": "2026-11-23T09:12:05", "already": False}
            self.local["return"] = {"state": "cancelled", "by": json.loads(input)["by"], "at": "2026-11-23T09:05:00"}
            return {"cancelled": True, "freed": 3 * MB}
        back = self.read("return.json")
        if step == "prepare":
            spec = self.inputs["set aside"] = json.loads(input)
            aside = {"at": rec["replaced_at"], "size": 50 * MB if self.kept else 0}
            self.write("return.json", {"link": spec["link"], "by": spec["by"], "host": spec["host"], "port": spec["port"],
                                       "state": "copying", "at": "2026-11-23T09:02:01", "expected": spec["size"],
                                       "machine": rec["link"], "aside": aside, "major": spec["major"]})
            return {"major": KEY, "aside": aside}
        if step == "copy":
            return {"seconds": 2.1, "size": 60 * MB}
        if step == "start":
            self.write("return.json", {**back, "state": "ready", "ready_at": "2026-11-23T09:02:10", "synced": True})
            self.local["return"]["state"] = "ready"
            return {"state": "ready", "seconds": 1.5}
        if step == "wait":
            self.inputs["wait"] = json.loads(input)
            return {"arrived": self.arrives, "seconds": 0.4 if self.arrives else 60.0}
        if step == "promote":
            self.write("return.json", {**back, "state": "done", "returned_at": "2026-11-23T09:12:03"})
            self.write("replica.json", None)
            return {"returned_at": "2026-11-23T09:12:03", "seconds": 0.3}
        if step == "purge":
            return {"freed": 50 * MB if self.kept else 0}
        if step == "cancel":
            self.write("return.json", None)
            return {"cancelled": True, "freed": 60 * MB, "link": "lab1", "machine": "lab1"}
        raise AssertionError(args)

    def steps(self):
        short = {"machine-db-replica-make": "make", "machine-db-return": "return", "machine-db-replica-return": "copy-return"}
        return [f"{short[c[0]]}:{c[2]}" if c[0] in short else c[0].replace("machine-db-replica-", "")
                for c in self.calls if not (c[0] in short and c[2] in ("state", "facts", "note"))
                and c[0] not in ("machine-db-replica-state", "machine-db-replica-hosted", "machine-db-replica-facts",
                                 "machine-db-replica-ready")]


def ours(args) -> bool:
    return (KEY in args[1:2] or COPY in args[1:2] or args[1:4] == ("acme", "default", "odoo"))


@pytest.fixture
async def pair(machines, monkeypatch, tmp_path):  # noqa: F811
    """The two machines of test_managed_replica, and on lab2 acme-odoo: a
    production with a database of its own."""
    client = machines
    found = {t: stacktemplates.load_template(TEMPLATES / t) for t in ("odoo-19", "odoo-19-machine", "odoo-19-remote")}
    monkeypatch.setattr(workspaceops, "templates", lambda: found)
    box = Ded(client.lab, tmp_path)
    monkeypatch.setattr(machinedb, "DEDICATED_SOURCES", box.sources)

    async def create_instance(job, ws, instance, tpl, values, setup=None, remote=None):
        (box.folder / "production").mkdir(parents=True)
        (box.folder / "instance.json").write_text(json.dumps({"template": tpl.name, "database": "odoo"}))
        return {"type": "docker", "production": {
            "database": "odoo", "odoo_service": "odoo", "db_service": "db", "http_port": 20080, "update_modules": True,
            "restart": {"type": "compose", "service": str(box.folder / "production"), "sudo": True}, "repositories": []}}
    monkeypatch.setattr(workspaceops, "create_instance", create_instance)
    made = await client.there.post("/api/workspaces/acme/instances/create", headers=THERE,
                                   json={"instance": "odoo", "template": "odoo-19"})
    assert made.status_code == 200, made.text
    assert (await job_of(client.there, made.json()["job"]))["status"] == "success"
    ask, helper = workspaceops.ask, workspaceops._helper

    async def asked(*args, timeout=None, input=None):
        if ours(args):
            box.calls.append(args)
            return box.ask(args, input)
        return await ask(*args, timeout=timeout, input=input)

    async def helped(job, *args, timeout=None, input=None):
        if ours(args):
            box.calls.append(args)
            job.log("$ sudo -n omniacli workspace-helper " + " ".join(args))
            return box.act(args, input)
        return await helper(job, *args, timeout=timeout, input=input)
    monkeypatch.setattr(workspaceops, "ask", asked)
    monkeypatch.setattr(workspaceops, "_helper", helped)
    monkeypatch.setattr(managedreplica, "hosted_record", lambda project: dict(box.local or {}))
    client.box = box
    # lab2's own way to lab1, for the return (the link itself is made by link_back)
    client.way = Network(client.lab1)
    client.lab2.state.link_clients.transport = client.way
    yield client


async def done(client, resp, there=False):
    assert resp.status_code == 200, resp.text
    job = await job_of(client.there if there else client, resp.json()["job"])
    assert job["status"] == "success", job
    return job


async def copied(client, **change):
    return await make(client, major=KEY, cap_gb="", restart=True, **change)


async def promoted(client):
    await copied(client)
    await client.lab1.state.replica_look_hosted()
    return await done(client, await client.post(f"/api/postgres/replica/{COPY}/promote", headers=POST,
                                                json={"confirm": "acme-odoo"}))


async def its_jobs(client, headers=MARIO):
    """The jobs of lab2 a user of the workspace acme reads, each with its lines."""
    listed = (await client.there.get("/api/jobs", headers=headers)).json()["jobs"]
    return [(await client.there.get(f"/api/jobs/{j['id']}", headers=headers)).json() for j in listed]


def row_of(data, server="acme-odoo"):
    return next(r for r in data["rows"] if r.get("server") == server)


# --- the replica: what is offered, let in, capped, dropped, promoted ----------------------------

async def test_what_a_machine_offers_to_copy(pair):
    client = pair
    found = (await client.there.get("/api/postgres/replica/sources", headers=ADMIN)).json()
    assert found["machine"] and [s["key"] for s in found["sources"]] == ["17", KEY]
    one = found["sources"][1]
    # the production with a database of its own, by the name its page has; the instance on the cluster is the cluster's
    assert (one["kind"], one["name"], one["workspace"], one["size"], one["replica"]) == ("dedicated", "acme-odoo", "acme",
                                                                                         46 * MB, None)
    assert found["sources"][0]["kind"] == "cluster" and "acme-mach" in found["sources"][0]["instances"]
    # the dialog of the other machine: which PostgreSQL, one customer's data, its disk, the cap, the restart
    said = (await client.get(f"/api/links/lab2/postgres/replica?which={KEY}", headers=ADMIN)).json()
    assert said["error"] is None and said["kind"] == "dedicated" and said["name"] == "acme-odoo"
    assert [c["key"] for c in said["choices"]] == ["17", KEY] and said["workspace"] == "acme"
    assert (said["cap_gb"], said["cap_max"], said["restarts"], said["disk_total"]) == (5.0, 5.0, True, 50 * GB)
    # only the machine's admins: a user of the workspace reads none of it
    assert (await client.there.get("/api/postgres/replica/sources", headers=MARIO)).status_code == 403
    assert (await client.there.get(f"/api/postgres/machine/{KEY}/replica/facts", headers=MARIO)).status_code == 403


async def test_let_in_through_a_link_only_and_a_production_with_its_own_database_only(pair):
    client = pair
    payload = {"host": "203.0.113.7", "reached_as": "lab2.test", "verifier": VERIFIER, "restart": True}
    path = f"/api/postgres/machine/{KEY}/replica"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.lab2), base_url="http://lab2.test") as raw:
        # asked by the machine that hosts the copy, by itself: not by an admin, not by a person through a panel
        assert (await client.there.post(path, headers=THERE, json=payload)).status_code == 403
        assert (await raw.post(path.replace(KEY, "acme.default.odoo"), headers=PERSON, json=payload)).status_code == 403
        assert (await client.there.post(path, headers=MARIO_POST, json=payload)).status_code == 403
        # an instance on the machine's cluster has no database of its own; nor one that is not here
        resp = await raw.post("/api/postgres/machine/acme.default.mach/replica", headers=LINK, json=payload)
        assert resp.status_code == 400 and "is not a production with a database of its own" in resp.json()["error"]
        assert (await raw.post("/api/postgres/machine/acme.default.none/replica", headers=LINK, json=payload)).status_code == 404
        resp = await raw.post(path, headers=LINK, json={**payload, "verifier": "a password"})
        assert resp.status_code == 400 and "SCRAM verifier" in resp.json()["error"]
        assert client.box.steps() == []
        # more than a tenth of the customer's disk: root refuses, and nothing is let in
        resp = await raw.post(path, headers=LINK, json={**payload, "cap_gb": 20})
        job = await job_of(client.there, resp.json()["job"])
        assert job["status"] == "failed" and "5 GB at most" in job["error"] and client.box.read("replica.json") is None
        # as the helper says it: no cap asked is the most that disk allows
        resp = await raw.post(path, headers=LINK, json=payload)
        job = await job_of(client.there, resp.json()["job"])
    assert job["status"] == "success" and client.box.inputs["allow"]["cap_gb"] is None
    assert (client.box.inputs["allow"]["link"], client.box.inputs["allow"]["address"]) == ("lab1", "203.0.113.7")
    assert job["result"]["replica"]["port"] == 15440 and VERIFIER not in json.dumps(job)
    assert any("keeps 5 GB of them at most, on the disk of the workspace acme" in line for line in job["lines"])


async def test_a_replica_of_one_instance_is_made_and_the_customer_reads_of_it(pair):
    client = pair
    job = await copied(client)
    assert client.box.steps() == ["allow", "make:prepare", "make:copy", "make:start"]
    prepare = client.box.inputs["prepare"]
    # the copy has its own key here, and says whose database it is
    assert prepare["source"] == {"key": KEY, "workspace": "acme", "name": "acme-odoo"} and prepare["major"] == "17"
    assert any("a read replica of the PostgreSQL of acme-odoo of lab2 on this machine" in line for line in job["lines"])
    hosted = (await client.get("/api/postgres", headers=ADMIN)).json()["replicas"]
    assert [(r["major"], r["instance"], r["source"], r["here"]["state"]) for r in hosted] == [(COPY, "acme-odoo", KEY, "ready")]
    # on the machine of the instance: its own row says the replica, and the port it listens on
    row = row_of((await client.there.get("/api/postgres", headers=ADMIN)).json())
    assert row["pg"] == KEY and row["replica"]["link"] == "lab1" and row["replica"]["verdict"]["code"] == "sync"
    assert row["port"] == 15440
    # the customer (D11-2): a job of its instance, which names no machine, no address and no port
    jobs = await its_jobs(client)
    note = next(j for j in jobs if j["title"] == "Copy of the database of acme-odoo on another machine of the provider")
    assert note["status"] == "success" and "restarted once" in "\n".join(note["lines"])
    assert not any(name in json.dumps(jobs) for name in NAMES)
    # the jobs that name them are the machine's: not the customer's to read
    machine = next(j for j in (await client.there.get("/api/jobs", headers=ADMIN)).json()["jobs"]
                   if j["title"].startswith("Read replica of the PostgreSQL of acme-odoo"))
    assert (await client.there.get(f"/api/jobs/{machine['id']}", headers=MARIO)).status_code in (403, 404)
    # nor its state, its cap, its drop
    assert (await client.there.get(f"/api/postgres/machine/{KEY}/replica/state", headers=MARIO)).status_code == 403
    for path in ("replica/cap", "replica/drop", "port/close", "return/prepare", "return/complete", "return/cancel"):
        resp = await client.there.post(f"/api/postgres/machine/{KEY}/{path}", headers=MARIO_POST, json={})
        assert resp.status_code == 403, path
    assert (await client.there.get(FACTS, headers=MARIO)).status_code == 403
    # one per database
    resp = await client.post("/api/postgres/replica/make", headers=POST, json={"link": "lab2", "major": KEY,
                                                                            "address": "203.0.113.7"})
    assert resp.status_code == 400 and "hosts a read replica of acme-odoo of lab2 already" in resp.json()["error"]


async def test_the_cap_is_never_above_the_tenth_of_the_customers_disk(pair):
    client = pair
    await copied(client)
    await client.lab2.state.replica_look_own()
    path = f"/api/postgres/machine/{KEY}/replica/cap"
    resp = await client.there.post(path, headers=THERE, json={"cap_gb": 20})
    assert resp.status_code == 400 and "5 GB at most, a tenth of that disk" in resp.json()["error"]
    assert "replica-cap" not in "".join(client.box.steps())
    job = await done(client, await client.there.post(path, headers=THERE, json={"cap_gb": "2.5"}), there=True)
    assert client.box.read("replica.json")["cap_gb"] == 2.5
    assert any("The PostgreSQL of acme-odoo keeps 2.5 GB of changes for its read replica at most" in line for line in job["lines"])
    # no replica: nothing to cap
    await done(client, await client.there.post(f"/api/postgres/machine/{KEY}/replica/drop", headers=THERE), there=True)
    assert (await client.there.post(path, headers=THERE, json={"cap_gb": 2})).status_code == 404


async def test_dropped_from_the_machine_of_the_instance_and_the_customer_reads_of_it(pair):
    client = pair
    await copied(client)
    await client.lab1.state.replica_look_hosted()
    job = await done(client, await client.there.post(f"/api/postgres/machine/{KEY}/replica/drop", headers=THERE), there=True)
    assert client.box.read("replica.json") is None and client.box.read("listen.json") == {"port": 15440, "bridge": ""}
    lines = "\n".join(job["lines"])
    assert "Its port stays open over TLS for the next replica, and nothing restarted" in lines
    assert "it deletes its copy at its next look" in lines
    jobs = await its_jobs(client)
    assert any("is no longer kept" in j["title"] for j in jobs) and not any(name in json.dumps(jobs) for name in NAMES)
    # asked again: gone already; the machine that hosts it deletes its copy at its next look
    assert (await client.there.post(f"/api/postgres/machine/{KEY}/replica/drop", headers=THERE)).json() == {"ok": True, "gone": True}
    await client.lab1.state.replica_look_hosted()
    for _ in range(100):
        if client.box.local is None:
            break
        await __import__("asyncio").sleep(0.02)
    assert client.box.local is None and COPY not in machinedb.hosted_replicas()


async def test_a_promotion_is_refused_before_anything_stops(pair):
    client = pair
    path = f"/api/postgres/replica/{COPY}/promote"
    assert (await client.post(path, headers=POST, json={"confirm": "x"})).status_code == 404
    await copied(client)
    await client.lab1.state.replica_look_hosted()
    # the instance's name is what is typed, not the machine's
    resp = await client.post(path, headers=POST, json={"confirm": "lab2"})
    assert resp.status_code == 400 and "Write acme-odoo to confirm" in resp.json()["error"]
    # a copy not whole is never promoted
    client.box.local["synced"] = False
    resp = await client.post(path, headers=POST, json={"confirm": "acme-odoo"})
    assert resp.status_code == 400 and "not a whole copy yet" in resp.json()["error"]
    client.box.local["synced"] = True
    # its machine does not answer: the instance itself runs there, and is pointed here by it. No way with it off
    client.network.down = True
    resp = await client.post(path, headers=POST, json={"confirm": "acme-odoo", "offline": True})
    assert resp.status_code == 409 and "is promoted only with lab2 answering" in resp.json()["error"]
    client.network.down = False
    assert not any(step in client.box.steps() for step in ("replaced", "promote", "follow"))


async def test_promoted_the_instance_keeps_its_row_and_the_customer_reads_no_machine(pair):
    client = pair
    job = await promoted(client)
    # that one PostgreSQL stopped, the copy takes the writes, that one instance pointed here: no other touched
    # (the machine of the instance is told twice that its database is replaced: asked again, it is done already)
    assert client.box.steps()[-4:] == ["replaced", "promote", "replaced", "follow"] and client.restarted == []
    assert any("the replica of acme-odoo of lab2 is promoted" in line for line in job["lines"])
    data = (await client.there.get("/api/postgres", headers=ADMIN)).json()
    row = row_of(data)
    # its own row, not the line Not here: replaced by the replica on lab1, with the way back
    assert row["where"] == "dedicated" and row["state"] == {"code": "replaced", "at": "2026-11-22T13:02:00", "link": "lab1"}
    assert row["pg"] == KEY and row["return"] is None and not any(r["server"] == "acme-odoo" for r in data["remote"])
    # nothing of it is tuned here
    assert (await client.there.get("/api/postgres/acme-odoo/production/proposal", headers=ADMIN)).status_code == 404
    jobs = await its_jobs(client)
    assert any(j["title"] == "The database of acme-odoo is on another machine of the provider now" for j in jobs)
    assert not any(name in json.dumps(jobs) for name in NAMES)
    # the copy is not dropped while the instance is there
    resp = await client.post(f"/api/postgres/replica/{COPY}/drop", headers=POST)
    assert resp.status_code == 400 and "dropped only after acme-odoo is removed on lab2" in resp.json()["error"]


# --- the return of the database of one instance (UX-011 §A, for one instance) ---------------------

async def test_the_return_of_an_instance_says_what_it_does_before_the_click(pair):
    client = pair
    # nothing to come back from, before a promotion
    assert (await client.there.get(FACTS, headers=ADMIN)).status_code == 404
    assert (await client.there.post(PREPARE, headers=THERE, json={})).status_code == 404
    await promoted(client)
    facts = (await client.there.get(FACTS, headers=ADMIN)).json()
    assert facts["missing"] and "A link from this machine to lab1 is needed" in facts["error"]
    assert (await client.there.post(PREPARE, headers=THERE, json={})).status_code == 400
    await link_back(client)
    facts = (await client.there.get(FACTS, headers=ADMIN)).json()
    assert facts["error"] is None and (facts["kind"], facts["name"], facts["workspace"]) == ("dedicated", "acme-odoo", "acme")
    assert facts["machine"] == "lab1" and facts["instances"] == ["acme-odoo"] and facts["size"] == 46 * MB
    assert facts["aside"] == {"at": "2026-11-22T13:02:00", "size": 50 * MB} and facts["fits"] and facts["disk_why"] == ""
    # the copy's machine is asked by the key this machine knows: it finds its own copy of it
    asked = [r.url.path for r in client.way.requests]
    assert asked == [f"/api/postgres/replica/{KEY}/return/facts"] and client.box.steps()[-1] == "follow"
    # no room on the workspace's disk: said whole, before the click, and refused
    client.box.free = 100 * MB
    facts = (await client.there.get(FACTS, headers=ADMIN)).json()
    assert not facts["fits"] and facts["disk_why"].startswith("No room on the disk of the workspace acme")
    resp = await client.there.post(PREPARE, headers=THERE, json={})
    assert resp.status_code == 507 and "No room on the disk of the workspace acme" in resp.json()["error"]
    client.box.free = 31 * GB
    client.way.down = True
    assert "lab1 does not answer" in (await client.there.get(FACTS, headers=ADMIN)).json()["error"]
    assert (await client.there.post(PREPARE, headers=THERE, json={})).status_code == 502
    assert not any(step.startswith(("return:", "copy-return:")) for step in client.box.steps())


async def test_the_return_of_an_instance_is_prepared_completed_and_the_customer_reads_no_machine(pair):
    client = pair
    await promoted(client)
    await link_back(client)
    before = len(client.box.calls)
    job = await done(client, await client.there.post(PREPARE, headers=THERE, json={}), there=True)
    # the other machine lets this one in first; then the data set aside, the copy, the start
    assert client.box.steps()[-4:] == ["copy-return:allow", "return:prepare", "return:copy", "return:start"]
    let_in, aside = client.box.inputs["let in"], client.box.inputs["set aside"]
    assert machinedb.VERIFIER.fullmatch(let_in["verifier"]) and (let_in["link"], let_in["by"]) == ("lab2", "admin")
    assert (aside["link"], aside["host"], aside["port"], aside["major"]) == ("lab1", "203.0.113.7", 15433, "17")
    assert aside["password"] not in json.dumps(job) and let_in["verifier"] not in json.dumps(job)
    lines = "\n".join(job["lines"])
    assert "the return of the database of acme-odoo from lab1" in lines and "on the disk of the workspace acme" in lines
    assert "acme-odoo keeps working on lab1: Complete the return… brings it back here" in lines
    # nothing of the instance stopped, and its row says the return without opening it
    assert client.restarted == [] and "back" not in "".join(c[0] for c in client.box.calls[before:] if "back" in c[0])
    row = row_of((await client.there.get("/api/postgres", headers=ADMIN)).json())
    assert row["state"]["code"] == "replaced" and row["return"]["here"]["state"] == "ready"
    assert row["return"]["there"]["verdict"]["code"] == "sync" and row["return"]["pending"] == ["acme-odoo"]
    copy = (await client.get("/api/postgres", headers=ADMIN)).json()["replicas"][0]["here"]
    assert copy["state"] == "promoted" and copy["return"]["state"] == "ready"
    # a second one is refused; the customer has read nothing of it
    assert (await client.there.post(PREPARE, headers=THERE, json={})).status_code == 400
    assert not any("return" in j["title"].lower() for j in await its_jobs(client))
    # completed: the name of the machine whose copy is stopped is typed
    resp = await client.there.post(COMPLETE, headers=THERE, json={"confirm": "acme-odoo"})
    assert resp.status_code == 400 and "Write lab1 to confirm the return" in resp.json()["error"]
    job = await done(client, await client.there.post(COMPLETE, headers=THERE, json={"confirm": "lab1"}), there=True)
    # the copy there is stopped first; this one takes the writes only once the last record arrived; then that one
    # instance is written back, and what was set aside goes
    assert client.box.steps()[-6:] == ["copy-return:stop", "return:wait", "return:promote", "copy-return:done", "back",
                                        "return:purge"]
    assert client.box.inputs["wait"] == {"checkpoint": "0/3000028"}
    lines = "\n".join(job["lines"])
    assert "acme-odoo is written again as an instance with a database of its own" in lines
    assert "acme-odoo answers again after 4.0 s, on its own database" in lines and "50 MB freed" in lines
    # an instance with a database of its own again, in the servers file too; no other instance restarted
    assert client.lab2.state.settings.servers["acme-odoo"].production.options.db_service == "db"
    assert client.restarted == []
    data = (await client.there.get("/api/postgres", headers=ADMIN)).json()
    row = row_of(data)
    assert row["state"]["code"] != "replaced" and row["replica"] is None and row["port"] == 15440
    assert row["return"]["here"]["state"] == "done" and row["return"]["here"]["machine"] == "lab1"
    # the copy on the other machine is stopped for good: a replica again, or dropped
    copy = (await client.get("/api/postgres", headers=ADMIN)).json()["replicas"][0]["here"]
    assert copy["state"] == "returned" and client.box.local["return"]["state"] == "done"
    # the customer: one job of its instance, no machine, no address, no port
    jobs = await its_jobs(client)
    note = next(j for j in jobs if j["title"] == "The database of acme-odoo is on this machine again")
    assert "Its backups are taken on this machine again" in note["lines"][-2] or "backups" in "\n".join(note["lines"])
    assert not any(name in json.dumps(jobs) for name in NAMES)
    # both access logs say it
    assert any("the database of acme-odoo came back from its promoted replica on lab1" in (e.get("detail") or "")
               for e in access_log(client.tmp, "lab2"))
    assert any("the promoted copy of the PostgreSQL of acme-odoo of lab2 is stopped for good" in (e.get("detail") or "")
               for e in access_log(client.tmp, "lab1"))
    # complete already
    assert (await client.there.post(COMPLETE, headers=THERE, json={"confirm": "lab1"})).status_code == 400


async def test_what_did_not_arrive_in_time_leaves_the_instance_where_it_is(pair):
    client = pair
    await promoted(client)
    await link_back(client)
    await done(client, await client.there.post(PREPARE, headers=THERE, json={}), there=True)
    client.box.arrives = False
    resp = await client.there.post(COMPLETE, headers=THERE, json={"confirm": "lab1"})
    job = await job_of(client.there, resp.json()["job"])
    assert job["status"] == "failed" and "the copy on lab1 runs again and acme-odoo stays there. Nothing is lost" in job["error"]
    assert client.box.steps()[-3:] == ["copy-return:stop", "return:wait", "copy-return:resume"]
    assert client.box.local["return"]["state"] == "ready" and client.box.read("replica.json")["state"] == "replaced"
    # with the other machine not answering there is no way: its copy is stopped first
    client.way.down = True
    resp = await client.there.post(COMPLETE, headers=THERE, json={"confirm": "lab1"})
    assert resp.status_code == 409 and "lab1 does not answer" in resp.json()["error"]


async def test_the_return_of_an_instance_is_cancelled_from_either_machine(pair):
    client = pair
    await promoted(client)
    await link_back(client)
    await done(client, await client.there.post(PREPARE, headers=THERE, json={}), there=True)
    job = await done(client, await client.there.post(CANCEL, headers=THERE), there=True)
    assert client.box.steps()[-2:] == ["return:cancel", "copy-return:cancel"]
    assert client.box.read("return.json") is None and client.box.local["return"]["state"] == "cancelled"
    assert "acme-odoo stays on lab1" in "\n".join(job["lines"])
    row = row_of((await client.there.get("/api/postgres", headers=ADMIN)).json())
    assert row["state"]["code"] == "replaced" and row["return"] is None
    assert (await client.there.post(CANCEL, headers=THERE)).status_code == 404
    # from the machine that hosts the copy: the other deletes what it made at its next look
    await done(client, await client.there.post(PREPARE, headers=THERE, json={}), there=True)
    await done(client, await client.post(f"/api/postgres/replica/{COPY}/return/cancel", headers=POST))
    assert client.box.read("return.json")["state"] == "ready"
    await client.lab2.state.return_look()
    for _ in range(100):
        if client.box.read("return.json") is None:
            break
        await __import__("asyncio").sleep(0.02)
    assert client.box.read("return.json") is None
    assert any("the return of the PostgreSQL of acme-odoo was cancelled on lab1" in (e.get("detail") or "")
               for e in access_log(client.tmp, "lab2"))


async def test_a_person_who_comes_through_another_panel_prepares_and_cancels_the_return(pair):
    """As test_a_person_who_comes_through_another_panel_is_an_admin_here: a
    third machine acting by itself is not let in to what only the copy's
    machine asks, and a person who reads lab2's pages through that machine's
    panel presses every button an admin who logs in on lab2 can."""
    client = pair
    await promoted(client)
    await link_back(client)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.lab2), base_url="http://lab2.test") as raw:
        assert (await raw.get(FACTS, headers=PERSON)).json()["error"] is None
        job = await done(client, await raw.post(PREPARE, headers=PERSON, json={}), there=True)
        assert "central" in job["source"] and client.box.read("return.json")["state"] == "ready"
        await done(client, await raw.post(CANCEL, headers=PERSON), there=True)
        assert client.box.read("return.json") is None
    # on the copy's machine: a machine acting by itself cancels its own return only, a person any
    await done(client, await client.there.post(PREPARE, headers=THERE, json={}), there=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.lab1), base_url="http://lab1.test") as raw:
        other = {"Authorization": LINK["Authorization"]}
        resp = await raw.post(f"/api/postgres/replica/{KEY}/return/stop", headers=other)
        assert resp.status_code in (401, 403)


# --- Close the port… of a database that still listens for a replica it no longer has -------------

async def test_the_port_of_a_dedicated_database_is_closed_once_it_has_no_replica(pair):
    client = pair
    # it listens on none: nothing to close
    resp = await client.there.post(CLOSE, headers=THERE)
    assert resp.status_code == 400 and "listens on no port of this machine" in resp.json()["error"]
    await copied(client)
    # a replica follows it through that port: refused before any job, with why
    resp = await client.there.post(CLOSE, headers=THERE)
    assert resp.status_code == 400 and "has a read replica on lab1, which follows it through this port" in resp.json()["error"]
    await done(client, await client.there.post(f"/api/postgres/machine/{KEY}/replica/drop", headers=THERE), there=True)
    row = row_of((await client.there.get("/api/postgres", headers=ADMIN)).json())
    assert row["port"] == 15440 and row["replica"] is None
    # a person through another panel closes it as an admin of the machine does
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.lab2), base_url="http://lab2.test") as raw:
        assert (await client.there.post(CLOSE, headers=MARIO_POST)).status_code == 403
        job = await done(client, await raw.post(CLOSE, headers=PERSON), there=True)
    assert client.box.steps()[-1] == "unlisten" and client.box.read("listen.json") is None
    lines = "\n".join(job["lines"])
    assert "The port 15440 is closed: the PostgreSQL of acme-odoo is as its compose.yaml says it again" in lines
    assert "its container was made again in 0.4 s" in lines
    row = row_of((await client.there.get("/api/postgres", headers=ADMIN)).json())
    assert row["port"] is None
    # the customer reads the restart of its PostgreSQL, and no machine, no port
    jobs = await its_jobs(client)
    note = next(j for j in jobs if j["title"] == "The PostgreSQL of acme-odoo was restarted once by the provider")
    assert "restarted once (0.4 s)" in "\n".join(note["lines"]) and not any(name in json.dumps(jobs) for name in NAMES)
    # the cluster's own Close the port… is the cluster's still
    assert (await client.there.post("/api/postgres/machine/18/port/close", headers=THERE)).status_code == 404


async def test_the_stopped_copy_of_an_instance_is_made_a_replica_again(pair):
    client = pair
    await promoted(client)
    await link_back(client)
    await done(client, await client.there.post(PREPARE, headers=THERE, json={}), there=True)
    await done(client, await client.there.post(COMPLETE, headers=THERE, json={"confirm": "lab1"}), there=True)
    said = (await client.get(f"/api/links/lab2/postgres/replica?again={KEY}&which={KEY}", headers=ADMIN)).json()
    # the stopped copy goes first: its room is free for the new one; the database listens still: nothing restarts
    assert said["error"] is None and said["stopped"] == 60 * MB and said["restarts"] is False and said["kind"] == "dedicated"
    job = await done(client, await client.post(f"/api/postgres/replica/{COPY}/remake", headers=POST, json={"reserve_gb": 2}))
    assert client.box.steps()[-6:] == ["remove", "allow", "return:forget", "make:prepare", "make:copy", "make:start"]
    assert client.box.inputs["allow"]["restart"] is False and client.box.inputs["prepare"]["source"]["key"] == KEY
    assert job["title"] == "Make the read replica of the PostgreSQL of acme-odoo of lab2 again"
    assert client.box.local["state"] == "ready" and client.box.read("replica.json")["state"] == "ready"
    # the note of the return goes with the copy it came from
    assert client.box.read("return.json") is None
    # a copy that is not one the instance came back from is not made again
    resp = await client.post(f"/api/postgres/replica/{COPY}/remake", headers=POST, json={})
    assert resp.status_code == 400 and "is not one lab2 came back from" in resp.json()["error"]


async def test_the_space_of_before_is_not_freed_under_a_return(pair):
    client = pair
    await promoted(client)
    await link_back(client)
    # what the instance had here is still beside it: Free the space of before… frees it
    (client.box.replica.parent / "data" / "pgdata").mkdir(parents=True)
    purge = "/api/servers/acme-odoo/database/purge"
    row = row_of((await client.there.get("/api/postgres", headers=ADMIN)).json())
    assert row["kept"] is True
    await done(client, await client.there.post(PREPARE, headers=THERE, json={}), there=True)
    # under a return that folder is the copy that follows the promoted one: not offered, and refused
    row = row_of((await client.there.get("/api/postgres", headers=ADMIN)).json())
    assert row["kept"] is False
    resp = await client.there.post(purge, headers=THERE)
    assert resp.status_code == 400 and "keeps nothing of a move" in resp.json()["error"]
    assert "machine-db-purge" not in [c[0] for c in client.box.calls]
    await done(client, await client.there.post(CANCEL, headers=THERE), there=True)
    await done(client, await client.there.post(purge, headers=THERE), there=True)


async def test_an_instance_removed_on_its_promoted_copy_says_no_address_to_the_customer(pair):
    client = pair
    await promoted(client)
    job = await done(client, await client.there.post("/api/workspaces/acme/instances/delete", headers=THERE,
                                                     json={"server": "acme-odoo"}), there=True)
    lines = "\n".join(job["lines"])
    # the copy is the provider's: where it is is not said in a job the workspace's users read
    assert "Its database stays on its copy on another machine of the provider, which drops it there" in lines
    assert "203.0.113" not in lines and "15433" not in lines
    read = json.dumps(await its_jobs(client))
    assert not any(name in read for name in NAMES)
    # the machine that hosts the copy drops it once the instance is gone
    await client.lab1.state.replica_look_hosted()
    assert (await client.get("/api/postgres", headers=ADMIN)).json()["replicas"][0]["orphan"] is True
    await done(client, await client.post(f"/api/postgres/replica/{COPY}/drop", headers=POST))
    assert client.box.local is None


def test_the_reason_a_job_failed_for_is_said_in_the_readers_language():
    line = ("15:52:05 FAILED: workspace-helper: the database of tmp comes back from its promoted copy: the data of before "
            "is set aside for the return, and is freed when it is complete or cancelled")
    assert i18n.tr("it", line).startswith("15:52:05 FALLITO: workspace-helper: il database di tmp sta tornando dalla sua copia")
    # a short reason, as before
    assert i18n.tr("it", "FAILED: workspace-helper: docker is not installed").startswith("FALLITO: workspace-helper: docker non")
    # a sentence inside another line: the reason, and the line that carries it
    said = i18n.tr("it", "FAILED: not everything lab3 wrote arrived here within 60 s: the copy on lab3 runs again and "
                         "acme-tmp stays there. Nothing is lost")
    assert said == ("FALLITO: non tutto quello che lab3 ha scritto è arrivato qui entro 60 s: la copia su lab3 riparte e "
                    "acme-tmp resta là. Non si perde niente")
    # a name of several words is not a sentence: it stays as it is
    assert i18n.tr("it", "FAILED: the server of the north") == "FALLITO: the server of the north"
