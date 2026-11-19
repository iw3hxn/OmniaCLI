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
    assert machines.Machine.parse({**FORM, "name": " Acme Cloud "}, "c").name == "acme-cloud"
    assert machines.Machine.parse({**FORM, "name": "Inaria"}, "c").name == "inaria"
    for key, bad in (("name", "x" * 33), ("host", "a host; rm"), ("port", "ssh"), ("port", 70000),
                     ("user", "root; id"), ("host_key", "not a key"), ("package_url", "http://example.com/x.deb"),
                     ("package_url", "https://example.com/x.deb'; id")):
        with pytest.raises(machines.MachineError):
            machines.Machine.parse({**FORM, key: bad}, "central")
    assert machines.Machine.parse({**FORM, "firewall": False}, "Not A Name!").here == "omniacli"
    assert machines.Machine.parse(FORM, "c").kind == "clean"
    with pytest.raises(machines.MachineError, match="clean or existing"):
        machines.Machine.parse({**FORM, "kind": "panel"}, "c")
    assert machines.here_name("Omnia-Server.example.com") == "omnia-server"
    # only the address: the name from it, port 22, root, the host key read later
    bare = machines.Machine.parse({"host": "ERP.acme.it"}, "central")
    assert (bare.name, bare.port, bare.user, bare.host_key) == ("erp-acme-it", 22, "root", "")
    assert machines.name_from_host("203.0.113.10") == "203-0-113-10"
    assert machines.name_from_host("2001:db8::1") == "2001-db8-1"


@pytest.mark.parametrize("said, told", [
    ("root@203.0.113.10: Permission denied (publickey).", "run the command of step 1"),
    ("sudo: a password is required", "deploy needs sudo without a password"),
    ("ssh: connect to host 203.0.113.10 port 22: Connection refused", "No SSH server on 203.0.113.10:22"),
    ("kex_exchange_identification: something odd", "failed: kex_exchange_identification: something odd"),
])
async def test_the_access_is_tried_first_and_said_plainly(tmp_path, said, told):
    sshlinks.key_file(tmp_path, sshlinks.PROVISION_KEY_NAME).write_text("key")
    fake = tmp_path / "ssh-fake"
    fake.write_text(f"#!/bin/sh\necho '{said}' >&2\nexit 255\n")
    fake.chmod(0o755)
    machine = machines.Machine.parse({**FORM, "user": "deploy"}, "c")
    with pytest.raises(machines.MachineError, match=told):
        await machines.check_access(machine, tmp_path, ssh=str(fake))
    fake.write_text("#!/bin/sh\nexit 0\n")
    await machines.check_access(machine, tmp_path, ssh=str(fake))


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
                        base64.b64encode(line.encode()).decode(), "clean"]
    # a server that already runs Odoo: no firewall, whatever the form says
    existing = machines.Machine.parse({**FORM, "kind": "existing", "firewall": True}, "central")
    args = shlex.split(machines.script_input(existing, line, "h").decode().partition("\n")[0])
    assert args[4] == "0" and args[-1] == "existing"
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

    async def fetch_host_key(host, port):
        return HOST_KEY

    async def check_access(machine, ssh_folder, ssh="ssh"):
        if machine.host == "198.51.100.9":
            raise machines.MachineError("The server does not accept OmniaCLI's key yet: run the command of step 1 there, as root")

    monkeypatch.setattr(sshlinks, "ensure_key", ensure_key)
    monkeypatch.setattr(sshlinks, "fetch_host_key", fetch_host_key)
    monkeypatch.setattr(machines, "check_access", check_access)
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
    # the provisioning key is made with the page: the command is there to copy
    assert page.status_code == 200 and "id_ed25519_provision@here" in page.text and "copyable" in page.text
    assert (await client.get("/api/machines", headers=as_user("dev"))).status_code == 403
    assert (await client.get("/machines", headers=as_user("dev"))).status_code == 403
    post = {**as_user("admin", post=True), "Accept": "application/json"}
    assert (await client.post("/api/machines/add", headers={**as_user("dev", post=True)}, json=FORM)).status_code == 403
    refused = await client.post("/api/machines/add", headers=post, json={"host": "198.51.100.9"})
    assert refused.status_code == 400 and "step 1" in refused.json()["error"]
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
    # only the address: named from it, the host key read; the same address again gets -2
    for expected in ("203-0-113-10", "203-0-113-10-2"):
        resp = await client.post("/api/machines/add", headers=post, json={"host": "203.0.113.10"})
        assert resp.status_code == 200, resp.text
        await wait(client, resp.json()["job"])
        saved = yaml.safe_load(client.settings.links_file.read_text())["links"][expected]
        assert saved["ssh"]["host_key"] == HOST_KEY
    # the cards: the machines linked, each with its last add job when it did not end well
    cards = {c["name"]: c for c in (await client.get("/api/machines", headers=as_user("admin"))).json()["machines"]}
    assert set(cards) == {"acme-cloud", "203-0-113-10", "203-0-113-10-2"}
    assert cards["acme-cloud"]["linked"] and cards["acme-cloud"]["job"]["status"] == "failed"
    assert machines.STEPS[cards["acme-cloud"]["job"]["step"]] == "the link "


def test_the_progress_of_an_add_job():
    lines = ["10:00:00 --- prepare root@h:22 (host key x)", "10:00:01 --- the system", "10:00:02 some output",
             "10:01:00 --- updates", "10:03:00 --- the link back"]
    assert machines.STEPS[machines.progress(lines)] == "the link back"
    assert machines.progress([]) == 0
    assert machines.STEPS[machines.progress(lines + ["10:05:00 --- the link acme, through SSH as omniacli-link"])] == "the link "


def test_what_discover_found_becomes_productions():
    unit = {"odoo_config": "/var/odoo/shop/odoo.conf", "restart": {"type": "systemd", "service": "odona-shop.service"}}
    found = [
        {"type": "odoo", "label": "process 1, unit odona-shop", "name": "shop",
         "instance": {"database": "shop", "repositories": [{"path": "/var/odoo/shop/src"}], **unit},
         "facts": [["odoo-bin", "/var/odoo/shop/src/odoo-bin (Odoo 19)"]]},
        {"type": "odoo", "label": "unit odona-shop-old", "name": "shop", "instance": {"database": "shop", **unit}},
        {"type": "odoo", "label": "process 2", "name": "c", "instance": {}, "skip": "in a container"},
        {"type": "odoo", "label": "unit odoo", "name": "erp", "instance": {}, "known": "erp/production"},
        {"type": "generic", "error": "boom"},
        # an Odoo started by hand in the unit of another (an update someone runs): not an instance
        {"type": "odoo", "label": "process 9", "name": "shop", "instance": {"restart": {"type": "systemd", "service": ""}}},
    ]
    assert machines.left_out(found[-1]) == "started by hand, no service of its own"
    items = machines.instances_to_add(found, {"erp"})
    assert [(i["name"], i["role"], i["type"]) for i in items] == [
        ("shop", "production", "odoo"), ("shop-2", "production", "odoo")]
    # the code is the panel's: nothing to pull, odoo-bin as found
    assert items[0]["instance"] == {"database": "shop", "repositories": [], "odoo_bin": "/var/odoo/shop/src/odoo-bin", **unit}
    assert items[1]["instance"] == {"database": "shop", "repositories": [], **unit}
