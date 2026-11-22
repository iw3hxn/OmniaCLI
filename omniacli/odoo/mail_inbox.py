# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run by odoo shell in a staging whose mail is caught (compose-helper
mail-inbox, UX-008 §5.4): its incoming mail, read from the mailboxes of the
project's service inbox (GreenMail) as a real Odoo reads its own. The
helper puts in front of this text one line, OMNIACLI_MAIL = {"mode":
"state" | "on" | "fetch", "domain": "<staging>.test"}, both checked.

- state: what is configured, nothing written.
- on: the alias domain of the staging on every company, the aliases with a
  name moved onto it (a copy of production answers on the test domain, not
  on the customer's), one incoming IMAP server per mailbox (catchall and
  each alias, on inbox:3143, any password) whose login is tried, every
  other incoming server archived (a copy's real IMAP would read, and delete,
  the customer's mailbox: the firewall stops SMTP only), the outgoing
  server the caught one alone, and the two mail crons on (the queue,
  the mailboxes); every other cron as it was. Run again, it changes nothing.
- fetch: on, then the outgoing queue sent (to the Mail tab) and every
  mailbox read now.

It refuses a database that is not neutralized: a staging's only. The answer
is its last line, OMNIACLI-MAIL {json}. Odoo 17 to 20 (mail.alias.domain)."""

import json

data = OMNIACLI_MAIL  # noqa: F821 (the helper's first line)
odoo_env = env  # noqa: F821 (given by odoo shell)

# the project's services (templates' compose.yaml): the caught SMTP (its
# network names mail and invalid, the host odoo neutralize writes) and the
# mailboxes; the mark of the servers this script owns
SMTP_HOSTS, SMTP_PORT = ("invalid", "mail"), 1025
IMAP_HOST, IMAP_PORT = "inbox", 3143
CRONS = ("mail.ir_cron_mail_scheduler_action", "mail.ir_cron_mail_gateway_action")
# the aliases that get no mailbox: bounces never come back from the inbox
SKIP = {"bounce"}
# a database with many aliases: the first ones by name, the others said
MAILBOX_MAX = 60
DOMAIN = data["domain"]
said = []


def stamp(value):
    return value.isoformat(sep="T", timespec="seconds") if value else None


def ours(server):
    return server.server == IMAP_HOST and server.port == IMAP_PORT


def mailboxes(domain):
    """catchall, then every named alias of the domain, without SKIP."""
    names = odoo_env["mail.alias"].search([("alias_domain_id", "=", domain.id), ("alias_name", "!=", False)],
                                          order="alias_name").mapped("alias_name")
    found = [domain.catchall_alias or "catchall"]
    found += [n for n in dict.fromkeys(names) if n not in found and n not in SKIP | {domain.bounce_alias}]
    return found[:MAILBOX_MAX], found[MAILBOX_MAX:]


def state():
    Fetch = odoo_env["fetchmail.server"].with_context(active_test=False)
    domain = odoo_env["mail.alias.domain"].search([("name", "=", DOMAIN)], limit=1)
    companies = odoo_env["res.company"].sudo().search([])
    servers = [{"user": s.user, "state": s.state, "active": s.active, "date": stamp(s.date),
                "error": stamp(getattr(s, "error_date", False))}
               for s in Fetch.search([], order="user") if ours(s)]
    others = Fetch.search_count([("active", "=", True)]) - len([s for s in servers if s["active"]])
    crons = {}
    for xmlid in CRONS:
        cron = odoo_env.ref(xmlid, raise_if_not_found=False)
        crons[xmlid.split(".")[1]] = {"active": bool(cron and cron.active),
                                      "lastcall": stamp(cron.lastcall) if cron else None}
    expected, left = mailboxes(domain) if domain else ([], [])
    done = {s["user"] for s in servers if s["active"] and s["state"] == "done"}
    on = bool(domain) and all(c.alias_domain_id == domain for c in companies) \
        and all(f"{m}@{DOMAIN}" in done for m in expected) and all(c["active"] for c in crons.values())
    return {"on": on, "domain": DOMAIN, "company_domains": sorted({c.alias_domain_id.name or "" for c in companies}),
            "mailboxes": [f"{m}@{DOMAIN}" for m in expected], "left_out": [f"{m}@{DOMAIN}" for m in left],
            "servers": servers, "other_servers": others, "crons": crons,
            "last_fetch": max((s["date"] for s in servers if s["date"]), default=None)}


def switch_on():
    AliasDomain = odoo_env["mail.alias.domain"]
    domain = AliasDomain.search([("name", "=", DOMAIN)], limit=1)
    if not domain:
        domain = AliasDomain.create({"name": DOMAIN, "catchall_alias": "catchall", "bounce_alias": "bounce",
                                     "default_from": "notifications"})
        said.append(f"alias domain {DOMAIN} made")
    companies = odoo_env["res.company"].sudo().search([("alias_domain_id", "!=", domain.id)])
    if companies:
        companies.write({"alias_domain_id": domain.id})
        said.append(f"{len(companies)} companies on {DOMAIN}")
    # the aliases answer on the staging's domain; one whose name is taken
    # there already stays where it was (said)
    Alias = odoo_env["mail.alias"].sudo()
    moved = 0
    for alias in Alias.search([("alias_name", "!=", False), ("alias_domain_id", "!=", domain.id)]):
        if Alias.search_count([("alias_name", "=", alias.alias_name), ("alias_domain_id", "=", domain.id)]):
            said.append(f"alias {alias.alias_name} left on {alias.alias_domain_id.name or 'no domain'}: "
                        f"{alias.alias_name}@{DOMAIN} is taken")
            continue
        alias.alias_domain_id = domain
        moved += 1
    if moved:
        said.append(f"{moved} aliases moved onto {DOMAIN}")

    # outgoing: the caught server alone (odoo neutralize made it; made again if gone)
    Smtp = odoo_env["ir.mail_server"].sudo().with_context(active_test=False)
    smtp = Smtp.search([("smtp_host", "in", SMTP_HOSTS), ("smtp_port", "=", SMTP_PORT)], order="active desc, id",
                       limit=1)
    if not smtp:
        smtp = Smtp.create({"name": "Staging: caught mail", "smtp_host": "invalid", "smtp_port": SMTP_PORT,
                            "smtp_encryption": "none", "sequence": 1})
        said.append("outgoing server of the caught mail made")
    elif not smtp.active:
        smtp.active = True
    others = Smtp.search([("id", "!=", smtp.id), ("active", "=", True)])
    if others:
        others.write({"active": False})
        said.append(f"{len(others)} other outgoing servers archived")

    # incoming: one server per mailbox, the others archived
    Fetch = odoo_env["fetchmail.server"].sudo().with_context(active_test=False)
    expected, left = mailboxes(domain)
    users = {f"{m}@{DOMAIN}" for m in expected}
    for server in Fetch.search([("active", "=", True)]):
        if not ours(server) or server.user not in users:
            server.write({"active": False})
            said.append(f"incoming server {server.name} archived")
    for user in sorted(users):
        server = Fetch.search([("server", "=", IMAP_HOST), ("port", "=", IMAP_PORT), ("user", "=", user)], limit=1)
        values = {"name": f"Staging: {user}", "server_type": "imap", "server": IMAP_HOST, "port": IMAP_PORT,
                  "is_ssl": False, "user": user, "password": "staging", "active": True}
        if not server:
            server = Fetch.create(values)
            said.append(f"incoming server {user} made")
        elif any(server[k] != v for k, v in values.items()):
            server.write(values)
        if server.state != "done":
            server.button_confirm_login()
    if left:
        said.append(f"{len(left)} aliases without a mailbox (more than {MAILBOX_MAX})")
    # odoo neutralize switched every cron off: these two only go back on
    for xmlid in CRONS:
        cron = odoo_env.ref(xmlid, raise_if_not_found=False)
        if cron and not cron.active:
            cron.sudo().active = True
            said.append(f"cron {xmlid} on")
    odoo_env.cr.commit()


parameters = odoo_env["ir.config_parameter"].sudo()
# get_param to the Odoo 20 of 2026-10; get_str (typed getters, no get_param) from the Odoo 20 of 2026-11
neutralized = (parameters.get_str if hasattr(parameters, "get_str") else parameters.get_param)("database.is_neutralized")
if "fetchmail.server" not in odoo_env:
    answer = {"error": "Odoo of the staging has no mail module (Discuss): install it, or an app that needs it "
                       "(CRM, Sales, Helpdesk), first"}
elif not neutralized:
    answer = {"error": "the database is not neutralized: its incoming mail is configured in a staging only"}
else:
    fetched = None
    if data["mode"] in ("on", "fetch"):
        switch_on()
    if data["mode"] == "fetch":
        before = odoo_env["mail.message"].sudo().search_count([("message_type", "=", "email")])
        # the queue first: what Odoo was about to send reaches the Mail tab (and a .test mailbox)
        odoo_env["mail.mail"].sudo().process_email_queue()
        odoo_env.cr.commit()
        Fetch = odoo_env["fetchmail.server"].sudo()
        for server in Fetch.search([("server", "=", IMAP_HOST), ("port", "=", IMAP_PORT), ("state", "=", "done")]):
            try:
                server.fetch_mail()
            except Exception as exc:  # noqa: BLE001 - one mailbox's failure is said, the others are read
                odoo_env.cr.rollback()
                said.append(f"{server.user} not read: {exc}")
        odoo_env.cr.commit()
        odoo_env.invalidate_all()
        fetched = odoo_env["mail.message"].sudo().search_count([("message_type", "=", "email")]) - before
    answer = {**state(), "said": said, "fetched": fetched}
print("OMNIACLI-MAIL " + json.dumps(answer))
