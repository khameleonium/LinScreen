"""
Перечень инструментов редактора аннотаций и их параметры по умолчанию."""

from __future__ import annotations

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
            Tool.ARROW: "Стрелка",
            Tool.RECT: "Рамка",
            Tool.PENCIL: "Карандаш",
            Tool.MARKER: "Маркер",
            Tool.BLUR: "Размытие",
            Tool.TEXT: "Текст",
            Tool.STEP: "Нумератор",
        }[self]

    @property
    def hint(self) -> str:
        """Подсказка о способе применения инструмента."""
        return {
            Tool.ARROW: "Протянуть от начала к цели",
            Tool.RECT: "Протянуть по диагонали",
            Tool.PENCIL: "Рисовать с зажатой кнопкой",
            Tool.MARKER: "Полупрозрачная подсветка",
            Tool.BLUR: "Выделить область для скрытия",
            Tool.TEXT: "Щелчок и ввод текста",
            Tool.STEP: "Щелчок ставит очередной номер",
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
