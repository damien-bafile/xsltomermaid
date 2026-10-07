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

from .excel_to_mermaid import is_audit_relationship
from .theme import (
    _link_hex,
    _muted_hex,
    _plural,
)


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
            f"{shown:,} of {_plural(len(table.columns), 'column')} shown · "
            f"references {_plural(len(outgoing), 'table')} · "
            f"referenced by {len(incoming):,} · {in_diagram}"
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
