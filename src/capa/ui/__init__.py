"""capa GUI — PySide6 + qasync, plan §10.

The UI never owns I/O. It talks to the engine via :class:`RunController`,
subscribes to the engine's :class:`~capa.core.databus.DataBus`, and reads
:class:`~capa.core.metrics.MetricsRegistry` for the status bar. Disk writes
remain on the engine's fan-out path; this layer is a viewport.
"""

from __future__ import annotations
