"""
Взаимодействие с xdg-desktop-portal через шину D-Bus.

Портал - единственный способ получить содержимое экрана в сессии Wayland:
композитор не даёт приложению прямого доступа к кадрам, а выдаёт его только
после подтверждения пользователем. Реализованы два интерфейса:

    * org.freedesktop.portal.Screenshot - снимок экрана;
    * org.freedesktop.portal.ScreenCast - поток кадров через PipeWire.

Обмен построен на шаблоне Request/Response: вызов метода возвращает путь
объекта запроса, а фактический результат приходит сигналом Response. Подписка
оформляется до вызова, иначе быстрый ответ портала будет пропущен.

Используется модуль QtDBus, входящий в состав PySide6: он работает в общем
цикле событий Qt, поэтому ожидание ответа не блокирует интерфейс и не требует
отдельного цикла asyncio или GLib.
"""

from __future__ import annotations

from core.i18n import tr

import os
import secrets
import urllib.parse
from dataclasses import dataclass

from PySide6.QtCore import QObject, QTimer, QUrl, SLOT, Signal, Slot
from PySide6.QtDBus import QDBusConnection, QDBusInterface, QDBusUnixFileDescriptor
from PySide6.QtGui import QImage

PORTAL_SERVICE = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
REQUEST_INTERFACE = "org.freedesktop.portal.Request"
SESSION_INTERFACE = "org.freedesktop.portal.Session"
SCREENSHOT_INTERFACE = "org.freedesktop.portal.Screenshot"
SCREENCAST_INTERFACE = "org.freedesktop.portal.ScreenCast"

# Коды ответа портала согласно спецификации интерфейса Request.
RESPONSE_SUCCESS = 0
RESPONSE_CANCELLED = 1
RESPONSE_OTHER = 2

# Предельное время ожидания ответа. Портал ждёт действий пользователя в
# диалоге подтверждения, поэтому срок взят с большим запасом.
REQUEST_TIMEOUT_MS = 120_000

# Режимы отрисовки указателя мыши в потоке ScreenCast согласно спецификации.
CURSOR_MODE_HIDDEN = 1
CURSOR_MODE_EMBEDDED = 2
CURSOR_MODE_METADATA = 4

# Типы источников: 1 - монитор, 2 - окно, 4 - виртуальный источник.
SOURCE_TYPE_MONITOR = 1
SOURCE_TYPE_WINDOW = 2


class PortalError(RuntimeError):
    """Портал недоступен либо вернул ошибку."""


def _session_bus() -> QDBusConnection:
    """Подключение к сессионной шине с проверкой работоспособности."""
    bus = QDBusConnection.sessionBus()
    if not bus.isConnected():
        raise PortalError(tr("Сессионная шина D-Bus недоступна"))
    return bus


def portal_version(interface: str) -> int | None:
    """
    Версия интерфейса портала либо пустое значение при его отсутствии.

    Вызов синхронный, но выполняется мгновенно: запрашивается свойство уже
    запущенной службы, диалогов пользователю не показывается.
    """
    try:
        bus = _session_bus()
    except PortalError:
        return None
    probe = QDBusInterface(PORTAL_SERVICE, PORTAL_PATH, interface, bus)
    if not probe.isValid():
        return None
    reply = probe.property("version")
    return int(reply) if reply is not None else None


def is_screenshot_available() -> bool:
    """Признак поддержки снимков экрана через портал."""
    return portal_version(SCREENSHOT_INTERFACE) is not None


def is_screencast_available() -> bool:
    """
    Признак поддержки захвата видеопотока через портал.

    Интерфейс предоставляют не все реализации: сборки xdg-desktop-portal-gtk
    и -xapp его не содержат, он появляется в бэкендах -gnome, -kde, -wlr
    и -hyprland.
    """
    return portal_version(SCREENCAST_INTERFACE) is not None


def _unique_token() -> str:
    """Случайная метка запроса, уникальная в пределах соединения."""
    return "linscreen_" + secrets.token_hex(8)


