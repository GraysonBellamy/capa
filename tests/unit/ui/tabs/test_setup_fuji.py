"""The Setup tab's handling of a Fuji analyzer: discovery rows, the device
payload built from one, and the ``fuji_channel`` binding form.
"""

from __future__ import annotations

from typing import Any

from capa.devices.registry import _import_builtins, get_descriptor
from capa.ui.forms.from_model import build_form
from capa.ui.tabs.setup_discovery import (
    _SERIAL_PORT_FAMILIES,
    _summarise_row,
    build_device_payload_from_row,
)
from capa.ui.tabs.setup_sections.channels import (
    _VARIANT_FIELDS,
    _VARIANT_LABELS,
    _compose_reads_from,
)

_import_builtins()

ROW = {
    "adapter": "fuji",
    "port": "COM8",
    "address": 1,
    "baudrate": 38400,
    "model": "ZPA",
    "serial": "N8A0259T",
    "type_code": "ZPACBJY1MPFYYYYYY2DEYAYAY0",
    "channels": "CH1=co2, CH2=co",
    "channel_map": {"CH1": "co2", "CH2": "co"},
}


def test_the_analyzer_is_scanned_with_the_other_serial_families() -> None:
    # Serial scans run one family at a time so they do not race for a port.
    assert "fuji" in _SERIAL_PORT_FAMILIES


def test_build_payload_for_fuji() -> None:
    desc = get_descriptor("capa.devices.fuji")
    assert desc is not None
    payload = build_device_payload_from_row(desc, ROW, existing_names=set())
    assert payload["adapter"] == "capa.devices.fuji"
    assert payload["name"] == "fuji1"
    params = payload["params"]
    assert (params["port"], params["address"], params["rate_hz"]) == ("COM8", 1, 1.0)
    # The type code's suggestion pre-fills the map; the operator confirms it.
    assert params["channel_map"] == {"CH1": "co2", "CH2": "co"}
    assert params["channel_map"] is not ROW["channel_map"]
    # The payload is a valid device as it stands.
    assert desc.params_model is not None
    _ = desc.params_model.model_validate(params)


def test_build_payload_without_a_suggested_map_leaves_it_to_the_operator() -> None:
    desc = get_descriptor("capa.devices.fuji")
    assert desc is not None
    payload = build_device_payload_from_row(
        desc, {"port": "COM8", "address": 2}, existing_names={"fuji1"}
    )
    assert payload["name"] == "fuji2"
    assert payload["params"]["address"] == 2
    assert "channel_map" not in payload["params"]


def test_the_device_form_takes_and_gives_back_the_parameters(qtbot: Any) -> None:
    desc = get_descriptor("capa.devices.fuji")
    assert desc is not None
    assert desc.params_model is not None
    form = build_form(desc.params_model)
    qtbot.addWidget(form)
    params = {
        "port": "COM8",
        "address": 1,
        "channel_map": {"CH1": "co2", "CH2": "co", "CH3": "o2"},
        "options": ["auto_zero"],
    }
    form.set_values(params)
    values = form.values()
    # The channel map, a table in the TOML, and the options, a list, both
    # survive the form.
    assert values["channel_map"] == params["channel_map"]
    assert tuple(values["options"]) == ("auto_zero",)
    assert form.validate() == []
    parsed = desc.params_model.model_validate(values)
    assert parsed.model_dump()["channel_map"] == params["channel_map"]
    # A form left empty says which parameter is missing.
    empty = build_form(desc.params_model)
    qtbot.addWidget(empty)
    assert [error["loc"] for error in empty.validate()] == [("channel_map",)]


def test_summary_line() -> None:
    assert _summarise_row("fuji", ROW) == "COM8  station=1  ZPA  sn=N8A0259T"
    assert _summarise_row("fuji", {}) == "(no identity)"


def test_the_binding_form_offers_channels_and_fields() -> None:
    assert _VARIANT_LABELS["fuji_channel"] == "Fuji analyzer channel"
    fields = {name: choices for name, _label, _dtype, choices in _VARIANT_FIELDS["fuji_channel"]}
    assert fields == {"channel": "fuji_channels", "field": "fuji_fields"}


def test_the_binding_reads_from_text() -> None:
    value = {"source": "fuji_channel", "device": "analyzer", "channel": "CH3", "field": "value"}
    assert _compose_reads_from(value) == "analyzer.CH3"
    assert _compose_reads_from({**value, "field": "valid"}) == "analyzer.CH3 (valid)"
    assert _compose_reads_from({"source": "fuji_channel"}) == "(fuji_channel)"
