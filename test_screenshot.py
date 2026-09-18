"""Headless GUI test: build the window, load the sample, and screenshot it.

Runs Qt with the offscreen platform so it works in CI with no display. The test
asserts the window actually painted something (a PNG of a sensible size).

Run: QT_QPA_PLATFORM=offscreen python test_screenshot.py
 or: uv run --extra – ...  (see README); pytest also picks it up.
"""

from __future__ import annotations

import os
import subprocess
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# Needed for the WebEngine (Chromium) diagram render to run headless / as root.
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox --disable-gpu --in-process-gpu"
)

import struct
import tempfile
import xml.etree.ElementTree as ET

import pytest

# Skip cleanly if the GUI stack (PySide6 + system libs) isn't importable.
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

import main as app_module  # noqa: E402
from make_sample import ROWS  # noqa: E402


def test_fit_scale_caps_large_exports():
    """A huge diagram at high scale is clamped to stay within the size caps."""
    from diagram_view import MAX_PNG_DIM, MAX_PNG_PIXELS, _fit_scale

    # Small diagram: requested scale is kept as-is.
    assert _fit_scale(400, 300, 2.0) == 2.0

    # Large diagram at 4x would be ~768 MP; must clamp under both caps.
    eff = _fit_scale(8000, 6000, 4.0)
    assert eff < 4.0
    assert 8000 * eff <= MAX_PNG_DIM + 1
    assert 6000 * eff <= MAX_PNG_DIM + 1
    assert (8000 * eff) * (6000 * eff) <= MAX_PNG_PIXELS * 1.01

    # Degenerate sizes don't blow up.
    assert _fit_scale(0, 0, 3.0) == 3.0


def test_render_style_defaults_to_left_to_right():
    from diagram_view import RenderStyle

    assert RenderStyle().layout_direction == "LR"


def test_diagram_options_bar_constructs_headlessly():
    app = QApplication.instance() or QApplication([])
    bar = app_module.DiagramOptionsBar()

    assert bar.render_style().layout_direction == "LR"


def test_schema_to_drawio_creates_table_vertices_and_edges():
    import diagram_view
    from excel_to_mermaid import build_schema

    records = [dict(zip(app_module.EXPECTED_HEADERS, row)) for row in ROWS]
    schema = build_schema(records)
    drawio = diagram_view.schema_to_drawio(schema)
    root = ET.fromstring(drawio)

    assert root.tag == "mxfile"
    vertices = root.findall(".//mxCell[@vertex='1']")
    edges = root.findall(".//mxCell[@edge='1']")
    assert len(vertices) == len(schema.tables)
    assert len(edges) == len(schema.relationships)
    assert all("shape=mxgraph.er.entity" in cell.attrib.get("style", "") for cell in vertices)
    assert all("endArrow=ERmany" in cell.attrib.get("style", "") for cell in edges)


def test_svg_dimensions_handles_fractional_and_exponent_sizes():
    import diagram_view

    w, h = diagram_view._svg_dimensions('<svg width=".5" height="1e3"></svg>')
    assert w == 0.5
    assert h == 1000.0
    w1, h1 = diagram_view._svg_dimensions('<svg width="1e3px" height="2.5e2pt"></svg>')
    assert w1 == 1000.0
    assert h1 == 250.0
    w0, h0 = diagram_view._svg_dimensions('<svg width="0" height="0"></svg>')
    assert w0 == 0.0
    assert h0 == 0.0


def test_schema_to_drawio_includes_table_names_and_fk_labels():
    import diagram_view
    from excel_to_mermaid import build_schema

    records = [dict(zip(app_module.EXPECTED_HEADERS, row)) for row in ROWS]
    schema = build_schema(records)
    drawio = diagram_view.schema_to_drawio(schema)
    root = ET.fromstring(drawio)
    vertex_values = [
        cell.attrib.get("value", "") for cell in root.findall(".//mxCell[@vertex='1']")
    ]
    edge_values = [
        cell.attrib.get("value", "") for cell in root.findall(".//mxCell[@edge='1']")
    ]
    assert any("<b>Customer</b>" in value for value in vertex_values)
    assert any("<b>Order</b>" in value for value in vertex_values)
    assert "CustomerID" in edge_values


def test_schema_to_drawio_relationship_matching_is_case_insensitive():
    import diagram_view
    from excel_to_mermaid import Relationship, Schema, Table

    schema = Schema(
        tables=[Table("dbo", "Customer"), Table("dbo", "Order")],
        relationships=[Relationship(parent_table="CUSTOMER", child_table="order", label="CustomerID")],
    )
    drawio = diagram_view.schema_to_drawio(schema)
    root = ET.fromstring(drawio)
    vertices = {
        cell.attrib["id"]: cell.attrib.get("value", "")
        for cell in root.findall(".//mxCell[@vertex='1']")
    }
    edges = root.findall(".//mxCell[@edge='1']")
    assert len(edges) == 1
    source = edges[0].attrib.get("source")
    target = edges[0].attrib.get("target")
    assert source in vertices and "<b>Customer</b>" in vertices[source]
    assert target in vertices and "<b>Order</b>" in vertices[target]


