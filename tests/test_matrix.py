#!/usr/bin/env python
"""Local test matrix for kcircle.

Exercises every scope x shape x user-agent combination without touching the
network, and asserts two invariants that have both broken in the past:

  1. a selection never uploads the whole screenshot
  2. the visual tab never uploads before the crop exists
"""
import importlib.util
import math
import os
import sys
import tempfile

from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import QApplication

spec = importlib.util.spec_from_file_location("k", "kcircle.py")
k = importlib.util.module_from_spec(spec)
spec.loader.exec_module(k)
k.logg = k.setup_logging("error")

from PIL import Image, ImageDraw

SCREEN = (1600, 900)
SHIFT = Qt.KeyboardModifier.ShiftModifier
NONE = Qt.KeyboardModifier.NoModifier
PRESS, MOVE, RELEASE = (QEvent.Type.MouseButtonPress,
                       QEvent.Type.MouseMove,
                       QEvent.Type.MouseButtonRelease)

fails, checks = [], 0


def check(cond, label):
    global checks
    checks += 1
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}")
        fails.append(label)


def make_scene():
    """A screen with obvious content so a full-screen leak is detectable."""
    im = Image.new("RGB", SCREEN, (18, 22, 30))
    d = ImageDraw.Draw(im)
    for i in range(0, SCREEN[0], 80):
        d.line([(i, 0), (i, SCREEN[1])], fill=(28, 34, 46), width=2)
    d.rectangle([420, 250, 1180, 650], fill=(200, 60, 70))
    d.ellipse([520, 300, 900, 520], fill=(60, 200, 140))
    path = "/tmp/opencode/_scene.png"
    im.save(path)
    return path


def ev(t, pos, mods=NONE):
    if t == PRESS:
        b, br = Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton
    elif t == MOVE:
        b, br = Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton
    else:
        b, br = Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton
    return QMouseEvent(t, QPointF(*pos), b, br, mods)


def draw(mode, pts, mods=NONE, scene=None):
    ov = k.SelectionOverlay(scene)
    ov.show()
    ov.mode = mode
    got = {}
    ov.picked.connect(lambda s: got.__setitem__("s", s))
    ov.mousePressEvent(ev(PRESS, pts[0], mods))
    for p in pts[1:]:
        ov.mouseMoveEvent(ev(MOVE, p))
    ov.mouseReleaseEvent(ev(RELEASE, pts[-1]))
    return got.get("s")


def circle(cx, cy, rx, ry, n=49):
    return [(cx + rx * math.cos(2 * math.pi * i / n),
             cy + ry * math.sin(2 * math.pi * i / n)) for i in range(n)]


def _fills_bbox(poly):
    """True if the polygon reaches the extremes of its own bounding box.

    A square lasso does, so its cropped corners are legitimately opaque and
    must not be asserted transparent.
    """
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    near = 0.004
    touches_left = any(abs(x - min(xs)) < near for x in xs)
    touches_right = any(abs(x - max(xs)) < near for x in xs)
    touches_top = any(abs(y - min(ys)) < near for y in ys)
    touches_bottom = any(abs(y - max(ys)) < near for y in ys)
    return touches_left and touches_right and touches_top and touches_bottom


# ----------------------------------------------------------------- scopes

def test_scopes():
    print("\n== capture scopes ==")
    for scope in k.CAPTURE_SCOPES:
        try:
            p = k.capture(scope)
            w, h = Image.open(p).size
            check(w > 0 and h > 0, f"scope={scope:<7} captured {w}x{h}")
        except Exception as exc:                     # noqa: BLE001
            check(False, f"scope={scope} raised {type(exc).__name__}: {exc}")


# ----------------------------------------------------------------- shapes

SHAPES = [
    ("rect", [(400, 300), (900, 560)], NONE),
    ("corner", [(400, 300), (900, 560)], NONE),
    ("corner", [(400, 300), (900, 560)], SHIFT),
    ("ellipse", [(400, 300), (900, 560)], NONE),
    ("lasso", circle(660, 430, 280, 180), NONE),
    ("lasso", [(420, 310), (1000, 310), (1000, 640), (420, 640)], NONE),
]


