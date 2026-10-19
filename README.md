# kcircle

Circle to Search for KDE Plasma on Wayland — OS-level selection, results in a
small overlay. No browser window.

Android's Circle to Search is a system feature: you lasso anything on any app
and Google answers. On desktop the official equivalents are all locked inside
the browser. Chrome's Lens overlay only sees the current web page, and Gemini's
"Select from screen" is limited to Chromebook Plus, Mac and Windows.

kcircle brings the gesture to the whole desktop. Freeze the screen, draw around
anything — a PDF, a video, a terminal, a photo on your desktop — and the
results land in a compact always-on-top panel.

## Features

- **Works on any application.** Capture is compositor-level, not
  browser-level.
- **True Circle to Search.** Draw a lasso around a subject and the crop is
  masked to your stroke and trimmed tight, so Lens sees the thing you circled
  rather than the whole screen around it.
- **Animated glow** while you draw: a soft halo, a bright core, a comet tail
  and a spark that rides the cursor.
- **Real Google Lens results** — AI Overview, Exact matches, Visual matches —
  rendered in an embedded view, not a browser window.
- **Text in any language.** Scribble over a run of text and it is read by
  Google's own OCR server-side, so every script and font works and your CPU
  does nothing. The result is selectable and copyable.
- **Auto-copy** — flip `⧉` and the result lands on your clipboard the moment
  it arrives, so you can paste without reaching for the panel. Off by default,
  because silently replacing the clipboard can lose work.
- **QR codes and barcodes** are decoded locally and shown above the OCR text.
  Install the optional decoder with `uv pip install zxing-cpp` (or the `qr`
  extra); without it QR images are still searched as pictures.
- **Tap to search.** The screen is scanned while the overlay opens and the
  objects on it are outlined. A single click selects whatever is under the
  cursor — the same gesture as Circle to Search, where you tap the boots and
  scribble the bag.
- **Scribble to search.** Draw a loose loop around one thing and it snaps to
  that thing. A big deliberate lasso is always honoured exactly as drawn.
- **Free-form by default.** Circling by hand is what Circle to Search is named
  for, so that is what you get with nothing held.
- **Change shape without lifting the mouse.** Hold a modifier at any point
  mid-drag and the selection becomes exactly that shape:

  | Held | Shape |
  | --- | --- |
  | *nothing* | free-form lasso |
  | `Shift` | rectangle |
  | `Ctrl` | ellipse |
  | `Alt` | circle |
  | `Shift` in `corner` mode | square |

  Swap straight from one modifier to another mid-stroke — no need to let go of
  one first — and when several are held the order is `Alt` > `Ctrl` > `Shift`.
  Release everything and you are back to your saved shape, which is remembered
  between runs: `lasso`, `ellipse`, `circle`, `rect` or `corner`.
- **Three capture scopes** — draw a region, grab the active window, or take
  the whole screen.
- **Reusable.** The `+` button starts another capture; the panel gets out of
  the way while you select.
- **Minimize without losing the result.** The `–` button hides the panel and
  keeps the app alive with everything still loaded. It goes to the **system
  tray**, and a notification says so — a frameless always-on-top panel has no
  taskbar entry, so without that it would simply vanish. Click the tray icon
  to bring it back, or run kcircle again.
- **User-agent switch** between Google's mobile and desktop presentations,
  remembered between runs.
- **Last tab** — Text or Visual matches, reopened on the next launch.
- **Resizable and movable**, with position and size remembered.

## Tap and scribble

Circle to Search on Android never waits for you to circle something: it
segments the screen the instant it freezes, then just checks your touch
against that pre-computed grid. kcircle does the same thing, locally.

While the overlay is opening, a background thread reduces the frozen frame to
a small binary edge map and runs recursive projection (XY-cut) layout analysis
on it — the same classic technique used to segment scanned documents. It cuts
the screen on empty gutters until the regions stop dividing, then keeps the
leaves that look like real things on it. Every pixel-level step runs inside
Pillow's C code; Python only walks a few hundred projection entries, so the
whole pass is a few tens of milliseconds on a 1080p capture.

Deliberately, it is a heuristic and not a neural network:

- No model, no download, no GPU, and **no new dependencies** — it uses the
  Pillow that was already there.
- When nothing is found (a gradient, a video, a dense wall of text) it says
  so quietly. A click then falls back to a small box around the cursor, which
  is a perfectly good search — you are never blocked.
