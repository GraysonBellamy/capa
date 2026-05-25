## Summary

<!-- 1–3 bullets: what changes and why. Link the design or docs section
     it implements, if any. -->

-
-

## Scope

- [ ] Touches a public API surface (`capa.runtime`, `capa.devices`, `capa.config`)
- [ ] Changes a device adapter (`capa.devices.<vendor>`)
- [ ] Changes runtime topology or shutdown sequence
- [ ] Changes the bundle schema or manifest format
- [ ] Touches safety-critical paths (saturation, authorization gate, shutdown, seal)
- [ ] Adds or changes a method-step kind or procedure

## Test plan

- [ ] `uv run ruff format --check`
- [ ] `uv run ruff check`
- [ ] `uv run mypy`
- [ ] `uv run pytest`
- [ ] Manual: launched `uv run capa gui configs/experiments/sim_freerun.yaml`, did X, observed Y
- [ ] Hardware smoke (if applicable): which adapter, which rig, paste assertion line

## Screenshots / artifacts

<!-- For UI changes, include before/after screenshots — the UI probe
     (docs/contributing/screenshot-probe.md) is built for this. For
     bundle-format changes, attach a sample manifest.json. -->

## Follow-ups

<!-- Anything intentionally unfinished. Link the issue if one exists. -->

-
