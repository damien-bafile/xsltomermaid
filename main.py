"""Qt (PySide6) desktop app: drag an Excel schema file in, get a Mermaid ER diagram.

Run with:  python main.py
"""

from __future__ import annotations

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

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from diagram_view import DiagramView
from excel_to_mermaid import (
    EXPECTED_HEADERS,
    Schema,
    Table,
    build_schema,
    filter_columns,
    filter_schema,
    generate_mermaid,
    read_rows,
    wrap_mermaid_html,
)

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
# in the current selection we skip it and ask the user to narrow the tables.
COLUMN_TREE_LIMIT = 4000

_ACCEPTED_SUFFIXES = (".xlsx", ".xlsm", ".xltx", ".xltm")


class DropArea(QLabel):
    """A large label that accepts a dragged spreadsheet file."""

    def __init__(self, on_file, parent=None):
        super().__init__(parent)
        self._on_file = on_file
        self.setAcceptDrops(True)
        self.setAlignment(Qt.AlignCenter)
        self.setWordWrap(True)
        self.setText(
            "\n\n⬇  Drag an Excel schema file here\n\n"
            "(.xlsx / .xlsm)  —  or click to browse\n\n"
        )
        self.setObjectName("dropArea")
        self.setMinimumHeight(120)
        self.setStyleSheet(
            "#dropArea {"
            "  border: 2px dashed #8a8f98;"
            "  border-radius: 12px;"
            "  color: #6b7078;"
            "  font-size: 15px;"
            "}"
        )

    def mousePressEvent(self, event):  # noqa: N802 (Qt naming)
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
                "  border: 2px solid #2f81f7;"
                "  border-radius: 12px;"
                "  color: #2f81f7;"
                "  font-size: 15px;"
                "  background: rgba(47,129,247,0.08);"
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
        self.setStyleSheet(
            "#dropArea {"
            "  border: 2px dashed #8a8f98;"
            "  border-radius: 12px;"
            "  color: #6b7078;"
            "  font-size: 15px;"
            "}"
        )


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
        self._count.setStyleSheet("color: #6b7078;")
        layout.addWidget(self._count)

        button_row = QHBoxLayout()
        select_shown = QPushButton("Select shown")
        clear_btn = QPushButton("Clear")
        select_shown.setToolTip("Tick every table currently visible in the list.")
        select_shown.clicked.connect(self.check_shown)
        clear_btn.clicked.connect(self.clear_selection)
        button_row.addWidget(select_shown)
        button_row.addWidget(clear_btn)
        layout.addLayout(button_row)

        self._related = QCheckBox("Include related tables")
        self._related.setToolTip(
            "Also draw tables connected by a foreign key to the ones you picked."
        )
        layout.addWidget(self._related)

        render_btn = QPushButton("Render selected")
        render_btn.clicked.connect(lambda: self.applied.emit())
        layout.addWidget(render_btn)

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

    def include_related(self) -> bool:
        return self._related.isChecked()

    def _update_count(self):
        total = self._list.count()
        selected = sum(1 for item in self._items() if item.checkState() == Qt.Checked)
        self._count.setText(f"{selected} of {total} selected")


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
        self._updating = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self._hint = QLabel(
            "Load a file and render some tables, then choose which columns to "
            "include here."
        )
        self._hint.setWordWrap(True)
        self._hint.setStyleSheet("color: #6b7078;")
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
        self._count.setStyleSheet("color: #6b7078;")
        layout.addWidget(self._count)

        button_row = QHBoxLayout()
        all_btn = QPushButton("All")
        none_btn = QPushButton("None")
        keys_btn = QPushButton("Keys only")
        keys_btn.setToolTip("Include only primary-key and foreign-key columns.")
        all_btn.clicked.connect(lambda: self._bulk("all"))
        none_btn.clicked.connect(lambda: self._bulk("none"))
        keys_btn.clicked.connect(lambda: self._bulk("keys"))
        button_row.addWidget(all_btn)
        button_row.addWidget(none_btn)
        button_row.addWidget(keys_btn)
        layout.addLayout(button_row)

        apply_btn = QPushButton("Apply to diagram")
        apply_btn.clicked.connect(lambda: self.applied.emit())
        layout.addWidget(apply_btn)

    # -- population --------------------------------------------------------
    def set_tables(self, tables: list[Table]):
        """Rebuild the tree for ``tables`` unless it already shows exactly them.

        The excluded set persists, so previously-unticked columns stay unticked
        and columns new to the selection default to included.
        """
        signature = tuple(f"{t.schema}.{t.name}" for t in tables)
        total_columns = sum(len(t.columns) for t in tables)

        if total_columns > COLUMN_TREE_LIMIT:
            self._tree.clear()
            self._tree.setVisible(False)
            self._filter.setVisible(False)
            self._hint.setText(
                f"This selection has {total_columns:,} columns — too many to list. "
                "Narrow the tables on the left, then come back to pick columns."
            )
            self._hint.setVisible(True)
            self._signature = None
            self._update_count()
            return

        # Same tables as last time → keep the tree (and the user's live edits).
        if signature == self._signature and self._tree.isVisible():
            return

        self._signature = signature
        self._filter.setVisible(True)
        self._tree.setVisible(True)
        self._hint.setVisible(not tables)
        if not tables:
            self._hint.setText(
                "Load a file and render some tables, then choose which columns to "
                "include here."
            )

        self._updating = True
        self._tree.clear()
        for table in tables:
            key = table.name.lower()
            parent = QTreeWidgetItem(self._tree, [table.name])
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
        self._updating = True
        for parent in self._top_items():
            for child in self._children(parent):
                table_key, col_key, is_key = child.data(0, self._ROLE_KEYS)
                if mode == "all":
                    keep = True
                elif mode == "none":
                    keep = False
                else:  # keys
                    keep = is_key
                child.setCheckState(0, Qt.Checked if keep else Qt.Unchecked)
                if keep:
                    self._excluded.discard((table_key, col_key))
                else:
                    self._excluded.add((table_key, col_key))
        self._updating = False
        self._update_count()

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
        total = 0
        included = 0
        for parent in self._top_items():
            for child in self._children(parent):
                total += 1
                if child.checkState(0) == Qt.Checked:
                    included += 1
        self._count.setText(f"{included} of {total} columns included")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Excel Schema → Mermaid ER Diagram")
        self.resize(1100, 760)

        self._schema: Schema | None = None
        self._mermaid_text: str = ""
        self._worker: LoadWorker | None = None
        self._pending_path: str = ""
        self._loaded_name: str = ""

        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(12)

        self._drop = DropArea(self.load_file_async)
        outer.addWidget(self._drop)

        self._status = QLabel("No file loaded.")
        self._status.setStyleSheet("color: #6b7078;")
        outer.addWidget(self._status)

        # Progress bar for loading a file; hidden until a load is in flight.
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setTextVisible(True)
        self._progress.setVisible(False)
        outer.addWidget(self._progress)

        # Tabs: extracted data table + generated Mermaid text.
        tabs = QTabWidget()

        self._table = QTableWidget(0, len(EXPECTED_HEADERS))
        self._table.setHorizontalHeaderLabels(EXPECTED_HEADERS)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        tabs.addTab(self._table, "Extracted data")

        self._mermaid_view = QPlainTextEdit()
        self._mermaid_view.setReadOnly(True)
        self._mermaid_view.setFont(QFont("Menlo, Consolas, monospace"))
        self._mermaid_view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self._mermaid_view.setPlaceholderText(
            "The generated Mermaid erDiagram will appear here."
        )
        tabs.addTab(self._mermaid_view, "Mermaid source")

        self._columns = ColumnSelector()
        self._columns.applied.connect(self._render_selection)
        tabs.addTab(self._columns, "Columns")

        self._diagram_view = DiagramView()
        tabs.addTab(self._diagram_view, "Rendered diagram")

        self._tabs = tabs

        # Left: table picker to limit what gets rendered. Right: the tabs.
        self._selector = TableSelector()
        self._selector.applied.connect(self._render_selection)

        body = QSplitter(Qt.Horizontal)
        body.addWidget(self._selector)
        body.addWidget(tabs)
        body.setStretchFactor(0, 0)
        body.setStretchFactor(1, 1)
        body.setSizes([280, 820])
        outer.addWidget(body, 1)

        # Action buttons.
        buttons = QHBoxLayout()
        self._copy_btn = QPushButton("Copy Mermaid")
        self._save_mmd_btn = QPushButton("Save .mmd")
        self._save_md_btn = QPushButton("Save .md")
        self._save_png_btn = QPushButton("Save diagram PNG")
        self._save_svg_btn = QPushButton("Save diagram SVG")
        self._preview_btn = QPushButton("Preview in browser")
        self._action_buttons = [
            self._copy_btn,
            self._save_mmd_btn,
            self._save_md_btn,
            self._save_png_btn,
            self._save_svg_btn,
            self._preview_btn,
        ]
        for btn in self._action_buttons:
            btn.setEnabled(False)
            buttons.addWidget(btn)
        buttons.addStretch(1)
        outer.addLayout(buttons)

        # Diagram export needs WebEngine; hide those buttons if it's unavailable.
        if not self._diagram_view.available:
            self._save_png_btn.setVisible(False)
            self._save_svg_btn.setVisible(False)

        self._copy_btn.clicked.connect(self.copy_mermaid)
        self._save_mmd_btn.clicked.connect(self.save_mmd)
        self._save_md_btn.clicked.connect(self.save_md)
        self._save_png_btn.clicked.connect(self.save_diagram_png)
        self._save_svg_btn.clicked.connect(self.save_diagram_svg)
        self._preview_btn.clicked.connect(self.preview_browser)

        self.setCentralWidget(central)

    # -- loading -----------------------------------------------------------
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
            self._progress.setValue(0)
            self._status.setText(f"Loading {name}…")

    def _apply_loaded(self, path: str, rows: list[dict], schema: Schema, mermaid_text: str):
        """Push a loaded schema into the widgets (must run on the UI thread).

        The passed-in ``mermaid_text`` is for the full schema; the diagram we
        actually show is driven by the table selection (all tables for a small
        schema, or a user-picked subset for a large one).
        """
        self._schema = schema
        self._loaded_name = os.path.basename(path)
        self._populate_table(rows)

        names = [t.name for t in schema.tables]
        self._selector.set_tables(names)
        # Small schemas render in full; large ones wait for the user to pick.
        if len(names) <= AUTO_RENDER_LIMIT:
            self._selector.check_all()
        self._render_selection()

        for btn in self._action_buttons:
            btn.setEnabled(True)

    def _render_selection(self):
        """Render the currently-selected tables (Mermaid source + diagram)."""
        if self._schema is None:
            return

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

        filtered = filter_schema(
            self._schema, names, include_related=self._selector.include_related()
        )

        # Refresh the column picker for the tables now in play, then apply the
        # user's column choices on top of the table filter.
        self._columns.set_tables(filtered.tables)
        final = filter_columns(filtered, self._columns.excluded_pairs())

        mermaid_text = generate_mermaid(final)
        self._mermaid_text = mermaid_text
        self._mermaid_view.setPlainText(mermaid_text)

        total = len(self._schema.tables)
        shown = len(final.tables)
        if shown == 0:
            self._diagram_view.show_message(
                "No tables selected.\n\n"
                "Tick the tables you want on the left, then click “Render selected”."
            )
            self._status.setText(
                f"Loaded {self._loaded_name} — {total} table(s). "
                "Select tables on the left to render a diagram."
            )
            return

        col_count = sum(len(t.columns) for t in final.tables)
        rel_count = len(final.relationships)
        scope = f"{shown} of {total}" if shown != total else f"{total}"

        # Too big for Mermaid to render inline — show guidance instead of letting
        # it fail with "Maximum text size in diagram exceeded". The Mermaid source
        # tab and the text/markdown exports still hold the full selection.
        if len(mermaid_text) > MAX_RENDER_CHARS:
            self._diagram_view.show_message(
                f"This selection is too large to render as a diagram "
                f"({shown} tables, {col_count} columns — about "
                f"{len(mermaid_text) // 1000:,} KB of Mermaid).\n\n"
                "Narrow it down with the filter and pick fewer tables, then click "
                "“Render selected”. The full selection is still available in the "
                "“Mermaid source” tab and via Save .mmd / .md."
            )
            self._status.setText(
                f"Loaded {self._loaded_name} — {scope} table(s) selected, "
                f"{col_count} column(s): too large to render (select fewer tables)."
            )
            return

        self._diagram_view.set_diagram(mermaid_text)
        self._status.setText(
            f"Loaded {self._loaded_name} — showing {scope} table(s), "
            f"{col_count} column(s), {rel_count} relationship(s)."
        )

    def _populate_table(self, rows: list[dict]):
        self._table.setRowCount(0)
        # Loose header matching so extra/renamed columns still line up.
        norm_map = {self._key(h): h for h in EXPECTED_HEADERS}
        self._table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            row_by_key = {self._key(k): v for k, v in row.items()}
            for c, header in enumerate(EXPECTED_HEADERS):
                value = row_by_key.get(self._key(header))
                item = QTableWidgetItem("" if value is None else str(value))
                self._table.setItem(r, c, item)
        self._table.resizeColumnsToContents()

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

    def save_diagram_png(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save diagram PNG", "diagram.png", "PNG image (*.png)"
        )
        if path:
            try:
                self._diagram_view.save_png(path)
            except Exception as exc:  # noqa: BLE001
                QMessageBox.critical(self, "Could not save diagram", str(exc))
                return
            self._status.setText(f"Saved rendered diagram to {path}")

    def save_diagram_svg(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save diagram SVG", "diagram.svg", "SVG image (*.svg)"
        )
        if path:
            try:
                self._diagram_view.save_svg(path)
            except Exception as exc:  # noqa: BLE001
                QMessageBox.critical(self, "Could not save diagram", str(exc))
                return
            self._status.setText(f"Saved rendered diagram to {path}")

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
            return self._diagram_view.save_svg(path)
        return self._diagram_view.save_png(path)


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
