"""
Поле ввода сочетания клавиш.

Виджет перехватывает нажатия и записывает их в привычном текстовом виде
"Ctrl+Alt+R". Разбор такой записи выполняет core/hotkeys.py, поэтому формат
одинаков в настройках, в файле конфигурации и в перехватчике.
"""

from __future__ import annotations

from core.i18n import tr

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFocusEvent, QKeyEvent
from PySide6.QtWidgets import QLineEdit, QWidget

# Клавиши-модификаторы, самостоятельного значения не имеющие.
_MODIFIER_KEYS = {
    Qt.Key.Key_Control,
    Qt.Key.Key_Alt,
    Qt.Key.Key_Shift,
    Qt.Key.Key_Meta,
    Qt.Key.Key_AltGr,
}


class HotkeyEdit(QLineEdit):
    """Поле, заполняемое нажатием нужного сочетания клавиш."""

    # Начало и конец набора сочетания. Пока поле в фокусе, глобальный
    # перехват приостанавливается: иначе нажатие в поле выполнило бы
    # назначенное этому сочетанию действие.
    captureChanged = Signal(bool)

    def __init__(self, value: str = "", parent: QWidget | None = None) -> None:
        super().__init__(value, parent)
        self.setReadOnly(True)
        self.setPlaceholderText(tr("Нажмите сочетание клавиш"))
        self.setClearButtonEnabled(True)

    def focusInEvent(self, event: QFocusEvent) -> None:  # noqa: N802 - имя из Qt
        """Начало набора сочетания."""
        super().focusInEvent(event)
        self.captureChanged.emit(True)

    def focusOutEvent(self, event: QFocusEvent) -> None:  # noqa: N802 - имя из Qt
        """Завершение набора сочетания."""
        super().focusOutEvent(event)
        self.captureChanged.emit(False)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - имя из Qt
        """Запись нажатого сочетания в текстовое представление."""
        key = Qt.Key(event.key())
        if key in _MODIFIER_KEYS:
            # Одни модификаторы сочетанием не являются: ожидается основная клавиша.
            return
        if key in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete):
            # Очистка поля отключает соответствующее действие.
            self.clear()
            return

        parts: list[str] = []
        modifiers = event.modifiers()
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            parts.append("Ctrl")
        if modifiers & Qt.KeyboardModifier.AltModifier:
            parts.append("Alt")
        if modifiers & Qt.KeyboardModifier.ShiftModifier:
            parts.append("Shift")
        if modifiers & Qt.KeyboardModifier.MetaModifier:
            parts.append("Super")

        name = _key_name(key, event.text())
        if not name:
            return
        parts.append(name)
        self.setText("+".join(parts))


def _key_name(key: Qt.Key, text: str) -> str:
    """Название основной клавиши в принятом текстовом формате."""
    special = {
        Qt.Key.Key_Print: "Print",
        Qt.Key.Key_SysReq: "Print",
        Qt.Key.Key_Space: "Space",
        Qt.Key.Key_Tab: "Tab",
        Qt.Key.Key_Return: "Enter",
        Qt.Key.Key_Enter: "Enter",
        Qt.Key.Key_Insert: "Insert",
        Qt.Key.Key_Home: "Home",
        Qt.Key.Key_End: "End",
        Qt.Key.Key_PageUp: "PageUp",
        Qt.Key.Key_PageDown: "PageDown",
        Qt.Key.Key_Up: "Up",
        Qt.Key.Key_Down: "Down",
        Qt.Key.Key_Left: "Left",
        Qt.Key.Key_Right: "Right",
    }
    if key in special:
        return special[key]
    if Qt.Key.Key_F1 <= key <= Qt.Key.Key_F35:
        return f"F{key - Qt.Key.Key_F1 + 1}"
    if text and text.isprintable() and not text.isspace():
        return text.upper()
    return ""
