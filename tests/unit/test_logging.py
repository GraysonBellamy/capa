"""Tests for :mod:`capa.core.logging`."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import structlog

from capa.core.logging import (
    bind_run_context,
    clear_run_context,
    configure_logging,
    configure_pre_run_logging,
)
from capa.storage.log_sink import LogSink


def _reset_root_handlers() -> None:
    """Strip every handler so each test starts clean. Called from fixture
    teardown via the LogSink lifecycle."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_capa_owned", False):
            root.removeHandler(handler)
            handler.close()


class TestConfigureLogging:
    def test_emits_json_lines_to_bundle_log(self, tmp_path: Path) -> None:
        sink = LogSink(tmp_path)
        try:
            log = configure_logging(bundle_log_sink=sink, console_renderer=False)
            bind_run_context(run_id="R1", operator_id="abr", procedure_id="P1")
            log.info("hello", count=3)
        finally:
            clear_run_context()
            sink.close()
            _reset_root_handlers()

        text = (tmp_path / "run.log").read_text(encoding="utf-8")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        assert lines, "expected at least one JSON line in run.log"
        record = json.loads(lines[-1])
        assert record["event"] == "hello"
        assert record["run_id"] == "R1"
        assert record["operator_id"] == "abr"
        assert record["procedure_id"] == "P1"
        assert record["count"] == 3
        assert record["level"] == "INFO"

    def test_idempotent_replaces_handlers(self, tmp_path: Path) -> None:
        (tmp_path / "a").mkdir(exist_ok=True)
        (tmp_path / "b").mkdir(exist_ok=True)
        sink_a = LogSink(tmp_path / "a")
        sink_b = LogSink(tmp_path / "b")
        try:
            configure_logging(bundle_log_sink=sink_a)
            configure_logging(bundle_log_sink=sink_b)
            log = structlog.get_logger("capa")
            bind_run_context(run_id="R2", operator_id="abr")
            log.info("after-reconfig")
        finally:
            clear_run_context()
            sink_a.close()
            sink_b.close()
            _reset_root_handlers()

        # First sink got nothing post-reconfigure.
        a_text = (tmp_path / "a" / "run.log").read_text(encoding="utf-8")
        b_text = (tmp_path / "b" / "run.log").read_text(encoding="utf-8")
        assert "after-reconfig" not in a_text
        assert "after-reconfig" in b_text

    def test_clear_run_context_drops_bindings(self, tmp_path: Path) -> None:
        sink = LogSink(tmp_path)
        try:
            log = configure_logging(bundle_log_sink=sink, console_renderer=False)
            bind_run_context(run_id="R3", operator_id="abr")
            log.info("first")
            clear_run_context()
            log.info("second")
        finally:
            sink.close()
            _reset_root_handlers()

        records = [
            json.loads(ln) for ln in (tmp_path / "run.log").read_text().splitlines() if ln.strip()
        ]
        first = next(r for r in records if r["event"] == "first")
        second = next(r for r in records if r["event"] == "second")
        assert first["run_id"] == "R3"
        assert "run_id" not in second


class TestPreRunLogging:
    def test_writes_to_dated_file(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr("capa.core.logging.PRE_RUN_LOG_DIR", tmp_path / "logs")
        try:
            log = configure_pre_run_logging(level="DEBUG")
            log.info("pre-run-event", k="v")
        finally:
            _reset_root_handlers()

        log_files = list((tmp_path / "logs").glob("capa-*.log"))
        assert len(log_files) == 1
        text = log_files[0].read_text(encoding="utf-8")
        assert "pre-run-event" in text
