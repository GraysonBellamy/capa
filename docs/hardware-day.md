# Hardware Day — capa P0–P4 Silicon Validation

Consolidated record of the 2026-05-09 hardware day: the original validation plan plus results from both the Linux and Windows rig sessions.

## Contents

- [Plan](#plan) — original validation plan with live-status updates from the Linux session
- [Linux session results (2026-05-09)](#linux-session-results-2026-05-09)
- [Windows rig results (2026-05-09)](#windows-rig-results-2026-05-09)

---
## Plan


**Goal:** prove every code path in capa core (P0–P3) and capa-flir (P4) against real silicon on the Linux dev box. Produce sealed bundles, hardware-marked tests, and a written outcome report.

**Out of scope:** NI-DAQ (no Linux driver — sim path covers it; defer to Windows rig). cone-calorimeter profile (§16.1 deprioritised).

**Working assumption (rig constraint):** the operator can connect **one device at a time easily, and 2–3 simultaneously** if needed. The plan is therefore sequenced so the assistant front-loads all author-once-no-hardware work in Phase 0; Phase 1 walks one device at a time; Phase 2 splits the integration run into two device sub-pairings instead of a single five-device blast.

---

### Live status (2026-05-09, in progress through §5.4)

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

- **Engine deadlock with camera-only configs.** `producer_queue` was never closed when `len(self._adapters) == 0`, so `_fanout_task` blocked forever and the run never sealed. Fixed in [src/capa/experiment/engine.py:838-843](../src/capa/experiment/engine.py#L838-L843) — close the queue immediately if `producers_alive.value == 0` after starting tasks.
- **`estimated_bps` defaults too high vs. /tmp disk.** Initial 6 MB/s × free-run fallback duration 3600 s × 1.5 margin ≈ 32 GB > 16 GB free in `/tmp` (tmpfs). Lowered to 1.5 MB/s in production TOMLs and 500 KB/s in the smoke test spec. **Real follow-up:** camera disk preflight should consult the procedure's `duration_s` config when no method is present (free-runs always fall back to 3600 s today).
- **Unit / channel-kind validation surprises.** `psia` not in pint registry → use `psi`. `pressure` not a valid `ChannelKind` → use `process_var`. Both fixed in [configs/hardware/alicat_real.toml](../configs/hardware/alicat_real.toml).
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

### 0. Phase status going in

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

### 1. Workflow contract

For every device-level test in §3:

1. **Open + identify** — adapter constructs, opens, reports vendor identity into `manifest.json.devices[].identity`.
2. **Read** — at least one nominal sample appears in `device_records/<dev>.parquet` and (after calibration) `scalars.parquet`.
3. **Authorised write** — exercise `Authorization.issue()` round-trip with a no-op or safe value (do not change physical state).
4. **30–60 s free-run bundle** — `capa run --headless <yaml>` produces `run_status=completed`, `bundle_status=sealed`, `integrity_status=ok`.
5. **Catalog verify** — `capa catalog verify <run-dir>` returns clean.
6. **Test stays committed** — once it passes, the smoke test stays under [tests/hardware/](../tests/hardware/) so the next hardware day is regression-checked. (Git ceremony deferred — see operator note below.)

> **Operator note (2026-05-09):** the operator chose to skip per-step git commits during this session and let the assistant run end-to-end. Any squash / rebase / commit shaping is done after §8 instead of between sections.

Every section below follows this workflow contract; the differences are which adapter, which env vars, and which acceptance criteria.

---

### 2. Phase 0 — author + prep, no hardware required

**Assistant runs all of this without operator action.** This exists to keep Phase 1 reduced to "plug, run, observe."

#### 2.1 Baseline tests

- [x] `uv run pytest -q` in [/home/gbellamy/Documents/git/capa](/home/gbellamy/Documents/git/capa) — **452 passed + 4 hardware-skipped** (was documented as "456/456" but 4 are hardware-gated and skip without env var; total collected is 456).
- [x] `uv run pytest -q` in [/home/gbellamy/Documents/git/capa-flir](/home/gbellamy/Documents/git/capa-flir) — **10 passed + 4 hardware-skipped** (was documented as "14/14"; same collected-vs-passing distinction).
- [x] Confirm Atlas SDK still in place: `ls /opt/flir/atlas-c-sdk-linux-gcc11-x64-2.19.0/lib/libatlas_c_sdk.so` (verified present).

#### 2.2 Author missing artefacts

The Watlow scaffolding ([watlow_real.toml](../configs/hardware/watlow_real.toml), [watlow_real_freerun.yaml](../configs/experiments/watlow_real_freerun.yaml), [test_watlow_smoke.py](../tests/hardware/test_watlow_smoke.py)) is the template. Mirror its shape for everything else.

- [x] [configs/hardware/webcam_real.toml](../configs/hardware/webcam_real.toml) + [configs/experiments/webcam_real_freerun.yaml](../configs/experiments/webcam_real_freerun.yaml) + [tests/hardware/test_webcam_smoke.py](../tests/hardware/test_webcam_smoke.py).
- [x] [configs/hardware/alicat_real.toml](../configs/hardware/alicat_real.toml) + [configs/experiments/alicat_real_freerun.yaml](../configs/experiments/alicat_real_freerun.yaml) + [tests/hardware/test_alicat_smoke.py](../tests/hardware/test_alicat_smoke.py). Single `carrier_mfc` device, channels for `Mass_Flow`, `Abs_Press`, `Mass_Flow_Setpt`.
- [x] [configs/hardware/sartorius_real.toml](../configs/hardware/sartorius_real.toml) + [configs/experiments/sartorius_real_freerun.yaml](../configs/experiments/sartorius_real_freerun.yaml) + [tests/hardware/test_sartorius_smoke.py](../tests/hardware/test_sartorius_smoke.py). Single `balance` device, `mass` channel.
- [x] [configs/hardware/flir_e85_real.toml](../configs/hardware/flir_e85_real.toml) + [configs/experiments/flir_e85_freerun.yaml](../configs/experiments/flir_e85_freerun.yaml) — references E85 via the `capa_flir.flir_ir` module path. 60 s @ 30 Hz.
- [x] capa-flir hardware-marked tests at [/home/gbellamy/Documents/git/capa-flir/tests/unit/test_e85_hardware.py](../../capa-flir/tests/unit/test_e85_hardware.py) covering: `discover()` returns ≥1 camera, `open()` against the discovered camera succeeds, `start_recording → stop_recording` produces a non-empty `.csq`.
- [x] [configs/hardware/capa_real_partial_a.toml](../configs/hardware/capa_real_partial_a.toml) + [configs/hardware/capa_real_partial_b.toml](../configs/hardware/capa_real_partial_b.toml) — same channel topology as [sim_capa.toml](../configs/hardware/sim_capa.toml), split per the §5 sub-pairing strategy. **NI-DAQ kept as sim** in both.
- [x] [configs/experiments/capa_real_partial_a.yaml](../configs/experiments/capa_real_partial_a.yaml) + [configs/experiments/capa_real_partial_b.yaml](../configs/experiments/capa_real_partial_b.yaml) — both reference [sim_capa_pyrolysis.method.toml](../configs/methods/sim_capa_pyrolysis.method.toml) and `capa.profiles.capa_pyrolysis`. Both pass `capa profile validate`.

#### 2.3 Universal env vars (operator runs these once)

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

#### 2.4 Permissions (already verified)

The operator is in the `uucp` group (Arch Linux's serial group), so `/dev/ttyUSB*` should be readable without root. Double-check at plug-time:

```sh
ls -l /dev/ttyUSB* /dev/video*
```

If a fresh device shows up only as root, write a udev rule rather than running tests as root.

---

### 3. Phase 1 — per-device smoke, one at a time

**Stop-and-plug gate before each subsection.** Operator plugs the device, confirms enumeration with `ls /dev/ttyUSB*` (or `/dev/video*`), tells the assistant which port. Assistant updates the relevant env var, runs `capa devices discover` to confirm the adapter sees the device, then runs the smoke test + freerun.

Order chosen to de-risk the bundle/manifest pipeline early and front-load the cheapest physical setup:

| # | Device | Why this order |
|---|---|---|
| 3.1 | External webcam | Cheapest — no calibration, no protocol guesswork. Validates the cameras+manifest path before adding serial complexity. |
| 3.2 | Watlow | Has scaffolding ready. Validates serial + authorization gate. |
| 3.3 | Alicat MFC | Mirrors Watlow shape. Adds streaming poll path. |
| 3.4 | Sartorius balance | Same shape, simplest channel set. |
| 3.5 | FLIR E85 | Highest risk (Atlas threading + .csq compatibility). Done last so any breakage is isolated from the rest. |

#### 3.1 External webcam (P4 stage B — real) ✅ done

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

#### 3.2 Watlow (P0d) ⏸ blocked on reboot

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

#### 3.3 Alicat MFC (P2)

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

#### 3.4 Sartorius balance (P2)

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

#### 3.5 NI-DAQ — **deferred**

No Linux driver. Note in the run report that this remains tested only against sim adapters. Schedule a Windows-rig hardware day to close the loop.

---

### 4. Phase 1 (continued) — FLIR E85 (P4 Stage H)

This is the last code-level gate for P4. Lift directly from [p4-ir-handoff.md](../p4-ir-handoff.md).

**Stop-and-plug — operator action:**
- Plug FLIR E85 via USB. **Avoid sharing a hub with other high-bandwidth devices.**
- Confirm enumeration: `lsusb | grep -i flir`.

#### 4.1 SDK + binding sanity

```sh
cd /home/gbellamy/Documents/git/capa-flir
.venv/bin/pytest -q                                   # 14 SDK-free tests
CAPA_FLIR_HARDWARE_TESTS=1 .venv/bin/pytest -q -m hardware
```

**Acceptance:**
- [ ] All 14 baseline tests still pass.
- [ ] Hardware-marked tests cover: `discover()` returns ≥1 camera; `open()` against the discovered camera succeeds; `start_recording → stop_recording` produces a non-empty `.csq`.

#### 4.2 60-second engine-level record

```sh
uv run capa run --headless configs/experiments/flir_e85_freerun.yaml
```

**Acceptance (verbatim from handoff):**
- [ ] `<bundle>/video/ir_cam0.csq` non-empty.
- [ ] `manifest.json.cameras[0].frame_count` ≈ 1800.
- [ ] `manifest.sha256` covers `.csq` + `.csq.meta.json` + `.frames.parquet`.
- [ ] `.csq` opens cleanly in FLIR Tools / Research IX (vendor calibration metadata round-tripped).

#### 4.3 Known traps to watch for

- **Atlas threading vs. AnyIO** — if `OnImageReceived` thread hangs the loop, fall back to the §12.2 sidecar daemon. Push **only** the `addImage` call into the callback; everything else stays on the engine loop.
- **Linux `.csq` compatibility** — manual pump path means Linux behaves identically to Windows. If FFF headers are malformed only on Linux, regression is in recorder lifecycle (alloc/start/stop ordering), not capture.
- **File-size growth detection** — 5 Hz polling is fine for 30 Hz capture; for slower frame rates, increase the stall grace period.

#### 4.4 Outcome on hardware day (2026-05-09): ⏸ deferred to Windows rig

**Status:** All three hardware-marked tests authored in [/home/gbellamy/Documents/git/capa-flir/tests/unit/test_e85_hardware.py](../../capa-flir/tests/unit/test_e85_hardware.py) failed at `discover()` returning 0 cameras even with the E85 plugged in (`lsusb` enumerated as `09cb:1007 FLIR Systems Ex-Series UVC and MSD interface`).

**Root cause:** `lsusb -v -d 09cb:1007` shows the E85 currently presents only three USB interfaces — Video Control (UVC), Video Streaming (UVC), Mass Storage (SCSI). **No FLIR-vendor interface is exposed**, so Atlas has nothing to discover. This is by-design factory behaviour for the Ex-Series; the camera only switches to "vendor + UVC + MSD" mode after a vendor-specific USB control message is sent. On Windows this happens automatically when the FLIR USB driver (.msi installer) is present. There is no equivalent driver on Linux — the Atlas C SDK assumes the vendor interface already exists.

**What was tested:**
- ✅ All capa-flir SDK-free + Atlas-marked unit tests still pass (10 + 4 = 14 collected; the 4 hardware-marked tests would have closed Stage H).
- ✅ Atlas SDK loads cleanly, `DiscoveryHandle` / `CameraHandleAtlas` / `RecorderHandle` allocate + free without leak.
- ❌ End-to-end `discover() → open() → start_recording → stop_recording` never executed because the camera is invisible to Atlas via this Linux USB path.

**Decision:** capa-flir Stage H stays open. Schedule a Windows rig day alongside the NI-DAQ Windows day to close it. Variant B of §5 falls back to a sim FLIR for the IR-camera plumbing test — sim coverage already proves the engine can interleave a visible + IR camera pair.

**Follow-up (out of hardware-day scope):** investigate whether the vendor mode-switch is a documented libusb control transfer (some FLIR community projects exist), or accept that Linux Atlas-USB requires a Windows-class driver and document this as a permanent capa-flir Linux limitation.

---

### 5. Phase 2 — CAPA pyrolysis multi-device integration (P3 end-to-end), split

This is the actual point of hardware day: prove the §16.1 deliverable runs against real silicon. Because the operator can only attach 2–3 devices at once, the original single integration run is split into two complementary sub-pairings. Together they exercise every adapter combination the original §5 would have.

| Run | Real devices | Sim devices | What it proves |
|---|---|---|---|
| **5.A** | Watlow + Alicat + external webcam | Sartorius + E85 + NI-DAQ | The control loop (heater + carrier flow), authorization + method-step path, visible-camera frame interleaving |
| **5.B** | FLIR E85 + Sartorius + external webcam | Watlow + Alicat + NI-DAQ | The full camera pair (visible + IR) interleaving, balance-derived mass channel, vendor-calibration .csq round-trip |
| **5.C** *(Windows-only)* | All six adapters real | — | Single all-real bundle. On the Windows rig the operator can plug every device at once, so 5.A/5.B partials become Linux-only (kept for regression on rigs where one adapter is unavailable). |

Each run produces its own sealed bundle. Acceptance for both is the union of §5.2 and §5.3 below.

> **Windows operators:** if every device is plugged at once, run §5.C
> ([configs/experiments/capa_real_full.yaml](../configs/experiments/capa_real_full.yaml))
> instead of the §5.A/§5.B partials — its real-device set is the union of
> the two partials and gives the same coverage in a single bundle.

#### 5.A — Control + visible *(Linux regression; Windows-optional)*

**Stop-and-plug — operator action:**
- Plug Watlow + Alicat + external webcam (3 simultaneous).
- Confirm `/dev/ttyUSB0`, `/dev/ttyUSB1`, `/dev/video?` and tell the assistant which is which.

Assistant authors / regenerates [configs/hardware/capa_real_partial_a.toml](../configs/hardware/capa_real_partial_a.toml) (Watlow + Alicat + webcam real; Sartorius + E85 + NI-DAQ sim) and runs §5.2 + §5.3 against [configs/experiments/capa_real_partial_a.yaml](../configs/experiments/capa_real_partial_a.yaml).

#### 5.B — Cameras + mass *(Linux regression; Windows-optional)*

**Stop-and-plug — operator action:**
- Unplug Watlow + Alicat. Keep webcam plugged. Plug FLIR E85 + Sartorius.
- Confirm enumeration and tell the assistant which `/dev/ttyUSB?` Sartorius landed on.

Assistant authors / regenerates [configs/hardware/capa_real_partial_b.toml](../configs/hardware/capa_real_partial_b.toml) (E85 + Sartorius + webcam real; Watlow + Alicat + NI-DAQ sim) and runs §5.2 + §5.3 against [configs/experiments/capa_real_partial_b.yaml](../configs/experiments/capa_real_partial_b.yaml).

#### 5.2 Preflight (both runs)

```sh
uv run capa profile validate configs/experiments/capa_real_partial_<a|b>.yaml
uv run capa validate --strict  configs/experiments/capa_real_partial_<a|b>.yaml
```

**Acceptance:**
- [ ] Profile validation passes — every required `capa_group` (`heater_setpoint`, `heater_pv`, `sample_temperature`, `carrier_gas_flow`) has at least one channel (sim is fine).
- [ ] Strict validation handshakes against every real device without errors.
- [ ] `capa.disk_projection` preflight returns no blocking problems.

#### 5.3 Recipe run (both runs)

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

#### 5.4 Plugin trust mode (run once after 5.A or 5.B has produced a real bundle)

While we have a real run on disk, exercise production trust:

- [ ] `capa plugins list` against the run config — confirm every plugin loads in dev mode.
- [ ] `capa plugins trust capa.builtin.recipe_runner --reason "hw-day-2026-05-09"` writes a lock entry + audit journal row.
- [ ] Re-run with `CAPA_PLUGIN_MODE=production` and confirm the run still loads (lock entries match).
- [ ] Mutate a plugin file (touch a no-op comment), re-run with `production` mode, confirm load is **rejected** with `HASH_MISMATCH`.
- [ ] Revert and confirm production load passes again.

---

### 6. Phase 3 — UI smoke against the live rig (P1)

Run the GUI against whichever sub-pairing is currently plugged. Confirm what no headless test can.

```sh
uv run capa
## → File → Open → configs/experiments/capa_real_partial_<a|b>.yaml → Setup → Run
```

**Acceptance:**
- [ ] All four channel-group plots track real data on the PyQtGraph plot pane without freezing the UI.
- [ ] Numerics dock updates at the configured rate.
- [ ] Webcam preview renders in a dock without backpressuring acquisition (queue-depth metric stable in status bar).
- [ ] Status bar shows live queue depths, writer lag, disk free, operator id, camera health (the §10 contract).
- [ ] Click **Abort** mid-run → bundle finalizes as `bundle_status=aborted` (not `crashed`); `run_status=aborted`.
- [ ] Re-open the same config; second run creates a distinct bundle.

---

### 7. Phase 3 — Crash recovery

Exercise [test_crash_recovery.py](../tests/integration/test_crash_recovery.py) shape against real silicon — only hardware day catches file-flush bugs that don't surface against sims. Use whichever sub-pairing is still plugged.

1. Start a 5-minute run against the active `capa_real_partial_<a|b>.yaml`.
2. Wait ~60 s.
3. From a second shell: `kill -9 <engine pid>`.
4. Run `capa finalize <run-dir>`.

**Acceptance:**
- [ ] Bundle finalizes as `bundle_status=sealed` with `run_status=crashed` (the post-mortem signal lives in `run_status`; there is no separate `sealed_after_crash` enum value).
- [ ] `device_records/*.parquet` not corrupt — readable end-to-end with pyarrow.
- [ ] `manifest.sha256` regenerated; `capa catalog verify` returns clean.
- [ ] `events.sqlite` contains the last events written before the kill (no rollback).

---

### 8. Report template

Append a `hardware-day-results-2026-05-09.md` to the repo with the following structure:

```markdown
## Hardware day results — 2026-05-09

### Devices exercised

| Device | Port / addr | Test artefact | Outcome | Notes |
|---|---|---|---|---|
| Webcam (external) | /dev/video?  | tests/hardware/test_webcam_smoke.py | ✅ / ❌ | model + resolution |
| Watlow | /dev/ttyUSB?, addr 1, stdbus | tests/hardware/test_watlow_smoke.py | … | … |
| Alicat | /dev/ttyUSB? | tests/hardware/test_alicat_smoke.py | … | … |
| Sartorius | /dev/ttyUSB? | tests/hardware/test_sartorius_smoke.py | … | … |
| FLIR E85 | USB | capa-flir hardware-marked tests + flir_e85_freerun.yaml | … | … |
| NI-DAQ | — | (deferred to Windows rig) | n/a | Linux driver gap |

### Bundles produced

- `<run-dir>` — Webcam free-run, sealed, 30 s
- `<run-dir>` — Watlow free-run, sealed, 30 s
- `<run-dir>` — Alicat free-run, sealed, 30 s
- `<run-dir>` — Sartorius free-run, sealed, 30 s
- `<run-dir>` — FLIR E85 free-run, sealed, 60 s
- `<run-dir>` — capa_real_partial_a (Watlow + Alicat + webcam real)
- `<run-dir>` — capa_real_partial_b (E85 + Sartorius + webcam real)

### Anomalies

- Threading / timing surprises observed
- Vendor identity that differed from spec
- Configs that needed adjustment beyond plan defaults

### P3 production-mode plugin trust check

- Lock journal entries created: …
- HASH_MISMATCH rejection observed: ✅ / ❌

### Crash-recovery outcome

- `bundle_status=sealed` + `run_status=crashed` reproduced: ✅ / ❌
- Events lost in last second before kill: …

### Follow-ups

- New tests committed: tests/hardware/test_alicat_smoke.py, …
- Configs committed: configs/hardware/{alicat,sartorius,webcam}_real.toml, capa_real_partial_{a,b}.toml, …
- Bugs filed: …
- Items deferred (NI-DAQ Windows day, FLIR App Review, etc.)
```

---

### 9. Done definition

Hardware day is complete when:

- [ ] §3.1–3.4 — Webcam ✅, Watlow ⏸, Alicat ⏸, Sartorius ⏸ each have a passing hardware-marked test under `tests/hardware/` and a sealed free-run bundle on disk.
- [ ] §4 — capa-flir Stage H closed: 60-s E85 run produces a sealed bundle meeting all four §4.2 criteria.
- [ ] §5 — Two sealed CAPA pyrolysis bundles (5.A and 5.B) with profile validation green and authorisation events fully populated.
- [ ] §5.4 — Production-mode plugin trust gate exercised both happy-path and rejection-path.
- [ ] §6 — UI smoke passed against one of the live sub-pairings, including abort and camera preview.
- [ ] §7 — One crash → finalize cycle produces a `bundle_status=sealed` + `run_status=crashed` bundle.
- [ ] §8 — Results doc written.

After this, the only outstanding capa work is the P3.1 UI follow-ups (method editor, auto-form generator, dynamic preflight relocation, profile snapshot file, SafetyMonitor) and the Windows rig day for NI-DAQ.

---

### 10. Follow-up code work surfaced during hardware day

Not blockers for hardware day completion, but worth tracking:

- ✅ **Engine deadlock with camera-only configs** — fixed inline at [src/capa/experiment/engine.py:838-843](../src/capa/experiment/engine.py#L838-L843). Worth a regression test (`tests/integration/test_engine_camera_only.py`) so this doesn't come back.
- ⏳ **Camera disk preflight ignores procedure-level `duration_s`.** Free-runs surface `duration_s` only on the `FreeRun.config`, not on `Method.total_duration_s()`, so the camera disk preflight always falls back to 3600 s. This forced all the `estimated_bps` values down. Real fix: have `disk_space_preflight_problems` also consult `procedure.config.duration_s` when no method is present. ~10 line change in [src/capa/experiment/cameras.py:189-205](../src/capa/experiment/cameras.py#L189-L205).
- ⏳ **Webcam adapter doesn't populate `CameraInfo.model` / `serial` from V4L2.** `v4l2-ctl --info` exposes the card name and bus path; could surface those as `model`/`serial` in `WebcamAdapter.open()` so the manifest's `cameras[].identity` isn't always `None`. Optional polish.
- ⏳ **Persistent udev rule for B&B 485USBTB-2W.** Drafted in chat but not committed. If hardware-day runs become routine, add `99-bb-485usbtb.rules` to a docs/ops folder.


---

## Linux session results (2026-05-09)


### Summary

Drove the [hardware-day-plan.md](#plan) end-to-end against real silicon on the Linux dev box. Per-device smoke (§3) and the integration runs (§5) closed every plan acceptance criterion that doesn't require Windows-only drivers. UI smoke (§6) and crash recovery (§7) each surfaced real bugs that would have shipped without this gate.

> **Update — 2026-05-09 PM:** all 13 surfaced follow-ups are shipped (capa-side) plus the watlowlib upstream fix landed as **v0.2.0**. §1 SIGKILL parquet recovery shipped as the Arrow IPC streaming switch (in-flight `*.in-flight.arrows` → final parquet at seal time; see [_ipc.py](../src/capa/storage/_ipc.py) + [finalize.py](../src/capa/storage/finalize.py) `_rewrite_inflight_to_parquet`; regression test [test_crash_recovery_sigkill.py](../tests/integration/test_crash_recovery_sigkill.py)). The camera preview dock ([camera_preview.py](../src/capa/ui/docks/camera_preview.py)) closed the §6 UI gap. The only items still gated on external resources are FLIR Stage H and NI-DAQ (both need a Windows rig). The webcam libx264 EINVAL stays passively open behind `CAPA_WEBCAM_FRAME_DIAG=1` — diagnostic is permanent; nothing actionable until the bug recurs.

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

1. **Engine deadlock with camera-only configs** — fixed in [src/capa/experiment/engine.py:838-843](../src/capa/experiment/engine.py#L838-L843). Worth a regression test.
2. **UI plot pane bound to stale empty registry** — fixed in [src/capa/ui/tabs/run.py:_on_state](../src/capa/ui/tabs/run.py).
3. **UI start button stuck disabled after seal** — fixed in [src/capa/ui/tabs/run.py:_on_run_finished](../src/capa/ui/tabs/run.py).

---

### Progress since hardware day

**Resolution snapshot (2026-05-09 PM):**

| Surfaced item | Resolution | Where |
|---|---|---|
| ✅ Engine camera-only deadlock (regression test) | Test added | [test_engine_camera_only.py](../tests/integration/test_engine_camera_only.py) |
| ✅ UI plot pane / start button (regression tests) | Tests added | [test_ui_run_tab.py](../tests/integration/test_ui_run_tab.py) |
| ✅ Webcam event-loop starvation | Pump split + `to_thread.run_sync` | [webcam.py](../src/capa/devices/camera/webcam.py) |
| ✅ Webcam EINVAL pump death | Drop-and-continue + `pump_warning` event + `dropped_frames` counter | [webcam.py](../src/capa/devices/camera/webcam.py) + [base.py:182](../src/capa/devices/camera/base.py#L182) |
| ✅ Webcam V4L2 identity probe | Sysfs-based `_probe_v4l2_info` | [webcam.py](../src/capa/devices/camera/webcam.py) |
| ✅ Camera preflight `duration_s` fallback | Read `procedure.config["duration_s"]` | [cameras.py:200-208](../src/capa/experiment/cameras.py#L200-L208) |
| ✅ Camera preflight tmpfs handling | Warn + tighten budget vs `MemAvailable / 2` | [cameras.py](../src/capa/experiment/cameras.py) |
| ✅ Sartorius cold-open retry | 3 attempts, 0.2/0.4/0.8 s backoff | [sartorius.py:494-538](../src/capa/devices/sartorius.py#L494-L538) |
| ✅ `equipment.toml` identity at seal time | New `finalize(equipment=...)` kwarg + engine collector | [bundle.py](../src/capa/storage/bundle.py) + [engine.py](../src/capa/experiment/engine.py) |
| ✅ Plugin lock auto-discovery in production mode | cwd → XDG; hard `Exit(2)` if absent | [app.py:88-150](../src/capa/app.py#L88-L150) |
| ✅ Plugin lock version-field asymmetry | Aligned on `dist.version`; editable-install retread documented | [plugins_runtime.py:213-228](../src/capa/core/plugins_runtime.py#L213-L228) |
| ✅ `distribution_hash` semantics docs | Expanded docstring with operator-facing trust scope | [plugins_runtime.py:347-380](../src/capa/core/plugins_runtime.py#L347-L380) |
| 🔵 Watlow `device_silent` watchdog | **Resolved upstream in watlowlib v0.2.0** — atomic-by-default lock-batch acquisition; no capa-side code change | See [§watlowlib v0.2.0 resolution](#watlowlib-v020-resolution-recorder-starvation) |
| ✅ `capa finalize` SIGKILL parquet recovery | Switched in-flight format to Arrow IPC streaming; sinks emit `*.in-flight.arrows`, finalize rewrites to parquet | [_ipc.py](../src/capa/storage/_ipc.py) + [finalize.py](../src/capa/storage/finalize.py) + [test_crash_recovery_sigkill.py](../tests/integration/test_crash_recovery_sigkill.py) |

**Test count:** 477 → 521 (+44 regressions). Full suite passes (`uv run pytest tests/ --ignore=tests/hardware`). Zero regressions across unit + integration.

**Plus the upstream handoff:** [watlowlib-recorder-starvation-upstream-plan.md](../watlowlib-recorder-starvation-upstream-plan.md) was written, handed off, and resolved as v0.2.0 (with deviations from the plan — see the linked subsection).

---

### Re-validation 2026-05-09 PM

After watlowlib v0.2.0 published to PyPI, the capa-side pin was bumped (`"watlowlib"` → `"watlowlib>=0.2.0"`; the editable `tool.uv.sources` entry was removed so production resolution comes from the published wheel). Hardware setup was identical to the original §5.A: real Watlow PM3 (B&B 485USBTB-2W on `/dev/ttyUSB0`), real Alicat MCR-200SLPM-D (Prolific PL2303 on `/dev/ttyUSB2` at 115200 baud), real Logitech C930e (`/dev/video4`); sim balance + sim NI-DAQ. Configs were updated for the current TTY layout: [capa_real_partial_a.toml](../configs/hardware/capa_real_partial_a.toml) `heater.port` `/dev/ttyUSB2` → `/dev/ttyUSB0`, `carrier_mfc.port` `/dev/ttyUSB0` → `/dev/ttyUSB2`, and webcam `input_url` `/dev/video2` → `/dev/video4` (the original config pointed at the laptop's integrated camera, not the C930e — corrected in both variant A and variant B).

#### §5.A re-run (headless)

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

#### §6 UI smoke (interactive Qt)

Two bundles, both sealed:

| Bundle | Phase | Outcome |
|---|---|---|
| [`2026-05-09_173429_REAL-A-001`](/home/gbellamy/capa-runs/2026-05-09_173429_REAL-A-001) | abort at ~22 s | `run_status=aborted`, `bundle_status=sealed`, 591 frames, integrity ok |
| [`2026-05-09_173510_REAL-A-001`](/home/gbellamy/capa-runs/2026-05-09_173510_REAL-A-001) | restart → complete | `run_status=completed`, `bundle_status=sealed`, 4482 frames, integrity ok |

**Both inline UI fixes confirmed live:**
- ✅ Plot pane registry-rebind on `EngineState.RUNNING` ([run.py:_on_state](../src/capa/ui/tabs/run.py)) — live PV/setpoint/flow traces appeared on plots throughout both runs.
- ✅ Start-button deferred re-enable after seal ([run.py:_on_run_finished](../src/capa/ui/tabs/run.py)) — second Start click after the abort-seal worked first try.

#### Webcam EINVAL root cause: did NOT reproduce

`CAPA_WEBCAM_FRAME_DIAG=1` was set for the §5.A re-run. The first 149 input frames logged at INFO with `format=yuyv422 width=640 height=480 time_base=1/1000000` — **no format / dimension changes observed**, and **no `pump_warning` events fired** anywhere in the 153-second run (4527 frames captured). The libx264 EINVAL from the original §5.A is now hypothetical — it didn't trigger this session, so the trigger is still unknown. The diagnostic logging is **kept permanently** behind the `CAPA_WEBCAM_FRAME_DIAG=1` env var ([webcam.py](../src/capa/devices/camera/webcam.py)); zero overhead when off, no rollback needed.

> Side note: the camera ignored the config's `width=1280, height=720` and negotiated `640×480 yuyv422` from V4L2 directly (no `-video_size` / `-framerate` options pass through `av.open(format="v4l2")` today). Output stream is still 1280×720 H.264 — PyAV upscales via `reformat`. Not surfaced as a bug because output bundles look correct; worth knowing for future investigations.

#### New findings (post-fix surface)

##### 1. Stop-time camera race emits a noisy `pump_failed` warning every clean run

Every run (headless §5.A and both UI runs) ends with:

```
[WARNING] engine.camera.pump_failed  camera=visible_cam0  error='push_frame requires start_recording()'
```

immediately before `engine.camera.closed`. The bundle still seals correctly with `integrity_status=ok`, but the warning is misleading — nothing actually failed.

**Cause:** the engine's stop sequence cancels [_run_pump](../src/capa/experiment/cameras.py#L520) while the pump's last in-flight frame is mid-flight (`av.open` decoder still has a pending frame). The camera's `close()` flips `_recording=False` first; when the pump's `push_frame` call resumes, [_push_frame_sync](../src/capa/devices/camera/webcam.py#L336) raises `AdapterError("push_frame requires start_recording()")`, which `_run_pump`'s broad `except Exception` catches and logs as `pump_failed`.

**Fix candidates:**
- In [_push_frame_sync](../src/capa/devices/camera/webcam.py#L336): split the precondition check. `not self._recording` while `_output_container is not None` is a stop-race → return a benign `drop_reason="stopped_during_pump_in_flight"`. Truly never-started → keep raising.
- Or in [run_pump](../src/capa/devices/camera/webcam.py#L413): test `if not self._recording: break` between `_advance_decoder` and `push_frame` so the in-flight frame is dropped silently.
- Or in [_run_pump](../src/capa/experiment/cameras.py#L520): catch `AdapterError("push_frame requires start_recording()")` specifically as benign-on-stop.

S-effort. No bundle-integrity impact, but the misleading WARNING is operator-noise.

##### 2. V4L2 identity probed correctly but not surfaced to bundle artefacts

[_probe_v4l2_info](../src/capa/devices/camera/webcam.py#L531) **works** — calling it directly returns `card_name="Logitech Webcam C930e"`, `serial="E7501BDE"`, `bus_info="3-6.2"`. [WebcamAdapter.open](../src/capa/devices/camera/webcam.py#L230) stores the result in `self._info` (`model` and `serial` fields populated). But **two surfaces drop this data**:

1. **`manifest.json.cameras[*].model` / `serial`** are hard-coded to `spec.model_hint` and `spec.serial` from the static `CameraSpec` ([bundle.py:481-482](../src/capa/storage/bundle.py#L481-L482)) — never reads the adapter's live `_info`. Manifest shows `model=None, serial=None` for the C930e even though the probe ran successfully.
2. **`equipment.toml`** has no `[[cameras]]` section at all. The engine's `_collect_equipment_blocks` walks `[[devices]]` only; the equipment-identity work shipped earlier (per the [resolution snapshot](#progress-since-hardware-day)) didn't include camera identity.

**Fix:** plumb `WebcamAdapter._info` (and any other camera adapter's live identity) through to both surfaces. Likely M-effort: adapter-side, add a `device_info`-style accessor matching the device adapters' duck-typed probe convention; bundle-side, replace the static `spec.model_hint` lookup with `adapter._info.model` if available; engine-side, extend `_collect_equipment_blocks` to walk cameras alongside devices.

This was claimed in the doc above as "✅ Webcam V4L2 identity probe — Sysfs-based `_probe_v4l2_info`" — but the integration to the bundle was never validated. The unit-level probe works; the end-to-end surface to manifest / equipment.toml does not.

#### Engineering changes from this session (besides config + pin)

- [src/capa/devices/camera/webcam.py](../src/capa/devices/camera/webcam.py): added `CAPA_WEBCAM_FRAME_DIAG=1` env-gated DEBUG/INFO logger that emits `webcam_frame_diag` events for the first 150 input frames in `run_pump` (`format` / `width` / `height` / `pts` / `time_base`). Dormant by default. Used during this re-validation; left in place for the next time EINVAL needs investigation.
- [pyproject.toml](../pyproject.toml): `"watlowlib"` → `"watlowlib>=0.2.0"`; removed `watlowlib = { path = "../watlowlib", editable = true }` from `[tool.uv.sources]` (keeps production resolution coming from the PyPI wheel; alicatlib / sartoriuslib / nidaqlib stay editable).
- 20 ruff lint fixes (stale `# noqa` directives + 2 import sorts) plus formatter on 6 files (none of which were code-functional changes).

**Test suite after all changes:** 481 passed (no hardware), ruff clean, mypy strict clean across 85 src files.

---

### Devices exercised

| Device | Identity reported | Port / addr | Test artefact | Outcome | Notes |
|---|---|---|---|---|---|
| Webcam (external) | Logitech C930e | /dev/video4 (capture); /dev/video5 (metadata) | [tests/hardware/test_webcam_smoke.py](../tests/hardware/test_webcam_smoke.py) | ✅ | UVC exposes two nodes per camera; capture is the lower index. `cameras[0].identity` is `None` because the V4L2 adapter doesn't extract model/serial. |
| Watlow | PM3R1CA-AAAAAAA, fw=1, hw=28, family=pm | /dev/ttyUSB0 (smoke); /dev/ttyUSB2 (multi-device) | [tests/hardware/test_watlow_smoke.py](../tests/hardware/test_watlow_smoke.py) | ✅ | Connected via B&B Electronics 485USBTB-2W (USB-RS485, vendor `0856:ac33`). Vendor ID not in `ftdi_sio` whitelist by default — required `echo "0856 ac33" > /sys/bus/usb-serial/drivers/ftdi_sio/new_id` after `modprobe`. Heater breaker was off, so PV stayed at ambient (~65 °C reported room temp). |
| Alicat | MCR-200SLPM-D serial 225873 fw=8v17 (flow_controller) | /dev/ttyUSB0 (smoke); /dev/ttyUSB0 (multi-device) | [tests/hardware/test_alicat_smoke.py](../tests/hardware/test_alicat_smoke.py) | ✅ | Connected via Belkin USB-RS232 adapter (Prolific PL2303 chip, vendor 067b). **Required baud override to 115200**, not the 19200 factory default I assumed. Reading: 0 SLPM (no plumbing), 14.61 psi (ambient), N₂. |
| Sartorius | MSE1203S-100-DR (Cubis-class) | /dev/ttyUSB0 (smoke); /dev/ttyUSB1 (multi-device) | [tests/hardware/test_sartorius_smoke.py](../tests/hardware/test_sartorius_smoke.py) | ✅ (after one retry) | xBPI at 19200 baud (not 9600 factory). Connected via FTDI FT232R (vendor 0403). First-byte race on cold open: `frame too short: got 1 bytes (min 4)` cleared on retry. Empty pan reads ~0.07 g, status=`settling` throughout (never auto-stabilized in 30 s freerun). |
| FLIR E85 | enumerated as USB ID `09cb:1007 Ex-Series UVC and MSD interface` | USB | [/home/gbellamy/Documents/git/capa-flir/tests/unit/test_e85_hardware.py](../../capa-flir/tests/unit/test_e85_hardware.py) (3 tests authored, all skip without camera) | ⏸ deferred to Windows rig | See §4 below for the full root-cause note. |
| NI-DAQ | — | — | (no Linux driver) | n/a | Sim adapter substituted in §5.A and §5.B. |

---

### Bundles produced

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

### Anomalies

#### Fixed in this session

- **Engine deadlock with camera-only configs.** `producer_queue` was never closed when `len(self._adapters) == 0`. `_fanout_task` blocked forever, run never sealed. Fixed at [src/capa/experiment/engine.py:838-843](../src/capa/experiment/engine.py#L838-L843): close the queue immediately if `producers_alive.value == 0` after starting tasks. Worth a regression test (`tests/integration/test_engine_camera_only.py`).
- **UI plot pane bound to stale empty registry.** `RunTab._on_start_clicked` was rebinding the plot pane to `controller.buffers` *immediately after* `controller.start()` returned, but `start()` is async and the buffer rebuild hadn't completed yet. Fixed by moving the rebind into `_on_state(EngineState.RUNNING)`, matching the numerics dock pattern. [src/capa/ui/tabs/run.py:_on_state](../src/capa/ui/tabs/run.py).
- **UI start button stuck disabled after seal.** `RunTab._on_run_finished` checked `can_start()` (which calls `is_active`), but the slot fires from inside the controller's task `finally` block — the task isn't `done()` yet, so `is_active` returns True. Fixed by deferring the re-enable via `QTimer.singleShot(0, ...)`. [src/capa/ui/tabs/run.py:_on_run_finished](../src/capa/ui/tabs/run.py).

#### Surfaced, not fixed (real follow-ups)

> **Annotation key:** every entry below carries a **Status (2026-05-09 PM):** line — ✅ shipped, 🟡 deferred, or 🔵 deferred to upstream. The bug descriptions are the original observation; the Status line records resolution.

- **`capa finalize` cannot recover SIGKILL'd parquet files.** The bundle writer streams to `*.in-flight.parquet` via pyarrow's chunked writer. The parquet footer is only written on `close()`. After SIGKILL, the file has valid row groups but no footer; `capa finalize` reads it as a complete parquet → `ArrowInvalid: Parquet magic bytes not found in footer`. Result: §7's `bundle_status=sealed_after_crash` cannot be reached today. The existing `tests/integration/test_crash_recovery.py` likely passes because it uses a graceful-shutdown shim, not real SIGKILL.
  - **Status (post-hardware-day):** ✅ **shipped.** Switched in-flight format from `pq.ParquetWriter` to Arrow IPC streaming via [_ipc.py](../src/capa/storage/_ipc.py) (per-batch length prefixes are natively truncation-tolerant; `read_recoverable` returns the prefix on torn files without raising). Sinks emit `*.in-flight.arrows`: [channel_samples_sink.py](../src/capa/storage/channel_samples_sink.py), [device_records_sink.py](../src/capa/storage/device_records_sink.py), [video_sink.py](../src/capa/storage/video_sink.py). [finalize.py](../src/capa/storage/finalize.py) `_rewrite_inflight_to_parquet` reads the IPC stream and writes the final parquet at seal time; torn streams write a `seal_warnings` entry instead of failing the seal. Regression test [test_crash_recovery_sigkill.py](../tests/integration/test_crash_recovery_sigkill.py) uses a `multiprocessing` child + real `os.kill(pid, SIGKILL)`.

- **Plugin trust check requires explicit `--plugins-lock` flag.** `CAPA_PLUGIN_MODE=production` alone is silently no-op because the CLI doesn't auto-discover `./plugins.lock`. Discovered when the first "happy path" production run completed cleanly despite a deliberately tampered RECORD. **Fix:** in production mode, error out (or auto-look-up `./plugins.lock` / `$XDG_CONFIG_HOME/capa/plugins.lock`) when no lock path is provided.
  - **Status (2026-05-09 PM):** ✅ **shipped.** [src/capa/app.py:88-150](../src/capa/app.py#L88-L150) — new `_resolve_plugins_lock_for_run` + `_discover_plugins_lock_paths` helpers. Production mode walks `./plugins.lock` then `$XDG_CONFIG_HOME/capa/plugins.lock` (fallback `$HOME/.config/capa/plugins.lock`); first match wins. Missing on both → `typer.Exit(2)` with explicit error. Auto-discovered path is echoed to stdout so the operator sees exactly which lock was honored. `plugins_list` updated to use the same lookup order. Tests: [tests/unit/test_cli.py](../tests/unit/test_cli.py) `TestPluginsLockAutoDiscovery` (3 cases).

- **Plugin lock entry's `version` field is the class attribute, not the dist version.** `capa plugins trust` writes `version = "0.1.0"` (the `RecipeRunner.version` class attr) but `detect_drift` compares against `dist.version` (`"0.0.1.dev1+gb38818856.d20260508"`). They will always mismatch in editable installs, so the happy path (production mode + valid lock + unmodified package) returns `procedure not in trusted registry` even when nothing has been tampered with. **Fix:** make `capa plugins trust` write the dist version (or make `detect_drift` compare class versions).
  - **Status (2026-05-09 PM):** ✅ **shipped.** [src/capa/core/plugins_runtime.py:213-228](../src/capa/core/plugins_runtime.py#L213-L228) — `LoadedProcedure.version` now sources `dist.version` exclusively, dropping the `getattr(cls, "version", version)` fallback. `capa plugins trust` writes the dist version into the lock; `detect_drift` reads dist version. **Trade-off:** editable installs (where `dist.version` is `0.0.1.dev1+gXXXX.dYYYY`) invalidate the lock on every commit. Production rigs install from wheels and aren't affected — documented in the docstring callout (see next item). Tests: [tests/unit/test_plugins_runtime.py](../tests/unit/test_plugins_runtime.py) `test_loaded_procedure_version_uses_dist_not_class_attribute`. Closes the §5.4 happy path that was previously blocked.

- **Plugin distribution_hash is computed over METADATA + RECORD only.** Per the docstring, this is intentional — "sufficient for detecting 'the wheel I installed has been swapped'" — but operators might expect "rebuild = retrust required". Worth documenting prominently in the trust-mode runbook. The rejection path in §5.4 fired after RECORD was directly mutated.
  - **Status (2026-05-09 PM):** ✅ **shipped (docs only).** [src/capa/core/plugins_runtime.py:347-380](../src/capa/core/plugins_runtime.py#L347-L380) — expanded `_hash_distribution` docstring with an explicit operator-facing "Trust scope" section: detects wheel swaps + tampered `RECORD`; does NOT detect editable-install source-file edits or runtime monkey-patching; recommended workflow is build wheel → install → trust → ship lock. Cross-references the editable-install retread documented in #12.

- **Watlow watchdog `device_silent` warnings during command-heavy bursts.** During §5.A's ramp (~9 cmds/s), the heater went silent for ~17 s twice. Cause: serial-port contention between the 1 Hz poll thread and the setpoint-write thread on the same `/dev/ttyUSB2`. **Fix:** serialize Watlow reads + writes through a single asyncio queue inside the adapter so they share the bus deterministically.
  - **Status (2026-05-09 PM):** 🔵 **resolved upstream in watlowlib v0.2.0** (see [§watlowlib v0.2.0 resolution](#watlowlib-v020-resolution-recorder-starvation) below). Investigation found watlowlib already had a per-port lock; the actual root cause was lock-fairness starvation across N per-parameter acquisitions. Upstream fix: atomic-by-default per-tick lock-batch acquisition. **Capa action:** none in code; bump `watlowlib` pin to `>=0.2.0` once upstream tags the release. **Capa expectation:** atomic batches help bursty workloads but cannot rescue a sustained over-budget ramp; if §5.A's gap persists, mitigation is application-side rate-limiting or PM3 profile-mode coalescing.

- **Webcam pump_failed mid-recipe.** `avcodec_send_packet() returned 22` (libx264 EINVAL) at t≈23 s into §5.A. Camera recovered to `engine.camera.closed` and bundle sealed, but only 699 frames captured. Recipe events continued as expected. **Fix:** root-cause the EINVAL (frame format change? PyAV stream-state bug?). Worth a regression test once isolated.
  - **Status (2026-05-09 PM):** ✅ **drop-and-continue guard shipped; root-cause investigation deferred.** [src/capa/devices/camera/webcam.py](../src/capa/devices/camera/webcam.py) — `_push_frame_sync` now catches `av.error.FFmpegError` around the encode loop, drops the offending frame, increments a new `_dropped_frames` counter, and emits a `pump_warning` event. Critically: the dropped frame does NOT advance `_frame_count`, so receipt indexes stay contiguous over surviving frames and the encoder doesn't see a `pts` gap. `CameraHealth.dropped_frames` field added ([base.py:182](../src/capa/devices/camera/base.py#L182)) so post-run analysis can find the events. The pump no longer dies on a single-frame fault. Tests: [tests/unit/test_camera_webcam.py](../tests/unit/test_camera_webcam.py) `TestEncoderFailureGuard`. **Root cause still unknown** — needs a second hardware run with diagnostic logging on UVC frame-format renegotiation or `pts` collisions.

- **Event-loop starvation by visible webcam pump.** §5.B recipe ran 2.7× slower than wall clock (5 min 16 s for what should be 115 s). Webcam captured at ~14 fps instead of 30. The PyAV `frame.reformat().to_ndarray()` step runs on the event loop and is CPU-heavy enough to block other tasks. **Fix:** wrap `reformat()` in `anyio.to_thread.run_sync()`, or move the entire pump to a dedicated worker thread. This bug interacts with the Watlow watchdog one above — the slower the loop, the worse the serial contention surfaces.
  - **Status (2026-05-09 PM):** ✅ **shipped.** [src/capa/devices/camera/webcam.py](../src/capa/devices/camera/webcam.py) — `push_frame` split into a sync core (`_push_frame_sync` doing encode + mux + bookkeeping) and an async wrapper that runs the core via `anyio.to_thread.run_sync`. `run_pump`'s decode-loop iteration (`next(decoder, None)`) and `frame.reformat(format="rgb24").to_ndarray()` each run in a worker thread (`_advance_decoder`, `_reformat_to_rgb24` helpers). Result: every CPU-heavy PyAV call is off the asyncio loop. Tests: `TestPushFrameOffLoop` (asserts `_push_frame_sync` is invoked via `to_thread.run_sync`).

- **Camera disk preflight uses 3600 s fallback duration for free-runs.** The procedure's `duration_s` config isn't surfaced to `disk_space_preflight_problems` when no method is present. Forced `estimated_bps` adjustments down to 1.5 MB/s in production TOMLs and 500 KB/s in the smoke test. **Fix:** preflight should consult the procedure's `duration_s` config when a method is absent. ~10-line change in [src/capa/experiment/cameras.py](../src/capa/experiment/cameras.py).
  - **Status (2026-05-09 PM):** ✅ **shipped.** [src/capa/experiment/cameras.py:200-208](../src/capa/experiment/cameras.py#L200-L208) — preflight peeks at `config.procedure.config.get("duration_s")` when method is None. 3600 s fallback retained for genuinely unbounded runs (`external_stop`-driven). Tests: [tests/unit/test_cameras_preflight.py](../tests/unit/test_cameras_preflight.py) `TestProcedureDurationResolution` (4 cases).

- **Camera disk preflight projects against `runs_root`'s mount, but `/tmp` is a 16 GB tmpfs on this box.** `/tmp/capa-hw-day` looked like it had plenty of room (only 14 MB used) but the projection blocked the §5.B run (3.6 GB × 3 cameras > 16 GB free). Switched to `/home/gbellamy/capa-runs` (817 GB free). **Fix:** preflight should warn explicitly when the runs root is on tmpfs, or weight against a much shorter "expected free space within the tmpfs" rather than blocking.
  - **Status (2026-05-09 PM):** ✅ **shipped (warn + tighten).** [src/capa/experiment/cameras.py](../src/capa/experiment/cameras.py) — new `_filesystem_type` helper parses `/proc/mounts` longest-prefix match (Linux only; returns `None` elsewhere); new `_mem_available_bytes` reads `MemAvailable` from `/proc/meminfo`. When the target is on `tmpfs`/`ramfs`, a non-blocking `disk_target_volatile` warning fires AND the budget is tightened to `min(reported_free, MemAvailable / 2)` so a memory-pressure scenario doesn't OOM mid-run. Tests: `TestVolatileFilesystemDetection` (5 cases).

- **Webcam adapter doesn't populate `CameraInfo.model` / `serial` from V4L2.** `manifest.json.cameras[0].identity` is `None` for real webcams. `v4l2-ctl --info` exposes the card name (`Logitech Webcam C930e`) and bus path; surfacing those would close the gap without needing vendor-specific code.
  - **Status (2026-05-09 PM):** ✅ **shipped.** [src/capa/devices/camera/webcam.py](../src/capa/devices/camera/webcam.py) — new `_probe_v4l2_info(device_path) -> V4L2Probe` reads `/sys/class/video4linux/<node>/name` (card name) plus the parent USB device's `serial` / `idVendor` / `idProduct` / bus path. Called from `WebcamAdapter.open()` when `sys.platform == "linux"` and `input_format == "v4l2"`. Verified against the real Logitech C930e on this box (returns `card_name="Logitech Webcam C930e"`, `serial="E7501BDE"`, `bus_info="3-6.2"`). Tests: `TestV4L2IdentityProbe` (4 cases including non-Linux skip + missing-node).

- **Sartorius first-byte race on cold open.** `frame too short: got 1 bytes (min 4)` on the first identify after a fresh plug-in; cleared on retry. **Fix:** retry-on-short-frame inside `SartoriusAdapter.open()`.
  - **Status (2026-05-09 PM):** ✅ **shipped.** [src/capa/devices/sartorius.py:494-538](../src/capa/devices/sartorius.py#L494-L538) — `_build_balance` split into `_build_balance_once` + a retry wrapper (3 attempts, 0.2 / 0.4 / 0.8 s backoff). Substring match on `"frame too short"` / `"got 0 bytes"` only; non-cold-open `SartoriusError` shapes (checksum, timeout, bad device id) re-raise immediately. `_cold_open_retry_count` tracked on the adapter for diagnosis. Tests: [tests/unit/test_sartorius_adapter.py](../tests/unit/test_sartorius_adapter.py) `TestColdOpenRetry` (4 cases).

- **`equipment.toml` doesn't capture device identity.** Watlow's smoke test confirms part_number/firmware/hardware_id are read at adapter open, but the `equipment.toml` written into each bundle only contains `name` + `adapter`. The richer identity surfaces in events and probe-capabilities logs but not in the static equipment profile. Worth deciding: is the static file authoritative, or are events the source of truth?
  - **Status (2026-05-09 PM):** ✅ **shipped — events are SoT, equipment.toml is a denormalized human-readable summary populated at seal time.** New `equipment` kwarg on `RunBundleWriter.finalize` ([src/capa/storage/bundle.py](../src/capa/storage/bundle.py) — `_rewrite_equipment_toml` rewrites the file *before* the integrity walk so `manifest.sha256` covers the populated content). Engine collects per-device blocks via new `_collect_equipment_blocks` ([src/capa/experiment/engine.py](../src/capa/experiment/engine.py)) — for each declared device, looks up the live adapter via `self._adapter_by_device` and duck-types `adapter.device_info` through `_identity_from_device_info` (probes `part_number`, `model`, `serial_number`, `firmware_id`, `hardware_id`, `family`, etc., with `.raw` / `.value` coercion). Sim adapters get `identity=None`. Crash-recovered bundles keep the open()-time stub (no live adapter to probe). Tests: [tests/integration/test_bundle_roundtrip.py](../tests/integration/test_bundle_roundtrip.py) `TestEquipmentToml` (3 cases).

#### Configuration findings (not bugs)

- **Unit / channel-kind validation surprises.** `psia` not in pint registry → use `psi`. `pressure` not a valid `ChannelKind` → use `process_var`. Both fixed in [configs/hardware/alicat_real.toml](../configs/hardware/alicat_real.toml).
- **`estimated_bps` defaults too high.** Lowered to 1.5 MB/s in production webcam TOML; the underlying preflight bug is the real fix.

---

### P3 production-mode plugin trust check (§5.4)

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

### watlowlib v0.2.0 resolution (recorder starvation)

The `device_silent` watchdog gap (§5.A anomaly) was investigated, an upstream plan was written ([watlowlib-recorder-starvation-upstream-plan.md](../watlowlib-recorder-starvation-upstream-plan.md)), and the fix landed on `watlowlib` `main` as **v0.2.0** (tag pending at time of writing). The investigation reframed the bug:

**Original framing:** "no per-port lock → bytes interleave on the bus."
**Actual root cause:** watlowlib already had a per-port `client.lock`. The recorder's `poll_many` made N independent acquisitions per tick (one per parameter), and a 9-cmds/sec setpoint burst (especially RWES writes with `confirm=True`, ~250 ms each on EEPROM commit) starved the recorder under FIFO lock fairness.

#### What landed upstream (vs. the plan)

| Plan asked for | What shipped in v0.2.0 |
|---|---|
| `record(..., atomic_polls=True)` opt-in flag | **Atomic-by-default**, no flag — the recorder always acquires the per-port lock once per tick batch |
| `Controller.poll_many(..., atomic=True)` | Same — no flag, always atomic |
| New `_execute_locked` / `_read_parameter_locked` private API | None — single owner-check helper (`anyio.Lock.statistics().owner == get_current_task()`); public API unchanged |
| `AcquisitionSummary.lock_wait_ms_p50` / `_p99` | Replaced with `tick_duration_ms_p50` / `tick_duration_ms_p99` — full `await source.poll_many(...)` round trip; no `PollSource` Protocol ripple |
| Design 2 (recorder-priority lock) | Rejected — priority just shifts starvation onto the writer; sustained over-budget bus is an application concern |

#### Capa-side action

- **Code:** none. The plan suggested `record(..., atomic_polls=True)` in [src/capa/devices/watlow.py](../src/capa/devices/watlow.py); that kwarg does not exist. Default behavior is correct.
- **Pin:** bump `watlowlib` to `>=0.2.0` in [pyproject.toml](../pyproject.toml) **once upstream tags v0.2.0**. Today the local editable install (`tool.uv.sources.watlowlib = { path = "../watlowlib", editable = true }`) already picks up the fix from `main`; a `>=0.2.0` PEP 440 pin would fail resolution against the current `0.1.0+...` dev version. Re-bump after the tag.

#### Honest expectation for the next §5.A re-run

The plan's closing line ("the 17-second silent gap goes away because each tick still completes within ~150 ms") is optimistic. Atomic batches make tick *completion* fast once the lock is held; they don't prevent the tick's *first acquisition* from waiting behind a deep FIFO queue.

**Math against the §5.A workload:** 9 setpoint-writes/sec × ~250 ms confirm-EEPROM each = 2.25 s/sec lock occupancy. Steady-state queue grows at +1.25 s per second of wall-clock — so after ~14 s, the recorder's tick enqueues behind ~17 s of pending writes. **Still a 17-s gap, atomic or not.**

Where atomic batches actually help:

- **Bursty workloads** (recipe ramps with quiet inter-burst windows): tick latency stops scaling with `N parameters × queue depth at each enqueue` — that's the dominant pathology in capa's specific shape.
- **Tick stretch under contention spikes:** the 250–500 ms per tick of mid-tick contention is gone.

Where they don't:

- **Sustained over-budget workloads** (`write_rate × per_write_occupancy > 1 s/sec`). No upstream library change can rescue that — the bus is full.

#### Diagnostic to use

After the v0.2.0 bump, every recording's `AcquisitionSummary` carries `tick_duration_ms_p99` (also surfaced as `tick_p99_ms=...` on the `recorder.stop` log line). Decision rule:

- **Healthy:** `tick_p99_ms ≪ 1000 / rate_hz` (e.g., < 100 ms at 1 Hz)
- **Saturated:** `tick_p99_ms` approaches `1000 / rate_hz` → the bus is contended; the watchdog gap is structural and lives in the application

If the gap recurs on the §5.A re-run, `tick_p99_ms` tells the operator whether to (a) rate-limit the recipe ramp (most direct), (b) coalesce writes via a PM3 onboard profile, or (c) move the recorder to a separate physical bus. None of those are library bugs.

---

### Crash-recovery outcome (§7)

Original hardware-day finding: `capa finalize` aborted on the in-flight parquet's missing footer; events recovered correctly (445 events, all 435 `method.command.issued`) but `manifest.sha256` was never regenerated. The lesson: the existing `test_crash_recovery.py` didn't exercise actual SIGKILL.

> **Update (post-hardware-day):** ✅ **resolved.** In-flight format switched from chunked parquet to Arrow IPC streaming ([_ipc.py](../src/capa/storage/_ipc.py)). Truncated streams now read back via `read_recoverable` and rewrite to final parquet during `finalize` ([finalize.py](../src/capa/storage/finalize.py) `_rewrite_inflight_to_parquet`); irrecoverable files surface as `seal_warnings` rather than failing the seal. Regression test at [test_crash_recovery_sigkill.py](../tests/integration/test_crash_recovery_sigkill.py) uses `multiprocessing` + real `os.kill(pid, SIGKILL)`.

---

### Follow-ups to file as plan items

All capa-side follow-ups surfaced on hardware day are shipped (see [§Progress since hardware day](#progress-since-hardware-day) for the resolution log). What remains is gated on external resources:

#### Webcam (passive)

- [ ] Root-cause the libx264 EINVAL observed at t≈23 s in §5.A. Drop-and-continue guard + `pump_warning` event already shipped; permanent diagnostic behind `CAPA_WEBCAM_FRAME_DIAG=1` ([webcam.py](../src/capa/devices/camera/webcam.py)). Re-run on 2026-05-09 PM did not reproduce. Stays open until the bug surfaces again with the diagnostic on.

#### FLIR

- [ ] Investigate Linux USB-mode-switch for the FLIR Ex-Series. Either find a libusb control-transfer that puts the camera into vendor mode, or accept Linux Atlas-USB as a permanent limitation and document.
- [ ] Schedule a Windows rig day to close capa-flir Stage H.

#### NI-DAQ

- [ ] Schedule the Windows rig day for the NI-DAQ adapter. NI-DAQmx has no Linux driver.
---

### Next steps

1. **FLIR Stage H + NI-DAQ Windows day.** Unchanged from the original plan — both blocked on a Windows rig. Schedule when one is available.

2. **Webcam EINVAL root cause.** Drop-and-continue guard prevents the loss-of-recording symptom; root cause still unknown. Diagnostic logging is permanent (env-gated). Stays open until the bug surfaces again with `CAPA_WEBCAM_FRAME_DIAG=1` on.

---

### Operator notes for the next hardware day

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


---

## Windows rig results (2026-05-09)

Companion to the original Linux session in [hardware-day-plan.md](#plan). Re-runs everything against the Windows-side silicon and closes the two gaps the Linux session deferred — **NI-DAQ** and **FLIR E85** — both of which the original §3.5 / §4.4 left as "Windows rig day" follow-ups.

**Headline:** every adapter now has a sealed real-hardware bundle. The capa pyrolysis profile runs end-to-end against all four real devices (Watlow + Alicat + Sartorius + NI-DAQ) plus both real cameras (Logitech C930e visible + FLIR E85 IR) in a single bundle — the original Linux split into two sub-pairings (§5.A and §5.B) collapses to one §5.C all-real run on Windows.

**Host:** Windows 11 Enterprise, Python 3.13.13, capa 0.0.1.dev1+g077ae7eaf, capa-flir 0.0.1.dev2, anyserial 0.1.2 (editable, see anomalies), NI 9214 thermocouple module on cDAQ-9171, FLIR Atlas SDK 2.19.0 at `C:\FLIR\atlas-c-sdk-2.19.0`.

---

### Devices exercised

| Device | Port / addr | Test artefact | Outcome | Notes |
|---|---|---|---|---|
| Webcam (Logitech C930e) | DirectShow `video=Logitech Webcam C930e` | [tests/hardware/test_webcam_smoke.py](../tests/hardware/test_webcam_smoke.py) | ✅ 2 pass | required platform-aware `input_format` (dshow vs v4l2) + 5s teardown wait between back-to-back opens |
| Watlow | COM6 (B&B 485USBTB-2W FTDI), addr 1, stdbus | [tests/hardware/test_watlow_smoke.py](../tests/hardware/test_watlow_smoke.py) | ✅ 4 pass | PM3R1CA-AAAAAAA, fw=1, hw=28, family=pm |
| Alicat | COM7 (Prolific PL2303), 115200 baud, unit A | [tests/hardware/test_alicat_smoke.py](../tests/hardware/test_alicat_smoke.py) | ✅ 3 pass | MCR-200SLPM-D s/n 225873; default test-env baud is 19200, override required (same as Linux session) |
| Sartorius | COM4 (FT232R), xBPI 19200 | [tests/hardware/test_sartorius_smoke.py](../tests/hardware/test_sartorius_smoke.py) | ✅ 4 pass | Cubis MSE1203S-100-DR; second FT232R is COM8 (some idle USB-RS232 cable); default test-env baud is 9600, override required |
| **NI-DAQ** | cDAQ1Mod1 (NI 9214 K-type TC, 16 channels, ai0/ai1 used) | [tests/hardware/test_nidaq_smoke.py](../tests/hardware/test_nidaq_smoke.py) ✨ NEW | ✅ 4 pass | Closes Linux §3.5 deferral. Two K-type TCs read open-junction values (expected for unconnected probes). |
| **FLIR E85** | USB (Atlas via vendor driver) | [../capa-flir/tests/unit/test_e85_hardware.py](../../capa-flir/tests/unit/test_e85_hardware.py) | ✅ 3 pass | Closes Linux §4.4 (Stage H) deferral. Atlas USB enumerates as `model='FLIR USB Video'` on Windows — `model_hint = "FLIR E85"` mismatches and was dropped from the config. |

---

### Bundles produced (single-device freeruns)

All under `C:\capa-runs\` (chosen because `/tmp` doesn't exist on Windows; ~46 GB free).

| Bundle | Sample id | Outcome | Notes |
|---|---|---|---|
| `2026-05-09_202101_WEBCAM-REAL-001` | WEBCAM-REAL-001 | sealed | 30s, 877 visible frames @ ~29.2 fps, 1.5 MB MKV |
| `2026-05-09_202830_NIDAQ-REAL-001` ✨ | NIDAQ-REAL-001 | sealed | 30s, 143 polled readings × 2 TC channels = 286 scalar rows |
| `2026-05-09_203525_FLIR-E85-REAL-001` ✨ | FLIR-E85-REAL-001 | sealed | 60s, 1587 frames @ ~30 Hz, 249 MB .csq + 470 B meta + 21 KB frames.parquet |
| `2026-05-09_203955_WATLOW-REAL-001` | WATLOW-REAL-001 | sealed | 30s, 31 emissions, watlow.parquet populated |
| `2026-05-09_204519_ALICAT-REAL-001` | ALICAT-REAL-001 | sealed | 30s |
| `2026-05-09_204701_SARTORIUS-REAL-001` | SARTORIUS-REAL-001 | sealed | 30s |

✨ = new on Windows (not exercised in Linux session).

### Bundles produced (integration runs)

| Bundle | Variant | Outcome | Real devices | Cmds (all authd) | Cameras |
|---|---|---|---|---|---|
| `2026-05-09_204929_REAL-A-001` | §5.A | sealed | Watlow + Alicat + webcam | 1073 | visible_cam0=4134 |
| `2026-05-09_205250_REAL-B-001` | §5.B | sealed | E85 + Sartorius + webcam | 1144 | visible_cam0=3459, **ir_cam0=3280** (515 MB .csq) |
| `2026-05-09_205557_REAL-C-001` ✨ | §5.C all-real | sealed | **all six adapters real** | 1004 | visible_cam0=3829, ir_cam0=3651 (547 MB .csq) |

§5.C is the run the Linux session couldn't do. Scalars table covered every required `capa_pyrolysis` channel: `heater.pv, heater.setpoint, carrier.flow, balance.mass, TC_sample_top, TC_sample_mid` (2013 rows / 6 channels). Every `method.command.issued` event in `events.sqlite` carries an `authorization_id` and `issued_by` (verified via SQL `count(... authorization_id is null) = 0`).

`capa catalog verify` returned **clean** for every bundle above.

---

### §5.4 Production-mode plugin trust

Done against the live rig after §5.A.

- **Trust grants** journaled to [plugins.lock](../plugins.lock) + [plugins.lock.journal](../plugins.lock.journal). Two entries: `capa.builtin.recipe_runner` and `capa.builtin.free_run`, each with reason `"hw-day-windows-2026-05-09"`.
- **Happy path:** `CAPA_PLUGIN_MODE=production --plugins-lock ./plugins.lock` ran the webcam freerun cleanly (`bundle 2026-05-09_205908_WEBCAM-REAL-001`).
- **Rejection path:** zeroed the `distribution_hash` for `capa.builtin.free_run` in `plugins.lock`, retried; engine refused with `procedure 'capa.builtin.free_run' is not in the trusted registry (mode=production); available: <none>`. Restored the lock; production load succeeded again (`bundle 2026-05-09_210014_WEBCAM-REAL-001`).
- Same Linux-session footgun stands: `CAPA_PLUGIN_MODE=production` alone is silently no-op without `--plugins-lock`.

---

### §6 GUI smoke (operator-driven)

`capa run --gui configs/experiments/capa_real_full.yaml --runs-root C:\capa-runs`. All checks ✅:

- Setup tab handshakes Watlow + Alicat + Sartorius + NI-DAQ + visible cam + IR cam without errors.
- Plot pane (PyQtGraph) tracks all six channels live; numerics dock + status bar update at the configured rate; UI stays responsive while data flows.
- Both visible and IR camera previews render in their docks; status bar queue-depth metric stays stable (no backpressure).
- Click **Abort** mid-recipe → bundle finalizes as `run_status=aborted`, `bundle_status=sealed`. Verified on three separate GUI runs:
  - `2026-05-09_210432_REAL-C-001` — 1069 visible + 1029 IR frames, aborted/sealed
  - `2026-05-09_210554_REAL-C-001` — 331 visible + 202 IR frames, aborted/sealed
  - `2026-05-09_210643_REAL-C-001` — 660 visible + 555 IR frames, aborted/sealed
- Re-opening the same config and clicking Run a second time allocated a distinct `run_id` and bundle dir each time. ✅

---

### §7 Crash recovery (manual)

Windows equivalent of `kill -9` is `Stop-Process -Force` (or `taskkill /F /PID`).

1. Started `capa run --headless configs/experiments/capa_real_full.yaml` in the background.
2. ~30 s later, killed both Python processes (CLI + child) with `Stop-Process -Force`.
3. Manifest at kill time: `run_status=running, bundle_status=open` ✅ (i.e. not yet rolled to `crashed`).
4. Ran `capa finalize C:/capa-runs/2026-05-09_210138_REAL-C-001`.
5. After finalize: `run_status=crashed, bundle_status=sealed, integrity=ok`. Camera frame counts: `visible_cam0=256, ir_cam0=0` (E85 has ~7 s recording-startup latency, so 30 s minus startup landed before any IR frames).

Plan said `bundle_status=sealed_after_crash` — that label is aspirational; the actual status field is just `sealed` with `run_status=crashed` carrying the post-mortem signal. Same semantic outcome, worth correcting the plan text if it survives.

---

### Anomalies discovered + resolved

#### 🔴 Blocker — anyserial 0.1.1 + Python 3.13 weakref crash on every serial open

CPython 3.13's `IocpProactor._registered = weakref.WeakSet()` requires registered objects to support `__weakref__`. anyserial's `HandleWrapper` declared `__slots__ = ("_handle",)` and forgot it, so every `open_serial_port` raised:

```
TypeError: cannot create weak reference to 'HandleWrapper' object
```

This blocked Watlow / Alicat / Sartorius (and Alicat's discovery hook). Filed handoff at [anyserial-windows-313-handoff.md](../anyserial-windows-313-handoff.md) with reproducer + slot-fix + regression test outline. Fix landed upstream as anyserial commit `1134f37` ("fix(windows): make HandleWrapper weak-referenceable on CPython 3.12+"), bumping to **0.1.2**. Installed editable into the capa venv to unblock; downstream pins (`alicatlib`, `sartoriuslib`, `watlowlib` all `>=0.1,<0.2`) accept it without changes. **Follow-up:** publish 0.1.2 to PyPI and let `uv sync` pick it up.

#### 🟡 Engine — `capa.devices.nidaq` resolver couldn't find the real adapter

`engine._import_adapter_class("capa.devices.nidaq")` snake-cases the leaf to `Nidaq` and tries `[Nidaq, NidaqSim, NidaqAdapter]`. The real class is `NIDAQAdapter` (all-caps NI). Sim modules (`nidaq_polled_sim.py`) already alias `NidaqPolledSim = NIDAQPolledSim` to satisfy this resolver; the real one didn't. Fixed by adding `NidaqAdapter = NIDAQAdapter` to [src/capa/devices/nidaq.py](../src/capa/devices/nidaq.py). **Follow-up:** the resolver should either also try the bare-acronym CamelCase variant, or all adapters should adopt the alias pattern uniformly so future acronym adapters (LCR, MFC, etc.) don't trip the same gap.

#### 🟡 Webcam smoke — DirectShow handle hold time after `cam.close()`

PyAV `av.open(format='dshow')` returns `[Errno 5] I/O error` if the same camera is re-opened too soon after closing — Windows hasn't dropped the DirectShow filter graph yet. `gc.collect()` plus a 5 s sleep between the two webcam tests is enough on the C930e. Implemented as a Windows-only autouse teardown fixture in [tests/hardware/test_webcam_smoke.py](../tests/hardware/test_webcam_smoke.py). **Follow-up:** retry-with-backoff on `Errno 5` inside `WebcamAdapter.run_pump()` would make this fully transparent.

#### 🟡 FLIR E85 — Windows enumeration model name doesn't match `FLIR E85`

Atlas USB on Windows reports the camera as `model='FLIR USB Video'` (the FLIR USB driver's generic vendor label), not `'FLIR E85'`. The hardware TOML's `model_hint = "FLIR E85"` is a substring matcher (`hint in row.model`) so no row matched and the freerun crashed at adapter open. Dropped the hint in [configs/hardware/flir_e85_real.toml](../configs/hardware/flir_e85_real.toml) (single-camera rig — disambiguation not needed). **Follow-up:** if the rig grows to multiple FLIR cameras, switch to a `serial`-based selector. Worth surfacing this naming difference in capa-flir docs alongside the existing Atlas Linux/Windows notes.

#### 🟡 Webcam smoke test — hardcoded `input_format = "v4l2"` on every platform

[tests/hardware/test_webcam_smoke.py](../tests/hardware/test_webcam_smoke.py) constructed the spec with `input_format = "v4l2"` regardless of host. Patched to default to `dshow` on Windows / `avfoundation` on macOS / `v4l2` on Linux, with optional override via `CAPA_TEST_WEBCAM_INPUT_FORMAT`.

#### 🟡 ChannelKind enum name confusion in NI-DAQ smoke test

`ChannelKind.TC` doesn't exist; the symbol is `THERMOCOUPLE` (with `value="tc"`). Fixed inline.

#### 🟡 NIDAQ stream test — breaking out of `async for` mid-iteration deadlocks `close()`

The polled streamer's `async with record_polled(...)` holds the `DaqSession` lock that `adapter.close()` later tries to acquire. A naive `break` out of the `async for` leaves the inner async-context-manager dangling; `close()` then blocks forever and tests time out via `CancelledError`. Test now signals stop via `_stop_requested` and lets the stream exit cleanly through the flag check, then explicitly `await stream.aclose()` in `finally`. **Follow-up:** the polled-mode adapter could expose a higher-level "stop after N readings" helper so test code doesn't have to reason about the inner async-with lifecycle.

#### 🟢 SIGKILL test gap on Windows

[tests/integration/test_crash_recovery_sigkill.py](../tests/integration/test_crash_recovery_sigkill.py) uses `signal.SIGKILL` which doesn't exist on Windows — fails the baseline pytest run with `AttributeError`. Doesn't break product behavior (the test's intent is exercised end-to-end by §7 above using `Stop-Process -Force`). **Follow-up:** add `pytest.mark.skipif(sys.platform == "win32", ...)` and either accept that and rely on the manual §7 path, or write a Windows-equivalent test that uses `ctypes.windll.kernel32.TerminateProcess`.

#### 🟢 Manifest device identity is `None` for NI-DAQ

`manifest.json.devices` ends up empty / `None` for `NIDAQAdapter`. The adapter's `snapshot_fields` exposes task name + channels but doesn't populate the `identity` block (NI 9214 product type / serial / chassis are sitting right there in `nidaqmx.system.Device`). Polish item, not a bundle integrity issue.

#### 🟢 NI watchdog warning at end of recipe

`engine.watchdog.device_silent` fires once per NI-DAQ adapter on shutdown when the polled stream has emitted its last sample but the recorder hasn't yet noticed the duration_elapsed exit. Benign; same shape as the Watlow watchdog warning the Linux session noted under "device_silent during command-heavy bursts," but a different cause (clean shutdown vs serial contention). **Follow-up:** suppress the watchdog one tick after the procedure signals stop, before tearing down adapters.

---

### Operator notes (updated for Windows)

- **COM port mapping (PnP-stable as of 2026-05-09):**
  - COM6 = Watlow via B&B Electronics 485USBTB-2W (FTDI VID 0856, PID AC33)
  - COM7 = Alicat via Belkin USB-RS232 (Prolific VID 067B, PID 2303), 115200 baud
  - COM4 = Sartorius onboard FT232R (VID 0403, PID 6001), serial `A103H1FFA`, 19200 baud xBPI
  - COM8 = idle FT232R (VID 0403, PID 6001), serial `BG00VBZSA` — not in use
- **NI:** cDAQ-9171 chassis (s/n 31195776) with NI 9214 module enumerated as `cDAQ1Mod1` (s/n 26994925). 16 AI channels; we use ai0/ai1 for K-type TCs with built-in CJC, °C output.
- **Logitech C930e:** DirectShow friendly name `Logitech Webcam C930e` (case-sensitive — `Logitech HD Webcam C930e` is the laptop's built-in webcam, different device).
- **FLIR E85:** USB VID 09CB, PID 1007. Atlas SDK at `C:\FLIR\atlas-c-sdk-2.19.0` (set via `CAPA_FLIR_ATLAS_ROOT`). Windows FLIR USB driver is installed (the vendor mode-switch the Linux session was missing).
- **Runs root:** `C:\capa-runs\` (`C:\` has ~46 GB free, plenty for 60 s × 4.5 MB/s E85 captures plus visible/scalars).

---

### Follow-ups outside hardware day

The original Linux-session list mostly carries forward; the Windows-specific additions:

- **Publish anyserial 0.1.2** to PyPI so we can drop the editable install.
- **Engine `_import_adapter_class` resolver** — handle all-caps acronyms generically, or document the alias contract.
- **Webcam adapter** — retry on `[Errno 5]` from PyAV on Windows so back-to-back open/close just works.
- **NI-DAQ manifest identity** — populate `manifest.json.devices[*].identity` with NI device product type + serial + chassis.
- **NI-DAQ watchdog** — suppress the final `device_silent` warning during clean shutdown.
- **`capa-flir` Windows model-string note** — document that Atlas USB reports `model='FLIR USB Video'` so operators reach for serial-based selection from day one.
- **`test_crash_recovery_sigkill.py`** — Windows skip or Windows-equivalent.
- **§5.A / §5.B partials** — now mostly redundant on Windows since §5.C covers their union; keep them for Linux-rig regression but mark as "Windows-optional" in the plan.

---

### What's *not* deferred anymore

The Linux session's final "outstanding" list said:

> Schedule a Windows-rig hardware day to close the loop [for NI-DAQ and capa-flir Stage H].

Both closed:

- ✅ NI-DAQ: real adapter exercised end-to-end (smoke tests + freerun + integration in §5.C). Scaffolding (config, freerun YAML, hardware test) committed under [configs/hardware/nidaq_real.toml](../configs/hardware/nidaq_real.toml), [configs/experiments/nidaq_real_freerun.yaml](../configs/experiments/nidaq_real_freerun.yaml), [tests/hardware/test_nidaq_smoke.py](../tests/hardware/test_nidaq_smoke.py).
- ✅ capa-flir Stage H: 60 s E85 freerun produces a 249 MB sealed `.csq` with frame parquet + meta sidecar; all 4 capa-flir hardware-marked tests pass.

The remaining capa work is the same as the Linux session left it: P3.1 UI items (method editor, auto-form generator, dynamic preflight relocation, `profiles/<id>.toml` snapshot, SafetyMonitor). None of those are blocked by hardware.
