"""
Определение границ окон под указателем мыши (Wayland).

Протокол Wayland не раскрывает приложению чужие окна, поэтому единого
способа получить их перечень нет. Сведения берутся у композитора, если он
их предоставляет:
    * KDE Plasma - скриптом KWin (backends/wayland/kwin.py): видимые окна текущего
      рабочего стола с порядком наложения;
    * прочие окружения перечень не выдают, и подсветка окна под курсором
      не применяется. Ручное выделение области работает везде одинаково.

Перечень собирается один раз перед показом оверлея выделения, после чего
поиск объекта под курсором ведётся в памяти.

В отличие от X11, дочерние элементы окон (панели, кнопки) в Wayland
отдельными окнами не являются и не определяются: подсвечивается окно
целиком вместе с рамкой.
"""

from __future__ import annotations

import os
from PySide6.QtCore import QRect

from capture.windows import MINIMUM_SIZE, InterfaceObject

# Скрипт KWin, собирающий видимые окна. Отбрасываются свёрнутые окна,
# окна других рабочих столов, фон рабочего стола, всплывающие подсказки и
# меню, а также окна самого приложения: к моменту сбора на экране уже
# открыт оверлей выделения, и иначе он оказался бы единственным
# совпадением под курсором.
_LIST_WINDOWS = """
const own = %(pid)d;
result = [];
const windows = stacking();
for (let i = 0; i < windows.length; ++i) {
    const w = windows[i];
    if (w.deleted || w.minimized || w.hidden) continue;
    if (w.pid === own) continue;
    if (w.desktopWindow || w.tooltip || w.notification || w.criticalNotification) continue;
    if (w.popupWindow || w.dropdownMenu || w.popupMenu || w.onScreenDisplay) continue;
    if (!onCurrentDesktop(w)) continue;
    if (w.visible === false) continue;
    const g = w.frameGeometry;
    result.push([Math.round(g.x), Math.round(g.y), Math.round(g.width), Math.round(g.height)]);
}
"""


def list_objects(limit_rect: QRect | None = None) -> list[InterfaceObject]:
    """
    Снимок видимых окон в порядке наложения снизу вверх.

    Пустой перечень означает, что композитор сведений не предоставляет:
    в этом случае автоматическое выделение не применяется, а ручное
    продолжает работать. Функция блокирующая и вызывается в фоне.
    """
    from backends.wayland import kwin

    try:
        raw = kwin.run_query(kwin.WINDOW_HELPERS + _LIST_WINDOWS % {"pid": os.getpid()})
    except kwin.KWinUnavailable:
        return []
    return parse_windows(raw, limit_rect)


def parse_windows(raw: object, limit_rect: QRect | None = None) -> list[InterfaceObject]:
    """Разбор перечня окон, полученного от композитора."""
    objects: list[InterfaceObject] = []
    if not isinstance(raw, list):
        return objects
    for index, item in enumerate(raw):
        try:
            x, y, width, height = (int(value) for value in item)
        except (TypeError, ValueError):
            continue
        if width < MINIMUM_SIZE or height < MINIMUM_SIZE:
            continue
        rect = QRect(x, y, width, height)
        if limit_rect is not None and not rect.intersects(limit_rect):
            continue
        # Позиция в перечне задаёт порядок наложения: композитор отдаёт
        # окна снизу вверх.
        objects.append(InterfaceObject(rect, 0, index))
    return objects
