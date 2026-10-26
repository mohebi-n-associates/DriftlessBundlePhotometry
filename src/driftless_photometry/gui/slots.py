"""Signal handlers that carry arguments, as methods of a QObject.

On macOS with Python 3.13.16 and PySide6 6.8.3, CI showed signal connections to
lambdas and to ``functools.partial`` objects being dropped soon after they were made,
so sliders, spin boxes, buttons, and menu actions silently did nothing. Connections to
methods of a QObject survived. ``slot`` returns such a method: a small QObject, parented
to the widget that sends the signal (so it lives and dies with it), holds the target
and its arguments.

It holds them weakly. A strong reference back to the window would close a reference
cycle through Qt's parent/child ownership, and Python's cycle collector could then free
the window on whichever thread happened to trigger it, such as the NWB writer, which
deadlocked on Windows and crashed on macOS.
"""

from __future__ import annotations

import weakref
from collections.abc import Callable
from types import MethodType
from typing import Any

from PySide6.QtCore import QObject


def _weak(value: Any) -> Callable[[], Any]:
    """A callable returning ``value``, holding it weakly when it is a method or QObject."""

    if isinstance(value, MethodType):
        return weakref.WeakMethod(value)
    if isinstance(value, QObject):
        return weakref.ref(value)
    return lambda: value


_GONE = object()


class _Slot(QObject):
    def __init__(
        self,
        parent: QObject,
        func: Callable[..., object],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        with_signal_args: bool,
    ) -> None:
        super().__init__(parent)
        self._func = _weak(func)
        self._args = tuple(_weak(arg) for arg in args)
        self._kwargs = {name: _weak(value) for name, value in kwargs.items()}
        self._with_signal_args = with_signal_args

    def call(self, *signal_args: object) -> None:
        func = self._func()
        args = [ref() for ref in self._args]
        kwargs = {name: ref() for name, ref in self._kwargs.items()}
        if func is None or any(value is None for value in (*args, *kwargs.values())):
            return  # the window or a widget it needs has gone
        if self._with_signal_args:
            func(*args, *signal_args, **kwargs)
        else:
            func(*args, **kwargs)


def slot(
    sender: QObject, func: Callable[..., object], *args: Any, **kwargs: Any
) -> Callable[..., None]:
    """A handler calling ``func(*args, *signal_args, **kwargs)``, owned by ``sender``."""

    return _Slot(sender, func, args, kwargs, True).call


def button_slot(
    sender: QObject, func: Callable[..., object], *args: Any, **kwargs: Any
) -> Callable[..., None]:
    """Like ``slot``, but ignores the signal's own arguments (such as a button's checked)."""

    return _Slot(sender, func, args, kwargs, False).call
