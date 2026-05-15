""":class:`Batch` — runs a wrapped procedure N times with cooldown between iterations.

Plan §11 line 964: catches the most-common composition (replicate runs) so
each researcher does not re-implement it badly.

Each iteration produces its **own bundle** (a fresh :class:`ExperimentEngine`
is constructed per iteration), so a crashed iteration does not contaminate
its siblings. The parent batch id is mirrored into every child bundle's
``manifest.json.custom['batch']`` block so the runs.sqlite catalog can pull
the family back together.

Notes on lifecycle:

* Batch runs as a *procedure* in the parent engine, but it does not need
  any of the parent engine's adapters / fan-out — its job is to orchestrate
  N child engines. The parent engine's adapters still run (the data is
  available to the parent procedure via the databus) but Batch ignores
  them; the data of substance lands in the *child* bundles.
* The simplest, least surprising shape is therefore: parent engine arms a
  zero-device hardware profile, runs Batch, Batch executes children one at
  a time inside its own task. We document this in the README; the
  config-time linter doesn't enforce it because some experiments may legit
  want shared sensor data correlated against children.

This implementation is conservative — synchronous iteration, no parallelism,
fail-fast on the first crashed child. Concurrent-batch support may be added
when the parameter sweeps grow.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

import anyio
from pydantic import BaseModel, ConfigDict, Field, model_validator

from capa.experiment.config import ExperimentConfig, ProcedureRef
from capa.experiment.procedures.base import (
    ChannelRequirement,
    Problem,
    Procedure,
    ProcedureContext,
    ProcedureError,
)

PROCEDURE_ID = "capa.builtin.batch"
PROCEDURE_NAME = "Batch (replicate runs)"
PROCEDURE_VERSION = "0.1.0"


def _mint_batch_id() -> str:
    """Eight bytes of urandom hex — ample for distinguishing batches.

    Mirrored into every child bundle's ``manifest.json.custom['batch']`` so
    the runs.sqlite catalog can join the family back together.
    """
    return secrets.token_hex(8)


class BatchConfig(BaseModel):
    """``config.procedure.config`` shape for :class:`Batch`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    iterations: int = Field(ge=1, le=10_000)
    """How many child runs to execute. Upper bound is a sanity cap; raise
    if you genuinely need it."""

    cooldown_s: float = Field(default=0.0, ge=0)
    """Delay between iterations. Useful when the rig physically needs to
    cool / re-stabilize between runs."""

    inner: ProcedureRef
    """The procedure each child run executes. Defaults to RecipeRunner in
    practice, but Batch deliberately does not hardcode that — a custom
    procedure can also be batched."""

    sample_id_template: str = Field(default="{base}_{idx:03d}")
    """``str.format`` template used to derive each child's
    ``sample.id`` from the parent's ``sample.id``. ``{base}`` is the parent
    id, ``{idx}`` is the 0-indexed iteration."""

    fail_fast: bool = True
    """When ``True`` (default), the first crashed child stops the batch.
    ``False`` keeps going — useful for parameter sweeps where one bad
    config shouldn't block the rest."""

    @model_validator(mode="after")
    def _check_template(self) -> BatchConfig:
        try:
            self.sample_id_template.format(base="x", idx=0)
        except (KeyError, IndexError, ValueError) as exc:
            raise ValueError(
                f"sample_id_template {self.sample_id_template!r} is not a valid format string: {exc}"
            ) from exc
        # Forbid recursive batching at the schema level — Batch invoking
        # Batch is almost always a misuse and the lifecycle gets hairy.
        if self.inner.id == PROCEDURE_ID:
            raise ValueError("Batch cannot wrap another Batch")
        return self


