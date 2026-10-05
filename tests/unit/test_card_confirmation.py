"""A manual card's confirmation box must not run inside the dispatching task.

Under qasync a modal box spins a nested Qt event loop. Opened inside a task,
every other task that wakes meanwhile — a camera's preview drain — fails with
"Cannot enter into task … while another task … is being executed" and is never
resumed. This runs the card's confirmation on a real qasync loop with such a
task alongside.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import qasync
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from capa.ui.manual.cards.base import DeviceCard
from capa.ui.state import RunController
from capa.ui.statusbar import OperatorIdProvider


def test_other_tasks_keep_running_while_a_confirmation_is_open(
    qtbot: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    card = DeviceCard(
        name="dev",
        title="dev",
        controller=RunController(runs_root=tmp_path),
        operator_provider=OperatorIdProvider(initial="opA"),
    )
    qtbot.addWidget(card)
    ticks = 0
    ticks_while_open: list[int] = []

    def modal(*_args: Any, **_kwargs: Any) -> QMessageBox.StandardButton:
        # A modal box's nested event loop, open for 200 ms.
        opened_at = ticks
        nested = QEventLoop()
        QTimer.singleShot(200, nested.quit)
        nested.exec()
        ticks_while_open.append(ticks - opened_at)
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", modal)

    async def scenario() -> tuple[bool, list[dict[str, Any]]]:
        nonlocal ticks
        errors: list[dict[str, Any]] = []
        asyncio.get_running_loop().set_exception_handler(lambda _loop, ctx: errors.append(ctx))

        async def drain() -> None:  # like a camera's preview drain
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        task = asyncio.create_task(drain())
        await asyncio.sleep(0.03)
        confirmed = await card._confirm("Switch range?")
        task.cancel()
        return confirmed, errors

    loop = qasync.QEventLoop(QApplication.instance())
    asyncio.set_event_loop(loop)
    try:
        with loop:
            confirmed, errors = loop.run_until_complete(scenario())
    finally:
        asyncio.set_event_loop(None)
    assert confirmed is True
    assert errors == []
    assert ticks_while_open[0] > 0
