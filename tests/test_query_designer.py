"""Headless GUI tests for the Query designer view (query_designer.py).

Run: QT_QPA_PLATFORM=offscreen uv run pytest tests/test_query_designer.py
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox --disable-gpu --in-process-gpu"
)

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from xsltomermaid import main as app_module  # noqa: E402
from xsltomermaid.excel_to_mermaid import (  # noqa: E402
    EXPECTED_HEADERS,
    Column,
    Relationship,
    Schema,
    Table,
)
from xsltomermaid.make_sample import ROWS  # noqa: E402
from xsltomermaid.query_designer import QueryDesignerView, TableCard  # noqa: E402
from xsltomermaid.query_model import OutputColumn  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("XSLTOMERMAID_SETTINGS", str(tmp_path / "settings.ini"))


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def _sample(path):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(EXPECTED_HEADERS)
    for row in ROWS:
        ws.append(row)
    wb.save(path)
    return str(path)


def _designer(app, schema):  # noqa: ARG001
    view = QueryDesignerView()
    view.resize(1200, 700)
    view.show()
    view.set_schema(schema)
    return view


def _pump(view):
    """Let the deferred refresh (panels and SQL) run."""
    QApplication.processEvents()
    view._refresh_now()


def _shop() -> Schema:
    def col(table, name, kind="int", pk=False, fk=""):
        return Column("dbo", table, 1, name, kind, is_primary_key=pk, foreign_key_reference=fk)

    return Schema(
        tables=[
            Table("dbo", "Customer", [col("Customer", "CustomerID", pk=True),
                                      col("Customer", "Name", "nvarchar")]),
            Table("dbo", "Order", [col("Order", "OrderID", pk=True),
                                   col("Order", "CustomerID", fk="dbo.Customer.CustomerID"),
                                   col("Order", "Total", "decimal")]),
            Table("dbo", "Note", [col("Note", "NoteID", pk=True), col("Note", "About")]),
        ],
        relationships=[Relationship("Customer", "Order", "CustomerID",
                                    ("CustomerID",), ("CustomerID",))],
    )


def test_tables_join_on_foreign_keys_and_the_sql_follows(app):
    view = _designer(app, _shop())
    view.add_table("Customer")
    view.add_table("Order")
    _pump(view)
    assert set(view.cards) == {"c", "o"}
    assert len(view.edges) == 1
    sql = view.sql_text()
    assert "FROM dbo.Customer AS c\nINNER JOIN dbo.[Order] AS o\n    ON c.CustomerID = o.CustomerID" in sql
    assert view.joins_table.rowCount() == 1
    assert view.start.currentData() == "c"


def test_clicking_a_column_puts_it_in_the_result(app):
    view = _designer(app, _shop())
    view.add_table("Customer")
    card = view.cards["c"]
    row = next(i for i, c in enumerate(card.rows) if c and c.name == "Name")
    point = card.mapToScene(QPointF(60, card.HEAD_H + row * card.ROW_H + card.ROW_H / 2))
    pos = view.canvas.mapFromScene(point)
    QTest.mouseClick(view.canvas.viewport(), Qt.LeftButton, Qt.NoModifier, pos)
    _pump(view)
    assert [(o.alias, o.column) for o in view.design.outputs] == [("c", "Name")]
    assert view.sql_text().startswith("SELECT\n    c.Name\nFROM")
    assert view.outputs.rowCount() == 1
    QTest.mouseClick(view.canvas.viewport(), Qt.LeftButton, Qt.NoModifier, pos)
    _pump(view)
    assert view.design.outputs == []


def test_dragging_a_column_onto_another_table_joins_them(app):
    view = _designer(app, _shop())
    view.auto_join.setChecked(False)
    view.add_table("Customer")
    view.add_table("Note")
    _pump(view)
    assert view.design.joins == []

    def at(card, column):
        row = next(i for i, c in enumerate(card.rows) if c and c.name == column)
        point = card.mapToScene(QPointF(60, card.HEAD_H + row * card.ROW_H + card.ROW_H / 2))
        return view.canvas.mapFromScene(point)

    start, end = at(view.cards["c"], "CustomerID"), at(view.cards["n"], "About")
    viewport = view.canvas.viewport()
    QTest.mousePress(viewport, Qt.LeftButton, Qt.NoModifier, start)
    for t in (0.25, 0.5, 0.75, 1.0):
        point = QPointF(start + (end - start) * t)
        move = QMouseEvent(QEvent.MouseMove, point, viewport.mapToGlobal(point),
                           Qt.NoButton, Qt.LeftButton, Qt.NoModifier)
        QApplication.sendEvent(viewport, move)
    QTest.mouseRelease(viewport, Qt.LeftButton, Qt.NoModifier, end)
    _pump(view)
    assert [(j.left, j.right, j.pairs) for j in view.design.joins] == [
        ("c", "n", (("CustomerID", "About"),))
    ]
    assert "ON c.CustomerID = n.About" in view.sql_text()
    assert view._link is None  # the rubber-band line is gone


def test_join_type_unjoined_tables_and_removal(app):
    view = _designer(app, _shop())
    view.add_table("Customer")
    view.add_table("Order")
    view.add_table("Note")  # no foreign key to the others
    _pump(view)
    assert "-- Not joined: Note (n)." in view.sql_text()
    view.set_join_kind(view.design.joins[0], "LEFT")
    _pump(view)
    assert "LEFT JOIN dbo.[Order] AS o" in view.sql_text()
    view.remove_table("o")
    _pump(view)
    assert set(view.cards) == {"c", "n"} and view.edges == [] and view.design.joins == []


def test_output_panel_aggregates_sort_and_options(app):
    view = _designer(app, _shop())
    view.add_table("Customer")
    view.add_table("Order")
    view.group_and_count("c", "Name")
    view.add_output(OutputColumn("o", "Total", "SUM"))
    view.top.setValue(5)
    view.distinct.setChecked(True)
    _pump(view)
    sql = view.sql_text()
    assert sql.startswith("SELECT DISTINCT TOP (5)\n    c.Name,\n    COUNT(*) AS row_count,\n"
                          "    SUM(o.Total) AS sum_Total\n")
    assert "GROUP BY c.Name\nORDER BY row_count DESC;" in sql
    # The aggregate box edits the design.
    box = view.outputs.cellWidget(2, 1)
    box.setCurrentIndex(box.findData("MAX"))
    _pump(view)
    assert "MAX(o.Total) AS max_Total" in view.sql_text()
    # Editing the Column cell names the column in the result.
    view.outputs.item(0, 0).setText("c.Name AS Customer name")
    _pump(view)
    assert view.design.outputs[0].label == "Customer name"
    assert "    c.Name AS [Customer name],\n" in view.sql_text()
    assert view.outputs.item(0, 0).text() == "c.Name AS Customer name"
    view.outputs.item(0, 0).setText("c.Name")  # back to no name
    _pump(view)
    assert view.design.outputs[0].label == ""
    # Clear starts over, options included.
    view.clear()
    _pump(view)
    assert view.sql_text() == "" and not view.distinct.isChecked() and view.top.value() == 0


def test_wide_tables_fold_to_their_keys(app):
    columns = [Column("", "Wide", 1, "WideID", "int", is_primary_key=True)] + [
        Column("", "Wide", i, f"col{i}", "int") for i in range(2, 40)
    ]
    view = _designer(app, Schema(tables=[Table("", "Wide", columns)]))
    view.add_table("Wide")
    card = view.cards["w"]
    assert [c.name for c in card.rows[:-1]] == ["WideID"] and card.rows[-1] is None
    assert card.hidden == 38
    view.toggle_output("w", "col7")  # a column the query uses always shows
    assert [c.name for c in card.rows[:-1]] == ["WideID", "col7"]
    view.toggle_expanded("w")
    assert len(card.rows) == 40  # every column plus "show fewer"


def test_the_window_switches_to_the_query_view(app, tmp_path):
    window = app_module.MainWindow()
    window.load_file(_sample(tmp_path / "sample.xlsx"))
    assert not window.query_visible()
    window._query_btn.click()
    assert window.query_visible() and window._query_btn.isChecked()
    assert not window._options_bar._orientation.isVisible()  # diagram-only controls
    window._map_btn.click()
    assert window.map_visible() and not window.query_visible()
    window._query_action.trigger()  # Ctrl+J
    assert window.query_visible()
    window._query_action.trigger()
    assert not window.query_visible() and window._diagram_view_btn.isChecked()

    # Table list › Add to query opens the designer with the table placed.
    window._selector.query_requested.emit("Order")
    assert window.query_visible()
    assert [t.table for t in window._query_view.design.tables] == ["Order"]
    window._selector.query_requested.emit("Customer")
    window._query_view._refresh_now()
    assert "JOIN dbo.Customer AS c" in window._query_view.sql_text()

    # Ctrl+Shift+Q copies the designed query while the designer is showing.
    window.copy_sql()
    assert QApplication.clipboard().text() == window._query_view.sql_text()

    # Add ticked tables places every ticked table.
    window._query_view.clear()
    window._query_view.add_ticked_btn.click()
    assert len(window._query_view.design.tables) == len(window._selector.selected_tables()) > 0

    window.retheme()  # repaints without error
    window.close_file()
    assert not window.query_visible() and window._query_view.design.tables == []
    window._diagram_view.cleanup()


def test_cards_are_painted(app, tmp_path):
    view = _designer(app, _shop())
    view.add_table("Customer")
    view.add_table("Order")
    view.toggle_output("c", "Name")
    _pump(view)
    view.canvas.fit()
    out = tmp_path / "designer.png"
    assert view.grab().save(str(out))
    assert out.stat().st_size > 5000
    assert all(isinstance(c, TableCard) for c in view.cards.values())
