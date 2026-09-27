"""
Снимок рабочего стола через портал Screenshot (Wayland).

Приложение Wayland не видит чужих окон и не может прочитать содержимое
экрана само. Снимок выдаёт портал Screenshot: композитор сохраняет кадр
всего рабочего стола в файл и возвращает его адрес. Приложение читает
файл и сразу удаляет его - дальше снимок живёт в памяти.
"""

from __future__ import annotations

from core.i18n import tr

import os
import urllib.parse

from PySide6.QtCore import QUrl
from PySide6.QtGui import QImage

from capture.screen import CaptureBackendError, CaptureCancelled
from core import portal


# ===========================================================================
# Получение растрового снимка
# ===========================================================================


def _local_path(uri: str) -> str:
    """
    Путь к файлу по адресу, возвращённому порталом.

    Портал возвращает адрес с процентным кодированием: без обратного
    преобразования путь с национальными символами не найдётся на диске.
    """
    path = QUrl(uri).toLocalFile()
    if path:
        return path
    return urllib.parse.unquote(uri.removeprefix("file://"))


def missing_portal_message() -> str:
    """Пояснение для случая, когда в системе нет бэкенда порталов рабочего стола."""
    return tr(
        "Рабочий стол не предоставляет снимки экрана и запись через порталы. "
        "Требуется бэкенд порталов для вашего окружения: пакет "
        "xdg-desktop-portal-gnome для GNOME или xdg-desktop-portal-kde для KDE Plasma."
    )


def grab_virtual_desktop() -> QImage:
    """
    Снимок всего рабочего стола через портал Screenshot.

    Функция блокирующая и вызывается из фонового потока: портал отвечает
    за доли секунды, но при первом обращении композитор может спросить
    разрешение у пользователя. Снимок содержит все мониторы в физических
    точках.
    """
    if portal.interface_version(portal.SCREENSHOT_INTERFACE) is None:
        raise CaptureBackendError(missing_portal_message())
    try:
        results = portal.request(
            portal.SCREENSHOT_INTERFACE,
            "Screenshot",
            "sa{sv}",
            lambda token: (
                "",
                {
                    "handle_token": ("s", token),
                    # Выбор области ведётся оверлеем приложения, поэтому
                    # собственный диалог портала не нужен.
                    "interactive": ("b", False),
                },
            ),
        )
    except portal.PortalCancelled as error:
        raise CaptureCancelled(str(error))
    except portal.PortalError as error:
        raise CaptureBackendError(tr("Портал снимков отказал: {0}").format(error))

    uri = str(results.get("uri", ""))
    if not uri:
        raise CaptureBackendError(tr("Портал не вернул путь к снимку"))

    path = _local_path(uri)
    image = QImage(path)
    try:
        # Файл создан порталом по запросу приложения и является
        # промежуточным: без удаления каталог снимков пользователя
        # заполнялся бы копиями.
        os.unlink(path)
    except OSError:
        # Невозможность удаления не влияет на результат операции.
        pass
    if image.isNull():
        raise CaptureBackendError(tr("Не удалось прочитать снимок по пути {0}").format(path))
    # Приведение к формату с альфа-каналом упрощает дальнейшую отрисовку
    # и совпадает с форматом, который ожидает редактор.
    return image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
