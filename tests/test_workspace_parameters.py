# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The editable Odoo parameters of a workspace's instance and its debug log:
the template's bounds or the workspace's, workers x memory against the
workspace's memory, the helper's conf, the role restarted."""
import json
from pathlib import Path

import pytest

from omniacli import workspacehelper, workspaceops
from omniacli.config import ConfigError, parse_workspaces

from .test_app import POST, client, wait_job  # noqa: F401 - the client fixture
from .test_workspace_repositories import instance  # noqa: F401 - the instance fixture
from .test_workspaces import basic, post_as, workspaces  # noqa: F401 - the workspaces fixture

REPO_TEMPLATES = Path(__file__).parent.parent / "templates"


def test_parse_the_bounds():
    ws = parse_workspaces({"acme": {"parameter_bounds": {"workers": ["0", "16"]}}})["acme"]
    assert ws.parameter_bounds == {"workers": [0, 16]} and ws.to_dict()["parameter_bounds"] == {"workers": [0, 16]}
    for bounds, message in (({"workers": [5, 1]}, "min <= max"), ({"workers": ["a", 1]}, "two whole numbers"),
                            ({"Bad-Name": [0, 1]}, "parameter name")):
        with pytest.raises(ConfigError, match=message):
            parse_workspaces({"acme": {"parameter_bounds": bounds}})


@pytest.fixture
def made(instance, monkeypatch):
    """acme-main made from odooplm-20 (its instance.json), the helper's calls recorded."""
    monkeypatch.setattr(workspacehelper, "TEMPLATES", REPO_TEMPLATES)
    folder = instance["folder"]
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "instance.json").write_text(json.dumps({"template": "odooplm-20", "version": "x", "database": "main"}))
    helper = []

    async def call(job, *args):
        if args[0] != "conf":
            helper.append(args)
            return {}
        helper.append(args)
        confs.append(args)
        helper.append(args)
        return {}
    monkeypatch.setattr(workspaceops, "_helper", call)
    confs = []
    return {**instance, "helper": helper, "confs": confs}


async def test_the_parameters_of_a_role(made, client):
    settings, helper, calls, confs = made["settings"], made["helper"], made["calls"], made["confs"]
    # the workspace's page sends to the instance's Settings, which hold the parameters (UX-007): not twice
    page = (await client.get("/workspaces/acme", headers=basic("mario"))).text
    assert 'href="/servers/acme-main#modules"' in page and "data-param=" not in page
    page = (await client.get("/servers/acme-main", headers=basic("mario"))).text
    assert 'data-section="odoo-config"' in page and 'data-param="limit_time_cpu"' in page
    url = "/api/workspaces/acme/parameters/save"
    body = {"server": "acme-main", "role": "test", "parameters": {"workers": "2", "limit_memory_hard": "3072",
                                                                    "smtp_password": "s3cret"}, "debug": True}
    resp = await client.post(url, headers=post_as("mario"), json=body)
    assert resp.status_code == 200, resp.text
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success", job["lines"]
    action, *args = confs[-1]
    assert (action, args[:4]) == ("conf", ["acme", settings.workspaces["acme"].storage, "main", "test"])
    # acme has a domain: its names published again (the websocket follows workers)
    assert helper[-1][0] == "web"
    spec = json.loads(args[4])
    assert spec["parameters"]["workers"] == "2" and spec["debug"] is True and spec["bounds"] == {}
    assert calls["restart"] == ["acme-main/test"]
    # the template's bounds; the workspace's when the machine's admins set them
    for parameters, message in (({"workers": "9"}, "between 0 and 8"), ({"data_dir": "/"}, "no editable")):
        resp = await client.post(url, headers=post_as("mario"), json={**body, "parameters": parameters})
        assert resp.status_code == 400 and message in resp.json()["error"], parameters
    settings.workspaces["acme"].parameter_bounds = {"workers": [0, 16]}
    resp = await client.post(url, headers=post_as("mario"), json={**body, "parameters": {"workers": "12"}})
    assert resp.status_code == 200 and json.loads(confs[-1][-1])["bounds"] == {"workers": [0, 16]}
    await wait_job(client, resp.json()["job"])
    # workers x limit_memory_hard within the workspace's memory
    settings.workspaces["acme"].limits = {"memory_gb": 4}
    resp = await client.post(url, headers=post_as("mario"), json=body)
    assert resp.status_code == 400 and "more than the workspace's 4 GB" in resp.json()["error"]
    # a password left empty keeps the one saved (the helper's record of the role)
    (made["folder"] / "test").mkdir(parents=True, exist_ok=True)
    (made["folder"] / "test" / "omniacli.json").write_text(json.dumps({"parameters": {"smtp_password": "Pw-kept-9Z"}}))
    resp = await client.post(url, headers=post_as("mario"), json={**body, "parameters": {"smtp_password": ""}})
    assert resp.status_code == 200
    assert json.loads(confs[-1][-1])["parameters"]["smtp_password"] == "Pw-kept-9Z"
    await wait_job(client, resp.json()["job"])
    # a password saved is never in a page: not in the instance's options of its test, not in the workspace's
    page = (await client.get("/servers/acme-main?view=test", headers=basic("mario"))).text
    assert 'data-param="smtp_password"' in page and "Pw-kept-9Z" not in page
    assert "Pw-kept-9Z" not in (await client.get("/workspaces/acme", headers=basic("mario"))).text
    # its users and other workspaces' cannot
    assert (await client.post(url, headers=post_as("luca"), json=body)).status_code == 403
    assert (await client.post(url, headers=post_as("eva"), json=body)).status_code == 403
    resp = await client.post(url, headers=post_as("mario"), json={**body, "role": "stage"})
    assert resp.status_code == 400


