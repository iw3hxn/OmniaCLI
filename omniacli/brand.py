# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The panel under another name (README, Whitelabel): the name shown in the
header, in the titles of the pages and in the emails, a logo, and the
primary colour, on the panel, the customers' portal and the login pages.
Nothing set is today's look exactly: no style is added, the name is
OmniaCLI, the logo the OmniaSolutions «O».

The colour is used as the light theme uses its red (docs/ux/UX-017): under
white text (the primary buttons) and as the focus ring (--accent-strong);
the links stay in the colour of the text and what destroys stays red. It is
refused when white text on it is below WCAG AA (4.5:1), so as a ring it is
at least 4.5:1 on the panel and 4.1:1 on the page's grey (3:1 is needed). The dark theme
takes a lighter tint of it (mixed with white until it reaches AA against
the dark backgrounds of the dark theme and of the top bar, and under dark
text, which its primary buttons then carry), as it takes #F87171 for #DC2626.

The logo: a PNG or an SVG of at most LOGO_MAX bytes, kept in LOGO_FOLDER
next to the options (mode 600), served at LOGO_ROUTE without login (the
login page shows it) with a policy that runs nothing of it. An SVG with a
script, an event handler, an embedded document, an outside reference or a
DOCTYPE is refused."""

from __future__ import annotations

import os
import re
import struct
import xml.etree.ElementTree as ET
from pathlib import Path

DEFAULT_NAME = "OmniaCLI"
NAME_MAX = 40
COLOR = re.compile(r"#[0-9A-Fa-f]{6}")
# WCAG 2.x AA for normal text
AA = 4.5
WHITE = "#FFFFFF"
# the dark theme's backgrounds (base.html: --panel, --panel-2, --bg), the top bar's (--side-bg) and its text on a
# primary button (--text of the light theme)
DARK_GROUNDS = ("#161B22", "#1C222B", "#0E1117", "#0F172A", "#111827")
LOGO_FOLDER = "brand"
LOGO_MAX = 256 * 1024
LOGO_SIDE_MAX = 4096
LOGO_ROUTE = "/brand/logo"
LOGO_TYPES = {"png": "image/png", "svg": "image/svg+xml"}
# what an SVG must not hold: it runs, it fetches, it declares
_SVG_REFUSED = re.compile(
    r"<\s*(?:script|foreignObject|iframe|object|embed|use|image|feImage|animate|set|handler|listener)\b"
    r"|<!DOCTYPE|<!ENTITY|\bon[a-z]+\s*=|javascript:|data:(?!image/(?:png|jpeg|gif|webp);base64,)"
    r"|(?:xlink:)?href\s*=\s*[\"'](?!#)|url\(\s*[\"']?(?!#)|@import", re.I)


class BrandRefused(ValueError):
    """A name, a colour or a logo refused: why, said to the person."""


def check_name(value) -> str:
    """The panel's name: one line of at most NAME_MAX characters, "" for OmniaCLI's own."""
    name = str(value or "").strip()
    if len(name) > NAME_MAX or any(ord(c) < 32 or c in "<>" for c in name):
        raise BrandRefused(f"The name of the panel: one line of at most {NAME_MAX} characters, without < or >")
    return "" if name == DEFAULT_NAME else name


def _channel(value: int) -> float:
    c = value / 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def luminance(color: str) -> float:
    """The relative luminance of #RRGGBB (WCAG 2.x)."""
    r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast(one: str, other: str) -> float:
    a, b = sorted((luminance(one), luminance(other)), reverse=True)
    return (a + 0.05) / (b + 0.05)


def mix(color: str, other: str, share: float) -> str:
    """share of other mixed into color, as #RRGGBB."""
    parts = [round(int(color[i:i + 2], 16) * (1 - share) + int(other[i:i + 2], 16) * share) for i in (1, 3, 5)]
    return "#" + "".join(f"{p:02X}" for p in parts)


def dark_tint(color: str) -> str:
    """color mixed with white, the least that reaches AA against every dark ground."""
    for step in range(0, 101):
        tint = mix(color, WHITE, step / 100)
        if all(contrast(tint, ground) >= AA for ground in DARK_GROUNDS):
            return tint
    return WHITE


def check_color(value) -> str:
    """The primary colour, #RRGGBB in capitals; "" for OmniaCLI's own. Refused
    when white text on it is below AA."""
    color = str(value or "").strip()
    if not color:
        return ""
    if not COLOR.fullmatch(color):
        raise BrandRefused(f"{color!r} is not a colour #RRGGBB (for example #0B5FFF)")
    color = color.upper()
    ratio = contrast(color, WHITE)
    if ratio < AA:
        raise BrandRefused(f"The colour {color} has a contrast of {ratio:.2f}:1 with white text: at least 4.5:1 is "
                           "needed (WCAG AA). Choose a darker one")
    return color


