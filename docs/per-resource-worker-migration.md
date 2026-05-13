# Per-resource worker migration — remaining work

**Status:** Phases 0–4 complete; Phase 5 (hardening & docs) is the only remaining work.
**Last updated:** 2026-05-12
**Architecture reference:** [`docs/runtime-architecture.md`](runtime-architecture.md) — the standalone reference for the runtime as built. Read it first.

The migration moved every hardware resource (each serial port, each DAQmx task, each camera handle) onto its own thread and asyncio loop, replacing the old single-loop `Engine`. Phases 0–4 landed the architecture, deleted the legacy code paths (`engine.py`, `cameras.py`, `registry.py`), and wired the UI through the new `ManualClient` / `Conductor` surface. What follows is the residual hardening work that closes out the migration.

---

## Phase 5 — Hardening & docs (≈ 3-5 days)

### 5.1 Hardware bring-up

Run `capa_real_full` config end-to-end against the dev rig. Validate:

- No Watlow `ReadResponse` errors over a 1-hour run (the original symptom — see [`runtime-architecture.md` §5](runtime-architecture.md#5-the-cancellation-shield)).
- Sartorius sustains 50 Hz with `samples_late / samples_emitted < 0.02`.
- UI `loop_lag_ms_p99 < 50` throughout.
- Setpoint commands complete in < 100 ms under full load.
- Multiple consecutive runs against the same pool (no cold-open between runs — verifiable: adapter-open counter only bumps at `pool.open()`).

### 5.2 Shutdown stress test

Stuck adapter, disconnected USB, full disk. Confirm grace + hard-stop produce diagnostic bundles with stack traces. Specifically:

- A stuck adapter that ignores `adapter.stop()` triggers Phase B and records `worker_hard_stop_attempt` with the worker's frame stack from `sys._current_frames()`.
- If `thread.join` times out, the bundle records `worker_thread_leaked` and the run is marked degraded.
- USB disconnect mid-run trips the per-worker watchdog per the device's `on_failure` policy.

### 5.3 Saturation deadline stress test

Induce a writer stall (fill disk to < margin). Confirm:

- The bundle seals as `crashed_but_sealed` within `saturation_deadline_s + poll_period` (default 11 s).
- The bundle contains a `saturation_deadline` event with the cause (`writer_inbox_stalled` or `worker_<id>_outbound_saturated`) and the relevant metrics (`depth`, `since_last_accept_s`, or `blocked_s`).
- `adapter.stop()`'s safe-shutdown path still ran — hardware is not left in an inconsistent state.

### 5.4 Cancellation shield stress test on real hardware

Rapid manual-card cancel/retry against the real Watlow. Confirm zero `ReadResponse` errors over 100 attempts. This is the load-bearing rule from [`runtime-architecture.md` §5](runtime-architecture.md#5-the-cancellation-shield); the test was simulated in Phase 1 unit tests, but the real-hardware confirmation is a Phase 5 deliverable.

### 5.5 Documentation

- [x] [`docs/runtime-architecture.md`](runtime-architecture.md) — standalone runtime reference for contributors (cancellation shield rule, `CustomStep` CPU-offload contract, topology invariants).
- [ ] Update [`docs/capa-plan.md`](capa-plan.md) to reflect the new architecture.
- [ ] Update [`CLAUDE.md`](../CLAUDE.md) (and any plugin-author-facing docs) if it still references the engine.
- [ ] Delete or fold in [`docs/perf-architecture-plan.md`](perf-architecture-plan.md) — that plan was superseded by this migration.

### 5.6 Manifest schema

Add the `diagnostics.runtime` block specification to the manifest doc. Contents (each per-run):

- Per-thread CPU %.
- Per-loop `loop_lag_ms_p99`.
- Per-bridge `latency_p99_ms` and `blocked_since_ms` (max-seen during the run).
- Per-worker `tick_duration_ms_p99` and `samples_late`.
- `global_sdk_constraints` map (which adapters depend on which process-singleton SDKs — see [`runtime-architecture.md` §10.3](runtime-architecture.md#103-resource-validation)).

### 5.7 Performance baseline

Record metrics for `capa_real_full` on the dev rig; commit as a regression baseline. Targets (from the migration plan, carried forward):

| Metric | Target |
|---|---|
| UI `loop_lag_ms_p99` | < 50 ms |
| UI plot repaint cadence | steady 10 Hz |
| Watlow setpoint round-trip p99 | < 100 ms, zero errors |
| Sartorius @ 50 Hz `samples_late` | < 2 % |
| Sartorius @ 50 Hz `tick_duration_ms_p99` | < 18 ms |
| Conductor → writer-submit p99 | < 5 ms |
| Cross-thread bridge latency p99 | < 1 ms |
| Pool open time (full config) | < 800 ms (parallel) |
| Per-run arm time | < 50 ms (no adapter re-open) |
| Memory resident (full config) | < 350 MB |

CI nightly bench fails if any metric exceeds threshold by > 2× baseline.

### 5.8 Loop-implementation benchmark (optional)

With the baseline committed, branch off and swap `asyncio.new_event_loop()` for `winloop.new_event_loop()` in each worker and the conductor (and `uvloop.new_event_loop()` on Linux/macOS — both are drop-in `AbstractEventLoop` implementations; no bridge / command / shutdown changes needed). Re-run the bench suite and hardware smoke. Adopt as a separate change if wins are material; revert if not.

---

## Acceptance criteria

Phase 5 closes — and the migration is complete — when **all** of the following hold:

1. `engine.py`, `cameras.py`, and `registry.py` are deleted; ripgrep confirms zero references. *(done in Phase 4)*
2. Every adapter exposes `resource_id`; Adapter Protocol requires it. *(done in Phase 0)*
3. `python -m capa.app run configs/experiments/sim_capa_pyrolysis.yaml` produces a bundle with no manifest drift vs the pre-migration baseline (modulo `diagnostics.runtime` block). *(done in Phase 2)*
4. `python -m capa.app gui --config configs/experiments/capa_real_full.yaml` (on the hardware rig) completes a 30-min run with:
   - No Watlow `ReadResponse` errors.
   - Sartorius `samples_late / samples_emitted < 0.02`.
   - UI `loop_lag_ms_p99 < 50` throughout.
   - No `worker_hard_stop_attempt` or `worker_thread_leaked` events.
5. Multiple consecutive runs against the same pool succeed without re-opening adapters (verified by adapter-open counter — bumped only at `pool.open()`).
6. Manual cards work between runs without re-opening hardware (the Sartorius cold-open race is paid once at config-load, not per run).
7. Cancellation shield holds: rapid manual-card cancel/retry produces zero Watlow `ReadResponse` errors over 100 attempts.
8. Saturation deadline observable: induced writer stall seals the run as `crashed_but_sealed` with a `saturation_deadline` event, within `deadline_s + 2s`.
9. All tests pass: `uv run pytest tests/` green.
10. Shutdown stress tests pass (stuck adapter, disconnected USB, full disk, kill -9 conductor process).
11. [`docs/runtime-architecture.md`](runtime-architecture.md), [`docs/capa-plan.md`](capa-plan.md), and `CLAUDE.md` reflect the new architecture.
12. Manifest's `diagnostics.runtime` block contains per-thread CPU%, per-loop lag p99, per-bridge latency p99 and `blocked_since_ms`, per-worker tick duration p99, per-worker `samples_late`, and `global_sdk_constraints` map.
13. Hardware bench-baseline committed; CI nightly bench passes.

Items 1–3 and parts of 9 already hold from Phases 0–4. The hardware-rig and observability items (4, 7, 8, 10, 12, 13) close in Phase 5.

---

*End of document.*
