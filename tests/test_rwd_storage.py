import json
import threading
from contextlib import suppress
from pathlib import Path

import numpy as np
import pytest
from pynwb import NWBHDF5IO, validate

from driftless_photometry.config import (
    RWDChannelMapping,
    RWDPreambleMode,
    RWDSourceConfig,
    SessionConfig,
    demo_config,
)
from driftless_photometry.domain import InvalidTimeInterval, SystemEvent
from driftless_photometry.provenance import RuntimeProvenance
from driftless_photometry.rwd import (
    RWDClockDiscontinuity,
    RWDEventRecord,
    RWDFluorescenceRecord,
    RWDFluorescenceSample,
    RWDReceivedRecord,
    RWDSessionData,
    RWDStreamMetadata,
    RWDWavelength,
    normalize_rwd_session,
)
from driftless_photometry.settings import (
    NWB_SETTINGS_SCRATCH_NAME,
    configuration_from_nwb,
)
from driftless_photometry.storage import (
    RWDSessionSpool,
    RWDSpoolBackpressureError,
    RWDSpoolError,
    discover_rwd_session_spools,
    inspect_rwd_session_spool,
    load_rwd_session_spool,
    recover_rwd_session_spool,
    write_rwd_session_nwbs,
)
from driftless_photometry.storage.rwd_nwb import RWD_STREAM_METADATA_SCRATCH_NAME


def _rwd_config(tmp_path: Path, *, fiber_count: int = 2) -> SessionConfig:
    config = demo_config(tmp_path, fiber_count=fiber_count, width_px=32, height_px=24)
    source = RWDSourceConfig(
        host="127.0.0.1",
        port=8080,
        preamble_mode=RWDPreambleMode.NONE,
        timestamp_scale_s=0.001,
        value_scale=0.001,
        enabled_wavelengths_nm=(410, 470, 560),
        channel_mappings=tuple(
            RWDChannelMapping(
                device_channel=index,
                fiber_id=roi.fiber_id,
                label=roi.label,
            )
            for index, roi in enumerate(config.enabled_rois)
        ),
        expected_machine_name="RWD1",
    )
    return config.model_copy(update={"session_id": "rwd-golden", "source": source})


def _fluorescence(channel: int, tick: int, offset: int = 0) -> RWDFluorescenceRecord:
    return RWDFluorescenceRecord(
        machine_name=b"RWD1",
        device_channel=channel,
        samples=tuple(
            RWDFluorescenceSample(
                wavelength_nm=wavelength,
                timestamp_tick=tick,
                raw_value=base + offset,
                scaled_value=(base + offset) * 0.001,
            )
            for wavelength, base in (
                (RWDWavelength.LED_410, 410_000),
                (RWDWavelength.LED_470, 470_000),
                (RWDWavelength.LED_560, 560_000),
            )
        ),
    )


def _event(tick: int, status: int = 0) -> RWDEventRecord:
    return RWDEventRecord(
        machine_name=b"RWD1",
        timestamp_tick=tick,
        event_name_raw=b"lever press".ljust(20, b"\x00"),
        status=status,
    )


def _received(records: list[RWDFluorescenceRecord | RWDEventRecord]) -> list[RWDReceivedRecord]:
    return [
        RWDReceivedRecord(
            wire_sequence=index,
            host_received_s=10.0 + index * 0.01,
            record=record,
        )
        for index, record in enumerate(records)
    ]


def _golden_data() -> RWDSessionData:
    return RWDSessionData(
        received_records=_received(
            [
                _fluorescence(0, 100),
                _fluorescence(1, 100, 1000),
                _event(150, 0),
                _fluorescence(0, 200, 10),
                _fluorescence(1, 200, 1010),
                _event(250, 1),
            ]
        ),
        system_events=[SystemEvent(0.075, "rwd_test_marker", "synthetic")],
        invalid_times=[InvalidTimeInterval(0.08, 0.09, "synthetic gap", "test_gap")],
        runtime_provenance=RuntimeProvenance(
            application_version="test-app",
            python_version="test-python",
            operating_system="test-os",
            adapter_name="rwd-golden-adapter",
            adapter_version="2",
            protocol_version="rwd-golden-protocol",
            dependencies=(("pynwb", "test-version"),),
        ),
        stream_metadata=RWDStreamMetadata(
            resolved_preamble_mode=RWDPreambleMode.NONE,
            preamble=None,
            machine_name=b"RWD1",
        ),
    )


def _submit_data(spool: RWDSessionSpool, data: RWDSessionData) -> None:
    assert data.stream_metadata is not None
    spool.set_stream_metadata(data.stream_metadata)
    for received in data.received_records:
        spool.submit_record(received)
    for event in data.system_events:
        spool.submit_system_event(event)
    for interval in data.invalid_times:
        spool.submit_invalid_time(interval)


