"""The options asked before exporting the ticked tables and their columns."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from .config import app_settings
from .excel_to_mermaid import Schema, without_system_columns
from .schema_export import (
    BOOL_STYLES,
    CSV_DELIMITERS,
    DEFAULT_FIELDS,
    FIELD_KEYS,
    FIELDS,
    FORMATS,
    TableExportOptions,
)
from .theme import _plural

_KEY = "table_export/"


def _bool(value, default: bool) -> bool:
    if value is None:
        return default
    return str(value).lower() in ("1", "true", "yes")


class TableExportDialog(QDialog):
    """Format, scope and layout for File › Export tables and columns.

    ``ticked`` is the selection with the user's column choices applied;
    ``every`` is the same tables with all their columns. ``table_name``
    exports that one table (right-click › Export this table). Options that
    can't change this export are greyed out; the rest are remembered.
    """

    def __init__(
        self,
        ticked: Schema,
        every: Schema,
        source_name: str = "",
        hide_system: bool = False,
        parent=None,
        table_name: str = "",
    ):
        super().__init__(parent)
        self._table_name = table_name
        self.setWindowTitle(
            f"Export table {table_name}" if table_name else "Export tables and columns"
        )
        self._ticked = ticked
        self._every = every
        self._source = source_name
        settings = app_settings()

        def saved(name, default):
            return settings.value(_KEY + name, default)

        layout = QVBoxLayout(self)

        self._summary = QLabel()
        self._summary.setWordWrap(True)
        layout.addWidget(self._summary)

        # Format
        format_box = QGroupBox("Format")
        format_row = QHBoxLayout(format_box)
        self._format_group = QButtonGroup(self)
        current = str(saved("format", "xlsx"))
        for i, (key, label) in enumerate(FORMATS):
            radio = QRadioButton(label)
            radio.setProperty("key", key)
            radio.setChecked(key == current)
            self._format_group.addButton(radio, i)
            format_row.addWidget(radio)
        if self._format_group.checkedButton() is None:
            self._format_group.buttons()[-1].setChecked(True)
        format_row.addStretch(1)
        layout.addWidget(format_box)

        # What to include
        include_box = QGroupBox("Include")
        include = QVBoxLayout(include_box)
        self._ticked_only = QRadioButton("Only the ticked columns")
        self._every_column = QRadioButton(
            "Every column of the table" if table_name else "Every column of the ticked tables"
        )
        scope = QButtonGroup(self)
        scope.addButton(self._ticked_only)
        scope.addButton(self._every_column)
        if _bool(saved("every_column", None), False):
            self._every_column.setChecked(True)
        else:
            self._ticked_only.setChecked(True)
        include.addWidget(self._ticked_only)
        include.addWidget(self._every_column)
        self._hide_system = QCheckBox("Leave out system columns (createdon, versionnumber, …)")
        self._hide_system.setChecked(_bool(saved("hide_system", None), hide_system))
        include.addWidget(self._hide_system)
        self._relationships = QCheckBox("Relationships between the ticked tables")
        self._relationships.setChecked(_bool(saved("relationships", None), True))
        include.addWidget(self._relationships)

        include.addWidget(QLabel("Fields for each column:"))
        self._fields = QListWidget()
        self._fields.setAccessibleName("Fields for each column")
        chosen = str(saved("fields", ",".join(DEFAULT_FIELDS))).split(",")
        for key, sheet, label in FIELDS:
            item = QListWidgetItem(f"{label}  ({sheet})")
            item.setData(Qt.UserRole, key)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if key in chosen else Qt.Unchecked)
            self._fields.addItem(item)
        self._fields.setMinimumHeight(160)
        include.addWidget(self._fields)
        field_buttons = QHBoxLayout()
        for text, keys in (
            ("&All", FIELD_KEYS),
            ("&None", []),
            ("&Defaults", DEFAULT_FIELDS),
            ("&Essentials", ["schema", "table", "name", "full_type", "is_nullable",
                             "is_primary_key", "foreign_key_reference"]),
        ):
            button = QPushButton(text)
            button.setAutoDefault(False)
            button.clicked.connect(lambda _=False, k=keys: self._check_fields(k))
            field_buttons.addWidget(button)
        field_buttons.addStretch(1)
        include.addLayout(field_buttons)
        layout.addWidget(include_box)

        # Layout: shared, then the chosen format's own options.
        layout_box = QGroupBox("Layout")
        form = QFormLayout(layout_box)
        self._headers = QComboBox()
        self._headers.addItem("Spreadsheet headers (SchemaName, …): reloads in this app", True)
        self._headers.addItem("Readable labels (Schema, Data type, …)", False)
        self._headers.setCurrentIndex(0 if _bool(saved("spreadsheet_headers", None), True) else 1)
        form.addRow("Header names:", self._headers)
        self._bool_style = self._combo(BOOL_STYLES, saved("bool_style", "true_false"))
        form.addRow("Yes/no values as:", self._bool_style)

        # Each format's own rows; only the chosen format's are shown.
        self._format_rows: dict[str, list[QWidget]] = {}
        self._delimiter = self._combo(CSV_DELIMITERS, saved("delimiter", ","))
        form.addRow("Separator:", self._delimiter)
        self._bom = QCheckBox("Mark as UTF-8 so Excel shows accented names correctly")
        self._bom.setChecked(_bool(saved("excel_bom", None), True))
        form.addRow(self._bom)
        self._csv_note = QLabel(
            "Relationships go in a second file beside it, ending “_relationships.csv”."
        )
        self._csv_note.setWordWrap(True)
        form.addRow(self._csv_note)
        self._format_rows["csv"] = [self._delimiter, self._bom, self._csv_note]

        self._nested = QComboBox()
        self._nested.addItem("Nested: tables, each with its columns", True)
        self._nested.addItem("Flat: one object per column", False)
        self._nested.setCurrentIndex(0 if _bool(saved("nested", None), True) else 1)
        form.addRow("Structure:", self._nested)
        self._pretty = QCheckBox("Indent for reading (larger file)")
        self._pretty.setChecked(_bool(saved("pretty", None), True))
        form.addRow(self._pretty)
        self._format_rows["json"] = [self._nested, self._pretty]

        self._sheets = QComboBox()
        self._sheets.addItem("One sheet with every column: reloads in this app", False)
        self._sheets.addItem("One sheet per table", True)
        self._sheets.setCurrentIndex(1 if _bool(saved("sheet_per_table", None), False) else 0)
        form.addRow("Sheets:", self._sheets)
        self._summary_sheet = QCheckBox("Add a Tables sheet (column count, keys)")
        self._summary_sheet.setChecked(_bool(saved("summary_sheet", None), True))
        form.addRow(self._summary_sheet)
        self._format_rows["xlsx"] = [self._sheets, self._summary_sheet]
        self._form = form
        layout.addWidget(layout_box)

        self._open_after = QCheckBox("Open the file when it's saved")
        self._open_after.setChecked(_bool(saved("open_after", None), False))
        layout.addWidget(self._open_after)

        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self._ok = buttons.addButton("E&xport…", QDialogButtonBox.AcceptRole)
        self._ok.setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._format_group.idToggled.connect(lambda *_: self._sync())
        for widget in (self._every_column, self._hide_system, self._relationships):
            widget.toggled.connect(self._sync)
        self._fields.itemChanged.connect(self._sync)
        self._disable_moot()
        self._sync()
        self.adjustSize()

    def _disable(self, widget, why: str):
        """Grey out an option that can't change this export (not remembered)."""
        widget.setEnabled(False)
        widget.setToolTip(why)
        if isinstance(widget, QCheckBox):
            widget.setChecked(False)

    def _disable_moot(self):
        def n_columns(schema: Schema) -> int:
            return sum(len(t.columns) for t in schema.tables)

        if n_columns(self._ticked) == n_columns(self._every):
            for radio in (self._ticked_only, self._every_column):
                self._disable(radio, "Every column is ticked, so both give the same columns.")
        if n_columns(without_system_columns(self._every)) == n_columns(self._every):
            self._disable(self._hide_system, "There are no system columns to leave out.")
        if self._table_name:
            self._relationships.setText("Links from the table to itself")
        if not self._every.relationships:
            self._disable(
                self._relationships,
                "This table has no links to itself."
                if self._table_name
                else "There are no relationships between these tables.",
            )
        if self._table_name:
            self._sheets.setCurrentIndex(0)
            self._disable(self._sheets, "One table fills one sheet.")
            self._disable(self._summary_sheet, "A summary is for several tables.")

    def _combo(self, pairs, current) -> QComboBox:
        combo = QComboBox()
        for value, label in pairs:
            combo.addItem(label, value)
        at = combo.findData(current)
        combo.setCurrentIndex(max(at, 0))
        return combo

    def _check_fields(self, keys):
        for i in range(self._fields.count()):
            item = self._fields.item(i)
            item.setCheckState(Qt.Checked if item.data(Qt.UserRole) in keys else Qt.Unchecked)

    def chosen_fields(self) -> list[str]:
        items = (self._fields.item(i) for i in range(self._fields.count()))
        return [i.data(Qt.UserRole) for i in items if i.checkState() == Qt.Checked]

    def format(self) -> str:
        button = self._format_group.checkedButton()
        return str(button.property("key")) if button is not None else "xlsx"

    def schema(self) -> Schema:
        """The tables and columns the export will write."""
        schema = self._every if self._every_column.isChecked() else self._ticked
        if self._hide_system.isChecked():
            schema = without_system_columns(schema)
        return schema

    def open_after(self) -> bool:
        return self._open_after.isChecked()

    def options(self) -> TableExportOptions:
        return TableExportOptions(
            format=self.format(),
            fields=self.chosen_fields(),
            spreadsheet_headers=bool(self._headers.currentData()),
            bool_style=str(self._bool_style.currentData()),
            include_relationships=self._relationships.isChecked(),
            delimiter=str(self._delimiter.currentData()),
            excel_bom=self._bom.isChecked(),
            nested=bool(self._nested.currentData()),
            pretty=self._pretty.isChecked(),
            sheet_per_table=bool(self._sheets.currentData()),
            summary_sheet=self._summary_sheet.isChecked(),
        )

    def _sync(self, *_):
        kind = self.format()
        for key, widgets in self._format_rows.items():
            for widget in widgets:
                visible = key == kind
                if widget is self._csv_note:
                    visible = visible and self._relationships.isChecked()
                self._form.setRowVisible(widget, visible)
        schema = self.schema()
        n_cols = sum(len(t.columns) for t in schema.tables)
        source = f" from {self._source}" if self._source else ""
        what = (
            f"{self._table_name}" if self._table_name
            else _plural(len(schema.tables), "table")
        )
        self._summary.setText(f"Exports {what} ({_plural(n_cols, 'column')}){source}.")
        fields = self.chosen_fields()
        self._ok.setEnabled(bool(fields) and bool(schema.tables))
        self._ok.setToolTip("" if fields else "Tick at least one field.")

    def accept(self):
        settings = app_settings()
        opts = self.options()
        for name, widget, value in (
            ("format", None, opts.format),
            ("fields", None, ",".join(opts.fields)),
            ("every_column", self._every_column, self._every_column.isChecked()),
            ("hide_system", self._hide_system, self._hide_system.isChecked()),
            ("relationships", self._relationships, opts.include_relationships),
            ("spreadsheet_headers", None, opts.spreadsheet_headers),
            ("bool_style", None, opts.bool_style),
            ("delimiter", None, opts.delimiter),
            ("excel_bom", None, opts.excel_bom),
            ("nested", None, opts.nested),
            ("pretty", None, opts.pretty),
            ("sheet_per_table", self._sheets, opts.sheet_per_table),
            ("summary_sheet", self._summary_sheet, opts.summary_sheet),
            ("open_after", None, self.open_after()),
        ):
            # A greyed-out option was forced for this export, not chosen.
            if widget is None or widget.isEnabled():
                settings.setValue(_KEY + name, value)
        super().accept()
