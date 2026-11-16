"""Keep the computer awake while a recording runs."""

from __future__ import annotations

import sys
from collections.abc import Callable

_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001


def _windows_set_state() -> Callable[[int], int] | None:
    if sys.platform != "win32":
        return None
    import ctypes

    return ctypes.WinDLL("kernel32").SetThreadExecutionState  # type: ignore[attr-defined]


class KeepAwake:
    """Ask Windows not to sleep while held; does nothing on other systems.

    A recording can run for a day. Windows may sleep after a period without input,
    which stops the cameras mid-recording. The request belongs to the thread that made
    it, so acquire and release on the same, long-lived thread (the GUI thread). The
    display may still turn off.
    """

    def __init__(self, set_state: Callable[[int], int] | None = None) -> None:
        self._set_state = set_state if set_state is not None else _windows_set_state()
        self.held = False

    def acquire(self) -> None:
        if not self.held and self._set_state is not None:
            self._set_state(_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED)
        self.held = True

    def release(self) -> None:
        if self.held and self._set_state is not None:
            self._set_state(_ES_CONTINUOUS)
        self.held = False

    def __enter__(self) -> KeepAwake:
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()
