# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Command line: serve the web application, hash passwords, check a config,
sign the Debian packages instances install."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .accesslog import AccessLog
from .auth import ROLES, hash_password, hash_token, new_token
from . import centralbackup
from .centralbackup import CentralBackupError
from .config import ConfigError, load_settings
from .packagesign import SigningError, load_private_key, new_private_key, sign_file, verify_local


# who may say the client's address and scheme (X-Forwarded-For, -Proto):
# the nginx of this machine (the customers' portal, the workspaces' sites),
# never a client, nor the FORWARDED_ALLOW_IPS of an environment
TRUSTED_PROXIES = "127.0.0.1,::1"


def _serve(args) -> int:
    import uvicorn

    os.environ["OMNIACLI_CONFIG"] = args.config
    load_settings(args.config)  # fail here, not inside uvicorn
    uvicorn.run(
        "omniacli.app:app_from_env",
        factory=True,
        host=args.host,
        port=args.port,
        proxy_headers=True,
        forwarded_allow_ips=TRUSTED_PROXIES,
    )
    return 0


def _hash_password(args) -> int:
    first = getpass.getpass("Password: ")
    if not first or first != getpass.getpass("Repeat: "):
        print("Passwords are empty or do not match", file=sys.stderr)
        return 1
    print(hash_password(first))
    return 0


def _new_token(args) -> int:
    token = new_token()
    print(f"Token for {args.name} (give it to the MCP client, it is not shown again):")
    print(f"  {token}")
    print("Add to auth.tokens in the main configuration:")
    print(f"  {args.name}: {{token: \"{hash_token(token)}\", role: {args.role}}}")
    return 0


def _new_link_token(args) -> int:
    token = new_token()
    print(f"Token for the linked instance {args.name} (enter it in its link, it is not shown again):")
    print(f"  {token}")
    print("Add to auth.links in the main configuration:")
    print(f"  {args.name}: {{token: \"{hash_token(token)}\", role: {args.role}}}")
    return 0


def _check_config(args) -> int:
    from . import checks

    settings = load_settings(args.config)
    failed = False
    for server in settings.servers.values():
        report = asyncio.run(checks.check_server(server, settings.backup_dir, settings.machine))
        ok = checks.passed(report)
        print(f"{server.name} ({server.status}): {'OK' if ok else 'PROBLEMS'}")
        for check in report:
            mark = "ok  " if check.ok else ("warn" if check.level == checks.WARNING else "FAIL")
            print(f"  [{mark}] {check.target:<10} {check.name}: {check.detail}")
        failed = failed or not ok
    return 1 if failed else 0


def _verify_access_log(args) -> int:
    """The chain of the access log (accesslog.py): 0 intact, 1 broken."""
    folder = Path(args.logs_dir) if args.logs_dir else load_settings(args.config).logs_dir
    found = AccessLog(folder / "access.jsonl").verify()
    for broken in found["breaks"]:
        print(f"{broken['file']}:{broken['line']}: {broken['reason']}")
    print(f"{found['entries']} entries in {folder}: " + ("the chain is intact" if found["ok"] else
                                                          f"the chain breaks in {len(found['breaks'])} places"))
    return 0 if found["ok"] else 1


def _passphrase(prompt: str, repeat: bool) -> bytes:
    first = os.environ.get("OMNIACLI_KEY_PASSPHRASE") or getpass.getpass(prompt)
    if repeat and "OMNIACLI_KEY_PASSPHRASE" not in os.environ and first != getpass.getpass("Repeat: "):
        raise SigningError("the passphrases do not match")
    return first.encode()


def _package_key(args) -> int:
    """A new signing key: the private one, encrypted, to args.private_key
    (never overwritten), the public one printed for the instances."""
    path = Path(args.private_key)
    if path.exists():
        print(f"{path} exists: not overwritten", file=sys.stderr)
        return 1
    pem, public = new_private_key(_passphrase("Passphrase of the new key: ", repeat=True))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(pem)
    print(f"private key: {path} (keep it on the build machine, and a copy offline)", file=sys.stderr)
    print(public)
    return 0