def test_schema_to_drawio_qualified_names_avoid_ambiguous_table_matches():
    import diagram_view
    from excel_to_mermaid import Relationship, Schema, Table

    schema = Schema(
        tables=[
            Table("dbo", "Customer"),
            Table("sales", "Customer"),
            Table("dbo", "Order"),
        ],
        relationships=[
            Relationship(parent_table="dbo.Customer", child_table="Order", label="CustomerID"),
            Relationship(parent_table="Customer", child_table="Order", label="AmbiguousCustomer"),
        ],
    )
    drawio = diagram_view.schema_to_drawio(schema)
    root = ET.fromstring(drawio)
    edge_values = [cell.attrib.get("value", "") for cell in root.findall(".//mxCell[@edge='1']")]
    assert "CustomerID" in edge_values
    assert "AmbiguousCustomer" not in edge_values


def test_schema_to_drawio_qualified_case_collision_is_ambiguous():
    import diagram_view
    from excel_to_mermaid import Relationship, Schema, Table

    schema = Schema(
        tables=[Table("dbo", "Customer"), Table("DBO", "customer"), Table("dbo", "Order")],
        relationships=[
            Relationship(parent_table="dbo.customer", child_table="Order", label="ShouldSkip")
        ],
    )
    drawio = diagram_view.schema_to_drawio(schema)
    root = ET.fromstring(drawio)
    edge_values = [cell.attrib.get("value", "") for cell in root.findall(".//mxCell[@edge='1']")]
    assert "ShouldSkip" not in edge_values


def _png_size(path: str) -> tuple[int, int]:
    """Return (width, height) read from a PNG header, or (0, 0) if invalid."""
    with open(path, "rb") as handle:
        header = handle.read(24)
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        return (0, 0)
    width, height = struct.unpack(">II", header[16:24])
    return (width, height)


def _ensure_sample(path: str):
    from openpyxl import Workbook

    from excel_to_mermaid import EXPECTED_HEADERS

    wb = Workbook()
    ws = wb.active
    ws.append(EXPECTED_HEADERS)
    for row in ROWS:
        ws.append(row)
    wb.save(path)


def test_window_screenshot(tmp_path):
    # Reuse a single QApplication across tests.
    app = QApplication.instance() or QApplication([])

    sample = tmp_path / "sample.xlsx"
    _ensure_sample(str(sample))

    window = app_module.MainWindow()
    window.load_file(str(sample))
    window.resize(1100, 760)
    assert window._options_bar.render_style().layout_direction == "LR"
    assert not window._export_btn.isHidden()
    kinds = [
        window._export_format.itemData(i)
        for i in range(window._export_format.count())
    ]
    assert kinds == ["drawio", "pdf", "png", "svg"]

    out = tmp_path / "window.png"
    window.capture(str(out))

    assert out.exists(), "screenshot file was not created"
    width, height = _png_size(str(out))
    assert width > 200 and height > 200, f"screenshot looks empty: {width}x{height}"

    # Sanity: the load actually populated the model.
    assert len(window._schema.tables) == 4
    assert window._table.model().rowCount() == len(ROWS)

    del app  # keep linters quiet; app is a singleton


def test_export_drawio_checks_schema_before_prompt(monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = app_module.MainWindow()

    info_calls = []

    def _info(*args, **kwargs):
        info_calls.append((args, kwargs))
        return app_module.QMessageBox.Ok

    def _unexpected_dialog(*args, **kwargs):
        raise AssertionError("File dialog should not be opened when schema is empty.")

    monkeypatch.setattr(app_module.QMessageBox, "information", _info)
    monkeypatch.setattr(app_module.QFileDialog, "getSaveFileName", _unexpected_dialog)
    window._export_format.setCurrentIndex(0)  # drawio
    window.export_diagram()
    assert info_calls, "Expected an informational prompt for empty schema."

    window._diagram_view.cleanup()
    del app


def test_export_rendered_format_requires_webengine(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])
    sample = tmp_path / "sample.xlsx"
    _ensure_sample(str(sample))

    window = app_module.MainWindow()
    window.load_file(str(sample))
    window._diagram_view.available = False
    window._render_selection()

    info_calls = []

    def _info(*args, **kwargs):
        info_calls.append((args, kwargs))
        return app_module.QMessageBox.Ok

    def _unexpected_dialog(*args, **kwargs):
        raise AssertionError("File dialog should not open when WebEngine is unavailable.")

    monkeypatch.setattr(app_module.QMessageBox, "information", _info)
    monkeypatch.setattr(app_module.QFileDialog, "getSaveFileName", _unexpected_dialog)
    window._export_format.setCurrentIndex(2)  # pdf
    window.export_diagram()
    assert info_calls, "Expected an informational prompt when WebEngine is unavailable."

    window._diagram_view.cleanup()
    del app


