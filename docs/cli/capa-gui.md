# capa gui

**Audience:** operators.
**Scope:** launch the PySide6 GUI. The headless runtime sits underneath unchanged; the GUI is the visualization and command surface on top of it.

```
$ capa gui --help
Usage: capa gui [OPTIONS] [CONFIG]

  Launch the GUI. Loads the optional config if provided; otherwise opens
  empty and the operator picks a config via File > Open.

Arguments:
  CONFIG  Optional experiment config to preload.

Options:
  --runs-root    PATH  Where to write bundles. Default: $CAPA_RUNS_ROOT or ./runs.
  --plugins-lock PATH  plugins.lock for procedure trust; mirrored into the manifest.
  --help               Show this message and exit.
```

---

## What it does

Starts the qasync bootstrap (Qt event loop merged with asyncio), loads PySide6, and opens the main window. If `CONFIG` is provided, the Setup tab opens with that experiment already loaded; otherwise the welcome screen lets the operator pick one from **File → Open** or **Try a simulator**.

The conductor / worker-pool stack underneath is the same one [`capa run`](capa-run.md) uses. The GUI does not replace the runtime — it talks to the same conductor through the ManualClient surface described in the [runtime architecture](../architecture/runtime-architecture.md) doc.

Lazy-imported PySide6: if you run anything other than `capa gui` (or `capa run --gui`), the GUI dependency is not loaded. This is why headless CI can run on a machine without a working Qt platform plugin.

---

## Synopsis

```bash
# Empty welcome screen — pick a config from File > Open
uv run capa gui

# Launch with a config preloaded
uv run capa gui configs/experiments/sim_freerun.yaml

# Custom runs root
uv run capa gui --runs-root /data/runs

# Equivalent — `capa run --gui` dispatches to the same path
uv run capa run configs/experiments/sim_freerun.yaml --gui
```

---

## Flags

| Flag | Default | Meaning |
|---|---|---|
| `CONFIG` (positional, optional) | none | Preload this experiment YAML on launch. Omit to open empty. |
| `--runs-root PATH` | `$CAPA_RUNS_ROOT` → `./runs` | Where the GUI writes bundles. |
| `--plugins-lock PATH` | auto-discovery | `plugins.lock` to enforce. See [plugins-lock resolution](headless-runs.md#plugins-lock-resolution). |

---

## Exit codes

The GUI returns the underlying Qt exit code:

| Code | Meaning |
|---|---|
| 0 | Clean shutdown via the window close button or **File → Quit**. |
| 2 | Initial config refused at launch. |
| other non-zero | An uncaught Qt or runtime error. Check the log. |

The headless exit-code table from [`capa run`](capa-run.md) does **not** apply here — the GUI does not collapse a run outcome into a process exit code, because the operator can run many sessions in one launch. Use [`capa catalog list`](capa-catalog.md) to inspect outcomes after the fact.

---

## What the GUI shows

A quick orientation; the dedicated pages have screenshots and the deep tour:

- **Setup tab** — experiment/hardware editor, validation problems panel, connection strip. See [the Setup tab](../user-guide/the-setup-tab.md).
- **Method tab** — method-step editor for ramped/multi-step runs. See [the Method tab](../user-guide/the-method-tab.md).
- **Run tab** — live plots, state badge, start/stop, manual cards. See [the Run tab](../user-guide/the-run-tab.md).
- **Status bar** — saturation, loop, queue depth, current bundle path. See [status bar guide](../user-guide/status-bar-guide.md).
- **Diagnostics dock** — internal metrics; hidden by default. See [diagnostics dock](../user-guide/diagnostics-dock.md).

---

## See also

- [`capa run`](capa-run.md) — the headless counterpart (identical underlying runtime)
- [Quick start](../getting-started/quick-start.md) — the five-minute simulator tour
- [Operator handbook](../user-guide/operator-handbook.md) — single-page operator reference
- [UI / runtime boundary](../architecture/ui-runtime-boundary.md) — how the GUI talks to the conductor
