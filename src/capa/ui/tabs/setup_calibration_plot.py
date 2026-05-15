""":class:`CalibrationPlotDialog` — `value(raw)` curve popup.

The Setup tab's Channels section exposes a **Plot…** button next to
each channel's calibration sub-form; clicking it emits
``plotCalibrationRequested(channel_name)`` which the Setup tab routes
to this dialog. The popup is intentionally minimal:

* one :class:`pyqtgraph.PlotWidget`;
* a header showing the channel name, calibration kind, and input /
  output units;
* a 200-point sample of ``calibration.evaluate(raw)`` over the
  relevant range (chosen per variant — see :func:`_curve_for`);
* an uncertainty band drawn via :class:`pyqtgraph.FillBetweenItem`
  when the calibration carries an :class:`UncertaintySpec`;
* dots at the calibration's reference points (LinearTwoPoint) or
  table rows (Lookup), so the operator can spot a transposed pair at
  a glance.

The dialog is read-only — it never writes back to the draft. Operators
edit calibration values through the inline sub-form; the plot reflects
the current draft state and re-renders when the operator reopens it.

``CustomCallable`` variants cannot be evaluated outside the plugin
runtime; the dialog shows an informational placeholder instead of
crashing on ``evaluate()``.
"""

from __future__ import annotations

from typing import Any

import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from capa.channels.calibration import (
    Calibration,
    CustomCallable,
    Identity,
    LinearTwoPoint,
    Lookup,
    Piecewise,
    Polynomial,
)

_SAMPLE_COUNT = 200
"""Points along the raw axis. 200 is enough that polynomial wiggles
look smooth at typical plot widths without paying for a denser
sample. PyQtGraph's downsampling handles wide ranges automatically."""


def _curve_for(cal: Calibration) -> tuple[list[float], list[float]] | None:
    """Sample ``cal.evaluate(raw)`` over a sensible raw range.

    Returns ``(raws, values)`` or ``None`` for variants we cannot
    evaluate (currently only :class:`CustomCallable` — its plugin
    runtime isn't loaded at Setup-edit time).
    """
    if isinstance(cal, CustomCallable):
        return None
    raw_min, raw_max = _raw_range_for(cal)
    if raw_max <= raw_min:
        # Degenerate range — pad ±1 around the centre point so the
        # plot still shows something rather than a single dot.
        centre = raw_min
        raw_min = centre - 1.0
        raw_max = centre + 1.0
    step = (raw_max - raw_min) / (_SAMPLE_COUNT - 1)
    raws = [raw_min + i * step for i in range(_SAMPLE_COUNT)]
    values = [cal.evaluate(r) for r in raws]
    return raws, values


def _raw_range_for(cal: Calibration) -> tuple[float, float]:
    """Pick a raw-axis range that lets the operator see the curve.

    Heuristics by variant:

    * Identity / Polynomial — sample 0..1000 (no natural range; the
      input unit usually drives reasonable raw scales for thermo /
      MFC / mass).
    * LinearTwoPoint — span the two reference raws, padded 10% each side.
    * Lookup — span the table extents exactly.
    * Piecewise — span the segments end-to-end.
    """
    if isinstance(cal, LinearTwoPoint):
        lo, hi = sorted((cal.ref_low_raw, cal.ref_high_raw))
        pad = max(1e-9, (hi - lo) * 0.1)
        return lo - pad, hi + pad
    if isinstance(cal, Lookup):
        lo = cal.table[0][0]
        hi = cal.table[-1][0]
        return lo, hi
    if isinstance(cal, Piecewise):
        return cal.segments[0].raw_min, cal.segments[-1].raw_max
    if isinstance(cal, (Identity, Polynomial)):
        return 0.0, 1000.0
    return 0.0, 1.0


