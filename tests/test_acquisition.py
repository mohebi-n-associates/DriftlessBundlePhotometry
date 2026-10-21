import json
import threading
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from pynwb import NWBHDF5IO

from driftless_photometry.acquisition import (
    AcquisitionCancelled,
    AcquisitionEngine,
    preview_duration_s,
    run_live_view,
    run_preview,
    spool_chunk_size_for,
    spool_queue_size_for,
)
from driftless_photometry.config import SessionConfig, demo_config
from driftless_photometry.diagnostics import FinalizationStage
from driftless_photometry.domain import InvalidTimeInterval, SystemEvent, TTLEdge
from driftless_photometry.faults import AcquisitionFault, AcquisitionFaultCode
from driftless_photometry.hardware import (
    ExcitationCycle,
    RigPacket,
    SimulatedRig,
    StartCondition,
)
from driftless_photometry.provenance import NWB_RUNTIME_PROVENANCE_SCRATCH_NAME
from driftless_photometry.settings import configuration_from_nwb
from driftless_photometry.state import AcquisitionState
from driftless_photometry.storage import InsufficientStorageError, load_session_spool


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


def test_simulator_refuses_rwd_settings(tmp_path: Path) -> None:
    rwd_config = demo_config(tmp_path, fiber_count=1, system="rwd")

    with pytest.raises(ValueError, match="RWD sessions run on RWDRig"):
        SimulatedRig(rwd_config)


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


class _EventRig:
    """Simulator frames plus source-reported spans and events, then a frame-less packet."""

    def __init__(self, config) -> None:
        self._source = SimulatedRig(config, seed=23)

    def packets(self, duration_s: float) -> Iterator[RigPacket]:
        for index, packet in enumerate(self._source.packets(duration_s)):
            if index == 1:
                packet = replace(
                    packet,
                    invalid_times=(
                        InvalidTimeInterval(0.01, 0.02, "source restart", "source_restart"),
                    ),
                    system_events=(SystemEvent(0.01, "source_restart", "reason"),),
                )
            yield packet
        edge = TTLEdge(
            timestamp_s=0.09,
            controller_tick_us=90_000,
            line=1,
            label="behavior_1",
            edge="rising",
            new_level=True,
            sequence=0,
        )
        yield RigPacket(
            frame=None,
            ttl_edges=(edge,),
            system_events=(SystemEvent(0.09, "source_stopped"),),
        )

    def stop(self) -> None:
        self._source.stop()


def test_engine_records_source_spans_events_and_final_frameless_packet(tmp_path: Path) -> None:
    config = demo_config(tmp_path, fiber_count=1, width_px=32, height_px=24)

    result = AcquisitionEngine().run(config, _EventRig(config), duration_s=0.1)

    assert result.report.frame_count == 3
    with NWBHDF5IO(result.report.path, mode="r", load_namespaces=True) as io:
        nwbfile = io.read()
        invalid = nwbfile.intervals["invalid_times"].to_dataframe()
        assert invalid["source_event"].tolist() == ["source_restart"]
        events = nwbfile.events["system_events"].to_dataframe()["event"].tolist()
        assert "source_restart" in events
        assert "source_stopped" in events
        ttl = nwbfile.events["ttl_edges"].to_dataframe()
        assert 0.09 in ttl["timestamp"].round(6).tolist()


def test_preview_ignores_frameless_packets(tmp_path: Path) -> None:
    config = demo_config(tmp_path, fiber_count=1, width_px=32, height_px=24)

    result = run_preview(config, _EventRig(config))

    assert result.frame_count == 15


def test_live_view_streams_without_recording_and_throttles_the_display(tmp_path: Path) -> None:
    config = demo_config(tmp_path, fiber_count=1, width_px=32, height_px=24)
    shown = []

    count = run_live_view(config, SimulatedRig(config), on_frame=shown.append, duration_s=1.0)

    assert count == 30
    # Simulated frames arrive within microseconds, so each camera/wavelength shows once.
    assert sorted(int(frame.wavelength_nm) for frame in shown) == [405, 470, 565]
    assert not list(tmp_path.iterdir())


def test_live_view_stops_when_its_source_is_stopped(tmp_path: Path) -> None:
    config = demo_config(tmp_path, fiber_count=1, width_px=32, height_px=24)
    rig = SimulatedRig(config)
    shown = []

    def stop_after_first(frame) -> None:
        shown.append(frame)
        rig.stop()

    count = run_live_view(config, rig, on_frame=stop_after_first, duration_s=60.0)

    assert count == 1
    assert len(shown) == 1


@pytest.mark.parametrize(("fps", "chunk"), [(20.0, 16), (64.0, 16), (120.0, 30), (250.0, 62)])
def test_spool_chunks_hold_about_a_quarter_second(tmp_path: Path, fps, chunk) -> None:
    payload = demo_config(tmp_path, fiber_count=1).model_dump()
    payload["camera"]["requested_fps"] = fps
    config = SessionConfig.model_validate(payload)

    assert spool_chunk_size_for(config) == chunk


@pytest.mark.parametrize(
    ("fps", "raw", "frames"),
    [(1.0, False, 64), (20.0, False, 600), (120.0, False, 3600), (120.0, True, 1262)],
)
def test_spool_queue_holds_seconds_of_frames(tmp_path: Path, fps, raw, frames) -> None:
    config = demo_config(tmp_path, fiber_count=1, system="rwd")
    camera = config.camera.model_copy(update={"requested_fps": fps, "raw_capture": raw})

    assert spool_queue_size_for(config.model_copy(update={"camera": camera})) == frames


