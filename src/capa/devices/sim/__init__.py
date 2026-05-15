"""Simulated adapters mirroring each :class:`DeviceAdapter` Protocol exactly.

Sim adapters unblock every UI iteration from hardware availability
and give the test suite a fast deterministic substrate. They mirror the real
Protocol exactly, so any Procedure or sink that works against a sim adapter
works against the real one.
"""
