# Docs authoring plan

A staged plan for filling in the 108 stub pages under `docs/`.

## Why this order

Four principles drive the sequencing:

1. **Foundations before consumers.** Pages other pages link to (glossary,
   file formats, "what is a bundle") get written first so later pages
   can link instead of re-defining terms inline.
2. **Operator path before plugin authors.** A user must be able to run
   capa from a clean install before we owe contributors a plugin guide.
3. **Capture volatile knowledge early.** The deeper into a subsystem,
   the harder it is to re-derive. Architecture deep-dives and
   safety/saturation pages should be written while the implementation
   choices are fresh.
4. **Defer what mkdocstrings handles.** API pages render from source
   docstrings. The leverage point is improving docstrings in
   `src/capa/`, not authoring `docs/api/*.md`.

Estimates assume ~30–60 min per stub once you sit down — most stubs
already have their audience/scope/will-cover skeleton.

---

## Phase 0 — Polish the migrated content (≈ 2 h)

These already have real prose; they need light edits to fit the new IA.

- [ ] `docs/index.md` — written; sanity-check the "if you are…" table after a few real pages land.
- [ ] `docs/installation.md` — replace stub with real content (the highest-bounce-rate page; people abandon if install fails).
- [ ] `docs/glossary.md` — already exists (93 lines). Add entries for terms introduced after it was written: *worker*, *conductor*, *bridge*, *bundle outcome*, *saturation deadline*, *plugin lockfile*.
- [ ] `docs/getting-started/quick-start.md` — already real prose. Re-read for new-IA cross-links.
- [ ] `docs/user-guide/status-bar-guide.md` — already real prose. Verify the migrated `../../src/...` links resolve in the built site.
- [ ] `docs/architecture/capa-plan.md` — already real prose. Mark the "stale" sections explicitly (the README already warns).
- [ ] `docs/architecture/runtime-architecture.md` — already real prose. Verify cross-link from `capa-plan.md` works.

---

## Phase 1 — Foundations everything else links to (10 pages, ≈ 6 h)

Write these next so subsequent pages can `[link instead of re-defining](…)`.

- [ ] `docs/configuration/overview.md` — the four config kinds (.toml vs .yaml), how they compose.
- [ ] `docs/configuration/experiment-yaml.md` — every field of the experiment YAML.
- [ ] `docs/configuration/hardware-toml.md` — every device entry kind.
- [ ] `docs/configuration/method-toml.md` — every step kind in the file format.
- [ ] `docs/configuration/channel-bindings.md` — binding kinds and how records become samples.
- [ ] `docs/bundles/what-is-a-bundle.md` — top-level tour of the on-disk layout.
- [ ] `docs/bundles/manifest-and-schema.md` — every key in `manifest.json`.
- [ ] `docs/procedures/what-is-a-procedure.md` — procedure vs method vs profile.
- [ ] `docs/reference/file-formats.md` — single page that points at every on-disk format.
- [ ] `docs/devices/overview.md` — adapter contract, resource grouping, emission shapes.

**Gate before Phase 2:** any page from later phases that needs a cross-reference can now link instead of inline-explain.

---

## Phase 2 — Operator path (10 pages, ≈ 8 h)

Get a fresh user from install → first successful sealed bundle without
having to ask you in person.

- [ ] `docs/installation.md` — full real prose (Phase 0 marked this for replacement; this is where it actually happens).
- [ ] `docs/getting-started/first-real-run.md` — most-asked walk-through.
- [ ] `docs/getting-started/daily-workflow.md` — sustainable cadence.
- [ ] `docs/user-guide/operator-handbook.md` — single-page operator reference.
- [ ] `docs/user-guide/the-setup-tab.md` — the most surface-area UI page.
- [ ] `docs/user-guide/the-run-tab.md`.
- [ ] `docs/user-guide/the-method-tab.md`.
- [ ] `docs/user-guide/manual-controls.md`.
- [ ] `docs/user-guide/aborting-safely.md`.
- [ ] `docs/user-guide/reviewing-a-run.md`.

**Gate before Phase 3:** a non-Grayson operator can run capa
unsupervised on a known-good rig. From here on, you can hand the rig
to a new student with a docs link instead of a sit-down.

---

## Phase 3 — Safety contract (5 pages, ≈ 4 h)

Owed *before* you encourage anyone outside the immediate team to use
capa. Also the highest "if I forget I'll have to re-derive it from the
code" cost — write these while authorization gates and saturation
deadlines are still fresh.

