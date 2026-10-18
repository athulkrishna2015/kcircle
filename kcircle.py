#!/usr/bin/env python3
"""
kcircle - Circle to Search for KDE Plasma (Wayland), overlay-only.

Trigger -> screen freezes -> draw a box -> results appear in a small
always-on-top panel. No browser is launched; Lens is queried over its
native Protobuf endpoint via chrome-lens-py.

  OCR text, translation and object labels render natively in the panel.
  "Visual matches" is the one thing that opens a browser, since Lens
  visual search is inherently a web product.
"""

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile

from PyQt6.QtCore import (
    QPoint, QPointF, QRect, QRectF, QUrl, Qt, QThread, QTimer, pyqtSignal,
)
from PyQt6.QtGui import (
    QBrush, QColor, QFont, QGuiApplication, QImage, QKeySequence, QLinearGradient,
    QPainter, QPainterPath, QPen, QPixmap, QRadialGradient, QRegion, QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QLineEdit, QPushButton, QStackedWidget,
    QVBoxLayout, QWidget,
)
from PyQt6.QtGui import QKeySequence

try:
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    HAVE_WEBENGINE = True
except Exception:                                    # noqa: BLE001
    QWebEngineView = None
    HAVE_WEBENGINE = False


# ----------------------------------------------------------------- config

PANEL_W, PANEL_H = 460, 520
MIN_PANEL_W, MIN_PANEL_H = 320, 260
DARK = "#1c1b22"
PANEL_BG = "#232228"
FG = "#e6e1e5"
ACCENT = "#d78ce0"
MUTED = "#9a96a0"

_tmpdir = None


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
    d = os.path.expanduser("~/.config/kcircle")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "webengine.json")


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
                           capture_output=True, timeout=60)
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
    d = os.path.expanduser("~/.config/kcircle")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "kcircle.log")


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


def _virtual_desktop() -> QRect:
    """Geometry covering every screen, matching a full-desktop capture."""
    screens = QGuiApplication.screens()
    if not screens:
        return QRect(0, 0, 1920, 1080)
    rect = QRect(screens[0].geometry())
    for s in screens[1:]:
        rect = rect.united(s.geometry())
    return rect


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
    path = os.path.join(_tmpdir, "screen.png")

    if not shutil.which("spectacle"):
        raise RuntimeError("spectacle not found - cannot capture the screen")

    flag = {"select": "-f", "window": "-a", "screen": "-f"}.get(scope, "-f")
    subprocess.run(
        ["spectacle", "-b", "-n", flag, "-o", path],
        check=True, capture_output=True, timeout=30,
    )

    if not os.path.exists(path):
        raise RuntimeError("capture produced no file")
    if logg:
        logg.debug("captured scope=%s -> %s", scope, path)
    return path


# ----------------------------------------------------------------- selection

