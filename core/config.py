"""
Менеджер настроек приложения.

Настройки хранятся в текстовом файле формата INI в каталоге стандарта XDG.
Формат выбран ради читаемости: разделы имеют заголовки, каждое значение
снабжено поясняющим примечанием, а править файл можно любым текстовым
редактором. Предусмотрены перенос настроек в файл и обратно, что позволяет
переносить их между машинами.

Чтение устойчиво к изменению структуры: незнакомые ключи и разделы
игнорируются, отсутствующие заполняются значениями по умолчанию, значения
неподходящего вида отбрасываются. Благодаря этому обновление приложения
не требует преобразования файла, а повреждённый файл не мешает запуску.

Запись выполняется атомарно через временный файл и переименование: обрыв
питания в момент сохранения не приведёт к появлению повреждённого файла.
"""

from __future__ import annotations

import configparser
import json
import os
import re
import time
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

from core.i18n import tr

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
    # Одно сочетание начинает запись и останавливает её: отдельная клавиша
    # остановки не нужна, а запоминать приходится вдвое меньше.
    record_toggle: str = "Ctrl+Alt+R"
    record_toggle_pause: str = "Ctrl+Alt+P"

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
    # Всплывающие уведомления. Отключение не влияет на запись в журнал:
    # сообщения остаются доступными в окне журнала и в файле.
    show_notifications: bool = True
    # Скрытие собственных окон и панели записи на время съёмки, чтобы они
    # не попадали в кадр. Управление остаётся через значок в трее и
    # горячие клавиши.
    hide_while_recording: bool = True
    # Задержка перед снимком, позволяющая раскрыть меню или подсказку.
    capture_delay_ms: int = 0
    # Язык интерфейса: ru, en либо auto для определения по окружению.
    language: str = "ru"


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
# Описание файла настроек
# ===========================================================================

# Заголовки разделов, поясняющие их назначение в самом файле.
SECTION_TITLES: dict[str, str] = {
    "paths": "Каталоги сохранения и шаблоны имён файлов",
    "images": "Параметры сохранения снимков экрана",
    "video": "Параметры записи экрана",
    "encoding": "Ручная настройка кодирования, уточняющая выбранный профиль",
    "hotkeys": "Глобальные сочетания клавиш; пустое значение отключает действие",
    "general": "Общее поведение приложения",
}

# Примечания к отдельным значениям. Выводятся в файл перед каждой строкой,
# чтобы назначение параметра было понятно без обращения к документации.
FIELD_COMMENTS: dict[str, dict[str, str]] = {
    "paths": {
        "images_dir": "Каталог снимков. Пусто — стандартный каталог изображений",
        "videos_dir": "Каталог записей. Пусто — стандартный каталог видео",
        "image_template": "Шаблон имени снимка, подстановки strftime: %Y %m %d %H %M %S",
        "video_template": "Шаблон имени записи, подстановки те же",
    },
    "images": {
        "image_format": "Формат снимков: png, jpeg, webp, avif, bmp",
        "png_compression": "Сжатие PNG: 0 — без сжатия, 9 — наименьший файл",
        "jpeg_quality": "Качество JPEG от 1 до 100",
        "webp_quality": "Качество WEBP от 1 до 99",
        "webp_lossless": "Сохранять WEBP без потерь: да или нет",
        "avif_quality": "Качество AVIF от 1 до 100",
    },
    "video": {
        "profile_id": "Профиль записи, например mkv_h264, mp4_h264, webm_vp9, gif",
        "animation_profile_id": "Профиль анимации: gif, webp_anim, apng",
        "fps": "Частота кадров захвата",
        "show_cursor": "Записывать указатель мыши: да или нет",
        "audio_mode": "Источник звука: none, system, microphone, mix, separate",
        "system_device": "Имя источника системного звука. Пусто — по умолчанию",
        "microphone_device": "Имя микрофона. Пусто — по умолчанию",
    },
    "encoding": {
        "video_codec": "Энкодер видео: h264, h265, av1, vp9, ffv1, prores, "
        "mpeg4. Пусто — из профиля",
        "audio_codec": "Энкодер звука: opus, aac, flac, pcm, mp3. Пусто — из профиля",
        "rate_mode": "Управление качеством: crf — постоянное качество, bitrate — битрейт",
        "crf": "Значение постоянного качества. Ноль — из профиля",
        "video_bitrate": "Битрейт видео, например 8000k. Пусто — из профиля",
        "preset": "Пресет скорости: ultrafast … veryslow. Пусто — из профиля",
        "keyint": "Интервал ключевых кадров. Ноль — из профиля",
        "pix_fmt": "Формат пикселей, например yuv420p. Пусто — из профиля",
        "audio_bitrate": "Битрейт звука, например 160k. Пусто — из профиля",
        "audio_sample_rate": "Частота дискретизации звука. Ноль — из профиля",
        "audio_channels": "Число каналов звука. Ноль — из профиля",
        "extra_args": "Дополнительные аргументы FFmpeg перед путём к файлу",
        "use_custom_command": "Использовать собственную команду: да или нет",
        "custom_command": "Образец команды с подстановками {ffmpeg} "
        "{video_input} {audio_input} {output} {fps} {width} {height}",
    },
    "hotkeys": {
        "screenshot_region": "Снимок выделенной области",
        "screenshot_fullscreen": "Снимок всего экрана",
        "screenshot_window": "Снимок активного окна",
        "record_toggle": "Начать запись области и остановить её тем же " "сочетанием",
        "record_toggle_pause": "Пауза и продолжение записи",
    },
    "general": {
        "ffmpeg_path": "Путь к FFmpeg. Пусто — вложенный либо системный",
        "copy_to_clipboard": "Копировать снимок в буфер обмена: да или нет",
        "open_editor_after_capture": "Открывать редактор после снимка: да или нет",
        "show_notifications": "Показывать всплывающие уведомления: да или нет. "
        "Отключение не влияет на запись в журнал",
        "hide_while_recording": "Скрывать окна программы и панель записи "
        "во время записи: да или нет",
        "capture_delay_ms": "Задержка перед снимком в миллисекундах",
        "language": "Язык интерфейса: ru, en либо auto для определения по окружению",
    },
}