class PortalRequest(QObject):
    """
    Один запрос к порталу по шаблону Request/Response.

    Объект живёт до получения ответа или истечения срока ожидания, после чего
    удаляется самостоятельно. Подписка на сигнал оформляется в конструкторе, то есть до
    вызова метода портала.
    """

    # Успешный ответ: передаётся словарь результатов.
    succeeded = Signal(dict)
    # Пользователь отменил операцию в диалоге портала.
    cancelled = Signal()
    # Ошибка обмена или отказ портала.
    failed = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._bus = _session_bus()
        self._token = _unique_token()
        # Путь объекта запроса формируется порталом по известному правилу,
        # что позволяет подписаться на ответ заранее.
        sender = self._bus.baseService()[1:].replace(".", "_")
        self._path = f"/org/freedesktop/portal/desktop/request/{sender}/{self._token}"
        self._finished = False

        # Шестиаргументная форма connect отсутствует в стабах PySide6,
        # но поддерживается самой библиотекой и используется здесь.
        connected = self._bus.connect(  # type: ignore[call-overload]
            PORTAL_SERVICE,
            self._path,
            REQUEST_INTERFACE,
            "Response",
            self,
            SLOT("onResponse(uint,QVariantMap)"),
        )
        if not connected:
            raise PortalError(tr("Не удалось подписаться на ответ портала"))

        # Страховка от зависшего диалога: запрос закрывается принудительно.
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(REQUEST_TIMEOUT_MS)
        self._timer.timeout.connect(self._on_timeout)
        self._timer.start()

    @property
    def token(self) -> str:
        """Метка запроса, передаваемая порталу в параметрах вызова."""
        return self._token

    @Slot("uint", "QVariantMap")
    def onResponse(self, code: int, results: dict) -> None:  # noqa: N802 - имя слота
        """Обработка ответа портала."""
        if self._finished:
            return
        self._finished = True
        self._timer.stop()

        if code == RESPONSE_SUCCESS:
            self.succeeded.emit(dict(results))
        elif code == RESPONSE_CANCELLED:
            self.cancelled.emit()
        else:
            self.failed.emit(tr("Портал прервал выполнение запроса"))
        self.deleteLater()

    def close(self) -> None:
        """Досрочное закрытие запроса."""
        if self._finished:
            return
        self._finished = True
        self._timer.stop()
        QDBusInterface(PORTAL_SERVICE, self._path, REQUEST_INTERFACE, self._bus).call("Close")
        self.deleteLater()

    def _on_timeout(self) -> None:
        """Прекращение ожидания ответа."""
        if self._finished:
            return
        self._finished = True
        QDBusInterface(PORTAL_SERVICE, self._path, REQUEST_INTERFACE, self._bus).call("Close")
        self.failed.emit(tr("Портал не ответил за отведённое время"))
        self.deleteLater()


class ScreenshotPortal(QObject):
    """Снимок экрана средствами портала."""

    # Готовое изображение.
    captured = Signal(QImage)
    # Пользователь отказался от съёмки.
    cancelled = Signal()
    failed = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._request: PortalRequest | None = None

    def take(self, interactive: bool = False, cleanup: bool = True) -> None:
        """
        Запрос снимка экрана.

        Режим interactive передаёт выбор области самому порталу; при его
        отключении снимается весь экран. Файл, созданный порталом по запросу
        приложения, после чтения удаляется, если не указано иное.
        """
        self._cleanup = cleanup
        try:
            request = PortalRequest(self)
        except PortalError as error:
            self.failed.emit(str(error))
            return

        request.succeeded.connect(self._on_result)
        request.cancelled.connect(self.cancelled)
        request.failed.connect(self.failed)
        self._request = request

        portal = QDBusInterface(PORTAL_SERVICE, PORTAL_PATH, SCREENSHOT_INTERFACE, _session_bus())
        reply = portal.call(
            "Screenshot",
            "",
            {"handle_token": request.token, "interactive": interactive},
        )
        if reply.errorMessage():
            request.close()
            self.failed.emit(tr("Портал снимков отказал: {0}").format(reply.errorMessage()))

    def _on_result(self, results: dict) -> None:
        """Чтение файла, переданного порталом."""
        uri = str(results.get("uri", ""))
        if not uri:
            self.failed.emit(tr("Портал не вернул путь к снимку"))
            return

        # Портал возвращает адрес в виде URI с процентным кодированием,
        # поэтому обратное преобразование обязательно: без него путь с
        # национальными символами не найдётся на диске.
        path = QUrl(uri).toLocalFile() or urllib.parse.unquote(uri.removeprefix("file://"))

        image = QImage(path)
        if image.isNull():
            self.failed.emit(tr("Не удалось прочитать снимок по пути {0}").format(path))
            return

        if self._cleanup:
            try:
                # Файл создан порталом по запросу приложения и является
                # промежуточным: дальше снимок живёт в памяти.
                os.unlink(path)
            except OSError:
                # Невозможность удаления не влияет на результат операции.
                pass
        self.captured.emit(image)


@dataclass(frozen=True)
class PipeWireStream:
    """Дескриптор видеопотока, выданного порталом."""

    # Файловый дескриптор соединения с сервером PipeWire.
    file_descriptor: int
    # Идентификатор узла потока в графе PipeWire.
    node_id: int
    width: int = 0
    height: int = 0


