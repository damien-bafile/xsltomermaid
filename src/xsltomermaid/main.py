"""Qt (PySide6) desktop app: drag an Excel schema file in, get a Mermaid ER diagram.

Run with:  uv run xsltomermaid
"""

from __future__ import annotations

import dataclasses
import html
import os
import shutil
import sys
import tempfile
import webbrowser
from pathlib import Path


def _normalize_cli_args(argv: list[str] | None) -> list[str]:
    """Treat optional argv consistently whether it includes program name or not."""
    if argv is None:
        return list(sys.argv[1:])
    args = list(argv)
    script_names = {
        sys.argv[0],
        os.path.basename(sys.argv[0]),
        __file__,
        os.path.abspath(__file__),
        os.path.basename(__file__),
    }
    if args and args[0] in script_names:
        return args[1:]
    return args


def _configure_headless_env(argv: list[str] | None = None) -> None:
    """Set Qt/WebEngine env vars for screenshot modes before Qt imports."""
    args = _normalize_cli_args(argv)
    headless = "--screenshot" in args or "--screenshot-diagram" in args
    if not headless:
        return

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    if "--screenshot-diagram" in args:
        # Chromium (WebEngine) needs these to run headless / as root in CI.
        os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
        os.environ.setdefault(
            "QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox --disable-gpu --in-process-gpu"
        )


_configure_headless_env()

from PySide6.QtCore import (
    QAbstractTableModel,
    QEvent,
    QModelIndex,
    QPointF,
    QRectF,
    QSettings,
    QSortFilterProxyModel,
    QUrl,
    Qt,
    QThread,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QActionGroup,
    QDesktopServices,
    QPainter,
    QPen,
    QPixmap,
    QTextDocumentFragment,
    QAction,
    QColor,
    QFont,
    QGuiApplication,
    QIcon,
    QKeySequence,
    QPalette,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QMenu,
    QApplication,
    QCheckBox,
    QComboBox,
    QDockWidget,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QToolButton,
    QHeaderView,
    QTableView,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .diagram_view import (
    VENDOR_MERMAID,
    DiagramView,
    RenderStyle,
    resource_path,
    schema_to_drawio,
    schema_to_excalidraw,
)
from .excel_to_mermaid import (
    is_audit_relationship,
    mermaid_entity_ids,
    EXPECTED_HEADERS,
    DiagramOptions,
    Schema,
    Table,
    filter_columns,
    filter_schema,
    generate_mermaid,
    related_tables,
    route_paths,
    unresolved_foreign_keys,
    MERMAID_CDN,
    wrap_mermaid_html,
)
from .make_sample import write_sample
from .selection_preset import dump_selection_toml, load_selection_toml
from .map_view import SchemaMapView
from .schema_map import link_counts, name_prefix
from .services import SchemaImportService
from .sql_query import generate_select
from . import __version__
from .updates import (
    RELEASES_PAGE,
    UpdateCheckError,
    fetch_latest_release,
    is_newer,
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
# Building a checkable tree of every column gets heavy; above this many columns
# the "All tables" view isn't built at once — the user picks a single table from
# the dropdown instead (which is always fast, whatever the schema size).
COLUMN_TREE_LIMIT = 10000

_ACCEPTED_SUFFIXES = (".xlsx", ".xlsm", ".xltx", ".xltm")

SQL_SERVER_SCHEMA_QUERY = """\
-- Run this in the SQL Server database you want to document.
-- Export the results, including column headers, to .xlsx for xsltomermaid.
SELECT
    s.name AS [SchemaName],
    t.name AS [TableName],
    c.column_id AS [ColumnOrder],
    c.name AS [ColumnName],
    ty.name AS [DataType],
    CASE
        WHEN ty.name IN (N'varchar', N'nvarchar', N'varbinary')
            AND c.max_length = -1 THEN N'max'
        WHEN ty.name IN (N'nchar', N'nvarchar') THEN CONVERT(varchar(10), c.max_length / 2)
        WHEN ty.name IN (N'char', N'varchar', N'binary', N'varbinary')
            THEN CONVERT(varchar(10), c.max_length)
    END AS [Length],
    NULLIF(c.precision, 0) AS [Precision],
    NULLIF(c.scale, 0) AS [Scale],
    c.is_nullable AS [IsNullable],
    c.is_identity AS [IsIdentity],
    c.is_computed AS [IsComputed],
    CASE WHEN EXISTS (
        SELECT 1
        FROM sys.indexes AS pk
        INNER JOIN sys.index_columns AS pkc
            ON pkc.object_id = pk.object_id
            AND pkc.index_id = pk.index_id
        WHERE pk.object_id = t.object_id
            AND pk.is_primary_key = 1
            AND pkc.column_id = c.column_id
    ) THEN 1 ELSE 0 END AS [IsPrimaryKey],
    fk.ForeignKeyReference AS [ForeignKeyReference],
    dc.definition AS [DefaultValue],
    cc.definition AS [ComputedDefinition],
    c.collation_name AS [Collation],
    CONVERT(nvarchar(max), ep.value) AS [Description]
FROM sys.tables AS t
INNER JOIN sys.schemas AS s ON s.schema_id = t.schema_id
INNER JOIN sys.columns AS c ON c.object_id = t.object_id
INNER JOIN sys.types AS ty ON ty.user_type_id = c.user_type_id
LEFT JOIN sys.default_constraints AS dc
    ON dc.object_id = c.default_object_id
LEFT JOIN sys.computed_columns AS cc
    ON cc.object_id = c.object_id AND cc.column_id = c.column_id
LEFT JOIN sys.extended_properties AS ep
    ON ep.class = 1
    AND ep.major_id = t.object_id
    AND ep.minor_id = c.column_id
    AND ep.name = N'MS_Description'
OUTER APPLY (
    SELECT TOP (1)
        CONCAT(rs.name, N'.', rt.name, N'.', rc.name) AS ForeignKeyReference
    FROM sys.foreign_key_columns AS fkc
    INNER JOIN sys.tables AS rt ON rt.object_id = fkc.referenced_object_id
    INNER JOIN sys.schemas AS rs ON rs.schema_id = rt.schema_id
    INNER JOIN sys.columns AS rc
        ON rc.object_id = fkc.referenced_object_id
        AND rc.column_id = fkc.referenced_column_id
    WHERE fkc.parent_object_id = t.object_id
        AND fkc.parent_column_id = c.column_id
    ORDER BY fkc.constraint_object_id, fkc.constraint_column_id
) AS fk
WHERE t.is_ms_shipped = 0
ORDER BY s.name, t.name, c.column_id;
"""

# Accent family and state colours, kept together so a tweak lives in one place
# rather than scattered across widget stylesheets.
_ACCENT = "#2f81f7"  # blue accent: checkboxes, selection, spinner, rings
# The filled primary button carries white text, so its fill is a darker blue
# that clears 4.5:1 with white (4.76) in every state; the lighter _ACCENT only
# managed 3.75.
_ACCENT_FILL = "#1f6fe0"  # primary button
_ACCENT_HOVER = "#1a64d6"  # primary button, hover (5.47:1 with white)
_ACCENT_PRESSED = "#1559c2"  # primary button, pressed (6.48:1)
_ACCENT_RING = "#cfe0ff"  # light focus ring on a filled accent button
_ACCENT_WASH = "rgba(47,129,247,0.08)"  # translucent accent fill (drag-hover)
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
        f"QPushButton:focus {{ border-color: {_ACCENT_RING}; }}"
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
    p.setColor(QPalette.Link, QColor(_ACCENT))
    p.setColor(QPalette.Highlight, QColor(_ACCENT))
    p.setColor(QPalette.HighlightedText, QColor(0xFF, 0xFF, 0xFF))
    p.setColor(QPalette.PlaceholderText, disabled)
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
    palette.setColor(QPalette.Highlight, accent)
    palette.setColor(QPalette.HighlightedText, QColor(0xFF, 0xFF, 0xFF))
    if hasattr(QPalette, "Accent"):  # Qt 6.6+
        palette.setColor(QPalette.Accent, accent)
    app.setPalette(palette)
    return dark


class DropArea(QLabel):
    """A large label that accepts a dragged spreadsheet file."""

    _IDLE_TEXT = (
        "\n\nDrag an Excel schema file here\n\n"
        "(.xlsx / .xlsm)  —  or click to browse\n\n"
    )

    def __init__(self, on_file, parent=None):
        super().__init__(parent)
        self._on_file = on_file
        self._compact = False
        self.setAcceptDrops(True)
        self.setAlignment(Qt.AlignCenter)
        self.setWordWrap(True)
        self.setText(self._IDLE_TEXT)
        self.setObjectName("dropArea")
        self.setMinimumHeight(120)
        # Keyboard-operable: a keyboard-only user can focus it and press
        # Enter/Space to browse, so loading a file never requires the mouse.
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAccessibleName("Excel schema drop area")
        self.setAccessibleDescription(
            "Drop an .xlsx or .xlsm schema file here, or press Enter to browse."
        )
        self._reset_style()

    def keyPressEvent(self, event):  # noqa: N802 (Qt naming)
        if event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self._browse()
            event.accept()
            return
        super().keyPressEvent(event)

    def set_loaded(self, name: str):
        """Shrink to a slim file chip once a schema is loaded.

        The big idle target is worth its height only until a file is in; after
        that it becomes a compact bar so the tabs get the room. The whole area
        stays a drop target and click-to-browse, so replacing the file is a
        drop or a click away.
        """
        self._compact = True
        self.setText(f"{name}     ·     drop or click to load another file")
        self.setMinimumHeight(0)
        self.setMaximumHeight(46)
        self._reset_style()

    def mousePressEvent(self, event):  # noqa: N802 (Qt naming)
        self._browse()

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select an Excel schema file",
            "",
            "Excel files (*.xlsx *.xlsm *.xltx *.xltm);;All files (*)",
        )
        if path:
            self._on_file(path)

    def dragEnterEvent(self, event):  # noqa: N802
        if self._has_valid_url(event):
            event.acceptProposedAction()
            self.setStyleSheet(
                "#dropArea {"
                f"  border: 2px solid {_ACCENT};"
                "  border-radius: 12px;"
                f"  color: {_link_hex(self)};"
                "  font-size: 15px;"
                f"  background: {_ACCENT_WASH};"
                "}"
            )
        else:
            event.ignore()

    def dragLeaveEvent(self, event):  # noqa: N802
        self._reset_style()

    def dropEvent(self, event):  # noqa: N802
        self._reset_style()
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path.lower().endswith(_ACCEPTED_SUFFIXES):
                self._on_file(path)
                return
        QMessageBox.warning(
            self, "Unsupported file", "Please drop an .xlsx or .xlsm file."
        )

    @staticmethod
    def _has_valid_url(event) -> bool:
        if not event.mimeData().hasUrls():
            return False
        return any(
            url.toLocalFile().lower().endswith(_ACCEPTED_SUFFIXES)
            for url in event.mimeData().urls()
        )

    def _reset_style(self):
        # A keyboard-focus ring in the accent colour, on either variant.
        link = _link_hex(self)
        focus = f"#dropArea:focus {{ border-color: {link}; color: {link}; }}"
        if self._compact:
            self.setStyleSheet(
                "#dropArea {"
                f"  border: 1px solid {_line_hex(self)};"
                "  border-radius: 8px;"
                f"  color: {_muted_hex(self)};"
                "  font-size: 13px;"
                "  padding: 4px 12px;"
                "}"
                + focus
            )
        else:
            self.setStyleSheet(
                "#dropArea {"
                f"  border: 2px dashed {_line_hex(self)};"
                "  border-radius: 12px;"
                f"  color: {_muted_hex(self)};"
                "  font-size: 15px;"
                "}"
                + focus
            )

    def retheme(self):
        """Re-apply the idle style for the current palette (light/dark)."""
        self._reset_style()


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


class StatusLabel(QLabel):
    """The window's status line; every change is also read out to screen readers.

    Messages such as "copied" or "saved" are otherwise visual only.
    """

    def setText(self, text: str):  # noqa: N802 - Qt override
        super().setText(text)
        if text:
            plain = QTextDocumentFragment.fromHtml(text).toPlainText() if "<" in text else text
            announce(self, plain)


class LoadWorker(QThread):
    """Read + parse a spreadsheet off the UI thread, reporting progress.

    The heavy work (streaming the workbook and building the schema) runs here so
    the window stays responsive and can show a live progress bar. Only the final
    ``loaded`` payload is handed back to the UI thread, which then touches the
    widgets.
    """

    progressed = Signal(int)  # overall percentage, 0..100
    staged = Signal(str)  # human-readable phase label
    loaded = Signal(object, object, str)  # rows, schema, mermaid_text
    failed = Signal(str)  # error message

    def __init__(self, path: str, parent=None, importer=None):
        super().__init__(parent)
        self._path = path
        self._importer = importer or SchemaImportService()

    def run(self):  # noqa: D401 - QThread entry point
        try:
            previous_stage = None

            def report_progress(stage: str, percent: int) -> None:
                nonlocal previous_stage
                if stage != previous_stage:
                    self.staged.emit(stage)
                    previous_stage = stage
                self.progressed.emit(percent)

            rows, schema, mermaid_text = self._importer.load(
                self._path, report_progress
            )
            self.loaded.emit(rows, schema, mermaid_text)
        except Exception as exc:  # noqa: BLE001 - surface any parse error to the UI
            self.failed.emit(str(exc))


class UpdateCheckWorker(QThread):
    """Ask GitHub for the latest release off the UI thread (network I/O)."""

    found = Signal(object)  # updates.Release
    failed = Signal(str)  # user-presentable error message

    def run(self):  # noqa: D401 - QThread entry point
        try:
            self.found.emit(fetch_latest_release())
        except UpdateCheckError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - never crash the app on a check
            self.failed.emit(f"Unexpected error: {exc}")


# One shared vocabulary for foreign-key direction, used by BOTH the path tracer
# and "Add related tables" so the same idea is never worded two ways. Plain
# language leads; the DBA terms (child→parent) live in the tooltips. "forward"
# follows the FK reference (a child points to its parent); "reverse" the reverse.
_FK_DIRECTION_CHOICES = (
    ("Either direction", "either"),
    ("Tables it points to", "forward"),
    ("Tables that point to it", "reverse"),
)
_FK_DIRECTION_LABELS = {value: label for label, value in _FK_DIRECTION_CHOICES}
_FK_DIRECTION_TOOLTIP = (
    "Which way to follow foreign keys:\n"
    "Either direction — ignore direction, just connectivity.\n"
    "Tables it points to — follow each FK to its target (child → parent).\n"
    "Tables that point to it — follow FKs back to their source (parent → child)."
)

# Adding more than this many related tables in one click asks first (with a
# preview), so a hub table can't silently drag in dozens.
RELATED_WARN_COUNT = 10


_ROLE_LINKS = Qt.UserRole + 1  # (out, in) link counts
_ROLE_DRAWN = Qt.UserRole + 2  # True when the table is in the current diagram


class _TableRowDelegate(QStyledItemDelegate):
    """Paints a table row with its link counts right-aligned (↗ out ↙ in) and
    a dot when the table is in the diagram. The name is elided to fit."""

    def paint(self, painter, option, index):
        links = index.data(_ROLE_LINKS)
        drawn = bool(index.data(_ROLE_DRAWN))
        if not links and not drawn:
            super().paint(painter, option, index)
            return
        out, in_ = links or (0, 0)
        text = f"↗{out} ↙{in_}" if links else ""
        metrics = option.fontMetrics
        dot = 12 if drawn else 0
        width = metrics.horizontalAdvance(text) + dot + 10
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        style = opt.widget.style() if opt.widget else QApplication.style()
        # Full-width selection/hover background, then the item in a narrower
        # rect so its text elides before the counts.
        style.drawPrimitive(QStyle.PE_PanelItemViewItem, opt, painter, opt.widget)
        opt.rect = option.rect.adjusted(0, 0, -width, 0)
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, opt.widget)
        painter.save()
        right = option.rect.adjusted(0, 0, -6, 0)
        selected = bool(option.state & QStyle.State_Selected)
        muted = option.palette.color(
            QPalette.HighlightedText if selected else QPalette.PlaceholderText
        )
        painter.setPen(muted)
        if text:
            painter.drawText(right.adjusted(0, 0, -dot, 0), Qt.AlignRight | Qt.AlignVCenter, text)
        if drawn:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(_ACCENT))
            r = 3.5
            cy = right.center().y()
            painter.drawEllipse(QPointF(right.right() - r, cy), r, r)
        painter.restore()


