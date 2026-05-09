"""Derived channels and transforms.

P0a stub. Concrete derivations (oxygen depletion, mass-loss rate, smoke
production, heat-release inputs — plan §5.4.1) land with the cone-calorimeter
profile's runtime in P3+.

The :class:`DerivedBinding` schema in :mod:`capa.channels.spec` already declares
the dependency edges so a future topological-sort pass can be added without
schema churn.
"""

from __future__ import annotations

__all__: list[str] = []
