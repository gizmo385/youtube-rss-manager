"""Colour themes the user can pick in Settings.

The colours themselves live in style.css, one ``:root[data-theme=...]`` block
per theme; base.html puts the user's choice on ``<html>``. This module is the
list of what exists, plus the few colours the server has to know: the browser
chrome colour (``<meta name="theme-color">``) and the swatches on the picker.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_THEME = "ember"
# Follows the device: Ember when it's set to dark, Paper when it's set to light.
AUTO_THEME = "auto"


@dataclass(frozen=True)
class Theme:
    key: str
    label: str
    hint: str
    bg: str
    panel: str  # also the browser chrome colour
    accent: str
    ink: str
    light: bool = False


THEMES: tuple[Theme, ...] = (
    Theme("ember", "Ember", "Warm near-black, orange", "#1a1918", "#201e1d", "#ff563c", "#f3f2f2"),
    Theme("slate", "Slate", "Cool grey, blue", "#15181c", "#1a1e23", "#5aa9ff", "#eef1f4"),
    Theme("rose", "Rose", "Plum-tinted dark, pink", "#1c1719", "#221c1f", "#ff6f9c", "#f5eff1"),
    Theme("midnight", "Midnight", "True black for OLED, violet", "#000000", "#0b0b0d", "#a98bff", "#f2f2f5"),
    Theme("paper", "Paper", "Warm light, red-orange", "#f6f4f1", "#fdfcfa", "#c2381f", "#1c1a19", light=True),
    Theme("mist", "Mist", "Cool light, blue", "#f2f4f7", "#fbfcfd", "#1a64c0", "#161a1f", light=True),
)

THEMES_BY_KEY: dict[str, Theme] = {t.key: t for t in THEMES}
THEME_KEYS: frozenset[str] = frozenset(THEMES_BY_KEY) | {AUTO_THEME}


def parse_theme(value: str | None) -> str:
    """A stored theme key; anything unknown falls back to the default."""
    text = (value or "").strip().lower()
    return text if text in THEME_KEYS else DEFAULT_THEME


def chrome_colors(key: str | None) -> list[tuple[str, str | None]]:
    """``(colour, media query)`` pairs for ``<meta name="theme-color">``.

    One unconditional colour for a fixed theme; for Auto, one per colour scheme
    so the browser chrome follows the device along with the page.
    """
    key = parse_theme(key)
    if key == AUTO_THEME:
        return [
            (THEMES_BY_KEY["ember"].panel, "(prefers-color-scheme: dark)"),
            (THEMES_BY_KEY["paper"].panel, "(prefers-color-scheme: light)"),
        ]
    return [(THEMES_BY_KEY[key].panel, None)]


def manifest_theme(key: str | None) -> Theme:
    """The theme whose colours go in the web app manifest, which can't vary
    by colour scheme: Auto installs with Ember's."""
    key = parse_theme(key)
    return THEMES_BY_KEY[DEFAULT_THEME if key == AUTO_THEME else key]
