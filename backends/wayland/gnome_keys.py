"""
Сочетания клавиш через собственные комбинации GNOME.

Портал GlobalShortcuts появился в GNOME только с версии 48. В более ранних
версиях приложение Wayland не может занять глобальную клавишу само, зато
GNOME позволяет назначить на клавишу любую команду: «Параметры» →
«Клавиатура» → «Комбинации клавиш» → «Дополнительные комбинации». Там
хранятся и сочетания LinScreen - по одной записи на действие.

Команда записи мгновенно передаёт действие работающему экземпляру через
его службу на шине D-Bus (утилита gdbus из состава GLib). Если приложение
не запущено, команда запускает его с ключом действия.

Записи хранятся в dconf по адресу
/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/linscreen-<действие>/
и видны в «Параметрах» как обычные пользовательские комбинации. Изменения,
сделанные там, сообщает сигнал Notify службы dconf.

Функции модуля блокирующие (запуск gsettings) и выполняются в фоновом
потоке либо из командной строки.
"""

from __future__ import annotations

import ast
import shlex
import shutil
import subprocess
import threading
from typing import Any

from PySide6.QtGui import QKeySequence

from core.actions import action_titles
from core.identity import BUS_INTERFACE, BUS_NAME, BUS_PATH

# Схема списка пользовательских комбинаций и схема отдельной записи.
MEDIA_KEYS_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
CUSTOM_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding"
CUSTOM_LIST_KEY = "custom-keybindings"
MEDIA_KEYS_PATH = "/org/gnome/settings-daemon/plugins/media-keys/"
CUSTOM_DIR = MEDIA_KEYS_PATH + "custom-keybindings/"
# Приставка имён записей приложения: по ней записи находятся при удалении.
ENTRY_PREFIX = "linscreen-"

# Схемы со встроенными сочетаниями GNOME: клавиша, занятая в них, до
# пользовательской комбинации не дойдёт.
SYSTEM_SCHEMAS = (
    "org.gnome.shell.keybindings",
    "org.gnome.desktop.wm.keybindings",
    "org.gnome.mutter.keybindings",
    "org.gnome.mutter.wayland.keybindings",
    MEDIA_KEYS_SCHEMA,
)

# Сигнал службы dconf об изменении значений.
DCONF_INTERFACE = "ca.desrt.dconf.Writer"
DCONF_PATH = "/ca/desrt/dconf/Writer/user"

# Предельное время работы одного вызова gsettings.
GSETTINGS_TIMEOUT_SEC = 10

# Модификаторы в записи GNOME и в записи Qt.
_TO_GNOME_MODIFIERS = {"CTRL": "<Control>", "ALT": "<Alt>", "SHIFT": "<Shift>", "LOGO": "<Super>"}
_FROM_GNOME_MODIFIERS = {
    "control": "Ctrl",
    "ctrl": "Ctrl",
    "primary": "Ctrl",
    "alt": "Alt",
    "mod1": "Alt",
    "shift": "Shift",
    "super": "Meta",
    "meta": "Meta",
    "mod4": "Meta",
}
# Названия клавиш xkb, которые Qt записывает иначе.
_FROM_GNOME_KEYS = {
    "page_up": "PgUp",
    "page_down": "PgDown",
    "escape": "Esc",
    "delete": "Del",
    "insert": "Ins",
    "space": "Space",
    "return": "Return",
}


# Порядок фоновых записей клавиш: запись с меньшим номером, начатая позже
# более новой, пропускается.
_write_lock = threading.Lock()
_latest_generation = 0


class GnomeKeysError(Exception):
    """Сбой обращения к настройкам GNOME."""


def available() -> bool:
    """Признак возможности записать пользовательские комбинации GNOME."""
    if shutil.which("gsettings") is None or shutil.which("gdbus") is None:
        return False
    try:
        _gsettings("list-keys", CUSTOM_SCHEMA)
    except GnomeKeysError:
        return False
    return True


def to_gnome_accelerator(combination: str) -> str:
    """Преобразование сочетания вида "Ctrl+Alt+R" в запись GNOME "<Control><Alt>r"."""
    from backends.wayland.hotkeys import to_portal_trigger

    trigger = to_portal_trigger(combination)
    if not trigger:
        return ""
    *modifiers, key = trigger.split("+")
    return "".join(_TO_GNOME_MODIFIERS.get(item, "") for item in modifiers) + key


