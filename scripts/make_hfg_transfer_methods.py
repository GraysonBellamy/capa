"""Write the heat-flux-gauge transfer-calibration methods.

Run with: ``uv run python scripts/make_hfg_transfer_methods.py``

Writes ``configs/methods/hfg_transfer_pilot.method.toml`` and
``configs/methods/hfg_transfer_cal.method.toml`` from the setpoint table
below. ``--from-level N`` writes only
``configs/methods/hfg_transfer_cal_fromNN.method.toml``, the calibration
from level N on, to resume an interrupted run. The procedure behind them
is ``docs/calibration/heat-flux-gauge-transfer.md``.

Every window step carries a ``notes`` tag such as
``hfgcal level=03 q=15 dir=up win=R1``. The tags land in the bundle's
``method.step.entered`` / ``method.step.exited`` events, which is how the
analysis finds the windows.

The setpoints are starting estimates: a fit of
``F = k (T^4 - Ta^4) + h (T - Ta)`` to the in-chamber 40 mm tunes in
``configs/calibrations/flux/``. After the open-air pilot, replace them
with the values the pilot gives and rerun this script.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

# (target kW/m^2, heater setpoint degC). The levels only need to land near
# their targets: the reference measures the flux actually delivered.
SETPOINTS_C: dict[int, float] = {
    5: 268.0,
    10: 378.0,
    15: 454.0,
    20: 512.0,
    30: 602.0,
    40: 671.0,
    50: 728.0,
    60: 776.0,
    70: 819.0,
}
UP = (5, 10, 15, 20, 30, 40, 50, 60, 70)
DOWN = (50, 30, 10)  # repeats on the way down: hysteresis and drift check

WINDOW_S = 70.0  # analysis keeps the last 60 s; the first 10 s follow a move
# After the PV first comes within 1 degC of the setpoint. The in-chamber
# data put the bias below 0.02 % at 5 min; 10 min is margin on top.
SETTLE_S = 600.0
FIRST_SETTLE_S = 900.0  # first level: heater body still cold
ZERO_S = 120.0
REACH_BAND_C = 1.0
REACH_TIMEOUT_UP_S = 1200.0
REACH_TIMEOUT_DOWN_S = 2700.0  # cooling is passive and slow at low setpoints

METHODS_DIR = Path(__file__).resolve().parents[1] / "configs" / "methods"


def _s(value: str) -> str:
    return json.dumps(value)


def _tag(**fields: object) -> str:
    return "hfgcal " + " ".join(f"{k}={v}" for k, v in fields.items())


def setpoint(value: float, notes: str, target: str = "heater.setpoint") -> str:
    return (
        f'[[steps]]\nkind = "setpoint"\nvalue = {value}\nnotes = {_s(notes)}\n\n'
        f'[steps.target]\nname = "{target}"\n'
    )


def reach(value: float, *, up: bool, notes: str) -> str:
    op, threshold = (">=", value - REACH_BAND_C) if up else ("<=", value + REACH_BAND_C)
    timeout = REACH_TIMEOUT_UP_S if up else REACH_TIMEOUT_DOWN_S
    return (
        f'[[steps]]\nkind = "wait"\ntimeout_s = {timeout}\non_timeout = "warn"\n'
        f"notes = {_s(notes)}\n\n"
        f'[steps.end_condition]\nchannel = "heater.pv"\nop = "{op}"\nvalue = {threshold}\n'
    )


def acquire(duration_s: float, notes: str) -> str:
    return f'[[steps]]\nkind = "acquire"\nduration_s = {duration_s}\nnotes = {_s(notes)}\n'


def prompt(title: str, message: str, notes: str) -> str:
    # No timeout_s: in the GUI an unanswered prompt waits for the operator
    # (Stop ends it) instead of crashing the run with the heater hot.
    return (
        f'[[steps]]\nkind = "prompt"\ntitle = {_s(title)}\nmessage = {_s(message)}\n'
        f"notes = {_s(notes)}\n"
    )


def shutdown() -> str:
    return (
        '[[steps]]\nkind = "safe_shutdown"\nduration_s = 0.0\n'
        f"notes = {_s(_tag(win='shutdown'))}\n\n"
        '[steps.cool_target]\n"heater.setpoint" = 20.0\n'
    )


# The heater can't slide clear of the gauges, so the only zero is the opening
# one, taken cold with the heater over stop A. A heater 8 degC above the room
# still puts ~0.04 kW/m2 on the reference (0.8 % of the 5 kW/m2 level).
# The body temperature, not the time on water, says when the zero is good:
# on 2026-10-17 the supply drifted 1.3 degC over the session and the zero
# moved with it.
CHECKLIST = (
    "Cooling water flowing through both gauges; HFG2 body temperature flat on "
    "the plot (drifting less than 0.02 degC/min) and above the dew point; "
    "both gauges seated with faces level; heater cold (PV within 2 degC of the "
    "room) and at stop A, centred on the REFERENCE. Continue to record the zero; "
    "the heater heats to level 1 after it."
)
TO_REF = "Slide the heater to stop A (centred on the REFERENCE gauge), then Continue."
TO_HFG2 = "Slide the heater to stop B (centred on HFG2), then Continue."


def zero(name: str) -> str:
    return acquire(ZERO_S, _tag(level="00", win=name))


def resume_checklist(level: int) -> str:
    return (
        f"Resuming at level {level}. Cooling water flowing through both gauges; "
        "HFG2 body temperature flat on the plot; gauges, stops "
        "and faces untouched since the interrupted run; heater at stop A, centred "
        "on the REFERENCE. No zero is recorded: the analysis uses the interrupted "
        "run's cold zero."
    )


def cal_method(start_level: int = 1) -> str:
    """The calibration method, or with ``start_level > 1`` the rest of it.

    A resumed method keeps the full run's level numbers and tags, so its
    windows sit beside the interrupted run's in the analysis. It records no
    zero (the heater is hot); the interrupted run's cold Z1 serves both.
    """
    levels = [(q, True) for q in UP] + [(q, False) for q in DOWN]
    n = len(levels)
    if not 1 <= start_level <= n:
        raise SystemExit(f"--from-level must be 1-{n}, got {start_level}")
    if start_level == 1:
        header = (
            'name = "hfg_transfer_cal"\n'
            'description = "HFG2 transfer calibration against the reference: 5-70 kW/m2 up, '
            '50/30/10 down; R D R with 70 s windows per level."\n'
        )
        parts = [header, prompt("Before heating", CHECKLIST, _tag(win="checklist")), zero("Z1")]
    else:
        header = (
            f'name = "hfg_transfer_cal_from{start_level:02d}"\n'
            f'description = "HFG2 transfer calibration resumed at level {start_level} of {n}; '
            'same levels, tags and timing as hfg_transfer_cal."\n'
        )
        parts = [
            header,
            prompt("Resume", resume_checklist(start_level), _tag(win="checklist")),
        ]
    for i, (q, up) in enumerate(levels, start=1):
        if i < start_level:
            continue
        sp = SETPOINTS_C[q]
        tag = {"level": f"{i:02d}", "q": q, "dir": "up" if up else "down"}
        title = f"Level {i} of {n}: ~{q} kW/m2 ({tag['dir']})"
        parts += [
            setpoint(sp, _tag(**tag, win="command")),
            reach(sp, up=up, notes=_tag(**tag, win="reach")),
            acquire(FIRST_SETTLE_S if i == 1 else SETTLE_S, _tag(**tag, win="settle")),
            acquire(WINDOW_S, _tag(**tag, win="R1")),
            prompt(title, TO_HFG2, _tag(**tag, win="move")),
            acquire(WINDOW_S, _tag(**tag, win="D")),
            prompt(title, TO_REF, _tag(**tag, win="move")),
            acquire(WINDOW_S, _tag(**tag, win="R2")),
        ]
    parts.append(shutdown())
    return "\n".join(parts)


def pilot_method() -> str:
    lo, hi = UP[0], UP[-1]
    lo_sp, hi_sp = SETPOINTS_C[lo], SETPOINTS_C[hi]
    parts = [
        'name = "hfg_transfer_pilot"\n'
        'description = "Open-air pilot for the HFG2 transfer calibration: stationary '
        'records at the lowest and highest levels, then one R D R."\n',
        prompt("Before heating", CHECKLIST, _tag(win="checklist")),
        zero("Z1"),
        setpoint(lo_sp, _tag(level="P1", q=lo, dir="up", win="command")),
        reach(lo_sp, up=True, notes=_tag(level="P1", q=lo, dir="up", win="reach")),
        acquire(1800.0, _tag(level="P1", q=lo, dir="up", win="stationary")),
        setpoint(hi_sp, _tag(level="P2", q=hi, dir="up", win="command")),
        reach(hi_sp, up=True, notes=_tag(level="P2", q=hi, dir="up", win="reach")),
        acquire(1200.0, _tag(level="P2", q=hi, dir="up", win="stationary")),
        prompt(f"~{hi} kW/m2: one swap", TO_HFG2, _tag(level="P2", q=hi, win="move")),
        acquire(120.0, _tag(level="P2", q=hi, win="D")),
        prompt(f"~{hi} kW/m2: one swap", TO_REF, _tag(level="P2", q=hi, win="move")),
        acquire(120.0, _tag(level="P2", q=hi, win="R2")),
        shutdown(),
    ]
    return "\n".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--from-level",
        type=int,
        default=None,
        help="write only hfg_transfer_cal_fromNN.method.toml, resuming an interrupted run",
    )
    args = parser.parse_args()
    if args.from_level is None:
        outputs = (
            ("hfg_transfer_pilot.method.toml", pilot_method()),
            ("hfg_transfer_cal.method.toml", cal_method()),
        )
    else:
        name = f"hfg_transfer_cal_from{args.from_level:02d}.method.toml"
        outputs = ((name, cal_method(args.from_level)),)
    for name, text in outputs:
        path = METHODS_DIR / name
        path.write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
