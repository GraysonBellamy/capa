"""Write the purged-radiometer transfer-calibration method.

Run with: ``uv run python scripts/make_rad_transfer_methods.py``

Writes ``configs/methods/rad_transfer_cal.method.toml``. ``--from-level N``
writes only ``configs/methods/rad_transfer_cal_fromNN.method.toml``, the
calibration from level N on, to resume an interrupted run. ``--single``
writes ``rad_transfer_cal_single[_fromNN].method.toml`` instead: the purge
held at the calibration rate the whole run, and one plain R D R per level.
The procedure is ``docs/calibration/heat-flux-gauge-transfer.md``.

Same rig, levels, setpoints, heater settle and tag scheme as the HFG2
calibration (``scripts/make_hfg_transfer_methods.py``, whose step builders
this reuses), plus the radiometer's N2 purge, which the method sets through
the Alicat (``purge.setpoint``, in sccm):

* cold zeros at each purge rate: off, then 200, 100 and 400 sccm;
* the purge is off from each setpoint command until the heater PV reaches
  the setpoint, then at the calibration rate through the heater settle;
* with the heater over the radiometer, four holds: 200, 100, 400, 200 sccm.

Every window tag carries ``purge=<sccm>``. The two 200 sccm holds are the
calibration (with R1 and R2, as in the HFG2 run); the 100 and 400 sccm
holds, read against the off-centre reference at the same heater position,
give the purge effect relative to 200 sccm without moving the heater.

The single-rate method is the repeat once the 2026-10-18 run had shown no
purge effect over 0.1-0.4 L/min and no transient after a slide or a purge
change: one zero at 200 sccm, the purge left at 200 sccm throughout, and
the HFG2 timing (70 s windows, D straight after the slide), about 3 h.
"""

from __future__ import annotations

import argparse

from make_hfg_transfer_methods import (
    DOWN,
    FIRST_SETTLE_S,
    METHODS_DIR,
    REACH_TIMEOUT_DOWN_S,
    REACH_TIMEOUT_UP_S,
    SETPOINTS_C,
    SETTLE_S,
    UP,
    WINDOW_S,
    _s,
    _tag,
    acquire,
    prompt,
    reach,
    setpoint,
)

PURGE = "purge.setpoint"
CAL_SCCM = 200  # 0.2 L/min, the likely service rate
SWEEP_SCCM = (100, 400)  # held between the two calibration-rate holds
ZERO_SCCM = (0, 200, 100, 400)
# Before each radiometer hold: after the slide (the window warms when the
# heater centres on it) and after each purge change. The window's thermal
# time constant isn't known; the analysis checks the drift in each kept
# window.
HOLD_SETTLE_S = 150.0
# Before each zero window, after the purge change. On 2026-10-22 both zeros
# were still moving 5-8 uV three minutes after the purge came on, which is
# 1-3 % of the signal at 4-10 kW/m2.
ZERO_SETTLE_S = 300.0
ZERO_S = 120.0

CHECKLIST = (
    "Cooling water flowing through both gauges at a steady rate; N2 cylinder "
    "and regulator open (the method sets the purge, 0 now); Alicat setpoint "
    "source on serial; radiometer window clean; both gauges seated, top faces "
    "level; heater cold (PV within 2 degC of the room) and at stop A, centred "
    "on the REFERENCE; both gauge signals flat on the plot. Continue to record "
    "the zeros at purge off, 0.2, 0.1 and 0.4 L/min (about 12 min); the heater "
    "heats to level 1 after them."
)
SINGLE_CHECKLIST = (
    "Cooling water flowing through both gauges at a steady rate; N2 cylinder "
    "and regulator open (the method sets the purge to 0.2 L/min now and holds "
    "it); Alicat setpoint source on serial; radiometer window clean; both "
    "gauges seated, top faces level; heater cold (PV within 2 degC of the "
    "room) and at stop A, centred on the REFERENCE; both gauge signals flat on "
    "the plot. Continue to record the zero at 0.2 L/min (3 min); the heater "
    "heats to level 1 after it."
)
TO_REF = "Slide the heater to stop A (centred on the REFERENCE gauge), then Continue."
TO_RAD = "Slide the heater to stop B (centred on the RADIOMETER), then Continue."


