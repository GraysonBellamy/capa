"""Regenerate the user-guide GUI screenshots.

Drives the live CAPA GUI through every state the user-guide pages need
and saves PNGs to ``docs/_snippets/images/``. Idempotent — re-runs
overwrite previous captures.

Usage:

    uv run python scripts/capture_doc_screenshots.py

Requirements:

    * Working directory must be the repo root.
    * Sim configs at ``configs/experiments/sim_freerun.yaml`` and
      ``configs/experiments/sim_capa_pyrolysis.yaml`` must load cleanly.
    * Pillow installed (already a dev dep).

How it works:

    1. Edits ``configs/experiments/sim_freerun.yaml`` to bump
       ``duration_s`` from 0.5 to 600 so runs stay alive long enough to
       capture mid-flight. Restores the original on exit (try/finally).
    2. Launches ``capa gui`` in a subprocess with the screenshot probe
       enabled (env vars ``CAPA_SCREENSHOT_PROBE=1`` and
       ``CAPA_SCREENSHOT_PROBE_INTERACTIVE=1``).
    3. Walks a list of named "shots", each a function that drives the
       probe via HTTP and writes a PNG (full window, dock, or PIL-cropped
       region).
    4. Quits the GUI, then relaunches with a different config for
       method-tab shots that need a multi-step method preloaded.
    5. Relaunches once more with no config for the welcome / no-config
       shots.

Probe API: see ``docs/contributing/screenshot-probe.md``.

Known gaps (skipped — capture manually if needed):

    * ``run-emergency-hold.png`` — :class:`HoldToConfirmButton` only
      paints its progress fill while ``mousePressEvent`` is in flight,
      and the probe's ``/click`` does press+release atomically. Needs a
      probe extension (separate ``/mouse_press`` and ``/mouse_release``,
      or a test-only ``simulate_hold()`` on the button) to capture.
    * ``setup-cal-plot.png``, ``setup-cal-diff.png``,
      ``setup-apply-to-channels.png`` — Calibration plot/diff are modal
      dialogs spawned per-channel from inline buttons that don't yet
      have objectNames; the sim configs ship with identity calibrations
      so the plots would be flat. Needs a non-identity sim fixture plus
      objectNames on the inline calibration buttons.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import urllib.parse
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "docs" / "_snippets" / "images"
SIM_FREERUN_YAML = REPO_ROOT / "configs" / "experiments" / "sim_freerun.yaml"
SIM_PYROLYSIS_YAML = REPO_ROOT / "configs" / "experiments" / "sim_capa_pyrolysis.yaml"
PROBE_HOST = "127.0.0.1"
PROBE_PORT = 9876
WINDOW_W = 1500
WINDOW_H = 950
# The connection strip lives at SetupTab-relative (4, 34) with size 1234x38.
# Within the 1500x950 main window with toolbar above it, that maps to:
STRIP_CROP_BOX = (4, 95, 1238, 135)  # (left, top, right, bottom)


# ----------------------------------------------------------------------- probe


@dataclass
class Probe:
    """Minimal HTTP client for the screenshot probe."""

    base_url: str = f"http://{PROBE_HOST}:{PROBE_PORT}"

    def get(self, path: str, **params: str) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        data = json.dumps(body or {}).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def screenshot(self, target: str, out_path: Path) -> dict[str, Any]:
        return self.post("/screenshot", {"target": target, "out": str(out_path)})

    def click(self, target: str) -> dict[str, Any]:
        return self.post("/click", {"target": target})

    def set_tab(self, target: str, tab: str | int) -> dict[str, Any]:
        return self.post("/set_tab", {"target": target, "tab": str(tab)})

    def trigger(self, action: str) -> dict[str, Any]:
        return self.post("/trigger", {"action": action})

    def resize(self, w: int, h: int, target: str = "main") -> dict[str, Any]:
        return self.post("/resize", {"target": target, "width": w, "height": h})

    def key(self, key: str, target: str = "", modifiers: list[str] | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"key": key, "modifiers": modifiers or []}
        if target:
            body["target"] = target
        return self.post("/key", body)

    def property(self, target: str, name: str) -> Any:
        result = self.get("/property", target=target, name=name)
        return result.get("value")

    def wait_until(
        self,
        target: str,
        name: str,
        expected: Any,
        *,
        timeout_s: float = 10.0,
        poll_s: float = 0.5,
    ) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                if self.property(target, name) == expected:
                    return True
            except urllib.error.URLError:
                pass
            time.sleep(poll_s)
        return False


def _port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        try:
            sock.connect((host, port))
        except OSError:
            return False
        return True


def _wait_for_probe(timeout_s: float = 60.0) -> Probe:
    """Block until the probe accepts connections, then return a client."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _port_open(PROBE_HOST, PROBE_PORT):
            probe = Probe()
            try:
                probe.get("/widgets")
                return probe
            except (urllib.error.URLError, json.JSONDecodeError):
                pass
        time.sleep(0.5)
    raise RuntimeError(f"probe never came up on {PROBE_HOST}:{PROBE_PORT}")


