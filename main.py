"""Qt (PySide6) desktop app: drag an Excel schema file in, get a Mermaid ER diagram.

Run with:  python main.py
"""

from __future__ import annotations

import html
import os
import sys
import tempfile
import webbrowser


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
    Qt,
    QThread,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QFont,
    QGuiApplication,
    QIcon,
    QKeySequence,
    QPalette,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
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
    QSplitter,
    QHeaderView,
    QTableView,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from diagram_view import (
    DiagramView,
    RenderStyle,
    resource_path,
    schema_to_drawio,
    schema_to_visio,
)
from excel_to_mermaid import (
    EXPECTED_HEADERS,
    DiagramOptions,
    Schema,
    Table,
    build_schema,
    filter_columns,
    filter_schema,
    generate_mermaid,
    read_rows,
    shortest_path,
    wrap_mermaid_html,
)
from make_sample import write_sample
from selection_preset import dump_selection_toml, load_selection_toml

# Above this many tables we don't auto-render the whole diagram (it's slow and
# Mermaid chokes); the user picks a subset instead.
AUTO_RENDER_LIMIT = 25
# Rendering more than this many tables at once prompts a confirmation first.
RENDER_WARN_LIMIT = 60
# Mermaid refuses to render past its own ``maxTextSize`` (2,000,000 chars, set in
# diagram_view). Stay under it so we can show a helpful message instead of
# Mermaid's cryptic "Maximum text size in diagram exceeded".
MAX_RENDER_CHARS = 1_800_000
# Building a checkable tree of every column gets heavy; above this many columns
# the "All tables" view isn't built at once — the user picks a single table from
# the dropdown instead (which is always fast, whatever the schema size).
COLUMN_TREE_LIMIT = 10000

_ACCEPTED_SUFFIXES = (".xlsx", ".xlsm", ".xltx", ".xltm")

# Accent family and state colours, kept together so a tweak lives in one place
# rather than scattered across widget stylesheets.
_ACCENT = "#2f81f7"  # blue accent, reads well on light and dark
_ACCENT_HOVER = "#4a92f9"  # accent, hover
_ACCENT_PRESSED = "#1f6fe0"  # accent, pressed
_ACCENT_RING = "#cfe0ff"  # light focus ring on a filled accent button
_ACCENT_WASH = "rgba(47,129,247,0.08)"  # translucent accent fill (drag-hover)
_DISABLED_BG = "rgba(128,128,128,0.18)"  # filled button, disabled
_DISABLED_FG = "rgba(128,128,128,0.75)"  # filled button text, disabled
# Semantic status colours for the render indicator. Both clear the 3:1 non-text
# (icon) contrast threshold on the light and dark surfaces the icon sits on.
_OK_GREEN = "#2e9e57"  # render succeeded
_ERR_ORANGE = "#d9822b"  # render failed


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
    app.setPalette(_dark_palette() if dark else app.style().standardPalette())
    return dark


