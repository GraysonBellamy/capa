"""Tests for :mod:`capa.runtime.state` — :class:`ConductorState` and edges."""

from __future__ import annotations

import pytest

from capa.runtime.state import (
    LEGAL_CONDUCTOR_EDGES,
    ConductorState,
    conductor_edge_legal,
)


class TestConductorState:
    def test_string_values_are_stable(self) -> None:
        # The UI eventually renders these strings; once shipped they can't
        # change without breaking dashboards.
        assert ConductorState.PREPARING.value == "preparing"
        assert ConductorState.RUNNING.value == "running"
        assert ConductorState.DRAINING.value == "draining"
        assert ConductorState.FINALIZING.value == "finalizing"
        assert ConductorState.SEALED.value == "sealed"
        assert ConductorState.FAILED.value == "failed"

    @pytest.mark.parametrize(
        "state, expected",
        [
            (ConductorState.PREPARING, True),
            (ConductorState.RUNNING, True),
            (ConductorState.DRAINING, False),
            (ConductorState.FINALIZING, False),
            (ConductorState.SEALED, False),
            (ConductorState.FAILED, False),
        ],
    )
    def test_permits_dispatch(self, state: ConductorState, expected: bool) -> None:
        assert state.permits_dispatch() is expected

    @pytest.mark.parametrize(
        "state, expected",
        [
            (ConductorState.PREPARING, False),
            (ConductorState.RUNNING, False),
            (ConductorState.DRAINING, False),
            (ConductorState.FINALIZING, False),
            (ConductorState.SEALED, True),
            (ConductorState.FAILED, True),
        ],
    )
    def test_is_terminal(self, state: ConductorState, expected: bool) -> None:
        assert state.is_terminal() is expected


class TestLegalEdges:
    def test_normal_path_is_legal(self) -> None:
        assert conductor_edge_legal(ConductorState.PREPARING, ConductorState.RUNNING)
        assert conductor_edge_legal(ConductorState.RUNNING, ConductorState.DRAINING)
        assert conductor_edge_legal(ConductorState.DRAINING, ConductorState.FINALIZING)
        assert conductor_edge_legal(ConductorState.FINALIZING, ConductorState.SEALED)

    def test_short_circuit_to_draining_from_preparing(self) -> None:
        """An operator stop during preflight goes PREPARING → DRAINING
        without ever entering RUNNING — we never lie about having run."""
        assert conductor_edge_legal(ConductorState.PREPARING, ConductorState.DRAINING)

    @pytest.mark.parametrize(
        "src",
        [
            ConductorState.PREPARING,
            ConductorState.RUNNING,
            ConductorState.DRAINING,
            ConductorState.FINALIZING,
        ],
    )
    def test_any_nonterminal_state_can_fail(self, src: ConductorState) -> None:
        assert conductor_edge_legal(src, ConductorState.FAILED)

    @pytest.mark.parametrize(
        "src, dst",
        [
            # Skipping states is illegal.
            (ConductorState.PREPARING, ConductorState.FINALIZING),
            (ConductorState.PREPARING, ConductorState.SEALED),
            (ConductorState.RUNNING, ConductorState.SEALED),
            # Backwards is illegal.
            (ConductorState.RUNNING, ConductorState.PREPARING),
            (ConductorState.DRAINING, ConductorState.RUNNING),
            # Out of terminal states is illegal.
            (ConductorState.SEALED, ConductorState.PREPARING),
            (ConductorState.FAILED, ConductorState.PREPARING),
            (ConductorState.SEALED, ConductorState.FAILED),
        ],
    )
    def test_illegal_edges(self, src: ConductorState, dst: ConductorState) -> None:
        assert not conductor_edge_legal(src, dst)

    def test_terminal_states_have_no_outgoing_legal_edges(self) -> None:
        for src in (ConductorState.SEALED, ConductorState.FAILED):
            for dst in ConductorState:
                assert not conductor_edge_legal(src, dst)

    def test_self_loops_not_legal(self) -> None:
        # Conductor._transition treats same-state as a no-op, but the table
        # itself doesn't permit them — keeps the table semantically minimal.
        for state in ConductorState:
            assert (state, state) not in LEGAL_CONDUCTOR_EDGES
