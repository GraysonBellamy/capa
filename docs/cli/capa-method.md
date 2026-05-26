---
description: Lint standalone capa method files (.method.toml / .method.yaml) from CLI — Pydantic-validate the step recipe a procedure executes without an experiment.
---

# capa method

**Audience:** method authors.
**Scope:** sub-app for working with standalone method files (`.method.toml`, `.method.yaml`). Today the sub-app exposes a single command: `validate`.

```
$ capa method --help
Usage: capa method [OPTIONS] COMMAND [ARGS]...

  Method-file utilities.

Commands:
  validate  Validate a standalone method file (TOML or YAML).
```

A *method* is the step-by-step recipe a procedure executes. Most operators edit methods inside the GUI's Method tab; `capa method validate` is the headless equivalent of "lint this file before I check it in."

---

## `capa method validate`

```
$ capa method validate --help
Usage: capa method validate [OPTIONS] PATH

  Validate a standalone method file (TOML or YAML).

  Useful for a quick lint without loading a full experiment config.

Arguments:
  PATH  [required]
```

Loads the file (TOML or YAML, picked by suffix), Pydantic-validates it against the [`Method`](../../src/capa/experiment/method.py) model, and prints a one-line summary plus a per-step list.

```bash
$ uv run capa method validate configs/methods/sim_capa_pyrolysis.method.toml
OK: configs/methods/sim_capa_pyrolysis.method.toml
  method:  capa_pyrolysis_demo
  steps:   4
    [00] set_setpoint  target=watlow_sim
    [01] hold          target=-
    [02] ramp          target=watlow_sim
    [03] hold          target=-
```

Each step line shows the step kind and its target (the device or channel the step acts on), so structural problems — wrong kind name, target referring to a device that does not exist — surface immediately.

### Supported file extensions

| Suffix | Loader |
|---|---|
| `.toml` | `tomllib.load` |
| `.yaml`, `.yml` | `ruamel.yaml.YAML(typ="safe")` |

Any other suffix is rejected with exit 2 ("unsupported suffix").

### Exit codes

| Code | Meaning |
|---|---|
| 0 | File parsed and the `Method` model validated. |
| 2 | Unsupported suffix, parse error, or Pydantic validation failure. |

---

## When to reach for it vs the others

| Situation | Command |
|---|---|
| "I have a `.method.toml` and want to lint it." | `capa method validate <path>` |
| "I want to check a method *inside* a full experiment YAML." | [`capa validate <yaml>`](capa-validate.md) — full experiment load includes method validation. |
| "I want every layered finding against the experiment." | [`capa config validate <yaml>`](capa-config.md) |

`capa method validate` cannot tell you whether a method's `target=watlow_sim` matches an actual device in your hardware profile — that needs the cross-section check from the full layered pipeline, which only runs against a complete experiment. Use it for syntax-and-shape lint; use `capa config validate` for cross-section drift.

---

## Roadmap

The earlier stub for this page listed two commands that have not been implemented: `describe`, `graph`.

- **`describe`** — pretty-print a method's steps with their resolved parameters. Workaround: `capa method validate` already prints the per-step list, which is most of what `describe` would have done.
- **`graph`** — render the method as a directed graph (useful for branching/conditional methods). No equivalent today; in practice most CAPA methods are linear lists. If you need this, open an issue with an example method.

---

## See also

- [Method TOML](../configuration/method-toml.md) — every step kind and field
- [Method step reference](../procedures/method-steps-reference.md) — semantics per step kind
- [`capa validate`](capa-validate.md) — full-experiment validation that includes the method
- [The Method tab](../user-guide/the-method-tab.md) — GUI-side method authoring
