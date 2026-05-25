# Plugin lockfile

**Audience:** operators reading the file, and plugin authors regenerating it after a change.
**Scope:** the format, contents, generation, and verification of `plugins.lock` — the per-environment file that pins which third-party procedure plugins are trusted to run real hardware.

For the *why* — what the lock is gating against and why capa has a trust mode at all — see [Plugin system → The trust model](plugin-system.md#the-trust-model).

---

## What the lockfile is

`plugins.lock` is a small TOML file that contains one assertion per third-party procedure plugin:

> *"This plugin id was installed from this distribution at this version, and its on-disk hash was this. It is permitted to run."*

Production-mode runs refuse any installed procedure plugin that does not have a matching entry. Dev-mode runs ignore the gate entirely (drift is still computed and shown, just not enforced).

The full models live in [`src/capa/core/plugins_lock.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/core/plugins_lock.py) — `PluginsLock` for the file, `PluginEntry` for one record.

## Format

One header (the schema version) and one `[[plugins]]` block per trusted plugin:

```toml
version = 1

[[plugins]]
id = "lab.heatflux.calibration"
package = "lab-heatflux"
version = "1.4.2"
entry_point = "lab_heatflux.proc:CalibrationProcedure"
distribution_hash = "sha256:f1e9d4ab6c0a8c1e1c0a8c1e1c0a8c1e1c0a8c1e1c0a8c1e1c0a8c1e1c0a8c1e"

[[plugins]]
id = "site.bench42.spark_test"
package = "bench42-procedures"
version = "0.7.0"
entry_point = "bench42.procedures.spark:SparkTest"
distribution_hash = "sha256:1c0a8c1e1c0a8c1e1c0a8c1e1c0a8c1e1c0a8c1e1c0a8c1e1c0a8c1e1c0a8c1e"
```

The five `PluginEntry` fields are the trust assertion:

| Field | What it means |
|---|---|
| `id` | Plugin id, as declared on the class's `id` ClassVar. Matched against the live entry point's loaded class. |
| `package` | PyPI / distribution name. Informational; the live discovery cross-checks it. |
| `version` | PEP 440 version string. Compared *exactly* — range matching is not supported. |
| `entry_point` | `module.path:Class` form, used to import and instantiate. A drift here would mean the package's `pyproject.toml` moved the class. |
| `distribution_hash` | `sha256:HEX` (or `sha512:HEX`) of the installed wheel's `METADATA + RECORD`. |

Built-in plugins (`capa.builtin.*`) do not need to appear here — they share the running engine's distribution and are always trusted.

## What the hash covers (and what it does not)

The hash is computed by `_hash_distribution` over the distribution's `METADATA` and `RECORD` files. This is fast — no walking of every installed source file — and stable.

- It **does** catch a wheel swap: a different version installed over the previous one, or `RECORD` tampered with.
- It **does not** catch a source-file edit in an editable install (`pip install -e .`), because `METADATA` and `RECORD` are unchanged when only `your_plugin/something.py` changes.
- It **does not** catch runtime monkey-patching of the loaded class.

The practical rule: production-mode runs should be against wheel installs, not editable installs. Editable installs are appropriate during plugin development (where the engine runs in `dev` mode); they are not appropriate when the trust gate is meant to mean something.

The lockfile-aware version of `dist.version` for editable installs (e.g. `0.0.1.dev1+gAB12CD3.d20260525`) *does* change on each git commit, so even editable installs invalidate the lock on every commit — operators have to re-trust before the next production run. That is by design.

## Generating: `capa plugins trust`

`capa plugins trust <plugin-id> --reason "..."` is the only command that writes to `plugins.lock`. It does three things:

1. Run the same procedure discovery the live engine runs (in `dev` mode, so contract-pass plugins surface even without a prior trust entry).
2. Find the plugin whose id matches the argument, snapshot its current `(version, entry_point, distribution_hash)`, and write that as a `PluginEntry`.
3. Append one line to the journal (`plugins.lock.journal`) recording the action — the ISO timestamp, the action verb, the plugin id, the snapshotted version and hash, and the operator-provided `--reason`.

```text
$ capa plugins trust hello.procedure.hold_setpoint \
      --reason "approved by Grayson 2026-05-25 after demo on rig A"
trusted: hello.procedure.hold_setpoint 0.1.0 (sha256:f1e9d4ab6c0a8c1e1c0a8c…)
  lock:    plugins.lock
  journal: plugins.lock.journal
```

`--reason` is required. The journal is the audit trail; it is not a developer comment. Treat the reason text like a Git commit message — it should make sense to someone reading the journal six months later.

If the plugin is already in the lock, `trust` refreshes the entry rather than refusing — useful when you upgrade the plugin to a new version. The journal then has both the old and the new line, preserving the history of the trust grant.

## Verifying: what production-mode runs check

When `capa run` (or any other entry-point that builds a `ProcedureRegistry`) starts in production mode, the trust check runs as part of discovery. It is implemented by `detect_drift` in [`plugins_lock.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/core/plugins_lock.py) and exhaustively catalogues every mismatch:

| Drift kind | What it means | Action |
|---|---|---|
| `MISSING_FROM_INSTALL` | The lock names a plugin id that is not installed. | Informational. The lock has an obsolete entry; clean it up at your convenience. |
| `MISSING_FROM_LOCK` | A plugin is installed but has no lock entry. | Blocking. Production refuses to load it. Run `capa plugins trust` to add one. |
| `VERSION_MISMATCH` | The lock and the install disagree on `version`. | Blocking. Refresh the lock with `capa plugins trust`. |
| `HASH_MISMATCH` | The lock and the install disagree on `distribution_hash`. | Blocking. Most often: the wheel was rebuilt or re-installed. Refresh with `capa plugins trust`. |
| `ENTRY_POINT_MISMATCH` | The lock and the install disagree on `module.path:Class`. | Blocking. The plugin's `pyproject.toml` moved the class. Refresh with `capa plugins trust`. |

Every blocking drift is also visible in `capa plugins list` in production mode when you point it at an existing lockfile, with the same `(expected, actual)` detail. If no lockfile is found, the list command reports that production mode requires one but still exits successfully for inspection.

## Workflow recipes

The lockfile is not something you author by hand. These are the four flows that should cover almost every interaction with it:

**Adopt a new third-party plugin.**
Start from an existing lockfile. In a brand-new environment, a schema-only file with `version = 1` is enough for `capa plugins list --plugin-mode production` to report `MISSING_FROM_LOCK`.

```text
uv pip install lab-heatflux                                                # install the wheel
capa plugins list --plugin-mode production --plugins-lock ./plugins.lock   # see it rejected as MISSING_FROM_LOCK
capa plugins trust lab.heatflux.calibration --reason "lab policy LP-12"   # add the trust entry
capa plugins list --plugin-mode production --plugins-lock ./plugins.lock   # now loaded
```

**Upgrade a trusted plugin.**
```text
uv pip install --upgrade lab-heatflux                                       # new wheel; hash changes
capa plugins list --plugin-mode production --plugins-lock ./plugins.lock    # rejected as HASH_MISMATCH
capa plugins trust lab.heatflux.calibration --reason "upgrade to 1.5.0"    # refresh hash+version
```

**Remove a plugin from the trust set.**
Edit `plugins.lock` by hand to drop the `[[plugins]]` block. There is no `capa plugins revoke` today — the lockfile is small enough that hand-editing is acceptable, and removing an entry leaves the audit journal intact. Add a journal line yourself if you want the removal recorded.

**Lock a fresh environment.**
Generally CI / deployment pipelines should commit `plugins.lock` to source control. A fresh environment starts in `dev` mode (no gate); switch to `production` once you have written the lock entries the deployed plugins need.

## Where the file goes

By default, `plugins.lock` is read from the current working directory. The [`discover_plugins_lock_paths`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/cli/_helpers.py) helper looks in a small candidate list; you can also pass `--plugins-lock <path>` to any of the `capa plugins …` commands.

The lockfile is per-environment, not per-experiment. Bundles do not contain their own `plugins.lock` file — the trust assertion is about whether the running engine is permitted to load the procedure plugin in the first place. The bundle's `manifest.json.plugins` block mirrors the lock entries handed to the run, so a later analyst can see the active trust set. It is not proof that every listed plugin was loaded or used by that specific run.

## When the format changes

`version = 1` is the schema version. A breaking change to the file format will increment it; the engine will refuse to load a higher-version lockfile than it understands and will tell you exactly that. There is no automatic migration today — the lock is small enough that re-trusting plugins on a version bump is cheaper than maintaining a migration path.