# -------------------------------------------------------------- yaml lifecycle


@contextmanager
def long_freerun_duration() -> Iterator[None]:
    """Temporarily set sim_freerun.yaml duration_s to 600 so runs persist."""
    original = SIM_FREERUN_YAML.read_text(encoding="utf-8")
    bumped = original.replace(
        "duration_s: 0.5",
        "duration_s: 600",
    )
    if bumped == original:
        # Already changed, or unexpected format — leave alone.
        yield
        return
    try:
        SIM_FREERUN_YAML.write_text(bumped, encoding="utf-8")
        yield
    finally:
        SIM_FREERUN_YAML.write_text(original, encoding="utf-8")


# --------------------------------------------------------------- gui lifecycle


@contextmanager
def gui(
    config: Path | None = None,
    *,
    env_extra: dict[str, str] | None = None,
) -> Iterator[Probe]:
    """Launch CAPA GUI with the probe, yield a connected Probe, kill on exit.

    ``env_extra`` lets the caller inject sim-only knobs that change the
    apply behaviour for a single shot (e.g. ``CAPA_SIM_OPEN_DELAY_MS``
    to hold the CONNECTING state long enough to screenshot, or
    ``CAPA_SIM_OPEN_FAIL`` to force apply into the FAILED state).
    """
    env = {
        **os.environ,
        "CAPA_SCREENSHOT_PROBE": "1",
        "CAPA_SCREENSHOT_PROBE_INTERACTIVE": "1",
    }
    if env_extra:
        env.update(env_extra)
    cmd = ["uv", "run", "capa", "gui"]
    if config is not None:
        cmd.append(str(config))
    proc = subprocess.Popen(
        cmd,
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
    )
    try:
        probe = _wait_for_probe()
        probe.resize(WINDOW_W, WINDOW_H)
        # Initial paint settle — first paint can lag a beat behind probe ready.
        time.sleep(1.0)
        yield probe
    finally:
        try:
            Probe().post("/trigger", {"action": "Quit"})
        except Exception:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


# ------------------------------------------------------------------- crop util


def crop(src: Path, dst: Path, box: tuple[int, int, int, int]) -> None:
    """PIL crop. ``box`` is (left, top, right, bottom)."""
    with Image.open(src) as img:
        img.crop(box).save(dst, "PNG")


# ------------------------------------------------------------------- captures
#
# Each function captures one or more named PNGs. They assume the probe is
# pointed at the right config and the GUI is in a defensible starting state
# (Setup tab, clean draft, no run in progress) — callers compose them.


