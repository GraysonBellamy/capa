""":class:`GuiStallWatchdog` — record what the GUI thread was doing when it froze.

qasync runs the asyncio loop on the Qt main thread, so one blocking call
anywhere on that loop freezes the window ("Not Responding") while the
conductor thread carries on recording. Nothing in the run's log says why:
the log only sees the threads that are still running.

A :class:`QTimer` on the main thread re-arms
:func:`faulthandler.dump_traceback_later` every :data:`ARM_INTERVAL_MS`.
While the thread turns, the dump never fires. If it stalls for longer than
:data:`STALL_AFTER_S`, faulthandler's own C thread writes the stack of
every Python thread to the log, and again every :data:`STALL_AFTER_S` for
as long as the stall lasts. It needs no GIL, so a thread spinning in Python
is caught as well as one blocked in C.
"""

from __future__ import annotations

import faulthandler
import os
from datetime import datetime
from pathlib import Path
from typing import Final, TextIO

from PySide6.QtCore import QObject, QTimer

STALL_AFTER_S: Final[float] = 15.0
"""Main-thread silence that counts as a stall. Long enough that a slow
config load or a big plot redraw doesn't fill the log."""

ARM_INTERVAL_MS: Final[int] = 2000
"""How often the main thread re-arms the dump; well inside the stall window."""

LOG_NAME: Final[str] = "gui-stall.log"
"""File in the runs root that collects the dumps, appended across launches."""


class GuiStallWatchdog(QObject):
    """Dumps every thread's stack to ``log_path`` while the GUI thread is stalled."""

    def __init__(
        self,
        log_path: Path,
        *,
        stall_after_s: float = STALL_AFTER_S,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._stall_after_s = stall_after_s
        self._file: TextIO | None = log_path.open("a", encoding="utf-8")
        self._file.write(
            f"--- capa gui started {datetime.now().isoformat(timespec='seconds')} "
            f"pid={os.getpid()}; a dump below means the GUI thread stalled "
            f"for {stall_after_s:g} s\n"
        )
        self._file.flush()
        self._timer = QTimer(self)
        self._timer.setInterval(ARM_INTERVAL_MS)
        self._timer.timeout.connect(self._arm)
        self._timer.start()
        self._arm()

    def _arm(self) -> None:
        if self._file is None:
            return
        # Each call replaces the pending dump, so it only fires if no
        # re-arm arrives within the window.
        faulthandler.dump_traceback_later(self._stall_after_s, repeat=True, file=self._file)

    def stop(self) -> None:
        """Disarm and close the log. Idempotent."""
        self._timer.stop()
        if self._file is None:
            return
        faulthandler.cancel_dump_traceback_later()
        self._file.close()
        self._file = None


__all__ = ["ARM_INTERVAL_MS", "LOG_NAME", "STALL_AFTER_S", "GuiStallWatchdog"]