class TableSelector(QWidget):
    """A filterable, checkable list of tables to include in the diagram."""

    applied = Signal()  # user asked to (re)render the current selection
    selection_changed = Signal()  # the user ticked/unticked tables (auto-render)
    current_table_changed = Signal(str)  # the highlighted row ("" for none)
    related_requested = Signal()  # user asked to also tick the related tables
    path_requested = Signal()  # user asked for the shortest path between two tables

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        title = QLabel("Tables in diagram")
        title.setStyleSheet("font-weight: 600;")
        self._header_row = QHBoxLayout()
        self._header_row.addWidget(title)
        self._header_row.addStretch(1)
        layout.addLayout(self._header_row)

        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Filter tables…")
        self._filter.setAccessibleName("Filter tables")
        self._filter.setClearButtonEnabled(True)
        self._filter.textChanged.connect(self._apply_filter_text)
        layout.addWidget(self._filter)

        # How to read a long list: order, and which publisher prefix.
        view_row = QHBoxLayout()
        self._sort = QComboBox()
        self._sort.addItem("Name A–Z", "name")
        self._sort.addItem("Most connected", "links")
        self._sort.setAccessibleName("Sort tables")
        self._sort.setToolTip("Order the list by name, or by links (audit and system links not counted)")
        self._sort.currentIndexChanged.connect(lambda _i: self._resort())
        self._prefix = QComboBox()
        self._prefix.setAccessibleName("Table name prefix")
        self._prefix.setToolTip("Show only tables with this publisher prefix (msdyn_, hsl_, …)")
        self._prefix.currentIndexChanged.connect(lambda _i: self._apply_filters())
        view_row.addWidget(self._sort, 1)
        view_row.addWidget(self._prefix, 1)
        layout.addLayout(view_row)

        self._list = QListWidget()
        self._list.setUniformItemSizes(True)
        self._list.setItemDelegate(_TableRowDelegate(self._list))
        self._list.setAccessibleName("Tables in diagram")
        self._list.itemChanged.connect(self._on_item_changed)
        self._list.currentItemChanged.connect(
            lambda item, _prev: self.current_table_changed.emit(item.text() if item else "")
        )
        layout.addWidget(self._list, 1)

        # The count gets its own line so the two checkboxes below don't
        # force the rail wider than the table names need.
        self._count = QLabel("No tables loaded yet")
        self._count.setStyleSheet(f"color: {_muted_hex(self)};")
        layout.addWidget(self._count)
        count_row = QHBoxLayout()
        # For big schemas: review what's ticked without scrolling 1,000+ rows.
        self._ticked_only = QCheckBox("Ticked onl&y")
        self._ticked_only.setToolTip("List only the tables that are ticked.")
        self._ticked_only.toggled.connect(lambda _on: self._apply_filters())
        count_row.addWidget(self._ticked_only)
        self._hide_unconnected = QCheckBox("Hide unconnected")
        self._hide_unconnected.setToolTip(
            "Hide tables with no links (audit and system links not counted)."
        )
        self._hide_unconnected.toggled.connect(lambda _on: self._apply_filters())
        count_row.addWidget(self._hide_unconnected)
        count_row.addStretch(1)
        layout.addLayout(count_row)

        button_row = QHBoxLayout()
        self._select_shown_btn = QPushButton("&Select shown")
        self._clear_btn = QPushButton("&Clear")
        self._select_shown_btn.setToolTip("Tick every table currently visible in the list.")
        self._clear_btn.setToolTip("Untick every table. Can be undone.")
        self._select_shown_btn.clicked.connect(self._on_select_shown)
        self._clear_btn.clicked.connect(self._on_clear)
        button_row.addWidget(self._select_shown_btn)
        button_row.addWidget(self._clear_btn)
        layout.addLayout(button_row)

        related_row = QHBoxLayout()
        self._related_btn = QPushButton("&Add related tables")
        self._related_btn.setToolTip(
            "Tick the tables one foreign-key hop from the ones you've checked. "
            "Adding a lot at once asks first, and can be undone."
        )
        self._related_btn.clicked.connect(lambda: self.related_requested.emit())
        self._related_direction = QComboBox()
        self._related_direction.setAccessibleName("Related tables direction")
        for label, value in _FK_DIRECTION_CHOICES:
            self._related_direction.addItem(label, value)
        self._related_direction.setToolTip(
            "Which neighbours to add, by foreign-key direction.\n" + _FK_DIRECTION_TOOLTIP
        )
        related_row.addWidget(self._related_btn, 1)
        related_row.addWidget(self._related_direction)
        layout.addLayout(related_row)

        # Path tracing is a power tool, not the main loop, so it lives behind a
        # disclosure: the header is one quiet line until opened, keeping the rail
        # focused on "pick tables → render".
        self._path_toggle = QToolButton()
        self._path_toggle.setText("Trace path between tables")
        self._path_toggle.setCheckable(True)
        self._path_toggle.setChecked(False)
        self._path_toggle.setArrowType(Qt.RightArrow)
        self._path_toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self._path_toggle.setToolTip("Find the shortest FK path between two tables.")
        # A borderless header, but keyboard focus and hover must still show — a
        # background wash gives both without shifting the layout.
        self._path_toggle.setStyleSheet(
            "QToolButton { border: none; font-weight: 600; padding: 2px 4px;"
            "  border-radius: 4px; }"
            f"QToolButton:hover {{ background: {_ACCENT_WASH}; }}"
            f"QToolButton:focus {{ background: {_ACCENT_WASH}; }}"
        )
        self._path_toggle.toggled.connect(self._on_path_toggled)
        layout.addWidget(self._path_toggle)

        # Everything the path tracer needs, hidden until the disclosure is open.
        self._path_box = QWidget()
        path_box = QVBoxLayout(self._path_box)
        path_box.setContentsMargins(0, 0, 0, 4)
        path_box.setSpacing(6)

        endpoints_row = QHBoxLayout()
        self._path_from = QComboBox()
        self._path_from.setToolTip("Starting table for the path.")
        self._path_from.setAccessibleName("Path from table")
        self._path_to = QComboBox()
        self._path_to.setToolTip("Destination table for the path.")
        self._path_to.setAccessibleName("Path to table")
        for combo in (self._path_from, self._path_to):
            combo.currentIndexChanged.connect(self._update_path_enabled)
        endpoints_row.addWidget(self._path_from, 1)
        arrow = QLabel("→")
        arrow.setAlignment(Qt.AlignCenter)
        endpoints_row.addWidget(arrow)
        endpoints_row.addWidget(self._path_to, 1)
        path_box.addLayout(endpoints_row)

        # Optional single intermediate stop the route must pass through.
        via_row = QHBoxLayout()
        via_label = QLabel("via")
        self._path_via = QComboBox()
        via_label.setBuddy(self._path_via)
        self._path_via.setAccessibleName("Path via table")
        self._path_via.setToolTip(
            "Optional: force the route through this table on the way."
        )
        via_row.addWidget(via_label)
        via_row.addWidget(self._path_via, 1)
        path_box.addLayout(via_row)

        # Foreign keys are directed, so let the user say which way to walk them.
        self._path_direction = QComboBox()
        self._path_direction.setAccessibleName("Path direction")
        for label, value in _FK_DIRECTION_CHOICES:
            self._path_direction.addItem(label, value)
        self._path_direction.setToolTip(_FK_DIRECTION_TOOLTIP)
        path_box.addWidget(self._path_direction)

        # Teach the one domain concept this panel assumes, at point of use, so a
        # non-DBA doesn't have to hover a tooltip to know what "direction" means.
        self._path_hint = QLabel(
            "A foreign key points from a child table to the parent it references."
        )
        self._path_hint.setWordWrap(True)
        self._path_hint.setStyleSheet(f"color: {_muted_hex(self)}; font-size: 11px;")
        path_box.addWidget(self._path_hint)

        path_row = QHBoxLayout()
        self._path_btn = QPushButton("Trace path")
        self._path_btn.setToolTip(
            "Trace the shortest foreign-key path between the two chosen tables "
            "(through the optional Via stop) and render it."
        )
        self._path_btn.clicked.connect(lambda: self.path_requested.emit())
        self._path_replace = QCheckBox("Replace selection")
        self._path_replace.setToolTip(
            "On: the path replaces your current selection.\n"
            "Off: the path is added to what you've already checked."
        )
        path_row.addWidget(self._path_btn, 1)
        path_row.addWidget(self._path_replace)
        path_box.addLayout(path_row)

        self._path_box.setVisible(False)
        layout.addWidget(self._path_box)

        # The diagram follows the ticks on its own (see MainWindow's auto-render),
        # so Render is a quiet manual refresh, needed for big selections that
        # don't auto-render. Undo only appears when there's something to undo.
        layout.addSpacing(4)
        action_row = QHBoxLayout()
        self._render_btn = QPushButton("&Render selected")
        self._render_btn.setToolTip(
            "Redraw the diagram now (F5). Small selections update automatically; "
            f"more than {RENDER_WARN_LIMIT} tables wait for this."
        )
        self._render_btn.clicked.connect(lambda: self.applied.emit())
        self._undo_btn = QPushButton("&Undo")
        self._undo_btn.setToolTip(
            "Restore the selection from before the last clear, add or traced path."
        )
        self._undo_btn.clicked.connect(self.undo_last_change)
        self._undo_btn.setVisible(False)
        action_row.addWidget(self._render_btn, 1)
        action_row.addWidget(self._undo_btn)
        layout.addLayout(action_row)
        self._undo_snapshot: list[str] | None = None

        # Keyboard accelerators for the frequent loop: jump to the filter, and
        # render without reaching for the mouse (Ctrl+Enter alongside the menu's
        # F5). Button mnemonics (Alt+R/A/C/…) cover the rest.
        QShortcut(QKeySequence("Ctrl+F"), self, activated=self._focus_filter)
        for seq in ("Ctrl+Return", "Ctrl+Enter"):
            QShortcut(QKeySequence(seq), self, activated=self._emit_render_if_ready)

        # Nothing to act on until a schema is loaded.
        self.set_ready(False)

    def add_header_widget(self, widget):
        """Put a small control (the table-list menu) beside the panel title."""
        self._header_row.addWidget(widget)

    def _focus_filter(self):
        if self._filter.isEnabled():
            self._filter.setFocus(Qt.ShortcutFocusReason)
            self._filter.selectAll()

    def _emit_render_if_ready(self):
        if self._render_btn.isEnabled():
            self.applied.emit()

    def set_ready(self, ready: bool):
        """Enable the selection controls only once a schema is loaded.

        With no file open there are no tables to pick, so the filter, the list
        and every action button are disabled to match the (already disabled)
        export bar rather than inviting dead clicks at "0 of 0 selected".
        """
        for widget in (
            self._filter,
            self._list,
            self._ticked_only,
            self._hide_unconnected,
            self._sort,
            self._prefix,
            self._select_shown_btn,
            self._clear_btn,
            self._related_btn,
            self._related_direction,
            self._path_toggle,
            self._path_from,
            self._path_to,
            self._path_via,
            self._path_direction,
            self._path_replace,
            self._render_btn,
        ):
            widget.setEnabled(ready)
        # The trace/undo buttons have their own readiness (two valid endpoints;
        # an available snapshot) layered on top of the schema being loaded.
        self._update_path_enabled()
        if not ready:
            self._undo_snapshot = None
            self._undo_btn.setText("&Undo")
        self._undo_btn.setVisible(ready and self._undo_snapshot is not None)

    # -- population --------------------------------------------------------
    def set_tables(self, names: list[str]):
        self._list.blockSignals(True)
        self._list.clear()
        for name in names:
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            self._list.addItem(item)
        self._list.blockSignals(False)
        self._ticked_only.setChecked(False)
        self._hide_unconnected.setChecked(False)
        self._sort.blockSignals(True)
        self._sort.setCurrentIndex(0)
        self._sort.blockSignals(False)
        self._fill_prefixes(names)
        self._filter.clear()
        # Refill the From/To pickers and default them to the first two tables so
        # a path can be traced without any prior clicking.
        for combo in (self._path_from, self._path_to):
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(names)
            combo.blockSignals(False)
        if self._path_to.count() > 1:
            self._path_to.setCurrentIndex(1)
        # The Via picker starts at an explicit "no stop" entry (data None).
        self._path_via.blockSignals(True)
        self._path_via.clear()
        self._path_via.addItem("(no via stop)", None)
        for name in names:
            self._path_via.addItem(name, name)
        self._path_via.blockSignals(False)
        self._undo_snapshot = None
        self._undo_btn.setText("&Undo")
        self._undo_btn.setVisible(False)
        self._update_path_enabled()
        self._update_count()

    # -- selection helpers -------------------------------------------------
    def _items(self):
        return (self._list.item(i) for i in range(self._list.count()))

    def _apply_filter_text(self, _text: str = ""):
        self._apply_filters()

    def _apply_filters(self):
        """Text, Ticked only, prefix and Hide unconnected, all together."""
        needle = self._filter.text().strip().lower()
        ticked_only = self._ticked_only.isChecked()
        prefix = self._prefix.currentData()
        hide_lonely = self._hide_unconnected.isChecked()
        for item in self._items():
            links = item.data(_ROLE_LINKS)
            item.setHidden(
                needle not in item.text().lower()
                or (ticked_only and item.checkState() != Qt.Checked)
                or (prefix is not None and name_prefix(item.text()) != prefix)
                or (hide_lonely and links is not None and sum(links) == 0)
            )

    def _fill_prefixes(self, names):
        counts: dict[str, int] = {}
        for name in names:
            counts[name_prefix(name)] = counts.get(name_prefix(name), 0) + 1
        self._prefix.blockSignals(True)
        self._prefix.clear()
        self._prefix.addItem(f"All prefixes ({len(names):,})", None)
        for prefix, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
            label = f"{prefix}_ ({n:,})" if prefix else f"No prefix ({n:,})"
            self._prefix.addItem(label, prefix)
        self._prefix.blockSignals(False)
        # One prefix (or none at all) gives nothing to choose between.
        self._prefix.setVisible(self._prefix.count() > 2)

    def set_link_counts(self, counts: dict[str, tuple[int, int]]):
        """Show each table's (out, in) link counts and enable those views."""
        # Data changes fire itemChanged, which is for ticks; with 1,800 rows
        # that cascaded into thousands of re-counts and a hang.
        self._list.blockSignals(True)
        for item in self._items():
            out, in_ = counts.get(item.text(), (0, 0))
            item.setData(_ROLE_LINKS, (out, in_))
            item.setToolTip(
                f"{item.text()}: {out} reference{'s' if out != 1 else ''} out, {in_} in "
                "(audit and system links not counted)"
            )
            item.setData(Qt.AccessibleDescriptionRole, f"{out} out, {in_} in")
        self._list.blockSignals(False)
        self._resort()
        self._apply_filters()

    def set_drawn(self, names):
        """Mark the tables currently drawn in the diagram."""
        drawn = {n.lower() for n in names}
        self._list.blockSignals(True)
        for item in self._items():
            item.setData(_ROLE_DRAWN, item.text().lower() in drawn)
        self._list.blockSignals(False)
        self._list.viewport().update()

    def _resort(self):
        """Reorder rows in place (ticks, data and the current row survive)."""
        mode = self._sort.currentData()
        current = self._list.currentItem().text() if self._list.currentItem() else ""
        items = [self._list.takeItem(0) for _ in range(self._list.count())]
        if mode == "links":
            items.sort(key=lambda it: (-sum(it.data(_ROLE_LINKS) or (0, 0)), it.text().lower()))
        else:
            items.sort(key=lambda it: it.text().lower())
        self._list.blockSignals(True)
        for item in items:
            self._list.addItem(item)
        self._list.blockSignals(False)
        if current:
            self.focus_table(current)

    def focus_table(self, name: str):
        """Highlight and scroll to a table's row (no tick change); "" clears."""
        match = next((i for i in self._items() if i.text() == name), None) if name else None
        self._list.blockSignals(True)
        self._list.setCurrentItem(match)
        if match is None:
            self._list.clearSelection()
        self._list.blockSignals(False)
        if match is not None:
            self._list.scrollToItem(match)

    def _on_item_changed(self, _item):
        self._update_count()
        self.selection_changed.emit()

    def _on_select_shown(self):
        self.check_shown()
        self.selection_changed.emit()

    def _on_clear(self):
        if not self.selected_tables():
            return
        self.snapshot_for_undo("clear")
        self.clear_selection()
        self.selection_changed.emit()

    def check_shown(self):
        self._set_state((item for item in self._items() if not item.isHidden()), Qt.Checked)

    def check_all(self):
        self._set_state(self._items(), Qt.Checked)

    def clear_selection(self):
        self._set_state(self._items(), Qt.Unchecked)

    def _set_state(self, items, state):
        self._list.blockSignals(True)
        for item in items:
            item.setCheckState(state)
        self._list.blockSignals(False)
        self._update_count()
        if self._ticked_only.isChecked():
            self._apply_filter_text(self._filter.text())

    def selected_tables(self) -> list[str]:
        return [item.text() for item in self._items() if item.checkState() == Qt.Checked]

    def check_tables(self, names) -> int:
        """Tick the listed tables (by name, case-insensitive), additively.

        Returns how many were newly ticked.
        """
        wanted = {n.lower() for n in names}
        newly = 0
        self._list.blockSignals(True)
        for item in self._items():
            if item.text().lower() in wanted and item.checkState() != Qt.Checked:
                item.setCheckState(Qt.Checked)
                newly += 1
        self._list.blockSignals(False)
        self._update_count()
        if self._ticked_only.isChecked():
            self._apply_filter_text(self._filter.text())
        return newly

    def set_selected_tables(self, names) -> tuple[int, list[str]]:
        wanted = [str(name).strip() for name in names if str(name).strip()]
        present = {item.text().lower() for item in self._items()}
        missing = [name for name in wanted if name.lower() not in present]
        self.clear_selection()
        applied = self.check_tables(wanted)
        return applied, missing

    def _update_count(self):
        total = self._list.count()
        if total == 0:
            # Empty state: orient a newcomer instead of a bare "0 of 0 selected".
            self._count.setText("No tables loaded yet")
            return
        selected = sum(1 for item in self._items() if item.checkState() == Qt.Checked)
        self._count.setText(f"{selected} of {total} selected")

    # -- path tracing (endpoints + non-destructive apply) ------------------
    def path_endpoints(self) -> tuple[str, str]:
        """The two tables chosen in the From/To pickers."""
        return self._path_from.currentText(), self._path_to.currentText()

    def path_replaces_selection(self) -> bool:
        """Whether a traced path should replace (vs add to) the selection."""
        return self._path_replace.isChecked()

    def path_direction(self) -> str:
        """Which way to walk foreign keys: 'either', 'forward', or 'reverse'."""
        return self._path_direction.currentData()

    def path_via(self) -> str | None:
        """The optional intermediate stop, or None when '(no via stop)' is picked."""
        return self._path_via.currentData()

    def _on_path_toggled(self, open_: bool):
        """Expand or collapse the path-tracer disclosure."""
        self._path_toggle.setArrowType(Qt.DownArrow if open_ else Qt.RightArrow)
        self._path_box.setVisible(open_)

    def _update_path_enabled(self):
        """Enable Trace only when two distinct, real endpoints are chosen."""
        start, end = self.path_endpoints()
        # Both pickers share the schema-loaded enabled state, so testing one is enough.
        ready = self._path_from.isEnabled() and bool(start) and bool(end) and start != end
        self._path_btn.setEnabled(ready)

    def related_direction(self) -> str:
        """Which neighbours 'Add related' pulls in: 'either', 'forward', 'reverse'."""
        return self._related_direction.currentData()

    def snapshot_for_undo(self, label: str):
        """Remember the current selection so the next change can be undone.

        ``label`` names the action on the button (e.g. "Undo add", "Undo path").
        """
        self._undo_snapshot = self.selected_tables()
        self._undo_btn.setText(f"&Undo {label}")
        self._undo_btn.setVisible(True)

    def undo_last_change(self):
        """Restore the selection captured before the last add / traced path."""
        if self._undo_snapshot is None:
            return
        self.set_selected_tables(self._undo_snapshot)
        self._undo_snapshot = None
        self._undo_btn.setText("&Undo")
        self._undo_btn.setVisible(False)
        self.applied.emit()

    def retheme(self):
        muted = _muted_hex(self)
        self._count.setStyleSheet(f"color: {muted};")
        self._path_hint.setStyleSheet(f"color: {muted}; font-size: 11px;")


