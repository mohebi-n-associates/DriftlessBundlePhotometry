import json
from pathlib import Path

import pytest

from driftless_photometry.config import SessionConfig, TraceDisplayConfig, Wavelength, demo_config
from driftless_photometry.settings import (
    SETTINGS_FORMAT,
    default_settings_path,
    export_settings,
    import_settings,
    load_default_settings,
    save_default_settings,
    settings_root,
)


def test_complete_json_settings_round_trip(tmp_path: Path) -> None:
    original = demo_config(tmp_path / "recordings", fiber_count=2, raw_capture=True)
    payload = original.model_dump()
    payload["rois"][1]["enabled"] = False
    payload.update(
        recording_duration_s=42.5,
        session_description="Dopamine cohort A",
        experimenter="A. Researcher",
        lab="Neural Dynamics",
        institution="Example University",
        display=TraceDisplayConfig(
            horizon_s=600.0,
            visible_wavelengths=(Wavelength.CONTROL_405, Wavelength.GREEN_470),
            mode="dff",
            dff_baseline_s=12.0,
        ),
    )
    original = SessionConfig.model_validate(payload)

    written = export_settings(tmp_path / "cohort-a", original)
    restored = import_settings(written)

    assert written.name == "cohort-a.json"
    assert restored == original
    document = json.loads(written.read_text(encoding="utf-8"))
    assert document["format"] == SETTINGS_FORMAT
    assert document["settings"]["rois"][1]["animal_id"] == "animal-02"
    assert document["settings"]["rois"][1]["enabled"] is False
    assert document["settings"]["camera"]["raw_capture"] is True
    assert document["settings"]["display"]["mode"] == "dff"


def test_settings_import_rejects_unknown_format(tmp_path: Path) -> None:
    path = tmp_path / "wrong.json"
    path.write_text('{"format": "some_other_app", "settings": {}}', encoding="utf-8")

    with pytest.raises(ValueError, match="not a Driftless Bundle Photometry settings file"):
        import_settings(path)


def test_default_settings_live_in_operator_settings_root(tmp_path: Path) -> None:
    config = demo_config(tmp_path / "recordings", fiber_count=1)

    written = save_default_settings(config)

    assert settings_root() == tmp_path / "operator-settings"
    assert written == default_settings_path()
    assert written.name == "default_settings.json"
    assert load_default_settings() == config
