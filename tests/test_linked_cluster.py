# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A database on the cluster of a linked OmniaCLI (AD-003): what the holder
writes (pg_hba, the cluster's project, the firewall, its CA and
certificate), what the asker does before and after (the verifier in place
of the password, the port reached as Odoo will), and the two applications
talking through the link: the grant, the instance, the removal, the pages."""
import base64
import hashlib
import hmac
import json
import socket
import ssl
import struct
import threading
from pathlib import Path

import httpx
import pytest
import yaml

from omniacli import composehelper, linkcopy, linkedcluster, machinedb, stacktemplates, workspacehelper, workspaceops
from omniacli.app import create_app
from omniacli.auth import hash_password
from omniacli.config import Access, LinkTarget, SshRoute, Workspace, parse_settings
from omniacli.jobs import JobFailed

from .conftest import LINK_TOKEN, PASSWORD, USER_HASH, BasicAsSession, hash_token

TEMPLATES = Path(__file__).parent.parent / "templates"
HOST = "lab1.test"
ADMIN = {"Authorization": "Basic " + base64.b64encode(f"admin:{PASSWORD}".encode()).decode()}
POST = {**ADMIN, "Origin": f"http://{HOST}"}


# --- the holder's side: what root writes -----------------------------------------

def test_pg_hba_lets_one_address_into_one_database():
    text = machinedb.hba_text({"acme_shop": {}}, {"beta_erp": {"address": "203.0.113.7"},
                                                  "gone_erp": {"address": "203.0.113.8", "revoked": True}})
    lines = text.splitlines()
    assert 'hostssl "beta_erp",postgres "beta_erp" 203.0.113.7/32 scram-sha-256' in lines
    # a revoked one has no line; nothing for a network, nothing but hostssl, the reject last
    assert "gone_erp" not in text and "0.0.0.0" not in text and "/0 " not in text
    assert [line for line in lines if line.startswith("host ")] == ["host all all all reject"]
    assert lines.index('hostssl "beta_erp",postgres "beta_erp" 203.0.113.7/32 scram-sha-256') < lines.index("local all all reject")
    # an instance of this machine keeps its line as it was
    assert machinedb.hba_text({"acme_shop": {}}) == machinedb.hba_text({"acme_shop": {}}, {})


@pytest.mark.parametrize("bad", ["0.0.0.0", "10.0.0.0/8", "0.0.0.0/0", "127.0.0.1", "::1", "2001:db8::1", "224.0.0.1",
                                 "169.254.1.1", "db.example.com", "", None])
def test_only_one_address_of_another_machine_is_let_in(bad):
    with pytest.raises(machinedb.ClusterError):
        machinedb.address(bad)
    with pytest.raises(machinedb.ClusterError):
        machinedb.hba_text({}, {"beta_erp": {"address": bad}})
    with pytest.raises(machinedb.ClusterError):
        machinedb.firewall_rules({"omnidb17": [bad]})


def test_the_cluster_listens_only_when_asked():
    roots = ["/var/lib/omniacli-storage/default"]
    closed = machinedb.compose_text("17", 990, 990, roots)
    assert "network_mode: none" in closed and '"listen_addresses=",' in closed and "ports:" not in closed
    assert "ssl=on" not in closed and "networks:" not in closed
    listening = machinedb.compose_text("17", 990, 990, roots, 15432)
    project = yaml.safe_load(listening)
    service = project["services"]["db"]
    assert "network_mode" not in service and service["ports"] == ["0.0.0.0:15432:5432"] and service["networks"] == ["db"]
    assert project["networks"]["db"]["driver_opts"]["com.docker.network.bridge.name"] == "omnidb17"
    command = service["command"]
    for option in ("listen_addresses=*", "ssl=on", "ssl_cert_file=/etc/omniacli-db/server.crt",
                   "ssl_key_file=/etc/omniacli-db/server.key", "ssl_min_protocol_version=TLSv1.2"):
        assert option in command
    # the rest of the project is the same: closing the port writes the file of before again
    assert machinedb._roots_of(listening) == roots == machinedb._roots_of(closed)
    assert machinedb.compose_text("17", 990, 990, machinedb._roots_of(listening)) == closed
    assert yaml.safe_load(closed)["services"]["db"]["volumes"] == service["volumes"]


def test_the_port_is_none_a_system_postgresql_has(tmp_path, monkeypatch):
    conf = tmp_path / "16" / "main"
    conf.mkdir(parents=True)
    (conf / "postgresql.conf").write_text("# port = 9999\nport = 15432\t\t# (change requires restart)\n")
    monkeypatch.setattr(machinedb, "SYSTEM_POSTGRES", tmp_path)
    assert machinedb.system_ports(tmp_path) == {5432, 15432}
    assert machinedb.pick_port(taken={15433}, free=lambda port: port != 15434) == 15435
    assert 5432 not in machinedb.PORTS and 5433 not in machinedb.PORTS
    with pytest.raises(machinedb.ClusterError, match="no port free"):
        machinedb.pick_port(free=lambda port: False)


def test_the_firewall_lets_the_grants_in_and_nothing_out():
    rules = machinedb.firewall_rules({"omnidb17": ["203.0.113.7", "198.51.100.4"]})
    forward = rules["forward"]
    assert forward[:2] == [["-o", "omnidb17", "-s", "198.51.100.4/32", "-p", "tcp", "--dport", "5432", "-j", "RETURN"],
                           ["-o", "omnidb17", "-s", "203.0.113.7/32", "-p", "tcp", "--dport", "5432", "-j", "RETURN"]]
    # then everything else to the cluster is dropped, and from it only answers pass
    assert forward[2:] == [["-o", "omnidb17", "-j", "DROP"],
                           ["-i", "omnidb17", "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "RETURN"],
                           ["-i", "omnidb17", "-j", "DROP"]]
    assert rules["input"][-1] == ["-i", "omnidb17", "-j", "DROP"]
    # no grant: the port answers nobody
    assert machinedb.firewall_rules({"omnidb17": []})["forward"][0] == ["-o", "omnidb17", "-j", "DROP"]
    assert machinedb.firewall_rules({}) == {"forward": [], "input": []}
    with pytest.raises(machinedb.ClusterError, match="not the network of a cluster"):
        machinedb.firewall_rules({"eth0": ["203.0.113.7"]})


def test_the_firewall_file_keeps_only_what_is_right(tmp_path, monkeypatch):
    path = tmp_path / "db-listen.json"
    monkeypatch.setattr(workspacehelper, "DB_LISTEN", path)
    path.write_text('{"omnidb17": ["203.0.113.7", "203.0.113.7"], "omnidb16": ["0.0.0.0/0"], "eth0": ["203.0.113.9"]}')
    assert workspacehelper.db_listen_file() == {"omnidb17": ["203.0.113.7"]}
    path.write_text("not json")
    assert workspacehelper.db_listen_file() == {}


def test_the_grants_file_and_what_listens(tmp_path, monkeypatch):
    project = tmp_path / "pg17"
    project.mkdir()
    assert machinedb.grants(project) == {} and machinedb.listening(project) is None
    (project / "grants.json").write_text(json.dumps({"beta_erp": {"address": "203.0.113.7", "link": "lab1"},
                                                     "Bad Name": {"address": "203.0.113.8"}, "x": "y"}))
    assert list(machinedb.grants(project)) == ["beta_erp"]
    (project / "listen.json").write_text('{"port": 15432, "bridge": "omnidb17"}')
    assert machinedb.listening(project) == {"port": 15432, "bridge": "omnidb17"}
    registry = tmp_path / "machine-db.json"
    registry.write_text(json.dumps({"17": {"project": str(project), "storage": "default"}}))
    monkeypatch.setattr(machinedb, "REGISTRY", registry)
    assert machinedb.firewall_state() == {"omnidb17": ["203.0.113.7"]}
    # revoked: its address is no longer let in
    (project / "grants.json").write_text(json.dumps({"beta_erp": {"address": "203.0.113.7", "revoked": True}}))
    assert machinedb.firewall_state() == {"omnidb17": []}
    (project / "listen.json").unlink()
    assert machinedb.firewall_state() == {}


def test_a_grant_is_checked_before_anything_is_made(tmp_path, monkeypatch):
    project = tmp_path / "pg17"
    project.mkdir()
    (project / "compose.yaml").write_text("services: {}\n")
    (project / "environments.json").write_text(json.dumps({"acme_shop": {"workspace": "acme", "instance": "shop"}}))
    registry = tmp_path / "machine-db.json"
    registry.write_text(json.dumps({"17": {"project": str(project), "storage": "default"}}))
    monkeypatch.setattr(machinedb, "REGISTRY", registry)
    asked = []
    monkeypatch.setattr(machinedb, "sql", lambda *args: asked.append(args) or "0")
    monkeypatch.setattr(machinedb, "listen_on", lambda *args: pytest.fail("the cluster was touched"))
    good = {"name": "beta_erp", "address": "203.0.113.7", "link": "lab1", "by": "admin", "reached_as": "db.example.com",
            "verifier": linkedcluster.scram_verifier("a-password-of-the-other-machine")}
    for change, message in (({"name": "Bad Name"}, "not an environment"), ({"address": "0.0.0.0"}, "not the address"),
                            ({"address": "10.0.0.0/8"}, "not an address"), ({"verifier": "plain-password"}, "not of its form"),
                            ({"verifier": "md5" + "0" * 32}, "not of its form"), ({"link": ""}, "through its link"),
                            ({"name": "acme_shop"}, "an instance of this machine")):
        with pytest.raises(machinedb.ClusterError, match=message) as refused:
            machinedb.grant_add("17", {**good, **change})
        assert good["verifier"] not in str(refused.value)
    # the name of a grant is taken, another link's or the same one's: a grant is never written again
    # (a second instance of that name would take the first one's role and database)
    for link in ("other", "lab1"):
        (project / "grants.json").write_text(json.dumps({"beta_erp": {"address": "203.0.113.9", "link": link}}))
        with pytest.raises(machinedb.ClusterError, match="of another machine on this cluster already"):
            machinedb.grant_add("17", good)
    # and an instance of this machine does not take the name of a grant
    with pytest.raises(machinedb.ClusterError, match="database of another machine"):
        machinedb.env_add("17", "beta", tmp_path, "erp", "a-password-of-this-machine")
    # nothing reached SQL but the checks; a cluster with grants is not taken away
    (project / "environments.json").write_text("{}")
    assert not any("CREATE" in str(call) or "ALTER" in str(call) for call in asked)
    with pytest.raises(machinedb.ClusterError, match="databases of other machines"):
        machinedb.remove("17")


# --- its CA and certificate, and the port reached as Odoo will ------------------------

def _postgres(names, answer):
    """A TLS server that speaks the first messages of PostgreSQL's protocol, with a
    certificate of a new CA made for names: (the CA, its port, its thread)."""
    ca_key, ca = machinedb.make_ca("17", "lab2")
    key, cert = machinedb.make_certificate(ca_key, ca, names)
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]

    def serve():
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "crt").write_text(cert)
            (Path(folder) / "key").write_text(key)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(Path(folder) / "crt", Path(folder) / "key")
        try:
            conn, _ = listener.accept()
            with conn:
                assert conn.recv(8) == struct.pack("!II", 8, 80877103)
                conn.sendall(b"S")
                try:
                    tls = context.wrap_socket(conn, server_side=True)
                except (ssl.SSLError, OSError):
                    return
                length = struct.unpack("!I", tls.recv(4))[0]
                startup = tls.recv(length - 4)
                assert b"user\0beta_erp\0database\0beta_erp\0" in startup and b"password" not in startup
                tls.sendall(answer)
                tls.close()
        finally:
            listener.close()
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return ca, port, thread


SASL = b"R" + struct.pack("!II", 23, 10) + b"SCRAM-SHA-256\0\0"


def _refusal(text):
    body = b"SFATAL\0C28000\0M" + text.encode() + b"\0\0"
    return b"E" + struct.pack("!I", len(body) + 4) + body


def test_the_port_is_checked_as_odoo_reaches_it():
    ca, port, thread = _postgres(["127.0.0.1", "db.example.com"], SASL)
    found = linkedcluster.check("127.0.0.1", port, ca, "beta_erp", "beta_erp", "203.0.113.7")
    thread.join(5)
    assert found["tls"].startswith("TLSv1")
    assert machinedb.certificate_names(machinedb.make_certificate(*machinedb.make_ca("17", "x"), ["a.example", "10.1.2.3"])[1]) \
        == ["a.example", "10.1.2.3"]


def test_another_ca_or_another_name_is_refused():
    # the certificate of another CA: verify-full fails on the CA
    _, port, thread = _postgres(["127.0.0.1"], SASL)
    other = machinedb.make_ca("17", "elsewhere")[1]
    with pytest.raises(linkedcluster.CheckFailed, match="not the one of its CA"):
        linkedcluster.check("127.0.0.1", port, other, "beta_erp", "beta_erp")
    thread.join(5)
    # the right CA, a certificate made for another name: verify-full fails on the name
    ca, port, thread = _postgres(["db.example.com"], SASL)
    with pytest.raises(linkedcluster.CheckFailed, match="not made for 127.0.0.1"):
        linkedcluster.check("127.0.0.1", port, ca, "beta_erp", "beta_erp")
    thread.join(5)
    with pytest.raises(linkedcluster.CheckFailed, match="not a certificate"):
        linkedcluster.check("127.0.0.1", port, "not a pem", "beta_erp", "beta_erp")


def test_the_address_the_database_saw_is_said():
    said = 'pg_hba.conf rejects connection for host "198.51.100.9", user "beta_erp", database "beta_erp", SSL encryption'
    ca, port, thread = _postgres(["127.0.0.1"], _refusal(said))
    with pytest.raises(linkedcluster.CheckFailed, match="come from 198.51.100.9, not from 203.0.113.7"):
        linkedcluster.check("127.0.0.1", port, ca, "beta_erp", "beta_erp", "203.0.113.7")
    thread.join(5)
    # the address is the one given: it is the role or the database that has no line
    ca, port, thread = _postgres(["127.0.0.1"], _refusal(said))
    with pytest.raises(linkedcluster.CheckFailed, match="refused beta_erp on beta_erp"):
        linkedcluster.check("127.0.0.1", port, ca, "beta_erp", "beta_erp", "198.51.100.9")
    thread.join(5)
    # a port nothing answers on
    closed = socket.create_server(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()
    with pytest.raises(linkedcluster.CheckFailed, match="cannot be reached"):
        linkedcluster.check("127.0.0.1", port, ca, "beta_erp", "beta_erp")


def test_the_round_trip_is_measured_on_a_port_that_answers():
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    found = linkedcluster.measure("127.0.0.1", [port], tries=3)
    assert found["port"] == port and found["tries"] == 3 and 0 < found["ms"] < 1000 and found["address"] == "127.0.0.1"
    listener.close()
    # a refusal is an answer too: one packet there, one back
    assert linkedcluster.measure("127.0.0.1", [port], tries=2)["ms"] is not None
    assert linkedcluster.page_seconds(1.0) == 0.5 and linkedcluster.page_seconds(0.2) == 0.1


def test_the_verifier_is_postgresqls_and_holds_no_password():
    """RFC 7677's example (user, pencil): the server's signature made from the
    verifier's ServerKey is the one of the RFC."""
    salt = base64.b64decode("W22ZaJ0SNY7soEsUEjb6gQ==")
    verifier = linkedcluster.scram_verifier("pencil", salt)
    assert machinedb.VERIFIER.fullmatch(verifier) and "pencil" not in verifier
    head, keys = verifier.rsplit("$", 1)
    assert head == "SCRAM-SHA-256$4096:W22ZaJ0SNY7soEsUEjb6gQ=="
    stored, server = (base64.b64decode(k) for k in keys.split(":"))
    message = ("n=user,r=rOprNGfwEbeRWgbNEkqO,r=rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0,"
               "s=W22ZaJ0SNY7soEsUEjb6gQ==,i=4096,c=biws,r=rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0").encode()
    assert base64.b64encode(hmac.new(server, message, hashlib.sha256).digest()).decode() \
        == "6rriTRBi23WpRR/wtup+mMhUZUn/dB5nLTJRsjl95G4="
    # the client's proof of the RFC opens the stored key: the password behind the verifier is pencil
    proof = base64.b64decode("dHzbZapWIk4jUhN+Ute9ytag9zjfMHgsqmmiz7AndVQ=")
    signature = hmac.new(stored, message, hashlib.sha256).digest()
    client_key = bytes(a ^ b for a, b in zip(proof, signature))
    assert hashlib.sha256(client_key).digest() == stored
    # another salt every time
    assert linkedcluster.scram_verifier("pencil") != linkedcluster.scram_verifier("pencil")


