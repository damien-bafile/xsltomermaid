"""In-app Mermaid rendering: a Qt widget that draws the ER diagram and can save
it as SVG or PNG.

Rendering is done by a headless-capable ``QWebEngineView`` running a locally
vendored ``mermaid.min.js`` (no network needed). The rendered ``<svg>`` is read
back out of the page, which lets us:

* show the diagram live in a tab, and
* save it as a crisp ``.svg`` or rasterise it to ``.png`` with ``QtSvg`` — which
  works even headless (``QWebEngineView.grab()`` does not capture web content in
  offscreen mode, so we deliberately avoid it).

If PySide6's WebEngine module isn't installed, :class:`DiagramView` degrades to a
message and :attr:`DiagramView.available` is ``False``.
"""

from __future__ import annotations

import html
import math
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote
import xml.etree.ElementTree as ET


@dataclass
class RenderStyle:
    """Visual/layout options passed to Mermaid's ``mermaid.initialize``.

    These map onto Mermaid v10's global config and its ``er`` block, so changing
    them only needs a re-render of the same diagram text (no regeneration).
    """

    theme: str = "default"  # default | neutral | dark | forest | base
    background: str = "#ffffff"  # page background (CSS colour or "transparent")
    layout_direction: str = "LR"  # TB | LR | BT | RL
    entity_padding: int = 15
    min_entity_width: int = 100
    min_entity_height: int = 75
    use_max_width: bool = True
    font_size: int = 12

from PySide6.QtCore import QByteArray, QEventLoop, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget


def resource_path(relative: str) -> Path:
    """Resolve a bundled data file, both in dev and inside a PyInstaller build.

    When frozen, PyInstaller unpacks data files under ``sys._MEIPASS``; in a normal
    checkout they sit next to this module.
    """
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / relative
    return Path(__file__).resolve().parent / relative


VENDOR_MERMAID = resource_path("vendor/mermaid.min.js")

try:
    from PySide6.QtWebEngineCore import QWebEngineSettings
    from PySide6.QtWebEngineWidgets import QWebEngineView

    WEBENGINE_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on PySide6-Addons being present
    WEBENGINE_AVAILABLE = False


def _diagram_html(mermaid_text: str, style: RenderStyle | None = None) -> str:
    """HTML that renders ``mermaid_text`` using the sibling ``mermaid.min.js``."""
    style = style or RenderStyle()
    er_config = (
        f"layoutDirection:'{style.layout_direction}',"
        f"entityPadding:{style.entity_padding},"
        f"minEntityWidth:{style.min_entity_width},"
        f"minEntityHeight:{style.min_entity_height},"
        f"useMaxWidth:{'true' if style.use_max_width else 'false'},"
        f"fontSize:{style.font_size}"
    )
    # The diagram text is placed inside <pre> verbatim; Mermaid reads textContent.
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
  html, body {{ margin: 0; padding: 12px; background: {style.background}; }}
  .mermaid {{ font-family: "Trebuchet MS", Verdana, Arial, sans-serif; }}
</style>
<script src="mermaid.min.js"></script>
</head>
<body>
<pre class="mermaid">{mermaid_text}</pre>
<script>
  window._mermaidDone = false;
  window._mermaidError = null;
  try {{
    mermaid.initialize({{ startOnLoad: false, securityLevel: 'loose', theme: '{style.theme}',
      maxTextSize: 2000000, maxEdges: 10000, er: {{ {er_config} }} }});
    mermaid.run().then(function () {{ window._mermaidDone = true; }})
      .catch(function (e) {{ window._mermaidError = String(e); window._mermaidDone = true; }});
  }} catch (e) {{
    window._mermaidError = String(e); window._mermaidDone = true;
  }}