@dataclass(slots=True)
class Batch(Procedure):
    """Run an inner procedure N times, each in its own bundle."""

    id: ClassVar[str] = PROCEDURE_ID
    name: ClassVar[str] = PROCEDURE_NAME
    version: ClassVar[str] = PROCEDURE_VERSION
    config_model: ClassVar[type] = BatchConfig
    required_capabilities: ClassVar[tuple[str, ...]] = ()
    required_channels: ClassVar[tuple[ChannelRequirement, ...]] = ()

    iterations: int = 1
    cooldown_s: float = 0.0
    inner_ref: ProcedureRef = field(
        default_factory=lambda: ProcedureRef(id="capa.builtin.recipe_runner")
    )
    sample_id_template: str = "{base}_{idx:03d}"
    fail_fast: bool = True
    _runs_root: Path | None = field(default=None, init=False)
    _batch_id: str = field(default_factory=_mint_batch_id, init=False)

    @classmethod
    def from_config(cls, raw: dict[str, object] | None) -> Batch:
        cfg = BatchConfig.model_validate(raw or {})
        return cls(
            iterations=cfg.iterations,
            cooldown_s=cfg.cooldown_s,
            inner_ref=cfg.inner,
            sample_id_template=cfg.sample_id_template,
            fail_fast=cfg.fail_fast,
        )

    @property
    def batch_id(self) -> str:
        return self._batch_id

    def configure_runs_root(self, runs_root: Path) -> None:
        """Engine-side hook so Batch knows where to put child bundles.

        The parent engine knows its own runs_root; it calls this immediately
        before :meth:`run` so the batch can spawn children alongside its
        parent (rather than pulling the runs_root from a global)."""
        self._runs_root = runs_root

    async def preflight(self, ctx: ProcedureContext) -> list[Problem]:
        problems: list[Problem] = []
        if self.iterations < 1:
            raise ProcedureError(f"Batch.iterations must be >=1; got {self.iterations}")
        if self._runs_root is None:
            problems.append(
                Problem(
                    code="batch.no_runs_root",
                    message=(
                        "engine did not call Batch.configure_runs_root() before run; "
                        "child bundles have nowhere to go"
                    ),
                    blocking=True,
                )
            )
        return problems

    async def run(self, ctx: ProcedureContext) -> None:
        # Lazy import: avoids a circular dependency between
        # capa.runtime.headless (which imports procedures.base) and Batch
        # (which spawns child runs through the headless entry point).
        from capa.runtime.headless import run_headless  # noqa: PLC0415
        from capa.runtime.session import make_run_id  # noqa: PLC0415

        assert self._runs_root is not None  # guaranteed by preflight
        ctx.logger.info(
            "batch.start",
            batch_id=self._batch_id,
            iterations=self.iterations,
            inner=self.inner_ref.id,
        )
        ctx.bundle_writer.write_event(
            kind="batch.started",
            message=f"batch {self._batch_id}: {self.iterations} iterations of {self.inner_ref.id}",
            severity="info",
            source=f"procedure:{PROCEDURE_ID}",
            t_mono_ns=ctx.clock.t_mono_ns(),
            t_utc=ctx.clock.to_wall_ns(ctx.clock.t_mono_ns()),
            metadata={
                "batch_id": self._batch_id,
                "iterations": self.iterations,
                "inner": self.inner_ref.id,
            },
        )

        completed = 0
        crashed: list[str] = []
        base_sample_id = ctx.config.sample.id

        for idx in range(self.iterations):
            if ctx.external_stop.is_set():
                ctx.logger.info("batch.interrupted", at=idx, completed=completed)
                break

            child_sample_id = self.sample_id_template.format(base=base_sample_id, idx=idx)
            child_config = _build_child_config(
                parent=ctx.config,
                inner=self.inner_ref,
                child_sample_id=child_sample_id,
                batch_id=self._batch_id,
                iteration=idx,
            )
            child_run_id = make_run_id(sample_id=child_sample_id)

            ctx.logger.info(
                "batch.child.start",
                idx=idx,
                run_id=child_run_id,
                sample_id=child_sample_id,
            )
            ctx.bundle_writer.write_event(
                kind="batch.child.started",
                message=f"batch {self._batch_id} child {idx}: {child_run_id}",
                severity="info",
                source=f"procedure:{PROCEDURE_ID}",
                t_mono_ns=ctx.clock.t_mono_ns(),
                t_utc=ctx.clock.to_wall_ns(ctx.clock.t_mono_ns()),
                metadata={
                    "batch_id": self._batch_id,
                    "child_idx": idx,
                    "child_run_id": child_run_id,
                    "child_sample_id": child_sample_id,
                },
            )

            # Each child gets its own bundle via a nested conductor stack.
            # ``run_headless`` opens a fresh :class:`WorkerPool` for the child
            # config and tears it down before returning — child resources
            # stay isolated from the parent's pool.
            result = await run_headless(
                child_config,
                runs_root=self._runs_root,
                run_id=child_run_id,
            )

            severity = "info" if result.run_status == "completed" else "warning"
            ctx.bundle_writer.write_event(
                kind="batch.child.ended",
                message=(
                    f"batch {self._batch_id} child {idx}: "
                    f"{result.run_status}/{result.bundle_status}"
                ),
                severity=severity,
                source=f"procedure:{PROCEDURE_ID}",
                t_mono_ns=ctx.clock.t_mono_ns(),
                t_utc=ctx.clock.to_wall_ns(ctx.clock.t_mono_ns()),
                metadata={
                    "batch_id": self._batch_id,
                    "child_idx": idx,
                    "child_run_id": child_run_id,
                    "run_status": result.run_status,
                    "bundle_status": result.bundle_status,
                    "exit_reason": result.exit_reason,
                    "bundle_path": str(result.bundle_path) if result.bundle_path else None,
                },
            )

            if result.run_status == "completed":
                completed += 1
            else:
                crashed.append(child_run_id)
                if self.fail_fast:
                    ctx.logger.error(
                        "batch.fail_fast",
                        crashed=child_run_id,
                        completed=completed,
                    )
                    break

            if idx + 1 < self.iterations and self.cooldown_s > 0:
                with anyio.move_on_after(self.cooldown_s):
                    await ctx.external_stop.wait()

        ctx.bundle_writer.write_event(
            kind="batch.ended",
            message=(
                f"batch {self._batch_id}: {completed}/{self.iterations} completed, "
                f"{len(crashed)} crashed"
            ),
            severity="info" if not crashed else "warning",
            source=f"procedure:{PROCEDURE_ID}",
            t_mono_ns=ctx.clock.t_mono_ns(),
            t_utc=ctx.clock.to_wall_ns(ctx.clock.t_mono_ns()),
            metadata={
                "batch_id": self._batch_id,
                "completed": completed,
                "crashed": crashed,
                "fail_fast": self.fail_fast,
            },
        )
        ctx.logger.info(
            "batch.end",
            batch_id=self._batch_id,
            completed=completed,
            crashed=len(crashed),
        )


def _build_child_config(
    *,
    parent: ExperimentConfig,
    inner: ProcedureRef,
    child_sample_id: str,
    batch_id: str,
    iteration: int,
) -> ExperimentConfig:
    """Derive a child :class:`ExperimentConfig` from the parent.

    Differences vs the parent:

    * ``procedure`` swaps to the inner :class:`ProcedureRef`.
    * ``sample.id`` is the templated child id; other sample fields carry over.
    * ``custom`` gains a ``batch`` block recording the parent batch id and
      iteration index. The parent batch id ends up in the bundle's
      manifest, which is what the catalog cross-indexes.
    """
    new_sample = parent.sample.model_copy(update={"id": child_sample_id})
    new_custom: dict[str, Any] = dict(parent.custom)
    new_custom["batch"] = {
        "batch_id": batch_id,
        "iteration": iteration,
        "parent_sample_id": parent.sample.id,
    }
    return parent.model_copy(
        update={
            "procedure": inner,
            "sample": new_sample,
            "custom": new_custom,
        }
    )


__all__ = [
    "PROCEDURE_ID",
    "PROCEDURE_NAME",
    "PROCEDURE_VERSION",
    "Batch",
    "BatchConfig",
]
