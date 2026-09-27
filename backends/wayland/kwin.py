"""
Вспомогательные скрипты композитора KWin (KDE Plasma).

Протокол Wayland намеренно не даёт приложению сведений о чужих окнах и не
позволяет располагать свои окна в произвольном месте. Композитор KWin
предоставляет для этого сценарии на JavaScript, загружаемые через шину
D-Bus. Приложение пользуется ими как необязательным улучшением:
    * перечень видимых окон с порядком наложения - для подсветки окна
      под курсором при выделении области;
    * размещение рамки записи и панели управления поверх прочих окон.

Скрипт не может вернуть результат напрямую, поэтому он вызывает метод
службы приложения (core/bus_service.py), передавая данные строкой JSON.

Интерфейс сценариев различается между версиями Plasma 5 и 6, поэтому
тексты скриптов проверяют наличие свойств, а не номер версии. Функции
модуля блокирующие и вызываются только из фоновых потоков.
"""

from __future__ import annotations

import json
import os
import queue
import secrets
import tempfile
from pathlib import Path
from typing import Any

from core import portal
from core.bus_service import expect_report, forget_report
from core.identity import BUS_INTERFACE, BUS_NAME, BUS_PATH

KWIN_SERVICE = "org.kde.KWin"
SCRIPTING_PATH = "/Scripting"
SCRIPTING_INTERFACE = "org.kde.kwin.Scripting"
SCRIPT_INTERFACE = "org.kde.kwin.Script"

# Предельное время ожидания отчёта скрипта. Скрипт выполняется за
# миллисекунды, запас нужен на случай занятости композитора.
REPORT_TIMEOUT_SEC = 3.0


class KWinUnavailable(RuntimeError):
    """Композитор KWin недоступен или отказал в выполнении скрипта."""


def is_available() -> bool:
    """Признак доступности интерфейса сценариев KWin."""
    try:
        portal.call(
            SCRIPTING_PATH,
            SCRIPTING_INTERFACE,
            "isScriptLoaded",
            "s",
            ("linscreen-probe",),
            service=KWIN_SERVICE,
            timeout=2.0,
        )
    except portal.PortalError:
        return False
    return True


def run_script(source: str) -> None:
    """
    Однократное выполнение скрипта KWin.

    Скрипт записывается во временный файл, загружается под уникальным
    именем, запускается и выгружается. Выгрузка обязательна: иначе
    каждый вызов оставлял бы в композиторе ещё один загруженный сценарий.
    """
    plugin = "linscreen_" + secrets.token_hex(6)
    handle, name = tempfile.mkstemp(prefix="linscreen-kwin-", suffix=".js")
    path = Path(name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(source)
        try:
            (script_id,) = portal.call(
                SCRIPTING_PATH,
                SCRIPTING_INTERFACE,
                "loadScript",
                "ss",
                (str(path), plugin),
                service=KWIN_SERVICE,
            )
        except portal.PortalError as error:
            raise KWinUnavailable(str(error))
        if int(script_id) < 0:
            raise KWinUnavailable("loadScript")
        _start_script(int(script_id))
    finally:
        try:
            portal.call(
                SCRIPTING_PATH,
                SCRIPTING_INTERFACE,
                "unloadScript",
                "s",
                (plugin,),
                service=KWIN_SERVICE,
            )
        except portal.PortalError:
            pass
        path.unlink(missing_ok=True)


def _start_script(script_id: int) -> None:
    """
    Запуск загруженного скрипта.

    Путь объекта скрипта различается: Plasma 5 публикует его в корне
    (/0), Plasma 6 - внутри /Scripting (/Scripting/Script0).
    """
    last_error: Exception | None = None
    for path in (f"/Scripting/Script{script_id}", f"/{script_id}"):
        try:
            portal.call(path, SCRIPT_INTERFACE, "run", service=KWIN_SERVICE)
            return
        except portal.PortalError as error:
            last_error = error
    raise KWinUnavailable(str(last_error))


def run_query(body: str) -> Any:
    """
    Выполнение скрипта, возвращающего данные.

    Текст body обязан сформировать переменную result; она передаётся
    службе приложения строкой JSON. Возвращается разобранное значение.
    """
    token = secrets.token_hex(8)
    waiter = expect_report(token)
    source = (
        "(function () {\n"
        "let result = null;\n"
        f"{body}\n"
        f"callDBus({json.dumps(BUS_NAME)}, {json.dumps(BUS_PATH)}, "
        f"{json.dumps(BUS_INTERFACE)}, \"ReportWindows\", {json.dumps(token)}, "
        "JSON.stringify(result));\n"
        "})();\n"
    )
    try:
        run_script(source)
        try:
            payload = waiter.get(timeout=REPORT_TIMEOUT_SEC)
        except queue.Empty:
            raise KWinUnavailable("timeout")
    finally:
        forget_report(token)
    try:
        return json.loads(payload)
    except ValueError:
        raise KWinUnavailable("json")


# Общая часть скриптов: перечень окон в порядке наложения снизу вверх
# и проверка принадлежности окна текущему рабочему столу.
WINDOW_HELPERS = """
function stacking() {
    if (workspace.stackingOrder !== undefined) return workspace.stackingOrder;
    if (workspace.windowList !== undefined) return workspace.windowList();
    return workspace.clientList();
}
function onCurrentDesktop(w) {
    if (w.onAllDesktops) return true;
    const current = workspace.currentVirtualDesktop || workspace.currentDesktop;
    if (w.desktops !== undefined && current && current.id !== undefined) {
        for (let i = 0; i < w.desktops.length; ++i) {
            if (w.desktops[i].id === current.id) return true;
        }
        return w.desktops.length === 0;
    }
    if (typeof w.desktop === "number") return w.desktop === workspace.currentDesktop;
    return true;
}
"""


# Скрипт размещения окна приложения. Окно находится по уникальному
# началу заголовка и получает заданную геометрию, признак удержания поверх
# прочих окон и исключение из панели задач и переключателя окон.
_PLACE_WINDOW = """
const caption = %(caption)s;
const target = %(rect)s;
result = false;
const windows = stacking();
for (let i = 0; i < windows.length; ++i) {
    const w = windows[i];
    // Qt дописывает к заголовку окна название приложения, поэтому
    // сравнивается начало заголовка.
    if (String(w.caption).indexOf(caption) !== 0) continue;
    w.keepAbove = true;
    w.skipTaskbar = true;
    w.skipSwitcher = true;
    w.skipPager = true;
    if (target !== null) {
        const g = w.frameGeometry;
        g.x = target[0];
        g.y = target[1];
        if (target.length > 2) {
            g.width = target[2];
            g.height = target[3];
        }
        w.frameGeometry = g;
    }
    result = true;
}
"""


def place_window(caption: str, rect: tuple[int, ...] | None) -> bool:
    """
    Размещение окна приложения средствами KWin.

    Аргумент rect задаёт положение (x, y) либо положение с размером
    (x, y, ширина, высота) в логических координатах рабочего стола.
    Возвращается признак того, что окно найдено.
    """
    body = WINDOW_HELPERS + _PLACE_WINDOW % {
        "caption": json.dumps(caption),
        "rect": json.dumps(list(rect)) if rect is not None else "null",
    }
    return bool(run_query(body))