def resume_checklist(level: int) -> str:
    return (
        f"Resuming at level {level}. Cooling water flowing through both gauges; "
        "N2 open; gauges, stops and faces untouched since the interrupted run; "
        "heater at stop A, centred on the REFERENCE. No zero is recorded: the "
        "analysis uses the interrupted run's cold zeros."
    )


def shutdown() -> str:
    return (
        '[[steps]]\nkind = "safe_shutdown"\nduration_s = 0.0\n'
        f"notes = {_s(_tag(win='shutdown'))}\n\n"
        f'[steps.cool_target]\n"heater.setpoint" = 20.0\n"{PURGE}" = 0.0\n'
    )


def zeros() -> list[str]:
    parts = []
    for sccm in ZERO_SCCM:
        tag = {"level": "00", "purge": sccm}
        parts += [
            setpoint(float(sccm), _tag(**tag, win="purge"), target=PURGE),
            acquire(ZERO_SETTLE_S, _tag(**tag, win="settleZ1")),
            acquire(ZERO_S, _tag(**tag, win="Z1")),
        ]
    return parts


def single_zero() -> list[str]:
    tag = {"level": "00", "purge": CAL_SCCM}
    return [
        setpoint(float(CAL_SCCM), _tag(**tag, win="purge"), target=PURGE),
        acquire(ZERO_SETTLE_S, _tag(**tag, win="settleZ1")),
        acquire(ZERO_S, _tag(**tag, win="Z1")),
    ]


def single_level_steps(i: int, n: int, q: int, up: bool) -> list[str]:
    """R D R at the calibration rate, the purge left on through every move."""
    sp = SETPOINTS_C[q]
    tag = {"level": f"{i:02d}", "q": q, "dir": "up" if up else "down", "purge": CAL_SCCM}
    title = f"Level {i} of {n}: ~{q} kW/m2 ({tag['dir']})"
    return [
        setpoint(sp, _tag(**tag, win="command")),
        reach(sp, up=up, notes=_tag(**tag, win="reach")),
        acquire(FIRST_SETTLE_S if i == 1 else SETTLE_S, _tag(**tag, win="settle")),
        acquire(WINDOW_S, _tag(**tag, win="R1")),
        prompt(title, TO_RAD, _tag(**tag, win="move")),
        acquire(WINDOW_S, _tag(**tag, win="D")),
        prompt(title, TO_REF, _tag(**tag, win="move")),
        acquire(WINDOW_S, _tag(**tag, win="R2")),
    ]


def level_steps(i: int, n: int, q: int, up: bool) -> list[str]:
    sp = SETPOINTS_C[q]
    base = {"level": f"{i:02d}", "q": q, "dir": "up" if up else "down"}
    title = f"Level {i} of {n}: ~{q} kW/m2 ({base['dir']})"
    cal = {**base, "purge": CAL_SCCM}
    parts = [
        setpoint(0.0, _tag(**base, purge=0, win="purge"), target=PURGE),
        setpoint(sp, _tag(**base, win="command")),
        reach(sp, up=up, notes=_tag(**base, win="reach")),
        setpoint(float(CAL_SCCM), _tag(**cal, win="purge"), target=PURGE),
        acquire(FIRST_SETTLE_S if i == 1 else SETTLE_S, _tag(**cal, win="settle")),
        acquire(WINDOW_S, _tag(**cal, win="R1")),
        prompt(title, TO_RAD, _tag(**cal, win="move")),
    ]
    for k, sccm in enumerate((CAL_SCCM, *SWEEP_SCCM, CAL_SCCM), start=1):
        tag = {**base, "purge": sccm}
        if k > 1:
            parts.append(setpoint(float(sccm), _tag(**tag, win="purge"), target=PURGE))
        parts += [
            acquire(HOLD_SETTLE_S, _tag(**tag, win=f"settleD{k}")),
            acquire(WINDOW_S, _tag(**tag, win=f"D{k}")),
        ]
    parts += [
        prompt(title, TO_REF, _tag(**cal, win="move")),
        acquire(WINDOW_S, _tag(**cal, win="R2")),
    ]
    return parts


