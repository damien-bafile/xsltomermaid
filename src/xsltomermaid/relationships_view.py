"""The Details panel's Links view: the tables the selected one links to."""

from __future__ import annotations

from collections import defaultdict

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
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .schema_map import is_hidden_link
from .theme import (
    _link_hex,
    _muted_hex,
    _plural,
)

IN_DIAGRAM = "●"  # the same marker as the table list's "in the diagram"


def _join_columns(rel) -> list[tuple[str, str]]:
    """The (table, column) pairs a relationship joins on, both sides."""
    child = rel.child_columns or tuple(c.strip() for c in rel.label.split(",") if c.strip())
    return [(rel.child_table, c) for c in child] + [(rel.parent_table, p) for p in rel.parent_columns]


def link_index(relationships) -> dict[str, list[tuple[str, object]]]:
    """``{table: [(linked table, relationship)]}`` both ways, without audit
    and system links (they reach nearly every table)."""
    links: dict[str, list[tuple[str, object]]] = defaultdict(list)
    for rel in relationships:
        if is_hidden_link(rel):
            continue
        links[rel.child_table.lower()].append((rel.parent_table, rel))
        links[rel.parent_table.lower()].append((rel.child_table, rel))
    return links


def extended_relations(relationships, name: str, links=None) -> dict[str, list[tuple[str, object]]]:
    """Tables two links from ``name``: ``{through table: [(table, relationship)]}``.

    Either direction on both links. Leaves out the tables ``name`` already
    links to directly, ``name`` itself, and audit and system links (through
    them everything would be "extended"). ``links`` is a ready
    :func:`link_index` of ``relationships``.
    """
    key = name.lower()
    if links is None:
        links = link_index(relationships)
    direct = {other.lower() for other, _r in links.get(key, [])}
    result: dict[str, list[tuple[str, object]]] = {}
    for via in sorted({o for o, _r in links.get(key, []) if o.lower() != key}, key=str.lower):
        found = {}
        for other, rel in links.get(via.lower(), []):
            low = other.lower()
            if low != key and low not in direct and low != via.lower():
                found.setdefault((low, rel.label.lower()), (other, rel))
        if found:
            result[via] = sorted(found.values(), key=lambda p: (p[0].lower(), p[1].label.lower()))
    return result


def link_summary(name: str, relationships) -> tuple[list, list, str]:
    """(outgoing, incoming, summary line) for ``name``, counted like the
    table list: audit and system links apart, and a link from the table to
    itself once, as outgoing."""
    key = name.lower()
    outgoing = [r for r in relationships if r.child_table.lower() == key]
    incoming = [
        r for r in relationships
        if r.parent_table.lower() == key and r.child_table.lower() != key
    ]
    out = [r for r in outgoing if not is_hidden_link(r)]
    in_ = [r for r in incoming if not is_hidden_link(r)]
    out_tables = len({r.parent_table.lower() for r in out})
    in_tables = len({r.child_table.lower() for r in in_})
    line = (
        f"↗ {_plural(len(out), 'foreign key')} to {_plural(out_tables, 'table')} · "
        f"↙ {len(in_):,} from {_plural(in_tables, 'table')}"
    )
    audit = len(outgoing) + len(incoming) - len(out) - len(in_)
    if audit:
        line += f" · {audit:,} audit and system links folded below"
    return outgoing, incoming, line


