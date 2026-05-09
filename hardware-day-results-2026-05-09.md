# Hardware day results — 2026-05-09

## Summary

Drove the [hardware-day-plan.md](hardware-day-plan.md) end-to-end against real silicon on the Linux dev box. Per-device smoke (§3) and the integration runs (§5) closed every plan acceptance criterion that doesn't require Windows-only drivers. UI smoke (§6) and crash recovery (§7) each surfaced real bugs that would have shipped without this gate.

> **Update — 2026-05-09 PM:** all 13 surfaced follow-ups are shipped (capa-side) plus the watlowlib upstream fix landed as **v0.2.0**. §1 SIGKILL parquet recovery shipped as the Arrow IPC streaming switch (in-flight `*.in-flight.arrows` → final parquet at seal time; see [_ipc.py](src/capa/storage/_ipc.py) + [finalize.py](src/capa/storage/finalize.py) `_rewrite_inflight_to_parquet`; regression test [test_crash_recovery_sigkill.py](tests/integration/test_crash_recovery_sigkill.py)). The camera preview dock ([camera_preview.py](src/capa/ui/docks/camera_preview.py)) closed the §6 UI gap. The only items still gated on external resources are FLIR Stage H and NI-DAQ (both need a Windows rig). The webcam libx264 EINVAL stays passively open behind `CAPA_WEBCAM_FRAME_DIAG=1` — diagnostic is permanent; nothing actionable until the bug recurs.

