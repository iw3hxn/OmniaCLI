# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The handover of a workspace to the customer's own OmniaCLI (AD-008…AD-011, UX-014 part A; step 7 of §9 of
docs/adr/AD-008-consegna-e-spostamento.md, up to «ready»): the code (one line, 72 hours, one machine, its hash only
kept, the pin of the provider's certificate), the provider's routes (Prepare, Redo, Cancel) and the public ones the
customer's machine calls with the code and then its session, the customer's side (read the code, cancel the
reception, told when the provider cancels), the pages."""

import asyncio
import dataclasses
import json
import os
import re
import ssl
import subprocess
from pathlib import Path

import httpx
import pytest

from omniacli import handover, machinedb, treesync, workspaceops
from omniacli.jobs import SUCCESS, JobFailed

from .conftest import BasicAsSession
from .test_app import AUTH, POST, wait_job  # noqa: F401
from .test_staging_dialog import acme, two  # noqa: F401 - the fixtures
from .test_treesync import InProcess
from .test_workspaces import post_as, workspaces  # noqa: F401 - the workspaces fixture

PIN = "ab" * 32
URL = "https://panel.example.net"


# --- the code and the records ----------------------------------------------------------------------------------

def test_the_code_carries_the_address_the_id_the_secret_and_the_pin_only():
    secret = handover.new_secret()
    code = handover.make_code(URL, "0123456789abcdef", secret, PIN)
    assert code.startswith("omniacli-consegna-1.") and "\n" not in code and len(code) < 300
    assert handover.read_code(f"  {code}\n") == {"u": URL, "h": "0123456789abcdef", "s": secret, "p": PIN}
    # whatever is wrong in it is «not valid»: another prefix, HTTP, a path or a user in the address, a part more
    for bad in ("omniacli-consegna-2.x", "nothing", handover.make_code("http://panel.example.net", "0123456789abcdef", secret, PIN),
                handover.make_code(URL + "/x", "0123456789abcdef", secret, PIN),
                handover.make_code("https://u:p@panel.example.net", "0123456789abcdef", secret, PIN),
                handover.make_code(URL, "0123", secret, PIN), handover.make_code(URL, "0123456789abcdef", "short", PIN),
                handover.make_code(URL, "0123456789abcdef", secret, "zz"), handover.PREFIX + "e30"):
        with pytest.raises(handover.CodeError):
            handover.read_code(bad)
    # 256 bits of secret, and only their SHA-256 kept
    assert len(handover._unb64(secret)) == 32
    assert handover.same(handover.hashed(secret), secret) and not handover.same(handover.hashed(secret), secret + "x")
    assert not handover.same("", secret)


def test_the_pin_is_the_key_of_the_certificate(tmp_path):
    """p pins the public key (SPKI) of the provider's certificate: a renewal with a new key changes it."""
    def cert(name):
        subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
                        "-keyout", str(tmp_path / f"{name}.key"), "-out", str(tmp_path / f"{name}.pem"), "-days", "1",
                        "-subj", "/CN=panel.example.net"], check=True, capture_output=True)
        return ssl.PEM_cert_to_DER_cert((tmp_path / f"{name}.pem").read_text())
    one, other = cert("one"), cert("other")
    assert re.fullmatch(r"[0-9a-f]{64}", handover.spki_pin(one)) and handover.spki_pin(one) != handover.spki_pin(other)


def test_the_records_are_private_and_hold_no_secret(tmp_path):
    out = handover.Out(tmp_path)
    out.save("acme", {"id": "0123456789abcdef", "code_hash": handover.hashed("s")})
    path = tmp_path / "handover" / "out" / "acme" / "state.json"
    assert oct(path.stat().st_mode & 0o777) == "0o600" and oct(path.parent.stat().st_mode & 0o777) == "0o700"
    assert out.find("0123456789abcdef")[0] == "acme" and out.find("../etc") is None
    inbox = handover.In(tmp_path)
    inbox.put_secret("0123456789abcdef", "claim.key", b"k")
    assert oct((tmp_path / "handover" / "in" / "0123456789abcdef" / "claim.key").stat().st_mode & 0o777) == "0o600"
    with pytest.raises(handover.CodeError):
        inbox.folder("../../etc")
    inbox.forget("0123456789abcdef")
    assert not (tmp_path / "handover" / "in" / "0123456789abcdef").exists()


def test_a_code_expires_and_ten_wrong_tries_are_enough():
    assert handover.phase_of({"phase": "prepared", "expires": handover.now() + 10}) == "prepared"
    assert handover.phase_of({"phase": "ready", "expires": handover.now() - 1}) == "expired"
    assert handover.phase_of({"phase": "cancelled", "expires": handover.now() + 10}) == "cancelled"
    tries = handover.Tries()
    for _ in range(handover.FAILED_MOST - 1):
        tries.failed("203.0.113.40", 1000.0)
    assert not tries.blocked("203.0.113.40", 1001.0)
    tries.failed("203.0.113.40", 1001.0)
    assert tries.blocked("203.0.113.40", 1002.0) and not tries.blocked("203.0.113.41", 1002.0)
    # 10 minutes later the address may try again
    assert not tries.blocked("203.0.113.40", 1001.0 + handover.FAILED_WINDOW + 1)


def test_the_names_that_arrive():
    assert handover.received_name("acme", {"beta"}) == "acme"
    assert handover.received_name("acme", {"acme", "acme-2"}) == "acme-3"
    # a user name the customer has already arrives as <name>.<workspace> (Carlo, 23/11/2026)
    assert handover.user_name("mario", "acme", {"mario"}) == "mario.acme"
    assert handover.user_name("anna", "acme", {"mario"}) == "anna"


