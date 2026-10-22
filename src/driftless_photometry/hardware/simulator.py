"""Deterministic camera, excitation, and TTL simulator."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime

import numpy as np

from driftless_photometry.config import (
    CUSTOM_PROFILE,
    SessionConfig,
    Wavelength,
    profile_for,
)
from driftless_photometry.domain import (
    ExposureRecord,
    FramePacket,
    SystemEvent,
    TTLEdge,
)
from driftless_photometry.hardware.base import ExcitationCycle, RigPacket, StartCondition
from driftless_photometry.roi import circular_mask
from driftless_photometry.timing import WavelengthSchedule

_BASE_SIGNAL = {
    Wavelength.CONTROL_405: 500.0,
    Wavelength.GREEN_470: 1100.0,
    Wavelength.RED_565: 850.0,
}


class SimulatedRig:
    """A repeatable software rig using the production frame/event contracts."""

    adapter_name = "driftless_simulator"
    adapter_version = "1"
    protocol_version = "simulated_explicit_exposure_v1"

    def __init__(
        self,
        config: SessionConfig,
        *,
        seed: int = 12345,
        realtime: bool = False,
    ) -> None:
        if profile_for(config.source) is not CUSTOM_PROFILE:
            raise ValueError(
                "the simulator stands in for the Custom system; RWD sessions run on RWDRig"
            )
        self._config = config
        self._rng = np.random.default_rng(seed)
        self._schedule = WavelengthSchedule(config.channels)
        self._realtime = realtime
        self._stop = threading.Event()
        self._masks = tuple(
            circular_mask((config.camera.height_px, config.camera.width_px), roi)
            for roi in config.rois
        )

    def stop(self) -> None:
        self._stop.set()

    def packets(
        self,
        duration_s: float,
        start: StartCondition | None = None,
        cycle: ExcitationCycle | None = None,
    ) -> Iterator[RigPacket]:
        if duration_s <= 0:
            raise ValueError("duration_s must be positive")
        # Clear now, not when the generator starts: a Stop that arrives before the
        # first packet is requested must still end the session.
        self._stop.clear()
        return self._packets(duration_s, start, cycle)

    def _packets(
        self, duration_s: float, start: StartCondition | None, cycle: ExcitationCycle | None
    ) -> Iterator[RigPacket]:
        start_events: tuple[SystemEvent, ...] = ()
        if start is not None:
            # A simulated TTL input pulses every ``line`` seconds, so wait one period.
            wait_s = start.delay_s if start.ttl_line is None else float(start.ttl_line)
            if self._realtime and self._stop.wait(wait_s):
                return
            start_events = (
                SystemEvent(0.0, "recording_start_trigger", f"simulated {start.describe()}"),
            )
        fps = self._config.camera.requested_fps
        frame_count = max(1, round(duration_s * fps))
        started_monotonic = time.monotonic()
        started_utc = datetime.now(UTC)
        previous_time = -1.0
        ttl_sequence = 0
        sequence = 0
        was_lit = True
        events = list(start_events)
        for slot in range(frame_count):
            if self._stop.is_set():
                break
            timestamp_s = slot / fps
            if self._realtime:
                wait = started_monotonic + timestamp_s - time.monotonic()
                if wait > 0 and self._stop.wait(wait):
                    break
            lit = cycle is None or timestamp_s % cycle.period_s < cycle.on_s
            if lit != was_lit:
                window_s = timestamp_s - timestamp_s % cycle.period_s  # type: ignore[union-attr]
                events.append(
                    SystemEvent(window_s, "excitation_resumed", "sequential excitation")
                    if lit
                    else SystemEvent(
                        window_s + cycle.on_s,  # type: ignore[union-attr]
                        "excitation_paused",
                        f"sequential excitation: {cycle.describe()}",  # type: ignore[union-attr]
                    )
                )
                was_lit = lit
            if not lit:
                edges, ttl_sequence = self._ttl_edges_between(
                    previous_time, timestamp_s, ttl_sequence
                )
                previous_time = timestamp_s
                if edges or events:
                    yield RigPacket(frame=None, ttl_edges=edges, system_events=tuple(events))
                    events.clear()
                continue
            channel = self._schedule.channel_for_sequence(sequence)
            controller_tick_us = round(timestamp_s * 1_000_000)
            exposure = ExposureRecord(
                sequence=sequence,
                timestamp_s=timestamp_s,
                controller_tick_us=controller_tick_us,
                wavelength_nm=channel.wavelength_nm,
                commanded_voltage_v=channel.voltage_v,
            )
            image = self._render_frame(timestamp_s, channel.wavelength_nm)
            frame = FramePacket(
                frame_id=slot + 1,
                image=image,
                exposure=exposure,
                host_received_s=time.monotonic() - started_monotonic,
            )
            edges, ttl_sequence = self._ttl_edges_between(
                previous_time,
                timestamp_s,
                ttl_sequence,
            )
            previous_time = timestamp_s
            yield RigPacket(
                frame=frame,
                ttl_edges=edges,
                system_events=tuple(events),
                session_start_time=started_utc if sequence == 0 else None,
            )
            events.clear()
            sequence += 1

    def _render_frame(self, timestamp_s: float, wavelength: Wavelength) -> np.ndarray:
        camera = self._config.camera
        maximum = (1 << camera.bit_depth) - 1
        image = self._rng.normal(
            loc=180.0,
            scale=4.0,
            size=(camera.height_px, camera.width_px),
        )
        for index, mask in enumerate(self._masks):
            frequency_hz = 0.15 + index * 0.03
            modulation = 1.0 + 0.18 * np.sin(2 * np.pi * frequency_hz * timestamp_s + index)
            transient = 0.0
            if wavelength is not Wavelength.CONTROL_405:
                transient = 300.0 * np.exp(-((timestamp_s - 1.5 - index * 0.1) ** 2) / 0.08)
            image[mask] += _BASE_SIGNAL[wavelength] * modulation + transient
        return np.clip(np.rint(image), 0, maximum).astype(np.uint16)

    def _ttl_edges_between(
        self,
        previous_time_s: float,
        current_time_s: float,
        sequence: int,
    ) -> tuple[tuple[TTLEdge, ...], int]:
        edges: list[TTLEdge] = []
        for ttl in self._config.ttl_inputs:
            period_s = float(ttl.line)
            pulse_width_s = 0.10
            first_period = max(0, int(np.floor(previous_time_s / period_s)))
            last_period = int(np.floor(current_time_s / period_s))
            for period_index in range(first_period, last_period + 1):
                rising_time = period_index * period_s
                falling_time = rising_time + pulse_width_s
                for edge_time, edge_name, level in (
                    (rising_time, "rising", True),
                    (falling_time, "falling", False),
                ):
                    if previous_time_s < edge_time <= current_time_s:
                        if ttl.edges.value not in {"both", edge_name}:
                            continue
                        edges.append(
                            TTLEdge(
                                timestamp_s=edge_time,
                                controller_tick_us=round(edge_time * 1_000_000),
                                line=ttl.line,
                                label=ttl.label,
                                edge=edge_name,
                                new_level=level,
                                sequence=sequence,
                            )
                        )
                        sequence += 1
        edges.sort(key=lambda edge: (edge.timestamp_s, edge.line))
        return tuple(edges), sequence
