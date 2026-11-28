# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
import asyncio
import gc
import io
import os
import subprocess
import sys
import threading
import zipfile
from pathlib import Path

import pytest
import yaml

from omniacli import config, workspacehelper
from omniacli.auth import hash_password, hash_token
from omniacli.composehelper import HelperError
from omniacli.config import Restart, parse_settings

PASSWORD = "secret"
TOKEN = "mcp-test-token"
# linked instances: one may act on the servers, one only watches
LINK_TOKEN = "link-developer-token"
LINK_VIEW_TOKEN = "link-user-token"
USER_HASH = hash_password(PASSWORD, iterations=1000)


def sh(*cmd, cwd=None) -> str:
    return subprocess.run(
        cmd, cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def commit(repo: Path, name: str, content: str, message: str) -> str:
    (repo / name).write_text(content)
    sh("git", "add", name, cwd=repo)
    sh("git", "commit", "-q", "-m", message, cwd=repo)
    return sh("git", "rev-parse", "HEAD", cwd=repo)


def make_remote(base: Path, name: str) -> Path:
    """A bare remote with branch main and a test branch forked from it."""
    remote = base / f"{name}.git"
    sh("git", "init", "-q", "--bare", "-b", "main", str(remote))
    seed = base / f"{name}_seed"
    sh("git", "clone", "-q", str(remote), str(seed))
    commit(seed, "base.txt", "base\n", "initial")
    sh("git", "push", "-q", "origin", "HEAD:main", cwd=seed)
    sh("git", "push", "-q", "origin", "HEAD:test", cwd=seed)
    return remote


def clone(remote: Path, dest: Path, branch: str) -> Path:
    sh("git", "clone", "-q", "-b", branch, str(remote), str(dest))
    return dest


# the storages' binds (workspacehelper.bind_storage) need root and systemd: in
# the tests a storage is used where it is, its folder, still one of
# workspaces.storage (name: folder); a test that uses the folder as the name
# finds its root path there too. The real ones are tested with these
# (test_stacktemplates, storage binds)
REAL_BOUND_ROOT = config.bound_root
REAL_BIND_STORAGE = workspacehelper.bind_storage


@pytest.fixture(autouse=True)
def storage_where_it_is(monkeypatch):
    def bind_storage(storage):
        roots = workspacehelper.storage_roots()
        if storage not in roots:
            raise HelperError(f"{storage} is not a storage of workspaces.storage of {workspacehelper.SYSTEM_CONFIG}")
        return Path(roots[storage])
    monkeypatch.setattr(config, "bound_root", lambda storage: storage)
    monkeypatch.setattr(workspacehelper, "bind_storage", bind_storage)


@pytest.fixture(autouse=True)
def server_page_parity(monkeypatch):
    """OMNIACLI_PARITY=1: every instance's page and every page of class B a test draws is drawn again from its
    model and compared (tests/serverparity.py, AD-017 slices 3-4); off by default (each page twice)."""
    if os.environ.get("OMNIACLI_PARITY") == "1":
        from .serverparity import Twice
        Twice(monkeypatch)


@pytest.fixture(autouse=True)
def mail_guard_here(monkeypatch, tmp_path):
    """The capturing stagings' SMTP rules are kept in the test's folder, not in /etc/omniacli."""
    monkeypatch.setattr(workspacehelper, "MAIL_GUARD", tmp_path / "mail-guard.json")


@pytest.fixture(autouse=True)
def docker_here(monkeypatch):
    """Workspace instances are made in the tests whether docker is installed or not."""
    from omniacli import workspaceops
    monkeypatch.setattr(workspaceops, "docker_missing", lambda: None)


@pytest.fixture(autouse=True)
def no_root_helper_for_the_log(monkeypatch, request):
    """A job's read of its instance's log (AD-019 slice 2) never runs the real root helper in the tests: on a
    runner with sudo and docker it did, and a job outlived the test that waited for it (CI of 2026.1.5532). The
    tests of the reading itself keep it."""
    if request.module.__name__.rsplit(".", 1)[-1] in ("test_instance_log", "test_instance_log_review"):
        return
    from omniacli import instancelog

    async def no_helper(instance, cursor):
        raise OSError("no root helper in the tests")
    monkeypatch.setattr(instancelog, "helper_read", no_helper)


@pytest.fixture(autouse=True)
def no_root_helper_for_the_mail(monkeypatch, request):
    """The monitor's read of an instance's mail (AD-019 slice 3, in the round of the logs) never runs the real root
    helper nor psql in the tests, nor a staging's count of its caught mail; the tests of the mail itself keep them."""
    if request.module.__name__.rsplit(".", 1)[-1] == "test_instance_mail":
        return
    from omniacli import instancemail, stagingmail

    async def no_database(instance, from_id):
        raise OSError("no root helper in the tests")

    async def no_mailpit(instance, since):
        raise OSError("no root helper in the tests")
    monkeypatch.setattr(instancemail, "ask", no_database)
    monkeypatch.setattr(stagingmail, "held", no_mailpit)


@pytest.fixture(autouse=True)
def login_path_passed(monkeypatch):
    """The instances' nginx pass Log in as's path here (no network in the tests)."""
    from omniacli import loginas

    async def reaches_here(host):
        return True

    async def passed_locally(host, port=443):
        return False
    monkeypatch.setattr(loginas, "reaches_here", reaches_here)
    monkeypatch.setattr(loginas, "passed_locally", passed_locally)


@pytest.fixture(autouse=True)
def git_identity(monkeypatch, tmp_path):
    # the start's look at the names interrupted jobs left: at once, so that a test sees it ended
    monkeypatch.setenv("OMNIACLI_INTERRUPTED_WAIT", "0")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    # ops copies os.environ at import time
    import omniacli.ops as ops

    monkeypatch.setattr(ops, "_ENV", {**ops._ENV, **{
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"),
    }})


@pytest.fixture
def fake_venv(tmp_path) -> Path:
    """click-odoo scripts that record their calls instead of touching Odoo."""
    bin_dir = tmp_path / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    calls = tmp_path / "calls.log"
    (bin_dir / "click-odoo-update").write_text(
        f'#!/bin/sh\necho "update $*" >> {calls}\necho updated\n'
    )
    # fails when the file restore-fails exists next to the calls
    (bin_dir / "click-odoo-restoredb").write_text(
        f'#!/bin/sh\necho "restore $*" >> {calls}\n'
        f'if [ -e {tmp_path / "restore-fails"} ]; then echo "restore failed" >&2; exit 1; fi\n'
    )
    # click-odoo runs a script: its name and arguments are recorded
    (bin_dir / "click-odoo").write_text(
        f'#!/bin/sh\nargs=""\nfor a; do case "$a" in */*.py) a=$(basename "$a");; esac; args="$args $a"; done\n'
        f'echo "click-odoo$args" >> {calls}\n'
    )
    (bin_dir / "click-odoo-backupdb").write_text(
        f'#!/bin/sh\necho "backup $*" >> {calls}\n'
        'for last; do :; done\necho dump > "$last"\n'
    )
    for script in bin_dir.iterdir():
        script.chmod(0o755)
    return bin_dir


def fake_backups_are(venv_bin: Path, content: bytes | None = None) -> bytes:
    """The fake click-odoo-backupdb writes content: by default a zip Odoo could
    read, as a restore of a backup from another machine asks. What it writes."""
    if content is None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            zf.writestr("dump.sql", "-- dump")
        content = buffer.getvalue()
    made = venv_bin / "backup-content"
    made.write_bytes(content)
    script = venv_bin / "click-odoo-backupdb"
    script.write_text(script.read_text().replace('echo dump > "$last"', f'cp {made} "$last"'))
    return content


@pytest.fixture
def fake_sudo(monkeypatch, tmp_path):
    """A sudo first in ops' PATH: it records its arguments, answers -l with
    yes, sets the VAR=value it is given and runs the command as this user."""
    bin_dir = tmp_path / "sudo-bin"
    bin_dir.mkdir()
    calls = tmp_path / "sudo.log"
    (bin_dir / "sudo").write_text(
        f'#!/bin/sh\necho "sudo $*" >> {calls}\n'
        'while [ $# -gt 0 ]; do case "$1" in\n'
        '  -n) shift ;;\n  -u) shift 2 ;;\n  -l) exit 0 ;;\n'
        '  *=*) export "$1"; shift ;;\n  *) break ;;\n'
        'esac; done\nexec "$@"\n')
    (bin_dir / "sudo").chmod(0o755)
    from omniacli import ops

    monkeypatch.setitem(ops._ENV, "PATH", f"{bin_dir}{os.pathsep}{ops._ENV.get('PATH', '')}")
    return calls


@pytest.fixture
def restart_calls(monkeypatch, tmp_path):
    calls = tmp_path / "calls.log"
    monkeypatch.setattr(
        Restart, "command",
        lambda self, action="restart", *args: ["sh", "-c", f'echo "{action} {self.service}" >> {calls}', "sh", action, *args]
    )
    return calls


@pytest.fixture
def env(tmp_path, fake_venv, restart_calls):
    """A server with production on main and test on test, two repositories."""
    remotes = {name: make_remote(tmp_path, name) for name in ("odoo", "addons")}
    log = tmp_path / "odoo.log"
    log.write_text("".join(f"line {i}\n" for i in range(10)))

    def instance(role, branch):
        return {
            "database": f"DB_{role}",
            "odoo_config": str(tmp_path / "odoo.conf"),
            "log_file": str(log),
            "venv_bin": str(fake_venv),
            "restart": {"type": "systemd", "service": f"odoo-{role}", "sudo": False},
            "repositories": [
                {
                    "name": name,
                    "path": str(clone(remote, tmp_path / role / name, branch)),
                    "branch": branch,
                }
                for name, remote in remotes.items()
            ],
        }

    servers_file = tmp_path / "conf" / "servers.yaml"
    servers_file.parent.mkdir()
    servers_file.write_text(
        yaml.safe_dump(
            {"servers": {"srv": {"production": instance("production", "main"),
                                 "test": instance("test", "test")}}}
        )
    )
    settings = parse_settings(
        {
            "auth": {
                "users": {
                    "admin": USER_HASH,
                    "viewer": {"password": USER_HASH, "role": "user"},
                    "dev": {"password": USER_HASH, "role": "developer"},
                },
                "tokens": {"tester": hash_token(TOKEN)},
                "links": {
                    "omnia": {"token": hash_token(LINK_TOKEN), "role": "developer"},
                    "watcher": {"token": hash_token(LINK_VIEW_TOKEN), "role": "user"},
                },
            },
            "backup_dir": str(tmp_path / "backups"),
            "jobs_dir": str(tmp_path / "jobs"),
            "servers_file": "servers.yaml",
            # the shells open: test_machine.py closes them
            "machine": "development",
        },
        base_dir=servers_file.parent,
    )
    return {"settings": settings, "remotes": remotes, "tmp": tmp_path, "calls": restart_calls}


class BasicAsSession:
    """The tests send HTTP Basic credentials, as OmniaCLI took them before the
    login form: this turns valid ones into the session cookie the form would
    give (one per user), and drops wrong ones, so every page is reached the
    way a browser reaches it now."""

    def __init__(self, app):
        self.app = app
        self._cookies = {}

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = [(k, v) for k, v in scope["headers"] if k != b"authorization"]
            basic = dict(scope["headers"]).get(b"authorization", b"")
            if basic.lower().startswith(b"basic "):
                import base64

                from omniacli.sessions import COOKIE

                user, _, password = base64.b64decode(basic[6:]).decode().partition(":")
                state = self.app.state
                if state.authenticator.check(user, password):
                    key = (user, password)
                    if key not in self._cookies or not state.sessions.user(self._cookies[key], state.settings.users):
                        self._cookies[key] = state.sessions.create(user, state.settings.users[user])
                    headers.append((b"cookie", f"{COOKIE}={self._cookies[key]}".encode()))
                scope = dict(scope, headers=headers)
        await self.app(scope, receive, send)


# a test still running after HANG_SECONDS: the asyncio tasks not done are
# printed with where each one waits (a task that does not end when it is
# cancelled hangs the loop's closing, which faulthandler shows only as select)
HANG_SECONDS = 240


def _dump_tasks(nodeid: str) -> None:
    out = sys.__stderr__
    print(f"\n=== {nodeid}: still running after {HANG_SECONDS} s; the asyncio tasks not done:", file=out)
    for obj in gc.get_objects():
        if isinstance(obj, asyncio.Task) and not obj.done():
            print(repr(obj), file=out)
            obj.print_stack(file=out)
    out.flush()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    timer = threading.Timer(HANG_SECONDS, _dump_tasks, args=(item.nodeid,))
    timer.daemon = True
    timer.start()
    try:
        yield
    finally:
        timer.cancel()
