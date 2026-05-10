# Handoff: deferred device-control work (UI panel, method step, config apply)

**Status:** ready to pick up after the dispatch foundation lands (this commit)
**Parent context:** [docs/capa-plan.md](capa-plan.md), `capa-plan §5.2` and `§9`
**Sibling:** [handoff-capa-flir-control-surface.md](handoff-capa-flir-control-surface.md) — upstream FLIR work needed before camera control can be added to the panel

---

## What landed in this iteration

The "forge the pathway" pass:

- New `Capability` flags in [src/capa/devices/adapter.py](../src/capa/devices/adapter.py):
  `HAS_INTERNAL_CAL`, `HAS_PARAMETER_CONFIG`, `HAS_TOTALIZER`, `HAS_VALVE_HOLD`,
  `HAS_DISPLAY_CONTROL`.
- `SartoriusAdapter._dispatch_command` extended with: `internal_adjust`,
  `set_filter_mode`, `set_display_unit`, `set_auto_zero`, `set_isocal_mode`,
  `set_tare_behavior`, `save_menu`, `reload_menu`. CAPA's authorization gate
  forwards `confirm=True` to sartoriuslib's own DANGEROUS / PERSISTENT gates
  so the operator never sees two confirm prompts.
- `SartoriusAdapter` typed wrappers + `read_last_cal_record()` (pure read,
  no auth gate; mirrors `read_mass()`).
- `AlicatAdapter._dispatch_command` extended with: `set_units`,
  `set_zero_band`, `set_stp_pressure`, `set_stp_temperature`,
  `set_setpoint_source`, `set_loop_variable`, `set_ramp_rate`,
  `set_deadband`, `set_auto_tare`, `set_power_up_tare`, `blink_display`,
  `lock_display`, `unlock_display`, `hold_valves`, `hold_valves_closed`,
  `cancel_valve_hold`, `totalizer_reset`, `totalizer_reset_peak`,
  `totalizer_save`. Controller-only verbs gated through
  `_require_controller`; `gas` payload now accepts a `save` boolean.
- `AlicatAdapter` typed wrappers for the high-frequency manual ops
  (`hold_valves`, `hold_valves_closed`, `cancel_valve_hold`,
  `totalizer_reset`, `set_units`, `lock_display`, `unlock_display`)
  + `read_gas_list()` (pure read).
- Unit tests covering the new dispatch verbs and the
  `_require_controller` gate (controller-only verbs reject on a meter).
- `update_capabilities_from_device` now adds `HAS_VALVE_HOLD` alongside
  `HAS_SETPOINT` after `open()` identifies the device as a controller.

**Test status:** 626 unit + integration tests passing, 17 hardware-skipped.
mypy clean. ruff clean.

---

## Completed: 1. Manual device-control panel UI

**Status:** landed. See [src/capa/ui/manual/](../src/capa/ui/manual/) and
[src/capa/ui/docks/manual_control.py](../src/capa/ui/docks/manual_control.py).

What shipped:

- **[`DeviceRegistry`](../src/capa/devices/registry.py)** — shared,
  connection-layer adapter pool. The engine and the manual control panel
  now both borrow live adapters from one registry instead of opening
  their own. Adapters stay open across runs, eliminating the cold-open
  cost on every run-arm cycle. The engine accepts an optional
  `device_registry` parameter on construction; when `None`, it
  constructs and owns its own (preserves the CLI test path).
- **[`ManualControlDock`](../src/capa/ui/docks/manual_control.py)** —
  single dock with per-device cards, rebuilt on each config load.
- **`BalanceCard` / `AlicatCard` / `FlirCard`** — capability-gated card
  classes under [src/capa/ui/manual/cards/](../src/capa/ui/manual/cards/).
  Each card subscribes to engine-state changes and auto-disables writes
  while a run is in progress.
- **`Authorization.issue_manual()`** invoked from a shared `dispatch()`
  on the card base. Destructive ops (`internal_adjust`, `save_menu`,
  `hold_valves_closed`, `totalizer_reset*`, `set_temperature_range`,
  gas with `save=True`) pop a `QMessageBox.question` before issuing.
- **Devices menu** in the menu bar (`Ctrl+M`) toggles the dock.
- **Setup-tab right-click** on a device row emits
  `device_action_requested` which `MainWindow` routes to the dock.
