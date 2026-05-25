"""Worked example: hold one channel at a setpoint for N seconds.

The smallest procedure that does something useful with a real device write.
Drives `target_channel` to `value`, waits `duration_s`, exits. Every write
goes through `ctx.authorization` -> `ctx.dispatcher`, so each command lands
in the bundle's event log with `issued_by` and `authorization_id` stamps.

Pair this with the [Writing a procedure](../../../../docs/extending/writing-a-procedure.md)
tutorial.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import ClassVar

import anyio
from pydantic import BaseModel, ConfigDict, Field

from capa.experiment.procedures.base import (
    ChannelRequirement,
    Problem,
    Procedure,
    ProcedureContext,
)


class HoldSetpointConfig(BaseModel):
    """Validated config for `HoldSetpoint`.

    Lands at `ExperimentConfig.procedure.config` in the experiment YAML.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_channel: str = Field(
        description="Channel name to command. Must resolve in the active hardware profile.",
    )
    value: float = Field(description="Setpoint to command at run start.")
    duration_s: float = Field(gt=0, description="How long to hold before exiting.")


@dataclass(slots=True)
class HoldSetpoint(Procedure):
    """Drive `target_channel` to `value`, hold for `duration_s`, exit."""

    id: ClassVar[str] = "hello.procedure.hold_setpoint"
    name: ClassVar[str] = "Hello Procedure: Hold Setpoint"
    version: ClassVar[str] = "0.1.0"
    config_model: ClassVar[type] = HoldSetpointConfig
    required_capabilities: ClassVar[tuple[str, ...]] = ("HAS_SETPOINT",)
    required_channels: ClassVar[tuple[ChannelRequirement, ...]] = ()
    uses_method: ClassVar[bool] = False

    target_channel: str = ""
    value: float = 0.0
    duration_s: float = 0.0

    @classmethod
    def from_config(cls, raw: dict[str, object] | None) -> HoldSetpoint:
        cfg = HoldSetpointConfig.model_validate(raw or {})
        return cls(
            target_channel=cfg.target_channel,
            value=cfg.value,
            duration_s=cfg.duration_s,
        )

    async def preflight(self, ctx: ProcedureContext) -> list[Problem]:
        problems: list[Problem] = []
        try:
            ctx.instruments.resolve(self.target_channel)
        except Exception:
            problems.append(
                Problem(
                    code="hello.unknown_channel",
                    message=f"target_channel {self.target_channel!r} is not bound",
                    severity="error",
                    blocking=True,
                )
            )
        return problems

    async def run(self, ctx: ProcedureContext) -> None:
        resolved = ctx.instruments.resolve(self.target_channel)
        device = resolved.binding.device

        cmd = ctx.authorization.issue(
            kind="set_setpoint",
            target=self.target_channel,
            payload={
                "value": self.value,
                "channel": self.target_channel,
                "device": device,
            },
        )
        result = await ctx.dispatcher.dispatch(device, cmd)
        ctx.bundle_writer.write_event(
            kind="hello.setpoint.commanded",
            message=f"set {self.target_channel}={self.value} (accepted={result.accepted})",
            severity="info",
            source=f"procedure:{self.id}",
            t_mono_ns=ctx.clock.t_mono_ns(),
            t_utc=datetime.now(UTC),
            metadata={"value": self.value, "accepted": result.accepted},
        )

        with anyio.move_on_after(self.duration_s):
            await ctx.external_stop.wait()
