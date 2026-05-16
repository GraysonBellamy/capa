"""Tests for the profile preflight runtime — registry categories and
``adapters_started``-aware silent-channel handling.

Static checks read only config; dynamic
checks observe live samples and must run after ``adapter.start()``. The
silent-channel branches in dynamic checks branch on ``adapters_started``
so a quiet stream is a warning before the engine has opened producers but
a blocking error once they should be running.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from capa.experiment.profiles.runtime import (
    ProfilePreflightContext,
    filter_by_category,
    get_category,
    register,
    registered_ids,
)


def _ctx(adapters_started: bool) -> ProfilePreflightContext:
    """Minimal context for direct check invocation. The mocks satisfy the
    type slots; the dynamic checks under test only touch ``profile_metadata``,
    ``config.domain_profile.id``, ``config.hardware.channels``, and
    ``databus``."""
    config = MagicMock()
    config.domain_profile.id = "capa.profiles.capa_pyrolysis"
    config.hardware.channels = ()  # no channels declared

    databus = MagicMock()
    databus.subscribe.return_value = iter(())  # empty async iter stub

    return ProfilePreflightContext(
        config=config,
        instruments=MagicMock(),
        databus=databus,
        profile_metadata={},
        adapters_started=adapters_started,
    )


def test_register_default_category_is_static() -> None:
    @register("test.runtime.default_category")
    async def _check(ctx: ProfilePreflightContext) -> None:
        return None

    try:
        assert get_category("test.runtime.default_category") == "static"
    finally:
        # Clean up to avoid leaking into other tests.
        from capa.experiment.profiles.runtime import _REGISTRY

        _REGISTRY.pop("test.runtime.default_category", None)


def test_register_dynamic_category_is_recorded() -> None:
    @register("test.runtime.dynamic_marker", category="dynamic")
    async def _check(ctx: ProfilePreflightContext) -> None:
        return None

    try:
        assert get_category("test.runtime.dynamic_marker") == "dynamic"
    finally:
        from capa.experiment.profiles.runtime import _REGISTRY

        _REGISTRY.pop("test.runtime.dynamic_marker", None)


def test_filter_by_category_partitions_known_ids() -> None:
    # The CAPA profile registers both static and dynamic checks; the
    # filter should partition them cleanly.
    all_ids = tuple(i for i in registered_ids() if i.startswith("capa."))
    static_ids = filter_by_category(all_ids, "static")
    dynamic_ids = filter_by_category(all_ids, "dynamic")

    # Disjoint and complete (under the assumption that every registered id
    # has exactly one category).
    assert set(static_ids).isdisjoint(set(dynamic_ids))
    assert set(static_ids) | set(dynamic_ids) == set(all_ids)

    # The three checks that read live samples must be classified dynamic.
    assert "capa.heater_pv_in_safe_range" in dynamic_ids
    assert "capa.purge_flow_established" in dynamic_ids
    assert "capa.balance_stability" in dynamic_ids
    # Pure config/filesystem checks must be classified static.
    assert "capa.required_channel_mappings" in static_ids
    assert "capa.atmosphere_consistency" in static_ids
    assert "capa.leak_test_recency" in static_ids
    assert "capa.disk_projection" in static_ids


@pytest.mark.anyio
async def test_purge_silent_warns_before_adapters_started() -> None:
    """Pre-adapter-start, a silent purge-flow channel is a warning, not
    blocking. Mirrors the legacy behavior so plugin-registered checks that
    reuse ``ProfilePreflightContext`` keep working."""
    from capa.experiment.profiles.runtime import _purge_flow_established

    ctx = _ctx(adapters_started=False)
    ctx.profile_metadata = {"atmosphere": {"purge": {"target_flow_sccm": 100.0}}}

    problem = await _purge_flow_established(ctx)
    assert problem is not None
    assert problem.code == "capa.purge_silent"
    assert problem.severity == "warning"
    assert problem.blocking is False


@pytest.mark.anyio
async def test_purge_zero_target_is_explicit_optout() -> None:
    """target_flow_sccm == 0 declares a no-flow run; the check should
    no-op even when adapters are started and no samples arrive."""
    from capa.experiment.profiles.runtime import _purge_flow_established

    ctx = _ctx(adapters_started=True)
    ctx.profile_metadata = {"atmosphere": {"purge": {"target_flow_sccm": 0.0}}}

    problem = await _purge_flow_established(ctx)
    assert problem is None


@pytest.mark.anyio
async def test_purge_silent_blocks_after_adapters_started() -> None:
    """Post-adapter-start, a silent purge-flow channel is a blocking
    error — the device should be streaming and isn't."""
    from capa.experiment.profiles.runtime import _purge_flow_established

    ctx = _ctx(adapters_started=True)
    ctx.profile_metadata = {"atmosphere": {"purge": {"target_flow_sccm": 100.0}}}

    problem = await _purge_flow_established(ctx)
    assert problem is not None
    assert problem.code == "capa.purge_silent"
    assert problem.severity == "error"
    assert problem.blocking is True


@pytest.mark.anyio
async def test_heater_pv_silent_blocks_after_adapters_started() -> None:
    from capa.experiment.profiles.runtime import _heater_pv_safe

    ctx = _ctx(adapters_started=True)

    problem = await _heater_pv_safe(ctx)
    assert problem is not None
    assert problem.code == "capa.heater_pv_silent"
    assert problem.severity == "error"
    assert problem.blocking is True
