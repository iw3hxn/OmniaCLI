# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Whitelabel (brand.py, README Whitelabel): the panel's name, logo and
primary colour on the panel, the customers' portal and the login pages;
a colour below WCAG AA refused; nothing set is today's look exactly."""

import json
import re
import stat
import struct
import zlib

import pytest

from omniacli import brand, notify
from omniacli.config import Options, load_options, parse_options, save_options

from .test_app import AUTH, POST, client  # noqa: F401 - the client fixture
from .test_workspaces import ORIGIN, basic, workspaces  # noqa: F401 - the workspaces fixture

PAGE = {"Authorization": AUTH, "Accept": "text/html"}
SAVE_BASE = {"shell": {"font_size": 14}, "tunnel": {"idle_hours": 4, "bind": "127.0.0.1", "ports": "18000-18099"}}


def png(width=2, height=2) -> bytes:
    """A small PNG that a browser draws (one row of red pixels per line)."""
    raw = b"".join(b"\x00" + b"\xdc\x26\x26" * width for _ in range(height))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def test_the_contrast_of_the_colour():
    # OmniaCLI's own red reaches AA with white text; its dark theme's red would not
    assert round(brand.contrast("#DC2626", "#FFFFFF"), 2) == 4.83 and brand.check_color("#dc2626") == "#DC2626"
    with pytest.raises(brand.BrandRefused, match=r"2\.77:1 with white text"):
        brand.check_color("#F87171")
    with pytest.raises(brand.BrandRefused, match="not a colour"):
        brand.check_color("blue")
    assert brand.check_color("") == ""
    # the dark theme takes a tint that reaches AA on every dark ground and under dark text
    for color in ("#0B5FFF", "#1D4ED8", "#047857", "#000000"):
        tint = brand.dark_tint(color)
        assert all(brand.contrast(tint, ground) >= brand.AA for ground in brand.DARK_GROUNDS), (color, tint)
    assert brand.facts("#0B5FFF")["white"] >= 4.5 and brand.facts("#0B5FFF")["dark_ratio"] >= 4.5
    assert brand.css("") == "" and "--accent: #0B5FFF" in brand.css("#0B5FFF")


def test_the_name():
    assert brand.check_name("  Acme Cloud ") == "Acme Cloud" and brand.check_name("OmniaCLI") == ""
    for bad in ("x" * 41, "a\nb", "<b>"):
        with pytest.raises(brand.BrandRefused):
            brand.check_name(bad)


def test_saved_and_read_again(tmp_path):
    options = parse_options({"brand": {"name": "Acme Cloud", "color": "#0b5fff"}})
    assert (options.brand_name, options.brand_color) == ("Acme Cloud", "#0B5FFF")
    save_options(tmp_path / "options.yaml", options)
    assert load_options(tmp_path / "options.yaml") == options
    # nothing set: nothing written
    save_options(tmp_path / "plain.yaml", Options())
    assert "brand" not in (tmp_path / "plain.yaml").read_text()


