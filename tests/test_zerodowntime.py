# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The update without stopping (zerodowntime.py): what the old Odoo can keep
serving, the order of the steps, the normal update when it cannot, a
failure before nginx is switched leaving the old Odoo serving, the site
switched and back, the drain, the users' time measured, the root helpers'
commands."""

import asyncio
import json
import subprocess

import pytest

from omniacli import composehelper, deploys, ops, workspacehelper, workspaceweb, zerodowntime
from omniacli import docker as docker_plugin
from omniacli.composehelper import HelperError
from omniacli.config import parse_servers
from omniacli.jobs import Job, JobFailed

from .conftest import clone, make_remote, sh
from .test_app import AUTH, POST, client  # noqa: F401 - the client fixture
from .test_stacktemplates import root  # noqa: F401 - the root fixture
from .test_workspaceweb import spec, web_root  # noqa: F401 - the web_root fixture

MANIFEST = '{"name": "M1", "version": "%s", "depends": %s, "data": ["views.xml"], "license": "LGPL-3"}\n'
MODEL = 'from odoo import fields, models\n\n\nclass P(models.Model):\n    _inherit = "res.partner"\n%s'


def job() -> Job:
    return Job("j", "Git pull and restart ws-inst/production", ["ws-inst/production"], "manual: admin")


# --- what the old Odoo can keep serving ---------------------------------------

def repo(files, py_lines=None, manifests=None, new_dirs=("m1", "m2"), old_dirs=("m1", "m2"), **more):
    return {"name": "addons", "files": files, "old_dirs": set(old_dirs), "new_dirs": set(new_dirs),
            "py_lines": py_lines or {}, "manifests": manifests or {}, **more}


def test_what_the_old_odoo_can_keep_serving():
    installed = {"m1"}
    # views, data, translations, static files, a method's body, the version: records it reads, code it does not
    ok = repo([("M", "m1/views.xml"), ("M", "m1/static/src/x.js"), ("M", "m1/i18n/it.po"), ("M", "m1/models/p.py"),
               ("M", "m1/__manifest__.py"), ("M", "README.md")],
              py_lines={"m1/models/p.py": ["        return self.name.upper()", "        return self.name"]},
              manifests={"m1": (MANIFEST % ("19.0.1.0.0", '["base"]'), MANIFEST % ("19.0.1.0.1", '["base"]'))})
    assert zerodowntime.refusals([ok], installed) == []
    # a module that is not installed is not loaded: anything goes
    assert zerodowntime.refusals([repo([("A", "m2/migrations/1.0/pre.py"), ("M", "m2/models.py")],
                                       py_lines={"m2/models.py": ["    x = fields.Char()"]})], installed) == []
    cases = [
        (repo([("M", "m1/models/p.py")], py_lines={"m1/models/p.py": ["    note = fields.Char()"]}),
         "the module m1: m1/models/p.py changes a field or a model"),
        (repo([("M", "m1/models/p.py")], py_lines={"m1/models/p.py": ["        required=True,"]}),
         "the module m1: m1/models/p.py changes a field or a model"),
        (repo([("M", "m1/models/p.py")], py_lines={"m1/models/p.py": ['    _name = "m1.thing"']}),
         "the module m1: m1/models/p.py changes a field or a model"),
        (repo([("A", "m1/migrations/19.0.1.0.1/post-migrate.py")]), "the module m1: a migration script"),
        (repo([("M", "m1/__manifest__.py")],
              manifests={"m1": (MANIFEST % ("1", '["base"]'), MANIFEST % ("1", '["base", "sale"]'))}),
         "the module m1: what it depends on changes"),
        (repo([("D", "m1/__manifest__.py"), ("D", "m1/views.xml")], new_dirs=("m2",)),
         "the module m1 is installed and its folder is taken away"),
        (repo([("M", "requirements.txt")]), "[addons] requirements.txt changes"),
        (repo([], submodules=True), "[addons] has submodules"),
        (repo([("M", "sub")], gitlinks=["sub"]), "[addons] the submodule sub moves"),
    ]
    for case, said in cases:
        found = zerodowntime.refusals([case], installed)
        assert found and found[0].startswith(said), (said, found)


def test_the_module_of_a_file():
    dirs = {"m1", "pack/m2"}
    assert zerodowntime.module_of("m1/models/p.py", dirs) == "m1"
    assert zerodowntime.module_of("pack/m2/views/v.xml", dirs) == "pack/m2"
    assert zerodowntime.module_of("README.md", dirs) is None and zerodowntime.module_of("m1", dirs) is None


# --- an instance of a workspace, its repository a real one -----------------------

@pytest.fixture
def zd(tmp_path, monkeypatch):
    """ws-inst/production: a compose project whose repository addons (module
    m1, installed) is in its addons/; the root helpers answered here (each
    call recorded: helpers), git for real; the Odoos answer 200, version 19.0;
    a new commit pushed by push()."""
    remote = make_remote(tmp_path, "addons")
    work = clone(remote, tmp_path / "work", "main")
    (work / "m1").mkdir()
    (work / "m1" / "__manifest__.py").write_text(MANIFEST % ("19.0.1.0.0", '["base"]'))
    (work / "m1" / "views.xml").write_text("<odoo/>\n")
    (work / "m1" / "p.py").write_text(MODEL % "    a = fields.Char()\n")
    sh("git", "add", "-A", cwd=work)
    sh("git", "commit", "-q", "-m", "m1 19.0.1.0.0", cwd=work)
    sh("git", "push", "-q", "origin", "HEAD:main", cwd=work)
    project = tmp_path / "storage" / "ws" / "inst" / "production"
    (project / "addons").mkdir(parents=True)
    live = clone(remote, project / "addons" / "addons", "main")
    srv = parse_servers({"ws-inst": {"type": "docker", "production": {
        "database": "inst", "http_port": 20000, "restart": {"type": "compose", "service": str(project)},
        "repositories": [{"name": "addons", "path": str(live), "branch": "main"}]}}})["ws-inst"]
    instance = srv.production
    target = zerodowntime.Target("ws", "default", "inst", "production", "https://inst.acme.it", project)
    state = {"helpers": [], "answers": {}, "next": None, "restarts": 0, "seen_at_update": None, "status": 200}
    real_capture, real_run = ops.capture, ops.run

    def answer(cmd):
        if cmd[3] == "workspace-helper":
            tail = cmd[4:]
        else:
            tail = cmd[5:]
        state["helpers"].append(tail)
        action = tail[0]
        if action == "next-update":
            copy = project / "addons" / composehelper.NEXT_FOLDER / "addons"
            state["seen_at_update"] = {"copy": (copy / "m1" / "__manifest__.py").read_text(),
                                       "live": sh("git", "rev-parse", "HEAD", cwd=live)}
        if action in state["answers"]:
            return state["answers"][action]
        if action == "next-start":
            state["next"] = {"name": "ws-inst-production-next", "running": True, "http_port": 40000,
                             "websocket_port": None}
            return 0, '{"container": "ws-inst-production-next", "http_port": 40000, "websocket_port": null}'
        if action == "next-state":
            return 0, json.dumps(state["next"])
        if action == "state":
            return 0, '{"Service": "odoo", "State": "running"}\n{"Service": "db", "State": "running"}'

        if action == "next-stop":
            state["next"] = None
            return 0, '{"removed": "ws-inst-production-next"}'
        if action == "upstream":
            back = tail[5] == "-"
            return 0, json.dumps({"http_port": 20000 if back else int(tail[5]), "websocket_port": None,
                                  "from": {"http_port": 40000 if back else 20000, "websocket_port": 20001},
                                  "nginx": "nginx reloaded"})
        return 0, ""

    async def capture(cmd, **kw):
        if cmd[0] == "sudo":
            return answer(cmd)
        return await real_capture(cmd, **kw)

    async def run(job, cmd, **kw):
        if cmd[0] == "sudo":
            code, out = answer(cmd)
            for line in out.splitlines():
                job.log(line)
            if code and kw.get("check", True):
                raise JobFailed(f"omniacli exited with code {code}")
            return code
        return await real_run(job, cmd, **kw)

    async def restart(job, inst):
        state["helpers"].append(["restart"])
        state["restarts"] += 1

    async def known(self, inst):
        return {"base": "installed", "m1": "installed"}

    async def status(url):
        return state["status"]

    async def version(port):
        return "19.0-20261112"

    async def asked(url):
        return True

    monkeypatch.setattr(ops, "capture", capture)
    monkeypatch.setattr(ops, "run", run)
    monkeypatch.setattr(ops, "restart", restart)
    monkeypatch.setattr(docker_plugin.DockerPlugin, "known_modules", known)
    monkeypatch.setattr(zerodowntime, "_status", status)
    monkeypatch.setattr(zerodowntime, "version_of", version)
    monkeypatch.setattr(zerodowntime, "ask", asked)
    monkeypatch.setattr(zerodowntime, "open_connections", lambda ports: 0)
    monkeypatch.setattr(zerodowntime, "DRAIN_LEAST", 0)
    monkeypatch.setattr(zerodowntime, "PROBE_EVERY", 0.01)

    def push(version: str, body: str = "    a = fields.Char()\n", name: str = "p.py") -> str:
        sh("git", "pull", "-q", "origin", "main", cwd=work)
        (work / "m1" / "__manifest__.py").write_text(MANIFEST % (version, '["base"]'))
        (work / "m1" / name).write_text(MODEL % body)
        sh("git", "add", "-A", cwd=work)
        sh("git", "commit", "-q", "-m", f"m1 {version}", cwd=work)
        sh("git", "push", "-q", "origin", "HEAD:main", cwd=work)
        return sh("git", "rev-parse", "HEAD", cwd=work)

    zerodowntime.configure(lambda inst: target if inst is instance else None)
    yield {"instance": instance, "target": target, "state": state, "live": live, "project": project, "push": push}
    zerodowntime.configure(None)


def actions(state) -> list[str]:
    return [h[0] if h[0] != "upstream" else f"upstream {h[5]}" for h in state["helpers"]]


async def test_the_steps_in_their_order(zd):
    new = zd["push"]("19.0.1.0.1", "    a = fields.Char()\n\n    def hello(self):\n        return 1\n")
    before = sh("git", "rev-parse", "HEAD", cwd=zd["live"])
    j = job()
    await ops.pull_restart(j, zd["instance"], backup_folder=None)
    assert actions(zd["state"]) == ["state", "next-state", "next-update", "next-start", "upstream 40000",
                                    "restart", "upstream -", "next-stop"]
    # the -u ran on the copy at the new commit, the instance's own code not moved yet
    seen = zd["state"]["seen_at_update"]
    assert '"19.0.1.0.1"' in seen["copy"] and seen["live"] == before
    assert sh("git", "rev-parse", "HEAD", cwd=zd["live"]) == new
    assert not (zd["project"] / "addons" / composehelper.NEXT_FOLDER).exists()
    assert len(sh("git", "worktree", "list", cwd=zd["live"]).splitlines()) == 1
    assert j.notes["unserved"]["seconds"] == 0 and j.notes["unserved"]["how"] == "without stopping"
    text = "\n".join(j.lines)
    for said in ("--- nginx of https://inst.acme.it to 127.0.0.1:40000", "the second Odoo: /web/login 200, Odoo 19.0",
                 "the old Odoo: no request open", "Users not served: 0.0 s (without stopping"):
        assert said in text, said
    assert text.index("127.0.0.1:40000") < text.index(f"{before[:10]} → {new[:10]}") < text.index("to its own Odoo")


async def test_nothing_new_switches_nothing(zd):
    j = job()
    await ops.pull_restart(j, zd["instance"], only_if_changed=True)
    assert actions(zd["state"]) == ["state", "next-state"] and "Nothing new to deploy" in "\n".join(j.lines)


async def test_a_field_falls_back_to_the_normal_update(zd, monkeypatch):
    zd["push"]("19.0.1.0.1", "    a = fields.Char()\n    b = fields.Integer()\n")
    normal = []

    async def plain(job, instance, only_if_changed, only, backup_folder):
        normal.append(instance.key)
    monkeypatch.setattr(ops, "_pull_restart", plain)
    j = job()
    await ops.pull_restart(j, zd["instance"])
    assert normal == ["ws-inst/production"]
    assert "next-update" not in actions(zd["state"]) and "upstream 40000" not in actions(zd["state"])
    assert any("Not without stopping: the module m1: m1/p.py changes a field or a model" in w for w in j.warnings)
    assert j.notes["unserved"]["how"] == "the normal update, a restart"


async def test_a_failing_update_leaves_the_old_odoo_serving(zd):
    zd["push"]("19.0.1.0.1", "    a = fields.Char()\n\n    def broken(self):\n        raise ValueError\n")
    before = sh("git", "rev-parse", "HEAD", cwd=zd["live"])
    zd["state"]["answers"]["next-update"] = (1, "odoo.tools.convert.ParseError: while parsing views.xml")
    j = job()
    with pytest.raises(JobFailed, match="the old Odoo keeps serving, its code was not moved"):
        await ops.pull_restart(j, zd["instance"])
    found = actions(zd["state"])
    assert "next-start" not in found and not any(a.startswith("upstream") for a in found) and "restart" not in found
    assert sh("git", "rev-parse", "HEAD", cwd=zd["live"]) == before
    assert not (zd["project"] / "addons" / composehelper.NEXT_FOLDER).exists()
    assert "Nothing changed for the users" in "\n".join(j.lines)


async def test_a_second_odoo_that_does_not_answer_is_removed(zd, monkeypatch):
    zd["push"]("19.0.1.0.1", "    a = fields.Char()\n\n    def hello(self):\n        return 2\n")
    before = sh("git", "rev-parse", "HEAD", cwd=zd["live"])
    zd["state"]["status"] = 502
    monkeypatch.setattr(zerodowntime, "HEALTH_TIMEOUT", 0)
    with pytest.raises(JobFailed, match="the second Odoo did not answer /web/login"):
        await ops.pull_restart(job(), zd["instance"])
    found = actions(zd["state"])
    assert found[-1] == "next-stop" and not any(a.startswith("upstream") for a in found)
    assert zd["state"]["next"] is None and sh("git", "rev-parse", "HEAD", cwd=zd["live"]) == before


async def test_back_to_a_deploy_without_stopping(zd, tmp_path):
    first = sh("git", "rev-parse", "HEAD", cwd=zd["live"])
    zd["push"]("19.0.1.0.1", "    a = fields.Char()\n\n    def hello(self):\n        return 3\n")
    sh("git", "pull", "-q", "--ff-only", "origin", "main", cwd=zd["live"])
    entry = {"id": "d1", "state": {"addons": {"sha": first, "branch": "main"}}}
    j = job()
    await deploys.go_back(j, zd["instance"], entry, None)
    assert "upstream 40000" in actions(zd["state"]) and zd["state"]["restarts"] == 1
    assert sh("git", "rev-parse", "HEAD", cwd=zd["live"]) == first
    assert j.notes["back_to"] == "d1" and j.notes["unserved"]["how"] == "without stopping"


async def test_a_reason_of_the_app_is_the_normal_update(zd, monkeypatch):
    zd["push"]("19.0.1.0.1")
    normal = []

    async def plain(job, instance, only_if_changed, only, backup_folder):
        normal.append(1)
    monkeypatch.setattr(ops, "_pull_restart", plain)
    zerodowntime.configure(lambda inst: "it has no name served with https yet")
    j = job()
    await ops.pull_restart(j, zd["instance"])
    assert normal == [1] and zd["state"]["helpers"] == [["state"]]
    assert any("Not without stopping: it has no name served with https yet" in w for w in j.warnings)


async def test_a_second_odoo_left_by_an_update_before(zd):
    zd["state"]["next"] = {"name": "ws-inst-production-next", "running": True, "http_port": 40001, "websocket_port": None}
    await zerodowntime.leftover(job(), zd["instance"], zd["target"])
    assert actions(zd["state"]) == ["next-state", "upstream -", "next-stop"]
    # the instance's own Odoo does not answer: its users may be on the second one, it stays
    zd["state"].update(next={"name": "x", "running": True, "http_port": 40001}, status=None, helpers=[])
    with pytest.raises(JobFailed, match="its users may be on it"):
        await zerodowntime.leftover(job(), zd["instance"], zd["target"])
    assert actions(zd["state"]) == ["next-state"]


# --- the users' time, the drain -------------------------------------------------

def test_the_time_without_an_answer():
    points = [(0.0, True), (0.1, False), (0.2, False), (0.3, True), (0.4, True), (0.5, False), (0.6, True)]
    found = zerodowntime.measure(points, 0.7)
    assert (found["asked"], found["failed"], found["seconds"], found["longest"]) == (7, 3, 0.3, 0.2)
    # still failing at the end: until the end
    assert zerodowntime.measure([(0.0, True), (1.0, False)], 3.0)["seconds"] == 2.0
    assert zerodowntime.measure([], 1.0) == {"asked": 0, "failed": 0, "seconds": 0.0, "longest": 0.0, "every": 0.1}


async def test_the_probe_measures(monkeypatch):
    answers = iter([True, False, False, True] + [True] * 1000)

    async def ask(url):
        return next(answers)
    monkeypatch.setattr(zerodowntime, "ask", ask)
    probe = zerodowntime.Probe("https://x", every=0.06).start()
    await asyncio.sleep(0.5)
    found = await probe.stop()
    assert found["failed"] == 2 and found["seconds"] > 0 and found["asked"] > 4
    # settle: until an ask after now is answered (the restart of a normal update ends before its Odoo answers)
    answers = iter([False] * 5 + [True] * 1000)
    probe = zerodowntime.Probe("https://x", every=0.01).start()
    await probe.settle()
    found = await probe.stop()
    assert found["failed"] == 5 and found["asked"] >= 6


def test_the_connections_open_to_a_port(tmp_path):
    table = tmp_path / "tcp"
    # 127.0.0.1:41000 -> 127.0.0.1:20000 established, one to 8090, one listening, one closing
    table.write_text("  sl  local_address rem_address   st\n"
                     "   0: 0100007F:A028 0100007F:4E20 01 0\n"
                     "   1: 0100007F:A029 0100007F:1F9A 01 0\n"
                     "   2: 0100007F:4E20 00000000:0000 0A 0\n"
                     "   3: 0100007F:A02A 0100007F:4E20 06 0\n")
    assert zerodowntime.open_connections({20000}, (str(table),)) == 1
    assert zerodowntime.open_connections({20000, 8090}, (str(table),)) == 2
    assert zerodowntime.open_connections({20001}, (str(table), str(tmp_path / "none"))) == 0


async def test_the_drain(monkeypatch):
    left = iter([2, 1, 0])
    monkeypatch.setattr(zerodowntime, "open_connections", lambda ports: next(left))
    monkeypatch.setattr(zerodowntime, "DRAIN_LEAST", 0)
    monkeypatch.setattr(zerodowntime, "DRAIN_EVERY", 0)
    j = job()
    await zerodowntime.drain(j, {20000}, "the old Odoo")
    assert "the old Odoo: no request open after" in j.lines[-1]
    # connections that never end (websockets): at most DRAIN_MOST seconds
    monkeypatch.setattr(zerodowntime, "open_connections", lambda ports: 3)
    monkeypatch.setattr(zerodowntime, "DRAIN_MOST", 0)
    await zerodowntime.drain(j, {20000}, "the old Odoo")
    assert "3 connections still open after 0 s" in j.lines[-1]


# --- the site switched and back ------------------------------------------------

def test_the_site_switched_and_back():
    sites = [{"instance": "main", "role": "production", "names": ["acme.it"], "http_port": 20000, "websocket_port": None},
             {"instance": "main", "role": "test", "names": ["test.acme.it"], "http_port": 20002, "websocket_port": 20003}]
    text = workspaceweb.conf_text("acme", sites, ("/c/f.pem", "/c/k.pem"), ipv6=False, omniacli="127.0.0.1:8090")
    switched = workspaceweb.switch_upstream(text, "main", "production", {20000: 40000})
    production, test = switched.split("# main test")
    # threaded: / and /websocket go to the http port, both switched; OmniaCLI's own paths and the test kept
    assert production.count("proxy_pass http://127.0.0.1:40000;") == 2 and "20000" not in production
    assert production.count("proxy_pass http://127.0.0.1:8090;") == 2 and test == text.split("# main test")[1]
    with_workers = workspaceweb.switch_upstream(text, "main", "test", {20002: 40010, 20003: 40011})
    assert "location /websocket {\n        proxy_pass http://127.0.0.1:40011;" in with_workers.split("# main test")[1]
    assert workspaceweb.switch_upstream(switched, "main", "production", {40000: 20000}) == text
    for instance, role, ports in (("other", "production", {20000: 1}), ("main", "production", {12345: 1})):
        with pytest.raises(ValueError):
            workspaceweb.switch_upstream(text, instance, role, ports)


def test_the_helper_switches_the_site(web_root, monkeypatch):
    storage, site = str(web_root["storage"]), web_root["site"]
    workspacehelper.web("acme", storage, spec())
    listening = {40000}
    monkeypatch.setattr(workspacehelper, "_listens", lambda port: port in listening)
    project = web_root["storage"] / "acme" / "main" / "production"
    made = workspacehelper.upstream("acme", storage, "main", "production", "40000", "-")
    assert made["http_port"] == 40000 and made["from"]["http_port"] == 20000 and not made["own"]
    production = site.read_text().split("# main test")[0]
    assert "proxy_pass http://127.0.0.1:40000;" in production and "127.0.0.1:20000;" not in production
    assert json.loads((project / workspaceweb.UPSTREAM_FILE).read_text()) == {"http_port": 40000, "websocket_port": None}
    # the site written again keeps it while it lasts
    workspacehelper.web("acme", storage, spec())
    assert "proxy_pass http://127.0.0.1:40000;" in site.read_text().split("# main test")[0]
    # nothing listens there: refused, nothing changed
    with pytest.raises(HelperError, match="nothing answers on 127.0.0.1:40001"):
        workspacehelper.upstream("acme", storage, "main", "production", "40001", "-")
    with pytest.raises(HelperError, match="own ports"):
        workspacehelper.upstream("acme", storage, "main", "production", "20000", "-")
    # nginx -t refuses: the site and the record of before are back
    web_root["web"]["nginx_t"] = 1
    listening.add(20000)
    with pytest.raises(HelperError, match="nginx -t refused"):
        workspacehelper.upstream("acme", storage, "main", "production", "-", "-")
    assert (project / workspaceweb.UPSTREAM_FILE).is_file() and "127.0.0.1:40000;" in site.read_text()
    web_root["web"]["nginx_t"] = 0
    back = workspacehelper.upstream("acme", storage, "main", "production", "-", "-")
    assert back["own"] and back["http_port"] == 20000 and not (project / workspaceweb.UPSTREAM_FILE).exists()
    assert "proxy_pass http://127.0.0.1:20000;" in site.read_text() and "40000" not in site.read_text()
    # one action at a time on the site (another instance switched, a publication): a lock nginx does not read
    assert (web_root["tmp"] / "nginx" / ".omniacli-acme.lock").is_file()
    # the site taken away (no domain, no instance): its lock with it
    workspacehelper.web("acme", storage, spec(domain=""))
    assert not site.exists() and not (web_root["tmp"] / "nginx" / ".omniacli-acme.lock").exists()


def test_the_maintenance_page_on_and_off():
    """The server block of an environment answers its Odoo's 502 and 504
    with a page of its own: 503, Retry-After, the reader's language (the
    catalog's), English else; off, the site is the one of before."""
    sites = [{"instance": "main", "role": "production", "names": ["acme.it"], "http_port": 20000, "websocket_port": None},
             {"instance": "main", "role": "test", "names": ["test.acme.it"], "http_port": 20002, "websocket_port": 20003}]
    text = workspaceweb.conf_text("acme", sites, ("/c/f.pem", "/c/k.pem"), ipv6=False, omniacli="127.0.0.1:8090")
    on = workspaceweb.switch_maintenance(text, "main", "production", True)
    production, test = on.split("# main test")
    assert "error_page 502 504 = @omniacli_maintenance;" in production and test == text.split("# main test")[1]
    assert f"add_header Retry-After {workspaceweb.MAINTENANCE_RETRY} always;" in production
    assert 'if ($http_accept_language ~* "^it") {' in production and "Aggiornamento in corso, torna tra poco" in production
    assert "Update in progress, come back shortly" in production and production.count("return 503 ") == 2
    # before the locations, so it applies to all of them; the proxying is the same
    assert production.index("error_page") < production.index("location /websocket")
    assert workspaceweb.switch_maintenance(on, "main", "production", True) == on
    assert workspaceweb.switch_maintenance(on, "main", "production", False) == text
    assert workspaceweb.switch_maintenance(text, "main", "production", False) == text
    # a site written while it is on keeps it
    kept = workspaceweb.conf_text("acme", [{**sites[0], "maintenance": True}, sites[1]], ("/c/f.pem", "/c/k.pem"),
                                  ipv6=False, omniacli="127.0.0.1:8090")
    assert kept == on
    with pytest.raises(ValueError):
        workspaceweb.switch_maintenance(text, "other", "production", True)
    # nginx's string: no quote, no variable
    for language in ("en", "it"):
        page = workspaceweb.maintenance_page(language)
        assert '"' not in page and "$" not in page and f"lang='{language}'" in page