async def test_the_backups_of_a_handover_are_the_last_one_only(tmp_path):
    """Of an environment's backups the handover passes its last one (AD-009, D-A7): the end of the tree lists that
    one and refuses the others."""
    from omniacli import treesync
    for name in ("old.zip", "new.zip"):
        (tmp_path / name).write_bytes(name.encode())
    end = treesync.LocalFiles(tmp_path, only=["new.zip"])
    listing = tmp_path.parent / f"{tmp_path.name}.ndjson"
    counts = await end.listing(listing)
    assert counts == {"files": 1, "bytes": 7} and [e["p"] for e in treesync.read_lines(listing)] == ["new.zip"]
    with pytest.raises(treesync.TreeError):
        async with end.fetch("old.zip", 0):
            pass


def test_the_purge_takes_the_handovers_away():
    from pathlib import Path
    purge = (Path(__file__).parent.parent / "packaging" / "debian" / "files" / "postrm").read_text()
    assert "rm -rf /var/lib/omniacli/handover" in purge


# --- the provider's side ----------------------------------------------------------------------------------------

MB = 1 << 20


class Root:
    """The root helpers both machines ask in a handover (one module, two applications), faked; any other command
    goes to the real one (which sudo refuses in the tests). The trees of root are folders of this process
    (InProcess), one per workspace, instance, role and tree."""

    def __init__(self, tmp_path, monkeypatch):
        self.calls, self.tmp, self.arrive, self.projects, self.allowed = [], tmp_path, {}, {}, []
        self.lag, self.trees = 0.4, {}
        self.original = workspaceops._helper
        monkeypatch.setattr(workspaceops, "_helper", self.helper)
        monkeypatch.setattr(workspaceops, "ask", lambda *args, timeout=None, input=None: self.helper(
            None, *args, timeout=timeout, input=input))
        monkeypatch.setattr(treesync, "LocalTree", self.tree)

    def tree(self, helper, ws, storage, instance, role, tree="filestore"):
        key = (ws, instance, role, tree)
        if key not in self.trees:
            self.trees[key] = InProcess(self.tmp / "trees" / ws / instance / role / tree)
        return self.trees[key]

    def asked(self, name):
        return [args for args in self.calls if args[0] == name]

    async def helper(self, job, *args, timeout=None, input=None):
        self.calls.append(args)
        name = args[0]
        folder = Path(self.projects[args[1]]) / machinedb.DEDICATED_FOLDER if args[1:] and args[1] in self.projects \
            else None
        if name == "machine-db-replica-listen":
            folder.mkdir(parents=True, exist_ok=True)
            was = machinedb.listening(folder)
            (folder / machinedb.LISTEN_FILE).write_text(json.dumps({"port": 15433, "bridge": ""}))
            return {"port": 15433, "restarted_s": None if was else 2.5}
        if name == "machine-db-replica-allow":
            spec = json.loads(input)
            self.allowed.append(spec)
            if machinedb.listening(folder) is None and not spec.get("restart"):
                raise JobFailed("workspace-helper: the PostgreSQL of acme-odoo does not listen for the copy")
            (folder / machinedb.REPLICA_FILE).write_text(json.dumps({"link": spec["link"], "address": spec["address"],
                                                                      "state": "copying"}))
            return {"port": 15433, "restarted_s": None, "ca": "CA", "size": 60 * MB, "major": "17",
                    "address": spec["address"]}
        if name == "machine-db-replica-facts":
            return {"size": 60 * MB, "major": "17", "database": "odoo"}
        if name == "machine-db-replica-state":
            return {"connected": True, "lag": self.lag, "state": "streaming"}
        if name == "machine-db-replica-drop":
            (folder / machinedb.REPLICA_FILE).unlink(missing_ok=True)
            return {"dropped": True}
        if name == "machine-db-arrive":
            key, op = args[1], args[2]
            if op == "state":
                return dict(self.arrive.get(key) or {"state": ""})
            if op == "prepare":
                self.arrive[key] = {"state": "copying", **json.loads(input)}
                return {}
            if op == "copy":
                return {"size": 60 * MB, "seconds": 1.1}
            if op == "start":
                self.arrive[key]["state"] = "ready"
                return {"seconds": 0.7}
            self.arrive.pop(key, None)
            return {"cancelled": True}
        if name in ("disk", "limits"):
            return {}
        if name == "create":
            ws, storage, instance = args[1:4]
            project = self.tmp / "far-disk" / ws / instance / "production"
            project.mkdir(parents=True, exist_ok=True)
            self.projects[f"{ws}.{storage}.{instance}"] = project
            return {"database": instance, "odoo_service": "odoo", "db_service": "db",
                    "roles": {"production": {"http_port": 21000, "folder": str(project)}}}
        if name == "remove":
            return {"workspace": args[1], "instance": args[3]}
        return await self.original(job, *args, timeout=timeout, input=input)


@pytest.fixture
def provider(two, env, monkeypatch, tmp_path):
    """two's machine as the provider: a portal with a certificate (its pin read as PIN), acme-odoo without its test
    environment (an instance with a test passes in a later step), with a database of its own that does not listen
    yet; the root helpers of both machines faked (two.root)."""
    settings = env["settings"]
    settings.options = dataclasses.replace(settings.options, portal_address="panel.example.net",
                                           provider_label="Studio Rossi")
    monkeypatch.setattr(handover, "presented_pin", lambda url, timeout=10.0: PIN)
    settings.servers["acme-odoo"].instances.pop("test", None)
    two.root = Root(tmp_path, monkeypatch)
    two.root.projects["acme.main.odoo"] = settings.servers["acme-odoo"].production.restart.service
    return two


async def prepare(client) -> str:
    resp = await client.post("/api/workspaces/acme/handover", headers=POST,
                             json={"confirm": "acme", "instances": ["acme-odoo"]})
    assert resp.status_code == 200, resp.text
    return resp.json()["code"]


