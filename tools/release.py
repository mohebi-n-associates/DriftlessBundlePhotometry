"""Release metadata checks and deterministic SHA-256 manifest generation."""

from __future__ import annotations

import argparse
import hashlib
import re
import tomllib
from pathlib import Path

PROJECT_NAME = "driftless-bundle-photometry"
_VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


def project_version(root: Path) -> str:
    with (root / "pyproject.toml").open("rb") as stream:
        document = tomllib.load(stream)
    version = document["project"]["version"]
    if not isinstance(version, str) or not _VERSION_PATTERN.fullmatch(version):
        raise ValueError(f"unsupported project version: {version!r}")
    return version


def check_release_metadata(root: Path, tag: str) -> str:
    version = project_version(root)
    expected_tag = f"v{version}"
    if tag != expected_tag:
        raise ValueError(
            f"release tag {tag!r} does not match pyproject version {version!r}; "
            f"expected {expected_tag!r}"
        )

    readme = (root / "readme.md").read_text(encoding="utf-8")
    if f"**Version {version}**" not in readme:
        raise ValueError(f"readme.md does not identify version {version}")

    changelog = (root / "WHATS_NEW.md").read_text(encoding="utf-8")
    unreleased = re.search(r"^## Unreleased\n(?P<body>.*?)(?=^## |\Z)", changelog, re.M | re.S)
    if unreleased is None or unreleased.group("body").strip() != "_No unreleased changes._":
        raise ValueError("WHATS_NEW.md Unreleased section must be empty for a release")
    if not re.search(rf"^## {re.escape(version)} - \d{{4}}-\d{{2}}-\d{{2}}$", changelog, re.M):
        raise ValueError(f"WHATS_NEW.md has no dated {version} release section")
    return version


def write_sha256_manifest(paths: list[Path], destination: Path) -> None:
    files = sorted(path.resolve(strict=True) for path in paths)
    if not files:
        raise ValueError("at least one release artifact is required")
    if any(not path.is_file() for path in files):
        raise ValueError("release artifacts must be files")
    lines = [f"{_sha256(path)}  {path.name}" for path in files]
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    version_parser = subparsers.add_parser("version", help="print the project version")
    version_parser.add_argument("--root", type=Path, default=Path.cwd())

    check_parser = subparsers.add_parser("check", help="validate release metadata and tag")
    check_parser.add_argument("tag")
    check_parser.add_argument("--root", type=Path, default=Path.cwd())

    checksum_parser = subparsers.add_parser("checksums", help="write a SHA-256 manifest")
    checksum_parser.add_argument("destination", type=Path)
    checksum_parser.add_argument("artifacts", type=Path, nargs="+")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    if args.command == "version":
        print(project_version(args.root))
    elif args.command == "check":
        print(check_release_metadata(args.root, args.tag))
    else:
        write_sha256_manifest(args.artifacts, args.destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
