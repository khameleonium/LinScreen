"""
Перечисление звуковых источников и сборка аудиовходов FFmpeg.

Сведения об устройствах берутся у сервера PulseAudio либо у совместимого
слоя pipewire-pulse через утилиту pactl: оба сервера отвечают на один и тот
же протокол, поэтому отдельная ветка для PipeWire не требуется.

Модуль не зависит от Qt. Вызов list_audio_devices() блокирующий, так как
запускает внешний процесс, и выполняется через core.workers.run_async().
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Sequence

from core.runtime import child_environment
from encoder.profiles import AudioInput, AudioMode, AudioRole

# Время ожидания ответа звукового сервера. Превышение означает, что сервер
# не запущен либо завис: приложение продолжит работу без звука.
PACTL_TIMEOUT_SEC = 3.0

# Размер очереди пакетов входа. Значение по умолчанию мало для параллельной
# записи видео и двух звуковых потоков и приводит к сообщениям о переполнении.
AUDIO_QUEUE_SIZE = "1024"

# Имя источника, принимаемого звуковым сервером без предварительного
# перечисления устройств. Применяется, когда утилита pactl недоступна.
DEFAULT_SOURCE_NAME = "default"


@dataclass(frozen=True)
class AudioDevice:
    """Звуковой источник, пригодный для записи."""

    # Системное имя источника, передаваемое FFmpeg.
    name: str
    # Название для показа пользователю.
    description: str
    # Признак монитора вывода, то есть источника системного звука.
    is_monitor: bool = False
    # Признак источника, выбранного в системе по умолчанию.
    is_default: bool = False

    @property
    def title(self) -> str:
        """Подпись для выпадающего списка настроек."""
        suffix = " (по умолчанию)" if self.is_default else ""
        return f"{self.description}{suffix}"

    @property
    def role(self) -> AudioRole:
        """Назначение устройства: системный звук либо микрофон."""
        return AudioRole.SYSTEM if self.is_monitor else AudioRole.MICROPHONE


def _run_pactl(args: Sequence[str]) -> str:
    """Запуск утилиты pactl с приведением локали к предсказуемому виду."""
    # Пути поиска библиотек приводятся к системным: утилита не должна
    # загружать копии, вложенные в приложение.
    environment = child_environment()
    environment["LC_ALL"] = "C"
    completed = subprocess.run(
        ["pactl", *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=PACTL_TIMEOUT_SEC,
        env=environment,
        shell=False,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "Звуковой сервер не отвечает")
    return completed.stdout


def _default_source_name() -> str:
    """Имя источника, выбранного в системе по умолчанию."""
    try:
        info = _run_pactl(["info"])
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return ""
    for line in info.splitlines():
        if line.startswith("Default Source:"):
            return line.partition(":")[2].strip()
    return ""


def _parse_json_sources(payload: str, default_name: str) -> list[AudioDevice]:
    """Разбор ответа pactl в формате JSON."""
    devices: list[AudioDevice] = []
    for entry in json.loads(payload):
        name = entry.get("name", "")
        if not name:
            continue
        # Источник считается монитором, если он связан с устройством вывода.
        is_monitor = bool(entry.get("monitor_of_sink")) or name.endswith(".monitor")
        properties = entry.get("properties") or {}
        # Часть сборок pactl не умеет выводить не-ASCII строки в формате
        # JSON и подставляет вместо описания заглушку "(null)". В этом
        # случае название собирается из свойств устройства.
        description = entry.get("description") or ""
        if not description or description == "(null)":
            description = (
                properties.get("node.description")
                or properties.get("device.description")
                or name
            )
        devices.append(
            AudioDevice(
                name=name,
                description=description,
                is_monitor=is_monitor,
                is_default=name == default_name,
            )
        )
    return devices


def _parse_verbose_sources(payload: str, default_name: str) -> list[AudioDevice]:
    """
    Разбор подробного текстового вывода pactl.

    Основной способ получения сведений: ключи полей остаются английскими
    при локали C, а значения приходят в исходной кодировке UTF-8, поэтому
    локализованные названия устройств сохраняются полностью.
    """
    devices: list[AudioDevice] = []
    name = ""
    description = ""
    monitor_of = ""

    def flush() -> None:
        """Сохранение накопленного блока сведений об источнике."""
        if not name:
            return
        devices.append(
            AudioDevice(
                name=name,
                description=description or name,
                # Признак монитора определяется по связанному устройству
                # вывода, а при его отсутствии - по суффиксу имени.
                is_monitor=bool(monitor_of) or name.endswith(".monitor"),
                is_default=name == default_name,
            )
        )

    for line in payload.splitlines():
        stripped = line.strip()
        if line.startswith("Source #"):
            # Начало нового блока: предыдущий считается завершённым.
            flush()
            name, description, monitor_of = "", "", ""
        elif stripped.startswith("Name:"):
            name = stripped.partition(":")[2].strip()
        elif stripped.startswith("Description:"):
            description = stripped.partition(":")[2].strip()
        elif stripped.startswith("Monitor of Sink:"):
            value = stripped.partition(":")[2].strip()
            # Значение "n/a" означает, что источник не является монитором.
            monitor_of = "" if value in ("n/a", "") else value
    flush()
    return devices


def _parse_short_sources(payload: str, default_name: str) -> list[AudioDevice]:
    """
    Разбор краткого табличного ответа pactl.

    Запасной путь для сборок без поддержки формата JSON: столбцы разделены
    табуляцией, второй столбец содержит имя источника.
    """
    devices: list[AudioDevice] = []
    for line in payload.splitlines():
        columns = line.split("\t")
        if len(columns) < 2:
            continue
        name = columns[1].strip()
        if not name:
            continue
        devices.append(
            AudioDevice(
                name=name,
                # Понятного описания в кратком выводе нет, используется имя.
                description=name,
                is_monitor=name.endswith(".monitor"),
                is_default=name == default_name,
            )
        )
    return devices


def list_audio_devices() -> list[AudioDevice]:
    """
    Перечисление доступных звуковых источников.

    Опрос ведётся тремя способами по убыванию информативности:
    подробный текстовый вывод, формат JSON и краткая таблица.
    """
    default_name = _default_source_name()

    # Основной путь: подробный текстовый вывод с названиями устройств.
    try:
        devices = _parse_verbose_sources(_run_pactl(["list", "sources"]), default_name)
        if devices:
            return devices
    except OSError:
        # Утилита pactl отсутствует: запись со звуком невозможна.
        return []
    except (RuntimeError, subprocess.SubprocessError):
        pass

    # Запасной путь: машиночитаемый формат более новых сборок.
    try:
        return _parse_json_sources(_run_pactl(["-f", "json", "list", "sources"]), default_name)
    except (json.JSONDecodeError, OSError, RuntimeError, subprocess.SubprocessError):
        pass

    # Последний путь: краткая таблица без названий устройств.
    try:
        devices = _parse_short_sources(_run_pactl(["list", "sources", "short"]), default_name)
        if devices:
            return devices
    except (OSError, RuntimeError, subprocess.SubprocessError):
        pass
    return fallback_devices()


def fallback_devices() -> list[AudioDevice]:
    """
    Запасной перечень источников при недоступной утилите pactl.

    Звуковой сервер принимает условное имя источника по умолчанию, поэтому
    запись с микрофона остаётся возможной даже без перечисления устройств.
    Монитор системного звука таким способом получить нельзя: его имя
    зависит от устройства вывода.
    """
    return [
        AudioDevice(
            name=DEFAULT_SOURCE_NAME,
            description="Источник по умолчанию",
            is_monitor=False,
            is_default=True,
        )
    ]


def find_device(devices: Sequence[AudioDevice], name: str) -> AudioDevice | None:
    """Поиск устройства по системному имени."""
    for device in devices:
        if device.name == name:
            return device
    return None


def default_system_source(devices: Sequence[AudioDevice]) -> AudioDevice | None:
    """
    Источник системного звука по умолчанию.

    Системный звук записывается с монитора устройства вывода; при наличии
    нескольких мониторов предпочтение отдаётся связанному с активным выводом.
    """
    monitors = [device for device in devices if device.is_monitor]
    if not monitors:
        return None
    for device in monitors:
        if device.is_default:
            return device
    return monitors[0]


def default_microphone(devices: Sequence[AudioDevice]) -> AudioDevice | None:
    """Микрофон по умолчанию."""
    inputs = [device for device in devices if not device.is_monitor]
    if not inputs:
        return None
    for device in inputs:
        if device.is_default:
            return device
    return inputs[0]


def build_audio_input(device: AudioDevice) -> AudioInput:
    """Сборка аргументов входа FFmpeg для указанного устройства."""
    return AudioInput(
        args=[
            "-f",
            "pulse",
            # Увеличенная очередь пакетов предотвращает потерю звука при
            # одновременной записи нескольких потоков.
            "-thread_queue_size",
            AUDIO_QUEUE_SIZE,
            "-i",
            device.name,
        ],
        role=device.role,
        title=device.description,
    )


def resolve_audio_inputs(
    mode: AudioMode,
    devices: Sequence[AudioDevice],
    system_name: str = "",
    microphone_name: str = "",
) -> list[AudioInput]:
    """
    Подбор входов под выбранную схему работы со звуком.

    Имена устройств берутся из настроек; при пустом значении или отсутствии
    устройства применяется выбранное в системе по умолчанию.
    """
    if mode is AudioMode.NONE or not devices:
        return []

    system = find_device(devices, system_name) or default_system_source(devices)
    microphone = find_device(devices, microphone_name) or default_microphone(devices)

    if mode is AudioMode.SYSTEM:
        return [build_audio_input(system)] if system else []
    if mode is AudioMode.MICROPHONE:
        return [build_audio_input(microphone)] if microphone else []

    # Режимы MIX и SEPARATE требуют обоих источников; при нехватке одного
    # из них запись ведётся с оставшимся.
    selected = [device for device in (system, microphone) if device is not None]
    return [build_audio_input(device) for device in selected]
