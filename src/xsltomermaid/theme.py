"""Colours, palettes, button styles, status icons and small text helpers."""

from __future__ import annotations

from PySide6.QtCore import (
    QPointF,
    QRectF,
    Qt,
)
from PySide6.QtGui import (
    QColor,
    QIcon,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
)

from PySide6.QtWidgets import (
    QProxyStyle,
    QStyle,
)

from .diagram_view import resource_path


# Accent family and state colours, kept together so a tweak lives in one place
# rather than scattered across widget stylesheets.
_ACCENT = "#2f81f7"  # blue accent: checkboxes, selection, spinner, rings


# The filled primary button carries white text, so its fill is a darker blue
# that clears 4.5:1 with white (4.76) in every state; the lighter _ACCENT only
# managed 3.75.
_ACCENT_FILL = "#1f6fe0"  # primary button


# Diagram highlights, per page brightness. Blue marks the selection (solid
# outline), orange a picked reference and its join rows (dashed outline), so
# the two differ by line style as well as hue. Each clears 3:1 against every
# Mermaid cell fill on its page: light #fff/#f2f2f2/#ececff, dark
# #525252/#383838/#1f2020.
_HIGHLIGHT = {
    "light": {"select": "#1f6fe0", "reference": "#c25e00"},  # 4.09+ / 3.68+
    "dark": {"select": "#8ab8ff", "reference": "#ffb066"},  # 3.86+ / 4.34+
}


_ACCENT_HOVER = "#1a64d6"  # primary button, hover (5.47:1 with white)


_ACCENT_PRESSED = "#1559c2"  # primary button, pressed (6.48:1)


_ACCENT_RING = "#cfe0ff"  # light focus ring on a filled accent button


_ACCENT_WASH = "rgba(47,129,247,0.16)"  # translucent accent fill (drag-hover)


_DISABLED_BG = "rgba(128,128,128,0.18)"  # filled button, disabled


_DISABLED_FG = "rgba(128,128,128,0.75)"  # filled button text, disabled


def _apply_primary_button_style(button) -> None:
    """Filled-accent styling for the window's one lead action (Export).

    Works for a QPushButton or a QToolButton (the Export split button, whose
    menu arrow keeps the same fill).
    """
    kind = button.metaObject().className()
    button.setStyleSheet(
        (
        "QPushButton {"
        f"  background: {_ACCENT_FILL};"
        "  color: white;"
        "  font-weight: 600;"
        # A 2px transparent border reserves space so the focus ring doesn't
        # shift the button; padding is trimmed 2px to compensate.
        "  border: 2px solid transparent;"
        "  border-radius: 6px;"
        "  padding: 4px 12px;"
        "}"
        f"QPushButton:hover:enabled {{ background: {_ACCENT_HOVER}; }}"
        f"QPushButton:pressed:enabled {{ background: {_ACCENT_PRESSED}; }}"
        f"QPushButton:focus {{ border-color: {button.palette().color(QPalette.WindowText).name()}; }}"
        "QPushButton:disabled {"
        f"  background: {_DISABLED_BG};"
        f"  color: {_DISABLED_FG};"
        "}"
        + (
            # The split button's arrow segment: same fill, a hairline divider.
            "QToolButton::menu-button {"
            "  border: none; border-left: 1px solid rgba(255,255,255,0.35);"
            "  border-top-right-radius: 6px; border-bottom-right-radius: 6px;"
            "  width: 18px;"
            "}"
            "QToolButton { padding-right: 24px; }"
            if kind == "QToolButton"
            else ""
        )
        ).replace("QPushButton", kind)
    )


# Semantic colours come in a light-surface and a dark-surface shade: one value
# can't clear the contrast thresholds on both. Status icons need 3:1 against
# the window; link-like text needs 4.5:1. Measured on #f3f3f3 / #2b2d31.
_OK_GREEN = {"light": "#1a7f37", "dark": "#2e9e57"}  # 4.58 / 4.04


_ERR_ORANGE = {"light": "#b4540a", "dark": "#d9822b"}  # 4.49 / 4.72


_LINK = {"light": "#0a58ca", "dark": "#6aa7ff"}  # 5.80 / 5.64


def _surface(widget) -> str:
    """"dark" or "light", from the widget's own window colour."""
    return "dark" if widget.palette().color(QPalette.Window).lightness() < 128 else "light"


def _ok_hex(widget) -> str:
    return _OK_GREEN[_surface(widget)]


def _warn_hex(widget) -> str:
    return _ERR_ORANGE[_surface(widget)]


def _link_hex(widget) -> str:
    return _LINK[_surface(widget)]