def _deb_field(path: Path, name: str) -> str:
    try:
        return subprocess.run(["dpkg-deb", "-f", str(path), name], capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SigningError(f"{path}: dpkg-deb cannot read its {name}: {exc}") from None


def _package_sign(args) -> int:
    """The signed manifest of each file: a .deb's version and name from
    itself, any other file's version from --version."""
    files = [Path(f) for f in args.files]
    for path in files:
        if not path.is_file():
            raise SigningError(f"{path}: no such file")
        if path.suffix != ".deb" and not args.version:
            raise SigningError(f"{path}: not a .deb, give its version with --version")
    key = load_private_key(Path(args.private_key).read_bytes(), _passphrase("Passphrase: ", repeat=False))
    for path in files:
        deb = path.suffix == ".deb"
        version = args.version or _deb_field(path, "Version")
        for written in sign_file(key, path, version, _deb_field(path, "Package") if deb else None):
            print(written)
    return 0


def _package_verify(args) -> int:
    """A downloaded file against the signed manifest next to it."""
    data = verify_local(args.public_key, Path(args.file))
    print(f"{data['file']}: version {data['version']}, built {data.get('built')}, "
          f"sha256 {data['sha256']}: signed by the key, the file is the original")
    return 0


def _plugin_install(args) -> int:
    """As root (sudo, postinst): a downloaded plugin into this venv, checked
    with the key of the main configuration (pluginstore.py)."""
    from . import pluginstore

    if args.all:
        for line in pluginstore.install_all():
            print(f"omniacli: {line}")
        return 0
    if not args.name:
        print("plugin-install: name a plugin, or --all", file=sys.stderr)
        return 2
    try:
        print(f"installed {pluginstore.install(args.name)}")
    except pluginstore.StoreError as exc:
        print(f"plugin-install: {exc}", file=sys.stderr)
        return 1
    return 0


def _template_pack(args) -> int:
    """The archive of a template, checked, into a folder: what the release
    script publishes (templatestore.py). Its index entry, as JSON."""
    import json

    from . import stacktemplates

    try:
        print(json.dumps(stacktemplates.pack(Path(args.folder), Path(args.dest))))
    except stacktemplates.TemplateError as exc:
        print(f"template-pack: {exc}", file=sys.stderr)
        return 1
    return 0


def _compose_helper(args) -> int:
    """As root (sudo): an action on a compose project, which must be safe
    from the user who called sudo (composehelper.py)."""
    from . import composehelper

    return composehelper.main(args.args)


def _workspace_helper(args) -> int:
    """As root (sudo): a workspace's disk and instances (workspacehelper.py)."""
    from . import workspacehelper

    return workspacehelper.main(args.args)


def _self_update(args) -> int:
    """As root (sudo): OmniaCLI's own package downloaded, verified and
    installed in a unit of its own (selfupdate.py)."""
    from . import selfupdate

    return selfupdate.helper_main(args.args)


SYSTEM_CONFIG = "/etc/omniacli/omniacli.yaml"
SERVICE = "omniacli"


def _restore_passphrase(args) -> str:
    """The recovery passphrase: from a file only its owner reads (--passphrase-file), else asked on the terminal;
    never on the command line nor in the environment, where other users or the shell's history would see it."""
    if args.passphrase_file:
        path = Path(args.passphrase_file)
        if path.stat().st_mode & 0o077:
            raise CentralBackupError(f"{path} is readable by others: chmod 600 it first")
        return path.read_text(encoding="utf-8").strip()
    return getpass.getpass("Recovery passphrase of the backup: ")


def _systemctl(action: str) -> None:
    subprocess.run(["systemctl", action, SERVICE], check=False, capture_output=True)


def _central_restore(args) -> int:
    """A central panel's state put back on a fresh install from its sealed backup (centralbackup.py)."""
    config = Path(args.config or (SYSTEM_CONFIG if Path(SYSTEM_CONFIG).exists() else "omniacli.yaml"))
    settings = load_settings(config)
    backup = Path(args.file)
    header, _, _ = centralbackup.read_header(backup)
    print(f"{backup.name}: the backup of {header.get('host') or '?'} made {header.get('made') or '?'} by OmniaCLI "
          f"{header.get('version') or '?'}, key {centralbackup.fingerprint(header['key'])}")
    passphrase = _restore_passphrase(args)
    started = time.monotonic()
    if args.dry_run:
        with tempfile.TemporaryDirectory(prefix="omniacli-central-check-") as tmp:
            centralbackup.open_sealed(backup, passphrase, Path(tmp) / "state.tar.gz")
        print(f"it opens with this passphrase and is intact ({time.monotonic() - started:.1f} s): nothing restored")
        return 0
    root = os.geteuid() == 0
    owner = None
    if root:
        data_dir = settings.servers_file.parent
        owner = (data_dir.stat().st_uid, data_dir.stat().st_gid)
    service = root and not args.no_restart and shutil.which("systemctl") is not None
    found = centralbackup.restore(backup, passphrase, settings, config, replace=args.replace,
                                  main_config=args.main_config, owner=owner,
                                  stop=(lambda: _systemctl("stop")) if service else (lambda: None),
                                  start=(lambda: _systemctl("start")) if service else (lambda: None))
    print(f"restored in {time.monotonic() - started:.1f} s: {', '.join(found['restored'])}")
    print(f"links: {', '.join(found['links']) or 'none'}" + (
        f" (in place of {', '.join(found['replaced'])})" if found["replaced"] else ""))
    if found["access"]:
        print(f"the access log of the backup: {found['access']} (omniacli verify-access-log --logs-dir {found['access']})")
    if found["config_beside"]:
        print(f"the main configuration of the backup: {found['config_beside']}, beside {config}: compare them (diff) "
              "and copy what this machine needs; the admin of this install logs in as before")
    elif found["main_config"]:
        print(f"the main configuration of the backup is {config} now")
    print("OmniaCLI restarted: log in, then each link opens its machine as before" if service else
          "start OmniaCLI again, then log in: each link opens its machine as before")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="omniacli")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the web application")
    serve.add_argument("-c", "--config", default="omniacli.yaml")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8090)
    serve.set_defaults(func=_serve)

    hashpw = sub.add_parser("hash-password", help="hash a password for auth.users")
    hashpw.set_defaults(func=_hash_password)

    token = sub.add_parser("new-token", help="create a bearer token for an MCP client (auth.tokens)")
    token.add_argument("name", help="who uses it, e.g. dev-laptop")
    token.add_argument("--role", choices=ROLES, default="developer",
                       help="user: reads only; developer: also update_test and backup_test; "
                            "admin: also adds draft servers")
    token.set_defaults(func=_new_token)

    link = sub.add_parser("new-link-token",
                          help="create a token for another OmniaCLI instance linked to this one (auth.links)")
    link.add_argument("name", help="who links in, e.g. omniasolutions")
    link.add_argument("--role", choices=ROLES, default="user",
                      help="user: monitors only; developer: acts on the servers; admin: everything")
    link.set_defaults(func=_new_link_token)

    key = sub.add_parser("package-key", help="create the key that signs published Debian packages")
    key.add_argument("private_key", help="where the private key goes (encrypted with a passphrase)")
    key.set_defaults(func=_package_key)

    sign = sub.add_parser("package-sign",
                          help="write next to each file its signed manifest (.json, .json.sig) and .sha256")
    sign.add_argument("--key", dest="private_key", required=True, help="the private key of package-key")
    sign.add_argument("--version", help="the version of files that are not .deb (a .deb gives its own)")
    sign.add_argument("files", nargs="+")
    sign.set_defaults(func=_package_sign)

    verify = sub.add_parser("package-verify",
                            help="check a downloaded file against its signed manifest (.json, .json.sig next to it)")
    verify.add_argument("--public-key", required=True, help="ed25519:... as package-key printed it")
    verify.add_argument("file")
    verify.set_defaults(func=_package_verify)

    plugin = sub.add_parser("plugin-install",
                            help="as root: install a downloaded plugin, checked with plugins.public_key "
                                 "of /etc/omniacli/omniacli.yaml")
    plugin.add_argument("name", nargs="?", help="the plugin, as the Plugins page downloaded it")
    plugin.add_argument("--all", action="store_true", help="every plugin downloaded (after a new venv)")
    plugin.set_defaults(func=_plugin_install)

    helper = sub.add_parser("compose-helper",
                            help="as root: an action on a Docker Compose project that the caller "
                                 "cannot change (start, stop, restart, state)")
    helper.add_argument("args", nargs=argparse.REMAINDER, help="<project folder> <action> [arguments]")
    helper.set_defaults(func=_compose_helper)

    pack = sub.add_parser("template-pack",
                          help="the archive of a workspace template, checked, for the download server")
    pack.add_argument("folder", help="the template's folder (templates/<name>)")
    pack.add_argument("dest", help="the folder the archive is written into")
    pack.set_defaults(func=_template_pack)

    workspace = sub.add_parser("workspace-helper",
                               help="as root: a workspace's virtual disk, and its instances made from a "
                                    "template or removed (disk, create, remove)")
    workspace.add_argument("args", nargs=argparse.REMAINDER, help="<action> [arguments]")
    workspace.set_defaults(func=_workspace_helper)

    update = sub.add_parser("self-update",
                            help="as root: download and verify OmniaCLI's own package from self_update.url of "
                                 "/etc/omniacli/omniacli.yaml and install it in a unit of its own "
                                 "(check, prepare, start update|back <job>)")
    update.add_argument("args", nargs=argparse.REMAINDER, help="<action> [arguments]")
    update.set_defaults(func=_self_update)

    check = sub.add_parser("check-config", help="run the Test & activate checks on every server")
    check.add_argument("-c", "--config", default="omniacli.yaml")
    check.set_defaults(func=_check_config)

    verify_log = sub.add_parser("verify-access-log",
                                help="check that no entry of the access log was changed, removed or added (its hash chain)")
    verify_log.add_argument("-c", "--config", default="omniacli.yaml")
    verify_log.add_argument("--logs-dir", help="the folder of access.jsonl, instead of the configuration's")
    verify_log.set_defaults(func=_verify_access_log)

    restore = sub.add_parser("central-restore",
                             help="as root, on a fresh install: put back a central panel's state from its sealed "
                                  "backup (Settings › Protection of this panel), asking its recovery passphrase")
    restore.add_argument("file", help="the backup, central_<date>.occb, brought from its destination")
    restore.add_argument("-c", "--config", help=f"the main configuration (default {SYSTEM_CONFIG})")
    restore.add_argument("--passphrase-file", help="read the passphrase from this file (mode 600) instead of "
                                                   "asking it")
    restore.add_argument("--dry-run", action="store_true", help="only check that it opens and is intact")
    restore.add_argument("--replace", action="store_true",
                         help="on a panel that has links already: put the backup's in their place")
    restore.add_argument("--main-config", action="store_true",
                         help="put the backup's main configuration in place of this one (default: beside it, "
                              ".from-backup)")
    restore.add_argument("--no-restart", action="store_true", help="do not stop and start the service")
    restore.set_defaults(func=_central_restore)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except SigningError as exc:
        print(f"Signing error: {exc}", file=sys.stderr)
        return 2
    except CentralBackupError as exc:
        print(f"Not restored: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
