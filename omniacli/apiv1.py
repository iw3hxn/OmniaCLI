# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The REST API for scripts and CI (README, REST API): the shapes of its
answers and bodies, with their descriptions for the OpenAPI document; the
list of its routes and of the MCP tools that do the same (V1_TOOLS: a v1
action without its MCP tool fails tests/test_api_v1.py). The routes are in
app.py, a thin layer over what the pages and MCP call; the rules they share
with MCP are in apiguard.py."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

PREFIX = "/api/v1"
TITLE = "OmniaCLI REST API"
VERSION = "1"
DESCRIPTION = (
    "A small, stable API for scripts and CI: the instances and their state, the jobs and their logs, the backups, "
    "and the actions (start, stop, restart, update the code, go back to a deploy, make a staging), each answered "
    "with the job to follow; for the machine's admin tokens, the handover of a workspace to a customer's OmniaCLI "
    "(prepare it here, receive it and switch it there) and the move of an instance to another machine and its "
    "return, each step with `confirm` = the name, never a CI token's. Authenticate with "
    "`Authorization: Bearer <token>`: a token made on Settings › Users › API and AI assistants (or on a "
    "workspace's page), the same token as for MCP at /mcp. Its level is its role: "
    "`user` reads; `developer` also acts on the tests and the stagings; `admin` also acts on a production, each "
    "call with `confirm` = the server's name. A token marked CI never acts on a production: a production is "
    "updated by hand only. Every write is in the access log with the token's name. At most 600 reads and 60 "
    "writes a minute per token (429 with Retry-After). A POST with an `Idempotency-Key` header answered once "
    "answers the same (the same job) for 24 hours; the same key with another body is a 409. Errors are "
    '`{"error": {"code": "...", "message": "..."}}` with a stable code.')

