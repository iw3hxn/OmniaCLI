# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""AD-018 slice 1, «Esportazione, prova a vuoto e import» (§18.3.1 steps 1-3): a machine with two workspaces and six
people (an authenticator app, a passkey, a single sign-on, a token, an OAuth grant, an invitation, a GitHub webhook on
its portal, a custom role) and one name a person of the central has already. The dry run counts right item by item;
the import keeps on the central the same hashes and secrets, and a code computed from the TOTP secret passes there;
no secret in any page or JSON the central shows; only the central's admin link reads them on the machine, every read
in its access log with who asked; the undo deletes the import and leaves the machine as it was."""

import dataclasses
import hashlib
import json
import stat
import time
from pathlib import Path

import httpx
import pytest
import yaml

from omniacli import apiv1, centralbackup, cli, deployhooks, identityimport, oauth, totp
from omniacli.app import create_app
from omniacli.auth import hash_password
from omniacli.config import Access, Invitation, LinkAccess, parse_settings, parse_workspaces, save_invitations

from .conftest import PASSWORD, TOKEN, USER_HASH, BasicAsSession, hash_token
from .test_delegated_scope import running
from .test_mcp import HEADERS as MCP_HEADERS, call, rpc

CENTRAL_LINK_TOKEN = "central-admin-link-token-0123456789"
DEV_LINK_TOKEN = "central-developer-link-token-01234"
HOST = "central.test"
ADMIN = {"Authorization": "Basic YWRtaW46c2VjcmV0", "Accept": "application/json"}
POST = {**ADMIN, "Origin": f"http://{HOST}"}
MACHINE_PORTAL = "portal.machine.test"
CENTRAL_PORTAL = "portale.central.test"
SECRET = "jbswy3dpehpk3pxpjbswy3dpehpk3pxp"
WORKSPACE_TOKEN = "beta-token-of-dario-0123456789abcdef"
CLIENT_SECRET = "client-secret-of-the-idp-0123456789"
ACCESS_TOKEN = "oat_access-token-of-elena-0123456789"
REFRESH_TOKEN = "ort_refresh-token-of-elena-0123456789"
INVITE_TOKEN = "invite-token-of-fabio-0123456789"
PUBLIC_KEY = b"cose-public-key-of-bruno-0123456789"
HOOK_SECRET = "hook-secret-of-the-repo-0123456789"


def files_of(folder) -> dict[str, str]:
    """{path: sha256} of every file under folder: the machine's state, byte by byte."""
    return {str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(folder.rglob("*")) if p.is_file() and "logs" not in p.parts and "jobs" not in p.parts}


class Machine(httpx.AsyncBaseTransport):
    """The machine behind the link: what it was asked, with which headers."""

    def __init__(self, app):
        self.asgi = httpx.ASGITransport(app=app)
        self.asked: list[tuple[str, dict]] = []

    async def handle_async_request(self, request):
        self.asked.append((request.url.path, dict(request.headers)))
        return await self.asgi.handle_async_request(request)


