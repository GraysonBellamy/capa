# Hardware day — capa P0–P4 silicon validation

**Goal:** prove every code path in capa core (P0–P3) and capa-flir (P4) against real silicon on the Linux dev box. Produce sealed bundles, hardware-marked tests, and a written outcome report.

**Out of scope:** NI-DAQ (no Linux driver — sim path covers it; defer to Windows rig). cone-calorimeter profile (§16.1 deprioritised).

**Working assumption (rig constraint):** the operator can connect **one device at a time easily, and 2–3 simultaneously** if needed. The plan is therefore sequenced so the assistant front-loads all author-once-no-hardware work in Phase 0; Phase 1 walks one device at a time; Phase 2 splits the integration run into two device sub-pairings instead of a single five-device blast.

---

## Live status (2026-05-09, in progress through §5.4)

**Completed:**

- ✅ §2 Phase 0 — baselines green; all artefacts authored.
- ✅ §3.1 Webcam — Logitech C930e on `/dev/video4`. 30 s freerun: 891 frames at 29.7 fps. Bundle sealed.
- ✅ §3.2 Watlow — PM3R1CA-AAAAAAA on `/dev/ttyUSB0` (later /dev/ttyUSB2 in multi-device runs). 4 smoke tests pass; PV reading 64.66 °C. Bundle sealed.
- ✅ §3.3 Alicat — MCR-200SLPM-D serial 225873 on `/dev/ttyUSB0`. Required baud override to 115200 (not the 19200 factory default I assumed). 3 smoke tests pass. Bundle sealed.
- ✅ §3.4 Sartorius — MSE1203S-100-DR (Cubis-class) at `xbpi/19200`. 4 smoke tests pass after one retry (first-byte race on initial connect). Empty pan reads 0.069 g. Bundle sealed.
- ⏸ §4 FLIR E85 — **deferred to Windows rig.** See §4.4 below for full root cause.
- ✅ §5.A — Watlow + Alicat (real) + sim balance/NIDAQ + webcam (real). Recipe ran end-to-end; 1075 method.command.issued events all carry authorization_id; bundle sealed; `capa catalog verify` ok.
- ✅ §5.B — sim Watlow/Alicat/NIDAQ + Sartorius (real) + webcam (real) + sim FLIR. 1144 commands all authorized; both real cameras' .mkv/.csq + frame parquets present; bundle sealed.
- ✅ §5.4 — Plugin trust mode happy + reject paths exercised. Required two unobvious flags (see anomalies).

**Anomalies discovered + resolved:**

