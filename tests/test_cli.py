import json
from pathlib import Path

import pytest
from pynwb import NWBHDF5IO

from driftless_photometry.cli import LEGACY_RWD_SPOOL_NOTICE, build_parser, main
from driftless_photometry.settings import configuration_from_nwb


def test_cli_reports_short_name_and_version(capsys) -> None:
    with pytest.raises(SystemExit, match="0"):
        build_parser().parse_args(["--version"])

    assert capsys.readouterr().out == "DBF 0.3.11\n"


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


def test_cli_reports_and_refuses_legacy_rwd_tcp_spools(tmp_path: Path, capsys) -> None:
    legacy = tmp_path / "rwd-20260901T120000-000000Z.rwd-spool"
    legacy.mkdir()

    assert main(["--inspect-spools", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out) == [
        {"path": str(legacy), "unsupported": LEGACY_RWD_SPOOL_NOTICE}
    ]

    with pytest.raises(SystemExit):
        main(["--recover-spool", str(legacy)])
    assert "recover them with DBF 0.2.1" in capsys.readouterr().err
    assert legacy.is_dir()


@pytest.mark.parametrize(
    "removed_flag",
    ["--rwd-settings", "--rwd-wire-capture", "--rwd-replay", "--inspect-rwd-capture"],
)
def test_cli_no_longer_offers_rwd_tcp_modes(removed_flag: str, tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([removed_flag, str(tmp_path / "unused")])
