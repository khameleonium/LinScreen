"""
Снимок рабочего стола без указателя мыши.

Портал Screenshot в KDE Plasma встраивает указатель мыши в снимок, и
отключить это средствами портала нельзя. Видеопоток портала ScreenCast,
напротив, позволяет скрыть указатель. Снимок собирается так:

    * портал Screenshot даёт снимок всех мониторов (с указателем);
    * одновременно открывается сеанс ScreenCast со скрытым указателем, и с
      каждого разрешённого монитора берётся один кадр;
    * кадры накладываются на снимок поверх участков своих мониторов.

Портал не сообщает, какому монитору соответствует поток (в KDE Plasma
известен лишь размер, а у двух мониторов он может совпадать). Поэтому
кадр сопоставляется с монитором по сходству с участком снимка:
изображения различаются только указателем, и сходство однозначно.

Сопоставление запоминается вместе с конфигурацией мониторов: порядок
потоков в восстановленном сеансе не меняется. Пока конфигурация прежняя и
разрешены все мониторы, снимок собирается только из кадров потока, без
портала Screenshot, - это в разы быстрее: с двумя мониторами портал KDE
отвечает за две секунды, кадры потока приходят за десятые доли.

Если пользователь не разрешил захват или поток не получен, остаётся
обычный снимок портала - с указателем, но без потери снимка.

Функции модуля блокирующие и вызываются из фонового потока.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QImage, QPainter

from backends.wayland import gstreamer, screencast
from backends.wayland.screen import grab_virtual_desktop
from core import portal
from core.config import config_dir

# Обозначение разрешения на захват для снимков: выбор мониторов в диалоге
# портала запоминается отдельно от разрешений записи отдельных мониторов.
PERMISSION_KEY = "screenshot|all-monitors"

# Предельное время получения одного кадра.
FRAME_TIMEOUT_SEC = 6.0

# Размер уменьшенных копий для сравнения изображений. Достаточно мал для
# быстроты и достаточно велик, чтобы различить содержимое мониторов.
THUMBNAIL_WIDTH = 64
THUMBNAIL_HEIGHT = 36


@dataclass
class SnapshotResult:
    """Снимок и сведения о том, удалось ли убрать указатель."""

    image: QImage
    # Число мониторов, чьи участки заменены кадрами без указателя.
    replaced: int = 0
    # Причина, по которой кадры без указателя не получены.
    problem: str = ""
    # Кадры мониторов в родном разрешении вместе с логической геометрией
    # монитора. Общий снимок собран в наибольшем масштабе, и участок
    # монитора с меньшим масштабом в нём растянут; из родного кадра
    # область вырезается без потери чёткости.
    native: list[tuple[QRect, QImage]] = field(default_factory=list)


def native_frames(
    frames: list[QImage], pairs: list[tuple[int, int]], screens: list[QRect]
) -> list[tuple[QRect, QImage]]:
    """Родные кадры мониторов по сопоставлению кадров и участков."""
    return [
        (QRect(screens[region]), frames[frame])
        for frame, region in pairs
        if region < len(screens)
    ]


def crop_native(result: SnapshotResult, rect: QRect) -> QImage | None:
    """
    Вырезание области из родного кадра монитора.

    Пустое значение означает, что область не лежит целиком на одном
    мониторе либо его кадра нет, и вырезать следует из общего снимка.
    """
    for monitor, frame in result.native:
        if not monitor.contains(rect) or monitor.width() <= 0:
            continue
        scale_x = frame.width() / monitor.width()
        scale_y = frame.height() / monitor.height()
        local = QRect(
            round((rect.x() - monitor.x()) * scale_x),
            round((rect.y() - monitor.y()) * scale_y),
            round(rect.width() * scale_x),
            round(rect.height() * scale_y),
        ).intersected(frame.rect())
        if local.isEmpty():
            return None
        return frame.copy(local)
    return None


def grab_desktop_without_cursor(screens: list[QRect], desktop_rect: QRect) -> SnapshotResult:
    """
    Снимок всех мониторов без указателя мыши.

    Аргументы - логическая геометрия мониторов и всего рабочего стола,
    собранная в потоке интерфейса: обращаться к экранам Qt из фонового
    потока нельзя.
    """
    signature = _signature(screens)
    order = _load_order(signature)
    if portal.interface_version(portal.SCREENCAST_INTERFACE) is None:
        # Без захвата видеопотока указатель убрать нечем; сообщение об
        # отсутствии портала снимков, если его тоже нет, выдаст сам снимок.
        return SnapshotResult(grab_virtual_desktop(), 0, "no-screencast")
    if order is not None:
        # Быстрый путь: сопоставление известно, портал снимков не нужен.
        try:
            frames = _capture_frames()
        except portal.PortalCancelled:
            return SnapshotResult(grab_virtual_desktop(), 0, "cancelled")
        except Exception:  # noqa: BLE001 - переход к полному пути ниже
            frames = []
        if len(frames) == len(order):
            image = compose_frames(frames, order, screens, desktop_rect)
            pairs = list(enumerate(order))
            return SnapshotResult(
                image, len(frames), native=native_frames(frames, pairs, screens)
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        # Снимок портала и кадры потока запрашиваются одновременно: общая
        # задержка определяется более медленным из двух путей.
        base_future = pool.submit(grab_virtual_desktop)
        frames_future = pool.submit(_capture_frames)
        base = base_future.result()
        try:
            frames = frames_future.result()
        except portal.PortalCancelled:
            return SnapshotResult(base, 0, "cancelled")
        except Exception as error:  # noqa: BLE001 - снимок портала остаётся в силе
            return SnapshotResult(base, 0, str(error))

    regions = monitor_regions(base, screens, desktop_rect)
    pairs = match_frames(base, frames, regions + [base.rect()])
    image = compose(base, frames, regions + [base.rect()], pairs)
    covered = {region for _frame, region in pairs}
    if len(pairs) == len(frames) and (
        covered >= set(range(len(screens))) or len(screens) in covered
    ):
        # Кадры покрывают все мониторы: следующие снимки обойдутся без
        # портала снимков.
        by_frame = dict(pairs)
        _store_order(signature, [by_frame[index] for index in range(len(frames))])
    return SnapshotResult(image, len(pairs), native=native_frames(frames, pairs, screens))


def open_for_recording(
    target: int, screens: list[QRect], desktop_rect: QRect, show_cursor: bool
) -> tuple[screencast.CastStream, QRect | None] | None:
    """
    Открытие видеопотока монитора для записи.

    Запись пользуется тем же разрешением на все мониторы, что и снимки.
    Plasma 5.27 запоминает в разрешении не монитор, а его положение, и
    отдельные разрешения для каждого монитора путаются при смене
    расположения. Поток нужного монитора выбирается по сопоставлению,
    найденному для снимков; если его нет, оно находится снимком.

    Возвращается поток и логическая геометрия того, что он показывает
    (монитор или весь рабочий стол), либо пустое значение при отмене.
    Если нужный монитор не разрешён, выбор предлагается диалогом без
    запоминания; геометрия потока тогда неизвестна (пустое значение).
    Функция выполняется в фоновом потоке.
    """
    signature = _signature(screens)
    order = _load_order(signature)
    if order is None:
        result = grab_desktop_without_cursor(screens, desktop_rect)
        if result.problem == "cancelled":
            return None
        order = _load_order(signature)

    try:
        if order is not None:
            streams = screencast.open_streams(PERMISSION_KEY, show_cursor, multiple=True)
            screencast.store_restore_token(PERMISSION_KEY, streams[0].restore_token)
            if len(streams) == len(order):
                if target in order:
                    return streams[order.index(target)], QRect(screens[target])
                if len(screens) in order:
                    return streams[order.index(len(screens))], QRect(desktop_rect)
            # Нужного монитора среди разрешённых нет: сеанс не пригодится.
            portal.close_session(streams[0].session_path)
        stream = screencast.open_streams("", show_cursor, multiple=False, persist=False)[0]
    except portal.PortalCancelled:
        return None
    return stream, None


# ===========================================================================
# Запоминание сопоставления потоков и мониторов
# ===========================================================================


def _order_file() -> Path:
    """Файл с сопоставлением потоков снимка и мониторов."""
    return config_dir() / "screenshot-streams.json"


def _signature(screens: list[QRect]) -> str:
    """Обозначение конфигурации мониторов."""
    return ";".join(f"{r.x()},{r.y()},{r.width()},{r.height()}" for r in screens)


def _load_order(signature: str) -> list[int] | None:
    """Сохранённое сопоставление для конфигурации мониторов либо пустое значение."""
    try:
        data = json.loads(_order_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("signature") != signature:
        return None
    order = data.get("order")
    if not isinstance(order, list) or not all(isinstance(item, int) for item in order):
        return None
    return order


def _store_order(signature: str, order: list[int]) -> None:
    """Запоминание сопоставления потоков и мониторов."""
    try:
        path = _order_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"signature": signature, "order": order}), encoding="utf-8")
    except OSError:
        # Без сохранения следующий снимок просто пойдёт полным путём.
        pass


def forget_order() -> None:
    """Сброс сопоставления, например после его ошибки."""
    _order_file().unlink(missing_ok=True)


def _capture_frames() -> list[QImage]:
    """Получение по одному кадру со всех разрешённых мониторов."""
    streams = screencast.open_streams(PERMISSION_KEY, show_cursor=False, multiple=True)
    session_path = streams[0].session_path
    screencast.store_restore_token(PERMISSION_KEY, streams[0].restore_token)
    try:
        with ThreadPoolExecutor(max_workers=len(streams)) as pool:
            results = list(pool.map(lambda stream: _grab_frame(session_path, stream), streams))
    finally:
        portal.close_session(session_path)
    return [frame for frame in results if not frame.isNull()]


def _grab_frame(session_path: str, stream: screencast.CastStream) -> QImage:
    """Получение одного кадра потока в виде изображения."""
    descriptor = screencast.open_remote(session_path)
    kit = gstreamer.select()
    handle, name = tempfile.mkstemp(prefix="linscreen-frame-", suffix=".png")
    os.close(handle)
    path = Path(name)
    try:
        command = [
            kit.launch,
            "-q",
            "pipewiresrc",
            f"fd={descriptor}",
            f"path={stream.node_id}",
            "always-copy=true",
            # Первый же кадр потока содержит текущее изображение монитора.
            "num-buffers=1",
            "!",
            "videoconvert",
            "!",
            # Формат без канала прозрачности: GNOME отдаёт кадры с
            # незаполненной прозрачностью, и снимок получался блёклым.
            "video/x-raw,format=RGB",
            "!",
            # Сжатие не нужно: файл читается сразу и удаляется.
            "pngenc",
            "compression-level=0",
            "!",
            "filesink",
            f"location={path}",
        ]
        try:
            subprocess.run(
                command,
                pass_fds=(descriptor,),
                env=kit.environment,
                capture_output=True,
                timeout=FRAME_TIMEOUT_SEC,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return QImage()
        image = QImage(str(path))
        # Непрозрачный формат - страховка от канала прозрачности в кадре.
        return image.convertToFormat(QImage.Format.Format_RGB32) if not image.isNull() else image
    finally:
        os.close(descriptor)
        path.unlink(missing_ok=True)


# ===========================================================================
# Сопоставление кадров с мониторами
# ===========================================================================


def _thumbnail(image: QImage) -> bytes:
    """Уменьшенная полутоновая копия изображения для сравнения."""
    small = image.scaled(
        THUMBNAIL_WIDTH,
        THUMBNAIL_HEIGHT,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    ).convertToFormat(QImage.Format.Format_Grayscale8)
    stride = small.bytesPerLine()
    data = bytes(small.constBits())
    # Строки изображения выровнены, поэтому хвосты строк отбрасываются.
    return b"".join(
        data[row * stride : row * stride + THUMBNAIL_WIDTH] for row in range(THUMBNAIL_HEIGHT)
    )


def _same_proportions(frame: QImage, region: QRect) -> bool:
    """Совпадение пропорций кадра и участка с допуском в два процента."""
    if frame.height() <= 0 or region.height() <= 0 or region.width() <= 0:
        return False
    frame_ratio = frame.width() / frame.height()
    region_ratio = region.width() / region.height()
    return abs(frame_ratio - region_ratio) <= 0.02 * region_ratio


def _difference(first: bytes, second: bytes) -> float:
    """Средняя разница яркости двух уменьшенных копий."""
    return sum(abs(a - b) for a, b in zip(first, second)) / max(1, len(first))


def monitor_regions(base: QImage, screens: list[QRect], desktop_rect: QRect) -> list[QRect]:
    """Участки снимка, приходящиеся на мониторы, в точках снимка."""
    scale = base.width() / desktop_rect.width() if desktop_rect.width() else 1.0
    regions: list[QRect] = []
    for screen in screens:
        region = QRect(
            round((screen.x() - desktop_rect.x()) * scale),
            round((screen.y() - desktop_rect.y()) * scale),
            round(screen.width() * scale),
            round(screen.height() * scale),
        )
        regions.append(region.intersected(base.rect()))
    return regions


def match_frames(
    base: QImage, frames: list[QImage], regions: list[QRect]
) -> list[tuple[int, int]]:
    """
    Сопоставление кадров с участками мониторов.

    Возвращаются пары (номер кадра, номер участка). Пары выбираются по
    возрастанию различия, каждый кадр и каждый участок используются не
    более одного раза.
    """
    frame_thumbs = [_thumbnail(frame) for frame in frames]
    region_thumbs = [_thumbnail(base.copy(region)) for region in regions]
    # Кадр сопоставляется только участку с теми же пропорциями. Иначе при
    # однотонном фоне кадр монитора оказывается «похож» на весь рабочий
    # стол, растягивается на все мониторы и закрывает их содержимое.
    candidates = sorted(
        (_difference(frame_thumb, region_thumb), frame_index, region_index)
        for frame_index, frame_thumb in enumerate(frame_thumbs)
        for region_index, region_thumb in enumerate(region_thumbs)
        if _same_proportions(frames[frame_index], regions[region_index])
    )
    used_frames: set[int] = set()
    used_regions: set[int] = set()
    pairs: list[tuple[int, int]] = []
    for _difference_value, frame_index, region_index in candidates:
        if frame_index in used_frames or region_index in used_regions:
            continue
        used_frames.add(frame_index)
        used_regions.add(region_index)
        pairs.append((frame_index, region_index))
    return pairs


def compose(
    base: QImage, frames: list[QImage], regions: list[QRect], pairs: list[tuple[int, int]]
) -> QImage:
    """Наложение кадров без указателя на снимок портала."""
    result = base.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    painter = QPainter(result)
    try:
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        for frame_index, region_index in pairs:
            # Кадр потока обычно совпадает по размеру с участком; при
            # расхождении он масштабируется в границы участка.
            painter.drawImage(regions[region_index], frames[frame_index])
    finally:
        painter.end()
    return result


def compose_frames(
    frames: list[QImage], order: list[int], screens: list[QRect], desktop_rect: QRect
) -> QImage:
    """
    Сборка снимка рабочего стола только из кадров потока.

    Масштаб снимка равен наибольшему масштабу мониторов - так же поступает
    портал снимков, и дальнейшая обработка не различает источники.
    Номер участка, равный числу мониторов, означает кадр всего стола.
    """
    scale = 1.0
    for frame, region_index in zip(frames, order):
        area = screens[region_index] if region_index < len(screens) else desktop_rect
        if area.width():
            scale = max(scale, frame.width() / area.width())
    canvas = QImage(
        round(desktop_rect.width() * scale),
        round(desktop_rect.height() * scale),
        QImage.Format.Format_ARGB32_Premultiplied,
    )
    # Участки, не покрытые мониторами, остаются чёрными, как у портала.
    canvas.fill(0xFF000000)
    regions = monitor_regions(canvas, screens, desktop_rect) + [canvas.rect()]
    return compose(canvas, frames, regions, list(enumerate(order)))
