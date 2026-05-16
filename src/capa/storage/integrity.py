"""sha256 over every artifact in a bundle.

Catches bit-rot, partial copies, and post-hoc tampering. The
output file (``manifest.sha256``) follows the standard ``sha256sum`` line
format::

    <hex>  <relative-path>

so that a researcher can verify the bundle five years from now with
``sha256sum -c manifest.sha256`` and no capa install.

Walking is depth-first deterministic by sorted relative path. ``manifest.sha256``
itself is excluded — it cannot describe its own hash. ``*.in-flight.parquet``
files are excluded too: they only exist mid-write or in a crashed bundle, and
finalize unlinks them before integrity is computed (see §8.5).
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Literal

from capa.core.errors import CapaError

HASH_BUFFER = 65_536
"""Streaming chunk size. 64 KiB amortizes syscall overhead without hogging
RAM on large IR ``.csq`` (10–20 GiB) files."""

MANIFEST_FILENAME = "manifest.sha256"


class IntegrityError(CapaError):
    """Raised on integrity-related failures (re-walk vs. recorded mismatch,
    missing files, malformed manifest line)."""


def hash_file(path: Path) -> str:
    """Streaming sha256 of one file. Returns the hex digest."""
    digest = sha256()
    with open(path, "rb") as fp:
        while True:
            chunk = fp.read(HASH_BUFFER)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _iter_artifact_files(
    bundle_root: Path,
    *,
    skip_inflight: bool = True,
) -> list[Path]:
    """Return every regular file under ``bundle_root`` except the manifest
    file itself and any ``*.in-flight.*`` files.

    Sorted by relative POSIX path so the on-disk ``manifest.sha256`` is stable
    regardless of OS file-listing order. Symlinks are skipped — bundles are
    self-contained directories.
    """
    out: list[Path] = []
    for path in sorted(bundle_root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if path.name == MANIFEST_FILENAME and path.parent == bundle_root:
            continue
        if skip_inflight and ".in-flight." in path.name:
            continue
        out.append(path)
    return out


def compute_manifest_sha256(bundle_root: Path) -> dict[str, str]:
    """Walk ``bundle_root`` and return ``{relative_posix_path: hex_digest}``.

    Pure computation — does not write the manifest file. Use
    :func:`write_manifest_sha256` to also persist the result.
    """
    bundle_root = Path(bundle_root).resolve()
    if not bundle_root.is_dir():
        raise IntegrityError(f"bundle root is not a directory: {bundle_root}")
    digests: dict[str, str] = {}
    for path in _iter_artifact_files(bundle_root):
        rel = path.relative_to(bundle_root).as_posix()
        digests[rel] = hash_file(path)
    return digests


def format_manifest_lines(digests: dict[str, str]) -> bytes:
    """Render a ``{path: digest}`` mapping into the on-disk file format.

    Two-space separator matches ``sha256sum`` so the file is verifiable with
    the standard CLI without a capa install.
    """
    lines = [f"{digest}  {path}\n" for path, digest in sorted(digests.items())]
    return "".join(lines).encode("utf-8")


def parse_manifest_lines(data: bytes | str) -> dict[str, str]:
    """Inverse of :func:`format_manifest_lines`. Returns ``{path: digest}``.

    Strict: any line missing the two-space separator or the 64-hex digest
    raises :class:`IntegrityError`. Empty / whitespace-only lines are
    tolerated (text editors love adding trailing newlines).
    """
    text = data.decode("utf-8") if isinstance(data, bytes) else data
    out: dict[str, str] = {}
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        # The sha256sum format is "<hex><space><space><path>".
        # Tolerate "<hex> *<path>" (binary mode) and "<hex> <path>" (one space)
        # by splitting on the first run of whitespace.
        parts = stripped.split(None, 1)
        if len(parts) != 2:
            raise IntegrityError(f"manifest.sha256 line {lineno}: malformed line {line!r}")
        digest, path = parts
        if path.startswith("*"):
            path = path[1:]
        if len(digest) != 64 or any(c not in "0123456789abcdefABCDEF" for c in digest):
            raise IntegrityError(f"manifest.sha256 line {lineno}: invalid sha256 digest {digest!r}")
        out[path] = digest.lower()
    return out


def write_manifest_sha256(
    bundle_root: Path, digests: dict[str, str] | None = None
) -> dict[str, str]:
    """Compute (or re-use) digests and atomically write
    ``<bundle_root>/manifest.sha256``.

    Returns the digest dict. Atomic write is via ``.tmp`` + rename so a reader
    never sees a half-finished file.
    """
    bundle_root = Path(bundle_root)
    if digests is None:
        digests = compute_manifest_sha256(bundle_root)
    target = bundle_root / MANIFEST_FILENAME
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_bytes(format_manifest_lines(digests))
    tmp.replace(target)
    return digests


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FileMismatch:
    """One file whose recorded vs. recomputed digest disagrees, or that is
    missing/extra relative to the recorded manifest."""

    path: str
    kind: Literal["missing", "extra", "digest_mismatch"]
    expected: str | None
    actual: str | None


@dataclass(frozen=True, slots=True)
class VerifyResult:
    """Outcome of :func:`verify`. ``status`` mirrors the manifest's
    ``IntegrityStatus`` enum so callers can stamp the manifest directly."""

    status: Literal["ok", "mismatch", "partial"]
    """``ok`` = every file matches; ``mismatch`` = at least one file's digest
    is wrong; ``partial`` = files referenced in the manifest are missing or
    extra files exist not in the manifest."""
    mismatches: tuple[FileMismatch, ...]


def verify(bundle_root: Path) -> VerifyResult:
    """Re-walk ``bundle_root`` and compare against ``manifest.sha256``.

    Reads ``manifest.sha256`` rather than recomputing-and-comparing so a
    manifest written by an older capa is verifiable by a newer one without
    re-running the writer.
    """
    bundle_root = Path(bundle_root).resolve()
    manifest_path = bundle_root / MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise IntegrityError(f"bundle has no {MANIFEST_FILENAME}; nothing to verify against")
    recorded = parse_manifest_lines(manifest_path.read_bytes())
    actual = compute_manifest_sha256(bundle_root)

    mismatches: list[FileMismatch] = []

    # Files recorded but missing-or-wrong on disk.
    for path, expected_digest in recorded.items():
        if path not in actual:
            mismatches.append(
                FileMismatch(path=path, kind="missing", expected=expected_digest, actual=None)
            )
            continue
        if actual[path] != expected_digest:
            mismatches.append(
                FileMismatch(
                    path=path,
                    kind="digest_mismatch",
                    expected=expected_digest,
                    actual=actual[path],
                )
            )

    # Files on disk but not recorded.
    for path, actual_digest in actual.items():
        if path not in recorded:
            mismatches.append(
                FileMismatch(path=path, kind="extra", expected=None, actual=actual_digest)
            )

    if not mismatches:
        status: Literal["ok", "mismatch", "partial"] = "ok"
    elif any(m.kind == "digest_mismatch" for m in mismatches):
        status = "mismatch"
    else:
        status = "partial"
    return VerifyResult(status=status, mismatches=tuple(mismatches))


__all__ = [
    "HASH_BUFFER",
    "MANIFEST_FILENAME",
    "FileMismatch",
    "IntegrityError",
    "VerifyResult",
    "compute_manifest_sha256",
    "format_manifest_lines",
    "hash_file",
    "parse_manifest_lines",
    "verify",
    "write_manifest_sha256",
]
