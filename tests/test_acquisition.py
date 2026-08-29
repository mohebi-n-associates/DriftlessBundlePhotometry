from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from pynwb import NWBHDF5IO

from driftless_photometry.acquisition import AcquisitionEngine
from driftless_photometry.config import demo_config
from driftless_photometry.hardware import RigPacket, SimulatedRig
from driftless_photometry.settings import configuration_from_nwb
from driftless_photometry.state import AcquisitionState


def test_headless_engine_finalizes_valid_session_and_removes_spool(tmp_path: Path) -> None:
    config = demo_config(
        tmp_path,
        fiber_count=2,
        raw_capture=True,
        width_px=32,
        height_px=24,
    )
    states = []
    progress = []
    engine = AcquisitionEngine(spool_chunk_size=2, spool_queue_size=4)
    result = engine.run(
        config,
        SimulatedRig(config, seed=9),
        duration_s=0.2,
        on_state=states.append,
        on_progress=progress.append,
    )
    assert len(result.reports) == 2
    assert result.report.frame_count == 6
    assert len(progress) == 6
    assert engine.state is AcquisitionState.READY
    assert states == [
        AcquisitionState.READY,
        AcquisitionState.ARMED,
        AcquisitionState.RECORDING,
        AcquisitionState.DRAINING,
        AcquisitionState.READY,
    ]
    assert not list(tmp_path.glob("*.photometry-spool"))
    for roi_index, report in enumerate(result.reports):
        with NWBHDF5IO(report.path, mode="r", load_namespaces=True) as io:
            nwbfile = io.read()
            assert nwbfile.subject.subject_id == config.rois[roi_index].animal_id
            assert nwbfile.acquisition["camera_frames"].data.shape == (6, 24, 32)
            assert len(nwbfile.processing["photometry"]["camera_rois"]) == 1


def test_headless_engine_records_only_enabled_rois(tmp_path: Path) -> None:
    base = demo_config(tmp_path, fiber_count=2, width_px=32, height_px=24)
    config = base.model_copy(
        update={
            "rois": (
                base.rois[0].model_copy(update={"enabled": False}),
                base.rois[1],
            )
        }
    )
    progress = []

    result = AcquisitionEngine(spool_chunk_size=2).run(
        config,
        SimulatedRig(config, seed=11),
        duration_s=0.1,
        on_progress=progress.append,
    )

    assert len(result.reports) == 1
    assert all(len(item.sample.values) == 1 for item in progress)
    with NWBHDF5IO(result.report.path, mode="r", load_namespaces=True) as io:
        assert io.read().subject.subject_id == config.rois[1].animal_id
    restored, warnings = configuration_from_nwb(result.report.path)
    assert restored == config
    assert warnings == []


class _GapRig:
    def __init__(self, config) -> None:
        self._packets = list(SimulatedRig(config, seed=3).packets(0.2))

    def packets(self, duration_s: float) -> Iterator[RigPacket]:
        del duration_s
        yield self._packets[0]
        yield from self._packets[2:]

    def stop(self) -> None:
        pass


def test_sequence_gap_is_preserved_as_drop_event(tmp_path: Path) -> None:
    config = demo_config(
        tmp_path,
        fiber_count=1,
        raw_capture=False,
        width_px=32,
        height_px=24,
    )
    result = AcquisitionEngine(spool_chunk_size=2).run(
        config,
        _GapRig(config),
        duration_s=0.2,
    )
    with NWBHDF5IO(result.report.path, mode="r", load_namespaces=True) as io:
        dropped = io.read().events["dropped_frame_events"].to_dataframe()
        assert len(dropped) == 1
        assert dropped.iloc[0]["expected_sequence"] == 1
        assert dropped.iloc[0]["observed_sequence"] == 2
        assert dropped.iloc[0]["missing_count"] == 1


class _DuplicateSequenceRig(_GapRig):
    def packets(self, duration_s: float) -> Iterator[RigPacket]:
        del duration_s
        yield self._packets[0]
        yield self._packets[0]


def test_non_increasing_sequence_enters_error_and_preserves_spool(tmp_path: Path) -> None:
    config = demo_config(
        tmp_path,
        fiber_count=1,
        raw_capture=False,
        width_px=32,
        height_px=24,
    )
    engine = AcquisitionEngine(spool_chunk_size=2)
    with pytest.raises(RuntimeError, match="not strictly increasing"):
        engine.run(config, _DuplicateSequenceRig(config), duration_s=0.2)
    assert engine.state is AcquisitionState.ERROR
    assert len(list(tmp_path.glob("*.photometry-spool"))) == 1
    engine.reset_error()
    assert engine.state is AcquisitionState.READY


def test_stop_request_drains_received_frames_and_finalizes(tmp_path: Path) -> None:
    config = demo_config(
        tmp_path,
        fiber_count=1,
        raw_capture=True,
        width_px=32,
        height_px=24,
    )
    engine = AcquisitionEngine(spool_chunk_size=4)

    def stop_after_two(progress) -> None:
        if progress.frame_count == 2:
            engine.stop()

    result = engine.run(
        config,
        SimulatedRig(config, seed=7),
        duration_s=1.0,
        on_progress=stop_after_two,
    )

    assert result.stopped_by_request is True
    assert result.report.frame_count == 2
    assert engine.state is AcquisitionState.READY
    with NWBHDF5IO(result.report.path, mode="r", load_namespaces=True) as io:
        assert io.read().acquisition["camera_frames"].data.shape == (2, 24, 32)


class _BadTickRig(_GapRig):
    def packets(self, duration_s: float) -> Iterator[RigPacket]:
        del duration_s
        yield self._packets[0]
        second = self._packets[1]
        bad_exposure = replace(second.frame.exposure, controller_tick_us=0)
        yield replace(second, frame=replace(second.frame, exposure=bad_exposure))


def test_non_increasing_controller_tick_is_rejected(tmp_path: Path) -> None:
    config = demo_config(
        tmp_path,
        fiber_count=1,
        raw_capture=False,
        width_px=32,
        height_px=24,
    )
    engine = AcquisitionEngine(spool_chunk_size=2)
    with pytest.raises(RuntimeError, match="controller ticks are not strictly increasing"):
        engine.run(config, _BadTickRig(config), duration_s=0.2)
    assert engine.state is AcquisitionState.ERROR


def test_finalizer_failure_preserves_complete_spool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = demo_config(
        tmp_path,
        fiber_count=1,
        raw_capture=False,
        width_px=32,
        height_px=24,
    )

    def fail_finalization(*args, **kwargs):
        del args, kwargs
        raise OSError("simulated finalizer disk failure")

    monkeypatch.setattr("driftless_photometry.acquisition.write_session_nwbs", fail_finalization)
    engine = AcquisitionEngine(spool_chunk_size=2)
    with pytest.raises(OSError, match="simulated finalizer disk failure"):
        engine.run(config, SimulatedRig(config), duration_s=0.1)

    spools = list(tmp_path.glob("*.photometry-spool"))
    assert len(spools) == 1
    from driftless_photometry.storage import load_session_spool

    assert load_session_spool(spools[0]).complete is True
    assert engine.state is AcquisitionState.ERROR
