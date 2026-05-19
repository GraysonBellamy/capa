"""Small helpers shared by setup-section table models."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt

if TYPE_CHECKING:
    from PySide6.QtWidgets import QTableView


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


def fit_table_height(table: QTableView, *, max_rows: int | None = None) -> None:
    """Lock ``table`` height to header + visible row heights.

    Sized so the table never shows an inner vertical scrollbar while the
    enclosing pane still has free space. When ``max_rows`` is set and the
    model holds more, the height pins to that many rows and the scrollbar
    is re-enabled so the overflow remains reachable.
    """
    model = table.model()
    rows = model.rowCount() if model is not None else 0
    visible_rows = rows if max_rows is None else min(rows, max_rows)
    header = table.horizontalHeader()
    header_h = header.sizeHint().height() if header is not None else 0
    if header_h <= 0:
        header_h = table.fontMetrics().height() + 8
    if visible_rows == 0:
        v_header = table.verticalHeader()
        rows_h = v_header.defaultSectionSize() if v_header is not None else 24
    else:
        rows_h = sum(table.rowHeight(i) for i in range(visible_rows))
    frame = 2 * table.frameWidth()
    table.setFixedHeight(header_h + rows_h + frame)
    overflow = max_rows is not None and rows > max_rows
    table.setVerticalScrollBarPolicy(
        Qt.ScrollBarPolicy.ScrollBarAsNeeded if overflow else Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    )


__all__ = ["fit_table_height", "horizontal_header", "unique_name"]
