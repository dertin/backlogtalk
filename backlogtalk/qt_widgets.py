"""Reusable Qt presentation components for BacklogTalk."""

from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from backlogtalk import core

STYLE = """
QWidget { color: #e5eaf1; font-family: "Inter", "Segoe UI", sans-serif; font-size: 13px; }
QDialog#instructionsDialog { background: #151c24; }
QLabel { background: transparent; }
QLabel#title { font-size: 20px; font-weight: 700; color: #f2f5f9; }
QLabel#muted { color: #98a7b7; }
QLabel#eyebrow { font-size: 10px; font-weight: 700; letter-spacing: 1px; color: #8b9bad; }
QLabel#sectionTitle { font-size: 13px; font-weight: 700; color: #eef4fa; }
QFrame#card { background: transparent; border: 0; }
QFrame#header { background: transparent; border: 0; }
QFrame#sidebar { background: #11171e; border: 1px solid #252f3a; border-radius: 9px; }
QFrame#surface { background: #1b232e; border: 1px solid #313d4b; border-radius: 9px; }
QFrame#recordSurface { background: #1b2830; border: 1px solid #35545a; border-radius: 9px; }
QPushButton { background: #273342; border: 1px solid #445268; border-radius: 5px; padding: 8px 13px; font-weight: 600; }
QPushButton:hover { background: #35465a; border-color: #71849a; }
QPushButton:pressed { background: #1e2a38; }
QPushButton:disabled { color: #75818e; background: #222b35; border-color: #313c49; }
QPushButton#primary { background: #3974d8; border: 1px solid #548de9; color: white; }
QPushButton#primary:hover { background: #4a85eb; }
QPushButton#record { background: #63ddc4; border: 1px solid #85ecda; color: #102e31; }
QPushButton#record:hover { background: #96eddb; }
QPushButton#danger { color: #ffb6b6; background: #35282d; border: 1px solid #634348; }
QPushButton#danger:hover { background: #55343b; }
QPushButton#nav { border: 0; border-radius: 5px; background: transparent; color: #b3bfcd; text-align: left; padding: 9px 12px; }
QPushButton#nav:hover { background: #202b38; color: white; }
QPushButton#nav:checked { background: #263849; color: #f2fbff; }
QPushButton#window { border: 0; background: transparent; padding: 1px; font-size: 19px; color: #aab9c9; }
QPushButton#window:hover { background: #34445c; color: white; }
QLineEdit, QTextEdit, QListWidget, QTableWidget, QComboBox { background: #151c24; color: #e9eff6; border: 1px solid #3b4958; border-radius: 5px; padding: 8px; selection-background-color: #355d87; }
QLineEdit:focus, QTextEdit:focus, QListWidget:focus, QComboBox:focus { border-color: #6bcabf; }
QHeaderView::section { background: #273342; color: #dce7f1; padding: 8px; border: 0; border-bottom: 1px solid #445268; }
QTableWidget { gridline-color: #303b47; alternate-background-color: #1b2631; }
QComboBox#sourceSelector { background: #26363f; border: 1px solid #3e5b60; color: #bceee5; padding: 5px 7px; font-size: 12px; combobox-popup: 0; }
QComboBox::drop-down { border: 0; width: 26px; }
QComboBox::down-arrow { image: url("__CHEVRON__"); width: 14px; height: 9px; }
QComboBox QAbstractItemView { background: #202b37; selection-background-color: #35516d; }
QListWidget::item { padding: 12px 9px; border-bottom: 1px solid #303b47; }
QListWidget::item:hover { background: #243341; }
QListWidget::item:selected { background: #315170; border-radius: 3px; color: white; }
QProgressBar { background: #33434d; border: 0; border-radius: 3px; max-height: 6px; }
QProgressBar::chunk { background: #70dfc8; border-radius: 3px; }
QMenu { background: #202a35; color: #e1e6ef; border: 1px solid #46515f; padding: 5px; }
QMenu::item { padding: 8px 24px 8px 12px; border-radius: 5px; }
QMenu::item:selected { background: #354454; }
QScrollBar:vertical { background: transparent; width: 10px; }
QScrollBar::handle:vertical { background: #526785; border-radius: 5px; min-height: 20px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
""".replace(
    "__CHEVRON__", (Path(__file__).resolve().parent.parent / "assets" / "chevron.svg").as_posix()
)


