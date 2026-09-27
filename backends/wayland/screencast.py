"""
Видеопоток экрана через портал ScreenCast и PipeWire.

В Wayland приложение не может читать содержимое экрана напрямую: поток
кадров выдаёт композитор через PipeWire после согласия пользователя в
диалоге портала. Цепочка записи устроена так:

    портал ScreenCast -> PipeWire -> GStreamer (pipewiresrc)
        -> именованный канал (FIFO) -> FFmpeg (-f rawvideo)

GStreamer только принимает кадры: вырезает записываемую область, приводит
частоту к постоянной и отдаёт несжатые кадры в канал. Всё кодирование
выполняет FFmpeg. Канал выбран вместо стандартного ввода FFmpeg, потому что
стандартный ввод занят командой остановки "q", обязательной для корректной
финализации контейнера.

Разрешение пользователя запоминается порталом по метке восстановления,
которую приложение хранит отдельно для каждого монитора. Повторная запись
того же монитора начинается без диалога.

Функции модуля, обращающиеся к порталу, блокирующие и вызываются только
из фоновых потоков.
"""

from __future__ import annotations

from core.i18n import tr

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, cast

from PySide6.QtCore import QObject, QRect, Signal
from PySide6.QtGui import QGuiApplication, QScreen

from backends.base import VideoSource
from backends.wayland import gstreamer
from backends.wayland.gstreamer import GStreamer
from capture.screen import screen_for_rect
from core import portal
from core.config import config_dir
from core.workers import run_async
from encoder.process import FrameFeed
from encoder.profiles import VideoInput

# Размер очереди пакетов видеовхода: при просадке диска кадры не теряются.
VIDEO_QUEUE_SIZE = "1024"

# Типы источников согласно спецификации портала: 1 - монитор, 2 - окно.
SOURCE_TYPE_MONITOR = 1

# Режимы указателя мыши: 1 - скрыт, 2 - встроен в кадры, 4 - метаданные.
CURSOR_MODE_HIDDEN = 1
CURSOR_MODE_EMBEDDED = 2

# Режим сохранения разрешения: 2 - до явного отзыва пользователем.
PERSIST_UNTIL_REVOKED = 2

# Формат несжатых кадров в канале. Четыре байта на точку совпадают с
# внутренним форматом большинства композиторов, поэтому преобразование
# цвета в GStreamer обычно не требуется.
GSTREAMER_FORMAT = "BGRx"
FFMPEG_PIXEL_FORMAT = "bgr0"


class ScreenCastError(RuntimeError):
    """Видеопоток экрана получить не удалось."""


@dataclass(frozen=True)
class CastStream:
    """Сведения о потоке, выданном порталом."""

    # Путь сеанса портала: сеанс живёт до конца записи.
    session_path: str
    # Идентификатор узла потока в графе PipeWire.
    node_id: int
    # Размер источника в координатах композитора, если портал его сообщил.
    width: int = 0
    height: int = 0
    # Метка для восстановления разрешения при следующей записи.
    restore_token: str = ""


@dataclass(frozen=True)
class CropPlan:
    """
    Геометрия вырезаемой области в точках потока.

    Поток монитора передаётся в физических точках, поэтому логические
    координаты Qt пересчитываются по отношению размера потока к
    логическому размеру монитора.
    """

    # Полный размер кадра потока.
    source_width: int
    source_height: int
    # Вырезаемая область в точках потока.
    left: int
    top: int
    width: int
    height: int

    @property
    def right(self) -> int:
        """Отступ области от правого края кадра."""
        return max(0, self.source_width - self.left - self.width)

    @property
    def bottom(self) -> int:
        """Отступ области от нижнего края кадра."""
        return max(0, self.source_height - self.top - self.height)


