"""Tests for :class:`capa.runtime.bundle_ref.BundleWriterRef`.

The ref must:

1. Satisfy the :class:`BundleRef` protocol (verified via ``isinstance``).
2. Expose the bundle path and run id from the underlying writer.
3. Be safely shared across threads (frozen, no mutable state).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from capa.runtime.bundle_ref import BundleWriterRef
from capa.runtime.runcontext import BundleRef


def test_satisfies_bundle_ref_protocol() -> None:
    ref = BundleWriterRef(bundle_path=Path("/tmp/runs/abc"), run_id="abc")
    assert isinstance(ref, BundleRef)


def test_root_returns_bundle_path() -> None:
    p = Path("/tmp/runs/run-001")
    ref = BundleWriterRef(bundle_path=p, run_id="run-001")
    assert ref.root == p
    assert isinstance(ref.root, Path)


def test_from_writer_uses_writer_fields() -> None:
    """Mirror :class:`RunBundleWriter`'s ``bundle_path`` / ``run_id`` props
    without needing a real bundle on disk."""
    fake_writer = SimpleNamespace(
        bundle_path=Path("/tmp/runs/r42"),
        run_id="r42",
    )
    ref = BundleWriterRef.from_writer(fake_writer)  # type: ignore[arg-type]
    assert ref.bundle_path == Path("/tmp/runs/r42")
    assert ref.run_id == "r42"
    assert ref.root == Path("/tmp/runs/r42")


def test_is_frozen() -> None:
    ref = BundleWriterRef(bundle_path=Path("/tmp/r"), run_id="r")
    with pytest.raises((AttributeError, TypeError)):
        ref.run_id = "other"  # type: ignore[misc]
