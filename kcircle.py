#!/usr/bin/env python3
"""
kcircle - Circle to Search for KDE Plasma (Wayland), overlay-only.

Trigger -> screen freezes -> draw a box -> results appear in a small
always-on-top panel. No browser is launched; Lens is queried over its
native Protobuf endpoint via chrome-lens-py.

  OCR text and translation render natively in the panel. "Visual matches"
  is the one thing that opens a browser, since Lens visual search is
  inherently a web product.
"""

import asyncio
import atexit
import itertools
import os
import queue
import socket
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from PyQt6.QtCore import QPointF, QRect, QRectF, Qt, QSharedMemory, QThread, QTimer, QUrl, pyqtSignal
from PyQt6.QtNetwork import QLocalServer, QLocalSocket

from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QGuiApplication,
    QImage,
    QKeySequence,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
    QRegion,
    QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QSystemTrayIcon,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

try:
    from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    HAVE_WEBENGINE = True
except Exception:                                    # noqa: BLE001
    QWebEnginePage = None
    QWebEngineSettings = None
    QWebEngineView = None
    HAVE_WEBENGINE = False


# ----------------------------------------------------------------- config

PANEL_W, PANEL_H = 460, 520
MIN_PANEL_W, MIN_PANEL_H = 320, 260
MAX_PANEL_W, MAX_PANEL_H = 4000, 4000
DARK = "#1c1b22"
PANEL_BG = "#232228"
FG = "#e6e1e5"
ACCENT = "#d78ce0"
MUTED = "#9a96a0"

_tmpdir = None
_capture_seq = itertools.count(1)
_crop_seq = itertools.count(1)
_RELAUNCH_REQUESTS = queue.SimpleQueue()
_RELAY_SOCKET = os.path.join(
    os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir(),
    f"kcircle-{os.getuid()}.sock",
)


def _start_relay_socket() -> None:
    """Accept fast relaunch requests without importing Qt in the client."""
    try:
        os.unlink(_RELAY_SOCKET)
    except FileNotFoundError:
        pass
    except OSError:
        return

    def serve():
        try:
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(_RELAY_SOCKET)
            os.chmod(_RELAY_SOCKET, 0o600)
            server.listen(8)
        except OSError:
            return
        while True:
            try:
                conn, _ = server.accept()
                conn.close()
                _RELAUNCH_REQUESTS.put(True)
            except OSError:
                return

    threading.Thread(target=serve, name="kcircle-relay", daemon=True).start()


def _stop_relay_socket() -> None:
    try:
        os.unlink(_RELAY_SOCKET)
    except FileNotFoundError:
        pass
    except OSError:
        pass


atexit.register(_stop_relay_socket)


def _config_dir() -> str:
    """~/.config/kcircle, created on demand. Single source of truth so the
    probe cache, settings, geometry and log never drift apart."""
    d = os.path.expanduser("~/.config/kcircle")
    os.makedirs(d, exist_ok=True)
    return d


def _cleanup_tmpdir() -> None:
    """Remove this run's scratch directory on exit.

    Captures and crops are only ever needed for the life of the process, and
    without this every launch would leave a kcircle-* directory in /tmp forever.
    """
    if _tmpdir:
        shutil.rmtree(_tmpdir, ignore_errors=True)


atexit.register(_cleanup_tmpdir)


def _webengine_signature() -> str:
    """Identify the Qt/PyQt6 combination in use.

    The probe verdict depends on these, so a change to any of them re-probes.
    """
    try:
        from PyQt6.QtCore import PYQT_VERSION_STR, QT_VERSION_STR
        qt, pyqt = QT_VERSION_STR, PYQT_VERSION_STR
    except Exception:                                # noqa: BLE001
        qt = pyqt = "?"
    return f"py{sys.version_info.major}.{sys.version_info.minor}|qt{qt}|pyqt{pyqt}"


def _probe_cache_path() -> str:
    return os.path.join(_config_dir(), "webengine.json")


def _cached_webengine_ok():
    """Previous verdict, or None if unknown or stale."""
    import json

    try:
        with open(_probe_cache_path()) as fh:
            data = json.load(fh)
        if data.get("sig") == _webengine_signature():
            return bool(data.get("ok"))
    except Exception:                                # noqa: BLE001
        pass
    return None


def _cache_webengine(ok: bool) -> None:
    import json

    try:
        with open(_probe_cache_path(), "w") as fh:
            json.dump({"sig": _webengine_signature(), "ok": bool(ok)}, fh)
    except Exception:                                # noqa: BLE001
        pass


def webengine_is_healthy(force: bool = False) -> bool:
    """Probe WebEngine in a subprocess.

    Importing the bindings is not enough: constructing a QWebEngineView can
    hard-abort the process (seen with Arch's python-pyqt6 6.11.0 against
    qt6 6.11.2, where the bindings and the runtime are a patch apart). A
    SIGABRT cannot be caught, so the only safe check is to try it out of
    process and degrade to OCR-only if it dies.

    The verdict is cached against the Qt/PyQt6 versions. Booting QtWebEngine in
    a subprocess costs ~3s, and whether it loads is a property of the
    installed Qt rather than of this launch - so it is paid once, not every
    time. KCIRCLE_REPROBE=1 forces a fresh check.
    """
    if not HAVE_WEBENGINE:
        return False
    if not force and os.environ.get("KCIRCLE_REPROBE") != "1":
        cached = _cached_webengine_ok()
        if cached is not None:
            if logg:
                logg.debug("webengine probe: cached verdict %s", cached)
            return cached

    probe = (
        "import sys;"
        "from PyQt6.QtWidgets import QApplication;"
        "from PyQt6.QtCore import QTimer;"
        "from PyQt6.QtWebEngineWidgets import QWebEngineView;"
        "app=QApplication(sys.argv[:1]);v=QWebEngineView();v.resize(400,300);"
        "v.page().profile().setHttpUserAgent('probe');"
        "v.setHtml('<html><body>probe</body></html>');"
        "QTimer.singleShot(900, app.quit);app.exec()"
    )
    ok = False
    try:
        r = subprocess.run([sys.executable, "-c", probe],
                           capture_output=True, timeout=60, check=False)
        ok = r.returncode == 0
        if not ok and logg:
            err = (r.stderr or b"").decode(errors="replace").strip().splitlines()
            logg.debug("webengine probe failed rc=%s: %s", r.returncode,
                       err[-1] if err else "(no stderr)")
    except Exception:                                # noqa: BLE001
        ok = False
    _cache_webengine(ok)
    return ok

def _log_path():
    return os.path.join(_config_dir(), "kcircle.log")


def setup_logging(level="info"):
    """File + stderr logging.

    KCIRCLE_LOG=debug turns on verbose tracing; KCIRCLE_LOG=<path> overrides
    the file. The log is trimmed to 512 KB so it cannot grow without bound.
    """
    import logging
    from logging.handlers import RotatingFileHandler

    lvl = getattr(logging, os.environ.get("KCIRCLE_LOG", level).upper(),
                  logging.INFO)
    root = logging.getLogger("kcircle")
    root.setLevel(lvl)
    # idempotent: calling setup_logging twice must not double every line
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                            "%H:%M:%S")
    path = _log_path()
    try:
        if os.path.exists(path) and os.path.getsize(path) > 512 * 1024:
            with open(path, "w"):
                pass
        fh = RotatingFileHandler(path, maxBytes=512 * 1024, backupCount=1)
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except Exception:                                # noqa: BLE001
        pass
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
    root.addHandler(sh)
    logg = logging.getLogger("kcircle")
    logg.debug("logging initialised -> %s (%s)", path, lvl)
    return logg


logg = None


# ----------------------------------------------------------------- capture

CAPTURE_SCOPES = ["select", "window", "screen"]


def clamp_panel_size(w, h) -> tuple:
    """Clamp a restored panel size into a sane range.

    A corrupt or hand-edited geometry.json could otherwise resize the panel to
    an unusable (or off-screen) size.
    """
    return (max(MIN_PANEL_W, min(int(w), MAX_PANEL_W)),
            max(MIN_PANEL_H, min(int(h), MAX_PANEL_H)))


def _virtual_desktop() -> QRect:
    """Geometry covering every screen, matching a full-desktop capture."""
    screens = QGuiApplication.screens()
    if not screens:
        return QRect(0, 0, 1920, 1080)
    rect = QRect(screens[0].geometry())
    for s in screens[1:]:
        rect = rect.united(s.geometry())
    return rect


def _capture_ratio(shot_path, desktop=None) -> float:
    """Capture pixels per logical (device-independent) screen pixel.

    This is the number that has to be right, and it must not be taken from Qt.

    On a scaled display Qt reports coordinates in device-independent units (a
    1920x1080 panel at 1.2x is 1600x900 as far as Qt is concerned) while
    spectacle captures real device pixels (1920x1080). Mixing the two offsets
    everything by exactly the scale factor, which is how the overlay ends up
    not quite covering the screen and leaves a live strip - with a second copy
    of the panel - showing underneath it.

    Deriving the ratio from the two things actually in hand, the captured image
    and the virtual geometry, avoids trusting Qt's reported devicePixelRatio,
    which is not reliable under Wayland/XWayland.
    """
    desktop = desktop or _virtual_desktop()
    try:
        from PIL import Image
        with Image.open(shot_path) as im:
            width = im.width
    except Exception:                                # noqa: BLE001
        return 1.0
    if not width or not desktop.width():
        return 1.0
    return float(width) / float(desktop.width())


def _spectacle_env() -> dict:
    """Environment for the spectacle child process.

    spectacle is its own Qt application. If the caller's environment pins a Qt
    platform (QT_QPA_PLATFORM=offscreen in tests, or a nested/minimal session),
    the child inherits it and silently captures nothing while still exiting 0.
    Strip the Qt platform selection so spectacle uses the real session.
    """
    return {k: v for k, v in os.environ.items()
            if not k.startswith("QT_QPA_PLATFORM")}


def capture(scope: str = "select") -> str:
    """Grab the screen. Returns a PNG path.

    scope="select"  full screen; the user then draws the region
    scope="window"  the active window only
    scope="screen"  the whole screen, no selection step

    spectacle is used because it is KDE's own capture path and is already
    authorised in-session; the ScreenShot2 D-Bus route additionally needs a
    X-KDE-DBUS-Restricted-Interfaces .desktop entry and a compositor restart.
    """
    global _tmpdir
    if _tmpdir is None:
        _tmpdir = tempfile.mkdtemp(prefix="kcircle-")
    # A fresh path per capture: reusing one filename means a cancelled
    # re-capture silently overwrites the screenshot a restored panel still
    # points at.
    path = os.path.join(_tmpdir, f"screen-{next(_capture_seq)}.png")

    spectacle = shutil.which("spectacle")
    if not spectacle:
        raise RuntimeError("spectacle not found - cannot capture the screen")

    flag = {"select": "-f", "window": "-a", "screen": "-f"}.get(scope, "-f")
    env = _spectacle_env()
    last = "no output"
    # spectacle occasionally returns before the file is flushed, so verify and
    # try once more rather than surfacing a hard failure for a transient miss.
    for _ in range(2):
        try:
            r = subprocess.run(
                [spectacle, "-b", "-n", flag, "-o", path],
                check=False, capture_output=True, timeout=30, env=env,
            )
        except subprocess.TimeoutExpired as exc:
            last = f"spectacle timed out after {exc.timeout:g}s"
            continue
        if r.returncode != 0:
            last = (r.stderr or b"").decode(errors="replace").strip().splitlines()
            last = last[-1] if last else f"exit {r.returncode}"
        if os.path.exists(path) and os.path.getsize(path) > 0:
            if logg:
                logg.debug("captured scope=%s -> %s (%d bytes)",
                           scope, path, os.path.getsize(path))
            return path
        last = last if r.returncode != 0 else "spectacle produced no file"

    raise RuntimeError(f"capture failed for scope={scope}: {last}")


# ------------------------------------------------------------ object detection

# Object boxes are proposed locally, before the user moves the mouse, so that a
# single click lands on a thing instead of needing a careful drag. This is the
# desktop stand-in for the on-device pre-segmentation Circle to Search does
# while the screen is still freezing.
#
# Everything here is a heuristic on the frozen PNG. It has no model and no
# network: it finds visually coherent rectangles (cards, images, buttons, text
# blocks, icons) and hands them to the overlay as tap targets. When it finds
# nothing useful the overlay silently falls back to drag-only selection, so a
# bad guess can never block a selection the user is drawing by hand.

# Work at a fraction of the capture: detection only needs to place a box around
# a subject, and a 1080p pass costs far more time for no extra accuracy.
OBJ_WORK_W = 480

# An "object" has to be big enough to be worth searching and small enough not to
# be the whole desktop. Both bounds are fractions of the screen area.
OBJ_MIN_AREA = 0.0015          # 0.15% - a small icon
OBJ_MAX_AREA = 0.45            # 45% - a photo, but not the page

# Ignore regions thinner than this many pixels at work scale. UI hairlines and
# 1px dividers produce a huge number of degenerate boxes otherwise.
OBJ_MIN_SIDE = 14

# Edge threshold on the 0..255 FIND_EDGES output. Low enough to catch a glyph
# or a hairline border, high enough to ignore compression noise in flat areas.
OBJ_EDGE_T = 22

# Dilation radius in work pixels. Two is enough to weld the gaps between the
# strokes of a word or an icon into one blob.
OBJ_DILATE = 2

# A row or column counts as a gutter when its ink count is at or below this
# fraction of the region's own peak, with an absolute floor underneath.
#
# It has to be relative. One full-height divider or one full-width toolbar puts
# a little ink into every row or column of the other axis, so an absolute
# "empty" threshold never fires on a real desktop and nothing splits at all.
# Measuring against the peak calls a line quiet relative to the busiest line in
# this region, which is what a margin actually looks like.
OBJ_QUIET_RATIO = 0.03
OBJ_QUIET_FLOOR = 1

# A gutter also has to be reasonably wide, as a fraction of the region being
# cut. Without this, the small gaps between the lines of a paragraph read as row
# gutters and a block of text gets sliced into one target per line.
OBJ_MIN_GAP_RATIO = 0.025


