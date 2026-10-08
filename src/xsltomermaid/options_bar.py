"""The diagram options bar above the canvas."""

from __future__ import annotations

from PySide6.QtCore import (
    QSettings,
    Qt,
    Signal,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .diagram_view import RenderStyle
from .excel_to_mermaid import DiagramOptions
from .schema_map import HIDDEN_BY_DEFAULT
from .theme import (
    _ACCENT_WASH,
    _line_hex,
    _muted_hex,
    _ACCENT,
    _control_border_hex,
)


class DiagramOptionsBar(QWidget):
    """A compact bar of controls for how the diagram looks and reads.

    Emits :attr:`changed` whenever any option is touched; the window reads
    :meth:`render_style` and :meth:`diagram_options` to re-render.
    """

    changed = Signal()

    # (label, value) pairs for the dropdowns.
    _ORIENTATIONS = [
        ("Left → Right", "LR"),
        ("Top → Bottom", "TB"),
        ("Bottom → Top", "BT"),
        ("Right → Left", "RL"),
    ]
    _SPACINGS = [
        ("Compact", (6, 60, 40)),  # entity_padding, min_width, min_height
        ("Normal", (15, 100, 75)),
        ("Spacious", (28, 140, 100)),
    ]
    _THEMES = [
        ("Default", "default"),
        ("Neutral", "neutral"),
        ("Dark", "dark"),
        ("Forest", "forest"),
        ("Base", "base"),
    ]
    _BACKGROUNDS = [
        ("White", "#ffffff"),
        ("Transparent", "transparent"),
        ("Dark", "#16181d"),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(4)

        self._orientation = self._combo(self._ORIENTATIONS)
        self._spacing = self._combo(self._SPACINGS)
        self._spacing.setToolTip("Spacing: padding around each table.")
        self._theme = self._combo(self._THEMES)
        # Theme and background follow the OS's light/dark mode until the user
        # picks one (signals are blocked when the app sets them).
        self._follow_system = True
        self._background = self._combo(self._BACKGROUNDS)
        for combo in (self._theme, self._background):
            combo.currentIndexChanged.connect(self._stop_following_system)
        self._orientation.setToolTip("Orientation: which way the diagram flows.")
        self._theme.setToolTip("Theme: the diagram's colours.")
        self._background.setToolTip(
            "Canvas background: the view only. Exports use their own background "
            "(shown beside the Export button), set from its arrow."
        )
        self._font = QSpinBox()
        self._font.setRange(8, 28)
        self._font.setValue(12)
        self._font.setToolTip("Font size: the diagram's text, before any zoom.")
        self._font.setSuffix(" px")
        self._font.valueChanged.connect(lambda _v: self.changed.emit())
        self._fit_width = QCheckBox("Fit to vie&w")
        self._fit_width.setChecked(True)
        self._fit_width.setToolTip(
            "Size the diagram to the view: enlarged up to 150% when small, "
            "shrunk when large. Exports keep the diagram's own size."
        )
        self._show_comments = QCheckBox("Descriptions/&notes")
        self._show_comments.setChecked(True)
        self._show_comments.setToolTip(
            "Show each column's description, identity, computed and not-null notes."
        )
        self._show_rel_labels = QCheckBox("Relationship la&bels")
        self._show_rel_labels.setChecked(True)
        self._show_rel_labels.setToolTip("Relationship labels: name the foreign-key column on each line.")
        self._show_rel_labels.setAccessibleName("Relationship labels")
        self._prefix_schema = QCheckBox("Prefi&x schema name")
        self._prefix_schema.setToolTip("Title tables as schema.Table instead of Table.")
        self._keys_only = QCheckBox("&Keys only")
        self._keys_only.setToolTip("Show only primary-key and foreign-key columns.")
        self._hide_audit = QCheckBox("Hide audit and system links")
        self._hide_audit.setToolTip(
            "Leave out createdby, modifiedby, owning…, organizationid and "
            "transactioncurrencyid links from the diagram, SQL and exports. They "
            "only appear when systemuser, team, businessunit and similar are ticked."
        )
        for chk in (
            self._fit_width,
            self._show_comments,
            self._show_rel_labels,
            self._prefix_schema,
            self._keys_only,
            self._hide_audit,
        ):
            chk.toggled.connect(lambda _v: self.changed.emit())
            # Never clip a checkbox label when the canvas narrows.
            chk.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)

        # Row 1: the everyday choices. Each label is the buddy of its control,
        # so screen readers name the dropdown and Alt+letter jumps to it.
        row1 = QHBoxLayout()
        row1.setSpacing(8)
        # Everything that only affects the ER diagram; hidden on the map.
        self._diagram_only: list[QWidget] = []
        self._row1_labels: list[QLabel] = []
        self._full_width = 0
        # Theme and background are set once and rarely touched, so they sit
        # behind More; the row keeps room for its labels at narrow widths.
        for label, widget in [
            ("&Orientation:", self._orientation),
        ]:
            buddy = self._buddy(label, widget)
            self._row1_labels.append(buddy)
            row1.addWidget(buddy)
            row1.addWidget(widget)
            self._diagram_only += [buddy, widget]
        row1.addSpacing(8)
        row1.addWidget(self._show_rel_labels)
        row1.addWidget(self._keys_only)
        self._diagram_only += [self._show_rel_labels, self._keys_only]

        # Chips for states the app chose and that last: one click undoes each.
        # (The status line is for one-off messages and gets overwritten.)
        self._audit_share = 0.0
        self._keys_auto = False
        self._diagram_visible = True
        self._audit_chip = self._chip(lambda: self._hide_audit.setChecked(False))
        self._keys_chip = self._chip(lambda: self._keys_only.setChecked(False))
        self._keys_chip.setText("Keys only: on for wide tables  ✕")
        self._keys_chip.setToolTip(
            "Keys only was switched on because these tables are wide. Click to "
            "show all columns."
        )
        # The name starts with the visible text (WCAG 2.5.3).
        self._keys_chip.setAccessibleName("Keys only: on for wide tables. Click to turn off")
        self._keys_only.toggled.connect(self._on_keys_toggled)
        self._hide_audit.toggled.connect(lambda _on: self._sync_chips())
        self._sync_chips()
        row1.addStretch(1)

        # The rest is fine-tuning, so it sits behind a disclosure, closed by
        # default, that keeps the bar to one line.
        self._more = QToolButton()
        self._more.setText("More")
        self._more.setCheckable(True)
        self._more.setArrowType(Qt.RightArrow)
        self._more.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self._more.setToolTip(
            "Theme, canvas background, spacing, font size, width, notes, schema "
            "prefix and audit links."
        )
        self._more.setStyleSheet(
            # A transparent border that turns accent on keyboard focus (3.4:1+);
            # the wash alone was barely visible.
            "QToolButton { border: 1px solid transparent; padding: 2px 6px; border-radius: 4px; }"
            f"QToolButton:hover {{ background: {_ACCENT_WASH}; }}"
            f"QToolButton:focus {{ background: {_ACCENT_WASH}; border-color: {_ACCENT}; }}"
            "QToolButton:checked { background: transparent; }"
            f"QToolButton:checked:hover {{ background: {_ACCENT_WASH}; }}"
        )
        self._more.toggled.connect(self._on_more_toggled)
        row1.addWidget(self._more)
        self._diagram_only.append(self._more)
        self._row1 = row1
        outer.addLayout(row1)

        # Two lines (looks, then content) so neither is squeezed in a narrow
        # canvas.
        self._more_box = QWidget()
        more = QVBoxLayout(self._more_box)
        more.setContentsMargins(0, 0, 0, 0)
        more.setSpacing(4)
        row2 = QHBoxLayout()
        row2.setSpacing(8)
        for label, widget in [
            ("&Theme:", self._theme),
            ("Canvas back&ground:", self._background),
            ("Spac&ing:", self._spacing),
            ("Font si&ze:", self._font),
        ]:
            row2.addWidget(self._buddy(label, widget))
            row2.addWidget(widget)
        row2.addStretch(1)
        row3 = QHBoxLayout()
        row3.setSpacing(12)
        for chk in (self._fit_width, self._show_comments, self._prefix_schema, self._hide_audit):
            row3.addWidget(chk)
        row3.addStretch(1)
        more.addLayout(row2)
        more.addLayout(row3)
        self._more_box.setVisible(False)
        outer.addWidget(self._more_box)

    def add_leading_widget(self, widget):
        """Add a control at the left end of the first row (the view switch)."""
        self._row1.insertWidget(0, widget)

    def set_diagram_controls_visible(self, visible: bool):
        """Show the diagram-only options (hidden while the map is showing)."""
        self._diagram_visible = visible
        for widget in self._diagram_only:
            widget.setVisible(visible)
        self._more_box.setVisible(visible and self._more.isChecked())
        self._sync_chips()
        self._fit_labels()

    def resizeEvent(self, event):  # noqa: N802 (Qt naming)
        super().resizeEvent(event)
        self._fit_labels()

    def _fit_labels(self):
        """Drop the Orientation/Theme/Background labels and shorten
        "Relationship labels" when the row is short of room, so no control is
        squeezed. Each keeps its accessible name and tooltip."""
        if not self._diagram_visible:
            return
        if self._row1_labels[0].isVisible():
            margins = self.layout().contentsMargins()
            self._full_width = self._row1.sizeHint().width() + margins.left() + margins.right()
        compact = self.width() < self._full_width
        for label in self._row1_labels:
            label.setVisible(not compact)
        self._show_rel_labels.setText("La&bels" if compact else "Relationship la&bels")

    # -- state chips --------------------------------------------------------
    def _chip(self, on_click) -> QPushButton:
        chip = QPushButton()
        chip.setFlat(True)
        chip.setCursor(Qt.PointingHandCursor)
        chip.clicked.connect(on_click)
        chip.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)  # never elide
        return chip

    def state_chips(self) -> list[QPushButton]:
        """The chips, for the window to place (the bar itself is full)."""
        return [self._keys_chip, self._audit_chip]

    def set_audit_share(self, share: float):
        """How much of the schema's links are audit/system ones (0–1)."""
        self._audit_share = share
        self._sync_chips()

    def set_keys_only_auto(self):
        """Switch Keys only on for the user, marked as the app's choice."""
        self._keys_only.setChecked(True)  # re-renders via changed
        self._keys_auto = True
        self._sync_chips()

    def keys_only_auto(self) -> bool:
        return self._keys_auto and self._keys_only.isChecked()

    def _on_keys_toggled(self, _on: bool):
        self._keys_auto = False  # any change after the app's makes it the user's
        self._sync_chips()

    def _sync_chips(self):
        audit = self._hide_audit.isChecked() and self._audit_share > 0
        if audit:
            share = f"{self._audit_share:.0%}"
            self._audit_chip.setText(f"Audit links hidden ({share})  ✕")
            self._audit_chip.setToolTip(
                f"createdby, modifiedby, owning… and similar links are {share} of "
                "all links, so the map, diagram, SQL and exports leave them out. "
                "Click to show them."
            )
            self._audit_chip.setAccessibleName(
                f"Audit links hidden ({share}). Click to show them"
            )
        self._audit_chip.setVisible(audit)
        self._keys_chip.setVisible(self._diagram_visible and self.keys_only_auto())

    def add_trailing_widget(self, widget):
        """Add a control at the right end of the first row (e.g. Details)."""
        self._row1.addWidget(widget)

    @staticmethod
    def _buddy(text: str, widget) -> QLabel:
        label = QLabel(text)
        label.setBuddy(widget)
        widget.setAccessibleName(text.replace("&", "").rstrip(":"))
        return label

    def _on_more_toggled(self, open_: bool):
        self._more.setArrowType(Qt.DownArrow if open_ else Qt.RightArrow)
        self._more_box.setVisible(open_)

    def _combo(self, pairs) -> QComboBox:
        combo = QComboBox()
        for label, value in pairs:
            combo.addItem(label, value)
        combo.currentIndexChanged.connect(lambda _i: self.changed.emit())
        return combo

    # -- read the current choices ------------------------------------------
    def render_style(self) -> RenderStyle:
        padding, min_w, min_h = self._spacing.currentData()
        return RenderStyle(
            theme=self._theme.currentData(),
            background=self._background.currentData(),
            layout_direction=self._orientation.currentData(),
            entity_padding=padding,
            min_entity_width=min_w,
            min_entity_height=min_h,
            use_max_width=self._fit_width.isChecked(),
            font_size=self._font.value(),
        )

    def diagram_options(self) -> DiagramOptions:
        return DiagramOptions(
            show_comments=self._show_comments.isChecked(),
            show_rel_labels=self._show_rel_labels.isChecked(),
            prefix_schema=self._prefix_schema.isChecked(),
            keys_only=self._keys_only.isChecked(),
            hide_columns=HIDDEN_BY_DEFAULT if self._hide_audit.isChecked() else frozenset(),
        )

    def hide_audit_links(self) -> bool:
        return self._hide_audit.isChecked()

    def background_value(self) -> str:
        return self._background.currentData()

    def retheme(self):
        style = (
            f"QPushButton {{ border: 1px solid {_control_border_hex(self)}; border-radius: 10px;"
            f" padding: 2px 10px; color: {_muted_hex(self)}; background: transparent; }}"
            f"QPushButton:hover {{ background: {_ACCENT_WASH}; }}"
            f"QPushButton:focus {{ background: {_ACCENT_WASH}; border-color: {_ACCENT}; }}"
        )
        for chip in (self._audit_chip, self._keys_chip):
            chip.setStyleSheet(style)

    # Theme and background are left out on purpose: they follow the OS's light
    # or dark mode at every launch (see apply_system_defaults).
    def _remembered(self):
        return [
            ("orientation", self._orientation),
            ("spacing", self._spacing),
            ("font", self._font),
            ("fit_width", self._fit_width),
            ("notes", self._show_comments),
            ("rel_labels", self._show_rel_labels),
            ("prefix_schema", self._prefix_schema),
            ("keys_only", self._keys_only),
            ("hide_audit", self._hide_audit),
            ("more_open", self._more),
        ]

    def save_state(self, settings: QSettings):
        for key, widget in self._remembered():
            if isinstance(widget, QComboBox):
                value = widget.currentIndex()
            elif isinstance(widget, QSpinBox):
                value = widget.value()
            else:
                value = widget.isChecked()
            settings.setValue(f"diagram/{key}", value)

    def restore_state(self, settings: QSettings):
        """Put back the choices from the last session, without re-rendering."""
        for key, widget in self._remembered():
            value = settings.value(f"diagram/{key}")
            if value is None:
                continue
            widget.blockSignals(True)
            try:
                if isinstance(widget, QComboBox):
                    index = int(value)
                    if 0 <= index < widget.count():
                        widget.setCurrentIndex(index)
                elif isinstance(widget, QSpinBox):
                    widget.setValue(int(value))
                else:
                    widget.setChecked(str(value).lower() in ("true", "1"))
            except (TypeError, ValueError):
                pass  # a hand-edited or stale value: keep the default
            finally:
                widget.blockSignals(False)
        self._on_more_toggled(self._more.isChecked())

    def apply_system_defaults(self, dark: bool, notify: bool = False) -> bool:
        """Default the diagram's own theme + background to match the OS.

        Called at launch and on every light/dark switch, until the user picks a
        theme or background themselves (for the rest of the session). Returns
        whether anything changed; ``notify`` then emits :attr:`changed` so the
        diagram is redrawn in its new colours.
        """
        if not self._follow_system:
            return False
        before = (self._theme.currentIndex(), self._background.currentIndex())
        blocked = [
            (w, w.blockSignals(True))
            for w in (self._theme, self._background)
        ]
        self._theme.setCurrentIndex(2 if dark else 0)  # Dark : Default
        self._background.setCurrentIndex(2 if dark else 0)  # Dark : White
        for widget, _ in blocked:
            widget.blockSignals(False)
        changed = before != (self._theme.currentIndex(), self._background.currentIndex())
        if changed and notify:
            self.changed.emit()
        return changed

    def _stop_following_system(self, _index=None):
        self._follow_system = False

    def follows_system(self) -> bool:
        return self._follow_system
