"""The schema map: a native Qt view of :func:`schema_map.build_map`.

Drawn with QGraphicsView rather than in the web view, so it needs no bundled
layout library and gets tooltips, zoom, rubber-band selection and keyboard
focus from Qt. Every action also has a list/keyboard route in the window.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QTransform,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .excel_to_mermaid import Schema
from .schema_map import HIDDEN_BY_DEFAULT, SchemaMap, build_map
from .theme import _ACCENT

# Categorical colours for the largest clusters (Tableau 10); smaller clusters
# share a neutral grey so colour stays meaningful. Points are non-text marks
# and need 3:1 against the map: Tableau's originals all pass on the dark map,
# but half fail on white, so light mode uses darkened shades of the same hues.
_CLUSTER_COLOURS = {
    "dark": [
        "#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f",
        "#edc948", "#b07aa1", "#ff9da7", "#9c755f", "#86bcb6",
    ],
    "light": [
        "#4e79a7", "#dc740e", "#e15759", "#539d97", "#59a14f",
        "#af8c11", "#b07aa1", "#c9497a", "#9c755f", "#579e96",
    ],
}
_OTHER = {"dark": "#8c95a3", "light": "#8992a1"}
_LABEL_MIN_MEMBERS = 8  # clusters this big get their hub's name drawn


def _node_radius(degree: int) -> float:
    return 3.0 + 1.6 * math.sqrt(degree)


class _MapGraphicsView(QGraphicsView):
    """Wheel zooms (no modifier needed on a map); drag draws a selection box."""

    double_clicked = Signal(str)
    zoomed = Signal()
    cluster_clicked = Signal(int)
    key_pressed = Signal(object)  # the QKeyEvent, handled by SchemaMapView

    def __init__(self, scene, parent=None):
        super().__init__(scene, parent)
        self.setRenderHint(QPainter.Antialiasing)
        self.setDragMode(QGraphicsView.RubberBandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setViewportUpdateMode(QGraphicsView.BoundingRectViewportUpdate)
        self.setFocusPolicy(Qt.StrongFocus)
        self.user_zoomed = False

    def mousePressEvent(self, event):  # noqa: N802 (Qt naming)
        item = self.itemAt(event.position().toPoint())
        if item is not None and item.data(1) is not None:  # a cluster label
            self.cluster_clicked.emit(int(item.data(1)))
            event.accept()
            return
        super().mousePressEvent(event)

    def keyPressEvent(self, event):  # noqa: N802 (Qt naming)
        self.key_pressed.emit(event)
        if not event.isAccepted():
            super().keyPressEvent(event)

    def wheelEvent(self, event):  # noqa: N802 (Qt naming)
        factor = 1.2 if event.angleDelta().y() > 0 else 1 / 1.2
        self.scale(factor, factor)
        self.user_zoomed = True
        self.zoomed.emit()

    def mouseDoubleClickEvent(self, event):  # noqa: N802 (Qt naming)
        item = self.itemAt(event.position().toPoint())
        name = item.data(0) if item is not None else None
        if name:
            self.double_clicked.emit(name)
        super().mouseDoubleClickEvent(event)

    def resizeEvent(self, event):  # noqa: N802 (Qt naming)
        super().resizeEvent(event)
        if not self.user_zoomed:
            self.fit()
        self.zoomed.emit()

    def fit(self):
        rect = self.scene().itemsBoundingRect()
        if not rect.isEmpty():
            self.fitInView(rect.adjusted(-30, -30, 30, 30), Qt.KeepAspectRatio)


class SchemaMapView(QWidget):
    """Summary bar, the map, and the list of unconnected tables."""

    table_activated = Signal(str, bool)  # name, True for a double-click
    selection_changed = Signal(list)  # names selected on the map
    draw_requested = Signal(list)  # draw these tables as the ER diagram
    show_diagram_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._schema: Schema | None = None
        self._map: SchemaMap | None = None
        self._items: dict[str, QGraphicsEllipseItem] = {}
        self._ticked: set[str] = set()
        self._filter = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        bar = QHBoxLayout()
        self._summary = QLabel()
        self._summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._hide = QCheckBox("Hide audit and system links")
        self._hide.setChecked(True)
        self._hide.setToolTip(
            "Leave out createdby, modifiedby, owning…, organizationid and "
            "transactioncurrencyid links. They touch almost every Dynamics "
            "table, so with them the map is one block."
        )
        self._hide.toggled.connect(lambda _on: self._rebuild())
        self._diagram_btn = QPushButton("Diagram")
        self._diagram_btn.setToolTip("Back to the ER diagram of the ticked tables")
        self._diagram_btn.clicked.connect(self.show_diagram_requested.emit)
        self._draw_btn = QPushButton("Draw selection")
        self._draw_btn.setToolTip(
            "Tick the tables selected on the map (replacing the current ticks) "
            "and draw them as an ER diagram. Drag on the map to select a region; "
            "Ctrl+click adds or removes one table."
        )
        self._draw_btn.clicked.connect(lambda: self.draw_requested.emit(self.selected()))
        self._draw_btn.setEnabled(False)
        bar.addWidget(self._summary, 1)
        bar.addWidget(self._hide)
        bar.addWidget(self._diagram_btn)
        bar.addWidget(self._draw_btn)
        layout.addLayout(bar)

        self._scene = QGraphicsScene(self)
        self._scene.selectionChanged.connect(self._on_selection_changed)
        self._view = _MapGraphicsView(self._scene, self)
        self._view.setAccessibleName("Schema map")
        self._view.setAccessibleDescription(
            "Tables as points grouped into clusters. Every table is also in the "
            "list on the left."
        )
        self._view.double_clicked.connect(lambda n: self.table_activated.emit(n, True))
        self._view.zoomed.connect(self._declutter_labels)
        self._view.cluster_clicked.connect(self.select_cluster)
        self._view.key_pressed.connect(self._on_key)
        self._current = ""  # the table keyboard navigation moves from
        # (label, cluster) pairs, biggest cluster first.
        self._labels: list[tuple[QGraphicsSimpleTextItem, object]] = []
        self._edges = None
        layout.addWidget(self._view, 1)

        self._unconnected = QLabel()
        self._unconnected.setWordWrap(True)
        self._unconnected.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self._unconnected)
        self.retheme()

    # -- data -----------------------------------------------------------------
    def set_schema(self, schema: Schema | None):
        """Point the map at a schema (computed lazily, when first shown)."""
        self._schema = schema
        self._map = None
        self._scene.clear()
        self._items = {}
        if self.isVisible():
            self._rebuild()

    def showEvent(self, event):  # noqa: N802 (Qt naming)
        super().showEvent(event)
        if self._map is None and self._schema is not None:
            self._rebuild()

    def schema_map(self) -> SchemaMap | None:
        return self._map

    def _rebuild(self):
        if self._schema is None:
            return
        hidden = HIDDEN_BY_DEFAULT if self._hide.isChecked() else frozenset()
        self._map = build_map(self._schema, hidden)
        self._draw()
        self._view.user_zoomed = False
        self._view.fit()
        self._declutter_labels()

    def _draw(self):
        m = self._map
        scene = self._scene
        scene.blockSignals(True)
        scene.clear()
        self._items = {}
        text = self.palette().color(QPalette.WindowText)
        surface = "dark" if self.palette().color(QPalette.Base).lightness() < 128 else "light"
        palette = _CLUSTER_COLOURS[surface]

        colours = {}
        for i, comm in enumerate(m.communities):
            colours[i] = QColor(palette[i] if i < len(palette) else _OTHER[surface])
            if len(comm.members) >= _LABEL_MIN_MEMBERS:
                disc = QColor(colours[i])
                disc.setAlphaF(0.07)
                d = scene.addEllipse(
                    QRectF(comm.x - comm.radius, comm.y - comm.radius, comm.radius * 2, comm.radius * 2),
                    QPen(Qt.NoPen), QBrush(disc),
                )
                d.setZValue(-2)

        path = QPainterPath()
        for a, b in m.edges:
            na, nb = m.nodes.get(a), m.nodes.get(b)
            if na and nb:
                path.moveTo(na.x, na.y)
                path.lineTo(nb.x, nb.y)
        edge_colour = QColor(text)
        edge_colour.setAlphaF(0.10)
        edges = scene.addPath(path, QPen(edge_colour, 0))  # cosmetic hairline
        edges.setZValue(-1)
        self._edges = edges

        for node in m.nodes.values():
            r = _node_radius(node.degree)
            item = QGraphicsEllipseItem(node.x - r, node.y - r, 2 * r, 2 * r)
            item.setBrush(QBrush(colours[node.community]))
            item.setPen(QPen(Qt.NoPen))
            item.setFlag(QGraphicsItem.ItemIsSelectable, True)
            item.setData(0, node.name)
            item.setToolTip(
                f"{node.name}\n{node.degree} link{'s' if node.degree != 1 else ''} shown"
                f" · cluster: {m.communities[node.community].hub}"
            )
            scene.addItem(item)
            self._items[node.name] = item

        self._labels = []
        font = QFont(self.font())
        font.setPointSizeF(font.pointSizeF() * 1.05)
        font.setWeight(QFont.DemiBold)
        for comm in m.communities:
            if len(comm.members) < _LABEL_MIN_MEMBERS:
                continue
            label = QGraphicsSimpleTextItem(f"{comm.hub} ({len(comm.members)})")
            label.setFont(font)
            label.setBrush(QBrush(text))
            # Constant on-screen size at any zoom; placed by _declutter_labels.
            label.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
            label.setZValue(2)
            label.setData(1, m.communities.index(comm))
            label.setCursor(Qt.PointingHandCursor)
            label.setToolTip(f"Click to select all {len(comm.members)} tables in this cluster")
            scene.addItem(label)
            self._labels.append((label, comm))  # communities are biggest first
        scene.blockSignals(False)

        self._summary.setText(
            f"{len(m.nodes) + len(m.unconnected):,} tables · {len(m.communities):,} clusters · "
            f"{m.shown_links:,} of {m.total_links:,} links shown"
        )
        shown = ", ".join(m.unconnected[:12])
        more = f", … (+{len(m.unconnected) - 12:,} more)" if len(m.unconnected) > 12 else ""
        self._unconnected.setText(
            f"Unconnected ({len(m.unconnected):,}), not drawn: {shown}{more}"
            if m.unconnected else ""
        )
        self._restyle()

    def _declutter_labels(self):
        """Show cluster names biggest first, hiding any that would overlap.

        Labels keep a constant on-screen size, so overlaps depend on zoom:
        zooming in reveals the smaller clusters' names.
        """
        taken = []
        transform = self._view.viewportTransform()
        for label, comm in self._labels:
            w, h = label.boundingRect().width(), label.boundingRect().height()
            placed = False
            # Centred above the cluster, else centred below it. The offset is
            # in screen pixels: the label ignores the view's zoom.
            for y, dy in ((comm.y - comm.radius, -h - 2), (comm.y + comm.radius, 2)):
                label.setPos(comm.x, y)
                label.setTransform(QTransform.fromTranslate(-w / 2, dy))
                rect = label.deviceTransform(transform).mapRect(label.boundingRect())
                rect = rect.adjusted(-4, -2, 4, 2)
                if not any(rect.intersects(other) for other in taken):
                    taken.append(rect)
                    placed = True
                    break
            label.setVisible(placed)

    # -- selection, ticks and filter highlights ---------------------------------
    def selected(self) -> list[str]:
        return sorted(item.data(0) for item in self._scene.selectedItems() if item.data(0))

    def select(self, names):
        """Select these tables on the map (and centre on a single one)."""
        wanted = set(names)
        self._scene.blockSignals(True)
        for name, item in self._items.items():
            item.setSelected(name in wanted)
        self._scene.blockSignals(False)
        self._on_selection_changed(emit=False)
        if len(wanted) == 1:
            item = self._items.get(next(iter(wanted)))
            if item is not None:
                self._view.centerOn(item)

    def select_cluster(self, index: int):
        """Select every table in one cluster (click its label, or Ctrl+A)."""
        if self._map is None or not 0 <= index < len(self._map.communities):
            return
        members = self._map.communities[index].members
        self.select(members)
        self.selection_changed.emit(self.selected())

    def _nearest(self, key) -> str:
        """The table nearest the current one in an arrow key's direction."""
        nodes = self._map.nodes
        if not self._current or self._current not in nodes:
            return max(nodes, key=lambda n: (nodes[n].degree, n), default="")
        here = nodes[self._current]
        best, best_score = "", float("inf")
        for name, node in nodes.items():
            dx, dy = node.x - here.x, node.y - here.y
            along = {Qt.Key_Right: dx, Qt.Key_Left: -dx, Qt.Key_Down: dy, Qt.Key_Up: -dy}[key]
            across = abs(dy) if key in (Qt.Key_Left, Qt.Key_Right) else abs(dx)
            if along <= 0.5:
                continue
            score = along + 2 * across
            if score < best_score:
                best, best_score = name, score
        return best

    def _on_key(self, event):
        """Arrows move between tables, Enter opens, Space selects,
        Ctrl+A selects the current table's cluster, Esc clears."""
        if self._map is None or not self._map.nodes:
            event.ignore()
            return
        key = event.key()
        if key in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down):
            nxt = self._nearest(key)
            if nxt:
                self._current = nxt
                self._view.centerOn(self._items[nxt])
                self._restyle()
                self.table_activated.emit(nxt, False)
        elif key in (Qt.Key_Return, Qt.Key_Enter) and self._current:
            self.table_activated.emit(self._current, True)
        elif key == Qt.Key_Space and self._current:
            item = self._items[self._current]
            item.setSelected(not item.isSelected())
        elif key == Qt.Key_A and event.modifiers() & Qt.ControlModifier and self._current:
            self.select_cluster(self._map.nodes[self._current].community)
        elif key == Qt.Key_Escape:
            self._scene.clearSelection()
        else:
            event.ignore()
            return
        event.accept()

    def set_ticked(self, names):
        self._ticked = set(names)
        self._update_draw_button(self.selected())
        self._restyle()

    def _update_draw_button(self, names):
        """Name what drawing does: it replaces the ticked tables."""
        if not names:
            self._draw_btn.setText("Draw selection")
            return
        text = f"Draw these {len(names):,}"
        if self._ticked and self._ticked != set(names):
            text += f" (replaces {len(self._ticked):,} ticked)"
        self._draw_btn.setText(text)

    def set_filter(self, text: str):
        self._filter = text.strip().lower()
        self._restyle()

    def _on_selection_changed(self, emit: bool = True):
        names = self.selected()
        self._draw_btn.setEnabled(bool(names))
        self._update_draw_button(names)
        self._restyle()
        if emit:
            self.selection_changed.emit(names)
            if len(names) == 1:
                self.table_activated.emit(names[0], False)

    def _restyle(self):
        text = self.palette().color(QPalette.WindowText)
        accent = QColor(_ACCENT)
        # Cosmetic pens keep a constant screen width at any zoom (a scene-unit
        # pen vanished at the default fit). Selection uses the text colour,
        # which contrasts with every cluster colour; ticks a dashed accent.
        selected_pen = QPen(text, 2.5)
        selected_pen.setCosmetic(True)
        ticked_pen = QPen(accent, 1.5, Qt.DashLine)
        ticked_pen.setCosmetic(True)
        current_pen = QPen(accent, 3.5)
        current_pen.setCosmetic(True)
        for name, item in self._items.items():
            if name == self._current and self._view.hasFocus():
                item.setPen(current_pen)
            elif item.isSelected():
                item.setPen(selected_pen)
            elif name in self._ticked:
                item.setPen(ticked_pen)
            else:
                item.setPen(QPen(Qt.NoPen))
            match = not self._filter or self._filter in name.lower()
            # A selected table is never dimmed, even when it isn't a match.
            item.setOpacity(1.0 if (match or item.isSelected()) else 0.15)
            item.setZValue(1 if (item.isSelected() or (self._filter and match)) else 0)
        if self._edges is not None:
            # Links would drown the matches while filtering.
            self._edges.setOpacity(0.35 if self._filter else 1.0)

    def retheme(self):
        base = self.palette().color(QPalette.Base)
        self._view.setBackgroundBrush(QBrush(base))
        pal = self.palette()
        text, window = pal.color(QPalette.WindowText), pal.color(QPalette.Window)
        muted = QColor(
            round(text.red() * 0.7 + window.red() * 0.3),
            round(text.green() * 0.7 + window.green() * 0.3),
            round(text.blue() * 0.7 + window.blue() * 0.3),
        )
        self._unconnected.setStyleSheet(f"color: {muted.name()};")
        if self._map is not None:
            self._draw()