async def test_prepare_is_the_machines_admins_only_and_wants_the_portal_and_the_name(two, env):
    for user in ("mario", "luca"):
        assert (await two.get("/api/workspaces/acme/handover", headers=post_as(user))).status_code == 403
        assert (await two.post("/api/workspaces/acme/handover", headers=post_as(user),
                               json={"confirm": "acme", "instances": ["acme-odoo"]})).status_code == 403
    # no address of this panel the customer's machine opens: said before, Prepare refused
    view = (await two.get("/api/workspaces/acme/handover", headers={"Authorization": AUTH, "Accept-Language": "it"})).json()
    assert view["url_why"].startswith("La macchina del cliente deve raggiungere questo pannello in HTTPS")
    resp = await two.post("/api/workspaces/acme/handover", headers=POST, json={"confirm": "acme", "instances": ["acme-odoo"]})
    assert resp.status_code == 400 and "must reach this panel in HTTPS" in resp.json()["error"]
    # an instance with a test environment, said why, in the reader's language
    rows = {r["server"]: r for r in view["rows"]}
    assert "ambiente di test" in rows["acme-odoo"]["why"]
    assert not handover.Out(env["settings"].jobs_dir.parent).all()


async def test_the_code_is_shown_once_and_only_its_hash_stays(provider, env):
    view = (await provider.get("/api/workspaces/acme/handover", headers={"Authorization": AUTH})).json()
    row = {r["server"]: r for r in view["rows"]}["acme-odoo"]
    assert row["why"] == "" and row["kind"] in ("dedicated", "cluster") and view["url_why"] == ""
    assert view["copy_days"] == 7 and view["people"]["users"] >= 1
    # without the name written, nothing
    resp = await provider.post("/api/workspaces/acme/handover", headers=POST, json={"confirm": "acm", "instances": ["acme-odoo"]})
    assert resp.status_code == 400 and not handover.Out(env["settings"].jobs_dir.parent).all()
    code = await prepare(provider)
    parts = handover.read_code(code)
    assert parts["u"] == URL and parts["p"] == PIN
    stored = (env["settings"].jobs_dir.parent / "handover" / "out" / "acme" / "state.json").read_text()
    assert parts["s"] not in stored and handover.hashed(parts["s"]) in stored
    # one handover at a time
    resp = await provider.post("/api/workspaces/acme/handover", headers=POST, json={"confirm": "acme", "instances": ["acme-odoo"]})
    assert resp.status_code == 400 and "prepared already" in resp.json()["error"]
    # the page of the workspace says it, with its two buttons; the access log has it
    page = (await provider.get("/workspaces/acme", headers={"Authorization": AUTH})).text
    assert 'id="handover-band" data-phase="prepared"' in page and 'id="handover-redo"' in page and 'id="handover-cancel"' in page


async def test_the_offer_and_the_claim_with_the_code(provider, env):
    code = await prepare(provider)
    parts = handover.read_code(code)
    offer = await provider.get("/api/handover/v1/offer", headers={handover.CODE_HEADER: code})
    assert offer.status_code == 200, offer.text
    data = offer.json()
    assert data["provider"] == "Studio Rossi" and data["workspace"]["name"] == "acme" and not data["claimed"]
    assert [i["server"] for i in data["items"]] == ["acme-odoo"]
    # nothing of this machine's in what the customer's machine reads: no folder, no address
    text = json.dumps(data)
    assert "/srv" not in text and "/var/lib" not in text and str(env["settings"].jobs_dir.parent) not in text
    # a wrong secret and a wrong id: the same 401
    wrong = handover.make_code(URL, parts["h"], handover.new_secret(), PIN)
    other = handover.make_code(URL, "0123456789abcdef", parts["s"], PIN)
    said = [(await provider.get("/api/handover/v1/offer", headers={handover.CODE_HEADER: c})) for c in (wrong, other)]
    assert [r.status_code for r in said] == [401, 401] and said[0].json() == said[1].json() == {"error": "The code is not valid"}
    # the first claim binds it to a key and gives a session; another key, 409; the same key, a new session
    _raw, key = handover.new_key()
    _raw2, key2 = handover.new_key()
    first = await provider.post("/api/handover/v1/claim", json={"code": code, "key": key})
    assert first.status_code == 200, first.text
    session = first.json()["session"]
    assert re.fullmatch(r"[0-9a-f]{4}( [0-9a-f]{4}){3}", first.json()["fingerprint"])
    again = await provider.post("/api/handover/v1/claim", json={"code": code, "key": key2})
    assert again.status_code == 409 and "another machine" in again.json()["error"]
    second = await provider.post("/api/handover/v1/claim", json={"code": code, "key": key})
    assert second.status_code == 200 and second.json()["session"] != session
    auth = {"Authorization": f"Bearer {session}"}
    assert (await provider.get(f"/api/handover/v1/{parts['h']}/manifest", headers=auth)).status_code == 401
    auth = {"Authorization": f"Bearer {second.json()['session']}"}
    manifest = (await provider.get(f"/api/handover/v1/{parts['h']}/manifest", headers=auth)).json()
    assert manifest["format"] == 1 and manifest["requirements"]["protocol"] == handover.PROTOCOL
    assert {n["what"] for n in manifest["not_passed"]} >= {"stagings", "passkeys", "tokens"}
    # the trees of the handover: the instance chosen, its production only
    for spec in ({"server": "acme-odoo", "role": "test", "tree": "filestore"}, {"server": "beta-x", "role": "production", "tree": "filestore"},
                 {"server": "acme-odoo", "role": "production", "tree": "etc"}):
        resp = await provider.post(f"/api/handover/v1/{parts['h']}/treesync/list", headers=auth, json=spec)
        assert resp.status_code == 404, (spec, resp.text)
    # the provider's state: receiving, by that machine
    state = handover.Out(env["settings"].jobs_dir.parent).state("acme")
    assert state["phase"] == "receiving" and state["claim"]["key"] == key and session not in json.dumps(state)


