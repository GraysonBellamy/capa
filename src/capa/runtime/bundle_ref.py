""":class:`BundleRef` over a real :class:`RunBundleWriter`.

Migration doc §3.7 step (c) and the :class:`~capa.runtime.runcontext.BundleRef`
protocol. The bundle ref is an opaque read-only handle that workers and
analyzers thread through diagnostic events so a reader can locate the bundle
without parsing surrounding context.

Workers never write to the bundle directly — that's the writer thread's job.
The ref exists for read-only attribution (e.g. an event's metadata may include
``"bundle_root": str(ctx.bundle.root)``).

Two construction paths:

* :meth:`BundleWriterRef.from_writer` — wrap an already-open
  :class:`RunBundleWriter` (the Conductor's normal path).
* Direct construction — for tests or for headless callers that haven't
  opened a bundle yet (path-only). Tests in :mod:`tests.integration.runtime`
  prefer the :class:`~tests.integration.runtime.fakes.FakeBundleRef` over
  this for in-memory cases.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from capa.storage.bundle import RunBundleWriter


@dataclass(frozen=True, slots=True)
class BundleWriterRef:
    """Production :class:`BundleRef` impl backed by a :class:`RunBundleWriter`.

    Carries the bundle's filesystem root and the run-id; both are constant
    after the writer opens. Frozen for safe cross-thread sharing.
    """

    bundle_path: Path
    run_id: str

    @classmethod
    def from_writer(cls, writer: RunBundleWriter) -> BundleWriterRef:
        """Build a ref from an already-open :class:`RunBundleWriter`.

        The writer must have been ``open()``-ed; otherwise its
        ``bundle_path`` is still set but the directory may not exist on
        disk. Conductor calls this immediately after
        :meth:`RunBundleWriter.open` returns.
        """
        return cls(bundle_path=writer.bundle_path, run_id=writer.run_id)

    @property
    def root(self) -> object:
        """:class:`pathlib.Path` of the bundle root.

        Typed as ``object`` by the protocol so fakes can return ``None``;
        the real implementation always returns a :class:`Path`. Callers
        that need a path should type-narrow with ``isinstance(root, Path)``.
        """
        return self.bundle_path


__all__ = ["BundleWriterRef"]
