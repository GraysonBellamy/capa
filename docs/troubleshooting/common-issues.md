# Common issues

**Audience:** all users.
**Scope:** the issues that hit most often during day-to-day operation, with the symptom, the cause, and the fix.

This page is a triage starting point, not a complete catalogue. For deeper investigation see [Status-bar symptoms](status-bar-symptoms.md) (for live-run distress) and [Reading event logs](reading-event-logs.md) (for post-mortem). For a corrupted bundle see [Crash recovery](crash-recovery.md).

Each section follows the same shape: *Symptom* → *Cause* → *Fix*.

---

## Sartorius balance doesn't open on the first try

**Symptom.** Capa starts up. The Sartorius card shows "opening…" and stays there for a few seconds. Eventually it succeeds, or it gives up with `SartoriusTransportError: cold-open frame underrun`.

**Cause.** The Sartorius YDP/SBI protocol has a well-known cold-open race: the first read from the serial port after the device powers on can return a zero-byte frame before the balance's controller has finished its own boot sequence. The library handles this with a bounded retry; the visible "opening…" period is that retry loop. See the `_build_balance` notes in [`devices/sartorius.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/sartorius.py).

**Fix.** Wait. The retry is bounded by the configured `timeout_s` (default ~3 s) and almost always succeeds within that window. If it doesn't:

- Confirm the balance is actually powered on and the cable is seated.
- Confirm no other process holds the COM port (Sartorius display software, a previous capa instance that wasn't fully closed).
- Confirm the serial settings in [`hardware.toml`](../configuration/hardware-toml.md) match the balance's menu (baud, parity, stop bits, protocol kind).

If the open succeeds on the second capa launch but not the first, the cold-open race is the cause and the existing retry should have caught it — file a bug with the bundle's `run.log` so the retry schedule can be tuned. See [Reporting bugs](reporting-bugs.md).

---

## NI-DAQ chassis not found

**Symptom.** Pool open fails with `NIDAQError: device "cDAQ1" not found` or similar, or the NI-DAQ worker stays IDLE with no channels discovered.

**Cause.** One of three things, in rough order of frequency:

1. **NI-DAQmx runtime not installed.** Capa imports `nidaqmx`; the underlying driver is a separate NI install (NI-DAQmx Runtime ≥ 2024 Q3).
2. **Chassis powered off, USB cable unseated, or chassis claimed by another process.** Confirm with NI MAX (NI Measurement & Automation Explorer) — if the chassis doesn't appear there, capa can't see it either.
3. **Chassis name mismatch.** The `channels` entries in [`hardware.toml`](../configuration/hardware-toml.md) encode the chassis name (e.g. `cDAQ1Mod1/ai0`). If NI MAX renamed the chassis (e.g. `cDAQ2` after a USB replug), update the config.

**Fix.**

```powershell
# Verify the driver is loaded
python -c "import nidaqmx.system; print(nidaqmx.system.System.local().devices.device_names)"
```

If that prints `()`, the driver isn't seeing any hardware — start at NI MAX. If it prints chassis names that don't match your `hardware.toml`, update the config.

---

## FLIR Atlas DLL not on PATH

**Symptom.** Capa fails to import the FLIR IR adapter at startup: `ImportError: DLL load failed while importing _flir_atlas` or `OSError: cannot load library 'AtlasIPxxx.dll'`. The IR worker never comes up.

**Cause.** The FLIR Atlas SDK installs its DLLs into a directory that isn't on the Python process's `PATH` by default. On Windows this typically means `C:\Program Files\FLIR Systems\sdks\file\Atlas\bin` (or similar) is missing from the user environment.

**Fix.**

1. Confirm the SDK is actually installed: `Get-ChildItem "C:\Program Files\FLIR Systems"` (or your install path).
2. Add the Atlas `bin` directory to the user `PATH`. Re-open the terminal / re-launch capa so the change takes effect.
3. Verify the import:
   ```powershell
   python -c "import flir_atlas; print(flir_atlas.__version__)"
   ```

This is a per-machine setup issue, not a runtime issue. Once `PATH` is right it stays right. See [cameras-flir.md](../devices/cameras-flir.md) for the broader FLIR install story.

---

## Camera encoder choice causing saturation

**Symptom.** A run starts fine, then within 30–60 s the `sat` pill turns yellow then red on the [status bar](../user-guide/status-bar-guide.md). Loop lag (`loop`) stays low. The [Acquisition Diagnostics dock](../user-guide/diagnostics-dock.md) shows every worker's Age climbing in lockstep — not just the camera. `events.sqlite` shows a `saturation_deadline` event whose metadata's `resource_id` is `webcam:0` or `flir_ir:0`.

**Cause.** The configured video codec is `libx264` (the default), which encodes on a single CPU thread in the writer process. At high resolution × high fps × dual-camera, encode can't keep up with capture; the writer's inbox fills; the conductor's drain blocks on `await writer.record_frame(...)`; every bridge backs up. This is the canonical mechanism behind a saturation trip with `loop` low.

**Fix.** Swap the codec in [`hardware.toml`](../configuration/hardware-toml.md) for the offending camera:

```toml
[devices.visible_cam0.params]
codec = "h264_qsv"     # Intel iGPU (zero-CPU encode on supported chipsets)
# codec = "h264_nvenc" # NVIDIA GPU encode
# codec = "mjpeg"      # no inter-frame compression; much larger files; near-zero CPU
```

Defaults live in [`devices/camera/webcam/constants.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/camera/webcam/constants.py). The hardware-accelerated codecs require either an Intel CPU with Quick Sync or an NVIDIA GPU; `mjpeg` works everywhere but produces bundles 5–10× larger.

Reload the config and try a short test run. The `sat` pill should stay green; the diagnostics dock should show every camera's Age stable below 1 s. See also [cameras-webcam.md](../devices/cameras-webcam.md).

---

## Disk filling up mid-run

**Symptom.** The `disk` pill turns yellow (< 15% free) then red (< 5% free). Shortly after, `sat` follows.

**Cause.** Camera bitrate dominates bundle size. A 30 fps dual-camera run at `libx264` is typically 50–100 MB/min; at `mjpeg` it can be 500 MB/min or more. NI-DAQ at high sample rates also contributes (5 Hz × 64 channels × 8 bytes × 60 s ≈ 150 KB/min — negligible by comparison).

**Fix, by urgency.**

- **`disk` yellow, run still going:** finish the run if you can. Do not start a new run on this volume.
- **`disk` red, run still going:** stop the run cleanly if the rig allows it. The writer will eventually stall on `ENOSPC`, which trips saturation and seals as `crashed_but_sealed`.
- **After:** free space, or launch with `--runs-root` / `CAPA_RUNS_ROOT` pointing at a larger volume. If you also use profile disk-space preflight, keep `storage.bundle_root` aligned with the actual runs root.

**Pre-flight estimate.** Roughly `bundle_size ≈ Σ(producer_rate × bytes_per_sample × duration_s) + video_bitrate × duration_s`. For a one-hour capa_real_full run, plan on 6–10 GB at default settings, or 30–60 GB with `mjpeg`.

---

## Procedure plugin entry-point not discovered

**Symptom.** A procedure plugin you installed via `uv pip install capa-myplugin` doesn't show up in `capa plugins list` or in the GUI's procedure picker.

**Cause.** Three possibilities, ranked:

1. **Lockfile is stale.** In production mode, capa enforces a plugin lockfile to make runs reproducible — see [Plugin lockfile](../extending/plugin-lockfile.md). A procedure plugin installed but not present in the lockfile is rejected.
2. **Entry-point typo.** The plugin's `pyproject.toml` must declare the entry-point under the `capa.procedures` group. Check with:
   ```powershell
   python -c "from importlib.metadata import entry_points; print(list(entry_points(group='capa.procedures')))"
   ```
3. **Wrong Python environment.** Capa is running under a different `uv`-managed venv than the one where you installed the plugin.

**Fix.**

```powershell
# Confirm capa sees the plugin's entry-point
capa plugins list

# If listed but not loaded → lockfile gate
capa plugins trust <plugin-id> --reason "approved for this rig"

# If not listed at all → entry-point or env problem
uv pip show capa-myplugin    # confirm it's in the active environment
```

See [Plugin system](../extending/plugin-system.md) for the full discovery and gating mechanism.

Device adapter descriptors are a separate surface: they use `capa.adapters` / `capa.cameras`, show up through `capa hardware discover` and the Setup registry, and are not listed by `capa plugins list`.

---

## Not finding what you need?

This page covers the most common issues, not all of them. For deeper triage:

- **Live run looks wrong** → [Status-bar symptoms](status-bar-symptoms.md).
- **Past run looks wrong** → [Reading event logs](reading-event-logs.md).
- **Bundle won't open / appears partial** → [Crash recovery](crash-recovery.md).
- **Reporting it as a bug** → [Reporting bugs](reporting-bugs.md).
