""":class:`RecipeRunner` — the standard "walk a Method" procedure.

90% of standard runs use this. The body is one line: hand the
method to :class:`~capa.experiment.executor.MethodExecutor` and let the
executor do the rest. Procedures that need anything custom subclass this or
write their own.

The procedure declares no special capabilities — every step kind already
goes through the executor's adapter command path which validates against the
:class:`~capa.experiment.config.HardwareProfile` at preflight.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from capa.experiment.executor import MethodExecutor
from capa.experiment.procedures.base import (
    ChannelRequirement,
    Problem,
    Procedure,
    ProcedureContext,
    ProcedureError,
)

PROCEDURE_ID = "capa.builtin.recipe_runner"
PROCEDURE_NAME = "Standard Recipe Run"
PROCEDURE_VERSION = "0.1.0"


class RecipeRunnerConfig(BaseModel):
    """``config.procedure.config`` shape for :class:`RecipeRunner`.

    every procedure exposes a Pydantic model so the auto-form
    generator can build the Run-tab editor without per-plugin Qt code.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    auto_acknowledge_prompts: bool = Field(default=False)
    """Headless tests / batch runs set this to ``True`` so ``prompt`` steps
    don't block on operator input."""

    notes: str | None = Field(default=None)
    """Operator-typed comment captured into ``manifest.json.custom``."""


@dataclass(slots=True)
class RecipeRunner(Procedure):
    """Walk a :class:`Method` to completion.

    Constructor params come from ``ExperimentConfig.procedure.config``
    validated against :class:`RecipeRunnerConfig`.
    """

    id: ClassVar[str] = PROCEDURE_ID
    name: ClassVar[str] = PROCEDURE_NAME
    version: ClassVar[str] = PROCEDURE_VERSION
    config_model: ClassVar[type] = RecipeRunnerConfig
    required_capabilities: ClassVar[tuple[str, ...]] = ()
    required_channels: ClassVar[tuple[ChannelRequirement, ...]] = ()
    uses_method: ClassVar[bool] = True

    auto_acknowledge_prompts: bool = False
    notes: str | None = None

    @classmethod
    def from_config(cls, raw: dict[str, object] | None) -> RecipeRunner:
        cfg = RecipeRunnerConfig.model_validate(raw or {})
        return cls(
            auto_acknowledge_prompts=cfg.auto_acknowledge_prompts,
            notes=cfg.notes,
        )

    async def preflight(self, ctx: ProcedureContext) -> list[Problem]:
        problems: list[Problem] = []
        if ctx.config.method is None:
            raise ProcedureError(
                "RecipeRunner requires ExperimentConfig.method; for record-only "
                "runs use capa.builtin.free_run instead."
            )
        if ctx.method_executor is None:
            problems.append(
                Problem(
                    code="recipe_runner.no_executor",
                    message="engine did not wire a MethodExecutor",
                    severity="error",
                    blocking=True,
                )
            )
        return problems

    async def run(self, ctx: ProcedureContext) -> None:
        assert ctx.config.method is not None  # guaranteed by preflight
        executor: MethodExecutor = ctx.method_executor  # type: ignore[assignment]
        executor.auto_acknowledge_prompts = self.auto_acknowledge_prompts
        ctx.logger.info(
            "recipe_runner.start",
            method=ctx.config.method.name,
            steps=len(ctx.config.method.steps),
            notes=self.notes,
        )
        await executor.run_to_completion(ctx.config.method)
        ctx.logger.info("recipe_runner.end")


__all__ = [
    "PROCEDURE_ID",
    "PROCEDURE_NAME",
    "PROCEDURE_VERSION",
    "RecipeRunner",
    "RecipeRunnerConfig",
]
