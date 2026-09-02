import json
from pathlib import Path

import pytest
from pynwb import NWBHDF5IO

from driftless_photometry.cli import build_parser, main
from driftless_photometry.config import (
    RWDChannelMapping,
    RWDPreambleMode,
    RWDSourceConfig,
    demo_config,
)
from driftless_photometry.rwd import (
    RWDFluorescenceRecord,
    RWDFluorescenceSample,
    RWDReceivedRecord,
    RWDStreamMetadata,
    RWDWavelength,
)
from driftless_photometry.settings import configuration_from_nwb
from driftless_photometry.storage import RWDSessionSpool


def test_cli_reports_short_name_and_version(capsys) -> None:
    with pytest.raises(SystemExit, match="0"):
        build_parser().parse_args(["--version"])

    assert capsys.readouterr().out == "DBF 0.1.6\n"


def test_headless_cli_writes_machine_readable_result(tmp_path: Path, capsys) -> None:
    exit_code = main(
        [
            "--headless",
            "--duration",
            "0.04",
            "--fibers",
            "2",
            "--output",
            str(tmp_path),
        ]
    )

    result = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert result["frames"] == 1
    assert result["trace_samples"] == 1
    assert result["stopped_by_request"] is False
    assert result["roi_files"] == 2
    assert len(result["nwb_paths"]) == 2
    for nwb_path_text in result["nwb_paths"]:
        nwb_path = Path(nwb_path_text)
        assert nwb_path.is_file()
        with NWBHDF5IO(nwb_path, mode="r", load_namespaces=True) as io:
            nwbfile = io.read()
            assert len(nwbfile.events["camera_frames"]) == 1
            assert len(nwbfile.processing["photometry"]["camera_rois"]) == 1
            assert "camera_frames" not in nwbfile.acquisition
        restored, warnings = configuration_from_nwb(nwb_path)
        assert restored.recording_duration_s == 0.04
        assert warnings == []


def test_cli_inspects_only_recovery_spools_without_mutating_directory(
    tmp_path: Path,
    capsys,
) -> None:
    ordinary = tmp_path / "ordinary-directory"
    ordinary.mkdir()

    exit_code = main(["--inspect-spools", str(tmp_path)])

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == []
    assert ordinary.is_dir()


def test_cli_inspects_and_recovers_rwd_spool_by_suffix(tmp_path: Path, capsys) -> None:
    base = demo_config(tmp_path, fiber_count=1, width_px=32, height_px=24)
    source = RWDSourceConfig(
        preamble_mode=RWDPreambleMode.NONE,
        channel_mappings=(
            RWDChannelMapping(device_channel=0, fiber_id="fiber_01", label="Fiber 1"),
        ),
        expected_machine_name="RWD1",
    )
    config = base.model_copy(update={"session_id": "rwd-cli-recovery", "source": source})
    spool = RWDSessionSpool(config, chunk_size=1)
    spool.set_stream_metadata(RWDStreamMetadata(RWDPreambleMode.NONE, None, b"RWD1"))
    spool.submit_record(
        RWDReceivedRecord(
            wire_sequence=0,
            host_received_s=0.01,
            record=RWDFluorescenceRecord(
                machine_name=b"RWD1",
                device_channel=0,
                samples=(
                    RWDFluorescenceSample(
                        wavelength_nm=RWDWavelength.LED_410,
                        timestamp_tick=100,
                        raw_value=1234,
                        scaled_value=1.234,
                    ),
                ),
            ),
        )
    )
    spool.close()

    assert main(["--inspect-spools", str(tmp_path)]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert len(inspected) == 1
    assert inspected[0]["source_kind"] == "rwd"
    assert inspected[0]["stream_records"] == 1
    assert inspected[0]["fiber_files"] == 1

    assert main(["--recover-spool", str(spool.path)]) == 0
    recovered = json.loads(capsys.readouterr().out)
    assert recovered["source_kind"] == "rwd"
    assert recovered["stream_records"] == 1
    assert recovered["fiber_files"] == 1
    assert recovered["spool_removed"] is True
    assert Path(recovered["nwb_paths"][0]).is_file()