# what each action of /api/v1 is as an MCP tool (the same rules): a test fails when one is missing
V1_WORKSPACE_TOOLS = {
    ("GET", "/api/v1/instances"): ("list_servers",),
    ("GET", "/api/v1/instances/{server}/{role}"): ("server_status",),
    ("GET", "/api/v1/jobs"): ("list_jobs",),
    ("GET", "/api/v1/jobs/{job_id}"): ("get_job",),
    ("GET", "/api/v1/jobs/{job_id}/log"): ("job_log",),
    ("GET", "/api/v1/instances/{server}/{role}/backups"): ("list_backups",),
    ("POST", "/api/v1/instances/{server}/{role}/backups"): ("backup", "backup_test"),
    ("GET", "/api/v1/instances/{server}/{role}/backups/{file}"): ("list_backups",),
    ("POST", "/api/v1/instances/{server}/{role}/start"): ("start_instance",),
    ("POST", "/api/v1/instances/{server}/{role}/stop"): ("stop_instance",),
    ("POST", "/api/v1/instances/{server}/{role}/restart"): ("restart_instance",),
    ("POST", "/api/v1/instances/{server}/{role}/pull"): ("update_test", "update_production"),
    ("GET", "/api/v1/instances/{server}/{role}/deploys"): ("list_deploys",),
    ("POST", "/api/v1/instances/{server}/{role}/deploys/{deploy_id}/back"): ("deploy_back",),
    ("POST", "/api/v1/instances/{server}/stagings"): ("create_staging",),
}
# the handover of a workspace to a customer's OmniaCLI and the move of an instance to another machine (AD-008 §9
# step 10, UX-014 A13 and B6): the machine's admin tokens only, never a workspace's (not in WORKSPACE_ROUTES)
V1_MACHINE_TOOLS = {
    ("GET", "/api/v1/workspaces/{workspace}/handover"): ("handover_status",),
    ("POST", "/api/v1/workspaces/{workspace}/handover"): ("prepare_handover",),
    ("POST", "/api/v1/handovers/receive"): ("receive_workspace",),
    ("GET", "/api/v1/handovers/{handover_id}"): ("reception_status",),
    ("POST", "/api/v1/handovers/{handover_id}/switch"): ("switch_workspace",),
    ("GET", "/api/v1/moves/{server}"): ("move_status",),
    ("POST", "/api/v1/moves/{server}"): ("move_instance",),
    ("POST", "/api/v1/moves/{server}/complete"): ("move_instance",),
    ("POST", "/api/v1/moves/{server}/cancel"): ("move_instance",),
    ("POST", "/api/v1/moves/{server}/delete-copy"): ("move_instance",),
    ("POST", "/api/v1/moves/{server}/back"): ("move_back",),
    ("POST", "/api/v1/moves/{server}/back/complete"): ("move_back",),
    ("POST", "/api/v1/moves/{server}/back/cancel"): ("move_back",),
    ("POST", "/api/v1/moves/{server}/back/delete-copy"): ("move_back",),
}
# the protection of this panel (AD-017 slice 7): its nightly sealed backup and its key; the machine's admin tokens
# only, never a workspace's nor a CI's (not in WORKSPACE_ROUTES either)
V1_CENTRAL_TOOLS = {
    ("GET", "/api/v1/central-backup"): ("central_backup_status",),
    ("POST", "/api/v1/central-backup"): ("central_backup_configure",),
    ("POST", "/api/v1/central-backup/key"): ("central_backup_new_key",),
    ("POST", "/api/v1/central-backup/key/confirm"): ("central_backup_confirm_key",),
    ("POST", "/api/v1/central-backup/run"): ("central_backup_run",),
}
# a machine behind NAT managed from a central panel (AD-017 §17.6): its SSH published through a relay, and the
# central's link to it through that relay; the machine's admin tokens only, never a workspace's nor a CI's
V1_RELAY_TOOLS = {
    ("GET", "/api/v1/agent-relay"): ("agent_relay_status",),
    ("POST", "/api/v1/agent-relay"): ("agent_relay_publish",),
    ("POST", "/api/v1/agent-relay/remove"): ("agent_relay_remove",),
    ("POST", "/api/v1/links/{name}/relay"): ("link_relay",),
}
# the identities of a linked machine's workspaces carried to this central panel (AD-018 slice 1): the dry run, the
# import with the same hashes and secrets, its undo; the machine's admin tokens only, never a workspace's nor a CI's
V1_IDENTITY_TOOLS = {
    ("GET", "/api/v1/links/{name}/identities"): ("identity_import_status",),
    ("POST", "/api/v1/links/{name}/identities/import"): ("identity_import",),
    ("POST", "/api/v1/links/{name}/identities/delete"): ("identity_import_delete",),
}
V1_TOOLS = {**V1_WORKSPACE_TOOLS, **V1_MACHINE_TOOLS, **V1_CENTRAL_TOOLS, **V1_RELAY_TOOLS, **V1_IDENTITY_TOOLS}
# the steps of a move and of its return as MCP's move_instance and move_back name them: what follows move/ (or
# move/back/) in the REST path
MOVE_STEPS = {"prepare": "", "complete": "complete", "cancel": "cancel", "delete_copy": "delete-copy"}
# the routes of the document itself
DOCUMENT_ROUTES = {("GET", "/api/v1/openapi.json"), ("GET", "/api/v1/docs")}

# the code of a refusal by its HTTP status, for the refusals the pages' code gives ({"error": "text"})
CODES = {400: "refused", 401: "unauthorized", 403: "forbidden", 404: "not_found", 405: "method_not_allowed",
         409: "conflict", 422: "invalid_request", 429: "rate_limited", 502: "failed", 503: "unavailable"}


def code_of(status: int) -> str:
    return CODES.get(status, "failed" if status >= 500 else "refused")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErrorDetail(BaseModel):
    code: str = Field(description="A stable code for programs: unauthorized, read_only_token, forbidden, "
                                  "not_found, invalid_request, refused, confirm_required, production_by_hand, "
                                  "production_level_required, machine_token_required, rate_limited, "
                                  "idempotency_conflict, invalid_idempotency_key, failed")
    message: str = Field(description="What happened and what to do, in English, for a person")


