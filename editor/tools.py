"""
Перечень инструментов редактора аннотаций и их параметры по умолчанию."""

from __future__ import annotations

from core.i18n import tr

from dataclasses import dataclass
from enum import Enum

from PySide6.QtGui import QColor


class Tool(str, Enum):
    """Инструменты рисования поверх снимка."""

    ARROW = "arrow"
    RECT = "rect"
    PENCIL = "pencil"
    MARKER = "marker"
    BLUR = "blur"
    TEXT = "text"
    STEP = "step"

    @property
    def label(self) -> str:
        """Название инструмента для панели редактора."""
        return {
            Tool.ARROW: tr("Стрелка"),
            Tool.RECT: tr("Рамка"),
            Tool.PENCIL: tr("Карандаш"),
            Tool.MARKER: tr("Маркер"),
            Tool.BLUR: tr("Размытие"),
            Tool.TEXT: tr("Текст"),
            Tool.STEP: tr("Нумератор"),
        }[self]

    @property
    def hint(self) -> str:
        """Подсказка о способе применения инструмента."""
        return {
            Tool.ARROW: tr("Протянуть от начала к цели"),
            Tool.RECT: tr("Протянуть по диагонали"),
            Tool.PENCIL: tr("Рисовать с зажатой кнопкой"),
            Tool.MARKER: tr("Полупрозрачная подсветка"),
            Tool.BLUR: tr("Выделить область для скрытия"),
            Tool.TEXT: tr("Щелчок и ввод текста"),
            Tool.STEP: tr("Щелчок ставит очередной номер"),
        }[self]


@dataclass
class ToolSettings:
    """Текущие параметры рисования, общие для всех инструментов."""

    color: QColor
    width: int = 3
    font_size: int = 14
    # Степень уменьшения при размытии: чем больше, тем сильнее эффект.
    blur_factor: int = 12
    # Номер, присваиваемый следующему кружку нумератора.
    step_counter: int = 1

    @staticmethod
    def default() -> "ToolSettings":
        """Параметры по умолчанию: насыщенный красный, средняя толщина."""
        return ToolSettings(color=QColor(230, 60, 60))
