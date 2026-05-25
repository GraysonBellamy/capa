"""Dev-only HTTP probe for capturing screenshots of and driving the running GUI.

Enabled by setting ``CAPA_SCREENSHOT_PROBE=1`` before launching the GUI;
otherwise this module is never imported and has zero runtime cost.

Two privilege levels, set by environment variable:

* ``CAPA_SCREENSHOT_PROBE=1`` — read-only endpoints. Safe at all times.
* ``CAPA_SCREENSHOT_PROBE_INTERACTIVE=1`` *(also requires the above)* —
  enables write endpoints that can fire real UI actions.

Listens on ``127.0.0.1:9876``.

Target resolution:

* ``"main"`` / ``"window"`` / ``""`` → the visible non-Tool top-level window
  (typically ``MainWindow``).
* ``"active_dialog"`` → the topmost visible top-level widget *other than*
  ``main`` — usually a modal dialog.
* ``"focused"`` → the currently focused widget.
* anything else → the first widget whose ``objectName`` matches.

Endpoints:

Read:

* ``GET  /widgets``                                    list named widgets
* ``GET  /actions``                                    list QActions across all top-levels
* ``GET  /property?target=<t>&name=<n>``               read a Qt property value

Write (require ``CAPA_SCREENSHOT_PROBE_INTERACTIVE=1``):

* ``POST /screenshot {target, out}``                   PNG of widget/screen/dialog
* ``POST /click {target}``                             click button/checkbox/radio (or QTest fallback)
* ``POST /type {target, text}``                        emit real keystrokes
* ``POST /key {target?, key, modifiers?}``             single key press; ``modifiers`` is a list
                                                       of ``"Ctrl"|"Shift"|"Alt"|"Meta"``
* ``POST /trigger {action}``                           fire QAction by visible text
* ``POST /set_tab {target, tab}``                      switch QTabWidget to label or index
* ``POST /set_property {target, name, value}``         set an arbitrary Qt property
* ``POST /dismiss``                                    Escape the active dialog
* ``POST /wait_for {target, condition, timeout_ms?}``  poll until ``visible|hidden|exists|missing``
* ``POST /resize {target?, width, height}``            resize a top-level window
* ``POST /hover {target}``                             move mouse cursor over widget (tooltip trigger)

All Qt operations marshal to the GUI thread via ``QMetaObject.invokeMethod``
with a blocking queued connection. The HTTP server runs in a daemon thread.

**Hang risk:** if a triggered action calls ``QDialog.exec()`` (truly modal,
thread-blocking), the GUI thread blocks until the dialog closes and the
probe will appear frozen. CAPA's built-in actions all use ``show()`` so
this is not a current concern, but be aware when wiring new actions.
"""

from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import structlog
from PySide6.QtCore import (
    Q_ARG,
    Q_RETURN_ARG,
    QMetaObject,
    QObject,
    QPoint,
    Qt,
    Slot,
)
from PySide6.QtGui import QAction, QCursor, QPainter, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractButton, QApplication, QMainWindow, QTabWidget, QWidget

_PORT = 9876
_logger = structlog.get_logger("capa.ui.screenshot_probe")

_MODIFIER_MAP = {
    "Ctrl": Qt.KeyboardModifier.ControlModifier,
    "Control": Qt.KeyboardModifier.ControlModifier,
    "Shift": Qt.KeyboardModifier.ShiftModifier,
    "Alt": Qt.KeyboardModifier.AltModifier,
    "Meta": Qt.KeyboardModifier.MetaModifier,
}


def _app() -> QApplication | None:
    # QApplication.instance() is typed as QCoreApplication | None, but at
    # runtime in a GUI process it's always a QApplication. The isinstance
    # check narrows the type for mypy.
    app = QApplication.instance()
    return app if isinstance(app, QApplication) else None


def _visible_top_levels() -> list[QWidget]:
    app = _app()
    if app is None:
        return []
    return [w for w in app.topLevelWidgets() if w.isVisible()]


def _main_window() -> QWidget | None:
    # Prefer an actual QMainWindow — without this, opening a modal dialog
    # (HardwareInitDialog, etc.) can leave the dialog as the first non-Tool
    # top-level returned by Qt and ``target=main`` then resolves to the
    # dialog. Fall back to the first non-Tool top-level if no QMainWindow
    # is visible (e.g. very early startup before MainWindow has shown).
    tops = _visible_top_levels()
    for w in tops:
        if isinstance(w, QMainWindow):
            return w
    for w in tops:
        if w.windowType() != Qt.WindowType.Tool:
            return w
    return None


