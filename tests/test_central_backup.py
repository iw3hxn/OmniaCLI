# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""AD-017 slice 7, the protection of the central panel (§17.4.3): its own state sealed every night on a destination
to a key whose passphrase only its admin keeps; the setup from the panel (admin only, logged), the REST API and MCP;
and omniacli central-restore on a fresh install, which refuses another passphrase and a file changed."""

import datetime
import json
import os
import stat
import tarfile
from pathlib import Path

import pytest
import yaml

from omniacli import apiv1, centralbackup, cli, destinations, pullcrypto
from omniacli.auth import Principal
from omniacli.config import load_links, parse_settings

from .conftest import LINK_TOKEN, USER_HASH
from .test_api_v1 import token
from .test_app import AUTH, POST, client, wait_job  # noqa: F401 - the client fixture
from .test_destinations import rclone  # noqa: F401 - the rclone fixture
from .test_links import as_user
from .test_mcp import HEADERS, call

LINK_SECRET = "tok-of-lab4-in-clear-0123456789"
SSH_KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nsecret-key-of-the-link\n-----END OPENSSH PRIVATE KEY-----\n"


@pytest.fixture(autouse=True)
def quick_scrypt(monkeypatch):
    """scrypt at 1 MiB in the tests (the real cost is checked apart)."""
    monkeypatch.setattr(centralbackup, "SCRYPT_N", 1 << 10)


def central_dir(base: Path, links: bool = True) -> tuple[dict, Path]:
    """A central panel's main configuration and its files, as the package lays them out: /etc and /var/lib."""
    etc, var = base / "etc", base / "var"
    etc.mkdir(parents=True)
    (var / "ssh").mkdir(parents=True)
    data = {"auth": {"users": {"admin": USER_HASH}}, "backup_dir": str(var / "backups"),
            "jobs_dir": str(var / "jobs"), "servers_file": str(var / "servers.yaml")}
    config = etc / "omniacli.yaml"
    config.write_text(yaml.safe_dump(data))
    if links:
        ca = base / "ca" / "lab4.pem"
        ca.parent.mkdir()
        ca.write_text("-----BEGIN CERTIFICATE-----\nthe CA of lab4\n-----END CERTIFICATE-----\n")
        (var / "links.yaml").write_text(yaml.safe_dump({"links": {
            "lab4": {"url": "http://127.0.0.1:8090", "token": LINK_SECRET,
                     "ssh": {"host": "172.30.0.14", "port": 22, "user": "omniacli-link",
                             "host_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA/Mn4aNxF5yB5noa8ISaSF/ScHulHckMhT6cwmWPB2Y"}},
            "ext": {"url": "https://ext.test", "token": "tok-of-ext", "ca_file": str(ca)}}}))
        (var / "ssh" / "id_ed25519").write_text(SSH_KEY)
        (var / "ssh" / "id_ed25519.pub").write_text("ssh-ed25519 AAAA omniacli\n")
        (var / "ssh" / "known_hosts_lab4").write_text("172.30.0.14 ssh-ed25519 AAAA\n")
        (var / "link-agents.json").write_text('{"lab4": 4}')
        (var / "link-panels.json").write_text('{"lab4": "agent"}')
        (var / "users.yaml").write_text(yaml.safe_dump({"users": {"carla": {"password": USER_HASH, "role": "admin"}}}))
        (var / "second-factor.json").write_text('{"carla": {"secret": "TOTP-SECRET"}}')
        (var / "options.yaml").write_text("{}\n")
        (var / "sessions.json").write_text('{"a-session": "of carla"}')
        (var / "logs").mkdir()
        (var / "logs" / "access.jsonl").write_text('{"event": "login", "name": "carla"}\n')
        (var / "logs" / "omniacli.log").write_text("not in the backup\n")
    return data, config


def settings_of(config: Path):
    return parse_settings(yaml.safe_load(config.read_text()), base_dir=config.parent)


