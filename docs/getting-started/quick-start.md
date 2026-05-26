---
description: From fresh capa install to a sealed simulator run bundle in five minutes — Setup tab, simulated Watlow and Alicat devices, Run tab, first sealed bundle.
---

# Quick start — your first run on the simulator

This walkthrough takes you from a fresh capa install to a sealed run
bundle in about five minutes. No real hardware needed — every device
referenced here has a built-in simulator.

## 1. Launch capa

```
uv run capa gui
```

You should see the welcome screen with three cards: **New setup**,
**Open config**, **Try a simulator**.

## 2. Open the simulator config

Click **Try a simulator**. capa loads `configs/experiments/sim_freerun.yaml`,
a stock free-run config with one simulated Watlow heater and one
simulated Alicat mass-flow controller.

The Setup tab opens. The connection strip at the top shows
`Connected — draft matches rig`. The status bar pills at the bottom
start showing live values for `sat`, `loop`, and `q` — the simulator is
already streaming samples.

## 3. (Optional) Edit a setpoint

Click the **Experiment** entry in the outline. Find **Heater setpoint
\[°C\]** — change it from `25` to `50`. The connection strip now reads
`Connected — draft has 1 unsaved edit` and the **Apply & Connect**
button lights up.

Click **Apply & Connect**. The strip cycles to `Connecting…` for a
moment, then back to `Connected — draft matches rig`.

## 4. Start the run

Switch to the **Run** tab. The state badge says `● Idle · Free run`.
The plot pane shows axes and channel legends but no data yet.

Click **Start**. The state badge flips to `Running`, the elapsed timer
ticks up, and the plot starts drawing live samples from the simulators.

## 5. Stop the run

Click **Stop run**. capa runs the safe-shutdown step and seals the
bundle. The badge shows `Sealed` and the status bar's `bundle:` label
displays the new bundle path.

## 6. Find the bundle

Open **Help → Open logs folder**. The bundle lives under
`runs/<timestamp>/` — `manifest.json` has the metadata, `events.sqlite`
has the event log, and `scalars.parquet` has the channel data.

## Next steps

- Open the **Method** tab and click **+ Add a hold step** to script a
  multi-step run.
- Read the [glossary](../glossary.md) for short definitions of *bundle*,
  *method*, *channel binding*, and other capa-specific terms.
- The [status bar guide](../user-guide/status-bar-guide.md) explains
  every pill in the diagnostic strip — `sat`, `loop`, `q`, etc.