def app_icon() -> QIcon:
    """The application icon, or an empty icon if the asset is missing."""
    for name in ("assets/app_icon.ico", "assets/app_icon.png"):
        path = resource_path(name)
        if path.exists():
            return QIcon(str(path))
    return QIcon()


def _blend(a: QColor, b: QColor, f: float) -> QColor:
    """Mix colour ``a`` toward ``b`` by fraction ``f`` (0..1)."""
    return QColor(
        round(a.red() * (1 - f) + b.red() * f),
        round(a.green() * (1 - f) + b.green() * f),
        round(a.blue() * (1 - f) + b.blue() * f),
    )


def _font_size(scale: float = 1.0) -> str:
    """A stylesheet font size relative to the app font, so text follows the
    system text size (fixed px sizes ignored it)."""
    from PySide6.QtWidgets import QApplication

    base = QApplication.font().pointSizeF()
    return f"font-size: {(base if base > 0 else 9.0) * scale:.1f}pt;"


def _muted_hex(widget) -> str:
    """A subdued but legible secondary-text colour for the current palette.

    Blends the normal text colour ~30% toward the window background: softer than
    body text, but still meets WCAG AA (~4.5:1). The palette's Disabled role is
    only ~3.7:1, so it must not be reused for active secondary text.
    """
    palette = widget.palette()
    return _blend(
        palette.color(QPalette.WindowText), palette.color(QPalette.Window), 0.30
    ).name()


def _line_hex(widget) -> str:
    """A subtle border/divider colour for the current palette."""
    return widget.palette().color(QPalette.Mid).name()


class AppStyle(QProxyStyle):
    """Fusion, with tick boxes you can see.

    Fusion outlines a check box in the window colour darkened by 40%: 1.8:1
    in light and 1.05:1 in dark, so an empty box all but vanished. This
    redraws the outline in the control-border colour (3:1 or better) for
    every check box and every tickable list row.
    """

    _BOXES = (QStyle.PE_IndicatorCheckBox, QStyle.PE_IndicatorItemViewItemCheck)

    def __init__(self, base: str = "Fusion"):
        super().__init__(base)

    def drawPrimitive(self, element, option, painter, widget=None):  # noqa: N802 (Qt naming)
        super().drawPrimitive(element, option, painter, widget)
        if element not in self._BOXES or not option.state & QStyle.State_Enabled:
            return
        dark = option.palette.color(QPalette.Window).lightness() < 128
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, False)
        painter.setPen(QPen(QColor(_BORDER_DARK if dark else _BORDER_LIGHT), 1))
        painter.setBrush(Qt.NoBrush)
        side = min(option.rect.width(), option.rect.height())
        box = QRectF(option.rect.x(), option.rect.y() + (option.rect.height() - side) / 2,
                     side, side)
        painter.drawRect(box.adjusted(0.5, 0.5, -0.5, -0.5))
        painter.restore()


_BORDER_DARK, _BORDER_LIGHT = "#7a7e86", "#8a8a8a"  # 3.39:1 / 3.11:1 on the window


def _control_border_hex(widget) -> str:
    """A border that marks a control's edge: at least 3:1 with the window."""
    dark = widget.palette().color(QPalette.Window).lightness() < 128
    return _BORDER_DARK if dark else _BORDER_LIGHT  # 3.39:1 / 3.11:1


def _text_hex(widget) -> str:
    """The normal (full-contrast) text colour for the current palette."""
    return widget.palette().color(QPalette.WindowText).name()


def _plural(n: int, word: str) -> str:
    """"3, 'table'" -> "3 tables"; "1, 'table'" -> "1 table" (regular +s)."""
    return f"{n:,} {word}" if n == 1 else f"{n:,} {word}s"


def _dark_palette() -> QPalette:
    """A Fusion-style dark palette used when the system is in dark mode."""
    p = QPalette()
    window = QColor(0x2B, 0x2D, 0x31)
    base = QColor(0x1E, 0x1F, 0x22)
    text = QColor(0xE6, 0xE6, 0xE6)
    disabled = QColor(0x80, 0x84, 0x8C)
    p.setColor(QPalette.Window, window)
    p.setColor(QPalette.WindowText, text)
    p.setColor(QPalette.Base, base)
    p.setColor(QPalette.AlternateBase, window)
    p.setColor(QPalette.ToolTipBase, window)
    p.setColor(QPalette.ToolTipText, text)
    p.setColor(QPalette.Text, text)
    p.setColor(QPalette.Button, window)
    p.setColor(QPalette.ButtonText, text)
    p.setColor(QPalette.BrightText, QColor(0xFF, 0x6B, 0x6B))
    p.setColor(QPalette.Link, QColor(_LINK["dark"]))  # 5.6:1
    p.setColor(QPalette.Highlight, QColor(_ACCENT_FILL))
    p.setColor(QPalette.HighlightedText, QColor(0xFF, 0xFF, 0xFF))
    p.setColor(QPalette.PlaceholderText, QColor(0x90, 0x94, 0x9C))  # 5.4:1 on Base
    p.setColor(QPalette.Mid, QColor(0x4A, 0x4D, 0x54))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, disabled)
    return p


