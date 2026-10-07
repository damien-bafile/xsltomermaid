"""Small GUI pieces: the drop area, status line, render status, data model, workers."""

from __future__ import annotations

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    Qt,
    QThread,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QTextDocumentFragment,
)
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QWidget,
)

from .config import _ACCEPTED_SUFFIXES
from .services import SchemaImportService
from .theme import (
    _ACCENT,
    _ACCENT_WASH,
    _line_hex,
    _link_hex,
    _muted_hex,
    _ok_hex,
    _warn_hex,
    announce,
    status_icon,
)
from .updates import (
    fetch_latest_release,
    UpdateCheckError,
)


class DropArea(QLabel):
    """A large label that accepts a dragged spreadsheet file."""

    _IDLE_TEXT = (
        "\n\nDrag an Excel schema file here\n\n"
        "(.xlsx / .xlsm)  —  or click to browse\n\n"
    )

    def __init__(self, on_file, parent=None):
        super().__init__(parent)
        self._on_file = on_file
        self._compact = False
        self.setAcceptDrops(True)
        self.setAlignment(Qt.AlignCenter)
        self.setWordWrap(True)
        self.setText(self._IDLE_TEXT)
        self.setObjectName("dropArea")
        self.setMinimumHeight(120)
        # Keyboard-operable: a keyboard-only user can focus it and press
        # Enter/Space to browse, so loading a file never requires the mouse.
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAccessibleName("Excel schema drop area")
        self.setAccessibleDescription(
            "Drop an .xlsx or .xlsm schema file here, or press Enter to browse."
        )
        self._reset_style()

    def keyPressEvent(self, event):  # noqa: N802 (Qt naming)
        if event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self._browse()
            event.accept()
            return
        super().keyPressEvent(event)

    def set_loaded(self, name: str):
        """Shrink to a slim file chip once a schema is loaded.

        The big idle target is worth its height only until a file is in; after
        that it becomes a compact bar so the tabs get the room. The whole area
        stays a drop target and click-to-browse, so replacing the file is a
        drop or a click away.
        """
        self._compact = True
        self.setText(f"{name}     ·     drop or click to load another file")
        self.setMinimumHeight(0)
        self.setMaximumHeight(46)
        self._reset_style()

    def mousePressEvent(self, event):  # noqa: N802 (Qt naming)
        self._browse()

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select an Excel schema file",
            "",
            "Excel files (*.xlsx *.xlsm *.xltx *.xltm);;All files (*)",
        )
        if path:
            self._on_file(path)

    def dragEnterEvent(self, event):  # noqa: N802
        if self._has_valid_url(event):
            event.acceptProposedAction()
            self.setStyleSheet(
                "#dropArea {"
                f"  border: 2px solid {_ACCENT};"
                "  border-radius: 12px;"
                f"  color: {_link_hex(self)};"
                "  font-size: 15px;"
                f"  background: {_ACCENT_WASH};"
                "}"
            )
        else:
            event.ignore()

    def dragLeaveEvent(self, event):  # noqa: N802
        self._reset_style()

    def dropEvent(self, event):  # noqa: N802
        self._reset_style()
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path.lower().endswith(_ACCEPTED_SUFFIXES):
                self._on_file(path)
                return
        QMessageBox.warning(
            self, "Unsupported file", "Please drop an .xlsx or .xlsm file."
        )

    @staticmethod
    def _has_valid_url(event) -> bool:
        if not event.mimeData().hasUrls():
            return False
        return any(
            url.toLocalFile().lower().endswith(_ACCEPTED_SUFFIXES)
            for url in event.mimeData().urls()
        )

    def _reset_style(self):
        # A keyboard-focus ring in the accent colour, on either variant.
        link = _link_hex(self)
        focus = f"#dropArea:focus {{ border-color: {link}; color: {link}; }}"
        if self._compact:
            self.setStyleSheet(
                "#dropArea {"
                f"  border: 1px solid {_line_hex(self)};"
                "  border-radius: 8px;"
                f"  color: {_muted_hex(self)};"
                "  font-size: 13px;"
                "  padding: 4px 12px;"
                "}"
                + focus
            )
        else:
            self.setStyleSheet(
                "#dropArea {"
                f"  border: 2px dashed {_line_hex(self)};"
                "  border-radius: 12px;"
                f"  color: {_muted_hex(self)};"
                "  font-size: 15px;"
                "}"
                + focus
            )

    def retheme(self):
        """Re-apply the idle style for the current palette (light/dark)."""
        self._reset_style()


class StatusLabel(QLabel):
    """The window's status line; every change is also read out to screen readers.

    Messages such as "copied" or "saved" are otherwise visual only.
    """

    def setText(self, text: str):  # noqa: N802 - Qt override
        super().setText(text)
        if text:
            plain = QTextDocumentFragment.fromHtml(text).toPlainText() if "<" in text else text
            announce(self, plain)


