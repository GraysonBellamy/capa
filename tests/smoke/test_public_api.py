"""Public-API snapshot tests.

Pin the names exported from ``capa.runtime`` and ``capa.devices`` so
intentional changes (and accidental drift) surface as visible diffs in
PRs. This is a *snapshot* test, not a contract — when an export is
intentionally added or removed, update the literal sets below.

``capa.runtime.__all__`` is the small production facade below. Test
seams, state-machine internals, bridge
plumbing, heartbeat/saturation helpers, and dispatcher impls are no
longer re-exported from the package; consumers and tests import them
from their concrete submodules (``capa.runtime.runner``,
``capa.runtime.bridge``, ``capa.runtime.lifecycle``, etc.).
"""

from __future__ import annotations

import capa.devices
import capa.runtime

# ---------------------------------------------------------------------------
# capa.runtime.__all__ — production facade
# ---------------------------------------------------------------------------

_EXPECTED_RUNTIME_EXPORTS: frozenset[str] = frozenset(
    {
        "RUNTIME_VERSION",
        "Conductor",
        "ConductorConfig",
        "ConductorStateError",
        "HeadlessResult",
        "ManualClient",
        "PoolStateError",
        "RealRunSession",
        "ResourceConflict",
        "RunOutcome",
        "RunResult",
        "RunSession",
        "RunnerStateError",
        "UnknownDeviceError",
        "WorkerPool",
        "WorkerStateError",
        "install_sigint_handler",
        "run_headless",
    }
)


def test_capa_runtime_public_exports_match_snapshot() -> None:
    """Pin the ``capa.runtime.__all__`` set.

    Any addition or removal here should be a deliberate API decision;
    the diff in this test is the audit trail for which symbols were
    promoted or demoted.
    """
    actual = frozenset(capa.runtime.__all__)
    missing = _EXPECTED_RUNTIME_EXPORTS - actual
    extra = actual - _EXPECTED_RUNTIME_EXPORTS
    assert not missing and not extra, (
        f"capa.runtime.__all__ drift detected.\n"
        f"  Removed (was exported, now missing): {sorted(missing)}\n"
        f"  Added   (newly exported, not in snapshot): {sorted(extra)}\n"
        f"If this change is intentional, update _EXPECTED_RUNTIME_EXPORTS."
    )


def test_capa_devices_public_exports_match_snapshot() -> None:
    """Pin the current ``capa.devices.__all__`` set (empty today).

    ``AdapterStartContext`` lives in ``capa.devices.adapter`` but is not
    re-exported from ``capa.devices`` itself, so this snapshot stays
    empty until a deliberate device-layer facade is added.
    """
    expected: frozenset[str] = frozenset()
    actual = frozenset(capa.devices.__all__)
    assert actual == expected, (
        f"capa.devices.__all__ drift detected: {sorted(actual)}. "
        f"Update the expected snapshot if intentional."
    )
