from pathlib import Path

import numpy as np

from driftless_photometry.config import Wavelength, demo_config
from driftless_photometry.hardware import SimulatedRig
from driftless_photometry.roi import extract_circular_rois


def test_simulator_is_deterministic_and_explicitly_cycles_wavelengths(tmp_path: Path) -> None:
    config = demo_config(tmp_path, fiber_count=2, width_px=48, height_px=32)
    first = list(SimulatedRig(config, seed=7).packets(0.2))
    second = list(SimulatedRig(config, seed=7).packets(0.2))
    assert len(first) == 6
    assert [packet.frame.exposure.wavelength_nm for packet in first] == list(Wavelength) * 2
    for left, right in zip(first, second, strict=True):
        np.testing.assert_array_equal(left.frame.image, right.frame.image)
        assert left.frame.exposure == right.frame.exposure


def test_simulator_emits_extractable_fiber_signal_and_both_ttl_edges(tmp_path: Path) -> None:
    config = demo_config(tmp_path, fiber_count=1, width_px=48, height_px=32)
    packets = list(SimulatedRig(config, seed=11).packets(0.2))
    extracted = extract_circular_rois(
        packets[1].frame.image,
        config.rois,
        bit_depth=config.camera.bit_depth,
    )
    assert extracted.values[0] > 500
    edges = [edge.edge for packet in packets for edge in packet.ttl_edges if edge.line == 1]
    assert edges == ["rising", "falling"]
