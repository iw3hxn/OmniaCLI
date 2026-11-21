# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Is a backup good: its SHA-256 written when made and read again, a restore
tried in a database of its own (the helper's verify), the result kept and
shown; the automatic backups try one on their day."""

import datetime
import json
import stat

import pytest

from omniacli import app as app_module
from omniacli import backupcheck, composehelper, schedules
from omniacli import docker as docker_plugin
from omniacli.config import ConfigError, parse_servers
from omniacli.jobs import Job, JobFailed

from .test_app import AUTH, POST, client, wait_job  # noqa: F401 - the client fixture
from .test_workspaces import post_as


def _job():
    return Job(id="j", title="t", instances=[], source="s")


class _Plugin:
    has_verify = True

    def __init__(self, answer=None, error=None):
        self.answer, self.error, self.tried = answer or {}, error, []

    async def verify_backup(self, job, instance, path):
        self.tried.append(path.name)
        if self.error:
            raise JobFailed(self.error)
        return self.answer


class _Instance:
    def __init__(self, plugin):
        self.plugin = plugin


async def test_the_checksum_and_a_restore_tried(tmp_path):
    zip_path = tmp_path / "db_1.zip"
    zip_path.write_bytes(b"PK one")
    await backupcheck.record(zip_path)
    assert stat.S_IMODE((tmp_path / backupcheck.INDEX).stat().st_mode) == 0o600
    good = _Plugin({"modules": 80, "users": 4, "companies": 1, "last": "2026-11-19 23:58"})
    result = await backupcheck.verify(_job(), _Instance(good), zip_path)
    assert result["ok"] and result["modules"] == 80 and good.tried == ["db_1.zip"]
    assert backupcheck.state(tmp_path, "db_1.zip") == {"sha256": True, "verified": result}
    # changed on the disk since: told, no restore tried, the failure kept
    zip_path.write_bytes(b"PK one, damaged")
    with pytest.raises(JobFailed, match="changed since it was made"):
        await backupcheck.verify(_job(), _Instance(good), zip_path)
    assert good.tried == ["db_1.zip"]
    failed = backupcheck.state(tmp_path, "db_1.zip")["verified"]
    assert not failed["ok"] and "changed since" in failed["detail"]
    # a restore that fails is kept as failed; a renamed backup keeps its entry
    other = tmp_path / "db_2.zip"
    other.write_bytes(b"PK two")
    await backupcheck.record(other)
    with pytest.raises(JobFailed, match="does not load"):
        await backupcheck.verify(_job(), _Instance(_Plugin(error="does not load")), other)
    other.rename(tmp_path / "scheduled_db_2.zip")
    backupcheck.rename(tmp_path, "db_2.zip", "scheduled_db_2.zip")
    assert backupcheck.state(tmp_path, "scheduled_db_2.zip")["verified"]["detail"] == "does not load"
    # a deleted backup leaves the index at the next write
    zip_path.unlink()
    await backupcheck.record(tmp_path / "scheduled_db_2.zip")
    assert set(backupcheck.load(tmp_path)) == {"scheduled_db_2.zip"}


def test_the_helpers_verify(tmp_path, monkeypatch):
    monkeypatch.setattr(composehelper, "services", lambda project: ["odoo", "db"])
    [cmd] = composehelper.commands(tmp_path, "verify", ["odoo"])
    assert cmd[:3] == ["run", "--rm", "-T"] and cmd[3] == "-e" and cmd[4].startswith("OMNIACLI_CHECK=")
    assert "verify_check.py" not in cmd[4] and "OMNIACLI-VERIFY" in cmd[4]
    script = cmd[cmd.index("-c") + 1]
    # a database of its own, dropped on the way out whatever happens; neutralized
    assert 'd="${OMNIACLI_VERIFY:-omniacli_verify_}' in script and "trap 'odoo db drop \"$d\"" in script
    assert 'odoo db load --neutralize "$d"' in script
    with pytest.raises(composehelper.HelperError):
        composehelper.commands(tmp_path, "verify", ["odoo", "x"])
    # the packages of the archive: in the running service, as its user
    assert composehelper.commands(tmp_path, "pip-freeze", ["odoo"]) == [
        ["exec", "-T", "odoo", "sh", "-c", "exec python3 -m pip freeze --user --disable-pip-version-check", "sh"]]


async def test_the_docker_plugin_reads_the_answer(monkeypatch, tmp_path):
    data = {"type": "docker", "test": {"restart": {"type": "compose", "service": str(tmp_path)},
                                       "repositories": [], "database": "main"}}
    inst = parse_servers({"s": data})["s"].test

    async def helper(job, instance, action, *args, stdin=None, **kw):
        assert action == "verify" and args == ("odoo",) and stdin.name == "b.zip"
        job.log("odoo: loading")
        job.log('OMNIACLI-VERIFY {"modules": 12, "users": 2, "companies": 1, "last": null}')

    async def major(instance):
        return 19
    monkeypatch.setattr(docker_plugin, "_helper", helper)
    monkeypatch.setattr(docker_plugin, "instance_major", major)
    zip_path = tmp_path / "b.zip"
    zip_path.write_bytes(b"x")
    assert await inst.plugin.verify_backup(_job(), inst, zip_path) == {
        "modules": 12, "users": 2, "companies": 1, "last": None}

    async def silent(job, instance, action, *args, **kw):
        job.log("nothing")
    monkeypatch.setattr(docker_plugin, "_helper", silent)
    with pytest.raises(JobFailed, match="could not be read"):
        await inst.plugin.verify_backup(_job(), inst, zip_path)


def test_the_day_of_the_try():
    s = schedules.parse_schedule("b", {"instance": "srv/production", "cron": "0 3 * * *",
                                       "backup": {"verify_weekday": 6}})
    assert s.backup.verify_weekday == 6 and schedules.parse_schedule("b", schedules.schedule_to_dict(s)) == s
    with pytest.raises(ConfigError, match="day of the week"):
        schedules.parse_schedule("b", {"instance": "srv/production", "cron": "0 3 * * *",
                                       "backup": {"verify_weekday": 7}})


async def test_verified_from_the_page_and_on_its_day(client, env, monkeypatch):
    plugin = type(env["settings"].servers["srv"].production.plugin)
    tried = []

    async def verify_backup(self, job, instance, path):
        tried.append(path.name)
        return {"modules": 9, "users": 1, "companies": 1, "last": "2026-11-20 03:00"}
    monkeypatch.setattr(plugin, "has_verify", property(lambda self: True))
    monkeypatch.setattr(plugin, "verify_backup", verify_backup)
    url = "/api/servers/srv/production/backup-schedule"
    body = {"enabled": True, "time": "02:30", "daily": 7, "weekly": 4, "monthly": 12, "verify": True}
    data = (await client.post(url, headers=POST, json=body)).json()
    assert data["verify"] and data["has_verify"] and data["verified"] is None
    sch = schedules.load_schedules(env["settings"].schedules_file)["backup.srv.production"]
    assert sch.backup.verify_weekday == 6
    # on its day the backup made is tried (today, here)
    monkeypatch.setattr(app_module, "VERIFY_WEEKDAY", datetime.date.today().weekday())
    assert (await client.post(url, headers=POST, json=body)).status_code == 200
    resp = await client.post("/api/schedules/backup.srv.production/run", headers=POST)
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success", job["lines"]
    listing = (await client.get("/api/servers/srv/production/backups", headers={"Authorization": AUTH})).json()
    assert listing[0]["sha256"] and listing[0]["verified"]["ok"] and listing[0]["verified"]["modules"] == 9
    assert tried == [listing[0]["file"]]
    assert (await client.get(url, headers={"Authorization": AUTH})).json()["verified"]["file"] == listing[0]["file"]
    # by hand, from the list
    resp = await client.post("/api/servers/srv/production/backups/verify", headers=POST,
                             json={"file": listing[0]["file"]})
    assert (await wait_job(client, resp.json()["job"]))["status"] == "success" and len(tried) == 2
    for bad in ("../x.zip", "nope.zip"):
        resp = await client.post("/api/servers/srv/production/backups/verify", headers=POST, json={"file": bad})
        assert resp.status_code == 404
    # a viewer may not
    resp = await client.post("/api/servers/srv/production/backups/verify", headers=post_as("viewer"),
                             json={"file": listing[0]["file"]})
    assert resp.status_code == 403


async def test_the_full_archive(client, env, monkeypatch):
    """The backup, each repository's code at its commit (an address without
    its token), the plugin's files, a manifest with every part's SHA-256;
    restored and tried through its backup."""
    import subprocess
    import tarfile
    import zipfile
    from omniacli import instancearchive
    instance = env["settings"].servers["srv"].production
    plugin = type(instance.plugin)

    async def archive_files(self, job, inst):
        return [("odoo.conf", b"[options]\nworkers = 2\n"), ("pip-freeze.txt", b"requests==2.32.0\n")]
    monkeypatch.setattr(plugin, "archive_files", archive_files)
    for repo in instance.repositories:
        subprocess.run(["git", "remote", "set-url", repo.remote, "https://bot:SECRET@github.com/acme/x.git"],
                       cwd=repo.path, check=True)
    resp = await client.post("/api/servers/srv/production/archive", headers=POST, json={})
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success", job["lines"]
    listing = (await client.get("/api/servers/srv/production/backups", headers={"Authorization": AUTH})).json()
    [row] = [f for f in listing if f["archive"]]
    path = env["settings"].backup_dir / "srv" / "production" / row["file"]
    assert row["file"].startswith(instancearchive.PREFIX) and row["sha256"] and stat.S_IMODE(path.stat().st_mode) == 0o600
    manifest = instancearchive.read_manifest(path)
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        repos = json.loads(zf.read("repositories.json"))
        assert zf.getinfo("backup.zip").file_size > 0
        first = repos[0]
        with tarfile.open(fileobj=zf.open(first["code"]), mode="r:gz") as tar:
            assert tar.getnames()
    assert {"manifest.json", "backup.zip", "repositories.json", "config/odoo.conf", "config/pip-freeze.txt"} <= names
    assert first["url"] == "https://github.com/acme/x.git" and len(first["commit"]) == 40 and "SECRET" not in str(repos)
    assert set(manifest["files"]) == names - {"manifest.json"} and instancearchive.check_parts(path) == []
    # restored: the backup inside goes to the plugin
    restored = []

    async def restore(self, job, inst, backup_zip, options):
        restored.append(backup_zip.name)
    monkeypatch.setattr(plugin, "restore", restore)
    monkeypatch.setattr(plugin, "backup_problem", lambda self, p: None if p.name == "backup.zip" else "not a backup")
    resp = await client.post("/servers/srv/production/restore", headers=POST,
                             data={"backup": row["file"], "confirm": instance.plugin.describe(instance).get("database") or "srv/production"})
    assert resp.status_code == 200, resp.text
    assert (await wait_job(client, resp.json()["job"]))["status"] == "success" and restored == ["backup.zip"]
    # tried: the parts against the manifest, then its backup
    tried = []

    async def verify_backup(self, job, inst, p):
        tried.append(p.name)
        return {"modules": 1, "users": 1, "companies": 1, "last": None}
    monkeypatch.setattr(plugin, "has_verify", property(lambda self: True))
    monkeypatch.setattr(plugin, "verify_backup", verify_backup)
    resp = await client.post("/api/servers/srv/production/backups/verify", headers=POST, json={"file": row["file"]})
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success" and tried == ["backup.zip"]
    assert any("every part of the archive" in line for line in job["lines"])


def test_pip_freeze_keeps_the_packages_only():
    from omniacli.plugin_api import pip_lines
    out = ("WARNING: Skipping /usr/lib/python3.12/dist-packages/x.dist-info due to invalid metadata\n"
           "requests==2.32.0\nDEPRECATION: something\nzeep==4.3.1\n")
    assert pip_lines(out) == b"requests==2.32.0\nzeep==4.3.1\n"