def shot_setup_tab_full_and_strip_unapplied(probe: Probe) -> None:
    """Adds a sim Alicat device to make the draft dirty, then captures
    ``setup-tab-full.png`` and crops ``strip-unapplied.png`` and
    ``setup-outline-markers.png`` from the resulting main window.

    Note: outline markers will be `●` only (dirty); for stacked `●✗`
    use ``shot_setup_problems_and_outline_markers``.
    """
    probe.set_tab("main_tabs", "Setup")
    # Navigate outline to Devices (Down 6 from Overview).
    probe.click("setup_outline")
    probe.key("Home", target="setup_outline")
    for _ in range(6):
        probe.key("Down", target="setup_outline")
    # Add a sim device to introduce a dirty edit.
    probe.trigger("Alicat MFC / MFM (simulated)")
    time.sleep(0.5)
    main_png = OUT_DIR / "_main_unapplied.png"
    probe.screenshot("main", main_png)
    shutil.copy(main_png, OUT_DIR / "setup-tab-full.png")
    crop(main_png, OUT_DIR / "strip-unapplied.png", STRIP_CROP_BOX)
    probe.screenshot("setup_outline", OUT_DIR / "setup-outline-markers.png")
    # Revert the dirty edit so subsequent shots start clean.
    probe.trigger("Revert draft")
    time.sleep(0.5)


def shot_strip_connected(probe: Probe) -> None:
    """Captures ``strip-connected.png`` from current connected/clean state."""
    main_png = OUT_DIR / "_main_connected.png"
    probe.screenshot("main", main_png)
    crop(main_png, OUT_DIR / "strip-connected.png", STRIP_CROP_BOX)


def shot_setup_problems_and_outline_markers(probe: Probe) -> None:
    """Adds a real Watlow device (which requires a port and so fails
    validation) to produce an error in the Problems panel and ``●✗``
    stacked markers in the outline."""
    probe.set_tab("main_tabs", "Setup")
    probe.trigger("Watlow PM-series controller")
    time.sleep(1.0)
    probe.screenshot("setup_problems", OUT_DIR / "setup-problems.png")
    probe.screenshot("setup_outline", OUT_DIR / "setup-outline-markers.png")
    probe.trigger("Revert draft")
    time.sleep(0.5)


def shot_manual_dock_cards(probe: Probe) -> None:
    """Manual control dock — clean connected state, no run, all cards live."""
    probe.set_tab("main_tabs", "Setup")
    probe.screenshot("dock_manual_control", OUT_DIR / "manual-dock-cards.png")


def shot_run_tab_idle(probe: Probe) -> None:
    """Run tab in Idle state — pre-Start, empty axes."""
    probe.set_tab("main_tabs", "Run")
    time.sleep(0.5)
    probe.screenshot("main", OUT_DIR / "run-tab-idle.png")
    shot_run_state_badge_grabs(probe, "idle")


def shot_run_tab_running_and_buttons(probe: Probe) -> None:
    """Start a run, wait ~20s, capture ``run-tab-running.png`` and crop
    ``run-buttons.png`` from the run header."""
    probe.set_tab("main_tabs", "Run")
    probe.click("run_start_button")
    if not probe.wait_until("run_state_badge", "text", "Running", timeout_s=10):
        raise RuntimeError("run never reached Running state — check sim_freerun duration_s")
    time.sleep(15)
    probe.screenshot("main", OUT_DIR / "run-tab-running.png")
    shot_run_state_badge_grabs(probe, "running")
    header_png = OUT_DIR / "_run_header.png"
    probe.screenshot("run_header", header_png)
    crop(header_png, OUT_DIR / "run-buttons.png", (720, 0, 1230, 36))


def shot_strip_frozen_and_manual_writeblocked(probe: Probe) -> None:
    """While the run is running, switch to Setup tab and capture
    ``strip-frozen.png`` (FROZEN state) and ``manual-writeblocked.png``."""
    # Caller must have a run in progress.
    probe.set_tab("main_tabs", "Setup")
    time.sleep(0.5)
    main_png = OUT_DIR / "_main_frozen.png"
    probe.screenshot("main", main_png)
    crop(main_png, OUT_DIR / "strip-frozen.png", STRIP_CROP_BOX)
    probe.screenshot("dock_manual_control", OUT_DIR / "manual-writeblocked.png")


