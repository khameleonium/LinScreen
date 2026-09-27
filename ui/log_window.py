"""
Окно журнала работы внешних процессов.

Показывает команды, переданные FFmpeg, и его вывод. Предназначено для
самостоятельной диагностики: при сбое записи текст журнала объясняет причину
точнее, чем короткое уведомление.
"""

from __future__ import annotations

from core.i18n import tr

from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)


class LogWindow(QDialog):
    """Окно просмотра журнала с возможностью копирования."""

    def __init__(self, lines: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Журнал LinScreen"))
        self.resize(900, 520)

        self._view = QPlainTextEdit()
        self._view.setReadOnly(True)
        # Моноширинный шрифт сохраняет выравнивание вывода кодировщика.
        font = QFont("monospace")
        font.setStyleHint(QFont.StyleHint.TypeWriter)
        font.setPointSize(9)
        self._view.setFont(font)
        self._view.setPlainText("\n".join(lines))
        self._scroll_to_end()

        buttons = QDialogButtonBox()
        copy_button = buttons.addButton(tr("Копировать"), QDialogButtonBox.ButtonRole.ActionRole)
        close_button = buttons.addButton(tr("Закрыть"), QDialogButtonBox.ButtonRole.RejectRole)
        copy_button.clicked.connect(self._copy)
        close_button.clicked.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self._view)
        layout.addWidget(buttons)

    def append(self, line: str) -> None:
        """Добавление строки в конец журнала."""
        self._view.appendPlainText(line)
        self._scroll_to_end()

    def _scroll_to_end(self) -> None:
        """Перемотка к последней записи."""
        bar = self._view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _copy(self) -> None:
        """Копирование всего журнала в буфер обмена."""
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self._view.toPlainText())