def test_rwd_normalization_maps_channels_scales_and_shared_ticks(tmp_path: Path) -> None:
    normalized = normalize_rwd_session(_rwd_config(tmp_path), _golden_data())

    assert normalized.origin_unwrapped_tick == 100
    assert len(normalized.records) == 6
    assert len(normalized.samples) == 12
    assert len(normalized.events) == 2
    assert {sample.fiber_id for sample in normalized.samples} == {"fiber_01", "fiber_02"}
    assert normalized.samples[0].timestamp_s == 0.0
    assert normalized.samples[-1].timestamp_s == pytest.approx(0.1)
    assert normalized.events[0].timestamp_s == pytest.approx(0.05)
    assert normalized.events[0].active is True
    assert normalized.events[1].active is False
    assert normalized.samples[0].raw_value == 410_000
    assert normalized.samples[0].scaled_value == pytest.approx(410.0)


def test_rwd_normalization_unwraps_uint32_rollover(tmp_path: Path) -> None:
    config = _rwd_config(tmp_path, fiber_count=1)
    data = RWDSessionData(
        received_records=_received(
            [
                _fluorescence(0, 2**32 - 2),
                _event(1),
                _fluorescence(0, 3, 1),
            ]
        ),
        stream_metadata=RWDStreamMetadata(RWDPreambleMode.NONE, None, b"RWD1"),
    )

    normalized = normalize_rwd_session(config, data)

    assert normalized.events[0].unwrapped_timestamp_tick == 2**32 + 1
    assert normalized.samples[-1].unwrapped_timestamp_tick == 2**32 + 3
    assert normalized.samples[-1].timestamp_s == pytest.approx(0.005)


def test_rwd_normalization_does_not_assume_slot_timestamp_order(tmp_path: Path) -> None:
    config = _rwd_config(tmp_path, fiber_count=1)
    record = RWDFluorescenceRecord(
        machine_name=b"RWD1",
        device_channel=0,
        samples=(
            RWDFluorescenceSample(RWDWavelength.LED_410, 0x01020304, 1, 0.001),
            RWDFluorescenceSample(RWDWavelength.LED_470, 0, 2, 0.002),
            RWDFluorescenceSample(RWDWavelength.LED_560, 2**32 - 1, 3, 0.003),
        ),
    )
    data = RWDSessionData(
        received_records=_received([record, _event(0), _event(0, status=1)]),
        stream_metadata=RWDStreamMetadata(RWDPreambleMode.NONE, None, b"RWD1"),
    )

    normalized = normalize_rwd_session(config, data)

    assert normalized.origin_unwrapped_tick == -1
    assert [sample.unwrapped_timestamp_tick for sample in normalized.samples] == [
        0x01020304,
        0,
        -1,
    ]
    assert [event.timestamp_s for event in normalized.events] == [0.001, 0.001]


def test_rwd_normalization_rejects_clock_reset_and_wrong_scale(tmp_path: Path) -> None:
    config = _rwd_config(tmp_path, fiber_count=1)
    reset = RWDSessionData(
        received_records=_received([_fluorescence(0, 100), _fluorescence(0, 99, 1)]),
        stream_metadata=RWDStreamMetadata(RWDPreambleMode.NONE, None, b"RWD1"),
    )
    with pytest.raises(RWDClockDiscontinuity, match="moved backward"):
        normalize_rwd_session(config, reset)

    record = _fluorescence(0, 100)
    wrong = RWDFluorescenceRecord(
        machine_name=record.machine_name,
        device_channel=record.device_channel,
        samples=(RWDFluorescenceSample(RWDWavelength.LED_410, 100, 1000, 2.0),),
    )
    data = RWDSessionData(
        received_records=_received([wrong]),
        stream_metadata=RWDStreamMetadata(RWDPreambleMode.NONE, None, b"RWD1"),
    )
    with pytest.raises(ValueError, match="value_scale"):
        normalize_rwd_session(config, data)


