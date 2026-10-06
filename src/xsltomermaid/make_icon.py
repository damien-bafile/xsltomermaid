"""Render ``assets/app_icon.svg`` to the PNG + multi-size ICO the app uses.

Run:  uv run python -m xsltomermaid.make_icon (needs PySide6; QtSvg does the rasterising)

Outputs:
    assets/app_icon.png   256x256 PNG (window icon on Linux/macOS, previews)
    assets/app_icon.ico   multi-size Windows icon (used for the .exe)
"""

from __future__ import annotations

import struct
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

# Sizes to bake into the .ico (Windows picks the best one per context).
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]
HERE = Path(__file__).resolve().parent
SVG = HERE / "assets" / "app_icon.svg"


def _render(renderer: QSvgRenderer, size: int) -> QImage:
    """Render the SVG into a transparent square QImage of the given size."""
    image = QImage(size, size, QImage.Format_ARGB32)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
    renderer.render(painter)
    painter.end()
    return image


def _png_bytes(image: QImage) -> bytes:
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


def _write_ico(pngs: list[bytes], sizes: list[int], path: Path) -> None:
    """Assemble a PNG-compressed .ico (Vista+ format) from encoded images."""
    count = len(pngs)
    header = struct.pack("<HHH", 0, 1, count)  # reserved, type=icon, count
    entries = b""
    offset = 6 + count * 16  # header + directory entries
    for png, size in zip(pngs, sizes):
        dim = 0 if size >= 256 else size  # 0 means 256 in the ICO spec
        entries += struct.pack(
            "<BBBBHHII",
            dim,  # width
            dim,  # height
            0,  # palette count
            0,  # reserved
            1,  # colour planes
            32,  # bits per pixel
            len(png),  # size of image data
            offset,  # offset of image data
        )
        offset += len(png)
    path.write_bytes(header + entries + b"".join(pngs))


def main() -> None:
    # QGuiApplication is needed for QImage/QPainter to work off-screen.
    app = QGuiApplication.instance() or QGuiApplication([])

    renderer = QSvgRenderer(QByteArray(SVG.read_bytes()))
    if not renderer.isValid():
        raise SystemExit(f"Could not load SVG: {SVG}")

    images = {size: _render(renderer, size) for size in ICO_SIZES}

    png_path = HERE / "assets" / "app_icon.png"
    images[256].save(str(png_path), "PNG")

    ico_path = HERE / "assets" / "app_icon.ico"
    _write_ico([_png_bytes(images[s]) for s in ICO_SIZES], ICO_SIZES, ico_path)

    print(f"Wrote {png_path.relative_to(HERE)} and {ico_path.relative_to(HERE)}")
    del app  # keep linters quiet; app is a singleton


if __name__ == "__main__":
    main()
