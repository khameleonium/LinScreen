"""
Обмен с xdg-desktop-portal и другими службами сессионной шины D-Bus.

Порталы - единственный стандартный для всех композиторов Wayland способ
получить снимок экрана, видеопоток и глобальные сочетания клавиш. Реализации
различаются (xdg-desktop-portal-kde, -gnome, -wlr), но интерфейс общий,
поэтому приложение ведёт себя одинаково во всех окружениях.

Для обмена применяется библиотека jeepney, а не модуль QtDBus. Порталы
строго проверяют типы параметров: число режима курсора обязано быть
беззнаковым (uint32), иначе параметр молча отбрасывается. Привязки PySide6
передают любое целое число только как int32, а jeepney требует явной
сигнатуры каждого значения. Кроме того, jeepney написана на чистом Python,
поддерживает передачу файловых дескрипторов (нужна для потока PipeWire) и
не тянет системных библиотек в сборку.

Все функции модуля, кроме подписки на сигналы, блокирующие и вызываются
только из фоновых потоков (core.workers.run_async). Соединение общее: его
маршрутизатор потокобезопасен и сам разбирает входящие сообщения в
отдельном потоке.
"""

from __future__ import annotations

from core.i18n import tr

import secrets
import threading
from typing import Any, Callable, Sequence

from PySide6.QtCore import QObject, Signal

from core.identity import APPLICATION_ID

try:
    from jeepney import DBusAddress, MatchRule, MessageType, message_bus, new_method_call
    from jeepney.io.threading import DBusRouter, Proxy, open_dbus_router
except ImportError:  # pragma: no cover - проверяется режимом --check
    DBusAddress = MatchRule = MessageType = message_bus = new_method_call = None
    DBusRouter = Proxy = open_dbus_router = None

PORTAL_SERVICE = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
REQUEST_INTERFACE = "org.freedesktop.portal.Request"
SESSION_INTERFACE = "org.freedesktop.portal.Session"
SCREENSHOT_INTERFACE = "org.freedesktop.portal.Screenshot"
SCREENCAST_INTERFACE = "org.freedesktop.portal.ScreenCast"
SHORTCUTS_INTERFACE = "org.freedesktop.portal.GlobalShortcuts"
REGISTRY_INTERFACE = "org.freedesktop.host.portal.Registry"

# Коды ответа портала согласно спецификации интерфейса Request.
RESPONSE_SUCCESS = 0
RESPONSE_CANCELLED = 1

# Предельное время ожидания ответа на запрос. Портал ждёт решения
# пользователя в диалоге подтверждения, поэтому срок взят с запасом.
REQUEST_TIMEOUT_SEC = 180.0

# Предельное время ожидания ответа на простой вызов метода.
CALL_TIMEOUT_SEC = 10.0

# Ёмкость очереди подписки на сигналы. Сигналы копятся в ней, пока поток
# доставки передаёт предыдущие в цикл событий Qt.
SIGNAL_QUEUE_SIZE = 256


class PortalError(RuntimeError):
    """Служба недоступна либо вернула ошибку."""


class PortalCancelled(RuntimeError):
    """Пользователь отменил операцию в диалоге портала."""


# ===========================================================================
# Соединение с шиной
# ===========================================================================

_router: Any = None
_router_manager: list[Any] = []
_router_lock = threading.Lock()
# Результат регистрации идентификатора приложения: пустая строка означает
# успех, иначе - причина отказа для показа в режиме --check.
_registration_error: str | None = None


def is_available() -> bool:
    """Признак наличия библиотеки обмена с шиной D-Bus."""
    return open_dbus_router is not None


def connection() -> Any:
    """
    Общее соединение с сессионной шиной.

    Соединение открывается при первом обращении. Сразу после открытия
    приложение регистрирует свой идентификатор у портала: регистрация
    допускается только до первого обращения к порталам по этому соединению.
    """
    global _router, _registration_error
    with _router_lock:
        if _router is not None:
            return _router
        if open_dbus_router is None:
            raise PortalError(tr("Не установлена библиотека jeepney для работы с D-Bus"))
        try:
            manager = open_dbus_router(bus="SESSION", enable_fds=True)
            # Контекстный менеджер открывается вручную: соединение живёт
            # до завершения процесса. Ссылка на менеджер удерживается - он
            # построен на генераторе, и его удаление сборщиком мусора
            # закрыло бы соединение.
            router = manager.__enter__()
            _router_manager.append(manager)
        except Exception as error:  # noqa: BLE001 - любая ошибка означает отсутствие шины
            raise PortalError(tr("Сессионная шина D-Bus недоступна: {0}").format(error))
        _router = router
        _registration_error = _register_application(router)
        return router


def registration_error() -> str | None:
    """Причина отказа в регистрации приложения у портала, если он был."""
    return _registration_error


