"""Diagnostic: why didn't ``capa.devices.sartorius.discover`` find the balance?

Run with: ``uv run python scripts/diagnose_sartorius_discover.py``

The Setup-editor Discover dialog (Phase F) calls
``capa.devices.sartorius.discover()``, which has five silent-failure
paths that all look identical from the GUI ("scan succeeded, zero
rows"). This script touches each layer in order and prints what it
sees so you can tell *which* layer is empty-handed.

Optional: pass a specific port to skip enumeration and probe only it,
e.g. ``uv run python scripts/diagnose_sartorius_discover.py COM4``.
"""

from __future__ import annotations

import asyncio
import sys
import traceback


async def main(explicit_port: str | None = None) -> int:
    print("=" * 72)
    print("Sartorius discover diagnostic")
    print("=" * 72)

    # Layer 1: anyserial importable?
    print("\n[1] importing anyserial …", end=" ")
    try:
        import anyserial
    except ImportError as exc:
        print("MISSING")
        print(f"    -> anyserial is not installed: {exc}")
        print("    -> capa.devices.sartorius.discover returns [] silently in this case.")
        return 1
    print("ok")

    # Layer 2: list visible serial ports.
    if explicit_port is None:
        print("\n[2] anyserial.list_serial_ports() …")
        try:
            visible = await anyserial.list_serial_ports()
        except Exception as exc:
            print(f"    -> raised {type(exc).__name__}: {exc}")
            traceback.print_exc()
            return 2
        if not visible:
            print("    -> empty list. No serial ports enumerated.")
            print("    -> Check USB cable, COM driver, and that the balance is powered.")
            return 3
        print(f"    -> found {len(visible)} port(s):")
        for p in visible:
            desc = getattr(p, "description", "")
            hwid = getattr(p, "hwid", "")
            print(f"       {p.device}    description={desc!r}    hwid={hwid!r}")
        ports = [p.device for p in visible]
    else:
        print(f"\n[2] (skipped — using explicit port {explicit_port!r})")
        ports = [explicit_port]

    # Layer 3: sartoriuslib.discover_port per port.
    print("\n[3] importing sartoriuslib …", end=" ")
    try:
        import sartoriuslib
    except ImportError as exc:
        print(f"MISSING: {exc}")
        return 4
    print("ok")

    print("\n[4] probing each port with sartoriuslib.discover_port()")
    print("    sweeping baudrates (capa's wrapper only tries the library default,")
    print("    which is the Phase G item 3 follow-up — this loop is what the fixed")
    print("    wrapper will do):")
    from sartoriuslib.errors import SartoriusError  # local import per layer
    from sartoriuslib.transport.base import SerialSettings  # type: ignore[import-not-found]

    sweep_baudrates = (9600, 19200, 38400, 57600, 115200)
    any_hit = False
    for port in ports:
        print(f"\n  port {port}:")
        for baudrate in sweep_baudrates:
            settings = SerialSettings(port=port, baudrate=baudrate)
            try:
                result = await sartoriuslib.discover_port(port, serial_settings=settings)
            except SartoriusError as exc:
                print(f"    {baudrate}: SartoriusError: {exc}")
                continue
            except Exception as exc:
                print(f"    {baudrate}: {type(exc).__name__}: {exc}")
                traceback.print_exc()
                continue
            status = "HIT" if (result.ok and result.protocol is not None) else "miss"
            print(
                f"    {baudrate}: {status}  ok={result.ok}  "
                f"protocol={result.protocol!r}  "
                f"autoprint_active={getattr(result, 'autoprint_active', '?')}"
            )
            if result.ok and result.protocol is not None:
                any_hit = True
                break  # don't double-report on multiple baudrates

    # Layer 5: re-run the actual capa-side discover for the same answer.
    print("\n[5] capa.devices.sartorius.discover() (the real call):")
    from capa.devices.sartorius import discover as capa_discover

    rows = await capa_discover(ports=ports)
    if not rows:
        print("    -> [] (no rows surfaced)")
    else:
        for r in rows:
            print(f"    -> {r}")

    print("\n" + "=" * 72)
    if any_hit and rows:
        print("DIAGNOSIS: discover is working — balance(s) found.")
        print("Re-run the GUI Discover dialog; if rows still don't appear there,")
        print("the issue is in the dialog wiring, not the adapter.")
    elif any_hit and not rows:
        print("DIAGNOSIS: probe found a balance but discover() filtered it out.")
        print("Inspect the result fields above against the discover() body.")
    elif not any_hit:
        print("DIAGNOSIS: no port responded. Likely causes:")
        print("  - balance powered off, cable disconnected, or wrong COM port")
        print("  - balance is configured for a baudrate / protocol the library")
        print("    doesn't probe (check the balance's display menu)")
        print("  - another program (vendor utility, terminal) holds the port open")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    port = sys.argv[1] if len(sys.argv) > 1 else None
    sys.exit(asyncio.run(main(port)))
