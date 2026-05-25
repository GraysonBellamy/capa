""":class:`ChannelRegistry` — runtime name → ``(adapter, binding, calibration, sinks)`` lookup.

Channel names are the stable contract; the registry resolves a name
to the concrete handles needed to emit/route a sample. The registry is
*frozen* at run-arm time — later config edits do not change historical meaning.

The registry ships the schema and the in-memory resolver. The actual wiring of an
adapter handle into :class:`ResolvedChannel` lands when the engine task group
constructs adapters at run-arm.
"""

from __future__ import annotations

from dataclasses import dataclass

from capa.channels.calibration import Calibration
from capa.channels.spec import ChannelSpec, SourceBinding
from capa.core.errors import ConfigError


@dataclass(frozen=True, slots=True)
class ResolvedChannel:
    """Snapshot of one channel's resolved binding.

    Frozen because :class:`ChannelRegistry` snapshots all specs at run start;
    mutation here would let later edits silently change historical meaning.
    """

    name: str
    spec: ChannelSpec
    binding: SourceBinding
    calibration: Calibration
    sinks: tuple[str, ...]


class ChannelRegistry:
    """In-memory lookup keyed by channel name.

    Lifecycle:

    * Build with :meth:`from_specs` (or by repeated :meth:`register`).
    * Call :meth:`freeze` at run-arm. Mutations after freeze raise
      :class:`ConfigError`.
    * :meth:`resolve` returns the snapshotted :class:`ResolvedChannel`.

    Held by :class:`~capa.experiment.config.ExperimentConfig`; the engine
    calls ``freeze()`` once it has opened all adapters and verified
    the channel set.
    """

    __slots__ = ("_channels", "_frozen")

    def __init__(self) -> None:
        self._channels: dict[str, ResolvedChannel] = {}
        self._frozen = False

    @classmethod
    def from_specs(cls, specs: list[ChannelSpec]) -> ChannelRegistry:
        """Build a registry by registering every spec in ``specs`` in order."""
        registry = cls()
        for spec in specs:
            registry.register(spec)
        return registry

    @property
    def is_frozen(self) -> bool:
        """``True`` once :meth:`freeze` has been called; further registrations raise."""
        return self._frozen

    def register(self, spec: ChannelSpec) -> None:
        """Register a new :class:`ChannelSpec`.

        Raises:
            ConfigError: The registry has been frozen, or ``spec.name``
                duplicates an already-registered channel.
        """
        if self._frozen:
            raise ConfigError(f"ChannelRegistry is frozen; cannot register {spec.name!r}")
        if spec.name in self._channels:
            raise ConfigError(f"duplicate channel name {spec.name!r}")
        self._channels[spec.name] = ResolvedChannel(
            name=spec.name,
            spec=spec,
            binding=spec.source,
            calibration=spec.calibration,
            sinks=spec.sinks,
        )

    def freeze(self) -> None:
        """Lock the registry. After this, :meth:`register` raises."""
        self._frozen = True

    def resolve(self, name: str) -> ResolvedChannel:
        """Return the :class:`ResolvedChannel` registered for ``name``.

        Raises:
            ConfigError: ``name`` is not in the registry.
        """
        try:
            return self._channels[name]
        except KeyError as exc:
            raise ConfigError(f"channel {name!r} is not registered") from exc

    def names(self) -> tuple[str, ...]:
        """All registered channel names, in registration order."""
        return tuple(self._channels.keys())

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._channels

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self._channels.values())

    def __len__(self) -> int:
        return len(self._channels)


__all__ = ["ChannelRegistry", "ResolvedChannel"]