- [x] `docs/safety/principles.md` — set the philosophy first.
- [x] `docs/safety/authorization-gates.md`.
- [x] `docs/safety/destructive-operations.md`.
- [x] `docs/safety/shutdown-sequence.md`.
- [x] `docs/safety/saturation-and-deadlines.md` — **highest volatility-recovery cost**; the design choices here (10 s deadline, escalation thresholds, monitor cadence) live nowhere else.

---

## Phase 4 — Bundle deep-dive (7 pages, ≈ 6 h)

Unblocks every downstream tool, notebook, and analyst.

- [ ] `docs/bundles/parquet-channel-samples.md` — long-format schema, units, time bases.
- [ ] `docs/bundles/parquet-device-records.md` — per-device native shapes.
- [ ] `docs/bundles/events-sqlite.md` — tables and event taxonomy.
- [ ] `docs/bundles/video.md` — visible vs IR layout, frame-to-monoclock sidecar.
- [ ] `docs/bundles/integrity-and-sealing.md` — sealing protocol, outcome states.
- [ ] `docs/bundles/bundle-versioning.md` — schema bump policy.
- [ ] `docs/bundles/reading-bundles.md` — recipes (polars, sqlite3, video extraction).

**Gate before Phase 5:** an analyst can interpret a bundle without
reading capa source.

---

## Phase 5 — Per-device + discovery (9 pages, ≈ 6 h)

Mostly parallels of each other. **Safe to delegate** — each page is
bounded and the sibling `*lib` docs (alicatlib in particular) provide a
template.

- [ ] `docs/devices/watlow.md`.
- [ ] `docs/devices/alicat.md`.
- [ ] `docs/devices/sartorius.md` — call out the cold-open race.
- [ ] `docs/devices/nidaq.md` — polled vs block mode.
- [ ] `docs/devices/cameras-webcam.md`.
- [ ] `docs/devices/cameras-flir.md`.
- [ ] `docs/devices/simulators.md`.
- [ ] `docs/devices/discovery.md`.
- [ ] `docs/user-guide/camera-preview.md` — sits with cameras conceptually; pair with the two camera pages.

---

## Phase 6 — Procedures, method steps, calibration (10 pages, ≈ 8 h)

CAPA-specific scientific content. Tribal knowledge that ONLY lives in
your head — high recovery cost. Write while context is fresh.

- [x] `docs/procedures/builtin-free-run.md`.
- [x] `docs/procedures/builtin-recipe-runner.md`.
- [x] `docs/procedures/builtin-batch.md`.
- [x] `docs/procedures/builtin-heat-flux-tune.md`.
- [x] `docs/procedures/method-steps-reference.md`.
- [x] `docs/calibration/overview.md`.
- [x] `docs/calibration/calibration-sets.md`.
- [x] `docs/calibration/tuning-workflow.md`.
- [x] `docs/calibration/heat-flux-tune-procedure.md`.
- [x] `docs/calibration/tune-artifacts.md`.
- [x] `docs/configuration/capa-profile.md` — CAPA-specific scientific fields. Belongs in this phase even though it's filed under Configuration.
- [x] `docs/configuration/calibrations.md` — on-disk format of calibration sets.

---

## Phase 7 — CLI surface (13 pages, ≈ 5 h)

Almost mechanical. **Safe to delegate**; each page can be drafted by
reading `src/capa/cli/<name>.py` and the Typer help output.

- [ ] `docs/cli/overview.md` first (dispatcher + sub-apps map).
- [ ] `docs/cli/capa-run.md`, `capa-gui.md`, `capa-validate.md`, `capa-finalize.md`.
- [ ] `docs/cli/capa-devices.md`, `capa-hardware.md`, `capa-config.md`.
- [ ] `docs/cli/capa-catalog.md`, `capa-plugins.md`.
- [ ] `docs/cli/capa-method.md`, `capa-profile.md`.
- [ ] `docs/cli/headless-runs.md` — process model, signals, exit codes.

---

## Phase 8 — Architecture deep-dives (5 pages, ≈ 8 h)

Companions to the long `architecture/runtime-architecture.md`. **High
volatility-recovery cost** — the threading model, write path, and
channel pipeline are non-obvious and rely on choices made over months.
Capture before the next refactor obscures them.

- [x] `docs/architecture/threading-model.md`.
- [x] `docs/architecture/data-flow.md`.
- [x] `docs/architecture/channel-pipeline.md`.
- [x] `docs/architecture/bundle-write-path.md`.
- [x] `docs/architecture/ui-runtime-boundary.md`.

---

## Phase 9 — Troubleshooting + diagnostics (6 pages, ≈ 5 h)

Write *after* a real user has hit issues — you'll know what actually
comes up. Until then, fill in skeletally from your own bug-fix history.