async def test_ten_wrong_codes_from_one_address_are_429(provider):
    await prepare(provider)
    for _ in range(handover.FAILED_MOST):
        await provider.get("/api/handover/v1/offer", headers={handover.CODE_HEADER: "omniacli-consegna-1.bad"})
    resp = await provider.get("/api/handover/v1/offer", headers={handover.CODE_HEADER: "omniacli-consegna-1.bad"})
    assert resp.status_code == 429


async def test_redo_cancel_and_expiry_say_why_to_the_right_secret_only(provider, env):
    code = await prepare(provider)
    hid = handover.read_code(code)["h"]
    _raw, key = handover.new_key()
    session = (await provider.post("/api/handover/v1/claim", json={"code": code, "key": key})).json()["session"]
    # Redo: the code of before no longer holds, nor its session (they read why: made anew); the new one is claimed
    # by any machine
    redo = await provider.post("/api/workspaces/acme/handover/code", headers=POST)
    assert redo.status_code == 200
    new = redo.json()["code"]
    assert handover.read_code(new)["h"] == hid
    for got in (await provider.get("/api/handover/v1/offer", headers={handover.CODE_HEADER: code}),
                await provider.get(f"/api/handover/v1/{hid}/state", headers={"Authorization": f"Bearer {session}"}),
                await provider.get(f"/api/handover/v1/{hid}/manifest", headers={"Authorization": f"Bearer {session}"})):
        assert got.status_code == 410 and got.json()["phase"] == "redone", got.text
    offer = (await provider.get("/api/handover/v1/offer", headers={handover.CODE_HEADER: new})).json()
    assert offer["claimed"] is False
    _raw2, key2 = handover.new_key()
    session = (await provider.post("/api/handover/v1/claim", json={"code": new, "key": key2})).json()["session"]
    # cancelled: its code and its session read why (410, the phase), and reach nothing else
    resp = await provider.post("/api/workspaces/acme/handover/cancel", headers=POST)
    assert (await wait_job(provider, resp.json()["job"]))["status"] == SUCCESS
    for got in (await provider.get("/api/handover/v1/offer", headers={handover.CODE_HEADER: new}),
                await provider.get(f"/api/handover/v1/{hid}/state", headers={"Authorization": f"Bearer {session}"}),
                await provider.get(f"/api/handover/v1/{hid}/manifest", headers={"Authorization": f"Bearer {session}"})):
        assert got.status_code == 410 and got.json()["phase"] == "cancelled", got.text
    italian = await provider.get("/api/handover/v1/offer", headers={handover.CODE_HEADER: new, "Accept-Language": "it"})
    assert italian.json()["error"] == "La consegna è stata annullata dal fornitore: chiedi un codice nuovo"
    page = (await provider.get("/workspaces/acme", headers={"Authorization": AUTH})).text
    assert 'id="handover-band"' not in page
    # a new handover after it, and its expiry
    code = await prepare(provider)
    out = handover.Out(env["settings"].jobs_dir.parent)
    out.update("acme", expires=handover.now() - 60)
    got = await provider.get("/api/handover/v1/offer", headers={handover.CODE_HEADER: code})
    assert got.status_code == 410 and got.json()["phase"] == "expired" and "expired on" in got.json()["error"]
    _raw3, key3 = handover.new_key()
    assert (await provider.post("/api/handover/v1/claim", json={"code": code, "key": key3})).status_code == 410
    page = (await provider.get("/workspaces/acme", headers={"Authorization": AUTH})).text
    assert 'data-phase="expired"' in page and 'id="handover-redo"' in page
    # an expired one is not prepared over (its customer's machine still holds its copy), nor its workspace deleted:
    # it is cancelled, and its customer's machine reads why
    resp = await provider.post("/api/workspaces/acme/handover", headers=POST, json={"confirm": "acme", "instances": ["acme-odoo"]})
    assert resp.status_code == 400 and "prepared already" in resp.json()["error"]
    resp = await provider.post("/api/workspaces/delete", headers=POST, json={"name": "acme"})
    assert resp.status_code == 400 and "Cancel the handover" in resp.json()["error"]
    resp = await provider.post("/api/workspaces/acme/handover/cancel", headers=POST)
    assert resp.status_code == 200 and (await wait_job(provider, resp.json()["job"]))["status"] == SUCCESS
    assert (await provider.get("/api/handover/v1/offer", headers={handover.CODE_HEADER: code})).json()["phase"] == "cancelled"


async def test_after_redo_the_old_session_reads_made_anew_and_the_new_code_is_still_let_in(provider, env):
    """The customer's machine that copied keeps asking every minute with its session of before: after Redo it reads
    why (410 «redone») and is never counted as a failed try, so the new code pasted hours later still passes from
    the same address (it went 429 after 10 looks)."""
    code = await prepare(provider)
    hid = handover.read_code(code)["h"]
    _raw, key = handover.new_key()
    session = (await provider.post("/api/handover/v1/claim", json={"code": code, "key": key})).json()["session"]
    new = (await provider.post("/api/workspaces/acme/handover/code", headers=POST)).json()["code"]
    for _ in range(handover.FAILED_MOST + 5):
        got = await provider.get(f"/api/handover/v1/{hid}/state", headers={"Authorization": f"Bearer {session}"})
        assert got.status_code == 410 and got.json()["phase"] == "redone", got.text
        # Retry with the code of before: the same why
        old = await provider.post("/api/handover/v1/claim", json={"code": code, "key": key})
        assert old.status_code == 410 and old.json()["phase"] == "redone"
    assert (await provider.get("/api/handover/v1/offer", headers={handover.CODE_HEADER: new})).status_code == 200
    claimed = await provider.post("/api/handover/v1/claim", json={"code": new, "key": key})
    assert claimed.status_code == 200, claimed.text
    # a second Redo retires that one too, and a cancel then reads «cancelled» to every session of before
    await provider.post("/api/workspaces/acme/handover/code", headers=POST)
    resp = await provider.post("/api/workspaces/acme/handover/cancel", headers=POST)
    assert (await wait_job(provider, resp.json()["job"]))["status"] == SUCCESS
    for old in (session, claimed.json()["session"]):
        got = await provider.get(f"/api/handover/v1/{hid}/state", headers={"Authorization": f"Bearer {old}"})
        assert got.status_code == 410 and got.json()["phase"] == "cancelled"
    # a session nobody made is still a failed try
    for _ in range(handover.FAILED_MOST):
        await provider.get(f"/api/handover/v1/{hid}/state", headers={"Authorization": "Bearer nothing"})
    assert (await provider.get("/api/handover/v1/offer", headers={handover.CODE_HEADER: new})).status_code == 429


