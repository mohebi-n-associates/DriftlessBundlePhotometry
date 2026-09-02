import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from pynwb import NWBHDF5IO

from driftless_photometry.acquisition import AcquisitionEngine, preview_duration_s, run_preview
from driftless_photometry.config import RWDChannelMapping, RWDSourceConfig, demo_config
from driftless_photometry.diagnostics import FinalizationStage
from driftless_photometry.faults import AcquisitionFault, AcquisitionFaultCode
from driftless_photometry.hardware import RigPacket, SimulatedRig
from driftless_photometry.provenance import NWB_RUNTIME_PROVENANCE_SCRATCH_NAME
from driftless_photometry.settings import configuration_from_nwb
from driftless_photometry.state import AcquisitionState
from driftless_photometry.storage import InsufficientStorageError, SessionSpool, load_session_spool


def test_preview_validates_all_channels_without_creating_output(tmp_path: Path) -> None:
    config = demo_config(tmp_path, fiber_count=2, width_px=32, height_px=24)
    progress = []

    result = run_preview(config, SimulatedRig(config, seed=17), on_progress=progress.append)

    assert result.duration_s == preview_duration_s(config)
    assert result.frame_count == len(progress) == 15
    assert result.roi_count == 2
    assert all(count == 5 for _wavelength, count in result.wavelength_frame_counts)
    assert 0 <= result.maximum_saturation_fraction <= 1
    assert not list(tmp_path.iterdir())


def test_native_services_reject_rwd_source_before_creating_output(tmp_path: Path) -> None:
    base = demo_config(tmp_path, fiber_count=1, width_px=32, height_px=24)
    rwd_config = base.model_copy(
        update={
            "source": RWDSourceConfig(
                channel_mappings=(
                    RWDChannelMapping(
                        device_channel=0,
                        fiber_id="fiber_01",
                        label="RWD channel 0",
                    ),
                )
            )
        }
    )

    with pytest.raises(ValueError, match="native preview"):
        run_preview(rwd_config, SimulatedRig(rwd_config))
    with pytest.raises(ValueError, match="native acquisition engine"):
        AcquisitionEngine().run(
            rwd_config,
            SimulatedRig(rwd_config),
            duration_s=0.1,
        )
    with pytest.raises(ValueError, match="native session spool"):
        SessionSpool(rwd_config)

    assert not list(tmp_path.iterdir())


class _IncompletePreviewRig:
    def __init__(self, config) -> None:
        self._source = SimulatedRig(config, seed=19)

    def packets(self, duration_s: float) -> Iterator[RigPacket]:
        for packet in self._source.packets(duration_s):
            if int(packet.frame.exposure.wavelength_nm) == 470:
                yield packet

    def stop(self) -> None:
        self._source.stop()


def test_preview_rejects_missing_enabled_wavelengths(tmp_path: Path) -> None:
    config = demo_config(tmp_path, fiber_count=1, width_px=32, height_px=24)

    with pytest.raises(RuntimeError, match="did not observe two frames"):
        run_preview(config, _IncompletePreviewRig(config))


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
    finalization = []
    engine = AcquisitionEngine(spool_chunk_size=2, spool_queue_size=4)
    result = engine.run(
        config,
        SimulatedRig(config, seed=9),
        duration_s=0.2,
        on_state=states.append,
        on_progress=progress.append,
        on_finalization=finalization.append,
    )
    assert len(result.reports) == 2
    assert result.report.frame_count == 6
    assert len(progress) == 6
    assert [item.stage for item in finalization] == [
        FinalizationStage.PREPARING,
        FinalizationStage.WRITING,
        FinalizationStage.VALIDATING,
        FinalizationStage.WRITING,
        FinalizationStage.VALIDATING,
        FinalizationStage.PROMOTING,
        FinalizationStage.COMPLETE,
    ]
    assert result.diagnostics.spool.committed_frames == 6
    assert result.diagnostics.spool.committed_chunks == 3
    assert result.diagnostics.spool.maximum_write_latency_s >= 0
    assert result.storage_capacity.estimated_frame_count == 6
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
            provenance = json.loads(str(nwbfile.get_scratch(NWB_RUNTIME_PROVENANCE_SCRATCH_NAME)))
            assert provenance["adapter_name"] == "driftless_simulator"
            assert provenance["protocol_version"] == "simulated_explicit_exposure_v1"


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
    assert result.diagnostics.dropped_frames == 1
    with NWBHDF5IO(result.report.path, mode="r", load_namespaces=True) as io:
        nwbfile = io.read()
        dropped = nwbfile.events["dropped_frame_events"].to_dataframe()
        assert len(dropped) == 1
        assert dropped.iloc[0]["expected_sequence"] == 1
        assert dropped.iloc[0]["observed_sequence"] == 2
        assert dropped.iloc[0]["missing_count"] == 1
        invalid = nwbfile.intervals["invalid_times"].to_dataframe()
        assert len(invalid) == 1
        assert invalid.iloc[0]["source_event"] == "controller_sequence_gap"


