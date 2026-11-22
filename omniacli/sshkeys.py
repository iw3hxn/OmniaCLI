# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The SSH keys of the people who log in to the machine (README, SSH access):
the lines OmniaCLI adds to an account's authorized_keys, and only those.

A line OmniaCLI wrote carries a marker in its comment, after the key:

    ssh-ed25519 AAAA... omniacli-managed by=carlo at=2026-11-22T10:40 name=Mario%27s%20laptop

(by, at and name percent-encoded, name last). Every other line of the file
(OmniaCLI's own provisioning key, a person's key added by hand, a comment,
a line with options) is never changed, moved or taken away: a key is added
at the end, a key OmniaCLI added is taken out where it is. The texts are
made by the functions of the first part; the second part is root's (the
root helper's actions ssh-keys, ssh-key-add, ssh-key-remove,
workspacehelper.ACTIONS): it checks the key with ssh-keygen and writes the
file, owned by its account, mode 600, through a temporary file renamed over
it, never following a link."""

from __future__ import annotations

import base64
import binascii
import datetime
import fcntl
import hashlib
import os
import pwd
import re
import subprocess
import tempfile
import urllib.parse
from pathlib import Path

MARK = "omniacli-managed"
# the kinds of key a person may add; ssh-dss is refused (OpenSSH turned it off)
TYPES = ("ssh-ed25519", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521",
         "sk-ssh-ed25519@openssh.com", "sk-ecdsa-sha2-nistp256@openssh.com", "ssh-rsa")
# every kind a line of authorized_keys may hold, to read the lines of others
KNOWN = (*TYPES, "ssh-dss", "rsa-sha2-256", "rsa-sha2-512")
MIN_RSA_BITS = 2048
# root, or a user of the machine (README, SSH access: which accounts and why)
ACCOUNT = re.compile(r"[a-z_][a-z0-9_.-]{0,31}")
NAME_MAX = 64
COMMENT_MAX = 80
FINGERPRINT = re.compile(r"SHA256:[A-Za-z0-9+/]{43}")
# a user of the machine a key may open: a person's (uid from 1000), with a shell
FIRST_UID, LAST_UID = 1000, 60000
NO_LOGIN = ("nologin", "false", "sync", "halt", "shutdown")
_BLOB = re.compile(r"[A-Za-z0-9+/]+={0,3}")


class KeyRefused(ValueError):
    """A key, a name or an account refused: why, said to the person."""


def fingerprint(blob: str) -> str:
    """SHA256:... of a key as ssh-keygen -l writes it (base64 of the SHA-256
    of the key's bytes, without its =)."""
    raw = base64.b64decode(blob, validate=True)
    return "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")


def _blob_type(blob: str) -> str | None:
    """The kind of key the bytes say (their first string), None: not a key."""
    try:
        raw = base64.b64decode(blob, validate=True)
    except (binascii.Error, ValueError):
        return None
    if len(raw) < 4:
        return None
    size = int.from_bytes(raw[:4], "big")
    if size > 64 or len(raw) < 4 + size:
        return None
    try:
        return raw[4:4 + size].decode("ascii")
    except UnicodeDecodeError:
        return None


def parse_line(line: str) -> dict | None:
    """{type, blob, comment, options} of a line of authorized_keys, None for a
    comment, an empty line or one that holds no key. The options (command=,
    from=, ...) come before the key: the first word that is a kind of key
    starts it."""
    text = line.strip()
    if not text or text.startswith("#"):
        return None
    words = text.split()
    for index, word in enumerate(words[:-1]):
        if word in KNOWN and _BLOB.fullmatch(words[index + 1]) and _blob_type(words[index + 1]) == word:
            head = text.find(word + " " + words[index + 1])
            rest = text[head + len(word) + 1 + len(words[index + 1]):].strip()
            return {"type": word, "blob": words[index + 1], "comment": rest,
                    "options": text[:head].strip() if head > 0 else ""}
    return None


def check_name(name) -> str:
    name = " ".join(str(name or "").split())
    if not name:
        raise KeyRefused("Give the key a name: whose it is, or which computer")
    if len(name) > NAME_MAX or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise KeyRefused(f"The name of a key is at most {NAME_MAX} characters, on one line")
    return name