async def test_an_instance_in_a_handover_under_way_is_neither_moved_nor_deleted_nor_renamed(provider, env):
    """After Prepare the customer copies the instance as it is: nothing moves it to another machine, deletes it,
    renames it, gives it a test or moves its database until the handover is cancelled (an expired one too)."""
    from omniacli.config import LinkTarget
    settings = env["settings"]
    settings.link_targets = {"lab9": LinkTarget("lab9", "https://lab9.example.net", "t")}
    await prepare(provider)
    said = "is in the handover of acme under way"

    async def refusals():
        return [await provider.post("/api/workspaces/acme/instances/delete", headers=POST, json={"server": "acme-odoo"}),
                await provider.post("/api/servers/delete", headers=POST, json={"name": "acme-odoo"}),
                await provider.post("/api/servers/save", headers=POST,
                                    json={"original": "acme-odoo", "name": "acme-erp", "server": {}}),
                await provider.post("/api/servers/acme-odoo/move", headers=POST, json={"link": "lab9"}),
                await provider.post("/api/servers/acme-odoo/database/move", headers=POST, json={"to": "machine"})]
    for answer in await refusals():
        assert answer.status_code == 400 and said in answer.json()["error"], answer.text
    plan = (await provider.get("/api/servers/acme-odoo/move", headers={"Authorization": AUTH})).json()
    assert any(said in why for why in plan["refusals"])
    # expired and not cancelled: the customer's machine still holds its copy
    handover.Out(settings.jobs_dir.parent).update("acme", expires=handover.now() - 60)
    assert all(said in a.json().get("error", "") for a in await refusals())
    resp = await provider.post("/api/workspaces/acme/handover/cancel", headers=POST)
    assert (await wait_job(provider, resp.json()["job"]))["status"] == SUCCESS
    plan = (await provider.get("/api/servers/acme-odoo/move", headers={"Authorization": AUTH})).json()
    assert not any(said in why for why in plan["refusals"])


# --- the customer's side --------------------------------------------------------------------------------------

@pytest.fixture
def customer(provider, monkeypatch):
    """far as the customer's own OmniaCLI: it calls the provider (two's app) through its handover client, the pin
    checked as true."""
    monkeypatch.setattr(handover, "check_pin", lambda url, pin, timeout=10.0: None)
    app = provider._transport.app
    monkeypatch.setattr(handover, "client", lambda url, pin, timeout=None: httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=url))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=BasicAsSession(provider.far)), base_url="http://far.test")


async def test_the_customer_reads_the_code_and_nothing_changes(customer, provider, env):
    code = await prepare(provider)
    async with customer as c:
        page = (await c.get("/workspaces", headers={"Authorization": AUTH})).text
        # Recover a workspace… beside the primary New workspace, not a primary itself, with its help
        tag = re.search(r'<button type="button" id="receive-open"[^>]*>', page).group(0)
        assert 'class="primary' not in tag and re.search(r'title="[^"]{20,}"', tag)
        dialog = page[page.index('<dialog id="receive-dialog"'):]
        dialog = dialog[:dialog.index("</dialog>")]
        assert 'class="icon-btn dialog-x"' in dialog
        for button in re.findall(r"<button\b[^>]*>", dialog):
            assert re.search(r'title="[^"]+"', button), button
        resp = await c.post("/api/handover/read", headers={"Authorization": AUTH, "Origin": "http://far.test"},
                            json={"code": code})
        assert resp.status_code == 200, resp.text
        read = resp.json()
        assert read["provider"] == "Studio Rossi" and read["workspace"]["name"] == "acme" and read["resume"] is False
        assert read["checks"]["name"] == "acme" and [i["instance"] for i in read["items"]] == ["odoo"]
        # a wrong code: refused, said in the reader's language
        bad = await c.post("/api/handover/read", headers={"Authorization": AUTH, "Origin": "http://far.test",
                                                           "Accept-Language": "it"}, json={"code": "nothing"})
        assert bad.status_code == 400 and bad.json()["error"] == "Il codice non è valido"
    # reading claims nothing there
    assert handover.Out(env["settings"].jobs_dir.parent).state("acme")["phase"] == "prepared"


