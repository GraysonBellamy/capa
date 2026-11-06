""":class:`MethodPromptDialog` — shows a waiting method prompt and confirms it.

The executor side (ticks out, ``confirm_prompt`` in) is covered in
``tests/unit/test_executor.py``; the whole loop through a real
:class:`RunController` in ``tests/integration/test_method_prompt.py``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from capa.experiment.executor import PROMPT_TICK_KIND
from capa.runtime.emissions import ProcedureTick
from capa.ui import method_prompt as method_prompt_module
from capa.ui.method_prompt import MethodPromptDialog
from capa.ui.state import RunUiState


def _controller(*, accepts: bool = True) -> MagicMock:
    controller = MagicMock()
    controller.send_operator_command = MagicMock(return_value=accepts)
    return controller


def _tick(state: str, prompt_id: str, **extra: object) -> ProcedureTick:
    payload: dict[str, object] = {
        "kind": PROMPT_TICK_KIND,
        "state": state,
        "prompt_id": prompt_id,
        "title": "Level 3 of 12",
        "message": "Slide the heater to stop B, then Continue.",
        "step_index": 2,
        "step_count": 10,
    }
    payload.update(extra)
    return ProcedureTick(procedure_id="capa.builtin.recipe_runner", t_mono_ns=0, payload=payload)


def _dialog(qtbot: Any, controller: MagicMock) -> MethodPromptDialog:
    dialog = MethodPromptDialog(controller)
    qtbot.addWidget(dialog)
    return dialog


def test_open_tick_shows_the_prompt(qtbot: Any) -> None:
    dialog = _dialog(qtbot, _controller())
    dialog._on_tick(_tick("open", "2:1"))
    assert dialog.isVisible()
    assert dialog.prompt_id == "2:1"
    assert dialog._title_label.text() == "Level 3 of 12"
    assert dialog._message_label.text() == "Slide the heater to stop B, then Continue."
    assert dialog._step_label.text() == "Step 3 of 10"
    assert dialog._continue_btn.isEnabled()


def test_continue_sends_confirm_with_the_prompt_id(qtbot: Any) -> None:
    controller = _controller()
    dialog = _dialog(qtbot, controller)
    dialog._on_tick(_tick("open", "2:1"))
    dialog._continue_btn.click()
    cmd = controller.send_operator_command.call_args.args[0]
    assert cmd.kind == "confirm_prompt"
    assert cmd.metadata == {"prompt_id": "2:1"}
    assert not dialog._continue_btn.isEnabled()
    assert dialog.isVisible()  # stays up until the executor closes the prompt


def test_closed_tick_takes_the_dialog_down(qtbot: Any) -> None:
    dialog = _dialog(qtbot, _controller())
    dialog._on_tick(_tick("open", "2:1"))
    dialog._on_tick(_tick("closed", "2:1"))
    assert not dialog.isVisible()
    assert dialog.prompt_id is None


def test_closed_tick_for_another_prompt_is_ignored(qtbot: Any) -> None:
    dialog = _dialog(qtbot, _controller())
    dialog._on_tick(_tick("open", "5:2"))
    dialog._on_tick(_tick("closed", "2:1"))
    assert dialog.isVisible()
    assert dialog.prompt_id == "5:2"


def test_next_prompt_replaces_the_one_on_screen(qtbot: Any) -> None:
    controller = _controller()
    dialog = _dialog(qtbot, controller)
    dialog._on_tick(_tick("open", "2:1"))
    dialog._continue_btn.click()
    dialog._on_tick(_tick("open", "4:2", title="Level 4 of 12"))
    assert dialog.prompt_id == "4:2"
    assert dialog._title_label.text() == "Level 4 of 12"
    assert dialog._continue_btn.isEnabled()


def test_lost_confirm_re_enables_continue(qtbot: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(method_prompt_module, "RESEND_AFTER_S", 0.0)
    dialog = _dialog(qtbot, _controller())
    dialog._on_tick(_tick("open", "2:1"))
    dialog._continue_btn.click()
    assert not dialog._continue_btn.isEnabled()
    dialog._on_tick(_tick("open", "2:1"))  # still open: the confirm never arrived
    assert dialog._continue_btn.isEnabled()
    assert "again" in dialog._status_label.text()


def test_undelivered_confirm_keeps_continue_enabled(qtbot: Any) -> None:
    dialog = _dialog(qtbot, _controller(accepts=False))
    dialog._on_tick(_tick("open", "2:1"))
    dialog._continue_btn.click()
    assert dialog._continue_btn.isEnabled()
    assert "Not delivered" in dialog._status_label.text()


def test_run_leaving_running_dismisses(qtbot: Any) -> None:
    dialog = _dialog(qtbot, _controller())
    dialog._on_tick(_tick("open", "2:1"))
    dialog._on_state(RunUiState.RUNNING)
    assert dialog.isVisible()
    dialog._on_state(RunUiState.DRAINING)
    assert not dialog.isVisible()
    assert dialog.prompt_id is None


def test_escape_and_close_do_not_dismiss_a_waiting_prompt(qtbot: Any) -> None:
    dialog = _dialog(qtbot, _controller())
    dialog._on_tick(_tick("open", "2:1"))
    dialog.reject()
    assert dialog.isVisible()
    assert dialog.close() is False
    assert dialog.isVisible()


def test_other_ticks_are_ignored(qtbot: Any) -> None:
    dialog = _dialog(qtbot, _controller())
    dialog._on_tick(
        ProcedureTick(
            procedure_id="capa.builtin.heat_flux_tune", t_mono_ns=0, payload={"phase": "x"}
        )
    )
    dialog._on_tick(object())
    assert not dialog.isVisible()
    assert dialog.prompt_id is None
