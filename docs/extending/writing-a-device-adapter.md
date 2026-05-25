# Writing a device adapter

> **Status:** stub — content to be written.

**Audience:** integrators adding a new device family.
**Scope:** implementing the ``DeviceAdapter`` contract against your device library.

## Will cover

- The contract: ``open/close/start/stop/stream/command/snapshot``
- ``resource_id`` and why workers group by it
- Emission shape: wide row vs narrow row
- Safe-shutdown obligations
- ``expected_emission_rate_hz`` and bridge sizing
- Per-family discovery hooks
- Testing with a fake transport

