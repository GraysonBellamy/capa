from __future__ import annotations

from pathlib import Path

import pytest

from capa.storage.integrity import (
    IntegrityError,
    compute_manifest_sha256,
    format_manifest_lines,
    parse_manifest_lines,
    verify,
    write_manifest_sha256,
)


def _populate(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.toml").write_text("a = 1\n")
    (root / "manifest.json").write_text('{"x": 1}')
    sub = root / "device_records"
    sub.mkdir()
    (sub / "watlow.parquet").write_bytes(b"\x00\x01\x02")
    # Should be skipped (substring match on .in-flight. covers .arrows too)
    (sub / "watlow.in-flight.arrows").write_bytes(b"DEAD")


class TestCompute:
    def test_skips_inflight_and_self(self, tmp_path: Path) -> None:
        _populate(tmp_path)
        # Pre-existing manifest.sha256 — should be skipped from its own walk
        (tmp_path / "manifest.sha256").write_text("placeholder")
        digests = compute_manifest_sha256(tmp_path)
        assert "config.toml" in digests
        assert "manifest.json" in digests
        assert "device_records/watlow.parquet" in digests
        assert "device_records/watlow.in-flight.arrows" not in digests
        assert "manifest.sha256" not in digests

    def test_stable_order(self, tmp_path: Path) -> None:
        _populate(tmp_path)
        a = compute_manifest_sha256(tmp_path)
        b = compute_manifest_sha256(tmp_path)
        assert list(a.items()) == list(b.items())

    def test_not_a_directory(self, tmp_path: Path) -> None:
        with pytest.raises(IntegrityError):
            compute_manifest_sha256(tmp_path / "missing")


class TestRoundTrip:
    def test_format_and_parse(self) -> None:
        digests = {
            "config.toml": "a" * 64,
            "manifest.json": "b" * 64,
        }
        rendered = format_manifest_lines(digests)
        parsed = parse_manifest_lines(rendered)
        assert parsed == digests

    def test_parse_tolerates_binary_marker(self) -> None:
        line = "a" * 64 + " *config.toml\n"
        parsed = parse_manifest_lines(line)
        assert parsed == {"config.toml": "a" * 64}

    def test_parse_rejects_short_digest(self) -> None:
        with pytest.raises(IntegrityError):
            parse_manifest_lines("abc  config.toml\n")

    def test_parse_rejects_non_hex(self) -> None:
        with pytest.raises(IntegrityError):
            parse_manifest_lines(("z" * 64) + "  config.toml\n")


class TestVerify:
    def test_clean_round_trip_is_ok(self, tmp_path: Path) -> None:
        _populate(tmp_path)
        write_manifest_sha256(tmp_path)
        result = verify(tmp_path)
        assert result.status == "ok"
        assert not result.mismatches

    def test_mutation_is_detected(self, tmp_path: Path) -> None:
        _populate(tmp_path)
        write_manifest_sha256(tmp_path)
        (tmp_path / "config.toml").write_text("a = 2\n")
        result = verify(tmp_path)
        assert result.status == "mismatch"
        kinds = {m.kind for m in result.mismatches}
        assert "digest_mismatch" in kinds

    def test_extra_file_is_partial(self, tmp_path: Path) -> None:
        _populate(tmp_path)
        write_manifest_sha256(tmp_path)
        (tmp_path / "extra.txt").write_text("snuck in")
        result = verify(tmp_path)
        assert result.status == "partial"
        assert any(m.kind == "extra" for m in result.mismatches)

    def test_missing_file_is_partial(self, tmp_path: Path) -> None:
        _populate(tmp_path)
        write_manifest_sha256(tmp_path)
        (tmp_path / "config.toml").unlink()
        result = verify(tmp_path)
        assert result.status == "partial"
        assert any(m.kind == "missing" for m in result.mismatches)

    def test_no_manifest_raises(self, tmp_path: Path) -> None:
        _populate(tmp_path)
        with pytest.raises(IntegrityError):
            verify(tmp_path)