@pytest.fixture
async def pair(env, tmp_path):
    """lab4 (the machine: acme with srv and beta, six people) and lab3 (the central, with its own mario), linked."""
    m = env["settings"]
    people = {"anna": "admin", "bruno": "developer", "carla": "admin", "dario": "admin", "elena": "user",
              "mario": "developer"}
    m.set_web_access(Access(
        users={**{n: hash_password(PASSWORD, iterations=1000) for n in people}, "fabio": "unset$" + "0" * 64,
               "staff": USER_HASH},
        roles={**{n: "workspace" for n in people}, "fabio": "workspace", "staff": "admin"},
        tokens={"t-dario": hash_token(WORKSPACE_TOKEN), "machine-ci": hash_token("machine-token-0123456789")},
        token_roles={"t-dario": "developer", "machine-ci": "admin"}, token_workspaces={"t-dario": "beta"}))
    m.fixed.links["central"] = LinkAccess(hash_token(CENTRAL_LINK_TOKEN), "admin")
    m.fixed.links["central-dev"] = LinkAccess(hash_token(DEV_LINK_TOKEN), "developer")
    m.set_web_access(m.web)
    m.workspaces = parse_workspaces({
        "acme": {"label": "ACME", "servers": ["srv"],
                 "members": {"anna": "admin", "bruno": "developer", "carla": "admin", "staff": "admin"},
                 "roles": {"backup-only": ["backup"]}, "member_roles": {"carla": "backup-only"}},
        "beta": {"label": "Beta", "members": {"dario": "admin", "elena": "user", "mario": "developer",
                                              "fabio": "user"},
                 "token_people": {"t-dario": "dario"}}})
    m.options = dataclasses.replace(m.options, portal_address=MACHINE_PORTAL)
    save_invitations(m.users_file.with_name("invitations.yaml"),
                     [Invitation("fabio", hash_token(INVITE_TOKEN), time.time(), time.time() + 72 * 3600, "dario",
                                 "beta")], time.time())
    deployhooks.save(m.servers_file.parent / "github-hooks.json", {"github.com/acme/addons": {
        "host": "github.com", "repository": "acme/addons", "id": 7, "url": f"https://{MACHINE_PORTAL}/hooks/github/r1",
        "rid": "r1", "secret": HOOK_SECRET, "server": "srv", "instances": ["srv/production"]}})
    store = oauth.OAuthStore(m.jobs_dir.parent / oauth.FILE)
    now = time.time()
    store.clients["c1"] = {"id": "c1", "name": "Claude", "kind": "dynamic", "secret": oauth.digest("c1-secret-012345")}
    store.grants["g1"] = {"id": "g1", "name": "oauth:elena:claude:g1g1", "user": "elena", "workspace": "beta",
                          "client_id": "c1", "client_name": "Claude", "client_kind": "dynamic", "redirect_host": "claude.ai",
                          "level": "read", "servers": [], "resource": f"https://{MACHINE_PORTAL}/mcp", "created": now,
                          "ends": now + 86400 * 30, "days": 30, "used": 0, "refreshed": 0,
                          "access": {oauth.digest(ACCESS_TOKEN): now + 3600}, "refresh": oauth.digest(REFRESH_TOKEN),
                          "refresh_ends": now + 86400, "person": oauth.digest(m.users["elena"])}
    store._save()
    machine_app = create_app(m)
    state = machine_app.state
    state.second_factors.enable("anna", SECRET, -1)
    state.passkeys.add("bruno", b"h" * 32, MACHINE_PORTAL, b"cred-of-bruno", PUBLIC_KEY, 41, "phone", [], False)
    state.sso.set_provider("acme", {"issuer": "https://idp.acme.test", "client_id": "omnia", "client_secret": CLIENT_SECRET,
                                    "domains": ["acme.test"], "role": "user", "mfa": False}, "anna")
    state.sso.link("acme", "https://idp.acme.test", "sub-carla", "carla", "carla@acme.test")
    machine = Machine(machine_app)
    conf = tmp_path / "central"
    conf.mkdir()
    (conf / "links.yaml").write_text(yaml.safe_dump({"links": {
        "lab4": {"url": "http://machine.test", "token": CENTRAL_LINK_TOKEN},
        "lab4dev": {"url": "http://machine.test", "token": DEV_LINK_TOKEN}}}))
    central = parse_settings({"auth": {"users": {"admin": USER_HASH, "mario": USER_HASH,
                                                 "dev": {"password": USER_HASH, "role": "developer"}},
                                       "tokens": {"tester": hash_token(TOKEN)}},
                              "backup_dir": str(tmp_path / "cb"), "jobs_dir": str(conf / "jobs"),
                              "servers_file": "servers.yaml"}, base_dir=conf)
    central.options = dataclasses.replace(central.options, portal_address=CENTRAL_PORTAL)
    app = create_app(central, link_transport=machine)
    async with running(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=BasicAsSession(app)),
                                               base_url=f"http://{HOST}") as c:
        c.app, c.machine, c.machine_app, c.m = app, machine, machine_app, m
        c.secrets = [SECRET, CLIENT_SECRET, WORKSPACE_TOKEN, hash_token(WORKSPACE_TOKEN), ACCESS_TOKEN,
                     oauth.digest(ACCESS_TOKEN), oauth.digest(REFRESH_TOKEN), oauth.digest("c1-secret-012345"),
                     hash_token(INVITE_TOKEN), HOOK_SECRET, identityimport.passkeys.b64(PUBLIC_KEY),
                     *(m.users[n] for n in people),
                     *((state.second_factors.record("anna") or {}).get("recovery") or []),
                     (state.second_factors.record("anna") or {}).get("salt")]
        c.before = files_of(m.servers_file.parent)
        yield c


