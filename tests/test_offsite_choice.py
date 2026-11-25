# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""AD-016 slice 1, «La scelta»: the machine's default destination and the instances' choices in destinations.yaml;
the one function that says an instance's effective destination; the destinations of the backup steps carried there
at the first start, and the effective one written back into each step for the release before; the Destinations
page («Default», «Make default…» with the key shown before, «No longer the default…», the note-box), the field
«Off-site copy to» of the Backups tab; who may choose (UX-016 §13); the REST API and MCP; the access log."""

import json

import yaml

from omniacli import destinations, pullcrypto, schedules, stagings
from omniacli.app import create_app
from omniacli.config import Workspace

from .conftest import TOKEN
from .test_api_v1 import V1, error_of, token
from .test_app import AUTH, POST, client  # noqa: F401 - the client fixture
from .test_workspaces import basic, post_as, workspaces  # noqa: F401 - the workspaces fixture

MACHINE, OTHER, OF_ACME = "wasabi-main", "nas-office", "dropbox-acme"
SECRET = "Sftp-Secret-pw-71"


def _dest(name, workspace=""):
    return destinations.parse_destination(name, {
        "kind": "sftp", "path": "backups/odoo", "workspace": workspace,
        "settings": {"host": "sftp.example.com", "user": "u", "pass": SECRET}, "key": pullcrypto.new_key()})


def _seed(settings, default=""):
    found = {MACHINE: _dest(MACHINE), OTHER: _dest(OTHER), OF_ACME: _dest(OF_ACME, "acme")}
    destinations.save_destinations(settings.destinations_file, found)
    if default:
        choices = destinations.load_choices(settings.destinations_file)
        choices.default = default
        destinations.save_choices(settings.destinations_file, choices)
    return found


def _backup_schedule(instance, destination=""):
    return schedules.parse_schedule(f"backup.{instance.replace('/', '.')}", {
        "instance": instance, "cron": "0 2 * * *", "backup": {"destination": destination}})


def test_the_effective_destination(env):
    settings = env["settings"]
    settings.workspaces["acme"] = Workspace("acme", servers=["srv"])
    found = _seed(settings)
    prod, test = settings.instance("srv", "production"), settings.instance("srv", "test")
    # no default, no choice: none (the production is to be looked at)
    assert destinations.effective(settings, prod).why == destinations.FROM_NONE
    choices = destinations.load_choices(settings.destinations_file)
    choices.default = MACHINE
    eff = destinations.effective(settings, prod, found, choices)
    assert (eff.name, eff.why) == (MACHINE, destinations.FROM_DEFAULT)
    # a choice it may use wins; a workspace's it may not use (the server left it) falls back to the default
    choices.instances["srv/production"] = {"destination": OF_ACME, "since": "2026-11-25T10:00:00"}
    assert destinations.effective(settings, prod, found, choices).name == OF_ACME
    settings.workspaces["acme"] = Workspace("acme", servers=[])
    eff = destinations.effective(settings, prod, found, choices)
    assert (eff.name, eff.why, eff.chosen) == (MACHINE, destinations.FROM_DEFAULT, OF_ACME)
    # a test and a staging: none, whatever is chosen (D-N3)
    assert destinations.effective(settings, test, found, choices).why == destinations.FROM_TEST
    stagings.save(settings.servers_file.parent / stagings.FILE, {"srv": {"parent": "erp"}})
    assert destinations.effective(settings, prod, found, choices).why == destinations.FROM_STAGING
    # a workspace's destination is never a default
    choices.default, choices.instances = OF_ACME, {}
    stagings.save(settings.servers_file.parent / stagings.FILE, {})
    assert destinations.effective(settings, prod, found, choices).why == destinations.FROM_NONE


def test_the_choices_and_the_destinations_keep_each_other(env):
    path = env["settings"].destinations_file
    _seed(env["settings"], default=MACHINE)
    # a destination saved keeps the choices; the choices saved keep the destinations, mode 600
    found = destinations.load_destinations(path)
    destinations.save_destinations(path, {k: v for k, v in found.items() if k != OTHER})
    assert destinations.load_choices(path).default == MACHINE and destinations.load_choices(path).migrated
    assert set(destinations.load_destinations(path)) == {MACHINE, OF_ACME}
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    # the release before reads the file as it reads its own: the destinations, and nothing else
    assert set(yaml.safe_load(path.read_text())["destinations"]) == {MACHINE, OF_ACME}


def test_the_migration_carries_the_backup_steps_once_and_writes_them_back(env):
    """A production with «Copy also to» of before keeps its destination after the migration, and the release before
    (which reads only the backup step) copies where this one does."""
    settings = env["settings"]
    _seed(settings)
    before = {"backup.srv.production": _backup_schedule("srv/production", OTHER),
              "backup.srv.test": _backup_schedule("srv/test", MACHINE)}
    schedules.save_schedules(settings.schedules_file, before)
    app = create_app(settings)
    assert app is not None
    choices = destinations.load_choices(settings.destinations_file)
    assert {k: v["destination"] for k, v in choices.instances.items()} == {"srv/production": OTHER,
                                                                           "srv/test": MACHINE}
    assert choices.migrated
    prod = settings.instance("srv", "production")
    assert destinations.effective(settings, prod).name == OTHER
    # each step says the effective one: the production's as it was, the test's none (a test has no copy, D-N3)
    after = schedules.load_schedules(settings.schedules_file)
    assert after["backup.srv.production"].backup.destination == OTHER
    assert after["backup.srv.test"].backup.destination == ""
    # done once: a default made later is not pinned as a choice at the next start
    choices.default = MACHINE
    choices.instances.pop("srv/production")
    destinations.save_choices(settings.destinations_file, choices)
    create_app(settings)
    assert "srv/production" not in destinations.load_choices(settings.destinations_file).instances
    assert schedules.load_schedules(settings.schedules_file)["backup.srv.production"].backup.destination == MACHINE


async def test_the_default_on_the_destinations_page(client, env):
    settings = env["settings"]
    found = _seed(settings)
    log = client.app.state.access_log
    page = (await client.get("/destinations", headers={"Authorization": AUTH})).text
    # no default: the note-box with the number of productions, a «Make default…» on each of the machine's
    assert 'class="note-box warn"' in page and "the 1 productions of this server" in page
    assert page.count("data-default title") == 2 and "data-undefault title" not in page and SECRET not in page
    # its key was never shown: refused without key_saved; shown, then it is the default
    resp = await client.post("/api/destinations/default", headers=POST, json={"destination": MACHINE})
    assert resp.status_code == 409 and resp.json()["code"] == "key_not_saved"
    assert (await client.post("/api/destinations/key", headers=POST, json={"name": MACHINE})).json()["key"] \
        == found[MACHINE].key
    assert destinations.load_choices(settings.destinations_file).keys_shown[MACHINE] \
        == destinations.key_fingerprint(found[MACHINE].key)
    resp = await client.post("/api/destinations/default", headers=POST, json={"destination": MACHINE})
    assert resp.status_code == 200 and resp.json()["default"]["destination"] == MACHINE, resp.text
    assert resp.json()["productions"] == {"total": 1, "chosen": 0, "following": 1}
    page = (await client.get("/destinations", headers={"Authorization": AUTH})).text
    assert 'class="note-box warn"' not in page and page.count("data-undefault title") == 1
    assert page.count("data-default title") == 1 and ">Default</span>" in page
    # a workspace's is never the default; the default is not deleted
    resp = await client.post("/api/destinations/default", headers=POST, json={"destination": OF_ACME})
    assert resp.status_code == 404
    resp = await client.post("/api/destinations/delete", headers=POST, json={"name": MACHINE})
    assert resp.status_code == 400 and "default" in resp.json()["error"]
    # a developer of the machine does not choose it
    resp = await client.post("/api/destinations/default", headers=post_as("dev"), json={"destination": ""})
    assert resp.status_code == 403
    # «No longer the default…»
    resp = await client.post("/api/destinations/default", headers=POST, json={"destination": ""})
    assert resp.status_code == 200 and resp.json()["default"] is None
    said = [e["detail"] for e in log.recent() if e["event"] == "default destination"]
    assert said == [f"the default destination of the backups: none (was {MACHINE})",
                    f"the default destination of the backups: {MACHINE} (was none)"]


async def test_the_field_of_an_instance_and_who_chooses(workspaces, client, env):
    settings = env["settings"]
    _seed(settings, default=MACHINE)
    url = "/api/servers/srv/production"
    # the machine's admin: the default with its name first and chosen, the machine's others, the workspace's
    data = (await client.get(f"{url}/backup-schedule", headers={"Authorization": AUTH})).json()["offsite"]
    assert data["shown"] and data["value"] == "" and data["options"][0] == {"value": "", "kind": "default",
                                                                            "name": MACHINE}
    assert [o["value"] for o in data["options"]] == ["", OTHER, MACHINE, OF_ACME]
    assert not [o for o in data["options"] if o["kind"] == "none"]
    # the admin of the workspace (P2): «Server's default» without its name, the workspace's only, no name of the
    # machine's destinations anywhere in what it reads
    raw = (await client.get(f"{url}/backup-schedule", headers=basic("mario"))).text
    data = json.loads(raw)["offsite"]
    assert data["options"] == [{"value": "", "kind": "default", "name": ""},
                               {"value": OF_ACME, "kind": "workspace", "name": OF_ACME, "label": "SFTP"}]
    assert MACHINE not in raw and OTHER not in raw and "sftp.example.com" not in raw
    resp = await client.post(f"{url}/backup-destination", headers=post_as("mario"), json={"destination": OTHER})
    assert resp.status_code == 404
    resp = await client.post(f"{url}/backup-destination", headers=post_as("mario"), json={"destination": OF_ACME})
    assert resp.status_code == 200 and resp.json()["value"] == OF_ACME and resp.json()["effective"] == OF_ACME
    assert destinations.load_choices(settings.destinations_file).choice("srv/production") == OF_ACME
    # a user of the workspace does not choose; the release before reads the step: written there
    resp = await client.post(f"{url}/backup-destination", headers=post_as("luca"), json={"destination": ""})
    assert resp.status_code == 403
    sch = schedules.load_schedules(settings.schedules_file) if settings.schedules_file.exists() else {}
    assert all(s.backup.destination == OF_ACME for s in sch.values() if s.backup and s.instance == "srv/production")
    # chosen by the machine's admin among the machine's: the workspace's admin reads «a destination of the server»
    resp = await client.post(f"{url}/backup-destination", headers=POST, json={"destination": OTHER})
    assert resp.status_code == 200
    raw = (await client.get(f"{url}/backup-schedule", headers=basic("mario"))).text
    data = json.loads(raw)["offsite"]
    assert data["value"] == "*" and data["effective"] == "" and data["effective_kind"] == "machine"
    assert OTHER not in raw and MACHINE not in raw
    # back to the default; each change in the access log, the workspace's without the machine's names
    resp = await client.post(f"{url}/backup-destination", headers=POST, json={"destination": ""})
    assert resp.status_code == 200 and resp.json()["value"] == "" and resp.json()["effective"] == MACHINE
    said = [e["detail"] for e in client.app.state.access_log.recent() if e["event"] == "instance destination"]
    assert said[0] == "srv/production: the destination of its backups the default (was a destination of the machine)"
    assert len(said) == 3
    # a test has no field (D-N3)
    data = (await client.get("/api/servers/srv/test/backup-schedule", headers={"Authorization": AUTH})).json()
    assert data["offsite"]["shown"] is False
    resp = await client.post("/api/servers/srv/test/backup-destination", headers=POST, json={"destination": OTHER})
    assert resp.status_code == 400


async def test_a_staging_has_no_field(client, env):
    _seed(env["settings"], default=MACHINE)
    stagings.save(env["settings"].servers_file.parent / stagings.FILE, {"srv": {"parent": "erp"}})
    data = (await client.get("/api/servers/srv/production/backup-schedule", headers={"Authorization": AUTH})).json()
    assert data["offsite"]["shown"] is False and data["destination"] == ""


async def test_the_rest_api_and_mcp(client, env):
    from .test_mcp import HEADERS, call

    settings = env["settings"]
    _seed(settings)
    admin = await token(client, "boss", "admin")
    dev = await token(client, "devtok", "developer")
    status = (await client.get(f"{V1}/destinations", headers=admin)).json()
    assert status["default"] is None and {d["name"] for d in status["destinations"]} == {MACHINE, OTHER, OF_ACME}
    assert SECRET not in json.dumps(status) and not any(d["key_shown"] for d in status["destinations"])
    error_of(await client.get(f"{V1}/destinations", headers=dev), 403, "forbidden")
    error_of(await client.post(f"{V1}/destinations/default", headers=admin, json={"destination": MACHINE}),
             409, "key_not_saved")
    done = (await client.post(f"{V1}/destinations/default", headers=admin,
                              json={"destination": MACHINE, "key_saved": True})).json()
    assert done["default"]["destination"] == MACHINE
    one = (await client.get(f"{V1}/instances/srv/production/destination", headers=dev)).json()
    assert one["effective"] == MACHINE and one["why"] == "default"
    # a production's is an admin's
    error_of(await client.post(f"{V1}/instances/srv/production/destination", headers=dev,
                               json={"destination": OTHER}), 403, "forbidden")
    done = (await client.post(f"{V1}/instances/srv/production/destination", headers=admin,
                              json={"destination": OTHER})).json()
    assert done["value"] == OTHER
    # MCP: the same code
    found = await call(client, "instance_destination", headers={**HEADERS, **admin}, server_name="srv",
                       role="production")
    assert found["value"] == OTHER
    found = await call(client, "set_instance_destination", headers={**HEADERS, **admin}, server_name="srv",
                       role="production", destination="")
    assert found["value"] == "" and found["effective"] == MACHINE
    found = await call(client, "set_default_destination", headers={**HEADERS, **admin}, destination="")
    assert found["default"] is None
    found = await call(client, "destinations_status", headers={**HEADERS, **admin})
    assert found["productions"] == {"total": 1, "chosen": 0, "following": 1}
    # the machine's default is not for a developer's token
    found = await call(client, "set_default_destination", headers=HEADERS, destination=MACHINE)
    assert "error" in found
    assert TOKEN not in json.dumps(found)

