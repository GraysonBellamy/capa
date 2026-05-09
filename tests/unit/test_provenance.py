from __future__ import annotations

import json
from pathlib import Path

from capa.core.plugins_lock import PluginEntry, PluginsLock
from capa.core.provenance import (
    gather_capa,
    gather_lockfile,
    gather_packages_json,
    gather_platform,
    gather_plugins,
    gather_provenance,
    gather_python,
)


class TestGatherers:
    def test_python_block(self) -> None:
        block = gather_python()
        assert block.version
        assert block.implementation in {"CPython", "PyPy"}
        assert block.executable

    def test_platform_block(self) -> None:
        block = gather_platform()
        assert block.os
        assert block.machine
        assert block.node

    def test_capa_no_repo_root(self) -> None:
        block = gather_capa(repo_root=None)
        assert block.version
        # No git probe → both None
        assert block.git_sha is None
        assert block.git_dirty is None

    def test_capa_with_real_repo(self) -> None:
        block = gather_capa(repo_root=Path(__file__).resolve().parents[2])
        # Either we got real git data or the repo doesn't have git installed.
        if block.git_sha is not None:
            assert len(block.git_sha) == 40
            assert isinstance(block.git_dirty, bool)

    def test_lockfile_missing(self, tmp_path: Path) -> None:
        block, data = gather_lockfile(tmp_path / "no.lock")
        assert block.path is None
        assert block.sha256 is None
        assert data is None

    def test_lockfile_present(self, tmp_path: Path) -> None:
        src = tmp_path / "src.lock"
        src.write_text("hello")
        block, data = gather_lockfile(src)
        assert block.path == "env/uv.lock"
        assert block.sha256 == ("2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824")
        assert data == b"hello"

    def test_plugins_empty(self) -> None:
        assert gather_plugins(None) == ()

    def test_plugins_mirrored(self) -> None:
        lock = PluginsLock(
            plugins=(
                PluginEntry(
                    id="capa.builtin.free_run",
                    package="capa",
                    version="0.0.1",
                    entry_point="capa.procedures:FreeRun",
                    distribution_hash="sha256:abc",
                ),
            ),
        )
        out = gather_plugins(lock)
        assert len(out) == 1
        assert out[0].id == "capa.builtin.free_run"
        assert out[0].distribution_hash == "sha256:abc"

    def test_packages_json_is_sorted(self) -> None:
        data = gather_packages_json()
        rows = json.loads(data)
        assert isinstance(rows, list)
        if rows:
            names = [r["name"].lower() for r in rows]
            assert names == sorted(names)
            for row in rows:
                assert "name" in row and "version" in row


class TestTopLevel:
    def test_full_provenance_round_trip(self, tmp_path: Path) -> None:
        src_lock = tmp_path / "uv.lock"
        src_lock.write_bytes(b"version = 1\n")
        prov = gather_provenance(
            repo_root=None,
            lockfile_source=src_lock,
            plugins_lock=None,
        )
        assert prov.lockfile.path == "env/uv.lock"
        assert prov.lockfile_bytes == b"version = 1\n"
        assert prov.python.version
        assert isinstance(prov.packages_json_bytes, bytes)
