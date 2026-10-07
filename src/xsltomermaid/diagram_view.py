"""In-app Mermaid rendering: a Qt widget that draws the ER diagram and can save
it as SVG, PNG, or PDF.

Rendering is done by a headless-capable ``QWebEngineView`` running a locally
vendored ``mermaid.min.js`` (no network needed). The rendered ``<svg>`` is read
back out of the page, which lets us:

* show the diagram live in a tab, and
* save it as a crisp ``.svg`` or rasterise it to ``.png`` / ``.pdf`` with Qt —
  works even headless (``QWebEngineView.grab()`` does not capture web content in
  offscreen mode, so we deliberately avoid it).

If PySide6's WebEngine module isn't installed, :class:`DiagramView` degrades to a
message and :attr:`DiagramView.available` is ``False``.
"""

from __future__ import annotations

import html
import json
import math
import re
import shutil
import sys
import tempfile
import time
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

from PySide6.QtCore import (
    QByteArray,
    QEventLoop,
    QMarginsF,
    QRectF,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import QColor, QImage, QPainter, QPageSize, QPalette, QPdfWriter
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


def resource_path(relative: str) -> Path:
    """Resolve a bundled data file, both in dev and inside a PyInstaller build.

    When frozen, PyInstaller unpacks data files under ``sys._MEIPASS``; otherwise
    they are installed alongside this package.
    """
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / relative
    return Path(__file__).resolve().parent / relative


VENDOR_MERMAID = resource_path("vendor/mermaid.min.js")

try:
    from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
    from PySide6.QtWebEngineWidgets import QWebEngineView

    WEBENGINE_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on PySide6-Addons being present
    WEBENGINE_AVAILABLE = False

# The diagram page reports clicks by logging "xsltomermaid:<kind>:<entity id>";
# a tiny bridge that needs no extra script (QWebChannel would need qwebchannel.js).
_BRIDGE_PREFIX = "xsltomermaid:"
# Mermaid's ER renderer gives each table's group the id
# "entity-<our entity id>-<uuid>"; our ids are [0-9A-Za-z_] only.
_ENTITY_GROUP_RE = re.compile(r"^entity-(.+)-[0-9a-f]{8}-[0-9a-f-]{27}$")


def entity_id_from_group(group_id: str) -> str | None:
    """``"entity-Customer-7db3…"`` → ``"Customer"`` (None if it isn't one)."""
    match = _ENTITY_GROUP_RE.match(group_id or "")
    return match.group(1) if match else None


if WEBENGINE_AVAILABLE:

    class _BridgePage(QWebEnginePage):
        """Forwards the diagram's bridge messages; everything else is ignored."""

        bridged = Signal(str, str)  # kind ("click" | "dblclick"), group id

        def javaScriptConsoleMessage(self, level, message, line, source):  # noqa: N802
            if message.startswith(_BRIDGE_PREFIX):
                kind, _, group = message[len(_BRIDGE_PREFIX):].partition(":")
                self.bridged.emit(kind, group)


def _mermaid_config(style: RenderStyle) -> dict:
    """The ``mermaid.initialize`` config for a RenderStyle (theme + er block)."""
    return {
        "startOnLoad": False,
        "securityLevel": "loose",
        "theme": style.theme,
        "maxTextSize": 2000000,
        "maxEdges": 10000,
        "er": {
            "layoutDirection": style.layout_direction,
            "entityPadding": style.entity_padding,
            "minEntityWidth": style.min_entity_width,
            "minEntityHeight": style.min_entity_height,
            "useMaxWidth": style.use_max_width,
            "fontSize": style.font_size,
        },
    }


def _shell_html() -> str:
    """A page loaded once that keeps mermaid.js resident and re-renders on demand.

    ``window.renderDiagram(text, config, background, canvas)`` re-initialises
    Mermaid with the given config and draws ``text`` into ``#container`` — which
    keeps the ``mermaid`` class so the SVG-export selector (``.mermaid svg``)
    still finds it — without reloading the ~3 MB library. It sets
    ``window._mermaidDone`` / ``window._mermaidError`` for the poller, and a
    sequence number so a stale async result from a superseded render is ignored.

    ``background`` goes on ``body`` (exports read it back from there); the page
    behind it (``html``) takes the same colour so the diagram fills the whole
    view, or ``canvas`` (the app's palette) when the background is transparent.
    """
    return """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
  html { height: 100%; background: #ffffff; }
  /* Flex + auto margins centre a small diagram; a large one simply overflows
     right/down and scrolls, without being clipped on the left or top. */
  body { margin: 0; padding: 12px; min-height: 100%; box-sizing: border-box;
         background: #ffffff; display: flex; }
  #container { margin: auto; font-family: "Trebuchet MS", Verdana, Arial, sans-serif; }
  /* The selected table: a glow that doesn't move the layout. Page CSS, so
     exports (which copy only inline attributes) never carry it. */
  #container g[id^="entity-"] { cursor: pointer; }
  #container g.xsel { filter: drop-shadow(0 0 2px #2f81f7) drop-shadow(0 0 4px #2f81f7); }
</style>
<script src="mermaid.min.js"></script>
</head>
<body>
<div id="container" class="mermaid"></div>
<script>
  window._mermaidDone = false;
  window._mermaidError = null;
  window._renderSeq = 0;
  window._fit = true;
  window._actual = false;  // Ctrl+0: true 1:1 until Fit is pressed
  window._lastScale = 1;   // the scale drawn, read by the zoom readout
  // Size the diagram to the view: up to 150% when it's small, down when it's
  // big. "force" fits once even when the Fit option is off (the Fit button).
  window.fitDiagram = function (force) {
    var svg = document.querySelector('#container svg');
    if (!svg) return;
    var vb = svg.viewBox && svg.viewBox.baseVal;
    if (!vb || !vb.width || !vb.height) return;
    if (force) window._actual = false;
    var scale = 1;
    if (!window._actual && (force || window._fit)) {
      var w = window.innerWidth - 24, h = window.innerHeight - 24;
      scale = Math.min(w / vb.width, h / vb.height, 1.5);
      // Below three-quarter size the column text (12px) drops under about
      // 9px, so automatic fitting stops there and the view scrolls (centred
      // on the busiest table); the Fit button (force) still shows it all.
      scale = Math.max(force ? 0.05 : 0.75, scale);
    }
    svg.style.maxWidth = 'none';
    svg.style.width = (vb.width * scale) + 'px';
    svg.style.height = (vb.height * scale) + 'px';
    window._lastScale = scale;
  };
  window.actualSize = function () {
    window._actual = true;
    window.fitDiagram(false);
  };
  window.addEventListener('resize', function () { window.fitDiagram(false); });
  // Click a table to select it, double-click to open it, click the canvas or
  // press Esc to clear. Messages go to Python through the console bridge.
  window.selectEntity = function (groupId) {
    document.querySelectorAll('#container g.xsel').forEach(function (g) {
      g.classList.remove('xsel');
    });
    if (!groupId) return;
    var g = document.getElementById(groupId);
    if (g) {
      g.classList.add('xsel');
      if (g.scrollIntoViewIfNeeded) g.scrollIntoViewIfNeeded(true);
    }
  };
  window.selectEntityByName = function (entityId) {
    var g = document.querySelector('#container g[id^="entity-' + entityId + '-"]');
    window.selectEntity(g ? g.id : '');
    return !!g;
  };
  function report(kind, event) {
    var g = event.target.closest && event.target.closest('#container g[id^="entity-"]');
    window.selectEntity(g ? g.id : '');
    console.log('xsltomermaid:' + kind + ':' + (g ? g.id : ''));
  }
  document.addEventListener('click', function (e) { report('click', e); });
  document.addEventListener('dblclick', function (e) { report('dblclick', e); });
  // Keyboard twin of clicking: arrows move to the nearest table in that
  // direction, Enter opens the selected one, Esc clears.
  function centre(g) {
    var r = g.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
  }
  function neighbour(from, key) {
    var groups = Array.prototype.slice.call(
      document.querySelectorAll('#container g[id^="entity-"]'));
    if (!groups.length) return null;
    if (!from) return groups[0];
    var a = centre(from), best = null, bestScore = Infinity;
    groups.forEach(function (g) {
      if (g === from) return;
      var b = centre(g), dx = b.x - a.x, dy = b.y - a.y;
      var along = { ArrowRight: dx, ArrowLeft: -dx, ArrowDown: dy, ArrowUp: -dy }[key];
      var across = (key === 'ArrowRight' || key === 'ArrowLeft') ? Math.abs(dy) : Math.abs(dx);
      if (along <= 1) return;  // not in that direction
      var score = along + 2 * across;  // prefer straight ahead
      if (score < bestScore) { bestScore = score; best = g; }
    });
    return best;
  }
  document.addEventListener('keydown', function (e) {
    var current = document.querySelector('#container g.xsel');
    if (e.key === 'Escape') { window.selectEntity(''); console.log('xsltomermaid:click:'); return; }
    if (e.key === 'Enter' && current) {
      console.log('xsltomermaid:dblclick:' + current.id); e.preventDefault(); return;
    }
    if (['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].indexOf(e.key) < 0) return;
    var next = neighbour(current, e.key);
    if (next) {
      window.selectEntity(next.id);
      console.log('xsltomermaid:click:' + next.id);
      e.preventDefault();
    }
  });
  // When the diagram is bigger than the view even at the fit floor, start
  // with the busiest table in the middle rather than the top-left corner.
  window.centreOnEntity = function (entityId) {
    var svg = document.querySelector('#container svg');
    if (!entityId || !svg) return false;
    var r = svg.getBoundingClientRect();
    if (r.width <= window.innerWidth && r.height <= window.innerHeight) return false;
    var g = document.querySelector('#container g[id^="entity-' + entityId + '-"]');
    if (!g) return false;
    // A table taller than the view shows its title first, not its middle.
    var tall = g.getBoundingClientRect().height > window.innerHeight - 24;
    g.scrollIntoView({ block: tall ? 'start' : 'center', inline: 'center' });
    return true;
  };
  window.renderDiagram = function (text, config, background, canvas, fit, focus) {
    var seq = ++window._renderSeq;
    window._mermaidDone = false;
    window._mermaidError = null;
    try {
      document.body.style.background = background;
      document.documentElement.style.background =
        background === 'transparent' ? (canvas || '#ffffff') : background;
      mermaid.initialize(config);
      mermaid.render('erGraph' + seq, text).then(function (res) {
        if (seq !== window._renderSeq) return;   // a newer render superseded us
        document.getElementById('container').innerHTML = res.svg;
        window._fit = fit !== false;
        window.fitDiagram(false);
        window.centreOnEntity(focus);
        window._mermaidDone = true;
      }).catch(function (e) {
        if (seq !== window._renderSeq) return;
        window._mermaidError = String(e); window._mermaidDone = true;
      });
    } catch (e) {
      window._mermaidError = String(e); window._mermaidDone = true;
    }
  };
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


def svg_to_pdf(svg: str, path: str, background: str = "white") -> str:
    """Render an SVG string to a PDF file."""
    from PySide6.QtSvg import QSvgRenderer

    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    size = renderer.defaultSize()
    width = max(float(size.width()), 1.0)
    height = max(float(size.height()), 1.0)
    if width <= 1.0 or height <= 1.0:
        fallback_width, fallback_height = _svg_dimensions(svg)
        width = max(fallback_width, 1.0)
        height = max(fallback_height, 1.0)

    writer = QPdfWriter(path)
    writer.setPageMargins(QMarginsF(0, 0, 0, 0))
    writer.setPageSize(QPageSize(QRectF(0, 0, width, height).size(), QPageSize.Point))
    painter = QPainter(writer)
    if not painter.isActive():
        raise RuntimeError(f"Failed to start PDF painter for {path}")
    if background != "transparent":
        painter.fillRect(QRectF(0, 0, width, height), QColor(background))
    renderer.render(painter, QRectF(0, 0, width, height))
    painter.end()
    return path


def _svg_dimensions(svg: str) -> tuple[float, float]:
    """Best-effort width/height read from SVG attributes or viewBox."""

    def _number(text: str | None) -> float | None:
        if not text:
            return None
        value = str(text).strip()
        match = re.fullmatch(
            r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)([A-Za-z%]*)",
            value,
        )
        if not match:
            return None
        # A relative unit ("100%") is not an absolute pixel size — Mermaid emits
        # width="100%" under useMaxWidth. Reject it so we fall back to the
        # viewBox, which carries the real dimensions.
        if match.group(2) in ("%", "em", "ex"):
            return None
        try:
            return float(match.group(1))
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
            "width": str(max(width, 0.0)),
            "height": str(max(height, 0.0)),
            "as": "geometry",
        },
    )
    return ET.tostring(mxfile, encoding="unicode")


def schema_to_drawio(schema, page_name: str = "Page-1", dark: bool = False) -> str:
    """Build a native Draw.io ER-like diagram from schema tables + relationships.

    ``dark=True`` emits dark-filled boxes, light text/strokes, and a dark page
    background so the diagram is coherent on Draw.io's dark canvas (mirrors
    :func:`schema_to_excalidraw`'s dark option); the default stays light.
    """
    # Box fill / stroke / text / edge colours, and the page background.
    if dark:
        c_fill, c_stroke, c_font, c_edge, c_bg = (
            "#2b2b2b", "#9aa0a6", "#e8eaed", "#9aa0a6", "#1e1e1e",
        )
    else:
        c_fill, c_stroke, c_font, c_edge, c_bg = (
            "#ffffff", "#36393d", "", "", "",
        )
    mxfile = ET.Element(
        "mxfile",
        {
            "host": "app.diagrams.net",
            "compressed": "false",
        },
    )
    diagram = ET.SubElement(mxfile, "diagram", {"id": "diagram-1", "name": page_name})
    graph_attrs = {
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
    }
    if c_bg:
        graph_attrs["background"] = c_bg
    graph = ET.SubElement(diagram, "mxGraphModel", graph_attrs)
    root = ET.SubElement(graph, "root")
    ET.SubElement(root, "mxCell", {"id": "0"})
    ET.SubElement(root, "mxCell", {"id": "1", "parent": "0"})

    tables = list(getattr(schema, "tables", []) or [])
    rels = list(getattr(schema, "relationships", []) or [])
    if not tables:
        return ET.tostring(mxfile, encoding="unicode")

    def _column_line(column) -> str:
        keys = []
        if getattr(column, "is_primary_key", False):
            keys.append("PK")
        if getattr(column, "foreign_key_reference", ""):
            keys.append("FK")
        key_part = f" [{' '.join(keys)}]" if keys else ""
        data_type = (getattr(column, "data_type", "") or "").strip()
        if hasattr(column, "rendered_type"):
            data_type = column.rendered_type()
        typed = f" : {data_type}" if data_type else ""
        return f"{column.name}{typed}{key_part}"

    def _table_value(table) -> str:
        lines: list[str] = []
        for column in table.columns:
            line = html.escape(_column_line(column))
            lines.append(line)
        body = "<br/>".join(lines) if lines else " "
        return f"<b>{html.escape(table.name)}</b><hr/>{body}"

    def _table_size(table) -> tuple[int, int]:
        line_texts = [table.name] + [_column_line(col) for col in table.columns]
        longest = max((len(text) for text in line_texts), default=10)
        width = min(max(220, longest * 7 + 48), 520)
        height = max(80, 40 + len(table.columns) * 18)
        return width, height

    columns = max(1, int(math.ceil(math.sqrt(len(tables)))))
    # Widen the horizontal gap so a long relationship label riding the edge
    # between two boxes doesn't collide with either box. Mirrors the label-aware
    # spacing in the Excalidraw export; ~7px/char matches the width metric
    # _table_size uses for Draw.io's default label font.
    max_label = max((len(getattr(rel, "label", "") or "") for rel in rels), default=0)
    x_spacing = max(80, max_label * 7 + 40)
    y_spacing = 60
    x = 40
    y = 40
    max_height_in_row = 0

    table_ids_exact: dict[str, str | None] = {}
    table_ids_lower: dict[str, str | None] = {}
    table_ids_qualified: dict[str, str | None] = {}
    table_ids_qualified_lower: dict[str, str | None] = {}
    for pos, table in enumerate(tables):
        width, height = _table_size(table)
        table_id = str(pos + 2)
        if table.name in table_ids_exact and table_ids_exact[table.name] != table_id:
            table_ids_exact[table.name] = None
        else:
            table_ids_exact[table.name] = table_id
        lowered = table.name.lower()
        if lowered in table_ids_lower and table_ids_lower[lowered] != table_id:
            table_ids_lower[lowered] = None
        else:
            table_ids_lower[lowered] = table_id
        qualified = getattr(table, "full_name", None) or (
            f"{table.schema}.{table.name}" if getattr(table, "schema", "") else table.name
        )
        qualified = str(qualified)
        if qualified in table_ids_qualified and table_ids_qualified[qualified] != table_id:
            table_ids_qualified[qualified] = None
        else:
            table_ids_qualified[qualified] = table_id
        qualified_lower = qualified.lower()
        if (
            qualified_lower in table_ids_qualified_lower
            and table_ids_qualified_lower[qualified_lower] != table_id
        ):
            table_ids_qualified_lower[qualified_lower] = None
        else:
            table_ids_qualified_lower[qualified_lower] = table_id
        style = (
            "shape=mxgraph.er.entity;whiteSpace=wrap;html=1;align=left;verticalAlign=top;"
            f"spacing=8;rounded=0;strokeColor={c_stroke};fillColor={c_fill};"
            + (f"fontColor={c_font};" if c_font else "")
        )
        cell = ET.SubElement(
            root,
            "mxCell",
            {
                "id": table_id,
                "value": _table_value(table),
                "style": style,
                "vertex": "1",
                "parent": "1",
            },
        )
        ET.SubElement(
            cell,
            "mxGeometry",
            {
                "x": str(x),
                "y": str(y),
                "width": str(width),
                "height": str(height),
                "as": "geometry",
            },
        )

        max_height_in_row = max(max_height_in_row, height)
        if (pos + 1) % columns == 0:
            x = 40
            y += max_height_in_row + y_spacing
            max_height_in_row = 0
        else:
            x += width + x_spacing

    def _lookup_table_id(value) -> str | None:
        if not isinstance(value, str):
            return None
        table_id = table_ids_qualified.get(value)
        if table_id is not None:
            return table_id
        table_id = table_ids_qualified_lower.get(value.lower())
        if table_id is not None:
            return table_id
        table_id = table_ids_exact.get(value)
        if table_id is not None:
            return table_id
        return table_ids_lower.get(value.lower())

    edge_id = len(tables) + 2
    for rel in rels:
        parent_id = _lookup_table_id(getattr(rel, "parent_table", None))
        child_id = _lookup_table_id(getattr(rel, "child_table", None))
        if not parent_id or not child_id:
            continue
        edge = ET.SubElement(
            root,
            "mxCell",
            {
                "id": str(edge_id),
                "value": getattr(rel, "label", "") or "",
                "style": (
                    "edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;"
                    "html=1;startArrow=ERone;endArrow=ERmany;startFill=1;endFill=1;"
                    + (f"strokeColor={c_edge};fontColor={c_font};" if c_edge else "")
                ),
                "edge": "1",
                "parent": "1",
                "source": parent_id,
                "target": child_id,
            },
        )
        # Draw.io's file-open decoder requires an edge to carry a geometry; without
        # it the whole page decodes to empty (the file opens blank), even though
        # dragging the file in as an import still works. Give every edge the
        # standard relative geometry.
        ET.SubElement(
            edge, "mxGeometry", {"relative": "1", "as": "geometry"}
        )
        edge_id += 1

    return ET.tostring(mxfile, encoding="unicode")


# ---------------------------------------------------------------------------
# Excalidraw (.excalidraw) export — a simple, stable JSON scene
# ---------------------------------------------------------------------------
# Tables become rectangles with bound (left/top-aligned) text; relationships
# become arrows glued to the boxes with the foreign-key column as the label.
# Crisp style (roughness 0), monospaced text so the column lists line up.
_EXCALI_FONT = 3  # 1=hand-drawn, 2=normal, 3=code/monospace
_EXCALI_FONT_SIZE = 16
_EXCALI_LINE_H = 1.25
_EXCALI_CHAR_W = 9.6  # ~advance width of the mono font at size 16
_EXCALI_PAD = 10


def _excali_line(column) -> str:
    keys = []
    if getattr(column, "is_primary_key", False):
        keys.append("PK")
    if getattr(column, "foreign_key_reference", ""):
        keys.append("FK")
    key_part = f" [{' '.join(keys)}]" if keys else ""
    data_type = (getattr(column, "data_type", "") or "").strip()
    if hasattr(column, "rendered_type"):
        data_type = column.rendered_type()
    typed = f" : {data_type}" if data_type else ""
    return f"{column.name}{typed}{key_part}"


def _excali_common(eid: str, etype: str, x: float, y: float, w: float, h: float,
                   seed: int, stroke: str = "#1e1e1e") -> dict:
    return {
        "id": eid,
        "type": etype,
        "x": round(x, 2),
        "y": round(y, 2),
        "width": round(w, 2),
        "height": round(h, 2),
        "angle": 0,
        "strokeColor": stroke,
        "backgroundColor": "transparent",
        "fillStyle": "solid",
        "strokeWidth": 1,
        "strokeStyle": "solid",
        "roughness": 0,  # crisp lines
        "opacity": 100,
        "groupIds": [],
        "frameId": None,
        "roundness": None,
        "seed": seed,
        "version": 1,
        "versionNonce": seed,
        "isDeleted": False,
        "boundElements": [],
        "updated": 1,
        "link": None,
        "locked": False,
    }


def schema_to_excalidraw(schema, dark: bool = False) -> str:
    """Return an Excalidraw scene (.excalidraw JSON) of the schema.

    ``dark`` switches to a dark canvas with light strokes/text so the export
    matches the app when the diagram theme is dark.
    """
    tables = list(getattr(schema, "tables", []) or [])
    rels = list(getattr(schema, "relationships", []) or [])

    # Excalidraw's dark theme inverts the whole canvas at render time, so a dark
    # scene keeps the same (dark-on-light) element colours and just flips the
    # theme flag — Excalidraw then shows it light-on-dark.
    stroke = "#1e1e1e"
    view_bg = "#ffffff"
    theme = "dark" if dark else "light"

    def entity_size(table) -> tuple[float, float]:
        lines = [table.name, ""] + [_excali_line(c) for c in table.columns]
        longest = max((len(t) for t in lines), default=8)
        w = min(max(180.0, longest * _EXCALI_CHAR_W + 2 * _EXCALI_PAD), 560.0)
        h = len(lines) * _EXCALI_FONT_SIZE * _EXCALI_LINE_H + 2 * _EXCALI_PAD
        return w, round(h, 2)

    # Spread boxes so the widest relationship label sits in the gap between
    # boxes instead of overlapping one; the vertical gap only needs to clear a
    # single line of label text.
    max_label = max((len(getattr(r, "label", "") or "") for r in rels), default=0)
    label_w = max_label * _EXCALI_CHAR_W + 2 * _EXCALI_PAD
    x_gap = max(80.0, label_w + 24.0)
    y_gap = max(60.0, _EXCALI_FONT_SIZE * _EXCALI_LINE_H + 24.0)
    cols = max(1, int(math.ceil(math.sqrt(len(tables))))) if tables else 1
    grid = [tables[i:i + cols] for i in range(0, len(tables), cols)]

    elements: list[dict] = []
    rect_by_idx: dict[int, dict] = {}
    centers: dict[int, tuple[float, float]] = {}
    seed = 1000
    idx = 0
    y = 40.0
    for row in grid:
        x = 40.0
        row_h = 0.0
        for table in row:
            w, h = entity_size(table)
            rect = _excali_common(f"rect{idx}", "rectangle", x, y, w, h, seed, stroke)
            seed += 1
            text_lines = [table.name, ""] + [_excali_line(c) for c in table.columns]
            text = _excali_common(
                f"txt{idx}", "text",
                x + _EXCALI_PAD, y + _EXCALI_PAD, w - 2 * _EXCALI_PAD,
                h - 2 * _EXCALI_PAD, seed, stroke,
            )
            seed += 1
            text.update({
                "text": "\n".join(text_lines),
                "originalText": "\n".join(text_lines),
                "fontSize": _EXCALI_FONT_SIZE,
                "fontFamily": _EXCALI_FONT,
                "textAlign": "left",
                "verticalAlign": "top",
                "containerId": f"rect{idx}",
                "lineHeight": _EXCALI_LINE_H,
            })
            rect["boundElements"] = [{"id": f"txt{idx}", "type": "text"}]
            elements.append(rect)
            elements.append(text)
            rect_by_idx[idx] = rect
            centers[idx] = (x + w / 2, y + h / 2, w / 2, h / 2)
            x += w + x_gap
            row_h = max(row_h, h)
            idx += 1
        y += row_h + y_gap

    name_to_idx: dict[str, int] = {}
    for i, t in enumerate(tables):
        name_to_idx.setdefault(t.name.lower(), i)

    def _edge_point(cx, cy, hw, hh, tx, ty):
        """Where the centre->target line crosses this box's boundary."""
        dx, dy = tx - cx, ty - cy
        if dx == 0 and dy == 0:
            return cx, cy
        sx = hw / abs(dx) if dx else float("inf")
        sy = hh / abs(dy) if dy else float("inf")
        t = min(sx, sy)
        return cx + dx * t, cy + dy * t

    edge_no = 0
    for rel in rels:
        p = name_to_idx.get(str(getattr(rel, "parent_table", "")).lower())
        c = name_to_idx.get(str(getattr(rel, "child_table", "")).lower())
        if p is None or c is None:
            continue
        pcx, pcy, phw, phh = centers[p]
        ccx, ccy, chw, chh = centers[c]
        # Route edge-to-edge so the connector doesn't cut through the box text;
        # the bindings still let Excalidraw reroute when boxes are moved.
        sx, sy = _edge_point(pcx, pcy, phw, phh, ccx, ccy)
        ex, ey = _edge_point(ccx, ccy, chw, chh, pcx, pcy)
        arrow_id = f"arrow{edge_no}"
        arrow = _excali_common(
            arrow_id, "arrow", sx, sy, abs(ex - sx), abs(ey - sy), seed, stroke
        )
        seed += 1
        arrow.update({
            "points": [[0, 0], [round(ex - sx, 2), round(ey - sy, 2)]],
            "lastCommittedPoint": None,
            "startBinding": {"elementId": f"rect{p}", "focus": 0, "gap": 4},
            "endBinding": {"elementId": f"rect{c}", "focus": 0, "gap": 4},
            "startArrowhead": None,
            "endArrowhead": "arrow",
        })
        label = getattr(rel, "label", "") or ""
        if label:
            lbl = _excali_common(
                f"lbl{edge_no}", "text",
                (sx + ex) / 2, (sy + ey) / 2, len(label) * _EXCALI_CHAR_W,
                _EXCALI_FONT_SIZE * _EXCALI_LINE_H, seed, stroke,
            )
            seed += 1
            lbl.update({
                "text": label, "originalText": label,
                "fontSize": _EXCALI_FONT_SIZE, "fontFamily": _EXCALI_FONT,
                "textAlign": "center", "verticalAlign": "middle",
                "containerId": arrow_id, "lineHeight": _EXCALI_LINE_H,
            })
            arrow["boundElements"] = [{"id": f"lbl{edge_no}", "type": "text"}]
        elements.append(arrow)
        if label:
            elements.append(lbl)
        rect_by_idx[p]["boundElements"].append({"id": arrow_id, "type": "arrow"})
        rect_by_idx[c]["boundElements"].append({"id": arrow_id, "type": "arrow"})
        edge_no += 1

    scene = {
        "type": "excalidraw",
        "version": 2,
        "source": "https://github.com/damien-bafile/xsltomermaid",
        "elements": elements,
        "appState": {
            "gridSize": None,
            "viewBackgroundColor": view_bg,
            "theme": theme,
        },
        "files": {},
    }
    return json.dumps(scene, indent=2)


class DiagramView(QWidget):
    """A tab that renders a Mermaid ER diagram and can export it as SVG/PNG/PDF."""

    # Emitted when a diagram starts loading, and when Mermaid finishes (or
    # fails) rendering it — so the UI can show a spinner then a tick.
    render_started = Signal()
    render_finished = Signal(bool)  # True on success, False on error/timeout
    render_error = Signal(str)  # why a render failed (before render_finished)
    # A table in the diagram was clicked (entity id, or "" for empty canvas);
    # the bool is True for a double-click.
    entity_clicked = Signal(str, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.available = WEBENGINE_AVAILABLE
        self._workdir: str | None = None
        self._view = None
        self._render_gen = 0  # bumped per render so stale polls are ignored
        self._fit_scale = 1.0  # the page's own fit scale, for the zoom readout
        self._font_px = RenderStyle().font_size  # the drawn text size at 100%
        # Keep-alive rendering: the shell page (mermaid.js) is loaded once, then
        # each diagram is drawn by a JS call rather than a full page reload.
        self._shell_url: QUrl | None = None
        self._shell_loaded = False
        self._loading_shell = False
        self._pending_render: tuple[str, RenderStyle, int] | None = None
        self._focus_entity = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        if WEBENGINE_AVAILABLE:
            self._workdir = tempfile.mkdtemp(prefix="xsltomermaid_")
            shutil.copy(VENDOR_MERMAID, Path(self._workdir) / "mermaid.min.js")
            shell_path = Path(self._workdir) / "shell.html"
            shell_path.write_text(_shell_html(), encoding="utf-8")
            self._shell_url = QUrl.fromLocalFile(str(shell_path))
            self._view = QWebEngineView(self)
            self._page = _BridgePage(self._view)
            self._page.bridged.connect(self._on_bridge)
            self._view.setPage(self._page)
            self._view.settings().setAttribute(
                QWebEngineSettings.LocalContentCanAccessFileUrls, True
            )
            self._view.loadFinished.connect(self._on_load_finished)
            layout.addWidget(self._view, 1)
            # One strip under the diagram: render status on the left (added by
            # the window through add_status_widget), zoom controls on the right.
            self._strip = QHBoxLayout()
            self._strip.setContentsMargins(6, 2, 4, 2)
            self._strip.setSpacing(4)
            self._strip.addStretch(1)
            self._zoom_label = QLabel("100%")
            self._zoom_label.setAlignment(Qt.AlignCenter)
            self._zoom_label.setMinimumWidth(44)
            self._zoom_label.setToolTip(
                "Ctrl+scroll, or Ctrl++ / Ctrl+- / Ctrl+0, to zoom the diagram."
            )
            self._zoom_label.setAccessibleName("Zoom level")
            # Said when the text is drawn too small to read, with the way out.
            self._small_hint = QLabel("Text too small to read · zoom in or double-click a table")
            self._small_hint.setVisible(False)
            self._small_hint.setAccessibleName("Diagram text is too small to read")
            self._strip.addWidget(self._small_hint)
            for text, tip, slot in (
                ("−", "Zoom out (Ctrl+-)", lambda: self.zoom_by(0.8)),
                (None, None, None),
                ("+", "Zoom in (Ctrl++)", lambda: self.zoom_by(1.25)),
                ("Fit", "Fit the diagram to the view", self.fit_to_view),
            ):
                if text is None:
                    self._strip.addWidget(self._zoom_label)
                    continue
                btn = QToolButton()
                btn.setText(text)
                btn.setToolTip(tip)
                btn.setAccessibleName(tip.split(" (")[0])
                btn.setAutoRaise(True)
                btn.clicked.connect(slot)
                self._strip.addWidget(btn)
            self._view.setAccessibleName("Rendered diagram")
            self._view.setAccessibleDescription(
                "A picture of the diagram. Its text form is in the Mermaid tab."
            )
            layout.addLayout(self._strip)
            self._zoom_timer = QTimer(self)
            self._zoom_timer.setInterval(200)
            self._zoom_timer.timeout.connect(self._update_zoom_label)
            self._zoom_timer.start()
        else:
            label = QLabel(
                "The rendered-diagram view needs PySide6's WebEngine module.\n"
                "Install it with:  uv add PySide6-Addons\n\n"
                "The Mermaid tab and text export still work."
            )
            label.setAlignment(Qt.AlignCenter)
            label.setWordWrap(True)
            # Use the palette text colour so it stays legible in dark mode.
            layout.addWidget(label)

    def _update_zoom_label(self):
        """Show the real on-screen scale: browser zoom × the page's fit scale.

        The fit is done in the page (CSS), so zoomFactor alone said "100%"
        while the diagram was drawn at, say, 55%.
        """
        self._zoom_label.setText(f"{self._view.zoomFactor() * self._fit_scale:.0%}")
        self._sync_small_hint()
        if self._shell_loaded:
            self._view.page().runJavaScript(
                "window._lastScale || 1", self._on_fit_scale
            )

    def _on_fit_scale(self, value):
        try:
            scale = float(value) if value else 1.0
        except (TypeError, ValueError):
            scale = 1.0
        if abs(scale - self._fit_scale) > 1e-6:
            self._fit_scale = scale
            self._zoom_label.setText(f"{self._view.zoomFactor() * scale:.0%}")
            self._sync_small_hint()

    # Drawn text smaller than this (in px) is unreadable; say so.
    SMALL_TEXT_PX = 9.0

    def effective_text_px(self) -> float:
        """How tall the diagram's text is on screen right now."""
        if self._view is None:
            return float(self._font_px)
        return self._font_px * self._view.zoomFactor() * self._fit_scale

    def _sync_small_hint(self):
        small = self._shell_loaded and self.effective_text_px() < self.SMALL_TEXT_PX
        if small == self._small_hint.isHidden():
            self._small_hint.setVisible(small)

    def focus_entity(self, entity_id: str):
        """Highlight a table and, if the text is too small to read, return to
        the readable automatic fit (75% or more) centred on it."""
        if self._view is None or not self._shell_loaded or not entity_id:
            self.highlight_entity(entity_id)
            return
        if self.effective_text_px() < self.SMALL_TEXT_PX:
            self._view.setZoomFactor(1.0)
            self._view.page().runJavaScript(
                "window._actual = false; window.fitDiagram(false)"
            )
            self._update_zoom_label()
            # Scroll once the new size has laid out.
            QTimer.singleShot(60, lambda: self.highlight_entity(entity_id))
            return
        self.highlight_entity(entity_id)

    def add_status_widget(self, widget):
        """Put a status widget (render status, Stop) at the strip's left end."""
        if self._view is not None:
            self._strip.insertWidget(self._strip.count() - 5, widget)

    def fit_to_view(self):
        """Reset zoom and size the diagram to the view, whatever Fit says."""
        if self._view is None:
            return
        self._view.setZoomFactor(1.0)
        self._update_zoom_label()
        self._view.page().runJavaScript("window.fitDiagram && window.fitDiagram(true)")

    def _on_bridge(self, kind: str, group_id: str):
        if kind not in ("click", "dblclick"):
            return
        self.entity_clicked.emit(entity_id_from_group(group_id) or "", kind == "dblclick")

    def highlight_entity(self, entity_id: str):
        """Highlight one table in the drawn diagram ("" clears)."""
        if self._view is None or not self._shell_loaded:
            return
        self._view.page().runJavaScript(
            "window.selectEntityByName && window.selectEntityByName({})".format(
                json.dumps(entity_id or "")
            )
        )

    def zoom_by(self, factor: float):
        """Zoom the diagram in (>1) or out (<1); the keyboard twin of Ctrl+scroll."""
        if self._view is None:
            return
        # QWebEngineView accepts 25%–500%.
        self._view.setZoomFactor(min(5.0, max(0.25, self._view.zoomFactor() * factor)))
        self._update_zoom_label()

    def reset_zoom(self):
        """Ctrl+0: a true 100%, with no fit, until Fit is pressed."""
        if self._view is None:
            return
        self._view.setZoomFactor(1.0)
        self._view.page().runJavaScript("window.actualSize && window.actualSize()")
        self._update_zoom_label()

    # -- rendering ---------------------------------------------------------
    def set_diagram(
        self, mermaid_text: str, style: RenderStyle | None = None, focus: str = ""
    ):
        """Render a diagram into the view (async).

        Draws into the already-loaded shell via a JS call; only the first render
        (or the first after a message page) loads the shell + mermaid.js.
        ``focus`` is an entity id to centre on when the diagram overflows the
        view at the smallest automatic fit.
        """
        if not self.available or self._view is None or self._workdir is None:
            return
        self._focus_entity = focus
        style = style or RenderStyle()
        self._font_px = style.font_size
        self._render_gen += 1
        gen = self._render_gen
        self.render_started.emit()
        if self._shell_loaded:
            self._invoke_render(mermaid_text, style, gen)
        else:
            # Draw as soon as the shell finishes loading; coalesce rapid calls
            # so only the latest diagram is rendered.
            self._pending_render = (mermaid_text, style, gen)
            if not self._loading_shell:
                self._loading_shell = True
                self._view.load(self._shell_url)

    def _invoke_render(self, mermaid_text: str, style: RenderStyle, gen: int):
        script = "renderDiagram({}, {}, {}, {}, {}, {})".format(
            json.dumps(mermaid_text),
            json.dumps(_mermaid_config(style)),
            json.dumps(style.background),
            json.dumps(self.palette().color(QPalette.Base).name()),
            json.dumps(bool(style.use_max_width)),
            json.dumps(self._focus_entity),
        )
        self._view.page().runJavaScript(script)
        self._poll_mermaid(gen, 0)

    def cancel_render(self):
        """Abandon an in-flight render.

        Showing a message page bumps the render generation (so the in-flight
        poll is superseded and emits nothing) and navigates away from the shell,
        which tears down the running Mermaid layout — the effective "stop".
        """
        if not self.available or self._view is None or self._workdir is None:
            return
        self.show_message(
            "Render cancelled.\n\n"
            "Adjust the tables, columns, or options, then click “Draw ticked”."
        )

    def _on_load_finished(self, ok: bool):
        """When the shell page finishes loading, kick off the pending render."""
        if not self._loading_shell:
            return  # a message page (or unrelated load), not the render shell
        self._loading_shell = False
        pending = self._pending_render
        self._pending_render = None
        if not ok:
            self._shell_loaded = False
            if pending is not None:
                self._fail("The diagram page (mermaid.js) failed to load.")
            return
        self._shell_loaded = True
        if pending is not None:
            text, style, gen = pending
            if gen == self._render_gen:  # not already superseded
                self._invoke_render(text, style, gen)

    def _fail(self, reason: str):
        """End a render as failed, keeping the reason visible and copyable."""
        self.render_error.emit(reason)
        self.render_finished.emit(False)
        self.show_message(
            "Mermaid couldn't draw this diagram.\n\n"
            "Try fewer tables or columns, or open the Mermaid tab to "
            "find the line the error points to.",
            detail=reason,
        )

    def _poll_mermaid(self, gen: int, elapsed: int):
        if gen != self._render_gen or self._view is None:
            return  # superseded by a newer render
        if elapsed >= 60000:  # give up after 60s
            self._fail("The layout didn't finish within 60 seconds.")
            return

        def on_error(err):
            if gen != self._render_gen:
                return
            if err:
                self._fail(str(err))
            else:
                self.render_finished.emit(True)

        def on_done(done):
            if gen != self._render_gen:
                return
            if done:
                self._view.page().runJavaScript(
                    "window._mermaidError || ''", on_error
                )
            else:
                # Poll fast at first so quick diagrams return promptly, then back
                # off so a slow layout doesn't spin the CPU.
                delay = 16 if elapsed < 200 else 50 if elapsed < 1000 else 150
                QTimer.singleShot(
                    delay, lambda: self._poll_mermaid(gen, elapsed + delay)
                )

        self._view.page().runJavaScript("window._mermaidDone === true", on_done)

    def show_message(self, message: str, detail: str = ""):
        """Show a plain text message in place of a diagram (e.g. a hint).

        Blank lines in ``message`` separate paragraphs. ``detail`` (an error
        message, say) is shown below in a selectable monospace block.
        """
        if not self.available or self._view is None or self._workdir is None:
            return
        # Invalidate any in-flight render poll; this isn't a diagram. Navigating
        # to the message page drops the shell, so the next diagram reloads it.
        self._render_gen += 1
        self._shell_loaded = False
        self._loading_shell = False
        self._pending_render = None
        # Theme the message page from the palette so it matches the app in dark
        # mode instead of flashing a white panel.
        pal = self.palette()
        base = pal.color(QPalette.Base)
        text = pal.color(QPalette.WindowText)
        window = pal.color(QPalette.Window)
        muted = QColor(
            round(text.red() * 0.6 + window.red() * 0.4),
            round(text.green() * 0.6 + window.green() * 0.4),
            round(text.blue() * 0.6 + window.blue() * 0.4),
        )
        # The app's own UI font, so the tab doesn't switch type voice.
        family = self.font().family().replace("'", "")
        paragraphs = "".join(
            f"<p>{html.escape(p.strip())}</p>"
            for p in message.split("\n\n")
            if p.strip()
        )
        detail_html = (
            f"<pre>{html.escape(detail.strip())}</pre>" if detail.strip() else ""
        )
        line = f"rgba({muted.red()},{muted.green()},{muted.blue()},.35)"
        page = (
            "<!DOCTYPE html><html><head><meta charset='utf-8'><style>"
            f"html{{height:100%;background:{base.name()}}}"
            f"body{{margin:0;padding:32px;color:{muted.name()};"
            f"font-family:'{family}',system-ui,sans-serif;"
            "font-size:15px;line-height:1.5;max-width:72ch}"
            "p{margin:0 0 .9em}p:first-child{color:"
            f"{text.name()};font-weight:600}}"
            "pre{margin:1.2em 0 0;padding:12px 14px;white-space:pre-wrap;"
            f"border:1px solid {line};border-radius:6px;"
            "font:13px/1.45 Consolas,Menlo,monospace;user-select:text}"
            "</style></head><body>"
            f"{paragraphs}{detail_html}</body></html>"
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
        # Wall-clock deadline: each poll below can itself block up to ~2s, so
        # counting fixed 150ms steps badly under-counted real time (a "60s" cap
        # ran for minutes when a render stalled). Measure elapsed time instead.
        deadline = time.monotonic() + timeout_ms / 1000.0
        while time.monotonic() < deadline:
            done = self._run_js("window._mermaidDone === true", timeout_ms=2000)
            if done:
                break
            pause = QEventLoop()
            QTimer.singleShot(150, pause.quit)
            pause.exec()

        # Mermaid colours text/shapes via a <style> block, not inline attributes.
        # QtSvg (used to rasterise PNG/PDF) ignores CSS <style> rules, so that
        # colour is lost — most visibly, the dark theme's light text turns black
        # and vanishes. Before reading the markup back, copy each element's
        # *computed* fill/stroke (which Chromium has resolved from the CSS) onto
        # the element as an attribute, which QtSvg does honour. Theme-agnostic,
        # and it also makes the exported .svg render correctly in weak viewers.
        #
        # Two more QtSvg gaps are patched here:
        #  * Relationship-label boxes are filled by a `.relationshipLabelBox{
        #    fill:hsl(...)}` CSS rule. QtSvg applies the class but can't parse
        #    hsl(), so it falls back to a dark fill (a dark redaction bar in
        #    light exports). An inline `style` fill wins over the stylesheet, so
        #    pin each box to the page background (masking the line, correct in
        #    both themes).
        #  * QtSvg ignores `dominant-baseline: middle`, so every centred text
        #    (table cells, entity titles, edge labels) drops to its baseline and
        #    rides above where Mermaid placed it. Switch those to an alphabetic
        #    baseline and shift `y` by the measured centre offset — cached per
        #    font-size, so it's exact for any font/size with no constant, and
        #    keeps the .svg centred in browsers too.
        svg = self._run_js(
            "(function(){"
            "var s=document.querySelector('.mermaid svg');"
            "if(!s)return '';"
            # The view's fit sets inline width/height; the file keeps its own.
            "s.style.removeProperty('width');s.style.removeProperty('height');"
            "function skip(v){return !v||v==='none'||v==='transparent'||v==='rgba(0, 0, 0, 0)';}"
            "var els=s.querySelectorAll('text,tspan,path,rect,circle,ellipse,line,polygon,polyline');"
            "for(var i=0;i<els.length;i++){var el=els[i],cs=getComputedStyle(el);"
            "['fill','stroke'].forEach(function(p){var v=cs.getPropertyValue(p);"
            "if(!skip(v)&&!el.getAttribute(p))el.setAttribute(p,v);});}"
            "var bg=getComputedStyle(document.body).backgroundColor;if(skip(bg))bg='';"
            "Array.prototype.forEach.call(s.querySelectorAll('.relationshipLabelBox'),"
            "function(b){b.style.setProperty('opacity','1');"
            "b.style.setProperty('fill',bg||'none');});"
            "var offs={};"
            "Array.prototype.forEach.call(s.querySelectorAll('text'),function(t){"
            "var cs=getComputedStyle(t);var db=cs.dominantBaseline;"
            "if(db!=='middle'&&db!=='central')return;"
            "var fs=cs.fontSize;"
            "if(offs[fs]===undefined){var m=t.getBBox();var cm=m.y+m.height/2;"
            "t.style.setProperty('dominant-baseline','alphabetic');"
            "var b2=t.getBBox();offs[fs]=cm-(b2.y+b2.height/2);}"
            "else{t.style.setProperty('dominant-baseline','alphabetic');}"
            "var oy=parseFloat(t.getAttribute('y'))||0;"
            "t.setAttribute('y',(oy+offs[fs]).toFixed(2));});"
            "var out=s.outerHTML;window.fitDiagram(false);return out;})()",
            timeout_ms=5000,
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

    def save_pdf(self, path: str, background: str = "white") -> str:
        """Save the rendered diagram as PDF."""
        svg = self.current_svg()
        if not svg:
            raise RuntimeError("No rendered diagram available to save.")
        return svg_to_pdf(svg, path, background=background)

    def save_drawio(self, path: str) -> str:
        """Save the rendered diagram as a Draw.io (.drawio) file."""
        svg = self.current_svg()
        if not svg:
            raise RuntimeError("No rendered diagram available to save.")
        Path(path).write_text(svg_to_drawio(svg), encoding="utf-8")
        return path

    def cleanup(self):
        if self._view is not None:
            self._zoom_timer.stop()
        if self._workdir:
            shutil.rmtree(self._workdir, ignore_errors=True)
            self._workdir = None
