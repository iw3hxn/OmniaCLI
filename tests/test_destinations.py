# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The remote destinations of the backups: their settings (a secret never
sent back, kept when left empty), the file (mode 600), the copy encrypted
before it leaves, the rotation there, a copy brought back; the pages and
who may use them; a scheduled backup copied out; a failed run told."""

import datetime
import json
import os
import stat
import sys
import textwrap
from pathlib import Path

import pytest

from omniacli import destinations, notify, ops, pullcrypto, schedules
from omniacli.config import ConfigError, Options, Workspace
from omniacli.jobs import Job, JobFailed

from .test_app import AUTH, POST, client, wait_job  # noqa: F401 - the client fixture
from .test_links import as_user
from .test_workspaces import post_as

# rclone, played on a folder: the remote's "host" setting is its root; what
# it was asked, with its environment's settings, goes to calls.jsonl
FAKE_RCLONE = textwrap.dedent('''\
    #!{python}
    import json, os, shutil, sys
    from pathlib import Path
    args = sys.argv[1:]
    env = {{k: v for k, v in os.environ.items() if k.startswith("RCLONE_CONFIG_OMNIACLI_")}}
    with open(os.environ["FAKE_RCLONE_CALLS"], "a") as fh:
        fh.write(json.dumps({{"args": args, "env": env}}) + "\\n")
    if args[0] == "obscure":
        print("OBS:" + sys.stdin.read()); sys.exit(0)
    root = Path(env.get("RCLONE_CONFIG_OMNIACLI_HOST", "/nowhere"))
    if not root.is_dir():
        print("ERROR : couldn't connect: dial tcp: no such host"); sys.exit(1)
    def local(remote):
        return root / remote.split(":", 1)[1]
    paths = [a for a in args[1:] if not a.startswith("-") and not a.isdigit() and a not in ("60s",)]
    if args[0] == "mkdir":
        local(paths[0]).mkdir(parents=True, exist_ok=True)
    elif args[0] == "copyto":
        src, dst = paths
        source = local(src) if src.startswith("omniacli:") else Path(src)
        target = local(dst) if dst.startswith("omniacli:") else Path(dst)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    elif args[0] == "deletefile":
        local(paths[0]).unlink()
    elif args[0] == "lsjson":
        folder = local(paths[0])
        if not folder.is_dir():
            print("ERROR : directory not found"); sys.exit(3)
        print(json.dumps([{{"Name": p.name, "Size": p.stat().st_size, "ModTime": "2026-11-20T03:00:00Z"}}
                          for p in sorted(folder.iterdir()) if p.is_file()]))
    ''')


@pytest.fixture
def rclone(tmp_path, monkeypatch):
    """A fake rclone first in PATH; its root, and what it was asked."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "rclone"
    script.write_text(FAKE_RCLONE.format(python=sys.executable))
    script.chmod(0o755)
    calls = tmp_path / "calls.jsonl"
    path = f"{bindir}:{os.environ.get('PATH', '')}"
    monkeypatch.setenv("PATH", path)
    monkeypatch.setenv("FAKE_RCLONE_CALLS", str(calls))
    monkeypatch.setattr(ops, "_ENV", {**ops._ENV, "PATH": path, "FAKE_RCLONE_CALLS": str(calls)})
    root = tmp_path / "remote"
    root.mkdir()

    def asked():
        return [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
    return {"root": root, "asked": asked}


def _dest(root, name="nas", workspace=""):
    return destinations.parse_destination(name, {
        "kind": "sftp", "path": "backups/odoo", "workspace": workspace,
        "settings": {"host": str(root), "user": "u", "pass": "Secret-pw-1"}, "key": pullcrypto.new_key()})


def _job():
    return Job(id="j", title="t", instances=[], source="s")


def test_the_settings_of_a_kind():
    saved = destinations.check_settings("sftp", {"host": "h", "user": "u", "pass": "pw"})
    assert saved == {"host": "h", "port": "22", "user": "u", "pass": "pw"}
    # a secret left empty keeps the saved one; a checkbox not ticked is off
    assert destinations.check_settings("sftp", {"host": "h2", "user": "u", "pass": ""}, saved)["pass"] == "pw"
    assert "tls" not in destinations.check_settings("ftp", {"host": "h", "user": "u", "pass": "p", "tls": False})
    for kind, given, message in (("sftp", {"host": "h", "user": "u"}, "a password or a private key"),
                                 ("sftp", {"host": "h", "user": "u", "pass": "p", "port": "x"}, "not a number"),
                                 ("drive", {"token": "nope"}, "the JSON rclone authorize prints"),
                                 ("s3", {"provider": "Evil", "access_key_id": "a", "secret_access_key": "s"},
                                  "not one of the choices"),
                                 ("ftp", {"host": "h\nx", "user": "u", "pass": "p"}, "one line"),
                                 ("gopher", {}, "not one of")):
        with pytest.raises(ConfigError, match=message):
            destinations.check_settings(kind, given)
    with pytest.raises(ConfigError, match="without .."):
        destinations.parse_destination("x", {"kind": "sftp", "path": "a/../b", "key": pullcrypto.new_key(),
                                             "settings": {"host": "h", "user": "u", "pass": "p"}})


def test_the_file_is_its_owners_and_secrets_stay_there(tmp_path):
    dest = _dest(tmp_path)
    path = tmp_path / "destinations.yaml"
    destinations.save_destinations(path, {"nas": dest})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert destinations.load_destinations(path) == {"nas": dest}
    shown = dest.public()
    assert shown["settings"]["pass"] == "" and shown["set"] == ["pass"] and "key" not in shown


async def test_a_copy_encrypted_there_rotated_and_brought_back(rclone, tmp_path):
    dest = _dest(rclone["root"])
    local = tmp_path / "local"
    local.mkdir()
    made = {}
    for day in (1, 2, 3):
        backup = local / f"scheduled_db_202611{day:02d}_030000.zip"
        backup.write_bytes(b"PK backup of day %d" % day * 1000)
        made[day] = backup
        assert await destinations.upload(_job(), dest, backup, "srv/production") == backup.name + ".ocpf"
    there = rclone["root"] / "backups" / "odoo" / "srv" / "production"
    copy = there / "scheduled_db_20261103_030000.zip.ocpf"
    # what the provider has: encrypted, nothing left here but the backups
    assert pullcrypto.is_encrypted_file(copy) and b"backup of day" not in copy.read_bytes()
    assert sorted(p.name for p in local.iterdir()) == sorted(p.name for p in made.values())
    # the password reached rclone obscured, in its environment, never on its command line
    copies = [c for c in rclone["asked"]() if c["args"][0] == "copyto"]
    assert copies[0]["env"]["RCLONE_CONFIG_OMNIACLI_PASS"] == "OBS:Secret-pw-1"
    assert not [c for c in rclone["asked"]() if any("Secret-pw-1" in a for a in c["args"])]
    listing = await destinations.listing(dest, "srv/production")
    assert [r["file"] for r in listing][0] == "scheduled_db_20261103_030000.zip"
    assert listing[0]["time"] == "2026-11-03T03:00:00"
    # the rotation there: the newest one day kept, a copy by hand untouched
    (there / "db_by_hand.zip.ocpf").write_bytes(b"x")
    await destinations.rotate(_job(), dest, "srv/production", schedules.SCHEDULED_PREFIX,
                              lambda times: schedules.rotation_kept(times, 1, 0, 0))
    assert sorted(p.name for p in there.iterdir()) == ["db_by_hand.zip.ocpf", copy.name]
    # brought back: decrypted into the backups, as it was
    back = tmp_path / "back"
    got = await destinations.fetch(_job(), dest, "srv/production", copy.name, back)
    assert got.read_bytes() == made[3].read_bytes() and stat.S_IMODE(got.stat().st_mode) == 0o600
    with pytest.raises(JobFailed, match="already in the backups"):
        await destinations.fetch(_job(), dest, "srv/production", copy.name, back)
    # another key cannot read it
    other = destinations.Destination(**{**dest.__dict__, "key": pullcrypto.new_key()})
    with pytest.raises(JobFailed, match="does not decrypt"):
        await destinations.fetch(_job(), other, "srv/production", copy.name, tmp_path / "other")
    assert not list((tmp_path / "other").iterdir())
    for bad in ("../x.zip.ocpf", "x.zip", ".hidden.ocpf"):
        with pytest.raises(JobFailed):
            await destinations.fetch(_job(), dest, "srv/production", bad, back)


async def test_a_private_key_reaches_rclone_on_one_line(rclone):
    dest = destinations.parse_destination("k", {
        "kind": "sftp", "path": "b", "key": pullcrypto.new_key(),
        "settings": {"host": str(rclone["root"]), "user": "u",
                     "key_pem": "-----BEGIN OPENSSH PRIVATE KEY-----\r\nAAAA\nBBBB\n-----END OPENSSH PRIVATE KEY-----\n"}})
    await destinations.check(dest)
    sent = rclone["asked"]()[-1]["env"]["RCLONE_CONFIG_OMNIACLI_KEY_PEM"]
    assert sent == "-----BEGIN OPENSSH PRIVATE KEY-----\\nAAAA\\nBBBB\\n-----END OPENSSH PRIVATE KEY-----"


async def test_check_and_an_unreachable_one(rclone, tmp_path):
    assert await destinations.check(_dest(rclone["root"])) == "0 files in backups/odoo"
    with pytest.raises(JobFailed, match="no such host"):
        await destinations.check(_dest(tmp_path / "missing"))
    assert await destinations.listing(_dest(rclone["root"]), "never/made") == []


async def test_without_rclone(monkeypatch, tmp_path):
    monkeypatch.setattr(destinations, "rclone_path", lambda: None)
    with pytest.raises(JobFailed, match="apt install rclone"):
        await destinations.check(_dest(tmp_path))


def test_a_backup_step_with_a_destination_and_whom_to_tell():
    s = schedules.parse_schedule("b", {"instance": "srv/production", "cron": "0 3 * * *",
                                       "backup": {"destination": "nas"},
                                       "notify": "ops@acme.it, https://hooks.slack.com/services/T/B/x"})
    assert s.backup.destination == "nas" and s.notify == ["ops@acme.it", "https://hooks.slack.com/services/T/B/x"]
    assert schedules.parse_schedule("b", schedules.schedule_to_dict(s)) == s
    for bad, message in (({"backup": {"destination": "../x"}}, "not the name of a destination"),
                         ({"backup": {}, "notify": ["http://plain.example.com"]}, "neither an email")):
        with pytest.raises(ConfigError, match=message):
            schedules.parse_schedule("b", {"instance": "srv/production", "cron": "0 3 * * *", **bad})


async def test_notices_by_webhook_and_mail(monkeypatch):
    import httpx
    posted = []

    def handler(request):
        posted.append((str(request.url), json.loads(request.content)))
        return httpx.Response(200 if "good" in str(request.url) else 500)
    real = httpx.AsyncClient
    monkeypatch.setattr(notify.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    sent = []
    monkeypatch.setattr(notify, "_send_mail", lambda options, to, subject, text: sent.append((to, subject)))
    problems = await notify.send(Options(), ["https://good.example.com/h", "https://bad.example.com/bot123:SECRET/x",
                                             "ops@acme.it"], "Subject", "Text")
    assert posted[0][0] == "https://good.example.com/h" and posted[0][1]["text"] == "Subject\nText"
    assert (posted[0][1]["event"], posted[0][1]["level"], posted[0][1]["detail"]) == ("notice", "warning", "Text")
    # no SMTP server: said; a webhook's token never in what is said
    assert problems == ["no SMTP server on the Options page: the emails are not sent",
                        "webhook to bad.example.com: HTTP 500"]
    assert await notify.send(Options(smtp_host="smtp.acme.it"), ["ops@acme.it"], "S", "T") == []
    assert sent == [(["ops@acme.it"], "S")]


async def test_the_pages_and_who_may(client, env, rclone):
    url = "/api/destinations/save"
    body = {"name": "nas", "kind": "sftp", "path": "backups", "new": True,
            "settings": {"host": str(rclone["root"]), "user": "u", "pass": "Pw-9"}}
    resp = await client.post(url, headers=POST, json=body)
    assert resp.status_code == 200 and resp.json()["new_key"], resp.text
    assert "Pw-9" not in resp.text
    saved = destinations.load_destinations(env["settings"].destinations_file)["nas"]
    assert saved.settings["pass"] == "Pw-9" and saved.key.startswith(pullcrypto.KEY_PREFIX)
    # changed: the password left empty is kept, the key too
    resp = await client.post(url, headers=POST, json={**body, "new": False, "path": "other",
                                                       "settings": {**body["settings"], "pass": ""}})
    again = destinations.load_destinations(env["settings"].destinations_file)["nas"]
    assert resp.status_code == 200 and again.settings["pass"] == "Pw-9" and again.key == saved.key
    assert (await client.post(url, headers=POST, json=body)).status_code == 400  # exists already
    page = (await client.get("/destinations", headers={"Authorization": AUTH})).text
    assert 'data-name="nas"' in page and "Pw-9" not in page and saved.key not in page
    resp = await client.post("/api/destinations/key", headers=POST, json={"name": "nas"})
    assert resp.json()["key"] == saved.key
    assert any(e["event"] == "show destination key" for e in client.app.state.access_log.recent())
    resp = await client.post("/api/destinations/check", headers=POST, json={"name": "nas"})
    assert resp.status_code == 200 and resp.json()["message"] == "0 files in other"
    # a developer does not reach them
    assert (await client.get("/destinations", headers=as_user("dev"))).status_code == 403
    assert (await client.post("/api/destinations/key", headers=post_as("dev"), json={"name": "nas"})).status_code == 403
    # a workspace's: its own route, the machine's one is not there
    env["settings"].workspaces["acme"] = Workspace("acme", servers=["srv"])
    ws_url = "/api/workspaces/acme/destinations"
    resp = await client.post(f"{ws_url}/save", headers=POST, json={**body, "name": "acme-ftp"})
    assert resp.status_code == 200
    assert (await client.post(f"{ws_url}/key", headers=POST, json={"name": "nas"})).status_code == 404
    found = destinations.load_destinations(env["settings"].destinations_file)
    assert found["acme-ftp"].workspace == "acme" and found["nas"].workspace == ""
    page = (await client.get("/workspaces/acme/destinations", headers={"Authorization": AUTH})).text
    assert 'data-name="acme-ftp"' in page and 'data-name="nas"' not in page


async def test_the_automatic_backups_copied_out_and_told(client, env, rclone, monkeypatch):
    destinations.save_destinations(env["settings"].destinations_file, {"nas": _dest(rclone["root"])})
    url = "/api/servers/srv/production/backup-schedule"
    data = (await client.get(url, headers={"Authorization": AUTH})).json()
    assert [d["name"] for d in data["destinations"]] == ["nas"] and data["destination"] == ""
    body = {"enabled": True, "time": "02:30", "daily": 7, "weekly": 4, "monthly": 12, "destination": "nas",
            "notify": ["ops@acme.it"]}
    for bad in ({**body, "destination": "nope"}, {**body, "notify": ["not-an-address"]}):
        assert (await client.post(url, headers=POST, json=bad)).status_code == 400
    resp = await client.post(url, headers=POST, json=body)
    assert resp.status_code == 200 and resp.json()["destination"] == "nas", resp.text
    resp = await client.post("/api/schedules/backup.srv.production/run", headers=POST)
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success", job["lines"]
    there = rclone["root"] / "backups" / "odoo" / "srv" / "production"
    [copy] = list(there.iterdir())
    assert copy.name.startswith(schedules.SCHEDULED_PREFIX) and pullcrypto.is_encrypted_file(copy)
    listing = (await client.get("/api/servers/srv/production/remote-backups", headers={"Authorization": AUTH})).json()
    assert [(f["name"], f["here"]) for f in listing["files"]] == [(copy.name, True)] and listing["can_fetch"]
    # brought back once the one here is gone
    local = env["settings"].backup_dir / "srv" / "production" / copy.name[:-len(".ocpf")]
    plain = local.read_bytes()
    local.unlink()
    resp = await client.post("/api/servers/srv/production/remote-backups/fetch", headers=POST, json={"file": copy.name})
    assert (await wait_job(client, resp.json()["job"]))["status"] == "success" and local.read_bytes() == plain
    # a developer sees a production's copies, does not bring one back
    assert (await client.post("/api/servers/srv/production/remote-backups/fetch", headers=post_as("dev"),
                              json={"file": copy.name})).status_code == 403
    # saved again from the Schedules page, which does not show them: kept
    saved = schedules.load_schedules(env["settings"].schedules_file)["backup.srv.production"]
    form = schedules.schedule_to_dict(saved)
    form.pop("notify"), form["backup"].pop("destination")
    resp = await client.post("/api/schedules/save", headers=POST,
                             json={"original": "backup.srv.production", "name": "backup.srv.production", "schedule": form})
    assert resp.status_code == 200, resp.text
    again = schedules.load_schedules(env["settings"].schedules_file)["backup.srv.production"]
    assert again.backup.destination == "nas" and again.notify == ["ops@acme.it"]
    # the destination in use is not deleted
    resp = await client.post("/api/destinations/delete", headers=POST, json={"name": "nas"})
    assert resp.status_code == 400 and "srv/production" in resp.json()["error"]
    # unreachable: the run fails, whom it names is told
    told = []

    async def send(options, recipients, subject, text, **kwargs):
        told.append((recipients, subject, text))
        return []
    monkeypatch.setattr(notify, "send", send)
    broken = _dest(rclone["root"] / "gone")
    destinations.save_destinations(env["settings"].destinations_file, {"nas": broken})
    resp = await client.post("/api/schedules/backup.srv.production/run", headers=POST)
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "failed" and told and told[0][0] == ["ops@acme.it"]
    assert "failed on srv/production" in told[0][1] and "no such host" in told[0][2]
    assert any("notified: ops@acme.it" in line for line in job["lines"])


async def test_the_mail_of_the_options(client, env):
    body = {"shell": {"font_size": 14}, "tunnel": {"idle_hours": 4, "bind": "127.0.0.1", "ports": "18000-18099"},
            "mail": {"host": "smtp.acme.it", "port": 465, "security": "ssl", "user": "u", "password": "Pw-1",
                     "from": "omniacli@acme.it"}}
    assert (await client.post("/api/options/save", headers=POST, json=body)).status_code == 200
    path = env["settings"].options_file
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and env["settings"].options.smtp_password == "Pw-1"
    # the password left empty is kept; the page never shows it
    body["mail"]["password"] = ""
    assert (await client.post("/api/options/save", headers=POST, json=body)).status_code == 200
    assert env["settings"].options.smtp_password == "Pw-1"
    assert "Pw-1" not in (await client.get("/options", headers={"Authorization": AUTH})).text
    body["mail"]["security"] = "tls13"
    assert (await client.post("/api/options/save", headers=POST, json=body)).status_code == 400
