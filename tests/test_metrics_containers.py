# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The load of a Docker Compose instance: its running containers' cgroups."""

import json

from omniacli import metrics, ops
from omniacli.config import parse_servers


def test_the_cgroups_of_the_containers(tmp_path):
    proc = tmp_path / "proc"
    for pid, line in (("10", "0::/omniacli_acme.slice/docker-" + "a" * 64 + ".scope"), ("11", "0::/system.slice/x"),
                      ("self", "0::/")):
        (proc / pid).mkdir(parents=True)
        (proc / pid / "cgroup").write_text(line + "\n")
    assert metrics.container_cgroups(proc) == {"a" * 64: "/omniacli_acme.slice/docker-" + "a" * 64 + ".scope"}
    base = tmp_path / "cg"
    base.mkdir()
    (base / "cpu.stat").write_text("usage_usec 2500000\nuser_usec 1\n")
    (base / "cgroup.procs").write_text("1\n2\n3\n")
    (base / "memory.current").write_text(str(200 << 20))
    assert metrics.cgroup_usage(base) == (2.5, 200 << 20, 3)


async def test_a_compose_instance_measured(tmp_path, monkeypatch):
    data = {"type": "docker", "test": {"restart": {"type": "compose", "service": str(tmp_path)}, "repositories": [],
                                       "database": "main"}}
    servers = parse_servers({"s": data})
    settings = type("S", (), {"workspaces": {}, "all_instances": lambda self: [servers["s"].test]})()
    sampler = metrics.Sampler(settings)

    async def capture(cmd, **kwargs):
        return 0, "\n".join(json.dumps(r) for r in ({"ID": "aaaaaaaaaaaa", "State": "running"},
                                                     {"ID": "bbbbbbbbbbbb", "State": "exited"}))
    monkeypatch.setattr(ops, "capture", capture)
    monkeypatch.setattr(metrics, "container_cgroups", lambda proc=None: {"a" * 64: "/x/docker-a.scope",
                                                                         "b" * 64: "/x/docker-b.scope"})
    used = {"cpu": 1.0}
    monkeypatch.setattr(metrics, "cgroup_usage", lambda base: (used["cpu"], 300 << 20, 4) if "docker-a" in str(base)
                        else (99.0, 1, 1))
    monkeypatch.setattr(metrics.Sampler, "sample_machine", lambda self: None)
    await sampler.sample()
    used["cpu"] = 1.5
    await sampler.sample()
    point = sampler.series(servers["s"].test.key)[-1]
    # only the running container, its CPU between the two samples
    assert point["mem"] == 300.0 and point["procs"] == 4 and point["cpu"] is not None and point["cpu"] > 0
    assert "containers" in sampler.source[servers["s"].test.key]


def test_how_it_is_measured_is_said_in_the_users_language():
    """The source of an instance's load, shown under its charts, is translated
    (it was the one English line of the Monitoring tab)."""
    from omniacli import i18n

    assert i18n.tr("it", "the containers of /srv/acme/odoo/production") == "i container di /srv/acme/odoo/production"
    assert i18n.tr("it", "systemd unit odoo-prod") == "unità systemd odoo-prod"
    assert i18n.tr("it", "processes started with -c /etc/odoo.conf") == "processi avviati con -c /etc/odoo.conf"
    assert i18n.tr("en", "systemd unit odoo-prod") == "systemd unit odoo-prod"
