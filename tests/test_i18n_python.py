# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the Python code says (errors, job lines, labels, checks) is in
Italian where it is shown: real texts, as the code makes them, through
i18n.tr and the catalog omniacli/i18n/it/python.json."""

import json
import re
from pathlib import Path

import pytest

from omniacli import i18n

PYTHON = Path(i18n.__file__).with_name("i18n") / "it" / "python.json"


@pytest.fixture(autouse=True)
def fresh_catalog():
    i18n.catalog.cache_clear()
    i18n._patterns.cache_clear()
    i18n._tr.cache_clear()
    yield
    i18n._tr.cache_clear()


@pytest.mark.parametrize("english, italian", [
    # errors returned to the browser
    ("No instance erp/test", "Nessuna istanza erp/test"),
    ("A job is running on erp: try again when it ends", "Su erp è in esecuzione un job: riprova quando finisce"),
    ("The job is not running: it is failed", "Il job non è in esecuzione: è failed"),
    ("Type erp to replace the production database", "Scrivi erp per sostituire il database di produzione"),
    ("'bad user' is not a user name", "'bad user' non è un nome utente"),
    # job lines, with the time in front, and job failures
    ("14:10:09 --- [addons] pull origin/18.0", "14:10:09 --- [addons] pull origin/18.0"),
    ("14:10:09 --- restart systemd service odoo", "14:10:09 --- riavvio del servizio systemd odoo"),
    ("14:10:10 [addons] already up to date", "14:10:10 [addons] già aggiornato"),
    ("--- backup database erp without its filestore to /var/b/erp.zip",
     "--- backup del database erp senza filestore in /var/b/erp.zip"),
    ("--- backup database erp to /var/b/erp.zip", "--- backup del database erp in /var/b/erp.zip"),
    ("[addons] /srv/addons is on branch 'dev', expected '18.0'",
     "[addons] /srv/addons è sul branch 'dev', atteso '18.0'"),
    ("the backup on prod failed: no reason given", "il backup su prod è fallito: no reason given"),
    ("FAILED: no reason", "FALLITO: no reason"),
    # labels, job titles, checks
    ("Copy production to test", "Copia produzione in test"),
    ("Restart erp/test", "Riavvia erp/test"),
    ("Restart after the update", "Riavvia dopo l'aggiornamento"),
    ("Update changed modules on erp/test + restart", "Aggiorna i moduli modificati su erp/test + riavvio"),
    ("Update sale, stock on erp/test", "Aggiorna sale, stock su erp/test"),
    ("Restart permission", "Permesso di riavvio"),
    ("/var/log/odoo.log readable", "/var/log/odoo.log leggibile"),
    ("/srv/addons on 18.0, clean, origin/18.0 reachable", "/srv/addons su 18.0, pulito, origin/18.0 raggiungibile"),
    ("Memory of a worker, soft, MB", "Memoria di un worker, soft, MB"),
])
def test_a_real_text_is_in_italian(english, italian):
    assert i18n.tr("it", english) == italian
    assert i18n.tr("en", english) == english


def test_a_value_is_not_translated():
    # "test" is the name of a server here, not a word to translate
    assert i18n.tr("it", "No server test") == "Nessun server test"


def test_no_pattern_is_too_generic():
    """A pattern whose fixed text is short would swallow other texts."""
    field = re.compile(r"\{\w+\}")
    data = json.loads(PYTHON.read_text(encoding="utf-8"))
    short = [k for k in data if field.search(k) and len(field.sub("", k).strip()) < 4]
    assert not short, short
