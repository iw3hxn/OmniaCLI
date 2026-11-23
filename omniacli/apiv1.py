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
    "with the job to follow. Authenticate with `Authorization: Bearer <token>`: a token made on Settings › Users › "
    "API and AI assistants (or on a workspace's page), the same token as for MCP at /mcp. Its level is its role: "
    "`user` reads; `developer` also acts on the tests and the stagings; `admin` also acts on a production, each "
    "call with `confirm` = the server's name. A token marked CI never acts on a production: a production is "
    "updated by hand only. Every write is in the access log with the token's name. At most 600 reads and 60 "
    "writes a minute per token (429 with Retry-After). A POST with an `Idempotency-Key` header answered once "
    "answers the same (the same job) for 24 hours; the same key with another body is a 409. Errors are "
    '`{"error": {"code": "...", "message": "..."}}` with a stable code.')

# what each action of /api/v1 is as an MCP tool (the same rules): a test fails when one is missing
V1_TOOLS = {
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
                                  "production_level_required, rate_limited, idempotency_conflict, "
                                  "invalid_idempotency_key, failed")
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


class DeployRow(BaseModel):
    id: Optional[str] = Field(None, description="The deploy's id: give it to deploys/{id}/back")
    kind: str = Field(description="deploy, or available (a commit a push told of a production, not deployed)")
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
