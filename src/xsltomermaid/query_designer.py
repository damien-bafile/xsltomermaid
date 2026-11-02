"""The Query view: build a T-SQL ``SELECT`` by placing tables and joining them.

The canvas holds a card per placed table. Adding a table joins it on the
spreadsheet's foreign keys; dragging from a column to a column of another card
joins them by hand, and clicking a join line changes its type. Clicking a
column puts it in the result. The panel beside the canvas lists the output
columns (with aggregates and sorting) and the joins, over the T-SQL, which is
rewritten on every change. The design itself lives in
:class:`~xsltomermaid.query_model.QueryDesign`; this module only draws and
edits it.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPalette,
    QPen,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QCompleter,
    QGraphicsItem,
    QGraphicsPathItem,
    QGraphicsScene,
    QGraphicsView,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .excel_to_mermaid import Column, Schema, Table, system_columns
from .highlight import SQL, RuleHighlighter
from .query_model import (
    AGGREGATES,
    JOIN_KINDS,
    Join,
    OutputColumn,
    QueryDesign,
    candidate_joins,
    generate_designed_select,
)
from .theme import _ACCENT_FILL, _blend, _muted_hex, _ok_hex, _surface

# A card with more columns than this starts collapsed to its keys and the
# columns the query uses, with a row to show the rest.
COLLAPSE_OVER = 12
_FULL_JOIN_COLOUR = {"light": "#8250df", "dark": "#a371f7"}
_JOIN_TEXT = {
    "INNER": "Inner join: only rows that match in both",
    "LEFT": "Left join: every {left} row, matched {right} rows",
    "RIGHT": "Right join: every {right} row, matched {left} rows",
    "FULL": "Full outer join: every row of both",
}
_SORT_LABELS = (("", ""), ("↑ Asc", "ASC"), ("↓ Desc", "DESC"))
# New cards are laid out in rows of this many.
_CARDS_PER_ROW = 3


def _join_colour(kind: str, widget) -> QColor:
    if kind == "FULL":
        return QColor(_FULL_JOIN_COLOUR[_surface(widget)])
    if kind in ("LEFT", "RIGHT"):
        return QColor(_ok_hex(widget))
    return QColor(_ACCENT_FILL)


def _short_type(column: Column, limit: int = 16) -> str:
    text = column.rendered_type()
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# Canvas items
# ---------------------------------------------------------------------------
class TableCard(QGraphicsItem):
    """A placed table: a header (alias and table name) over its columns.

    Drag the header to move it. Click a column to put it in the result; drag
    from a column to a column of another card to join them.
    """

    HEAD_H, ROW_H = 30, 22

    def __init__(self, view: "QueryDesignerView", alias: str):
        super().__init__()
        self.v, self.alias = view, alias
        self.rows: list[Column | None] = []  # None = the show-all / show-fewer row
        self.hidden = 0
        self.width = 220.0
        self.hover_row: int | None = None
        self.target_row: int | None = None  # under a join being dragged here
        self._press: tuple[int, QPointF] | None = None
        self._linking = False
        self.setFlags(
            QGraphicsItem.ItemIsMovable
            | QGraphicsItem.ItemIsSelectable
            | QGraphicsItem.ItemSendsGeometryChanges
        )
        self.setAcceptHoverEvents(True)
        self.refresh()

    # --- layout -------------------------------------------------------------

    def refresh(self):
        """Re-read the columns to show (after a change to the design)."""
        self.prepareGeometryChange()
        columns, self.hidden, collapsible = self.v.card_columns(self.alias)
        self.rows = list(columns) + ([None] if collapsible else [])
        fm = QFontMetrics(self.v.font())
        bold = QFont(self.v.font())
        bold.setBold(True)
        title = QFontMetrics(bold).horizontalAdvance(self.title()) + 60
        widest = max(
            (fm.horizontalAdvance(c.name) + fm.horizontalAdvance(_short_type(c)) + 74
             for c in columns),
            default=0,
        )
        self.width = float(max(200, title, widest))
        self.update()

    def title(self) -> str:
        table = self.v.design.ref(self.alias).table
        star = "  ★" if self.v.design.base == self.alias else ""
        return (self.alias if table.lower() == self.alias.lower() else f"{self.alias}  ·  {table}") + star

    def height(self) -> float:
        return self.HEAD_H + max(1, len(self.rows)) * self.ROW_H + 4

    def boundingRect(self) -> QRectF:  # noqa: N802 (Qt naming)
        return QRectF(0, 0, self.width, self.height())

    def row_at(self, pos: QPointF) -> int | None:
        if pos.y() < self.HEAD_H:
            return None
        i = int((pos.y() - self.HEAD_H) // self.ROW_H)
        return i if 0 <= i < len(self.rows) else None

    def column_at(self, pos: QPointF) -> str | None:
        row = self.row_at(pos)
        return self.rows[row].name if row is not None and self.rows[row] is not None else None

    def anchor(self, column: str, right: bool) -> QPointF:
        """Where a join line meets ``column`` (the header when it isn't shown)."""
        lowered = column.lower()
        for i, c in enumerate(self.rows):
            if c is not None and c.name.lower() == lowered:
                y = self.HEAD_H + i * self.ROW_H + self.ROW_H / 2
                break
        else:
            y = self.HEAD_H / 2
        return self.mapToScene(QPointF(self.width if right else 0, y))

    def close_rect(self) -> QRectF:
        return QRectF(self.width - 26, 5, 20, 20)

    # --- painting -------------------------------------------------------------

    def paint(self, p: QPainter, option, widget=None):  # noqa: ARG002
        pal = self.v.palette()
        base, text = pal.color(QPalette.Base), pal.color(QPalette.Text)
        muted = QColor(_muted_hex(self.v))
        accent = QColor(_ACCENT_FILL)
        p.setRenderHint(QPainter.Antialiasing)
        selected = self.isSelected()
        p.setPen(QPen(accent if selected else pal.color(QPalette.Mid), 2.5 if selected else 1))
        p.setBrush(base)
        p.drawRoundedRect(self.boundingRect().adjusted(1, 1, -1, -1), 6, 6)

        head = QPainterPath()
        head.addRoundedRect(QRectF(1, 1, self.width - 2, self.HEAD_H), 6, 6)
        head.addRect(QRectF(1, self.HEAD_H - 6, self.width - 2, 6))
        p.fillPath(head.simplified(), accent)
        bold = QFont(self.v.font())
        bold.setBold(True)
        p.setFont(bold)
        p.setPen(QColor("white"))
        p.drawText(QRectF(10, 0, self.width - 42, self.HEAD_H), Qt.AlignVCenter, self.title())
        p.drawText(self.close_rect(), Qt.AlignCenter, "✕")

        used = self.v.used_columns(self.alias)
        joined = self.v.joined_columns(self.alias)
        stripe = _blend(base, text, 0.04)
        hover = _blend(base, accent, 0.14)
        target = QColor(_ok_hex(self.v))
        target.setAlpha(90)
        for i, column in enumerate(self.rows):
            r = QRectF(1, self.HEAD_H + i * self.ROW_H, self.width - 2, self.ROW_H)
            if i == self.target_row:
                p.fillRect(r, target)
            elif i == self.hover_row:
                p.fillRect(r, hover)
            elif i % 2:
                p.fillRect(r, stripe)
            if column is None:  # the show-all / show-fewer row
                p.setFont(self.v.font())
                p.setPen(muted)
                label = (f"Show all columns (+{self.hidden})" if self.hidden
                         else "Show fewer columns")
                p.drawText(r.adjusted(10, 0, -8, 0), Qt.AlignVCenter, label)
                continue
            box = QRectF(10, r.y() + 5, 12, 12)
            out = column.name.lower() in used
            p.setPen(QPen(text, 1.2))
            p.setBrush(accent if out else Qt.NoBrush)
            p.drawRoundedRect(box, 2, 2)
            if out:
                p.setPen(QPen(QColor("white"), 1.8))
                p.drawPolyline([box.topLeft() + QPointF(2.5, 6), box.topLeft() + QPointF(5, 9),
                                box.topLeft() + QPointF(9.5, 3)])
            font = QFont(self.v.font())
            font.setBold(column.name.lower() in joined)
            p.setFont(font)
            p.setPen(text)
            p.drawText(r.adjusted(30, 0, -8, 0), Qt.AlignVCenter, column.name)
            p.setFont(self.v.font())
            p.setPen(muted)
            key = "PK " if column.is_primary_key else "FK " if column.foreign_key_reference else ""
            p.drawText(r.adjusted(30, 0, -8, 0), Qt.AlignVCenter | Qt.AlignRight,
                       key + _short_type(column))

    def itemChange(self, change, value):  # noqa: N802 (Qt naming)
        if change == QGraphicsItem.ItemPositionHasChanged:
            self.v.positions[self.alias] = self.pos()
            self.v.update_edges(self.alias)
        return super().itemChange(change, value)

    # --- mouse ----------------------------------------------------------------

    def mousePressEvent(self, event):  # noqa: N802 (Qt naming)
        if event.button() == Qt.LeftButton:
            if self.close_rect().contains(event.pos()):
                QTimer.singleShot(0, lambda: self.v.remove_table(self.alias))
                event.accept()
                return
            row = self.row_at(event.pos())
            if row is not None:
                self._press = (row, event.scenePos())
                self._linking = False
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802 (Qt naming)
        if self._press is not None:
            row, start = self._press
            column = self.rows[row]
            if (not self._linking and column is not None
                    and (event.scenePos() - start).manhattanLength() >= 6):
                self._linking = True
                self.v.begin_link(self, column.name)
            if self._linking:
                self.v.drag_link(event.scenePos())
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):  # noqa: N802 (Qt naming)
        if self._press is not None:
            row, _ = self._press
            self._press = None
            if self._linking:
                self._linking = False
                self.v.end_link(event.scenePos())
            elif self.rows[row] is None:
                self.v.toggle_expanded(self.alias)
            else:
                self.v.toggle_output(self.alias, self.rows[row].name)
            return
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event):  # noqa: N802 (Qt naming)
        self.v.card_menu(self, self.column_at(event.pos()), event.screenPos())

    def hoverMoveEvent(self, event):  # noqa: N802 (Qt naming)
        row = self.row_at(event.pos())
        if row != self.hover_row:
            self.hover_row = row
            self.update()
        on_header = row is None and not self.close_rect().contains(event.pos())
        self.setCursor(Qt.OpenHandCursor if on_header else Qt.PointingHandCursor)

    def hoverLeaveEvent(self, event):  # noqa: N802 (Qt naming), ARG002
        self.hover_row = None
        self.update()