class _ClockResidualRig(_GapRig):
    def packets(self, duration_s: float) -> Iterator[RigPacket]:
        del duration_s
        first, second = self._packets[:2]
        yield replace(first, frame=replace(first.frame, host_received_s=0.010))
        yield replace(
            second,
            frame=replace(
                second.frame,
                host_received_s=second.frame.exposure.timestamp_s + 0.015,
            ),
        )


def test_clock_residual_diagnostic_tracks_transport_offset_change(tmp_path: Path) -> None:
    config = demo_config(tmp_path, fiber_count=1, width_px=32, height_px=24)

    result = AcquisitionEngine(spool_chunk_size=2).run(
        config,
        _ClockResidualRig(config),
        duration_s=0.2,
    )

    assert result.diagnostics.clock_residual_s == pytest.approx(0.005)
    assert result.diagnostics.maximum_absolute_clock_residual_s == pytest.approx(0.005)


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
    loaded = load_session_spool(next(tmp_path.glob("*.photometry-spool")))
    assert loaded.data.system_events[-1].event == "acquisition_fault:clock_discontinuity"
    assert loaded.data.invalid_times[-1].source_event == "clock_discontinuity"


class _FaultingRig:
    def __init__(self, config, code: AcquisitionFaultCode) -> None:
        self._first = next(SimulatedRig(config, seed=31).packets(0.1))
        self._code = code
        self.stop_called = False

    def packets(self, duration_s: float) -> Iterator[RigPacket]:
        del duration_s
        yield self._first
        raise AcquisitionFault(self._code, f"simulated {self._code.value}")

    def stop(self) -> None:
        self.stop_called = True


@pytest.mark.parametrize(
    "fault_code",
    [
        AcquisitionFaultCode.CAMERA_DISCONNECT,
        AcquisitionFaultCode.CONTROLLER_DISCONNECT,
        AcquisitionFaultCode.MALFORMED_CONTROLLER_STREAM,
    ],
)
def test_source_fault_is_committed_before_incomplete_spool_is_closed(
    tmp_path: Path,
    fault_code: AcquisitionFaultCode,
) -> None:
    config = demo_config(tmp_path, fiber_count=1, width_px=32, height_px=24)
    source = _FaultingRig(config, fault_code)
    engine = AcquisitionEngine(spool_chunk_size=4)

    with pytest.raises(AcquisitionFault, match="simulated"):
        engine.run(config, source, duration_s=0.2)

    loaded = load_session_spool(next(tmp_path.glob("*.photometry-spool")))
    assert loaded.complete is False
    assert loaded.data.frame_ids == [1]
    assert loaded.data.system_events[-1].event == f"acquisition_fault:{fault_code.value}"
    assert loaded.data.invalid_times[-1].source_event == fault_code.value
    assert source.stop_called is True
    assert engine.state is AcquisitionState.ERROR


def test_insufficient_storage_fails_before_arming_or_creating_a_spool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = demo_config(tmp_path, fiber_count=1, width_px=32, height_px=24)
    source = _FaultingRig(config, AcquisitionFaultCode.CAMERA_DISCONNECT)

    def fail_capacity(*args, **kwargs):
        del args, kwargs
        raise InsufficientStorageError("simulated insufficient disk")

    monkeypatch.setattr("driftless_photometry.acquisition.check_storage_capacity", fail_capacity)
    engine = AcquisitionEngine()
    with pytest.raises(InsufficientStorageError, match="insufficient disk"):
        engine.run(config, source, duration_s=0.2)

    assert source.stop_called is True
    assert engine.state is AcquisitionState.ERROR
    assert not list(tmp_path.glob("*.photometry-spool"))


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
