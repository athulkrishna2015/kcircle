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

## Screenshots

The results panel, showing a live Google Lens search rendered in place:

![kcircle results panel](docs/panel.png)

The animated glow that follows your stroke while you draw:

![the selection glow](docs/glow.png)

The overlay while drawing — the screen dims, the selection is punched back to
full brightness, and the glow trails your stroke:

![the selection overlay](docs/overlay.png)

`corner` mode with <kbd>Shift</kbd> held, constraining the drag to a square:

![square selection](docs/overlay-square.png)

What each shape actually sends to Google — note that only the lasso trims
everything outside the stroke to white:

![the crops that get uploaded](docs/crops.png)

```
╭─ kcircle ──────────────────────────────╮
│   Circle to Search                     │  ← drag to move
│ ┌────────┬───────────────┐             │
│ │  Text  │ Visual matches│             │  ← image search is default
│ ├────────┴───────────────┤             │
│ │  AI Overview       🔊  │             │
│ │  "The image shows    │             │  ← real Lens results
│ │   an abstract 3D…"    │             │
│ │  ┌────┐               │             │
│ │  │All │Exact│Visual    │             │
│ │  └────┘               │             │
│ └───────────────────────┘             │
│  [UA: desktop] [Shape: ◯ lasso]    ╲   │
╰─ Esc close · drag to move ────────────╯
```

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
- **Instant OCR** on the Text tab, no browser engine involved.
- **Four selection shapes** — `corner`, `rect`, `lasso`, `ellipse` — with the
  choice remembered between runs. Hold Shift in `corner` mode for a square.
- **Three capture scopes** — draw a region, grab the active window, or take
  the whole screen.
- **Reusable.** The `+` button starts another capture; the panel gets out of
  the way while you select.
- **User-agent switch** between Google's mobile and desktop presentations,
  remembered between runs.
- **Resizable and movable**, with position and size remembered.

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

A lasso selection is alpha-masked to your stroke before it is sent, so Lens
sees the circled subject rather than its bounding box.

## Why two tabs

Google serves a different layout per user agent, and the useful ones trade off:

| UA | Result set | Layout |
| --- | --- | --- |
| `mobile` | often thin — Google tends to answer with location and local junk | reflows natively |
| `desktop` | **richer** — real AI Overview and visual matches | fixed ~652px column |

kcircle ships `desktop` by default and injects CSS to reflow it to the panel
width, including collapsing the desktop visual-matches grid into `auto-fill`
columns and hiding Google's search box. That injection is re-applied whenever
the panel is resized, so the layout tracks the window instead of staying
stranded at the original column width.

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
| `chrome-lens-py` | Lens endpoint client for OCR, translation, labels |

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

The four buttons along the bottom cycle **Shape**, **UA** and **Scope**, and
copy text to the clipboard. `+` starts a new capture, `×` closes.

Bind a shortcut in *System Settings → Shortcuts → Custom Shortcuts*:

```
Command: /path/to/kcircle/kcircle
```

## Configuration

`~/.config/kcircle/`

| File | Purpose |
| --- | --- |
| `settings.json` | `{"ua": "desktop"\|"mobile", "mode": "corner"\|"rect"\|"lasso"\|"ellipse", "scope": "select"\|"window"\|"screen"}` |
| `geometry.json` | panel position and size |
| `kcircle.log` | debug log, see below |

All are written from the panel buttons, so you rarely need to edit them.

Environment:

| Variable | Effect |
| --- | --- |
| `KCIRCLE_TRANSLATE` | target language code, e.g. `de`, `fr` |
| `KCIRCLE_LOG` | `debug` for verbose logging, or a log file path |
| `KCIRCLE_BYPASS_WM` | `1` to add `BypassWindowManagerHint` to the overlay |

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
4. The crop goes to `chrome-lens-py` for OCR, translation and object labels,
   and separately to `lens.google.com/v3/upload` for the full search results.
5. Those results render in a `QWebEngineView` with Google chrome stripped and
   the layout collapsed to the panel width.

A note on moving the window: on Wayland a client cannot set its own toplevel
position, so kcircle uses Qt's `startSystemMove()` and lets KWin drive the
drag. Resizing uses a custom corner grip for the same reason.

## Tests

A local matrix covers every scope, shape and user-agent combination, and
asserts the two invariants that have broken in the past: a selection never
uploads the whole screenshot, and the visual tab never uploads before the crop
exists.

```bash
.venv/bin/python tests/test_matrix.py
```

The documentation images are generated from a synthetic scene rather than a
real screen, so they never leak whatever happens to be on your display:

```bash
.venv/bin/python tests/make_docs_images.py
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

## License

MIT