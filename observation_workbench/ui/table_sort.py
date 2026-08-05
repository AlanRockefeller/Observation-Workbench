"""Click-to-sort helpers for preview tables built from ``QTableWidgetItem`` cells.

Qt's default ``QTableWidgetItem`` comparison is a plain string compare, so an
"Observation ID" column would order ``10`` before ``9``.  The items here compare
numerically whenever both cells hold plain numbers and fall back to a
case-insensitive string compare otherwise.

Only use these on tables whose rows are made entirely of items: ``setCellWidget``
widgets do not follow their row when the view is sorted, so a table with embedded
checkboxes or combo boxes would silently mis-associate its controls.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from typing import Generator

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QTableWidget, QTableWidgetItem

_NUMBER_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?$")


def _as_number(text: str) -> float | None:
    stripped = text.strip().lstrip("#")
    if _NUMBER_RE.match(stripped):
        return float(stripped)
    return None


class SortableTableWidgetItem(QTableWidgetItem):
    """Cell that sorts numerically when both compared cells hold numbers."""

    def __lt__(self, other: QTableWidgetItem) -> bool:
        mine = self.text()
        theirs = other.text() if isinstance(other, QTableWidgetItem) else ""
        left = _as_number(mine)
        right = _as_number(theirs)
        if left is not None and right is not None:
            return left < right
        return mine.strip().casefold() < theirs.strip().casefold()


class SortableCheckItem(SortableTableWidgetItem):
    """Checkbox cell that sorts by check state rather than by its (empty) text."""

    def __lt__(self, other: QTableWidgetItem) -> bool:
        if isinstance(other, QTableWidgetItem):
            mine = 1 if self.checkState() == Qt.CheckState.Checked else 0
            theirs = 1 if other.checkState() == Qt.CheckState.Checked else 0
            if mine != theirs:
                return mine < theirs
        return super().__lt__(other)


def enable_click_sorting(table: QTableWidget) -> None:
    """Let the user sort/reverse-sort the table by clicking a column header."""
    header = table.horizontalHeader()
    header.setSectionsClickable(True)
    header.setSortIndicatorShown(True)
    table.setSortingEnabled(True)


@contextmanager
def sorting_suspended(table: QTableWidget) -> Generator[None, None, None]:
    """Fill a table without rows shuffling underneath the writes.

    Restoring the previous state re-applies the current sort indicator, so a sort
    the user picked survives a repopulate.
    """
    was_enabled = table.isSortingEnabled()
    table.setSortingEnabled(False)
    try:
        yield
    finally:
        table.setSortingEnabled(was_enabled)
