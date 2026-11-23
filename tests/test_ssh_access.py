# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""SSH access (README, SSH access; sshkeys.py): the keys of the people who
log in to the machine, written by the root helper in an account's
authorized_keys, only the lines it marked, the others kept byte for byte;
the tab and its routes, the machine's admins only (a person through a link
too, never a machine acting by itself, never a customer), each add and
remove in the access log with its fingerprint and account."""
import base64
import collections
import hashlib
import json
import os
import shutil
import subprocess

import httpx
import pytest

from omniacli import sshkeys, workspacehelper, workspaceops
from omniacli.app import create_app
from omniacli.auth import hash_password
from omniacli.config import Access, Workspace, parse_settings
from omniacli.jobs import JobFailed

from .conftest import PASSWORD, USER_HASH, BasicAsSession, hash_token

HOST = "lab3.test"
ADMIN = {"Authorization": "Basic " + base64.b64encode(f"admin:{PASSWORD}".encode()).decode(), "Origin": f"http://{HOST}"}
MARIO = {"Authorization": "Basic " + base64.b64encode(f"mario:{PASSWORD}".encode()).decode(), "Origin": f"http://{HOST}"}
CENTRAL_TOKEN = "central-" + "c" * 40
# a machine through its link by itself, and a person's browser through that machine's pages
ALONE = {"Authorization": f"Bearer {CENTRAL_TOKEN}", "X-OmniaCLI-Prefix": "/links/lab3", "X-OmniaCLI-User": "carlo",
         "X-OmniaCLI-Role-Cap": "admin"}
PERSON = {**ALONE, "X-OmniaCLI-Menu": "servers,instances,backups,settings"}
Entry = collections.namedtuple("Entry", "pw_name pw_uid pw_gid pw_dir pw_shell")


def ed25519(seed: int) -> str:
    """The base64 of an ed25519 public key (its form: name, 32 bytes)."""
    raw = b"\x00\x00\x00\x0bssh-ed25519" + b"\x00\x00\x00\x20" + hashlib.sha256(bytes([seed])).digest()
    return base64.b64encode(raw).decode()


KEY1, KEY2, KEY3 = ed25519(1), ed25519(2), ed25519(3)


def test_a_line_is_read_with_its_options_and_its_mark():
    parsed = sshkeys.parse_line(f'command="echo hi",no-pty ssh-ed25519 {KEY1} mario@laptop')
    assert parsed["type"] == "ssh-ed25519" and parsed["blob"] == KEY1 and parsed["comment"] == "mario@laptop"
    assert parsed["options"] == 'command="echo hi",no-pty'
    for nothing in ("", "   ", "# a comment", "ssh-ed25519", "ssh-ed25519 notbase64!", f"ssh-rsa {KEY1}"):
        assert sshkeys.parse_line(nothing) is None, nothing
    raw = base64.b64decode(KEY1)
    assert sshkeys.fingerprint(KEY1) == "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")
    line = sshkeys.key_line("ssh-ed25519", KEY1, "Mario's laptop", "link lab3 (carlo)")
    mark = sshkeys.read_marker(sshkeys.parse_line(line)["comment"])
    assert mark["name"] == "Mario's laptop" and mark["by"] == "link lab3 (carlo)" and len(mark["at"]) == 16
    assert " " not in line.split("name=")[1]


def test_what_is_pasted():
    assert sshkeys.check_pasted(f"  ssh-ed25519 {KEY1} mario@laptop \n") == ("ssh-ed25519", KEY1)
    for text, why in (("", "Paste the public key"),
                      ("-----BEGIN OPENSSH PRIVATE KEY-----\nabc", "private key"),
                      (f"ssh-ed25519 {KEY1}\nssh-ed25519 {KEY2}", "one key"),
                      (f'command="x" ssh-ed25519 {KEY1}', "without options"),
                      (f"ssh-dss {KEY1}", "ssh-dss keys are not taken"),
                      (f"ssh-rsa {KEY1}", "not a key of that kind"),
                      ("ssh-ed25519 AAAA", "not a key of that kind")):
        with pytest.raises(sshkeys.KeyRefused, match=why):
            sshkeys.check_pasted(text)
    assert sshkeys.check_name("  Mario   laptop ") == "Mario laptop"
    for bad in ("", "x" * 65, "a\tb\x07"):
        with pytest.raises(sshkeys.KeyRefused):
            sshkeys.check_name(bad)


def test_which_accounts_a_key_may_open():
    entries = [Entry("root", 0, 0, "/root", "/bin/bash"), Entry("daemon", 1, 1, "/usr/sbin", "/usr/sbin/nologin"),
               Entry("odoo", 112, 118, "/opt/odoo", "/bin/bash"), Entry("ubuntu", 1000, 1000, "/home/ubuntu", "/bin/bash"),
               Entry("omniacli", 1001, 1001, "/var/lib/omniacli", "/bin/bash"),
               Entry("deploy", 1002, 1002, "/home/deploy", "/usr/sbin/nologin"),
               Entry("anna", 1003, 1003, "/home/anna", "/bin/zsh"), Entry("nobody", 65534, 65534, "/nonexistent", "/bin/sh")]
    found = sshkeys.offered(entries, {"/bin/bash", "/bin/sh", "/usr/bin/zsh"}, {"omniacli"})
    # anna's shell is not one of /etc/shells; omniacli is OmniaCLI's own
    assert [a["account"] for a in found] == ["root", "ubuntu"]


def test_lines_of_others_are_never_touched():
    others = (f"# keys of the machine\nssh-ed25519 {KEY1} omniacli@provision\n"
              f'from="10.0.0.1"  ssh-ed25519 {KEY2}   someone  \n\nssh-rsa {KEY3}')  # broken kind: kept too
    text = sshkeys.with_line(others, sshkeys.key_line("ssh-ed25519", KEY3, "Mario", "carlo"))
    assert text.startswith(others + "\n") and text.endswith("\n") and text.count("\n") == others.count("\n") + 2
    ours, theirs = sshkeys.listing(text)
    assert [k["name"] for k in ours] == ["Mario"] and [k["fingerprint"] for k in theirs] == [
        sshkeys.fingerprint(KEY1), sshkeys.fingerprint(KEY2)]
    assert theirs[1]["options"] is True and theirs[0]["comment"] == "omniacli@provision"
    # a line of someone else with the same key and the mark, but options: not ours
    kept, gone = sshkeys.without_key(text, sshkeys.fingerprint(KEY3))
    assert gone == 1 and kept == others + "\n"
    # a key of others is never taken away, even when asked by its fingerprint
    assert sshkeys.without_key(text, sshkeys.fingerprint(KEY1)) == (text, 0)


@pytest.fixture
def account(tmp_path, monkeypatch):
    """An account of this test's own user, its home in tmp_path; ssh-keygen and sshd faked."""
    home = tmp_path / "home"
    home.mkdir()
    entry = Entry("ubuntu", os.getuid(), os.getgid(), str(home), "/bin/bash")
    monkeypatch.setattr(sshkeys, "accounts", lambda service: [{"account": "ubuntu", "home": str(home)}])
    monkeypatch.setattr(sshkeys.pwd, "getpwnam", lambda name: entry)
    facts = {"permitrootlogin": "prohibit-password", "authorizedkeysfile": ".ssh/authorized_keys .ssh/authorized_keys2"}
    monkeypatch.setattr(sshkeys, "sshd_facts", lambda: facts)
    monkeypatch.setattr(sshkeys, "keygen", lambda kind, blob: (256, sshkeys.fingerprint(blob), "ED25519"))
    return {"home": home, "facts": facts}