- **Synthesized `DeviceEvent`s** mirror every manual command into the
  events dock via a new `RunController.manual_event` signal — manual
  commands appear alongside engine events in the live audit surface.
  (Bundle persistence is still out-of-scope since no bundle is open
  during manual mode; see §3 below.)

Cameras are intentionally **not** routed through the `DeviceRegistry`:
frame timestamps must anchor to the per-run clock, and the registry's
clock is anchored at MainWindow construction. `FlirCard` constructs its
own camera handle on first action and auto-closes it on engine
`PREPARING` so the engine can re-open with the run clock. Devices have
no such constraint, so they share freely.

**Test status:** 680 unit + integration tests passing (was 626 before
this work landed), 17 hardware-skipped. mypy clean over the modified
files. New tests live at
[tests/unit/test_device_registry.py](../tests/unit/test_device_registry.py)
and
[tests/unit/test_manual_control_cards.py](../tests/unit/test_manual_control_cards.py)
with stub adapters in [tests/fixtures/](../tests/fixtures/).

---

### Original deferred design notes (kept for reference)

The bracketed "Why it's deferred" framing is preserved below because
parts of the design rationale carried into the implementation.

**Why it was deferred:** UI architecture work, not part of the dispatch
foundation. The core need (start-of-day balance internal calibration,
manual gas swap, manual tare) is now reachable via `adapter.command(...)`
with `Authorization.issue_manual()` — an operator with terminal access
can do it. The panel is the ergonomic surface.

### Design sketch

One dock or popup per device, opened from the Setup tab's device tree.
Driven entirely off `adapter.capabilities`:

```
┌─ Balance: balance.main ─ [×] ────────────┐
│ Model: MSE1203S   Serial: SN-BAL-001     │
│ Last cal: 2026-04-22 14:30 OK            │  ← read_last_cal_record()
│                                          │
│ [Tare]  [Zero]                           │  ← HAS_TARE / HAS_ZERO
│ [Internal calibration…]                  │  ← HAS_INTERNAL_CAL (confirm)
│                                          │
│ Filter mode: [Stable        ▾]           │  ← HAS_PARAMETER_CONFIG
│ Auto-zero:    [On            ▾]          │
│ Display unit: [g             ▾]          │
│ [Save to EEPROM]  [Reload]               │
└──────────────────────────────────────────┘
```

```
┌─ MFC: mfc.purge ─ [×] ────────────────────┐
│ Model: MC-100SCCM-D    Gas: N2            │
│ ─── Setpoint ───                          │  ← HAS_SETPOINT
│ Current: 50.0 SCCM     [Set…]             │
│ ─── Tare ───                              │  ← HAS_TARE
│ [Tare flow]  [Tare ΔP]                    │
│ ─── Valves ───                            │  ← HAS_VALVE_HOLD
│ [Hold]  [Hold closed!]  [Cancel hold]     │
│ ─── Totalizer ───                         │  ← HAS_TOTALIZER
│ Total: 1234.5 SCC     [Reset]             │
│ ─── Display ───                           │  ← HAS_DISPLAY_CONTROL
│ [Blink 3s]  [Lock]  [Unlock]              │
└───────────────────────────────────────────┘
```

### Implementation steps

1. **New module:** `src/capa/ui/dialogs/device_control.py` (or a dock
   under `src/capa/ui/docks/`). Per-adapter widget tree built reflectively
   from the capabilities frozenset. The class hierarchy is small enough
   that you can hardcode one widget class per adapter type — don't try
   to fully generalize this until there's a third or fourth device family.