def machine_log(c) -> str:
    return json.dumps(c.machine_app.state.access_log.recent())


def rows(plan, ws) -> dict:
    found = {}
    for row in next(w for w in plan["workspaces"] if w["name"] == ws)["items"]:
        found[(row["item"], row.get("rp", ""), row["verdict"])] = (row["count"], row["who"])
    return found


async def test_the_dry_run_counts_right_item_by_item(pair):
    resp = await pair.get("/api/identities/lab4", headers=ADMIN)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["reached"] and data["imported"] is None and data["error"] == ""
    plan = data["plan"]
    assert rows(plan, "acme") == {
        ("people", "", "same"): (3, ""), ("roles", "", "same"): (3, ""), ("custom_roles", "", "same"): (1, ""),
        ("totp", "", "same"): (1, ""), ("passkeys", MACHINE_PORTAL, "redo"): (1, "person"),
        ("sso", "", "redo"): (1, "provider_admin"), ("sso_identities", "", "same"): (1, ""),
        ("tokens", "", "same"): (0, ""), ("webhooks", "", "redo"): (1, "switch"), ("staff", "", "stays"): (1, "")}
    assert rows(plan, "beta") == {
        ("people", "", "same"): (4, ""), ("renamed", "", "redo"): (1, "person"), ("roles", "", "same"): (4, ""),
        ("custom_roles", "", "same"): (0, ""), ("totp", "", "same"): (0, ""), ("tokens", "", "same"): (1, ""),
        ("grants", "", "redo"): (1, "person"), ("invitations", "", "redo"): (1, "workspace_admin")}
    sso = next(r for r in plan["workspaces"][0]["items"] if r["item"] == "sso")
    assert sso["uri"] == f"https://{CENTRAL_PORTAL}/sso/acme/callback"
    # one name the central has already: it arrives as <name>.<workspace>
    assert plan["renamed"] == {"mario": "mario.beta"}
    beta = {p["name"]: p for p in plan["workspaces"][1]["people"]}
    assert beta["mario"]["arrives_as"] == "mario.beta" and beta["dario"]["arrives_as"] == "dario"
    assert plan["totals"]["people"] == 7 and plan["portal"] == f"https://{CENTRAL_PORTAL}"
    # the machine was asked through the link, read only, and wrote it in its access log with who asked
    assert [p for p, _ in pair.machine.asked] == [identityimport.API + "/inventory"]
    assert "identities of the workspaces (dry run): GET /api/identity/v1/inventory -> 200 for admin" in machine_log(pair)
    assert "identities of the workspaces of lab4 (dry run)" in json.dumps(pair.app.state.access_log.recent())
    assert files_of(pair.m.servers_file.parent) == pair.before


