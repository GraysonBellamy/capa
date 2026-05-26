---
description: Lifecycle of a headless capa run — process model, two-stage SIGINT handling, runs-root and plugins.lock resolution, bundle sealing, exit-code contract for CI.
---

# Headless runs

**Audience:** CI, automation, regression tests against real hardware, and any operator who would rather not look at a GUI.
**Scope:** what happens when you start a run with `capa run` (the default, `--headless`) — the process model, signal handling, where artifacts land, and the exit-code contract every wrapping script depends on.

This page is the cross-cutting reference; the per-command pages ([capa run](capa-run.md), [capa finalize](capa-finalize.md), [capa validate](capa-validate.md)) link back here instead of re-explaining the lifecycle.

---

## Process model

A headless run is **one process, one config, one bundle**. The dispatcher in [src/capa/cli/main.py](../../src/capa/cli/main.py) hands off to [src/capa/cli/run.py](../../src/capa/cli/run.py), which:

1. Resolves the runs root (`--runs-root` > `$CAPA_RUNS_ROOT` > `./runs`).
2. Resolves the plugins lockfile (`--plugins-lock` > auto-discovery; see [plugins lockfile resolution](#plugins-lock-resolution) below).
3. Configures pre-run structured logging.
4. Loads and Pydantic-validates the experiment config.
5. Installs a SIGINT handler (see [signals](#signals) below).
6. Calls [`run_headless`](../../src/capa/runtime/headless.py) inside an `anyio.run` block, which builds the worker pool, opens every adapter, constructs the conductor, runs the procedure, and seals the bundle.
7. Prints a five-line result summary and exits with a code derived from the result (see [exit codes](#exit-codes) below).

There is no daemon, no background process, no shared state across runs. Re-running across many configs in a sweep means re-running the executable — adapters open and close once per process. If a single config triggers the Sartorius cold-open race or similar one-time cost, you pay it once per `capa run` invocation.

---

## Signals

### SIGINT (Ctrl-C)

The CLI installs a two-stage handler via [`install_sigint_handler`](../../src/capa/runtime/signals.py):

- **First Ctrl-C** sets an `anyio.Event` that the conductor polls. The run unwinds gracefully: procedure steps stop, the writer drains, the bundle seals normally. You see `SIGINT received — initiating graceful stop (Ctrl-C again to force)` on stderr.
- **Second Ctrl-C** restores the OS default handler and the process terminates. Any rows still buffered in memory are lost; flushed in-flight Arrow IPC streams remain recoverable. The bundle is left as `open` on disk. Use [`capa finalize`](capa-finalize.md) to recover what made it to disk.

The first-stroke path is the supported way to stop a headless run from a wrapper script.

### SIGTERM and other signals

The CLI does **not** install a SIGTERM handler. Under systemd, supervisord, or `kill <pid>` (no signal flag), the process receives the default disposition and exits hard. If your CI runner sends SIGTERM, write a wrapper that translates SIGTERM to SIGINT before forwarding:

```bash
trap 'kill -INT $PID' TERM
uv run capa run "$CONFIG" &
PID=$!
wait $PID
```

This is the same constraint that applies to most asyncio-based Python applications.

---

## Where artifacts land

| Artifact | Default location | Override |
|---|---|---|
| Run bundle directory | `./runs/<run_id>/` | `--runs-root` or `$CAPA_RUNS_ROOT` |
| Run catalog (sqlite) | `./runs/runs.sqlite` | derived from runs root |
| Pre-run log output | stderr and `~/.capa/logs/capa-YYYYMMDD.log` | no env override today |
| Bundle manifest | `<run_id>/manifest.json` | n/a — fixed |
| Bundle checksums | `<run_id>/manifest.sha256` | n/a — fixed |

`<run_id>` is generated at run start; it is the bundle directory name and the key into `runs.sqlite`. The five-line summary printed at exit shows the run id, bundle path, and the three status fields:

```
run_id:           20260524T143200-7f3a
bundle:           /home/lab/runs/20260524T143200-7f3a
run_status:       completed
bundle_status:    sealed
integrity_status: ok
exit_reason:
```

`exit_reason` is empty on a clean exit; it carries a short string like `procedure_resolution: …` or `pool_open: …` on preflight refusals.

---

## Exit codes

The headless runner's exit code is computed from `(run_status, bundle_status, integrity_status)` in [`HeadlessResult.exit_code()`](../../src/capa/runtime/headless.py). The canonical mapping:

| Code | Meaning | Typical cause |
|---|---|---|
| **0** | `completed` + `sealed` | The run finished and the bundle is durable. The only "all good" exit. |
| **1** | `aborted` | Operator stop, SIGINT, preflight refusal (unknown procedure id, plugin not trusted, adapter materialization error). |
| **2** | `crashed` | An exception escaped the procedure or the worker pool. Bundle on disk may still be recoverable with [`capa finalize`](capa-finalize.md). |
| **3** | `verification_failed` | Bundle sealed but the recomputed `manifest.sha256` does not match what the writer recorded. The bundle is *not* trustworthy. |
| **5** | unexpected | Status combination the table above does not cover. Should never happen; treat as a bug. |

`capa validate` and the sub-app commands use a smaller code set: `0` on success, `2` on validation failure, `3` on a verify mismatch (catalog), `64` on missing-argument usage errors. See each per-command page for the table that applies.

**CI pattern:** treat `0` as "ship", `3` as "stop the line — bundle is corrupt", and `1`/`2` as "investigate, maybe retry":

```bash
uv run capa run experiment.yaml
case $? in
    0) echo "sealed" ;;
    1) echo "aborted — operator or preflight" ;;
    2) echo "crashed — check logs, maybe finalize" ;;
    3) echo "INTEGRITY FAILURE — stop the line" ; exit 3 ;;
    *) echo "unexpected exit $?" ; exit 1 ;;
esac
```

---

## Plugins-lock resolution

Plugin trust gates the runtime: in `production` mode, only procedures listed in `plugins.lock` will load. Resolution order (from [src/capa/cli/_helpers.py](../../src/capa/cli/_helpers.py)):

1. `--plugins-lock <path>` — explicit, must exist.
2. `./plugins.lock` — per-project lockfile in cwd.
3. `$XDG_CONFIG_HOME/capa/plugins.lock` (or `$HOME/.config/capa/plugins.lock`) — user-global lockfile.

In `dev` mode (default when `$CAPA_PLUGIN_MODE` is unset), nothing being found is silently OK. In `production` mode, nothing being found is a hard exit-2 with a message listing the searched paths.

When auto-discovery finds a lockfile and `--plugins-lock` was not passed, capa echoes `plugins.lock (auto-discovered): <path>` to stdout so the chosen file is visible in the log.

---

## Environment variables relevant to headless

| Variable | Effect |
|---|---|
| `CAPA_RUNS_ROOT` | Default runs root if `--runs-root` is unset. |
| `CAPA_PLUGIN_MODE` | `dev` (permissive) or `production` (gated by `plugins.lock`). Default `dev`. |
| `CAPA_DRAINING_DELAY_S` | Doc-tooling hook. Holds the conductor in the draining state for N seconds before sealing. Default `0`. Leave unset in real runs. |

The full env-var reference lives at [reference/environment-variables.md](../reference/environment-variables.md).

---

## Timeouts and watchdogs

`capa run` does **not** impose a wall-clock timeout on itself — long runs are a supported use case. If your CI needs a ceiling, wrap with `timeout(1)`:

```bash
timeout --signal=INT --kill-after=30s 4h \
    uv run capa run experiment.yaml --runs-root ./ci-runs
```

`--signal=INT` triggers the first-stage graceful stop. `--kill-after` falls back to SIGKILL if the graceful path itself wedges; in that case the bundle will be `open` on disk and you should run [`capa finalize`](capa-finalize.md) before consuming it.

Inside the runtime, the [saturation monitor](../safety/saturation-and-deadlines.md) provides the durable-output watchdog: it trips after 10 s of no writer progress and escalates to a `crashed_but_sealed` outcome rather than letting the rig run blind.

---

## Sweeps and multi-config CI

The pattern for running many configs in one CI job:

```bash
for cfg in configs/sweep/*.yaml; do
    uv run capa run "$cfg" --runs-root ./sweep-runs
    rc=$?
    [ $rc -eq 3 ] && { echo "INTEGRITY FAIL on $cfg" ; exit 3 ; }
done
uv run capa catalog list --runs-root ./sweep-runs --json > sweep-summary.json
```

Each `capa run` invocation re-opens adapters; for high-throughput sweeps where adapter open is expensive (Sartorius), batch many configs into a single experiment YAML and let the [batch procedure](../procedures/builtin-batch.md) reuse the pool across children.

---

## See also

- [`capa run`](capa-run.md) — full flag reference and examples
- [`capa finalize`](capa-finalize.md) — recover a crashed or interrupted bundle
- [`capa validate`](capa-validate.md) — confirm a config will load before arming hardware
- [Saturation and deadlines](../safety/saturation-and-deadlines.md) — the durable-output watchdog
- [Exit codes](../reference/exit-codes.md) — canonical exit-code table for every command
- [Environment variables](../reference/environment-variables.md) — full env-var reference
