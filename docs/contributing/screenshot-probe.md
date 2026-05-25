# UI probe

**Audience:** anyone generating or refreshing GUI screenshots, or driving
the CAPA UI through a scripted workflow (docs builds, visual regression
checks, automation scripts).
**Scope:** how to launch CAPA with the in-process probe, capture
pixel-perfect PNGs of any widget, and drive
buttons / menus / tabs / form fields / dialogs from HTTP.

## What it is

A small HTTP server ([src/capa/ui/_screenshot_probe.py](../../src/capa/ui/_screenshot_probe.py))
that loads inside the running PySide6 app and exposes `QWidget.grab()` plus
a set of `QTest`-driven interaction primitives over `127.0.0.1:9876`.

Two privilege levels, controlled by environment variables:

| Variable | Effect |
|---|---|
| `CAPA_SCREENSHOT_PROBE=1` | Loads the probe. Enables read endpoints (`/widgets`, `/actions`, `/property`, `/screenshot`). Safe at all times — no side effects. |
| `CAPA_SCREENSHOT_PROBE_INTERACTIVE=1` | *Additionally* enables write endpoints (`/click`, `/type`, `/key`, `/trigger`, `/set_tab`, `/set_property`, `/dismiss`, `/wait_for`, `/resize`, `/hover`). Must be set alongside the screenshot var; alone it does nothing. |

The HTTP server is `ThreadingHTTPServer` — concurrent requests are
supported. This matters: see [Modal dialogs and recovery](#modal-dialogs-and-recovery)
below.

Use it for: doc screenshots, visual regression checks, scripted UI walks
("open File → Open, type a path, hit Enter, screenshot the result").

Do **not** use it for: programmatic pixel readback (`widget.grab().toImage()`
in `pytest-qt` is better), or as the test harness for new features
(`pytest-qt` is the canonical UI test path; this probe is for live-app
inspection and doc tooling).

## Launch

**Read-only** (screenshots + inventory):

```powershell
$env:CAPA_SCREENSHOT_PROBE=1; uv run capa gui
```

**Full automation** (also enables click/type/etc.):

```powershell
$env:CAPA_SCREENSHOT_PROBE=1; $env:CAPA_SCREENSHOT_PROBE_INTERACTIVE=1; uv run capa gui
```

Or with a config preloaded:

```powershell
$env:CAPA_SCREENSHOT_PROBE=1; uv run capa gui configs/experiments/sim_freerun.yaml
```

The probe binds `127.0.0.1:9876` and logs `ui.screenshot_probe.started`
with the `interactive` flag on success. If the port is taken, it logs
`ui.screenshot_probe.bind_failed` and the app continues without the
probe — check the log dock if endpoints 404.

Closing the CAPA window shuts the probe down (daemon thread).

## Target resolution

Every endpoint that takes a `target` field accepts one of:

| Target | Resolves to |
|---|---|
| `"main"` / `"window"` / `""` | The visible non-Tool top-level (usually `MainWindow`). |
| `"active_dialog"` | The topmost visible top-level *other than* `main` — usually a modal dialog. |
| `"focused"` | The currently focused widget (whatever has keyboard focus). |
| `"screen"` *(screenshot only)* | A composite of all visible top-levels, preserving their on-screen positions. Use this to capture `main` with an open dialog overlaid. |
| any other string | The first widget whose `objectName` matches. |

For `objectName`-based targeting, `GET /widgets` enumerates what's
available.

## Read endpoints

### `GET /widgets`

JSON list of named widgets plus the three aliases (`main`,
`active_dialog`, `focused`). Each entry: `{objectName, class, visible}`.

```bash
curl -s http://127.0.0.1:9876/widgets
```

The output includes Qt's auto-named internals (`qt_scrollarea_*`,
`qt_spinbox_*`, etc.) — filter them out for a useful inventory:

```bash
curl -s http://127.0.0.1:9876/widgets \
  | python -c "import sys, json; print(json.dumps([w for w in json.load(sys.stdin) if not w['objectName'].startswith('qt_')], indent=2))"
```

### `GET /actions`

JSON list of `QAction`s across all top-levels. Each entry:
`{text, shortcut, enabled, checkable, checked}`. This is what `/trigger`
can fire.

```bash
curl -s http://127.0.0.1:9876/actions
```

### `GET /property?target=<t>&name=<n>`

