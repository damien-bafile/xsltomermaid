"""The Details panel's SQL view: a T-SQL SELECT over the diagram's tables."""

from __future__ import annotations

from PySide6.QtCore import (
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QFont,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGridLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .excel_to_mermaid import Schema, without_system_columns
from .highlight import SqlHighlighter
from .sql_query import generate_select


class SqlTab(QWidget):
    """A ``SELECT`` joining the diagram's tables on their foreign keys, with the
    columns chosen in the Columns view, and the options that shape it.

    The window gives it the drawn schema (:meth:`set_schema`); Copy asks the
    window (:attr:`copy_requested`), which checks the diagram is current.
    """

    copy_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        sql_layout = QVBoxLayout(self)
        sql_layout.setContentsMargins(8, 8, 8, 8)
        sql_layout.setSpacing(6)
        self.root = QComboBox()
        self.root.setToolTip("The table in FROM; joins branch out from it.")
        self.join = QComboBox()
        self.join.addItem("INNER JOIN", "INNER")
        self.join.addItem("LEFT JOIN", "LEFT")
        self.join.setToolTip(
            "INNER keeps only rows that match in every table; LEFT keeps every "
            "row of the start table."
        )
        self.top = QSpinBox()
        self.top.setRange(0, 1_000_000)
        self.top.setValue(100)
        self.top.setSpecialValueText("No limit")
        self.top.setToolTip("SELECT TOP (n). 0 means no limit.")
        # Size to their longest value so the narrow panel never clips them.
        self.join.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.join.setMinimumWidth(
            self.join.fontMetrics().horizontalAdvance("INNER JOIN") + 44
        )
        self.top.setMinimumWidth(self.top.fontMetrics().horizontalAdvance("1000000") + 48)
        # A grid, one control per row, so the labels line up and nothing
        # collides or clips in the narrow Details panel.
        sql_grid = QGridLayout()
        sql_grid.setHorizontalSpacing(8)
        sql_grid.setVerticalSpacing(6)
        for r, (text, widget) in enumerate((
            ("Start table:", self.root),
            ("&Join:", self.join),
            ("Row limit:", self.top),
        )):
            label = QLabel(text)
            label.setBuddy(widget)
            widget.setAccessibleName(text.replace("&", "").rstrip(":"))
            sql_grid.addWidget(label, r, 0)
            sql_grid.addWidget(widget, r, 1, 1, 2 if widget is self.root else 1)
        # Brackets only where T-SQL needs them; this puts them on every name.
        self.quote_all = QCheckBox("Quote all names")
        self.quote_all.setToolTip(
            "Bracket every name ([dbo].[Order]). Off: only reserved words, names "
            "with spaces or symbols, and names starting with a digit are bracketed."
        )
        self.quote_all.toggled.connect(lambda _on: self.refresh())
        # Dynamics data: an append-only copy keeps every version of a row.
        self.latest = QCheckBox("Latest version only")
        self.latest.setToolTip(
            "For a Dynamics copy made by Azure Synapse Link or Fabric, where every "
            "change adds a row: keep each record's newest row (highest "
            "versionnumber per primary key) and leave out deleted ones "
            "(IsDelete). Those copies add both columns to every table."
        )
        self.active = QCheckBox("Active records only")
        self.active.setToolTip(
            "Keep rows with statecode = 0 (active) in every table that has a "
            "statecode column."
        )
        for box in (self.latest, self.active):
            box.toggled.connect(lambda _on: self.refresh())
        self.copy_btn = QPushButton("Copy S&QL")
        self.copy_btn.setToolTip("Copy the query to the clipboard (Ctrl+Shift+Q).")
        self.copy_btn.clicked.connect(self.copy_requested.emit)
        sql_grid.addWidget(self.copy_btn, 2, 2, Qt.AlignRight)
        sql_grid.addWidget(self.quote_all, 3, 1, 1, 2)
        sql_grid.addWidget(self.latest, 4, 1, 1, 2)
        sql_grid.addWidget(self.active, 5, 1, 1, 2)
        sql_grid.setColumnStretch(2, 1)
        sql_layout.addLayout(sql_grid)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setFont(QFont("Menlo, Consolas, monospace"))
        self.view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.view.setAccessibleName("SQL query")
        self.highlighter = SqlHighlighter(self.view.document(), self.view)
        self.view.setPlaceholderText(
            "A SELECT joining the diagram's tables on their foreign keys "
            "appears here once tables are selected."
        )
        sql_layout.addWidget(self.view, 1)
        self._schema: Schema | None = None
        self._full: Schema | None = None
        self.root.currentIndexChanged.connect(lambda _i: self.refresh())
        self.join.currentIndexChanged.connect(lambda _i: self.refresh())
        self.top.valueChanged.connect(lambda _v: self.refresh())

    def set_schema(self, schema: Schema, full: Schema | None = None,
                   hide_system: bool = False):
        """The drawn tables; ``full`` is the whole sheet (filters look columns
        up there). Start table keeps its choice when it's still drawn."""
        if hide_system:
            schema = without_system_columns(schema)  # out of the SELECT list too
        self._schema, self._full = schema, full
        current = self.root.currentText()
        names = [t.name for t in schema.tables]
        self.root.blockSignals(True)
        self.root.clear()
        self.root.addItems(names)
        if current in names:
            self.root.setCurrentText(current)
        self.root.blockSignals(False)
        self.refresh()

    def refresh(self):
        """Write the query for the current tables and options."""
        if self._schema is None or not self._schema.tables:
            self.view.setPlainText("")
            return
        self.view.setPlainText(
            generate_select(
                self._schema,
                root=self.root.currentText() or None,
                join=self.join.currentData(),
                top=self.top.value(),
                quote_all=self.quote_all.isChecked(),
                latest_only=self.latest.isChecked(),
                active_only=self.active.isChecked(),
                full_schema=self._full,
            )
        )

    def text(self) -> str:
        return self.view.toPlainText()

    def retheme(self):
        self.highlighter.retheme()