class ErrorAnswer(BaseModel):
    error: ErrorDetail = Field(description="Why the request was refused")


class Repository(BaseModel):
    name: str = Field(description="The repository's name in the instance")
    branch: str = Field(description="The branch the instance follows")


class InstanceRow(BaseModel):
    server: str = Field(description="The server's name: an instance is a server and a role")
    role: str = Field(description="production or test")
    type: str = Field(description="The kind of service (odoo, docker...)")
    state: str = Field(description="running, stopped or unknown")
    busy: bool = Field(description="A job runs on it now")
    workspace: Optional[str] = Field(None, description="The workspace it belongs to, null for none")
    staging_of: Optional[str] = Field(None, description="For a staging: the server of the production it is a copy of")
    by_hand: bool = Field(description="A production (not a staging): updated by hand only, see confirm")
    version: Optional[str] = Field(None, description="Its Odoo version, when known")
    url: Optional[str] = Field(None, description="The address it answers on, when it has one")
    repositories: List[Repository] = Field(description="Its repositories with their branches")


class InstanceDetail(InstanceRow):
    commits: Dict[str, Optional[str]] = Field(description="The commit checked out in each repository")
    last_jobs: List["JobRow"] = Field(description="Its last five jobs, newest first")


class JobRow(BaseModel):
    id: str = Field(description="The job's id: follow it at /api/v1/jobs/{id}")
    title: str = Field(description="What the job does")
    status: str = Field(description="queued, running, success, failed, stopped or interrupted")
    instances: List[str] = Field(description="The instances it acts on, as server/role")
    source: str = Field(description="Who started it: api: token <name>, mcp: <name>, manual: <user>...")
    created: str = Field(description="When it was asked (ISO 8601, local time)")
    started: Optional[str] = Field(None, description="When it started")
    finished: Optional[str] = Field(None, description="When it ended")
    error: Optional[str] = Field(None, description="Why it failed")
    result: Dict[str, Any] = Field(description="What it produced: a backup's file and download path, "
                                               "a staging's server, warnings")


InstanceDetail.model_rebuild()


class JobLog(BaseModel):
    job: str = Field(description="The job's id")
    status: str = Field(description="The job's status now")
    finished: bool = Field(description="It ended: no more lines will come")
    offset: int = Field(description="The index of the first line given")
    next_offset: int = Field(description="The offset to ask next for the lines that follow")
    lines: List[str] = Field(description="The lines of its log from offset, each with its time")


class Accepted(BaseModel):
    job: JobRow = Field(description="The job started (or, for a repeated Idempotency-Key, the one started then)")
    server: Optional[str] = Field(None, description="For a new staging: the name of its server")
    follow: str = Field(description="Where to follow the job: GET this path")
    log: str = Field(description="Where to read its log from an offset: GET this path")


class BackupRow(BaseModel):
    file: str = Field(description="The backup's file name")
    size: int = Field(description="Its size in bytes")
    time: int = Field(description="When it was made (Unix seconds)")
    kind: str = Field(description="manual, scheduled or pre-update (made before an update)")
    download: str = Field(description="Its download path: GET it with the same token")


class DeployRepository(BaseModel):
    name: Optional[str] = Field(None, description="The repository")
    branch: Optional[str] = Field(None, description="Its branch")
    before: Optional[str] = Field(None, description="The commit before the deploy")
    after: Optional[str] = Field(None, description="The commit after it")
    subject: Optional[str] = Field(None, description="The subject of the commit after it")
    author: Optional[str] = Field(None, description="Who wrote the commit after it (none for a deploy recorded "
                                                    "before OmniaCLI kept it)")


