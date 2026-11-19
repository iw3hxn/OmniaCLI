# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run by odoo shell on a new database (compose-helper setup): the password
of admin chosen in the New instance dialog,
the country of the company. The helper puts in front of this text one line,
OMNIACLI_SETUP = {"password": ..., "country": "CH"} (keys optional): the
password reaches Odoo on the shell's input, never on a command line.

The password is written through the ORM, which hashes it. The country is
set before any accounting is installed, so Odoo picks its localization when
Accounting is installed."""

data = OMNIACLI_SETUP  # noqa: F821 (the helper's first line)
odoo_env = env  # noqa: F821 (given by odoo shell)
said = []
admin = odoo_env.ref("base.user_admin", raise_if_not_found=False) or odoo_env["res.users"].browse(2)
if data.get("password"):
    admin.password = data["password"]
    said.append(f"password of {admin.login} set")
if data.get("country"):
    country = odoo_env["res.country"].search([("code", "=", data["country"])], limit=1)
    company = odoo_env.company if hasattr(odoo_env, "company") else odoo_env.user.company_id
    if country:
        company.country_id = country
        said.append(f"country of {company.name}: {country.name}")
    else:
        said.append(f"no country {data['country']}: left as it was")
odoo_env.cr.commit()
print("OMNIACLI-SETUP " + "; ".join(said or ["nothing to do"]))