Read a Qt property value. Useful for verifying state (e.g. is the
"Start Run" button enabled? what's the current text in the operator-id
field?).

```bash
curl -s "http://127.0.0.1:9876/property?target=dock_log&name=visible"
```

Non-JSON-serializable values come back as `repr()` strings.

### `POST /screenshot`

Body: `{"target": "<target>", "out": "<abs path>"}`. Saves PNG via
`QWidget.grab()` (or for `target: "screen"`, a composite). Returns
`{ok, path, width, height}`.

```bash
curl -s -X POST http://127.0.0.1:9876/screenshot \
  -H "Content-Type: application/json" \
  -d '{"target":"main","out":"C:/Users/gbellamy/Documents/git/capa/docs/_snippets/img/welcome.png"}'
```

Parent directories of `out` are auto-created. PNG encoding via Qt — no
Pillow / external image lib.

## Write endpoints

All require `CAPA_SCREENSHOT_PROBE_INTERACTIVE=1` at launch. Return 403
otherwise. All return `{ok, ...}` or `{ok: false, error}`.

### `POST /click`

Body: `{"target": "<objectName>"}`. Calls `click()` on `QAbstractButton`
subclasses (buttons, checkboxes, radios) for proper signal emission;
falls back to `QTest.mouseClick` for other widgets.

### `POST /type`

Body: `{"target": "<objectName>", "text": "..."}`. Focuses the widget and
emits real keystrokes via `QTest.keyClicks` — triggers validators and
`textChanged` signals per character.

Prefer `/set_property` with `name: "text"` when you need atomic
replacement without firing per-keystroke signals.

### `POST /key`

Body: `{"target": "<objectName>"?, "key": "<Qt.Key suffix>", "modifiers": ["Ctrl", "Shift", ...]?}`.

- `key` is the part after `Key_` — e.g. `"Return"`, `"Tab"`, `"Escape"`,
  `"Down"`, `"F2"`.
- `modifiers` is optional, accepts `"Ctrl"` / `"Control"`, `"Shift"`,
  `"Alt"`, `"Meta"`.
- Omit `target` to send to the active dialog (if open) or main window.

```bash
curl -s -X POST http://127.0.0.1:9876/key \
  -H "Content-Type: application/json" \
  -d '{"key":"S","modifiers":["Ctrl"]}'
```

### `POST /trigger`

Body: `{"action": "<visible text>"}`. Finds a `QAction` by visible text
across all top-levels and calls `trigger()`. Strips `&` accelerators so
`"&File"` matches `"File"`. Fails on ambiguous matches.

Right tool for menu items and toolbar actions.

