"""
Точка входа приложения LinScreen.

Приложение работает в системном трее и не имеет главного окна, поэтому
закрытие любого окна не должно завершать работу процесса.

В собранном виде терминал отсутствует, и необработанная ошибка осталась бы
незамеченной. Поэтому такие ошибки записываются в файл журнала и, по
возможности, показываются пользователю окном сообщения.
"""

from __future__ import annotations

from core.i18n import tr

import fcntl
import os
import signal
import socket
import sys
import traceback
from datetime import datetime
from pathlib import Path
from types import TracebackType

from PySide6.QtCore import QSocketNotifier
from PySide6.QtWidgets import QApplication, QMessageBox

from app import LinScreenApplication
from ui.tray import TrayIcon

APPLICATION_VERSION = "1.3"

# Число уже показанных сообщений об ошибке. Окно показывается только для
# первой: повторяющийся сбой в обработчике событий иначе завалил бы экран
# диалогами и сделал бы работу невозможной.
_reported_errors = 0

# Объекты обработки сигналов операционной системы. Ссылки удерживаются на
# время работы приложения: уведомитель и сокеты не должны быть удалены.
_signal_objects: list[object] = []


def lock_file_path() -> Path:
    """Путь файла блокировки, удерживаемого работающим приложением."""
    base = os.environ.get("XDG_RUNTIME_DIR") or os.environ.get("XDG_CACHE_HOME")
    if not base:
        base = str(Path.home() / ".cache")
    return Path(base) / "linscreen.lock"


