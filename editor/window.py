"""
Окно редактора аннотаций.

Окно открывается сразу после снимка и предоставляет набор инструментов,
быстрые действия с результатом и отмену последних правок. Сохранение и
работа с буфером обмена выполняются вызывающей стороной через сигналы:
редактор не знает ни о настройках, ни о путях сохранения.
"""

from __future__ import annotations

from core.i18n import tr

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QGuiApplication,
    QImage,
    QKeyEvent,
    QKeySequence,
    QShowEvent,
)
from PySide6.QtWidgets import (
    QColorDialog,
    QLabel,
    QMainWindow,
    QPushButton,
    QSpinBox,
    QToolBar,
    QWidget,
)

from editor.canvas import AnnotationScene, AnnotationView
from editor.icons import render_tool_icon
from editor.tools import Tool

# Предельная доля экрана, занимаемая окном редактора при открытии.
SCREEN_FRACTION = 0.85


class EditorWindow(QMainWindow):
    """Редактор аннотаций поверх готового снимка."""

    # Запрос быстрого сохранения в настроенный каталог.
    saveRequested = Signal(QImage)
    # Запрос сохранения с выбором пути.
    saveAsRequested = Signal(QImage)
    # Запрос копирования результата в буфер обмена.
    copyRequested = Signal(QImage)

    def __init__(self, image: QImage, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Редактор снимка — {0}×{1}").format(image.width(), image.height()))
        # Закрытое окно уничтожается вместе со сценой и снимком: иначе
        # каждый снимок навсегда оставался бы в памяти приложения.
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        self._scene = AnnotationScene(image, self)
        self._view = AnnotationView(self._scene, self)
        self.setCentralWidget(self._view)

        self._tool_actions: dict[Tool, QAction] = {}
        # Признак однократного вписывания снимка в окно.
        self._fitted = False
        self._build_toolbar()
        self._build_actions_bar()
        self._scene.contentChanged.connect(self._refresh_actions)
        self._scene.imageResized.connect(self._on_image_resized)
        self._resize_to_content(image)
        self._build_shortcuts()

    # ------------------------------------------------------------- интерфейс

    def _build_toolbar(self) -> None:
        """Панель выбора инструмента и параметров рисования."""
        toolbar = QToolBar(tr("Инструменты"), self)
        toolbar.setMovable(False)
        toolbar.setIconSize(QSize(22, 22))
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, toolbar)

        # Инструменты объединены в группу: активным может быть только один.
        group = QActionGroup(self)
        group.setExclusive(True)
        for position, tool in enumerate(Tool, start=1):
            action = QAction(tool.label, self)
            action.setIcon(render_tool_icon(tool))
            action.setCheckable(True)
            # Подсказка содержит название инструмента, способ применения и клавишу вызова.
            action.setToolTip(
                f"{tool.label} — {tr('{0} (клавиша {1})').format(tool.hint, position)}"
            )
            action.triggered.connect(lambda _checked=False, t=tool: self._select_tool(t))
            group.addAction(action)
            toolbar.addAction(action)
            self._tool_actions[tool] = action
        self._tool_actions[Tool.ARROW].setChecked(True)

        toolbar.addSeparator()

        # Кнопка выбора цвета показывает текущий цвет собственным фоном.
        self._color_button = QPushButton(tr("Цвет"))
        self._color_button.setFixedWidth(90)
        self._color_button.clicked.connect(self._choose_color)
        toolbar.addWidget(self._color_button)
        self._apply_color_button()

        toolbar.addWidget(QLabel(tr("  Толщина ")))
        self._width_spin = QSpinBox()
        self._width_spin.setRange(1, 40)
        self._width_spin.setValue(self._scene.settings.width)
        self._width_spin.valueChanged.connect(self._set_width)
        toolbar.addWidget(self._width_spin)

        toolbar.addWidget(QLabel(tr("  Размер текста ")))
        self._font_spin = QSpinBox()
        self._font_spin.setRange(6, 96)
        self._font_spin.setValue(self._scene.settings.font_size)
        self._font_spin.valueChanged.connect(self._set_font_size)
        toolbar.addWidget(self._font_spin)

    def _build_actions_bar(self) -> None:
        """Панель быстрых действий с готовым изображением."""
        toolbar = QToolBar(tr("Действия"), self)
        toolbar.setMovable(False)
        self.addToolBar(Qt.ToolBarArea.BottomToolBarArea, toolbar)

        self._undo_action = QAction(tr("Отменить"), self)
        self._undo_action.setShortcut(QKeySequence.StandardKey.Undo)
        self._undo_action.triggered.connect(self._scene.undo)
        toolbar.addAction(self._undo_action)

        self._redo_action = QAction(tr("Вернуть"), self)
        self._redo_action.setShortcut(QKeySequence.StandardKey.Redo)
        self._redo_action.triggered.connect(self._scene.redo)
        toolbar.addAction(self._redo_action)

        clear_action = QAction(tr("Очистить"), self)
        clear_action.triggered.connect(self._scene.clear_annotations)
        toolbar.addAction(clear_action)

        toolbar.addSeparator()

        copy_action = QAction(tr("Копировать"), self)
        copy_action.setShortcut(QKeySequence.StandardKey.Copy)
        copy_action.triggered.connect(self._emit_copy)
        toolbar.addAction(copy_action)

        save_action = QAction(tr("Сохранить"), self)
        save_action.setShortcut(QKeySequence.StandardKey.Save)
        save_action.triggered.connect(self._emit_save)
        toolbar.addAction(save_action)

        save_as_action = QAction(tr("Сохранить как…"), self)
        save_as_action.setShortcut(QKeySequence.StandardKey.SaveAs)
        save_as_action.triggered.connect(self._emit_save_as)
        toolbar.addAction(save_as_action)

        close_action = QAction(tr("Закрыть"), self)
        close_action.setShortcut(QKeySequence(Qt.Key.Key_Escape))
        close_action.triggered.connect(self._handle_close_or_cancel)
        toolbar.addAction(close_action)

        self._refresh_actions()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        """Подтверждение кадрирования по клавишам Enter / Return."""
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self._scene.is_cropping:
                self._scene.confirm_crop()
                event.accept()
                return
        super().keyPressEvent(event)

    def _handle_close_or_cancel(self) -> None:
        """Отмена кадрирования или закрытие окна при нажатии Esc."""
        if self._scene.is_cropping:
            self._scene.cancel_crop()
        else:
            self.close()

    def _on_image_resized(self, size: QSize) -> None:
        """Обновление заголовка окна и подгонка масштаба при изменении размера снимка."""
        self.setWindowTitle(tr("Редактор снимка — {0}×{1}").format(size.width(), size.height()))
        self._view.fit_to_window()

    def _build_shortcuts(self) -> None:
        """Клавиши выбора инструмента и удаления выбранной аннотации."""
        # Инструменты переключаются цифрами в порядке их следования
        # на панели, что заметно ускоряет разметку.
        for position, tool in enumerate(Tool, start=1):
            action = QAction(self)
            action.setShortcut(QKeySequence(str(position)))
            action.triggered.connect(lambda _checked=False, t=tool: self._activate_tool(t))
            self.addAction(action)

        remove = QAction(self)
        remove.setShortcut(QKeySequence(QKeySequence.StandardKey.Delete))
        remove.triggered.connect(self._scene.remove_selected)
        self.addAction(remove)

    def _activate_tool(self, tool: Tool) -> None:
        """Выбор инструмента с отметкой соответствующей кнопки панели."""
        self._tool_actions[tool].setChecked(True)
        self._select_tool(tool)

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - имя из Qt
        """Вписывание снимка в окно при первом показе."""
        super().showEvent(event)
        if not self._fitted:
            # Крупный снимок иначе показывается фрагментом в натуральную
            # величину, и разметить его целиком невозможно.
            self._fitted = True
            self._view.fit_to_window()

    def _resize_to_content(self, image: QImage) -> None:
        """Подбор размеров окна под снимок с учётом размеров экрана."""
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            self.resize(image.size())
            return
        area = screen.availableGeometry()
        width = min(image.width() + 40, int(area.width() * SCREEN_FRACTION))
        height = min(image.height() + 140, int(area.height() * SCREEN_FRACTION))
        self.resize(max(640, width), max(480, height))

    # --------------------------------------------------------------- действия

    def _select_tool(self, tool: Tool) -> None:
        """Переключение активного инструмента."""
        self._scene.tool = tool

    def _choose_color(self) -> None:
        """Выбор цвета рисования."""
        chosen = QColorDialog.getColor(self._scene.settings.color, self, tr("Цвет аннотаций"))
        if chosen.isValid():
            self._scene.settings.color = chosen
            self._apply_color_button()

    def _apply_color_button(self) -> None:
        """Отображение текущего цвета на кнопке выбора."""
        color: QColor = self._scene.settings.color
        # Подпись подбирается контрастной к фону кнопки.
        text_color = "#000000" if color.lightness() > 140 else "#ffffff"
        self._color_button.setStyleSheet(f"background-color: {color.name()}; color: {text_color};")

    def _set_width(self, value: int) -> None:
        """Изменение толщины линии."""
        self._scene.settings.width = value

    def _set_font_size(self, value: int) -> None:
        """Изменение размера шрифта надписей."""
        self._scene.settings.font_size = value

    def _refresh_actions(self) -> None:
        """Согласование доступности кнопок отмены и возврата."""
        self._undo_action.setEnabled(self._scene.can_undo)
        self._redo_action.setEnabled(self._scene.can_redo)

    def result_image(self) -> QImage:
        """Готовое изображение со всеми аннотациями."""
        return self._scene.render_result()

    def _emit_copy(self) -> None:
        """Копирование результата с закрытием окна."""
        self.copyRequested.emit(self.result_image())
        self.close()

    def _emit_save(self) -> None:
        """Быстрое сохранение с закрытием окна."""
        self.saveRequested.emit(self.result_image())
        self.close()

    def _emit_save_as(self) -> None:
        """Сохранение с выбором пути. Окно закрывается вызывающей стороной."""
        # Диалог выбора файла показывает вызывающая сторона, и до его
        # закрытия результат неизвестен, поэтому окно остаётся открытым.
        self.saveAsRequested.emit(self.result_image())