# Recursion cap for the gutter split. Deep enough to separate a page into
# sidebar / body / cards, shallow enough to stay a few hundred operations.
OBJ_MAX_DEPTH = 7

# Hard cap on proposed regions. A pathological screen (dense collage, a
# terminal full of text) can otherwise split until the depth limit, and the
# caller only ever shows a handful of targets anyway.
OBJ_MAX_REGIONS = 64


# A leaf region has to actually contain edges. Very sparse leaves are just the
# background between elements that survived a cut.
OBJ_MIN_INK = 0.02

# Pad the detected box outward slightly. Object detectors report the visible
# extent; a search crop that clips the subject's shadow or outline reads worse
# than one that includes a little background.
OBJ_PAD = 0.004

# A click counts as a tap (rather than the start of a drag) below this many
# pixels of travel. Qt reports integer widget coords, so a few px of jitter is
# normal on a trackpad.
TAP_SLOP = 8
TAP_MS = 450

# Free-form first: circling by hand is what Circle to Search is named for and
# it needs no modifier at all. Everything more precise is one keystroke away.
DEFAULT_MODE = "lasso"

# How long the pointer stays hidden after the overlay opens. Long
# enough to cover the swap to the frozen frame, short enough that it is
# already back before the user starts aiming at something.
POINTER_REVEAL_MS = 250

# Scribble snapping. The object has to be at least this fraction inside what was
# drawn, and the drawn region has to be at least this much larger than the
# object. Both guards exist so that snapping never hijacks a deliberate lasso.
OBJ_SNAP_COVERAGE = 0.55
OBJ_SNAP_MAX_FILL = 0.92
# A stroke covering more of the screen than this is a deliberate
# selection, never a rough circle around one object.
OBJ_SNAP_MAX_DRAWN = 0.30

# A region this wide-and-flat, and no denser than this, reads as a run of text
# rather than a picture.
OBJ_TEXT_MIN_ASPECT = 2.2
OBJ_TEXT_MAX_INK = 0.75


class ObjectBox:
    """One proposed object, in normalised 0..1 screen coordinates."""

    __slots__ = ("rect", "score", "kind")

    def __init__(self, rect, score, kind):
        self.rect = rect                    # (x1, y1, x2, y2) normalised
        self.score = score                  # 0..1, higher is more confident
        self.kind = kind                    # "region" or "text"

    def contains(self, nx, ny, slack=0.0):
        x1, y1, x2, y2 = self.rect
        return (x1 - slack <= nx <= x2 + slack
                and y1 - slack <= ny <= y2 + slack)

    def area(self):
        x1, y1, x2, y2 = self.rect
        return max(0.0, x2 - x1) * max(0.0, y2 - y1)

    def iou(self, other):
        ax1, ay1, ax2, ay2 = self.rect
        bx1, by1, bx2, by2 = other
        ix1, iy1 = max(ax1, bx1), max(ay1, by1)
        ix2, iy2 = min(ax2, bx2), min(ay2, by2)
        if ix2 <= ix1 or iy2 <= iy1:
            return 0.0
        inter = (ix2 - ix1) * (iy2 - iy1)
        union = self.area() + max(0.0, bx2 - bx1) * max(0.0, by2 - by1) - inter
        return inter / union if union > 0 else 0.0

    def __repr__(self):
        return f"ObjectBox({self.kind}, {self.rect}, {self.score:.2f})"


