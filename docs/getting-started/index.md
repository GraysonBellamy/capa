---
description: Five short capa onboarding pages — installation, simulator quick start, your first real run, daily operator workflow, and the simulator profile tour.
---

# Get started

Five short pages that get you from a fresh install to a running rig.
Each is self-contained — pick the entry point that matches what's in
front of you.

<div class="grid cards" markdown>

-   :lucide-download:{ .lg .middle } &nbsp; **[Installation](../installation.md)**

    ---

    Python, `uv`, sibling device libraries, optional FLIR extras, and
    the vendored Windows `duvc-ctl` wheel. The highest-bounce-rate page
    — finish here before anything else makes sense.

-   :lucide-rocket:{ .lg .middle } &nbsp; **[Quick start (simulator)](quick-start.md)**

    ---

    Install → sealed bundle in about five minutes, no hardware needed.
    Every device referenced has a built-in simulator.

-   :lucide-cog:{ .lg .middle } &nbsp; **[Your first real run](first-real-run.md)**

    ---

    Same path, but on a real rig. Discovery, hardware-TOML editing,
    first connect, first sealed real bundle.

-   :lucide-calendar-check:{ .lg .middle } &nbsp; **[Daily operator workflow](daily-workflow.md)**

    ---

    The sustainable cadence — what to do at the start of a session,
    between runs, and at the end of the day.

-   :lucide-flask-conical:{ .lg .middle } &nbsp; **[The simulator profile](simulator-tour.md)**

    ---

    What the stock `sim_*.yaml` configs ship, what each simulated
    device emits, and how to exercise the engine without lighting
    anything up.

</div>

## What "done with Get started" looks like

You can sit at a clean PC and:

1. Install capa with `uv sync`.
2. Open the simulator config and finish a sealed run.
3. Edit the hardware TOML for a real rig, connect, and finish a sealed
   real run.

Once that's true, the next stop is the
[Operator handbook](../user-guide/operator-handbook.md) — the single-page
reference designed to live next to the rig.
