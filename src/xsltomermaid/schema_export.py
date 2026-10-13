"""Export the selected tables and their columns as CSV, JSON or Excel.

Free of Qt so it can be unit-tested on its own; the options dialog lives in
:mod:`schema_export_dialog`. With the spreadsheet headers, a CSV or Excel export
of every field reads straight back into the app.
"""

from __future__ import annotations

import csv
import json
import os
import re
from dataclasses import dataclass, field

from . import __version__
from .excel_to_mermaid import Column, Relationship, Schema, Table

# (key, spreadsheet header, readable label). The spreadsheet headers are the
# ones the app reads (EXPECTED_HEADERS), plus FullType, which it ignores.
FIELDS: list[tuple[str, str, str]] = [
    ("schema", "SchemaName", "Schema"),
    ("table", "TableName", "Table"),
    ("order", "ColumnOrder", "Order"),
    ("name", "ColumnName", "Column"),
    ("data_type", "DataType", "Data type"),
    ("full_type", "FullType", "Full type"),
    ("length", "Length", "Length"),
    ("precision", "Precision", "Precision"),
    ("scale", "Scale", "Scale"),
    ("is_nullable", "IsNullable", "Nullable"),
    ("is_identity", "IsIdentity", "Identity"),
    ("is_computed", "IsComputed", "Computed"),
    ("is_primary_key", "IsPrimaryKey", "Primary key"),
    ("foreign_key_reference", "ForeignKeyReference", "Foreign key reference"),
    ("default_value", "DefaultValue", "Default"),
    ("computed_definition", "ComputedDefinition", "Computed definition"),
    ("collation", "Collation", "Collation"),
    ("description", "Description", "Description"),
]
FIELD_KEYS = [key for key, *_ in FIELDS]
# Left on by default; FullType repeats DataType/Length/Precision/Scale.
DEFAULT_FIELDS = [key for key in FIELD_KEYS if key != "full_type"]
_BOOL_FIELDS = {"is_nullable", "is_identity", "is_computed", "is_primary_key"}
_NUMBER_FIELDS = {"order", "length", "precision", "scale"}

FORMATS = [("csv", "CSV"), ("json", "JSON"), ("xlsx", "Excel")]
EXTENSIONS = {"csv": ".csv", "json": ".json", "xlsx": ".xlsx"}
FILE_FILTERS = {
    "csv": "CSV file (*.csv)",
    "json": "JSON file (*.json)",
    "xlsx": "Excel workbook (*.xlsx)",
}
BOOL_STYLES = [("true_false", "TRUE / FALSE"), ("yes_no", "Yes / No"), ("one_zero", "1 / 0")]
CSV_DELIMITERS = [(",", "Comma"), (";", "Semicolon"), ("\t", "Tab")]

_RELATIONSHIP_HEADERS = ["ParentTable", "ParentColumns", "ChildTable", "ChildColumns", "Label"]
_TABLE_HEADERS = ["SchemaName", "TableName", "Columns", "PrimaryKey", "ForeignKeys"]


@dataclass
class TableExportOptions:
    format: str = "xlsx"
    fields: list[str] = field(default_factory=lambda: list(DEFAULT_FIELDS))
    # Spreadsheet headers (SchemaName, …) read back in; labels read better.
    spreadsheet_headers: bool = True
    bool_style: str = "true_false"
    include_relationships: bool = True
    # CSV
    delimiter: str = ","
    excel_bom: bool = True
    # JSON
    nested: bool = True
    pretty: bool = True
    # Excel
    sheet_per_table: bool = False
    summary_sheet: bool = True


def headers(fields: list[str], spreadsheet_headers: bool) -> list[str]:
    by_key = {key: (sheet, label) for key, sheet, label in FIELDS}
    return [by_key[f][0 if spreadsheet_headers else 1] for f in fields]


def _number(text: str):
    """``"18"`` as 18, so Excel and JSON don't hold numbers as text."""
    return int(text) if re.fullmatch(r"-?\d+", text or "") else text


def field_value(column: Column, key: str, typed: bool = False):
    """One field of a column. ``typed`` keeps numbers and booleans native
    (for JSON and Excel); otherwise every value is text-ready."""
    if key == "full_type":
        return column.rendered_type()
    value = getattr(column, key)
    if typed and key in _NUMBER_FIELDS:
        return _number(str(value))
    return value


def _bool_text(value: bool, style: str):
    if style == "yes_no":
        return "Yes" if value else "No"
    if style == "one_zero":
        return 1 if value else 0
    return "TRUE" if value else "FALSE"


def column_row(column: Column, opts: TableExportOptions, typed: bool = False) -> list:
    row = []
    for key in opts.fields:
        value = field_value(column, key, typed)
        if key in _BOOL_FIELDS:
            # Excel's own TRUE/FALSE are real booleans; the other styles are
            # what was asked for, verbatim.
            value = value if typed and opts.bool_style == "true_false" else _bool_text(
                value, opts.bool_style
            )
        row.append(value)
    return row


def _relationship_row(rel: Relationship) -> list[str]:
    return [
        rel.parent_table,
        ", ".join(rel.parent_columns),
        rel.child_table,
        ", ".join(rel.child_columns),
        rel.label,
    ]


def _table_row(table: Table) -> list:
    keys = [c.name for c in table.columns if c.is_primary_key]
    fks = sum(1 for c in table.columns if c.foreign_key_reference)
    return [table.schema, table.plain_name, len(table.columns), ", ".join(keys), fks]


def relationships_path(path: str) -> str:
    """Where a CSV export puts its relationships: beside it, ``…_relationships.csv``."""
    stem, ext = os.path.splitext(path)
    return f"{stem}_relationships{ext or '.csv'}"