class CalibrationPlotDialog(QDialog):
    """Non-modal popup showing one channel's calibration curve.

    Constructed via :meth:`show_for_channel`. The dialog is parented to
    the Setup tab so closing the tab tears it down; it doesn't hold a
    reference to the draft (the channel dict is captured once at
    construction).
    """

    def __init__(
        self,
        *,
        channel_name: str,
        calibration: Calibration,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Calibration — {channel_name}")
        self.resize(560, 420)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(8)

        header_lines = [
            f"<b>{channel_name}</b>",
            f"kind: {calibration.kind}",
            f"input: {calibration.input_unit}  →  output: {calibration.output_unit}",
        ]
        header = QLabel("<br>".join(header_lines), self)
        header.setTextFormat(Qt.TextFormat.RichText)
        outer.addWidget(header)

        plot = pg.PlotWidget(self)
        plot.setBackground("w")
        plot.showGrid(x=True, y=True, alpha=0.2)
        plot.setLabel("bottom", f"raw ({calibration.input_unit})")
        plot.setLabel("left", f"value ({calibration.output_unit})")
        outer.addWidget(plot, stretch=1)

        sample = _curve_for(calibration)
        if sample is None:
            plot.addItem(
                pg.TextItem(
                    f"calibration kind {calibration.kind!r} cannot be"
                    " evaluated outside the plugin runtime",
                    anchor=(0.5, 0.5),
                )
            )
        else:
            raws, values = sample
            plot.plot(raws, values, pen=pg.mkPen("k", width=2))
            self._maybe_draw_uncertainty(plot, calibration, raws, values)
            self._maybe_draw_reference_points(plot, calibration)

    # ------------------------------------------------------------------
    # Public constructor (used by SetupTab).
    # ------------------------------------------------------------------

    @classmethod
    def show_for_channel(
        cls,
        *,
        channel: dict[str, Any],
        parent: QWidget | None,
    ) -> CalibrationPlotDialog | None:
        """Build + show a dialog from a channel-payload dict.

        Returns the constructed dialog, or ``None`` when the channel
        does not carry a recognisable calibration. ``None`` lets the
        caller surface a friendlier message instead of opening an empty
        popup.
        """
        cal_dict = channel.get("calibration")
        if not isinstance(cal_dict, dict):
            return None
        try:
            from pydantic import TypeAdapter  # noqa: PLC0415

            calibration: Calibration = TypeAdapter(Calibration).validate_python(cal_dict)
        except Exception:
            return None
        name = channel.get("name") or "(unnamed)"
        dialog = cls(
            channel_name=str(name),
            calibration=calibration,
            parent=parent,
        )
        dialog.show()
        return dialog

    # ------------------------------------------------------------------
    # Variant-specific embellishments.
    # ------------------------------------------------------------------

    def _maybe_draw_uncertainty(
        self,
        plot: pg.PlotWidget,
        cal: Calibration,
        raws: list[float],
        values: list[float],
    ) -> None:
        if cal.uncertainty is None:
            return
        try:
            bands = [cal.uncertainty.absolute_for(v) for v in values]
        except Exception:
            return
        upper = [v + b for v, b in zip(values, bands, strict=False)]
        lower = [v - b for v, b in zip(values, bands, strict=False)]
        upper_curve = pg.PlotDataItem(raws, upper, pen=pg.mkPen("k", width=0))
        lower_curve = pg.PlotDataItem(raws, lower, pen=pg.mkPen("k", width=0))
        band = pg.FillBetweenItem(upper_curve, lower_curve, brush=pg.mkBrush(80, 120, 200, 60))
        plot.addItem(band)

    def _maybe_draw_reference_points(self, plot: pg.PlotWidget, cal: Calibration) -> None:
        """Mark the calibration's anchor points (two-point endpoints,
        lookup table rows). Operators trace transposed reference pairs
        by eye much faster than by reading the form fields."""
        if isinstance(cal, LinearTwoPoint):
            xs = [cal.ref_low_raw, cal.ref_high_raw]
            ys = [cal.ref_low_value, cal.ref_high_value]
            plot.plot(
                xs,
                ys,
                pen=None,
                symbol="o",
                symbolSize=8,
                symbolBrush=pg.mkBrush("r"),
            )
        elif isinstance(cal, Lookup):
            xs = [pair[0] for pair in cal.table]
            ys = [pair[1] for pair in cal.table]
            plot.plot(
                xs,
                ys,
                pen=None,
                symbol="o",
                symbolSize=6,
                symbolBrush=pg.mkBrush("r"),
            )


__all__ = ["CalibrationPlotDialog"]
