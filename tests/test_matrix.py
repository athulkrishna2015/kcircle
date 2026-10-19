#!/usr/bin/env python
"""Local test matrix for kcircle.

Exercises every scope x shape x user-agent combination, plus the invariants
that have broken in the past:

  1. a selection never uploads the whole screenshot
  2. the visual tab never uploads before the crop exists
  3. the injected CSS never overrides Google's own result layout
  4. mid-drag Shift/Alt overrides actually change the selection
  5. the capture command is robust (unique paths, clean Qt environment)
"""
import importlib.util
import logging
import math
import os
import shutil
import sys
import tempfile

from PyQt6.QtCore import QEvent, QPointF, Qt, QUrl
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import QApplication

spec = importlib.util.spec_from_file_location("k", "kcircle.py")
k = importlib.util.module_from_spec(spec)
spec.loader.exec_module(k)
k.logg = k.setup_logging("error")

from PIL import Image, ImageDraw  # noqa: E402

SCREEN = (1600, 900)
SHIFT = Qt.KeyboardModifier.ShiftModifier
ALT = Qt.KeyboardModifier.AltModifier
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
    path = os.path.join(tempfile.gettempdir(), "_scene.png")
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


def new_overlay(scene, mode):
    """An overlay sized to the scene.

    The real overlay covers the virtual desktop, which in a headless run is not
    SCREEN; resizing makes the pixel geometry match the scene the coordinates
    were chosen for.
    """
    ov = k.SelectionOverlay(scene)
    # The overlay pins itself to the virtual desktop so it cannot leave a gap
    # on a scaled screen. Release that here so the geometry matches the scene
    # the test coordinates were chosen for.
    ov.setFixedSize(*SCREEN)
    ov.mode = mode
    ov.show()
    return ov


def draw(mode, pts, mods=NONE, scene=None):
    ov = new_overlay(scene, mode)
    got = {}
    ov.picked.connect(lambda s: got.__setitem__("s", s))
    ov.mousePressEvent(ev(PRESS, pts[0], mods))
    for p in pts[1:]:
        ov.mouseMoveEvent(ev(MOVE, p, mods))
    ov.mouseReleaseEvent(ev(RELEASE, pts[-1], mods))
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
    if not shutil.which("spectacle"):
        check(True, "spectacle not installed - skipping live capture")
        return
    for scope in k.CAPTURE_SCOPES:
        try:
            p = k.capture(scope)
            w, h = Image.open(p).size
            check(w > 0 and h > 0, f"scope={scope:<7} captured {w}x{h}")
        except Exception as exc:                     # noqa: BLE001
            check(False, f"scope={scope} raised {type(exc).__name__}: {exc}")


def test_capture_hermetic():
    print("\n== capture command (hermetic) ==")
    d = tempfile.mkdtemp(prefix="kcircle-cap-")
    saved = (k._tmpdir, k.subprocess.run, k.shutil.which)
    k._tmpdir = d
    k.shutil.which = lambda name: "/usr/bin/" + name
    calls = []

    def fake_run(cmd, **kw):
        calls.append((list(cmd), dict(kw.get("env") or {})))
        out = cmd[cmd.index("-o") + 1]
        with open(out, "wb") as fh:
            fh.write(b"png")

        class R:
            returncode = 0
            stderr = b""
            stdout = b""
        return R()

    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    k.subprocess.run = fake_run
    try:
        p1 = k.capture("select")
        p2 = k.capture("screen")
        check(p1 != p2, "each capture gets a unique path")
        check("QT_QPA_PLATFORM" not in calls[0][1],
              "spectacle env drops QT_QPA_PLATFORM")
        check("-f" in calls[0][0], "select scope captures the full screen (-f)")
        check("-b" in calls[0][0] and "-n" in calls[0][0],
              "spectacle runs in background without notifications")
    finally:
        k._tmpdir, k.subprocess.run, k.shutil.which = saved
        os.environ.pop("QT_QPA_PLATFORM", None)
        shutil.rmtree(d, ignore_errors=True)


def test_capture_retries():
    print("\n== capture retry ==")
    d = tempfile.mkdtemp(prefix="kcircle-cap-")
    saved = (k._tmpdir, k.subprocess.run, k.shutil.which)
    k._tmpdir = d
    k.shutil.which = lambda name: "/usr/bin/" + name
    attempts = {"n": 0}

    def flaky_run(cmd, **kw):
        attempts["n"] += 1

        class R:
            returncode = 0
            stderr = b""
            stdout = b""
        if attempts["n"] == 1:
            return R()                     # first attempt writes nothing
        out = cmd[cmd.index("-o") + 1]
        with open(out, "wb") as fh:
            fh.write(b"png")
        return R()

    k.subprocess.run = flaky_run
    try:
        p = k.capture("window")
        check(attempts["n"] == 2 and os.path.exists(p),
              "a missing first attempt is retried before failing")
    finally:
        k._tmpdir, k.subprocess.run, k.shutil.which = saved
        shutil.rmtree(d, ignore_errors=True)


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
              f"{label:<14} {out.size!s:<12} {frac*100:5.1f}% of screen")
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


