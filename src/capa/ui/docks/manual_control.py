""":class:`ManualControlDock` — single dock hosting per-device manual-control cards.

The dock is rebuilt on every config-load (same
pattern as :class:`NumericsDock` and :class:`CameraPreviewDock`): the old
cards are torn down, fresh ones are built for the new config's
``hardware.devices`` and ``hardware.cameras``. Cards are gated reflectively
on the adapter's :class:`Capability` flagset — devices that advertise no
manual-relevant capability are skipped.

The dock does **not** open any adapter connections. Cards dispatch through
the controller's :class:`ManualClient`, which routes to the
:class:`WorkerPool` (between runs) or to the :class:`Conductor` (during a
run, with bundle recording + state gating).
"""

from __future__ import annotations

import structlog
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDockWidget,
    QLabel,
    QMainWindow,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from capa.devices.adapter import Capability
from capa.experiment.config import ExperimentConfig
from capa.ui.async_util import schedule_bg
from capa.ui.manual.cards.alicat import (
    AlicatCard,
    is_alicat_device,
)
from capa.ui.manual.cards.balance import (
    BalanceCard,
    is_balance_device,
)
from capa.ui.manual.cards.base import (
    DeviceCard,
    has_any_capability,
)
from capa.ui.manual.cards.camera import (
    FlirCard,
    camera_has_manual_controls,
)
from capa.ui.manual.cards.fuji import (
    FujiCard,
    is_fuji_device,
)
from capa.ui.manual.cards.watlow import (
    HeaterCard,
    is_heater_device,
)
from capa.ui.manual.cards.webcam import (
    WebcamCard,
    is_webcam_camera,
)
from capa.ui.state import RunController
from capa.ui.statusbar import OperatorIdProvider
from capa.ui.theme import COLOR_IDLE, monospace_font

_logger = structlog.get_logger("capa.ui.manual_dock")