class DropArea(QLabel):
    """A large label that accepts a dragged spreadsheet file."""

    _IDLE_TEXT = (
        "\n\n⬇  Drag an Excel schema file here\n\n"
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
        self.setText(f"📄  {name}     ·     drop or click to load another file")
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
                f"  color: {_ACCENT};"
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
        focus = f"#dropArea:focus {{ border-color: {_ACCENT}; color: {_ACCENT}; }}"
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

    def __init__(self, path: str, parent=None):
        super().__init__(parent)
        self._path = path

    def run(self):  # noqa: D401 - QThread entry point
        try:
            self.staged.emit("Reading file…")
            rows = read_rows(
                self._path,
                progress=lambda fraction: self.progressed.emit(int(fraction * 50)),
            )
            self.staged.emit("Building schema…")
            schema = build_schema(
                rows,
                progress=lambda fraction: self.progressed.emit(
                    50 + int(fraction * 40)
                ),
            )
            self.staged.emit("Generating diagram…")
            mermaid_text = generate_mermaid(schema)
            self.progressed.emit(95)
            self.loaded.emit(rows, schema, mermaid_text)
        except Exception as exc:  # noqa: BLE001 - surface any parse error to the UI
            self.failed.emit(str(exc))


class TableSelector(QWidget):
    """A filterable, checkable list of tables to include in the diagram."""

    applied = Signal()  # user asked to (re)render the current selection
    related_requested = Signal()  # user asked to also tick the related tables
    path_requested = Signal()  # user asked for the shortest path between two tables

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        title = QLabel("Tables in diagram")
        title.setStyleSheet("font-weight: 600;")
        layout.addWidget(title)

        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Filter tables…")
        self._filter.setClearButtonEnabled(True)
        self._filter.textChanged.connect(self._apply_filter_text)
        layout.addWidget(self._filter)

        self._list = QListWidget()
        self._list.setUniformItemSizes(True)
        self._list.itemChanged.connect(lambda _item: self._update_count())
        layout.addWidget(self._list, 1)

        self._count = QLabel("0 of 0 selected")
        self._count.setStyleSheet(f"color: {_muted_hex(self)};")
        layout.addWidget(self._count)

        button_row = QHBoxLayout()
        self._select_shown_btn = QPushButton("Select shown")
        self._clear_btn = QPushButton("Clear")
        self._select_shown_btn.setToolTip("Tick every table currently visible in the list.")
        self._select_shown_btn.clicked.connect(self.check_shown)
        self._clear_btn.clicked.connect(self.clear_selection)
        button_row.addWidget(self._select_shown_btn)
        button_row.addWidget(self._clear_btn)
        layout.addLayout(button_row)

        self._related_btn = QPushButton("Add related tables")
        self._related_btn.setToolTip(
            "Tick the tables directly connected by a foreign key to the ones "
            "you've checked (one hop out), then render."
        )
        self._related_btn.clicked.connect(lambda: self.related_requested.emit())
        layout.addWidget(self._related_btn)

        self._path_btn = QPushButton("Shortest path between 2")
        self._path_btn.setToolTip(
            "Check exactly two tables, then tick every table on the shortest "
            "foreign-key path connecting them and render it."
        )
        self._path_btn.clicked.connect(lambda: self.path_requested.emit())
        layout.addWidget(self._path_btn)

        self._render_btn = QPushButton("Render selected")
        self._render_btn.clicked.connect(lambda: self.applied.emit())
        layout.addWidget(self._render_btn)

        # Nothing to act on until a schema is loaded.
        self.set_ready(False)

    def set_ready(self, ready: bool):
        """Enable the selection controls only once a schema is loaded.

        With no file open there are no tables to pick, so the filter, the list
        and every action button are disabled to match the (already disabled)
        export bar rather than inviting dead clicks at "0 of 0 selected".
        """
        for widget in (
            self._filter,
            self._list,
            self._select_shown_btn,
            self._clear_btn,
            self._related_btn,
            self._path_btn,
            self._render_btn,
        ):
            widget.setEnabled(ready)

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
        self._filter.clear()
        self._update_count()

    # -- selection helpers -------------------------------------------------
    def _items(self):
        return (self._list.item(i) for i in range(self._list.count()))

    def _apply_filter_text(self, text: str):
        needle = text.strip().lower()
        for item in self._items():
            item.setHidden(needle not in item.text().lower())

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
        selected = sum(1 for item in self._items() if item.checkState() == Qt.Checked)
        self._count.setText(f"{selected} of {total} selected")

    def retheme(self):
        self._count.setStyleSheet(f"color: {_muted_hex(self)};")


class ColumnSelector(QWidget):
    """A tab with a checkable tree (table → columns) to choose diagram columns.

    Owns the set of *excluded* ``(table, column)`` pairs — columns are included
    unless unticked — so the choice survives re-rendering and changing which
    tables are shown.
    """

    applied = Signal()  # user asked to re-render with the current column choice

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
        scope_row.addWidget(QLabel("Show:"))
        self._scope = QComboBox()
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
        self._filter.setClearButtonEnabled(True)
        self._filter.textChanged.connect(self._apply_filter_text)
        layout.addWidget(self._filter)

        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
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
        layout.addLayout(button_row)

        # Same verb as the left panel: one "Render selected" commits the whole
        # table + column selection, from whichever surface you're on.
        render_btn = QPushButton("Render selected")
        render_btn.setToolTip(
            "Render the diagram with the current table and column selection."
        )
        render_btn.clicked.connect(lambda: self.applied.emit())
        layout.addWidget(render_btn)

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

    def _scope_tables(self) -> list[Table]:
        """The tables the current dropdown choice covers ('All' or just one)."""
        index = self._scope.currentData()
        if index is None or index < 0:
            return self._tables
        if 0 <= index < len(self._tables):
            return [self._tables[index]]
        return self._tables

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
            "Untick a column to leave it out, then Render selected."
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
            for column in table.columns:
                child = QTreeWidgetItem(parent, [column.name])
                child.setFlags(child.flags() | Qt.ItemIsUserCheckable)
                is_key = bool(column.is_primary_key or column.foreign_key_reference)
                child.setData(0, self._ROLE_KIND, "column")
                child.setData(0, self._ROLE_KEYS, (key, column.name.lower(), is_key))
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

    def _bulk(self, mode: str):
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

        # Row 1 — appearance / layout (all applied via Mermaid config).
        row1 = QHBoxLayout()
        row1.setSpacing(8)
        self._orientation = self._combo(self._ORIENTATIONS)
        self._spacing = self._combo(self._SPACINGS)
        self._theme = self._combo(self._THEMES)
        self._background = self._combo(self._BACKGROUNDS)
        self._font = QSpinBox()
        self._font.setRange(8, 28)
        self._font.setValue(12)
        self._font.setSuffix(" px")
        self._font.valueChanged.connect(lambda _v: self.changed.emit())
        self._fit_width = QCheckBox("Fit width")
        self._fit_width.setChecked(True)
        self._fit_width.toggled.connect(lambda _v: self.changed.emit())

        for label, widget in [
            ("Orientation", self._orientation),
            ("Spacing", self._spacing),
            ("Theme", self._theme),
            ("Background", self._background),
            ("Font", self._font),
        ]:
            row1.addWidget(QLabel(label + ":"))
            row1.addWidget(widget)
        row1.addWidget(self._fit_width)
        row1.addStretch(1)
        outer.addLayout(row1)

        # Row 2 — content toggles (applied by regenerating the Mermaid source).
        row2 = QHBoxLayout()
        row2.setSpacing(8)
        self._show_comments = QCheckBox("Descriptions/notes")
        self._show_comments.setChecked(True)
        self._show_rel_labels = QCheckBox("Relationship labels")
        self._show_rel_labels.setChecked(True)
        self._prefix_schema = QCheckBox("Prefix schema name")
        self._keys_only = QCheckBox("Keys only")
        for chk in (
            self._show_comments,
            self._show_rel_labels,
            self._prefix_schema,
            self._keys_only,
        ):
            chk.toggled.connect(lambda _v: self.changed.emit())
            row2.addWidget(chk)
        row2.addStretch(1)
        outer.addLayout(row2)

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


class RenderStatus(QWidget):
    """A small spinner while the diagram renders, then a tick when it's done."""

    _FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"  # braille spinner

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
        self._timer.setInterval(90)
        self._timer.timeout.connect(self._spin)
        self._frame = 0
        self.setVisible(False)

    def _spin(self):
        self._frame = (self._frame + 1) % len(self._FRAMES)
        self._icon.setText(self._FRAMES[self._frame])

    def start(self):
        self._icon.setStyleSheet(f"color: {_ACCENT}; font-weight: 600;")
        self._icon.setText(self._FRAMES[0])
        self._text.setText("Rendering…")
        self.setVisible(True)
        self._timer.start()

    def finish(self, ok: bool = True):
        self._timer.stop()
        if ok:
            self._icon.setStyleSheet(f"color: {_OK_GREEN}; font-weight: 700;")
            self._icon.setText("✓")
            self._text.setText("Rendered")
        else:
            self._icon.setStyleSheet(f"color: {_ERR_ORANGE}; font-weight: 700;")
            self._icon.setText("⚠")
            self._text.setText("Render failed")
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
        if not index.isValid() or role not in (Qt.DisplayRole, Qt.ToolTipRole):
            return None
        return self._rows[index.row()][index.column()] or None

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
        self.resize(1100, 760)
        # A floor so the window can't shrink small enough to clip the action bar
        # or collapse the panels; every action is also reachable from the menu.
        self.setMinimumSize(960, 600)

        self._schema: Schema | None = None
        self._mermaid_text: str = ""
        self._worker: LoadWorker | None = None
        self._pending_path: str = ""
        self._loaded_name: str = ""
        self._rendering: bool = False
        self._diagram_rendered: bool = False
        self._drawio_schema: Schema | None = None

        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(12)

        self._drop = DropArea(self.load_file_async)
        outer.addWidget(self._drop)

        self._status = QLabel("No file loaded.")
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
            "Draw.io, Visio, PDF, PNG or SVG —"
        )
        self._onboard_hint.setStyleSheet(f"color: {_muted_hex(self)};")
        self._sample_btn = QPushButton("try a sample")
        self._sample_btn.setCursor(Qt.PointingHandCursor)
        self._sample_btn.setStyleSheet(
            "QPushButton {"
            f"  color: {_ACCENT};"
            "  border: 1px solid transparent;"
            "  border-radius: 3px;"
            "  background: transparent;"
            "  padding: 0 2px;"
            "  text-decoration: underline;"
            "}"
            f"QPushButton:hover {{ color: {_ACCENT_HOVER}; }}"
            f"QPushButton:focus {{ border-color: {_ACCENT}; }}"
        )
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
        self._table = QTableView()
        self._table.setModel(self._table_model)
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
        tabs.addTab(self._table, "Extracted data")

        self._columns = ColumnSelector()
        self._columns.applied.connect(self._render_selection)
        tabs.addTab(self._columns, "Columns")

        self._mermaid_view = QPlainTextEdit()
        self._mermaid_view.setReadOnly(True)
        self._mermaid_view.setFont(QFont("Menlo, Consolas, monospace"))
        self._mermaid_view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self._mermaid_view.setPlaceholderText(
            "The generated Mermaid erDiagram will appear here."
        )
        tabs.addTab(self._mermaid_view, "Mermaid source")

        # Rendered diagram tab = an options bar above the actual diagram view.
        self._diagram_view = DiagramView()
        self._options_bar = DiagramOptionsBar()
        self._options_bar.changed.connect(self._render_selection)
        self._render_status = RenderStatus()
        self._diagram_view.render_started.connect(self._render_status.start)
        self._diagram_view.render_finished.connect(self._render_status.finish)
        self._diagram_view.render_finished.connect(self._announce_render)
        diagram_tab = QWidget()
        diagram_layout = QVBoxLayout(diagram_tab)
        diagram_layout.setContentsMargins(0, 0, 0, 0)
        diagram_layout.setSpacing(4)
        diagram_layout.addWidget(self._options_bar)
        self._divider = QFrame()
        self._divider.setFrameShape(QFrame.HLine)
        self._divider.setStyleSheet(f"color: {_line_hex(self)};")
        diagram_layout.addWidget(self._divider)
        # A right-aligned render status (spinner → tick) just above the diagram.
        status_row = QHBoxLayout()
        status_row.setContentsMargins(4, 0, 6, 0)
        status_row.addStretch(1)
        status_row.addWidget(self._render_status)
        diagram_layout.addLayout(status_row)
        diagram_layout.addWidget(self._diagram_view, 1)
        tabs.addTab(diagram_tab, "Rendered diagram")

        self._tabs = tabs
        self._diagram_tab = diagram_tab

        # Left: table picker to limit what gets rendered. Right: the tabs.
        self._selector = TableSelector()
        self._selector.applied.connect(self._render_selection)
        self._selector.related_requested.connect(self._add_related_tables)
        self._selector.path_requested.connect(self._find_shortest_path)

        body = QSplitter(Qt.Horizontal)
        body.addWidget(self._selector)
        body.addWidget(tabs)
        body.setStretchFactor(0, 0)
        body.setStretchFactor(1, 1)
        body.setSizes([280, 820])
        outer.addWidget(body, 1)

        # Action buttons. Labels follow one convention: a trailing "…" marks the
        # actions that open a file dialog; immediate actions (copy, preview) omit
        # it. Tooltips disambiguate the near-identical .mmd / .md pair.
        buttons = QHBoxLayout()
        self._copy_btn = QPushButton("Copy Mermaid")
        self._copy_btn.setToolTip("Copy the Mermaid diagram source to the clipboard.")
        self._save_mmd_btn = QPushButton("Save .mmd…")
        self._save_mmd_btn.setToolTip("Save the raw Mermaid diagram source (.mmd).")
        self._save_md_btn = QPushButton("Save .md…")
        self._save_md_btn.setToolTip(
            "Save as Markdown with the diagram in a ```mermaid code block (.md)."
        )
        self._save_selection_btn = QPushButton("Save table list…")
        self._save_selection_btn.setToolTip(
            "Save the current table selection as a .toml preset."
        )
        self._load_selection_btn = QPushButton("Load table list…")
        self._load_selection_btn.setToolTip(
            "Restore a table selection from a .toml preset."
        )
        self._export_format = QComboBox()
        self._export_format.setAccessibleName("Diagram export format")
        self._export_format.addItem("Draw.io (.drawio)", "drawio")
        self._export_format.addItem("MS Visio (.vdx)", "visio")
        self._export_format.addItem("PDF (.pdf)", "pdf")
        self._export_format.addItem("PNG (.png)", "png")
        self._export_format.addItem("SVG (.svg)", "svg")
        self._export_format.setToolTip("Choose the diagram export format.")
        # The button names the format the combo has selected, so the pair reads
        # as one pick-then-export control rather than two rival export widgets.
        self._export_btn = QPushButton("Export…")
        self._export_format.currentIndexChanged.connect(self._sync_export_label)
        self._preview_btn = QPushButton("Preview in browser")
        self._preview_btn.setToolTip("Open the rendered diagram in your web browser.")
        self._sync_export_label()
        self._action_buttons = [
            self._copy_btn,
            self._save_mmd_btn,
            self._save_md_btn,
            self._save_selection_btn,
            self._load_selection_btn,
            self._export_format,
            self._export_btn,
            self._preview_btn,
        ]
        for btn in self._action_buttons:
            btn.setEnabled(False)

        # "Copy Mermaid" is the most-reached-for action, so it leads as the one
        # filled/accent button; the rest stay quiet.
        self._copy_btn.setStyleSheet(
            "QPushButton {"
            f"  background: {_ACCENT};"
            "  color: white;"
            "  font-weight: 600;"
            # A 2px transparent border reserves space so the focus ring below
            # doesn't shift the button; padding is trimmed 2px to compensate.
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
        )

        # The row reads as three jobs, not eight equal buttons: get the Mermaid
        # text · save/load the table selection · export or preview the diagram.
        self._button_seps: list[QFrame] = []
        groups = [
            [self._copy_btn, self._save_mmd_btn, self._save_md_btn],
            [self._save_selection_btn, self._load_selection_btn],
            [self._export_format, self._export_btn, self._preview_btn],
        ]
        for i, group in enumerate(groups):
            if i > 0:
                sep = QFrame()
                sep.setFrameShape(QFrame.VLine)
                sep.setFixedHeight(24)
                self._button_seps.append(sep)
                buttons.addSpacing(4)
                buttons.addWidget(sep)
                buttons.addSpacing(4)
            for widget in group:
                buttons.addWidget(widget)

        buttons.addStretch(1)
        outer.addLayout(buttons)

        self._copy_btn.clicked.connect(self.copy_mermaid)
        self._save_mmd_btn.clicked.connect(self.save_mmd)
        self._save_md_btn.clicked.connect(self.save_md)
        self._save_selection_btn.clicked.connect(self.save_table_selection_toml)
        self._load_selection_btn.clicked.connect(self.load_table_selection_toml)
        self._export_btn.clicked.connect(self.export_diagram)
        self._preview_btn.clicked.connect(self.preview_browser)

        self._build_menu_bar()

        self.setCentralWidget(central)

        # Match the OS: default the diagram's own theme/background to dark when
        # the app starts dark, and apply palette-derived colours everywhere.
        app = QApplication.instance()
        if app is not None:
            self._options_bar.apply_system_defaults(system_is_dark(app))
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

        diagram_menu = bar.addMenu("&Diagram")
        diagram_menu.addAction(
            act("&Render selected", self._render_selection,
                QKeySequence("F5"), schema_only=True)
        )
        diagram_menu.addSeparator()
        diagram_menu.addAction(
            act("&Copy Mermaid", self.copy_mermaid,
                QKeySequence("Ctrl+Shift+C"), schema_only=True)
        )
        diagram_menu.addAction(
            act("&Export diagram…", self.export_diagram,
                QKeySequence("Ctrl+E"), schema_only=True)
        )
        diagram_menu.addAction(
            act("&Preview in browser", self.preview_browser, schema_only=True)
        )

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

    def _announce_render(self, ok: bool):
        """Tell a screen reader when a render finishes.

        The spinner→tick status is visual only; without this a screen-reader
        user gets no signal that the diagram finished (or failed). Best-effort:
        it degrades silently where the announcement API isn't available.
        """
        message = "Diagram rendered" if ok else "Diagram render failed"
        try:
            from PySide6.QtGui import QAccessible, QAccessibleAnnouncementEvent

            if QAccessible.isActive():
                QAccessible.updateAccessibility(
                    QAccessibleAnnouncementEvent(self, message)
                )
        except Exception:  # noqa: BLE001 - a11y announcement must never be fatal
            pass

    def retheme(self):
        """Re-apply palette-derived colours after a light/dark scheme change."""
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
            rows = read_rows(path)
            schema = build_schema(rows)
            mermaid_text = generate_mermaid(schema)
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

        worker = LoadWorker(path, self)
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
        QMessageBox.critical(self, "Could not read file", message)
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
        self._export_format.setEnabled(False)
        app = QApplication.instance()
        if app is not None:
            app.processEvents()  # paint the bar before we block

    def _end_render(self):
        self._progress.setVisible(False)
        self._progress.setRange(0, 100)
        self._export_btn.setEnabled(True)
        self._export_format.setEnabled(True)
        self._rendering = False

    def _apply_loaded(self, path: str, rows: list[dict], schema: Schema, mermaid_text: str):
        """Push a loaded schema into the widgets (must run on the UI thread).

        The passed-in ``mermaid_text`` is for the full schema; the diagram we
        actually show is driven by the table selection (all tables for a small
        schema, or a user-picked subset for a large one).
        """
        self._schema = schema
        self._loaded_name = os.path.basename(path)
        self._populate_table(rows)
        # The big drop target has done its job; shrink it to a file chip so the
        # tabs get the height, and wake up the (until now inert) table picker.
        self._drop.set_loaded(self._loaded_name)
        self._onboard.setVisible(False)  # onboarding is a first-run nudge only
        self._selector.set_ready(True)

        names = [t.name for t in schema.tables]
        self._selector.set_tables(names)
        # Small schemas render in full; large ones wait for the user to pick.
        if len(names) <= AUTO_RENDER_LIMIT:
            self._selector.check_all()
        self._render_selection()

        for btn in self._action_buttons:
            btn.setEnabled(True)
        for action in self._schema_actions:
            action.setEnabled(True)

        # Land on the diagram — the reason they opened the file — once one is
        # actually rendered. Only in the live window: headless captures, the CLI
        # and tests grab the window before it's shown and should keep landing on
        # the data table.
        if self.isVisible() and self._diagram_rendered:
            self._tabs.setCurrentWidget(self._diagram_tab)

    def _add_related_tables(self):
        """Tick the one-hop foreign-key neighbours of the checked tables, render."""
        if self._schema is None:
            return
        names = self._selector.selected_tables()
        if not names:
            self._status.setText("Check at least one table first, then add related.")
            return
        # filter_schema with include_related gives us the selection + its one-hop
        # neighbours; tick every table in that expanded set.
        expanded = filter_schema(self._schema, names, include_related=True)
        added = self._selector.check_tables(t.name for t in expanded.tables)
        if added:
            self._render_selection()
        else:
            self._status.setText("No related tables to add.")

    def _find_shortest_path(self):
        """Tick every table on the shortest path between the two checked tables."""
        if self._schema is None:
            return
        names = self._selector.selected_tables()
        if len(names) < 2:
            self._status.setText(
                "Check at least two tables, then find the shortest path between them."
            )
            return
        if len(names) == 2:
            start, end = names
        else:
            start, ok = QInputDialog.getItem(
                self,
                "Pick starting table",
                "More than two tables are selected.\nChoose the starting table:",
                names,
                0,
                False,
            )
            if not ok or not start:
                return
            ends = [name for name in names if name != start]
            end, ok = QInputDialog.getItem(
                self,
                "Pick destination table",
                "Choose the destination table:",
                ends,
                0,
                False,
            )
            if not ok or not end:
                return
        path = shortest_path(self._schema, start, end)
        if not path:
            self._status.setText(
                f"No foreign-key path connects {start} and {end}."
            )
            return
        self._selector.check_tables(path)
        self._render_selection()
        hops = len(path) - 1
        self._status.setText(
            f"Shortest path ({hops} hop{'s' if hops != 1 else ''}): "
            + " → ".join(path)
        )

    def _render_selection(self):
        """Render the currently-selected tables (Mermaid source + diagram)."""
        if self._schema is None:
            self._drawio_schema = None
            return

        self._drawio_schema = None
        names = self._selector.selected_tables()
        if len(names) > RENDER_WARN_LIMIT:
            answer = QMessageBox.question(
                self,
                "Render a large diagram?",
                f"You selected {len(names)} tables. A diagram that big can be slow "
                "to render and hard to read. Render it anyway?",
            )
            if answer != QMessageBox.Yes:
                return

        # Related tables are ticked explicitly via "Add related tables", so the
        # checked list is the whole selection — no implicit expansion here.
        filtered = filter_schema(self._schema, names)

        # Refresh the column picker for the tables now in play, then apply the
        # user's column choices on top of the table filter.
        self._columns.set_tables(filtered.tables)
        final = filter_columns(filtered, self._columns.excluded_pairs())
        self._drawio_schema = final

        mermaid_text = generate_mermaid(final, self._options_bar.diagram_options())
        self._mermaid_text = mermaid_text
        self._mermaid_view.setPlainText(mermaid_text)

        total = len(self._schema.tables)
        shown = len(final.tables)
        if shown == 0:
            self._drawio_schema = None
            self._diagram_rendered = False
            self._render_status.clear()
            self._diagram_view.show_message(
                "No tables selected.\n\n"
                "Tick the tables you want on the left, then click “Render selected”."
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
    def copy_mermaid(self):
        QGuiApplication.clipboard().setText(self._mermaid_text)
        self._status.setText("Mermaid diagram copied to clipboard.")

    def save_mmd(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Mermaid file", "schema.mmd", "Mermaid (*.mmd);;All files (*)"
        )
        if path:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(self._mermaid_text)
            self._status.setText(f"Saved {path}")

    def save_md(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Markdown file", "schema.md", "Markdown (*.md);;All files (*)"
        )
        if path:
            content = f"# Database ER Diagram\n\n```mermaid\n{self._mermaid_text.strip()}\n```\n"
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(content)
            self._status.setText(f"Saved {path}")

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
        html = wrap_mermaid_html(self._mermaid_text)
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".html", delete=False, encoding="utf-8"
        )
        tmp.write(html)
        tmp.close()
        webbrowser.open(f"file://{tmp.name}")
        self._status.setText(
            "Opened diagram preview in your browser (needs internet for Mermaid CDN)."
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

    def _png_export_options(self) -> tuple[float, str] | None:
        scale, ok = QInputDialog.getDouble(
            self,
            "PNG scale",
            "Scale multiplier (1.0–10.0):",
            2.0,
            1.0,
            10.0,
            1,
        )
        if not ok:
            return None
        transparent = (
            QMessageBox.question(
                self,
                "PNG background",
                "Use transparent background?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            == QMessageBox.Yes
        )
        if transparent:
            return float(scale), "transparent"
        background = self._options_bar.background_value()
        if background == "transparent":
            background = "white"
        return float(scale), background

    def _sync_export_label(self):
        """Name the export button after the format the combo has selected.

        Keeps the format picker and the trigger reading as a single
        pick-then-export control (e.g. "Export .drawio…").
        """
        exts = {
            "drawio": ".drawio",
            "visio": ".vdx",
            "pdf": ".pdf",
            "png": ".png",
            "svg": ".svg",
        }
        ext = exts.get(str(self._export_format.currentData() or ""))
        self._export_btn.setText(f"Export {ext}…" if ext else "Export…")

    def export_diagram(self):
        if self._rendering:
            return

        render_started = False
        schema = None
        export_kind = str(self._export_format.currentData() or "")
        file_specs = {
            "drawio": ("Save Draw.io diagram", "diagram.drawio", "Draw.io file (*.drawio)"),
            "visio": ("Save Visio diagram", "diagram.vdx", "Visio XML Drawing (*.vdx)"),
            "pdf": ("Save diagram PDF", "diagram.pdf", "PDF document (*.pdf)"),
            "png": ("Save diagram PNG", "diagram.png", "PNG image (*.png)"),
            "svg": ("Save diagram SVG", "diagram.svg", "SVG image (*.svg)"),
        }
        if export_kind not in file_specs:
            return

        rendered_export_kinds = {"pdf", "png", "svg"}
        if export_kind in {"drawio", "visio"}:
            schema = self._schema_for_export()
            if schema is None:
                return
        elif export_kind in rendered_export_kinds and self._nothing_to_export():
            return

        png_options: tuple[float, str] | None = None
        if export_kind == "png":
            png_options = self._png_export_options()
            if png_options is None:
                return

        title, default_name, file_filter = file_specs[export_kind]
        path, _ = QFileDialog.getSaveFileName(self, title, default_name, file_filter)
        if not path:
            return

        try:
            if export_kind == "drawio":
                content = schema_to_drawio(schema)
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(content)
                self._status.setText(f"Saved Draw.io diagram to {path}")
                return

            if export_kind == "visio":
                content = schema_to_visio(schema)
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(content)
                self._status.setText(f"Saved Visio diagram to {path}")
                return

            if export_kind == "svg":
                self._begin_render("Rendering diagram to SVG…")
                render_started = True
                self._diagram_view.save_svg(path)
                self._end_render()
                render_started = False
                self._status.setText(f"Saved rendered diagram to {path}")
                return

            if export_kind == "pdf":
                background = self._options_bar.background_value()
                if background == "transparent":
                    background = "white"
                self._begin_render("Rendering diagram to PDF…")
                render_started = True
                self._diagram_view.save_pdf(path, background=background)
                self._end_render()
                render_started = False
                self._status.setText(f"Saved rendered diagram to {path}")
                return

            scale, background = png_options
            self._begin_render("Rendering diagram to PNG…")
            render_started = True
            used = self._diagram_view.save_png(path, scale=scale, background=background)
            self._end_render()
            render_started = False
            note = ""
            if used < scale - 1e-6:
                note = f" (scaled to {used:.2f}× to keep it within size limits)"
            self._status.setText(f"Saved rendered diagram to {path}{note}")
        except Exception as exc:  # noqa: BLE001
            if render_started:
                self._end_render()
            QMessageBox.critical(self, "Could not save diagram", str(exc))

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
