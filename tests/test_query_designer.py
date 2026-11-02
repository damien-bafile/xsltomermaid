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


# ---------------------------------------------------------------------------
# Filters (query_filters.py)
# ---------------------------------------------------------------------------
def _drop(widget, y, panel, item):
    """A drag of ``item`` entering, moving over and dropping on ``widget``."""
    from PySide6.QtCore import QMimeData, QPoint
    from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent

    from xsltomermaid.query_filters import MIME_FILTER

    mime = QMimeData()
    mime.setData(MIME_FILTER, b"1")
    panel.dragged = item
    args = (Qt.MoveAction, mime, Qt.LeftButton, Qt.NoModifier)
    QApplication.sendEvent(widget, QDragEnterEvent(QPoint(30, int(y)), *args))
    QApplication.sendEvent(widget, QDragMoveEvent(QPoint(30, int(y)), *args))
    marker = widget.marker.isVisible() if hasattr(widget, "marker") else None
    drop = QDropEvent(QPointF(30, y), *args)
    QApplication.sendEvent(widget, drop)
    panel.dragged = None
    return drop.isAccepted(), marker


def test_nested_filters_are_built_in_the_filters_tab(app):
    view = _designer(app, _shop())
    view.add_table("Customer")
    view.add_table("Order")
    panel = view.filters
    # Name starts with A, and (Total > 100 or Total between 1 and 5).
    first = view.add_filter("c", "Name")
    row = panel.rows()[0]
    assert view.side_tabs.currentWidget() is panel and row.condition is first
    row.op.setCurrentText("starts with")
    row.value.setText("A")
    panel.add_group(view.design.where)
    inner = panel.root.children_widgets[1]
    inner.mode.setCurrentIndex(1)  # Any of these (OR)
    view.add_filter("o", "Total", group=inner.group)
    view.add_filter("o", "Total", group=panel.root.children_widgets[1].group)
    rows = panel.rows()
    rows[1].op.setCurrentText(">")
    rows[1].value.setText("100")
    rows[2].op.setCurrentText("between")
    assert rows[2].value2.isVisibleTo(rows[2]) and not rows[1].value2.isVisibleTo(rows[1])
    rows[2].value.setText("1")
    rows[2].value2.setText("5")
    _pump(view)
    assert ("WHERE c.Name LIKE N'A%'\n  AND (o.Total > 100\n    OR o.Total BETWEEN 1 AND 5)"
            in view.sql_text())
    # The card marks the filtered columns.
    assert view.filtered_columns("o") == {"total"}
    # The column box re-targets a condition.
    combo = rows[1].column
    combo.setCurrentIndex(combo.findData("o\tOrderID"))
    _pump(view)
    assert "OR o.Total BETWEEN" in view.sql_text() and "(o.OrderID > 100" in view.sql_text()
    # "None of these" negates the group.
    panel.root.children_widgets[1].mode.setCurrentIndex(2)
    _pump(view)
    assert "AND NOT (o.OrderID > 100\n    OR o.Total BETWEEN 1 AND 5)" in view.sql_text()


def test_filter_items_move_by_drag_and_drop(app):
    view = _designer(app, _shop())
    view.add_table("Customer")
    view.add_table("Order")
    panel = view.filters
    a = view.add_filter("c", "Name")
    b = view.add_filter("o", "Total")
    panel.add_group(view.design.where)
    group = view.design.where.items[2]
    c = view.add_filter("o", "OrderID", group=group)
    for cond, value in ((a, "x"), (b, "1"), (c, "2")):
        cond.value = value
    panel.rebuild()
    view.resize(1200, 900)
    QApplication.processEvents()

    # Drop Total into the group, above OrderID.
    box = panel.root.children_widgets[2]
    accepted, marker = _drop(box, box.children_widgets[0].geometry().top() + 1, panel, b)
    assert accepted and marker
    assert view.design.where.items == [a, group] and group.items == [b, c]
    # Drop Name at the end of its own group (reorder).
    QApplication.processEvents()
    root = panel.root
    _drop(root, root.height() - 2, panel, a)
    assert view.design.where.items == [group, a]
    # A group can't go inside itself: over its own box, the group around it
    # takes the drop and leaves it where it is.
    QApplication.processEvents()
    inner = panel.root.children_widgets[0]
    _drop(inner, 10, panel, group)
    assert view.design.where.items == [group, a] and group.items == [b, c]
    # Nor into a group nested inside it.
    panel.add_group(group)
    nested = group.items[-1]
    panel.move_item(group, nested, 0)
    assert view.design.where.items == [group, a] and group.items == [b, c, nested]
    panel.remove_item(nested)
    _pump(view)
    assert "WHERE (o.Total = 1\n    AND o.OrderID = 2)\n  AND c.Name = N'x'" in view.sql_text()