def sealed_backup(tmp_path: Path, passphrase: str = "abcde-fghjk-mnpqr-stuvw-xyz23-45678") -> tuple[Path, dict, Path]:
    """A central with links, its backup sealed to a key of passphrase: (the backup, its key, its config)."""
    _, config = central_dir(tmp_path / "old")
    settings = settings_of(config)
    key = centralbackup.new_key(passphrase)
    archive = tmp_path / "state.tar.gz"
    manifest = centralbackup.build_archive(settings, config, archive, "2026-11-25T02:30:00", "2026.1.5521")
    assert manifest["links"] == ["ext", "lab4"]
    backup = tmp_path / "central_20261125_023000.occb"
    centralbackup.seal(key, archive, backup, {"made": "2026-11-25T02:30:00", "host": "lab3", "version": "2026.1.5521"})
    return backup, key, config


# --- the key: the central encrypts without being able to decrypt ---------------------------------------------

def test_the_passphrase_and_the_key():
    passphrase = centralbackup.new_passphrase()
    groups = passphrase.split("-")
    assert len(groups) == 6 and all(len(g) == 5 and set(g) <= set(centralbackup.ALPHABET) for g in groups)
    assert centralbackup.new_passphrase() != passphrase
    key = centralbackup.new_key(passphrase)
    # what the panel keeps: a public key and the private key sealed, never the passphrase nor the key in clear
    assert set(key) == {"public", "kdf", "nonce", "sealed"} and passphrase not in json.dumps(key)
    assert key["kdf"] == {**key["kdf"], "name": "scrypt", "r": 8, "p": 1}
    private = centralbackup.open_key(key, passphrase)
    # typed with capitals, spaces or without the dashes it is the same
    centralbackup.open_key(key, " " + passphrase.upper().replace("-", " ") + " ")
    with pytest.raises(centralbackup.CentralBackupError, match="the passphrase does not open this key"):
        centralbackup.open_key(key, centralbackup.new_passphrase())
    assert private.public_key() is not None and len(centralbackup.fingerprint(key)) == 19


def test_the_real_cost_of_the_passphrase(monkeypatch):
    monkeypatch.undo()
    assert centralbackup.SCRYPT_N == 1 << 17
    key = centralbackup.new_key("x")
    assert key["kdf"]["n"] == 1 << 17
    # a header that asks more than MAX_N is refused before scrypt runs
    with pytest.raises(centralbackup.CentralBackupError, match="out of range"):
        centralbackup.open_key({**key, "kdf": {**key["kdf"], "n": 1 << 24}}, "x")


def test_a_backup_cannot_be_read_without_the_passphrase(tmp_path):
    backup, key, _ = sealed_backup(tmp_path)
    data = backup.read_bytes()
    # nothing of the state in clear: not the token of the link, not its SSH key, not the second factor
    for secret in (LINK_SECRET, "secret-key-of-the-link", "TOTP-SECRET", "carla"):
        assert secret.encode() not in data, secret
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    with pytest.raises(centralbackup.CentralBackupError, match="does not open"):
        centralbackup.open_sealed(backup, "wrong-pass", tmp_path / "out.tar.gz")
    assert not (tmp_path / "out.tar.gz").exists()
    # the pullcrypto body alone, with the central's own view (the public key) as a key, does not open either
    header, _, start = centralbackup.read_header(backup)
    body = tmp_path / "body"
    body.write_bytes(data[start:])
    with pytest.raises(pullcrypto.PullerCryptoError):
        pullcrypto.decrypt_file(centralbackup._unb64(key["public"]), "central backup", body, tmp_path / "x")
    header = centralbackup.open_sealed(backup, "abcde-fghjk-mnpqr-stuvw-xyz23-45678", tmp_path / "out.tar.gz")
    assert header["host"] == "lab3" and header["made"] == "2026-11-25T02:30:00"


@pytest.mark.parametrize("where", ["body", "header", "end", "cut"])
def test_a_tampered_backup_is_refused(tmp_path, where):
    backup, _, _ = sealed_backup(tmp_path)
    data = bytearray(backup.read_bytes())
    _, _, start = centralbackup.read_header(backup)
    if where == "body":
        data[start + 200] ^= 1
    elif where == "header":
        # the time it says it was made: the header is authenticated too
        data = bytearray(bytes(data).replace(b"2026-11-25T02:30:00", b"2026-11-26T02:30:00", 1))
    elif where == "end":
        data += b"more"
    else:
        data = data[:-100]
    backup.write_bytes(bytes(data))
    with pytest.raises(centralbackup.CentralBackupError, match="changed, cut short or damaged"):
        centralbackup.open_sealed(backup, "abcde-fghjk-mnpqr-stuvw-xyz23-45678", tmp_path / "out.tar.gz")
    assert not (tmp_path / "out.tar.gz").exists()