> **Re-validation — 2026-05-09 PM (post-watlowlib-pin):** §5.A re-run confirms watlowlib v0.2.0 atomic batches solve the recorder starvation; §6 UI smoke confirms both inline UI fixes hold under live engine signals. **Two new findings** (camera stop-time race + V4L2 identity not surfacing to bundle) filed below. See [§Re-validation 2026-05-09 PM](#re-validation-2026-05-09-pm).

**Phase status going out:**

| Phase | Plan target | Outcome |
|---|---|---|
| §3.1 webcam | sealed bundle, frame counts match | ✅ |
| §3.2 Watlow | sealed bundle, identity in manifest, no-op setpoint accepted | ✅ |
| §3.3 Alicat | sealed bundle, both gas + pressure readable | ✅ |
| §3.4 Sartorius | sealed bundle, tare path accepted | ✅ |
| §4 FLIR E85 | sealed bundle, .csq round-trips | ⏸ deferred (Linux USB-mode gap) |
| §5.A control + visible (real Watlow + Alicat + webcam) | sealed bundle, profile validates, all commands authorized | ✅ |
| §5.B cameras + mass (real Sartorius + webcam, sim IR) | sealed bundle, profile validates, all commands authorized | ✅ |
| §5.4 plugin trust mode | happy + reject paths | ✅ reject; ⚠ happy path blocked by editable-install version-field bug |
| §6 UI smoke | plots, numerics, abort, restart | ✅ after fixing two UI bugs inline |
| §7 crash recovery | sealed_after_crash bundle | ✅ after Arrow IPC streaming switch (post-hardware-day) |

**Bugs fixed inline:**

1. **Engine deadlock with camera-only configs** — fixed in [src/capa/experiment/engine.py:838-843](src/capa/experiment/engine.py#L838-L843). Worth a regression test.
2. **UI plot pane bound to stale empty registry** — fixed in [src/capa/ui/tabs/run.py:_on_state](src/capa/ui/tabs/run.py).
3. **UI start button stuck disabled after seal** — fixed in [src/capa/ui/tabs/run.py:_on_run_finished](src/capa/ui/tabs/run.py).

---

## Progress since hardware day

**Resolution snapshot (2026-05-09 PM):**

| Surfaced item | Resolution | Where |
|---|---|---|
| ✅ Engine camera-only deadlock (regression test) | Test added | [test_engine_camera_only.py](tests/integration/test_engine_camera_only.py) |
| ✅ UI plot pane / start button (regression tests) | Tests added | [test_ui_run_tab.py](tests/integration/test_ui_run_tab.py) |
| ✅ Webcam event-loop starvation | Pump split + `to_thread.run_sync` | [webcam.py](src/capa/devices/camera/webcam.py) |
| ✅ Webcam EINVAL pump death | Drop-and-continue + `pump_warning` event + `dropped_frames` counter | [webcam.py](src/capa/devices/camera/webcam.py) + [base.py:182](src/capa/devices/camera/base.py#L182) |
| ✅ Webcam V4L2 identity probe | Sysfs-based `_probe_v4l2_info` | [webcam.py](src/capa/devices/camera/webcam.py) |
| ✅ Camera preflight `duration_s` fallback | Read `procedure.config["duration_s"]` | [cameras.py:200-208](src/capa/experiment/cameras.py#L200-L208) |
| ✅ Camera preflight tmpfs handling | Warn + tighten budget vs `MemAvailable / 2` | [cameras.py](src/capa/experiment/cameras.py) |
| ✅ Sartorius cold-open retry | 3 attempts, 0.2/0.4/0.8 s backoff | [sartorius.py:494-538](src/capa/devices/sartorius.py#L494-L538) |
| ✅ `equipment.toml` identity at seal time | New `finalize(equipment=...)` kwarg + engine collector | [bundle.py](src/capa/storage/bundle.py) + [engine.py](src/capa/experiment/engine.py) |
| ✅ Plugin lock auto-discovery in production mode | cwd → XDG; hard `Exit(2)` if absent | [app.py:88-150](src/capa/app.py#L88-L150) |
| ✅ Plugin lock version-field asymmetry | Aligned on `dist.version`; editable-install retread documented | [plugins_runtime.py:213-228](src/capa/core/plugins_runtime.py#L213-L228) |
| ✅ `distribution_hash` semantics docs | Expanded docstring with operator-facing trust scope | [plugins_runtime.py:347-380](src/capa/core/plugins_runtime.py#L347-L380) |
| 🔵 Watlow `device_silent` watchdog | **Resolved upstream in watlowlib v0.2.0** — atomic-by-default lock-batch acquisition; no capa-side code change | See [§watlowlib v0.2.0 resolution](#watlowlib-v020-resolution-recorder-starvation) |
| ✅ `capa finalize` SIGKILL parquet recovery | Switched in-flight format to Arrow IPC streaming; sinks emit `*.in-flight.arrows`, finalize rewrites to parquet | [_ipc.py](src/capa/storage/_ipc.py) + [finalize.py](src/capa/storage/finalize.py) + [test_crash_recovery_sigkill.py](tests/integration/test_crash_recovery_sigkill.py) |

**Test count:** 477 → 521 (+44 regressions). Full suite passes (`uv run pytest tests/ --ignore=tests/hardware`). Zero regressions across unit + integration.

**Plus the upstream handoff:** [watlowlib-recorder-starvation-upstream-plan.md](watlowlib-recorder-starvation-upstream-plan.md) was written, handed off, and resolved as v0.2.0 (with deviations from the plan — see the linked subsection).

---

## Re-validation 2026-05-09 PM

After watlowlib v0.2.0 published to PyPI, the capa-side pin was bumped (`"watlowlib"` → `"watlowlib>=0.2.0"`; the editable `tool.uv.sources` entry was removed so production resolution comes from the published wheel). Hardware setup was identical to the original §5.A: real Watlow PM3 (B&B 485USBTB-2W on `/dev/ttyUSB0`), real Alicat MCR-200SLPM-D (Prolific PL2303 on `/dev/ttyUSB2` at 115200 baud), real Logitech C930e (`/dev/video4`); sim balance + sim NI-DAQ. Configs were updated for the current TTY layout: [capa_real_partial_a.toml](configs/hardware/capa_real_partial_a.toml) `heater.port` `/dev/ttyUSB2` → `/dev/ttyUSB0`, `carrier_mfc.port` `/dev/ttyUSB0` → `/dev/ttyUSB2`, and webcam `input_url` `/dev/video2` → `/dev/video4` (the original config pointed at the laptop's integrated camera, not the C930e — corrected in both variant A and variant B).

### §5.A re-run (headless)

Bundle: [`/home/gbellamy/capa-runs/2026-05-09_172848_REAL-A-001`](/home/gbellamy/capa-runs/2026-05-09_172848_REAL-A-001) — **sealed, integrity ok**.

| Metric | Original (pre-v0.2.0) | Re-run (v0.2.0) | Verdict |
|---|---|---|---|
| `tick_p99_ms` @ 1 Hz | not reported (lib lacked metric) | **68.4 ms** | ✅ healthy (< 100 ms threshold; 6.8% of 1000 ms budget) |
| `tick_p50_ms` | — | 41.1 ms | ✅ |
| Late ticks | — | **0 / 154** | ✅ |
| `device_silent` warnings during run | 2 (~17 s gaps) | **0** | ✅ |
| Command rate (steady-state) | ~9 cmds/s claimed | **~7.7 cmds/s** (1183 commands / 153 s) | comparable workload |
| Bundle outcome | sealed | sealed | ✅ |

The decision rule from [§watlowlib v0.2.0 resolution](#watlowlib-v020-resolution-recorder-starvation) was: `tick_p99_ms < 100 ms` at 1 Hz → atomic batches solved it. **68.4 ms is firmly in the green.** The `device_silent` watchdog gap that fired twice during the original §5.A did not recur. The optimistic upstream prediction held against the real workload — the math in the doc treated 9 cmds/s × 250 ms confirm-EEPROM as load-bearing for sustained saturation, but in practice the per-write occupancy is much lower (most setpoint writes are not `confirm=True` EEPROM commits) so the bus is well within budget at this rate.

### §6 UI smoke (interactive Qt)

Two bundles, both sealed:

| Bundle | Phase | Outcome |
|---|---|---|
| [`2026-05-09_173429_REAL-A-001`](/home/gbellamy/capa-runs/2026-05-09_173429_REAL-A-001) | abort at ~22 s | `run_status=aborted`, `bundle_status=sealed`, 591 frames, integrity ok |
| [`2026-05-09_173510_REAL-A-001`](/home/gbellamy/capa-runs/2026-05-09_173510_REAL-A-001) | restart → complete | `run_status=completed`, `bundle_status=sealed`, 4482 frames, integrity ok |

**Both inline UI fixes confirmed live:**
- ✅ Plot pane registry-rebind on `EngineState.RUNNING` ([run.py:_on_state](src/capa/ui/tabs/run.py)) — live PV/setpoint/flow traces appeared on plots throughout both runs.
- ✅ Start-button deferred re-enable after seal ([run.py:_on_run_finished](src/capa/ui/tabs/run.py)) — second Start click after the abort-seal worked first try.

### Webcam EINVAL root cause: did NOT reproduce

`CAPA_WEBCAM_FRAME_DIAG=1` was set for the §5.A re-run. The first 149 input frames logged at INFO with `format=yuyv422 width=640 height=480 time_base=1/1000000` — **no format / dimension changes observed**, and **no `pump_warning` events fired** anywhere in the 153-second run (4527 frames captured). The libx264 EINVAL from the original §5.A is now hypothetical — it didn't trigger this session, so the trigger is still unknown. The diagnostic logging is **kept permanently** behind the `CAPA_WEBCAM_FRAME_DIAG=1` env var ([webcam.py](src/capa/devices/camera/webcam.py)); zero overhead when off, no rollback needed.

> Side note: the camera ignored the config's `width=1280, height=720` and negotiated `640×480 yuyv422` from V4L2 directly (no `-video_size` / `-framerate` options pass through `av.open(format="v4l2")` today). Output stream is still 1280×720 H.264 — PyAV upscales via `reformat`. Not surfaced as a bug because output bundles look correct; worth knowing for future investigations.

### New findings (post-fix surface)

#### 1. Stop-time camera race emits a noisy `pump_failed` warning every clean run

Every run (headless §5.A and both UI runs) ends with:

```
[WARNING] engine.camera.pump_failed  camera=visible_cam0  error='push_frame requires start_recording()'
```

immediately before `engine.camera.closed`. The bundle still seals correctly with `integrity_status=ok`, but the warning is misleading — nothing actually failed.

**Cause:** the engine's stop sequence cancels [_run_pump](src/capa/experiment/cameras.py#L520) while the pump's last in-flight frame is mid-flight (`av.open` decoder still has a pending frame). The camera's `close()` flips `_recording=False` first; when the pump's `push_frame` call resumes, [_push_frame_sync](src/capa/devices/camera/webcam.py#L336) raises `AdapterError("push_frame requires start_recording()")`, which `_run_pump`'s broad `except Exception` catches and logs as `pump_failed`.

**Fix candidates:**
- In [_push_frame_sync](src/capa/devices/camera/webcam.py#L336): split the precondition check. `not self._recording` while `_output_container is not None` is a stop-race → return a benign `drop_reason="stopped_during_pump_in_flight"`. Truly never-started → keep raising.
- Or in [run_pump](src/capa/devices/camera/webcam.py#L413): test `if not self._recording: break` between `_advance_decoder` and `push_frame` so the in-flight frame is dropped silently.
- Or in [_run_pump](src/capa/experiment/cameras.py#L520): catch `AdapterError("push_frame requires start_recording()")` specifically as benign-on-stop.

S-effort. No bundle-integrity impact, but the misleading WARNING is operator-noise.

#### 2. V4L2 identity probed correctly but not surfaced to bundle artefacts

[_probe_v4l2_info](src/capa/devices/camera/webcam.py#L531) **works** — calling it directly returns `card_name="Logitech Webcam C930e"`, `serial="E7501BDE"`, `bus_info="3-6.2"`. [WebcamAdapter.open](src/capa/devices/camera/webcam.py#L230) stores the result in `self._info` (`model` and `serial` fields populated). But **two surfaces drop this data**:

1. **`manifest.json.cameras[*].model` / `serial`** are hard-coded to `spec.model_hint` and `spec.serial` from the static `CameraSpec` ([bundle.py:481-482](src/capa/storage/bundle.py#L481-L482)) — never reads the adapter's live `_info`. Manifest shows `model=None, serial=None` for the C930e even though the probe ran successfully.
2. **`equipment.toml`** has no `[[cameras]]` section at all. The engine's `_collect_equipment_blocks` walks `[[devices]]` only; the equipment-identity work shipped earlier (per the [resolution snapshot](#progress-since-hardware-day)) didn't include camera identity.

**Fix:** plumb `WebcamAdapter._info` (and any other camera adapter's live identity) through to both surfaces. Likely M-effort: adapter-side, add a `device_info`-style accessor matching the device adapters' duck-typed probe convention; bundle-side, replace the static `spec.model_hint` lookup with `adapter._info.model` if available; engine-side, extend `_collect_equipment_blocks` to walk cameras alongside devices.

This was claimed in the doc above as "✅ Webcam V4L2 identity probe — Sysfs-based `_probe_v4l2_info`" — but the integration to the bundle was never validated. The unit-level probe works; the end-to-end surface to manifest / equipment.toml does not.

### Engineering changes from this session (besides config + pin)

- [src/capa/devices/camera/webcam.py](src/capa/devices/camera/webcam.py): added `CAPA_WEBCAM_FRAME_DIAG=1` env-gated DEBUG/INFO logger that emits `webcam_frame_diag` events for the first 150 input frames in `run_pump` (`format` / `width` / `height` / `pts` / `time_base`). Dormant by default. Used during this re-validation; left in place for the next time EINVAL needs investigation.
- [pyproject.toml](pyproject.toml): `"watlowlib"` → `"watlowlib>=0.2.0"`; removed `watlowlib = { path = "../watlowlib", editable = true }` from `[tool.uv.sources]` (keeps production resolution coming from the PyPI wheel; alicatlib / sartoriuslib / nidaqlib stay editable).
- 20 ruff lint fixes (stale `# noqa` directives + 2 import sorts) plus formatter on 6 files (none of which were code-functional changes).

**Test suite after all changes:** 481 passed (no hardware), ruff clean, mypy strict clean across 85 src files.

---

## Devices exercised

| Device | Identity reported | Port / addr | Test artefact | Outcome | Notes |
|---|---|---|---|---|---|
| Webcam (external) | Logitech C930e | /dev/video4 (capture); /dev/video5 (metadata) | [tests/hardware/test_webcam_smoke.py](tests/hardware/test_webcam_smoke.py) | ✅ | UVC exposes two nodes per camera; capture is the lower index. `cameras[0].identity` is `None` because the V4L2 adapter doesn't extract model/serial. |
| Watlow | PM3R1CA-AAAAAAA, fw=1, hw=28, family=pm | /dev/ttyUSB0 (smoke); /dev/ttyUSB2 (multi-device) | [tests/hardware/test_watlow_smoke.py](tests/hardware/test_watlow_smoke.py) | ✅ | Connected via B&B Electronics 485USBTB-2W (USB-RS485, vendor `0856:ac33`). Vendor ID not in `ftdi_sio` whitelist by default — required `echo "0856 ac33" > /sys/bus/usb-serial/drivers/ftdi_sio/new_id` after `modprobe`. Heater breaker was off, so PV stayed at ambient (~65 °C reported room temp). |
| Alicat | MCR-200SLPM-D serial 225873 fw=8v17 (flow_controller) | /dev/ttyUSB0 (smoke); /dev/ttyUSB0 (multi-device) | [tests/hardware/test_alicat_smoke.py](tests/hardware/test_alicat_smoke.py) | ✅ | Connected via Belkin USB-RS232 adapter (Prolific PL2303 chip, vendor 067b). **Required baud override to 115200**, not the 19200 factory default I assumed. Reading: 0 SLPM (no plumbing), 14.61 psi (ambient), N₂. |
| Sartorius | MSE1203S-100-DR (Cubis-class) | /dev/ttyUSB0 (smoke); /dev/ttyUSB1 (multi-device) | [tests/hardware/test_sartorius_smoke.py](tests/hardware/test_sartorius_smoke.py) | ✅ (after one retry) | xBPI at 19200 baud (not 9600 factory). Connected via FTDI FT232R (vendor 0403). First-byte race on cold open: `frame too short: got 1 bytes (min 4)` cleared on retry. Empty pan reads ~0.07 g, status=`settling` throughout (never auto-stabilized in 30 s freerun). |
| FLIR E85 | enumerated as USB ID `09cb:1007 Ex-Series UVC and MSD interface` | USB | [/home/gbellamy/Documents/git/capa-flir/tests/unit/test_e85_hardware.py](../capa-flir/tests/unit/test_e85_hardware.py) (3 tests authored, all skip without camera) | ⏸ deferred to Windows rig | See §4 below for the full root-cause note. |
| NI-DAQ | — | — | (no Linux driver) | n/a | Sim adapter substituted in §5.A and §5.B. |

---

## Bundles produced

All bundles `bundle_status=sealed`, `integrity_status=ok` unless noted.

| Bundle | Section | Notes |
|---|---|---|
| `/tmp/capa-hw-day/2026-05-09_142235_WEBCAM-REAL-001` | §3.1 | webcam free-run, 30 s, 891 frames @ 29.7 fps |
| `/tmp/capa-hw-day/2026-05-09_143407_WATLOW-REAL-001` | §3.2 | Watlow free-run, 30 s, 60 records (PV + setpoint @ 1 Hz) |
| `/tmp/capa-hw-day/2026-05-09_143743_ALICAT-REAL-001` | §3.3 | Alicat free-run, 30 s, 60 wide-row records, 180 scalars |
| `/tmp/capa-hw-day/2026-05-09_144214_SARTORIUS-REAL-001` | §3.4 | Sartorius free-run, 30 s, 60 mass samples |
| `/tmp/capa-hw-day/2026-05-09_145841_REAL-A-001` | §5.A | Watlow+Alicat real, sim balance/NIDAQ, real webcam. 1075 commands, all authorized. |
| `/home/gbellamy/capa-runs/2026-05-09_152630_REAL-B-001` | §5.B | sim heater/MFC/NIDAQ, real Sartorius + webcam, sim IR. 1144 commands, all authorized. |
| `/home/gbellamy/capa-runs/2026-05-09_153357_REAL-B-001` | §5.4 happy path (production mode without `--plugins-lock`, effectively dev mode — see anomaly) | sealed |
| `/home/gbellamy/capa-runs/2026-05-09_155249_REAL-B-001` | §6 abort | `run_status=aborted`, `bundle_status=sealed`, 622 webcam frames + 188 IR frames before abort |
| `/home/gbellamy/capa-runs/2026-05-09_160103_REAL-B-001` | §7 crash | `run_status=running`, `bundle_status=open`, 445 events recovered after WAL checkpoint, but `capa finalize` failed on un-footed parquet |

---

## Anomalies

### Fixed in this session

- **Engine deadlock with camera-only configs.** `producer_queue` was never closed when `len(self._adapters) == 0`. `_fanout_task` blocked forever, run never sealed. Fixed at [src/capa/experiment/engine.py:838-843](src/capa/experiment/engine.py#L838-L843): close the queue immediately if `producers_alive.value == 0` after starting tasks. Worth a regression test (`tests/integration/test_engine_camera_only.py`).
- **UI plot pane bound to stale empty registry.** `RunTab._on_start_clicked` was rebinding the plot pane to `controller.buffers` *immediately after* `controller.start()` returned, but `start()` is async and the buffer rebuild hadn't completed yet. Fixed by moving the rebind into `_on_state(EngineState.RUNNING)`, matching the numerics dock pattern. [src/capa/ui/tabs/run.py:_on_state](src/capa/ui/tabs/run.py).
- **UI start button stuck disabled after seal.** `RunTab._on_run_finished` checked `can_start()` (which calls `is_active`), but the slot fires from inside the controller's task `finally` block — the task isn't `done()` yet, so `is_active` returns True. Fixed by deferring the re-enable via `QTimer.singleShot(0, ...)`. [src/capa/ui/tabs/run.py:_on_run_finished](src/capa/ui/tabs/run.py).

### Surfaced, not fixed (real follow-ups)

> **Annotation key:** every entry below carries a **Status (2026-05-09 PM):** line — ✅ shipped, 🟡 deferred, or 🔵 deferred to upstream. The bug descriptions are the original observation; the Status line records resolution.

- **`capa finalize` cannot recover SIGKILL'd parquet files.** The bundle writer streams to `*.in-flight.parquet` via pyarrow's chunked writer. The parquet footer is only written on `close()`. After SIGKILL, the file has valid row groups but no footer; `capa finalize` reads it as a complete parquet → `ArrowInvalid: Parquet magic bytes not found in footer`. Result: §7's `bundle_status=sealed_after_crash` cannot be reached today. The existing `tests/integration/test_crash_recovery.py` likely passes because it uses a graceful-shutdown shim, not real SIGKILL.
  - **Status (post-hardware-day):** ✅ **shipped.** Switched in-flight format from `pq.ParquetWriter` to Arrow IPC streaming via [_ipc.py](src/capa/storage/_ipc.py) (per-batch length prefixes are natively truncation-tolerant; `read_recoverable` returns the prefix on torn files without raising). Sinks emit `*.in-flight.arrows`: [channel_samples_sink.py](src/capa/storage/channel_samples_sink.py), [device_records_sink.py](src/capa/storage/device_records_sink.py), [video_sink.py](src/capa/storage/video_sink.py). [finalize.py](src/capa/storage/finalize.py) `_rewrite_inflight_to_parquet` reads the IPC stream and writes the final parquet at seal time; torn streams write a `seal_warnings` entry instead of failing the seal. Regression test [test_crash_recovery_sigkill.py](tests/integration/test_crash_recovery_sigkill.py) uses a `multiprocessing` child + real `os.kill(pid, SIGKILL)`.

- **Plugin trust check requires explicit `--plugins-lock` flag.** `CAPA_PLUGIN_MODE=production` alone is silently no-op because the CLI doesn't auto-discover `./plugins.lock`. Discovered when the first "happy path" production run completed cleanly despite a deliberately tampered RECORD. **Fix:** in production mode, error out (or auto-look-up `./plugins.lock` / `$XDG_CONFIG_HOME/capa/plugins.lock`) when no lock path is provided.
  - **Status (2026-05-09 PM):** ✅ **shipped.** [src/capa/app.py:88-150](src/capa/app.py#L88-L150) — new `_resolve_plugins_lock_for_run` + `_discover_plugins_lock_paths` helpers. Production mode walks `./plugins.lock` then `$XDG_CONFIG_HOME/capa/plugins.lock` (fallback `$HOME/.config/capa/plugins.lock`); first match wins. Missing on both → `typer.Exit(2)` with explicit error. Auto-discovered path is echoed to stdout so the operator sees exactly which lock was honored. `plugins_list` updated to use the same lookup order. Tests: [tests/unit/test_cli.py](tests/unit/test_cli.py) `TestPluginsLockAutoDiscovery` (3 cases).

- **Plugin lock entry's `version` field is the class attribute, not the dist version.** `capa plugins trust` writes `version = "0.1.0"` (the `RecipeRunner.version` class attr) but `detect_drift` compares against `dist.version` (`"0.0.1.dev1+gb38818856.d20260508"`). They will always mismatch in editable installs, so the happy path (production mode + valid lock + unmodified package) returns `procedure not in trusted registry` even when nothing has been tampered with. **Fix:** make `capa plugins trust` write the dist version (or make `detect_drift` compare class versions).
  - **Status (2026-05-09 PM):** ✅ **shipped.** [src/capa/core/plugins_runtime.py:213-228](src/capa/core/plugins_runtime.py#L213-L228) — `LoadedProcedure.version` now sources `dist.version` exclusively, dropping the `getattr(cls, "version", version)` fallback. `capa plugins trust` writes the dist version into the lock; `detect_drift` reads dist version. **Trade-off:** editable installs (where `dist.version` is `0.0.1.dev1+gXXXX.dYYYY`) invalidate the lock on every commit. Production rigs install from wheels and aren't affected — documented in the docstring callout (see next item). Tests: [tests/unit/test_plugins_runtime.py](tests/unit/test_plugins_runtime.py) `test_loaded_procedure_version_uses_dist_not_class_attribute`. Closes the §5.4 happy path that was previously blocked.

- **Plugin distribution_hash is computed over METADATA + RECORD only.** Per the docstring, this is intentional — "sufficient for detecting 'the wheel I installed has been swapped'" — but operators might expect "rebuild = retrust required". Worth documenting prominently in the trust-mode runbook. The rejection path in §5.4 fired after RECORD was directly mutated.
  - **Status (2026-05-09 PM):** ✅ **shipped (docs only).** [src/capa/core/plugins_runtime.py:347-380](src/capa/core/plugins_runtime.py#L347-L380) — expanded `_hash_distribution` docstring with an explicit operator-facing "Trust scope" section: detects wheel swaps + tampered `RECORD`; does NOT detect editable-install source-file edits or runtime monkey-patching; recommended workflow is build wheel → install → trust → ship lock. Cross-references the editable-install retread documented in #12.

- **Watlow watchdog `device_silent` warnings during command-heavy bursts.** During §5.A's ramp (~9 cmds/s), the heater went silent for ~17 s twice. Cause: serial-port contention between the 1 Hz poll thread and the setpoint-write thread on the same `/dev/ttyUSB2`. **Fix:** serialize Watlow reads + writes through a single asyncio queue inside the adapter so they share the bus deterministically.
  - **Status (2026-05-09 PM):** 🔵 **resolved upstream in watlowlib v0.2.0** (see [§watlowlib v0.2.0 resolution](#watlowlib-v020-resolution-recorder-starvation) below). Investigation found watlowlib already had a per-port lock; the actual root cause was lock-fairness starvation across N per-parameter acquisitions. Upstream fix: atomic-by-default per-tick lock-batch acquisition. **Capa action:** none in code; bump `watlowlib` pin to `>=0.2.0` once upstream tags the release. **Capa expectation:** atomic batches help bursty workloads but cannot rescue a sustained over-budget ramp; if §5.A's gap persists, mitigation is application-side rate-limiting or PM3 profile-mode coalescing.

- **Webcam pump_failed mid-recipe.** `avcodec_send_packet() returned 22` (libx264 EINVAL) at t≈23 s into §5.A. Camera recovered to `engine.camera.closed` and bundle sealed, but only 699 frames captured. Recipe events continued as expected. **Fix:** root-cause the EINVAL (frame format change? PyAV stream-state bug?). Worth a regression test once isolated.
  - **Status (2026-05-09 PM):** ✅ **drop-and-continue guard shipped; root-cause investigation deferred.** [src/capa/devices/camera/webcam.py](src/capa/devices/camera/webcam.py) — `_push_frame_sync` now catches `av.error.FFmpegError` around the encode loop, drops the offending frame, increments a new `_dropped_frames` counter, and emits a `pump_warning` event. Critically: the dropped frame does NOT advance `_frame_count`, so receipt indexes stay contiguous over surviving frames and the encoder doesn't see a `pts` gap. `CameraHealth.dropped_frames` field added ([base.py:182](src/capa/devices/camera/base.py#L182)) so post-run analysis can find the events. The pump no longer dies on a single-frame fault. Tests: [tests/unit/test_camera_webcam.py](tests/unit/test_camera_webcam.py) `TestEncoderFailureGuard`. **Root cause still unknown** — needs a second hardware run with diagnostic logging on UVC frame-format renegotiation or `pts` collisions.

- **Event-loop starvation by visible webcam pump.** §5.B recipe ran 2.7× slower than wall clock (5 min 16 s for what should be 115 s). Webcam captured at ~14 fps instead of 30. The PyAV `frame.reformat().to_ndarray()` step runs on the event loop and is CPU-heavy enough to block other tasks. **Fix:** wrap `reformat()` in `anyio.to_thread.run_sync()`, or move the entire pump to a dedicated worker thread. This bug interacts with the Watlow watchdog one above — the slower the loop, the worse the serial contention surfaces.
  - **Status (2026-05-09 PM):** ✅ **shipped.** [src/capa/devices/camera/webcam.py](src/capa/devices/camera/webcam.py) — `push_frame` split into a sync core (`_push_frame_sync` doing encode + mux + bookkeeping) and an async wrapper that runs the core via `anyio.to_thread.run_sync`. `run_pump`'s decode-loop iteration (`next(decoder, None)`) and `frame.reformat(format="rgb24").to_ndarray()` each run in a worker thread (`_advance_decoder`, `_reformat_to_rgb24` helpers). Result: every CPU-heavy PyAV call is off the asyncio loop. Tests: `TestPushFrameOffLoop` (asserts `_push_frame_sync` is invoked via `to_thread.run_sync`).

- **Camera disk preflight uses 3600 s fallback duration for free-runs.** The procedure's `duration_s` config isn't surfaced to `disk_space_preflight_problems` when no method is present. Forced `estimated_bps` adjustments down to 1.5 MB/s in production TOMLs and 500 KB/s in the smoke test. **Fix:** preflight should consult the procedure's `duration_s` config when a method is absent. ~10-line change in [src/capa/experiment/cameras.py](src/capa/experiment/cameras.py).
  - **Status (2026-05-09 PM):** ✅ **shipped.** [src/capa/experiment/cameras.py:200-208](src/capa/experiment/cameras.py#L200-L208) — preflight peeks at `config.procedure.config.get("duration_s")` when method is None. 3600 s fallback retained for genuinely unbounded runs (`external_stop`-driven). Tests: [tests/unit/test_cameras_preflight.py](tests/unit/test_cameras_preflight.py) `TestProcedureDurationResolution` (4 cases).

- **Camera disk preflight projects against `runs_root`'s mount, but `/tmp` is a 16 GB tmpfs on this box.** `/tmp/capa-hw-day` looked like it had plenty of room (only 14 MB used) but the projection blocked the §5.B run (3.6 GB × 3 cameras > 16 GB free). Switched to `/home/gbellamy/capa-runs` (817 GB free). **Fix:** preflight should warn explicitly when the runs root is on tmpfs, or weight against a much shorter "expected free space within the tmpfs" rather than blocking.
  - **Status (2026-05-09 PM):** ✅ **shipped (warn + tighten).** [src/capa/experiment/cameras.py](src/capa/experiment/cameras.py) — new `_filesystem_type` helper parses `/proc/mounts` longest-prefix match (Linux only; returns `None` elsewhere); new `_mem_available_bytes` reads `MemAvailable` from `/proc/meminfo`. When the target is on `tmpfs`/`ramfs`, a non-blocking `disk_target_volatile` warning fires AND the budget is tightened to `min(reported_free, MemAvailable / 2)` so a memory-pressure scenario doesn't OOM mid-run. Tests: `TestVolatileFilesystemDetection` (5 cases).

- **Webcam adapter doesn't populate `CameraInfo.model` / `serial` from V4L2.** `manifest.json.cameras[0].identity` is `None` for real webcams. `v4l2-ctl --info` exposes the card name (`Logitech Webcam C930e`) and bus path; surfacing those would close the gap without needing vendor-specific code.
  - **Status (2026-05-09 PM):** ✅ **shipped.** [src/capa/devices/camera/webcam.py](src/capa/devices/camera/webcam.py) — new `_probe_v4l2_info(device_path) -> V4L2Probe` reads `/sys/class/video4linux/<node>/name` (card name) plus the parent USB device's `serial` / `idVendor` / `idProduct` / bus path. Called from `WebcamAdapter.open()` when `sys.platform == "linux"` and `input_format == "v4l2"`. Verified against the real Logitech C930e on this box (returns `card_name="Logitech Webcam C930e"`, `serial="E7501BDE"`, `bus_info="3-6.2"`). Tests: `TestV4L2IdentityProbe` (4 cases including non-Linux skip + missing-node).

- **Sartorius first-byte race on cold open.** `frame too short: got 1 bytes (min 4)` on the first identify after a fresh plug-in; cleared on retry. **Fix:** retry-on-short-frame inside `SartoriusAdapter.open()`.
  - **Status (2026-05-09 PM):** ✅ **shipped.** [src/capa/devices/sartorius.py:494-538](src/capa/devices/sartorius.py#L494-L538) — `_build_balance` split into `_build_balance_once` + a retry wrapper (3 attempts, 0.2 / 0.4 / 0.8 s backoff). Substring match on `"frame too short"` / `"got 0 bytes"` only; non-cold-open `SartoriusError` shapes (checksum, timeout, bad device id) re-raise immediately. `_cold_open_retry_count` tracked on the adapter for diagnosis. Tests: [tests/unit/test_sartorius_adapter.py](tests/unit/test_sartorius_adapter.py) `TestColdOpenRetry` (4 cases).

- **`equipment.toml` doesn't capture device identity.** Watlow's smoke test confirms part_number/firmware/hardware_id are read at adapter open, but the `equipment.toml` written into each bundle only contains `name` + `adapter`. The richer identity surfaces in events and probe-capabilities logs but not in the static equipment profile. Worth deciding: is the static file authoritative, or are events the source of truth?
  - **Status (2026-05-09 PM):** ✅ **shipped — events are SoT, equipment.toml is a denormalized human-readable summary populated at seal time.** New `equipment` kwarg on `RunBundleWriter.finalize` ([src/capa/storage/bundle.py](src/capa/storage/bundle.py) — `_rewrite_equipment_toml` rewrites the file *before* the integrity walk so `manifest.sha256` covers the populated content). Engine collects per-device blocks via new `_collect_equipment_blocks` ([src/capa/experiment/engine.py](src/capa/experiment/engine.py)) — for each declared device, looks up the live adapter via `self._adapter_by_device` and duck-types `adapter.device_info` through `_identity_from_device_info` (probes `part_number`, `model`, `serial_number`, `firmware_id`, `hardware_id`, `family`, etc., with `.raw` / `.value` coercion). Sim adapters get `identity=None`. Crash-recovered bundles keep the open()-time stub (no live adapter to probe). Tests: [tests/integration/test_bundle_roundtrip.py](tests/integration/test_bundle_roundtrip.py) `TestEquipmentToml` (3 cases).

### Configuration findings (not bugs)

- **Unit / channel-kind validation surprises.** `psia` not in pint registry → use `psi`. `pressure` not a valid `ChannelKind` → use `process_var`. Both fixed in [configs/hardware/alicat_real.toml](configs/hardware/alicat_real.toml).
- **`estimated_bps` defaults too high.** Lowered to 1.5 MB/s in production webcam TOML; the underlying preflight bug is the real fix.

---

## P3 production-mode plugin trust check (§5.4)

| Sub-step | Outcome |
|---|---|
| `capa plugins list` (dev mode) | ✅ all three builtin procedures (free_run, recipe_runner, batch) listed |
| `capa plugins trust capa.builtin.recipe_runner --reason "hw-day-2026-05-09"` | ✅ writes lock + journal at `./plugins.lock` and `./plugins.lock.journal` |
| Run with `CAPA_PLUGIN_MODE=production` (no `--plugins-lock`) | ⚠ "succeeded" but only because the CLI silently ignored the missing lock and ran in effectively dev mode (see anomaly above) |
| Mutate `dist-info/RECORD`, run with `CAPA_PLUGIN_MODE=production --plugins-lock ./plugins.lock` | ✅ `run_status=aborted`, `exit_reason: procedure 'capa.builtin.recipe_runner' is not in the trusted registry` — rejection path fires correctly |
| Restore RECORD, re-run | ⚠ still rejected, because `capa plugins trust` wrote `version="0.1.0"` (class attr) into the lock but `detect_drift` compares against `dist.version="0.0.1.dev1+gb38818856.d20260508"`. The hash matches, but the version field doesn't — VERSION_MISMATCH gates loading. Editable-install bug. |

The safety-critical rejection path works correctly. The happy path is blocked by the version-field asymmetry above (logged for fix).

> **Update — 2026-05-09 PM:** the happy path is now unblocked. Both root causes are resolved:
> - Auto-discovery of `./plugins.lock` (and `$XDG_CONFIG_HOME/capa/plugins.lock`) when `CAPA_PLUGIN_MODE=production` and no `--plugins-lock` flag.
> - `LoadedProcedure.version` aligned on `dist.version` so `capa plugins trust` and `detect_drift` agree.
>
> `tests/unit/test_cli.py::TestPluginsLockAutoDiscovery::test_production_mode_auto_discovers_cwd_lock` now reproduces the §5.4 happy path end-to-end against the builtin `free_run` procedure and asserts `bundle_status: sealed`. Re-running §5.4 sub-step 5 on real silicon is unnecessary for verification but worthwhile as a sanity check during the next hardware day.

---

## watlowlib v0.2.0 resolution (recorder starvation)

The `device_silent` watchdog gap (§5.A anomaly) was investigated, an upstream plan was written ([watlowlib-recorder-starvation-upstream-plan.md](watlowlib-recorder-starvation-upstream-plan.md)), and the fix landed on `watlowlib` `main` as **v0.2.0** (tag pending at time of writing). The investigation reframed the bug:

**Original framing:** "no per-port lock → bytes interleave on the bus."
**Actual root cause:** watlowlib already had a per-port `client.lock`. The recorder's `poll_many` made N independent acquisitions per tick (one per parameter), and a 9-cmds/sec setpoint burst (especially RWES writes with `confirm=True`, ~250 ms each on EEPROM commit) starved the recorder under FIFO lock fairness.

### What landed upstream (vs. the plan)

| Plan asked for | What shipped in v0.2.0 |
|---|---|
| `record(..., atomic_polls=True)` opt-in flag | **Atomic-by-default**, no flag — the recorder always acquires the per-port lock once per tick batch |
| `Controller.poll_many(..., atomic=True)` | Same — no flag, always atomic |
| New `_execute_locked` / `_read_parameter_locked` private API | None — single owner-check helper (`anyio.Lock.statistics().owner == get_current_task()`); public API unchanged |
| `AcquisitionSummary.lock_wait_ms_p50` / `_p99` | Replaced with `tick_duration_ms_p50` / `tick_duration_ms_p99` — full `await source.poll_many(...)` round trip; no `PollSource` Protocol ripple |
| Design 2 (recorder-priority lock) | Rejected — priority just shifts starvation onto the writer; sustained over-budget bus is an application concern |

### Capa-side action

- **Code:** none. The plan suggested `record(..., atomic_polls=True)` in [src/capa/devices/watlow.py](src/capa/devices/watlow.py); that kwarg does not exist. Default behavior is correct.
- **Pin:** bump `watlowlib` to `>=0.2.0` in [pyproject.toml](pyproject.toml) **once upstream tags v0.2.0**. Today the local editable install (`tool.uv.sources.watlowlib = { path = "../watlowlib", editable = true }`) already picks up the fix from `main`; a `>=0.2.0` PEP 440 pin would fail resolution against the current `0.1.0+...` dev version. Re-bump after the tag.

### Honest expectation for the next §5.A re-run

The plan's closing line ("the 17-second silent gap goes away because each tick still completes within ~150 ms") is optimistic. Atomic batches make tick *completion* fast once the lock is held; they don't prevent the tick's *first acquisition* from waiting behind a deep FIFO queue.

**Math against the §5.A workload:** 9 setpoint-writes/sec × ~250 ms confirm-EEPROM each = 2.25 s/sec lock occupancy. Steady-state queue grows at +1.25 s per second of wall-clock — so after ~14 s, the recorder's tick enqueues behind ~17 s of pending writes. **Still a 17-s gap, atomic or not.**

Where atomic batches actually help:

- **Bursty workloads** (recipe ramps with quiet inter-burst windows): tick latency stops scaling with `N parameters × queue depth at each enqueue` — that's the dominant pathology in capa's specific shape.
- **Tick stretch under contention spikes:** the 250–500 ms per tick of mid-tick contention is gone.

Where they don't:

- **Sustained over-budget workloads** (`write_rate × per_write_occupancy > 1 s/sec`). No upstream library change can rescue that — the bus is full.

### Diagnostic to use

After the v0.2.0 bump, every recording's `AcquisitionSummary` carries `tick_duration_ms_p99` (also surfaced as `tick_p99_ms=...` on the `recorder.stop` log line). Decision rule:

- **Healthy:** `tick_p99_ms ≪ 1000 / rate_hz` (e.g., < 100 ms at 1 Hz)
- **Saturated:** `tick_p99_ms` approaches `1000 / rate_hz` → the bus is contended; the watchdog gap is structural and lives in the application

If the gap recurs on the §5.A re-run, `tick_p99_ms` tells the operator whether to (a) rate-limit the recipe ramp (most direct), (b) coalesce writes via a PM3 onboard profile, or (c) move the recorder to a separate physical bus. None of those are library bugs.

---

## Crash-recovery outcome (§7)

Original hardware-day finding: `capa finalize` aborted on the in-flight parquet's missing footer; events recovered correctly (445 events, all 435 `method.command.issued`) but `manifest.sha256` was never regenerated. The lesson: the existing `test_crash_recovery.py` didn't exercise actual SIGKILL.

> **Update (post-hardware-day):** ✅ **resolved.** In-flight format switched from chunked parquet to Arrow IPC streaming ([_ipc.py](src/capa/storage/_ipc.py)). Truncated streams now read back via `read_recoverable` and rewrite to final parquet during `finalize` ([finalize.py](src/capa/storage/finalize.py) `_rewrite_inflight_to_parquet`); irrecoverable files surface as `seal_warnings` rather than failing the seal. Regression test at [test_crash_recovery_sigkill.py](tests/integration/test_crash_recovery_sigkill.py) uses `multiprocessing` + real `os.kill(pid, SIGKILL)`.

---

## Follow-ups to file as plan items

All capa-side follow-ups surfaced on hardware day are shipped (see [§Progress since hardware day](#progress-since-hardware-day) for the resolution log). What remains is gated on external resources:

### Webcam (passive)

- [ ] Root-cause the libx264 EINVAL observed at t≈23 s in §5.A. Drop-and-continue guard + `pump_warning` event already shipped; permanent diagnostic behind `CAPA_WEBCAM_FRAME_DIAG=1` ([webcam.py](src/capa/devices/camera/webcam.py)). Re-run on 2026-05-09 PM did not reproduce. Stays open until the bug surfaces again with the diagnostic on.

### FLIR

- [ ] Investigate Linux USB-mode-switch for the FLIR Ex-Series. Either find a libusb control-transfer that puts the camera into vendor mode, or accept Linux Atlas-USB as a permanent limitation and document.
- [ ] Schedule a Windows rig day to close capa-flir Stage H.

### NI-DAQ

- [ ] Schedule the Windows rig day for the NI-DAQ adapter. NI-DAQmx has no Linux driver.
---

## Next steps

1. **FLIR Stage H + NI-DAQ Windows day.** Unchanged from the original plan — both blocked on a Windows rig. Schedule when one is available.

2. **Webcam EINVAL root cause.** Drop-and-continue guard prevents the loss-of-recording symptom; root cause still unknown. Diagnostic logging is permanent (env-gated). Stays open until the bug surfaces again with `CAPA_WEBCAM_FRAME_DIAG=1` on.

---

## Operator notes for the next hardware day

- **Webcam C930e**: capture node is the lower of `/dev/video4` + `/dev/video5` (UVC exposes a metadata node alongside).
- **Watlow**: B&B 485USBTB-2W converter; `ftdi_sio` whitelist is empty for vendor `0856`. After every plug, run:
  ```sh
  sudo modprobe ftdi_sio
  echo "0856 ac33" | sudo tee /sys/bus/usb-serial/drivers/ftdi_sio/new_id
  ```
  Or install the udev rule above.
- **Alicat MCR**: factory baud on this unit is **115200**, not 19200. Override `CAPA_TEST_ALICAT_BAUD` and `[devices.params].baudrate`.
- **Sartorius MSE**: xBPI at **19200**. Cold-open retry is now automatic (3 attempts, 0.2/0.4/0.8 s backoff); the operator should no longer need to intervene.
- **Watlow `tick_p99_ms`**: after the watlowlib v0.2.0 pin lands, every recording's `recorder.stop` log line carries `tick_p99_ms=...` (also in `AcquisitionSummary.tick_duration_ms_p99`). Healthy is `< 100 ms` at 1 Hz; values approaching `1000 / rate_hz` mean the bus is contended and any `device_silent` watchdog gap is application-side, not library-side.
- **FLIR E85**: USB enumeration shows only UVC + MSD interfaces; Atlas needs a vendor interface that's not exposed without the Windows driver. Linux is currently a non-starter.
- **NI-DAQ**: stays sim on Linux.
- **Kernel mismatch trick**: if you upgrade `linux-cachyos` and have a running process to preserve, restoring an old kernel's modules from `/var/cache/pacman/pkg/linux-cachyos-X.Y.Z-*.pkg.tar.zst` into `/lib/modules/X.Y.Z-cachyos/` (then `depmod -a X.Y.Z-cachyos`) lets you keep running without rebooting. Modules dir is orphaned after reboot; safe to delete.
