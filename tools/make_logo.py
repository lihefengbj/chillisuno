"""生成 chillisuno 应用 Logo：圆角方形渐变底 + 声波柱。

运行：.venv\\Scripts\\python.exe tools/make_logo.py
产物：assets/logo.png（512px）、assets/icon.png（256px）
"""

import math
import os

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QGuiApplication,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
)

SIZE = 512
RADIUS_RATIO = 0.225

# 声波柱：相对高度（0~1），7 根，中间最高
BARS = [0.30, 0.52, 0.74, 0.95, 0.74, 0.52, 0.30]


def render(size: int) -> QImage:
    img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)

    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    # 圆角方形渐变底（商务蓝 -> 紫）
    path = QPainterPath()
    r = size * RADIUS_RATIO
    path.addRoundedRect(QRectF(0, 0, size, size), r, r)
    grad = QLinearGradient(0, 0, size, size)
    grad.setColorAt(0.0, QColor("#4F7CFF"))
    grad.setColorAt(1.0, QColor("#8E5BFF"))
    p.fillPath(path, QBrush(grad))

    # 声波柱
    n = len(BARS)
    field_w = size * 0.56
    gap_ratio = 0.55  # 柱宽 : 间距
    bar_w = field_w / (n + (n - 1) * gap_ratio)
    gap = bar_w * gap_ratio
    x0 = (size - field_w) / 2
    max_h = size * 0.46
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(255, 255, 255, 235))
    for i, h in enumerate(BARS):
        bh = max_h * h
        x = x0 + i * (bar_w + gap)
        y = (size - bh) / 2
        p.drawRoundedRect(QRectF(x, y, bar_w, bh), bar_w / 2, bar_w / 2)

    p.end()
    return img


def main() -> None:
    os.makedirs("assets", exist_ok=True)
    logo = render(SIZE)
    logo.save("assets/logo.png")
    icon = logo.scaled(
        256,
        256,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    icon.save("assets/icon.png")
    print("assets/logo.png, assets/icon.png 已生成")


if __name__ == "__main__":
    app = QGuiApplication([])
    main()
