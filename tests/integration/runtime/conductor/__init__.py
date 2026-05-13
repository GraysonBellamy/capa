"""Integration tests for :class:`capa.runtime.conductor.Conductor`.

Each test spins up a real :class:`Conductor` (real thread, real loop)
against a :class:`WorkerPool` of :class:`Worker`\\ s hosting
:class:`FakeAdapter`\\ s on real :class:`ThreadedRunner`\\ s. The point is
to exercise the cross-thread orchestration end-to-end without dragging
real hardware (or even real bundle writers) into scope; the production
session (Phase 2.4) wires the bundle pipeline; here we use
:class:`FakeRunSession`.
"""
