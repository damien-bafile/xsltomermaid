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


def test_svg_to_drawio_wraps_svg_image():
    import diagram_view

    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 480">'
        '<rect width="640" height="480" fill="white"/></svg>'
    )
    drawio = diagram_view.svg_to_drawio(svg)
    root = ET.fromstring(drawio)
    image_cell = root.find(".//mxCell[@id='2']")
    geometry = root.find(".//mxCell[@id='2']/mxGeometry")

    assert root.tag == "mxfile"
    assert image_cell is not None
    style = image_cell.attrib.get("style", "")
    assert isinstance(style, str)
    assert style.startswith("shape=image;")
    assert "data:image/svg+xml," in style
    assert "('" not in style and "'," not in style
    assert geometry is not None
    assert geometry.attrib.get("width") == "640"
    assert geometry.attrib.get("height") == "480"
    assert "%2Fsvg%3E" in style


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


def test_svg_to_drawio_keeps_non_integer_geometry():
    import diagram_view

    drawio = diagram_view.svg_to_drawio('<svg width="640.5" height="480.25"></svg>')
    root = ET.fromstring(drawio)
    geometry = root.find(".//mxCell[@id='2']/mxGeometry")
    assert geometry is not None
    assert geometry.attrib.get("width") == "640.5"
    assert geometry.attrib.get("height") == "480.25"


def test_save_drawio_writes_file(tmp_path):
    from diagram_view import DiagramView

    class _DummyView:
        def current_svg(self):
            return '<svg width="10" height="20"></svg>'

    out = tmp_path / "diagram.drawio"
    DiagramView.save_drawio(_DummyView(), str(out))
    text = out.read_text(encoding="utf-8")
    assert "<mxfile" in text
    assert "data:image/svg+xml," in text


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
    assert window._png_scale.minimum() == 1
    assert window._png_scale.maximum() == 10
    assert window._png_scale.value() == 2
    assert window._png_scale_value.text() == "2×"
    window._png_scale.setValue(7)
    assert window._png_scale_value.text() == "7×"
    if window._diagram_view.available:
        assert not window._save_drawio_btn.isHidden()
    else:
        assert window._save_drawio_btn.isHidden()

    out = tmp_path / "window.png"
    window.capture(str(out))

    assert out.exists(), "screenshot file was not created"
    width, height = _png_size(str(out))
    assert width > 200 and height > 200, f"screenshot looks empty: {width}x{height}"

    # Sanity: the load actually populated the model.
    assert len(window._schema.tables) == 4
    assert window._table.rowCount() == len(ROWS)

    del app  # keep linters quiet; app is a singleton


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
