# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The handover of a workspace to the customer's own OmniaCLI (AD-008…AD-011, UX-014 part A; step 7 of §9 of
docs/adr/AD-008-consegna-e-spostamento.md, up to «ready»): the code (one line, 72 hours, one machine, its hash only
kept, the pin of the provider's certificate), the provider's routes (Prepare, Redo, Cancel) and the public ones the
customer's machine calls with the code and then its session, the customer's side (read the code, cancel the
reception, told when the provider cancels), the pages."""

import dataclasses
import json
import re
import ssl
import subprocess

import httpx
import pytest

from omniacli import handover
from omniacli.jobs import SUCCESS

from .conftest import BasicAsSession
from .test_app import AUTH, POST, wait_job  # noqa: F401
from .test_staging_dialog import acme, two  # noqa: F401 - the fixtures
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

@pytest.fixture
def provider(two, env, monkeypatch):
    """two's machine as the provider: a portal with a certificate (its pin read as PIN), acme-odoo without its test
    environment (an instance with a test passes in a later step)."""
    settings = env["settings"]
    settings.options = dataclasses.replace(settings.options, portal_address="panel.example.net",
                                           provider_label="Studio Rossi")
    monkeypatch.setattr(handover, "presented_pin", lambda url, timeout=10.0: PIN)
    settings.servers["acme-odoo"].instances.pop("test", None)
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
    # Redo: the code of before no longer holds, nor its session; the new one is claimed by any machine
    redo = await provider.post("/api/workspaces/acme/handover/code", headers=POST)
    assert redo.status_code == 200
    new = redo.json()["code"]
    assert handover.read_code(new)["h"] == hid
    assert (await provider.get("/api/handover/v1/offer", headers={handover.CODE_HEADER: code})).status_code == 401
    assert (await provider.get(f"/api/handover/v1/{hid}/state", headers={"Authorization": f"Bearer {session}"})).status_code == 401
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
