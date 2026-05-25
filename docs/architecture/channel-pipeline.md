# Channel pipeline

**Audience:** contributors writing or maintaining device adapters; config authors who need to understand why a channel value is what it is.
**Scope:** how a *declared channel* in YAML becomes a calibrated, decimated, sample-emitting object at run-time. The layer above adapters and below storage.

This is the page `runtime-architecture.md` deliberately does not cover — channels are a higher-level abstraction on top of emissions, owned by `capa.channels`.

---

## Channel vs emission

The runtime moves [**emissions**](../glossary.md#scalarsparquet) — `SourceRecord`, `ChannelSample`, `DeviceEvent`, `DeviceSnapshot`, `FrameReceipt`. These are the atomic units of acquisition output.

A **channel** is something different: a stable, named, calibrated, decimated *view* declared by config. It is not a record on a wire. The pipeline's job is to turn each raw adapter record into the channels the operator asked for, with calibrations applied and unit-stamped values attached.

The two things relate like this:

```
adapter record (SourceRecord)         channel sample (ChannelSample)
─────────────────────────────         ───────────────────────────────
WatlowSample(parameter=PV,           ChannelSample(
              instance=1,                  channel="heater_pv",
              raw=25.1, unit="C")    ──►   value=25.1, unit="C",
                                           source_record_id="watlow:heater:42",
                                           source_field="process_value:1")
```

One incoming record can produce *N* channel samples (e.g. one polled NI-DAQ tick yields one `SourceRecord` and one `ChannelSample` per declared analog channel). The original `SourceRecord` is also preserved in `device_records/*.parquet` so diagnostic fields the channel binding didn't promote are not lost.

---

## Lifecycle

A channel's lifecycle has four phases, in order:

1. **Declare** — the operator writes a `ChannelSpec` into the experiment YAML.
2. **Bind** — a `SourceBinding` variant inside the spec points at the exact field of the exact adapter record.
3. **Materialize** — at run-arm, the `ChannelRegistry` snapshots each spec into a `ResolvedChannel` and freezes.
4. **Emit** — at sample time, adapters call `build_channel_sample(...)` per matching binding, producing one `ChannelSample` per channel per tick.

### 1. Declare

[`ChannelSpec`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/channels/spec.py) is a frozen Pydantic model with the full per-channel config. The interesting fields:

- `name` — the stable run-local identifier. UI, sinks, and plots key off this.
- `kind` — `ChannelKind` (`tc`, `analog_in`, `process_var`, `setpoint`, `mass`, `mfc_flow`, `video_visible`, `video_ir`, `derived`, ...). Used by the UI for widget/axis choice.
- `source` — the binding variant (see below).
- `unit` / `derived_unit` — pre- and post-calibration units. Dimensional consistency is checked at config-load via `pint`.
- `calibration` — a `Calibration` variant (`Identity`, `Polynomial`, `Piecewise`, `CustomCallable`, ...). See [`calibration.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/channels/calibration.py).
- `keep_raw` — when `True`, the pre-calibration value is also written to `scalars.parquet` alongside the calibrated value.
- `decimate_to_hz` — plot-only decimation cap (default 60 Hz). **Disk capture stays at the native producer rate**; only the UI ring buffer thins.
- `sinks` — which named sinks receive this channel.
- `alarms` — declarative `AlarmBand` rules; the evaluator lands with the planned `SafetyMonitor`.

The dimensional check at construction time is the load-bearing guard against the classic "kPa-to-psi" class of errors:

```python
# Will raise ConfigError at YAML load time
ChannelSpec(name="purge_pressure",
            unit="kPa",
            derived_unit="psi",
            calibration=Identity(input_unit="kPa", output_unit="kPa"))  # ← mismatch
```

### 2. Bind — `SourceBinding` variants

[`SourceBinding`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/channels/spec.py) is a tagged union; the `source` discriminator picks the variant on deserialization. There are six variants, one per library row shape:

| Variant                      | Reads from                          | Selector fields                |
|------------------------------|-------------------------------------|--------------------------------|
| `alicat_frame_field`         | `alicatlib.streaming.Sample` (wide) | `device`, `field`              |
| `watlow_parameter`           | `watlowlib.streaming.Sample` (long) | `device`, `parameter`, `instance` |
| `sartorius_reading`          | `sartoriuslib.streaming.Sample`     | `device`, `field` (defaults `"value"`) |
| `nidaq_reading_field`        | polled `nidaqlib.DaqReading` (wide) | `device`, `task`, `field`      |
| `nidaq_block_channel`        | hardware-clocked `nidaqlib.DaqBlock`| `device`, `task`, `channel`    |
| `derived`                    | other channels                      | `expression`, `inputs`         |

The binding shape mirrors the library's native row shape. `watlowlib` emits one row per parameter per instance, so `WatlowParameter` carries `parameter` + `instance`. `alicatlib` emits a single wide row per tick, so `AlicatFrameField` only needs `field`. NI-DAQ has two shapes (polled and block) and binds to each accordingly.

This is the "tagged-union mirrors the library" decision behind [`runtime-architecture.md`](runtime-architecture.md)'s observation that capa preserves native records rather than flattening them.

### 3. Materialize — `ChannelRegistry`

At run-arm, the [`ChannelRegistry`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/channels/registry.py) snapshots every spec into a `ResolvedChannel`:

```python
@dataclass(frozen=True, slots=True)
class ResolvedChannel:
    name: str
    spec: ChannelSpec
    binding: SourceBinding
    calibration: Calibration
    sinks: tuple[str, ...]
```

The registry then calls `freeze()`. After freeze:

- `register(...)` raises `ConfigError`.
- `resolve(name)` returns the snapshot.
- The set of channels is the contract for the rest of the run.

**Why freeze:** changing channel meaning (binding, calibration, unit) mid-run silently corrupts historical interpretation. A `heater_pv` that referred to instance 1 for the first 30 minutes and instance 2 for the next 30 minutes would land in `scalars.parquet` as a single channel name — readers five years later have no way to tell. The freeze is the "channel set is frozen between Arm and Stop" invariant from the runtime plan, given teeth at this layer.

What *is* frozen at arm: every spec field — name, binding, calibration, unit, decimation, sinks, alarms.

What is *not* frozen: live samples (still flowing), derivation results (computed from live samples), ring buffer contents.

---

## Calibration — where the transform is applied

The single answer is: **at emission**, inside the adapter, via the shared helper `build_channel_sample(...)` ([`_helpers.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/_helpers.py)). The relevant fragment:

```python
def build_channel_sample(*, spec, raw_value, t_mono_ns,
                        source_record_id, source_field, status="ok"):
    cal = spec.calibration
    value, uncertainty = cal.evaluate_with_uncertainty(raw_value)
    return ChannelSample(
        channel=spec.name,
        t_mono_ns=t_mono_ns,
        value=value,
        raw=raw_value if spec.keep_raw else None,
        unit=spec.output_unit(),
        uncertainty=uncertainty,
        ...)
```

Two consequences worth knowing:

1. **`scalars.parquet`'s `value` column is calibrated.** The `raw_value` column is populated only when `keep_raw=True`. Analysts asking "are these counts or engineering values?" should default to "engineering" for the `value` column.
2. **Uncertainty is propagated per-sample.** The `UncertaintySpec` carried on the calibration variant produces a per-sample absolute uncertainty in output units, written into `scalars.parquet`'s `uncertainty` column. The `coverage_factor` (k=1 vs k=2) is recorded in the calibration's metadata, separately, so an analyzer five years later quotes the right interval without guessing.

`CustomCallable` calibrations are not evaluated inside `build_channel_sample` — they require the plugin runtime and are rejected with an `AdapterError`. The supported flow for plugin-driven calibrations runs through the calibration-fitting procedure, not the live sample path.

---

## Adapter responsibilities

Every adapter that produces channels calls into `channels_for_device(...)` + `build_channel_sample(...)`. The pattern is uniform:

```python
# Inside e.g. WatlowAdapter.stream()
async for sample in watlowlib.record(...):
    # Emit the native record
    yield SourceRecord(adapter="watlow", device=self.name, ...)
    # Per channel that binds to this device, emit one ChannelSample
    for spec in channels_for_device(self._specs,
                                    device=self.name,
                                    binding_source="watlow_parameter"):
        if not _binding_matches(spec, sample):
            continue
        yield build_channel_sample(
            spec=spec,
            raw_value=sample.value,
            t_mono_ns=clock.t_mono_ns(),
            source_record_id=make_record_id("watlow", self.name, seq),
            source_field=f"{spec.source.parameter}:{spec.source.instance}",
        )
```