def facts(color: str) -> dict:
    """What the page shows of a colour: its ratio with white text, its dark tint and that tint's least ratio."""
    if not color:
        return {}
    tint = dark_tint(color)
    return {"color": color, "white": round(contrast(color, WHITE), 2), "dark": tint,
            "dark_ratio": round(min(contrast(tint, ground) for ground in DARK_GROUNDS), 2)}


def css(color: str) -> str:
    """The style the pages add for a colour; "" for none (today's look, nothing added)."""
    if not color:
        return ""
    tint = dark_tint(color)
    soft = mix(color, WHITE, 0.92)
    dark_soft = mix(tint, "#161B22", 0.85)
    dark = (f"--accent: {tint}; --accent-soft: {dark_soft}; --brand-on-accent: #111827; --on-accent: #111827; "
            f"--accent-strong: {tint};")
    # the top bar is dark in both themes: its marks take the tint, the avatar's letter is dark on it
    return (f":root {{ --accent: {color}; --accent-soft: {soft}; --brand-top: {tint}; --brand-on-accent: #fff; "
            f"--on-accent: #fff; --accent-strong: {color}; }}\n"
            f":root[data-theme=\"dark\"] {{ {dark} }}\n"
            f"@media (prefers-color-scheme: dark) {{ :root:not([data-theme=\"light\"]) {{ {dark} }} }}\n"
            ".brand-name span { color: var(--brand-top); }\n"
            ".nav-link[aria-current=\"page\"] { box-shadow: inset 3px 0 0 var(--brand-top); }\n"
            "@media (min-width: 768px) { .nav-link[aria-current=\"page\"] { box-shadow: inset 0 -2px 0 var(--brand-top); } }\n"
            ".avatar { background: var(--brand-top); color: #111827; }\n"
            "button.primary:not(.danger), .button.primary, button.primary:not(.danger):hover, .button.primary:hover,"
            " main.card button:not(.other), main.card a.button { color: var(--brand-on-accent); }\n")


# --- the logo


def logo_kind(data: bytes) -> str:
    """png or svg, from what the bytes are; refused otherwise, or too big, or an SVG that could run or fetch."""
    if not data:
        raise BrandRefused("The logo is empty")
    if len(data) > LOGO_MAX:
        raise BrandRefused(f"The logo is {len(data) // 1024} kB: at most {LOGO_MAX // 1024} kB")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        if len(data) < 24 or data[12:16] != b"IHDR":
            raise BrandRefused("The logo is not a readable PNG")
        width, height = struct.unpack(">II", data[16:24])
        if not (0 < width <= LOGO_SIDE_MAX and 0 < height <= LOGO_SIDE_MAX):
            raise BrandRefused(f"The logo is {width}×{height} pixels: at most {LOGO_SIDE_MAX} on each side")
        return "png"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise BrandRefused("The logo is neither a PNG nor an SVG") from None
    if "<svg" not in text[:4096].lower():
        raise BrandRefused("The logo is neither a PNG nor an SVG")
    found = _SVG_REFUSED.search(text)
    if found:
        raise BrandRefused(f"The SVG holds {found.group(0).strip()[:30]!r}: a logo without scripts, events, "
                           "embedded documents or outside references is needed")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise BrandRefused(f"The SVG is not well formed: {exc}") from None
    if root.tag.rsplit("}", 1)[-1] != "svg":
        raise BrandRefused("The logo is neither a PNG nor an SVG")
    return "svg"


def logo_folder(options_file: Path) -> Path:
    return options_file.parent / LOGO_FOLDER


def logo_path(options_file: Path) -> Path | None:
    """The logo kept, None when there is none."""
    folder = logo_folder(options_file)
    for kind in LOGO_TYPES:
        path = folder / f"logo.{kind}"
        if path.is_file() and not path.is_symlink():
            return path
    return None


def save_logo(options_file: Path, data: bytes) -> str:
    """The logo written (checked first, mode 600, the other kind taken away): its kind."""
    kind = logo_kind(data)
    folder = logo_folder(options_file)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    target = folder / f"logo.{kind}"
    tmp = folder / f".logo.{kind}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.chmod(tmp, 0o600)
    os.replace(tmp, target)
    for other in LOGO_TYPES:
        if other != kind:
            (folder / f"logo.{other}").unlink(missing_ok=True)
    return kind


def remove_logo(options_file: Path) -> bool:
    """The logo taken away; whether there was one."""
    gone = False
    for kind in LOGO_TYPES:
        path = logo_folder(options_file) / f"logo.{kind}"
        if path.exists() or path.is_symlink():
            path.unlink()
            gone = True
    return gone


def logo_version(options_file: Path) -> str:
    """A word that changes with the logo (its time and size): the pages ask it again when it changes."""
    path = logo_path(options_file)
    if path is None:
        return ""
    found = path.stat()
    return f"{int(found.st_mtime)}-{found.st_size}"
