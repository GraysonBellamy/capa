"""Tests for :class:`capa.runtime.conductor.ConductorConfig`."""

from __future__ import annotations

from capa.experiment.config import RuntimeConfig
from capa.runtime.conductor import ConductorConfig


def test_from_runtime_copies_operator_tunables() -> None:
    runtime = RuntimeConfig(
        shutdown_grace_s=7.5,
        loop_lag_warn_ms=25.0,
        ui_bridge_capacity=128,
    )

    cfg = ConductorConfig.from_runtime(runtime)

    assert cfg.shutdown_grace_s == 7.5
    assert cfg.loop_lag_warn_ms == 25.0
