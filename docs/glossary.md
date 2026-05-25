# Glossary

Plain-English definitions of capa's core concepts. Each entry is short on
purpose — the operator should be able to read the whole page once and walk
away with the mental model.

## Bundle

A self-contained on-disk record of a single run. Every bundle holds the
config, method, calibration snapshot, equipment manifest, events, sample
data, and any captured video. Five years from now, you should be able to
open a bundle and know exactly what was measured and how.

## Method

A scripted sequence of steps the rig executes during a run: hold a
setpoint, ramp to a target, prompt the operator, run a custom routine.
Methods are stored as `.method.toml` files and can be loaded into the
Method tab. A run with no method loaded is a **free run** — see below.

## Procedure

The class of experiment being performed (e.g. *CAPA cone calorimeter*,
*heat-flux gauge calibration*, *paint emissivity ramp*). Procedures are
plugins; each one declares what configuration it needs and how to
interpret the bundle.

## Profile (domain profile)

A *domain profile* layers scientific metadata and preflight checks on
top of the generic experiment recipe — atmosphere composition, specimen
holder geometry, heat-flux setpoint, leak-check provenance. Profile is
**orthogonal to procedure**: the same `capa.builtin.recipe_runner`
procedure can drive a CAPA-pyrolysis run or a cone-calorimeter run
depending on which profile is attached. The CAPA-pyrolysis profile
(`capa.profiles.capa_pyrolysis`) is the project default; the
cone-calorimeter variant lives in `capa.profiles.cone_calorimeter`. The
profile section in the UI only appears when `domain_profile.id` is set.

## Channel binding

How a channel gets its value. A channel like `heater_pv` doesn't store
data itself — it **reads from** a specific device parameter (e.g. a
Watlow controller's `PV` register). Six binding kinds exist today,
each matching a device family's emission shape: `watlow_parameter`,
`alicat_frame_field`, `sartorius_reading`, `nidaq_reading_field`,
`nidaq_block_channel`, or `derived` (computed from other channels).
See [Channel bindings](configuration/channel-bindings.md) for the
field-by-field reference.

## NI cDAQ inputs vs capa channels

NI-DAQ has its own two-layer model that mirrors the device / channel
split everywhere else in capa, but more visibly. **NI cDAQ inputs** are
physical-layer rows in the NI device's params (which `cDAQ1Mod1/ai0`
pin, what thermocouple type, what NI ADC mode); they configure the NI
task that produces sample readings. **Capa channels** are the stable
scientific identifiers — `TC_sample_top`, with units, plot group,
calibration — that show up in `scalars.parquet` and on the plot. One
NI cDAQ input can feed multiple capa channels (one raw, one calibrated),
and a capa channel can switch its source between NI and another device
without changing its name. Edit NI inputs from the Devices section's
"NI cDAQ inputs" table; edit capa channels from the Channels section.
The join is by name: the capa channel's `field` must equal an NI input's
display name on the same `(device, task)`.

## Free run

A run with no method loaded. Recording starts when you press Start in
the Run tab. The heater holds whatever setpoint it had when the run
began; nothing in capa drives it. About 90% of CAPA experiments are
free runs — the dynamic-program case (ramps, multi-step) is the
minority.

## CAPA group

A logical grouping label attached to a channel via its metadata. CAPA
groups let the Profile section know which channels belong to which
physical role (`gas_inlet`, `heater_loop`, `specimen_load`) without
the operator having to wire up mappings by hand.

## Apply & Connect

The Setup-tab action that validates the current draft, opens hardware
connections to every device, and starts background acquisition. It is
the bridge between *editing a config* and *the rig being live*. Once
applied, the draft and the rig match — edits afterwards mark the draft
as having unsaved changes until you Apply again.

## Verify connection

A read-only handshake against every device declared in the draft. It
tells you whether the rig can be reached without actually opening
long-lived connections. Useful when troubleshooting a new device.

## Scan for devices

Walks each device family's bus / network / USB tree and reports what
hardware is physically reachable. Lets you add discovered devices to
the draft with one click instead of typing in addresses by hand.

## Worker