def check_pasted(text) -> tuple[str, str]:
    """(type, blob) of a public key pasted as its .pub file writes it (type,
    key, comment), or why not: a private key, options, more than one line,
    a kind refused."""
    text = str(text or "").strip()
    if not text:
        raise KeyRefused("Paste the public key: the line of the .pub file, as ssh-ed25519 AAAA… name")
    if "PRIVATE KEY" in text:
        raise KeyRefused("This is a private key: paste the public one, the .pub file. The private key never leaves "
                        "its computer")
    if "\n" in text or "\r" in text:
        raise KeyRefused("Paste one key, on one line: the line of the .pub file")
    words = text.split()
    if words[0] not in KNOWN:
        raise KeyRefused("A public key starts with its kind, as ssh-ed25519 or ssh-rsa: paste the line of the .pub "
                        "file, without options before it")
    if words[0] not in TYPES:
        raise KeyRefused(f"{words[0]} keys are not taken: make an ssh-ed25519 key (ssh-keygen -t ed25519)")
    if len(words) < 2 or not _BLOB.fullmatch(words[1]) or _blob_type(words[1]) != words[0]:
        raise KeyRefused(f"The text after {words[0]} is not a key of that kind: paste the whole line of the .pub file")
    return words[0], words[1]


def check_account(account) -> str:
    account = str(account or "")
    if not ACCOUNT.fullmatch(account):
        raise KeyRefused(f"{account!r} is not the name of an account of the machine")
    return account


def _quote(value: str) -> str:
    return urllib.parse.quote(value, safe="@:._-()")


def marker(name: str, by: str, at: datetime.datetime | None = None) -> str:
    """The comment of a line OmniaCLI writes: its mark, who added it and when
    (local time, to the minute), its name."""
    at = (at or datetime.datetime.now()).strftime("%Y-%m-%dT%H:%M")
    return f"{MARK} by={_quote(by or '?')} at={at} name={_quote(name)}"


def read_marker(comment: str) -> dict | None:
    """{by, at, name} of a comment OmniaCLI wrote, None for any other."""
    found = re.fullmatch(rf"{MARK} by=(\S+) at=(\S+) name=(\S+)", comment.strip())
    if found is None:
        return None
    return {"by": urllib.parse.unquote(found[1]), "at": found[2], "name": urllib.parse.unquote(found[3])}


def key_line(key_type: str, blob: str, name: str, by: str, at: datetime.datetime | None = None) -> str:
    return f"{key_type} {blob} {marker(name, by, at)}"


def listing(text: str) -> tuple[list[dict], list[dict]]:
    """(the keys OmniaCLI added, the others) of an authorized_keys: each with
    its type and fingerprint; ours with name, by, at; the others with their
    comment (cut) and whether they have options."""
    ours, others = [], []
    for line in text.splitlines():
        parsed = parse_line(line)
        if parsed is None:
            continue
        try:
            found = fingerprint(parsed["blob"])
        except (binascii.Error, ValueError):
            continue
        mark = None if parsed["options"] else read_marker(parsed["comment"])
        if mark is not None:
            ours.append({"type": parsed["type"], "fingerprint": found, **mark})
        else:
            comment = parsed["comment"]
            others.append({"type": parsed["type"], "fingerprint": found,
                           "comment": comment[:COMMENT_MAX] + ("…" if len(comment) > COMMENT_MAX else ""),
                           "options": bool(parsed["options"])})
    return ours, others


def fingerprints(text: str) -> set[str]:
    """The fingerprints of every key of the file, ours and the others."""
    ours, others = listing(text)
    return {k["fingerprint"] for k in (*ours, *others)}


def with_line(text: str, line: str) -> str:
    """The file with line at its end; the lines before as they were (a last
    line without its newline gets one)."""
    if text and not text.endswith("\n"):
        text += "\n"
    return text + line + "\n"


def without_key(text: str, found: str) -> tuple[str, int]:
    """The file without the lines OmniaCLI wrote whose key is found (a
    fingerprint), and how many went; every other line kept, in its place,
    as it was (byte for byte: its spaces, its newline)."""
    kept, gone = [], 0
    for line in text.splitlines(keepends=True):
        parsed = parse_line(line)
        if parsed is not None and not parsed["options"] and read_marker(parsed["comment"]) is not None:
            try:
                if fingerprint(parsed["blob"]) == found:
                    gone += 1
                    continue
            except (binascii.Error, ValueError):
                pass
        kept.append(line)
    return "".join(kept), gone


def offered(entries, shells: set[str], service_users: set[str]) -> list[dict]:
    """The accounts a key may open: root, and each user of the machine with a
    uid from FIRST_UID, a login shell of /etc/shells and a home, but the user
    OmniaCLI runs as (a key there would reach its configuration and its
    tokens, around the panel). entries: pwd.getpwall()."""
    found = []
    for entry in entries:
        name, uid, home, shell = entry.pw_name, entry.pw_uid, entry.pw_dir, entry.pw_shell
        if not ACCOUNT.fullmatch(name) or name in service_users:
            continue
        if uid == 0 and name == "root":
            found.append({"account": name, "home": home})
            continue
        if not FIRST_UID <= uid < LAST_UID or not home.startswith("/") or home == "/":
            continue
        if shell not in shells or shell.rsplit("/", 1)[-1] in NO_LOGIN:
            continue
        found.append({"account": name, "home": home})
    return sorted(found, key=lambda a: (a["account"] != "root", a["account"]))


