# capa run

**Audience:** operators and CI/headless users.
**Scope:** start a run from the command line. Default is headless; `--gui` is a synonym for [`capa gui`](capa-gui.md).

```
$ capa run --help
Usage: capa run [OPTIONS] CONFIG

  Run an experiment.

Arguments:
  CONFIG  [required]

Options:
  --headless / --gui   Headless mode (no GUI).  [default: headless]
  --runs-root    PATH  Where to write the bundle. Default: $CAPA_RUNS_ROOT or ./runs.
  --plugins-lock PATH  plugins.lock for procedure trust; mirrored into the manifest.
  --help               Show this message and exit.
```

---

## What it does

Reads the experiment YAML at `CONFIG`, loads it into an [`ExperimentConfig`](../../src/capa/experiment/config.py), and dispatches to either the headless runner or the GUI runner.

In **headless mode** (default), the dispatcher in [src/capa/cli/run.py](../../src/capa/cli/run.py):

1. Resolves the runs root and plugins lockfile.
2. Configures structured pre-run logging.
3. Installs the two-stage SIGINT handler.
4. Builds the worker pool, opens every adapter, constructs the conductor, runs the procedure, and seals the bundle — see [headless runs](headless-runs.md) for the full lifecycle.
5. Prints the run id, bundle path, and the three status fields, then exits with the code derived from them.

In **`--gui` mode** the command is equivalent to `capa gui CONFIG` and the qasync bootstrap takes over. Same `--runs-root` and `--plugins-lock` apply.

---

## Synopsis

```bash
# Headless on the simulator (default)
uv run capa run configs/experiments/sim_freerun.yaml

# Headless against a custom runs root
uv run capa run configs/experiments/prod.yaml --runs-root /data/runs

# Hand off to the GUI (or just run `capa gui` directly)
uv run capa run configs/experiments/sim_freerun.yaml --gui

# Headless with an explicit plugins lockfile
uv run capa run configs/experiments/prod.yaml \
    --runs-root /data/runs \
    --plugins-lock /etc/capa/plugins.lock
```

---

## Flags

| Flag | Default | Meaning |
|---|---|---|
| `CONFIG` (positional) | — | Path to the experiment YAML. File must exist. |
| `--headless` / `--gui` | `--headless` | Choose runner. `--gui` is identical to running `capa gui CONFIG`. |
| `--runs-root PATH` | `$CAPA_RUNS_ROOT` → `./runs` | Where the bundle directory is created. |
| `--plugins-lock PATH` | auto-discovery | `plugins.lock` to enforce. See [plugins-lock resolution](headless-runs.md#plugins-lock-resolution). |

The CLI deliberately exposes a small flag surface. Anything experiment-specific — operator id, sample id, procedure, method, calibration set — lives in the YAML and changes via editing the file, not via flags. This keeps the audit trail (the manifest records the exact config that ran) honest.

---

## Output

```
$ uv run capa run configs/experiments/sim_freerun.yaml
plugins.lock (auto-discovered): /home/lab/capa/plugins.lock
run_id:           20260524T143200-7f3a
bundle:           /home/lab/capa/runs/20260524T143200-7f3a
run_status:       completed
bundle_status:    sealed
integrity_status: ok
```

`exit_reason:` is appended (and non-empty) on preflight refusals or crashed runs.

---

## Exit codes

The full table lives in [headless runs](headless-runs.md#exit-codes). In short:

| Code | Meaning |
|---|---|
| **0** | `completed` + `sealed` — the happy path |
| **1** | `aborted` (SIGINT, operator stop, preflight refusal) |
| **2** | `crashed` (exception escaped the runtime) |
| **3** | `verification_failed` (sealed bundle's checksums do not match) |

For CI, the key distinction is between **1/2** (investigate and probably retry) and **3** (do not trust the bundle).

---

## Common scenarios

### Stop a long run cleanly

```
^C
SIGINT received — initiating graceful stop (Ctrl-C again to force)
…
run_id:           20260524T143200-7f3a
bundle:           …
run_status:       aborted
bundle_status:    sealed
integrity_status: ok
```

The first Ctrl-C triggers the graceful path; the bundle still seals. The second Ctrl-C falls back to OS-default and the bundle is left `open` on disk; recover with [`capa finalize`](capa-finalize.md).

### The runner died mid-run

The bundle directory is on disk but `bundle_status` is `open`. Run:

```bash
uv run capa finalize <run_id> --runs-root /data/runs
```

See [`capa finalize`](capa-finalize.md) for what it can and cannot recover.

### Re-arm the same config in a loop

```bash
for i in 1 2 3; do
    uv run capa run configs/experiments/repeat.yaml --runs-root ./runs
done
```

Each invocation re-opens adapters. If adapter open is expensive on your rig (Sartorius cold-open), prefer a single config that drives multiple children via the [batch procedure](../procedures/builtin-batch.md) over a shell loop.

---

## What `capa run` does NOT do

- Take `--operator`, `--sample`, `--method`, or `--calibration` flags. These all live in the YAML.
- Override the procedure id from the command line. Picking a procedure is a config-level decision.
- Wall-clock timeout itself. Wrap with `timeout(1)` if you need a ceiling; see [headless runs](headless-runs.md#timeouts-and-watchdogs).
- Run multiple configs in parallel. One process = one bundle.

---

## See also

- [Headless runs](headless-runs.md) — lifecycle, signals, exit codes
- [`capa gui`](capa-gui.md) — the GUI counterpart (identical underlying runtime)
- [`capa validate`](capa-validate.md) — confirm the config loads before arming
- [`capa finalize`](capa-finalize.md) — recover a bundle whose `capa run` did not exit cleanly
- [`capa catalog list`](capa-catalog.md) — find the bundle after the fact