# Прежние имена значений, принимаемые при чтении файла. Позволяют
# сохранить настройки пользователя после переименования параметра.
FIELD_ALIASES: dict[str, dict[str, tuple[str, ...]]] = {
    "hotkeys": {"record_toggle": ("record_region",)},
}

# Обозначения истины и лжи в файле настроек.
TRUE_VALUES = frozenset({"да", "yes", "true", "1", "вкл", "on"})
FALSE_VALUES = frozenset({"нет", "no", "false", "0", "выкл", "off"})


def _format_value(value: Any) -> str:
    """Представление значения в виде строки файла настроек."""
    if isinstance(value, bool):
        # Русские обозначения читаются понятнее, чем true и false.
        return "да" if value else "нет"
    text = str(value)
    # Многострочное значение продолжается строками с отступом: таково
    # правило формата INI для переноса.
    return text.replace("\n", "\n\t")


def _parse_value(current: Any, text: str) -> Any:
    """
    Преобразование строки файла к типу текущего значения.

    Возвращается исходное значение, если строка не соответствует типу:
    ошибка в файле не должна приводить к отказу приложения.
    """
    text = text.strip()
    if isinstance(current, bool):
        lowered = text.lower()
        if lowered in TRUE_VALUES:
            return True
        if lowered in FALSE_VALUES:
            return False
        return current
    if isinstance(current, int):
        try:
            return int(text)
        except ValueError:
            return current
    return text


def dump_ini(settings: "Settings") -> str:
    """Представление настроек в виде текста файла с примечаниями."""
    lines = [
        "# Настройки LinScreen",
        "#",
        "# Файл можно править вручную любым текстовым редактором.",
        "# Пустое значение означает значение по умолчанию.",
        "# Неизвестные строки и разделы при чтении пропускаются.",
        "",
    ]
    for section in fields(settings):
        group = getattr(settings, section.name)
        if not is_dataclass(group):
            continue
        title = SECTION_TITLES.get(section.name, "")
        if title:
            # Примечания переводятся при записи: сами строки объявлены на
            # уровне модуля и переводу при загрузке не подлежат.
            lines.append(f"# {tr(title)}")
        lines.append(f"[{section.name}]")
        comments = FIELD_COMMENTS.get(section.name, {})
        for item in fields(group):
            comment = comments.get(item.name)
            if comment:
                lines.append(f"# {tr(comment)}")
            lines.append(f"{item.name} = {_format_value(getattr(group, item.name))}")
        lines.append("")
    return "\n".join(lines)


