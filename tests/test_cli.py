import json
from pathlib import Path

from pynwb import NWBHDF5IO

from driftless_photometry.cli import main


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
    nwb_path = Path(result["nwb_path"])
    assert nwb_path.is_file()
    with NWBHDF5IO(nwb_path, mode="r", load_namespaces=True) as io:
        nwbfile = io.read()
        assert len(nwbfile.events["camera_frames"]) == 1
        assert "camera_frames" not in nwbfile.acquisition