async def test_a_cancel_from_the_customer_tells_the_provider(customer, provider, env):
    code = await prepare(provider)
    parts = handover.read_code(code)
    _raw, key = handover.new_key()
    session = (await provider.post("/api/handover/v1/claim", json={"code": code, "key": key})).json()["session"]
    inbox = handover.In(provider.far_settings.jobs_dir.parent)
    inbox.save(parts["h"], {"id": parts["h"], "phase": "ready", "workspace": "acme", "provider": "Studio Rossi"})
    inbox.put_secret(parts["h"], "code", code.encode())
    inbox.put_secret(parts["h"], "session", session.encode())
    async with customer as c:
        page = (await c.get("/api/handover/" + parts["h"], headers={"Authorization": AUTH})).json()
        assert page["phase"] == "ready" and "session" not in json.dumps(page)
        resp = await c.post(f"/api/handover/{parts['h']}/cancel", headers={"Authorization": AUTH, "Origin": "http://far.test"})
        assert resp.status_code == 200, resp.text
        job = await wait_job(c, resp.json()["job"])
        assert job["status"] == SUCCESS, job["lines"]
        assert any("Studio Rossi told" in line for line in job["lines"])
    assert not (provider.far_settings.jobs_dir.parent / "handover" / "in" / parts["h"]).exists()
    assert handover.Out(env["settings"].jobs_dir.parent).state("acme")["phase"] == "cancelled"


# --- the reception, end to end (the provider's app, the customer's app, root's helpers faked) -----------------------

def provider_trees(provider, env):
    """What acme-odoo has at the provider's: its code with a history, its filestore, two backups (the older one
    must not pass)."""
    root = provider.root
    addons = root.tree(None, "acme", "main", "odoo", "production", "addons").top
    (addons / "custom" / ".git").mkdir(parents=True, exist_ok=True)
    (addons / "custom" / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (addons / "custom" / "module.py").write_text("print('hello')\n")
    filestore = root.tree(None, "acme", "main", "odoo", "production", "filestore").top
    (filestore / "ab").mkdir(parents=True, exist_ok=True)
    (filestore / "ab" / "ab01").write_bytes(os.urandom(50_000))
    folder = env["settings"].backup_dir / "acme-odoo" / "production"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "old.zip").write_bytes(b"PK-old")
    os.utime(folder / "old.zip", (1_000_000, 1_000_000))
    (folder / "new.zip").write_bytes(b"PK-new-backup")
    return filestore


async def receive(c, code, name="acme-in"):
    resp = await c.post("/api/handover/receive", headers={"Authorization": AUTH, "Origin": "http://far.test"},
                        json={"code": code, "name": name})
    assert resp.status_code == 200, resp.text
    return resp.json()["id"], await wait_job(c, resp.json()["job"])


async def test_receive_copies_every_tree_and_follows_the_database_without_restarting_the_providers(customer, provider,
                                                                                                    env):
    """Receive end to end: the claim, the workspace and its instance made never started, the code, the filestore and
    the last backup pulled over the handover's routes (old.zip refused there), the database of its own let in for
    the address the customer called from and followed; ready on both sides. The provider's database restarted at
    Prepare, on its click, and not at Receive."""
    root = provider.root
    provider_trees(provider, env)
    code = await prepare(provider)
    hid = handover.read_code(code)["h"]
    listened = root.asked("machine-db-replica-listen")
    assert len(listened) == 1 and listened[0][1] == "acme.main.odoo"
    prepared = [j for j in provider._transport.app.app.state.jobs.recent(20) if "opened for the copy" in j.title]
    assert prepared and any("2.5 s without answers" in line for line in prepared[0].lines)
    # the dialog says it before: a database of its own that does not listen restarts at Prepare (and no more after)
    async with customer as c:
        rid, job = await receive(c, code)
        assert rid == hid and job["status"] == SUCCESS, job["lines"]
        text = "\n".join(job["lines"])
        assert "nothing restarted" in text and "Ready:" in text
        # what was copied, here: the code with its history, the filestore, the last backup only
        here = root.trees[("acme-in", "odoo", "production", "addons")].top
        assert (here / "custom" / ".git" / "HEAD").read_text() == "ref: refs/heads/main\n"
        assert (root.trees[("acme-in", "odoo", "production", "filestore")].top / "ab" / "ab01").stat().st_size == 50_000
        backups = provider.far_settings.backup_dir / "acme-in-odoo" / "production"
        assert sorted(p.name for p in backups.iterdir() if p.suffix == ".zip") == ["new.zip"]
        mine = handover.In(provider.far_settings.jobs_dir.parent).state(hid)
        assert mine["phase"] == "ready" and mine["workspace"] == "acme-in" and mine["servers"] == ["acme-in-odoo"]
        assert mine["made_workspace"] is True and "acme-in-odoo" in provider.far_settings.servers
        page = (await c.get("/workspaces/acme-in", headers={"Authorization": AUTH})).text
        assert 'id="reception-band" data-phase="ready"' in page and 'id="reception-retry"' not in page
    # the allow restarted nothing and let in the one address the customer called from
    assert len(root.allowed) == 1 and root.allowed[0]["restart"] is False
    assert root.allowed[0]["address"] == "127.0.0.1" and root.allowed[0]["link"] == f"handover:{hid}"
    assert len(root.asked("machine-db-replica-listen")) == 1
    arrived = root.arrive["acme-in.main.odoo"]
    assert arrived["state"] == "ready" and arrived["link"] == f"handover:{hid}" and arrived["port"] == 15433
    out = handover.Out(env["settings"].jobs_dir.parent).state("acme")
    assert out["phase"] == "ready" and out["receiver"]["phase"] == "ready"
    # the backups the routes serve: the last one only, the older one 404 even asked by name
    session = handover.In(provider.far_settings.jobs_dir.parent).get_secret(hid, "session").decode()
    auth = {"Authorization": f"Bearer {session}"}
    url = f"/api/handover/v1/{hid}/treesync/file?server=acme-odoo&role=production&tree=backups&path="
    assert (await provider.get(url + "new.zip", headers=auth)).content == b"PK-new-backup"
    assert (await provider.get(url + "old.zip", headers=auth)).status_code == 404
    assert (await provider.get(url + "../production/old.zip", headers=auth)).status_code in (400, 404)
    # a tree of another instance or role is not served
    resp = await provider.post(f"/api/handover/v1/{hid}/treesync/read", headers=auth,
                               json={"server": "beta-x", "role": "production", "tree": "filestore", "paths": ["x"]})
    assert resp.status_code == 404