def parse_ini(text: str, settings: "Settings") -> None:
    """Наложение значений из текста файла на переданный набор настроек."""
    parser = configparser.RawConfigParser()
    # Обычный разборщик приводит имена ключей к нижнему регистру, что для
    # имён полей допустимо, но подстановка значений отключена: шаблоны
    # имён файлов содержат знак процента.
    parser.read_string(text)

    for section in fields(settings):
        if not parser.has_section(section.name):
            continue
        group = getattr(settings, section.name)
        if not is_dataclass(group):
            continue
        aliases = FIELD_ALIASES.get(section.name, {})
        for item in fields(group):
            # Значение ищется под текущим именем, а при его отсутствии -
            # под прежними, чтобы переименование параметра не сбрасывало
            # настройку пользователя.
            names = (item.name, *aliases.get(item.name, ()))
            found = next((name for name in names if parser.has_option(section.name, name)), None)
            if found is None:
                continue
            raw = parser.get(section.name, found)
            current = getattr(group, item.name)
            setattr(group, item.name, _parse_value(current, raw))


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
    source = (
        Path(os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")) / "user-dirs.dirs"
    )
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
        self._path = path or (config_dir() / "config.ini")
        self._settings = Settings()

    @property
    def path(self) -> Path:
        """Путь к файлу конфигурации."""
        return self._path

    @property
    def settings(self) -> Settings:
        """Текущий набор настроек."""
        return self._settings

    @property
    def legacy_path(self) -> Path:
        """Путь файла настроек прежнего формата."""
        return self._path.with_name("config.json")

    def load(self) -> Settings:
        """
        Чтение настроек с диска.

        Отсутствие файла не является ошибкой. Настройки прежнего формата
        переносятся однократно и сразу сохраняются в новом виде, поэтому
        обновление приложения не требует действий пользователя.
        """
        self._settings = Settings()
        if self._path.is_file():
            self._read_ini()
            return self._settings

        if self.legacy_path.is_file():
            self._read_legacy_json()
            try:
                self.save()
            except OSError:
                # Перенос не удался: настройки всё равно прочитаны и
                # приложение может работать.
                pass
        return self._settings

    def _read_ini(self) -> None:
        """Чтение файла текущего формата."""
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError:
            return
        try:
            parse_ini(text, self._settings)
        except configparser.Error:
            # Повреждённый файл заменяется значениями по умолчанию:
            # приложение обязано запуститься при любом его содержимом.
            self._settings = Settings()

    def _read_legacy_json(self) -> None:
        """Чтение настроек прежнего формата для переноса."""
        try:
            data = json.loads(self.legacy_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(data, dict):
            _merge(self._settings, data)

    def save(self) -> None:
        """Атомарная запись настроек на диск."""
        self._write(self._path)

    def export_to(self, path: Path) -> Path:
        """
        Сохранение копии настроек в указанный файл.

        Копия совпадает с рабочим файлом и пригодна для переноса на другую
        машину либо для хранения нескольких наборов настроек.
        """
        self._write(path)
        return path

    def import_from(self, path: Path) -> Settings:
        """
        Чтение настроек из указанного файла с немедленным сохранением.

        Значения накладываются на текущие: отсутствующие в файле разделы
        сохраняют прежние значения, а не сбрасываются.
        """
        text = Path(path).read_text(encoding="utf-8")
        parse_ini(text, self._settings)
        self.save()
        return self._settings

    def _write(self, path: Path) -> None:
        """Атомарная запись настроек в указанный файл."""
        path.parent.mkdir(parents=True, exist_ok=True)
        # Временный файл создаётся в том же каталоге, что гарантирует
        # нахождение обоих путей в одной файловой системе и атомарность
        # последующего переименования.
        temporary = path.with_suffix(".tmp")
        temporary.write_text(dump_ini(self._settings), encoding="utf-8")
        os.replace(temporary, path)

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
        return self._build_path(self.images_dir(), self._settings.paths.image_template, extension)

    def build_video_path(self, extension: str) -> Path:
        """Путь нового файла записи с учётом шаблона имени."""
        return self._build_path(self.videos_dir(), self._settings.paths.video_template, extension)

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
