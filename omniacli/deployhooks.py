# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deploy on push: an instance's address that GitHub, GitLab or Bitbucket
(or any CI) calls when a branch is pushed; the instance then pulls that
repository, updates the changed modules and restarts, as Git pull and
restart does.

Each instance that has one has its own secret, in deploy-hooks.json next to
the servers file (mode 600): GitHub signs the body with it
(X-Hub-Signature-256, HMAC-SHA256), GitLab sends it (X-Gitlab-Token), any
other caller sends it as X-OmniaCLI-Token. A call without it, or for a
branch or a repository the instance does not follow, changes nothing.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
from pathlib import Path

FILE = "deploy-hooks.json"
MAX_BODY = 5 << 20
_HOST_PATH = re.compile(r"(?:https?://(?:[^/@]*@)?|ssh://(?:[^@/]*@)?|git@)([^/:]+)(?::\d+)?[:/](.+?)(?:\.git)?/?$")


def load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


def new_secret() -> str:
    return secrets.token_urlsafe(24)


def verified(headers: dict, body: bytes, secret: str) -> bool:
    """Whether the call carries the instance's secret: GitHub's signature,
    GitLab's token, or ours."""
    if not secret:
        return False
    signature = headers.get("x-hub-signature-256", "")
    if signature.startswith("sha256="):
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature[len("sha256="):], expected)
    for name in ("x-gitlab-token", "x-omniacli-token"):
        if headers.get(name):
            return hmac.compare_digest(headers[name], secret)
    return False


def host_path(url: str) -> str | None:
    """github.com/acme/x of any address of a repository (https, ssh, git@)."""
    found = _HOST_PATH.match((url or "").strip())
    return f"{found[1].lower()}/{found[2].lower()}" if found else None


def pushed(body: bytes) -> tuple[str | None, set[str]]:
    """The branch a push event names and the addresses of its repository
    (host/path), from GitHub's, GitLab's or Bitbucket's body; (None, ...)
    when it is not a push of a branch (a ping, a tag)."""
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        return None, set()
    if not isinstance(data, dict):
        return None, set()
    ref = str(data.get("ref") or "")
    branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else None
    urls = set()
    repo = data.get("repository") if isinstance(data.get("repository"), dict) else {}
    project = data.get("project") if isinstance(data.get("project"), dict) else {}
    for value in (repo.get("clone_url"), repo.get("ssh_url"), repo.get("html_url"), repo.get("git_http_url"),
                  repo.get("git_ssh_url"), repo.get("url"), repo.get("homepage"), project.get("git_http_url"),
                  project.get("git_ssh_url"), project.get("web_url")):
        found = host_path(str(value or ""))
        if found:
            urls.add(found)
    # Bitbucket: push.changes[].new.name, repository.links.html.href
    push = data.get("push") if isinstance(data.get("push"), dict) else {}
    for change in push.get("changes") or []:
        new = (change or {}).get("new") or {}
        if new.get("type") == "branch" and branch is None:
            branch = str(new.get("name") or "") or None
    href = (((repo.get("links") or {}).get("html") or {}).get("href")) if isinstance(repo.get("links"), dict) else None
    if href and host_path(href):
        urls.add(host_path(href))
    return branch, urls


def head_author(body: bytes) -> str:
    """The author's name of the commit at the head of a push (GitHub's
    head_commit, GitLab's last commit, Bitbucket's target); "" when the
    body does not say."""
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    head = data.get("head_commit") if isinstance(data.get("head_commit"), dict) else None
    commits = [c for c in data.get("commits") or [] if isinstance(c, dict)]
    if head is None and commits:
        sha = str(data.get("after") or data.get("checkout_sha") or "")
        head = next((c for c in commits if c.get("id") == sha), commits[-1])
    found = (head or {}).get("author")
    push = data.get("push") if isinstance(data.get("push"), dict) else {}
    for change in push.get("changes") or []:
        target = ((change or {}).get("new") or {}).get("target") or {}
        if isinstance(target, dict) and isinstance(target.get("author"), dict):
            user = target["author"].get("user") if isinstance(target["author"].get("user"), dict) else {}
            found = {"name": user.get("display_name") or str(target["author"].get("raw") or "").split(" <")[0]}
    name = found.get("name") if isinstance(found, dict) else ""
    return str(name or "").strip().split("\n", 1)[0][:120]


def head_commit(body: bytes) -> tuple[str, str]:
    """(sha, first line of its message) of the commit a push brought to the
    head of its branch (GitHub's after and head_commit, GitLab's
    checkout_sha and commits, Bitbucket's push.changes[].new.target); ("",
    "") when the body does not say."""
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        return "", ""
    if not isinstance(data, dict):
        return "", ""
    sha, message = str(data.get("after") or data.get("checkout_sha") or ""), ""
    head = data.get("head_commit") if isinstance(data.get("head_commit"), dict) else None
    commits = [c for c in data.get("commits") or [] if isinstance(c, dict)]
    if head is None and commits:
        head = next((c for c in commits if c.get("id") == sha), commits[-1])
    if head is not None:
        sha = sha or str(head.get("id") or "")
        message = str(head.get("message") or "")
    push = data.get("push") if isinstance(data.get("push"), dict) else {}
    for change in push.get("changes") or []:
        target = ((change or {}).get("new") or {}).get("target") or {}
        if isinstance(target, dict) and target.get("hash"):
            sha, message = str(target["hash"]), str(target.get("message") or "")
    sha = sha if re.fullmatch(r"[0-9a-f]{7,64}", sha) and set(sha) != {"0"} else ""
    return sha, message.strip().split("\n", 1)[0][:200]
