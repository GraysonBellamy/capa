"""Active-bundle checkpoint: durable record of an in-flight run.

A capa process that exits hard (the
:class:`~capa.ui.shutdown.ShutdownCoordinator` hits its wall-clock fuse,
the OS kills the process, the box loses power) leaves no in-process
signal behind. The catalog row inserted by
:meth:`~capa.runtime.session.RealRunSession.open` is the *intent* to run;
the manifest finalize is the *commitment*. Between those two points we
need a side-channel that the next launch can use to recognize "this
bundle was being recorded into when the previous capa died" — separate
from the catalog so a torn catalog write doesn't strand the recovery.

This module owns three things:

* :class:`ActiveCheckpoint` — the on-disk payload (atomic JSON).
* :func:`write_active_checkpoint` / :func:`delete_active_checkpoint` —
  the writer pair that :class:`RealRunSession` drives.
* :func:`recover_active_bundle_checkpoint` — the startup helper that
  reads the checkpoint, checks whether its PID is still alive, and (if
  the previous capa is gone) marks the bundle as crashed before clearing
  the checkpoint so the next launch starts clean.

The checkpoint lives at ``<runs_root>/.runtime-active.json``. Writes use
the temp-file + ``os.replace`` recipe so a torn write leaves either the
prior valid JSON or the new valid JSON — never a partial file.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import structlog

from capa.storage.manifest import BundleManifest

_logger = structlog.get_logger("capa.runtime.recovery")

CHECKPOINT_FILENAME: Final[str] = ".runtime-active.json"
"""Lives alongside the bundles inside ``runs_root`` so the recovery
helper has one obvious path to probe at startup."""


# Win32 process-liveness constants. Module-level because PEP 8 allows
# uppercase at module scope (function-scoped uppercase trips N806).
# Only referenced inside the ``os.name == "nt"`` branch of
# :func:`_pid_is_alive`, but defined unconditionally so static checkers
# see them on every platform.
_WIN_PROCESS_QUERY_LIMITED_INFORMATION: Final[int] = 0x1000
_WIN_STILL_ACTIVE: Final[int] = 259


@dataclass(frozen=True, slots=True)
class ActiveCheckpoint:
    """Snapshot of the currently-recording run.

    Written immediately after the bundle directory is created and the
    catalog row inserted (so the recovery helper has both a path and a
    catalog row to reconcile against). Cleared at clean finalize.
    """

    pid: int
    run_id: str
    bundle_path: Path
    config_path: Path | None
    started_utc: datetime
    last_update_utc: datetime

    def to_json(self) -> str:
        """Serialize to the on-disk JSON layout (indented, stable key order)."""
        return json.dumps(
            {
                "pid": self.pid,
                "run_id": self.run_id,
                "bundle_path": str(self.bundle_path),
                "config_path": str(self.config_path) if self.config_path else None,
                "started_utc": self.started_utc.isoformat(),
                "last_update_utc": self.last_update_utc.isoformat(),
            },
            indent=2,
        )

    @classmethod
    def from_json(cls, payload: str) -> ActiveCheckpoint:
        """Parse a checkpoint from its JSON serialization. Inverse of :meth:`to_json`."""
        data = json.loads(payload)
        config_raw = data.get("config_path")
        return cls(
            pid=int(data["pid"]),
            run_id=str(data["run_id"]),
            bundle_path=Path(data["bundle_path"]),
            config_path=Path(config_raw) if config_raw else None,
            started_utc=datetime.fromisoformat(data["started_utc"]),
            last_update_utc=datetime.fromisoformat(data["last_update_utc"]),
        )


# ---------------------------------------------------------------------------
# Atomic write/read/delete
# ---------------------------------------------------------------------------


def _checkpoint_path(runs_root: Path) -> Path:
    return runs_root / CHECKPOINT_FILENAME


def _atomic_write(path: Path, data: bytes) -> None:
    """Temp file in the same directory, fsync, ``os.replace``.

    Same-volume guarantee gives us a true atomic swap — a torn write
    leaves either the prior file or the new file, never a partial one.
    """
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def write_active_checkpoint(runs_root: Path, checkpoint: ActiveCheckpoint) -> None:
    """Atomically write the checkpoint to ``<runs_root>/.runtime-active.json``.

    Idempotent. The :class:`RealRunSession` calls this once at
    :meth:`~capa.runtime.session.RealRunSession.open` time and may call
    it again from :meth:`set_outcome` paths to refresh the
    ``last_update_utc`` field, though the load-bearing write is the open
    one (everything else is best-effort).
    """
    runs_root.mkdir(parents=True, exist_ok=True)
    _atomic_write(_checkpoint_path(runs_root), checkpoint.to_json().encode("utf-8"))


def read_active_checkpoint(runs_root: Path) -> ActiveCheckpoint | None:
    """Return the current checkpoint, or ``None`` if absent / unreadable.

    A corrupted checkpoint is logged and treated as absent rather than
    raising — the startup recovery path must never block app launch on a
    decode error. The atomic-write recipe makes corruption rare (it
    would require an OS-level partial flush), so this is mostly a
    defense against an operator manually editing the file.
    """
    path = _checkpoint_path(runs_root)
    if not path.exists():
        return None
    try:
        return ActiveCheckpoint.from_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError) as exc:
        _logger.warning(
            "recovery.checkpoint.unreadable",
            path=str(path),
            error=str(exc),
        )
        return None


def delete_active_checkpoint(runs_root: Path) -> None:
    """Remove the checkpoint. Idempotent; missing file is a no-op.

    Called by :class:`~capa.runtime.session.RealRunSession.close` after
    the catalog row is updated, and by :func:`recover_active_bundle_checkpoint`
    after a dead-PID checkpoint has been reconciled.
    """
    path = _checkpoint_path(runs_root)
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        _logger.warning(
            "recovery.checkpoint.delete_failed",
            path=str(path),
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# Startup recovery
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    """Outcome of one :func:`recover_active_bundle_checkpoint` call.

    ``status`` enumerates the cases:

    * ``"absent"`` — no checkpoint file. Normal clean-shutdown path.
    * ``"reconciled"`` — checkpoint existed, owning PID is dead, the
      bundle was marked crashed and the checkpoint cleared.
    * ``"live_owner"`` — checkpoint existed and its PID is still
      running. Another capa instance owns the bundle; we left it alone.
    * ``"missing_bundle"`` — checkpoint existed but the bundle path is
      gone (operator deleted it). The checkpoint was cleared without
      manifest work.
    """

    status: str
    checkpoint: ActiveCheckpoint | None
    error: str | None = None


def _pid_is_alive(pid: int) -> bool:
    """Best-effort liveness check that is cross-platform safe.

    On POSIX, ``os.kill(pid, 0)`` raises ``ProcessLookupError`` for a
    dead PID. On Windows, ``os.kill`` is a hard kill; instead we open
    the process handle. Both branches catch every error and return
    ``False`` so a permission denial doesn't masquerade as a live
    process and strand the checkpoint indefinitely.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        return _pid_is_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we can't signal it. Treat as alive — the
        # safer default is "leave the checkpoint alone."
        return True
    except OSError:
        return False
    return True