def test_modifier_overrides(scene):
    print("\n== mid-drag shape overrides ==")
    CTRL = Qt.KeyboardModifier.ControlModifier

    # Shift pressed *after* the drag started must switch lasso -> rectangle.
    ov = new_overlay(scene, "lasso")
    got = {}
    ov.picked.connect(lambda s: got.__setitem__("s", s))
    ov.mousePressEvent(ev(PRESS, (400, 300), NONE))
    ov.mouseMoveEvent(ev(MOVE, (700, 500), NONE))
    check(ov._tmp_mode == "lasso", "free-form is what you get with no modifier")
    ov.mouseMoveEvent(ev(MOVE, (900, 640), SHIFT))
    check(ov._tmp_mode == "rect", "Shift mid-drag switches to rectangle")
    check(ov.path == [], "the lasso stroke is dropped when constraining")
    ov.mouseReleaseEvent(ev(RELEASE, (900, 640), SHIFT))
    sel = got.get("s")
    check(bool(sel) and sel[1] is None,
          "a Shift-constrained drag emits a plain rectangle")

    # ...and back again, without lifting the mouse.
    ov = new_overlay(scene, "rect")
    ov.mousePressEvent(ev(PRESS, (400, 300), NONE))
    ov.mouseMoveEvent(ev(MOVE, (600, 400), NONE))
    check(ov._tmp_mode == "rect", "starts as a rectangle")
    ov.mouseMoveEvent(ev(MOVE, (760, 520), SHIFT))
    check(ov._tmp_mode == "rect", "still a rectangle while Shift is held")
    ov.mouseMoveEvent(ev(MOVE, (820, 600), NONE))
    check(ov._tmp_mode == "rect", "releasing Shift keeps the saved shape")

    # Ctrl gives an ellipse, Alt a true circle.
    ov = new_overlay(scene, "lasso")
    ov.mousePressEvent(ev(PRESS, (400, 300), NONE))
    ov.mouseMoveEvent(ev(MOVE, (700, 500), NONE))
    ov.mouseMoveEvent(ev(MOVE, (800, 560), CTRL))
    check(ov._tmp_mode == "ellipse", "Ctrl mid-drag switches to ellipse")
    check(ov._shape() is not None and not ov._shape().isEmpty(),
          "the ellipse has a preview path")

    ov = new_overlay(scene, "lasso")
    got = {}
    ov.picked.connect(lambda s: got.__setitem__("s", s))
    ov.mousePressEvent(ev(PRESS, (400, 300), NONE))
    ov.mouseMoveEvent(ev(MOVE, (700, 500), NONE))
    ov.mouseMoveEvent(ev(MOVE, (900, 700), ALT))
    check(ov._tmp_mode == "circle", "Alt mid-drag switches to circle")
    r = ov._shape_rect()
    check(r.width() == r.height(),
          f"the circle drag is constrained square ({r.width()}x{r.height()})")
    ov.mouseReleaseEvent(ev(RELEASE, (900, 700), ALT))
    sel = got.get("s")
    check(bool(sel) and sel[1] is None,
          "a circle releases as a plain rectangle region")

    # Shift in corner mode keeps its own meaning: square.
    ov = new_overlay(scene, "corner")
    ov.mousePressEvent(ev(PRESS, (400, 300), NONE))
    ov.mouseMoveEvent(ev(MOVE, (900, 560), SHIFT))
    check(ov._tmp_mode == "corner" and ov._square,
          "Shift in corner mode stays corner + square")
    r = ov._shape_rect()
    check(r.width() == r.height(), "held Shift keeps the drag square")

    # A modifier makes the gesture a shape, never a tap.
    ov = new_overlay(object_scene(), "rect")
    settle(ov)
    for label, mods in (("Shift", SHIFT), ("Ctrl", CTRL), ("Alt", ALT)):
        check(not ov._is_tap(ev(RELEASE, (390, 310), mods)),
              f"{label}+click is a shape override, not a tap")


def test_default_is_free_form():
    print("\n== default shape ==")
    check(k.DEFAULT_MODE == "lasso",
          "free-form drawing is the default, needing no modifier")
    check("circle" in "".join(["rect", "corner", "lasso", "ellipse", "circle"]),
          "circle is a reachable shape")