def test_not_a_backup(tmp_path):
    other = tmp_path / "x.occb"
    other.write_bytes(b"PK\x03\x04 a zip")
    with pytest.raises(centralbackup.CentralBackupError, match="not a backup of a central panel"):
        centralbackup.read_header(other)


# --- what is saved ----------------------------------------------------------------------------------------------

def test_the_inventory_is_what_opens_the_machines_again(tmp_path):
    _, config = central_dir(tmp_path)
    settings = settings_of(config)
    archive = tmp_path / "state.tar.gz"
    manifest = centralbackup.build_archive(settings, config, archive, "now", "v")
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600
    with tarfile.open(archive) as tar:
        names = set(tar.getnames())
        links = tar.extractfile("slots/links").read().decode()
    assert {"slots/links", "slots/ssh/id_ed25519", "slots/ssh/id_ed25519.pub", "slots/ssh/known_hosts_lab4",
            "slots/link_agents", "slots/link_panels", "slots/users", "slots/second_factor", "slots/options",
            "ca/ext", "access/access.jsonl", "config/omniacli.yaml", "manifest.json"} == names
    assert LINK_SECRET in links
    # the sessions, the panel's own log, the jobs: not in it
    assert not any("sessions" in n or "omniacli.log" in n for n in names)
    assert manifest["ca"] == {"ext": str(tmp_path / "ca" / "lab4.pem")} and manifest["config"] is True


# --- the way back ---------------------------------------------------------------------------------------------

