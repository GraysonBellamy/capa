---
description: capa simulator adapters for hardware-free development, CI, and training — the signal-generator schema, sim_*.toml configs, and what sims do and do not model.
---

# Simulators

**Audience:** developers, CI, anyone running capa without the rig in front of them — and operators training new students on a laptop.
**Scope:** the sim family under [`capa.devices.sim`](https://github.com/GraysonBellamy/capa/tree/main/src/capa/devices/sim) — why sims exist, the signal-generator schema, what's faithful to real hardware, and what is not.

---

## Why sim adapters exist

Sims unblock every UI iteration, test, and demo from hardware availability.
Each sim adapter mirrors the corresponding real adapter's `DeviceAdapter`
Protocol *exactly* — same capabilities, same emission shapes, same
`SourceRecord.shape` tag. Any procedure or sink that works against a sim
works against the real adapter, and the `device_records/<adapter>.parquet`
file inside a sim bundle is indistinguishable from one produced by the
real recorder.

What sims are *not*: a model of the physics. They emit deterministic,
operator-scripted signals. They will not catch a calibration mistake, a
loose thermocouple, or a flow controller that drifts under back-pressure.

## Naming convention

Hardware configs sit side-by-side in [`configs/hardware/`](https://github.com/GraysonBellamy/capa/tree/main/configs/hardware):

| Naming | Purpose |
|---|---|
| `sim_*.toml` (e.g. `sim_capa.toml`, `sim_minimal.toml`) | All-sim configs; no `[devices]` entry references a real adapter. Safe to run on any machine. |
| `*_real.toml` (e.g. `watlow_real.toml`, `webcam_real.toml`) | Single-family real configs; the others stay sim. Useful for bringing up one device at a time. |
| `capa_real_full.toml`, `capa_real_partial_*.toml` | Rig-shaped real configs. |

The pattern is conventional, not enforced — capa loads any TOML the user
points at. Sticking to it keeps the difference between "I am testing
without hardware" and "I am running the rig" visible in the file name.

## Signal generators

Sim adapters do not roll their own value generators; they call into
[`capa.devices.sim._signals`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/sim/_signals.py).
A *signal* is a stateless callable `(t_s) -> float` keyed off
[`RunClock`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/core/clock.py)
time — pure, deterministic, and testable.

Four kinds ship today:

| `kind` | Shape | Required keys | Notes |
|---|---|---|---|
| `constant` | `value` | `value` | Holds `value` forever. |
| `sine` | `offset + amplitude·sin(2π·f·t + phase)` | `amplitude`, `frequency_hz` | `offset` and `phase` default to `0`. |
| `ramp` | linear `start → end` over `duration_s`, then hold | `start`, `end`, `duration_s` | Clamps at both ends. |
| `step` | `before` until `at_s`, then `after` | `before`, `after`, `at_s` | Discrete jump at `at_s`. |

Sim configs in TOML carry the *spec*, not the callable:

```toml
[devices.params.signals."process_value/1"]
kind = "ramp"
start = 30.0
end = 600.0
duration_s = 120.0
```

The adapter's `from_params` classmethod materializes specs into
`SignalFn` instances at construction. Adding a new kind means
registering it in `_SIGNAL_KINDS` in
[`_signals.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/sim/_signals.py)
and writing the dataclass — no adapter-side changes are needed.

## Worked example: a CAPA-shaped sim run

The shipping [`sim_capa.toml`](https://github.com/GraysonBellamy/capa/blob/main/configs/hardware/sim_capa.toml)
covers every channel group the
[`capa_pyrolysis`](../configuration/capa-profile.md) profile requires.
Here is the heater portion:

```toml
[[devices]]
name = "heater"
adapter = "capa.devices.sim.watlow_sim"

[devices.params.signals."process_value/1"]
kind = "ramp"
start = 30.0
end = 600.0
duration_s = 0.2

[devices.params.signals."setpoint/1"]
kind = "constant"
value = 600.0
```

At `t = 0` the simulated PV reads 30 °C; at `t ≥ 0.2 s` it holds at
600 °C. The setpoint is constant at 600 °C from the first tick. This is
deliberately a fast ramp — the `sim_capa.toml` defaults are tuned for
the test suite. For UI demos, set `duration_s` to a more human-scale
60–300 s.

The signal keys are *adapter-specific* — Watlow uses
`"<parameter>/<instance>"`; Alicat uses underscored frame-field names
(`Mass_Flow`, `Abs_Press`); Sartorius takes a single `mass_signal`;
NI-DAQ uses display-name channels. The per-device pages list each
adapter's accepted key schema.

## Per-sim summary

Every real adapter has a sim peer. The sim mirrors the Protocol —
including the `Capability` flags it advertises — so the UI cannot tell
them apart by feature surface.

| Sim adapter | Mirrors | Emission shape | Signal-key schema | Use it to exercise |
|---|---|---|---|---|
| [`capa.devices.sim.watlow_sim`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/sim/watlow_sim.py) | [Watlow](watlow.md) | `long_row` | `{"<parameter>/<instance>": spec}` | Heater PV/SP rendering, setpoint command path, ramp UI. |
| [`capa.devices.sim.alicat_sim`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/sim/alicat_sim.py) | [Alicat](alicat.md) | `wide_row` | `{"<Frame_Field>": spec}` | Flow plots, gas-select UI, totalizer surfaces. |
| [`capa.devices.sim.sartorius_sim`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/sim/sartorius_sim.py) | [Sartorius](sartorius.md) | `single_value_row` | single `mass_signal` spec | Mass loss curves, tare/zero plumbing, stability flag propagation. |
| [`capa.devices.sim.nidaq_polled_sim`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/sim/nidaq_polled_sim.py) | [NI-DAQ polled](nidaq.md#polled-mode) | `wide_row` | `{"<channel>": spec}` | TC channels, polled binding kind. |
| [`capa.devices.sim.nidaq_block_sim`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/sim/nidaq_block_sim.py) | [NI-DAQ block](nidaq.md#hardware-clocked-block-mode) | `block` | `{"<channel>": spec}` | Block sidecar, kHz path, downsampled channels. |
| [`capa.devices.sim.flir_ir_sim`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/sim/flir_ir_sim.py) | [FLIR IR](cameras-flir.md) | `FrameReceipt` (camera path) | n/a — synthetic gradient frames | Camera-task wiring, manifest `cameras` block, frame-index parquet round-trip without an Atlas install. |

Each per-device page has a "Sim equivalent" subsection covering the
fields specific to its sim — what `tick_period_s` defaults to, which
`Capability` flags are advertised, and any sim-only escape hatches
(e.g. `wire_temperature_unit=None` on the Watlow sim suppresses the
drift check).

## Sim-only environment flags

Two environment variables exist purely to drive sim adapters into
states that don't occur naturally under a clean apply. They are
documentation-tooling and test-suite knobs; do not set them in normal
operator use.

| Variable | Effect |
|---|---|
| `CAPA_SIM_OPEN_DELAY_MS` | Sleep this many ms inside `open()`. Holds the [connection strip](../user-guide/the-setup-tab.md) in `CONNECTING` long enough to screenshot it. |
| `CAPA_SIM_OPEN_FAIL` | Raise `AdapterError` from `open()`. Forces the strip into `FAILED`. |

## What sims do not model

This is the operationally important list — read it before you trust a
green sim run as a green real run.

- **Serial-bus contention.** Sim adapters declare `sim:<name>`
  `resource_id`s, so each gets its own worker. Real Watlow/Alicat/
  Sartorius devices sharing a serial multi-drop bus share one worker
  that serializes I/O. A multi-device deadlock or starvation visible
  only under shared-bus serialization will not reproduce against sims.
- **The Sartorius cold-open race.** The real balance pays a multi-
  second cost on first `open()` while it waits for a stable reading
  window; the sim opens instantly. See [Sartorius §
  Cold-open race](sartorius.md#cold-open-race).
- **Hardware timing jitter.** Sims tick on an `anyio.sleep`. The real
  Watlow polls take ~50 ms per parameter; real DAQmx blocks land on
  the onboard clock; real cameras emit at their UVC/Atlas frame
  cadence. Sim ticks have no equivalent jitter.
- **Auto-reconnect paths.** `auto_reconnect=True` on real adapters
  swallows transient `*ConnectionError`s and retries on the next tick.
  Sims do not raise those errors, so the recovery code is not
  exercised by sim runs alone.
- **NI-DAQ block sidecar / TDMS passthrough.** The block sim emits a
  block-shaped `SourceRecord` so the bundle plumbing exercises, but
  the high-rate sidecar path is exercised at sim-friendly rates, not
  at the kHz rates real chassis produce.
- **Camera SDK quirks.** `flir_ir_sim` writes a deterministic
  capa-private "fake-csq" file with its own magic header. It is
  *not* a real FLIR FFF/`.csq` — a downstream tool that parses real
  Atlas output will reject it (intentionally — capa ships its own
  extractor for the sim format alongside the real one).
- **UVC/dshow probe results.** The webcam sim (where used) does not
  enumerate real OS camera APIs; the dshow `list_options` resolution
  probe only runs against real devices.

When a behavior is sim-only, the engine flags it: snapshot pings
declare the `family` and the `adapter` id, so a bundle's
`manifest.json` carries the `sim` family label and any post-hoc
analysis can detect that the run was synthetic.

## See also

- [Devices overview](overview.md) — adapter contract and emission shapes.
- [Hardware TOML](../configuration/hardware-toml.md) — how the `[[devices]]` block is parsed.
- [Channel bindings](../configuration/channel-bindings.md) — which binding kinds consume which sim emissions.
- The shipping configs in [`configs/hardware/`](https://github.com/GraysonBellamy/capa/tree/main/configs/hardware) — every example referenced above is a real file you can run.
