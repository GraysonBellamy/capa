"""Runtime-layer exception hierarchy.

Every runtime exception inherits from :class:`~capa.core.errors.CapaError`
so the existing UI / events.sqlite plumbing can render them with no special
case. The runtime types here cover only the new per-resource-worker machinery
(``docs/per-resource-worker-migration.md``); adapter-layer errors keep using
:class:`~capa.core.errors.AdapterError` unchanged.

Hierarchy:

.. code::

    CapaError
    └── RuntimeError (in this module — note: shadows builtins.RuntimeError
        intentionally NOT used as the base; we inherit from CapaError instead)
        ├── WorkerStateError       — illegal worker transition or dispatch in wrong state
        ├── PoolStateError         — illegal pool operation (close while armed, etc.)
        ├── ResourceConflict       — two adapters claim the same hardware contention domain
        ├── RunnerStateError       — WorkerRunner used in a state it doesn't permit
        └── UnknownDeviceError     — dispatch to a device not in the pool

The migration doc references each of these by name:

* ``WorkerStateError`` — §3.3 line 263, §4.1 line 591.
* ``PoolStateError`` — §4.3 lines 737-767.
* ``ResourceConflict`` — §4.12 lines 1311-1336, §7.4 line 1622.
* ``UnknownDeviceError`` — §4.3 line 781.

``RunnerStateError`` is new in this implementation; the
:class:`~capa.runtime.runner.WorkerRunner` abstraction lifted out for testability
(plan §3.1) needs its own state-misuse error so a confused test fixture doesn't
masquerade as a worker bug.
"""

from __future__ import annotations

from capa.core.errors import CapaError


class WorkerStateError(CapaError):
    """Worker is in a state that does not permit the attempted operation.

    Raised at two kinds of site:

    * **Illegal transition.** A caller asked the worker to move
      ``from_state → to_state`` where that edge is not in
      :data:`~capa.runtime.lifecycle.LEGAL_WORKER_EDGES`. The
      ``from_state``/``to_state`` attributes are populated.
    * **Operation refused in current state.** ``dispatch()`` called while the
      worker is DRAINING/CLOSED, ``arm()`` while not IDLE, etc. The
      ``from_state`` is populated; ``to_state`` is ``None``.

    The exception is the canonical thing the worker-loop coroutine raises;
    the sync facade (``Worker.dispatch`` → ``concurrent.futures.Future``)
    re-raises it across the thread seam via ``asyncio.wrap_future`` so the
    caller observes the original.
    """

    def __init__(
        self,
        message: str,
        *,
        from_state: object | None = None,
        to_state: object | None = None,
        resource_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.from_state = from_state
        self.to_state = to_state
        self.resource_id = resource_id


class PoolStateError(CapaError):
    """Pool is in a state that does not permit the attempted operation.

    Migration doc §4.3 lines 737-767: ``open()`` may not be called twice;
    ``close()`` may not be called while any worker is non-IDLE; ``arm_all()``
    requires :attr:`~capa.runtime.lifecycle.PoolState.OPEN`.
    """

    def __init__(
        self,
        message: str,
        *,
        from_state: object | None = None,
        to_state: object | None = None,
    ) -> None:
        super().__init__(message)
        self.from_state = from_state
        self.to_state = to_state


class ResourceConflict(CapaError):  # noqa: N818 - documented public API name
    """Two adapters claim the same hardware contention domain.

    Migration doc §4.12 / §7.4. Raised synchronously from
    :func:`~capa.runtime.build.build_workers` *before* any worker thread is
    spawned, so a misconfigured config fails fast with no hardware side
    effects.

    The ``conflicting_names`` attribute is the pair (or set) of adapter names
    that triggered the conflict, surfaced into the operator-facing error
    toast so the fix is obvious.
    """

    def __init__(
        self,
        message: str,
        *,
        conflicting_names: tuple[str, ...] = (),
        resource_key: str | None = None,
    ) -> None:
        super().__init__(message)
        self.conflicting_names = conflicting_names
        self.resource_key = resource_key


class UnknownDeviceError(CapaError):
    """Caller asked the pool to dispatch to a device name not in this config.

    Distinct from :class:`ResourceConflict` (which fires at build time) —
    this fires at dispatch time when a UI card or procedure step references
    a device whose configuration was removed or renamed. The
    ``configured_names`` attribute lists what is available, so the operator
    sees the typo immediately.
    """

    def __init__(
        self,
        name: str,
        *,
        configured_names: tuple[str, ...] = (),
    ) -> None:
        super().__init__(f"unknown device {name!r}; configured: {sorted(configured_names)}")
        self.name = name
        self.configured_names = configured_names


class RunnerStateError(CapaError):
    """:class:`~capa.runtime.runner.WorkerRunner` used in a state it doesn't permit.

    The runner abstraction (plan §3.1) supports both real-thread and inline
    test backends; both have lifecycles (``start`` → ``submit`` ... → ``stop``)
    and both raise this when called out of order. Kept separate from
    :class:`WorkerStateError` so test failures point at the harness rather
    than the production state machine.
    """


__all__ = [
    "PoolStateError",
    "ResourceConflict",
    "RunnerStateError",
    "UnknownDeviceError",
    "WorkerStateError",
]