def shot_run_tab_sealed(probe: Probe) -> None:
    """Stop the running run; wait for Sealed badge; capture ``run-tab-sealed.png``."""
    probe.set_tab("main_tabs", "Run")
    probe.click("run_stop_button")
    if not probe.wait_until("run_state_badge", "text", "Sealed", timeout_s=20):
        raise RuntimeError("run never reached Sealed state")
    time.sleep(1)
    probe.screenshot("main", OUT_DIR / "run-tab-sealed.png")
    shot_run_state_badge_grabs(probe, "sealed")


def shot_method_tab_freerun(probe: Probe) -> None:
    """Method tab placeholder pane when the experiment has no method."""
    probe.set_tab("main_tabs", 1)
    time.sleep(0.5)
    probe.screenshot("main", OUT_DIR / "method-tab-freerun.png")


def shot_method_tab_multistep_and_addstep_menu(probe: Probe) -> None:
    """Assumes the GUI was launched with sim_capa_pyrolysis.yaml.
    Captures ``method-tab-multistep.png`` and ``method-add-step-menu.png``."""
    probe.set_tab("main_tabs", 1)
    time.sleep(0.5)
    probe.screenshot("main", OUT_DIR / "method-tab-multistep.png")
    # Open the Add Step menu.
    probe.click("method_add_step_button")
    time.sleep(0.5)
    composite_png = OUT_DIR / "_method_addstep.png"
    probe.screenshot("screen", composite_png)
    probe.key("Escape")
    crop(composite_png, OUT_DIR / "method-add-step-menu.png", (200, 60, 460, 330))


def shot_welcome_and_manual_empty(probe: Probe) -> None:
    """Assumes the GUI was launched with no config.
    Captures ``welcome-hero.png``, ``welcome-recents.png``, ``manual-empty.png``."""
    welcome_png = OUT_DIR / "welcome-hero.png"
    probe.screenshot("main", welcome_png)
    crop(welcome_png, OUT_DIR / "welcome-recents.png", (50, 390, 800, 480))
    probe.screenshot("dock_manual_control", OUT_DIR / "manual-empty.png")


# ---------------------------------------------------------- new shot helpers


