""":func:`build_workers` — group resolved adapters into per-resource workers.

Materialization (TOML rows → constructed adapters) lives in
:mod:`capa.devices.materialize`. This module accepts a
:class:`~capa.devices.materialize.MaterializedHardware` and produces
unstarted :class:`~capa.runtime.worker.Worker` instances grouped by
``resource_id``. Resource-conflict validation runs synchronously here,
**before any worker thread spawns**: a misconfigured config fails fast
with no hardware side-effects. Any :class:`ResourceConflict` raised
propagates out of :meth:`WorkerPool.open` before the pool's state moves
out of CLOSED.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping
from typing import Final

import structlog

from capa.config.problems import ConfigProblem
from capa.devices.materialize import MaterializedHardware, collect_resource_problems
from capa.devices.resolved import ResolvedAdapter
from capa.experiment.config import RuntimeConfig
from capa.runtime.bridge import ThreadBridge
from capa.runtime.errors import ResourceConflict
from capa.runtime.preview import PreviewFrame
from capa.runtime.runner import ThreadedRunner, WorkerRunner
from capa.runtime.worker import Worker

_logger = structlog.get_logger("capa.runtime.build")


_BRIDGE_CAPACITY_FACTOR: Final[float] = 8.0
"""How many seconds of emission headroom the worker outbound bridge
holds at the adapter's declared rate. Documented at
[runtime-architecture.md §13](docs/runtime-architecture.md). Internal
constant — not part of the user-facing :class:`RuntimeConfig`."""

_BRIDGE_MIN_CAPACITY: Final[int] = 64
"""Floor for the per-worker outbound bridge. Below ~64 slots a transient
GC pause or scheduler hiccup pushes BLOCK policy bridges into the
saturation deadline path. Internal constant."""


def build_workers(
    materialized: MaterializedHardware,
    *,
    runner_factory: Callable[..., WorkerRunner] | None = None,
    preview_bridges: Mapping[str, ThreadBridge[PreviewFrame]] | None = None,
) -> tuple[dict[str, Worker], dict[str, str]]:
    """Build one :class:`Worker` per ``resource_id`` group.

    Returns ``(workers, device_to_resource)`` where:

    * ``workers`` keys are ``resource_id`` strings; values are unstarted
      :class:`Worker` instances.
    * ``device_to_resource`` maps each adapter name (devices + cameras)
      to its ``resource_id`` — the lookup the pool uses to route
      :meth:`WorkerPool.dispatch` calls.

    Validation runs *before* any :class:`Worker` is instantiated. A
    :class:`ResourceConflict` raised mid-validation is observable to
    :meth:`WorkerPool.open` and surfaces with the offending adapter
    names, so the operator's TOML fix is unambiguous.

    Per-worker outbound bridge capacity is derived from the sum of each
    adapter's :attr:`DeviceAdapter.expected_emission_rate_hz`:
    ``max(_BRIDGE_MIN_CAPACITY, ceil(_BRIDGE_CAPACITY_FACTOR * total_rate))``.
    Adapters that return ``None`` contribute nothing to the sum; a
    worker whose adapters all return ``None`` falls back to the floor.

    ``runner_factory`` defaults to :class:`ThreadedRunner` — production
    use. Tests pass :class:`InlineRunner` (or a class compatible with the
    runner protocol) to get deterministic, single-loop behaviour.
    """
    _validate_resources(materialized)

    by_resource: dict[str, list[ResolvedAdapter]] = {}
    device_to_resource: dict[str, str] = {}
    for r in materialized.adapters:
        by_resource.setdefault(r.resource_id, []).append(r)
        device_to_resource[r.name] = r.resource_id

    preview_bridges = preview_bridges or {}
    workers: dict[str, Worker] = {}
    for resource_id, group in by_resource.items():
        runner_name = f"worker-{resource_id}"
        runner: WorkerRunner = (
            runner_factory(name=runner_name)
            if runner_factory is not None
            else ThreadedRunner(name=runner_name)
        )
        # Partition preview bridges by adapter name onto each worker.
        # Non-camera workers receive an empty map; camera workers
        # receive the bridge keyed by their camera spec name.
        per_worker_previews = {
            r.name: preview_bridges[r.name] for r in group if r.name in preview_bridges
        }
        capacity = _outbound_capacity_for(r.expected_rate_hz for r in group)
        on_failure = {r.name: r.on_failure for r in group}
        workers[resource_id] = Worker(
            resource_id=resource_id,
            adapters=[r.adapter for r in group],
            runner=runner,
            outbound_capacity=capacity,
            preview_bridges=per_worker_previews,
            on_failure=on_failure,
        )

    _logger.info(
        "runtime.build_workers",
        worker_count=len(workers),
        adapter_count=len(materialized.adapters),
        resources=tuple(workers),
    )
    return workers, device_to_resource


def _outbound_capacity_for(rates: Iterable[float | None]) -> int:
    """Compute the worker outbound bridge capacity from declared rates.

    Adapters that decline to declare a rate (return ``None``) contribute
    nothing; a worker whose adapters all decline falls back to the
    minimum floor. The formula matches the documented
    ``bridge_capacity_factor`` at [runtime-architecture.md §13]
    (docs/runtime-architecture.md).
    """
    total = sum(r for r in rates if r is not None)
    if total <= 0.0:
        return _BRIDGE_MIN_CAPACITY
    return max(_BRIDGE_MIN_CAPACITY, math.ceil(_BRIDGE_CAPACITY_FACTOR * total))


# ---------------------------------------------------------------------------
# Resource validation — raising boundary.
# ---------------------------------------------------------------------------


def _validate_resources(materialized: MaterializedHardware) -> None:
    """Run resource-conflict checks; raise on first conflict.

    Boundary wrapper: collects :class:`ConfigProblem`\\ s via
    :func:`capa.devices.materialize.collect_resource_problems`, then
    raises :class:`ResourceConflict` for the first error so existing
    call sites keep their exception contract. Setup-editor Layer-4
    calls :func:`collect_resource_problems` directly.
    """
    problems = collect_resource_problems(materialized)
    errors = [p for p in problems if p.severity == "error"]
    if errors:
        first = errors[0]
        raise _resource_conflict_from_problem(first)


def _resource_conflict_from_problem(problem: ConfigProblem) -> ResourceConflict:
    """Promote a resource :class:`ConfigProblem` back to ``ResourceConflict``.

    Preserves ``conflicting_names`` and ``resource_key`` payload so the
    runtime exception payload is unchanged from the pre-refactor shape.
    Auxiliary fields live under ``ConfigProblem.path`` and the message.
    """
    names: tuple[str, ...] = ()
    resource_key: str | None = None
    if len(problem.path) >= 3 and problem.path[0] == "conflict":
        names = (str(problem.path[1]), str(problem.path[2]))
        if len(problem.path) >= 4:
            resource_key = str(problem.path[3])
    return ResourceConflict(
        problem.message,
        conflicting_names=names,
        resource_key=resource_key,
    )


# ---------------------------------------------------------------------------
# Module re-exports.
# ---------------------------------------------------------------------------

# RuntimeConfig is re-exported so callers that need the schema (notably
# `Conductor.from_runtime`-style wiring) can import via the runtime layer.
__all__ = [
    "RuntimeConfig",
    "build_workers",
]