async def test_the_import_keeps_the_same_hashes_and_a_totp_code_passes(pair):
    store = identityimport.Store(pair.app.state.settings.jobs_dir.parent)
    assert (await pair.post("/api/identities/lab4/import", headers=POST, json={})).status_code == 400
    resp = await pair.post("/api/identities/lab4/import", headers=POST, json={"confirm": "lab4"})
    assert resp.status_code == 200, resp.text
    done = resp.json()["imported"]
    assert done["people"] == 7 and done["totp"] == 1 and done["passkeys"] == 1 and done["renamed"] == {"mario": "mario.beta"}
    record = store.load("lab4")
    m = pair.m
    # the same hashes, under the names they arrive with; the central's own mario as it was
    for name, person in record["people"].items():
        assert person["hash"] == m.users[person["from"]], name
    assert record["people"]["mario.beta"]["from"] == "mario"
    assert pair.app.state.settings.users["mario"] == USER_HASH
    assert record["workspaces"]["beta"]["members"]["mario.beta"] == "developer"
    assert set(record["people"]) == {"anna", "bruno", "carla", "dario", "elena", "mario.beta", "fabio"}
    assert "staff" not in record["people"] and "staff" not in record["workspaces"]["acme"]["members"]
    # the same secrets: the passkey with its counter, the SSO with its client secret, the token's hash, the grant
    assert record["people"]["bruno"]["passkeys"]["keys"][0]["count"] == 41
    assert record["workspaces"]["acme"]["sso"]["provider"]["client_secret"] == CLIENT_SECRET
    assert record["workspaces"]["acme"]["member_roles"] == {"carla": "backup-only"}
    assert record["workspaces"]["beta"]["tokens"]["t-dario"]["hash"] == hash_token(WORKSPACE_TOKEN)
    assert "machine-ci" not in json.dumps(record)
    assert record["workspaces"]["beta"]["grants"]["g1"]["refresh"] == oauth.digest(REFRESH_TOKEN)
    # a code computed from the secret passes on the central (the check slice 2's login calls), once
    at = time.time()
    code = totp.totp(SECRET, at)
    assert not store.check("lab4", "anna", "wrong", code, at)
    assert store.check("lab4", "anna", PASSWORD, code, at)
    assert not store.check("lab4", "anna", PASSWORD, code, at)
    assert not store.check("lab4", "mario", PASSWORD, code, at)
    # kept readable by its owner only, the package gone
    assert stat.S_IMODE(store.path("lab4").stat().st_mode) == 0o600
    assert stat.S_IMODE(store.folder.stat().st_mode) == 0o700
    assert store.staged() == []
    # the export read through the link, in the machine's access log with who asked; nothing written there
    assert "identities of the workspaces (export, with their secrets): GET /api/identity/v1/export -> 200 for admin" \
        in machine_log(pair)
    assert "the identities of lab4 imported on this panel: 7 people" in json.dumps(pair.app.state.access_log.recent())
    assert files_of(pair.m.servers_file.parent) == pair.before
    # again: the import of before replaced, the names the same
    again = await pair.post("/api/identities/lab4/import", headers=POST, json={"confirm": "lab4"})
    assert again.json()["imported"]["renamed"] == {"mario": "mario.beta"}


async def test_a_name_taken_by_another_import_goes_on_with_a_number():
    assert identityimport.names_map({"mario": "beta", "anna": "acme"}, {"mario", "mario.beta"}) == {
        "anna": "anna", "mario": "mario.beta-2"}


async def test_no_secret_in_the_pages(pair):
    await pair.post("/api/identities/lab4/import", headers=POST, json={"confirm": "lab4"})
    admin = await pair.post("/api/access/tokens/save", headers=POST, json={"name": "boss", "role": "admin", "new": True})
    bearer = {"Authorization": f"Bearer {admin.json()['token']}", "Accept": "application/json"}
    shown = [(await pair.get("/identities/lab4", headers={**ADMIN, "Accept": "text/html"})).text,
             (await pair.get("/api/identities/lab4", headers=ADMIN)).text,
             (await pair.get(apiv1.PREFIX + "/links/lab4/identities", headers=bearer)).text,
             json.dumps(await call(pair, "identity_import_status", headers={**MCP_HEADERS, **bearer}, link="lab4")),
             (await pair.post(apiv1.PREFIX + "/links/lab4/identities/import", headers=bearer,
                              json={"confirm": "lab4"})).text,
             json.dumps(pair.app.state.access_log.recent()), machine_log(pair)]
    # what the panel shows is the dry run and the import, with the names: only no secret in them
    assert all("mario.beta" in text for text in shown[:5]), [t[:80] for t in shown[:5]]
    for text in shown:
        assert text
        found = [s[:12] for s in pair.secrets if s and s in text]
        assert not found, found