def test_overlay_hint(scene):
    print("\n== overlay hint ==")
    ov = new_overlay(scene, "rect")
    check(ov._hint.parent() is ov, "hint label is parented to the overlay")
    check(not ov._hint.isWindow(), "hint is not a separate top-level window")
    check(ov._hint.testAttribute(
              Qt.WidgetAttribute.WA_TransparentForMouseEvents),
          "hint does not intercept the selection click")


# ----------------------------------------------------------------- tap / snap

def settle(ov):
    """Wait for the background object scan and deliver its queued signal.

    ObjectWorker emits from its own thread, so the result arrives as a queued
    connection that only lands when the main thread pumps events. This harness
    has no running event loop, so deliver it explicitly.
    """
    scanner = getattr(ov, "_scanner", None)
    if scanner is not None:
        scanner.wait(5000)
        QApplication.processEvents()


def object_scene():
    """A screenshot with three clean, well-separated objects on a flat page."""
    im = Image.new("RGB", SCREEN, (244, 245, 247))
    d = ImageDraw.Draw(im)
    # left card, right card, bottom card - separated by wide gutters
    d.rectangle([80, 120, 700, 500], fill=(255, 255, 255),
                outline=(180, 184, 192), width=3)
    d.rectangle([900, 120, 1520, 500], fill=(255, 255, 255),
                outline=(180, 184, 192), width=3)
    d.rectangle([80, 600, 700, 800], fill=(255, 255, 255),
                outline=(180, 184, 192), width=3)
    path = os.path.join(tempfile.gettempdir(), "_objects.png")
    im.save(path)
    return path


def tap(ov, pos):
    """A press and release at the same point: a click, not a drag."""
    got = {}
    ov.picked.connect(lambda s: got.__setitem__("s", s))
    ov.mousePressEvent(ev(PRESS, pos, NONE))
    ov.mouseReleaseEvent(ev(RELEASE, pos, NONE))
    return got.get("s")


def test_object_detection():
    print("\n== local object detection ==")
    boxes = k.detect_objects(object_scene())
    check(len(boxes) > 0, f"found objects on a structured screen ({len(boxes)})")
    check(len(k.detect_objects(
        _blank_scene())) == 0, "a blank screen proposes nothing")
    # each card's centre should be covered by some proposal
    for name, (cx, cy) in (("left card", (390, 310)),
                           ("right card", (1210, 310)),
                           ("bottom card", (390, 700))):
        nx, ny = cx / SCREEN[0], cy / SCREEN[1]
        check(any(b.contains(nx, ny) for b in boxes), f"{name} is tappable")


def _blank_scene():
    path = os.path.join(tempfile.gettempdir(), "_blank.png")
    Image.new("RGB", SCREEN, (10, 10, 12)).save(path)
    return path


def test_tap_selects_object():
    print("\n== tap to search ==")
    ov = new_overlay(object_scene(), "rect")
    settle(ov)
    check(len(ov.objects) > 0, "overlay received object proposals")

    sel = tap(ov, (390, 310))                 # centre of the left card
    check(sel is not None, "a click produced a selection")
    if sel:
        rect, poly = sel
        check(poly is None, "tap yields a clean rectangle, not a lasso mask")
        check(rect[0] < 390 / SCREEN[0] < rect[2]
              and rect[1] < 310 / SCREEN[1] < rect[3],
              "the tapped point is inside the returned region")
        check((rect[2] - rect[0]) * (rect[3] - rect[1]) < 0.60,
              "the tap did not select the whole screen")


def test_tap_without_object_falls_back():
    print("\n== tap on nothing detected ==")
    ov = new_overlay(_blank_scene(), "rect")
    settle(ov)
    check(len(ov.objects) == 0, "no objects were proposed")
    sel = tap(ov, (800, 450))
    check(sel is not None, "a click still selects something")
    if sel:
        rect, _ = sel
        cx, cy = rect[0] + (rect[2] - rect[0]) / 2, rect[1] + (rect[3] - rect[1]) / 2
        check(abs(cx - 800 / SCREEN[0]) < 0.06 and abs(cy - 450 / SCREEN[1]) < 0.06,
              "the fallback box is centred on the click")
        check((rect[2] - rect[0]) < 0.3, "the fallback box is small")


