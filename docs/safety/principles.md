---
description: Four safety invariants in capa — audit-attributed device writes, isolated safety subsystem, fail-loud bundle sealing, and explicit non-real-time scope.
---

# Safety principles

**Audience:** all users; required reading for plugin authors and contributors who touch device-write paths.
**Scope:** the four invariants that govern every device-write, shutdown, and bundle-seal decision in CAPA — and an explicit statement of what CAPA is *not* responsible for.

CAPA controls a cone-calorimeter chamber that runs gas through heated samples for tens of minutes at a time. Every commanded write to a Watlow heater, an Alicat flow controller, a Sartorius balance, or a FLIR camera has consequences that survive a power cycle, a process crash, or an operator walking away mid-run. The safety contract is the set of rules that keep those consequences predictable.

The contract has four invariants. Everything else — the [authorization gate](authorization-gates.md), the [hold-to-confirm widget](destructive-operations.md), the [shutdown order](shutdown-sequence.md), the [saturation deadline](saturation-and-deadlines.md) — is mechanism in service of these four.

---

## Invariant 1: every device write is attributable

Every [`DeviceCommand`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/adapter.py) carries three audit fields. At least one of two valid combinations must be set:

| Provenance | `issued_by` | `authorization_id` | `confirmed_by` |
|---|---|---|---|
| Procedure / method step | operator id | run-arm id (8-byte hex) | `None` |
| Manual operator override | operator id | `None` | operator id |
| (no other shape is valid) | — | — | — |

The [`Authorization`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/authorization.py) helper mints `authorization_id` at run-arm time and refuses to issue once disarmed. The UI's manual-write path constructs a per-click `Authorization` with `run_id="manual"` and calls `issue_manual()`, which requires both `issued_by` and `confirmed_by` and stamps `authorization_id=None` so the audit log shows clearly that this command was not part of any procedure.

Every command ends up in the bundle's event log with all three fields preserved. **A device write that is missing this attribution chain is a bug.** See [authorization gates](authorization-gates.md) for the calling pattern.

---

## Invariant 2: safety is its own subsystem with its own state

The pieces that enforce safety run on dedicated paths, with state separate from the data-acquisition path:

