"""
Действия приложения, назначаемые на сочетания клавиш.

Названия показываются рабочим столом в его настройках клавиш и
приложением в сообщениях, поэтому они общие для обеих графических систем.
"""

from __future__ import annotations

from core.i18n import tr


def action_titles() -> dict[str, str]:
    """Описания действий, показываемые рабочим столом в настройках клавиш."""
    return {
        "screenshot_region": tr("Снимок области"),
        "screenshot_fullscreen": tr("Снимок всего экрана"),
        "screenshot_window": tr("Снимок окна под курсором"),
        "record_toggle": tr("Запись области: старт и стоп"),
        "record_toggle_pause": tr("Пауза записи"),
    }
