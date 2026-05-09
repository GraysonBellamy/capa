"""Shared test config."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest_plugins = ("anyio",)

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIGS_DIR = REPO_ROOT / "configs"


@pytest.fixture(scope="session")
def configs_dir() -> Path:
    return CONFIGS_DIR


@pytest.fixture
def anyio_backend() -> str:
    """Default to asyncio for every async test in capa."""
    return "asyncio"
