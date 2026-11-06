"""Analyse the radiometer fan test (``configs/experiments/rad_fan_test.yaml``).

Run with::

    uv run python scripts/analyze_fan_test.py FAN_RUN --zero-run CAL_RUN

The heater sits over the reference throughout, so V_rad / V_ref is the
off-centre radiometer over the centred reference, read at the same instant:
the heater's own swings divide out. The reference sees radiation plus
convection, the radiometer radiation only. Per setpoint the script prints:

* the fan effect on the ratio: each fan-on window (last ``--window-s``)
  against the mean of the fan-off windows either side;
* the step at each switch: ratio over ``--step-s`` before the prompt against
  ``--step-s`` after it (less ``--skip-s``), sign-normalised to on minus off.
  Convection changes within seconds; the heater shell cooling under the fan
  takes minutes, which shows as the drift inside each window instead;
* each gauge's own change, which includes the heater's response to the fan.

The fan run has no zero (the heater is hot); the cold Z1 at the fan run's
purge rate comes from ``--zero-run``, the calibration run.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
from analyze_hfg_transfer import (
    REF_CHANNEL,
    REF_SLOPE_KW_M2_PER_V,
    load_run,
    paired_ratio,
    window_mean,
)

DUT = "radiometer.voltage"


def fan_windows(path: Path) -> dict[tuple[str, str], tuple[float, float]]:
    """(sp, win) -> (entered, exited) in run seconds, for ``fantest`` steps."""
    con = sqlite3.connect(f"file:{path / 'events.sqlite'}?mode=ro", uri=True)
    rows = con.execute(
        "select t_mono_ns, kind, metadata_json from events "
        "where kind in ('method.step.entered', 'method.step.exited') order by id"
    ).fetchall()
    con.close()
    # Same run-clock anchor as load_run, recovered from the scalars.
    table = pq.read_table(path / "scalars.parquet", columns=["t_mono_ns", "t_mono_s"])
    t_ns = table.column("t_mono_ns").to_numpy()
    t_s_col = table.column("t_mono_s").to_numpy()
    anchor_ns = int(t_ns.min() - round(t_s_col[np.argmin(t_ns)] * 1e9))
    opened: dict[tuple[str, str], float] = {}
    out: dict[tuple[str, str], tuple[float, float]] = {}
    for t_ns_ev, kind, md_json in rows:
        notes = json.loads(md_json or "{}").get("notes") or ""
        if not notes.startswith("fantest"):
            continue
        tags = dict(part.split("=", 1) for part in notes.split()[1:])
        win = tags.get("win", "")
        if win == "switch":
            win = f"switch-{tags.get('fan')}-{len([k for k in out if k[1].startswith('switch')])}"
        key = (tags.get("sp", ""), win)
        t_run = (t_ns_ev - anchor_ns) / 1e9
        if kind == "method.step.entered":
            opened[key] = t_run
        elif key in opened:
            out[key] = (opened.pop(key), t_run)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run", type=Path)
    parser.add_argument("--zero-run", type=Path, required=True)
    parser.add_argument("--purge", default="200", help="the fan run's purge rate, sccm")
    parser.add_argument("--window-s", type=float, default=120.0)
    parser.add_argument("--step-s", type=float, default=20.0)
    parser.add_argument("--skip-s", type=float, default=5.0)
    args = parser.parse_args()

    zr = load_run(args.zero_run, (REF_CHANNEL, DUT))
    za, zb = zr.windows[("00", "Z1", args.purge)]
    z_ref = window_mean(zr, REF_CHANNEL, za + 10, zb, raw=True)[0]
    z_dut = window_mean(zr, DUT, za + 10, zb)[0]
    print(
        f"Zeros ({args.zero_run.name}, {args.purge} sccm): reference {z_ref * 1e6:+.3f} uV, "
        f"radiometer {z_dut * 1e6:+.3f} uV"
    )

    run = load_run(args.run, (REF_CHANNEL, DUT, "heater.pv"))
    wins = fan_windows(args.run)

    def ratio(a: float, b: float) -> float:
        return paired_ratio(run, DUT, a, b, z_ref, z_dut)

    def gauge(ch: str, a: float, b: float) -> float:
        z = z_ref if ch == REF_CHANNEL else z_dut
        return window_mean(run, ch, a, b, raw=ch == REF_CHANNEL)[0] - z

    for sp in sorted({k[0] for k in wins}, key=lambda s: -float(s or 0)):
        f = {k[1]: v for k, v in wins.items() if k[0] == sp}
        states = [f"F{i}" for i in range(5) if f"F{i}" in f]
        if len(states) < 3:
            continue
        print(f"\n=== {sp} C (heater over the reference) ===")
        kept = {w: (f[w][1] - args.window_s, f[w][1]) for w in states}
        r = {w: ratio(*kept[w]) for w in states}
        q = {w: gauge(REF_CHANNEL, *kept[w]) * REF_SLOPE_KW_M2_PER_V for w in states}
        v = {w: gauge(DUT, *kept[w]) for w in states}
        pv = {w: window_mean(run, "heater.pv", *kept[w])[0] for w in states}
        print(
            "window  fan   ratio V_rad/V_ref   q_ref kW/m2   V_rad mV   heater C   drift in window %"
        )
        for i, w in enumerate(states):
            a, b = f[w]
            early = ratio(a + args.skip_s, a + args.skip_s + 30)
            late = ratio(b - 60, b)
            fan = "on" if i % 2 else "off"
            print(
                f"  {w}    {fan:3}   {r[w]:.5f}           {q[w]:7.3f}     {v[w] * 1e3:7.4f}   "
                f"{pv[w]:6.1f}     {(late / early - 1) * 100:+.3f}"
            )
        effects = []
        for i in range(1, len(states) - 1, 2):
            on, off1, off2 = states[i], states[i - 1], states[i + 1]
            base_r = 0.5 * (r[off1] + r[off2])
            base_q = 0.5 * (q[off1] + q[off2])
            base_v = 0.5 * (v[off1] + v[off2])
            effects.append(
                ((r[on] / base_r - 1) * 100, (q[on] / base_q - 1) * 100, (v[on] / base_v - 1) * 100)
            )
            print(
                f"  fan on ({on}) vs off either side: ratio {effects[-1][0]:+.3f} %, "
                f"reference {effects[-1][1]:+.3f} %, radiometer {effects[-1][2]:+.3f} %"
            )
        if effects:
            mean = np.mean(np.array(effects), axis=0)
            print(
                f"  mean fan effect: ratio {mean[0]:+.3f} %, reference {mean[1]:+.3f} %, "
                f"radiometer {mean[2]:+.3f} %"
            )
        steps = []
        for key, (a, b) in sorted(f.items(), key=lambda kv: kv[1][0]):
            if not key.startswith("switch"):
                continue
            to_on = key.split("-")[1] == "on"
            before = ratio(a - args.step_s, a)
            after = ratio(b + args.skip_s, b + args.skip_s + args.step_s)
            step = (after / before - 1) * 100
            steps.append(step if to_on else -step)
            print(
                f"  switch {'ON ' if to_on else 'OFF'} at {a:8.1f} s: ratio step "
                f"{step:+.3f} % (prompt open {b - a:.0f} s)"
            )
        if steps:
            print(f"  mean immediate step, on minus off: {np.mean(steps):+.3f} % (n {len(steps)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
