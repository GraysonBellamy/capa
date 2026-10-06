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

A tile's **Pop out** button (or a double-click on the tile) opens a
:class:`CameraPreviewWindow`: a resizable, full-screen-capable window for
setting focus by eye. While it is open the dock emits
:attr:`CameraPreviewDock.preview_detail_changed` so the camera sends
full-size frames instead of thumbnails. Pop-outs are for between runs:
starting a run closes them and disables Pop out until it ends.
"""

from __future__ import annotations

from collections.abc import Iterable
from functools import partial
from typing import Final

from PySide6.QtCore import QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import (
    QCloseEvent,
    QColor,
    QImage,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPixmap,
)
from PySide6.QtWidgets import (
    QDockWidget,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from capa.devices.camera.base import CameraEvent, CameraSpec
from capa.ui.state import RunUiState
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

_PREVIEW_BACKGROUND: Final[str] = "#1a1a1a"

_POPOUT_HINT: Final[str] = "double-click or F11: full screen   ·   Esc: leave full screen / close"

_RUN_STATES: Final[frozenset[RunUiState]] = frozenset(
    {
        RunUiState.PREPARING,
        RunUiState.RUNNING,
        RunUiState.DRAINING,
        RunUiState.FINALIZING,
    }
)
"""States in which pop-out windows are closed and can't be opened."""