def _light_palette(app) -> QPalette:
    """The style's light palette, with today's Windows window grey.

    The Windows style's standard palette still uses the classic #d4d0c8 grey,
    which reads dated next to white panels; #f3f3f3 matches Windows 11.
    """
    p = app.style().standardPalette()
    for role in (QPalette.Window, QPalette.Button, QPalette.AlternateBase):
        p.setColor(role, QColor(0xF3, 0xF3, 0xF3))
    p.setColor(QPalette.Link, QColor(_LINK["light"]))
    # Disabled inputs (the empty table list) otherwise fall back to the old
    # beige; keep them a quiet off-white.
    p.setColor(QPalette.Disabled, QPalette.Base, QColor(0xF8, 0xF8, 0xF8))
    p.setColor(QPalette.PlaceholderText, QColor(0x6B, 0x6B, 0x6B))  # 5.3:1 on white
    return p


def system_is_dark(app) -> bool:
    """Whether the OS is currently asking for a dark colour scheme (Qt 6.5+)."""
    hints = app.styleHints()
    scheme = getattr(hints, "colorScheme", None)
    if scheme is None:  # pragma: no cover - very old Qt
        return False
    return scheme() == Qt.ColorScheme.Dark


def apply_system_palette(app) -> bool:
    """Apply a light or dark palette to match the OS. Returns True if dark."""
    dark = system_is_dark(app)
    palette = _dark_palette() if dark else _light_palette(app)
    # One accent everywhere: without this, Windows 11 tints checkboxes with the
    # system accent (often lilac) while the app's buttons are blue.
    accent = QColor(_ACCENT)
    palette.setColor(QPalette.Highlight, QColor(_ACCENT_FILL))
    palette.setColor(QPalette.HighlightedText, QColor(0xFF, 0xFF, 0xFF))
    if hasattr(QPalette, "Accent"):  # Qt 6.6+
        palette.setColor(QPalette.Accent, accent)
    app.setPalette(palette)
    return dark


def announce(widget, message: str) -> None:
    """Tell a screen reader about ``message`` (best-effort, never fatal)."""
    try:
        from PySide6.QtGui import QAccessible, QAccessibleAnnouncementEvent

        if QAccessible.isActive():
            QAccessible.updateAccessibility(
                QAccessibleAnnouncementEvent(widget, message)
            )
    except Exception:  # noqa: BLE001 - a11y announcement must never be fatal
        pass


def status_icon(kind: str, color: str, size: int = 16, angle: int = 0) -> QPixmap:
    """A small drawn status icon: "spin" (an arc at ``angle``), "ok", "warn", "stale".

    Painted rather than typed so it looks the same on every OS and font.
    """
    ratio = 2  # draw at 2x so it stays crisp on high-DPI screens
    pix = QPixmap(size * ratio, size * ratio)
    pix.fill(Qt.transparent)
    pix.setDevicePixelRatio(ratio)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    c = QColor(color)
    r = QRectF(1.5, 1.5, size - 3, size - 3)
    if kind == "spin":
        track = QColor(c)
        track.setAlphaF(0.25)
        p.setPen(QPen(track, 2))
        p.drawEllipse(r)
        p.setPen(QPen(c, 2, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(r, -angle * 16, 100 * 16)
    elif kind == "ok":
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        p.drawEllipse(r)
        p.setPen(QPen(QColor("white"), 1.8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawPolyline([QPointF(4.6, 8.2), QPointF(7.0, 10.6), QPointF(11.4, 5.6)])
    elif kind == "warn":
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        p.drawPolygon([QPointF(8, 1.5), QPointF(15, 14.5), QPointF(1, 14.5)])
        p.setPen(QPen(QColor("white"), 1.8, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(QPointF(8, 6), QPointF(8, 9.6))
        p.drawPoint(QPointF(8, 12.2))
    else:  # "stale": a hollow ring, "something to do"
        p.setPen(QPen(c, 2))
        p.drawEllipse(QRectF(3.5, 3.5, size - 7, size - 7))
    p.end()
    return pix