def button(text, callback, kind=None):
    result = QPushButton(text)
    if kind:
        result.setObjectName(kind)
    result.clicked.connect(callback)
    return result


def label(text, name=None):
    result = QLabel(text)
    result.setWordWrap(True)
    if name:
        result.setObjectName(name)
    return result


class ClickComboBox(QComboBox):
    """Scrolling over a closed selector must not change its value."""

    def wheelEvent(self, event):
        if self.view().isVisible():
            super().wheelEvent(event)
        else:
            event.ignore()


class SelectableList(QListWidget):
    delete_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.delete_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)


def navigation_icon(kind):
    """Small, scalable line icons drawn locally rather than font-dependent glyphs."""
    pixmap = QPixmap(36, 36)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor("#9eb4c7"))
    pen.setWidthF(2)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    if kind == 0:
        painter.drawRoundedRect(QRectF(13, 5, 10, 17), 5, 5)
        painter.drawArc(QRectF(9, 10, 18, 18), 180 * 16, 180 * 16)
        painter.drawLine(18, 28, 18, 32)
        painter.drawLine(13, 32, 23, 32)
    elif kind == 1:
        painter.drawRoundedRect(QRectF(7, 6, 22, 24), 3, 3)
        painter.drawLine(12, 13, 24, 13)
        painter.drawLine(12, 18, 24, 18)
        painter.drawLine(12, 23, 21, 23)
    elif kind == 2:
        painter.drawEllipse(QRectF(6, 6, 24, 24))
        painter.drawLine(18, 11, 18, 18)
        painter.drawLine(18, 18, 23, 21)
    else:
        painter.drawEllipse(QRectF(8, 8, 20, 20))
        painter.drawEllipse(QRectF(14, 14, 8, 8))
        for x1, y1, x2, y2 in [
            (18, 3, 18, 8),
            (18, 28, 18, 33),
            (3, 18, 8, 18),
            (28, 18, 33, 18),
            (7, 7, 10, 10),
            (26, 26, 29, 29),
            (7, 29, 10, 26),
            (26, 10, 29, 7),
        ]:
            painter.drawLine(x1, y1, x2, y2)
    painter.end()
    return QIcon(pixmap)


def tray_icon(recording: bool = False) -> QIcon:
    """Render independent tray pixmaps so each state has its own image."""
    source = QIcon(str(core.ROOT / "assets/backlogtalk-mark.svg"))
    icon = QIcon()
    for size in (16, 20, 24, 32, 40, 48, 64, 96, 128):
        pixmap = source.pixmap(size, size).copy()
        if recording:
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            pen = QPen(QColor("#12315d"))
            pen.setWidthF(max(1.0, size * 0.045))
            painter.setPen(pen)
            painter.setBrush(QColor("#ff4356"))
            painter.drawEllipse(QRectF(size * 0.59, size * 0.59, size * 0.37, size * 0.37))
            painter.end()
        icon.addPixmap(pixmap)
    return icon


def recording_tray_icon() -> QIcon:
    return tray_icon(recording=True)


class AudioWave(QWidget):
    """An idle waveform that responds to the current microphone level."""

    def __init__(self):
        super().__init__()
        self.level = 0
        self.active = False
        self.setMinimumSize(105, 44)

    def set_level(self, level, active):
        self.level = level
        self.active = active
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        heights = (6, 10, 15, 9, 21, 13, 25, 15, 20, 9, 15, 8, 12)
        step = self.width() / len(heights)
        center = self.height() / 2
        for i, height in enumerate(heights):
            amplitude = height * (0.55 + self.level / 85) if self.active else height * 0.55
            painter.setPen(
                QPen(
                    QColor("#78e4d1" if self.active else "#537d7c"),
                    3,
                    Qt.PenStyle.SolidLine,
                    Qt.PenCapStyle.RoundCap,
                )
            )
            x = int(step * (i + 0.5))
            painter.drawLine(x, int(center - amplitude / 2), x, int(center + amplitude / 2))