class TableInspector(QWidget):
    """The Details panel's Table view: one table, its columns and neighbours.

    Shows the table picked in the diagram or the list. Column ticks change the
    diagram (shared with the Columns view); each foreign-key neighbour has an
    Add action, and double-clicking one moves the view to it. Dynamics' audit
    and ownership links are folded into their own collapsed group, since one
    table can have thousands of them.
    """

    column_toggled = Signal(str, str, bool)  # table, column, included
    add_requested = Signal(str)  # tick this table
    select_requested = Signal(str)  # move the view (and selection) here

    _ROLE_TABLE = Qt.UserRole
    _ADD_COL = 1

    def __init__(self, parent=None):
        super().__init__(parent)
        self._table = ""
        self._drawn: set[str] = set()
        self._updating = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self._title = QLabel()
        self._title.setStyleSheet("font-weight: 600;")
        self._title.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._meta = QLabel()
        self._meta.setWordWrap(True)
        self._empty = QLabel(
            "Click a table in the diagram, or select one in the list, to see "
            "its columns and the tables it connects to."
        )
        self._empty.setWordWrap(True)
        self._empty.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        layout.addWidget(self._title)
        layout.addWidget(self._meta)
        layout.addWidget(self._empty)

        split = QSplitter(Qt.Vertical)
        self._columns = QListWidget()
        self._columns.setAccessibleName("Columns of the selected table")
        self._columns.setUniformItemSizes(True)
        self._columns.itemChanged.connect(self._on_column_changed)
        self._links = QTreeWidget()
        self._links.setAccessibleName("Tables connected to the selected table")
        self._links.setColumnCount(2)
        self._links.setHeaderHidden(True)
        self._links.setRootIsDecorated(True)
        self._links.setUniformRowHeights(True)
        self._links.itemClicked.connect(self._on_link_clicked)
        self._links.itemActivated.connect(self._on_link_activated)
        header = self._links.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        split.addWidget(self._columns)
        split.addWidget(self._links)
        split.setSizes([300, 300])
        self._split = split
        layout.addWidget(split, 1)
        self._hint = QLabel(
            "Tick columns to show them. Double-click a connected table to move "
            "to it; Add (or Space) ticks it in the diagram."
        )
        self._hint.setWordWrap(True)
        layout.addWidget(self._hint)
        self.retheme()
        self.show_table(None, [], [], set(), set())

    def current_table(self) -> str:
        return self._table

    def show_table(self, table, outgoing, incoming, drawn: set[str], excluded: set):
        """Fill the view for ``table`` (None shows the empty state).

        ``outgoing`` / ``incoming`` are the table's relationships, ``drawn`` the
        lowercase names of tables in the diagram, ``excluded`` the hidden
        (table, column) pairs.
        """
        self._table = table.name if table is not None else ""
        self._drawn = drawn
        has = table is not None
        for widget in (self._title, self._meta, self._split, self._hint):
            widget.setVisible(has)
        self._empty.setVisible(not has)
        if not has:
            self._columns.clear()
            self._links.clear()
            return

        key = table.name.lower()
        shown = sum(1 for c in table.columns if (key, c.name.lower()) not in excluded)
        self._title.setText(table.full_name)
        in_diagram = "in the diagram" if key in drawn else "not in the diagram"
        self._meta.setText(
            f"{_plural(len(table.columns), 'column')} · {shown:,} shown · "
            f"{_plural(len(outgoing), 'reference')} out · "
            f"{len(incoming):,} in · {in_diagram}"
        )

        self._updating = True
        self._columns.clear()
        for column in table.columns:
            marks = [m for m, on in (("PK", column.is_primary_key),
                                     ("FK", bool(column.foreign_key_reference))) if on]
            text = f"{column.name}  ·  {column.rendered_type()}"
            if marks:
                text += f"  [{', '.join(marks)}]"
            item = QListWidgetItem(text)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setData(Qt.UserRole, column.name)
            excluded_here = (key, column.name.lower()) in excluded
            item.setCheckState(Qt.Unchecked if excluded_here else Qt.Checked)
            self._columns.addItem(item)
        self._updating = False

        self._links.clear()
        self._add_group("References", [(r.parent_table, r) for r in outgoing])
        self._add_group("Referenced by", [(r.child_table, r) for r in incoming])

    def _add_group(self, title: str, pairs):
        regular = [(t, r) for t, r in pairs if not is_audit_relationship(r)]
        audit = [(t, r) for t, r in pairs if is_audit_relationship(r)]
        group = QTreeWidgetItem(self._links, [f"{title} ({len(pairs):,})"])
        group.setFirstColumnSpanned(True)
        group.setFlags(group.flags() & ~Qt.ItemIsSelectable)
        self._add_rows(group, regular)
        if audit:
            folded = QTreeWidgetItem(group, [f"Audit and ownership links ({len(audit):,})"])
            folded.setFirstColumnSpanned(True)
            folded.setFlags(folded.flags() & ~Qt.ItemIsSelectable)
            self._add_rows(folded, audit)
            folded.setExpanded(False)
        group.setExpanded(True)

    def _add_rows(self, parent, pairs):
        for other, rel in sorted(pairs, key=lambda p: (p[0].lower(), p[1].label.lower())):
            drawn = other.lower() in self._drawn
            # One cell, table first: the panel is narrow and the column names
            # are long, so a separate "through" column squeezed the table out.
            row = QTreeWidgetItem(
                parent, [f"{other}  ·  via {rel.label}", "in diagram" if drawn else "Add"]
            )
            row.setData(0, self._ROLE_TABLE, other)
            row.setToolTip(0, f"{other} (through {rel.label}). Double-click to show it here.")
            row.setToolTip(self._ADD_COL, "" if drawn else f"Tick {other} in the diagram")
            if not drawn:
                font = row.font(self._ADD_COL)
                font.setUnderline(True)
                row.setFont(self._ADD_COL, font)
                row.setForeground(self._ADD_COL, QColor(_link_hex(self)))

    def _on_column_changed(self, item):
        if self._updating or not self._table:
            return
        self.column_toggled.emit(
            self._table, item.data(Qt.UserRole), item.checkState() == Qt.Checked
        )

    def _on_link_clicked(self, item, column):
        other = item.data(0, self._ROLE_TABLE)
        if other and column == self._ADD_COL and other.lower() not in self._drawn:
            self.add_requested.emit(other)

    def _on_link_activated(self, item, _column):
        other = item.data(0, self._ROLE_TABLE)
        if other:
            self.select_requested.emit(other)

    def keyPressEvent(self, event):  # noqa: N802 (Qt naming)
        item = self._links.currentItem() if self._links.hasFocus() else None
        other = item.data(0, self._ROLE_TABLE) if item is not None else None
        if event.key() == Qt.Key_Space and other and other.lower() not in self._drawn:
            self.add_requested.emit(other)
            event.accept()
            return
        super().keyPressEvent(event)

    def retheme(self):
        muted = f"color: {_muted_hex(self)};"
        for label in (self._meta, self._empty, self._hint):
            label.setStyleSheet(muted)


class ColumnSelector(QWidget):
    """A tab with a checkable tree (table → columns) to choose diagram columns.

    Owns the set of *excluded* ``(table, column)`` pairs — columns are included
    unless unticked — so the choice survives re-rendering and changing which
    tables are shown.
    """

    changed = Signal()  # the user changed which columns show, or their order

    # Item data roles.
    _ROLE_KIND = Qt.UserRole  # "table" | "column"
    _ROLE_KEYS = Qt.UserRole + 1  # (table_lower, column_lower, is_key)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._excluded: set[tuple[str, str]] = set()
        self._signature: tuple[str, ...] | None = None
        self._tables: list[Table] = []
        self._updating = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # Frame this tab as a refinement of the tables picked on the left, not a
        # second place to select tables. Palette-coloured (no stylesheet) so it
        # tracks light/dark on its own.
        self._header = QLabel()
        self._header.setWordWrap(True)
        self._header.setVisible(False)
        layout.addWidget(self._header)

        # A dropdown to focus on one table's columns (fast for huge schemas).
        scope_row = QHBoxLayout()
        self._scope = QComboBox()
        scope_label = QLabel("Show:")
        scope_label.setBuddy(self._scope)
        self._scope.setAccessibleName("Show columns for")
        scope_row.addWidget(scope_label)
        self._scope.currentIndexChanged.connect(lambda _i: self._rebuild_view())
        scope_row.addWidget(self._scope, 1)
        layout.addLayout(scope_row)

        self._hint = QLabel(
            "Select tables at left, then refine which of their columns to "
            "include here."
        )
        self._hint.setWordWrap(True)
        self._hint.setStyleSheet(f"color: {_muted_hex(self)};")
        layout.addWidget(self._hint)

        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Filter columns…")
        self._filter.setAccessibleName("Filter columns")
        self._filter.setClearButtonEnabled(True)
        self._filter.textChanged.connect(self._apply_filter_text)
        layout.addWidget(self._filter)

        self._tree = QTreeWidget()
        sort_row = QHBoxLayout()
        self._sort = QComboBox()
        sort_label = QLabel("Sort columns:")
        sort_label.setBuddy(self._sort)
        self._sort.setAccessibleName("Sort columns")
        sort_row.addWidget(sort_label)
        for label, value in (
            ("Original order", "order"),
            ("Name A–Z", "name"),
            ("Name Z–A", "name_desc"),
            ("Data type", "type"),
        ):
            self._sort.addItem(label, value)
        self._sort.currentIndexChanged.connect(self._on_order_changed)
        sort_row.addWidget(self._sort)
        self._keys_first = QCheckBox("PK, FK first")
        self._keys_first.setToolTip(
            "Place primary-key columns, then foreign-key columns, above other "
            "columns in each table."
        )
        self._keys_first.toggled.connect(self._on_order_changed)
        sort_row.addWidget(self._keys_first)
        sort_row.addStretch(1)
        layout.addLayout(sort_row)
        self._tree.setHeaderHidden(True)
        self._tree.setAccessibleName("Columns to include")
        self._tree.setUniformRowHeights(True)
        self._tree.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self._tree, 1)

        self._count = QLabel("0 of 0 columns included")
        self._count.setStyleSheet(f"color: {_muted_hex(self)};")
        layout.addWidget(self._count)

        button_row = QHBoxLayout()
        all_btn = QPushButton("All")
        none_btn = QPushButton("None")
        keys_btn = QPushButton("Keys only")
        for btn, tip in (
            (all_btn, "Include every column (across all tables)."),
            (none_btn, "Exclude every column (across all tables)."),
            (keys_btn, "Include only primary-key and foreign-key columns everywhere."),
        ):
            btn.setToolTip(tip)
        all_btn.clicked.connect(lambda: self._bulk("all"))
        none_btn.clicked.connect(lambda: self._bulk("none"))
        keys_btn.clicked.connect(lambda: self._bulk("keys"))
        button_row.addWidget(all_btn)
        button_row.addWidget(none_btn)
        button_row.addWidget(keys_btn)
        # All / None / Keys only rewrite every table's columns at once, so the
        # previous choice is kept for one undo.
        self._undo_btn = QPushButton("Undo")
        self._undo_btn.setToolTip(
            "Restore the columns from before the last All / None / Keys only."
        )
        self._undo_btn.clicked.connect(self._undo_bulk)
        self._undo_btn.setVisible(False)
        self._undo_excluded: set[tuple[str, str]] | None = None
        button_row.addWidget(self._undo_btn)
        layout.addLayout(button_row)

    # -- population --------------------------------------------------------
    def set_tables(self, tables: list[Table]):
        """Point the picker at ``tables``; the excluded set persists.

        Rebuilds the scope dropdown and view only when the set of tables
        actually changes, so re-rendering (e.g. tweaking diagram options) keeps
        the user's current table focus and column edits.
        """
        signature = tuple(f"{t.schema}.{t.name}" for t in tables)
        if signature == self._signature:
            return

        self._signature = signature
        self._tables = list(tables)

        total = sum(len(t.columns) for t in tables)
        self._scope.blockSignals(True)
        self._scope.clear()
        self._scope.addItem(f"All tables ({total:,} columns)", -1)
        for i, table in enumerate(tables):
            self._scope.addItem(f"{table.full_name} ({len(table.columns)})", i)
        self._scope.setCurrentIndex(0)
        self._scope.blockSignals(False)
        self._scope.setVisible(bool(tables))

        self._rebuild_view()

    def focus_table(self, name: str):
        """Show one table's columns ("" goes back to all tables)."""
        index = next(
            (i for i, t in enumerate(self._tables) if t.name == name), -1
        ) if name else -1
        combo_index = self._scope.findData(index)
        if combo_index >= 0 and combo_index != self._scope.currentIndex():
            self._scope.setCurrentIndex(combo_index)

    def _scope_tables(self) -> list[Table]:
        """The tables the current dropdown choice covers ('All' or just one)."""
        index = self._scope.currentData()
        if index is None or index < 0:
            return self._tables
        if 0 <= index < len(self._tables):
            return [self._tables[index]]
        return self._tables

    def ordered_columns(self, table: Table):
        columns = list(table.columns)
        mode = self._sort.currentData()
        if mode in ("name", "name_desc"):
            columns.sort(key=lambda c: c.name.casefold(), reverse=mode == "name_desc")
        elif mode == "type":
            columns.sort(key=lambda c: (c.data_type.casefold(), c.name.casefold()))
        # Stable grouping retains the chosen order within each key group.
        if self._keys_first.isChecked():
            columns.sort(
                key=lambda c: 0 if c.is_primary_key
                else 1 if c.foreign_key_reference
                else 2
            )
        return columns

    def sorted_schema(self, schema: Schema) -> Schema:
        return Schema(
            tables=[
                Table(t.schema, t.name, self.ordered_columns(t))
                for t in schema.tables
            ],
            relationships=schema.relationships,
        )

    def _rebuild_view(self):
        """(Re)build the tree for the current dropdown scope."""
        if not self._tables:
            self._header.setVisible(False)
            self._tree.clear()
            self._tree.setVisible(False)
            self._filter.setVisible(False)
            self._hint.setText(
                "Select tables at left, then refine which of their columns to "
                "include here."
            )
            self._hint.setVisible(True)
            self._update_count()
            return

        n = len(self._tables)
        self._header.setText(
            f"Columns for the {n} table{'' if n == 1 else 's'} selected at left. "
            "Untick a column to leave it out; the diagram updates as you go."
        )
        self._header.setVisible(True)

        scope = self._scope_tables()
        is_all = self._scope.currentData() in (None, -1)
        total_all = sum(len(t.columns) for t in self._tables)

        # The "All tables" view is only built when it's not too heavy; otherwise
        # the dropdown is the way in (one table at a time is always fine).
        if is_all and total_all > COLUMN_TREE_LIMIT:
            self._tree.clear()
            self._tree.setVisible(False)
            self._filter.setVisible(False)
            self._hint.setText(
                f"This selection has {total_all:,} columns — too many to list at "
                "once. Pick a single table from the “Show” dropdown above to "
                "choose its columns (or narrow the tables on the left)."
            )
            self._hint.setVisible(True)
            self._update_count()
            return

        self._hint.setVisible(False)
        self._filter.setVisible(True)
        self._tree.setVisible(True)

        self._updating = True
        self._tree.clear()
        for table in scope:
            key = table.name.lower()
            # Show the column count so a table row reads as an "all columns of
            # this table" group toggle, distinct from the plain table names in
            # the left picker.
            parent = QTreeWidgetItem(self._tree, [f"{table.name}  ({len(table.columns)})"])
            parent.setFlags(parent.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsAutoTristate)
            parent.setData(0, self._ROLE_KIND, "table")
            for column in self.ordered_columns(table):
                markers = []
                if column.is_primary_key:
                    markers.append("PK")
                if column.foreign_key_reference:
                    markers.append("FK")
                label = (
                    f"{column.name}  [{', '.join(markers)}]"
                    if markers
                    else column.name
                )
                child = QTreeWidgetItem(parent, [label])
                child.setFlags(child.flags() | Qt.ItemIsUserCheckable)
                is_key = bool(column.is_primary_key or column.foreign_key_reference)
                child.setData(0, self._ROLE_KIND, "column")
                child.setData(0, self._ROLE_KEYS, (key, column.name.lower(), is_key))
                if markers:
                    child.setToolTip(0, f"{', '.join(markers)} key column: {column.name}")
                excluded = (key, column.name.lower()) in self._excluded
                child.setCheckState(0, Qt.Unchecked if excluded else Qt.Checked)
        self._tree.expandAll()
        self._updating = False
        self._apply_filter_text(self._filter.text())
        self._update_count()

    # -- interaction -------------------------------------------------------
    def _on_item_changed(self, item, _column=0):
        if self._updating:
            return
        if item.data(0, self._ROLE_KIND) != "column":
            return  # parent (table) toggles cascade to children via auto-tristate
        table_key, col_key, _is_key = item.data(0, self._ROLE_KEYS)
        pair = (table_key, col_key)
        if item.checkState(0) == Qt.Checked:
            self._excluded.discard(pair)
        else:
            self._excluded.add(pair)
        self._update_count()
        self.changed.emit()

    def _on_order_changed(self, *_args):
        self._rebuild_view()
        self.changed.emit()

    def _undo_bulk(self):
        if self._undo_excluded is None:
            return
        self._excluded = self._undo_excluded
        self._undo_excluded = None
        self._undo_btn.setVisible(False)
        self._sync_tree_checks()
        self._update_count()
        self.changed.emit()

    def _bulk(self, mode: str):
        self._undo_excluded = set(self._excluded)
        self._undo_btn.setText(
            {"all": "Undo all", "none": "Undo none", "keys": "Undo keys only"}[mode]
        )
        self._undo_btn.setVisible(True)
        # Operate on the whole selection (not just the visible scope) so "Keys
        # only" etc. apply everywhere, even for tables not currently listed.
        for table in self._tables:
            table_key = table.name.lower()
            for column in table.columns:
                col_key = column.name.lower()
                is_key = bool(column.is_primary_key or column.foreign_key_reference)
                keep = True if mode == "all" else False if mode == "none" else is_key
                if keep:
                    self._excluded.discard((table_key, col_key))
                else:
                    self._excluded.add((table_key, col_key))
        self._sync_tree_checks()
        self._update_count()
        self.changed.emit()

    def _sync_tree_checks(self):
        """Update the visible tree's checkboxes to match the excluded set."""
        self._updating = True
        for parent in self._top_items():
            for child in self._children(parent):
                table_key, col_key, _is_key = child.data(0, self._ROLE_KEYS)
                state = (
                    Qt.Unchecked
                    if (table_key, col_key) in self._excluded
                    else Qt.Checked
                )
                child.setCheckState(0, state)
        self._updating = False

    def _apply_filter_text(self, text: str):
        needle = text.strip().lower()
        for parent in self._top_items():
            any_visible = False
            for child in self._children(parent):
                hidden = needle not in child.text(0).lower()
                child.setHidden(hidden)
                any_visible = any_visible or not hidden
            parent.setHidden(needle != "" and not any_visible)

    # -- helpers -----------------------------------------------------------
    def _top_items(self):
        return (self._tree.topLevelItem(i) for i in range(self._tree.topLevelItemCount()))

    @staticmethod
    def _children(parent):
        return (parent.child(i) for i in range(parent.childCount()))

    def set_column_included(self, table: str, column: str, included: bool):
        """Include or leave out one column (the Table view's ticks)."""
        pair = (table.lower(), column.lower())
        if (pair not in self._excluded) == included:
            return
        if included:
            self._excluded.discard(pair)
        else:
            self._excluded.add(pair)
        self._sync_tree_checks()
        self._update_count()
        self.changed.emit()

    def excluded_pairs(self) -> set[tuple[str, str]]:
        return set(self._excluded)

    def _update_count(self):
        total = sum(len(t.columns) for t in self._tables)
        excluded_here = sum(
            1
            for t in self._tables
            for c in t.columns
            if (t.name.lower(), c.name.lower()) in self._excluded
        )
        included = total - excluded_here
        self._count.setText(f"{included:,} of {total:,} columns included")

    def retheme(self):
        muted = f"color: {_muted_hex(self)};"
        self._hint.setStyleSheet(muted)
        self._count.setStyleSheet(muted)