def plan_crop(region: QRect, monitor: QRect, source_width: int, source_height: int) -> CropPlan:
    """
    Расчёт вырезаемой области потока монитора.

    Масштаб определяется отношением размера потока к логическому размеру
    монитора, а не коэффициентом Qt: при дробном масштабе экрана Qt
    округляет коэффициент (1,25 превращается в 2), и расчёт по нему
    выводил бы область за края кадра.

    Область ограничивается границами монитора: портал выдаёт поток одного
    монитора, и захватить часть соседнего невозможно. Стороны приводятся к
    чётным значениям - кодеки с субдискретизацией 4:2:0 нечётный кадр не
    принимают, а обрезка на входе дешевле дополнения фильтром.
    """
    bounded = region.intersected(monitor)
    if bounded.isEmpty():
        bounded = QRect(monitor)
    scale_x = source_width / max(1, monitor.width())
    scale_y = source_height / max(1, monitor.height())
    left = round((bounded.x() - monitor.x()) * scale_x)
    top = round((bounded.y() - monitor.y()) * scale_y)
    width = min(round(bounded.width() * scale_x), source_width - left)
    height = min(round(bounded.height() * scale_y), source_height - top)
    width = max(2, width - width % 2)
    height = max(2, height - height % 2)
    return CropPlan(source_width, source_height, left, top, width, height)


# ===========================================================================
# Метки восстановления разрешений
# ===========================================================================


def _tokens_file() -> Path:
    """Файл с метками восстановления разрешений на захват экрана."""
    return config_dir() / "screencast-tokens.json"


