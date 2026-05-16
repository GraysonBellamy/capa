# Device-Library Unification — capa-side Cleanup

**Status:** waiting on `alicatlib`, `watlowlib`, `sartoriuslib`, `nidaqlib` to land their `UNIFIED_API_HANDOFF.md` work.

**Trigger to start:** all four libraries published a version with the unified API. Pin them in `pyproject.toml` (likely `>=1.0.0` or whatever the post-unification minor is) before doing any of the work below.

This document is the capa-side counterpart to the four `UNIFIED_API_HANDOFF.md` files in the sibling repos. It enumerates every shim, workaround, and manual conversion in capa's device layer that goes away once the libraries adopt the unified API.

---

## 1. Per-adapter cleanup

### 1.1 `capa/devices/alicat.py`

**Delete:**
- `_SingleDevicePollSource` class at lines ~141-162 — replace with `alicatlib.streaming.SingleDevicePollSource(name, device)`
- Any local `_ALICAT_UNIT_TO_PINT` table (if added during prior work) — replace with `alicatlib.units.to_pint(unit)`

**Rename throughout:**
- `DataFrame` → `Reading` everywhere the type appears (imports, type hints, isinstance checks)

**Update timestamp accesses in `_record_for()` (~line 945):**
- `sample.monotonic_ns` → `sample.t_mono_ns`
- `sample.received_at` → `sample.t_utc`
- `sample.midpoint_at` → derive from `sample.t_midpoint_mono_ns`

**Replace identity probing:**
- Wherever the adapter builds a DeviceSnapshot from cached `DeviceInfo` (`snapshot()` method), call `await device.snapshot()` and project the lib's `AlicatDeviceSnapshot` into capa's `DeviceSnapshot` emission

**Wire library counters:**
- `state.recoverable_error_count` → drive from `device.session.recoverable_error_count` (or whichever attribute the lib exposes) instead of adapter-incremented counter

**`expected_emission_rate_hz`:**
- Read from `device.expected_rate_hz` once stream is configured, fall back to params-declared rate

### 1.2 `capa/devices/watlow.py`

**Delete:**
- `_WATLOW_UNIT_TO_PINT` mapping at ~line 101-105 — replace with `watlowlib.units.to_pint(unit)`
- `_SingleDevicePollSource` shim — replace with `watlowlib.streaming.SingleDevicePollSource`

**Simplify `open_device` callers:**
- Today the adapter has different code paths for opened-vs-unopened controllers depending on protocol. With the symmetry fix, `controller = await watlowlib.open_device(...)` always returns an opened controller. Drop any `async with controller:` enter dance.
- If anything used `open_controller` (lower-level), migrate to `open_device` or to the manager directly.

**Update timestamp accesses** in the `_record_for()` path and any `sample_to_row()` consumer.

**Replace identity probing:**
- `snapshot()` → call `await controller.snapshot()` and project `WatlowDeviceSnapshot` into capa's `DeviceSnapshot`. The lib's snapshot already carries `family`, `capabilities`, `availability_summary` — currently the adapter assembles those manually.

**Wire library counters:**
- Replace adapter-tracked `recoverable_error_count` with `controller.session.recoverable_error_count`.

**Use top-level `sample_to_row`:**
- `from watlowlib.sinks import sample_to_row` → `from watlowlib import sample_to_row`

**Unit-drift quarantine logic (lines ~961-1014):**
- Stays in the adapter — this is capa-side calibration policy, not a library concern. Just make sure `Sample.unit` continues to be readable.

### 1.3 `capa/devices/sartorius.py`

**Highest-priority deletion:**