class DiagramOptionsBar(QWidget):
    """A compact bar of controls for how the diagram looks and reads.

    Emits :attr:`changed` whenever any option is touched; the window reads
    :meth:`render_style` and :meth:`diagram_options` to re-render.
    """

    changed = Signal()

    # (label, value) pairs for the dropdowns.
    _ORIENTATIONS = [
        ("Left → Right", "LR"),
        ("Top → Bottom", "TB"),
        ("Bottom → Top", "BT"),
        ("Right → Left", "RL"),
    ]
    _SPACINGS = [
        ("Compact", (6, 60, 40)),  # entity_padding, min_width, min_height
        ("Normal", (15, 100, 75)),
        ("Spacious", (28, 140, 100)),
    ]
    _THEMES = [
        ("Default", "default"),
        ("Neutral", "neutral"),
        ("Dark", "dark"),
        ("Forest", "forest"),
        ("Base", "base"),
    ]
    _BACKGROUNDS = [
        ("White", "#ffffff"),
        ("Transparent", "transparent"),
        ("Dark", "#16181d"),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(4)

        self._orientation = self._combo(self._ORIENTATIONS)
        self._spacing = self._combo(self._SPACINGS)
        self._theme = self._combo(self._THEMES)
        self._background = self._combo(self._BACKGROUNDS)
        self._font = QSpinBox()
        self._font.setRange(8, 28)
        self._font.setValue(12)
        self._font.setSuffix(" px")
        self._font.valueChanged.connect(lambda _v: self.changed.emit())
        self._fit_width = QCheckBox("Fit to vie&w")
        self._fit_width.setChecked(True)
        self._fit_width.setToolTip(
            "Size the diagram to the view: enlarged up to 150% when small, "
            "shrunk when large. Exports keep the diagram's own size."
        )
        self._show_comments = QCheckBox("Descriptions/&notes")
        self._show_comments.setChecked(True)
        self._show_comments.setToolTip(
            "Show each column's description, identity, computed and not-null notes."
        )
        self._show_rel_labels = QCheckBox("Relationship la&bels")
        self._show_rel_labels.setChecked(True)
        self._show_rel_labels.setToolTip("Name the foreign-key column on each line.")
        self._prefix_schema = QCheckBox("Prefi&x schema name")
        self._prefix_schema.setToolTip("Title tables as schema.Table instead of Table.")
        self._keys_only = QCheckBox("&Keys only")
        self._keys_only.setToolTip("Show only primary-key and foreign-key columns.")
        for chk in (
            self._fit_width,
            self._show_comments,
            self._show_rel_labels,
            self._prefix_schema,
            self._keys_only,
        ):
            chk.toggled.connect(lambda _v: self.changed.emit())
            # Never clip a checkbox label when the canvas narrows.
            chk.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)

        # Row 1: the everyday choices. Each label is the buddy of its control,
        # so screen readers name the dropdown and Alt+letter jumps to it.
        row1 = QHBoxLayout()
        row1.setSpacing(8)
        for label, widget in [
            ("&Orientation:", self._orientation),
            ("&Theme:", self._theme),
            ("Back&ground:", self._background),
        ]:
            row1.addWidget(self._buddy(label, widget))
            row1.addWidget(widget)
        row1.addSpacing(8)
        row1.addWidget(self._show_rel_labels)
        row1.addWidget(self._keys_only)
        row1.addStretch(1)

        # The rest is fine-tuning, so it sits behind a disclosure, closed by
        # default, that keeps the bar to one line.
        self._more = QToolButton()
        self._more.setText("More")
        self._more.setCheckable(True)
        self._more.setArrowType(Qt.RightArrow)
        self._more.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self._more.setToolTip("Spacing, font size, width, notes and schema prefix.")
        self._more.setStyleSheet(
            "QToolButton { border: none; padding: 2px 6px; border-radius: 4px; }"
            f"QToolButton:hover, QToolButton:focus {{ background: {_ACCENT_WASH}; }}"
            "QToolButton:checked { background: transparent; }"
            f"QToolButton:checked:hover {{ background: {_ACCENT_WASH}; }}"
        )
        self._more.toggled.connect(self._on_more_toggled)
        row1.addWidget(self._more)
        self._row1 = row1
        outer.addLayout(row1)

        self._more_box = QWidget()
        row2 = QHBoxLayout(self._more_box)
        row2.setContentsMargins(0, 0, 0, 0)
        row2.setSpacing(8)
        for label, widget in [
            ("Spac&ing:", self._spacing),
            ("Font si&ze:", self._font),
        ]:
            row2.addWidget(self._buddy(label, widget))
            row2.addWidget(widget)
        row2.addSpacing(8)
        for chk in (self._fit_width, self._show_comments, self._prefix_schema):
            row2.addWidget(chk)
        row2.addStretch(1)
        self._more_box.setVisible(False)
        outer.addWidget(self._more_box)

    def add_trailing_widget(self, widget):
        """Add a control at the right end of the first row (e.g. Details)."""
        self._row1.addWidget(widget)

    @staticmethod
    def _buddy(text: str, widget) -> QLabel:
        label = QLabel(text)
        label.setBuddy(widget)
        widget.setAccessibleName(text.replace("&", "").rstrip(":"))
        return label

    def _on_more_toggled(self, open_: bool):
        self._more.setArrowType(Qt.DownArrow if open_ else Qt.RightArrow)
        self._more_box.setVisible(open_)

    def _combo(self, pairs) -> QComboBox:
        combo = QComboBox()
        for label, value in pairs:
            combo.addItem(label, value)
        combo.currentIndexChanged.connect(lambda _i: self.changed.emit())
        return combo

    # -- read the current choices ------------------------------------------
    def render_style(self) -> RenderStyle:
        padding, min_w, min_h = self._spacing.currentData()
        return RenderStyle(
            theme=self._theme.currentData(),
            background=self._background.currentData(),
            layout_direction=self._orientation.currentData(),
            entity_padding=padding,
            min_entity_width=min_w,
            min_entity_height=min_h,
            use_max_width=self._fit_width.isChecked(),
            font_size=self._font.value(),
        )

    def diagram_options(self) -> DiagramOptions:
        return DiagramOptions(
            show_comments=self._show_comments.isChecked(),
            show_rel_labels=self._show_rel_labels.isChecked(),
            prefix_schema=self._prefix_schema.isChecked(),
            keys_only=self._keys_only.isChecked(),
        )

    def background_value(self) -> str:
        return self._background.currentData()

    def retheme(self):
        # All labels here use the default palette text colour, which already
        # follows the light/dark scheme — nothing hand-coloured to update.
        pass

    # Theme and background are left out on purpose: they follow the OS's light
    # or dark mode at every launch (see apply_system_defaults).
    def _remembered(self):
        return [
            ("orientation", self._orientation),
            ("spacing", self._spacing),
            ("font", self._font),
            ("fit_width", self._fit_width),
            ("notes", self._show_comments),
            ("rel_labels", self._show_rel_labels),
            ("prefix_schema", self._prefix_schema),
            ("keys_only", self._keys_only),
            ("more_open", self._more),
        ]

    def save_state(self, settings: QSettings):
        for key, widget in self._remembered():
            if isinstance(widget, QComboBox):
                value = widget.currentIndex()
            elif isinstance(widget, QSpinBox):
                value = widget.value()
            else:
                value = widget.isChecked()
            settings.setValue(f"diagram/{key}", value)

    def restore_state(self, settings: QSettings):
        """Put back the choices from the last session, without re-rendering."""
        for key, widget in self._remembered():
            value = settings.value(f"diagram/{key}")
            if value is None:
                continue
            widget.blockSignals(True)
            try:
                if isinstance(widget, QComboBox):
                    index = int(value)
                    if 0 <= index < widget.count():
                        widget.setCurrentIndex(index)
                elif isinstance(widget, QSpinBox):
                    widget.setValue(int(value))
                else:
                    widget.setChecked(str(value).lower() in ("true", "1"))
            except (TypeError, ValueError):
                pass  # a hand-edited or stale value: keep the default
            finally:
                widget.blockSignals(False)
        self._on_more_toggled(self._more.isChecked())

    def apply_system_defaults(self, dark: bool):
        """Default the diagram's own theme + background to match the OS."""
        blocked = [
            (w, w.blockSignals(True))
            for w in (self._theme, self._background)
        ]
        self._theme.setCurrentIndex(2 if dark else 0)  # Dark : Default
        self._background.setCurrentIndex(2 if dark else 0)  # Dark : White
        for widget, _ in blocked:
            widget.blockSignals(False)


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


class RenderStatus(QWidget):
    """A small spinner while the diagram renders, then a tick when it's done."""

    def __init__(self, parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)

        self._icon = QLabel()
        self._icon.setFixedWidth(16)
        self._icon.setAlignment(Qt.AlignCenter)
        self._text = QLabel()
        self._text.setStyleSheet(f"color: {_muted_hex(self)};")
        row.addWidget(self._icon)
        row.addWidget(self._text)

        self._timer = QTimer(self)
        self._timer.setInterval(70)
        self._timer.timeout.connect(self._spin)
        self._frame = 0
        self.setVisible(False)

    def _spin(self):
        self._frame = (self._frame + 1) % 12
        self._icon.setPixmap(status_icon("spin", _ACCENT, angle=self._frame * 30))

    def start(self):
        self.setToolTip("")  # a previous failure's reason no longer applies
        self._frame = 0
        self._icon.setPixmap(status_icon("spin", _ACCENT))
        self._text.setText("Rendering…")
        self.setVisible(True)
        self._timer.start()

    def finish(self, ok: bool = True):
        self._timer.stop()
        if ok:
            self._icon.setPixmap(status_icon("ok", _ok_hex(self)))
            self._text.setText("Rendered")
        else:
            self._icon.setPixmap(status_icon("warn", _warn_hex(self)))
            self._text.setText("Render failed")
        self.setVisible(True)

    def stale(self, text: str):
        """The drawn diagram no longer matches the selection."""
        self._timer.stop()
        self._icon.setPixmap(status_icon("stale", _warn_hex(self)))
        self._text.setText(text)
        self.setVisible(True)

    def clear(self):
        self._timer.stop()
        self.setVisible(False)

    def retheme(self):
        self._text.setStyleSheet(f"color: {_muted_hex(self)};")