def test_the_helper_puts_the_maintenance_page(web_root):
    storage, site = str(web_root["storage"]), web_root["site"]
    workspacehelper.web("acme", storage, spec())
    project = web_root["storage"] / "acme" / "main" / "production"
    made = workspacehelper.maintenance("acme", storage, "main", "production", "on")
    assert made["maintenance"] and (project / workspaceweb.MAINTENANCE_FILE).is_file()
    assert "@omniacli_maintenance" in site.read_text().split("# main test")[0]
    # the site written again keeps it while it lasts
    workspacehelper.web("acme", storage, spec())
    assert "@omniacli_maintenance" in site.read_text()
    # nginx -t refuses: the site and the record of before are back
    web_root["web"]["nginx_t"] = 1
    with pytest.raises(HelperError, match="nginx -t refused"):
        workspacehelper.maintenance("acme", storage, "main", "production", "off")
    assert (project / workspaceweb.MAINTENANCE_FILE).is_file() and "@omniacli_maintenance" in site.read_text()
    web_root["web"]["nginx_t"] = 0
    off = workspacehelper.maintenance("acme", storage, "main", "production", "off")
    assert not off["maintenance"] and not (project / workspaceweb.MAINTENANCE_FILE).exists()
    assert "@omniacli_maintenance" not in site.read_text()
    assert workspacehelper.maintenance("acme", storage, "main", "production", "off")["nginx"] == "the site is unchanged"
    for args, message in ((("main", "production", "maybe"), "usage"), (("nope", "production", "on"), "no environment"),
                          (("main", "staging", "on"), "not production or test")):
        with pytest.raises(HelperError, match=message):
            workspacehelper.maintenance("acme", storage, *args)


