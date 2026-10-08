"""The left-hand table list (TableSelector) and its row painter."""

from __future__ import annotations

from PySide6.QtCore import (
    QPointF,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QKeySequence,
    QPalette,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .config import RENDER_WARN_LIMIT
from .schema_map import name_prefix
from .theme import (
    _ACCENT,
    _ACCENT_WASH,
    _link_hex,
    _muted_hex,
    _apply_primary_button_style,
    _plural,
)


# One shared vocabulary for foreign-key direction, used by BOTH the path tracer
# and "Add related tables" so the same idea is never worded two ways. Plain
# language leads; the DBA terms (child→parent) live in the tooltips. "forward"
# follows the FK reference (a child points to its parent); "reverse" the reverse.
_FK_DIRECTION_CHOICES = (
    ("Either direction", "either"),
    ("Tables it points to", "forward"),
    ("Tables that point to it", "reverse"),
)


_FK_DIRECTION_LABELS = {value: label for label, value in _FK_DIRECTION_CHOICES}


_FK_DIRECTION_TOOLTIP = (
    "Which way to follow foreign keys:\n"
    "Either direction — ignore direction, just connectivity.\n"
    "Tables it points to — follow each FK to its target (child → parent).\n"
    "Tables that point to it — follow FKs back to their source (parent → child)."
)


_ROLE_LINKS = Qt.UserRole + 1  # (out, in) link counts


_ROLE_DRAWN = Qt.UserRole + 2  # True when the table is in the current diagram


_ROLE_MAPSEL = Qt.UserRole + 3  # True when the table is selected on the map


_ROLE_GROUP = Qt.UserRole + 4  # header text above the first row of a cluster


class _TableRowDelegate(QStyledItemDelegate):
    """Paints a table row with its link counts right-aligned (↗ out ↙ in) and
    a dot when the table is in the diagram. The name is elided to fit."""

    def sizeHint(self, option, index):  # noqa: N802 (Qt naming)
        size = super().sizeHint(option, index)
        if index.data(_ROLE_GROUP):
            size.setHeight(size.height() * 2)
        return size

    def paint(self, painter, option, index):
        group = index.data(_ROLE_GROUP)
        if group:
            # The first row of a cluster is double height: a header line on
            # top, then the row itself in the bottom half.
            header = option.rect.adjusted(6, 2, -6, -option.rect.height() // 2)
            painter.save()
            font = QFont(option.font)
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(QColor(_muted_hex(option.widget)))
            painter.drawText(header, Qt.AlignLeft | Qt.AlignBottom, group)
            painter.restore()
            option = QStyleOptionViewItem(option)
            option.rect = option.rect.adjusted(0, option.rect.height() // 2, 0, 0)
        links = index.data(_ROLE_LINKS)
        drawn = bool(index.data(_ROLE_DRAWN))
        if index.data(_ROLE_MAPSEL):
            # Selected on the map: a tint and an accent bar, distinct from ticks.
            tint = QColor(_ACCENT)
            tint.setAlphaF(0.16)
            painter.fillRect(option.rect, tint)
            painter.fillRect(option.rect.adjusted(0, 0, -(option.rect.width() - 3), 0), QColor(_ACCENT))
        if not links and not drawn:
            super().paint(painter, option, index)
            return
        out, in_ = links or (0, 0)
        text = f"↗{out} ↙{in_}" if links else ""
        metrics = option.fontMetrics
        dot = 12 if drawn else 0
        width = metrics.horizontalAdvance(text) + dot + 10
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        style = opt.widget.style() if opt.widget else QApplication.style()
        # Full-width selection/hover background, then the item in a narrower
        # rect so its text elides before the counts.
        style.drawPrimitive(QStyle.PE_PanelItemViewItem, opt, painter, opt.widget)
        opt.rect = option.rect.adjusted(0, 0, -width, 0)
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, opt.widget)
        painter.save()
        right = option.rect.adjusted(0, 0, -6, 0)
        selected = bool(option.state & QStyle.State_Selected)
        # The shared muted text colour (≥ 6:1); the palette's placeholder
        # grey was under 4.5:1 for these counts.
        muted = (
            option.palette.color(QPalette.HighlightedText)
            if selected
            else QColor(_muted_hex(option.widget))
        )
        painter.setPen(muted)
        if text:
            painter.drawText(right.adjusted(0, 0, -dot, 0), Qt.AlignRight | Qt.AlignVCenter, text)
        if drawn:
            painter.setPen(Qt.NoPen)
            # Accent on the accent selection vanished; use the selected text colour.
            painter.setBrush(muted if selected else QColor(_ACCENT))
            r = 3.5
            cy = right.center().y()
            painter.drawEllipse(QPointF(right.right() - r, cy), r, r)
        painter.restore()


class TableSelector(QWidget):
    """A filterable, checkable list of tables to include in the diagram."""

    applied = Signal()  # user asked to (re)render the current selection
    selection_changed = Signal()  # the user ticked/unticked tables (auto-render)
    current_table_changed = Signal(str)  # the highlighted row ("" for none)
    related_requested = Signal()  # user asked to also tick the related tables
    path_requested = Signal()  # user asked for the shortest path between two tables
    undone = Signal(str)  # the undo button restored the selection before this action

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        title = QLabel("Tables")
        self._title = title
        title.setStyleSheet("font-weight: 600;")
        self._header_row = QHBoxLayout()
        self._header_row.addWidget(title)
        self._header_row.addStretch(1)
        layout.addLayout(self._header_row)

        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Filter tables…")
        self._filter.setAccessibleName("Filter tables")
        self._filter.setClearButtonEnabled(True)
        self._filter.textChanged.connect(self._apply_filter_text)
        layout.addWidget(self._filter)
        # The map selection tints rows; say so when the filters hide them.
        self._map_selection: set[str] = set()
        self._mapsel_note = QLabel()
        self._mapsel_note.setWordWrap(True)
        self._mapsel_note.setVisible(False)
        self._mapsel_note.linkActivated.connect(lambda _href: self.clear_filters())
        layout.addWidget(self._mapsel_note)

        # How to read a long list: order, and which publisher prefix.
        view_row = QHBoxLayout()
        self._sort = QComboBox()
        self._sort.addItem("Name A–Z", "name")
        self._sort.addItem("Most connected", "links")
        self._sort.addItem("By cluster", "cluster")
        # Set by the window: returns {table: (cluster, hub)}, computed once.
        self.cluster_provider = None
        self._clusters: dict[str, tuple[int, str]] | None = None
        self._sort.setAccessibleName("Sort tables")
        self._sort.setToolTip(
            "Order the list by name, by links (audit and system links not "
            "counted), or by the map's clusters"
        )
        self._sort.currentIndexChanged.connect(lambda _i: self._resort())
        self._prefix = QComboBox()
        self._prefix.setAccessibleName("Table name prefix")
        self._prefix.setToolTip("Show only tables with this publisher prefix (msdyn_, hsl_, …)")
        self._prefix.currentIndexChanged.connect(lambda _i: self._apply_filters())
        view_row.addWidget(self._sort, 1)
        view_row.addWidget(self._prefix, 1)
        layout.addLayout(view_row)
        # Find: these narrow the list, so they sit with the filter, above it.
        count_row = QHBoxLayout()
        # For big schemas: review what's ticked without scrolling 1,000+ rows.
        self._ticked_only = QCheckBox("Ticked onl&y")
        self._ticked_only.setToolTip("List only the tables that are ticked.")
        self._ticked_only.toggled.connect(lambda _on: self._apply_filters())
        count_row.addWidget(self._ticked_only)
        self._hide_unconnected = QCheckBox("Hide unconnected")
        self._hide_unconnected.setToolTip(
            "Hide tables with no links (audit and system links not counted)."
        )
        self._hide_unconnected.toggled.connect(lambda _on: self._apply_filters())
        count_row.addWidget(self._hide_unconnected)
        count_row.addStretch(1)
        layout.addLayout(count_row)

        self._list = QListWidget()
        # Not uniform: cluster header rows are double height. Long names
        # elide rather than scroll sideways.
        self._list.setUniformItemSizes(False)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._list.setTextElideMode(Qt.ElideRight)
        self._list.setItemDelegate(_TableRowDelegate(self._list))
        self._list.setAccessibleName("Tables")
        self._list.itemChanged.connect(self._on_item_changed)
        self._list.currentItemChanged.connect(
            lambda item, _prev: self.current_table_changed.emit(item.text() if item else "")
        )
        layout.addWidget(self._list, 1)

        # The count gets its own line so the two checkboxes below don't
        # force the rail wider than the table names need.
        self._count = QLabel("No tables loaded yet")
        self._count.setStyleSheet(f"color: {_muted_hex(self)};")
        layout.addWidget(self._count)
        # What the painted marks on each row mean (shown once counts exist).
        self._legend = QLabel("↗ references  ·  ↙ referenced by  ·  ● in the diagram")
        self._legend.setAccessibleName(
            "Legend: up-right arrow, tables referenced; down-left arrow, tables "
            "referencing this one; dot, in the diagram"
        )
        self._legend.setVisible(False)
        layout.addWidget(self._legend)

        # Select: build up the ticks.
        layout.addSpacing(4)
        button_row = QHBoxLayout()
        self._select_shown_btn = QPushButton("Tick &shown")
        self._clear_btn = QPushButton("&Clear")
        self._select_shown_btn.setToolTip("Tick every table currently visible in the list.")
        self._clear_btn.setToolTip("Untick every table. Can be undone.")
        self._select_shown_btn.clicked.connect(self._on_select_shown)
        self._clear_btn.clicked.connect(self._on_clear)
        button_row.addWidget(self._select_shown_btn)
        button_row.addWidget(self._clear_btn)
        layout.addLayout(button_row)

        related_row = QHBoxLayout()
        self._related_btn = QPushButton("&Add related tables")
        self._related_btn.setToolTip(
            "Tick the tables one foreign-key hop from the ones already ticked. "
            "Adding a lot at once asks first, and can be undone."
        )
        self._related_btn.clicked.connect(lambda: self.related_requested.emit())
        self._related_direction = QComboBox()
        self._related_direction.setAccessibleName("Related tables direction")
        for label, value in _FK_DIRECTION_CHOICES:
            self._related_direction.addItem(label, value)
        self._related_direction.setToolTip(
            "Which neighbours to add, by foreign-key direction.\n" + _FK_DIRECTION_TOOLTIP
        )
        related_row.addWidget(self._related_btn, 1)
        related_row.addWidget(self._related_direction)

        # Growing the selection (its neighbours, or a path between two tables)
        # is a power tool, not the main loop, so it lives behind one
        # disclosure: a quiet line until opened, keeping the rail focused on
        # "find tables → tick → draw".
        self._path_toggle = QToolButton()
        self._path_toggle.setText("Grow selection")
        self._path_toggle.setCheckable(True)
        self._path_toggle.setChecked(False)
        self._path_toggle.setArrowType(Qt.RightArrow)
        self._path_toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self._path_toggle.setToolTip(
            "Add the tables next to the ticked ones, or trace the shortest "
            "foreign-key path between two tables."
        )
        # A borderless header, but keyboard focus and hover must still show — a
        # background wash gives both without shifting the layout.
        self._path_toggle.setStyleSheet(
            "QToolButton { border: none; font-weight: 600; padding: 2px 4px;"
            "  border-radius: 4px; }"
            f"QToolButton:hover {{ background: {_ACCENT_WASH}; }}"
            f"QToolButton:focus {{ background: {_ACCENT_WASH}; border: 1px solid {_ACCENT}; }}"
        )
        self._path_toggle.toggled.connect(self._on_path_toggled)
        layout.addWidget(self._path_toggle)

        # Everything for growing the selection, hidden until the disclosure is
        # open: neighbours first, then the path tracer.
        self._path_box = QWidget()
        path_box = QVBoxLayout(self._path_box)
        path_box.setContentsMargins(0, 0, 0, 4)
        path_box.setSpacing(6)
        path_box.addLayout(related_row)
        self._path_heading = QLabel("Or trace the shortest path between two tables:")
        self._path_heading.setWordWrap(True)
        path_box.addWidget(self._path_heading)

        endpoints_row = QHBoxLayout()
        self._path_from = QComboBox()
        self._path_from.setToolTip("Starting table for the path.")
        self._path_from.setAccessibleName("Path from table")
        self._path_to = QComboBox()
        self._path_to.setToolTip("Destination table for the path.")
        self._path_to.setAccessibleName("Path to table")
        for combo in (self._path_from, self._path_to):
            combo.currentIndexChanged.connect(self._update_path_enabled)
        endpoints_row.addWidget(self._path_from, 1)
        arrow = QLabel("→")
        arrow.setAlignment(Qt.AlignCenter)
        endpoints_row.addWidget(arrow)
        endpoints_row.addWidget(self._path_to, 1)
        path_box.addLayout(endpoints_row)

        # Optional single intermediate stop the route must pass through.
        via_row = QHBoxLayout()
        via_label = QLabel("via")
        self._path_via = QComboBox()
        via_label.setBuddy(self._path_via)
        self._path_via.setAccessibleName("Path via table")
        self._path_via.setToolTip(
            "Optional: force the route through this table on the way."
        )
        via_row.addWidget(via_label)
        via_row.addWidget(self._path_via, 1)
        path_box.addLayout(via_row)

        # Foreign keys are directed, so let the user say which way to walk them.
        self._path_direction = QComboBox()
        self._path_direction.setAccessibleName("Path direction")
        for label, value in _FK_DIRECTION_CHOICES:
            self._path_direction.addItem(label, value)
        self._path_direction.setToolTip(_FK_DIRECTION_TOOLTIP)
        path_box.addWidget(self._path_direction)

        # Teach the one domain concept this panel assumes, at point of use, so a
        # non-DBA doesn't have to hover a tooltip to know what "direction" means.
        self._path_hint = QLabel(
            "A foreign key points from a child table to the parent it references."
        )
        self._path_hint.setWordWrap(True)
        self._path_hint.setStyleSheet(f"color: {_muted_hex(self)}; font-size: 11px;")
        path_box.addWidget(self._path_hint)

        path_row = QHBoxLayout()
        self._path_btn = QPushButton("Trace path")
        self._path_btn.setToolTip(
            "Trace the shortest foreign-key path between the two chosen tables "
            "(through the optional Via stop) and draw it."
        )
        self._path_btn.clicked.connect(lambda: self.path_requested.emit())
        self._path_replace = QCheckBox("Replace selection")
        self._path_replace.setToolTip(
            "On: the path replaces your current selection.\n"
            "Off: the path is added to what you've already checked."
        )
        path_row.addWidget(self._path_btn, 1)
        path_row.addWidget(self._path_replace)
        path_box.addLayout(path_row)

        self._path_box.setVisible(False)
        layout.addWidget(self._path_box)

        # The diagram follows the ticks on its own (see MainWindow's auto-render),
        # so Render is a quiet manual refresh, needed for big selections that
        # don't auto-render. Undo only appears when there's something to undo.
        layout.addSpacing(4)
        action_row = QHBoxLayout()
        self._render_btn = QPushButton("D&raw ticked")
        self._render_btn.setToolTip(
            "Draw the ticked tables now (F5). Small selections update automatically; "
            f"more than {RENDER_WARN_LIMIT} tables wait for this."
        )
        self._render_btn.clicked.connect(lambda: self.applied.emit())
        self._undo_btn = QPushButton("&Undo")
        self._undo_btn.setToolTip(
            "Restore the selection from before the last clear, add or traced path."
        )
        self._undo_btn.clicked.connect(self.undo_last_change)
        self._undo_btn.setVisible(False)
        action_row.addWidget(self._render_btn, 1)
        action_row.addWidget(self._undo_btn)
        layout.addLayout(action_row)
        self._undo_snapshot: list[str] | None = None
        self._draw_pending = False

        # Keyboard accelerators for the frequent loop: jump to the filter, and
        # render without reaching for the mouse (Ctrl+Enter alongside the menu's
        # F5). Button mnemonics (Alt+R/A/C/…) cover the rest.
        QShortcut(QKeySequence("Ctrl+F"), self, activated=self._focus_filter)
        for seq in ("Ctrl+Return", "Ctrl+Enter"):
            QShortcut(QKeySequence(seq), self, activated=self._emit_render_if_ready)

        # Nothing to act on until a schema is loaded.
        self.set_ready(False)

    def add_header_widget(self, widget):
        """Put a small control (the table-list menu) beside the panel title."""
        self._header_row.addWidget(widget)

    def _focus_filter(self):
        if self._filter.isEnabled():
            self._filter.setFocus(Qt.ShortcutFocusReason)
            self._filter.selectAll()

    def set_draw_pending(self, pending: bool):
        """Give Draw the filled, lead style while ticks wait to be drawn (big
        selections don't redraw on every tick); quiet when the diagram is
        up to date."""
        if pending == self._draw_pending:
            return
        self._draw_pending = pending
        if pending:
            _apply_primary_button_style(self._render_btn)
        else:
            self._render_btn.setStyleSheet("")

    def _emit_render_if_ready(self):
        if self._render_btn.isEnabled():
            self.applied.emit()

    def set_ready(self, ready: bool):
        """Enable the selection controls only once a schema is loaded.

        With no file open there are no tables to pick, so the filter, the list
        and every action button are disabled to match the (already disabled)
        export bar rather than inviting dead clicks at "0 of 0 selected".
        """
        for widget in (
            self._filter,
            self._list,
            self._ticked_only,
            self._hide_unconnected,
            self._sort,
            self._prefix,
            self._select_shown_btn,
            self._clear_btn,
            self._related_btn,
            self._related_direction,
            self._path_toggle,
            self._path_from,
            self._path_to,
            self._path_via,
            self._path_direction,
            self._path_replace,
            self._render_btn,
        ):
            widget.setEnabled(ready)
        # The trace/undo buttons have their own readiness (two valid endpoints;
        # an available snapshot) layered on top of the schema being loaded.
        self._update_path_enabled()
        if not ready:
            self._undo_snapshot = None
            self._undo_btn.setText("&Undo")
        self._undo_btn.setVisible(ready and self._undo_snapshot is not None)

    # -- population --------------------------------------------------------
    def set_tables(self, names: list[str]):
        self._list.blockSignals(True)
        self._list.clear()
        for name in names:
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            self._list.addItem(item)
        self._list.blockSignals(False)
        self._clusters = None  # a new file: recompute on demand
        self._ticked_only.setChecked(False)
        self._hide_unconnected.setChecked(False)
        self._sort.blockSignals(True)
        self._sort.setCurrentIndex(0)
        self._sort.blockSignals(False)
        self._fill_prefixes(names)
        self._filter.clear()
        # Refill the From/To pickers and default them to the first two tables so
        # a path can be traced without any prior clicking.
        for combo in (self._path_from, self._path_to):
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(names)
            combo.blockSignals(False)
        if self._path_to.count() > 1:
            self._path_to.setCurrentIndex(1)
        # The Via picker starts at an explicit "no stop" entry (data None).
        self._path_via.blockSignals(True)
        self._path_via.clear()
        self._path_via.addItem("(no via stop)", None)
        for name in names:
            self._path_via.addItem(name, name)
        self._path_via.blockSignals(False)
        self._undo_snapshot = None
        self._undo_btn.setText("&Undo")
        self._undo_btn.setVisible(False)
        self._update_path_enabled()
        self._update_count()

    # -- selection helpers -------------------------------------------------
    def _items(self):
        return (self._list.item(i) for i in range(self._list.count()))

    def _apply_filter_text(self, _text: str = ""):
        self._apply_filters()

    def _apply_filters(self):
        """Text, Ticked only, prefix and Hide unconnected, all together."""
        needle = self._filter.text().strip().lower()
        ticked_only = self._ticked_only.isChecked()
        prefix = self._prefix.currentData()
        hide_lonely = self._hide_unconnected.isChecked()
        for item in self._items():
            links = item.data(_ROLE_LINKS)
            item.setHidden(
                needle not in item.text().lower()
                or (ticked_only and item.checkState() != Qt.Checked)
                or (prefix is not None and name_prefix(item.text()) != prefix)
                or (hide_lonely and links is not None and sum(links) == 0)
            )
        self._update_mapsel_note()

    def clear_filters(self):
        """Show every table again: no text, prefix, Ticked only or Hide unconnected."""
        for widget in (self._filter, self._prefix, self._ticked_only, self._hide_unconnected):
            widget.blockSignals(True)
        self._filter.clear()
        self._prefix.setCurrentIndex(0)
        self._ticked_only.setChecked(False)
        self._hide_unconnected.setChecked(False)
        for widget in (self._filter, self._prefix, self._ticked_only, self._hide_unconnected):
            widget.blockSignals(False)
        self._apply_filters()

    def _update_mapsel_note(self):
        hidden = sum(
            1 for item in self._items() if item.isHidden() and item.text() in self._map_selection
        )
        if hidden:
            n = len(self._map_selection)
            self._mapsel_note.setText(
                f"{n:,} selected on the map"
                + (f", {hidden:,} hidden by the filters" if hidden < n else ", all hidden by the filters")
                + f' · <a href="show" style="color:{_link_hex(self)}">Show</a>'
            )
        self._mapsel_note.setVisible(bool(hidden))

    def _fill_prefixes(self, names):
        counts: dict[str, int] = {}
        for name in names:
            counts[name_prefix(name)] = counts.get(name_prefix(name), 0) + 1
        self._prefix.blockSignals(True)
        self._prefix.clear()
        self._prefix.addItem(f"All prefixes ({len(names):,})", None)
        for prefix, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
            label = f"{prefix}_ ({n:,})" if prefix else f"No prefix ({n:,})"
            self._prefix.addItem(label, prefix)
        self._prefix.blockSignals(False)
        # One prefix (or none at all) gives nothing to choose between.
        self._prefix.setVisible(self._prefix.count() > 2)

    def set_link_counts(self, counts: dict[str, tuple[int, int]]):
        """Show each table's (out, in) link counts and enable those views."""
        # Data changes fire itemChanged, which is for ticks; with 1,800 rows
        # that cascaded into thousands of re-counts and a hang.
        self._list.blockSignals(True)
        for item in self._items():
            out, in_ = counts.get(item.text(), (0, 0))
            item.setData(_ROLE_LINKS, (out, in_))
            item.setToolTip(
                f"{item.text()}: {out} reference{'s' if out != 1 else ''} out, {in_} in "
                "(audit and system links not counted)"
            )
            item.setData(Qt.AccessibleDescriptionRole, f"{out} out, {in_} in")
        self._list.blockSignals(False)
        self._legend.setVisible(bool(counts))
        self._resort()
        self._apply_filters()

    def mark_map_selection(self, names):
        """Tint the rows of the tables selected on the map."""
        wanted = set(names)
        self._map_selection = wanted
        self._list.blockSignals(True)
        for item in self._items():
            item.setData(_ROLE_MAPSEL, item.text() in wanted)
        self._list.blockSignals(False)
        self._list.viewport().update()
        self._update_mapsel_note()

    def set_drawn(self, names):
        """Mark the tables currently drawn in the diagram."""
        drawn = {n.lower() for n in names}
        self._list.blockSignals(True)
        for item in self._items():
            item.setData(_ROLE_DRAWN, item.text().lower() in drawn)
        self._list.blockSignals(False)
        self._list.viewport().update()

    def _resort(self):
        """Reorder rows in place (ticks, data and the current row survive)."""
        mode = self._sort.currentData()
        current = self._list.currentItem().text() if self._list.currentItem() else ""
        self._list.blockSignals(True)
        items = [self._list.takeItem(0) for _ in range(self._list.count())]
        self._list.blockSignals(False)
        for item in items:  # headers only in "By cluster" order
            item.setData(_ROLE_GROUP, None)
        if mode == "cluster" and self.cluster_provider is not None:
            if self._clusters is None:
                self._clusters = self.cluster_provider()
            clusters = self._clusters
            for item in items:
                hub = clusters.get(item.text(), (None, ""))[1]
                links = item.data(_ROLE_LINKS) or (0, 0)
                item.setToolTip(
                    f"{item.text()}: {links[0]} out, {links[1]} in · "
                    + (f"cluster: {hub}" if hub else "not connected")
                )
            # Cluster by cluster (biggest first), hub-like tables first within
            # each; unconnected tables last.
            items.sort(key=lambda it: (
                clusters.get(it.text(), (10**9, ""))[0],
                -sum(it.data(_ROLE_LINKS) or (0, 0)),
                it.text().lower(),
            ))
            sizes: dict[int, int] = {}
            for item in items:
                key = clusters.get(item.text(), (-1, ""))[0]
                sizes[key] = sizes.get(key, 0) + 1
            previous = None
            for item in items:
                key, hub = clusters.get(item.text(), (-1, ""))
                if key != previous:
                    label = f"{hub} · {sizes[key]:,} tables" if key >= 0 else (
                        f"Unconnected · {sizes[key]:,} tables")
                    item.setData(_ROLE_GROUP, label)
                    previous = key
        elif mode == "links":
            items.sort(key=lambda it: (-sum(it.data(_ROLE_LINKS) or (0, 0)), it.text().lower()))
        else:
            items.sort(key=lambda it: it.text().lower())
        self._list.blockSignals(True)
        for item in items:
            self._list.addItem(item)
        self._list.blockSignals(False)
        if current:
            self.focus_table(current)

    def focus_table(self, name: str):
        """Highlight and scroll to a table's row (no tick change); "" clears."""
        match = next((i for i in self._items() if i.text() == name), None) if name else None
        self._list.blockSignals(True)
        self._list.setCurrentItem(match)
        if match is None:
            self._list.clearSelection()
        self._list.blockSignals(False)
        if match is not None:
            self._list.scrollToItem(match)

    def _on_item_changed(self, _item):
        self._update_count()
        self.selection_changed.emit()

    def _on_select_shown(self):
        self.check_shown()
        self.selection_changed.emit()

    def _on_clear(self):
        if not self.selected_tables():
            return
        self.snapshot_for_undo("clear")
        self.clear_selection()
        self.selection_changed.emit()

    def check_shown(self):
        self._set_state((item for item in self._items() if not item.isHidden()), Qt.Checked)

    def check_all(self):
        self._set_state(self._items(), Qt.Checked)

    def clear_selection(self):
        self._set_state(self._items(), Qt.Unchecked)

    def _set_state(self, items, state):
        self._list.blockSignals(True)
        for item in items:
            item.setCheckState(state)
        self._list.blockSignals(False)
        self._update_count()
        if self._ticked_only.isChecked():
            self._apply_filter_text(self._filter.text())

    def selected_tables(self) -> list[str]:
        return [item.text() for item in self._items() if item.checkState() == Qt.Checked]

    def check_tables(self, names) -> int:
        """Tick the listed tables (by name, case-insensitive), additively.

        Returns how many were newly ticked.
        """
        wanted = {n.lower() for n in names}
        newly = 0
        self._list.blockSignals(True)
        for item in self._items():
            if item.text().lower() in wanted and item.checkState() != Qt.Checked:
                item.setCheckState(Qt.Checked)
                newly += 1
        self._list.blockSignals(False)
        self._update_count()
        if self._ticked_only.isChecked():
            self._apply_filter_text(self._filter.text())
        return newly

    def set_selected_tables(self, names) -> tuple[int, list[str]]:
        wanted = [str(name).strip() for name in names if str(name).strip()]
        present = {item.text().lower() for item in self._items()}
        missing = [name for name in wanted if name.lower() not in present]
        self.clear_selection()
        applied = self.check_tables(wanted)
        return applied, missing

    def _update_count(self):
        total = self._list.count()
        if total == 0:
            # Empty state: orient a newcomer instead of a bare "0 of 0 selected".
            self._count.setText("No tables loaded yet")
            return
        selected = sum(1 for item in self._items() if item.checkState() == Qt.Checked)
        self._count.setText(f"{selected:,} of {total:,} ticked for the diagram")
        # Draw: name what it commits.
        self._render_btn.setText(
            f"D&raw {_plural(selected, 'table')}" if selected else "D&raw ticked"
        )
        self._title.setText(f"Tables ({total:,})")

    # -- path tracing (endpoints + non-destructive apply) ------------------
    def path_endpoints(self) -> tuple[str, str]:
        """The two tables chosen in the From/To pickers."""
        return self._path_from.currentText(), self._path_to.currentText()

    def path_replaces_selection(self) -> bool:
        """Whether a traced path should replace (vs add to) the selection."""
        return self._path_replace.isChecked()

    def path_direction(self) -> str:
        """Which way to walk foreign keys: 'either', 'forward', or 'reverse'."""
        return self._path_direction.currentData()

    def path_via(self) -> str | None:
        """The optional intermediate stop, or None when '(no via stop)' is picked."""
        return self._path_via.currentData()

    def _on_path_toggled(self, open_: bool):
        """Expand or collapse the path-tracer disclosure."""
        self._path_toggle.setArrowType(Qt.DownArrow if open_ else Qt.RightArrow)
        self._path_box.setVisible(open_)

    def _update_path_enabled(self):
        """Enable Trace only when two distinct, real endpoints are chosen."""
        start, end = self.path_endpoints()
        # Both pickers share the schema-loaded enabled state, so testing one is enough.
        ready = self._path_from.isEnabled() and bool(start) and bool(end) and start != end
        self._path_btn.setEnabled(ready)

    def related_direction(self) -> str:
        """Which neighbours 'Add related' pulls in: 'either', 'forward', 'reverse'."""
        return self._related_direction.currentData()

    def snapshot_for_undo(self, label: str):
        """Remember the current selection so the next change can be undone.

        ``label`` names the action on the button (e.g. "Undo add", "Undo path").
        """
        self._undo_snapshot = self.selected_tables()
        self._undo_btn.setText(f"&Undo {label}")
        self._undo_btn.setVisible(True)

    def undo_last_change(self):
        """Restore the selection captured before the last add / traced path."""
        if self._undo_snapshot is None:
            return
        self.set_selected_tables(self._undo_snapshot)
        self._undo_snapshot = None
        label = self._undo_btn.text().replace("&Undo", "").strip()
        self._undo_btn.setText("&Undo")
        self._undo_btn.setVisible(False)
        self.undone.emit(label)
        self.applied.emit()

    def retheme(self):
        muted = _muted_hex(self)
        self._count.setStyleSheet(f"color: {muted};")
        self._legend.setStyleSheet(f"color: {muted};")
        self._path_heading.setStyleSheet(f"color: {muted};")
        self._path_hint.setStyleSheet(f"color: {muted}; font-size: 11px;")
