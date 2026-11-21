# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""An administrator stops a job that hangs or loops."""
import asyncio
import os

from omniacli import ops
from omniacli.jobs import STOPPED, SUCCESS, Job, JobManager, duration_text

from .test_app import AUTH, HOST, POST, basic, client, wait_job  # noqa: F401


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # a zombie is dead: only its parent has not reaped it yet
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return False


async def until(condition, seconds=10):
    for _ in range(int(seconds * 20)):
        if condition():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not reached")


async def test_stop_a_looping_job_and_its_children(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, "KILL_WAIT", 1)
    jobs = JobManager(tmp_path / "jobs")
    pids = tmp_path / "pids"
    # a loop that ignores SIGTERM, with a child that ignores it too
    script = f'trap "" TERM; sleep 1000 & echo $$ $! > {pids}; while :; do sleep 1; done'
    looping = jobs.submit("loop", ["s/test"], "test", lambda job: ops.run(job, ["sh", "-c", script]))
    after = jobs.submit("after", ["s/test"], "test", lambda job: ops.run(job, ["true"]))
    await until(lambda: pids.exists() and pids.read_text().strip())
    shell, child = map(int, pids.read_text().split())
    assert jobs.is_busy("s/test") and after.status == "queued"

    assert jobs.stop(looping.id, "admin")
    await until(lambda: looping.finished_flag)
    assert looping.status == STOPPED and looping.error == "stopped by admin"
    assert any("stop asked by admin" in line for line in looping.lines)
    assert any("STOPPED by admin" in line for line in looping.lines)
    # the whole group, though it ignores SIGTERM: SIGKILL after KILL_WAIT
    await until(lambda: not alive(shell) and not alive(child))
    # the instance is free again: the next job runs
    await until(lambda: after.finished_flag)
    assert after.status == SUCCESS
    assert not jobs.stop(looping.id, "admin")  # finished: nothing to stop


async def test_stop_a_queued_job(tmp_path):
    jobs = JobManager(tmp_path / "jobs")
    first = jobs.submit("first", ["s/test"], "test", lambda job: ops.run(job, ["sleep", "30"]))
    queued = jobs.submit("queued", ["s/test", "s/production"], "test", lambda job: ops.run(job, ["true"]))
    await until(lambda: first.status == "running")
    assert jobs.stop(queued.id, "admin")
    await until(lambda: queued.finished_flag)
    assert queued.status == STOPPED and queued.started is None
    assert first.status == "running" and not jobs.is_busy("s/production")
    assert jobs.stop(first.id, "admin")
    await until(lambda: first.finished_flag)
    assert not jobs.is_busy("s/test")


async def test_only_an_admin_stops_a_job(client, env):
    settings = client.app.state.settings
    jobs = client.app.state.jobs
    inst = settings.instance("srv", "test")
    job = jobs.submit("Hangs", [inst.key], "test", lambda job: ops.run(job, ["sleep", "30"]))
    await until(lambda: job.status == "running")

    page = (await client.get(f"/jobs/{job.id}", headers={"Authorization": AUTH})).text
    assert f'action="/jobs/{job.id}/stop"' in page
    dev = {"Authorization": basic("dev"), "Origin": f"http://{HOST}"}
    assert "/stop" not in (await client.get(f"/jobs/{job.id}", headers=dev)).text
    assert (await client.post(f"/jobs/{job.id}/stop", headers=dev)).status_code == 403
    assert (await client.post("/jobs/nope/stop", headers=POST)).status_code == 404

    resp = await client.post(f"/jobs/{job.id}/stop", headers=POST)
    assert resp.status_code == 303 and resp.headers["location"] == f"/jobs/{job.id}"
    data = await wait_job(client, job.id)
    assert data["status"] == "stopped" and data["error"] == "stopped by admin"
    assert (await client.post(f"/jobs/{job.id}/stop", headers=POST)).status_code == 409
    assert f'action="/jobs/{job.id}/stop"' not in (await client.get(f"/jobs/{job.id}", headers={"Authorization": AUTH})).text


def test_duration():
    assert duration_text(12.7) == "12 s"
    assert duration_text(185) == "3 min 05 s"
    assert duration_text(3720) == "1 h 02 min"
    job = Job(id="x", title="t", instances=[], source="s")
    assert job.duration == ""
    job.started, job.finished = "2026-10-25T10:00:00", "2026-10-25T10:01:30"
    assert job.duration == "1 min 30 s"