def _active_dialog() -> QWidget | None:
    main = _main_window()
    candidates = [w for w in _visible_top_levels() if w is not main]
    if not candidates:
        return None
    # Heuristic: prefer the topmost-stacked window (last in stacking order is
    # frontmost on most platforms). Qt doesn't expose stacking order directly,
    # so use the active window first then fall back to the last visible top-level.
    app = _app()
    if app is not None:
        active = app.activeWindow()
        if active is not None and active is not main and active in candidates:
            return active
    return candidates[-1]


def _find_by_name(target: str) -> QWidget | None:
    app = _app()
    if app is None:
        return None
    for w in app.allWidgets():
        if w.objectName() == target:
            return w
    return None


def _resolve_target(target: str) -> QWidget | None:
    if target in ("main", "window", ""):
        return _main_window()
    if target == "active_dialog":
        return _active_dialog()
    if target == "focused":
        app = _app()
        return app.focusWidget() if app is not None else None
    return _find_by_name(target)


def _strip_accel(text: str) -> str:
    return text.replace("&", "")


def _ensure_parent(path: str) -> None:
    parent = Path(path).parent
    if parent and not parent.exists():
        parent.mkdir(parents=True, exist_ok=True)


def _compose_screen() -> QPixmap | None:
    """Composite every visible top-level widget into a single pixmap, preserving
    their relative on-screen positions. Useful for capturing main + open dialog.

    Top-levels are painted in z-order so dialogs always sit on top of
    the main window: ``QApplication.topLevelWidgets()`` returns them in
    construction order, which means an early-constructed main window
    would otherwise be painted *over* a dialog opened later, hiding it
    in the composite.
    """
    tops = _visible_top_levels()
    if not tops:
        return None
    main = _main_window()
    app = _app()
    active = app.activeWindow() if app is not None else None
    # Paint order: main first, then dialogs (non-main), with the
    # active window last so it ends up on top.
    others = [w for w in tops if w is not main]
    others_sorted = [w for w in others if w is not active] + (
        [active] if active is not None and active in others else []
    )
    ordered = ([main] if main is not None else []) + others_sorted
    grabs: list[tuple[QPoint, QPixmap]] = [(w.pos(), w.grab()) for w in ordered]
    xs = [p.x() for p, _ in grabs]
    ys = [p.y() for p, _ in grabs]
    rights = [p.x() + pm.width() for p, pm in grabs]
    bottoms = [p.y() + pm.height() for p, pm in grabs]
    min_x, min_y = min(xs), min(ys)
    width = max(rights) - min_x
    height = max(bottoms) - min_y
    canvas = QPixmap(width, height)
    canvas.fill(Qt.GlobalColor.transparent)
    painter = QPainter(canvas)
    try:
        for pos, pm in grabs:
            painter.drawPixmap(pos.x() - min_x, pos.y() - min_y, pm)
    finally:
        painter.end()
    return canvas


