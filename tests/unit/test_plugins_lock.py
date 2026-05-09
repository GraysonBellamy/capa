from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from capa.core.plugins_lock import (
    DriftKind,
    InstalledPlugin,
    PluginEntry,
    PluginsLock,
    detect_drift,
)

HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64


def _entry(plugin_id: str, version: str = "1.0.0", hash: str = HASH_A) -> PluginEntry:
    return PluginEntry(
        id=plugin_id,
        package="capa",
        version=version,
        entry_point=f"capa.procedures:{plugin_id.rsplit('.', maxsplit=1)[-1]}",
        distribution_hash=hash,
    )


def _installed(
    plugin_id: str,
    version: str = "1.0.0",
    hash: str = HASH_A,
    entry_point: str | None = None,
) -> InstalledPlugin:
    return InstalledPlugin(
        id=plugin_id,
        package="capa",
        version=version,
        entry_point=entry_point or f"capa.procedures:{plugin_id.rsplit('.', maxsplit=1)[-1]}",
        distribution_hash=hash,
    )


class TestPluginEntry:
    def test_bad_entry_point_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PluginEntry(
                id="x",
                package="x",
                version="1.0",
                entry_point="no_colon",
                distribution_hash=HASH_A,
            )

    def test_bad_hash_algo_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PluginEntry(
                id="x",
                package="x",
                version="1.0",
                entry_point="x:Y",
                distribution_hash="md5:abc",
            )

    def test_empty_hash_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PluginEntry(
                id="x",
                package="x",
                version="1.0",
                entry_point="x:Y",
                distribution_hash="sha256:",
            )


class TestPluginsLock:
    def test_duplicate_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PluginsLock(plugins=(_entry("x"), _entry("x")))

    def test_load_fixture(self, configs_dir: Path) -> None:
        lock = PluginsLock.load(configs_dir / "experiments/example_plugins.lock")
        assert lock.version == 1
        assert len(lock.plugins) == 2
        ids = {p.id for p in lock.plugins}
        assert "capa.builtin.recipe_runner" in ids


class TestDriftDetection:
    def test_clean_install(self) -> None:
        lock = PluginsLock(plugins=(_entry("capa.recipe"),))
        installed = [_installed("capa.recipe")]
        assert detect_drift(lock, installed) == []

    def test_missing_from_install(self) -> None:
        lock = PluginsLock(plugins=(_entry("capa.recipe"),))
        drifts = detect_drift(lock, [])
        assert len(drifts) == 1
        assert drifts[0].kind == DriftKind.MISSING_FROM_INSTALL
        assert drifts[0].plugin_id == "capa.recipe"

    def test_missing_from_lock(self) -> None:
        lock = PluginsLock(plugins=())
        drifts = detect_drift(lock, [_installed("extra.thing")])
        assert len(drifts) == 1
        assert drifts[0].kind == DriftKind.MISSING_FROM_LOCK

    def test_version_mismatch(self) -> None:
        lock = PluginsLock(plugins=(_entry("capa.recipe", version="1.0.0"),))
        drifts = detect_drift(lock, [_installed("capa.recipe", version="2.0.0")])
        kinds = [d.kind for d in drifts]
        assert DriftKind.VERSION_MISMATCH in kinds

    def test_hash_mismatch(self) -> None:
        lock = PluginsLock(plugins=(_entry("capa.recipe", hash=HASH_A),))
        drifts = detect_drift(lock, [_installed("capa.recipe", hash=HASH_B)])
        kinds = [d.kind for d in drifts]
        assert DriftKind.HASH_MISMATCH in kinds

    def test_entry_point_mismatch(self) -> None:
        lock = PluginsLock(plugins=(_entry("capa.recipe"),))
        drifts = detect_drift(
            lock,
            [_installed("capa.recipe", entry_point="other:Class")],
        )
        kinds = [d.kind for d in drifts]
        assert DriftKind.ENTRY_POINT_MISMATCH in kinds

    def test_multiple_drifts_for_one_id(self) -> None:
        lock = PluginsLock(plugins=(_entry("capa.recipe", version="1.0", hash=HASH_A),))
        drifts = detect_drift(lock, [_installed("capa.recipe", version="2.0", hash=HASH_B)])
        kinds = [d.kind for d in drifts]
        assert DriftKind.VERSION_MISMATCH in kinds
        assert DriftKind.HASH_MISMATCH in kinds
