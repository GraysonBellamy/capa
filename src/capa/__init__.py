"""capa — control and DAQ for a custom cone-calorimeter-class instrument."""

from __future__ import annotations

try:
    from capa._version import __version__  # type: ignore[import-not-found]
except ImportError:
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