def test_added_and_removed_in_the_file_of_the_account(account):
    home = account["home"]
    first = sshkeys.add("ubuntu", {"name": "Mario", "key": f"ssh-ed25519 {KEY1} x", "by": "carlo"}, set())
    path = home / ".ssh" / "authorized_keys"
    assert first["fingerprint"] == sshkeys.fingerprint(KEY1) and first["file"] == str(path)
    assert oct((home / ".ssh").stat().st_mode & 0o777) == "0o700" and oct(path.stat().st_mode & 0o777) == "0o600"
    # someone's key added by hand afterwards, without a newline at its end
    with open(path, "a") as fh:
        fh.write(f"ssh-ed25519 {KEY2} anna@desk")
    sshkeys.add("ubuntu", {"name": "Luca", "key": f"ssh-ed25519 {KEY3}", "by": "link lab3 (carlo)"}, set())
    lines = path.read_text().splitlines()
    assert lines[1] == f"ssh-ed25519 {KEY2} anna@desk" and "name=Luca" in lines[2]
    with pytest.raises(sshkeys.KeyRefused, match="opens ubuntu already"):
        sshkeys.add("ubuntu", {"name": "Again", "key": f"ssh-ed25519 {KEY2}", "by": "carlo"}, set())
    with pytest.raises(sshkeys.KeyRefused, match="a key it did not add is taken away by hand"):
        sshkeys.remove("ubuntu", sshkeys.fingerprint(KEY2), set())
    done = sshkeys.remove("ubuntu", sshkeys.fingerprint(KEY1), set())
    assert done["removed"] == 1 and done["name"] == "Mario"
    assert path.read_text().splitlines()[0] == f"ssh-ed25519 {KEY2} anna@desk"
    state = sshkeys.state(set())
    assert [k["name"] for k in state["accounts"][0]["keys"]] == ["Luca"] and len(state["accounts"][0]["others"]) == 1
    assert state["reads_file"] is True
    assert not [p for p in (home / ".ssh").iterdir() if p.name != "authorized_keys"]


