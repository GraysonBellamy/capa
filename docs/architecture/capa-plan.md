---
description: capa architecture plan — the product surface for the controlled-atmosphere pyrolysis app, covering channels, run bundles, procedures, safety, and storage.
---

# capa — Architecture Plan

**Scope:** the product surface of capa — what a channel is, what the run bundle contains, how procedures and methods compose, how the safety layer and storage layer work. The runtime topology that hosts all of this (per-resource workers coordinated by a per-run conductor over a long-lived worker pool) is documented separately in [`runtime-architecture.md`](runtime-architecture.md); this document references it but does not duplicate it.

**Target:** Python 3.13 control & data-acquisition application for a custom controlled-atmosphere pyrolysis lab instrument.

> **What this doc is, and isn't.** This is the *design plan* — the
> shape of the product surface, written ahead of (and updated alongside)
> implementation. It is not always 1:1 with what ships today.
> Sections that describe *future* work — most prominently the
> rule-based `SafetyMonitor` (§9.2) and the external event ingest
> endpoint (§11.1) — are tagged "planned" inline. For the canonical
> view of *what runs today*, see [`runtime-architecture.md`](runtime-architecture.md).

---

## 1. Overview

`capa` is a single-rig, single-operator control and DAQ application written in Python with a PySide6 GUI. It drives a heterogeneous instrument set (NI-DAQ, Watlow temperature controllers, Alicat mass-flow controllers, Sartorius balances, USB and IR thermal cameras) through async-first device libraries (`nidaqlib`, `watlowlib`, `alicatlib`, `sartoriuslib`), records every run as a self-contained on-disk bundle, and supports research workflows (calibrations, custom routines) as first-class plugins rather than one-off UI hacks.

Sample rates are modest (3–60 Hz per device); the dominant I/O concern is video, not analog throughput. The architecture is therefore optimized for **reproducibility, extensibility, and operational clarity** over raw bandwidth — with explicit escape hatches for the cases that need it (high-rate NI DAQ via TDMS, native-format radiometric IR via the FLIR Atlas SDK).

### 1.1 Goals

- **Configurable rigs.** Channels, devices, calibrations, methods are all declarative (YAML / TOML / JSON) and version-controllable.
- **Reproducible runs.** Every run produces a bundle that contains everything needed to interpret the data five years later — config, method, calibration snapshot, equipment, events, scalars, video.
- **Domain-standard capture.** CAPA pyrolysis runs capture the metadata needed to interpret a controlled-atmosphere temperature program: temperature profile, atmosphere (carrier and optional reactive gas), specimen form/mass/holder, and leak-check timestamp.
- **Extensible procedures.** New routines (heat-flux gauge calibration, paint emissivity ramp, future profile variants) are plugin packages, not core changes.
- **Operable.** A trained operator can launch a known recipe, monitor it, abort safely, and review the result without touching code.
- **Headless-capable.** The engine runs without the GUI for testing, automation, and CI.
- **Honest about safety.** Every device-write goes through an explicit run authorization or manual confirmation gate; safety monitoring is its own subsystem with its own state, not a check buried in an acquisition callback.
- **OS-flexible.** Primary target Windows; Linux is aspirational (kept achievable through portable choices, but not actively tested).

### 1.2 Non-goals

- **Hard real-time control.** All closed-loop control (PID, ramp profiles) lives on the instruments themselves. `capa` is a supervisor: it issues setpoints and watches.
- **Distributed operation.** One rig, one PC, one user at a time.
- **Hot channel reconfiguration mid-run.** Channel set is frozen between Arm and Stop.
- **Built-in data analysis.** A separate `capa-analyze` (or notebook workflow) consumes the run bundles. Out of scope here.
- **Multi-tenant networked access.** UI is local to the rig PC.

### 1.3 Operating envelope

These are the assumed working conditions; the architecture is sized for them, not against them.

- **Sample rates.** 3–60 Hz per numeric device, mixed across the rig.
- **Library data shape.** The existing device libraries do not all emit the same row shape: `alicatlib` and polled `nidaqlib` emit wide rows, `watlowlib` emits one row per parameter, `sartoriuslib` emits one value row with rich provenance/error fields, and hardware-clocked `nidaqlib` emits rectangular `DaqBlock`s. Capa preserves those native records and derives normalized channel samples from them.
- **Control latency budget.** 10–100 ms is acceptable for software-issued setpoints. Anything tighter belongs on the instrument firmware.
- **Attended runs.** An operator is present; the application does not need to recover unattended from arbitrary failures, but it does need to fail loudly and finalize the bundle cleanly.
- **Camera count.** Two cameras at the current rig (one visible, one FLIR IR). The architecture is N-camera; the count is configuration.
- **Run duration.** Minutes to a few hours. IR `.csq` files commonly land in the 1–20 GB range; total bundle size occasionally crosses 50 GB.

---

## 2. Tech stack (pinned)

| Concern              | Choice                                  | Rationale                                                                 |
|----------------------|-----------------------------------------|---------------------------------------------------------------------------|
| Language             | Python 3.12 / 3.13                      | Matches device libraries; modern typing, `match`, `ExceptionGroup`; stay below Python 3.14 until `qasync` supports it |
| Async runtime        | AnyIO (asyncio backend by default)      | Every device library is built on AnyIO; consistent task-group semantics   |
| GUI                  | PySide6 + `qasync`                      | Mature dock framework; well-trodden asyncio bridge                        |
| Plotting             | PyQtGraph                               | Only library that handles 60-Hz live updates without sweat                |
| Data validation      | Pydantic v2                             | Auto-validation; auto-form generation; YAML/TOML-friendly                 |
| Units                | `pint` + UCUM-aligned vocabulary        | Dimensional analysis at config-load; catches kPa/psi-class errors before a run |
| Storage — channel samples | Parquet (per-run, normalized long table) | Mixed-rate analysis-ready stream: one row per resolved channel sample, queryable across runs with DuckDB/Polars/Arrow |
| Storage — device records | Parquet sidecars by device family    | Preserves library-native emitted rows/blocks without flattening away diagnostic fields |
| Storage — events     | SQLite (per-run)                        | Transactional, crash-safe, queryable                                      |
| Storage — IR thermal | FLIR `.seq` / `.csq` (native, in-bundle)| Atlas SDK records directly; preserves vendor calibration metadata; large (1–20+ GB per run) but transcoding is wasteful |
| Storage — visible    | MKV/MP4 (H.264 via PyAV)                | Tiny, broadly compatible, no radiometric data to preserve                 |
| Camera SDK (IR)      | FLIR Atlas Multiplatform C SDK          | Owns the radiometric `.csq` writer; the only path that preserves vendor calibration |
| Atlas Python binding | CFFI (ABI mode)                         | Pure-C surface, no compile step at install, no FLIR headers vendored      |
| Storage — manifest   | JSON / TOML                             | Human-readable, git-diffable                                              |
| Logging              | `structlog` (JSON to `run.log`)         | Run-id correlated structured logs, captured into the bundle               |
| High-rate DAQ escape | TDMS via `nidaqlib.TdmsLogging`         | Driver-side logging if a future task exceeds capa's normal 3–60 Hz class  |
| Run catalog          | Single SQLite file (`runs.sqlite`)      | Cross-run index; zero-admin                                               |
| Plugin discovery     | `importlib.metadata` entry_points + dev-folder fallback | Pip-install for stable plugins; drop-in folder for development          |
| Packaging            | uv + `pyproject.toml` (committed `uv.lock`) | Lockfile is provenance for the bundle                                  |
| Lint / format        | `ruff`                                  | One tool, fast, deterministic                                             |
| Type checking        | `mypy --strict`                         | Pydantic v2 + strict typing pays for itself across a long-lived codebase  |
| GUI testing          | `pytest-qt` with `qasync` integration   | Standard for PySide6 + asyncio test harnesses                             |
| Versioning           | `setuptools-scm` (git tag → `__version__`) | App version embedded in every bundle without manual bumps              |

**On the choice of Parquet+SQLite over HDF5:** for a long-lived multi-researcher artifact, the Parquet ecosystem (DuckDB, Polars, Arrow, Spark, Julia, R) is materially richer than HDF5's, the file-per-concern layout is more crash-tolerant, and FLIR's native radiometric format preserves vendor metadata that float32 transcoding loses. HDF5 remains an option later for archival merge — see §16.

---

## 3. Architecture layers

```
┌────────────────────────────────────────────────────────────────┐
│ UI Layer  (PySide6, main thread, qasync event loop)            │
│   Tabs: Setup · Method · Run · Review                          │
│   Docks: Numerics · Events · Notes · Camera previews           │
│   ManualClient routes commands to Conductor (during a run)     │
│   or directly to the WorkerPool (between runs). Owns no I/O.   │
└──────────────────────────▲─────────────────────────────────────┘
                           │ commands / state queries
                           │ subscribes to ConductorDataBus + AlarmBus
┌──────────────────────────┴─────────────────────────────────────┐
│ Run coordinator  (per-run Conductor, own thread + asyncio loop)│
│   ┌──────────────────────────────────────────────────────────┐ │
│   │ Procedure (state machine, plugin)                        │ │
│   │ MethodExecutor (Hold/Ramp/Step/Wait/Prompt — reusable)   │ │
│   │ SafetyMonitor (independent task; can call safe_shutdown) │ │
│   │ ChannelRegistry (cross-device, name-keyed surface)       │ │
│   │ ConductorDataBus (authoritative in-process pub/sub)      │ │
│   │ Per-worker drain → writer thread + UI bridge             │ │
│   └──────────────────────────────────────────────────────────┘ │
└──────────────────────────▲─────────────────────────────────────┘
                           │ ThreadBridge per worker (BLOCK +
                           │ saturation deadline at Conductor side)
┌──────────────────────────┴─────────────────────────────────────┐
│ WorkerPool  (config-lived; survives runs)                      │
│   One Worker per resource (serial port / DAQmx chassis /       │
│   camera handle). Each Worker = own thread + own asyncio loop  │
│   + adapter instances + IDLE↔ARMED↔SAMPLING↔DRAINING state.    │
│   nidaq · watlow · alicat · sartorius · webcam · flir_ir       │
│   Sim adapters mirror the same Protocol for tests + UI dev.    │
└──────────────────────────▲─────────────────────────────────────┘
                           │
┌──────────────────────────┴─────────────────────────────────────┐
│ Storage Layer  (orthogonal to devices)                         │
│   RunBundleWriter: parquet · sqlite · video · sidecars         │
│   RunCatalog: cross-run SQLite index                           │
└────────────────────────────────────────────────────────────────┘
```

Three nested lifetimes shape the system:

1. **Config lifetime — `WorkerPool`.** Opened when a hardware config loads. Builds one `Worker` per resource, opens every adapter, and stays open across many runs. Closed only on config-reload or app-quit. Operators issue manual commands through the pool when no run is active; expensive open-once costs (e.g. the Sartorius cold-open race) are paid once per config load.
2. **Run lifetime — `Conductor`.** Constructed when a run starts. Owns run-only state (clock, writer, procedure, drain tasks, heartbeat, saturation monitor). Arms the existing workers, drives sampling, disarms on stop. Workers stay open for the next run.
3. **Command lifetime — single dispatch.** A UI command crosses the thread seam once or twice and resolves.

The UI never directly owns a balance, controller, DAQ session, or camera. It only talks to the run coordinator (during a run) or the pool (between runs). The coordinator runs identically with or without the UI — see §14.

---

## 4. Project layout

