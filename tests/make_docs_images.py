#!/usr/bin/env python
"""Render documentation images with synthetic data.

Real captures leak whatever happens to be on screen; these are deterministic
and show each mode doing its job.
"""
import importlib.util
import math
import os
import sys

from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QColor, QImage, QMouseEvent, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication

spec = importlib.util.spec_from_file_location("k", "kcircle.py")
k = importlib.util.module_from_spec(spec)
spec.loader.exec_module(k)
k.logg = k.setup_logging("error")

from PIL import Image, ImageDraw

OUT = os.environ.get("KCIRCLE_DOCS_DIR", "/tmp/kcircle-docs")
os.makedirs(OUT, exist_ok=True)
W, H = 1400, 850


def scene():
    im = Image.new("RGB", (W, H), (14, 17, 24))
    d = ImageDraw.Draw(im)
    for y in range(0, H, 34):                       # fake app chrome
        d.line([(0, y), (W, y)], fill=(22, 26, 34), width=1)
    d.rectangle([0, 0, W, 46], fill=(30, 35, 45))
    d.text((16, 18), "Reference window", fill=(150, 158, 170))
    d.rounded_rectangle([360, 190, 1040, 660], 18,
                        fill=(196, 74, 86))          # the subject
    d.ellipse([470, 250, 930, 560], fill=(72, 196, 150))
    d.rectangle([360, 190, 1040, 660], outline=(255, 255, 255, 40), width=3)
    p = "/tmp/opencode/_doc_scene.png"
    im.save(p)
    return p


SHIFT = Qt.KeyboardModifier.ShiftModifier
NONE = Qt.KeyboardModifier.NoModifier
PRESS, MOVE, RELEASE = (QEvent.Type.MouseButtonPress,
                       QEvent.Type.MouseMove,
                       QEvent.Type.MouseButtonRelease)


def ev(t, pos, mods=NONE):
    if t == PRESS:
        b, br = Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton
    elif t == MOVE:
        b, br = Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton
    else:
        b, br = Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton
    return QMouseEvent(t, QPointF(*pos), b, br, mods)


def circle(cx, cy, r, n=64):
    return [(cx + r * math.cos(2 * math.pi * i / n),
             cy + r * 0.78 * math.sin(2 * math.pi * i / n)) for i in range(n)]


def render(mode, pts, mods, src, name, glow=False):
    ov = k.SelectionOverlay(src)
    ov.resize(W, H)
    ov.mode = mode
    ov.show()
    ov.mousePressEvent(ev(PRESS, pts[0], mods))
    for p in pts[1:]:
        ov.mouseMoveEvent(ev(MOVE, p))
    if glow:
        ov.glow.t = 0.72
        ov._glow_timer.stop()
    pm = QPixmap(W, H)
    pm.fill(QColor("black"))
    ov.render(pm)
    ov._reset()
    path = os.path.join(OUT, name)
    pm.save(path)
    print(f"  wrote {path}")


def render_crops(src):
    """A grid of the actual crops that get uploaded, with the masked edges."""
    modes = [
        ("rect", [(360, 190), (1040, 660)], NONE, "rect"),
        ("corner+Shift", [(360, 190), (900, 500)], SHIFT, "square"),
        ("ellipse", [(360, 190), (1040, 660)], NONE, "ellipse"),
        ("lasso", circle(700, 425, 340), NONE, "lasso"),
    ]
    tiles = []
    for mode, pts, mods, label in modes:
        ov = k.SelectionOverlay(src)
        ov.resize(W, H)
        ov.mode = mode.replace("+Shift", "")
        got = {}
        ov.picked.connect(lambda s: got.__setitem__("s", s))
        ov.mousePressEvent(ev(PRESS, pts[0], mods))
        for p in pts[1:]:
            ov.mouseMoveEvent(ev(MOVE, p))
        ov.mouseReleaseEvent(ev(RELEASE, pts[-1]))
        sel = got.get("s")
        if not sel:
            continue
        region, poly = sel
        crop = k.crop_region(src, region, poly)
        c = Image.open(crop)
        if c.width > 260:
            c = c.resize((260, int(c.height * 260 / c.width)))
        tiles.append((label, c))

    if not tiles:
        return
    cw = max(t[1].width for t in tiles) + 20
    ch = sum(t[1].height + 34 for t in tiles) + 20
    sheet = Image.new("RGB", (cw, ch), (24, 26, 33))
    d = ImageDraw.Draw(sheet)
    y = 10
    for label, c in tiles:
        d.text((10, y), label, fill=(215, 140, 224))
        sheet.paste(c, (10, y + 18))
        d.rectangle([10, y + 18, 10 + c.width, y + 18 + c.height],
                    outline=(60, 64, 76))
        y += c.height + 34
    path = os.path.join(OUT, "crops.png")
    sheet.save(path)
    print(f"  wrote {path}")


def render_glow(src):
    ov = k.SelectionOverlay(src)
    ov.resize(W, H)
    ov.mode = "lasso"
    ov.show()
    ring = circle(700, 425, 340)
    ov.mousePressEvent(ev(PRESS, ring[0]))
    for p in ring[1:]:
        ov.mouseMoveEvent(ev(MOVE, p))
    ov.glow.t = 0.68
    pm = QPixmap(W, H)
    pm.fill(QColor("black"))
    ov.render(pm)
    ov._reset()
    path = os.path.join(OUT, "glow.png")
    pm.save(path)
    print(f"  wrote {path}")


def main():
    app = QApplication([])
    src = scene()
    print(f"rendering docs from a {W}x{H} synthetic scene")
    render("lasso", circle(700, 425, 340), NONE, src, "overlay.png")
    render("corner", [(360, 190), (900, 500)], SHIFT, src, "overlay-square.png")
    render_glow(src)
    render_crops(src)
    return 0


if __name__ == "__main__":
    sys.exit(main())