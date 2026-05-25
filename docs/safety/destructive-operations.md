# Destructive operations

**Audience:** operators issuing manual commands; plugin authors exposing controls in the Manual Control dock.
**Scope:** commands flagged `destructive=True` and the two confirmation gates (modal dialog and hold-to-confirm button) that stand between an operator gesture and the device write.

A "destructive" command in CAPA terminology is one whose effect survives a power cycle, alters device state in a way the run does not need, or is dangerous to invoke accidentally. EEPROM writes, factory-style resets, valve closures, totalizer resets, and the Emergency Stop on the Run tab all qualify. The bar is *survives power cycle OR can't be undone by issuing the opposite command*.

The audit-trail guarantee from [safety principle 1](principles.md#invariant-1-every-device-write-is-attributable) applies just as it does to non-destructive writes — every confirmed destructive command lands in the bundle event log with `issued_by` and `confirmed_by`. The additional thing destructive commands get is a *confirmation gate* in the UI.

---

## What counts as destructive today

The full list of `destructive=True` dispatches in the Manual Control cards, as of this writing:

| Adapter | Command kind | What it does |
|---|---|---|
| Watlow | `set_setpoint` (from the "Cool to safe" button) | Drives the heater to a low setpoint; commits via the same write path as ordinary setpoints but is gated because a stuck low setpoint mid-experiment is destructive to the run. [`watlow.py:352`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/ui/manual/cards/watlow.py#L352) |
| Watlow | `write_parameter` | Persistent parameter write to the controller — survives power-cycle. [`watlow.py:422`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/ui/manual/cards/watlow.py#L422) |
| Sartorius balance | `internal_adjust` | Triggers the balance's motorized internal calibration cycle. [`balance.py:164`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/ui/manual/cards/balance.py#L164) |
| Sartorius balance | `save_menu` | Save menu settings to EEPROM. [`balance.py:225`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/ui/manual/cards/balance.py#L225) |
| Sartorius balance | `reload_menu` | Reload menu settings from EEPROM. [`balance.py:241`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/ui/manual/cards/balance.py#L241) |
| Alicat MFC | `set_gas` (with `save=True`) | Persist gas selection to EEPROM (the session-only variant is non-destructive). [`alicat.py:193`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/ui/manual/cards/alicat.py#L193) |
| Alicat MFC | `hold_valves_closed` | Force the controller's valves fully closed regardless of setpoint. [`alicat.py:254`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/ui/manual/cards/alicat.py#L254) |
| Alicat MFC | `totalizer_reset` | Zero a totalizer's accumulated flow history. [`alicat.py:284`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/ui/manual/cards/alicat.py#L284) |
| Alicat MFC | `totalizer_reset_peak` | Zero a totalizer's peak-flow watermark. [`alicat.py:298`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/ui/manual/cards/alicat.py#L298) |
| FLIR camera | `set_temperature_range` | Persistent camera-firmware config that survives power-cycle. [`camera.py:184`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/ui/manual/cards/camera.py#L184) |

Not on this list today: factory reset, baud rate change, and explicit gas-supply-line venting. If a future adapter adds those, flag them `destructive=True` and add a `destructive_summary` describing the consequence.

---

## Gate 1: modal confirmation in manual cards

Every dispatch from a Manual Control card flows through [`ManualCardBase.dispatch()`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/ui/manual/cards/base.py). The relevant snippet:

```python
if destructive:
    summary = destructive_summary or f"{kind} on {self._name}"
    answer = QMessageBox.question(
        self,
        "Confirm device write",
        f"Confirm destructive operation:\n\n  {summary}\n\n"
        f"Operator: {operator}\n\n"
        "This may persist to EEPROM or otherwise alter device "
        "state in a way that survives power-cycle.",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,  # default is No
    )
    if answer != QMessageBox.StandardButton.Yes:
        self._set_status("cancelled", level="idle")
        return None
```

Three things to note:

- **Default is "No."** The dialog opens with focus on `No`, so an accidental Enter-press cancels.
- **The operator id is shown in the dialog.** This is the operator who will be stamped into the `confirmed_by` field if they click `Yes`.
- **Cancellation is logged as an `idle` status, not as an event.** The card resets to its quiescent state; no `confirmed` row is written into the event log. (Future change: log cancellations to make accidental-near-misses auditable — track in your project notes.)

If the operator clicks `Yes`, the card constructs a one-off `Authorization(operator_id, run_id="manual")` and calls `issue_manual()` — see [authorization gates](authorization-gates.md#manual-overrides-from-the-ui).

The dialog is *only* shown when `destructive=True`. Non-destructive manual commands (`zero_tare`, `set_setpoint` to a normal value, totalizer read) dispatch immediately with no confirm dialog.

---

## Gate 2: hold-to-confirm (Run tab Emergency Stop)

The Manual Control dialog handles "EEPROM survival" destructiveness. A separate widget — [`HoldToConfirmButton`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/ui/hold_to_confirm.py) — handles "fast-clickable + lose-an-hour-of-work" destructiveness.

It is used in exactly one place today: the Run tab's Emergency Stop button. The mechanic:

- Press and hold for **`HOLD_DURATION_MS = 1000`** milliseconds.
- A progress fill animates inside the button at ~30 FPS.
- Release before the 1 s mark — the in-progress hold is silently cancelled and the button re-arms for the next press.
- Hold for the full second — the `confirmed` signal fires, which (for the Emergency Stop) calls `_on_abort_clicked("immediate")`.

> **Emergency stop is "immediate," not "graceful."** The two buttons both stop the run and both produce a sealed bundle. They differ in the `exit_reason` stamped into the audit log — `operator_immediate` (Emergency Stop) vs `operator_safe_shutdown` (Stop run) — and in what the procedure does in response. The convention: `operator_safe_shutdown` means "honour your cleanup discipline (cool to safe target, run a `SafeShutdownStep` via `run_segment`, etc.) before unwinding"; `operator_immediate` means "exit as fast as you can." See [shutdown sequence](shutdown-sequence.md#graceful-stop-vs-emergency-stop) for the full mechanics — including the subtlety that the conductor itself does not differentiate, so the actual cleanup behaviour comes from the procedure.

The 1-second duration is a deliberate compromise: long enough that an accidental brush against the button cannot trigger an emergency stop on a 30-minute run, short enough that a deliberate operator response (rig misbehaving, operator wants to stop *now*) is not frustrating.

The widget is *not* themed to the manual-card visual language — it has its own destructive-red accent and font weight. That's intentional. Visually, the Emergency Stop should not be mistakable for a normal button.

---

## Flagging a custom command destructive

If you're writing a custom manual card (e.g. as a plugin) and exposing a command that meets the bar — survives power cycle, can't be undone by issuing the opposite command, or has consequences disproportionate to a click — flag it.

The contract is: pass `destructive=True` and `destructive_summary="…"` to `schedule_dispatch()` (or the underlying `dispatch()` if you're driving directly). The base class handles the rest:

```python
self.schedule_dispatch(
    kind="my_destructive_verb",
    payload={"foo": 1},
    destructive=True,
    destructive_summary=(
        "One sentence: what does this do, and what survives power-cycle? "
        "Mention EEPROM if relevant — operators look for that word."
    ),
)
```

The `destructive_summary` is what the operator sees in the confirmation dialog. Write it for an operator who knows the device but doesn't remember exactly which verb you're invoking. Mention EEPROM, flash wear, or "can't be undone" explicitly when applicable.

**Do not** plumb the hold-to-confirm widget into a Manual Control card. The widget is intentionally scoped to the Emergency Stop today — adding a layer of "hold for 1 s" on top of every destructive dispatch is more friction than the safety benefit warrants, and the modal dialog is the established convention.

---

## What does NOT need the destructive gate

For symmetry, here are the kinds of writes that intentionally do *not* require destructive confirmation:

- **Procedure-issued setpoints during a run.** They go through `Authorization.issue()` and inherit the run-arm coverage. The fact that the operator armed the run is the consent.
- **Method-step setpoints.** Same — the method is part of the armed config.
- **Read commands.** Any command whose effect is "ask the device for a value" — `read_status`, `read_gas_list`, `get_totalizer`, etc. — has no destructive component.
- **Session-only writes.** E.g. Alicat `set_gas` *without* `save=True` is dispatched without confirmation — the gas selection reverts on power-cycle. Only the persistent `save=True` variant is destructive.

The principle: confirmation is for actions whose consequences the operator might not have anticipated. A setpoint change during an armed run is part of the contract the operator already agreed to.

---

*See also:* [Authorization gates](authorization-gates.md) · [Shutdown sequence](shutdown-sequence.md) · [Manual controls](../user-guide/manual-controls.md) · [The Run tab](../user-guide/the-run-tab.md)
