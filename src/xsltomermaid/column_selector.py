"""The Details panel's Columns view: which columns each table shows."""

from __future__ import annotations

from PySide6.QtCore import (
    Qt,
    Signal,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .config import COLUMN_TREE_LIMIT
from .excel_to_mermaid import (
    Schema,
    Table,
)
from .theme import _muted_hex


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
        self._keys_only = False
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
        text = f"{included:,} of {total:,} columns included"
        if self._keys_only:
            text += " · Keys only is on, so only key columns are drawn"
        self._count.setText(text)

    def set_keys_only(self, on: bool):
        """Say in the count when Keys only draws fewer columns than are ticked."""
        if on != self._keys_only:
            self._keys_only = on
            self._update_count()

    def retheme(self):
        muted = f"color: {_muted_hex(self)};"
        self._hint.setStyleSheet(muted)
        self._count.setStyleSheet(muted)
