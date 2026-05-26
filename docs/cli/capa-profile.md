---
description: Validate domain_profile metadata block of a capa experiment config — capa_pyrolysis and cone_calorimeter metadata checked against active profile model.
---

# capa profile

**Audience:** CAPA pyrolysis operators and analysts checking that the domain-profile metadata block on an experiment is well-formed.
**Scope:** sub-app for validating the `domain_profile` block of an experiment config. Today the sub-app exposes a single command: `validate`.

```
$ capa profile --help
Usage: capa profile [OPTIONS] COMMAND [ARGS]...

  Domain-profile utilities.

Commands:
  validate  Validate the domain-profile metadata block of an experiment config.
```

A *profile* here means a domain profile — `capa_pyrolysis`, `cone_calorimeter`, etc. — which carries scientific metadata the runtime does not interpret but the bundle preserves for downstream analysis. See [CAPA profile fields](../configuration/capa-profile.md) for what those fields are.

---

## `capa profile validate`

```
$ capa profile validate --help
Usage: capa profile validate [OPTIONS] CONFIG

  Validate the domain-profile metadata block of an experiment config.

  Loads the full :class:`ExperimentConfig`, then re-validates the
  profile-specific metadata against the active profile's metadata model
  (CAPA's :class:`CapaPyrolysisMetadata`, etc.). Does not run preflight
  checks — those need a live engine.

Arguments:
  CONFIG  [required]
```

Loads the experiment config, then runs the profile-specific metadata validator (CAPA's [`validate_metadata`](../../src/capa/experiment/profiles/capa_pyrolysis.py) for `capa_pyrolysis` profiles, the cone-calorimeter equivalent for `cone_calorimeter`). The check is **metadata-only** — it does not touch hardware or run preflight checks.

The active profile is picked by the `domain_profile.id` field in the config:

| Profile id contains… | Validator used |
|---|---|
| `capa_pyrolysis` | `capa.experiment.profiles.capa_pyrolysis.validate_metadata` |
| `cone_calorimeter` | `capa.experiment.profiles.cone_calorimeter.validate_metadata` |
| anything else | exit 2 (unknown profile) |

### Output

Config with a CAPA profile:

```
$ uv run capa profile validate configs/experiments/sim_capa.yaml
OK: configs/experiments/sim_capa.yaml
  profile: capa.profile.capa_pyrolysis
  standards: ISO 5660-1, ASTM E1354
```

Config with no profile block — also a pass:

```
$ uv run capa profile validate configs/experiments/sim_freerun.yaml
OK: configs/experiments/sim_freerun.yaml (no domain_profile)
```

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Profile metadata validated, or config has no `domain_profile` block. |
| 2 | Config load failed, profile id is unknown, or profile metadata failed validation. |

---

## When to reach for it vs the others

| Situation | Command |
|---|---|
| "Are the CAPA-specific scientific fields (heat flux, sample mass, gas mixture, …) well-formed?" | `capa profile validate <yaml>` |
| "Will the experiment load at all?" | [`capa validate <yaml>`](capa-validate.md) |
| "Give me everything — structure, hardware, method, profile, in one report." | [`capa config validate <yaml>`](capa-config.md) |

`capa profile validate` is narrower than `capa config validate`: it skips structural and hardware checks and focuses on the scientific-metadata block. Use it for a fast pre-publish lint of the profile-only portion (e.g., "I just edited the heat-flux fields, is the YAML still valid?"); use `capa config validate` for the full layered report.

---

## Roadmap

The earlier stub for this page listed two commands that have not been implemented: `describe`, `dump <bundle>`.

- **`describe`** — print the profile's expected field schema. Workaround: look at the profile's metadata model (`CapaPyrolysisMetadata` and similar) or read [CAPA profile fields](../configuration/capa-profile.md).
- **`dump <bundle>`** — extract the profile snapshot from a sealed bundle. Workaround: `jq '.domain_profile' <bundle>/manifest.json` gives the active id and `standard_refs`; the full metadata snapshot lives under `<bundle>/profiles/<short_id>.toml`.

If either of these would be load-bearing in your workflow (e.g., a tool that needs structured profile output), open an issue.

---

## See also

- [CAPA profile fields](../configuration/capa-profile.md) — every scientific-metadata field
- [`capa validate`](capa-validate.md) — full experiment load
- [`capa config validate`](capa-config.md) — layered Problems-panel output
- [Manifest and schema](../bundles/manifest-and-schema.md) — where the profile block lands on disk
