"""Unit tests for :mod:`capa.runtime.lifecycle`.

The lifecycle module is the single source of truth for worker and pool state
transitions.
These tests enumerate every ``(from, to)`` pair in ``WorkerState`` × ``WorkerState``
and assert membership in ``LEGAL_WORKER_EDGES`` matches the intended state
diagrams.

If the edge table changes, exactly one of these tests fails — the test file
is the regression guard for "someone added an edge but forgot the
corresponding transition assertion in Worker."
"""

from __future__ import annotations

import pytest

from capa.runtime.lifecycle import (
    LEGAL_POOL_EDGES,
    LEGAL_WORKER_EDGES,
    PoolState,
    WorkerState,
    pool_edge_legal,
    worker_edge_legal,
)


class TestWorkerEdges:
    """Edge-table assertions for the worker state machine.

    The expected legal edges match the worker lifecycle contract.
    """

    def test_legal_edges_exact_set(self) -> None:
        """Lock the legal edge set verbatim — adding an edge is a doc-level
        decision and should require explicit test update."""
        expected = {
            (WorkerState.CLOSED, WorkerState.IDLE),
            (WorkerState.IDLE, WorkerState.ARMED),
            (WorkerState.ARMED, WorkerState.SAMPLING),
            (WorkerState.SAMPLING, WorkerState.DRAINING),
            (WorkerState.ARMED, WorkerState.DRAINING),
            (WorkerState.DRAINING, WorkerState.IDLE),
            (WorkerState.IDLE, WorkerState.CLOSED),
        }
        assert set(LEGAL_WORKER_EDGES) == expected

    def test_no_self_loops(self) -> None:
        """Every transition must change state; a self-loop is always a bug
        (it would mask a missed transition in production code)."""
        for src, dst in LEGAL_WORKER_EDGES:
            assert src is not dst, f"illegal self-loop {src} → {dst}"

    def test_closed_cannot_be_re_entered_directly(self) -> None:
        """CLOSED is reachable only from IDLE. A worker can't go
        DRAINING → CLOSED, etc. — the IDLE step is the canonical "no run
        state installed" anchor for close()."""
        for src, dst in LEGAL_WORKER_EDGES:
            if dst is WorkerState.CLOSED:
                assert src is WorkerState.IDLE

    def test_closed_only_leaves_via_idle(self) -> None:
        """Mirror of the above — start() is the only edge out of CLOSED,
        and it goes to IDLE."""
        for src, dst in LEGAL_WORKER_EDGES:
            if src is WorkerState.CLOSED:
                assert dst is WorkerState.IDLE

    def test_draining_only_leads_to_idle(self) -> None:
        """DRAINING is the bounded shutdown state; it always lands in IDLE
        on clean drain. The 'forced' / 'leaked' outcomes are encoded in
        DisarmResult, NOT as additional edges out of DRAINING — the worker
        still transitions to IDLE in those cases (the run is just degraded)."""
        for src, dst in LEGAL_WORKER_EDGES:
            if src is WorkerState.DRAINING:
                assert dst is WorkerState.IDLE

    def test_sampling_only_reachable_via_armed(self) -> None:
        """The state graph requires arm() before begin_sampling();
        there is no shortcut from IDLE to SAMPLING."""
        for src, dst in LEGAL_WORKER_EDGES:
            if dst is WorkerState.SAMPLING:
                assert src is WorkerState.ARMED

    @pytest.mark.parametrize("src", list(WorkerState))
    @pytest.mark.parametrize("dst", list(WorkerState))
    def test_edge_legal_matches_table(self, src: WorkerState, dst: WorkerState) -> None:
        """Exhaustive: every product pair agrees with the helper function."""
        expected = (src, dst) in LEGAL_WORKER_EDGES
        assert worker_edge_legal(src, dst) is expected

    def test_total_legal_edges_count(self) -> None:
        """The doc's diagram lists exactly 7 edges (counting ARMED→DRAINING
        as a separate edge from SAMPLING→DRAINING). If this fails the doc
        or the table has drifted."""
        assert len(LEGAL_WORKER_EDGES) == 7


class TestPoolEdges:
    """Edge-table assertions for the pool state machine."""

    def test_legal_edges_exact_set(self) -> None:
        expected = {
            (PoolState.CLOSED, PoolState.OPENING),
            (PoolState.OPENING, PoolState.OPEN),
            (PoolState.OPENING, PoolState.CLOSING),
            (PoolState.OPEN, PoolState.CLOSING),
            (PoolState.CLOSING, PoolState.CLOSED),
        }
        assert set(LEGAL_POOL_EDGES) == expected

    def test_no_self_loops(self) -> None:
        for src, dst in LEGAL_POOL_EDGES:
            assert src is not dst

    def test_open_only_reachable_after_opening(self) -> None:
        """OPEN is the only "happy steady state"; it's reachable solely
        from OPENING, never from CLOSED directly."""
        for src, dst in LEGAL_POOL_EDGES:
            if dst is PoolState.OPEN:
                assert src is PoolState.OPENING

    def test_closed_only_reachable_via_closing(self) -> None:
        """CLOSING → CLOSED is the only inbound edge to CLOSED. This guards
        the invariant that close() always tears down workers in a defined
        order before the pool resets."""
        for src, dst in LEGAL_POOL_EDGES:
            if dst is PoolState.CLOSED:
                assert src is PoolState.CLOSING

    def test_opening_can_rollback_to_closing(self) -> None:
        """Partial open failure must be able to flow OPENING → CLOSING
        without first hitting OPEN."""
        assert (PoolState.OPENING, PoolState.CLOSING) in LEGAL_POOL_EDGES

    @pytest.mark.parametrize("src", list(PoolState))
    @pytest.mark.parametrize("dst", list(PoolState))
    def test_edge_legal_matches_table(self, src: PoolState, dst: PoolState) -> None:
        expected = (src, dst) in LEGAL_POOL_EDGES
        assert pool_edge_legal(src, dst) is expected


class TestEdgeHelpersAreReadOnly:
    """Lock immutability — the legal edge sets are imported in many places;
    a stray ``LEGAL_*.add(...)`` would corrupt every consumer."""

    def test_worker_edges_is_frozenset(self) -> None:
        assert isinstance(LEGAL_WORKER_EDGES, frozenset)

    def test_pool_edges_is_frozenset(self) -> None:
        assert isinstance(LEGAL_POOL_EDGES, frozenset)
