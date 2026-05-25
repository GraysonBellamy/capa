# Writing a device adapter

**Audience:** integrators adding support for a new device family (a new mass flow controller, a new balance, a new analog DAQ).
**Scope:** the `DeviceAdapter` contract, how `resource_id` interacts with the worker model, and how to land your adapter in capa today.

---

## Status: contract is real, descriptor plugins are young

Read this carefully before you start.

The `DeviceAdapter` Protocol in [`src/capa/devices/adapter.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/adapter.py) is production-quality and stable. Every shipped adapter (watlow, alicat, sartorius, nidaq, webcam, FLIR sim) satisfies it, and the engine routes commands and emissions through this surface.

What is younger is **how** an adapter gets registered. The registry is descriptor-driven: an adapter module exports an `AdapterDescriptor` and registers it. Built-in modules are imported eagerly, and installable packages can expose descriptor entry points under `capa.adapters` (devices) or `capa.cameras` (cameras). Setup and discovery surfaces call `ensure_adapters_loaded()` and will see those descriptors.

The caveat: adapter descriptors are **not** governed by `plugins.lock`, and the headless runtime path does not run the procedure-plugin discovery step. Runtime materialization can lazily import dotted module-path adapter ids, so a dotted module path with a module-level `DESCRIPTOR` is still the safest production shape. Short entry-point ids work after the registry has been loaded by Setup/discovery, but treat that as newer integration surface and test it on your deployment.

**Implication:** writing an adapter today is most useful if you intend to upstream it, fork capa for your own deployment, or ship a descriptor entry point with a careful site-level validation pass. The contract documented here is the part expected to stay stable.

The shipped adapters are also the best learning resource:

| Family | Source | What it teaches |
|---|---|---|
| Watlow | [`src/capa/devices/watlow.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/watlow.py) | Setpoint + PV with hardware-side PID; `HAS_SETPOINT`/`HAS_RAMP`. |
| Alicat | [`src/capa/devices/alicat.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/alicat.py) | Multi-parameter device on RS-485 multi-drop bus — multiple adapters per `resource_id`. |
| Sartorius | [`src/capa/devices/sartorius.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/sartorius.py) | Stability flag and the cold-open race. |
| NI-DAQ | [`src/capa/devices/nidaq.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/nidaq.py) | Hardware-clocked task, polled vs block mode, `HARDWARE_CLOCKED`/`EMITS_BLOCKS`. |

Read the family closest to yours before opening this page's contract section.

---

## The contract

```python
@runtime_checkable
class DeviceAdapter(Protocol):
    name: str                                       # operator-assigned device name
    capabilities: frozenset[Capability]             # what the device can do
    resource_id: str                                # see "resource_id" below

    async def open(self) -> None: ...               # establish connection (idempotent)
    async def close(self) -> None: ...              # release connection (idempotent)
    async def start(self, ctx: AdapterStartContext) -> None: ...  # arm sampling
    async def stop(self) -> None: ...               # disarm without closing
    async def snapshot(self) -> DeviceEmission: ...

    def stream(self) -> AsyncIterable[DeviceEmission]: ...
    async def command(self, cmd: DeviceCommand) -> CommandResult: ...

    @property
    def expected_emission_rate_hz(self) -> float | None: ...
```

Every method has a precise contract. The Protocol is in the source; treat the source as authoritative and this page as orientation.

### The lifecycle: open / close / start / stop

These are two separate axes. `open`/`close` is the **connection** — opening a serial port, querying device identity, configuring baud rate. `start`/`stop` is the **sampling** — arming hardware-clocked tasks, beginning the stream. Separating them lets the engine recover from a transient I/O hiccup by stopping and restarting sampling without renegotiating the connection. It also lets the engine arm hardware-clocked tasks at run-start rather than at adapter-open.

Both `open` and `close` must be **idempotent**. A second `open()` call on an already-open adapter is a no-op, not an error. A second `close()` on an already-closed adapter is similarly a no-op. The `AdapterLifecycle` helper in `adapter.py` is the reference state machine — most sim adapters delegate to it; real adapters do the same dance against their device library.

### `start` takes a context

```python
@dataclass(frozen=True, slots=True)
class AdapterStartContext:
    clock: RunClock
    run_id: str
    bundle_root: Path
    recording_enabled: bool = True
```

You read only the fields you need:

- `clock` — the authoritative `RunClock` for the run. Use it to stamp every emission's `t_mono_ns`. Do not call `time.monotonic_ns()` directly — the run clock may be a proxy.
- `run_id` — stable run identifier. Most adapters do not need this; camera adapters use it to compute per-run output paths.
- `bundle_root` — filesystem root of the bundle. Only camera adapters write directly here; everyone else ignores it.
- `recording_enabled` — set to `False` when the engine has decided this adapter's output should not be persisted (a `plan_capture` filter). Today only the camera adapter consults this; data adapters can ignore it (the engine filters their emissions downstream).

### `stream` is an async iterable

```python
def stream(self) -> AsyncIterable[DeviceEmission]: ...
```

This is the main path for measurement data. The contract is intentionally minimal: yield emissions while sampling is active, stop yielding when `stop()` has been called. The shape backs every shipped adapter's `recorder.stream()` from its library — capa does not impose a polling cadence; the underlying library determines it.

`DeviceEmission` is the discriminated union of every emission shape capa supports — `SourceRecord` for native device rows, `ChannelSample` for normalised long-format rows, plus camera-specific shapes. Most adapters yield `SourceRecord` (the native row from the device library) and let the engine downstream demultiplex into `ChannelSample`s.

### Emission shapes: wide / long / single-value / block

The `DeviceRecordsSink` in [`src/capa/storage/device_records_sink.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/device_records_sink.py) writes one parquet file per adapter family, preserving the library's native row shape:

| Shape | Example | Stored as |
|---|---|---|
| wide row | Alicat: one row, many columns (pressure, temperature, density, flow) | `device_records/alicat.parquet` |
| long row | Watlow: one row per (timestamp, channel, value) tuple | `device_records/watlow.parquet` |
| single_value_row | Sartorius: one row, value + stable flag | `device_records/sartorius.parquet` |
| block | NI-DAQ block mode: large arrays | deferred (planned TDMS sidecar) |

The shape is declared on the `SourceRecord` your adapter emits. Pick the shape that matches your library's natural emission — do not normalise into long-format in the adapter. The reason for preserving native shape is exactly that downstream analysts shouldn't have to guess how the library originally laid out its data; the normalised `scalars.parquet` is the cross-adapter view.

If your library emits blocks (e.g. NI-DAQ in block mode), the sink will currently log and skip them — block-shape persistence is wired in to the contract but not to the sink. Until that lands, block-mode adapters must either: (a) chunk their blocks into row emissions inside `stream()`, or (b) accept that the native parquet is not produced for now (the engine still derives `ChannelSample`s downstream).

### `snapshot` is for status, not data

```python
async def snapshot(self) -> DeviceEmission: ...
```

A snapshot is a one-shot read of current health / config — firmware version, controller mode, alarm bits. It is routed to `status.sqlite`, never to the main fan-out. The engine calls `snapshot()` at arm time (to capture the device state at run start) and on demand from the diagnostics dock. Snapshots are not part of the streaming data path; do not emit measurement data from here.

### `command` is the write path

```python
async def command(self, cmd: DeviceCommand) -> CommandResult: ...
```

Every device write — setpoint changes, tare commands, parameter writes — goes through this method. The `DeviceCommand` arrives pre-stamped with `issued_by`, `authorization_id`, and (for manual overrides) `confirmed_by`. The adapter dispatches on `cmd.kind` (a verb string like `"set_setpoint"` or `"tare"`), reads `cmd.target` and `cmd.payload`, and returns a `CommandResult` with `accepted: bool` plus an optional `detail` string.

Concrete adapters typically also expose typed methods (`WatlowAdapter.set_setpoint(...)`) for IDE help. Those typed methods build a `DeviceCommand` internally and call `command()` — the generic `command` is the supported plugin entry point.

