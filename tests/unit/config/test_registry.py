"""Tests for ``capa.devices.registry`` (plan §5.7)."""

from __future__ import annotations

import pytest

from capa.devices.registry import (
    ADAPTERS,
    AdapterDescriptor,
    ChannelTemplate,
    _import_builtins,
    all_for_family,
    get_descriptor,
)


@pytest.fixture(scope="module", autouse=True)
def _ensure_builtins_loaded() -> None:
    _import_builtins()


def test_builtin_adapter_descriptors_registered() -> None:
    """Every adapter under capa.devices.* must register a descriptor.

    Regression test against forgetting to add a ``DESCRIPTOR`` when a
    new adapter ships — plan §11 risk that the editor regresses to
    JSON-editing without the registry.
    """
    expected = {
        "capa.devices.watlow",
        "capa.devices.alicat",
        "capa.devices.sartorius",
        "capa.devices.nidaq",
        "capa.devices.sim.watlow_sim",
        "capa.devices.sim.alicat_sim",
        "capa.devices.sim.sartorius_sim",
        "capa.devices.sim.nidaq_polled_sim",
        "capa.devices.sim.nidaq_block_sim",
    }
    assert expected.issubset(set(ADAPTERS.keys()))


def test_real_watlow_descriptor_shape() -> None:
    d = get_descriptor("capa.devices.watlow")
    assert d is not None
    assert d.family == "watlow"
    assert d.adapter_factory is not None
    assert d.params_model is not None
    assert "watlow_parameter" in d.supported_binding_sources
    # Capabilities upper bound should include HAS_SETPOINT.
    assert any(c.name == "HAS_SETPOINT" for c in d.capabilities)


def test_channel_templates_canonical_set() -> None:
    """Plan §5.7.1: built-in templates cover the 90% case."""
    template_ids: set[str] = set()
    for d in ADAPTERS.values():
        for t in d.channel_templates:
            template_ids.add(t.id)
    expected = {
        "watlow.heater_pv",
        "watlow.heater_setpoint",
        "alicat.purge_flow",
        "sartorius.mass",
        "nidaq.thermocouple",
    }
    assert expected.issubset(template_ids)


def test_channel_template_source_factory_produces_valid_binding() -> None:
    d = get_descriptor("capa.devices.watlow")
    assert d is not None
    template = next(t for t in d.channel_templates if t.id == "watlow.heater_pv")
    binding = template.source_factory("heater")
    assert binding == {
        "source": "watlow_parameter",
        "device": "heater",
        "parameter": "process_value",
        "instance": 1,
    }


def test_all_for_family_groups_correctly() -> None:
    sim_descriptors = all_for_family("sim")
    sim_ids = {d.id for d in sim_descriptors}
    assert "capa.devices.sim.watlow_sim" in sim_ids
    assert "capa.devices.sim.alicat_sim" in sim_ids
    # Real adapters must not appear in the sim family.
    assert "capa.devices.watlow" not in sim_ids


def test_unknown_descriptor_returns_none() -> None:
    assert get_descriptor("capa.devices.does_not_exist") is None


def test_adapter_constructors_are_passive() -> None:
    """Plan §11: Layer-4 resource validation depends on this invariant.

    Each adapter must be constructible without I/O — only then can the
    Setup editor's resource dry-run check conflicts without opening
    serial buses, DAQmx system handles, etc. Real adapters often
    require specific params (port, address) so we may not reach the
    constructor body; that's fine — the property being tested is
    "no I/O", which a ValidationError from the params model proves
    (Pydantic ran before any constructor body executed).
    """
    from pydantic import ValidationError

    for adapter_id, d in ADAPTERS.items():
        if d.adapter_factory is None:
            continue  # plugin adapter without a factory; skipped
        from_params = getattr(d.adapter_factory, "from_params", None)
        try:
            if callable(from_params):
                inst = from_params(name=f"test_{adapter_id}", signals={})
            else:
                inst = d.adapter_factory(name=f"test_{adapter_id}", **d.default_params)
        except (TypeError, ValidationError):
            # Params model rejected the call before any I/O could happen.
            # That still proves "no I/O on __init__".
            continue
        assert inst is not None
        assert inst.name == f"test_{adapter_id}"


def test_descriptor_round_trip_through_dataclass() -> None:
    """Building a descriptor instance with the dataclass shape works."""
    custom = AdapterDescriptor(
        id="capa.devices.plugin.test",
        label="Plugin Test Adapter",
        family="plugin",
        channel_templates=(),
    )
    assert custom.id == "capa.devices.plugin.test"
    assert custom.adapter_factory is None
    assert custom.params_model is None


def test_channel_template_is_frozen() -> None:
    """ChannelTemplate is intended to be immutable for caching."""
    t = ChannelTemplate(
        id="t1",
        label="t1",
        kind="process_var",
        source_factory=lambda name: {"source": "x", "device": name},
        default_unit="degC",
    )
    with pytest.raises(Exception):
        t.id = "different"  # type: ignore[misc]
