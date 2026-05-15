"""Method profile graph — PyQtGraph helper.

Renders a single :class:`pyqtgraph.PlotWidget` showing the
operator-declared setpoint vs. elapsed time across the method.

Scope: kept deliberately small. ~90% of CAPA runs are a single
setpoint hold, so the graph's primary job is to make that case
self-evident (one horizontal segment, the setpoint clearly labelled).
The 10% case (ramps, multi-step setpoints) gets correct rendering but
no fancy interactions — no draggable points, no per-step annotations,
no zoom-to-step UX.

Per-target color comes from :func:`pyqtgraph.intColor`. Steps that
don't drive a setpoint (wait / prompt / acquire / safe_shutdown /
custom) render as faint vertical guide lines spanning the y-range, so
the graph still shows where in time those steps occur without
implying a setpoint command.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import pyqtgraph as pg
from PySide6.QtCore import Qt

from capa.experiment.method import (
    HoldStep,
    RampStep,
    SetpointStep,
    Step,
)


def _step_duration(step: Step) -> float:
    """Best-effort duration for the time axis. Steps with no fixed
    duration (a hold-until-condition, an open-ended wait, a prompt) get
    a small placeholder so the graph still advances; a real duration
    would require the operator to specify one."""
    duration = getattr(step, "duration_s", None)
    if duration is not None:
        return float(duration)
    return 1.0  # placeholder so the time axis advances


def _setpoint_targeting(step: Step) -> bool:
    return isinstance(step, HoldStep | RampStep | SetpointStep)


def render_method_graph(plot: pg.PlotWidget, steps: Iterable[Step]) -> None:
    """Clear and redraw ``plot`` to reflect ``steps``.

    Pure: doesn't store state on the plot beyond the items themselves.
    Callers responsible for hooking this up to model-change signals.

    Currently held setpoint per target carries forward across steps
    that don't touch that target — that's how an operator reads a
    typical method (heater stays at last setpoint until the next ramp
    or hold). Tracked per-target via a running dict.
    """
    plot.clear()
    plot.setLabel("bottom", "Elapsed time", units="s")
    plot.setLabel("left", "Setpoint")
    plot.addLegend(offset=(8, 8))

    targets: list[str] = []
    target_color: dict[str, Any] = {}
    series_x: dict[str, list[float]] = {}
    series_y: dict[str, list[float]] = {}
    last_value: dict[str, float] = {}

    elapsed = 0.0
    for step in steps:
        target = getattr(step, "target", None)
        target_name = target.name if target is not None else None

        if target_name is not None and target_name not in target_color:
            targets.append(target_name)
            target_color[target_name] = pg.intColor(len(targets) - 1, hues=8)
            series_x[target_name] = []
            series_y[target_name] = []

        duration = _step_duration(step)

        if isinstance(step, HoldStep) and target_name is not None:
            # Horizontal segment at step.value across [elapsed, elapsed + duration].
            series_x[target_name].extend([elapsed, elapsed + duration])
            series_y[target_name].extend([float(step.value), float(step.value)])
            last_value[target_name] = float(step.value)
        elif isinstance(step, RampStep) and target_name is not None:
            start = (
                float(step.start_value)
                if step.start_value is not None
                else last_value.get(target_name, 0.0)
            )
            end = float(step.end_value)
            series_x[target_name].extend([elapsed, elapsed + duration])
            series_y[target_name].extend([start, end])
            last_value[target_name] = end
        elif isinstance(step, SetpointStep) and target_name is not None:
            v = float(step.value)
            series_x[target_name].extend([elapsed, elapsed])
            series_y[target_name].extend([last_value.get(target_name, v), v])
            last_value[target_name] = v
        else:
            # Non-setpoint step — draw a faint vertical guide line at the
            # step's start. PyQtGraph's InfiniteLine handles the full
            # y-range automatically; saves us the explicit ymin/ymax math.
            line = pg.InfiniteLine(
                pos=elapsed,
                angle=90,
                pen=pg.mkPen(color=(160, 160, 160), width=1, style=Qt.PenStyle.DashLine),
                label=step.kind,
                labelOpts={"position": 0.95, "color": (120, 120, 120)},
            )
            plot.addItem(line)

        elapsed += duration

    for target_name in targets:
        plot.plot(
            series_x[target_name],
            series_y[target_name],
            pen=pg.mkPen(target_color[target_name], width=2),
            name=target_name,
        )


__all__ = ["render_method_graph"]