def load_restore_token(monitor_key: str) -> str:
    """Сохранённая метка разрешения для монитора либо пустая строка."""
    try:
        data = json.loads(_tokens_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    value = data.get(monitor_key, "") if isinstance(data, dict) else ""
    return value if isinstance(value, str) else ""


def store_restore_token(monitor_key: str, token: str) -> None:
    """
    Сохранение метки разрешения для монитора.

    Портал выдаёт новую метку после каждого сеанса, прежняя при этом
    становится недействительной, поэтому запись обновляется всякий раз.
    """
    path = _tokens_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    if token:
        data[monitor_key] = token
    else:
        data.pop(monitor_key, None)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        # Без сохранённой метки следующая запись просто покажет диалог.
        pass


# ===========================================================================
# Сеанс портала
# ===========================================================================


def is_screencast_available() -> bool:
    """
    Признак поддержки захвата видеопотока установленным порталом.

    Интерфейс предоставляют бэкенды -kde, -gnome, -wlr и -hyprland;
    сборки -gtk и -xapp его не содержат.
    """
    return portal.interface_version(portal.SCREENCAST_INTERFACE) is not None


def open_session(monitor_key: str, show_cursor: bool) -> CastStream:
    """Открытие сеанса захвата одного монитора."""
    return open_streams(monitor_key, show_cursor, multiple=False)[0]


def open_streams(
    permission_key: str, show_cursor: bool, multiple: bool, persist: bool = True
) -> list[CastStream]:
    """
    Открытие сеанса захвата с одним или несколькими мониторами.

    Последовательность обмена определена спецификацией портала:
    CreateSession, SelectSources, Start. Диалог выбора мониторов
    появляется только при отсутствии действующего разрешения, которое
    хранится под обозначением permission_key. Все потоки относятся к
    одному сеансу и закрываются вместе с ним. Без признака persist
    разрешение не запоминается и диалог показывается каждый раз.
    """
    version = portal.interface_version(portal.SCREENCAST_INTERFACE)
    if version is None:
        from backends.wayland.screen import missing_portal_message

        raise ScreenCastError(missing_portal_message())

    session_token = portal.new_token()
    created = portal.request(
        portal.SCREENCAST_INTERFACE,
        "CreateSession",
        "a{sv}",
        lambda token: (
            {
                "handle_token": ("s", token),
                "session_handle_token": ("s", session_token),
            },
        ),
    )
    session_path = str(created.get("session_handle", ""))
    if not session_path:
        raise ScreenCastError(tr("Портал не вернул идентификатор сеанса"))

    try:
        return _select_and_start(
            session_path, permission_key, show_cursor, multiple, version if persist else 0
        )
    except BaseException:
        # Незавершённый сеанс закрывается, иначе значок демонстрации экрана
        # останется в панели композитора до выхода из программы.
        portal.close_session(session_path)
        raise


def _cursor_mode(show_cursor: bool) -> int:
    """
    Режим указателя, поддерживаемый порталом.

    Встраивание указателя в кадры поддерживают все известные реализации,
    но проверка делается по объявленному набору режимов: при его
    отсутствии поток отдаётся с поведением портала по умолчанию.
    """
    wanted = CURSOR_MODE_EMBEDDED if show_cursor else CURSOR_MODE_HIDDEN
    available = portal.interface_property(portal.SCREENCAST_INTERFACE, "AvailableCursorModes")
    if isinstance(available, int) and not available & wanted:
        return 0
    return wanted


def _select_and_start(
    session_path: str, monitor_key: str, show_cursor: bool, multiple: bool, version: int
) -> list[CastStream]:
    """Выбор источников и запуск сеанса."""
    options: dict[str, Any] = {
        "types": ("u", SOURCE_TYPE_MONITOR),
        "multiple": ("b", multiple),
    }
    cursor_mode = _cursor_mode(show_cursor)
    if cursor_mode:
        options["cursor_mode"] = ("u", cursor_mode)
    if version >= 4:
        # Сохранение разрешения появилось в четвёртой версии интерфейса.
        options["persist_mode"] = ("u", PERSIST_UNTIL_REVOKED)
        restore_token = load_restore_token(monitor_key)
        if restore_token:
            options["restore_token"] = ("s", restore_token)

    portal.request(
        portal.SCREENCAST_INTERFACE,
        "SelectSources",
        "oa{sv}",
        lambda token: (session_path, {**options, "handle_token": ("s", token)}),
    )
    started = portal.request(
        portal.SCREENCAST_INTERFACE,
        "Start",
        "osa{sv}",
        lambda token: (session_path, "", {"handle_token": ("s", token)}),
    )

    streams = started.get("streams") or []
    if not streams:
        raise ScreenCastError(tr("Портал не вернул ни одного потока"))
    token = str(started.get("restore_token", ""))
    result: list[CastStream] = []
    for node_id, properties in streams:
        size = properties.get("size") if isinstance(properties, dict) else None
        width, height = (int(size[0]), int(size[1])) if isinstance(size, tuple) else (0, 0)
        result.append(CastStream(session_path, int(node_id), width, height, token))
    return result


def open_remote(session_path: str) -> int:
    """
    Новое соединение с PipeWire для чтения потока сеанса.

    Соединение нельзя передать второму процессу после завершения первого:
    сервер PipeWire связывает с ним состояние клиента. Поэтому каждый
    фрагмент записи получает собственное соединение, а сеанс портала -
    и с ним разрешение пользователя - остаётся прежним.

    Возвращаемый дескриптор наследуется дочерним процессом.
    """
    (descriptor,) = portal.call(
        portal.PORTAL_PATH,
        portal.SCREENCAST_INTERFACE,
        "OpenPipeWireRemote",
        "oa{sv}",
        (session_path, {}),
    )
    raw = int(descriptor.to_raw_fd())
    # Дескриптор, полученный через шину, закрывается при запуске дочерних
    # процессов. Признак снимается: копия нужна процессу GStreamer.
    os.set_inheritable(raw, True)
    return raw


# ===========================================================================
# Канал и команда GStreamer
# ===========================================================================


def create_fifo() -> Path:
    """
    Создание именованного канала для передачи кадров.

    Канал размещается в личном каталоге сеанса пользователя: он доступен
    только владельцу и очищается системой при выходе.
    """
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    directory = Path(tempfile.mkdtemp(prefix="linscreen-", dir=base))
    fifo = directory / "frames.fifo"
    os.mkfifo(fifo, 0o600)
    return fifo


def remove_fifo(fifo: Path) -> None:
    """Удаление канала вместе с его каталогом."""
    shutil.rmtree(fifo.parent, ignore_errors=True)


def build_gstreamer_command(
    descriptor: int,
    node_id: int,
    crop: CropPlan,
    fps: int,
    fifo: Path,
    gst_launch: str = "gst-launch-1.0",
) -> list[str]:
    """
    Команда конвейера GStreamer, доставляющего кадры в канал.

    Кадр потока сначала приводится к расчётному полному размеру и лишь
    затем вырезается. Если фактический размер потока совпадает с
    расчётным (обычный случай), приведение ничего не делает. Если нет -
    например, реализация портала сообщила размер в других единицах, - кадр
    масштабируется целиком, и вырезанная область остаётся на своём месте.
    Без этого вырезание по неверному размеру выходило бы за края кадра, и
    согласование формата срывалось бы.
    """
    # Повтор последнего кадра при неподвижном экране: композитор присылает
    # кадры только при изменениях, а FFmpeg ждёт постоянной частоты.
    keepalive = max(1, 1000 // max(1, fps))
    return [
        gst_launch,
        "-q",
        "pipewiresrc",
        f"fd={descriptor}",
        f"path={node_id}",
        "do-timestamp=true",
        f"keepalive-time={keepalive}",
        # Копирование кадра из видеопамяти в обычную обязательно: элементы
        # обработки ниже не умеют работать с буферами DMA-BUF.
        "always-copy=true",
        "!",
        "videoconvert",
        "!",
        "videoscale",
        "!",
        (
            f"video/x-raw,format={GSTREAMER_FORMAT},"
            f"width={crop.source_width},height={crop.source_height}"
        ),
        "!",
        "videocrop",
        f"left={crop.left}",
        f"top={crop.top}",
        f"right={crop.right}",
        f"bottom={crop.bottom}",
        "!",
        # Размер кадра в канале обязан в точности совпадать с объявленным
        # FFmpeg, иначе изображение рассыплется; фильтр это закрепляет.
        f"video/x-raw,format={GSTREAMER_FORMAT},width={crop.width},height={crop.height}",
        "!",
        "videorate",
        "!",
        f"video/x-raw,framerate={fps}/1",
        "!",
        "filesink",
        f"location={fifo}",
        # Запись в канал без привязки к часам конвейера: темп задаёт
        # элемент videorate, а задержки канала не должны копиться.
        "sync=false",
    ]


def build_rawvideo_input(fifo: Path, crop: CropPlan, fps: int, queue_size: str) -> list[str]:
    """Аргументы входа FFmpeg, читающего кадры из канала."""
    return [
        "-f",
        "rawvideo",
        "-pixel_format",
        FFMPEG_PIXEL_FORMAT,
        "-video_size",
        f"{crop.width}x{crop.height}",
        "-framerate",
        str(fps),
        "-thread_queue_size",
        queue_size,
        "-i",
        str(fifo),
    ]


# ===========================================================================
# Источник кадров для контроллера записи
# ===========================================================================


class ScreenCastSource(VideoSource):
    """
    Источник кадров экрана на время одной записи.

    Объект открывает сеанс портала, создаёт канал передачи кадров и для
    каждого фрагмента записи готовит процесс GStreamer с собственным
    соединением PipeWire. Сеанс закрывается по окончании записи.
    """

    # Источник готов: передаётся описание видеовхода FFmpeg.
    ready = Signal(object)
    # Пользователь отказался в диалоге портала.
    cancelled = Signal()
    # Сеанс открыть не удалось: передаётся причина.
    failed = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._stream: CastStream | None = None
        self._crop: CropPlan | None = None
        self._fifo: Path | None = None
        self._fps = 30
        self._region = QRect()
        self._recorded_rect = QRect()
        self._closed = False
        self._screen: QScreen | None = None

    @property
    def recorded_rect(self) -> QRect:
        """Фактически записываемая область в логических координатах."""
        return QRect(self._recorded_rect)

    def open(self, region: QRect, fps: int, show_cursor: bool) -> None:
        """Открытие сеанса захвата монитора, на котором лежит область."""
        from backends.wayland.snapshot import open_for_recording

        self._fps = fps
        self._region = QRect(region)
        screen = screen_for_rect(region)
        if screen is None:
            self.failed.emit(tr("Графические экраны не обнаружены"))
            return
        self._screen = screen
        # Геометрия мониторов собирается здесь: обращаться к экранам Qt из
        # фонового потока нельзя.
        screens = [item.geometry() for item in QGuiApplication.screens()]
        desktop = QRect()
        for item in screens:
            desktop = desktop.united(item)
        run_async(
            self,
            open_for_recording,
            self._on_opened,
            self.failed.emit,
            QGuiApplication.screens().index(screen),
            screens,
            desktop,
            show_cursor,
        )

    def _on_opened(self, result: object) -> None:
        """Подготовка видеовхода после открытия сеанса."""
        if result is None:
            self.cancelled.emit()
            return
        stream, area = cast(tuple[CastStream, "QRect | None"], result)
        if self._closed:
            # Запись отменили, пока портал ждал решения пользователя.
            run_async(self, portal.close_session, lambda _result: None, None, stream.session_path)
            return
        self._stream = stream

        screen = self._screen or QGuiApplication.primaryScreen()
        if screen is None:
            self.close()
            self.failed.emit(tr("Графические экраны не обнаружены"))
            return
        # Логическая геометрия того, что показывает поток: монитор либо весь
        # рабочий стол, если пользователь выбрал его целиком.
        monitor = QRect(area) if area is not None else screen.geometry()
        region = self._region if self._region.intersects(monitor) else QRect(monitor)
        # Размер потока берётся из ответа портала; коэффициент Qt служит
        # лишь запасным вариантом, так как при дробном масштабе он неточен.
        if stream.width > 0 and stream.height > 0:
            source_width, source_height = stream.width, stream.height
        else:
            ratio = screen.devicePixelRatio()
            source_width = round(monitor.width() * ratio)
            source_height = round(monitor.height() * ratio)
        self._crop = plan_crop(region, monitor, source_width, source_height)
        self._recorded_rect = region.intersected(monitor)
        try:
            # Канал создаётся мгновенно: это единственный системный вызов.
            self._fifo = create_fifo()
        except OSError as error:
            self.close()
            self.failed.emit(tr("Не удалось создать канал передачи кадров: {0}").format(error))
            return

        crop = self._crop
        self.ready.emit(
            VideoInput(
                args=build_rawvideo_input(self._fifo, crop, self._fps, VIDEO_QUEUE_SIZE),
                fps=self._fps,
                width=crop.width,
                height=crop.height,
                # Стороны уже приведены к чётным при расчёте вырезания.
                needs_even_padding=False,
            )
        )

    def feeder(
        self,
        on_ready: Callable[[FrameFeed], None],
        on_error: Callable[[str], None],
    ) -> None:
        """Подготовка поставщика кадров для очередного фрагмента записи."""
        stream, crop, fifo = self._stream, self._crop, self._fifo
        if stream is None or crop is None or fifo is None or self._closed:
            on_error(tr("Сеанс захвата экрана закрыт"))
            return
        fps = self._fps

        def prepare(session_path: str) -> tuple[int, GStreamer]:
            """Новое соединение с потоком и выбор GStreamer (в фоне)."""
            return open_remote(session_path), gstreamer.select()

        def deliver(result: object) -> None:
            """Сборка команды поставщика по полученному соединению."""
            descriptor, kit = cast(tuple[int, GStreamer], result)
            on_ready(
                FrameFeed(
                    args=build_gstreamer_command(
                        descriptor, stream.node_id, crop, fps, fifo, kit.launch
                    ),
                    descriptor=descriptor,
                    fifo=fifo,
                    environment=kit.environment,
                )
            )

        run_async(self, prepare, deliver, on_error, stream.session_path)

    def close(self) -> None:
        """Закрытие сеанса портала и удаление канала."""
        self._closed = True
        stream, self._stream = self._stream, None
        if stream is not None:
            # Закрытие сеанса убирает значок демонстрации экрана из панели.
            run_async(self, portal.close_session, lambda _result: None, None, stream.session_path)
        fifo, self._fifo = self._fifo, None
        if fifo is not None:
            remove_fifo(fifo)