> ⚠ See [Modal dialogs and recovery](#modal-dialogs-and-recovery) — actions
> that open dialogs via `exec()` will block this call until the dialog
> closes.

### `POST /set_tab`

Body: `{"target": "<QTabWidget objectName>", "tab": "<label>|<index>"}`.
Switches a `QTabWidget`. The `tab` value can be the visible label or a
numeric index (also as a string, e.g. `"0"`).

### `POST /set_property`

Body: `{"target": "<objectName>", "name": "<property>", "value": <json>}`.
Generic setter — works for any Qt property exposed by the widget
(`text`, `value`, `checked`, `currentIndex`, `enabled`, etc.). Returns
`ok: false` if the property doesn't exist or can't be set.

```bash
# Set a spinbox value
curl -s -X POST http://127.0.0.1:9876/set_property \
  -H "Content-Type: application/json" \
  -d '{"target":"max_traces_spin","name":"value","value":500}'
```

### `POST /dismiss`

No body. Sends `Escape` to the active dialog. Use this to:
- Unblock a `/trigger` that's hung on a modal `dialog.exec()`.
- Close any dialog the agent opened.

### `POST /wait_for`

Body: `{"target": "<objectName>", "condition": "visible|hidden|exists|missing", "timeout_ms": 5000?, "poll_ms": 100?}`.
Polls until the condition is met or the timeout expires.

```bash
# Wait for a dialog to appear after triggering an action
curl -s -X POST http://127.0.0.1:9876/wait_for \
  -H "Content-Type: application/json" \
  -d '{"target":"active_dialog","condition":"visible","timeout_ms":3000}'
```

### `POST /resize`

Body: `{"target": "main"?, "width": <int>, "height": <int>}`. Resizes a
top-level window. Critical for repeatable doc screenshots — different
operators have different window sizes; resize before capturing to keep
docs visually stable.

```bash
curl -s -X POST http://127.0.0.1:9876/resize \
  -H "Content-Type: application/json" \
  -d '{"target":"main","width":1400,"height":900}'
```

### `POST /hover`

Body: `{"target": "<objectName>"}`. Moves the OS cursor to the widget's
center. Use to trigger tooltip rendering before capturing.

## Modal dialogs and recovery

When an action like "Open Config..." opens its dialog via `QDialog.exec()`,
the GUI thread enters a nested event loop and **`/trigger` blocks until the
dialog closes**. This is real: the HTTP request will not return.

The probe handles this two ways:

1. **`ThreadingHTTPServer`**: concurrent requests are supported. Even with
   `/trigger` stuck, you can fire other endpoints in parallel.
2. **Nested event loops process queued cross-thread invocations**: a
   parallel `/dismiss` (or `/screenshot`, or anything else) marshals to the
   GUI thread, runs inside the dialog's nested loop, and can close the
   dialog — which unblocks the original `/trigger`.

Recommended pattern when triggering a possibly-modal action:

```bash
# 1. Fire the trigger in background — it may block until the dialog closes.
curl -s -X POST http://127.0.0.1:9876/trigger \
  -H "Content-Type: application/json" \
  -d '{"action":"Open Config..."}' &

# 2. Wait for the dialog to appear.
curl -s -X POST http://127.0.0.1:9876/wait_for \
  -H "Content-Type: application/json" \
  -d '{"target":"active_dialog","condition":"visible","timeout_ms":3000}'

# 3. Capture it (composite so main is also in frame).
curl -s -X POST http://127.0.0.1:9876/screenshot \
  -H "Content-Type: application/json" \
  -d '{"target":"screen","out":"C:/.../open-config-dialog.png"}'

# 4. Dismiss to unblock the trigger request.
curl -s -X POST http://127.0.0.1:9876/dismiss

# 5. Reap the backgrounded trigger.
wait
```

If you forget step 4 the original `/trigger` request will sit forever.
That's fine in practice (the probe is daemon-thread, the process exits
clean), but it's noisy.

## Targeting strategy

CAPA's docks all have `objectName` set (`dock_log`, `dock_events`,
`dock_manual_control`, `dock_numerics`, `dock_diagnostics`,
`dock_camera_preview`, `dock_heat_flux_tune`). These are reliable
targets.

For finer-grained targets (specific buttons, form fields, tabs inside
the Setup panel), the widget probably lacks an `objectName`. Three
strategies in order of preference:

1. **Capture the parent dock** and note the crop region for
   post-processing.
2. **Use `target: "screen"`** if the widget is on a dialog you want to
   capture along with the main window.
3. **Add `setObjectName("descriptive_name")`** in source. Keep names
   stable — docs will reference them.

Anonymous-widget targeting via traversal index is intentionally
unsupported; it would break on every layout change.

## Recipes

### Inventory then screenshot

```bash
curl -s http://127.0.0.1:9876/widgets \
  | python -c "import sys, json; [print(w['objectName'], w['class'], 'visible' if w['visible'] else 'hidden') for w in json.load(sys.stdin) if not w['objectName'].startswith('qt_')]"

curl -s -X POST http://127.0.0.1:9876/screenshot \
  -H "Content-Type: application/json" \
  -d '{"target":"dock_log","out":"C:/path/to/out.png"}'
```

### Standardized window size before capture

```bash
curl -s -X POST http://127.0.0.1:9876/resize \
  -H "Content-Type: application/json" \
  -d '{"target":"main","width":1400,"height":900}'

curl -s -X POST http://127.0.0.1:9876/screenshot \
  -H "Content-Type: application/json" \
  -d '{"target":"main","out":"C:/.../main.png"}'
```

### Batch capture every dock

```bash
for target in dock_log dock_events dock_manual_control dock_numerics dock_diagnostics; do
  curl -s -X POST http://127.0.0.1:9876/screenshot \
    -H "Content-Type: application/json" \
    -d "{\"target\":\"$target\",\"out\":\"C:/Users/gbellamy/Documents/git/capa/docs/_snippets/img/$target.png\"}"
  echo
done
```