async def test_the_customers_look_follows_the_provider(customer, provider, env, monkeypatch):
    """Ready, the customer's machine asks every minute: the pin changed or the code expired, out of step with why;
    the code made anew, out of step and no Retry (the code of before goes nowhere) until the new code is read, which
    goes on from what is here (a second round copies only what changed); cancelled there, what it made is taken
    away, and only that."""
    root = provider.root
    filestore = provider_trees(provider, env)
    code = await prepare(provider)
    hid = handover.read_code(code)["h"]
    far = provider.far.state
    inbox = handover.In(provider.far_settings.jobs_dir.parent)
    async with customer as c:
        _rid, job = await receive(c, code)
        assert job["status"] == SUCCESS, job["lines"]
        root.lag = 1.5
        await far.handover_look()
        assert inbox.state(hid)["phase"] == "ready" and inbox.state(hid)["db"]["odoo"]["lag"] == 1.5
        # the provider's certificate changed key: out of step with why, then in step again when it is back
        def other(url, pin, timeout=10.0):
            raise handover.PinError(handover.PIN_REFUSED)
        monkeypatch.setattr(handover, "check_pin", other)
        await far.handover_look()
        assert inbox.state(hid)["phase"] == "out_of_step" and inbox.state(hid)["error"] == handover.PIN_REFUSED
        monkeypatch.setattr(handover, "check_pin", lambda url, pin, timeout=10.0: None)
        await far.handover_look()
        assert inbox.state(hid)["phase"] == "ready" and inbox.state(hid)["error"] is None
        # Redo: out of step, its why the new code, no Retry
        new = (await provider.post("/api/workspaces/acme/handover/code", headers=POST)).json()["code"]
        for _ in range(handover.FAILED_MOST + 2):
            await far.handover_look()
        state = inbox.state(hid)
        assert state["phase"] == "out_of_step" and state["stale"] == "redone" and "new code" in state["error"]
        retry = await c.post(f"/api/handover/{hid}/retry", headers={"Authorization": AUTH, "Origin": "http://far.test"})
        assert retry.status_code == 400 and "new code" in retry.json()["error"]
        page = (await c.get("/workspaces/acme-in", headers={"Authorization": AUTH})).text
        assert 'data-phase="out_of_step"' in page and 'id="reception-retry"' not in page
        # the new code from the same address (after more than 10 looks): it goes on, and copies what changed only
        (filestore / "cd").mkdir()
        (filestore / "cd" / "cd02").write_bytes(os.urandom(7_000))
        _rid, job = await receive(c, new)
        assert job["status"] == SUCCESS, job["lines"]
        assert inbox.state(hid)["rounds"] == 2 and not inbox.state(hid).get("stale")
        assert inbox.state(hid)["done"] == 7_000, inbox.state(hid)
        # expired there: out of step, with the day
        handover.Out(env["settings"].jobs_dir.parent).update("acme", expires=handover.now() - 60)
        await provider._transport.app.app.state.handover_look()
        for _ in range(50):
            if handover.Out(env["settings"].jobs_dir.parent).state("acme").get("phase") == "expired":
                break
            await asyncio.sleep(0.05)
        await far.handover_look()
        assert inbox.state(hid)["phase"] == "out_of_step" and "expired on" in inbox.state(hid)["error"]
        # cancelled there: what this reception made goes, and nothing else (a server it did not make stays)
        ws = provider.far_settings.workspaces["acme-in"]
        provider.far_settings.workspaces["acme-in"] = dataclasses.replace(ws, servers=[*ws.servers, "acme-in-mine"])
        resp = await provider.post("/api/workspaces/acme/handover/cancel", headers=POST)
        assert (await wait_job(provider, resp.json()["job"]))["status"] == SUCCESS
        await far.handover_look()
        assert inbox.state(hid)["phase"] == "cancelling"
        cancel = [j for j in far.jobs.recent(20) if j.title.startswith("Cancel the reception")][0]
        done = await wait_job(c, cancel.id)
        assert done["status"] == SUCCESS, done["lines"]
        assert "acme-in-odoo" not in provider.far_settings.servers
        assert provider.far_settings.workspaces["acme-in"].servers == ["acme-in-mine"]
        assert "acme-in.main.odoo" not in root.arrive and not inbox.state(hid)
        assert [a for a in root.asked("remove") if a[1] == "acme-in"] == [("remove", "acme-in", "main", "odoo")]
    # the provider's database closed again to the customer
    assert root.asked("machine-db-replica-drop")


async def test_a_session_the_provider_refuses_is_not_asked_again(customer, provider, env):
    """A 401 to the look (a session the provider no longer knows) is a failed try there: the customer's machine says
    to read the code again and stops asking, so as not to block its own address."""
    code = await prepare(provider)
    hid = handover.read_code(code)["h"]
    inbox = handover.In(provider.far_settings.jobs_dir.parent)
    inbox.save(hid, {"id": hid, "phase": "ready", "workspace": "acme", "provider": "Studio Rossi"})
    inbox.put_secret(hid, "code", code.encode())
    inbox.put_secret(hid, "session", b"a-session-nobody-made")
    failed = env["settings"].logs_dir / "access-failed.jsonl"
    for _ in range(3):
        await provider.far.state.handover_look()
    state = inbox.state(hid)
    assert state["phase"] == "out_of_step" and state["stale"] == "refused" and "read the code again" in state["error"]
    assert failed.read_text().count("a handover session refused") == 1


