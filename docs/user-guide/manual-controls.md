---
description: capa Manual Control dock — per-device cards for Watlow heaters, Alicat MFCs, Sartorius balances, Fuji gas analyzers, and cameras, with capability gating and destructive-op confirm.
---

# Manual controls

**Audience:** operators issuing one-off commands — taring a balance,
setting a heater between runs, picking a webcam resolution.
**Scope:** the Manual Control dock, the per-device cards inside it, how
commands route to the right place at the right time, and the
confirmation dialog that protects destructive operations.

---

## Anatomy

The dock is one vertically-stacked, scrollable list of cards. One card
per device that advertises any manual-relevant capability.

![Manual control dock with two cards: Heater "heater" (watlow_sim, setpoint + safe-cool) and Alicat "air_mfc" (alicat_sim, setpoint + tare flow / ΔP). Both show `idle` status.](../_snippets/images/manual-dock-cards.png)

```
┌─ Manual Control ─────────────────────────────────────────────────┐
│                                                                  │
│  ┌─ Balance: balance_main ────────────────────────────────────┐  │
│  │  Last cal: 22.4 °C                                         │  │
│  │  [Tare]   [Zero]   [Internal cal…]   [Save settings…]      │  │
│  │  Filter: [stable    ▾]   Auto-zero: [on ▾]                 │  │
│  └────────────────────────────────────────────────────────────┘  │
│                                                                  │
│  ┌─ Heater: heater_01 ────────────────────────────────────────┐  │
│  │  PV: 24.6 °C    SP: 25.0 °C                                │  │
│  │  Setpoint: [   25.0  ] °C   [Apply]                        │  │
│  │  [Heat-flux tune…]                                         │  │
│  └────────────────────────────────────────────────────────────┘  │
│                                                                  │
│  ┌─ Alicat: mfc_n2 ───────────────────────────────────────────┐  │
│  │  Gas: N2   Setpoint: 0 SLPM                                │  │
│  │  Value: [   0.000 ] SLPM   [Set]                           │  │
│  │  Gas: [N2 ▾]   [Set (session)]   [Set + save (EEPROM)]     │  │
│  └────────────────────────────────────────────────────────────┘  │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

The dock can be torn off and re-docked on any edge of the main window.

When no config is loaded:

![Manual control dock empty state: "No config loaded.\n\nOpen a config from File → Open Config…"](../_snippets/images/manual-empty.png)

When a config has loaded but no device advertises a manual-control
capability:

```
No devices with manual controls in this config.
```

---

## How cards appear and disappear

The dock rebuilds on every config-load. Cards are gated *reflectively*
on each adapter's `Capability` flagset. If a device advertises **none**
of the manual-relevant capabilities (`HAS_TARE`, `HAS_ZERO`,
`HAS_INTERNAL_CAL`, `HAS_PARAMETER_CONFIG`, `HAS_SETPOINT`,
`HAS_GAS_SELECT`, `HAS_VALVE_HOLD`, `HAS_TOTALIZER`,
`HAS_DISPLAY_CONTROL`, `HAS_GAS_CALIBRATION`), the device is skipped
entirely — no empty card.

If the worker pool is still **opening** when the dock builds, the cards
fall back to the adapter-import-string fingerprint (e.g. *sartorius* →
balance, *alicat* → MFC) to pick the right card class. Their controls
stay disabled until the pool is open.

An Alicat only reports whether it is a controller once it has been
opened, so the Alicat card shows every section until then. Once the
pool is open it rebuilds from the device's real flags: a meter loses
its Setpoint and Valves sections. It then reads the active gas, the
gas list and the setpoint from the device; the subtitle shows the
active gas and setpoint, and the gas box stays empty until the device
has reported one. The setpoint is always in the device's own
engineering units, shown beside the value: the card has no unit to
pick, because the device applies the number in its units whatever unit
is named. Change units on the device itself. On firmware that reports
its setpoint unit, a setpoint sent after the device's unit has changed
is refused.

The six card classes that ship today:

| Card | Devices it matches | Common controls |
|---|---|---|
| **HeaterCard** | Watlow temperature controllers | Setpoint, Heat-flux tune launcher |
| **BalanceCard** | Sartorius balances | Tare, zero, internal cal, filter / auto-zero / display unit / tare behavior, save settings; shows those settings and the last calibration |
| **AlicatCard** | Alicat MFCs and pressure devices | Flow setpoint, gas select, valve hold, totalizer reset; shows the device's active gas and setpoint |
| **FujiCard** | Fuji ZP-series gas analyzers | Response time, range and range method, output hold, calibration-gas setting, a guarded zero or span with a live steadiness readout |
| **FlirCard** | FLIR IR cameras | Temperature range (picked from the ranges the camera reports, in °C), NUC trigger, auto-NUC interval, radiometric parameters, palettes (choices read from the camera); every field shows the camera's current setting |
| **WebcamCard** | USB / built-in cameras | Resolution, framerate, codec |

---

## Routing: where does my command actually go?

Cards never dispatch directly. They go through `ManualClient`, which
routes one of two ways depending on the current [run state](the-run-tab.md):

| When | Routes to | Recorded in bundle? |
|---|---|---|
| `IDLE` / `SEALED` / `FAILED` (no run, or run finished) | **WorkerPool** | No — there is no run. The dispatch shows up in the events dock as a "manual_event" for audit. |
| `RUNNING` | **Conductor** | Yes — the command is recorded as an event in `events.sqlite`. The operator id is stamped onto it. |
| `PREPARING`, `DRAINING`, `FINALIZING` *(write-blocked states)* | **Refused** | Card disables its buttons. |

During a run, the cards show their write-blocked state inline:

![Manual dock during a run: both cards' value spinboxes and action buttons are grayed; the inline status text reads `run running — manual writes disabled`.](../_snippets/images/manual-writeblocked.png)

The write-blocked states exist because the conductor doesn't own the
workers cleanly during them — `PREPARING` is opening, `DRAINING` is
disarming, `FINALIZING` has already handed the bundle to the sealer.
Trying to dispatch a manual command in those windows would race the
state machine.

If you need to change a setpoint mid-run, the right place is *during*
`RUNNING`. The card will accept it, the conductor will route it, and
the change is captured in the bundle alongside the sample data.

---

## Confirmation for destructive operations

Some manual commands physically change the device's stored state in a
way that can't be undone by a power cycle. These show a
**confirmation dialog** before dispatching:

- **Balance:** Internal cal, Save settings (writes EEPROM), Reload from EEPROM.
- **Heater:** Save parameters (writes EEPROM).
- **Alicat:** Save calibration (when chosen), Reset totalizer peak-flow watermarks.

The dialog spells out exactly what's about to happen — for example:

> Confirm destructive operation:
>
>   Trigger internal calibration on balance_main. Requires ~30 s; the
>   balance refuses other commands during the cal cycle.
>
> Proceed?

This is a standard modal `QMessageBox` — click Yes / No, not hold-to-
confirm. (The 1-second hold-to-confirm pattern is used only for the
Emergency stop button on the [Run tab](the-run-tab.md#emergency-stop),
where the cost of a misclick is much higher.)

Non-destructive commands (Tare, Zero, setpoint changes, gas select)
dispatch immediately on click.

A confirmation in flight:

![Confirmation dialog: "Drive heater to 25 °C? The current setpoint will be overwritten." with OK / Cancel buttons.](../_snippets/images/manual-confirm-dialog.png)

---

## Read-back values

Each card shows the device's current state above its controls — a heater
card shows `PV` and `SP`, a balance shows the temperature at its last
calibration (`Last cal: 22.4 °C`, or `none since power-up`; the balance
keeps no date), an MFC shows its active gas and setpoint, an IR camera
shows its active temperature range. A setting the card can change — an
MFC's gas, a balance's filter mode — is selected from what the device
reports, and stays empty until the device has reported it. Every field
on an IR camera card — range, auto-NUC interval, radiometric values,
both palettes — shows the camera's setting; one you have changed but
not yet applied keeps your change through a refresh, and a command you
cancel at its confirmation leaves it changed. These refresh:

- **Once the pool reports ready**, asynchronously.
- **After a command that changes them** — a setpoint, a gas, a balance
  setting or calibration — so the card shows what the device took.

If the read-back fails (typically because the pool is mid-rebuild after
a cold reload), the card logs at debug level and leaves the previous
value visible. No banner; the next refresh attempt catches up.

---

## Jumping to a card from the Setup tab

In the [Setup tab](the-setup-tab.md#hardware-devices)'s Devices
section, right-click a row → **Open manual control**. The main window
switches to the Run tab, shows the manual dock if it was hidden, and
scrolls the named device's card into view (centred in the viewport).

Convenient when you're triaging a device that's misbehaving and don't
want to scrub through a long manual-dock scroll.

---

*See also:* [The Run tab](the-run-tab.md) for write-blocked-state
gating, [The Setup tab](the-setup-tab.md) for the Devices section that
jumps here, [Authorization gates](../safety/authorization-gates.md) and
[Destructive operations](../safety/destructive-operations.md) for the
safety contract behind the confirmation dialog.
