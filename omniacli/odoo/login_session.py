# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Log in as a user of the instance, without the user's password and without
a module in Odoo: the session Odoo makes at a login, made here.

Run with an Odoo environment `env` (Odoo 16 and later):

- by click-odoo on the instance's database (an Odoo of a venv), the request
  as the first argument;
- by `odoo shell` reading this text on its input (an Odoo in a container,
  compose-helper login), the request in OMNIACLI_LOGIN.

The request is `users` (the active users, as JSON) or a user's id (a new
session of that user: its sid). The session is written in Odoo's own
session store (<data_dir>/sessions), as the user Odoo runs as, with the
token Odoo checks at every request (security.check_session): it is made of
the user's password hash, so a password changed later ends it. The password
itself is never read in clear nor changed, and nothing is written in the
database. The answer is one line, OMNIACLI-LOGIN <json>, after whatever
Odoo prints."""

import json
import os
import sys

from odoo import http, release
from odoo.service import security

MARK = "OMNIACLI-LOGIN "
# where a browser lands after the login: /odoo from Odoo 18, /web before
HOME_FROM = 18


def answer(data):
    # the caller reads this line among Odoo's own output: no logger
    sys.stdout.write("\n" + MARK + json.dumps(data) + "\n")
    sys.stdout.flush()


def request_of():
    value = os.environ.get("OMNIACLI_LOGIN") or (sys.argv[1] if len(sys.argv) > 1 else "")
    return value.strip()


def main(env):
    # the session store and get_default_session of the http of Odoo 16
    if not hasattr(http, "get_default_session") or not hasattr(http, "root"):
        return {"error": "Odoo 16 or later is needed to log in as a user"}
    request = request_of()
    # env is the superuser's (click-odoo, odoo shell): it reads every user
    users = env["res.users"].with_context(active_test=True)
    if request == "users":
        found = users.search([("active", "=", True)], order="share, name")
        return {"users": [{"id": u.id, "login": u.login, "name": u.name, "internal": not u.share,
                           "admin": u.has_group("base.group_system")} for u in found]}
    if not request.isdigit():
        return {"error": "the request is users or a user's id, not %r" % request}
    user = users.browse(int(request)).exists()
    if not user or not user.active:
        return {"error": "no active user with id %s" % request}
    store = http.root.session_store
    session = store.new()
    session.update(http.get_default_session())
    session.db = env.cr.dbname
    session.uid = user.id
    session.login = user.login
    session.context = dict(user.with_user(user).context_get())
    session.session_token = security.compute_session_token(session, env)
    store.save(session)
    return {"sid": session.sid, "id": user.id, "login": user.login, "name": user.name,
            "home": "/odoo" if release.version_info[0] >= HOME_FROM else "/web"}


try:
    answer(main(env))  # noqa: F821 (given by click-odoo and by odoo shell)
except Exception as exc:  # noqa: BLE001 (the caller shows the answer: a traceback would be lost in Odoo's output)
    answer({"error": "%s: %s" % (type(exc).__name__, exc)})