def test_restore_on_a_fresh_install_opens_the_links_again(tmp_path):
    backup, key, old_config = sealed_backup(tmp_path)
    ca = tmp_path / "old" / "ca" / "lab4.pem"
    ca.unlink()
    _, config = central_dir(tmp_path / "new", links=False)
    fresh = settings_of(config)
    assert fresh.link_targets == {}
    config_before = config.read_text()
    calls = []
    found = centralbackup.restore(backup, "abcde-fghjk-mnpqr-stuvw-xyz23-45678", fresh, config,
                                  stop=lambda: calls.append("stop"), start=lambda: calls.append("start"),
                                  now=datetime.datetime(2026, 11, 25, 9, 0))
    assert calls == ["stop", "start"] and found["links"] == ["ext", "lab4"] and found["replaced"] == []
    var = tmp_path / "new" / "var"
    restored = settings_of(config)
    old = settings_of(old_config)
    assert restored.link_targets["lab4"].token == LINK_SECRET
    assert restored.link_targets["lab4"].ssh == old.link_targets["lab4"].ssh
    assert (var / "ssh" / "id_ed25519").read_text() == SSH_KEY
    assert json.loads((var / "link-panels.json").read_text()) == {"lab4": "agent"}
    assert "carla" in restored.users and json.loads((var / "second-factor.json").read_text())["carla"]
    for path in (var / "links.yaml", var / "ssh" / "id_ed25519", var / "users.yaml", var / "second-factor.json"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path
    assert ca.read_text().startswith("-----BEGIN CERTIFICATE-----") and stat.S_IMODE(ca.stat().st_mode) == 0o644
    # the access log of the backup beside the new one; the main configuration beside this one, not in its place
    assert (var / "logs" / "before-restore-20261125-090000" / "access.jsonl").exists()
    assert Path(found["config_beside"]) == config.with_name("omniacli.yaml.from-backup")
    assert config.read_text() == config_before
    assert config.with_name("omniacli.yaml.from-backup").read_text() == old_config.read_text()
    assert not (var / "sessions.json").exists()
    # the next backup of the restored panel carries the access log of before too
    manifest = centralbackup.build_archive(restored, config, tmp_path / "next.tar.gz", "later", "v")
    assert manifest["access"] == ["before-restore-20261125-090000/access.jsonl"]
    centralbackup.seal(key, tmp_path / "next.tar.gz", tmp_path / "next.occb", {"made": "later"})
    _, third = central_dir(tmp_path / "third", links=False)
    centralbackup.restore(tmp_path / "next.occb", "abcde-fghjk-mnpqr-stuvw-xyz23-45678", settings_of(third), third,
                          now=datetime.datetime(2026, 12, 1, 9, 0))
    logs = tmp_path / "third" / "var" / "logs"
    assert (logs / "before-restore-20261201-090000" / "before-restore-20261125-090000" / "access.jsonl").read_text() \
        == '{"event": "login", "name": "carla"}\n'


def test_restore_refuses_a_panel_with_links_and_a_wrong_passphrase(tmp_path):
    backup, _, _ = sealed_backup(tmp_path)
    _, config = central_dir(tmp_path / "new")
    settings = settings_of(config)
    (tmp_path / "new" / "var" / "links.yaml").write_text(yaml.safe_dump({"links": {"other": {
        "url": "https://other.test", "token": "the-other-token"}}}))
    before = (tmp_path / "new" / "var" / "links.yaml").read_text()
    with pytest.raises(centralbackup.CentralBackupError, match="has links already .other.*--replace"):
        centralbackup.restore(backup, "abcde-fghjk-mnpqr-stuvw-xyz23-45678", settings, config)
    calls = []
    with pytest.raises(centralbackup.CentralBackupError, match="does not open"):
        centralbackup.restore(backup, "another", settings, config, replace=True, stop=lambda: calls.append("stop"))
    assert calls == [] and (tmp_path / "new" / "var" / "links.yaml").read_text() == before
    # a CA file this machine has at that path stays its own
    (tmp_path / "old" / "ca" / "lab4.pem").write_text("this machine's")
    found = centralbackup.restore(backup, "abcde-fghjk-mnpqr-stuvw-xyz23-45678", settings, config, replace=True)
    assert (tmp_path / "old" / "ca" / "lab4.pem").read_text() == "this machine's"
    assert found["replaced"] == ["other"] and set(load_links(tmp_path / "new" / "var" / "links.yaml")) == {"ext", "lab4"}


def test_the_command_central_restore(tmp_path, capsys):
    backup, _, _ = sealed_backup(tmp_path)
    _, config = central_dir(tmp_path / "new", links=False)
    secret = tmp_path / "pass"
    secret.write_text("abcde-fghjk-mnpqr-stuvw-xyz23-45678\n")
    secret.chmod(0o644)
    args = ["central-restore", str(backup), "-c", str(config), "--passphrase-file", str(secret), "--no-restart"]
    assert cli.main(args) == 1 and "readable by others" in capsys.readouterr().err
    secret.chmod(0o600)
    assert cli.main([*args, "--dry-run"]) == 0
    assert "it opens with this passphrase and is intact" in capsys.readouterr().out
    assert load_links(tmp_path / "new" / "var" / "links.yaml") == {}
    wrong = tmp_path / "wrong"
    wrong.write_text("not-it")
    wrong.chmod(0o600)
    assert cli.main([*args[:-3], "--passphrase-file", str(wrong), "--no-restart"]) == 1
    assert "Not restored: the passphrase does not open this key" in capsys.readouterr().err
    assert cli.main(args) == 0
    out = capsys.readouterr().out
    assert "links: ext, lab4" in out and "omniacli.yaml.from-backup" in out
    assert set(load_links(tmp_path / "new" / "var" / "links.yaml")) == {"ext", "lab4"}
    # again on the same panel: refused, it has links now
    assert cli.main(args) == 1 and "has links already" in capsys.readouterr().err


# --- the nightly run --------------------------------------------------------------------------------------------

def test_it_runs_at_its_time_only_when_ready():
    state = {"key": {"x": 1}, "confirmed": "2026-11-25T01:00:00", "destination": "nas", "time": "02:30"}
    fired = []
    nightly = centralbackup.Nightly(lambda: state, lambda: fired.append(1) or (202, {}))
    assert not nightly.tick(datetime.datetime(2026, 11, 25, 2, 29)) and nightly.tick(datetime.datetime(2026, 11, 25, 2, 30))
    for missing in ({"confirmed": ""}, {"destination": ""}, {"enabled": False}, {"key": None}):
        changed = {**state, **missing}
        assert centralbackup.ready(changed)
        assert not centralbackup.Nightly(lambda: changed, lambda: fired.append(2)).tick(
            datetime.datetime(2026, 11, 25, 2, 30))
    assert fired == [1]
    assert centralbackup.next_run(state, datetime.datetime(2026, 11, 25, 3, 0)) == "2026-11-26T02:30"
    assert centralbackup.next_run({**state, "confirmed": ""}) is None


def test_the_purge_takes_its_files():
    purge = (Path(cli.__file__).parent.parent / "packaging" / "debian" / "files" / "postrm").read_text()
    purge = purge.split("    purge)", 1)[1]
    # invitations.yaml, which a backup carries, was left by the purge before this slice
    for path in (f"/var/lib/omniacli/{centralbackup.FILE}", "/etc/omniacli/omniacli.yaml.from-backup",
                 "/var/lib/omniacli/invitations.yaml",
                 "/var/lib/omniacli/.central-backup-*", "/var/lib/omniacli/logs"):
        assert path in purge, path


# --- the panel: the setup from the page, the REST API and MCP ----------------------------------------------

def _nas(client, root) -> None:
    settings = client.app.state.settings
    dest = destinations.parse_destination("nas", {
        "kind": "sftp", "path": "backups", "settings": {"host": str(root), "user": "u", "pass": "Secret-pw-1"},
        "key": pullcrypto.new_key()})
    destinations.save_destinations(settings.destinations_file, {"nas": dest})


async def test_the_setup_from_the_page_and_a_run(client, rclone, tmp_path):  # noqa: F811
    _nas(client, rclone["root"])
    settings = client.app.state.settings
    page = await client.get("/central-backup", headers=as_user("admin"))
    assert page.status_code == 200 and "<h1>Protection of this panel</h1>" in page.text
    assert 'class="primary"' in page.text and page.text.count('class="primary"') == 1
    assert "no key yet: make one" in page.text
    italian = await client.get("/central-backup", headers={**as_user("admin"), "Cookie": "omniacli_lang=it"})
    assert "Protezione di questo pannello" in italian.text and "nessuna chiave per ora: creala" in italian.text
    # Settings lists it for a person here
    assert 'href="/central-backup"' in (await client.get("/settings", headers=as_user("admin"))).text
    # a developer reaches none of it
    for method, url in (("GET", "/central-backup"), ("GET", "/api/central-backup"), ("POST", "/api/central-backup/key")):
        resp = await client.request(method, url, headers=as_user("dev", post=True), json={})
        assert resp.status_code == 403, url
    saved = (await client.post("/api/central-backup", headers=POST, json={"destination": "nas", "time": "03:15"})).json()
    assert saved["destination"] == "nas" and saved["time"] == "03:15" and saved["why_not"] == "no key yet: make one"
    assert (await client.post("/api/central-backup", headers=POST, json={"destination": "nope"})).status_code == 404
    assert (await client.post("/api/central-backup", headers=POST, json={"time": "25:00"})).status_code == 400
    made = (await client.post("/api/central-backup/key", headers=POST, json={})).json()
    passphrase = made["passphrase"]
    assert made["key"]["fingerprint"] and not made["key"]["confirmed"]
    assert made["why_not"] == "the key's passphrase is not confirmed yet: type it back"
    # given once: never again, nowhere
    state_file = centralbackup.state_file(settings)
    assert stat.S_IMODE(state_file.stat().st_mode) == 0o600 and passphrase not in state_file.read_text()
    again = (await client.get("/api/central-backup", headers={"Authorization": AUTH})).json()
    assert "passphrase" not in again and passphrase not in json.dumps(again)
    assert (await client.post("/api/central-backup/key", headers=POST, json={})).status_code == 409
    assert (await client.post("/api/central-backup/run", headers=POST, json={})).status_code == 409
    wrong = await client.post("/api/central-backup/key/confirm", headers=POST, json={"passphrase": "no"})
    assert wrong.status_code == 400 and "not the passphrase" in wrong.json()["error"]
    ok = (await client.post("/api/central-backup/key/confirm", headers=POST, json={"passphrase": passphrase})).json()
    assert ok["key"]["confirmed"] and ok["why_not"] == "" and ok["next"].endswith("03:15")
    run = (await client.post("/api/central-backup/run", headers=POST, json={})).json()
    job = await wait_job(client, run["job"])
    assert job["status"] == "success", job
    folder = rclone["root"] / "backups" / centralbackup.folder_of(settings)
    copies = list(folder.glob("central_*.occb"))
    assert len(copies) == 1 and job["result"]["file"] == copies[0].name
    # what was copied opens with the passphrase, and only with it
    out = tmp_path / "opened.tar.gz"
    centralbackup.open_sealed(copies[0], passphrase, out)
    with pytest.raises(centralbackup.CentralBackupError):
        centralbackup.open_sealed(copies[0], centralbackup.new_passphrase(), tmp_path / "x.tar.gz")
    last = (await client.get("/api/central-backup", headers={"Authorization": AUTH})).json()["last"]
    assert last["status"] == "success" and last["file"] == copies[0].name
    # nothing of the passphrase in the access log, the job's log, the temporary folders
    log = json.dumps(client.app.state.access_log.recent())
    assert passphrase not in log and passphrase.replace("-", "") not in log
    assert "a new key of this panel's backup" in log and "confirmed" in log and "does not open the key" in log
    lines = (await client.get(f"/api/jobs/{run['job']}", headers={"Authorization": AUTH})).json()
    assert passphrase not in json.dumps(lines)
    assert not list(settings.jobs_dir.parent.glob(".central-backup-*"))
    # the page now: the key kept, the last copy, Back up now on
    page = (await client.get("/central-backup", headers=as_user("admin"))).text
    assert copies[0].name in page and 'id="cb-run"  ' not in page and "Replace the key…" in page
    # a new key replaces it only when asked, and asks its confirmation again
    replaced = (await client.post("/api/central-backup/key", headers=POST, json={"replace": True})).json()
    assert replaced["passphrase"] != passphrase and replaced["why_not"].startswith("the key's passphrase")


async def test_the_rest_api_and_mcp(client, rclone):  # noqa: F811
    _nas(client, rclone["root"])
    admin = await token(client, "boss", "admin")
    ci = await token(client, "ci-bot", "admin", ci=True)
    developer = await token(client, "dev-bot", "developer")
    v1 = apiv1.PREFIX + "/central-backup"
    assert (await client.get(v1, headers=developer)).json()["error"]["code"] == "forbidden"
    assert (await client.post(v1 + "/key", headers=ci, json={})).json()["error"]["code"] == "forbidden"
    assert (await client.post(v1, headers=admin, json={"destination": "nas", "time": "04:00"})).json()["time"] == "04:00"
    made = (await client.post(v1 + "/key", headers=admin, json={})).json()
    confirmed = (await client.post(v1 + "/key/confirm", headers=admin, json={"passphrase": made["passphrase"]})).json()
    assert confirmed["why_not"] == ""
    run = await client.post(v1 + "/run", headers=admin)
    assert run.status_code == 202 and run.json()["follow"].startswith(apiv1.PREFIX + "/jobs/")
    await wait_job(client, run.json()["job"]["id"])
    # MCP: the same code, the same rules
    tools = {**HEADERS, **admin}
    status = await call(client, "central_backup_status", headers=tools)
    assert status["destination"] == "nas" and status["last"]["status"] == "success"
    assert "error" in await call(client, "central_backup_new_key", headers=tools)
    new = await call(client, "central_backup_new_key", headers=tools, replace=True)
    assert "forbidden" not in json.dumps(new) and len(new["passphrase"].split("-")) == 6
    assert "not the passphrase" in (await call(client, "central_backup_confirm_key", headers=tools,
                                               passphrase="no"))["error"]
    assert (await call(client, "central_backup_confirm_key", headers=tools,
                       passphrase=new["passphrase"]))["why_not"] == ""
    job = await call(client, "central_backup_run", headers=tools, wait_seconds=30)
    assert job["status"] == "success", job
    assert (await call(client, "central_backup_configure", headers=tools, destination="nas", time="05:00",
                       enabled=False))["why_not"] == "switched off"
    # a developer's token does not list them; a link never sets this panel's protection
    from .test_mcp import rpc
    listed = (await rpc(client, "tools/list", headers={**HEADERS, **developer})).json()["result"]["tools"]
    assert not {t["name"] for t in listed} & {"central_backup_status", "central_backup_run"}
    hook = client.app.state.api_hooks["central_backup"]
    code, data = await hook("status", Principal("omnia", "admin", "link"), "link omnia")
    assert code == 403 and "not through a link" in data["error"]
    link = await client.get("/api/central-backup", headers={"Authorization": f"Bearer {LINK_TOKEN}",
                                                             "Accept": "application/json"})
    assert link.status_code == 403