async def test_the_undo_leaves_the_machine_untouched(pair):
    store = identityimport.Store(pair.app.state.settings.jobs_dir.parent)
    await pair.post("/api/identities/lab4/import", headers=POST, json={"confirm": "lab4"})
    assert store.load("lab4") is not None
    assert (await pair.post("/api/identities/lab4/delete", headers=POST, json={"confirm": "nope"})).status_code == 400
    gone = await pair.post("/api/identities/lab4/delete", headers=POST, json={"confirm": "lab4"})
    assert gone.status_code == 200 and gone.json()["imported"] is None
    assert store.load("lab4") is None and store.links() == []
    assert (await pair.post("/api/identities/lab4/delete", headers=POST, json={"confirm": "lab4"})).status_code == 404
    assert files_of(pair.m.servers_file.parent) == pair.before
    assert "the identities imported from lab4 deleted on this panel" in json.dumps(pair.app.state.access_log.recent())
    # the people of the machine log in there as before
    assert pair.machine_app.state.authenticator.check("anna", PASSWORD)


async def test_only_the_central_admin_link_reads_them(pair):
    link = {"Authorization": f"Bearer {CENTRAL_LINK_TOKEN}", "Accept": "application/json", "X-OmniaCLI-User": "carlo"}
    async with httpx.AsyncClient(transport=pair.machine.asgi, base_url="http://machine.test") as m:
        for path in ("/inventory", "/export"):
            url = identityimport.API + path
            assert (await m.get(url, headers=link)).status_code == 200
            # a link with the developer role, a role capped below admin, a person through the link's pages
            assert (await m.get(url, headers={**link, "Authorization": f"Bearer {DEV_LINK_TOKEN}"})).status_code == 403
            assert (await m.get(url, headers={**link, "X-OmniaCLI-Role-Cap": "developer"})).status_code == 403
            assert (await m.get(url, headers={**link, "X-OmniaCLI-Prefix": "/links/lab4",
                                              "X-OmniaCLI-Menu": "instances"})).status_code == 403
            # a person of the machine, a token of the machine
            assert (await m.get(url, headers=ADMIN)).status_code in (401, 403)
            assert (await m.get(url, headers={"Authorization": f"Bearer {WORKSPACE_TOKEN}"})).status_code == 403
    log = machine_log(pair)
    assert "GET /api/identity/v1/export -> 403 for carlo" in log and "-> 200 for carlo" in log
    # the central's link proxy never passes them to a browser
    for path in ("api/identity/v1/export", "api/identity/v1/inventory"):
        assert (await pair.get(f"/links/lab4/{path}", headers=ADMIN)).status_code == 404
    # the central refuses a developer, and the developer link's machine refuses it
    dev = {"Authorization": "Basic ZGV2OnNlY3JldA==", "Accept": "application/json", "Origin": f"http://{HOST}"}
    assert (await pair.get("/identities/lab4", headers=dev)).status_code == 403
    assert (await pair.post("/api/identities/lab4/import", headers=dev, json={"confirm": "lab4"})).status_code == 403
    through_dev = (await pair.get("/api/identities/lab4dev", headers=ADMIN)).json()
    assert not through_dev["reached"] and "admin role" in through_dev["error"]
    assert (await pair.post("/api/identities/lab4dev/import", headers=POST, json={"confirm": "lab4dev"})).status_code == 502
    assert (await pair.get("/api/identities/nope", headers=ADMIN)).status_code == 404