class _Probe(QObject):
    @Slot(str, str, result=str)
    def grab(self, target: str, out_path: str) -> str:
        try:
            _ensure_parent(out_path)
        except OSError as exc:
            return json.dumps({"ok": False, "error": f"cannot create parent dir: {exc}"})
        if target == "screen":
            pixmap = _compose_screen()
            if pixmap is None:
                return json.dumps({"ok": False, "error": "no visible top-level widgets"})
        else:
            widget = _resolve_target(target)
            if widget is None:
                return json.dumps({"ok": False, "error": f"target not found: {target!r}"})
            try:
                pixmap = widget.grab()
            except Exception as exc:
                return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        if not pixmap.save(out_path, "PNG"):
            return json.dumps({"ok": False, "error": f"failed to save PNG to {out_path!r}"})
        return json.dumps(
            {"ok": True, "path": out_path, "width": pixmap.width(), "height": pixmap.height()}
        )

    @Slot(result=str)
    def list_widgets(self) -> str:
        app = _app()
        if app is None:
            return json.dumps([])
        out: list[dict[str, Any]] = [
            {"objectName": "main", "class": "<alias: top-level window>", "visible": True},
            {
                "objectName": "active_dialog",
                "class": "<alias: topmost non-main top-level>",
                "visible": _active_dialog() is not None,
            },
            {
                "objectName": "focused",
                "class": "<alias: focused widget>",
                "visible": app.focusWidget() is not None,
            },
        ]
        for w in app.allWidgets():
            name = w.objectName()
            if name:
                out.append(
                    {"objectName": name, "class": type(w).__name__, "visible": w.isVisible()}
                )
        return json.dumps(out)

    @Slot(result=str)
    def list_actions(self) -> str:
        app = _app()
        if app is None:
            return json.dumps([])
        seen: set[int] = set()
        out: list[dict[str, Any]] = []
        for w in app.topLevelWidgets():
            for action in w.findChildren(QAction):
                if id(action) in seen:
                    continue
                seen.add(id(action))
                text = _strip_accel(action.text()).strip()
                if not text:
                    continue
                out.append(
                    {
                        "text": text,
                        "shortcut": action.shortcut().toString() or None,
                        "enabled": action.isEnabled(),
                        "checkable": action.isCheckable(),
                        "checked": action.isChecked() if action.isCheckable() else None,
                    }
                )
        return json.dumps(out)

    @Slot(str, str, result=str)
    def get_property(self, target: str, name: str) -> str:
        widget = _resolve_target(target)
        if widget is None:
            return json.dumps({"ok": False, "error": f"target not found: {target!r}"})
        try:
            value = widget.property(name)
        except Exception as exc:
            return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        try:
            json.dumps(value)  # check serializable
            serialized: Any = value
        except (TypeError, ValueError):
            serialized = repr(value)
        return json.dumps({"ok": True, "target": target, "name": name, "value": serialized})

    @Slot(str, str, str, result=str)
    def set_property(self, target: str, name: str, value_json: str) -> str:
        widget = _resolve_target(target)
        if widget is None:
            return json.dumps({"ok": False, "error": f"target not found: {target!r}"})
        try:
            value = json.loads(value_json)
        except json.JSONDecodeError as exc:
            return json.dumps({"ok": False, "error": f"value not JSON: {exc}"})
        try:
            if not widget.setProperty(name, value):
                return json.dumps(
                    {
                        "ok": False,
                        "error": f"setProperty({name!r}, ...) returned False (property may not exist)",
                    }
                )
        except Exception as exc:
            return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        return json.dumps({"ok": True})

    @Slot(str, result=str)
    def click(self, target: str) -> str:
        widget = _resolve_target(target)
        if widget is None:
            return json.dumps({"ok": False, "error": f"target not found: {target!r}"})
        try:
            if isinstance(widget, QAbstractButton):
                widget.click()
            else:
                QTest.mouseClick(widget, Qt.MouseButton.LeftButton)
        except Exception as exc:
            return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        return json.dumps({"ok": True})

    @Slot(str, str, result=str)
    def type_text(self, target: str, text: str) -> str:
        widget = _resolve_target(target)
        if widget is None:
            return json.dumps({"ok": False, "error": f"target not found: {target!r}"})
        try:
            widget.setFocus()
            QTest.keyClicks(widget, text)
        except Exception as exc:
            return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        return json.dumps({"ok": True})

    @Slot(str, str, str, result=str)
    def key_press(self, target: str, key_name: str, modifiers_json: str) -> str:
        widget = _resolve_target(target) if target else (_active_dialog() or _main_window())
        if widget is None:
            return json.dumps({"ok": False, "error": f"target not found: {target!r}"})
        key = getattr(Qt.Key, f"Key_{key_name}", None)
        if key is None:
            return json.dumps({"ok": False, "error": f"unknown key: {key_name!r}"})
        try:
            mod_names: list[str] = json.loads(modifiers_json) if modifiers_json else []
        except json.JSONDecodeError as exc:
            return json.dumps({"ok": False, "error": f"modifiers not JSON: {exc}"})
        mods = Qt.KeyboardModifier.NoModifier
        for name in mod_names:
            mapped = _MODIFIER_MAP.get(name)
            if mapped is None:
                return json.dumps({"ok": False, "error": f"unknown modifier: {name!r}"})
            mods |= mapped
        try:
            widget.setFocus()
            QTest.keyClick(widget, key, mods)
        except Exception as exc:
            return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        return json.dumps({"ok": True})

    @Slot(str, result=str)
    def trigger_action(self, action_text: str) -> str:
        app = _app()
        if app is None:
            return json.dumps({"ok": False, "error": "no QApplication"})
        wanted = action_text.strip()
        seen: set[int] = set()
        matches: list[QAction] = []
        for w in app.topLevelWidgets():
            for action in w.findChildren(QAction):
                if id(action) in seen:
                    continue
                seen.add(id(action))
                if _strip_accel(action.text()).strip() == wanted:
                    matches.append(action)
        if not matches:
            return json.dumps({"ok": False, "error": f"action not found: {action_text!r}"})
        if len(matches) > 1:
            return json.dumps(
                {"ok": False, "error": f"ambiguous action {action_text!r}: {len(matches)} matches"}
            )
        try:
            matches[0].trigger()
        except Exception as exc:
            return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        return json.dumps({"ok": True})

    @Slot(str, str, result=str)
    def set_tab(self, target: str, tab: str) -> str:
        widget = _resolve_target(target)
        if not isinstance(widget, QTabWidget):
            return json.dumps({"ok": False, "error": f"target {target!r} is not a QTabWidget"})
        if tab.isdigit():
            idx = int(tab)
            if not (0 <= idx < widget.count()):
                return json.dumps({"ok": False, "error": f"tab index {idx} out of range"})
            widget.setCurrentIndex(idx)
            return json.dumps({"ok": True, "index": idx, "label": widget.tabText(idx)})
        for i in range(widget.count()):
            if _strip_accel(widget.tabText(i)).strip() == tab.strip():
                widget.setCurrentIndex(i)
                return json.dumps({"ok": True, "index": i, "label": widget.tabText(i)})
        labels = [widget.tabText(i) for i in range(widget.count())]
        return json.dumps({"ok": False, "error": f"tab {tab!r} not found; available: {labels}"})

    @Slot(result=str)
    def dismiss(self) -> str:
        dialog = _active_dialog()
        if dialog is None:
            return json.dumps({"ok": False, "error": "no active dialog"})
        try:
            QTest.keyClick(dialog, Qt.Key.Key_Escape)
        except Exception as exc:
            return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        return json.dumps({"ok": True})

    @Slot(str, int, int, result=str)
    def resize(self, target: str, width: int, height: int) -> str:
        widget = _resolve_target(target)
        if widget is None:
            return json.dumps({"ok": False, "error": f"target not found: {target!r}"})
        if not widget.isWindow():
            return json.dumps(
                {"ok": False, "error": f"target {target!r} is not a top-level window"}
            )
        try:
            widget.resize(width, height)
        except Exception as exc:
            return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        size = widget.size()
        return json.dumps({"ok": True, "width": size.width(), "height": size.height()})

    @Slot(str, result=str)
    def hover(self, target: str) -> str:
        widget = _resolve_target(target)
        if widget is None:
            return json.dumps({"ok": False, "error": f"target not found: {target!r}"})
        try:
            global_pt = widget.mapToGlobal(widget.rect().center())
            QCursor.setPos(global_pt)
        except Exception as exc:
            return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        return json.dumps({"ok": True, "x": global_pt.x(), "y": global_pt.y()})

    @Slot(str, str, result=str)
    def check_condition(self, target: str, condition: str) -> str:
        """Single condition check; the HTTP-side loop drives polling."""
        widget = _resolve_target(target)
        exists = widget is not None
        visible = widget is not None and widget.isVisible()
        result = {
            "visible": visible,
            "hidden": exists and not visible,
            "exists": exists,
            "missing": not exists,
        }.get(condition)
        if result is None:
            return json.dumps({"ok": False, "error": f"unknown condition: {condition!r}"})
        return json.dumps({"ok": True, "met": result, "exists": exists, "visible": visible})


