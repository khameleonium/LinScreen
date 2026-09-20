"""
Перевод строк интерфейса на другие языки.

Исходным языком является русский: строки записаны прямо в коде, и без
словаря приложение работает как прежде. Перевод хранится отдельным файлом
в каталоге locale: ключом служит русская строка, значением - её перевод.
Отсутствующий перевод не является ошибкой, в этом случае показывается
исходная строка.

Формат файла выбран простым и общеизвестным, чтобы словарь можно было
править обычным текстовым редактором, не заглядывая в исходные тексты.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from core.runtime import bundle_dir, project_root

# Обозначение исходного языка: для него словарь не требуется.
SOURCE_LANGUAGE = "ru"

# Названия языков для выбора в настройках.
LANGUAGE_NAMES: dict[str, str] = {
    "ru": "Русский",
    "en": "English",
}

# Текущий словарь перевода. Пустой словарь означает исходный язык.
_catalog: dict[str, str] = {}
_language = SOURCE_LANGUAGE


def locale_dir() -> Path:
    """Каталог со словарями перевода."""
    # В собранном виде словари лежат рядом с прочими вложениями.
    bundled = bundle_dir()
    if bundled is not None:
        return bundled / "locale"
    return project_root() / "locale"


def available_languages() -> dict[str, str]:
    """
    Языки, доступные для выбора.

    Исходный язык присутствует всегда, остальные определяются наличием
    файлов словарей.
    """
    languages = {SOURCE_LANGUAGE: LANGUAGE_NAMES[SOURCE_LANGUAGE]}
    try:
        for path in sorted(locale_dir().glob("*.json")):
            code = path.stem
            languages[code] = LANGUAGE_NAMES.get(code, code)
    except OSError:
        # Каталог недоступен: остаётся только исходный язык.
        pass
    return languages


def detect_system_language() -> str:
    """
    Язык, заданный окружением пользователя.

    Применяется при значении настройки "auto". Учитываются переменные
    окружения по убыванию приоритета согласно стандарту POSIX.
    """
    for name in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        value = os.environ.get(name, "")
        if value:
            code = value.split(".")[0].split("_")[0].lower()
            if code in available_languages():
                return code
    return SOURCE_LANGUAGE


def set_language(code: str) -> str:
    """
    Выбор языка интерфейса.

    Возвращается фактически установленный язык: при отсутствии словаря
    приложение остаётся на исходном языке.
    """
    global _catalog, _language

    if code == "auto":
        code = detect_system_language()

    if code == SOURCE_LANGUAGE:
        _catalog = {}
        _language = SOURCE_LANGUAGE
        return _language

    try:
        data = json.loads((locale_dir() / f"{code}.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # Повреждённый или отсутствующий словарь не мешает работе.
        _catalog = {}
        _language = SOURCE_LANGUAGE
        return _language

    # Служебные ключи, начинающиеся с подчёркивания, в перевод не идут:
    # они хранят сведения о самом словаре.
    _catalog = {
        key: value
        for key, value in data.items()
        if isinstance(value, str) and not key.startswith("_")
    }
    _language = code
    return _language


def current_language() -> str:
    """Действующий язык интерфейса."""
    return _language


def tr(text: str) -> str:
    """
    Перевод строки на выбранный язык.

    При отсутствии перевода возвращается исходная строка, поэтому неполный
    словарь приводит лишь к смешанному языку, а не к пустым надписям.
    """
    return _catalog.get(text, text)