2. **Authorization wiring:** every action button builds a
   `DeviceCommand` via
   [src/capa/experiment/authorization.py:115](../src/capa/experiment/authorization.py#L115)
   `Authorization.issue_manual(...)`. Pull `issued_by` and `confirmed_by`
   from the operator-id widget that already lives in the status bar; for
   destructive ops (internal cal, hold-valves-closed, totalizer reset,
   save_menu / reload_menu) show a `QMessageBox` confirm before issuing.

3. **Run-state gating:** the panel must refuse to dispatch any device
   write while `engine.state == "running"`. Manual commands during a
   run go through procedure steps, not this panel. The Run tab already
   has the Arm/Start state machine; subscribe to it.

4. **Result surface:** `CommandResult.accepted=False` (auth refusal) or
   library exceptions need to render as inline status, not as silent
   no-ops. Use the toast / status-bar pattern that already exists for
   adapter watchdog warnings.

5. **Live read-back:** the panel should call `adapter.read_last_cal_record`
   / `adapter.read_gas_list` etc. on open and after each successful write,
   so the displayed state stays in sync with the device.

6. **Discoverability:** add a "Devices" menu in the menu bar that lists
   every connected adapter; clicking opens its panel. Also make each
   device row in the Setup tab right-clickable.

### Tests

- `pytest-qt` test that builds an `AlicatAdapter` with sim/stub device,
  shows the panel, clicks each button, asserts the right
  `_dispatch_command` verb landed.
- Test that the run-state gate hides / disables write buttons when
  `engine.state == "running"`.

---

## Deferred: 2. Generic `DeviceCommandStep` method-step type

**Why it's deferred:** plan §11 / §16 P3 already shipped methods. Adding
a new step type is a schema change. We currently have no method that
needs to do "tare balance at t=120s" — it's hypothetical demand. Wait
for a real recipe to ask for it.

### Sketch when needed

Add to [src/capa/experiment/method.py](../src/capa/experiment/method.py)
in the `Step` discriminated union:

```python
class DeviceCommandStep(_StepBase):
    """Issue an arbitrary :class:`DeviceCommand` mid-method.

    The procedure's run authorization covers the dispatch — no manual
    confirm needed. Lighter than a full ``CustomStep`` because the
    dispatch table already validates the verb at the adapter.
    """

    kind: Literal["device_command"]
    device: str               # name in HardwareProfile.devices
    command_kind: str         # passes straight to DeviceCommand.kind
    payload: dict[str, Any] = Field(default_factory=dict)
    # No issued_by / authorization_id — set by the executor at run time
    # from the procedure's Authorization.
```

Wire one branch into `MethodExecutor` in
[src/capa/experiment/executor.py](../src/capa/experiment/executor.py)
that builds a `DeviceCommand` and calls `adapter.command(...)`.

**Out of scope until a real test calls for it.** When it lands, validate
in `Method.preflight` that `device` exists in the hardware profile and
that the adapter advertises the right capability for `command_kind`
(prevent a recipe from scheduling `internal_adjust` against an Alicat).

---

## Deferred: 3. "Apply config on open" for Alicat / Sartorius

**Why it's deferred:** EEPROM-wear concern. If `AlicatAdapterParams.default_gas`
gets applied with `save=True` on every `open()`, that's one EEPROM write per
power-cycle — fine for occasional use, brutal for a development loop where
the adapter cycles a hundred times. We need to think about this carefully
before shipping it as default behavior.

### What we'd want to add

```python
class AlicatAdapterParams(BaseModel):
    # ... existing fields ...
    default_gas: str | int | None = None
    """Apply at open(). Persists with save=False (session-only) by default."""
    default_gas_persist: bool = False
    """When True, calls gas(default_gas, save=True) — wears EEPROM."""

    default_units: dict[str, str] = Field(default_factory=dict)
    """Statistic-name → unit mapping. Applied at open() with engineering_units."""

    default_loop_variable: str | None = None
    """Applied at open() for controllers."""
```

```python
class SartoriusAdapterParams(BaseModel):
    # ... existing fields ...
    default_filter_mode: str | None = None
    default_auto_zero: str | None = None
    default_display_unit: str | None = None
    apply_defaults_persist: bool = False
    """When True, runs save_menu(confirm=True) after applying defaults.
    Default False — runtime-only, won't survive a power cycle."""
```

`open()` would, after `update_capabilities_from_device`, run a
`_apply_defaults()` helper that:

1. Builds an authorization handle (a synthetic "adapter-open" authorization
   id, or a pre-arm authorization).
2. Issues each non-None default through `_dispatch_command(...)` so the
   audit trail captures it like any other command.
3. Surfaces failures as `AdapterError`-with-context so a startup misconfig
   doesn't silently ignore the operator's intent.

### Open questions to resolve first

1. **Is "session only" actually safe?** If the device power-cycles
   independently of the adapter (e.g. someone bumps the Alicat's power),
   the adapter doesn't know to re-apply. Either:
   - Re-apply on every `open()` (current proposal), or
   - Detect the device's "fresh boot" status and re-apply only then.

2. **Audit trail location.** Open-time defaults should land in the run
   bundle's events table only when applied during an armed run, not
   when the adapter opens for `capa validate --strict` or `capa devices
   discover`. Need to scope the authorization handle accordingly.

3. **Sim-adapter parity.** `sim_alicat`'s default-state needs to honor
   the same params or recipes will diverge between sim and real runs.

4. **Plan-doc update.** Plan §5.4 currently says "adapter-specific knobs
   live under `DeviceConfig.params`" — the doc should grow a paragraph
   about apply-on-open semantics and EEPROM-wear policy.

### When to come back to this

When a recipe needs to start the day with the gas type set deterministically
or with non-default filter mode. Until then, the manual control panel +
`save_menu` covers it (the operator sets it once at the front panel, then
runs `save_menu` from the panel; the device remembers across power cycles).

---

## Completed: 4. CAPA's `Camera.command(...)` Protocol extension

**Status:** landed alongside the five capa-core control-surface
`CameraCapability` flags (the latter previously tracked under
`docs/handoff-capa-core-camera-capabilities.md`, now deleted).

What shipped in capa core:

- `CameraCapability` gained `NUC_TRIGGER`, `RADIOMETRIC_PARAMS`,
  `TEMPERATURE_RANGE_SELECT`, `AUTO_NUC_INTERVAL`, `REMOTE_PALETTE`
  ([src/capa/devices/camera/base.py](../src/capa/devices/camera/base.py)).
  Naming is bare nouns to match the existing convention in that file
  (the `HAS_*` prefix is the `DeviceAdapter` `Capability` style, not
  `CameraCapability`).
- The `Camera` Protocol now carries
  `async def command(self, cmd: DeviceCommand) -> CommandResult`
  reusing `DeviceCommand` / `CommandResult` from
  [src/capa/devices/adapter.py](../src/capa/devices/adapter.py). One
  command shape, one audit trail across every device write.
- [`WebcamAdapter`](../src/capa/devices/camera/webcam.py) implements
  `command()` with gate-then-reject (auth → not-open → no-verbs).
- [`FlirIrSim`](../src/capa/devices/sim/flir_ir_sim.py) advertises all
  five new flags and implements `command()` mirroring capa-flir's
  `_dispatch_command` verb table (`trigger_nuc`, `set_emissivity`,
  `set_temperature_range`, `set_atmospheric_temp`, `set_reflected_temp`,
  `set_distance_m`, `set_relative_humidity`,
  `set_atmospheric_transmission`, `set_auto_nuc_interval`,
  `set_remote_palette`, `set_preview_palette`). State is held in-memory
  on the sim instance; the recording-time guards on `trigger_nuc` and
  `set_temperature_range` match the real adapter.
- Tests at
  [tests/unit/test_camera_command.py](../tests/unit/test_camera_command.py)
  and
  [tests/unit/test_camera_capability_flags.py](../tests/unit/test_camera_capability_flags.py).

---

## Order of operations recommendation

§1 and §4 have shipped. Remaining work, in priority order:

1. **Apply-on-open config** (deferred §3) — only if a real workflow asks
   for it; the manual panel handles 90% of cases.
2. **`DeviceCommandStep`** (deferred §2) — only if a real recipe asks
   for it.

---

## Cross-references

- The `Authorization` system: [src/capa/experiment/authorization.py](../src/capa/experiment/authorization.py)
- Existing dispatch examples: [src/capa/devices/sartorius.py:455](../src/capa/devices/sartorius.py#L455),
  [src/capa/devices/alicat.py:430](../src/capa/devices/alicat.py#L430)
- Capability flags: [src/capa/devices/adapter.py:22](../src/capa/devices/adapter.py#L22)
- UI tab patterns: [src/capa/ui/tabs/](../src/capa/ui/tabs/)
- CAPA project memory note: 90% of CAPA experiments hold a single setpoint —
  don't over-engineer dynamic control surfaces.
