"""The Query view's Filters tab: a tree of conditions and nested groups.

Each group combines what's in it with AND or OR, optionally negated; each
condition compares a column of a placed table with a typed value. Drag an
item by its ⠿ grip to reorder it or move it into another group. The widgets
edit the design's :class:`~xsltomermaid.query_model.Group` tree in place and
ask the view to rewrite the SQL; moving or adding items rebuilds the tree.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QMimeData, QPoint, Qt
from PySide6.QtGui import QDrag, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .query_model import (
    LIST_OPERATORS,
    NO_VALUE_OPERATORS,
    OPERATORS,
    Condition,
    Group,
)

if TYPE_CHECKING:
    from .query_designer import QueryDesignerView

MIME_FILTER = "application/x-xsltomermaid-filter-item"

# (label, mode, negate), in the order the group's box lists them.
GROUP_MODES = (
    ("All of these (AND)", "AND", False),
    ("Any of these (OR)", "OR", False),
    ("None of these (NOT … OR …)", "OR", True),
    ("Not all of these (NOT … AND …)", "AND", True),
)
_DEPTH_COLOURS = ("#2f81f7", "#2e9e57", "#a371f7", "#d9822b", "#db61a2")
_PLACEHOLDERS = {"in": "a, b, c", "not in": "a, b, c", "between": "from"}


def _mode_index(group: Group) -> int:
    for i, (_label, mode, negate) in enumerate(GROUP_MODES):
        if (mode, negate) == (group.mode, group.negate):
            return i
    return 0


def find_parent(root: Group, item) -> Group | None:
    """The group directly holding ``item`` (a Condition or Group), if any."""
    for child in root.items:
        if child is item:
            return root
        if isinstance(child, Group):
            found = find_parent(child, item)
            if found is not None:
                return found
    return None


def contains(group: Group, other: Group) -> bool:
    """True if ``other`` is ``group`` or nested anywhere inside it."""
    if group is other:
        return True
    return any(isinstance(c, Group) and contains(c, other) for c in group.items)


class _Grip(QLabel):
    """The ⠿ grip that drags a condition or group somewhere else."""

    def __init__(self, panel: "FiltersPanel", item):
        super().__init__("⠿")
        self.panel, self.item = panel, item
        self._start = None
        self.setCursor(Qt.OpenHandCursor)
        self.setToolTip("Drag to move: reorder, or drop into another group")
        font = self.font()
        font.setPointSizeF(font.pointSizeF() * 1.3)
        self.setFont(font)
        self.setFixedWidth(16)

    def mousePressEvent(self, event):  # noqa: N802 (Qt naming)
        if event.button() == Qt.LeftButton:
            self._start = event.position()

    def mouseMoveEvent(self, event):  # noqa: N802 (Qt naming)
        if self._start is not None and (
            (event.position() - self._start).manhattanLength() >= QApplication.startDragDistance()
        ):
            self._start = None
            self.panel.drag_item(self.item, self)

    def mouseReleaseEvent(self, event):  # noqa: N802 (Qt naming), ARG002
        self._start = None


class ConditionRow(QWidget):
    """``column  operator  value [and value2]  ✕``."""

    def __init__(self, panel: "FiltersPanel", condition: Condition):
        super().__init__()
        self.panel, self.condition = panel, condition
        self.column = QComboBox()
        self.column.setEditable(True)
        self.column.setInsertPolicy(QComboBox.NoInsert)
        self.column.completer().setFilterMode(Qt.MatchContains)
        self.column.completer().setCaseSensitivity(Qt.CaseInsensitive)
        self.column.setMinimumWidth(120)
        self.column.setAccessibleName("Filter column")
        choices = panel.view.column_choices()
        several = len({alias for alias, _c in choices}) > 1
        for alias, column in choices:
            # Item data is "alias<TAB>column": a plain string finds reliably
            # (findData), where a tuple doesn't.
            self.column.addItem(f"{column} ({alias})" if several else column, f"{alias}\t{column}")
            self.column.setItemData(self.column.count() - 1, f"{alias}.{column}", Qt.ToolTipRole)
        key = (condition.alias, condition.column)
        index = next((i for i, c in enumerate(choices)
                      if (c[0], c[1].lower()) == (key[0], key[1].lower())), -1)
        self.column.setCurrentIndex(index)
        self.column.currentIndexChanged.connect(self._column_changed)
        self.op = QComboBox()
        self.op.addItems(OPERATORS)
        self.op.setCurrentText(condition.op if condition.op in OPERATORS else "=")
        self.op.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.op.setAccessibleName("Filter operator")
        self.op.currentTextChanged.connect(self._op_changed)
        self.value = QLineEdit(condition.value)
        self.value.setAccessibleName("Filter value")
        # Wide enough for a date: the tree scrolls sideways rather than hide values.
        value_width = self.fontMetrics().horizontalAdvance("2026-12-31 23:59") + 16
        self.value.setMinimumWidth(value_width)
        self.value.textChanged.connect(self._value_changed)
        self.and_label = QLabel("and")
        self.value2 = QLineEdit(condition.value2)
        self.value2.setPlaceholderText("to")
        self.value2.setMinimumWidth(value_width)
        self.value2.setAccessibleName("Filter upper value")
        self.value2.textChanged.connect(self._value2_changed)
        remove = QToolButton()
        remove.setText("✕")
        remove.setToolTip("Remove this condition")
        remove.setAccessibleName("Remove condition")
        remove.clicked.connect(lambda: panel.remove_item(condition))
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(_Grip(panel, condition))
        row.addWidget(self.column, 3)
        row.addWidget(self.op)
        row.addWidget(self.value, 2)
        row.addWidget(self.and_label)
        row.addWidget(self.value2, 1)
        row.addWidget(remove)
        self._sync_value_boxes()

    def _sync_value_boxes(self):
        op = self.condition.op
        self.value.setVisible(op not in NO_VALUE_OPERATORS)
        self.and_label.setVisible(op == "between")
        self.value2.setVisible(op == "between")
        self.value.setPlaceholderText(_PLACEHOLDERS.get(op, "value"))
        tip = "Separate values with commas." if op in LIST_OPERATORS else ""
        self.value.setToolTip(tip)

    def _column_changed(self):
        data = self.column.currentData()
        if data:
            self.condition.alias, self.condition.column = data.split("\t", 1)
            self.panel.edited(columns=True)

    def _op_changed(self, op: str):
        self.condition.op = op
        self._sync_value_boxes()
        self.panel.edited()

    def _value_changed(self, text: str):
        self.condition.value = text
        self.panel.edited()

    def _value2_changed(self, text: str):
        self.condition.value2 = text
        self.panel.edited()


class GroupBox(QFrame):
    """A group: its combining mode and buttons, over its items (indented)."""

    def __init__(self, panel: "FiltersPanel", group: Group, depth: int = 0):
        super().__init__()
        self.panel, self.group, self.depth = panel, group, depth
        self.children_widgets: list[QWidget] = []
        self.setObjectName("filterGroup")
        self.setAcceptDrops(True)
        colour = _DEPTH_COLOURS[depth % len(_DEPTH_COLOURS)]
        self._style = f"QFrame#filterGroup {{ border: none; border-left: 3px solid {colour}; }}"
        self._drop_style = (f"QFrame#filterGroup {{ border: 1px dashed {colour};"
                            f" border-left: 3px solid {colour}; }}")
        self.setStyleSheet(self._style)
        self.marker = QFrame(self)
        self.marker.setObjectName("dropMarker")
        self.marker.setStyleSheet(f"QFrame#dropMarker {{ background: {colour}; border: none; }}")
        self.marker.hide()

        self.mode = QComboBox()
        for label, _mode, _negate in GROUP_MODES:
            self.mode.addItem(label)
        self.mode.setCurrentIndex(_mode_index(group))
        self.mode.setAccessibleName("Combine with")
        self.mode.currentIndexChanged.connect(self._mode_changed)
        add_condition = QPushButton("+ Condition")
        add_condition.setToolTip("Add a condition to this group")
        add_condition.clicked.connect(lambda: panel.view.add_filter(group=group))
        add_group = QPushButton("+ Group")
        add_group.setToolTip("Add a group inside this one, combined its own way")
        add_group.clicked.connect(lambda: panel.add_group(group))
        head = QHBoxLayout()
        if depth:
            head.addWidget(_Grip(panel, group))
        head.addWidget(self.mode)
        head.addWidget(add_condition)
        head.addWidget(add_group)
        head.addStretch()
        if depth:
            remove = QToolButton()
            remove.setText("✕")
            remove.setToolTip("Remove this group and everything in it")
            remove.setAccessibleName("Remove group")
            remove.clicked.connect(lambda: panel.remove_item(group))
            head.addWidget(remove)
        self.body = QVBoxLayout()
        self.body.setContentsMargins(12, 0, 0, 0)
        for item in group.items:
            widget = (GroupBox(panel, item, depth + 1) if isinstance(item, Group)
                      else ConditionRow(panel, item))
            self.children_widgets.append(widget)
            self.body.addWidget(widget)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 2, 4)
        layout.addLayout(head)
        layout.addLayout(self.body)

    def _mode_changed(self, index: int):
        _label, self.group.mode, self.group.negate = GROUP_MODES[index]
        self.panel.edited()

    # --- drops: moving items ---------------------------------------------------

    def _accepts(self, event) -> bool:
        item = self.panel.dragged
        return (event.mimeData().hasFormat(MIME_FILTER) and item is not None
                and not (isinstance(item, Group) and contains(item, self.group)))

    def insert_index(self, y: float) -> int:
        for i, widget in enumerate(self.children_widgets):
            if y < widget.geometry().center().y():
                return i
        return len(self.children_widgets)

    def _show_marker(self, index: int):
        widgets = self.children_widgets
        if index < len(widgets):
            y = widgets[index].geometry().top() - 3
        elif widgets:
            y = widgets[-1].geometry().bottom() + 2
        else:
            y = self.mode.geometry().bottom() + 3
        self.marker.setGeometry(16, y, max(20, self.width() - 22), 3)
        self.marker.show()
        self.marker.raise_()

    def dragEnterEvent(self, event):  # noqa: N802 (Qt naming)
        if self._accepts(event):
            self.setStyleSheet(self._drop_style)
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):  # noqa: N802 (Qt naming)
        if self._accepts(event):
            self._show_marker(self.insert_index(event.position().y()))
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):  # noqa: N802 (Qt naming), ARG002
        self.setStyleSheet(self._style)
        self.marker.hide()

    def dropEvent(self, event):  # noqa: N802 (Qt naming)
        self.setStyleSheet(self._style)
        self.marker.hide()
        if self._accepts(event):
            self.panel.move_item(self.panel.dragged, self.group,
                                 self.insert_index(event.position().y()))
            event.acceptProposedAction()
        else:
            event.ignore()


class _Holder(QWidget):
    """Under the tree: an item dropped on the empty space goes to the end of
    the top group."""

    def __init__(self, panel: "FiltersPanel"):
        super().__init__()
        self.panel = panel
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event):  # noqa: N802 (Qt naming)
        if event.mimeData().hasFormat(MIME_FILTER) and self.panel.dragged is not None:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):  # noqa: N802 (Qt naming)
        where = self.panel.view.design.where
        self.panel.move_item(self.panel.dragged, where, len(where.items))
        event.acceptProposedAction()


class FiltersPanel(QWidget):
    """The Filters tab: the design's WHERE tree, edited in place."""

    def __init__(self, view: "QueryDesignerView"):
        super().__init__()
        self.view = view
        self.dragged = None  # the Condition or Group being dragged
        self.root: GroupBox | None = None
        note = QLabel(
            "Filters become the WHERE clause. “+ Group” nests conditions combined their own "
            "way, e.g. Name = X AND (Total > 100 OR Status IN (1, 2)). Drag ⠿ to move one. "
            "Right-click a column on the canvas › Filter on this column."
        )
        note.setWordWrap(True)
        clear = QPushButton("Clear filters")
        clear.setToolTip("Remove every condition and group.")
        clear.clicked.connect(self.clear)
        top = QHBoxLayout()
        top.addWidget(note, 1)
        top.addWidget(clear, 0, Qt.AlignTop)
        self._holder = _Holder(self)
        self._holder_layout = QVBoxLayout(self._holder)
        self._holder_layout.setContentsMargins(0, 0, 0, 0)
        self._holder_layout.addStretch()
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setWidget(self._holder)
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.scroll, 1)
        self.rebuild()

    def rebuild(self):
        """Redraw the tree from the design (after a structural change)."""
        bar = self.scroll.verticalScrollBar().value()
        if self.root is not None:
            self.root.setParent(None)
            self.root.deleteLater()
        self.root = GroupBox(self, self.view.design.where)
        self._holder_layout.insertWidget(0, self.root)
        self.scroll.verticalScrollBar().setValue(bar)

    def rows(self) -> list[ConditionRow]:
        found: list[ConditionRow] = []

        def walk(box: GroupBox):
            for widget in box.children_widgets:
                if isinstance(widget, GroupBox):
                    walk(widget)
                else:
                    found.append(widget)

        if self.root is not None:
            walk(self.root)
        return found

    def edited(self, columns: bool = False):
        """A value changed in place: rewrite the SQL (and repaint the cards'
        filter marks when a condition's column changed)."""
        self.view.filters_edited(columns)

    def clear(self):
        self.view.design.where = Group()
        self.rebuild()
        self.edited(columns=True)

    def add_group(self, parent: Group):
        parent.items.append(Group())
        self.rebuild()
        self.edited()

    def remove_item(self, item):
        parent = find_parent(self.view.design.where, item)
        if parent is not None:
            parent.items.remove(item)
            self.rebuild()
            self.edited(columns=True)

    def move_item(self, item, target: Group, index: int):
        """Move a condition or group to ``target`` at ``index``; a group can't
        go inside itself."""
        source = find_parent(self.view.design.where, item)
        if source is None or (isinstance(item, Group) and contains(item, target)):
            return
        if source is target and source.items.index(item) < index:
            index -= 1
        source.items.remove(item)
        target.items.insert(max(0, min(index, len(target.items))), item)
        self.rebuild()
        self.edited()

    def drag_item(self, item, grip: QWidget):
        """Start dragging ``item`` from its grip."""
        widget = grip.parentWidget()
        if isinstance(item, Group):  # the grip sits in the group's header
            while widget is not None and not isinstance(widget, GroupBox):
                widget = widget.parentWidget()
        snap = widget.grab() if widget is not None else QPixmap()
        if snap.width() > 420:
            snap = snap.copy(0, 0, 420, snap.height())
        faded = QPixmap(snap.size())
        faded.fill(Qt.transparent)
        painter = QPainter(faded)
        painter.setOpacity(0.75)
        painter.drawPixmap(0, 0, snap)
        painter.end()
        mime = QMimeData()
        mime.setData(MIME_FILTER, b"1")
        drag = QDrag(grip)
        drag.setMimeData(mime)
        drag.setPixmap(faded)
        drag.setHotSpot(QPoint(8, 12))
        self.dragged = item
        if widget is not None:
            widget.setEnabled(False)  # dimmed while it's in the air
        try:
            drag.exec(Qt.MoveAction)
        finally:
            self.dragged = None
            if widget is not None:
                try:
                    widget.setEnabled(True)
                except RuntimeError:  # rebuilt (deleted) by the drop
                    pass