def test_a_link_is_never_followed(account, tmp_path):
    home, target = account["home"], tmp_path / "elsewhere"
    target.write_text("precious\n")
    (home / ".ssh").mkdir(mode=0o700)
    (home / ".ssh" / "authorized_keys").symlink_to(target)
    with pytest.raises(sshkeys.KeyRefused, match="not a file"):
        sshkeys.add("ubuntu", {"name": "Mario", "key": f"ssh-ed25519 {KEY1}", "by": "carlo"}, set())
    (home / ".ssh" / "authorized_keys").unlink()
    (home / ".ssh").rmdir()
    (home / ".ssh").symlink_to(tmp_path)
    with pytest.raises(sshkeys.KeyRefused, match="not a folder"):
        sshkeys.add("ubuntu", {"name": "Mario", "key": f"ssh-ed25519 {KEY1}", "by": "carlo"}, set())
    assert target.read_text() == "precious\n"


def test_refused_where_sshd_reads_another_file_and_for_another_account(account):
    account["facts"]["authorizedkeysfile"] = "/etc/ssh/keys/%u"
    with pytest.raises(sshkeys.KeyRefused, match="/etc/ssh/keys/%u"):
        sshkeys.add("ubuntu", {"name": "Mario", "key": f"ssh-ed25519 {KEY1}", "by": "carlo"}, set())
    with pytest.raises(sshkeys.KeyRefused, match="not an account a key may open"):
        sshkeys.add("omniacli", {"name": "Mario", "key": f"ssh-ed25519 {KEY1}", "by": "carlo"}, set())


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen is not installed")
def test_ssh_keygen_reads_the_key_as_this_module_does(tmp_path):
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "t", "-f", str(tmp_path / "k")], check=True)
    kind, blob = sshkeys.check_pasted((tmp_path / "k.pub").read_text())
    bits, found, name = sshkeys.keygen(kind, blob)
    assert (bits, name) == (256, "ED25519") and found == sshkeys.fingerprint(blob)
    with pytest.raises(sshkeys.KeyRefused, match="ssh-keygen does not read it"):
        sshkeys.keygen("ssh-ed25519", base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x02ab").decode())


def test_the_helper_takes_the_key_on_its_input(monkeypatch):
    got = {}

    def add(account, given, service):
        got.update(account=account, given=given, service=service)
        return {"fingerprint": "SHA256:x"}
    monkeypatch.setattr(sshkeys, "add", add)
    monkeypatch.setattr(workspacehelper, "caller", lambda: ("omniasvc", 999, []))
    monkeypatch.setattr(workspacehelper.sys, "stdin", __import__("io").StringIO('{"name": "M", "key": "k", "by": "c"}'))
    assert workspacehelper.ssh_key_add("root") == {"fingerprint": "SHA256:x"}
    assert got == {"account": "root", "given": {"name": "M", "key": "k", "by": "c"}, "service": {"omniasvc", "omniacli"}}
    assert workspacehelper.ACTIONS["ssh-key-remove"][1] == 2 and workspacehelper.ACTIONS["ssh-keys"][1] == 0

    def refused(*args):
        raise sshkeys.KeyRefused("no")
    monkeypatch.setattr(sshkeys, "remove", refused)
    with pytest.raises(workspacehelper.HelperError, match="no"):
        workspacehelper.ssh_key_remove("root", "SHA256:" + "a" * 43)


# --- the routes and the tab


@pytest.fixture
async def panel(tmp_path, monkeypatch):
    """A machine (admin) with a workspace acme (mario its admin) and a panel
    that reaches it through a link as an admin; the root helper faked: its
    calls recorded, its file kept in memory with sshkeys' own functions."""
    conf = tmp_path / "lab3"
    conf.mkdir()
    settings = parse_settings({"auth": {"users": {"admin": USER_HASH}, "links": {
        "central": {"token": hash_token(CENTRAL_TOKEN), "role": "admin"}}},
        "backup_dir": str(conf / "backups"), "jobs_dir": str(conf / "jobs"), "servers_file": "servers.yaml"},
        base_dir=conf)
    settings.set_web_access(Access(users={"mario": hash_password(PASSWORD, iterations=1000)}, roles={"mario": "workspace"}))
    settings.workspaces = {"acme": Workspace("acme", "ACME", members={"mario": "admin"})}
    files = {"root": f"ssh-ed25519 {KEY1} omniacli@provision\n"}
    calls = []

    async def ask(*args, timeout=None, input=None):
        calls.append((args, json.loads(input) if input else None))
        if args[0] == "ssh-keys":
            rows = []
            for name, text in files.items():
                ours, others = sshkeys.listing(text)
                rows.append({"account": name, "file": f"/{name}/.ssh/authorized_keys", "keys": ours, "others": others})
            rows.append({"account": "ubuntu", "file": "/home/ubuntu/.ssh/authorized_keys", "keys": [], "others": [],
                         "error": "/home/ubuntu/.ssh is not a folder (a link is not followed)"})
            return {"accounts": rows, "sshd": {"permitrootlogin": "prohibit-password"}, "reads_file": True}
        if args[0] == "ssh-key-add":
            kind, blob = sshkeys.check_pasted(json.loads(input)["key"])
            found = sshkeys.fingerprint(blob)
            if found in sshkeys.fingerprints(files[args[1]]):
                raise JobFailed(f"workspace-helper: The key {found} opens {args[1]} already")
            given = json.loads(input)
            files[args[1]] = sshkeys.with_line(files[args[1]], sshkeys.key_line(kind, blob, given["name"], given["by"]))
            return {"account": args[1], "fingerprint": found, "type": kind, "name": given["name"]}
        if args[0] == "ssh-key-remove":
            text, gone = sshkeys.without_key(files[args[1]], args[2])
            if not gone:
                raise JobFailed(f"workspace-helper: OmniaCLI added no key {args[2]} to {args[1]}")
            files[args[1]] = text
            return {"account": args[1], "fingerprint": args[2], "removed": gone, "type": "ssh-ed25519", "name": "?"}
        raise AssertionError(args)
    monkeypatch.setattr(workspaceops, "ask", ask)
    app = create_app(settings)
    transport = httpx.ASGITransport(app=BasicAsSession(app))
    async with httpx.AsyncClient(transport=transport, base_url=f"http://{HOST}") as client:
        client.files, client.calls, client.log = files, calls, conf / "logs" / "access.jsonl"
        yield client


def entries(client):
    return [json.loads(line) for line in client.log.read_text().splitlines()]


async def test_an_admin_adds_and_removes_a_key(panel):
    client = panel
    data = (await client.get("/api/machine/ssh-keys", headers=ADMIN)).json()
    root = data["accounts"][0]
    assert root["keys"] == [] and [o["comment"] for o in root["others"]] == ["omniacli@provision"]
    resp = await client.post("/api/machine/ssh-keys/add", headers=ADMIN,
                             json={"account": "root", "name": "Mario laptop", "key": f"ssh-ed25519 {KEY2} m@l"})
    assert resp.status_code == 200, resp.text
    found = resp.json()["fingerprint"]
    assert found == sshkeys.fingerprint(KEY2)
    assert client.calls[-1] == (("ssh-key-add", "root"), {"name": "Mario laptop", "key": f"ssh-ed25519 {KEY2} m@l",
                                                          "by": "admin"})
    root = (await client.get("/api/machine/ssh-keys", headers=ADMIN)).json()["accounts"][0]
    assert [(k["name"], k["by"]) for k in root["keys"]] == [("Mario laptop", "admin")] and len(root["others"]) == 1
    # the same key again: the helper's refusal, in Italian too
    resp = await client.post("/api/machine/ssh-keys/add", headers={**ADMIN, "Accept-Language": "it"},
                             json={"account": "root", "name": "Again", "key": f"ssh-ed25519 {KEY2}"})
    assert resp.status_code == 400 and resp.json()["error"] == f"La chiave {found} apre già root"
    # refused before the helper: a private key, a bad account
    calls = len(client.calls)
    resp = await client.post("/api/machine/ssh-keys/add", headers=ADMIN,
                             json={"account": "root", "name": "x", "key": "-----BEGIN OPENSSH PRIVATE KEY-----"})
    assert resp.status_code == 400 and "private key" in resp.json()["error"]
    resp = await client.post("/api/machine/ssh-keys/add", headers=ADMIN,
                             json={"account": "../root", "name": "x", "key": f"ssh-ed25519 {KEY3}"})
    assert resp.status_code == 400 and len(client.calls) == calls
    resp = await client.post("/api/machine/ssh-keys/remove", headers=ADMIN, json={"account": "root", "fingerprint": found})
    assert resp.status_code == 200 and client.files["root"] == f"ssh-ed25519 {KEY1} omniacli@provision\n"
    resp = await client.post("/api/machine/ssh-keys/remove", headers=ADMIN,
                             json={"account": "root", "fingerprint": sshkeys.fingerprint(KEY1)})
    assert resp.status_code == 400 and "OmniaCLI added no key" in resp.json()["error"]
    # NIS 2: who, which key, which account
    said = [e["detail"] for e in entries(client) if e["event"] == "action" and "SSH key" in e.get("detail", "")]
    assert said == [f"SSH key {found} (ssh-ed25519, Mario laptop) added: it opens root on this machine",
                    f"SSH key {found} (ssh-ed25519, ?) removed: it no longer opens root on this machine"]
    assert any(e["event"] == "read" and e.get("what") == "SSH keys of the machine" for e in entries(client))


async def test_through_a_link_a_person_may_a_machine_alone_may_not(panel):
    client = panel
    resp = await client.post("/api/machine/ssh-keys/add", headers=PERSON,
                             json={"account": "root", "name": "Anna", "key": f"ssh-ed25519 {KEY3}"})
    assert resp.status_code == 200, resp.text
    assert client.calls[-1][1]["by"] == "link central (carlo)"
    assert "for carlo" in [e for e in entries(client) if "SSH key" in e.get("detail", "")][-1]["detail"]
    page = (await client.get("/", headers=PERSON)).text
    assert 'data-tab="ssh"' in page and "/links/lab3/api/machine/ssh-keys" in page
    for method, url in (("get", "/api/machine/ssh-keys"), ("post", "/api/machine/ssh-keys/add"),
                        ("post", "/api/machine/ssh-keys/remove")):
        resp = await getattr(client, method)(url, headers=ALONE, **({"json": {}} if method == "post" else {}))
        assert resp.status_code == 403, (url, resp.text)


async def test_a_customer_sees_nothing_of_it(panel):
    client = panel
    page = (await client.get("/", headers=MARIO)).text
    assert 'data-tab="ssh"' not in page and "ssh-keys" not in page
    for method, url in (("get", "/api/machine/ssh-keys"), ("post", "/api/machine/ssh-keys/add"),
                        ("post", "/api/machine/ssh-keys/remove")):
        resp = await getattr(client, method)(url, headers=MARIO, **({"json": {"account": "root"}} if method == "post" else {}))
        assert resp.status_code == 403, (url, resp.text)
    assert not [c for c in client.calls if c[0][0] != "ssh-keys"]


async def test_the_tab(panel, monkeypatch):
    client = panel
    page = (await client.get("/", headers={**ADMIN, "Accept-Language": "it"})).text
    assert ">Accesso SSH<" in page and ">Aggiungi una chiave…" in page
    panel_html = page.split('data-panel="ssh"', 1)[1].split("</section>", 1)[0]
    assert panel_html.count('class="primary') == 1
    dialog = page.split('<dialog id="ssh-dialog"', 1)[1].split("</dialog>", 1)[0]
    for button in __import__("re").findall(r"<button\b[^>]*>[^<]*", panel_html + dialog):
        assert 'title="' in button, button
    # sudo that refuses the helper: said how to allow it
    async def refused(*args, **kw):
        raise JobFailed("sudo: a password is required")
    with monkeypatch.context() as patch:
        patch.setattr(workspaceops, "ask", refused)
        resp = await client.get("/api/machine/ssh-keys", headers=ADMIN)
    # said in the tab, not an HTTP 502 the browser counts (an OmniaCLI not prepared, Carlo's own panel)
    assert resp.status_code == 200 and "OMNIACLI_WORKSPACES=1" in resp.json()["unavailable"]
    # a row's error in the page's language
    data = (await client.get("/api/machine/ssh-keys", headers={**ADMIN, "Accept-Language": "it"})).json()
    assert data["accounts"][-1]["error"] == "/home/ubuntu/.ssh non è una cartella (un link non si segue)"
