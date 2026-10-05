"""Tests for the channel-name pickers (``capa_widget = "channel" | "command_channel"``)."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt

from capa.channels.spec import ChannelKind
from capa.experiment.method import HoldStep, SafeShutdownStep
from capa.ui.forms import ChannelOption, build_form, channel_options_from_hardware
from capa.ui.forms.widgets._channels import ChannelCombo, _ChannelField
from capa.ui.forms.widgets._collection import _DictStrFloatField

_OPTIONS = (
    ChannelOption(name="heater.pv", kind="process_var", unit="degC"),
    ChannelOption(name="heater.setpoint", kind="setpoint", unit="degC"),
    ChannelOption(name="purge.flow", kind="mfc_flow", unit="slpm"),
    ChannelOption(name="balance.mass", kind="mass", unit="g"),
)


def _items(combo: ChannelCombo) -> list[str]:
    return [combo.itemText(i) for i in range(combo.count())]


def _hold(**overrides: Any) -> HoldStep:
    data: dict[str, Any] = {"target": {"name": "heater.setpoint"}, "value": 600.0}
    data.update(overrides)
    data.setdefault("duration_s", 60.0)
    return HoldStep.model_validate(data)


def test_target_lists_only_commandable_channels(qtbot: Any) -> None:
    form = build_form(HoldStep, initial=_hold())
    qtbot.addWidget(form)
    form.set_channel_options(_OPTIONS)
    combo = form.findChild(ChannelCombo)
    assert combo is not None
    assert _items(combo) == ["heater.setpoint", "purge.flow"]
    assert combo.currentText() == "heater.setpoint"
    assert combo.itemData(1, Qt.ItemDataRole.ToolTipRole) == "mfc_flow · slpm"


def test_picking_a_target_round_trips_through_the_step(qtbot: Any) -> None:
    form = build_form(HoldStep, initial=_hold())
    qtbot.addWidget(form)
    form.set_channel_options(_OPTIONS)
    combo = form.findChild(ChannelCombo)
    assert combo is not None
    with qtbot.waitSignal(form.valuesChanged):
        combo.setCurrentIndex(_items(combo).index("purge.flow"))
    values = form.values()
    values["kind"] = "hold"
    assert HoldStep.model_validate(values).target.name == "purge.flow"


def test_unknown_target_is_kept_when_options_change(qtbot: Any) -> None:
    form = build_form(HoldStep, initial=_hold(target={"name": "furnace.sp"}))
    qtbot.addWidget(form)
    form.set_channel_options(_OPTIONS)
    form.set_channel_options(_OPTIONS[:2])
    assert form.values()["target"] == {"name": "furnace.sp"}


def test_end_condition_lists_every_channel(qtbot: Any) -> None:
    step = _hold(duration_s=None, end_condition={"channel": "balance.mass", "op": "<", "value": 1})
    form = build_form(HoldStep, initial=step)
    qtbot.addWidget(form)
    form.set_channel_options(_OPTIONS)
    fields = form.findChildren(_ChannelField)
    end_channel = next(f for f in fields if f.value() == "balance.mass")
    assert _items(end_channel._combo) == [o.name for o in _OPTIONS]


def test_cool_target_keys_are_pickers(qtbot: Any) -> None:
    step = SafeShutdownStep(cool_target={"heater.setpoint": 25.0})
    form = build_form(SafeShutdownStep, initial=step)
    qtbot.addWidget(form)
    form.set_channel_options(_OPTIONS)
    field = form.field_widget("cool_target")
    assert isinstance(field, _DictStrFloatField)
    # A row added after the options arrive gets them too.
    field._on_add()
    combos = form.findChildren(ChannelCombo)
    assert len(combos) == 2
    assert all(_items(c) == ["heater.setpoint", "purge.flow"] for c in combos)
    combos[1].setCurrentIndex(1)
    assert form.values()["cool_target"] == {"heater.setpoint": 25.0, "purge.flow": 0.0}


def test_typing_works_without_options(qtbot: Any) -> None:
    form = build_form(HoldStep, initial=_hold())
    qtbot.addWidget(form)
    combo = form.findChild(ChannelCombo)
    assert combo is not None
    assert combo.count() == 0
    combo.setEditText("heater.sp2")
    assert form.values()["target"] == {"name": "heater.sp2"}


def test_channel_options_from_hardware_skips_malformed_rows() -> None:
    hardware = {
        "channels": [
            {"name": "heater.setpoint", "kind": ChannelKind.SETPOINT, "unit": "degC"},
            {"name": "", "kind": "tc"},
            "not-a-row",
            {"kind": "tc"},
            {"name": "TC_top"},
        ]
    }
    assert channel_options_from_hardware(hardware) == (
        ChannelOption(name="heater.setpoint", kind="setpoint", unit="degC"),
        ChannelOption(name="TC_top"),
    )
    assert channel_options_from_hardware({}) == ()
