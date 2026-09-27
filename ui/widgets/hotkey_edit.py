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
from PySide6.QtWidgets import QLineEdit, QToolTip, QWidget

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
        # Удерживаемые клавиши-модификаторы, отслеживаемые самим полем.
        self._held: set[Qt.Key] = set()
        self.setPlaceholderText(tr("Нажмите сочетание клавиш"))
        self.setClearButtonEnabled(True)

    def focusInEvent(self, event: QFocusEvent) -> None:  # noqa: N802 - имя из Qt
        """Начало набора сочетания."""
        super().focusInEvent(event)
        self.captureChanged.emit(True)

    def focusOutEvent(self, event: QFocusEvent) -> None:  # noqa: N802 - имя из Qt
        """Завершение набора сочетания."""
        super().focusOutEvent(event)
        self._held.clear()
        self.captureChanged.emit(False)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - имя из Qt
        """Учёт отпущенных модификаторов."""
        self._held.discard(Qt.Key(event.key()))
        if event.nativeScanCode() in _SHIFT_SCANCODES:
            self._held.discard(Qt.Key.Key_Shift)
        super().keyReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - имя из Qt
        """Запись нажатого сочетания в текстовое представление."""
        key = Qt.Key(event.key())
        if event.nativeScanCode() in _SHIFT_SCANCODES:
            # Shift узнаётся по физической клавише: при переключении раскладки
            # сочетанием Ctrl+Shift нажатие Shift приходит как смена раскладки,
            # а не как модификатор.
            self._held.add(Qt.Key.Key_Shift)
            return
        if key in _MODIFIER_KEYS:
            # Одни модификаторы сочетанием не являются: ожидается основная
            # клавиша. Удерживаемый модификатор запоминается: в Wayland Qt не
            # сообщает Shift, израсходованный на выбор заглавной буквы.
            self._held.add(key)
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
        if modifiers & Qt.KeyboardModifier.ShiftModifier or Qt.Key.Key_Shift in self._held:
            parts.append("Shift")
        if modifiers & Qt.KeyboardModifier.MetaModifier:
            parts.append("Super")

        name = _layout_independent_name(event.nativeScanCode()) or _key_name(key, event.text())
        if not name:
            return
        if is_reserved(parts, name):
            # Сочетание не назначается: система перехватит его раньше
            # приложения, а нажатие переключит виртуальный терминал.
            QToolTip.showText(
                self.mapToGlobal(self.rect().bottomLeft()),
                tr(
                    "Сочетания Ctrl+Alt+F1…F12 зарезервированы системой "
                    "для переключения терминалов"
                ),
                self,
            )
            return
        parts.append(name)
        self.setText("+".join(parts))


def is_reserved(modifiers: list[str], key: str) -> bool:
    """
    Признак сочетания, зарезервированного системой.

    Ctrl+Alt+F1…F12 (в том числе с Shift) переключают виртуальный
    терминал: сеанс рабочего стола уходит на другой экран, и нажатие
    выглядит для пользователя как зависание.
    """
    function_key = key.startswith("F") and key[1:].isdigit() and 1 <= int(key[1:]) <= 12
    return function_key and "Ctrl" in modifiers and "Alt" in modifiers


# Буквы и цифры по физическому положению клавиши: коды evdev клавиатуры с
# раскладкой QWERTY. Скан-код в Wayland и X11 на 8 больше кода evdev.
_EVDEV_KEYS: dict[int, str] = {
    **{code: char for code, char in zip(range(2, 11), "123456789")},
    11: "0",
    **{code: char for code, char in zip(range(16, 26), "QWERTYUIOP")},
    **{code: char for code, char in zip(range(30, 39), "ASDFGHJKL")},
    **{code: char for code, char in zip(range(44, 51), "ZXCVBNM")},
}
_SCANCODE_OFFSET = 8
# Скан-коды левого и правого Shift.
_SHIFT_SCANCODES = frozenset({42 + _SCANCODE_OFFSET, 54 + _SCANCODE_OFFSET})


def _layout_independent_name(scan_code: int) -> str:
    """
    Латинское имя буквенной или цифровой клавиши по её положению.

    Текст нажатия для этого непригоден: с Ctrl он превращается в
    управляющий символ, а при русской раскладке даёт кириллицу. Служба
    сочетаний рабочего стола сопоставляет буквенные клавиши по латинской
    раскладке, поэтому и запись ведётся латиницей.
    """
    return _EVDEV_KEYS.get(scan_code - _SCANCODE_OFFSET, "")


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