class LoadWorker(QThread):
    """Read + parse a spreadsheet off the UI thread, reporting progress.

    The heavy work (streaming the workbook and building the schema) runs here so
    the window stays responsive and can show a live progress bar. Only the final
    ``loaded`` payload is handed back to the UI thread, which then touches the
    widgets.
    """

    progressed = Signal(int)  # overall percentage, 0..100
    staged = Signal(str)  # human-readable phase label
    loaded = Signal(object, object, str)  # rows, schema, mermaid_text
    failed = Signal(str)  # error message

    def __init__(self, path: str, parent=None, importer=None):
        super().__init__(parent)
        self._path = path
        self._importer = importer or SchemaImportService()

    def run(self):  # noqa: D401 - QThread entry point
        try:
            previous_stage = None

            def report_progress(stage: str, percent: int) -> None:
                nonlocal previous_stage
                if stage != previous_stage:
                    self.staged.emit(stage)
                    previous_stage = stage
                self.progressed.emit(percent)

            rows, schema, mermaid_text = self._importer.load(
                self._path, report_progress
            )
            self.loaded.emit(rows, schema, mermaid_text)
        except Exception as exc:  # noqa: BLE001 - surface any parse error to the UI
            self.failed.emit(str(exc))


class UpdateCheckWorker(QThread):
    """Ask GitHub for the latest release off the UI thread (network I/O)."""

    found = Signal(object)  # updates.Release
    failed = Signal(str)  # user-presentable error message

    def run(self):  # noqa: D401 - QThread entry point
        try:
            self.found.emit(fetch_latest_release())
        except UpdateCheckError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - never crash the app on a check
            self.failed.emit(f"Unexpected error: {exc}")


class RenderStatus(QWidget):
    """A small spinner while the diagram renders, then a tick when it's done."""

    def __init__(self, parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)

        self._icon = QLabel()
        self._icon.setFixedWidth(16)
        self._icon.setAlignment(Qt.AlignCenter)
        self._text = QLabel()
        self._text.setStyleSheet(f"color: {_muted_hex(self)};")
        row.addWidget(self._icon)
        row.addWidget(self._text)

        self._timer = QTimer(self)
        self._timer.setInterval(70)
        self._timer.timeout.connect(self._spin)
        self._frame = 0
        self.setVisible(False)

    def _spin(self):
        self._frame = (self._frame + 1) % 12
        self._icon.setPixmap(status_icon("spin", _ACCENT, angle=self._frame * 30))

    def start(self):
        self.setToolTip("")  # a previous failure's reason no longer applies
        self._frame = 0
        self._icon.setPixmap(status_icon("spin", _ACCENT))
        self._text.setText("Rendering…")
        self.setVisible(True)
        self._timer.start()

    def finish(self, ok: bool = True):
        self._timer.stop()
        if ok:
            self._icon.setPixmap(status_icon("ok", _ok_hex(self)))
            self._text.setText("Rendered")
        else:
            self._icon.setPixmap(status_icon("warn", _warn_hex(self)))
            self._text.setText("Render failed")
        self.setVisible(True)

    def stale(self, text: str):
        """The drawn diagram no longer matches the selection."""
        self._timer.stop()
        self._icon.setPixmap(status_icon("stale", _warn_hex(self)))
        self._text.setText(text)
        self.setVisible(True)

    def clear(self):
        self._timer.stop()
        self.setVisible(False)

    def retheme(self):
        self._text.setStyleSheet(f"color: {_muted_hex(self)};")


class ExtractedDataModel(QAbstractTableModel):
    """Read-only model for the raw extracted rows.

    Backing the Extracted-data tab with a model + ``QTableView`` means only the
    visible cells are realised, instead of building a widget item for every cell
    (which was ~n_rows × 17 items on the UI thread for large sheets). The cell
    string doubles as its tooltip, so the full value is available on hover for
    elided cells without any per-cell allocation.
    """

    def __init__(self, headers: list[str], parent=None):
        super().__init__(parent)
        self._headers = list(headers)
        self._rows: list[list[str]] = []

    def set_rows(self, rows: list[list[str]]):
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._headers)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        value = self._rows[index.row()][index.column()]
        if role == Qt.UserRole:
            if self._headers[index.column()] in ("ColumnOrder", "Length", "Precision", "Scale"):
                try:
                    return int(value)
                except (ValueError, TypeError):
                    return -1
            return value.casefold()
        if role in (Qt.DisplayRole, Qt.ToolTipRole):
            return value or None
        return None

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal:
            return self._headers[section]
        return section + 1  # 1-based row numbers, like the old vertical header
