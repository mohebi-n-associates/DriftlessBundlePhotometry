import json

import pytest

from driftless_photometry.gui.crosstalk import CrosstalkDialog, crosstalk_percent


def test_crosstalk_is_the_other_rois_rise_as_a_share_of_the_lit_fibers_rise() -> None:
    dark = {470: {"a": 100.0, "b": 110.0, "c": 90.0}}
    lit = {
        "a": {470: {"a": 1100.0, "b": 160.0, "c": 90.0}},
        "b": {470: {"a": 105.0, "b": 115.0, "c": 95.0}},  # target barely lit b
    }
    result = crosstalk_percent(dark, lit)
    assert result[470]["a"] == pytest.approx({"b": 5.0, "c": 0.0})
    assert result[470]["b"] == {"a": None, "c": None}  # too dim to say


def test_dialog_captures_dark_then_each_fiber_and_summarises(qtbot) -> None:
    saved = []
    dialog = CrosstalkDialog(None, fibers=[("a", "ROI 1"), ("b", "ROI 2")], on_save=saved.append)
    qtbot.addWidget(dialog)
    assert not dialog.dark_button.isEnabled()  # needs live view
    dialog.set_live(True)
    assert dialog.dark_button.isEnabled() and not dialog.fiber_button.isEnabled()

    def frames(values, start_s):
        for index in range(30):
            dialog.add_frame(470, start_s + index / 10, values)

    dialog.dark_button.click()
    assert not dialog.dark_button.isEnabled()
    frames({"a": 100.0, "b": 100.0}, 0.0)
    assert dialog.dark == {470: {"a": 100.0, "b": 100.0}}
    assert dialog.fiber_button.isEnabled()

    dialog.fiber_combo.setCurrentIndex(1)
    dialog.fiber_button.click()
    frames({"a": 120.0, "b": 1100.0}, 10.0)
    assert dialog.result() == {470: {"b": {"a": pytest.approx(2.0)}}}
    assert dialog.table.item(1, 0).text() == "2.0 %"
    assert dialog.table.item(0, 0).text() == "—"

    dialog.save_button.click()
    (summary,) = saved
    assert summary.startswith("470 nm max 2.0 % (b -> a)")
    assert json.loads(summary.split("; percent ", 1)[1]) == {"470": {"b": {"a": 2.0}}}

    dialog.set_live(False)  # live view stopped: no capture can be left running
    assert not dialog.capturing and not dialog.dark_button.isEnabled()
