"""A recording :class:`DeviceAdapter` stub used by manual-control card tests.

The real Sartorius / Alicat adapters require live hardware; the bundled
sim adapters only declare a subset of capability flags (they only need to
satisfy the producer surface, not the manual-control surface). This stub
advertises a configurable capability set and records every command dispatch
into a buffer that tests can inspect.

Import path: ``tests.fixtures.stub_recording_adapter``. The
``_import_adapter_class`` resolver looks for ``StubRecordingAdapter`` first.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterable
from datetime import UTC, datetime
from typing import Any

from capa.devices.adapter import (
    AdapterLifecycle,
    Capability,
    CommandResult,
    DeviceCommand,
)
from capa.devices.records import DeviceEmission


class StubRecordingAdapter:
    """Stub adapter that captures every command for later inspection.

    Configure via ``params`` on the :class:`DeviceConfig`:

    * ``capabilities`` — iterable of :class:`Capability` flag *names* (str).
      Defaults to a Sartorius-shaped set.
    * ``accept_kinds`` — iterable of command-kind names this stub will
      accept. Anything else returns ``accepted=False``. Defaults to ``"*"``
      (accept anything).
    """

    name: str
    capabilities: frozenset[Capability]
    resource_id: str

    def __init__(
        self,
        *,
        name: str,
        capabilities: list[str] | None = None,
        accept_kinds: list[str] | None = None,
    ) -> None:
        self.name = name
        # The :class:`WorkerPool` groups adapters by ``resource_id``; the
        # stub uses ``stub:<name>`` so each test device gets its own
        # worker. Real adapters key on the underlying transport
        # (``serial:<port>``, ``daqmx:chassis:<id>``, …).
        self.resource_id = f"stub:{name}"
        flag_names = capabilities or [
            "HAS_TARE",
            "HAS_ZERO",
            "HAS_INTERNAL_CAL",
            "HAS_PARAMETER_CONFIG",
            "HAS_SETPOINT",
            "HAS_GAS_SELECT",
            "HAS_VALVE_HOLD",
            "HAS_TOTALIZER",
            "HAS_DISPLAY_CONTROL",
        ]
        self.capabilities = frozenset(Capability[n] for n in flag_names)
        self._accept_kinds: tuple[str, ...] | None = tuple(accept_kinds) if accept_kinds else None
        self._lifecycle = AdapterLifecycle()
        self.commands_received: list[DeviceCommand] = []
        self.open_count: int = 0
        self.close_count: int = 0

    async def open(self) -> None:
        if self._lifecycle.state in ("open", "running"):
            return
        self.open_count += 1
        self._lifecycle.open()

    async def close(self) -> None:
        if self._lifecycle.state == "closed":
            return
        self.close_count += 1
        self._lifecycle.close()

    async def start(self, *args: Any, **kwargs: Any) -> None:
        self._lifecycle.start()

    async def stop(self) -> None:
        self._lifecycle.stop()

    async def snapshot(self) -> DeviceEmission:  # pragma: no cover
        raise NotImplementedError("stub does not produce snapshots")

    def stream(self) -> AsyncIterable[DeviceEmission]:  # pragma: no cover
        async def _empty() -> AsyncIterable[DeviceEmission]:
            return
            yield

        return _empty()

    async def command(self, cmd: DeviceCommand) -> CommandResult:
        self.commands_received.append(cmd)
        accepted = self._accept_kinds is None or cmd.kind in self._accept_kinds
        await asyncio.sleep(0)  # exercise the await path
        return CommandResult(
            accepted=accepted,
            detail=f"stub {cmd.kind} ({'ok' if accepted else 'reject'})",
            t_mono_ns=time.monotonic_ns(),
            t_utc=datetime.now(UTC),
        )

    # Optional read-back hooks used by BalanceCard / AlicatCard refresh.
    async def read_last_cal_record(self) -> Any:
        return _CalRecord()

    async def read_gas_list(self) -> dict[int, str]:
        return {0: "Air", 1: "N2", 2: "Ar"}


class _CalRecord:
    timestamp = datetime(2026, 4, 22, 14, 30, tzinfo=UTC)
    result = "OK"


__all__ = ["StubRecordingAdapter"]