def test_removing_filters_tables_and_clearing(app):
    view = _designer(app, _shop())
    view.add_table("Customer")
    view.add_table("Order")
    view.add_filter("c", "Name")
    view.add_filter("o", "Total")
    assert len(view.filters.rows()) == 2
    view.remove_table("o")  # its conditions go with it
    QApplication.processEvents()
    assert [r.condition.alias for r in view.filters.rows()] == ["c"]
    view.filters.remove_item(view.design.where.items[0])
    assert view.filters.rows() == [] and view.design.where.items == []
    view.add_filter()  # no column given: the first one
    assert view.design.where.items[0].column == "CustomerID"
    view.filters.clear()
    assert view.design.where.items == [] and view.filters.rows() == []


# ---------------------------------------------------------------------------
# Saving and undo
# ---------------------------------------------------------------------------
def _settle(view):
    """Record the pending undo step now, as if the half second had passed."""
    _pump(view)
    view._history_timer.stop()
    view._record()


def test_state_round_trips_with_the_layout(app):
    view = _designer(app, _shop())
    view.add_table("Customer")
    view.add_table("Order")
    view.toggle_output("c", "Name")
    view.add_filter("o", "Total").value = "5"
    view.set_join_kind(view.design.joins[0], "FULL")
    view.cards["o"].setPos(QPointF(400, 250))
    view.quote_all.setChecked(True)
    _pump(view)
    sql, state = view.sql_text(), view.state()
    assert state["positions"]["o"] == [400.0, 250.0]

    other = _designer(app, _shop())
    other.restore(state)
    _pump(other)
    assert other.sql_text() == sql
    assert other.cards["o"].pos() == QPointF(400, 250)
    assert other.quote_all.isChecked()
    assert [r.condition.value for r in other.filters.rows()] == ["5"]


def test_a_saved_design_without_a_layout_is_laid_out(app):
    view = _designer(app, _shop())
    view.restore({"design": {"tables": [{"table": "Customer", "alias": "c"},
                                        {"table": "Order", "alias": "o"}]}})
    xs = [view.cards[a].pos().x() for a in ("c", "o")]
    assert xs[0] < xs[1]  # side by side, not stacked at the origin


def test_undo_and_redo(app):
    view = _designer(app, _shop())
    assert not view.can_undo() and not view.can_redo()
    view.add_table("Customer")
    _settle(view)
    view.add_table("Order")
    _settle(view)
    view.toggle_output("c", "Name")
    _settle(view)
    view.undo()
    _settle(view)  # the refresh after an undo isn't a new step
    assert view.design.outputs == [] and len(view.design.tables) == 2 and view.can_redo()
    view.undo()
    assert [t.alias for t in view.design.tables] == ["c"] and set(view.cards) == {"c"}
    view.redo()
    _pump(view)
    assert [t.alias for t in view.design.tables] == ["c", "o"] and len(view.edges) == 1
    # A change after undoing drops the redo steps.
    view.clear()
    _settle(view)
    assert view.design.tables == [] and not view.can_redo()
    view.undo()  # Clear is undone like any change
    assert [t.alias for t in view.design.tables] == ["c", "o"]
    # Typing a value is one step, however many keys.
    view.add_filter("c", "Name")
    _settle(view)
    row = view.filters.rows()[0]
    for text in ("A", "Ad", "Ada"):
        row.value.setText(text)
        _pump(view)  # within the half second: no step yet
    _settle(view)
    view.undo()
    assert view.design.where.items[0].value == ""
    # A new file starts a new history.
    view.set_schema(_shop())
    assert not view.can_undo() and not view.can_redo()


def test_the_window_saves_and_loads_the_query_in_presets(app, tmp_path, monkeypatch):
    window = app_module.MainWindow()
    window.load_file(_sample(tmp_path / "sample.xlsx"))
    window.show_query(True)
    view = window._query_view
    view.add_table("Customer")
    view.add_table("Order")
    view.group_and_count("c", "Name")
    _pump(view)
    sql = view.sql_text()
    path = tmp_path / "preset.toml"
    monkeypatch.setattr(app_module.QFileDialog, "getSaveFileName", lambda *a, **k: (str(path), ""))
    window.save_table_selection_toml()
    assert "query = " in path.read_text(encoding="utf-8")

    view.clear()
    _pump(view)
    assert view.sql_text() == ""
    monkeypatch.setattr(app_module.QFileDialog, "getOpenFileName", lambda *a, **k: (str(path), ""))
    window.load_table_selection_toml()
    _pump(view)
    assert view.sql_text() == sql

    # Ctrl+Z undoes the designer's change while it's showing.
    _settle(view)
    view.toggle_output("o", "Total")
    _settle(view)
    window._sync_undo_action()
    assert window._undo_action.isEnabled() and "query" in window._undo_action.text()
    window._undo_action.trigger()
    assert not any(o.column == "Total" for o in view.design.outputs)
    window._diagram_view.cleanup()
