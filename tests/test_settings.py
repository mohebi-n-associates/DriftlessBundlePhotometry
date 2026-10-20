import json
from pathlib import Path

import pytest

from driftless_photometry.config import (
    CustomSourceConfig,
    RWDSourceConfig,
    SessionConfig,
    TraceDisplayConfig,
    Wavelength,
    demo_config,
)
from driftless_photometry.settings import (
    SETTINGS_FORMAT,
    SETTINGS_FORMAT_V1,
    SETTINGS_FORMAT_V2,
    default_settings_path,
    export_settings,
    import_settings,
    import_settings_with_warnings,
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
    assert document["format"] == "driftless_bundle_photometry_settings_v3"
    assert document["settings"]["source"] == {"kind": "custom"}


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


def test_v1_settings_migrate_explicitly_to_custom_source(tmp_path: Path) -> None:
    original = demo_config(tmp_path / "recordings", fiber_count=1)
    legacy_settings = original.model_dump(mode="json")
    legacy_settings.pop("source")
    path = tmp_path / "legacy-v1.json"
    path.write_text(
        json.dumps({"format": SETTINGS_FORMAT_V1, "settings": legacy_settings}),
        encoding="utf-8",
    )

    restored, warnings = import_settings_with_warnings(path)

    assert restored == original
    assert restored.source == CustomSourceConfig()
    assert len(warnings) == 1
    assert "migrated explicitly" in warnings[0]
    with pytest.warns(UserWarning, match="migrated explicitly"):
        assert import_settings(path) == original


def _write_v2(path: Path, config: SessionConfig, source: dict[str, object]) -> Path:
    settings = config.model_dump(mode="json")
    settings["source"] = source
    path.write_text(
        json.dumps({"format": SETTINGS_FORMAT_V2, "settings": settings}), encoding="utf-8"
    )
    return path


def test_v2_native_settings_migrate_to_custom_source(tmp_path: Path) -> None:
    original = demo_config(tmp_path / "recordings", fiber_count=1)
    path = _write_v2(tmp_path / "native-v2.json", original, {"kind": "native"})

    restored, warnings = import_settings_with_warnings(path)

    assert restored == original
    assert len(warnings) == 1
    assert "native source is now the custom" in warnings[0]


def test_v2_rwd_tcp_settings_keep_session_and_name_dropped_fields(tmp_path: Path) -> None:
    original = demo_config(tmp_path / "recordings", fiber_count=2)
    tcp_source = {
        "kind": "rwd",
        "host": "192.168.65.9",
        "port": 8080,
        "channel_mappings": [{"device_channel": 4, "fiber_id": "fiber_01", "label": "A"}],
    }
    path = _write_v2(tmp_path / "rwd-v2.json", original, tcp_source)

    restored, warnings = import_settings_with_warnings(path)

    assert restored == original.model_copy(update={"source": RWDSourceConfig()})
    assert len(warnings) == 1
    assert "removed RWD read-only TCP bridge" in warnings[0]
    assert "dropped: channel_mappings, host, port" in warnings[0]


def test_v2_settings_require_a_known_source(tmp_path: Path) -> None:
    original = demo_config(tmp_path / "recordings", fiber_count=1)
    path = _write_v2(tmp_path / "unknown-v2.json", original, {"kind": "other"})

    with pytest.raises(ValueError, match="without a native or rwd source"):
        import_settings(path)


def test_v3_rwd_settings_round_trip_without_warnings(tmp_path: Path) -> None:
    base = demo_config(tmp_path / "recordings", fiber_count=1)
    original = base.model_copy(update={"source": RWDSourceConfig()})

    written = export_settings(tmp_path / "rwd.settings.json", original)
    restored, warnings = import_settings_with_warnings(written)

    assert restored == original
    assert warnings == []
