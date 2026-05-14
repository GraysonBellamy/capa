""":class:`DeviceCard` — shared scaffolding for per-device manual-control cards.

Each card is a :class:`QGroupBox` rendered into the
:class:`~capa.ui.docks.manual_control.ManualControlDock`. The base owns:

* header layout (name, model, optional status indicator),
* the inline result label (color-coded by accepted / rejected),
* the engine-state gate (writes disabled while a run is active),
* the operator-id / authorization plumbing,
* the shared command-dispatch entry point used by every action button.

Subclasses implement :meth:`build_capability_sections` to add per-adapter
widget groups inside the card body.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

import structlog
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QGroupBox,
    QLabel,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from capa.devices.adapter import (
    Capability,
    CommandResult,
    DeviceAdapter,
)
from capa.devices.camera.base import Camera
from capa.devices.records import DeviceEvent
from capa.experiment.authorization import Authorization, AuthorizationError
from capa.ui.async_util import schedule_bg
from capa.ui.state import RunController, RunUiState
from capa.ui.statusbar import OperatorIdProvider
from capa.ui.theme import COLOR_FAIL, COLOR_IDLE, COLOR_OK, COLOR_WARN, monospace_font

# A "command target" — anything that exposes the shared command-dispatch
# entry point. Both :class:`DeviceAdapter` and :class:`Camera` qualify,
# but the manual control panel doesn't need to distinguish them at the
# dispatch site, so we type the cached handle permissively.
CommandTarget = DeviceAdapter | Camera

_logger = structlog.get_logger("capa.ui.manual")

# UI states during which writes are NEVER routed through the manual
# panel. The plan's principle (handoff §1.3): manual commands during a run
# go through procedure steps, not the panel.
_WRITE_BLOCKED_STATES: Final[frozenset[RunUiState]] = frozenset(
    {
        RunUiState.PREPARING,
        RunUiState.RUNNING,
        RunUiState.DRAINING,
        RunUiState.FINALIZING,
    }
)


class DeviceCard(QGroupBox):
    """Base class for per-device manual-control cards.

    Subclasses populate the card via :meth:`build_capability_sections`,
    using :meth:`dispatch` to issue commands. The base handles:

    * lazy adapter acquire from the shared registry,
    * engine-state-driven enable/disable,
    * destructive-operation confirmation dialogs,
    * result rendering in the inline status label,
    * synthesized :class:`DeviceEvent` emission for the events dock.
    """

    def __init__(
        self,
        *,
        name: str,
        title: str,
        controller: RunController,
        operator_provider: OperatorIdProvider,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(title, parent)
        self.setObjectName(f"manual_card_{name}")
        self._name: str = name
        self._controller: RunController = controller
        self._operator_provider: OperatorIdProvider = operator_provider
        self._adapter: CommandTarget | None = None
        # Track every section widget so we can disable them en masse when
        # the engine is in a write-blocked state.
        self._action_widgets: list[QWidget] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        # Subtitle area (subclass-populated): shows model/serial/last-cal/etc.
        self._subtitle_label = QLabel("", self)
        self._subtitle_label.setFont(monospace_font(point_size=9))
        self._subtitle_label.setWordWrap(True)
        self._subtitle_label.setStyleSheet(f"color: {COLOR_IDLE.name()};")
        outer.addWidget(self._subtitle_label)

        # Capability-driven sections live under this layout. Subclasses
        # append to it via the section helpers.
        self._sections_layout = QVBoxLayout()
        self._sections_layout.setSpacing(4)
        outer.addLayout(self._sections_layout)

        # Inline status: last-command outcome.
        self._status_label = QLabel("idle", self)
        self._status_label.setFont(monospace_font(point_size=9))
        self._status_label.setStyleSheet(f"color: {COLOR_IDLE.name()};")
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignLeft)
        outer.addWidget(self._status_label)

        # React to engine-state transitions.
        self._controller.state_changed.connect(self._on_engine_state)
        ready_signal = getattr(self._controller, "hardware_ready_changed", None)
        if ready_signal is not None:
            ready_signal.connect(self._on_hardware_ready_changed)

    # ------------------------------------------------------------------ subclass hooks

    @property
    def device_name(self) -> str:
        return self._name

    def set_subtitle(self, text: str) -> None:
        """Subclasses call this with the live model / serial / readback line."""
        self._subtitle_label.setText(text)

    def add_section(self, title: str) -> QVBoxLayout:
        """Append a labeled subsection to the card body and return its layout.

        Subclasses use this to group capability-driven widgets ("Tare/Zero",
        "Setpoint", "Valves", ...) so the card stays scannable.
        """
        header = QLabel(f"── {title} ──", self)
        header.setFont(monospace_font(point_size=9))
        header.setStyleSheet(f"color: {COLOR_IDLE.name()};")
        self._sections_layout.addWidget(header)
        body = QVBoxLayout()
        body.setSpacing(2)
        body.setContentsMargins(0, 0, 0, 4)
        self._sections_layout.addLayout(body)
        return body

    def register_action_widget(self, widget: QWidget) -> None:
        """Subclasses register every button / payload widget here so the
        base can disable them as a group when the engine blocks writes."""
        self._action_widgets.append(widget)
        widget.setEnabled(self._manual_controls_enabled())

    # ------------------------------------------------------------------ adapter handle

    async def _ensure_adapter(self) -> CommandTarget | None:
        """Probe for liveness; pure no-op for device cards.

        Phase 4: cards no longer hold adapter references. Dispatch goes
        through :class:`ManualClient` which routes to the
        :class:`WorkerPool`'s worker for the device. The wrapper / adapter
        instance lives in the worker thread; cards never see it directly.

        Returns a sentinel non-``None`` to satisfy the legacy contract that
        cards check before dispatch (camera subclasses override this to
        return the live :class:`Camera` handle they need for preview
        subscriptions). For device cards the actual dispatch happens via
        :meth:`dispatch` → ``ManualClient.dispatch``, which doesn't need
        the cached handle.
        """
        client = self._controller.manual_client
        if client is None:
            self._set_status("no config loaded — open a config first", level="warn")
            return None
        # Return the client itself as the sentinel — callers (camera
        # subclasses) override this method when they need a real handle.
        return client  # type: ignore[return-value]

    # ------------------------------------------------------------------ dispatch

    async def dispatch(
        self,
        *,
        kind: str,
        payload: dict[str, Any] | None = None,
        target: str | None = None,
        destructive: bool = False,
        destructive_summary: str | None = None,
    ) -> CommandResult | None:
        """Issue one :class:`DeviceCommand` through the adapter.

        Returns the :class:`CommandResult` on success, or ``None`` if the
        operation was refused (no operator id, run-state gate, declined
        confirm, etc.) — caller should not re-read state on ``None`` since
        nothing changed.

        ``destructive`` triggers a :class:`QMessageBox` confirmation
        showing ``destructive_summary`` (or a generated default). Sartorius
        DANGEROUS / PERSISTENT verbs and Alicat valve-closed / totalizer
        verbs use this.
        """
        # Refuse during an active run. The same gate also drops the action
        # widgets so this is belt-and-braces.
        if self._engine_blocks_writes():
            self._set_status("run in progress — manual writes disabled", level="warn")
            return None
        if not self._hardware_ready_for_writes():
            self._set_status("hardware initializing — manual writes disabled", level="warn")
            return None

        operator = self._operator_provider.current_operator_id()
        if not operator:
            self._set_status(
                "operator id required (set in config or status bar)",
                level="warn",
            )
            return None

        if destructive:
            summary = destructive_summary or f"{kind} on {self._name}"
            answer = QMessageBox.question(
                self,
                "Confirm device write",
                (
                    f"Confirm destructive operation:\n\n  {summary}\n\n"
                    f"Operator: {operator}\n\n"
                    "This may persist to EEPROM or otherwise alter device "
                    "state in a way that survives power-cycle."
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self._set_status("cancelled", level="idle")
                return None

        adapter = await self._ensure_adapter()
        if adapter is None:
            return None

        client = self._controller.manual_client
        if client is None:
            self._set_status("no config loaded — open a config first", level="warn")
            return None

        # Build the command via the run-arm-free manual-issue path. The
        # Authorization helper enforces the issued_by + confirmed_by
        # invariant for us — both stamps are the same operator in P1.
        auth = Authorization(operator_id=operator, run_id="manual")
        try:
            cmd = auth.issue_manual(
                kind=kind,
                target=target,
                payload=payload or {},
                issued_by=operator,
                confirmed_by=operator,
            )
        except AuthorizationError as exc:
            self._set_status(f"authorization: {exc}", level="error")
            return None

        try:
            result = await client.dispatch(self._name, cmd)
        except Exception as exc:
            self._set_status(f"{kind} failed: {exc}", level="error")
            self._emit_manual_event(
                kind=kind,
                severity="error",
                message=str(exc),
            )
            _logger.warning(
                "manual.dispatch_failed",
                device=self._name,
                kind=kind,
                error=str(exc),
            )
            return None

        if result.accepted:
            self._set_status(f"✓ {kind}: {result.detail}", level="ok")
            self._emit_manual_event(kind=kind, severity="info", message=result.detail)
        else:
            self._set_status(f"✗ {kind} rejected: {result.detail}", level="warn")
            self._emit_manual_event(kind=kind, severity="warning", message=result.detail)
        return result

    def schedule_dispatch(
        self,
        *,
        kind: str,
        payload: dict[str, Any] | None = None,
        target: str | None = None,
        destructive: bool = False,
        destructive_summary: str | None = None,
    ) -> None:
        """Sync entry point for Qt button slots. Wraps :meth:`dispatch`
        as an asyncio task on the running qasync loop. Errors are surfaced
        through :meth:`dispatch`'s own handling; this thin wrapper just
        keeps slot connections terse."""
        task = schedule_bg(
            self.dispatch(
                kind=kind,
                payload=payload,
                target=target,
                destructive=destructive,
                destructive_summary=destructive_summary,
            )
        )
        if task is None:
            self._set_status("no event loop — UI not running?", level="error")

    # ------------------------------------------------------------------ state surface

    def _engine_blocks_writes(self, state: RunUiState | None = None) -> bool:
        return (state or self._controller.state) in _WRITE_BLOCKED_STATES

    def _hardware_ready_for_writes(self) -> bool:
        return bool(getattr(self._controller, "hardware_ready", True))

    def _manual_controls_enabled(self, state: RunUiState | None = None) -> bool:
        return self._hardware_ready_for_writes() and not self._engine_blocks_writes(state)

    def _sync_action_widgets(self, state: RunUiState | None = None) -> None:
        enabled = self._manual_controls_enabled(state)
        for widget in self._action_widgets:
            widget.setEnabled(enabled)

    def _on_engine_state(self, state: object) -> None:
        if not isinstance(state, RunUiState):
            return
        blocked = state in _WRITE_BLOCKED_STATES
        self._sync_action_widgets(state)
        if blocked:
            self._set_status(f"run {state.value} — manual writes disabled", level="warn")
        elif self._status_label.text().endswith("manual writes disabled"):
            # Clear the gate message on return to idle so the next action
            # starts from a clean slate.
            self._set_status("ready", level="idle")

    def _on_hardware_ready_changed(self, ready: bool) -> None:
        self._sync_action_widgets()
        if ready:
            if self._status_label.text().endswith("manual writes disabled"):
                self._set_status("ready", level="idle")
        else:
            self._set_status("hardware initializing — manual writes disabled", level="warn")

    # ------------------------------------------------------------------ status surface

    def _set_status(
        self,
        text: str,
        *,
        level: str = "idle",  # "idle" | "ok" | "warn" | "error"
    ) -> None:
        self._status_label.setText(text)
        color = {
            "ok": COLOR_OK,
            "warn": COLOR_WARN,
            "error": COLOR_FAIL,
            "idle": COLOR_IDLE,
        }.get(level, COLOR_IDLE)
        self._status_label.setStyleSheet(f"color: {color.name()};")

    def _emit_manual_event(self, *, kind: str, severity: str, message: str) -> None:
        """Mirror this command to the events dock as a :class:`DeviceEvent`.

        ``t_mono_ns`` is zero because manual events fire outside any run
        clock. The events dock renders that as the column ``0.000s`` —
        visually distinct from in-run events, which start at small positive
        values.
        """
        event = DeviceEvent(
            adapter=type(self).__name__.replace("Card", "").lower(),
            device=self._name,
            t_mono_ns=0,
            t_utc=datetime.now(UTC),
            kind=f"manual.{kind}",
            message=message,
            severity=severity,
            metadata={"manual": True},
        )
        self._controller.emit_manual_event(event)


def has_any_capability(capabilities: frozenset[Capability], flags: list[Capability]) -> bool:
    """Convenience: ``True`` if any of ``flags`` is in ``capabilities``.

    Used by the dock to decide whether to render a card at all — if an
    adapter advertises none of the manual-control-relevant flags, we don't
    waste vertical space on an empty card.
    """
    return any(f in capabilities for f in flags)


__all__ = ["DeviceCard", "has_any_capability"]