class _LinkTree(QTreeWidget):
    """The links tree. Space adds the current row's table: the tree's own
    Space handling would otherwise swallow it."""

    add_current = Signal()

    def keyPressEvent(self, event):  # noqa: N802 (Qt naming)
        if event.key() == Qt.Key_Space and not event.modifiers():
            self.add_current.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class RelationshipsView(QWidget):
    """The selected table's foreign keys, both ways, and optionally the tables
    one more link away (through another table).

    Each row has an Add action that ticks its table (for a table reached
    through another, both); double-click or Enter moves the view to it.
    Dynamics' audit and system links are folded into their own collapsed
    group, since one table can have thousands of them.
    """

    add_requested = Signal(list)  # tick these tables
    select_requested = Signal(str)  # move the view (and selection) here
    reference_picked = Signal(str, list)  # connected table ("" for none), [(table, column)] it joins on

    _ROLE_TABLE = Qt.UserRole
    _ROLE_JOIN = Qt.UserRole + 1  # [(table, column), ...] the link joins on
    _ROLE_ADD = Qt.UserRole + 2  # [table, ...] the Add action ticks
    _ADD_COL = 1
    _LAZY_FROM = 50  # a folded group this big gets its rows when first opened

    def __init__(self, parent=None):
        super().__init__(parent)
        self._table = ""
        self._title_text = ""
        self._drawn: set[str] = set()
        self._relationships: list = []
        self._index: tuple[int, dict] | None = None  # (id of relationships, link_index)
        self._pending: dict[int, tuple] = {}  # folded group -> (rows, via) to add on opening
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self._title = QLabel()
        self._title.setStyleSheet("font-weight: 600;")
        self._title.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._meta = QLabel()
        self._meta.setWordWrap(True)
        self._empty = QLabel()
        self._empty.setWordWrap(True)
        self._empty.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self._extended = QCheckBox("Also show tables linked through another table")
        self._extended.setToolTip(
            "Also list the tables one more link away, grouped by the table "
            "between. Audit and system links are left out."
        )
        self._extended.toggled.connect(self._on_extended_toggled)
        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Filter linked tables…")
        self._filter.setAccessibleName("Filter linked tables")
        self._filter.setClearButtonEnabled(True)
        self._filter.textChanged.connect(self._apply_filter_text)
        for widget in (self._title, self._meta, self._extended, self._filter, self._empty):
            layout.addWidget(widget)

        self._links = _LinkTree()
        self._links.setAccessibleName("Tables linked to the selected table")
        self._links.setColumnCount(2)
        self._links.setHeaderLabels(["Table · column", "Add"])
        self._links.setHeaderHidden(True)
        self._links.setRootIsDecorated(True)
        self._links.setUniformRowHeights(True)
        self._links.setTextElideMode(Qt.ElideMiddle)  # keep the table and the column's end
        self._links.itemClicked.connect(self._on_link_clicked)
        self._links.itemActivated.connect(self._on_link_activated)
        self._links.currentItemChanged.connect(self._on_link_current)
        self._links.itemExpanded.connect(self._fill_group)
        self._links.add_current.connect(self._add_current)
        self._links.setContextMenuPolicy(Qt.CustomContextMenu)
        self._links.customContextMenuRequested.connect(self._menu)
        header = self._links.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        layout.addWidget(self._links, 1)
        self._hint = QLabel(
            f"Double-click or Enter moves to a table · Add (or Space) ticks it · "
            f"{IN_DIAGRAM} in the diagram"
        )
        self._hint.setWordWrap(True)
        layout.addWidget(self._hint)
        self.retheme()
        self.show_table("", [], set())

    def current_table(self) -> str:
        return self._table

    def show_table(self, name: str, relationships, drawn: set[str], title: str = ""):
        """Fill the view for table ``name`` ("" shows nothing).

        ``relationships`` are the whole schema's; ``drawn`` the lowercase
        names of the tables in the diagram; ``title`` the name to show
        (schema-qualified, like the Columns view).
        """
        self._table = name
        self._title_text = title or name
        self._relationships = relationships
        self._drawn = drawn
        self._fill()

    def _fill(self):
        self._links.clear()
        self._pending.clear()
        self.reference_picked.emit("", [])
        name = self._table
        outgoing, incoming, summary = link_summary(name, self._relationships)
        has_links = bool(outgoing or incoming)
        self._title.setVisible(bool(name))
        self._meta.setVisible(bool(name))
        for widget in (self._extended, self._filter, self._links, self._hint):
            widget.setVisible(has_links)
        self._empty.setVisible(not has_links)
        if not name:
            self._empty.setText(
                "Select a table (in the diagram, the map, the list or the Tables "
                "view) to see the tables it's linked to."
            )
            return
        self._title.setText(self._title_text)
        if not has_links:
            self._empty.setText(
                f"{name} has no relationships: it has no foreign keys, and no "
                "table has a foreign key to it."
            )
            return
        self._add_group("Has foreign keys to", [(r.parent_table, r) for r in outgoing])
        self._add_group("Targeted by foreign keys from", [(r.child_table, r) for r in incoming])
        if self._extended.isChecked():
            if self._index is None or self._index[0] != id(self._relationships):
                self._index = (id(self._relationships), link_index(self._relationships))
            by_via = extended_relations(self._relationships, name, self._index[1])
            targets = len({o.lower() for rows in by_via.values() for o, _r in rows})
            summary += f" · {targets:,} more through another table"
            self._add_extended(by_via, targets)
        self._meta.setText(summary)
        if self._filter.text().strip():
            self._apply_filter_text(self._filter.text())

    def _on_extended_toggled(self, on: bool):
        self._fill()
        if on:  # it lands below the direct links: bring it into view
            last = self._links.topLevelItem(self._links.topLevelItemCount() - 1)
            if last is not None:
                self._links.scrollToItem(last, QAbstractItemView.PositionAtTop)

    def _add_group(self, title: str, pairs):
        # The same rule as the map, the diagram and the link counts.
        regular = [(t, r) for t, r in pairs if not is_hidden_link(r)]
        audit = [(t, r) for t, r in pairs if is_hidden_link(r)]
        group = self._group(self._links, f"{title} ({len(regular):,})")
        if not regular and not audit:
            self._note(group, "None")
        self._add_rows(group, regular)
        if audit:
            folded = self._group(group, f"Audit and system links ({len(audit):,})")
            self._add_folded(folded, audit)
        group.setExpanded(True)

    def _add_extended(self, by_via: dict, targets: int):
        group = self._group(self._links, f"Through another table ({targets:,})")
        if not by_via:
            self._note(group, "None: every linked table links only back here.")
        for via, rows in by_via.items():
            sub = self._group(group, f"through {via} ({len(rows):,})")
            if len(by_via) == 1:
                self._add_rows(sub, rows, via=via)
                sub.setExpanded(True)
            else:
                self._add_folded(sub, rows, via)
        group.setExpanded(True)

    @staticmethod
    def _note(parent, text: str):
        note = QTreeWidgetItem(parent, [text])
        note.setFirstColumnSpanned(True)
        note.setFlags(Qt.ItemIsEnabled)

    def _add_folded(self, group, rows, via: str = ""):
        """Rows for a closed group: built now if few, else when first opened
        (a Dynamics hub has thousands of audit links)."""
        if len(rows) < self._LAZY_FROM:
            self._add_rows(group, rows, via=via)
        else:
            self._pending[id(group)] = (rows, via)
            group.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
        group.setExpanded(False)

    def _fill_group(self, group):
        pending = self._pending.pop(id(group), None)
        if pending is not None:
            rows, via = pending
            self._add_rows(group, rows, via=via)

    @staticmethod
    def _group(parent, text: str):
        group = QTreeWidgetItem(parent, [text])
        group.setFirstColumnSpanned(True)
        group.setFlags(group.flags() & ~Qt.ItemIsSelectable)
        if isinstance(parent, QTreeWidget):  # top-level groups only: a wall of bold reads as none
            font = group.font(0)
            font.setBold(True)  # the tree's structure survives scrolling
            group.setFont(0, font)
        return group

    def _add_rows(self, parent, pairs, via: str = ""):
        muted = QColor(_muted_hex(self))
        link = QColor(_link_hex(self))
        for other, rel in sorted(pairs, key=lambda p: (p[0].lower(), p[1].label.lower())):
            to_add = [t for t in ([via, other] if via else [other]) if t.lower() not in self._drawn]
            # "via" is for columns only: the row is "table · column".
            itself = other.lower() == self._table.lower() and not via
            shown = f"{other} (itself)" if itself else other
            row = QTreeWidgetItem(parent, [f"{shown}  ·  {rel.label}", "Add" if to_add else IN_DIAGRAM])
            row.setData(0, self._ROLE_TABLE, other)
            # A table reached through another has no join with this one.
            row.setData(0, self._ROLE_JOIN, [] if via else _join_columns(rel))
            row.setData(0, self._ROLE_ADD, to_add)
            row.setToolTip(0, f"{shown}, through the {rel.label} column. Double-click or Enter to move to it.")
            row.setData(0, Qt.AccessibleTextRole, f"{shown}, column {rel.label}")
            if to_add:
                row.setToolTip(self._ADD_COL, f"Tick {' and '.join(to_add)} in the diagram (Space)")
                row.setData(
                    self._ADD_COL, Qt.AccessibleTextRole,
                    f"Add {' and '.join(to_add)} to the diagram (Space)",
                )
                font = row.font(self._ADD_COL)
                font.setUnderline(True)
                row.setFont(self._ADD_COL, font)
                row.setForeground(self._ADD_COL, link)
            else:
                row.setToolTip(self._ADD_COL, "In the diagram")
                row.setData(self._ADD_COL, Qt.AccessibleTextRole, "In the diagram")
                row.setForeground(self._ADD_COL, muted)
                row.setTextAlignment(self._ADD_COL, Qt.AlignCenter)

    def _apply_filter_text(self, text: str):
        """Show the rows whose table or column matches; folded groups with a
        match are built and opened."""
        needle = text.strip().lower()

        def walk(item) -> bool:
            if item.data(0, self._ROLE_TABLE) is not None:  # a row
                hit = not needle or needle in item.text(0).lower()
                item.setHidden(not hit)
                return hit
            if needle and id(item) in self._pending:
                self._fill_group(item)
            any_hit = False
            for i in range(item.childCount()):
                any_hit = walk(item.child(i)) or any_hit
            if item.parent() is not None or item.childCount():
                item.setHidden(bool(needle) and not any_hit)
            if needle and any_hit:
                item.setExpanded(True)
            return any_hit

        for i in range(self._links.topLevelItemCount()):
            walk(self._links.topLevelItem(i))

    def _on_link_current(self, item, _prev=None):
        other = item.data(0, self._ROLE_TABLE) if item is not None else None
        if not other:
            self.reference_picked.emit("", [])
        else:
            self.reference_picked.emit(other, list(item.data(0, self._ROLE_JOIN) or []))

    def _on_link_clicked(self, item, column):
        to_add = item.data(0, self._ROLE_ADD)
        if to_add and column == self._ADD_COL:
            self.add_requested.emit(list(to_add))

    def _add_current(self):
        item = self._links.currentItem()
        to_add = item.data(0, self._ROLE_ADD) if item is not None else None
        if to_add:
            self.add_requested.emit(list(to_add))

    def _on_link_activated(self, item, _column):
        other = item.data(0, self._ROLE_TABLE)
        if other:
            self.select_requested.emit(other)

    def _menu(self, pos):
        item = self._links.itemAt(pos)
        other = item.data(0, self._ROLE_TABLE) if item is not None else None
        if not other:
            return
        menu = QMenu(self)
        to_add = item.data(0, self._ROLE_ADD)
        if to_add:
            menu.addAction(
                f"Add {' and '.join(to_add)} to the diagram",
                lambda: self.add_requested.emit(list(to_add)),
            )
        menu.addAction(f"Go to {other}", lambda: self.select_requested.emit(other))
        menu.exec(self._links.viewport().mapToGlobal(pos))

    def retheme(self):
        muted = f"color: {_muted_hex(self)};"
        for label in (self._meta, self._empty, self._hint):
            label.setStyleSheet(muted)
