# capa

**Control and DAQ for a custom controlled-atmosphere pyrolysis lab
instrument** (cone-calorimeter-class). capa drives a heterogeneous
rig — NI-DAQ, Watlow heaters, Alicat mass-flow controllers, Sartorius
balances, USB and FLIR IR cameras — through async-first device
libraries, records every run as a self-contained on-disk **bundle**,
and treats research workflows (calibrations, custom routines) as
first-class plugins.

The project is named after the **C**ontrolled **A**tmosphere
**P**yrolysis **A**pparatus — the bench-scale gasification rig
originally introduced by Swann et al. (CAPA II, 2017) and revisited
in Bellamy's M.S. thesis (UMD, 2022). See [References](#references).

> 📚 **Full docs:** <https://capa.graysonbellamy.dev/>

## What capa is for

If you run pyrolysis or cone-calorimeter-class experiments and you
care about:

- a single GUI that drives every device on the rig from one config,
- a recorded *bundle* per run — config, method, calibration snapshot,
  events, samples, video — that you can re-open five years from now,
- async, per-resource workers so a stuck serial port can't take the
  whole rig down,
- plugins for custom procedures, profiles, device adapters, and sinks,

…capa is built for that workflow.

## Status

> ⚠️ **Pre-alpha.** Beta runtime, alpha UX, pre-alpha API.

The per-resource-worker runtime (`Conductor` + `WorkerPool`) is the
current production runtime; the older single-loop `ExperimentEngine`
has been removed. The public surface of `capa.runtime`, `capa.devices`,
and `capa.config` is **unstable** — imports, exception types, and
adapter contracts may change between commits without backward-compat
shims. **Pin to a specific commit if you are building against capa
today.**

See the [architecture docs](docs/architecture/runtime-architecture.md)
for the current runtime layer and the
[high-level plan](docs/architecture/capa-plan.md) for the product
shape (parts predating the runtime cutover are flagged stale).

## Three concepts that unlock the rest

- **Bundle** — a self-contained on-disk record of one run
  (`manifest.json`, `events.sqlite`, `scalars.parquet`, video
  sidecars). See [what's in a bundle](docs/bundles/what-is-a-bundle.md).
- **Channel** — a named quantity (`heater_pv`, `mfc_flow`) bound to a
  device parameter. See [channel bindings](docs/configuration/channel-bindings.md).
- **Procedure** — the class of experiment (free run, recipe runner,
  heat-flux tune, …). Procedures are plugins, not core code. See
  [what is a procedure](docs/procedures/what-is-a-procedure.md).

## Try it (no hardware needed)

Every device capa supports has a built-in simulator. A sealed bundle
in about five minutes:

```sh
uv sync --group dev
uv run capa gui configs/experiments/sim_freerun.yaml
```

In the GUI: switch to the **Run** tab, click **Start**, watch live
samples stream into the plot, then click **Stop run**. capa runs the
safe-shutdown step and seals the bundle into `runs/<timestamp>/`.

Full walkthrough: [Quick start](docs/getting-started/quick-start.md).

## Installation

Short version (Windows is the primary target; Linux works for most
adapters):

1. Install [uv](https://github.com/astral-sh/uv) and Python 3.13.
2. Clone capa **and its sibling device libraries** into the same
   parent directory:

   ```
   ~/git/
   ├── capa/          ← this repo
   ├── alicatlib/
   ├── watlowlib/
   ├── sartoriuslib/
   ├── nidaqlib/
   └── capa-flir/     ← optional, for FLIR IR cameras
   ```

3. From `capa/`:

   ```sh
   uv sync --group dev               # baseline + dev tooling
   uv sync --group dev --extra flir  # with FLIR IR cameras
   ```

4. Verify:

   ```sh
   uv run capa version
   uv run capa validate configs/experiments/sim_freerun.yaml
   ```

The Windows-only `duvc-ctl` cp313 wheel is vendored under
[`vendor/`](vendor/) because upstream hasn't published 3.13 wheels yet.

Full instructions, including the FLIR Atlas SDK and NI-DAQmx drivers:
see [Installation](docs/installation.md).

## Development

The dev loop is four commands, in order:

```sh
uv run ruff format       # format
uv run ruff check        # lint
uv run mypy              # type-check
uv run pytest            # test suite (excludes hardware-marked tests)
```

The full non-hardware suite completes in under a minute on a laptop.
Hardware-attached tests are opt-in via `CAPA_HARDWARE_TESTS=1` — see
[hardware tests](docs/contributing/hardware-tests.md).

More: [Dev setup](docs/contributing/dev-setup.md) ·
[Running tests](docs/contributing/running-tests.md) ·
[Code style](docs/contributing/code-style.md) ·
[Commits and PRs](docs/contributing/commit-and-pr.md).

## Where to go next

| If you're… | Start here |
|---|---|
| New and want a feel for it | [Quick start (simulator)](docs/getting-started/quick-start.md) |
| Setting up a real rig | [Your first real run](docs/getting-started/first-real-run.md) |
| Running experiments daily | [Operator handbook](docs/user-guide/operator-handbook.md) |
| Reading a finished bundle | [Reading bundles](docs/bundles/reading-bundles.md) |
| Writing a plugin | [Writing a procedure](docs/extending/writing-a-procedure.md) · [Plugin system](docs/extending/plugin-system.md) |
| Touching the source | [Runtime architecture](docs/architecture/runtime-architecture.md) · [Dev setup](docs/contributing/dev-setup.md) |

## References

The CAPA the project is named after has been described in:

- Swann, J. D., Ding, Y., McKinnon, M. B., & Stoliarov, S. I. (2017).
  *Controlled atmosphere pyrolysis apparatus II (CAPA II): A new tool
  for analysis of pyrolysis of charring and intumescent polymers.*
  Fire Safety Journal, 91, 130–139.
  <https://doi.org/10.1016/j.firesaf.2017.03.038>
- Bellamy, G. T. (2022). *Development and Improvements of the
  Controlled Atmosphere Pyrolysis Apparatus.* M.S. thesis, Department
  of Fire Protection Engineering, University of Maryland, College
  Park. <https://doi.org/10.13016/gzi8-ivaz>

If you use capa itself in academic work, see [CITATION.cff](CITATION.cff).

## License

MIT — see [LICENSE](LICENSE).