class Header(QFrame):
    def __init__(self, window):
        super().__init__()
        self.window = window
        self.setObjectName("header")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 3, 8, 3)
        layout.setSpacing(10)
        mark = QLabel()
        mark.setPixmap(QIcon(str(core.ROOT / "assets/backlogtalk-mark.svg")).pixmap(36, 36))
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setFixedSize(36, 36)
        layout.addWidget(mark)
        stack = QVBoxLayout()
        stack.setSpacing(1)
        brand = QLabel("BacklogTalk")
        brand.setStyleSheet("font-size:15px; font-weight:700; color:#f5f7ff;")
        tagline = label("DE TU VOZ AL BACKLOG", "eyebrow")
        tagline.setWordWrap(False)
        stack.addWidget(brand)
        stack.addWidget(tagline)
        layout.addLayout(stack)
        layout.addStretch()
        for text, callback, tip in [
            ("−", window.showMinimized, "Minimizar"),
            ("□", window.toggle_maximize, "Maximizar o restaurar"),
            ("×", window.close, "Cerrar"),
        ]:
            b = button(text, callback, "window")
            b.setToolTip(tip)
            b.setFixedSize(32, 30)
            layout.addWidget(b)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.window.windowHandle():
            self.window.windowHandle().startSystemMove()
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.window.toggle_maximize()
        super().mouseDoubleClickEvent(event)


class CapturePopup(QWidget):
    """Compact recording surface; it never owns the note editor."""

    def __init__(self, window):
        super().__init__(
            None,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.window = window
        self.started = 0.0
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedWidth(340)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        row = QHBoxLayout()
        self.title = label("●  GRABANDO", "eyebrow")
        row.addWidget(self.title)
        row.addStretch()
        self.clock = label("00:00", "muted")
        row.addWidget(self.clock)
        layout.addLayout(row)
        self.message = label("Grabando. Guardá o cancelá cuando quieras.", "muted")
        layout.addWidget(self.message)
        self.level = QProgressBar()
        self.level.setRange(0, 100)
        self.level.setTextVisible(False)
        layout.addWidget(self.level)
        row = QHBoxLayout()
        self.action = button("Detener y guardar", self.stop_or_close, "record")
        row.addWidget(self.action)
        self.cancel = button("Cancelar", self.cancel_recording)
        row.addWidget(self.cancel)
        layout.addLayout(row)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 16, 16)
        painter.fillPath(path, QColor("#1b222b"))
        painter.setPen(QColor("#536270"))
        painter.drawPath(path)

    def recording_started(self):
        self.started = time.monotonic()
        self.title.setText(
            "●  MIC + SALIDA"
            if self.window.recording_source == "system"
            else "●  GRABANDO MICRÓFONO"
        )
        self.clock.setText("00:00")
        self.message.setText("Grabando. Guardá o cancelá cuando quieras.")
        self.level.show()
        self.action.setText("Guardar")
        self.cancel.show()
        self.cancel.setEnabled(True)
        self.show()
        self.raise_()

    def update_recording(self, level):
        elapsed = int(time.monotonic() - self.started)
        self.clock.setText(f"{elapsed // 60:02d}:{elapsed % 60:02d}")
        self.level.setValue(level)

    def saved(self, capture_id):
        self.title.setText("✓  AUDIO GUARDADO")
        self.clock.setText(f"#{capture_id}")
        self.message.setText("Preparando y transcribiendo en segundo plano…")
        self.level.hide()
        self.action.setText("Ocultar")
        self.cancel.hide()
        if self.window.tray_available() or self.window.isVisible():
            QTimer.singleShot(2500, self.hide_if_not_recording)

    def transcription_finished(self, success):
        if self.window.recording:
            return
        self.title.setText("✓  NOTA GUARDADA" if success else "!  AUDIO GUARDADO")
        self.message.setText(
            "Transcripción lista en Historial."
            if success
            else "No se pudo transcribir. Revisá Historial."
        )
        if self.window.capture_only and not self.window.tray_available():
            self.action.setText("Cerrar")
            self.show()
        else:
            QTimer.singleShot(2500, self.hide_if_not_recording)

    def hide_if_not_recording(self):
        if not self.window.recording:
            self.hide()

    def cancel_recording(self):
        if self.window.recording:
            self.window.cancel_record()

    def stop_or_close(self):
        if self.window.recording:
            self.window.stop_record()
            return
        if self.window.capture_only and not self.window.tray_available() and self.window.pending:
            self.message.setText(
                "La transcripción sigue en curso. Abrí BacklogTalk desde el menú de aplicaciones para ver el historial."
            )
            return
        self.hide()
        if self.window.capture_only and not self.window.tray_available():
            self.window.request_exit()

    def closeEvent(self, event):
        event.ignore()
        self.stop_or_close()
