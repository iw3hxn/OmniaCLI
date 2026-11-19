# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Odoo config tab: the writer of the config file (write_config.py) and
its routes, the options read from the instance's Odoo replaced here."""

import json
import subprocess
import sys

import omniacli.odoo as odoo_plugin
from omniacli.odoo import ops as odoo_ops
from omniacli.odoo import write_config

from .test_app import AUTH, POST, client, wait_job  # noqa: F401 - the fixture

CONF = """[options]
; the panel's
db_user = odoo
workers = 2
proxy_mode = True

[queue_job]
channels = root:2
"""


def test_the_file_keeps_its_other_lines():
    text, changed = write_config.rewrite(CONF, {"workers": "4", "limit_time_real": "1200"}, ["proxy_mode"])
    assert changed == ["limit_time_real", "proxy_mode", "workers"]
    assert text == ("[options]\n; the panel's\ndb_user = odoo\nworkers = 4\nlimit_time_real = 1200\n\n"
                    "[queue_job]\nchannels = root:2\n")
    # no [options] yet: made
    assert write_config.rewrite("", {"workers": "2"}, [])[0] == "[options]\nworkers = 2\n"


def test_the_layout_and_odd_values_are_refused():
    for bad in ({"set": {"db_host": "x"}}, {"unset": ["http_port"]}, {"set": {"workers": "2\nadmin_passwd = x"}},
                {"set": {"Bad Name": "1"}}, {"other": 1}):
        try:
            write_config.check(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad} accepted")
    assert odoo_ops.locked_options() == write_config.LOCKED


def test_the_script_writes_in_place(tmp_path):
    conf = tmp_path / "odoo.conf"
    conf.write_text(CONF)
    conf.chmod(0o640)
    done = subprocess.run([sys.executable, write_config.__file__, str(conf)], input=json.dumps({"set": {"workers": "6"}}),
                          capture_output=True, text=True, check=True)
    assert odoo_ops.answer_of(odoo_ops.CONFIG_MARK, 0, done.stdout) == {"changed": ["workers"]}
    assert "workers = 6" in conf.read_text() and conf.stat().st_mode & 0o777 == 0o640
    done = subprocess.run([sys.executable, write_config.__file__, str(conf)], input=json.dumps({"set": {"db_user": "x"}}),
                          capture_output=True, text=True, check=True)
    assert "layout" in odoo_ops.answer_of(odoo_ops.CONFIG_MARK, 0, done.stdout)["error"]


async def test_the_tab_reads_and_saves(client, env, monkeypatch):  # noqa: F811
    async def options(instance):
        return {"options": [{"name": "workers", "help": "processes", "kind": "int", "group": "Multiprocessing",
                             "default": 0}], "values": {"workers": "2"}, "secrets": [], "locked": ["db_host"],
                "writable": True, "file": "/etc/odoo.conf", "version": "19.0"}
    written, restarted = [], []

    async def write(job, instance, changes):
        written.append(changes)
        return list(changes["set"])

    async def restart(job, instance):
        restarted.append(instance.key)

    monkeypatch.setattr(odoo_ops, "odoo_options", options)
    monkeypatch.setattr(odoo_ops, "write_odoo_options", write)
    monkeypatch.setattr(odoo_plugin, "restart", restart)
    url = "/x/odoo/servers/srv/production/odoo-config"
    data = (await client.get(url, headers={"Authorization": AUTH})).json()
    assert data["values"] == {"workers": "2"} and data["writable"]
    # the layout's options are refused before any job
    resp = await client.post(url, json={"set": {"db_host": "x"}}, headers=POST)
    assert resp.status_code == 400 and "db_host" in resp.json()["error"]
    assert (await client.post(url, json={}, headers=POST)).status_code == 400
    resp = await client.post(url, json={"set": {"workers": "4"}}, headers=POST)
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success", job["lines"]
    assert written == [{"set": {"workers": "4"}, "unset": []}] and restarted == ["srv/production"]
    # the page has the tab for an Odoo instance
    page = (await client.get("/servers/srv", headers={"Authorization": AUTH, "Accept": "text/html"})).text
    assert 'data-tab="odoo-config"' in page and 'data-url="/x/odoo/servers/srv/production/odoo-config"' in page
