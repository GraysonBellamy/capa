"""Storage layer — the run bundle.

A run produces one directory containing every artifact needed to
interpret it later. The storage layer provides:

* :class:`~capa.storage.bundle.RunBundleWriter` — opens a run dir, owns sink
  lifecycle, drives the ``bundle_status`` state machine, writes ``manifest.json``
  at start and finalize.
* :mod:`capa.storage.channel_samples_sink` — normalized
  ``scalars.parquet`` long-table writer.
* :mod:`capa.storage.device_records_sink` — library-native
  ``device_records/<adapter>.parquet`` sidecars (one writer per family).
* :mod:`capa.storage.events_sink` / :mod:`capa.storage.status_sink` —
  transactional SQLite sinks for events and device snapshots.
* :mod:`capa.storage.log_sink` — JSON-lines append handle to ``run.log``.
* :mod:`capa.storage.finalize` — pure-function finalize-in-place: rewrite
  in-flight Arrow IPC streams to final Parquet with large row groups,
  compute ``manifest.sha256``, set ``bundle_status``.
* :mod:`capa.storage.integrity` — sha256 over every artifact, in
  ``sha256sum``-compatible format.
* :mod:`capa.storage.manifest` — Pydantic ``BundleManifest`` model.
* :mod:`capa.storage.schema` — ``BUNDLE_SCHEMA_VERSION`` and migration registry.
"""

from __future__ import annotations