def acquire_single_instance() -> object | None:
    """
    Захват признака единственного запущенного приложения.

    Второй запущенный экземпляр перехватывал бы те же горячие клавиши и
    показывал бы собственный оверлей поверх первого, из-за чего каждое
    действие выполнялось бы дважды. Возвращается удерживаемый файл либо
    пустое значение, если приложение уже работает.

    Блокировка снимается ядром при завершении процесса, поэтому
    аварийное завершение не оставляет её висящей.
    """
    path = lock_file_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("w")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return None

    try:
        handle.write(f"{os.getpid()}\n")
        handle.flush()
    except OSError:
        # Запись номера процесса носит справочный характер.
        pass
    return handle


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

    Ошибка внутри обработчика события не прекращает работу приложения:
    цикл событий продолжается. Поэтому запись ведётся всегда, а окно
    показывается только для первой ошибки за запуск - иначе повторяющийся
    сбой сделал бы работу невозможной.

    Прерывание с клавиатуры обрабатывается отдельно: это штатный способ
    завершения работы, а не сбой.
    """
    global _reported_errors

    if issubclass(kind, KeyboardInterrupt):
        QApplication.quit()
        return

    path = crash_log_path()
    report = "".join(traceback.format_exception(kind, value, trace))
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Записи добавляются в конец: история сбоев сохраняется целиком.
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n===== {stamp} =====\n{report}")
    except OSError:
        # Невозможность записи не должна порождать вторую ошибку.
        pass

    _reported_errors += 1
    if _reported_errors > 1:
        # Последующие ошибки попадают только в файл.
        return

    if QApplication.instance() is not None:
        try:
            QMessageBox.critical(
                None,
                "LinScreen",
                tr(
                    "Произошла ошибка:\n{0}\n\nРабота продолжается. Подробности записаны в "
                    "файл:\n{1}\n\nДальнейшие ошибки будут записываться без показа этого окна."
                ).format(value, path),
            )
        except Exception:  # noqa: BLE001 - обработчик ошибок не вправе падать
            pass


def install_signal_handling(controller: LinScreenApplication) -> None:
    """
    Штатное завершение по сигналам операционной системы.

    Интерпретатор обрабатывает сигналы только при исполнении своего кода,
    а приложение почти всё время проводит в цикле событий Qt. Вместо
    периодического пробуждения, расходующего заряд и процессорное время,
    применяется пара сокетов: интерпретатор записывает в неё номер
    сигнала, а Qt пробуждается на готовности к чтению.

    Обрабатываются прерывание с терминала и запрос завершения при выходе
    из сеанса: в обоих случаях незаконченная запись корректно завершается.
    """
    receiver, sender = socket.socketpair()
    receiver.setblocking(False)
    sender.setblocking(False)
    signal.set_wakeup_fd(sender.fileno())

    notifier = QSocketNotifier(receiver.fileno(), QSocketNotifier.Type.Read)

    def on_signal() -> None:
        """Обработка полученного сигнала в потоке интерфейса."""
        try:
            receiver.recv(1024)
        except OSError:
            pass
        controller.quit()

    notifier.activated.connect(lambda *_: on_signal())

    # Обработчики оставляются пустыми: действие выполняет уведомитель,
    # которому сигнал доставляется через пару сокетов.
    for number in (signal.SIGINT, signal.SIGTERM):
        signal.signal(number, lambda *_: None)

    _signal_objects.extend((receiver, sender, notifier))


def self_test() -> int:
    """
    Проверка работоспособности установленных составных частей.

    Вызывается ключом --check и предназначена для быстрой диагностики:
    выводит найденный FFmpeg, доступные кодеки, звуковые источники и тип
    сессии, после чего завершает работу.
    """
    from capture.audio import list_audio_devices
    from core.config import ConfigManager
    from core.i18n import set_language
    from core.runtime import bundle_dir, is_frozen
    from core.session import detect_session
    from encoder.ffmpeg import FFmpegNotFoundError, find_ffmpeg, probe_capabilities

    # Язык берётся из настроек: вывод проверки читает тот же человек,
    # который пользуется приложением.
    configuration = ConfigManager()
    configuration.load()
    set_language(configuration.settings.general.language)

    print(f"LinScreen {APPLICATION_VERSION}")
    print(
        tr("Режим запуска: {0}").format(
            tr("собранный файл") if is_frozen() else tr("исходные тексты")
        )
    )
    if bundle_dir() is not None:
        print(tr("Каталог вложений: {0}").format(bundle_dir()))

    session = detect_session()
    print(tr("Сессия: {0}, окружение: {1}").format(session.session_type.label, session.desktop))

    try:
        binary = find_ffmpeg()
    except FFmpegNotFoundError as error:
        print(tr("FFmpeg: НЕ НАЙДЕН — {0}").format(error))
        return 1

    print(f"FFmpeg: {binary}")
    capabilities = probe_capabilities(binary)
    print(tr("Версия FFmpeg: {0}").format(capabilities.version))
    codecs = [
        name
        for name in ("libx264", "libx265", "libsvtav1", "libvpx-vp9", "ffv1", "prores_ks")
        if capabilities.has_encoder(name)
    ]
    print(tr("Видеокодеки: {0}").format(", ".join(codecs) or tr("отсутствуют")))
    print(
        tr("Захват экрана X11: {0}").format(tr("да") if capabilities.can_capture_x11 else tr("нет"))
    )
    print(
        tr("Захват звука PulseAudio: {0}").format(
            tr("да") if capabilities.can_capture_pulse else tr("НЕТ")
        )
    )
    print(
        tr("Палитра GIF: {0}").format(tr("да") if capabilities.can_build_gif_palette else tr("нет"))
    )
    print(
        tr("Захват PipeWire (Wayland): {0}").format(
            tr("да") if capabilities.can_capture_pipewire else tr("нет")
        )
    )

    from encoder.images import is_avif_available

    print(tr("Сохранение в AVIF: {0}").format(tr("да") if is_avif_available() else tr("нет")))

    devices = list_audio_devices()
    print(tr("Звуковых источников: {0}").format(len(devices)))
    for device in devices:
        kind = tr("монитор") if device.is_monitor else tr("вход")
        print(f"  [{kind}] {device.description}")

    problems = capabilities.missing_essentials()

    # Проверка составных частей, попадающих в сборку: их отсутствие
    # проявилось бы только при попытке воспользоваться соответствующей
    # возможностью, что заметно позже момента запуска.
    for module, purpose in (
        ("pynput", tr("глобальные клавиши")),
        ("Xlib", tr("определение активного окна")),
        ("PIL", tr("сохранение изображений")),
        ("PySide6.QtDBus", tr("порталы рабочего стола")),
    ):
        try:
            __import__(module)
            print(tr("Модуль {0}: есть ({1})").format(module, purpose))
        except ImportError:
            problems.append(tr("Недоступен модуль {0}: не работают {1}.").format(module, purpose))

    # Проверка оконной подсистемы: создание приложения и отрисовка значка.
    try:
        from PySide6.QtWidgets import QApplication, QSystemTrayIcon

        probe = QApplication.instance() or QApplication([])
        from ui.tray import render_tray_icon
        from encoder.process import RecorderState

        icon = render_tray_icon(RecorderState.IDLE)
        print(
            tr("Графическая подсистема: работает, значок {0} px").format(
                icon.availableSizes()[0].width()
            )
        )
        available = QSystemTrayIcon.isSystemTrayAvailable()
        print(tr("Системный трей: {0}").format(tr("доступен") if available else tr("НЕДОСТУПЕН")))
        del probe
    except Exception as error:  # noqa: BLE001 - причина выводится пользователю
        problems.append(tr("Оконная подсистема недоступна: {0}").format(error))

    for problem in problems:
        print(tr("ОГРАНИЧЕНИЕ: {0}").format(problem))
    print(
        tr("Проверка завершена: ") + (tr("есть ограничения") if problems else tr("всё в порядке"))
    )
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
            tr("Системный трей недоступен в текущем окружении рабочего стола."),
        )
        return 1

    # Ссылка на файл блокировки удерживается до конца работы: закрытие
    # файла сняло бы признак единственного экземпляра.
    instance_lock = acquire_single_instance()
    if instance_lock is None:
        # Окно с сообщением здесь неуместно: запуск мог быть выполнен из
        # меню рабочего стола, и нажимать кнопку будет некому. Работающий
        # экземпляр уже виден значком в системном трее.
        print(
            tr("Приложение уже запущено: его значок находится в системном трее."),
            file=sys.stderr,
        )
        return 0
    _signal_objects.append(instance_lock)

    controller = LinScreenApplication()
    controller.start()
    install_signal_handling(controller)

    return application.exec()


if __name__ == "__main__":
    sys.exit(main())
