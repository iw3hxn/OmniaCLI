# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The active users of an instance for Log in as, read in SQL: what
login_session.py answers for `users`, without loading Odoo (its registry
takes seconds, a minute on a big database).

Run with the instance's python (its psycopg2) as the user the instance runs
as: list_users.py <odoo config> <database>. The connection is the config's
(db_host, db_port, db_user, db_password, db_sslmode; no db_host: the local
socket). The answer is one line, OMNIACLI-LOGIN <json>, as login_session.py's.
Odoo 16 and later: res_users.share, res_groups_users_rel, base.group_system."""

import configparser
import json
import sys

import psycopg2

MARK = "OMNIACLI-LOGIN "


def option(parser, name):
    value = parser.get("options", name, fallback="")
    return None if value in ("", "False", "false", "None") else value


def main(config, database):
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(config)
    args = {"dbname": database}
    for name, key in (("db_host", "host"), ("db_port", "port"), ("db_user", "user"),
                      ("db_password", "password"), ("db_sslmode", "sslmode")):
        if option(parser, name):
            args[key] = option(parser, name)
    connection = psycopg2.connect(**args)
    try:
        with connection.cursor() as cr:
            cr.execute("""
                SELECT u.id, u.login, p.name, u.share,
                       EXISTS (SELECT 1 FROM res_groups_users_rel r
                               JOIN ir_model_data d ON d.res_id = r.gid AND d.model = 'res.groups'
                                AND d.module = 'base' AND d.name = 'group_system'
                               WHERE r.uid = u.id)
                  FROM res_users u JOIN res_partner p ON p.id = u.partner_id
                 WHERE u.active
                 ORDER BY u.share, p.name""")
            users = [{"id": uid, "login": login, "name": name, "internal": not share, "admin": admin}
                     for uid, login, name, share, admin in cr.fetchall()]
    finally:
        connection.close()
    return {"users": users}


if __name__ == "__main__":
    sys.stdout.write("\n" + MARK + json.dumps(main(sys.argv[1], sys.argv[2])) + "\n")
