---
description: Single-page capa operator lookup — Setup, Run, and Method tabs, connection-strip states, status badges, and quick links to manual controls for pyrolysis runs.
---

# Operator handbook

**Audience:** trained rig operators during an active session.
**Scope:** the single-page lookup for "what does this button do / where
do I look / what should I click." Designed to print and tape next to
the rig.

Every entry below points at the page that owns the fuller explanation.
This page deliberately does not re-explain — when something surprises
you, follow the link.

---

## The three tabs — what they're for

| Tab | Use when |
|---|---|
| **[Setup](the-setup-tab.md)** | Loading or editing the config. Always between runs, never during. |
| **[Run](the-run-tab.md)** | Starting and watching a run. The live console. |
| **[Method](the-method-tab.md)** | Authoring a multi-step program. Usually empty for CAPA's common free-run case. |

---

## The connection strip (top of Setup tab)

[Full reference →](the-setup-tab.md#connection-strip)

| Dot | State | What you do |
|---|---|---|
| ○ gray | **No config loaded** | Open or pick from Recents. |
| ● green | **Connected — draft matches rig** | You're good. Switch to Run. |
| ◐ amber | **Draft has *N* unsaved edits** | Click **Apply & Connect**. |
| ◐ blue | **Connecting…** | Wait — opening hardware. |
| ◐ indigo | **Verifying connection…** | Wait — read-only handshake. |
| ✗ red | **Last apply failed** | Click **Details…**; fix; retry. |
| 🔒 purple | **Run in progress — config locked** | Edits refused. End the run first. |

---

## The state badge (top of Run tab)

[Full reference →](the-run-tab.md#the-state-badge)

| Badge | Colour | Meaning | Manual writes? |
|---|---|---|---|
| `Idle` | gray | No conductor | yes (to pool) |
| `Preparing…` | warn | Opening session, arming workers | **no** |
| `Running` | green | Samples flowing | yes (to conductor, recorded) |
| `Draining…` | warn | Workers disarming | **no** |
| `Finalizing…` | warn | Bundle being sealed | **no** |
| `Sealed` | green | Done; bundle ready | yes (to pool) |
| `Failed` | red | Preflight refused or finalize crashed | yes (to pool) |

The four warn states are "write-blocked" — the
[manual control dock](manual-controls.md) refuses commands.

---

## The status bar (bottom of every tab)

[Full reference →](status-bar-guide.md)

Left to right: `state` · `elapsed` · `UI overflow` · `sat` · `loop` · `q` · `safety queue` · `disk` · `cam` · `op` · `bundle`.

Healthy steady state:

```
RUNNING  00:00:42  UI overflow 0  sat ok  loop 8 ms  q 1/47  disk 87% free
```

If anything goes yellow or red:

| Pill | Red means | First check |
|---|---|---|
| **sat** | Producer blocked ≥ 5 s | [loop pill next](status-bar-guide.md#sat-saturation) |
| **loop** | Conductor loop ≥ 200 ms p99 | CPU-busy CustomStep |
| **q** | Bridge near capacity | About to saturate |
| **disk** | < 5% free | Free space immediately |

---

## The three buttons (Run tab header)

[Full reference →](the-run-tab.md#the-three-buttons)

| Button | Click | What happens |
|---|---|---|
| **Start** | Once | New run begins. Disabled until pool is open. |
| **Stop run** | Once | Graceful abort request → procedure cleanup → disarm → seal. Bundle is `aborted` + `sealed`. |
| **⛔ Emergency stop** | **Hold 1 s** | Immediate abort request → disarm → seal. Bundle is `aborted` + `sealed`. |

Both abort modes seal the bundle. Neither loses recorded data. See
[aborting safely](aborting-safely.md).

---

## Manual control cards

[Full reference →](manual-controls.md)

- Available cards depend on which devices declare manual capabilities.
- During `RUNNING`, commands route through the conductor and **are
  recorded** in the bundle.
- Between runs, commands go to the pool and **are not recorded** (they
  show up as `manual_event` in the events dock for audit).
- Destructive operations (internal cal, save-to-EEPROM, totalizer
  reset) show a confirmation dialog. Hit Yes deliberately.

---

## When to call which stop

[Full reference →](aborting-safely.md#what-you-should-do-by-situation)

| Situation | Click |
|---|---|
| Run on schedule, ending early | **Stop run** |
| Something looks off, rig responding | **Stop run** |
| Overshoot or leak suspected | **Emergency stop** |
| GUI unresponsive | Kill the process |
| Actively dangerous | **Physical e-stop**, then kill the process |

---

## Reviewing the sealed bundle

[Full reference →](reviewing-a-run.md)

- The Run tab's run-identity line shows `run: <id>  (<status>)`.
- The status bar's `bundle:` pill shows the path.
- **Help → Open logs folder** opens the runs root in your OS file
  browser.
- `manifest.json` is the index card; check `run_status`,
  `bundle_status`, `integrity.status` first.
- `scalars.parquet` is the data; `events.sqlite` is the log;
  `video/*.mkv` is visible video and `video/*.csq` is FLIR IR.

Expected post-abort manifest: `aborted` + `sealed` + `ok`. Anything else
warrants investigation.

---

## Common between-runs sequence

1. New sample in the holder.
2. Setup tab → **Operator & sample**: new sample id.
3. Setup tab → **CAPA Profile**: new mass.
4. **Apply && Connect** → wait for green.
5. Run tab → **Start**.
6. Watch status bar. Wait.
7. Run ends (naturally or via Stop). Badge → `Sealed`.
8. Repeat.

---

## When in doubt

- [Status bar guide](status-bar-guide.md) — every pill explained.
- [Aborting safely](aborting-safely.md) — when each stop is right.
- [Reviewing a run](reviewing-a-run.md) — confirming what happened.
- [Glossary](../glossary.md) — vocabulary.

If a button surprises you, the page you want is on this handbook
somewhere. If a *concept* surprises you, the page you want is the
[glossary](../glossary.md).