</script>
</body></html>
"""


# Caps so a huge diagram can't ask for a multi-gigabyte QImage (which freezes
# or crashes the app). We scale the export down to fit within these instead.
MAX_PNG_DIM = 20000  # max width/height in pixels
MAX_PNG_PIXELS = 60_000_000  # ~240 MB at 4 bytes/pixel


def _fit_scale(width: float, height: float, scale: float) -> float:
    """Clamp ``scale`` so the rasterised image stays within the size caps."""
    if width <= 0 or height <= 0:
        return scale
    scale = min(scale, MAX_PNG_DIM / width, MAX_PNG_DIM / height)
    if (width * scale) * (height * scale) > MAX_PNG_PIXELS:
        scale = math.sqrt(MAX_PNG_PIXELS / (width * height))
    return scale


def svg_to_png(
    svg: str, path: str, scale: float = 2.0, background: str = "white"
) -> float:
    """Rasterise an SVG string to a PNG file using QtSvg (works headless).

    Returns the scale actually used, which may be smaller than requested when
    the diagram is large enough that the full-scale image would blow past the
    size caps above.
    """
    from PySide6.QtSvg import QSvgRenderer

    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    size = renderer.defaultSize()
    effective = _fit_scale(size.width(), size.height(), scale)
    width = max(int(size.width() * effective), 1)
    height = max(int(size.height() * effective), 1)

    image = QImage(width, height, QImage.Format_ARGB32)
    if image.isNull():
        raise RuntimeError(
            "The diagram is too large to rasterise to PNG. Save as SVG instead, "
            "or render fewer tables/columns."
        )
    image.fill(QColor(background))
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()

    if not image.save(path, "PNG"):
        raise RuntimeError(f"Failed to save PNG to {path}")
    return effective


def _svg_dimensions(svg: str) -> tuple[float, float]:
    """Best-effort width/height read from SVG attributes or viewBox."""

    def _number(text: str | None) -> float | None:
        if not text:
            return None
        value = str(text).strip()
        value = re.sub(r"(px|pt|pc|cm|mm|in)$", "", value, flags=re.IGNORECASE)
        try:
            return float(value)
        except ValueError:
            return None

    width = height = None
    try:
        root = ET.fromstring(svg)
        width = _number(root.attrib.get("width"))
        height = _number(root.attrib.get("height"))
        if width is None or height is None:
            view_box = root.attrib.get("viewBox", "")
            parts = [p for p in re.split(r"[\s,]+", view_box.strip()) if p]
            if len(parts) == 4:
                width = width if width is not None else _number(parts[2])
                height = height if height is not None else _number(parts[3])
    except ET.ParseError:
        pass
    return (
        width if width is not None else 1200.0,
        height if height is not None else 800.0,
    )


def svg_to_drawio(svg: str, page_name: str = "Page-1") -> str:
    """Wrap an SVG as a Draw.io diagram containing one image cell."""
    width, height = _svg_dimensions(svg)
    encoded_svg = quote(svg, safe="")
    style = (
        f"shape=image;verticalLabelPosition=bottom;verticalAlign=top;aspect=fixed;"
        f"imageAspect=0;image=data:image/svg+xml,{encoded_svg};"
    )

    mxfile = ET.Element(
        "mxfile",
        {
            "host": "app.diagrams.net",
            "compressed": "false",
        },
    )
    diagram = ET.SubElement(mxfile, "diagram", {"id": "diagram-1", "name": page_name})
    graph = ET.SubElement(
        diagram,
        "mxGraphModel",
        {
            "dx": "1200",
            "dy": "800",
            "grid": "1",
            "gridSize": "10",
            "guides": "1",
            "tooltips": "1",
            "connect": "1",
            "arrows": "1",
            "fold": "1",
            "page": "1",
            "pageScale": "1",
            "pageWidth": "827",
            "pageHeight": "1169",
            "math": "0",
            "shadow": "0",
        },
    )
    root = ET.SubElement(graph, "root")
    ET.SubElement(root, "mxCell", {"id": "0"})
    ET.SubElement(root, "mxCell", {"id": "1", "parent": "0"})
    image = ET.SubElement(
        root,
        "mxCell",
        {
            "id": "2",
            "value": "",
            "style": style,
            "vertex": "1",
            "parent": "1",
        },
    )
    ET.SubElement(
        image,
        "mxGeometry",
        {
            "x": "0",
            "y": "0",
            "width": f"{max(width, 1.0):g}",
            "height": f"{max(height, 1.0):g}",
            "as": "geometry",
        },
    )
    return ET.tostring(mxfile, encoding="unicode")


class DiagramView(QWidget):
    """A tab that renders a Mermaid ER diagram and can export it as SVG/PNG."""

    # Emitted when a diagram starts loading, and when Mermaid finishes (or
    # fails) rendering it — so the UI can show a spinner then a tick.
    render_started = Signal()
    render_finished = Signal(bool)  # True on success, False on error/timeout

    def __init__(self, parent=None):
        super().__init__(parent)
        self.available = WEBENGINE_AVAILABLE
        self._workdir: str | None = None
        self._view = None
        self._render_gen = 0  # bumped per load so stale polls are ignored
        self._expect_render = False  # True for a diagram, False for a message

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        if WEBENGINE_AVAILABLE:
            self._workdir = tempfile.mkdtemp(prefix="xsltomermaid_")
            shutil.copy(VENDOR_MERMAID, Path(self._workdir) / "mermaid.min.js")
            self._view = QWebEngineView(self)
            self._view.settings().setAttribute(
                QWebEngineSettings.LocalContentCanAccessFileUrls, True
            )
            self._view.loadFinished.connect(self._on_load_finished)
            layout.addWidget(self._view)
        else:
            label = QLabel(
                "The rendered-diagram view needs PySide6's WebEngine module.\n"
                "Install it with:  uv add PySide6-Addons\n\n"
                "The Mermaid source (other tab) and text export still work."
            )
            label.setAlignment(Qt.AlignCenter)
            label.setWordWrap(True)
            label.setStyleSheet("color: #6b7078;")
            layout.addWidget(label)

    # -- rendering ---------------------------------------------------------
    def set_diagram(self, mermaid_text: str, style: RenderStyle | None = None):
        """Load a diagram into the view (async render)."""
        if not self.available or self._view is None or self._workdir is None:
            return
        self._render_gen += 1
        self._expect_render = True
        self.render_started.emit()
        html_path = Path(self._workdir) / "diagram.html"
        html_path.write_text(_diagram_html(mermaid_text, style), encoding="utf-8")
        self._view.load(QUrl.fromLocalFile(str(html_path)))

    def _on_load_finished(self, ok: bool):
        """Once the diagram page has loaded, poll until Mermaid signals done."""
        if not self._expect_render:
            return  # a message page, not a diagram
        if not ok:
            self.render_finished.emit(False)
            return
        self._poll_mermaid(self._render_gen, 0)

    def _poll_mermaid(self, gen: int, elapsed: int):
        if gen != self._render_gen or self._view is None:
            return  # superseded by a newer render
        if elapsed >= 60000:  # give up after 60s
            self.render_finished.emit(False)
            return

        def on_done(done):
            if gen != self._render_gen:
                return
            if done:
                self._view.page().runJavaScript(
                    "window._mermaidError || ''",
                    lambda err: self.render_finished.emit(not err),
                )
            else:
                QTimer.singleShot(
                    150, lambda: self._poll_mermaid(gen, elapsed + 150)
                )

        self._view.page().runJavaScript("window._mermaidDone === true", on_done)

    def show_message(self, message: str):
        """Show a plain text message in place of a diagram (e.g. a hint)."""
        if not self.available or self._view is None or self._workdir is None:
            return
        # Invalidate any in-flight render poll; this isn't a diagram.
        self._render_gen += 1
        self._expect_render = False
        page = (
            "<!DOCTYPE html><html><head><meta charset='utf-8'><style>"
            "html,body{margin:0;padding:32px;background:#ffffff;color:#6b7078;"
            "font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;"
            "font-size:15px;line-height:1.5}</style></head><body>"
            f"{html.escape(message)}</body></html>"
        )
        html_path = Path(self._workdir) / "message.html"
        html_path.write_text(page, encoding="utf-8")
        self._view.load(QUrl.fromLocalFile(str(html_path)))

    def _run_js(self, script: str, timeout_ms: int = 5000):
        loop = QEventLoop()
        box: dict = {}
        self._view.page().runJavaScript(script, lambda r: (box.update(r=r), loop.quit()))
        QTimer.singleShot(timeout_ms, loop.quit)
        loop.exec()
        return box.get("r")

    def current_svg(self, timeout_ms: int = 60000) -> str | None:
        """Block until Mermaid finishes, then return the rendered ``<svg>`` markup.

        Returns ``None`` if WebEngine is unavailable or rendering times out.
        """
        if not self.available or self._view is None:
            return None
        deadline = timeout_ms
        step = 150
        elapsed = 0
        while elapsed < deadline:
            done = self._run_js("window._mermaidDone === true", timeout_ms=2000)
            if done:
                break
            pause = QEventLoop()
            QTimer.singleShot(step, pause.quit)
            pause.exec()
            elapsed += step

        svg = self._run_js(
            "(function(){var s=document.querySelector('.mermaid svg');"
            "return s ? s.outerHTML : '';})()",
            timeout_ms=3000,
        )
        return svg or None

    # -- exporting ---------------------------------------------------------
    def save_svg(self, path: str) -> str:
        svg = self.current_svg()
        if not svg:
            raise RuntimeError("No rendered diagram available to save.")
        Path(path).write_text(svg, encoding="utf-8")
        return path

    def save_png(self, path: str, scale: float = 2.0, background: str = "white") -> float:
        """Save the rendered diagram as PNG; returns the scale actually used."""
        svg = self.current_svg()
        if not svg:
            raise RuntimeError("No rendered diagram available to save.")
        return svg_to_png(svg, path, scale=scale, background=background)

    def save_drawio(self, path: str) -> str:
        """Save the rendered diagram as a Draw.io (.drawio) file."""
        svg = self.current_svg()
        if not svg:
            raise RuntimeError("No rendered diagram available to save.")
        Path(path).write_text(svg_to_drawio(svg), encoding="utf-8")
        return path

    def cleanup(self):
        if self._workdir:
            shutil.rmtree(self._workdir, ignore_errors=True)
            self._workdir = None
