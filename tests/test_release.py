import hashlib
from pathlib import Path

import pytest

from tools.release import check_release_metadata, project_version, write_sha256_manifest


def _write_release_metadata(root: Path, *, version: str = "1.2.3") -> None:
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "driftless-bundle-photometry"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    (root / "readme.md").write_text(f"**Version {version}**\n", encoding="utf-8")
    (root / "WHATS_NEW.md").write_text(
        f"## Unreleased\n\n_No unreleased changes._\n\n## {version} - 2026-09-02\n",
        encoding="utf-8",
    )


def test_release_metadata_requires_exact_tag_and_documented_version(tmp_path: Path) -> None:
    _write_release_metadata(tmp_path)

    assert project_version(tmp_path) == "1.2.3"
    assert check_release_metadata(tmp_path, "v1.2.3") == "1.2.3"

    with pytest.raises(ValueError, match="does not match"):
        check_release_metadata(tmp_path, "v1.2.4")


def test_release_metadata_requires_dated_changelog_entry(tmp_path: Path) -> None:
    _write_release_metadata(tmp_path)
    (tmp_path / "WHATS_NEW.md").write_text(
        "## Unreleased\n\n_No unreleased changes._\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"no dated 1\.2\.3 release section"):
        check_release_metadata(tmp_path, "v1.2.3")


def test_release_metadata_requires_empty_unreleased_section(tmp_path: Path) -> None:
    _write_release_metadata(tmp_path)
    (tmp_path / "WHATS_NEW.md").write_text(
        "## Unreleased\n\n- unfinished change\n\n## 1.2.3 - 2026-09-02\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Unreleased section must be empty"):
        check_release_metadata(tmp_path, "v1.2.3")


def test_sha256_manifest_is_sorted_and_uses_basename(tmp_path: Path) -> None:
    second = tmp_path / "b.whl"
    first = tmp_path / "a.tar.gz"
    second.write_bytes(b"second")
    first.write_bytes(b"first")
    destination = tmp_path / "SHA256SUMS.txt"

    write_sha256_manifest([second, first], destination)

    expected = [
        f"{hashlib.sha256(b'first').hexdigest()}  a.tar.gz",
        f"{hashlib.sha256(b'second').hexdigest()}  b.whl",
    ]
    assert destination.read_text(encoding="utf-8").splitlines() == expected
