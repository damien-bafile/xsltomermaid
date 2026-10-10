"""Help menu dialogs: the spreadsheet format and the keyboard shortcuts."""

from __future__ import annotations

from PySide6.QtWidgets import QMessageBox

from .excel_to_mermaid import EXPECTED_HEADERS


def show_format_help(parent) -> None:
    """Help › Spreadsheet format: the input the app reads."""
    headers = ", ".join(f"<code>{h}</code>" for h in EXPECTED_HEADERS)
    QMessageBox.information(
        parent,
        "Spreadsheet format",
        "<p><b>One row per database column.</b> The first sheet needs a header "
        "row with at least <code>TableName</code> and <code>ColumnName</code>; "
        "headers can be in any order, case or spacing, and extra columns are "
        "ignored.</p>"
        f"<p>Recognised headers: {headers}.</p>"
        "<p><b>Foreign keys</b> come from <code>ForeignKeyReference</code>, in any "
        "of these forms: <code>dbo.Customer.CustomerID</code>, "
        "<code>Customer.CustomerID</code>, <code>Customer(CustomerID)</code> or "
        "just <code>Customer</code>. Repeat a reference on several columns for a "
        "composite key.</p>"
        "<p><b>From SQL Server:</b> View › T-SQL statement shows a query that "
        "produces this sheet; run it, then paste or export the result to Excel.</p>",
    )

def show_shortcuts_help(parent) -> None:
    """Help › Keyboard shortcuts, in one place."""
    rows = [
        ("Ctrl+O", "Open a spreadsheet"),
        ("Ctrl+M", "Switch between the diagram and the map"),
        ("Ctrl+I / Ctrl+1–6", "Show the Details panel / one of its views"),
        ("Ctrl+F", "Find tables by name or column (fuzzy)"),
        ("F5 or Ctrl+Enter", "Draw the ticked tables"),
        ("Esc", "Stop drawing; in the diagram or map, clear the selection"),
        ("Ctrl+Z", "Undo the last change to the ticks"),
        ("Alt+K", "Keys only"),
        ("Ctrl+E", "Export"),
        ("Ctrl+Shift+C / Ctrl+Shift+Q", "Copy the Mermaid / the SQL query"),
        ("Ctrl+= / Ctrl+- / Ctrl+0", "Zoom in / out / 100%"),
        ("Diagram: arrows, Enter", "Move between tables, open one"),
        ("Map: drag, Ctrl+click", "Select a region, add or remove a table"),
        ("Map: arrows, Space, Enter, Ctrl+A", "Move, select, open, select the cluster"),
        ("Relationships view: Space", "Tick the highlighted linked table"),
        ("Columns view: drag a row", "Give the table its own column order"),
        ("Diagram: drag a relationship label", "Move it; drag its dot to rotate "
         "(Shift: 15° steps); double-click to reset"),
    ]
    table = "".join(
        f"<tr><td style='padding:2px 16px 2px 0'><b>{keys}</b></td><td>{what}</td></tr>"
        for keys, what in rows
    )
    QMessageBox.information(parent, "Keyboard shortcuts", f"<table>{table}</table>")
