""":func:`install_sigint_handler` — CLI SIGINT integration for the runtime.

The headless CLI ([`capa.app`](../app.py)) and any in-tree harness that drives
a :class:`~capa.runtime.conductor.Conductor` from a script wants a clean
two-stage Ctrl-C: the first stroke initiates a graceful shutdown, the second
falls back to the OS default handler so a wedged run can still be terminated.

Lives here (rather than in :mod:`capa.app`) so test harnesses and any
non-Typer driver can wire the same behaviour without depending on the CLI.
"""

from __future__ import annotations

import signal
import sys

import anyio


def install_sigint_handler(stop_event: anyio.Event) -> None:
    """Install a ``SIGINT`` handler that sets ``stop_event``.

    Idempotent against re-entry: a second Ctrl-C terminates the process via
    the OS default handler. Wires the conductor stack to the same
    operator-stop semantics the legacy engine had.
    """
    triggered = False

    def _handler(signum: int, frame: object) -> None:
        nonlocal triggered
        if triggered:
            sys.stderr.write("\nsecond SIGINT — exiting hard\n")
            sys.stderr.flush()
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            return
        triggered = True
        sys.stderr.write("\nSIGINT received — initiating graceful stop (Ctrl-C again to force)\n")
        sys.stderr.flush()
        stop_event.set()

    signal.signal(signal.SIGINT, _handler)


__all__ = ["install_sigint_handler"]
