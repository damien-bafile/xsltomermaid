"""Qt (PySide6) desktop app: drag an Excel schema file in, get a Mermaid ER diagram.

Run with:  python main.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import webbrowser


def _configure_headless_env(argv: list[str] | None = None) -> None:
    """Set Qt/WebEngine env vars for screenshot modes before Qt imports."""
    args = argv if argv is not None else sys.argv[1:]
    headless = "--screenshot" in args or "--screenshot-diagram" in args
    if not headless:
        return

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    if "--screenshot-diagram" in args and os.environ.get("QT_QPA_PLATFORM") == "offscreen":
        # Chromium (WebEngine) needs these to run headless / as root in CI.
        os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
        os.environ.setdefault(
            "QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox --disable-gpu --in-process-gpu"
        )


_configure_headless_env()

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from diagram_view import DiagramView
from excel_to_mermaid import (
    EXPECTED_HEADERS,
    Schema,
    build_schema,
    generate_mermaid,
    read_rows,
    wrap_mermaid_html,
)

_ACCEPTED_SUFFIXES = (".xlsx", ".xlsm", ".xltx", ".xltm")


class DropArea(QLabel):
    """A large label that accepts a dragged spreadsheet file."""

    def __init__(self, on_file, parent=None):
        super().__init__(parent)
        self._on_file = on_file
        self.setAcceptDrops(True)
        self.setAlignment(Qt.AlignCenter)
        self.setWordWrap(True)
        self.setText(
            "\n\n⬇  Drag an Excel schema file here\n\n"
            "(.xlsx / .xlsm)  —  or click to browse\n\n"
        )
        self.setObjectName("dropArea")
        self.setMinimumHeight(120)
        self.setStyleSheet(
            "#dropArea {"
            "  border: 2px dashed #8a8f98;"
            "  border-radius: 12px;"
            "  color: #6b7078;"
            "  font-size: 15px;"
            "}"
        )

    def mousePressEvent(self, event):  # noqa: N802 (Qt naming)
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
                "  border: 2px solid #2f81f7;"
                "  border-radius: 12px;"
                "  color: #2f81f7;"
                "  font-size: 15px;"
                "  background: rgba(47,129,247,0.08);"
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
        self.setStyleSheet(
            "#dropArea {"
            "  border: 2px dashed #8a8f98;"
            "  border-radius: 12px;"
            "  color: #6b7078;"
            "  font-size: 15px;"
            "}"
        )


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Excel Schema → Mermaid ER Diagram")
        self.resize(1100, 760)

        self._schema: Schema | None = None
        self._mermaid_text: str = ""

        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(12)

        self._drop = DropArea(self.load_file)
        outer.addWidget(self._drop)

        self._status = QLabel("No file loaded.")
        self._status.setStyleSheet("color: #6b7078;")
        outer.addWidget(self._status)

        # Tabs: extracted data table + generated Mermaid text.
        tabs = QTabWidget()

        self._table = QTableWidget(0, len(EXPECTED_HEADERS))
        self._table.setHorizontalHeaderLabels(EXPECTED_HEADERS)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        tabs.addTab(self._table, "Extracted data")

        self._mermaid_view = QPlainTextEdit()
        self._mermaid_view.setReadOnly(True)
        self._mermaid_view.setFont(QFont("Menlo, Consolas, monospace"))
        self._mermaid_view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self._mermaid_view.setPlaceholderText(
            "The generated Mermaid erDiagram will appear here."
        )
        tabs.addTab(self._mermaid_view, "Mermaid source")

        self._diagram_view = DiagramView()
        tabs.addTab(self._diagram_view, "Rendered diagram")

        self._tabs = tabs
        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(tabs)
        outer.addWidget(splitter, 1)

        # Action buttons.
        buttons = QHBoxLayout()
        self._copy_btn = QPushButton("Copy Mermaid")
        self._save_mmd_btn = QPushButton("Save .mmd")
        self._save_md_btn = QPushButton("Save .md")
        self._save_png_btn = QPushButton("Save diagram PNG")
        self._save_svg_btn = QPushButton("Save diagram SVG")
        self._preview_btn = QPushButton("Preview in browser")
        self._action_buttons = [
            self._copy_btn,
            self._save_mmd_btn,
            self._save_md_btn,
            self._save_png_btn,
            self._save_svg_btn,
            self._preview_btn,
        ]
        for btn in self._action_buttons:
            btn.setEnabled(False)
            buttons.addWidget(btn)
        buttons.addStretch(1)
        outer.addLayout(buttons)

        # Diagram export needs WebEngine; hide those buttons if it's unavailable.
        if not self._diagram_view.available:
            self._save_png_btn.setVisible(False)
            self._save_svg_btn.setVisible(False)

        self._copy_btn.clicked.connect(self.copy_mermaid)
        self._save_mmd_btn.clicked.connect(self.save_mmd)
        self._save_md_btn.clicked.connect(self.save_md)
        self._save_png_btn.clicked.connect(self.save_diagram_png)
        self._save_svg_btn.clicked.connect(self.save_diagram_svg)
        self._preview_btn.clicked.connect(self.preview_browser)

        self.setCentralWidget(central)

    # -- loading -----------------------------------------------------------
    def load_file(self, path: str):
        try:
            rows = read_rows(path)
            schema = build_schema(rows)
            mermaid_text = generate_mermaid(schema)
        except Exception as exc:  # noqa: BLE001 - surface any parse error to the user
            QMessageBox.critical(self, "Could not read file", str(exc))
            return

        self._schema = schema
        self._mermaid_text = mermaid_text
        self._populate_table(rows)
        self._mermaid_view.setPlainText(mermaid_text)
        self._diagram_view.set_diagram(mermaid_text)

        table_count = len(schema.tables)
        rel_count = len(schema.relationships)
        col_count = sum(len(t.columns) for t in schema.tables)
        self._status.setText(
            f"Loaded {os.path.basename(path)} — {table_count} table(s), "
            f"{col_count} column(s), {rel_count} relationship(s)."
        )
        for btn in self._action_buttons:
            btn.setEnabled(True)

    def _populate_table(self, rows: list[dict]):
        self._table.setRowCount(0)
        # Loose header matching so extra/renamed columns still line up.
        norm_map = {self._key(h): h for h in EXPECTED_HEADERS}
        self._table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            row_by_key = {self._key(k): v for k, v in row.items()}
            for c, header in enumerate(EXPECTED_HEADERS):
                value = row_by_key.get(self._key(header))
                item = QTableWidgetItem("" if value is None else str(value))
                self._table.setItem(r, c, item)
        self._table.resizeColumnsToContents()

    @staticmethod
    def _key(text: str) -> str:
        return "".join(ch for ch in str(text).lower() if ch.isalnum())

    # -- actions -----------------------------------------------------------
    def copy_mermaid(self):
        QGuiApplication.clipboard().setText(self._mermaid_text)
        self._status.setText("Mermaid diagram copied to clipboard.")

    def save_mmd(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Mermaid file", "schema.mmd", "Mermaid (*.mmd);;All files (*)"
        )
        if path:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(self._mermaid_text)
            self._status.setText(f"Saved {path}")

    def save_md(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Markdown file", "schema.md", "Markdown (*.md);;All files (*)"
        )
        if path:
            content = f"# Database ER Diagram\n\n```mermaid\n{self._mermaid_text.strip()}\n```\n"
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(content)
            self._status.setText(f"Saved {path}")

    def preview_browser(self):
        html = wrap_mermaid_html(self._mermaid_text)
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".html", delete=False, encoding="utf-8"
        )
        tmp.write(html)
        tmp.close()
        webbrowser.open(f"file://{tmp.name}")
        self._status.setText(
            "Opened diagram preview in your browser (needs internet for Mermaid CDN)."
        )

    def save_diagram_png(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save diagram PNG", "diagram.png", "PNG image (*.png)"
        )
        if path:
            try:
                self._diagram_view.save_png(path)
            except Exception as exc:  # noqa: BLE001
                QMessageBox.critical(self, "Could not save diagram", str(exc))
                return
            self._status.setText(f"Saved rendered diagram to {path}")

    def save_diagram_svg(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save diagram SVG", "diagram.svg", "SVG image (*.svg)"
        )
        if path:
            try:
                self._diagram_view.save_svg(path)
            except Exception as exc:  # noqa: BLE001
                QMessageBox.critical(self, "Could not save diagram", str(exc))
                return
            self._status.setText(f"Saved rendered diagram to {path}")

    # -- testing helpers ---------------------------------------------------
    def capture(self, path: str) -> str:
        """Render the current window to a PNG and return the saved path.

        Works headless (with ``QT_QPA_PLATFORM=offscreen``) so it can be used in
        automated tests / CI to verify the UI actually paints.
        """
        app = QApplication.instance()
        if app is not None:
            # Let layout, resizing and painting settle before grabbing.
            app.processEvents()
        pixmap = self.grab()
        if not pixmap.save(path, "PNG"):
            raise RuntimeError(f"Failed to save screenshot to {path}")
        return path

    def capture_diagram(self, path: str) -> str:
        """Render the Mermaid ER diagram itself and save it (.png or .svg).

        Unlike :meth:`capture` (which grabs the Qt window), this saves the actual
        rendered ER diagram. Works headless via the vendored mermaid.js + QtSvg.
        """
        if not self._diagram_view.available:
            raise RuntimeError(
                "Rendering the diagram needs PySide6's WebEngine module "
                "(install PySide6-Addons)."
            )
        if path.lower().endswith(".svg"):
            return self._diagram_view.save_svg(path)
        return self._diagram_view.save_png(path)


def main(argv: list[str] | None = None):
    import argparse

    parser = argparse.ArgumentParser(
        description="Excel database-schema → Mermaid ER diagram (Qt app)."
    )
    parser.add_argument(
        "file",
        nargs="?",
        help="Optional Excel file to load on startup (.xlsx/.xlsm).",
    )
    parser.add_argument(
        "--screenshot",
        metavar="PNG",
        help="Headless self-test: load the file, save a PNG of the whole window, "
        "and exit without showing the GUI. Use with QT_QPA_PLATFORM=offscreen in CI.",
    )
    parser.add_argument(
        "--screenshot-diagram",
        metavar="FILE",
        help="Headless: load the file, render the Mermaid ER diagram, and save it as "
        ".png or .svg (by extension), then exit. Works offscreen.",
    )
    args = parser.parse_args(argv)

    _configure_headless_env(argv)

    app = QApplication(sys.argv[:1])
    window = MainWindow()
    if args.file:
        window.load_file(args.file)

    if args.screenshot:
        window.resize(1100, 760)
        saved = window.capture(args.screenshot)
        print(f"Saved window screenshot to {saved}")
        return 0

    if args.screenshot_diagram:
        if not args.file:
            parser.error("--screenshot-diagram requires a file argument to render.")
        saved = window.capture_diagram(args.screenshot_diagram)
        print(f"Saved diagram to {saved}")
        return 0

    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