class JoinEdge(QGraphicsPathItem):
    """One column pair of a join; the first pair carries the join's label.
    A filled end marks a table whose every row is kept."""

    def __init__(self, view: "QueryDesignerView", join: Join, pair: int):
        super().__init__()
        self.v, self.join, self.pair = view, join, pair
        self.hover = False
        self.p1 = self.p2 = QPointF()
        self.setZValue(-1)
        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.PointingHandCursor)
        self.update_path()

    def update_path(self):
        left, right = self.v.cards.get(self.join.left), self.v.cards.get(self.join.right)
        if left is None or right is None:
            return
        left_col, right_col = self.join.pairs[self.pair]
        forward = left.sceneBoundingRect().center().x() <= right.sceneBoundingRect().center().x()
        p1, p2 = left.anchor(left_col, forward), right.anchor(right_col, not forward)
        dx = max(50.0, abs(p2.x() - p1.x()) / 2) * (1 if forward else -1)
        path = QPainterPath(p1)
        path.cubicTo(p1 + QPointF(dx, 0), p2 - QPointF(dx, 0), p2)
        self.prepareGeometryChange()
        self.p1, self.p2 = p1, p2
        self.setPath(path)
        text = _JOIN_TEXT[self.join.kind].format(left=self.join.left, right=self.join.right)
        self.setToolTip(
            f"{self.join.left}.{left_col} = {self.join.right}.{right_col}\n{text}\nClick to change."
        )

    def label_rect(self) -> QRectF:
        mid = self.path().pointAtPercent(0.5)
        return QRectF(mid.x() - 26, mid.y() - 10, 52, 20)

    def boundingRect(self) -> QRectF:  # noqa: N802 (Qt naming)
        return self.path().boundingRect().adjusted(-30, -20, 30, 20)

    def shape(self) -> QPainterPath:
        stroker = QPainterPathStroker()
        stroker.setWidth(12)
        shape = stroker.createStroke(self.path())
        if self.pair == 0:
            shape.addRect(self.label_rect())
        return shape

    def paint(self, p: QPainter, option, widget=None):  # noqa: ARG002
        colour = _join_colour(self.join.kind, self.v)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(colour, 3.2 if self.hover else 2))
        p.setBrush(Qt.NoBrush)
        p.drawPath(self.path())
        kind = self.join.kind
        for point, kept in ((self.p1, kind in ("LEFT", "FULL")), (self.p2, kind in ("RIGHT", "FULL"))):
            p.setBrush(colour if kept else self.v.palette().color(QPalette.Base))
            p.drawEllipse(point, 4.5, 4.5)
        if self.pair == 0:
            r = self.label_rect()
            p.setPen(Qt.NoPen)
            p.setBrush(colour)
            p.drawRoundedRect(r, 9, 9)
            font = QFont(self.v.font())
            font.setBold(True)
            font.setPointSizeF(max(6.0, font.pointSizeF() - 1))
            p.setFont(font)
            p.setPen(QColor("white"))
            p.drawText(r, Qt.AlignCenter, kind)

    def hoverEnterEvent(self, event):  # noqa: N802 (Qt naming), ARG002
        self.hover = True
        self.update()

    def hoverLeaveEvent(self, event):  # noqa: N802 (Qt naming), ARG002
        self.hover = False
        self.update()

    def mousePressEvent(self, event):  # noqa: N802 (Qt naming)
        self.v.join_menu(self.join, event.screenPos())
        event.accept()


