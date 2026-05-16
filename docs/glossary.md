# Glossary

Plain-English definitions of capa's core concepts. Each entry is short on
purpose — the operator should be able to read the whole page once and walk
away with the mental model.

## Bundle

A self-contained on-disk record of a single run. Every bundle holds the
config, method, calibration snapshot, equipment manifest, events, sample
data, and any captured video. Five years from now, you should be able to
open a bundle and know exactly what was measured and how.

## Method

A scripted sequence of steps the rig executes during a run: hold a
setpoint, ramp to a target, prompt the operator, run a custom routine.
Methods are stored as `.method.toml` files and can be loaded into the
Method tab. A run with no method loaded is a **free run** — see below.

## Procedure

The class of experiment being performed (e.g. *CAPA cone calorimeter*,
*heat-flux gauge calibration*, *paint emissivity ramp*). Procedures are
plugins; each one declares what configuration it needs and how to
interpret the bundle.

## Profile (CAPA profile)

A curated table of configuration fragments specific to CAPA cone
calorimeter runs — atmosphere composition, specimen holder geometry,
heat-flux setpoint, leak-check provenance. The CAPA profile section only
appears when the procedure is `capa_cone`.

## Channel binding

How a channel gets its value. A channel like `heater_pv` doesn't store
data itself — it **reads from** a specific device parameter (e.g. a
Watlow controller's `PV` register). Each binding kind matches a device
family: `watlow_parameter`, `alicat_frame_field`, `sartorius_reading`,
`nidaq_reading_field`, `nidaq_block_channel`, or `derived` (computed
from other channels).

## Free run

A run with no method loaded. Recording starts when you press Start in
the Run tab. The heater holds whatever setpoint it had when the run
began; nothing in capa drives it. About 90% of CAPA experiments are
free runs — the dynamic-program case (ramps, multi-step) is the
minority.

## CAPA group

A logical grouping label attached to a channel via its metadata. CAPA
groups let the Profile section know which channels belong to which
physical role (`gas_inlet`, `heater_loop`, `specimen_load`) without
the operator having to wire up mappings by hand.

## Apply & Connect

The Setup-tab action that validates the current draft, opens hardware
connections to every device, and starts background acquisition. It is
the bridge between *editing a config* and *the rig being live*. Once
applied, the draft and the rig match — edits afterwards mark the draft
as having unsaved changes until you Apply again.

## Verify connection

A read-only handshake against every device declared in the draft. It
tells you whether the rig can be reached without actually opening
long-lived connections. Useful when troubleshooting a new device.

## Scan for devices

Walks each device family's bus / network / USB tree and reports what
hardware is physically reachable. Lets you add discovered devices to
the draft with one click instead of typing in addresses by hand.