def _register_application(router: Any) -> str:
    """
    Регистрация идентификатора приложения у xdg-desktop-portal.

    Приложения, запущенные без песочницы, портал по умолчанию считает
    безымянными. Регистрация связывает соединение с ярлыком приложения:
    по нему портал сохраняет выданные разрешения, а служба сочетаний клавиш
    отличает приложение от других. Интерфейс появился в версии 1.19,
    более старый портал просто продолжит работать без регистрации.
    """
    address = DBusAddress(PORTAL_PATH, PORTAL_SERVICE, REGISTRY_INTERFACE)
    try:
        reply = router.send_and_get_reply(
            new_method_call(address, "Register", "sa{sv}", (APPLICATION_ID, {})),
            timeout=CALL_TIMEOUT_SEC,
        )
    except Exception as error:  # noqa: BLE001 - регистрация необязательна
        return str(error)
    if reply.header.message_type is MessageType.error:
        return str(reply.body[0]) if reply.body else tr("отказ без пояснения")
    return ""


def unique_name() -> str:
    """Уникальное имя соединения на шине вида :1.234."""
    return str(connection().unique_name)


def call(
    path: str,
    interface: str,
    method: str,
    signature: str | None = None,
    body: Sequence[Any] = (),
    service: str = PORTAL_SERVICE,
    timeout: float = CALL_TIMEOUT_SEC,
) -> tuple[Any, ...]:
    """Вызов метода с ожиданием ответа; ошибка службы превращается в исключение."""
    router = connection()
    address = DBusAddress(path, service, interface)
    message = new_method_call(address, method, signature, tuple(body))
    try:
        reply = router.send_and_get_reply(message, timeout=timeout)
    except TimeoutError:
        raise PortalError(tr("Служба {0} не ответила вовремя").format(service))
    except Exception as error:  # noqa: BLE001 - обрыв соединения и подобное
        raise PortalError(str(error))
    if reply.header.message_type is MessageType.error:
        detail = reply.body[0] if reply.body else reply.header.fields
        raise PortalError(str(detail))
    return tuple(reply.body)


def add_match(rule: Any) -> None:
    """Просьба к шине доставлять сообщения, подходящие под правило."""
    Proxy(message_bus, connection()).AddMatch(rule)


def remove_match(rule: Any) -> None:
    """Отмена доставки сообщений по правилу."""
    try:
        Proxy(message_bus, connection()).RemoveMatch(rule)
    except Exception:  # noqa: BLE001 - соединение могло закрыться
        pass


# ===========================================================================
# Разбор значений со сигнатурой
# ===========================================================================


def _split_types(signature: str) -> list[str]:
    """Разбиение сигнатуры на полные типы верхнего уровня."""
    types: list[str] = []
    index = 0
    while index < len(signature):
        end = _type_end(signature, index)
        types.append(signature[index:end])
        index = end
    return types


def _type_end(signature: str, start: int) -> int:
    """Позиция конца полного типа, начинающегося в указанной позиции."""
    char = signature[start]
    if char == "a":
        return _type_end(signature, start + 1)
    if char in "({":
        closing = ")" if char == "(" else "}"
        depth = 0
        for index in range(start, len(signature)):
            if signature[index] in "({":
                depth += 1
            elif signature[index] in ")}":
                depth -= 1
                if depth == 0 and signature[index] == closing:
                    return index + 1
        raise ValueError(f"unbalanced signature: {signature}")
    return start + 1


def convert(signature: str, value: Any) -> Any:
    """
    Приведение значения шины к обычным типам Python.

    jeepney представляет вариант парой (сигнатура, значение). Пары
    разворачиваются рекурсивно по сигнатуре, поэтому структура с первым
    строковым полем не путается с вариантом.
    """
    if signature == "v":
        inner_signature, inner_value = value
        return convert(inner_signature, inner_value)
    if signature.startswith("a{"):
        key_type, value_type = _split_types(signature[2:-1])
        return {
            convert(key_type, key): convert(value_type, item) for key, item in dict(value).items()
        }
    if signature.startswith("a"):
        item_type = signature[1:]
        return [convert(item_type, item) for item in value]
    if signature.startswith("("):
        return tuple(
            convert(item_type, item)
            for item_type, item in zip(_split_types(signature[1:-1]), value)
        )
    return value


def vardict(values: dict[str, Any]) -> dict[str, Any]:
    """
    Приведение словаря a{sv} к обычным значениям.

    Результаты ответов порталов всегда передаются этим типом.
    """
    return convert("a{sv}", values)


# ===========================================================================
# Запросы по шаблону Request/Response
# ===========================================================================


def new_token() -> str:
    """Случайная метка запроса, уникальная в пределах соединения."""
    return "linscreen_" + secrets.token_hex(8)


def request_path(token: str) -> str:
    """
    Путь объекта запроса, который создаст портал.

    Правило формирования пути закреплено спецификацией, что позволяет
    подписаться на ответ до вызова метода: иначе быстрый ответ портала
    был бы пропущен.
    """
    sender = unique_name().lstrip(":").replace(".", "_")
    return f"{PORTAL_PATH}/request/{sender}/{token}"