- **Cold-open retry logic at lines ~740-778** — replace with a clean `except SartoriusTransientTransportError` block:

  ```python
  for attempt in range(MAX_COLD_OPEN_ATTEMPTS):
      try:
          balance = await sartoriuslib.open_device(...)
          break
      except SartoriusTransientTransportError as exc:
          if attempt == MAX_COLD_OPEN_ATTEMPTS - 1:
              raise
          await anyio.sleep(COLD_OPEN_BACKOFF_S * (2 ** attempt))
  ```

  (Or simpler — if the lib's `open_device()` internally swallows up to N transients, the adapter just calls it and never sees them.)

- **Delete** the string-matching helpers at lines ~92-109 (`_is_frame_too_short`, `_is_zero_bytes`, etc.). These were hacks around the lib not exposing typed transients.

**Delete:**
- `_SingleDevicePollSource` shim at lines ~186-209 — replace with `sartoriuslib.streaming.SingleDevicePollSource`

**Rename:**
- `open_balance` → `open_device` if any call site still uses the alias
- `BalanceManager` → `SartoriusManager` if any call site uses the alias

**Update timestamp accesses** in `_record_for()` and channel sample extraction (~lines 788-843).

**Replace identity probing:**
- `snapshot()` → `await balance.snapshot()` for `SartoriusDeviceSnapshot` (carries `family`, `capabilities`, `protocol`, `mode`)

**Wire library counters:**
- Drop adapter-side `recoverable_error_count`; read from `session.recoverable_error_count`.

**Use top-level `sample_to_row`:**
- `from sartoriuslib.sinks import sample_to_row` → `from sartoriuslib import sample_to_row`

**Use `to_pint`:**
- For Sartorius `Quantity` values, use `sartoriuslib.units.to_pint(quantity.unit)` (or the new `Quantity.to_pint()` method if it was added) instead of any local mapping.

### 1.4 `capa/devices/nidaq.py` and `capa/devices/nidaq_channels.py`

**Critical deletion:**

- **Private-path import at line ~724**: `from nidaqlib.sinks.base import reading_to_row` → `from nidaqlib import reading_to_row`

**Delete manual block unrolling:**
- Lines ~807-856 (`_channel_samples_for_block`) — the rectangular-to-rows unroll currently hand-written. Replace with `nidaqlib.block_to_rows(block)` and project each row into a `ChannelSample`:

  ```python
  for row in nidaqlib.block_to_rows(block):
      # row already carries t_mono_ns, t_utc, channel, value
      yield build_channel_sample_from_row(row, run_clock, configured_bindings)
  ```

  The reconstruction math (`task_started_at + k / sample_rate`) moves into the library; capa just consumes per-sample rows.

**Delete identity probing:**
- `_probe_device_info` at lines ~666-715 — replace with `await session.snapshot()` returning `NIDaqSnapshot`. Project the snapshot's `product_type`, `serial`, `chassis`, `physical_module` into capa's `NIDAQDeviceInfo`.

**Update discovery:**
- `from nidaqlib.system.discovery import list_devices` → `from nidaqlib import find_devices`
- Iterate `DiscoveryResult` objects instead of bare `DeviceInfo`

**Update timestamp accesses:**
- `DaqReading` and `DaqBlock` now expose `t_mono_ns` / `t_utc` / `t_midpoint_mono_ns`. Update `_record_for_reading` (~line 717) and the block path.
- For per-sample timestamp reconstruction in block mode, use `block.block_period_ns` (or `block.sample_rate_hz`) instead of computing from `task_started_at + k / rate_hz`.

**Replace `PollSource` shim:**
- Anywhere the adapter wraps a polled `DaqSession` as a `PollSource`, use `nidaqlib.streaming.PolledSessionAdapter(session)`.

**Use `to_pint`:**
- Channel unit emission should use `nidaqlib.units.to_pint(unit)` for `TemperatureUnits.DEG_C` → `"degC"`, etc.

**Wire library counters:**
- `error_policy=NidaqErrorPolicy.RETURN` paths increment `session.recoverable_error_count` automatically; drop adapter-side counter.

**Keep capa-side:**
- `NIDAQThermocoupleConfig`, `NIDAQVoltageConfig` Pydantic validators in `nidaq_channels.py` stay — they're capa-config validation, not a library gap. The library accepts `ChannelSpec.from_dict()`; capa adds the Pydantic schema layer on top.
- Chunk-size guardrail at lines ~378-386 stays — capa's Parquet unroll budget, not a library concern.

---

## 2. Cross-cutting capa changes

### 2.1 `capa/devices/_helpers.py` — generic library-error wrap

Today each adapter has its own `AdapterError` wrapping (`watlow.py:403-407`, `sartorius.py:524-528`, `alicat.py`, `nidaq.py:524-568`). With every lib's `ErrorContext` exposing the same base fields (`port`, `address`, `command_name`, `protocol`, `elapsed_s`, `extra`), consolidate into one helper:

```python
def wrap_lib_error(exc: BaseDeviceLibError, device_name: str) -> AdapterError:
    """Wrap any unified-API device-library error into a capa AdapterError."""
    return AdapterError(
        device_name=device_name,
        message=str(exc),
        port=exc.context.port,
        command=exc.context.command_name,
        elapsed_s=exc.context.elapsed_s,
        underlying=exc,
    )
```

All four adapter `command()` and stream error paths can call this. The `BaseDeviceLibError` type can be a `typing.Protocol` capa defines (just requires `.context` with the six base fields) — capa doesn't need to import a shared base class from elsewhere.

### 2.2 `capa/devices/runtime_state.py` — health from session counters

`AdapterRuntimeState.compute_health()` (lines ~129-159) today reads adapter-tracked `recoverable_error_count`. After unification, source that from `device.session.recoverable_error_count` (or `session.recoverable_error_count` for nidaqlib). The adapter's job becomes "expose the session's counter on the runtime state", not "count errors itself".

### 2.3 Snapshot consolidation

Today every adapter assembles a `DeviceSnapshot` from cached `DeviceInfo` + adapter-side metrics. After unification:

1. Adapter calls `await device.snapshot()` → gets lib-native snapshot (`AlicatDeviceSnapshot`, etc.)
2. Adapter projects into capa's `DeviceSnapshot` emission (adds capa-only fields: bundle root, run-id, etc.)
3. Health pill computation stays in `AdapterRuntimeState`, but its inputs (`connected`, `last_error`, `recoverable_error_count`) come from the lib snapshot

Consider adding a `capa.devices._helpers.project_lib_snapshot(lib_snap, runtime_state) -> DeviceSnapshot` helper so each adapter's `snapshot()` method shrinks to ~3 lines.

### 2.4 Channel-sample unit handling

In `_helpers.py:build_channel_sample()` (~lines 60-98), the channel's declared unit is compared/converted against the library's emitted unit. Today each adapter pre-maps via its own table. After unification:

- `lib.units.to_pint(reading.unit)` produces a pint-compatible string for every library
- Capa's calibration layer (`evaluate_with_uncertainty()`) can compare strings consistently
- Watlow's existing unit-drift quarantine logic at `watlow.py:961-1014` keeps living in the adapter (it's a policy, not a translation), but the string-vs-Unit-enum step disappears