class ExtractedDataModel(QAbstractTableModel):
    """Read-only model for the raw extracted rows.

    Backing the Extracted-data tab with a model + ``QTableView`` means only the
    visible cells are realised, instead of building a widget item for every cell
    (which was ~n_rows × 17 items on the UI thread for large sheets). The cell
    string doubles as its tooltip, so the full value is available on hover for
    elided cells without any per-cell allocation.
    """

    def __init__(self, headers: list[str], parent=None):
        super().__init__(parent)
        self._headers = list(headers)
        self._rows: list[list[str]] = []

    def set_rows(self, rows: list[list[str]]):
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._headers)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        value = self._rows[index.row()][index.column()]
        if role == Qt.UserRole:
            if self._headers[index.column()] in ("ColumnOrder", "Length", "Precision", "Scale"):
                try:
                    return int(value)
                except (ValueError, TypeError):
                    return -1
            return value.casefold()
        if role in (Qt.DisplayRole, Qt.ToolTipRole):
            return value or None
        return None

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal:
            return self._headers[section]
        return section + 1  # 1-based row numbers, like the old vertical header


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Excel Schema → Mermaid ER Diagram")
        self.setWindowIcon(app_icon())
        self.resize(1450, 760)
        # A floor so the window can't shrink small enough to clip the action bar
        # or collapse the panels; every action is also reachable from the menu.
        self.setMinimumSize(960, 600)

        self._schema: Schema | None = None
        self._mermaid_text: str = ""
        # The table selection last confirmed past the large-diagram warning, so
        # re-rendering the same big selection (e.g. after a render-option change)
        # doesn't re-ask. None means nothing confirmed yet.
        self._render_confirmed_sig: frozenset[str] | None = None
        self._worker: LoadWorker | None = None
        self._update_worker: UpdateCheckWorker | None = None
        self._pending_path: str = ""
        self._loaded_name: str = ""
        self._rendering: bool = False
        # True when the ticked tables/columns differ from the drawn diagram.
        self._stale: bool = False
        self._auto_render_timer = QTimer(self)
        self._auto_render_timer.setSingleShot(True)
        self._auto_render_timer.setInterval(AUTO_RENDER_DELAY_MS)
        # Looked up at fire time (not bound now) so it always calls the current method.
        self._auto_render_timer.timeout.connect(lambda: self._render_selection())
        self._diagram_rendered: bool = False
        self._drawio_schema: Schema | None = None
        self._importer = SchemaImportService()

        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(12)

        self._drop = DropArea(self.load_file_async)
        outer.addWidget(self._drop)

        self._status = StatusLabel("No file loaded.")
        self._status.setTextInteractionFlags(Qt.LinksAccessibleByMouse | Qt.LinksAccessibleByKeyboard)
        self._status.linkActivated.connect(self._on_status_link)
        self._status.setAccessibleName("Status")
        self._status.setStyleSheet(f"color: {_muted_hex(self)};")
        # AutoText (the default) renders the <span> success summary as rich text
        # while keeping plain status messages plain — so a filename with & or <
        # in a plain message can't be mis-parsed as markup.
        outer.addWidget(self._status)

        # First-run nudge: say what the app produces, and offer a one-click
        # sample so a newcomer can see real output without hunting for a file.
        # Hidden the moment a schema loads (see _apply_loaded).
        self._onboard = QWidget()
        onboard_row = QHBoxLayout(self._onboard)
        onboard_row.setContentsMargins(0, 0, 0, 0)
        self._onboard_hint = QLabel(
            "New here? Load a schema to get an ER diagram you can export to "
            "Draw.io, Excalidraw, PDF, PNG or SVG —"
        )
        self._onboard_hint.setStyleSheet(f"color: {_muted_hex(self)};")
        self._sample_btn = QPushButton("try a sample")
        self._sample_btn.setCursor(Qt.PointingHandCursor)
        self._style_sample_btn()
        self._sample_btn.clicked.connect(self.load_sample)
        onboard_row.addWidget(self._onboard_hint)
        onboard_row.addWidget(self._sample_btn)
        onboard_row.addStretch(1)
        outer.addWidget(self._onboard)

        # Progress bar for loading a file; hidden until a load is in flight.
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setTextVisible(True)
        self._progress.setVisible(False)
        outer.addWidget(self._progress)

        # Tabs: extracted data table + generated Mermaid text.
        tabs = QTabWidget()

        # A model + view (not per-cell widgets) so only visible rows are realised.
        self._table_model = ExtractedDataModel(EXPECTED_HEADERS, self)
        self._table_proxy = QSortFilterProxyModel(self)
        self._table_proxy.setSourceModel(self._table_model)
        self._table_proxy.setSortRole(Qt.UserRole)
        self._table = QTableView()
        self._table.setModel(self._table_proxy)
        self._table.setSortingEnabled(True)
        self._table.sortByColumn(-1, Qt.AscendingOrder)
        self._table.setToolTip("Click a column header to sort; click again to reverse.")
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        # Keep rows single-line and let long free-text cells elide rather than
        # wrap into tall rows; the model serves the full value as the tooltip.
        self._table.setWordWrap(False)
        self._table.setTextElideMode(Qt.ElideRight)
        table_header = self._table.horizontalHeader()
        table_header.setSectionResizeMode(QHeaderView.Interactive)
        table_header.setStretchLastSection(True)  # Description soaks up spare width
        table_header.setMinimumSectionSize(44)
        # Size columns from a sample of rows, not all of them, so auto-sizing a
        # huge sheet stays fast.
        table_header.setResizeContentsPrecision(50)
        # Before a file loads, the tab explains what the sheet should look like
        # instead of showing 17 empty column headers.
        self._table_empty = QLabel(
            "<p style='font-weight:600'>No spreadsheet loaded yet.</p>"
            "<p>The sheet needs <b>one row per database column</b>. Only "
            "<b>TableName</b> and <b>ColumnName</b> are required; these headers "
            "are also understood, in any order and case:</p>"
            "<p>" + " · ".join(h for h in EXPECTED_HEADERS
                                if h not in ("TableName", "ColumnName")) + "</p>"
            "<p>Foreign keys come from <b>ForeignKeyReference</b>, written as "
            "<i>dbo.Customer.CustomerID</i>, <i>Customer.CustomerID</i>, "
            "<i>Customer(CustomerID)</i> or just <i>Customer</i>. Running "
            "SQL Server? <b>View → T-SQL statement</b> produces this export.</p>"
        )
        self._table_empty.setTextFormat(Qt.RichText)
        self._table_empty.setWordWrap(True)
        self._table_empty.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self._table_empty.setContentsMargins(24, 20, 24, 20)
        self._table_empty.setMaximumWidth(760)
        self._table_stack = QStackedWidget()
        self._table_stack.addWidget(self._table_empty)
        self._table_stack.addWidget(self._table)
        self._inspector = TableInspector()
        self._inspector.column_toggled.connect(self._columns_set_included)
        self._inspector.add_requested.connect(self._add_table_from_inspector)
        self._inspector.select_requested.connect(self._focus_table)
        tabs.addTab(self._inspector, "Table")
        tabs.setTabToolTip(0, "The selected table: its columns and connected tables")
        tabs.addTab(self._table_stack, "Data")
        tabs.setTabToolTip(1, "The rows read from the spreadsheet")

        self._columns = ColumnSelector()
        self._columns.changed.connect(self._on_selection_edited)
        tabs.addTab(self._columns, "Columns")

        self._mermaid_view = QPlainTextEdit()
        self._mermaid_view.setAccessibleName("Mermaid source")
        self._mermaid_view.setReadOnly(True)
        self._mermaid_view.setFont(QFont("Menlo, Consolas, monospace"))
        self._mermaid_view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self._mermaid_view.setPlaceholderText(
            "The generated Mermaid erDiagram will appear here."
        )
        tabs.addTab(self._mermaid_view, "Mermaid")

        # SQL query: a T-SQL SELECT over the diagram's tables, joined on their
        # foreign keys, listing the columns chosen in the Columns tab.
        sql_tab = QWidget()
        sql_layout = QVBoxLayout(sql_tab)
        sql_layout.setContentsMargins(8, 8, 8, 8)
        sql_layout.setSpacing(6)
        sql_row = QHBoxLayout()
        sql_row.setSpacing(8)
        self._sql_root = QComboBox()
        self._sql_root.setToolTip("The table in FROM; joins branch out from it.")
        self._sql_join = QComboBox()
        self._sql_join.addItem("INNER JOIN", "INNER")
        self._sql_join.addItem("LEFT JOIN", "LEFT")
        self._sql_join.setToolTip(
            "INNER keeps only rows that match in every table; LEFT keeps every "
            "row of the start table."
        )
        self._sql_top = QSpinBox()
        self._sql_top.setRange(0, 1_000_000)
        self._sql_top.setValue(100)
        self._sql_top.setSpecialValueText("No limit")
        self._sql_top.setToolTip("SELECT TOP (n). 0 means no limit.")
        # Size to their longest value so the narrow panel never clips them.
        self._sql_join.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self._sql_top.setMinimumWidth(self._sql_top.fontMetrics().horizontalAdvance("1000000") + 48)
        # Two rows so the controls fit the narrow Details panel: the start
        # table gets the width its long names need; the rest share row two.
        sql_row2 = QHBoxLayout()
        sql_row2.setSpacing(8)
        for row, text, widget in (
            (sql_row, "Start table:", self._sql_root),
            (sql_row2, "&Join:", self._sql_join),
            (sql_row2, "Row limit:", self._sql_top),
        ):
            label = QLabel(text)
            label.setBuddy(widget)
            widget.setAccessibleName(text.replace("&", "").rstrip(":"))
            row.addWidget(label)
            row.addWidget(widget, 1 if widget is self._sql_root else 0)
        sql_row2.addStretch(1)
        self._copy_sql_btn = QPushButton("Copy S&QL")
        self._copy_sql_btn.setToolTip("Copy the query to the clipboard (Ctrl+Shift+Q).")
        self._copy_sql_btn.clicked.connect(self.copy_sql)
        sql_row2.addWidget(self._copy_sql_btn)
        sql_layout.addLayout(sql_row)
        sql_layout.addLayout(sql_row2)
        self._sql_view = QPlainTextEdit()
        self._sql_view.setReadOnly(True)
        self._sql_view.setFont(QFont("Menlo, Consolas, monospace"))
        self._sql_view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self._sql_view.setAccessibleName("SQL query")
        self._sql_view.setPlaceholderText(
            "A SELECT joining the diagram's tables on their foreign keys "
            "appears here once tables are selected."
        )
        sql_layout.addWidget(self._sql_view, 1)
        self._sql_tab = sql_tab  # added after the diagram tab, below
        self._sql_schema: Schema | None = None
        self._sql_root.currentIndexChanged.connect(lambda _i: self._refresh_sql())
        self._sql_join.currentIndexChanged.connect(lambda _i: self._refresh_sql())
        self._sql_top.valueChanged.connect(lambda _v: self._refresh_sql())

        # Rendered diagram tab = an options bar above the actual diagram view.
        self._diagram_view = DiagramView()
        self._options_bar = DiagramOptionsBar()
        self._options_bar.changed.connect(self._render_selection)
        self._render_status = RenderStatus()
        # A Stop control that only appears while a diagram is rendering, so a
        # slow/large layout can be abandoned without waiting it out.
        self._stop_btn = QPushButton("Stop")
        self._stop_btn.setToolTip("Stop the current render.")
        self._stop_btn.setVisible(False)
        self._stop_btn.clicked.connect(self.cancel_render)
        self._diagram_view.render_started.connect(self._render_status.start)
        self._diagram_view.render_started.connect(lambda: self._set_rendering(True))
        self._diagram_view.render_finished.connect(self._render_status.finish)
        self._diagram_view.render_finished.connect(self._announce_render)
        self._diagram_view.render_error.connect(self._on_render_error)
        self._diagram_view.entity_clicked.connect(self._on_entity_clicked)
        self._entity_to_table: dict[str, str] = {}
        self._diagram_view.render_finished.connect(lambda _ok: self._set_rendering(False))
        diagram_tab = QWidget()
        diagram_layout = QVBoxLayout(diagram_tab)
        diagram_layout.setContentsMargins(0, 0, 0, 0)
        diagram_layout.setSpacing(4)
        diagram_layout.addWidget(self._options_bar)
        self._divider = QFrame()
        self._divider.setFrameShape(QFrame.HLine)
        self._divider.setStyleSheet(f"color: {_line_hex(self)};")
        diagram_layout.addWidget(self._divider)
        # The render status (spinner → tick) and Stop share the diagram's
        # bottom strip with the zoom controls, instead of spending a row.
        self._diagram_view.add_status_widget(self._render_status)
        self._diagram_view.add_status_widget(self._stop_btn)
        # The canvas shows either the ER diagram or, for big schemas, the map.
        self._map_view = SchemaMapView()
        self._canvas = QStackedWidget()
        self._canvas.addWidget(self._diagram_view)
        self._canvas.addWidget(self._map_view)
        diagram_layout.addWidget(self._canvas, 1)
        tabs.addTab(self._sql_tab, "SQL")
        tabs.setUsesScrollButtons(False)

        # Diagram-first: the diagram is the centre of the window, always in
        # view; the other views sit in a Details panel on the right, closed
        # by default so the canvas gets the width.
        self._tabs = tabs
        self._diagram_tab = diagram_tab
        tabs.setMinimumWidth(300)
        tabs.setVisible(False)
        self._details_btn = QToolButton()
        self._details_btn.setText("Details")
        self._details_btn.setCheckable(True)
        self._details_btn.setArrowType(Qt.LeftArrow)
        self._details_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self._details_btn.setToolTip(
            "Show or hide the Details panel: extracted data, columns, Mermaid "
            "source and SQL (Ctrl+I; Ctrl+1–5 open a view)."
        )
        self._details_btn.setStyleSheet(
            "QToolButton { border: none; padding: 2px 6px; border-radius: 4px; }"
            f"QToolButton:hover, QToolButton:focus {{ background: {_ACCENT_WASH}; }}"
            "QToolButton:checked { background: transparent; font-weight: 600; }"
            f"QToolButton:checked:hover {{ background: {_ACCENT_WASH}; }}"
        )
        self._details_btn.toggled.connect(self.set_details_visible)
        self._map_btn = QToolButton()
        self._map_btn.setText("Map")
        self._map_btn.setCheckable(True)
        self._map_btn.setToolTip(
            "Show the whole schema as a map of clusters, to pick tables from (Ctrl+M)"
        )
        self._map_btn.setStyleSheet(self._details_btn.styleSheet())
        self._map_btn.toggled.connect(self.show_map)
        self._options_bar.add_trailing_widget(self._map_btn)
        self._options_bar.add_trailing_widget(self._details_btn)
        self._map_view.table_activated.connect(self._on_map_table_activated)
        self._map_view.draw_requested.connect(self._draw_from_map)
        self._map_view.show_diagram_requested.connect(lambda: self.show_map(False))

        # Left: table picker to limit what gets rendered. Right: the tabs.
        self._selector = TableSelector()
        self._selector.applied.connect(self._render_selection)
        self._selector.selection_changed.connect(self._on_selection_edited)
        self._selector.current_table_changed.connect(self._on_list_table_changed)
        self._selector._filter.textChanged.connect(lambda t: self._map_view.set_filter(t))
        self._selector.related_requested.connect(self._add_related_tables)
        self._selector.path_requested.connect(self._find_shortest_path)

        body = QSplitter(Qt.Horizontal)
        self._body = body
        body.addWidget(self._selector)
        body.addWidget(diagram_tab)
        body.addWidget(tabs)
        body.setStretchFactor(0, 0)
        body.setStretchFactor(1, 1)
        body.setStretchFactor(2, 0)
        body.setCollapsible(1, False)
        body.setSizes([280, 940, 380])
        outer.addWidget(body, 1)

        # The bottom bar holds one job, getting the diagram out: Export is the
        # lead (a split button; its arrow picks another format and exports in
        # it), Preview sits beside it, and the Mermaid text outputs share one
        # menu. Labels ending in "…" open a file dialog.
        buttons = QHBoxLayout()
        self._export_kind = self._remembered_export_kind()
        self._export_btn = QToolButton()
        self._export_btn.setPopupMode(QToolButton.MenuButtonPopup)
        self._export_btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self._export_btn.setToolTip(
            "Export the diagram. Use the arrow to export in another format."
        )
        self._export_btn.setAccessibleName("Export diagram")
        self._export_btn.clicked.connect(self.export_diagram)
        export_menu = QMenu(self._export_btn)
        self._export_actions: dict[str, QAction] = {}
        for key, label, *_rest in EXPORT_FORMATS:
            action = export_menu.addAction(f"Export as {label}…")
            action.triggered.connect(
                lambda _checked=False, k=key: self._export_as(k)
            )
            self._export_actions[key] = action
        export_menu.addSeparator()
        bg_menu = export_menu.addMenu("&Background")
        self._export_bg_group = QActionGroup(self)
        saved_bg = str(app_settings().value("export/background", "white"))
        for key, label in EXPORT_BACKGROUNDS:
            action = bg_menu.addAction(label)
            action.setCheckable(True)
            action.setData(key)
            action.setChecked(key == saved_bg)
            self._export_bg_group.addAction(action)
        if self._export_bg_group.checkedAction() is None:
            self._export_bg_group.actions()[0].setChecked(True)
        self._export_bg_group.triggered.connect(
            lambda a: app_settings().setValue("export/background", a.data())
        )
        scale_menu = export_menu.addMenu("PNG &scale")
        self._png_scale_group = QActionGroup(self)
        try:
            saved_scale = float(app_settings().value("export/png_scale", 2.0))
        except (TypeError, ValueError):
            saved_scale = 2.0
        for scale in PNG_SCALES:
            action = scale_menu.addAction(f"{scale:g}×")
            action.setCheckable(True)
            action.setData(scale)
            action.setChecked(abs(scale - saved_scale) < 1e-6)
            self._png_scale_group.addAction(action)
        if self._png_scale_group.checkedAction() is None:
            self._png_scale_group.actions()[1].setChecked(True)
        self._png_scale_group.triggered.connect(
            lambda a: app_settings().setValue("export/png_scale", a.data())
        )
        self._export_btn.setMenu(export_menu)
        _apply_primary_button_style(self._export_btn)

        self._preview_btn = QPushButton("&Preview in browser")
        self._preview_btn.setToolTip(
            "Open the diagram in your web browser (works offline)."
        )

        self._mermaid_btn = QPushButton("&Mermaid")
        self._mermaid_btn.setToolTip("Copy or save the diagram's Mermaid source.")
        mermaid_menu = QMenu(self._mermaid_btn)
        mermaid_menu.addAction("&Copy Mermaid source", self.copy_mermaid)
        mermaid_menu.addAction("Save as .&mmd…", self.save_mmd)
        mermaid_menu.addAction("Save as Mark&down (.md)…", self.save_md)
        self._mermaid_btn.setMenu(mermaid_menu)

        # Table-list presets are about the selection, so they live with it.
        self._presets_btn = QPushButton("Table &list")
        self._presets_btn.setFlat(True)
        self._presets_btn.setToolTip("Save or load the ticked tables as a .toml preset.")
        presets_menu = QMenu(self._presets_btn)
        presets_menu.addAction("&Save table list…", self.save_table_selection_toml)
        presets_menu.addAction("&Load table list…", self.load_table_selection_toml)
        self._presets_btn.setMenu(presets_menu)
        self._selector.add_header_widget(self._presets_btn)

        self._sync_export_label()
        self._action_buttons = [
            self._export_btn,
            self._preview_btn,
            self._mermaid_btn,
            self._presets_btn,
        ]
        for btn in self._action_buttons:
            btn.setEnabled(False)

        self._button_seps: list[QFrame] = []
        sep = QFrame()
        sep.setFrameShape(QFrame.VLine)
        sep.setFixedHeight(24)
        self._button_seps.append(sep)
        buttons.addWidget(self._export_btn)
        buttons.addWidget(self._preview_btn)
        buttons.addSpacing(4)
        buttons.addWidget(sep)
        buttons.addSpacing(4)
        buttons.addWidget(self._mermaid_btn)
        buttons.addStretch(1)
        outer.addLayout(buttons)

        self._preview_btn.clicked.connect(self.preview_browser)

        self.setCentralWidget(central)
        self._build_sql_dock()
        self._build_menu_bar()

        # Match the OS: default the diagram's own theme/background to dark when
        # the app starts dark, and apply palette-derived colours everywhere.
        app = QApplication.instance()
        if app is not None:
            self._options_bar.apply_system_defaults(system_is_dark(app))
        self._restore_session()
        # The canvas is the first thing anyone sees, so it says what to do.
        self._diagram_view.show_message(
            "No spreadsheet loaded yet.\n\n"
            "Drop an Excel schema file on the bar above, or click it to browse. "
            "New here? Use “try a sample”.\n\n"
            "Once a file loads, the diagram appears here. The Details panel "
            "(Ctrl+I) shows the extracted rows, columns, Mermaid source and SQL."
        )
        save_options = lambda *_: self._options_bar.save_state(app_settings())  # noqa: E731
        self._options_bar.changed.connect(save_options)
        self._options_bar._more.toggled.connect(save_options)
        self.retheme()

    def _build_menu_bar(self):
        """A menu bar so every action has a keyboard path and a shortcut.

        The bottom button row stays as the visible controls; this mirrors them
        for keyboard/screen-reader users (loading a file was otherwise
        mouse-only) and adds the standard accelerators a desktop app is expected
        to have. Schema-dependent actions are disabled until a file loads,
        matching the button row.
        """
        bar = self.menuBar()

        def act(text, slot, shortcut=None, schema_only=False):
            action = QAction(text, self)
            if shortcut is not None:
                action.setShortcut(shortcut)
            action.triggered.connect(slot)
            if schema_only:
                action.setEnabled(False)
                self._schema_actions.append(action)
            return action

        self._schema_actions: list[QAction] = []

        file_menu = bar.addMenu("&File")
        file_menu.addAction(act("&Open…", self.open_file_dialog, QKeySequence.StandardKey.Open))
        self._recent_menu = file_menu.addMenu("Open &Recent")
        self._recent_menu.aboutToShow.connect(self._rebuild_recent_menu)
        self._rebuild_recent_menu()
        file_menu.addAction(
            act("&Load table list…", self.load_table_selection_toml,
                QKeySequence("Ctrl+L"), schema_only=True)
        )
        file_menu.addSeparator()
        file_menu.addAction(
            act("&Save Mermaid (.mmd)…", self.save_mmd,
                QKeySequence.StandardKey.Save, schema_only=True)
        )
        file_menu.addAction(
            act("Save Mark&down (.md)…", self.save_md, schema_only=True)
        )
        file_menu.addAction(
            act("Save &table list…", self.save_table_selection_toml, schema_only=True)
        )
        file_menu.addSeparator()
        file_menu.addAction(act("E&xit", self.close, QKeySequence.StandardKey.Quit))

        view_menu = bar.addMenu("&View")
        view_menu.addAction(
            act("Zoom &in", lambda: self._diagram_view.zoom_by(1.25),
                QKeySequence.StandardKey.ZoomIn, schema_only=True)
        )
        view_menu.addAction(
            act("Zoom &out", lambda: self._diagram_view.zoom_by(0.8),
                QKeySequence.StandardKey.ZoomOut, schema_only=True)
        )
        view_menu.addAction(
            act("&Actual size", self._diagram_view.reset_zoom,
                QKeySequence("Ctrl+0"), schema_only=True)
        )
        view_menu.addSeparator()
        self._map_action = act("Schema ma&p", lambda: self.show_map(not self.map_visible()),
                               QKeySequence("Ctrl+M"), schema_only=True)
        view_menu.addAction(self._map_action)
        self._details_action = QAction("&Details panel", self)
        self._details_action.setCheckable(True)
        self._details_action.setShortcut(QKeySequence("Ctrl+I"))
        self._details_action.toggled.connect(self.set_details_visible)
        view_menu.addAction(self._details_action)
        for number, (label, widget) in enumerate(
            [
                ("&Table", self._inspector),
                ("E&xtracted data", self._table_stack),
                ("&Columns", self._columns),
                ("&Mermaid source", self._mermaid_view),
                ("&SQL query", self._sql_tab),
            ],
            start=1,
        ):
            action = QAction(label, self)
            action.setShortcut(QKeySequence(f"Ctrl+{number}"))
            action.triggered.connect(lambda _c=False, w=widget: self.show_details(w))
            view_menu.addAction(action)
        view_menu.addSeparator()
        view_menu.addAction(self._sql_dock.toggleViewAction())

        diagram_menu = bar.addMenu("&Diagram")
        diagram_menu.addAction(
            act("&Render selected", self._render_selection,
                QKeySequence("F5"), schema_only=True)
        )
        # Enabled only while a render is in flight (see _set_rendering).
        self._stop_action = act("&Stop rendering", self.cancel_render, QKeySequence("Esc"))
        self._stop_action.setEnabled(False)
        diagram_menu.addAction(self._stop_action)
        diagram_menu.addSeparator()
        diagram_menu.addAction(
            act("&Copy Mermaid", self.copy_mermaid,
                QKeySequence("Ctrl+Shift+C"), schema_only=True)
        )
        diagram_menu.addAction(
            act("Copy S&QL query", self.copy_sql,
                QKeySequence("Ctrl+Shift+Q"), schema_only=True)
        )
        diagram_menu.addAction(
            act("&Export diagram…", self.export_diagram,
                QKeySequence("Ctrl+E"), schema_only=True)
        )
        diagram_menu.addAction(
            act("&Preview in browser", self.preview_browser, schema_only=True)
        )

        help_menu = bar.addMenu("&Help")
        self._update_action = act("Check for &updates…", self.check_for_updates)
        help_menu.addAction(self._update_action)
        help_menu.addAction(act("&About", self.show_about))

    def _build_sql_dock(self):
        """Create the fixed right-side panel with the SQL Server export query."""
        self._sql_dock = QDockWidget("T-SQL statement", self)
        self._sql_dock.setObjectName("sqlServerQueryDock")
        self._sql_dock.setAllowedAreas(Qt.RightDockWidgetArea)
        self._sql_dock.setFeatures(QDockWidget.DockWidgetClosable)

        panel = QWidget(self._sql_dock)
        layout = QVBoxLayout(panel)
        hint = QLabel(
            "Run this in the SQL Server database you want to document, then export "
            "the results with column headers to an .xlsx file."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self._sql_query_view = QPlainTextEdit()
        self._sql_query_view.setObjectName("sqlServerSchemaQuery")
        self._sql_query_view.setAccessibleName("SQL Server schema export query")
        self._sql_query_view.setReadOnly(True)
        self._sql_query_view.setFont(QFont("Menlo, Consolas, monospace"))
        self._sql_query_view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self._sql_query_view.setPlainText(SQL_SERVER_SCHEMA_QUERY)
        layout.addWidget(self._sql_query_view, 1)

        copy_button = QPushButton("Copy T-SQL")
        copy_button.clicked.connect(self.copy_sql_query)
        layout.addWidget(copy_button)

        self._sql_dock.setWidget(panel)
        self.addDockWidget(Qt.RightDockWidgetArea, self._sql_dock)
        self.resizeDocks([self._sql_dock], [410], Qt.Horizontal)
        self._sql_dock.hide()

    def copy_sql_query(self):
        """Copy the displayed SQL Server schema query to the clipboard."""
        QGuiApplication.clipboard().setText(SQL_SERVER_SCHEMA_QUERY)
        self._status.setText("T-SQL statement copied to clipboard.")

    def open_file_dialog(self):
        """Open the file picker from the menu / Ctrl+O and load the choice."""
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open an Excel schema file",
            "",
            "Excel files (*.xlsx *.xlsm *.xltx *.xltm);;All files (*)",
        )
        if path:
            self.load_file_async(path)

    def _on_render_error(self, reason: str):
        first_line = reason.strip().splitlines()[0] if reason.strip() else ""
        self._render_status.setToolTip(reason)
        first_line = first_line.rstrip(":. ")
        self._status.setText(
            "The diagram couldn't be drawn"
            + (f": {first_line}." if first_line else ".")
            + " Details are in the Rendered diagram tab."
        )

    def _announce_render(self, ok: bool):
        """Tell a screen reader when a render finishes (the tick is visual only)."""
        announce(self, "Diagram rendered" if ok else "Diagram render failed")

    def _on_selection_edited(self):
        self._map_view.set_ticked(self._selector.selected_tables())
        self._on_selection_edited_render()

    def _on_selection_edited_render(self):
        """Tables or columns changed: redraw shortly, or flag the diagram stale.

        Small selections follow the ticks automatically (debounced). Past
        RENDER_WARN_LIMIT each new selection would re-ask the large-diagram
        question, so those wait for an explicit Render (F5) instead.
        """
        if self._schema is None:
            return
        self._stale = True
        count = len(self._selector.selected_tables())
        if count > RENDER_WARN_LIMIT:
            self._auto_render_timer.stop()
            self._render_status.stale(
                f"Out of date ({count} tables) · press F5 to render"
            )
        else:
            self._auto_render_timer.start()

    def _ensure_current(self) -> bool:
        """Bring the diagram up to date before it's exported or copied.

        Returns False if it is still stale (the user declined the
        large-diagram render), in which case the caller should stop.
        """
        if self._stale:
            self._render_selection()
        if self._stale:
            self._status.setText(
                "The diagram doesn't match your selection yet. Render it first (F5)."
            )
        return not self._stale

    def _set_rendering(self, active: bool):
        """Show/hide the Stop control for the duration of a render."""
        self._stop_btn.setVisible(active)
        self._stop_action.setEnabled(active)

    def cancel_render(self):
        """Abandon the in-progress render (Stop button / menu)."""
        self._diagram_view.cancel_render()
        self._render_status.clear()
        self._set_rendering(False)
        self._status.setText("Render cancelled.")

    def _style_sample_btn(self):
        link = _link_hex(self)
        self._sample_btn.setStyleSheet(
            "QPushButton {"
            f"  color: {link};"
            "  border: 1px solid transparent;"
            "  border-radius: 3px;"
            "  background: transparent;"
            "  padding: 0 2px;"
            "  text-decoration: underline;"
            "}"
            f"QPushButton:focus {{ border-color: {link}; }}"
        )

    def retheme(self):
        """Re-apply palette-derived colours after a light/dark scheme change."""
        self._style_sample_btn()
        muted = f"color: {_muted_hex(self)};"
        self._status.setStyleSheet(muted)
        self._onboard_hint.setStyleSheet(muted)
        line = f"color: {_line_hex(self)};"
        self._divider.setStyleSheet(line)
        for sep in self._button_seps:
            sep.setStyleSheet(line)
        self._drop.retheme()
        self._selector.retheme()
        self._columns.retheme()
        self._options_bar.retheme()
        self._render_status.retheme()

    def changeEvent(self, event):  # noqa: N802 (Qt naming)
        # The palette swap (light↔dark) arrives as a PaletteChange; restyle the
        # widgets whose colours we set by hand.
        if event.type() in (
            QEvent.PaletteChange,
            QEvent.ApplicationPaletteChange,
            QEvent.ThemeChange,
        ):
            self.retheme()
        super().changeEvent(event)

    # -- diagram ↔ list selection ------------------------------------------
    def _on_entity_clicked(self, entity_id: str, double: bool):
        """A table was clicked in the diagram: select it everywhere.

        Single click highlights it in the list and scopes the Columns view to
        it; double-click also opens the Details panel there. Clicking the
        empty canvas (or Esc) clears the selection.
        """
        name = self._entity_to_table.get(entity_id, "")
        self._focus_table(name, from_diagram=True)
        if not name:
            return
        if double:
            self.show_details(self._inspector)
        else:
            self._status.setText(
                f"{html.escape(name)} selected · double-click it for its columns "
                "and connected tables"
            )

    def _on_list_table_changed(self, name: str):
        """A row was highlighted in the list: highlight that table if drawn."""
        entity = next((e for e, t in self._entity_to_table.items() if t == name), "")
        self._diagram_view.highlight_entity(entity)
        self._columns.focus_table(name)
        self._refresh_inspector(name)

    # -- schema map -----------------------------------------------------------
    def map_visible(self) -> bool:
        return self._canvas.currentWidget() is self._map_view

    def show_map(self, visible: bool):
        """Switch the canvas between the map and the ER diagram."""
        visible = bool(visible) and self._schema is not None
        self._canvas.setCurrentWidget(self._map_view if visible else self._diagram_view)
        if self._map_btn.isChecked() != visible:
            self._map_btn.blockSignals(True)
            self._map_btn.setChecked(visible)
            self._map_btn.blockSignals(False)
        if visible:
            self._map_view.set_ticked(self._selector.selected_tables())
            self._map_view.set_filter(self._selector._filter.text())

    def _on_map_table_activated(self, name: str, double: bool):
        self._focus_table(name, from_map=True)
        if double:
            self.show_details(self._inspector)

    def _draw_from_map(self, names: list[str]):
        """Tick the map's selection (replacing the ticks, undoably) and draw it."""
        self._selector.snapshot_for_undo("map")
        self._selector.set_selected_tables(names)
        self.show_map(False)
        self._render_selection()

    def _focus_table(self, name: str, from_diagram: bool = False, from_map: bool = False):
        """Make ``name`` the selected table everywhere ("" clears)."""
        self._selector.focus_table(name)
        self._columns.focus_table(name)
        if not from_diagram:
            entity = next((e for e, t in self._entity_to_table.items() if t == name), "")
            self._diagram_view.highlight_entity(entity)
        if not from_map and self.map_visible() and name:
            self._map_view.select([name])
        self._refresh_inspector(name)

    def _refresh_inspector(self, name: str | None = None):
        """Show ``name`` (default: the current one) in the Table view."""
        if name is None:
            name = self._inspector.current_table()
        schema = self._schema
        table = None
        if schema is not None and name:
            table = next((t for t in schema.tables if t.name == name), None)
        if table is None:
            self._inspector.show_table(None, [], [], set(), set())
            return
        key = table.name.lower()
        outgoing = [r for r in schema.relationships if r.child_table.lower() == key]
        incoming = [r for r in schema.relationships if r.parent_table.lower() == key]
        drawn = {t.name.lower() for t in (self._drawio_schema.tables if self._drawio_schema else [])}
        self._inspector.show_table(
            table, outgoing, incoming, drawn, self._columns.excluded_pairs()
        )

    def _columns_set_included(self, table: str, column: str, included: bool):
        self._columns.set_column_included(table, column, included)

    def _add_table_from_inspector(self, name: str):
        """Tick a connected table from the Table view (undoable, auto-renders)."""
        self._selector.snapshot_for_undo("add")
        if self._selector.check_tables([name]):
            self._on_selection_edited()
            self._status.setText(f"Added {html.escape(name)} to the diagram.")

    # -- details panel ----------------------------------------------------
    def set_details_visible(self, visible: bool):
        """Open or close the Details panel, keeping its two toggles in step."""
        visible = bool(visible)
        self._tabs.setVisible(visible)
        for toggle in (self._details_btn, getattr(self, "_details_action", None)):
            if toggle is not None and toggle.isChecked() != visible:
                toggle.blockSignals(True)
                toggle.setChecked(visible)
                toggle.blockSignals(False)
        self._details_btn.setArrowType(Qt.RightArrow if visible else Qt.LeftArrow)
        app_settings().setValue("window/details_open", visible)

    def show_details(self, widget):
        """Open the Details panel on one of its views."""
        self._tabs.setCurrentWidget(widget)
        self.set_details_visible(True)

    # -- recent files and session state --------------------------------------
    def recent_files(self) -> list[str]:
        value = app_settings().value("recent/files", [])
        if isinstance(value, str):  # QSettings returns a bare str for one item
            value = [value]
        return [str(v) for v in (value or []) if v]

    def _remember_recent(self, path: str):
        """Put ``path`` at the top of File → Open Recent (not the temp sample)."""
        path = os.path.abspath(path)
        sample = os.path.abspath(os.path.join(tempfile.gettempdir(), "sample_schema.xlsx"))
        if os.path.normcase(path) == os.path.normcase(sample):
            return
        files = [f for f in self.recent_files()
                 if os.path.normcase(f) != os.path.normcase(path)]
        app_settings().setValue("recent/files", [path, *files][:RECENT_FILES_MAX])

    def _rebuild_recent_menu(self):
        menu = self._recent_menu
        menu.clear()
        files = self.recent_files()
        for i, path in enumerate(files, start=1):
            mnemonic = f"&{i}" if i < 10 else str(i)
            action = menu.addAction(f"{mnemonic}  {os.path.basename(path)}")
            action.setToolTip(path)
            action.setStatusTip(path)
            action.triggered.connect(lambda _c=False, p=path: self._open_recent(p))
        if files:
            menu.addSeparator()
            menu.addAction("&Clear list", self._clear_recent)
        else:
            empty = menu.addAction("No recent files")
            empty.setEnabled(False)
        menu.setToolTipsVisible(True)

    def _open_recent(self, path: str):
        if not os.path.exists(path):
            QMessageBox.information(
                self,
                "File not found",
                f"{path}\n\nThis file has been moved or deleted, so it was "
                "removed from the recent list.",
            )
            app_settings().setValue(
                "recent/files", [f for f in self.recent_files() if f != path]
            )
            return
        self.load_file_async(path)

    def _clear_recent(self):
        app_settings().remove("recent/files")

    def _restore_session(self):
        """Window size, panel split and diagram options from the last session."""
        settings = app_settings()
        geometry = settings.value("window/geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        split = settings.value("window/splitter3")
        if split is not None:
            self._body.restoreState(split)
        open_ = str(settings.value("window/details_open", "false")).lower() in ("true", "1")
        self.set_details_visible(open_)
        tab = settings.value("window/details_tab")
        try:
            if tab is not None and 0 <= int(tab) < self._tabs.count():
                self._tabs.setCurrentIndex(int(tab))
        except (TypeError, ValueError):
            pass
        self._options_bar.restore_state(settings)

    def closeEvent(self, event):  # noqa: N802 (Qt naming)
        settings = app_settings()
        settings.setValue("window/geometry", self.saveGeometry())
        settings.setValue("window/splitter3", self._body.saveState())
        settings.setValue("window/details_tab", self._tabs.currentIndex())
        super().closeEvent(event)

    # -- loading -----------------------------------------------------------
    def load_sample(self):
        """Write the bundled sample schema to a temp file and load it.

        Lets a first-time user see real output in one click without hunting
        for a spreadsheet. Errors surface the same way a normal load would.
        """
        try:
            path = write_sample(
                os.path.join(tempfile.gettempdir(), "sample_schema.xlsx")
            )
        except Exception as exc:  # noqa: BLE001 - surface to the user, don't crash
            QMessageBox.critical(self, "Could not create sample", str(exc))
            return
        self.load_file_async(path)

    def load_file(self, path: str):
        """Load a file synchronously (used by tests and the CLI)."""
        try:
            rows, schema, mermaid_text = self._importer.load(path)
        except Exception as exc:  # noqa: BLE001 - surface any parse error to the user
            QMessageBox.critical(self, "Could not read file", str(exc))
            return

        self._apply_loaded(path, rows, schema, mermaid_text)

    def load_file_async(self, path: str):
        """Load a file on a background thread, showing a progress bar.

        Keeps the window responsive for large spreadsheets. When the worker
        finishes, the results are applied on the UI thread via
        :meth:`_apply_loaded`.
        """
        if self._worker is not None and self._worker.isRunning():
            return  # a load is already in progress; ignore extra drops/clicks

        self._pending_path = path
        self._set_loading(True, os.path.basename(path))

        worker = LoadWorker(path, self, self._importer)
        worker.progressed.connect(self._progress.setValue)
        worker.staged.connect(self._status.setText)
        worker.loaded.connect(self._on_loaded)
        worker.failed.connect(self._on_load_failed)
        worker.finished.connect(self._on_worker_finished)
        self._worker = worker
        worker.start()

    def _on_loaded(self, rows, schema, mermaid_text):
        self._status.setText("Populating view…")
        self._progress.setValue(96)
        self._apply_loaded(self._pending_path, rows, schema, mermaid_text)
        self._progress.setValue(100)

    def _on_load_failed(self, message: str):
        name = os.path.basename(self._pending_path) or "the file"
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Couldn't read the file")
        box.setTextFormat(Qt.PlainText)
        box.setText(f"{name} couldn't be read as a schema spreadsheet.")
        box.setInformativeText(
            f"{message}\n\nThe sheet needs one row per column, with at least a "
            "TableName and a ColumnName header. Show Details lists every header "
            "the app understands; View → T-SQL statement produces a matching export."
        )
        box.setDetailedText(
            "Recognised headers (any order, case and spacing):\n\n"
            + "\n".join(EXPECTED_HEADERS)
        )
        box.exec()
        self._status.setText("No file loaded.")

    def _on_worker_finished(self):
        self._set_loading(False)
        self._worker = None

    def _set_loading(self, loading: bool, name: str = ""):
        """Toggle the progress bar and block re-entrant loads while running."""
        self._progress.setVisible(loading)
        self._drop.setEnabled(not loading)
        if loading:
            self._progress.setRange(0, 100)
            self._progress.setValue(0)
            self._status.setText(f"Loading {name}…")

    def _begin_render(self, message: str):
        """Show a busy (indeterminate) bar while a diagram export runs.

        The export blocks the UI thread, but its wait loop keeps pumping events
        (see DiagramView.current_svg), so a marquee bar animates and the message
        stays visible. Mermaid gives no progress percentage, hence indeterminate.
        """
        self._rendering = True
        self._progress.setRange(0, 0)  # 0..0 == busy indicator
        self._progress.setVisible(True)
        self._status.setText(message)
        self._export_btn.setEnabled(False)
        app = QApplication.instance()
        if app is not None:
            app.processEvents()  # paint the bar before we block

    def _end_render(self):
        self._progress.setVisible(False)
        self._progress.setRange(0, 100)
        self._export_btn.setEnabled(True)
        self._rendering = False

    def _apply_loaded(self, path: str, rows: list[dict], schema: Schema, mermaid_text: str):
        """Push a loaded schema into the widgets (must run on the UI thread).

        The passed-in ``mermaid_text`` is for the full schema; the diagram we
        actually show is driven by the table selection (all tables for a small
        schema, or a user-picked subset for a large one).
        """
        self._schema = schema
        self._loaded_name = os.path.basename(path)
        self._render_confirmed_sig = None  # new file: forget the prior confirmation
        self._populate_table(rows)
        self._table_stack.setCurrentWidget(self._table)
        self._remember_recent(path)
        # The big drop target has done its job; shrink it to a file chip so the
        # tabs get the height, and wake up the (until now inert) table picker.
        self._drop.set_loaded(self._loaded_name)
        self._onboard.setVisible(False)  # onboarding is a first-run nudge only
        self._selector.set_ready(True)

        names = [t.name for t in schema.tables]
        self._selector.set_tables(names)
        self._selector.set_link_counts(link_counts(schema))
        self._map_view.set_schema(schema if names else None)

        # The file parsed but yielded no tables — every row was missing a
        # TableName or ColumnName. Say so plainly instead of "0 tables / select
        # tables on the left" (there are none to select).
        if not names:
            self._diagram_view.show_message(
                "No tables found in this file.\n\n"
                "Each row needs both a TableName and a ColumnName under the "
                "header row. The sheet loaded, but no rows had both — check you "
                "opened the right sheet and that the data sits directly under "
                "the headers."
            )
            self._status.setText(
                f"Loaded {self._loaded_name} — no table/column data found "
                "(each row needs a TableName and a ColumnName)."
            )
            return

        # Small schemas render in full; large ones open on the map, where the
        # shape of the schema shows and a region can be picked to draw.
        if len(names) <= AUTO_RENDER_LIMIT:
            self._selector.check_all()
        self._render_selection()
        self.show_map(len(names) > RENDER_WARN_LIMIT)

        # Foreign keys pointing at tables not in this sheet draw no relationship;
        # flag them so a missing edge isn't a silent surprise.
        dangling = unresolved_foreign_keys(schema)
        if dangling:
            self._status.setText(
                self._status.text()
                + f'  <span style="color:{_muted_hex(self)}">· '
                f"{_plural(len(dangling), 'foreign key')} reference a table not "
                "in this sheet</span>"
            )

        for btn in self._action_buttons:
            btn.setEnabled(True)
        self._sync_export_enabled()
        for action in self._schema_actions:
            action.setEnabled(True)


    def _add_related_tables(self):
        """Add the one-hop FK neighbours of the checked tables.

        Only the neighbours in the chosen direction are considered (B), a large
        batch is previewed and confirmed before it lands (A), and the prior
        selection is snapshotted so the add can be undone (C).
        """
        if self._schema is None:
            return
        names = self._selector.selected_tables()
        if not names:
            self._status.setText("Check at least one table first, then add related.")
            return

        direction = self._selector.related_direction()
        new = sorted(related_tables(self._schema, names, direction))
        if not new:
            self._status.setText("No related tables to add.")
            return
        if len(new) > RELATED_WARN_COUNT and not self._confirm_add_related(new):
            return

        self._selector.snapshot_for_undo("add")
        added = self._selector.check_tables(new)
        self._render_selection()
        self._status.setText(f"Added {_plural(added, 'related table')}.")

    def _confirm_add_related(self, new: list[str]) -> bool:
        """Preview a large related-table batch and let the user back out."""
        count = len(new)
        shown = ", ".join(new[:8])
        more = "" if count <= 8 else f", +{count - 8} more"
        answer = QMessageBox.question(
            self,
            "Add related tables?",
            f"This adds {_plural(count, 'related table')}:\n\n{shown}{more}\n\n"
            "Add them all?",
        )
        return answer == QMessageBox.Yes

    def _find_shortest_path(self):
        """Trace a shortest route between the chosen tables and apply it.

        Endpoints come from the From/To pickers, with an optional single Via
        stop the route must pass through (#3). Foreign keys are walked per the
        Direction picker. When several equally-short routes exist the user picks
        one, and when none exists a dialog offers to retry ignoring direction
        (#5). Either way the prior selection is snapshotted first so it can be
        undone.
        """
        if self._schema is None:
            return
        start, end = self._selector.path_endpoints()
        if not start or not end or start == end:
            self._status.setText(
                "Pick two different tables in the From / To pickers to trace a path."
            )
            return

        direction = self._selector.path_direction()
        via = self._selector.path_via()
        stops = [start, via, end] if via else [start, end]

        routes, broken = route_paths(self._schema, stops, direction)
        if not routes:
            routes = self._retry_or_report_no_path(stops, direction, broken)
            if not routes:
                return

        if len(routes) == 1:
            chosen = routes[0]
        else:
            chosen = self._choose_route(routes)
            if chosen is None:
                return

        # Snapshot before mutating so the trace can be undone, then apply the
        # route additively or as a replacement per the user's choice.
        self._selector.snapshot_for_undo("path")
        if self._selector.path_replaces_selection():
            self._selector.set_selected_tables(chosen)
        else:
            self._selector.check_tables(chosen)
        self._render_selection()
        hops = len(chosen) - 1
        mode = "replaced with" if self._selector.path_replaces_selection() else "added"
        self._status.setText(
            f"Path ({hops} hop{'s' if hops != 1 else ''}, {mode}): "
            + " → ".join(chosen)
        )

    def _retry_or_report_no_path(self, stops, direction, broken):
        """Handle a route with no connection: offer a direction retry, else report.

        Returns the routes found on retry (possibly empty). ``broken`` is the
        ``(from, to)`` pair that couldn't be connected.
        """
        a, b = broken
        # A directed search that fails can often succeed if direction is ignored,
        # so offer that rather than leaving the user at a dead end.
        if direction != "either":
            resp = QMessageBox.question(
                self,
                "No directed path",
                f"No path from {a} to {b} following "
                f"“{_FK_DIRECTION_LABELS[direction]}”.\n\n"
                "Search again ignoring foreign-key direction?",
            )
            if resp == QMessageBox.Yes:
                routes, broken = route_paths(self._schema, stops, "either")
                if routes:
                    return routes
                a, b = broken
        QMessageBox.information(
            self,
            "No path",
            f"No foreign-key path connects {a} and {b}"
            + ("." if direction == "either" else " (even ignoring direction)."),
        )
        self._status.setText(f"No path connects {a} and {b}.")
        return []

    def _choose_route(self, routes):
        """Ask the user which of several equally-short routes to use."""
        labels = [" → ".join(route) for route in routes]
        picked, ok = QInputDialog.getItem(
            self,
            "Choose a path",
            f"{len(routes)} equally short paths were found.\nChoose one:",
            labels,
            0,
            False,
        )
        if not ok or not picked:
            return None
        return routes[labels.index(picked)]

    def _render_selection(self):
        """Render the currently-selected tables (Mermaid source + diagram)."""
        try:
            self._render_selection_now()
        finally:
            if self._schema is not None:
                self._sync_export_enabled()

    def _render_selection_now(self):
        if self._schema is None:
            self._drawio_schema = None
            return

        self._drawio_schema = None
        names = self._selector.selected_tables()
        if len(names) > RENDER_WARN_LIMIT:
            # Ask once per selection: a re-render of the same big set (e.g. after
            # toggling a render option) shouldn't nag again.
            sig = frozenset(n.lower() for n in names)
            if sig != self._render_confirmed_sig:
                answer = QMessageBox.question(
                    self,
                    "Render a large diagram?",
                    f"You selected {len(names)} tables. A diagram that big can be "
                    "slow to render and hard to read. Render it anyway?",
                )
                if answer != QMessageBox.Yes:
                    return
                self._render_confirmed_sig = sig
        elif self._render_confirmed_sig is not None:
            # Back under the limit — clear so growing past it re-asks.
            self._render_confirmed_sig = None

        # Committed: whatever is drawn next matches the current selection.
        self._auto_render_timer.stop()
        self._stale = False

        # Related tables are ticked explicitly via "Add related tables", so the
        # checked list is the whole selection — no implicit expansion here.
        filtered = filter_schema(self._schema, names)

        # Refresh the column picker for the tables now in play, then apply the
        # user's column choices on top of the table filter.
        self._columns.set_tables(filtered.tables)
        final = self._columns.sorted_schema(
            filter_columns(filtered, self._columns.excluded_pairs())
        )
        self._drawio_schema = final
        self._set_sql_schema(final)
        self._refresh_inspector()
        self._map_view.set_ticked(self._selector.selected_tables())
        self._selector.set_drawn(
            [t.name for t in self._drawio_schema.tables] if self._drawio_schema else []
        )

        mermaid_text = generate_mermaid(final, self._options_bar.diagram_options())
        self._entity_to_table = mermaid_entity_ids(final, self._options_bar.diagram_options())
        self._mermaid_text = mermaid_text
        self._mermaid_view.setPlainText(mermaid_text)

        total = len(self._schema.tables)
        shown = len(final.tables)
        if shown == 0:
            self._drawio_schema = None
            self._diagram_rendered = False
            self._render_status.clear()
            self._diagram_view.show_message(
                (
                    "No tables selected.\n\n"
                    "Tick tables on the left; the diagram updates as you go."
                    if total <= RENDER_WARN_LIMIT
                    else f"{total:,} tables is too many to draw at once.\n\n"
                    "Filter the list on the left and tick a starting table, then use "
                    "“Add related tables” to grow the diagram around it, or open "
                    "“Trace path between tables” to connect two tables. Saved "
                    "selections load from the Table list menu."
                )
            )
            self._status.setText(
                f"Loaded {self._loaded_name} — {_plural(total, 'table')}. "
                "Select tables on the left to render a diagram."
            )
            return

        col_count = sum(len(t.columns) for t in final.tables)
        rel_count = len(final.relationships)
        tables_phrase = (
            _plural(total, "table")
            if shown == total
            else f"{shown:,} of {total:,} tables"
        )

        # Too big for Mermaid to render inline — show guidance instead of letting
        # it fail with "Maximum text size in diagram exceeded". The Mermaid source
        # tab and the text/markdown exports still hold the full selection.
        if len(mermaid_text) > MAX_RENDER_CHARS:
            self._diagram_rendered = False
            self._render_status.clear()
            self._diagram_view.show_message(
                f"This selection is too large to render as a diagram "
                f"({_plural(shown, 'table')}, {_plural(col_count, 'column')} — about "
                f"{len(mermaid_text) // 1000:,} KB of Mermaid).\n\n"
                "Narrow it down with the filter and pick fewer tables, then click "
                "“Render selected”. The full selection is still available in the "
                "“Mermaid source” tab and via Save .mmd / .md."
            )
            self._status.setText(
                f"Loaded {self._loaded_name} — {tables_phrase} selected, "
                f"{_plural(col_count, 'column')}: too large to render "
                "(select fewer tables)."
            )
            return

        self._diagram_rendered = True
        if not self._diagram_view.available:
            self._diagram_rendered = False
            self._render_status.clear()
            self._status.setText(
                f"Loaded {self._loaded_name} — showing {tables_phrase}, "
                f"{_plural(col_count, 'column')}, {_plural(rel_count, 'relationship')}. "
                "Rendered exports require PySide6 WebEngine."
            )
            return
        self._diagram_view.set_diagram(mermaid_text, self._options_bar.render_style())
        # A brighter, scannable success summary: dim the "Loaded <file> —" lead
        # (the name is already in the file chip) and give the counts full contrast.
        stats = (
            f"{tables_phrase} · {_plural(col_count, 'column')} · "
            f"{_plural(rel_count, 'relationship')}"
        )
        # Dynamics tables run to hundreds of columns; at that width the
        # relationships are lost in the attribute lists.
        if col_count / max(shown, 1) > 50 and not self._options_bar.diagram_options().keys_only:
            stats += (
                f" · about {col_count // max(shown, 1)} columns per table: "
                "try Keys only (Alt+K) to see the relationships"
            )
        self._status.setText(
            f'<span style="color:{_muted_hex(self)}">Loaded '
            f'{html.escape(self._loaded_name)} —</span> '
            f'<span style="color:{_text_hex(self)}">{stats}</span>'
        )

    def _populate_table(self, rows: list[dict]):
        # Build a plain 2-D grid of strings (loose header matching so extra or
        # renamed columns still line up) and hand it to the model in one reset —
        # no per-cell widgets.
        keyed_headers = [self._key(h) for h in EXPECTED_HEADERS]
        data: list[list[str]] = []
        for row in rows:
            row_by_key = {self._key(k): v for k, v in row.items()}
            data.append(
                [
                    "" if row_by_key.get(k) is None else str(row_by_key.get(k))
                    for k in keyed_headers
                ]
            )
        self._table_model.set_rows(data)
        self._table.resizeColumnsToContents()
        # Keep any single long free-text cell (Description, DefaultValue, …) from
        # blowing a column out to the point it shoves the rest off-screen; the
        # value is still readable via elision + tooltip, or by widening the column.
        _MAX_COL_WIDTH = 320
        header = self._table.horizontalHeader()
        for c in range(self._table_model.columnCount()):
            if c == header.count() - 1:
                continue  # last column stretches; don't fight it
            if self._table.columnWidth(c) > _MAX_COL_WIDTH:
                self._table.setColumnWidth(c, _MAX_COL_WIDTH)

    @staticmethod
    def _key(text: str) -> str:
        return "".join(ch for ch in str(text).lower() if ch.isalnum())

    # -- actions -----------------------------------------------------------
    def _set_sql_schema(self, schema: Schema):
        """New tables for the SQL tab: refill Start from, keeping the choice."""
        self._sql_schema = schema
        current = self._sql_root.currentText()
        names = [t.name for t in schema.tables]
        self._sql_root.blockSignals(True)
        self._sql_root.clear()
        self._sql_root.addItems(names)
        if current in names:
            self._sql_root.setCurrentText(current)
        self._sql_root.blockSignals(False)
        self._refresh_sql()

    def _refresh_sql(self):
        if self._sql_schema is None or not self._sql_schema.tables:
            self._sql_view.setPlainText("")
            return
        self._sql_view.setPlainText(
            generate_select(
                self._sql_schema,
                root=self._sql_root.currentText() or None,
                join=self._sql_join.currentData(),
                top=self._sql_top.value(),
            )
        )

    def copy_sql(self):
        if not self._ensure_current():
            return
        text = self._sql_view.toPlainText()
        if not text:
            self._status.setText("Select some tables first; there's no query yet.")
            return
        QGuiApplication.clipboard().setText(text)
        self._status.setText("SQL query copied to clipboard.")

    def copy_mermaid(self):
        if not self._ensure_current():
            return
        QGuiApplication.clipboard().setText(self._mermaid_text)
        self._status.setText("Mermaid diagram copied to clipboard.")

    def save_mmd(self):
        if not self._ensure_current():
            return
        path = self._save_path("Save Mermaid file", ".mmd", "Mermaid (*.mmd);;All files (*)")
        if path:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(self._mermaid_text)
            self._report_saved(path)

    def save_md(self):
        if not self._ensure_current():
            return
        path = self._save_path("Save Markdown file", ".md", "Markdown (*.md);;All files (*)")
        if path:
            content = f"# Database ER Diagram\n\n```mermaid\n{self._mermaid_text.strip()}\n```\n"
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(content)
            self._report_saved(path)

    def save_table_selection_toml(self):
        if self._schema is None:
            QMessageBox.information(
                self,
                "No schema loaded",
                "Load a schema file first, then save selected tables to TOML.",
            )
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save selected tables",
            "table-selection.toml",
            "TOML files (*.toml);;All files (*)",
        )
        if not path:
            return
        text = dump_selection_toml(
            self._loaded_name,
            self._selector.selected_tables(),
        )
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        self._status.setText(f"Saved selected tables to {path}")

    def load_table_selection_toml(self):
        if self._schema is None:
            QMessageBox.information(
                self,
                "No schema loaded",
                "Load a schema file first, then load a table-selection TOML file.",
            )
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Load selected tables",
            "",
            "TOML files (*.toml);;All files (*)",
        )
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            source_name, selected = load_selection_toml(text)
        except OSError:
            preset_name = os.path.basename(path) or "selected preset"
            QMessageBox.critical(
                self,
                "Could not load table list",
                f"Could not read '{preset_name}'.",
            )
            return
        except ValueError as exc:
            message = str(exc).strip() or "Invalid table-selection TOML file."
            preset_name = os.path.basename(path) or "selected preset"
            QMessageBox.critical(
                self,
                "Could not load table list",
                f"Could not load '{preset_name}': {message}",
            )
            return

        current_name = self._loaded_name or ""
        source_base = os.path.basename(source_name.strip())
        current_base = os.path.basename(current_name.strip())
        if source_base and current_base and source_base.lower() != current_base.lower():
            answer = QMessageBox.question(
                self,
                "Different source file",
                f"This preset was saved for '{source_base}', but you loaded "
                f"'{current_base}'. Apply anyway?",
            )
            if answer != QMessageBox.Yes:
                return

        available = {table.name.lower() for table in self._schema.tables}
        matched = [name for name in selected if name.lower() in available]
        if not matched:
            self._status.setText(
                f"Loaded table list from {path} — no matching tables in the current schema."
            )
            return

        applied, missing = self._selector.set_selected_tables(matched)
        self._render_selection()
        if missing:
            self._status.setText(
                f"Loaded table list from {path} — applied {applied}, missing {len(missing)}."
            )
        else:
            self._status.setText(f"Loaded table list from {path}")

    def preview_browser(self):
        if not self._ensure_current():
            return
        # Ship the bundled mermaid.js beside the page so the preview works
        # offline, like the in-app render; fall back to the CDN only if the
        # vendored copy is somehow missing.
        folder = Path(tempfile.mkdtemp(prefix="xsltomermaid_preview_"))
        script_src = MERMAID_CDN
        try:
            shutil.copy(VENDOR_MERMAID, folder / "mermaid.min.js")
            script_src = "mermaid.min.js"
        except OSError:
            pass
        page = folder / "diagram.html"
        page.write_text(
            wrap_mermaid_html(self._mermaid_text, script_src), encoding="utf-8"
        )
        webbrowser.open(page.as_uri())
        self._status.setText("Opened the diagram preview in your browser.")

    # -- updates -----------------------------------------------------------
    def check_for_updates(self):
        """Look up the latest GitHub release in the background, then report."""
        if self._update_worker is not None and self._update_worker.isRunning():
            return
        self._update_action.setEnabled(False)
        self._status.setText("Checking GitHub for a newer version…")
        worker = UpdateCheckWorker(self)
        worker.found.connect(self._on_update_found)
        worker.failed.connect(self._on_update_failed)
        worker.finished.connect(self._on_update_worker_finished)
        self._update_worker = worker
        worker.start()

    def _on_update_found(self, release):
        if not is_newer(release.tag, __version__):
            self._status.setText(f"You're up to date (version {__version__}).")
            QMessageBox.information(
                self,
                "No updates available",
                f"You're running the latest version, {__version__}.",
            )
            return

        self._status.setText(f"Version {release.tag} is available.")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Information)
        box.setWindowTitle("Update available")
        box.setTextFormat(Qt.PlainText)  # the tag comes from the network
        box.setText(
            f"A newer version, {release.tag}, is available.\n"
            f"You're running {__version__}."
        )
        box.setInformativeText("Open the release page to download it?")
        if release.notes:
            box.setDetailedText(release.notes)
        open_btn = box.addButton("Open download page", QMessageBox.AcceptRole)
        box.addButton("Later", QMessageBox.RejectRole)
        box.setDefaultButton(open_btn)
        box.exec()
        if box.clickedButton() is open_btn:
            webbrowser.open(release.url)

    def _on_update_failed(self, message: str):
        self._status.setText("Couldn't check for updates.")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Couldn't check for updates")
        box.setText(message)
        box.setInformativeText("You can check the releases page yourself instead.")
        open_btn = box.addButton("Open releases page", QMessageBox.AcceptRole)
        box.addButton(QMessageBox.Close)
        box.exec()
        if box.clickedButton() is open_btn:
            webbrowser.open(RELEASES_PAGE)

    def _on_update_worker_finished(self):
        self._update_action.setEnabled(True)
        self._update_worker = None

    def show_about(self):
        QMessageBox.about(
            self,
            "About xsltomermaid",
            f"<b>xsltomermaid</b> {__version__}<br>"
            "Turns a database-schema spreadsheet into a Mermaid ER diagram.<br><br>"
            f'<a href="{RELEASES_PAGE}">Releases on GitHub</a> · MIT licensed',
        )

    def _nothing_to_export(self) -> bool:
        """True (and warns) if there's no rendered diagram to export."""
        if not self._diagram_rendered:
            QMessageBox.information(
                self,
                "Nothing to export",
                "There's no rendered diagram to export yet. It's empty or too "
                "large to render. Pick some tables (and, for very large schemas, "
                "fewer tables/columns) so the diagram renders, then try again.",
            )
            return True
        return False

    def _schema_for_export(self):
        if self._drawio_schema is not None and self._drawio_schema.tables:
            return self._drawio_schema
        QMessageBox.information(
            self,
            "Nothing to export",
            "There are no selected tables to export yet.",
        )
        return None

    def _confirm_large_export(self, export_kind: str, schema) -> bool:
        """Advise (but never block) before exporting a big interchange file.

        Large .drawio / .excalidraw files open slowly in their editors; warn the
        user so it isn't a surprise, but always let them proceed — no cap.
        """
        n_tables = len(schema.tables)
        if n_tables <= EXPORT_WARN_TABLES:
            return True
        n_cols = sum(len(t.columns) for t in schema.tables)
        app_name = "Excalidraw" if export_kind == "excalidraw" else "Draw.io"
        answer = QMessageBox.question(
            self,
            "Export a large diagram?",
            f"This exports {_plural(n_tables, 'table')} "
            f"({_plural(n_cols, 'column')}). A {app_name} file that big can be "
            "slow to open and edit. Export anyway?",
        )
        return answer == QMessageBox.Yes

    @staticmethod
    def _remembered_export_kind() -> str:
        kind = str(app_settings().value("export/format", "png"))
        return kind if kind in {k for k, *_ in EXPORT_FORMATS} else "png"

    def export_formats(self) -> list[str]:
        return [key for key, *_ in EXPORT_FORMATS]

    def set_export_format(self, kind: str):
        """Make ``kind`` the default export (remembered for next launch)."""
        if kind not in self.export_formats():
            return
        self._export_kind = kind
        app_settings().setValue("export/format", kind)
        self._sync_export_label()

    def export_background(self) -> str:
        action = self._export_bg_group.checkedAction()
        return str(action.data()) if action is not None else "white"

    def png_scale(self) -> float:
        action = self._png_scale_group.checkedAction()
        return float(action.data()) if action is not None else 2.0

    def _export_style(self) -> RenderStyle:
        """How the exported diagram is drawn: the view's style, re-lit for paper.

        White/Transparent swap a dark theme for the default one (dark-theme
        text is light and vanishes on a white page); Match keeps the view.
        """
        view = self._options_bar.render_style()
        choice = self.export_background()
        if choice == "match":
            return view
        return dataclasses.replace(
            view,
            background="#ffffff" if choice == "white" else "transparent",
            theme="default" if view.theme == "dark" else view.theme,
        )

    def _save_path(self, title: str, ext: str, file_filter: str) -> str:
        """Ask where to save, defaulting to "<spreadsheet name>.<ext>" in the
        folder used last time; remembers the folder chosen."""
        stem = os.path.splitext(self._loaded_name)[0] or "diagram"
        folder = str(app_settings().value("export/dir", "") or "")
        if not os.path.isdir(folder):
            folder = ""
        default = os.path.join(folder, f"{stem}{ext}") if folder else f"{stem}{ext}"
        path, _ = QFileDialog.getSaveFileName(self, title, default, file_filter)
        if path:
            app_settings().setValue("export/dir", os.path.dirname(path))
        return path

    def _report_saved(self, path: str, note: str = ""):
        """Say where the file went, with a link that opens its folder."""
        self._last_saved = path
        self._status.setText(
            f"Saved {html.escape(os.path.basename(path))}{html.escape(note)} · "
            f'<a href="show-in-folder" style="color:{_link_hex(self)}">Show in folder</a>'
        )

    def _on_status_link(self, href: str):
        if href == "show-in-folder" and getattr(self, "_last_saved", ""):
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(self._last_saved)))

    def _sync_export_enabled(self):
        """Export/Preview/Mermaid only when there's a selection to output."""
        has_tables = bool(self._drawio_schema is not None and self._drawio_schema.tables)
        for widget in (self._export_btn, self._preview_btn, self._mermaid_btn):
            widget.setEnabled(has_tables)
        self._export_btn.setToolTip(
            "Export the diagram. Use the arrow to pick another format, the "
            "background, or the PNG scale."
            if has_tables
            else "Tick some tables first; there's nothing to export yet."
        )

    def _export_as(self, kind: str):
        self.set_export_format(kind)
        self.export_diagram()

    def _sync_export_label(self):
        """Name the export button after the format it will produce."""
        ext = {k: f".{k}" for k in self.export_formats()}.get(self._export_kind)
        self._export_btn.setText(f"&Export {ext}…" if ext else "&Export…")
        for key, action in self._export_actions.items():
            action.setEnabled(True)
            font = action.font()
            font.setBold(key == self._export_kind)
            action.setFont(font)

    def export_diagram(self):
        if self._rendering or not self._ensure_current():
            return
        export_kind = self._export_kind
        specs = {key: rest for key, _label, *rest in EXPORT_FORMATS}
        if export_kind not in specs:
            return

        schema = None
        if export_kind in {"drawio", "excalidraw"}:
            schema = self._schema_for_export()
            if schema is None or not self._confirm_large_export(export_kind, schema):
                return
        elif self._nothing_to_export():
            return

        title, default_name, file_filter = specs[export_kind]
        path = self._save_path(title, os.path.splitext(default_name)[1], file_filter)
        if not path:
            return

        style = self._export_style()
        try:
            if export_kind in {"drawio", "excalidraw"}:
                build = schema_to_drawio if export_kind == "drawio" else schema_to_excalidraw
                content = build(schema, dark=style.theme == "dark")
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(content)
                self._report_saved(path)
                return
            note = self._export_rendered(export_kind, path, style)
            self._report_saved(path, note)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Could not save diagram", str(exc))

    def _export_rendered(self, kind: str, path: str, style: RenderStyle) -> str:
        """Save an SVG/PDF/PNG drawn in ``style``; returns a note for the status.

        If the export style differs from the view (e.g. a white export of a
        dark diagram), the diagram is redrawn for the capture and the view is
        put back afterwards.
        """
        view_style = self._options_bar.render_style()
        relit = style != view_style
        self._begin_render(f"Rendering diagram to {kind.upper()}…")
        try:
            if relit:
                self._diagram_view.set_diagram(self._mermaid_text, style)
            if kind == "svg":
                self._diagram_view.save_svg(path)
                return ""
            if kind == "pdf":
                self._diagram_view.save_pdf(path, background=style.background)
                return ""
            scale = self.png_scale()
            used = self._diagram_view.save_png(path, scale=scale, background=style.background)
            if used < scale - 1e-6:
                return f" (scaled to {used:.2f}× to stay within size limits)"
            return ""
        finally:
            if relit:
                self._diagram_view.set_diagram(self._mermaid_text, view_style)
            self._end_render()

    # -- testing helpers ---------------------------------------------------
    def capture(self, path: str) -> str:
        """Render the current window to a PNG and return the saved path.

        Works headless (with ``QT_QPA_PLATFORM=offscreen``) so it can be used in
        automated tests / CI to verify the UI actually paints.
        """
        app = QApplication.instance()
        if app is not None:
            # Let layout, resizing and painting settle before grabbing.
            app.processEvents()
        pixmap = self.grab()
        if not pixmap.save(path, "PNG"):
            raise RuntimeError(f"Failed to save screenshot to {path}")
        return path

    def capture_diagram(self, path: str) -> str:
        """Render the Mermaid ER diagram itself and save it (.png or .svg).

        Unlike :meth:`capture` (which grabs the Qt window), this saves the actual
        rendered ER diagram. Works headless via the vendored mermaid.js + QtSvg.
        """
        if not self._diagram_view.available:
            raise RuntimeError(
                "Rendering the diagram needs PySide6's WebEngine module "
                "(install PySide6-Addons)."
            )
        if path.lower().endswith(".svg"):
            self._diagram_view.save_svg(path)
        else:
            self._diagram_view.save_png(path)
        return path


