"""Manual device-control panel UI (plan §10, handoff §1).

Per-device cards rendered into a single :class:`~capa.ui.docks.manual_control.ManualControlDock`,
gated reflectively on adapter / camera capability flags. Adapters are
acquired from the controller's shared :class:`~capa.devices.registry.DeviceRegistry`
so the bus is not re-opened on every run-arm cycle.
"""
