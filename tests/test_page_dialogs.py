"""The pages ask with their own dialog, never the browser's (UX-006 §3): no
alert, confirm nor prompt in any template; base.html's omniaSay, omniaAsk,
omniaWrite and omniaTypeName instead, and a form with data-confirm asks
before it is posted."""
import re
from pathlib import Path

from tests.test_app import AUTH, client  # noqa: F401 (client: the fixture of the pages)

PACKAGE = Path(__file__).parent.parent / "omniacli"
# a call of the browser's dialog: confirm(…), window.alert(…); not a method of something else (b.confirm)
BROWSER_DIALOG = re.compile(r"(?:(?<![\w$.])|(?<=window\.))(?:alert|confirm|prompt)\s*\(")


def test_no_template_calls_the_browsers_dialogs():
    found = [f"{path.relative_to(PACKAGE)}:{number}: {line.strip()}"
             for path in sorted(PACKAGE.rglob("*.html"))
             for number, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1)
             if BROWSER_DIALOG.search(line)]
    assert not found, "the browser's alert/confirm/prompt (use omniaSay, omniaAsk, omniaWrite):\n" + "\n".join(found)


def test_the_pattern_finds_them():
    for call in ('if (!confirm("x")) return;', "alert(1)", 'window.confirm("x")', "prompt ('name')"):
        assert BROWSER_DIALOG.search(call), call
    for other in ("b.confirm(target)", "omniaAsk(x)", "data-confirm=", "self.confirm(target)"):
        assert not BROWSER_DIALOG.search(other), other


async def test_the_forms_ask_through_data_confirm(client):
    page = (await client.get("/servers/srv?view=test", headers={"Authorization": AUTH})).text
    assert "window.omniaAsk = " in page and 'document.addEventListener("submit"' in page
    assert "onsubmit=" not in page
    # the head's restart asks its question in the page's dialog, its message as it was
    assert 'action="/servers/srv/test/restart" data-confirm="Restart Odoo of' in page
    assert re.search(r'action="/servers/srv/test/stop" data-confirm="Stop Odoo of [^"]+" data-confirm-danger>', page)
