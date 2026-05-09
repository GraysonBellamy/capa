"""Camera preview dock — tile lifecycle + JPEG render + stale watchdog.

The dock subscribes to ``RunController.preview_received`` and routes each
``(camera_name, jpeg_bytes)`` payload to the matching tile. Tiles render
the JPEG via ``QImage.fromData`` and flip a small status label between
"idle" / "live" / "stale" so the operator can tell at a glance whether
previews are actually flowing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO
from typing import Any, Literal

from PIL import Image

from capa.devices.camera.base import CameraEvent, CameraSpec
from capa.ui.docks.camera_preview import (
    PREVIEW_TILE_WIDTH,
    STALE_THRESHOLD_MS,
    CameraPreviewDock,
)


def _event(
    name: str,
    kind: str,
    *,
    severity: Literal["info", "warning", "error"] = "info",
    message: str = "",
) -> CameraEvent:
    return CameraEvent(
        name=name,
        t_mono_ns=0,
        t_utc=datetime.now(UTC),
        kind=kind,
        message=message,
        severity=severity,
    )


def _spec(name: str, kind: str = "visible") -> CameraSpec:
    return CameraSpec.model_validate(
        {
            "name": name,
            "adapter": "capa.devices.camera.webcam",
            "kind": kind,
        }
    )


def _jpeg(size: tuple[int, int] = (320, 240), color: tuple[int, int, int] = (0, 200, 30)) -> bytes:
    """Synthesize a small valid JPEG."""
    img = Image.new("RGB", size, color)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=70)
    return buf.getvalue()


class TestTileRendering:
    def test_idle_tile_shows_no_preview_placeholder(self, qtbot: Any) -> None:
        dock = CameraPreviewDock(cameras=[_spec("visible_cam0")])
        qtbot.addWidget(dock)

        tile = dock._tiles["visible_cam0"]
        assert tile._image_label.text() == "no preview"
        assert tile._cadence_label.text() == "idle"
        assert tile._has_frame is False

    def test_update_preview_renders_pixmap_and_flips_to_live(self, qtbot: Any) -> None:
        dock = CameraPreviewDock(cameras=[_spec("visible_cam0")])
        qtbot.addWidget(dock)

        dock.update_preview("visible_cam0", _jpeg())

        tile = dock._tiles["visible_cam0"]
        pixmap = tile._image_label.pixmap()
        assert not pixmap.isNull()
        # Width-cap matches the adapter-side preview width.
        assert pixmap.width() == PREVIEW_TILE_WIDTH
        # Placeholder text cleared once a frame lands.
        assert tile._image_label.text() == ""
        assert tile._cadence_label.text() == "live"
        assert tile._has_frame is True

    def test_unknown_camera_name_silently_ignored(self, qtbot: Any) -> None:
        """Belt-and-braces: an emission tagged with a name the dock has
        never heard of must not raise (e.g. config reloaded after run
        start, dock rebuilt mid-stream)."""
        dock = CameraPreviewDock(cameras=[_spec("visible_cam0")])
        qtbot.addWidget(dock)

        # No exception — the unknown-name path returns silently.
        dock.update_preview("phantom_cam", _jpeg())

    def test_undecodable_jpeg_does_not_corrupt_tile(self, qtbot: Any) -> None:
        dock = CameraPreviewDock(cameras=[_spec("visible_cam0")])
        qtbot.addWidget(dock)

        dock.update_preview("visible_cam0", b"not-a-jpeg")

        tile = dock._tiles["visible_cam0"]
        # Tile stays in the idle state when the bytes can't be decoded.
        assert tile._has_frame is False
        assert tile._cadence_label.text() == "idle"


class TestStaleWatchdog:
    def test_stale_label_after_threshold(self, qtbot: Any) -> None:
        """After STALE_THRESHOLD_MS without a new preview the cadence
        indicator flips from ``live`` to ``stale``. Otherwise the
        operator can't tell that previews stopped flowing."""
        dock = CameraPreviewDock(cameras=[_spec("visible_cam0")])
        qtbot.addWidget(dock)

        dock.update_preview("visible_cam0", _jpeg())
        tile = dock._tiles["visible_cam0"]
        assert tile._cadence_label.text() == "live"

        # Wait for the single-shot timer to fire. Add 200 ms slack so the
        # event loop has room to deliver the timeout.
        qtbot.waitUntil(
            lambda: tile._cadence_label.text() == "stale",
            timeout=STALE_THRESHOLD_MS + 500,
        )

    def test_stale_timer_resets_on_each_preview(self, qtbot: Any) -> None:
        """Successive previews must keep the tile in ``live`` — the
        single-shot timer is restarted on every ``update_preview``."""
        dock = CameraPreviewDock(cameras=[_spec("visible_cam0")])
        qtbot.addWidget(dock)

        dock.update_preview("visible_cam0", _jpeg())
        # Halfway through the threshold push another frame; the timer
        # restarts and ``stale`` should NOT fire by the original deadline.
        qtbot.wait(STALE_THRESHOLD_MS // 2)
        dock.update_preview("visible_cam0", _jpeg())
        qtbot.wait(STALE_THRESHOLD_MS // 2 + 100)

        tile = dock._tiles["visible_cam0"]
        # Past the original threshold but inside the second window → live.
        assert tile._cadence_label.text() == "live"


class TestDockLayout:
    def test_no_cameras_shows_placeholder(self, qtbot: Any) -> None:
        dock = CameraPreviewDock(cameras=[])
        qtbot.addWidget(dock)
        # Placeholder label exists; no tiles registered.
        assert not dock._tiles

    def test_two_cameras_two_tiles(self, qtbot: Any) -> None:
        dock = CameraPreviewDock(cameras=[_spec("visible_cam0"), _spec("ir_cam0", kind="ir")])
        qtbot.addWidget(dock)
        assert set(dock._tiles.keys()) == {"visible_cam0", "ir_cam0"}

    def test_dock_object_name_persists_in_window_state(self, qtbot: Any) -> None:
        """``QMainWindow.saveState()`` keys docks by ``objectName``;
        without one the bottom-area placement does not survive a
        restart."""
        dock = CameraPreviewDock(cameras=[_spec("visible_cam0")])
        qtbot.addWidget(dock)
        assert dock.objectName() == "dock_camera_preview"


class TestEventDrivenSurfaces:
    """Drops counter + sticky borders driven by ``CameraEvent`` (the
    follow-up to v1's preview-only dock). Covers the full
    ``pump_warning`` / ``pump_failed`` / ``recording_stopped`` triad."""

    def test_pump_warning_increments_drops_counter(self, qtbot: Any) -> None:
        dock = CameraPreviewDock(cameras=[_spec("visible_cam0")])
        qtbot.addWidget(dock)

        tile = dock._tiles["visible_cam0"]
        assert tile._drops_label.text() == "drops: 0"

        for _ in range(3):
            dock.note_event(_event("visible_cam0", "pump_warning", severity="warning"))

        assert tile._drops_label.text() == "drops: 3"
        assert tile._dropped_frames == 3

    def test_pump_warning_sets_warn_border(self, qtbot: Any) -> None:
        dock = CameraPreviewDock(cameras=[_spec("visible_cam0")])
        qtbot.addWidget(dock)

        tile = dock._tiles["visible_cam0"]
        # Idle border: 1 px solid grey.
        assert "1px" in tile.styleSheet()

        dock.note_event(_event("visible_cam0", "pump_warning", severity="warning"))

        # Warn border: 2 px solid yellow. Match by the 2px width — the
        # specific colour tuple is theme-dependent.
        assert "2px" in tile.styleSheet()

    def test_pump_failed_sets_fail_border_and_failed_label(self, qtbot: Any) -> None:
        dock = CameraPreviewDock(cameras=[_spec("visible_cam0")])
        qtbot.addWidget(dock)

        tile = dock._tiles["visible_cam0"]
        dock.note_event(_event("visible_cam0", "pump_failed", severity="error", message="boom"))

        assert tile._failed is True
        assert tile._cadence_label.text() == "failed"
        assert "2px" in tile.styleSheet()

    def test_pump_failed_is_sticky_against_subsequent_warnings(self, qtbot: Any) -> None:
        """A ``pump_warning`` after a ``pump_failed`` must not downgrade
        the surface — once the recording has died the sticky red border
        and ``failed`` cadence stay until the dock rebuilds."""
        dock = CameraPreviewDock(cameras=[_spec("visible_cam0")])
        qtbot.addWidget(dock)

        tile = dock._tiles["visible_cam0"]
        dock.note_event(_event("visible_cam0", "pump_failed", severity="error"))
        failed_style = tile.styleSheet()

        dock.note_event(_event("visible_cam0", "pump_warning", severity="warning"))

        # Counter still tracks the warning, but the border + label stay
        # in the failed state.
        assert tile._dropped_frames == 1
        assert tile._cadence_label.text() == "failed"
        assert tile.styleSheet() == failed_style

    def test_pump_failed_freezes_live_label_against_new_previews(self, qtbot: Any) -> None:
        """Once a recording is marked failed, an in-flight preview must
        not flip the cadence back to ``live`` — the operator needs to
        see the failure even if the pump recovered."""
        dock = CameraPreviewDock(cameras=[_spec("visible_cam0")])
        qtbot.addWidget(dock)

        tile = dock._tiles["visible_cam0"]
        dock.note_event(_event("visible_cam0", "pump_failed", severity="error"))
        dock.update_preview("visible_cam0", _jpeg())

        assert tile._cadence_label.text() == "failed"

    def test_recording_stopped_marks_cadence_stopped(self, qtbot: Any) -> None:
        dock = CameraPreviewDock(cameras=[_spec("visible_cam0")])
        qtbot.addWidget(dock)

        tile = dock._tiles["visible_cam0"]
        # Need at least one preview frame for ``stopped`` to render — a
        # never-started camera has nothing to "stop".
        dock.update_preview("visible_cam0", _jpeg())
        dock.note_event(_event("visible_cam0", "recording_stopped"))

        assert tile._cadence_label.text() == "stopped"

    def test_event_routes_to_correct_camera(self, qtbot: Any) -> None:
        """A two-camera dock must only update the named tile, not the
        other one — earlier drafts that filtered on `event.adapter`
        regressed this."""
        dock = CameraPreviewDock(cameras=[_spec("visible_cam0"), _spec("visible_cam1")])
        qtbot.addWidget(dock)

        dock.note_event(_event("visible_cam0", "pump_warning", severity="warning"))

        assert dock._tiles["visible_cam0"]._dropped_frames == 1
        assert dock._tiles["visible_cam1"]._dropped_frames == 0

    def test_unknown_event_camera_silently_ignored(self, qtbot: Any) -> None:
        dock = CameraPreviewDock(cameras=[_spec("visible_cam0")])
        qtbot.addWidget(dock)

        # Must not raise.
        dock.note_event(_event("phantom_cam", "pump_warning", severity="warning"))

    def test_non_camera_event_silently_ignored(self, qtbot: Any) -> None:
        """The signal is typed ``object``; the dock defensively narrows
        to ``CameraEvent`` and returns silently on anything else."""
        dock = CameraPreviewDock(cameras=[_spec("visible_cam0")])
        qtbot.addWidget(dock)

        # Anything that isn't a CameraEvent is dropped on the floor.
        dock.note_event("not-an-event")
        dock.note_event(None)
        dock.note_event(42)
