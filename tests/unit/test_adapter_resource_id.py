"""Adapter ``resource_id`` conformance tests.

Phase 0 of the per-resource worker migration adds a ``resource_id``
property to every adapter (``docs/per-resource-worker-migration.md``
§4.10). The string identifies the underlying hardware contention domain;
``build_workers`` (Phase 1) will group adapters with the same
``resource_id`` into a single worker.

These tests enforce the contract documented on
:class:`capa.devices.adapter.DeviceAdapter` ``.resource_id``:

* non-empty string;
* stable across calls (no I/O, no time-dependence);
* follows the ``<scheme>:<body>`` convention;
* two adapters that share a physical resource produce the same string;
* two adapters that do not share a physical resource produce different
  strings.

The factories below are intentionally inline and minimal — Phase 1's
``build_workers`` will need richer shared fixtures, but Phase 0 only
needs to read a property off a constructed adapter.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from capa.core.clock import RunClock
from capa.devices.adapter import DeviceAdapter
from capa.devices.alicat import AlicatAdapter
from capa.devices.camera.base import CameraSpec
from capa.devices.camera.webcam import WebcamAdapter
from capa.devices.nidaq import NIDAQAdapter
from capa.devices.sartorius import SartoriusAdapter
from capa.devices.sim.alicat_sim import AlicatSim
from capa.devices.sim.flir_ir_sim import FlirIrSim
from capa.devices.sim.nidaq_block_sim import NIDAQBlockSim
from capa.devices.sim.nidaq_polled_sim import NIDAQPolledSim
from capa.devices.sim.sartorius_sim import SartoriusSim
from capa.devices.sim.watlow_sim import WatlowSim
from capa.devices.watlow import WatlowAdapter

# ---------------------------------------------------------------------------
# Adapter factories — minimal constructions, no open() called.
# ---------------------------------------------------------------------------


def _make_watlow(port: str = "COM6") -> WatlowAdapter:
    return WatlowAdapter(name="heater", port=port)


def _make_alicat(port: str = "COM7") -> AlicatAdapter:
    return AlicatAdapter(name="purge_mfc", port=port)


def _make_sartorius(port: str = "COM4") -> SartoriusAdapter:
    return SartoriusAdapter(name="balance", port=port)


def _nidaq_channel(physical_channel: str) -> dict[str, Any]:
    return {
        "kind": "ai_voltage",
        "physical_channel": physical_channel,
        "name": physical_channel.replace("/", "_"),
        "unit": "V",
        "min_val": -10.0,
        "max_val": 10.0,
    }


def _make_nidaq(channels: list[str] | None = None) -> NIDAQAdapter:
    if channels is None:
        channels = ["cDAQ1Mod1/ai0", "cDAQ1Mod1/ai1"]
    return NIDAQAdapter(
        name="daq1",
        task_name="t1",
        channels=tuple(_nidaq_channel(c) for c in channels),
    )


def _make_webcam(name: str = "visible_cam0", serial: str | None = None) -> WebcamAdapter:
    spec_kwargs: dict[str, Any] = {
        "name": name,
        "adapter": "capa.devices.camera.webcam",
        "kind": "visible",
    }
    if serial is not None:
        spec_kwargs["serial"] = serial
    spec = CameraSpec.model_validate(spec_kwargs)
    return WebcamAdapter(
        spec=spec,
        clock=RunClock.now(),
        fps=30.0,
        width=64,
        height=48,
        codec="mpeg4",
        pix_fmt="yuv420p",
    )


def _make_watlow_sim(name: str = "heater") -> WatlowSim:
    return WatlowSim(name=name)


def _make_alicat_sim(name: str = "purge_mfc") -> AlicatSim:
    return AlicatSim(name=name)


def _make_sartorius_sim(name: str = "balance") -> SartoriusSim:
    return SartoriusSim(name=name)


def _make_nidaq_polled_sim(name: str = "cdaq1") -> NIDAQPolledSim:
    return NIDAQPolledSim(name=name, task="tc_task")


def _make_nidaq_block_sim(name: str = "cdaq1_block") -> NIDAQBlockSim:
    return NIDAQBlockSim(name=name, task="block_task")


def _make_flir_ir_sim(name: str = "ir_cam0") -> FlirIrSim:
    spec = CameraSpec.model_validate(
        {
            "name": name,
            "adapter": "capa.devices.sim.flir_ir_sim",
            "kind": "ir",
        }
    )
    return FlirIrSim(spec=spec, clock=RunClock.now())


# ---------------------------------------------------------------------------
# All-adapter conformance
# ---------------------------------------------------------------------------

ALL_FACTORIES: list[tuple[str, Callable[[], object]]] = [
    ("watlow", _make_watlow),
    ("alicat", _make_alicat),
    ("sartorius", _make_sartorius),
    ("nidaq", _make_nidaq),
    ("webcam", _make_webcam),
    ("watlow_sim", _make_watlow_sim),
    ("alicat_sim", _make_alicat_sim),
    ("sartorius_sim", _make_sartorius_sim),
    ("nidaq_polled_sim", _make_nidaq_polled_sim),
    ("nidaq_block_sim", _make_nidaq_block_sim),
    ("flir_ir_sim", _make_flir_ir_sim),
]


@pytest.mark.parametrize("factory", [pytest.param(f, id=label) for label, f in ALL_FACTORIES])
def test_adapter_exposes_nonempty_stable_resource_id(factory: Callable[[], object]) -> None:
    adapter = factory()
    rid1 = adapter.resource_id  # type: ignore[attr-defined]
    rid2 = adapter.resource_id  # type: ignore[attr-defined]
    assert isinstance(rid1, str)
    assert rid1, "resource_id must be a non-empty string"
    assert rid1 == rid2, "resource_id must be stable across calls"
    assert ":" in rid1, f"resource_id {rid1!r} must follow '<scheme>:<body>' convention"
    scheme = rid1.split(":", 1)[0]
    assert scheme in {"serial", "daqmx", "webcam", "sim"}, (
        f"unexpected resource_id scheme {scheme!r} in {rid1!r}"
    )


@pytest.mark.parametrize("factory", [pytest.param(f, id=label) for label, f in ALL_FACTORIES])
def test_adapter_satisfies_device_adapter_protocol_resource_id_attr(
    factory: Callable[[], object],
) -> None:
    """Camera adapters (Webcam, FlirIrSim) implement the Camera Protocol,
    not the DeviceAdapter Protocol, but every adapter must expose the
    ``resource_id`` attribute either way. Phase 3 unifies cameras into
    the device adapter shape; for Phase 0 we just assert presence.
    """
    adapter = factory()
    assert hasattr(adapter, "resource_id")


# ---------------------------------------------------------------------------
# Serial-port sharing (the load-bearing bus-grouping case for Phase 1)
# ---------------------------------------------------------------------------


def test_two_watlow_adapters_on_same_port_share_resource_id() -> None:
    """Two Watlow controllers on the same RS-485 bus must collapse onto
    one worker. The mechanism is identical ``resource_id``; see migration
    doc §7.1 example."""
    primary = _make_watlow(port="COM6")
    secondary = _make_watlow(port="COM6")
    assert primary.resource_id == secondary.resource_id == "serial:COM6"


def test_watlow_and_alicat_on_same_port_share_resource_id() -> None:
    """Two different adapter classes sharing a serial port (unusual but
    permitted; e.g. a multi-protocol RS-485 bus) must still collapse onto
    one worker. The scheme is keyed on the resource, not on the adapter
    type."""
    watlow = _make_watlow(port="COM6")
    alicat = _make_alicat(port="COM6")
    assert watlow.resource_id == alicat.resource_id


def test_two_watlow_adapters_on_different_ports_have_distinct_resource_ids() -> None:
    a = _make_watlow(port="COM6")
    b = _make_watlow(port="COM7")
    assert a.resource_id != b.resource_id


def test_watlow_resource_id_normalizes_serial_port_case() -> None:
    """Windows COM names are case-insensitive at the OS level; mixed-case
    configs should not silently split workers."""
    upper = _make_watlow(port="COM6")
    lower = _make_watlow(port="com6")
    assert upper.resource_id == lower.resource_id == "serial:COM6"


def test_watlow_resource_id_strips_whitespace() -> None:
    a = _make_watlow(port="COM6")
    b = _make_watlow(port=" COM6 ")
    assert a.resource_id == b.resource_id


# ---------------------------------------------------------------------------
# NI-DAQ chassis derivation
# ---------------------------------------------------------------------------


def test_nidaq_resource_id_strips_module_suffix_for_cdaq() -> None:
    """Two cDAQ tasks bound to different modules in the same chassis must
    share a worker. Migration doc §4.10 + §7.4 rule 2."""
    a = _make_nidaq(channels=["cDAQ1Mod1/ai0", "cDAQ1Mod1/ai1"])
    b = _make_nidaq(channels=["cDAQ1Mod3/ai0"])
    assert a.resource_id == b.resource_id == "daqmx:cDAQ1"


def test_nidaq_resource_id_keeps_single_board_device_name() -> None:
    """Single-board PCI cards (e.g. ``Dev1/ai0``) have no chassis. The
    device name itself is the contention domain."""
    a = _make_nidaq(channels=["Dev1/ai0"])
    assert a.resource_id == "daqmx:Dev1"


def test_nidaq_resource_id_differs_across_chassis() -> None:
    a = _make_nidaq(channels=["cDAQ1Mod1/ai0"])
    b = _make_nidaq(channels=["cDAQ2Mod1/ai0"])
    assert a.resource_id != b.resource_id


# ---------------------------------------------------------------------------
# Webcam
# ---------------------------------------------------------------------------


def test_webcam_resource_id_uses_serial_when_set() -> None:
    cam = _make_webcam(name="any_label", serial="GVFI3-12345")
    assert cam.resource_id == "webcam:GVFI3-12345"


def test_webcam_resource_id_falls_back_to_name_when_no_serial() -> None:
    cam = _make_webcam(name="visible_cam0", serial=None)
    assert cam.resource_id == "webcam:visible_cam0"


def test_webcam_resource_id_is_pure_property_no_open_required() -> None:
    """The migration doc requires ``resource_id`` to be safe to read before
    ``open()``. Webcam construction does not call ``av.open``; verify the
    property is readable on the bare-constructed adapter."""
    cam = _make_webcam()
    _ = cam.resource_id  # must not raise


# ---------------------------------------------------------------------------
# Sim adapters
# ---------------------------------------------------------------------------


def test_distinct_sim_adapters_have_distinct_resource_ids() -> None:
    """Sim adapters do not share physical resources; each gets its own
    worker. Verify uniqueness across the canonical sim configuration."""
    sims = [
        _make_watlow_sim(name="heater"),
        _make_alicat_sim(name="purge_mfc"),
        _make_sartorius_sim(name="balance"),
        _make_nidaq_polled_sim(name="cdaq1"),
        _make_nidaq_block_sim(name="cdaq1_block"),
        _make_flir_ir_sim(name="ir_cam0"),
    ]
    rids = [s.resource_id for s in sims]
    assert len(set(rids)) == len(rids), f"sim adapter resource_ids collided: {rids}"


def test_two_sim_adapters_with_same_name_share_resource_id() -> None:
    """The sim scheme keys off ``name``; two sims with the same name
    collapse to one worker. This is mostly a degenerate case (configs
    forbid duplicate device names) but documents the rule."""
    a = _make_watlow_sim(name="dup")
    b = _make_watlow_sim(name="dup")
    assert a.resource_id == b.resource_id


# ---------------------------------------------------------------------------
# Protocol surface
# ---------------------------------------------------------------------------


def test_device_adapter_protocol_declares_resource_id() -> None:
    """Phase 0 adds ``resource_id`` to the Protocol so ``build_workers``
    can ``getattr`` it without per-adapter dispatch."""
    assert "resource_id" in DeviceAdapter.__annotations__
