"""Deterministic camera, excitation, and TTL simulator."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator

import numpy as np

from driftless_photometry.config import SessionConfig, Wavelength
from driftless_photometry.domain import ExposureRecord, FramePacket, TTLEdge
from driftless_photometry.hardware.base import RigPacket
from driftless_photometry.roi import circular_mask
from driftless_photometry.timing import WavelengthSchedule

_BASE_SIGNAL = {
    Wavelength.CONTROL_405: 500.0,
    Wavelength.GREEN_470: 1100.0,
    Wavelength.RED_565: 850.0,
}


class SimulatedRig:
    """A repeatable software rig using the production frame/event contracts."""

    def __init__(
        self,
        config: SessionConfig,
        *,
        seed: int = 12345,
        realtime: bool = False,
    ) -> None:
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

    def packets(self, duration_s: float) -> Iterator[RigPacket]:
        if duration_s <= 0:
            raise ValueError("duration_s must be positive")
        self._stop.clear()
        fps = self._config.camera.requested_fps
        frame_count = max(1, round(duration_s * fps))
        started_monotonic = time.monotonic()
        previous_time = -1.0
        ttl_sequence = 0
        for sequence in range(frame_count):
            if self._stop.is_set():
                break
            timestamp_s = sequence / fps
            if self._realtime:
                wait = started_monotonic + timestamp_s - time.monotonic()
                if wait > 0 and self._stop.wait(wait):
                    break
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
                frame_id=sequence + 1,
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
            yield RigPacket(frame=frame, ttl_edges=edges)

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