- Snapping is deliberately conservative: it only fires when the stroke is
  small, exactly one object is inside it, and that object is clearly smaller
  than the stroke. Otherwise your lasso is used verbatim.

Turn it off with `KCIRCLE_NO_SNAP=1`, or from the `◎ tap` button in the panel.

Text is never OCR'd here. Draw over text and the crop goes to Google's own
endpoint via `chrome-lens-py`, exactly like the rest of the app, so all
languages and fonts are handled by the service that owns them.

## Scaled displays and screen coverage

If the overlay ever failed to cover the whole screen — leaving a live strip
along the bottom with a second copy of the panel visible under it — this is
why, and what is done about it.

Qt and `spectacle` count in different units. Qt talks in device-independent
coordinates: on a 1920x1080 panel running at 1.2x, the desktop is 1600x900 as
far as Qt is concerned. `spectacle` captures real device pixels: 1920x1080.
Mixing the two offsets everything by exactly the scale factor.

kcircle therefore never uses Qt's reported `devicePixelRatio` (which is not
reliable under Wayland/XWayland — it reports 2.0 here, which is simply wrong).
Instead it measures the ratio between the two things it actually has:

```
ratio = captured image width / virtual desktop width      # 1920 / 1600 = 1.2
```

Three things follow from that:

- **The launcher prefers Wayland** when `WAYLAND_DISPLAY` is set, so Qt and
  the screenshotter share one display server and one coordinate system.
- **The overlay size is pinned** with `setFixedSize()`. Merely asking a window
  to cover the screen can come back a few pixels short on a scaled output;
  fixing min and max to the same value makes the compositor honour it. This is
  the same trick NormCap uses.
- **The pointer** is hidden only while the overlay is swapping to the frozen
  frame, then comes straight back as a crosshair. You cannot draw around
  something you cannot see yourself pointing at, so it is deliberately not
  hidden for the whole session. Set `KCIRCLE_NO_SIZE_LOCK=1` to opt out of the
  size pin.

This is a scale bug, not a multi-monitor one — it happens on a single display
just as easily.

## Capture scopes

| Scope | Behaviour |
| --- | --- |
| `select` | freeze the screen, then draw a region |
| `window` | the focused window only, no drawing step |
| `screen` | the entire screen, no drawing step |

Switch with the **Scope** button. Remembered between runs.

## Selection shapes

| Shape | Gesture |
| --- | --- |
| `corner` | drag corner to corner; hold <kbd>Shift</kbd> to constrain to a square |
| `rect` | plain drag |
| `lasso` | draw freehand around the subject |
| `ellipse` | drag an ellipse |

Switch with the **Shape** button on the panel. The choice is written to
`settings.json` and restored next launch, so the mode you last used is the one
you get back.

Mid-drag overrides:

| Key | Effect |
| --- | --- |
| <kbd>Shift</kbd> | square (in `corner`) · lasso (anywhere else) |
| <kbd>Alt</kbd> | ellipse |

A lasso selection is masked to your stroke before it is sent, so Lens sees
the circled subject rather than its bounding box. The crop is trimmed tight to
the drawn area and everything outside the stroke stays **transparent** — set
`KCIRCLE_CROP_BG=white` if you need a solid background instead. Shapes without
a mask (`rect`, `corner`, `ellipse`) send their bounding box as plain RGB.

## Why two tabs

Google serves a different layout per user agent, and the useful ones trade off:

| UA | Result set | Layout |
| --- | --- | --- |
| `mobile` | often thin — Google tends to answer with location and local junk | fluid |
| `desktop` | **richer** — real AI Overview and visual matches | fixed ~652px column |

kcircle ships `desktop` and fits it to the panel using the browser's own page
zoom: when the panel is narrower than Google's fixed column, the page is scaled
down so the column fits, and **Chromium does all the wrapping and re-wrapping
itself**. Resizing re-fits on the fly. This is deliberately the opposite of
overriding Google's CSS — an earlier version carried ~70 lines of `!important`
rules to force the layout, which fought the page while it was still loading and
made results appear and then vanish. The only CSS kcircle injects now hides
Google's own header and search box, which are never results.

If desktop results look cramped, widen the panel — or switch to `mobile`, which
reflows natively but often returns less.

## Install

