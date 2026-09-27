"""
Выбор GStreamer, доставляющего кадры экрана из PipeWire.

Используется один из двух вариантов:
    * системный GStreamer, если в нём есть все нужные элементы, - лучший
      выбор: его модуль PipeWire собран под ту же версию PipeWire, что
      работает в системе;
    * вложенный в образ AppImage GStreamer вместе с клиентской частью
      PipeWire - когда системный отсутствует или неполон. Протокол PipeWire
      совместим между версиями, поэтому вложенный клиент работает и с
      другой версией сервера.

Вложенный комплект лежит в каталоге gstreamer рядом с приложением; его
собирает build.sh. Выбор выполняется один раз и запоминается.

Функции модуля запускают внешние утилиты и вызываются из фонового потока
либо из режима --check.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

from core.identity import APPLICATION_SLUG
from core.runtime import bundle_dir, child_environment

# Элементы, без которых запись и снимки без указателя невозможны.
REQUIRED_ELEMENTS = (
    "pipewiresrc",
    "videoconvert",
    "videocrop",
    "videoscale",
    "videorate",
    "pngenc",
    "filesink",
)

# Предельное время проверки одного элемента. Первый запуск после входа в
# сеанс или обновления пакетов пересобирает реестр модулей GStreamer и
# может занять десятки секунд.
INSPECT_TIMEOUT_SEC = 90.0


@dataclass(frozen=True)
class GStreamer:
    """Выбранный комплект GStreamer."""

    # Путь к утилите запуска конвейеров.
    launch: str
    # Окружение процессов конвейера.
    environment: dict[str, str] = field(default_factory=dict)
    # Признак вложенного комплекта.
    bundled: bool = False
    # Отсутствующие элементы; пустой перечень означает готовность.
    missing: tuple[str, ...] = ()

    @property
    def is_ready(self) -> bool:
        """Признак наличия всех нужных элементов."""
        return not self.missing


_selected: GStreamer | None = None
_lock = threading.Lock()


def bundled_root() -> Path | None:
    """Каталог вложенного GStreamer либо пустое значение."""
    base = bundle_dir()
    if base is None:
        return None
    root = base / "gstreamer"
    return root if (root / "bin" / "gst-launch-1.0").is_file() else None


def bundled_environment(root: Path) -> dict[str, str]:
    """
    Окружение вложенного GStreamer.

    Пути поиска библиотек, модулей GStreamer и модулей PipeWire указывают
    внутрь комплекта, чтобы системные версии не подмешивались. Реестр
    модулей GStreamer хранится отдельно от системного: он описывает другой
    набор файлов.
    """
    environment = child_environment()
    library = str(root / "lib")
    environment["LD_LIBRARY_PATH"] = library
    environment["GST_PLUGIN_SYSTEM_PATH_1_0"] = str(root / "plugins")
    environment["GST_PLUGIN_PATH_1_0"] = ""
    environment["GST_PLUGIN_SCANNER_1_0"] = str(root / "bin" / "gst-plugin-scanner")
    cache = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    environment["GST_REGISTRY_1_0"] = str(Path(cache) / APPLICATION_SLUG / "gst-registry.bin")
    environment["PIPEWIRE_MODULE_DIR"] = str(root / "pipewire" / "modules")
    environment["SPA_PLUGIN_DIR"] = str(root / "pipewire" / "spa")
    environment["PIPEWIRE_CONFIG_DIR"] = str(root / "pipewire" / "config")
    return environment


def missing_elements(launch: str, environment: dict[str, str]) -> tuple[str, ...]:
    """Перечень элементов, отсутствующих в комплекте GStreamer."""
    inspect = str(Path(launch).with_name("gst-inspect-1.0"))
    if not Path(inspect).is_file():
        found = shutil.which("gst-inspect-1.0", path=environment.get("PATH"))
        if found is None:
            return REQUIRED_ELEMENTS
        inspect = found
    missing: list[str] = []
    for element in REQUIRED_ELEMENTS:
        try:
            completed = subprocess.run(
                [inspect, "--exists", element],
                env=environment,
                capture_output=True,
                timeout=INSPECT_TIMEOUT_SEC,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            missing.append(element)
            continue
        if completed.returncode != 0:
            missing.append(element)
    return tuple(missing)


# Переменная окружения, принудительно включающая вложенный комплект.
# Предназначена для проверки образа на машине с полным системным GStreamer.
FORCE_BUNDLED_VARIABLE = "LINSCREEN_BUNDLED_GSTREAMER"


def _probe() -> GStreamer:
    """Проверка системного и вложенного комплектов."""
    system: GStreamer | None = None
    environment = child_environment()
    launch = shutil.which("gst-launch-1.0", path=environment.get("PATH"))
    if os.environ.get(FORCE_BUNDLED_VARIABLE) == "1":
        launch = None
    if launch is not None:
        system = GStreamer(launch, environment, False, missing_elements(launch, environment))
        if system.is_ready:
            return system

    root = bundled_root()
    if root is not None:
        environment = bundled_environment(root)
        launch = str(root / "bin" / "gst-launch-1.0")
        bundled = GStreamer(launch, environment, True, missing_elements(launch, environment))
        if bundled.is_ready:
            return bundled

    # Ни один комплект не готов: сообщается состояние системного, так как
    # недостающее проще всего доустановить в систему.
    if system is not None:
        return system
    return GStreamer("gst-launch-1.0", child_environment(), False, REQUIRED_ELEMENTS)


def select() -> GStreamer:
    """
    Выбранный комплект GStreamer.

    Готовый комплект запоминается; неудачная проверка повторяется при
    следующем обращении - недостающее могли доустановить.
    """
    global _selected
    with _lock:
        if _selected is None or not _selected.is_ready:
            _selected = _probe()
        return _selected