def test_a_plain_http_link_carries_no_database():
    plain = LinkTarget("plain", "http://lab2.example.com:8090", "token")
    assert "plain HTTP" in linkedcluster.unprotected(plain)
    https = LinkTarget("lab2", "https://lab2.example.com", "token")
    assert linkedcluster.unprotected(https) is None and linkedcluster.host_of(https) == "lab2.example.com"
    assert linkedcluster.link_port(https) == 443
    # through SSH: the tunnel protects it, and the machine is its SSH server, not the 127.0.0.1 of the URL
    tunnel = LinkTarget("lab2", "http://127.0.0.1:8090", "token", ssh=SshRoute("203.0.113.12", "omniacli-link", "ssh-ed25519 AAAA", 2222))
    assert linkedcluster.unprotected(tunnel) is None and linkedcluster.host_of(tunnel) == "203.0.113.12"
    assert linkedcluster.link_port(tunnel) == 2222


def test_the_connection_names_its_link():
    ca = machinedb.make_ca("17", "lab2")[1]
    spec = workspacehelper.remote_spec({"host": "203.0.113.12", "port": 15432, "user": "9lives_erp", "password": "x" * 32,
                                        "database": "9lives_erp", "ca": ca, "link": "lab2", "major": "17"})
    assert (spec["link"], spec["major"], spec["user"]) == ("lab2", "17", "9lives_erp")
    # checked by the app, sent as JSON, checked again by the helper: the link is still there
    assert workspacehelper.remote_spec(json.dumps(spec))["link"] == "lab2"
    for change, message in (({"link": "bad link"}, "not what they should be"), ({"major": "x"}, "not what they should be"),
                            ({"replica_host": "db-ro.example.com"}, "no read replica")):
        with pytest.raises(workspacehelper.HelperError, match=message):
            workspacehelper.remote_spec({**spec, **change})