- [ ] `docs/user-guide/diagnostics-dock.md`.
- [ ] `docs/troubleshooting/common-issues.md`.
- [ ] `docs/troubleshooting/status-bar-symptoms.md`.
- [ ] `docs/troubleshooting/reading-event-logs.md`.
- [ ] `docs/troubleshooting/crash-recovery.md`.
- [ ] `docs/troubleshooting/reporting-bugs.md`.

---

## Phase 10 — Extending capa (7 pages, ≈ 8 h)

Owed before declaring the plugin system "supported." Each page is a
medium-effort tutorial; **partially delegatable** but the
authorization/saturation contract must come from you.

- [ ] `docs/extending/plugin-system.md` first (entry-points, registries, lockfile relationship).
- [ ] `docs/extending/writing-a-procedure.md` — most-requested.
- [ ] `docs/extending/writing-a-device-adapter.md` — most-impactful.
- [ ] `docs/extending/writing-a-profile.md`.
- [ ] `docs/extending/custom-method-steps.md`.
- [ ] `docs/extending/custom-sinks.md`.
- [ ] `docs/extending/plugin-lockfile.md`.

---

## Phase 11 — Contributing (7 pages, ≈ 4 h)

Easy to copy-paste-adapt from `alicatlib`/`watlowlib` conventions. **Safe
to delegate** entirely.

- [ ] `docs/contributing/dev-setup.md`.
- [ ] `docs/contributing/running-tests.md`.
- [ ] `docs/contributing/hardware-tests.md`.
- [ ] `docs/contributing/code-style.md`.
- [ ] `docs/contributing/typing-and-mypy.md`.
- [ ] `docs/contributing/commit-and-pr.md`.
- [ ] `docs/contributing/release-process.md`.

---

## Phase 12 — Reference tables + changelog (3 pages, ≈ 2 h)

Mostly mechanical. **Safe to delegate.** Changelog could just point at
GitHub releases.

- [ ] `docs/reference/environment-variables.md`.
- [ ] `docs/reference/exit-codes.md`.
- [ ] `docs/reference/changelog.md` (or replace with a redirect to GitHub releases).

---

## Phase 13 — API reference (ongoing, in source)

No `docs/api/*.md` content to write — those files contain only `:::
capa.foo` directives. The work is in source docstrings.

- [ ] Audit `src/capa/runtime/__init__.py`, `capa.devices/__init__.py`, `capa.config/__init__.py` for module docstrings.
- [ ] Run `uv run zensical build` and look at the rendered API pages — the gaps are obvious in the rendered output.
- [ ] Pick one subpackage per week to bring docstring coverage to "every public class and function has a Google-style docstring."

---

## Quick triage matrix

| Page kind | Volatility-recovery cost | Delegatable? | Phase |
|---|---|---|---|
| Glossary, file formats | low (mechanical) | yes | 1, 12 |
| Operator UI pages | medium | partially (you screenshot, contributor writes) | 2 |
| Safety pages | **high** | no — only you have the context | 3 |
| Bundle schema pages | low (mechanical, code is the source of truth) | yes | 4 |
| Per-device pages | low (parallel to *lib repos) | yes | 5 |
| CAPA scientific pages (profile, calibration, heat-flux tune) | **highest** | no | 6 |
| CLI per-command pages | low (Typer help + src skim) | yes | 7 |
| Architecture deep-dives | **high** | no | 8 |
| Plugin authoring | medium-high (you set the contract) | partially | 10 |
| Contributing | low | yes | 11 |
| API auto-gen | n/a — improve source docstrings | no | 13 |

## Suggested parallelism

If you ever delegate to another contributor or run agent passes:

- **Self-only phases** (do not delegate): 3, 6, 8, 10's first page.
- **Pair-with-screenshots phases** (you provide screenshots/feedback; contributor writes): 2, 5's camera pages, 9.
- **Fully-delegatable phases**: 4, 5 (most), 7, 11, 12.

## Recommended weekly cadence

Roughly 6–10 pages per week sustained → ~12 weeks to clear the
backlog. If aiming for a public docs launch:

- **Week 1:** Phase 0 + Phase 1 (foundations).
- **Weeks 2–3:** Phase 2 (operator path).
- **Week 4:** Phase 3 (safety). **← public-launch-ready milestone.**
- **Weeks 5–6:** Phases 4 + 5.
- **Weeks 7–9:** Phases 6 + 7 + 8.
- **Weeks 10–11:** Phases 9 + 10.
- **Week 12:** Phases 11 + 12.
- **Ongoing:** Phase 13 docstring sweeps.
