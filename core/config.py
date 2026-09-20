"""
Менеджер настроек приложения.

Настройки хранятся в формате JSON в каталоге стандарта XDG и читаются
устойчиво к изменению структуры: незнакомые ключи игнорируются, а
отсутствующие заполняются значениями по умолчанию. Такой подход позволяет
обновлять приложение без миграций и не терять пользовательские правки.

Запись выполняется атомарно через временный файл и переименование: обрыв
питания в момент сохранения не приведёт к появлению повреждённого конфига.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

APPLICATION_NAME = "linscreen"

# Шаблон недопустимых символов в имени файла. Разделитель каталогов
# заменяется, чтобы шаблон имени не увёл файл в другой каталог.
_UNSAFE_FILENAME_CHARS = re.compile(r"[/\0]+")


# ===========================================================================
# Структуры настроек
# ===========================================================================


@dataclass
class PathSettings:
    """Каталоги сохранения и шаблоны имён файлов."""

    # Пустое значение означает каталог пользователя по стандарту XDG,
    # вычисляемый при первом обращении.
    images_dir: str = ""
    videos_dir: str = ""
    # Шаблоны имён обрабатываются функцией strftime.
    image_template: str = "Снимок %Y-%m-%d %H-%M-%S"
    video_template: str = "Запись %Y-%m-%d %H-%M-%S"


@dataclass
class ImageSettings:
    """Параметры сохранения снимков экрана."""

    # Один из вариантов: png, jpeg, webp, avif, bmp.
    image_format: str = "png"
    # Степень сжатия PNG в шкале Qt: 0 - без сжатия, 9 - максимальное.
    png_compression: int = 6
    jpeg_quality: int = 92
    webp_quality: int = 90
    webp_lossless: bool = False
    avif_quality: int = 70


@dataclass
class VideoSettings:
    """Параметры записи экрана."""

    # Идентификаторы профилей из encoder/profiles.py.
    profile_id: str = "mkv_h264"
    animation_profile_id: str = "gif"
    fps: int = 30
    show_cursor: bool = True
    # Значение из перечисления AudioMode: none, system, microphone, mix, separate.
    audio_mode: str = "none"
    # Имена устройств PulseAudio. Пустое значение означает устройство
    # по умолчанию, определяемое при старте записи.
    system_device: str = ""
    microphone_device: str = ""


@dataclass
class EncodingSettings:
    """
    Ручная настройка параметров кодирования.

    Пустое значение или ноль означают «взять из выбранного профиля»,
    поэтому настройки остаются необязательными: профиль продолжает
    работать как готовый набор параметров, а эти поля лишь уточняют его.
    """

    # Имена из перечислений VideoCodec и AudioCodec.
    video_codec: str = ""
    audio_codec: str = ""

    # Способ управления качеством: crf - постоянное качество,
    # bitrate - заданный битрейт.
    rate_mode: str = "crf"
    crf: int = 0
    video_bitrate: str = ""

    # Пресет скорости кодирования и интервал ключевых кадров.
    preset: str = ""
    keyint: int = 0
    pix_fmt: str = ""

    audio_bitrate: str = ""
    audio_sample_rate: int = 0
    audio_channels: int = 0

    # Дополнительные аргументы, добавляемые в конец команды перед путём
    # к файлу. Разбираются по правилам оболочки.
    extra_args: str = ""

    # Полностью ручная команда. При её использовании прочие параметры
    # кодирования не применяются.
    use_custom_command: bool = False
    custom_command: str = ""


@dataclass
class HotkeySettings:
    """Глобальные сочетания клавиш."""

    screenshot_region: str = "Print"
    screenshot_fullscreen: str = "Shift+Print"
    screenshot_window: str = "Alt+Print"
    record_region: str = "Ctrl+Alt+R"
    record_toggle_pause: str = "Ctrl+Alt+P"
    record_stop: str = "Ctrl+Alt+S"

    def as_mapping(self) -> dict[str, str]:
        """Соответствие имени действия и сочетания клавиш."""
        return {item.name: getattr(self, item.name) for item in fields(self)}


@dataclass
class GeneralSettings:
    """Общее поведение приложения."""

    # Пустое значение означает поиск бинарника в системных путях.
    ffmpeg_path: str = ""
    copy_to_clipboard: bool = True
    open_editor_after_capture: bool = True
    show_notifications: bool = True
    # Задержка перед снимком, позволяющая раскрыть меню или подсказку.
    capture_delay_ms: int = 0


@dataclass
class Settings:
    """Корневой набор настроек приложения."""

    paths: PathSettings = field(default_factory=PathSettings)
    images: ImageSettings = field(default_factory=ImageSettings)
    video: VideoSettings = field(default_factory=VideoSettings)
    encoding: EncodingSettings = field(default_factory=EncodingSettings)
    hotkeys: HotkeySettings = field(default_factory=HotkeySettings)
    general: GeneralSettings = field(default_factory=GeneralSettings)


# ===========================================================================
# Определение пользовательских каталогов
# ===========================================================================


def config_dir() -> Path:
    """Каталог хранения конфигурации по стандарту XDG."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APPLICATION_NAME


