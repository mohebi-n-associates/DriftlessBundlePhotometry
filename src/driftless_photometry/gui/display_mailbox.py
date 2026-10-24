"""Bounded, display-only handoff; Qt queues notifications rather than camera arrays."""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass

import numpy as np

from driftless_photometry.acquisition import AcquisitionProgress, LiveFrame
from driftless_photometry.config import SessionConfig
from driftless_photometry.domain import EventMark, InvalidTimeInterval, TTLEdge
from driftless_photometry.live_trace import LiveTracePoint, camera_live_trace_points

from .trace_display import compact_history


@dataclass(frozen=True)
class DisplayBatch:
    images: tuple[AcquisitionProgress | LiveFrame, ...]
    points: tuple[LiveTracePoint, ...]
    marks: tuple[EventMark, ...]
    aggregated: bool
    edges: tuple[TTLEdge, ...] = ()
    invalid_times: tuple[InvalidTimeInterval, ...] = ()


class DisplayMailbox:
    """Keep latest images and bounded extrema histories while the GUI is busy.

    publish returns True only when a notification is needed. No recording data goes
    through this mailbox: the acquisition engine writes original samples independently.
    """

    MAX_POINTS = 2048
    MAX_MARKS = 1024
    MAX_EDGES = 4096

    def __init__(self, config: SessionConfig) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._notified = False
        self._images: dict[tuple[int, int], AcquisitionProgress | LiveFrame] = {}
        self._points: dict[tuple[str, int], list[LiveTracePoint]] = {}
        self._marks: deque[EventMark] = deque(maxlen=self.MAX_MARKS)
        self._edges: deque[TTLEdge] = deque(maxlen=self.MAX_EDGES)
        self._invalid: deque[InvalidTimeInterval] = deque(maxlen=self.MAX_MARKS)
        self._aggregated = False

    def publish(self, item: AcquisitionProgress | LiveFrame) -> bool:
        with self._lock:
            wavelength = int(
                item.sample.wavelength_nm
                if isinstance(item, AcquisitionProgress)
                else item.wavelength_nm
            )
            self._images[(item.camera_index, wavelength)] = item
            if isinstance(item, AcquisitionProgress):
                if len(self._marks) + len(item.event_marks) > self.MAX_MARKS:
                    self._aggregated = True
                self._marks.extend(item.event_marks)
                if len(self._edges) + len(item.ttl_edges) > self.MAX_EDGES:
                    self._aggregated = True
                self._edges.extend(item.ttl_edges)
                self._invalid.extend(item.invalid_times)
                for point in camera_live_trace_points(self._config, item):
                    key = (point.fiber_id, point.wavelength_nm)
                    points = self._points.setdefault(key, [])
                    points.append(point)
                    if len(points) > self.MAX_POINTS:
                        times, values = compact_history(
                            np.asarray([p.timestamp_s for p in points]),
                            np.asarray([p.value for p in points]),
                            self.MAX_POINTS // 2,
                        )
                        self._points[key] = [
                            LiveTracePoint(float(t), *key, float(v))
                            for t, v in zip(times, values, strict=True)
                        ]
                        self._aggregated = True
            notify = not self._notified
            self._notified = True
            return notify

    def take(self) -> DisplayBatch:
        with self._lock:
            batch = DisplayBatch(
                tuple(sorted(self._images.values(), key=lambda p: p.frame_count)),
                tuple(p for points in self._points.values() for p in points),
                tuple(self._marks),
                self._aggregated,
                tuple(self._edges),
                tuple(self._invalid),
            )
            self._images.clear()
            self._points.clear()
            self._marks.clear()
            self._edges.clear()
            self._invalid.clear()
            self._aggregated = False
            self._notified = False
            return batch
