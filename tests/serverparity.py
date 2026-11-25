# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The parity of the pages drawn from a model (AD-017 §8.3, slices 3 and 4): every time a machine draws an
instance's page (server.html) or a page of class B with its live context, the same page is drawn again from its
model as a panel receives it (centralpages.server_model or page_model, through JSON, then server_view or page_view)
and what the person may press must be the same: the whole <main>, its buttons, the disabled ones and their reasons,
the switches of its scripts. Used by tests/test_central_pages.py for the roles and the scopes of AD-014;
OMNIACLI_PARITY=1 turns it on for every test of the suite (tests/conftest.py)."""

import json
import re

from omniacli import app as app_module
from omniacli import centralpages, clock

# names of secrets a model of a page never carries (AD-017 §8.1)
SECRETS = ("admin_passwd", "db_password", "token", "secret", "password")
# names that have one of those words and say no secret: whether a token is set, which person a workspace's token is,
# whether a copy resets the passwords of its database (an option of a schedule)
# (slice 5: the tokens of a workspace and of the machine by their names, whether an SSO provider's secret and the SMTP
# password are set, the shortest password a page accepts)
NOT_SECRETS = frozenset({"token_set", "token_people", "reset_passwords", "secret_set", "smtp_password_set",
                         "min_password"})
# a name of a secret that is no secret at one place of one page's model only: the list of the tokens of a workspace's
# page and of Users (their names, roles and people)
NOT_SECRETS_AT = {"workspace": frozenset({"tokens"}), "users": frozenset({"tokens"})}
# the pages of classes B and C drawn again from page_view, by their template (a person's own pages have no model for
# a link)
CLASS_B = {p.template: p for p in centralpages.BY_MODEL if p.name not in centralpages.PERSON_PAGES}


def keys(value) -> list[str]:
    """Every key of a JSON value, at any depth."""
    if isinstance(value, dict):
        return [*value, *(k for v in value.values() for k in keys(v))]
    if isinstance(value, list):
        return [k for v in value for k in keys(v)]
    return []


def secret_keys(value, page: str = "") -> list[str]:
    """The keys of a JSON value (a page's model) named as a secret; at its top only, those NOT_SECRETS_AT allows the
    page."""
    top = NOT_SECRETS_AT.get(page, frozenset()) if isinstance(value, dict) else frozenset()
    found = [k for k in keys(value) if k not in NOT_SECRETS and any(s in k.lower() for s in SECRETS)]
    for k in top & set(value):
        # one of the top's, at the top: not counted; anywhere deeper it still is
        found.remove(k)
    return found


def main_of(html: str) -> str:
    """What a page shows in its <main>, the controls and their scripts with it; a part of a page (the Dashboard's
    table of instances), all of it."""
    if "<main>" not in html:
        return html
    return html[html.index("<main>"):html.index("</main>")]


class Twice:
    """TemplateResponse that draws server.html and the pages of class B twice, from the live context and from its
    model, and compares."""

    def __init__(self, monkeypatch):
        self.real = app_module._TEMPLATES.TemplateResponse
        self.compared: list[dict] = []
        monkeypatch.setattr(app_module._TEMPLATES, "TemplateResponse", self)

    def __call__(self, request, name, context=None, **kw):
        if name == "moved_away.html" and not isinstance((context or {}).get("server"), centralpages.Fields):
            # the page of a move (class D): drawn the same from its small model (what /api/pages/server answers)
            text = json.dumps(centralpages.plain(context))
            resp = self.real(request, name, context, **kw)
            prefix = getattr(request.state, "prefix", "") or ""
            drawn = self.real(request, name, centralpages.moved_away_view(json.loads(text), prefix), **kw)
            own, theirs = main_of(resp.body.decode()), main_of(drawn.body.decode())
            assert own == theirs, first_difference(own, theirs)
            self.compared.append({"path": request.url.path, "page": name})
            return resp
        page = CLASS_B.get(name)
        if page is not None and not isinstance(context, centralpages.Drawn):
            # the context as the route made it: only fields of the page's list
            assert set(context) <= set(page.fields), set(context) - set(page.fields)
            text = json.dumps(centralpages.page_model(page.name, context))
            return self.compare(request, name, context, text, lambda model, prefix: centralpages.page_view(
                page.name, model, prefix, app_module.request_language(request)), page.name, **kw)
        if name != "server.html" or isinstance((context or {}).get("server"), centralpages.Fields):
            return self.real(request, name, context, **kw)
        # the context as the route made it (the response adds the page's own: prefix, can_act, request...)
        assert set(context) == set(centralpages.SERVER_FIELDS), set(context) ^ set(centralpages.SERVER_FIELDS)
        text = json.dumps(centralpages.server_model(context))
        return self.compare(request, name, context, text, centralpages.server_view, "server", **kw)

    def compare(self, request, name, context, text, view, page, **kw):
        """The page drawn from context, and again from its model (text) as the panel of the link would: its
        prefix's addresses checked, and through a link its globals (what the role there allows, the person, the
        machine's clock: centralpages.drawn_for), as draw_linked_page sets them."""
        resp = self.real(request, name, context, **kw)
        found = secret_keys(json.loads(text), page)
        assert not found, (page, found)
        prefix = getattr(request.state, "prefix", "") or ""
        user = getattr(request.state, "user", None)
        saved = getattr(request.state, "drawn_for", None)
        if getattr(user, "kind", "") == "link":
            development = request.app.state.settings.machine == "development"
            request.state.drawn_for = centralpages.drawn_for(prefix.rsplit("/", 1)[-1], user.role, development,
                                                             user.on_behalf, clock.here())
        try:
            drawn = self.real(request, name, view(json.loads(text), prefix), **kw)
        finally:
            request.state.drawn_for = saved
        own, theirs = main_of(resp.body.decode()), main_of(drawn.body.decode())
        assert own == theirs, (page, first_difference(own, theirs))
        self.compared.append({"path": request.url.path, "page": page, "user": getattr(user, "name", ""),
                              "role": getattr(user, "role", ""), "scoped": bool(getattr(user, "scoped", False)),
                              "via": getattr(user, "via", "") or "", "buttons": own.count("<button"),
                              "disabled": own.count(" disabled"), "main": hash(own)})
        return resp


def first_difference(own: str, theirs: str) -> str:
    """Where two pages part, with some text around."""
    at = next((i for i, (a, b) in enumerate(zip(own, theirs)) if a != b), min(len(own), len(theirs)))
    return f"at {at}:\nmachine: {own[max(0, at - 200):at + 200]!r}\nmodel:   {theirs[max(0, at - 200):at + 200]!r}"


def controls(main: str) -> dict:
    """What a person may press on a page's <main>: its buttons with their state and title, its links' addresses,
    its forms' actions."""
    return {"buttons": re.findall(r"<button\b[^>]*>", main), "links": re.findall(r'href="([^"]*)"', main),
            "forms": re.findall(r'action="([^"]*)"', main), "disabled": main.count(" disabled")}


def same_controls(own: str, theirs: str, where=None, whole: bool = True) -> None:
    """The parity of the people of a workspace served by a central panel (AD-018 slice 2, constraint 3): a page of a
    person through the central has the same controls as on its machine (its buttons with their state and title, its
    links, its forms) and, whole, the same <main> (not for a page of what the request itself adds to: an access log,
    the jobs)."""
    assert controls(own) == controls(theirs), (where, first_difference(own, theirs))
    if whole:
        assert own == theirs, (where, first_difference(own, theirs))
