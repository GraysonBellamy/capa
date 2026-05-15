"""Manual device-control panel UI.

Per-device cards rendered into a single :class:`~capa.ui.docks.manual_control.ManualControlDock`,
gated reflectively on adapter / camera capability flags. Commands route
through the controller's shared :class:`~capa.runtime.dispatch.ManualClient`,
which targets the long-lived :class:`~capa.runtime.pool.WorkerPool`
between runs and the active :class:`~capa.runtime.conductor.Conductor`
during a run — so hardware is not re-opened on every run-arm cycle.
"""
