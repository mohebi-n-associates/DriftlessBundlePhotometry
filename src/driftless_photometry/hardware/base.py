"""Dependency-injected contracts for camera/controller acquisition rigs."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol

from driftless_photometry.domain import FramePacket, TTLEdge


@dataclass(frozen=True, slots=True)
class RigPacket:
    frame: FramePacket
    ttl_edges: tuple[TTLEdge, ...] = ()


class RigSource(Protocol):
    def packets(self, duration_s: float) -> Iterator[RigPacket]:
        """Yield matched camera/controller packets for the requested duration."""

    def stop(self) -> None:
        """Request prompt, safe termination of packet production."""