class _PreviewTile(QFrame):
    """One camera's tile: name + image + drops counter + cadence indicator."""

    popout_requested = Signal()
    """The operator asked for this camera's pop-out window."""

    def __init__(self, spec: CameraSpec, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._spec: CameraSpec = spec
        self._has_frame: bool = False
        self._last_image: QImage | None = None
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
        self._popout_button = QToolButton(self)
        self._popout_button.setObjectName(f"popout_{spec.name}")
        self._popout_button.setText("Pop out")
        self._popout_button.setToolTip(
            "Open a resizable window with a full-size preview, for setting "
            "focus. Double-clicking the tile does the same. Not available "
            "during a run."
        )
        self._popout_button.clicked.connect(self.popout_requested)
        header.addWidget(name_label)
        header.addStretch(1)
        header.addWidget(kind_label)
        header.addWidget(self._popout_button)
        layout.addLayout(header)

        # Image area: QLabel with a centered pixmap. Fixed width, height
        # follows the JPEG; placeholder height for the idle state.
        self._image_label = QLabel(self)
        self._image_label.setFixedWidth(PREVIEW_TILE_WIDTH)
        self._image_label.setMinimumHeight(PREVIEW_TILE_PLACEHOLDER_HEIGHT)
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_label.setStyleSheet(
            f"background-color: {_PREVIEW_BACKGROUND}; color: {COLOR_IDLE.name()};"
        )
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

    @property
    def spec(self) -> CameraSpec:
        """The camera this tile shows."""
        return self._spec

    @property
    def last_image(self) -> QImage | None:
        """The most recent frame, or ``None`` before the first one."""
        return self._last_image

    # ----------------------------------------------------------- public slots

    def set_popout_enabled(self, enabled: bool) -> None:
        self._popout_button.setEnabled(enabled)

    def show_frame(self, image: QImage) -> None:
        """Render a decoded preview frame into the tile, scaled to the
        tile width (frames arrive full size while a pop-out is open)."""
        self._last_image = image
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

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        """Qt event handler — a double-click opens the pop-out window."""
        self.popout_requested.emit()
        event.accept()

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


class _FrameView(QWidget):
    """Paints the latest frame as large as fits, aspect preserved and
    centered. Painting directly (rather than a pixmap in a ``QLabel``)
    lets the window shrink below the frame size."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._image: QImage | None = None
        self.setMinimumSize(160, 120)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_image(self, image: QImage) -> None:
        self._image = image
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt override
        """Qt event handler — see :class:`PySide6.QtWidgets.QWidget`."""
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(_PREVIEW_BACKGROUND))
        image = self._image
        if image is None:
            painter.setPen(COLOR_IDLE)
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "no preview")
        else:
            size = image.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
            target = QRect(QPoint(0, 0), size)
            target.moveCenter(self.rect().center())
            # Smooth only when shrinking: smoothing an enlarged frame
            # softens every edge, which reads as poor focus.
            painter.setRenderHint(
                QPainter.RenderHint.SmoothPixmapTransform, size.width() < image.width()
            )
            painter.drawImage(target, image)
        painter.end()


class CameraPreviewWindow(QWidget):
    """Resizable, full-screen-capable preview of one camera.

    Opened from a tile; a child of the dock so it closes when the dock is
    rebuilt. The footer gives the frame's size in pixels: a camera that
    sends full-size frames shows its capture resolution, one that only
    sends thumbnails (no detail mode) shows the thumbnail size.
    """

    closed = Signal()
    """The operator closed the window."""

    def __init__(self, spec: CameraSpec, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.setObjectName(f"preview_window_{spec.name}")
        self.setWindowTitle(f"{spec.name} — camera preview")
        self.resize(960, 600)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._view = _FrameView(self)
        layout.addWidget(self._view, 1)
        self._footer = QLabel(self)
        self._footer.setFont(monospace_font(point_size=9))
        self._footer.setContentsMargins(8, 4, 8, 4)
        self._footer.setStyleSheet(f"color: {COLOR_IDLE.name()};")
        # Clip rather than let the hint text set the window's minimum width.
        self._footer.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        layout.addWidget(self._footer)

        self._frame_size: str = "waiting for frames"
        self._show_status(self._frame_size)
        # Same watchdog as the tile: say so when frames stop, so a frozen
        # image isn't mistaken for a focus change that did nothing.
        self._stale_timer = QTimer(self)
        self._stale_timer.setSingleShot(True)
        self._stale_timer.timeout.connect(
            lambda: self._show_status(f"{self._frame_size}, no new frames")
        )

    def show_frame(self, image: QImage) -> None:
        """Show a decoded preview frame."""
        self._view.set_image(image)
        self._frame_size = f"{image.width()} × {image.height()} px"
        self._show_status(self._frame_size)
        self._stale_timer.start(STALE_THRESHOLD_MS)

    def toggle_full_screen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def _show_status(self, status: str) -> None:
        self._footer.setText(f"{status}   ·   {_POPOUT_HINT}")

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt override
        """Qt event handler — F11 toggles full screen; Esc leaves it, or
        closes the window when not full screen."""
        if event.key() == Qt.Key.Key_F11:
            self.toggle_full_screen()
        elif event.key() == Qt.Key.Key_Escape:
            if self.isFullScreen():
                self.showNormal()
            else:
                self.close()
        else:
            super().keyPressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        """Qt event handler — a double-click toggles full screen."""
        self.toggle_full_screen()
        event.accept()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt override
        """Qt event handler — see :class:`PySide6.QtWidgets.QWidget`."""
        self._stale_timer.stop()
        super().closeEvent(event)
        self.closed.emit()


class CameraPreviewDock(QDockWidget):
    """Dockable grid of camera thumbnails.

    One tile per :class:`CameraSpec` in the loaded config. Allowed in all
    four dock areas; defaults to the bottom area (thumbnails-in-a-row reads
    naturally there). Layout state persists via the standard
    ``QMainWindow.saveState()`` path because the dock declares
    ``setObjectName("dock_camera_preview")``.
    """

    preview_detail_changed = Signal(str, bool)
    """``(camera_name, enabled)`` — a camera's pop-out window opened
    (``True``: send full-size frames) or closed (``False``: thumbnails
    again). Connected to :meth:`RunController.set_preview_detail`."""

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
        # Built on first open and kept, hidden, after the operator closes
        # one; children of the dock, so they go when it is rebuilt.
        self._popouts: dict[str, CameraPreviewWindow] = {}
        self._run_active: bool = False

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
            tile.popout_requested.connect(partial(self.open_popout, spec.name))
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
        """Connected to ``RunController.preview_received``. Decodes once
        for the tile and the camera's pop-out window. Empty / undecodable
        bytes are silently ignored — the engine's drain task already
        logged it."""
        tile = self._tiles.get(camera_name)
        if tile is None:
            return
        image = QImage.fromData(jpeg)
        if image.isNull():
            return
        tile.show_frame(image)
        window = self._popouts.get(camera_name)
        if window is not None and window.isVisible():
            window.show_frame(image)

    def open_popout(self, camera_name: str) -> None:
        """Show ``camera_name``'s pop-out window (or bring it to the
        front) and ask the camera for full-size frames. Refused during a
        run."""
        tile = self._tiles.get(camera_name)
        if tile is None or self._run_active:
            return
        window = self._popouts.get(camera_name)
        if window is None:
            window = CameraPreviewWindow(tile.spec, self)
            window.closed.connect(partial(self.preview_detail_changed.emit, camera_name, False))
            self._popouts[camera_name] = window
        if not window.isVisible():
            # Show the last thumbnail until full-size frames arrive.
            if tile.last_image is not None:
                window.show_frame(tile.last_image)
            window.show()
            self.preview_detail_changed.emit(camera_name, True)
        window.raise_()
        window.activateWindow()

    def reassert_preview_detail(self, pool: object) -> None:
        """Ask again for full-size frames for every open pop-out.

        Connected to ``RunController.pool_changed``: a window opened while
        the pool was still opening asked before any camera could hear it.
        """
        if pool is None:
            return
        for name, window in self._popouts.items():
            if window.isVisible():
                self.preview_detail_changed.emit(name, True)

    def set_run_state(self, state: object) -> None:
        """Connected to ``RunController.state_changed``.

        Starting a run closes every pop-out (each close drops its camera
        back to thumbnails) and disables Pop out until the run ends, so
        full-size previews never share the camera's pump with a recording.
        """
        if not isinstance(state, RunUiState):
            return
        self._run_active = state in _RUN_STATES
        for tile in self._tiles.values():
            tile.set_popout_enabled(not self._run_active)
        if self._run_active:
            for window in self._popouts.values():
                if window.isVisible():
                    window.close()

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
    "CameraPreviewWindow",
]
