""":class:`ConductorState` — the per-run lifecycle states.

Migration doc §3.2 (run lifetime) and §3.7 / §3.8. The :class:`Conductor`
exposes its current state so callers (UI status bar in Phase 4, CLI logs,
tests) can observe the run's macro phase without inspecting internal
attributes.

The shape intentionally mirrors today's
:class:`~capa.experiment.engine.EngineState` so the eventual UI cutover
(Phase 4) is a pure rename rather than a redesign. Two differences:

* No ``IDLE`` value. A :class:`Conductor` is constructed per-run; "no
  conductor" already means "no run". A pre-run state would be dead code.
* :meth:`permits_dispatch` lives here, codifying the migration doc §3.5
  rule that commands are accepted in PREPARING / RUNNING but refused in
  DRAINING / FINALIZING (and later).
"""

from __future__ import annotations

import enum
from typing import Final


class ConductorState(enum.StrEnum):
    """Live conductor state.

    Linear progression with one bypass: any state may transition to
    :attr:`FAILED` on an unrecoverable error. Operator-requested stops
    move through :attr:`DRAINING` like any normal completion.
    """

    PREPARING = "preparing"
    """Conductor thread spawned; arming pool workers, opening writer/bundle,
    running static preflight, spawning drain tasks."""

    RUNNING = "running"
    """Procedure / runner active; drain tasks pumping; saturation monitor
    armed. Dispatch is permitted."""

    DRAINING = "draining"
    """Procedure complete (or stop requested). Disarming workers; bridges
    drain then close. Dispatch is refused (migration doc §3.5)."""

    FINALIZING = "finalizing"
    """Workers IDLE; writer-thread closing; bundle finalizing (Parquet
    rewrite, integrity hashes). Dispatch is refused."""

    SEALED = "sealed"
    """Bundle finalized. Run outcome (completed / aborted / crashed) is on
    :class:`RunResult.run_status` — this state only signals "lifecycle
    machine is done". Terminal."""

    FAILED = "failed"
    """The conductor itself failed before sealing — preflight refusal,
    pool-arm failure, writer-thread death. Terminal; bundle may be
    partially written but is not sealed."""

    def permits_dispatch(self) -> bool:
        """Migration doc §3.5: commands accepted only during PREPARING and
        RUNNING. PREPARING is included so dynamic preflight (which may need
        to settle a setpoint) can issue commands before the procedure starts.
        """
        return self in (ConductorState.PREPARING, ConductorState.RUNNING)

    def is_terminal(self) -> bool:
        return self in (ConductorState.SEALED, ConductorState.FAILED)


# Legal edges. Any state may move to FAILED. The forward path is otherwise
# strictly linear: PREPARING → RUNNING → DRAINING → FINALIZING → SEALED.
# An operator stop during PREPARING short-circuits straight to DRAINING (no
# RUNNING) — we never lie about having run a procedure that never started.
LEGAL_CONDUCTOR_EDGES: Final[frozenset[tuple[ConductorState, ConductorState]]] = frozenset(
    {
        (ConductorState.PREPARING, ConductorState.RUNNING),
        (ConductorState.PREPARING, ConductorState.DRAINING),
        (ConductorState.RUNNING, ConductorState.DRAINING),
        (ConductorState.DRAINING, ConductorState.FINALIZING),
        (ConductorState.FINALIZING, ConductorState.SEALED),
        # Failure can interrupt any non-terminal state.
        (ConductorState.PREPARING, ConductorState.FAILED),
        (ConductorState.RUNNING, ConductorState.FAILED),
        (ConductorState.DRAINING, ConductorState.FAILED),
        (ConductorState.FINALIZING, ConductorState.FAILED),
    }
)


def conductor_edge_legal(src: ConductorState, dst: ConductorState) -> bool:
    """Return ``True`` if transitioning ``src → dst`` is allowed."""
    return (src, dst) in LEGAL_CONDUCTOR_EDGES


__all__ = [
    "LEGAL_CONDUCTOR_EDGES",
    "ConductorState",
    "conductor_edge_legal",
]
