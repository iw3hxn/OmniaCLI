# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Add a machine (machines.py): a clean server prepared through SSH by
provision.sh, then linked here through SSH."""

import asyncio
import base64
import shlex
import subprocess

import httpx
import pytest
import yaml

from omniacli import machines, sshlinks
from omniacli.app import create_app
from omniacli.auth import hash_token
from omniacli.jobs import Job, JobFailed

from .conftest import BasicAsSession
from .test_links import HOST, as_user

# a well formed ed25519 host key (its fingerprint is computed from it)
HOST_KEY = "ssh-ed25519 " + base64.b64encode(b"\0\0\0\x0bssh-ed25519\0\0\0\x20" + bytes(range(32))).decode()
FORM = {"name": "acme-cloud", "host": "203.0.113.10", "port": 22, "user": "root", "host_key": HOST_KEY}


def test_the_form_is_checked():
    machine = machines.Machine.parse(FORM, "central")
    assert (machine.name, machine.host, machine.port, machine.user) == ("acme-cloud", "203.0.113.10", 22, "root")
    assert machine.package_url == machines.PACKAGE_URL and machine.firewall
    for key, bad in (("name", "Acme Cloud"), ("host", "a host; rm"), ("port", "ssh"), ("port", 70000),
                     ("user", "root; id"), ("host_key", ""), ("package_url", "http://example.com/x.deb"),
                     ("package_url", "https://example.com/x.deb'; id")):
        with pytest.raises(machines.MachineError):
            machines.Machine.parse({**FORM, key: bad}, "central")
    assert machines.Machine.parse({**FORM, "firewall": False}, "Not A Name!").here == "omniacli"
    assert machines.here_name("Omnia-Server.example.com") == "omnia-server"


def test_ssh_reads_only_the_key_and_the_host_key_given(tmp_path):
    machine = machines.Machine.parse({**FORM, "port": 2222, "user": "deploy"}, "central")
    cmd = machines.ssh_command(machine, tmp_path / "key", tmp_path / "known", machines.remote_command(machine))
    joined = " ".join(cmd)
    for option in ("-F /dev/null", "IdentitiesOnly=yes", "IdentityAgent=none", "BatchMode=yes",
                   "StrictHostKeyChecking=yes", f"UserKnownHostsFile={tmp_path / 'known'}",
                   "GlobalKnownHostsFile=/dev/null", "-p 2222", "-l deploy"):
        assert option in joined
    # another user than root: sudo without a password, nothing typed
    assert cmd[-2:] == ["203.0.113.10", "sudo -n bash -s"]
    assert machines.remote_command(machines.Machine.parse(FORM, "central")) == "bash -s"


def test_the_values_reach_bash_on_its_input(tmp_path):
    machine = machines.Machine.parse(FORM, "central")
    line = 'restrict,port-forwarding,permitopen="127.0.0.1:8090",command="/bin/false" ssh-ed25519 AAAA x@y'
    data = machines.script_input(machine, line, "sha256$abc").decode()
    first, _, script = data.partition("\n")
    args = shlex.split(first)
    assert args[:2] == ["set", "--"]
    assert args[2:] == [machines.PACKAGE_URL, "22", "1", machines.LINK_USER, "central", "sha256$abc",
                        base64.b64encode(line.encode()).decode()]
    assert script == machines.PROVISION_SCRIPT.read_text()
    # the script is read whole before it runs: what it runs cannot eat the rest of its input
    assert script.rstrip().endswith('main "$@" </dev/null')
    assert subprocess.run(["bash", "-n", str(machines.PROVISION_SCRIPT)]).returncode == 0


def test_the_link_goes_through_ssh_as_the_link_user():
    machine = machines.Machine.parse({**FORM, "port": 2222}, "central")
    link = machines.link_of(machine, "the-token")
    assert (link.name, link.url, link.token) == ("acme-cloud", machines.REMOTE_URL, "the-token")
    assert (link.ssh.host, link.ssh.port, link.ssh.user, link.ssh.host_key) == (
        "203.0.113.10", 2222, machines.LINK_USER, HOST_KEY)
    assert 'permitopen="127.0.0.1:8090"' in sshlinks.authorized_line("ssh-ed25519 AAAA x@y", link)


async def test_provision_runs_the_script_through_ssh(tmp_path):
    folder = tmp_path / "ssh"
    folder.mkdir()
    sshlinks.key_file(folder, sshlinks.PROVISION_KEY_NAME).write_text("key")
    fake = tmp_path / "ssh-fake"
    seen = tmp_path / "seen"
    fake.write_text(f'#!/bin/sh\necho "$@" > {seen}.args\ncat > {seen}.input\necho OMNIACLI-PROVISIONED\n')
    fake.chmod(0o755)
    machine = machines.Machine.parse(FORM, "central")
    job = Job(id="j", title="t", instances=[], source="test")
    await machines.provision(job, machine, folder, "ssh-ed25519 AAAA link@here", hash_token("tok"), ssh=str(fake))
    sent = (tmp_path / "seen.input").read_text()
    assert sent.startswith("set -- ") and hash_token("tok") in sent.splitlines()[0]
    args = (tmp_path / "seen.args").read_text()
    assert hash_token("tok") not in args and hash_token("tok") not in "\n".join(job.lines)
    fake.write_text("#!/bin/sh\ncat >/dev/null\necho 'PROVISION FAILED: not root'\nexit 1\n")
    with pytest.raises(JobFailed):
        await machines.provision(job, machine, folder, "ssh-ed25519 AAAA link@here", "h", ssh=str(fake))


async def test_without_the_provisioning_key_nothing_runs(tmp_path):
    job = Job(id="j", title="t", instances=[], source="test")
    with pytest.raises(JobFailed, match="no provisioning key"):
        await machines.provision(job, machines.Machine.parse(FORM, "c"), tmp_path, "pub", "h", ssh="/bin/false")


@pytest.fixture
async def client(env, monkeypatch):
    async def ensure_key(folder, name=sshlinks.KEY_NAME):
        return f"ssh-ed25519 AAAA {name}@here"

    monkeypatch.setattr(sshlinks, "ensure_key", ensure_key)
    monkeypatch.setattr(machines, "LINK_WAIT", 1)
    app = create_app(env["settings"])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=BasicAsSession(app)),
                                 base_url=f"http://{HOST}") as c:
        c.settings = env["settings"]
        yield c


async def wait(client, job_id):
    for _ in range(300):
        job = (await client.get(f"/api/jobs/{job_id}", headers=as_user("admin"))).json()
        if job["status"] not in ("queued", "running"):
            return job
        await asyncio.sleep(0.02)
    raise AssertionError("the job did not end")


async def test_an_admin_adds_a_machine(client, monkeypatch):
    ran = []

    async def provision(job, machine, ssh_folder, link_public_key, token_hash, ssh="ssh"):
        ran.append((machine.name, link_public_key, token_hash))
        job.log("prepared")

    monkeypatch.setattr(machines, "provision", provision)
    page = await client.get("/machines", headers=as_user("admin"))
    assert page.status_code == 200 and "Make the key" in page.text
    assert (await client.get("/machines", headers=as_user("dev"))).status_code == 403
    post = {**as_user("admin", post=True), "Accept": "application/json"}
    assert (await client.post("/api/machines/add", headers={**as_user("dev", post=True)}, json=FORM)).status_code == 403
    assert (await client.post("/api/machines/add", headers=post, json={**FORM, "host_key": ""})).status_code == 400
    resp = await client.post("/api/machines/add", headers=post, json=FORM)
    assert resp.status_code == 200, resp.text
    job = await wait(client, resp.json()["job"])
    # prepared and linked; no tunnel in the tests, so the link does not answer
    assert job["status"] == "failed" and "its link does not answer yet" in job["error"]
    assert ran and ran[0][:2] == ("acme-cloud", "ssh-ed25519 AAAA id_ed25519@here")
    links = yaml.safe_load(client.settings.links_file.read_text())["links"]
    saved = links["acme-cloud"]
    assert saved["url"] == machines.REMOTE_URL and saved["ssh"]["user"] == machines.LINK_USER
    assert hash_token(saved["token"]) == ran[0][2]
    again = await client.post("/api/machines/add", headers=post, json=FORM)
    assert again.status_code == 400 and "already exists" in again.json()["error"]
