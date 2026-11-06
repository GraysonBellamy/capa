"""Analyse a heat-flux-gauge transfer calibration (R D R per level).

Run with::

    uv run python scripts/analyze_hfg_transfer.py RUN_DIR [RUN_DIR ...]
    uv run python scripts/analyze_hfg_transfer.py --dut-channel radiometer.voltage         --dut-label RAD RUN_DIR [RUN_DIR ...]

The run directories are bundles recorded with the
``configs/methods/hfg_transfer_cal*.method.toml`` (HFG2) or
``rad_transfer_cal*.method.toml`` (purged radiometer) methods, in any
order; a calibration resumed after an interruption spans several. Windows
are found from the ``hfgcal level=NN win=R1|D|D1..D4|R2|Z1 [purge=SCCM]``
tags on the method steps, so no timing log is needed. The procedure is
``docs/calibration/heat-flux-gauge-transfer.md``:

1. Each window keeps its last ``--window-s`` seconds (the start follows a
   heater move).
2. Each gauge's zero is its mean over the cold ``Z1`` window (first
   ``--discard-s`` dropped), subtracted from every window; with a purge, the
   ``Z1`` at the window's purge rate. A resumed run has no Z1 and uses the
   zero of the run that has one. ``--zero-run`` takes the zeros from a
   separate cold bundle instead; one stopped before its ``Z1`` falls back
   to its ``settleZ1`` window, up to the last sample.
3. The reference flux at each D window is the zero-corrected reference
   voltage, interpolated linearly in time between R1 and R2, times the
   reference slope.
4. Sensitivity S = V_HFG2 / q_ref per level (the "bracketed" estimate).
5. The heater's controller oscillation is large at low setpoints (±4–6 %
   within a window at 4–15 kW/m2 on 2026-10-17), which the bracketed
   estimate can't average out. Both gauges record all the time, so the
   off-centre gauge sees the same oscillation at the same instant: the
   per-sample ratio V_HFG2/V_ref with the reference centred (rho_A) and with
   HFG2 centred (rho_B) gives S_ratio = S_ref * sqrt(rho_A * rho_B), free of
   the oscillation. It assumes the field is equally strong at the two
   off-centre positions, so it carries a constant bias F; F is the mean
   S_ratio / S_bracketed over the levels at or above ``--anchor-min-kw``,
   where the bracketed estimate is reliable. The per-level calibration is
   S = S_ratio / F.
   With a purge, the calibration is at the R1/R2 purge rate (``--cal-purge``
   to override): its D windows are averaged, and each other purge rate's D
   window gives the purge effect as its ratio rho_B over the calibration
   rate's rho_B interpolated to the same time. Same heater position, so the
   oscillation and the field both divide out.
6. The fits weight each level by 1/q^2 (the procedure error is relative):
   through the origin (the calibration), with an intercept, and with a
   quadratic term (both checks).

Prints the per-level table, the fits, residuals, the up/down repeats, the
settling after each move, and a ``linear_two_point`` block for the gauge's
channel.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

REF_CHANNEL = "heat_flux_gauge"
PV_CHANNEL = "heater.pv"
AIR_CHANNEL = "ambient.temperature"
FLOW_CHANNEL = "purge.flow"
REF_SLOPE_KW_M2_PER_V = 12867.29134
"""CAPA reference 232041, transferred from 223481 (NIST colleague, 2022-12-19)."""
KW_M2_PER_BTU_FT2_S = 11.356526682
RESIDUAL_LIMIT_PCT = 0.6


@dataclass
class Run:
    path: Path
    t_s: dict[str, np.ndarray]
    value: dict[str, np.ndarray]
    raw: dict[str, np.ndarray]
    windows: dict[tuple[str, str, str], tuple[float, float]]
    """(level, win, purge) -> (entered, exited) in run seconds; purge is
    ``""`` when the tag has none."""


def load_run(path: Path, names: tuple[str, ...], *, close_open: bool = False) -> Run:
    """Load ``names`` (absent channels come back empty) and the tagged windows.

    ``close_open`` ends a window still open when the run stopped at the last
    sample; only for zero bundles, where a cut-short window is still usable.
    """
    table = pq.read_table(path / "scalars.parquet")
    t_ns = table.column("t_mono_ns").to_numpy()
    t_s_col = table.column("t_mono_s").to_numpy()
    channel = table.column("channel").to_numpy(zero_copy_only=False)
    value = table.column("value").to_numpy(zero_copy_only=False)
    raw = table.column("raw_value").to_numpy(zero_copy_only=False)
    # Run-clock anchor: events carry absolute t_mono_ns, scalars both.
    anchor_ns = int(t_ns.min() - round(t_s_col[np.argmin(t_ns)] * 1e9))
    t_s: dict[str, np.ndarray] = {}
    vals: dict[str, np.ndarray] = {}
    raws: dict[str, np.ndarray] = {}
    for name in names:
        m = channel == name
        order = np.argsort(t_ns[m])
        t_s[name] = (t_ns[m][order] - anchor_ns) / 1e9
        vals[name] = np.asarray(value[m][order], dtype=float)
        raws[name] = np.asarray(
            [np.nan if r is None else float(r) for r in raw[m][order]], dtype=float
        )

    con = sqlite3.connect(f"file:{path / 'events.sqlite'}?mode=ro", uri=True)
    rows = con.execute(
        "select t_mono_ns, kind, metadata_json from events "
        "where kind in ('method.step.entered', 'method.step.exited') order by id"
    ).fetchall()
    con.close()
    opened: dict[tuple[str, str, str], float] = {}
    windows: dict[tuple[str, str, str], tuple[float, float]] = {}
    for t, kind, md_json in rows:
        md = json.loads(md_json or "{}")
        notes = md.get("notes") or ""
        if md.get("step_kind") != "acquire" or not notes.startswith("hfgcal"):
            continue
        tags = dict(part.split("=", 1) for part in notes.split()[1:])
        key = (tags.get("level", ""), tags.get("win", ""), tags.get("purge", ""))
        t_run = (t - anchor_ns) / 1e9
        if kind == "method.step.entered":
            opened[key] = t_run
        elif key in opened:
            windows[key] = (opened.pop(key), t_run)
    if close_open and opened:
        t_end = max((float(t[-1]) for t in t_s.values() if t.size), default=0.0)
        windows.update({key: (t0, t_end) for key, t0 in opened.items()})
    return Run(path=path, t_s=t_s, value=vals, raw=raws, windows=windows)


def window_mean(
    run: Run, name: str, a: float, b: float, *, raw: bool = False
) -> tuple[float, float, int]:
    """Mean, standard error and count of a channel over [a, b] run seconds."""
    t = run.t_s[name]
    v = (run.raw if raw else run.value)[name]
    m = (t >= a) & (t <= b) & np.isfinite(v)
    n = int(m.sum())
    if n == 0:
        return math.nan, math.nan, 0
    sel = v[m]
    return float(sel.mean()), float(sel.std(ddof=1) / math.sqrt(n)) if n > 1 else math.nan, n


def paired_ratio(run: Run, dut: str, a: float, b: float, z_ref: float, z_dut: float) -> float:
    """Mean of the per-sample ratio V_dut / V_ref over [a, b], zero-corrected.

    Both channels come from the same DAQ read, so samples pair by time.
    """
    t_r = run.t_s[REF_CHANNEL]
    v_r = run.raw[REF_CHANNEL] - z_ref
    t_d = run.t_s[dut]
    v_d = run.value[dut] - z_dut
    m = (t_r >= a) & (t_r <= b)
    tr, vr = t_r[m], v_r[m]
    idx = np.clip(np.searchsorted(t_d, tr), 1, len(t_d) - 1)
    idx = np.where(np.abs(t_d[idx - 1] - tr) < np.abs(t_d[idx] - tr), idx - 1, idx)
    ok = np.abs(t_d[idx] - tr) < 0.05
    return float(np.mean(v_d[idx][ok] / vr[ok]))


def wls(
    design: np.ndarray, y: np.ndarray, w: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Weighted least squares. Returns coefficients, their standard errors
    (scaled by the residual variance) and the residuals."""
    sw = np.sqrt(w)
    a = design * sw[:, None]
    coef, *_ = np.linalg.lstsq(a, y * sw, rcond=None)
    resid = y - design @ coef
    dof = max(len(y) - design.shape[1], 1)
    s2 = float(np.sum(w * resid**2) / dof)
    cov = s2 * np.linalg.inv(a.T @ a)
    return coef, np.sqrt(np.diag(cov)), resid


