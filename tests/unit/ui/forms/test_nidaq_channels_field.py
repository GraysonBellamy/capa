"""Tests for ``NIDAQChannelsField`` — the Devices-pane NI editor.

Replaces the generic ``_JsonFallbackField`` that the auto-form factory
used to drop on ``NIDAQAdapterParams.channels`` when it couldn't build a
typed sub-form for the discriminated-union tuple. These tests cover:

* Factory dispatch on the ``capa_widget`` json_schema_extra hint.
* Round-trip ``set_value`` / ``value`` through the dict-shaped row list.
* Kind-switching detail pane.
* "Add from inventory" populated through the provider hook + greying of
  already-used physical channels.
* Inline collision validation surfacing duplicate names / physical channels.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import TypeAdapter

from capa.devices.nidaq import NIDAQAdapterParams
from capa.devices.nidaq_channels import (
    NIDAQChannelConfig,
    NIDAQThermocoupleConfig,
)
from capa.ui.forms.widgets._factory import build_field_widget
from capa.ui.forms.widgets._nidaq_channels import (
    NIDAQChannelsField,
    set_nidaq_bound_names_provider,
    set_nidaq_bound_provider,
    set_nidaq_cross_section_handlers,
    set_nidaq_inventory_provider,
    set_nidaq_rescan_handler,
)


@pytest.fixture(autouse=True)
def _reset_inventory_provider() -> Any:
    """Ensure each test starts with no inventory provider installed.

    SetupTab installs a provider in production; tests assert specific
    inventory shapes by setting their own and yielding so the teardown
    clears it.
    """
    set_nidaq_inventory_provider(None)
    set_nidaq_bound_provider(None)
    set_nidaq_bound_names_provider(None)
    set_nidaq_cross_section_handlers()
    set_nidaq_rescan_handler(None)
    yield
    set_nidaq_inventory_provider(None)
    set_nidaq_bound_provider(None)
    set_nidaq_bound_names_provider(None)
    set_nidaq_cross_section_handlers()
    set_nidaq_rescan_handler(None)


# ---------------------------------------------------------------------------
# Factory dispatch
# ---------------------------------------------------------------------------


def test_factory_picks_nidaq_channels_field_for_annotated_param(qtbot: Any) -> None:
    """The capa_widget=nidaq_channels json_schema_extra opts the field into
    the hardware-aware widget instead of the JSON-fallback path.
    """
    field_info = NIDAQAdapterParams.model_fields["channels"]
    widget = build_field_widget(field_info.annotation, field_info, field_name="channels")
    qtbot.addWidget(widget)
    assert isinstance(widget, NIDAQChannelsField)


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_set_value_with_dicts_round_trips_through_pydantic(qtbot: Any) -> None:
    """A dict shape that came out of TOML round-trips back through the
    widget and validates as ``NIDAQChannelConfig``."""
    widget = NIDAQChannelsField()
    qtbot.addWidget(widget)
    rows = [
        {
            "kind": "thermocouple",
            "physical_channel": "cDAQ1Mod1/ai0",
            "name": "TC_a",
            "thermocouple_type": "K",
            "min_val": 0.0,
            "max_val": 1000.0,
            "cjc_source": "BUILT_IN",
            "units": "DEG_C",
        }
    ]
    widget.set_value(rows)
    out = widget.value()
    assert len(out) == 1
    adapter = TypeAdapter(NIDAQChannelConfig)
    parsed = adapter.validate_python(out[0])
    assert isinstance(parsed, NIDAQThermocoupleConfig)
    assert parsed.name == "TC_a"
    assert parsed.physical_channel == "cDAQ1Mod1/ai0"


def test_set_value_accepts_pydantic_model_instances(qtbot: Any) -> None:
    widget = NIDAQChannelsField()
    qtbot.addWidget(widget)
    model = NIDAQThermocoupleConfig(
        kind="thermocouple",
        physical_channel="cDAQ1Mod1/ai2",
        name="TC_x",
        thermocouple_type="K",
        min_val=0.0,
        max_val=500.0,
        units="DEG_C",
    )
    widget.set_value([model])
    out = widget.value()
    assert out[0]["name"] == "TC_x"
    assert out[0]["thermocouple_type"] == "K"


def test_empty_value_produces_empty_list(qtbot: Any) -> None:
    widget = NIDAQChannelsField()
    qtbot.addWidget(widget)
    widget.set_value([])
    assert widget.value() == []


# ---------------------------------------------------------------------------
# Add from inventory
# ---------------------------------------------------------------------------


def test_add_menu_populates_from_inventory_provider(qtbot: Any) -> None:
    set_nidaq_inventory_provider(
        lambda: {
            "cDAQ1": {
                "device": "cDAQ1",
                "ai_channels": ["cDAQ1Mod1/ai0", "cDAQ1Mod1/ai1"],
            }
        }
    )
    widget = NIDAQChannelsField()
    qtbot.addWidget(widget)
    widget._rebuild_add_menu()
    actions = widget._add_menu.actions()
    texts = [a.text() for a in actions]
    # Header + one entry per ai channel + separator.
    assert any("cDAQ1" in t for t in texts)
    assert "cDAQ1Mod1/ai0" in texts
    assert "cDAQ1Mod1/ai1" in texts


def test_add_menu_greys_out_already_used_physical_channels(qtbot: Any) -> None:
    set_nidaq_inventory_provider(
        lambda: {
            "cDAQ1": {
                "device": "cDAQ1",
                "ai_channels": ["cDAQ1Mod1/ai0", "cDAQ1Mod1/ai1"],
            }
        }
    )
    widget = NIDAQChannelsField()
    qtbot.addWidget(widget)
    widget.set_value(
        [
            {
                "kind": "thermocouple",
                "physical_channel": "cDAQ1Mod1/ai0",
                "name": "TC_a",
                "thermocouple_type": "K",
                "min_val": 0.0,
                "max_val": 1000.0,
                "units": "DEG_C",
            }
        ]
    )
    widget._rebuild_add_menu()
    by_text = {a.text(): a for a in widget._add_menu.actions()}
    used_action = next((a for t, a in by_text.items() if "cDAQ1Mod1/ai0" in t), None)
    free_action = by_text.get("cDAQ1Mod1/ai1")
    assert used_action is not None
    assert not used_action.isEnabled()
    assert free_action is not None
    assert free_action.isEnabled()


def test_add_menu_shows_placeholder_when_no_inventory(qtbot: Any) -> None:
    # Provider returns empty mapping — Setup-tab path on a machine
    # with no NI driver installed.
    set_nidaq_inventory_provider(lambda: {})
    widget = NIDAQChannelsField()
    qtbot.addWidget(widget)
    widget._rebuild_add_menu()
    texts = [a.text() for a in widget._add_menu.actions()]
    assert any("No NI inventory" in t for t in texts)


def test_add_menu_offers_rescan_when_handler_is_wired(qtbot: Any) -> None:
    called: list[bool] = []
    set_nidaq_inventory_provider(lambda: {})
    set_nidaq_rescan_handler(lambda: called.append(True))
    widget = NIDAQChannelsField()
    qtbot.addWidget(widget)
    widget._rebuild_add_menu()
    rescan = next(a for a in widget._add_menu.actions() if "Rescan" in a.text())
    assert rescan.isEnabled()
    rescan.trigger()
    assert called == [True]


def test_add_from_inventory_inserts_thermocouple_default(qtbot: Any) -> None:
    set_nidaq_inventory_provider(
        lambda: {"cDAQ1": {"device": "cDAQ1", "ai_channels": ["cDAQ1Mod1/ai0"]}}
    )
    widget = NIDAQChannelsField()
    qtbot.addWidget(widget)
    widget._on_add_from_inventory("cDAQ1Mod1/ai0")
    out = widget.value()
    assert len(out) == 1
    assert out[0]["physical_channel"] == "cDAQ1Mod1/ai0"
    assert out[0]["kind"] == "thermocouple"
    # Default to K-type / 0-1000 / DEG_C — matches the typical NI 9214 setup.
    assert out[0]["thermocouple_type"] == "K"
    assert out[0]["units"] == "DEG_C"


# ---------------------------------------------------------------------------
# Add blank / Remove
# ---------------------------------------------------------------------------


def test_add_blank_creates_row_without_physical_channel(qtbot: Any) -> None:
    widget = NIDAQChannelsField()
    qtbot.addWidget(widget)
    widget._on_add_blank()
    out = widget.value()
    assert len(out) == 1
    # Empty physical_channel is normally stripped, but a fresh blank row
    # carries an empty string that's stripped on export.
    assert out[0].get("physical_channel") in (None, "", "")  # absent or empty
    assert out[0]["kind"] == "thermocouple"


def test_remove_drops_selected_row(qtbot: Any) -> None:
    widget = NIDAQChannelsField()
    qtbot.addWidget(widget)
    widget.set_value(
        [
            {
                "kind": "thermocouple",
                "physical_channel": "cDAQ1Mod1/ai0",
                "name": "TC_a",
                "thermocouple_type": "K",
                "min_val": 0.0,
                "max_val": 1000.0,
                "units": "DEG_C",
            },
            {
                "kind": "thermocouple",
                "physical_channel": "cDAQ1Mod1/ai1",
                "name": "TC_b",
                "thermocouple_type": "K",
                "min_val": 0.0,
                "max_val": 1000.0,
                "units": "DEG_C",
            },
        ]
    )
    widget._table.selectRow(0)
    widget._on_remove()
    out = widget.value()
    assert len(out) == 1
    assert out[0]["name"] == "TC_b"


def test_bound_lookup_filters_by_join_context(qtbot: Any) -> None:
    """The same NI display name on another device/task is not this row."""
    set_nidaq_bound_provider(
        lambda: {
            ("cdaq1", "task_a", "TC_shared"),
            ("cdaq2", "task_b", "TC_shared"),
        }
    )
    widget = NIDAQChannelsField()
    qtbot.addWidget(widget)
    widget.set_join_context(device_name="cdaq2", task_name="task_b")
    assert widget._lookup_bound_for_field("TC_shared") == {("cdaq2", "task_b", "TC_shared")}


def test_unbound_payload_includes_join_context(qtbot: Any) -> None:
    widget = NIDAQChannelsField()
    qtbot.addWidget(widget)
    widget.set_join_context(device_name="cdaq2", task_name="task_b")
    widget.set_value(
        [
            {
                "kind": "thermocouple",
                "physical_channel": "cDAQ2Mod1/ai0",
                "name": "TC_shared",
                "thermocouple_type": "K",
                "min_val": 0.0,
                "max_val": 1000.0,
                "units": "DEG_C",
            }
        ]
    )
    [entry] = widget._unbound_declared()
    assert entry["device_name"] == "cdaq2"
    assert entry["task_name"] == "task_b"


# ---------------------------------------------------------------------------
# Validation banner
# ---------------------------------------------------------------------------


def test_duplicate_name_surfaces_in_banner(qtbot: Any) -> None:
    widget = NIDAQChannelsField()
    qtbot.addWidget(widget)
    widget.set_value(
        [
            {
                "kind": "thermocouple",
                "physical_channel": "cDAQ1Mod1/ai0",
                "name": "TC_a",
                "thermocouple_type": "K",
                "min_val": 0.0,
                "max_val": 1000.0,
                "units": "DEG_C",
            },
            {
                "kind": "thermocouple",
                "physical_channel": "cDAQ1Mod1/ai1",
                "name": "TC_a",  # collision
                "thermocouple_type": "K",
                "min_val": 0.0,
                "max_val": 1000.0,
                "units": "DEG_C",
            },
        ]
    )
    assert not widget._banner.isHidden()
    assert "TC_a" in widget._banner.text()


def test_duplicate_physical_channel_surfaces_in_banner(qtbot: Any) -> None:
    widget = NIDAQChannelsField()
    qtbot.addWidget(widget)
    widget.set_value(
        [
            {
                "kind": "thermocouple",
                "physical_channel": "cDAQ1Mod1/ai0",
                "name": "TC_a",
                "thermocouple_type": "K",
                "min_val": 0.0,
                "max_val": 1000.0,
                "units": "DEG_C",
            },
            {
                "kind": "thermocouple",
                "physical_channel": "cDAQ1Mod1/ai0",  # collision
                "name": "TC_b",
                "thermocouple_type": "K",
                "min_val": 0.0,
                "max_val": 1000.0,
                "units": "DEG_C",
            },
        ]
    )
    assert not widget._banner.isHidden()
    assert "cDAQ1Mod1/ai0" in widget._banner.text()


def test_clean_table_hides_banner(qtbot: Any) -> None:
    widget = NIDAQChannelsField()
    qtbot.addWidget(widget)
    widget.set_value(
        [
            {
                "kind": "thermocouple",
                "physical_channel": "cDAQ1Mod1/ai0",
                "name": "TC_a",
                "thermocouple_type": "K",
                "min_val": 0.0,
                "max_val": 1000.0,
                "units": "DEG_C",
            }
        ]
    )
    assert widget._banner.isHidden()
