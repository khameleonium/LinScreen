"""
Сохранение снимков экрана в поддерживаемых растровых форматах.

Запись выполняется средствами Qt для форматов PNG, JPEG, WEBP и BMP и
средствами библиотеки Pillow для AVIF, поддержка которого в Qt отсутствует.
Операция обращается к диску, поэтому вызывается через core.workers.run_async().
"""

from __future__ import annotations

from core.i18n import tr

from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QGuiApplication, QImage, QImageWriter

from core.config import ImageSettings

# Соответствие названия формата и расширения файла.
FORMAT_EXTENSIONS: dict[str, str] = {
    "png": "png",
    "jpeg": "jpg",
    "webp": "webp",
    "avif": "avif",
    "bmp": "bmp",
}

# Названия форматов для выпадающего списка настроек.
FORMAT_TITLES: dict[str, str] = {
    "png": "PNG (без потерь)",
    "jpeg": "JPEG (с потерями)",
    "webp": "WEBP (с потерями или без)",
    "avif": "AVIF (современный, компактный)",
    "bmp": "BMP (без сжатия)",
}


class ImageSaveError(RuntimeError):
    """Сохранение изображения завершилось неудачей."""


def extension_for(image_format: str) -> str:
    """Расширение файла для указанного формата."""
    return FORMAT_EXTENSIONS.get(image_format.lower(), "png")


def is_avif_available() -> bool:
    """Проверка наличия поддержки AVIF в установленной сборке Pillow."""
    try:
        import pillow_avif  # noqa: F401 - импорт регистрирует кодек в Pillow
        from PIL import Image

        return "AVIF" in Image.SAVE
    except ImportError:
        return False


def qimage_to_pillow(image: QImage):  # type: ignore[no-untyped-def]
    """
    Преобразование изображения Qt в объект Pillow.

    Передача ведётся через сырой буфер точек без промежуточного файла:
    формат RGBA8888 совпадает с внутренним представлением Pillow.
    """
    from PIL import Image

    converted = image.convertToFormat(QImage.Format.Format_RGBA8888)
    width, height = converted.width(), converted.height()
    # Строка изображения в Qt выровнена, поэтому передаётся её реальный шаг.
    buffer = bytes(converted.constBits())
    return Image.frombuffer(
        "RGBA", (width, height), buffer, "raw", "RGBA", converted.bytesPerLine(), 1
    )


def _save_with_qt(image: QImage, path: Path, image_format: str, settings: ImageSettings) -> None:
    """Сохранение средствами Qt с учётом параметров качества."""
    writer = QImageWriter(str(path), image_format.upper().encode("ascii"))

    if image_format == "png":
        # Обработчик PNG в Qt принимает степень сжатия в процентах от нуля
        # до ста, тогда как в настройках хранится привычный уровень zlib
        # от нуля до девяти, поэтому выполняется пересчёт шкалы.
        # Проверено опытным путём: значение ноль даёт файл без сжатия.
        level = max(0, min(9, settings.png_compression))
        writer.setCompression(round(level * 100 / 9))
    elif image_format == "jpeg":
        writer.setQuality(max(1, min(100, settings.jpeg_quality)))
    elif image_format == "webp":
        # Значение сто включает режим без потерь в обработчике WebP.
        quality = 100 if settings.webp_lossless else max(1, min(99, settings.webp_quality))
        writer.setQuality(quality)

    if not writer.write(image):
        raise ImageSaveError(writer.errorString() or tr("Не удалось записать файл"))


def _save_avif(image: QImage, path: Path, settings: ImageSettings) -> None:
    """Сохранение в формате AVIF средствами Pillow."""
    if not is_avif_available():
        raise ImageSaveError(tr("Поддержка AVIF недоступна: требуется пакет pillow-avif-plugin"))
    picture = qimage_to_pillow(image)
    # Прозрачность сохраняется: формат поддерживает альфа-канал.
    picture.save(path, format="AVIF", quality=max(1, min(100, settings.avif_quality)))


def save_image(image: QImage, path: Path, settings: ImageSettings) -> Path:
    """
    Сохранение снимка в файл согласно настройкам.

    Каталог создаётся при необходимости; возвращается фактический путь
    сохранённого файла.
    """
    if image.isNull():
        raise ImageSaveError(tr("Пустое изображение сохранению не подлежит"))

    path.parent.mkdir(parents=True, exist_ok=True)
    image_format = settings.image_format.lower()

    if image_format == "avif":
        _save_avif(image, path, settings)
    else:
        _save_with_qt(image, path, image_format, settings)

    if not path.is_file():
        raise ImageSaveError(tr("Файл не был создан"))
    return path


def image_to_png_bytes(image: QImage) -> bytes:
    """Кодирование изображения в PNG в памяти, без обращения к диску."""
    payload = QByteArray()
    buffer = QBuffer(payload)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    try:
        # Подпись метода в стабах PySide6 не описывает запись в QIODevice.
        if not image.save(buffer, "PNG"):  # type: ignore[call-overload]
            raise ImageSaveError(tr("Не удалось закодировать изображение"))
    finally:
        buffer.close()
    return bytes(payload.data())


def copy_image_to_clipboard(image: QImage) -> None:
    """
    Помещение изображения в буфер обмена.

    Операция выполняется только в потоке интерфейса: буфер обмена является
    частью оконной подсистемы и из рабочих потоков недоступен.
    """
    clipboard = QGuiApplication.clipboard()
    if clipboard is not None:
        clipboard.setImage(image)