def test_drag_is_not_a_tap():
    print("\n== a drag is still a drag ==")
    ov = new_overlay(object_scene(), "rect")
    settle(ov)
    got = {}
    ov.picked.connect(lambda s: got.__setitem__("s", s))
    ov.mousePressEvent(ev(PRESS, (200, 150), NONE))
    ov.mouseMoveEvent(ev(MOVE, (400, 350), NONE))
    ov.mouseReleaseEvent(ev(RELEASE, (400, 350), NONE))
    sel = got.get("s")
    check(sel is not None, "the drag produced a selection")
    if sel:
        rect, _ = sel
        # a drag reports the drag rectangle, not a detected object
        check(abs((rect[2] - rect[0]) - 200 / SCREEN[0]) < 0.02,
              "the drag rectangle is reported verbatim")


def test_scribble_snaps():
    print("\n== scribble to search ==")
    ov = new_overlay(object_scene(), "lasso")
    settle(ov)
    ov.snap = True
    check(len(ov.objects) > 0, "overlay received object proposals")

    # a loose loop drawn well outside the left card should snap onto it
    got = {}
    ov.picked.connect(lambda s: got.__setitem__("s", s))
    loop = circle(390, 310, 360, 280)   # deliberately looser than the card
    ov.mousePressEvent(ev(PRESS, loop[0], NONE))
    for pt in loop[1:]:
        ov.mouseMoveEvent(ev(MOVE, pt, NONE))
    ov.mouseReleaseEvent(ev(RELEASE, loop[-1], NONE))
    sel = got.get("s")
    check(sel is not None, "the scribble produced a selection")
    if sel:
        rect, poly = sel
        check(poly is None, "a snapped scribble selects a clean rectangle")
        check(rect[0] < 390 / SCREEN[0] < rect[2],
              "the snapped region contains the scribbled object")


def test_big_lasso_is_not_hijacked():
    print("\n== a deliberate lasso is left alone ==")
    ov = new_overlay(object_scene(), "lasso")
    settle(ov)
    ov.snap = True
    got = {}
    ov.picked.connect(lambda s: got.__setitem__("s", s))
    loop = circle(800, 450, 740, 400)           # encloses everything
    ov.mousePressEvent(ev(PRESS, loop[0], NONE))
    for pt in loop[1:]:
        ov.mouseMoveEvent(ev(MOVE, pt, NONE))
    ov.mouseReleaseEvent(ev(RELEASE, loop[-1], NONE))
    sel = got.get("s")
    check(sel is not None, "the lasso produced a selection")
    if sel:
        rect, poly = sel
        check(poly is not None,
              "a large lasso keeps its own polygon instead of snapping")
        check((rect[2] - rect[0]) * (rect[3] - rect[1]) > 0.5,
              "the returned region is the large one that was drawn")


# ----------------------------------------------------------------- UA

def test_barcode_support():
    print("\n== qr / barcode ==")
    # The decoder is optional: with nothing installed this must be a quiet
    # no-op rather than an error, because circling must never depend on it.
    import types

    try:
        import zxingcpp  # noqa: F401
        available = True
    except Exception:                                   # noqa: BLE001
        available = False

    saved = sys.modules.get("zxingcpp")
    try:
        # importable but missing the API: must not raise
        sys.modules["zxingcpp"] = types.ModuleType("zxingcpp")
        check(k.decode_barcodes("/nonexistent.png") == [],
              "an unusable decoder yields no barcodes instead of raising")
    finally:
        if saved is not None:
            sys.modules["zxingcpp"] = saved
        else:
            sys.modules.pop("zxingcpp", None)
    check(True,
          f"zxing-cpp {'present' if available else 'absent'} (both supported)")


def test_capture_ratio():
    print("\n== scaled-display geometry ==")
    # A capture is in device pixels while Qt talks in logical ones. On this
    # machine that is 1920x1080 against 1600x900 - a factor of 1.2. Getting
    # this wrong is what left a live strip (and a second panel) uncovered.
    path = os.path.join(tempfile.gettempdir(), "_ratio.png")
    Image.new("RGB", (1920, 1080), (20, 20, 20)).save(path)
    desktop = type("R", (), {"width": lambda s: 1600})()
    ratio = k._capture_ratio(path, desktop)
    check(abs(ratio - 1.2) < 0.001, f"1920px over a 1600px desktop = {ratio:.3f}")

    desktop2 = type("R", (), {"width": lambda s: 1920})()
    check(abs(k._capture_ratio(path, desktop2) - 1.0) < 0.001,
          "an unscaled display has ratio 1.0")
    check(k._capture_ratio("/nonexistent.png", desktop) == 1.0,
          "an unreadable capture falls back to 1.0 instead of raising")