class _Canvas(QGraphicsView):
    """Pan by dragging the background; Ctrl+wheel zooms; Delete removes."""

    def __init__(self, view: "QueryDesignerView"):
        super().__init__()
        self.v = view
        self.setScene(QGraphicsScene(self))
        self.scene().setSceneRect(-3000, -3000, 9000, 7000)
        self.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setAccessibleName("Query canvas")

    def wheelEvent(self, event):  # noqa: N802 (Qt naming)
        if event.modifiers() & Qt.ControlModifier:
            factor = 1.0015 ** max(-360, min(360, event.angleDelta().y()))
            scale = self.transform().m11() * factor
            if 0.25 <= scale <= 2.5:
                self.scale(factor, factor)
        else:
            super().wheelEvent(event)

    def keyPressEvent(self, event):  # noqa: N802 (Qt naming)
        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            for item in self.scene().selectedItems():
                if isinstance(item, TableCard):
                    self.v.remove_table(item.alias)
        else:
            super().keyPressEvent(event)

    def fit(self):
        cards = list(self.v.cards.values())
        if not cards:
            self.resetTransform()
            self.centerOn(0, 0)
            return
        rect = cards[0].sceneBoundingRect()
        for card in cards[1:]:
            rect = rect.united(card.sceneBoundingRect())
        self.resetTransform()
        self.fitInView(rect.adjusted(-40, -40, 40, 40), Qt.KeepAspectRatio)
        if self.transform().m11() > 1:
            self.resetTransform()
            self.centerOn(rect.center())


