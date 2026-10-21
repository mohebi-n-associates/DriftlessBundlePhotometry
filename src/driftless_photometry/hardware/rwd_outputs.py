"""TTL output programs: turn an output's configured steps into timed train starts and stops.

The R821 only configures a pulse train (frequency, width) and switches it on or off, so
delays, train lengths, gaps, cycles, steps, and repeats are sequenced by the host. A
program's times are on the host clock; each train start is reported with the step that
should be configured first.
"""

from __future__ import annotations

from dataclasses import dataclass

from driftless_photometry.config import RWDTTLOutputConfig


@dataclass(frozen=True, slots=True)
class OutputTrain:
    frequency_hz: float
    pulse_width_ms: int
    start_s: float
    stop_s: float | None
    """None: the train lasts until the session ends."""

    def describe(self, port: int) -> str:
        length = (
            "until the recording ends"
            if self.stop_s is None
            else (f"for {self.stop_s - self.start_s:g} s")
        )
        return f"output {port}: {self.frequency_hz:g} Hz, {self.pulse_width_ms} ms pulses {length}"


@dataclass(frozen=True, slots=True)
class OutputAction:
    at_s: float
    train: OutputTrain
    starts: bool
    """True switches the train on; False switches the output off."""


def program_trains(output: RWDTTLOutputConfig, triggered_s: float) -> tuple[OutputTrain, ...]:
    """Every train of one triggered run of ``output``'s program, in order."""

    steps = [
        (output.frequency_hz, output.pulse_width_ms, output.train_s, output.gap_s, output.cycles),
        *(
            (step.frequency_hz, step.pulse_width_ms, step.train_s, step.gap_s, step.cycles)
            for step in output.steps
        ),
    ]
    trains = []
    at_s = triggered_s + output.delay_s
    for _repeat in range(output.repeats):
        for frequency_hz, pulse_width_ms, train_s, gap_s, cycles in steps:
            for _cycle in range(cycles):
                stop_s = None if train_s is None else at_s + train_s
                trains.append(OutputTrain(frequency_hz, pulse_width_ms, at_s, stop_s))
                if stop_s is None:
                    return tuple(trains)
                at_s = stop_s + gap_s
    return tuple(trains)


class OutputProgramRun:
    """One triggered run of a port's program; ``due`` returns its actions in order."""

    def __init__(self, output: RWDTTLOutputConfig, triggered_s: float) -> None:
        self.output = output
        self.triggered_s = triggered_s
        actions = []
        for train in program_trains(output, triggered_s):
            actions.append(OutputAction(train.start_s, train, True))
            if train.stop_s is not None:
                actions.append(OutputAction(train.stop_s, train, False))
        self._actions = actions
        self._next = 0

    @property
    def done(self) -> bool:
        return self._next >= len(self._actions)

    def due(self, now_s: float) -> tuple[OutputAction, ...]:
        start = self._next
        while self._next < len(self._actions) and self._actions[self._next].at_s <= now_s:
            self._next += 1
        return tuple(self._actions[start : self._next])