def from_gnome_accelerator(accelerator: str) -> str:
    """Преобразование записи GNOME "<Control><Alt>r" в сочетание вида "Ctrl+Alt+R"."""
    text = accelerator.strip()
    modifiers: list[str] = []
    while text.startswith("<") and ">" in text:
        name, text = text[1 : text.index(">")], text[text.index(">") + 1 :]
        modifier = _FROM_GNOME_MODIFIERS.get(name.lower())
        if modifier and modifier not in modifiers:
            modifiers.append(modifier)
    if not text:
        return ""
    key = _FROM_GNOME_KEYS.get(text.lower(), text.upper() if len(text) == 1 else text)
    # Порядок модификаторов и написание клавиши приводятся к виду Qt.
    sequence = QKeySequence("+".join([*modifiers, key]))
    return sequence.toString() if not sequence.isEmpty() else ""


def entry_path(action: str) -> str:
    """Адрес записи действия в dconf."""
    return f"{CUSTOM_DIR}{ENTRY_PREFIX}{action.replace('_', '-')}/"


def action_of(path: str) -> str:
    """Действие по адресу записи либо пустая строка для чужой записи."""
    if not path.startswith(CUSTOM_DIR + ENTRY_PREFIX):
        return ""
    name = path[len(CUSTOM_DIR + ENTRY_PREFIX) :].split("/", 1)[0]
    return name.replace("-", "_")


def activation_command(action: str, launch: list[str], argument: str) -> str:
    """
    Команда, назначаемая на клавишу.

    Действие передаётся работающему экземпляру вызовом его службы D-Bus:
    это быстрее запуска второго процесса приложения. Если службы нет,
    приложение запускается с ключом действия. GNOME разбирает команду по
    правилам оболочки, но не выполняет её в оболочке, поэтому условие
    записано отдельным вызовом sh.
    """
    call = shlex.join(
        [
            "gdbus", "call", "--session",
            "--dest", BUS_NAME,
            "--object-path", BUS_PATH,
            "--method", f"{BUS_INTERFACE}.Activate",
            action,
        ]
    )
    script = f"{call} >/dev/null 2>&1 || exec {shlex.join([*launch, argument])}"
    return shlex.join(["sh", "-c", script])


def bind(
    mapping: dict[str, str], commands: dict[str, str]
) -> tuple[dict[str, str], dict[str, tuple[str, str]]]:
    """
    Запись сочетаний приложения в пользовательские комбинации GNOME.

    Сочетание, уже занятое встроенной или чужой комбинацией, не
    записывается: GNOME всё равно отдал бы клавишу первому владельцу.
    Возвращаются назначенные сочетания и занятые {действие: (сочетание,
    владелец)}. Записи действий без сочетания удаляются.
    """
    titles = action_titles()
    taken = _taken_accelerators()
    paths = _custom_paths()
    assigned: dict[str, str] = {}
    conflicts: dict[str, tuple[str, str]] = {}
    for action, combination in mapping.items():
        path = entry_path(action)
        accelerator = to_gnome_accelerator(combination)
        owner = taken.get(_normalized(accelerator)) if accelerator else None
        if owner:
            conflicts[action] = (combination, owner)
            accelerator = ""
        if not accelerator:
            _reset_entry(path)
            if path in paths:
                paths.remove(path)
            assigned[action] = ""
            continue
        schema = f"{CUSTOM_SCHEMA}:{path}"
        _gsettings("set", schema, "name", _literal(f"LinScreen: {titles.get(action, action)}"))
        _gsettings("set", schema, "command", _literal(commands.get(action, "")))
        _gsettings("set", schema, "binding", _literal(accelerator))
        if path not in paths:
            paths.append(path)
        assigned[action] = combination
    # Записи действий, исчезнувших из набора, тоже снимаются.
    for path in list(paths):
        action = action_of(path)
        if action and action not in mapping:
            _reset_entry(path)
            paths.remove(path)
    _write_paths(paths)
    return assigned, conflicts


def set_bindings(bindings: dict[str, str], generation: int = 0) -> None:
    """
    Замена одних только клавиш у существующих записей приложения.

    Пустое значение временно отключает запись: так на время ввода нового
    сочетания в настройках приложения GNOME перестаёт перехватывать его
    клавиши. Запись с номером меньше уже выполненной пропускается.
    """
    global _latest_generation

    with _write_lock:
        if generation < _latest_generation:
            return
        _latest_generation = generation
        paths = set(_custom_paths())
        for action, combination in bindings.items():
            path = entry_path(action)
            if path not in paths:
                continue
            accelerator = to_gnome_accelerator(combination) if combination else ""
            _gsettings("set", f"{CUSTOM_SCHEMA}:{path}", "binding", _literal(accelerator))


def read_binding(action: str) -> str | None:
    """
    Текущее сочетание записи действия в виде Qt.

    Пустое значение означает, что запись удалена из списка комбинаций или
    клавиша снята.
    """
    path = entry_path(action)
    if path not in _custom_paths():
        return ""
    value = _parse_string(_gsettings("get", f"{CUSTOM_SCHEMA}:{path}", "binding"))
    return from_gnome_accelerator(value)