def _read_xdg_user_dir(key: str) -> Path | None:
    """
    Чтение пользовательского каталога из файла user-dirs.dirs.

    Файл содержит строки вида XDG_PICTURES_DIR="$HOME/Изображения" и
    учитывает локализованные названия каталогов, заданные при установке.
    """
    source = Path(
        os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    ) / "user-dirs.dirs"
    if not source.is_file():
        return None
    try:
        content = source.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    for line in content.splitlines():
        line = line.strip()
        if not line.startswith(key):
            continue
        _, _, value = line.partition("=")
        value = value.strip().strip('"')
        # В значении встречается подстановка $HOME, раскрываемая вручную.
        value = value.replace("$HOME", str(Path.home()))
        if value:
            return Path(value)
    return None


def default_images_dir() -> Path:
    """Каталог сохранения снимков по умолчанию."""
    base = _read_xdg_user_dir("XDG_PICTURES_DIR") or Path.home() / "Pictures"
    return base / "LinScreen"


def default_videos_dir() -> Path:
    """Каталог сохранения записей по умолчанию."""
    base = _read_xdg_user_dir("XDG_VIDEOS_DIR") or Path.home() / "Videos"
    return base / "LinScreen"


# ===========================================================================
# Загрузка, сохранение и производные значения
# ===========================================================================


def _merge(instance: Any, data: dict[str, Any]) -> None:
    """
    Рекурсивное наложение прочитанных значений на объект настроек.

    Значения с неподходящим типом отбрасываются: повреждённый вручную файл
    не должен приводить к падению приложения при старте.
    """
    for item in fields(instance):
        if item.name not in data:
            continue
        value = data[item.name]
        current = getattr(instance, item.name)
        if is_dataclass(current) and isinstance(value, dict):
            _merge(current, value)
        elif isinstance(current, bool) and isinstance(value, bool):
            setattr(instance, item.name, value)
        elif isinstance(current, int) and isinstance(value, int) and not isinstance(value, bool):
            setattr(instance, item.name, value)
        elif isinstance(current, str) and isinstance(value, str):
            setattr(instance, item.name, value)


def sanitize_filename(name: str) -> str:
    """Приведение имени файла к безопасному виду."""
    # Удаляются разделители каталогов и нулевые байты; пробелы сохраняются,
    # поскольку передача аргументов ведётся списками и экранирования не требует.
    cleaned = _UNSAFE_FILENAME_CHARS.sub("_", name).strip()
    return cleaned or "capture"


class ConfigManager:
    """Загрузка, хранение и сохранение настроек приложения."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or (config_dir() / "config.json")
        self._settings = Settings()

    @property
    def path(self) -> Path:
        """Путь к файлу конфигурации."""
        return self._path

    @property
    def settings(self) -> Settings:
        """Текущий набор настроек."""
        return self._settings

    def load(self) -> Settings:
        """Чтение настроек с диска. Отсутствие файла не является ошибкой."""
        self._settings = Settings()
        if not self._path.is_file():
            return self._settings
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # Повреждённый файл заменяется значениями по умолчанию:
            # приложение обязано запуститься при любом содержимом конфига.
            return self._settings
        if isinstance(data, dict):
            _merge(self._settings, data)
        return self._settings

    def save(self) -> None:
        """Атомарная запись настроек на диск."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Временный файл создаётся в том же каталоге, что гарантирует
        # нахождение обоих путей в одной файловой системе и атомарность
        # последующего переименования.
        temporary = self._path.with_suffix(".tmp")
        payload = json.dumps(asdict(self._settings), ensure_ascii=False, indent=2)
        temporary.write_text(payload + "\n", encoding="utf-8")
        os.replace(temporary, self._path)

    # ----------------------------------------------------- производные пути

    def images_dir(self) -> Path:
        """Действующий каталог сохранения снимков."""
        configured = self._settings.paths.images_dir
        return Path(configured).expanduser() if configured else default_images_dir()

    def videos_dir(self) -> Path:
        """Действующий каталог сохранения записей."""
        configured = self._settings.paths.videos_dir
        return Path(configured).expanduser() if configured else default_videos_dir()

    def build_image_path(self, extension: str) -> Path:
        """Путь нового файла снимка с учётом шаблона имени."""
        return self._build_path(
            self.images_dir(), self._settings.paths.image_template, extension
        )

    def build_video_path(self, extension: str) -> Path:
        """Путь нового файла записи с учётом шаблона имени."""
        return self._build_path(
            self.videos_dir(), self._settings.paths.video_template, extension
        )

    def _build_path(self, directory: Path, template: str, extension: str) -> Path:
        """
        Формирование уникального пути файла.

        При совпадении имён к нему добавляется порядковый номер: перезапись
        ранее сохранённого материала недопустима.
        """
        stem = sanitize_filename(time.strftime(template))
        candidate = directory / f"{stem}.{extension.lstrip('.')}"
        counter = 1
        while candidate.exists():
            candidate = directory / f"{stem} ({counter}).{extension.lstrip('.')}"
            counter += 1
        return candidate