A few non-obvious requirements:

- **Refuse commands without authorization.** A `DeviceCommand` with `authorization_id is None` and `confirmed_by is None` is a misuse (a manual override that did not get the operator's confirmation click). The adapter must reject it with `accepted=False`.
- **Return promptly.** Commands run on the engine task — a long-blocking `command()` is exactly the kind of thing that trips the saturation deadline. Acknowledge fast; if the device takes seconds to react, log a follow-up event after the device confirms.
- **Surface device-side rejection in `accepted=False`.** A controller that says "I will not accept this setpoint while in manual mode" should not raise — it should return `CommandResult(accepted=False, detail="device in manual mode")`. The executor logs the rejection and continues.

### `resource_id` and the worker model

```python
resource_id: str  # "<scheme>:<body>"
```

This is the most important field for getting concurrency right. Two adapters that share a physical resource — a serial port on an RS-485 multi-drop bus, a DAQmx chassis, a single camera handle — **must** expose the same `resource_id`. The engine groups adapters by `resource_id` into worker threads, so adapters that share a resource cannot stomp on each other.

The format is `<scheme>:<body>` where `scheme` is one of:

| Scheme | Body | Used by |
|---|---|---|
| `serial` | port name | watlow, alicat, sartorius |
| `daqmx` | chassis/device name | nidaq |
| `webcam` | serial number | webcam |
| `sim` | adapter-chosen name | simulators |

Two practical rules:

- **`resource_id` must be readable before `open()`.** It is computed from constructor inputs without I/O. The engine reads it during arm planning, before any adapter has been opened.
- **Adapters that do not share a resource must have different `resource_id`s.** Two Watlow controllers on two different serial ports get `"serial:COM3"` and `"serial:COM5"`. Two Alicat devices on the same RS-485 bus share `"serial:COM3"` and end up in one worker.

### `expected_emission_rate_hz`

```python
@property
def expected_emission_rate_hz(self) -> float | None: ...
```

A hint for engine queue sizing — total emissions per second this adapter will produce once `start()` is running, summed across `SourceRecord` and per-bound-channel `ChannelSample`s. Return `None` if you do not know; the engine falls back to a conservative default and logs the fallback.

This number is used to size the bounded inter-thread bridge between your worker and the conductor. If you lie too low, the bridge will saturate and trip the [saturation deadline](../safety/saturation-and-deadlines.md); if you lie too high, you waste a little memory.

Compute this *after* the engine has called `configure_channels` (the optional hook your adapter implements to learn which channels are bound). Before that, you do not know how many `ChannelSample`s you will emit per record.

---

## Capability flags

```python
class Capability(Flag):
    HAS_SETPOINT
    HAS_RAMP
    HAS_TARE
    HAS_ZERO
    HARDWARE_CLOCKED
    EMITS_BLOCKS
    SUPPORTS_DISCOVERY
    READS_PROCESS_VAR
    WRITES_DIGITAL
    HAS_GAS_SELECT
    EMITS_STABILITY_FLAG
    SUPPORTS_AUTO_RECONNECT
    HAS_INTERNAL_CAL
    HAS_PARAMETER_CONFIG
    HAS_TOTALIZER
    HAS_VALVE_HOLD
    HAS_DISPLAY_CONTROL
```

These flags drive both the UI (an Alicat adapter that declares `HAS_GAS_SELECT` gets a gas-select widget on its tile) and procedure preflight (a procedure that declares `required_capabilities=("HAS_SETPOINT",)` is rejected if no adapter declares that flag).

Two rules of thumb:

- **Declare conservatively.** A flag means "I support this safely." A balance that has an `HAS_INTERNAL_CAL` flag commits to "operators can ask me to internally calibrate without consequences I do not know about." If the operation has consequences (e.g. moves the load cell), do not declare the flag — let it be a manual-override path.
- **Skip flags you do not use.** Capacity flags are not free metadata; each one implies UI behaviour. If your device has a totalizer but capa has no use for it in your deployment, do not declare `HAS_TOTALIZER` — the UI will render a widget you cannot back.

---

## Safe-shutdown obligations

`close()` must leave the device in a state that is safe to walk away from. For control devices (heaters, MFCs), that means commanding outputs to their safe value — typically zero — before releasing the handle. Capa cannot do this for you; the device library is the only thing that knows what "safe" looks like for the hardware.

This is the dual of [Shutdown sequence](../safety/shutdown-sequence.md). The runtime's shutdown handler calls `close()` on every adapter; if your `close()` does not drive outputs to safe values, the rig stays hot after the run ends. There is no second chance.

A concrete sequence for a heater controller:

1. Stop sampling (`stop()` was likely already called by the engine).
2. Command the setpoint to zero (or the controller's documented safe value) and wait for an acknowledgment.
3. Optionally drive the controller into manual / standby mode.
4. Release the connection.

The watlow adapter is the cleanest reference here; read its `close()` implementation.

---

## Discovery hooks

Adapters that support discovery (`SUPPORTS_DISCOVERY` capability) implement a `discover()` classmethod that returns a list of detected devices. `capa hardware discover` routes through the descriptor registry and includes cameras and descriptor plugins; `capa devices discover` uses the same path but stays scoped to non-camera adapters for older scripts.

The shape varies by family — watlow scans serial ports for valid Modbus responses, NI-DAQ asks DAQmx for connected chassis, the webcam adapter enumerates DirectShow devices. Match your library's natural discovery surface; capa expects the result to be a list of dicts with a stable `resource_id` and human-readable `name`, `model`, and `serial` fields.

---

## Testing with a fake transport

The shipped adapters are tested against fake transports that satisfy the same library interface the real device satisfies. This is the cleanest way to exercise the adapter without hardware:

- Build a fake recorder / driver that yields `SourceRecord`s on demand.
- Inject it into your adapter through the constructor.
- Drive `open`/`start`/`stream`/`stop`/`close` and assert against the emissions and against the events the engine writes.

Look at `tests/unit/test_*_adapter.py` in the capa repo (or the corresponding test in the matching `*lib` package) for the pattern your library probably already supports.

For higher-fidelity testing, the `capa.devices.sim` simulators can be wired into a real engine task group — that exercises the whole stack from adapter → worker → conductor → writer. Integration tests in `tests/integration/` use this pattern.

---

## Registering today

Your adapter must contribute an `AdapterDescriptor` to the registry in [`src/capa/devices/registry.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/registry.py). There are two current paths.

**Core or forked deployment:** put the descriptor next to your adapter module, call `register(DESCRIPTOR)` at import time, and use the dotted module path as the `adapter` value in `hardware.toml`. `require_descriptor()` imports dotted module paths lazily, so this path works in headless runs without a prior Setup/discovery pass.

**Installable package:** expose the descriptor through an entry point:

```toml
[project.entry-points."capa.adapters"]
"lab.power_supply" = "lab_supply.capa_adapter:DESCRIPTOR"

# or, for camera descriptors:
[project.entry-points."capa.cameras"]
"lab_ir" = "lab_ir.capa_camera:DESCRIPTOR"
```

The target may be an `AdapterDescriptor` instance or a callable that returns one. Setup and `capa hardware discover` load these groups. They do not appear in `capa plugins list`, and they are not added to `plugins.lock`.

For deployments where you want the adapter to land upstream: open a PR. The acceptance criteria for the PR are roughly:

- The contract methods are implemented and the adapter passes the shipped contract-conformance tests in `tests/unit/test_adapter_protocol.py`.
- `close()` drives outputs to safe values (covered above).
- `resource_id` is stable and computed without I/O.
- At least one capability flag is declared accurately.
- The adapter is paired with a sim variant under `capa.devices.sim` so integration tests can run it without hardware.

*See also:* [Plugin system](plugin-system.md), [Threading model](../architecture/threading-model.md), [Shutdown sequence](../safety/shutdown-sequence.md), [Saturation and deadlines](../safety/saturation-and-deadlines.md).
