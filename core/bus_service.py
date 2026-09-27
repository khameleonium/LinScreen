"""
Служба приложения на сессионной шине D-Bus.

Служба решает две задачи:
    * приём команд от повторно запущенного экземпляра: вызов вида
      "linscreen --screenshot" передаёт действие работающему приложению,
      что позволяет назначить его на сочетание клавиш средствами
      рабочего стола, если портал сочетаний клавиш недоступен;
    * приём сведений от вспомогательных скриптов композитора KDE: скрипт
      KWin не может вернуть результат напрямую и вызывает метод службы.

Вызовы принимаются потоком подписки и передаются в поток интерфейса
сигналами Qt; ответ вызывающей стороне отправляется сразу из потока
подписки, поэтому медленная обработка не задерживает её.
"""

from __future__ import annotations

import queue
import threading
from typing import Any

from PySide6.QtCore import QObject, Signal

from core import portal
from core.identity import BUS_INTERFACE, BUS_NAME, BUS_PATH

try:
    from jeepney import HeaderFields, MatchRule, message_bus, new_method_return
    from jeepney.bus_messages import DBusNameFlags
    from jeepney.io.threading import Proxy
except ImportError:  # pragma: no cover - проверяется режимом --check
    HeaderFields = MatchRule = message_bus = new_method_return = None
    DBusNameFlags = Proxy = None

# Описание объекта службы для запроса Introspect. Утилита gdbus перед
# вызовом метода запрашивает описание и без ответа ждёт около трёх секунд;
# сочетания клавиш GNOME вызывают службу именно через gdbus.
INTROSPECTABLE_INTERFACE = "org.freedesktop.DBus.Introspectable"
INTROSPECTION_XML = f"""<node>
  <interface name="{INTROSPECTABLE_INTERFACE}">
    <method name="Introspect"><arg name="xml" type="s" direction="out"/></method>
  </interface>
  <interface name="{BUS_INTERFACE}">
    <method name="Activate"><arg name="action" type="s" direction="in"/></method>
    <method name="ReportWindows">
      <arg name="token" type="s" direction="in"/>
      <arg name="payload" type="s" direction="in"/>
    </method>
  </interface>
</node>
"""

# Коды ответа RequestName согласно спецификации D-Bus.
NAME_PRIMARY_OWNER = 1
NAME_ALREADY_OWNER = 4

# Ожидающие ответа сборщики данных от скриптов композитора: метка
# вызова сопоставлена очереди, в которую попадёт результат.
_waiters: dict[str, "queue.Queue[str]"] = {}
_waiters_lock = threading.Lock()


def expect_report(token: str) -> "queue.Queue[str]":
    """Регистрация ожидания отчёта скрипта с указанной меткой."""
    result: "queue.Queue[str]" = queue.Queue(maxsize=1)
    with _waiters_lock:
        _waiters[token] = result
    return result


def forget_report(token: str) -> None:
    """Снятие ожидания отчёта."""
    with _waiters_lock:
        _waiters.pop(token, None)


def _deliver_report(token: str, payload: str) -> None:
    """Передача отчёта скрипта ожидающему его потоку."""
    with _waiters_lock:
        waiter = _waiters.get(token)
    if waiter is not None:
        try:
            waiter.put_nowait(payload)
        except queue.Full:
            pass


def claim_name() -> bool:
    """
    Захват имени службы на шине.

    Возвращается признак успеха. Неудача означает, что имя принадлежит
    другому процессу - уже работающему экземпляру приложения.
    Функция блокирующая, но вызывается при старте до цикла событий.
    """
    try:
        router = portal.connection()
        (code,) = Proxy(message_bus, router).RequestName(
            BUS_NAME, DBusNameFlags.do_not_queue
        )
    except Exception:  # noqa: BLE001 - шина недоступна
        return False
    return code in (NAME_PRIMARY_OWNER, NAME_ALREADY_OWNER)


def send_action(action: str) -> bool:
    """
    Передача действия работающему экземпляру.

    Используется повторно запущенным процессом с ключом командной строки.
    Возвращается признак доставки.
    """
    try:
        portal.call(BUS_PATH, BUS_INTERFACE, "Activate", "s", (action,), service=BUS_NAME)
    except portal.PortalError:
        return False
    return True


class BusService(QObject):
    """Обработчик вызовов, адресованных службе приложения."""

    # Запрошено действие из командной строки: передаётся его имя.
    actionRequested = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._listener: portal.SignalListener | None = None

    def start(self) -> None:
        """Начало приёма вызовов."""
        if MatchRule is None or self._listener is not None:
            return
        # Интерфейс в правиле не указывается: кроме вызовов службы
        # принимается и запрос описания объекта.
        rule = MatchRule(type="method_call", path=BUS_PATH)
        listener = portal.SignalListener(rule, self, on_message=self._answer)
        listener.received.connect(self._dispatch)
        self._listener = listener

    def stop(self) -> None:
        """Прекращение приёма вызовов."""
        if self._listener is not None:
            self._listener.stop()
            self._listener = None

    @staticmethod
    def _answer(message: Any) -> None:
        """
        Немедленный ответ вызывающей стороне из потока подписки.

        Отчёты скриптов передаются ожидающим потокам прямо здесь, минуя
        поток интерфейса: сборщик ждёт их в фоновом потоке.
        """
        member = message.header.fields.get(HeaderFields.member)
        interface = message.header.fields.get(HeaderFields.interface)
        if member == "ReportWindows" and len(message.body) >= 2:
            _deliver_report(str(message.body[0]), str(message.body[1]))
        if interface == INTROSPECTABLE_INTERFACE and member == "Introspect":
            reply = new_method_return(message, "s", (INTROSPECTION_XML,))
        else:
            reply = new_method_return(message)
        try:
            portal.connection().send(reply)
        except Exception:  # noqa: BLE001 - вызывающая сторона могла отключиться
            pass

    def _dispatch(self, message: Any) -> None:
        """Обработка вызова в потоке интерфейса."""
        member = message.header.fields.get(HeaderFields.member)
        interface = message.header.fields.get(HeaderFields.interface)
        if interface == BUS_INTERFACE and member == "Activate" and message.body:
            self.actionRequested.emit(str(message.body[0]))
