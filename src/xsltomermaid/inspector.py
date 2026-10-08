"""The Details panel's Table view: one table, its columns and neighbours."""

from __future__ import annotations

from PySide6.QtCore import (
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QColor,
)
from PySide6.QtWidgets import (
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .excel_to_mermaid import (
    DiagramOptions,
    Table,
    drawn_columns,
)
from .schema_map import is_hidden_link
from .theme import (
    _link_hex,
    _muted_hex,
    _plural,
)


def _join_columns(rel) -> list[tuple[str, str]]:
    """The (table, column) pairs a relationship joins on, both sides."""
    child = rel.child_columns or tuple(c.strip() for c in rel.label.split(",") if c.strip())
    return [(rel.child_table, c) for c in child] + [(rel.parent_table, p) for p in rel.parent_columns]


class TableInspector(QWidget):
    """The Details panel's Table view: one table, its columns and neighbours.

    Shows the table picked in the diagram or the list. Column ticks change the
    diagram (shared with the Columns view); each foreign-key neighbour has an
    Add action, and double-clicking one moves the view to it. Dynamics' audit
    and system links are folded into their own collapsed group, since one
    table can have thousands of them.
    """

    column_toggled = Signal(str, str, bool)  # table, column, included
    add_requested = Signal(str)  # tick this table
    select_requested = Signal(str)  # move the view (and selection) here
    reference_picked = Signal(str, list)  # connected table ("" for none), [(table, column)] it joins on
    column_picked = Signal(str, str)  # (table, column) picked in the column list; ("", "") clears

    _ROLE_TABLE = Qt.UserRole
    _ROLE_JOIN = Qt.UserRole + 1  # [(table, column), ...] the link joins on
    _ROLE_FOLD = Qt.UserRole + 2  # the "Hidden by Keys only" group header
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
        self._columns.currentItemChanged.connect(self._on_column_current)
        self._columns.itemClicked.connect(self._on_fold_clicked)
        self._columns.itemActivated.connect(self._on_fold_clicked)
        self._fold = None
        self._fold_label = "Hidden by Keys only"
        self._hidden_items: list[QListWidgetItem] = []
        self._hidden_open = False  # remembered while the app runs
        self._links = QTreeWidget()
        self._links.setAccessibleName("Tables connected to the selected table")
        self._links.setColumnCount(2)
        self._links.setHeaderHidden(True)
        self._links.setRootIsDecorated(True)
        self._links.setUniformRowHeights(True)
        self._links.itemClicked.connect(self._on_link_clicked)
        self._links.itemActivated.connect(self._on_link_activated)
        self._links.currentItemChanged.connect(self._on_link_current)
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

    def show_table(
        self, table, outgoing, incoming, drawn: set[str], excluded: set,
        options: DiagramOptions | None = None, linked: set[str] | None = None,
    ):
        """Fill the view for ``table`` (None shows the empty state).

        ``outgoing`` / ``incoming`` are the table's relationships, ``drawn`` the
        lowercase names of tables in the diagram, ``excluded`` the hidden
        (table, column) pairs. ``options`` and ``linked`` (its FK columns whose
        table is drawn) say what Keys only leaves out, so the counts and ticks
        match the diagram.
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
        opts = options or DiagramOptions()
        included = [c for c in table.columns if (key, c.name.lower()) not in excluded]
        result = drawn_columns(Table(table.schema, table.name, included), opts, linked)
        on_canvas = {c.name.lower() for c in result.columns}
        self._title.setText(table.full_name)
        total = _plural(len(table.columns), "column")
        if key in drawn:
            count = f"{len(result.columns):,} of {total} drawn"
            if result.hidden:
                count += " (Keys only)" if opts.keys_only else " (system columns hidden)"
        else:
            count = f"{len(included):,} of {total} ticked · not in the diagram"
        self._meta.setText(
            f"{count} · references {_plural(len(outgoing), 'table')} · "
            f"referenced by {len(incoming):,}"
        )

        self._updating = True
        self._columns.clear()
        # Under Keys only, the drawn columns come first and the ticked-but-
        # hidden ones fold into one group below them (closed by default), so
        # the few drawn keys aren't lost among dozens of hidden columns.
        shown_items, hidden_items = [], []
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
            if key in drawn and not excluded_here and column.name.lower() not in on_canvas:
                # Ticked, but Keys only leaves it out: the group header says so.
                item.setForeground(QColor(_muted_hex(self)))
                item.setToolTip(
                    "Ticked, but Keys only leaves it out of the diagram."
                    if opts.keys_only
                    else "A Dynamics system column, hidden (More › Hide system columns)."
                )
                hidden_items.append(item)
            else:
                shown_items.append(item)
        for item in shown_items:
            self._columns.addItem(item)
        if hidden_items:
            self._fold = QListWidgetItem()
            self._fold.setData(self._ROLE_FOLD, True)
            self._fold.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            font = self._fold.font()
            font.setBold(True)
            self._fold.setFont(font)
            self._fold_label = (
                "Hidden by Keys only" if opts.keys_only else "Hidden system columns"
            )
            self._fold.setToolTip(
                (
                    "Ticked columns that Keys only leaves out of the diagram. "
                    if opts.keys_only
                    else "Dynamics system columns, left out of the diagram and SQL. "
                )
                + "Click (or Enter) to show or hide them."
            )
            self._columns.addItem(self._fold)
            for item in hidden_items:
                self._columns.addItem(item)
            self._hidden_items = hidden_items
            self._set_hidden_open(self._hidden_open)
        else:
            self._fold, self._hidden_items = None, []
        self._updating = False

        self._links.clear()
        # A new table: no column or reference is picked yet.
        self.column_picked.emit("", "")
        self.reference_picked.emit("", [])
        self._add_group("References", [(r.parent_table, r) for r in outgoing])
        self._add_group("Referenced by", [(r.child_table, r) for r in incoming])

    def _add_group(self, title: str, pairs):
        # The same rule as the map, the diagram and the link counts.
        regular = [(t, r) for t, r in pairs if not is_hidden_link(r)]
        audit = [(t, r) for t, r in pairs if is_hidden_link(r)]
        group = QTreeWidgetItem(self._links, [f"{title} ({len(pairs):,})"])
        group.setFirstColumnSpanned(True)
        group.setFlags(group.flags() & ~Qt.ItemIsSelectable)
        self._add_rows(group, regular)
        if audit:
            folded = QTreeWidgetItem(group, [f"Audit and system links ({len(audit):,})"])
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
            row.setData(0, self._ROLE_JOIN, _join_columns(rel))
            row.setToolTip(0, f"{other} (through {rel.label}). Double-click to show it here.")
            row.setToolTip(self._ADD_COL, "" if drawn else f"Tick {other} in the diagram")
            if not drawn:
                font = row.font(self._ADD_COL)
                font.setUnderline(True)
                row.setFont(self._ADD_COL, font)
                row.setForeground(self._ADD_COL, QColor(_link_hex(self)))

    def _set_hidden_open(self, open_: bool):
        self._hidden_open = open_
        if self._fold is None:
            return
        arrow = "▾" if open_ else "▸"
        self._fold.setText(f"{arrow}  {self._fold_label} ({len(self._hidden_items):,})")
        for item in self._hidden_items:
            item.setHidden(not open_)

    def _on_fold_clicked(self, item):
        if item is not None and item.data(self._ROLE_FOLD):
            self._set_hidden_open(not self._hidden_open)

    def _on_column_current(self, item, _prev=None):
        if self._updating:
            return
        if item is None or not self._table or item.data(Qt.UserRole) is None:
            self.column_picked.emit("", "")
        else:
            self.column_picked.emit(self._table, item.data(Qt.UserRole))

    def _on_link_current(self, item, _prev=None):
        other = item.data(0, self._ROLE_TABLE) if item is not None else None
        if not other:
            self.reference_picked.emit("", [])
        else:
            self.reference_picked.emit(other, list(item.data(0, self._ROLE_JOIN) or []))

    def select_column(self, name: str) -> bool:
        """Make ``name`` the current column (a row clicked in the diagram)."""
        for i in range(self._columns.count()):
            item = self._columns.item(i)
            if (item.data(Qt.UserRole) or "").lower() == name.lower():
                self._columns.setCurrentItem(item)
                self._columns.scrollToItem(item)
                return True
        return False

    def _on_column_changed(self, item):
        if self._updating or not self._table or item.data(Qt.UserRole) is None:
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
