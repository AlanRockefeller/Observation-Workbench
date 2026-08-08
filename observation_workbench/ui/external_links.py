"""Helpers for opening external links without leaking browser startup noise."""

from __future__ import annotations

from contextlib import contextmanager
import logging
import os
import sys
from typing import Iterator

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices


def open_external_url_silently(url: str | QUrl) -> bool:
    """Open a URL while suppressing native browser startup output."""
    qurl = url if isinstance(url, QUrl) else QUrl(url)
    if logging.getLogger().isEnabledFor(logging.DEBUG):
        return QDesktopServices.openUrl(qurl)
    with _suppress_native_output():
        return QDesktopServices.openUrl(qurl)


@contextmanager
def _suppress_native_output() -> Iterator[None]:
    if os.name != "posix":
        yield
        return

    sys.stdout.flush()
    sys.stderr.flush()
    saved_stdout_fd = os.dup(1)
    saved_stderr_fd = os.dup(2)
    devnull_fd = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull_fd, 1)
        os.dup2(devnull_fd, 2)
        yield
    finally:
        os.dup2(saved_stdout_fd, 1)
        os.dup2(saved_stderr_fd, 2)
        os.close(saved_stdout_fd)
        os.close(saved_stderr_fd)
        os.close(devnull_fd)
