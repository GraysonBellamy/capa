"""Hardware smoke tests.

Gated behind ``CAPA_HARDWARE_TESTS=1``; otherwise every test in this package
is skipped. "run on the rig PC."

Per-vendor envvars supply the connection parameters so the tests are
portable across different rig wiring:

* ``CAPA_TEST_WATLOW_PORT`` — serial port (``/dev/ttyUSB0``, ``COM3``).
* ``CAPA_TEST_WATLOW_ADDR`` — bus address; defaults to ``1``.
* ``CAPA_TEST_WATLOW_PROTOCOL`` — ``stdbus`` (default) / ``modbus_rtu`` / ``auto``.
* ``CAPA_TEST_WATLOW_OPERATOR`` — operator id required to authorize the
  no-op setpoint write; defaults to ``"hw-test"``.
"""