def export_schema(schema: Schema, path: str, opts: TableExportOptions) -> list[str]:
    """Write ``schema`` to ``path``; returns every file written."""
    if not opts.fields:
        raise ValueError("Choose at least one field to export.")
    writer = {"csv": write_csv, "json": write_json, "xlsx": write_xlsx}.get(opts.format)
    if writer is None:
        raise ValueError(f"Unknown export format: {opts.format}")
    return writer(schema, path, opts)


def write_csv(schema: Schema, path: str, opts: TableExportOptions) -> list[str]:
    # A BOM is how Excel knows a CSV is UTF-8 (accented names stay intact).
    encoding = "utf-8-sig" if opts.excel_bom else "utf-8"
    with open(path, "w", encoding=encoding, newline="") as handle:
        out = csv.writer(handle, delimiter=opts.delimiter)
        out.writerow(headers(opts.fields, opts.spreadsheet_headers))
        for table in schema.tables:
            for column in table.columns:
                out.writerow(column_row(column, opts))
    written = [path]
    if opts.include_relationships and schema.relationships:
        rel_path = relationships_path(path)
        with open(rel_path, "w", encoding=encoding, newline="") as handle:
            out = csv.writer(handle, delimiter=opts.delimiter)
            out.writerow(_RELATIONSHIP_HEADERS)
            for rel in schema.relationships:
                out.writerow(_relationship_row(rel))
        written.append(rel_path)
    return written


def _column_object(column: Column, opts: TableExportOptions, skip=()) -> dict:
    names = headers(opts.fields, opts.spreadsheet_headers)
    values = column_row(column, opts, typed=True)
    return {n: v for k, n, v in zip(opts.fields, names, values) if k not in skip}


def schema_to_json(schema: Schema, opts: TableExportOptions) -> dict:
    data: dict = {"generator": f"xsltomermaid {__version__}"}
    if opts.nested:
        # Schema and table name are the table's, not repeated on each column.
        data["tables"] = [
            {
                "schema": table.schema,
                "name": table.plain_name,
                "columns": [
                    _column_object(c, opts, skip=("schema", "table"))
                    for c in table.columns
                ],
            }
            for table in schema.tables
        ]
    else:
        data["columns"] = [
            _column_object(c, opts) for table in schema.tables for c in table.columns
        ]
    if opts.include_relationships:
        data["relationships"] = [
            {
                "parentTable": rel.parent_table,
                "parentColumns": list(rel.parent_columns),
                "childTable": rel.child_table,
                "childColumns": list(rel.child_columns),
                "label": rel.label,
            }
            for rel in schema.relationships
        ]
    return data


def write_json(schema: Schema, path: str, opts: TableExportOptions) -> list[str]:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(
            schema_to_json(schema, opts),
            handle,
            ensure_ascii=False,
            indent=2 if opts.pretty else None,
        )
        handle.write("\n")
    return [path]


# Excel forbids these in sheet names, and caps them at 31 characters.
_BAD_SHEET_CHARS = re.compile(r"[\[\]:*?/\\]")


def sheet_name(name: str, taken: set[str]) -> str:
    base = _BAD_SHEET_CHARS.sub("_", name).strip("'") or "Sheet"
    candidate, n = base[:31], 2
    while candidate.lower() in taken:
        suffix = f" ({n})"
        candidate, n = base[: 31 - len(suffix)] + suffix, n + 1
    taken.add(candidate.lower())
    return candidate


def _fill_sheet(ws, header: list[str], rows) -> None:
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
    from openpyxl.styles import Font

    ws.append(header)
    for row in rows:
        # Control characters (pasted into a description) make openpyxl refuse.
        ws.append([ILLEGAL_CHARACTERS_RE.sub("", v) if isinstance(v, str) else v for v in row])
        for cell in ws[ws.max_row]:
            # A value like "=getdate()" is text from the sheet, not a formula.
            if cell.data_type == "f":
                cell.data_type = "s"
    for cell in ws[1]:
        cell.font = Font(bold=True)
    ws.freeze_panes = "A2"
    if ws.max_row > 1:
        ws.auto_filter.ref = ws.dimensions
    for col_cells in ws.iter_cols(min_row=1, max_row=min(ws.max_row, 500)):
        width = max(len(str(c.value)) if c.value is not None else 0 for c in col_cells)
        ws.column_dimensions[col_cells[0].column_letter].width = min(max(width + 2, 8), 60)


def write_xlsx(schema: Schema, path: str, opts: TableExportOptions) -> list[str]:
    from openpyxl import Workbook

    wb = Workbook()
    wb.remove(wb.active)
    taken: set[str] = set()
    header = headers(opts.fields, opts.spreadsheet_headers)
    # The column sheet comes first: it's the one the app reads back.
    if opts.sheet_per_table:
        for table in schema.tables:
            ws = wb.create_sheet(sheet_name(table.name, taken))
            _fill_sheet(ws, header, (column_row(c, opts, typed=True) for c in table.columns))
    else:
        ws = wb.create_sheet(sheet_name("Columns", taken))
        _fill_sheet(
            ws,
            header,
            (column_row(c, opts, typed=True) for t in schema.tables for c in t.columns),
        )
    if opts.summary_sheet:
        ws = wb.create_sheet(sheet_name("Tables", taken))
        _fill_sheet(ws, _TABLE_HEADERS, (_table_row(t) for t in schema.tables))
    if opts.include_relationships and schema.relationships:
        ws = wb.create_sheet(sheet_name("Relationships", taken))
        _fill_sheet(ws, _RELATIONSHIP_HEADERS, (_relationship_row(r) for r in schema.relationships))
    if not wb.worksheets:
        wb.create_sheet("Columns")
    wb.save(path)
    return [path]
