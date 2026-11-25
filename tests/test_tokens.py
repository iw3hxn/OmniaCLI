# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The design tokens (docs/ux/UX-017-token-e-scale.md): one file, included by
every page, the login and the invitation; the dark theme written once; the
contrasts of §3 computed from the file itself, light and dark."""

import json
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
    the invitation (and the one-time link of a second step, its sibling) also their own primary and the brand's
    mark; never a link, a tab, a chosen state."""
    wrong = []
    for page in TEMPLATES.glob("*.html"):
        if page.name == "_tokens.html":
            continue
        text = page.read_text(encoding="utf-8")
        for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", text):
            if re.search(r"var\(--accent(?:-hover|-soft)?\)", body):
                selector = " ".join(selector.split())[-120:]
                if "primary" in selector or page.name in ("login.html", "invite.html", "second_factor_link.html") and (
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


def test_every_dialog_has_its_x_and_its_title():
    """UX-017 §5.5: the X as the last child of every dialog of the templates (named and helped), the dialog named by
    its title; the recovery codes of the login excepted, a dialog that only closes by saying the codes are kept."""
    missing, unnamed, total = [], [], 0
    for page in TEMPLATES.glob("*.html"):
        text = page.read_text(encoding="utf-8")
        for found in re.finditer(r"<dialog\b([^>]*)>(.*?)</dialog>", text, flags=re.S):
            total += 1
            if 'id="recovery-dialog"' in found.group(1):
                continue
            if not found.group(2).rstrip().endswith("{{ dialog_x() }}"):
                missing.append(f"{page.name}: {found.group(1)[:40]}")
            labelled = re.search(r'aria-labelledby="([^"]+)"', found.group(1))
            if not labelled or f'id="{labelled.group(1)}"' not in found.group(2):
                unnamed.append(f"{page.name}: {found.group(1)[:40]}")
    assert total >= 53 and not missing and not unnamed, (missing, unnamed)
    icons = (TEMPLATES / "_icons.html").read_text(encoding="utf-8")
    x = icons[icons.index("{% macro dialog_x() -%}"):icons.index("{%- endmacro %}", icons.index("{% macro dialog_x() -%}"))]
    assert "aria-label=\"{{ _('Close') }}\"" in x and "title=\"{{ _('Closes without changing anything') }}\"" in x
    assert 'type="button"' in x and 'href="#i-x"' in x
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    # the X does what Esc does: the cancel event first, a dialog that refuses it stays open; omniaAsk's too
    assert 'dialog.dispatchEvent(new Event("cancel", {cancelable: true}))) dialog.close()' in base
    assert "{{ dialog_x() | tojson }}" in base and 'dialog.setAttribute("aria-labelledby", "omnia-ask-text")' in base
    # a sheet from the bottom on a phone, centred from 576 px
    assert "dialog { position: fixed; inset: auto 0 0 0; margin: 0; width: 100%;" in base


def test_one_look_and_one_keyboard_for_the_tabs():
    """UX-017 §5.3: the tabs' style once (base.html), the active one bold on a neutral line; one script for every
    tablist: arrows, Home, End, a roving tabindex, the panels named by their tab; chosen by Enter or Space only."""
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    for page in TEMPLATES.glob("*.html"):
        if page.name != "base.html":
            assert ".tabs button[aria-selected" not in page.read_text(encoding="utf-8"), page.name
    assert '.tabs button[aria-selected="true"] { color: var(--text); font-weight: var(--fw-bold); border-bottom-color: var(--selected); }' in base
    for needed in ("ArrowRight:", "ArrowLeft:", "Home:", "End:", "t.tabIndex = t === chosen ? 0 : -1",
                   'panel.setAttribute("role", "tabpanel")', 'panel.setAttribute("aria-labelledby", t.id)'):
        assert needed in base, needed


GLYPHS = re.compile(r"[✓✔✗✘✕⚠⛔↗▾🔔☰☾☀⟳↻⏳⋯←↑↓■○●]")


def test_icons_not_glyphs():
    """UX-017 §5.9: Lucide's symbols in one sprite, its licence with it; every icon used is in it; no character or
    emoji stands for an icon in the templates nor in the Italian texts (the map keeps its signs: D2)."""
    sprite = (TEMPLATES / "_sprite.html").read_text(encoding="utf-8")
    assert "Lucide License (ISC)" in sprite and "Feather (MIT License)" in sprite and "http" not in sprite.split("-#}")[-1].replace(
        'xmlns="http://www.w3.org/2000/svg"', "")
    symbols = set(re.findall(r'<symbol id="i-([\w-]+)"', sprite))
    used = set()
    for page in TEMPLATES.glob("*.html"):
        text = page.read_text(encoding="utf-8")
        used |= set(re.findall(r'href="#i-([\w-]+)"', text)) | set(re.findall(r'omniaIcon\(\s*"([\w-]+)"', text))
        used |= {m for m in re.findall(r'\bicon\("([\w-]+)"', text) if m not in ("github", "python")}
        if page.name in ("_map.html", "_sprite.html"):
            continue
        assert not GLYPHS.search(text), (page.name, GLYPHS.search(text).group(0))
    aliases = {"start": "play", "stop": "square", "restart": "rotate-cw", "backup": "database", "production": "rocket", "test": "flask-conical"}
    missing = {aliases.get(name, name) for name in used} - symbols
    assert not missing, missing
    for catalog in (TEMPLATES.parent / "i18n" / "it").glob("*.json"):
        if catalog.name in ("map.json", "python.json"):
            continue
        for key, value in json.loads(catalog.read_text(encoding="utf-8")).items():
            assert not GLYPHS.search(key) and not GLYPHS.search(value), (catalog.name, key)
    # the about page names it among the third parties
    assert "https://lucide.dev" in (TEMPLATES / "about.html").read_text(encoding="utf-8")


def _css_rules():
    """(template, selector, declarations) of every <style> block and style="" attribute but the tokens'."""
    for page in TEMPLATES.glob("*.html"):
        if page.name in ("_tokens.html", "_sprite.html"):
            continue
        text = page.read_text(encoding="utf-8")
        for block in re.findall(r"<style[^>]*>(.*?)</style>", text, flags=re.S):
            for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", block):
                yield page.name, " ".join(selector.split()), body
        for body in re.findall(r'style="([^"]*)"', text):
            yield page.name, "", body


SVG_TEXT = re.compile(r"map-canvas|map-flow|chart-axis|chart-capacity|chart-empty|svg\b|(?:^|[\s>+~,])(?:text|tspan)(?![\w-])|svg\.spark")


def test_the_type_scale():
    """UX-017 §2.5: font sizes only from the scale (the text drawn inside an SVG excepted, §4.7), weights 400, 600,
    700, line heights only the five of the scale or none (0 and 1 on what holds no text)."""
    wrong = []
    for name, selector, body in _css_rules():
        if SVG_TEXT.search(selector):
            continue
        for value in re.findall(r"(?<![\w-])font-size\s*:\s*([^;\"}!]+)", body):
            if not re.fullmatch(r"var\(--fs-(12|14|16|18|20|24)\)|inherit|100%", value.strip()):
                wrong.append(f"{name} {selector[-40:]} font-size {value}")
        for value in re.findall(r"(?<![\w-])font-weight\s*:\s*([^;\"}!]+)", body):
            if not re.fullmatch(r"var\(--fw-(regular|semibold|bold)\)|inherit", value.strip()):
                wrong.append(f"{name} {selector[-40:]} font-weight {value}")
        for value in re.findall(r"(?<![\w-])line-height\s*:\s*([^;\"}!]+)", body):
            if not re.fullmatch(r"var\(--lh-(18|20|24|28|32)\)|0|1|normal|inherit", value.strip()):
                wrong.append(f"{name} {selector[-40:]} line-height {value}")
    assert not wrong, wrong[:20]


def test_the_closed_scales():
    """UX-017 §2.6-2.9 and A9: no colour written in a template but in the tokens (and the marks of github and python);
    spaces on the scale (no 3, 5, 7, 9, 11, 13, 14, 18, 22, 26, 28, 30 px), radii only the tokens, 0 and 50%; the
    durations of a transition the tokens' (the job's progress, a datum, excepted); no loop without end (5 s at
    most, then still); the media queries on the four breakpoints; one rule for less motion."""
    wrong = []
    off_scale = {"3", "5", "7", "9", "11", "13", "14", "18", "22", "26", "28", "30"}
    breakpoints = {"576px", "768px", "992px", "1200px", "575.98px", "767.98px", "991.98px", "1199.98px"}
    for name, selector, body in _css_rules():
        if re.search(r"(?<![&\w])#[0-9A-Fa-f]{3,8}\b", body) and not (name == "_icons.html"):
            wrong.append(f"{name} {selector[-40:]}: a colour")
        for prop, value in re.findall(r"(?<![\w-])((?:padding|margin|gap|row-gap|column-gap|inset)(?:-[a-z-]+)?)\s*:\s*([^;\"}]+)", body):
            if set(re.findall(r"(?<![\w.-])(\d+)px", value)) & off_scale:
                wrong.append(f"{name} {selector[-40:]}: {prop} {value}")
        for value in re.findall(r"(?<![\w-])border(?:-[a-z]+)*-radius\s*:\s*([^;\"}]+)", body):
            if not all(re.fullmatch(r"var\(--r(?:-sm|-lg|-pill)?\)|0|50%|inherit", p) for p in value.replace("!important", "").split()):
                wrong.append(f"{name} {selector[-40:]}: radius {value}")
        for value in re.findall(r"(?<![\w-])transition(?:-duration)?\s*:\s*([^;\"}]+)", body):
            if re.search(r"(?<![\w-])[\d.]+m?s\b", value) and "1.4s linear" not in value:
                wrong.append(f"{name} {selector[-40:]}: transition {value}")
        if re.search(r"(?<![\w-])animation(?:-iteration-count)?\s*:[^;\"}]*\binfinite\b", body):
            wrong.append(f"{name} {selector[-40:]}: a loop without end")
    for page in TEMPLATES.glob("*.html"):
        text = page.read_text(encoding="utf-8")
        for query in re.findall(r"@media\s*([^{]+)\{", text):
            for width in re.findall(r"(?:min|max)-width\s*:\s*([\d.]+px)", query):
                if width not in breakpoints:
                    wrong.append(f"{page.name}: @media {query.strip()}")
        if page.name != "_tokens.html":
            assert "@media (prefers-reduced-motion" not in text, page.name
    assert not wrong, wrong[:20]
    assert TOKENS.count("@media (prefers-reduced-motion: reduce)") == 1


def test_the_tables_say_what_they_hold():
    """UX-017 §5.4: every header cell of the templates scoped; the headers in sentence case at 14; the tables named
    by the title of their part of the page, also those the scripts draw (base.html's script)."""
    unscoped = []
    for page in TEMPLATES.glob("*.html"):
        unscoped += [f"{page.name}: {m.group(0)[:50]}" for m in re.finditer(r"<th(?=[\s>])[^>]*>", page.read_text(encoding="utf-8"))
                     if "scope=" not in m.group(0)]
    assert not unscoped, unscoped[:10]
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    header = re.search(r"\nth \{([^}]*)\}", base).group(1)
    assert "text-transform: none" in header and "var(--fs-14)" in header and "uppercase" not in header
    assert 'table.setAttribute("aria-labelledby", title.id)' in base and 'th.setAttribute("scope"' in base


def test_a_field_says_what_it_wants_and_what_is_wrong():
    """UX-017 §5.6: a hint beside a field is read with it; a field refused says why under it (aria-invalid, the
    message tied to it, the first one focused), and typing takes it away; boxes to tick 18 px in the neutral colour."""
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    for needed in ('field.setAttribute("aria-invalid", "true")', "describe(field, note)", 'document.addEventListener("invalid"',
                   "window.omniaFieldError = ", "first === field", 'field.removeAttribute("aria-invalid")'):
        assert needed in base, needed
    assert re.search(r"input\[type=checkbox\], input\[type=radio\] \{ width: var\(--checkbox\) !important; height: var\(--checkbox\) !important;[^}]*accent-color: var\(--selected\)", base)
    assert "--checkbox: 18px" in TOKENS


def test_menus_close_and_reading_shows_its_place():
    """UX-017 §5.10, §5.12: one script closes the menus that open from a button (Esc gives the focus back, a click
    elsewhere); their chevron turns; what is read shows grey lines in its place, the words said at once and shown
    after 5 s; no pulse beyond 5 s."""
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert 'const MENUS = "details.more-menu, details.row-menu, details.m-more"' in base
    assert 'menu.querySelector("summary")?.focus()' in base and "rotate(180deg)" in base
    assert 'setTimeout(() => note.classList.add("reading-slow"), 5000)' in base
    assert "skeleton-pulse var(--dur-400) var(--ease-in-out) 12 alternate" in base
    for name, marker in (("_machine_database.html", 'id="pg-loading" data-skeleton='), ("_modules_tab.html", 'id="mods-line" data-skeleton='),
                         ("backups.html", '<tbody id="bk-rows"><tr aria-hidden="true"><td colspan="6"><span class="skeleton-line')):
        assert marker in (TEMPLATES / name).read_text(encoding="utf-8"), name


async def test_the_header_on_a_phone_leaves_nothing_out(client):  # noqa: F811
    """UX-017 §5.11: under 768 px your account, the language and Log out are in the menu, never nowhere."""
    page = (await client.get("/", headers={"Authorization": AUTH, "Accept": "text/html"})).text
    menu = page[page.index('<nav class="topnav"'):page.index("</nav>", page.index('<nav class="topnav"'))]
    more = menu[menu.index('class="topnav-more"'):]
    assert 'action="/logout"' in more and 'href="/language/it?' in more and 'href="/account"' in more
    # in the bar too (shown from 768 px); no width hides them any more
    end = page[page.index('<div class="top-end">'):page.index("</header>")]
    assert 'action="/logout"' in end and 'href="/language/it?' in end and 'href="/account"' in end
    assert "@media (max-width: 560px)" not in page and ".top-end .side-account, .top-end .logout { display: flex; }" in page



# the rules that may paint their words in the warning's colour: each says it with a mark too (an icon, the dot of a
# state, a badge's or a line's triangle), or is a step of a job, or the map's (D2)
WARN_COLOURED = {
    ("base.html", ".status.running, .status.queued, .status.warned"), ("base.html", ".warn-line, .status-warning"),
    ("base.html", ".job-running"), ("base.html", ".disk-head .warn"), ("base.html", ".disk-note.warn"),
    ("base.html", ".disk-compact .warn"), ("base.html", ".badge.draft, .badge.warn"), ("base.html", ".bell-list .mark"),
    ("_machine_database.html", ".pg-table .pg-sub.warn-line"), ("_new_instance_dialog.html", ".ni-help.warn-line"),
    ("_new_instance_dialog.html", ".ni-server .ni-why.warn-line"), ("_receive_dialog.html", "#receive-dialog .rv-warn"),
    ("instances.html", ".inst-backup.warn, .inst-backup.warn a"), ("job.html", ".job-warned"), ("job.html", ".step.active"),
    ("machines.html", ".m-state.starting, .m-state.preparing"), ("machines.html", ".m-rows .m-pct.warn"),
    ("server.html", ".repo-state .warn-text"), ("workspaces.html", ".ws-rows .ws-warn"), ("workspaces.html", ".ws-machine-row td"),
    ("_map.html", ".map-verdict.watch"), ("_map.html", ".map-watch .mark"), ("_map.html", ".map-job.warned .sign"),
    ("_map.html", ".map-strip .step.active"),
}


def test_a_warning_is_a_banner_or_a_marked_line():
    """UX-017 §5.8 and §7: no loose orange paragraph left. A warning that is a paragraph of its own is a .banner.warn
    (its border, its triangle, its words in the colour of the text); one said in a line of another place (a cell, a
    fact, a line under a value) is a .warn-line, the triangle before its words; no other rule paints a text orange."""
    painted = set()
    for name, selector, body in _css_rules():
        if re.search(r"(?<![\w-])color\s*:\s*var\(--warn\)", body):
            assert selector, f"{name}: a colour written in a style attribute"
            painted.add((name, selector))
    assert painted == WARN_COLOURED, (painted - WARN_COLOURED, WARN_COLOURED - painted)
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert re.search(r"\.warn-line::before, \.status-warning::before \{[^}]*mask: var\(--m-triangle\)", base)
    assert re.search(r"\.banner\.warn \{[^}]*--mark: var\(--m-triangle\)", base)
    for page in TEMPLATES.glob("*.html"):
        text = page.read_text(encoding="utf-8")
        # a paragraph, a box or a list's line painted orange without its mark: the classes of before are gone
        for gone in ("pg-warn", 'class="fb-off"', "ni-once", "mods-alpha", 'class="mods-licence"', "ab-line off", "pub-warn", "--warn-text"):
            assert gone not in text, (page.name, gone)
        assert not re.search(r'el\("p", \{class(?:Name)?: "warn"', text), page.name
    # the paragraphs of before, each a warning banner now
    for name, marker in (("backups.html", '<p class="banner warn fb-off" id="fb-staging-off"'),
                         ("backups.html", '<p class="banner warn fb-off" id="fb-instance-off"'),
                         ("backups.html", 'lines.push(el("p", {class: "banner warn"}, p.passwords))'),
                         ("_machine_database.html", 'box.append(el("p", {className: "banner warn", textContent: fill(T.rp.replacedStill'),
                         ("_machine_database.html", 'for (const c of ["banner", "warn"]) trip.classList.toggle(c, slow);'),
                         ("_machine_database.html", 'for (const c of ["banner", "warn"]) changes.classList.toggle(c, !!f.restarts);'),
                         ("_machine_database.html", 'for (const c of ["banner", "warn"]) restart.classList.toggle(c, !a.enabled && archiveRestarts(row));'),
                         ("_modules_tab.html", '<div class="banner warn mods-licence" id="mods-licence"'),
                         ("server.html", 'line.className = "ab-line" + (data.enabled ? "" : " banner warn");'),
                         ("workspace.html", '<p class="banner warn" id="rd-rtt-warn"'),
                         ("_new_instance_dialog.html", '<div class="ni-help warn-line">'),
                         ("_modules_tab.html", 'paid.className = "badge warn";'), ("_modules_tab.html", 'alpha.className = "badge warn";')):
        assert marker in (TEMPLATES / name).read_text(encoding="utf-8"), (name, marker)
    # the values beside a meter and the verdict of the map say their level with a mark too
    assert "marked(spaceLevel(d), node(" in (TEMPLATES / "_disk.html").read_text(encoding="utf-8")
    assert 'window.omniaIcon(lv === "warn" ? "triangle-alert" : "circle-x")' in (TEMPLATES / "machines.html").read_text(encoding="utf-8")
    assert 'verdict.replaceChildren(window.omniaIcon(watch.length ? "triangle-alert" : "circle-check")' in (TEMPLATES / "_map.html").read_text(encoding="utf-8")


def test_badges_carry_their_mark():
    """UX-017 §5.7: a badge 12/18 semibold, padding 2/8, a pill; a good, a warned, a failed and (Carlo, 24/11/2026) an
    informative one draw their icon (check, triangle, circle-x, info) from the CSS, in the colour of their words."""
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    badge = re.search(r"\n\.badge \{([^}]*)\}", base).group(1)
    assert "padding: 2px 8px" in badge and "var(--fs-12)" in badge and "var(--r-pill)" in badge and "var(--lh-18)" in badge
    assert re.search(r"\.badge\.ok::before, \.badge\.active::before, \.badge\.warn::before, \.badge\.draft::before, "
                     r"\.badge\.error::before, \.badge\.err::before,\s*\.badge\.info::before \{[^}]*background-color: currentColor", base)
    for state, mark in (("ok", "--m-check"), ("warn", "--m-triangle"), ("error", "--m-circle-x"), ("info", "--m-info")):
        assert re.search(rf"\.badge\.{state}[^{{]*\{{ --mark: var\({mark}\); \}}", base), state
    # no badge of a template pads itself apart
    for name, selector, body in _css_rules():
        assert selector == ".badge" or not (re.search(r"\.badge(?![\w-])", selector) and re.search(r"(?<![\w-])padding", body)), (name, selector)


def test_what_is_read_shows_grey_lines_everywhere_alike():
    """UX-017 §5.12: the instance's Backup tab reads with grey lines too; the skeletons made by hand before (the
    deploys, the map, the workspaces) use the same two classes; a list drawn in the place of the line leaves its box
    no longer busy."""
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert "note.isConnected && !note.hidden" in base and "new MutationObserver(check).observe(box, {childList: true})" in base
    server = (TEMPLATES / "server.html").read_text(encoding="utf-8")
    assert '<div class="backup-list"><p class="muted" data-skeleton="3">' in server
    for name, marker in (("_environments.html", '<span class="skeleton-line w-80"></span><span class="skeleton-line w-60"></span>'),
                         ("_map.html", '<span class="skeleton-block" style="--h: 150px"></span>'),
                         ("workspaces.html", '<td><span class="skeleton-line"></span></td>')):
        assert marker in (TEMPLATES / name).read_text(encoding="utf-8"), name
    for name, selector, body in _css_rules():
        assert not re.search(r"\.skeleton span|map-skeleton div|ws-skel td div", selector), (name, selector)


# the fields that keep their help in a title: the filters of a list (their placeholder and label say what they do),
# a box ticked in a row of a table, the colour picker named by it, the two ways of adding a repository (their words
# are the choice), the options locked by the Mail tab (said on the page above them) and a choice in a row of three
KEEP_TITLE = {
    "_environments.html": 1, "_list.html": 1, "_machine_database.html": 1, "_modules_tab.html": 1, "_odoo_log.html": 3,
    "access.html": 4, "options.html": 2, "server.html": 7, "server_form.html": 1,
}


def test_a_field_shows_its_help():
    """UX-017 §5.13 rule 2: the help of a field is written under it (.hint, read with it by aria-describedby), not
    only in a title nobody sees on a phone; every hint with an id is the description of its field."""
    kept = {}
    for page in TEMPLATES.glob("*.html"):
        text = page.read_text(encoding="utf-8")
        for m in re.finditer(r'<(?:input|select|textarea)\b((?:[^>"]|"[^"]*")*)>', text):
            attrs = m.group(1)
            if 'type="hidden"' not in attrs and re.search(r'(?<![\w-])title="', attrs):
                kept[page.name] = kept.get(page.name, 0) + 1
        said = re.findall(r'aria-describedby="([^"]+)"', text)
        described = set(said) | set(" ".join(said).split())
        for hint in re.findall(r'<small class="hint" id="([^"]+)"', text):
            assert hint in described, (page.name, hint)
    assert kept == KEEP_TITLE, kept
    assert "flex-basis: 100%" in re.search(r"\n\.hint \{([^}]*)\}", (TEMPLATES / "base.html").read_text(encoding="utf-8")).group(1)
    assert 'id="op-mail-said"' in (TEMPLATES / "server.html").read_text(encoding="utf-8")


def test_a_dangerous_question_says_its_consequence():
    """UX-017 §5.13 rule 3: what the help of a dangerous button says that cannot be undone is in its question too."""
    assert "that instance gets in again only with a new token" in (TEMPLATES / "users.html").read_text(encoding="utf-8")
    assert "on the other instance its token stays valid until you revoke it there" in (TEMPLATES / "links.html").read_text(encoding="utf-8")


def _italian():
    found = {}
    for catalog in (TEMPLATES.parent / "i18n" / "it").glob("*.json"):
        found.update(json.loads(catalog.read_text(encoding="utf-8")))
    return found


def test_a_hint_is_one_line_and_the_rest_a_toggletip():
    """UX-017 §5.13 (Carlo, 24/11/2026): a field's hint on the Options and on the database of a new instance on another
    machine is one line (80 characters in English, 95 in Italian); what it said more is in the «?» of its part, at most
    one a part: a dialog that does not block the page, named, with its X, opened and closed by its «?» (aria-expanded),
    by the X, Esc and a click elsewhere, its text at most about 350 characters."""
    italian = _italian()
    icons = (TEMPLATES / "_icons.html").read_text(encoding="utf-8")
    macro = icons[icons.index("{% macro toggletip("):icons.index("{%- endmacro %}", icons.index("{% macro toggletip("))]
    for needed in ('class="icon-btn toggletip-open" aria-expanded="false" aria-controls="{{ id }}"', 'role="dialog"', "hidden",
                   'class="icon-btn toggletip-x" aria-label="{{ _(\'Close\') }}"', "#i-circle-help"):
        assert needed in macro, needed
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    for needed in ('button.setAttribute("aria-expanded", "true")', 'target.closest(".toggletip-x")', "close(box, true);\n  }, true);",
                   "max-width: min(320px, calc(100vw - 32px))"):
        assert needed in base, needed
    options = (TEMPLATES / "options.html").read_text(encoding="utf-8")
    workspace = (TEMPLATES / "workspace.html").read_text(encoding="utf-8")
    remote = workspace[workspace.index('<div id="remote-fields"'):workspace.index('<div id="from-archive"')]
    relay = workspace[workspace.index('<div id="names-form">'):workspace.index('id="relay-via-hint"') + 300]
    tips = 0
    for name, text in (("options.html", options), ("remote", remote), ("relay", relay)):
        for hint in re.findall(r'<small class="hint" id="[^"]+">\{\{ _\(\'((?:[^\'\\]|\\.)*)\'', text):
            hint = hint.replace("\\'", "'")
            assert len(hint) <= 80 and hint in italian and len(italian[hint]) <= 95, (name, hint)
        for said in re.findall(r'toggletip\("[^"]+", _\("[^"]*"\), _\(\'((?:[^\'\\]|\\.)*)\'\)\)', text):
            said = said.replace("\\'", "'")
            assert len(said) <= 360 and said in italian, (name, said[:40])
            tips += 1
    assert tips == 10
    # at most one a part of the Options
    for part in options.split("<section")[1:]:
        assert part.count("toggletip(") <= 1, part[:80]
    for msgid in ("More about {part}", "Opens a short explanation of this part; Esc closes it", "Closes this explanation"):
        assert msgid in italian, msgid


def test_the_arrows_move_in_a_menu():
    """UX-017 §5.10 (Carlo, 24/11/2026): in More, Actions and the menus of a machine and of an environment the arrows
    move the focus between the entries, round, Home and End to the first and the last; ↓ on a closed menu's button
    opens it on its first entry."""
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    script = base[base.index('const MENUS = "details.more-menu, details.row-menu, details.m-more"'):]
    script = script[:script.index("})();")]
    for needed in ('["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)', 'if (!onButton || event.key !== "ArrowDown") return;',
                   "(at + 1) % list.length", "(at - 1 + list.length) % list.length", "list[next].focus();", "event.preventDefault();"):
        assert needed in script, needed