```
capa/
├── pyproject.toml
├── src/capa/
│   ├── cli/                         # `capa` entry point — `cli/main.py` dispatches to:
│   │                                # run · gui · validate · finalize · catalog · plugins ·
│   │                                # devices · config · hardware · method · profile
│   ├── core/
│   │   ├── clock.py                 # RunClock — monotonic + UTC anchor
│   │   ├── databus.py               # in-process pub/sub
│   │   ├── ringbuffer.py            # per-channel decimating ring
│   │   ├── backpressure.py          # BackpressurePolicy enum + queue helpers
│   │   ├── logging.py               # structlog config + run-id context binder
│   │   ├── metrics.py               # queue-depth + writer-lag histograms
│   │   ├── provenance.py            # collect env/version/lockfile metadata
│   │   ├── units.py                 # pint registry + UCUM validation
│   │   ├── plugins_runtime.py       # entry-point discovery + trust enforcement
│   │   ├── plugins_lock.py          # plugins.lock parser + audit journal
│   │   └── errors.py
│   ├── channels/
│   │   ├── spec.py                  # ChannelSpec (Pydantic)
│   │   ├── registry.py              # name -> (adapter, channel-on-adapter, calibration, sinks)
│   │   ├── calibration.py           # Identity / Polynomial / Lookup / Piecewise / Custom
│   │   └── derived.py               # derived channels and transforms
│   ├── devices/
│   │   ├── adapter.py               # DeviceAdapter Protocol + Capability enum
│   │   ├── nidaq.py                 # wraps nidaqlib.DaqManager
│   │   ├── nidaq_join.py            # multi-task join helpers
│   │   ├── nidaq_channels.py
│   │   ├── watlow.py                # wraps WatlowManager
│   │   ├── alicat.py                # wraps AlicatManager
│   │   ├── sartorius.py             # wraps SartoriusManager
│   │   ├── discovery.py             # `capa devices discover`
│   │   ├── records.py · resolved.py · materialize.py · runtime_state.py
│   │   ├── camera/
│   │   │   ├── base.py              # Camera Protocol + shared types
│   │   │   ├── metadata.py
│   │   │   └── webcam/              # PyAV USB capture (visible)
│   │   │       ├── adapter.py · descriptor.py · encoding.py · probe.py · constants.py
│   │   │   # The FLIR IR adapter ships in a separate package, `capa-flir`,
│   │   │   # registered through the `capa.cameras` entry point. capa core
│   │   │   # contains zero FLIR SDK material — see §12 for the rationale.
│   │   └── sim/                     # simulated adapters mirroring each Protocol
│   │       └── flir_ir_sim.py       # in-process .csq writer fixture (fake header + frames)
│   ├── experiment/
│   │   ├── config.py                # ExperimentConfig (Pydantic)
│   │   ├── method.py                # Step types + Method
│   │   ├── executor.py              # MethodExecutor — reusable, callable from any procedure
│   │   ├── authorization.py         # arm/start approvals + manual-command confirmations
│   │   ├── profiles/
│   │   │   ├── base.py              # DomainProfile Protocol
│   │   │   ├── runtime.py           # static/dynamic preflight registry
│   │   │   └── capa_pyrolysis.py    # controlled-atmosphere pyrolysis metadata + preflight
│   │   └── procedures/
│   │       ├── base.py              # Procedure Protocol + ProcedureContext
│   │       └── builtin/
│   │           ├── recipe_runner.py # thin: delegates to MethodExecutor
│   │           ├── free_run.py      # record-only, no method
│   │           ├── batch.py         # run a recipe N times with auto-increment sample IDs
│   │           └── heat_flux_tune/  # heater→flux tuning routine (controller + signals + …)
│   ├── runtime/                     # per-resource worker runtime — see runtime-architecture.md
│   │   ├── worker.py · pool.py · conductor.py · dispatch.py · bridge.py
│   │   ├── headless.py · build.py · session.py · runner.py · recording.py
│   │   ├── saturation.py · heartbeat.py · shutdown.py · lifecycle.py
│   │   └── camera_adapter.py · preview.py · …
│   ├── calibration/
│   │   └── tune_artifact.py         # calibration artifact emit/load + uncertainty
│   ├── storage/
│   │   ├── bundle.py                # RunBundleWriter — opens / finalizes a run dir
│   │   ├── channel_samples_sink.py  # scalars.parquet normalized long table
│   │   ├── device_records_sink.py   # device_records/*.parquet native sidecars
│   │   ├── events_sink.py           # events.sqlite
│   │   ├── status_sink.py           # status.sqlite (device snapshots)
│   │   ├── video_sink.py            # frame-index Arrow-IPC → frames.parquet
│   │   ├── log_sink.py              # run.log (structlog JSON, captured into bundle)
│   │   ├── writer_thread.py         # per-run writer task ownership
│   │   ├── finalize.py              # idempotent finalize-in-place (recover orphaned bundles)
│   │   ├── integrity.py             # sha256 over bundle artifacts
│   │   ├── manifest.py              # manifest.json read/write + schema
│   │   ├── catalog.py               # runs.sqlite (cross-run index)
│   │   └── schema.py                # bundle_schema_version + migration registry
│   └── ui/
│       ├── main_window.py
│       ├── docks/                   # numerics, events, notes, camera_preview, manual_control
│       ├── tabs/                    # setup, method, run, review
│       ├── plots/                   # PyQtGraph multi-pane
│       ├── forms/                   # auto-form from Pydantic models
│       ├── manual/                  # manual cards (Watlow, Alicat, …)
│       ├── statusbar.py             # run state, dropped samples, writer lag, disk free
│       └── theme.py
├── plugins/                         # local-dev procedure plugins (hot-load)
├── configs/
│   ├── hardware/                    # one TOML per rig setup
│   ├── methods/                     # segmented profiles
│   ├── experiments/                 # full ExperimentConfig recipes
│   └── calibrations/                # calibration sets
├── runs/                            # default run-bundle root
└── tests/
    ├── unit/
    ├── integration/
    └── hardware/                    # gated behind env var; otherwise skipped
```

The FLIR IR adapter lives in a sibling repository, `capa-flir/`, with its own pyproject and CFFI binding. It registers on `capa.cameras` and is consumed by capa core like any other plugin.

---

## 5. Core data model

### 5.1 `ChannelSpec` — the universal binding unit

UI binds to channels, sinks key off channels, calibrations attach to channels, plotting groups by channels. Devices come and go; channels are the stable contract.

```python
class ChannelSpec(BaseModel):
    name: str                                # "TC_top_1", "HRR_raw", "MFC_air.flow"
    kind: ChannelKind                        # analog_in | tc | ao | do | counter |
                                             # process_var | setpoint | mass | mfc_flow |
                                             # video_visible | video_ir | derived
    source: SourceBinding                    # tagged union: NIDAQAI / WatlowParameter /
                                             # AlicatFrameField / SartoriusReading / ...
    unit: UnitStr                            # validated against pint registry; UCUM-aligned
                                             # raw unit (pre-calibration). e.g. "V", "K", "kPa"
    derived_unit: UnitStr | None = None      # post-calibration unit; pint-validated
    keep_raw: bool = False                   # also write pre-calibration value to scalars
    uncertainty: UncertaintySpec | None = None  # propagated to derived channels (see §5.5)
    sample_rate_hz: float | None = None      # None for event-driven (cameras)
    calibration: Calibration = Identity()
    plot_group: str | None = None            # "temperatures", "flows", "mass"
    alarms: list[AlarmBand] = []
    sinks: list[str] = ["scalars"]           # which sinks receive this channel
    decimate_to_hz: float = 10.0             # plot-only decimation
    metadata: dict[str, str] = {}
```

`ChannelRegistry` is the runtime lookup: `registry.resolve("TC_top_1")` returns an object that knows the adapter, the on-adapter channel handle, the active calibration, and the sink routing. Channel names are stable run-local identifiers; the registry snapshots all specs at run start so later config edits never change historical meaning.

`SourceBinding` is deliberately more specific than "device + channel." Each variant is a capa-side selector that points at the exact emitted field or parameter inside the underlying library record:

| Library shape | Binding example | Notes |
|---------------|-----------------|-------|
| `alicatlib.Sample` | `AlicatFrameField(device="air_mfc", field="Mass_Flow")` | `alicatlib` emits one wide `DataFrame` per poll; `DataFrame.as_dict()` includes the library's canonical underscored field names (e.g. `Mass_Flow`, `Abs_Press`, `Mass_Flow_Setpt`) plus a comma-joined `status` column and `received_at`. Wire-format names (`"Mass Flow"`) are not used as keys. |
| `watlowlib.Sample` | `WatlowParameter(device="heater", parameter="process_value", instance=1)` | `watlowlib` emits long-format one-row-per-parameter samples; `parameter` is the canonical name from `watlowlib.registry.parameters` (e.g. `process_value`) and `instance` is the loop number. |
| `sartoriuslib.Sample` | `SartoriusReading(device="balance", field="value")` | The reading row includes `value`, `unit`, `stable` / `overload` / `underload` flags, raw bytes, protocol, and error fields. |
| `nidaqlib.DaqReading` | `NIDAQReadingField(task="tc_task", field="TC_top_1")` | Polled DAQ readings are wide rows keyed by channel display name (`ChannelSpec.display_name`); `task` is `TaskSpec.name`. |
| `nidaqlib.DaqBlock` | `NIDAQBlockChannel(task="tc_task", channel="TC_top_1")` | Hardware-clocked blocks stay rectangular until the adapter derives channel samples or hands off to TDMS. |

The adapter is responsible for emitting both the preserved native record and any normalized `ChannelSample`s declared by the registry. Capa does not infer scientific channels from arbitrary library columns unless the config maps them.