def main(argv: list[str] | None = None):
    import argparse

    parser = argparse.ArgumentParser(
        description="Excel database-schema → Mermaid ER diagram (Qt app)."
    )
    parser.add_argument(
        "file",
        nargs="?",
        help="Optional Excel file to load on startup (.xlsx/.xlsm).",
    )
    parser.add_argument(
        "--screenshot",
        metavar="PNG",
        help="Headless self-test: load the file, save a PNG of the whole window, "
        "and exit without showing the GUI. Use with QT_QPA_PLATFORM=offscreen in CI.",
    )
    parser.add_argument(
        "--screenshot-diagram",
        metavar="FILE",
        help="Headless: load the file, render the Mermaid ER diagram, and save it as "
        ".png or .svg (by extension), then exit. Works offscreen.",
    )
    normalized_argv = _normalize_cli_args(argv)
    args = parser.parse_args(normalized_argv)

    _configure_headless_env(normalized_argv)

    app = QApplication(sys.argv[:1])
    app.setWindowIcon(app_icon())
    # Follow the OS light/dark scheme, and keep following it if the user flips
    # the system theme while the app is open.
    app.setStyle("Fusion")
    apply_system_palette(app)
    hints = app.styleHints()
    if hasattr(hints, "colorSchemeChanged"):
        hints.colorSchemeChanged.connect(lambda _scheme: apply_system_palette(app))

    window = MainWindow()
    if args.file:
        window.load_file(args.file)

    if args.screenshot:
        window.resize(1100, 760)
        saved = window.capture(args.screenshot)
        print(f"Saved window screenshot to {saved}")
        return 0

    if args.screenshot_diagram:
        if not args.file:
            parser.error("--screenshot-diagram requires a file argument to render.")
        saved = window.capture_diagram(args.screenshot_diagram)
        print(f"Saved diagram to {saved}")
        return 0

    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
