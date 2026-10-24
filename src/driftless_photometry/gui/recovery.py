"""Background inspection and explicit recovery of interrupted recordings."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from driftless_photometry.storage import inspect_session_spool, recover_session_spool
from driftless_photometry.storage.recovery import (
    RecoveryReport,
    SpoolInspection,
    session_spool_candidates,
)

from .worker import BoardTaskWorker


def inspect_recovery_folder(root: Path) -> list[tuple[Path, SpoolInspection | None, str]]:
    """A corrupt spool must not hide the other recoverable sessions in a folder."""
    results = []
    for path in session_spool_candidates(root):
        if not path.is_dir():
            continue
        try:
            inspection = inspect_session_spool(path)
        except Exception as error:
            results.append((path, None, str(error)))
        else:
            results.append((path, inspection, ""))
    return results


class RecoveryDialog(QDialog):
    def __init__(self, root: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Recover a recording")
        self.resize(760, 420)
        self._root = root
        self._thread = None
        self._worker = None
        self._close_pending = False
        self._retired = []
        layout = QVBoxLayout(self)
        self.location = QLabel(str(root))
        self.location.setWordWrap(True)
        layout.addWidget(self.location)
        self.sessions = QListWidget()
        self.sessions.currentItemChanged.connect(self._selection_changed)
        layout.addWidget(self.sessions)
        self.status = QLabel("Checking committed chunks and checksums…")
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status)
        self.keep = QCheckBox("Keep recovery spool after validation")
        self.keep.setChecked(True)
        layout.addWidget(self.keep)
        actions = QHBoxLayout()
        self.browse = QPushButton("Choose output folder…")
        self.browse.clicked.connect(self._browse)
        self.recover = QPushButton("Recover selected session")
        self.recover.setEnabled(False)
        self.recover.clicked.connect(self._recover)
        actions.addWidget(self.browse)
        actions.addWidget(self.recover)
        layout.addLayout(actions)
        QTimer.singleShot(0, self._scan)

    def _browse(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Output folder", str(self._root))
        if path:
            self._root = Path(path)
            self.location.setText(path)
            self._scan()

    def _scan(self) -> None:
        self.sessions.clear()
        self.status.setText("Checking committed chunks and checksums…")
        self._run(lambda: inspect_recovery_folder(self._root), self._scanned)

    def _run(self, task: Callable[[], object], completed: Callable[..., None]) -> None:
        self.browse.setEnabled(False)
        self.recover.setEnabled(False)
        self.keep.setEnabled(False)
        thread = QThread(self)
        worker = BoardTaskWorker(task)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(completed)
        worker.failed.connect(self._failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(self._finished)
        thread.finished.connect(thread.deleteLater)
        self._thread, self._worker = thread, worker
        thread.start()

    def _scanned(self, results: list[tuple[Path, SpoolInspection | None, str]]) -> None:
        for path, inspection, error in results:
            if inspection is None:
                text = f"{path.name} — cannot recover: {error}"
            else:
                state = "complete acquisition" if inspection.acquisition_complete else "interrupted"
                text = (
                    f"{inspection.session_id} — {state}; {inspection.frame_count:,} frames; "
                    f"{inspection.ttl_edge_count:,} TTL edges; "
                    f"{inspection.roi_file_count} ROI files"
                )
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, path if inspection is not None else None)
            self.sessions.addItem(item)
        self.status.setText(
            "Select a session to validate and finalize its ROI files."
            if results
            else "No recovery spools found in this output folder."
        )

    def _selection_changed(self, *_args: object) -> None:
        item = self.sessions.currentItem()
        self.recover.setEnabled(
            self._thread is None
            and item is not None
            and item.data(Qt.ItemDataRole.UserRole) is not None
        )

    def _recover(self) -> None:
        item = self.sessions.currentItem()
        if item is None:
            return
        path = item.data(Qt.ItemDataRole.UserRole)
        keep = self.keep.isChecked()
        self.status.setText("Writing and validating every ROI file; please wait…")
        self._run(lambda: recover_session_spool(path, keep_spool=keep), self._recovered)

    def _recovered(self, report: RecoveryReport) -> None:
        if report.spool_removed and self.sessions.currentItem() is not None:
            self.sessions.currentItem().setData(Qt.ItemDataRole.UserRole, None)
        self.status.setText(
            f"Validated {len(report.nwbs)} ROI files; "
            f"{report.inspection.frame_count:,} frames each.\n"
            + "\n".join(str(nwb.path) for nwb in report.nwbs)
            + (
                "\nRecovery spool retained."
                if not report.spool_removed
                else "\nRecovery spool removed."
            )
        )

    def _failed(self, detail: str) -> None:
        self.status.setText("Recovery failed; source spool retained.\n" + detail.splitlines()[-1])
        self.status.setToolTip(detail)

    def _finished(self) -> None:
        self._retired.append(self._worker)
        self._thread, self._worker = None, None
        self.browse.setEnabled(True)
        self.keep.setEnabled(True)
        self._selection_changed()
        if self._close_pending:
            self.close()

    def reject(self) -> None:
        if self._thread is not None:
            self._close_pending = True
            self.status.setText("Closing after recovery finishes validation…")
            return
        super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._thread is not None:
            self._close_pending = True
            event.ignore()
        else:
            event.accept()