def is_dut_window(win: str) -> bool:
    """``D`` (one window per level) or ``D1``, ``D2``, ... (purge holds)."""
    return win == "D" or (win[:1] == "D" and win[1:].isdigit())


def purge_label(purge: str) -> str:
    return f" at {purge} sccm" if purge else ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("runs", nargs="+", type=Path)
    parser.add_argument("--window-s", type=float, default=60.0)
    parser.add_argument("--discard-s", type=float, default=10.0)
    parser.add_argument("--ref-slope", type=float, default=REF_SLOPE_KW_M2_PER_V)
    parser.add_argument("--dut-channel", default="hfg2.voltage")
    parser.add_argument(
        "--body-channel",
        default="hfg2.body_temperature",
        help="gauge body temperature channel; absent from the bundle is fine",
    )
    parser.add_argument("--dut-label", default="HFG2")
    parser.add_argument(
        "--cal-purge",
        default=None,
        help="purge rate (sccm, as tagged) the calibration is at; default: R1's",
    )
    parser.add_argument(
        "--zero-run",
        type=Path,
        default=None,
        help="cold bundle to take the zeros from instead of the runs' own Z1",
    )
    parser.add_argument(
        "--exclude", type=str, default="", help="comma-separated levels, e.g. 01,07"
    )
    parser.add_argument(
        "--anchor-min-kw",
        type=float,
        default=19.0,
        help="levels at or above this flux set the ratio estimator's bias F",
    )
    args = parser.parse_args()
    dut, body_ch, label = args.dut_channel, args.body_channel, args.dut_label
    names = (REF_CHANNEL, dut, body_ch, PV_CHANNEL, AIR_CHANNEL, FLOW_CHANNEL)

    runs = [load_run(p, names) for p in args.runs]
    zero_win = "Z1"
    if args.zero_run is not None:
        zr = load_run(args.zero_run, names, close_open=True)
        if not any(k[:2] == ("00", "Z1") for k in zr.windows):
            zero_win = "settleZ1"
    else:
        zero_runs = [r for r in runs if any(k[:2] == ("00", "Z1") for k in r.windows)]
        if not zero_runs:
            raise SystemExit("no run with a Z1 zero window")
        zr = zero_runs[0]
    zeros: dict[str, tuple[float, float]] = {}
    for (lvl, win, purge), (za, zb) in sorted(zr.windows.items(), key=lambda kv: kv[1][0]):
        if (lvl, win) != ("00", zero_win):
            continue
        z_ref, z_ref_se, _ = window_mean(zr, REF_CHANNEL, za + args.discard_s, zb, raw=True)
        z_dut, z_dut_se, _ = window_mean(zr, dut, za + args.discard_s, zb)
        z_body, _, _ = window_mean(zr, body_ch, za + args.discard_s, zb)
        zeros[purge] = (z_ref, z_dut)
        body = f", body {z_body:.2f} C" if math.isfinite(z_body) else ""
        print(
            f"Zero ({zr.path.name} {win}{purge_label(purge)}): reference {z_ref * 1e6:+.3f} uV "
            f"(+-{z_ref_se * 1e6:.3f}), {label} {z_dut * 1e6:+.3f} uV "
            f"(+-{z_dut_se * 1e6:.3f}){body}"
        )
    r1_purges = {k[2] for r in runs for k in r.windows if k[1] == "R1"}
    if args.cal_purge is not None:
        cal = args.cal_purge
    elif len(r1_purges) == 1:
        cal = next(iter(r1_purges))
    else:
        raise SystemExit(f"R1 windows at several purge rates {sorted(r1_purges)}; pass --cal-purge")
    if cal not in zeros:
        raise SystemExit(f"no Z1 zero{purge_label(cal)}")

    def kept(span: tuple[float, float]) -> tuple[float, float]:
        return span[1] - args.window_s, span[1]

    excluded = {s.strip() for s in args.exclude.split(",") if s.strip()}
    levels = []
    for run in runs:
        for lvl in sorted({k[0] for k in run.windows if is_dut_window(k[1])}):
            r1 = run.windows.get((lvl, "R1", cal))
            r2 = run.windows.get((lvl, "R2", cal))
            d_keys = sorted(
                (k for k in run.windows if k[0] == lvl and is_dut_window(k[1])),
                key=lambda k: run.windows[k][0],
            )
            if not (r1 and r2 and any(k[2] == cal for k in d_keys)):
                continue
            z_ref, z_dut = zeros[cal]
            (lo1, b1), (lo2, b2) = kept(r1), kept(r2)
            v1 = window_mean(run, REF_CHANNEL, lo1, b1, raw=True)[0] - z_ref
            v2 = window_mean(run, REF_CHANNEL, lo2, b2, raw=True)[0] - z_ref
            t1, t2 = (lo1 + b1) / 2, (lo2 + b2) / 2
            dwin = []
            for key in d_keys:
                lo, b = kept(run.windows[key])
                zr_p, zd_p = zeros.get(key[2], zeros[cal])
                td = (lo + b) / 2
                v_ref_d = v1 + (v2 - v1) * (td - t1) / (t2 - t1)
                q_d = args.ref_slope * v_ref_d
                v_d = window_mean(run, dut, lo, b)[0] - zd_p
                first = paired_ratio(run, dut, lo, td, zr_p, zd_p)
                second = paired_ratio(run, dut, td, b, zr_p, zd_p)
                dwin.append(
                    {
                        "purge": key[2],
                        "span": (lo, b),
                        "t": td,
                        "q": q_d,
                        "v_dut": v_d,
                        "s": v_d / q_d * 1e6,
                        "rho": paired_ratio(run, dut, lo, b, zr_p, zd_p),
                        "trend_pct": (second / first - 1) * 100,
                        "flow": window_mean(run, FLOW_CHANNEL, lo, b)[0],
                    }
                )
            calw = [w for w in dwin if w["purge"] == cal]
            rho_b = float(np.mean([w["rho"] for w in calw]))
            rho_a1 = paired_ratio(run, dut, lo1, b1, z_ref, z_dut)
            rho_a2 = paired_ratio(run, dut, lo2, b2, z_ref, z_dut)
            rho_a = 0.5 * (rho_a1 + rho_a2)
            for w in dwin:
                if w["purge"] != cal:
                    ref_rho = np.interp(w["t"], [c["t"] for c in calw], [c["rho"] for c in calw])
                    w["rel_pct"] = (w["rho"] / float(ref_rho) - 1) * 100
            lo, b = calw[0]["span"]
            levels.append(
                {
                    "level": lvl,
                    "run": run.path.name,
                    "q": float(np.mean([w["q"] for w in calw])),
                    "v_dut": float(np.mean([w["v_dut"] for w in calw])),
                    "s": float(np.mean([w["s"] for w in calw])),
                    "s_ratio": math.sqrt(rho_a * rho_b) / args.ref_slope * 1e6,
                    "drift_pct": (v2 / v1 - 1) * 100,
                    "body": window_mean(run, body_ch, lo, b)[0],
                    "pv": window_mean(run, PV_CHANNEL, lo, b)[0],
                    "air": window_mean(run, AIR_CHANNEL, lo, b)[0],
                    "rho_a_change_pct": (rho_a2 / rho_a1 - 1) * 100,
                    "dwin": dwin,
                }
            )
    levels.sort(key=lambda x: x["level"])
    used = [x for x in levels if x["level"] not in excluded]
    anchors = [x for x in used if x["q"] >= args.anchor_min_kw]
    bias = np.array([x["s_ratio"] / x["s"] for x in anchors])
    f_bias = float(bias.mean())
    f_se = float(bias.std(ddof=1) / math.sqrt(len(bias)))
    for x in levels:
        x["s_final"] = x["s_ratio"] / f_bias

    print(
        f"\nlevel  q_ref     {'V_' + label:<10}S bracketed  S ratio   S final    ref R1->R2  "
        "body   air    heater"
    )
    print(
        "       kW/m2     mV        uV/(kW/m2)   (biased)  uV/(kW/m2) %           C      C      C"
    )
    for x in levels:
        flag = "  (excluded)" if x["level"] in excluded else ""
        print(
            f"  {x['level']}  {x['q']:7.3f}  {x['v_dut'] * 1e3:8.4f}  {x['s']:9.3f}    "
            f"{x['s_ratio']:7.3f}   {x['s_final']:8.3f}   {x['drift_pct']:+6.2f}    "
            f"{x['body']:5.2f}  {x['air']:5.2f}  {x['pv']:6.1f}{flag}"
        )
    print(
        f"\nRatio-estimator bias F = {f_bias:.5f} +- {f_se:.5f} "
        f"(from {len(anchors)} levels >= {args.anchor_min_kw:g} kW/m2; "
        f"field {(f_bias**2 - 1) * 100:+.2f} % stronger on one side of centre)"
    )

    q = np.array([x["q"] for x in used])
    # The calibration uses the oscillation-free per-level values.
    v = np.array([x["s_final"] * 1e-6 * x["q"] for x in used])
    w = 1.0 / q**2
    (s0,), (s0_se,), r0 = wls(q[:, None], v, w)
    (a1, s1), (a1_se, s1_se), _ = wls(np.column_stack([np.ones_like(q), q]), v, w)
    (s2, c2), (s2_se, c2_se), _ = wls(np.column_stack([q, q**2]), v, w)
    n = len(used)
    s_brk = np.array([x["s"] for x in used])
    print(
        f"\nBracketed estimate alone (for comparison): mean {s_brk.mean():.3f}, "
        f"rms scatter {s_brk.std(ddof=1) / s_brk.mean() * 100:.3f} %"
    )
    print(f"\nFits of S final over {n} levels (weights 1/q^2):")
    print(
        f"  through origin: S = {s0 * 1e6:.3f} +- {s0_se * 1e6:.3f} uV/(kW/m2)  "
        f"(+-{s0_se / s0 * 100:.3f} %; F adds +-{f_se / f_bias * 100:.3f} %)"
    )
    print(
        f"  with intercept: S = {s1 * 1e6:.3f} +- {s1_se * 1e6:.3f}, intercept "
        f"{a1 * 1e6:+.3f} +- {a1_se * 1e6:.3f} uV  (t = {a1 / a1_se:+.2f})"
    )
    print(
        f"  quadratic:      S = {s2 * 1e6:.3f} +- {s2_se * 1e6:.3f}, curvature "
        f"{c2 / s2 * 100 * 50:+.3f} % at 50 kW/m2  (t = {c2 / c2_se:+.2f})"
    )
    print("\nResiduals from the through-origin fit:")
    worst = 0.0
    for x, r in zip(used, r0, strict=True):
        pct = r / (s0 * x["q"]) * 100
        worst = max(worst, abs(pct))
        mark = "  <-- over limit, rerun" if abs(pct) > RESIDUAL_LIMIT_PCT else ""
        print(f"  level {x['level']}  {x['q']:7.3f} kW/m2  {pct:+.3f} %{mark}")
    rms = float(np.sqrt(np.mean((r0 / (s0 * q)) ** 2)) * 100)
    print(f"  rms {rms:.3f} %, worst {worst:.3f} % (limit {RESIDUAL_LIMIT_PCT} %)")

    by_target: dict[int, list[dict]] = {}
    for x in levels:
        by_target.setdefault(round(x["q"] / 5) * 5, []).append(x)
    pairs = [(k, g) for k, g in sorted(by_target.items()) if len(g) > 1]
    if pairs:
        print("\nUp/down repeats (same setpoint):")
        for k, group in pairs:
            g = sorted(group, key=lambda x: x["level"])
            diff = (g[-1]["s_final"] / g[0]["s_final"] - 1) * 100
            print(
                f"  ~{k} kW/m2: level {g[0]['level']} S {g[0]['s_final']:.3f}, level "
                f"{g[-1]['level']} S {g[-1]['s_final']:.3f}  ->  {diff:+.3f} %"
            )

    rates = sorted({d["purge"] for x in levels for d in x["dwin"] if d["purge"] != cal}, key=float)
    if rates:
        print_purge_effect(levels, used, cal, rates, s0)

    c_kw_m2_per_mv = 1 / (s0 * 1e3)
    print(f"\nCalibration of {label}{purge_label(cal)} (through origin):")
    print(
        f"  {s0 * 1e6:.3f} uV per kW/m2 = {c_kw_m2_per_mv:.4f} kW/m2 per mV = "
        f"{s0 * 1e3 * KW_M2_PER_BTU_FT2_S:.4f} mV per BTU/(ft2 s) = "
        f"{s0 * 1e3 * 10:.4f} mV per W/cm2"
    )
    print(
        f"\n[channels.calibration]  # {label.lower()}, raw volts -> kW/m^2; zero the gauge in place"
    )
    print('kind = "linear_two_point"')
    print('input_unit = "V"\noutput_unit = "kW/m^2"')
    print("ref_low_raw = 0.0\nref_low_value = 0.0")
    print(f"ref_high_raw = 0.01\nref_high_value = {0.01 / s0:.6g}")
    return 0


