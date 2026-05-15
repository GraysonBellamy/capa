"""structlog configuration and run-context binding.

Plan §13.1. Every log line carries a context-bound ``run_id``,
``procedure_id``, ``step_id`` (when applicable), and ``operator_id``. During a
run, logs are tee'd to:

* **stdout** — human-friendly console renderer for headless / dev,
* **``run.log``** — JSON lines, captured for archival debugging via
  :class:`~capa.storage.log_sink.LogSink`,
* **the status bar / events dock** — WARNING and above only.

Pre-run logs (config errors, plugin load failures) go to
``~/.capa/logs/capa-YYYYMMDD.log`` since there is no bundle yet to write into.

The configuration is intentionally context-var based: spawned tasks inside an
AnyIO task group inherit the bound run/procedure/step ids without explicit
plumbing. ``configure_logging`` returns a bound :class:`structlog.BoundLogger`
that is also the root for downstream callers; the same processors handle every
log site.
"""

from __future__ import annotations

import contextlib
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars
from structlog.types import EventDict, Processor

from capa.storage.log_sink import LogSink

PRE_RUN_LOG_DIR = Path.home() / ".capa" / "logs"
"""Where pre-run logs (config errors, plugin failures) land. Plan §13.1."""


def _utc_timestamper(_logger: object, _method_name: str, event_dict: EventDict) -> EventDict:
    """Stamp every event with an ISO-8601 UTC timestamp."""
    event_dict["timestamp"] = datetime.now(UTC).isoformat()
    return event_dict


def _level_uppercase(_logger: object, method_name: str, event_dict: EventDict) -> EventDict:
    """Normalize ``method_name`` (debug/info/warn/...) into a ``level`` field."""
    event_dict["level"] = method_name.upper() if method_name != "warn" else "WARNING"
    return event_dict


_BASE_PROCESSORS: tuple[Processor, ...] = (
    structlog.contextvars.merge_contextvars,
    _level_uppercase,
    _utc_timestamper,
    structlog.processors.StackInfoRenderer(),
    structlog.dev.set_exc_info,
)
"""Processors run before the renderer fork. Order matters: contextvars first
so a per-task ``run_id`` always lands on the event dict, then level/timestamp,
then exception detail. ``set_exc_info`` (instead of ``format_exc_info``) keeps
the raw exception on the event dict so the chosen renderer (ConsoleRenderer
on stdout, JSONRenderer to ``run.log``) can format it natively — pre-
formatting via ``format_exc_info`` confuses the dev renderer."""


class _StructlogJSONLineHandler(logging.Handler):
    """Receive already-rendered JSON strings from structlog and append them
    to the underlying :class:`LogSink`.

    Plan §13.1: we want the bundle's ``run.log`` to be one JSON object per
    line. structlog's :class:`structlog.stdlib.ProcessorFormatter` will hand
    us the rendered string in :attr:`LogRecord.msg` after the JSONRenderer
    runs.
    """

    __slots__ = ("_sink",)

    def __init__(self, sink: LogSink) -> None:
        super().__init__(level=logging.DEBUG)
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._sink.write_line(self.format(record))
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._sink.flush()
        super().close()


