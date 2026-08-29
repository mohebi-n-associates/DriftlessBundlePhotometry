"""Memory-bounded frame stream abstraction used by NWB finalization."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True, slots=True)
class FrameStream:
    count: int
    frame_shape: tuple[int, int]
    iterator_factory: Callable[[], Iterator[np.ndarray]]
    first_frame: np.ndarray
    last_frame: np.ndarray

    def __post_init__(self) -> None:
        if self.count <= 0:
            raise ValueError("frame stream count must be positive")
        for frame in (self.first_frame, self.last_frame):
            if frame.dtype != np.uint16 or tuple(frame.shape) != self.frame_shape:
                raise TypeError("frame stream endpoints must be uint16 arrays of frame_shape")

    def iter_frames(self) -> Iterator[np.ndarray]:
        return self.iterator_factory()

    def to_array(self) -> np.ndarray:
        """Materialize the stream for tests or explicitly small recordings."""

        return np.stack(list(self.iter_frames())).astype(np.uint16, copy=False)
