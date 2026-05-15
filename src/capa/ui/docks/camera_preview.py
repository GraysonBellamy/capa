"""Camera preview dock — thumbnails for each active camera.

One :class:`_PreviewTile` per camera in :attr:`HardwareProfile.cameras`,
laid out in a flow grid. Tiles update at the adapter's throttled cadence
(``WebcamAdapter`` caps at 2 Hz; see ``PREVIEW_INTERVAL_NS`` in that
module). Cameras whose adapters do not declare
:attr:`CameraCapability.LIVE_PREVIEW` show a static "no preview" placeholder
and never receive frames —
:meth:`capa.runtime.camera_adapter.CameraDeviceAdapter.start_preview_channel`
early-outs on the capability flag, so the pool-owned preview
:class:`ThreadBridge` stays empty for those cameras.

Three live surfaces:

* **JPEG thumbnail** — driven by ``RunController.preview_received``.
* **Cadence indicator** — flips ``idle`` / ``live`` / ``stale`` based on
  preview arrival; ``stale`` after :data:`STALE_THRESHOLD_MS` of silence.
* **Drops counter + sticky border** — driven by
  ``RunController.camera_event_received``. ``pump_warning`` events bump
  the per-tile drop count and turn the tile border yellow;
  ``pump_failed`` (an end-of-recording fault) turns it red and labels it
  ``failed``. Both are sticky until the dock is rebuilt on the next
  config-load — the operator must see that something failed; auto-revert
  hides bugs.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QDockWidget,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from capa.devices.camera.base import CameraEvent, CameraSpec
from capa.ui.theme import COLOR_FAIL, COLOR_IDLE, COLOR_OK, COLOR_WARN, monospace_font

PREVIEW_TILE_WIDTH: Final[int] = 320
"""Matches :data:`capa.devices.camera.webcam.PREVIEW_MAX_WIDTH`. Tiles render
the JPEG at its native size — no upscale on the UI side."""

PREVIEW_TILE_PLACEHOLDER_HEIGHT: Final[int] = 180
"""Aspect-2:1-ish placeholder for the idle / no-preview state. Real frames
override the height to whatever the camera produced."""

STALE_THRESHOLD_MS: Final[int] = 2_500
"""Tile flips to ``stale`` if no preview arrives within this window. Sized
above the 500 ms preview cadence so a single dropped tick does not trip it."""


class _PreviewTile(QFrame):
    """One camera's tile: name + image + drops counter + cadence indicator."""

    def __init__(self, spec: CameraSpec, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._spec: CameraSpec = spec
        self._has_frame: bool = False
        self._dropped_frames: int = 0
        self._failed: bool = False

        self.setObjectName(f"preview_tile_{spec.name}")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setFrameShadow(QFrame.Shadow.Plain)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        self._set_border_idle()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        # Header row: camera name + kind tag.
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        name_label = QLabel(spec.name, self)
        name_label.setFont(monospace_font(point_size=10))
        kind_label = QLabel(spec.kind, self)
        kind_label.setFont(monospace_font(point_size=9))
        kind_label.setStyleSheet(f"color: {COLOR_IDLE.name()};")
        header.addWidget(name_label)
        header.addStretch(1)
        header.addWidget(kind_label)
        layout.addLayout(header)

        # Image area: QLabel with a centered pixmap. Fixed width, height
        # follows the JPEG; placeholder height for the idle state.
        self._image_label = QLabel(self)
        self._image_label.setFixedWidth(PREVIEW_TILE_WIDTH)
        self._image_label.setMinimumHeight(PREVIEW_TILE_PLACEHOLDER_HEIGHT)
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_label.setStyleSheet(f"background-color: #1a1a1a; color: {COLOR_IDLE.name()};")
        self._image_label.setText("no preview")
        layout.addWidget(self._image_label)

        # Status row: drops counter (left) + cadence indicator (right).
        # Per-run scope; the dock rebuilds on every config-load so counters
        # naturally reset between runs without explicit teardown.
        status = QHBoxLayout()
        status.setContentsMargins(0, 0, 0, 0)
        status.setSpacing(8)
        self._drops_label = QLabel("drops: 0", self)
        self._drops_label.setFont(monospace_font(point_size=9))
        self._drops_label.setStyleSheet(f"color: {COLOR_IDLE.name()};")
        self._cadence_label = QLabel("idle", self)
        self._cadence_label.setFont(monospace_font(point_size=9))
        self._cadence_label.setStyleSheet(f"color: {COLOR_IDLE.name()};")
        status.addWidget(self._drops_label)
        status.addStretch(1)
        status.addWidget(self._cadence_label)
        layout.addLayout(status)

        # Stale-preview watchdog: every preview frame restarts this timer.
        # When it fires (no preview within the threshold) the cadence label
        # flips to ``stale``. Single-shot so it self-quiesces between
        # restarts.
        self._stale_timer = QTimer(self)
        self._stale_timer.setSingleShot(True)
        self._stale_timer.timeout.connect(self._mark_stale)

    # ----------------------------------------------------------- public slots

    def update_preview(self, jpeg: bytes) -> None:
        """Render ``jpeg`` into the tile. Empty / undecodable bytes are
        silently ignored — the engine's drain task already logged it."""
        image = QImage.fromData(jpeg)
        if image.isNull():
            return
        pixmap = QPixmap.fromImage(image)
        if pixmap.width() != PREVIEW_TILE_WIDTH:
            pixmap = pixmap.scaledToWidth(
                PREVIEW_TILE_WIDTH, Qt.TransformationMode.SmoothTransformation
            )
        self._image_label.setPixmap(pixmap)
        self._image_label.setText("")
        self._has_frame = True
        # Don't overwrite a sticky ``failed`` cadence — once a recording
        # actually died, the operator must see that even if frames keep
        # arriving from a recovered pump.
        if not self._failed:
            self._cadence_label.setText("live")
            self._cadence_label.setStyleSheet(f"color: {COLOR_OK.name()};")
        self._stale_timer.start(STALE_THRESHOLD_MS)

    def note_event(self, event: CameraEvent) -> None:
        """React to a :class:`CameraEvent` for this camera.

        * ``pump_warning`` — single-frame encoder fault (libx264 EINVAL,
          format renegotiation, …). The recording continues; bump the
          drops counter and switch the border to a warning shade.
        * ``pump_failed`` — the recording itself died. Sticky red border
          + ``failed`` cadence. ``CameraSpec.on_failure`` decides whether
          the run also aborts; that path is engine-side, not our concern.
        * ``recording_stopped`` — clean shutdown. Cadence flips to
          ``stopped`` so the operator can tell the freeze frame is final.
        """
        if event.kind == "pump_warning":
            self._dropped_frames += 1
            self._drops_label.setText(f"drops: {self._dropped_frames}")
            self._drops_label.setStyleSheet(f"color: {COLOR_WARN.name()};")
            if not self._failed:
                self._set_border_warn()
        elif event.kind == "pump_failed":
            self._failed = True
            self._drops_label.setStyleSheet(f"color: {COLOR_FAIL.name()};")
            self._cadence_label.setText("failed")
            self._cadence_label.setStyleSheet(f"color: {COLOR_FAIL.name()};")
            self._set_border_fail()
        elif event.kind == "recording_stopped":
            if self._has_frame and not self._failed:
                self._cadence_label.setText("stopped")
                self._cadence_label.setStyleSheet(f"color: {COLOR_IDLE.name()};")
            # Leave ``_stale_timer`` to fire naturally; the operator
            # already sees ``stopped`` so an additional ``stale`` flip
            # is just noise.

    # ----------------------------------------------------------- internal

    def _mark_stale(self) -> None:
        if not self._has_frame or self._failed:
            return
        if self._cadence_label.text() == "stopped":
            return
        self._cadence_label.setText("stale")
        self._cadence_label.setStyleSheet(f"color: {COLOR_WARN.name()};")

    def _set_border_idle(self) -> None:
        self.setStyleSheet(f"#{self.objectName()} {{ border: 1px solid {COLOR_IDLE.name()}; }}")

    def _set_border_warn(self) -> None:
        self.setStyleSheet(f"#{self.objectName()} {{ border: 2px solid {COLOR_WARN.name()}; }}")

    def _set_border_fail(self) -> None:
        self.setStyleSheet(f"#{self.objectName()} {{ border: 2px solid {COLOR_FAIL.name()}; }}")


class CameraPreviewDock(QDockWidget):
    """Dockable grid of camera thumbnails.

    One tile per :class:`CameraSpec` in the loaded config. Allowed in all
    four dock areas; defaults to the bottom area (thumbnails-in-a-row reads
    naturally there). Layout state persists via the standard
    ``QMainWindow.saveState()`` path because the dock declares
    ``setObjectName("dock_camera_preview")``.
    """

    def __init__(
        self,
        *,
        cameras: Iterable[CameraSpec],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__("Camera previews", parent)
        self.setObjectName("dock_camera_preview")
        self.setAllowedAreas(Qt.DockWidgetArea.AllDockWidgetAreas)

        self._tiles: dict[str, _PreviewTile] = {}

        body = QWidget(self)
        # Two-column grid; main_window can drag-resize the dock and tiles
        # will re-flow into rows. Three-camera rigs still fit in a
        # bottom-mounted dock without scrolling.
        grid = QGridLayout(body)
        grid.setContentsMargins(8, 8, 8, 8)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)

        for idx, spec in enumerate(cameras):
            tile = _PreviewTile(spec, body)
            self._tiles[spec.name] = tile
            row, col = divmod(idx, 2)
            grid.addWidget(tile, row, col)

        if not self._tiles:
            placeholder = QLabel("No cameras configured", body)
            placeholder.setStyleSheet(f"color: {COLOR_IDLE.name()};")
            placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            grid.addWidget(placeholder, 0, 0)

        # Push the grid up so empty space sits at the bottom.
        grid.setRowStretch(grid.rowCount(), 1)
        grid.setColumnStretch(2, 1)

        self.setWidget(body)

    # ------------------------------------------------------------------ slots

    def update_preview(self, camera_name: str, jpeg: bytes) -> None:
        """Connected to ``RunController.preview_received``."""
        tile = self._tiles.get(camera_name)
        if tile is None:
            return
        tile.update_preview(jpeg)

    def note_event(self, event: object) -> None:
        """Connected to ``RunController.camera_event_received``.

        Accepts ``object`` because that's what the underlying ``Signal
        (object)`` delivers; defensively narrows to :class:`CameraEvent`
        and routes to the matching tile by ``event.name``. Unknown camera
        names (config reloaded mid-stream, etc.) are ignored.
        """
        if not isinstance(event, CameraEvent):
            return
        tile = self._tiles.get(event.name)
        if tile is None:
            return
        tile.note_event(event)


__all__ = [
    "PREVIEW_TILE_PLACEHOLDER_HEIGHT",
    "PREVIEW_TILE_WIDTH",
    "STALE_THRESHOLD_MS",
    "CameraPreviewDock",
]