def request(
    interface: str,
    method: str,
    signature: str,
    make_body: Callable[[str], Sequence[Any]],
    timeout: float = REQUEST_TIMEOUT_SEC,
) -> dict[str, Any]:
    """
    Выполнение запроса к порталу с ожиданием ответа.

    Функция make_body получает метку запроса и возвращает аргументы
    вызова, в которые метка должна попасть параметром handle_token.
    Возвращается словарь результатов; отмена пользователем порождает
    исключение PortalCancelled, прочие отказы - PortalError.
    """
    router = connection()
    token = new_token()
    rule = MatchRule(
        type="signal",
        interface=REQUEST_INTERFACE,
        member="Response",
        path=request_path(token),
    )
    add_match(rule)
    try:
        with router.filter(rule, bufsize=4) as queue:
            call(PORTAL_PATH, interface, method, signature, make_body(token))
            try:
                message = queue.get(timeout=timeout)
            except Exception:  # noqa: BLE001 - истечение срока ожидания
                _close_request(request_path(token))
                raise PortalError(tr("Портал не ответил за отведённое время"))
    finally:
        remove_match(rule)

    code, results = message.body
    if code == RESPONSE_SUCCESS:
        return vardict(results)
    if code == RESPONSE_CANCELLED:
        raise PortalCancelled(tr("Операция отменена пользователем"))
    raise PortalError(tr("Портал прервал выполнение запроса"))


def _close_request(path: str) -> None:
    """Закрытие зависшего запроса, чтобы портал убрал свой диалог."""
    try:
        call(path, REQUEST_INTERFACE, "Close")
    except PortalError:
        pass


def close_session(path: str) -> None:
    """Завершение сеанса портала (захвата экрана или сочетаний клавиш)."""
    if not path:
        return
    try:
        call(path, SESSION_INTERFACE, "Close")
    except PortalError:
        # Сеанс мог быть уже закрыт порталом или пользователем.
        pass


def interface_version(interface: str) -> int | None:
    """
    Версия интерфейса портала либо пустое значение при его отсутствии.

    Реализации порталов предоставляют разный набор интерфейсов: например,
    xdg-desktop-portal-gtk не умеет захватывать видеопоток, а
    GlobalShortcuts появился в GNOME только в версии 48.
    """
    try:
        (value,) = call(
            PORTAL_PATH,
            "org.freedesktop.DBus.Properties",
            "Get",
            "ss",
            (interface, "version"),
        )
    except PortalError:
        return None
    try:
        return int(convert("v", value))
    except (TypeError, ValueError):
        return None


def interface_property(interface: str, name: str) -> Any:
    """Значение свойства интерфейса портала либо пустое значение."""
    try:
        (value,) = call(
            PORTAL_PATH,
            "org.freedesktop.DBus.Properties",
            "Get",
            "ss",
            (interface, name),
        )
    except PortalError:
        return None
    return convert("v", value)


# ===========================================================================
# Подписка на сигналы
# ===========================================================================


class SignalListener(QObject):
    """
    Доставка сигналов шины в цикл событий Qt.

    Сообщения принимает маршрутизатор соединения в своём потоке и кладёт
    в очередь подписки. Отдельный поток разбирает очередь и испускает
    сигнал Qt: соединение со слотами в потоке интерфейса выполняется
    очередью событий, поэтому обработчики работают с виджетами безопасно.
    """

    # Тело принятого сообщения шины.
    received = Signal(object)
    # Подписаться не удалось: передаётся причина.
    failed = Signal(str)

    def __init__(
        self,
        rule: Any,
        parent: QObject | None = None,
        on_message: Callable[[Any], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._rule = rule
        # Обработчик, вызываемый прямо в потоке доставки до передачи
        # сообщения в поток интерфейса. Нужен для немедленного ответа на
        # входящие вызовы методов.
        self._on_message = on_message
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Прекращение доставки; поток завершится в течение полсекунды."""
        self._stopped.set()

    def _run(self) -> None:
        """Цикл потока доставки."""
        try:
            router = connection()
            add_match(self._rule)
        except Exception as error:  # noqa: BLE001 - причина уходит в интерфейс
            self._emit(self.failed, str(error))
            return
        try:
            with router.filter(self._rule, bufsize=SIGNAL_QUEUE_SIZE) as queue:
                while not self._stopped.is_set():
                    try:
                        # Ожидание ограничено, чтобы поток замечал остановку.
                        message = queue.get(timeout=0.5)
                    except Exception:  # noqa: BLE001 - истекло время ожидания
                        continue
                    if self._on_message is not None:
                        try:
                            self._on_message(message)
                        except Exception:  # noqa: BLE001 - поток не должен падать
                            pass
                    self._emit(self.received, message)
        finally:
            remove_match(self._rule)

    @staticmethod
    def _emit(signal: Any, payload: Any) -> None:
        """Испускание сигнала с защитой от уже удалённого объекта."""
        try:
            signal.emit(payload)
        except RuntimeError:
            pass
