"""
Регистрация и прослушивание глобальных сочетаний клавиш.

В сессии X11 применяется прямой перехват библиотекой pynput, работающей
поверх XGrabKey.

Обработчики pynput вызываются в отдельном потоке, поэтому результат
передаётся сигналом Qt: доставка в поток интерфейса выполняется очередью
событий, и прямых обращений к виджетам из чужого потока не происходит.
"""

from __future__ import annotations

from core.i18n import tr

import time
from dataclasses import dataclass

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

# Наименьший промежуток между двумя срабатываниями одного действия.
# Система доставляет повторные события нажатия при удержании клавиши, а
# перехваченная другой программой клавиша может прийти дважды. Без этого
# промежутка одно нажатие выполняло бы действие несколько раз.
REPEAT_GUARD_SEC = 0.4

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

    При недоступности X11 возвращается пустой перечень.
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


@dataclass(frozen=True)
class Combination:
    """
    Разобранное сочетание клавиш.

    Хранится набор модификаторов и допустимые обозначения основной
    клавиши. Обозначений может быть несколько: одна физическая клавиша
    выдаёт разные символы на разных уровнях раскладки.
    """

    modifiers: frozenset[str]
    keys: frozenset[str]

    @property
    def is_valid(self) -> bool:
        """Признак пригодного к перехвату сочетания."""
        return bool(self.keys)


