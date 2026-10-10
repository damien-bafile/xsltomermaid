"""The Details panel's Tables view: the tables in the diagram."""

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
    QLineEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .theme import (
    _link_hex,
    _muted_hex,
)

_FILTER_FROM = 12  # show the filter box from this many tables


class TablesView(QWidget):
    """The tables in the diagram, each with a Columns action.

    Clicking a row selects that table everywhere; Columns (or Enter) also
    opens the Columns view on it.
    """

    table_picked = Signal(str)
    columns_requested = Signal(str)

    _ROLE_TABLE = Qt.UserRole
    _SHOWN, _OPEN = 1, 2

    def __init__(self, parent=None):
        super().__init__(parent)
        self._updating = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self._meta = QLabel()
        self._meta.setWordWrap(True)
        self._empty = QLabel(
            "The tables in the diagram are listed here. Tick tables at left to draw them."
        )
        self._empty.setWordWrap(True)
        self._empty.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Filter tables…")
        self._filter.setAccessibleName("Filter tables")
        self._filter.setClearButtonEnabled(True)
        self._filter.textChanged.connect(self._apply_filter_text)
        self._tree = QTreeWidget()
        self._tree.setAccessibleName("Tables in the diagram")
        self._tree.setColumnCount(3)
        self._tree.setHeaderLabels(["Table", "Columns shown", ""])
        self._tree.setRootIsDecorated(False)
        self._tree.setUniformRowHeights(True)
        header = self._tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(self._SHOWN, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(self._OPEN, QHeaderView.ResizeToContents)
        self._tree.currentItemChanged.connect(self._on_current)
        self._tree.itemClicked.connect(self._on_clicked)
        self._tree.itemActivated.connect(self._on_activated)
        for widget in (self._meta, self._empty, self._filter):
            layout.addWidget(widget)
        layout.addWidget(self._tree, 1)
        self.retheme()
        self.set_tables([])

    def set_tables(self, rows: list[tuple[str, int, int]]):
        """List ``(table, columns shown, columns)`` rows, keeping the current one."""
        current = self.current_table()
        self._updating = True
        self._tree.clear()
        link = QColor(_link_hex(self))
        for name, shown, total in rows:
            item = QTreeWidgetItem([name, f"{shown:,} of {total:,}", "Columns ›"])
            item.setData(0, self._ROLE_TABLE, name)
            item.setTextAlignment(self._SHOWN, Qt.AlignRight | Qt.AlignVCenter)
            item.setToolTip(self._OPEN, f"Choose {name}'s columns")
            font = item.font(self._OPEN)
            font.setUnderline(True)
            item.setFont(self._OPEN, font)
            item.setForeground(self._OPEN, link)
            self._tree.addTopLevelItem(item)
        self._updating = False
        has = bool(rows)
        self._tree.setVisible(has)
        self._empty.setVisible(not has)
        self._meta.setVisible(has)
        self._meta.setText(
            f"{len(rows):,} table{'' if len(rows) == 1 else 's'} in the diagram. "
            "Click one to select it; Columns opens its columns."
        )
        self._filter.setVisible(len(rows) >= _FILTER_FROM)
        self._apply_filter_text(self._filter.text())
        self.set_current(current)

    def current_table(self) -> str:
        item = self._tree.currentItem()
        return item.data(0, self._ROLE_TABLE) if item is not None else ""

    def set_current(self, name: str):
        """Highlight ``name``'s row (none if it isn't listed), without signalling."""
        self._updating = True
        match = next((i for i in self._rows() if i.data(0, self._ROLE_TABLE) == name), None)
        self._tree.setCurrentItem(match)
        if match is None:
            self._tree.clearSelection()
        else:
            self._tree.scrollToItem(match)
        self._updating = False

    def _on_current(self, item, _prev=None):
        if not self._updating and item is not None:
            self.table_picked.emit(item.data(0, self._ROLE_TABLE))

    def _on_clicked(self, item, column):
        if column == self._OPEN:
            self.columns_requested.emit(item.data(0, self._ROLE_TABLE))

    def _on_activated(self, item, _column):
        self.columns_requested.emit(item.data(0, self._ROLE_TABLE))

    def _apply_filter_text(self, text: str):
        needle = text.strip().lower()
        for item in self._rows():
            item.setHidden(needle not in item.text(0).lower())

    def _rows(self):
        return [self._tree.topLevelItem(i) for i in range(self._tree.topLevelItemCount())]

    def retheme(self):
        muted = f"color: {_muted_hex(self)};"
        for label in (self._meta, self._empty):
            label.setStyleSheet(muted)
