# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The options an Odoo knows (its odoo.tools.config parser), for the Odoo
config tab of an instance: name, help, kind, group, default. Run with the
instance's python, odoo importable (a venv with odoo-bin's folder on
PYTHONPATH, or an Odoo container's python3, the text on its input). Nothing is
read from or written to the config file here.

The answer is one line, OMNIACLI-OPTIONS <json>, after whatever Odoo prints."""

import json
import sys

from odoo import release
from odoo.tools import config

MARK = "OMNIACLI-OPTIONS "
# --help and --version are actions, not options
NOT_OPTIONS = ("help", "version")


def kind_of(option) -> str:
    if option.action in ("store_true", "store_false"):
        return "bool"
    return option.type if option.type in ("int", "float") else "string"


def main() -> dict:
    parser = config.parser
    found, seen = [], set()
    for group in [parser, *parser.option_groups]:
        title = getattr(group, "title", "") or ""
        for option in group.option_list:
            name = option.dest
            if not name or name in NOT_OPTIONS or name in seen:
                continue
            seen.add(name)
            text = option.help or ""
            if "SUPPRESS" in text:
                continue
            default = option.default
            if isinstance(default, tuple):
                default = None
            found.append({"name": name, "help": text.replace("%default", str(default)), "kind": kind_of(option),
                          "group": title, "default": default if isinstance(default, (str, int, float, bool)) else None})
    return {"options": found, "version": release.version}


if __name__ == "__main__":
    sys.stdout.write("\n" + MARK + json.dumps(main()) + "\n")