def parse_combination(combination: str) -> Combination:
    """
    Разбор записи вида "Ctrl+Alt+R" на модификаторы и основную клавишу.

    Для сочетаний с Shift добавляются обозначения символов верхнего
    уровня той же физической клавиши: раскладка выдаёт с Shift другой
    символ, и сравнение по одному обозначению не проходит.
    """
    modifiers: set[str] = set()
    keys: set[str] = set()

    parts = [chunk.strip().lower() for chunk in combination.split("+") if chunk.strip()]
    for index, part in enumerate(parts):
        if part in _MODIFIERS and index < len(parts) - 1:
            modifiers.add(_MODIFIERS[part].strip("<>"))
            continue
        if part in _SPECIAL_KEYS:
            keys.add(_SPECIAL_KEYS[part].strip("<>"))
        elif len(part) > 1 and part.startswith("f") and part[1:].isdigit():
            keys.add(part)
        elif part in _MODIFIERS:
            # Сочетание состоит из одного модификатора.
            keys.add(_MODIFIERS[part].strip("<>"))
        else:
            keys.add(part[:1])

    if "shift" in modifiers and parts:
        # Основной клавишей считается последняя в записи сочетания.
        for keysym in shifted_keysyms(parts[-1]):
            # Числовое обозначение принимается наравне с именами и
            # покрывает символы, отсутствующие в таблице перехватчика.
            keys.add(str(keysym))

    return Combination(frozenset(modifiers), frozenset(keys))


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
        self._combinations: list[tuple[str, Combination]] = []
        # Набор удерживаемых модификаторов на момент нажатия клавиши.
        self._pressed_modifiers: set[str] = set()
        # Обозначения удерживаемых обычных клавиш: повторные события при
        # удержании и дубликаты от других перехватчиков не учитываются.
        self._pressed_keys: set[str] = set()
        # Время последнего срабатывания каждого действия.
        self._last_fired: dict[str, float] = {}
        # Признак приостановки: применяется при вводе нового сочетания.
        self._suspended = False

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

        if self._session.is_x11:
            self._start_pynput()
        else:
            self.unavailable.emit(
                tr("Глобальные клавиши поддерживаются только в сессии X11.")
            )

    def stop(self) -> None:
        """Снятие регистрации и остановка потока перехвата."""
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:  # noqa: BLE001 - остановка не должна ронять выход
                pass
            self._listener = None

    def suspend(self) -> None:
        """
        Временная приостановка перехвата.

        Применяется при вводе нового сочетания в настройках: иначе
        нажатие клавиш в поле ввода выполнило бы назначенное действие.
        """
        self._suspended = True
        self._pressed_modifiers.clear()
        self._pressed_keys.clear()

    def resume(self) -> None:
        """Возобновление перехвата после приостановки."""
        self._suspended = False
        self._pressed_modifiers.clear()
        self._pressed_keys.clear()

    def _start_pynput(self) -> None:
        """Запуск перехватчика клавиш для сессии X11."""
        try:
            from pynput import keyboard
        except ImportError:
            self.unavailable.emit(
                tr("Библиотека pynput не установлена: глобальные клавиши отключены.")
            )
            return

        self._combinations = []
        for action, combination in self._mapping.items():
            parsed = parse_combination(combination)
            if parsed.is_valid:
                self._combinations.append((action, parsed))
        if not self._combinations:
            return

        self._pressed_modifiers = set()
        self._pressed_keys = set()
        self._last_fired = {}
        try:
            listener = keyboard.Listener(
                on_press=self._on_press, on_release=self._on_release
            )
            listener.daemon = True
            listener.start()
        except Exception as error:  # noqa: BLE001 - причина уходит в интерфейс
            self.unavailable.emit(
                tr("Не удалось зарегистрировать клавиши: {0}").format(error)
            )
            return
        self._listener = listener

    @staticmethod
    def _modifier_name(key: object) -> str | None:
        """Обобщённое имя модификатора либо пустое значение."""
        name = getattr(key, "name", None)
        if not name:
            return None
        for prefix in ("ctrl", "alt", "shift", "cmd"):
            if name.startswith(prefix):
                # Левая и правая клавиши считаются одним модификатором,
                # как и правый Alt, применяемый в части раскладок.
                return prefix
        return None

    def _key_names(self, key: object) -> set[str]:
        """
        Обозначения нажатой клавиши, пригодные для сравнения.

        Возвращается несколько вариантов: имя специальной клавиши, символ
        и числовой код. Это покрывает и раскладки, и уровни клавиш.
        """
        names: set[str] = set()
        name = getattr(key, "name", None)
        if name:
            names.add(name)

        candidates = [key]
        listener = self._listener
        if listener is not None:
            try:
                # Приведение к основной раскладке: буквенные сочетания
                # должны работать при любом выбранном языке ввода.
                candidates.append(listener.canonical(key))
            except Exception:  # noqa: BLE001 - приведение не обязано удаваться
                pass

        for candidate in candidates:
            char = getattr(candidate, "char", None)
            if char:
                names.add(char.lower())
            code = getattr(candidate, "vk", None)
            if code:
                names.add(str(code))
            candidate_name = getattr(candidate, "name", None)
            if candidate_name:
                names.add(candidate_name)
        return names

    def _on_press(self, key: object) -> None:
        """Обработка нажатия в потоке перехватчика."""
        modifier = self._modifier_name(key)
        if modifier is not None:
            self._pressed_modifiers.add(modifier)
            return
        if self._suspended:
            return

        names = self._key_names(key)
        if names & self._pressed_keys:
            # Клавиша уже удерживается: повторное событие пропускается.
            return
        self._pressed_keys |= names

        pressed = frozenset(self._pressed_modifiers)
        moment = time.monotonic()
        for action, combination in self._combinations:
            # Набор модификаторов сравнивается целиком: иначе сочетание
            # без модификаторов срабатывало бы и при нажатых Ctrl или Alt,
            # то есть одно нажатие выполняло бы несколько действий.
            if combination.modifiers != pressed or not (names & combination.keys):
                continue
            if moment - self._last_fired.get(action, 0.0) < REPEAT_GUARD_SEC:
                # Повторное событие того же нажатия: часть программ
                # перехватывает клавишу и вызывает её доставку дважды.
                return
            self._last_fired[action] = moment
            self.activated.emit(action)
            return

    def _on_release(self, key: object) -> None:
        """Обработка отпускания в потоке перехватчика."""
        modifier = self._modifier_name(key)
        if modifier is not None:
            self._pressed_modifiers.discard(modifier)
            return
        self._pressed_keys -= self._key_names(key)
