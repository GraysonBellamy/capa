# capa validate

**Audience:** config authors and CI.
**Scope:** confirm an experiment config will load — and optionally that every declared adapter is reachable — before arming hardware. Headless equivalent of "did I typo something?" without standing up the runtime.

```
$ capa validate --help
Usage: capa validate [OPTIONS] CONFIG

  Validate an experiment config without running it.

Arguments:
  CONFIG  [required]

Options:
  --strict  Open + close each declared adapter (read-only handshake).
  --help    Show this message and exit.
```

---

## What it does

Three checks, in order:

1. **Pydantic load.** Parses the YAML and builds an [`ExperimentConfig`](../../src/capa/experiment/config.py). Catches structural errors: missing required fields, wrong types, malformed device entries.
2. **Plugin resolution.** Looks up the declared procedure id in the active plugin registry. Catches "you asked for `capa.my_procedure` but it is not installed."
3. **Adapter handshake (only with `--strict`).** For each device entry, looks up the adapter descriptor and — if the adapter module exposes an async `handshake(params)` hook — opens + identifies + closes the device. Read-only; no setpoints are written.

It is intentionally narrower than [`capa config validate`](capa-config.md), which runs the full layered validation pipeline (Layers 1–5). Use this command for a quick "will this thing start?" check; use `capa config validate` when you need the Problems-panel output with severities and per-section findings.

---

## Output

Clean validation (no `--strict`):

```
$ uv run capa validate configs/experiments/sim_freerun.yaml
OK: configs/experiments/sim_freerun.yaml
  hardware:  rig_sim (4 devices)
  channels:  7
  procedure: capa.builtin.free_run
```

With `--strict`, one extra line per device:

```
$ uv run capa validate configs/experiments/sim_freerun.yaml --strict
OK: configs/experiments/sim_freerun.yaml
  hardware:  rig_sim (4 devices)
  channels:  7
  procedure: capa.builtin.free_run
  strict: watlow_sim -> {'idn': 'WatlowSim/1.0', 'serial': 'sim-001'}
  strict: alicat_sim -> {'idn': 'AlicatSim/1.0'}
  strict: balance_sim -> WatlowSim.handshake (no handshake hook; descriptor-only check)
```

The `strict:` line per device reports either the adapter's identify summary or — for adapters whose module did not expose a `handshake` function — a "descriptor-only" note. Either is a pass; the only failure path is an exception, which exits non-zero.

---

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Config loaded and (with `--strict`) every adapter handshook. |
| 2 | Pydantic / plugin / handshake error. The first error encountered is printed in red to stderr. |

---

## When to reach for which validate command

| Situation | Command |
|---|---|
| "Will my YAML even parse?" | `capa validate <yaml>` |
| "Are the adapters wired right?" | `capa validate <yaml> --strict` |
| "I want the full layered problem report (errors / warnings / info)." | [`capa config validate <yaml>`](capa-config.md) |
| "Run the live discovery + handshake layer too." | [`capa config validate <yaml> --live`](capa-config.md) |
| "I have a hardware TOML, not an experiment YAML." | [`capa hardware validate`](capa-hardware.md) (offline) or [`capa hardware check`](capa-hardware.md) (live) |
| "I have a standalone `.method.toml`." | [`capa method validate`](capa-method.md) |
| "I want to check just the CAPA profile metadata block." | [`capa profile validate`](capa-profile.md) |

---

## CI usage

The classic pre-merge gate:

```yaml
- name: Validate experiment configs
  run: |
    for cfg in configs/experiments/*.yaml; do
      uv run capa validate "$cfg" || exit 1
    done
```

For a real-hardware CI runner, escalate to `--strict` — that adds the open-close handshake without writing any setpoints:

```yaml
- name: Validate against live rig
  run: uv run capa validate configs/experiments/prod.yaml --strict
```

A failed handshake prints `strict: <device>: handshake failed: <message>` to stderr and exits 2. Wire that message into your CI summary; it usually points at a serial port number or DAQ device name the runner cannot see.

---

## See also

- [`capa config validate`](capa-config.md) — full layered pipeline with severity-tagged problems
- [`capa hardware check`](capa-hardware.md) — same handshake idea, scoped to a hardware-only TOML
- [Validation and problems](../configuration/validation-and-problems.md) — the layered validator concept
- [Headless runs](headless-runs.md) — for how `--strict` differs from arming a real run
