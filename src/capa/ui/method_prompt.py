""":class:`MethodPromptDialog` — the operator's side of a method ``prompt`` step.

While a ``prompt`` step waits, the
:class:`~capa.experiment.executor.MethodExecutor` publishes a
``method.prompt`` :class:`~capa.runtime.emissions.ProcedureTick` every
couple of seconds. This dialog shows its title and message with a
**Continue** button; Continue sends a ``confirm_prompt``
:class:`~capa.experiment.procedures.base.OperatorCommand` carrying the
prompt's id. The ``"closed"`` tick, an ``"open"`` tick for a different
prompt, or the run leaving RUNNING takes the dialog down.

Non-modal (``show()``, never ``exec()``), so Stop, the plots and the
manual dock stay usable while it's up. Esc and the title-bar close do
nothing: the run is blocked on the answer, so the only ways out are
Continue and stopping the run.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Final

from PySide6.QtGui import QCloseEvent, QFont
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from capa.experiment.executor import PROMPT_TICK_KIND
from capa.experiment.procedures.base import OperatorCommand
from capa.runtime.emissions import ProcedureTick
from capa.ui.state import RunUiState

if TYPE_CHECKING:
    from capa.ui.state import RunController

RESEND_AFTER_S: Final = 5.0
"""After Continue, the button stays disabled until the prompt closes. If
the prompt is still open this long after the click, the confirm was
lost: the button comes back so the operator can send it again."""


class MethodPromptDialog(QDialog):
    """Shows the method prompt that is waiting, and confirms it."""

    def __init__(self, controller: RunController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._controller = controller
        self._prompt_id: str | None = None
        self._sent_at: float | None = None
        self._dismissing = False

        self.setObjectName("method_prompt_dialog")
        self.setWindowTitle("Method prompt")
        self.setModal(False)
        self.setMinimumWidth(460)

        self._title_label = QLabel(self)
        title_font = QFont(self._title_label.font())
        title_font.setPointSize(title_font.pointSize() + 4)
        title_font.setBold(True)
        self._title_label.setFont(title_font)
        self._title_label.setWordWrap(True)

        self._message_label = QLabel(self)
        message_font = QFont(self._message_label.font())
        message_font.setPointSize(message_font.pointSize() + 2)
        self._message_label.setFont(message_font)
        self._message_label.setWordWrap(True)

        self._step_label = QLabel(self)
        self._step_label.setStyleSheet("color: #667085;")
        self._status_label = QLabel(self)
        self._status_label.setStyleSheet("color: #667085;")

        self._continue_btn = QPushButton("Continue", self)
        self._continue_btn.setDefault(True)
        self._continue_btn.setMinimumHeight(40)
        self._continue_btn.clicked.connect(self._on_continue)

        footer = QHBoxLayout()
        footer.addWidget(self._step_label)
        footer.addStretch(1)
        footer.addWidget(self._status_label)
        footer.addWidget(self._continue_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(self._title_label)
        layout.addSpacing(6)
        layout.addWidget(self._message_label)
        layout.addSpacing(12)
        layout.addLayout(footer)

        controller.procedure_tick_received.connect(self._on_tick)
        controller.state_changed.connect(self._on_state)

    @property
    def prompt_id(self) -> str | None:
        """Id of the prompt on screen, or ``None`` when none is."""
        return self._prompt_id

    # --------------------------------------------------------------- slots

    def _on_tick(self, tick: object) -> None:
        if not isinstance(tick, ProcedureTick):
            return
        payload = tick.payload
        if payload.get("kind") != PROMPT_TICK_KIND:
            return
        prompt_id = payload.get("prompt_id")
        if not isinstance(prompt_id, str):
            return
        state = payload.get("state")
        if state == "closed":
            if prompt_id == self._prompt_id:
                self._dismiss()
            return
        if state != "open":
            return
        if prompt_id != self._prompt_id:
            self._present(prompt_id, payload)
            return
        # A republish of the prompt on screen.
        if not self.isVisible():
            self.show()
        if self._sent_at is not None and time.monotonic() - self._sent_at > RESEND_AFTER_S:
            self._sent_at = None
            self._continue_btn.setEnabled(True)
            self._status_label.setText("Still waiting: click Continue again")

    def _on_continue(self) -> None:
        if self._prompt_id is None:
            return
        cmd = OperatorCommand(kind="confirm_prompt", metadata={"prompt_id": self._prompt_id})
        if self._controller.send_operator_command(cmd):
            self._sent_at = time.monotonic()
            self._continue_btn.setEnabled(False)
            self._status_label.setText("Continuing…")
        else:
            self._status_label.setText("Not delivered: no active run")

    def _on_state(self, state: object) -> None:
        if isinstance(state, RunUiState) and state is not RunUiState.RUNNING:
            self._dismiss()

    # --------------------------------------------------------------- internals

    def _present(self, prompt_id: str, payload: Mapping[str, object]) -> None:
        self._prompt_id = prompt_id
        self._sent_at = None
        self.setWindowTitle(str(payload.get("title") or "Method prompt"))
        self._title_label.setText(str(payload.get("title") or "Operator action"))
        self._message_label.setText(str(payload.get("message") or ""))
        index = payload.get("step_index")
        count = payload.get("step_count")
        if isinstance(index, int) and index >= 0 and isinstance(count, int):
            self._step_label.setText(f"Step {index + 1} of {count}")
        else:
            self._step_label.setText("")
        self._status_label.setText("")
        self._continue_btn.setEnabled(True)
        self.show()
        self.raise_()
        self.activateWindow()
        self._continue_btn.setFocus()
        QApplication.beep()

    def _dismiss(self) -> None:
        self._prompt_id = None
        self._sent_at = None
        if self.isVisible():
            self._dismissing = True
            try:
                self.hide()
            finally:
                self._dismissing = False

    # --------------------------------------------------------------- Qt overrides

    def reject(self) -> None:
        """Esc does nothing: the run is waiting on this answer."""

    def closeEvent(self, event: QCloseEvent | None) -> None:  # noqa: N802 — Qt override
        """The title-bar close does nothing while a prompt is waiting."""
        if event is None:
            return
        if self._dismissing or self._prompt_id is None:
            event.accept()
            return
        event.ignore()


__all__ = ["RESEND_AFTER_S", "MethodPromptDialog"]