def test_a_backup_is_not_loaded_over_it(tmp_path):
    """Odoo drops the database and makes it again: there its role makes none."""
    project = tmp_path / "erp" / "production"
    project.mkdir(parents=True)
    (project / ".env").write_text("PROJECT=acme-erp-production\nDB_REMOTE=1\nDB_HOST=203.0.113.12\nDB_PORT=15432\n")
    (tmp_path / "erp" / "instance.json").write_text('{"database": "acme_erp", "database_on": "203.0.113.12:15432"}')
    assert not composehelper.on_linked(project)  # a third party's: its role is given CREATEDB by who runs it
    (tmp_path / "erp" / "instance.json").write_text('{"database": "acme_erp", "database_link": "lab2", "database_major": "17"}')
    assert composehelper.on_linked(project)
    # the plugin refuses the restore and the check before it stops or touches anything
    import asyncio
    import types

    from omniacli import docker as docker_plugin
    instance = types.SimpleNamespace(key="acme-erp/production", options=types.SimpleNamespace(db_service="remote"),
                                     restart=types.SimpleNamespace(service=str(project)))
    assert docker_plugin.on_linked_cluster(instance)
    plugin = docker_plugin.DockerPlugin.__new__(docker_plugin.DockerPlugin)
    for refused in (plugin.restore(None, instance, tmp_path / "b.zip", {}), plugin.verify_backup(None, instance, tmp_path / "b.zip")):
        with pytest.raises(JobFailed, match="its role makes no database"):
            asyncio.run(refused)
    instance.options.db_service = "db"
    assert not docker_plugin.on_linked_cluster(instance)
    (project / ".env").write_text("PROJECT=acme-erp-production\n")
    assert not composehelper.on_linked(project)