_probe: _Probe | None = None
_server: ThreadingHTTPServer | None = None
_interactive: bool = False


def _call(method: str, *args: tuple[type, Any]) -> str:
    assert _probe is not None
    q_args = [Q_ARG(t, v) for (t, v) in args]
    return str(
        QMetaObject.invokeMethod(
            _probe,
            method,
            Qt.ConnectionType.BlockingQueuedConnection,
            Q_RETURN_ARG(str),
            *q_args,
        )
    )


def _wait_for(body: dict[str, Any]) -> str:
    target = body["target"]
    condition = body.get("condition", "visible")
    timeout_ms = int(body.get("timeout_ms", 5000))
    poll_ms = max(25, int(body.get("poll_ms", 100)))
    deadline = time.monotonic() + timeout_ms / 1000.0
    last: dict[str, Any] = {}
    while True:
        last = json.loads(_call("check_condition", (str, target), (str, condition)))
        if not last.get("ok"):
            return json.dumps(last)
        if last.get("met"):
            return json.dumps(
                {
                    "ok": True,
                    "waited_ms": int(timeout_ms - max(0, (deadline - time.monotonic()) * 1000)),
                }
            )
        if time.monotonic() >= deadline:
            return json.dumps(
                {
                    "ok": False,
                    "error": f"timeout after {timeout_ms}ms; condition {condition!r} on {target!r} not met",
                    "last": last,
                }
            )
        time.sleep(poll_ms / 1000.0)


def _h_screenshot(body: dict[str, Any]) -> str:
    target = body.get("target", "main")
    out = body.get("out", "")
    if not out:
        return json.dumps({"ok": False, "error": "missing 'out'"})
    return _call("grab", (str, target), (str, out))