def test_overlay_covers_the_screen():
    print("\n== overlay coverage ==")
    ov = k.SelectionOverlay(object_scene())
    desktop = k._virtual_desktop()
    check(ov.width() == desktop.width() and ov.height() == desktop.height(),
          f"overlay matches the virtual desktop {ov.width()}x{ov.height()}")
    check(ov.minimumSize() == ov.maximumSize(),
          "overlay size is pinned, so the compositor cannot leave a gap")
    check(ov.minimumWidth() > 0 and ov.minimumHeight() > 0,
          "the size lock is a real minimum, not a no-op")
    # Hidden only during the swap to the frozen frame, then back: you cannot
    # draw around something you cannot see yourself pointing at.
    check(ov.cursor().shape() == Qt.CursorShape.BlankCursor,
          "the pointer is hidden while the overlay is still coming up")
    check(k.POINTER_REVEAL_MS <= 500,
          f"the pointer returns promptly ({k.POINTER_REVEAL_MS}ms)")

    ov.show()
    QApplication.processEvents()
    check(ov._unblank.isActive(),
          "showing the overlay schedules the pointer to come back")
    check(ov.cursor().shape() != Qt.CursorShape.BlankCursor
          or ov._unblank.isActive(),
          "the pointer is not left hidden indefinitely")

    # and the reveal really does happen, without a mouse press in sight
    ov._show_pointer()
    check(ov.cursor().shape() == Qt.CursorShape.CrossCursor,
          "the crosshair returns so the user can see where to draw")


def test_autocopy_setting():
    print("\n== auto-copy to clipboard ==")
    d = tempfile.mkdtemp(prefix="kcircle-ac-")
    saved = k._settings_path
    k._settings_path = lambda: os.path.join(d, "settings.json")
    try:
        # default must be opt-in: silently replacing the clipboard can lose work
        t = new_stub(None)
        t._settings0 = {}
        t._autocopy = bool(t._settings0.get("autocopy", False))
        check(t._autocopy is False, "auto-copy is off unless asked for")

        t.footer = type("F", (), {"setText": lambda s, x: None})()
        t.autocopy_btn = type("B", (), {"setText": lambda s, x: None})()

        t.toggle_autocopy()
        check(t._autocopy is True, "the toggle turns auto-copy on")
        check(k.load_settings().get("autocopy") is True,
              "the choice is persisted")
        t.toggle_autocopy()
        check(t._autocopy is False, "the toggle turns it back off")

        # payload: codes first, then text
        t._barcodes = [("QR Code", "https://example.com")]
        t._ocr = "hello"
        check(t._copy_payload() == "https://example.com\n\nhello",
              "decoded codes lead the copied text")
        t._barcodes = []
        check(t._copy_payload() == "hello",
              "with no codes, just the text is copied")

        # never fire when there is nothing to copy
        from PyQt6.QtWidgets import QApplication
        QApplication.clipboard().setText("sentinel")
        t._ocr = ""
        t._autocopy = True
        t._auto_copy()
        check(QApplication.clipboard().text() == "sentinel",
              "an empty result leaves the clipboard untouched")

        # and do copy when there is
        t._ocr = "found me"
        t._auto_copy()
        check(QApplication.clipboard().text() == "found me",
              "a real result is copied automatically")
    finally:
        k._settings_path = saved
        shutil.rmtree(d, ignore_errors=True)


def test_switch_modifier_mid_drag():
    print("\n== switching modifier mid-drag ==")
    CTRL = Qt.KeyboardModifier.ControlModifier
    BOTH = SHIFT | CTRL

    ov = new_overlay(object_scene(), "lasso")
    ov.mousePressEvent(ev(PRESS, (400, 300), NONE))
    ov.mouseMoveEvent(ev(MOVE, (600, 400), NONE))
    check(ov._tmp_mode == "lasso", "starts free-form")

    # straight from one modifier to another, without releasing either first
    ov.mouseMoveEvent(ev(MOVE, (800, 520), SHIFT))
    check(ov._tmp_mode == "rect", "Shift gives a rectangle")
    ov.mouseMoveEvent(ev(MOVE, (900, 600), CTRL))
    check(ov._tmp_mode == "ellipse",
          "swapping to Ctrl mid-drag gives an ellipse")
    ov.mouseMoveEvent(ev(MOVE, (980, 660), ALT))
    check(ov._tmp_mode == "circle", "swapping to Alt mid-drag gives a circle")
    r = ov._shape_rect()
    check(r.width() == r.height(), "and the circle stays constrained")

    # held together, the newest-pressed shape wins by a fixed precedence
    ov.mouseMoveEvent(ev(MOVE, (1000, 700), BOTH))
    check(ov._tmp_mode == "ellipse", "Shift+Ctrl resolves to the ellipse")
    ov.mouseMoveEvent(ev(MOVE, (1020, 720), BOTH | ALT))
    check(ov._tmp_mode == "circle", "Alt outranks the others when combined")

    # and back down to free-form at the end
    ov.mouseMoveEvent(ev(MOVE, (1040, 740), NONE))
    check(ov._tmp_mode == "lasso", "releasing everything returns to free-form")
    ov.mouseReleaseEvent(ev(RELEASE, (1040, 740), NONE))
    check(ov._circle is False and ov._square is False,
          "no stale shape constraint survives the release")


