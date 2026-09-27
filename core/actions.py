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


# Ключи командной строки, передающие действие работающему экземпляру.
# Их можно назначить на клавиши средствами рабочего стола, если портал
# глобальных сочетаний недоступен.
COMMAND_LINE_ACTIONS: dict[str, str] = {
    "--screenshot": "screenshot_region",
    "--screenshot-full": "screenshot_fullscreen",
    "--screenshot-window": "screenshot_window",
    "--record": "record_toggle",
    "--pause": "record_toggle_pause",
    "--settings": "open_settings",
    "--check-system": "check_system",
    "--quit": "quit",
}


def command_line_argument(action: str) -> str:
    """Ключ командной строки действия либо пустая строка."""
    for argument, name in COMMAND_LINE_ACTIONS.items():
        if name == action:
            return argument
    return ""
