"""The schema map: a native Qt view of :func:`schema_map.build_map`.

Drawn with QGraphicsView rather than in the web view, so it needs no bundled
layout library and gets tooltips, zoom, rubber-band selection and keyboard
focus from Qt. Every action also has a list/keyboard route in the window.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QImage,
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


class _TableLabel(QGraphicsSimpleTextItem):
    """A table's name on the map, on a soft backing in the canvas colour so
    it stays readable over dots and links."""

    backing = QColor(0, 0, 0, 0)

    def paint(self, painter, option, widget=None):  # noqa: D401 (Qt naming)
        painter.setPen(Qt.NoPen)
        painter.setBrush(self.backing)
        painter.drawRoundedRect(self.boundingRect().adjusted(-3, -1, 3, 1), 3, 3)
        super().paint(painter, option, widget)


class _MapGraphicsView(QGraphicsView):
    """Wheel zooms (no modifier needed on a map) and sideways scrolling pans;
    drag draws a selection box."""

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
        """The wheel zooms; sideways scrolling (a trackpad, a tilt wheel, or
        Shift+wheel) pans left and right instead of being read as a zoom out."""
        angle, pixels = event.angleDelta(), event.pixelDelta()
        sideways = abs(angle.x()) > abs(angle.y())
        if sideways or event.modifiers() & Qt.ShiftModifier:
            # Some platforms report Shift+wheel on y, others already on x.
            if not pixels.isNull():
                delta = pixels.x() if sideways else pixels.y()
            else:
                steps = (angle.x() if sideways else angle.y()) / 120
                delta = round(steps * self.horizontalScrollBar().singleStep() * 3)
            bar = self.horizontalScrollBar()
            bar.setValue(bar.value() - delta)
            event.accept()
            return
        if angle.y() == 0:
            event.ignore()
            return
        factor = 1.2 if angle.y() > 0 else 1 / 1.2
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

    fit_scale = 1.0  # the scale fit() chose: zoom is measured from it

    def fit(self):
        rect = self.scene().itemsBoundingRect()
        if not rect.isEmpty():
            self.fitInView(rect.adjusted(-30, -30, 30, 30), Qt.KeepAspectRatio)
            self.fit_scale = self.transform().m11() or 1.0

    def zoom_level(self) -> float:
        """How far in from the whole-map fit (1.0 = fitted)."""
        return self.transform().m11() / (self.fit_scale or 1.0)


class SchemaMapView(QWidget):
    """Summary bar, the map, and the list of unconnected tables."""

    table_activated = Signal(str, bool)  # name, True for a double-click
    selection_changed = Signal(list)  # names selected on the map
    draw_requested = Signal(list)  # draw these tables as the ER diagram
    show_diagram_requested = Signal()
    export_requested = Signal()  # save the map as an image

    # Long side of an exported PNG, in pixels: sharp when zoomed in a viewer,
    # without the multi-hundred-megabyte images a 1:1 scene would give.
    EXPORT_PNG_SIDE = 4000

    def __init__(self, parent=None):
        super().__init__(parent)
        self._schema: Schema | None = None
        self._map: SchemaMap | None = None
        self._items: dict[str, QGraphicsEllipseItem] = {}
        self._ticked: set[str] = set()
        self._filter = ""
        self._matches = None  # the list's search, or None

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
        self._draw_btn = QPushButton("Draw tables")
        self._draw_btn.setToolTip(
            "Tick the tables selected on the map (replacing the current ticks) "
            "and draw them as an ER diagram. Drag on the map to select a region; "
            "Ctrl+click adds or removes one table."
        )
        self._draw_btn.clicked.connect(lambda: self.draw_requested.emit(self.selected()))
        self._draw_btn.setEnabled(False)
        self._export_btn = QPushButton("Save map image…")
        self._export_btn.setToolTip(
            "Save the whole map as a PNG or SVG image, with the cluster names "
            "showing at the current zoom."
        )
        self._export_btn.clicked.connect(self.export_requested.emit)
        bar.addWidget(self._summary, 1)
        bar.addWidget(self._hide)
        bar.addWidget(self._export_btn)
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
        self._node_labels: dict = {}  # name -> (label, node, radius)
        layout.addWidget(self._view, 1)

        self._unconnected = QLabel()
        self._unconnected.setWordWrap(True)
        self._unconnected.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self._unconnected)
        self.retheme()

    # -- export ---------------------------------------------------------------
    def export_image(self, path: str) -> None:
        """Write the whole map to ``path`` (.svg, otherwise PNG).

        The scene is drawn as it looks now (colours, ticks, selection and the
        labels the current zoom shows) on the view's background.
        """
        rect = self._scene.itemsBoundingRect().adjusted(-30, -30, 30, 30)
        if rect.isEmpty():
            raise ValueError("The map is empty; load a schema first.")
        background = self._view.backgroundBrush().color()
        self._labels_for_export(max(rect.width(), rect.height()))
        # The bigger labels can reach past the clusters at the edges.
        rect = self._scene.itemsBoundingRect().adjusted(-30, -30, 30, 30)
        try:
            self._write_image(path, rect, background)
        finally:
            self._declutter_labels()  # back to the on-screen labels

    def _labels_for_export(self, extent: float):
        """Size cluster names to the whole map rather than the screen (a
        constant screen size is unreadable in a 4,000 px image), then show
        as many as fit without overlapping, biggest cluster first."""
        for name_label, _node, _r in self._node_labels.values():
            name_label.setVisible(False)
        taken = []
        target = extent / 90  # text height, in scene units
        for label, comm in self._labels:
            label.setTransform(QTransform())
            h = label.boundingRect().height() or 1
            k = target / h
            w = label.boundingRect().width() * k
            placed = False
            for y in (comm.y - comm.radius - target - 4, comm.y + comm.radius + 4):
                box = QRectF(comm.x - w / 2, y, w, target).adjusted(-6, -3, 6, 3)
                if not any(box.intersects(other) for other in taken):
                    taken.append(box)
                    label.setFlag(QGraphicsItem.ItemIgnoresTransformations, False)
                    label.setScale(k)
                    label.setPos(comm.x - w / 2, y)
                    placed = True
                    break
            label.setVisible(placed)

    def _write_image(self, path: str, rect: QRectF, background) -> None:
        if path.lower().endswith(".svg"):
            from PySide6.QtSvg import QSvgGenerator

            generator = QSvgGenerator()
            generator.setFileName(path)
            generator.setSize(QSize(int(rect.width()), int(rect.height())))
            generator.setViewBox(QRectF(0, 0, rect.width(), rect.height()))
            generator.setTitle("Schema map")
            painter = QPainter(generator)
            painter.fillRect(QRectF(0, 0, rect.width(), rect.height()), background)
            self._scene.render(painter, QRectF(0, 0, rect.width(), rect.height()), rect)
            painter.end()
            return
        scale = self.EXPORT_PNG_SIDE / max(rect.width(), rect.height())
        image = QImage(
            max(1, round(rect.width() * scale)), max(1, round(rect.height() * scale)),
            QImage.Format_ARGB32,
        )
        image.fill(background)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.TextAntialiasing)
        self._scene.render(painter, QRectF(image.rect()), rect)
        painter.end()
        if not image.save(path):
            raise OSError(f"Couldn't write {path}.")

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

        # A name beside each table, shown once zoomed in far enough to read
        # them (placed by _declutter_labels; clicks pass through to the map).
        self._node_labels = {}
        name_font = QFont(self.font())
        name_font.setPointSizeF(name_font.pointSizeF() * 0.92)
        backing = QColor(self.palette().color(QPalette.Base))
        backing.setAlphaF(0.78)
        _TableLabel.backing = backing
        for node in m.nodes.values():
            name_label = _TableLabel(node.name)
            name_label.setFont(name_font)
            name_label.setBrush(QBrush(text))
            name_label.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
            name_label.setAcceptedMouseButtons(Qt.NoButton)
            name_label.setZValue(1.5)
            name_label.setVisible(False)
            scene.addItem(name_label)
            self._node_labels[node.name] = (name_label, node, _node_radius(node.degree))

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
            label.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
            label.setScale(1.0)
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
        self._place_table_labels(taken, transform)

    # Zoomed in this far from the whole-map fit, tables get their names.
    TABLE_LABEL_ZOOM = 2.5

    def _place_table_labels(self, taken, transform):
        """Name the tables on screen once zoomed in, without overlaps.

        Selected, current and ticked tables come first (and are named at any
        zoom), then filter matches, then the most connected. A label that
        would cover another (or a cluster name) is left out, so zooming in
        further names more of them.
        """
        labels = self._node_labels
        if not labels:
            return
        zoomed = self._view.zoom_level() >= self.TABLE_LABEL_ZOOM
        visible = self._view.mapToScene(self._view.viewport().rect()).boundingRect()
        scale = transform.m11()

        def rank(name):
            item = self._items[name]
            node = labels[name][1]
            return (
                not (item.isSelected() or name == self._current),
                name not in self._ticked,
                not (self._filter and self._is_match(name)),
                -node.degree,
                name,
            )

        for label, _node, _r in labels.values():
            label.setVisible(False)
        for name in sorted(labels, key=rank):
            label, node, r = labels[name]
            item = self._items[name]
            marked = item.isSelected() or name == self._current or name in self._ticked
            if not (zoomed or marked) or not visible.contains(node.x, node.y):
                continue
            if self._filter and not self._is_match(name) and not item.isSelected():
                continue  # dimmed tables stay unnamed while filtering
            w, h = label.boundingRect().width(), label.boundingRect().height()
            offset = r * scale + 4  # just right of the dot, in screen pixels
            centre = transform.map(QPointF(node.x, node.y))
            rect = QRectF(centre.x() + offset, centre.y() - h / 2, w, h).adjusted(-2, -1, 2, 1)
            if any(rect.intersects(other) for other in taken):
                continue
            taken.append(rect)
            label.setPos(node.x, node.y)
            label.setTransform(QTransform.fromTranslate(offset, -h / 2))
            label.setVisible(True)

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
            self._draw_btn.setText("Draw tables")
            return
        text = f"Draw {len(names):,} table{'' if len(names) == 1 else 's'}"
        if self._ticked and self._ticked != set(names):
            text += f" (replaces {len(self._ticked):,} ticked)"
        self._draw_btn.setText(text)

    def set_filter(self, text: str):
        """Highlight tables containing ``text`` (see :meth:`set_matches`)."""
        needle = text.strip().lower()
        self.set_matches(
            {n for n in self._items if needle in n.lower()} if needle else None
        )

    def set_matches(self, names):
        """Highlight these tables (the list's search) and dim the rest; None
        clears the search."""
        self._matches = set(names) if names is not None else None
        self._filter = "1" if names is not None else ""  # searching or not
        self._restyle()

    def _is_match(self, name: str) -> bool:
        return self._matches is None or name in self._matches

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
            match = self._is_match(name)
            # A selected table is never dimmed, even when it isn't a match.
            item.setOpacity(1.0 if (match or item.isSelected()) else 0.15)
            item.setZValue(1 if (item.isSelected() or (self._filter and match)) else 0)
        if self._edges is not None:
            # Links would drown the matches while filtering.
            self._edges.setOpacity(0.35 if self._filter else 1.0)
        if self._labels or self._node_labels:
            self._declutter_labels()

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
