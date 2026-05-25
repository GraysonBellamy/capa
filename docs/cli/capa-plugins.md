# capa plugins

**Audience:** plugin authors, operators verifying a production-mode rig, lab leads granting trust to a new procedure.
**Scope:** sub-app for inspecting the live plugin set and writing entries into `plugins.lock`. Two commands: `list`, `trust`.

```
$ capa plugins --help
Usage: capa plugins [OPTIONS] COMMAND [ARGS]...

  Inspect plugin trust state.

Commands:
  list   List discovered procedure plugins.
  trust  Add (or refresh) a discovered plugin in ``plugins.lock``.
```

Plugin trust gates whether the runtime will load a procedure. See [plugin system](../extending/plugin-system.md) and [plugin lockfile](../extending/plugin-lockfile.md) for the underlying model; this page is the CLI surface.

---

## `capa plugins list`

```
$ capa plugins list --help
Usage: capa plugins list [OPTIONS]

  List discovered procedure plugins.

  Walks the ``capa.procedures`` entry-point group, runs the load-time
  contract check, and (in production mode) gates each plugin against
  ``plugins.lock``. Rejected plugins are listed with the reason.

Options:
  --plugins-lock  PATH  Path to plugins.lock; default ./plugins.lock
  --plugin-mode   TEXT  dev|production; overrides $CAPA_PLUGIN_MODE.
```

Walks `capa.procedures` entry-points, runs the load-time contract check on each, and in production mode gates them against `plugins.lock` when a lockfile is loaded. Three buckets are reported: **loaded**, **rejected**, and **drifts** (lockfile entries whose metadata no longer matches the installed package). If no lockfile is found, this inspection command prints that production mode requires one but still exits `0`.

```bash
# Inspect what's loadable with the current settings
uv run capa plugins list

# Try resolving against a non-default lockfile
uv run capa plugins list --plugins-lock /etc/capa/plugins.lock

# Force production-mode behavior even if $CAPA_PLUGIN_MODE is unset.
# Pass a lockfile to see MISSING_FROM_LOCK / HASH_MISMATCH rejections.
uv run capa plugins list --plugin-mode production --plugins-lock ./plugins.lock
```

### Output

```
$ uv run capa plugins list
plugin mode: production
plugins.lock: /home/lab/capa/plugins.lock (schema v1)

id                                        package               version     hash
capa.builtin.free_run                     capa                  0.0.0       sha256:9a8b7c6d5e4f…
capa.builtin.recipe_runner                capa                  0.0.0       sha256:9a8b7c6d5e4f…
capa.builtin.batch                        capa                  0.0.0       sha256:9a8b7c6d5e4f…
capa.contrib.my_procedure                 capa_contrib          0.3.1       sha256:a1b2c3d4e5f6…

Rejected:
  capa.contrib.experimental: contract_check_failed: missing async run()

Drift vs plugins.lock:
  capa.contrib.my_procedure: version (expected='0.3.0', actual='0.3.1')
```

**Plugin mode**: `dev` allows anything that passes the contract check; `production` allows only entries listed in the loaded `plugins.lock` (and rejects drift). The `run` and `gui` commands fail fast in production if no lockfile is found; `plugins list` reports the missing lock but remains an inspection command.

**Rejected** lists plugins that did not survive the contract check or that failed lockfile gating, with a one-line reason per plugin.

**Drift vs plugins.lock** reports plugins where the lockfile expected one version/hash and the installed package presents another. In `dev` mode, drift is informational. In `production` mode with a loaded lockfile, blocking drift also keeps that procedure out of the loaded set.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Discovery completed (even if every plugin was rejected). |

---

## `capa plugins trust`

```
$ capa plugins trust --help
Usage: capa plugins trust [OPTIONS] PLUGIN_ID

  Add (or refresh) a discovered plugin in ``plugins.lock``.

Arguments:
  PLUGIN_ID  Plugin id to add to plugins.lock  [required]

Options:
  --plugins-lock  PATH  Path to plugins.lock; default ./plugins.lock
  --reason        TEXT  Audit text recorded alongside the trust grant. Required.
```

Grants trust to a discovered plugin by writing its current entry-point + version + distribution hash into the lockfile. If the plugin is already present in `plugins.lock`, its metadata is refreshed (use case: a trusted package version-bumped and you have decided to accept the new version).

**`--reason` is required.** The text is appended to a journal sidecar (`plugins.lock.journal`) so there is an audit trail of *who trusted what, when, and why*. The technical primitive is here; the policy (who has the authority to grant trust) lives in lab procedure, not in code.

```bash
$ uv run capa plugins trust capa.contrib.my_procedure \
      --reason "approved by GB, ticket OPS-1284, code-review #421"
trusted: capa.contrib.my_procedure 0.3.1 (sha256:a1b2c3d4e5f6789abcdef0…)
  lock:    /home/lab/capa/plugins.lock
  journal: /home/lab/capa/plugins.lock.journal
```

### Effects on disk

- `plugins.lock` is rewritten (TOML, using `tomli_w`). Existing entries for *other* plugins are preserved verbatim.
- `plugins.lock.journal` is appended with a tab-separated line: `<iso-timestamp>\ttrust\t<id>\t<version>\t<hash>\t<reason>`.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Plugin entry written and journaled. |
| 2 | Missing `--reason`, plugin not discoverable, or other refusal. The error message lists available plugin ids. |

### Removing trust

There is no `capa plugins untrust` today. To revoke trust, edit `plugins.lock` and remove the entry by hand — keep the journal append-only by also writing a manual revoke line:

```bash
echo -e "$(date -Iseconds)\trevoke\tcapa.contrib.my_procedure\t-\t-\trevoked: see ticket OPS-1290" \
    >> plugins.lock.journal
```

A first-class `untrust` command may land later. Until then, the rule is: changes to `plugins.lock` should always be paired with a journal line.

---

## Roadmap

The earlier stub for this page mentioned `lock` (write a new lockfile) and `verify` (fail on drift). Today:

- **`lock`** — there is no separate "write me an empty lockfile" command. The first `capa plugins trust …` call creates the file if it does not exist.
- **`verify`** — `capa plugins list` already reports drift; treat any non-empty "Drift vs plugins.lock" section as a verify failure in your CI.

A dedicated `capa plugins verify` that exits non-zero on drift would be a useful add; open an issue if you need it.

---

## See also

- [Plugin system](../extending/plugin-system.md) — entry-points, registries, the load-time contract
- [Plugin lockfile](../extending/plugin-lockfile.md) — what's in `plugins.lock`, how it's resolved
- [Headless runs](headless-runs.md#plugins-lock-resolution) — auto-discovery order at run time
- [Writing a procedure](../extending/writing-a-procedure.md) — the contract that `list` enforces