def _h_click(body: dict[str, Any]) -> str:
    return _call("click", (str, body["target"]))


def _h_type(body: dict[str, Any]) -> str:
    return _call("type_text", (str, body["target"]), (str, body["text"]))


def _h_key(body: dict[str, Any]) -> str:
    return _call(
        "key_press",
        (str, body.get("target", "")),
        (str, body["key"]),
        (str, json.dumps(body.get("modifiers", []))),
    )


def _h_trigger(body: dict[str, Any]) -> str:
    return _call("trigger_action", (str, body["action"]))


def _h_set_tab(body: dict[str, Any]) -> str:
    return _call("set_tab", (str, body["target"]), (str, str(body["tab"])))


def _h_set_property(body: dict[str, Any]) -> str:
    return _call(
        "set_property",
        (str, body["target"]),
        (str, body["name"]),
        (str, json.dumps(body["value"])),
    )


def _h_dismiss(body: dict[str, Any]) -> str:
    return _call("dismiss")


def _h_resize(body: dict[str, Any]) -> str:
    return _call(
        "resize",
        (str, body.get("target", "main")),
        (int, int(body["width"])),
        (int, int(body["height"])),
    )


def _h_hover(body: dict[str, Any]) -> str:
    return _call("hover", (str, body["target"]))


_dispatch: dict[str, Any] = {
    "/screenshot": _h_screenshot,
    "/click": _h_click,
    "/type": _h_type,
    "/key": _h_key,
    "/trigger": _h_trigger,
    "/set_tab": _h_set_tab,
    "/set_property": _h_set_property,
    "/dismiss": _h_dismiss,
    "/resize": _h_resize,
    "/hover": _h_hover,
    "/wait_for": _wait_for,
}

_INTERACTIVE_PATHS = {
    "/click",
    "/type",
    "/key",
    "/trigger",
    "/set_tab",
    "/set_property",
    "/dismiss",
    "/resize",
    "/hover",
}


def _make_handler() -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            return

        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            if parsed.path == "/widgets":
                self._respond(200, _call("list_widgets"))
            elif parsed.path == "/actions":
                self._respond(200, _call("list_actions"))
            elif parsed.path == "/property":
                params = parse_qs(parsed.query)
                target = (params.get("target") or [""])[0]
                name = (params.get("name") or [""])[0]
                if not target or not name:
                    self._respond(400, json.dumps({"error": "missing 'target' or 'name'"}))
                    return
                self._respond(200, _call("get_property", (str, target), (str, name)))
            else:
                self._respond(404, json.dumps({"error": "not found"}))

        def do_POST(self) -> None:
            if self.path in _INTERACTIVE_PATHS and not _interactive:
                self._respond(
                    403,
                    json.dumps(
                        {
                            "error": "interactive endpoints disabled; "
                            "set CAPA_SCREENSHOT_PROBE_INTERACTIVE=1 before launch",
                        }
                    ),
                )
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode()) if length else {}
            except (ValueError, json.JSONDecodeError) as exc:
                self._respond(400, json.dumps({"error": f"bad request: {exc}"}))
                return
            handler = _dispatch.get(self.path)
            if handler is None:
                self._respond(404, json.dumps({"error": "not found"}))
                return
            try:
                result = handler(body)
            except KeyError as exc:
                self._respond(400, json.dumps({"error": f"missing field: {exc}"}))
                return
            self._respond(200, result)

        def _respond(self, code: int, body: str) -> None:
            payload = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler


def install() -> None:
    """Start the HTTP probe in a daemon thread. Idempotent.

    Uses ``ThreadingHTTPServer`` so concurrent requests are possible — this
    is what makes ``/dismiss`` (or any other endpoint) work even when a
    thread-blocking action like ``QDialog.exec()`` has the GUI thread stuck
    in a nested event loop. Nested event loops process queued cross-thread
    invocations, so a parallel HTTP request can still drive the GUI thread
    out of the modal block.
    """
    global _probe, _server, _interactive  # noqa: PLW0603 — module-level singleton init
    if _server is not None:
        return
    _interactive = bool(os.environ.get("CAPA_SCREENSHOT_PROBE_INTERACTIVE"))
    _probe = _Probe()
    try:
        _server = ThreadingHTTPServer(("127.0.0.1", _PORT), _make_handler())
    except OSError as exc:
        _logger.warning("ui.screenshot_probe.bind_failed", port=_PORT, error=str(exc))
        _probe = None
        return
    threading.Thread(
        target=_server.serve_forever, daemon=True, name="capa-screenshot-probe"
    ).start()
    _logger.info("ui.screenshot_probe.started", port=_PORT, interactive=_interactive)
