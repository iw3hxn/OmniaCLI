# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Log in as on a site of another nginx (a hosting panel's): its server block
of 443 for the instance's name gets one include line, nginx -t checks it,
the files are put back when it refuses; removed again by remove."""

import json
import subprocess

import pytest

from omniacli import workspacehelper
from omniacli.composehelper import HelperError

SITE = """# a panel's site
server {
    listen 80;
    server_name shop.example.com;
    return 301 https://$host$request_uri;
}
server {
    listen 443 ssl http2;
    server_name shop.example.com www.shop.example.com;
    location / {  # odoo
        proxy_pass http://127.0.0.1:8069;
    }
}
server {
    listen 443 ssl;
    server_name other.example.com;
}
"""


@pytest.fixture
def nginx(tmp_path, monkeypatch):
    site = tmp_path / "shop.conf"
    site.write_text(SITE)
    state = {"test": 0, "reloads": 0}
    monkeypatch.setattr(workspacehelper, "PATHS_SNIPPET", tmp_path / "omniacli-paths.conf")
    monkeypatch.setattr(workspacehelper, "_tool", lambda name: name)
    monkeypatch.setattr(workspacehelper, "omniacli_address", lambda: "127.0.0.1:8090")

    def run(*cmd, check=True):
        return f"# configuration file {site}:\n{site.read_text()}" if "-T" in cmd else ""
    monkeypatch.setattr(workspacehelper, "_run", run)
    monkeypatch.setattr(workspacehelper, "_write", lambda path, text, mode=0o644, uid=0, gid=0: path.write_text(text))

    def nginx_t(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, state["test"], "", "emerg: bad" if state["test"] else "")
    monkeypatch.setattr(workspacehelper.subprocess, "run", nginx_t)

    def reload():
        state["reloads"] += 1
        return "nginx reloaded"
    monkeypatch.setattr(workspacehelper, "_nginx_reload", reload)
    # the names the service says its instances answer on
    (tmp_path / "state").mkdir()
    monkeypatch.setattr(workspacehelper, "_system", lambda: {"servers_file": str(tmp_path / "state" / "servers.yaml")})
    monkeypatch.setattr(workspacehelper, "SYSTEMD", tmp_path / "systemd")
    (tmp_path / "systemd").mkdir()
    return {"site": site, "state": state, "tmp": tmp_path,
            "hosts": tmp_path / "state" / workspacehelper.workspaceweb.LOGIN_HOSTS_FILE}


def test_check_add_remove(nginx):
    site = nginx["site"]
    assert workspacehelper.login_path("check", "shop.example.com")["ready"] is False
    done = workspacehelper.login_path("add", "shop.example.com")
    text = site.read_text()
    # one line, in the 443 block of that name only
    assert text.count(workspacehelper.PATHS_MARK) == 1
    assert text.index("return 301") < text.index(workspacehelper.PATHS_MARK) < text.index("listen 443 ssl http2")
    assert text.index(workspacehelper.PATHS_MARK) < text.index("other.example.com")
    snippet = workspacehelper.PATHS_SNIPPET.read_text()
    assert "location = /.omniacli/login {" in snippet and "proxy_pass http://127.0.0.1:8090;" in snippet
    assert (nginx["tmp"] / ("shop.conf" + workspacehelper.BEFORE_SUFFIX)).read_text() == SITE
    assert done["undo"].endswith("login-path remove shop.example.com") and nginx["state"]["reloads"] == 1
    assert workspacehelper.login_path("check", "shop.example.com")["ready"] is True
    # again: nothing more
    assert workspacehelper.login_path("add", "shop.example.com")["changed"] == []
    workspacehelper.login_path("remove", "shop.example.com")
    assert site.read_text() == SITE


def test_refused_by_nginx_or_unknown(nginx):
    nginx["state"]["test"] = 1
    with pytest.raises(HelperError, match="files are as before"):
        workspacehelper.login_path("add", "shop.example.com")
    assert nginx["site"].read_text() == SITE
    with pytest.raises(HelperError, match="no server block"):
        workspacehelper.login_path("check", "nowhere.example.com")
    with pytest.raises(HelperError, match="not a domain"):
        workspacehelper.login_path("add", "x; rm -rf /")


def test_kept_every_minute(nginx, monkeypatch):
    """The timer's run: the names of the service's instances, the line where
    it is missing (a site the panel wrote again, a new instance), nothing
    touched and no reload when it is all there; what is not a name is not read."""
    site, hosts, state = nginx["site"], nginx["hosts"], nginx["state"]
    hosts.write_text(json.dumps(["shop.example.com", "other.example.com", "new.example.com", "x; rm -rf /", 7]))
    done = workspacehelper.login_path("sync", "all")
    text = site.read_text()
    assert text.count(workspacehelper.PATHS_MARK) == 2 and done["no_site"] == ["new.example.com"]
    assert state["reloads"] == 1
    # every minute, no backup beside the panel's files
    assert not (nginx["tmp"] / ("shop.conf" + workspacehelper.BEFORE_SUFFIX)).exists()
    again = workspacehelper.login_path("sync", "all")
    assert again["changed"] == [] and again["nginx"] == "nothing to change" and state["reloads"] == 1
    # the panel writes the site again, with a new instance in it: back within the next run
    site.write_text(SITE + "server {\n    listen 443 ssl;\n    server_name new.example.com;\n}\n")
    done = workspacehelper.login_path("sync", "all")
    assert site.read_text().count(workspacehelper.PATHS_MARK) == 3 and done["no_site"] == [] and done["ready"] is True
    assert workspacehelper.login_path("check", "all")["ready"] is True
    # one name's only, the others' stay
    workspacehelper.login_path("remove", "other.example.com")
    assert site.read_text().count(workspacehelper.PATHS_MARK) == 2
    # all of ours gone, the snippet too: the files as the panel wrote them
    workspacehelper.login_path("remove", "all")
    assert workspacehelper.PATHS_MARK not in site.read_text() and not workspacehelper.PATHS_SNIPPET.exists()
    assert site.read_text() == SITE + "server {\n    listen 443 ssl;\n    server_name new.example.com;\n}\n"
    # no file of names, or a link in its place: nothing done
    hosts.unlink()
    assert workspacehelper.login_path("sync", "all")["changed"] == []
    hosts.symlink_to(site)
    assert workspacehelper.login_path("sync", "all")["changed"] == []


def test_the_timer(nginx, monkeypatch):
    ran = []
    real = workspacehelper._run

    def run(*cmd, check=True):
        ran.append(cmd)
        return real(*cmd, check=check)
    monkeypatch.setattr(workspacehelper, "_run", run)
    nginx["hosts"].write_text(json.dumps(["shop.example.com"]))
    done = workspacehelper.login_path("keep", "on")
    service, timer = workspacehelper._keep_units()
    assert "workspace-helper login-path sync all" in service.read_text() and "OnUnitActiveSec=60" in timer.read_text()
    assert ("systemctl", "enable", "--now", "omniacli-login-path.timer") in ran
    assert done["keep"] is True and nginx["site"].read_text().count(workspacehelper.PATHS_MARK) == 1
    off = workspacehelper.login_path("keep", "off")
    assert off["keep"] is False and not service.exists() and not timer.exists()
    # the lines stay until they are taken away
    assert nginx["site"].read_text().count(workspacehelper.PATHS_MARK) == 1
    with pytest.raises(HelperError, match="on or off"):
        workspacehelper.login_path("keep", "maybe")
