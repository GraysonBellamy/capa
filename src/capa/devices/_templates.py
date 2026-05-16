"""Built-in :class:`ChannelTemplate` instances ().

Templates are shared between real and sim adapters when their
:class:`SourceBinding` shape is identical — a Watlow heater PV is the
same source row regardless of whether the underlying adapter is the
real Watlow class or its simulator. Adapter modules import the relevant
templates and attach them to their :class:`AdapterDescriptor`.
"""

from __future__ import annotations

from collections.abc import Callable

from capa.devices.registry import ChannelTemplate


def _watlow_param_binding(parameter: str, instance: int = 1) -> Callable[[str], dict[str, object]]:
    def _factory(device_name: str) -> dict[str, object]:
        return {
            "source": "watlow_parameter",
            "device": device_name,
            "parameter": parameter,
            "instance": instance,
        }

    return _factory


def _alicat_frame_binding(frame_field: str) -> Callable[[str], dict[str, object]]:
    def _factory(device_name: str) -> dict[str, object]:
        return {
            "source": "alicat_frame_field",
            "device": device_name,
            "field": frame_field,
        }

    return _factory


def _sartorius_reading_binding(field: str = "value") -> Callable[[str], dict[str, object]]:
    def _factory(device_name: str) -> dict[str, object]:
        return {
            "source": "sartorius_reading",
            "device": device_name,
            "field": field,
        }

    return _factory


def _nidaq_reading_binding(task: str, field: str) -> Callable[[str], dict[str, object]]:
    def _factory(device_name: str) -> dict[str, object]:
        return {
            "source": "nidaq_reading_field",
            "device": device_name,
            "task": task,
            "field": field,
        }

    return _factory


# ---------------------------------------------------------------------------
# — canonical templates.
# ---------------------------------------------------------------------------


WATLOW_HEATER_PV = ChannelTemplate(
    id="watlow.heater_pv",
    label="Heater PV from Watlow",
    kind="process_var",
    source_factory=_watlow_param_binding("process_value", instance=1),
    default_unit="degC",
    default_derived_unit="degC",
    default_calibration={"kind": "identity", "input_unit": "degC", "output_unit": "degC"},
    capa_group="heater_pv",
    plot_group="temperatures",
)

WATLOW_HEATER_SETPOINT = ChannelTemplate(
    id="watlow.heater_setpoint",
    label="Heater setpoint from Watlow",
    kind="setpoint",
    source_factory=_watlow_param_binding("setpoint", instance=1),
    default_unit="degC",
    default_derived_unit="degC",
    default_calibration={"kind": "identity", "input_unit": "degC", "output_unit": "degC"},
    capa_group="heater_setpoint",
    plot_group="temperatures",
)

ALICAT_PURGE_FLOW = ChannelTemplate(
    id="alicat.purge_flow",
    label="Purge flow from Alicat MFC",
    kind="mfc_flow",
    source_factory=_alicat_frame_binding("Mass_Flow"),
    default_unit="slpm",
    default_derived_unit="slpm",
    default_calibration={"kind": "identity", "input_unit": "slpm", "output_unit": "slpm"},
    capa_group="purge_gas_flow",
    plot_group="flows",
)

SARTORIUS_MASS = ChannelTemplate(
    id="sartorius.mass",
    label="Mass from Sartorius balance",
    kind="mass",
    source_factory=_sartorius_reading_binding("value"),
    default_unit="g",
    default_derived_unit="g",
    default_calibration={"kind": "identity", "input_unit": "g", "output_unit": "g"},
    capa_group="mass",
    plot_group="mass",
)

NIDAQ_THERMOCOUPLE = ChannelTemplate(
    id="nidaq.thermocouple",
    label="Thermocouple from NI-DAQ task",
    kind="tc",
    source_factory=_nidaq_reading_binding("default_task", "TC_1"),
    default_unit="K",
    default_derived_unit="K",
    default_calibration={"kind": "identity", "input_unit": "K", "output_unit": "K"},
    capa_group=None,
    plot_group="temperatures",
)


__all__ = [
    "ALICAT_PURGE_FLOW",
    "NIDAQ_THERMOCOUPLE",
    "SARTORIUS_MASS",
    "WATLOW_HEATER_PV",
    "WATLOW_HEATER_SETPOINT",
]
