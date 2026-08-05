"""Shared Retry/Cancel presentation for user-initiated, read-only requests."""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMessageBox, QWidget


def show_safe_read_failure(
    parent: QWidget,
    operation: str,
    exc: Exception,
    retry: Callable[[], None],
) -> None:
    """Show complete diagnostics and optionally repeat the same safe read."""
    message = QMessageBox(parent)
    message.setIcon(QMessageBox.Icon.Critical)
    message.setWindowTitle("Read failed")
    message.setText(operation)
    message.setInformativeText(_format_read_failure(exc))
    retry_button = message.addButton("Retry", QMessageBox.ButtonRole.AcceptRole)
    message.addButton(QMessageBox.StandardButton.Cancel)
    message.exec()
    if message.clickedButton() is retry_button:
        # Returning to the event loop before retrying avoids nested failure
        # dialogs and lets close/reject lifecycle guards take effect first.
        QTimer.singleShot(0, lambda: _retry_if_parent_is_alive(parent, retry))


def format_read_failure(exc: Exception) -> str:
    """Return bounded, credential-free diagnostics suitable for display."""
    return _format_read_failure(exc)


def _format_read_failure(exc: Exception) -> str:
    endpoint = getattr(exc, "endpoint", "") or "Unavailable"
    status = _status_code(exc)
    response_body = getattr(exc, "response_body", "") or ""
    lines = [
        f"Endpoint: {endpoint}",
        f"HTTP status: {status if status is not None else 'Unavailable'}",
        f"Exception: {type(exc).__name__}",
        f"Message: {exc}",
    ]
    if response_body:
        lines.append(f"Response: {response_body[:800]}")
    return "\n".join(lines)


def _status_code(exc: Exception) -> int | None:
    direct_status = getattr(exc, "status_code", None)
    if isinstance(direct_status, int):
        return direct_status
    response = getattr(exc, "response", None)
    response_status = getattr(response, "status_code", None)
    return response_status if isinstance(response_status, int) else None


def _retry_if_parent_is_alive(parent: QWidget, retry: Callable[[], None]) -> None:
    try:
        parent.windowTitle()
    except RuntimeError:
        return
    retry()
