# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Odoo versions a new instance may have (templates odoo-16, odoo-17,
odoo-20 beside odoo-18 and odoo-19, each with a database of its own, on the
machine's cluster and on another machine): what each template runs, the
New instance dialog offering them, Of the machine turned off with why for
a version whose PostgreSQL is not the cluster's, and the refusals of what
an older Odoo cannot do (a read replica before 18, mailboxes before 17)."""

import json
import re
import shutil
from pathlib import Path

import pytest
import yaml

from omniacli import composehelper, machinedb, stacktemplates, workspacehelper, workspaceops

from .test_app import AUTH, POST, client  # noqa: F401 - the client fixture
from .test_stacktemplates import REPO_TEMPLATES, root  # noqa: F401 - the root fixture
from .test_workspaces import space_of, workspaces  # noqa: F401 - the workspaces fixture

# the versions with a template of the official image, and the user each image runs Odoo as
# (id in the container, measured on the images of 22/11/2026)
OFFERED = {"16": "101:101", "17": "101:101", "18": "100:101", "19": "100:101", "20": "100:101"}
NEW = ("16", "17", "20")
REFERENCE = stacktemplates.load_template(REPO_TEMPLATES / "odoo-19")


@pytest.mark.parametrize("major", NEW)
@pytest.mark.parametrize("suffix", ["", "-machine", "-remote"])
def test_each_new_template_runs_its_image_and_postgresql(major, suffix):
    template = stacktemplates.load_template(REPO_TEMPLATES / f"odoo-{major}{suffix}")
    assert template.name == f"odoo-{major}{suffix}" and template.version == "2026.1.1"
    assert template.label.startswith(f"Odoo {major} Community (official image")
    image = next(f for f in template.fields if f.name == "IMAGE")
    assert image.default == f"odoo:{major}.0" and image.choices == [f"odoo:{major}.0", f"odoo:{major}"]
    services = yaml.safe_load(template.compose)["services"]
    assert services["odoo"]["image"] == "${IMAGE}"
    if suffix == "":
        # its own PostgreSQL 17, named in template.json and in compose.yaml alike
        assert template.own_database and template.postgres == "17" and services["db"]["image"] == "postgres:17"
    elif suffix == "-machine":
        assert template.on_machine and template.postgres == "17" and template.dedicated == f"odoo-{major}"
        assert "db" not in services
    else:
        assert template.on_remote and template.postgres == "" and "db" not in services
    # the filestore and Odoo's /tmp owned by the user the image runs Odoo as
    owners = {d["path"]: d["owner"] for d in template.data}
    assert owners["data/filestore"] == owners["data/tmp"] == OFFERED[major]
    # the same options, limits, mail and log as the others
    assert [p.to_dict() for p in template.parameters] == [p.to_dict() for p in
                                                          stacktemplates.load_template(
                                                              REPO_TEMPLATES / f"odoo-19{suffix}").parameters]
    assert services["odoo"]["logging"] == REFERENCE_SERVICES["odoo"]["logging"]
    assert services["odoo"]["read_only"] is True and services["odoo"]["cap_drop"] == ["ALL"]
    # no secret in compose.yaml: the passwords are variables of .env
    assert not re.search(r"PASSWORD: (?!\$\{)", template.compose)
    assert "{{ADMIN_PASSWD}}" in template.conf and f"template odoo-{major}{suffix}:" in template.conf


REFERENCE_SERVICES = yaml.safe_load(REFERENCE.compose)["services"]


def test_odoo_16_catches_the_outgoing_mail_only():
    """Odoo 16 has no mail.alias.domain, which odoo/mail_inbox.py needs: no
    service inbox, Mailpit hands nothing on; Odoo 17 and 20 have both."""
    for suffix in ("", "-machine", "-remote"):
        template = stacktemplates.load_template(REPO_TEMPLATES / f"odoo-16{suffix}")
        assert template.captures_mail() and not template.receives_mail()
        mail = yaml.safe_load(template.compose)["services"]["mail"]
        assert not [k for k in mail["environment"] if k.startswith("MP_SMTP_RELAY")]
    for major in ("17", "20"):
        assert stacktemplates.load_template(REPO_TEMPLATES / f"odoo-{major}").receives_mail()
    assert workspacehelper.without_alias_domain({"template": "odoo-16-machine"}) == 16
    assert workspacehelper.without_alias_domain({"template": "odoo-17"}) is None
    assert workspacehelper.without_alias_domain({}) is None


def test_the_postgresql_a_dedicated_template_names_is_its_image(tmp_path):
    folder = tmp_path / "odoo-16"
    shutil.copytree(REPO_TEMPLATES / "odoo-16", folder)
    meta = json.loads((folder / "template.json").read_text())
    (folder / "template.json").write_text(json.dumps({**meta, "postgres": "16"}))
    with pytest.raises(stacktemplates.TemplateError, match="postgres:16, the PostgreSQL its template.json names"):
        stacktemplates.load_template(folder)
    (folder / "template.json").write_text(json.dumps({**meta, "postgres": "seventeen"}))
    with pytest.raises(stacktemplates.TemplateError, match="the major of its PostgreSQL"):
        stacktemplates.load_template(folder)


def test_the_update_finds_a_click_for_an_older_python():
    """click 8.5 of the tools needs Python 3.10: odoo:16.0 (3.9) takes click
    8.1.8 of the tools' py39, which the package installs."""
    script = composehelper.SCRIPTS["update"]
    assert "sys.version_info < (3, 10)" in script
    assert f"p={composehelper.TOOLS_MOUNT}/py39:$p" in script and 'export PYTHONPATH="$p"' in script
    assert script.index("PYTHONPATH") < script.index("click-odoo-update")
    requirements = Path(__file__).parent.parent / "packaging" / "debian" / "requirements"
    assert [line for line in (requirements / "tools-py39.txt").read_text().splitlines()
            if line and not line.startswith("#")] == ["click==8.1.8"]
    postinst = (Path(__file__).parent.parent / "packaging" / "debian" / "files" / "postinst").read_text()
    assert '--python-version 3.9' in postinst and '--target "$VENV/omniacli-tools/py39"' in postinst
    assert "tools-py39.txt" in (Path(__file__).parent.parent / "packaging" / "debian" / "build_deb.sh").read_text()


