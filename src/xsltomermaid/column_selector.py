"""The Details panel's Columns view: the selected table's columns."""

from __future__ import annotations

from PySide6.QtCore import (
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QColor,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .excel_to_mermaid import (
    DiagramOptions,
    Schema,
    Table,
    drawn_columns,
)
from .theme import (
    _muted_hex,
    _plural,
)


class _ColumnTree(QTreeWidget):
    """A flat, checkable column list whose rows can be dragged into order, or
    moved with Alt+Up / Alt+Down."""

    reordered = Signal()
    move_requested = Signal(int)  # -1 up, +1 down

    def dropEvent(self, event):  # noqa: N802 (Qt naming)
        super().dropEvent(event)
        self.reordered.emit()

    def keyPressEvent(self, event):  # noqa: N802 (Qt naming)
        if event.modifiers() == Qt.AltModifier and event.key() in (Qt.Key_Up, Qt.Key_Down):
            self.move_requested.emit(-1 if event.key() == Qt.Key_Up else 1)
            event.accept()
            return
        super().keyPressEvent(event)


class ColumnSelector(QWidget):
    """The selected table's columns: tick to show, sort or drag to order.

    Owns the set of *excluded* ``(table, column)`` pairs (columns are included
    unless unticked) and the column order, so both survive re-rendering and
    changing which tables are shown. The order is one sort for every table
    (original, name, type; optionally keys first), unless a table's rows were
    dragged: then that table keeps its own order until another sort is picked.
    """

    changed = Signal()  # the user changed which columns show, or their order
    column_picked = Signal(str, str)  # (table, column) highlighted; ("", "") clears

    _ROLE_NAME = Qt.UserRole  # the column's name
    _ROLE_KEY = Qt.UserRole + 1  # True for a PK or FK column
    _NAME, _TYPE, _KEYS, _NULL = range(4)
    _DRAGGED = "dragged"  # the sort value shown for a table with its own order

    def __init__(self, parent=None):
        super().__init__(parent)
        self._excluded: set[tuple[str, str]] = set()
        self._custom: dict[str, list[str]] = {}  # table key -> dragged column order
        self._table: Table | None = None
        self._shared_sort = 0  # the sort combo's index for undragged tables
        self._updating = False
        self._undo: tuple[str, set[tuple[str, str]]] | None = None  # (table, excluded)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self._title = QLabel()
        self._title.setStyleSheet("font-weight: 600;")
        self._title.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._meta = QLabel()
        self._meta.setWordWrap(True)
        self._empty = QLabel(
            "Select a table in the diagram, the map, the list or the Tables view "
            "to choose its columns."
        )
        self._empty.setWordWrap(True)
        self._empty.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        layout.addWidget(self._title)
        layout.addWidget(self._meta)
        layout.addWidget(self._empty)

        self._body = QWidget()
        body = QVBoxLayout(self._body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(6)
        layout.addWidget(self._body, 1)

        sort_row = QHBoxLayout()
        self._sort = QComboBox()
        sort_label = QLabel("Sort:")
        sort_label.setBuddy(self._sort)
        self._sort.setAccessibleName("Sort columns")
        self._sort.setToolTip(
            "The order columns are listed and drawn in. Drag rows to give this "
            "table its own order."
        )
        for label, value in (
            ("Original order", "order"),
            ("Name A–Z", "name"),
            ("Name Z–A", "name_desc"),
            ("Data type", "type"),
        ):
            self._sort.addItem(label, value)
        self._sort.currentIndexChanged.connect(self._on_sort_changed)
        self._keys_first = QCheckBox("PK, FK first")
        self._keys_first.setToolTip(
            "Place primary-key columns, then foreign-key columns, above the "
            "other columns."
        )
        self._keys_first.toggled.connect(self._on_keys_first_changed)
        sort_row.addWidget(sort_label)
        sort_row.addWidget(self._sort)
        sort_row.addWidget(self._keys_first)
        sort_row.addStretch(1)
        body.addLayout(sort_row)

        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Filter columns…")
        self._filter.setAccessibleName("Filter columns")
        self._filter.setClearButtonEnabled(True)
        self._filter.textChanged.connect(self._apply_filter_text)
        body.addWidget(self._filter)

        self._tree = _ColumnTree()
        self._tree.setAccessibleName("Columns of the selected table")
        self._tree.setColumnCount(4)
        self._tree.setHeaderLabels(["Column", "Type", "Key", "Null"])
        self._tree.setRootIsDecorated(False)
        self._tree.setUniformRowHeights(True)
        self._tree.setDragDropMode(QAbstractItemView.InternalMove)
        self._tree.setDefaultDropAction(Qt.MoveAction)
        self._tree.setDragDropOverwriteMode(False)
        self._tree.headerItem().setToolTip(self._NULL, "Yes: the column can be NULL.")
        header = self._tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(self._NAME, QHeaderView.Stretch)
        for section in (self._TYPE, self._KEYS, self._NULL):
            header.setSectionResizeMode(section, QHeaderView.ResizeToContents)
        self._tree.itemChanged.connect(self._on_item_changed)
        self._tree.currentItemChanged.connect(self._on_current_changed)
        self._tree.reordered.connect(self._on_reordered)
        self._tree.move_requested.connect(self._move)
        self._tree.currentItemChanged.connect(lambda *_a: self._sync_move_buttons())
        body.addWidget(self._tree, 1)

        button_row = QHBoxLayout()
        for label, mode, tip in (
            ("All", "all", "Show every column of this table."),
            ("None", "none", "Leave out every column of this table."),
            ("Keys only", "keys", "Show only this table's primary and foreign keys."),
        ):
            button = QPushButton(label)
            button.setToolTip(tip)
            button.clicked.connect(lambda _c=False, m=mode: self._bulk(m))
            button_row.addWidget(button)
        # All / None / Keys only rewrite the whole table, so the previous
        # choice is kept for one undo.
        self._undo_btn = QPushButton("Undo")
        self._undo_btn.setToolTip("Restore this table's columns from before the last button.")
        self._undo_btn.clicked.connect(self._undo_bulk)
        self._undo_btn.setVisible(False)
        button_row.addWidget(self._undo_btn)
        button_row.addStretch(1)
        # The same moves as dragging, for the keyboard and a single click.
        self._move_btns = []
        for arrow, step, name in ((Qt.UpArrow, -1, "Move up"), (Qt.DownArrow, 1, "Move down")):
            button = QToolButton()
            button.setArrowType(arrow)
            button.setAccessibleName(f"{name}: the selected column")
            button.setToolTip(f"{name} (Alt+{'Up' if step < 0 else 'Down'})")
            button.clicked.connect(lambda _c=False, s=step: self._move(s))
            button_row.addWidget(button)
            self._move_btns.append(button)
        body.addLayout(button_row)

        self._hint = QLabel(
            "Drag rows, or Alt+Up / Alt+Down, to reorder. Click a column to tint "
            "its row in the diagram."
        )
        self._hint.setWordWrap(True)
        body.addWidget(self._hint)
        self.retheme()
        self.show_table(None)

    # -- the selected table -------------------------------------------------
    def current_table(self) -> str:
        return self._table.name if self._table is not None else ""

    def show_table(
        self, table: Table | None, drawn: bool = False,
        options: DiagramOptions | None = None, linked: set[str] | None = None,
    ):
        """Fill the view for ``table`` (None shows the empty state).

        ``drawn`` says whether it's in the diagram; ``options`` and ``linked``
        (its FK columns whose table is drawn) say which ticked columns Keys
        only or Hide system columns leave out, so those are greyed.
        """
        new = self._table is None or table is None or table.name != self._table.name
        if new:
            self._undo = None
            self._undo_btn.setVisible(False)
        self._table = table
        has = table is not None
        for widget in (self._title, self._meta, self._body):
            widget.setVisible(has)
        self._empty.setVisible(not has)
        if not has:
            self._tree.clear()
            self.column_picked.emit("", "")
            return

        key = table.name.lower()
        opts = options or DiagramOptions()
        included = [c for c in table.columns if (key, c.name.lower()) not in self._excluded]
        result = drawn_columns(Table(table.schema, table.name, included), opts, linked)
        on_canvas = {c.name.lower() for c in result.columns}
        self._title.setText(table.full_name)
        total = _plural(len(table.columns), "column")
        if drawn:
            meta = f"{len(result.columns):,} of {total} drawn"
            if result.hidden:
                why = "Keys only" if opts.keys_only else "Hide system columns"
                meta += f" · {result.hidden:,} ticked but hidden by {why} (greyed)"
        else:
            meta = f"{len(included):,} of {total} ticked · not in the diagram"
        self._meta.setText(meta)

        self._sync_sort_controls()
        muted = QColor(_muted_hex(self))
        self._updating = True
        current = None if new else self._tree.currentItem()
        current_name = current.data(self._NAME, self._ROLE_NAME) if current else None
        self._tree.clear()
        for column in self.ordered_columns(table):
            marks = [m for m, on in (("PK", column.is_primary_key),
                                     ("FK", bool(column.foreign_key_reference))) if on]
            item = QTreeWidgetItem([
                column.name, column.rendered_type(), ", ".join(marks),
                "Yes" if column.is_nullable else "No",
            ])
            # Checkable and draggable, but nothing drops *onto* a row (that
            # would nest it): drops land between rows.
            item.setFlags(
                (item.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsDragEnabled)
                & ~Qt.ItemIsDropEnabled
            )
            item.setData(self._NAME, self._ROLE_NAME, column.name)
            item.setData(self._NAME, self._ROLE_KEY, bool(marks))
            if column.foreign_key_reference:
                item.setToolTip(self._KEYS, f"References {column.foreign_key_reference}")
            excluded = (key, column.name.lower()) in self._excluded
            item.setCheckState(self._NAME, Qt.Unchecked if excluded else Qt.Checked)
            if drawn and not excluded and column.name.lower() not in on_canvas:
                # Greyed and italic (not colour alone), and named for screen readers.
                why = "Keys only" if opts.keys_only else "Hide system columns"
                font = item.font(self._NAME)
                font.setItalic(True)
                for section in range(4):
                    item.setForeground(section, muted)
                    item.setFont(section, font)
                item.setData(self._NAME, Qt.AccessibleTextRole, f"{column.name}, hidden by {why}")
                item.setToolTip(
                    self._NAME,
                    "Ticked, but Keys only leaves it out of the diagram."
                    if opts.keys_only
                    else "A Dynamics system column, hidden (More › Hide system columns).",
                )
            self._tree.addTopLevelItem(item)
            if current_name and column.name == current_name:
                self._tree.setCurrentItem(item)
        self._updating = False
        self._apply_filter_text(self._filter.text())
        self._sync_move_buttons()
        if new:  # a new table: no column is picked yet
            self.column_picked.emit("", "")

    def select_column(self, name: str) -> bool:
        """Make ``name`` the current column (a row clicked in the diagram)."""
        for item in self._rows():
            if (item.data(self._NAME, self._ROLE_NAME) or "").lower() == name.lower():
                self._tree.setCurrentItem(item)
                self._tree.scrollToItem(item)
                return True
        return False

    # -- order ---------------------------------------------------------------
    def ordered_columns(self, table: Table):
        columns = list(table.columns)
        custom = self._custom.get(table.name.lower())
        if custom is not None:
            rank = {name: i for i, name in enumerate(custom)}
            # Columns the dragged order doesn't know keep their place at the end.
            return sorted(columns, key=lambda c: rank.get(c.name.lower(), len(rank) + c.order))
        mode = self._sort.itemData(self._sort_index())
        if mode in ("name", "name_desc"):
            columns.sort(key=lambda c: c.name.casefold(), reverse=mode == "name_desc")
        elif mode == "type":
            columns.sort(key=lambda c: (c.data_type.casefold(), c.name.casefold()))
        # Stable grouping keeps the chosen order within each key group.
        if self._keys_first.isChecked():
            columns.sort(
                key=lambda c: 0 if c.is_primary_key else 1 if c.foreign_key_reference else 2
            )
        return columns

    def sorted_schema(self, schema: Schema) -> Schema:
        return Schema(
            tables=[Table(t.schema, t.name, self.ordered_columns(t)) for t in schema.tables],
            relationships=schema.relationships,
        )

    def _sort_index(self) -> int:
        """The shared sort's index (the "Dragged order" entry isn't one)."""
        index = self._sort.currentIndex()
        return self._shared_sort if self._sort.itemData(index) == self._DRAGGED else index

    def _sync_sort_controls(self):
        """Show "Dragged order" (and grey PK, FK first) for a dragged table."""
        dragged = self._table is not None and self._table.name.lower() in self._custom
        at = self._sort.findData(self._DRAGGED)
        self._sort.blockSignals(True)
        if dragged and at < 0:
            self._sort.addItem("Dragged order", self._DRAGGED)
            at = self._sort.count() - 1
        if dragged:
            self._sort.setCurrentIndex(at)
        else:
            if at >= 0:
                self._sort.removeItem(at)
            self._sort.setCurrentIndex(self._shared_sort)
        self._sort.blockSignals(False)
        self._keys_first.setEnabled(not dragged)
        self._keys_first.setToolTip(
            "This table has its own dragged order; pick a sort to use the shared one."
            if dragged
            else "Place primary-key columns, then foreign-key columns, above the "
            "other columns."
        )

    def _on_sort_changed(self, index: int):
        if self._sort.itemData(index) == self._DRAGGED:
            return
        self._shared_sort = index
        if self._table is not None:
            self._custom.pop(self._table.name.lower(), None)  # picking a sort replaces the drag
        self._redraw_rows()
        self.changed.emit()

    def _on_keys_first_changed(self, _on: bool):
        self._redraw_rows()
        self.changed.emit()

    def _move(self, step: int):
        """Move the current row up (-1) or down (+1), as a drag would."""
        item = self._tree.currentItem()
        if item is None or not self._tree.dragEnabled():
            return
        row = self._tree.indexOfTopLevelItem(item)
        target = row + step
        if not 0 <= target < self._tree.topLevelItemCount():
            return
        self._updating = True
        self._tree.insertTopLevelItem(target, self._tree.takeTopLevelItem(row))
        self._tree.setCurrentItem(item)
        self._updating = False
        self._tree.scrollToItem(item)
        self._on_reordered()
        self._sync_move_buttons()

    def _sync_move_buttons(self):
        item = self._tree.currentItem()
        row = self._tree.indexOfTopLevelItem(item) if item is not None else -1
        movable = row >= 0 and self._tree.dragEnabled()
        self._move_btns[0].setEnabled(movable and row > 0)
        self._move_btns[1].setEnabled(movable and row < self._tree.topLevelItemCount() - 1)

    def _on_reordered(self):
        if self._table is None:
            return
        self._custom[self._table.name.lower()] = [
            item.data(self._NAME, self._ROLE_NAME).lower() for item in self._rows()
        ]
        self._sync_sort_controls()
        self.changed.emit()

    def _redraw_rows(self):
        """Put the rows in the new order. They're moved, not rebuilt, so
        greying, ticks and the current row stay as they are."""
        if self._table is None:
            return
        order = {c.name: i for i, c in enumerate(self.ordered_columns(self._table))}
        self._updating = True
        current = self._tree.currentItem()
        items = [self._tree.takeTopLevelItem(0) for _ in range(self._tree.topLevelItemCount())]
        items.sort(key=lambda it: order.get(it.data(self._NAME, self._ROLE_NAME), len(order)))
        self._tree.addTopLevelItems(items)
        if current is not None:
            self._tree.setCurrentItem(current)
        self._updating = False
        self._sync_sort_controls()

    # -- ticks ---------------------------------------------------------------
    def _on_item_changed(self, item, column=0):
        if self._updating or column != self._NAME or self._table is None:
            return
        pair = (self._table.name.lower(), item.data(self._NAME, self._ROLE_NAME).lower())
        if item.checkState(self._NAME) == Qt.Checked:
            self._excluded.discard(pair)
        else:
            self._excluded.add(pair)
        self.changed.emit()

    def _on_current_changed(self, item, _prev=None):
        if self._updating:
            return
        if item is None or self._table is None:
            self.column_picked.emit("", "")
        else:
            self.column_picked.emit(self._table.name, item.data(self._NAME, self._ROLE_NAME))

    def _bulk(self, mode: str):
        if self._table is None:
            return
        self._undo = (self._table.name, set(self._excluded))
        self._undo_btn.setText(
            {"all": "Undo all", "none": "Undo none", "keys": "Undo keys only"}[mode]
        )
        self._undo_btn.setVisible(True)
        key = self._table.name.lower()
        for column in self._table.columns:
            is_key = bool(column.is_primary_key or column.foreign_key_reference)
            keep = mode == "all" or (mode == "keys" and is_key)
            if keep:
                self._excluded.discard((key, column.name.lower()))
            else:
                self._excluded.add((key, column.name.lower()))
        self._sync_checks()
        self.changed.emit()

    def _undo_bulk(self):
        if self._undo is None:
            return
        _table, self._excluded = self._undo
        self._undo = None
        self._undo_btn.setVisible(False)
        self._sync_checks()
        self.changed.emit()

    def _sync_checks(self):
        """Update the rows' ticks to match the excluded set."""
        if self._table is None:
            return
        key = self._table.name.lower()
        self._updating = True
        for item in self._rows():
            pair = (key, item.data(self._NAME, self._ROLE_NAME).lower())
            item.setCheckState(
                self._NAME, Qt.Unchecked if pair in self._excluded else Qt.Checked
            )
        self._updating = False

    def set_column_included(self, table: str, column: str, included: bool):
        """Include or leave out one column."""
        pair = (table.lower(), column.lower())
        if (pair not in self._excluded) == included:
            return
        if included:
            self._excluded.discard(pair)
        else:
            self._excluded.add(pair)
        self._sync_checks()
        self.changed.emit()

    def excluded_pairs(self) -> set[tuple[str, str]]:
        return set(self._excluded)

    # -- helpers -------------------------------------------------------------
    def _apply_filter_text(self, text: str):
        needle = text.strip().lower()
        for item in self._rows():
            item.setHidden(needle not in item.text(self._NAME).lower())
        # Moving among a filtered few would scramble the hidden rows' places.
        self._tree.setDragEnabled(not needle)
        if hasattr(self, "_move_btns"):
            self._sync_move_buttons()

    def _rows(self):
        return [self._tree.topLevelItem(i) for i in range(self._tree.topLevelItemCount())]

    def retheme(self):
        muted = f"color: {_muted_hex(self)};"
        for label in (self._meta, self._empty, self._hint):
            label.setStyleSheet(muted)