Requires KDE Plasma 6 on Wayland and [`uv`](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/athulkrishna2015/kcircle.git
cd kcircle
uv run ./kcircle
```

That is the whole install. `uv` reads `pyproject.toml`, builds the
environment on first run, and launches it. There is nothing to activate.

To create the environment up front and reuse it:

```bash
uv venv
uv pip install -r requirements.txt
./kcircle
```

`./kcircle` is a thin launcher that execs the checkout's own `.venv`.

### Dependencies

| Package | Why |
| --- | --- |
| `PyQt6` | overlay, panel, glow animation, masking |
| `PyQt6-WebEngine` | renders the Google Lens results page |
| `chrome-lens-py` | Lens endpoint client for OCR and translation |

`Pillow` comes in as a `chrome-lens-py` dependency and is what draws the lasso
alpha mask. At runtime you also need `spectacle`, which ships with Plasma.

### A note on size

The environment is roughly **560 MB**, and nearly all of it is QtWebEngine:
`libQt6WebEngineCore.so` alone is ~194 MB, because the wheel bundles a full
Chromium.

If your distribution already ships PyQt6 and QtWebEngine (Arch does:
`python-pyqt6` and `qt6-webengine`), you can build a venv that reuses them and
drop to about **32 MB**:

```bash
uv venv --system-site-packages
uv pip install chrome-lens-py
```

Be aware that some distro combinations are mismatched — Arch currently pairs
PyQt6 6.11.0 with Qt 6.11.2, and `QWebEngineView` aborts on load. kcircle
probes WebEngine in a subprocess at startup, so this failure degrades to
OCR-only instead of taking the app down.

To upgrade later:

```bash
uv pip install --upgrade -r requirements.txt
```

```bash
./kcircle
```

1. The screen freezes and dims.
2. Draw around what you want — box, ellipse, square or lasso. Right-click or
   `Esc` cancels.
3. Results appear in the panel, image search already open. Drag the title to
   move, grab the bottom-right corner to resize, `Esc` to close.
4. Press `+` for another capture — the panel hides while you select.

The header carries `+`, the two tab buttons, a **URL bar** showing the current
Lens result (selectable, copyable, and editable — type a URL and press Enter to
navigate), and `×`. The buttons along the bottom cycle **Shape**, **UA** and
**Scope**, and copy the OCR text to the clipboard. `Esc` closes.

Bind a shortcut in *System Settings → Shortcuts → Custom Shortcuts*:

```
Command: /path/to/kcircle/kcircle
```

## Configuration

`~/.config/kcircle/`

| File | Purpose |
| --- | --- |
| `settings.json` | `{"ua": "desktop"\|"mobile", "mode": "lasso"\|"ellipse"\|"circle"\|"rect"\|"corner", "scope": "select"\|"window"\|"screen", "snap": true, "autocopy": false}` |
| `geometry.json` | panel position and size |
| `kcircle.log` | debug log, see below |
| `run.pid` | PID of the running instance, so a second launch restores it |

All are written from the panel buttons, so you rarely need to edit them.

Environment:

| Variable | Effect |
| --- | --- |
| `KCIRCLE_TRANSLATE` | target language code, e.g. `de`, `fr` |
| `KCIRCLE_LOG` | `debug` for verbose logging, or a log file path |
| `KCIRCLE_CROP_BG` | `white` to flatten a lasso crop onto an opaque background; the default keeps it transparent |
| `KCIRCLE_REPROBE` | `1` to re-run the QtWebEngine health check |
| `KCIRCLE_NO_BYPASS_WM` | `1` to drop `BypassWindowManagerHint` from the capture overlay |
| `KCIRCLE_NO_SNAP` | `1` to disable the tap and scribble-to-search detection |

Debug logging records the selection region, the crop size, how much of the
upload the mask covers, and which file is sent to Google — enough to diagnose
a selection that comes back wrong:

```bash
KCIRCLE_LOG=debug ./kcircle
tail -f ~/.config/kcircle/kcircle.log
```

## How it works

1. `spectacle` captures the screen to a PNG. Freezing first sidesteps
   Wayland's ban on live screen capture by third-party clients.
2. A frameless PyQt6 overlay draws the frozen frame, animates the glow, and
   collects the drag as a rectangle, ellipse or lasso path.
3. The selection is cropped locally and, for a lasso, alpha-masked to the
   stroke, so **only the pixels you actually selected are transmitted**.
4. The crop goes to `chrome-lens-py` for OCR and translation, and separately
   to `lens.google.com/v3/upload` for the full search results.
5. Those results render in a `QWebEngineView`, page-zoomed to fit the panel.

A note on moving the window: on Wayland a client cannot set its own toplevel
position, so kcircle uses Qt's `startSystemMove()` and lets KWin drive the
drag. Resizing uses a custom corner grip for the same reason.

## Installing to your system

To run it from the app launcher rather than a terminal, install it somewhere
permanent and register a desktop entry.

```bash
# 1. put it where it will live
mkdir -p ~/.local/share/kcircle
git clone https://github.com/athulkrishna2015/kcircle.git \
    ~/.local/share/kcircle/repo

# 2. build the environment
cd ~/.local/share/kcircle/repo
uv venv
uv pip install -r requirements.txt
```

Install the icon at every size the launcher wants:

```bash
cd ~/.local/share/kcircle/repo
for s in 16 22 32 48 64 128 256; do
  mkdir -p ~/.local/share/icons/hicolor/${s}x${s}/apps
  rsvg-convert -w $s -h $s kcircle.svg \
    -o ~/.local/share/icons/hicolor/${s}x${s}/apps/kcircle.png
done
mkdir -p ~/.local/share/icons/hicolor/scalable/apps
cp kcircle.svg ~/.local/share/icons/hicolor/scalable/apps/
gtk-update-icon-cache -f -t ~/.local/share/icons/hicolor
```

Add the launcher entry — note the **absolute** path, since the launcher does
not inherit your shell's `PATH`:

```bash
mkdir -p ~/.local/share/applications
cat > ~/.local/share/applications/kcircle.desktop <<'EOF'
[Desktop Entry]
Type=Application
Name=Circle to Search
GenericName=Screen region search
Comment=Draw around anything on screen and search it with Google Lens
Exec=/home/YOU/.local/share/kcircle/repo/kcircle
Icon=kcircle
Terminal=false
Categories=Utility;
Keywords=circle;search;lens;ocr;translate;crop;screenshot;
StartupNotify=true
X-KDE-DBUS-Restricted-Interfaces=org.kde.KWin.ScreenShot2
EOF
update-desktop-database ~/.local/share/applications
```

Then bind a shortcut in *System Settings → Shortcuts → Custom Shortcuts*:

```
Command: /home/YOU/.local/share/kcircle/repo/kcircle
```

The `X-KDE-DBUS-Restricted-Interfaces` line is only needed if you later switch
capture to KWin's own `org.kde.KWin.ScreenShot2` API; the default `spectacle`
path does not require it.

The shipped `kcircle.desktop` sets `StartupNotify=false`. With start-up feedback
on, Plasma pops an animated icon at the pointer every time the overlay opens,
which looks like a stray cursor artefact. If you wrote the desktop entry by
hand and see that icon, add the line.

## Tests

A local matrix covers every scope, shape and user-agent combination, and
asserts the invariants that have broken in the past: a selection never uploads
the whole screenshot, the visual tab never uploads before the crop exists, and
the stylesheet never overrides Google's result layout.

```bash
.venv/bin/python tests/test_matrix.py
```

## Credits and provenance

Written from scratch. Attribution:

- **[chrome-lens-py](https://github.com/bropines/chrome-lens-py)** (MIT) — pip
  dependency; the client for Google's Lens Protobuf endpoint used for OCR and
  translation.
- **[jaslrobinson/circle-to-search](https://github.com/jaslrobinson/circle-to-search)** (MIT)
  and **[dinossht/lens-desktop](https://github.com/dinossht/lens-desktop)** —
  prior art read during research. The "freeze → draw → search" concept and the
  idea of showing results in a panel rather than a browser came from them. No
  code was copied from either.

PyQt6/Qt are LGPLv3; chrome-lens-py is MIT. Google Lens is a Google service
with its own terms — kcircle sends your selection to Google the same way the
official feature does.

## Known limitations

- Wayland only. No X11 support.
- Capture depends on `spectacle`, so it is Plasma-specific.
- Results require network access; OCR is the only offline-capable path.
- Google's endpoints are unofficial and may change without notice.
- The selection is a rectangle, ellipse or freehand lasso — not the pressure-
  or curvature-aware stroke of the Android original.
- The Visual matches tab needs QtWebEngine. If the bundled engine is missing
  or crashes (some distro Qt/PyQt6 combinations abort on load), kcircle detects
  that in a subprocess at startup and falls back to OCR-only rather than
  crashing.
- **There are no screenshots in this repository, by design.** A screen capture
  of the results panel leaked this machine's geolocation. `docs/` and all image
  types are gitignored, and the image generator writes to `/tmp` — so a
  screenshot of your own session can never be committed by accident.

## License

MIT