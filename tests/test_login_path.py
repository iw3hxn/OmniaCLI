# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Log in as on a site of another nginx (a hosting panel's): its server block
of 443 for the instance's name gets one include line, nginx -t checks it,
the files are put back when it refuses; removed again by remove."""

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
    return {"site": site, "state": state, "tmp": tmp_path}


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