class SelectionOverlay(QWidget):
    """Fullscreen, frameless. Shows the frozen screen; user drags a box."""

    picked = pyqtSignal(tuple)   # (x1, y1, x2, y2) normalised 0..1
    cancelled = pyqtSignal()

    def __init__(self, shot_path: str):
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
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)

        self.shot = QPixmap(shot_path)
        self.origin = None
        self.current = None
        self.path = []            # lasso points, in widget coords
        self.mode = load_settings().get("mode", "rect")
        self._tmp_mode = None
        self._square = False

        # Cover the whole capture, not just the primary output. spectacle grabs the
        # entire virtual desktop, so an overlay sized to a single screen would
        # leave the rest of the capture unreachable and mis-map every
        # coordinate on a second monitor.
        self.setGeometry(_virtual_desktop())

        mode_label = {
            "rect": "rectangle",
            "corner": "corner to corner",
            "lasso": "circle / lasso",
            "ellipse": "ellipse",
        }.get(self.mode, "rectangle")

        hint = QLabel(
            f"{mode_label}  ·  Shift = square / lasso  ·  Alt = ellipse"
            "  ·  Esc to cancel"
        )
        hint.setStyleSheet("color:#ffffff; background:#000000aa; padding:6px 12px;")
        hint.setFont(QFont("", 11))
        hint.adjustSize()
        hint.move(24, 24)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.glow = GlowTrail()
        self._glow_timer = QTimer(self)
        self._glow_timer.timeout.connect(self._glow_tick)
        self._hint = hint

    def _glow_tick(self):
        self.glow.t = (self.glow.t + 0.022) % 1.0
        self.update()

    def resizeEvent(self, e):
        hint = getattr(self, "_hint", None)
        if hint is not None:
            hint.adjustSize()
        super().resizeEvent(e)

    # painting ---------------------------------------------------------
    def _effective_mode(self, ev=None):
        """Held modifiers give a temporary override of the saved mode.

        Shift -> lasso, Alt -> ellipse. In corner mode Shift is left free so
        it can mean "square" instead.
        """
        if ev is not None:
            mods = ev.modifiers()
            base = self._tmp_mode or self.mode
            if mods & Qt.KeyboardModifier.AltModifier:
                return "ellipse"
            if mods & Qt.KeyboardModifier.ShiftModifier and base != "corner":
                return "lasso"
        return self._tmp_mode or self.mode

    def _shape_rect(self):
        rect = QRect(self.origin, self.current).normalized()
        if self._square:
            side = max(rect.width(), rect.height())
            # grow a square outward from the corner the drag started at
            ax, ay = self.origin.x(), self.origin.y()
            left = ax if self.current.x() >= ax else ax - side
            top = ay if self.current.y() >= ay else ay - side
            rect = QRect(left, top, side, side)
        return rect

    def paintEvent(self, _):
        p = QPainter(self)
        p.drawPixmap(self.rect(), self.shot)
        p.fillRect(self.rect(), QColor(0, 0, 0, 110))

        shape = self._shape()

        if shape is not None:
            p.save()
            p.setClipPath(shape, Qt.ClipOperation.IntersectClip)
            p.drawPixmap(self.rect(), self.shot)
            p.restore()

        self.glow.paint(p)
        p.end()

    def _shape(self):
        """Current selection as a QPainterPath, or None if nothing drawn."""
        if self.mode == "lasso" or self._tmp_mode == "lasso":
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
        if self._tmp_mode == "ellipse":
            path = QPainterPath()
            path.addEllipse(rect)
            return path

        path = QPainterPath()
        path.addRect(rect)
        return path

    # input ------------------------------------------------------------
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.RightButton:
            self.cancelled.emit()
            return
        self._tmp_mode = self._effective_mode(e)
        # Shift in corner/rect mode means "square"
        self._square = bool(e.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        if self._tmp_mode == "lasso":
            self.path = [e.position().toPoint()]
        self.origin = e.position().toPoint()
        self.current = self.origin
        self.glow.begin(QPointF(e.position()))
        self._glow_timer.start(16)          # ~60fps
        self.update()

    def mouseMoveEvent(self, e):
        if self.origin is None:
            return
        self.current = e.position().toPoint()
        self.glow.spark = QPointF(e.position())
        if (self._tmp_mode or self.mode) == "lasso":
            self.path.append(self.current)
            self.glow.path.lineTo(QPointF(self.current))
        else:
            self.glow.path = self._shape() or QPainterPath()
        self.update()

    def mouseReleaseEvent(self, e):
        if self.origin is None:
            return
        self.current = e.position().toPoint()
        mode = self._tmp_mode or self.mode
        self._tmp_mode = None
        self._glow_timer.stop()

        if mode == "lasso":
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
            self._reset()
            self.picked.emit((rect, poly))
            return

        rect = self._shape_rect()
        if rect.width() < 12 or rect.height() < 12:
            self._reset()
            return
        norm = (rect.left() / self.width(), rect.top() / self.height(),
                rect.right() / self.width(), rect.bottom() / self.height())
        self._reset()
        self.picked.emit((norm, None))

    def _reset(self):
        self.origin = self.current = None
        self.path = []
        self._square = False
        self._glow_timer.stop()
        self.glow.clear()
        self.update()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()


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
    out = os.path.join(
        _tmpdir, f"crop-{abs(hash((region, len(poly or ()))) ) % 10**8}.png")

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
        if bbox and (bbox[2] - bbox[0] > 8 and bbox[3] - bbox[1] > 8):
            cut = cut.crop(bbox)
            mask = mask.crop(bbox)
            L("mask: %d points, trimmed to drawn area %s", len(pts), bbox)

        total = cut.size[0] * cut.size[1]
        covered = sum(1 for px in mask.getdata() if px > 128)
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

    if poly is None or len(poly) < 3:
        # no mask means no transparency to preserve; ship a plain RGB crop
        if cut.mode != "RGB":
            cut = cut.convert("RGB")

    cut.save(out, "PNG")
    L("crop saved: %s (%dx%d %s)", out, cut.size[0], cut.size[1], cut.mode)
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

            r = asyncio.run(go())
            r["_crop"] = crop
            self.done.emit(r)
        except Exception as exc:                     # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")


def _upload_page(crop_path: str) -> str:
    """A self-contained page that POSTs the crop to Lens and navigates to the
    results. Same trick Chromium itself uses, so it runs in our embedded view
    instead of spawning a browser."""
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

    return f"""<!doctype html><meta charset=utf-8>
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
_RESPONSIVE_CSS = """
header, [role="banner"], #topnav, #gnav, div#header, div#hdt,
.navigation, #foot, #search_footer {
  display: none !important;
}
form[action="/search"], #search,
textarea[name="q"], input[name="q"] {
  display: none !important;
}
"""

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
GOOGLE_DESKTOP_COLUMN = 652


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


def _responsive_script() -> str:
    """Install the (deliberately tiny) furniture-hiding stylesheet.

    The tag is updated in place rather than removed and re-appended: swapping
    it forces the page to restyle from scratch, which during Google's
    progressive hydration blanks content that has not finished rendering.
    """
    return f"""