# --- the two applications through the link ----------------------------------------

class Network(httpx.AsyncBaseTransport):
    """lab2.test is the holder's application; with down, it does not answer."""

    def __init__(self, app):
        self.asgi = httpx.ASGITransport(app=app)
        self.down = False
        self.requests = []

    async def handle_async_request(self, request):
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        self.requests.append(request)
        return await self.asgi.handle_async_request(request)


class Cluster:
    """What the root helper of the holder would do, kept in memory."""

    def __init__(self):
        self.grants, self.port, self.calls, self.inputs = {}, None, [], []
        self.ca = machinedb.make_ca("17", "lab2")[1]

    def state(self):
        return {"clusters": [{"major": "17", "storage": "default", "project": "/storage/.machine-db/pg17",
                              "running": True, "promised": 64, "port": self.port, "grants": dict(self.grants),
                              "environments": {"acme_mach": {"workspace": "acme", "instance": "mach"},
                                               "beta_shop": {"workspace": "beta", "instance": "shop"}}}],
                "majors": ["17"]}

    async def ask(self, *args, timeout=None):
        assert args == ("machine-db-state",)
        return self.state()

    async def helper(self, job, *args, timeout=None, input=None):
        self.calls.append(args)
        job.log("$ sudo -n omniacli workspace-helper " + " ".join(args))
        if args[0] == "machine-db-grant":
            spec = json.loads(input)
            self.inputs.append(spec)
            if spec["name"] in self.grants:
                raise JobFailed(f"workspace-helper: {spec['name']} is a database of another machine on this cluster already")
            first, self.port = self.port is None, 15432
            self.grants[spec["name"]] = {k: spec[k] for k in ("link", "by", "address", "workspace", "instance")}
            self.grants[spec["name"]].update(at="2026-11-22T10:00:00", revoked=False)
            return {"role": spec["name"], "database": spec["name"], "port": 15432, "address": spec["address"],
                    "created": True, "restarted_s": 0.5 if first else None, "serves": 2, "names": [spec["reached_as"]],
                    "ca": self.ca, "version": "17.12", "firewall": [spec["address"]]}
        if args[0] == "machine-db-ungrant":
            _, major, role, what = args
            grant = self.grants[role]
            if what == "drop":
                del self.grants[role]
            else:
                grant["revoked"] = what == "revoke"
            allowed = sorted({g["address"] for g in self.grants.values() if not g["revoked"]})
            return {"role": role, "address": grant["address"], "closed": 3, "dropped": [role], "port": self.port,
                    "grants_left": len(self.grants), "firewall": allowed}
        if args[0] == "machine-db-port":
            closed, self.port = self.port, None
            return {"closed": closed, "restarted_s": 0.4}
        raise AssertionError(args)