async def test_the_other_options_of_a_role(made, client, monkeypatch):
    """The Extended view: the options its Odoo knows, read in the container;
    the other options saved with the parameters, a secret kept when empty."""
    from omniacli import ops, stacktemplates
    asked = []

    async def capture(cmd, **kwargs):
        asked.append(cmd)
        if cmd[-2:] != ["options", "odoo"]:
            return 1, ""
        options = [{"name": n, "help": "h", "kind": "string", "group": "g", "default": None}
                   for n in ("workers", "unaccent", "data_dir", "geoip_db_key", "init")]
        return 0, "noise\n" + stacktemplates.OPTIONS_MARK + json.dumps({"options": options, "version": "18.0"})
    monkeypatch.setattr(ops, "capture", capture)
    (made["folder"] / "test" / "config").mkdir(parents=True, exist_ok=True)
    (made["folder"] / "test" / "config" / "odoo.conf").write_text(
        "[options]\ndata_dir = /var/lib/odoo\ndb_name = main\nadmin_passwd = Hidden-1\nunaccent = True\n")
    (made["folder"] / "test" / "omniacli.json").write_text(json.dumps({"extra": {"unaccent": "True",
                                                                                 "geoip_db_key": "Kept-1"}}))
    url = "/api/workspaces/acme/parameters/options"
    resp = await client.post(url, headers=post_as("mario"), json={"server": "acme-main", "role": "test"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # the command line's own (init) are not a config file's
    assert ["options", "odoo"] in [c[-2:] for c in asked] and data["version"] == "18.0" and len(data["options"]) == 4
    # a greyed one shows what its odoo.conf has, a secret never
    assert data["values"] == {"data_dir": "/var/lib/odoo", "db_name": "main", "admin_passwd": ""}
    # a secret's value never goes to the browser
    assert data["extra"] == {"unaccent": "True", "geoip_db_key": ""} and data["secrets"] == ["geoip_db_key"]
    assert {"data_dir", "workers", "dbfilter"} <= set(data["locked"])
    save = "/api/workspaces/acme/parameters/save"
    body = {"server": "acme-main", "role": "test", "parameters": {}, "debug": False,
            "extra": {"unaccent": "", "geoip_db_key": "", "limit_time_real_cron": "60"}}
    resp = await client.post(save, headers=post_as("mario"), json=body)
    assert resp.status_code == 400 and "not here" in resp.json()["error"]
    body["extra"] = {"unaccent": "", "geoip_db_key": "", "list_db": "x", "server_wide_modules": "base,web"}
    resp = await client.post(save, headers=post_as("mario"), json=body)
    assert resp.status_code == 400 and "list_db" in resp.json()["error"]
    del body["extra"]["list_db"]
    resp = await client.post(save, headers=post_as("mario"), json=body)
    assert resp.status_code == 200, resp.text
    assert json.loads(made["confs"][-1][-1])["extra"] == {"geoip_db_key": "Kept-1", "server_wide_modules": "base,web"}
    await wait_job(client, resp.json()["job"])
    # without the Extended view read, the others are not sent: the helper keeps them
    del body["extra"]
    resp = await client.post(save, headers=post_as("mario"), json=body)
    assert "extra" not in json.loads(made["confs"][-1][-1])
    await wait_job(client, resp.json()["job"])
    # its users and other workspaces' cannot read them
    for user in ("luca", "eva"):
        assert (await client.post(url, headers=post_as(user), json={"server": "acme-main"})).status_code == 403


def test_commands_typed_in_the_container_reach_the_database(tmp_path, monkeypatch):
    """odoo shell and psql typed in the Odoo container connect: the templates
    give PG* beside the HOST... of the image's entrypoint, the Terminal
    exports them, and an instance made before gets them at its next
    configuration (taken at the restart: up -d). The empty folder of the
    image's volume is not an addons path (Odoo warned at every start)."""
    import json
    from pathlib import Path

    from omniacli import composehelper, stacktemplates, workspacehelper

    root = Path(__file__).parent.parent / "templates"
    for name in ("odoo-18", "odoo-19", "odooplm-20"):
        compose = (root / name / "compose.yaml").read_text()
        for line in ("PGHOST: db", 'PGPORT: "5432"', "PGUSER: odoo", "PGPASSWORD: ${DB_PASSWORD}"):
            assert compose.count(line) == 1, (name, line)
        assert "/mnt/extra-addons" not in json.loads((root / name / "template.json").read_text())["addons_path"]
        # still mounted: a volume of the image, otherwise an anonymous one
        assert "./data/extra-addons:/mnt/extra-addons:ro" in compose
    template = stacktemplates.load_template(root / "odoo-19")
    assert "addons_path = /mnt/customer-addons/web\n" in stacktemplates.conf_text(template, "db", "pw", ["web"])
    assert composehelper.SCRIPTS["terminal"].startswith(composehelper.ODOO_DB)
    # an instance made before
    monkeypatch.setattr(workspacehelper, "_write", lambda path, text, mode=0o644, uid=0, gid=0: path.write_text(text))
    project = tmp_path / "production"
    project.mkdir()
    new = (root / "odoo-19" / "compose.yaml").read_text()
    old = "\n".join(line for line in new.splitlines() if "PG" not in line and "the same connection" not in line
                    and "click-odoo, psql" not in line) + "\n"
    assert "PGHOST" not in old
    (project / "compose.yaml").write_text(old)
    assert workspacehelper._ensure_pg_env(project) is True
    assert [line.strip() for line in (project / "compose.yaml").read_text().splitlines() if "PG" in line] == \
        ["PGHOST: db", 'PGPORT: "5432"', "PGUSER: odoo", "PGPASSWORD: ${DB_PASSWORD}"]
    assert workspacehelper._ensure_pg_env(project) is False  # once
    (project / "compose.yaml").write_text("services: {}\n")
    assert workspacehelper._ensure_pg_env(project) is False  # not a template's: left alone