def _with_recording(config: SessionConfig, **recording) -> SessionConfig:
    payload = config.model_dump()
    payload["recording"] = recording
    return SessionConfig.model_validate(payload)


def test_recording_stops_on_a_ttl_edge_and_records_why(tmp_path: Path) -> None:
    config = _with_recording(
        demo_config(tmp_path, recording_duration_s=5.0),
        stop="ttl",
        stop_ttl_line=2,
        stop_ttl_edge="falling",
    )

    result = AcquisitionEngine().run(config, SimulatedRig(config), duration_s=5.0)

    assert result.stopped_by_request
    with NWBHDF5IO(result.report.path, mode="r", load_namespaces=True) as io:
        nwbfile = io.read()
        events = nwbfile.events["system_events"].to_dataframe()
        trigger = events[events["event"] == "recording_stop_trigger"]
        assert len(trigger) == 1
        assert trigger["timestamp"].iloc[0] == pytest.approx(0.1)
        assert "falling edge on input 2" in trigger["detail"].iloc[0]
        frames = nwbfile.events["camera_frames"].to_dataframe()
        assert frames["timestamp"].max() < 0.2


def test_stopping_while_waiting_for_the_start_records_nothing(tmp_path: Path) -> None:
    config = _with_recording(demo_config(tmp_path), start="delayed", start_delay_s=30.0)
    engine = AcquisitionEngine()
    states: list[AcquisitionState] = []
    timer = threading.Timer(0.3, engine.stop)
    timer.start()

    with pytest.raises(AcquisitionCancelled, match="nothing was recorded"):
        engine.run(
            config, SimulatedRig(config, realtime=True), duration_s=5.0, on_state=states.append
        )

    timer.join()
    assert AcquisitionState.RECORDING not in states
    assert engine.state is AcquisitionState.READY
    assert not list(tmp_path.glob("*.photometry-spool"))
    assert not list(tmp_path.glob("*.nwb"))


def test_delayed_and_ttl_starts_reach_the_source_and_are_recorded(tmp_path: Path) -> None:
    config = _with_recording(
        demo_config(tmp_path), start="ttl", start_ttl_line=3, start_ttl_edge="falling"
    )
    seen = []

    class Source(SimulatedRig):
        def packets(self, duration_s, start=None):
            seen.append(start)
            return super().packets(duration_s, start)

    result = AcquisitionEngine().run(config, Source(config), duration_s=0.5)

    assert seen == [StartCondition(ttl_line=3, ttl_edge="falling")]
    with NWBHDF5IO(result.report.path, mode="r", load_namespaces=True) as io:
        events = io.read().events["system_events"].to_dataframe()
    detail = events[events["event"] == "recording_start_trigger"]["detail"].iloc[0]
    assert detail == "simulated a falling edge on TTL input 3"


def test_operator_marks_are_timed_through_the_latest_frame(tmp_path: Path) -> None:
    config = demo_config(tmp_path, fiber_count=1, width_px=32, height_px=24)
    engine = AcquisitionEngine()
    stamps = []
    shown = []

    def on_progress(progress) -> None:
        shown.extend(progress.event_marks)
        if progress.frame_count == 10:
            stamps.append(progress.sample.timestamp_s)
            engine.mark_event("Tone", "button")

    engine.mark_event("Early", "key")  # cleared: a new run starts with no pending marks
    result = engine.run(config, SimulatedRig(config), duration_s=1.0, on_progress=on_progress)

    with NWBHDF5IO(result.report.path, mode="r", load_namespaces=True) as io:
        marks = io.read().events["event_marks"].to_dataframe()
    assert marks["event_name"].tolist() == ["Tone"]
    assert marks["source"].tolist() == ["button"]
    # Simulated frames arrive at once, so the mark lands on the frame it followed.
    assert marks["timestamp"].iloc[0] == pytest.approx(stamps[0], abs=0.01)
    assert [mark.name for mark in shown] == ["Tone"]
    with pytest.raises(ValueError, match="key or a button"):
        engine.mark_event("Tone", "ttl")


def test_sequential_excitation_records_only_lit_windows(tmp_path: Path) -> None:
    config = _with_recording(
        demo_config(tmp_path, fiber_count=1, width_px=32, height_px=24),
        excitation="sequential",
        excitation_on_s=1.0,
        excitation_off_s=1.0,
    )
    seen = []

    class Source(SimulatedRig):
        def packets(self, duration_s, start=None, cycle=None):
            seen.append(cycle)
            return super().packets(duration_s, start, cycle)

    result = AcquisitionEngine().run(config, Source(config), duration_s=4.0)

    assert seen == [ExcitationCycle(1.0, 1.0)]
    assert result.report.frame_count == 60
    assert result.diagnostics.dropped_frames == 0
    with NWBHDF5IO(result.report.path, mode="r", load_namespaces=True) as io:
        nwbfile = io.read()
        times = nwbfile.events["camera_frames"].to_dataframe()["timestamp"]
        events = nwbfile.events["system_events"].to_dataframe()
    assert not ((times >= 1.0) & (times < 2.0)).any()
    assert ((times >= 2.0) & (times < 3.0)).sum() == 30
    pauses = events[events["event"] == "excitation_paused"]["timestamp"].tolist()
    assert pauses == pytest.approx([1.0, 3.0])