def _ink_map(shot_path):
    """Reduce a screenshot to a small 1-bit "ink" map of visual edges.

    Everything expensive runs inside Pillow's C code; Python never touches an
    individual pixel. Three choices carry the speed:

      * convert straight to "L" - we only need luma, never colour;
      * `reduce()` rather than a general resample - integer box averaging is
        several times cheaper than resizing 2076px down to 480px;
      * dilate with ImageChops.offset+lighter instead of MaxFilter. On a binary
        mask that is the same morphology at roughly a fortieth the cost
        (7ms -> 0.2ms per pass here).
    """
    from PIL import Image, ImageChops, ImageFilter

    with Image.open(shot_path) as im:
        gray = im.convert("L")
        full_w, full_h = gray.size

    factor = max(1, full_w // OBJ_WORK_W)
    if factor > 1:
        gray = gray.reduce(factor)
    if gray.size[0] > OBJ_WORK_W * 3 // 2:    # reduce() may overshoot
        gray = gray.resize((OBJ_WORK_W,
                            max(1, round(gray.size[1] * OBJ_WORK_W
                                         / float(gray.size[0])))),
                           Image.Resampling.BOX)

    edges = gray.filter(ImageFilter.FIND_EDGES)
    edges = edges.point(lambda v: 255 if v > OBJ_EDGE_T else 0, mode="L")

    # FIND_EDGES convolves against an assumed black border, so the outermost
    # rows and columns light up along their entire length. That one artifact
    # would put ink in every row and every column, defeating both the gutter
    # search and the tight-bbox pass. Clear it before dilating, or the
    # dilation smears it straight back inwards.
    w, h = edges.size
    for box in ((0, 0, w, 1), (0, h - 1, w, h),
                (0, 0, 1, h), (w - 1, 0, w, h)):
        edges.paste(0, box)

    for _ in range(OBJ_DILATE):
        edges = ImageChops.lighter(edges, ImageChops.offset(edges, 1, 0))
        edges = ImageChops.lighter(edges, ImageChops.offset(edges, -1, 0))
        edges = ImageChops.lighter(edges, ImageChops.offset(edges, 0, 1))
        edges = ImageChops.lighter(edges, ImageChops.offset(edges, 0, -1))

    # offset() wraps around, which would smear the screen's own edge pixels onto
    # the opposite border and hide a genuine gutter there.
    w, h = edges.size
    for box in ((0, 0, w, OBJ_DILATE), (0, h - OBJ_DILATE, w, h),
                (0, 0, OBJ_DILATE, h), (w - OBJ_DILATE, 0, w, h)):
        edges.paste(0, box)

    return edges, full_w, full_h


def _projection(mask, axis):
    """Count of ink pixels per column (axis=0) or per row (axis=1).

    Uses Pillow's BOX resample to collapse one axis, so the whole projection is
    a single C-level operation rather than a Python loop over pixels. The BOX
    resample yields a 0..255 average per remaining line, so scaling back by the
    collapsed axis length turns it into an integer ink-pixel count.
    """
    from PIL import Image

    w, h = mask.size
    if axis == 0:                                     # per column
        raw = mask.resize((w, 1), Image.Resampling.BOX).tobytes()
        return [raw[i] * h // 255 for i in range(len(raw))]
    raw = mask.resize((1, h), Image.Resampling.BOX).tobytes()   # per row
    return [raw[i] * w // 255 for i in range(len(raw))]


def _split_region(prof, lo, hi, min_side):
    """The widest quiet band inside prof[lo:hi], as (start, end), or None.

    A "quiet" run is a stretch of rows/columns carrying almost no ink - a
    gutter between two cards, or the margin beside a paragraph. Splitting on
    gutters is the classic recursive projection layout analysis, and it finds
    the rectangular structure of a screen without Python ever looking at a
    single pixel.
    """
    peak = max(prof[lo:hi]) if hi > lo else 0
    quiet = max(OBJ_QUIET_FLOOR, int(peak * OBJ_QUIET_RATIO))
    min_run = max(3, int((hi - lo) * OBJ_MIN_GAP_RATIO))
    best = None
    i = lo
    while i < hi:
        if prof[i] > quiet:
            i += 1
            continue
        j = i
        while j < hi and prof[j] <= quiet:
            j += 1
        if (j - i) >= min_run and (i - lo) >= min_side and (hi - j) >= min_side:
            if best is None or (j - i) > (best[1] - best[0]):
                best = (i, j)
        i = j
    return best


def _xy_cut(mask, x0, y0, x1, y1, depth, out):
    """Recursively split on the widest ink-free gutter until regions stop.

    Projections are recomputed per region rather than reused from the whole
    screen: a column that is busy elsewhere is still a perfectly good gutter
    inside the block currently being cut.

    The axis matters. A screenshot almost always has full-width chrome (a
    toolbar, a dock), which makes every single column "busy" and no vertical
    gutter findable until that chrome has been cut away. So both axes are
    measured and the wider gutter wins, exactly as in classical XY-cut -
    cutting columns first unconditionally would never split a normal desktop.
    """
    w_region, h_region = x1 - x0, y1 - y0
    if (depth >= OBJ_MAX_DEPTH or len(out) >= OBJ_MAX_REGIONS
            or w_region < OBJ_MIN_SIDE * 2 or h_region < OBJ_MIN_SIDE * 2):
        out.append((x0, y0, x1, y1))
        return

    sub = mask.crop((x0, y0, x1, y1))
    vrun = _split_region(_projection(sub, 0), 0, w_region, OBJ_MIN_SIDE)
    hrun = _split_region(_projection(sub, 1), 0, h_region, OBJ_MIN_SIDE)

    vw = 0 if vrun is None else vrun[1] - vrun[0]
    hw = 0 if hrun is None else hrun[1] - hrun[0]
    if vw == 0 and hw == 0:
        out.append((x0, y0, x1, y1))
        return

    if vw >= hw:
        cut = (vrun[0] + vrun[1]) // 2
        _xy_cut(mask, x0, y0, x0 + cut, y1, depth + 1, out)
        _xy_cut(mask, x0 + cut, y0, x1, y1, depth + 1, out)
    else:
        cut = (hrun[0] + hrun[1]) // 2
        _xy_cut(mask, x0, y0, x1, y0 + cut, depth + 1, out)
        _xy_cut(mask, x0, y0 + cut, x1, y1, depth + 1, out)


def _classify(bw, bh, ink):
    """Label a region "text" or "object", for display and snap preference.

    Text runs are wide, short and only moderately filled: the gaps between
    glyphs keep the ink density well below a solid photo or button. This is a
    shape heuristic only - the actual reading of the words is Google's job,
    done server-side by chrome-lens-py, so nothing here has to recognise a
    language, a font or a script.
    """
    wide = bw / float(max(1, bh))
    if wide >= OBJ_TEXT_MIN_ASPECT and ink < OBJ_TEXT_MAX_INK:
        return "text"
    return "object"


def _tighten(mask, box):
    """Shrink a region to the bounding box of its own ink.

    The gutter split cuts on empty space, so a leaf routinely carries a wide
    margin: a card sitting in the middle of a column is proposed as "the whole
    column". Measuring against the drawn area rather than the cut area is both
    more accurate and stops a generous margin from pushing a perfectly good
    object past the size limit.

    One getbbox() call does the whole job in C.
    """
    x0, y0, x1, y1 = box
    inner = mask.crop(box).getbbox()
    if not inner:
        return box
    return (x0 + inner[0], y0 + inner[1], x0 + inner[2], y0 + inner[3])


def _ink_fraction(mask, box):
    """How much of box is ink, 0..1, via a histogram rather than a pixel loop."""
    x0, y0, x1, y1 = box
    if x1 <= x0 or y1 <= y0:
        return 0.0
    crop = mask.crop(box)
    hist = crop.histogram()
    ink = sum(hist[128:])
    return ink / float((x1 - x0) * (y1 - y0))


def _merge_overlapping(boxes, iou_limit=0.45):
    """Collapse near-duplicate proposals, keeping the larger/higher-scoring."""
    kept = []
    for box in boxes:
        rect, score, kind = box.rect, box.score, box.kind
        merged = False
        for other in kept:
            if ObjectBox(rect, 0, kind).iou(other.rect) >= iou_limit:
                # keep whichever covers more area; ties go to the better score
                if rect_area(rect) > rect_area(other.rect) or (
                        rect_area(rect) == rect_area(other.rect)
                        and score > other.score):
                    kept[kept.index(other)] = box
                merged = True
                break
        if not merged:
            kept.append(box)
    return kept


def rect_area(rect):
    return max(0.0, rect[2] - rect[0]) * max(0.0, rect[3] - rect[1])


def detect_objects(shot_path):
    """Propose tap targets for a frozen screenshot.

    Recursive projection (XY-cut) layout analysis on a heavily reduced edge map:
    split the screen on ink-free gutters until the regions stop dividing, then
    keep the leaves that look like real things on screen.

    The cost is bounded and small. The image is resized to 480px wide and every
    pixel-level operation runs inside Pillow's C code; Python only walks a few
    hundred projection entries. On a 1080p capture this is a handful of
    milliseconds, which is what lets it run while the overlay is still opening.

    Returns a list of ObjectBox, best first. An empty list means "nothing
    confident found", and the caller must fall back to manual selection rather
    than pretend a tap meant something.
    """
    try:
        mask, W, H = _ink_map(shot_path)
    except Exception as exc:                          # noqa: BLE001
        if logg:
            logg.debug("object detection could not read %s: %s", shot_path, exc)
        return []

    w, h = mask.size
    if w < 8 or h < 8:
        return []
    if max(_projection(mask, 0)) <= 0:
        return []                                     # blank screen

    regions = []
    _xy_cut(mask, 0, 0, w, h, 0, regions)

    proposals = []
    for (x0, y0, x1, y1) in regions:
        bw, bh = x1 - x0, y1 - y0
        if bw < OBJ_MIN_SIDE or bh < OBJ_MIN_SIDE:
            continue

        # Judge the object, not the gutter cell it arrived in.
        x0, y0, x1, y1 = _tighten(mask, (x0, y0, x1, y1))
        bw, bh = x1 - x0, y1 - y0
        if bw < OBJ_MIN_SIDE or bh < OBJ_MIN_SIDE:
            continue
        area = (bw * bh) / float(w * h)
        if area < OBJ_MIN_AREA or area > OBJ_MAX_AREA:
            continue

        ink = _ink_fraction(mask, (x0, y0, x1, y1))
        if ink < OBJ_MIN_INK:
            continue
        score = min(1.0, ink * 0.85 + 0.15)
        proposals.append(
            ObjectBox(_normalise(x0, y0, x1, y1, w, h), score, _classify(bw, bh, ink)))

    if not proposals:
        # Nothing structural. That is a legitimate answer - a gradient, a video
        # or a dense page of text has no clean object bounds - and the overlay
        # falls back to a small box around the tap point rather than guessing.
        return []

    proposals = _merge_overlapping(proposals)
    # Smaller, better-filled boxes are what a user most likely means to tap.
    proposals.sort(key=lambda b: (-b.score, rect_area(b.rect)))
    if logg:
        logg.debug("object detection: %d proposals from %dx%d in %d regions",
                   len(proposals), W, H, len(regions))
        for b in proposals[:6]:
            logg.debug("  %r", b)
    return proposals


def _normalise(x0, y0, x1, y1, w, h):
    """Work-pixel box -> padded normalised screen rect."""
    x1n, y1n = x0 / float(w), y0 / float(h)
    x2n, y2n = x1 / float(w), y1 / float(h)
    return (max(0.0, x1n - OBJ_PAD), max(0.0, y1n - OBJ_PAD),
            min(1.0, x2n + OBJ_PAD), min(1.0, y2n + OBJ_PAD))


class ObjectWorker(QThread):
    """Runs detect_objects off the GUI thread.

    The pass costs a few tens of milliseconds, most of it decoding the PNG.
    Doing that inline would delay the overlay's first paint and make the whole
    trigger feel laggy, which is exactly the latency this feature exists to
    remove. Running it here means the overlay appears immediately and the tap
    targets arrive a frame or two later - the same trick Circle to Search uses
    while the screen is still freezing.
    """

    found = pyqtSignal(list)

    def __init__(self, shot_path):
        super().__init__()
        self.shot_path = shot_path

    def run(self):
        boxes = []
        try:
            boxes = detect_objects(self.shot_path)
        except Exception:                            # noqa: BLE001
            if logg:
                logg.debug("object detection failed", exc_info=True)
        self.found.emit(boxes)


class CaptureWorker(QThread):
    """Capture off the GUI thread so the scan surface can appear immediately."""

    captured = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, scope):
        super().__init__()
        self.scope = scope

    def run(self):
        try:
            self.captured.emit(capture(self.scope))
        except Exception as exc:                    # noqa: BLE001
            self.failed.emit(str(exc))


# Threads are deliberately not parented to the overlay. A QThread owned by a
# widget dies with it, and destroying a running QThread aborts the process; the
# overlay can close long before a scan finishes. Holding them here keeps the
# Python wrapper alive until the scan is genuinely done.
_SCANNERS = set()


# ----------------------------------------------------------------- selection

class SelectionOverlay(QWidget):
    """Fullscreen, frameless. Shows the frozen screen; user drags a box."""

    picked = pyqtSignal(tuple)   # (x1, y1, x2, y2) normalised 0..1
    # Same payload as picked(), but the caller should put the text on the
    # clipboard and stop there - no panel is opened.
    copyOnly = pyqtSignal(tuple)
    cancelled = pyqtSignal()
    missed = pyqtSignal()         # a tap that matched no object

    def __init__(self, shot_path: str | None = None):
        super().__init__()
        # BypassWindowManagerHint by default. KWin keeps panels and docks on
        # their own top layer, which a plain always-on-top window cannot rise
        # above, so the real panel would show through the capture and the
        # frozen image would not line up with what is on screen. A bypass
        # surface is drawn above everything. Set KCIRCLE_NO_BYPASS_WM=1 to opt
        # out if it misbehaves on a given setup.
        flags = (Qt.WindowType.FramelessWindowHint
                 | Qt.WindowType.WindowStaysOnTopHint)
        if os.environ.get("KCIRCLE_NO_BYPASS_WM") != "1":
            flags |= Qt.WindowType.BypassWindowManagerHint
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        # keyPressEvent only fires when the widget has focus, which a frameless
        # always-on-top window does not get for free on Wayland
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        # The pointer is hidden only while the overlay is coming up, then
        # brought straight back.
        #
        # Hiding it for the whole session is the wrong trade: you cannot draw
        # around something you cannot see yourself pointing at. And on Plasma a
        # pointer sitting over the freshly-mapped overlay also drags the
        # start-up feedback icon along with it, which looks like a stray
        # artefact. So: blank during the swap to the frozen frame, crosshair
        # from the moment the user can actually see what they are aiming at.
        self.setCursor(Qt.CursorShape.BlankCursor)
        self.setMouseTracking(True)
        self._unblank = QTimer(self)
        self._unblank.setSingleShot(True)
        self._unblank.timeout.connect(self._show_pointer)

        self.shot_path = shot_path
        self.shot = QPixmap(shot_path) if shot_path else QPixmap()
        self.origin = None
        self.current = None
        self.path = []            # lasso points, in widget coords
        settings = load_settings()
        self.mode = settings.get("mode", DEFAULT_MODE)
        self._tmp_mode = None
        self._square = False
        self._circle = False

        # Tap targets and scribble snapping. Both are heuristics over the
        # frozen screenshot, and both are strictly an accelerator: every one of
        # them degrades to plain drag selection, and none of them can block a
        # selection the user is making by hand.
        self.objects = []
        self.snap = bool(settings.get("snap", True))
        self._press_t = None
        self._show_objects = bool(settings.get("hint", True))
        self._scanner = None
        if shot_path:
            self._start_object_scan(shot_path)

        # Cover the whole capture, not just the primary output. spectacle grabs the
        # entire virtual desktop, so an overlay sized to a single screen would
        # leave the rest of the capture unreachable and mis-map every
        # coordinate on a second monitor.
        desktop = _virtual_desktop()
        self.setGeometry(desktop)

        # Pinning the size is what actually makes the overlay cover the screen.
        # A window that is merely asked to be fullscreen can come back a few
        # pixels short on a scaled output, which leaves the live desktop (and
        # its panel) showing underneath - the "two taskbars" artefact. Fixing
        # min and max to the same size makes the compositor honour it.
        if os.environ.get("KCIRCLE_NO_SIZE_LOCK") != "1":
            self.setFixedSize(desktop.width(), desktop.height())

        if logg and shot_path:
            ratio = _capture_ratio(shot_path, desktop)
            logg.info("overlay %dx%d logical, capture ratio %.3f "
                      "(capture %dx%d device px)", desktop.width(),
                      desktop.height(), ratio,
                      int(desktop.width() * ratio),
                      int(desktop.height() * ratio))

        mode_label = {
            "rect": "rectangle",
            "corner": "corner to corner",
            "lasso": "free-form lasso",
            "ellipse": "ellipse",
            "circle": "circle",
        }.get(self.mode, "free-form lasso")

        # Parented to the overlay: a parentless QLabel is a top-level window and
        # would never be shown, so the hint silently never appeared before.
        hint = QLabel(
            f"{mode_label}  ·  click an object  ·  hold Shift = rectangle"
            "  ·  Ctrl = ellipse  ·  Alt = circle  ·  Super = copy text"
            "  ·  Esc to cancel",
            self,
        )
        hint.setStyleSheet("color:#ffffff; background:#000000aa; padding:6px 12px;")
        hint.setFont(QFont("", 11))
        # never let the hint eat the click that starts a selection
        hint.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        hint.adjustSize()
        hint.move(24, 24)
        hint.raise_()
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.glow = GlowTrail()
        self._glow_timer = QTimer(self)
        self._glow_timer.timeout.connect(self._glow_tick)
        self._hint = hint

    def set_shot(self, shot_path: str):
        """Install the frozen frame after the already-visible overlay opens."""
        self.shot_path = shot_path
        self.shot = QPixmap(shot_path)
        self._start_object_scan(shot_path)
        self.update()

    def _start_object_scan(self, shot_path: str):
        if os.environ.get("KCIRCLE_NO_SNAP") != "1":
            self._scanner = ObjectWorker(shot_path)
            _SCANNERS.add(self._scanner)
            self._scanner.finished.connect(
                lambda w=self._scanner: _SCANNERS.discard(w))
            self._scanner.found.connect(self._on_objects)
            self._scanner.start()

    def _show_pointer(self):
        self.setCursor(Qt.CursorShape.CrossCursor)

    def showEvent(self, e):
        """Restore the pointer as soon as the overlay is actually up.

        Keyed off showEvent rather than the first mouse press: the pointer has
        to be visible *before* anything is pressed, because deciding where to
        draw is precisely what the user is doing at that moment.
        """
        super().showEvent(e)
        self._unblank.start(POINTER_REVEAL_MS)

    def _glow_tick(self):
        self.glow.t = (self.glow.t + 0.022) % 1.0
        self.update()

    # tap targets -------------------------------------------------------
    def _on_objects(self, boxes):
        self.objects = boxes
        self.update()

    def object_at(self, nx, ny):
        """The most specific detected object containing a normalised point.

        Preferring the smallest match matters: a tap inside an icon that sits
        in a card should select the icon, not the whole card.
        """
        best, best_area = None, None
        for box in self.objects:
            if not box.contains(nx, ny):
                continue
            area = box.area()
            if best_area is None or area < best_area:
                best, best_area = box, area
        return best

    def _tap_size(self):
        """Side of the box used when a tap lands on no detected object."""
        return max(48, int(min(self.width(), self.height()) * 0.08))

    def _tap_rect(self, nx, ny):
        half = self._tap_size() / float(max(1, min(self.width(),
                                                    self.height())))
        return (max(0.0, nx - half), max(0.0, ny - half),
                min(1.0, nx + half), min(1.0, ny + half))

    def object_for_stroke(self, rect):
        """Best object to snap a loose scribble onto.

        Scribble-to-search means "I drew roughly around one thing". Three
        conditions, all required:

          * the stroke covers less than a third of the screen - a loop that
            big is a deliberate selection, not a rough circle;
          * exactly one object is mostly inside it - two candidates means the
            user drew around a group and wants the group;
          * that object is clearly smaller than the stroke, so the scribble
            was slack rather than a tracing of the object's own edges.

        If any of them fails the lasso is honoured exactly as drawn.
        """
        rx1, ry1, rx2, ry2 = rect
        drawn = max(1e-6, (rx2 - rx1) * (ry2 - ry1))
        if drawn > OBJ_SNAP_MAX_DRAWN:
            return None
        hits = []
        for box in self.objects:
            ix1, iy1 = max(box.rect[0], rx1), max(box.rect[1], ry1)
            ix2, iy2 = min(box.rect[2], rx2), min(box.rect[3], ry2)
            if ix2 <= ix1 or iy2 <= iy1:
                continue
            inter = (ix2 - ix1) * (iy2 - iy1)
            coverage = inter / max(1e-6, box.area())
            if coverage >= OBJ_SNAP_COVERAGE:
                hits.append(box)
        if len(hits) != 1:
            return None                        # nothing, or an ambiguous group
        box = hits[0]
        if box.area() > OBJ_SNAP_MAX_FILL * drawn:
            return None                        # the stroke was the point
        return box

    def resizeEvent(self, e):
        hint = getattr(self, "_hint", None)
        if hint is not None:
            hint.adjustSize()
        super().resizeEvent(e)

    # painting ---------------------------------------------------------
    def _effective_mode(self, mods):
        """Resolve the selection shape for a given modifier state.

        With nothing held you get the saved shape, which defaults to a free-form
        lasso - the gesture Circle to Search is actually named for. Any modifier
        pressed *during* the drag overrides it, so you can start scribbling and
        tighten up to an exact shape without lifting the mouse.

        Re-evaluated on every mouse event rather than fixed at press, which is
        what makes switching mid-stroke work.
        """
        if mods & Qt.KeyboardModifier.AltModifier:
            return "circle"
        if mods & Qt.KeyboardModifier.ControlModifier:
            return "ellipse"
        if mods & Qt.KeyboardModifier.ShiftModifier:
            if self.mode == "corner":
                return "corner"           # Shift still means "square" here
            return "rect"
        return self.mode

    @staticmethod
    def _is_copy_only(mods) -> bool:
        """Super held: read the selection, copy it, and stop.

        Super is the one modifier the shape keys do not use - Shift, Ctrl and
        Alt already mean rectangle, ellipse and circle - so it combines with
        any of them without changing the shape. It changes what happens on
        release, not how the stroke is drawn.
        """
        return bool(mods & Qt.KeyboardModifier.MetaModifier)

    def _shape_rect(self):
        rect = QRect(self.origin, self.current).normalized()
        if self._square:
            side = max(rect.width(), rect.height())
            # grow a square outward from the corner the drag started at
            ax, ay = self.origin.x(), self.origin.y()
            left = ax if self.current.x() >= ax else ax - side
            top = ay if self.current.y() >= ay else ay - side
            rect = QRect(left, top, side, side)
        elif self._circle:
            # a true circle: force the shorter axis up to the longer one
            side = max(rect.width(), rect.height())
            cx, cy = rect.center().x(), rect.center().y()
            rect = QRect(cx - side // 2, cy - side // 2, side, side)
        return rect

    def paintEvent(self, _):
        p = QPainter(self)
        p.drawPixmap(self.rect(), self.shot)
        p.fillRect(self.rect(), QColor(0, 0, 0, 110))

        shape = self._shape()

        if self._show_objects and self.objects and shape is None:
            # Faint outlines while nothing is being drawn: the visual cue that
            # a click will land on something, which is what makes tapping feel
            # deliberate rather than random.
            pen = QPen(QColor(255, 255, 255, 46))
            pen.setWidth(1)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            for box in self.objects:
                r = QRectF(box.rect[0] * self.width(), box.rect[1] * self.height(),
                           (box.rect[2] - box.rect[0]) * self.width(),
                           (box.rect[3] - box.rect[1]) * self.height())
                p.drawRoundedRect(r, 4, 4)

        if shape is not None:
            p.save()
            p.setClipPath(shape, Qt.ClipOperation.IntersectClip)
            p.drawPixmap(self.rect(), self.shot)
            p.restore()

        self.glow.paint(p)
        p.end()

    def _shape(self):
        """Current selection as a QPainterPath, or None if nothing drawn.

        Uses the effective (modifier-aware) mode so an Alt-held override of a
        saved lasso previews as an ellipse while the drag is in progress.
        """
        mode = self._tmp_mode or self.mode
        if mode == "lasso":
            if len(self.path) < 3:
                return None
            path = QPainterPath(QPointF(self.path[0]))
            for pt in self.path[1:]:
                path.lineTo(QPointF(pt))
            path.closeSubpath()
            return path

        if self.origin is None or self.current is None:
            return None

        rect = QRectF(self._shape_rect())
        path = QPainterPath()
        if mode in ("ellipse", "circle"):
            path.addEllipse(rect)
        else:
            path.addRect(rect)
        return path

    # input ------------------------------------------------------------
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.RightButton:
            self.cancelled.emit()
            return
        self._tmp_mode = self._effective_mode(e.modifiers())
        self._tmp_copy = self._is_copy_only(e.modifiers())
        # Constraints follow the resolved shape, not the raw modifier: in corner
        # mode Shift means "square", elsewhere it just means "rectangle".
        self._square = self._tmp_mode == "corner"
        self._circle = self._tmp_mode == "circle"
        if self._tmp_mode == "lasso":
            self.path = [e.position().toPoint()]
        self.origin = e.position().toPoint()
        self.current = self.origin
        self.glow.begin(QPointF(e.position()))
        self._glow_timer.start(16)          # ~60fps
        self._press_t = time.monotonic()
        self.update()

    def mouseMoveEvent(self, e):
        if self.origin is None:
            return
        # Re-resolve the mode on every move: the README promises Shift/Alt
        # overrides mid-drag, and _tmp_mode was previously frozen at press.
        self._tmp_mode = self._effective_mode(e.modifiers())
        # Live too: you can start scribbling and press Super a moment later.
        self._tmp_copy = self._is_copy_only(e.modifiers())
        self._square = self._tmp_mode == "corner"
        self._circle = self._tmp_mode == "circle"
        self.current = e.position().toPoint()
        self.glow.spark = QPointF(e.position())
        if self._tmp_mode == "lasso":
            if not self.path:
                self.path = [self.origin]
            self.path.append(self.current)
            self.glow.path.lineTo(QPointF(self.current))
        else:
            self.path = []
            self.glow.path = self._shape() or QPainterPath()
        self.update()

    def mouseReleaseEvent(self, e):
        if self.origin is None:
            return
        self.current = e.position().toPoint()
        mode = self._tmp_mode or self.mode
        # Super can be released just before the mouse, so fold in whatever the
        # release event itself says.
        copy_only = self._tmp_copy or self._is_copy_only(e.modifiers())
        self._tmp_mode = None
        self._tmp_copy = False
        self._glow_timer.stop()

        # A press and release that barely moved is a tap, not a drag. Handle it
        # before any shape logic, in every mode: on Android you tap whatever
        # shape the tool happens to be in and still get the object.
        if self._is_tap(e):
            self._handle_tap(copy_only)
            return

        if mode == "lasso":
            if not self.path:
                self.path = [self.origin]
            self.path.append(self.current)
            if len(self.path) < 4:
                self._reset()
                return
            # close the loop explicitly so the mask is a filled shape even
            # when the stroke never returns to where it started
            if self.path[0] != self.path[-1]:
                self.path.append(self.path[0])

            xs = [pt.x() for pt in self.path]
            ys = [pt.y() for pt in self.path]
            if max(xs) - min(xs) < 12 or max(ys) - min(ys) < 12:
                self._reset()
                return
            poly = [(pt.x() / self.width(), pt.y() / self.height())
                    for pt in self.path]
            rect = (min(xs) / self.width(), min(ys) / self.height(),
                    max(xs) / self.width(), max(ys) / self.height())
            if logg:
                logg.info("lasso: %d points, bbox %.3f,%.3f-%.3f,%.3f",
                          len(poly), *rect)

            # Scribble to search: a loose stroke drawn around one thing snaps
            # to that thing. Only when the match is unambiguous - see
            # object_for_stroke.
            if self.snap and self.objects:
                snapped = self.object_for_stroke(rect)
                if snapped is not None:
                    if logg:
                        logg.info("scribble snapped to object %s", snapped.rect)
                    self._reset()
                    self._emit((snapped.rect, None), copy_only)
                    return

            self._reset()
            self._emit((rect, poly), copy_only)
            return

        rect = self._shape_rect()
        if rect.width() < 12 or rect.height() < 12:
            self._reset()
            return
        norm = (rect.left() / self.width(), rect.top() / self.height(),
                rect.right() / self.width(), rect.bottom() / self.height())
        self._reset()
        self._emit((norm, None), copy_only)

    def _is_tap(self, e):
        mods = e.modifiers()
        if mods & (Qt.KeyboardModifier.ShiftModifier
                   | Qt.KeyboardModifier.ControlModifier
                   | Qt.KeyboardModifier.AltModifier):
            return False          # a modifier is an explicit shape override
        if self.origin is None:
            return False
        moved = (self.current - self.origin).manhattanLength()
        if moved > TAP_SLOP:
            return False
        elapsed = (time.monotonic() - self._press_t) * 1000.0
        return elapsed <= TAP_MS

    def _emit(self, payload, copy=None):
        """Deliver a finished selection, as a copy or as a search."""
        if copy if copy is not None else getattr(self, "_tmp_copy", False):
            self.copyOnly.emit(payload)
        else:
            self.picked.emit(payload)

    def _handle_tap(self, copy=False):
        """Turn a tap into a selection of whatever object it landed on."""
        nx = self.current.x() / float(max(1, self.width()))
        ny = self.current.y() / float(max(1, self.height()))
        box = self.object_at(nx, ny)
        self._reset()
        if box is not None:
            if logg:
                logg.info("tap selected object %s (score %.2f)", box.rect,
                          box.score)
            self._emit((box.rect, None), copy)
            return
        # No object under the finger. Rather than refuse the gesture, fall back
        # to a small box around the tap: on a dense page the detector has
        # nothing to offer, and a tight crop is still a useful search.
        if logg:
            logg.info("tap hit no object; using a small box at %.3f,%.3f",
                      nx, ny)
        self._emit((self._tap_rect(nx, ny), None), copy)

    def _reset(self):
        self.origin = self.current = None
        self.path = []
        self._square = False
        self._circle = False
        self._tmp_mode = None
        self._tmp_copy = False
        self._glow_timer.stop()
        self.glow.clear()
        self.update()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()
            e.accept()
            return
        super().keyPressEvent(e)

    def closeEvent(self, e):
        # The detector thread is parented to this widget, so closing while it
        # still holds a reference to the screenshot would abort the process
        # ("QThread: Destroyed while thread is still running"). Drop the boxes
        # first so a late signal cannot touch a half-torn-down widget.
        scanner = getattr(self, "_scanner", None)
        if scanner is not None:
            try:
                scanner.found.disconnect(self._on_objects)
            except (TypeError, RuntimeError):
                pass
            scanner.wait(3000)
            _SCANNERS.discard(scanner)
            self._scanner = None
        self.objects = []
        super().closeEvent(e)


# ----------------------------------------------------------------- lens work

def crop_region(shot_path: str, region: tuple, poly: list | None = None) -> str:
    """Cut the selection out of the full screenshot.

    A lasso also gets a soft alpha mask, so what Lens sees is the circled
    subject rather than the whole bounding box.

    We crop locally rather than leaning on Lens' own region parameter so the
    same request can carry a translation target, and so nothing outside the
    user's selection is ever transmitted.
    """
    global _tmpdir
    if _tmpdir is None:
        _tmpdir = tempfile.mkdtemp(prefix="kcircle-")
    out = os.path.join(_tmpdir, f"crop-{next(_crop_seq)}.png")

    try:
        from PIL import Image, ImageDraw, ImageFilter
    except Exception as exc:                         # noqa: BLE001
        raise RuntimeError("Pillow is required to crop the selection") from exc

    img = Image.open(shot_path).convert("RGBA")
    W, H = img.size

    x1, y1, x2, y2 = region
    box = (max(0, int(x1 * W)), max(0, int(y1 * H)),
           min(W, int(x2 * W)), min(H, int(y2 * H)))
    if box[2] - box[0] < 1 or box[3] - box[1] < 1:
        raise RuntimeError("selection is empty")
    cut = img.crop(box)

    L = logg.debug if logg else (lambda *a, **k: None)
    L("crop: screen=%dx%d region=%.3f,%.3f-%.3f,%.3f -> box=%s size=%dx%d "
      "(%.0f%% of screen)", W, H, x1, y1, x2, y2, box,
      box[2] - box[0], box[3] - box[1],
      100.0 * (box[2] - box[0]) * (box[3] - box[1]) / float(W * H))

    if poly and len(poly) >= 3:
        pts = [(int(nx * W) - box[0], int(ny * H) - box[1]) for nx, ny in poly]
        mask = Image.new("L", cut.size, 0)
        ImageDraw.Draw(mask).polygon(pts, fill=255)
        mask = mask.filter(ImageFilter.GaussianBlur(1.2))

        # Trim to what the lasso actually covered, so the upload is tight
        # around the subject instead of its whole bounding box.
        bbox = mask.getbbox()
        if bbox and bbox[2] - bbox[0] > 8 and bbox[3] - bbox[1] > 8:
            cut = cut.crop(bbox)
            mask = mask.crop(bbox)
            L("mask: %d points, trimmed to drawn area %s", len(pts), bbox)

        total = cut.size[0] * cut.size[1]
        # histogram()[i] is the count of pixels with value i; summing 129..255
        # counts "inside the lasso" in C instead of a Python loop over every
        # pixel of a possibly multi-megapixel crop.
        covered = sum(mask.histogram()[129:])
        L("mask: %d/%d px inside (%.0f%% of the upload)", covered, total,
          100.0 * covered / float(total or 1))
        if covered < total * 0.10:
            L("mask covers almost nothing - the lasso is tiny or degenerate")
        elif covered > total * 0.97:
            L("mask covers everything - this will read as a plain rectangle")

        # Keep real transparency by default: everything outside the stroke is
        # left see-through rather than filled with an opaque colour. This used
        # to flatten onto white, on the theory that a transparent PNG would be
        # composited badly by the uploader - a theory that was never actually
        # tested, and the real cause of the whole-screen bug turned out to be
        # the visual tab uploading before the crop existed.
        # KCIRCLE_CROP_BG=white flattens onto white if a solid background is
        # ever needed.
        bg = (os.environ.get("KCIRCLE_CROP_BG") or "transparent").lower()
        cut.putalpha(mask)
        if bg in ("white", "#fff", "solid"):
            base = Image.new("RGBA", cut.size, (255, 255, 255, 255))
            cut = Image.alpha_composite(base, cut).convert("RGB")
            L("crop: flattened onto opaque white (KCIRCLE_CROP_BG=white)")
        else:
            L("crop: transparent outside the lasso")
    elif poly is not None:
        L("mask: skipped, polygon had %d points (need 3+)", len(poly))

    if (poly is None or len(poly) < 3) and cut.mode != "RGB":
        # no mask means no transparency to preserve; ship a plain RGB crop
        cut = cut.convert("RGB")

    cut.save(out, "PNG")
    L("crop saved: %s (%dx%d %s)", out, cut.size[0], cut.size[1], cut.mode)
    return out


# QThread objects that are still running. A QThread whose Python wrapper is
# collected while its thread is alive aborts the process ("QThread: Destroyed
# while thread is still running"), so keep a reference until finished fires.
_ACTIVE_WORKERS = set()


def decode_barcodes(crop_path):
    """Decode QR codes / barcodes in a crop, if a decoder is installed.

    Deliberately optional. zxing-cpp is a small self-contained wheel, but it is
    not needed for anything else kcircle does, so it is an extra rather than a
    requirement - install it with `uv pip install zxing-cpp` (or the `qr` extra)
    and QR/barcode searching starts working.

    Without it this returns an empty list and the crop is searched as an image,
    exactly as before. Nothing here runs unless the module is importable.
    """
    try:
        import zxingcpp
    except Exception:                                # noqa: BLE001
        return []
    try:
        from PIL import Image
        with Image.open(crop_path) as im:
            found = zxingcpp.read_barcodes(im.convert("RGB"))
    except Exception as exc:                         # noqa: BLE001
        if logg:
            logg.debug("barcode decode failed: %s", exc)
        return []

    out = []
    for item in found or []:
        text = getattr(item, "text", "") or ""
        if not text:
            continue
        fmt = str(getattr(item, "format", "") or "")
        out.append((fmt, text))
        if logg:
            logg.info("barcode: %s -> %s", fmt, text[:120])
    return out


class LensWorker(QThread):
    done = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, shot_path: str, region: tuple, translate_to: str | None,
                 poly: list | None = None):
        super().__init__()
        self.shot_path = shot_path
        self.region = region
        self.translate_to = translate_to
        self.poly = poly

    def run(self):
        try:
            from chrome_lens_py import LensAPI

            crop = crop_region(self.shot_path, self.region, self.poly)

            async def go():
                api = LensAPI()
                try:
                    return await api.process_image(
                        crop,
                        target_translation_language=self.translate_to,
                        output_format="full_text",
                    )
                finally:
                    await api.aclose()

            # Decode locally and instantly: a QR code on screen should turn
            # into its URL straight away rather than after a round trip.
            barcodes = decode_barcodes(crop)

            r = asyncio.run(go())
            r["_crop"] = crop
            r["_barcodes"] = barcodes
            self.done.emit(r)
        except Exception as exc:                     # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")


def _upload_page(crop_path: str) -> str:
    """Write a self-contained page that POSTs the crop to Lens, and return its
    path. Same trick Chromium itself uses, so it runs in our embedded view
    instead of spawning a browser.

    The page is written to a file and loaded by URL rather than handed to
    setHtml(). QtWebEngine cannot display setHtml() content larger than 2 MB,
    and a full-screen crop base64-encodes well past that, which silently left
    the Visual matches tab blank. A local file has no such limit.
    """
    import base64

    try:
        from PIL import Image
        with Image.open(crop_path) as im:
            dims = f"{im.width}x{im.height}"
    except Exception:                                # noqa: BLE001
        dims = ""

    with open(crop_path, "rb") as fh:
        raw = fh.read()
    b64 = base64.b64encode(raw).decode()

    page = f"""<!doctype html><meta charset=utf-8>
<body style="background:#202124;color:#9aa0a6;font:13px sans-serif;
             display:flex;align-items:center;justify-content:center;height:100vh">
<div>Uploading selection to Google Lens…</div>
<form id=f method=POST enctype="multipart/form-data"
      action="https://lens.google.com/v3/upload">
  <input name=processed_image_dimensions value="{dims}">
</form>
<script>
(function(){{
  var bin = atob("{b64}");
  var arr = new Uint8Array(bin.length);
  for (var i=0;i<bin.length;i++) arr[i]=bin.charCodeAt(i);
  // The real pixel dimensions matter to Lens; byte length does not.
  var file = new File([arr], "selection.png", {{type:"image/png"}});
  var f = document.getElementById('f');
  var fi = document.createElement('input');
  fi.type='file'; fi.name='encoded_image';
  var dt = new DataTransfer(); dt.items.add(file); fi.files = dt.files;
  f.appendChild(fi);
  HTMLFormElement.prototype.submit.call(f);
}})();
</script>"""

    base, _ = os.path.splitext(crop_path)
    out = base + ".upload.html"
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(page)
    return out


def _allow_local_lens_upload(page) -> None:
    """Allow the local upload document to POST to Lens.

    Chromium blocks local-origin documents from accessing remote URLs by
    default. The Visual tab is intentionally a file:// form that submits the
    selected image to lens.google.com, so enable this narrowly on that page
    rather than on the whole WebEngine profile. Do not enable local file URL
    access: the upload document embeds its crop and needs no other local files.
    """
    settings = page.settings()
    settings.setAttribute(
        QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True
    )
    settings.setAttribute(
        QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, False
    )

# Kept deliberately tiny.
#
# This used to be ~70 lines of !important overrides forcing Google's desktop
# results into a narrow panel: fixed-width flattening, grid-template rewrites,
# flex-direction column on every [style*=flex], and word-break on every
# element. All of it fought Google's own layout while the page was still
# hydrating, and results would appear and then vanish seconds later.
#
# What is left only removes page furniture that is never a result - the
# header and the search box. Results are not restyled or re-wrapped at all;
# they lay out as Google intends at whatever width the panel is. Widen the
# panel if desktop results look cramped.
MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/157.0.0.0 Mobile Safari/537.36"
)

DESKTOP_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36"
)

