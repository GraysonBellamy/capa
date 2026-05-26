---
description: Running capa `tests/hardware/` smoke suite on real instruments — `CAPA_HARDWARE_TESTS=1` opt-in, per-vendor `CAPA_TEST_*` vars, non-destructive writes.
---

# Hardware tests

**Audience:** contributors with a real rig — or any subset of one (a single Watlow on a USB-serial adapter is enough to run one of the smoke tests).
**Scope:** the `hardware` marker, the `CAPA_HARDWARE_TESTS=1` opt-in, per-vendor connection env vars, the uniform shape every smoke test follows, and when to keep a test as `hardware`-marked vs. demote it to a sim-backed integration test.

Hardware tests live exclusively under [tests/hardware/](../../tests/hardware/) and they are all `pytest.mark.hardware`. Default `uv run pytest` does **not** run them. They exist to catch the bugs that only manifest against real instruments: real serial timing, real Modbus framing edge cases, real V4L2 / FLIR camera quirks. They are not a substitute for unit tests against the stub adapters in `tests/fixtures/` — those run on every push and catch most regressions before a rig is in the loop.

---

## The opt-in gate

A single env var unlocks the entire tree:

```powershell
$env:CAPA_HARDWARE_TESTS = "1"
uv run pytest tests/hardware
```

[tests/hardware/__init__.py](../../tests/hardware/__init__.py) documents the gate; each smoke module also re-asserts it at module level:

```python
pytestmark = [
    pytest.mark.hardware,
    pytest.mark.anyio,
    pytest.mark.skipif(
        os.environ.get("CAPA_HARDWARE_TESTS") != "1",
        reason="CAPA_HARDWARE_TESTS=1 required",
    ),
]
```

Unlike `alicatlib`'s three-tier scheme (`hardware` / `hardware_stateful` / `hardware_destructive`), capa uses a single tier. Reasoning: every smoke test in `tests/hardware/` is already designed to be **non-destructive** — the writes they perform are deliberate no-op echoes (set the heater's current setpoint to its current value, set the MFC setpoint to its current setpoint). If a destructive hardware test ever becomes necessary, add a second marker rather than weakening the meaning of `hardware`.

---

## Per-vendor connection parameters

The opt-in env var alone is not enough — each smoke module also reads a small set of `CAPA_TEST_<VENDOR>_*` env vars that supply the connection parameters. If the vendor's primary env var is unset, the test calls `pytest.skip(...)` rather than failing, so it's safe to run `pytest tests/hardware` with only some instruments wired up.

| Adapter | Required | Optional (defaults shown) |
|---|---|---|
| Watlow | `CAPA_TEST_WATLOW_PORT` | `CAPA_TEST_WATLOW_ADDR` (`1`), `CAPA_TEST_WATLOW_PROTOCOL` (`stdbus` \| `modbus_rtu` \| `auto`), `CAPA_TEST_WATLOW_OPERATOR` (`"hw-test"`) |
| Alicat | `CAPA_TEST_ALICAT_PORT` | `CAPA_TEST_ALICAT_UNIT_ID` (`"A"`), `CAPA_TEST_ALICAT_BAUD` (`19200`), `CAPA_TEST_ALICAT_OPERATOR` (`"hw-test"`) |
| Sartorius | `CAPA_TEST_SARTORIUS_PORT` | `CAPA_TEST_SARTORIUS_PROTOCOL` (`"xbpi"`), `CAPA_TEST_SARTORIUS_BAUD` (`9600`), `CAPA_TEST_SARTORIUS_OPERATOR` (`"hw-test"`) |
| NI-DAQ | none if your rig uses `cDAQ1Mod1` | `CAPA_TEST_NIDAQ_DEVICE` (`"cDAQ1Mod1"`), `CAPA_TEST_NIDAQ_OPERATOR` (`"hw-test"`) |
| Webcam | `CAPA_TEST_WEBCAM_DEVICE` | `CAPA_TEST_WEBCAM_INPUT_FORMAT` (platform default), `CAPA_TEST_WEBCAM_OPERATOR` (`"hw-test"`) |

Operator IDs default to `"hw-test"` and exist so the [authorization gate](../safety/authorization-gates.md) has a real attributable identity in the bundle's event log — they have no rig-side meaning.

