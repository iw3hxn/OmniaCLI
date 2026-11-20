# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a backup restored for a try holds: run by odoo shell on the
throwaway database (env is odoo shell's), its answer one line,
OMNIACLI-VERIFY <json>. It reads, never writes."""

import json

env.cr.execute("SELECT max(write_date) FROM res_partner")  # noqa: F821 - odoo shell's env
newest = env.cr.fetchone()[0]  # noqa: F821
print("OMNIACLI-VERIFY " + json.dumps({
    "modules": env["ir.module.module"].search_count([("state", "=", "installed")]),  # noqa: F821
    "users": env["res.users"].with_context(active_test=True).search_count([("share", "=", False)]),  # noqa: F821
    "companies": env["res.company"].search_count([]),  # noqa: F821
    "last": newest.isoformat(sep=" ", timespec="minutes") if newest else None,
}))