UA_PRESETS = {"mobile": MOBILE_UA, "desktop": DESKTOP_UA}

# Google lays desktop results out in a fixed-width column of roughly this
# width. The panel is usually narrower, so the page is scaled down to fit and
# Chromium does the wrapping natively, instead of us overriding its CSS.


def _settings_path() -> str:
    d = os.path.expanduser("~/.config/kcircle")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "settings.json")


def load_settings() -> dict:
    import json
    try:
        with open(_settings_path()) as fh:
            return json.load(fh)
    except Exception:                                # noqa: BLE001
        return {}


def save_settings(s: dict) -> None:
    import json
    try:
        with open(_settings_path(), "w") as fh:
            json.dump(s, fh, indent=2)
    except Exception:                                # noqa: BLE001
        pass


# ----------------------------------------------------------------- results

def rounded_mask(w: int, h: int, radius: int) -> "QRegion":
    """Clip a frameless window to rounded corners.

    On Wayland the compositor gives no titlebar, so the only way to get soft
    corners is to mask the surface ourselves.
    """
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, w, h), radius, radius)
    return QRegion(path.toFillPolygon().toPolygon())


class GlowTrail:
    """The travelling light trail behind a Circle-to-Search stroke.

    A wide soft halo, a tighter mid glow, a bright core, and a spark that rides
    the cursor - the same read as Google's gesture.

    This paints into a QPainter the caller already owns. It is deliberately
    NOT a child widget: a full-size child would paint over the screenshot the
    overlay is showing.
    """

    def __init__(self):
        self.path = QPainterPath()
        self.spark = QPointF()
        self.t = 0.0
        # QPainterPath.isEmpty() is True for a path holding only a moveTo,
        # so a press with no motion yet would paint nothing. Track it.
        self.drawing = False

    def begin(self, at: QPointF):
        self.drawing = True
        self.spark = at
        self.path = QPainterPath(at)

    def clear(self):
        self.path = QPainterPath()
        self.t = 0.0
        self.drawing = False

    @property
    def active(self):
        return self.drawing

    def paint(self, p):
        if not self.drawing:
            return
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        length = self.path.length()
        if length <= 0:
            # pressed but not moved: just the spark
            self._paint_spark(p)
            p.restore()
            return
        head = min(length, self.t * length)

        # 1. soft halo
        for width, alpha, colour in ((26, 26, (150, 190, 255)),
                                     (14, 55, (120, 180, 255)),
                                     (7, 110, (90, 160, 255))):
            pen = QPen(QColor(*colour, alpha))
            pen.setWidth(width)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(self.path)

        # 2. bright core
        pen = QPen(QColor(255, 255, 255, 225))
        pen.setWidth(3)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawPath(self.path)

        # 3. comet tail: a short bright segment chasing the cursor
        tail = QPainterPath()
        tail_start = max(0.0, head - 110.0)
        for i in range(41):
            d = tail_start + (head - tail_start) * i / 40.0
            pt = self.path.pointAtPercent(min(1.0, d / length))
            if i == 0:
                tail.moveTo(pt)
            else:
                tail.lineTo(pt)
        grad = QLinearGradient(self.path.pointAtPercent(
            tail_start / length), self.path.pointAtPercent(min(1.0, head / length)))
        grad.setColorAt(0.0, QColor(255, 255, 255, 0))
        grad.setColorAt(1.0, QColor(255, 255, 255, 255))
        pen2 = QPen(grad, 5)
        pen2.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen2)
        p.drawPath(tail)

        # 4. spark at the leading edge
        self._paint_spark(p)
        p.restore()

    def _paint_spark(self, p):
        halo = QRadialGradient(self.spark, 22)
        halo.setColorAt(0.0, QColor(255, 255, 255, 190))
        halo.setColorAt(0.35, QColor(140, 190, 255, 120))
        halo.setColorAt(1.0, QColor(120, 170, 255, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(halo))
        p.drawEllipse(self.spark, 22, 22)

        core = QRadialGradient(self.spark, 8)
        core.setColorAt(0.0, QColor(255, 255, 255, 255))
        core.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setBrush(QBrush(core))
        p.drawEllipse(self.spark, 8, 8)


class Shimmer(QWidget):
    """Placeholder sweep shown while the Lens request is in flight."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.pos = -1.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(28)

    def stop(self):
        self._timer.stop()

    def _tick(self):
        self.pos += 0.035
        if self.pos > 1.4:
            self.pos = -0.4
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = self.rect()

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 16))
        p.drawRoundedRect(QRectF(0, 8, r.width(), 14), 7, 7)
        p.drawRoundedRect(QRectF(0, 30, r.width(), 46), 8, 8)
        p.drawRoundedRect(QRectF(0, 84, int(r.width() * 0.7), 14), 7, 7)
        p.drawRoundedRect(QRectF(0, 106, int(r.width() * 0.45), 14), 7, 7)

        # sweeping highlight
        x = int(self.pos * r.width())
        grad = QLinearGradient(x - 90, 0, x + 90, 0)
        grad.setColorAt(0.0, QColor(255, 255, 255, 0))
        grad.setColorAt(0.5, QColor(215, 230, 255, 55))
        grad.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setBrush(QBrush(grad))
        p.drawRoundedRect(QRectF(0, 8, r.width(), 112), 8, 8)
        p.end()


class DragBar(QLabel):
    """Header strip that drags the frameless panel around.

    On Wayland a client cannot set its own toplevel position - QWidget.move()
    is ignored by the compositor. The supported route is startSystemMove(),
    which hands the drag to KWin so the pointer drives the window natively.
    """

    def __init__(self, text: str):
        super().__init__(text)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self._off = None

    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return
        win = self.window()
        handle = win.windowHandle()
        if handle is not None and handle.startSystemMove():
            return                      # compositor owns the drag now

        # fallback for compositors without the protocol
        self._off = (e.globalPosition().toPoint()
                     - win.frameGeometry().topLeft())
        self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, e):
        if self._off is not None:
            self.window().move(e.globalPosition().toPoint() - self._off)

    def mouseReleaseEvent(self, _):
        self._off = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)


class ResizeGrip(QWidget):
    """Bottom-right corner handle. Frameless windows get no WM resize."""

    def __init__(self, panel: QWidget):
        super().__init__(panel)
        self.panel = panel
        self.setFixedSize(20, 20)
        self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        self._pos = None
        self._size = None

    def paintEvent(self, _):
        p = QPainter(self)
        pen = QPen(QColor(MUTED))
        pen.setWidth(1)
        p.setPen(pen)
        w, h = self.width(), self.height()
        for i in range(1, 4):
            p.drawLine(w - i * 5, h - 2, w - 2, h - i * 5)
        p.end()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._pos = e.globalPosition().toPoint()
            self._size = self.panel.size()

    def mouseMoveEvent(self, e):
        if self._pos and self._size:
            d = e.globalPosition().toPoint() - self._pos
            self.panel.resize(
                max(MIN_PANEL_W, self._size.width() + d.x()),
                max(MIN_PANEL_H, self._size.height() + d.y()),
            )

    def mouseReleaseEvent(self, _):
        self._pos = self._size = None


class ResultsPanel(QWidget):
    """Small always-on-top card that minimizes to the tray instead of closing."""

    finished = pyqtSignal()
    newCapture = pyqtSignal()
    minimized = pyqtSignal()

    def __init__(self, shot_path: str, region: tuple, translate_to: str | None,
                 poly: list | None = None):
        super().__init__()
        # see SelectionOverlay: no Qt.Tool, or the panel hides behind others
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # Esc needs focus to reach keyPressEvent
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setStyleSheet(f"background:{PANEL_BG}; color:{FG};")
        self.setMinimumSize(MIN_PANEL_W, MIN_PANEL_H)
        self.resize(PANEL_W, PANEL_H)
        self._restore_geometry()
        # Geometry used to be written only from closeEvent, so a resize was
        # lost whenever the app was killed, crashed, or closed from the tray
        # instead of by closing the panel. Saving on a debounce keeps the file
        # current at all times and costs one write per drag, not per pixel.
        self._geom_timer = QTimer(self)
        self._geom_timer.setSingleShot(True)
        self._geom_timer.setInterval(400)
        self._geom_timer.timeout.connect(self._save_geometry)
        self._apply_mask()

        self.shot_path = shot_path
        self.region = region
        self._crop = shot_path
        self._poly = poly
        self._translate_to = translate_to
        self._started = False
        self._closing = False
        self.worker = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(6)

        # Two rows, because one could not hold everything.
        #
        # Row 1: window controls and page navigation. Row 2: the results tabs
        # and the URL bar. They used to share a single row, which needed about
        # 610px against a 320px minimum - so the URL bar was squeezed out of
        # existence at any realistic panel size. Splitting it gives both room.
        head_row = QHBoxLayout()
        head_row.setSpacing(3)
        self.new_btn = QPushButton("+")
        self.new_btn.setToolTip("Start a new capture")
        # Minimize keeps the app alive so the result stays loaded and can be
        # brought straight back - closing quits, which is not the same thing.
        self.min_btn = QPushButton("\u2013")
        self.min_btn.setToolTip("Minimize to the tray")
        self.min_btn.clicked.connect(self.minimize)
        self.close_btn = QPushButton("\u00d7")
        self.close_btn.setToolTip("Minimize to the tray (Esc)")
        self.close_btn.clicked.connect(self.minimize)
        for b in (self.new_btn, self.min_btn, self.close_btn):
            b.setFixedWidth(28)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(
                "QPushButton{background:#34313d; color:#e6e1e5;"
                "border:none; border-radius:5px; font-size:15px; padding:1px;}"
                "QPushButton:hover{background:#4a4557;}"
            )
        self.new_btn.clicked.connect(self.newCapture.emit)
        head_row.addWidget(self.new_btn)

        # --- page navigation -------------------------------------------
        # Deliberately plain glyphs: no icon theme dependency, and they line
        # up with each other without shipping assets.
        nav_qss = (
            "QPushButton{background:#2b2932; color:#cfccd4;"
            "border:none; border-radius:5px; font-size:14px; padding:2px 8px;}"
            "QPushButton:hover{background:#38343f;}"
            "QPushButton:disabled{color:#5c5866; background:#232128;}"
            "QToolButton{background:#2b2932; color:#cfccd4;"
            "border:none; border-radius:5px; font-size:14px; padding:2px 7px;}"
            "QToolButton:hover{background:#38343f;}"
        )
        self.back_btn = QPushButton("\u2190")
        self.back_btn.setToolTip("Back")
        self.back_btn.clicked.connect(lambda: self._nav("back"))
        self.fwd_btn = QPushButton("\u2192")
        self.fwd_btn.setToolTip("Forward")
        self.fwd_btn.clicked.connect(lambda: self._nav("forward"))
        self.home_btn = QPushButton("\u2302")
        self.home_btn.setToolTip("Home - back to the Lens results")
        self.home_btn.clicked.connect(lambda: self._nav("home"))
        self.hist_btn = QToolButton()
        self.hist_btn.setText("\u22ee")
        self.hist_btn.setToolTip("History")
        self.hist_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.hist_btn.setStyleSheet(nav_qss)
        self.reload_btn = QPushButton("\u21bb")
        self.reload_btn.setToolTip("Reload")
        self.reload_btn.clicked.connect(lambda: self._nav("reload"))
        for b in (self.back_btn, self.fwd_btn, self.home_btn,
                  self.reload_btn, self.hist_btn):
            b.setStyleSheet(nav_qss)
            b.setFixedWidth(30)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setEnabled(False)          # until there is somewhere to go
        for b in (self.back_btn, self.fwd_btn, self.home_btn, self.hist_btn,
                  self.reload_btn):
            head_row.addWidget(b)

        # The current Lens result URL, beside the navigation it belongs to.
        # Lens lands on a google.com/search page with a long ?vsrid= token -
        # worth showing so it can be copied, edited, or opened elsewhere.
        self.url_bar = QLineEdit()
        self.url_bar.setPlaceholderText("results URL")
        self.url_bar.setClearButtonEnabled(True)
        self.url_bar.setFixedHeight(24)
        self.url_bar.setMinimumWidth(46)
        self.url_bar.setStyleSheet(
            f"QLineEdit{{background:#1b1a20; color:{MUTED};"
            "border:1px solid #34313d; border-radius:5px;"
            "padding:0 6px; font-size:11px;}"
            f"QLineEdit:focus{{color:{FG}; border-color:#5a5470;}}"
        )
        self.url_bar.returnPressed.connect(self._url_go)
        self.url_bar.editingFinished.connect(self._url_go)
        head_row.addWidget(self.url_bar, 1)

        # Small on purpose: the title is a drag handle, not a heading, and it
        # was competing with the buttons for a single row of space.
        self._title_text = "Circle to Search"
        title = DragBar(self._title_text)
        title.setStyleSheet(
            f"color:{ACCENT}; font-size:11px; font-weight:600; padding:0 4px;")
        title.setFixedHeight(18)
        # Small on purpose, and allowed to shrink to a bare strip: it is a
        # drag handle first. At the minimum panel width the eight header
        # buttons plus the URL bar leave about 60px, which is not enough to
        # spell out the name - so the text is dropped and the empty label
        # stays draggable. _fit_header decides when.
        self.title = title
        title.setMinimumWidth(16)
        head_row.addWidget(title)
        head_row.addWidget(self.min_btn)
        head_row.addWidget(self.close_btn)
        outer.addLayout(head_row)

        self.stack = QStackedWidget()
        self.stack.setStyleSheet(
            "QStackedWidget{border:1px solid #34313d;border-radius:6px;}")
        outer.addWidget(self.stack, 1)

        # ---- tab 1: native text (fast, no browser engine involved)
        text_page = QWidget()
        tp = QVBoxLayout(text_page)
        tp.setContentsMargins(12, 10, 12, 10)
        tp.setSpacing(8)

        self.status = QLabel("Working...")
        self.status.setStyleSheet(f"color:{MUTED};")
        tp.addWidget(self.status)

        self.preview = QLabel()
        self.preview.setFixedHeight(96)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setStyleSheet(
            f"background:{DARK}; border:1px solid #3a3742; border-radius:6px;")
        tp.addWidget(self.preview)

        self.text = QLabel()
        self.text.setWordWrap(True)
        self.text.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self.text.setStyleSheet("font-size:13px;")
        tp.addWidget(self.text, 1)

        # Built here but placed in the panel's single bottom toolbar below, so
        # they stay reachable from the Visual matches tab too - previously they
        # lived inside this page and vanished with it.
        self.copy_btn = QPushButton("Copy text")
        self.copy_btn.clicked.connect(self.copy_text)
        self.mode_btn = QPushButton()
        self.mode_btn.clicked.connect(self.cycle_mode)
        self.mode_btn.setToolTip(
            "Selection style for the next search.\n\n"
            "corner - drag corner to corner; hold Shift for a square\n"
            "rect   - plain drag\n"
            "lasso  - draw around a subject, like Android Circle to Search\n"
            "ellipse- drag an ellipse\n\n"
            "Hold Shift for lasso and Alt for ellipse mid-drag\n"
            "(in corner mode Shift means square instead)."
        )
        self._settings0 = load_settings()
        self._mode = self._settings0.get("mode", DEFAULT_MODE)
        self._snap = bool(self._settings0.get("snap", True))
        # Opt-in: overwriting the clipboard unasked can lose work.
        self._autocopy = bool(self._settings0.get("autocopy", False))
        self._ua_mode = self._settings0.get("ua", "desktop")
        self._scope = self._settings0.get("scope", "select")
        self._tab = self._settings0.get("tab", 1 if HAVE_WEBENGINE else 0)
        if self._tab not in (0, 1) or (self._tab == 1 and not HAVE_WEBENGINE):
            self._tab = 1 if HAVE_WEBENGINE else 0
        if self._scope not in CAPTURE_SCOPES:
            self._scope = "select"
        self.scope_btn = QPushButton()
        self.scope_btn.clicked.connect(self.cycle_scope)
        self.scope_btn.setToolTip(
            "What gets captured on the next search.\n\n"
            "select - freeze the screen, then draw a region\n"
            "window - the active window, no drawing\n"
            "screen - the whole screen, no drawing"
        )
        self._refresh_scope_btn()
        self.snap_btn = QPushButton()
        self.snap_btn.clicked.connect(self.toggle_snap)
        self.snap_btn.setToolTip(
            "Tap and scribble assist.\n\n"
            "When on, the screen is scanned while the overlay opens and objects "
            "are outlined:\n"
            "  - a click selects the object under it\n"
            "  - a loose scribble snaps to the object it encircles\n\n"
            "Dragging still works exactly as before; this only changes what a "
            "click or a sloppy loop means."
        )
        self._refresh_snap_btn()
        self.autocopy_btn = QPushButton()
        self.autocopy_btn.clicked.connect(self.toggle_autocopy)
        self.autocopy_btn.setToolTip(
            "Copy the result as soon as it arrives.\n\n"
            "Decoded QR/barcode values first, then any text read out of the "
            "selection.\n\n"
            "Off by default: it replaces whatever was already on the "
            "clipboard. The Copy button always works regardless."
        )
        self._refresh_autocopy_btn()
        self.ua_btn = QPushButton(f"UA: {self._ua_mode}")
        self.ua_btn.clicked.connect(self.toggle_ua)
        self.ua_btn.setToolTip(
            "Which Google layout the results use.\n\n"
            "mobile  - reflows to the panel width\n"
            "desktop - richer result set, restyled to fit\n\n"
            "Applies to the Visual matches tab."
        )
        for b in (self.copy_btn, self.mode_btn, self.ua_btn, self.scope_btn,
                  self.snap_btn, self.autocopy_btn):
            b.setStyleSheet(
                "QPushButton{background:#34313d; color:#e6e1e5;"
                "border:none; border-radius:5px; padding:7px 11px;}"
                "QPushButton:hover{background:#413d4d;}"
            )
            b.setCursor(Qt.CursorShape.PointingHandCursor)
        self._refresh_mode_btn()
        self.stack.addWidget(text_page)

        # ---- tab 2: real Google Lens results (embedded, not a browser)
        self.web = None
        if HAVE_WEBENGINE:
            web_page = QWidget()
            wp = QVBoxLayout(web_page)
            wp.setContentsMargins(0, 0, 0, 0)
            self.web = QWebEngineView()
            # The initial Lens upload page is file:// and must POST remotely.
            # Keep this permission page-scoped; disabling local-file access
            # prevents it from reading any other local URLs.
            _allow_local_lens_upload(self.web.page())
            self.web.page().profile().setHttpUserAgent(
                UA_PRESETS.get(self._ua_mode, MOBILE_UA))
            self.web.loadFinished.connect(self._on_web_loaded)
            self.web.urlChanged.connect(self._sync_url)
            # Use Chromium's own menu (Reload, Open link in new tab/window,
            # Save page, Save link, Copy link address) rather than leaving it
            # to the default, which is not reliably raised on this widget.
            self.web.setContextMenuPolicy(
                Qt.ContextMenuPolicy.CustomContextMenu)
            self.web.customContextMenuRequested.connect(self._web_menu)
            wp.addWidget(self.web, 1)
            # status floats over the view instead of stealing a layout row
            self.web_status = QLabel("Loading Google Lens results...", self.web)
            self.web_status.setStyleSheet(
                f"color:{MUTED}; background:#232228cc; padding:3px 7px;"
                "border-radius:4px; font-size:11px;")
            self.web_status.move(6, 6)
            self.web_status.raise_()
            self.stack.addWidget(web_page)
        else:
            self.stack.addWidget(QLabel("PyQt6-WebEngine not installed"))

        # --- the one bottom bar -----------------------------------------
        # Tabs, the result URL and the capture controls share one row under
        # the results. At the full panel width everything fits; narrower, the
        # controls nearest the right edge move into a "»" menu rather than
        # being squeezed. QToolBar was tried first and refused: it clips a
        # button to a few pixels instead of overflowing it, which turned
        # "Shape: lasso" into a single letter and stranded the rest.
        self.bottom = QWidget()
        brow = QHBoxLayout(self.bottom)
        brow.setContentsMargins(0, 0, 0, 0)
        brow.setSpacing(4)

        tab_qss = (
            "QPushButton{background:#26242c; color:#9a96a0;"
            "border:none; border-radius:5px; padding:5px 11px; font-size:13px;}"
            "QPushButton:checked{background:#3a3742; color:#e6e1e5;}"
            "QPushButton:hover{background:#332f3c;}"
        )
        self.tab_text = QPushButton("Text")
        self.tab_visual = QPushButton("Visual matches")
        for b in (self.tab_text, self.tab_visual):
            b.setCheckable(True)
            b.setAutoExclusive(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(tab_qss)
            b.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            brow.addWidget(b)

        # A stretch keeps the tabs left and the overflow button right without
        # pinning either to an edge.
        brow.addStretch(1)

        self._controls = [self.copy_btn, self.mode_btn, self.ua_btn,
                          self.scope_btn, self.snap_btn, self.autocopy_btn]
        for b in self._controls:
            b.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            brow.addWidget(b)

        # Only shown when something has been pushed into it.
        self._overflow = QToolButton()
        self._overflow.setText("\u00bb")
        self._overflow.setToolTip("More controls")
        self._overflow.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup)
        self._overflow.setFixedWidth(28)
        self._overflow.setCursor(Qt.CursorShape.PointingHandCursor)
        self._overflow.setStyleSheet(
            "QToolButton{background:#34313d; color:#e6e1e5;"
            "border:none; border-radius:5px; font-size:13px; padding:4px;}"
            "QToolButton:hover{background:#413d4d;}"
        )
        self._overflow.setVisible(False)
        brow.addWidget(self._overflow)
        outer.addWidget(self.bottom)

        self.tab_text.setChecked(self._tab == 0)
        self.tab_visual.setChecked(self._tab == 1)
        self.tab_text.clicked.connect(lambda: self._set_tab(0))
        self.tab_visual.clicked.connect(lambda: self._set_tab(1))

        # Image search is the main event - open on it.
        if self._tab == 1:
            self._select_visual()
        else:
            self.stack.setCurrentIndex(0)

        self.footer = QLabel("Esc minimize · drag title · corner resize")
        self.footer.setStyleSheet(f"color:{MUTED}; font-size:11px; padding:0 2px;")
        self.footer.setFixedHeight(16)
        outer.addWidget(self.footer)

        self.grip = ResizeGrip(self)
        self.grip.raise_()

        self.shimmer = Shimmer(self)
        self._place_shimmer()
        self.shimmer.show()

        self._ocr = ""
        self._barcodes = []

        self.place_near_cursor()

        # Esc dismisses the panel without ending the resident app.
        self._esc = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        self._esc.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self._esc.activated.connect(self.minimize)

    def show_results(self):
        """Kick off the Lens request and make the panel visible."""
        if getattr(self, "_started", False):
            return
        self._started = True
        self.worker = LensWorker(self.shot_path, self.region,
                                 self._translate_to, self._poly)
        self.worker.done.connect(self.on_result)
        self.worker.failed.connect(self.on_error)
        # Hold the thread until it finishes, even if this panel is destroyed
        # first, so it can never be garbage-collected mid-run.
        _ACTIVE_WORKERS.add(self.worker)
        self.worker.finished.connect(lambda: _ACTIVE_WORKERS.discard(self.worker))
        self.worker.start()
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus(Qt.FocusReason.ActiveWindowFocusReason)

    def minimize(self):
        """Get the panel out of the way without discarding the result.

        Deliberately not showMinimized(): this is a frameless always-on-top
        tool window with no taskbar entry, so there would be nothing to click
        to get it back. Instead it just hides, and the app stays running with
        the result loaded.
        """
        self._save_geometry()
        self.hide()
        self.minimized.emit()
        if logg:
            logg.info("panel minimized")

    def restore(self):
        """Bring a minimized panel back, anchored where it was left."""
        if self._closing:
            return
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus(Qt.FocusReason.ActiveWindowFocusReason)

    def _apply_mask(self):
        self.setMask(rounded_mask(self.width(), self.height(), 14))

    def _cfg_path(self):
        return os.path.join(_config_dir(), "geometry.json")

    def _restore_geometry(self):
        import json
        try:
            with open(self._cfg_path()) as fh:
                g = json.load(fh)
            # Clamp: a corrupt or stale file must not resize us off-screen
            w, h = clamp_panel_size(g.get("w", PANEL_W), g.get("h", PANEL_H))
            self.resize(w, h)
            if "x" in g and "y" in g:
                self.move(int(g["x"]), int(g["y"]))
                self._restored_pos = True
        except Exception:                            # noqa: BLE001
            pass

    def _save_geometry(self):
        import json
        try:
            g = {"w": self.width(), "h": self.height(),
                 "x": self.x(), "y": self.y()}
            with open(self._cfg_path(), "w") as fh:
                json.dump(g, fh)
        except Exception:                            # noqa: BLE001
            pass

    def _place_shimmer(self):
        """Keep the loading placeholder inside the panel at any size."""
        sh = getattr(self, "shimmer", None)
        if sh is None:
            return
        w = max(160, min(self.width() - 40, 300))
        h = max(96, min(self.height() - 200, 132))
        sh.setGeometry(20, 60, w, h)

    def resizeEvent(self, e):
        # resize() is called during __init__, before the grip/shimmer exist
        grip = getattr(self, "grip", None)
        if grip is not None:
            grip.move(self.width() - grip.width() - 4,
                      self.height() - grip.height() - 4)
            grip.raise_()
        self._place_shimmer()
        self._apply_mask()
        self._fit_bottom_bar()
        self._fit_header()
        # not during __init__, before the timer exists
        timer = getattr(self, "_geom_timer", None)
        if timer is not None:
            timer.start()
        super().resizeEvent(e)

    def _fit_header(self):
        """Drop the title text before the header runs out of room.

        The title is a label with a fixed pixel width; the eight buttons and
        the URL bar are not. Rather than let the layout clip whichever it
        likes, the name is hidden when there is not enough space to spell it
        and the label is left as a small empty strip to drag the window by.
        """
        title = getattr(self, "title", None)
        if title is None:
            return
        row = title.parentWidget()
        if row is None or not row.layout():
            return
        gap = row.layout().spacing()
        glyphs = (self.new_btn, self.back_btn, self.fwd_btn, self.home_btn,
                  self.hist_btn, self.reload_btn, self.min_btn, self.close_btn)
        fixed = sum(g.maximumWidth() for g in glyphs)
        slack = (self.width() - fixed - gap * len(glyphs)
                 - self.url_bar.minimumWidth() - title.minimumWidth())
        needed = title.fontMetrics().horizontalAdvance(self._title_text) + 8
        want = self._title_text if slack >= needed else ""
        if title.text() != want:
            title.setText(want)
            title.setToolTip(self._title_text if not want else "")

    def _fit_bottom_bar(self):
        """Move capture controls into the "»" menu until the bar fits.

        Everything is tried visible first. If the row is wider than the panel,
        the control nearest the right edge is hidden into the overflow menu and
        the measurement is repeated. The tabs and the URL bar are never hidden:
        they are the two things that must stay reachable at any width.

        Called from resizeEvent, so dragging a corner smaller is enough to
        trigger it - there is no separate breakpoint to keep in step.
        """
        if not hasattr(self, "_controls"):
            return                      # still in __init__

        # Count everything the row holds, not just the controls: forgetting the
        # tabs and the inter-item spacing made the first attempt look as though
        # everything fitted, and the layout then compressed the buttons instead.
        layout = self.bottom.layout()
        gap = layout.spacing()

        def needed():
            items = 2                                  # the two tabs
            total = (self.tab_text.sizeHint().width()
                     + self.tab_visual.sizeHint().width())
            for b in self._controls:
                if b.isVisible():
                    items += 1
                    total += b.sizeHint().width()
            if hidden:
                items += 1
                total += self._overflow.sizeHint().width()
            return total + gap * max(0, items - 1)

        menu = self._overflow.menu()
        if menu is None:
            menu = QMenu(self._overflow)
            self._overflow.setMenu(menu)
        menu.clear()

        hidden = []
        for b in self._controls:
            b.setVisible(True)
        self._overflow.setVisible(False)

        avail = self.bottom.width()
        while needed() > avail and any(b.isVisible() for b in self._controls):
            for b in reversed(self._controls):
                if b.isVisible():
                    b.setVisible(False)
                    hidden.insert(0, b)
                    break

        if hidden:
            self._overflow.setVisible(True)
            for b in hidden:
                action = menu.addAction(b.text())
                action.triggered.connect(b.click)

    def place_near_cursor(self):
        """Honour a saved position; only auto-place on the first ever run."""
        scr = QGuiApplication.primaryScreen()
        g = scr.availableGeometry()

        if getattr(self, "_restored_pos", False):
            # keep it on-screen in case the display layout changed
            x = min(max(self.x(), g.left() + 8),
                    g.right() - self.width() - 8)
            y = min(max(self.y(), g.top() + 8),
                    g.bottom() - self.height() - 8)
            self.move(x, y)
        else:
            x = min(g.right() - self.width() - 24, g.left() + g.width() // 2)
            y = min(g.bottom() - self.height() - 24, g.top() + g.height() // 3)
            self.move(max(g.left() + 24, x), max(g.top() + 24, y))

        self.show()
        self.raise_()

    def on_result(self, r):
        # the panel may have been closed while the request was in flight
        if getattr(self, "_closing", False) or not hasattr(self, "shimmer"):
            return
        self.shimmer.stop()
        self.shimmer.hide()
        crop = r.get("_crop") or self.shot_path
        self._crop = crop
        img = QImage(crop)
        if not img.isNull():
            self.preview.setPixmap(
                QPixmap.fromImage(img).scaled(
                    self.preview.width() - 12,
                    self.preview.height() - 12,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )

        ocr = (r.get("ocr_text") or "").strip()
        barcodes = r.get("_barcodes") or []
        self._barcodes = barcodes
        self._ocr = ocr

        parts = []
        # A decoded code is the most reliable result there is, so lead with it.
        for fmt, value in barcodes:
            label = fmt.replace("BarcodeFormat.", "") if fmt else "code"
            parts.append(f"[{label}]  {value}")
        if ocr:
            parts.append(ocr)
        tr = (r.get("translated_text") or "").strip()
        if tr and tr != ocr:
            parts.append(f"— translated —\n{tr}")

        # chrome-lens-py returns detected objects as geometry/segmentation data
        # with no human-readable label, so there is nothing to list here. (The
        # old code looked for an "name" key that this client never emits, so
        # that section could never render.)

        self.text.setText("\n\n".join(parts) if parts else "No text found here.")
        self.status.setText(f"done · {r.get('detected_language') or 'unknown'}")
        self._auto_copy()

        # the selection crop now exists - the visual tab can finally load
        if self.stack.currentIndex() == 1:
            self._load_results()

    def on_error(self, msg):
        if getattr(self, "_closing", False) or not hasattr(self, "shimmer"):
            return
        self.shimmer.stop()
        self.shimmer.hide()
        self.status.setText("Lens request failed")
        self.text.setText(msg)
        self.ua_btn.setEnabled(False)
        if logg:
            logg.error("lens request failed: %s", msg)

    def _copy_payload(self):
        """Everything worth putting on the clipboard, as one string.

        Decoded codes come first when there are any: they are exact, whereas
        OCR output can carry its own errors.
        """
        codes = [v for _f, v in (getattr(self, "_barcodes", None) or [])]
        if codes:
            payload = "\n".join(codes)
            if self._ocr:
                payload += "\n\n" + self._ocr
            return payload
        return self._ocr

    def copy_text(self):
        from PyQt6.QtWidgets import QApplication
        payload = self._copy_payload()
        QApplication.clipboard().setText(payload)
        self.footer.setText("Copied to clipboard" if payload
                            else "Nothing to copy")

    def _auto_copy(self):
        """Put the result on the clipboard without being asked.

        Off by default, and deliberately so: silently replacing whatever the
        user had copied is the kind of thing that loses work. Only fires when
        there is something to copy, so a failed or empty search leaves the
        clipboard alone.
        """
        if not getattr(self, "_autocopy", False):
            return
        payload = self._copy_payload()
        if not payload:
            return
        from PyQt6.QtWidgets import QApplication
        QApplication.clipboard().setText(payload)
        self.footer.setText("Copied to clipboard automatically")
        if logg:
            logg.info("auto-copied %d characters", len(payload))

    def _refresh_mode_btn(self):
        names = {
            "rect": "▭ rect",
            "corner": "◱ corner",
            "lasso": "◯ lasso",
            "ellipse": "⬭ ellipse",
            "circle": "◯ circle",
        }
        self.mode_btn.setText("Shape: " + names.get(self._mode, self._mode))

    def cycle_mode(self):
        """Persist the selection style so the next launch starts with it."""
        order = ["lasso", "ellipse", "circle", "rect", "corner"]
        # settings.json may hold a mode this build no longer knows
        if self._mode not in order:
            self._mode = order[0]
        self._mode = order[(order.index(self._mode) + 1) % len(order)]
        self._settings0["mode"] = self._mode
        save_settings(self._settings0)
        self._refresh_mode_btn()
        self.footer.setText(f"Next selection: {self._mode}")

    def _refresh_scope_btn(self):
        names = {"select": "▣ select", "window": "▢ window",
                 "screen": "▦ screen"}
        self.scope_btn.setText(names.get(self._scope, self._scope))

    def _refresh_autocopy_btn(self):
        self.autocopy_btn.setText("⧉ auto" if self._autocopy else "⧉")

    def toggle_autocopy(self):
        self._autocopy = not self._autocopy
        self._settings0["autocopy"] = self._autocopy
        save_settings(self._settings0)
        self._refresh_autocopy_btn()
        self.footer.setText("Auto-copy " + ("on" if self._autocopy else "off"))

    def _refresh_snap_btn(self):
        self.snap_btn.setText("◎ tap" if self._snap else "○ tap")

    def toggle_snap(self):
        self._snap = not self._snap
        self._settings0["snap"] = self._snap
        save_settings(self._settings0)
        self._refresh_snap_btn()
        self.footer.setText("Tap assist " + ("on" if self._snap else "off"))

    def cycle_scope(self):
        self._scope = CAPTURE_SCOPES[
            (CAPTURE_SCOPES.index(self._scope) + 1) % len(CAPTURE_SCOPES)]
        self._settings0["scope"] = self._scope
        save_settings(self._settings0)
        self._refresh_scope_btn()
        self.footer.setText(f"Next capture: {self._scope}")

    def toggle_ua(self):
        """Flip between Google's mobile and desktop presentations."""
        if self.web is None:
            return
        self._ua_mode = "desktop" if self._ua_mode == "mobile" else "mobile"
        self._settings0["ua"] = self._ua_mode
        save_settings(self._settings0)

        self.ua_btn.setText(f"UA: {self._ua_mode}")
        self.web.page().profile().setHttpUserAgent(UA_PRESETS[self._ua_mode])

        # re-run the search with the new identity
        self._load_results(force=True)

    def _on_web_loaded(self, ok):
        """Re-apply the responsive rules after every navigation."""
        if self.web is None:
            return
        self.web_status.setText("Google Lens results" if ok else "load failed")
        self._nav_state()
        if ok:
            self.reload_btn.setEnabled(True)

    def _url_go(self):
        """Navigate the embedded view to whatever is typed in the bar.

        Connected to both returnPressed and editingFinished, which both fire on
        Enter; skip the second so a single Enter is not two navigations.
        """
        if self.web is None:
            return
        text = self.url_bar.text().strip()
        if not text:
            return
        if not text.startswith(("http://", "https://")):
            text = "https://" + text
            self.url_bar.setText(text)
        if text == self.web.url().toString():
            return
        if logg:
            logg.info("url bar: navigating to %s", text)
        self.web.load(QUrl(text))

    def _sync_url(self, url=None):
        """Mirror the view's address into the bar unless the user is editing."""
        if self.web is None:
            return
        if self.url_bar.hasFocus():
            return
        u = url.toString() if url is not None else self.web.url().toString()
        if u and u != "about:blank":
            self.url_bar.setText(u)
            self.url_bar.setCursorPosition(0)
        self._nav_state()

    def _nav(self, action):
        """Back / forward / home / reload on the embedded page."""
        if self.web is None:
            return
        try:
            if action == "back":
                self.web.back()
            elif action == "forward":
                self.web.forward()
            elif action == "reload":
                self.web.reload()
            elif action == "home":
                # "Home" is the Lens result we came from, not a start page.
                if self._uploaded_crop:
                    self._load_results(force=True)
        except Exception as exc:                     # noqa: BLE001
            if logg:
                logg.debug("nav %s failed: %s", action, exc)

    def _nav_state(self):
        """Grey out whatever cannot be done from here."""
        if self.web is None:
            return
        hist = self.web.history()
        self.back_btn.setEnabled(hist.canGoBack())
        self.fwd_btn.setEnabled(hist.canGoForward())
        self.home_btn.setEnabled(bool(getattr(self, "_uploaded_crop", None)))
        self._fill_history_menu()

    def _fill_history_menu(self):
        """Populate the history dropdown from the engine's own list.

        QWebEngineHistory exposes items() and goToItem() rather than an index
        API, and the URL and title live on the item itself.
        """
        menu = self.hist_btn.menu()
        if menu is None:
            menu = QMenu(self.hist_btn)
            self.hist_btn.setMenu(menu)
        menu.clear()
        if self.web is None:
            return
        hist = self.web.history()
        try:
            entries = []
            for item in hist.items():
                url = item.url().toString()
                title = (item.title() or "").strip()
                if url and url != "about:blank":
                    entries.append((title or url, url))
        except Exception as exc:                     # noqa: BLE001
            if logg:
                logg.debug("history unavailable: %s", exc)
            entries = []
        if not entries:
            empty = menu.addAction("No history yet")
            empty.setEnabled(False)
            return
        current = None
        try:
            item = hist.currentItem()
            current = item.url().toString() if item is not None else None
        except Exception:                            # noqa: BLE001
            current = None
        for title, url in reversed(entries[-25:]):
            action = menu.addAction(("› " if url == current else "") + title[:70])
            action.setToolTip(url)
            action.triggered.connect(
                lambda _checked=False, u=url: self._nav_go(u))

    def _nav_go(self, url):
        if self.web is not None and url:
            self.web.load(QUrl(url))

    def _web_menu(self, pos):
        """A real browser context menu for the results page.

        PyQt 6 exposes no createStandardContextMenu(), so the menu is assembled
        from QWebEnginePage.WebAction instead - the same set Chromium offers:
        Back, Forward, Reload, Copy, Copy link address, Save page, and the
        open/save link actions. Note the name is DownloadLinkToDisk, not
        SaveLinkToDisk; the latter does not exist and raised AttributeError on
        every right-click.

        Nothing is injected into the page to build this; it is a native menu.
        """
        if self.web is None:
            return
        page = self.web.page()
        menu = QMenu(self.web)
        act = page.triggerAction

        def add(action, label, enabled=True):
            item = menu.addAction(label)
            item.setEnabled(enabled)
            item.triggered.connect(lambda _c=False, a=action: act(a))

        wa = QWebEnginePage.WebAction
        add(wa.Back, "Back", self.back_btn.isEnabled())
        add(wa.Forward, "Forward", self.fwd_btn.isEnabled())
        add(wa.Reload, "Reload", self.reload_btn.isEnabled())
        menu.addSeparator()
        add(wa.Copy, "Copy")
        add(wa.CopyLinkToClipboard, "Copy link address")
        add(wa.Paste, "Paste")
        menu.addSeparator()
        add(wa.SavePage, "Save page…")
        add(wa.DownloadLinkToDisk, "Save link as…")
        menu.addSeparator()
        add(wa.OpenLinkInNewTab, "Open link in new tab")
        add(wa.OpenLinkInNewWindow, "Open link in new window")
        menu.exec(self.web.mapToGlobal(pos))

    def _select_visual(self):
        """Switch to the Visual matches page and load it if we can."""
        self.tab_visual.setChecked(True)
        self.stack.setCurrentIndex(1)
        self._on_tab(1)

    def _set_tab(self, idx: int):
        """Remember which results tab was last open, and show it.

        The choice is written to settings.json so the tab the user last used
        is the one reopened on the next launch.
        """
        self._tab = idx
        self._settings0["tab"] = idx
        save_settings(self._settings0)
        if idx == 1:
            self._select_visual()
        else:
            self.stack.setCurrentIndex(0)

    def _on_tab(self, idx):
        """Only pay for the embedded engine when the user asks for it."""
        if idx == 1 and self.web is not None:
            self._load_results()

    @property
    def _crop_ready(self):
        """True once the worker has produced the real selection crop.

        Until then self._crop is still the full screenshot. Uploading that is
        exactly how the tool ended up searching the whole screen, so the
        visual tab must refuse to load until the crop exists.
        """
        c = getattr(self, "_crop", None)
        return bool(c) and c != self.shot_path and os.path.exists(c)

    def _load_results(self, force=False):
        if self.web is None:
            return
        if not self._crop_ready:
            self.web_status.setText("Waiting for the selection…")
            if logg:
                logg.debug("visual tab requested before the crop exists; "
                           "not uploading the full screenshot")
            return
        # upload each crop exactly once: Google re-renders on resize and on
        # every navigation, and re-POSTing the image each time is both slow
        # and pointless
        if getattr(self, "_uploaded_crop", None) == self._crop and not force:
            return
        self._uploaded_crop = self._crop
        self.web_status.setText("Contacting Google Lens...")
        if logg:
            logg.info("uploading selection: %s", self._crop)
        # load from a file URL rather than setHtml: a full-screen crop exceeds
        # QtWebEngine's 2 MB setHtml() limit and would render an empty tab
        self.web.load(QUrl.fromLocalFile(_upload_page(self._crop)))

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.minimize()

    def closeEvent(self, e):
        # The panel is never an exit control. Window-manager close requests
        # (including any compositor-provided close gesture) go to the tray.
        # Internal replacement during an explicit new search is the one
        # operation allowed to destroy this widget.
        if not getattr(self, "_destroying", False):
            e.ignore()
            self.minimize()
            return
        self._closing = True
        self.shimmer.stop()
        # The Lens request may still be in flight. quit() is a no-op here (the
        # thread runs asyncio, not a Qt event loop), so wait briefly, and hand
        # ownership to _ACTIVE_WORKERS regardless: dropping our reference while
        # the thread still runs would abort the process.
        w = getattr(self, "worker", None)
        if w is not None and w.isRunning():
            w.wait(1500)
        self.worker = None
        self._settings0["tab"] = self._tab
        save_settings(self._settings0)
        self._save_geometry()
        self.finished.emit()
        super().closeEvent(e)


# ----------------------------------------------------------------- main

def _tray_icon():
    """App icon for the tray, if we can find one.

    Prefers the installed themed icon so the tray entry looks like everything
    else in the panel, and falls back to the copy shipped next to the script.
    """
    from PyQt6.QtGui import QIcon
    themed = QIcon.fromTheme("kcircle")
    if not themed.isNull():
        return themed
    here = os.path.dirname(os.path.abspath(__file__))
    for name in ("kcircle.svg", "kcircle.png"):
        path = os.path.join(here, name)
        if os.path.exists(path):
            return QIcon(path)
    return QIcon()


def main():
    global logg, HAVE_WEBENGINE
    logg = setup_logging()
    translate_to = os.environ.get("KCIRCLE_TRANSLATE") or None

    # Check for the resident process before doing any expensive startup work
    # (notably the QtWebEngine health probe). A relaunch must hand off to the
    # in-memory process immediately, even when its panel is in the tray.
    _shm = QSharedMemory("kcircle-single-instance")
    if _shm.attach():
        _sock = QLocalSocket()
        connected = False
        _sock.connectToServer("kcircle-ipc")
        for _ in range(10):
            if _sock.waitForConnected(250):
                connected = True
                break
            _sock.abort()
            _sock.connectToServer("kcircle-ipc")
        if connected:
            _sock.write(b"new-search")
            _sock.flush()
            _sock.waitForBytesWritten(1000)
            _sock.disconnectFromServer()
            _shm.detach()
            sys.exit(0)
        if logg:
            logg.warning("stale instance marker; starting a new resident process")
        _sock.disconnectFromServer()
        _shm.detach()
    if not _shm.create(1):
        # A just-exited process may release the marker a moment later. Retry
        # once rather than consuming the user's first shortcut press.
        time.sleep(0.05)
        if not _shm.create(1):
            logg.error("could not claim the single-instance marker")
            sys.exit(1)
    _start_relay_socket()

    logg.info("kcircle start (translate=%s, webengine=%s, settings=%s)",
              translate_to or "off", HAVE_WEBENGINE, load_settings())

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    state = {"panel": None, "overlay": None}

    def restore_panel(*_):
        """Restore the one resident session without starting another capture."""
        overlay = state["overlay"]
        if overlay is not None:
            overlay.raise_()
            overlay.activateWindow()
            return
        panel = state["panel"]
        if panel is None or panel._closing:
            return
        if state["overlay"] is not None:
            return                       # a selection is in progress
        panel.restore()
        # No explicit dismissal: Qt 6 dropped QSystemTrayIcon.hideMessage(), and
        # the notification expires on its own. Calling it raised AttributeError
        # and made the tray icon - the only way back from minimize - crash.

    # The tray is where a minimized panel goes. Without it the minimize button
    # would hide a frameless, always-on-top window that has no taskbar entry and
    # no visible affordance - it would simply vanish, which is worse than not
    # offering the option at all.
    tray = QSystemTrayIcon(_tray_icon(), app)
    tray.setToolTip("Circle to Search — click to search, right-click for more")
    tray_menu = QMenu()
    tray_menu.addAction("Restore panel", restore_panel)
    # Resolved at click time, by which point begin_capture is defined below.
    tray_menu.addAction("New search", lambda: begin_capture())
    tray_menu.addSeparator()
    tray_menu.addAction("Quit", app.quit)
    tray.setContextMenu(tray_menu)
    # A left or double tray click is the fast search gesture. Restoring an
    # existing result remains available from the right-click menu.
    def on_tray_activated(reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            begin_capture()

    tray.activated.connect(on_tray_activated)
    if QSystemTrayIcon.isSystemTrayAvailable():
        tray.show()
    else:
        # No tray on this desktop. Minimize still works, but there is then
        # nowhere to retrieve the panel from - so say so plainly rather than
        # letting the window vanish.
        if logg:
            logg.info("no system tray: a minimized panel cannot be restored")

    def begin_capture():
        """Show the scan surface immediately, then capture it in background."""
        if state["overlay"] is not None:
            # An external relaunch always means a new search. Replace an
            # in-progress selection rather than silently reusing it.
            old_overlay = state["overlay"]
            state["overlay"] = None
            old_overlay.close()
            if logg:
                logg.info("restarting the active selection from a relaunch")
        panel = state["panel"]
        if panel is not None:
            panel.hide()
        scope = load_settings().get("scope", "select")
        if scope not in CAPTURE_SCOPES:
            scope = "select"
        ov = SelectionOverlay()
        state["overlay"] = ov
        shot = None

        def on_capture_failed(message):
            state["overlay"] = None
            ov.close()
            logg.error("capture failed: %s", message)
            if QSystemTrayIcon.isSystemTrayAvailable():
                tray.showMessage("Circle to Search", "Capture failed; still running in the tray",
                                 QSystemTrayIcon.MessageIcon.Warning, 3000)

        def on_captured(shot_path):
            nonlocal shot
            shot = shot_path
            logg.info("captured scope=%s -> %s", scope, shot_path)
            ov.set_shot(shot_path)
            if scope != "select":
                state["overlay"] = None
                ov.close()
                show_panel(shot_path, (0.0, 0.0, 1.0, 1.0), None)

        def show_panel(shot_path, region, poly):
            # Keep the scan/copy path fast. WebEngine's safety probe can spawn
            # Chromium and take seconds, but copy-only never needs a panel or
            # browser, so defer it until a result panel is actually required.
            global HAVE_WEBENGINE
            if HAVE_WEBENGINE and not webengine_is_healthy():
                logg.warning("QtWebEngine is installed but crashes on this system "
                             "(likely a PyQt6/Qt patch mismatch). Disabling the "
                             "Visual matches tab; OCR and translation still work.")
                HAVE_WEBENGINE = False
            old = state["panel"]
            if old is not None:
                old._destroying = True
                old.deleteLater()
            new = ResultsPanel(shot_path, region, translate_to, poly)
            new.newCapture.connect(begin_capture)

            def on_minimized():
                # Say where it went. A frameless always-on-top panel has no
                # taskbar entry, so without this the window just disappears.
                tray.showMessage(
                    "Circle to Search",
                    "Minimized - click the tray icon to restore",
                    QSystemTrayIcon.MessageIcon.Information, 3000)
            new.minimized.connect(on_minimized)
            state["panel"] = new
            new.show_results()

        def on_pick(selection):
            if shot is None:
                return
            state["overlay"] = None
            ov.close()
            region, poly = selection
            logg.info("picked region=%.3f,%.3f-%.3f,%.3f poly=%s",
                      *region, f"{len(poly)}pts" if poly else "none")
            show_panel(shot, region, poly)

        def on_copy_only(selection):
            """Super was held: copy the text and show nothing else.

            Deliberately independent of the auto-copy setting - holding Super
            is an explicit request to copy, and a panel would defeat the point.
            """
            if shot is None:
                return
            state["overlay"] = None
            ov.close()
            region, poly = selection
            logg.info("copy-only region=%.3f,%.3f-%.3f,%.3f poly=%s",
                      *region, f"{len(poly)}pts" if poly else "none")
            worker = LensWorker(shot, region, translate_to, poly)
            _ACTIVE_WORKERS.add(worker)
            worker.done.connect(lambda r: on_copied(r, worker))
            worker.failed.connect(lambda m: on_copy_failed(m, worker))
            worker.finished.connect(lambda: _ACTIVE_WORKERS.discard(worker))
            worker.start()

        def on_copied(r, worker):
            parts = []
            # A decoded code is the most reliable text there is, so it leads.
            for fmt, value in (r.get("_barcodes") or []):
                label = fmt.replace("BarcodeFormat.", "") if fmt else "code"
                parts.append(f"[{label}]  {value}")
            ocr = (r.get("ocr_text") or "").strip()
            if ocr:
                parts.append(ocr)
            tr = (r.get("translated_text") or "").strip()
            if tr and tr != ocr:
                parts.append(f"— translated —\n{tr}")
            if not parts:
                notify("No text found", "Nothing readable in that selection.")
                return
            payload = "\n".join(parts)
            QApplication.clipboard().setText(payload)
            logg.info("copy-only: %d characters to the clipboard",
                      len(payload))
            notify(f"Copied {len(payload)} characters",
                   payload[:120].replace("\n", " "))

        def on_copy_failed(message, worker):
            logg.error("copy-only failed: %s", message)
            notify("Copy failed", str(message)[:160])

        def notify(title, body):
            """Say it happened - with no panel, silence would look broken."""
            if QSystemTrayIcon.isSystemTrayAvailable():
                tray.showMessage("Circle to Search", f"{title}\n{body}",
                                 QSystemTrayIcon.MessageIcon.Information, 2500)
            else:
                logg.info("copy-only: %s - %s", title, body)

        def on_cancel():
            # Escape can reach both the application shortcut and the overlay's
            # key handler. Make cancellation idempotent so the second delivery
            # cannot tear down the resident tray session.
            if state["overlay"] is not ov:
                return
            state["overlay"] = None
            logg.info("cancelled by user")
            ov.close()
            if QSystemTrayIcon.isSystemTrayAvailable():
                tray.show()
            if state["panel"] is not None:
                state["panel"].show()
                state["panel"].raise_()
            else:
                # Keep the process and tray resident even if the first capture
                # is cancelled before a results panel exists.
                return

        ov.picked.connect(on_pick)
        ov.copyOnly.connect(on_copy_only)
        ov.cancelled.connect(on_cancel)
        ov.show()
        # Esc must work even if KWin refuses us keyboard focus, so grab the key
        # at application level rather than relying on keyPressEvent
        esc = QShortcut(QKeySequence(Qt.Key.Key_Escape), ov)
        esc.setContext(Qt.ShortcutContext.ApplicationShortcut)
        esc.activated.connect(on_cancel)
        ov._esc_shortcut = esc

        def _focus():
            ov.raise_()
            ov.activateWindow()
            ov.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
            ov.update()
        QTimer.singleShot(0, _focus)
        QTimer.singleShot(180, _focus)

        capture_worker = CaptureWorker(scope)
        capture_worker.captured.connect(on_captured)
        capture_worker.failed.connect(on_capture_failed)
        _SCANNERS.add(capture_worker)
        capture_worker.finished.connect(lambda: _SCANNERS.discard(capture_worker))
        capture_worker.start()

    def _poll_relaunch():
        if not _RELAUNCH_REQUESTS.empty():
            _RELAUNCH_REQUESTS.get_nowait()
            begin_capture()

    relay_timer = QTimer()
    relay_timer.setInterval(20)
    relay_timer.timeout.connect(_poll_relaunch)
    relay_timer.start()

    # IPC server: every relaunch starts a new search in the running instance.
    _srv = QLocalServer()
    _srv.removeServer("kcircle-ipc")
    _srv.listen("kcircle-ipc")
    def _on_ipc():
        conn = _srv.nextPendingConnection()
        if conn is None:
            return
        conn.waitForReadyRead(200)
        QTimer.singleShot(0, begin_capture)
        conn.disconnectFromServer()
    _srv.newConnection.connect(_on_ipc)

    QTimer.singleShot(60, begin_capture)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
