"""
Регистрация и прослушивание глобальных сочетаний клавиш.

Способ перехвата определяется типом сессии:
    * X11 - прямой перехват библиотекой pynput, работающей поверх XGrabKey;
    * Wayland - интерфейс org.freedesktop.portal.GlobalShortcuts, поскольку
      композитор не позволяет приложению перехватывать ввод самостоятельно.

Обработчики pynput вызываются в отдельном потоке, поэтому результат
передаётся сигналом Qt: доставка в поток интерфейса выполняется очередью
событий, и прямых обращений к виджетам из чужого потока не происходит.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from core.session import DesktopSession

# Соответствие названий клавиш-модификаторов формату pynput.
_MODIFIERS: dict[str, str] = {
    "ctrl": "<ctrl>",
    "control": "<ctrl>",
    "alt": "<alt>",
    "shift": "<shift>",
    "super": "<cmd>",
    "meta": "<cmd>",
    "win": "<cmd>",
}

# Соответствие названий специальных клавиш формату pynput.
_SPECIAL_KEYS: dict[str, str] = {
    "print": "<print_screen>",
    "printscreen": "<print_screen>",
    "prtsc": "<print_screen>",
    "space": "<space>",
    "tab": "<tab>",
    "esc": "<esc>",
    "escape": "<esc>",
    "enter": "<enter>",
    "return": "<enter>",
    "insert": "<insert>",
    "delete": "<delete>",
    "home": "<home>",
    "end": "<end>",
    "pageup": "<page_up>",
    "pagedown": "<page_down>",
    "up": "<up>",
    "down": "<down>",
    "left": "<left>",
    "right": "<right>",
}


def to_pynput_sequence(combination: str) -> str:
    """
    Преобразование сочетания вида "Ctrl+Alt+R" в формат pynput.

    Результат имеет вид "<ctrl>+<alt>+r"; функциональные клавиши
    записываются как "<f5>", специальные - согласно таблице соответствий.
    """
    parts: list[str] = []
    for chunk in combination.split("+"):
        key = chunk.strip().lower()
        if not key:
            continue
        if key in _MODIFIERS:
            parts.append(_MODIFIERS[key])
        elif key in _SPECIAL_KEYS:
            parts.append(_SPECIAL_KEYS[key])
        elif len(key) > 1 and key.startswith("f") and key[1:].isdigit():
            # Функциональные клавиши записываются в угловых скобках.
            parts.append(f"<{key}>")
        else:
            # Обычный символ передаётся в нижнем регистре как есть.
            parts.append(key[:1])
    return "+".join(parts)


class HotkeyManager(QObject):
    """Менеджер глобальных сочетаний клавиш с автовыбором способа перехвата."""

    # Срабатывание сочетания: передаётся имя действия из настроек.
    activated = Signal(str)
    # Сообщение о невозможности регистрации, показываемое пользователю.
    unavailable = Signal(str)

    def __init__(self, session: DesktopSession, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._session = session
        self._listener = None
        self._mapping: dict[str, str] = {}

    @property
    def is_active(self) -> bool:
        """Признак работающего перехватчика."""
        return self._listener is not None

    def apply(self, mapping: dict[str, str]) -> None:
        """
        Регистрация набора сочетаний.

        Предыдущая регистрация снимается полностью: библиотека не
        поддерживает изменение отдельной комбинации на лету.
        """
        self.stop()
        self._mapping = {name: value for name, value in mapping.items() if value.strip()}
        if not self._mapping:
            return

        if self._session.supports_native_hotkeys:
            self._start_pynput()
        else:
            # Портал GlobalShortcuts подключается отдельным бэкендом;
            # до его появления пользователь работает через меню трея.
            self.unavailable.emit(
                "Глобальные клавиши в сессии Wayland требуют портала "
                "GlobalShortcuts. Действия доступны через меню в трее."
            )

    def stop(self) -> None:
        """Снятие регистрации и остановка потока перехвата."""
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:  # noqa: BLE001 - остановка не должна ронять выход
                pass
            self._listener = None

    def _start_pynput(self) -> None:
        """Запуск перехватчика клавиш для сессии X11."""
        try:
            from pynput import keyboard
        except ImportError:
            self.unavailable.emit(
                "Библиотека pynput не установлена: глобальные клавиши отключены."
            )
            return

        handlers: dict[str, object] = {}
        for action, combination in self._mapping.items():
            sequence = to_pynput_sequence(combination)
            if not sequence:
                continue
            # Замыкание фиксирует имя действия: сигнал испускается из
            # потока перехватчика и доставляется очередью событий Qt.
            handlers[sequence] = self._make_handler(action)

        if not handlers:
            return
        try:
            listener = keyboard.GlobalHotKeys(handlers)
            listener.daemon = True
            listener.start()
        except Exception as error:  # noqa: BLE001 - причина уходит в интерфейс
            self.unavailable.emit(f"Не удалось зарегистрировать клавиши: {error}")
            return
        self._listener = listener

    def _make_handler(self, action: str):  # type: ignore[no-untyped-def]
        """Создание обработчика одного сочетания клавиш."""

        def handler() -> None:
            """Обработчик, вызываемый потоком перехвата."""
            self.activated.emit(action)

        return handler
