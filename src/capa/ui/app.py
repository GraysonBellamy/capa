"""qasync bootstrap — bring up :class:`MainWindow` in the asyncio loop.

Plan §10. Called from ``capa.app:run`` when ``--gui`` is in effect (or no
``--headless`` flag is passed). The GUI takes the same arguments as the
headless ``capa run``: an :class:`ExperimentConfig` path, an optional runs
root, and an optional ``plugins.lock``.

qasync makes the asyncio loop the same thread as Qt's main GUI thread, so
the engine's task group, the DataBus pump, and Qt slot dispatch all share
one cooperative event loop. There is no producer-consumer thread boundary
to manage.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import qasync
import structlog
from PyQt6.QtWidgets import QApplication

from capa.core.errors import CapaError
from capa.core.logging import configure_pre_run_logging
from capa.core.plugins_lock import PluginsLock
from capa.experiment.config import ExperimentConfig
from capa.storage.catalog import RunCatalog
from capa.ui.main_window import MainWindow

_logger = structlog.get_logger("capa.ui.app")


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

    # Engine's catalog usage is per-run; the window holds one catalog handle
    # for the lifetime of the GUI so every run inserts/updates against it.
    catalog = RunCatalog(runs_root)
    catalog.__enter__()
    try:
        catalog.flip_orphans()
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
        with loop:
            loop.run_forever()
    finally:
        catalog.__exit__(None, None, None)
    return 0


__all__ = ["run_gui"]
