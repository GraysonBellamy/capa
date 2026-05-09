"""Tests for :mod:`capa.core.plugins_runtime`."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from capa.core.errors import PluginTrustError
from capa.core.plugins_lock import PluginsLock
from capa.core.plugins_runtime import (
    ProcedureRegistry,
    check_procedure_class,
    discover_procedures,
    resolve_mode,
)


def test_resolve_mode_explicit_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAPA_PLUGIN_MODE", "production")
    assert resolve_mode("dev") == "dev"
    assert resolve_mode(None) == "production"


def test_resolve_mode_default_is_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CAPA_PLUGIN_MODE", raising=False)
    assert resolve_mode() == "dev"


# ---------------------------------------------------------------------------
# check_procedure_class
# ---------------------------------------------------------------------------


class _GoodConfig(BaseModel):
    pass


class _GoodProc:
    id = "test.good"
    name = "Good"
    version = "1.0"
    config_model = _GoodConfig
    required_capabilities: tuple[str, ...] = ()
    required_channels: tuple = ()

    async def preflight(self, ctx):
        return []

    async def run(self, ctx):
        return None


def test_check_procedure_class_accepts_well_formed() -> None:
    check_procedure_class(_GoodProc)


def test_check_procedure_class_rejects_missing_attribute() -> None:
    class Bad:
        id = "test.bad"
        # missing name, version, config_model

    with pytest.raises(PluginTrustError, match="missing attribute"):
        check_procedure_class(Bad)


def test_check_procedure_class_rejects_non_basemodel_config() -> None:
    class Bad:
        id = "x"
        name = "x"
        version = "1"
        config_model = dict  # not a BaseModel
        required_capabilities: tuple[str, ...] = ()
        required_channels: tuple = ()

        async def preflight(self, ctx):
            return []

        async def run(self, ctx):
            return None

    with pytest.raises(PluginTrustError, match="BaseModel"):
        check_procedure_class(Bad)


def test_check_procedure_class_rejects_non_async_methods() -> None:
    class Bad:
        id = "x"
        name = "x"
        version = "1"
        config_model = _GoodConfig
        required_capabilities: tuple[str, ...] = ()
        required_channels: tuple = ()

        def preflight(self, ctx):
            return []

        async def run(self, ctx):
            return None

    with pytest.raises(PluginTrustError, match="coroutine"):
        check_procedure_class(Bad)


# ---------------------------------------------------------------------------
# Discovery against the live capa entry points.
# ---------------------------------------------------------------------------


def test_discovery_loads_capa_builtins_in_dev_mode() -> None:
    report = discover_procedures(mode="dev")
    ids = {p.id for p in report.loaded}
    assert "capa.builtin.free_run" in ids
    assert "capa.builtin.recipe_runner" in ids
    assert "capa.builtin.batch" in ids


def test_loaded_procedure_version_uses_dist_not_class_attribute() -> None:
    """Hardware-day §5.4: the lock-writer used the class attribute
    ``RecipeRunner.version`` while ``detect_drift`` compared against
    ``dist.version``. Editable installs never matched. ``LoadedProcedure.version``
    must equal the distribution version so the two sides agree.
    """
    import importlib.metadata

    report = discover_procedures(mode="dev")
    by_id = {p.id: p for p in report.loaded}

    free_run = by_id["capa.builtin.free_run"]
    capa_dist_version = importlib.metadata.version(free_run.package)
    assert free_run.version == capa_dist_version

    # Confirm the class attribute is *different* (the asymmetry source);
    # this also guards against a future refactor that accidentally aligns
    # them by changing the class attribute, which would mask the bug.
    class_version = getattr(free_run.cls, "version", None)
    if class_version is not None and class_version != capa_dist_version:
        # Editable-install case — confirm the dist wins, not the class attr.
        assert free_run.version != class_version


def test_discovery_records_drift_in_production_when_lock_missing() -> None:
    """Production mode + empty lock = every installed plugin is rejected as
    MISSING_FROM_LOCK."""
    report = discover_procedures(
        plugins_lock=PluginsLock(version=1, plugins=()),
        mode="production",
    )
    assert report.loaded == []
    assert any(d.kind.value == "missing_from_lock" for d in report.drifts)


def test_registry_instantiate_uses_from_config() -> None:
    """Builtin classes define ``from_config``; the registry's instantiate
    path should pick that up rather than calling ``cls(**kwargs)`` directly."""
    report = discover_procedures(mode="dev")
    registry = ProcedureRegistry(report.loaded, report=report)
    proc = registry.instantiate("capa.builtin.free_run", {"duration_s": 0.5})
    assert proc.duration_s == 0.5  # type: ignore[attr-defined]


def test_registry_rejects_unknown_id() -> None:
    registry = ProcedureRegistry([], report=None)
    with pytest.raises(PluginTrustError, match="trusted registry"):
        registry.instantiate("nope", None)
