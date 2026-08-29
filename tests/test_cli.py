import json
from pathlib import Path

import pytest
from pynwb import NWBHDF5IO

from driftless_photometry.cli import build_parser, main


def test_cli_reports_short_name_and_version(capsys) -> None:
    with pytest.raises(SystemExit, match="0"):
        build_parser().parse_args(["--version"])

    assert capsys.readouterr().out == "DBF 0.1.4\n"


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
