"""Tests for form-generator polish."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from capa.ui.forms import build_form
from capa.ui.forms.widgets import _ComboBoxField, _PathField

# ---------------------------------------------------------------------------
# StrEnum routing.
# ---------------------------------------------------------------------------


class _Mode(StrEnum):
    INERT = "inert"
    OXIDATIVE = "oxidative"
    REDUCING = "reducing"


class _StrEnumModel(BaseModel):
    mode: _Mode = _Mode.INERT


def test_strenum_field_renders_combobox(qtbot: Any) -> None:
    form = build_form(_StrEnumModel)
    qtbot.addWidget(form)
    widget = form._fields["mode"]
    assert isinstance(widget, _ComboBoxField)
    assert tuple(widget._choices) == (_Mode.INERT, _Mode.OXIDATIVE, _Mode.REDUCING)


def test_strenum_value_round_trip(qtbot: Any) -> None:
    form = build_form(_StrEnumModel)
    qtbot.addWidget(form)
    widget = form._fields["mode"]
    widget.set_value(_Mode.OXIDATIVE)
    assert widget.value() == _Mode.OXIDATIVE
    # Validate back through the model.
    parsed = _StrEnumModel.model_validate({"mode": widget.value()})
    assert parsed.mode == _Mode.OXIDATIVE


# ---------------------------------------------------------------------------
# Directory-mode Path widget.
# ---------------------------------------------------------------------------


class _DirModel(BaseModel):
    bundle_root: Path = Field(default=Path("runs"), json_schema_extra={"capa_path_mode": "dir"})


class _FileModel(BaseModel):
    config_file: Path = Field(default=Path("config.toml"))


def test_directory_mode_path_field(qtbot: Any) -> None:
    form = build_form(_DirModel)
    qtbot.addWidget(form)
    widget = form._fields["bundle_root"]
    assert isinstance(widget, _PathField)
    assert widget._mode == "dir"


def test_default_path_field_is_file_mode(qtbot: Any) -> None:
    form = build_form(_FileModel)
    qtbot.addWidget(form)
    widget = form._fields["config_file"]
    assert isinstance(widget, _PathField)
    assert widget._mode == "file"