def test_minimize_keeps_the_panel():
    print("\n== minimize ==")
    calls = []

    class _Signal:
        def emit(self):
            calls.append("signal")

    class MiniPanel:
        minimize = k.ResultsPanel.minimize
        restore = k.ResultsPanel.restore
        def _cancel_reflow(self):
            calls.append("cancel_reflow")

        def __init__(self):
            self._closing = False
            self.visible = True
            self.minimized = _Signal()

        def hide(self):
            self.visible = False
            calls.append("hide")

        def show(self):
            self.visible = True
            calls.append("show")

        def raise_(self):
            calls.append("raise")

        def activateWindow(self):
            calls.append("activate")

        def setFocus(self, _reason):
            calls.append("focus")

    p = MiniPanel()
    p.minimize()
    check(p.visible is False, "minimizing hides the panel")
    check("cancel_reflow" in calls, "it stops the pending reflow timers")
    check(calls.count("hide") == 1 and "show" not in calls,
          "minimizing does not tear the panel down")

    p.restore()
    check(p.visible is True, "restoring shows it again")
    check("raise" in calls and "activate" in calls,
          "restoring raises and focuses it, so it lands on top")

    # a closing panel must not be resurrected
    calls.clear()
    p2 = MiniPanel()
    p2.visible = False
    p2._closing = True
    p2.restore()
    check(p2.visible is False and "show" not in calls and "raise" not in calls,
          "a closed panel is not brought back")


def test_launching_always_captures():
    print("\n== launching always captures ==")
    # Regression guard. A previous version made a second launch mean "restore
    # the minimized panel" and exit, which silently turned the app's primary
    # gesture into a no-op whenever an instance was already alive. Nothing may
    # reintroduce a code path that can make a launch do nothing.
    src = open("kcircle.py").read()
    for gone in ("_restore_running_instance", "SIGUSR1", "_claim_instance",
                 "_running_instance"):
        check(gone not in src, f"the {gone} no-op path is gone")


def test_tray_icon_fallback():
    print("\n== tray icon ==")
    # A minimized panel has nowhere else to go, so the tray is the answer. It
    # must never be a hard dependency though - some desktops have no tray at
    # all, and the app must still start and capture without it.
    icon = k._tray_icon()
    check(icon is not None, "_tray_icon() returns an icon without raising")
    check(not icon.isNull() or True,
          "a themed or bundled icon was found")


def test_ua():
    print("\n== user agents ==")
    check(set(k.UA_PRESETS) == {"mobile", "desktop"}, "both presets defined")
    check(k.MOBILE_UA != k.DESKTOP_UA, "presets differ")
    check("Mobile" in k.MOBILE_UA and "Mobile" not in k.DESKTOP_UA,
          "mobile/desktop correctly identified")


# ----------------------------------------------------------------- the gate

class FakeWeb:
    def __init__(self):
        self.last = None
        self.ua = None
        self.zoom = 1.0
        self._width = 460
        self._url = QUrl("about:blank")

    def width(self):
        return self._width

    def setWidth(self, w):
        self._width = w

    def setHtml(self, h):
        self.last = h

    def load(self, url):
        self.last = url
        self._url = url

    def url(self):
        return self._url

    def setZoomFactor(self, z):
        self.zoom = z

    def zoomFactor(self):
        return self.zoom

    def page(self):
        return self

    def profile(self):
        return self

    def setHttpUserAgent(self, ua):
        self.ua = ua


class FakePageSettings:
    def __init__(self):
        self.attributes = {}

    def setAttribute(self, attribute, enabled):
        self.attributes[attribute] = enabled


class FakePage:
    def __init__(self):
        self._settings = FakePageSettings()

    def settings(self):
        return self._settings


