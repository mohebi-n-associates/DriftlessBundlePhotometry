# Release guide

GitHub Releases are the release authority. Publishing a release whose tag is exactly
`v<project-version>` starts `.github/workflows/release.yml`. The workflow builds from
that immutable tag and publishes the verified wheel and source distribution to
PyPI. It also creates a self-contained 64-bit Windows application, compiles and
smoke-tests a per-user installer, and attaches the installer, Python distributions,
and `SHA256SUMS.txt` to the matching GitHub release.

## One-time repository setup

An owner of the GitHub repository and the relevant package indexes must complete
these steps once. No PyPI API token is stored in GitHub.

1. In GitHub, create an environment named `pypi` and require a trusted reviewer.
2. On PyPI, add a pending Trusted Publisher for:
   - PyPI project: `driftless-bundle-photometry`
   - GitHub owner: `mohebi-n-associates`
   - repository: `DriftlessBundlePhotometry`
   - workflow: `release.yml`
   - environment: `pypi`

The first approved publish can create the project from the pending publisher. The
workflow requests short-lived OpenID Connect credentials and generates PyPI
provenance attestations; it does not use a password or repository secret.

## Prepare and publish a release

1. Choose the version and update `pyproject.toml`, the README version badge, and any
   exact-version regression assertions.
2. Move completed changelog entries from `Unreleased` into a dated version section,
   then leave a new empty `Unreleased` section.
3. Run the local gates:

   ```bash
   conda env update --file environment.yml --prune
   conda activate dbf
   python -m ruff check .
   python -m ruff format --check .
   python -m pytest
   python -m build
   python -m twine check --strict dist/*
   python tools/release.py check v0.2.1
   ```

4. Commit the release preparation, create and push the matching annotated tag, then
   create a GitHub release from that tag. Publishing the GitHub release starts the
   protected workflow.
5. Approve the `pypi` deployment when requested. Do not approve it unless the Python
   distribution checks and Windows install/uninstall smoke test passed.
6. Confirm that the GitHub release contains one `.whl`, one `.tar.gz`, the versioned
   Windows Setup `.exe`, and `SHA256SUMS.txt`, and that PyPI shows the same version.

The workflow fails before publication when the tag, package version, README, or
dated changelog section disagree. PyPI files are immutable; fix a failed or incorrect
release with a new version rather than trying to replace an uploaded file.

## Windows package details

PyInstaller builds an onedir application so Qt, HDF5, PyNWB, NDX schemas, and their
native libraries remain explicit. Inno Setup packages that directory into one
downloadable installer. CI then performs a silent per-user install, launches the
installed executable, and uninstalls it before allowing publication.

The current installer is unsigned. Code signing, clean-machine physical-adapter
installation, and hardware validation remain release-readiness work and must be
completed before making corresponding hardware claims.