def print_purge_effect(
    levels: list[dict], used: list[dict], cal: str, rates: list[str], s0: float
) -> None:
    """Each sweep rate's sensitivity relative to the calibration rate, per
    level, with the checks a windowed gauge needs: drift inside each kept
    window (an unfinished window transient), the off-centre ratio after the
    slide back (R2 against R1) and the measured flow against the tag."""
    print(
        f"\nPurge effect, relative to {cal} sccm (D-window ratio to the off-centre "
        "reference, same heater position):"
    )
    rate_cols = "".join(f"{r + ' sccm':>11}" for r in rates)
    print(f"level  q_ref    {rate_cols}   worst drift  R2/R1 rho_A  worst flow error")
    pct_cols = "".join(f"{'%':>11}" for _ in rates)
    print(f"       kW/m2    {pct_cols}   in window %  %            sccm")
    for x in levels:
        rel = {d["purge"]: d["rel_pct"] for d in x["dwin"] if "rel_pct" in d}
        trend = max(abs(d["trend_pct"]) for d in x["dwin"])
        flow_err = [
            abs(d["flow"] - float(d["purge"])) for d in x["dwin"] if math.isfinite(d["flow"])
        ]
        flow_s = f"{max(flow_err):6.2f}" if flow_err else "     -"
        cols = "".join(f"{rel.get(r, math.nan):+11.3f}" for r in rates)
        print(
            f"  {x['level']}  {x['q']:7.3f}{cols}   {trend:9.3f}    "
            f"{x['rho_a_change_pct']:+8.3f}     {flow_s}"
        )
    print("\nPer rate over the used levels (mean, sd, slope with flux):")
    for r in rates:
        pts = [(x["q"], d["rel_pct"]) for x in used for d in x["dwin"] if d["purge"] == r]
        if len(pts) < 3:
            continue
        qs, rel = np.array(pts).T
        slope, intercept = np.polyfit(qs, rel, 1)
        resid = rel - (slope * qs + intercept)
        slope_se = math.sqrt(
            float(np.sum(resid**2)) / (len(qs) - 2) / float(np.sum((qs - qs.mean()) ** 2))
        )
        print(
            f"  {r:>4} sccm: {rel.mean():+.3f} % (sd {rel.std(ddof=1):.3f} %, n {len(rel)}); "
            f"{slope * 10:+.3f} % per 10 kW/m2 (t = {slope / slope_se:+.2f}); "
            f"S = {s0 * 1e6 * (1 + rel.mean() / 100):.3f} uV/(kW/m2)"
        )


if __name__ == "__main__":
    raise SystemExit(main())