- The [`SaturationMonitor`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/saturation.py) runs on the [conductor](../glossary.md#conductor) loop alongside the procedure but with its own poll cadence and its own escalation callback. It cannot be silenced by a misbehaving procedure or a slow writer; it only ever *signals*. It enforces the [saturation deadline](../glossary.md#saturation-deadline) regardless of what the procedure does.
- The [`Authorization`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/authorization.py) handle is constructed once per run and disarmed in the conductor's `finally` block — surviving any exception inside the procedure or executor.
- The [`HoldToConfirmButton`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/ui/hold_to_confirm.py) on the Run tab runs entirely in the UI thread; the conductor cannot bypass it, and a CPU-starved conductor loop does not prevent the button from animating.

The result: failure modes that take out the procedure (an unhandled exception, a busy-loop, a wedged adapter) do not also take out the safety machinery. The conductor's [`RunOutcome.CRASHED`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/conductor.py) path still runs adapter `stop()` and still seals the bundle.

---

## Invariant 3: fail loud, seal the bundle

When CAPA detects an unrecoverable condition — a saturation deadline trip, an unhandled exception in the procedure, a writer-thread fault — the conductor's first responsibility is **not** to continue acquiring data or to keep the procedure running. It is to:

1. Disarm authorization so no further procedure-issued commands can leave the system.
2. Call `adapter.stop()` on every worker so streaming exits and resources close. Adapter `stop()` is not a universal hardware-safe-state command; procedures must drive explicit safe setpoints when needed.
3. Seal the bundle with a `RunOutcome` that names the failure cause.

"Best-effort continue" is not a CAPA failure mode. The conductor recognises four outcomes ([`RunOutcome`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/conductor.py)):

| Outcome | Meaning |
|---|---|
| `COMPLETED` | Procedure ran to its natural end. |
| `ABORTED` | Operator (or supervising code) stopped before completion. |
| `CRASHED` | Unhandled exception in procedure, drain task, or pool. **Bundle still sealed.** |
| `CRASHED_BUT_SEALED` | Saturation deadline tripped. Conductor disarmed workers and sealed anyway. |

Every one of those four outcomes ends in a sealed bundle. The bundle's manifest carries the [outcome string](../glossary.md#bundle-outcome), so any downstream tool reading a bundle can tell at a glance whether the run completed normally, was stopped, or was sealed under duress.

This is why the [shutdown sequence](shutdown-sequence.md) does not have an "abandon" path — there is no code path that exits without going through disarm, drain, and seal. If the host *process* dies (kill -9, power cut), the next CAPA invocation's [crash recovery](shutdown-sequence.md#crash-recovery) seals the orphaned bundle on startup.

---

## Invariant 4: CAPA is not a hard-real-time control system

This is the most important invariant for plugin authors to understand, and the easiest to forget.

CAPA orchestrates devices, records what they emit, and provides an authorization gate over what gets issued. It is **not** the layer that keeps the rig safe in a hardware sense. The actual safety boundaries are:

- **The Watlow PID controller's own loop.** Watlow runs at hardware loop rates with its own setpoint, alarm limits, OUT4 alarm relay, and overtemperature trip. CAPA *sends* setpoints; Watlow *enforces* them.
- **The chamber's physical exhaust and gas-supply integrity.** Gas flow, exhaust path, sample fixturing, and the chamber's own interlocks are physical-plant concerns CAPA cannot reach.
- **The host operating system.** CAPA runs on Windows, on a general-purpose CPU, on top of an asyncio event loop. It is not a real-time OS. Loop lag of tens of milliseconds is normal and acceptable; the [saturation deadline](saturation-and-deadlines.md) is 10 seconds, not 10 milliseconds.

What this means in practice:

- **Do not put hard-real-time logic in a `CustomStep`.** If a procedure step needs to "react within 50 ms," put that reaction in the device firmware (Watlow OUT4 alarm, Alicat flow setpoint with hardware ramp). The CAPA loop will not honor it reliably.
- **Do not treat the absence of a CAPA command as "safe."** If CAPA crashes mid-run, the heater can stay at its last setpoint until a procedure-level cleanup reaches it, an adapter-specific stop hook drives it safe, or the Watlow's own alarm trips. Configure Watlow alarms accordingly.
- **Do not rely on CAPA for emergency stop.** The big red button on the rig is the emergency stop. The UI's [hold-to-confirm Emergency Stop](destructive-operations.md) is a software abort request — it still goes through the normal disarm-drain-seal sequence.

CAPA's job is reproducibility, attribution, and clean sealing. Hardware safety is the hardware's job, by design.

---

## How the four invariants compose

Read together: **every write is attributable** (invariant 1), **safety state survives data-path failure** (invariant 2), **failure paths still seal cleanly** (invariant 3), and **CAPA is not the last line of defense** (invariant 4).

A plugin author who internalises these four does not need to ask permission for most design decisions. A custom step that issues commands through `ctx.authorization.issue()` and tolerates `AuthorizationError` is correct by construction. A custom manual control that calls `dispatch(destructive=True, …)` and trusts the parent card's `QMessageBox` is correct by construction. A custom sink that fails loudly rather than silently dropping is correct by construction.

If your change to CAPA would violate any of these four, that's a discussion to have *before* writing the code.

---

*See also:* [Authorization gates](authorization-gates.md) · [Destructive operations](destructive-operations.md) · [Shutdown sequence](shutdown-sequence.md) · [Saturation and deadlines](saturation-and-deadlines.md) · [Runtime architecture §6.3](../architecture/runtime-architecture.md#63-saturation-deadline)