def keygen_answer(out: str) -> tuple[int, str, str]:
    """(bits, fingerprint, kind) of what ssh-keygen -l -E sha256 writes for a
    key: '256 SHA256:... comment (ED25519)'."""
    found = re.match(r"\s*(\d+)\s+(SHA256:[A-Za-z0-9+/]+)\s.*\(([A-Z0-9-]+)\)\s*$", out.strip().splitlines()[0]
                     if out.strip() else "")
    if found is None:
        raise KeyRefused("ssh-keygen does not read it as a public key")
    return int(found[1]), found[2], found[3]


# --- root's part: the files of the accounts -----------------------------

INPUT_MAX = 16384
SHELLS = Path("/etc/shells")
KEYGEN_TIMEOUT = 20
FILE = "authorized_keys"


def _shells(path: Path = SHELLS) -> set[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {"/bin/bash", "/bin/sh", "/usr/bin/bash", "/usr/bin/sh"}
    return {line.strip() for line in lines if line.strip().startswith("/")}


def accounts(service_users: set[str]) -> list[dict]:
    return offered(pwd.getpwall(), _shells(), service_users)


def account_entry(account: str, service_users: set[str]):
    """The pwd entry of an account a key may open, or why not."""
    check_account(account)
    if account not in {a["account"] for a in accounts(service_users)}:
        raise KeyRefused(f"{account} is not an account a key may open here: root, or a user of the machine with "
                         f"a login shell (from uid {FIRST_UID}), not the one OmniaCLI runs as")
    return pwd.getpwnam(account)


def sshd_facts() -> dict:
    """What the SSH server says of the keys (sshd -T): whether root may log in
    with one, where it reads them; {} when it does not answer."""
    try:
        done = subprocess.run(["sshd", "-T"], capture_output=True, text=True, timeout=KEYGEN_TIMEOUT,
                              env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"})
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if done.returncode:
        return {}
    found = {}
    for line in done.stdout.splitlines():
        key, _, value = line.partition(" ")
        if key in ("permitrootlogin", "pubkeyauthentication", "authorizedkeysfile", "passwordauthentication"):
            found[key] = value.strip()
    return found


def reads_our_file(facts: dict) -> bool:
    """sshd reads ~/.ssh/authorized_keys (its default), the file written here."""
    files = facts.get("authorizedkeysfile")
    return files is None or any(f in (".ssh/authorized_keys", "%h/.ssh/authorized_keys") for f in files.split())


def _open_ssh(entry, make: bool) -> int | None:
    """The account's ~/.ssh opened (never through a link), made 700 for it
    when make; None when it is not there and not made."""
    home = os.open(entry.pw_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if make:
            try:
                os.mkdir(".ssh", 0o700, dir_fd=home)
                os.chown(".ssh", entry.pw_uid, entry.pw_gid, dir_fd=home, follow_symlinks=False)
            except FileExistsError:
                pass
        try:
            ssh = os.open(".ssh", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=home)
        except FileNotFoundError:
            return None
        except OSError:
            raise KeyRefused(f"{entry.pw_dir}/.ssh is not a folder (a link is not followed)") from None
    finally:
        os.close(home)
    if os.fstat(ssh).st_uid != entry.pw_uid:
        os.close(ssh)
        raise KeyRefused(f"{entry.pw_dir}/.ssh is not {entry.pw_name}'s: nothing is written there")
    return ssh


def _read(ssh: int, entry) -> str:
    try:
        fd = os.open(FILE, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=ssh)
    except FileNotFoundError:
        return ""
    except OSError:
        raise KeyRefused(f"{entry.pw_dir}/.ssh/{FILE} is not a file (a link is not followed)") from None
    with os.fdopen(fd, "rb") as fh:
        return fh.read().decode("utf-8", "surrogateescape")


def _write(ssh: int, entry, text: str) -> None:
    """The file written whole: a temporary file of the account's, 600, in the
    same folder, renamed over it."""
    temp = f".{FILE}.omniacli.{os.getpid()}"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=ssh)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(text.encode("utf-8", "surrogateescape"))
            fh.flush()
            os.fchown(fh.fileno(), entry.pw_uid, entry.pw_gid)
            os.fchmod(fh.fileno(), 0o600)
            os.fsync(fh.fileno())
        os.replace(temp, FILE, src_dir_fd=ssh, dst_dir_fd=ssh)
    except BaseException:
        try:
            os.unlink(temp, dir_fd=ssh)
        except OSError:
            pass
        raise


def keygen(key_type: str, blob: str) -> tuple[int, str, str]:
    """(bits, fingerprint, kind) as ssh-keygen -l reads the key, or why not."""
    with tempfile.TemporaryDirectory(prefix="omniacli-key-") as folder:
        path = Path(folder) / "key.pub"
        path.write_text(f"{key_type} {blob}\n", encoding="ascii")
        try:
            done = subprocess.run(["ssh-keygen", "-l", "-E", "sha256", "-f", str(path)], capture_output=True,
                                  text=True, timeout=KEYGEN_TIMEOUT, env={"PATH": "/usr/bin:/bin"})
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise KeyRefused(f"ssh-keygen did not answer: {exc}") from None
    if done.returncode:
        raise KeyRefused("ssh-keygen does not read it as a public key: paste the whole line of the .pub file")
    return keygen_answer(done.stdout)


def state(service_users: set[str]) -> dict:
    """Every account a key may open, with the keys OmniaCLI added there and
    the others (their fingerprints), and what sshd says."""
    facts = sshd_facts()
    rows = []
    for account in accounts(service_users):
        entry = pwd.getpwnam(account["account"])
        row = {"account": entry.pw_name, "file": f"{entry.pw_dir}/.ssh/{FILE}", "keys": [], "others": []}
        try:
            ssh = _open_ssh(entry, make=False)
            if ssh is not None:
                try:
                    row["keys"], row["others"] = listing(_read(ssh, entry))
                finally:
                    os.close(ssh)
        except (KeyRefused, OSError) as exc:
            row["error"] = str(exc)
        rows.append(row)
    return {"accounts": rows, "sshd": facts, "reads_file": reads_our_file(facts)}


def add(account: str, given: dict, service_users: set[str]) -> dict:
    """given: {name, key, by}. The key checked (its form, then ssh-keygen),
    refused when the account has it already, then a line at the end of the
    account's file with the mark."""
    entry = account_entry(account, service_users)
    name = check_name(given.get("name"))
    key_type, blob = check_pasted(given.get("key"))
    by = " ".join(str(given.get("by") or "?").split())[:100]
    facts = sshd_facts()
    if not reads_our_file(facts):
        raise KeyRefused(f"The SSH server of this machine reads the keys from {facts.get('authorizedkeysfile')}, "
                         f"not from ~/.ssh/{FILE}: a key written there would open nothing")
    bits, found, kind = keygen(key_type, blob)
    if found != fingerprint(blob):
        raise KeyRefused("ssh-keygen reads another key than the one pasted")
    if key_type == "ssh-rsa" and bits < MIN_RSA_BITS:
        raise KeyRefused(f"An RSA key of {bits} bits is too short: at least {MIN_RSA_BITS} (or ssh-keygen -t ed25519)")
    ssh = _open_ssh(entry, make=True)
    try:
        fcntl.flock(ssh, fcntl.LOCK_EX)
        text = _read(ssh, entry)
        if found in fingerprints(text):
            raise KeyRefused(f"The key {found} opens {account} already")
        _write(ssh, entry, with_line(text, key_line(key_type, blob, name, by)))
    finally:
        os.close(ssh)
    return {"account": account, "fingerprint": found, "type": key_type, "bits": bits, "kind": kind, "name": name,
            "file": f"{entry.pw_dir}/.ssh/{FILE}"}


def remove(account: str, found: str, service_users: set[str]) -> dict:
    """The lines OmniaCLI wrote with that key taken out of the account's file,
    every other line kept where it is; refused when it has none."""
    entry = account_entry(account, service_users)
    if not FINGERPRINT.fullmatch(found or ""):
        raise KeyRefused(f"{found!r} is not the fingerprint of a key (SHA256:…)")
    ssh = _open_ssh(entry, make=False)
    if ssh is None:
        raise KeyRefused(f"OmniaCLI added no key {found} to {account}")
    try:
        fcntl.flock(ssh, fcntl.LOCK_EX)
        text = _read(ssh, entry)
        mine = {k["fingerprint"]: k for k in listing(text)[0]}
        if found not in mine:
            raise KeyRefused(f"OmniaCLI added no key {found} to {account}: a key it did not add is taken away by hand")
        kept, gone = without_key(text, found)
        _write(ssh, entry, kept)
    finally:
        os.close(ssh)
    return {"account": account, "fingerprint": found, "removed": gone, "name": mine[found]["name"],
            "type": mine[found]["type"], "file": f"{entry.pw_dir}/.ssh/{FILE}"}