async def test_the_workspace_of_a_job_is_kept_and_an_old_file_still_reads(tmp_path):
    """A workspace's job keeps its workspace on disk; the others are written
    as before, without the key (an older OmniaCLI still reads them)."""
    import json as _json

    from omniacli.jobs import JobManager

    manager = JobManager(tmp_path)

    async def action(job):
        job.log("done")
    one = manager.submit("Create acme-main", ["acme-main/production"], "manual", action, workspace="acme")
    two = manager.submit("Pull", ["srv/production"], "manual", action)
    await asyncio.gather(*manager._tasks)
    assert _json.loads((tmp_path / f"{one.id}.json").read_text())["workspace"] == "acme"
    assert "workspace" not in _json.loads((tmp_path / f"{two.id}.json").read_text())
    again = JobManager(tmp_path)
    assert again.get(one.id).workspace == "acme" and again.get(two.id).workspace == ""


def test_the_warnings_of_a_certificate_not_obtained():
    """Confronto 21/11/2026 §5.5: the web helper's result as warnings, one
    sentence each: the names asked and why the certificate did not come."""
    from omniacli.workspaceops import certificate_warnings

    assert certificate_warnings({"names": ["acme.it"], "certificate_error": None}) == []
    made = {"names": ["acme.it", "www.acme.it"], "without_dns": ["www.acme.it"], "elsewhere": [],
            "certificate_error": "Certbot failed: DNS problem: NXDOMAIN looking up A for acme.it"}
    assert certificate_warnings(made) == [
        "The certificate for acme.it did not come: the DNS does not point here",
        "Left out of the certificate, on http only: www.acme.it: the DNS does not point here"]
    made = {"names": ["www.acme.it"], "without_dns": ["www.acme.it"], "certificate_error": "no name of the workspace is in DNS yet"}
    assert certificate_warnings(made) == ["The certificate for www.acme.it did not come: the DNS does not point here"]
    assert certificate_warnings({"names": ["acme.it"], "elsewhere": ["old.acme.it"]}) == [
        "Left out of the certificate, on http only: old.acme.it: the DNS does not point here"]


async def test_a_job_that_succeeds_with_warnings(client):
    """The job's main work succeeded, something did not: success with
    warnings, in amber, its warnings said in the reader's language on its
    page, in the list and in the API for the page (said=1); the status stays
    success for an older OmniaCLI and for the API's own readers."""
    jobs = client.app.state.jobs
    inst = client.app.state.settings.instance("srv", "test")

    async def action(job):
        job.log("--- the firewall")
        job.warn("The certificate for www.acme.it did not come: the DNS does not point here")
    job = jobs.submit("Names of workspace acme", [inst.key], "test", action)
    data = await wait_job(client, job.id)
    assert data["status"] == "success" and data["outcome"] == "warned"
    assert data["result"]["warnings"] == ["The certificate for www.acme.it did not come: the DNS does not point here"]
    italian = {"Authorization": AUTH, "Accept-Language": "it"}
    said = (await client.get(f"/api/jobs/{job.id}?said=1", headers=italian)).json()
    assert said["warnings"] == ["Il certificato per www.acme.it non è arrivato: il DNS non punta qui"]
    assert said["lines"][0].endswith(" --- il firewall") and said["lines"][1].split(" ", 1)[1].startswith("ATTENZIONE: Il certificato")
    # the API's own lines stay the code's
    assert (await client.get(f"/api/jobs/{job.id}", headers=italian)).json()["lines"][0].endswith("--- the firewall")
    page = (await client.get(f"/jobs/{job.id}", headers=italian)).text
    assert 'data-status="warned"' in page and "riuscito con avvisi" in page
    assert "<li>Il certificato per www.acme.it non è arrivato: il DNS non punta qui</li>" in page
    listed = (await client.get("/jobs", headers=italian)).text
    assert 'class="status warned job-status" data-status="success"' in listed and "il DNS non punta qui" in listed
    # the Last action line of the instance
    page = (await client.get("/servers/srv?view=test", headers=italian)).text
    assert '<span class="status warned">riuscito con avvisi</span>' in page