@pytest.fixture
async def machines(tmp_path, monkeypatch):
    """lab2 holds the machine's cluster and lets lab1 in as an admin's link; lab1 has
    the workspace acme and reaches lab2 through HTTPS (and, as plain, through HTTP)."""
    cluster = Cluster()
    registry = tmp_path / "machine-db.json"
    registry.write_text(json.dumps({"17": {"project": "/storage/.machine-db/pg17", "storage": "default"}}))
    monkeypatch.setattr(machinedb, "REGISTRY", registry)
    monkeypatch.setattr(workspaceops, "ask", cluster.ask)
    monkeypatch.setattr(workspaceops, "_helper", cluster.helper)
    monkeypatch.setattr(linkcopy, "POLL_EVERY", 0.01)
    checked = []
    monkeypatch.setattr(linkedcluster, "check", lambda *args: checked.append(args) or {"tls": "TLSv1.3"})
    monkeypatch.setattr(linkedcluster, "measure", lambda host, ports, *a: {
        "ms": 0.4, "port": ports[0], "tries": 7, "address": "203.0.113.7"})

    def settings_of(name, auth):
        conf = tmp_path / name
        conf.mkdir()
        if name == "lab1":
            (conf / "links.yaml").write_text(yaml.safe_dump({"links": {
                "lab2": {"url": "https://lab2.test", "token": LINK_TOKEN},
                "plain": {"url": "http://lab2.test", "token": LINK_TOKEN}}}))
        return parse_settings({"auth": auth, "backup_dir": str(conf / "backups"), "jobs_dir": str(conf / "jobs"),
                               "servers_file": "servers.yaml"}, base_dir=conf)
    holder = settings_of("lab2", {"users": {"admin": USER_HASH}, "links": {
        "lab1": {"token": hash_token(LINK_TOKEN), "role": "admin"},
        "reader": {"token": hash_token("a-developer-link"), "role": "developer"}}})
    holder.storage_roots = {"default": str(tmp_path / "lab2-storage")}
    lab2 = create_app(holder)
    asker = settings_of("lab1", {"users": {"admin": USER_HASH}})
    password = hash_password(PASSWORD, iterations=1000)
    asker.set_web_access(Access(users={"mario": password}, roles={"mario": "workspace"}))
    asker.storage_roots = {"default": str(tmp_path / "storage")}
    asker.workspaces = {"acme": Workspace("acme", "ACME", storage="default",
                                          limits={"disk_gb": 10}, members={"mario": "admin"})}
    network = Network(lab2)
    lab1 = create_app(asker, link_transport=network)
    template = stacktemplates.load_template(TEMPLATES / "odoo-19-remote")
    monkeypatch.setattr(workspaceops, "templates", lambda: {template.name: template})
    made, removed = [], []

    async def create_instance(job, ws, instance, tpl, values, setup=None):
        remote = values[workspacehelper.REMOTE_KEY]
        made.append(remote)
        if instance == "broken":
            raise JobFailed("the helper failed")
        folder = tmp_path / "storage" / ws.name / instance
        (folder / "production").mkdir(parents=True)
        (folder / "instance.json").write_text(json.dumps({
            "database": remote["database"], "database_on": f"{remote['host']}:{remote['port']}",
            "database_link": remote["link"], "database_major": remote["major"]}))
        return {"type": "docker", "production": {
            "database": remote["database"], "odoo_service": "odoo", "db_service": "remote", "http_port": 20000,
            "update_modules": True, "restart": {"type": "compose", "service": str(folder / "production"), "sudo": True},
            "repositories": []}}

    async def remove_instance(job, ws, instance, linked=False):
        removed.append((instance, linked))

    async def publish(job, ws):
        return {}
    monkeypatch.setattr(workspaceops, "create_instance", create_instance)
    monkeypatch.setattr(workspaceops, "remove_instance", remove_instance)
    monkeypatch.setattr(workspaceops, "publish", publish)
    transport = httpx.ASGITransport(app=BasicAsSession(lab1))
    async with httpx.AsyncClient(transport=transport, base_url=f"http://{HOST}") as client:
        client.lab1, client.lab2, client.network, client.cluster = lab1, lab2, network, cluster
        client.made, client.removed, client.checked = made, removed, checked
        client.there = httpx.AsyncClient(transport=httpx.ASGITransport(app=BasicAsSession(lab2)), base_url="http://lab2.test")
        yield client
        await client.there.aclose()


async def job_of(client, job_id, headers=ADMIN):
    import asyncio
    for _ in range(400):
        data = (await client.get(f"/api/jobs/{job_id}", headers=headers)).json()
        if data["status"] in ("success", "failed", "interrupted", "stopped"):
            return data
        await asyncio.sleep(0.02)
    raise AssertionError("job did not finish")


LINKED = {"link": "lab2", "major": "17", "address": "203.0.113.7"}


async def create(client, instance, linked=LINKED, headers=POST):
    return await client.post("/api/workspaces/acme/instances/create", headers=headers,
                             json={"instance": instance, "template": "odoo-19-remote", "linked": linked})