Example session running every wired-up smoke test:

```powershell
$env:CAPA_HARDWARE_TESTS = "1"
$env:CAPA_TEST_WATLOW_PORT = "COM5"
$env:CAPA_TEST_ALICAT_PORT = "COM6"
$env:CAPA_TEST_SARTORIUS_PORT = "COM7"
$env:CAPA_TEST_NIDAQ_DEVICE = "Dev1"
$env:CAPA_TEST_WEBCAM_DEVICE = "USB Video Device"
uv run pytest tests/hardware -v
```

Devices that aren't wired up will skip cleanly.

---

## The shape every smoke test follows

Every vendor smoke module is built the same way. If you add a new device adapter and want to give it a hardware smoke test, mirror this shape exactly so future contributors can read one and understand all of them.

### 1. Open / identify / close

The cheapest check — does the serial port open, does the device respond to its identification query, does `close()` clean up.

```python
class TestRealWatlow:
    async def test_open_identify_close(self) -> None:
        adapter = WatlowAdapter(name="heater", **_watlow_params())
        await adapter.open()
        try:
            assert adapter.device_info is not None
        finally:
            await adapter.close()
```

The `try / finally` is non-negotiable. A test that throws between `open()` and `close()` and leaves the port held breaks every subsequent test that wants that port.

### 2. Short headless freerun → sealed bundle

The integration-shaped check: build a minimal `ExperimentConfig` with one device, run `run_headless` for a few seconds, assert the bundle ended `completed + sealed + ok` and that both `device_records/<vendor>.parquet` and `scalars.parquet` were populated.

```python
async def _go() -> Path:
    result = await run_headless(config, runs_root=tmp_path / "runs")
    assert result.bundle_path is not None
    assert result.run_status == "completed", result.exit_reason
    assert result.bundle_status == "sealed", result.exit_reason
    assert result.integrity_status == "ok", result.exit_reason
    return result.bundle_path
```

Each of the three assertions includes `result.exit_reason` as its message because the only context you'll have when this fails in three months is the pytest output.

### 3. Authorized no-op write (where applicable)

For controllers (not pure sensors), echo the current setpoint back to the device. The physical state doesn't change, but the test exercises the `Authorization` + `confirm=True` round-trip through the real adapter — the path that goes wrong silently if anything in the authorization plumbing drifts. The test is automatically skipped if the connected device is a meter rather than a controller.

See [authorization gates](../safety/authorization-gates.md) for why this check exists at all.

---

## Fixture hygiene rules

These exist because hardware tests don't have the same isolation that pytest gives unit tests:

1. **Always `try / finally` an `open()`.** A leaked serial handle silently breaks the next test that wants the port.
2. **Don't share an adapter across tests.** Each test opens its own; modular state on the adapter object survives the test boundary otherwise.
3. **No `pytest.fixture(scope="session")` for hardware connections.** Some platforms misreport which test was running when an instrument-side error fires — per-test open isolates blame.
4. **Tag any new hardware test with the same three-element `pytestmark` list** (the `hardware` marker, `anyio`, and the `skipif` on `CAPA_HARDWARE_TESTS`). Don't rely on the package-level `__init__.py` alone; the per-module re-assertion is what gives you a useful skip reason in the pytest output.

---

## When a hardware test should become a sim-backed integration test

If the bug a hardware test catches can be reproduced via a [sim adapter](../devices/simulators.md), **promote it** to `tests/integration/`. Reasoning:

- CI never sees `tests/hardware/` (it can't — no rig in the GitHub Actions runner). A bug only covered by a hardware test is effectively only covered when a contributor is sitting at the rig PC.
- Sim adapters are deliberately built to mirror the real adapter's emission shape (`AlicatFrameField` on a sim looks like one on a real Alicat). Most protocol-shape bugs reproduce on the sim.
- The remaining hardware-only failure surface — Modbus framing under real serial timing, FLIR's USB enumeration quirks, V4L2 input-format negotiation — is small and worth keeping in `tests/hardware/`. Everything else belongs in `integration/`.

A test should stay `hardware`-marked only if (a) reproducing it requires real-instrument timing or framing behaviour that sim can't fake, or (b) it is explicitly checking that the adapter physically opens and closes the underlying device handle.