def _pid_is_alive_windows(pid: int) -> bool:
    """Windows branch of :func:`_pid_is_alive`.

    Uses ``OpenProcess`` with the minimum-rights flag plus
    ``GetExitCodeProcess`` to distinguish a dead PID (``OpenProcess``
    returns NULL) from a live one (exit code is ``STILL_ACTIVE``).
    ``ctypes`` is imported lazily so non-Windows platforms don't pay
    the import cost.
    """
    import ctypes  # noqa: PLC0415 — Windows-only path
    from ctypes import wintypes  # noqa: PLC0415

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    handle = kernel32.OpenProcess(_WIN_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == _WIN_STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def recover_active_bundle_checkpoint(runs_root: Path) -> RecoveryResult:
    """Reconcile an orphaned active-bundle checkpoint at startup.

    Called once on GUI / headless launch after the catalog has been
    opened but before any new run can start. The contract:

    * No checkpoint → no-op.
    * Checkpoint with live owning PID → no-op (other capa instance).
    * Checkpoint with dead owning PID → mark the bundle's manifest as
      crashed (best-effort; the manifest may already be sealed or may
      not exist if the previous capa died before the writer thread
      stamped anything beyond the initial open). Then delete the
      checkpoint.

    The catalog row is handled separately by
    :meth:`RunCatalog.flip_orphans` (which the GUI bootstrap calls
    immediately before this helper) — that flips any ``running`` row to
    ``crashed`` without needing the checkpoint. This helper exists for
    the rarer case where the manifest itself needs a recovery marker so
    bundle-only consumers (a CLI that reads ``manifest.json`` without
    consulting the catalog) see the crash status too.
    """
    checkpoint = read_active_checkpoint(runs_root)
    if checkpoint is None:
        return RecoveryResult(status="absent", checkpoint=None)

    if _pid_is_alive(checkpoint.pid):
        _logger.info(
            "recovery.checkpoint.live_owner",
            pid=checkpoint.pid,
            run_id=checkpoint.run_id,
        )
        return RecoveryResult(status="live_owner", checkpoint=checkpoint)

    bundle_path = checkpoint.bundle_path
    manifest_path = bundle_path / "manifest.json"
    error: str | None = None
    if not bundle_path.exists() or not manifest_path.exists():
        _logger.warning(
            "recovery.checkpoint.missing_bundle",
            run_id=checkpoint.run_id,
            bundle_path=str(bundle_path),
        )
        delete_active_checkpoint(runs_root)
        return RecoveryResult(status="missing_bundle", checkpoint=checkpoint)

    try:
        manifest = BundleManifest.read(manifest_path)
        if manifest.run_status == "running":
            updated = manifest.model_copy(
                update={
                    "run_status": "crashed",
                    "ended_utc": datetime.now(UTC),
                    "exit_reason": (
                        f"recovered after hard exit (pid {checkpoint.pid} did not finalize)"
                    ),
                }
            )
            updated.write(manifest_path)
            _logger.info(
                "shutdown.bundle_checkpoint_recovered",
                run_id=checkpoint.run_id,
                bundle_path=str(bundle_path),
                pid=checkpoint.pid,
            )
        else:
            _logger.info(
                "recovery.checkpoint.already_finalized",
                run_id=checkpoint.run_id,
                bundle_status=manifest.bundle_status,
                run_status=manifest.run_status,
            )
    except Exception as exc:
        error = str(exc)
        _logger.warning(
            "recovery.checkpoint.manifest_update_failed",
            run_id=checkpoint.run_id,
            bundle_path=str(bundle_path),
            error=error,
        )

    delete_active_checkpoint(runs_root)
    return RecoveryResult(status="reconciled", checkpoint=checkpoint, error=error)


__all__ = [
    "CHECKPOINT_FILENAME",
    "ActiveCheckpoint",
    "RecoveryResult",
    "delete_active_checkpoint",
    "read_active_checkpoint",
    "recover_active_bundle_checkpoint",
    "write_active_checkpoint",
]
