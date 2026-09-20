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

from core.i18n import tr

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

# Соответствие названий клавиш обозначениям протокола X11. Требуется для
# поиска символов, которые та же физическая клавиша выдаёт с Shift: часть
# клавиш меняет символ полностью, например PrintScreen с Shift выдаёт
# Sys_Req, и сочетание перестаёт совпадать.
_X11_KEY_NAMES: dict[str, str] = {
    "print": "Print",
    "printscreen": "Print",
    "prtsc": "Print",
    "space": "space",
    "tab": "Tab",
    "esc": "Escape",
    "escape": "Escape",
    "enter": "Return",
    "return": "Return",
    "insert": "Insert",
    "delete": "Delete",
    "home": "Home",
    "end": "End",
    "pageup": "Prior",
    "pagedown": "Next",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
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


def _x11_key_name(key: str) -> str:
    """Обозначение клавиши в терминах протокола X11."""
    if key in _X11_KEY_NAMES:
        return _X11_KEY_NAMES[key]
    if len(key) > 1 and key.startswith("f") and key[1:].isdigit():
        return key.upper()
    return key


def shifted_keysyms(key: str) -> list[int]:
    """
    Символы, выдаваемые той же физической клавишей при нажатом Shift.

    Раскладка клавиатуры сопоставляет одной клавише несколько символов по
    уровням: без Shift, с Shift и так далее для каждой группы раскладок.
    Перехватчик сравнивает именно символ, поэтому для сочетаний с Shift
    требуются все его варианты. Нечётные уровни соответствуют нажатому
    Shift.

    При недоступности X11 возвращается пустой перечень: в сессии Wayland
    перехват выполняется порталом, и эта поправка не нужна.
    """
    try:
        from Xlib import XK, display as xdisplay
    except ImportError:
        return []

    try:
        connection = xdisplay.Display()
    except Exception:  # noqa: BLE001 - отсутствие X11 не является ошибкой
        return []

    try:
        base = XK.string_to_keysym(_x11_key_name(key))
        if not base:
            return []
        keycode = connection.keysym_to_keycode(base)
        if not keycode:
            return []
        # Нечётные уровни таблицы соответствуют нажатому Shift в каждой
        # из групп раскладок.
        found = {connection.keycode_to_keysym(keycode, level) for level in (1, 3, 5, 7)}
        return sorted(value for value in found if value and value != base)
    except Exception:  # noqa: BLE001 - любая ошибка означает отсутствие поправки
        return []
    finally:
        try:
            connection.close()
        except Exception:  # noqa: BLE001 - соединение уже закрыто
            pass


def to_pynput_sequences(combination: str) -> list[str]:
    """
    Все обозначения сочетания, по которым его следует распознавать.

    Кроме основного обозначения перечень содержит варианты с символами
    верхнего уровня той же клавиши. Без них сочетания вида Shift+Print не
    срабатывают: клавиатура выдаёт другой символ, и сравнение не проходит.
    """
    base = to_pynput_sequence(combination)
    if not base:
        return []

    parts = [chunk.strip().lower() for chunk in combination.split("+") if chunk.strip()]
    if "shift" not in parts or len(parts) < 2:
        # Без Shift символ клавиши не меняется, поправка не требуется.
        return [base]

    main_key = parts[-1]
    if main_key == "shift":
        return [base]

    sequences = [base]
    prefix = base.rsplit("+", 1)[0]
    for keysym in shifted_keysyms(main_key):
        # Символ задаётся числом: в этом виде перехватчик принимает любые
        # обозначения, включая отсутствующие в его собственной таблице.
        variant = f"{prefix}+<{keysym}>"
        if variant not in sequences:
            sequences.append(variant)
    return sequences


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
                tr(
                    "Глобальные клавиши в сессии Wayland требуют портала "
                    "GlobalShortcuts. Действия доступны через меню в трее."
                )
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
                tr("Библиотека pynput не установлена: глобальные клавиши отключены.")
            )
            return

        handlers: dict[str, object] = {}
        for action, combination in self._mapping.items():
            handler = self._make_handler(action)
            for sequence in to_pynput_sequences(combination):
                # Замыкание фиксирует имя действия: сигнал испускается из
                # потока перехватчика и доставляется очередью событий Qt.
                # Все обозначения одного сочетания ведут к одному действию.
                handlers[sequence] = handler

        if not handlers:
            return
        try:
            listener = keyboard.GlobalHotKeys(handlers)
            listener.daemon = True
            listener.start()
        except Exception as error:  # noqa: BLE001 - причина уходит в интерфейс
            self.unavailable.emit(tr("Не удалось зарегистрировать клавиши: {0}").format(error))
            return
        self._listener = listener

    def _make_handler(self, action: str):  # type: ignore[no-untyped-def]
        """Создание обработчика одного сочетания клавиш."""

        def handler() -> None:
            """Обработчик, вызываемый потоком перехвата."""
            self.activated.emit(action)

        return handler
