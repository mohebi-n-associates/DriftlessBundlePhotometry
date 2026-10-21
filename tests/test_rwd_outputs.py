from __future__ import annotations

import pytest
from pydantic import ValidationError

from driftless_photometry.config import (
    DffTriggerConfig,
    RWDTTLOutputConfig,
    RWDTTLOutputStepConfig,
)
from driftless_photometry.hardware.rwd_outputs import OutputProgramRun, program_trains


def test_a_plain_output_is_one_train_until_the_recording_ends() -> None:
    output = RWDTTLOutputConfig(port=1, frequency_hz=5.0, pulse_width_ms=50)

    (train,) = program_trains(output, 10.0)

    assert (train.start_s, train.stop_s) == (10.0, None)
    assert output.program_s is None
    assert train.describe(1) == "output 1: 5 Hz, 50 ms pulses until the recording ends"


def test_programs_run_delay_cycles_steps_and_repeats_in_order() -> None:
    output = RWDTTLOutputConfig(
        port=2,
        frequency_hz=20.0,
        pulse_width_ms=10,
        delay_s=1.0,
        train_s=2.0,
        gap_s=3.0,
        cycles=2,
        steps=(RWDTTLOutputStepConfig(frequency_hz=40.0, pulse_width_ms=5, train_s=1.0),),
        repeats=2,
    )

    trains = program_trains(output, 0.0)

    assert [(t.frequency_hz, t.start_s, t.stop_s) for t in trains] == [
        (20.0, 1.0, 3.0),
        (20.0, 6.0, 8.0),
        (40.0, 11.0, 12.0),
        (20.0, 12.0, 14.0),
        (20.0, 17.0, 19.0),
        (40.0, 22.0, 23.0),
    ]
    assert output.program_s == pytest.approx(23.0)


def test_a_program_run_hands_out_each_action_once() -> None:
    output = RWDTTLOutputConfig(
        port=3,
        frequency_hz=10.0,
        pulse_width_ms=20,
        train_s=1.0,
        gap_s=1.0,
        cycles=2,
        trigger="manual",
    )
    run = OutputProgramRun(output, 5.0)

    assert run.due(4.9) == ()
    first = run.due(6.0)
    assert [(action.at_s, action.starts) for action in first] == [(5.0, True), (6.0, False)]
    assert run.due(6.5) == ()
    assert [action.starts for action in run.due(100.0)] == [True, False]
    assert run.done


def test_program_settings_are_checked() -> None:
    with pytest.raises(ValidationError, match="only a single train"):
        RWDTTLOutputConfig(port=1, frequency_hz=5.0, pulse_width_ms=50, cycles=2)
    with pytest.raises(ValidationError, match="needs a train length"):
        RWDTTLOutputConfig(port=1, frequency_hz=5.0, pulse_width_ms=50, trigger="manual")
    with pytest.raises(ValidationError, match="needs its threshold settings"):
        RWDTTLOutputConfig(
            port=1, frequency_hz=5.0, pulse_width_ms=50, train_s=1.0, trigger="dff_threshold"
        )
    with pytest.raises(ValidationError, match="does not fit"):
        RWDTTLOutputConfig(
            port=1,
            frequency_hz=5.0,
            pulse_width_ms=50,
            train_s=1.0,
            steps=(RWDTTLOutputStepConfig(frequency_hz=100.0, pulse_width_ms=10, train_s=1),),
        )
    RWDTTLOutputConfig(
        port=1,
        frequency_hz=5.0,
        pulse_width_ms=50,
        train_s=1.0,
        trigger="dff_threshold",
        dff_trigger=DffTriggerConfig(fiber_id="fiber_01"),
    )
