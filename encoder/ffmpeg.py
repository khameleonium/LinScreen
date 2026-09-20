"""
Обнаружение бинарника FFmpeg, опрос его возможностей и разбор вывода.

Зона ответственности модуля:
    * поиск исполняемых файлов ffmpeg и ffprobe в системе;
    * однократный опрос сборки на предмет доступных энкодеров, мультиплексоров,
      демультиплексоров и фильтров;
    * разбор машиночитаемого потока "-progress" в структуру ProgressReport;
    * выделение осмысленных строк ошибок из журнала stderr.

Модуль намеренно не зависит от Qt и не содержит ни одного элемента интерфейса:
это делает его пригодным для модульных тестов без графической подсистемы.
Функция probe_capabilities() выполняет блокирующие вызовы subprocess, поэтому
вызывать её из GUI-потока запрещено - для этого в encoder/process.py
предусмотрена обёртка CapabilityProbe, работающая в пуле потоков.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from core.runtime import bundle_dir, child_environment

# Тайм-аут одного опроса возможностей. Нормальный ответ приходит за доли
# секунды, превышение означает зависший или подменённый бинарник.
PROBE_TIMEOUT_SEC = 15.0

# Каталоги, просматриваемые при отсутствии бинарника в PATH. Порядок задаёт
# приоритет: собранная вручную сборка в /usr/local обычно свежее системной.
FALLBACK_SEARCH_PATHS: tuple[str, ...] = (
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    "/opt/ffmpeg/bin",
    "/var/lib/flatpak/exports/bin",
    "/snap/bin",
)

# Демультиплексоры и фильтры, без которых отдельные режимы захвата недоступны.
X11_DEMUXER = "x11grab"
PULSE_DEMUXER = "pulse"
ALSA_DEMUXER = "alsa"
PIPEWIRE_FILTER = "pipewiregrab"
KMS_DEMUXER = "kmsgrab"


class FFmpegNotFoundError(RuntimeError):
    """Бинарник FFmpeg отсутствует в системе или недоступен для запуска."""


class FFmpegProbeError(RuntimeError):
    """Опрос возможностей завершился ошибкой или тайм-аутом."""


# ===========================================================================
# Поиск исполняемых файлов
# ===========================================================================


def _is_executable(path: Path) -> bool:
    """Проверка того, что путь указывает на исполняемый обычный файл."""
    return path.is_file() and os.access(path, os.X_OK)


def find_binary(name: str, explicit_path: str | None = None) -> str:
    """
    Поиск исполняемого файла по имени с учётом пути из настроек.

    Порядок просмотра: явно заданный путь, вложенная в сборку копия,
    затем PATH и типовые каталоги дистрибутивов. Возвращается строка,
    пригодная для передачи первым элементом списка аргументов процесса.
    """
    # Явно указанный пользователем путь имеет наивысший приоритет, но
    # проверяется на пригодность: битая настройка не должна ронять запуск.
    if explicit_path:
        candidate = Path(explicit_path).expanduser()
        if _is_executable(candidate):
            return str(candidate)

    # Копия, вложенная в собранный файл, заведомо совместима с приложением
    # и не зависит от состава пакетов в системе.
    bundled = bundle_dir()
    if bundled is not None:
        candidate = bundled / name
        if _is_executable(candidate):
            return str(candidate)

    # Штатный поиск по переменной окружения PATH.
    located = shutil.which(name)
    if located:
        return located

    # Ручной просмотр каталогов на случай урезанного PATH, характерного
    # для процессов, запущенных менеджером автозапуска сессии.
    for directory in FALLBACK_SEARCH_PATHS:
        candidate = Path(directory) / name
        if _is_executable(candidate):
            return str(candidate)

    raise FFmpegNotFoundError(
        f"Исполняемый файл {name} не найден. "
        "Требуется установить пакет ffmpeg или указать путь в настройках."
    )


def find_ffmpeg(explicit_path: str | None = None) -> str:
    """Поиск бинарника ffmpeg."""
    return find_binary("ffmpeg", explicit_path)


def find_ffprobe(explicit_path: str | None = None) -> str:
    """Поиск бинарника ffprobe, используемого для чтения свойств файлов."""
    return find_binary("ffprobe", explicit_path)


# ===========================================================================
# Опрос возможностей сборки
# ===========================================================================


@dataclass(frozen=True)
class FFmpegCapabilities:
    """Снимок возможностей конкретной установленной сборки FFmpeg."""

    path: str
    version: str
    encoders: frozenset[str]
    muxers: frozenset[str]
    demuxers: frozenset[str]
    filters: frozenset[str]

    def has_encoder(self, name: str) -> bool:
        """Проверка наличия энкодера, например libsvtav1."""
        return name in self.encoders

    def has_muxer(self, name: str) -> bool:
        """Проверка наличия мультиплексора, например matroska."""
        return name in self.muxers

    def has_demuxer(self, name: str) -> bool:
        """Проверка наличия демультиплексора или устройства ввода."""
        return name in self.demuxers

    def has_filter(self, name: str) -> bool:
        """Проверка наличия фильтра, например palettegen."""
        return name in self.filters

    @property
    def can_capture_x11(self) -> bool:
        """Признак возможности прямого захвата экрана в сессии X11."""
        return self.has_demuxer(X11_DEMUXER)

    @property
    def can_capture_pipewire(self) -> bool:
        """Признак наличия встроенного источника PipeWire."""
        # Фильтр pipewiregrab появился в сборках начиная с FFmpeg 7.1 и
        # позволяет обойтись без посредника в виде GStreamer.
        return self.has_filter(PIPEWIRE_FILTER)

    @property
    def can_capture_pulse(self) -> bool:
        """
        Признак поддержки источников PulseAudio и PipeWire.

        Проверяется отдельно от ALSA: имена устройств приложение получает
        у звукового сервера, и без этого демультиплексора они непригодны,
        даже если захват через ALSA возможен.
        """
        return self.has_demuxer(PULSE_DEMUXER)

    @property
    def can_capture_system_audio(self) -> bool:
        """Признак доступности хотя бы одного способа захвата звука."""
        return self.has_demuxer(PULSE_DEMUXER) or self.has_demuxer(ALSA_DEMUXER)

    @property
    def can_build_gif_palette(self) -> bool:
        """Признак доступности двухпроходного алгоритма палитры для GIF."""
        return self.has_filter("palettegen") and self.has_filter("paletteuse")

    def missing_essentials(self) -> list[str]:
        """
        Перечень отсутствующих компонентов, критичных для работы приложения.

        Результат предназначен для показа пользователю при первом запуске,
        чтобы не выяснять ограничения сборки в момент начала записи.
        """
        problems: list[str] = []
        if not self.has_encoder("libx264"):
            problems.append("Отсутствует энкодер libx264: недоступны профили H.264.")
        if not self.has_muxer("matroska"):
            problems.append("Отсутствует мультиплексор matroska: недоступен формат MKV.")
        if not self.can_build_gif_palette:
            problems.append("Отсутствуют фильтры palettegen/paletteuse: недоступен GIF.")
        if not self.can_capture_pulse:
            problems.append(
                "Отсутствует источник pulse: запись звука недоступна, так как "
                "имена устройств приложение получает у звукового сервера."
            )
        if not (self.can_capture_x11 or self.can_capture_pipewire):
            problems.append(
                "Отсутствуют источники захвата x11grab и pipewiregrab: "
                "запись возможна только через внешние утилиты."
            )
        return problems


def _run_probe(args: Sequence[str], timeout: float = PROBE_TIMEOUT_SEC) -> str:
    """
    Запуск вспомогательной команды FFmpeg с возвратом её вывода.

    Вызов блокирующий и рассчитан на выполнение в отдельном потоке.
    """
    # Локаль принудительно приводится к C: разбор ведётся по английским
    # заголовкам разделов, а локализованный вывод сломал бы регулярные
    # выражения. Прочие переменные окружения наследуются без изменений.
    environment = child_environment()
    environment["LC_ALL"] = "C"
    environment["LANG"] = "C"

    try:
        completed = subprocess.run(
            list(args),
            # Ввод закрыт: справочные команды интерактивного ввода не ждут.
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            env=environment,
            # Оболочка не используется: аргументы передаются списком,
            # поэтому спецсимволы в путях интерпретации не подлежат.
            shell=False,
            check=False,
        )
    except FileNotFoundError as error:
        raise FFmpegNotFoundError(f"Не удалось запустить {args[0]}") from error
    except subprocess.TimeoutExpired as error:
        raise FFmpegProbeError(f"Команда {args[0]} не ответила за {timeout} с") from error

    if completed.returncode != 0 and not completed.stdout:
        # Часть справочных команд пишет в stderr, поэтому ненулевой код
        # считается ошибкой только при полностью пустом stdout.
        raise FFmpegProbeError(completed.stderr.strip()[:500] or "Неизвестная ошибка опроса")
    return completed.stdout


# Допустимое имя энкодера, мультиплексора или фильтра. Отсекает элементы
# легенды и служебные разделители, попадающие в таблицы вывода FFmpeg.
_VALID_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$")


def parse_version(text: str) -> str:
    """Извлечение номера версии из вывода команды "-version"."""
    match = re.search(r"ffmpeg version (\S+)", text)
    return match.group(1) if match else "unknown"


def parse_encoders(text: str) -> frozenset[str]:
    """
    Разбор таблицы энкодеров.

    Строка таблицы имеет вид " V....D libx264   libx264 H.264 ...", где
    первая группа символов описывает свойства, а второй столбец - имя.
    """
    names: set[str] = set()
    # Таблице предшествует легенда с расшифровкой флагов, отделённая
    # строкой из дефисов. Разбор начинается только после разделителя,
    # иначе в результат попадут элементы легенды вида "V..... = Video".
    body_reached = False
    for line in text.splitlines():
        if not body_reached:
            body_reached = line.strip().startswith("---")
            continue
        match = re.match(r"^\s*[VASFXBDT.]{6}\s+(\S+)", line)
        if match and _VALID_NAME.match(match.group(1)):
            names.add(match.group(1))
    return frozenset(names)


def _parse_format_table(text: str) -> frozenset[str]:
    """
    Разбор таблиц мультиплексоров и демультиплексоров.

    Строка имеет вид " DE matroska  Matroska" либо " D  x11grab  X11 capture".
    Имя может содержать перечисление через запятую, которое разворачивается
    в отдельные элементы множества.
    """
    names: set[str] = set()
    for line in text.splitlines():
        match = re.match(r"^\s*[DE]{1,2}\s+([A-Za-z0-9_,\-]+)\s", line)
        if not match:
            continue
        for alias in match.group(1).split(","):
            alias = alias.strip()
            if alias:
                names.add(alias)
    return frozenset(names)


def parse_muxers(text: str) -> frozenset[str]:
    """Разбор таблицы мультиплексоров."""
    return _parse_format_table(text)


def parse_demuxers(text: str) -> frozenset[str]:
    """Разбор таблицы демультиплексоров и устройств ввода."""
    return _parse_format_table(text)


def parse_filters(text: str) -> frozenset[str]:
    """
    Разбор таблицы фильтров.

    Строка имеет вид " ... palettegen  V->V  Find the optimal palette".
    Число символов в столбце признаков менялось между версиями FFmpeg:
    до восьмой их было три, начиная с восьмой - два, поэтому допускаются
    оба варианта. Строки легенды не содержат обозначения потоков со
    стрелкой и в результат не попадают.
    """
    names: set[str] = set()
    for line in text.splitlines():
        match = re.match(r"^\s*[TSC.]{2,3}\s+(\S+)\s+\S+->\S+", line)
        if match and _VALID_NAME.match(match.group(1)):
            names.add(match.group(1))
    return frozenset(names)


def probe_capabilities(ffmpeg_path: str | None = None) -> FFmpegCapabilities:
    """
    Сбор сведений о возможностях сборки FFmpeg.

    Выполняется пять коротких блокирующих запусков. Вызов предназначен для
    фонового потока: обёртка CapabilityProbe из encoder/process.py запускает
    эту функцию в пуле и возвращает результат сигналом.
    """
    binary = ffmpeg_path or find_ffmpeg()
    base = [binary, "-hide_banner"]

    version_text = _run_probe([*base, "-version"])
    # Устройства ввода перечисляются отдельной командой: x11grab и pulse
    # присутствуют в списке демультиплексоров не во всех версиях FFmpeg,
    # поэтому таблицы объединяются.
    demuxer_text = _run_probe([*base, "-demuxers"])
    device_text = _run_probe([*base, "-devices"])

    return FFmpegCapabilities(
        path=binary,
        version=parse_version(version_text),
        encoders=parse_encoders(_run_probe([*base, "-encoders"])),
        muxers=parse_muxers(_run_probe([*base, "-muxers"])),
        demuxers=parse_demuxers(demuxer_text) | parse_demuxers(device_text),
        filters=parse_filters(_run_probe([*base, "-filters"])),
    )


# ===========================================================================
# Разбор потока прогресса
# ===========================================================================


def _to_int(value: str, default: int = 0) -> int:
    """Преобразование значения прогресса в целое с защитой от "N/A"."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value: str, default: float = 0.0) -> float:
    """Преобразование значения прогресса в вещественное с защитой от "N/A"."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def format_timecode(seconds: float) -> str:
    """Форматирование длительности в вид ЧЧ:ММ:СС для интерфейса."""
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


@dataclass(frozen=True)
class ProgressReport:
    """Снимок состояния кодирования, полученный из потока "-progress"."""

    frame: int = 0
    fps: float = 0.0
    bitrate_kbits: float = 0.0
    total_size: int = 0
    out_time_us: int = 0
    dup_frames: int = 0
    drop_frames: int = 0
    speed: float = 0.0
    # Признак завершающего отчёта, отправляемого FFmpeg перед выходом.
    is_final: bool = False

    @property
    def seconds(self) -> float:
        """Длительность обработанного материала в секундах."""
        return self.out_time_us / 1_000_000

    @property
    def timecode(self) -> str:
        """Длительность в виде ЧЧ:ММ:СС."""
        return format_timecode(self.seconds)

    @property
    def size_mb(self) -> float:
        """Текущий размер результата в мегабайтах."""
        return self.total_size / (1024 * 1024)

    @property
    def is_dropping_frames(self) -> bool:
        """
        Признак нехватки производительности.

        Рост числа отброшенных кадров означает, что захват не успевает за
        заданной частотой: интерфейсу следует предупредить пользователя.
        """
        return self.drop_frames > 0


class ProgressParser:
    """
    Накопительный разборщик потока "-progress pipe:1".

    FFmpeg выводит блок строк вида "ключ=значение", завершая его строкой
    "progress=continue" или "progress=end". Данные приходят произвольными
    порциями, поэтому разборщик хранит незавершённый остаток между вызовами.
    """

    def __init__(self) -> None:
        # Остаток строки, не завершённой переводом каретки.
        self._tail = ""
        # Накопленные пары текущего блока.
        self._values: dict[str, str] = {}

    def feed(self, chunk: str) -> list[ProgressReport]:
        """Передача очередной порции вывода и получение готовых отчётов."""
        reports: list[ProgressReport] = []
        # Незавершённая часть предыдущей порции склеивается с новой.
        data = self._tail + chunk
        lines = data.split("\n")
        # Последний элемент остаётся в буфере: перевод строки ещё не получен.
        self._tail = lines.pop()

        for line in lines:
            key, separator, value = line.strip().partition("=")
            if not separator:
                continue
            if key == "progress":
                # Маркер конца блока: накопленные значения превращаются в отчёт.
                reports.append(self._build(final=value == "end"))
                self._values.clear()
            else:
                self._values[key] = value
        return reports

    def reset(self) -> None:
        """Сброс состояния перед запуском следующего шага задания."""
        self._tail = ""
        self._values.clear()

    def _build(self, final: bool) -> ProgressReport:
        """Сборка отчёта из накопленных пар ключ-значение."""
        values = self._values
        # Поле out_time_us в части версий называется out_time_ms, но при этом
        # содержит микросекунды; проверяются оба варианта.
        out_time = values.get("out_time_us") or values.get("out_time_ms") or ""
        # Битрейт приходит в виде "1234.5kbits/s" либо "N/A".
        bitrate = values.get("bitrate", "").replace("kbits/s", "").strip()
        # Скорость приходит в виде "1.02x".
        speed = values.get("speed", "").replace("x", "").strip()

        return ProgressReport(
            frame=_to_int(values.get("frame", "")),
            fps=_to_float(values.get("fps", "")),
            bitrate_kbits=_to_float(bitrate),
            total_size=_to_int(values.get("total_size", "")),
            out_time_us=_to_int(out_time),
            dup_frames=_to_int(values.get("dup_frames", "")),
            drop_frames=_to_int(values.get("drop_frames", "")),
            speed=_to_float(speed),
            is_final=final,
        )


# ===========================================================================
# Разбор журнала ошибок
# ===========================================================================

# Признаки строк, которые следует показать пользователю при сбое.
_ERROR_MARKERS: tuple[str, ...] = (
    "error",
    "invalid",
    "no such file",
    "permission denied",
    "unknown encoder",
    "unrecognized option",
    "cannot open",
    "could not",
    "failed",
    "not found",
)


def looks_like_error(line: str) -> bool:
    """Проверка строки журнала на принадлежность к сообщениям об ошибке."""
    lowered = line.lower()
    return any(marker in lowered for marker in _ERROR_MARKERS)


def summarize_log(lines: Iterable[str], limit: int = 5) -> str:
    """
    Выделение из журнала кратких сведений о причине сбоя.

    Показ пользователю полного вывода FFmpeg бесполезен, поэтому
    отбираются только строки с признаками ошибки; при их отсутствии
    возвращаются последние строки журнала как есть.
    """
    collected = [line.strip() for line in lines if line.strip()]
    errors = [line for line in collected if looks_like_error(line)]
    selected = errors[-limit:] if errors else collected[-limit:]
    return "\n".join(selected)
