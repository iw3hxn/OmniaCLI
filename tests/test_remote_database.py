"""A database of another machine (AD-003): the templates' rules, the
connection checked at the creation, the helper's database actions from the
Odoo service, the one address the workspaces' firewall lets through."""
from pathlib import Path

import pytest

from omniacli import composehelper, stacktemplates, workspacehelper
from omniacli.composehelper import HelperError

TEMPLATES = Path(__file__).parent.parent / "templates"
CA = "-----BEGIN CERTIFICATE-----\nMIIBszCCAVmgAwIBAgIUQ0\nabcd\n-----END CERTIFICATE-----"
GOOD = {"host": "db.example.com", "port": 5432, "user": "acme_shop", "password": "Sup3r_secret-pw",
        "database": "acme_shop", "ca": CA}


@pytest.mark.parametrize("name", ["odoo-18-remote", "odoo-19-remote", "odooplm-20-remote"])
def test_the_remote_templates_pass_the_rules(name):
    template = stacktemplates.load_template(TEMPLATES / name)
    assert template.on_remote and not template.own_database and template.db_service == stacktemplates.REMOTE_DB_SERVICE
    assert stacktemplates.check_compose(template) == []
    assert "DB_SOCKET" not in template.compose and "db_sslmode = verify-full" in template.conf


def test_a_remote_template_is_verified_tls_or_nothing(tmp_path):
    import shutil
    folder = tmp_path / "odoo-19-remote"
    shutil.copytree(TEMPLATES / "odoo-19-remote", folder)
    compose = folder / "compose.yaml"
    compose.write_text(compose.read_text().replace("PGSSLMODE: verify-full", "PGSSLMODE: require"))
    with pytest.raises(stacktemplates.TemplateError, match="PGSSLMODE: verify-full is required"):
        stacktemplates.load_template(folder)
    compose.write_text(compose.read_text().replace("PGSSLMODE: require", "PGSSLMODE: verify-full"))
    conf = folder / "odoo.conf.in"
    conf.write_text(conf.read_text().replace("db_sslmode = verify-full", "db_sslmode = prefer"))
    with pytest.raises(stacktemplates.TemplateError, match="db_sslmode = verify-full is required"):
        stacktemplates.load_template(folder)


def test_the_connection_is_checked():
    spec = workspacehelper.remote_spec(dict(GOOD))
    assert spec["host"] == "db.example.com" and spec["port"] == 5432 and spec["ca"].endswith("-----END CERTIFICATE-----\n")
    for change, message in (({"host": "127.0.0.1"}, "not on this one"), ({"host": "localhost"}, "not on this one"),
                            ({"host": "bad host"}, "not a host name"), ({"port": "0"}, "not a port"),
                            ({"user": "Bad-Name"}, "not the name of a role"), ({"user": "postgres"}, "superuser"),
                            ({"database": "a b"}, "not the name of a database"),
                            ({"password": "has space in it"}, "the password of the role"),
                            ({"password": "quote'in$it"}, "the password of the role"),
                            ({"ca": ""}, "the CA of the database"), ({"ca": "not a certificate"}, "the CA of the database"),
                            ({"ca": CA.replace("abcd", "ab cd; rm -rf /")}, "the CA of the database")):
        with pytest.raises(HelperError, match=message) as refused:
            workspacehelper.remote_spec({**GOOD, **change})
        assert GOOD["password"] not in str(refused.value)
    with pytest.raises(HelperError, match="not JSON"):
        workspacehelper.remote_spec("{")


def test_database_actions_run_from_the_odoo_service(tmp_path, monkeypatch):
    project = tmp_path / "production"
    project.mkdir()
    (project / ".env").write_text("PROJECT=acme-shop-production\nDB_REMOTE=1\nDB_HOST=db.example.com\nDB_PORT=5432\n")
    assert composehelper.on_remote(project) == {"host": "db.example.com", "port": "5432"}
    monkeypatch.setattr(composehelper, "service", lambda p, name: name)
    name, prefix, _ = composehelper.db_target(project, composehelper.REMOTE_DB_SERVICE)
    assert (name, prefix) == (composehelper.MACHINE_ODOO, "machine-")
    with pytest.raises(HelperError, match="has its database on db.example.com"):
        composehelper.db_target(project, "db")
    # a project with a database of its own is not one
    (project / ".env").write_text("PROJECT=acme-shop-production\n")
    assert composehelper.on_remote(project) is None