def test_rwd_spool_round_trip_checksums_counts_and_provenance(tmp_path: Path) -> None:
    config = _rwd_config(tmp_path)
    data = _golden_data()
    spool = RWDSessionSpool(
        config,
        chunk_size=2,
        runtime_provenance=data.runtime_provenance,
    )
    _submit_data(spool, data)
    spool.close()

    loaded = load_rwd_session_spool(spool.path)

    assert loaded.complete is True
    assert loaded.config == config
    assert loaded.data == data
    assert loaded.schema_version == 1
    assert spool.diagnostics.committed_frames == 6
    assert spool.diagnostics.committed_chunks == 3
    manifest = json.loads((spool.path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["record_count"] == 6
    assert manifest["trace_sample_count"] == 12
    assert manifest["event_count"] == 2
    assert manifest["complete"] is True


def test_rwd_spool_committed_chunks_are_recoverable_before_graceful_close(
    tmp_path: Path,
) -> None:
    config = _rwd_config(tmp_path, fiber_count=1)
    spool = RWDSessionSpool(config, chunk_size=1)
    spool.set_stream_metadata(RWDStreamMetadata(RWDPreambleMode.NONE, None, b"RWD1"))
    spool.submit_record(RWDReceivedRecord(0, 1.0, _fluorescence(0, 100)))
    spool._queue.join()

    crash_snapshot = load_rwd_session_spool(spool.path)

    assert crash_snapshot.complete is False
    assert len(crash_snapshot.data.received_records) == 1
    assert len(crash_snapshot.data.received_records[0].record.samples) == 3
    spool.abort()


def test_rwd_spool_detects_chunk_and_event_log_corruption(tmp_path: Path) -> None:
    config = _rwd_config(tmp_path)
    spool = RWDSessionSpool(config, chunk_size=2)
    _submit_data(spool, _golden_data())
    spool.close()
    chunk = next((spool.path / "chunks").glob("*.jsonl"))
    chunk.write_bytes(chunk.read_bytes() + b"corrupt")
    with pytest.raises(RWDSpoolError, match="checksum"):
        load_rwd_session_spool(spool.path)

    second_config = config.model_copy(update={"session_id": "rwd-log-corrupt"})
    second = RWDSessionSpool(second_config)
    _submit_data(second, _golden_data())
    second.close()
    with (second.path / "system_events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write("{}\n")
    with pytest.raises(RWDSpoolError, match=r"system_events\.jsonl"):
        load_rwd_session_spool(second.path)


def test_rwd_spool_requires_contiguous_wire_order_and_metadata(tmp_path: Path) -> None:
    config = _rwd_config(tmp_path, fiber_count=1)
    spool = RWDSessionSpool(config)
    with pytest.raises(ValueError, match="expected 0"):
        spool.submit_record(RWDReceivedRecord(1, 1.0, _fluorescence(0, 100)))
    spool.submit_record(RWDReceivedRecord(0, 1.0, _fluorescence(0, 100)))
    with pytest.raises(RWDSpoolError, match="background") as caught:
        spool.close()
    assert "metadata" in str(caught.value.__cause__)


def test_rwd_spool_surfaces_background_failure_and_closes(tmp_path: Path) -> None:
    spool = RWDSessionSpool(_rwd_config(tmp_path, fiber_count=1), chunk_size=1)
    failed = threading.Event()

    def fail_commit(_records) -> None:
        failed.set()
        raise OSError("synthetic RWD disk failure")

    spool._commit_chunk = fail_commit  # type: ignore[method-assign]
    spool.set_stream_metadata(RWDStreamMetadata(RWDPreambleMode.NONE, None, b"RWD1"))
    with suppress(RWDSpoolError):
        spool.submit_record(RWDReceivedRecord(0, 1.0, _fluorescence(0, 100)))
    assert failed.wait(timeout=1)

    with pytest.raises(RWDSpoolError, match="background") as caught:
        spool.close()

    assert isinstance(caught.value.__cause__, OSError)
    assert "disk failure" in str(caught.value.__cause__)
    spool.close()


def test_rwd_spool_reports_bounded_queue_pressure(tmp_path: Path) -> None:
    spool = RWDSessionSpool(
        _rwd_config(tmp_path, fiber_count=1),
        chunk_size=1,
        queue_size=1,
        submit_timeout_s=0.02,
    )
    writer_entered = threading.Event()
    release_writer = threading.Event()
    original_commit = spool._commit_chunk

    def blocked_commit(records) -> None:
        writer_entered.set()
        assert release_writer.wait(timeout=2)
        original_commit(records)

    spool._commit_chunk = blocked_commit  # type: ignore[method-assign]
    spool.set_stream_metadata(RWDStreamMetadata(RWDPreambleMode.NONE, None, b"RWD1"))
    spool.submit_record(RWDReceivedRecord(0, 1.0, _fluorescence(0, 100)))
    assert writer_entered.wait(timeout=1)
    spool.submit_record(RWDReceivedRecord(1, 1.1, _fluorescence(0, 200, 1)))

    with pytest.raises(RWDSpoolBackpressureError, match="queue is full"):
        spool.submit_record(RWDReceivedRecord(2, 1.2, _fluorescence(0, 300, 2)))

    assert spool.diagnostics.queue_capacity == 1
    assert spool.diagnostics.peak_queue_depth == 1
    release_writer.set()
    spool.close()


def test_golden_rwd_nwb_is_trace_only_exact_and_independently_restorable(
    tmp_path: Path,
) -> None:
    config = _rwd_config(tmp_path)
    data = _golden_data()

    reports = write_rwd_session_nwbs(config, data)

    assert len(reports) == 2
    for roi_index, report in enumerate(reports):
        assert validate(path=report.path) == []
        assert report.stream_record_count == 6
        assert report.trace_sample_count == 6
        assert report.named_event_count == 2
        with NWBHDF5IO(report.path, mode="r", load_namespaces=True) as io:
            nwbfile = io.read()
            assert nwbfile.subject.subject_id == config.enabled_rois[roi_index].animal_id
            assert NWB_SETTINGS_SCRATCH_NAME in nwbfile.scratch
            stream_metadata = json.loads(str(nwbfile.get_scratch(RWD_STREAM_METADATA_SCRATCH_NAME)))
            assert stream_metadata["timestamp_scale_s"] == 0.001
            assert stream_metadata["value_scale"] == 0.001
            assert stream_metadata["stream_metadata"]["machine_name"] == b"RWD1".hex()
            assert "camera_frames" not in nwbfile.acquisition
            assert not nwbfile.stimulus
            assert set(nwbfile.events) == {
                "rwd_stream_records",
                "rwd_fluorescence_samples",
                "rwd_named_events",
                "system_events",
            }
            table = nwbfile.lab_meta_data["FiberPhotometry"].fiber_photometry_table
            assert table.to_dataframe()["excitation_wavelength_in_nm"].tolist() == [
                410.0,
                470.0,
                560.0,
            ]
            sample_table = nwbfile.events["rwd_fluorescence_samples"].to_dataframe()
            assert set(sample_table["fiber_id"]) == {config.enabled_rois[roi_index].fiber_id}
            assert set(sample_table["wavelength_nm"]) == {410, 470, 560}
            assert sample_table["raw_timestamp_tick"].tolist() == [100, 100, 100, 200, 200, 200]
            named = nwbfile.events["rwd_named_events"].to_dataframe()
            assert named["status"].tolist() == [0, 1]
            assert named["active"].tolist() == [True, False]
            assert named["event_name"].tolist() == ["lever press", "lever press"]
            for wavelength_nm, base in ((410, 410_000), (470, 470_000), (560, 560_000)):
                raw = nwbfile.acquisition[f"rwd_raw_value_{wavelength_nm}"]
                scaled = nwbfile.acquisition[f"rwd_fluorescence_{wavelength_nm}"]
                assert raw.data.dtype == np.dtype("uint32")
                np.testing.assert_array_equal(
                    raw.data[:],
                    [base + roi_index * 1000, base + 10 + roi_index * 1000],
                )
                np.testing.assert_allclose(
                    scaled.data[:, 0],
                    np.asarray(raw.data[:], dtype=np.float64) * 0.001,
                )

        restored, warnings = configuration_from_nwb(report.path)
        assert restored == config
        assert warnings == []


def test_incomplete_rwd_spool_recovery_is_marked_and_idempotent(tmp_path: Path) -> None:
    config = _rwd_config(tmp_path)
    data = _golden_data()
    spool = RWDSessionSpool(config, chunk_size=2, runtime_provenance=data.runtime_provenance)
    _submit_data(spool, data)
    spool.abort()

    inspection = inspect_rwd_session_spool(spool.path)
    assert inspection.acquisition_complete is False
    assert inspection.stream_record_count == 6
    assert inspection.trace_sample_count == 12
    assert inspection.named_event_count == 2
    assert inspection.fiber_file_count == 2
    assert discover_rwd_session_spools(tmp_path, incomplete_only=True) == (inspection,)

    first = recover_rwd_session_spool(spool.path, keep_spool=True)
    second = recover_rwd_session_spool(spool.path, keep_spool=True)

    assert first.acquisition_was_complete is False
    assert second.reused_existing_outputs is True
    assert spool.path.exists()
    for report in second.nwbs:
        with NWBHDF5IO(report.path, mode="r", load_namespaces=True) as io:
            events = io.read().events["system_events"].to_dataframe()["event"].tolist()
            assert events[-1] == "rwd_spool_recovered_incomplete_acquisition"


def test_successful_rwd_recovery_removes_spool_only_after_nwb_validation(
    tmp_path: Path,
) -> None:
    config = _rwd_config(tmp_path)
    data = _golden_data()
    spool = RWDSessionSpool(config, runtime_provenance=data.runtime_provenance)
    _submit_data(spool, data)
    spool.close()

    result = recover_rwd_session_spool(spool.path)

    assert result.acquisition_was_complete is True
    assert result.spool_removed is True
    assert not spool.path.exists()
    assert all(report.path.exists() for report in result.nwbs)