def _wait_strip_text(probe: Probe, prefix: str, *, timeout_s: float = 15.0) -> bool:
    """Poll the strip text label until it starts with ``prefix``."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            text = probe.property("strip_text_label", "text")
            if isinstance(text, str) and text.startswith(prefix):
                return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


def _make_draft_dirty(probe: Probe) -> None:
    """Add a sim Alicat device to flip ``draft.unapplied`` true.

    Apply & Connect is gated on ``draft.unapplied``; without a dirty
    edit the apply button stays disabled even after a failed initial
    open. Adding (and *not* reverting) a sim device is the cheapest way
    to unlock it.
    """
    probe.set_tab("main_tabs", "Setup")
    probe.click("setup_outline")
    probe.key("Home", target="setup_outline")
    for _ in range(6):
        probe.key("Down", target="setup_outline")
    probe.trigger("Alicat MFC / MFM (simulated)")
    time.sleep(0.4)


def shot_strip_failed(probe: Probe) -> None:
    """Force a failed Apply and crop ``strip-failed.png`` from the
    main-window capture (full-strip grab returns washed-out CSS).

    Launched with ``CAPA_SIM_OPEN_FAIL=1`` set in the GUI process env so
    the sim watlow adapter's ``open()`` raises. Initial config-load
    failure doesn't set ``last_apply_failed`` — only an explicit Apply
    does — so we make a dirty edit and click the strip's Apply button.
    """
    _make_draft_dirty(probe)
    probe.click("strip_apply_button")
    if not _wait_strip_text(probe, "Last apply failed", timeout_s=15.0):
        raise RuntimeError("strip never reached FAILED state — check CAPA_SIM_OPEN_FAIL env")
    main_png = OUT_DIR / "_main_failed.png"
    probe.screenshot("main", main_png)
    crop(main_png, OUT_DIR / "strip-failed.png", STRIP_CROP_BOX)


def shot_strip_connecting(probe: Probe) -> None:
    """Catch the brief CONNECTING state by holding the sim adapter's
    ``open()`` in an artificial sleep (``CAPA_SIM_OPEN_DELAY_MS``).

    Launch sequence: initial config-load runs through the delay too (so
    we have to wait a bit before the GUI is settled to CONNECTED); then
    we make a dirty edit, click Apply, and capture during the second
    open's delay window.
    """
    # Initial load already burned through the delay; wait for it to settle.
    _wait_strip_text(probe, "Connected", timeout_s=20.0)
    _make_draft_dirty(probe)
    probe.click("strip_apply_button")
    # Strip should flip to "Connecting — opening hardware…" within ms.
    if not _wait_strip_text(probe, "Connecting", timeout_s=3.0):
        raise RuntimeError("strip never reached CONNECTING state — check CAPA_SIM_OPEN_DELAY_MS")
    # Capture inside the delay window. The delay env var below should
    # exceed (capture latency + a margin) so the strip is still on
    # CONNECTING when /screenshot runs.
    main_png = OUT_DIR / "_main_connecting.png"
    probe.screenshot("main", main_png)
    crop(main_png, OUT_DIR / "strip-connecting.png", STRIP_CROP_BOX)
    # Let the open complete so the gui() teardown isn't racing the sleep.
    _wait_strip_text(probe, "Connected", timeout_s=20.0)


def shot_method_validate_error(probe: Probe) -> None:
    """Trigger Method-tab validate with no steps to surface the
    ``Method has no valid steps yet…`` info dialog. Captures the
    composite ``method-validate-error.png``.

    ``Validate method`` opens a modal ``QMessageBox.information`` via
    ``.exec()``; the /trigger call will block until the dialog closes,
    so we fire it through a background thread and let the foreground
    dismiss it after the capture lands.
    """
    import threading

    probe.set_tab("main_tabs", "Method")
    time.sleep(0.4)

    def _fire() -> None:
        try:
            Probe().post("/trigger", {"action": "Validate method"})
        except Exception:
            pass

    t = threading.Thread(target=_fire, daemon=True)
    t.start()
    # Dialog should appear within a tick.
    probe.post(
        "/wait_for",
        {"target": "active_dialog", "condition": "visible", "timeout_ms": 3000},
    )
    time.sleep(0.3)
    probe.screenshot("screen", OUT_DIR / "method-validate-error.png")
    probe.post("/dismiss")
    t.join(timeout=2.0)


def shot_setup_discovery(probe: Probe) -> None:
    """Open the Discovery dialog and capture ``setup-discovery.png``."""
    probe.set_tab("main_tabs", "Setup")
    time.sleep(0.3)

    probe.trigger("Scan for devices")
    # DiscoveryDialog uses dialog.show() (non-modal) so /trigger returns
    # immediately; just poll for the named dialog to appear.
    ok = probe.post(
        "/wait_for",
        {"target": "discovery_dialog", "condition": "visible", "timeout_ms": 5000},
    )
    if not ok.get("ok"):
        raise RuntimeError(f"discovery dialog never appeared: {ok}")
    # Let the initial "Scanning…" header + a few rows paint.
    time.sleep(1.5)
    probe.screenshot("screen", OUT_DIR / "setup-discovery.png")
    # Click the dialog's Close button — /dismiss-via-Escape doesn't
    # always reach the dialog's keyPressEvent depending on focus, and
    # leaving the dialog open contaminates subsequent screen captures.
    probe.click("discovery_close_button")
    probe.post(
        "/wait_for",
        {"target": "discovery_dialog", "condition": "hidden", "timeout_ms": 3000},
    )


def shot_manual_confirm_dialog(probe: Probe) -> None:
    """Click Heater → "Cool to safe" and capture its confirmation
    QMessageBox. Demonstrates the destructive-write confirmation flow
    every manual card uses.
    """
    import threading

    probe.set_tab("main_tabs", "Setup")
    time.sleep(0.3)

    def _fire() -> None:
        try:
            Probe().post("/click", {"target": "heater_cool_to_safe_button"})
        except Exception:
            pass

    t = threading.Thread(target=_fire, daemon=True)
    t.start()
    ok = probe.post(
        "/wait_for",
        {"target": "active_dialog", "condition": "visible", "timeout_ms": 3000},
    )
    if not ok.get("ok"):
        raise RuntimeError(f"confirm dialog never appeared: {ok}")
    time.sleep(0.3)
    probe.screenshot("screen", OUT_DIR / "manual-confirm-dialog.png")
    probe.post("/dismiss")
    t.join(timeout=2.0)


def shot_run_state_badge_grabs(probe: Probe, state_slug: str) -> None:
    """Capture the current run-state badge to a scratch PNG. Called from
    each run-state shot so :func:`compose_run_badge_states` can stitch
    them later."""
    probe.screenshot("run_state_badge", OUT_DIR / f"_badge_{state_slug}.png")


def compose_run_badge_states() -> None:
    """Stitch the per-state badge grabs into ``run-badge-states.png``.

    Reads the ``_badge_<state>.png`` scratch files produced by the
    per-state shots and lays them out vertically with the state name
    above each. Missing states are silently skipped so the composite
    still emits something usable when (e.g.) the draining hook isn't
    enabled.
    """
    from PIL import Image, ImageDraw, ImageFont

    states = ["idle", "running", "draining", "sealed"]
    grabs: list[tuple[str, Image.Image]] = []
    for slug in states:
        path = OUT_DIR / f"_badge_{slug}.png"
        if path.exists():
            grabs.append((slug.title(), Image.open(path).copy()))
    if not grabs:
        return
    pad = 12
    label_h = 22
    badge_w = max(img.width for _, img in grabs)
    badge_h = max(img.height for _, img in grabs)
    row_h = label_h + badge_h
    canvas_w = badge_w + 2 * pad
    canvas_h = len(grabs) * row_h + (len(grabs) + 1) * pad
    canvas = Image.new("RGBA", (canvas_w, canvas_h), (32, 32, 32, 255))
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.truetype("arial.ttf", 14)
    except OSError:
        font = ImageFont.load_default()
    y = pad
    for label, img in grabs:
        draw.text((pad, y), label, fill=(220, 220, 220, 255), font=font)
        canvas.paste(img, (pad, y + label_h))
        y += row_h + pad
    canvas.convert("RGB").save(OUT_DIR / "run-badge-states.png", "PNG")


def shot_abort_draining(probe: Probe) -> None:
    """Start a run, click Stop, capture the Run tab while the conductor
    is dwelling in ``DRAINING`` (held open by ``CAPA_DRAINING_DELAY_S``
    set on the GUI process). Also captures the badge crop used by the
    ``run-badge-states.png`` composite.
    """
    probe.set_tab("main_tabs", "Run")
    probe.click("run_start_button")
    if not probe.wait_until("run_state_badge", "text", "Running", timeout_s=10):
        raise RuntimeError("run never reached Running — check sim_freerun duration_s")
    time.sleep(2.0)
    probe.click("run_stop_button")
    if not probe.wait_until("run_state_badge", "text", "Draining…", timeout_s=10):
        raise RuntimeError("run never reached Draining — check CAPA_DRAINING_DELAY_S")
    probe.screenshot("main", OUT_DIR / "abort-draining.png")
    shot_run_state_badge_grabs(probe, "draining")
    # Let the run finish so the gui() teardown isn't racing the draining sleep.
    probe.wait_until("run_state_badge", "text", "Sealed", timeout_s=30)


def shot_strip_idle(probe: Probe) -> None:
    """Trigger File → Close Config and crop ``strip-idle.png`` from the
    main-window capture once the strip flips to IDLE ("No config loaded").
    Must run LAST in its pass — tears down the worker pool.
    """
    probe.trigger("Close Config")
    if not _wait_strip_text(probe, "No config loaded", timeout_s=10.0):
        raise RuntimeError("strip never reached IDLE — Close Config may have failed")
    probe.set_tab("main_tabs", "Setup")
    time.sleep(0.4)
    main_png = OUT_DIR / "_main_idle.png"
    probe.screenshot("main", main_png)
    crop(main_png, OUT_DIR / "strip-idle.png", STRIP_CROP_BOX)


def shot_setup_wizard(probe: Probe) -> None:
    """Open the New Setup wizard and capture ``setup-wizard.png``.

    The wizard uses ``.exec()`` so /trigger blocks until it closes —
    fire it in a background thread, wait for the named wizard top-level
    to appear, screenshot, and dismiss.
    """
    import threading

    probe.set_tab("main_tabs", "Setup")
    time.sleep(0.3)

    def _fire() -> None:
        try:
            Probe().post("/trigger", {"action": "New from template…"})
        except Exception:
            pass

    t = threading.Thread(target=_fire, daemon=True)
    t.start()
    ok = probe.post(
        "/wait_for",
        {"target": "setup_wizard", "condition": "visible", "timeout_ms": 5000},
    )
    if not ok.get("ok"):
        raise RuntimeError(f"setup wizard never appeared: {ok}")
    time.sleep(0.5)
    probe.screenshot("screen", OUT_DIR / "setup-wizard.png")
    probe.post("/dismiss")
    t.join(timeout=2.0)


# ------------------------------------------------------------ scratch cleanup


def cleanup_scratch() -> None:
    """Delete the underscore-prefixed scratch PNGs in OUT_DIR."""
    for path in OUT_DIR.glob("_*.png"):
        path.unlink()


# ------------------------------------------------------------------- entry pt


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    with long_freerun_duration():
        # --- Pass 1: sim_freerun.yaml — Setup / Run / Manual shots ---
        with gui(SIM_FREERUN_YAML) as probe:
            shot_strip_connected(probe)
            shot_setup_tab_full_and_strip_unapplied(probe)
            shot_setup_problems_and_outline_markers(probe)
            shot_manual_dock_cards(probe)
            shot_manual_confirm_dialog(probe)
            shot_method_tab_freerun(probe)
            shot_method_validate_error(probe)
            shot_setup_discovery(probe)
            shot_setup_wizard(probe)
            shot_run_tab_idle(probe)
            shot_run_tab_running_and_buttons(probe)
            shot_strip_frozen_and_manual_writeblocked(probe)
            shot_run_tab_sealed(probe)
            # strip-idle runs LAST in this pass — File → Close Config
            # tears down the worker pool, so any later Setup-tab shot
            # would see hardware_ready=False.
            shot_strip_idle(probe)

        # --- Pass 1b: sim_freerun + open-fail env — strip-failed ---
        with gui(SIM_FREERUN_YAML, env_extra={"CAPA_SIM_OPEN_FAIL": "1"}) as probe:
            shot_strip_failed(probe)

        # --- Pass 1c: sim_freerun + open-delay env — strip-connecting ---
        # 3000 ms gives the screenshot a comfortable window between
        # apply-click and the open completing.
        with gui(SIM_FREERUN_YAML, env_extra={"CAPA_SIM_OPEN_DELAY_MS": "3000"}) as probe:
            shot_strip_connecting(probe)

        # --- Pass 1d: sim_freerun + draining-delay env — abort-draining ---
        # 3s of artificial draining is long enough to catch the badge
        # transition and the screenshot without pushing total pass
        # duration noticeably higher.
        with gui(SIM_FREERUN_YAML, env_extra={"CAPA_DRAINING_DELAY_S": "3"}) as probe:
            shot_abort_draining(probe)

        # --- Pass 2: sim_capa_pyrolysis.yaml — Method tab multistep ---
        with gui(SIM_PYROLYSIS_YAML) as probe:
            shot_method_tab_multistep_and_addstep_menu(probe)

        # --- Pass 3: no config — welcome + empty manual ---
        with gui() as probe:
            shot_welcome_and_manual_empty(probe)

    # Composite the per-state badge grabs collected across passes 1 and 1d.
    compose_run_badge_states()
    cleanup_scratch()
    print(f"\nCaptured to {OUT_DIR}/")
    print("\nKnown gaps — capture manually if needed:")
    print("  - run-emergency-hold (HoldToConfirmButton needs probe extension)")
    print("  - setup-cal-plot, setup-cal-diff, setup-apply-to-channels")
    print("    (need non-identity calibration fixture + inline-button objectNames)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
