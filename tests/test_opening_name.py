# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The name an environment is opened at: one the DNS leads to this machine,
not the first of its names whatever happens; the name OmniaCLI gives by
itself when none of them does; the reason a preparation gives when it stops."""
import json
from types import SimpleNamespace

import pytest

from omniacli import machines
from omniacli.config import (AUTOMATIC_NAMES, ConfigError, Options, apply_sites, automatic_name, load_web_names,
                             opening_name, parse_options, parse_workspaces, save_options)

STG = "stg-20261121.odoo.customer.com"
AUTO = "odoo-stg-20261121-acme.203-0-113-7.sslip.io"


def test_the_first_name_when_nothing_is_known():
    assert opening_name([STG, AUTO], None) == STG
    assert opening_name([STG, AUTO], {}) == STG


def test_a_name_without_dns_is_not_where_the_browser_is_sent():
    known = {STG: {"dns": "none", "https": False}, AUTO: {"dns": "here", "https": True}}
    assert opening_name([STG, AUTO], known) == AUTO


def test_its_own_name_again_once_its_record_is_there():
    known = {STG: {"dns": "here", "https": True}, AUTO: {"dns": "here", "https": True}}
    assert opening_name([STG, AUTO], known) == STG


def test_a_name_that_leads_here_with_https_before_one_without():
    known = {STG: {"dns": "here", "https": False}, AUTO: {"dns": "here", "https": True}}
    assert opening_name([STG, AUTO], known) == AUTO
    assert opening_name([STG, AUTO], {STG: {"dns": "here", "https": False}, AUTO: {"dns": "none"}}) == STG


def test_a_name_that_leads_elsewhere_is_not_opened():
    known = {STG: {"dns": "elsewhere", "https": False}, AUTO: {"dns": "here", "https": True}}
    assert opening_name([STG, AUTO], known) == AUTO


def test_the_automatic_name_is_built_on_the_public_address():
    assert automatic_name("acme", "odoo-stg-20261121", "production", "203.0.113.7", "sslip.io") == AUTO
    assert automatic_name("acme", "odoo", "test", "203.0.113.7", "sslip.io") == "test-odoo-acme.203-0-113-7.sslip.io"


def test_no_automatic_name_without_an_ipv4_or_a_domain():
    assert automatic_name("acme", "odoo", "production", "", "sslip.io") is None
    assert automatic_name("acme", "odoo", "production", "2001:db8::1", "sslip.io") is None
    assert automatic_name("acme", "odoo", "production", "203.0.113.7", "") is None


def test_a_long_automatic_label_stays_a_label():
    name = automatic_name("w" * 31, "i" * 31 + "-stg-20261121", "production", "203.0.113.7", "sslip.io")
    assert name is not None and len(name.split(".")[0]) <= 63
    other = automatic_name("w" * 31, "i" * 31 + "-stg-20261122", "production", "203.0.113.7", "sslip.io")
    assert other != name


def _fixture():
    workspaces = parse_workspaces({"acme": {
        "storage": "default", "domain": "customer.com", "main": "odoo",
        "servers": ["acme-odoo", "acme-odoo-stg-20261121"],
        "aliases": {"odoo-stg-20261121": {"production": [AUTO]}}}}, ())
    servers = {name: SimpleNamespace(instances={"production": SimpleNamespace(site=None)})
               for name in ("acme-odoo", "acme-odoo-stg-20261121")}
    return servers, workspaces


def test_a_staging_is_opened_at_its_other_name_when_its_own_has_no_record():
    servers, workspaces = _fixture()
    own = "stg-20261121.customer.com"
    apply_sites(servers, workspaces)
    assert servers["acme-odoo-stg-20261121"].instances["production"].site == f"https://{own}"
    known = {"acme": {own: {"dns": "none", "https": False}, AUTO: {"dns": "here", "https": True},
                      "customer.com": {"dns": "here", "https": True}}}
    apply_sites(servers, workspaces, known)
    assert servers["acme-odoo-stg-20261121"].instances["production"].site == f"https://{AUTO}"
    assert servers["acme-odoo"].instances["production"].site == "https://customer.com"


def test_the_names_file_of_the_publications_is_read(tmp_path):
    path = tmp_path / "webnames.json"
    assert load_web_names(path) == {}
    path.write_text(json.dumps({"acme": {"at": "x", "names": {STG: {"dns": "none", "https": False}}}, "bad": 3}))
    assert load_web_names(path) == {"acme": {STG: {"dns": "none", "https": False}}}
    path.write_text("not json")
    assert load_web_names(path) == {}


def test_the_option_of_the_automatic_names(tmp_path):
    assert parse_options({}).automatic_names == AUTOMATIC_NAMES == "sslip.io"
    assert parse_options({"names": {"automatic": "off"}}).automatic_names == ""
    assert parse_options({"names": {"automatic": "Apps.Example.com."}}).automatic_names == "apps.example.com"
    with pytest.raises(ConfigError):
        parse_options({"names": {"automatic": "not a domain"}})
    path = tmp_path / "options.yaml"
    save_options(path, Options(automatic_names=""))
    assert parse_options(__import__("yaml").safe_load(path.read_text())).automatic_names == ""
    save_options(path, Options())
    assert "names" not in (__import__("yaml").safe_load(path.read_text()) or {})


def test_a_preparation_fails_with_what_the_script_said():
    lines = ["10:21:41 --- prepare root@203.0.113.7:22", "10:21:42 --- the system",
             "10:21:42 PROVISION FAILED: Ubuntu 20.04.6 LTS is supported only as a server that already has Odoo",
             "10:21:42 FAILED: ssh exited with code 1"]
    assert machines.failure_said(lines) == "Ubuntu 20.04.6 LTS is supported only as a server that already has Odoo"
    assert machines.failure_said(["10:21:41 --- prepare", "10:21:42 Connection closed"]) is None


def test_no_name_is_an_address_when_none_came_out_with_https():
    servers, workspaces = _fixture()
    own = "stg-20261121.customer.com"
    known = {"acme": {own: {"dns": "here", "https": False}, AUTO: {"dns": "here", "https": False},
                      "customer.com": {"dns": "here", "https": True}}}
    apply_sites(servers, workspaces, known)
    # https://<name> would reach another site of the machine: the port is its address
    assert servers["acme-odoo-stg-20261121"].instances["production"].site is None
    assert servers["acme-odoo"].instances["production"].site == "https://customer.com"
    # an environment the publication has not read yet keeps its first name
    apply_sites(servers, workspaces, {"acme": {"customer.com": {"dns": "here", "https": True}}})
    assert servers["acme-odoo-stg-20261121"].instances["production"].site == f"https://{own}"
