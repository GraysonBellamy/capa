""":class:`GuiStallWatchdog` — a stalled GUI thread leaves its stacks in the log."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from capa.ui.watchdog import GuiStallWatchdog


def test_header_written_and_no_dump_while_the_thread_turns(qtbot: Any, tmp_path: Path) -> None:
    log = tmp_path / "gui-stall.log"
    watchdog = GuiStallWatchdog(log, stall_after_s=5.0)
    try:
        qtbot.wait(100)
    finally:
        watchdog.stop()
    text = log.read_text(encoding="utf-8")
    assert text.startswith("--- capa gui started")
    assert "Timeout" not in text


def test_stall_dumps_every_thread(qtbot: Any, tmp_path: Path) -> None:
    log = tmp_path / "gui-stall.log"
    watchdog = GuiStallWatchdog(log, stall_after_s=0.3)
    try:
        time.sleep(0.8)  # the main thread stops turning; nothing re-arms
    finally:
        watchdog.stop()
    text = log.read_text(encoding="utf-8")
    assert "Timeout (0:00:00.300000)!" in text
    assert "test_stall_dumps_every_thread" in text  # the stalled frame is named


def test_stop_is_idempotent(qtbot: Any, tmp_path: Path) -> None:
    watchdog = GuiStallWatchdog(tmp_path / "gui-stall.log")
    watchdog.stop()
    watchdog.stop()