async def test_the_rest_api_and_mcp(pair):
    async def token(name, role, **more):
        made = await pair.post("/api/access/tokens/save", headers=POST, json={"name": name, "role": role, "new": True,
                                                                               **more})
        return {"Authorization": f"Bearer {made.json()['token']}", "Accept": "application/json"}
    admin, developer, ci = await token("boss", "admin"), await token("dev-bot", "developer"), await token(
        "ci-bot", "admin", ci=True)
    v1 = apiv1.PREFIX + "/links/lab4/identities"
    assert (await pair.get(v1, headers=developer)).json()["error"]["code"] == "forbidden"
    assert (await pair.post(v1 + "/import", headers=ci, json={"confirm": "lab4"})).json()["error"]["code"] == "forbidden"
    assert (await pair.post(v1 + "/import", headers=admin, json={"confirm": "x"})).json()["error"]["code"] == \
        "confirm_required"
    assert (await pair.get(v1, headers=admin)).json()["plan"]["renamed"] == {"mario": "mario.beta"}
    made = (await pair.post(v1 + "/import", headers=admin, json={"confirm": "lab4"})).json()
    assert made["imported"]["people"] == 7
    assert (await pair.post(v1 + "/delete", headers=admin, json={"confirm": "lab4"})).json()["imported"] is None
    # MCP: the same code, the same rules
    tools = {**MCP_HEADERS, **admin}
    status = await call(pair, "identity_import_status", headers=tools, link="lab4")
    assert status["reached"] and status["imported"] is None
    assert "confirm_required" in (await call(pair, "identity_import", headers=tools, link="lab4", confirm="x"))["error"]
    assert (await call(pair, "identity_import", headers=tools, link="lab4", confirm="lab4"))["imported"]["people"] == 7
    assert (await call(pair, "identity_import_delete", headers=tools, link="lab4", confirm="lab4"))["imported"] is None
    listed = (await rpc(pair, "tools/list", headers={**MCP_HEADERS, **developer})).json()["result"]["tools"]
    assert not {t["name"] for t in listed} & {"identity_import_status", "identity_import"}
    # a link never imports on this panel
    from omniacli.auth import Principal
    code, data = await pair.app.state.api_hooks["identity"]("status", "lab4", Principal("x", "admin", "link"), "")
    assert code == 403 and "not through a link" in data["error"]


async def test_the_page_in_italian_with_one_primary_button(pair):
    page = await pair.get("/identities/lab4", headers={**ADMIN, "Accept": "text/html", "Cookie": "omniacli_lang=it"})
    assert page.status_code == 200
    text = page.text
    assert "<h1>Identità di lab4</h1>" in text and "La prova a vuoto, voce per voce" in text
    assert "Passa uguale" in text and "Va rifatta dalla persona" in text and "mario.beta" in text
    assert text.count('class="primary"') == 1 and "Importa qui" in text
    # every button's title says more than its text
    import re
    for title, label in re.findall(r'<button[^>]*title="([^"]+)"[^>]*>([^<]+)</button>', text):
        assert title.strip() != label.strip(), label
    await pair.post("/api/identities/lab4/import", headers=POST, json={"confirm": "lab4"})
    text = (await pair.get("/identities/lab4", headers={**ADMIN, "Accept": "text/html",
                                                         "Cookie": "omniacli_lang=it"})).text
    assert "Importa di nuovo" in text and "Cancella l&#39;import…" in text and text.count('class="primary"') == 1
    # the Links page leads there
    links = (await pair.get("/links", headers={**ADMIN, "Accept": "text/html"})).text
    assert 'href="/identities/lab4"' in links
    refused = await pair.post("/api/identities/lab4/import", headers={**POST, "Cookie": "omniacli_lang=it"}, json={})
    assert refused.json()["error"] == "Scrivi il nome del collegamento, lab4, per confermare"


def test_the_purge_and_the_backup_of_the_central_take_them():
    purge = (Path(cli.__file__).parent.parent / "packaging" / "debian" / "files" / "postrm").read_text()
    purge = purge.split("    purge)", 1)[1]
    assert "/var/lib/omniacli/identity-imports" in purge and f"/var/lib/omniacli/{identityimport.STAGED}*" in purge
    slot = centralbackup.BY_SLOT["identity_imports"]
    assert slot.folder