class DeployRow(BaseModel):
    id: Optional[str] = Field(None, description="The deploy's id: give it to deploys/{id}/back")
    kind: str = Field(description="deploy; available (a commit a push told of a production, not deployed); failed "
                                  "(a job of the code that failed or was stopped before moving any commit)")
    started: Optional[str] = Field(None, description="When its job started")
    finished: Optional[str] = Field(None, description="When it ended")
    by: str = Field(description="Who or what started it")
    outcome: Optional[str] = Field(None, description="success, failed...")
    current: bool = Field(description="The code is at this deploy now")
    repositories: List[DeployRepository] = Field(description="Each repository's commits before and after")
    unserved: Optional[Dict[str, Any]] = Field(
        None, description="An instance updated without stopping: the time its users were not served, measured "
                          "(seconds, failed and asked of the asks every 100 ms through nginx, how)")


class Deploys(BaseModel):
    deploys: List[DeployRow] = Field(description="The deploys, newest first (the last 100)")
    ci: bool = Field(description="Its CI is on: a push deploys it")


class ActionBody(_Model):
    confirm: Optional[str] = Field(None, description="On a production only: the server's name, given once the "
                                                     "token's owner agreed to this very action")


class BackupBody(_Model):
    confirm: Optional[str] = Field(None, description="Not needed: a backup does not change the instance")
    options: Dict[str, Any] = Field(default_factory=dict,
                                    description="The backup choices of the instance's kind; Odoo: "
                                                "no_filestore (true: the database only), no_binaries (true: "
                                                "the binary columns emptied), binary_columns "
                                                '(["table.column", ...])')


class DeployBackBody(_Model):
    confirm: Optional[str] = Field(None, description="On a production only: the server's name")
    pause: bool = Field(False, description="Switch the instance's CI off first, so a push does not deploy the "
                                           "branch's head again")


class StagingBody(_Model):
    pass


class ConfirmBody(_Model):
    confirm: Optional[str] = Field(None, description="The name typed, given once the token's owner agreed to this "
                                                     "very action: the instance's server for a move and its return, "
                                                     "the workspace's name for a handover and its switch")


class MoveBody(ConfirmBody):
    link: Optional[str] = Field(None, description="Prepare only: the link of the machine the instance moves to "
                                                  "(GET .../move lists them, with why one cannot hold it)")


class HandoverBody(ConfirmBody):
    instances: List[str] = Field(default_factory=list,
                                 description="The servers of the workspace that pass (GET .../handover lists them, "
                                             "with why one cannot)")


class ReceiveBody(_Model):
    code: str = Field(description="The handover code the provider gave (omniacli-consegna-1.…), in the body: a "
                                  "password, never in a URL")
    dry_run: bool = Field(True, description="true (the default): what arrives and what this machine lacks, read "
                                            "only, nothing changes here or there; false: receive")
    name: Optional[str] = Field(None, description="The workspace's name here, when its own is taken (default: its "
                                                  "own, or <name>-2)")
    disk_gb: Optional[int] = Field(None, description="The workspace's disk here in GB (default: its own)")


class CentralBackupBody(_Model):
    destination: Optional[str] = Field(None, description="A destination of the machine (GET lists them); empty: none")
    time: Optional[str] = Field(None, description="The time of every day it runs, HH:MM in the machine's clock")
    enabled: Optional[bool] = Field(None, description="false: it does not run at night (Run now still does)")


class CentralKeyBody(_Model):
    replace: bool = Field(False, description="true: replace the key there is (the backups made with it open with "
                                             "its passphrase only)")


class CentralConfirmBody(_Model):
    passphrase: str = Field(description="The recovery passphrase the new key's answer gave, typed back (in the body: "
                                        "a password, never in a URL)")


class IdentityConfirmBody(_Model):
    confirm: str = Field(description="The name of the link, typed: the import (or its undo) of that machine's "
                                     "identities")


class AgentRelayBody(_Model):
    relay: str = Field(description="The link of this machine to the relay (a machine with a public address it "
                                   "reaches): this machine's SSH is published on a port of its loopback")


