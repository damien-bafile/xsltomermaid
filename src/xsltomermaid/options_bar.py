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
    QSizePolicy,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .diagram_view import RenderStyle
from .excel_to_mermaid import DiagramOptions
from .schema_map import HIDDEN_BY_DEFAULT
from .theme import _ACCENT_WASH


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
        self._theme = self._combo(self._THEMES)
        self._background = self._combo(self._BACKGROUNDS)
        self._font = QSpinBox()
        self._font.setRange(8, 28)
        self._font.setValue(12)
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
        self._show_rel_labels.setToolTip("Name the foreign-key column on each line.")
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
        for label, widget in [
            ("&Orientation:", self._orientation),
            ("&Theme:", self._theme),
            ("Back&ground:", self._background),
        ]:
            buddy = self._buddy(label, widget)
            row1.addWidget(buddy)
            row1.addWidget(widget)
            self._diagram_only += [buddy, widget]
        row1.addSpacing(8)
        row1.addWidget(self._show_rel_labels)
        row1.addWidget(self._keys_only)
        self._diagram_only += [self._show_rel_labels, self._keys_only]
        row1.addStretch(1)

        # The rest is fine-tuning, so it sits behind a disclosure, closed by
        # default, that keeps the bar to one line.
        self._more = QToolButton()
        self._more.setText("More")
        self._more.setCheckable(True)
        self._more.setArrowType(Qt.RightArrow)
        self._more.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self._more.setToolTip("Spacing, font size, width, notes and schema prefix.")
        self._more.setStyleSheet(
            "QToolButton { border: none; padding: 2px 6px; border-radius: 4px; }"
            f"QToolButton:hover, QToolButton:focus {{ background: {_ACCENT_WASH}; }}"
            "QToolButton:checked { background: transparent; }"
            f"QToolButton:checked:hover {{ background: {_ACCENT_WASH}; }}"
        )
        self._more.toggled.connect(self._on_more_toggled)
        row1.addWidget(self._more)
        self._diagram_only.append(self._more)
        self._row1 = row1
        outer.addLayout(row1)

        self._more_box = QWidget()
        row2 = QHBoxLayout(self._more_box)
        row2.setContentsMargins(0, 0, 0, 0)
        row2.setSpacing(8)
        for label, widget in [
            ("Spac&ing:", self._spacing),
            ("Font si&ze:", self._font),
        ]:
            row2.addWidget(self._buddy(label, widget))
            row2.addWidget(widget)
        row2.addSpacing(8)
        for chk in (self._fit_width, self._show_comments, self._prefix_schema, self._hide_audit):
            row2.addWidget(chk)
        row2.addStretch(1)
        self._more_box.setVisible(False)
        outer.addWidget(self._more_box)

    def add_leading_widget(self, widget):
        """Add a control at the left end of the first row (the view switch)."""
        self._row1.insertWidget(0, widget)

    def set_diagram_controls_visible(self, visible: bool):
        """Show the diagram-only options (hidden while the map is showing)."""
        for widget in self._diagram_only:
            widget.setVisible(visible)
        self._more_box.setVisible(visible and self._more.isChecked())

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
        # All labels here use the default palette text colour, which already
        # follows the light/dark scheme — nothing hand-coloured to update.
        pass

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

    def apply_system_defaults(self, dark: bool):
        """Default the diagram's own theme + background to match the OS."""
        blocked = [
            (w, w.blockSignals(True))
            for w in (self._theme, self._background)
        ]
        self._theme.setCurrentIndex(2 if dark else 0)  # Dark : Default
        self._background.setCurrentIndex(2 if dark else 0)  # Dark : White
        for widget, _ in blocked:
            widget.blockSignals(False)
