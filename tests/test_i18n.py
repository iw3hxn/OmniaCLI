# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The languages of the web interface (i18n.py): every text a template
marks has its Italian, the catalogs are well formed, and what the code says
is translated where it is shown."""

import json
import re
from pathlib import Path

import httpx
import pytest

from omniacli import i18n
from omniacli.app import create_app

from .conftest import BasicAsSession
from .test_links import HOST, as_user

ROOT = Path(i18n.__file__).parent
TEMPLATES = [*ROOT.glob("templates/*.html"), *ROOT.glob("*/templates/*.html")]
# _("text") or _('text'), the text a plain literal (its escapes as Jinja reads them)
MARKED = re.compile(r"""\b_\(\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')""")


def marked_texts() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for path in TEMPLATES:
        for match in MARKED.finditer(path.read_text(encoding="utf-8")):
            raw = match.group(1) if match.group(1) is not None else match.group(2)
            text = raw.encode().decode("unicode_escape").encode("latin-1").decode("utf-8")
            found.setdefault(text, set()).add(path.name)
    return found


def test_every_marked_text_has_its_italian():
    italian = i18n.catalog("it")
    missing = {text: sorted(files) for text, files in marked_texts().items() if text not in italian}
    assert not missing, "texts without Italian (omniacli/i18n/it/*.json):\n" + "\n".join(
        f"  {text!r}  ({', '.join(files)})" for text, files in sorted(missing.items()))


def test_the_catalogs_are_well_formed():
    seen: dict[str, str] = {}
    for path in sorted((ROOT / "i18n").glob("*/*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict), path
        for source, target in data.items():
            assert isinstance(source, str) and isinstance(target, str) and target, (path.name, source)
            # the same {names} on both sides: a value is never lost
            assert set(re.findall(r"\{(\w+)\}", source)) == set(re.findall(r"\{(\w+)\}", target)), (path.name, source)
            # one text, one translation, whichever file holds it
            if source in seen:
                assert seen[source] == target, f"{source!r}: {seen[source]!r} and {target!r}"
            seen[source] = target


def test_what_the_code_says_is_translated_where_shown(monkeypatch):
    monkeypatch.setattr(i18n, "catalog", lambda language: {
        "No link {name}": "Nessun link {name}", "--- restart {key}": "--- riavvio {key}",
        "test": "prova"} if language == "it" else {})
    i18n._patterns.cache_clear()
    i18n._tr.cache_clear()
    try:
        assert i18n.tr("it", "No link acme") == "Nessun link acme"
        # a value stays as it is, even when it is a word of the catalog
        assert i18n.tr("it", "No link test") == "Nessun link test"
        assert i18n.tr("it", "14:10:09 --- restart srv/test") == "14:10:09 --- riavvio srv/test"
        assert i18n.tr("it", "unknown text") == "unknown text"
        assert i18n.tr("en", "No link acme") == "No link acme"
        assert i18n.gettext("it", "No link {name}", name="x") == "Nessun link x"
    finally:
        i18n._patterns.cache_clear()
        i18n._tr.cache_clear()


def test_a_reason_inside_a_line_is_translated_too():
    """A line with a part that is itself a text of the catalog (the reason
    of a refusal, of several words): both in the reader's language; a name
    stays as it is."""
    stays = ("the role of the replication, its line of pg_hba.conf, the firewall rule for this machine's address and "
             "the place that holds the changes for the replica, which grows up to its cap; its instances that read "
             "from the replica go on trying it and read from their cluster. An admin there drops them in its Database "
             "tab (Drop the replica…)")
    said = i18n.tr("it", f"lab4 did not take its part back (the connection broke before the answer was whole): on it stay {stays}")
    assert said.startswith("lab4 non ha ripreso la sua parte (la connessione si è interrotta prima che la risposta fosse "
                           "intera): là restano il ruolo della replica")
    assert " the " not in said and " and " not in said
    assert i18n.tr("it", f"test did not take its part back (refused): on it stay {stays}").startswith(
        "test non ha ripreso la sua parte (refused)")


def test_the_language_of_a_request():
    assert i18n.choose("it", "en-US") == "it"
    assert i18n.choose(None, "it-IT,it;q=0.9,en;q=0.8") == "it"
    assert i18n.choose("xx", "fr-FR,de") == "en"


@pytest.fixture
async def client(env):
    app = create_app(env["settings"])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=BasicAsSession(app)),
                                 base_url=f"http://{HOST}") as c:
        yield c


async def test_the_selector_sets_the_language(client):
    resp = await client.get("/language/it?next=/jobs")
    assert resp.status_code == 303 and resp.headers["location"] == "/jobs"
    assert "omniacli_lang=it" in resp.headers["set-cookie"]
    assert (await client.get("/language/it?next=https://evil.test/")).headers["location"] == "/"
    page = await client.get("/", headers=as_user("admin"))
    assert '<html lang="it">' in page.text and ">Esci<" in page.text
    client.cookies.clear()
    page = await client.get("/", headers={**as_user("admin"), "Accept-Language": "it-IT"})
    assert '<html lang="it">' in page.text
    client.cookies.clear()
    page = await client.get("/", headers=as_user("admin"))
    assert '<html lang="en">' in page.text and ">Log out<" in page.text


async def test_an_error_is_said_in_the_callers_language(client, monkeypatch):
    monkeypatch.setattr(i18n, "tr", lambda language, text: f"[{language}] {text}")
    headers = {**as_user("admin", post=True), "Accept": "application/json", "Accept-Language": "it"}
    resp = await client.post("/api/links/delete", headers=headers, json={"name": "nope"})
    assert resp.status_code == 404 and resp.json()["detail"].startswith("[it] ")


def test_dates_and_measures_of_a_line_in_the_readers_language():
    """A job line the code writes with an ISO date and a dot: the date as the
    pages write it, a measure with the decimal comma; a version stays as it
    is (22/11/2026: «fermo dal 2026-11-22T15:04:38», «13.5 GB» on Italian pages)."""
    said = i18n.tr("it", "The replica answers and receives from lab1 (PostgreSQL 17.12, started in 1.7 s)")
    assert said == "La replica risponde e riceve da lab1 (PostgreSQL 17.12, avviata in 1,7 s)"
    assert i18n.tr("it", "The PostgreSQL of acme-tmp was stopped already, on 2026-11-22T15:04:38").endswith(
        "dal 22/11/2026 15:04")
    assert "13,5 GB" in i18n.tr("it", "A role for the replication and a place that holds the changes for the replica "
                                      "are there: the database keeps 13.5 GB of them at most, on the disk of the "
                                      "workspace acme, beyond which the replica is made again; pg_hba.conf lets "
                                      "172.30.0.12 in for the replication only, over TLS")
    # in English: as the code wrote it
    assert i18n.tr("en", "The PostgreSQL of acme-tmp was stopped already, on 2026-11-22T15:04:38").endswith("2026-11-22T15:04:38")