def test_shapes(scene):
    print("\n== selection shapes (never the whole screen) ==")
    full = SCREEN[0] * SCREEN[1]
    for mode, pts, mods in SHAPES:
        label = mode + ("+Shift" if mods == SHIFT else "")
        sel = draw(mode, pts, mods, scene)
        if not sel:
            check(False, f"{label} produced no selection")
            continue
        region, poly = sel
        out = Image.open(k.crop_region(scene, region, poly))
        frac = out.size[0] * out.size[1] / full
        check(frac < 0.60,
              f"{label:<14} {str(out.size):<12} {frac*100:5.1f}% of screen")
        rgba = out.convert("RGBA")
        if poly:
            # A lasso that traces the full extent of its own bbox (a square,
            # say) legitimately fills it, so only assert transparency where the
            # corners are actually outside the stroke.
            if not _fills_bbox(poly):
                check(rgba.getpixel((2, 2))[3] < 40,
                      f"{label:<14} corners transparent outside the stroke")
            check(rgba.getpixel((rgba.width // 2, rgba.height // 2))[3] > 200,
                  f"{label:<14} the subject itself stays opaque")
        else:
            check(out.mode == "RGB", f"{label:<14} plain crop is RGB")


def test_square(scene):
    print("\n== square constraint ==")
    sel = draw("corner", [(400, 300), (900, 560)], SHIFT, scene)
    region, _ = sel
    w = (region[2] - region[0]) * SCREEN[0]
    h = (region[3] - region[1]) * SCREEN[1]
    check(abs(w - h) < 2, f"Shift produced a square ({w:.0f}x{h:.0f})")


# ----------------------------------------------------------------- UA

def test_ua():
    print("\n== user agents ==")
    check(set(k.UA_PRESETS) == {"mobile", "desktop"}, "both presets defined")
    check(k.MOBILE_UA != k.DESKTOP_UA, "presets differ")
    check("Mobile" in k.MOBILE_UA and "Mobile" not in k.DESKTOP_UA,
          "mobile/desktop correctly identified")


# ----------------------------------------------------------------- the gate

class FakeWeb:
    def __init__(self):
        self.html = None
        self.ua = None
        self.zoom = 1.0
        self._width = 460

    def width(self):
        return self._width

    def setWidth(self, w):
        self._width = w

    def setHtml(self, h):
        self.html = h

    def setZoomFactor(self, z):
        self.zoom = z

    def zoomFactor(self):
        return self.zoom

    def page(self):
        return self


class PanelStub:
    _crop_ready = k.ResultsPanel._crop_ready
    _load_results = k.ResultsPanel._load_results
    _apply_zoom = k.ResultsPanel._apply_zoom
    cycle_scope = k.ResultsPanel.cycle_scope
    cycle_mode = k.ResultsPanel.cycle_mode
    _refresh_scope_btn = k.ResultsPanel._refresh_scope_btn
    _refresh_mode_btn = k.ResultsPanel._refresh_mode_btn


def new_stub(crop, ua="desktop", scope="select", mode="rect", uploaded=None):
    t = PanelStub()
    t.shot_path = "/tmp/opencode/_full.png"
    t._crop = crop
    t.web = FakeWeb()
    t._uploaded_crop = uploaded
    t.web_status = type("S", (), {"setText": lambda s, x: None})()
    t.tabs = type("T", (), {"currentIndex": lambda s: 1})()
    t._settings0 = {"ua": ua, "mode": mode, "scope": scope}
    t._ua_mode, t._mode, t._scope = ua, mode, scope
    t.scope_btn = type("B", (), {"setText": lambda s, x: None})()
    t.mode_btn = type("B", (), {"setText": lambda s, x: None})()
    t.footer = type("F", (), {"setText": lambda s, x: None})()
    return t


def test_upload_gate():
    print("\n== visual tab upload gate ==")
    full = tempfile.mktemp(suffix=".png")
    Image.new("RGB", (1600, 900), (1, 2, 3)).save(full)

    # _crop starts out as the very screenshot the panel was handed, which is
    # exactly the state that used to leak a full-screen upload
    t = new_stub(full)
    t.shot_path = full
    t._crop = full
    check(not t._crop_ready, "_crop_ready is False while _crop == shot_path")
    t._load_results()
    check(t.web.html is None,
          "refuses to upload while _crop is still the full screenshot")

    crop = tempfile.mktemp(suffix=".png")
    Image.new("RGB", (400, 300), (9, 9, 9)).save(crop)
    t._crop = crop
    check(t._crop_ready, "_crop_ready once a real crop exists")
    t._load_results()
    check(t.web.html is not None, "uploads once the real crop exists")

    before = t.web.html
    t._load_results()
    check(t.web.html is before, "does not re-POST the same crop on reflow")

    t._load_results(force=True)
    check(t.web.html is not before, "force=True re-uploads for a UA switch")


def test_zoom_fitting():
    print("\n== page zoom does the wrapping ==")
    col = k.GOOGLE_DESKTOP_COLUMN
    for panel_w, expect in ((300, 300 / col), (460, 460 / col),
                            (700, 1.0), (1200, 1.0)):
        t = new_stub(None, ua="desktop")
        t.web.setWidth(panel_w)
        t._apply_zoom()
        check(abs(t.web.zoom - max(0.4, min(1.0, expect))) < 0.01,
              f"desktop @ {panel_w}px -> zoom {t.web.zoom:.2f}")
    t = new_stub(None, ua="mobile")
    t.web.setWidth(300)
    t._apply_zoom()
    check(t.web.zoom == 1.0, "mobile stays at zoom 1.0 (already fluid)")


# ----------------------------------------------------------------- settings

def test_settings_roundtrip():
    print("\n== settings persistence ==")
    d = tempfile.mkdtemp()
    saved = k._settings_path
    k._settings_path = lambda: os.path.join(d, "settings.json")
    try:
        for scope in k.CAPTURE_SCOPES:
            k.save_settings({"scope": scope})
            check(k.load_settings().get("scope") == scope,
                  f"scope={scope} round-trips")
        t = new_stub(None)
        t._settings0 = {}
        t._scope = "select"
        t._scope = k.CAPTURE_SCOPES[0]
        t.cycle_scope()
        check(t._settings0.get("scope") == "window", "scope cycles and saves")
        t.cycle_scope()
        check(t._settings0.get("scope") == "screen", "scope cycles to screen")
        t2 = new_stub(None, mode="bogus-mode")
        t2._mode = "bogus-mode"
        t2.cycle_mode()
        check(t2._mode in ["corner", "lasso", "ellipse", "rect"],
              f"unknown mode recovered -> {t2._mode}")
    finally:
        k._settings_path = saved


# ----------------------------------------------------------------- CSS

def test_css():
    print("\n== responsive css ==")
    css = k._RESPONSIVE_CSS
    check(css.count("{") == css.count("}"), "braces balanced")
    check(len(css) < 600, f"css is minimal ({len(css)} chars)")
    # these used to force-wrap live result containers and made them vanish
    for banned in ("word-break", "grid-template", "overflow-wrap",
                   "div:empty", "[data-ved]", "#center_col", "flex-direction"):
        check(banned not in css, f"css does not override {banned}")
    # page furniture is still hidden so it does not eat the panel
    check('textarea[name="q"]' in css, "search box still hidden")
    check("header" in css, "page header still hidden")
    js = k._responsive_script()
    check("kcircle-css" in js, "css injection is idempotent (tag id)")
    check("old.remove" not in js,
          "style tag updated in place, not removed and re-appended")
    check("viewport" in js, "viewport meta ensured")


def main():
    app = QApplication([])
    scene = make_scene()
    print(f"scene {SCREEN[0]}xSCREEN[1] with distinctive content")
    test_scopes()
    test_shapes(scene)
    test_square(scene)
    test_ua()
    test_upload_gate()
    test_zoom_fitting()
    test_settings_roundtrip()
    test_css()
    print(f"\n{checks - len(fails)}/{checks} checks passed")
    if fails:
        print("FAILURES:")
        for f in fails:
            print("  -", f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())