import gc

from driftless_photometry.gui.gc_thread import _MainThreadCollector, install_main_thread_gc


def test_collection_runs_on_the_gui_thread_timer(qtbot, monkeypatch) -> None:
    from PySide6.QtCore import QCoreApplication

    install_main_thread_gc()
    assert not gc.isenabled()  # never on an arbitrary allocating thread
    assert install_main_thread_gc() is False  # installed once per application
    collector = QCoreApplication.instance().findChild(_MainThreadCollector)

    collected = []
    monkeypatch.setattr(gc, "collect", lambda generation=2: collected.append(generation) or 0)
    monkeypatch.setattr(gc, "get_count", lambda: (10**6, 10**6, 10**6))
    collector.collect()
    assert collected == [0, 1, 2]
    collected.clear()
    monkeypatch.setattr(gc, "get_count", lambda: (0, 0, 0))
    collector.collect()
    assert collected == []
