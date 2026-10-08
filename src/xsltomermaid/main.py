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
    QEvent,
    QSortFilterProxyModel,
    QUrl,
    Qt,
    QTimer,
)
from PySide6.QtGui import (
    QActionGroup,
    QDesktopServices,
    QAction,
    QFont,
    QGuiApplication,
    QKeySequence,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QMenu,
    QApplication,
    QComboBox,
    QDockWidget,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QToolButton,
    QHeaderView,
    QTableView,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .diagram_view import (
    VENDOR_MERMAID,
    DiagramView,
    RenderStyle,
    schema_to_drawio,
    schema_to_excalidraw,
)
from .excel_to_mermaid import (
    mermaid_entity_ids,
    EXPECTED_HEADERS,
    Schema,
    filter_columns,
    filter_schema,
    generate_mermaid,
    linked_fk_columns,
    related_tables,
    route_paths,
    unresolved_foreign_keys,
    MERMAID_CDN,
    wrap_mermaid_html,
)
from .make_sample import write_sample
from .selection_preset import dump_selection_toml, load_selection_toml
from .map_view import SchemaMapView
from .schema_map import (
    cluster_index,
    is_hidden_link,
    link_counts,
)
from .config import (
    app_settings,
    AUTO_RENDER_DELAY_MS,
    AUTO_RENDER_LIMIT,
    EXPORT_BACKGROUNDS,
    EXPORT_FORMATS,
    EXPORT_WARN_TABLES,
    MAX_RENDER_CHARS,
    PNG_SCALES,
    RECENT_FILES_MAX,
    RELATED_WARN_COUNT,
    RENDER_WARN_LIMIT,
)
from .theme import (
    _ACCENT_FILL,
    _control_border_hex,
    _ACCENT_WASH,
    _apply_primary_button_style,
    _line_hex,
    _link_hex,
    _muted_hex,
    _plural,
    _text_hex,
    announce,
    app_icon,
    apply_system_palette,
    system_is_dark,
    _ACCENT,
    _DISABLED_FG,
)
from .widgets import (
    DropArea,
    ExtractedDataModel,
    LoadWorker,
    RenderStatus,
    StatusLabel,
    UpdateCheckWorker,
)
from .table_list import (
    _FK_DIRECTION_LABELS,
    TableSelector,
)
from .table_list import (  # noqa: F401 - used by tests via the main module
    _ROLE_DRAWN,
    _ROLE_GROUP,
    _ROLE_LINKS,
    _ROLE_MAPSEL,
)
from .inspector import (
    TableInspector,
)
from .column_selector import (
    ColumnSelector,
)
from .options_bar import (
    DiagramOptionsBar,
)
from .services import SchemaImportService, describe_load_error
from .sql_query import generate_select
from . import __version__
from .updates import (
    RELEASES_PAGE,
    is_newer,
)


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
        self._sample_btn.setToolTip("Load a small built-in schema to try the app.")
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
        self._table.setAccessibleName("Extracted data")
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
        self._sql_join.setMinimumWidth(
            self._sql_join.fontMetrics().horizontalAdvance("INNER JOIN") + 44
        )
        self._sql_top.setMinimumWidth(self._sql_top.fontMetrics().horizontalAdvance("1000000") + 48)
        # A grid, one control per row, so the labels line up and nothing
        # collides or clips in the narrow Details panel.
        sql_grid = QGridLayout()
        sql_grid.setHorizontalSpacing(8)
        sql_grid.setVerticalSpacing(6)
        for r, (text, widget) in enumerate((
            ("Start table:", self._sql_root),
            ("&Join:", self._sql_join),
            ("Row limit:", self._sql_top),
        )):
            label = QLabel(text)
            label.setBuddy(widget)
            widget.setAccessibleName(text.replace("&", "").rstrip(":"))
            sql_grid.addWidget(label, r, 0)
            sql_grid.addWidget(widget, r, 1, 1, 2 if widget is self._sql_root else 1)
        self._copy_sql_btn = QPushButton("Copy S&QL")
        self._copy_sql_btn.setToolTip("Copy the query to the clipboard (Ctrl+Shift+Q).")
        self._copy_sql_btn.clicked.connect(self.copy_sql)
        sql_grid.addWidget(self._copy_sql_btn, 2, 2, Qt.AlignRight)
        sql_grid.setColumnStretch(2, 1)
        sql_layout.addLayout(sql_grid)
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
        self._options_bar.changed.connect(self._sync_export_caption)
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
        # Lasting states the app chose (audit links hidden, automatic Keys
        # only) sit beside it as chips, each undone with a click.
        for chip in self._options_bar.state_chips():
            self._diagram_view.add_status_widget(chip)
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
            "Show or hide the Details panel: the selected table, extracted data, "
            "columns, Mermaid and SQL (Ctrl+I; Ctrl+1–5 open a view)."
        )
        self._details_btn.setStyleSheet(
            # A transparent border that turns accent on keyboard focus (3.4:1+);
            # the wash alone was barely visible.
            "QToolButton { border: 1px solid transparent; padding: 2px 6px; border-radius: 4px; }"
            f"QToolButton:hover {{ background: {_ACCENT_WASH}; }}"
            f"QToolButton:focus {{ background: {_ACCENT_WASH}; border-color: {_ACCENT}; }}"
            "QToolButton:checked { background: transparent; font-weight: 600; }"
            f"QToolButton:checked:hover {{ background: {_ACCENT_WASH}; }}"
        )
        self._details_btn.toggled.connect(self.set_details_visible)
        self._details_btn.setMinimumWidth(self._details_btn.sizeHint().width())
        # One switch for one choice: the canvas shows the diagram or the map.
        switch = QWidget()
        switch_row = QHBoxLayout(switch)
        switch_row.setContentsMargins(0, 0, 8, 0)
        switch_row.setSpacing(0)
        self._view_group = QButtonGroup(self)
        self._view_group.setExclusive(True)
        self._diagram_view_btn = QToolButton()
        self._diagram_view_btn.setText("Diagram")
        self._diagram_view_btn.setCheckable(True)
        self._diagram_view_btn.setChecked(True)
        self._diagram_view_btn.setToolTip("Show the ER diagram of the ticked tables (Ctrl+M switches)")
        switch_row.addWidget(self._diagram_view_btn)
        self._view_group.addButton(self._diagram_view_btn)
        self._map_btn = QToolButton()
        self._map_btn.setText("Map")
        self._map_btn.setCheckable(True)
        self._map_btn.setToolTip(
            "Show the whole schema as a map of clusters, to pick tables from (Ctrl+M)"
        )
        switch_row.addWidget(self._map_btn)
        self._view_group.addButton(self._map_btn)
        self._map_btn.toggled.connect(self.show_map)
        switch.setAccessibleName("Canvas view")
        self._diagram_view_btn.setAccessibleDescription("Canvas view, 1 of 2")
        self._map_btn.setAccessibleDescription("Canvas view, 2 of 2")
        self._style_view_switch()
        # Equal, fixed segments: never squeezed to "…", text centred in each.
        for button in (self._diagram_view_btn, self._map_btn):
            button.ensurePolished()
        width = max(b.sizeHint().width() for b in (self._diagram_view_btn, self._map_btn))
        for button in (self._diagram_view_btn, self._map_btn):
            button.setFixedWidth(width)
        # Fixed at its size hint (both segments plus the layout's margin), so the
        # bar can never squeeze it into its neighbours.
        switch.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self._options_bar.add_leading_widget(switch)
        # The map no longer needs its own "Diagram" button.
        self._map_view._diagram_btn.setVisible(False)
        self._options_bar.add_trailing_widget(self._details_btn)
        self._map_view.table_activated.connect(self._on_map_table_activated)
        self._map_view.draw_requested.connect(self._draw_from_map)
        # The list is built after the canvas, so look it up when the signal fires.
        self._map_view.selection_changed.connect(lambda names: self._selector.mark_map_selection(names))
        self._map_view.show_diagram_requested.connect(lambda: self.show_map(False))
        self._map_view.export_requested.connect(self.export_map)
        self._map_view._hide.toggled.connect(self._options_bar._hide_audit.setChecked)
        self._options_bar._hide_audit.toggled.connect(self._sync_map_hide_audit)
        self._audit_share = 0.0

        # Left: table picker to limit what gets rendered. Right: the tabs.
        self._selector = TableSelector()
        self._selector.applied.connect(self._render_selection)
        self._selector.selection_changed.connect(self._on_selection_edited)
        self._selector.current_table_changed.connect(self._on_list_table_changed)
        self._selector._filter.textChanged.connect(lambda t: self._map_view.set_filter(t))
        self._selector.related_requested.connect(self._add_related_tables)
        self._selector.path_requested.connect(self._find_shortest_path)
        self._selector.undone.connect(self._on_selection_undone)

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
            lambda a: (app_settings().setValue("export/background", a.data()),
                       self._sync_export_caption())
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
            lambda a: (app_settings().setValue("export/png_scale", a.data()),
                       self._sync_export_caption())
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
        # Worded as in the Diagram menu.
        mermaid_menu.addAction("&Copy Mermaid", self.copy_mermaid)
        mermaid_menu.addAction("Save Mermaid (.&mmd)…", self.save_mmd)
        mermaid_menu.addAction("Save Mark&down (.md)…", self.save_md)
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

        # What Export will write, said before it's written: the file can look
        # different from the canvas (a white page, the light theme).
        self._export_caption = QLabel()
        self._export_caption.setAccessibleName("Export settings")
        # The last export stays named (with Show in folder) until the next one,
        # instead of in the status line the next click overwrites.
        self._saved_note = QLabel()
        self._saved_note.setTextFormat(Qt.RichText)
        self._saved_note.linkActivated.connect(self._on_status_link)
        self._saved_note.setVisible(False)

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
        buttons.addWidget(self._export_caption)
        buttons.addSpacing(8)
        buttons.addWidget(self._preview_btn)
        buttons.addSpacing(4)
        buttons.addWidget(sep)
        buttons.addSpacing(4)
        buttons.addWidget(self._mermaid_btn)
        buttons.addStretch(1)
        buttons.addWidget(self._saved_note)
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
            "(Ctrl+I) shows the selected table, the extracted rows, columns, "
            "Mermaid and SQL."
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
            if isinstance(shortcut, list):
                action.setShortcuts(shortcut)
            elif shortcut is not None:
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
            # Ctrl+= too: on most layouts Ctrl++ needs Shift.
            act("Zoom &in", lambda: self._diagram_view.zoom_by(1.25),
                QKeySequence.keyBindings(QKeySequence.StandardKey.ZoomIn)
                + [QKeySequence("Ctrl+=")], schema_only=True)
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
            act("&Draw ticked", self._render_selection,
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

        copy_button = QPushButton("&Copy T-SQL")
        copy_button.setToolTip("Copy this query, to run against your database.")
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
            + " The details are shown in its place."
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
            self._selector.set_draw_pending(True)
            self._render_status.stale(
                f"Out of date ({count} tables) · press F5 to draw"
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
                "The diagram doesn't match your ticks yet. Draw it first (F5)."
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
        self._status.setText("Drawing stopped.")

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
        # Greys out with Export (a stylesheet colour would otherwise win).
        self._export_caption.setStyleSheet(
            f"QLabel {{ {muted} }} QLabel:disabled {{ color: {_DISABLED_FG}; }}"
        )
        line = f"color: {_line_hex(self)};"
        self._divider.setStyleSheet(line)
        for sep in self._button_seps:
            sep.setStyleSheet(line)
        self._drop.retheme()
        self._selector.retheme()
        self._columns.retheme()
        self._options_bar.retheme()
        self._render_status.retheme()
        self._style_view_switch()
        _apply_primary_button_style(self._export_btn)  # its focus ring follows the text colour
        # Views that bake colours into items or pages when they fill.
        self._inspector.retheme()
        self._refresh_inspector()
        self._map_view.retheme()
        self._diagram_view.retheme()

    def _style_view_switch(self):
        """The Diagram | Map segments, in the current palette's colours."""
        if not hasattr(self, "_map_btn"):
            return
        segment = (
            f"QToolButton {{ border: 1px solid {_control_border_hex(self)}; padding: 2px 10px; }}"
            f"QToolButton:checked {{ background: {_ACCENT_FILL}; color: white;"
            f" border-color: {_ACCENT_FILL}; }}"
        )
        self._diagram_view_btn.setStyleSheet(
            segment + "QToolButton { border-top-left-radius: 5px; border-bottom-left-radius: 5px; }"
        )
        self._map_btn.setStyleSheet(
            segment
            + "QToolButton { border-top-right-radius: 5px; border-bottom-right-radius: 5px;"
            " border-left: none; }"
        )

    def changeEvent(self, event):  # noqa: N802 (Qt naming)
        # The palette swap (light↔dark) arrives as a PaletteChange; restyle the
        # widgets whose colours we set by hand.
        if event.type() in (
            QEvent.PaletteChange,
            QEvent.ApplicationPaletteChange,
            QEvent.ThemeChange,
        ):
            # After this turn of the event loop: the children get their new
            # palettes after the window does, and retheme reads theirs.
            if not getattr(self, "_retheme_pending", False):
                self._retheme_pending = True
                QTimer.singleShot(0, self._deferred_retheme)
        super().changeEvent(event)

    def _deferred_retheme(self):
        self._retheme_pending = False
        self.retheme()

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
            self._diagram_view.focus_entity(entity_id)  # readable, if it wasn't
            self.show_details(self._inspector)
        else:
            self._status.setText(
                f"{html.escape(name)} open · double-click it for its columns "
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
        for button, on in ((self._map_btn, visible), (self._diagram_view_btn, not visible)):
            if button.isChecked() != on:
                button.blockSignals(True)
                button.setChecked(on)
                button.blockSignals(False)
        self._options_bar.set_diagram_controls_visible(not visible)
        if visible:
            self._map_view.set_ticked(self._selector.selected_tables())
            self._map_view.set_filter(self._selector._filter.text())

    def _on_map_table_activated(self, name: str, double: bool):
        self._focus_table(name, from_map=True)
        if double:
            self.show_details(self._inspector)

    def _sync_map_hide_audit(self, on: bool):
        """The diagram's audit setting changed: mirror it on the map."""
        box = self._map_view._hide
        if box.isChecked() != on:
            box.blockSignals(True)
            box.setChecked(on)
            box.blockSignals(False)
            if self._map_view.schema_map() is not None:
                self._map_view._rebuild()

    def _auto_hide_audit(self, schema: Schema):
        """Hide audit/system links when they dominate (Dynamics exports)."""
        total = len(schema.relationships)
        hidden = sum(1 for r in schema.relationships if is_hidden_link(r))
        self._audit_share = hidden / total if total else 0.0
        if self._audit_share > 0.30:
            box = self._options_bar._hide_audit
            box.blockSignals(True)
            box.setChecked(True)
            box.blockSignals(False)
            self._sync_map_hide_audit(True)
        self._options_bar.set_audit_share(self._audit_share)

    def _draw_from_map(self, names: list[str]):
        """Tick the map's selection (replacing the ticks, undoably) and draw it.

        Dynamics tables run to hundreds of columns, so a draw of wide tables
        switches Keys only on (once, with an Undo link) to keep the
        relationships readable.
        """
        self._selector.snapshot_for_undo("draw")
        self._selector.set_selected_tables(names)
        self.show_map(False)
        self._render_selection()
        schema = self._drawio_schema
        keys = self._options_bar._keys_only
        if schema is not None and schema.tables and not keys.isChecked():
            per_table = sum(len(t.columns) for t in schema.tables) / len(schema.tables)
            if per_table > 50:
                self._options_bar.set_keys_only_auto()  # re-renders
                self._status.setText(
                    f"Drew {len(schema.tables)} tables with Keys only on (about "
                    f"{int(per_table)} columns per table) · "
                    f'<a href="undo-keys" style="color:{_link_hex(self)}">Show all columns</a>'
                )

    def _on_selection_undone(self, action: str):
        """Undo draw also undoes the Keys only the draw switched on."""
        if action == "draw" and self._options_bar.keys_only_auto():
            self._options_bar._keys_only.setChecked(False)

    def _focus_table(self, name: str, from_diagram: bool = False, from_map: bool = False):
        """Make ``name`` the selected table everywhere ("" clears)."""
        self._selector.focus_table(name)
        self._columns.focus_table(name)
        if not from_diagram:
            # Picked from the list, map or Table view: zoom to it if the
            # diagram is drawn too small to read.
            entity = next((e for e, t in self._entity_to_table.items() if t == name), "")
            if entity:
                self._diagram_view.focus_entity(entity)
            else:
                self._diagram_view.highlight_entity("")
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
        final = self._drawio_schema
        drawn = {t.name.lower() for t in (final.tables if final else [])}
        linked = linked_fk_columns(final).get(key, set()) if final else set()
        self._inspector.show_table(
            table, outgoing, incoming, drawn, self._columns.excluded_pairs(),
            self._options_bar.diagram_options(), linked,
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
            self._pending_path = path
            self._on_load_failed(describe_load_error(exc))
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
        self._saved_note.setVisible(False)  # that export was of the last file
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
        self._selector.cluster_provider = lambda s=schema: cluster_index(s)
        self._auto_hide_audit(schema)
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

        # Small schemas render in full. Every file opens on the diagram; for a
        # large one the canvas points at the map (Ctrl+M), where the shape of
        # the schema shows and a region can be picked to draw.
        if len(names) <= AUTO_RENDER_LIMIT:
            self._selector.check_all()
        self.show_map(False)  # before the render, whose status names the way in
        self._render_selection()

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
                    "slow to draw and hard to read. Draw it anyway?",
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
        self._selector.set_draw_pending(False)

        # Related tables are ticked explicitly via "Add related tables", so the
        # checked list is the whole selection — no implicit expansion here.
        filtered = filter_schema(self._schema, names)

        # Refresh the column picker for the tables now in play, then apply the
        # user's column choices on top of the table filter.
        self._columns.set_tables(filtered.tables)
        final = self._columns.sorted_schema(
            filter_columns(filtered, self._columns.excluded_pairs())
        )
        if self._options_bar.hide_audit_links():
            final = Schema(
                tables=final.tables,
                relationships=[r for r in final.relationships if not is_hidden_link(r)],
            )
        self._drawio_schema = final
        self._columns.set_keys_only(self._options_bar.diagram_options().keys_only)
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
                    "No tables ticked yet.\n\n"
                    "Tick tables on the left; the diagram updates as you go."
                    + (
                        " Or open the Map (Ctrl+M) to see the whole schema and "
                        "draw a cluster."
                        if total > AUTO_RENDER_LIMIT
                        else ""
                    )
                    if total <= RENDER_WARN_LIMIT
                    else f"{total:,} tables is too many to draw at once.\n\n"
                    "Open the Map (Ctrl+M) to see the whole schema, then select a "
                    "cluster or region and draw it.\n\n"
                    "Or filter the list on the left and tick a starting table, then use "
                    "“Add related tables” to grow the diagram around it, or open "
                    "“Trace path between tables” to connect two tables. Saved "
                    "selections load from the Table list menu."
                )
            )
            self._status.setText(
                f"Loaded {self._loaded_name} — {_plural(total, 'table')}. "
                + (
                    "Pick a cluster on the map, or tick tables on the left."
                    if self.map_visible()
                    else "Tick tables on the left, or open the Map (Ctrl+M) to pick a cluster."
                    if total > AUTO_RENDER_LIMIT
                    else "Tick tables on the left to draw a diagram."
                )
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
                "Narrow it down with the filter and tick fewer tables, then click "
                "“Draw ticked”. The full selection is still available in the "
                "Mermaid tab and via Save .mmd / .md."
            )
            self._status.setText(
                f"Loaded {self._loaded_name} — {tables_phrase} selected, "
                f"{_plural(col_count, 'column')}: too large to render "
                "(tick fewer tables)."
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
        self._diagram_view.set_diagram(
            mermaid_text, self._options_bar.render_style(), self._hub_entity(final)
        )
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

    def _hub_entity(self, schema: Schema) -> str:
        """The entity id of the drawn table with the most drawn links."""
        degree: dict[str, int] = {}
        for rel in schema.relationships:
            for name in {rel.parent_table, rel.child_table}:
                degree[name] = degree.get(name, 0) + 1
        if not degree:
            return ""
        hub = max(sorted(degree), key=lambda name: degree[name])
        return next((e for e, t in self._entity_to_table.items() if t == hub), "")

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
            self._status.setText("Tick some tables first; there's no query yet.")
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
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
        except OSError as exc:
            QMessageBox.warning(
                self,
                "Couldn't save the table list",
                f"{path}\n\n{exc.strerror or exc}. Choose another folder or file name.",
            )
            return
        self._report_saved(path)

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
        text = (
            f"Saved {html.escape(os.path.basename(path))}{html.escape(note)} · "
            f'<a href="show-in-folder" style="color:{_link_hex(self)}">Show in folder</a>'
        )
        self._status.setText(text)
        self._saved_note.setText(text)
        self._saved_note.setToolTip(path)
        self._saved_note.setVisible(True)

    def _export_summary(self) -> str:
        """What the next export writes, e.g. "PNG · 2× · white page · Keys only"."""
        name = next(
            (label.split(" (")[0].replace(" image", "") for k, label, *_ in EXPORT_FORMATS
             if k == self._export_kind),
            "",
        )
        parts = [name]
        if self._export_kind in ("drawio", "excalidraw"):
            parts.append("dark shapes" if self._export_style().theme == "dark" else "light shapes")
        else:
            if self._export_kind == "png":
                parts.append(f"{self.png_scale():g}×")
            parts.append({
                "white": "white page",
                "transparent": "transparent",
                "match": "as on screen",
            }.get(self.export_background(), "white page"))
        if self._options_bar.diagram_options().keys_only:
            parts.append("Keys only")
        return " · ".join(p for p in parts if p)

    def _sync_export_caption(self):
        if not hasattr(self, "_export_caption"):
            return
        summary = self._export_summary()
        self._export_caption.setText(summary)
        self._export_caption.setToolTip(
            f"The next export: {summary}. Change it from the Export button's arrow."
        )

    def _on_status_link(self, href: str):
        if href == "undo-keys":
            self._options_bar._keys_only.setChecked(False)
            return
        if href == "show-in-folder" and getattr(self, "_last_saved", ""):
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(self._last_saved)))

    def _sync_export_enabled(self):
        """Export/Preview/Mermaid only when there's a selection to output."""
        has_tables = bool(self._drawio_schema is not None and self._drawio_schema.tables)
        for widget in (self._export_btn, self._preview_btn, self._mermaid_btn,
                       self._export_caption):
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
        names = {k: label.split(" (")[0].replace(" image", "") for k, label, *_ in EXPORT_FORMATS}
        name = names.get(self._export_kind)
        text = f"&Export {name}…" if name else "&Export…"
        self._export_btn.setText(text)
        # The accessible name starts with the visible label (WCAG 2.5.3).
        self._export_btn.setAccessibleName(text.replace("&", ""))
        self._sync_export_caption()
        for key, action in self._export_actions.items():
            action.setEnabled(True)
            font = action.font()
            font.setBold(key == self._export_kind)
            action.setFont(font)

    def export_map(self):
        """Save the schema map as a PNG or SVG image."""
        if self._map_view.schema_map() is None:
            return
        path = self._save_path(
            "Save schema map", "-map.png", "PNG image (*.png);;SVG image (*.svg)"
        )
        if not path:
            return
        try:
            self._map_view.export_image(path)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Couldn't save the map", str(exc))
            return
        self._report_saved(path)

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
        # The map builds itself when first shown; a headless capture never
        # shows the window, so build it here or a big schema grabs a blank map.
        if self.map_visible() and self._map_view.schema_map() is None:
            self._map_view._rebuild()
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
