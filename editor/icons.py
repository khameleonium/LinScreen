"""
Генерация векторных пиктограмм для инструментов редактора.

Все значки отрисовываются программно через примитивы QPainter на прозрачном
растре QPixmap. Это обеспечивает масштабируемость, независимость от внешних
файлов ресурсов и стабильную работу в упакованных сборках PyInstaller и AppImage.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QGuiApplication,
    QIcon,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
)

from editor.tools import Tool

# Базовый размер отрисовки пиктограмм.
DEFAULT_ICON_SIZE = 22
# Нейтральный цвет для тёмных тем оформления.
DARK_THEME_ICON_COLOR = QColor(220, 224, 230)
# Контрастный цвет для светлых тем оформления.
LIGHT_THEME_ICON_COLOR = QColor(44, 48, 56)

_ICON_CACHE: dict[tuple[Tool, int, int], QIcon] = {}


def _resolve_default_color() -> QColor:
    """Определение цвета пиктограмм по палитре активной темы окружения."""
    app = QGuiApplication.instance()
    if app is not None and isinstance(app, QGuiApplication):
        btn_bg = app.palette().color(QPalette.ColorRole.Button)
        if btn_bg.lightness() > 128:
            return LIGHT_THEME_ICON_COLOR
    return DARK_THEME_ICON_COLOR


def render_tool_icon(
    tool: Tool,
    size: int = DEFAULT_ICON_SIZE,
    color: QColor | None = None,
) -> QIcon:
    """
    Получение векторного значка инструмента заданного размера и цвета.

    Значки кэшируются по комбинации параметров для предотвращения повторной
    отрисовки при частых вызовах.
    """
    target_color = color if color is not None else _resolve_default_color()
    cache_key = (tool, size, target_color.rgba())
    cached = _ICON_CACHE.get(cache_key)
    if cached is not None:
        return cached

    pixmap = _draw_tool_pixmap(tool, size, target_color)
    icon = QIcon(pixmap)
    _ICON_CACHE[cache_key] = icon
    return icon


def _draw_tool_pixmap(tool: Tool, size: int, color: QColor) -> QPixmap:
    """Отрисовка пиктограммы выбранного инструмента на растре с альфа-каналом."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    scale = size / 24.0
    pen_width = max(1.5, 2.0 * scale)
    pen = QPen(color, pen_width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)

    if tool is Tool.ARROW:
        _draw_arrow(painter, scale)
    elif tool is Tool.RECT:
        _draw_rect(painter, scale)
    elif tool is Tool.PENCIL:
        _draw_pencil(painter, scale)
    elif tool is Tool.MARKER:
        _draw_marker(painter, scale, color)
    elif tool is Tool.BLUR:
        _draw_blur(painter, scale, color)
    elif tool is Tool.TEXT:
        _draw_text(painter, scale)
    elif tool is Tool.STEP:
        _draw_step(painter, scale)
    elif tool is Tool.CROP:
        _draw_crop(painter, scale)

    painter.end()
    return pixmap


def _draw_arrow(painter: QPainter, scale: float) -> None:
    """Диагональная стрелка с острием в правый верхний угол."""
    painter.drawLine(
        QPointF(5.0 * scale, 19.0 * scale),
        QPointF(19.0 * scale, 5.0 * scale),
    )
    painter.drawLine(
        QPointF(10.5 * scale, 5.0 * scale),
        QPointF(19.0 * scale, 5.0 * scale),
    )
    painter.drawLine(
        QPointF(19.0 * scale, 5.0 * scale),
        QPointF(19.0 * scale, 13.5 * scale),
    )


def _draw_rect(painter: QPainter, scale: float) -> None:
    """Прямоугольная рамка с закруглёнными углами."""
    painter.drawRoundedRect(
        QRectF(4.0 * scale, 4.0 * scale, 16.0 * scale, 16.0 * scale),
        2.5 * scale,
        2.5 * scale,
    )