# --- the compose helper's commands -----------------------------------------------

@pytest.fixture
def next_project(tmp_path, monkeypatch):
    project = tmp_path / "p"
    (project / "config").mkdir(parents=True)
    (project / "addons" / composehelper.NEXT_FOLDER / "addons").mkdir(parents=True)
    (project / ".env").write_text("PROJECT=ws-inst-production\nHTTP_PORT=20000\n")
    (project / "config" / "odoo.conf").write_text(
        "[options]\naddons_path = /mnt/enterprise,/mnt/customer-addons/addons,/mnt/customer-addons/other/sub\n"
        "workers = 2\n")
    monkeypatch.setattr(composehelper, "services", lambda project: ["odoo", "db"])
    monkeypatch.setattr(composehelper, "compose", lambda project, *args: ["/usr/bin/docker", "compose", *args])
    return project


def test_the_addons_path_of_the_new_code(next_project):
    assert composehelper.next_addons_path(next_project) == \
        f"/mnt/enterprise,/mnt/customer-addons/{composehelper.NEXT_FOLDER}/addons,/mnt/customer-addons/other/sub"
    (next_project / "addons" / composehelper.NEXT_FOLDER / "addons").rmdir()
    with pytest.raises(HelperError, match="no repository of the addons path"):
        composehelper.next_addons_path(next_project)
    assert composehelper.next_name(next_project) == "ws-inst-production-next"


