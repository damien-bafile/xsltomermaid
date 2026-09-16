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
import shutil
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QByteArray, QEventLoop, Qt, QTimer, QUrl
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


def _diagram_html(mermaid_text: str, theme: str = "default") -> str:
    """HTML that renders ``mermaid_text`` using the sibling ``mermaid.min.js``."""
    # The diagram text is placed inside <pre> verbatim; Mermaid reads textContent.
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
  html, body {{ margin: 0; padding: 12px; background: #ffffff; }}
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
    mermaid.initialize({{ startOnLoad: false, securityLevel: 'loose', theme: '{theme}',
      maxTextSize: 2000000, maxEdges: 10000 }});
    mermaid.run().then(function () {{ window._mermaidDone = true; }})
      .catch(function (e) {{ window._mermaidError = String(e); window._mermaidDone = true; }});
  }} catch (e) {{
    window._mermaidError = String(e); window._mermaidDone = true;
  }}
</script>
</body></html>
"""


def svg_to_png(svg: str, path: str, scale: float = 2.0, background: str = "white") -> None:
    """Rasterise an SVG string to a PNG file using QtSvg (works headless)."""
    from PySide6.QtSvg import QSvgRenderer

    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    size = renderer.defaultSize()
    width = max(int(size.width() * scale), 1)
    height = max(int(size.height() * scale), 1)

    image = QImage(width, height, QImage.Format_ARGB32)
    image.fill(QColor(background))
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()

    if not image.save(path, "PNG"):
        raise RuntimeError(f"Failed to save PNG to {path}")


class DiagramView(QWidget):
    """A tab that renders a Mermaid ER diagram and can export it as SVG/PNG."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.available = WEBENGINE_AVAILABLE
        self._workdir: str | None = None
        self._view = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        if WEBENGINE_AVAILABLE:
            self._workdir = tempfile.mkdtemp(prefix="xsltomermaid_")
            shutil.copy(VENDOR_MERMAID, Path(self._workdir) / "mermaid.min.js")
            self._view = QWebEngineView(self)
            self._view.settings().setAttribute(
                QWebEngineSettings.LocalContentCanAccessFileUrls, True
            )
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
    def set_diagram(self, mermaid_text: str, theme: str = "default"):
        """Load a diagram into the view (async render)."""
        if not self.available or self._view is None or self._workdir is None:
            return
        html_path = Path(self._workdir) / "diagram.html"
        html_path.write_text(_diagram_html(mermaid_text, theme), encoding="utf-8")
        self._view.load(QUrl.fromLocalFile(str(html_path)))

    def show_message(self, message: str):
        """Show a plain text message in place of a diagram (e.g. a hint)."""
        if not self.available or self._view is None or self._workdir is None:
            return
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

    def save_png(self, path: str, scale: float = 2.0) -> str:
        svg = self.current_svg()
        if not svg:
            raise RuntimeError("No rendered diagram available to save.")
        svg_to_png(svg, path, scale=scale)
        return path

    def cleanup(self):
        if self._workdir:
            shutil.rmtree(self._workdir, ignore_errors=True)
            self._workdir = None
