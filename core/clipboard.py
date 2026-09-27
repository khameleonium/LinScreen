"""
Копирование изображения в буфер обмена Wayland.

В Wayland буфер обмена принадлежит окну, имеющему фокус клавиатуры:
композитор принимает новое содержимое только от него и лишь в ответ на
действие пользователя. Приложение из трея после закрытия оверлея фокуса
уже не имеет, и копирование средствами Qt может быть молча отклонено.

Поэтому основной путь - утилита wl-copy из пакета wl-clipboard: она
пользуется протоколом управления буфером обмена (data-control), который
не требует фокуса, и сама обслуживает запросы вставки после выхода
приложения. Если утилиты нет, применяется буфер обмена Qt.
"""

from __future__ import annotations

import os
import shutil

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, QProcess
from PySide6.QtGui import QGuiApplication, QImage

from core.runtime import child_environment
from core.workers import run_async

WL_COPY = "wl-copy"


def encode_png(image: QImage) -> bytes:
    """
    Кодирование изображения в PNG.

    Выполняется в фоновом потоке: крупный снимок кодируется заметное
    время. Сжатие минимальное - данные живут только в памяти.
    """
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    # Формат передаётся строкой: значение в байтах библиотека отвергает.
    image.save(buffer, "PNG", 100)
    buffer.close()
    return bytes(data.data())


def find_wl_copy() -> str | None:
    """
    Утилита wl-copy: вложенная в образ либо системная.

    Вложенная копия лежит в каталоге tools рядом с приложением вместе со
    своей библиотекой libwayland-client, см. build.sh.
    """
    from core.runtime import bundle_dir

    base = bundle_dir()
    if base is not None:
        bundled = base / "tools" / WL_COPY
        if bundled.is_file():
            return str(bundled)
    return shutil.which(WL_COPY)


def copy_image(owner: QObject, image: QImage) -> None:
    """Помещение изображения в буфер обмена без блокировки интерфейса."""
    tool = find_wl_copy()
    if tool is None:
        _copy_with_qt(image)
        return
    run_async(owner, encode_png, lambda data: _copy_with_tool(owner, tool, data), None, image)


def _copy_with_qt(image: QImage) -> None:
    """Запасной путь: буфер обмена Qt, работающий при наличии фокуса."""
    clipboard = QGuiApplication.clipboard()
    if clipboard is not None:
        clipboard.setImage(image)


def _copy_with_tool(owner: QObject, tool: str, data: object) -> None:
    """Передача данных утилите wl-copy через её стандартный ввод."""
    if not isinstance(data, bytes):
        return
    process = QProcess(owner)
    environment = process.processEnvironment()
    for name, value in child_environment().items():
        environment.insert(name, value)
    # Вложенная утилита ищет свою библиотеку рядом с собой.
    directory = os.path.dirname(tool)
    if os.path.exists(os.path.join(directory, "libwayland-client.so.0")):
        environment.insert("LD_LIBRARY_PATH", directory)
    process.setProcessEnvironment(environment)
    # По завершении объект процесса удаляется: утилита отделяется от
    # приложения и продолжает обслуживать буфер обмена сама.
    process.finished.connect(process.deleteLater)
    process.setProgram(tool)
    process.setArguments(["--type", "image/png"])
    process.start()
    process.write(data)
    process.closeWriteChannel()