def remove_all() -> list[str]:
    """Удаление всех записей приложения. Возвращаются названия удалённых действий."""
    if shutil.which("gsettings") is None:
        return []
    try:
        paths = _custom_paths()
    except GnomeKeysError:
        return []
    removed: list[str] = []
    kept: list[str] = []
    for path in paths:
        action = action_of(path)
        if action:
            _reset_entry(path)
            removed.append(action)
        else:
            kept.append(path)
    if removed:
        _write_paths(kept)
    return removed


def changed_actions(message: Any) -> list[str]:
    """Действия, записи которых затронуты сигналом Notify службы dconf."""
    body: tuple[Any, ...] = tuple(getattr(message, "body", ()))
    if len(body) < 2:
        return []
    prefix = str(body[0])
    keys = [prefix + str(item) for item in (body[1] or [""])]
    actions: list[str] = []
    for key in keys:
        # Изменение самого списка комбинаций касается всех записей.
        if key == MEDIA_KEYS_PATH + CUSTOM_LIST_KEY:
            return ["*"]
        action = action_of(key)
        if action and action not in actions:
            actions.append(action)
    return actions


def _gsettings(*arguments: str) -> str:
    """Вызов утилиты gsettings с системным окружением."""
    from core.runtime import child_environment

    try:
        result = subprocess.run(
            ["gsettings", *arguments],
            capture_output=True,
            text=True,
            timeout=GSETTINGS_TIMEOUT_SEC,
            env=child_environment(),
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise GnomeKeysError(str(error)) from error
    if result.returncode != 0:
        raise GnomeKeysError(result.stderr.strip() or f"gsettings {arguments[0]}")
    return result.stdout.strip()


def _literal(text: str) -> str:
    """Строка в записи GVariant, которую принимает gsettings."""
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _parse_string(value: str) -> str:
    """Разбор строки в записи GVariant."""
    try:
        parsed = ast.literal_eval(value)
    except (ValueError, SyntaxError):
        return value
    return parsed if isinstance(parsed, str) else ""


def _parse_list(value: str) -> list[str]:
    """Разбор списка строк в записи GVariant ("@as []", "['a', 'b']", "'a'")."""
    text = value.strip()
    if text.startswith("@as"):
        text = text[3:].strip()
    try:
        parsed = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return []
    if isinstance(parsed, str):
        return [parsed]
    if isinstance(parsed, (list, tuple)):
        return [str(item) for item in parsed]
    return []


def _custom_paths() -> list[str]:
    """Адреса всех пользовательских комбинаций."""
    return _parse_list(_gsettings("get", MEDIA_KEYS_SCHEMA, CUSTOM_LIST_KEY))


def _write_paths(paths: list[str]) -> None:
    """Запись списка пользовательских комбинаций."""
    value = "[" + ", ".join(_literal(path) for path in paths) + "]" if paths else "@as []"
    _gsettings("set", MEDIA_KEYS_SCHEMA, CUSTOM_LIST_KEY, value)


def _reset_entry(path: str) -> None:
    """Сброс полей записи: dconf удаляет пустую запись целиком."""
    schema = f"{CUSTOM_SCHEMA}:{path}"
    for key in ("name", "command", "binding"):
        try:
            _gsettings("reset", schema, key)
        except GnomeKeysError:
            continue


def _normalized(accelerator: str) -> str:
    """Запись сочетания без различий в написании модификаторов и регистре."""
    return from_gnome_accelerator(accelerator).lower()


def _taken_accelerators() -> dict[str, str]:
    """
    Клавиши, занятые встроенными и чужими комбинациями GNOME.

    Ключ - нормализованная запись сочетания, значение - название владельца.
    """
    taken: dict[str, str] = {}
    for schema in SYSTEM_SCHEMAS:
        try:
            listing = _gsettings("list-recursively", schema)
        except GnomeKeysError:
            continue
        for line in listing.splitlines():
            parts = line.split(" ", 2)
            if len(parts) < 3 or parts[1] == CUSTOM_LIST_KEY:
                continue
            for accelerator in _parse_list(parts[2]):
                if accelerator.strip():
                    taken.setdefault(_normalized(accelerator), f"GNOME: {parts[1]}")
    for path in _custom_paths():
        if action_of(path):
            continue
        schema = f"{CUSTOM_SCHEMA}:{path}"
        try:
            accelerator = _parse_string(_gsettings("get", schema, "binding"))
            name = _parse_string(_gsettings("get", schema, "name"))
        except GnomeKeysError:
            continue
        if accelerator.strip():
            taken.setdefault(_normalized(accelerator), name or path)
    taken.pop("", None)
    return taken
