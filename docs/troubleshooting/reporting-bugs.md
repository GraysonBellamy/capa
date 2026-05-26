---
description: Filing actionable capa bug reports — capturing capa version, devices discover output, plugin lists, bundle extracts, and redacting sensitive manifest fields.
---

# Reporting bugs

**Audience:** all users.
**Scope:** what to capture before filing an issue, how to redact sensitive fields from a bundle, and where to file.

A good capa bug report includes three things: enough version detail to know which code you were running, a bundle or extract that reproduces the issue, and a minimal description of what you expected versus what you saw. This page lays out each.

---

## Step 1: capture version detail

Run `capa version`:

```
$ capa version
capa 0.X.Y (runtime <revision>)
```

That single line is the minimum every report needs. It identifies both the package version and the engine-task-group revision (recorded into every bundle's `manifest.capa.engine_version`). Mismatches between the two are themselves a useful diagnostic.

For deeper version detail, the bundle's `manifest.json` `capa`, `python`, and `platform` blocks carry everything the provenance gather captured at run start — git SHA (when capa is run from a source checkout), git-dirty flag, Python implementation and executable, OS version. Attach the manifest (after redaction — see Step 3) if version mismatch is plausibly relevant.

---

## Step 2: capture the environment

When the issue involves devices not opening, plugins not loading, or anything that fails before a bundle exists, include:

```powershell
# What capa sees in the active environment
capa devices discover
capa plugins list

# The Python environment itself
uv pip list
python --version
```

And, if relevant to the platform-specific path (FLIR, NI-DAQ, serial ports):

```powershell
# Which COM ports does the OS see?
[System.IO.Ports.SerialPort]::GetPortNames()

# Which NI-DAQ devices?
python -c "import nidaqmx.system; print(nidaqmx.system.System.local().devices.device_names)"
```

For pre-run failures, also grab the daily pre-run log:

```
~/.capa/logs/capa-YYYYMMDD.log
```

Anything before the bundle was created — config errors, plugin discovery failures, pool open errors — lands there. See [Reading event logs](reading-event-logs.md) for the broader log layout.

---

## Step 3: attach a redacted bundle (or a minimal extract)

A complete bundle is the single most useful attachment. It carries the manifest, every event, `run.log`, and the parquet samples — i.e. everything you'd otherwise paste in pieces.

**There is no `capa redact` CLI today.** Redaction is manual. Before sharing, edit the bundle's `manifest.json` to scrub or replace these fields, in order of how often they matter:

| Field | Why you might redact |
|---|---|
| `operator.id`, `operator.display_name` | Personal identifier. |
| `sample.id` and any other `sample.*` keys | Sample naming may encode supplier, project, or material identity. |
| `bundle_path` and any path that contains a user directory name | Common leak source on shared screenshots. |
| `custom.*` | Free-form metadata that may include lab-specific notes. |
| `procedure.id` / `domain_profile.id` | Only if the procedure or profile itself is proprietary; most are not. |

Capa does not currently embed credentials, network paths, or operator-typed notes in any other manifest field, so for most bug reports the above list is exhaustive.

The bundle directory also contains:

- `events.sqlite` — operator notes (kind `method.prompt.acknowledged`) may carry free-text the operator typed at run time. Either remove the database, or open it with any SQLite client and `DELETE` the rows of concern.
- `run.log` — JSON lines; the only PII is whatever was bound to the structlog context (operator id, run id). Grep-and-replace is safe.
- `scalars.parquet`, `device_records/`, `video/` — sensor data and frames. Sensitive only if the material itself or the camera framing is.

If the bundle is too large to attach to a GitHub issue (the cameras alone often push it past 10 MB), the minimum useful extract is:

- `manifest.json` (redacted)
- `events.sqlite`
- `run.log` (filtered to `WARNING`+ if you want it smaller)

That trio is enough to reconstruct the run's timeline and identify most runtime issues.

---

## Step 4: prefer a minimum repro on sim configs

If the bug reproduces against simulators, attach a simulator-driven repro. Sim configs live in `configs/sim*.toml`; the simulator adapters live in [`capa/devices/sim/`](https://github.com/GraysonBellamy/capa/tree/main/src/capa/devices/sim). The advantages are obvious: no physical hardware required to reproduce, no operator-specific paths, no sample data to redact.

A typical sim-repro recipe: copy the sim config that's closest to your setup, edit the procedure or specific device entry to trigger the bug, and re-run. Attach the resulting bundle alongside the config diff.

---

## Step 5: file the issue

GitHub: **<https://github.com/GraysonBellamy/capa/issues>**.

Suggested body template:

```markdown
### Environment
- capa version: <output of `capa version`>
- OS: <Windows 11 / Ubuntu 22.04 / etc.>
- Python: <output of `python --version`>
- Install method: <uv tool install / source checkout / wheel / etc.>

### What I did
1. <step>
2. <step>
3. <step>

### What I expected
<…>

### What happened
<…>

### Attached
- [ ] Redacted bundle (or minimal extract: manifest.json + events.sqlite + run.log)
- [ ] Config diff (if reproducing against a sim)
- [ ] Pre-run log (`~/.capa/logs/capa-YYYYMMDD.log`) — only if the issue is pre-bundle
```

A bug report with all five sections filled in is typically actionable the day it's filed. A report with just "capa crashed" requires three rounds of follow-up.

---

## What not to include

- **Full unredacted bundles from real material runs.** Once attached to a GitHub issue, the data is public.
- **Screenshots that include the capa window title bar's bundle path,** because that path often contains a username, project name, and sample id. Crop or redact.
- **Pasted log fragments without context.** A 50-line excerpt with no `t_mono_ns` or surrounding events is rarely actionable. Either attach the full `run.log` or extract the section *with* its timestamps and surrounding lines.

---

*See also: [Reading event logs](reading-event-logs.md), [Crash recovery](crash-recovery.md), [Common issues](common-issues.md).*