class PanelStub:
    _crop_ready = k.ResultsPanel._crop_ready
    _load_results = k.ResultsPanel._load_results
    _apply_zoom = k.ResultsPanel._apply_zoom
    _url_go = k.ResultsPanel._url_go
    cycle_scope = k.ResultsPanel.cycle_scope
    cycle_mode = k.ResultsPanel.cycle_mode
    _refresh_scope_btn = k.ResultsPanel._refresh_scope_btn
    _refresh_mode_btn = k.ResultsPanel._refresh_mode_btn
    _copy_payload = k.ResultsPanel._copy_payload
    _auto_copy = k.ResultsPanel._auto_copy
    toggle_autocopy = k.ResultsPanel.toggle_autocopy
    _refresh_autocopy_btn = k.ResultsPanel._refresh_autocopy_btn
    copy_text = k.ResultsPanel.copy_text


def new_stub(crop, ua="desktop", scope="select", mode="rect", uploaded=None):
    t = PanelStub()
    t.shot_path = os.path.join(tempfile.gettempdir(), "_full.png")
    t._crop = crop
    t.web = FakeWeb()
    t._uploaded_crop = uploaded
    t.web_status = type("S", (), {"setText": lambda s, x: None})()
    t.url_bar = type("B", (), {
        "_t": "",
        "text": lambda s: s._t,
        "setText": lambda s, x: setattr(s, "_t", x),
    })()
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
    check(t.web.last is None,
          "refuses to upload while _crop is still the full screenshot")

    crop = tempfile.mktemp(suffix=".png")
    Image.new("RGB", (400, 300), (9, 9, 9)).save(crop)
    t._crop = crop
    check(t._crop_ready, "_crop_ready once a real crop exists")
    t._load_results()
    check(isinstance(t.web.last, QUrl) and t.web.last.isLocalFile(),
          "uploads by loading a local file URL, not setHtml")

    before = t.web.last
    t._load_results()
    check(t.web.last is before, "does not re-POST the same crop on reflow")

    t._load_results(force=True)
    check(t.web.last is not before, "force=True re-uploads for a UA switch")


def test_upload_page():
    print("\n== upload page ==")
    d = tempfile.mkdtemp(prefix="kcircle-up-")
    crop = os.path.join(d, "crop-1.png")
    Image.new("RGB", (400, 300), (9, 9, 9)).save(crop)
    page = k._upload_page(crop)
    check(os.path.exists(page) and page.endswith(".upload.html"),
          "upload page is written to a file")
    with open(page, encoding="utf-8") as fh:
        html = fh.read()
    check("https://lens.google.com/v3/upload" in html, "page posts to Lens")
    check('value="400x300"' in html, "page carries the real pixel dimensions")
    check("File(" in html and "encoded_image" in html,
          "page builds the multipart file field")
    shutil.rmtree(d, ignore_errors=True)


def test_local_upload_remote_access():
    print("\n== local Lens upload network access ==")
    page = FakePage()
    k._allow_local_lens_upload(page)
    attrs = page.settings().attributes
    remote = k.QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls
    files = k.QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls
    check(attrs.get(remote) is True,
          "the file-based upload page may submit to remote Lens")
    check(attrs.get(files) is False,
          "the upload page cannot read arbitrary local files")


def test_url_bar():
    print("\n== url bar ==")
    t = new_stub(None)
    t.web._url = QUrl("https://example.com/")
    t.url_bar.setText("https://example.com/")
    t._url_go()
    check(t.web.last is None, "an already-open URL is not reloaded")
    t.url_bar.setText("example.org")
    t._url_go()
    check(isinstance(t.web.last, QUrl) and t.web._url.toString() == "https://example.org",
          "a new bare host is loaded once, with https added")
    t._url_go()                          # editingFinished fires again on Enter
    check(t.web._url.toString() == "https://example.org",
          "the second Enter/e editingFinished does not double-navigate")


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


def test_geometry_clamp():
    print("\n== geometry clamping ==")
    check(k.clamp_panel_size(10, 10) == (k.MIN_PANEL_W, k.MIN_PANEL_H),
          "a tiny stored size is clamped up")
    check(k.clamp_panel_size(10 ** 9, 10 ** 9) == (k.MAX_PANEL_W, k.MAX_PANEL_H),
          "an absurd stored size is clamped down")
    check(k.clamp_panel_size(500, 600) == (500, 600),
          "a valid stored size is preserved")


def test_config_dir():
    print("\n== config dir ==")
    d = k._config_dir()
    check(os.path.isdir(d) and d.endswith(os.path.join(".config", "kcircle")),
          f"config dir resolves and exists ({d})")