async def test_the_dialog_reads_the_offer_and_the_round_trip(machines):
    found = (await machines.get("/api/links/lab2/postgres", headers=ADMIN)).json()
    assert found["error"] is None and found["host"] == "lab2.test" and found["address"] == "203.0.113.7"
    assert found["clusters"] == [{"major": "17", "running": True, "port": None, "serves": 2, "grants": 0}]
    assert (found["ms"], found["tries"], found["warn_ms"]) == (0.4, 7, linkedcluster.WARN_MS)
    # measured on the link's own port while the database's is not open: the one known to answer
    assert found["measured_on"] == 443
    # a plain HTTP link is not asked: what it would answer could be anyone's
    before = len(machines.network.requests)
    plain = (await machines.get("/api/links/plain/postgres", headers=ADMIN)).json()
    assert "plain HTTP" in plain["error"] and plain["clusters"] == [] and len(machines.network.requests) == before
    # the machine's admins only: a workspace's admin learns nothing of the links
    mario = {"Authorization": "Basic " + base64.b64encode(f"mario:{PASSWORD}".encode()).decode()}
    assert (await machines.get("/api/links/lab2/postgres", headers=mario)).status_code == 403
    page = (await machines.get("/workspaces/acme", headers=ADMIN)).text
    assert 'id="rd-where"' in page and '<option value="lab2">' in page
    page = (await machines.get("/workspaces/acme", headers=mario)).text
    assert 'id="rd-where"' not in page and "lab2" not in page


async def test_an_instance_on_the_cluster_of_a_link(machines):
    resp = await create(machines, "erp")
    assert resp.status_code == 200, resp.text
    job = await job_of(machines, resp.json()["job"])
    assert job["status"] == "success", job
    # the holder was asked with the verifier of a password it never sees, by the link and its user
    spec = machines.cluster.inputs[0]
    assert (spec["name"], spec["address"], spec["link"], spec["by"], spec["reached_as"]) \
        == ("acme_erp", "203.0.113.7", "lab1", "admin", "lab2.test")
    assert machinedb.VERIFIER.fullmatch(spec["verifier"]) and "password" not in spec
    remote = machines.made[0]
    assert (remote["host"], remote["port"], remote["user"], remote["database"], remote["link"], remote["major"]) \
        == ("lab2.test", 15432, "acme_erp", "acme_erp", "lab2", "17")
    assert remote["ca"] == machines.cluster.ca
    # the verifier is of that password, which is in no line of either job and in no request's log
    salt = base64.b64decode(spec["verifier"].split("$")[1].split(":")[1])
    assert linkedcluster.scram_verifier(remote["password"], salt) == spec["verifier"]
    there = (await machines.there.get("/api/jobs", headers=ADMIN)).json()["jobs"]
    lines = job["lines"] + [line for j in there for line in
                            (await machines.there.get(f"/api/jobs/{j['id']}", headers=ADMIN)).json()["lines"]]
    assert lines and not any(remote["password"] in line or spec["verifier"] in line for line in lines)
    assert any("PostgreSQL 17 of lab2 answers on lab2.test:15432 over TLSv1.3" in line for line in job["lines"])
    assert any("its container was made again in 0.5 s" in line for line in lines)
    # the port was reached as Odoo will, for that role and database, from the address given
    assert machines.checked[0][0:2] == ("lab2.test", 15432) and machines.checked[0][3:] == ("acme_erp", "acme_erp", "203.0.113.7")
    # the holder's access log names the link and who asked there
    log = [json.loads(line) for line in (machines.lab2.state.settings.logs_dir / "access.jsonl").read_text().splitlines()]
    grant = next(r for r in log if r.get("route") == "/api/postgres/machine/{major}/grants" and r["event"] == "action")
    assert (grant["kind"], grant["name"], grant["on_behalf"]) == ("link", "lab1", "admin")
    assert "name=acme_erp host=203.0.113.7" in grant["detail"] and spec["verifier"] not in json.dumps(log)
    # the pages: the holder lists it, the asker names the link to the machine's admins only
    row = (await machines.there.get("/api/postgres", headers=ADMIN)).json()["rows"][0]
    assert row["port"] == 15432 and row["grants"] == [{
        "name": "acme_erp", "link": "lab1", "address": "203.0.113.7", "by": "admin", "at": "2026-11-22T10:00:00",
        "revoked": False, "instance": "acme-erp"}]
    here = (await machines.get("/api/postgres", headers=ADMIN)).json()["remote"]
    assert here == [{"key": "acme-erp/production", "server": "acme-erp", "role": "production",
                     "host": "lab2.test:15432", "link": "lab2", "major": "17"}]
    data = (await machines.get("/api/map", headers=ADMIN)).json()
    assert [d["label"] for d in data["databases"]] == ["PostgreSQL 17 of lab2"]
    mario = {"Authorization": "Basic " + base64.b64encode(f"mario:{PASSWORD}".encode()).decode()}
    data = (await machines.get("/api/map", headers=mario)).json()
    assert [d["label"] for d in data["databases"]] == [None] and "lab2\"" not in json.dumps(data)
    # the job of the instance, as a user of its workspace reads it: no link, no machine, nobody of the provider
    read = (await machines.get(f"/api/jobs/{job['id']}", headers=mario)).json()
    told = json.dumps([read["title"], read["source"], read["lines"]])
    assert read["lines"] and "lab2" not in told and "lab1" not in told and "admin" not in told
    assert any("another machine" in line for line in read["lines"])
    # a second one: the cluster listens already, nothing restarts
    assert (await job_of(machines, (await create(machines, "crm")).json()["job"]))["status"] == "success"
    assert sorted(machines.cluster.grants) == ["acme_crm", "acme_erp"]


