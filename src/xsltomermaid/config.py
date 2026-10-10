"""Limits, export formats and per-user settings shared across the GUI."""

from __future__ import annotations

import os

from PySide6.QtCore import (
    QSettings,
)


# Above this many tables we don't auto-render the whole diagram (it's slow and
# Mermaid chokes); the user picks a subset instead.
AUTO_RENDER_LIMIT = 25


# Rendering more than this many tables at once prompts a confirmation first.
RENDER_WARN_LIMIT = 60


# After the last tick, wait this long before re-rendering, so ticking several
# tables in a row draws once rather than once per click.
AUTO_RENDER_DELAY_MS = 400


# How many files File → Open Recent remembers.
RECENT_FILES_MAX = 8


# Exporting more than this many tables to an interchange format (drawio /
# excalidraw) warns that the file may be slow to open — but never blocks it.
EXPORT_WARN_TABLES = 500


# Mermaid refuses to render past its own ``maxTextSize`` (2,000,000 chars, set in
# diagram_view). Stay under it so we can show a helpful message instead of
# Mermaid's cryptic "Maximum text size in diagram exceeded".
MAX_RENDER_CHARS = 1_800_000


_ACCEPTED_SUFFIXES = (".xlsx", ".xlsm", ".xltx", ".xltm")


# (key, menu label, file-dialog title, default filename, filter)
EXPORT_FORMATS = [
    ("drawio", "Draw.io (.drawio)", "Save Draw.io diagram", "diagram.drawio",
     "Draw.io file (*.drawio)"),
    ("excalidraw", "Excalidraw (.excalidraw)", "Save Excalidraw scene",
     "diagram.excalidraw", "Excalidraw file (*.excalidraw)"),
    ("pdf", "PDF (.pdf)", "Save diagram PDF", "diagram.pdf", "PDF document (*.pdf)"),
    ("png", "PNG image (.png)", "Save diagram PNG", "diagram.png", "PNG image (*.png)"),
    ("svg", "SVG image (.svg)", "Save diagram SVG", "diagram.svg", "SVG image (*.svg)"),
]


# What exported diagrams are drawn on. Exports usually land on white pages
# (Confluence, Word), so they default to White even when the app shows a dark
# diagram on screen; "Match view" keeps the old behaviour.
EXPORT_BACKGROUNDS = [
    ("white", "White"),
    ("transparent", "Transparent"),
    ("match", "Match the view"),
]


PNG_SCALES = [1.0, 2.0, 3.0, 4.0]


def app_settings() -> QSettings:
    """Per-user settings (remembered export format, …).

    ``XSLTOMERMAID_SETTINGS`` points at an .ini file instead, so tests and
    portable setups don't touch the user's real settings.
    """
    override = os.environ.get("XSLTOMERMAID_SETTINGS")
    if override:
        return QSettings(override, QSettings.IniFormat)
    return QSettings(QSettings.IniFormat, QSettings.UserScope, "xsltomermaid", "xsltomermaid")


# Adding more than this many related tables in one click asks first (with a
# preview), so a hub table can't silently drag in dozens.
RELATED_WARN_COUNT = 10