def test_the_commands_of_the_second_odoo(next_project, monkeypatch, tmp_path):
    tools = tmp_path / "tools"
    (tools / "bin").mkdir(parents=True)
    (tools / "bin" / "click-odoo-update").write_text("")
    monkeypatch.setattr(composehelper, "TOOLS", tools)
    [update] = composehelper.commands(next_project, "next-update", ["odoo", "inst"])
    assert update[:4] == ["run", "--rm", "--no-deps", "-T"] and update[-2:] == [
        "inst", f"/mnt/enterprise,/mnt/customer-addons/{composehelper.NEXT_FOLDER}/addons,/mnt/customer-addons/other/sub"]
    assert '--addons-path "$2"' in composehelper.SCRIPTS["next-update"]
    calls, printed = [], []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1] == "inspect":
            return subprocess.CompletedProcess(cmd, 1, "", "no such container")
        return subprocess.CompletedProcess(cmd, 0, "abc\n", "")
    monkeypatch.setattr(composehelper.subprocess, "run", run)
    monkeypatch.setattr("builtins.print", lambda text, **kw: printed.append(text))
    assert composehelper.next_action(next_project, "next-start", ["odoo"]) == 0
    started = json.loads(printed[-1])
    command = calls[-1]
    assert command[:7] == ["/usr/bin/docker", "compose", "run", "-d", "--no-deps", "--name", "ws-inst-production-next"]
    assert f"127.0.0.1:{started['http_port']}:8069" in command and f"127.0.0.1:{started['websocket_port']}:8072" in command
    assert command[-4:] == ["odoo", "odoo", f"--addons-path={started['addons_path']}", "--max-cron-threads=0"]
    with pytest.raises(HelperError):
        composehelper.next_action(next_project, "next-start", ["web"])
    assert composehelper.next_action(next_project, "next-stop", []) == 0 and json.loads(printed[-1]) == {"removed": None}