async def test_what_is_refused_before_the_job(machines):
    for linked, message in (({**LINKED, "link": "nope"}, "No link"), ({**LINKED, "link": "plain"}, "plain HTTP"),
                            ({**LINKED, "address": "0.0.0.0"}, "not the address of another machine"),
                            ({**LINKED, "address": "10.0.0.0/8"}, "is not an address"),
                            ({**LINKED, "major": "seventeen"}, "is not a PostgreSQL"), ("lab2", "linked:")):
        resp = await create(machines, "erp", linked)
        assert resp.status_code in (400, 404) and message in resp.json()["error"], linked
    # a template with a database of its own takes no link
    assert machines.cluster.calls == [] and machines.made == []
    # a workspace's admin makes no instance on a database of another machine
    mario = {"Authorization": "Basic " + base64.b64encode(f"mario:{PASSWORD}".encode()).decode(), "Origin": f"http://{HOST}"}
    assert (await create(machines, "erp", headers=mario)).status_code == 403


async def test_an_instance_not_made_gives_its_database_back(machines, monkeypatch):
    job = await job_of(machines, (await create(machines, "broken")).json()["job"])
    assert job["status"] == "failed"
    assert [call[0] for call in machines.cluster.calls] == ["machine-db-grant", "machine-db-ungrant"]
    assert machines.cluster.calls[1] == ("machine-db-ungrant", "17", "acme_broken", "drop") and machines.cluster.grants == {}
    assert any("database acme_broken and its role dropped" in line for line in job["lines"])
    # the port that does not let this machine in: the database goes back, the job says the address seen
    def refuse(*args):
        raise linkedcluster.CheckFailed("lab2.test:15432 sees this machine come from 198.51.100.9, not from 203.0.113.7")
    monkeypatch.setattr(linkedcluster, "check", refuse)
    job = await job_of(machines, (await create(machines, "nat")).json()["job"])
    assert job["status"] == "failed" and "198.51.100.9" in job["error"] and machines.cluster.grants == {}
    assert machines.made == [machines.made[0]] and "acme-nat" not in machines.lab1.state.settings.workspaces["acme"].servers


async def test_a_database_of_that_name_is_never_taken_nor_dropped(machines):
    """A grant of that name is there already (what a removal left while the holder did not
    answer, or another instance whose names make the same one): the holder refuses, and the
    instance not made drops nothing there: it is not this job's."""
    left = {"link": "lab1", "by": "admin", "address": "203.0.113.7", "workspace": "acme", "instance": "erp",
            "at": "2026-11-20T09:00:00", "revoked": False}
    machines.cluster.grants["acme_erp"] = dict(left)
    job = await job_of(machines, (await create(machines, "erp")).json()["job"])
    assert job["status"] == "failed" and "already" in job["error"]
    assert [call[0] for call in machines.cluster.calls] == ["machine-db-grant"] and machines.cluster.grants == {"acme_erp": left}
    assert machines.made == [] and "acme-erp" not in machines.lab1.state.settings.servers
    # a link takes back what it asked for, never the database of the same name of another machine's
    machines.cluster.grants["acme_erp"]["link"] = "another-machine"
    link = {"Authorization": f"Bearer {LINK_TOKEN}", "X-OmniaCLI-User": "admin", "X-OmniaCLI-Role-Cap": "admin"}
    for what in ("drop", "revoke", "allow"):
        resp = await machines.there.post(f"/api/postgres/machine/17/grants/acme_erp/{what}", headers=link)
        assert resp.status_code == 403 and "not of lab1" in resp.json()["error"], what
    assert [call[0] for call in machines.cluster.calls] == ["machine-db-grant"] and "acme_erp" in machines.cluster.grants
    # the holder's own admin may: it is its cluster
    resp = await machines.there.post("/api/postgres/machine/17/grants/acme_erp/drop",
                                     headers={**ADMIN, "Origin": "http://lab2.test"})
    assert (await job_of(machines.there, resp.json()["job"]))["status"] == "success" and machines.cluster.grants == {}


async def test_removing_the_instance_asks_the_holder(machines):
    assert (await job_of(machines, (await create(machines, "erp")).json()["job"]))["status"] == "success"
    assert (await job_of(machines, (await create(machines, "crm")).json()["job"]))["status"] == "success"
    resp = await machines.post("/api/workspaces/acme/instances/delete", headers=POST, json={"server": "acme-erp"})
    job = await job_of(machines, resp.json()["job"])
    assert job["status"] == "success" and machines.removed == [("erp", True)]
    assert machines.cluster.calls[-1] == ("machine-db-ungrant", "17", "acme_erp", "drop")
    assert sorted(machines.cluster.grants) == ["acme_crm"]
    # the other database of this machine still uses the firewall rule: said
    assert any("still lets 203.0.113.7 in, for other databases" in line for line in job["lines"])
    # the holder does not answer: the removal ends all the same, and says what stays there
    machines.network.down = True
    resp = await machines.post("/api/workspaces/acme/instances/delete", headers=POST, json={"server": "acme-crm"})
    job = await job_of(machines, resp.json()["job"])
    assert job["status"] == "success" and machines.removed[-1] == ("crm", True)
    warning = job["result"]["warnings"][0]
    assert "lab2 did not take its part back" in warning and "database acme_crm and role acme_crm of its PostgreSQL 17" in warning
    assert "acme-crm" not in machines.lab1.state.settings.servers and sorted(machines.cluster.grants) == ["acme_crm"]
    # the holder's admin drops what stayed, in its Database tab; asked again from the other machine, it is gone
    machines.network.down = False
    there = {**ADMIN, "Origin": "http://lab2.test"}
    resp = await machines.there.post("/api/postgres/machine/17/grants/acme_crm/drop", headers=there)
    assert (await job_of(machines.there, resp.json()["job"]))["status"] == "success" and machines.cluster.grants == {}
    assert (await machines.there.post("/api/postgres/machine/17/grants/acme_crm/drop", headers=there)).json() \
        == {"ok": True, "gone": True}