def test_a_replica_is_refused_before_odoo_18(root):  # noqa: F811
    remote = {"host": "db.example.com", "port": 5432, "replica": ("db2.example.com", 5432)}
    assert "Odoo 17 cannot read from a replica: it can from 18 on" in workspacehelper.replica_refused(
        "odoo-17-remote", remote)
    assert workspacehelper.replica_refused("odoo-16-remote", remote).startswith("Odoo 16 cannot")
    assert workspacehelper.replica_refused("odoo-18-remote", remote) is None
    assert workspacehelper.replica_refused("odoo-16-remote", {**remote, "replica": None}) is None


def _found(*names, extra=None):
    found = {n: stacktemplates.load_template(REPO_TEMPLATES / n) for n in names}
    return {**found, **(extra or {})}


async def test_the_dialog_offers_every_version(workspaces, client, env, monkeypatch):  # noqa: F811
    """The New instance dialog: 20 to 16, newest first, each on the machine's
    cluster when there is one."""
    names = [f"odoo-{m}{s}" for m in OFFERED for s in ("", "-machine", "-remote")]
    monkeypatch.setattr(workspaceops, "templates", lambda: _found(*names, "odooplm-20"))

    async def space():
        return space_of(500, {}, root="main")
    monkeypatch.setattr(workspaceops, "space", space)
    env["settings"].storage_roots = {"main": "/s"}
    monkeypatch.setattr(machinedb, "registry", lambda: {})
    data = (await client.get("/api/instances/versions", headers={"Authorization": AUTH})).json()
    assert [v["major"] for v in data["versions"]] == [20, 19, 18, 17, 16]
    assert data["cluster"] is False and data["cluster_postgres"] == []
    assert [v["label"] for v in data["versions"]][-1] == "Odoo 16 Community (official image + PostgreSQL 17)"
    monkeypatch.setattr(machinedb, "registry", lambda: {"17": {"storage": "main", "project": "/x"}})
    data = (await client.get("/api/instances/versions", headers={"Authorization": AUTH})).json()
    assert all(v["machine"] and "machine_postgres" not in v for v in data["versions"])
    assert data["cluster_postgres"] == ["17"]


async def test_a_version_on_another_postgresql_is_not_offered_on_the_cluster(workspaces, client, env,  # noqa: F811
                                                                             monkeypatch, tmp_path):
    """AD-001, one PostgreSQL per machine: a version whose template names
    another major (here an Odoo 14 on PostgreSQL 16, with no template on
    the cluster) has Of the machine off, with why; asked, it is refused."""
    folder = tmp_path / "odoo-14"
    shutil.copytree(REPO_TEMPLATES / "odoo-16", folder)
    meta = json.loads((folder / "template.json").read_text())
    (folder / "template.json").write_text(json.dumps({**meta, "name": "odoo-14", "postgres": "16"}))
    (folder / "compose.yaml").write_text((folder / "compose.yaml").read_text().replace("postgres:17", "postgres:16"))
    old = stacktemplates.load_template(folder)
    monkeypatch.setattr(workspaceops, "templates", lambda: _found("odoo-16", "odoo-16-machine", extra={"odoo-14": old}))
    monkeypatch.setattr(machinedb, "registry", lambda: {"17": {"storage": "main", "project": "/x"}})
    env["settings"].storage_roots = {}
    data = (await client.get("/api/instances/versions", headers={"Authorization": AUTH})).json()
    by = {v["major"]: v for v in data["versions"]}
    assert by[16]["machine"] is True and "machine_postgres" not in by[16]
    assert by[14]["machine"] is False and by[14]["machine_postgres"] == "16"
    resp = await client.post("/api/instances/new", headers=POST, json={"version": "14", "database": "machine"})
    assert resp.status_code == 400
    assert "Odoo 14 runs on PostgreSQL 16, the machine's cluster is PostgreSQL 17" in resp.json()["error"]
    # not asked: a dedicated one, as the dialog chooses (then no storage here)
    resp = await client.post("/api/instances/new", headers=POST, json={"version": "14"})
    assert resp.status_code == 400 and "No storage" in resp.json()["error"]


def test_the_dialog_says_why_of_the_machine_is_off():
    page = (Path(workspacehelper.__file__).parent / "templates" / "_new_instance_dialog.html").read_text()
    assert "version.machine_postgres ? T.otherPostgres" in page
    assert '.replace("{cluster}", (data.cluster_postgres || []).join(", "))' in page
    assert 'document.getElementById("ni-db-machine-label").title = why;' in page


def test_the_texts_are_in_italian():
    from omniacli import i18n
    assert i18n.tr("it", "Odoo 14 runs on PostgreSQL 16, the machine's cluster is PostgreSQL 17: choose a "
                         "dedicated database").startswith("Odoo 14 gira su PostgreSQL 16")
    assert i18n.tr("it", "Odoo 16 cannot read from a replica: it can from 18 on (db_replica_host). Leave the "
                         "replica empty").endswith("Lascia vuota la replica")
    assert i18n.tr("it", "s receives no mail: Odoo 16 has no alias domain, which its mailboxes need (Odoo 17 and "
                         "later). Its outgoing mail is caught only").startswith("s non riceve posta: Odoo 16")