The 11 adapter files in [`src/capa/devices/`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/) all follow the same shape — sim and real. Lifting the calibration application into one helper means every adapter calibrates identically and no adapter forgets to set `unit`, `source_record_id`, or `t_mono_ns`.

A `SourceRecord` is always emitted regardless of channel bindings — the record is the safety net for fields the channel binding didn't promote. The manifest declares `native_device_records="all"` to make this explicit ([`conductor.py:_dispatch_emission`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/conductor.py)).

---

## Decimation and ring buffers

`decimate_to_hz` does not affect what reaches `scalars.parquet`. Disk capture is at the adapter's native rate. The decimation applies only to the [`RingBufferRegistry`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/core/ringbuffer.py) that backs live plots.

The policy is *time-window decimation*: a sample is dropped if its `t_mono_ns` is within `1 / decimate_to_hz` of the last *kept* sample. This produces a stable plot line shape (vs. random skip-N decimation) and matches the operator's intuition that "`decimate_to_hz=10`" means "no more than ten points per second visible".

Sizing rules:

- Default `decimate_to_hz` is 60 Hz, intentionally above the fastest configured producer (a 50 Hz Sartorius balance), so the buffer keeps every sample. The plot pane's peak-mode downsampler then preserves transients regardless of the actual repaint cadence (~10 Hz).
- Per-buffer capacity is sized for "many seconds of samples at `decimate_to_hz`" — see [`ringbuffer.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/core/ringbuffer.py). Set lower in config only if a channel produces faster than the buffer can usefully store.
- Drop counters are exposed for the status bar's "Dropped UI samples (rolling 10 s)" pill.

Concurrency: single-producer (the UI-side drain) / single-consumer (the plot timer). Operations are not atomic across producer and consumer — a concurrent `snapshot()` during a `push()` may see one element fewer than the producer has just written. That's harmless: the missed sample appears on the next repaint tick. There is no lock on the hot path.

---

## What's *not* on this page

Two related concerns deliberately live elsewhere:

- **Derived channels.** The `DerivedBinding` variant + topological-sort derivation registry lands in a future PR; the binding schema is stable, the evaluator is not yet wired. The `derived` discriminant is reserved so YAML files written today don't have to migrate later.
- **Alarms.** `AlarmBand` rules are declared on `ChannelSpec` today but the evaluator ships with the planned `SafetyMonitor` (see [`capa-plan.md`](capa-plan.md) §9.2). For now, alarms are preserved in the snapshotted config but do not gate execution.

Both are flagged here so a reader who saw the YAML field doesn't wonder where the runtime code is.

---

## Mid-run mutability rules

Once the registry is frozen at arm, **nothing in the channel pipeline mutates**:

- `ChannelSpec` is `frozen=True` Pydantic.
- `ResolvedChannel` is a `frozen=True` dataclass.
- `Calibration` variants are `frozen=True`.
- `SourceBinding` variants are `frozen=True`.
- `ChannelRegistry._channels` is a regular dict, but `register` raises after `freeze`.

The only fields that change between Arm and Stop are the *content* of channel samples (values flow) and the ring-buffer contents. The shape of the pipeline is fixed.

This is enforced at the *registry* layer, not at the storage layer — a sink doesn't validate that its column set is stable; it trusts the registry. If a future change loosens this rule, the storage path's column-set assumption breaks too.

---

## Where to read more

- The raw record shapes (`SourceRecord`, `ChannelSample`, ...) and where they originate: [`devices/records.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/records.py).
- The channel-binding reference (every field of every variant): [Channel bindings](../configuration/channel-bindings.md).
- The disk schema the samples land in: [Channel samples parquet](../bundles/parquet-channel-samples.md).
- The downstream of derivation (future): [`capa-plan.md`](capa-plan.md).
- How channel samples reach the UI plot and the disk in parallel: [`data-flow.md`](data-flow.md).