class LinkRelayBody(_Model):
    host: str = Field("", description="The relay's address (its sshd); empty: the link's SSH goes directly again")
    port: int = Field(22, description="The relay's SSH port")
    host_key: Optional[str] = Field(None, description="The relay's host key, \"<type> <base64>\"; without it the "
                                                      "answer gives it (saved: false), to be compared and sent back")
    tunnel_port: Optional[int] = Field(None, description="The port of the relay's loopback the machine's tunnel "
                                                         "listens on (31000-31999: the machine's page says it)")
    machine_host_key: Optional[str] = Field(None, description="The machine's own SSH host key, when it changes "
                                                              "(default: the link's, kept)")
    fetch_machine_key: bool = Field(False, description="true: read the machine's host key through the relay and "
                                                       "give it (saved: false), to be compared and sent back")


class HandoverPrepared(BaseModel):
    code: Optional[str] = Field(None, description="The handover code, given once and kept here as a hash only: for "
                                                  "the customer, sent as a password. null in an answer repeated for "
                                                  "an Idempotency-Key: Redo the code on the page for a new one")
    handover: Dict[str, Any] = Field(description="The handover as its page has it: its phase, expiry, instances")


class Reception(BaseModel):
    id: str = Field(description="The handover's id: follow the reception at /api/v1/handovers/{id}")
    offer: Optional[Dict[str, Any]] = Field(None, description="dry_run=true: what arrives (instances, people, "
                                                              "sizes), what this machine has and lacks, what does "
                                                              "not pass and why")
    job: Optional[JobRow] = Field(None, description="dry_run=false: the job of the reception")
    follow: Optional[str] = Field(None, description="dry_run=false: where to follow the job")
    log: Optional[str] = Field(None, description="dry_run=false: where to read its log from an offset")


def _schema(doc: dict, schema: dict) -> dict:
    """schema with its $ref (and an Optional's anyOf [ref, null]) followed."""
    for _ in range(5):
        if "$ref" in schema:
            schema = doc.get("components", {}).get("schemas", {}).get(schema["$ref"].rsplit("/", 1)[-1], {})
        elif "anyOf" in schema:
            schema = next((s for s in schema["anyOf"] if s.get("type") != "null"), {})
        elif schema.get("type") == "array" and "items" in schema:
            schema = schema["items"]
        else:
            break
    return schema


def _type(doc: dict, schema: dict) -> str:
    if "$ref" in schema:
        return schema["$ref"].rsplit("/", 1)[-1]
    if "anyOf" in schema:
        return " | ".join(_type(doc, s) for s in schema["anyOf"])
    if schema.get("type") == "array":
        return f"list of {_type(doc, schema.get('items') or {})}"
    return str(schema.get("type") or "object")


def _fields(doc: dict, schema: dict) -> list:
    found = _schema(doc, schema)
    required = set(found.get("required") or ())
    return [{"name": name, "type": _type(doc, value), "required": name in required,
             "description": value.get("description") or _schema(doc, value).get("description") or ""}
            for name, value in (found.get("properties") or {}).items()]


def reading(doc: dict) -> list:
    """The routes of the OpenAPI document as the docs page lists them."""
    rows = []
    for path, methods in doc.get("paths", {}).items():
        for method, op in methods.items():
            body = (((op.get("requestBody") or {}).get("content") or {}).get("application/json") or {}).get("schema")
            answers = []
            for code, answer in sorted((op.get("responses") or {}).items()):
                schema = ((answer.get("content") or {}).get("application/json") or {}).get("schema")
                answers.append({"code": code, "description": answer.get("description") or "",
                                "type": _type(doc, schema) if schema else "",
                                "fields": _fields(doc, schema) if schema and str(code).startswith("2") else []})
            rows.append({"method": method.upper(), "path": path, "summary": op.get("summary") or "",
                         "description": op.get("description") or "",
                         "parameters": [{"name": p["name"], "in": p["in"], "required": p.get("required", False),
                                         "type": _type(doc, p.get("schema") or {}),
                                         "description": p.get("description") or ""}
                                        for p in op.get("parameters") or []],
                         "body": _fields(doc, body) if body else [], "responses": answers})
    return rows
