"""An editable dropdown of remembered values that the operator can extend and prune."""

from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtCore import (
    QEvent,
    QObject,
    QPoint,
    QRegularExpression,
    QSignalBlocker,
    Qt,
    Signal,
    SignalInstance,
)
from PySide6.QtGui import QKeyEvent, QRegularExpressionValidator
from PySide6.QtWidgets import QComboBox

_EXACT = Qt.MatchFlag.MatchFixedString | Qt.MatchFlag.MatchCaseSensitive


class ChoiceCombo(QComboBox):
    """Pick a remembered value, or type a new one and press Enter to add it.

    Reads and writes like a line edit (``text``/``setText``/``textChanged``), so the
    value need not be in the list. ``added`` and ``removed`` report list changes the
    operator made: right-click to remove the shown value, or press Delete on a
    highlighted entry in the open list. Removing an entry never changes the value.
    """

    added = Signal(str)
    removed = Signal(str)

    def __init__(self, text: str, *, pattern: str | None = None, what: str) -> None:
        super().__init__()
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.setMaxVisibleItems(20)
        line = self.lineEdit()
        if pattern is not None:
            line.setValidator(QRegularExpressionValidator(QRegularExpression(pattern), self))
        line.setPlaceholderText(f"Type a new {what} or pick one")
        line.returnPressed.connect(self.commit)
        line.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        line.customContextMenuRequested.connect(self._show_menu)
        self.view().installEventFilter(self)
        self.setToolTip(
            f"Pick a saved {what}, or type a new one and press Enter to keep it.\n"
            "Right-click, or press Delete on an entry in the open list, to remove it."
        )
        self.setCurrentText(text)

    @property
    def textChanged(self) -> SignalInstance:
        return self.currentTextChanged

    def text(self) -> str:
        return self.currentText()

    def setText(self, text: str) -> None:
        self.setCurrentText(text)

    def choices(self) -> tuple[str, ...]:
        return tuple(self.itemText(index) for index in range(self.count()))

    def set_choices(self, choices: Iterable[str]) -> None:
        """Replace the list without changing or signalling the current value."""

        current = self.currentText()
        blocker = QSignalBlocker(self)
        self.clear()
        self.addItems(list(choices))
        self.setCurrentText(current)
        del blocker

    def commit(self) -> None:
        """Add the typed value to the list, once, and report it."""

        value = self.currentText().strip()
        if not value or not self.lineEdit().hasAcceptableInput():
            return
        if self.findText(value, _EXACT) < 0:
            self.set_choices((*self.choices(), value))
            self.added.emit(value)

    def remove_choice(self, value: str) -> None:
        """Drop one value from the list and report it; the current value stays."""

        if self.findText(value, _EXACT) >= 0:
            self.set_choices(choice for choice in self.choices() if choice != value)
            self.removed.emit(value)

    def _show_menu(self, position: QPoint) -> None:
        line = self.lineEdit()
        menu = line.createStandardContextMenu()
        value = self.currentText().strip()
        if value and self.findText(value, _EXACT) >= 0:
            menu.addSeparator()
            action = menu.addAction(f"Remove “{value}” from the list")
            action.setData(value)
        chosen = menu.exec(line.mapToGlobal(position))
        if chosen is not None and chosen.data():
            self.remove_choice(str(chosen.data()))
        menu.deleteLater()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            watched is self.view()
            and event.type() == QEvent.Type.KeyPress
            and isinstance(event, QKeyEvent)
            and event.key() == Qt.Key.Key_Delete
        ):
            index = self.view().currentIndex()
            if index.isValid():
                self.remove_choice(str(index.data()))
                return True
        return super().eventFilter(watched, event)
