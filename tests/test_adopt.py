# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Odoo already on a machine made OmniaCLI's to operate (adopt.py): the
ACLs, the venv and the sudoers lines, without root (what it runs is
recorded)."""

import subprocess

import pytest

from omniacli.odoo import adopt
from omniacli.odoo import ops as odoo_ops


@pytest.fixture
def ran(monkeypatch):
    calls = []

    def call(cmd, user=None):
        calls.append((cmd, user))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(adopt, "call", call)
    return calls


def test_read_access_only_where_it_is_missing(tmp_path, ran):
    closed = tmp_path / "var-odoo"
    instance = closed / "shop"
    logs = instance / "logs"
    logs.mkdir(parents=True)
    conf = instance / "odoo.conf"
    conf.write_text("[options]\n")
    closed.chmod(0o744)
    instance.chmod(0o700)
    logs.chmod(0o744)
    conf.chmod(0o644)
    given = adopt.let_read("omniacli", conf)
    setfacl = [c for c, _ in ran]
    # the folders others cannot enter get x, the readable config nothing
    assert ["setfacl", "-m", "u:omniacli:x", str(closed)] in setfacl
    assert ["setfacl", "-m", "u:omniacli:x", str(instance)] in setfacl
    assert not any(str(conf) in c for c in setfacl)
    assert given[-2:] == [f"x {closed}", f"x {instance}"]
    ran.clear()
    adopt.let_read("omniacli", logs, folder=True)
    # the log folder: what is in it and what comes (a default ACL)
    assert ["setfacl", "-R", "-m", "u:omniacli:rX", str(logs)] in [c for c, _ in ran]
    assert ["setfacl", "-R", "-d", "-m", "u:omniacli:rX", str(logs)] in [c for c, _ in ran]
    instance.chmod(0o755)
    closed.chmod(0o755)


def test_the_sudoers_lines(monkeypatch, tmp_path):
    monkeypatch.setattr(adopt.shutil, "which", lambda name: f"/usr/bin/{name}")
    lines = adopt.sudoers_lines("omniacli", "odoo", tmp_path / "venv" / "bin", "odona-shop.service")
    assert lines == [
        f"omniacli ALL=(odoo) NOPASSWD:SETENV: {tmp_path}/venv/bin/",
        "omniacli ALL=(odoo) NOPASSWD:SETENV: /usr/bin/psql",
        "omniacli ALL=(root) NOPASSWD: /usr/bin/systemctl --no-ask-password restart odona-shop.service",
        "omniacli ALL=(root) NOPASSWD: /usr/bin/systemctl --no-ask-password start odona-shop.service",
        "omniacli ALL=(root) NOPASSWD: /usr/bin/systemctl --no-ask-password stop odona-shop.service",
    ]


def test_the_sudoers_file_keeps_its_lines_and_is_checked(monkeypatch, tmp_path, ran):
    sudoers = tmp_path / "omniacli-instances"
    monkeypatch.setattr(adopt, "SUDOERS", sudoers)
    adopt.write_sudoers(["a line", "b line"])
    adopt.write_sudoers(["b line", "c line"])
    assert sudoers.read_text().splitlines()[1:] == ["a line", "b line", "c line"]
    assert sudoers.stat().st_mode & 0o777 == 0o440
    assert all(c[:2] == ["visudo", "-cf"] for c, _ in ran)

    def refuse(cmd, user=None):
        return subprocess.CompletedProcess(cmd, 1, "", "syntax error")

    monkeypatch.setattr(adopt, "call", refuse)
    with pytest.raises(RuntimeError, match="visudo refused"):
        adopt.write_sudoers(["d line"])
    assert "d line" not in sudoers.read_text() and not list(tmp_path.glob(".omniacli-*"))


def test_a_system_python_gets_a_venv_of_its_own(monkeypatch, tmp_path, ran):
    monkeypatch.setattr(odoo_ops, "ADOPTED_VENVS", tmp_path / "venvs")
    system = tmp_path / "usr" / "bin"
    system.mkdir(parents=True)
    (system / "python3").write_text("")
    assert adopt.system_python(system) == system / "python3"
    venv_bin = adopt.ready_venv(system)
    venv = odoo_ops.adopted_venv(system / "python3")
    assert venv_bin == venv / "bin" and venv.parent == tmp_path / "venvs"
    cmds = [c for c, _ in ran]
    assert [str(system / "python3"), "-m", "venv", "--system-site-packages", str(venv)] in cmds
    # click-odoo checked with odoo, as root (the venv is OmniaCLI's)
    assert cmds[-1] == [str(venv / "bin" / "python"), "-c", "import click_odoo, odoo"]
    assert all(user is None for _, user in ran)
    # a real venv is not a system python
    (tmp_path / "pyvenv.cfg").write_text("home = /usr/bin\n")
    assert adopt.system_python(tmp_path / "bin") is None
