"""
Обвязка для выполнения блокирующих операций вне потока интерфейса.

Часть операций приложения обращается к внешним утилитам и файловой системе:
перечисление звуковых устройств, опрос возможностей FFmpeg, сохранение
крупных изображений. Прямой вызов таких функций из обработчика сигнала
подвешивает окно, поэтому они выполняются в пуле потоков, а результат
доставляется сигналом в поток, создавший объект.
"""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, SignalInstance


class BackgroundCall(QObject):
    """
    Однократный вызов функции в фоновом потоке.

    Объект остаётся живым до получения результата за счёт привязки к
    родителю, переданному при создании: преждевременное разрушение
    привело бы к потере сигнала.
    """

    # Успешное завершение: передаётся возвращённое функцией значение.
    done = Signal(object)
    # Текст исключения, возникшего при выполнении.
    error = Signal(str)

    def run(self, function: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        """Постановка вызова в глобальный пул потоков."""
        QThreadPool.globalInstance().start(_CallTask(self, function, args, kwargs))


class _CallTask(QRunnable):
    """Задача пула потоков, выполняющая переданную функцию."""

    def __init__(
        self,
        owner: BackgroundCall,
        function: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> None:
        super().__init__()
        self._owner = owner
        self._function = function
        self._args = args
        self._kwargs = kwargs

    def run(self) -> None:
        """Точка входа рабочего потока."""
        try:
            result = self._function(*self._args, **self._kwargs)
        except Exception as error:  # noqa: BLE001 - причина уходит в интерфейс
            _emit(self._owner.error, str(error))
            return
        _emit(self._owner.done, result)


def _emit(signal: SignalInstance, payload: Any) -> None:
    """Испускание сигнала с защитой от уже разрушенного владельца."""
    try:
        signal.emit(payload)
    except RuntimeError:
        # Владелец удалён, пока задача выполнялась: результат никому не нужен.
        pass


def run_async(
    parent: QObject,
    function: Callable[..., Any],
    on_done: Callable[[Any], None],
    on_error: Callable[[str], None] | None = None,
    *args: Any,
    **kwargs: Any,
) -> BackgroundCall:
    """
    Удобная обёртка запуска функции в фоне.

    Владелец вызова привязывается к переданному родителю, поэтому при
    закрытии окна незавершённый вызов не приведёт к обращению к
    разрушенным объектам.
    """
    call = BackgroundCall(parent)
    call.done.connect(on_done)
    if on_error is not None:
        call.error.connect(on_error)
    call.run(function, *args, **kwargs)
    return call
