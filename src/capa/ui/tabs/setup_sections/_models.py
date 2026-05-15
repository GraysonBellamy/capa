"""Small helpers shared by setup-section table models."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from PySide6.QtCore import Qt


def horizontal_header(
    headers: Sequence[str],
    section: int,
    orientation: Qt.Orientation,
    role: int,
) -> object:
    if role != Qt.ItemDataRole.DisplayRole:
        return None
    if orientation == Qt.Orientation.Horizontal and 0 <= section < len(headers):
        return headers[section]
    return None


def unique_name(existing: Iterable[str], base: str) -> str:
    used = set(existing)
    if base not in used:
        return base
    n = 2
    while f"{base}_{n}" in used:
        n += 1
    return f"{base}_{n}"


__all__ = ["horizontal_header", "unique_name"]