**Units are first-class.** Every `unit` and `derived_unit` is validated against a `pint` UnitRegistry at config-load. Calibrations declare their input/output dimensions; a thermocouple calibration that consumes `V` and emits `kg` is a config error, not a runtime surprise. Canonical unit names follow [UCUM](https://ucum.org). Operators may type natural strings ("kPa", "deg C") in configs; the loader normalizes them to canonical form and records both in `manifest.json`.

### 5.2 `DeviceAdapter` — uniform device surface

```python
class DeviceAdapter(Protocol):
    name: str
    capabilities: frozenset[Capability]      # HAS_RAMP, HAS_TARE, HAS_SETPOINT, ...

    async def open(self) -> None: ...        # establish connection, read identity
    async def close(self) -> None: ...       # release the bus / handle
    async def start(self) -> None: ...       # begin sampling / arm hardware-clocked tasks
    async def stop(self) -> None: ...        # stop sampling without closing the connection
    async def snapshot(self) -> DeviceSnapshot: ...      # status/health/config
    async def poll(self) -> list[DeviceEmission]: ...    # SourceRecord(s) + mapped ChannelSample(s)
    async def command(self, cmd: DeviceCommand) -> CommandResult: ...
```

`DeviceEmission` is a small tagged union: `SourceRecord | ChannelSample | DeviceEvent | DeviceSnapshot`. Most adapters emit one `SourceRecord` plus zero or more mapped `ChannelSample`s per poll tick; status and error paths may emit events/snapshots without channel samples.

Lifecycle is split deliberately: `open`/`close` is the connection layer (USB/serial/IP), `start`/`stop` is the sampling layer. This matters for hardware-clocked NI tasks (you want to arm them at run start, not adapter open) and for resuming after a transient I/O hiccup without renegotiating the device. The per-resource `Worker` (§3) hosts an adapter through these transitions; the `WorkerPool` keeps `open()` paid once per config load while individual runs arm and disarm.

**Two-tier command surface.** The generic `.command(...)` exists for plugins and recipe steps that need to issue commands without knowing the concrete adapter type. Each concrete adapter *also* exposes typed methods for IDE help and refactor safety:

```python
class WatlowAdapter(DeviceAdapter):
    async def set_setpoint(self, value: float, *, confirm: bool = False) -> None: ...
    async def read_pv(self) -> Reading: ...
    async def write_parameter(self, name: str, value: float, *, instance: int = 1,
                              confirm: bool = False) -> None: ...     # ramp rate, deadband, etc.
```

`Capability` flags gate UI — "show ramp control only if the adapter declares `HAS_RAMP`" — and `Procedure.preflight()` can validate against them.

**Simulated adapters** mirror each Protocol exactly. This unblocks UI development from hardware and gives integration tests a fast deterministic substrate. Each sim adapter has a `from_params` classmethod so a hardware TOML can declare `[devices.params.signals.<key>] kind = "ramp" start = ... end = ...` and the runtime materialises it at construction — sim runs are fully driven from disk, no Python fixtures required.

### 5.3 `Method` and `Step`

A Method is a typed segmented profile. Most experiments are expressible as a list of Steps; for anything more complex you write a Procedure plugin and reuse the `MethodExecutor` service for the parts that *are* expressible.

```python
class Step(BaseModel):
    kind: Literal["hold", "ramp", "setpoint", "wait", "prompt", "acquire", "safe_shutdown", "custom"]
    target: ChannelRef | None = None         # which channel this step drives
    value: float | None = None               # for setpoint / hold endpoint
    rate: float | None = None                # for ramp (per-second or per-time-unit)
    duration_s: float | None = None
    end_condition: EndCondition | None = None  # value crossing, event, manual
    safety_overrides: list[AlarmOverride] = []
    notes: str | None = None

class Method(BaseModel):
    name: str
    description: str
    steps: list[Step]
```

**Step kinds:**

| Kind            | Meaning                                                                         |
|-----------------|---------------------------------------------------------------------------------|
| `hold`          | Command a fixed value and wait for a duration or stability condition.           |
| `ramp`          | Update a setpoint linearly over time at a configured rate.                      |
| `setpoint`      | Command an immediate setpoint change; do not wait.                              |
| `wait`          | Wait on a channel condition, event, or operator action, with optional timeout. |
| `prompt`        | Block until the operator acknowledges (used for "ignite sample," etc.).         |
| `acquire`       | Record without changing any control outputs.                                    |
| `safe_shutdown` | Reusable cooldown phase; procedures may invoke it explicitly during cleanup.    |
| `custom`        | Plugin-defined; dispatched to a registered handler keyed by `target`.           |

Editor presents this as table + graph (each pane edits the other live).

A representative method:

```yaml
name: "S073 paint-A standard run"
steps:
  - {kind: hold,    target: heater.setpoint,  value: 650, duration_s: 300}
  - {kind: ramp,    target: heater.setpoint,  start: 650, end: 800, rate_per_min: 10}
  - {kind: setpoint,target: mfc_air.flow,     value: 50}
  - {kind: prompt,  notes: "Ignite sample, then press Continue"}
  - {kind: wait,    end_condition: {channel: mass_loss_fraction, op: ">", value: 0.1}, duration_s: 600}
  - {kind: safe_shutdown}
```

`MethodExecutor` is a reusable service exposed on `ProcedureContext`. The builtin `RecipeRunner` procedure is a thin wrapper that calls `executor.run_to_completion(method)`; custom procedures can call `executor.advance_until(step_id)`, `executor.run_segment(step)`, or skip the executor entirely. Every command the executor issues is routed through `Authorization` and stamped with `(issued_by, authorization_id)`; the audit trail lands in `events.sqlite` as `method.command.issued` events.

### 5.4 `ExperimentConfig`

The full run recipe — everything needed to launch a run. Pydantic-validated, YAML/TOML on disk, snapshotted into the run bundle.

```python
class ExperimentConfig(BaseModel):
    hardware: HardwareProfile                # devices + channels
    method: Method | None                    # optional — free runs have no method
    procedure: ProcedureRef                  # plugin id + version constraint + plugin-specific config
    domain_profile: DomainProfileRef | None  # default: capa_pyrolysis
    calibration_set: CalibrationSetRef       # which cal curves are active
    storage: StoragePolicy                   # parquet flush, video codec, optional TDMS, etc.
    safety: SafetyPolicy                     # alarm rules, fault behaviors
    operator: str
    sample: SampleInfo
    tags: list[str] = []
    custom: dict[str, Any] = {}              # operator-editable freeform
```

#### 5.4.1 Domain profile — CAPA pyrolysis

Domain profiles are optional schema/preflight bundles layered on top of the generic engine. One profile ships today: `capa.profiles.capa_pyrolysis`, registered on the `capa.profiles` entry-point group and used by default.

**`capa.profiles.capa_pyrolysis`.** Models the controlled-atmosphere pyrolysis apparatus the project is named after: a sample is heated under a controlled gas atmosphere (typically inert N2; sometimes a controlled O2 mix for partial-oxidation studies). Pyrolysis chemistry under controlled atmosphere — *not* oxygen-depletion calorimetry. Contributes:

- specimen fields: id, material, initial mass, form (`disk` for ~99% of runs, `other` as the escape hatch), particle size when applicable, specimen-holder description, optional holder diameter and depth, conditioning notes
- method fields: heater program summary (target heat flux at the specimen surface in kW/m², heater setpoint °C chosen to deliver that flux via the day-of calibration, optional `flux_calibration_ref`, optional ramp rate for the minority dynamic-program runs), atmosphere mode (inert / oxidative / reducing / reactive_blend), purge duration, leak-check timestamp
- atmosphere metadata: purge-gas spec (species, purity, supplier, cylinder lot, target purge flow), optional reactive-gas spec (species, purity, target flow, target mole fraction) for partial-oxidation runs
- optional downstream-analyzer block (reserved for future setups; the current CAPA rig does not route gases to an analyzer): kind (FTIR / GC / MS / GC-MS / NDIR / other), serial, sampling-line delay, response time, external-file ref. If a future rig adds one, the analyzer is not capa-controlled — its data lives outside the bundle — but the pedigree fields are captured so the run record cross-references the right external dataset
- required channel groups: `heater_setpoint`, `heater_pv`, `sample_temperature`, `purge_gas_flow`. Optional: `mass` (load-cell rigs), `reactive_gas_flow`, `reactor_pressure`
- preflight checks: static — required channel mappings, atmosphere consistency (oxidative/blend mode requires a reactive-gas channel), leak-test recency, disk projection; dynamic (after adapters start, inside the task group) — heater PV in safe startup range, purge flow established, balance stability when mass is present. A silent live-data channel post-start is a blocking error, not a downgraded warning.

The profile snapshot lands in `profiles/capa_pyrolysis.toml` and is referenced from `manifest.json.domain_profile`. The profile does not make `capa` a standards-certification tool; it ensures the run bundle captures the metadata a researcher or later analyzer needs.

A cone-calorimeter profile module exists in the codebase but is not part of the active product surface; there are no current plans to drive a cone calorimeter with capa. If a cone-mode (or any other) profile becomes a real workload it will be (re-)introduced alongside CAPA pyrolysis on the same Protocol.

### 5.5 `Calibration` — first-class snapshotted objects, with uncertainty

```python
Calibration = Identity | LinearTwoPoint | Polynomial | Lookup | Piecewise | CustomCallable

class Calibration(BaseModel):
    input_unit: UnitStr                      # pint-validated; matches ChannelSpec.unit
    output_unit: UnitStr                     # pint-validated; matches ChannelSpec.derived_unit
    uncertainty: UncertaintySpec | None      # k=1 / k=2 documented; method noted
    fit_metadata: FitMetadata | None         # if produced by a calibration routine:
                                             # reference instrument, serial, date, residuals,
                                             # source-procedure id + git SHA
    coefficients: ...                        # variant-specific
```

A `CalibrationSet` (collection of curves keyed by channel name) is loaded from disk at run-start. The current bundle records the selected set's `name` and `revision` in `calibration.json`; full resolved-curve snapshots are planned.

**Uncertainty is part of the contract, not bolted on.** Every Calibration carries an `UncertaintySpec` (or an explicit `None` declaring "unmeasured" — never silent). Derived channels propagate uncertainty through their transform (analytical for linear, Monte Carlo via configured budget for nonlinear). Once full calibration snapshots land, the uncertainty payload will travel with the resolved curves instead of relying on the referenced source file.

Calibration *routines* (e.g., "calibrate the heat-flux gauge against the reference") are themselves Procedure plugins that produce a new Calibration object — including a fitted uncertainty estimate from the reference comparison — on completion, and offer to write it into the active set behind an explicit operator approval gate. The fit metadata records *which* version of the calibration procedure produced the curve (procedure id + capa git SHA), so a curve's pedigree is recoverable.

`CustomCallable` is allowed only when it is reproducible. A custom calibration must name an entry point, package version, distribution hash, callable id, serialized parameters, input/output dimensions, and test vectors. The active callable metadata is snapshotted into `calibration.json`; an anonymous lambda or unversioned script path is a config error. Prefer pure-data calibration forms (`Polynomial`, `Lookup`, `Piecewise`) whenever they can express the curve.

### 5.6 `SourceRecord` and `ChannelSample` — native truth plus normalized channels

The device libraries already expose carefully designed acquisition shapes. Capa should not discard that work. The adapter layer therefore emits two related objects:

1. `SourceRecord` preserves the library-native record exactly enough to reconstruct the original tabular row or block.
2. `ChannelSample` is the normalized scientific channel stream used by plots, alarms, procedures, and cross-device analysis.

```python
class SourceRecord(BaseModel):
    record_id: str                            # stable within run; referenced by derived samples
    adapter: str                              # "alicat", "watlow", "sartorius", "nidaq"
    device: str
    shape: Literal["wide_row", "long_row", "block"]
    t_mono_ns: int                           # best record-level timestamp
    t_utc: datetime
    row: dict[str, float | int | str | bool | None] = {}
    block_ref: str | None = None             # for rectangular block sidecars / TDMS
    metadata: dict[str, Any] = {}

class ChannelSample(BaseModel):
    channel: str
    t_mono_s: float                          # in-memory ergonomics: seconds since run start
    t_mono_ns: int                           # canonical: int64 nanoseconds; what the parquet column stores
    value: float | int | bool
    raw: float | int | bool | str | None = None
    unit: str
    uncertainty: float | None = None         # absolute, in `unit`; populated when calibration declares one
    status: str = "ok"
    source_record_id: str | None = None       # back-pointer to SourceRecord
    source_field: str | None = None           # e.g. Alicat "Mass Flow", Watlow "process_value"
    metadata: dict[str, Any] = {}
```

Both `t_mono_s` and `t_mono_ns` are populated on `ChannelSample`; the in-memory float is convenient, but `scalars.parquet` stores the int64 ns column as the canonical join key (lossless across hour-long runs). `t_mono_s` is derived `t_mono_ns / 1e9`.

`SourceRecord.row` is the library-native flattened row:

- `alicatlib.sinks.sample_to_row()` style rows: timing provenance (`requested_at`, `received_at`, `midpoint_at`, `monotonic_ns`, `latency_s`) plus `device` / `unit_id` plus all `DataFrame.as_dict()` fields, often multiple measurement columns per poll.
- `watlowlib.sinks.sample_to_row()` style rows: one row per parameter read, with `device`, `address`, `protocol`, `parameter`, `parameter_id`, `instance`, `value`, `unit`, and the same timing fields.
- `sartoriuslib.sinks.sample_to_row()` style rows: `value`, `unit`, `sign`, `stable`, `overload`, `underload`, `decimals`, `sequence`, `protocol`, `raw`, `error_type`/`error_message`, plus timing fields.
- `nidaqlib.sinks.reading_to_row()` style rows: one wide row per polled DAQ read, with one value column per DAQ channel plus `<channel>_unit` columns, plus `device` / `task` / timing fields.

Hardware-clocked NI-DAQ blocks stay block-shaped inside the NI adapter (preserving driver timestamps) until either channel samples are derived at capa's normal rates or a TDMS/block sidecar takes over for high-rate capture.

Adapters may also emit structured device-health snapshots and events. The rule of thumb is simple: `SourceRecord` preserves what the library emitted; `ChannelSample` is what capa understands as a named scientific signal.

---

## 6. Time and synchronization

Single monotonic timebase per run.

```python
class RunClock:
    started_mono_ns: int      # time.monotonic_ns() captured at run.start()
    started_utc:    datetime  # datetime.now(UTC) captured at the same instant

    def t_mono(self) -> float:                 # seconds since run start
        return (time.monotonic_ns() - self.started_mono_ns) / 1e9

    def to_wall(self, t_mono: float) -> datetime: ...
```

Every `ChannelSample` carries `t_mono_s` (float64 seconds since run start) as the ergonomic join key and `t_mono_ns` as the canonical persisted join key. Wall time is derived from the run-start UTC anchor. NI-DAQ samples carry the driver's own timestamps (highest-quality timebase available). Serial-attached devices use the provenance already exposed by the libraries: `alicatlib`, `watlowlib`, and `sartoriuslib` samples all carry `requested_at`, `received_at`, and `midpoint_at` wall-clock timestamps plus a `monotonic_ns` int from `time.monotonic_ns()`; capa uses `midpoint_at` as the best point estimate to halve worst-case round-trip skew. Visible-camera frames are stamped on `frame_received` and stored in `visible_cam0.frames.parquet`. IR-camera frames are timestamped by the FLIR Atlas SDK via the per-frame receipt records the adapter pumps onto the engine loop; each receipt carries `(frame_idx, t_mono_ns, t_utc, capture_latency_s)` and lands in `ir_cam0.frames.parquet` directly, so analysis tools can correlate without parsing the `.csq` container.

At 3–60 Hz this is fine — sub-millisecond cross-device jitter is invisible. Hardware sync can be added later (NI trigger lines, IRIG-B) if a measurement demands it.

The run-start UTC anchor is also embedded in the visible MKV container metadata and stored as `started_utc` in `manifest.json` so external tools can re-correlate frames against the Parquet scalar stream by absolute time.

---

## 7. Acquisition pipeline

Each `Worker` runs a producer loop on its own asyncio loop, draining the adapter at its native rate. A `Conductor` drain task (one per worker, run on the Conductor's loop) consumes via the per-worker `ThreadBridge` and applies the fan-out:

```
   [Per-worker producer loops]                  [Procedure task on Conductor loop]
   nidaq · watlow · alicat ·                    walks the Method via
   sartorius · webcam · flir_ir                 MethodExecutor; issues
   (each on its own thread+loop;                setpoints; awaits events
    polls at its native rate)                            │
            │                                            │
            │  SourceRecord + ChannelSample              │  Commands
            │  via ThreadBridge (BLOCK +                 │  (dispatched via Worker
            │  saturation deadline)                      │   from the Conductor loop)
            ▼                                            │
       ┌────────────────────┐                            ▼
       │  Calibration apply │              ┌───────────────────┐
       │  (raw -> value)    │              │ ChannelRegistry   │
       └─────────┬──────────┘              │ .write(name, val) │
                 │                         └─────────┬─────────┘
                 ▼                                   │
        ┌────────────────────┐                       ▼
        │  Conductor fan-out │              [Adapters issue
        │  (one consumer per │               typed commands
        │   worker drain)    │               with run authorization]
        └──┬──────┬──────┬───┘
           │      │      │
           ▼      ▼      ▼
      ┌────┐  ┌────────┐ ┌────────┐  ┌──────────────┐
      │Ring│  │Writer  │ │ Alarm  │  │ Camera workers│
      │buf │  │thread  │ │ check  │  │ (frame-index  │
      │    │  │+sinks  │ │        │  │  + container) │
      └─┬──┘  └───┬────┘ └────┬───┘  └──────┬───────┘
        │       │          │             │
        ▼       ▼          ▼             ▼
    [UI 10Hz] [Disk]   [SafetyMon]   [video sinks]
```

### 7.1 Backpressure policy (named, enforced)

Every queue in the pipeline declares one of three policies. Sinks that violate their policy are a bug, not a configuration choice.

```python
class BackpressurePolicy(Enum):
    BLOCK         = "block"          # producer waits; durable record, must not lose
    DROP_OLDEST   = "drop_oldest"    # ring-buffer semantics; freshness > completeness
    ABORT_RUN     = "abort_run"      # fault if queue stays full past a timeout
```

| Stage                              | Policy                          | Reason                                                          |
|------------------------------------|---------------------------------|-----------------------------------------------------------------|
| Worker → ThreadBridge → Conductor  | `BLOCK` + saturation deadline   | Sample rates are low; loss on the floor is unacceptable. Conductor-side deadline trips a `safe_shutdown` if the bridge stays full. |
| Conductor → durable sinks          | `BLOCK` then `ABORT_RUN` @ 5 s  | The bundle is the scientific record                             |
| Conductor → safety checker (planned, see §9) | `BLOCK` (own queue)   | Safety must never starve behind a slow disk; separate queue from sinks |
| Conductor → ring buffers (UI)      | `DROP_OLDEST`                   | Repaint freshness matters more than every point                 |
| Conductor → alarm checker          | `BLOCK`                         | Missed alarm checks defeat the purpose                          |
| Camera SDK → file                  | (vendor-managed)                | Surface failure via health watchdog, not by intercepting bytes  |
| Worker → status snapshots          | `DROP_OLDEST`                   | Latest-value semantics; periodic, not historical                |

**Disk writes never happen on the fan-out hot path.** The writer thread (constructed per run by the Conductor) owns its own queue and writer task. The fan-out enqueues and moves on.

**Every queue is instrumented.** Each queue exposes (current depth, depth-high-water-mark, dequeue-latency-p50/p99). These feed the status bar live (§10.4) **and** are written to `manifest.json` at finalize as a histogram per queue, so post-run "was this run healthy?" is a one-glance check rather than a forensic dive into the log.

### 7.2 Pipeline rules

- **Producers** wrap each library Manager via the library's module-level recorder — `nidaqlib.record_polled(manager, ...)`, `alicatlib.record(manager, ...)`, `watlowlib.record(manager, ...)`, `sartoriuslib.record(manager, ...)` (each takes the Manager as a `PollSource`) — and translate the emitted `Sample` / `DaqReading` / `DaqBlock` into adapter-specific `SourceRecord` streams plus mapped `ChannelSample`s.
- **Native row preservation** happens before normalization. Adapters use the libraries' own `sample_to_row(...)` / `reading_to_row(...)` helpers where available so the bundle preserves the same row fields researchers would see if they used the library directly. Capa then applies `SourceBinding` mappings to derive named channel samples.
- **Calibration application** is the only non-trivial CPU step in the normalized pipeline; happens before fan-out so all consumers see calibrated `ChannelSample`s. Raw values are still routed to the channel-sample sink when `keep_raw=True` on the channel, and the unmodified library row remains in `device_records/`.
- **Fan-out** is the single consumer of the producer streams (one drain task per worker on the Conductor loop). It writes normalized channel samples to ring buffers (RAM, for plots), the channel-sample sink, the alarm checker, and procedure DataBus subscriptions. It also routes native source records to the device-record sink.
- **UI bridge** drains ring buffers at **10 Hz** and emits Qt signals on the GUI thread via `qasync`. Underlying disk capture is at native rate, untouched by the repaint cadence.
- **Control loop cadence.** Method/procedure control loops tick at a fixed cadence (default 10 Hz, configurable to 20 Hz). Polling rates are per-channel.
- **Command serialization.** Commands to a given physical bus are serialized by the underlying library Manager and routed through the owning Worker. Every commanded value is logged as an event *and*, when applicable, as a setpoint channel sample.
- **Safety** is split. *Today:* per-adapter `safe_shutdown()`, the saturation deadline (`crashed_but_sealed`), and authorization gates carry the safety contract end-to-end. *Planned:* the rule-based `SafetyMonitor` of §9, which will run as its own task on the Conductor loop.

Aborting cancels the Conductor's task group and disarms the workers; a `finally` block flushes sinks, closes camera files, writes `ended_utc`, `run_status`, and `bundle_status` into the bundle and the run catalog, and transitions the bundle through `finalizing` to `sealed`, `finalized_unverified`, or `verification_failed` as appropriate. The scientific run outcome remains separate: `completed`, `aborted`, or `crashed`. The pool stays open for the next run.

---

## 8. Storage — the run bundle

A run produces one directory containing every artifact needed to interpret it later. File-by-purpose, not one monolithic format.

```
runs/2026-05-07_153000_S073-paint-A/
├── manifest.json              # run id, schema version, operator, sample, full provenance,
│                              #   started/ended UTC, run outcome, queue health, integrity table
├── config.toml                # full ExperimentConfig snapshot (canonicalized units)
├── method.toml                # the loaded Method (segmented profile)
├── equipment.toml             # detected devices + firmware versions at run-start
├── calibration.json           # snapshot of all active cal curves (with uncertainty)
├── scalars.parquet            # normalized channel samples: t_mono_ns, t_utc, channel, value, unit, ...
├── device_records/            # library-native emitted rows/blocks, one file per adapter/family
│   ├── alicat.parquet         # wide DataFrame rows: pressure/temp/flow/setpoint/gas/status...
│   ├── watlow.parquet         # long parameter rows: device/parameter/instance/value/unit...
│   ├── sartorius.parquet      # balance value + stability/off-scale/protocol/error fields
│   └── nidaq_polled.parquet   # wide DaqReading rows: one value column per channel
├── events.sqlite              # operator notes, segment transitions, alarms, errors, commands
├── status.sqlite              # periodic device-snapshot rows (for diagnostics)
├── run.log                    # structlog JSON, run-id correlated, captured into bundle
├── env/
│   ├── uv.lock                # lockfile snapshot — exact dependency versions
│   └── packages.json          # python -m pip list output, plus Qt/PyQt build info
├── video/
│   ├── visible_cam0.mkv       # H.264, 30 fps (small, capa-encoded)
│   ├── visible_cam0.frames.parquet
│   ├── ir_cam0.csq            # FLIR native, SDK-recorded, 1–20+ GB, NOT transcoded
│   ├── ir_cam0.meta.json      # capa-recorded sidecar: SDK config, file size, sha256
│   └── ir_cam0.frames.parquet # per-frame (frame_idx, t_mono_ns, t_utc, capture_latency_s)
├── tdms/                      # optional, present only if storage.tdms.enabled
│   └── nidaq_<task>.tdms
├── exports/                   # optional, generated post-hoc by `capa export-parquet`
├── artifacts/                 # procedure-specific outputs (plots, fitted cal curves, etc.)
├── profiles/
│   └── capa_pyrolysis.toml   # domain-standard metadata/preflight snapshot for the active profile
├── ro-crate-metadata.json     # optional; present if storage.rocrate.enabled or `capa export-rocrate`
└── manifest.sha256            # finalize-time table: sha256 of every file in the bundle
```

**Why this shape:**
- `scalars.parquet` is a normalized long table queryable across thousands of runs with DuckDB / Polars / Arrow in one line, no glue code and no implicit mixed-rate interpolation.
- `device_records/*.parquet` preserves what the libraries actually emitted — important for diagnostics, future parser fixes, and researchers who expect Alicat/NI-style multi-column rows.
- `events.sqlite` is transactional and crash-safe — events written are not lost even on abnormal exit.
- `manifest.json` / `*.toml` are git-diff-friendly; you can inspect them with `cat` and review them in a PR.
- `tar -cf` over the directory produces a single-artifact bundle whenever you want one.
- Visible video stays in MKV (no need to preserve radiometry); IR thermal stays in native FLIR `.csq` (raw radiometry preserved, vendor calibration intact, can be re-colorized at any time).
- Frame-index parquets carry the canonical `(frame_idx, t_mono_ns, t_utc, capture_latency_s)` schema for every camera, so visible and IR analyses share the same join key against `scalars.parquet`.
- `status.sqlite` keeps low-rate device-health rows (e.g., Watlow alarm bits, Alicat valve drive, balance stable flag) separate from the `scalars.parquet` engineering channels — different cadence, different consumer.
- `profiles/` captures domain-standard context such as the temperature program summary, atmosphere metadata, specimen form/holder, leak-check recency, and (when present) downstream-analyzer pedigree fields.
- `env/` and `manifest.sha256` make the bundle a closed scientific record. Re-deriving values five years later does not depend on what tooling happened to be installed today.

**Live readback during a run is not needed** — the in-memory ConductorDataBus serves the UI. Files are written in flush-bounded chunks for crash safety, then **rewritten into well-sized row groups at finalize** (see §8.5). They are opened for analysis only after the bundle is readable (`finalized_unverified` or `sealed`).

### 8.1 Manifest contents — full provenance

`manifest.json` is the bundle's index card. Every field is required (or explicitly `null` with a comment). Schema-versioned and validated by Pydantic on read.

```jsonc
{
  "run_id": "2026-05-07_153000_S073-paint-A",
  "bundle_schema_version": 1,
  "started_utc": "2026-05-07T15:30:00.123Z",
  "ended_utc":   "2026-05-07T16:42:18.041Z",
  "started_mono_ns_anchor": 18293847203847,
  "run_status": "completed",             // running | completed | aborted | crashed
  "bundle_status": "sealed",             // open | finalizing | finalized_unverified |
                                          // sealed | verification_failed
  "exit_reason": null,                   // human-readable on abnormal run outcomes

  "operator":      { "id": "abr", "display_name": "A. Researcher" },
  "sample":        { /* SampleInfo: id, material, prep, mass, geometry, notes */ },
  "procedure":     { "id": "capa.builtin.recipe_runner", "version": "1.4.2" },
  "domain_profile": {
    "id": "capa.profiles.capa_pyrolysis",
    "standard_refs": []
  },
  "tags":          ["paint-A", "S073-series", "campaign-2026Q2"],

  // --- Software environment provenance ---
  "capa": {
    "version": "0.7.3",
    "git_sha": "a3f1c2d...",
    "git_dirty": false,
    "build_time": "2026-05-01T09:12:00Z"
  },
  "python": { "version": "3.12.4", "implementation": "CPython", "executable": "..." },
  "platform": { "os": "Windows-11-10.0.26100", "machine": "AMD64", "node": "RIG-PC-01" },
  "lockfile": { "path": "env/uv.lock", "sha256": "..." },
  "plugins": [
    { "id": "capa.builtin.recipe_runner", "version": "1.4.2",
      "package": "capa", "entry_point": "capa.procedures:RecipeRunner",
      "distribution_hash": "sha256:..." },
    { "id": "lab.heatflux.calibration",  "version": "0.3.0",
      "package": "lab-heatflux", "entry_point": "...",
      "distribution_hash": "sha256:..." }
  ],

  // --- Storage shape ---
  "data_shape": {
    "channel_samples": { "path": "scalars.parquet", "layout": "normalized_long" },
    "device_records": [
      { "adapter": "alicat", "path": "device_records/alicat.parquet", "layout": "wide_row" },
      { "adapter": "watlow", "path": "device_records/watlow.parquet", "layout": "long_row" },
      { "adapter": "sartorius", "path": "device_records/sartorius.parquet", "layout": "single_value_row" },
      { "adapter": "nidaq_polled", "path": "device_records/nidaq_polled.parquet", "layout": "wide_row" }
    ]
  },

  // --- Run health (post-finalize) ---
  "queue_health": {
    "fanout_to_sinks": { "depth_p50": 3, "depth_p99": 14, "depth_max": 31, "lag_s_max": 0.42 },
    "fanout_to_safety": { "depth_p50": 0, "depth_p99": 1, "depth_max": 2, "lag_s_max": 0.01 },
    /* one entry per named queue */
  },
  "dropped_samples": { "ui_ringbuffer": 1247, "status_snapshots": 0 },

  // --- Integrity ---
  "integrity": {
    "status": "ok",                      // unknown | ok | mismatch | partial
    "manifest_sha256_path": "manifest.sha256",
    "algorithm": "sha256"
  }
}
```

The same fields are mirrored into `runs.sqlite` (flattened) for cross-run search.

### 8.2 Integrity — sha256 over every artifact

At finalize, capa walks the bundle and writes `manifest.sha256`:

```
a3f1c2d...  config.toml
b8e2d4f...  method.toml
c1d3e5a...  calibration.json
9f4a8b2...  scalars.parquet
e7c2a1d...  events.sqlite
...
4d8e1f3...  video/ir_cam0.csq
```

This catches:
- Bit-rot on long-term storage
- Partial copies over flaky network shares
- Post-hoc tampering (intentional or accidental)

`capa catalog verify RUN_ID` re-walks the bundle and compares. Any drift is flagged in the catalog. The IR `.csq` sha is computed in a background task during finalize (10–60 s for 20 GB) so it does not block the operator's "run done" indicator.

Run outcome and bundle integrity are deliberately separate. This matters for research: an aborted or crash-recovered run can still be a valid, sealed scientific artifact.

| `run_status` | Meaning |
|--------------|---------|
| `running` | Acquisition is active. |
| `completed` | Method/free-run ended normally. |
| `aborted` | Operator or safety path stopped the run. |
| `crashed` | Recovered/finalized after abnormal termination; analysis tools can read it, but the outcome remains scientifically visible. |

| `bundle_status` | Meaning |
|-----------------|---------|
| `open` | Files may still be mid-write. |
| `finalizing` | Acquisition stopped; sinks are flushing, Parquet rewrite is in progress, large-file hashes may still be running. |
| `finalized_unverified` | Scientific data are readable, but one or more integrity hashes are pending. The UI may show "run complete" but not "safe to archive." |
| `sealed` | All files listed in `manifest.sha256`; integrity table verified. Safe to copy/archive. |
| `verification_failed` | Finalization completed enough to inspect data, but integrity generation or verification failed and operator review is required. |

The Review tab and catalog distinguish "readable" from "sealed." A bundle copy/export command refuses `open`, `finalizing`, and `verification_failed`; it warns on `finalized_unverified` unless the operator explicitly overrides. `run_status` is never hidden, so aborted or crash-recovered runs remain visible even when `bundle_status="sealed"`.

### 8.3 Schema versioning

`manifest.json` carries a `bundle_schema_version` integer. Changes to the bundle layout bump the version; a `storage/schema.py` migration registry maps old → new. This makes runs from previous versions of capa first-class — you read them with the same tooling as new ones, and `capa catalog list` flags any bundles whose version is older than the current.

### 8.4 Run catalog

A single `runs.sqlite` at the runs root indexes every bundle:

```
runs:
  run_id        TEXT PRIMARY KEY
  path          TEXT
  started_utc   TIMESTAMP
  ended_utc     TIMESTAMP
  operator_id   TEXT
  sample_id     TEXT
  procedure     TEXT
  capa_version  TEXT
  capa_git_sha  TEXT
  run_status    TEXT     -- running | completed | aborted | crashed
  bundle_status TEXT     -- open | finalizing | finalized_unverified | sealed | verification_failed
  schema_version INTEGER
  integrity_status TEXT  -- unknown | ok | mismatch | partial
  tags_json     TEXT
  summary_json  TEXT
operators:
  id            TEXT PRIMARY KEY
  display_name  TEXT
  active        INTEGER
artifacts:
  run_id, kind, path, sha256, size_bytes, metadata_json
```

Used by the Review tab and any future search / reporting tool. **The catalog is not the source of truth** — the bundle is. The catalog is a rebuildable index. On startup, any run whose `ended_utc` is null is flipped to `run_status="crashed"`, and capa offers to **finalize-in-place** any orphaned bundle (re-read partial Parquet/SQLite, write `ended_utc`, compute checksums, then set `bundle_status` to `sealed` or `verification_failed`) — see §13.

### 8.5 Parquet row-group sizing — two-stage write

The naive "row group every 1–2 s" approach gives crash safety but *destroys* cross-run query performance. At 60 Hz × 30 channels, a one-hour normalized channel table produces ~6.5M rows; flushing every second would create ~3600 tiny row groups — far smaller than what DuckDB / Arrow / Polars want.

Capa uses a **two-stage write**:

1. **In-flight (during the run).** Append to Arrow-IPC streams (`scalars.in-flight.arrows`, `device_records/*.in-flight.arrows`, `video/<camera>.frames.in-flight.arrows`) with small flush boundaries (~1 s of data) for crash safety. Metadata is fsync'd at every flush.
2. **Finalize (run end).** Rewrite to `scalars.parquet`, final `device_records/*.parquet`, and `video/<camera>.frames.parquet` with **large row groups (target 256k rows or ~64 MB)**, sorted by `t_mono_ns` where present, compressed with `zstd:6`. Delete in-flight files only after the new files' sha256 entries are verified.

If the engine crashes before finalize, the in-flight files are still readable and `capa finalize RUN_ID` (§14) performs the rewrite offline — no data is lost. The run remains scientifically marked `run_status="crashed"`, even after `bundle_status="sealed"`, so downstream users can distinguish clean runs from recovered ones.

This pattern makes both the live system crash-tolerant and the long-term archive query-fast.

### 8.6 RO-Crate / FAIR-ready bundles

Capa optionally emits an `ro-crate-metadata.json` alongside `manifest.json` so a sealed bundle can be ingested directly into Zenodo, Dataverse, or any FAIR-aligned repository without conversion. The crate references the same files the manifest already lists, with schema.org typing and license metadata. Off by default; opt in via `storage.rocrate.enabled = true` or `capa export-rocrate RUN_ID` post-hoc.

### 8.7 TDMS escape hatch (high-rate NI DAQ)

Capa's normal envelope is 3–60 Hz. If a future task needs hardware-clocked DAQ at kHz rates, pushing those samples through Python is the wrong approach. Instead:

- The NI adapter switches to driver-side TDMS via `nidaqlib.TdmsLogging`.
- The TDMS file lands in `runs/<bundle>/tdms/<task>.tdms`.
- `manifest.json` records the relative path, channel list, and effective rate.
- `scalars.parquet` carries low-rate summary channels derived from the TDMS stream (mean/min/max per second), so cross-run analysis still works without parsing TDMS.

This is opt-in per task. Most channels stay on the standard Parquet path.

### 8.8 Parquet export tool

`capa export-parquet RUN_ID [--channels ...] [--time-range ...] [--out PATH]` generates a focused Parquet from a sealed bundle for sharing — strips video, narrows columns, optionally resamples. The original bundle is never modified.

### 8.9 Device-record preservation

The source libraries already made careful choices about their emitted row shapes:

| Adapter | Native emitted shape | Capa preservation rule |
|---------|----------------------|------------------------|
| Alicat | One wide `DataFrame` row per poll, with timing plus firmware-dependent measurement fields. | Store one wide native row in `device_records/alicat.parquet`; derive configured channel samples from named frame fields. |
| Watlow | One long row per `(device, parameter, instance)` sample. | Store the long parameter rows in `device_records/watlow.parquet`; most capa channels map one-to-one to rows. |
| Sartorius | One balance row with value/unit/stability/off-scale/protocol/raw/error fields. | Store the native row in `device_records/sartorius.parquet`; map weight/stability channels explicitly. |
| NI DAQ polled | One wide `DaqReading` row per poll, with one column per DAQ channel. | Store the wide row in `device_records/nidaq_polled.parquet`; derive channel samples by field name. |
| NI DAQ hardware-clocked | Rectangular `DaqBlock` with `(channels, samples_per_channel)` data. | Keep block-shaped data until low-rate channel derivation or driver-side TDMS takes over. Avoid scalarizing kHz data through Python. |

This gives researchers both surfaces: a uniform `scalars.parquet` for "plot/merge/query channel X across runs" and native device records for "what did the Alicat/Watlow/Sartorius/NI library actually report?"

---

## 9. Safety architecture

Safety is its own subsystem with its own state and its own escalation path. It is not a check buried in an acquisition callback. The contract is split: several primitives are already wired into the `Conductor` (see [`runtime-architecture.md` §6](runtime-architecture.md)) today; the rule-based `SafetyMonitor` that ties them together against operator-configured rules is planned (see §16).

### 9.1 What's wired today

Four primitives carry the safety contract on the current runtime:

1. **Authorization gates on every device write.** Every `DeviceCommand` carries `issued_by: OperatorId`, `authorization_id: str | None`, and `confirmed_by: OperatorId | None`. Scheduled method/procedure commands inherit the arm/start authorization the operator approved; manual overrides require an immediate confirmation gesture in the UI. The dispatcher refuses to issue a command without either a valid run authorization or a manual confirmation, and `events.sqlite` records the full trail. Any setpoint change in the bundle is attributable to a named operator without after-the-fact reconstruction.
2. **Per-adapter `safe_shutdown()`.** Hardware adapters with state-bearing outputs (heaters, flow setpoints) implement a `safe_shutdown()` that drives outputs to a safe configured state. The Conductor's normal disarm calls it on every adapter before the worker exits; force-cancel paths also run it before signalling stream exit (`runtime-architecture.md` §6.2).
3. **Saturation deadline → `crashed_but_sealed`.** The `SaturationMonitor` (see `runtime-architecture.md` §6.3) escalates a stuck writer or stuck producer to a forced disarm + seal after `saturation_deadline_s` (default 10 s). The bundle is still sealed; hardware does not stay in an inconsistent state because each adapter's `safe_shutdown` still runs.
4. **Per-worker watchdog.** Each producer task reports last-sample time; the per-worker watchdog state is the canonical "is this producer alive" view and writes `device_silent` events when a producer goes quiet.

### 9.2 SafetyMonitor (planned)

The rule-based `SafetyMonitor` task that consumes `adapter.watchdog_state()` plus the channel-sample stream, evaluates declarative rules, and dispatches verdicts through the Conductor is **not yet implemented** (see §16 — *SafetyMonitor activation*). The design below is the target shape for that task.

**Verdict actions.** Each rule will declare one of:

| Action            | Behavior                                                                |
|-------------------|-------------------------------------------------------------------------|
| `warn`            | Record event, show UI banner, continue.                                 |
| `pause_method`    | Hold method execution at current step; keep acquiring; await operator.  |
| `abort_run`       | Immediate cancel + finalize. Sets `run_status="aborted"`.               |
| `safe_shutdown`   | Run cooldown procedure, then finalize. Default for thermal faults.      |

The action is part of the rule, not the verdict — the same fault should always have the same response, and the response is reviewable in config.

**Day-1 rule set (declarative, in `ExperimentConfig.safety`):**

- max heater temperature (per-channel)
- max temperature ramp rate (per-channel, sliding window)
- missing-data timeout (per-channel: no sample for N × period)
- device disconnected (adapter heartbeat lost)
- mass / flow out of expected range
- camera recording failure (SDK error, frame stall, or output file not growing)
- writer-lag watchdog (sink queue depth past threshold for >N s)
- **disk space low** (preflight gate + during-run monitor — IR `.csq` files commonly hit 5–20 GB; abort if free space < projected remaining × 1.5)
- emergency stop / manual abort (UI button or external digital input via NI-DAQ)

**Queue placement.** When wired, the SafetyMonitor will subscribe to the `DataBus` with its **own** queue (`BLOCK` policy, ~64 samples) and be the *first* fan-out subscriber so its latency is independent of disk health.

### 9.3 Two abort modes

`safe_shutdown()` is modeled as an explicit Procedure or MethodExecutor phase: it can ramp heaters to a configured cool target, close flow setpoints to zero, mark the run as aborted, and wait for thermal verification before finalizing the bundle. Current Run-tab buttons stamp fixed modes (`operator_safe_shutdown` for Stop, `operator_immediate` for Emergency); procedures decide how much cleanup to honor.

Hardware-enforced safety (over-temp relays, gas interlocks) lives outside Python and is not replaced by this layer. SafetyMonitor — once implemented — will be the *application-level* monitor, never the only line of defense.

---

## 10. UI design

`QMainWindow` with `QDockWidget` panels — all dockable / floatable / per-user persistent. Layout state saved to `~/.capa/window_state.json`.

### 10.1 Tabs (central widget)

- **Setup.** Hardware profile editor. Tree on the left (devices → channels), Pydantic-driven form on the right. "Detect devices" button issues a discovery scan (each adapter contributes a `discover()` classmethod). Calibration manager lives here as a sub-pane. The active domain profile (CAPA pyrolysis by default) contributes required metadata and preflight checks.
- **Method.** Third tab between Setup and Run. `QTableView` over `MethodTableModel` (`list[Step]` owner) with `#` / `kind` / `target` / `summary` columns; selecting a row builds a detail panel via `build_form(type(step))` so edits flow back into the model and the table summary updates live. Toolbar actions for Open / Save / Save As / Validate / Add Step (one menu entry per step kind, each with sane defaults) / Delete. A `pyqtgraph.PlotWidget` at the bottom shows operator-declared setpoint vs. elapsed time across the method, one series per target channel; non-setpoint steps render as dashed vertical guide lines. Saves as `.method.toml`.
- **Run.** The instrument-console view. Big run-state header (Idle / Armed / Running / Aborting / Finalizing / Sealed), Arm/Start/Abort buttons, method-progress bar, procedure-specific custom widget (rendered from the plugin), active authorization summary, live numeric grid, dockable plot panes, camera previews. This is the screen you stare at during a run.
- **Review.** Completed-run summary: manifest, headline plots, event log, segment timeline, link to "Open in analyzer." No editing.

### 10.2 Docks

- **Numerics** — large readouts of starred channels.
- **Events** — append-only log of segment transitions, alarms, and errors.
- **Notes** — operator-typed free text; each entry is tagged with `t_mono_s` and written to `events.sqlite` so it joins everything else on the timeline.
- **Camera previews** — small thumbnails for each active camera. Cameras whose adapters do not declare `CameraCapability.LIVE_PREVIEW` show a static "no preview" placeholder. Tiles update at the adapter's throttled cadence (the webcam adapter caps at 2 Hz), flip `idle` / `live` / `stale` based on preview arrival, and surface drops + sticky failure borders driven by `pump_warning` / `pump_failed` camera events.
- **Manual control cards** — one card per controllable device family (Watlow, Alicat, …) that routes through `ManualClient` to either the Conductor (during a run) or the WorkerPool (between runs).

Default placement: hardware tree on the left, important numerics and procedure-specific control widgets on the right, events / notes / alarms across the bottom. All panels are dockable / floatable / hidable; layout is per-user and persisted to `~/.capa/window_state.json`.

### 10.3 Plots

PyQtGraph multi-pane. Drag a channel from the tree into a pane to add it. Per-pane Y-axis assignment. **10 Hz** screen-repaint rate via per-channel ring buffers. Cursor tool, range-zoom, in-run annotation pins (annotations are events with a plot anchor).

### 10.4 Status bar

Always visible. Shows operational health at a glance:

- Run state and elapsed time
- Dropped UI samples (rolling 10 s)
- Sink writer lag (worst queue depth, in samples and seconds; clicking opens a per-queue histogram)
- Safety-queue depth (separate readout — must stay near zero)
- Disk free on the bundle volume + projected fill from active video streams
- Camera health (per camera: green/yellow/red)
- Operator id (who armed the run)

These are the numbers that answer "is my run OK right now?" without digging. The same metrics are written into `manifest.json`'s `queue_health` block at finalize, so "was this run healthy?" is a one-glance check post-hoc.

### 10.5 Forms from Pydantic

Procedure config and per-channel config are Pydantic models. The form generator (`ui/forms/`) walks `model_cls.model_fields` and produces a `ModelForm` (`QFormLayout`-based) with one widget per field. Annotation-driven mapping covers primitives (str, int/float with `Field(gt/ge/lt/le)` constraint application, bool, datetime, Path), `Literal[...]`, nested `BaseModel` (recursive form in a group box), `tuple[X, ...]` / `list[X]` (string and nested-model variants), `dict[str, float]`, and `X | None` (wrapped with a "Set" enable checkbox). `validate()` runs `model_cls.model_validate(values())` and paints inline error styles on offending widgets. New plugins get a UI for free.

### 10.6 Modes

**Simple Mode** and **Expert Mode** are saved window-state presets, not separate code paths. Simple Mode hides the Setup and Method tabs once a config is loaded, hides expert docks, and restricts the Run tab to Arm/Start/Abort + procedure widget + plots. Operators can toggle. This avoids building a second UI for operators and avoids a brittle role system.

---

## 11. Procedure plugin system

Procedures are how research workflows extend `capa`. They are not the only way to run an experiment — most pyrolysis runs are just `RecipeRunner(method=...)` — but they are the path for anything that needs custom logic or a custom UI.

```python
class Procedure(Protocol):
    id: str                              # "capa.builtin.recipe_runner"
    name: str                            # "Standard Recipe Run"
    version: str
    config_model: type[BaseModel]
    required_capabilities: list[Capability]
    required_channels: list[ChannelRequirement]

    async def preflight(self, ctx: ProcedureContext) -> list[Problem]: ...
    async def run(self, ctx: ProcedureContext) -> None: ...
    def widget(self, ctx: ProcedureContext) -> QWidget | None: return None
```

```python
class ProcedureContext:                           # capa/experiment/procedures/base.py
    clock: RunClock                               # single monotonic timebase
    config: ExperimentConfig                      # frozen run recipe (incl. config.method)
    bundle_writer: RunBundleWriter                # write_event from procedure layer
    databus: DataBus                              # in-process pub/sub
    logger: structlog.stdlib.BoundLogger          # bound with run_id / procedure_id
    external_stop: anyio.Event                    # SIGINT / abort button
    instruments: ChannelRegistry                  # frozen at run start
    adapters: dict[str, DeviceAdapter]            # introspection only (caps, resource_id)
    dispatcher: CommandDispatcher                 # ALL device writes go through here
    authorization: Authorization                  # stamps issued_by / authorization_id
    method_executor: MethodExecutor | None        # set when config.method present
    operator_commands: ObjectReceiveStream | None # UI control stream (None when headless)
    ui_sink: ProcedureUiSink | None               # UI-only telemetry (None when headless)
    metadata: dict[str, Any] | None               # cross-step scratchpad, not snapshotted
```

The procedure does not call adapter methods directly — `dispatcher.dispatch(...)` is the only sanctioned write path. This indirection lets a procedure run unchanged against either `ConductorDispatcher` (production: per-resource-worker, state-gated, recorded into the bundle) or `AdapterDispatcher` (in-loop direct call, used by tests). `bundle_writer.write_event(...)` is the procedure's path into `events.sqlite`; sink writes for sample data are the conductor's job.

**Method ↔ Procedure separation.** `MethodExecutor` is a service, not an abstract class. `RecipeRunner` is one line: `await ctx.method_executor.run_to_completion(ctx.method)`. A custom procedure can:
- ignore the method entirely
- run the method to completion (recipe runner pattern)
- run partial segments (`advance_until(step_id)`), interleave custom phases, then resume
- compose multiple methods

This is the flexibility researchers need for novel workflows without re-implementing segmented-profile logic.

**Discovery:**
1. Installed packages registering on the `capa.procedures` entry-point group.
2. Local `plugins/` folder — any module exporting a `Procedure` subclass is loaded at startup **only in dev mode**. Useful during development; disabled for production rig operation unless `CAPA_PLUGIN_MODE=production` is overridden or an explicit config flag is set.

**Trust policy.** Procedure plugins can command real hardware, so discovery is not the same as trust. Plugin mode is two-state — `dev` loads every contract-passing plugin and records drift for inspection; `production` refuses any drift. Production mode uses a `plugins.lock` file containing plugin id, package name, version, entry point, and distribution hash. Startup refuses an installed plugin whose hash/version differs from the lock (HASH_MISMATCH / VERSION_MISMATCH / ENTRY_POINT_MISMATCH / MISSING_FROM_LOCK all block load) unless the operator explicitly runs `capa plugins trust <id> --reason ...`, which writes the lock entry and appends an audit row to `plugins.lock.journal`. Mode resolves from `--plugin-mode` > `CAPA_PLUGIN_MODE` env > `dev` default. The lock snapshot is copied into every bundle and mirrored into `manifest.json.plugins`.

**Plugin contract is enforced at registration, not arm time.** When a plugin loads:
- The class is checked against the `runtime_checkable` `Procedure` protocol.
- `config_model` is verified to be a Pydantic `BaseModel` subclass.
- `required_capabilities` and `required_channels` are verified to be well-formed (parse, no unknown enum values).
- Plugin id collisions raise immediately.
- The plugin API version range is checked against capa's current plugin API.
- In production mode, the plugin is checked against `plugins.lock`.

A plugin with bad declarations fails to *load*; it never appears in the procedure picker. `preflight()` then handles run-specific checks against the *current* `HardwareProfile` (which devices the operator has connected for *this* run), returning a `list[Problem]` rather than raising; `Problem` has `code`, `message`, `severity`, `blocking`.

**Builtins.** All registered on the `capa.procedures` entry-point group.

- `capa.builtin.free_run` — record only, no method. Useful for ad-hoc captures.
- `capa.builtin.recipe_runner` — walks a `Method` via `MethodExecutor.run_to_completion`. 90% of standard runs use this.
- `capa.builtin.batch` — runs an inner procedure N times with auto-increment sample IDs and a configurable cooldown between runs. Each iteration spawns its own Conductor and writes its own bundle; the parent batch id is recorded in each child's `manifest.json.custom.batch.batch_id`. `fail_fast`, `cooldown_s`, and `sample_id_template` are configurable. Schema-rejects nested Batch.
- `capa.builtin.heat_flux_tune` — heater-to-flux tuning routine that converges the heater setpoint on a target heat flux at the specimen plane using a configurable controller; emits a `Calibration` artifact and surfaces an "approve and save into the active set?" gate to the operator. The supporting calibration-artifact emit/load + uncertainty plumbing lives in `src/capa/calibration/tune_artifact.py`.

A new routine is one file. Calibration routines are procedures whose final act is to emit a Calibration artifact and surface an "approve and save into the active set?" gate to the operator.

### 11.1 External event ingest (planned)

> **Status: not implemented.** The design below is the target shape; no
> ingest socket exists in the current codebase. External tools that need
> to add events today must do so through a Python plugin or post-run.

Some research workflows want to inject timestamped events from a sibling process — an external oxygen analyzer with its own polling, a video annotation tool, a lab-notebook macro. The planned ingest endpoint, opened while a run is active, will accept events through:

- **Local Unix-domain socket** (Linux) / **named pipe** (Windows) at `runs/<bundle>/.ingest.sock`
- Optional **HTTP loopback** on a configured port (off by default)

Both will accept newline-delimited JSON events: `{"t_utc": "...", "channel": "...", "kind": "annotation"|"event", "payload": {...}}`. Capa will stamp `t_mono_ns` from the conductor's `RunClock` at receipt (worse than producer-side stamping; the protocol allows the producer to provide its own `t_mono_ns_anchor` if it has one). Events land in the same `events.sqlite` everything else uses.

Ingest is **opt-in** at run-start so the default test path stays simple. Bind failures (stale socket, missing AF_UNIX) are non-fatal — the run logs an ingest-start-failed event and continues without ingest rather than aborting. The shutdown coordinator closes the listener so accept tasks exit cleanly when the procedure finishes.

This avoids forcing every external integration to become a Python plugin and keeps the bundle as the single source of truth for the run timeline.

---

## 12. Camera subsystem

Cameras are first-class devices but their data shape (frames, not scalar measurements) means they get their own subtree. Two distinct recording models live behind one Protocol:

- **Visible (capa-encoded).** capa pulls frames from the SDK and encodes via PyAV → MKV. capa owns the byte path.
- **IR thermal (Atlas-recorded).** The FLIR Atlas SDK writes `.seq` / `.csq` directly to a path we configure. capa **does not stream IR frame pixels through Python** during recording — the file is huge (1–20+ GB) and Atlas's native writer preserves vendor calibration metadata, atmospheric correction, emissivity, etc. that we'd lose by transcoding. capa only consumes a low-rate preview stream for the UI thumbnail and monitors recording health.

The `Camera` Protocol is intentionally separate from `DeviceAdapter`:

```python
class Camera(Protocol):
    name: str
    kind: Literal["visible", "ir_radiometric"]
    capabilities: frozenset[CameraCapability]
    recording_mode: Literal["capa_encoded", "sdk_recorded"]

    async def open(self) -> None: ...
    async def close(self) -> None: ...
    # For "capa_encoded" cameras: writer captures frames pulled by capa.
    # For "sdk_recorded" cameras: output_path is where the SDK should write.
    async def start_recording(
        self,
        *,
        clock: RunClock,
        writer: VideoSink | None = None,        # capa_encoded only
        output_path: Path | None = None,        # sdk_recorded only
    ) -> RecordingHandle: ...
    async def stop_recording(self) -> RecordingResult: ...   # final path, frame count, size
    async def preview_frame(self) -> np.ndarray | None: ...  # for UI thumbnail (low-rate)
    async def health(self) -> CameraHealth: ...
    def discover(self) -> AsyncIterator[CameraInfo]: ...     # if SUPPORTS_DISCOVERY
```

Lifecycle is `open` / `close` / `start_recording(path)` / `stop_recording` — *recording* is the explicit verb because cameras own their output container. Emissions are `FrameReceipt` records (one per frame, lightweight, posted from whichever thread the SDK callback runs on) and periodic `CameraHealth` snapshots, *not* `ChannelSample`. Discovery returns `CameraInfo` with model + serial + transport so the `serial` / `model_hint` selection rules can run without opening every camera.

`CameraCapability` is a `Flag` enum the UI uses to gate widgets and the engine's preflight uses to validate against profile requirements: `RADIOMETRIC`, `PALETTE`, `MEASUREMENT_SHAPES`, `SUPPORTS_DISCOVERY`, `MODEL_HINT`, `SERIAL_SELECT`, `LIVE_PREVIEW`, plus the FLIR control-surface flags (`NUC_TRIGGER`, `RADIOMETRIC_PARAMS`, `TEMPERATURE_RANGE_SELECT`, `AUTO_NUC_INTERVAL`, `REMOTE_PALETTE`, …).

### 12.1 FLIR IR adapter (`capa-flir`)

The FLIR adapter ships in a separate package, `capa-flir`, registered through the `capa.cameras` entry point. capa core contains the `Camera` Protocol, the webcam adapter, and the IR sim fixture; everything FLIR-specific lives in `capa-flir`. The split is motivated by the SDK license — see "Licensing posture" below.

- **Camera scope: any FLIR camera Atlas can drive over USB.** Reference / validation target is the **FLIR E85** (Exx handheld family, 384×288 radiometric, 30 Hz). The adapter is intentionally not E85-specific — Atlas presents Exx, Txxx, Axxx, and other FLIR families through the same `ACS_Camera` / `ACS_Stream` / `ACS_ThermalSequenceRecorder` surface, so any FLIR USB camera the operator has should work. Per-model differences (resolution, max frame rate, supported palettes, available measurement shapes) are read from the camera at `open()` and stored in `ir_cam0.meta.json`. capa-flir does not support non-FLIR thermal cameras — that constraint is set by the SDK license (§2.2(i)), not by capa.
- **Camera selection.** `CameraSpec` carries optional `model_hint: str | None` and `serial: str | None` fields. `discover()` enumerates all FLIR USB cameras via `ACS_Discovery_scan`; selection rules: if `serial` is set, require an exact match; else if `model_hint` is set, prefer a matching model and warn if multiple match; else pick the unique camera and fail clearly if more than one is connected.
- **Transport: USB.** Network/GigE FLIR cameras would also work via Atlas's network discovery, but capa-flir's first release is USB-only.
- **OS: Linux and Windows.** Atlas ships native binaries for both (`libatlas_c_sdk.so` / `atlas_c_sdk.dll`); the CFFI ABI binding (§12.2) is platform-independent — only the loader resolves the right shared library.
- **Recording strategy: a single, unified frame-pump path on both platforms.** Atlas's `ACS_Stream_attachRecorder` auto-attaches to the recorder for USB streams on Windows but *not* on Linux (the SDK headers explicitly mark USB attach as Windows-only). Rather than maintain two recording paths, capa-flir uses the manual pump everywhere:
  1. Open the camera, get its `ACS_Stream`, register an `ACS_OnImageReceived` callback.
  2. In the callback, call `ACS_ThermalSequenceRecorder_addImage(recorder, image)` to feed the recorder, and post a small `FrameReceipt` (`frame_idx`, `t_mono_ns`, `t_utc`, `capture_latency_s`) onto an AnyIO memory stream so the health monitor and frame-index sink run on the engine's event loop, not Atlas's streaming thread.
  3. The recorder still produces a proper FFF-encoded `.csq` — capa is just feeding it explicitly instead of letting Atlas auto-attach. Per-frame overhead vs. auto-attach is one C call (~µs), invisible at 30 Hz.

  This makes Linux and Windows behaviorally identical, halves the test matrix, and means the Linux-USB constraint is not a "workaround" — it's just how the adapter works.
- Output path: `runs/<bundle>/video/ir_cam0.csq` (or `.seq` if uncompressed is required for some downstream tool — configurable, default `.csq`).
- The frame-receipt stream feeds the video sink (§7.2) in real time, so `video/ir_cam0.frames.parquet` is built from per-frame receipts during the run rather than parsed post-hoc from `.csq` headers. The Conductor's writer thread owns the Arrow-IPC sidecar and the finalize-time Parquet rewrite, identical to the visible-camera path.
- Polls the SDK at ~5 Hz for: file size growth (health), frame count (health), live preview frame (UI). If file size hasn't grown for >2 s while recording is active, raises camera-recording-failure to `SafetyMonitor`.
- At `stop_recording()`: tells SDK to flush, waits for handle release, computes file size, optionally streams a sha256 in the background (10–60 s for 20 GB — non-blocking on finalize).
- **SDK choice: FLIR Atlas Multiplatform C SDK** (version 2.19.0 pinned; gcc11 x64 build on Linux, matching MSVC build on Windows). Atlas owns the native `.csq` writer and preserves vendor calibration metadata, atmospheric correction, and emissivity. Camera control and recording both go through Atlas. Spinnaker (FLIR's GenICam machine-vision stack) is *not* used here — it does not own the radiometric `.csq` recording path.

### 12.2 FLIR Atlas SDK ↔ Python binding

Atlas is shipped as a multiplatform **C** SDK (headers + `.dll` on Windows, `.so` on Linux; capa-flir pins version **2.19.0** on both platforms — gcc11 x64 on Linux, matching MSVC build on Windows). The Python ecosystem has several ways to call into a C library; for capa we use **CFFI in ABI (runtime) mode**.

**Why CFFI ABI mode is primary:**

| Option | Verdict | Reason |
|--------|---------|--------|
| **CFFI (ABI mode)** | **Primary** | No C compiler at install time. No FLIR headers needed in the source tree (the licensing-driven decision — see "Licensing posture" below). Per-call overhead is a libffi dispatch (~1 µs) — invisible at our 5 Hz health / 2 Hz preview rates. We hand-maintain the ~15 function signatures we use; trivial to keep in sync against a pinned SDK version. |
| CFFI (API mode) | Skip | Would compile against the headers at install time, requiring either vendored headers (forbidden by FLIR's confidentiality clause — see below) or a working gcc + a local SDK install on every operator machine. The type-safety win is small for a 15-function surface; the install-time complexity and licensing tangle are not worth it. |
| pybind11 / nanobind | Skip | Best when the SDK is C++; Atlas is C. Adds a C++ build dependency for no benefit here. |
| ctypes | Skip | Zero build pipeline, but verbose and brittle on struct layout / callback lifetimes. CFFI ABI mode wins on ergonomics with the same constraints. |
| Cython | Skip | Better suited to performance-critical inner loops; we're not in one. |
| Sidecar daemon | Reserved fallback | A small C executable (`capa-flir-bridge`) that owns the camera and exposes a UDS protocol — capa Python talks to it the same way external instruments use the §11.1 ingest endpoint. Use only if Atlas's threading model fights AnyIO or if FLIR objects to in-process binding distribution during App Review. |

**Plugin package layout — `capa-flir` (separate repo, not in capa core):**

```
capa-flir/
├── pyproject.toml               # entry: capa.cameras = "flir_ir = capa_flir:FlirIrAdapter"
├── NOTICE.md                    # SDK licensing posture; no-redistribution statement
└── src/capa_flir/
    ├── __init__.py              # exposes FlirIrAdapter
    ├── flir_ir.py               # high-level Camera Protocol implementation
    ├── _preview.py              # preview-frame rendering helpers
    ├── _atlas/
    │   ├── _loader.py           # resolves CAPA_FLIR_ATLAS_ROOT; dlopens libatlas_c_sdk.so / atlas_c_sdk.dll
    │   ├── _cdef.py             # ffi.cdef("""...""") — the ~15 function signatures we use
    │   ├── _atlas_api.py        # thin pythonic wrapper around the cffi binding
    │   ├── _frame_pump.py       # AnyIO bridge for the OnImageReceived callback (§12.1)
    │   └── _preview_cb.py       # Atlas preview-callback shim
    └── errors.py                # FlirAtlasError(AdapterError) + SDK-code translation
```

The wrapped surface is small (~15 functions: discovery, camera connect/disconnect, stream start/stop, recorder alloc/start/addImage/stop/free, error/log helpers, status getters, native-string helpers), so any of the fallbacks is a one-to-two-day port, not a rewrite.

**Threading.** Atlas's `ACS_Stream_start` callback runs on its own native thread. The adapter never does work in that callback — it only calls `ACS_ThermalSequenceRecorder_addImage` and pushes a `FrameReceipt` (`frame_idx`, `t_mono_ns`, optional preview pointer) onto an `anyio.streams.memory.MemoryObjectSendStream`. Everything else — health polling, preview rendering, frame-index building — runs on the engine's event loop. Each remaining blocking SDK call is wrapped in `anyio.to_thread.run_sync(...)`. Health polls at ~5 Hz, preview frames at ~2 Hz — both well within budget.

**Resource safety.** Every Atlas handle is owned by a Python object whose context-manager `__exit__` (and `__del__` as backstop) calls the corresponding SDK release. Atlas error codes are wrapped into a `FlirAtlasError(AdapterError)` carrying the SDK error name + message, so they surface in `events.sqlite` and the UI like any other adapter error.

**SDK install on the rig PC.** Atlas is *not* bundled with capa-flir's wheel. Operators install Atlas 2.19.0 separately and point capa-flir at it via `CAPA_FLIR_ATLAS_ROOT`:

- **Linux:** unpack the gcc11 x64 tarball (e.g. into `/opt/flir/atlas-c-sdk-linux-gcc11-x64-2.19.0/`), set `CAPA_FLIR_ATLAS_ROOT` to that path, prepend `$CAPA_FLIR_ATLAS_ROOT/lib` to `LD_LIBRARY_PATH`.
- **Windows:** install via the FLIR-provided MSI (or unpack the matching archive), set `CAPA_FLIR_ATLAS_ROOT` to the install root, ensure the `bin` directory is on `PATH` so the loader can find `atlas_c_sdk.dll` and its dependent DLLs.

The adapter's `discover()` validates that the platform-appropriate Atlas shared library is locatable and produces a clear error when missing.

**Licensing posture.** capa-flir is a separate package because the FLIR SDK License Agreement (v1.3 July 2020) imposes constraints that would otherwise contaminate capa's distribution model:

- **§5.1 — App Review.** Any App that uses the SDK must be approved in writing by FLIR before distribution. Keeping the binding in `capa-flir` means capa core is publishable freely; only `capa-flir` is gated by App Review.
- **§6 — Confidential Information.** SDK headers, sample code, and "non-public elements" are FLIR Confidential Information for 7 years post-termination. capa core's public repo therefore contains no FLIR headers, no FLIR sample code, no struct dumps. ABI mode means the binding source contains only function *signatures* we declare ourselves against the documented public API — no header text, no SDK internals.
- **§2.2(iii) — copyleft prohibition.** Both capa and capa-flir are permissively licensed (MIT/Apache-2.0). No GPL/AGPL/LGPL linkage anywhere on the import path.
- **§3.1 — no medical use.** capa is research instrumentation (controlled-atmosphere pyrolysis, materials testing); the docs explicitly state "not a medical device."
- **§8.1 — export controls.** Standard notice in capa-flir's README.
- **§11.2 — termination is fast** (60-day notice, 10-day cure). The plugin split means termination forces removal of `capa-flir` only; capa keeps shipping with webcam + IR-sim cameras, unaffected.

**Distribution.** capa ships as an open, permissively-licensed wheel on PyPI with no FLIR dependencies. capa-flir ships from a private package index to FLIR licensees only; the FLIR App Review submission for public distribution is tracked in §16 as an open decision.

### 12.3 Visible adapter (`webcam`)

PyAV-based H.264 → MKV. Per-frame receipt timestamps written into `visible_cam0.frames.parquet` via the same Arrow-IPC sidecar pipeline the IR adapter uses. Cross-platform (relies on FFmpeg). For most lab webcams this produces files in the tens-of-MB-per-hour range. The webcam adapter package (`src/capa/devices/camera/webcam/`) splits responsibilities into `adapter.py` (the Camera Protocol implementation), `descriptor.py` (device descriptors), `encoding.py` (PyAV encoder management), `probe.py` (UVC probing), and `constants.py` (preview cadence, JPEG sizing).

### 12.4 Bundle storage path for large IR files

Default: `.csq` lives inside the run bundle directory, alongside the rest. This keeps "bundle is self-sufficient" intact, and tar/zip archival still works (just slowly).

Escape hatch for sites where the bundle root is on a slow/small volume: `ExperimentConfig.cameras[*].output_root` can override the IR file location. The `manifest.json` then records both the path and the bundle-relative reference. Trade-off: bundle copy/move requires bringing the IR file along separately. Off by default.

### 12.5 Frame-to-DAQ correlation

All timestamps derive from `RunClock`. Every camera writes a `<name>.frames.parquet` with `(frame_idx, t_mono_ns, t_utc, capture_latency_s, camera)`, so visible and IR analyses share the same join key against `scalars.parquet`. The run-start UTC anchor is embedded in MKV container metadata and stored as `started_utc` in `manifest.json` so external tools can re-correlate by absolute time.

### 12.6 Health, failure policy, preflight

Camera tasks run inside the Conductor's task group. Each camera has a configurable `on_failure` policy (`warn` | `abort_run` | `safe_shutdown`). Disk-space preflight: before arming, capa checks free space against `projected_size = duration_s * estimated_bps_per_camera` and refuses to arm if the margin is below a configured threshold (default: 1.5×).

---

## 13. Errors, recovery, and observability

### 13.1 Structured logging

Application logs use `structlog` configured to emit JSON. Every log line carries a context-bound `run_id`, `procedure_id`, `step_id` (when applicable), and `operator_id`. During a run, logs are tee'd to:

- **stdout** (human-friendly console renderer for headless / dev),
- **`run.log`** in the bundle (JSON lines, captured for archival debugging),
- **the status bar / events dock** (WARNING and above only).

The status bar is *not* the only feedback channel; INFO-level operational detail goes to `run.log` so a post-mortem has the breadcrumbs without flooding the operator UI. The `metrics` module periodically logs queue-depth/lag samples at DEBUG; these aggregate into the `queue_health` block of `manifest.json` at finalize.

Crash logs from before run-start (config errors, plugin load failures) go to `~/.capa/logs/capa-YYYYMMDD.log` since there is no bundle yet to write into.

### 13.2 Errors

- **All device writes require authorization.** Scheduled method writes inherit the arm/start approval; manual writes go through `confirm=True`. UI exposes manual confirm as a checkbox-then-button pattern, never auto-yes. Every command carries `issued_by`, `authorization_id`, and/or `confirmed_by` operator IDs (§9); all are recorded in `events.sqlite` for full provenance.
- **Typed errors.** Each library already exposes a typed exception hierarchy (`SartoriusError`, `WatlowError`, ...). Adapters re-raise into a `capa.devices.AdapterError` that adds the channel/device context. UI error toasts read from these.
- **Watchdog.** Each producer task reports last-sample time; the per-worker watchdog state is the canonical "is this producer alive" view, and writes `device_silent` events. The planned `SafetyMonitor` (§9.2) will consume this view to apply configured actions.
- **Task-group unwrap.** anyio 4.x wraps in-task-group exceptions in a `BaseExceptionGroup`; the Conductor's `try/except` chain unwraps single-exception groups so `ProcedureError` (from preflight) and `BackpressureAbortError` route to the same `run_status="aborted"` / `run_status="crashed"` paths that pre-task-group raises do.
- **Abort vs. safe-shutdown.** Abort = immediate cancel request + finalize. Safe-shutdown = a procedure honors a configured cooldown phase first (heaters to safe temp, flows to zero, hold) and *then* finalizes. The current UI exposes Stop and hold-to-confirm Emergency buttons with fixed audit reasons.

### 13.3 Crash recovery and finalize-in-place

Bundles are openable mid-write because Arrow-IPC sidecars are flush-bounded, SQLite is journaled, and the manifest's `ended_utc` is null until clean exit. On startup, `runs.sqlite` flips any open-but-orphaned run to `run_status="crashed"` so the operator sees what happened.

For a crashed bundle, capa offers **finalize-in-place**:

- Read the in-flight Arrow-IPC streams and rewrite into the well-sized `scalars.parquet` and `device_records/*.parquet` (§8.5).
- Walk `events.sqlite` to recover the last good `t_mono_ns`.
- Set `ended_utc` to the wall-time of the last sample (with an `inferred_ended_utc: true` flag in the manifest).
- Compute checksums and write `manifest.sha256`.
- Mark `run_status: "crashed"` and seal the recovered bundle if integrity generation completes.

This is exposed both in the Review tab and as `capa finalize RUN_ID` (§14). The result: a crashed run does not leave a half-broken artifact lying around — it leaves a sealed artifact with `run_status: "crashed"` that the same analysis tooling can read.

---

## 14. Headless operation and CLI

The runtime runs without the GUI — for testing, CI, automation, and reproducibility. The CLI is a first-class surface, not a debugging afterthought. Headless mode is built on `run_headless()` ([`src/capa/runtime/headless.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/headless.py)), which constructs a `WorkerPool` + per-run `Conductor` exactly as the GUI does and then drives one run to completion.

The canonical command listing is `uv run capa --help`; this section sketches the shape. Per-command pages live under `docs/cli/`.

```
capa validate CONFIG
    Pydantic-validate the config; resolve plugin refs. Exits non-zero on
    any problem. Use this in version control / pre-commit hooks.

capa validate --strict CONFIG
    Same as above, plus a non-disruptive read-only handshake with each
    declared device (open + identify + close — no setpoint writes). Surfaces
    wiring errors before arming. Use this for pre-arm operator confidence;
    do NOT use in CI (requires real hardware).

capa run CONFIG [--headless | --gui]
    Run the experiment. --headless is the default; --gui launches the
    embedded GUI. Writes a bundle either way. Logs to stdout (human) and
    to run.log in the bundle (JSON). Exit codes:
      0 completed + sealed cleanly
      1 aborted
      2 crashed
      3 verification_failed
      4 preflight refusal

capa gui [CONFIG]
    Launch the GUI directly. Loads CONFIG if given, otherwise opens empty.
    Equivalent to `capa run CONFIG --gui` but does not require a config up front.

capa catalog list [--run-status ...] [--bundle-status ...] [--since DATE] [--json]
    Print indexed runs from runs.sqlite.

capa catalog rebuild
    Re-scan the runs directory and rebuild runs.sqlite from bundle manifests.

capa catalog verify RUN_ID
    Re-walk a bundle's artifacts and verify against manifest.sha256. Flags
    drift in the catalog as integrity_status = mismatch.

capa finalize RUN_ID
    Idempotent: rewrite in-flight Arrow-IPC streams, compute checksums, set
    ended_utc, progress bundle_status through finalized_unverified to sealed
    or verification_failed. Safe to run on already-sealed bundles (no-op).

capa devices discover
    Run each adapter's discover() and print findings. No run is created.

capa plugins list
    Print discovered plugins, trust status, API compatibility, package
    version, distribution hash, drift report, and rejection reasons.

capa plugins trust PLUGIN_ID --reason "..."
    Write a PluginEntry into plugins.lock and append an audit row to
    plugins.lock.journal. Never run implicitly.

capa method validate FILE
    Pydantic-validate a standalone method TOML/YAML file.

capa profile validate CONFIG
    Validate the domain-profile metadata block of an experiment config
    (required channel mappings, atmosphere consistency, etc.) without
    opening hardware.

capa config ...
    Validate and inspect experiment configs (sub-commands; see --help).

capa hardware ...
    Author and probe hardware profiles (sub-commands; see --help).
```

**Planned, not implemented today:** `capa export-parquet RUN_ID [--channels ...] [--time-range ...]` (focused Parquet export from a sealed bundle) and `capa export-rocrate RUN_ID [--license LICENSE]` (RO-Crate metadata generation for FAIR/Zenodo publishing). Both are tracked in §16.

Headless mode is the primary substrate for integration tests and for any future scripted workflows ("run the calibration suite nightly"). All CLI subcommands use the same structured logger as the runtime, so their output joins the same observability story.

---

## 15. Testing strategy

### 15.1 Unit tests

- Pydantic config validation (round-trip YAML/TOML, version migrations).
- Channel registry resolution and freeze-at-run-start invariants.
- SourceBinding extraction from native library records: Alicat wide `DataFrame`, Watlow parameter rows, Sartorius reading rows, NI polled `DaqReading`, NI `DaqBlock`.
- Calibration evaluation (every Calibration variant against canned points, including uncertainty propagation).
- CustomCallable calibration provenance: reject unversioned callables; snapshot entry point, distribution hash, serialized parameters, and test vectors.
- Unit validation (pint round-trip; reject ill-typed unit strings; reject dimensional mismatches between Calibration input/output and ChannelSpec).
- Method segment scheduling (timing, end-conditions, prompt acknowledgement).
- Safety rules (each rule type with edge-case inputs).
- Parquet sink: append, flush, row-group sizing, schema stability across runs, two-stage finalize rewrite.
- Device-record sidecars: preserve library-native row fields and schema-lock behavior without corrupting normalized `scalars.parquet`.
- Events sink: transactional commit, crash mid-write recovery.
- Integrity: sha256 table generation, `capa catalog verify` round-trip, mismatch detection on a tampered file.
- Run catalog: insert, finalize, crash-flip on startup.
- Provenance: manifest contains capa version + git sha + lockfile hash + plugin list.
- Finalize-in-place: kill mid-run, run `capa finalize`, assert the resulting bundle reads cleanly and `inferred_ended_utc` is set.
- Procedure plugin loading (entry-point + dev folder; version constraints; bad plugin fails at *load*, not arm).
- Plugin trust policy: production mode refuses unlocked plugins; dev-folder loading is gated; `plugins.lock` hash drift is visible.
- CAPA pyrolysis profile validation: missing required channel groups (heater_setpoint, heater_pv, sample_temperature, purge_gas_flow), atmosphere-mode/reactive-gas inconsistency, missing leak-test timestamp, and absent specimen form fail preflight.
- Ring buffer decimation and wrap-around.
- Backpressure policy enforcement (BLOCK actually blocks; DROP_OLDEST actually drops; saturation deadline trips a safe_shutdown).
- External event ingest: events submitted via the UDS / HTTP loopback land in `events.sqlite` with correct timestamps.
- FLIR Atlas binding (sim): `capa_flir._atlas._atlas_api` against `capa.devices.sim.flir_ir_sim` in-process fixture — open/start/stop lifecycle, error-code translation, RAII handle release on exception, threading via `anyio.to_thread.run_sync`. No real SDK required.

### 15.2 Integration tests

All run against simulated adapters — no hardware required. UI tests use **`pytest-qt`** with `qasync` integration so async procedure tasks and Qt signals coexist in the test loop.

- `capa run --headless freerun.yaml` produces a bundle; bundle reads back and validates.
- Multi-device run with simulated NI + Watlow + Alicat + Sartorius; verify channel routing, calibration, sinks, events.
- Verify that the same run writes both normalized `scalars.parquet` and native `device_records/*.parquet`, and that `source_record_id` links sampled channels back to their source records.
- Method execution: hold/ramp/step/wait/prompt/safe_shutdown all traversed.
- Procedure that uses `MethodExecutor.advance_until()` partially, runs custom phase, resumes.
- `Batch` builtin: 3 replicates, each producing its own bundle, all linked by parent batch id in the catalog.
- Writer crash mid-run: kill the engine, restart, run `capa finalize`, verify bundle reads cleanly with `inferred_ended_utc` set, `run_status: crashed`, and `bundle_status: sealed`.
- Safety: induce each fault type via simulator; verify safe-shutdown executes.
- CAPA pyrolysis end-to-end: `tests/integration/test_capa_recipe_run.py` exercises the headless path including the dynamic preflight phase inside the task group and the verbatim profile snapshot in the sealed bundle.
- UI smoke test (`pytest-qt`): bring up MainWindow against simulated adapters, click through Arm → Start → Abort, assert state transitions and that the resulting bundle is valid.

### 15.3 Performance regression tests

A separate suite (run on every PR, not just nightly) that asserts performance budgets. Without these, regressions creep in silently.

- 60 Hz × 30 channels × 5 minutes against sim adapters.
- Assertions: max fan-out queue depth ≤ N; writer-lag p99 ≤ 100 ms; UI repaint interval ≤ 150 ms; CPU < threshold; memory growth bounded.
- Output: queue-health histogram per run, archived as a CI artifact for trend tracking.

A failing perf test blocks PR merge the same as a failing functional test. Budgets live in `tests/performance/budgets.toml` and can be tuned with explicit review.

### 15.4 Hardware smoke tests

Gated behind `CAPA_HARDWARE_TESTS=1`; otherwise skipped. Run on the rig PC.

- Watlow: read PV; set safe setpoint (no-op or trivial delta); read back.
- Alicat: read flow; set safe setpoint; read back.
- Sartorius: read mass; tare/zero behind explicit operator gate.
- NI DAQ: discover devices; one short hardware-clocked acquisition; verify sample timestamps.
- Visible camera: open, record 5 s, verify frame count and timestamps.
- IR camera (Atlas SDK): open via CFFI binding, record 5 s to `.csq`, verify file growth, frame count, per-frame receipt parquet, and clean release of all SDK handles.

### 15.5 Continuous integration

- All unit + integration + performance tests run on every PR (Linux + Windows runners, Python 3.12 and 3.13).
- **Static checks on every PR:** `ruff check`, `ruff format --check`, `mypy --strict src/capa`.
- Hardware tests run manually on the rig PC before tagged releases.
- A `capa validate` step runs against every example YAML in `configs/` to ensure they stay valid as the schema evolves.
- A `capa validate` pre-commit hook is shipped in the repo (opt-in) so config files do not regress between commits.

---

## 16. Roadmap

The engine, builtins, plugin runtime, CAPA pyrolysis profile, the per-resource worker runtime, the bundle writer (including frame-index parquets for every camera), the Method editor UI, and the headless + GUI run paths are all in place. The CAPA sim configs drive an end-to-end integration test from disk with no Python fixtures, and `capa-flir` (FLIR Atlas binding + `FlirIrAdapter`) is implemented as a sibling package consumed via the `capa.cameras` entry point. What remains, in rough priority order:

**Calibration workflows.** A calibration manager UI (with uncertainty surfaced), the in-tree `heat_flux_tune` procedure (currently in progress on the working branch) finished and wired to the manager, an `EmissivityRamp` builtin, and an "approve and save into the active set?" gate that writes a fitted Calibration (with documented fit metadata + procedure pedigree) into the active `CalibrationSet`. The supporting plumbing — calibration snapshot in the bundle, uncertainty propagation, `CustomCallable` provenance — is already in place.

**SafetyMonitor activation.** The per-worker watchdog already detects silent producers and writes `device_silent` events; the rule set in §9 (heater max temp, ramp-rate windows, mass/flow bounds, camera failure, writer-lag, disk-low, emergency-stop) is declarative and ready. What is left is the `SafetyMonitor` task that consumes `adapter.watchdog_state()` plus the channel-sample stream, evaluates the rules, and dispatches `warn` / `pause_method` / `abort_run` / `safe_shutdown` actions through the Conductor.

**Review and polish.** The Review tab and run-catalog browser (completed-run summary with queue-health histograms and the sealed/unverified distinction visible), per-user theme + window-state persistence, Simple/Expert mode presets, the `capa export-parquet` and `capa export-rocrate` CLIs, a Linux smoke test exercising the same path as Windows, and an end-to-end exercise of the schema-version migration registry.

---

## 17. Open decisions

1. **Plugin trust workflow owner.** The technical primitive ships (`capa plugins trust <id> --reason ...` writes the lock + an audit journal); the policy question — who is allowed to run that command on the rig PC and how the review is documented — is open. Decide before flipping `CAPA_PLUGIN_MODE=production` on the rig.
2. **`capa-flir` public-distribution timing.** If/when capa-flir is to be distributed beyond UMD, submit the App-Review package to FLIR (§12.2 / SDK §5.1). Not blocking ongoing capa-flir development; only blocks public release of capa-flir. Decide who owns the submission and on what timeline.

---

## 18. Summary of design principles

1. **Channels are the universal binding unit.** UI, sinks, plots, calibrations all key off channel names. Devices are an implementation detail beneath.
2. **One Protocol per concern, mocked from Day 1.** `DeviceAdapter`, `Camera`, `Procedure`, `Sink` — every seam has a Protocol and a simulated implementation.
3. **Preserve native records, derive channel samples.** The device libraries emit meaningful row/block shapes. Capa keeps those in `device_records/` and derives normalized `ChannelSample`s for UI, safety, procedures, and cross-run analysis.
4. **Files do what they're best at.** Parquet for normalized channel samples and native device records, SQLite for events, JSON/TOML for metadata, MKV for visible video, native `.csq` for radiometric IR, optional TDMS for kHz NI DAQ. No monolithic format, no transcoding for transcoding's sake.
5. **Bundle is self-sufficient, versioned, and verifiable.** Config, method, calibration snapshot (with uncertainty), equipment, software-environment provenance (capa version + git sha + lockfile + trusted plugin set), native records, normalized scalars, events, video, and a sha256 table over every file — all in one directory, schema-versioned for forward compatibility, optionally RO-Crate-described for FAIR/Zenodo publishing.
6. **Units are dimensions, not strings.** Every unit is `pint`-validated; calibrations declare input/output dimensions; mismatches fail at config-load.
7. **Uncertainty is part of the contract.** Calibrations carry documented uncertainty (or an explicit `None`); derived channels propagate it; the bundle preserves both.
8. **Backpressure is named, not implicit.** Every queue declares `BLOCK` / `DROP_OLDEST` / `ABORT_RUN`. Durable storage never silently loses data; UI never blocks acquisition; safety has its own queue so a slow disk cannot starve fault detection.
9. **Method ↔ Procedure are separable.** `MethodExecutor` is a reusable service; procedures may use, augment, partially run, or ignore methods.
10. **Safety is its own subsystem.** Not a check buried in a callback. Has its own task, its own state, its own escalation path. Hardware interlocks exist independently and are not replaced or proxied.
11. **Plugins, not patches — but trusted.** New routines, new IR cameras, new sinks all enter as plugins with a Pydantic config model and a Protocol implementation. Plugin contracts are enforced at load time; production plugins are checked against `plugins.lock`; bad or untrusted plugins do not reach the picker.
12. **Auditable by default.** Every device write is attributable (`issued_by` plus run authorization or manual `confirmed_by` operator IDs). Every artifact is hashable. Every run carries the exact environment that produced it.
13. **Observable, not just logging-capable.** Structured logs, queue-depth and writer-lag metrics, performance-regression budgets — all instrumented from Day 1, not bolted on after the first regression bites.
14. **Headless is first-class.** The runtime runs without the GUI; the CLI exposes `validate`, `validate --strict`, `run` (headless by default; `--gui` for the embedded GUI), `gui`, `catalog`, `finalize`, `devices discover`, `plugins`, `method validate`, `profile validate`, plus `config` and `hardware` utilities. Bundle export commands (`export-parquet`, `export-rocrate`) are planned (§14, §16).
15. **The supervisor doesn't pretend to be real-time.** Closed-loop control lives on the instruments. Python's job is to issue setpoints, watch, and record.
16. **One resource, one thread, one loop.** Every hardware resource runs on its own thread and asyncio loop, hosted in a `WorkerPool` that survives across runs; a per-run `Conductor` arms the pool, drives sampling, and disarms on stop. The UI never owns I/O.

---

## 19. External design references

- FAIR data principles: https://www.go-fair.org/fair-principles/
- FAIR for Research Software (FAIR4RS): https://doi.org/10.15497/RDA00068
- RO-Crate specification: https://www.researchobject.org/ro-crate/specification.html
- NIST metrological traceability policy: https://www.nist.gov/calibrations/traceability
- DuckDB Parquet performance notes: https://duckdb.org/docs/current/guides/performance/file_formats
- SQLite atomic commit / durability notes: https://www.sqlite.org/atomiccommit.html
- Qt threads and QObjects: https://doc.qt.io/qt-6/threads-qobject.html
- FLIR Atlas SDK product page: https://www.flir.com/products/flir-atlas-sdk/
