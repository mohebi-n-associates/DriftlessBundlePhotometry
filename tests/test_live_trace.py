from pathlib import Path

import numpy as np

from driftless_photometry.acquisition import AcquisitionProgress
from driftless_photometry.config import RWDChannelMapping, RWDSourceConfig, demo_config
from driftless_photometry.domain import TraceSample
from driftless_photometry.live_trace import RWDLiveTraceProjector, native_live_trace_points
from driftless_photometry.rwd import (
    RWDEventRecord,
    RWDFluorescenceRecord,
    RWDFluorescenceSample,
    RWDReceivedRecord,
    RWDWavelength,
)


def test_native_live_trace_projection_preserves_stable_fibers_and_values(
    tmp_path: Path,
) -> None:
    config = demo_config(tmp_path, fiber_count=2)
    sample = TraceSample(
        timestamp_s=1.25,
        frame_id=4,
        sequence=3,
        controller_tick_us=1_250_000,
        wavelength_nm=config.enabled_channels[1].wavelength_nm,
        values=np.asarray([10.5, 20.5], dtype=np.float32),
        saturation_fractions=np.zeros(2, dtype=np.float32),
    )
    progress = AcquisitionProgress(4, sample, np.zeros((256, 256), dtype=np.uint16))

    points = native_live_trace_points(config, progress)

    assert [(point.fiber_id, point.wavelength_nm, point.value) for point in points] == [
        ("fiber_01", 470, 10.5),
        ("fiber_02", 470, 20.5),
    ]
    assert all(point.timestamp_s == 1.25 for point in points)


def test_rwd_live_trace_projection_maps_async_values_without_mutating_raw_records(
    tmp_path: Path,
) -> None:
    base = demo_config(tmp_path, fiber_count=1)
    source = RWDSourceConfig(
        timestamp_scale_s=0.001,
        channel_mappings=(
            RWDChannelMapping(device_channel=7, fiber_id="fiber_01", label="Animal A"),
        ),
    )
    config = base.model_copy(update={"source": source})
    record = RWDFluorescenceRecord(
        machine_name=b"RWD1",
        device_channel=7,
        samples=(
            RWDFluorescenceSample(RWDWavelength.LED_410, 100, 1000, 1.0),
            RWDFluorescenceSample(RWDWavelength.LED_560, 102, 3000, 3.0),
        ),
    )
    projector = RWDLiveTraceProjector(config)

    first = projector.project(RWDReceivedRecord(0, 0.01, record))
    second = projector.project(
        RWDReceivedRecord(
            1,
            0.02,
            RWDFluorescenceRecord(
                machine_name=b"RWD1",
                device_channel=7,
                samples=(RWDFluorescenceSample(RWDWavelength.LED_410, 110, 2000, 2.0),),
            ),
        )
    )
    event_points = projector.project(
        RWDReceivedRecord(
            2,
            0.03,
            RWDEventRecord(b"RWD1", 111, b"event".ljust(20, b"\x00"), 0),
        )
    )

    assert [(point.wavelength_nm, point.timestamp_s, point.value) for point in first] == [
        (410, 0.0, 1.0),
        (560, 0.002, 3.0),
    ]
    assert [(point.timestamp_s, point.value) for point in second] == [(0.01, 2.0)]
    assert event_points == ()
    assert record.samples[0].raw_value == 1000
