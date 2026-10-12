#!/usr/bin/env python3
"""Gera assets/icon.png (512) e assets/icon.ico a partir de assets/icon.svg.

Uso: python scripts/make_icons.py   (precisa de PySide6 e Pillow)
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SVG = ROOT / "assets" / "icon.svg"


def render(size: int) -> QImage:
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.SmoothPixmapTransform)
    QSvgRenderer(str(SVG)).render(p)
    p.end()
    return img


def main() -> int:
    app = QGuiApplication(sys.argv)  # noqa: F841
    big = ROOT / "assets" / "icon.png"
    render(512).save(str(big))
    base = Image.open(big).convert("RGBA")
    base.save(ROOT / "assets" / "icon.ico",
              sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("ok:", big, ROOT / "assets" / "icon.ico")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