def configure_logging(
    *,
    bundle_log_sink: LogSink | None = None,
    stdout_level: str = "INFO",
    file_level: str = "DEBUG",
    console_renderer: bool = True,
) -> structlog.stdlib.BoundLogger:
    """Configure root logging + structlog and return a bound logger.

    Tees output to:

    * **stdout** with a human-friendly renderer (or JSON if ``console_renderer
      = False``), at ``stdout_level``.
    * the **bundle's ``run.log``** as JSON lines via ``bundle_log_sink``, at
      ``file_level``.

    ``configure_logging`` is idempotent — a second call replaces the existing
    handlers. This is intentional: the engine reconfigures at run-start to
    point the file handler at the bundle that just opened.

    The returned logger is just ``structlog.get_logger("capa")`` after
    configuration; callers can also call :func:`structlog.get_logger` with
    their own name.
    """
    structlog.configure(
        processors=[
            *_BASE_PROCESSORS,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )

    root = logging.getLogger()
    # Root tracks the lower of the two levels so neither handler is starved.
    root.setLevel(min(_level(stdout_level), _level(file_level)))
    # Replace any existing capa-installed handlers; leave third-party handlers
    # alone (e.g. pytest caplog).
    for handler in list(root.handlers):
        if getattr(handler, "_capa_owned", False):
            root.removeHandler(handler)
            handler.close()

    console_handler = logging.StreamHandler()
    console_handler.setLevel(_level(stdout_level))
    if console_renderer:
        console_formatter = structlog.stdlib.ProcessorFormatter(
            processor=structlog.dev.ConsoleRenderer(colors=os.isatty(2)),
            foreign_pre_chain=list(_BASE_PROCESSORS),
        )
    else:
        console_formatter = structlog.stdlib.ProcessorFormatter(
            processor=structlog.processors.JSONRenderer(),
            foreign_pre_chain=list(_BASE_PROCESSORS),
        )
    console_handler.setFormatter(console_formatter)
    console_handler._capa_owned = True  # type: ignore[attr-defined]
    root.addHandler(console_handler)

    if bundle_log_sink is not None:
        json_formatter = structlog.stdlib.ProcessorFormatter(
            processor=structlog.processors.JSONRenderer(sort_keys=False),
            foreign_pre_chain=list(_BASE_PROCESSORS),
        )
        file_handler = _StructlogJSONLineHandler(bundle_log_sink)
        file_handler.setLevel(_level(file_level))
        file_handler.setFormatter(json_formatter)
        file_handler._capa_owned = True  # type: ignore[attr-defined]
        root.addHandler(file_handler)

    logger: structlog.stdlib.BoundLogger = structlog.get_logger("capa")
    return logger


def _level(name: str) -> int:
    """Coerce ``"DEBUG" | "INFO" | "WARNING" | "ERROR" | "CRITICAL"`` to int."""
    value = logging.getLevelNamesMapping().get(name.upper())
    if value is None:
        raise ValueError(f"unknown log level: {name!r}")
    return value


def bind_run_context(
    *,
    run_id: str,
    operator_id: str,
    procedure_id: str | None = None,
    step_id: str | None = None,
    **extra: Any,
) -> None:
    """Bind run-scoped context vars; every subsequent log line picks them up."""
    ctx: dict[str, Any] = {"run_id": run_id, "operator_id": operator_id}
    if procedure_id is not None:
        ctx["procedure_id"] = procedure_id
    if step_id is not None:
        ctx["step_id"] = step_id
    ctx.update(extra)
    bind_contextvars(**ctx)


def clear_run_context() -> None:
    """Clear every bound context var. Called at run end."""
    clear_contextvars()


def configure_pre_run_logging(*, level: str = "INFO") -> structlog.stdlib.BoundLogger:
    """Configure stdout-only + ``~/.capa/logs/capa-YYYYMMDD.log`` for the
    pre-run path.

    Used by ``capa validate`` and the early phase of ``capa run --headless``
    (config load, plugin lock check) where there's no bundle yet.
    """
    PRE_RUN_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = PRE_RUN_LOG_DIR / f"capa-{datetime.now(UTC).strftime('%Y%m%d')}.log"

    structlog.configure(
        processors=[
            *_BASE_PROCESSORS,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )

    root = logging.getLogger()
    root.setLevel(_level(level))
    for handler in list(root.handlers):
        if getattr(handler, "_capa_owned", False):
            root.removeHandler(handler)
            handler.close()

    console_handler = logging.StreamHandler()
    console_handler.setLevel(_level(level))
    console_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=structlog.dev.ConsoleRenderer(colors=os.isatty(2)),
            foreign_pre_chain=list(_BASE_PROCESSORS),
        )
    )
    console_handler._capa_owned = True  # type: ignore[attr-defined]
    root.addHandler(console_handler)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(_level(level))
    file_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=structlog.processors.JSONRenderer(sort_keys=False),
            foreign_pre_chain=list(_BASE_PROCESSORS),
        )
    )
    file_handler._capa_owned = True  # type: ignore[attr-defined]
    root.addHandler(file_handler)

    logger: structlog.stdlib.BoundLogger = structlog.get_logger("capa")
    return logger


__all__ = [
    "PRE_RUN_LOG_DIR",
    "bind_run_context",
    "clear_run_context",
    "configure_logging",
    "configure_pre_run_logging",
]
