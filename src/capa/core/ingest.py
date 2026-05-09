"""External event ingest endpoint.

Plan §11.1. While a run is active, sibling processes can post timestamped
events into the bundle. The transport is a Unix-domain socket (Linux) or
named pipe (Windows) at ``runs/<bundle>/.ingest.sock``; an optional HTTP
loopback can be enabled for clients that prefer it.

Wire format: newline-delimited JSON.

::

    {"t_utc": "2026-05-08T01:23:45.678Z", "channel": "annotation",
     "kind": "operator_note", "payload": {"text": "ignited"}}

Capa stamps ``t_mono_ns`` from the engine's :class:`RunClock` at receipt
unless the producer supplies its own ``t_mono_ns_anchor`` (a monotonic
nanoseconds value already on the run's timeline).

Failures are non-fatal — a bind error logs and disables ingest for that
run, leaving the engine running. The endpoint is opened by the engine just
after the writer opens and torn down in the engine's ``finally`` block.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio
import structlog

# A small helper type — the engine hands us a function that drops events into
# the bundle's ``events.sqlite``. We don't import RunBundleWriter here to
# keep this module dependency-light (and to make it trivially mockable in
# tests).
EventSink = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass(slots=True)
class IngestConfig:
    """Operator-tunable ingest knobs.

    Defaults are conservative: UDS only on POSIX, no HTTP loopback. Plan
    §11.1 line 977 — HTTP is off by default."""

    socket_path: Path
    """Unix-domain socket path. Conventionally
    ``<bundle>/.ingest.sock``. On Windows this becomes the named-pipe
    name (``\\\\.\\pipe\\<sanitized>``)."""

    enable_http: bool = False
    """When ``True``, also bind a TCP loopback HTTP-POST endpoint."""

    http_host: str = "127.0.0.1"
    http_port: int = 0
    """``0`` lets the OS pick a free port. The chosen port is returned by
    :meth:`IngestServer.start` so the engine can record it in the bundle's
    manifest."""


@dataclass(slots=True)
class IngestServer:
    """Lifecycle wrapper for the ingest endpoint(s).

    The engine constructs one per run, calls :meth:`start` inside the task
    group, and :meth:`stop` from the ``finally`` block. The server itself
    owns no state beyond the bound listeners; per-event state is recorded
    via the supplied :class:`EventSink`.
    """

    config: IngestConfig
    sink: EventSink
    logger: structlog.stdlib.BoundLogger
    """Bound logger so ingest log lines carry the same run_id context as
    the engine."""

    _uds_listener: anyio.abc.SocketListener | None = None
    _tcp_listener: anyio.abc.Listener[anyio.abc.SocketStream] | None = None
    _task_group: anyio.abc.TaskGroup | None = None
    _http_port: int | None = None

    @property
    def http_port(self) -> int | None:
        """Bound TCP port when HTTP is enabled; ``None`` otherwise."""
        return self._http_port

    async def start(self, task_group: anyio.abc.TaskGroup) -> None:
        """Bind the listener(s) and spawn accept loops in ``task_group``.

        Bind failures are logged and skipped; the engine keeps running with
        ingest disabled rather than crashing. UDS bind failures on Windows
        (no AF_UNIX before 10/2018) likewise downgrade to "ingest disabled"
        rather than aborting the run.
        """
        self._task_group = task_group
        if sys.platform != "win32":
            try:
                # Stale socket from a previous crashed run? Unlink to allow re-bind.
                if self.config.socket_path.exists():
                    self.config.socket_path.unlink()
                self.config.socket_path.parent.mkdir(parents=True, exist_ok=True)
                self._uds_listener = await anyio.create_unix_listener(self.config.socket_path)
                task_group.start_soon(self._serve_uds)
                self.logger.info("ingest.uds.bound", path=str(self.config.socket_path))
            except (OSError, NotImplementedError) as exc:
                self.logger.warning(
                    "ingest.uds.bind_failed", path=str(self.config.socket_path), error=str(exc)
                )
        else:
            self.logger.info("ingest.uds.skipped_on_windows")

        if self.config.enable_http:
            try:
                listeners = await anyio.create_tcp_listener(
                    local_host=self.config.http_host,
                    local_port=self.config.http_port,
                )
                # AnyIO returns a multi-listener; we only ever bind one host.
                self._tcp_listener = listeners
                # Pull the bound port out of the underlying socket.
                socks = getattr(listeners, "listeners", None)
                if socks:
                    sa = socks[0].extra(anyio.abc.SocketAttribute.local_address)
                    self._http_port = int(sa[1])
                task_group.start_soon(self._serve_tcp)
                self.logger.info(
                    "ingest.http.bound",
                    host=self.config.http_host,
                    port=self._http_port,
                )
            except OSError as exc:
                self.logger.warning(
                    "ingest.http.bind_failed",
                    host=self.config.http_host,
                    port=self.config.http_port,
                    error=str(exc),
                )

    async def stop(self) -> None:
        """Close the listeners. The accept tasks exit when their listener
        closes; the task group then completes."""
        for lst in (self._uds_listener, self._tcp_listener):
            if lst is None:
                continue
            try:
                await lst.aclose()
            except Exception as exc:
                self.logger.warning("ingest.close_failed", error=str(exc))
        if sys.platform != "win32":
            with suppress(OSError):
                self.config.socket_path.unlink(missing_ok=True)

    # ---------------------------------------------------------------- accept

    async def _serve_uds(self) -> None:
        assert self._uds_listener is not None
        try:
            await self._uds_listener.serve(self._handle_client)
        except anyio.ClosedResourceError:
            return

    async def _serve_tcp(self) -> None:
        assert self._tcp_listener is not None
        try:
            await self._tcp_listener.serve(self._handle_http_client)
        except anyio.ClosedResourceError:
            return

    async def _handle_client(self, stream: anyio.abc.SocketStream) -> None:
        """Read newline-delimited JSON until the peer closes."""
        buffer = b""
        try:
            async for chunk in stream:
                buffer += chunk
                while b"\n" in buffer:
                    line, _, buffer = buffer.partition(b"\n")
                    await self._dispatch_line(line)
        except (anyio.EndOfStream, anyio.BrokenResourceError):
            pass
        finally:
            with anyio.CancelScope(shield=True):
                await stream.aclose()

    async def _handle_http_client(self, stream: anyio.abc.SocketStream) -> None:
        """Minimal HTTP/1.1 server — just enough to accept POST /ingest with
        a JSON body. Anything else returns 400.

        Deliberately unsophisticated; the real story is the UDS path. HTTP
        exists only as an escape hatch for clients that can't open a unix
        socket easily."""
        try:
            data = b""
            async for chunk in stream:
                data += chunk
                if b"\r\n\r\n" in data:
                    break
            head, _, body = data.partition(b"\r\n\r\n")
            head_text = head.decode("latin-1", errors="replace")
            request_line = head_text.splitlines()[0] if head_text else ""
            if not request_line.startswith("POST "):
                await stream.send(b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\n\r\n")
                return
            content_length = 0
            for h in head_text.splitlines()[1:]:
                if h.lower().startswith("content-length:"):
                    content_length = int(h.split(":", 1)[1].strip() or "0")
            while len(body) < content_length:
                more = await stream.receive(content_length - len(body))
                if not more:
                    break
                body += more
            for line in body.splitlines():
                if line.strip():
                    await self._dispatch_line(line)
            await stream.send(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n")
        except (anyio.EndOfStream, anyio.BrokenResourceError):
            return
        finally:
            with anyio.CancelScope(shield=True):
                await stream.aclose()

    async def _dispatch_line(self, line: bytes) -> None:
        if not line.strip():
            return
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            self.logger.warning(
                "ingest.bad_json", error=str(exc), raw=line[:200].decode(errors="replace")
            )
            return
        if not isinstance(payload, dict):
            self.logger.warning("ingest.bad_shape", got=type(payload).__name__)
            return
        normalized = _normalize_event(payload)
        try:
            await self.sink(normalized)
        except Exception as exc:
            self.logger.warning("ingest.sink_failed", error=str(exc), kind=normalized.get("kind"))


def _normalize_event(payload: dict[str, Any]) -> dict[str, Any]:
    """Coerce a raw JSON event into the engine's events.sqlite shape.

    Required: ``kind``. Optional: ``channel``, ``message``, ``severity``,
    ``payload`` (extra metadata), ``t_utc`` (ISO-8601), ``t_mono_ns_anchor``
    (already on the run's monotonic timeline). Missing ``t_utc`` is filled
    from wall-clock-now; missing ``t_mono_ns`` is filled by the engine
    (we leave it unset here so the engine's clock is the source of truth)."""
    out: dict[str, Any] = {
        "kind": str(payload.get("kind", "ingest.event")),
        "message": str(payload.get("message", "")),
        "severity": str(payload.get("severity", "info")),
        "channel": payload.get("channel"),
        "payload": payload.get("payload") or {},
    }
    t_utc = payload.get("t_utc")
    if isinstance(t_utc, str):
        try:
            out["t_utc"] = datetime.fromisoformat(t_utc.replace("Z", "+00:00"))
        except ValueError:
            out["t_utc"] = datetime.now(UTC)
    elif isinstance(t_utc, datetime):
        out["t_utc"] = t_utc if t_utc.tzinfo else t_utc.replace(tzinfo=UTC)
    else:
        out["t_utc"] = datetime.now(UTC)
    if "t_mono_ns_anchor" in payload:
        with suppress(TypeError, ValueError):
            out["t_mono_ns_anchor"] = int(payload["t_mono_ns_anchor"])
    return out


__all__ = [
    "EventSink",
    "IngestConfig",
    "IngestServer",
]