def test_diagram_screenshot(tmp_path):
    """Render the actual Mermaid ER diagram to PNG and SVG (needs WebEngine)."""
    import diagram_view

    if not diagram_view.WEBENGINE_AVAILABLE:
        pytest.skip("PySide6 WebEngine not available")

    app = QApplication.instance() or QApplication([])

    sample = tmp_path / "sample.xlsx"
    _ensure_sample(str(sample))

    window = app_module.MainWindow()
    window.load_file(str(sample))

    png = tmp_path / "diagram.png"
    window.capture_diagram(str(png))
    assert png.exists()
    width, height = _png_size(str(png))
    assert width > 100 and height > 100, f"diagram PNG looks empty: {width}x{height}"

    svg = tmp_path / "diagram.svg"
    window.capture_diagram(str(svg))
    text = svg.read_text(encoding="utf-8")
    assert text.lstrip().startswith("<svg")
    # The rendered diagram should mention the tables from the sample.
    assert "OrderLine" in text and "Customer" in text

    window._diagram_view.cleanup()
    del app


def test_shortest_path_popup_for_more_than_two_selected(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])

    sample = tmp_path / "sample.xlsx"
    _ensure_sample(str(sample))

    window = app_module.MainWindow()
    window.load_file(str(sample))
    window._selector.clear_selection()
    window._selector.check_tables(["Customer", "Order", "Product"])

    calls = []

    def _fake_get_item(_parent, title, _label, items, _index, _editable):
        calls.append((title, list(items)))
        if "starting" in title.lower():
            return ("Customer", True)
        return ("Product", True)

    monkeypatch.setattr(app_module.QInputDialog, "getItem", _fake_get_item)
    window._find_shortest_path()

    selected = set(window._selector.selected_tables())
    assert "OrderLine" in selected
    assert len(calls) == 2
    assert calls[0][1] == ["Customer", "Order", "Product"]

    window._diagram_view.cleanup()
    del app


def test_shortest_path_popup_cancel_start_keeps_selection(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])

    sample = tmp_path / "sample.xlsx"
    _ensure_sample(str(sample))

    window = app_module.MainWindow()
    window.load_file(str(sample))
    window._selector.clear_selection()
    window._selector.check_tables(["Customer", "Order", "Product"])
    before = set(window._selector.selected_tables())

    def _cancel_start(_parent, _title, _label, _items, _index, _editable):
        return ("", False)

    monkeypatch.setattr(app_module.QInputDialog, "getItem", _cancel_start)
    window._find_shortest_path()
    after = set(window._selector.selected_tables())
    assert after == before

    window._diagram_view.cleanup()
    del app


def test_shortest_path_popup_cancel_destination_keeps_selection(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])

    sample = tmp_path / "sample.xlsx"
    _ensure_sample(str(sample))

    window = app_module.MainWindow()
    window.load_file(str(sample))
    window._selector.clear_selection()
    window._selector.check_tables(["Customer", "Order", "Product"])
    before = set(window._selector.selected_tables())

    calls = {"count": 0}

    def _cancel_destination(_parent, title, _label, _items, _index, _editable):
        calls["count"] += 1
        if "starting" in title.lower():
            return ("Customer", True)
        return ("", False)

    monkeypatch.setattr(app_module.QInputDialog, "getItem", _cancel_destination)
    window._find_shortest_path()
    after = set(window._selector.selected_tables())
    assert calls["count"] == 2
    assert after == before

    window._diagram_view.cleanup()
    del app


def test_module_import_sets_webengine_flags_for_cli_mode():
    """`--screenshot-diagram` should preconfigure WebEngine flags at import time."""
    cmd = [
        sys.executable,
        "-c",
        (
            "import os, sys; "
            "sys.argv=['main.py','sample.xlsx','--screenshot-diagram','diagram.png']; "
            "import main; "
            "print(os.environ.get('QT_QPA_PLATFORM','')); "
            "print(os.environ.get('QTWEBENGINE_DISABLE_SANDBOX','')); "
            "print(os.environ.get('QTWEBENGINE_CHROMIUM_FLAGS','')); "
        ),
    ]
    env = {
        k: v
        for k, v in os.environ.items()
        if k != "QT_QPA_PLATFORM" and not k.startswith("QTWEBENGINE_")
    }
    proc = subprocess.run(
        cmd,
        cwd=os.path.dirname(__file__),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    lines = [line.strip() for line in proc.stdout.splitlines()]
    assert lines[:3] == [
        "offscreen",
        "1",
        "--no-sandbox --disable-gpu --in-process-gpu",
    ]


if __name__ == "__main__":
    from pathlib import Path

    with tempfile.TemporaryDirectory() as directory:
        test_window_screenshot(Path(directory))
        print("PASS  test_window_screenshot")
    with tempfile.TemporaryDirectory() as directory:
        test_diagram_screenshot(Path(directory))
        print("PASS  test_diagram_screenshot")