class ManualControlDock(QDockWidget):
    """Vertically-stacked, scrollable list of per-device manual-control cards.

    Rebuilt on each :meth:`load_config` call. Cards subscribe to engine
    state themselves; the dock owns only the layout and the empty-state
    placeholder.
    """

    def __init__(
        self,
        *,
        controller: RunController,
        operator_provider: OperatorIdProvider,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__("Manual Control", parent)
        self.setObjectName("dock_manual_control")
        self.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea
            | Qt.DockWidgetArea.RightDockWidgetArea
            | Qt.DockWidgetArea.BottomDockWidgetArea
            | Qt.DockWidgetArea.TopDockWidgetArea
        )
        self._controller: RunController = controller
        self._operator_provider: OperatorIdProvider = operator_provider
        self._cards_by_name: dict[str, DeviceCard] = {}
        self._controller.pool_changed.connect(self._on_pool_changed)
        self._controller.device_settings_applied.connect(self.refresh_readbacks)

        # Outer scroll area so the dock stays usable when many cards stack.
        # The horizontal bar stays available: Qt lets a dock shrink below
        # its cards' minimum width, and without the bar the right-hand
        # controls would be clipped out of reach.
        outer_widget = QWidget(self)
        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setWidget(outer_widget)
        self.setWidget(self._scroll)
        # Re-fit on show: the dock may have been hidden, or restored
        # narrower, when the cards were built.
        self.visibilityChanged.connect(self._on_visibility_changed)

        self._cards_layout = QVBoxLayout(outer_widget)
        self._cards_layout.setContentsMargins(6, 6, 6, 6)
        self._cards_layout.setSpacing(8)

        self._empty_label = QLabel(
            "No config loaded.\n\nOpen a config from File → Open Config…",
            outer_widget,
        )
        self._empty_label.setFont(monospace_font(point_size=10))
        self._empty_label.setStyleSheet(f"color: {COLOR_IDLE.name()};")
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cards_layout.addWidget(self._empty_label)
        self._cards_layout.addStretch(1)

    # ------------------------------------------------------------------ slots

    def load_config(self, config: ExperimentConfig) -> None:
        """Rebuild cards for the given config. Tears down any previous
        cards (no live adapters were owned — the registry holds those)."""
        self._clear_cards()

        device_caps_to_check = [
            Capability.HAS_TARE,
            Capability.HAS_ZERO,
            Capability.HAS_INTERNAL_CAL,
            Capability.HAS_PARAMETER_CONFIG,
            Capability.HAS_SETPOINT,
            Capability.HAS_GAS_SELECT,
            Capability.HAS_VALVE_HOLD,
            Capability.HAS_TOTALIZER,
            Capability.HAS_DISPLAY_CONTROL,
            Capability.HAS_GAS_CALIBRATION,
        ]

        any_added = False
        pool = self._controller.worker_pool

        for dev in config.hardware.devices:
            # If the pool is open we can read the worker-hosted adapter's
            # declared capabilities; otherwise (pool still opening, or
            # opening failed) fall back to the adapter-string fingerprint
            # to pick the right card class.
            opened = None
            if pool is not None:
                try:
                    worker = pool.worker_for(dev.name)
                    opened = worker.adapters.get(dev.name)
                except Exception:
                    opened = None
            caps: frozenset[Capability] = (
                getattr(opened, "capabilities", frozenset()) if opened is not None else frozenset()
            )
            if opened is not None and not has_any_capability(caps, device_caps_to_check):
                continue  # adapter declared no manual controls; skip.

            card: DeviceCard | None = None
            if is_balance_device(dev):
                card = BalanceCard(
                    spec=dev,
                    controller=self._controller,
                    operator_provider=self._operator_provider,
                )
            elif is_alicat_device(dev):
                card = AlicatCard(
                    spec=dev,
                    controller=self._controller,
                    operator_provider=self._operator_provider,
                )
            elif is_heater_device(dev):
                card = HeaterCard(
                    spec=dev,
                    controller=self._controller,
                    operator_provider=self._operator_provider,
                )
            elif is_fuji_device(dev):
                card = FujiCard(
                    spec=dev,
                    controller=self._controller,
                    operator_provider=self._operator_provider,
                )

            if card is not None:
                self._add_card(card)
                any_added = True

        for cam in config.hardware.cameras:
            cam_card: DeviceCard | None = None
            if is_webcam_camera(cam):
                # Visible cameras always get a card (STREAM_FORMAT is a
                # baseline capability the adapter advertises unconditionally
                # — even without duvc-ctl matches the operator can change
                # the recording resolution / framerate between runs).
                cam_card = WebcamCard(
                    spec=cam,
                    controller=self._controller,
                    operator_provider=self._operator_provider,
                )
            elif camera_has_manual_controls(None, cam):
                cam_card = FlirCard(
                    spec=cam,
                    controller=self._controller,
                    operator_provider=self._operator_provider,
                )
            if cam_card is not None:
                self._add_card(cam_card)
                any_added = True

        if any_added:
            self._empty_label.hide()
        else:
            self._empty_label.setText("No devices with manual controls in this config.")
            self._empty_label.show()

        # Schedule a best-effort readback refresh for cards that already
        # have an open adapter (registry-shared with a recent run).
        self.refresh_readbacks()

        # Deferred so the fit sees the settled layout: MainWindow adds
        # its other right-hand docks after this call returns.
        QTimer.singleShot(0, self, self.fit_width_to_cards)

    def fit_width_to_cards(self) -> None:
        """Widen the dock until the widest card shows without clipping.

        Only ever widens: an operator who narrows the dock afterwards
        gets the horizontal scrollbar instead. No-op while floating or
        hidden, since only a docked, laid-out dock has a width to fix.
        """
        main_window = self.parentWidget()
        if not isinstance(main_window, QMainWindow) or self.isFloating() or not self.isVisible():
            return
        content = self._scroll.widget()
        if content is None:
            return
        # Reserve the vertical bar whether or not it shows yet, so the
        # fit survives cards stacking taller than the dock.
        needed = (
            content.minimumSizeHint().width()
            + self._scroll.verticalScrollBar().sizeHint().width()
            + 2 * self._scroll.frameWidth()
        )
        deficit = needed - self._scroll.width()
        if deficit > 0:
            main_window.resizeDocks([self], [self.width() + deficit], Qt.Orientation.Horizontal)

    def _on_visibility_changed(self, visible: bool) -> None:
        if visible:
            QTimer.singleShot(0, self, self.fit_width_to_cards)

    def _on_pool_changed(self, pool: object) -> None:
        if pool is not None:
            self.refresh_readbacks()

    def card_for(self, name: str) -> DeviceCard | None:
        """Return the card for ``name`` if one was built, else ``None``.
        Used by MainWindow to scroll to a card after a Setup-tab right-
        click."""
        return self._cards_by_name.get(name)

    def reveal(self, name: str) -> None:
        """Make the dock visible and scroll the named card into view."""
        if not self.isVisible():
            self.show()
        card = self._cards_by_name.get(name)
        if card is None:
            return
        # ensureWidgetVisible centers the card in the viewport.
        self._scroll.ensureWidgetVisible(card)

    # ------------------------------------------------------------------ internal

    def _add_card(self, card: DeviceCard) -> None:
        # Insert before the trailing stretch so cards remain top-aligned.
        # The stretch is always the last child (added in __init__).
        insert_at = self._cards_layout.count() - 1
        self._cards_layout.insertWidget(insert_at, card)
        self._cards_by_name[card.device_name] = card

    def _clear_cards(self) -> None:
        for card in self._cards_by_name.values():
            self._cards_layout.removeWidget(card)
            card.deleteLater()
        self._cards_by_name.clear()

    def refresh_readbacks(self, *_args: object) -> None:
        """Re-read every card's device in the background — on pool open,
        and after device settings were applied."""
        if not self._cards_by_name:
            return

        async def _refresh_all() -> None:
            for card in list(self._cards_by_name.values()):
                refresh = getattr(card, "refresh_readback", None)
                if callable(refresh):
                    try:
                        await refresh()
                    except Exception as exc:
                        _logger.debug(
                            "manual_dock.readback_failed",
                            device=card.device_name,
                            error=str(exc),
                        )

        schedule_bg(_refresh_all())


__all__ = ["ManualControlDock"]
