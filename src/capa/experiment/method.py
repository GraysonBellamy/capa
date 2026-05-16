""":class:`Step` types and :class:`Method` (segmented profile).

Ships the *schema*; most experiments are a list of Steps, and the
schema is the same whether the executor is present or not.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ChannelRef(BaseModel):
    """Pointer to a channel inside a method step.

    Kept as a typed model rather than a bare string so the configuration
    surface is forward-compatible with future addressing (e.g.
    ``device.parameter`` for parameters that aren't first-class channels).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str


class EndCondition(BaseModel):
    """Condition that ends a ``wait`` step (or interrupts a long ``hold``).

    example uses ``{channel: mass_loss_fraction, op: ">", value: 0.1}``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    channel: str
    op: Literal[">", ">=", "<", "<=", "=="]
    value: float


class AlarmOverride(BaseModel):
    """Per-step alarm-band override.

    A ``hold`` step at 800°C might temporarily widen the high-temp band to
    avoid spurious aborts during the spike of a fresh setpoint.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    alarm_id: str
    threshold: float | None = None
    disable: bool = False


# ---------------------------------------------------------------------------
# Step variants — discriminated by ``kind``.
# ---------------------------------------------------------------------------


class _StepBase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    notes: str | None = None
    safety_overrides: tuple[AlarmOverride, ...] = ()


class HoldStep(_StepBase):
    """Command a fixed value and wait for a duration or stability condition."""

    kind: Literal["hold"] = "hold"
    target: ChannelRef
    value: float
    duration_s: float | None = Field(
        default=None,
        ge=0,
        json_schema_extra={
            "capa_unit": "s",
            "capa_help": (
                "How long to hold the value. Leave unset if the step ends "
                "on an end_condition (e.g. mass loss fraction)."
            ),
        },
    )
    end_condition: EndCondition | None = None

    @model_validator(mode="after")
    def _check_at_least_one(self) -> HoldStep:
        if self.duration_s is None and self.end_condition is None:
            raise ValueError("hold step needs either duration_s or end_condition")
        return self


class RampStep(_StepBase):
    """Update a setpoint linearly over time.

    Either ``rate`` and ``end_value`` are given (compute duration from slope),
    or ``end_value`` and ``duration_s`` (compute slope from endpoints). At least
    one of those two pairings must hold.
    """

    kind: Literal["ramp"] = "ramp"
    target: ChannelRef
    start_value: float | None = None
    """``None`` means "ramp from the current setpoint"."""
    end_value: float
    rate_per_second: float | None = Field(
        default=None,
        json_schema_extra={
            "capa_help": (
                "Rate of change per second. Set this OR duration_s — capa "
                "derives the other from the endpoints."
            ),
        },
    )
    duration_s: float | None = Field(
        default=None,
        gt=0,
        json_schema_extra={
            "capa_unit": "s",
            "capa_help": (
                "Total ramp duration. Set this OR rate_per_second — capa "
                "derives the other from the endpoints."
            ),
        },
    )

    @model_validator(mode="after")
    def _check_consistent(self) -> RampStep:
        if self.rate_per_second is None and self.duration_s is None:
            raise ValueError("ramp step needs either rate_per_second or duration_s")
        return self


class SetpointStep(_StepBase):
    """Command an immediate setpoint change; do not wait."""

    kind: Literal["setpoint"] = "setpoint"
    target: ChannelRef
    value: float


class WaitStep(_StepBase):
    """Wait on a channel condition, event, or operator action.

    ``timeout_s`` is the engine-side deadline; on timeout the step's
    ``on_timeout`` policy applies (default: warn and continue).
    """

    kind: Literal["wait"] = "wait"
    end_condition: EndCondition | None = None
    duration_s: float | None = Field(
        default=None,
        ge=0,
        json_schema_extra={"capa_unit": "s"},
    )
    timeout_s: float | None = Field(
        default=None,
        gt=0,
        json_schema_extra={
            "capa_unit": "s",
            "capa_help": (
                "Engine-side deadline. If the end_condition has not fired "
                "by then, the on_timeout policy applies."
            ),
        },
    )
    on_timeout: Literal["warn", "abort", "safe_shutdown"] = "warn"

    @model_validator(mode="after")
    def _check(self) -> WaitStep:
        if self.duration_s is None and self.end_condition is None:
            raise ValueError("wait step needs either duration_s or end_condition")
        return self


class PromptStep(_StepBase):
    """Block until the operator acknowledges (e.g. "Ignite sample, then Continue")."""

    kind: Literal["prompt"] = "prompt"
    title: str = "Operator confirmation"
    message: str
    timeout_s: float | None = Field(
        default=None,
        gt=0,
        json_schema_extra={"capa_unit": "s"},
    )


class AcquireStep(_StepBase):
    """Record without changing any control outputs."""

    kind: Literal["acquire"] = "acquire"
    duration_s: float = Field(
        gt=0,
        json_schema_extra={"capa_unit": "s"},
    )


class SafeShutdownStep(_StepBase):
    """Reusable cooldown step. Also invoked by the safety system on
    ``safe_shutdown`` faults ()."""

    kind: Literal["safe_shutdown"] = "safe_shutdown"
    cool_target: dict[str, float] = Field(default_factory=dict)
    """``{channel_name: setpoint}`` to drive to during shutdown."""
    duration_s: float | None = Field(
        default=None,
        ge=0,
        json_schema_extra={"capa_unit": "s"},
    )


class CustomStep(_StepBase):
    """Plugin-defined step.

    Dispatched at runtime to a handler keyed by ``handler_id``. The plugin's
    Pydantic config schema validates ``params`` once the executor loads
    the plugin.
    """

    kind: Literal["custom"] = "custom"
    handler_id: str
    params: dict[str, Any] = Field(default_factory=dict)


Step = Annotated[
    HoldStep
    | RampStep
    | SetpointStep
    | WaitStep
    | PromptStep
    | AcquireStep
    | SafeShutdownStep
    | CustomStep,
    Field(discriminator="kind"),
]
"""Tagged union over every step variant."""


class Method(BaseModel):
    """Typed segmented profile.

    Most experiments are expressible as a list of Steps; for anything more
    complex you write a Procedure plugin and reuse the :class:`MethodExecutor`
    service for the parts that *are* expressible.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str = ""
    steps: tuple[Step, ...] = Field(min_length=1)

    def total_duration_s(self) -> float | None:
        """Sum of every step's ``duration_s`` if all steps have one.

        Returns ``None`` when at least one step has no fixed duration (a
        ``hold`` with only an end_condition, a ``wait`` with no time bound,
        a ``prompt`` step that the operator clears manually, etc.). Used by
        the camera disk-space preflight () — when the duration is
        unknowable, the preflight falls back to a configured default.
        """
        total = 0.0
        for step in self.steps:
            duration = getattr(step, "duration_s", None)
            if duration is None:
                return None
            total += float(duration)
        return total


__all__ = [
    "AcquireStep",
    "AlarmOverride",
    "ChannelRef",
    "CustomStep",
    "EndCondition",
    "HoldStep",
    "Method",
    "PromptStep",
    "RampStep",
    "SafeShutdownStep",
    "SetpointStep",
    "Step",
    "WaitStep",
]
