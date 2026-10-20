"""Dependency-injected contracts for camera/controller acquisition rigs."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol

from driftless_photometry.domain import FramePacket, InvalidTimeInterval, SystemEvent, TTLEdge


@dataclass(frozen=True, slots=True)
class RigPacket:
    """One camera frame and the events a source observed since its previous packet.

    ``frame`` is None only for a source's final packet, which carries events that
    arrived after its last frame.
    """

    frame: FramePacket | None
    ttl_edges: tuple[TTLEdge, ...] = ()
    invalid_times: tuple[InvalidTimeInterval, ...] = ()
    system_events: tuple[SystemEvent, ...] = ()


class RigSource(Protocol):
    def packets(self, duration_s: float) -> Iterator[RigPacket]:
        """Yield matched camera/controller packets for the requested duration."""

    def stop(self) -> None:
        """Request prompt, safe termination of packet production."""