- **Engine deadlock with camera-only configs.** `producer_queue` was never closed when `len(self._adapters) == 0`, so `_fanout_task` blocked forever and the run never sealed. Fixed in [src/capa/experiment/engine.py:838-843](src/capa/experiment/engine.py#L838-L843) — close the queue immediately if `producers_alive.value == 0` after starting tasks.
- **`estimated_bps` defaults too high vs. /tmp disk.** Initial 6 MB/s × free-run fallback duration 3600 s × 1.5 margin ≈ 32 GB > 16 GB free in `/tmp` (tmpfs). Lowered to 1.5 MB/s in production TOMLs and 500 KB/s in the smoke test spec. **Real follow-up:** camera disk preflight should consult the procedure's `duration_s` config when no method is present (free-runs always fall back to 3600 s today).
- **Unit / channel-kind validation surprises.** `psia` not in pint registry → use `psi`. `pressure` not a valid `ChannelKind` → use `process_var`. Both fixed in [configs/hardware/alicat_real.toml](configs/hardware/alicat_real.toml).
- **Sartorius first-byte race on cold open.** Got `frame too short: got 1 bytes (min 4)` on the first identify after a fresh plug-in; cleared on retry. **Follow-up:** retry-on-short-frame inside `SartoriusAdapter.open()`.
- **Watlow watchdog `device_silent` warnings during command-heavy bursts.** During §5.A's ramp (~9 cmds/s), the heater went silent for ~17 s twice. Cause: serial-port contention between the 1 Hz poll and the setpoint-write thread. **Follow-up:** serialize Watlow reads + writes through a single asyncio queue inside the adapter.
- **Webcam pump_failed mid-recipe.** `avcodec_send_packet() returned 22` (libx264 EINVAL) at t≈23s into §5.A. Camera recovered and bundle sealed, but only 699 frames captured. **Worth a regression test once root-caused.**
- **Event-loop starvation by visible webcam pump.** §5.B recipe ran 2.7× slower than wall clock (5 min 16 s for what should be 115 s). Webcam captured at ~14 fps instead of 30 fps. The PyAV `frame.reformat().to_ndarray()` runs on the event loop and is CPU-heavy. **Follow-up:** move `reformat()` to `anyio.to_thread.run_sync()`, or move the whole pump to a dedicated worker thread.
- **Camera disk preflight projects against the runs-root, not the bundle path.** With `/tmp` as tmpfs (16 GB), the 3.6 GB × 3 projection blocked §5.B even though actual bundle usage was tiny. Switching runs-root to `/home/gbellamy/capa-runs` (817 GB free) unblocked it. **Follow-up:** preflight should account for runs-root being a different mount than `/tmp`, or the engine should warn explicitly when runs-root looks like tmpfs.
- **Plugin trust check requires `--plugins-lock` CLI flag explicitly.** `CAPA_PLUGIN_MODE=production` alone is silently no-op. The CLI doesn't auto-discover `./plugins.lock`. **Follow-up:** in production mode, error out (or auto-look-up) when no lock path is provided. Also: `capa plugins trust` writes to `./plugins.lock` based on cwd, but `capa run` reads from a flag — the asymmetry is a footgun.
- **Plugin distribution_hash uses dist-info METADATA + RECORD only**, not source files. Tampering with source files in editable installs doesn't trip HASH_MISMATCH. By design (per the docstring) but worth documenting prominently — operators may expect "rebuild = retrust required".

**Operator notes:**
- Belkin USB-RS232 adapter to Alicat → Prolific PL2303 (vendor 067b)
- Sartorius onboard interface → FTDI FT232R (vendor 0403)
- Watlow via B&B 485USBTB-2W → vendor 0856 (needs `ftdi_sio` new_id bind on each plug)
- 7.0.3 kernel modules tree restored from pacman cache without rebooting; reboot is safe whenever the operator wants.

---

## 0. Phase status going in

Recorded against the working tree as of **2026-05-09**. 456 tests collected, all green; 4 hardware-marked tests skip without `CAPA_HARDWARE_TESTS=1`.

| Phase | Status | Hardware-day surface |
|---|---|---|
| **P0a** schema + sim substrate | ✅ shipped | nothing — covered by sim |
| **P0b** bundle writer | ✅ shipped | exercised end-to-end by every device test below |
| **P0c** headless engine + catalog + CLI | ✅ shipped | `capa run --headless` is the entrypoint for everything |
| **P0d** Watlow real adapter | ✅ code; ⏳ silicon | §3.2 |
| **P1** UI core | ✅ shipped | §6 |
| **P2** real device adapters (NI-DAQ / Alicat / Sartorius) | ✅ code; ⏳ silicon | §3.3 / §3.4. NI-DAQ deferred. |
| **P3** methods + procedures + plugin trust + CAPA pyrolysis profile | ✅ shipped (UI deferred to P3.1) | §5 |
| **P4** cameras (capa core A–D, capa-flir F+G) | ✅ code; ⏳ Stage H | §3.1 (webcam) + §4 (E85) |

**Outstanding code-level work after hardware day:** P3.1 UI items (method editor, auto-form generator, dynamic preflight relocation, dedicated `profiles/<id>.toml` snapshot, SafetyMonitor). None blocked by hardware day.

---

## 1. Workflow contract

For every device-level test in §3:

1. **Open + identify** — adapter constructs, opens, reports vendor identity into `manifest.json.devices[].identity`.
2. **Read** — at least one nominal sample appears in `device_records/<dev>.parquet` and (after calibration) `scalars.parquet`.
3. **Authorised write** — exercise `Authorization.issue()` round-trip with a no-op or safe value (do not change physical state).
4. **30–60 s free-run bundle** — `capa run --headless <yaml>` produces `run_status=completed`, `bundle_status=sealed`, `integrity_status=ok`.
5. **Catalog verify** — `capa catalog verify <run-dir>` returns clean.
6. **Test stays committed** — once it passes, the smoke test stays under [tests/hardware/](tests/hardware/) so the next hardware day is regression-checked. (Git ceremony deferred — see operator note below.)

> **Operator note (2026-05-09):** the operator chose to skip per-step git commits during this session and let the assistant run end-to-end. Any squash / rebase / commit shaping is done after §8 instead of between sections.

Every section below follows this workflow contract; the differences are which adapter, which env vars, and which acceptance criteria.

---

## 2. Phase 0 — author + prep, no hardware required

**Assistant runs all of this without operator action.** This exists to keep Phase 1 reduced to "plug, run, observe."

### 2.1 Baseline tests

- [x] `uv run pytest -q` in [/home/gbellamy/Documents/git/capa](/home/gbellamy/Documents/git/capa) — **452 passed + 4 hardware-skipped** (was documented as "456/456" but 4 are hardware-gated and skip without env var; total collected is 456).
- [x] `uv run pytest -q` in [/home/gbellamy/Documents/git/capa-flir](/home/gbellamy/Documents/git/capa-flir) — **10 passed + 4 hardware-skipped** (was documented as "14/14"; same collected-vs-passing distinction).
- [x] Confirm Atlas SDK still in place: `ls /opt/flir/atlas-c-sdk-linux-gcc11-x64-2.19.0/lib/libatlas_c_sdk.so` (verified present).

### 2.2 Author missing artefacts

The Watlow scaffolding ([watlow_real.toml](configs/hardware/watlow_real.toml), [watlow_real_freerun.yaml](configs/experiments/watlow_real_freerun.yaml), [test_watlow_smoke.py](tests/hardware/test_watlow_smoke.py)) is the template. Mirror its shape for everything else.

- [x] [configs/hardware/webcam_real.toml](configs/hardware/webcam_real.toml) + [configs/experiments/webcam_real_freerun.yaml](configs/experiments/webcam_real_freerun.yaml) + [tests/hardware/test_webcam_smoke.py](tests/hardware/test_webcam_smoke.py).
- [x] [configs/hardware/alicat_real.toml](configs/hardware/alicat_real.toml) + [configs/experiments/alicat_real_freerun.yaml](configs/experiments/alicat_real_freerun.yaml) + [tests/hardware/test_alicat_smoke.py](tests/hardware/test_alicat_smoke.py). Single `carrier_mfc` device, channels for `Mass_Flow`, `Abs_Press`, `Mass_Flow_Setpt`.
- [x] [configs/hardware/sartorius_real.toml](configs/hardware/sartorius_real.toml) + [configs/experiments/sartorius_real_freerun.yaml](configs/experiments/sartorius_real_freerun.yaml) + [tests/hardware/test_sartorius_smoke.py](tests/hardware/test_sartorius_smoke.py). Single `balance` device, `mass` channel.
- [x] [configs/hardware/flir_e85_real.toml](configs/hardware/flir_e85_real.toml) + [configs/experiments/flir_e85_freerun.yaml](configs/experiments/flir_e85_freerun.yaml) — references E85 via the `capa_flir.flir_ir` module path. 60 s @ 30 Hz.
- [x] capa-flir hardware-marked tests at [/home/gbellamy/Documents/git/capa-flir/tests/unit/test_e85_hardware.py](../capa-flir/tests/unit/test_e85_hardware.py) covering: `discover()` returns ≥1 camera, `open()` against the discovered camera succeeds, `start_recording → stop_recording` produces a non-empty `.csq`.
- [x] [configs/hardware/capa_real_partial_a.toml](configs/hardware/capa_real_partial_a.toml) + [configs/hardware/capa_real_partial_b.toml](configs/hardware/capa_real_partial_b.toml) — same channel topology as [sim_capa.toml](configs/hardware/sim_capa.toml), split per the §5 sub-pairing strategy. **NI-DAQ kept as sim** in both.
- [x] [configs/experiments/capa_real_partial_a.yaml](configs/experiments/capa_real_partial_a.yaml) + [configs/experiments/capa_real_partial_b.yaml](configs/experiments/capa_real_partial_b.yaml) — both reference [sim_capa_pyrolysis.method.toml](configs/methods/sim_capa_pyrolysis.method.toml) and `capa.profiles.capa_pyrolysis`. Both pass `capa profile validate`.

### 2.3 Universal env vars (operator runs these once)

Fish syntax. Adjust ports per device when each is plugged in (Phase 1 will confirm).

```fish
set -Ux CAPA_HARDWARE_TESTS 1
set -Ux CAPA_TEST_WATLOW_PORT      /dev/ttyUSB0
set -Ux CAPA_TEST_WATLOW_ADDR      1
set -Ux CAPA_TEST_WATLOW_PROTOCOL  stdbus
set -Ux CAPA_TEST_WATLOW_OPERATOR  hw-day-2026-05-09
set -Ux CAPA_TEST_ALICAT_PORT      /dev/ttyUSB0
set -Ux CAPA_TEST_SARTORIUS_PORT   /dev/ttyUSB0
set -Ux CAPA_TEST_WEBCAM_DEVICE    /dev/video2     # external webcam — index TBD at plug-in
set -Ux CAPA_FLIR_ATLAS_ROOT       /opt/flir/atlas-c-sdk-linux-gcc11-x64-2.19.0
set -Ux --path LD_LIBRARY_PATH     $CAPA_FLIR_ATLAS_ROOT/lib $LD_LIBRARY_PATH
```

> Because devices come in one-at-a-time, all three serial vars can default to `/dev/ttyUSB0` — the assistant will re-set the relevant one at each Phase 1 step after `ls /dev/ttyUSB*` confirms what enumerated.

### 2.4 Permissions (already verified)

The operator is in the `uucp` group (Arch Linux's serial group), so `/dev/ttyUSB*` should be readable without root. Double-check at plug-time:

```sh
ls -l /dev/ttyUSB* /dev/video*
```

If a fresh device shows up only as root, write a udev rule rather than running tests as root.

---

## 3. Phase 1 — per-device smoke, one at a time

**Stop-and-plug gate before each subsection.** Operator plugs the device, confirms enumeration with `ls /dev/ttyUSB*` (or `/dev/video*`), tells the assistant which port. Assistant updates the relevant env var, runs `capa devices discover` to confirm the adapter sees the device, then runs the smoke test + freerun.

Order chosen to de-risk the bundle/manifest pipeline early and front-load the cheapest physical setup:

| # | Device | Why this order |
|---|---|---|
| 3.1 | External webcam | Cheapest — no calibration, no protocol guesswork. Validates the cameras+manifest path before adding serial complexity. |
| 3.2 | Watlow | Has scaffolding ready. Validates serial + authorization gate. |
| 3.3 | Alicat MFC | Mirrors Watlow shape. Adds streaming poll path. |
| 3.4 | Sartorius balance | Same shape, simplest channel set. |
| 3.5 | FLIR E85 | Highest risk (Atlas threading + .csq compatibility). Done last so any breakage is isolated from the rest. |

### 3.1 External webcam (P4 stage B — real) ✅ done

**Operator action taken:** plugged Logitech C930e. UVC exposed two nodes — `/dev/video4` (capture) and `/dev/video5` (metadata). Capture is always the lower index.

Ran:

```sh
uv run pytest -q tests/hardware/test_webcam_smoke.py -m hardware    # 2 passed in 11.4s
uv run capa run --headless configs/experiments/webcam_real_freerun.yaml --runs-root /tmp/capa-hw-day
```

**Acceptance — all pass:**
- [x] `<bundle>/video/visible_cam0.mkv` exists (1.27 MB), valid MKV.
- [x] `<bundle>/video/visible_cam0.frames.parquet` row count = 891 (over 30 s ≈ 29.7 fps; spec target is 30 fps).
- [x] `manifest.json.cameras[0].frame_count` = 891 (matches parquet exactly).
- [x] Disk-space preflight ran without warnings (after the `estimated_bps` adjustment in the live-status anomalies).
- ⚠ `manifest.json.cameras[0].identity` is `None` — webcam adapter doesn't extract a model/serial from V4L2. Only `adapter` + `name` populate. Acceptable; documented as expected behavior.

Bundle: `/tmp/capa-hw-day/2026-05-09_142235_WEBCAM-REAL-001`.

### 3.2 Watlow (P0d) ⏸ blocked on reboot

**Status (2026-05-09):** Watlow physically plugged via B&B Electronics 485USBTB-2W (USB ID `0856:ac33`). Discovered two stacked blockers:
1. Running kernel 7.0.3-1-cachyos has no module tree (only the 7.0.5 + LTS trees exist) — **reboot required** to land on 7.0.5 where `ftdi_sio` is available.
2. After reboot, the B&B VID:PID needs to be added to `ftdi_sio`'s whitelist via `echo "0856 ac33" > /sys/bus/usb-serial/drivers/ftdi_sio/new_id`. Optional persistent udev rule available.

Resume after reboot. Bus address = `1`, protocol = `stdbus`.

**Stop-and-plug — operator action (when resuming):**
- Plug Watlow USB-serial adapter (already plugged at pause point — replug if removed).
- Run the FTDI bind sequence above.
- `ls /dev/ttyUSB*` — confirm the port.

Assistant runs:

```sh
uv run pytest -q tests/hardware/test_watlow_smoke.py -m hardware
uv run capa validate --strict configs/experiments/watlow_real_freerun.yaml
uv run capa run --headless     configs/experiments/watlow_real_freerun.yaml
```

**Acceptance:**
- [ ] `device_info.part_number.raw` non-empty (PM-class controller identified).
- [ ] PV reading in plausible °C / °F range (-200 … 1500).
- [ ] No-op setpoint write returns `accepted=True` (authorisation + confirm path).
- [ ] Bundle sealed; `device_records/watlow.parquet` ≥ 30 rows; `scalars.parquet` ≥ 30 rows.
- [ ] `manifest.json.devices[0].identity` includes part number, firmware, hardware id.

### 3.3 Alicat MFC (P2)

**Stop-and-plug — operator action:**
- Unplug Watlow if rig only allows one serial at a time; plug Alicat USB-serial.
- `ls /dev/ttyUSB*` — confirm the port.
- Confirm baud (default `19200`) and unit id (default `A`).

Assistant runs:

```sh
uv run pytest -q tests/hardware/test_alicat_smoke.py -m hardware
uv run capa validate --strict configs/experiments/alicat_real_freerun.yaml
uv run capa run --headless     configs/experiments/alicat_real_freerun.yaml
```

**Acceptance:**
- [ ] `device_info` populated (vendor identity from gas-frame poll).
- [ ] Mass flow + abs pressure both readable; numerics plausible for ambient state.
- [ ] Setpoint echo (write current setpoint as new setpoint) accepted.
- [ ] Bundle sealed; both alicat parquet sidecar and scalars populated.

### 3.4 Sartorius balance (P2)

**Stop-and-plug — operator action:**
- Unplug Alicat; plug Sartorius USB-serial.
- `ls /dev/ttyUSB*` — confirm the port.
- Confirm baud + protocol kind (xBPI vs SBI).

Assistant runs:

```sh
uv run pytest -q tests/hardware/test_sartorius_smoke.py -m hardware
uv run capa validate --strict configs/experiments/sartorius_real_freerun.yaml
uv run capa run --headless     configs/experiments/sartorius_real_freerun.yaml
```

**Acceptance:**
- [ ] Mass reading from empty pan ≈ 0 ± noise.
- [ ] Tare command (`Authorization.issue` path) accepted; subsequent reads center on 0.
- [ ] Bundle sealed.

### 3.5 NI-DAQ — **deferred**

No Linux driver. Note in the run report that this remains tested only against sim adapters. Schedule a Windows-rig hardware day to close the loop.

---

## 4. Phase 1 (continued) — FLIR E85 (P4 Stage H)

This is the last code-level gate for P4. Lift directly from [p4-ir-handoff.md](p4-ir-handoff.md).

**Stop-and-plug — operator action:**
- Plug FLIR E85 via USB. **Avoid sharing a hub with other high-bandwidth devices.**
- Confirm enumeration: `lsusb | grep -i flir`.

### 4.1 SDK + binding sanity

```sh
cd /home/gbellamy/Documents/git/capa-flir
.venv/bin/pytest -q                                   # 14 SDK-free tests
CAPA_FLIR_HARDWARE_TESTS=1 .venv/bin/pytest -q -m hardware
```

**Acceptance:**
- [ ] All 14 baseline tests still pass.
- [ ] Hardware-marked tests cover: `discover()` returns ≥1 camera; `open()` against the discovered camera succeeds; `start_recording → stop_recording` produces a non-empty `.csq`.

### 4.2 60-second engine-level record

```sh
uv run capa run --headless configs/experiments/flir_e85_freerun.yaml
```

**Acceptance (verbatim from handoff):**
- [ ] `<bundle>/video/ir_cam0.csq` non-empty.
- [ ] `manifest.json.cameras[0].frame_count` ≈ 1800.
- [ ] `manifest.sha256` covers `.csq` + `.csq.meta.json` + `.frames.parquet`.
- [ ] `.csq` opens cleanly in FLIR Tools / Research IX (vendor calibration metadata round-tripped).

### 4.3 Known traps to watch for

- **Atlas threading vs. AnyIO** — if `OnImageReceived` thread hangs the loop, fall back to the §12.2 sidecar daemon. Push **only** the `addImage` call into the callback; everything else stays on the engine loop.
- **Linux `.csq` compatibility** — manual pump path means Linux behaves identically to Windows. If FFF headers are malformed only on Linux, regression is in recorder lifecycle (alloc/start/stop ordering), not capture.
- **File-size growth detection** — 5 Hz polling is fine for 30 Hz capture; for slower frame rates, increase the stall grace period.

### 4.4 Outcome on hardware day (2026-05-09): ⏸ deferred to Windows rig

**Status:** All three hardware-marked tests authored in [/home/gbellamy/Documents/git/capa-flir/tests/unit/test_e85_hardware.py](../capa-flir/tests/unit/test_e85_hardware.py) failed at `discover()` returning 0 cameras even with the E85 plugged in (`lsusb` enumerated as `09cb:1007 FLIR Systems Ex-Series UVC and MSD interface`).

**Root cause:** `lsusb -v -d 09cb:1007` shows the E85 currently presents only three USB interfaces — Video Control (UVC), Video Streaming (UVC), Mass Storage (SCSI). **No FLIR-vendor interface is exposed**, so Atlas has nothing to discover. This is by-design factory behaviour for the Ex-Series; the camera only switches to "vendor + UVC + MSD" mode after a vendor-specific USB control message is sent. On Windows this happens automatically when the FLIR USB driver (.msi installer) is present. There is no equivalent driver on Linux — the Atlas C SDK assumes the vendor interface already exists.

**What was tested:**
- ✅ All capa-flir SDK-free + Atlas-marked unit tests still pass (10 + 4 = 14 collected; the 4 hardware-marked tests would have closed Stage H).
- ✅ Atlas SDK loads cleanly, `DiscoveryHandle` / `CameraHandleAtlas` / `RecorderHandle` allocate + free without leak.
- ❌ End-to-end `discover() → open() → start_recording → stop_recording` never executed because the camera is invisible to Atlas via this Linux USB path.

**Decision:** capa-flir Stage H stays open. Schedule a Windows rig day alongside the NI-DAQ Windows day to close it. Variant B of §5 falls back to a sim FLIR for the IR-camera plumbing test — sim coverage already proves the engine can interleave a visible + IR camera pair.

**Follow-up (out of hardware-day scope):** investigate whether the vendor mode-switch is a documented libusb control transfer (some FLIR community projects exist), or accept that Linux Atlas-USB requires a Windows-class driver and document this as a permanent capa-flir Linux limitation.

---

## 5. Phase 2 — CAPA pyrolysis multi-device integration (P3 end-to-end), split

This is the actual point of hardware day: prove the §16.1 deliverable runs against real silicon. Because the operator can only attach 2–3 devices at once, the original single integration run is split into two complementary sub-pairings. Together they exercise every adapter combination the original §5 would have.

| Run | Real devices | Sim devices | What it proves |
|---|---|---|---|
| **5.A** | Watlow + Alicat + external webcam | Sartorius + E85 + NI-DAQ | The control loop (heater + carrier flow), authorization + method-step path, visible-camera frame interleaving |
| **5.B** | FLIR E85 + Sartorius + external webcam | Watlow + Alicat + NI-DAQ | The full camera pair (visible + IR) interleaving, balance-derived mass channel, vendor-calibration .csq round-trip |

Each run produces its own sealed bundle. Acceptance for both is the union of §5.2 and §5.3 below.

### 5.A — Control + visible

**Stop-and-plug — operator action:**
- Plug Watlow + Alicat + external webcam (3 simultaneous).
- Confirm `/dev/ttyUSB0`, `/dev/ttyUSB1`, `/dev/video?` and tell the assistant which is which.

Assistant authors / regenerates [configs/hardware/capa_real_partial_a.toml](configs/hardware/capa_real_partial_a.toml) (Watlow + Alicat + webcam real; Sartorius + E85 + NI-DAQ sim) and runs §5.2 + §5.3 against [configs/experiments/capa_real_partial_a.yaml](configs/experiments/capa_real_partial_a.yaml).

### 5.B — Cameras + mass

**Stop-and-plug — operator action:**
- Unplug Watlow + Alicat. Keep webcam plugged. Plug FLIR E85 + Sartorius.
- Confirm enumeration and tell the assistant which `/dev/ttyUSB?` Sartorius landed on.

Assistant authors / regenerates [configs/hardware/capa_real_partial_b.toml](configs/hardware/capa_real_partial_b.toml) (E85 + Sartorius + webcam real; Watlow + Alicat + NI-DAQ sim) and runs §5.2 + §5.3 against [configs/experiments/capa_real_partial_b.yaml](configs/experiments/capa_real_partial_b.yaml).

### 5.2 Preflight (both runs)

```sh
uv run capa profile validate configs/experiments/capa_real_partial_<a|b>.yaml
uv run capa validate --strict  configs/experiments/capa_real_partial_<a|b>.yaml
```

**Acceptance:**
- [ ] Profile validation passes — every required `capa_group` (`heater_setpoint`, `heater_pv`, `sample_temperature`, `carrier_gas_flow`) has at least one channel (sim is fine).
- [ ] Strict validation handshakes against every real device without errors.
- [ ] `capa.disk_projection` preflight returns no blocking problems.

### 5.3 Recipe run (both runs)

```sh
uv run capa run --headless configs/experiments/capa_real_partial_<a|b>.yaml
```

**Acceptance:**
- [ ] Run completes (`run_status=completed`).
- [ ] `manifest.json.devices[].identity` populated for every real device; sim devices show the sim sentinel.
- [ ] `manifest.json.cameras[]` has the cameras present in this sub-pairing with frame counts matching method duration.
- [ ] `scalars.parquet` contains rows for every required `capa_group` channel.
- [ ] Mass channel populated (real Sartorius in 5.B; sim in 5.A — assistant verifies sim still emits).
- [ ] Every method-step command in `events.sqlite` carries `authorization_id` and `issued_by`. SQL check:
  ```sql
  select count(*) from events where kind='method.command.issued' and authorization_id is null;
  -- must return 0
  ```
- [ ] `capa catalog verify <run-dir>` returns clean.

### 5.4 Plugin trust mode (run once after 5.A or 5.B has produced a real bundle)

While we have a real run on disk, exercise production trust:

- [ ] `capa plugins list` against the run config — confirm every plugin loads in dev mode.
- [ ] `capa plugins trust capa.builtin.recipe_runner --reason "hw-day-2026-05-09"` writes a lock entry + audit journal row.
- [ ] Re-run with `CAPA_PLUGIN_MODE=production` and confirm the run still loads (lock entries match).
- [ ] Mutate a plugin file (touch a no-op comment), re-run with `production` mode, confirm load is **rejected** with `HASH_MISMATCH`.
- [ ] Revert and confirm production load passes again.

---

## 6. Phase 3 — UI smoke against the live rig (P1)

Run the GUI against whichever sub-pairing is currently plugged. Confirm what no headless test can.

```sh
uv run capa
# → File → Open → configs/experiments/capa_real_partial_<a|b>.yaml → Setup → Run
```

**Acceptance:**
- [ ] All four channel-group plots track real data on the PyQtGraph plot pane without freezing the UI.
- [ ] Numerics dock updates at the configured rate.
- [ ] Webcam preview renders in a dock without backpressuring acquisition (queue-depth metric stable in status bar).
- [ ] Status bar shows live queue depths, writer lag, disk free, operator id, camera health (the §10 contract).
- [ ] Click **Abort** mid-run → bundle finalizes as `bundle_status=aborted` (not `crashed`); `run_status=aborted`.
- [ ] Re-open the same config; second run creates a distinct bundle.

---

## 7. Phase 3 — Crash recovery

Exercise [test_crash_recovery.py](tests/integration/test_crash_recovery.py) shape against real silicon — only hardware day catches file-flush bugs that don't surface against sims. Use whichever sub-pairing is still plugged.

1. Start a 5-minute run against the active `capa_real_partial_<a|b>.yaml`.
2. Wait ~60 s.
3. From a second shell: `kill -9 <engine pid>`.
4. Run `capa finalize <run-dir>`.

**Acceptance:**
- [ ] Bundle finalizes as `bundle_status=sealed_after_crash`.
- [ ] `device_records/*.parquet` not corrupt — readable end-to-end with pyarrow.
- [ ] `manifest.sha256` regenerated; `capa catalog verify` returns clean.
- [ ] `events.sqlite` contains the last events written before the kill (no rollback).

---

## 8. Report template

Append a `hardware-day-results-2026-05-09.md` to the repo with the following structure:

```markdown
# Hardware day results — 2026-05-09

## Devices exercised

| Device | Port / addr | Test artefact | Outcome | Notes |
|---|---|---|---|---|
| Webcam (external) | /dev/video?  | tests/hardware/test_webcam_smoke.py | ✅ / ❌ | model + resolution |
| Watlow | /dev/ttyUSB?, addr 1, stdbus | tests/hardware/test_watlow_smoke.py | … | … |
| Alicat | /dev/ttyUSB? | tests/hardware/test_alicat_smoke.py | … | … |
| Sartorius | /dev/ttyUSB? | tests/hardware/test_sartorius_smoke.py | … | … |
| FLIR E85 | USB | capa-flir hardware-marked tests + flir_e85_freerun.yaml | … | … |
| NI-DAQ | — | (deferred to Windows rig) | n/a | Linux driver gap |

## Bundles produced

- `<run-dir>` — Webcam free-run, sealed, 30 s
- `<run-dir>` — Watlow free-run, sealed, 30 s
- `<run-dir>` — Alicat free-run, sealed, 30 s
- `<run-dir>` — Sartorius free-run, sealed, 30 s
- `<run-dir>` — FLIR E85 free-run, sealed, 60 s
- `<run-dir>` — capa_real_partial_a (Watlow + Alicat + webcam real)
- `<run-dir>` — capa_real_partial_b (E85 + Sartorius + webcam real)

## Anomalies

- Threading / timing surprises observed
- Vendor identity that differed from spec
- Configs that needed adjustment beyond plan defaults

## P3 production-mode plugin trust check

- Lock journal entries created: …
- HASH_MISMATCH rejection observed: ✅ / ❌

## Crash-recovery outcome

- `sealed_after_crash` reproduced: ✅ / ❌
- Events lost in last second before kill: …

## Follow-ups

- New tests committed: tests/hardware/test_alicat_smoke.py, …
- Configs committed: configs/hardware/{alicat,sartorius,webcam}_real.toml, capa_real_partial_{a,b}.toml, …
- Bugs filed: …
- Items deferred (NI-DAQ Windows day, FLIR App Review, etc.)
```

---

## 9. Done definition

Hardware day is complete when:

- [ ] §3.1–3.4 — Webcam ✅, Watlow ⏸, Alicat ⏸, Sartorius ⏸ each have a passing hardware-marked test under `tests/hardware/` and a sealed free-run bundle on disk.
- [ ] §4 — capa-flir Stage H closed: 60-s E85 run produces a sealed bundle meeting all four §4.2 criteria.
- [ ] §5 — Two sealed CAPA pyrolysis bundles (5.A and 5.B) with profile validation green and authorisation events fully populated.
- [ ] §5.4 — Production-mode plugin trust gate exercised both happy-path and rejection-path.
- [ ] §6 — UI smoke passed against one of the live sub-pairings, including abort and camera preview.
- [ ] §7 — One crash → finalize cycle produces a `sealed_after_crash` bundle.
- [ ] §8 — Results doc written.

After this, the only outstanding capa work is the P3.1 UI follow-ups (method editor, auto-form generator, dynamic preflight relocation, profile snapshot file, SafetyMonitor) and the Windows rig day for NI-DAQ.

---

## 10. Follow-up code work surfaced during hardware day

Not blockers for hardware day completion, but worth tracking:

- ✅ **Engine deadlock with camera-only configs** — fixed inline at [src/capa/experiment/engine.py:838-843](src/capa/experiment/engine.py#L838-L843). Worth a regression test (`tests/integration/test_engine_camera_only.py`) so this doesn't come back.
- ⏳ **Camera disk preflight ignores procedure-level `duration_s`.** Free-runs surface `duration_s` only on the `FreeRun.config`, not on `Method.total_duration_s()`, so the camera disk preflight always falls back to 3600 s. This forced all the `estimated_bps` values down. Real fix: have `disk_space_preflight_problems` also consult `procedure.config.duration_s` when no method is present. ~10 line change in [src/capa/experiment/cameras.py:189-205](src/capa/experiment/cameras.py#L189-L205).
- ⏳ **Webcam adapter doesn't populate `CameraInfo.model` / `serial` from V4L2.** `v4l2-ctl --info` exposes the card name and bus path; could surface those as `model`/`serial` in `WebcamAdapter.open()` so the manifest's `cameras[].identity` isn't always `None`. Optional polish.
- ⏳ **Persistent udev rule for B&B 485USBTB-2W.** Drafted in chat but not committed. If hardware-day runs become routine, add `99-bb-485usbtb.rules` to a docs/ops folder.