def test_the_logo_is_checked():
    assert brand.logo_kind(png()) == "png"
    assert brand.logo_kind(b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect width="10" height="10" fill="#0B5FFF"/></svg>') == "svg"
    for bad, said in ((b"", "empty"), (b"GIF89a", "neither"), (png(5000, 1)[:30], "5000"),
                      (b"x" * (brand.LOGO_MAX + 1), "at most"),
                      (b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', "script"),
                      (b'<svg xmlns="http://www.w3.org/2000/svg" onload="x()"></svg>', "onload"),
                      (b'<svg xmlns="http://www.w3.org/2000/svg"><image href="https://e.x/a.png"/></svg>', "image"),
                      (b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY a "b">]><svg/>', "DOCTYPE"),
                      (b"<svg><rect></svg>", "well formed")):
        with pytest.raises(brand.BrandRefused, match=said):
            brand.logo_kind(bad)


def test_the_emails_carry_the_name():
    named = Options(brand_name="Acme Cloud")
    assert notify.branded(named, "OmniaCLI lab3: [warning] the job x failed") == "Acme Cloud lab3: [warning] the job x failed"
    assert notify.branded(Options(), "OmniaCLI: your invitation") == "OmniaCLI: your invitation"


async def test_nothing_set_is_todays_look(workspaces, client):  # noqa: F811
    page = (await client.get("/", headers=PAGE)).text
    login = (await client.get("/login", headers={"Accept": "text/html"})).text
    for text in (page, login):
        # not even a blank line where the brand's style would be (compared with the release's pages too)
        assert "brand-style" not in text and "/brand/logo" not in text and "</style>\n</head>" in text
    assert '<title>OmniaCLI</title>' in page or "· OmniaCLI</title>" in page
    assert '<img src="/icon-180.png" alt=""><span class="brand-name">Omnia<span>CLI</span></span></a>' in page
    assert "<title>Login · OmniaCLI</title>" in login and "    <h1>Omnia<span>CLI</span></h1>\n" in login
    assert "--accent: #DC2626" in login and "--accent: #F87171" in login
    # a name, a colour and a logo set, then taken away: the same pages again, byte for byte
    body = {**SAVE_BASE, "brand": {"name": "Acme Cloud", "color": "#0B5FFF"}}
    assert (await client.post("/api/options/save", headers=POST, json=body)).status_code == 200
    assert (await client.post("/api/options/brand/logo", headers={**POST, "Content-Type": "image/png"}, content=png())).status_code == 200
    assert "Acme Cloud" in (await client.get("/login", headers={"Accept": "text/html"})).text
    body["brand"] = {"name": "", "color": ""}
    assert (await client.post("/api/options/save", headers=POST, json=body)).status_code == 200
    assert (await client.post("/api/options/brand/logo/delete", headers=POST, json={})).status_code == 200
    assert (await client.get("/login", headers={"Accept": "text/html"})).text == login
    assert (await client.get("/", headers=PAGE)).text == page


async def test_the_name_the_colour_and_the_logo_everywhere(workspaces, client):  # noqa: F811
    settings = workspaces
    body = {**SAVE_BASE, "brand": {"name": "Acme Cloud", "color": "#0b5fff"}}
    resp = await client.post("/api/options/save", headers=POST, json=body)
    assert resp.status_code == 200 and resp.json()["brand"]["color"] == "#0B5FFF"
    assert settings.options.brand_name == "Acme Cloud"
    logo = png(64, 64)
    resp = await client.post("/api/options/brand/logo", headers={**POST, "Content-Type": "image/png"}, content=logo)
    assert resp.status_code == 200 and resp.json()["kind"] == "png"
    kept = brand.logo_path(settings.options_file)
    assert kept.read_bytes() == logo and stat.S_IMODE(kept.stat().st_mode) == 0o600
    assert stat.S_IMODE(kept.parent.stat().st_mode) == 0o700
    # the logo without login (the login page shows it), nothing of it runs
    served = await client.get("/brand/logo")
    assert served.status_code == 200 and served.content == logo and served.headers["content-type"] == "image/png"
    assert "sandbox" in served.headers["content-security-policy"] and served.headers["x-content-type-options"] == "nosniff"
    # the panel, the portal (a customer's pages), the login page
    for text in ((await client.get("/options", headers=PAGE)).text, (await client.get("/", headers={**basic("mario"), "Accept": "text/html"})).text,
                 (await client.get("/login", headers={"Accept": "text/html"})).text):
        assert "Acme Cloud" in text and "--accent: #0B5FFF" in text and "/brand/logo?v=" in text
        assert "<title>" in text and "· Acme Cloud</title>" in text or "<title>Acme Cloud</title>" in text
    page = (await client.get("/", headers=PAGE)).text
    assert '<span class="brand-name">Acme Cloud</span>' in page and "Omnia<span>CLI</span>" not in page.split("</header>")[0]
    # the dark theme: the tint, under dark text on its primary buttons
    tint = brand.dark_tint("#0B5FFF")
    assert f"--accent: {tint}" in page and "--brand-on-accent: #111827" in page
    # a colour below AA: refused, said in Italian, nothing changed
    resp = await client.post("/api/options/save", headers={**POST, "Accept-Language": "it"},
                             json={**SAVE_BASE, "brand": {"name": "Acme Cloud", "color": "#F87171"}})
    assert resp.status_code == 400 and resp.json()["error"] == (
        "Il colore #F87171 ha un contrasto di 2.77:1 con il testo bianco: serve almeno 4,5:1 (WCAG AA). Scegline uno più scuro")
    assert settings.options.brand_color == "#0B5FFF"
    # an SVG that runs: refused; the PNG stays
    resp = await client.post("/api/options/brand/logo", headers={**POST, "Content-Type": "image/svg+xml"},
                             content=b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>')
    assert resp.status_code == 400 and "script" in resp.json()["error"] and kept.read_bytes() == logo
    # too big: refused while it is read
    resp = await client.post("/api/options/brand/logo", headers={**POST, "Content-Type": "image/png"},
                             content=b"\x89PNG" + b"x" * brand.LOGO_MAX)
    assert resp.status_code == 400
    # a customer may not change it
    resp = await client.post("/api/options/brand/logo", headers={**basic("mario"), "Origin": ORIGIN, "Content-Type": "image/png"},
                             content=logo)
    assert resp.status_code == 403
    # every change in the access log
    entries = [json.loads(line)["detail"] for line in (settings.logs_dir / "access.jsonl").read_text().splitlines()
               if "whitelabel" in line]
    assert any("name 'OmniaCLI' -> 'Acme Cloud', colour default -> #0B5FFF" in e for e in entries)
    assert any("logo set (PNG" in e for e in entries)


async def test_the_options_page_section(workspaces, client):  # noqa: F811
    page = (await client.get("/options", headers={**PAGE, "Accept-Language": "it"})).text
    section = page.split('id="whitelabel"', 1)[1].split("</section>", 1)[0]
    assert "Nome del pannello" in section and "Colore principale" in section and "Usa questo logo" in section
    assert 'class="primary' not in section
    for tag, text in re.findall(r"(<button\b[^>]*>)(.*?)</button>", section, re.S):
        title = re.search(r'title="([^"]*)"', tag)
        assert title and title.group(1) != text.strip(), tag
