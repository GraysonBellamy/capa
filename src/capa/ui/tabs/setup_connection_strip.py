""":class:`ConnectionStrip` — persistent rig-connectivity surface.

The Setup tab's always-on answer to "is the rig live?". One calm
colored dot, one sentence of status, and the inline action buttons that
matter right now (typically Apply & Connect / Revert, or nothing).
Replaces the prior auto-fading multi-state banner — there is never a
moment where the operator has to guess whether the previous green flash
was acknowledged or missed.

State machine, in priority order:

* **FROZEN** (purple) — a run is in progress; the config is locked.
* **CONNECTING** (blue) — an Apply & Connect is mid-flight.
* **CHECKING** (indigo) — a read-only Verify connection is mid-flight.
* **FAILED** (red) — the most recent Apply & Connect failed; stays until
  the operator edits the draft or retries.
* **CONNECTED** (green) — hardware is ready and the draft matches the
  applied rig.
* **UNAPPLIED** (amber) — hardware-ready and draft has unsaved edits.
* **IDLE** (gray) — no config loaded.

The widget is dumb on its own: callers push :class:`ConnectionInputs`
into :meth:`update_state` and the strip resolves which state applies.
The Setup tab owns the actual state inputs (in-flight flags, draft
status, controller signals).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import assert_never

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


class ConnectionState(StrEnum):
    """Mutually-exclusive surfaces the operator can see."""

    IDLE = "idle"
    CONNECTED = "connected"
    UNAPPLIED = "unapplied"
    CONNECTING = "connecting"
    CHECKING = "checking"
    FAILED = "failed"
    FROZEN = "frozen"


@dataclass(frozen=True, slots=True)
class ConnectionInputs:
    """Snapshot of every signal the strip cares about.

    The strip recomputes its :class:`ConnectionState` from this; callers
    don't reach into private fields. ``failure_detail`` is shown in the
    FAILED state's "Details…" dialog when present.
    """

    has_config: bool
    hardware_ready: bool
    draft_unapplied: bool
    draft_dirty_count: int
    draft_has_errors: bool
    apply_in_flight: bool
    check_in_flight: bool
    controller_busy: bool
    last_apply_failed: bool
    last_apply_succeeded: bool = False
    failure_detail: str = ""
    connected_detail: str = ""


# Per-state visual styling: (dot character, primary color, css block).
_STATE_STYLES: dict[ConnectionState, tuple[str, str]] = {
    ConnectionState.IDLE: (
        "○",
        "background: #f5f5f7; color: #344054; border: 1px solid #e4e7ec;",
    ),
    ConnectionState.CONNECTED: (
        "●",
        "background: #ecfdf3; color: #027a48; border: 1px solid #abefc6;",
    ),
    ConnectionState.UNAPPLIED: (
        "◐",
        "background: #fffaeb; color: #b54708; border: 1px solid #fedf89;",
    ),
    ConnectionState.CONNECTING: (
        "◐",
        "background: #eff8ff; color: #175cd3; border: 1px solid #b2ddff;",
    ),
    ConnectionState.CHECKING: (
        "◐",
        "background: #f4f3ff; color: #5925dc; border: 1px solid #d9d6fe;",
    ),
    ConnectionState.FAILED: (
        "✗",
        "background: #fef3f2; color: #b42318; border: 1px solid #fecdca;",
    ),
    ConnectionState.FROZEN: (
        "🔒",
        "background: #f4f3ff; color: #4338ca; border: 1px solid #d9d6fe;",
    ),
}


class ConnectionStrip(QWidget):
    """Always-visible connectivity surface above the Setup outline.

    Buttons emit signals; the Setup tab owns the implementations. The
    strip never directly calls into the run controller.
    """

    applyRequested = Signal()  # noqa: N815 — Qt naming
    """Operator clicked Apply & Connect."""

    revertRequested = Signal()  # noqa: N815 — Qt naming
    """Operator clicked Revert (only shown when UNAPPLIED)."""

    detailsRequested = Signal()  # noqa: N815 — Qt naming
    """Operator clicked Details… on a FAILED state."""

    openRequested = Signal()  # noqa: N815 — Qt naming
    """Operator clicked Open config… on an IDLE state."""

    newRequested = Signal()  # noqa: N815 — Qt naming
    """Operator clicked New setup on an IDLE state."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state: ConnectionState = ConnectionState.IDLE
        self._inputs: ConnectionInputs = ConnectionInputs(
            has_config=False,
            hardware_ready=False,
            draft_unapplied=False,
            draft_dirty_count=0,
            draft_has_errors=False,
            apply_in_flight=False,
            check_in_flight=False,
            controller_busy=False,
            last_apply_failed=False,
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(2)

        line = QHBoxLayout()
        line.setSpacing(8)
        self._dot_label = QLabel("○", self)
        self._dot_label.setStyleSheet("font-size: 14pt; font-weight: 600;")
        line.addWidget(self._dot_label)

        self._text_label = QLabel("No config loaded", self)
        self._text_label.setWordWrap(True)
        self._text_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        line.addWidget(self._text_label, stretch=1)

        # Inline action buttons — visibility tracks state.
        self._open_btn = QPushButton("Open config…", self)
        self._open_btn.clicked.connect(self.openRequested)
        line.addWidget(self._open_btn)
        self._new_btn = QPushButton("New setup", self)
        self._new_btn.clicked.connect(self.newRequested)
        line.addWidget(self._new_btn)
        self._details_btn = QPushButton("Details…", self)
        self._details_btn.clicked.connect(self.detailsRequested)
        line.addWidget(self._details_btn)
        self._revert_btn = QPushButton("Revert", self)
        self._revert_btn.clicked.connect(self.revertRequested)
        line.addWidget(self._revert_btn)
        self._apply_btn = QPushButton("Apply && Connect", self)
        self._apply_btn.clicked.connect(self.applyRequested)
        line.addWidget(self._apply_btn)

        outer.addLayout(line)

        self._apply_btn.setToolTip(
            "Validate the draft, open hardware connections, and start background acquisition."
        )

        # Initial render.
        self.update_state(self._inputs)

    # -- API ----------------------------------------------------------------

    @property
    def state(self) -> ConnectionState:
        """The currently-displayed state. Exposed for tests."""
        return self._state

    def update_state(self, inputs: ConnectionInputs) -> None:
        """Recompute the displayed state from the latest inputs.

        Setup tab calls this on every signal that could change the
        strip's surface — controller state, in-flight flags, draft edits.
        """
        self._inputs = inputs
        state = self._compute_state(inputs)
        self._state = state
        dot, css = _STATE_STYLES[state]
        self._dot_label.setText(dot)
        self._text_label.setText(self._compose_text(state, inputs))
        self.setStyleSheet(f"ConnectionStrip {{ {css} border-radius: 4px; }}")

        # Button visibility per state.
        is_idle = state is ConnectionState.IDLE
        self._open_btn.setVisible(is_idle)
        self._new_btn.setVisible(is_idle)
        self._details_btn.setVisible(state is ConnectionState.FAILED)
        self._revert_btn.setVisible(state is ConnectionState.UNAPPLIED)
        # Apply & Connect button is visible on UNAPPLIED and FAILED — the
        # two states where the operator's next action is to (re-)apply.
        self._apply_btn.setVisible(state in (ConnectionState.UNAPPLIED, ConnectionState.FAILED))
        # Disable Apply when the draft has errors so the strip never
        # offers an action that will immediately bounce. Tooltip on the
        # button itself explains the disabled state.
        apply_enabled = (
            not inputs.draft_has_errors
            and not inputs.apply_in_flight
            and not inputs.controller_busy
        )
        self._apply_btn.setEnabled(apply_enabled)
        if inputs.draft_has_errors:
            self._apply_btn.setToolTip(
                "Apply is disabled — the draft has validation errors. "
                "Fix them in the Problems panel below."
            )
        elif inputs.controller_busy:
            self._apply_btn.setToolTip("Apply is disabled — a run is in progress.")
        else:
            self._apply_btn.setToolTip(
                "Validate the draft, open hardware connections, and start background acquisition."
            )

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _compute_state(inputs: ConnectionInputs) -> ConnectionState:
        """Resolve the highest-priority state from the input snapshot."""
        if inputs.controller_busy:
            return ConnectionState.FROZEN
        if inputs.apply_in_flight:
            return ConnectionState.CONNECTING
        if inputs.check_in_flight:
            return ConnectionState.CHECKING
        if inputs.last_apply_failed:
            return ConnectionState.FAILED
        if not inputs.has_config:
            return ConnectionState.IDLE
        if inputs.draft_unapplied:
            return ConnectionState.UNAPPLIED
        if inputs.hardware_ready or inputs.last_apply_succeeded:
            return ConnectionState.CONNECTED
        # Config loaded but no apply has succeeded yet and no in-flight
        # apply — equivalent to UNAPPLIED for the operator's purposes.
        return ConnectionState.UNAPPLIED

    @staticmethod
    def _compose_text(state: ConnectionState, inputs: ConnectionInputs) -> str:
        if state is ConnectionState.IDLE:
            return "No config loaded"
        if state is ConnectionState.CONNECTED:
            detail = inputs.connected_detail or "draft matches rig"
            return f"Connected — {detail}"
        if state is ConnectionState.UNAPPLIED:
            n = inputs.draft_dirty_count
            if n == 0:
                return "Config loaded — Apply & Connect to open hardware."
            return f"Draft has {n} unsaved edit(s) — Apply & Connect to take effect."
        if state is ConnectionState.CONNECTING:
            return "Connecting — opening hardware…"
        if state is ConnectionState.CHECKING:
            return "Verifying connection — read-only handshake in progress…"
        if state is ConnectionState.FAILED:
            detail = inputs.failure_detail or "see Details for the full error."
            return f"Last apply failed — {detail}"
        if state is ConnectionState.FROZEN:
            return "Run in progress — config locked until the run completes."
        assert_never(state)


__all__ = ["ConnectionInputs", "ConnectionState", "ConnectionStrip"]
