import pytest

import driftless_photometry.desktop as desktop


def test_desktop_opens_gui_when_no_arguments(monkeypatch) -> None:
    received: list[list[str]] = []
    monkeypatch.setattr(desktop, "cli_main", lambda arguments: received.append(arguments) or 0)

    assert desktop.main([]) == 0
    assert received == [["--demo"]]


def test_desktop_forwards_explicit_diagnostic_arguments(monkeypatch) -> None:
    received: list[list[str]] = []
    monkeypatch.setattr(desktop, "cli_main", lambda arguments: received.append(arguments) or 0)

    assert desktop.main(["--version"]) == 0
    assert received == [["--version"]]


def test_desktop_smoke_loads_packaged_gui_without_calling_cli(monkeypatch) -> None:
    monkeypatch.setattr(
        desktop,
        "cli_main",
        lambda arguments: pytest.fail(f"unexpected CLI call: {arguments}"),
    )

    assert desktop.main(["--desktop-smoke"]) == 0