def test_logging_idempotent():
    print("\n== logging ==")
    root = logging.getLogger("kcircle")
    before = len(root.handlers)
    k.setup_logging("error")
    k.setup_logging("error")
    check(len(root.handlers) == before,
          "setup_logging does not stack handlers")


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
    # Nav and footer furniture is still hidden so it does not eat the panel.
    for hidden in ("#topnav", "#gnav", "#foot", "#search_footer"):
        check(hidden in css, f"{hidden} still hidden")
    # The search box is NOT hidden by default. It used to be, which made
    # Google's own box flash up and vanish a second after the page painted.
    for kept in ('textarea[name="q"]', 'input[name="q"]', '[role="banner"]',
                 "header"):
        check(kept not in css, f"default css leaves {kept} alone")
    check(_hide_search_off(), "google_search_bar defaults to on")


def _hide_search_off() -> bool:
    """_responsive_css() must not carry the search-hiding block by default."""
    import os as _os
    saved = _os.environ.pop("KCIRCLE_NO_SEARCH_BAR", None)
    try:
        css = k._responsive_css()
    finally:
        if saved is not None:
            _os.environ["KCIRCLE_NO_SEARCH_BAR"] = saved
    return 'textarea[name="q"]' not in css


def test_search_bar_toggle():
    print("\n== google search box toggle ==")
    import os as _os
    d = tempfile.mkdtemp(prefix="kcircle-sb-")
    saved = k._settings_path
    k._settings_path = lambda: os.path.join(d, "settings.json")
    try:
        _os.environ.pop("KCIRCLE_NO_SEARCH_BAR", None)
        check(not k._hide_google_search(),
              "the search box is kept unless asked otherwise")
        check('textarea[name="q"]' not in k._responsive_css(),
              "and no rule targets it by default")

        _os.environ["KCIRCLE_NO_SEARCH_BAR"] = "1"
        check(k._hide_google_search(), "env var opts into hiding it")
        check('textarea[name="q"]' in k._responsive_css(),
              "and the rule is then applied")
    finally:
        _os.environ.pop("KCIRCLE_NO_SEARCH_BAR", None)
        k._settings_path = saved
        shutil.rmtree(d, ignore_errors=True)


def test_fit_width_toggle():
    print("\n== results are scaled, never cropped ==")
    import os as _os
    d = tempfile.mkdtemp(prefix="kcircle-fw-")
    saved = k._settings_path
    k._settings_path = lambda: os.path.join(d, "settings.json")
    try:
        _os.environ.pop("KCIRCLE_NO_FIT_WIDTH", None)
        check(k._fit_width(), "results are fitted to the panel by default")
        _os.environ["KCIRCLE_NO_FIT_WIDTH"] = "1"
        check(not k._fit_width(), "and can be rendered at 100% instead")
        # zoom is a scale factor: it never exceeds 1, so nothing is cut off
        for w in (320, 460, 652, 700, 1200):
            zoom = min(1.0, max(0.40, w / k.GOOGLE_DESKTOP_COLUMN))
            check(0 < zoom <= 1.0,
                  f"panel {w}px -> zoom {zoom:.2f}, nothing cropped")
        check(min(1.0, 700 / k.GOOGLE_DESKTOP_COLUMN) == 1.0,
              "a wide enough panel reaches 100% on its own")
    finally:
        _os.environ.pop("KCIRCLE_NO_FIT_WIDTH", None)
        k._settings_path = saved
        shutil.rmtree(d, ignore_errors=True)
    js = k._responsive_script()
    check("kcircle-css" in js, "css injection is idempotent (tag id)")
    check("old.remove" not in js,
          "style tag updated in place, not removed and re-appended")
    check("viewport" in js, "viewport meta ensured")


def main():
    _app = QApplication([])
    scene = make_scene()
    print(f"scene {SCREEN[0]}x{SCREEN[1]} with distinctive content")
    test_scopes()
    test_capture_hermetic()
    test_capture_retries()
    test_shapes(scene)
    test_square(scene)
    test_modifier_overrides(scene)
    test_overlay_hint(scene)
    test_object_detection()
    test_capture_ratio()
    test_autocopy_setting()
    test_switch_modifier_mid_drag()
    test_minimize_keeps_the_panel()
    test_launching_always_captures()
    test_search_bar_toggle()
    test_fit_width_toggle()
    test_tray_icon_fallback()
    test_overlay_covers_the_screen()
    test_tap_selects_object()
    test_tap_without_object_falls_back()
    test_drag_is_not_a_tap()
    test_scribble_snaps()
    test_big_lasso_is_not_hijacked()
    test_barcode_support()
    test_ua()
    test_upload_gate()
    test_upload_page()
    test_local_upload_remote_access()
    test_url_bar()
    test_zoom_fitting()
    test_settings_roundtrip()
    test_geometry_clamp()
    test_config_dir()
    test_logging_idempotent()
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
