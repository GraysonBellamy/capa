""":class:`DiscoveryDialog` — adapter scan + "Add as device".

The Discover button on the Setup toolbar opens this dialog. It runs
each discoverable adapter's module-level discovery coroutine
against the local machine, aggregates the results into a table, and
offers one ``[Add]`` button per row. Adding a row emits a
:attr:`entryAdded` signal carrying the target section
(``"devices"`` / ``"cameras"``) and a spec-shaped payload; the Setup
tab merges that into ``hardware_payload`` and marks the section
dirty.

The dialog is *non-destructive*: it never writes to disk, never opens
a worker pool, and only reads from serial / USB enumeration APIs (the
adapter modules' discovery functions are required to be
read-only). Discover is disabled while a run is active because real
serial buses can only be probed by one process at a time.

Payload-extraction is exposed as a small pure helper
(:func:`build_device_payload_from_row`) so the routing logic is
testable without spinning up a qasync loop.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from capa.devices.discovery import discover_descriptor
from capa.devices.registry import ADAPTERS, AdapterDescriptor, ensure_adapters_loaded
from capa.ui.lifecycle import LifecycleKind, LifecycleRegistry

_logger = structlog.get_logger("capa.ui.setup_discovery")


# Adapter families that probe local serial ports during discovery.
# Running these scans in parallel makes them race for the same COM
# port handle — on Windows the loser gets a connection error on its
# very first probe and upstream ``find_devices`` puts the port in a
# ``dead_ports`` set, so the rest of the sweep (other bauds /
# protocols) is silently skipped. That manifests as "watlow not
# found on initial scan / rescan" intermittently. Scans for these
# families are run sequentially below so each adapter gets a clean
# shot at every port; non-serial scans (NI-DAQ, cameras) keep their
# parallel fan-out.
_SERIAL_PORT_FAMILIES: frozenset[str] = frozenset({"alicat", "watlow", "sartorius"})


# ---------------------------------------------------------------------------
# Pure helpers — testable without Qt.
# ---------------------------------------------------------------------------


def build_device_payload_from_row(
    descriptor: AdapterDescriptor,
    row: dict[str, Any],
    *,
    existing_names: set[str],
) -> dict[str, Any]:
    """Compose a ``DeviceConfig``-shaped dict from one discovery row.

    For non-camera adapters the result lands in
    ``hardware.devices``; cameras are routed through
    :func:`build_hw_entry_from_row` (which calls this for the
    ``params`` portion and assembles a :class:`CameraSpec`-shaped
    dict instead).

    Each adapter's ``discover()`` returns a list of dicts with adapter-
    specific keys (Watlow has ``port`` + ``address``; Alicat has
    ``port`` + ``unit_id`` + ``baudrate``; NI-DAQ has ``device`` +
    ``ai_channels``; cameras have ``selector`` / ``model`` /
    ``serial`` etc.). This function maps those into the schema
    that :class:`HardwareProfile` expects: ``name`` + ``adapter`` +
    ``params``. ``existing_names`` is used to pick a unique device
    name when the obvious one (``"alicat1"``) is already taken.
    """
    name = _pick_unique_name(descriptor.family, existing_names)
    params = dict(descriptor.default_params)
    family = descriptor.family
    if family == "alicat":
        for key in ("port", "unit_id", "baudrate"):
            if key in row:
                params[key] = row[key]
    elif family == "watlow":
        for key in ("port", "address"):
            if key in row:
                params[key] = row[key]
    elif family == "sartorius":
        for key in ("port", "baudrate"):
            if key in row:
                params[key] = row[key]
        if row.get("protocol"):
            # Some Sartorius scans surface the wire protocol; downstream
            # params model accepts it.
            params["protocol"] = row["protocol"]
    elif family == "nidaq" and row.get("device"):
        # NI-DAQ discover returns devices, not channels — operator
        # fills the channel list separately in the device detail pane.
        # We still pre-fill a friendlier task name from the device id.
        params.setdefault("task_name", f"{row['device']}_ai")
    return {
        "name": name,
        "adapter": descriptor.id,
        "params": params,
    }


def build_hw_entry_from_row(
    descriptor: AdapterDescriptor,
    row: dict[str, Any],
    *,
    existing_names: set[str],
) -> tuple[str, dict[str, Any]]:
    """Compose a hardware entry payload + target section from a discovery row.

    Returns ``("devices", payload)`` for serial/USB adapters and
    ``("cameras", payload)`` for camera adapters. The dialog uses the
    section value to route the merge into the right slot of
    :class:`HardwareProfile`.
    """
    family = descriptor.family
    if family in ("camera_visible", "camera_ir"):
        name = _pick_unique_name(family, existing_names)
        kind = "ir" if family == "camera_ir" else "visible"
        payload: dict[str, Any] = {
            "name": name,
            "adapter": descriptor.id,
            "kind": kind,
        }
        # Model + serial are first-class CameraSpec fields; the
        # adapter-specific bits (selector, transport) move into
        # ``params`` so the runtime adapter can read them.
        if isinstance(row.get("model"), str):
            payload["model_hint"] = row["model"]
        if isinstance(row.get("serial"), str):
            payload["serial"] = row["serial"]
        params = dict(descriptor.default_params)
        if "selector" in row:
            params["selector"] = row["selector"]
        if "transport" in row:
            params["transport"] = row["transport"]
        payload["params"] = params
        return ("cameras", payload)
    return (
        "devices",
        build_device_payload_from_row(descriptor, row, existing_names=existing_names),
    )


def _pick_unique_name(family: str, existing: set[str]) -> str:
    base = family if family != "plugin" else "device"
    candidate = f"{base}1"
    counter = 2
    while candidate in existing:
        candidate = f"{base}{counter}"
        counter += 1
    return candidate


def _summarise_row(family: str, row: dict[str, Any]) -> str:
    """Operator-facing one-line summary for the row table."""
    if family == "alicat":
        bits = []
        if row.get("port"):
            bits.append(str(row["port"]))
        if row.get("unit_id"):
            bits.append(f"id={row['unit_id']}")
        if row.get("model"):
            bits.append(str(row["model"]))
        if row.get("serial"):
            bits.append(f"sn={row['serial']}")
        return "  ".join(bits) or "(no identity)"
    if family == "watlow":
        bits = []
        if row.get("port"):
            bits.append(str(row["port"]))
        if row.get("address"):
            bits.append(f"addr={row['address']}")
        return "  ".join(bits) or "(no identity)"
    if family == "sartorius":
        bits = []
        if row.get("port"):
            bits.append(str(row["port"]))
        if row.get("protocol"):
            bits.append(row["protocol"])
        return "  ".join(bits) or "(no identity)"
    if family == "nidaq":
        bits = []
        if row.get("device"):
            bits.append(str(row["device"]))
        if row.get("product_type"):
            bits.append(str(row["product_type"]))
        ai = row.get("ai_channels") or ()
        ao = row.get("ao_channels") or ()
        if ai or ao:
            bits.append(f"{len(ai)}AI / {len(ao)}AO")
        return "  ".join(bits) or "(no identity)"
    return ", ".join(f"{k}={v}" for k, v in row.items() if k != "adapter") or "(empty)"


# ---------------------------------------------------------------------------
# Dialog.
# ---------------------------------------------------------------------------


class DiscoveryDialog(QDialog):
    """Modal-ish dialog that scans every ``discoverable`` adapter.

    Construction starts every scan on the next event-loop tick so the
    dialog can paint its initial "scanning…" state before any I/O
    kicks off. Each scan reports back through a Qt slot when it
    completes; results are appended to the table in arrival order.
    """

    entryAdded = Signal(str, dict)  # noqa: N815 — Qt signal naming convention
    """Emitted when the operator clicks [Add] on any discovered row.
    First argument is the target section (``"devices"`` or
    ``"cameras"``); second is the spec-shaped payload the Setup tab
    merges into ``hardware.devices`` / ``hardware.cameras``."""

    nidaqScanCompleted = Signal(list)  # noqa: N815 — Qt signal naming convention
    """Emitted after every NI-DAQ scan reports back with the full list of
    discovery rows (NI device dicts including the per-kind channel
    inventories). The :class:`SetupTab` consumes this to keep its
    ``nidaq_inventory()`` cache fresh without each form widget having to
    call ``nidaq.discover()`` itself."""

    def __init__(
        self,
        *,
        existing_names: set[str] | None = None,
        lifecycle: LifecycleRegistry | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("discovery_dialog")
        self.setWindowTitle("Discover devices")
        self.setMinimumSize(720, 400)
        self._existing_names: set[str] = set(existing_names or ())
        # Optional controller lifecycle registry. When provided, each
        # spawned scan task is registered as a non-critical entry so the
        # ShutdownCoordinator can cancel + await them at app shutdown —
        # without this, a dialog that Qt destroys via parent-destruction
        # (no closeEvent fires) leaves scan tasks orphaned on the loop,
        # holding serial-port / IOCP handles that wedge ``loop.close()``
        # and hang the terminal until the operator hits Ctrl-C.
        self._lifecycle: LifecycleRegistry | None = lifecycle
        # Per-adapter scan-status badges that the header label shows.
        self._scan_status: dict[str, str] = {}
        # Sequence of ``(descriptor, row)`` pairs — one per table row.
        # Tracked so we can rebuild payload on the [Add] click without
        # re-parsing the QTableWidget cells.
        self._rows: list[tuple[AdapterDescriptor, dict[str, Any]]] = []
        # In-flight scan tasks. Tracked so Rescan / Close / dialog
        # destruction can cancel them — otherwise the tasks keep
        # running with open serial-port handles, races against the next
        # scan's mark_scan_complete (producing duplicate rows), and
        # leaks pyserial threads that prevent the process from exiting.
        self._scan_tasks: list[asyncio.Task[None]] = []
        # Live cancellation-drain tasks. Held so they aren't garbage-
        # collected mid-flight by the event loop's weak task registry.
        self._drain_tasks: set[asyncio.Task[None]] = set()
        # Set on close so a scan task that completes after cancellation
        # short-circuits before touching deleted Qt widgets.
        self._closed = False
        # Belt-and-suspenders for the "operator closes the main window
        # while this dialog is still open" path: Qt destroys child
        # widgets without firing ``closeEvent``, so the close-side
        # overrides below never run. ``destroyed`` fires either way and
        # only touches Python-level state (the task list + flag), which
        # is safe even when the C++ widget is half-torn-down.
        self.destroyed.connect(self._on_destroyed)
        ensure_adapters_loaded()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(8)

        self._header = QLabel("Scanning…", self)
        outer.addWidget(self._header)

        self._table = QTableWidget(0, 4, self)
        self._table.setHorizontalHeaderLabels(["Adapter", "Identity", "Details", ""])
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        header = self._table.horizontalHeader()
        if header is not None:
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
            header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        outer.addWidget(self._table, stretch=1)

        # Bottom button row.
        buttons = QHBoxLayout()
        self._rescan_btn = QPushButton("Rescan", self)
        self._rescan_btn.clicked.connect(self.rescan)
        self._close_btn = QPushButton("Close", self)
        self._close_btn.setObjectName("discovery_close_button")
        self._close_btn.clicked.connect(self.accept)
        buttons.addWidget(self._rescan_btn)
        buttons.addStretch(1)
        buttons.addWidget(self._close_btn)
        outer.addLayout(buttons)

        # Discover the discoverable adapters once at construction. The
        # loop schedules each scan as a separate task so a slow port
        # doesn't block the rest of the table from filling.
        self._discoverable: list[AdapterDescriptor] = [
            d for d in ADAPTERS.values() if d.discoverable
        ]
        # Non-scannable adapters that still earn a row so operators
        # don't conclude "the scan didn't find my heater because it
        # never ran".
        self._non_discoverable: list[AdapterDescriptor] = [
            d for d in ADAPTERS.values() if not d.discoverable and d.discoverable_reason is not None
        ]
        self._render_non_discoverable_rows()
        QTimer.singleShot(0, self._start_all_scans)

    # ----------------------------------------------------------------- API

    def add_row(self, descriptor: AdapterDescriptor, row: dict[str, Any]) -> None:
        """Append one discovery row. Public-ish for tests that drive
        the dialog without spinning up a qasync loop."""
        idx = self._table.rowCount()
        self._table.insertRow(idx)
        self._table.setItem(idx, 0, QTableWidgetItem(descriptor.family))
        identity = _summarise_row(descriptor.family, row)
        self._table.setItem(idx, 1, QTableWidgetItem(identity))
        details = ", ".join(
            f"{k}={v}"
            for k, v in row.items()
            if k not in {"adapter", "port", "unit_id", "model", "serial"}
        )
        self._table.setItem(idx, 2, QTableWidgetItem(details))
        add_btn = QPushButton("Add", self._table)
        add_btn.clicked.connect(lambda _checked=False, d=descriptor, r=row: self._on_add(d, r))
        self._table.setCellWidget(idx, 3, add_btn)
        self._rows.append((descriptor, row))

    def rescan(self) -> None:
        """Clear the table and re-run every scan.

        Cancels in-flight scan tasks first so a slow probe from the
        previous round can't race the new round and produce duplicate
        rows or stray "no devices found" placeholders next to a hit.
        """
        self._cancel_pending_scans()
        self._table.setRowCount(0)
        self._rows.clear()
        self._scan_status.clear()
        self._render_non_discoverable_rows()
        self._start_all_scans()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 — Qt override
        """Cancel any in-flight scans before the dialog window closes.

        Without this, scan tasks survive the dialog and keep open
        serial-port handles. Those handles are owned by pyserial
        threads; on Windows they prevent the parent process from
        exiting until force-killed (the "terminal hangs after closing
        the app" symptom).
        """
        self._closed = True
        self._cancel_pending_scans()
        super().closeEvent(event)

    def reject(self) -> None:
        self._closed = True
        self._cancel_pending_scans()
        super().reject()

    def accept(self) -> None:
        self._closed = True
        self._cancel_pending_scans()
        super().accept()

    def _cancel_pending_scans(self) -> None:
        """Cancel every still-running scan task and drop the references.

        Idempotent: calling twice is fine. Tasks that already finished
        are skipped. Cancellation is best-effort — the underlying
        ``find_devices`` will raise :class:`asyncio.CancelledError` at
        the next ``await`` point and unwind, which closes the serial
        ports it opened.

        Also schedules an async drain (when an event loop is running)
        that awaits the cancelled tasks. The drain is fire-and-forget
        from the caller's perspective so Qt close slots stay
        non-blocking; the drain itself owns the await + suppress so a
        cancelled task's unwind can fully complete (closing IOCP
        handles, releasing serial ports) before the loop is stopped.
        Registered scan tasks are also re-cancelled and awaited by the
        :class:`~capa.ui.shutdown.ShutdownCoordinator` via the
        lifecycle registry; this drain is the per-dialog equivalent for
        the Rescan / dialog-Close cases that don't go through the
        coordinator.
        """
        cancelled: list[asyncio.Task[None]] = []
        for task in self._scan_tasks:
            if not task.done():
                task.cancel()
                cancelled.append(task)
        self._scan_tasks.clear()
        if not cancelled:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        drain = loop.create_task(
            self._drain_cancelled(cancelled),
            name="discover.drain_cancelled",
        )
        self._drain_tasks.add(drain)
        drain.add_done_callback(self._drain_tasks.discard)

    @staticmethod
    async def _drain_cancelled(tasks: list[asyncio.Task[None]]) -> None:
        """Await the cancellation unwind of every task in ``tasks``.

        Plain ``asyncio.gather(..., return_exceptions=True)`` swallows
        the :class:`asyncio.CancelledError` each task raises, so the
        coroutine returns without propagating cancellation back into
        the drain task itself — important when the caller is a Qt
        close slot and we've fired this off as a background task.
        """
        await asyncio.gather(*tasks, return_exceptions=True)

    def _register_with_lifecycle(self, task: asyncio.Task[None], *, name: str) -> None:
        """Register ``task`` with the controller's lifecycle registry
        if one was supplied. Non-critical so the cancel-tasks stage of
        :class:`~capa.ui.shutdown.ShutdownCoordinator` cancels + awaits
        these without blocking shutdown on a slow probe."""
        if self._lifecycle is None:
            return
        self._lifecycle.register(
            LifecycleKind.DISCOVERY,
            name,
            task,
            critical=False,
        )

    def _on_destroyed(self, _obj: object = None) -> None:
        """Cancel scans when Qt destroys the dialog without a closeEvent.

        Fires from :attr:`QObject.destroyed` so the close-side overrides
        (``closeEvent`` / ``reject`` / ``accept``) are not load-bearing
        for the "parent window closed first" path. Touches only Python
        state (the task list + flag) so it's safe even when the C++
        widget is partway through teardown.
        """
        self._closed = True
        self._cancel_pending_scans()

    def _add_placeholder_row(
        self,
        descriptor: AdapterDescriptor,
        *,
        summary: str,
        tooltip: str | None = None,
    ) -> None:
        """Append a non-actionable row carrying a status note.

        Used for empty scans, failed scans, and non-discoverable
        adapters. The row's [Add] button is
        replaced by a disabled placeholder so operators see the row
        is informational, not actionable.
        """
        idx = self._table.rowCount()
        self._table.insertRow(idx)
        family_item = QTableWidgetItem(descriptor.family)
        family_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        summary_item = QTableWidgetItem(summary)
        summary_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        # Slight grey so the row reads as informational, not a hit.
        for item in (family_item, summary_item):
            item.setForeground(Qt.GlobalColor.gray)
            if tooltip:
                item.setToolTip(tooltip)
        self._table.setItem(idx, 0, family_item)
        self._table.setItem(idx, 1, summary_item)
        self._table.setItem(idx, 2, QTableWidgetItem(""))
        placeholder_btn = QPushButton("—", self._table)
        placeholder_btn.setEnabled(False)
        if tooltip:
            placeholder_btn.setToolTip(tooltip)
        self._table.setCellWidget(idx, 3, placeholder_btn)

    def _render_non_discoverable_rows(self) -> None:
        """Show one disabled row per adapter that *would* be scannable
        but isn't (yet) — Watlow until watlowlib lands ``find_devices``,
        cameras until camera handshake ships, etc."""
        for descriptor in self._non_discoverable:
            self._add_placeholder_row(
                descriptor,
                summary="not scannable",
                tooltip=descriptor.discoverable_reason,
            )

    def set_existing_names(self, names: set[str]) -> None:
        self._existing_names = set(names)

    def mark_scan_complete(
        self,
        adapter_id: str,
        *,
        rows: list[dict[str, Any]] | None = None,
        error: str | None = None,
    ) -> None:
        """Record a scan's terminal state. Public-ish for tests."""
        descriptor = ADAPTERS.get(adapter_id)
        if descriptor is None:
            return
        if error is not None:
            self._scan_status[adapter_id] = "✗"
            self._add_placeholder_row(
                descriptor,
                summary=f"scan failed: {error}",
                tooltip=error,
            )
            _logger.warning("ui.setup.discover.scan_failed", adapter=adapter_id, error=error)
        else:
            self._scan_status[adapter_id] = "✓"
            row_list = list(rows or ())
            if not row_list:
                # Empty success must be visibly
                # distinct from "still scanning" — drop a placeholder
                # row so the table shows the scan actually ran.
                self._add_placeholder_row(
                    descriptor,
                    summary="no devices found",
                    tooltip=(
                        f"{descriptor.family} scan completed but found no devices on this machine"
                    ),
                )
            for row in row_list:
                self.add_row(descriptor, row)
            # NI scans contribute to the SetupTab inventory cache. The
            # empty-result case still fires — operators expect Rescan to
            # clear the cache when the NI driver is uninstalled or every
            # device is unplugged.
            if descriptor.family == "nidaq":
                self.nidaqScanCompleted.emit(row_list)
        self._refresh_header()

    # ---------------------------------------------------------------- internal

    def _start_all_scans(self) -> None:
        for descriptor in self._discoverable:
            self._scan_status[descriptor.id] = "…"
        self._refresh_header()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            # No event loop running. Surface this state in the header
            # so test paths that don't run qasync still see something
            # informative; tests can drive ``mark_scan_complete``
            # directly to exercise add-row behaviour.
            self._header.setText("Discovery requires the qasync event loop — run from the GUI.")
            return
        # Split scans by resource: serial-port scans race each other
        # for the same COM handle when run concurrently (see
        # ``_SERIAL_PORT_FAMILIES``), so chain those into one
        # sequential task. Everything else fans out in parallel.
        serial_scans: list[AdapterDescriptor] = []
        parallel_scans: list[AdapterDescriptor] = []
        for descriptor in self._discoverable:
            if descriptor.family in _SERIAL_PORT_FAMILIES:
                serial_scans.append(descriptor)
            else:
                parallel_scans.append(descriptor)
        for descriptor in parallel_scans:
            task = loop.create_task(
                self._run_one_scan(descriptor),
                name=f"discover.{descriptor.family}",
            )
            self._scan_tasks.append(task)
            self._register_with_lifecycle(task, name=f"discover.{descriptor.family}")
        if serial_scans:
            task = loop.create_task(
                self._run_scans_sequentially(serial_scans),
                name="discover.serial",
            )
            self._scan_tasks.append(task)
            self._register_with_lifecycle(task, name="discover.serial")

    async def _run_scans_sequentially(self, descriptors: list[AdapterDescriptor]) -> None:
        """Run each scan in turn, awaiting before starting the next.

        Used for serial-port-using adapters so they don't compete for
        the same COM handle. Cancellation propagates: if the parent
        task is cancelled, the in-flight ``_run_one_scan`` raises
        :class:`asyncio.CancelledError` and the remaining scans never
        start.
        """
        for descriptor in descriptors:
            if self._closed:
                return
            await self._run_one_scan(descriptor)

    async def _run_one_scan(self, descriptor: AdapterDescriptor) -> None:
        try:
            result = await discover_descriptor(descriptor)
        except asyncio.CancelledError:
            # Dialog closed or Rescan cancelled this run — let the task
            # end cleanly. ``find_devices`` closes the serial ports it
            # had open on the way up.
            raise
        if self._closed:
            return
        if result.error is not None:
            self.mark_scan_complete(descriptor.id, error=result.error)
        else:
            self.mark_scan_complete(descriptor.id, rows=result.rows)

    def _refresh_header(self) -> None:
        bits = []
        for descriptor in self._discoverable:
            status = self._scan_status.get(descriptor.id, "…")
            bits.append(f"{descriptor.family} {status}")
        # Non-scannable adapters are advertised with an em-dash so
        # operators know they were considered but cannot be scanned.
        # Suppress duplicates so two non-discoverable adapters of the
        # same family (e.g. two camera adapters) collapse to one entry.
        seen_families: set[str] = {d.family for d in self._discoverable}
        for descriptor in self._non_discoverable:
            if descriptor.family in seen_families:
                continue
            seen_families.add(descriptor.family)
            bits.append(f"{descriptor.family} —")
        self._header.setText("Scans: " + "   ".join(bits))

    def _on_add(self, descriptor: AdapterDescriptor, row: dict[str, Any]) -> None:
        section, payload = build_hw_entry_from_row(
            descriptor, row, existing_names=self._existing_names
        )
        self._existing_names.add(payload["name"])
        _logger.info(
            "ui.setup.discover.add",
            adapter=descriptor.id,
            section=section,
            name=payload["name"],
        )
        self.entryAdded.emit(section, payload)


__all__ = [
    "DiscoveryDialog",
    "build_device_payload_from_row",
    "build_hw_entry_from_row",
]