class ScreenCastPortal(QObject):
    """
    Захват видеопотока экрана средствами портала и PipeWire.

    Последовательность обмена определена спецификацией портала:
    CreateSession, SelectSources, Start, OpenPipeWireRemote. Каждый шаг, кроме
    последнего, возвращает результат отдельным сигналом Response.
    """

    # Поток готов к чтению.
    ready = Signal(object)
    cancelled = Signal()
    failed = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._session_path = ""
        self._show_cursor = True

    def start(self, show_cursor: bool = True) -> None:
        """Запуск согласования захвата с пользователем."""
        if not is_screencast_available():
            self.failed.emit(
                tr(
                    "Установленный бэкенд xdg-desktop-portal не предоставляет "
                    "интерфейс ScreenCast. Требуется пакет xdg-desktop-portal-gnome, "
                    "-kde, -wlr или -hyprland."
                )
            )
            return

        self._show_cursor = show_cursor
        try:
            request = PortalRequest(self)
        except PortalError as error:
            self.failed.emit(str(error))
            return

        request.succeeded.connect(self._on_session_created)
        request.cancelled.connect(self.cancelled)
        request.failed.connect(self.failed)

        portal = self._portal()
        reply = portal.call(
            "CreateSession",
            {"handle_token": request.token, "session_handle_token": _unique_token()},
        )
        if reply.errorMessage():
            request.close()
            self.failed.emit(tr("Портал захвата отказал: {0}").format(reply.errorMessage()))

    def _portal(self) -> QDBusInterface:
        """Интерфейс портала захвата экрана."""
        return QDBusInterface(PORTAL_SERVICE, PORTAL_PATH, SCREENCAST_INTERFACE, _session_bus())

    def _on_session_created(self, results: dict) -> None:
        """Выбор источников после создания сеанса."""
        self._session_path = str(results.get("session_handle", ""))
        if not self._session_path:
            self.failed.emit(tr("Портал не вернул идентификатор сеанса"))
            return

        try:
            request = PortalRequest(self)
        except PortalError as error:
            self.failed.emit(str(error))
            return
        request.succeeded.connect(self._on_sources_selected)
        request.cancelled.connect(self.cancelled)
        request.failed.connect(self.failed)

        reply = self._portal().call(
            "SelectSources",
            self._session_path,
            {
                "handle_token": request.token,
                # Запрашиваются мониторы и окна: конкретный выбор остаётся
                # за пользователем в диалоге портала.
                "types": SOURCE_TYPE_MONITOR | SOURCE_TYPE_WINDOW,
                "multiple": False,
                "cursor_mode": CURSOR_MODE_EMBEDDED if self._show_cursor else CURSOR_MODE_HIDDEN,
            },
        )
        if reply.errorMessage():
            request.close()
            self.failed.emit(tr("Не удалось выбрать источник: {0}").format(reply.errorMessage()))

    def _on_sources_selected(self, _results: dict) -> None:
        """Запуск сеанса захвата."""
        try:
            request = PortalRequest(self)
        except PortalError as error:
            self.failed.emit(str(error))
            return
        request.succeeded.connect(self._on_started)
        request.cancelled.connect(self.cancelled)
        request.failed.connect(self.failed)

        reply = self._portal().call(
            "Start", self._session_path, "", {"handle_token": request.token}
        )
        if reply.errorMessage():
            request.close()
            self.failed.emit(tr("Не удалось запустить захват: {0}").format(reply.errorMessage()))

    def _on_started(self, results: dict) -> None:
        """Получение дескриптора потока PipeWire."""
        streams = results.get("streams") or []
        if not streams:
            self.failed.emit(tr("Портал не вернул ни одного потока"))
            return

        node_id, properties = self._first_stream(streams)
        size = properties.get("size") if isinstance(properties, dict) else None
        width, height = (size[0], size[1]) if isinstance(size, (list, tuple)) else (0, 0)

        reply = self._portal().call("OpenPipeWireRemote", self._session_path, {})
        if reply.errorMessage():
            self.failed.emit(tr("Не удалось открыть поток: {0}").format(reply.errorMessage()))
            return

        arguments = reply.arguments()
        if not arguments or not isinstance(arguments[0], QDBusUnixFileDescriptor):
            self.failed.emit(tr("Портал не передал файловый дескриптор потока"))
            return

        # Дескриптор дублируется: копия не наследует признак закрытия при
        # запуске дочернего процесса, поэтому её получит FFmpeg.
        descriptor = os.dup(arguments[0].fileDescriptor())
        self.ready.emit(PipeWireStream(descriptor, node_id, width, height))

    @staticmethod
    def _first_stream(streams: object) -> tuple[int, dict]:
        """
        Разбор первого потока из ответа портала.

        Структура a(ua{sv}) разворачивается в пару из идентификатора узла и
        словаря свойств.
        """
        try:
            entry = streams[0]  # type: ignore[index]
            return int(entry[0]), dict(entry[1])
        except (IndexError, KeyError, TypeError, ValueError):
            return 0, {}

    def close_session(self) -> None:
        """Завершение сеанса захвата."""
        if not self._session_path:
            return
        QDBusInterface(PORTAL_SERVICE, self._session_path, SESSION_INTERFACE, _session_bus()).call(
            "Close"
        )
        self._session_path = ""