### 2.5 Discovery flow

`capa/devices/discovery.py` currently normalizes per-library discovery results. With every lib returning `DiscoveryResult` of the same shape, the normalization layer shrinks:

- Drop per-library result-shape adapters
- Each adapter's discovery hook just returns `list[DiscoveryResult]` directly
- `discover_descriptor()` at `discovery.py:53-88` can be simpler

### 2.6 Top-level `sample_to_row` imports

Every adapter currently imports `sample_to_row` from `<lib>.sinks`. After unification, switch to `<lib>.sample_to_row`. Tiny diff per file, but worth doing in the same pass for consistency.

### 2.7 PollSource imports

Three adapters (alicat, watlow, sartorius) currently use a custom `_SingleDevicePollSource`. After unification:

```python
from <lib>.streaming import SingleDevicePollSource
```

nidaq uses `PolledSessionAdapter` instead. All four adapters end up with one import line and zero shim code.

---

## 3. Tests

### 3.1 Update existing adapter tests

- Field renames: `sample.monotonic_ns` → `sample.t_mono_ns` in every test that constructs or inspects a Sample
- `DataFrame` → `Reading` in alicat tests
- `list_devices` → `find_devices` in nidaq tests
- Drop string-match assertions on cold-open behavior (sartorius); assert on `SartoriusTransientTransportError` instead

### 3.2 New parity tests

- `test_<adapter>_snapshot_matches_lib`: verify each adapter's `DeviceSnapshot` emission carries the projected fields from the lib's snapshot
- `test_<adapter>_recoverable_count_from_session`: verify adapter's runtime state reflects the lib's session counter, not an adapter-side count
- `test_sartorius_cold_open_retry_typed`: simulate `SartoriusTransientTransportError` and verify the adapter retries cleanly
- `test_nidaq_block_unroll_matches_lib`: verify `nidaqlib.block_to_rows()` output projects to the same ChannelSamples capa used to produce by hand

### 3.3 Smoke tests under `tests/integration/`

The `test_watlow_engine.py`, `test_camera_engine.py`, etc. baselines must keep passing after each phase of cleanup. Re-run the full smoke suite after each adapter is migrated, not just at the end.

### 3.4 Hardware smoke tests

`tests/hardware/test_<adapter>_smoke.py` files run against real hardware (gated on `CAPA_HARDWARE_TESTS=1`). After unification:
- They should still pass without modification, since the lib's public behavior is what they exercise
- If any test imports a private path (`<lib>.sinks.base.something`) — fix those imports too

---

## 4. `pyproject.toml` updates

Bump the four library pins to the post-unification minor:

```toml
"alicatlib>=X.Y.0,<X.(Y+1)",
"watlowlib>=X.Y.0,<X.(Y+1)",
"sartoriuslib>=X.Y.0,<X.(Y+1)",
"nidaqlib>=X.Y.0,<X.(Y+1)",
```

Replace `X.Y` with whatever version the libs land. The minor pin is intentional — these are coordinated breaking changes.

---

## 5. Suggested phasing

Don't do all four adapters at once. The unification is broken into four library efforts; do the capa-side in the same order so each phase is small and reviewable.

### Phase 0: prep
- Confirm all four libs published the unified API
- Bump pins in `pyproject.toml`, run `uv sync`, observe what breaks at import time
- Triage the breakage into the per-adapter sections of this doc

### Phase 1: nidaqlib (smallest changes, biggest cleanups)
- Drop private-path import → top-level `reading_to_row`
- Replace manual block unroll → `block_to_rows`
- Replace `list_devices` → `find_devices`
- Wire `session.snapshot()`, `expected_rate_hz`, `recoverable_error_count`
- Update tests
- Run `uv run pytest tests/unit/devices/test_nidaq*.py` and integration smoke

### Phase 2: sartoriuslib (kills the worst hack)
- Replace cold-open string-match → `SartoriusTransientTransportError` retry
- Drop alias usage (`open_balance` → `open_device`)
- Drop `_SingleDevicePollSource` shim
- Wire snapshot, counters
- Update tests
- Run sartorius unit + integration tests

### Phase 3: watlowlib (largest API surface change in capa)
- Drop `_WATLOW_UNIT_TO_PINT` → `watlowlib.units.to_pint`
- Drop `_SingleDevicePollSource`
- Simplify `open_device` callers (no asymmetry)
- Wire snapshot (capabilities, availability_summary travel via the lib snapshot now)
- Update tests
- Run watlow tests + the `test_watlow_engine` integration

### Phase 4: alicatlib (large rename churn but mechanical)
- `DataFrame` → `Reading` rename
- Drop `_SingleDevicePollSource`
- Wire snapshot, expected_rate_hz, counters
- Update tests
- Run alicat unit + smoke

### Phase 5: cross-cutting consolidation
- `_helpers.wrap_lib_error()` helper, replace four wrap sites
- `_helpers.project_lib_snapshot()` helper, replace four snapshot projections
- `discovery.py` normalization shrink
- Full integration suite + hardware-smoke pass
- Update `docs/runtime-architecture.md` if any of the above changed observable behavior

### Phase 6: cleanup verification
- `grep -r "DataFrame" src/capa/devices/` — should hit zero alicat-flavored matches
- `grep -r "from .*\.sinks\.base import" src/capa/` — should hit zero
- `grep -r "_SingleDevicePollSource\|_WATLOW_UNIT_TO_PINT" src/capa/` — should hit zero
- `grep -r "frame too short\|got 0 bytes" src/capa/` — should hit zero
- Mypy + ruff clean
- Final smoke test of every adapter against fake hardware
- CHANGELOG entry under `## [Unreleased]`

---

## 6. Out of scope (do not change in this pass)

- Calibration logic — stays in capa
- Authorization gate (`_helpers.reject_unless_authorized`) — capa-specific
- `SourceRecord` / `ChannelSample` envelope — capa schema
- `resource_id` contention domain — capa engine concern
- NI-DAQ chunk-size guardrail — capa Parquet budget
- Pydantic channel-config validators in `nidaq_channels.py` — capa config validation
- The `RunClock` arithmetic that converts library monotonic_ns to run-relative offsets — capa run-relative model
- Cone-calorimeter / capa-pyrolysis profile logic — domain orchestration

---

## 7. Verification checklist (end-state)

- [ ] All four library pins bumped in `pyproject.toml`
- [ ] Per-adapter cleanup items in §1 complete; phase-by-phase tests passing
- [ ] Cross-cutting helpers in §2 in place
- [ ] No private-path imports from any library (`<lib>._foo`, `<lib>.sinks.base`, etc.)
- [ ] No adapter-side unit mapping tables
- [ ] No adapter-side `_SingleDevicePollSource` shims
- [ ] No string-matching exception detection
- [ ] No adapter-side recoverable-error counters (all from session)
- [ ] No adapter-side snapshot assembly (all from lib snapshot + projection)
- [ ] `grep` checks in §5 Phase 6 all hit zero
- [ ] Full unit + integration + hardware-smoke suites pass
- [ ] `docs/runtime-architecture.md` updated if observable behavior moved
- [ ] CHANGELOG entry under `## [Unreleased]`
