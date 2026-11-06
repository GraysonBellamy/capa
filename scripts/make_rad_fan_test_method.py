"""Write the fan test: does convection on the reference explain the radiometer's
low-flux deficit?

Run with: ``uv run python scripts/make_rad_fan_test_method.py``

Writes ``configs/methods/rad_fan_test.method.toml``. The heater stays at stop A
(centred on the reference) throughout; at 378 C (~10 kW/m2, the calibration's
level 12) the operator switches a small fan on and off at prompts:
off, on, off, on, off, 180 s each. The radiometer sees radiation only, the
reference radiation plus convection, so a fan that changes the convection on
the reference steps the ratio V_rad/V_ref within seconds of the switch. A
slower drift over minutes is the fan cooling the heater shell instead (the
heat-soak / angular-response idea). Windows carry ``fantest sp=.. fan=on|off
win=Fn`` tags; ``scripts/analyze_fan_test.py`` reads them.
"""

from __future__ import annotations

from make_hfg_transfer_methods import METHODS_DIR, _s, acquire, prompt, setpoint

PURGE = "purge.setpoint"
PURGE_SCCM = 200.0
SETPOINTS_C = (378.0,)  # level 12 of the calibration
SETTLE_S = 300.0
STATE_S = 180.0
FAN_STATES = ("off", "on", "off", "on", "off")
BAND_C = 1.0
TIMEOUT_S = 2700.0


def _tag(**fields: object) -> str:
    return "fantest " + " ".join(f"{k}={v}" for k, v in fields.items())


def wait_pv(op: str, value: float, notes: str) -> str:
    return (
        f'[[steps]]\nkind = "wait"\ntimeout_s = {TIMEOUT_S}\non_timeout = "warn"\n'
        f"notes = {_s(notes)}\n\n"
        f'[steps.end_condition]\nchannel = "heater.pv"\nop = "{op}"\nvalue = {value}\n'
    )


def block(sp: float) -> list[str]:
    tag = {"sp": int(sp)}
    # Two waits reach the setpoint from either side: from above (still hot
    # from the run) the first holds until the heater has cooled, from below
    # the second until it has heated.
    parts = [
        setpoint(sp, _tag(**tag, win="command")),
        wait_pv("<=", sp + BAND_C, _tag(**tag, win="reach")),
        wait_pv(">=", sp - BAND_C, _tag(**tag, win="reach")),
        acquire(SETTLE_S, _tag(**tag, win="settle")),
    ]
    for i, state in enumerate(FAN_STATES):
        if i > 0:
            action = "ON" if state == "on" else "OFF"
            parts.append(
                prompt(
                    f"Fan test at {int(sp)} C",
                    f"Switch the fan {action}, then Continue.",
                    _tag(**tag, fan=state, win="switch"),
                )
            )
        parts.append(acquire(STATE_S, _tag(**tag, fan=state, win=f"F{i}")))
    return parts


CHECKLIST = (
    "Heater at stop A, centred on the REFERENCE, and it stays there for the whole "
    "test. Fan on a medium setting, OFF, placed about 0.5 m to the side and aimed "
    "horizontally across the gap between the gauge faces and the heater; don't move "
    "it during the test. Keep the cooling water as it was. Continue to start: the "
    "heater goes to 378 C, then the fan prompts follow."
)


def method() -> str:
    header = (
        'name = "rad_fan_test"\n'
        'description = "Fan on/off at 378 C with the heater over the reference: '
        'convection on the reference vs the radiometer."\n'
    )
    parts = [
        header,
        prompt("Fan test", CHECKLIST, _tag(win="checklist")),
        setpoint(PURGE_SCCM, _tag(win="purge"), target=PURGE),
    ]
    for sp in SETPOINTS_C:
        parts += block(sp)
    parts.append(
        '[[steps]]\nkind = "safe_shutdown"\nduration_s = 0.0\n'
        f"notes = {_s(_tag(win='shutdown'))}\n\n"
        f'[steps.cool_target]\n"heater.setpoint" = 20.0\n"{PURGE}" = 0.0\n'
    )
    return "\n".join(parts)


def main() -> None:
    path = METHODS_DIR / "rad_fan_test.method.toml"
    path.write_text(method(), encoding="utf-8", newline="\n")
    per_block = SETTLE_S + len(FAN_STATES) * STATE_S
    print(f"wrote {path}")
    print(
        f"fixed time {len(SETPOINTS_C) * per_block / 60:.0f} min, plus the reach waits "
        "and the fan prompts"
    )


if __name__ == "__main__":
    main()
