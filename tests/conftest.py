import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def isolate_operator_settings(monkeypatch, tmp_path) -> None:
    """Never let a test read or write the operator's real Documents settings."""

    monkeypatch.setenv("DBF_SETTINGS_DIR", str(tmp_path / "operator-settings"))
