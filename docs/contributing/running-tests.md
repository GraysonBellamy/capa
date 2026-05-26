---
description: Running capa test suite with `uv run pytest` — `unit/`, `integration/`, `smoke/`, `hardware/` tree, registered markers, async wiring, pytest-qt GUI tests.
---

# Running tests

**Audience:** contributors who have a working dev install (see [Dev setup](dev-setup.md)).
**Scope:** the test tree, the two registered markers, how async and GUI tests are wired, and what to run before pushing.

`uv run pytest` from the project root is the canonical command. It runs every test except the `hardware`-marked ones (which need real instruments and are described in [Hardware tests](hardware-tests.md)). The full suite completes in under a minute on a developer laptop.

---

## The test tree

```text
tests/
├── conftest.py        # session-wide fixtures (configs_dir, anyio_backend)
├── unit/              # hermetic per-module tests
├── integration/       # multi-component (UI smoke, headless run, crash recovery)
│   └── runtime/       # conductor + pool + worker integration
├── smoke/             # fast regression baselines (smoke marker)
└── hardware/          # real rig only (hardware marker, gated)
```

| Directory | What goes here | Default `uv run pytest`? |
|---|---|---|
| `tests/unit/` | One subject under test per file. No real I/O, no real subprocess. Use stub adapters from `tests/fixtures/`. | yes |
| `tests/integration/` | Multi-module flows: a full headless run via the Typer CLI, the runtime `Conductor` + `WorkerPool` against simulated workers, GUI smoke tests via `pytest-qt`. | yes |
| `tests/smoke/` | A small fixed set of behavioral baselines (`smoke` marker) pinning the shape of the runtime. Cheap to rerun after broad changes. | yes |
| `tests/hardware/` | Per-vendor smoke tests against real instruments. Each smoke module carries the `hardware` marker and an env-var skip unless `CAPA_HARDWARE_TESTS=1`. | **no** — needs opt-in |

The split between `unit/` and `integration/` is informal — there is no fixture-level enforcement. Prefer `unit/` if you can express the test with a stub adapter; promote to `integration/` when it needs `WorkerPool` or the full headless `run_headless` entry point.

---

## Markers

Only two markers are registered in [pyproject.toml](../../pyproject.toml#L233-L241):

```toml
markers = [
    "hardware: requires real hardware (skipped unless CAPA_HARDWARE_TESTS=1)",
    "smoke: thin behavioral baseline re-run after focused cleanup work",
]
```

`--strict-markers` is on, so any other `@pytest.mark.<foo>` will fail collection — a typo in a marker is a build break, not a silent skip. If you need a new marker, register it in `pyproject.toml` first.

| Marker | What it means | How to opt in |
|---|---|---|
| `hardware` | Test needs a real instrument. The smoke modules under `tests/hardware/` apply both the marker and the `CAPA_HARDWARE_TESTS=1` skip. Do not put `@pytest.mark.hardware` outside that directory. | `CAPA_HARDWARE_TESTS=1` |
| `smoke` | Fast behavioral baseline. Smoke baseline modules apply `pytestmark = pytest.mark.smoke`; run `uv run pytest -m smoke` for a quick regression check. | always runs; targetable via `-m smoke` |

`anyio` is not a marker registered in `pyproject.toml` — it comes from the AnyIO pytest plugin (see below).

---

## Async tests (AnyIO, not pytest-asyncio)

[tests/conftest.py](../../tests/conftest.py) wires up:

```python
pytest_plugins = ("anyio",)

@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
```

Every async test runs against asyncio. Unlike some sibling projects (e.g. `alicatlib`) that dual-test on both asyncio and trio, capa runs on asyncio only — the GUI is wired via `qasync`, which is asyncio-specific. Don't add a trio-only test.

Write async tests with `@pytest.mark.anyio`:

```python
@pytest.mark.anyio
async def test_open_close(self) -> None:
    adapter = MyAdapter(...)
    await adapter.open()
    try:
        ...
    finally:
        await adapter.close()
```

The fixture closes the cancel-scope cleanly even on failure; mirror the `try / finally` pattern above when an adapter is opened in a test so a failure halfway through doesn't leak a serial port to the next test.

---

## GUI tests (pytest-qt)

`qt_api = "pyside6"` is set in `pyproject.toml`. Use the `qtbot` fixture from `pytest-qt`:

```python
def test_main_window_renders(qtbot: Any, tmp_path: Path) -> None:
    window = MainWindow(...)
    qtbot.addWidget(window)
    qtbot.waitExposed(window)
    qtbot.waitUntil(lambda: window.numerics_dock is not None, timeout=1000)
```

`qtbot.waitUntil` is the right tool for "wait until the deferred-construction path lands"; don't `time.sleep` and don't poll without a timeout. The Qt event loop must be pumped, which is what `qtbot` does for you.

Pixel-level capture during a real running app (e.g. for documentation screenshots) is a separate tool — see [UI probe](screenshot-probe.md). It is **not** the test harness; `pytest-qt` is the canonical UI test path.

---

## Common invocations

```powershell
uv run pytest                          # full suite (excludes hardware)
uv run pytest -m smoke                 # fast regression check (~2 s)
uv run pytest tests/unit/test_clock.py # one file
uv run pytest -k saturation            # tests whose name matches
uv run pytest -x --ff                  # stop on first fail, run failures first
uv run pytest -q tests/integration     # quiet, integration tier only
```

`-q` is already the default via `addopts = ["-q", "--strict-markers", "--strict-config"]`.

`--strict-config` means an unknown key in `pyproject.toml`'s `[tool.pytest.ini_options]` fails collection. If you add an option there and tests stop collecting, that's why.

---

## What to run before pushing

```powershell
uv run ruff format --check
uv run ruff check
uv run mypy
uv run pytest
```

If you touched docs, also `uv run zensical build --clean`. There is no Python CI workflow gating pull requests today — these four commands are the contract (see [Commits and PRs](commit-and-pr.md) for the rationale).

Mypy is intentionally **not** an in-loop gate during feature work. Per the project preference, fix types in a dedicated pass at the end of the change rather than chasing them mid-task; pytest is the source-of-truth gate.

---

## Where to go next

- [Hardware tests](hardware-tests.md) — the `CAPA_HARDWARE_TESTS=1` tier and per-vendor env vars.
- [UI probe](screenshot-probe.md) — driving the live GUI from HTTP for screenshots and scripted UI walks.
- [Typing and mypy](typing-and-mypy.md) — how strict mode is configured.
