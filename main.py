"""
Точка входа приложения LinScreen.

Приложение работает в системном трее и не имеет главного окна, поэтому
закрытие любого окна не должно завершать работу процесса.

В собранном виде терминал отсутствует, и необработанная ошибка осталась бы
незамеченной. Поэтому такие ошибки записываются в файл журнала и, по
возможности, показываются пользователю окном сообщения.
"""

from __future__ import annotations

import os
import signal
import sys
import traceback
from datetime import datetime
from pathlib import Path
from types import TracebackType

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from app import LinScreenApplication
from ui.tray import TrayIcon

APPLICATION_VERSION = "1.0"


def crash_log_path() -> Path:
    """Путь файла с записями о необработанных ошибках."""
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "linscreen" / "crash.log"


def record_crash(
    kind: type[BaseException],
    value: BaseException,
    trace: TracebackType | None,
) -> None:
    """
    Запись необработанной ошибки в файл и показ сообщения пользователю.

    Прерывание с клавиатуры обрабатывается отдельно: это штатный способ
    завершения работы, а не сбой.
    """
    if issubclass(kind, KeyboardInterrupt):
        QApplication.quit()
        return

    report = "".join(traceback.format_exception(kind, value, trace))
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        path = crash_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Записи добавляются в конец: история сбоев сохраняется целиком.
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n===== {stamp} =====\n{report}")
    except OSError:
        # Невозможность записи не должна порождать вторую ошибку.
        path = crash_log_path()

    # Сообщение показывается только при работающей оконной подсистеме.
    if QApplication.instance() is not None:
        QMessageBox.critical(
            None,
            "LinScreen",
            f"Произошла ошибка:\n{value}\n\nПодробности записаны в файл:\n{path}",
        )


def self_test() -> int:
    """
    Проверка работоспособности установленных составных частей.

    Вызывается ключом --check и предназначена для быстрой диагностики:
    выводит найденный FFmpeg, доступные кодеки, звуковые источники и тип
    сессии, после чего завершает работу.
    """
    from capture.audio import list_audio_devices
    from core.runtime import bundle_dir, is_frozen
    from core.session import detect_session
    from encoder.ffmpeg import FFmpegNotFoundError, find_ffmpeg, probe_capabilities

    print(f"LinScreen {APPLICATION_VERSION}")
    print(f"Режим запуска: {'собранный файл' if is_frozen() else 'исходные тексты'}")
    if bundle_dir() is not None:
        print(f"Каталог вложений: {bundle_dir()}")

    session = detect_session()
    print(f"Сессия: {session.session_type.label}, окружение: {session.desktop}")

    try:
        binary = find_ffmpeg()
    except FFmpegNotFoundError as error:
        print(f"FFmpeg: НЕ НАЙДЕН — {error}")
        return 1

    print(f"FFmpeg: {binary}")
    capabilities = probe_capabilities(binary)
    print(f"Версия FFmpeg: {capabilities.version}")
    codecs = [
        name
        for name in ("libx264", "libx265", "libsvtav1", "libvpx-vp9", "ffv1", "prores_ks")
        if capabilities.has_encoder(name)
    ]
    print(f"Видеокодеки: {', '.join(codecs) or 'отсутствуют'}")
    print(f"Захват экрана X11: {'да' if capabilities.can_capture_x11 else 'нет'}")
    print(f"Захват звука PulseAudio: {'да' if capabilities.can_capture_pulse else 'НЕТ'}")
    print(f"Палитра GIF: {'да' if capabilities.can_build_gif_palette else 'нет'}")
    print(f"Захват PipeWire (Wayland): {'да' if capabilities.can_capture_pipewire else 'нет'}")

    from encoder.images import is_avif_available

    print(f"Сохранение в AVIF: {'да' if is_avif_available() else 'нет'}")

    devices = list_audio_devices()
    print(f"Звуковых источников: {len(devices)}")
    for device in devices:
        kind = "монитор" if device.is_monitor else "вход"
        print(f"  [{kind}] {device.description}")

    problems = capabilities.missing_essentials()

    # Проверка составных частей, попадающих в сборку: их отсутствие
    # проявилось бы только при попытке воспользоваться соответствующей
    # возможностью, что заметно позже момента запуска.
    for module, purpose in (
        ("pynput", "глобальные клавиши"),
        ("Xlib", "определение активного окна"),
        ("PIL", "сохранение изображений"),
        ("PySide6.QtDBus", "порталы рабочего стола"),
    ):
        try:
            __import__(module)
            print(f"Модуль {module}: есть ({purpose})")
        except ImportError:
            problems.append(f"Недоступен модуль {module}: не работают {purpose}.")

    # Проверка оконной подсистемы: создание приложения и отрисовка значка.
    try:
        from PySide6.QtWidgets import QApplication, QSystemTrayIcon

        probe = QApplication.instance() or QApplication([])
        from ui.tray import render_tray_icon
        from encoder.process import RecorderState

        icon = render_tray_icon(RecorderState.IDLE)
        print(f"Графическая подсистема: работает, значок {icon.availableSizes()[0].width()} px")
        available = QSystemTrayIcon.isSystemTrayAvailable()
        print(f"Системный трей: {'доступен' if available else 'НЕДОСТУПЕН'}")
        del probe
    except Exception as error:  # noqa: BLE001 - причина выводится пользователю
        problems.append(f"Оконная подсистема недоступна: {error}")

    for problem in problems:
        print(f"ОГРАНИЧЕНИЕ: {problem}")
    print("Проверка завершена: " + ("есть ограничения" if problems else "всё в порядке"))
    return 0


def main() -> int:
    """Запуск приложения и вход в цикл обработки событий."""
    if "--check" in sys.argv:
        return self_test()
    if "--version" in sys.argv:
        print(f"LinScreen {APPLICATION_VERSION}")
        return 0

    sys.excepthook = record_crash

    application = QApplication(sys.argv)
    application.setApplicationName("LinScreen")
    application.setApplicationDisplayName("LinScreen")
    application.setOrganizationName("LinScreen")
    # Приложение живёт в трее: закрытие редактора или настроек работу
    # процесса не прекращает.
    application.setQuitOnLastWindowClosed(False)

    if not TrayIcon.is_available():
        QMessageBox.critical(
            None,
            "LinScreen",
            "Системный трей недоступен в текущем окружении рабочего стола.",
        )
        return 1

    controller = LinScreenApplication()
    controller.start()

    # Прерывание с терминала обрабатывается штатно: таймер периодически
    # возвращает управление интерпретатору, позволяя сработать обработчику.
    signal.signal(signal.SIGINT, lambda *_: controller.quit())
    heartbeat = QTimer()
    heartbeat.start(500)
    heartbeat.timeout.connect(lambda: None)

    return application.exec()


if __name__ == "__main__":
    sys.exit(main())