def test_the_firewall_lets_one_address_through():
    import ipaddress
    pool = ipaddress.ip_network("10.213.0.0/16")
    rules = workspacehelper.firewall_rules(pool, [{"subnet": "10.213.4.0/24", "ip": "10.0.0.5", "port": 5432}])
    forward = rules[workspacehelper.CHAIN_FORWARD]
    through = ["-s", "10.213.4.0/24", "-d", "10.0.0.5", "-p", "tcp", "--dport", "5432", "-j", "RETURN"]
    drop = ["-s", str(pool), "-d", "10.0.0.0/8", "-j", "DROP"]
    assert through in forward and forward.index(through) < forward.index(drop)
    # nothing wider: no rule without its port or for the whole pool
    assert workspacehelper.firewall_rules(pool)[workspacehelper.CHAIN_FORWARD].count(through) == 0


def test_the_exceptions_file_keeps_only_what_is_right(tmp_path, monkeypatch):
    path = tmp_path / "db-remote.json"
    monkeypatch.setattr(workspacehelper, "DB_REMOTE", path)
    path.write_text('{"/a/production": {"subnet": "10.213.4.0/24", "ip": "10.0.0.5", "port": 5432},'
                    ' "/b/production": {"subnet": "x", "ip": "10.0.0.5", "port": 5432},'
                    ' "/c/production": {"subnet": "10.213.5.0/24", "ip": "10.0.0.6", "port": 99999}}')
    assert workspacehelper.db_remote_file() == {"/a/production": {"subnet": "10.213.4.0/24", "ip": "10.0.0.5", "port": 5432}}


def test_the_app_tells_a_remote_instance_apart():
    """The servers file's db_service of a remote instance is its own: the app
    never takes it for one of the machine's cluster (recovery at a minute,
    the Database tab, a staging are answered for what it is)."""
    import inspect

    import omniacli.app as appmod
    source = inspect.getsource(appmod)
    assert stacktemplates.REMOTE_DB_SERVICE != stacktemplates.MACHINE_DB_SERVICE
    assert source.count("if on_remote_db(prod):\n            return refuse(workspacehelper.PITR_ON_REMOTE)") == 3
    assert "a staging of it needs a database there" in source


def test_a_read_replica_is_checked_and_written():
    """AD-004 for a database of another machine: the replica's address
    checked, written in odoo.conf at the creation and at every rewrite."""
    spec = workspacehelper.remote_spec({**GOOD, "replica_host": "db-ro.example.com", "replica_port": "6433"})
    assert spec["replica"] == ("db-ro.example.com", 6433)
    # checked by the app, sent as JSON, checked again by the helper: the replica is still there
    import json
    again = workspacehelper.remote_spec(json.dumps(spec))
    assert again["replica"] == ("db-ro.example.com", 6433) and again["host"] == spec["host"]
    assert "replica" not in workspacehelper.remote_spec(dict(GOOD))
    # its port, when not given, is the database's
    assert workspacehelper.remote_spec({**GOOD, "replica_host": "db-ro.example.com"})["replica"] == ("db-ro.example.com", 5432)
    for change, message in (({"replica_host": "localhost"}, "not the host of a replica"),
                            ({"replica_host": "bad host"}, "not the host of a replica"),
                            ({"replica_host": "db.example.com"}, "the replica is the database itself"),
                            ({"replica_host": "db-ro.example.com", "replica_port": "70000"}, "not a port")):
        with pytest.raises(HelperError, match=message):
            workspacehelper.remote_spec({**GOOD, **change})
    template = stacktemplates.load_template(TEMPLATES / "odoo-19-remote")
    text = stacktemplates.conf_text(template, "acme_shop", "x", [], replica=("db-ro.example.com", 6433))
    assert "db_replica_host = db-ro.example.com\ndb_replica_port = 6433\n" in text
    assert "db_replica" not in stacktemplates.conf_text(template, "acme_shop", "x", [])
    assert workspacehelper._replica_of({"replica_on": "db-ro.example.com:6433"}) == ("db-ro.example.com", 6433)
    assert workspacehelper._replica_of({"replica_on": "bad host:1"}) is None and workspacehelper._replica_of({}) is None