One thread plus one asyncio loop that owns a single hardware
**resource** — a serial port, a VISA bus, an NI-DAQ chassis. Each
worker hosts the device adapters that share that resource and runs them
through a small state machine (`CLOSED → IDLE → ARMED → SAMPLING →
DRAINING → IDLE`). The [conductor](#conductor) drives every worker via
async lifecycle calls that cross the thread seam through a
[bridge](#bridge). Why one thread per resource: a hung adapter on one
worker cannot freeze the UI, the conductor, or any other worker. See
[`worker.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/worker.py).

## Conductor

The per-run coordinator. The conductor lives on its own thread and its
own asyncio loop — separate from the UI and from the [worker](#worker)
threads — and owns the lifecycle of one run from preflight to sealed
bundle. It arms workers, opens drains, runs the procedure, watches the
[saturation deadline](#saturation-deadline), and finalizes the bundle.
A new conductor is constructed for every run; the pool of workers
underneath outlives it. See
[`conductor.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/conductor.py).

## Bridge

A thread-safe bounded channel that connects two asyncio loops — almost
always a [worker](#worker) loop on one side and the
[conductor](#conductor) (or the qasync UI loop) on the other. Every
sample, frame, and event that leaves a worker crosses a bridge. The
bridge's `blocked_since_ms` reading — how long a producer has been
parked waiting for outbound space — is the canonical
[saturation](#saturation-deadline) signal and the source of the `sat`
status-bar pill. See
[`bridge.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/bridge.py).

## Bundle outcome

How a run actually ended, written to the bundle's `run_status` at
finalize. Distinct from where the conductor is in its lifecycle. Four
values, all defined on `RunOutcome` in
[`conductor.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/conductor.py#L88-L109):

- **`completed`** — the procedure ran to natural completion.
- **`aborted`** — the operator (or supervising code) called stop before
  completion.
- **`crashed`** — an unhandled exception escaped from the procedure,
  drain task, or pool. The bundle is still sealed; the cause is in the
  event log.
- **`crashed_but_sealed`** — the [saturation deadline](#saturation-deadline)
  tripped. The conductor disarmed every worker, ran safe-shutdown, and
  sealed the bundle anyway so the run isn't lost.

## Saturation deadline

The maximum time any single saturation signal — a [bridge's](#bridge)
`blocked_since_ms` or the writer thread's stalled inbox — may stay
tripped before the conductor escalates and seals the run as
`crashed_but_sealed`. Default is 10 seconds, set by
`DEFAULT_SATURATION_DEADLINE_S` in
[`saturation.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/saturation.py#L44).
Conservative on purpose: 10 s catches a genuinely wedged disk or hung
adapter while ignoring tens-of-ms hiccups. See the
[`sat` pill in the status bar guide](user-guide/status-bar-guide.md#sat-saturation)
for what the warn / fail thresholds look like in practice.

## Write-blocked state

A run lifecycle state in which the [manual control](user-guide/manual-controls.md)
dock refuses to dispatch commands because the [conductor](#conductor)
does not own the workers cleanly. There are four: `PREPARING` (pool
opening), `DRAINING` (workers disarming), `FINALIZING` (bundle being
sealed), and `RUNNING` is the *opposite* — writes during `RUNNING` are
allowed and recorded. The four blocked states show as warn-colored
badges in the run-tab header; cards in the manual dock disable their
buttons.

## RunUiState

The UI-facing enum that drives the [Run-tab state badge](user-guide/the-run-tab.md#the-state-badge):
`IDLE`, `PREPARING`, `RUNNING`, `DRAINING`, `FINALIZING`, `SEALED`,
`FAILED`. Mirrors the runtime's `ConductorState` plus an `IDLE`
sentinel for the between-runs / no-conductor case. Consumers (status
bar, run-tab badge, manual-card gate) read this enum rather than
reaching into the runtime directly. See
[`state.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/ui/state.py).

## RunStatus

One of `running` / `completed` / `aborted` / `crashed`. Recorded into
the bundle's `manifest.json` as `run_status`, this answers *what did
the run actually do?* Distinct from `bundle_status`, which answers
*what shape is the bundle in?*. See [aborting safely](user-guide/aborting-safely.md#what-ends-up-in-the-bundle)
for the combination matrix.

## BundleStatus

One of `open` / `finalizing` / `finalized_unverified` / `sealed` /
`verification_failed`. The bundle's own lifecycle state, separate from
the run's. Only `sealed` is safe to archive without further
inspection. See [aborting safely](user-guide/aborting-safely.md#bundle_status-what-shape-the-bundle-is-in).

## IntegrityStatus

One of `unknown` / `ok` / `mismatch` / `partial`. Tells you whether the
bundle's data hashes match what was sealed. Always confirm `ok` before
trusting data from an aborted or crashed run.

## scalars.parquet

The bundle's normalized channel-sample table. One row per
`(channel, t_mono_ns)`, long format. This is the file most analyses
read; the per-adapter `device_records/*.parquet` files are the raw
safety net for fields the channel binding didn't promote. Schema in
[Channel samples parquet](bundles/parquet-channel-samples.md).

## RecordShape

The four native shapes an adapter can emit, mirrored into the manifest's
`data_shape.device_records[].layout` so a reader knows what to expect
before opening the file: **`wide_row`** (one row per tick, columns =
readings — Alicat, NI-DAQ polled), **`long_row`** (rows of
`(device, parameter, instance, value)` — Watlow), **`single_value_row`**
(one value per record — Sartorius), **`block`** (reserved for
hardware-clocked bursts; today's NI-DAQ block mode still emits
`wide_row` metadata records — see
[Device records parquet](bundles/parquet-device-records.md)).

## frames.parquet sidecar

A per-camera Arrow→Parquet file at `video/<name>.frames.parquet`
mapping `frame_idx` to `t_mono_ns`. Without it, container-level PTS
timestamps are too coarse to align video frames with channel samples
at ms granularity. Always lives inside the bundle even when the
container `<name>.mkv` is written to an external `output_root`. Schema
in [Video](bundles/video.md).

## In-flight Arrow IPC

The v2 transit format: every Parquet-bound sink writes an Arrow IPC
stream to a `*.in-flight.arrows` file during the run, which finalize
rewrites into the final Parquet with sort-by-`t_mono_ns`, large row
groups, and zstd compression. Optimised for durable append, not for
read — analysts should never touch the `.in-flight.arrows` files
directly. The v1 → v2 schema bump was made specifically to capture
this transit-format change; see
[Bundle versioning](bundles/bundle-versioning.md).

## crashed_but_sealed

The combination `run_status: crashed` + `bundle_status: sealed` (with
`integrity.status: ok`). A recoverable abnormal termination: the
conductor died but recovery managed to finalize. Data is trustworthy
through the last flushed block. Specifically also the
`RunOutcome.crashed_but_sealed` value the conductor sets when the
[saturation deadline](#saturation-deadline) tripped — see
[Bundle outcome](#bundle-outcome).

## grace_s (shutdown grace)

The maximum time the conductor waits for a worker to disarm cleanly
before hard-stopping it and marking the run degraded. Default is
**5 seconds** (`DEFAULT_SHUTDOWN_GRACE_S` in
[`conductor.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/conductor.py#L246)).
The `Draining…` badge in the run tab is bounded by this value.

## Hot vs cold reload

Informal shorthand for what happens on Apply & Connect. Every Apply
rebuilds the worker pool — there is no separate "hot reload" code path
today — but the practical speed depends on whether the new config's
device list matches the old one. **Hot:** same hardware, parameter-only
edit, close-then-reopen runs in a second or two. **Cold:** new device
added or removed, plus enumeration / NI-DAQ chassis open cost. The
distinction is only in operator-visible latency. See
[daily workflow](getting-started/daily-workflow.md#what-apply-connect-actually-does).

## Plugin lockfile

A `plugins.lock` TOML file that pins every trusted procedure plugin's id,
package, version, entry point, and distribution hash. capa looks for
the lockfile at, in order: the project root `./plugins.lock`,
`$XDG_CONFIG_HOME/capa/plugins.lock`, or
`~/.config/capa/plugins.lock`. In production mode, run startup refuses any
procedure plugin whose hash or version differs from the lock — the operator must
run `capa plugins trust ...` to update the entry first. The lockfile
snapshot is copied into every bundle and mirrored into
`manifest.json.plugins`, so a sealed bundle records the active trust set.
It is not proof that every listed plugin was used by that run. See
[`plugins_lock.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/core/plugins_lock.py).
