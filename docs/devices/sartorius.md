---
description: Recording specimen mass on a Sartorius balance in capa via sartoriuslib — xBPI and SBI protocols, tare/zero commands, cold-open race, stability flag.
---

# Sartorius balance

**Audience:** operators recording specimen mass through a Sartorius balance — typically the mass-loss channel for a CAPA run.
**Scope:** capa's [`sartoriuslib`](https://github.com/GraysonBellamy/sartoriuslib) adapter — `[devices.params]` fields, single-value-row emission, tare/zero command surface, the **cold-open race**, and how the stability flag propagates into channel samples.

---

## At a glance

| | |
|---|---|
| Adapter id | `capa.devices.sartorius` |
| Sibling library | [`sartoriuslib`](https://github.com/GraysonBellamy/sartoriuslib) |
| Real adapter | [`capa.devices.sartorius`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/sartorius.py) |
| Sim adapter | [`capa.devices.sim.sartorius_sim`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/sim/sartorius_sim.py) |
| Resource scheme | `serial:<port>` |
| Channel binding | [`sartorius_reading`](../configuration/channel-bindings.md) |
| Emission shape | `single_value_row` — one balance reading per tick |
| Default poll rate | 50 Hz descriptor default; the shipping config sits at 1–10 Hz |

## Supported hardware

Sartorius laboratory balances over xBPI or SBI. Protocol-level scope —
which models speak which protocol, the parameter / command surface, the
discovery probe matrix — is owned by
[sartoriuslib](https://github.com/GraysonBellamy/sartoriuslib).

## Configuration

```toml title="configs/hardware/sartorius_real.toml (excerpt)"
[[devices]]
name = "balance"
adapter = "capa.devices.sartorius"

[devices.params]
port = "COM4"
protocol = "xbpi"        # "xbpi" | "sbi" | "auto"
baudrate = 57600
src_sbn = 1
dst_sbn = 9
rate_hz = 50.0
auto_reconnect = true
snapshot_period_s = 30.0
```

[`SartoriusAdapterParams`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/sartorius.py)
is `extra="forbid"`.

| Key | Default | Notes |
|---|---|---|
| `port` | required | `COM4`, `/dev/ttyUSB0`. |
| `protocol` | `"xbpi"` | `"auto"` runs the conservative library detector; pin one in production. |
| `baudrate` | `9600` | Factory shipment. Many labs reconfigure to 57600 — set explicitly when changed on the balance. |
| `timeout_s` | `1.0` | Per-call command timeout. |
| `src_sbn` | `0x01` | Host xBPI bus address. |
| `dst_sbn` | `0x09` | Balance xBPI bus address (factory default `0x09`). |
| `rate_hz` | `2.0` (params); `50.0` (descriptor default) | Production sits at 1–10 Hz; the high default supports high-resolution mass-loss curves where the balance can keep up. |
| `auto_reconnect` | `true` | Transient `SartoriusConnectionError`s increment a degradation counter without ending the stream. Toggles `SUPPORTS_AUTO_RECONNECT`. |
| `snapshot_period_s` | `30.0` | Health-ping cadence. |
| `overflow` | `"block"` | `"block"` or `"drop_newest"`. |
| `adjust_timeout_s` | `120.0` | How long `internal_adjust` waits for the balance to finish before reporting a timeout. The rig's MSE1203S takes ~24.5 s; a noisy bench takes longer. A timeout stops the waiting, not the balance. |

## Channels and emission shape

Sartorius is the single-value family: one
[`sartoriuslib.streaming.Sample`](https://github.com/GraysonBellamy/sartoriuslib)
per tick carries one balance reading (`value`, `unit`, `sign`,
`stable`, `overload`, `underload`). The native row is preserved into
`device_records/sartorius.parquet` via
[`sartoriuslib.sinks.sample_to_row`](https://github.com/GraysonBellamy/sartoriuslib).

The only supported binding source is `sartorius_reading`:

```toml
[[channels]]
name = "balance.mass"
kind = "mass"
unit = "g"
plot_group = "mass"

[channels.source]
source = "sartorius_reading"
device = "balance"
field = "value"

[channels.calibration]
kind = "identity"
input_unit = "g"
output_unit = "g"
```

The balance's **stability flag** propagates into each derived
`ChannelSample` as `status="settling"` while `stable=False`, then
clears once the balance reports stable. Procedures that need a stable
window before ignition (e.g. "balance stable for ≥ 5 s") can read this
flag off the live channel; the preserved native row in
`device_records/sartorius.parquet` also carries the per-tick boolean
for post-hoc analysis.

Errored polls (sartoriuslib's recorder produces a `Sample` with
`reading=None` on transient transport errors) preserve the native row
with the error fields populated but do not emit a `ChannelSample` —
the sample's `status` does not get to lie about a value that didn't
arrive.

Readings taken while the balance runs an **internal calibration** are
not pan weights: off-scale for ~6 s, then a fake *stable* 1300 g for
~9 s, then 0 g. The adapter keeps their native rows but emits no
`ChannelSample` for them, so `balance.mass` shows a gap rather than a
plateau. Two signals mark them: a calibration capa itself is waiting
for, and — on a Cubis balance over xBPI — measurement-flags bit `0x04`,
which is clear on every reading taken during a calibration (sartoriuslib
`docs/protocol.md` §8.1). The flag also catches an isoCAL the balance
starts on its own. `events.sqlite` gets an `adjusting` event (severity
`warning`, `metadata.started_by` = `capa` or `balance`) where the gap
starts and an `adjusting_ended` event (`withheld_readings`,
`duration_s`) where it ends. The periodic snapshot carries `adjusting`.

See [channel bindings](../configuration/channel-bindings.md) for the
selector contract.

## Capability flags

| Flag | Notes |
|---|---|
| `HAS_TARE` | Tare command. |
| `HAS_ZERO` | Zero command. |
| `EMITS_STABILITY_FLAG` | The `stable` boolean propagates into channel samples. |
| `HAS_INTERNAL_CAL` | Internal-adjust command available. |
| `HAS_PARAMETER_CONFIG` | Diagnostic dock parameter writes. |
| `SUPPORTS_AUTO_RECONNECT` | Added when `auto_reconnect=True`. |

## Commands

| Typed call | `DeviceCommand.kind` | Notes |
|---|---|---|
| `tare()` | `"tare"` | Cheap (does not move mass) but still gated. |
| `zero()` | `"zero"` | Same. |
| `internal_adjust()` | `"internal_adjust"` | Runs the internal calibration weight and returns once the balance has finished (~25 s). See [Internal calibration](#internal-calibration). Between runs only. |
| `abort_adjustment()` | `"abort_adjustment"` | Stops a running internal calibration (xBPI `0x29`); the balance keeps its previous calibration. Accepted whether or not one is running. |
| `set_filter_mode(...)` | `"set_filter_mode"` | Reconfigures the balance's digital filter. |
| `set_display_unit(...)` | `"set_display_unit"` | Display-only; does not change the wire scale. |
| `set_auto_zero(...)` | `"set_auto_zero"` | Auto-zero on/off; saved with `save_menu`. |
| `set_isocal_mode(...)` | `"set_isocal_mode"` | Isocal scheduling. |
| `set_tare_behavior(...)` | `"set_tare_behavior"` | Whether a tare waits for a stable reading (p05): `"without stability"`, `"with stability"` or `"at stability"`. |
| — | `"set_app_filter"` | Application filter (p02): `"final reading"`, `"filling"`, `"reduced"` or `"off"`. |
| — | `"set_stability_range"` | How narrow a band the reading must stay in to count as stable (p03): `"max accuracy"`, `"very accurate"`, `"accurate"`, `"fast"`, `"very fast"` or `"max fast"`. |
| — | `"set_stability_delay"` | How long the reading must stay in band before it's stable (p04): `"none"`, `"short"`, `"average"` or `"long"`. |
| `save_menu()` / `reload_menu()` | matching kinds | Persist / discard parameter changes. |
| `read_last_cal_record()` | n/a | Read-only — not gated. The temperature at the last calibration and when it finished, on the balance's own clock; empty after a power-up until the next calibration. |
| `read_state_snapshot()` | n/a | Read-only — not gated. Filter mode, application filter, stability range and delay, auto-zero, display unit, tare behavior and the last calibration, for the manual-control card. |

sartoriuslib has no typed accessor for p02–p04, so the adapter reads and
writes them through the raw parameter table. Each `set_*` payload is
`{"mode": <label>}`; the label may also be the sartoriuslib enum name
(`"VERY_ACCURATE"`) or the wire value. An unknown mode is refused
before anything reaches the balance.

An experiment can declare these menu settings (all but the display
unit) under [`device_settings:`](../configuration/experiment-yaml.md)
to have them applied when the config loads.

All writes go through the [authorization
gate](../safety/authorization-gates.md) — `issued_by` plus either
`authorization_id` or `confirmed_by`.

## Discovery and handshake

`discover(ports=None, baudrates=None, timeout_s=0.5)` wraps
[`sartoriuslib.find_devices`](https://github.com/GraysonBellamy/sartoriuslib).
Per-probe rows (one per `port × baudrate` combination) are folded into
per-port summaries via `summarize_discovery`, so the operator sees one
row per port that responded — keyed by `port`, `protocol`, `baudrate`,
and `autoprint_active`.

`handshake(params)` is the per-device form `capa validate --strict`
runs: read-only `open` + `identify` + `close`.

See [Discovery](discovery.md) for the cross-cutting UX.

## Quirks

### Cold-open race

The most visible Sartorius quirk: **the first `open()` against a
balance is slow and intermittently fails the first read**. The library
calls this the "cold-open first-byte race" — frame underrun or 0-byte
read on the first transport read after power-on. `sartoriuslib.open_device`
absorbs this internally with a bounded retry loop, so capa does not
need its own retry — but the latency cost is real and shows up at
`WorkerPool` open time.

That's why capa's [runtime topology](../architecture/runtime-architecture.md)
opens the `WorkerPool` once per *config*, not once per run. The
cold-open cost is paid exactly once after the operator loads a config
or applies a reconnect; every subsequent run arms the already-open
worker instantly.

Observable symptoms when this matters:

- **Pulled power between runs.** Power-cycling the balance forces the
  next `open()` to re-pay the cold-open cost. If the operator pulled
  the balance's USB cable, the next Apply may take several seconds in
  the `CONNECTING` state on the connection strip.
- **Hot-reload of the hardware TOML.** Hot reload tears down and
  rebuilds the worker; it pays the cold-open cost. Cold reload (full
  restart) likewise.
- **Auto-reconnect after a transient.** Post-open transients surface
  as `SartoriusTransientTransportError` and are handled by the stream
  loop, *not* by re-opening — so the cold-open cost is not re-paid for
  in-flight reconnects.

If first-open seems unusually slow on your rig, check:

1. The cable is the rig's known-good cable (USB-RS232 dongles vary).
2. `dst_sbn` matches the balance's stored address (the default is
   `0x09`; some labs renumber).
3. `protocol` matches what the balance is configured for — `"auto"`
   adds a probe step that pays the cold-open cost twice.

### One-sample ±tare readings

The rig's MSE1203S Cubis, polled at 50 Hz with net-weight reads
(xBPI `0x1E`), returns about one reading in 50 000 off by exactly the
stored tare: `+tare` is the gross weight (tare not subtracted), `-tare`
is the tare subtracted twice. The frame's checksum is valid, so it is
the balance, not the line. Seen at about 4 per hour since 2026-10-07.

The value is recorded as sent, in `balance.mass` and
`device_records/sartorius.parquet`. Analysis should drop single samples
more than 1 g from both neighbours. The rig configs set
`plot_despike = true` on `balance.mass` so the live trace (and its
autorange) doesn't jump.

### Sim does not model the race

[`capa.devices.sim.sartorius_sim`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/sim/sartorius_sim.py)
opens instantly. It models the stability flag via the
`stable_after_s` parameter (the sim reports `stable=False` until
`t_mono ≥ stable_after_s`, then `True`) so procedure-side stability
gates can be exercised offline — but the latency cost of the cold-open
itself is not reproduced. Acceptance test runs that depend on the
cold-open timing must hit a real balance.

### Internal calibration

`internal_adjust()` starts the balance's motorized internal-weight
calibration and **waits for it to finish** — 24.3–24.5 s on the rig's
MSE1203S, longer on a noisy bench — through sartoriuslib's
`internal_adjust(wait=True)`. It must not be issued during a live run;
the adapter does not refuse, the manual card does. Treat it as a
between-runs maintenance command.

How it ends:

| Ending | Result |
|---|---|
| New calibration record | Accepted; the detail gives the time taken and the balance temperature. |
| Already running (an isoCAL the balance started) | Nothing is sent; capa waits for that one instead, and says so in the detail. |
| Stopped without a new record (`abort_adjustment`, the front panel, or the balance gave up) | `BalanceAdjustmentError`, `outcome="aborted"`. |
| Still running after `adjust_timeout_s` | `BalanceAdjustmentError`, `outcome="timeout"`. The balance is **not** stopped. |
| Acknowledged but never started | `BalanceAdjustmentError`, `outcome="not_started"`. |

`BalanceAdjustmentError` is an `AdapterError` carrying the sartoriuslib
error as `__cause__`.

Closing the adapter mid-calibration (the GUI closing, a config reload)
stops the waiting between status polls and sends nothing: the port stays
in sync and the balance finishes on its own. Only `abort_adjustment`
sends `0x29`.

While the calibration runs, the wait polls the balance status every
0.25 s (~4 ms of bus time each) alongside the weight stream; mass
samples are withheld as described under
[Channels and emission shape](#channels-and-emission-shape).

The calibration record's time is on the **balance's** clock, whatever
its front panel is set to — the rig's runs about 2 h ahead of UTC —
so capa shows it labelled as such and never compares it with PC time.

### Wire jitter is tracked

The adapter tracks min/max inter-sample wire spacing and the count of
intervals narrower than 75% of the configured `rate_hz`. These land in
the periodic snapshot and `events.sqlite` for post-hoc diagnosis of
"why did the mass curve look choppy" without you having to
instrument anything else.

### Sim equivalent

[`capa.devices.sim.sartorius_sim`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/sim/sartorius_sim.py)
takes one `mass_signal` spec (any of the [signal
kinds](simulators.md#signal-generators)), an optional `unit` /
`protocol`, a `tick_period_s`, and `stable_after_s`. Capabilities are
`HAS_TARE | HAS_ZERO | HAS_INTERNAL_CAL | HAS_PARAMETER_CONFIG`. Its
`internal_adjust` takes 24.4 s and honours `abort_adjustment`, so the
manual card's calibration flow can be tried offline; its readings stay
the mass signal throughout. See [Simulators](simulators.md).

## See also

- [Devices overview](overview.md) — adapter contract, capability enum, resource grouping.
- [Hardware TOML](../configuration/hardware-toml.md) — how `[[devices]]` blocks are parsed.
- [Channel bindings](../configuration/channel-bindings.md) — the `sartorius_reading` source schema.
- [Runtime architecture § config-lifetime WorkerPool](../architecture/runtime-architecture.md) — why the cold-open cost is paid once per config.
- [Authorization gates](../safety/authorization-gates.md) — the contract for any device write.
- [Discovery](discovery.md) — cross-cutting Setup-tab and CLI behavior.
- [sartoriuslib docs](https://github.com/GraysonBellamy/sartoriuslib/tree/main/docs).