def cal_method(start_level: int = 1, *, single: bool = False) -> tuple[str, float]:
    """The calibration method (or, with ``start_level > 1``, the rest of it)
    and its fixed duration in seconds, excluding the reach waits and prompts.

    A resumed method keeps the full run's level numbers and tags and records
    no zero; the interrupted run's cold zeros serve both. ``single`` holds
    the purge at the calibration rate and takes one D window per level.
    """
    levels = [(q, True) for q in UP] + [(q, False) for q in DOWN]
    n = len(levels)
    if not 1 <= start_level <= n:
        raise SystemExit(f"--from-level must be 1-{n}, got {start_level}")
    base = "rad_transfer_cal_single" if single else "rad_transfer_cal"
    if start_level == 1:
        what = (
            "R D R per level, N2 purge held at 0.2 L/min."
            if single
            else "R D R per level at 0.2 L/min N2 purge, with 0.1 and 0.4 L/min holds "
            "on the radiometer."
        )
        header = (
            f'name = "{base}"\n'
            'description = "Radiometer 222091 transfer calibration against the reference: '
            f'5-70 kW/m2 up, 50/30/10 down; {what}"\n'
        )
        checklist = SINGLE_CHECKLIST if single else CHECKLIST
        parts = [header, prompt("Before heating", checklist, _tag(win="checklist"))]
        if single:
            parts += single_zero()
            fixed = ZERO_SETTLE_S + ZERO_S
        else:
            parts += zeros()
            fixed = len(ZERO_SCCM) * (ZERO_SETTLE_S + ZERO_S)
    else:
        header = (
            f'name = "{base}_from{start_level:02d}"\n'
            f'description = "Radiometer 222091 transfer calibration resumed at level '
            f'{start_level} of {n}; same levels, tags and timing as {base}."\n'
        )
        parts = [
            header,
            prompt("Resume", resume_checklist(start_level), _tag(win="checklist")),
        ]
        if single:
            parts.append(setpoint(float(CAL_SCCM), _tag(purge=CAL_SCCM, win="purge"), target=PURGE))
        fixed = 0.0
    holds = 2 + len(SWEEP_SCCM)
    for i, (q, up) in enumerate(levels, start=1):
        if i < start_level:
            continue
        settle = FIRST_SETTLE_S if i == 1 else SETTLE_S
        if single:
            parts += single_level_steps(i, n, q, up)
            fixed += settle + 3 * WINDOW_S
        else:
            parts += level_steps(i, n, q, up)
            fixed += settle + 2 * WINDOW_S + holds * (HOLD_SETTLE_S + WINDOW_S)
    parts.append(shutdown())
    return "\n".join(parts), fixed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--from-level",
        type=int,
        default=None,
        help="write only rad_transfer_cal_fromNN.method.toml, resuming an interrupted run",
    )
    parser.add_argument(
        "--single",
        action="store_true",
        help="purge held at 0.2 L/min, one R D R per level (rad_transfer_cal_single*)",
    )
    args = parser.parse_args()
    start = 1 if args.from_level is None else args.from_level
    base = "rad_transfer_cal_single" if args.single else "rad_transfer_cal"
    name = f"{base}.method.toml" if start == 1 else f"{base}_from{start:02d}.method.toml"
    text, fixed = cal_method(start, single=args.single)
    path = METHODS_DIR / name
    path.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {path}")
    print(
        f"fixed time {fixed / 3600:.2f} h, plus the reach waits (1-5 min a level on "
        f"2026-10-17, timeouts {REACH_TIMEOUT_UP_S / 60:.0f}/{REACH_TIMEOUT_DOWN_S / 60:.0f} "
        "min up/down) and the operator's slides"
    )


if __name__ == "__main__":
    main()