# ---------------------------------------------------------------------------
# The view
# ---------------------------------------------------------------------------
class QueryDesignerView(QWidget):
    """The Query canvas view. The window gives it the whole sheet
    (:meth:`set_schema`) and a way to read the ticked tables
    (:attr:`ticked_tables`); it reports what it did on :attr:`message`."""

    message = Signal(str)  # a sentence for the window's status line
    sql_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.schema: Schema | None = None
        self.design = QueryDesign()
        self.hide_system = False
        self.ticked_tables = lambda: []  # set by the window
        self.cards: dict[str, TableCard] = {}
        self.edges: list[JoinEdge] = []
        self.positions: dict[str, QPointF] = {}
        self.expanded: set[str] = set()
        self._tables: dict[str, Table] = {}
        self._link: QGraphicsPathItem | None = None
        self._link_from: tuple[TableCard, str] | None = None
        self._link_target: TableCard | None = None
        self._sql = ""

        # Toolbar
        self.add_box = QComboBox()
        self.add_box.setEditable(True)
        self.add_box.setInsertPolicy(QComboBox.NoInsert)
        self.add_box.setMinimumWidth(220)
        self.add_box.lineEdit().setPlaceholderText("Add a table…")
        self.add_box.completer().setFilterMode(Qt.MatchContains)
        self.add_box.completer().setCompletionMode(QCompleter.PopupCompletion)
        self.add_box.setAccessibleName("Table to add")
        self.add_box.lineEdit().returnPressed.connect(self._add_from_box)
        add_btn = QPushButton("&Add")
        add_btn.setToolTip("Add the table named in the box to the query.")
        add_btn.clicked.connect(self._add_from_box)
        self.add_ticked_btn = QPushButton("Add &ticked tables")
        self.add_ticked_btn.setToolTip("Add every table ticked in the table list.")
        self.add_ticked_btn.clicked.connect(lambda: self.add_tables(self.ticked_tables()))
        self.auto_join = QCheckBox("Auto-join")
        self.auto_join.setChecked(True)
        self.auto_join.setToolTip(
            "Join a table you add to one already in the query, on a foreign key between them."
        )
        fit_btn = QPushButton("Fit")
        fit_btn.setToolTip("Fit every table into view.")
        fit_btn.clicked.connect(lambda: self.canvas.fit())
        clear_btn = QPushButton("Clear")
        clear_btn.setToolTip("Remove every table from the query.")
        clear_btn.clicked.connect(self.clear)
        bar = QHBoxLayout()
        bar.setContentsMargins(8, 6, 8, 0)
        for w in (self.add_box, add_btn, self.add_ticked_btn, self.auto_join, fit_btn, clear_btn):
            bar.addWidget(w)
        bar.addStretch()

        # Canvas
        self.canvas = _Canvas(self)
        self.hint = self.canvas.scene().addText(
            "Add tables with the box above, “Add ticked tables”, or right-click a table in "
            "the list › Add to query.\n\n"
            "• Tables join on their foreign keys as they're added. Drag from a column to a "
            "column of another table to join them yourself; click a join to change its type.\n"
            "• Click a column to put it in the result. Right-click for counts and more."
        )
        self.hint.setTextWidth(560)

        # Output columns
        self.outputs = QTableWidget(0, 4)
        self.outputs.setHorizontalHeaderLabels(["Column", "Show as", "Sort", ""])
        self.outputs.horizontalHeaderItem(0).setToolTip(
            "Double-click a column to name it in the result (AS …)."
        )
        header = self.outputs.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        # The other columns hold controls, which "resize to contents" doesn't
        # measure: size them to their longest option instead.
        fm = self.fontMetrics()
        for section, widest in ((1, "Count Distinct"), (2, "↓ Desc"), (3, "↑ ↓ ✕")):
            header.setSectionResizeMode(section, QHeaderView.Fixed)
            self.outputs.setColumnWidth(section, fm.horizontalAdvance(widest) + 48)
        self.outputs.verticalHeader().setVisible(False)
        self.outputs.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        self.outputs.setAccessibleName("Output columns")
        self.outputs.itemChanged.connect(self._output_renamed)
        count_btn = QPushButton("Add row &count")
        count_btn.setToolTip("Add COUNT(*) to the result: the number of rows in each group.")
        count_btn.clicked.connect(lambda: self.add_output(OutputColumn("", aggregate="COUNT")))
        clear_out = QPushButton("Clear")
        clear_out.setToolTip("Remove every output column (the query then returns every column).")
        clear_out.clicked.connect(self.clear_outputs)
        out_tab = QWidget()
        out_layout = QVBoxLayout(out_tab)
        out_bar = QHBoxLayout()
        out_bar.addWidget(count_btn)
        out_bar.addWidget(clear_out)
        out_bar.addStretch()
        out_layout.addLayout(out_bar)
        note = QLabel(
            "Click columns on the canvas to add them. With none, every column is returned. "
            "When any column is counted or summed, the plain ones are grouped on."
        )
        note.setWordWrap(True)
        out_layout.addWidget(note)
        out_layout.addWidget(self.outputs, 1)

        # Joins
        self.joins_table = QTableWidget(0, 3)
        self.joins_table.setHorizontalHeaderLabels(["Join on", "Type", ""])
        self.joins_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.joins_table.verticalHeader().setVisible(False)
        self.joins_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.joins_table.setAccessibleName("Joins")
        joins_tab = QWidget()
        joins_layout = QVBoxLayout(joins_tab)
        joins_note = QLabel(
            "Left keeps every row of the first table, Right of the second. "
            "Right-click a table on the canvas › Join to… for the foreign keys it has."
        )
        joins_note.setWordWrap(True)
        joins_layout.addWidget(joins_note)
        joins_layout.addWidget(self.joins_table, 1)

        self.side_tabs = QTabWidget()
        self.side_tabs.addTab(out_tab, "&Output")
        self.side_tabs.addTab(joins_tab, "&Joins")

        # SQL
        self.start = QComboBox()
        self.start.setToolTip("The table in FROM; the joins branch out from it.")
        self.start.currentIndexChanged.connect(self._start_changed)
        self.top = QSpinBox()
        self.top.setRange(0, 1_000_000)
        self.top.setSpecialValueText("No limit")
        self.top.setToolTip("SELECT TOP (n). 0 means no limit.")
        self.top.valueChanged.connect(self._options_changed)
        self.distinct = QCheckBox("Distinct")
        self.distinct.setToolTip("SELECT DISTINCT: leave out repeated rows.")
        self.quote_all = QCheckBox("Quote all names")
        self.quote_all.setToolTip("Bracket every name ([dbo].[Order]).")
        self.latest = QCheckBox("Latest version only")
        self.latest.setToolTip(
            "For a Dynamics copy made by Azure Synapse Link or Fabric: keep each record's "
            "newest row (highest versionnumber per primary key) and leave out deleted ones."
        )
        self.active = QCheckBox("Active records only")
        self.active.setToolTip("Keep rows with statecode = 0 in every table that has one.")
        for box in (self.distinct, self.quote_all, self.latest, self.active):
            box.toggled.connect(self._options_changed)
        self.copy_btn = QPushButton("Copy S&QL")
        self.copy_btn.setToolTip("Copy the query to the clipboard (Ctrl+Shift+Q).")
        self.copy_btn.clicked.connect(self.copy_sql)
        grid = QGridLayout()
        grid.addWidget(QLabel("Start table:"), 0, 0)
        grid.addWidget(self.start, 0, 1, 1, 2)
        grid.addWidget(QLabel("Row limit:"), 1, 0)
        grid.addWidget(self.top, 1, 1)
        grid.addWidget(self.copy_btn, 1, 2, Qt.AlignRight)
        grid.addWidget(self.distinct, 2, 1)
        grid.addWidget(self.quote_all, 2, 2)
        grid.addWidget(self.latest, 3, 1)
        grid.addWidget(self.active, 3, 2)
        grid.setColumnStretch(2, 1)
        self.sql_view = QPlainTextEdit()
        self.sql_view.setReadOnly(True)
        self.sql_view.setFont(QFont("Menlo, Consolas, monospace"))
        self.sql_view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.sql_view.setAccessibleName("Designed SQL query")
        self.highlighter = RuleHighlighter(self.sql_view.document(), self.sql_view, SQL)
        sql_box = QWidget()
        sql_layout = QVBoxLayout(sql_box)
        sql_layout.setContentsMargins(0, 0, 0, 0)
        sql_layout.addLayout(grid)
        sql_layout.addWidget(self.sql_view, 1)

        side = QSplitter(Qt.Vertical)
        side.addWidget(self.side_tabs)
        side.addWidget(sql_box)
        side.setSizes([260, 340])
        split = QSplitter()
        split.addWidget(self.canvas)
        split.addWidget(side)
        split.setStretchFactor(0, 1)
        split.setSizes([720, 480])
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(bar)
        layout.addWidget(split, 1)

        self._refresh_timer = QTimer(self, singleShot=True, interval=0)
        self._refresh_timer.timeout.connect(self._refresh_now)
        self.set_schema(None)

    # --- the window's side ----------------------------------------------------

    def set_schema(self, schema: Schema | None):
        """A new spreadsheet (or none): start an empty query."""
        self.schema = schema
        self._tables = {t.name.lower(): t for t in schema.tables} if schema else {}
        self.add_box.clear()
        if schema:
            self.add_box.addItems(sorted((t.name for t in schema.tables), key=str.lower))
        self.add_box.setCurrentIndex(-1)
        self.add_box.setEnabled(schema is not None)
        self.add_ticked_btn.setEnabled(schema is not None)
        self.clear()

    def set_hide_system(self, hide: bool):
        """Follow the options bar's Hide system columns (on the cards only:
        a column the query uses is always shown)."""
        if hide != self.hide_system:
            self.hide_system = hide
            self._rebuild_cards()

    def sql_text(self) -> str:
        return self._sql if self.design.tables else ""

    def retheme(self):
        self.highlighter.retheme()
        self.hint.setDefaultTextColor(QColor(_muted_hex(self)))
        for item in self.canvas.scene().items():
            item.update()

    # --- tables -----------------------------------------------------------------

    def table(self, alias: str) -> Table | None:
        ref = self.design.ref(alias)
        return self._tables.get(ref.table.lower()) if ref else None

    def _add_from_box(self):
        name = self.add_box.currentText().strip()
        if not name:
            return
        match = self._tables.get(name.lower())
        if match is None:
            options = [t for t in self._tables.values() if name.lower() in t.name.lower()]
            match = options[0] if len(options) == 1 else None
        if match is None:
            self.message.emit(f"No table is called “{name}”.")
            return
        self.add_table(match.name)
        self.add_box.setEditText("")

    def add_tables(self, names):
        names = list(names)
        if not names:
            self.message.emit("Tick some tables in the list first.")
            return
        for name in names:
            self.add_table(name, announce=False)
        self.canvas.fit()
        self.message.emit(f"Added {len(names)} table{'s' if len(names) != 1 else ''} to the query.")

    def add_table(self, name: str, announce: bool = True):
        if self.schema is None or name.lower() not in self._tables:
            return None
        before = len(self.design.joins)
        ref = self.design.add_table(self.schema, name, auto_join=self.auto_join.isChecked())
        self.positions[ref.alias] = self._next_position()
        self._changed()
        if announce:
            if len(self.design.joins) > before:
                j = self.design.joins[-1]
                cols = ", ".join(a for a, _b in j.pairs)
                self.message.emit(f"{ref.table} added as {ref.alias}, joined to {j.left} on {cols}.")
            elif len(self.design.tables) > 1:
                self.message.emit(
                    f"{ref.table} added as {ref.alias}. No foreign key links it to the query: "
                    "drag from one of its columns to another table's to join it."
                )
            else:
                self.message.emit(f"{ref.table} added as {ref.alias}.")
        return ref

    def _next_position(self) -> QPointF:
        """Beside the last row of cards, or under it once it holds three."""
        cards = list(self.cards.values())
        if not cards:
            return QPointF(0, 0)
        row_top = max(c.pos().y() for c in cards)
        row = [c for c in cards if abs(c.pos().y() - row_top) < 1]
        if len(row) < _CARDS_PER_ROW:
            return QPointF(max(c.sceneBoundingRect().right() for c in row) + 90, row_top)
        return QPointF(0, max(c.sceneBoundingRect().bottom() for c in cards) + 60)

    def remove_table(self, alias: str):
        if self.design.ref(alias) is None:
            return
        self.design.remove_table(alias)
        self.positions.pop(alias, None)
        self.expanded.discard(alias)
        self._changed()

    def clear(self):
        self.design = QueryDesign()
        self.positions.clear()
        self.expanded.clear()
        self._changed()

    def set_base(self, alias: str):
        self.design.base = alias
        self._changed()

    def toggle_expanded(self, alias: str):
        self.expanded.symmetric_difference_update({alias})
        self._rebuild_cards()

    # --- what a card shows --------------------------------------------------------

    def used_columns(self, alias: str) -> set[str]:
        return {o.column.lower() for o in self.design.outputs if o.alias == alias}

    def joined_columns(self, alias: str) -> set[str]:
        found: set[str] = set()
        for j in self.design.joins:
            if j.left == alias:
                found |= {a.lower() for a, _b in j.pairs}
            if j.right == alias:
                found |= {b.lower() for _a, b in j.pairs}
        return found

    def card_columns(self, alias: str) -> tuple[list[Column], int, bool]:
        """(columns to show, how many are folded away, whether it can fold)."""
        table = self.table(alias)
        if table is None:
            return [], 0, False
        needed = self.used_columns(alias) | self.joined_columns(alias)
        system = system_columns(table) if self.hide_system else set()
        columns = [c for c in table.columns if c.name.lower() not in system or c.name.lower() in needed]
        if len(columns) <= COLLAPSE_OVER:
            return columns, 0, False
        if alias in self.expanded:
            return columns, 0, True
        shown = [c for c in columns if c.is_primary_key or c.foreign_key_reference
                 or c.name.lower() in needed]
        return shown, len(columns) - len(shown), True

    # --- joins ------------------------------------------------------------------

    def begin_link(self, card: TableCard, column: str):
        self._link_from = (card, column)
        self._link = QGraphicsPathItem()
        pen = QPen(QColor(_ACCENT_FILL), 2, Qt.DashLine)
        self._link.setPen(pen)
        self._link.setZValue(10)
        self.canvas.scene().addItem(self._link)

    def _card_at(self, pos: QPointF) -> TableCard | None:
        for item in self.canvas.scene().items(pos):
            if isinstance(item, TableCard):
                return item
        return None

    def drag_link(self, pos: QPointF):
        if self._link is None or self._link_from is None:
            return
        card, column = self._link_from
        start = card.anchor(column, pos.x() >= card.sceneBoundingRect().center().x())
        path = QPainterPath(start)
        path.lineTo(pos)
        self._link.setPath(path)
        target = self._card_at(pos)
        target = target if target is not card else None
        if self._link_target is not None and self._link_target is not target:
            self._link_target.target_row = None
            self._link_target.update()
        self._link_target = target
        if target is not None:
            row = target.row_at(target.mapFromScene(pos))
            target.target_row = row if row is not None and target.rows[row] is not None else None
            target.update()

    def end_link(self, pos: QPointF):
        source = self._link_from
        if self._link is not None:
            self.canvas.scene().removeItem(self._link)
        if self._link_target is not None:
            self._link_target.target_row = None
            self._link_target.update()
        self._link = self._link_from = self._link_target = None
        target = self._card_at(pos)
        if source is None or target is None or target is source[0]:
            return
        column = target.column_at(target.mapFromScene(pos))
        if column is not None:
            self.add_join(Join(source[0].alias, target.alias, ((source[1], column),)))

    def add_join(self, join: Join):
        for j in self.design.joins:
            if {j.left, j.right} == {join.left, join.right}:
                same = set(j.pairs) if j.left == join.left else {(b, a) for a, b in j.pairs}
                if set(join.pairs) <= same:
                    self.message.emit("Those columns are already joined.")
                    return
        self.design.joins.append(join)
        a, b = join.pairs[0]
        self.message.emit(f"Joined {join.left}.{a} = {join.right}.{b}.")
        self._changed()

    def set_join_kind(self, join: Join, kind: str):
        join.kind = kind
        self._changed()

    def remove_join(self, join: Join):
        if join in self.design.joins:
            self.design.joins.remove(join)
            self._changed()

    def join_menu(self, join: Join, screen_pos):
        menu = QMenu(self)
        pairs = " AND ".join(f"{join.left}.{a} = {join.right}.{b}" for a, b in join.pairs)
        menu.addSection(pairs)
        for kind in JOIN_KINDS:
            action = menu.addAction(_JOIN_TEXT[kind].format(left=join.left, right=join.right),
                                    lambda k=kind: self.set_join_kind(join, k))
            action.setCheckable(True)
            action.setChecked(kind == join.kind)
        menu.addSeparator()
        menu.addAction("Delete join", lambda: self.remove_join(join))
        menu.exec(screen_pos)

    # --- outputs ------------------------------------------------------------------

    def toggle_output(self, alias: str, column: str):
        lowered = column.lower()
        if lowered in self.used_columns(alias):
            self.design.outputs = [o for o in self.design.outputs
                                   if not (o.alias == alias and o.column.lower() == lowered)]
        else:
            self.design.outputs.append(OutputColumn(alias, column))
        self._changed()

    def add_output(self, output: OutputColumn):
        self.design.outputs.append(output)
        self.side_tabs.setCurrentIndex(0)
        self._changed()

    def clear_outputs(self):
        self.design.outputs.clear()
        self._changed()

    def _move_output(self, index: int, step: int):
        outputs = self.design.outputs
        other = index + step
        if 0 <= other < len(outputs):
            outputs[index], outputs[other] = outputs[other], outputs[index]
            self._changed()

    def _remove_output(self, output: OutputColumn):
        self.design.outputs.remove(output)
        self._changed()

    def group_and_count(self, alias: str, column: str):
        if not any(o.alias == alias and o.column == column and not o.aggregate
                   for o in self.design.outputs):
            self.design.outputs.append(OutputColumn(alias, column))
        if not any(o.is_row_count for o in self.design.outputs):
            self.design.outputs.append(OutputColumn("", aggregate="COUNT", sort="DESC"))
        self.side_tabs.setCurrentIndex(0)
        self._changed()

    def select_all(self, alias: str):
        table = self.table(alias)
        used = self.used_columns(alias)
        for c in table.columns if table else []:
            if c.name.lower() not in used:
                self.design.outputs.append(OutputColumn(alias, c.name))
        self._changed()

    # --- menus --------------------------------------------------------------------

    def card_menu(self, card: TableCard, column: str | None, screen_pos):
        alias = card.alias
        menu = QMenu(self)
        if column:
            menu.addSection(column)
            listed = column.lower() in self.used_columns(alias)
            menu.addAction("Leave out of the result" if listed else "Add to the result",
                           lambda: self.toggle_output(alias, column))
            menu.addAction("Count distinct values",
                           lambda: self.add_output(OutputColumn(alias, column, "COUNT DISTINCT")))
            menu.addAction("Group by this and count rows",
                           lambda: self.group_and_count(alias, column))
        menu.addSection(f"{alias} ({card.v.design.ref(alias).table})")
        joins = menu.addMenu("Join to…")
        for other in self.design.tables:
            if other.alias == alias:
                continue
            for join in candidate_joins(self.schema, self.design.ref(alias), other):
                pairs = ", ".join(f"{a} = {other.alias}.{b}" for a, b in join.pairs)
                joins.addAction(f"{other.alias}: {pairs}", lambda j=join: self.add_join(j))
        if joins.isEmpty():
            joins.addAction("No foreign keys to the other tables").setEnabled(False)
        menu.addAction("Make the start table (FROM)", lambda: self.set_base(alias))
        menu.addAction("Add every column to the result", lambda: self.select_all(alias))
        if card.rows and card.rows[-1] is None:
            menu.addAction("Show fewer columns" if alias in self.expanded else "Show all columns",
                           lambda: self.toggle_expanded(alias))
        menu.addAction("Add another copy of this table",
                       lambda: self.add_table(card.v.design.ref(alias).table))
        menu.addAction("Remove from the query", lambda: self.remove_table(alias))
        menu.exec(screen_pos)

    # --- refreshing ---------------------------------------------------------------

    def _changed(self):
        """The design changed: redraw now (cards, edges) and the panels and
        SQL once the current event is done."""
        self._rebuild_cards()
        self._refresh_timer.start()

    def _rebuild_cards(self):
        scene = self.canvas.scene()
        aliases = [t.alias for t in self.design.tables if self.table(t.alias) is not None]
        for alias in list(self.cards):
            if alias not in aliases:
                scene.removeItem(self.cards.pop(alias))
        for alias in aliases:
            card = self.cards.get(alias)
            if card is None:
                card = TableCard(self, alias)
                card.setPos(self.positions.get(alias, QPointF(0, 0)))
                scene.addItem(card)
                self.cards[alias] = card
            else:
                card.refresh()
        for edge in self.edges:
            scene.removeItem(edge)
        self.edges = [JoinEdge(self, j, i) for j in self.design.joins
                      if j.left in self.cards and j.right in self.cards
                      for i in range(len(j.pairs))]
        for edge in self.edges:
            scene.addItem(edge)
        self.hint.setVisible(not self.cards)
        self.hint.setDefaultTextColor(QColor(_muted_hex(self)))

    def update_edges(self, alias: str):
        for edge in self.edges:
            if alias in (edge.join.left, edge.join.right):
                edge.update_path()

    def _start_changed(self):
        alias = self.start.currentData()
        if alias and alias != self.design.base:
            self.design.base = alias
            self._changed()

    def _options_changed(self, *_):
        d = self.design
        d.top = self.top.value()
        d.distinct = self.distinct.isChecked()
        d.latest_only = self.latest.isChecked()
        d.active_only = self.active.isChecked()
        self._refresh_timer.start()

    def _refresh_now(self):
        self._fill_start()
        self._fill_outputs()
        self._fill_joins()
        if self.schema is None or not self.design.tables:
            self._sql = ""
            self.sql_view.setPlainText("")
            self.sql_view.setPlaceholderText("The SELECT appears here as you add tables.")
        else:
            self._sql = generate_designed_select(self.design, self.schema, self.quote_all.isChecked())
            self.sql_view.setPlainText(self._sql)
        self.sql_changed.emit(self._sql)

    def _fill_start(self):
        self.start.blockSignals(True)
        self.start.clear()
        for ref in self.design.tables:
            label = ref.alias if ref.alias.lower() == ref.table.lower() else f"{ref.alias} ({ref.table})"
            self.start.addItem(label, ref.alias)
        index = self.start.findData(self.design.base)
        self.start.setCurrentIndex(max(index, 0) if self.design.tables else -1)
        self.start.blockSignals(False)
        # The options show the design's own values (Clear starts a new one).
        for widget, setter, value in (
            (self.top, self.top.setValue, self.design.top),
            (self.distinct, self.distinct.setChecked, self.design.distinct),
            (self.latest, self.latest.setChecked, self.design.latest_only),
            (self.active, self.active.setChecked, self.design.active_only),
        ):
            widget.blockSignals(True)
            setter(value)
            widget.blockSignals(False)

    def _fill_outputs(self):
        table = self.outputs
        table.blockSignals(True)  # filling isn't renaming
        table.setRowCount(len(self.design.outputs))
        for i, o in enumerate(self.design.outputs):
            source = "COUNT(*)" if o.is_row_count else f"{o.alias}.{o.column}"
            item = QTableWidgetItem(f"{source} AS {o.label}" if o.label else source)
            item.setToolTip(f"{source}. Double-click to name it in the result (AS …).")
            table.setItem(i, 0, item)
            agg = QComboBox()
            for value in (["COUNT"] if o.is_row_count else AGGREGATES):
                agg.addItem(value.title() if value else "Value", value)
            agg.setCurrentIndex(max(agg.findData(o.aggregate if not o.is_row_count else "COUNT"), 0))
            agg.currentIndexChanged.connect(
                lambda _i, o=o, box=agg: self._set_output(o, "aggregate", box.currentData()))
            sort = QComboBox()
            for text, value in _SORT_LABELS:
                sort.addItem(text, value)
            sort.setCurrentIndex(max(sort.findData(o.sort), 0))
            sort.currentIndexChanged.connect(
                lambda _i, o=o, box=sort: self._set_output(o, "sort", box.currentData()))
            buttons = QWidget()
            row = QHBoxLayout(buttons)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(2)
            for text, tip, slot in (("↑", "Move up", lambda _c=False, i=i: self._move_output(i, -1)),
                                    ("↓", "Move down", lambda _c=False, i=i: self._move_output(i, 1)),
                                    ("✕", "Remove", lambda _c=False, o=o: self._remove_output(o))):
                button = QToolButton()
                button.setText(text)
                button.setToolTip(tip)
                button.setAccessibleName(tip)
                button.clicked.connect(slot)
                row.addWidget(button)
            table.setCellWidget(i, 1, agg)
            table.setCellWidget(i, 2, sort)
            table.setCellWidget(i, 3, buttons)
        table.blockSignals(False)

    def _output_renamed(self, item: QTableWidgetItem):
        """The Column cell was edited: what's typed after the source is the AS name."""
        if item.column() != 0 or not 0 <= item.row() < len(self.design.outputs):
            return
        output = self.design.outputs[item.row()]
        source = "COUNT(*)" if output.is_row_count else f"{output.alias}.{output.column}"
        text = item.text().strip()
        if text.lower().startswith(source.lower()):
            text = text[len(source):].strip()
        if text.upper().startswith("AS "):
            text = text[3:].strip()
        self._set_output(output, "label", "" if text == source else text)
        self._refresh_timer.start()

    def _set_output(self, output: OutputColumn, field: str, value: str):
        if getattr(output, field) != value:
            setattr(output, field, value)
            card = self.cards.get(output.alias)
            if card is not None:
                card.update()
            self._refresh_timer.start()

    def _fill_joins(self):
        table = self.joins_table
        table.setRowCount(len(self.design.joins))
        for i, j in enumerate(self.design.joins):
            pairs = " AND ".join(f"{j.left}.{a} = {j.right}.{b}" for a, b in j.pairs)
            table.setItem(i, 0, QTableWidgetItem(pairs))
            kind = QComboBox()
            for value in JOIN_KINDS:
                kind.addItem(value.title(), value)
                kind.setItemData(kind.count() - 1,
                                 _JOIN_TEXT[value].format(left=j.left, right=j.right), Qt.ToolTipRole)
            kind.setCurrentIndex(max(kind.findData(j.kind), 0))
            kind.currentIndexChanged.connect(
                lambda _i, j=j, box=kind: self.set_join_kind(j, box.currentData()))
            remove = QToolButton()
            remove.setText("✕")
            remove.setToolTip("Delete this join")
            remove.setAccessibleName("Delete join")
            remove.clicked.connect(lambda _c=False, j=j: self.remove_join(j))
            table.setCellWidget(i, 1, kind)
            table.setCellWidget(i, 2, remove)
        table.resizeColumnToContents(1)

    def copy_sql(self):
        from PySide6.QtGui import QGuiApplication

        text = self.sql_text()
        if not text:
            self.message.emit("Add tables to the query first; there's no SQL yet.")
            return
        QGuiApplication.clipboard().setText(text)
        self.message.emit("Query SQL copied to clipboard.")