def _draw_pencil(painter: QPainter, scale: float) -> None:
    """Карандаш, направленный острием в нижний левый угол."""
    # Контур корпуса карандаша
    painter.drawLine(QPointF(5.0 * scale, 15.0 * scale), QPointF(15.0 * scale, 5.0 * scale))
    painter.drawLine(QPointF(9.0 * scale, 19.0 * scale), QPointF(19.0 * scale, 9.0 * scale))
    painter.drawLine(QPointF(15.0 * scale, 5.0 * scale), QPointF(19.0 * scale, 9.0 * scale))
    # Грифель
    painter.drawLine(QPointF(5.0 * scale, 15.0 * scale), QPointF(4.0 * scale, 20.0 * scale))
    painter.drawLine(QPointF(9.0 * scale, 19.0 * scale), QPointF(4.0 * scale, 20.0 * scale))


def _draw_marker(painter: QPainter, scale: float, color: QColor) -> None:
    """Маркер-хайлайтер со скошенным наконечником."""
    painter.drawLine(QPointF(6.0 * scale, 18.0 * scale), QPointF(13.0 * scale, 11.0 * scale))
    thick_pen = QPen(color, 4.0 * scale)
    thick_pen.setCapStyle(Qt.PenCapStyle.FlatCap)
    painter.setPen(thick_pen)
    painter.drawLine(QPointF(13.0 * scale, 11.0 * scale), QPointF(17.0 * scale, 7.0 * scale))
    # Наконечник пера
    thin_pen = QPen(color, max(1.5, 2.0 * scale))
    thin_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(thin_pen)
    painter.drawLine(QPointF(4.0 * scale, 20.0 * scale), QPointF(6.0 * scale, 18.0 * scale))


def _draw_blur(painter: QPainter, scale: float, color: QColor) -> None:
    """Мозаичная сетка пикселизации."""
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(color))
    # Сетка квадратов 3x3 в шахматном порядке
    for row in range(3):
        for col in range(3):
            if (row + col) % 2 == 0:
                painter.drawRoundedRect(
                    QRectF(
                        (4.5 + col * 5.5) * scale,
                        (4.5 + row * 5.5) * scale,
                        4.0 * scale,
                        4.0 * scale,
                    ),
                    1.0 * scale,
                    1.0 * scale,
                )


def _draw_text(painter: QPainter, scale: float) -> None:
    """Буква Т с засечками."""
    # Верхняя перекладина
    painter.drawLine(QPointF(4.0 * scale, 5.0 * scale), QPointF(20.0 * scale, 5.0 * scale))
    # Вертикальная стойка
    painter.drawLine(QPointF(12.0 * scale, 5.0 * scale), QPointF(12.0 * scale, 19.0 * scale))
    # Нижнее основание
    painter.drawLine(QPointF(8.0 * scale, 19.0 * scale), QPointF(16.0 * scale, 19.0 * scale))


def _draw_step(painter: QPainter, scale: float) -> None:
    """Кружок нумератора с цифрой 1 внутри."""
    painter.drawEllipse(QPointF(12.0 * scale, 12.0 * scale), 8.5 * scale, 8.5 * scale)
    # Цифра 1
    painter.drawLine(QPointF(10.0 * scale, 9.5 * scale), QPointF(12.0 * scale, 7.5 * scale))
    painter.drawLine(QPointF(12.0 * scale, 7.5 * scale), QPointF(12.0 * scale, 16.5 * scale))
    painter.drawLine(QPointF(9.5 * scale, 16.5 * scale), QPointF(14.5 * scale, 16.5 * scale))


def _draw_crop(painter: QPainter, scale: float) -> None:
    """Перекрещивающиеся уголки инструмента обрезки."""
    # Верхний левый уголок
    painter.drawLine(QPointF(3.0 * scale, 7.5 * scale), QPointF(16.5 * scale, 7.5 * scale))
    painter.drawLine(QPointF(7.5 * scale, 3.0 * scale), QPointF(7.5 * scale, 16.5 * scale))
    # Нижний правый уголок
    painter.drawLine(QPointF(7.5 * scale, 16.5 * scale), QPointF(21.0 * scale, 16.5 * scale))
    painter.drawLine(QPointF(16.5 * scale, 7.5 * scale), QPointF(16.5 * scale, 21.0 * scale))
