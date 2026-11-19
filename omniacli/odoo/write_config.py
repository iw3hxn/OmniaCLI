# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Change options of an Odoo config file: write_config.py <odoo config>, on
the input {"set": {option: value}, "unset": [option]} as JSON. Run as the user
the file is written by (the instance's run_as, or root by compose-helper for
a workspace's instance): the file is rewritten in place, so its owner and
mode stay; every other line, comment and section stays as it is.

The options OmniaCLI and the server's layout own are refused (LOCKED): the
database connection, the ports, the folders and files of the instance. The
answer is one line, OMNIACLI-CONFIG <json>: {"changed": [...]} or {"error"}.
Standard library only: it runs with any python 3."""

import json
import os
import re
import sys

MARK = "OMNIACLI-CONFIG "
LOCKED = {"db_host", "db_port", "db_user", "db_password", "db_name", "db_sslmode", "db_template", "http_port",
          "gevent_port", "longpolling_port", "xmlrpc_port", "http_interface", "xmlrpc_interface", "data_dir",
          "logfile", "pidfile", "addons_path", "config", "save", "init", "update", "reinit", "stop_after_init"}
NAME = re.compile(r"[a-z_][a-z0-9_]{0,63}")
SECTION = re.compile(r"^\s*\[(?P<name>[^\]]+)\]\s*$")
LINE = re.compile(r"^\s*(?P<key>[A-Za-z0-9_]+)\s*[=:]")


def check(changes):
    if not isinstance(changes, dict) or set(changes) - {"set", "unset"}:
        raise ValueError("the input is {set: {option: value}, unset: [option]}")
    values = changes.get("set") or {}
    unset = changes.get("unset") or []
    if not isinstance(values, dict) or not isinstance(unset, list):
        raise ValueError("set is an object, unset a list")
    for name in [*values, *unset]:
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise ValueError(f"{name!r} is not an option name")
        if name in LOCKED:
            raise ValueError(f"{name} belongs to the server's layout: not changed here")
    clean = {}
    for name, value in values.items():
        text = str(value)
        if len(text) > 4096 or any(c in text for c in "\r\n\0"):
            raise ValueError(f"{name}: one line, 4096 characters at most")
        clean[name] = text
    return clean, [n for n in unset if n not in clean]


def rewrite(text, values, unset):
    """text with values set and unset removed in [options]; the options not
    there yet go at the end of [options] (made when the file has none)."""
    lines, out, section, done = text.splitlines(), [], None, set()
    end_of_options = None
    for line in lines:
        found = SECTION.match(line)
        if found:
            if section == "options":
                end_of_options = len(out)
            section = found["name"].strip()
            out.append(line)
            continue
        key = LINE.match(line)
        if section == "options" and key and key["key"] in values:
            out.append(f"{key['key']} = {values[key['key']]}")
            done.add(key["key"])
            continue
        if section == "options" and key and key["key"] in unset:
            done.add(key["key"])
            continue
        out.append(line)
    if section == "options":
        end_of_options = len(out)
    missing = [f"{k} = {v}" for k, v in values.items() if k not in done]
    if missing:
        if end_of_options is None:
            out = ["[options]", *missing, *out]
        else:
            # before the blank lines that close the section
            while end_of_options > 0 and not out[end_of_options - 1].strip():
                end_of_options -= 1
            out[end_of_options:end_of_options] = missing
    changed = sorted(done | {k for k in values if k not in done})
    return "\n".join(out) + "\n", changed


def main(path):
    values, unset = check(json.loads(sys.stdin.read() or "{}"))
    if not os.access(path, os.W_OK):
        raise ValueError(f"{path} cannot be written by {os.environ.get('USER') or os.getuid()}")
    with open(path, "r+", encoding="utf-8") as fh:
        text, changed = rewrite(fh.read(), values, unset)
        fh.seek(0)
        fh.write(text)
        fh.truncate()
    return {"changed": changed}


if __name__ == "__main__":
    try:
        answer = main(sys.argv[1])
    except (ValueError, OSError) as exc:
        answer = {"error": str(exc)}
    sys.stdout.write("\n" + MARK + json.dumps(answer) + "\n")
