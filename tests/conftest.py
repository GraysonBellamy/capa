"""Shared test config."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest_plugins = ("anyio",)

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIGS_DIR = REPO_ROOT / "configs"


def _nidaqmx_driver_available() -> bool:
    # The nidaqmx package loads NI-DAQmx's native DLL at import time; a missing
    # driver raises DaqNotFoundError (or OSError). CI runners don't ship it.
    try:
        import nidaqmx  # noqa: F401
    except Exception:
        return False
    return True


NIDAQMX_AVAILABLE = _nidaqmx_driver_available()

# Skip NI-dependent test modules entirely when the driver is missing — they
# import nidaqmx/nidaqlib (or capa.devices.nidaq, which transitively does) at
# module top, so they'd fail at collection before any test could run.
collect_ignore_glob: list[str] = []
if not NIDAQMX_AVAILABLE:
    collect_ignore_glob.extend(
        [
            "unit/test_nidaq_adapter.py",
            "unit/test_nidaq_channels.py",
            "unit/test_nidaq_join.py",
            "unit/test_adapter_resource_id.py",
            "unit/ui/forms/test_nidaq_channels_field.py",
            "unit/ui/tabs/test_setup_channels_nidaq.py",
        ]
    )


@pytest.fixture(scope="session")
def configs_dir() -> Path:
    return CONFIGS_DIR


@pytest.fixture
def anyio_backend() -> str:
    """Default to asyncio for every async test in capa."""
    return "asyncio"