# --- the switch kept in the file of deploys ----------------------------------------

def test_the_switch_is_kept_with_the_deploys(tmp_path):
    folder = tmp_path / "deploys"
    assert deploys.zero_downtime(folder, "s/test") is None
    on = deploys.set_zero_downtime(folder, "s/test", "admin")
    assert on["by"] == "admin" and deploys.zero_downtime(folder, "s/test") == on
    # the other things of the file keep it
    deploys.set_paused(folder, "s/test", "dev")
    deploys.record(folder, "s/test", {"id": "x"})
    assert deploys.zero_downtime(folder, "s/test") == on and deploys.paused(folder, "s/test")["by"] == "dev"
    assert deploys.set_zero_downtime(folder, "s/test", None) is None and deploys.zero_downtime(folder, "s/test") is None
    assert deploys.paused(folder, "s/test")["by"] == "dev"


async def test_the_switch_of_an_instance_that_cannot(client, env):
    """srv is no instance of a workspace: the switch says why and is refused on, off is always taken."""
    api = "/api/servers/srv/test/zero-downtime"
    view = (await client.get(api, headers={"Authorization": AUTH})).json()
    assert view["enabled"] is False and view["available"] is False and "nginx OmniaCLI writes" in view["reason"]
    refused = await client.post(api, headers=POST, json={"enabled": True})
    assert refused.status_code == 400 and "nginx OmniaCLI writes" in refused.json()["error"]
    assert (await client.post(api, headers=POST, json={"enabled": False})).json()["enabled"] is False
    # an update of it is the normal one: the app's lookup says off
    assert zerodowntime.choice(env["settings"].servers["srv"].test) is None
    # nor has it the maintenance page of a normal update: its nginx is not OmniaCLI's
    assert zerodowntime.site(env["settings"].servers["srv"].test) is None
