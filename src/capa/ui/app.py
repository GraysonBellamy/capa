"""qasync bootstrap — bring up :class:`MainWindow` in the asyncio loop.

Called from ``capa.cli.run:run`` when ``--gui`` is in effect (or no
``--headless`` flag is passed). The GUI takes the same arguments as the
headless ``capa run``: an :class:`ExperimentConfig` path, an optional runs
root, and an optional ``plugins.lock``.

qasync makes the asyncio loop the same thread as Qt's main GUI thread, so
the engine's task group, the DataBus pump, and Qt slot dispatch all share
one cooperative event loop. There is no producer-consumer thread boundary
to manage.

Shutdown contract
-----------------
After ``loop.run_forever()`` returns, we call :func:`_hard_exit_after_gui`
which calls ``os._exit(0)`` — bypassing Python's interpreter-shutdown
thread-join.  We deliberately do **not** call ``loop.close()`` before that
point.

Why skipping ``loop.close()`` is correct
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
``loop.close()`` calls ``QThreadExecutor.shutdown(wait=True)``, which sends
a stop-sentinel to each ``_QThreadWorker`` queue and then calls
``QThread.wait()`` on every worker.  A worker can only dequeue the sentinel
*after* finishing its current job.  If that job is a blocking C call —
``_duvc.list_devices`` (DirectShow), NI-DAQmx device enumeration, or a
``libav`` container open — the worker is stuck indefinitely.

``contextlib.suppress(BaseException)`` cannot help here: it only catches
Python exceptions.  A blocking ``QThread.wait()`` that never returns never
raises, so the suppress context never fires.  Everything placed after
``loop.close()`` — ``catalog.__exit__`` and ``_hard_exit_after_gui`` — is
permanently unreachable.

The :class:`~capa.ui.shutdown.ShutdownCoordinator` has already cancelled
every lifecycle-registered task and sealed every open bundle by the time
``run_forever()`` returns, so skipping ``loop.close()`` loses nothing of
consequence.  ``os._exit(0)`` terminates the process immediately, reclaiming
all OS handles.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
from pathlib import Path

import qasync
import structlog
from PySide6.QtWidgets import QApplication

from capa.core.errors import CapaError
from capa.core.logging import configure_pre_run_logging
from capa.core.plugins_lock import PluginsLock
from capa.experiment.config import ExperimentConfig
from capa.runtime.recovery import recover_active_bundle_checkpoint
from capa.storage.catalog import RunCatalog
from capa.ui.main_window import MainWindow

_logger = structlog.get_logger("capa.ui.app")


def _hard_exit_after_gui(rc: int) -> None:
    """Terminate the process after the GUI loop has closed.

    Several C extensions capa loads at import or first-use time (NI's
    DAQmx runtime, ``duvc_ctl`` for UVC camera control, libav under
    PyAV) leave behind non-daemon helper threads that Python's
    interpreter shutdown waits for indefinitely on Windows. The
    :class:`~capa.ui.shutdown.ShutdownCoordinator` has already drained
    every cooperative resource by the time we reach this function —
    bundles are sealed, the catalog handle is closed, run state is
    persisted — so the safe move is to bypass the threading-shutdown
    join entirely with ``os._exit``. Without it the terminal hangs
    until the operator hits Ctrl-C.

    The diagnostic snapshot of remaining non-daemon threads lets us
    spot future regressions (a new C extension that leaks a thread)
    without having to reproduce the hang interactively.
    """
    leftover = [
        t for t in threading.enumerate() if not t.daemon and t is not threading.main_thread()
    ]
    if leftover:
        _logger.info(
            "ui.gui.hard_exit_threads_alive",
            count=len(leftover),
            names=[t.name for t in leftover],
        )
    logging.shutdown()
    os._exit(rc)


def run_gui(
    *,
    config_path: Path | None,
    runs_root: Path,
    plugins_lock: PluginsLock | None = None,
    repo_root: Path | None = None,
    lockfile_source: Path | None = None,
) -> int:
    """Run the GUI until the operator quits. Returns a process exit code.

    Args:
        config_path: optional config to load on launch. ``None`` opens the
            window with no active experiment; the operator can use
            File→Open afterwards.
        runs_root: bundle parent directory.
        plugins_lock, repo_root, lockfile_source: forwarded to the engine
            on every run.

    Exit code: 0 on clean window close (regardless of what individual runs
    did inside; per-run results are visible in the bundle catalog and run
    log). Non-zero only on hard startup failures (e.g. config refused).
    """
    configure_pre_run_logging()

    runs_root.mkdir(parents=True, exist_ok=True)

    initial_config: ExperimentConfig | None = None
    if config_path is not None:
        try:
            initial_config = ExperimentConfig.load(config_path)
        except CapaError as exc:
            _logger.error("ui.initial_config_invalid", path=str(config_path), error=str(exc))
            sys.stderr.write(f"capa: invalid config {config_path}: {exc}\n")
            return 2

    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    if os.environ.get("CAPA_SCREENSHOT_PROBE"):
        # Dev-only, gated import — keep lazy so the probe module isn't
        # loaded in normal runs.
        from capa.ui import _screenshot_probe  # noqa: PLC0415

        _screenshot_probe.install()

    # Engine's catalog usage is per-run; the window holds one catalog handle
    # for the lifetime of the GUI so every run inserts/updates against it.
    catalog = RunCatalog(runs_root)
    catalog.__enter__()
    try:
        # Catalog-side orphan flip handles the ``running`` → ``crashed``
        # row transition. The active-bundle checkpoint recovery is the
        # bundle-side counterpart: if the previous capa hard-exited
        # between bundle creation and finalize, mark its manifest as
        # crashed so bundle-only consumers see the same status. The
        # helper is bounded internally and tolerates absent / live-PID
        # checkpoints.
        catalog.flip_orphans()
        recovery = recover_active_bundle_checkpoint(runs_root)
        if recovery.status == "reconciled":
            _logger.info(
                "shutdown.bundle_checkpoint_recovered",
                run_id=recovery.checkpoint.run_id if recovery.checkpoint else None,
            )
        window = MainWindow(
            runs_root=runs_root,
            catalog=catalog,
            repo_root=repo_root,
            lockfile_source=lockfile_source,
            plugins_lock=plugins_lock,
            configure_logging_for_bundle=True,
            initial_config=initial_config,
            initial_config_path=config_path,
        )
        window.show()
        loop.run_forever()
        # NOTE: loop.close() is intentionally omitted. See module docstring.
    finally:
        catalog.__exit__(None, None, None)
    # Hard-exit so the process actually terminates even if a C-extension
    # background thread (NI-DAQmx, duvc_ctl, libav) would otherwise hold
    # Python's interpreter shutdown open. See :func:`_hard_exit_after_gui`.
    _hard_exit_after_gui(0)
    return 0  # unreachable; keeps mypy / the signature honest


__all__ = ["run_gui"]
