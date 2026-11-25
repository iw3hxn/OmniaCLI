# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""OmniaCLI updated from its own panel (README, Update OmniaCLI;
selfupdate.py): the source https only, a package installed only when its
signed manifest verifies with the machine's key and it is the file signed,
the installed package kept for the way back (signed, or the release's
package checked file by file against what dpkg installed), the install
started in a unit of its own, its outcome read by the OmniaCLI that comes
back and the job finished with it, the routes for the machine's admins
only, the name of a machine that serves productions typed. Real small
packages made with dpkg-deb; the download server, dpkg's state, sudo and
systemd-run are fakes: nothing is installed here."""

import asyncio
import base64
import datetime
import hashlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

import httpx
import pytest

from omniacli import selfupdate
from omniacli.app import create_app
from omniacli.auth import hash_password
from omniacli.config import Access, Workspace, parse_settings
from omniacli.jobs import INTERRUPTED, SUCCESS, FAILED, JobFailed, JobManager
from omniacli.packagesign import load_private_key, new_private_key, sign_file
from omniacli.selfupdate_unit import Unit

from .conftest import PASSWORD, USER_HASH, BasicAsSession, hash_token

URL = "https://pkgs.test/latest/OmniaCLI.deb"
VERSION_URL = "https://pkgs.test/omniacli-v{version}/OmniaCLI.deb"
HOST = "lab3.test"
ADMIN = {"Authorization": "Basic " + base64.b64encode(f"admin:{PASSWORD}".encode()).decode(), "Origin": f"http://{HOST}"}
MARIO = {"Authorization": "Basic " + base64.b64encode(f"mario:{PASSWORD}".encode()).decode(), "Origin": f"http://{HOST}"}
CENTRAL_TOKEN = "central-" + "c" * 40
ALONE = {"Authorization": f"Bearer {CENTRAL_TOKEN}", "X-OmniaCLI-Prefix": "/links/lab3", "X-OmniaCLI-User": "carlo",
         "X-OmniaCLI-Role-Cap": "admin"}
PERSON = {**ALONE, "X-OmniaCLI-Menu": "servers,instances,backups,settings"}


def a_key():
    pem, public = new_private_key(b"pass")
    return load_private_key(pem, b"pass"), public


def make_deb(folder: Path, version: str, wheel: bytes = b"wheel", postinst: str = "#!/bin/sh\nexit 0\n",
             name: str = "omniacli") -> Path:
    """A package as build_deb.sh makes it, small: a file, a configuration file, a link, its scripts."""
    folder = Path(tempfile.mkdtemp(dir=folder))
    tree = folder / "tree"
    (tree / "DEBIAN").mkdir(parents=True)
    (tree / "DEBIAN" / "control").write_text(f"Package: {name}\nVersion: {version}\nArchitecture: all\n"
                                             "Maintainer: t <t@t>\nDescription: t\n")
    (tree / "DEBIAN" / "postinst").write_text(postinst)
    (tree / "DEBIAN" / "postinst").chmod(0o755)
    (tree / "DEBIAN" / "conffiles").write_text("/etc/default/omniacli\n")
    (tree / "opt/omniacli/dist").mkdir(parents=True)
    (tree / "opt/omniacli/dist/omniacli.whl").write_bytes(wheel)
    (tree / "etc/default").mkdir(parents=True)
    (tree / "etc/default/omniacli").write_text("OMNIACLI_PORT=8090\n")
    (tree / "usr/bin").mkdir(parents=True)
    os.symlink("/opt/omniacli/venv/bin/omniacli", tree / "usr/bin/omniacli")
    (tree / "usr/share/doc/omniacli").mkdir(parents=True)
    (tree / "usr/share/doc/omniacli/README.md").write_text("# OmniaCLI\n")
    deb = folder / f"{name}_{version}.deb"
    subprocess.run(["dpkg-deb", "--root-owner-group", "-Zgzip", "--build", str(tree), str(deb)], check=True,
                   capture_output=True)
    return deb


class Server:
    """The download server: {url: bytes}, redirects {url: location}; what was asked."""

    def __init__(self):
        self.files, self.redirects, self.asked = {}, {}, []

    def publish(self, url: str, deb: Path, key=None):
        """The package at url, as OmniaCLI.deb, with its signed manifest when key is given."""
        published = deb.parent / "published" / url.split("/")[-2]
        published.mkdir(parents=True, exist_ok=True)
        copy = published / "OmniaCLI.deb"
        copy.write_bytes(deb.read_bytes())
        self.files[url] = copy.read_bytes()
        if key is not None:
            version = subprocess.run(["dpkg-deb", "-f", str(deb), "Version"], capture_output=True, text=True).stdout.strip()
            for path in sign_file(key, copy, version, "omniacli"):
                self.files[url + path.name[len("OmniaCLI.deb"):]] = path.read_bytes()

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.asked.append(url)
        if url in self.redirects:
            return httpx.Response(302, headers={"location": self.redirects[url]})
        if url in self.files:
            return httpx.Response(200, content=self.files[url])
        return httpx.Response(404)


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """5501 installed (dpkg's state and its files in folders of the test), a server publishing 5502 signed."""
    key, public = a_key()
    server = Server()
    root, info, state = tmp_path / "root", tmp_path / "dpkg-info", tmp_path / "state"
    info.mkdir()
    old = make_deb(tmp_path, "2026.1.5501", wheel=b"old wheel")
    # what dpkg installed of 5501: its files, its scripts, its configuration file changed here since
    subprocess.run(["dpkg-deb", "-x", str(old), str(root)], check=True)
    (root / "etc/default/omniacli").write_text("OMNIACLI_PORT=8090\nOMNIACLI_WORKSPACES=1\n")
    # a minimal image: dpkg left the documentation out (path-exclude, as Ubuntu's container images)
    (root / "usr/share/doc/omniacli/README.md").unlink()
    dpkg_config = tmp_path / "dpkg"
    (dpkg_config / "dpkg.cfg.d").mkdir(parents=True)
    (dpkg_config / "dpkg.cfg.d" / "excludes").write_text("path-exclude=/usr/share/doc/*\npath-include=/usr/share/doc/*/copyright\n")
    (info / "omniacli.postinst").write_text("#!/bin/sh\nexit 0\n")
    (info / "omniacli.list").write_text("/.\n")
    conffile_md5 = hashlib.md5(b"OMNIACLI_PORT=8090\n").hexdigest()  # noqa: S324 - as dpkg records it
    new = make_deb(tmp_path, "2026.1.5502", wheel=b"new wheel")
    server.publish(URL, new, key)
    server.publish(VERSION_URL.format(version="2026.1.5501"), old)
    installed = {"version": "2026.1.5501"}
    runs = []

    def run(cmd, timeout=120):
        if cmd[0] == "systemd-run":
            runs.append(cmd)
            return 0, "Running as unit"
        return selfupdate_run(cmd, timeout)

    selfupdate_run = selfupdate._run
    monkeypatch.setattr(selfupdate, "_client", lambda: httpx.Client(transport=httpx.MockTransport(server.handler)))
    monkeypatch.setattr(selfupdate, "installed_version", lambda: installed["version"])
    monkeypatch.setattr(selfupdate, "_conffiles", lambda: {"/etc/default/omniacli": conffile_md5})
    monkeypatch.setattr(selfupdate, "DPKG_INFO", info)
    monkeypatch.setattr(selfupdate, "INSTALLED_ROOT", root)
    monkeypatch.setattr(selfupdate, "DPKG_CONFIG", dpkg_config)
    monkeypatch.setattr(selfupdate, "STATE", state)
    monkeypatch.setattr(selfupdate, "_run", run)
    source = selfupdate.Source(URL, public, VERSION_URL, "")
    return {"key": key, "public": public, "server": server, "state": state, "source": source, "installed": installed,
            "runs": runs, "root": root, "info": info, "tmp": tmp_path, "old": old}


def test_the_source_https_only_and_its_defaults():
    found = selfupdate.parse_source({}, "ed25519:x")
    # the package's own releases: their own key, never a plugin publisher's
    assert found.url == selfupdate.DEFAULT_URL and found.public_key == selfupdate.RELEASES_PUBLIC_KEY
    assert selfupdate.parse_source({}).public_key == selfupdate.RELEASES_PUBLIC_KEY
    other = selfupdate.parse_source({"url": "https://github.com/someone/fork/releases/latest/download/OmniaCLI.deb"},
                                    "ed25519:x")
    assert other.public_key == "ed25519:x"
    assert found.version_url == ("https://github.com/iw3hxn/OmniaCLI/releases/download/omniacli-v{version}/OmniaCLI.deb")
    assert found.releases == "https://api.github.com/repos/iw3hxn/OmniaCLI/releases?per_page=100"
    own = selfupdate.parse_source({"url": URL, "public_key": "ed25519:y", "version_url": VERSION_URL}, "ed25519:x")
    assert (own.public_key, own.version_url, own.releases) == ("ed25519:y", VERSION_URL, "")
    for bad in ({"url": "http://pkgs.test/OmniaCLI.deb"}, {"url": "ftp://x/y"}, {"url": "https://x/a b"},
                {"version_url": "https://pkgs.test/OmniaCLI.deb"}, {"releases": "http://api/x"}):
        with pytest.raises(selfupdate.SelfUpdateError):
            selfupdate.parse_source(bad)


def test_versions_are_compared_number_by_number():
    assert selfupdate.newer("2026.1.5502", "2026.1.5477") and not selfupdate.newer("2026.1.5477", "2026.1.5502")
    assert selfupdate.newer("2026.1.10", "2026.1.9") and selfupdate.newer("2026.1.0", "2026.1.0~check")
    assert not selfupdate.newer("2026.1.5501", "2026.1.5501")


def test_a_start_time_is_a_moment_of_this_machine():
    """systemd's @seconds made a moment of this machine's clock (the pages move it to the reader's zone); not
    "Mon 2026-11-23 13:56:38 UTC", whose time a page would move and whose UTC it would leave."""
    stamp = 1795960598
    assert selfupdate.moment(f"@{stamp}") == datetime.datetime.fromtimestamp(stamp).isoformat(timespec="seconds")
    assert selfupdate.moment("2026-11-23T13:56:38.1Z") == "2026-11-23T13:56:38.1Z"


def test_prepare_downloads_verifies_and_keeps_the_way_back(machine):
    done = selfupdate.prepare(machine["source"], machine["state"])
    assert (done["from"], done["to"], done["previous"]["how"]) == ("2026.1.5501", "2026.1.5502", "installed")
    kept = selfupdate.kept(machine["state"])
    assert set(kept) == {"2026.1.5501", "2026.1.5502"} and kept["2026.1.5502"]["how"] == "signed"
    # the version before: the release's package, unsigned, believed because it is the installed one file by file
    assert (machine["state"] / "packages/2026.1.5501/OmniaCLI.deb").read_bytes() == \
        machine["server"].files[VERSION_URL.format(version="2026.1.5501")]
    assert json.loads((machine["state"] / "pending.json").read_text())["to"] == "2026.1.5502"
    # the install, in a transient unit of its own, with the run that unit reads
    started = selfupdate.start("update", "20261123-101500-abcdef", machine["source"], machine["state"])
    assert started == {"unit": "omniacli-self-update-20261123-101500-abcdef", "from": "2026.1.5501",
                       "to": "2026.1.5502", "mode": "update"}
    cmd = machine["runs"][-1]
    assert cmd[:4] == ["systemd-run", "--unit", started["unit"], "--collect"] and cmd[-3] == "-I"
    run = json.loads((machine["state"] / "runs/20261123-101500-abcdef.json").read_text())
    assert run["deb"].endswith("2026.1.5502/OmniaCLI.deb") and run["previous_deb"].endswith("2026.1.5501/OmniaCLI.deb")
    assert (machine["state"] / "unit.py").read_text() == selfupdate.UNIT_SCRIPT.read_text()
    assert not (machine["state"] / "pending.json").exists()
    # a job id is a job id
    with pytest.raises(selfupdate.SelfUpdateError, match="is not a job"):
        selfupdate.start("update", "x; rm -rf /", machine["source"], machine["state"])


def test_a_package_signed_with_another_key_is_refused(machine):
    other, _ = a_key()
    machine["server"].publish(URL, make_deb(machine["tmp"], "2026.1.5503"), other)
    with pytest.raises(selfupdate.SelfUpdateError, match="does not verify with the public key"):
        selfupdate.prepare(machine["source"], machine["state"])
    assert not (machine["state"] / "packages" / "2026.1.5503").exists()
    # the service says it too, and offers nothing to install
    found = selfupdate.published(machine["source"], "2026.1.5501")
    assert found["signed"] is False and "does not verify" in found["error"]


def test_a_download_that_is_not_the_signed_file_is_refused(machine):
    server = machine["server"]
    # one byte changed: the same size, another sha256
    server.files[URL] = server.files[URL][:-1] + bytes([server.files[URL][-1] ^ 1])
    with pytest.raises(selfupdate.SelfUpdateError, match="not the file the manifest signs"):
        selfupdate.prepare(machine["source"], machine["state"])
    assert not (machine["state"] / "packages" / "2026.1.5502").exists()
    assert not list((machine["state"] / "packages").rglob("*.part"))


def test_a_download_sent_to_http_or_without_signature_is_refused(machine):
    server = machine["server"]
    server.redirects[URL + ".json"] = "http://evil.test/OmniaCLI.deb.json"
    with pytest.raises(selfupdate.SelfUpdateError, match="not https, refused"):
        selfupdate.prepare(machine["source"], machine["state"])
    del server.redirects[URL + ".json"]
    del server.files[URL + ".json"]
    with pytest.raises(selfupdate.SelfUpdateError, match="an unsigned package is never installed"):
        selfupdate.prepare(machine["source"], machine["state"])
    # nothing newer: refused before any download
    server.publish(URL, make_deb(machine["tmp"], "2026.1.5501", wheel=b"old wheel"), machine["key"])
    with pytest.raises(selfupdate.SelfUpdateError, match="nothing newer"):
        selfupdate.prepare(machine["source"], machine["state"])


def test_dpkg_leaves_out_what_its_configuration_excludes(machine, tmp_path):
    rules = selfupdate.dpkg_rules(tmp_path / "dpkg")
    assert selfupdate.dpkg_excluded("/usr/share/doc/omniacli/README.md", rules)
    assert not selfupdate.dpkg_excluded("/usr/share/doc/omniacli/copyright", rules)
    assert not selfupdate.dpkg_excluded("/opt/omniacli/dist/omniacli.whl", rules)
    # without the rule a file missing here is a file that is not the installed one
    (tmp_path / "dpkg" / "dpkg.cfg.d" / "excludes").unlink()
    with pytest.raises(selfupdate.SelfUpdateError, match="/usr/share/doc/omniacli/README.md is not the installed file"):
        selfupdate.prepare(machine["source"], machine["state"])


def test_no_way_back_no_update(machine):
    """The release's package of the installed version is not the installed one: not kept, not updated."""
    machine["server"].publish(VERSION_URL.format(version="2026.1.5501"),
                              make_deb(machine["tmp"], "2026.1.5501", wheel=b"another wheel"))
    with pytest.raises(selfupdate.SelfUpdateError, match="/opt/omniacli/dist/omniacli.whl is not the installed file"):
        selfupdate.prepare(machine["source"], machine["state"])
    assert selfupdate.kept(machine["state"]) == {}
    # a maintainer script changed: refused too
    (machine["info"] / "omniacli.postinst").write_text("#!/bin/sh\necho changed\n")
    machine["server"].publish(VERSION_URL.format(version="2026.1.5501"), machine["old"])
    with pytest.raises(selfupdate.SelfUpdateError, match="its postinst is not the installed one"):
        selfupdate.prepare(machine["source"], machine["state"])


def test_the_way_back(machine):
    selfupdate.prepare(machine["source"], machine["state"])
    selfupdate.start("update", "20261123-101500-abcdef", machine["source"], machine["state"])
    machine["installed"]["version"] = "2026.1.5502"
    os.utime(machine["info"] / "omniacli.list")
    back = selfupdate.way_back("2026.1.5502", machine["state"], machine["info"])
    assert back["version"] == "2026.1.5501"
    started = selfupdate.start("back", "20261123-102000-abcdef", machine["source"], machine["state"])
    assert (started["from"], started["to"], started["mode"]) == ("2026.1.5502", "2026.1.5501", "back")
    run = json.loads((machine["state"] / "runs/20261123-102000-abcdef.json").read_text())
    assert run["deb"].endswith("2026.1.5501/OmniaCLI.deb") and run["previous_deb"] == ""
    # a kept package changed since: not installed
    deb = machine["state"] / "packages/2026.1.5501/OmniaCLI.deb"
    deb.write_bytes(deb.read_bytes() + b"x")
    with pytest.raises(selfupdate.SelfUpdateError, match="changed since it was kept"):
        selfupdate.start("back", "20261123-102500-abcdef", machine["source"], machine["state"])
    # after KEEP_DAYS it is no longer offered
    past = time.time() - (selfupdate.KEEP_DAYS + 1) * 86400
    os.utime(machine["info"] / "omniacli.list", (past, past))
    assert selfupdate.way_back("2026.1.5502", machine["state"], machine["info"]) is None


def test_the_notes_of_the_versions_in_between(machine):
    releases = [{"tag_name": f"omniacli-v2026.1.{n}", "name": f"OmniaCLI 2026.1.{n}", "body": f"notes {n}",
                 "html_url": f"https://gh/{n}", "published_at": "2026-11-23T10:00:00Z"} for n in (5500, 5501, 5502, 5503)]
    machine["server"].files["https://api.test/releases"] = json.dumps(releases).encode()
    source = selfupdate.Source(URL, machine["public"], VERSION_URL, "https://api.test/releases")
    found = selfupdate.published(source, "2026.1.5500")
    assert (found["version"], found["signed"], found["error"]) == ("2026.1.5502", True, None)
    assert [n["version"] for n in found["notes"]] == ["2026.1.5502", "2026.1.5501"]
    # unsigned: the latest release's version, said as not installable
    del machine["server"].files[URL + ".json"]
    found = selfupdate.published(source, "2026.1.5500")
    assert found["version"] == "2026.1.5503" and found["signed"] is False and "cannot be installed from here" in found["error"]


def outcome(job_id, ok=True, mode="update", **extra):
    facts = {"omniacli": {"version": "2026.1.5501", "state": "active"}, "login": 200, "units": {},
             "containers": {"acme-prod-odoo-1": "2026-11-23T08:00:00Z"}, "nginx": {"test": "ok", "files": "abc"}}
    after = json.loads(json.dumps(facts))
    after["omniacli"]["version"] = "2026.1.5502"
    return {"job": job_id, "mode": mode, "from": "2026.1.5501", "to": "2026.1.5502", "ok": ok, "error": "" if ok else
            "apt-get ended with 100", "warnings": [], "went_back": not ok, "before": facts, "after": after,
            "apt_exit": 0 if ok else 100, "apt_tail": ["E: boom"], "away_seconds": 23.4, "install_seconds": 41.0, **extra}


async def test_the_outcome_is_read_by_the_omniacli_that_comes_back(tmp_path):
    state, recorded = tmp_path / "state", []
    (state / "runs").mkdir(parents=True)
    jobs = JobManager(tmp_path / "jobs")
    calls = []

    async def helper(*args):
        calls.append(args)
        if args[0] == "prepare":
            return {"from": "2026.1.5501", "to": "2026.1.5502", "size": 10, "sha256": "a" * 64, "built": None,
                    "previous": {"version": "2026.1.5501", "how": "installed"}}
        (state / "runs" / f"{args[2]}.json").write_text(json.dumps({"job": args[2], "mode": args[1]}))
        return {"unit": f"omniacli-self-update-{args[2]}", "from": "2026.1.5501", "to": "2026.1.5502", "mode": args[1]}

    async def action(job):
        await selfupdate.run_job(job, "update", recorded.append, helper=helper, state=state)

    job = jobs.submit(selfupdate.title("update", "2026.1.5501", "2026.1.5502"), [], "admin", action)
    for _ in range(100):
        if calls and len(calls) == 2 and any("install OmniaCLI" in line for line in job.lines):
            break
        await asyncio.sleep(0.01)
    assert [c[0] for c in calls] == ["prepare", "start"] and calls[1][1:] == ("update", job.id)
    assert recorded == ["OmniaCLI update 2026.1.5501 -> 2026.1.5502: downloaded and verified (sha256 " + "a" * 64 + ")"]
    # OmniaCLI stops (apt): its task ends, the job is interrupted; the OmniaCLI that comes back reads it so
    jobs._running[job.id].cancel()
    await asyncio.sleep(0.05)
    again = JobManager(tmp_path / "jobs")
    assert again.get(job.id).status == INTERRUPTED
    assert [j.id for j in selfupdate.to_resume(again, state)] == [job.id]
    (state / "runs" / f"{job.id}.log").write_text("10:15:03 --- the facts before\n10:15:04 --- install OmniaCLI 2026.1.5502 (it was 2026.1.5501)\n")
    (state / "runs" / f"{job.id}.outcome.json").write_text(json.dumps(outcome(job.id)))
    resumed = again.get(job.id)
    again.resume(resumed, lambda j: selfupdate.wait_outcome(j, recorded.append, state=state, every=0.01), "--- back")
    for _ in range(200):
        if resumed.finished_flag:
            break
        await asyncio.sleep(0.01)
    assert resumed.status == SUCCESS and resumed.started == job.started
    text = "\n".join(resumed.lines)
    assert "--- install OmniaCLI 2026.1.5502 (it was 2026.1.5501)" in text
    assert "--- the facts after: OmniaCLI 2026.1.5502 active, login page HTTP 200, nginx -t ok" in text
    assert "container acme-prod-odoo-1 started 2026-11-23T08:00:00Z" in text and "the panel was away 23 s" in text
    assert recorded[-1] == "OmniaCLI 2026.1.5501 -> 2026.1.5502 installed"
    assert resumed.result["self_update"]["done"] and resumed.result["self_update"]["away_seconds"] == 23.4
    # read once: not resumed again
    assert selfupdate.to_resume(again, state) == []


async def test_a_failed_install_fails_the_job_and_says_the_way_back(tmp_path):
    state, recorded = tmp_path / "state", []
    (state / "runs").mkdir(parents=True)
    jobs = JobManager(tmp_path / "jobs")
    (state / "runs" / "x").write_text("")
    job_holder = {}

    async def action(job):
        job_holder["job"] = job
        (state / "runs" / f"{job.id}.outcome.json").write_text(json.dumps(outcome(job.id, ok=False)))
        await selfupdate.wait_outcome(job, recorded.append, state=state, every=0.01)

    job = jobs.submit("Update OmniaCLI 2026.1.5501 → 2026.1.5502", [], "admin", action)
    for _ in range(200):
        if job.finished_flag:
            break
        await asyncio.sleep(0.01)
    assert job.status == FAILED and job.error == "apt-get ended with 100: OmniaCLI 2026.1.5501 was installed again"
    assert "apt: E: boom" in "\n".join(job.lines)
    assert recorded == ["OmniaCLI update 2026.1.5501 -> 2026.1.5502 failed: apt-get ended with 100; back to 2026.1.5501"]


def test_the_unit_installs_and_goes_back_when_the_install_fails(tmp_path, monkeypatch):
    run_file = tmp_path / "20261123-101500-abcdef.json"
    run_file.write_text(json.dumps({"job": "20261123-101500-abcdef", "mode": "update", "from": "2026.1.5501",
                                    "to": "2026.1.5502", "deb": "/k/new.deb", "previous_deb": "/k/old.deb", "port": 1}))
    installed = {"v": "2026.1.5501"}
    apt_calls = []

    def fake_out(cmd, timeout=60):
        if cmd[0] == "dpkg-query":
            return 0, installed["v"]
        if cmd[:2] == ["systemctl", "is-active"]:
            return 0, "active"
        return 1, ""

    def fake_apt(self, deb, downgrade):
        apt_calls.append((deb, downgrade))
        if deb == "/k/new.deb" and fail["new"]:
            return 100
        installed["v"] = "2026.1.5502" if deb == "/k/new.deb" else "2026.1.5501"
        return 0

    fail = {"new": False}
    monkeypatch.setattr(Unit, "out", staticmethod(fake_out))
    monkeypatch.setattr(Unit, "apt", fake_apt)
    monkeypatch.setattr(Unit, "login_code", lambda self, timeout=5: 200)
    monkeypatch.setattr("omniacli.selfupdate_unit.os.path.isdir", lambda p: False)
    assert Unit(str(run_file)).main() == 0
    done = json.loads((tmp_path / "20261123-101500-abcdef.outcome.json").read_text())
    assert done["ok"] and done["after"]["omniacli"]["version"] == "2026.1.5502" and apt_calls == [("/k/new.deb", False)]
    assert "--- install OmniaCLI 2026.1.5502 (it was 2026.1.5501)" in (tmp_path / "20261123-101500-abcdef.log").read_text()
    # apt fails: the version before from its kept package, the outcome says so
    installed["v"], fail["new"], apt_calls[:] = "2026.1.5501", True, []
    assert Unit(str(run_file)).main() == 1
    done = json.loads((tmp_path / "20261123-101500-abcdef.outcome.json").read_text())
    assert (done["ok"], done["went_back"], done["error"]) == (False, True, "apt-get ended with 100")
    assert apt_calls == [("/k/new.deb", False), ("/k/old.deb", True)]


@pytest.fixture
async def panel(tmp_path, monkeypatch):
    """A machine (admin) with a production, mario a workspace's admin, a central linked as an admin; what is
    published and the root helper are fakes."""
    conf = tmp_path / "lab3"
    conf.mkdir()
    settings = parse_settings({"auth": {"users": {"admin": USER_HASH}, "links": {
        "central": {"token": hash_token(CENTRAL_TOKEN), "role": "admin"}}},
        "backup_dir": str(conf / "backups"), "jobs_dir": str(conf / "jobs"), "servers_file": "servers.yaml",
        "self_update": {"url": URL, "public_key": "ed25519:" + base64.b64encode(b"k" * 32).decode()}},
        base_dir=conf)
    settings.set_web_access(Access(users={"mario": hash_password(PASSWORD, iterations=1000)}, roles={"mario": "workspace"}))
    settings.workspaces = {"acme": Workspace("acme", "ACME", members={"mario": "admin"})}
    calls = []
    state = tmp_path / "state"
    monkeypatch.setattr(selfupdate, "STATE", state)
    monkeypatch.setattr(selfupdate, "published_cached", lambda source, installed, fresh=False: {
        "version": "2026.1.5502", "built": "2026-11-23T09:00:00", "size": 10, "signed": True, "error": None,
        "notes": [{"version": "2026.1.5502", "name": "OmniaCLI 2026.1.5502", "body": "the update from the panel",
                   "url": "", "published": ""}]})

    async def ask(*args, timeout=None):
        calls.append(args)
        if args[0] == "check":
            return {"ok": True}
        raise JobFailed("not in this test")
    monkeypatch.setattr(selfupdate, "ask", ask)
    monkeypatch.setattr("omniacli.app._version", lambda: "2026.1.5501")
    app = create_app(settings)
    transport = httpx.ASGITransport(app=BasicAsSession(app))
    async with httpx.AsyncClient(transport=transport, base_url=f"http://{HOST}") as client:
        client.calls, client.log, client.settings = calls, conf / "logs" / "access.jsonl", settings
        yield client


async def test_the_dialog_reads_what_is_published(panel):
    data = (await panel.get("/api/machine/omniacli-update", headers=ADMIN)).json()
    assert (data["installed"], data["available"], data["signed"], data["unavailable"]) == \
        ("2026.1.5501", "2026.1.5502", True, None)
    assert data["notes"][0]["body"] == "the update from the panel" and data["back"] is None and data["confirm"] == ""
    # the machine's page: the button and the dialog, its own address through a link
    page = (await panel.get("/", headers={**ADMIN, "Accept": "text/html", "Accept-Language": "it"})).text
    assert 'id="omniacli-update-open"' in page and "Aggiorna OmniaCLI…" in page and 'id="ou-dialog"' in page
    page = (await panel.get("/", headers={**PERSON, "Accept": "text/html"})).text
    assert '"/links/lab3"' in page and "/api/machine/omniacli-update" in page


async def test_who_may_update(panel):
    for headers in (ALONE, MARIO):
        assert (await panel.get("/api/machine/omniacli-update", headers=headers)).status_code == 403
        assert (await panel.post("/api/machine/omniacli-update", headers=headers, json={"action": "update"})).status_code == 403
    assert (await panel.post("/api/machine/omniacli-update", headers=ADMIN, json={"action": "maybe"})).status_code == 400
    # the way back: nothing kept
    resp = await panel.post("/api/machine/omniacli-update", headers={**ADMIN, "Accept-Language": "it"}, json={"action": "back"})
    assert resp.status_code == 400 and "Non è tenuta nessuna versione prima della 2026.1.5501" in resp.json()["error"]


async def test_a_person_through_a_link_starts_it(panel, monkeypatch):
    started = []

    async def run_job(job, mode, record, helper=None, state=None):
        started.append((job.title, mode, job.source))
        record("OmniaCLI update 2026.1.5501 -> 2026.1.5502: downloaded and verified (sha256 x)")
    monkeypatch.setattr(selfupdate, "run_job", run_job)
    resp = await panel.post("/api/machine/omniacli-update", headers={**PERSON, "Accept-Language": "it"}, json={"action": "update"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["title"] == "Aggiorna OmniaCLI 2026.1.5501 → 2026.1.5502"
    for _ in range(100):
        if started:
            break
        await asyncio.sleep(0.01)
    assert started[0][:2] == ("Update OmniaCLI 2026.1.5501 → 2026.1.5502", "update")
    said = [json.loads(line)["detail"] for line in panel.log.read_text().splitlines() if "OmniaCLI update" in line]
    assert said[0] == "OmniaCLI update 2026.1.5501 -> 2026.1.5502 asked, for carlo"
    assert said[1] == "OmniaCLI update 2026.1.5501 -> 2026.1.5502: downloaded and verified (sha256 x)"


async def test_a_machine_that_serves_productions_asks_its_name(panel, monkeypatch):
    monkeypatch.setattr("omniacli.app.socket.gethostname", lambda: "lab3.example")
    server = type("S", (), {"instances": {"production": object()}, "name": "erp"})()
    monkeypatch.setattr(panel.settings, "active_servers", lambda: [server])
    assert (await panel.get("/api/machine/omniacli-update", headers=ADMIN)).json()["confirm"] == "lab3"
    resp = await panel.post("/api/machine/omniacli-update", headers=ADMIN, json={"action": "update", "confirm": "lab"})
    assert resp.status_code == 400 and "type lab3" in resp.json()["error"]


async def test_sudo_refusing_the_helper_is_said(panel, monkeypatch):
    async def refused(*args, timeout=None):
        raise JobFailed("sudo: a password is required")
    monkeypatch.setattr(selfupdate, "ask", refused)
    data = (await panel.get("/api/machine/omniacli-update", headers=ADMIN)).json()
    assert "installed from its package updates itself" in data["unavailable"]
    resp = await panel.post("/api/machine/omniacli-update", headers=ADMIN, json={"action": "update"})
    assert resp.status_code == 400 and "sudo does not let OmniaCLI" in resp.json()["error"]
