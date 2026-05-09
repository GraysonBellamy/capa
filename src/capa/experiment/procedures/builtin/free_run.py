""":class:`FreeRun` — record-only procedure with no method.

Plan §11. The smallest viable procedure that exercises the full engine
pipeline. Used for:

* ``capa run --headless freerun.yaml`` — the P0c outcome gate.
* Operator "just record what's happening" runs without a structured method.
* The simulator harness in tests.

Behaviour: write a ``free_run.started`` event, sleep for ``duration_s`` (or
wait on ``ctx.external_stop`` if duration is ``None``), write
``free_run.ended``. Returning normally is the clean-completion signal the
engine looks for.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

import anyio
from pydantic import BaseModel, ConfigDict, Field

from capa.experiment.procedures.base import (
    ChannelRequirement,
    Problem,
    Procedure,
    ProcedureContext,
    ProcedureError,
)

PROCEDURE_ID = "capa.builtin.free_run"
PROCEDURE_NAME = "Free Run (record-only)"
PROCEDURE_VERSION = "0.1.0"


class FreeRunConfig(BaseModel):
    """``config.procedure.config`` shape for :class:`FreeRun`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    duration_s: float | None = Field(default=None, ge=0)
    """Run length in seconds. ``None`` = run until ``external_stop`` fires.
    ``0`` = exit immediately after writing ``started`` / ``ended`` (used by
    integration tests that want the smallest possible bundle)."""


@dataclass(slots=True)
class FreeRun(Procedure):
    """Record-only procedure.

    Constructor params come from ``ExperimentConfig.procedure.config``
    validated against :class:`FreeRunConfig`.

    The protocol-required attributes (``id``, ``name``, ``version``,
    ``config_model``, ``required_capabilities``, ``required_channels``) are
    :class:`ClassVar` so the load-time contract check sees the actual values
    rather than the dataclass field descriptors. Plan §11 line 951 — the
    contract is enforced at registration, not at instance construction.
    """

    id: ClassVar[str] = PROCEDURE_ID
    name: ClassVar[str] = PROCEDURE_NAME
    version: ClassVar[str] = PROCEDURE_VERSION
    config_model: ClassVar[type] = FreeRunConfig
    required_capabilities: ClassVar[tuple[str, ...]] = ()
    required_channels: ClassVar[tuple[ChannelRequirement, ...]] = ()

    duration_s: float | None = None

    @classmethod
    def from_config(cls, raw: dict[str, object] | None) -> FreeRun:
        cfg = FreeRunConfig.model_validate(raw or {})
        return cls(duration_s=cfg.duration_s)

    async def preflight(self, ctx: ProcedureContext) -> list[Problem]:
        """FreeRun has no method. A method-bearing config is a misuse."""
        if ctx.config.method is not None:
            raise ProcedureError(
                "FreeRun cannot run with a method. Use capa.builtin.recipe_runner instead."
            )
        return []

    async def run(self, ctx: ProcedureContext) -> None:
        clock = ctx.clock
        ctx.logger.info(
            "free_run.start",
            duration_s=self.duration_s,
            sample_id=ctx.config.sample.id,
        )
        ctx.bundle_writer.write_event(
            kind="free_run.started",
            message=f"FreeRun started (duration_s={self.duration_s})",
            severity="info",
            source="procedure:capa.builtin.free_run",
            t_mono_ns=clock.t_mono_ns(),
            t_utc=clock.to_wall_ns(clock.t_mono_ns()),
            metadata={"duration_s": self.duration_s},
        )

        if self.duration_s is None:
            await ctx.external_stop.wait()
            stopped_by = "external_stop"
        elif self.duration_s == 0:
            stopped_by = "zero_duration"
        else:
            with anyio.move_on_after(self.duration_s) as scope:
                await ctx.external_stop.wait()
            stopped_by = "external_stop" if not scope.cancelled_caught else "duration_elapsed"

        ctx.bundle_writer.write_event(
            kind="free_run.ended",
            message=f"FreeRun ended ({stopped_by})",
            severity="info",
            source="procedure:capa.builtin.free_run",
            t_mono_ns=clock.t_mono_ns(),
            t_utc=clock.to_wall_ns(clock.t_mono_ns()),
            metadata={"stopped_by": stopped_by},
        )
        ctx.logger.info("free_run.end", stopped_by=stopped_by)


__all__ = ["PROCEDURE_ID", "PROCEDURE_NAME", "PROCEDURE_VERSION", "FreeRun", "FreeRunConfig"]
