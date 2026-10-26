"""Run Python's cycle collector on the GUI thread only.

Automatic collection runs on whichever thread happens to allocate when a threshold is
crossed, such as an acquisition worker loading a spool for NWB writing. If it then
frees a cycle holding Qt objects (an earlier window, its widgets, their handlers), Qt
destroys them off the GUI thread, which can deadlock or crash. CI showed exactly that:
a recording's worker in ``load_session_spool`` while the GUI thread never returned.

``install_main_thread_gc`` disables automatic collection and collects on a GUI-thread
timer instead, with the same per-generation thresholds (the approach of pyqtgraph's
``GarbageCollector``, written as a QObject so its timer connection is a QObject method).
"""

from __future__ import annotations

import gc

from PySide6.QtCore import QCoreApplication, QObject, QTimer

INTERVAL_MS = 1000


class _MainThreadCollector(QObject):
    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self._thresholds = gc.get_threshold()
        self._timer = QTimer(self)
        self._timer.setInterval(INTERVAL_MS)
        self._timer.timeout.connect(self.collect)
        gc.disable()
        self._timer.start()

    def collect(self) -> None:
        """Collect each generation whose count passed its threshold, as gc would."""

        count0, count1, count2 = gc.get_count()
        if count0 > self._thresholds[0]:
            gc.collect(0)
            if count1 > self._thresholds[1]:
                gc.collect(1)
                if count2 > self._thresholds[2]:
                    gc.collect(2)


def install_main_thread_gc() -> bool:
    """Collect only on the GUI thread from now on; True if this call installed it.

    Call from the GUI thread once a QApplication exists. Later calls do nothing.
    """

    app = QCoreApplication.instance()
    if app is None:
        raise RuntimeError("install_main_thread_gc needs a running QApplication")
    if app.findChild(_MainThreadCollector) is not None:
        return False
    _MainThreadCollector(app)
    return True