### Verify a capture with the Read tool

After saving, `Read` the PNG to confirm framing/content before committing
it to the docs tree. The Read tool renders PNGs inline.

### Capture a tooltip

```bash
curl -s -X POST http://127.0.0.1:9876/hover \
  -H "Content-Type: application/json" \
  -d '{"target":"dock_log"}'

# Tooltips have a ~700ms delay; give Qt a moment to render.
sleep 1

curl -s -X POST http://127.0.0.1:9876/screenshot \
  -H "Content-Type: application/json" \
  -d '{"target":"screen","out":"C:/.../tooltip.png"}'
```

### Conditional flow: trigger only if button is enabled

```bash
state=$(curl -s "http://127.0.0.1:9876/property?target=start_run_button&name=enabled")
echo "$state" | python -c "import sys, json; sys.exit(0 if json.load(sys.stdin)['value'] else 1)" \
  && curl -s -X POST http://127.0.0.1:9876/click \
      -H "Content-Type: application/json" -d '{"target":"start_run_button"}'
```

## Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| Connection refused on `:9876` | Probe not started — env var unset, app not yet showing window, or port collision | Confirm `CAPA_SCREENSHOT_PROBE=1` was set in the same shell as launch; check log dock for `bind_failed` |
| `target not found: ...` | objectName misspelled, widget not yet instantiated (lazy-loaded dock), or hidden behind a tab | Run `GET /widgets`; navigate the UI to make the target visible first; use `/wait_for` after triggering actions that create widgets |
| `failed to save PNG to ...` | Path not writable, or invalid filename | Parent dirs are auto-created — check the path itself is valid and the parent is writable |
| Screenshot looks blank / wrong content | Widget exists but isn't currently rendering (collapsed dock, hidden tab) | Make the widget visible; `Widget.grab()` renders the current paint state |
| `/trigger` hangs and never returns | The action opened a dialog via `exec()` (truly modal) | Fire `/dismiss` in parallel — see [Modal dialogs and recovery](#modal-dialogs-and-recovery) |
| `403` with `"interactive endpoints disabled"` | Launched with `CAPA_SCREENSHOT_PROBE=1` but not `CAPA_SCREENSHOT_PROBE_INTERACTIVE=1` | Restart CAPA with both env vars set |
| `/click` ok but nothing visibly happens | Target is non-button; `QTest.mouseClick` hit a non-interactive spot | Confirm target is clickable; prefer `/trigger` for menu actions; `/set_tab` for tabs; `/click` for buttons |
| `/trigger` says `ambiguous action` | Multiple `QAction`s share the same text | Give the action an `objectName` in source, or trigger a more specific parent action first |
| `/wait_for` times out | The condition was never met within `timeout_ms` | Increase `timeout_ms`; verify the target spelling; check whether the action that creates the target actually fired |
| `/set_property` returns ok but value didn't update | The widget has a custom setter not exposed as a Qt property | Use the corresponding endpoint instead (`/type` for text, `/click` for checkable buttons) |

## Why HTTP + threading and not async / native MCP

- **HTTP over a custom MCP server**: zero install, zero registration, agents drive via `curl` from `Bash` or `PowerShell`. The MCP ecosystem for desktop GUI automation is immature (May 2026: published "Qt MCP" and "PyWinAuto MCP" packages are GitHub-only with READMEs that claim PyPI publication but return 404).
- **`http.server` (threaded) in a daemon thread, not qasync `asyncio.start_server`**: avoids re-implementing HTTP parsing; concurrent request handling is critical for unblocking the modal-dialog case; stdlib only.
- **`QWidget.grab()`, not screen capture**: pixel-perfect, no window-focus dance, no cursor in frame, survives DPI scaling, works on hidden-but-rendered widgets.
- **`BlockingQueuedConnection`, not signal/slot pipes**: simplest correct way to call from HTTP thread → GUI thread with a return value. The "blocked GUI thread" risk is mitigated by the threading server and `/dismiss`.

## When NOT to invoke the probe

- One-off screenshot of something specific — Windows Snipping Tool (`Win+Shift+S`) is faster and equivalent quality.
- Verifying *behavior* (does clicking X cause Y?) — `pytest-qt` is the right tool. The probe captures pixels, not state transitions.
- UI is mid-redesign — screenshots will rot. Wait until layout stabilizes.