async def test_the_customers_cancel_takes_away_the_workspace_it_made(customer, provider, env):
    provider_trees(provider, env)
    code = await prepare(provider)
    async with customer as c:
        hid, job = await receive(c, code)
        assert job["status"] == SUCCESS, job["lines"]
        resp = await c.post(f"/api/handover/{hid}/cancel", headers={"Authorization": AUTH, "Origin": "http://far.test"})
        done = await wait_job(c, resp.json()["job"])
        assert done["status"] == SUCCESS, done["lines"]
        assert "acme-in" not in provider.far_settings.workspaces and "acme-in-odoo" not in provider.far_settings.servers
    assert handover.Out(env["settings"].jobs_dir.parent).state("acme")["phase"] == "cancelled"


def test_the_customer_calls_the_provider_over_ipv4_only(monkeypatch):
    """The provider lets a database in for the one IPv4 address the customer's HTTPS calls come from: those calls go
    over IPv4 (a dual-stack customer reached the portal over IPv6, and the allow was refused); a panel with no IPv4
    address is refused in words before the code is sent."""
    assert handover.ipv4_of("::ffff:203.0.113.5") == "203.0.113.5" and handover.ipv4_of("203.0.113.5") == "203.0.113.5"
    assert handover.ipv4_of("2001:db8::7") == "" and handover.ipv4_of("") == ""
    client = handover.client(URL, PIN)
    assert client._transport._pool._local_address == "0.0.0.0"
    asked = []

    def only_v6(host, port, family=0, type=0, proto=0, flags=0):
        asked.append(family)
        raise handover.socket.gaierror("no A record")
    monkeypatch.setattr(handover.socket, "getaddrinfo", only_v6)
    with pytest.raises(handover.NoIPv4) as refused:
        handover.check_pin(URL, PIN)
    assert asked == [handover.socket.AF_INET] and "no IPv4 address" in str(refused.value)


async def test_an_ipv6_caller_is_told_and_its_database_not_let_in(provider, env):
    code = await prepare(provider)
    hid = handover.read_code(code)["h"]
    _raw, key = handover.new_key()
    app = provider._transport.app
    for address, allowed in (("2001:db8::7", None), ("::ffff:203.0.113.5", "203.0.113.5")):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(address, 4000)),
                                     base_url="http://panel.example.net") as there:
            session = (await there.post("/api/handover/v1/claim", json={"code": code, "key": key})).json()["session"]
            resp = await there.post(f"/api/handover/v1/{hid}/db/odoo/allow", headers={"Authorization": f"Bearer {session}"},
                                    json={"verifier": "SCRAM-SHA-256$4096:x"})
            if allowed is None:
                assert resp.status_code == 409 and "over IPv6" in resp.json()["error"] and not provider.root.allowed
            else:
                assert resp.status_code == 200, resp.text
                assert resp.json()["address"] == allowed and provider.root.allowed[-1]["address"] == allowed


async def test_prepare_restarts_a_database_of_its_own_once_on_the_providers_click(provider, env):
    """The port of a database of its own opens at Prepare (the summary says it restarts, the job says its seconds),
    never at the customer's Receive; the dialog says which; once it listens, nothing restarts again."""
    view = (await provider.get("/api/workspaces/acme/handover", headers={"Authorization": AUTH})).json()
    row = {r["server"]: r for r in view["rows"]}["acme-odoo"]
    assert row["kind"] == "dedicated" and row["restarts"] is True
    form = (await provider.get("/workspaces?edit=acme", headers={"Authorization": AUTH})).text
    assert "restarts once to open its port for the copy" in form and "Here nothing stops until" not in form
    await prepare(provider)
    assert [a[1] for a in provider.root.asked("machine-db-replica-listen")] == ["acme.main.odoo"]
    page = (await provider.get("/workspaces/acme", headers={"Authorization": AUTH})).text
    assert 'id="handover-redo" data-restarts=""' in page
    # its port closed meanwhile (by hand): Redo says it restarts it, and opens it on its click
    folder = Path(env["settings"].servers["acme-odoo"].production.restart.service) / machinedb.DEDICATED_FOLDER
    (folder / machinedb.LISTEN_FILE).unlink()
    page = (await provider.get("/workspaces/acme", headers={"Authorization": AUTH})).text
    assert 'id="handover-redo" data-restarts="acme-odoo"' in page
    assert (await provider.post("/api/workspaces/acme/handover/code", headers=POST)).status_code == 200
    assert len(provider.root.asked("machine-db-replica-listen")) == 2
    view = (await provider.get("/api/workspaces/acme/handover", headers={"Authorization": AUTH})).json()
    assert view["handover"]["restarts"] == []


# --- the pages ------------------------------------------------------------------------------------------------------

async def test_hand_over_lives_in_limits_and_disk_only(provider):
    form = (await provider.get("/workspaces?edit=acme", headers={"Authorization": AUTH})).text
    assert form.count('id="handover-open"') == 1
    tag = re.search(r'<button type="button" id="handover-open"[^>]*>', form).group(0)
    assert 'class="primary' not in tag and re.search(r'title="[^"]{20,}"', tag)
    dialog = form[form.index('<dialog id="handover-dialog"'):]
    dialog = dialog[:dialog.index("</dialog>")]
    # one primary at a time: the others hidden until their step
    primaries = re.findall(r'<button[^>]*class="primary"[^>]*>', dialog)
    assert len(primaries) == 3 and len([p for p in primaries if " hidden" not in p]) == 1
    for button in re.findall(r"<(?:button|summary)\b[^>]*>", dialog):
        assert re.search(r'title="[^"]+"', button), button
    # not in the head of the workspace's page, nor in the list; not for a workspace's people
    page = (await provider.get("/workspaces/acme", headers={"Authorization": AUTH})).text
    assert "handover-open" not in page
    listing = (await provider.get("/workspaces", headers={"Authorization": AUTH})).text
    assert "handover-open" not in listing
    assert (await provider.get("/workspaces?edit=acme", headers=post_as("mario"))).status_code == 403