(function(){{
  var css = {_RESPONSIVE_CSS!r};
  var s = document.getElementById('kcircle-css');
  if (s) {{
    if (s.textContent !== css) s.textContent = css;
  }} else {{
    s = document.createElement('style');
    s.id = 'kcircle-css';
    s.textContent = css;
    (document.head || document.documentElement).appendChild(s);
  }}
  var m = document.querySelector('meta[name=viewport]');
  if (!m) {{
    m = document.createElement('meta');
    m.name = 'viewport';
    m.content = 'width=device-width, initial-scale=1';
    document.head.appendChild(m);
  }}
  return 'ok';
}})()"""


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
    """Small always-on-top card. Stays out of the way, closes on Esc."""

    finished = pyqtSignal()
    newCapture = pyqtSignal()

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
        self._apply_mask()

        self.shot_path = shot_path
        self.region = region
        self._crop = shot_path
        self._poly = poly
        self._translate_to = translate_to
        self._started = False
        self.worker = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(6)

        # One header row holding +, the tab buttons, the title and x. A
        # QTabWidget cannot sit inline with the buttons, so the tabs are
        # toggle buttons driving a QStackedWidget instead.
        head_row = QHBoxLayout()
        head_row.setSpacing(4)
        self.new_btn = QPushButton("+")
        self.new_btn.setToolTip("Start a new capture")
        self.close_btn = QPushButton("\u00d7")
        self.close_btn.setToolTip("Close (Esc)")
        self.close_btn.clicked.connect(self.close)
        for b in (self.new_btn, self.close_btn):
            b.setFixedWidth(28)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(
                "QPushButton{background:#34313d; color:#e6e1e5;"
                "border:none; border-radius:5px; font-size:15px; padding:1px;}"
                "QPushButton:hover{background:#4a4557;}"
            )
        self.new_btn.clicked.connect(self.newCapture.emit)
        head_row.addWidget(self.new_btn)

        tab_qss = (
            "QPushButton{background:#26242c; color:#9a96a0;"
            "border:none; border-radius:5px; padding:4px 12px; font-size:13px;}"
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
        head_row.addWidget(self.tab_text)
        head_row.addWidget(self.tab_visual)

        # The current Lens result URL. Lens lands on a google.com/search
        # page with a long ?vsrid= token - worth showing so it can be copied,
        # edited, or opened in a real browser.
        self.url_bar = QLineEdit()
        self.url_bar.setPlaceholderText("results URL")
        self.url_bar.setClearButtonEnabled(True)
        self.url_bar.setFixedHeight(24)
        self.url_bar.setStyleSheet(
            f"QLineEdit{{background:#1b1a20; color:{MUTED};"
            "border:1px solid #34313d; border-radius:5px;"
            "padding:0 7px; font-size:11px;}"
            f"QLineEdit:focus{{color:{FG}; border-color:#5a5470;}}"
        )
        self.url_bar.returnPressed.connect(self._url_go)
        self.url_bar.editingFinished.connect(self._url_go)
        head_row.addWidget(self.url_bar, 1)

        title = DragBar("Circle to Search")
        title.setStyleSheet(
            f"color:{ACCENT}; font-size:13px; font-weight:600; padding:0 6px;")
        title.setFixedHeight(22)
        head_row.addWidget(title)
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

        row = QHBoxLayout()
        row.setSpacing(6)
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
        self._mode = self._settings0.get("mode", "rect")
        self._ua_mode = self._settings0.get("ua", "desktop")
        self._scope = self._settings0.get("scope", "select")
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
        self.ua_btn = QPushButton(f"UA: {self._ua_mode}")
        self.ua_btn.clicked.connect(self.toggle_ua)
        self.ua_btn.setToolTip(
            "Which Google layout the results use.\n\n"
            "mobile  - reflows to the panel width\n"
            "desktop - richer result set, restyled to fit\n\n"
            "Applies to the Visual matches tab."
        )
        for b in (self.copy_btn, self.mode_btn, self.ua_btn, self.scope_btn):
            b.setStyleSheet(
                "QPushButton{background:#34313d; color:#e6e1e5;"
                "border:none; border-radius:5px; padding:7px 11px;}"
                "QPushButton:hover{background:#413d4d;}"
            )
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            row.addWidget(b)
        self._refresh_mode_btn()
        tp.addLayout(row)
        self.stack.addWidget(text_page)

        # ---- tab 2: real Google Lens results (embedded, not a browser)
        self.web = None
        if HAVE_WEBENGINE:
            web_page = QWidget()
            wp = QVBoxLayout(web_page)
            wp.setContentsMargins(0, 0, 0, 0)
            self.web = QWebEngineView()
            self.web.page().profile().setHttpUserAgent(
                UA_PRESETS.get(self._ua_mode, MOBILE_UA))
            self.web.loadFinished.connect(self._on_web_loaded)
            self.web.urlChanged.connect(self._sync_url)
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

        self.tab_text.setChecked(True)
        self.tab_text.clicked.connect(lambda: self.stack.setCurrentIndex(0))
        self.tab_visual.clicked.connect(lambda: self._select_visual())
        # Image search is the main event - open on it.
        if HAVE_WEBENGINE:
            self._select_visual()

        self.footer = QLabel("Esc close · drag title · corner resize")
        self.footer.setStyleSheet(f"color:{MUTED}; font-size:11px; padding:0 2px;")
        self.footer.setFixedHeight(16)
        outer.addWidget(self.footer)

        self.grip = ResizeGrip(self)
        self.grip.raise_()

        self.shimmer = Shimmer(self)
        self._place_shimmer()
        self.shimmer.show()

        self._ocr = ""

        self.place_near_cursor()

        # Esc closes the panel even if the compositor never gave us focus
        self._esc = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        self._esc.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self._esc.activated.connect(self.close)

    def show_results(self):
        """Kick off the Lens request and make the panel visible."""
        if getattr(self, "_started", False):
            return
        self._started = True
        self.worker = LensWorker(self.shot_path, self.region,
                                 self._translate_to, self._poly)
        self.worker.done.connect(self.on_result)
        self.worker.failed.connect(self.on_error)
        self.worker.start()
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus(Qt.FocusReason.ActiveWindowFocusReason)

    def _apply_mask(self):
        self.setMask(rounded_mask(self.width(), self.height(), 14))

    def _cfg_path(self):
        d = os.path.expanduser("~/.config/kcircle")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "geometry.json")

    def _restore_geometry(self):
        import json
        try:
            with open(self._cfg_path()) as fh:
                g = json.load(fh)
            self.resize(int(g["w"]), int(g["h"]))
            if "x" in g:
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
        # Google re-lays-out on its own breakpoints, so the injected CSS has
        # to be re-applied whenever the panel changes width - otherwise
        # resizing the window leaves the desktop layout stranded at its
        # original fixed column
        if self.web is not None:
            self._resize_reflow()
        super().resizeEvent(e)

    def _resize_reflow(self):
        """Re-fit the page and re-apply the tiny stylesheet after a resize."""
        self._apply_zoom()
        self._apply_reflow()

    def _apply_zoom(self):
        """Let the engine do the wrapping: zoom Google's fixed results column
        down to the panel width.

        Google lays desktop results out in a fixed ~652px column. Rather than
        overriding that with CSS - which fought the page while it was still
        hydrating and made results vanish - scale the page so the column fits,
        and let Chromium reflow and re-wrap everything natively, exactly as a
        browser zoom does. Mobile results are already fluid, so they are left
        at 1.0.
        """
        if self.web is None:
            return
        if self._ua_mode != "desktop":
            self.web.setZoomFactor(1.0)
            return
        width = max(1, self.web.width())
        zoom = min(1.0, width / GOOGLE_DESKTOP_COLUMN)
        zoom = max(0.40, zoom)
        # snap to whole percent so we are not reflowing on every pixel
        zoom = round(zoom * 100) / 100.0
        if abs(self.web.zoomFactor() - zoom) > 0.005:
            self.web.setZoomFactor(zoom)
            if logg:
                logg.debug("zoom %.2f for a %dpx panel (column %dpx)",
                           zoom, width, GOOGLE_DESKTOP_COLUMN)

    def moveEvent(self, e):
        super().moveEvent(e)

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
        if not hasattr(self, "shimmer"):
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
        self._ocr = ocr

        parts = []
        if ocr:
            parts.append(ocr)
        tr = (r.get("translated_text") or "").strip()
        if tr and tr != ocr:
            parts.append(f"— translated —\n{tr}")

        objs = r.get("objects") or []
        names = [o.get("name") for o in objs if isinstance(o, dict) and o.get("name")]
        if names:
            parts.append("— detected —\n" + ", ".join(dict.fromkeys(names)))

        self.text.setText("\n\n".join(parts) if parts else "No text or objects found here.")
        self.status.setText(f"done · {r.get('detected_language') or 'unknown'}")

        # the selection crop now exists - the visual tab can finally load
        if self.stack.currentIndex() == 1:
            self._load_results()

    def on_error(self, msg):
        if not hasattr(self, "shimmer"):
            return
        self.shimmer.stop()
        self.shimmer.hide()
        self.status.setText("Lens request failed")
        self.text.setText(msg)
        self.ua_btn.setEnabled(False)
        if logg:
            logg.error("lens request failed: %s", msg)

    def copy_text(self):
        from PyQt6.QtWidgets import QApplication
        QApplication.clipboard().setText(self._ocr)
        self.footer.setText("Copied to clipboard")

    def _refresh_mode_btn(self):
        names = {
            "rect": "▭ rect",
            "corner": "◱ corner",
            "lasso": "◯ lasso",
            "ellipse": "⬭ ellipse",
        }
        self.mode_btn.setText("Shape: " + names.get(self._mode, self._mode))

    def cycle_mode(self):
        """Persist the selection style so the next launch starts with it."""
        order = ["corner", "lasso", "ellipse", "rect"]
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
        self._apply_zoom()

        # re-run the search with the new identity
        self._load_results(force=True)

    def _on_web_loaded(self, ok):
        """Re-apply the responsive rules after every navigation."""
        if self.web is None:
            return
        self.web_status.setText("Google Lens results" if ok else "load failed")
        self._apply_reflow()
        self._schedule_reflow()

    def _apply_reflow(self):
        """Inject the CSS. Safe to call repeatedly - it replaces its own tag."""
        if self.web is None:
            return
        self.web.page().runJavaScript(_responsive_script())
        self._apply_zoom()

    def _schedule_reflow(self):
        """Re-apply a few times as results hydrate in.

        Only _on_web_loaded may call this. The timers must not call back into
        it, or every tick would spawn three more and the panel would lock up
        within seconds.
        """
        if self.web is None:
            return
        self._cancel_reflow()
        self._reflow_timers = []
        # results hydrate after loadFinished, so give them a couple of nudges
        # - but few. Every re-apply during hydration risks catching a result
        # container mid-render.
        for delay in (900, 2200):
            t = QTimer(self)
            t.setSingleShot(True)
            t.timeout.connect(self._apply_reflow)
            t.start(delay)
            self._reflow_timers.append(t)

    def _cancel_reflow(self):
        for t in getattr(self, "_reflow_timers", None) or []:
            t.stop()
        self._reflow_timers = []

    def _url_go(self):
        """Navigate the embedded view to whatever is typed in the bar."""
        if self.web is None:
            return
        text = self.url_bar.text().strip()
        if not text:
            return
        if not text.startswith(("http://", "https://")):
            text = "https://" + text
            self.url_bar.setText(text)
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

    def _select_visual(self):
        """Switch to the Visual matches page and load it if we can."""
        self.tab_visual.setChecked(True)
        self.stack.setCurrentIndex(1)
        self._on_tab(1)

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
        self._apply_zoom()
        if logg:
            logg.info("uploading selection: %s", self._crop)
        self.web.setHtml(_upload_page(self._crop))

    def open_browser(self):
        """Last resort: hand the crop to the system browser."""
        import webbrowser

        from PyQt6.QtWidgets import QApplication
        img = QImage(getattr(self, "_crop", self.shot_path))
        if img.isNull():
            self.footer.setText("Nothing to send")
            return
        QApplication.clipboard().setImage(img)
        webbrowser.open("https://lens.google.com/")
        self.footer.setText("Image copied — paste into Lens (Ctrl+V)")

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.close()

    def closeEvent(self, e):
        self._cancel_reflow()
        self.shimmer.stop()
        # the Lens request may still be in flight; a queued signal would then
        # hit a deleted C++ object and abort the process
        w = getattr(self, "worker", None)
        if w is not None and w.isRunning():
            w.finished.connect(w.deleteLater)
            w.quit()
            w.wait(1500)
            self.worker = None
        self._save_geometry()
        self.finished.emit()
        super().closeEvent(e)


# ----------------------------------------------------------------- main

def main():
    global logg, HAVE_WEBENGINE
    logg = setup_logging()
    translate_to = os.environ.get("KCIRCLE_TRANSLATE") or None

    if HAVE_WEBENGINE and not webengine_is_healthy():
        logg.warning("QtWebEngine is installed but crashes on this system "
                     "(likely a PyQt6/Qt patch mismatch). Disabling the "
                     "Visual matches tab; OCR and translation still work.")
        HAVE_WEBENGINE = False
        QWebEngineView = None

    logg.info("kcircle start (translate=%s, webengine=%s, settings=%s)",
              translate_to or "off", HAVE_WEBENGINE, load_settings())

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    state = {"panel": None, "overlay": None}

    def begin_capture():
        """Hide the panel, freeze the screen, wait for a selection."""
        panel = state["panel"]
        if panel is not None:
            panel.hide()
            panel._cancel_reflow()
        scope = load_settings().get("scope", "select")
        if scope not in CAPTURE_SCOPES:
            scope = "select"
        try:
            shot = capture(scope)
            logg.info("captured scope=%s -> %s", scope, shot)
        except Exception as exc:                     # noqa: BLE001
            logg.error("capture failed: %s", exc)
            QTimer.singleShot(200, app.quit)
            return

        def show_panel(shot_path, region, poly):
            old = state["panel"]
            if old is not None:
                old.deleteLater()
            new = ResultsPanel(shot_path, region, translate_to, poly)
            new.newCapture.connect(begin_capture)
            new.finished.connect(app.quit)
            state["panel"] = new
            new.show_results()

        if scope != "select":
            # the capture IS the selection here, so skip the overlay entirely
            logg.info("scope=%s: searching the capture directly", scope)
            show_panel(shot, (0.0, 0.0, 1.0, 1.0), None)
            return

        ov = SelectionOverlay(shot)
        state["overlay"] = ov

        def on_pick(selection):
            ov.close()
            region, poly = selection
            logg.info("picked region=%.3f,%.3f-%.3f,%.3f poly=%s",
                      *region, f"{len(poly)}pts" if poly else "none")
            show_panel(shot, region, poly)

        def on_cancel():
            logg.info("cancelled by user")
            ov.close()
            if state["panel"] is not None:
                state["panel"].show()
                state["panel"].raise_()
            else:
                app.quit()

        ov.picked.connect(on_pick)
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

    QTimer.singleShot(60, begin_capture)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()