async def test_a_link_renamed_since_is_still_the_one(machines):
    """The instance's record keeps the link's name of the day it was made: a
    link renamed since is found by where it leads, on the pages and at the removal."""
    import dataclasses
    assert (await job_of(machines, (await create(machines, "erp")).json()["job"]))["status"] == "success"
    settings = machines.lab1.state.settings
    target = settings.link_targets["lab2"]
    settings.link_targets = {"plain": settings.link_targets["plain"], "datacenter": dataclasses.replace(target, name="datacenter")}
    here = (await machines.get("/api/postgres", headers=ADMIN)).json()["remote"]
    assert here[0]["link"] == "datacenter"
    resp = await machines.post("/api/workspaces/acme/instances/delete", headers=POST, json={"server": "acme-erp"})
    job = await job_of(machines, resp.json()["job"])
    assert job["status"] == "success" and machines.cluster.grants == {}
    assert any("PostgreSQL 17 of datacenter: database acme_erp and its role dropped" in line for line in job["lines"])
    # no link leads there any more: the pages name the host alone, the removal says what stays
    assert (await job_of(machines, (await create(machines, "crm", {**LINKED, "link": "datacenter"})).json()["job"]))["status"] == "success"
    settings.link_targets = {}
    assert (await machines.get("/api/postgres", headers=ADMIN)).json()["remote"][0]["link"] is None
    resp = await machines.post("/api/workspaces/acme/instances/delete", headers=POST, json={"server": "acme-crm"})
    job = await job_of(machines, resp.json()["job"])
    assert job["status"] == "success" and "The link datacenter is no longer here" in job["result"]["warnings"][0]


async def test_the_holder_revokes_and_allows_again(machines):
    assert (await job_of(machines, (await create(machines, "erp")).json()["job"]))["status"] == "success"
    there = {**ADMIN, "Origin": "http://lab2.test"}
    resp = await machines.there.post("/api/postgres/machine/17/grants/acme_erp/revoke", headers=there)
    job = await job_of(machines.there, resp.json()["job"])
    assert job["status"] == "success" and machines.cluster.grants["acme_erp"]["revoked"] is True
    assert any("Database acme_erp is no longer reached from 203.0.113.7" in line for line in job["lines"])
    assert job["title"] == "Revoke database acme_erp of lab1 on the machine's PostgreSQL 17"
    assert (await machines.there.get("/api/postgres", headers=ADMIN)).json()["rows"][0]["grants"][0]["revoked"] is True
    resp = await machines.there.post("/api/postgres/machine/17/grants/acme_erp/allow", headers=there)
    assert (await job_of(machines.there, resp.json()["job"]))["status"] == "success"
    assert machines.cluster.grants["acme_erp"]["revoked"] is False
    # what is not a grant, not an action, not a cluster
    for path, code in (("/api/postgres/machine/17/grants/nope/revoke", 404), ("/api/postgres/machine/17/grants/acme_erp/explode", 404),
                       ("/api/postgres/machine/16/grants/acme_erp/revoke", 404)):
        assert (await machines.there.post(path, headers=there)).status_code == code
    # the port is closed by the holder's admin, a job
    resp = await machines.there.post("/api/postgres/machine/17/port/close", headers=there)
    assert (await job_of(machines.there, resp.json()["job"]))["status"] == "success" and machines.cluster.port is None


async def test_a_grant_is_asked_through_an_admins_link_only(machines):
    body = {"name": "acme_erp", "host": "203.0.113.7", "reached_as": "lab2.test",
            "verifier": linkedcluster.scram_verifier("a-password-of-the-other-machine")}
    # a user of the holder, even its admin: the link's name is what is recorded
    resp = await machines.there.post("/api/postgres/machine/17/grants", headers={**ADMIN, "Origin": "http://lab2.test"}, json=body)
    assert resp.status_code == 403 and "through its link" in resp.json()["error"]
    # a link that is no admin there, a wrong token
    link = {"Authorization": "Bearer a-developer-link", "X-OmniaCLI-User": "admin", "X-OmniaCLI-Role-Cap": "admin"}
    assert (await machines.there.post("/api/postgres/machine/17/grants", headers=link, json=body)).status_code == 403
    assert (await machines.there.get("/api/postgres/machine/offer", headers=link)).status_code == 403
    assert (await machines.there.post("/api/postgres/machine/17/grants", headers={"Authorization": "Bearer nope"},
                                      json=body)).status_code == 401
    # an admin's link whose user there is less than an admin
    capped = {"Authorization": f"Bearer {LINK_TOKEN}", "X-OmniaCLI-User": "dev", "X-OmniaCLI-Role-Cap": "developer"}
    assert (await machines.there.post("/api/postgres/machine/17/grants", headers=capped, json=body)).status_code == 403
    good = {"Authorization": f"Bearer {LINK_TOKEN}", "X-OmniaCLI-User": "admin", "X-OmniaCLI-Role-Cap": "admin"}
    for change, message in (({"host": "0.0.0.0"}, "not the address"), ({"host": "203.0.113.0/24"}, "not an address"),
                            ({"verifier": "the-password-itself"}, "SCRAM verifier"), ({"name": "Bad Name"}, "SCRAM verifier"),
                            ({"reached_as": "localhost"}, "not a name or an address")):
        resp = await machines.there.post("/api/postgres/machine/17/grants", headers=good, json={**body, **change})
        assert resp.status_code == 400 and message in resp.json()["error"], change
    assert (await machines.there.post("/api/postgres/machine/16/grants", headers=good, json=body)).status_code == 404
    assert machines.cluster.calls == []
