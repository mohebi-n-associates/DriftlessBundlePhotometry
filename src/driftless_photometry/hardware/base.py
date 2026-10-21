"""Dependency-injected contracts for camera/controller acquisition rigs."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol

from driftless_photometry.config import SessionConfig
from driftless_photometry.domain import FramePacket, InvalidTimeInterval, SystemEvent, TTLEdge


@dataclass(frozen=True, slots=True)
class RigPacket:
    """One camera frame and the events a source observed since its previous packet.

    ``frame`` is None for packets that carry only events: a source's final packet,
    or events observed while sequential excitation keeps the LEDs dark.
    """

    frame: FramePacket | None
    ttl_edges: tuple[TTLEdge, ...] = ()
    invalid_times: tuple[InvalidTimeInterval, ...] = ()
    system_events: tuple[SystemEvent, ...] = ()


@dataclass(frozen=True, slots=True)
class StartCondition:
    """Hold a recording's first exposure until a delay has passed or a TTL edge arrives.

    A source connects and prepares its hardware first, so the wait ends close to the
    first exposure. The excitation light stays off while the source waits.
    """

    delay_s: float = 0.0
    ttl_line: int | None = None
    ttl_edge: str = "rising"

    @classmethod
    def for_recording(cls, config: SessionConfig) -> StartCondition | None:
        """The recording's start condition; None when it starts at once."""

        recording = config.recording
        if recording.start == "delayed":
            return cls(delay_s=recording.start_delay_s)
        if recording.start == "ttl":
            return cls(ttl_line=recording.start_ttl_line, ttl_edge=recording.start_ttl_edge)
        return None

    def describe(self) -> str:
        if self.ttl_line is not None:
            return f"a {self.ttl_edge} edge on TTL input {self.ttl_line}"
        return f"a {self.delay_s:g} s delay"


@dataclass(frozen=True, slots=True)
class ExcitationCycle:
    """Sequential excitation: lit for ``on_s`` of session time, then dark for ``off_s``.

    Windows start at session zero, so window ``k`` is lit from ``k * period_s``. While
    dark, a source records no frames but keeps reporting TTL inputs where it can.
    """

    on_s: float
    off_s: float

    def __post_init__(self) -> None:
        if self.on_s <= 0 or self.off_s <= 0:
            raise ValueError("sequential excitation needs positive on and off times")

    @property
    def period_s(self) -> float:
        return self.on_s + self.off_s

    @classmethod
    def for_recording(cls, config: SessionConfig) -> ExcitationCycle | None:
        recording = config.recording
        if recording.excitation != "sequential":
            return None
        return cls(recording.excitation_on_s, recording.excitation_off_s)

    def describe(self) -> str:
        return f"{self.on_s:g} s on, {self.off_s:g} s off"


class RigSource(Protocol):
    def packets(
        self,
        duration_s: float,
        start: StartCondition | None = None,
        cycle: ExcitationCycle | None = None,
    ) -> Iterator[RigPacket]:
        """Yield matched camera/controller packets for the requested duration.

        With ``start``, the duration counts from the first exposure after it is met.
        With ``cycle``, frames are recorded only in its lit windows; frame-less packets
        may carry the TTL edges and events observed while dark.
        """

    def stop(self) -> None:
        """Request prompt, safe termination of packet production."""
