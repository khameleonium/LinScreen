"""
Уведомления рабочего стола через службу org.freedesktop.Notifications.

Qt показывает уведомления только через значок в трее. Без трея - а в
GNOME его нет, пока не включено расширение AppIndicator, - сообщения
отправляются напрямую стандартной службе уведомлений, которую
предоставляет любое окружение рабочего стола.

Отправка выполняется в отдельном потоке: служба отвечает быстро, но
поток интерфейса не должен ждать ответа по шине.
"""

from __future__ import annotations

import threading

from core import portal
from core.identity import APPLICATION_ID, DISPLAY_NAME

NOTIFICATIONS_SERVICE = "org.freedesktop.Notifications"
NOTIFICATIONS_PATH = "/org/freedesktop/Notifications"
NOTIFICATIONS_INTERFACE = "org.freedesktop.Notifications"

# Срочность уведомления по спецификации: 1 - обычная, 2 - критическая.
URGENCY_NORMAL = 1
URGENCY_CRITICAL = 2

# Время показа уведомления в миллисекундах.
DISPLAY_TIME_MS = 5000


def send(title: str, message: str, is_error: bool = False, persistent: bool = False) -> None:
    """
    Отправка уведомления без ожидания ответа.

    Признак persistent оставляет уведомление в списке, пока пользователь
    его не закроет: так показываются инструкции, которые нужно успеть
    прочитать.
    """

    def deliver() -> None:
        """Вызов службы уведомлений в фоновом потоке."""
        hints = {
            "urgency": ("y", URGENCY_CRITICAL if is_error else URGENCY_NORMAL),
            # Имя ярлыка позволяет рабочему столу показать значок и
            # название приложения рядом с уведомлением.
            "desktop-entry": ("s", APPLICATION_ID),
        }
        try:
            portal.call(
                NOTIFICATIONS_PATH,
                NOTIFICATIONS_INTERFACE,
                "Notify",
                "susssasa{sv}i",
                (
                    DISPLAY_NAME,
                    0,
                    "camera-photo",
                    title,
                    message,
                    [],
                    hints,
                    0 if persistent else DISPLAY_TIME_MS,
                ),
                service=NOTIFICATIONS_SERVICE,
            )
        except portal.PortalError:
            # Без службы уведомлений сообщение остаётся только в журнале.
            pass

    threading.Thread(target=deliver, daemon=True).start()
