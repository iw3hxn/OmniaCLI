# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The design tokens (docs/ux/UX-017-token-e-scale.md): one file, included by
every page, the login and the invitation; the dark theme written once; the
contrasts of §3 computed from the file itself, light and dark."""

import re
from pathlib import Path

import pytest

from omniacli import brand

from .test_app import AUTH, client  # noqa: F401 - the client fixture

TEMPLATES = Path(__file__).resolve().parent.parent / "omniacli" / "templates"
TOKENS = (TEMPLATES / "_tokens.html").read_text(encoding="utf-8")


def _declarations(block: str) -> dict:
    return dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", block))


def palette(dark: bool) -> dict:
    """The tokens of a theme, every var() resolved; a value that is not a plain colour stays as it is."""
    light = _declarations(TOKENS[TOKENS.index(":root {"):TOKENS.index("color-scheme: light;")])
    values = dict(light)
    if dark:
        values.update(_declarations(TOKENS[TOKENS.index("{%- set dark -%}"):TOKENS.index("{%- endset -%}")]))

    def resolve(value: str, depth=0) -> str:
        value = value.strip()
        found = re.fullmatch(r"var\((--[\w-]+)\)", value)
        return resolve(values[found.group(1)], depth + 1) if found and depth < 10 else value
    return {name: resolve(value) for name, value in values.items()}


# §3: the pairs, the ground, the least ratio
PAIRS = [("--text", "--bg", 4.5), ("--text", "--panel", 4.5),
         ("--muted", "--bg", 4.5), ("--muted", "--panel", 4.5), ("--muted", "--panel-2", 4.5),
         ("--muted", "--err-soft", 4.5), ("--muted", "--warn-soft", 4.5),
         ("--border-input", "--panel", 3), ("--border-input", "--bg", 3), ("--border-input", "--panel-2", 3),
         ("--focus", "--panel", 3), ("--focus", "--bg", 3), ("--focus", "--panel-2", 3),
         ("--brand-top", "--side-bg", 3),
         ("--on-accent", "--accent", 4.5), ("--on-danger", "--danger", 4.5), ("--on-danger", "--danger-hover", 4.5),
         ("--err", "--panel", 4.5), ("--err", "--err-soft", 4.5), ("--ok", "--ok-soft", 4.5),
         ("--warn", "--warn-soft", 4.5), ("--info", "--info-soft", 4.5),
         ("--side-text", "--side-bg", 4.5), ("--side-text", "--side-hover", 4.5),
         ("--side-muted", "--side-bg", 4.5), ("--side-muted", "--side-hover", 4.5),
         ("--on-count", "--count-bg", 4.5), ("--on-selected", "--selected", 4.5)]


@pytest.mark.parametrize("dark", [False, True])
def test_the_contrasts_of_the_tokens(dark):
    tokens = palette(dark)
    low = []
    for fore, ground, least in PAIRS:
        ratio = brand.contrast(tokens[fore], tokens[ground])
        if ratio < least:
            low.append(f"{fore} on {ground}: {ratio:.2f} < {least}")
    assert not low, low
    # the numbers of the contract, light
    if not dark:
        assert round(brand.contrast(tokens["--muted"], tokens["--bg"]), 2) == 5.34
        assert round(brand.contrast(tokens["--border-input"], tokens["--panel"]), 2) == 3.30
        assert round(brand.contrast(tokens["--focus"], tokens["--panel"]), 2) == 6.47


def test_one_file_of_tokens_and_the_dark_written_once():
    # the semantic names are declared in _tokens.html only, and the dark values once (said twice)
    assert TOKENS.count("{{ dark }}") == 2 and TOKENS.count("{%- set dark -%}") == 1
    assert TOKENS.count("prefers-reduced-motion") == 1 and TOKENS.count("prefers-color-scheme") == 1
    for page in TEMPLATES.glob("*.html"):
        if page.name == "_tokens.html":
            continue
        text = page.read_text(encoding="utf-8")
        assert not re.search(r"(?<![\w-])--(?:bg|panel|text|muted|accent|border)\s*:", text), page.name
        assert "@media (prefers-color-scheme" not in text, page.name
    for name in ("base.html", "login.html", "invite.html"):
        assert '{% include "_tokens.html" %}' in (TEMPLATES / name).read_text(encoding="utf-8"), name
    # the whitelabel overrides the accent's only: never the danger, the neutrals, the links
    css = brand.css("#0B5FFF")
    assert "--accent-strong: #0B5FFF" in css and "--link" not in css
    assert not re.search(r"--(?:danger|err|ok|warn|info|text|muted|bg|panel|selected)\b", css)


async def test_the_pages_carry_the_tokens_once(client):  # noqa: F811
    page = (await client.get("/", headers={"Authorization": AUTH, "Accept": "text/html"})).text
    login = (await client.get("/login", headers={"Accept": "text/html"})).text
    for text in (page, login):
        assert text.count("--gray-50: #F5F6F8") == 1 and text.count("@media (prefers-color-scheme: dark)") == 1
        assert text.count(":focus-visible { outline: var(--focus-width) solid var(--focus)") == 1
        # no ring taken away without another one
        assert "outline: none" not in text.replace(".map-canvas a:focus, .map-canvas a:focus-visible { outline: none; }", "")


def test_red_is_the_primarys_and_the_dangers_only():
    """UX-017 §5.2: the accent (and its hover) only in the rules of the primary button; on the login and
    the invitation also their own primary and the brand's mark; never a link, a tab, a chosen state."""
    wrong = []
    for page in TEMPLATES.glob("*.html"):
        if page.name == "_tokens.html":
            continue
        text = page.read_text(encoding="utf-8")
        for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", text):
            if re.search(r"var\(--accent(?:-hover|-soft)?\)", body):
                selector = " ".join(selector.split())[-120:]
                if "primary" in selector or page.name in ("login.html", "invite.html") and (
                        selector.endswith(("button", "button:hover", "a.button", "h1 span"))):
                    continue
                wrong.append(f"{page.name}: {selector}")
    assert not wrong, wrong
    assert "--prod" not in (TEMPLATES / "_tokens.html").read_text(encoding="utf-8")
    # the links: the colour of the text
    assert "--link: var(--text);" in TOKENS


def test_four_buttons():
    """UX-017 §5.1: primary, secondary (the default), tertiary (.ghost), danger; 36 px (32 small, 44 at the
    touch), flat; no coloured variant of their own any more."""
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    rule = re.search(r"\nbutton, \.button \{([^}]*)\}", base).group(1)
    assert "min-height: var(--control-h)" in rule and "box-shadow" not in rule and "var(--fw-semibold)" in rule
    assert re.search(r"button\.small, \.button\.small \{[^}]*min-height: var\(--control-h-sm\)", base)
    assert re.search(r"\.icon-btn, button\.icon-btn \{[^}]*width: var\(--icon-btn\); height: var\(--icon-btn\)", base)
    assert "@media (pointer: coarse) { :root { --control-h: 44px; --control-h-sm: 44px; --icon-btn: 44px; } }" in TOKENS
    for gone in ("--start", "--stop", "--restart", "--light-green", "--purple", "btn-start", "btn-activate", "btn-clone",
                 ".button.current"):
        for page in TEMPLATES.glob("*.html"):
            assert gone not in page.read_text(encoding="utf-8"), (gone, page.name)
    # what destroys stays red under any brand: its own tokens, never the accent
    assert "button.primary.danger, button.primary.danger:hover:not(:disabled) { background: var(--danger);" in base
